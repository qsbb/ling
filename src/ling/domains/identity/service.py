"""Identity 域：用户、登录会话、自然人、渠道账号绑定。

唯一写入方。对外只暴露 ``subject_id`` 与证明等级；渠道账号不等于自然人。
"""

from __future__ import annotations

import logging
import threading
import time
from asyncio import to_thread
from collections import OrderedDict
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

# 绝不能当成人名写进 ``subjects.display_name`` 的占位符。
#
# 为什么单独列一份：AstrBot 那里有个 ``"N/A"`` 字面量会一路进提示词和库
# （见 docs/GROUP-CARD-RESEARCH.md §A.8），而 ling 的历史遗留是建号时用
# 「群友」兜底。占位符一旦写进库，**"变了才写"就永远治不好它**——因为
# 它看起来就是一个合法的名字。所以入口处直接拒掉。
PLACEHOLDER_DISPLAY_NAMES = frozenset(
    {"群友", "用户", "未知", "有人", "N/A", "n/a", "none", "None", "unknown", "anonymous"}
)

# 进程内"上次见到的名字"缓存：``(connector_instance, account_ref) -> 名字``。
#
# 作用是**挡掉重复写**：群里同一个人连着说话时，第二条消息不该再查库/写库。
# 为什么是进程内而不是落库：这是纯性能旁路，丢了最多多一次 UPDATE（幂等）；
# 多副本各记一份也只是多写一次。上限 4096 条，超了丢最旧的——
# 缓存无限长在长跑进程里就是内存泄漏。
_DISPLAY_NAME_CACHE: OrderedDict[tuple[str, str], str] = OrderedDict()
_DISPLAY_NAME_CACHE_MAX = 4096


