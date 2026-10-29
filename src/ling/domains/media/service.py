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

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.consent.service import ConsentService
from ling.infra.models import MediaEvidenceRow, utcnow
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
        return self.description is None


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
    ) -> EvidenceView:
        """登记一条媒体证据。

        需要 ``media.inbound`` 授权；没有就拒绝——**不是静默忽略**。
        静默忽略会让"用户发了图但角色没反应"变成一个查不出来的怪现象。
        """
        if kind not in KINDS:
            raise MediaEvidenceError(f"未知媒体类型: {kind}")
        if not window_id:
            raise MediaEvidenceError("媒体证据必须绑定窗口")
        if not media_ref.strip():
            raise MediaEvidenceError("media_ref 不能为空")
        if subject_id is None:
            raise MediaEvidenceError("入站媒体必须带上传者 subject_id")

        consent = await ConsentService(self._session).get(subject_id, INBOUND_SCOPE)
        if not consent.granted:
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
        """
        row = await self._require(evidence_id, window_id)
        if row.subject_id is None:
            raise MediaEvidenceError("证据没有上传者，无法校验授权")

        consent = await ConsentService(self._session).get(row.subject_id, INBOUND_SCOPE)
        if not consent.granted:
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

    async def pending(self, *, window_id: str, limit: int = 20) -> list[EvidenceView]:
        """当前窗口里**还没描述**且在有效期内的证据。给描述任务用。"""
        rows = (
            (
                await self._session.execute(
                    select(MediaEvidenceRow)
                    .where(
                        MediaEvidenceRow.actor_id == self._actor_id,
                        MediaEvidenceRow.window_id == window_id,
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

    async def windows_with_pending(self, *, limit: int = 20) -> list[str]:
        """有未描述证据的窗口列表。

        后台任务按窗口跑（``pending`` 是按窗口设计的，因为描述要绑定上下文），
        所以需要一个"哪些窗口有活"的入口。按最早证据的先后排，
        避免新窗口一直压着旧窗口饿死。
        """
        rows = (
            (
                await self._session.execute(
                    select(MediaEvidenceRow.window_id, MediaEvidenceRow.created_at)
                    .where(
                        MediaEvidenceRow.actor_id == self._actor_id,
                        MediaEvidenceRow.description.is_(None),
                    )
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
