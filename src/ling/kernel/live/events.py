from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

CUSTOM_SCOPE = "custom_http"
Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


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
