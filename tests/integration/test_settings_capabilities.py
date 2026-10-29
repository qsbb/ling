"""设置页要说清「没绑模型会失去什么」。

系统不会因为没绑模型而报错——它会走**降级路径**。看起来一切正常，
只是能力悄悄变窄：

- `memory.extract` 没绑 → 提炼退回正则表，只能抓「我叫/我喜欢/我住在」
  三个句式，"下周三去成都出差"这种自由文本事实完全落空
- `perception.vision` 没绑 → 看不到图片内容，媒体描述始终为空

用户看到的只是"她好像记不住事"，无从判断是没配模型。所以必须明说。
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


async def test_unbound_roles_report_what_degrades(logged_in: TestEnv) -> None:  # noqa: ARG001
    """未绑定职责必须说清会失去什么。"""
    body = (await logged_in.client.get("/v1/settings/config")).json()
    degradations = body["unbound_degradations"]
    # 测试环境只绑了 dialogue.*，所以记忆提炼必然在降级名单里
    assert "memory.extract" in degradations, (
        f"没提示记忆提炼在降级路径上：{list(degradations)}"
    )
    # 措辞必须准确：它是"改用主对话模型代跑"，不是"退回正则表"。
    # 我一开始写成后者，那是错的——代码用的是 dialogue.fast，模型路径一直有效。
    assert "主对话模型" in degradations["memory.extract"], degradations["memory.extract"]


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
