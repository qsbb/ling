"""管理端点必须全部鉴权——审计发现的真实未授权访问缺陷。

写工具测试时发现 `GET /v1/tools` 没有鉴权，顺手审计 manage.py 全部端点，
发现 11 个缺失（含 POST /activity/transition、POST /goals、POST /knowledge
这些**写操作**）。未登录就能改数据是真实风险。

本测试逐个验证：未登录一律 401。新增端点时若忘了加 CurrentAuth 会立刻失败。
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


# 覆盖 manage.py 的全部端点。新增端点时应补进这个列表。
MANAGE_ENDPOINTS: list[tuple[str, str]] = [
    ("GET", "/v1/tools"),
    ("GET", "/v1/memory"),
    ("GET", "/v1/relationship"),
    ("GET", "/v1/affect"),
    ("GET", "/v1/world/places"),
    ("GET", "/v1/activity"),
    ("GET", "/v1/goals"),
    ("GET", "/v1/commitments"),
    ("GET", "/v1/proactive"),
    ("GET", "/v1/consents"),
    ("GET", "/v1/knowledge"),
    ("GET", "/v1/handoff"),
    ("GET", "/v1/speech"),
    ("GET", "/v1/channels"),
    ("GET", "/v1/isolation"),
    ("GET", "/v1/awareness"),
]


@pytest.mark.parametrize(("method", "path"), MANAGE_ENDPOINTS)
async def test_manage_endpoint_requires_auth(
    web_env: TestEnv, method: str, path: str
) -> None:
    handler = getattr(web_env.client, method.lower())
    response = await handler(path)
    assert response.status_code == 401, (
        f"{method} {path} 未要求鉴权（返回 {response.status_code}）"
    )
    assert response.json()["error"]["code"] == "unauthorized"


async def test_write_endpoints_require_auth(web_env: TestEnv) -> None:
    """写操作尤其不能漏：未登录就能改数据是严重缺陷。"""
    for path, payload in (
        ("/v1/tools/diagnostics.echo/call", {"args": {}}),
        ("/v1/activity/act_x/transition", {"to_state": "running"}),
        ("/v1/goals", {"title": "注入"}),
        ("/v1/knowledge", {"topic": "t", "claim": "c"}),
        ("/v1/handoff", {"source_window_id": "w"}),
        ("/v1/commitments/cmt_x/cancel", {"reason": "注入"}),
        ("/v1/together", {"kind": "read", "title": "注入"}),
        ("/v1/memory", {"kind": "assertion", "text": "x", "source_event_ids": ["e"]}),
    ):
        response = await web_env.client.post(path, json=payload)
        assert response.status_code == 401, f"POST {path} 未要求鉴权"


async def test_put_endpoints_require_auth(web_env: TestEnv) -> None:
    """PUT 端点同样要鉴权（consents / isolation / persona）。"""
    for path, payload in (
        ("/v1/consents", {"scope": "camera", "granted": True}),
        ("/v1/isolation", {"mode": "sealed"}),
        ("/v1/persona", {"display_name": "注入"}),
    ):
        response = await web_env.client.put(path, json=payload)
        assert response.status_code == 401, f"PUT {path} 未要求鉴权（{response.status_code}）"


async def test_authenticated_access_works(logged_in: TestEnv) -> None:
    """鉴权后正常可用（不能为了安全把功能锁死）。"""
    for path in ("/v1/tools", "/v1/awareness", "/v1/isolation", "/v1/channels"):
        response = await logged_in.client.get(path)
        assert response.status_code == 200, f"{path} → {response.status_code}"
