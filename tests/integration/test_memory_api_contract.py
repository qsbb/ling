"""记忆读契约（IA Step 0.5）的端点级测试。

覆盖 8 个记忆端点（`/v1/memory` 5 个 + `/v1/core-memory` 3 个）的
**正向路径**、默认过滤、参数校验、归属隔离与遗忘行为。全部离线运行，
只用 `tests/integration/conftest.py` 的既有 fixture（真实 PostgreSQL）。

契约冻结在 `docs/API-WEB-V1.md` §10。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from ling.contracts.v1 import MemoryProposal, Origin, Visibility
from ling.domains.identity.service import IdentityService
from ling.domains.memory.core import MAX_PER_SUBJECT, CoreMemoryService
from ling.domains.memory.service import MemoryService
from ling.infra.db import session_scope
from ling.infra.models import (
    AwarenessItemRow,
    CoreMemoryRow,
    MemoryEpisode,
    MemoryRow,
    utcnow,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


# ------------------------------------------------------------------ 帮手


async def _create_memory(env: TestEnv, text: str, **overrides: object) -> str:
    payload: dict[str, object] = {
        "kind": "assertion",
        "text": text,
        "origin": "user_statement",
        "visibility": "subject_private",
        "confidence": 0.8,
        "source_event_ids": [f"evt_{uuid.uuid4().hex[:8]}"],
    }
    payload.update(overrides)
    response = await env.client.post("/v1/memory", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["memory_id"]


async def _archive(memory_id: str) -> None:
    async with session_scope() as session:
        await session.execute(
            update(MemoryRow).where(MemoryRow.id == memory_id).values(archived_at=utcnow())
        )


async def _other_subject() -> str:
    async with session_scope() as session:
        view = await IdentityService(session).create_user(
            username=f"other_{uuid.uuid4().hex[:8]}", password="pw-not-used"
        )
        return view.subject_id


async def _seed_episode(subject_id: str, summary: str = "一段情节") -> str:
    episode_id = f"ep_{uuid.uuid4().hex[:12]}"
    async with session_scope() as session:
        session.add(
            MemoryEpisode(
                id=episode_id,
                subject_id=subject_id,
                window_id="win_contract",
                summary=summary,
                origin="user_statement",
                visibility="subject_private",
            )
        )
    return episode_id


async def _seed_awareness(subject_id: str, text: str = "一条意识") -> str:
    awareness_id = f"aw_{uuid.uuid4().hex[:12]}"
    async with session_scope() as session:
        session.add(
            AwarenessItemRow(
                id=awareness_id,
                subject_id=subject_id,
                kind="subject_fact",
                text=text,
                audience="subject_scoped",
            )
        )
    return awareness_id


async def _seed_memory_for(subject_id: str, text: str = "别人的记忆") -> str:
    async with session_scope() as session:
        memory_id = await MemoryService(session).propose(
            MemoryProposal(
                proposal_id=f"p_{uuid.uuid4().hex[:8]}",
                kind="assertion",
                text=text,
                origin=Origin.user_statement,
                source_event_ids=["evt_other"],
                visibility=Visibility.subject_private,
                confidence=0.9,
            ),
            subject_id=subject_id,
        )
    assert memory_id
    return memory_id


def _assert_error_shape(response, expected_code: str = "invalid_request") -> None:
    """统一错误形状：`{schema_version, error:{code,message,retriable,detail,trace_id}}`。"""
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["schema_version"] == "v1"
    assert body["error"]["code"] == expected_code
    assert set(body["error"]) >= {"code", "message", "retriable", "detail", "trace_id"}


def _assert_pydantic_error_shape(response, field: str | None = None) -> None:
    """统一形状 + 指明字段 + 带 pydantic 原始报错（`error.detail`）。

    `detail` 非空是"这条 422 真由 pydantic 的 `RequestValidationError` 抛出、
    再被 `create_app` 的前缀处理器翻译"的证据：端点内显式校验抛的 `LingError`
    不带 detail。约束若又退回端点（task-105 的做法），这里会红。
    """
    _assert_error_shape(response)
    error = response.json()["error"]
    if field is not None:
        assert field in error["message"], error
    assert error["detail"], f"应当保留 pydantic 的原始校验信息（detail）: {error}"
    # trace_id 必须真的能定位那一次请求：体内有、响应头同名
    assert error["trace_id"], error
    assert response.headers.get("X-Trace-Id") == error["trace_id"], response.headers


# ------------------------------------------------------- 错误形状的作用范围


async def test_validation_errors_outside_memory_keep_fastapi_default(
    logged_in: TestEnv,
) -> None:
    """非记忆路径仍是 FastAPI 默认的 `{"detail":[...]}`。

    前缀处理器只覆盖 `/v1/memory*`、`/v1/core-memory*`；把它改成全局会让
    其它端点（这里是登录与隔离模式）的 422 形状一起变——那是没被批准的
    行为变更，本用例就是这条边界的探针。
    """
    login = await logged_in.client.post("/v1/auth/login", json={})
    assert login.status_code == 422, login.text
    assert set(login.json()) == {"detail"}, login.text
    assert isinstance(login.json()["detail"], list)

    # 同样带 pattern 的字段，但不在记忆前缀内 → 仍是默认形状
    isolation = await logged_in.client.put("/v1/isolation", json={"mode": "nonsense"})
    assert isolation.status_code == 422, isolation.text
    assert set(isolation.json()) == {"detail"}, isolation.text


@pytest.mark.parametrize(
    "payload",
    [
        {"args": []},
        {"confirmed": "notabool"},
        {"window_id": 3},
    ],
)
async def test_tools_validation_errors_keep_fastapi_default(
    logged_in: TestEnv, payload: dict[str, object]
) -> None:
    """非记忆端点（tools）的 422 仍是 FastAPI 原生 `{"detail":[...]}`。

    前缀处理器只覆盖 `/v1/memory*`、`/v1/core-memory*`；如果把它扩大成全局，
    tools 这类既有端点的客户端就解析不到 `detail` 列表了。
    """
    response = await logged_in.client.post("/v1/tools/anything/call", json=payload)
    assert response.status_code == 422, response.text
    body = response.json()
    assert set(body) == {"detail"}, body
    assert isinstance(body["detail"], list)


@pytest.mark.parametrize("path", ["/v1/memoryfoo", "/v1/memories"])
async def test_memory_prefix_does_not_match_lookalike_paths(logged_in: TestEnv, path: str) -> None:
    """`/v1/memoryfoo`、`/v1/memories` 不是记忆端点，不能落进统一 422 形状。

    前缀匹配必须是整段（`path == prefix or path.startswith(prefix + "/")`）。
    改成裸 `startswith("/v1/memory")` 会把这两个路径也交给前缀处理器。
    它们没有路由，实际命中 404（有前端构建时是 SPA 兜底的
    `{"detail":"not found"}`，否则是 Starlette 默认的 `{"detail":"Not Found"}`）。
    """
    response = await logged_in.client.get(path)
    assert response.status_code == 404, response.text
    body = response.json()
    assert set(body) == {"detail"}, body
    assert body["detail"].lower() == "not found", body


# ---------------------------------------------- /openapi.json 的约束保真度


def test_memory_openapi_declares_the_same_constraints_as_runtime(
    logged_in: TestEnv,
) -> None:
    """机器可读契约必须带 required/pattern/minLength（task-105 的 Medium 回归）。

    约束一旦被搬进端点、只留在运行时，`/openapi.json` 就会允许"省略 kind /
    不带 retract body / reason 任意长度 / scope 任意值"这类必被 422 的请求；
    生成客户端和 Swagger UI 会照着拼。这里把 4 处钉死在契约上。
    """
    from fastapi import FastAPI

    app = logged_in.app
    assert isinstance(app, FastAPI)
    spec = app.openapi()
    schemas = spec["components"]["schemas"]

    create = schemas["MemoryCreate"]
    assert "kind" in create["required"]
    assert create["properties"]["kind"]["pattern"] == "^(episode|assertion|procedure)$"
    assert "default" not in create["properties"]["kind"]

    retract = schemas["MemoryRetract"]
    assert "reason" in retract["required"]
    assert retract["properties"]["reason"]["minLength"] == 1
    assert retract["properties"]["reason"]["maxLength"] == 500

    retract_body = spec["paths"]["/v1/memory/{memory_id}/retract"]["post"]["requestBody"]
    assert retract_body.get("required") is True
    assert retract_body["content"]["application/json"]["schema"]["$ref"].endswith("/MemoryRetract")

    core = schemas["CoreMemoryBody"]
    assert core["properties"]["scope"]["pattern"] == "^(private|group|global)$"
    assert core["properties"]["scope"]["default"] == "private"

    # `limit` 的类型/默认值不变（越界由端点判定），只新增 `include`
    params = {
        p["name"]: p for p in spec["paths"]["/v1/memory"]["get"]["parameters"] if p["in"] == "query"
    }
    assert params["limit"]["schema"]["type"] == "integer"
    assert params["limit"]["schema"]["default"] == 50
    assert params["include"]["required"] is False


def test_memory_openapi_pins_include_enum_and_limit_range(
    logged_in: TestEnv,
) -> None:
    """钉死“`include` 的三值进契约、`limit` 的范围只留在端点”的现状。

    `include` 以前在 OpenAPI 里只有 `string|null`：生成客户端会把
    `?include=deleted` 当合法值，运行时却 422（§10 的 Low-1）。现在三个值
    写进 `enum`；删掉它不是“清理”，而是让机器契约重新弱于运行时。
    `limit` 则刻意**不**加 `minimum`/`maximum`：`1..200` 仍由端点判定
    （端点级 `LingError`，`error.detail=null`），OpenAPI 只声明 `integer`。
    两条负断言防止有人只改 schema 或只改端点，让契约与 §10 漂移。
    """
    from fastapi import FastAPI

    app = logged_in.app
    assert isinstance(app, FastAPI)
    params = {
        p["name"]: p
        for p in app.openapi()["paths"]["/v1/memory"]["get"]["parameters"]
        if p["in"] == "query"
    }

    include_schema = params["include"]["schema"]
    assert include_schema["enum"] == ["retracted", "archived", "all"]
    assert params["include"]["required"] is False

    limit_schema = params["limit"]["schema"]
    assert limit_schema["type"] == "integer"
    assert "minimum" not in limit_schema
    assert "maximum" not in limit_schema


# ------------------------------------------------------------------ GET /v1/memory


async def test_get_memory_default_excludes_tombstones(logged_in: TestEnv) -> None:
    """默认只返回现行记忆：撤回的、归档的都不出现；字段带时间与归档状态。"""
    active = await _create_memory(logged_in, "现行记忆")
    retracted = await _create_memory(logged_in, "已撤回记忆")
    archived = await _create_memory(logged_in, "已归档记忆")
    response = await logged_in.client.post(
        f"/v1/memory/{retracted}/retract", json={"reason": "用户纠正"}
    )
    assert response.status_code == 200, response.text
    await _archive(archived)

    listing = await logged_in.client.get("/v1/memory")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    ids = [item["memory_id"] for item in body["memories"]]
    assert ids == [active]
    assert body["total"] == 1
    assert body["has_more"] is False
    assert body["include"] is None

    item = body["memories"][0]
    # 既有字段不变
    assert set(item) >= {
        "memory_id",
        "kind",
        "text",
        "origin",
        "visibility",
        "confidence",
        "retracted",
        "retract_reason",
        "supersedes",
        "superseded_by",
        "source_event_ids",
        "valid_until",
    }
    # 新增字段：无则 null，有则 ISO-8601
    assert item["archived_at"] is None
    assert item["valid_until"] is None
    assert datetime.fromisoformat(item["created_at"]).tzinfo is not None


async def test_get_memory_timestamps_match_database(logged_in: TestEnv) -> None:
    """`created_at` / `archived_at` 必须与 DB 真值逐字相等，不能是"当前时间"占位。"""
    memory_id = await _create_memory(logged_in, "时间戳真值")
    await _archive(memory_id)

    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row is not None

    body = (await logged_in.client.get("/v1/memory?include=archived")).json()
    item = next(i for i in body["memories"] if i["memory_id"] == memory_id)
    assert item["created_at"] == row.created_at.isoformat()
    assert item["archived_at"] == row.archived_at.isoformat()


async def test_get_memory_include_retracted_returns_tombstone(logged_in: TestEnv) -> None:
    active = await _create_memory(logged_in, "现行记忆")
    retracted = await _create_memory(logged_in, "已撤回记忆")
    await logged_in.client.post(f"/v1/memory/{retracted}/retract", json={"reason": "用户纠正"})

    body = (await logged_in.client.get("/v1/memory?include=retracted")).json()
    ids = {item["memory_id"] for item in body["memories"]}
    assert ids == {active, retracted}
    tombstone = next(i for i in body["memories"] if i["memory_id"] == retracted)
    assert tombstone["retracted"] is True
    assert tombstone["retract_reason"] == "用户纠正"
    assert body["include"] == "retracted"


async def test_get_memory_include_archived_returns_archived(logged_in: TestEnv) -> None:
    active = await _create_memory(logged_in, "现行记忆")
    archived = await _create_memory(logged_in, "已归档记忆")
    await _archive(archived)

    body = (await logged_in.client.get("/v1/memory?include=archived")).json()
    ids = {item["memory_id"] for item in body["memories"]}
    assert ids == {active, archived}
    row = next(i for i in body["memories"] if i["memory_id"] == archived)
    assert row["archived_at"] is not None
    assert datetime.fromisoformat(row["archived_at"]).tzinfo is not None


async def test_get_memory_include_all_returns_everything(logged_in: TestEnv) -> None:
    active = await _create_memory(logged_in, "现行记忆")
    retracted = await _create_memory(logged_in, "已撤回记忆")
    archived = await _create_memory(logged_in, "已归档记忆")
    await logged_in.client.post(f"/v1/memory/{retracted}/retract", json={"reason": "用户纠正"})
    await _archive(archived)

    body = (await logged_in.client.get("/v1/memory?include=all")).json()
    ids = {item["memory_id"] for item in body["memories"]}
    assert ids == {active, retracted, archived}
    assert body["total"] == 3


async def test_get_memory_include_all_keeps_retracted_and_archived_row(
    logged_in: TestEnv,
) -> None:
    """`include=all` 必须含"既撤回又归档"的行；单类 include 仍要排除它。"""
    both = await _create_memory(logged_in, "既撤回又归档")
    await logged_in.client.post(f"/v1/memory/{both}/retract", json={"reason": "用户纠正"})
    await _archive(both)

    all_body = (await logged_in.client.get("/v1/memory?include=all")).json()
    all_items = {item["memory_id"]: item for item in all_body["memories"]}
    assert both in all_items, "include=all 漏掉了同时撤回且归档的行"
    assert all_items[both]["retracted"] is True
    assert all_items[both]["archived_at"] is not None
    assert all_body["total"] == 1

    # include 是"追加一类"，另一类墓碑仍排除：双向都断言，防止只查一类。
    retracted_ids = {
        item["memory_id"]
        for item in (await logged_in.client.get("/v1/memory?include=retracted")).json()["memories"]
    }
    assert retracted_ids == set(), "include=retracted 不应带出已归档的行"
    archived_ids = {
        item["memory_id"]
        for item in (await logged_in.client.get("/v1/memory?include=archived")).json()["memories"]
    }
    assert archived_ids == set(), "include=archived 不应带出已撤回的行"


async def test_get_memory_rejects_invalid_include(logged_in: TestEnv) -> None:
    response = await logged_in.client.get("/v1/memory?include=deleted")
    _assert_error_shape(response)


@pytest.mark.parametrize("limit", [0, -1, 201, 999])
async def test_get_memory_rejects_out_of_range_limit(logged_in: TestEnv, limit: int) -> None:
    """limit 越界必须是 422 + 统一错误形状，不能 200 空列表也不能 500。"""
    response = await logged_in.client.get(f"/v1/memory?limit={limit}")
    _assert_error_shape(response)


async def test_get_memory_limit_total_and_has_more(logged_in: TestEnv) -> None:
    for text in ("一", "二", "三"):
        await _create_memory(logged_in, f"记忆{text}")

    body = (await logged_in.client.get("/v1/memory?limit=1")).json()
    assert len(body["memories"]) == 1
    assert body["limit"] == 1
    assert body["total"] == 3
    assert body["has_more"] is True


@pytest.mark.parametrize("raw_limit", ["abc", "", "1.5", "0x10", "1e3"])
async def test_get_memory_non_integer_limit_uses_error_shape(
    logged_in: TestEnv, raw_limit: str
) -> None:
    """非整数 limit 走 pydantic 校验，再被前缀处理器翻译成统一 ErrorResponse。

    以前这里靠端点前的手写解析（抛 `LingError`，`detail` 为空）；现在约束在
    pydantic 里，`limit` 的 OpenAPI 类型不变，`detail` 带 pydantic 原文。
    """
    response = await logged_in.client.get("/v1/memory", params={"limit": raw_limit})
    _assert_pydantic_error_shape(response, "limit")


async def test_get_memory_same_created_at_has_stable_order(logged_in: TestEnv) -> None:
    """同一 `created_at` 的多行必须有确定次级排序；否则 limit 截断会漏行/重复。"""
    stamp = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    # 故意按 c → a → b 插入，让"没有次级键时的物理顺序"（c,a,b）≠ 期望顺序。
    async with session_scope() as session:
        for suffix in ("c", "a", "b"):
            session.add(
                MemoryRow(
                    id=f"mem_tie_{suffix}",
                    subject_id=logged_in.subject_id,
                    kind="assertion",
                    text=f"同一时刻 {suffix}",
                    origin="user_statement",
                    visibility="subject_private",
                    confidence=0.5,
                    source_event_ids=["evt_tie"],
                    created_at=stamp,
                    updated_at=stamp,
                )
            )

    body = (await logged_in.client.get("/v1/memory")).json()
    assert [item["memory_id"] for item in body["memories"]] == [
        "mem_tie_c",
        "mem_tie_b",
        "mem_tie_a",
    ]

    # limit 截断必须取同一前缀：两页之间不漏、不重。
    first_page = (await logged_in.client.get("/v1/memory", params={"limit": 2})).json()
    assert [item["memory_id"] for item in first_page["memories"]] == [
        "mem_tie_c",
        "mem_tie_b",
    ]
    assert first_page["limit"] == 2
    assert first_page["total"] == 3
    assert first_page["has_more"] is True


async def test_get_memory_isolates_other_subject(logged_in: TestEnv) -> None:
    other = await _other_subject()
    theirs = await _seed_memory_for(other, "别人的秘密")
    mine = await _create_memory(logged_in, "我的记忆")

    body = (await logged_in.client.get("/v1/memory?include=all")).json()
    ids = {item["memory_id"] for item in body["memories"]}
    assert mine in ids
    assert theirs not in ids


async def test_get_memory_exposes_expired_valid_until_without_hiding(
    logged_in: TestEnv,
) -> None:
    """本次只收窄墓碑可见性：`valid_until` 过期的行仍返回（语义不变）。"""
    memory_id = await _create_memory(logged_in, "已经过期的事实")
    expired = datetime.now(UTC) - timedelta(days=1)
    async with session_scope() as session:
        await session.execute(
            update(MemoryRow).where(MemoryRow.id == memory_id).values(valid_until=expired)
        )

    body = (await logged_in.client.get("/v1/memory")).json()
    row = next(i for i in body["memories"] if i["memory_id"] == memory_id)
    assert datetime.fromisoformat(row["valid_until"]) == expired


# ------------------------------------------------------------------ 鉴权补齐


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/v1/memory/mem_x/retract", {"reason": "x"}),
        ("post", "/v1/memory/mem_x/supersede", {"text": "x"}),
        ("post", "/v1/memory/forget", None),
        ("put", "/v1/core-memory", {"label": "x", "text": "y"}),
        ("delete", "/v1/core-memory/core_x", None),
    ],
)
async def test_memory_write_endpoints_require_auth(
    web_env: TestEnv, method: str, path: str, payload: dict | None
) -> None:
    """retract/supersede/forget 与 core 写操作此前不在鉴权清单里。"""
    response = await web_env.client.request(method.upper(), path, json=payload)
    assert response.status_code == 401, f"{method.upper()} {path} → {response.status_code}"


# ------------------------------------------------------------------ POST /v1/memory


async def test_post_memory_persists_row(logged_in: TestEnv) -> None:
    event_id = f"evt_{uuid.uuid4().hex[:8]}"
    response = await logged_in.client.post(
        "/v1/memory",
        json={
            "kind": "assertion",
            "text": "用户喜欢下雨天",
            "origin": "user_statement",
            "visibility": "subject_private",
            "confidence": 0.73,
            "source_event_ids": [event_id],
        },
    )
    assert response.status_code == 200, response.text
    memory_id = response.json()["memory_id"]

    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row is not None
    assert row.subject_id == logged_in.subject_id
    assert row.text == "用户喜欢下雨天"
    assert row.confidence == pytest.approx(0.73)
    assert row.source_event_ids == [event_id]

    listed = (await logged_in.client.get("/v1/memory")).json()
    assert any(item["memory_id"] == memory_id for item in listed["memories"])


async def test_post_memory_requires_source_events(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/memory",
        json={"kind": "assertion", "text": "无来源", "source_event_ids": []},
    )
    _assert_error_shape(response)


@pytest.mark.parametrize(
    "payload",
    [
        {"origin": "nonsense"},
        {"visibility": "nonsense"},
        {"origin": "user_statement", "visibility": "not_a_scope"},
    ],
)
async def test_post_memory_invalid_origin_or_visibility_is_422_not_500(
    logged_in: TestEnv, payload: dict
) -> None:
    """非法枚举过去直接抛 ValueError → 真实服务 500；必须转成 422。"""
    body = {
        "kind": "assertion",
        "text": "非法枚举",
        "source_event_ids": [f"evt_{uuid.uuid4().hex[:8]}"],
        **payload,
    }
    response = await logged_in.client.post("/v1/memory", json=body)
    _assert_error_shape(response)


async def test_post_memory_model_inference_rejected(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/memory",
        json={
            "kind": "assertion",
            "text": "模型猜的",
            "origin": "model_inference",
            "source_event_ids": ["evt_x"],
        },
    )
    _assert_error_shape(response)


@pytest.mark.parametrize("kind", ["nonsense", "", "Episode"])
async def test_post_memory_invalid_kind_uses_error_shape(logged_in: TestEnv, kind: str) -> None:
    """非法 kind 走 pydantic 的 pattern 校验，形状由前缀处理器统一。

    `kind` 的约束不能搬进端点：搬走后 `/openapi.json` 就不再有
    `required: ["kind"]` 与 pattern（见
    `test_memory_openapi_declares_the_same_constraints_as_runtime`）。
    """
    response = await logged_in.client.post(
        "/v1/memory",
        json={
            "kind": kind,
            "text": "非法 kind",
            "source_event_ids": [f"evt_{uuid.uuid4().hex[:8]}"],
        },
    )
    _assert_pydantic_error_shape(response, "kind")


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"text": "x" * 4001}, "text"),  # 超过 max_length（端点从不显式检查）
        ({"confidence": 1.5}, "confidence"),  # 超过 le=1.0
        ({"source_event_ids": "evt_x"}, "source_event_ids"),  # 类型必须是数组
    ],
)
async def test_post_memory_pydantic_only_fields_use_error_shape(
    logged_in: TestEnv, overrides: dict, field: str
) -> None:
    """端点**没有**显式校验的字段也必须返回统一形状。

    这几条只有 pydantic 拦得住，所以它们绿 = 前缀处理器真的在翻译
    `RequestValidationError`，而不是端点里另写了校验。
    """
    payload = {
        "kind": "assertion",
        "text": "合法文本",
        "source_event_ids": [f"evt_{uuid.uuid4().hex[:8]}"],
    }
    payload.update(overrides)
    response = await logged_in.client.post("/v1/memory", json=payload)
    _assert_pydantic_error_shape(response, field)


# ------------------------------------------------------------------ retract


async def test_retract_own_memory_is_idempotent(logged_in: TestEnv) -> None:
    memory_id = await _create_memory(logged_in, "会被撤回的记忆")
    first = await logged_in.client.post(
        f"/v1/memory/{memory_id}/retract", json={"reason": "不再适用"}
    )
    assert first.status_code == 200, first.text
    assert first.json() == {"memory_id": memory_id, "retracted": True}

    second = await logged_in.client.post(
        f"/v1/memory/{memory_id}/retract", json={"reason": "再次撤回"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["retracted"] is True

    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row is not None
    assert row.retracted is True
    assert row.retract_reason == "再次撤回"
    assert row.retracted_at is not None

    # 默认列表立刻看不到；include=retracted 才看得到
    default_ids = {
        item["memory_id"] for item in (await logged_in.client.get("/v1/memory")).json()["memories"]
    }
    assert memory_id not in default_ids
    tombstone_ids = {
        item["memory_id"]
        for item in (await logged_in.client.get("/v1/memory?include=retracted")).json()["memories"]
    }
    assert memory_id in tombstone_ids


async def test_retract_other_subject_is_404(logged_in: TestEnv) -> None:
    other = await _other_subject()
    memory_id = await _seed_memory_for(other, "别人的记忆")

    response = await logged_in.client.post(
        f"/v1/memory/{memory_id}/retract", json={"reason": "越权"}
    )
    assert response.status_code == 404, response.text
    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row is not None and row.retracted is False


@pytest.mark.parametrize(
    "payload",
    [{}, {"reason": ""}, {"reason": None}, {"reason": "x" * 501}],
)
async def test_retract_rejects_invalid_reason(logged_in: TestEnv, payload: dict) -> None:
    """reason 缺失/空串/超长由 pydantic 的 required+minLength/maxLength 拦住。"""
    memory_id = await _create_memory(logged_in, "待撤回")
    response = await logged_in.client.post(f"/v1/memory/{memory_id}/retract", json=payload)
    _assert_pydantic_error_shape(response, "reason")


async def test_retract_missing_body_uses_error_shape(logged_in: TestEnv) -> None:
    """整个 body 缺失也由 pydantic（requestBody.required）拦住，形状同样统一。"""
    memory_id = await _create_memory(logged_in, "待撤回")
    response = await logged_in.client.post(f"/v1/memory/{memory_id}/retract")
    _assert_pydantic_error_shape(response)


# ------------------------------------------------------------------ supersede


async def test_supersede_creates_chain_and_retracts_old(logged_in: TestEnv) -> None:
    old_id = await _create_memory(logged_in, "用户住在北京")
    response = await logged_in.client.post(
        f"/v1/memory/{old_id}/supersede",
        json={"text": "用户住在上海", "reason": "用户纠正"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    new_id = body["memory_id"]
    assert new_id != old_id
    assert body["supersedes"] == old_id

    async with session_scope() as session:
        old = await session.get(MemoryRow, old_id)
        new = await session.get(MemoryRow, new_id)
    assert old is not None and new is not None
    assert old.retracted is True
    assert old.superseded_by == new_id
    assert old.retract_reason == "用户纠正"
    assert new.supersedes == old_id
    assert new.text == "用户住在上海"
    assert new.subject_id == logged_in.subject_id

    # 默认列表只剩纠正后的新条；旧条要 include=all 才看得到
    default_ids = {
        item["memory_id"] for item in (await logged_in.client.get("/v1/memory")).json()["memories"]
    }
    assert default_ids == {new_id}
    all_ids = {
        item["memory_id"]
        for item in (await logged_in.client.get("/v1/memory?include=all")).json()["memories"]
    }
    assert all_ids == {old_id, new_id}


async def test_supersede_other_subject_is_404(logged_in: TestEnv) -> None:
    other = await _other_subject()
    memory_id = await _seed_memory_for(other, "别人的记忆")

    response = await logged_in.client.post(
        f"/v1/memory/{memory_id}/supersede",
        json={"text": "改写别人的记忆"},
    )
    assert response.status_code == 404, response.text
    async with session_scope() as session:
        row = await session.get(MemoryRow, memory_id)
    assert row is not None
    assert row.text == "别人的记忆"
    assert row.retracted is False


async def test_supersede_unknown_id_is_404(logged_in: TestEnv) -> None:
    response = await logged_in.client.post("/v1/memory/mem_nope/supersede", json={"text": "x"})
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("payload", [{"text": ""}, {"text": "x" * 4001}, {}])
async def test_supersede_invalid_text_uses_error_shape(logged_in: TestEnv, payload: dict) -> None:
    """supersede 的 text 也只有 pydantic 拦（此前是 FastAPI 默认形状）。"""
    memory_id = await _create_memory(logged_in, "待纠正")
    response = await logged_in.client.post(f"/v1/memory/{memory_id}/supersede", json=payload)
    _assert_pydantic_error_shape(response, "text")


# ------------------------------------------------------------------ forget


async def test_forget_only_clears_own_and_returns_affected_count(
    logged_in: TestEnv,
) -> None:
    """forget 此前端点层零测试：只清本人、返回影响行数、清完默认查询为空。"""
    first = await _create_memory(logged_in, "自己的记忆一")
    second = await _create_memory(logged_in, "自己的记忆二")
    put = await logged_in.client.put("/v1/core-memory", json={"label": "称呼", "text": "叫他阿澄"})
    assert put.status_code == 200, put.text

    other = await _other_subject()
    theirs = await _seed_memory_for(other, "别人的记忆")
    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=other, label="别人的边界", text="不要提起"
        )

    response = await logged_in.client.post("/v1/memory/forget")
    assert response.status_code == 200, response.text
    # 2 条 MemoryRow + 1 条 CoreMemoryRow；没有 episode/awareness。
    assert response.json() == {"retracted": 3}

    async with session_scope() as session:
        rows = {
            memory_id: await session.get(MemoryRow, memory_id)
            for memory_id in (first, second, theirs)
        }
        own_core = list(
            (
                await session.execute(
                    select(CoreMemoryRow).where(CoreMemoryRow.subject_id == logged_in.subject_id)
                )
            ).scalars()
        )
        other_core = list(
            (
                await session.execute(
                    select(CoreMemoryRow).where(CoreMemoryRow.subject_id == other)
                )
            ).scalars()
        )
    assert rows[first] is not None and rows[first].retracted is True
    assert rows[second] is not None and rows[second].retracted is True
    assert rows[theirs] is not None and rows[theirs].retracted is False
    assert own_core and all(row.active is False for row in own_core)
    assert other_core and all(row.active is True for row in other_core)

    # 默认列表为空；include=all 仍能看到墓碑（不物理删除）
    default_body = (await logged_in.client.get("/v1/memory")).json()
    assert default_body["memories"] == []
    assert default_body["total"] == 0
    all_body = (await logged_in.client.get("/v1/memory?include=all")).json()
    assert {i["memory_id"] for i in all_body["memories"]} == {first, second}
    assert all(i["retracted"] for i in all_body["memories"])

    core_body = (await logged_in.client.get("/v1/core-memory")).json()
    assert core_body["items"] == []


async def test_forget_covers_four_object_kinds_and_only_own(logged_in: TestEnv) -> None:
    """forget 返回的影响行数必须覆盖 Memory/Episode/Core/Awareness 四类之和。"""
    memory_id = await _create_memory(logged_in, "自己的记忆")
    episode_id = await _seed_episode(logged_in.subject_id, "自己的情节")
    core = await logged_in.client.put("/v1/core-memory", json={"label": "称呼", "text": "叫他阿澄"})
    assert core.status_code == 200, core.text
    core_id = core.json()["core_id"]
    awareness_id = await _seed_awareness(logged_in.subject_id, "自己的意识")

    other = await _other_subject()
    their_memory = await _seed_memory_for(other, "别人的记忆")
    their_episode = await _seed_episode(other, "别人的情节")
    their_awareness = await _seed_awareness(other, "别人的意识")
    async with session_scope() as session:
        await CoreMemoryService(session).upsert(
            subject_id=other, label="别人的边界", text="不要提起"
        )

    response = await logged_in.client.post("/v1/memory/forget")
    assert response.status_code == 200, response.text
    # 1 MemoryRow + 1 Episode + 1 CoreMemoryRow + 1 AwarenessItemRow
    assert response.json() == {"retracted": 4}

    async with session_scope() as session:
        own_memory = await session.get(MemoryRow, memory_id)
        own_episode = await session.get(MemoryEpisode, episode_id)
        own_core = await session.get(CoreMemoryRow, core_id)
        own_awareness = await session.get(AwarenessItemRow, awareness_id)
        other_memory = await session.get(MemoryRow, their_memory)
        other_episode = await session.get(MemoryEpisode, their_episode)
        other_awareness = await session.get(AwarenessItemRow, their_awareness)
        other_core_rows = list(
            (
                await session.execute(
                    select(CoreMemoryRow).where(CoreMemoryRow.subject_id == other)
                )
            ).scalars()
        )

    assert own_memory is not None and own_memory.retracted is True
    assert own_episode is not None and own_episode.archived_at is not None
    assert own_core is not None and own_core.active is False
    assert own_awareness is not None and own_awareness.invalidated_at is not None

    # 只清本人：另一 subject 的四类对象都不动。
    assert other_memory is not None and other_memory.retracted is False
    assert other_episode is not None and other_episode.archived_at is None
    assert other_awareness is not None and other_awareness.invalidated_at is None
    assert other_core_rows and all(row.active is True for row in other_core_rows)


# ------------------------------------------------------------------ core-memory


async def test_core_memory_list_has_timestamps(logged_in: TestEnv) -> None:
    put = await logged_in.client.put("/v1/core-memory", json={"label": "称呼", "text": "叫他阿澄"})
    assert put.status_code == 200, put.text
    body = (await logged_in.client.get("/v1/core-memory")).json()
    item = next(i for i in body["items"] if i["label"] == "称呼")
    assert item["created_at"] is not None
    assert item["updated_at"] is not None
    assert datetime.fromisoformat(item["created_at"]).tzinfo is not None
    assert datetime.fromisoformat(item["updated_at"]).tzinfo is not None


async def test_core_memory_timestamps_match_database(logged_in: TestEnv) -> None:
    """时间戳必须与 `core_memories` 表逐字相等，不能是"当前时间"占位。"""
    put = await logged_in.client.put("/v1/core-memory", json={"label": "称呼", "text": "叫他阿澄"})
    assert put.status_code == 200, put.text
    core_id = put.json()["core_id"]

    async with session_scope() as session:
        row = await session.get(CoreMemoryRow, core_id)
    assert row is not None

    body = (await logged_in.client.get("/v1/core-memory")).json()
    item = next(i for i in body["items"] if i["core_id"] == core_id)
    assert item["created_at"] == row.created_at.isoformat()
    assert item["updated_at"] == row.updated_at.isoformat()


async def test_core_memory_list_hard_limits_to_max(logged_in: TestEnv) -> None:
    """`limit` 回显 20 就必须真的只返回 20；历史/并发数据超限时不能多给。"""
    async with session_scope() as session:
        for index in range(MAX_PER_SUBJECT + 1):
            session.add(
                CoreMemoryRow(
                    id=f"core_seed_{index:02d}",
                    subject_id=logged_in.subject_id,
                    label=f"label{index:02d}",
                    text=f"内容{index}",
                    priority=100 + index,
                    scope="private",
                    active=True,
                )
            )

    body = (await logged_in.client.get("/v1/core-memory")).json()
    assert body["limit"] == MAX_PER_SUBJECT
    returned = [item["core_id"] for item in body["items"]]
    assert len(returned) == MAX_PER_SUBJECT
    # priority ASC 的前 20 条；第 21 条（priority 最大）必须被硬上限截掉。
    assert returned == [f"core_seed_{index:02d}" for index in range(MAX_PER_SUBJECT)]


@pytest.mark.parametrize("scope", ["nonsense", "", "PUBLIC"])
async def test_core_memory_invalid_scope_uses_error_shape(logged_in: TestEnv, scope: str) -> None:
    """scope 由 pydantic 的 pattern 拦住（OpenAPI 也写着同一个 pattern）。"""
    response = await logged_in.client.put(
        "/v1/core-memory",
        json={"label": "边界", "text": "不要提工作", "scope": scope},
    )
    _assert_pydantic_error_shape(response, "scope")


@pytest.mark.parametrize("priority", [0, 1000])
async def test_core_memory_priority_range_uses_error_shape(
    logged_in: TestEnv, priority: int
) -> None:
    """priority 的 1..999 只在 pydantic 里（OpenAPI minimum/maximum），端点不重复校验。"""
    response = await logged_in.client.put(
        "/v1/core-memory",
        json={"label": "边界", "text": "不要提工作", "priority": priority},
    )
    _assert_pydantic_error_shape(response, "priority")


async def test_core_memory_put_returns_full_record(logged_in: TestEnv) -> None:
    """PUT 响应直接带 priority/scope/时间戳：写完不必再 GET 一次。"""
    response = await logged_in.client.put(
        "/v1/core-memory",
        json={"label": "称呼", "text": "叫他阿澄", "priority": 7, "scope": "group"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["priority"] == 7
    assert body["scope"] == "group"
    assert body["label"] == "称呼"

    async with session_scope() as session:
        row = await session.get(CoreMemoryRow, body["core_id"])
    assert row is not None
    assert body["created_at"] == row.created_at.isoformat()
    assert body["updated_at"] == row.updated_at.isoformat()
    # 与 GET 列表的字段完全一致（同一构造出口）
    listed = next(
        i
        for i in (await logged_in.client.get("/v1/core-memory")).json()["items"]
        if i["core_id"] == body["core_id"]
    )
    assert listed == body


async def test_core_memory_put_overwrites_same_label(logged_in: TestEnv) -> None:
    first = await logged_in.client.put(
        "/v1/core-memory", json={"label": "边界", "text": "不要提工作"}
    )
    assert first.status_code == 200, first.text
    second = await logged_in.client.put(
        "/v1/core-memory",
        json={"label": "边界", "text": "不要提工作，也不要提加班", "priority": 10},
    )
    assert second.status_code == 200, second.text

    body = (await logged_in.client.get("/v1/core-memory")).json()
    matching = [i for i in body["items"] if i["label"] == "边界"]
    assert len(matching) == 1
    assert matching[0]["text"] == "不要提工作，也不要提加班"
    assert matching[0]["priority"] == 10
    assert matching[0]["core_id"] == first.json()["core_id"]


async def test_core_memory_put_capacity_is_422(logged_in: TestEnv) -> None:
    for index in range(MAX_PER_SUBJECT):
        response = await logged_in.client.put(
            "/v1/core-memory",
            json={"label": f"l{index}", "text": f"内容{index}"},
        )
        assert response.status_code == 200, response.text

    overflow = await logged_in.client.put(
        "/v1/core-memory", json={"label": "overflow", "text": "超了"}
    )
    _assert_error_shape(overflow)
    assert "上限" in overflow.json()["error"]["message"]


async def test_core_memory_delete_is_soft_and_hides_from_list(logged_in: TestEnv) -> None:
    put = await logged_in.client.put(
        "/v1/core-memory", json={"label": "临时", "text": "这条待会删掉"}
    )
    core_id = put.json()["core_id"]

    deleted = await logged_in.client.delete(f"/v1/core-memory/{core_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"core_id": core_id, "active": False}

    body = (await logged_in.client.get("/v1/core-memory")).json()
    assert all(i["core_id"] != core_id for i in body["items"])

    async with session_scope() as session:
        row = await session.get(CoreMemoryRow, core_id)
    assert row is not None
    assert row.active is False


async def test_core_memory_delete_other_subject_is_404(logged_in: TestEnv) -> None:
    other = await _other_subject()
    async with session_scope() as session:
        view = await CoreMemoryService(session).upsert(
            subject_id=other, label="别人的", text="别人的核心记忆"
        )

    response = await logged_in.client.delete(f"/v1/core-memory/{view.core_id}")
    assert response.status_code == 404, response.text
    async with session_scope() as session:
        row = await session.get(CoreMemoryRow, view.core_id)
    assert row is not None and row.active is True


async def test_core_memory_delete_unknown_is_404(logged_in: TestEnv) -> None:
    response = await logged_in.client.delete("/v1/core-memory/core_nope")
    assert response.status_code == 404, response.text
