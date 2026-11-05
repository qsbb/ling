"""Identity 域：用户、登录会话、自然人、渠道账号绑定。

唯一写入方。对外只暴露 ``subject_id`` 与证明等级；渠道账号不等于自然人。
"""

from __future__ import annotations

import logging
import threading
import time
from asyncio import to_thread
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.errors import LingError, unauthorized
from ling.contracts.v1 import ErrorCode
from ling.infra.models import ActorAccount, AuthSession, Subject, User, utcnow
from ling.infra.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    hash_token,
    new_id,
    new_token,
    verify_password,
)

logger = logging.getLogger("ling.domains.identity")


def _security_event(
    action: str,
    *,
    account_ref: str | None = None,
    reason_code: str = "",
    remote: str = "",
    attributes: dict[str, object] | None = None,
) -> None:
    """薄封装 ``ling.audit.security_event``：函数内 import 保持原有导入方向。

    域模块在导入期不碰 audit（原来就是这么写的），这里只是把三处调用点
    收成一个，免得每处都重复一遍 ``outcome="denied"``。
    """
    from ling.audit import security_event

    security_event(
        action,
        outcome="denied",
        account_ref=account_ref,
        reason_code=reason_code,
        remote=remote,
        attributes=attributes,
    )


class LoginRateLimiter:
    """登录失败限速：per-IP + per-username 计数与指数退避（**进程内内存结构**）。

    ## 为什么需要

    原来失败的登录只记一条安全日志（``auth_failed``），不累计、不阻断：
    一个脚本可以在几秒内把常见口令全试一遍。scrypt 让**每次**尝试都变贵
    （约 16ms CPU），但那是"拖慢"，不是"挡住"——单 worker 部署下这还是
    一条把事件循环压死的路子。

    ## 语义诚实：这是**每个副本各自一份**的计数

    状态存在进程内存里，**不共享**。单进程部署（``entrypoint.sh`` 的 uvicorn
    没带 ``--workers``）下它等于全局；一旦起了多副本/多 worker，每个副本各有
    自己的计数，实际阈值会变成 ``副本数 × 阈值``，而锁定期也不会跨副本生效。
    要真正的全局限速，得把计数挪到共享存储（本仓库现成的 PostgreSQL 就行，
    ``infra/coordination.py`` 已是同一路数）。这里刻意不假装它是全局的。

    同理，**进程重启会清空计数**——正在被爆破的账号在重启后重新获得
    ``FAILURE_THRESHOLD`` 次机会。这是内存实现的已知代价。

    ## 策略

    - 同一个键（IP 或用户名）失败满 ``FAILURE_THRESHOLD`` 次后开始锁定：
      第 5 次失败后锁 ``BASE_LOCK_SECONDS``，之后每多失败一次翻倍，
      上限 ``MAX_LOCK_SECONDS``。
    - **锁定期间不再累计、也不延长**锁定：被锁的人等到窗口过去就能重试。
      若在锁定期继续累加，一次误锁会被后续无关请求越推越远（真用户永远
      出不来），而攻击者本来也拿不到东西。
    - 成功登录清零该用户名与来源 IP 的计数。
    - 计数有 ``IDLE_RESET_SECONDS`` 的静默衰减：一小时没再失败就从零开始，
      免得"半年前错了 4 次"永久占着额度。
    - 键数量上限 ``MAX_TRACKED_KEYS``：用户名/IP 都由外部输入决定，
      不设上限就是"任何人对任何名字发请求"的内存放大器。

    ## 已知取舍（写清楚，不藏）

    - **per-username 锁定可被用来做定向骚扰**：知道用户名的人连错 5 次，
      就能让真用户 60 秒登不上（指数增长后更久）。个人部署里这是可接受的
      代价（受害者是自己，且窗口会自动过去）；要彻底避免就得改成"只对
      同一 IP 的该用户名计数"，那又让轮换 IP 的爆破绕过阈值。两个都留。
    - IP 取自 ``request.client.host``。uvicorn 带
      ``--proxy-headers --forwarded-allow-ips='*'``，会按 ``X-Forwarded-For``
      改写它——**没有可信反代时 XFF 可被伪造**，per-IP 那一路就形同虚设；
      per-username 那一路仍然有效。
    - 被锁时**不做 scrypt**（否则爆破者能用锁定状态放大 CPU 消耗）。
      于是"被锁"与"口令错"在**耗时**上仍可区分；错误体形状一致，但
      攻击者能看出自己被锁了。这是为了让限速本身不成为 DoS 入口而接受的。
    """

    FAILURE_THRESHOLD = 5
    IP_FAILURE_THRESHOLD = 15
    """per-IP 的阈值更高：同一个出口 IP 后面可能是整栋楼（NAT/CGNAT），
    按 5 次锁一整个 IP 会把无关的人一起挡在门外。用户名那一路仍是 5 次。"""

    BASE_LOCK_SECONDS = 60.0
    MAX_LOCK_SECONDS = 3600.0
    IDLE_RESET_SECONDS = 3600.0
    MAX_TRACKED_KEYS = 4096

    def __init__(self) -> None:
        # 计数在 async 世界里也会被 to_thread / 多请求并发访问，必须加锁。
        # 临界区只有几次字典读写（无 IO、无 await），用 threading.Lock
        # 就够，不需要 asyncio.Lock（那会把同步方法变成异步接口）。
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}
        self._last_seen: dict[str, float] = {}

    # -------------------------------------------------- 键

    @staticmethod
    def keys_for(username: str, ip: str | None) -> tuple[str, ...]:
        """由用户名与来源 IP 组成计数的键集合。

        用户名**转小写**：登录名本身不区分大小写（``change_username`` 的重名
        检查就是这么判的），按原样计数会让 ``Ling`` / ``ling`` 各占一份额度。
        """
        keys = [f"user:{username.strip().lower()}"]
        if ip:
            keys.append(f"ip:{ip}")
        return tuple(keys)

    # -------------------------------------------------- 查询

    def retry_after(self, keys: tuple[str, ...]) -> float:
        """还有多久才能再试。0 表示没被锁。"""
        now = time.monotonic()
        with self._lock:
            remaining = 0.0
            for key in keys:
                until = self._locked_until.get(key, 0.0)
                if until > now:
                    remaining = max(remaining, until - now)
            return remaining

    # -------------------------------------------------- 记账

    def record_failure(self, keys: tuple[str, ...]) -> float:
        """记一次失败，返回**新的**锁定时长（0 = 尚未锁定）。

        调用方拿它去记日志；错误响应本身绝不透露锁定状态。
        """
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            lock_for = 0.0
            for key in keys:
                if self._locked_until.get(key, 0.0) > now:
                    # 已在锁定期：不累计、不延长（见类注释）
                    continue
                count = self._failures.get(key, 0) + 1
                self._failures[key] = count
                self._last_seen[key] = now
                threshold = (
                    self.IP_FAILURE_THRESHOLD
                    if key.startswith("ip:")
                    else self.FAILURE_THRESHOLD
                )
                if count >= threshold:
                    over = count - threshold
                    lock_for = max(
                        lock_for,
                        min(self.BASE_LOCK_SECONDS * (2.0**over), self.MAX_LOCK_SECONDS),
                    )
            if lock_for:
                for key in keys:
                    self._locked_until[key] = now + lock_for
            # 插入之后再压一次容量：只在上循环之前 prune 的话，每轮都会
            # 新塞进一个键、而 prune 的额度已经用满，`len` 会稳定停在
            # 上限+1（实测 4097 vs 4096）。
            self._prune(now)
            return lock_for

    def reset(self, keys: tuple[str, ...]) -> None:
        """成功登录后清零：真实用户不该被自己以前的手滑拖累。"""
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)
                self._locked_until.pop(key, None)
                self._last_seen.pop(key, None)

    def reset_all(self) -> None:
        """清空全部状态（测试用；也用于运维"我把所有人都锁住了"的救急）。"""
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()
            self._last_seen.clear()

    # -------------------------------------------------- 内部

    def _prune(self, now: float) -> None:
        """静默衰减 + 容量上限。**必须在已持有 self._lock 时调用。**"""
        stale = [
            key
            for key, seen in self._last_seen.items()
            if now - seen > self.IDLE_RESET_SECONDS
            and self._locked_until.get(key, 0.0) <= now
        ]
        for key in stale:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)
            self._last_seen.pop(key, None)
        overflow = len(self._failures) - self.MAX_TRACKED_KEYS
        if overflow > 0:
            # 先丢最久没动的（没有 last_seen 的键理论上不存在）
            for key, _ in sorted(
                ((k, self._last_seen.get(k, 0.0)) for k in self._failures),
                key=lambda item: item[1],
            )[:overflow]:
                self._failures.pop(key, None)
                self._locked_until.pop(key, None)
                self._last_seen.pop(key, None)


