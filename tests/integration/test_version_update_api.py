"""受控 Web 更新 API 的集成测试（真实 PostgreSQL + ASGI，不联网/Docker）。

覆盖：认证、owner 权限、Origin/自定义头、严格 body、目标二次核验、
单飞并发、离线/漂移/进行中门控、只读 status 目录与脱敏响应。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from ling.api.routes import version as version_routes
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.updates import protocol
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CURRENT_COMMIT = "a" * 40
TARGET_COMMIT = "b" * 40
CURRENT_VERSION = "1.0.0"
TARGET_TAG = "v1.2.0"
ORIGIN = {"Origin": "http://testserver"}


def _configure(web_env: TestEnv, tmp_path: Path, *, enabled: bool = True) -> Path:
    requests = tmp_path / "requests"
    status = tmp_path / "status"
    requests.mkdir()
    status.mkdir()
    settings = dataclasses.replace(
        web_env.settings,
        update_enabled=enabled,
        update_requests_dir=requests,
        update_status_dir=status,
    )
    web_env.app.state.settings = settings  # type: ignore[attr-defined]
    return tmp_path


def _heartbeat(
    base: Path,
    *,
    last_seen: str | None = None,
    commit: str = CURRENT_COMMIT,
    version: str = CURRENT_VERSION,
    status: str | None = "ok",
    pending_state: str | None = "absent",
    schema_version: str = "v1",
) -> None:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "last_seen_at": last_seen or protocol.iso_now(),
        "deployed_commit": commit,
        "current_version": version,
    }
    if status is not None:
        payload["status"] = status
    if pending_state is not None:
        payload["pending_state"] = pending_state
    protocol.atomic_write_json(
        base / "status" / protocol.HEARTBEAT_FILE,
        payload,
        mode=0o644,
    )


def _pending(base: Path) -> protocol.UpdateRequest:
    request = protocol.build_request(
        target_tag=TARGET_TAG,
        target_commit=TARGET_COMMIT,
        current_version=CURRENT_VERSION,
        current_commit=CURRENT_COMMIT,
    )
    protocol.atomic_write_json(base / "requests" / protocol.PENDING_FILE, request.to_dict(), mode=0o640)
    return request


def _body(**overrides: object) -> dict[str, object]:
    expected: dict[str, object] = {
        "release_id": 42,
        "tag": TARGET_TAG,
        "commit": TARGET_COMMIT,
        "current_version": CURRENT_VERSION,
        "current_commit": CURRENT_COMMIT,
    }
    expected.update(overrides)
    return {"confirm_tag": TARGET_TAG, "expected": expected}


def _latest(**overrides: object) -> version_routes.LatestRelease:
    values: dict[str, object] = {
        "status": "ok",
        "release_id": 42,
        "tag": TARGET_TAG,
        "commit": TARGET_COMMIT,
        "published_at": "2026-10-30T00:00:00Z",
        "html_url": "https://github.com/qsbb/ling/releases/tag/v1.2.0",
        "stable": True,
        "is_newer": True,
    }
    values.update(overrides)
    return version_routes.LatestRelease(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fixed_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LING_COMMIT", CURRENT_COMMIT)
    monkeypatch.setattr(version_routes, "current_version", lambda: CURRENT_VERSION)


@pytest.fixture()
def release(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    state: dict[str, object] = {"value": _latest()}

    async def fake() -> version_routes.LatestRelease:
        return state["value"]  # type: ignore[return-value]

    monkeypatch.setattr(version_routes, "_fetch_update_release", fake)
    return state


def _post(env: TestEnv, body: dict[str, object] | None = None, **kwargs: object):
    headers = {**ORIGIN, "X-Ling-Update": "1", **kwargs.pop("headers", {})}
    return env.client.post(
        "/v1/version/update-requests",
        json=body if body is not None else _body(),
        headers=headers,
        **kwargs,
    )


# ---------------------------------------------------------------- 认证


async def test_update_endpoints_require_auth(web_env: TestEnv) -> None:
    for method, path in (
        ("get", "/v1/version/update"),
        ("post", "/v1/version/update-requests"),
    ):
        response = await getattr(web_env.client, method)(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthorized"


# ---------------------------------------------------------------- GET


async def test_update_view_defaults_to_disabled(logged_in: TestEnv) -> None:
    response = await logged_in.client.get("/v1/version/update")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "schema_version": "v1",
        "enabled": False,
        "reason": "disabled",
        "executor": {"online": False, "last_seen_at": None},
        "current_job": None,
    }
    assert response.headers["cache-control"] == "no-store"


async def test_update_view_shows_pending_queued(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    request = _pending(base)
    response = await logged_in.client.get("/v1/version/update")
    body = response.json()
    assert body["enabled"] is True
    assert body["reason"] == "update_in_progress"
    assert body["executor"]["online"] is True
    assert body["current_job"]["state"] == "queued"
    assert body["current_job"]["request_id"] == request.request_id
    assert str(tmp_path) not in response.text


async def test_update_view_offline_and_readonly_status(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base, last_seen="2020-01-01T00:00:00Z")
    response = await logged_in.client.get("/v1/version/update")
    body = response.json()
    assert body["executor"]["online"] is False
    assert body["reason"] == "executor_offline"


async def test_update_view_exposes_backup_warnings(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """task-76：升级成功但备份不完整 → 告警在状态接口上可见（不只是日志文件）。"""
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    request = _pending(base)
    notes = [
        "manifest: media_state=partial（media.tar.zst 归档时有文件正在写入，可能漏掉刚上传的媒体）",
        "manifest: keys_env_set=true（备份时 LING_ROOT_KEY 非空，恢复时必须同时恢复 deploy/.env）",
    ]
    payload = request.to_job().with_progress(
        state="succeeded", stage="complete", backup_warnings=notes
    ).to_dict()
    protocol.atomic_write_json(base / "status" / protocol.CURRENT_FILE, payload, mode=0o644)
    # 宿主已 claim：pending 不在了，任务才算真正的终态。
    protocol.remove_file(base / "requests" / protocol.PENDING_FILE)

    response = await logged_in.client.get("/v1/version/update")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_job"]["state"] == "succeeded"
    assert body["current_job"]["backup_warnings"] == notes
    # 展示字段不参与门禁：成功的终态不阻塞下一次提交。
    assert body["reason"] is None
    assert response.headers["cache-control"] == "no-store"
    assert str(tmp_path) not in response.text


async def test_update_view_backup_warnings_absent_defaults_to_empty(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """旧执行器不写该字段：API 给空数组，不报错、门禁不变。"""
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    request = _pending(base)
    payload = request.to_job().with_progress(state="succeeded", stage="complete").to_dict()
    payload.pop("backup_warnings", None)
    protocol.atomic_write_json(base / "status" / protocol.CURRENT_FILE, payload, mode=0o644)
    protocol.remove_file(base / "requests" / protocol.PENDING_FILE)

    response = await logged_in.client.get("/v1/version/update")
    assert response.status_code == 200, response.text
    assert response.json()["current_job"]["backup_warnings"] == []


async def test_backup_warnings_do_not_affect_owner_gate_or_body_limit(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    """加字段不得回归既有的 owner 权限与 422 体积上限。"""
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    request = _pending(base)
    protocol.atomic_write_json(
        base / "status" / protocol.CURRENT_FILE,
        request.to_job().with_progress(
            state="succeeded", stage="complete", backup_warnings=["warn"]
        ).to_dict(),
        mode=0o644,
    )
    protocol.remove_file(base / "requests" / protocol.PENDING_FILE)

    # non-owner 仍然 403 not_owner（GET 也照旧报告 not_owner）。
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="member2", password="member-password", role="member"
        )
    assert (
        await logged_in.client.post(
            "/v1/auth/login", json={"username": "member2", "password": "member-password"}
        )
    ).status_code == 200
    view = await logged_in.client.get("/v1/version/update")
    assert view.status_code == 200
    assert view.json()["reason"] == "not_owner"
    denied = await _post(logged_in)
    assert denied.status_code == 403
    assert denied.json()["error"]["detail"] == "not_owner"

    # 换回 owner：体积上限照旧按 Content-Length 在 4097 字节处 422。
    assert (
        await logged_in.client.post(
            "/v1/auth/login",
            json={"username": "tester", "password": "tester-password"},
        )
    ).status_code == 200
    oversized = await logged_in.client.post(
        "/v1/version/update-requests",
        content=b"x" * (version_routes.MAX_UPDATE_BODY_BYTES + 1),
        headers={**ORIGIN, "X-Ling-Update": "1", "Content-Type": "application/json"},
    )
    assert oversized.status_code == 422
    assert oversized.json()["error"]["detail"] == version_routes.DETAIL_BODY_TOO_LARGE
    assert not (base / "requests" / protocol.PENDING_FILE).exists()


# ---------------------------------------------------------------- check-release


async def test_check_release_keeps_old_fields_and_is_unverifiable_without_id(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_latest():
        return "ok", {"tag_name": "v9.9.9", "published_at": None, "html_url": None}

    monkeypatch.setattr(version_routes, "_fetch_latest_release", fake_latest)
    response = await logged_in.client.get("/v1/version/check-release")
    body = response.json()
    assert body["status"] == "ok"
    assert body["latest"] == "v9.9.9"
    assert body["is_newer"] is True
    assert body["release_id"] is None
    assert body["target_commit"] is None
    assert body["update_eligible"] is False
    assert body["update_reason"] == "release_unverifiable"
    assert response.headers["cache-control"] == "no-store"


async def test_check_release_eligible_when_channel_healthy(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await logged_in.client.get("/v1/version/check-release")
    body = response.json()
    assert body["release_id"] == 42
    assert body["target_commit"] == TARGET_COMMIT
    assert body["update_eligible"] is True
    assert body["update_reason"] is None


async def test_check_release_not_newer_reason(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    release["value"] = _latest(is_newer=False)
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await logged_in.client.get("/v1/version/check-release")
    body = response.json()
    assert body["update_eligible"] is False
    assert body["update_reason"] == "not_newer"


# ---------------------------------------------------------------- POST 门控


async def test_post_disabled_is_503(logged_in: TestEnv, release: dict[str, object]) -> None:
    response = await _post(logged_in)
    assert response.status_code == 503
    assert response.json()["error"]["detail"] == "disabled"


async def test_post_offline_is_503(logged_in: TestEnv, tmp_path: Path, release: dict[str, object]) -> None:
    _configure(logged_in, tmp_path)
    response = await _post(logged_in)
    assert response.status_code == 503
    assert response.json()["error"]["detail"] == "executor_offline"


async def test_post_non_owner_is_403(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="member", password="member-password", role="member"
        )
    response = await logged_in.client.post(
        "/v1/auth/login", json={"username": "member", "password": "member-password"}
    )
    assert response.status_code == 200, response.text
    response = await _post(logged_in)
    assert response.status_code == 403
    assert response.json()["error"]["detail"] == "not_owner"
    assert not (base / "requests" / protocol.PENDING_FILE).exists()


async def test_post_cookie_requires_same_origin(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await logged_in.client.post(
        "/v1/version/update-requests",
        json=_body(),
        headers={"X-Ling-Update": "1"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["detail"] == "origin_required"


async def test_post_rejects_foreign_origin(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await logged_in.client.post(
        "/v1/version/update-requests",
        json=_body(),
        headers={"Origin": "http://evil.example", "X-Ling-Update": "1"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["detail"] == "origin_mismatch"


async def test_post_bearer_without_origin_allowed(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    token = logged_in.client.cookies.get(logged_in.settings.cookie_name)
    assert token
    logged_in.client.cookies.clear()
    response = await logged_in.client.post(
        "/v1/version/update-requests",
        json=_body(),
        headers={"Authorization": f"Bearer {token}", "X-Ling-Update": "1"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "queued"


async def test_post_requires_update_header(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    _configure(logged_in, tmp_path)
    response = await logged_in.client.post(
        "/v1/version/update-requests",
        json=_body(),
        headers=ORIGIN,
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == "update_header_required"


async def test_post_rejects_extra_fields(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    _configure(logged_in, tmp_path)
    body = _body()
    body["cancel"] = True
    response = await _post(logged_in, body)
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == "invalid_request_body"


# ---------------------------------------------------------------- POST 成功/冲突


async def test_post_happy_path_returns_job_and_single_pending(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await _post(logged_in)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body.get("schema_version") is None  # UpdateJob 没有 schema_version 字段
    assert body["state"] == "queued"
    assert body["stage"] == "queued"
    assert body["target_tag"] == TARGET_TAG
    assert body["target_commit"] == TARGET_COMMIT
    assert body["error_code"] is None
    assert body["message"]
    assert response.headers["cache-control"] == "no-store"
    pending = protocol.read_json_file(base / "requests" / protocol.PENDING_FILE)
    assert pending is not None
    assert pending["request_id"] == body["request_id"]
    assert (base / "requests" / protocol.PENDING_FILE).stat().st_mode & 0o777 == 0o640
    # 通道视图立即显示 queued
    view = (await logged_in.client.get("/v1/version/update")).json()
    assert view["current_job"]["request_id"] == body["request_id"]
    assert view["reason"] == "update_in_progress"


async def test_post_release_changed_409(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await _post(logged_in, _body(commit="c" * 40))
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "release_changed"
    assert not (base / "requests" / protocol.PENDING_FILE).exists()


async def test_post_installed_version_changed_409(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await _post(logged_in, _body(current_version="0.9.0"))
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "installed_version_changed"
    assert not (base / "requests" / protocol.PENDING_FILE).exists()


async def test_post_update_in_progress_409(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    _pending(base)
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_in_progress"


async def test_post_release_unverifiable_503(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    release["value"] = _latest(status="unavailable", release_id=None, tag=None, commit=None)
    response = await _post(logged_in)
    assert response.status_code == 503
    assert response.json()["error"]["detail"] == "release_unverifiable"
    assert not (base / "requests" / protocol.PENDING_FILE).exists()


async def test_post_concurrent_requests_single_flight(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    responses = await asyncio.gather(_post(logged_in), _post(logged_in))
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["error"]["detail"] == "update_in_progress"
    pending = protocol.read_json_file(base / "requests" / protocol.PENDING_FILE)
    assert pending is not None


async def test_update_errors_are_sanitized(
    logged_in: TestEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)

    async def boom() -> version_routes.LatestRelease:
        raise RuntimeError("secret-token /srv/ling/deploy")

    monkeypatch.setattr(version_routes, "_fetch_update_release", boom)
    response = await _post(logged_in)
    assert response.status_code == 503
    assert "secret-token" not in response.text
    assert "/srv/ling" not in response.text


async def test_status_files_cannot_forge_paths_or_logs(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    request = _pending(base)
    payload = request.to_job().to_dict()
    payload.update(
        {
            "state": "failed",
            "stage": "deploy",
            "error_code": "deploy_failed",
            "message": "rm -rf /srv/ling; cat /etc/passwd",
        }
    )
    protocol.atomic_write_json(base / "status" / protocol.CURRENT_FILE, payload, mode=0o644)
    response = await logged_in.client.get("/v1/version/update")
    assert response.json()["current_job"]["message"] == protocol.ERROR_MESSAGES["deploy_failed"]
    assert "/etc/passwd" not in response.text
    assert str(tmp_path) not in response.text


async def test_pending_written_by_api_is_valid_protocol(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await _post(logged_in)
    assert response.status_code == 201
    stored = json.loads((base / "requests" / protocol.PENDING_FILE).read_text(encoding="utf-8"))
    request = protocol.UpdateRequest.from_dict(stored)
    assert request is not None
    assert request.target_commit == TARGET_COMMIT
    assert request.current_commit == CURRENT_COMMIT


async def test_post_needs_manual_blocks_new_submission(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    """needs_manual 必须由宿主显式解除，API 不能再发起新更新。"""
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    stuck = protocol.build_request(
        target_tag=TARGET_TAG,
        target_commit=TARGET_COMMIT,
        current_version=CURRENT_VERSION,
        current_commit=CURRENT_COMMIT,
    )
    protocol.atomic_write_json(
        base / "status" / protocol.CURRENT_FILE,
        stuck.to_job()
        .with_progress(state="needs_manual", stage="verify", error_code="verify_failed")
        .to_dict(),
        mode=0o644,
    )
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_in_progress"
    assert not (base / "requests" / protocol.PENDING_FILE).exists()
    view = (await logged_in.client.get("/v1/version/update")).json()
    assert view["reason"] == "update_in_progress"
    assert view["current_job"]["state"] == "needs_manual"
    assert view["current_job"]["request_id"] == stuck.request_id


async def test_post_terminal_failed_allows_retry(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    """failed 是终态：重新确认后允许发起新任务（新的 request_id）。"""
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    failed = protocol.build_request(
        target_tag=TARGET_TAG,
        target_commit=TARGET_COMMIT,
        current_version=CURRENT_VERSION,
        current_commit=CURRENT_COMMIT,
    )
    protocol.atomic_write_json(
        base / "status" / protocol.CURRENT_FILE,
        failed.to_job()
        .with_progress(state="failed", stage="build", error_code="build_failed")
        .to_dict(),
        mode=0o644,
    )
    response = await _post(logged_in)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["request_id"] != failed.request_id
    assert body["state"] == "queued"


@pytest.mark.parametrize(
    "origin",
    [
        "http://testserver.example",  # 不同主机
        "https://testserver:80",  # 同端口不同 scheme
        "http://testserver:443",  # 同主机不同端口
        "http://user:pass@testserver",  # 带 userinfo
        "http://testserver/v1/version",  # 带 path
        "http://testserver?next=1",  # 带 query
        "http://testserver#frag",  # 带 fragment
        "http://testserver ",  # 空白
    ],
)
async def test_post_rejects_non_bare_or_foreign_origin(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object], origin: str
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await logged_in.client.post(
        "/v1/version/update-requests",
        json=_body(),
        headers={"Origin": origin, "X-Ling-Update": "1"},
    )
    assert response.status_code == 403, (origin, response.text)
    assert response.json()["error"]["detail"] == "origin_mismatch"
    assert not (base / "requests" / protocol.PENDING_FILE).exists()


# ================================================================ QA M1/M4/B1（API 侧）


async def test_view_reports_protocol_unsupported_without_status(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """旧执行器心跳没有 status：不能当成“在线空闲”，必须拒绝发起更新。"""
    base = _configure(logged_in, tmp_path)
    _heartbeat(base, status=None)
    view = (await logged_in.client.get("/v1/version/update")).json()
    assert view["enabled"] is True
    assert view["reason"] == "protocol_unsupported"
    assert view["executor"]["online"] is False


async def test_view_reports_protocol_unsupported_for_future_schema(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base, schema_version="v2")
    view = (await logged_in.client.get("/v1/version/update")).json()
    assert view["reason"] == "protocol_unsupported"


async def test_post_rejects_invalid_pending_and_preserves_file(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base, pending_state="invalid")
    pending = base / "requests" / protocol.PENDING_FILE
    pending.write_text('{"schema_version": "v9"}', encoding="utf-8")

    view = (await logged_in.client.get("/v1/version/update")).json()
    assert view["reason"] == "protocol_unsupported"

    response = await _post(logged_in)
    assert response.status_code == 503
    assert response.json()["error"]["detail"] == "protocol_unsupported"
    # 不可信文件必须原样留着等人工处理（既不删也不覆盖）。
    assert pending.read_text(encoding="utf-8") == '{"schema_version": "v9"}'


async def test_post_never_overwrites_leftover_pending(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    """B1：current 已终态、pending 残留同一 id 时，API 不能覆盖（会吞掉宿主的 claim）。"""
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    leftover = _pending(base)
    protocol.atomic_write_json(
        base / "status" / protocol.CURRENT_FILE,
        leftover.to_job()
        .with_progress(state="failed", stage="build", error_code="build_failed")
        .to_dict(),
        mode=0o644,
    )
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_in_progress"
    stored = json.loads((base / "requests" / protocol.PENDING_FILE).read_text(encoding="utf-8"))
    assert stored["request_id"] == leftover.request_id


async def test_post_manual_heartbeat_reports_manual_required(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base, status="manual")
    response = await _post(logged_in)
    assert response.status_code == 503
    assert response.json()["error"]["detail"] == "manual_required"
    assert not (base / "requests" / protocol.PENDING_FILE).exists()


async def test_post_rejects_oversized_body_by_content_length(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)
    response = await logged_in.client.post(
        "/v1/version/update-requests",
        content=b"x" * (version_routes.MAX_UPDATE_BODY_BYTES + 1),
        headers={
            "Origin": "http://testserver",
            "X-Ling-Update": "1",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == version_routes.DETAIL_BODY_TOO_LARGE
    assert not (base / "requests" / protocol.PENDING_FILE).exists()


async def test_post_rejects_chunked_oversized_body_without_buffering(
    logged_in: TestEnv, tmp_path: Path, release: dict[str, object]
) -> None:
    """M4：没有 Content-Length（chunked/伪造）时也要边读边限长。"""
    base = _configure(logged_in, tmp_path)
    _heartbeat(base)

    chunk = b"x" * 1024

    async def body() -> AsyncIterator[bytes]:
        # 异步生成器作为 content → httpx 用 chunked 传输，不带 Content-Length。
        for _ in range(version_routes.MAX_UPDATE_BODY_BYTES // 1024 + 2):
            yield chunk

    response = await logged_in.client.post(
        "/v1/version/update-requests",
        content=body(),
        headers={
            "Origin": "http://testserver",
            "X-Ling-Update": "1",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == version_routes.DETAIL_BODY_TOO_LARGE


# ================================================================ QA M7：GitHub 直连


def test_github_client_ignores_env_proxy_and_netrc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """httpx 默认 trust_env=True 会带上 HTTP(S)_PROXY/ALL_PROXY 与 ~/.netrc。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    client = version_routes._github_client()
    try:
        assert client.follow_redirects is False
        assert client._trust_env is False
        assert client._mounts == {}
    finally:
        asyncio.run(client.aclose())


