"""流式输出事件日志：支持断线续传与多实例（设计文档 §4.3 可重放）。

问题：delta 只推进程内存队列；前端断线后靠轮询
``active_turn.partial_text`` 拿半成品。多实例部署时 partial_text 直接失效。

做法：把已产出的片段**按批**写入数据库（不是逐 token），
并给每个片段一个 ``(turn_id, attempt, sequence)`` 游标。
重连时：
1. 先取缺失片段（sequence > last_event_id）
2. 再跟随新片段
3. 游标过期/片段已清理 → 返回权威文本快照，不假装能续传

保留期由调用方控制；W0 只在内存保留短窗口，避免为了"续传"把
私聊原文长期落库（隐私优先）。这里定义接口与内存实现，
持久化实现留给多实例部署时切换。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

DEFAULT_TTL_SECONDS = 600
# 单轮最多保留的片段数（防止异常长回复把内存打满）
MAX_CHUNKS_PER_TURN = 400


@dataclass
class StreamChunk:
    turn_id: str
    attempt: int
    sequence: int
    text: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class StreamLog:
    """流式片段日志（内存实现）。

    只存**增量文本**，不存 system prompt 或上下文；
    私聊原文的保留由 TTL 控制，默认 10 分钟后自动清理。
    """

    def __init__(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._chunks: dict[tuple[str, int], list[StreamChunk]] = {}
        self._updated: dict[tuple[str, int], datetime] = {}
        self._ttl = timedelta(seconds=max(60, ttl_seconds))

    def append(self, *, turn_id: str, attempt: int, text: str) -> StreamChunk:
        key = (turn_id, attempt)
        chunks = self._chunks.setdefault(key, [])
        chunk = StreamChunk(
            turn_id=turn_id,
            attempt=attempt,
            sequence=len(chunks) + 1,
            text=text,
        )
        chunks.append(chunk)
        # 超过上限时丢弃最旧的（但保留 sequence 单调，前端按游标续接）
        if len(chunks) > MAX_CHUNKS_PER_TURN:
            del chunks[: len(chunks) - MAX_CHUNKS_PER_TURN]
        self._updated[key] = chunk.created_at
        return chunk

    def since(
        self, *, turn_id: str, attempt: int, last_sequence: int = 0
    ) -> tuple[list[StreamChunk], bool]:
        """取 ``sequence > last_sequence`` 的片段。

        返回 (chunks, complete)：``complete=False`` 表示游标已过期或片段被清理，
        调用方应改用权威文本快照，而不是假装能续传。
        """
        key = (turn_id, attempt)
        if not self._alive(key):
            return [], False
        chunks = self._chunks.get(key, [])
        if last_sequence > 0 and chunks and chunks[0].sequence > last_sequence + 1:
            return [], False
        return [c for c in chunks if c.sequence > last_sequence], True

    def full_text(self, *, turn_id: str, attempt: int) -> str:
        return "".join(c.text for c in self._chunks.get((turn_id, attempt), []))

    def clear(self, *, turn_id: str, attempt: int | None = None) -> None:
        if attempt is not None:
            self._chunks.pop((turn_id, attempt), None)
            self._updated.pop((turn_id, attempt), None)
            return
        for key in [k for k in self._chunks if k[0] == turn_id]:
            self._chunks.pop(key, None)
            self._updated.pop(key, None)

    def _alive(self, key: tuple[str, int]) -> bool:
        updated = self._updated.get(key)
        if updated is None:
            return False
        return datetime.now(UTC) - updated <= self._ttl

    def stats(self) -> dict[str, object]:
        return {
            "turns": len({k[0] for k in self._chunks}),
            "chunks": sum(len(v) for v in self._chunks.values()),
            "ttl_seconds": int(self._ttl.total_seconds()),
        }


__all__ = ["MAX_CHUNKS_PER_TURN", "StreamChunk", "StreamLog"]
