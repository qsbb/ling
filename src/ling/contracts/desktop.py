"""Contracts for the owner-controlled Notepad task and its Windows companion."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    UUID4,
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

MAX_NOTE_LENGTH = 2000
_INVALID_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]")

TaskState = Literal["queued", "running", "succeeded", "failed", "interrupted", "cancelled"]
TaskStep = Literal["opened", "written", "verified"]
CompanionIssue = Literal[
    "desktop_unavailable", "window_changed", "open_failed", "editor_unavailable",
    "editor_error", "readback_mismatch", "connection_lost", "lease_expired",
]
TaskReason = CompanionIssue | Literal[
    "owner_paused", "owner_cancelled", "pairing_changed", "queue_expired",
]


def normalize_note(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _valid_note(value: str) -> str:
    if not value.strip() or _INVALID_TEXT.search(value):
        raise ValueError("请输入可写入记事本的文本")
    return normalize_note(value)


NoteText = Annotated[str, Field(min_length=1, max_length=MAX_NOTE_LENGTH), AfterValidator(_valid_note)]


class DesktopModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PairDesktop(DesktopModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[^\x00-\x1f]+$")

    @field_validator("name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        if not name.strip() or _INVALID_TEXT.search(name):
            raise ValueError("无效的电脑名称")
        return name.strip()


class DesktopPairing(DesktopModel):
    token: str


class DesktopControl(DesktopModel):
    paused: StrictBool


class CreateDesktopTask(DesktopModel):
    request_id: UUID4
    text: NoteText


class DesktopTask(DesktopModel):
    id: UUID4
    kind: Literal["notepad_write"] = "notepad_write"
    text: str
    state: TaskState
    step: TaskStep | None
    reason: TaskReason | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class DesktopDevice(DesktopModel):
    name: str
    state: Literal["offline", "unavailable", "ready"]
    last_seen_at: datetime | None


class DesktopStatus(DesktopModel):
    device: DesktopDevice | None
    paused: bool
    tasks: list[DesktopTask]


class DesktopHeartbeat(DesktopModel):
    ready: StrictBool


class DesktopAssignment(DesktopModel):
    id: UUID4
    text: NoteText
    lease_token: str = Field(min_length=32, max_length=64)
    lease_seconds: int = Field(gt=0, le=60)


class DesktopPoll(DesktopModel):
    task: DesktopAssignment | None


class DesktopReport(DesktopModel):
    lease_token: str = Field(min_length=32, max_length=64)
    step: TaskStep | Literal["aborted"]
    reason: CompanionIssue | None = None
    readback: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)

    @model_validator(mode="after")
    def check_payload(self) -> DesktopReport:
        if (self.step == "verified") != (self.readback is not None):
            raise ValueError("回读结果只用于验证步骤")
        if (self.step == "aborted") != (self.reason is not None):
            raise ValueError("中止步骤必须报告原因")
        return self


class DesktopReportResult(DesktopModel):
    accepted: bool
