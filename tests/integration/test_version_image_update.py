"""镜像更新 API 的集成测试（真实 PostgreSQL + ASGI；不联网、不碰 Docker）。

设计依据：``docs/UPDATE-SIMPLIFICATION-DESIGN.md`` §3/§6/§8（用 registry 发现版本、
删掉可信发布目录与 ``min_from``）、``docs/UPDATE-UX-DESIGN.md`` §卡片与四态；
``docs/IMAGE-DEPLOYMENT-DESIGN.md`` §2 最后一行（旧 ``executor.py`` 不兼容镜像拓扑）。

覆盖：镜像模式判定、固定请求体（多余字段/非 digest/未确认）、原子写与文件权限、
任务 ID 重入、开关/在线/协议/恢复点/当前镜像门禁、限流、最近登录（**只在安装
请求上**）、脱敏白名单、四态投影（唯一事实源 = 更新器的 ``release_check``）、
``release_notes`` 的三种状态、按需检查接口，以及旧的源码端点被显式拒绝/行为不变。

**可信发布目录已经删除**：旧 ``release_catalog`` / ``available_targets`` /
``recommended_target`` / ``min_from`` 不再被 API 读取。有用的回归是"它们缺失或
变成占位时 API 不崩，也不会因此说成已是最新"——见 ``test_legacy_*`` 那几条。

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
import os
import re
import sys
import time
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
# 目标版本必须**严格大于**当前版本：只允许升级。
TARGET_VERSION = "1.1.0"
# registry 侧（``release_check``）写的是**裸摘要**；安装请求里要完整引用。
CURRENT_DIGEST_HEX = "sha256:" + "a" * 64
TARGET_DIGEST_HEX = "sha256:" + "b" * 64
OTHER_DIGEST_HEX = "sha256:" + "c" * 64
NEXT_DIGEST_HEX = "sha256:" + "d" * 64
ORIGIN = {"Origin": "http://testserver"}
# 检查时间固定用这一个：断言"人话时间"时不受运行时刻影响（UTC → 东八区 14:20）。
CHECKED_AT = "2026-11-09T06:20:00Z"
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


def _release_check(
    *,
    state: str = "ok",
    checked_at: str | None = CHECKED_AT,
    check_id: str | None = None,
    source: str | None = "registry",
    reason: str | None = None,
    reason_detail: str | None = None,
    current_version: str | None = CURRENT_VERSION,
    current_digest: str | None = CURRENT_DIGEST_HEX,
    latest: object = _MISSING,
    tag_count: int = 3,
) -> dict[str, object]:
    """更新器 ``status.json`` 顶层 ``release_check``（发现版本的**唯一**事实源）。

    形状照 docs/UPDATE-SIMPLIFICATION-DESIGN.md §3 的实现约定：``state`` 三态、
    ``current``/``latest`` 两个对象、``tag_count`` 计数。``latest`` 默认给一条
    "比当前版本高的稳定版 + 摘要"，也就是默认用例可更新。
    """
    if latest is _MISSING:
        latest = {
            "version": TARGET_VERSION,
            "digest": TARGET_DIGEST_HEX,
            "commit": None,
        }
    payload: dict[str, object] = {
        "state": state,
        "checked_at": checked_at,
        "check_id": check_id,
        "source": source,
        "reason": reason,
        "reason_detail": reason_detail,
        "current": {"version": current_version, "digest": current_digest},
        "latest": latest,
        "tag_count": tag_count,
    }
    return payload


def _notes_payload(
    *, state: str = "ok", **entries: object
) -> dict[str, object]:
    """``release_notes``：三态 + 逐版说明（键是版本号，允许带 ``v`` 前缀）。"""
    return {"state": state, "notes": {version: note for version, note in entries.items()}}


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
    release_check: object = _MISSING,
    release_notes: object = _MISSING,
    legacy: dict[str, object] | None = None,
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
    # 发现事实的默认值：一条"有更高版本 + 摘要已解析"的 release_check，于是
    # "其余门禁都过 → 可安装"的既有用例继续成立；专门用例显式覆盖它。
    # ``release_check=None`` 表示"更新器还没写这个键"（旧更新器）。
    payload["release_check"] = (
        _release_check() if release_check is _MISSING else release_check
    )
    if release_notes is not _MISSING:
        payload["release_notes"] = release_notes
    # 旧可信发布目录的键：目录已删除，API **不再读**它们。只在明确要验证
    # "旧键在/不在都不能改变结论"的用例里用 ``legacy=`` 注入。
    if legacy is not None:
        payload.update(legacy)
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
    assert '"percent"' not in json.dumps(view)
    # 只禁止"预计剩余时间"这类**字段**："details" 里本来就含 eta 三个字母。
    assert '"eta"' not in json.dumps(view)
    assert "剩余时间" not in view["message"]


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
    # 四态那句话讲人话（"需要一个可用备份"），码表原文在 reason_message；
    # 原因码在 reason 里。
    assert view["state"] == "newer_unavailable"
    assert "备份" in view["message"]
    assert "恢复点" in (view["reason_message"] or "")
    assert "_" not in view["message"]
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
    assert view["reason_message"] is None
    assert view["state"] == "available"
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
    assert "没有可用的恢复点" in (view["reason_message"] or "")
    assert "离线" not in (view["reason_message"] or "")
    assert view["state"] == "newer_unavailable"
    assert "备份" in view["message"]
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
    assert view["reason_message"] is None
    assert view["message"] == f"发现新版 v{TARGET_VERSION}，可以更新。"
    assert view["state"] == "available"
    assert view["next_actions"] == []
    assert view["updater"]["online"] is True
    assert view["updater"]["protocol"] == 1
    # 旧字段 superset：新增 version（更新器实测的目录版本），digest/commit 不变。
    assert view["current"] == {
        "version": CURRENT_VERSION,
        "digest": CURRENT_DIGEST,
        "commit": CURRENT_COMMIT,
    }
    assert view["backup_gate"]["restore_point"] is True
    assert view["backup_gate"]["backup_id"] == "backup-0123456789ab"
    assert view["task"]["state"] == "succeeded"
    assert view["task"]["state_message"] == "更新完成"
    assert view["task"]["previous"]["digest"] == OTHER_DIGEST
    assert view["task"]["migration"]["current_after"] == "0002_next"
    assert view["task"]["migration"]["target_head"] == "0002_next"
    assert view["task"]["still_on_old_version"] is False
    assert view["request"] is None


# ---------------------------------------- registry 发现：可用目标 / target
#
# 目录（releases.json / min_from）已经删除：目标只能来自更新器的 ``release_check``。
# 这里同时覆盖"旧键缺失/占位时 API 不崩、也不说成已是最新"。


async def test_get_installable_exposes_whitelisted_target(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """有摘要可用的新版 → 可安装，``target`` 只给 version/digest/commit/has_migration。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is True
    assert view["reason"] is None
    # 目标提交拿不到（registry tag 元数据里没有）→ commit=null；has_migration
    # 随目录一起退场 → null（不是 false：那会假装"这一版一定不跑迁移"）。
    assert view["target"] == {
        "version": TARGET_VERSION,
        "digest": TARGET_DIGEST,
        "commit": None,
        "has_migration": None,
    }
    assert set(view["target"]) == {"version", "digest", "commit", "has_migration"}
    assert view["available_targets"] == [
        {
            "version": TARGET_VERSION,
            "digest": TARGET_DIGEST,
            "commit": None,
            "has_migration": None,
            "compatible": True,
            "incompatible_reason": None,
            "incompatible_reason_message": None,
        }
    ]
    # 旧 ``catalog`` 形状保留一个版本，但内容全部派生自 release_check。
    catalog = view["catalog"]
    assert catalog["state"] == "ok"
    assert catalog["reason"] is None
    assert catalog["source"] == "registry"
    assert catalog["entries"] == 3
    assert catalog["current_version"] == CURRENT_VERSION
    assert catalog["checked_at"] == CHECKED_AT


