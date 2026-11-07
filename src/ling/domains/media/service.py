"""媒体证据：图片/音频被看到或被推断出什么。

**唯一写入方**。两条硬规则：

> **1. 只在授权窗口被描述。**
> **2. 图像猜测不写事实。**

第 1 条的实现：记录绑定 ``window_id``，读取时窗口不匹配直接返回 ``None``
（不抛错——抛错会变成"这个 id 存不存在"的探测口）。能否描述由
``media.inbound`` 授权决定，**默认拒绝**："能看图"和"能把你发的图交给
模型描述"是两件事。

第 2 条的实现：``fact_eligible`` 是**计算属性**，不是调用方传进来的布尔。
模型生成的描述（``description_origin=model_inference``）永远为 ``False``。
这样即使将来有人写新代码，也没法"忘了检查"就把猜测当成事实——他必须
主动绕过一个叫 ``fact_eligible`` 的属性名。

为什么不复用 memory：媒体引用的生命周期（``expires_at``）比记忆短，
而且它的可信度决定它能不能进事实。混进 memory 会丢掉"这是猜的还是
看到的"这个区分。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.consent.service import ConsentService, ConsentView
from ling.infra.models import MediaEvidenceRow, Window, utcnow
from ling.infra.security import new_id

# 入站媒体能否被描述，取决于这个授权。默认关闭。
INBOUND_SCOPE = "media.inbound"

KINDS = frozenset({"image", "audio", "video", "emoji"})

# 描述来源。只有 human 的描述可以作为事实。
ORIGIN_HUMAN = "human"
ORIGIN_MODEL = "model_inference"

# 素材来源
ORIGIN_UPLOAD = "user_upload"
ORIGIN_CAMERA = "camera"
ORIGIN_GENERATED = "model_generated"

DEFAULT_VISIBILITY = "window_private"


class MediaEvidenceError(Exception):
    """记录或描述媒体证据时的业务错误。"""


def dedupe_key_for(source_event_id: str) -> str:
    """同一个来源事件只产生一条证据。重复回调不重复记。"""
    return f"media:{source_event_id}"


@dataclass(frozen=True)
class EvidenceView:
    evidence_id: str
    window_id: str
    kind: str
    media_ref: str
    origin: str
    visibility: str
    description: str | None
    description_origin: str | None
    expires_at: datetime | None
    # 登记时间与上传者。上下文里要写「阿懒 12:21 发了个视频，你还没看」——
    # 没有这两项就只能写「有个视频」，她不知道是谁发的、什么时候。
    created_at: datetime | None = None
    subject_id: str | None = None
    # 引用本身不可用的时间与原因（**永久失败终态**）。
    # 有值 = 不再进待办队列；`description` 仍为 None（没看到 ≠ 内容为空）。
    describe_failed_at: datetime | None = None
    describe_failed_reason: str | None = None

    @property
    def fact_eligible(self) -> bool:
        """这条描述能不能当作事实使用。

        **模型推断永远是猜测。** 即使描述看起来再确定，也不能变成
        "用户养了一只猫"这样的记忆。只有人给的描述才够格。
        """
        if self.description is None or not self.description.strip():
            return False
        return self.description_origin != ORIGIN_MODEL

    @property
    def needs_description(self) -> bool:
        """还该不该去描述它。

        **失败终态不算"需要描述"**：引用越界/超限/形式不支持是永久错误，
        重试同一个引用只会得到同一个结果。
        """
        return self.description is None and self.describe_failed_at is None


class MediaEvidenceService:
    """媒体证据的唯一写入方。"""

    def __init__(self, session: AsyncSession, *, actor_id: str) -> None:
        self._session = session
        self._actor_id = actor_id

    # ---------------------------------------------------------------- 写入

    async def record(
        self,
        *,
        subject_id: str | None,
        window_id: str,
        media_ref: str,
        kind: str = "image",
        origin: str = ORIGIN_UPLOAD,
        visibility: str = DEFAULT_VISIBILITY,
        source_event_id: str | None = None,
        expires_at: datetime | None = None,
        consent_subjects: Sequence[str] | None = None,
    ) -> EvidenceView:
        """登记一条媒体证据。

        需要 ``media.inbound`` 授权；没有就拒绝——**不是静默忽略**。
        静默忽略会让"用户发了图但角色没反应"变成一个查不出来的怪现象。

        ``consent_subjects``：除上传者本人之外，**也**算数的授权主体。
        群聊里调用方传会话主人（``windows.subject_id``）——**主人授权一次，
        群里别人发的图也能被描述**。不传就是原来的语义：只认上传者本人。

        为什么不是"全局开关"：授权是**按人**记的（``consent_rules`` 一行一个
        subject），撤权时得知道撤的是谁的。主人授权是"我把这件事的责任揽到
        自己身上"，不是"所有人自动同意"；两者在审计上完全不同。
        """
        if kind not in KINDS:
            raise MediaEvidenceError(f"未知媒体类型: {kind}")
        if not window_id:
            raise MediaEvidenceError("媒体证据必须绑定窗口")
        if not media_ref.strip():
            raise MediaEvidenceError("media_ref 不能为空")
        if subject_id is None:
            raise MediaEvidenceError("入站媒体必须带上传者 subject_id")

        consent = await self._granted_consent([subject_id, *(consent_subjects or ())])
        if consent is None:
            raise MediaEvidenceError(
                f"未授权描述入站媒体（授权记录 scope={INBOUND_SCOPE}）"
            )

        key = dedupe_key_for(source_event_id or media_ref)
        # **去重必须按窗口**。原来全局按 key 查，于是两个窗口收到同一张图时，
        # 后一个窗口拿到的是**前一个窗口的** evidence——它的 window_id 对不上，
        # 描述任务按窗口取待办永远取不到，而且跨窗口引用了别人的证据。
        existing = await self._by_dedupe(key, window_id=window_id)
        if existing is not None:
            return _view(existing)

        row = MediaEvidenceRow(
            id=new_id("media"),
            actor_id=self._actor_id,
            subject_id=subject_id,
            window_id=window_id,
            kind=kind,
            # 列宽 512。契约的 `MediaRef` 上限是 2048（见 contracts/v1.py），
            # 超过 512 的外部引用会在这里被**静默截断**，之后 fetch_media
            # 拿到的是一个坏 URL。真实 QQ 图片 URL 约 300–400 字符，暂时够用；
            # 要根治得加宽这一列（迁移），不要在契约层假装 2048 已完全支持。
            media_ref=media_ref[:512],
            origin=origin,
            visibility=visibility,
            consent_revision=consent.revision,
            description=None,
            description_origin=None,
            source_event_id=source_event_id,
            dedupe_key=key,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return _view(row)

    # ------------------------------------------------------------ 授权判据

    async def _granted_consent(self, subjects: Sequence[str | None]) -> ConsentView | None:
        """这些主体里**第一个**已授权的授权记录；一个都没有就返回 ``None``。

        顺序即优先级：发言人本人（最具体）在前，会话主人在后。返回整条
        记录而不只是布尔，是因为调用方要拿它记 ``consent_revision``——
        必须记**实际生效的那一条**，否则撤权审计时会指到另一个人的授权上。
        """
        service = ConsentService(self._session)
        seen: set[str] = set()
        for subject in subjects:
            if not subject or subject in seen:
                continue
            seen.add(subject)
            consent = await service.get(subject, INBOUND_SCOPE)
            if consent.granted:
                return consent
        return None

    async def _window_owner(self, window_id: str) -> str | None:
        """窗口主人（群窗口的 ``windows.subject_id`` 就是主人，不是发言人）。"""
        window = await self._session.get(Window, window_id)
        return getattr(window, "subject_id", None)

    async def describe(
        self,
        evidence_id: str,
        *,
        window_id: str,
        description: str,
        description_origin: str = ORIGIN_MODEL,
    ) -> EvidenceView:
        """给一条证据写描述。

        窗口不匹配按"不存在"处理（返回错误而不是泄漏存在性）。
        授权在描述时**重新检查**——中途撤权后不能再描述新内容。

        授权主体与登记时同一套规则：上传者本人，**或**这个窗口的主人。
        只在 ``record`` 那一步放宽是不够的：描述发生在 worker 里，那边
        从来拿不到"发言人是谁以外的上下文"，只认上传者本人的话，
        主人授权过的群图仍然会在描述这一步被拒——等于没修。
        """
        row = await self._require(evidence_id, window_id)
        if row.subject_id is None:
            raise MediaEvidenceError("证据没有上传者，无法校验授权")

        consent = await self._granted_consent([row.subject_id, await self._window_owner(window_id)])
        if consent is None:
            raise MediaEvidenceError(
                f"授权已撤销，不能再描述（scope={INBOUND_SCOPE}）"
            )

        text = description.strip()
        if not text:
            raise MediaEvidenceError("描述不能为空")

        row.description = text[:4000]
        row.description_origin = description_origin
        # 撤权后再授权时，以当前修订为准
        row.consent_revision = consent.revision
        # 描述成功 = 之前那个"引用不可用"的终态不再成立（比如文件后来补上了）
        row.describe_failed_at = None
        row.describe_failed_reason = None
        row.updated_at = utcnow()
        await self._session.flush()
        return _view(row)

    async def mark_describe_failed(
        self, evidence_id: str, *, window_id: str, reason: str
    ) -> EvidenceView:
        """把一条证据标成**描述永久失败**（引用越界/超限/形式不支持）。

        ## 为什么必须落库

        `MediaDescribeError.retriable` 原来没有任何读者：`pending()` 只按
        `description IS NULL` 过滤。于是永久错误每一轮都被重新捞出来重试，
        永远重试不完，日志里一条"下载失败"重复到把真问题淹掉。

        终态写进 `describe_failed_at` 之后，`pending()` 会把这类行排除，
        循环真的停下来。**不写进 `description`**：那会把"没看到"伪装成
        "看到了但没内容"，污染证据、也让 `fact_eligible` 误判。

        只由**永久错误**调用；可重试的失败（网络抖动、模型超时）不标，
        照旧每一轮重试。
        """
        row = await self._require(evidence_id, window_id)
        row.describe_failed_at = utcnow()
        row.describe_failed_reason = reason.strip()[:200]
        row.updated_at = utcnow()
        await self._session.flush()
        return _view(row)

    # ---------------------------------------------------------------- 读取

    async def get(self, evidence_id: str, *, window_id: str) -> EvidenceView | None:
        """按窗口读取。窗口不匹配或已过期都返回 ``None``。

        返回 ``None`` 而不是抛错，是为了不给"这个 id 在别的窗口存不存在"
        留探测口。
        """
        row = await self._load(evidence_id)
        if row is None or row.window_id != window_id:
            return None
        if _expired(row):
            return None
        return _view(row)

    async def pending(
        self,
        *,
        window_id: str,
        limit: int = 20,
        exclude_kinds: Sequence[str] = (),
    ) -> list[EvidenceView]:
        """当前窗口里**还没描述**且在有效期内的证据。给描述任务用。

        **排除已标成永久失败的行**（`describe_failed_at` 非空）：引用越界/
        超限/形式不支持不会自己变好，每轮重新捞出来重试就是无限重试。
        可重试的失败不写这个字段，所以照旧留在队列里。

        ``exclude_kinds``：**视频必须被排除**。视频只在"她主动说要看"时
        才读（见 ``domains/media/video.py``）——自动描述会把群里每一段
        视频都送进视频模型，那是白烧 token，而且她当时可能根本用不上。
        """
        conditions = [
            MediaEvidenceRow.actor_id == self._actor_id,
            MediaEvidenceRow.window_id == window_id,
            MediaEvidenceRow.description.is_(None),
            MediaEvidenceRow.describe_failed_at.is_(None),
        ]
        if exclude_kinds:
            conditions.append(MediaEvidenceRow.kind.not_in(list(exclude_kinds)))
        rows = (
            (
                await self._session.execute(
                    select(MediaEvidenceRow)
                    .where(*conditions)
                    .order_by(MediaEvidenceRow.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_view(row) for row in rows if not _expired(row)]

    async def described_in_window(
        self, *, window_id: str, limit: int = 5
    ) -> list[EvidenceView]:
        """本窗口里**已经有描述**且在有效期内的证据，最近的在前。

        给上下文装配用。注意返回的是原始视图：调用方必须自己看
        ``description_origin`` 决定怎么标注——**模型推断不能当事实写进提示**，
        但可以作为"我看到的（不确定）"参与对话。
        """
        rows = (
            (
                await self._session.execute(
                    select(MediaEvidenceRow)
                    .where(
                        MediaEvidenceRow.actor_id == self._actor_id,
                        MediaEvidenceRow.window_id == window_id,
                        MediaEvidenceRow.description.is_not(None),
                    )
                    .order_by(MediaEvidenceRow.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_view(row) for row in rows if not _expired(row)]

    async def undescribed_videos(self, *, window_id: str, limit: int = 5) -> list[EvidenceView]:
        """本窗口里**还没看过**的视频（``description IS NULL``），最早的在前。

        上下文要用它写「你还没看」那一行：视频默认不自动读，所以这条提示
        是她**唯一**知道"有个视频可以看"的途径。已标永久失败的也列出来
        （看不了 ≠ 不知道有这回事），但重复提示会被 ``describe_failed`` 的原因说明。
        """
        rows = (
            (
                await self._session.execute(
                    select(MediaEvidenceRow)
                    .where(
                        MediaEvidenceRow.actor_id == self._actor_id,
                        MediaEvidenceRow.window_id == window_id,
                        MediaEvidenceRow.kind == "video",
                        MediaEvidenceRow.description.is_(None),
                    )
                    .order_by(MediaEvidenceRow.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_view(row) for row in rows if not _expired(row)]

    async def windows_with_pending(
        self, *, limit: int = 20, exclude_kinds: Sequence[str] = ()
    ) -> list[str]:
        """有未描述证据的窗口列表。

        后台任务按窗口跑（``pending`` 是按窗口设计的，因为描述要绑定上下文），
        所以需要一个"哪些窗口有活"的入口。按最早证据的先后排，
        避免新窗口一直压着旧窗口饿死。

        **与 ``pending`` 保持同一套过滤**（排除已标成永久失败的行）：
        两处不一致的话，一个只剩永久失败证据的窗口会每轮都被列出来，
        `describe_pending` 又捞不到东西——空转，而且日志里看不出原因。
        """
        conditions = [
            MediaEvidenceRow.actor_id == self._actor_id,
            MediaEvidenceRow.description.is_(None),
            MediaEvidenceRow.describe_failed_at.is_(None),
        ]
        if exclude_kinds:
            conditions.append(MediaEvidenceRow.kind.not_in(list(exclude_kinds)))
        rows = (
            (
                await self._session.execute(
                    select(MediaEvidenceRow.window_id, MediaEvidenceRow.created_at)
                    .where(*conditions)
                    .order_by(MediaEvidenceRow.created_at)
                    .limit(limit * 10)
                )
            )
            .all()
        )
        seen: list[str] = []
        for window_id, _created in rows:
            if window_id not in seen:
                seen.append(window_id)
            if len(seen) >= limit:
                break
        return seen

    async def fact_descriptions(self, *, window_id: str) -> list[str]:
        """可以当事实用的描述。

        **只有 human 描述会出现在这里。** 模型推断虽然可能更详细，
        但它只是猜测，不能进事实——这条规则集中在一处，别处不必再判。
        """
        rows = (
            (
                await self._session.execute(
                    select(MediaEvidenceRow).where(
                        MediaEvidenceRow.actor_id == self._actor_id,
                        MediaEvidenceRow.window_id == window_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            view.description
            for view in (_view(row) for row in rows if not _expired(row))
            if view.fact_eligible and view.description
        ]

    # ---------------------------------------------------------------- 内部

    async def _load(self, evidence_id: str) -> MediaEvidenceRow | None:
        row = await self._session.get(MediaEvidenceRow, evidence_id)
        if row is None or row.actor_id != self._actor_id:
            return None
        return row

    async def _require(self, evidence_id: str, window_id: str) -> MediaEvidenceRow:
        row = await self._load(evidence_id)
        # 存在但属于别的窗口：对调用方表现成"不存在"，不泄漏
        if row is None or row.window_id != window_id:
            raise MediaEvidenceError("媒体证据不存在")
        if _expired(row):
            raise MediaEvidenceError("媒体证据已过期")
        return row

    async def _by_dedupe(
        self, key: str, *, window_id: str | None = None
    ) -> MediaEvidenceRow | None:
        """按去重键找已登记的同类证据。

        ``window_id`` 给了就**限定在这个窗口内**：同一张图在两个窗口里是
        两条独立证据（各自的描述、各自的有效期），不能互相顶替。
        """
        stmt = select(MediaEvidenceRow).where(MediaEvidenceRow.dedupe_key == key)
        if window_id is not None:
            stmt = stmt.where(MediaEvidenceRow.window_id == window_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()


def _expired(row: MediaEvidenceRow, *, now: datetime | None = None) -> bool:
    if row.expires_at is None:
        return False
    return row.expires_at <= (now or utcnow())


def _view(row: MediaEvidenceRow) -> EvidenceView:
    return EvidenceView(
        evidence_id=row.id,
        window_id=row.window_id,
        kind=row.kind,
        media_ref=row.media_ref,
        origin=row.origin,
        visibility=row.visibility,
        description=row.description,
        description_origin=row.description_origin,
        expires_at=row.expires_at,
        created_at=row.created_at,
        subject_id=row.subject_id,
        describe_failed_at=row.describe_failed_at,
        describe_failed_reason=row.describe_failed_reason,
    )


__all__ = [
    "INBOUND_SCOPE",
    "KINDS",
    "ORIGIN_HUMAN",
    "ORIGIN_MODEL",
    "EvidenceView",
    "MediaEvidenceError",
    "MediaEvidenceService",
    "dedupe_key_for",
]
