from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

CUSTOM_SCOPE = "custom_http"
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
LiveRole = Literal["host", "companion"]


class BroadcastWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @model_validator(mode="after")
    def ordered(self) -> BroadcastWindow:
        if self.starts_at >= self.ends_at:
            raise ValueError("授权结束时间必须晚于开始时间")
        return self


class LivePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: LiveRole = "host"
    public_topic: str = Field(default="", max_length=400)
    max_minutes: int = Field(default=30, ge=1, le=240)
    autonomous_window: BroadcastWindow | None = None
    host_name: str = Field(default="", max_length=64)
    companion_auto_reply: bool = False

    @model_validator(mode="after")
    def check_role(self) -> LivePlan:
        if self.role == "companion" and self.autonomous_window is not None:
            raise ValueError("陪伴场次由主播开播")
        return self


def plan_from_config(config: dict) -> LivePlan:
    if "live_plan" in config:
        return LivePlan.model_validate(config["live_plan"] or {})
    return LivePlan(
        public_topic=config.get("public_topic", ""), max_minutes=config.get("max_minutes", 30),
        autonomous_window=config.get("autonomous_window"),
    )


class Sender(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Identifier | None = None
    name: str | None = Field(default=None, max_length=128)


class CustomEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    event_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    kind: Literal["comment", "entrance", "like", "gift", "live_start", "live_end"]
    room_id: Identifier
    occurred_at: AwareDatetime
    session_id: str | None = Field(default=None, max_length=64)
    sender: Sender | None = None
    text: str = Field(default="", max_length=500)


def account_namespace(instance_id: str) -> str:
    return f"custom-http.{instance_id}"


def event_is_fresh(event: CustomEvent, now: datetime, *, max_age: float = 30) -> bool:
    return -5 <= (now - event.occurred_at).total_seconds() <= max_age
