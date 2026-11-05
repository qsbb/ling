"""镜像更新 API 的集成测试（真实 PostgreSQL + ASGI；不联网、不碰 Docker）。

设计依据：``docs/EASY-INSTALL-UPDATE-DESIGN.md`` §4（更新器与信任边界）、§5（一次
网页更新的实际顺序）、§6（故障时给用户什么结果）；``docs/IMAGE-DEPLOYMENT-DESIGN.md``
§2 最后一行（旧 ``executor.py`` 不兼容镜像拓扑）。

覆盖：镜像模式判定、固定请求体（多余字段/非 digest/未确认）、原子写与文件权限、
任务 ID 重入、开关/在线/协议/恢复点/当前镜像门禁、限流、最近登录、脱敏白名单、
旧的源码端点被显式拒绝，以及源码模式下旧端点行为不变。

两条**必修**回归各有一组用例：

- ``backup_gate``（与 task 无关的恢复点门禁）：有备份、没有 task 也要可安装；
  没有备份时报"没有可用的恢复点"而不是"更新器离线"；旧状态文件仍回退到
  ``task.backup``；字段存在时以它为准（不回退）。
- ``LING_APP_IMAGE`` 注入 api/worker 的 ``environment``：否则 API 拿不到当前摘要，
  ``GET`` 报 ``current_image_unknown``、``POST`` 409。
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
import sys
import urllib.parse
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select

from ling.api.routes import version as version_routes
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import AuthSession
from ling.infra.security import hash_token
from ling.updates import protocol as update_protocol
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_COMPOSE = REPO_ROOT / "deploy" / "compose.image.yaml"

CURRENT_DIGEST = "ghcr.io/qsbb/ling-api@sha256:" + "a" * 64
TARGET_DIGEST = "ghcr.io/qsbb/ling-api@sha256:" + "b" * 64
OTHER_DIGEST = "ghcr.io/qsbb/ling-api@sha256:" + "c" * 64
CURRENT_COMMIT = "a" * 40
TARGET_COMMIT = "b" * 40
NEXT_DIGEST = "ghcr.io/qsbb/ling-api@sha256:" + "d" * 64
NEXT_COMMIT = "d" * 40
CURRENT_VERSION = "1.0.0"
# 目标版本必须**严格大于**当前版本：可信发布目录只列"只允许升级"的目标。
TARGET_VERSION = "1.1.0"
MIN_FROM = [CURRENT_VERSION]
ORIGIN = {"Origin": "http://testserver"}
_STALE = "2020-01-01T00:00:00Z"
_MISSING = object()


@pytest.fixture(autouse=True)
def _fixed_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定构建提交/镜像引用与版本号：门禁比对的是这些值。"""
    monkeypatch.setenv("LING_COMMIT", CURRENT_COMMIT)
    monkeypatch.setenv("LING_APP_IMAGE", CURRENT_DIGEST)
    monkeypatch.delenv("LING_RUN_MIGRATIONS", raising=False)
    monkeypatch.setattr(version_routes, "current_version", lambda: CURRENT_VERSION)


# ---------------------------------------------------------------- 环境构造


def _configure(
    env: TestEnv,
    tmp_path: Path,
    *,
    enabled: bool = True,
    requests_dir: bool = True,
    status_dir: bool = True,
) -> Path:
    """把更新通道目录指到临时目录，并按需替换 app.state.settings。"""
    base = tmp_path / "channel"
    base.mkdir(exist_ok=True)
    reqs = base / "requests"
    stat = base / "status"
    if requests_dir:
        reqs.mkdir()
    if status_dir:
        stat.mkdir()
    settings = dataclasses.replace(
        env.settings,
        update_enabled=enabled,
        update_requests_dir=reqs,
        update_status_dir=stat,
    )
    env.app.state.settings = settings  # type: ignore[attr-defined]
    return base


def _body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "request_id": uuid.uuid4().hex,
        "target_digest": TARGET_DIGEST,
        "expected_current_digest": CURRENT_DIGEST,
        "expected_current_commit": CURRENT_COMMIT,
        "operator_confirmed": True,
    }
    body.update(overrides)
    return body


def _backup(
    *,
    complete: bool = True,
    backup_id: str = "backup-0123456789ab",
    tombstones: str = "ok",
    media: str = "present",
    keys_root: str = "present",
    keys_env: str = "false",
) -> dict[str, object]:
    return {
        "id": backup_id,
        "complete": complete,
        "tombstones_state": tombstones,
        "media_state": media,
        "keys_root": keys_root,
        "keys_env_set": keys_env,
    }


def _gate(
    *,
    restore_point: bool = True,
    complete: bool = True,
    backup_id: str = "20260101-000000",
    tombstones: str = "ok",
    media: str = "present",
    keys_root: str = "present",
    keys_env: str = "true",
    reason: str = "ok",
    checked_at: str | None = None,
) -> dict[str, object]:
    """更新器 ``status.json`` 顶层 ``backup_gate`` 的固定结构（扫描备份目录得出）。"""
    return {
        "restore_point": restore_point,
        "backup_id": backup_id,
        "complete": complete,
        "tombstones_state": tombstones,
        "media_state": media,
        "keys_root": keys_root,
        "keys_env_set": keys_env,
        "reason": reason,
        "checked_at": checked_at or update_protocol.iso_now(),
    }


def _available_target(
    *,
    version: str = TARGET_VERSION,
    digest: str = TARGET_DIGEST,
    commit: str = TARGET_COMMIT,
    has_migration: bool = True,
    compatible: bool = True,
    incompatible_reason: str | None = None,
) -> dict[str, object]:
    """更新器 ``status.json`` 里的一条可用目标（``available_targets[i]``）。"""
    return {
        "version": version,
        "digest": digest,
        "commit": commit,
        "has_migration": has_migration,
        "compatible": compatible,
        "incompatible_reason": incompatible_reason,
    }


def _catalog(
    *,
    state: str = "ok",
    reason: str | None = None,
    entries: int = 1,
    current_version: str | None = CURRENT_VERSION,
) -> dict[str, object]:
    """更新器 ``status.json`` 顶层 ``release_catalog``（可信发布目录的结论）。"""
    return {
        "state": state,
        "reason": reason,
        "entries": entries,
        "current_version": current_version,
    }


