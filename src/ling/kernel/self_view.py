"""SelfView：把各域的"允许信号"聚合成**当轮可安全注入**的自我投影。

为什么需要单独一层：表达层需要知道"我现在什么状态、在做什么、什么语气"，
但如果让它直接去读 `presence`/`affect`/`activity`/`relationship` 的表，
每个调用点都会自己发明一套阈值与过滤规则——`sealed` 漏一处就是隐私事故。

所以这里只做两件事：
1. **只读**：不写任何表，不持有状态
2. **只出安全的**：每条片段都经过 `AudienceSnapshot` 裁决，
   被隔离或不可见的片段直接丢弃，而不是"注入了再过滤"

字段来源与作用见本节说明。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import Origin, Visibility


@dataclass(frozen=True)
class SelfFragment:
    """一段可注入的自我描述。

    带 ``origin`` 与 ``visibility`` 不是为了展示，而是为了让下游
    （表达层、审计、测试）能判断"这句话能不能这么说"。
    """

    text: str
    source: str
    origin: Origin
    visibility: Visibility
    expires_at: datetime | None = None


@dataclass
class SelfView:
    fragments: list[SelfFragment] = field(default_factory=list)
    # 诊断用：哪些片段因为受众/隔离被丢掉，以及原因。
    # 不注入提示词，只给运维与测试看。
    dropped: list[tuple[str, str]] = field(default_factory=list)

    def render(self, *, budget_chars: int = 200) -> str:
        """按预算拼成一段。超预算就按顺序截断（重要的放前面）。"""
        if not self.fragments:
            return ""
        parts: list[str] = []
        used = 0
        for frag in self.fragments:
            cost = len(frag.text)
            if used + cost > budget_chars:
                break
            parts.append(frag.text)
            used += cost
        return "；".join(parts)

    def sources(self) -> list[str]:
        return [f.source for f in self.fragments]


class SelfViewBuilder:
    """聚合器。只读，不写表。"""

    def __init__(
        self,
        session: AsyncSession,
        *,
        actor_id: str,
        subject_id: str,
        window_id: str,
        sealed: bool = False,
        is_group: bool = False,
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._subject_id = subject_id
        self._window_id = window_id
        # sealed 时不注入任何跨窗口自我状态（见 ADR-0004）
        self._sealed = sealed
        self._is_group = is_group

    async def build(self) -> SelfView:
        view = SelfView()
        if self._sealed:
            # 完全隔离：连"我现在在做什么"都不给——
            # 那是跨窗口的自我状态，注入就等于隔离名不副实。
            view.dropped.append(("presence", "sealed 模式不注入跨窗口自我状态"))
            view.dropped.append(("scene", "sealed 模式不注入跨窗口自我状态"))
            return view

        await self._add_presence(view)
        await self._add_scene(view)
        await self._add_agenda(view)
        await self._add_self_timeline(view)
        return view

    async def _add_presence(self, view: SelfView) -> None:
        try:
            from ling.domains.presence.service import PresenceService

            presence = await PresenceService(
                self._session, actor_id=self._actor_id
            ).view()
        except Exception:  # noqa: BLE001 - 状态读不到不影响对话
            view.dropped.append(("presence", "读取失败"))
            return
        text = presence.describe()
        if not text:
            return
        view.fragments.append(
            SelfFragment(
                text=text,
                source="presence",
                origin=Origin.simulated,
                # 精力是角色私有的：群聊里说"我有点累"会显得在向所有人索取情绪价值
                visibility=(
                    Visibility.actor_private if self._is_group else Visibility.subject_private
                ),
            )
        )

    async def _add_scene(self, view: SelfView) -> None:
        try:
            from ling.domains.presence.service import PresenceService

            scene = await PresenceService(
                self._session, actor_id=self._actor_id
            ).scene()
        except Exception:  # noqa: BLE001
            view.dropped.append(("scene", "读取失败"))
            return
        if scene.expired:
            # 过期场景**不进提示词**：宁可不说，也不说三天前的事
            view.dropped.append(("scene", "已过期"))
            return
        text = scene.describe()
        if not text:
            return
        if self._is_group and scene.origin == "user_stated":
            # 用户私聊里说过的事，不在群里复述
            view.dropped.append(("scene", "群聊不复述私聊来源"))
            return
        view.fragments.append(
            SelfFragment(
                text=text,
                source="scene",
                origin=(
                    Origin.simulated
                    if scene.origin == "simulated"
                    else Origin.external_observation
                ),
                visibility=Visibility.subject_private,
            )
        )


    async def _add_agenda(self, view: SelfView) -> None:
        """把"今天打算做什么"注入。

        措辞**必须区分 tentative / confirmed**：用户会据此判断能不能当约定依赖。
        tentative 说"可能…想"，confirmed 说"约好"。
        没有计划时**什么都不加**——不硬编"我今天很充实"。
        """
        try:
            from ling.domains.agenda.service import AgendaService

            agenda = AgendaService(self._session, actor_id=self._actor_id)
            upcoming = await agenda.upcoming(limit=2)
        except Exception:  # noqa: BLE001
            view.dropped.append(("agenda", "读取失败"))
            return
        if not upcoming:
            return
        # 群聊里不报自己的行程：那对群里其他人没有意义，还像在自言自语
        if self._is_group:
            view.dropped.append(("agenda", "群聊不注入个人行程"))
            return
        text = "；".join(p.describe() for p in upcoming)
        view.fragments.append(
            SelfFragment(
                text=f"你手头的打算：{text}",
                source="agenda",
                origin=Origin.simulated,
                visibility=Visibility.subject_private,
            )
        )


    async def _add_self_timeline(self, view: SelfView) -> None:
        """"我今天做过什么"。

        只来自**已完成且有证据**的活动（见 `SelfTimelineService`）。
        群聊里不注入——对群里其他人没有意义，还可能带上私聊的因果。
        """
        if self._is_group:
            view.dropped.append(("self_timeline", "群聊不注入自我经历"))
            return
        try:
            from ling.domains.memory.self_timeline import SelfTimelineService

            experiences = await SelfTimelineService(
                self._session, actor_id=self._actor_id
            ).recent(days=1, limit=3)
        except Exception:  # noqa: BLE001
            view.dropped.append(("self_timeline", "读取失败"))
            return
        if not experiences:
            return
        text = "、".join(e.summary for e in experiences)
        view.fragments.append(
            SelfFragment(
                text=f"你今天做过的事：{text}（模拟生活，不是真实身体经历）",
                source="self_timeline",
                origin=Origin.simulated,
                visibility=Visibility.actor_private,
            )
        )


__all__ = ["SelfFragment", "SelfView", "SelfViewBuilder"]