async def test_no_newer_version_is_not_installable(logged_in: TestEnv, tmp_path: Path) -> None:
    """没有更高版本 → 不可安装，原因是固定码表文案（不是让调用方自己编摘要）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            latest={"version": CURRENT_VERSION, "digest": CURRENT_DIGEST_HEX, "commit": None}
        ),
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"
    assert view["reason_message"] == version_routes.IMAGE_REASON_MESSAGES["no_available_target"]
    # 检查完成且没有更高版本 → 四态给「已是最新」，
    # 但安装门禁仍说 no_available_target（"能装"与"有没有新版"是两件事）。
    assert view["state"] == "latest"
    assert view["newer"] == []
    assert view["next_actions"] == list(
        version_routes.IMAGE_REASON_NEXT_ACTIONS["no_available_target"]
    )
    assert view["target"] is None
    assert view["available_targets"] == []


async def test_undigested_newer_version_is_shown_but_not_installable(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """有更高版本但摘要为空 → 展示那一版，但不可安装（target=null）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            state="failed",
            reason="digest_unresolved",
            latest={"version": TARGET_VERSION, "digest": None, "commit": None},
        ),
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"
    assert view["state"] == "newer_unavailable"
    assert view["target"] is None
    entry = view["newer"][0]
    assert entry["version"] == TARGET_VERSION
    assert entry["digest"] is None
    assert entry["compatible"] is False
    assert entry["incompatible_reason"] == "digest_unresolved"
    assert entry["incompatible_reason_message"] == (
        version_routes.IMAGE_INCOMPATIBLE_MESSAGES["digest_unresolved"]
    )


async def test_api_rechecks_that_target_is_newer(logged_in: TestEnv, tmp_path: Path) -> None:
    """更新器给的 latest **不高于**当前版本 → API 自己判"没有新版"（只允许升级）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            latest={"version": CURRENT_VERSION, "digest": NEXT_DIGEST_HEX, "commit": None}
        ),
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"
    assert view["target"] is None
    assert view["newer"] == []
    assert view["state"] == "latest"
    # POST 也走同一套结论：没有可提交目标 → 409，且不写请求文件。
    response = await _post(logged_in, _body(target_digest=TARGET_DIGEST))
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "no_available_target"
    assert _request_files(base) == []


@pytest.mark.parametrize(
    "latest,expected",
    [
        # 版本号不是 semver（``latest`` 这类 tag）→ 不能比较，整条丢弃。
        ({"version": "latest", "digest": TARGET_DIGEST_HEX, "commit": None}, "latest"),
        ({"version": None, "digest": TARGET_DIGEST_HEX, "commit": None}, "latest"),
        # 摘要格式不对 → 当作拿不到摘要（有新版但装不了），不是可提交目标。
        (
            {"version": TARGET_VERSION, "digest": "ghcr.io/qsbb/ling-api:latest",
             "commit": None},
            "newer_unavailable",
        ),
        (
            {"version": TARGET_VERSION, "digest": f"sha256:{'b' * 63}", "commit": None},
            "newer_unavailable",
        ),
        ("not-an-object", "latest"),
    ],
)
async def test_malformed_latest_is_dropped(
    logged_in: TestEnv, tmp_path: Path, latest: object, expected: str
) -> None:
    """``latest`` 结构/版本号/摘要不认识 → 不猜、不错位展示，且绝不说成已是最新。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, release_check=_release_check(latest=latest))
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["target"] is None
    assert view["installable"] is False
    assert view["state"] == expected
    if expected == "latest":
        # 没有可比较的更高版本时，不能说"发现新版"。
        assert view["newer"] == []
    else:
        assert view["newer"][0]["incompatible_reason"] == "digest_unresolved"
        assert view["newer"][0]["digest"] is None


