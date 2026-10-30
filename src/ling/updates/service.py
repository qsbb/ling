"""更新通道的 API 侧视图与提交入口（不接触 Docker、不写数据库）。

只做四件事：

1. 读宿主的 ``heartbeat.json`` / ``current.json`` 与 ``pending.json``，合成一个
   稳定的、脱敏的 Web 视图；
2. 用固定的 reason 枚举回答“现在能不能更新、为什么不能”；
3. 用 ``fcntl`` 跨进程锁保证同一时刻只有一个 pending 请求（单飞）；
4. 把所有失败收敛成固定 detail 码，绝不把宿主日志/路径带给前端。

真正的执行（git、备份、docker compose、install.sh）在宿主的
``deploy/update/executor.py`` 里，本模块永远不会调用它。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ling.config import Settings
from ling.updates import protocol

# 心跳超过这个时间没有更新就认为执行器离线。执行器默认 5s 一次心跳。
EXECUTOR_OFFLINE_AFTER_SECONDS = 90.0
# 提交时等锁的上限：锁只覆盖一次原子写，正常是毫秒级。
SUBMIT_LOCK_TIMEOUT_SECONDS = 10.0

# reason 枚举（契约的一部分，前端按它显示固定文案）。
REASON_DISABLED = "disabled"
REASON_NOT_OWNER = "not_owner"
REASON_EXECUTOR_OFFLINE = "executor_offline"
REASON_CHANNEL_UNAVAILABLE = "channel_unavailable"
REASON_COMMIT_UNKNOWN = "installed_commit_unknown"
REASON_VERSION_DRIFT = "version_drift"
REASON_IN_PROGRESS = "update_in_progress"
REASON_PROTOCOL_UNSUPPORTED = "protocol_unsupported"
REASON_MANUAL_REQUIRED = "manual_required"
REASON_RELEASE_UNVERIFIABLE = "release_unverifiable"
REASON_NOT_NEWER = "not_newer"

UPDATE_REASONS = (
    REASON_DISABLED,
    REASON_NOT_OWNER,
    REASON_EXECUTOR_OFFLINE,
    REASON_CHANNEL_UNAVAILABLE,
    REASON_COMMIT_UNKNOWN,
    REASON_VERSION_DRIFT,
    REASON_IN_PROGRESS,
    REASON_PROTOCOL_UNSUPPORTED,
    REASON_MANUAL_REQUIRED,
    REASON_RELEASE_UNVERIFIABLE,
    REASON_NOT_NEWER,
)


class UpdateSubmissionError(Exception):
    """提交请求被拒绝。``detail`` 是固定的稳定代码，供前端/测试判断。"""

    def __init__(self, detail: str, *, status_code: int = 503, message: str | None = None) -> None:
        self.detail = detail
        self.status_code = status_code
        self.message = message or protocol.message_for(detail) or "更新请求被拒绝"
        super().__init__(self.message)


@dataclass(frozen=True)
class ReleaseTarget:
    """API 侧已经核验过的远端发布目标（由 routes.version 组装）。"""

    status: str
    release_id: int | None
    tag: str | None
    commit: str | None
    published_at: str | None
    html_url: str | None
    stable: bool
    is_newer: bool | None

    @property
    def update_ready(self) -> bool:
        return (
            self.status == "ok"
            and self.stable
            and self.release_id is not None
            and self.tag is not None
            and protocol.is_full_commit(self.commit)
        )


@dataclass(frozen=True)
class ExecutorState:
    online: bool
    last_seen_at: str | None
    deployed_commit: str | None
    current_version: str | None
    current_job: protocol.UpdateJob | None
    active_job: protocol.UpdateJob | None
    status_available: bool
    requests_available: bool
    heartbeat_present: bool = False
    protocol_ok: bool = False
    manual_required: bool = False
    pending_state: str = protocol.PENDING_STATE_UNKNOWN

    @property
    def channel_available(self) -> bool:
        return self.status_available and self.requests_available


class UpdateChannel:
    """只读视图 + 单飞提交。所有路径都来自进程配置，不接受请求参数。"""

    def __init__(self, settings: Settings, *, now: Callable[[], float] | None = None) -> None:
        self._settings = settings
        self._now = now or time.time

    # ------------------------------------------------------------ 目录

    @property
    def requests_dir(self) -> Path:
        return Path(self._settings.update_requests_dir)

    @property
    def status_dir(self) -> Path:
        return Path(self._settings.update_status_dir)

    @property
    def enabled(self) -> bool:
        return bool(self._settings.update_enabled)

    def _dirs(self) -> tuple[bool, bool]:
        return (
            protocol.directory_ready(self.status_dir),
            protocol.directory_ready(self.requests_dir),
        )

    # ------------------------------------------------------------ 读取

    def read_pending(self) -> protocol.PendingFile:
        """读取 pending；**不把“文件存在但不可解析”折叠成“没有请求”**。"""
        if not protocol.directory_ready(self.requests_dir):
            return protocol.PendingFile(protocol.PENDING_STATE_UNKNOWN)
        return protocol.read_pending_file(self.requests_dir / protocol.PENDING_FILE)

    def read_current_job(self) -> protocol.UpdateJob | None:
        return protocol.UpdateJob.from_dict(
            protocol.read_json_file(self.status_dir / protocol.CURRENT_FILE)
        )

    def read_heartbeat(self) -> dict[str, Any]:
        payload = protocol.read_json_file(self.status_dir / protocol.HEARTBEAT_FILE)
        return payload or {}

    def read_heartbeat_state(self) -> protocol.Heartbeat | None:
        return protocol.parse_heartbeat(self.read_heartbeat())

    def _heartbeat_present(self) -> bool:
        try:
            return (self.status_dir / protocol.HEARTBEAT_FILE).is_file()
        except OSError:  # pragma: no cover - 极端文件系统错误
            return False

    def executor_state(self) -> ExecutorState:
        status_available, requests_available = self._dirs()
        heartbeat_present = self._heartbeat_present() if status_available else False
        heartbeat = self.read_heartbeat_state() if status_available else None
        if heartbeat is None:
            last_seen = None
            deployed_commit = None
            current_version = None
            protocol_ok = False
            manual_required = False
        else:
            last_seen = heartbeat.last_seen_at
            deployed_commit = heartbeat.deployed_commit
            current_version = heartbeat.current_version
            protocol_ok = heartbeat.protocol_ok
            manual_required = heartbeat.manual_required
        age = protocol.seconds_since(last_seen, now=self._now())
        # 在线 = 心跳显式 ok + 新鲜。协议不兼容/人工门禁绝不算在线。
        online = bool(protocol_ok and age is not None and 0 <= age <= EXECUTOR_OFFLINE_AFTER_SECONDS)
        pending = (
            self.read_pending()
            if requests_available
            else protocol.PendingFile(protocol.PENDING_STATE_UNKNOWN)
        )
        if pending.state == protocol.PENDING_STATE_INVALID:
            # 存在但不可信的请求：API 不执行、不覆盖，也不能报告“空闲可更新”。
            protocol_ok = False
        current_job = self.read_current_job() if status_available else None
        active_job = self._active_job(current_job, pending)
        return ExecutorState(
            online=online,
            last_seen_at=last_seen,
            deployed_commit=deployed_commit,
            current_version=current_version,
            current_job=self._display_job(current_job, pending),
            active_job=active_job,
            status_available=status_available,
            requests_available=requests_available,
            heartbeat_present=heartbeat_present,
            protocol_ok=protocol_ok,
            manual_required=manual_required,
            pending_state=pending.state,
        )

    @staticmethod
    def _blocks_new_submission(job: protocol.UpdateJob | None) -> bool:
        """``needs_manual`` 也是门禁：人工确认之前绝不允许再发起更新。

        失败（``failed``）与成功（``succeeded``）是终态，可以直接重新发起
        （新的确认会生成新的 request_id）；``needs_manual`` 表示宿主无法证明
        服务状态，必须由宿主 ``adopt-current`` 在核验并取锁之后显式解除。
        """
        return job is not None and job.state in (*protocol.ACTIVE_JOB_STATES, "needs_manual")

    @classmethod
    def _active_job(
        cls, current: protocol.UpdateJob | None, pending: protocol.PendingFile
    ) -> protocol.UpdateJob | None:
        if cls._blocks_new_submission(current):
            return current
        if pending.state == protocol.PENDING_STATE_OK and pending.request is not None:
            # 只要 pending 还在，它就是“已排队、尚未被宿主 claim”的任务。
            # 即使 current 是同一个 id 的终态（宿主还没清干净），也按进行中处理：
            # 否则 API 会覆盖掉宿主正在 claim 的文件，把新请求吞掉。
            return pending.request.to_job()
        return None

    @classmethod
    def _display_job(
        cls, current: protocol.UpdateJob | None, pending: protocol.PendingFile
    ) -> protocol.UpdateJob | None:
        if current is not None and (current.active or current.state == "needs_manual"):
            # 人工门禁/进行中优先显示：不要被残留 pending 伪装成“已排队”。
            return current
        if (
            pending.state == protocol.PENDING_STATE_OK
            and pending.request is not None
            and (current is None or current.request_id != pending.request.request_id)
        ):
            # 执行器还没接手的新请求：显示为 queued，而不是被终态记录遮住。
            # 同一个 id 的残留 pending（终态 + 未清干净）仍显示终态记录：更真实，
            # 门禁也仍按“进行中”处理，等宿主 compare-and-unlink 清掉。
            return pending.request.to_job()
        return current

    # ------------------------------------------------------------ 门控

    def gate(
        self, *, role: str, current_version: str, current_commit: str | None
    ) -> str | None:
        """返回第一个不适用的 reason；全部通过返回 None。"""
        if not self.enabled:
            return REASON_DISABLED
        if role != "owner":
            return REASON_NOT_OWNER
        state = self.executor_state()
        if not state.channel_available:
            return REASON_CHANNEL_UNAVAILABLE
        if state.manual_required:
            # 执行器已经明确要求人工恢复：比“协议不兼容”更具体，优先告知。
            return REASON_MANUAL_REQUIRED
        if (state.heartbeat_present or state.pending_state == protocol.PENDING_STATE_INVALID) and (
            not state.protocol_ok
        ):
            # 心跳/schema 不认识或 pending 不可解析：先让人工处理，绝不执行。
            return REASON_PROTOCOL_UNSUPPORTED
        if state.active_job is not None:
            return REASON_IN_PROGRESS
        if not state.online:
            return REASON_EXECUTOR_OFFLINE
        if not protocol.is_full_commit(current_commit):
            return REASON_COMMIT_UNKNOWN
        if state.deployed_commit != current_commit or state.current_version != current_version:
            return REASON_VERSION_DRIFT
        return None

    def evaluate_release(
        self,
        target: ReleaseTarget,
        *,
        role: str,
        current_version: str,
        current_commit: str | None,
    ) -> tuple[bool, str | None]:
        """check-release 的 ``update_eligible`` / ``update_reason`` 判定。"""
        if target.status != "ok":
            return False, REASON_RELEASE_UNVERIFIABLE
        if target.is_newer is False:
            return False, REASON_NOT_NEWER
        if not target.update_ready:
            return False, REASON_RELEASE_UNVERIFIABLE
        gate = self.gate(
            role=role, current_version=current_version, current_commit=current_commit
        )
        if gate is not None:
            return False, gate
        return True, None

    # ------------------------------------------------------------ 视图

    def view(
        self,
        *,
        role: str,
        current_version: str,
        current_commit: str | None,
        include_reason: bool = True,
    ) -> dict[str, Any]:
        state = self.executor_state()
        reason = (
            self.gate(role=role, current_version=current_version, current_commit=current_commit)
            if include_reason
            else None
        )
        job = state.current_job
        return {
            "schema_version": protocol.SCHEMA_VERSION,
            "enabled": self.enabled,
            "reason": reason,
            "executor": {
                "online": state.online,
                "last_seen_at": state.last_seen_at,
            },
            "current_job": job.to_dict() if job is not None else None,
        }

    # ------------------------------------------------------------ 提交

    def submit(self, request: protocol.UpdateRequest) -> protocol.UpdateJob:
        """在跨进程锁内复查，再原子写 pending。

        顺序契约（与宿主执行器一致）：``requests/.lock`` 是**唯一**的 pending
        所有权锁。宿主要在锁内 claim（写 running + journal start 后 compare-and-
        unlink），API 也必须在锁内读一遍 pending：

        - 已存在合法 pending → 拒绝（``update_in_progress``）。**绝不覆盖**：覆盖
          会吃掉宿主的在途 claim；
        - 存在但不可解析 → 拒绝（``protocol_unsupported``），把不可信文件留给人工；
        - 没有 pending 且没有进行中/人工门禁任务 → 写入。
        """
        if not protocol.directory_ready(self.requests_dir):
            raise UpdateSubmissionError(REASON_CHANNEL_UNAVAILABLE)
        lock = protocol.FileLock(
            self.requests_dir / protocol.LOCK_FILE,
            blocking=True,
            timeout=SUBMIT_LOCK_TIMEOUT_SECONDS,
        )
        try:
            acquired = lock.acquire()
        except protocol.ProtocolError as exc:
            raise UpdateSubmissionError(REASON_CHANNEL_UNAVAILABLE) from exc
        if not acquired:
            raise UpdateSubmissionError(REASON_CHANNEL_UNAVAILABLE)
        try:
            pending = self.read_pending()
            if pending.state == protocol.PENDING_STATE_INVALID:
                raise UpdateSubmissionError(REASON_PROTOCOL_UNSUPPORTED)
            if pending.state == protocol.PENDING_STATE_OK:
                raise UpdateSubmissionError(REASON_IN_PROGRESS, status_code=409)
            state = self.executor_state()
            if state.manual_required:
                raise UpdateSubmissionError(REASON_MANUAL_REQUIRED)
            if state.heartbeat_present and not state.protocol_ok:
                raise UpdateSubmissionError(REASON_PROTOCOL_UNSUPPORTED)
            if state.active_job is not None:
                raise UpdateSubmissionError(REASON_IN_PROGRESS, status_code=409)
            try:
                protocol.atomic_write_json(
                    self.requests_dir / protocol.PENDING_FILE,
                    request.to_dict(),
                    mode=0o640,
                )
            except protocol.ProtocolError as exc:
                raise UpdateSubmissionError(REASON_CHANNEL_UNAVAILABLE) from exc
        finally:
            lock.release()
        return request.to_job()


__all__ = [
    "EXECUTOR_OFFLINE_AFTER_SECONDS",
    "ExecutorState",
    "REASON_CHANNEL_UNAVAILABLE",
    "REASON_COMMIT_UNKNOWN",
    "REASON_DISABLED",
    "REASON_EXECUTOR_OFFLINE",
    "REASON_IN_PROGRESS",
    "REASON_MANUAL_REQUIRED",
    "REASON_NOT_NEWER",
    "REASON_NOT_OWNER",
    "REASON_PROTOCOL_UNSUPPORTED",
    "REASON_RELEASE_UNVERIFIABLE",
    "REASON_VERSION_DRIFT",
    "ReleaseTarget",
    "UPDATE_REASONS",
    "UpdateChannel",
    "UpdateSubmissionError",
]