# 进程级兜底：直接 ``IdentityService(session)`` 构造的调用点（域内测试、
# worker）没有 app.state 可挂，共用这一份。API 路径会显式传入
# ``app.state.login_limiter``（见 api/routes/auth.py），彼此隔离。
_FALLBACK_LIMITER = LoginRateLimiter()


@dataclass(frozen=True)
class UserView:
    id: str
    username: str
    role: str
    subject_id: str
    display_name: str
    # 首次安装的随机口令还没改过
    must_change_password: bool = False


@dataclass(frozen=True)
class LoginResult:
    user: UserView
    token: str
    expires_at: datetime


class IdentityService:
    """账号与登录。口令用 scrypt，令牌只存哈希。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------- 用户

    async def count_users(self) -> int:
        return int((await self._session.execute(select(func.count(User.id)))).scalar_one())

    async def create_user(
        self,
        *,
        username: str,
        password: str,
        role: str = "member",
        display_name: str | None = None,
        subject_id: str | None = None,
        must_change_password: bool = False,
    ) -> UserView:
        existing = (
            await self._session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if existing is not None:
            raise LingError(ErrorCode.conflict, "用户名已存在", status_code=409)
        subject_id = subject_id or new_id("subj")
        subject = await self._session.get(Subject, subject_id)
        if subject is None:
            subject = Subject(
                id=subject_id,
                display_name=display_name or username,
                proof_level="password",
            )
            self._session.add(subject)
        user = User(
            id=new_id("user"),
            username=username,
            password_hash=hash_password(password),
            role=role,
            subject_id=subject_id,
            must_change_password=must_change_password,
        )
        self._session.add(user)
        await self._session.flush()
        return UserView(
            id=user.id,
            username=user.username,
            role=user.role,
            subject_id=user.subject_id,
            display_name=subject.display_name,
            must_change_password=user.must_change_password,
        )

    async def get_user(self, user_id: str) -> UserView | None:
        user = await self._session.get(User, user_id)
        if user is None or user.disabled:
            return None
        subject = await self._session.get(Subject, user.subject_id)
        return UserView(
            id=user.id,
            username=user.username,
            role=user.role,
            subject_id=user.subject_id,
            display_name=subject.display_name if subject else user.username,
            must_change_password=user.must_change_password,
        )

    # ---------------------------------------------------------- 登录

    # 会话有效期缺省值：与 ``Settings.session_ttl_seconds`` 的环境变量默认一致。
    # 只在**没有 settings** 时使用（例如域内直接构造 service 的测试）；
    # 生产路径必须由调用方把 ``settings.session_ttl_seconds`` 传进来。
    DEFAULT_SESSION_TTL_SECONDS = 60 * 60 * 24 * 14

    async def login(
        self,
        username: str,
        password: str,
        *,
        user_agent: str | None = None,
        ttl_seconds: int | None = None,
        client_ip: str | None = None,
        limiter: LoginRateLimiter | None = None,
    ) -> LoginResult:
        """校验口令并落一条会话。

        ``ttl_seconds`` 必须由调用方从 ``Settings.session_ttl_seconds`` 派生
        （``api/routes/auth.py`` 就是这么传的）。原来这里硬编码
        ``timedelta(days=14)``，而配置只被用来设 cookie 的 ``max_age``：
        服务端判定读的是**库里那一列**，于是把 TTL 调小只是让浏览器不再
        自动带 cookie——持有 token 的人（被窃 cookie / 重放 / 直接带
        ``Authorization: Bearer``）照样能用满 14 天；调大则完全无效，
        DB 会先到期。

        ``client_ip`` / ``limiter`` 由 API 层传入，用于失败限速。不传时退回
        进程级兜底计数器（语义仍是"本进程内"，见 ``LoginRateLimiter``）。
        """
        limiter = limiter if limiter is not None else _FALLBACK_LIMITER
        keys = LoginRateLimiter.keys_for(username, client_ip)

        # ① 已被限速：直接拒绝，**不做任何数据库查询、也不做 scrypt**。
        #    错误体与"口令错"完全一致（不告诉攻击者"你被锁了"），
        #    锁定状态只进安全日志。
        if limiter.retry_after(keys) > 0:
            _security_event(
                "auth_failed",
                account_ref=username,
                reason_code="rate_limited",
                remote=client_ip or "",
            )
            raise unauthorized("用户名或口令不正确")

        user = (
            await self._session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()

        # ② 恒定耗时：用户不存在 / 账号被禁用时，也拿**固定 dummy 哈希**跑一次
        #    同等成本的 scrypt。
        #
        #    原来的写法是 `user is None or user.disabled or not verify_password(...)`
        #    ——短路求值让"用户不存在"直接跳过 scrypt，响应快约一个数量级
        #    （实测 16ms vs 亚毫秒），而错误体一模一样：**耗时本身就在回答
        #    "这个账号存在吗"**，等于把账号枚举从错误文案里挪到了时间轴上。
        #    这里先算出要校验的哈希，再统一校验一次，两条路径代价相同。
        stored_hash = (
            user.password_hash
            if user is not None and not user.disabled
            else DUMMY_PASSWORD_HASH
        )
        # ③ scrypt 是同步 C 调用（单次约 16ms）。单 worker 部署下直接在事件
        #    循环里跑，并发 N 次登录就是 N×16ms 的**全站**停顿（连不相关的
        #    SSE 流都会被卡）。丢进线程池，事件循环照常转。
        password_ok = await to_thread(verify_password, password, stored_hash)

        if user is None or user.disabled or not password_ok:
            # 不区分“用户不存在”与“口令错误”，避免账号枚举
            # 安全事件：记"哪个用户名试过 + 结果"，**不记口令**。
            lock_for = limiter.record_failure(keys)
            if lock_for > 0:
                # 达到阈值：只记日志，**不改错误响应**（泄露锁定状态等于
                # 告诉攻击者"这个用户名值得继续试"）。
                logger.warning(
                    "登录失败次数达阈值，已限速：user=%s ip=%s 锁 %.0fs",
                    username,
                    client_ip or "-",
                    lock_for,
                )
                _security_event(
                    "rate_limited",
                    account_ref=username,
                    reason_code="too_many_failures",
                    remote=client_ip or "",
                    attributes={"lock_seconds": int(lock_for)},
                )
            _security_event(
                "auth_failed",
                account_ref=username,
                reason_code="bad_credentials",
                remote=client_ip or "",
            )
            raise unauthorized("用户名或口令不正确")

        # 成功即清零：真实用户不该被自己以前的手滑拖累。
        limiter.reset(keys)
        token = new_token()
        ttl = (
            self.DEFAULT_SESSION_TTL_SECONDS
            if ttl_seconds is None
            else max(1, int(ttl_seconds))
        )
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
        self._session.add(
            AuthSession(
                id=new_id("sess"),
                user_id=user.id,
                token_hash=hash_token(token),
                expires_at=expires_at,
                user_agent=(user_agent or "")[:256] or None,
            )
        )
        await self._session.flush()
        subject = await self._session.get(Subject, user.subject_id)
        return LoginResult(
            user=UserView(
                id=user.id,
                username=user.username,
                role=user.role,
                subject_id=user.subject_id,
                display_name=subject.display_name if subject else user.username,
                must_change_password=user.must_change_password,
            ),
            token=token,
            expires_at=expires_at,
        )

    async def resolve_token(self, token: str) -> UserView | None:
        if not token:
            return None
        row = (
            await self._session.execute(
                select(AuthSession).where(AuthSession.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()
        if row is None or row.revoked_at is not None:
            return None
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            return None
        return await self.get_user(row.user_id)

    async def purge_expired_sessions(self, *, now: datetime | None = None) -> int:
        """删除已过期的登录会话，返回删除条数。

        此前**全仓没有任何清理语句**：`auth_sessions` 只增不减，一次部署
        跑几个月后这张表会无限增长（每行还带一个 token 哈希）。

        只删 ``expires_at <= now`` 的。**不删已吊销但未到期的**：那些行的
        ``revoked_at`` 是审计事实（"谁在何时被吊销/改密踢下线"），
        它们的 ``expires_at`` 也会到，之后自然一起清掉。

        多副本同时跑也无害：`DELETE ... WHERE expires_at <= now` 幂等，
        后跑的那次删 0 行。
        """
        cutoff = now or datetime.now(UTC)
        result = await self._session.execute(
            delete(AuthSession).where(AuthSession.expires_at <= cutoff)
        )
        count = int(getattr(result, "rowcount", 0) or 0)
        if count:
            logger.info("清理已过期登录会话 %d 条", count)
        return count

    async def change_password(
        self, user_id: str, *, current: str, new: str, keep_token: str | None = None
    ) -> None:
        """改密码。校验旧口令；改完清掉 must_change_password 并吊销其它会话。

        为什么要吊销其它会话：改密码的动机通常就是"怀疑泄露"。如果旧 cookie
        还能继续用，改密码就只是个心理安慰。当前这次登录保留，免得刚改完
        就被踢下线。
        """
        user = await self._session.get(User, user_id)
        if user is None or user.disabled:
            raise unauthorized("账号不可用")
        if not verify_password(current, user.password_hash):
            raise unauthorized("当前密码不正确")

        user.password_hash = hash_password(new)
        user.must_change_password = False

        rows = (
            await self._session.execute(
                select(AuthSession).where(AuthSession.user_id == user_id)
            )
        ).scalars()
        keep_hash = hash_token(keep_token) if keep_token else None
        for row in rows:
            if row.revoked_at is not None:
                continue
            if keep_hash is not None and row.token_hash == keep_hash:
                continue
            row.revoked_at = utcnow()
        await self._session.flush()

    async def change_username(
        self, user_id: str, *, current: str, username: str, keep_token: str | None = None
    ) -> str:
        """改登录名。要求当前口令确认，改完**吊销其它会话**。

        为什么要验证当前口令：cookie 被盗时，改名字是"接管账号"的第一步
        （改成攻击者习惯的账号名，后续社工更容易）。只验 session 不够。
        """
        name = username.strip()
        if len(name) < 3 or len(name) > 64:
            raise LingError(ErrorCode.invalid_request, "用户名需 3-64 个字符", status_code=422)

        user = await self._session.get(User, user_id)
        if user is None or user.disabled:
            raise unauthorized("账号不可用")
        if not verify_password(current, user.password_hash):
            raise unauthorized("当前密码不正确")

        # 重名检查要不区分大小写：`Ling` 和 `ling` 登录时看起来一样。
        taken = (
            await self._session.execute(
                select(User).where(
                    func.lower(User.username) == name.lower(), User.id != user_id
                )
            )
        ).scalars().first()
        if taken is not None:
            raise LingError(ErrorCode.invalid_request, "这个用户名已被占用", status_code=409)

        user.username = name
        # 与改密码同一策略：吊销其它会话，**保留当前这次登录**。
        # 不保留的话，用户点"保存"就被踢下线，得重新登录两次。
        keep_hash = hash_token(keep_token) if keep_token else None
        rows = (
            await self._session.execute(
                select(AuthSession).where(AuthSession.user_id == user_id)
            )
        ).scalars()
        for row in rows:
            if row.revoked_at is not None:
                continue
            if keep_hash is not None and row.token_hash == keep_hash:
                continue
            row.revoked_at = utcnow()
        await self._session.flush()
        return name

    async def logout(self, token: str) -> None:
        row = (
            await self._session.execute(
                select(AuthSession).where(AuthSession.token_hash == hash_token(token))
            )
        ).scalar_one_or_none()
        if row is not None and row.revoked_at is None:
            row.revoked_at = utcnow()
            await self._session.flush()

    # ---------------------------------------------------------- 绑定

    async def bind_account(
        self,
        *,
        subject_id: str,
        connector_instance: str,
        external_account_id: str,
        proof: str = "pending",
    ) -> ActorAccount:
        """渠道账号绑定。``proof=pending`` 表示尚未完成控制证明，不能用于跨端召回。"""
        existing = (
            await self._session.execute(
                select(ActorAccount).where(
                    ActorAccount.connector_instance == connector_instance,
                    ActorAccount.external_account_id == external_account_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.subject_id != subject_id:
                raise LingError(
                    ErrorCode.conflict,
                    "该渠道账号已绑定到其他自然人，需先解绑或走人工确认",
                    status_code=409,
                )
            existing.proof = proof
            existing.revoked_at = None
            await self._session.flush()
            return existing
        row = ActorAccount(
            id=new_id("acct"),
            connector_instance=connector_instance,
            external_account_id=external_account_id,
            subject_id=subject_id,
            proof=proof,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    # 可用于身份解析的证明等级。``pending`` 表示尚未完成控制证明，
    # 一个"自称是某账号"的人不能据此获得该 natural person 的身份。
    PROVEN_LEVELS = ("proven", "admin")

    TRUST_OWNER = "owner"
    TRUST_KNOWN = "known"
    TRUST_STRANGER = "stranger"

    async def owner_subject_id(self) -> str | None:
        """主人的 subject_id。

        群聊窗口应当归属**主人**，而不是"谁先说话谁拥有这个群"。
        原来 `_ensure_window` 用发言人的 subject 建群窗口——第一个在群里
        说话的人（可能是陌生人）就成了这个窗口的主人，之后任何读
        `window.subject_id` 的地方（关系、主动推送、意识）都会用错人。
        """
        from ling.infra.models import User

        row = (
            await self._session.execute(
                select(User).where(User.role == "owner").limit(1)
            )
        ).scalars().first()
        return row.subject_id if row is not None else None

    async def trust_level_of(self, subject_id: str) -> str:
        """取某个 subject 的亲疏等级。查不到按最不信任处理。"""
        from ling.infra.models import Subject

        row = await self._session.get(Subject, subject_id)
        return getattr(row, "trust_level", None) or self.TRUST_STRANGER

    # ---------------------------------------------------------- 合并与拆分
    #
    # 设计要点：**合并只加一个指针，不动任何数据。**
    #
    # 一个自然人可能挂多个账号（QQ 大号/小号/微信）。把它们视作同一个人，
    # 有两种做法：
    #   (a) 把 B 的账号、记忆全搬到 A 名下 —— 一行写错就再也分不清谁是谁
    #   (b) B.merged_into = A，解析时跟随指针 —— 数据一个字节都没动
    #
    # 选 (b)。合并是可撤销的视图，不是一次不可逆的资料搬迁：
    # 拆开 = 把指针清掉，B 的账号和记忆立刻回到 B。

    async def canonical_subject(self, subject_id: str) -> str:
        """跟随 merged_into 找到最终的那个 subject。

        带环检测：A→B→A 这种脏数据不能把解析卡死（那是线上事故）。
        """
        from ling.infra.models import Subject

        seen: set[str] = set()
        current = subject_id
        while current and current not in seen:
            seen.add(current)
            row = await self._session.get(Subject, current)
            if row is None:
                return current
            nxt = getattr(row, "merged_into", None)
            if not nxt or nxt == current:
                return current
            current = nxt
        # 成环：停在原地比抛异常好，至少还能说话
        logger.warning("subject 合并链成环: %s", " -> ".join(sorted(seen)))
        return subject_id

    async def subject_group(self, subject_id: str) -> set[str]:
        """这个人当前包含哪些 subject（含被合并进来的）。"""
        from ling.infra.models import Subject

        root = await self.canonical_subject(subject_id)
        group = {root}
        # 一层足够（合并都指向同一个 root），但多跑几轮容得下历史脏数据
        for _ in range(5):
            rows = (
                await self._session.execute(
                    select(Subject).where(
                        Subject.merged_into.in_(list(group)),
                        Subject.merged_into.is_not(None),
                    )
                )
            ).scalars().all()
            added = {r.id for r in rows} - group
            if not added:
                break
            group |= added
        return group

    async def merge_subjects(self, source_id: str, target_id: str) -> dict:
        """把 source 当成 target 的同一个人。**只写一个指针。**

        拒绝三种情况：自己并自己、目标本身是被合并者、会形成环。
        这几种都是"操作者选错了"，早失败比事后串号好。
        """
        from ling.infra.models import Subject

        if source_id == target_id:
            raise ValueError("不能把 subject 合并到自己")
        source = await self._session.get(Subject, source_id)
        target = await self._session.get(Subject, target_id)
        if source is None or target is None:
            raise ValueError("subject 不存在")
        if getattr(target, "merged_into", None):
            raise ValueError("目标已经被合并到别处，请先拆开再合并")
        if await self.canonical_subject(target_id) == await self.canonical_subject(source_id):
            raise ValueError("这两个 subject 已经属于同一个人")

        source.merged_into = target_id
        await self._session.flush()
        logger.info("合并 subject：%s -> %s（数据未搬动，可拆分）", source_id, target_id)
        return {"source_id": source_id, "target_id": target_id, "moved": 0}

    async def unmerge_subject(self, source_id: str) -> dict:
        """把 source 从合并里拆出来。

        除了清指针，还要**把合并期间写的记忆送回原主**：那段时间解析出的
        主体是 target，新记忆记在 target 名下，但 `source_account` 一直写着
        "是哪个号说的"。按它归位，拆开之后两边都不串。
        """
        from sqlalchemy import update

        from ling.infra.models import ActorAccount, MemoryRow, Subject

        source = await self._session.get(Subject, source_id)
        if source is None:
            raise ValueError("subject 不存在")
        previous = getattr(source, "merged_into", None)
        if not previous:
            return {"source_id": source_id, "restored_memories": 0}

        accounts = (
            await self._session.execute(
                select(ActorAccount).where(
                    ActorAccount.subject_id == source_id,
                    ActorAccount.revoked_at.is_(None),
                )
            )
        ).scalars().all()
        labels = [
            f"{a.connector_instance}:{a.external_account_id}" for a in accounts
        ]

        restored = 0
        if labels:
            result = await self._session.execute(
                update(MemoryRow)
                .where(
                    MemoryRow.subject_id != source_id,
                    MemoryRow.source_account.in_(labels),
                )
                .values(subject_id=source_id)
            )
            restored = int(getattr(result, "rowcount", 0) or 0)

        source.merged_into = None
        await self._session.flush()
        logger.info(
            "拆分 subject：%s（从 %s 拆出，归位 %d 条记忆）", source_id, previous, restored
        )
        return {
            "source_id": source_id,
            "was_merged_into": previous,
            "restored_memories": restored,
        }

    async def resolve_or_provision(
        self,
        connector_instance: str,
        external_account_id: str,
        *,
        display_name: str | None = None,
    ) -> str | None:
        """解析身份；解析不到就**自动建一个 known 级 subject**。

        为什么需要：原来只有"已证明"或"不存在"两态，群里没走过绑定流程的人
        整条消息被拒——她只能听见已绑定的人说话，等于在有别人的群里是聋的。

        自动建的不是"已证明的身份"，只是"我认识这个账号了"：
        `proof` 保持 `pending`，`trust_level` 为 `known`，
        记忆只能写 `group_scope`，跨窗召回不开放。

        传空账号返回 None——无法识别的人不建 subject，避免所有缺字段的事件
        挤进同一个 subject 里。
        """
        from ling.infra.models import ActorAccount, Subject

        account = (external_account_id or "").strip()
        if not account:
            return None
        # 已证明的优先（可能是待绑定升级之后的老记录）
        proven = await self.resolve_account(connector_instance, account)
        if proven is not None:
            return proven

        # 已存在但未证明：复用它的 subject，不重复建
        existing = (
            await self._session.execute(
                select(ActorAccount).where(
                    ActorAccount.connector_instance == connector_instance,
                    ActorAccount.external_account_id == account,
                    ActorAccount.revoked_at.is_(None),
                )
            )
        ).scalars().first()
        if existing is not None:
            return existing.subject_id

        import uuid as _uuid

        subject_id = f"subj_{_uuid.uuid4().hex[:24]}"
        self._session.add(
            Subject(
                id=subject_id,
                # 名字只是标签：同名不同人靠 subject_id 区分，绝不靠名字。
                display_name=(display_name or "群友")[:128],
                proof_level="password",
                trust_level=self.TRUST_KNOWN,
            )
        )
        await self._session.flush()  # 先落 subject，再落引用它的账号（外键顺序）
        self._session.add(
            ActorAccount(
                id=f"acct_{_uuid.uuid4().hex[:24]}",
                connector_instance=connector_instance,
                external_account_id=account,
                subject_id=subject_id,
                proof="pending",
            )
        )
        await self._session.flush()
        return subject_id

    async def revoke_account(self, account_id: str) -> ActorAccount:
        """解绑（撤销）渠道账号。

        解绑不是删除记录：保留 ``revoked_at`` 便于审计"谁在何时解绑过"，
        也让误绑拆分有痕迹。解绑后 ``resolve_account`` 不再返回该 subject。
        """
        from ling.infra.models import utcnow

        row = await self._session.get(ActorAccount, account_id)
        if row is None:
            raise LingError(ErrorCode.not_found, "账号绑定不存在", status_code=404)
        row.revoked_at = utcnow()
        await self._session.flush()
        return row

    async def resolve_account(
        self,
        connector_instance: str,
        external_account_id: str,
        *,
        require_proven: bool = True,
    ) -> str | None:
        """把渠道账号解析为 subject_id。

        ``require_proven=True``（默认）时只接受已完成控制证明的绑定——
        这正是 ingress 注释里写的"解析到**已证明的** subject_id"。

        之前实现只检查 ``revoked_at``，没检查 ``proof``，
        于是 ``proof=pending`` 的绑定（仅"自称"该账号）也会被解析成
        真实的 subject_id，进而拿到该人的私聊记忆与跨端召回资格。
        实现与自身文档不一致，是真实缺口。

        需要知道"这个账号是否存在但未证明"时传 ``require_proven=False``
        （例如待绑定队列要让用户看到自己的申请状态）。
        """
        stmt = select(ActorAccount).where(
            ActorAccount.connector_instance == connector_instance,
            ActorAccount.external_account_id == external_account_id,
            ActorAccount.revoked_at.is_(None),
        )
        if require_proven:
            stmt = stmt.where(ActorAccount.proof.in_(self.PROVEN_LEVELS))
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        # 跟随合并指针：账号仍挂在原来的 subject 上，但**解析结果**必须是
        # 合并后的那个人——否则合并之后两个号还是两个人。
        return await self.canonical_subject(row.subject_id)

    async def binding_proof(
        self, connector_instance: str, external_account_id: str
    ) -> str | None:
        """查绑定的证明等级（不解析身份，用于待绑定队列与诊断）。"""
        row = (
            await self._session.execute(
                select(ActorAccount).where(
                    ActorAccount.connector_instance == connector_instance,
                    ActorAccount.external_account_id == external_account_id,
                    ActorAccount.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        return row.proof if row is not None else None

    async def list_accounts(self, subject_id: str) -> list[ActorAccount]:
        rows = (
            await self._session.execute(
                select(ActorAccount).where(ActorAccount.subject_id == subject_id)
            )
        ).scalars()
        return list(rows)
