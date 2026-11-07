"""设置页要说清「没绑模型会失去什么」。

系统不会因为没绑模型而报错——它会走**降级路径**。看起来一切正常，
只是能力悄悄变窄：

- `perception.vision` 没绑（或它跟随的「识图」槽位没配）→ 看不到图片内容，
  媒体描述始终为空
- `memory.extract` 没配 → 提炼退回正则表，只能抓「我叫/我喜欢/我住在」
  三个句式，"下周三去成都出差"这种自由文本事实完全落空

用户看到的只是"她好像记不住事"，无从判断是没配模型。所以必须明说。

判据是**解析结果**而不是"有没有一条绑定记录"：上了默认槽位之后，一个
职责完全可以没有显式绑定却照常工作（跟随槽位），按绑定记录判会把它误报
成降级，用户于是去补一条本来不需要的绑定。
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


async def test_unbound_roles_report_what_degrades(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没有可用模型的职责必须说清会失去什么。"""
    body = (await logged_in.client.get("/v1/settings/config")).json()
    degradations = body["unbound_degradations"]
    # 测试环境的引导配置只绑对话与提炼，「识图」槽位没配 → vision 必然在
    # 降级名单里。
    assert "perception.vision" in degradations, (
        f"没提示看图片在降级路径上：{list(degradations)}"
    )
    assert "图片" in degradations["perception.vision"], degradations["perception.vision"]

    # 测试环境的引导配置绑了 dialogue.* + memory.extract，所以它们不该报降级；
    # 没绑的那些才报（这条断言防止"降级提示永远挂着"变成噪音）。
    assert "dialogue.fast" not in degradations
    assert "memory.extract" not in degradations


def test_memory_extract_degradation_says_regex_not_borrowed_model() -> None:
    """提炼的降级说明**必须写成"退回正则"**，不能说"改用主对话模型代跑"。

    那条跨角色回退已经删掉了（见 `EffectiveConfig.resolve_profile`：只回退
    一层，不允许套娃）。措辞与代码不一致时，用户会照着提示去"检查主对话
    模型"，而那里根本没救——他要做的是配「创作」槽位或显式绑定提炼。
    """
    from ling.settings.service import PROFILE_DEGRADATIONS

    text = PROFILE_DEGRADATIONS["memory.extract"]
    assert "正则" in text, text
    assert "主对话模型" not in text, text


async def test_bound_roles_are_not_reported_as_degraded(logged_in: TestEnv) -> None:  # noqa: ARG001
    """已绑定的职责不该出现在降级名单里——否则提示永远挂着，变成噪音。"""
    body = (await logged_in.client.get("/v1/settings/config")).json()
    bound = {p["profile"] for p in body["profiles"]}
    overlap = bound & set(body["unbound_degradations"])
    assert not overlap, f"已绑定却仍报降级：{overlap}"


async def test_degradation_text_is_actionable(logged_in: TestEnv) -> None:  # noqa: ARG001
    """降级说明要能让用户判断该做什么，不能只说"未配置"。"""
    body = (await logged_in.client.get("/v1/settings/config")).json()
    for profile, why in body["unbound_degradations"].items():
        # 中文信息密度高，「不能识别语音消息」8 个字已经说清了
        assert len(why) >= 6, f"{profile} 的说明太短，看不出后果：{why}"
        assert "未配置" not in why, f"{profile} 只说了没配置，没说失去什么：{why}"


def test_proactive_status_reports_every_channel(monkeypatch) -> None:
    """主动推送不可用时，**每个渠道的原因都要报出来**。

    实测踩过：部署实例上 qqbot 配好了（只是平台没给主动推送权限），
    而 `proactive_connector_status()` 只查 OneBot，于是界面只说
    「OneBot 连接器已禁用」。用户去配 OneBot，照样发不出去，
    真正的原因一个字都没提到。
    """
    from ling.capabilities import channels

    class _Caps:
        def __init__(self, *, enabled: bool, proactive: bool) -> None:
            self.enabled = enabled
            self.supports_proactive = proactive

    class _OneBot:
        def capabilities(self):  # noqa: ANN201
            return _Caps(enabled=False, proactive=False)

    monkeypatch.setattr(channels, "build_onebot_connector", lambda: _OneBot())
    monkeypatch.setattr(
        channels,
        "qqbot_connector_status",
        lambda: (None, "QQ 官方机器人默认只支持被动回复：主动推送需在开放平台单独申请"),
    )

    connector, reason = channels.proactive_connector_status()
    assert connector is None
    assert "OneBot" in reason
    assert "QQ 官方机器人" in reason, "qqbot 的原因被吞了"
    assert "开放平台" in reason



async def test_proactive_gate_reads_published_quiet_hours(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """免打扰窗必须来自**已发布配置**，不能是服务内部的默认值。

    现象：面板把 quiet 改成 0-0（关闭）并发布后，主动状态仍报
    「处于勿扰时段（23:00-08:00）」——路由只读了 `proactive_delivery_enabled`，
    两个 quiet 字段没传下去，gate 退回默认值。设置页保存成功、实际不生效。

    用例设计：先发布一个**覆盖此刻**的窗口，断言确实报勿扰；再发布 0-0，
    断言不报。只做后者是无效的——默认值本来就是 0-0，改不改都会绿。
    """
    from dataclasses import replace
    from datetime import datetime
    from zoneinfo import ZoneInfo

    # clean env 下 `LING_USER_TIMEZONE` 未设置，route 会传 None，gate 直接
    # 只报时区原因；这样本用例测不到免打扰窗。只在**本用例**临时钉住
    # app 的 Settings（不动 conftest，也不放宽生产的「未知时区默认拒绝」），
    # 并用同一个时区计算"此刻"，保证窗口与 gate 判定一致。
    timezone = "Asia/Shanghai"
    monkeypatch.setattr(
        logged_in.app.state,
        "settings",
        replace(logged_in.settings, user_timezone=timezone),
    )

    async def _publish(payload: dict) -> None:
        draft = await logged_in.client.put("/v1/settings/draft", json=payload)
        assert draft.status_code == 200, draft.text
        published = await logged_in.client.post(
            "/v1/settings/publish", json={"revision": draft.json()["revision"]}
        )
        assert published.status_code == 200, published.text

    async def _reasons() -> str:
        body = (await logged_in.client.get("/v1/proactive")).json()
        return " ".join(body.get("gate_reasons") or [])

    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    minute = now.hour * 60 + now.minute

    # 前后各留 3 分钟余量，覆盖"此刻"；跨午夜由 _in_quiet_hours 处理
    await _publish(
        {
            "dialogue": {
                "quiet_start_minute": (minute - 3) % 1440,
                "quiet_end_minute": (minute + 3) % 1440,
            }
        }
    )
    assert "勿扰" in await _reasons(), "发布一个覆盖此刻的勿扰窗，gate 却没识别"

    await _publish({"dialogue": {"quiet_start_minute": 0, "quiet_end_minute": 0}})
    assert "勿扰" not in await _reasons(), "关闭免打扰后仍报勿扰：已发布配置没被读"