def _mock_github_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        trust_env=False,
    )


def test_release_fetch_rejects_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "https://evil.invalid/latest"})

    monkeypatch.setattr(
        version_routes, "_github_client", lambda: _mock_github_client(handler)
    )
    assert asyncio.run(version_routes._fetch_latest_release()) == ("unavailable", None)


def test_release_fetch_rejects_oversized_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    big = json.dumps({"tag_name": "v9.9.9", "padding": "x" * (300 * 1024)}).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big)

    monkeypatch.setattr(
        version_routes, "_github_client", lambda: _mock_github_client(handler)
    )
    # 流式读到上限就中止，绝不把整份响应缓冲进内存再判断。
    assert asyncio.run(version_routes._fetch_latest_release()) == ("unavailable", None)


def test_release_fetch_accepts_small_ok_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    body = json.dumps({"tag_name": "v9.9.9", "published_at": None}).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    monkeypatch.setattr(
        version_routes, "_github_client", lambda: _mock_github_client(handler)
    )
    status, payload = asyncio.run(version_routes._fetch_latest_release())
    assert status == "ok"
    assert payload is not None and payload["tag_name"] == "v9.9.9"


def test_tag_resolution_stops_on_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.invalid/"})

    monkeypatch.setattr(
        version_routes, "_github_client", lambda: _mock_github_client(handler)
    )
    assert asyncio.run(version_routes._resolve_tag_commit("v1.2.0")) is None