def _task(
    *,
    state: str = "succeeded",
    stage: str = "verify",
    backup: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    task: dict[str, object] = {
        "request_id": "0123456789abcdef0123456789abcdef",
        "state": state,
        "stage": stage,
        "started_at": "2026-11-04T00:00:00Z",
        "updated_at": "2026-11-04T00:01:00Z",
        "finished_at": "2026-11-04T00:01:00Z",
        "target": {"digest": CURRENT_DIGEST, "commit": CURRENT_COMMIT},
        "previous": {"digest": OTHER_DIGEST, "commit": TARGET_COMMIT},
        "migration": {
            "attempted": True,
            "current_before": "0001_initial",
            "current_after": "0002_next",
            "target_head": "0002_next",
        },
        "backup": _backup() if backup is None else backup,
        "maintenance": {"state": "released", "verified": True},
        "rollback": {"attempted": False, "verified": False},
        "still_on_old_version": False,
        "failure": None,
        "warnings": [],
        "next_actions": [],
    }
    task.update(extra)
    return task


def _write_status(
    base: Path,
    *,
    state: str = "idle",
    protocol: int = 1,
    schema: int = 1,
    updated_at: str | None = None,
    task: object = _MISSING,
    backup_gate: object = _MISSING,
    release_catalog: object = _MISSING,
    available_targets: object = _MISSING,
    recommended_target: object = _MISSING,
) -> None:
    payload: dict[str, object] = {
        "schema": schema,
        "protocol": protocol,
        "updated_at": updated_at or update_protocol.iso_now(),
        "updater": {"state": state, "version": "1.0.0"},
        "task": _task() if task is _MISSING else task,
    }
    # 默认**不写** backup_gate：老更新器写出的状态文件就没有这个字段，
    # 这样所有既有用例继续覆盖"回退到 task.backup"的兼容路径。
    if backup_gate is not _MISSING:
        payload["backup_gate"] = backup_gate
    # 可信发布目录的默认值：一条**兼容**目标（1.1.0 > 1.0.0，min_from 含 1.0.0），
    # 于是"其余门禁都过 → 可安装"的既有用例继续成立，而专门的目录用例显式覆盖它。
    payload["release_catalog"] = _catalog() if release_catalog is _MISSING else release_catalog
    payload["available_targets"] = (
        [_available_target()] if available_targets is _MISSING else available_targets
    )
    payload["recommended_target"] = (
        {key: value for key, value in _available_target().items() if key not in
         ("compatible", "incompatible_reason")}
        if recommended_target is _MISSING
        else recommended_target
    )
    base.joinpath("status").mkdir(exist_ok=True)
    update_protocol.atomic_write_json(
        base / "status" / version_routes.IMAGE_UPDATER_STATUS_FILE, payload, mode=0o644
    )


def _write_pending_request(base: Path, request_id: str | None = None) -> str:
    """模拟 API 已经写过一条请求（更新器还没认领）。"""
    request_id = request_id or uuid.uuid4().hex
    payload = {
        "schema": 1,
        "request_id": request_id,
        "target_digest": TARGET_DIGEST,
        "expected_current_digest": CURRENT_DIGEST,
        "expected_current_commit": CURRENT_COMMIT,
        "operator_confirmed": True,
        "created_at": update_protocol.iso_now(),
    }
    base.joinpath("requests").mkdir(exist_ok=True)
    update_protocol.atomic_write_json(
        base / "requests" / f"request-{request_id}.json", payload, mode=0o640
    )
    return request_id


def _post(env: TestEnv, body: dict[str, object] | None = None, **kwargs: object):
    headers = {**ORIGIN, "X-Ling-Update": "1", **kwargs.pop("headers", {})}
    return env.client.post(
        "/v1/version/image-update-requests",
        json=_body() if body is None else body,
        headers=headers,
        **kwargs,
    )


def _request_files(base: Path) -> list[str]:
    directory = base / "requests"
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.name.startswith("request-"))


# ---------------------------------------------------------------- 认证与权限


