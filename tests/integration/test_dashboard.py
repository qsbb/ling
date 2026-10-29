"""仪表盘聚合接口：分段结构、三态状态、失败隔离。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.w0_web

SECTION_KEYS = {"activity", "affect", "proactive", "memories", "relationship", "connectivity"}


async def test_dashboard_returns_all_sections_with_status(logged_in) -> None:
    """首屏要的每一块都要在，且每块自带 status / as_of。"""
    response = await logged_in.client.get("/v1/dashboard")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["schema_version"] == "v1"
    assert body["generated_at"]
    assert set(body["sections"]) == SECTION_KEYS

    for name, section in body["sections"].items():
        assert section["status"] in {"ok", "empty", "unavailable"}, (name, section)
        assert section["as_of"], name


async def test_empty_and_unavailable_are_distinguishable(logged_in) -> None:
    """空数据与查询失败必须分开。

    混在一起的话，"她此刻没在做什么"和"读不到她在做什么"长得一模一样，
    而这两件事的处理方式完全不同（前者是事实，后者是故障）。
    """
    body = (await logged_in.client.get("/v1/dashboard")).json()
    # 全新库：活动与待发候选都应该是 empty 而不是 unavailable
    assert body["sections"]["activity"]["status"] == "empty"
    assert body["sections"]["proactive"]["status"] == "empty"


async def test_one_broken_section_does_not_break_the_page(logged_in, monkeypatch) -> None:
    """某一块查询炸了，其它块照常返回，整页不变成错误页。"""
    from ling.domains.activity.service import ActivityService

    async def boom(self):  # noqa: ANN001
        raise RuntimeError("模拟数据库抖动")

    monkeypatch.setattr(ActivityService, "current", boom)

    response = await logged_in.client.get("/v1/dashboard")
    assert response.status_code == 200, response.text
    sections = response.json()["sections"]
    assert sections["activity"]["status"] == "unavailable"
    # 其它块仍然正常
    assert sections["affect"]["status"] in {"ok", "empty"}


async def test_connectivity_reports_unbound_models(logged_in) -> None:
    """没绑定模型职责时，首屏必须看得出来——那是"她说不出话"的直接原因。"""
    body = (await logged_in.client.get("/v1/dashboard")).json()
    data = body["sections"]["connectivity"]["data"]
    assert "bound_model_roles" in data
    assert isinstance(data["channels"], list)
    for ch in data["channels"]:
        assert set(ch) >= {"channel", "configured", "reason"}