async def test_release_check_extra_fields_are_not_echoed(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """``release_check`` 里的多余键（路径/日志/异常文本）一律进不了响应。"""
    base = _configure(logged_in, tmp_path)
    check = _release_check(
        reason_detail="Get \"https://ghcr.io/token?scope=repository:qsbb/ling-api:pull\": "
        "dial tcp 140.82.112.33:443: i/o timeout"
    )
    check["path"] = "/vol1/1000/ling/deploy/releases.json"
    check["journal"] = "journal.log"
    check["message"] = "ssh root@host"
    _write_status(base, release_check=check)
    response = await logged_in.client.get("/v1/version/image-update")
    text = response.text
    for secret in ("/vol1/1000", "journal.log", "ssh root@host", "dial tcp", "ghcr.io/token"):
        assert secret not in text, secret
    # reason_detail 只放固定枚举 ``null | "throttled"``：别的值（自由文本，可能带
    # URL/凭据/异常）一律丢弃，字段本身在（前端按需读，丢的是值）。
    assert response.json()["details"]["release_check"]["reason_detail"] is None


async def test_release_check_reason_detail_throttled_is_kept(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """更新器唯一的非空 ``reason_detail``（``throttled``）照原样投影。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(state="unknown", reason=None, reason_detail="throttled"),
    )
    view = await _view(logged_in)
    assert view["details"]["release_check"]["reason_detail"] == "throttled"
    # 限流丢弃只是"这次没查"，四态仍不能因此说「已是最新」。
    assert view["state"] == "unknown"


async def test_status_with_only_release_check_is_sufficient(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """更新器只写 ``release_check``（没有 release_catalog/available_targets）→ 照常可用。

    这是**新更新器**的实际形状：目录相关的键全部消失。API 不许因为读不到它们而
    崩，也不许因此改口说「已是最新」。
    """
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    raw = json.loads(
        (base / "status" / version_routes.IMAGE_UPDATER_STATUS_FILE).read_text(encoding="utf-8")
    )
    for removed in ("release_catalog", "available_targets", "recommended_target", "catalog_check"):
        assert removed not in raw, removed
    view = await _view(logged_in)
    assert view["state"] == "available"
    assert view["target"]["version"] == TARGET_VERSION
    assert view["details"]["release_check"]["state"] == "ok"
    # 旧字段还在响应里（前端一个版本内仍读得到），只是不再来自目录。
    assert view["catalog"]["state"] == "ok"
    assert view["available_targets"][0]["version"] == TARGET_VERSION


async def test_release_check_missing_is_unknown_not_latest(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """没有 ``release_check``（旧更新器）→ 「暂不能确认」，不是「已是最新」。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, release_check=None)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["state"] == "unknown"
    assert view["installable"] is False
    assert view["newer"] == []
    assert view["target"] is None
    assert view["details"]["release_check"] is None
    assert view["checked_at"] is None


async def test_unknown_release_check_state_and_reason_are_not_echoed(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """未知的 state/reason 不转发原文，只给固定的"没有可信结论"形态。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            state="weird", reason="/vol1/1000/ling/deploy/releases.json", tag_count=10**9
        ),
    )
    response = await logged_in.client.get("/v1/version/image-update")
    assert "/vol1/1000" not in response.text
    view = response.json()
    assert view["state"] == "unknown"
    check = view["details"]["release_check"]
    assert check["state"] is None
    assert check["reason"] is None
    # 计数有硬上限：状态文件跨信任边界，不为它提供渲染放大器。
    assert check["tag_count"] == version_routes.IMAGE_MAX_TAG_COUNT


async def test_post_rejects_target_that_is_not_the_discovered_one(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """这次发现的是 1.1.0；提交另一个格式合格的摘要 → 409，且不写请求文件。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await _post(logged_in, _body(target_digest=OTHER_DIGEST))
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "target_not_available"
    assert response.json()["error"]["message"] == (
        version_routes.IMAGE_REASON_MESSAGES["target_not_available"]
    )
    assert _request_files(base) == []


async def test_post_accepts_the_discovered_target(logged_in: TestEnv, tmp_path: Path) -> None:
    """发现的目标可以提交（201）——目标不是只读展示，安装门禁也是它。"""
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


# ---------------------------------------------------------------- 阶段口径（stage / failure.stage）


@pytest.mark.parametrize("stage", sorted(version_routes.IMAGE_STAGE_MESSAGES))
async def test_failure_stage_keeps_real_stage(
    logged_in: TestEnv, tmp_path: Path, stage: str
) -> None:
    """``failure.stage`` 是五个真实阶段之一 → 原样保留（前后端口径一致）。"""
    base = _configure(logged_in, tmp_path)
    task = _task(state="needs_manual", stage=stage)
    task["failure"] = {"code": "switch_failed", "stage": stage}
    _write_status(base, state="needs_manual", task=task)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["task"]["stage"] == stage
    assert view["task"]["failure"]["stage"] == stage


@pytest.mark.parametrize(
    "stage",
    [
        # 更新器自己的非阶段值：对账、启动、状态自查。
        "reconcile",
        "bootstrap",
        "status",
        # 复用 publish.py 时 ``failure.step`` 原样落进 ``failure.stage``。
        "preflight",
        "pull",
        "up_api",
        "env_update",
        # 明显垃圾值。
        "garbage-stage",
        "下载",
        "",
        # 超长：旧实现 `_image_text(..., max_length=32)` 只查长度/字符集，超长同样
        # 会被拒（这一条行为不变，留作锚点）。
        "x" * 40,
    ],
)
async def test_failure_stage_unknown_is_projected_to_none(
    logged_in: TestEnv, tmp_path: Path, stage: str
) -> None:
    """未知 ``failure.stage`` → None，且**不透传**到前端。

    回归的是截图里「停止阶段：处理中（阶段不可识别）」：任务已经停了，前端却
    因为没有匹配的标签把它渲染成"还在跑"。未知值必须投影成 None（前端显示
    「尚未开始」），并且原文不出现在响应里。
    """
    base = _configure(logged_in, tmp_path)
    task = _task(state="needs_manual", stage="verify")
    task["failure"] = {"code": "switch_failed", "stage": stage}
    _write_status(base, state="needs_manual", task=task)
    response = await logged_in.client.get("/v1/version/image-update")
    body = response.json()
    assert body["task"]["failure"]["stage"] is None
    # 失败码与文案照旧（只收敛阶段，不放宽别的门禁）。
    assert body["task"]["failure"]["code"] == "switch_failed"
    assert body["task"]["failure"]["message"] == version_routes.IMAGE_FAILURE_MESSAGES["switch_failed"]
    if stage and version_routes.IMAGE_TOKEN_RE.fullmatch(stage):
        # 只要过得了字符集校验的值曾经会被原样转发；现在一个都不能出现。
        assert stage not in response.text


@pytest.mark.parametrize("stage", ["reconcile", "preflight", "garbage-stage", ""])
async def test_top_level_stage_unknown_is_projected_to_none(
    logged_in: TestEnv, tmp_path: Path, stage: str
) -> None:
    """顶层 ``stage`` 的同类行为（与 ``failure.stage`` 同一口径）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, task=_task(state="running", stage=stage))
    response = await logged_in.client.get("/v1/version/image-update")
    body = response.json()
    assert body["task"]["stage"] is None
    assert body["task"]["stage_message"] is None
    if stage and version_routes.IMAGE_TOKEN_RE.fullmatch(stage):
        assert stage not in response.text


async def test_stage_and_failure_stage_share_one_whitelist() -> None:
    """两处必须走同一个 helper：``_image_stage`` 的键集合就是 ``IMAGE_STAGE_MESSAGES``。

    守的是"以后再次分叉"：只要有人在其中一处改回 ``_image_text``，这条用例就红。
    """
    assert version_routes._image_stage("verify") == "verify"
    assert version_routes._image_stage("reconcile") is None
    assert version_routes._image_stage(None) is None
    assert version_routes._image_stage(123) is None
    # 合法阶段恰好是码表的键，不多不少（更新器 ``STAGES`` 与本表一致）。
    assert {stage for stage in version_routes.IMAGE_STAGE_MESSAGES} == {
        "prepare",
        "backup",
        "migrate",
        "switch",
        "verify",
    }


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


# ================================================================ 四态视图
#
# 设计依据：docs/UPDATE-UX-DESIGN.md §卡片与四态 / §判定数据与文案映射。
# 这些用例只碰 ``status.json`` 与请求目录，不联网、不碰 Docker。


def _refresh_files(base: Path) -> list[str]:
    directory = base / "requests"
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if p.name.startswith("refresh-"))


async def _view(env: TestEnv) -> dict[str, object]:
    response = await env.client.get("/v1/version/image-update")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


async def _check(
    env: TestEnv,
    request_id: str | None = None,
    *,
    headers: dict[str, str] | None = None,
    **kwargs: object,
):
    """按需检查请求：**只带 Content-Type**（前端不发 X-Ling-Update）。"""
    return await env.client.post(
        "/v1/version/image-update-checks",
        json={"request_id": request_id or uuid.uuid4().hex},
        headers={**ORIGIN, **(headers or {})},
        **kwargs,
    )


async def test_state_latest_when_release_check_has_no_newer_version(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """检查完成、没有更高版本 → 「已是最新」（**唯一**能这么说的情形）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            latest={"version": CURRENT_VERSION, "digest": CURRENT_DIGEST_HEX, "commit": None},
            tag_count=5,
        ),
    )
    view = await _view(logged_in)
    assert view["state"] == "latest"
    # 时间必须是人话（东八区的 14:20），不是原始 ISO。
    assert view["message"] == "当前 v1.0.0 已是最新（检查于 11 月 9 日 14:20）。"
    assert view["newer"] == []
    assert view["target"] is None
    assert view["checked_at"] == CHECKED_AT
    # 「没有新版」≠「能装」：安装侧仍如实说没有可用目标。
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"


