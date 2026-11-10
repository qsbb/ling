"""她主动选择"过一会儿再回"：标记解析、状态机、持久化。

用户裁决（2026-11-10）：「做，但是只在她主动选择时才延后，且延后多久让她自由
发挥，上限 5 分钟」。

本域的职责边界：

- `markers.py`：把 ``<<等会回 90>>`` 解析成 :class:`DeferIntent`，并把标记从
  所有出口剥干净。**默认立即回复**：没有合法标记就没有延后。
- `state.py`：状态机纯函数（`pending → claimed → delivered/cancelled/failed`），
  终态吸收。
- `service.py`：持久化、认领（租约 + 代次）、抢占栅栏、终结。

**不做什么**：不调模型、不投递、不决定"该不该延后"。生成与投递在
`worker.py` 里用 `TurnCoordinator` 另开一个新逻辑轮次完成——本域只管"她答应过
一句话，这句话现在怎么样了"。
"""

from ling.domains.deferred.markers import (
    BAD_FORMAT_BODY_PRESENT,
    BAD_FORMAT_EMPTY,
    BAD_FORMAT_MULTIPLE,
    BAD_FORMAT_UNPARSABLE,
    DeferIntent,
    parse_defer_marker,
    parse_duration_seconds,
    stream_safe_deferred,
    strip_defer_markers,
)
from ling.domains.deferred.state import (
    ALLOWED_TRANSITIONS,
    HARD_DEADLINE_SECONDS,
    MAX_DELAY_SECONDS,
    TERMINAL_STATES,
    DeferredReplyState,
    InvalidDeferredTransition,
    is_terminal,
    transition,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "BAD_FORMAT_BODY_PRESENT",
    "BAD_FORMAT_EMPTY",
    "BAD_FORMAT_MULTIPLE",
    "BAD_FORMAT_UNPARSABLE",
    "DeferIntent",
    "DeferredReplyState",
    "HARD_DEADLINE_SECONDS",
    "InvalidDeferredTransition",
    "MAX_DELAY_SECONDS",
    "TERMINAL_STATES",
    "is_terminal",
    "parse_defer_marker",
    "parse_duration_seconds",
    "stream_safe_deferred",
    "strip_defer_markers",
    "transition",
]
