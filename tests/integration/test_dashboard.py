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


async def test_connectivity_separates_configured_from_proactive(
    logged_in, monkeypatch
) -> None:
    """「凭据齐全」和「能主动推送」是两件事，不能混成一个 configured。

    实测踩过：`qqbot_connector_status()` 在"有凭据但没申请主动推送"时返回
    (None, 原因)，仪表盘拿它判断 configured，于是把配好的渠道显示成
    "未配置"——用户会去翻凭据，而真正的问题是平台权限。
    """

    class _Cfg:
        configured = True

    monkeypatch.setattr(
        "ling.capabilities.channels.qqbot_config", lambda: _Cfg()
    )
    monkeypatch.setattr(
        "ling.capabilities.channels.qqbot_connector_status",
        lambda: (None, "主动推送需在开放平台单独申请"),
    )

    body = (await logged_in.client.get("/v1/dashboard")).json()
    data = body["sections"]["connectivity"]["data"]
    qq = next(c for c in data["channels"] if c["channel"] == "qqbot")
    assert qq["configured"] is True, qq
    assert qq["proactive_ready"] is False, qq
    assert "主动推送" in qq["proactive_reason"]
    # 关键：打桩 qqbot 的配置**不能影响**模型绑定那一段。
    # 曾经这里因为局部变量 `config` 遮蔽了外层的 ConfigDep，导致读模型绑定
    # 时调到 QQBotConfig.effective() → AttributeError 被吞 → 显示"未绑定"。
    assert "bound_model_roles" in data
    assert isinstance(data["bound_model_roles"], list)
    # 测试环境有 bootstrap 绑定，所以这里必须非空；
    # 一旦被遮蔽成 AttributeError → 空列表，这条会红
    assert data["bound_model_roles"], "模型绑定被读成空——检查 config 变量是否被遮蔽"


async def test_failed_section_does_not_poison_the_rest(logged_in, monkeypatch) -> None:
    """一块查询失败后，其它块照常返回（整页不变成错误页）。"""
    from ling.domains.memory.service import MemoryService

    async def boom(self, *_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("模拟数据库错误")

    monkeypatch.setattr(MemoryService, "list_memories", boom)

    response = await logged_in.client.get("/v1/dashboard")
    assert response.status_code == 200, response.text
    sections = response.json()["sections"]
    assert sections["memories"]["status"] == "unavailable"
    assert sections["relationship"]["status"] == "ok"
    assert sections["connectivity"]["status"] == "ok"


async def test_failed_section_rolls_back_the_shared_session() -> None:
    """失败后必须回滚：所有块共用同一个请求级 session。

    一条失败的查询会让 session 进入"需要回滚"状态，后面每一块都会跟着报错。
    实测在部署实例上就是这个现象：记忆那块失败，把模型绑定也读成空——
    界面上看着"没配模型"，其实模型是好的。**一块坏、全员背锅**的假象
    比直接报错难查得多，所以这里直接断言回滚被调用。
    """
    from ling.api.routes.dashboard import SECTION_UNAVAILABLE, _guard

    class _Session:
        def __init__(self) -> None:
            self.rollbacks = 0

        async def rollback(self) -> None:
            self.rollbacks += 1

    session = _Session()

    async def boom():  # noqa: ANN202
        raise RuntimeError("模拟查询失败")

    result = await _guard("probe", boom(), session)
    assert result["status"] == SECTION_UNAVAILABLE
    assert session.rollbacks == 1, "失败片段没有回滚共享 session"


async def test_rollback_failure_does_not_raise() -> None:
    """回滚本身再失败，也不能把整页拖垮。"""
    from ling.api.routes.dashboard import SECTION_UNAVAILABLE, _guard

    class _BadSession:
        async def rollback(self) -> None:
            raise RuntimeError("回滚也炸了")

    async def boom():  # noqa: ANN202
        raise RuntimeError("查询失败")

    result = await _guard("probe", boom(), _BadSession())
    assert result["status"] == SECTION_UNAVAILABLE