async def test_state_available_needs_digest_and_gates(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    view = await _view(logged_in)
    assert view["state"] == "available"
    assert view["message"] == f"发现新版 v{TARGET_VERSION}，可以更新。"
    assert view["target"] == {
        "version": TARGET_VERSION,
        "digest": TARGET_DIGEST,
        "commit": None,
        "has_migration": None,
    }
    assert [entry["version"] for entry in view["newer"]] == [TARGET_VERSION]


# 列表类失败（tags 拿不到/没查全）：连"有没有新版"都不能断言 → unknown。
# 每条原因对应设计 §6 表格里的一句话；码不进 message。
_LISTING_FAILURE_MESSAGES = {
    "registry_unreachable": "连不上镜像仓库",
    "registry_auth_failed": "镜像仓库不认这次查询的凭据（可能是私有包或凭据失效）",
    "registry_rate_limited": "镜像仓库暂时限流，这次没查完整",
    "registry_timeout": "查询镜像仓库超时",
    "no_stable_tags": "镜像仓库里还没有稳定版发布",
}


@pytest.mark.parametrize("reason", sorted(_LISTING_FAILURE_MESSAGES))
async def test_state_unknown_for_each_listing_failure(
    logged_in: TestEnv, tmp_path: Path, reason: str
) -> None:
    """不可达/认证/限流/超时/没有稳定版 → 「暂不能确认」，原因说人话。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(state="failed", reason=reason, latest=None, tag_count=0),
    )
    view = await _view(logged_in)
    assert view["state"] == "unknown"
    assert view["target"] is None
    assert view["installable"] is False
    # 列表本身不可信：连候选都不投影（不许拿旧结果冒充本次发现）。
    assert view["newer"] == []
    assert view["message"] == (
        f"暂时无法确认是否有新版本：{_LISTING_FAILURE_MESSAGES[reason]}。"
    )
    assert "_" not in view["message"]
    assert view["details"]["release_check"]["reason"] == reason


@pytest.mark.parametrize(
    "reason,clause",
    [
        ("digest_unresolved", "新版已发布，但暂时取不到安装包"),
        ("platform_unsupported", "新版本没有适合这台机器的镜像"),
    ],
)
async def test_state_newer_unavailable_for_each_artifact_failure(
    logged_in: TestEnv, tmp_path: Path, reason: str, clause: str
) -> None:
    """有更高版本、只是拿不到安装包 → 「有新版暂不可用」，条目留在 newer 里。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            state="failed",
            reason=reason,
            latest={"version": TARGET_VERSION, "digest": None, "commit": None},
        ),
    )
    view = await _view(logged_in)
    assert view["state"] == "newer_unavailable"
    assert view["newer"] == [
        {
            "version": TARGET_VERSION,
            "commit": None,
            "has_migration": None,
            "digest": None,
            "compatible": False,
            "incompatible_reason": reason,
            "incompatible_reason_message": version_routes.IMAGE_INCOMPATIBLE_MESSAGES[reason],
            "summary": None,
            "notes": [],
            "notes_url": None,
        }
    ]
    assert view["target"] is None
    assert view["message"] == f"发现新版 v{TARGET_VERSION}，但暂时不能更新：{clause}。"


# 更新器实际写的是**另一套拼写**（deploy/updater/registry.py：``auth_failed`` /
# ``rate_limited`` / ``timeout`` / ``manifest_failed`` / ``digest_missing`` /
# ``platform_missing``）。API 侧按别名表收敛成自己的码，**不改更新器**；
# 归一在判定之前，所以四态、条目原因和 ``details.release_check.reason`` 回显
# 用的都是归一码（前端码表就按归一码查）。
_LISTING_REASON_ALIASES = {
    "auth_failed": "registry_auth_failed",
    "rate_limited": "registry_rate_limited",
    "timeout": "registry_timeout",
}
_ARTIFACT_REASON_ALIASES = {
    "platform_missing": "platform_unsupported",
    "digest_missing": "digest_unresolved",
    "manifest_failed": "digest_unresolved",
}


@pytest.mark.parametrize("written,normalized", sorted(_LISTING_REASON_ALIASES.items()))
async def test_updater_listing_reason_alias_is_normalized(
    logged_in: TestEnv, tmp_path: Path, written: str, normalized: str
) -> None:
    """更新器的列表类拼写 → 认得的列表类码：仍是「暂不能确认」，文案是归一码那句。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(state="failed", reason=written, latest=None, tag_count=0),
    )
    view = await _view(logged_in)
    clause = version_routes.IMAGE_RELEASE_REASON_MESSAGES[normalized]
    assert view["state"] == "unknown"
    assert view["newer"] == []
    assert view["target"] is None
    assert view["installable"] is False
    assert view["message"] == f"暂时无法确认是否有新版本：{clause}。"
    # 首屏那句是人话：既没有更新器的原始拼写，也没有归一码。
    assert written not in view["message"]
    assert normalized not in view["message"]
    assert "_" not in view["message"]
    # 回显的是**归一码**（不是更新器写的那个拼写）。
    check = view["details"]["release_check"]
    assert check["reason"] == normalized
    assert check["reason_message"] == clause


@pytest.mark.parametrize("written,normalized", sorted(_ARTIFACT_REASON_ALIASES.items()))
async def test_updater_artifact_reason_alias_is_normalized(
    logged_in: TestEnv, tmp_path: Path, written: str, normalized: str
) -> None:
    """更新器的制品类拼写 → 认得的制品类码：有新版、装不了，原因不许错归。

    ``platform_missing`` 是最容易归错的一条：不归一就会被当成"列表类失败"，
    错说成「取不到安装包」，而事实是「没有适合这台机器的镜像」。
    """
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            state="failed",
            reason=written,
            latest={"version": TARGET_VERSION, "digest": None, "commit": None},
        ),
    )
    view = await _view(logged_in)
    assert view["state"] == "newer_unavailable"
    assert view["target"] is None
    assert view["installable"] is False
    entry = view["newer"][0]
    assert entry["compatible"] is False
    assert entry["incompatible_reason"] == normalized
    assert entry["incompatible_reason_message"] == (
        version_routes.IMAGE_INCOMPATIBLE_MESSAGES[normalized]
    )
    assert view["message"] == (
        f"发现新版 v{TARGET_VERSION}，但暂时不能更新："
        f"{version_routes.IMAGE_UNAVAILABLE_REASONS[normalized]}。"
    )
    assert written not in view["message"]
    assert "_" not in view["message"]
    check = view["details"]["release_check"]
    assert check["reason"] == normalized
    assert check["reason_message"] == version_routes.IMAGE_RELEASE_REASON_MESSAGES[normalized]


async def test_truly_unknown_release_check_reason_stays_degraded(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """别名表**不是**兜底：真正未知的码仍然降级成「暂不能确认」，且不回显原文。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            state="failed", reason="whatever_new_code", latest=None, tag_count=0
        ),
    )
    view = await _view(logged_in)
    assert view["state"] == "unknown"
    assert view["newer"] == []
    assert view["target"] is None
    assert view["installable"] is False
    # 码被丢掉：详情里没有原因码，首屏也没有原文（更不许说成「已是最新」）。
    check = view["details"]["release_check"]
    assert check["reason"] is None
    assert check["reason_message"] is None
    assert "whatever_new_code" not in view["message"]
    assert "whatever_new_code" not in json.dumps(view, ensure_ascii=False)
    assert "_" not in view["message"]
    assert "已是最新" not in view["message"]