def normalize_display_name(value: object) -> str | None:
    """把渠道给的名字清洗成可落库的值；空/占位符返回 ``None``（= 没有名字）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text in PLACEHOLDER_DISPLAY_NAMES:
        return None
    return text[:128]


def is_placeholder_display_name(value: object) -> bool:
    """这个名字是不是占位符（读侧据此决定"不要用它称呼人"）。"""
    if not isinstance(value, str):
        return True
    text = value.strip()
    return not text or text in PLACEHOLDER_DISPLAY_NAMES


def reset_display_name_cache() -> None:
    """清空进程内名字缓存（测试用）。"""
    _DISPLAY_NAME_CACHE.clear()


def _remember_display_name(key: tuple[str, str], name: str) -> None:
    _DISPLAY_NAME_CACHE[key] = name
    _DISPLAY_NAME_CACHE.move_to_end(key)
    while len(_DISPLAY_NAME_CACHE) > _DISPLAY_NAME_CACHE_MAX:
        _DISPLAY_NAME_CACHE.popitem(last=False)


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

    async def observe_display_name(
        self,
        connector_instance: str,
        external_account_id: str,
        *,
        nickname: str | None = None,
        sender_name: str | None = None,
        subject_id: str | None = None,
    ) -> str | None:
        """把这次看到的**昵称**同步进 ``subjects.display_name``；变了才写。

        返回写入的新名字；没写（没变/没名字/没这个账号/写失败）返回 ``None``。

        ## 为什么更新放在这里，而不放进 ``resolve_or_provision``

        ``resolve_or_provision`` **只在"未证明"分支**被调用：已绑定的主人/管理员
        走的是 ``resolve_account``，永远进不了那个函数。把更新塞进去等于
        "主人的昵称改了永远不生效"——这正是 ling 现在"名字固定不变"的一半原因。

        ## 为什么只用**昵称**（不回退群名片）

        ``subjects.display_name`` 是**人级**的一列。群名片是群级字段，
        用它写人级列会让同一个人在 A/B 群之间来回抖动，还会把别人的私聊标题
        一起改掉。群里"此刻该叫这个名片"由当轮上下文负责（bridge 把
        ``envelope.sender_name`` 透传下去），**不进这一列**。

        ## 已知代价（本轮明确接受）

        ``subjects.display_name`` 仍然是**跨群唯一**的一列（名字是"人"的标签，
        不是"人在某个群"的标签）。两条具体后果：

        - 一个人在 A 群改了**昵称**，B 群的称呼、私聊标题会一起变；
        - 更直接的：旧连接器不带 ``sender_nickname`` 时这里会退回 ``sender_name``
          （即 ``card > nickname`` 的群名片）——**最新群名片会覆盖其他群的称呼**。

        真正"每个群一份名字"需要新表（``channel_member_names``），属于下一批
        迁移的事（见 docs/GROUP-CARD-RESEARCH.md §C.5）。这里先不偷偷改成
        每群一份，也把这条代价用
        ``test_known_cost_legacy_fallback_is_shared_across_groups`` 钉住。

        ## 为什么吞掉异常

        改名是**旁路**：失败只意味着"库里的名字还是旧的"，下一条消息会重试。
        让它抛出去会变成"因为改名失败而拒收消息"，那个代价大得多。

        但**光 try/except 不够**：一次失败的 ``flush`` 会把这个 session 的
        事务标成待回滚，调用方随后 commit 会拿到 ``PendingRollbackError``——
        消息照样被拒收，只是换了个地方炸。所以写库那一步包在 SAVEPOINT 里
        （见实现处的注释，有实测对照）。
        """
        account = (external_account_id or "").strip()
        if not account:
            return None
        # 昵称优先；旧连接器不发昵称时退回本次称呼（card>nickname 的合并值），
        # 这样不升级连接器也能工作。
        candidate = normalize_display_name(nickname) or normalize_display_name(sender_name)
        if candidate is None:
            # 空名片 + 空昵称：**保持旧值**。空名不是"改名的信号"，
            # 拿它覆盖只会把好不容易采到的名字擦掉。
            return None

        key = (connector_instance, account)
        if _DISPLAY_NAME_CACHE.get(key) == candidate:
            # 内存 diff 命中：这条消息到此为止，**一次库都不碰**。
            return None

        try:
            if subject_id is None:
                # ``require_proven=False``：群里刚自动建档的群友是 ``proof=pending``，
                # 默认的"只认已证明"会把他们全判成 None，于是**名字永远更新不了**
                # ——那正是这个需求要修的 bug 的翻版。这里改的是显示名（标签），
                # 不涉及任何权限，pending 账号照写。
                subject_id = await self.resolve_account(
                    connector_instance, account, require_proven=False
                )
            if subject_id is None:
                # 还没建号（未证明且未自动注册）。建号是
                # `resolve_or_provision` 的职责，这里不越界；也不写缓存，
                # 让下一条消息重新判定。
                return None
            # 读一行再改，而不是一条 ``UPDATE ... WHERE display_name IS DISTINCT FROM``。
            #
            # 两个理由：
            # 1. 身份域有一条守卫测试禁止**任何** where 里出现名字字段（"绝不按名字
            #    找人"）。虽然这里按 subject_id 定位、名字只用来判"要不要改"，
            #    但把这个形状写出来，下一次就会有人照抄成真正的按名字查找。
            # 2. 读改写在多副本下本身就是幂等的（同名并发写同一个值），
            #    而"变了才写"的第一步（进程内 diff）已经把 99% 的调用挡在外面，
            #    这里多一条 SELECT 只发生在"真的改名了"那一次。
            #
            # 读取 + 更新整体放进一个 **SAVEPOINT**：真写失败（NOT NULL 违约、
            # 连接抖动）只回滚这一步，**不把调用方的事务标成"待回滚"**。
            # 没有 SAVEPOINT 时已实测：flush 抛 IntegrityError 之后，同一个
            # session 的 commit 变成 ``PendingRollbackError`` —— bridge 接着
            # 提交这条消息就会失败，等于「改名失败把用户消息拒了」，正是需求里
            # 明确不允许的那一条。有 SAVEPOINT：commit 照常成功。
            #
            # 这一层的前提：调用点**没有别人的未提交改动**（bridge 的建号/绑定
            # 在调用前已 commit，见 bridge.handle 的身份解析段）。若将来要在
            # 持有未提交改动的会话里调用，``flush()`` 会把那些改动一起卷进
            # savepoint，回滚时 ORM 状态会与库不一致——那时应改成独立 session
            # 的旁路写，不要在这里硬扛。
            async with self._session.begin_nested():
                row = await self._session.get(Subject, subject_id)
                if row is None:
                    return None
                if normalize_display_name(row.display_name) == candidate:
                    # 库里已经是这个名字（可能别的副本先写了）：不产生 UPDATE，
                    # 但缓存照样记下，后续消息不再查库。
                    _remember_display_name(key, candidate)
                    return None
                row.display_name = candidate
                row.updated_at = utcnow()
                await self._session.flush()
        except Exception as exc:  # noqa: BLE001 - 旁路失败绝不能拒收消息
            # 失败就把缓存里的判断**删掉**，下一条消息会重新试；
            # 留着会变成"缓存说写过、其实没写成"。
            _DISPLAY_NAME_CACHE.pop(key, None)
            logger.debug(
                "同步显示名失败（保留旧名，下一条重试）：%s", type(exc).__name__
            )
            return None

        _remember_display_name(key, candidate)
        # 名字是标签不是身份：日志里只说"改名了"，不打印名字本身
        # （群名片是个人信息，仓库的日志规约要求这类内容脱敏）。
        logger.info("渠道昵称已更新（subject 显示名变化）")
        return candidate

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
