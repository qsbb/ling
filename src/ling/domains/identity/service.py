"""Identity 域：用户、登录会话、自然人、渠道账号绑定。

唯一写入方。对外只暴露 ``subject_id`` 与证明等级；渠道账号不等于自然人。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.errors import LingError, unauthorized
from ling.contracts.v1 import ErrorCode
from ling.infra.models import ActorAccount, AuthSession, Subject, User, utcnow
from ling.infra.security import hash_password, hash_token, new_id, new_token, verify_password

logger = logging.getLogger("ling.domains.identity")


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

    async def login(
        self, username: str, password: str, *, user_agent: str | None = None
    ) -> LoginResult:
        user = (
            await self._session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if user is None or user.disabled or not verify_password(password, user.password_hash):
            # 不区分“用户不存在”与“口令错误”，避免账号枚举
            # 安全事件：记"哪个用户名试过 + 结果"，**不记口令**。
            # 也不区分"用户不存在"与"口令错误"（避免账号枚举）。
            from ling.audit import security_event

            security_event(
                "auth_failed",
                outcome="denied",
                account_ref=username,
                reason_code="bad_credentials",
            )
            raise unauthorized("用户名或口令不正确")
        token = new_token()
        expires_at = datetime.now(UTC) + timedelta(days=14)
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