@pytest.mark.parametrize(
    "latest",
    [
        {"version": TARGET_VERSION, "digest": None, "commit": None},
        {"version": TARGET_VERSION, "commit": None},
        {"version": TARGET_VERSION, "digest": 42, "commit": None},
    ],
)
async def test_state_newer_unavailable_when_digest_is_unresolved(
    logged_in: TestEnv, tmp_path: Path, latest: dict[str, object]
) -> None:
    """即使更新器没说原因，摘要为空也只可能是「拿不到安装包」。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, release_check=_release_check(latest=latest))
    view = await _view(logged_in)
    assert view["state"] == "newer_unavailable"
    assert view["newer"][0]["incompatible_reason"] == "digest_unresolved"
    assert view["target"] is None
    assert view["message"] == (
        f"发现新版 v{TARGET_VERSION}，但暂时不能更新："
        f"{version_routes.IMAGE_UNAVAILABLE_REASONS['digest_unresolved']}。"
    )


async def test_state_unknown_when_current_version_is_missing(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, release_check=_release_check(current_version=None))
    view = await _view(logged_in)
    assert view["state"] == "unknown"
    assert "无法确认现在运行的版本" in view["message"]
    # 当前版本未知时没有比较基准 → 任何「更新」都不能断言。
    assert view["newer"] == []
    assert view["current"]["version"] is None
    assert view["details"]["release_check"]["current"]["version"] is None


async def test_state_unknown_when_updater_offline(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, updated_at=_STALE)
    view = await _view(logged_in)
    assert view["updater"]["online"] is False
    assert view["state"] == "unknown"
    assert view["message"] == "暂时无法确认是否有新版本：更新服务暂时没有响应。"
    # 离线时连候选都不敢投影（旧结果不是本次发现）。
    assert view["newer"] == []
    assert view["installable"] is False


async def test_state_unknown_when_updater_status_missing(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    _configure(logged_in, tmp_path)
    view = await _view(logged_in)
    assert view["state"] == "unknown"
    assert view["message"] == "暂时无法确认是否有新版本：更新器还没有写出状态文件。"
    assert view["checked_at"] is None


async def test_legacy_catalog_fields_do_not_create_a_target(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """旧更新器只写 ``release_catalog``/``available_targets`` → 不崩、也不装。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=None,
        legacy={
            "release_catalog": {
                "state": "ok",
                "reason": None,
                "entries": 1,
                "current_version": CURRENT_VERSION,
                "source": "remote",
            },
            "available_targets": [
                {
                    "version": TARGET_VERSION,
                    "digest": TARGET_DIGEST,
                    "commit": TARGET_COMMIT,
                    "has_migration": True,
                    "compatible": True,
                    "incompatible_reason": None,
                }
            ],
            "recommended_target": {
                "version": TARGET_VERSION,
                "digest": TARGET_DIGEST,
                "commit": TARGET_COMMIT,
                "has_migration": True,
            },
        },
    )
    view = await _view(logged_in)
    assert view["state"] == "unknown"
    assert view["newer"] == []
    assert view["available_targets"] == []
    assert view["target"] is None
    assert view["installable"] is False
    response = await _post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "no_available_target"


async def test_legacy_catalog_fields_are_ignored_when_release_check_exists(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """新更新器同时留了旧键（占位/过期）→ 结论只认 ``release_check``。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        legacy={
            "release_catalog": {"state": "missing", "reason": "catalog_missing", "entries": 0},
            "available_targets": [],
            "recommended_target": None,
        },
    )
    view = await _view(logged_in)
    assert view["state"] == "available"
    assert view["target"]["version"] == TARGET_VERSION


async def test_state_unknown_when_measured_version_conflicts(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """更新器实测版本与 API 自报版本冲突 → 「暂不能确认」（不猜谁对）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, release_check=_release_check(current_version="1.0.1"))
    view = await _view(logged_in)
    assert view["state"] == "unknown"
    assert view["message"] == "暂时无法确认是否有新版本：更新器报的当前版本与本机记录不一致。"
    assert view["newer"] == []
    assert view["installable"] is False


async def test_state_is_independent_of_role(logged_in: TestEnv, tmp_path: Path) -> None:
    """非 owner 用同一套四态结论，只是请求/安装写入口不可用。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    assert (await _view(logged_in))["state"] == "available"
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="member-state", password="member-password", role="member"
        )
    assert (
        await logged_in.client.post(
            "/v1/auth/login",
            json={"username": "member-state", "password": "member-password"},
        )
    ).status_code == 200
    view = await _view(logged_in)
    assert view["state"] == "available"
    assert view["target"]["version"] == TARGET_VERSION
    assert view["installable"] is False
    assert view["reason"] == "not_owner"


async def test_active_task_keeps_discovered_state_and_overrides_message(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """活动任务不是第五种状态：state 仍按发现结果，首屏那句话换成进度。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, state="busy", task=_task(state="running", stage="migrate"))
    view = await _view(logged_in)
    assert view["state"] == "available"
    assert "进行中" in view["message"]
    assert "正在执行数据库迁移" in view["message"]
    assert view["reason"] == "update_in_progress"
    assert view["task"]["state"] == "running"
    assert view["task"]["stage"] == "migrate"


