"""AudienceSnapshot：本轮冻结的受众快照 + 统一取数出口。

核心约束：

> "把'模型能读到，但不能说出'当成了可执行的隐私边界。这条边界守不住。
> 中性摘要仍然携带事实……确定性的做法是在检索、排序和组装提示**之前**
> 用代码核验来源、owner、身份绑定、目标受众、授权版本、有效期和撤回状态；
> 无权内容根本不进入该轮模型输入。"

本模块就是那个"唯一出口"。所有会进入模型输入的内容
（长期记忆、情节、意识条目、交接胶囊）都必须经过它的 `allows_*` 判定。

设计要点：
- **冻结**：一轮开始时创建，本轮中途的授权/模式变更不影响已开始的轮次
- **交集**：可用范围 = 模式 ∩ 授权 ∩ 受众 ∩ 来源（不是任一层级的继承）
- **来源核对**：群聊内容只能在本群用；别的群、私聊事实一律不进入
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import Visibility
from ling.domains.awareness.service import AwarenessService, IsolationMode
from ling.domains.consent.service import ConsentService
from ling.infra.models import MemoryEpisode, MemoryRow

# 群聊里允许出现的来源可见范围。**只有本群**的 group_scope 与 public。
_GROUP_ALLOWED_SOURCES = {"group_scope", "public"}
# 私聊里允许出现的来源可见范围。群里说的话默认不进私聊。
_PRIVATE_ALLOWED_SOURCES = {"subject_private", "window_private", "public"}


@dataclass(frozen=True)
class AudienceSnapshot:
    """本轮冻结的受众快照。构造后不可变，避免轮次中途被改。"""

    subject_id: str
    window_id: str
    window_visibility: str
    mode: IsolationMode
    revision: int
    cross_device_granted: bool
    frozen_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_group(self) -> bool:
        return self.window_visibility == "group_scope"

    # ---------------------------------------------------------- 各来源的准入

    def allows_long_term_memory(self) -> bool:
        """sealed 模式不读任何长期记忆。"""
        return self.mode is not IsolationMode.sealed

    def allows_awareness(self) -> bool:
        return self.mode is IsolationMode.continuous

    def allows_handoff_capsule(self) -> bool:
        """sealed 不读胶囊（否则模式说明与实际行为不一致）。"""
        return self.mode is not IsolationMode.sealed and not self.is_group

    def memory_row_visible(self, row: MemoryRow) -> bool:
        """一条长期记忆是否可以在**本窗口**出现。

        三道独立的门，缺一不可：
        1. 模式允许读长期记忆
        2. 记录自身的 visibility 在本受众可见集合内
        3. **来源核对**：群聊内容只能在本群用；别的群或私聊内容不进群
        """
        if not self.allows_long_term_memory():
            return False
        if row.visibility not in self._allowed_visibilities():
            return False
        return self._source_allowed(
            source_visibility=getattr(row, "source_visibility", "subject_private"),
            source_window_id=getattr(row, "source_window_id", None),
        )

    def episode_visible(self, episode: MemoryEpisode) -> bool:
        if not self.allows_long_term_memory():
            return False
        if episode.visibility not in self._allowed_visibilities():
            return False
        return self._source_allowed(
            source_visibility=getattr(episode, "source_visibility", "subject_private"),
            source_window_id=episode.window_id,
        )

    def _allowed_visibilities(self) -> set[str]:
        # 委托到唯一权威实现，避免规则漂移
        return {
            v.value
            for v in readable_visibilities(
                is_group=self.is_group,
                cross_device_granted=self.cross_device_granted,
            )
        }

    def _source_allowed(self, *, source_visibility: str, source_window_id: str | None) -> bool:
        """来源核对——这是"群 A 不进群 B"的真正防线。

        - 来源是群聊内容（group_scope）：必须就是本窗口这个群
        - 其他来源（私聊/窗口级）：只在私聊窗口可用；群聊里绝不出现
        """
        if self.is_group:
            if source_visibility not in _GROUP_ALLOWED_SOURCES:
                return False
            # 别的群的内容：即使 visibility 是 group_scope 也不允许
            return not (
                source_visibility == "group_scope" and source_window_id != self.window_id
            )
        # 私聊窗口
        if source_visibility in _GROUP_ALLOWED_SOURCES - {"public"}:
            # 群里说的话默认不进私聊（必须由本人明确提升，而不是自动）
            return False
        return source_visibility in _PRIVATE_ALLOWED_SOURCES

    # ---------------------------------------------------------- 创建

    @classmethod
    async def freeze(
        cls,
        session: AsyncSession,
        *,
        subject_id: str,
        window_id: str,
        window_visibility: str,
    ) -> AudienceSnapshot:
        policy = await AwarenessService(session).policy(subject_id)
        consent = ConsentService(session)
        cross_device = await consent.is_active(subject_id, "memory.cross_device")
        return cls(
            subject_id=subject_id,
            window_id=window_id,
            window_visibility=window_visibility,
            mode=policy.mode,
            revision=policy.revision,
            cross_device_granted=cross_device,
        )

    def describe(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "revision": self.revision,
            "window_visibility": self.window_visibility,
            "is_group": self.is_group,
            "long_term_memory": self.allows_long_term_memory(),
            "awareness": self.allows_awareness(),
            "handoff_capsule": self.allows_handoff_capsule(),
            "cross_device": self.cross_device_granted,
        }


def readable_visibilities(*, is_group: bool, cross_device_granted: bool) -> set[Visibility]:
    """**唯一权威实现**：本轮可读取的记忆可见范围。

    这条规则曾有四份独立实现（本模块、AudienceSnapshot 内部、
    ConsentService、PolicyGate）。每份看起来都一样，但没有任何东西
    保证它们一致——改一处忘另一处就会出现"要么泄漏、要么锁死"。
    现在全部委托到这里，并有测试断言各路径结果相同。

    注意：**永不包含 admin_only**。管理视图需要单独授权通道，
    不能靠"往允许集里加一项"实现。
    """
    if is_group:
        return {Visibility.group_scope, Visibility.public}
    base = {Visibility.subject_private, Visibility.window_private, Visibility.public}
    if cross_device_granted:
        base.add(Visibility.actor_private)
    return base


def visibility_set(snapshot: AudienceSnapshot) -> set[Visibility]:
    """把快照翻译成契约层的可见范围集合（供 recall 使用）。"""
    return readable_visibilities(
        is_group=snapshot.is_group,
        cross_device_granted=snapshot.cross_device_granted,
    )


__all__ = ["AudienceSnapshot", "readable_visibilities", "visibility_set"]
