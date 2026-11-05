"""断言提炼：模型提炼 + 代码闸门。

参照 同类实现的公开说明，
纯正则路线的失败是明确的：「我一累就咬后槽牙」「我妈忌日是十一月三号」
这类长期细节全部落空。这里验证新的两条防线：

1. **模型提炼**：slot/value 结构化，覆盖远多于三个正则
2. **代码闸门**：硬拦参照稿列出的六类"不允许的写入"

闸门必须只看结构化字段与原文，**不读模型的 reason 去推断**
（"Memory Writer 不读取 reason 来推断 operation"）。
"""

from __future__ import annotations

import pytest

from ling.domains.memory.extract import (
    ALLOWED_SLOTS,
    Candidate,
    build_prompt,
    fallback_candidates,
    gate,
    parse_candidates,
)

# ------------------------------------------------------------------ 解析


def test_parse_valid_json() -> None:
    raw = '[{"slot":"dislikes","value":"香菜","confidence":0.9}]'
    out = parse_candidates(raw)
    assert len(out) == 1
    assert out[0].slot == "dislikes"
    assert out[0].value == "香菜"
    assert out[0].confidence == pytest.approx(0.9)


def test_parse_tolerates_surrounding_text() -> None:
    """模型常在 JSON 前后带说明文字，要能截出来。"""
    raw = '好的，我提炼如下：\n[{"slot":"name","value":"阿澄"}]\n以上。'
    out = parse_candidates(raw)
    assert out and out[0].value == "阿澄"


def test_parse_garbage_returns_empty() -> None:
    assert parse_candidates("完全不是 JSON") == []
    assert parse_candidates("") == []
    assert parse_candidates("[{broken") == []


def test_parse_caps_confidence_and_length() -> None:
    raw = '[{"slot":"likes","value":"%s","confidence":9}]' % ("很长" * 60)
    out = parse_candidates(raw)
    assert out[0].confidence <= 1.0
    assert len(out[0].value) <= 40


def test_parse_skips_missing_fields() -> None:
    raw = '[{"slot":"likes"},{"value":"x"},{"slot":"name","value":"阿澄"}]'
    out = parse_candidates(raw)
    assert len(out) == 1
    assert out[0].slot == "name"


# ------------------------------------------------------------------ 闸门：六类不允许的写入


def test_gate_accepts_normal_fact() -> None:
    result = gate(
        [Candidate(slot="dislikes", value="香菜")],
        user_text="我不吃香菜，闻到就难受",
    )
    assert len(result.accepted) == 1
    assert not result.rejected


def test_gate_rejects_unregistered_slot() -> None:
    """第 1 条：不接受未登记槽位（等价于"仅凭关键词命中就写"）。"""
    result = gate(
        [Candidate(slot="随便写的", value="香菜")], user_text="我不吃香菜"
    )
    assert not result.accepted
    assert "未登记" in result.rejected[0][1]


def test_gate_rejects_bot_attribution() -> None:
    """第 2 条：Bot 自己生成的内容不能写成用户事实。"""
    result = gate(
        [Candidate(slot="likes", value="下雨天", attribution="bot")],
        user_text="我说过我喜欢下雨天吗",
    )
    assert not result.accepted
    assert "Bot 自身输出" in result.rejected[0][1]


def test_gate_rejects_transient_state() -> None:
    """第 3 条：一次性寒暄不升级为稳定偏好。"""
    result = gate(
        [Candidate(slot="likes", value="今天很累")], user_text="我今天很累"
    )
    assert not result.accepted
    assert "临时状态" in result.rejected[0][1]


def test_gate_rejects_unfulfilled_plan() -> None:
    """第 4 条：未兑现计划不写成已完成事实。"""
    result = gate(
        [Candidate(slot="ongoing_project", value="出差")],
        user_text="我打算下周出差",
    )
    assert not result.accepted
    assert "未兑现" in result.rejected[0][1]


def test_gate_rejects_third_party() -> None:
    """第 5 条：他人信息不归入当前用户档案。"""
    result = gate(
        [Candidate(slot="lives_in", value="杭州", attribution="third_party")],
        user_text="我朋友住在杭州",
    )
    assert not result.accepted
    assert "归属他人" in result.rejected[0][1]