async def test_queued_request_keeps_state_and_says_waiting(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    _write_pending_request(base)
    view = await _view(logged_in)
    assert view["state"] == "available"
    assert "正在" in view["message"] and "等待更新器" in view["message"]
    assert view["request"] is not None


@pytest.mark.parametrize(
    "mutate",
    [
        "backup_gate_missing",
        "update_disabled",
        "current_image_unknown",
        "manual_required",
    ],
)
async def test_gate_blocks_availability_but_state_still_reports_newer(
    logged_in: TestEnv, tmp_path: Path, mutate: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """安装门禁不过 → 「有新版暂不可用」，且原因就是那道门（不是删掉新版）。"""
    base = _configure(logged_in, tmp_path, enabled=mutate != "update_disabled")
    kwargs: dict[str, object] = {}
    if mutate == "backup_gate_missing":
        kwargs["task"] = None
    elif mutate == "current_image_unknown":
        monkeypatch.delenv("LING_APP_IMAGE")
    elif mutate == "manual_required":
        kwargs["state"] = "needs_manual"
        kwargs["task"] = _task(state="needs_manual", stage="verify")
    _write_status(base, **kwargs)  # type: ignore[arg-type]
    view = await _view(logged_in)
    assert view["installable"] is False
    assert view["state"] == "newer_unavailable"
    assert view["target"] is not None  # 目标本身是有的，只是装不了
    assert view["reason"] == mutate
    assert view["message"] == (
        f"发现新版 v{TARGET_VERSION}，但暂时不能更新："
        f"{version_routes.IMAGE_UNAVAILABLE_REASONS[mutate]}。"
    )


async def test_installable_matches_available_state_for_owner(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """owner 视角：「可安装」与「可更新」必须是同一个结论（不会自相矛盾）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    view = await _view(logged_in)
    assert view["installable"] is True
    assert view["state"] == "available"
    # 反过来：没有更高版本时两边的结论一致（不可装、也没有"可更新"）。
    _write_status(
        base,
        release_check=_release_check(
            latest={"version": CURRENT_VERSION, "digest": CURRENT_DIGEST_HEX, "commit": None}
        ),
    )
    view = await _view(logged_in)
    assert view["installable"] is False
    assert view["state"] == "latest"


# ---------------------------------------------------------------- newer / target


async def test_target_never_comes_from_a_digest_less_entry(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            latest={"version": TARGET_VERSION, "digest": None, "commit": None}
        ),
    )
    view = await _view(logged_in)
    # 「有版本但没摘要」被归一成「拿不到安装包」：既不是目标，也不是可安装。
    assert view["target"] is None
    assert view["newer"][0]["digest"] is None
    assert view["newer"][0]["compatible"] is False
    assert view["newer"][0]["incompatible_reason"] == "digest_unresolved"
    assert view["state"] == "newer_unavailable"
    assert view["installable"] is False


async def test_newer_excludes_versions_not_above_current(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """不高于当前版本的不算「新版」；latest 就是当前版本 → 已是最新。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            latest={"version": CURRENT_VERSION, "digest": TARGET_DIGEST_HEX, "commit": None}
        ),
    )
    view = await _view(logged_in)
    assert view["newer"] == []
    assert view["target"] is None
    assert view["state"] == "latest"


async def test_newer_reports_missing_commit_as_null(logged_in: TestEnv, tmp_path: Path) -> None:
    """目标版本的构建提交拿不到就如实给 null（不是编一个，也不是整条丢掉）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    entry = (await _view(logged_in))["newer"][0]
    assert entry["commit"] is None
    assert entry["version"] == TARGET_VERSION
    assert entry["digest"] == TARGET_DIGEST


async def test_newer_accepts_both_digest_forms(logged_in: TestEnv, tmp_path: Path) -> None:
    """裸摘要（registry）与完整引用（旧写法）都能归一成安装请求要的形状。"""
    base = _configure(logged_in, tmp_path)
    for given in (TARGET_DIGEST_HEX, TARGET_DIGEST):
        _write_status(
            base,
            release_check=_release_check(
                latest={"version": TARGET_VERSION, "digest": given, "commit": None}
            ),
        )
        view = await _view(logged_in)
        assert view["target"]["digest"] == TARGET_DIGEST
        assert view["state"] == "available"


async def test_newer_is_whitelisted(logged_in: TestEnv, tmp_path: Path) -> None:
    """条目里的多余键（路径/日志/自由文本）进不了响应。"""
    base = _configure(logged_in, tmp_path)
    check = _release_check()
    latest = dict(check["latest"])  # type: ignore[arg-type]
    latest["path"] = "/vol1/1000/ling/deploy/releases.json"
    latest["journal"] = "journal.log"
    latest["message"] = "ssh root@host"
    check["latest"] = latest
    _write_status(base, release_check=check)
    response = await logged_in.client.get("/v1/version/image-update")
    text = response.text
    for secret in ("/vol1/1000", "journal.log", "ssh root@host"):
        assert secret not in text, secret


# ---------------------------------------------------------------- 首屏文案


_PLAIN_STATES: list[tuple[str, dict[str, object]]] = [
    (
        "latest",
        {
            "release_check": _release_check(
                latest={
                    "version": CURRENT_VERSION,
                    "digest": CURRENT_DIGEST_HEX,
                    "commit": None,
                }
            )
        },
    ),
    ("available", {}),
    (
        "newer_unavailable",
        {
            "release_check": _release_check(
                state="failed",
                reason="platform_unsupported",
                latest={"version": TARGET_VERSION, "digest": None, "commit": None},
            )
        },
    ),
    (
        "unknown",
        {"release_check": _release_check(state="failed", reason="registry_unreachable",
                                          latest=None, tag_count=0)},
    ),
]


@pytest.mark.parametrize("expected,status_kwargs", _PLAIN_STATES)
async def test_first_screen_message_is_plain_language(
    logged_in: TestEnv,
    tmp_path: Path,
    expected: str,
    status_kwargs: dict[str, object],
) -> None:
    """首屏一句话必须是**人话**：没有码表码、没有摘要、没有路径、没有 ISO 时间。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, **status_kwargs)  # type: ignore[arg-type]
    view = await _view(logged_in)
    assert view["state"] == expected
    message = view["message"]
    assert isinstance(message, str) and message
    assert message.endswith("。")
    assert "_" not in message, message
    assert "sha256" not in message
    assert "ghcr.io" not in message
    assert "/" not in message
    # 原始 ISO（``2026-11-09T06:20:00Z``）不许出现在首屏：时间要人话。
    assert "T06:20:00Z" not in message
    for code in (
        *version_routes.IMAGE_UNAVAILABLE_REASONS,
        *version_routes.IMAGE_UNKNOWN_REASONS,
        *version_routes.IMAGE_RELEASE_REASON_MESSAGES,
        *version_routes.IMAGE_REASON_MESSAGES,
    ):
        assert code not in message, (code, message)


async def test_latest_message_has_no_check_time_when_unknown(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """没有检查时间就不要编一个：只有"已是最新"，不带"（检查于 …）"。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            checked_at=None,
            latest={"version": CURRENT_VERSION, "digest": CURRENT_DIGEST_HEX, "commit": None},
        ),
    )
    view = await _view(logged_in)
    assert view["checked_at"] is None
    assert view["message"] == "当前 v1.0.0 已是最新。"


async def test_checked_at_and_check_id_come_from_release_check(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    check_id = "0123456789abcdef0123456789abcdef"
    _write_status(
        base,
        release_check=_release_check(
            checked_at="2026-11-09T06:20:00Z",
            check_id=check_id,
            latest={"version": CURRENT_VERSION, "digest": CURRENT_DIGEST_HEX, "commit": None},
        ),
    )
    view = await _view(logged_in)
    assert view["checked_at"] == "2026-11-09T06:20:00Z"
    # 前端按 ``details.catalog.check.check_id`` 核对"这次检查真的完成了"。
    check = view["details"]["catalog"]["check"]
    assert check["check_id"] == check_id
    assert check["state"] == "ok"
    assert check["finished_at"] == "2026-11-09T06:20:00Z"
    raw = view["details"]["release_check"]
    assert raw["check_id"] == check_id
    assert raw["state"] == "ok"
    assert raw["source"] == "registry"
    assert raw["tag_count"] == 3
    assert raw["current"] == {"version": CURRENT_VERSION, "digest": CURRENT_DIGEST}
    assert raw["latest"] == {
        "version": CURRENT_VERSION,
        "digest": CURRENT_DIGEST,
        "commit": None,
    }


async def test_check_time_uses_the_users_timezone(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """``checked_at`` 是 UTC，首屏按用户时区显示（默认 Asia/Shanghai）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_check=_release_check(
            checked_at="2026-11-09T06:20:00Z",
            latest={"version": CURRENT_VERSION, "digest": CURRENT_DIGEST_HEX, "commit": None},
        ),
    )
    view = await _view(logged_in)
    assert "11 月 9 日 14:20" in view["message"]


# ---------------------------------------------------------------- 更新内容（notes）


async def test_notes_absent_means_no_content(logged_in: TestEnv, tmp_path: Path) -> None:
    """更新器没有 ``release_notes``（或 state=missing）→ 空数组，绝不从 CHANGELOG 里编。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    entry = (await _view(logged_in))["newer"][0]
    assert entry["notes"] == []
    assert entry["summary"] is None
    assert entry["notes_url"] is None


async def test_notes_present_are_projected_verbatim(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_notes=_notes_payload(
            **{
                "v1.1.0": {
                    "summary": "日程子系统与发送链路的可靠性修复",
                    "items": ["她的一天：前夜生成次日安排", "修复重试时丢消息"],
                    "url": "https://github.com/qsbb/ling/blob/main/CHANGELOG.md",
                }
            }
        ),
    )
    entry = (await _view(logged_in))["newer"][0]
    assert entry["summary"] == "日程子系统与发送链路的可靠性修复"
    assert entry["notes"] == ["她的一天：前夜生成次日安排", "修复重试时丢消息"]
    assert entry["notes_url"] == "https://github.com/qsbb/ling/blob/main/CHANGELOG.md"
    # 摘要**不**混进 items：前端契约里它们是两个字段。
    assert entry["summary"] not in entry["notes"]


@pytest.mark.parametrize("state", ["missing", "invalid"])
async def test_notes_missing_or_invalid_state_means_no_content(
    logged_in: TestEnv, tmp_path: Path, state: str
) -> None:
    """``release_notes.state`` 说没有/坏了 → 没有内容，但**不影响**能不能更新。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, release_notes={"state": state, "notes": {}})
    view = await _view(logged_in)
    assert view["state"] == "available"
    entry = view["newer"][0]
    assert entry["notes"] == []
    assert entry["summary"] is None
    assert entry["notes_url"] is None


