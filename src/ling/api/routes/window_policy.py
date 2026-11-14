"""Web 私聊的会话级对话策略。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ling.api.deps import ConfigDep, CurrentAuth, DbSession
from ling.api.errors import LingError
from ling.api.routes.version import _origin_problem, _require_json_content_type
from ling.contracts.v1 import (
    ErrorCode,
    WindowDialoguePolicyPreview,
    WindowDialoguePolicyProposed,
    WindowDialoguePolicyView,
)
from ling.infra.models import Window
from ling.infra.security import load_signing_root_key
from ling.settings.window_policy import (
    WindowPolicySnapshot,
    lock_dialogue_config,
    locked_window_policy,
    policy_window,
    require_active_window,
    save_window_policy,
)

router = APIRouter(tags=["sessions"])
_FIELD: Literal["private_may_stay_silent"] = "private_may_stay_silent"
_PREVIEW_SECONDS = 300


class PolicyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: dict[Literal["private_may_stay_silent"], StrictBool] = Field(default_factory=dict)
    clear: list[Literal["private_may_stay_silent"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def one_operation(self) -> PolicyChange:
        if len(self.set) + len(self.clear) != 1:
            raise ValueError("请选择一个设置操作")
        return self

    @property
    def value(self) -> bool | None:
        return self.set.get(_FIELD) if self.set else None


class PolicyCommit(PolicyChange):
    preview_token: str = Field(min_length=1, max_length=2048)


def _write_guards(request: Request, auth: CurrentAuth) -> None:
    if auth.user.role != "owner":
        raise LingError(ErrorCode.forbidden, "只有所有者可以修改会话设置", status_code=403)
    _require_json_content_type(request)
    if _origin_problem(request, request.app.state.settings) is not None:
        raise LingError(
            ErrorCode.forbidden, "这个请求的来源不可信", status_code=403, detail="bad_origin"
        )


def _view(snapshot: WindowPolicySnapshot) -> WindowDialoguePolicyView:
    return WindowDialoguePolicyView(
        field=_FIELD,
        global_value=snapshot.global_value,
        override=snapshot.override,
        effective=snapshot.value,
        source=snapshot.source,
        global_revision=snapshot.config.revision,
        session_revision=snapshot.override_revision,
    )


def _state(snapshot: WindowPolicySnapshot, window: Window) -> dict:
    return {
        "global_revision": snapshot.config.revision,
        "global_value": snapshot.global_value,
        "generation": snapshot.generation,
        "session_revision": snapshot.override_revision,
        "override": snapshot.override,
        "window_updated_at": window.updated_at.isoformat(),
    }


def _signing_key(request: Request) -> bytes:
    return hmac.new(
        load_signing_root_key(request.app.state.settings),
        b"ling-window-dialogue-policy-preview-v1",
        hashlib.sha256,
    ).digest()


def _token(request: Request, payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=")
    signature = hmac.new(_signing_key(request), encoded, hashlib.sha256).hexdigest()
    return f"{encoded.decode()}.{signature}"


def _read_token(request: Request, token: str) -> dict:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_signing_key(request), encoded.encode("ascii"), hashlib.sha256)
        if not hmac.compare_digest(signature, expected.hexdigest()):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(payload, dict) or type(payload.get("expires")) is not int:
            raise ValueError("bad payload")
        if payload["expires"] <= int(time.time()):
            raise ValueError("expired")
        return payload
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise LingError(
            ErrorCode.conflict, "预览已失效，请重新预览", status_code=409
        ) from exc


@router.get("/sessions/{session_id}/dialogue-policy", response_model=WindowDialoguePolicyView)
async def get_policy(
    session_id: str, auth: CurrentAuth, session: DbSession, config: ConfigDep
) -> WindowDialoguePolicyView:
    await lock_dialogue_config(session)
    await policy_window(session, window_id=session_id, subject_id=auth.user.subject_id)
    snapshot = await locked_window_policy(session, config, session_id)
    return _view(snapshot)


@router.post(
    "/sessions/{session_id}/dialogue-policy/preview", response_model=WindowDialoguePolicyPreview
)
async def preview_policy(
    session_id: str,
    body: PolicyChange,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> WindowDialoguePolicyPreview:
    _write_guards(request, auth)
    await lock_dialogue_config(session)
    window = await policy_window(session, window_id=session_id, subject_id=auth.user.subject_id)
    require_active_window(window)
    snapshot = await locked_window_policy(session, config, session_id)
    expires = int(time.time()) + _PREVIEW_SECONDS
    payload = {
        "user_id": auth.user.id,
        "subject_id": auth.user.subject_id,
        "window_id": session_id,
        "value": body.value,
        "state": _state(snapshot, window),
        "expires": expires,
    }
    return WindowDialoguePolicyPreview(
        current=_view(snapshot),
        proposed=WindowDialoguePolicyProposed(
            override=body.value,
            effective=snapshot.global_value if body.value is None else body.value,
            source="global" if body.value is None else "session",
        ),
        preview_token=_token(request, payload),
        expires_at=expires,
    )


@router.put("/sessions/{session_id}/dialogue-policy", response_model=WindowDialoguePolicyView)
async def put_policy(
    session_id: str,
    body: PolicyCommit,
    request: Request,
    auth: CurrentAuth,
    session: DbSession,
    config: ConfigDep,
) -> WindowDialoguePolicyView:
    _write_guards(request, auth)
    token = _read_token(request, body.preview_token)
    if (
        token.get("user_id") != auth.user.id
        or token.get("subject_id") != auth.user.subject_id
        or token.get("window_id") != session_id
        or token.get("value") is not body.value
    ):
        raise LingError(ErrorCode.conflict, "预览与请求不一致，请重新预览", status_code=409)
    await lock_dialogue_config(session)
    window = await policy_window(session, window_id=session_id, subject_id=auth.user.subject_id)
    require_active_window(window)
    snapshot = await locked_window_policy(session, config, session_id)
    if token.get("state") != _state(snapshot, window) or token["expires"] <= int(time.time()):
        raise LingError(ErrorCode.conflict, "设置已变更，请重新预览", status_code=409)
    await save_window_policy(
        session,
        window_id=session_id,
        expected_revision=snapshot.override_revision,
        value=body.value,
        user_id=auth.user.id,
    )
    return _view(await locked_window_policy(session, config, session_id))