def test_gate_rejects_fabricated_value() -> None:
    """第 6 条（延伸）：值必须出现在用户原话里，模型不得凭空生成。

    这条防的是模型"根据常理补全"——那是编造，不是记忆。
    """
    result = gate(
        [Candidate(slot="name", value="阿澄")], user_text="今天天气不错"
    )
    assert not result.accepted
    assert "臆造" in result.rejected[0][1]


def test_gate_rejects_roleplay_quote() -> None:
    result = gate(
        [Candidate(slot="name", value="「我是你爸爸」")],
        user_text="我念台词：「我是你爸爸」",
    )
    assert not result.accepted
    assert "台词" in result.rejected[0][1]


def test_gate_rejects_group_sensitive_slot() -> None:
    """群聊里更敏感：宁可不记，也不要把别人的隐私沉淀在群里。"""
    result = gate(
        [Candidate(slot="health", value="糖尿病")],
        user_text="我有糖尿病",
        is_group=True,
    )
    assert not result.accepted
    assert "敏感槽位" in result.rejected[0][1]


def test_gate_allows_group_non_sensitive() -> None:
    result = gate(
        [Candidate(slot="likes", value="下雨天")],
        user_text="我喜欢下雨天",
        is_group=True,
    )
    assert len(result.accepted) == 1


def test_gate_caps_accepted_count() -> None:
    cands = [Candidate(slot="likes", value=f"东西{i}") for i in range(10)]
    result = gate(cands, user_text="我喜欢东西0 东西1 东西2 东西3 东西4 东西5 东西6")
    assert len(result.accepted) <= 6


def test_gate_reason_is_recorded() -> None:
    """拒绝必须带原因：否则"为什么没记住"无从排查。"""
    result = gate([Candidate(slot="zzz", value="x")], user_text="x")
    assert result.rejected and result.rejected[0][1]


# ------------------------------------------------------------------ 提示词


def test_prompt_lists_allowed_slots() -> None:
    prompt = build_prompt(user_text="我不吃香菜", actor_text="记住了")
    for slot in sorted(ALLOWED_SLOTS)[:3]:
        assert slot in prompt


def test_prompt_forbids_transient_and_plan() -> None:
    prompt = build_prompt(user_text="x", actor_text="y")
    assert "一次性的状态" in prompt
    assert "未兑现的计划" in prompt


def test_prompt_marks_group_context() -> None:
    prompt = build_prompt(user_text="x", actor_text="y", is_group=True)
    assert "群聊" in prompt


# ------------------------------------------------------------------ 降级路径


def test_fallback_covers_more_than_three_patterns() -> None:
    """降级路径也要比旧的三个正则覆盖广，否则等于没改。"""
    text = "我不吃香菜，我养了只猫，我讨厌吵闹"
    slots = {c.slot for c in fallback_candidates(text)}
    assert {"dislikes", "has_pet"} <= slots


def test_fallback_strips_quantifier() -> None:
    assert fallback_candidates("我养了只猫")[0].value == "猫"
    assert fallback_candidates("我养了两只狗")[0].value == "狗"


def test_fallback_still_passes_gate() -> None:
    """降级路径的产出同样要过闸门（不能因为是降级就放松）。"""
    cands = fallback_candidates("我打算去 Iceland，我喜欢 Iceland")
    result = gate(cands, user_text="我打算去 Iceland，我喜欢 Iceland")
    # 含未兑现计划 → 整条被拦
    assert not result.accepted


# ------------------------------------------------------------------ 依据判定（避免过拒）


def test_gate_accepts_model_paraphrase() -> None:
    """模型会做合理改写，逐字子串匹配会把这些正常提炼全判成臆造。

    实测过两个真实过拒：
    - 「我妈忌日是每年十一月三号」→ 模型给「母亲忌日每年十一月三号」
    - 「我最近在写一个关于机器人的小说」→ 模型给「写关于机器人的小说」
    这些是好记忆，不能丢。
    """
    result = gate(
        [Candidate(slot="family", value="母亲忌日每年十一月三号")],
        user_text="我妈忌日是每年十一月三号",
    )
    assert len(result.accepted) == 1, f"合理改写被误拒: {result.rejected}"