async def test_notes_for_another_version_are_not_borrowed(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """说明只按**这一版**取：别的版本有内容也不借过来（不冒充）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_notes=_notes_payload(
            **{"1.2.0": {"summary": "更晚版本的说明", "items": ["不属于 1.1.0"]}}
        ),
    )
    entry = (await _view(logged_in))["newer"][0]
    assert entry["version"] == TARGET_VERSION
    assert entry["summary"] is None
    assert entry["notes"] == []


async def test_notes_inner_table_shape_is_accepted(logged_in: TestEnv, tmp_path: Path) -> None:
    """也接受直接给内层表（``{"1.1.0": {...}}``），不要求外层 ``notes`` 键。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_notes={"1.1.0": {"items": ["一条说明"]}},
    )
    entry = (await _view(logged_in))["newer"][0]
    assert entry["notes"] == ["一条说明"]
    assert entry["summary"] is None


async def test_notes_malformed_is_empty_not_invented(logged_in: TestEnv, tmp_path: Path) -> None:
    """结构非法/超限/坏 URL → 没有更新内容（但更新结论照旧）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(
        base,
        release_notes={
            "schema": 1,
            "notes": {
                "1.1.0": {
                    "summary": "x" * (version_routes.IMAGE_NOTE_MAX_CHARS + 1),
                    "items": [
                        "y" * (version_routes.IMAGE_NOTE_MAX_CHARS + 1),
                        "",
                        "  正常的一条  ",
                        "../../etc/passwd",
                        *[f"第{i}条" for i in range(20)],
                        42,
                    ],
                    "url": "javascript:alert(1)",
                    "path": "/vol1/1000/ling",
                },
                "../escape": {"items": ["不该出现"]},
                "not-semver": {"items": ["不该出现"]},
            },
        },
    )
    response = await logged_in.client.get("/v1/version/image-update")
    assert "/vol1/1000" not in response.text
    assert "javascript:" not in response.text
    entry = response.json()["newer"][0]
    assert entry["summary"] is None  # 超长摘要整条丢弃，不截半句话
    assert entry["notes_url"] is None
    assert "正常的一条" in entry["notes"]
    assert all(len(note) <= version_routes.IMAGE_NOTE_MAX_CHARS for note in entry["notes"])
    assert len(entry["notes"]) <= version_routes.IMAGE_NOTE_MAX_ITEMS
    assert "不该出现" not in entry["notes"]


# ================================================================ 按需检查（POST checks）
#
# 设计依据：docs/UPDATE-UX-DESIGN.md §真正的按需检查协议。
# 这个接口**只写一条刷新请求**：不访问 registry、不改 status.json、不伪造 checked_at。


def _visible_files(directory: Path) -> list[str]:
    """目录里的普通文件（不含 ``.lock`` 这类隐藏文件，也不含 ``archive/`` 子目录）。"""
    if not directory.is_dir():
        return []
    return sorted(
        item.name
        for item in directory.iterdir()
        if not item.name.startswith(".") and item.is_file()
    )


async def test_check_requires_auth(web_env: TestEnv) -> None:
    response = await web_env.client.post(
        "/v1/version/image-update-checks", json={"request_id": uuid.uuid4().hex}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_check_requires_owner(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="member-check", password="member-password", role="member"
        )
    assert (
        await logged_in.client.post(
            "/v1/auth/login",
            json={"username": "member-check", "password": "member-password"},
        )
    ).status_code == 200
    response = await _check(logged_in)
    assert response.status_code == 403
    assert response.json()["error"]["detail"] == "not_owner"
    assert _visible_files(base / "requests") == []


async def test_check_requires_same_origin(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    missing = await logged_in.client.post(
        "/v1/version/image-update-checks", json={"request_id": uuid.uuid4().hex}
    )
    assert missing.status_code == 403
    assert missing.json()["error"]["detail"] == "origin_required"
    foreign = await logged_in.client.post(
        "/v1/version/image-update-checks",
        json={"request_id": uuid.uuid4().hex},
        headers={"Origin": "http://evil.example"},
    )
    assert foreign.status_code == 403
    assert foreign.json()["error"]["detail"] == "origin_mismatch"
    assert _visible_files(base / "requests") == []


async def test_check_works_without_recent_login(logged_in: TestEnv, tmp_path: Path) -> None:
    """检查是**只读**动作：登录 45 分钟后仍可发起（旧版这里给了个看不懂的 403）。

    安装请求 ``POST /v1/version/image-update-requests`` 仍然要求最近登录
    （见 ``test_post_requires_recent_login``）——两件事的授权边界不同。
    """
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
    request_id = uuid.uuid4().hex
    response = await _check(logged_in, request_id)
    assert response.status_code == 202, response.text
    assert response.json()["request_id"] == request_id
    assert _visible_files(base / "requests") == [f"refresh-{request_id}.json"]

    # 同一个过期会话提交**安装**请求仍然被拒（权限边界没变）。
    install = await _post(logged_in)
    assert install.status_code == 403
    assert install.json()["error"]["detail"] == "recent_login_required"
    assert _request_files(base) == []


async def test_check_writes_one_atomic_refresh_file(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """202 + 恰好一个 ``refresh-<id>.json``，内容严格固定，且**不**写安装请求。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    request_id = uuid.uuid4().hex
    response = await _check(logged_in, request_id)
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "schema_version": "v1",
        "request_id": request_id,
        "accepted": True,
    }
    requests_dir = base / "requests"
    assert _visible_files(requests_dir) == [f"refresh-{request_id}.json"]
    assert _request_files(base) == []  # 安装队列一个字节都没写
    path = requests_dir / f"refresh-{request_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "schema": 1,
        "request_id": request_id,
        "kind": "refresh",
        "created_at": payload["created_at"],
    }
    assert update_protocol.parse_iso_timestamp(payload["created_at"]) is not None
    # 私密性：文件不含 tag/URL/路径，权限不向组/其他用户开放。
    for forbidden in ("ghcr.io", "/vol1", "http"):
        assert forbidden not in path.read_text(encoding="utf-8")
    assert (path.stat().st_mode & 0o007) == 0  # 其他用户不可读（0600/0640）


async def test_check_does_not_touch_status_json(logged_in: TestEnv, tmp_path: Path) -> None:
    """按需检查**不改**状态文件（事实只能由更新器写，API 不许伪造）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, release_check=None)
    status = base / "status" / version_routes.IMAGE_UPDATER_STATUS_FILE
    before = status.read_bytes()
    before_mtime = status.stat().st_mtime_ns
    assert (await _check(logged_in)).status_code == 202
    assert status.read_bytes() == before
    assert status.stat().st_mtime_ns == before_mtime
    view = await _view(logged_in)
    # 状态没变 → 检查时间照旧为 null，不因为"刚点过检查"就编一个时间。
    assert view["checked_at"] is None


async def test_check_is_idempotent_for_the_same_request_id(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    request_id = uuid.uuid4().hex
    first = await _check(logged_in, request_id)
    assert first.status_code == 202
    second = await _check(logged_in, request_id)
    assert second.status_code == 200
    assert second.json() == {
        "schema_version": "v1",
        "request_id": request_id,
        "accepted": True,
    }
    assert _visible_files(base / "requests") == [f"refresh-{request_id}.json"]


async def test_check_keeps_one_pending_request(logged_in: TestEnv, tmp_path: Path) -> None:
    """已有未认领的刷新请求 → 第二个不同 ID 只回**那一个**，绝不写第二个文件。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    first_id = uuid.uuid4().hex
    assert (await _check(logged_in, first_id)).status_code == 202
    second = await _check(logged_in, uuid.uuid4().hex)
    assert second.status_code == 200
    assert second.json()["request_id"] == first_id
    assert _visible_files(base / "requests") == [f"refresh-{first_id}.json"]


