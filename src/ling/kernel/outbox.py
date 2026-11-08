"""outbox 幂等投递的小工具（**无状态、不碰数据库**）。

## 为什么单独成模块

`worker.py` 里一直有一个 `_like_prefix`：把字面量转成 LIKE 前缀模式，
必须转义 ``%`` / ``_`` / ``\\``——否则 ``actor_ling`` 里的下划线会被当成
"任意一个字符"，``actorXling`` 的事件也会被算进同一个作业。

现在**不止 worker** 要用它了：手动生成日程（`domains/agenda/outline_jobs.py`）
也要按"同一天的 outbox 事件"判断是否在途。继续放着就会有人从 worker
import 一个下划线开头的私有函数——跨模块依赖私有名的那天，改它的形状
不会有任何调用点报错。放到这里之后，"幂等键怎么拼"只有一处实现。
"""

from __future__ import annotations


def like_prefix(value: str) -> str:
    """把字面量变成 LIKE 前缀模式（转义 ``%`` / ``_`` / ``\\``）。"""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


__all__ = ["like_prefix"]