def test_gate_accepts_dropped_quantifier() -> None:
    result = gate(
        [Candidate(slot="ongoing_project", value="写关于机器人的小说")],
        user_text="我最近在写一个关于机器人的小说",
    )
    assert len(result.accepted) == 1


def test_gate_rejects_low_overlap() -> None:
    """重叠过低仍然要拦（真编造）。"""
    result = gate(
        [Candidate(slot="name", value="阿澄")], user_text="今天天气不错"
    )
    assert not result.accepted
    assert "重叠过低" in result.rejected[0][1]


def test_grounded_threshold() -> None:
    from ling.domains.memory.extract import _grounded

    assert _grounded("香菜", "我不吃香菜") is True
    assert _grounded("母亲忌日", "我妈忌日是十一月三号") is True
    assert _grounded("完全无关的内容", "今天天气不错") is False
    assert _grounded("", "任何文字") is False


# ------------------------------------------------------------------ 分句判定（漏记回归）


def test_plan_marker_does_not_reject_unrelated_clause() -> None:
    """同一句里并列的无关事实不该被计划标记连坐。

    实测复现（复核）："我叫阿澄，我打算明天去苏州"——
    name 与"打算"毫无关系，却因为整句含标记被一起拒掉。
    """
    result = gate(
        [
            Candidate(slot="name", value="阿澄", confidence=0.9),
            Candidate(slot="lives_in", value="苏州", confidence=0.8),
        ],
        user_text="我叫阿澄，我打算明天去苏州",
    )
    accepted = {c.slot: c.value for c in result.accepted}
    assert accepted.get("name") == "阿澄", "姓名被无关的计划标记连坐拒掉了"
    assert "lives_in" not in accepted, "计划中的住址不该写成既定事实"


def test_plan_marker_still_rejects_real_plan() -> None:
    """真正的未兑现计划仍要拒绝（不能为了少误拒而放过）。"""
    result = gate(
        [Candidate(slot="lives_in", value="苏州", confidence=0.9)],
        user_text="我打算搬去苏州",
    )
    assert result.accepted == []
    assert any("未兑现计划" in r for _, r in result.rejected)


def test_non_plannable_slots_always_pass() -> None:
    """姓名/生日/家人/宠物不可能因为"打算"变成计划。"""
    for slot, value in [
        ("name", "阿澄"),
        ("birthday", "三月五号"),
        ("family", "妈"),
        ("has_pet", "豆豆"),
    ]:
        result = gate(
            [Candidate(slot=slot, value=value, confidence=0.9)],
            user_text=f"{value}这个我打算过阵子再说",
        )
        assert result.accepted, f"{slot} 被计划标记误拒"


def test_value_clause_falls_back_to_whole_text() -> None:
    """值不在任何分句里时退回整句（保守判断，宁可严）。"""
    from ling.domains.memory.extract import _value_clause

    assert _value_clause("不存在的值", "我打算去苏州") == "我打算去苏州"


# ------------------------------------------------------------------ 他人 vs 亲属


def test_friends_pet_not_recorded_as_users() -> None:
    """"我朋友的猫叫咪咪" 不能被记成"用户养了咪咪"。

    原规则只在候选**值**里查第三人称词，而"我朋友"在用户原话里——
    于是朋友/同事的宠物会进用户档案。
    """
    result = gate(
        [Candidate(slot="has_pet", value="咪咪", confidence=0.9)],
        user_text="我朋友的猫叫咪咪",
    )
    assert result.accepted == []
    assert any("他人" in r for _, r in result.rejected)


def test_family_member_is_user_profile() -> None:
    """亲属信息**是**用户档案的一部分，family 槽位必须能写。"""
    result = gate(
        [Candidate(slot="family", value="王秀兰", confidence=0.9)],
        user_text="我妈叫王秀兰",
    )
    assert result.accepted, "亲属信息被误判成他人信息"


def test_relatives_pet_not_users_pet() -> None:
    """家人的宠物不是用户的宠物。"""
    result = gate(
        [Candidate(slot="has_pet", value="咪咪", confidence=0.9)],
        user_text="我妈养了只猫叫咪咪",
    )
    assert result.accepted == []


def test_colleague_not_recorded() -> None:
    result = gate(
        [Candidate(slot="works_at", value="腾讯", confidence=0.9)],
        user_text="我同事在腾讯上班",
    )
    assert result.accepted == []