async def test_check_recent_finished_check_is_deduped(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """归档里 30 秒内刚完成过检查 → 回同一个 ID，不再打一次 registry。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    finished_id = uuid.uuid4().hex
    archive = base / "requests" / "archive"
    archive.mkdir()
    update_protocol.atomic_write_json(
        archive / f"refresh-{finished_id}.json",
        {"schema": 1, "request_id": finished_id, "kind": "refresh",
         "created_at": update_protocol.iso_now()},
        mode=0o640,
    )
    response = await _check(logged_in, uuid.uuid4().hex)
    assert response.status_code == 200
    assert response.json()["request_id"] == finished_id
    assert _visible_files(base / "requests") == []


async def test_check_old_archived_check_does_not_block(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """超过 30 秒的旧检查不挡新检查（限流窗口是有意的，不是无限期）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    old_id = uuid.uuid4().hex
    archive = base / "requests" / "archive"
    archive.mkdir()
    path = archive / f"refresh-{old_id}.json"
    update_protocol.atomic_write_json(
        path,
        {"schema": 1, "request_id": old_id, "kind": "refresh",
         "created_at": update_protocol.iso_now()},
        mode=0o640,
    )
    stale = time.time() - version_routes.IMAGE_CHECK_DEDUP_SECONDS - 5
    os.utime(path, (stale, stale))
    request_id = uuid.uuid4().hex
    response = await _check(logged_in, request_id)
    assert response.status_code == 202
    assert response.json()["request_id"] == request_id


async def test_claimed_refresh_counts_as_pending(logged_in: TestEnv, tmp_path: Path) -> None:
    """更新器已认领（``claimed-refresh-``）但还没归档 → 同一 ID 幂等，不写第二份。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    request_id = uuid.uuid4().hex
    update_protocol.atomic_write_json(
        base / "requests" / f"claimed-refresh-{request_id}.json",
        {"schema": 1, "request_id": request_id, "kind": "refresh",
         "created_at": update_protocol.iso_now()},
        mode=0o640,
    )
    response = await _check(logged_in, request_id)
    assert response.status_code == 200
    assert response.json()["request_id"] == request_id
    assert _visible_files(base / "requests") == [f"claimed-refresh-{request_id}.json"]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"request_id": "not-a-hex"},
        {"request_id": "A" * 32},
        {"request_id": uuid.uuid4().hex, "target_digest": TARGET_DIGEST},
        {"request_id": uuid.uuid4().hex, "schema": 1},
        {"check_id": uuid.uuid4().hex},
        "not-an-object",
    ],
)
async def test_check_rejects_invalid_body(
    logged_in: TestEnv, tmp_path: Path, body: object
) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await logged_in.client.post(
        "/v1/version/image-update-checks", json=body, headers=ORIGIN
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["detail"] in {"invalid_request_body", "invalid_request_id"}
    assert _visible_files(base / "requests") == []


async def test_check_rejects_non_json_body(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await logged_in.client.post(
        "/v1/version/image-update-checks",
        content=b'{"request_id": "' + uuid.uuid4().hex.encode() + b'"}',
        headers={**ORIGIN, "Content-Type": "text/plain"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == "content_type_required"
    assert _visible_files(base / "requests") == []


async def test_check_rejects_oversized_body(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    response = await logged_in.client.post(
        "/v1/version/image-update-checks",
        content=b"{" + b" " * (version_routes.MAX_UPDATE_BODY_BYTES + 1) + b"}",
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["detail"] == "request_body_too_large"
    assert _visible_files(base / "requests") == []


async def test_check_rate_limited(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    for _ in range(version_routes.IMAGE_UPDATE_RATE_LIMIT):
        assert (await _check(logged_in)).status_code == 202
        # 每次清掉上一条待处理请求：让"一个待处理上限"不掩盖限流本身。
        for item in (base / "requests").iterdir():
            if item.name.startswith("refresh-"):
                item.unlink()
    response = await _check(logged_in)
    assert response.status_code == 429
    assert response.json()["error"]["detail"] == "rate_limited"


async def test_check_requests_dir_unwritable_is_409(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """请求目录不可写 → 409 + 码表原因（绝不假装"已排队"）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    requests_dir = base / "requests"
    requests_dir.chmod(0o500)
    try:
        response = await _check(logged_in)
    finally:
        requests_dir.chmod(0o750)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_channel_unavailable"
    assert _visible_files(requests_dir) == []


async def test_check_write_failure_leaves_no_partial_file(
    logged_in: TestEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rename 失败（磁盘/权限问题）→ 409，且**不留下**半截文件或临时文件。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    requests_dir = base / "requests"

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(update_protocol.os, "replace", boom)
    response = await _check(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_channel_unavailable"
    monkeypatch.undo()
    assert _visible_files(requests_dir) == []
    assert [p for p in requests_dir.iterdir() if p.name != ".lock"] == []


async def test_check_updater_offline_is_409(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    _write_status(base, updated_at=_STALE)
    response = await _check(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "updater_offline"
    assert _visible_files(base / "requests") == []


async def test_check_status_missing_is_409(logged_in: TestEnv, tmp_path: Path) -> None:
    base = _configure(logged_in, tmp_path)
    response = await _check(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "updater_status_missing"
    assert _visible_files(base / "requests") == []


async def test_check_channel_dir_missing_is_409(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    base = _configure(logged_in, tmp_path, requests_dir=False)
    _write_status(base)
    response = await _check(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "update_channel_unavailable"


async def test_check_works_with_install_disabled(logged_in: TestEnv, tmp_path: Path) -> None:
    """安装被关掉时「重新检查」仍要能用（设计：否则用户无法确认有没有新版）。"""
    base = _configure(logged_in, tmp_path, enabled=False)
    _write_status(base)
    response = await _check(logged_in)
    assert response.status_code == 202
    view = await _view(logged_in)
    assert view["installable"] is False
    assert view["reason"] == "update_disabled"


async def test_check_works_while_install_task_is_running(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """安装进行中：刷新请求排队等收尾，API 不在这里拒绝（设计 §按需检查协议）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base, state="busy", task=_task(state="running", stage="backup"))
    response = await _check(logged_in)
    assert response.status_code == 202


async def test_install_request_and_check_queues_do_not_collide(
    logged_in: TestEnv, tmp_path: Path
) -> None:
    """两条队列前缀不同：检查不占安装队列，安装也不被检查挡住（旧语义不变）。"""
    base = _configure(logged_in, tmp_path)
    _write_status(base)
    check_id = uuid.uuid4().hex
    assert (await _check(logged_in, check_id)).status_code == 202
    install = await _post(logged_in)
    assert install.status_code == 201, install.text
    names = _visible_files(base / "requests")
    assert names == sorted(
        [f"refresh-{check_id}.json", f"request-{install.json()['request_id']}.json"]
    ), names
    # 安装请求仍按原样排队（更新器只认 request-*.json）。
    view = await _view(logged_in)
    assert view["request"]["request_id"] == install.json()["request_id"]