async def test_image_endpoints_require_auth(web_env: TestEnv) -> None:
    for method, path in (
        ("get", "/v1/version/image-update"),
        ("post", "/v1/version/image-update-requests"),
    ):
        response = await getattr(web_env.client, method)(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthorized"


async def test_post_non_owner_is_forbidden(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="member-image", password="member-password", role="member"
        )
    assert (
        await logged_in.client.post(
            "/v1/auth/login", json={"username": "member-image", "password": "member-password"}
        )
    ).status_code == 200
    response = await _post(logged_in)
    assert response.status_code == 403
    assert response.json()["error"]["detail"] == "not_owner"
    assert _request_files(base) == []
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "not_owner"


async def test_post_cookie_requires_same_origin_and_update_header(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    missing_origin = await logged_in.client.post(
        "/v1/version/image-update-requests", json=_body(), headers={"X-Ling-Update": "1"}
    )
    assert missing_origin.status_code == 403
    assert missing_origin.json()["error"]["detail"] == "origin_required"
    foreign = await logged_in.client.post(
        "/v1/version/image-update-requests",
        json=_body(),
        headers={"Origin": "http://evil.example", "X-Ling-Update": "1"},
    )
    assert foreign.status_code == 403
    assert foreign.json()["error"]["detail"] == "origin_mismatch"
    no_header = await logged_in.client.post(
        "/v1/version/image-update-requests", json=_body(), headers=ORIGIN
    )
    assert no_header.status_code == 422
    assert no_header.json()["error"]["detail"] == "update_header_required"
    assert _request_files(base) == []


async def test_post_requires_recent_login(logged_in: TestEnv, tmp_path: Path) -> None:
    """会话本身仍有效，但建立时间超过 15 分钟 → 拒绝（服务端按会话表判定）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    token = logged_in.client.cookies.get(logged_in.settings.cookie_name)
    assert token
    async with session_scope() as session:
        row = (
            await session.execute(
                select(AuthSession).where(AuthSession.token_hash == hash_token(token))
            )
        ).scalar_one()
        row.created_at = datetime.now(UTC) - timedelta(minutes=45)
    response = await _post(logged_in)
    assert response.status_code == 403
    assert response.json()["error"]["detail"] == "recent_login_required"
    assert "15 分钟" in response.json()["error"]["message"]
    assert _request_files(base) == []

    # 重新登录（新会话）后即可提交。
    assert (
        await logged_in.client.post(
            "/v1/auth/login", json={"username": "tester", "password": "tester-password"}
        )
    ).status_code == 200
    assert (await _post(logged_in)).status_code == 201, "重新登录后应恢复"


async def test_post_rate_limited(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    _write_pending_request(base)  # 有在途请求 → 前几次是 409，最后触发限流
    limit = version_routes.IMAGE_UPDATE_RATE_LIMIT
    statuses = [(await _post(logged_in)).status_code for _ in range(limit)]
    assert statuses == [409] * limit
    blocked = await _post(logged_in)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert blocked.json()["error"]["detail"] == "rate_limited"
    assert len(_request_files(base)) == 1


# ---------------------------------------------------------------- 请求体（固定格式）


async def test_post_writes_atomic_request_file(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    body = _body()
    response = await _post(logged_in, body)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["request_id"] == body["request_id"]
    assert payload["state"] == "requested"
    assert payload["target_digest"] == TARGET_DIGEST
    assert payload["expected_current_digest"] == CURRENT_DIGEST
    assert payload["expected_current_commit"] == CURRENT_COMMIT

    path = base / "requests" / f"request-{body['request_id']}.json"
    assert path.is_file()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored) == {
        "schema",
        "request_id",
        "target_digest",
        "expected_current_digest",
        "expected_current_commit",
        "operator_confirmed",
        "created_at",
    }
    assert stored["schema"] == 1
    assert stored["request_id"] == body["request_id"]
    assert stored["target_digest"] == TARGET_DIGEST
    assert stored["expected_current_digest"] == CURRENT_DIGEST
    assert stored["expected_current_commit"] == CURRENT_COMMIT
    assert stored["operator_confirmed"] is True
    assert update_protocol.parse_iso_timestamp(stored["created_at"]) is not None
    assert path.stat().st_mode & 0o777 == 0o640
    # 原子写：只剩请求文件与单飞锁，没有临时文件残留。
    assert sorted(p.name for p in (base / "requests").iterdir()) == [".lock", path.name]

    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "update_in_progress"
    assert view["request"] == {
        "request_id": body["request_id"],
        "readable": True,
        "target_digest": TARGET_DIGEST,
        "created_at": stored["created_at"],
    }


@pytest.mark.parametrize(
    "digest",
    [
        "latest",
        "ghcr.io/qsbb/ling-api:latest",
        "ghcr.io/qsbb/ling-api@sha256:" + "a" * 63,
        "ghcr.io/qsbb/ling-api@sha256:" + "a" * 65,
        "ghcr.io/qsbb/ling-api@sha256:" + "A" * 64,
        "ghcr.io/other/ling-api@sha256:" + "a" * 64,
        "docker.io/qsbb/ling-api@sha256:" + "a" * 64,
        "ghcr.io/qsbb/ling-api@sha256:" + "a" * 64 + " ",
        "ghcr.io/qsbb/ling-api@sha256:" + "a" * 64 + "\n",
    ],
)
async def test_post_rejects_non_digest_target(
    logged_in: TestEnv, tmp_path: Path, digest: str
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await _post(logged_in, _body(target_digest=digest))
    assert response.status_code == 422, (digest, response.text)
    assert response.json()["error"]["detail"] == "invalid_target_digest"
    assert _request_files(base) == []


@pytest.mark.parametrize("confirmed", ["true", 1, False, None])
async def test_post_requires_operator_confirmation(
    logged_in: TestEnv, tmp_path: Path, confirmed: object
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await _post(logged_in, _body(operator_confirmed=confirmed))
    assert response.status_code == 422, (confirmed, response.text)
    assert response.json()["error"]["detail"] == "operator_not_confirmed"
    assert _request_files(base) == []


async def test_post_rejects_missing_fields_and_extra_fields(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    without_confirm = _body()
    without_confirm.pop("operator_confirmed")
    response = await _post(logged_in, without_confirm)
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == "invalid_request_body"

    # 多余字段：不给未来的「顺带传 tag/shell/compose」留后门。
    extra = _body()
    extra["target_tag"] = "v1.2.0"
    response = await _post(logged_in, extra)
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == "invalid_request_body"

    nested = _body()
    nested["expected"] = {"tag": "v1.2.0"}
    response = await _post(logged_in, nested)
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == "invalid_request_body"
    assert _request_files(base) == []


@pytest.mark.parametrize(
    "request_id",
    ["", "ABC", "a" * 31, "a" * 33, "A" * 32, "upd_" + "a" * 32, 123, "a" * 32 + "\n"],
)
async def test_post_rejects_bad_request_id(
    logged_in: TestEnv, tmp_path: Path, request_id: object
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await _post(logged_in, _body(request_id=request_id))
    assert response.status_code == 422, (request_id, response.text)
    assert response.json()["error"]["detail"] == "invalid_request_id"
    assert _request_files(base) == []


async def test_post_rejects_oversized_body(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await logged_in.client.post(
        "/v1/version/image-update-requests",
        content=b"x" * (version_routes.MAX_UPDATE_BODY_BYTES + 1),
        headers={**ORIGIN, "X-Ling-Update": "1", "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == version_routes.DETAIL_BODY_TOO_LARGE
    assert _request_files(base) == []


# ---------------------------------------------------------------- 冲突/预期值


async def test_post_duplicate_request_id_409(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    body = _body()
    assert (await _post(logged_in, body)).status_code == 201
    conflict = await _post(logged_in, body)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["detail"] == "duplicate_request_id"
    assert _request_files(base) == [f"request-{body['request_id']}.json"]


async def test_post_archived_request_id_is_duplicate(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    request_id = uuid.uuid4().hex
    archive = base / "requests" / "archive"
    archive.mkdir()
    (archive / f"claimed-{request_id}.json").write_text("{}", encoding="utf-8")
    response = await _post(logged_in, _body(request_id=request_id))
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "duplicate_request_id"


async def test_post_rejects_stale_expected_current(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    wrong_digest = await _post(logged_in, _body(expected_current_digest=OTHER_DIGEST))
    assert wrong_digest.status_code == 409
    assert wrong_digest.json()["error"]["detail"] == "installed_image_changed"
    wrong_commit = await _post(logged_in, _body(expected_current_commit=TARGET_COMMIT))
    assert wrong_commit.status_code == 409
    assert wrong_commit.json()["error"]["detail"] == "installed_version_changed"
    same_target = await _post(logged_in, _body(target_digest=CURRENT_DIGEST))
    assert same_target.status_code == 409
    assert same_target.json()["error"]["detail"] == "already_on_target"
    assert _request_files(base) == []


async def test_post_in_progress_when_pending_exists(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    pending = _write_pending_request(base)
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_in_progress"
    # 绝不覆盖在途请求（宿主靠 rename 认领）。
    assert _request_files(base) == [f"request-{pending}.json"]


# ---------------------------------------------------------------- 开关与门禁


async def test_post_disabled_is_rejected(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path, enabled=False)
    _write_status(base)
    response = await _post(logged_in)
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["detail"] == "update_disabled"
    assert "LING_UPDATE_ENABLED" in body["error"]["message"]
    assert _request_files(base) == []
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["enabled"] is False
    assert view["installable"] is False
    assert view["reason"] == "update_disabled"
    assert view["next_actions"] == list(version_routes.IMAGE_REASON_NEXT_ACTIONS["update_disabled"])


async def test_post_disabled_by_default_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``LING_UPDATE_ENABLED`` 缺省必须是 0（构造一份没被测试覆盖过的 Settings）。"""
    monkeypatch.delenv("LING_UPDATE_ENABLED", raising=False)
    from ling.config import Settings

    assert Settings().update_enabled is False


async def test_status_file_missing_is_not_idle(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "updater_status_missing"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "updater_status_missing"
    assert view["updater"]["online"] is False
    assert view["updater"]["state"] != "idle"  # 不伪装成空闲
    assert view["updater"]["state"] == "unknown"
    assert view["message"] and "状态文件" in view["message"]
    assert _request_files(base) == []


async def test_status_file_invalid_is_rejected(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    (base / "status" / "status.json").write_text("{not json", encoding="utf-8")
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "updater_status_invalid"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["reason"] == "updater_status_invalid"
    assert view["updater"]["state"] == "unknown"


async def test_unknown_status_schema_is_invalid(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, schema=2)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["reason"] == "updater_status_invalid"
    assert view["task"] is None


async def test_post_updater_offline_is_rejected(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, updated_at=_STALE)
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "updater_offline"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "updater_offline"
    assert view["updater"]["online"] is False
    assert view["updater"]["state"] == "idle"  # 文件说的状态照实给
    assert view["updater"]["age_seconds"] and view["updater"]["age_seconds"] > 120
    assert _request_files(base) == []


async def test_protocol_mismatch_is_rejected(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, protocol=2)
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "protocol_unsupported"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["reason"] == "protocol_unsupported"
    assert view["updater"]["protocol"] == 2
    assert view["installable"] is False
    assert _request_files(base) == []


async def test_manual_required_blocks_new_request(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, state="needs_manual", task=_task(state="needs_manual"))
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "manual_required"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["reason"] == "manual_required"
    assert view["task"]["state"] == "needs_manual"
    assert view["task"]["state_message"] == "更新未完成，需要维护处理"


async def test_running_task_blocks_and_shows_real_stage(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, state="busy", task=_task(state="running", stage="backup"))
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_in_progress"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["reason"] == "update_in_progress"
    assert view["task"]["stage"] == "backup"
    assert view["task"]["stage_message"] == "正在做一致备份（恢复点）"
    # 只用真实阶段，不给假的百分比/剩余时间。
    assert "percent" not in json.dumps(view)
    assert "eta" not in json.dumps(view)


@pytest.mark.parametrize(
    "backup",
    [
        _backup(complete=False),
        _backup(media="partial"),
        _backup(tombstones="unknown"),
        _backup(keys_root="absent", keys_env="false"),
        _backup(backup_id=""),
    ],
)
async def test_restore_point_gate(logged_in: TestEnv, tmp_path: Path, backup: dict[str, object]) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=_task(backup=backup))
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "backup_gate_missing"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "backup_gate_missing"
    assert view["backup_gate"]["restore_point"] is False
    assert "恢复点" in (view["message"] or "")
    assert _request_files(base) == []


async def test_no_task_means_no_restore_point(logged_in: TestEnv, tmp_path: Path) -> None:
    """**旧状态文件**（没有 ``backup_gate``）且没有 task → 没有恢复点。

    这条用例守的是"回退到 ``task.backup``"的兼容路径：不是全新部署的正常路径。
    改前它是死锁的复现（有备份也一样不可安装）；修法见下面 ``backup_gate`` 的用例。
    """
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=None)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "backup_gate_missing"
    assert view["task"] is None
    assert view["backup_gate"]["source"] is None


# ------------------------------------------------- 恢复点门禁（与 task 无关的 backup_gate）


def _compose_service_env(service: str) -> dict[str, object]:
    """按 compose 的合并语义取服务真正会收到的 environment（含 `<<: *app-env`）。"""
    data = yaml.safe_load(IMAGE_COMPOSE.read_text(encoding="utf-8"))
    env = (data.get("services") or {}).get(service, {}).get("environment") or {}
    assert isinstance(env, dict), f"{service}.environment 必须是映射"
    return dict(env)


async def test_backup_gate_without_task_is_installable(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """回归（必修 A）：备份目录里有可用恢复点、status 里**没有 task** → 可安装。

    改前：``restore_point`` 只从 ``task.backup`` 推导，task 为 null ⇒ 永远
    ``backup_gate_missing``。全新镜像部署的第一次网页更新因此永远不可能发生。
    """
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=None, backup_gate=_gate())
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["task"] is None
    assert view["installable"] is True, view
    assert view["reason"] is None
    assert view["message"] is None
    assert view["backup_gate"]["restore_point"] is True
    assert view["backup_gate"]["source"] == "backup_gate"
    assert view["backup_gate"]["backup_id"] == "20260101-000000"
    assert view["backup_gate"]["complete"] is True
    assert view["backup_gate"]["media_state"] == "present"
    assert view["backup_gate"]["checked_at"] is not None
    # 门禁真的放行：POST 能写出请求文件。
    response = await _post(logged_in)
    assert response.status_code == 201, response.text
    assert len(_request_files(base)) == 1


async def test_missing_backup_gate_and_no_task_is_not_updater_offline(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """没有 ``backup_gate``、没有 task → 「没有可用的恢复点」，**不是**「更新器离线」。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=None)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["updater"]["online"] is True
    assert view["installable"] is False
    assert view["reason"] == "backup_gate_missing"
    assert view["reason"] != "updater_offline"
    assert "没有可用的恢复点" in (view["message"] or "")
    assert "离线" not in (view["message"] or "")
    assert view["backup_gate"]["restore_point"] is False
    assert _request_files(base) == []


async def test_stale_backup_gate_is_still_installable(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """``checked_at`` 很早**不**单独判不可安装（刻意取舍，理由见下）。

    更新器只在「启动」与「处理完任务」时重算并写 ``checked_at``：一个健康、长期
    空闲的更新器写出很早的时间是**正常状态**，不是故障。状态文件的新鲜度已经由
    ``updater.online``（``updated_at`` ≤ 120 秒）单独把关；再拿 ``checked_at``
    判一次"过期"会在毫无故障时把更新锁掉——等于把死锁换个形式装回来。
    "备份被删了、门禁却还说有"由更新器在迁移前重新核 manifest + 产物哈希兜底
    （``publish.Context.validate_backup_manifest``）。
    """
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=None, backup_gate=_gate(checked_at=_STALE))
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["backup_gate"]["checked_at"] == _STALE
    assert view["updater"]["online"] is True
    assert view["installable"] is True


@pytest.mark.parametrize(
    ("gate", "reason_text"),
    [
        (
            _gate(restore_point=False, complete=False, backup_id=None, reason="no_manifest"),
            "没有可用的恢复点",
        ),
        (
            _gate(restore_point=False, reason="manifest_unreadable"),
            "无法解析",
        ),
        (
            _gate(restore_point=False, media="partial", reason="media_partial"),
            "媒体",
        ),
        (
            _gate(restore_point=False, tombstones="FAILED", reason="tombstones_not_ok"),
            "墓碑",
        ),
        (
            _gate(restore_point=False, keys_root="absent", keys_env="false", reason="keys_missing"),
            "根密钥",
        ),
    ],
)
async def test_backup_gate_without_usable_restore_point_blocks(
    logged_in: TestEnv, tmp_path: Path, gate: dict[str, object], reason_text: str
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=None, backup_gate=gate)
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "backup_gate_missing"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "backup_gate_missing"
    assert view["backup_gate"]["restore_point"] is False
    assert view["backup_gate"]["source"] == "backup_gate"
    assert reason_text in (view["backup_gate"]["reason_message"] or "")
    assert _request_files(base) == []


async def test_backup_gate_true_is_not_trusted_without_complete(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """更新器说 ``restore_point=true`` 但 ``complete=false``：API 不盲信，按不可安装。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=None, backup_gate=_gate(restore_point=True, complete=False))
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "backup_gate_missing"
    assert view["backup_gate"]["restore_point"] is False


async def test_malformed_backup_gate_does_not_fall_back_to_task(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """``backup_gate`` 结构不认识 → 判无恢复点，**不**回退到万能的任务记录。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=_task(backup=_backup()), backup_gate="not-a-dict")
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "backup_gate_missing"
    assert view["backup_gate"]["source"] == "backup_gate"
    assert view["backup_gate"]["restore_point"] is False
    assert view["backup_gate"]["complete"] is False


async def test_backup_gate_takes_precedence_over_task_backup(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """``backup_gate`` 存在时**不**回退到 ``task.backup``：两处矛盾时按更严的那个。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        task=_task(backup=_backup()),  # 任务记录说恢复点齐全
        backup_gate=_gate(restore_point=False, complete=False, backup_id=None, reason="no_manifest"),
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "backup_gate_missing"
    assert view["backup_gate"]["source"] == "backup_gate"
    assert view["backup_gate"]["restore_point"] is False


async def test_backup_gate_media_present_is_a_valid_restore_point(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """``media_state=present`` 是 publish 真实写出的值，必须算完整（不是只认 ok）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=None, backup_gate=_gate(media="present"))
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["backup_gate"]["media_state"] == "present"
    assert view["installable"] is True


async def test_image_compose_injects_ling_app_image_into_api_env(
    logged_in: TestEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归（必修 B）：compose 必须把 ``LING_APP_IMAGE`` 注入 api/worker 的 env。

    改前 api 容器 env 有 47 项、**没有** ``LING_APP_IMAGE``：API 判断不出镜像模式，
    也拿不到当前摘要 → GET 报 ``current_image_unknown``、POST 409。
    这里直接按 compose 的 environment 取值（同一个必填插值），再证明 GET 不再报
    那个原因。
    """
    expected_prefix = "${LING_APP_IMAGE:?镜像模式必须先在受限 env 配置 LING_APP_IMAGE"
    for service in ("api", "worker"):
        env = _compose_service_env(service)
        value = env.get("LING_APP_IMAGE")
        assert isinstance(value, str), f"{service}.environment 缺 LING_APP_IMAGE"
        # 必填插值（`?` 不是 `:-`）：缺值时 `docker compose config` 直接失败。
        assert value.startswith(expected_prefix), value
        assert value.endswith("}") and ":-" not in value
    monkeypatch.delenv("LING_APP_IMAGE")
    monkeypatch.setenv("LING_APP_IMAGE", CURRENT_DIGEST)
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=None, backup_gate=_gate())
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["current"]["digest"] == CURRENT_DIGEST
    assert view["reason"] != "current_image_unknown"
    assert view["installable"] is True


async def test_busy_updater_blocks_without_task(logged_in: TestEnv, tmp_path: Path) -> None:
    """``busy`` 期间即使任务块暂时缺失也不能放行新请求。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, state="busy", task=None)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["reason"] == "update_in_progress"
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_in_progress"


async def test_requests_dir_missing_is_channel_unavailable(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path, requests_dir=False)
    _write_status(base)
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_channel_unavailable"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["reason"] == "update_channel_unavailable"


async def test_current_image_unknown_blocks_request(
    logged_in: TestEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """镜像模式成立（status.json 在），但没注入 LING_APP_IMAGE → 无法核对当前摘要。"""
    monkeypatch.delenv("LING_APP_IMAGE")
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "current_image_unknown"
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["reason"] == "current_image_unknown"
    assert view["image_mode"] is True
    assert view["current"]["digest"] is None
    assert _request_files(base) == []


async def test_get_installable_when_all_gates_pass(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await logged_in.client.get("/v1/version/image-update")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    view = response.json()
    assert view["schema_version"] == "v1"
    assert view["image_mode"] is True
    assert view["enabled"] is True
    assert view["installable"] is True
    assert view["reason"] is None
    assert view["message"] is None
    assert view["next_actions"] == []
    assert view["updater"]["online"] is True
    assert view["updater"]["protocol"] == 1
    assert view["current"] == {"digest": CURRENT_DIGEST, "commit": CURRENT_COMMIT}
    assert view["backup_gate"]["restore_point"] is True
    assert view["backup_gate"]["backup_id"] == "backup-0123456789ab"
    assert view["task"]["state"] == "succeeded"
    assert view["task"]["state_message"] == "更新完成"
    assert view["task"]["previous"]["digest"] == OTHER_DIGEST
    assert view["task"]["migration"]["current_after"] == "0002_next"
    assert view["task"]["migration"]["target_head"] == "0002_next"
    assert view["task"]["still_on_old_version"] is False
    assert view["request"] is None


# ------------------------------------------------ 可信发布目录：可用目标 / target


async def test_get_installable_exposes_whitelisted_target(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """有推荐目标 → 可安装，``target`` 只给 version/digest/commit/has_migration。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is True
    assert view["reason"] is None
    assert view["target"] == {
        "version": TARGET_VERSION,
        "digest": TARGET_DIGEST,
        "commit": TARGET_COMMIT,
        "has_migration": True,
    }
    assert set(view["target"]) == {"version", "digest", "commit", "has_migration"}
    assert view["available_targets"] == [
        {
            "version": TARGET_VERSION,
            "digest": TARGET_DIGEST,
            "commit": TARGET_COMMIT,
            "has_migration": True,
            "compatible": True,
            "incompatible_reason": None,
            "incompatible_reason_message": None,
        }
    ]
    assert view["catalog"] == {
        "state": "ok",
        "state_message": version_routes.IMAGE_CATALOG_STATE_MESSAGES["ok"],
        "reason": None,
        "reason_message": None,
        "entries": 1,
        "current_version": CURRENT_VERSION,
    }


async def test_no_available_target_is_not_installable(logged_in: TestEnv, tmp_path: Path) -> None:
    """没有兼容目标 → 不可安装，原因是固定码表文案（不是让调用方自己编摘要）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, available_targets=[], recommended_target=None)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"
    assert view["message"] == version_routes.IMAGE_REASON_MESSAGES["no_available_target"]
    assert view["message"] == "暂无可用的更新目标（发布目录里没有适用于当前版本的新版本）"
    assert view["next_actions"] == list(
        version_routes.IMAGE_REASON_NEXT_ACTIONS["no_available_target"]
    )
    assert view["target"] is None
    assert view["available_targets"] == []


async def test_incompatible_target_is_shown_but_not_installable(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """更新器判不兼容（min_from 不含当前版本）→ 展示原因，但不可安装。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        available_targets=[
            _available_target(compatible=False, incompatible_reason="not_in_min_from")
        ],
        recommended_target=None,
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"
    assert view["target"] is None
    entry = view["available_targets"][0]
    assert entry["compatible"] is False
    assert entry["incompatible_reason"] == "not_in_min_from"
    assert entry["incompatible_reason_message"] == (
        version_routes.IMAGE_INCOMPATIBLE_MESSAGES["not_in_min_from"]
    )


async def test_api_rechecks_that_target_is_newer(logged_in: TestEnv, tmp_path: Path) -> None:
    """更新器说兼容，但目标版本**不高于**当前版本 → API 自己判不可用（只允许升级）。"""
    base = _configure(logged_in, tmp_path)
    target = _available_target(version=CURRENT_VERSION)
    _write_status(base, available_targets=[target], recommended_target=target)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"
    assert view["target"] is None
    entry = view["available_targets"][0]
    assert entry["compatible"] is False
    assert entry["incompatible_reason"] == "not_newer"
    assert entry["incompatible_reason_message"] == (
        version_routes.IMAGE_INCOMPATIBLE_MESSAGES["not_newer"]
    )
    # POST 也走同一套结论：没有兼容目标 → 409，且不写请求文件。
    response = await _post(logged_in, _body(target_digest=TARGET_DIGEST))
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "no_available_target"
    assert _request_files(base) == []


async def test_recommended_target_wins_when_it_matches_a_compatible_entry(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """推荐目标与兼容目标一致 → 用它（不是简单地取版本最大的）。"""
    base = _configure(logged_in, tmp_path)
    newer = _available_target(version="1.2.0", digest=NEXT_DIGEST, commit=NEXT_COMMIT)
    recommended = _available_target()
    _write_status(
        base,
        available_targets=[recommended, newer],
        recommended_target={key: recommended[key] for key in ("version", "digest", "commit", "has_migration")},
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["target"]["version"] == TARGET_VERSION
    assert view["target"]["digest"] == TARGET_DIGEST


async def test_inconsistent_recommended_target_falls_back_to_newest_compatible(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """推荐目标与可用目标对不上（或被篡改）→ 不盲信，退回版本最大的兼容目标。"""
    base = _configure(logged_in, tmp_path)
    older = _available_target(version="1.1.0")
    newer = _available_target(version="1.2.0", digest=NEXT_DIGEST, commit=NEXT_COMMIT)
    _write_status(
        base,
        available_targets=[newer, older],
        recommended_target={
            "version": "9.9.9",
            "digest": OTHER_DIGEST,
            "commit": "f" * 40,
            "has_migration": True,
        },
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is True
    assert view["target"] == {
        "version": "1.2.0",
        "digest": NEXT_DIGEST,
        "commit": NEXT_COMMIT,
        "has_migration": True,
    }


@pytest.mark.parametrize(
    "entry",
    [
        {"version": "1.1.0", "digest": TARGET_DIGEST, "commit": TARGET_COMMIT, "has_migration": True},
        {
            "version": "latest",
            "digest": TARGET_DIGEST,
            "commit": TARGET_COMMIT,
            "has_migration": True,
            "compatible": True,
        },
        {
            "version": "1.1.0",
            "digest": "ghcr.io/qsbb/ling-api:latest",
            "commit": TARGET_COMMIT,
            "has_migration": True,
            "compatible": True,
        },
        {
            "version": "1.1.0",
            "digest": TARGET_DIGEST,
            "commit": "not-a-commit",
            "has_migration": True,
            "compatible": True,
        },
        {
            "version": "1.1.0",
            "digest": TARGET_DIGEST,
            "commit": TARGET_COMMIT,
            "has_migration": "true",
            "compatible": True,
        },
        "not-an-object",
    ],
)
async def test_malformed_target_entries_are_dropped(
    logged_in: TestEnv, tmp_path: Path, entry: object
) -> None:
    """状态文件里的一条目标结构不对 → 整条丢弃（不猜、不错位展示）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, available_targets=[entry], recommended_target=None)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["available_targets"] == []
    assert view["target"] is None
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"


async def test_target_entry_extra_fields_are_not_echoed(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """目标条目里的多余键（路径/日志）一律进不了响应。"""
    base = _configure(logged_in, tmp_path)
    entry = _available_target()
    entry["path"] = "/vol1/1000/ling/deploy/releases.json"
    entry["journal"] = "journal.log"
    entry["message"] = "ssh root@host"
    _write_status(base, available_targets=[entry])
    response = await logged_in.client.get("/v1/version/image-update")
    text = response.text
    for secret in ("/vol1/1000", "journal.log", "ssh root@host"):
        assert secret not in text, secret
    assert "/" not in response.json()["target"]["digest"].rsplit("@sha256:", 1)[1]


async def test_catalog_missing_reason_is_explicit(logged_in: TestEnv, tmp_path: Path) -> None:
    """目录缺失 → ``available_targets=[]`` + 明确原因（不静默）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_catalog=_catalog(
            state="missing", reason="catalog_missing", entries=0, current_version=None
        ),
        available_targets=[],
        recommended_target=None,
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"
    assert view["catalog"]["state"] == "missing"
    assert view["catalog"]["reason"] == "catalog_missing"
    assert view["catalog"]["reason_message"] == (
        version_routes.IMAGE_CATALOG_REASON_MESSAGES["catalog_missing"]
    )
    assert view["catalog"]["current_version"] is None


async def test_unknown_catalog_state_and_reason_are_not_echoed(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """未知的目录状态/原因码不转发原文，只给固定的"缺字段"形态。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_catalog={
            "state": "weird",
            "reason": "/vol1/1000/ling/deploy/releases.json",
            "entries": 10**9,
            "current_version": CURRENT_VERSION,
        },
        available_targets=[],
        recommended_target=None,
    )
    response = await logged_in.client.get("/v1/version/image-update")
    assert "/vol1/1000" not in response.text
    catalog = response.json()["catalog"]
    assert catalog["state"] is None
    assert catalog["reason"] is None
    assert catalog["entries"] == version_routes.IMAGE_MAX_TARGETS


async def test_post_rejects_target_outside_catalog(logged_in: TestEnv, tmp_path: Path) -> None:
    """目录里只有 1.1.0；提交另一个格式合格的摘要 → 409，且不写请求文件。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await _post(logged_in, _body(target_digest=OTHER_DIGEST))
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "target_not_in_catalog"
    assert response.json()["error"]["message"] == (
        version_routes.IMAGE_REASON_MESSAGES["target_not_in_catalog"]
    )
    assert _request_files(base) == []


async def test_post_accepts_the_catalog_target(logged_in: TestEnv, tmp_path: Path) -> None:
    """目录里的兼容目标可以提交（201）——目录不是只读展示，门禁也是它。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await _post(logged_in)
    assert response.status_code == 201, response.text
    assert response.json()["target_digest"] == TARGET_DIGEST
    assert len(_request_files(base)) == 1


async def test_source_mode_reports_not_applicable(
    logged_in: TestEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LING_APP_IMAGE")
    monkeypatch.delenv("LING_RUN_MIGRATIONS", raising=False)
    _configure(logged_in, tmp_path)  # 目录在，但没有 status.json / 镜像标记
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["image_mode"] is False
    assert view["installable"] is False
    assert view["reason"] == "image_mode_required"
    assert "源码模式" in (view["message"] or "")
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "image_mode_required"


# ---------------------------------------------------------------- 脱敏


async def test_get_never_leaks_paths_logs_or_secrets(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    task = _task(
        state="needs_manual",
        stage="verify",
        failure={
            "code": "switch_failed",
            "stage": "verify",
            "message": "ssh root@host; cat /vol1/1000/ling/deploy/.env",
        },
        warnings=["docker_free_unverified", "/vol1/1000/ling-backups/journal.log"],
        next_actions=["inspect_maintenance", "/vol1/1000/ling-updates/journal.log"],
    )
    backup = task["backup"]
    assert isinstance(backup, dict)
    backup["path"] = "/vol1/1000/ling-backups/2026-11-04T00-00-00"
    backup["password"] = "hunter2"
    task["journal"] = "/vol1/1000/ling-updates/journal.log"
    task["raw_log"] = "POSTGRES_PASSWORD=hunter2"
    _write_status(base, state="needs_manual", task=task)

    response = await logged_in.client.get("/v1/version/image-update")
    text = response.text
    for secret in (
        "/vol1/1000",
        "journal.log",
        "hunter2",
        "POSTGRES_PASSWORD",
        "ssh root@host",
        "raw_log",
        "password",
    ):
        assert secret not in text, secret
    body = response.json()
    assert body["task"]["failure"]["message"] == version_routes.IMAGE_FAILURE_MESSAGES["switch_failed"]
    assert body["task"]["failure"]["code"] == "switch_failed"
    assert body["task"]["warnings"] == [
        version_routes.IMAGE_WARNING_MESSAGES["docker_free_unverified"]
    ]
    assert body["task"]["next_actions"] == [
        version_routes.IMAGE_NEXT_ACTION_MESSAGES["inspect_maintenance"]
    ]
    assert set(body["task"]["backup"]) == {
        "id",
        "complete",
        "tombstones_state",
        "media_state",
        "keys_root",
        "keys_env_set",
    }


async def test_get_unknown_failure_code_is_not_echoed(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    task = _task()
    task["failure"] = {"code": "cat /etc/passwd", "stage": "deploy", "message": "/secret"}
    _write_status(base, task=task)
    response = await logged_in.client.get("/v1/version/image-update")
    assert "cat /etc/passwd" not in response.text
    assert "/secret" not in response.text
    body = response.json()
    assert body["task"]["failure"]["message"] == version_routes.IMAGE_FAILURE_FALLBACK
    assert body["task"]["failure"]["code"] is None


# ---------------------------------------------------------------- 旧端点：显式拒绝 / 行为不变


async def test_old_source_endpoint_rejected_in_image_mode(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await logged_in.client.post(
        "/v1/version/update-requests",
        json={"confirm_tag": "v1.2.0", "expected": {}},
        headers={**ORIGIN, "X-Ling-Update": "1"},
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["detail"] == "image_mode_requires_image_channel"
    assert "源码模式的更新通道不适用于镜像模式" in error["message"]
    assert "image-update-requests" in error["message"]
    assert not (base / "requests" / "pending.json").exists()


async def test_old_source_endpoint_rejected_via_run_migrations_marker(
    logged_in: TestEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有 LING_APP_IMAGE、也没有 status.json 时，compose.image.yaml 写死的
    ``LING_RUN_MIGRATIONS=0`` 仍是镜像模式的证据。"""
    monkeypatch.delenv("LING_APP_IMAGE")
    monkeypatch.setenv("LING_RUN_MIGRATIONS", "0")
    base = _configure(logged_in, tmp_path)
    response = await logged_in.client.post(
        "/v1/version/update-requests",
        json={},
        headers={**ORIGIN, "X-Ling-Update": "1"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "image_mode_requires_image_channel"
    assert not (base / "requests" / "pending.json").exists()


@pytest.fixture()
def source_release(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake() -> version_routes.LatestRelease:
        return version_routes.LatestRelease(
            status="ok",
            release_id=7,
            tag="v1.2.0",
            commit=TARGET_COMMIT,
            published_at="2026-11-01T00:00:00Z",
            html_url="https://github.com/qsbb/ling/releases/tag/v1.2.0",
            stable=True,
            is_newer=True,
        )

    monkeypatch.setattr(version_routes, "_fetch_update_release", fake)


async def test_old_source_endpoint_unchanged_in_source_mode(
    logged_in: TestEnv,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_release: None,
) -> None:
    monkeypatch.delenv("LING_APP_IMAGE")
    monkeypatch.delenv("LING_RUN_MIGRATIONS", raising=False)
    base = _configure(logged_in, tmp_path)
    update_protocol.atomic_write_json(
        base / "status" / update_protocol.HEARTBEAT_FILE,
        update_protocol.heartbeat_payload(
            last_seen_at=update_protocol.iso_now(),
            deployed_commit=CURRENT_COMMIT,
            current_version=CURRENT_VERSION,
            status="ok",
            pending_state="absent",
        ),
        mode=0o644,
    )
    response = await logged_in.client.post(
        "/v1/version/update-requests",
        json={
            "confirm_tag": "v1.2.0",
            "expected": {
                "release_id": 7,
                "tag": "v1.2.0",
                "commit": TARGET_COMMIT,
                "current_version": CURRENT_VERSION,
                "current_commit": CURRENT_COMMIT,
            },
        },
        headers={**ORIGIN, "X-Ling-Update": "1"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["state"] == "queued"
    assert (base / "requests" / update_protocol.PENDING_FILE).is_file()
    view = (await logged_in.client.get("/v1/version/update")).json()
    assert view["enabled"] is True
    assert view["executor"]["online"] is True
    assert view["current_job"]["state"] == "queued"

# ------------------------------------------- 探活免鉴权（缺陷 6 的防回归门禁）
#
# 真机演练 P5 第 6 个缺陷：``deploy/release/publish.py`` 拿 ``/v1/version`` 当
# 探活，而它挂 ``CurrentAuth``，未鉴权永远 401（容器里那条 exec 探针同样）。
# 单元测试的 HTTP stub 无条件回 200，所以一直是绿的。
#
# 现在发布/更新流程**不持有用户凭据**，HTTP 探活只允许打 ``HTTP_PROBE_PATHS``
# 里登记的免鉴权端点（当前只有 ``/readyz``）；"运行容器是不是目标构建"改用
# 容器内进程证据（``compose exec api python -c``）。
#
# 下面几条就是防回归：
#   1. 源码里所有本机 HTTP 探活路径 ⊆ 两个模块各自声明的表；
#   2. 表里每一条在**真实 FastAPI 应用**上都不是 401/403；
#   3. ``/v1/version`` 仍然要鉴权（没有被顺手改开）。

PUBLISH_PY = REPO_ROOT / "deploy" / "release" / "publish.py"
UPDATER_PY = REPO_ROOT / "deploy" / "updater" / "updater.py"
_URL_LITERAL_RE = re.compile(r"https?://[^\s'\"\\]+")
# 探活只打本机（127.0.0.1 / localhost）或 compose 插值出来的宿主地址
# （``http://{self.ready_host}:{self.published_port}/readyz``）。
_LOCAL_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})


def _load_deploy_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _probe_paths_in_source(path: Path) -> set[str]:
    """收集源码里"打本机的 HTTP 探活"目标路径（``{占位符}`` 形式的宿主也算）。"""
    paths: set[str] = set()
    for raw in _URL_LITERAL_RE.findall(path.read_text(encoding="utf-8")):
        parsed = urllib.parse.urlsplit(raw)
        hostname = parsed.hostname or ""
        if "{" not in hostname and hostname not in _LOCAL_HOSTNAMES:
            continue
        if parsed.path and parsed.path != "/":
            paths.add(parsed.path)
    return paths


def test_http_probe_paths_are_declared_and_match_between_modules() -> None:
    """新增 HTTP 探活必须登记到 ``HTTP_PROBE_PATHS``，且两个模块口径一致。"""
    publish = _load_deploy_module("ling_deploy_publish_probe", PUBLISH_PY)
    updater = _load_deploy_module("ling_deploy_updater_probe", UPDATER_PY)
    declared_publish = set(publish.HTTP_PROBE_PATHS)
    declared_updater = set(updater.HTTP_PROBE_PATHS)
    assert declared_publish == declared_updater, (declared_publish, declared_updater)
    found_publish = _probe_paths_in_source(PUBLISH_PY)
    assert found_publish, "发布脚本里没找到任何 HTTP 探活（正则或代码结构变了？）"
    missing = found_publish - declared_publish
    assert not missing, f"{PUBLISH_PY} 里有未登记的 HTTP 探活：{sorted(missing)}"
    # 更新器目前**完全复用** `publish.Context` 的探活实现（自己不打 HTTP）。
    # 它一旦自造探活，必须登记到 `HTTP_PROBE_PATHS`，否则这条立刻红。
    found_updater = _probe_paths_in_source(UPDATER_PY)
    missing_updater = found_updater - declared_updater
    assert not missing_updater, f"{UPDATER_PY} 里有未登记的 HTTP 探活：{sorted(missing_updater)}"


async def test_declared_http_probe_paths_are_unauthenticated_on_real_app(
    web_env: TestEnv,
) -> None:
    """每一条登记过的探活路径，在真实应用上未鉴权都必须**不是** 401/403。"""
    publish = _load_deploy_module("ling_deploy_publish_probe_auth", PUBLISH_PY)
    assert publish.HTTP_PROBE_PATHS, "探活路径表不能是空的"
    for probe in publish.HTTP_PROBE_PATHS:
        response = await web_env.client.get(probe)
        assert response.status_code not in (401, 403), (
            f"探活 {probe} 需要鉴权（{response.status_code}）：发布/更新流程不持有"
            f"用户凭据，这样的探活永远失败。响应：{response.text[:200]}"
        )
        assert response.status_code == 200, (probe, response.status_code, response.text[:200])


async def test_v1_version_still_requires_auth(web_env: TestEnv) -> None:
    """``/v1/version`` 仍然要鉴权：没有为了给探活让路而把它改开。"""
    response = await web_env.client.get("/v1/version")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthorized"
    assert (await web_env.client.get("/readyz")).status_code == 200


def test_container_probe_uses_process_evidence_without_http() -> None:
    """提交证据来自容器内进程：探针字符串里没有任何 HTTP/端点字样。"""
    publish = _load_deploy_module("ling_deploy_publish_probe_script", PUBLISH_PY)
    script = publish.CONTAINER_VERSION_PROBE
    for forbidden in ("http", "urllib", "/v1/", "socket"):
        assert forbidden not in script, (forbidden, script)
    assert "LING_COMMIT" in script
    assert "__version__" in script
