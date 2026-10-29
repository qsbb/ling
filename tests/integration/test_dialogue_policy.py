"""⑤ 对话与主动性 的设置面：门槛从设置写进决策与主动服务。

两组参数分别对应「要不要接话」和「要不要主动找你」：

- `group_turn_budget`：群聊里角色近期已发言几次就不再插话
- `proactive_daily_limit` / `quiet_*_minute`：每天最多主动几次、什么时段闭嘴

默认值 = 原常量，不配置时行为完全不变。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ling.api.routes.chat import _group_turn_budget
from ling.domains.proactive.service import ProactivityService
from ling.infra.db import session_scope
from ling.kernel.decision import DecisionContext, DecisionEngine
from ling.ports.config import DialoguePolicy
from ling.settings.service import _dialogue_policy
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"

DEFAULTS = {
    "group_turn_budget": 3,
    "proactive_daily_limit": 2,
    "quiet_start_minute": 1380,
    "quiet_end_minute": 480,
    # 自主度：私聊可沉默（用户明确要求），群里被 @ 仍必须回
    "private_may_stay_silent": True,
    "group_mention_requires_reply": True,
    # 休息时可以先不进入决策（"没空回"）
    "rest_may_skip": True,
    "group_attention_deferred": True,
    "proactive_delivery_enabled": False,
}


async def _publish(env: TestEnv, payload: dict) -> dict:
    draft = await env.client.put("/v1/settings/draft", json=payload)
    assert draft.status_code == 200, draft.text
    revision = draft.json()["revision"]
    published = await env.client.post("/v1/settings/publish", json={"revision": revision})
    assert published.status_code == 200, published.text
    return (await env.client.get("/v1/settings/config")).json()


# ---------------------------------------------------------------- 默认与往返


async def test_dialogue_defaults_when_unset(logged_in: TestEnv) -> None:
    body = (await logged_in.client.get("/v1/settings/config")).json()
    assert body["dialogue"] == DEFAULTS


async def test_dialogue_roundtrip(logged_in: TestEnv) -> None:
    body = await _publish(
        logged_in,
        {
            "dialogue": {
                "group_turn_budget": 5,
                "proactive_daily_limit": 1,
                "quiet_start_minute": 1320,  # 22:00
                "quiet_end_minute": 420,  # 07:00
            }
        },
    )
    assert body["dialogue"] == {
        "group_turn_budget": 5,
        "proactive_daily_limit": 1,
        "quiet_start_minute": 1320,
        "quiet_end_minute": 420,
        "private_may_stay_silent": True,
        "group_mention_requires_reply": True,
        "rest_may_skip": True,
        "group_attention_deferred": True,
        "proactive_delivery_enabled": False,
    }


async def test_dialogue_partial_update_keeps_other_fields(logged_in: TestEnv) -> None:
    await _publish(
        logged_in, {"dialogue": {"group_turn_budget": 5, "proactive_daily_limit": 1}}
    )
    body = await _publish(logged_in, {"dialogue": {"group_turn_budget": 8}})

    assert body["dialogue"]["group_turn_budget"] == 8
    assert body["dialogue"]["proactive_daily_limit"] == 1, "没传的字段被重置了"


async def test_three_sections_do_not_interfere(logged_in: TestEnv) -> None:
    """④⑧⑤ 三段共用一份修订。改其中一段不该动到另两段。"""
    await _publish(
        logged_in,
        {
            "memory": {"rerank_top_n": 7},
            "life": {"max_live_plans": 2},
            "dialogue": {"group_turn_budget": 5},
        },
    )
    body = await _publish(logged_in, {"dialogue": {"proactive_daily_limit": 1}})

    assert body["memory"]["rerank_top_n"] == 7, "改对话策略时记忆策略被重置"
    assert body["life"]["max_live_plans"] == 2, "改对话策略时生活策略被重置"
    assert body["dialogue"]["group_turn_budget"] == 5


# ---------------------------------------------------------------- 校验


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("group_turn_budget", 0),
        ("group_turn_budget", 99),
        ("proactive_daily_limit", -1),
        ("proactive_daily_limit", 50),
        ("quiet_start_minute", -1),
        ("quiet_start_minute", 1440),
        ("quiet_end_minute", 2000),
    ],
)
async def test_out_of_range_is_rejected(
    logged_in: TestEnv, field: str, value: float
) -> None:
    response = await logged_in.client.put(
        "/v1/settings/draft", json={"dialogue": {field: value}}
    )
    assert response.status_code == 422, response.text


def test_dirty_value_is_clamped_on_read() -> None:
    policy = _dialogue_policy(
        {
            "group_turn_budget": 999,
            "proactive_daily_limit": -3,
            "quiet_start_minute": "oops",
            "quiet_end_minute": 5000,
        }
    )
    assert policy.group_turn_budget == 20
    assert policy.proactive_daily_limit == 0
    assert policy.quiet_start_minute == DEFAULTS["quiet_start_minute"]
    assert policy.quiet_end_minute == 1439


def test_quiet_minutes_convert_to_time() -> None:
    policy = DialoguePolicy(quiet_start_minute=1320, quiet_end_minute=420)
    assert policy.quiet_start == (22, 0)
    assert policy.quiet_end == (7, 0)


# ---------------------------------------------------------------- 真的用上了


def test_group_budget_changes_decision() -> None:
    """同一个场景，预算不同结论不同——这才证明预算真的在起作用。

    场景：不是被点名（豁免前提不成立）、但接角色的话说（本该可答）。
    预算 3 且已说 3 次 → 被硬规则挡成 silence；
    预算 5 且已说 3 次 → 进入弹性区间 reply(hard=False)。
    如果只断言"都是 silence"（因为没点名），就证明不了预算生效——
    那正是假绿的典型写法。
    """
    engine = DecisionEngine()

    def decide(budget: int):  # noqa: ANN202
        return engine.decide(
            DecisionContext(
                is_group=True,
                mentioned=False,
                addressed_to_actor=True,
                is_question=True,
                recent_actor_turns=3,
                has_meaningful_text=True,
                group_turn_budget=budget,
            )
        )

    tight = decide(3)
    loose = decide(5)
    assert tight.decision.value == "silence", tight.reason
    assert "上限" in tight.reason, tight.reason
    assert loose.decision.value == "reply", loose.reason
    assert loose.hard is False, "进入弹性区间才允许模型参与"


def test_mention_still_exempts_budget() -> None:
    """被点名时豁免频率限制——预算再小也要答。这是预算的边界。"""
    result = DecisionEngine().decide(
        DecisionContext(
            is_group=True,
            mentioned=True,
            addressed_to_actor=True,
            is_question=True,
            recent_actor_turns=99,
            has_meaningful_text=True,
            group_turn_budget=1,
        )
    )
    assert result.decision.value == "reply"
    assert result.hard is True


async def test_quiet_hours_honor_policy(logged_in: TestEnv) -> None:
    """免打扰时段真的按策略走。

    策略改成 22:00-07:00 后，22:30 应当被挡；默认 23:00-08:00 时不该挡。
    """
    async with session_scope() as session:
        wide = ProactivityService(
            session,
            actor_id=ACTOR,
            policy=DialoguePolicy(quiet_start_minute=1320, quiet_end_minute=420),
        )
        # 2026-10-28 22:30 Asia/Shanghai = 14:30 UTC
        at_2230 = datetime(2026, 10, 28, 14, 30, tzinfo=UTC)
        decision = await wide.gate(
            subject_id="subj_dialogue_test",
            now=at_2230,
            channel_can_proactive=True,
            subject_timezone="Asia/Shanghai",
        )
        assert not decision.allowed
        assert any("勿扰" in r for r in decision.reasons), decision.reasons

        # 默认策略（23:00 起）在 22:30 不该因免打扰被挡
        default = ProactivityService(session, actor_id=ACTOR)
        decision2 = await default.gate(
            subject_id="subj_dialogue_test",
            now=at_2230,
            channel_can_proactive=True,
            subject_timezone="Asia/Shanghai",
        )
        assert not any("勿扰" in r for r in decision2.reasons), decision2.reasons


async def test_daily_limit_zero_blocks_all(logged_in: TestEnv) -> None:
    """日限额设成 0 = 完全不要主动联系。"""
    async with session_scope() as session:
        off = ProactivityService(
            session, actor_id=ACTOR, policy=DialoguePolicy(proactive_daily_limit=0)
        )
        decision = await off.gate(
            subject_id="subj_dialogue_test",
            now=datetime(2026, 10, 28, 4, 0, tzinfo=UTC),  # 12:00 上海，非勿扰
            channel_can_proactive=True,
            subject_timezone="Asia/Shanghai",
        )
        assert not decision.allowed
        assert any("上限" in r for r in decision.reasons), decision.reasons


async def test_explicit_override_beats_policy(logged_in: TestEnv) -> None:
    """调用方显式传的值优先于策略——特殊场景仍可覆盖。"""
    async with session_scope() as session:
        svc = ProactivityService(
            session, actor_id=ACTOR, policy=DialoguePolicy(proactive_daily_limit=0)
        )
        decision = await svc.gate(
            subject_id="subj_dialogue_test",
            now=datetime(2026, 10, 28, 4, 0, tzinfo=UTC),
            daily_limit=5,
            channel_can_proactive=True,
            subject_timezone="Asia/Shanghai",
        )
        assert decision.allowed, decision.reasons


# ---------------------------------------------------------------- 接线回归


class _FakeConfig:
    """只实现 effective() 的最小替身。"""

    def __init__(self, budget: int) -> None:
        self._budget = budget
        self.calls = 0

    async def effective(self):  # noqa: ANN202
        from types import SimpleNamespace

        self.calls += 1
        return SimpleNamespace(
            dialogue=SimpleNamespace(group_turn_budget=self._budget)
        )


async def test_web_chat_reads_budget_from_config() -> None:
    """Web 聊天路径必须真的**读配置**得到预算。

    这条是给一个真实 bug 收尸：早先这里读 ``app.state.config_service``，
    那个属性根本不存在，于是 try/except 永远走 except 分支、永远返回默认值。
    表面上"有回退很安全"，实际是**设置从来没生效过**。

    只断言"读不到时返回默认"是不够的——那样这个 bug 依然绿。
    必须同时断言"读得到时用的是配置里的值"。
    """
    fake = _FakeConfig(7)
    assert await _group_turn_budget(fake) == 7
    assert fake.calls == 1, "根本没有去读配置"


async def test_web_chat_falls_back_when_config_broken() -> None:
    """配置读失败退回默认，不阻断对话——但这是退路，不是常态。"""
    assert await _group_turn_budget(object()) == DecisionContext.group_turn_budget


# ---------------------------------------------------------------- 沉默要能解释

# 她选择不回时，后端一直记着 handled_as / handled_reason，但**接口不返回**，
# 前端也不显示。用户看到的是一条没人理的消息，像故障。
# DESIGN 的原则是「沉默和失败都要能被解释」，这条是它的落地。


async def test_silence_reason_is_exposed_to_client(logged_in: TestEnv) -> None:
    """沉默时接口必须返回 handled_as 与理由，前端才能解释。"""

    from ling.infra.db import session_scope
    from ling.infra.models import Message

    # 直接造一条"她看见了但没回"的记录（模拟她选了沉默）
    created = await logged_in.client.post("/v1/sessions", json={"title": "沉默"})
    window_id = created.json()["session_id"]
    async with session_scope() as session:
        session.add(
            Message(
                id="msg_silence_probe",
                window_id=window_id,
                role="user",
                text="我自己去看电影了",
                sequence=1,
                state="completed",
                handled_as="ignored",
                handled_reason="你自个儿去，我没必要回话",
            )
        )

    r = await logged_in.client.get(f"/v1/sessions/{window_id}/messages")
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    row = next(m for m in msgs if m["message_id"] == "msg_silence_probe")
    assert row["handled_as"] == "ignored", "接口没暴露处置结果，前端无法解释沉默"
    assert row["handled_reason"] == "你自个儿去，我没必要回话"


async def test_replied_message_also_carries_reason(logged_in: TestEnv) -> None:  # noqa: ARG001
    """回了的时候也带上理由——事后能复盘"当时为什么这么说"。"""
    from ling.infra.db import session_scope
    from ling.infra.models import Message

    created = await logged_in.client.post("/v1/sessions", json={"title": "回复"})
    window_id = created.json()["session_id"]
    async with session_scope() as session:
        session.add(
            Message(
                id="msg_reply_probe", window_id=window_id, role="user",
                text="在吗", sequence=1, state="completed",
                handled_as="replied", handled_reason="状态还行，聊两句",
            )
        )
    r = await logged_in.client.get(f"/v1/sessions/{window_id}/messages")
    row = next(m for m in r.json()["messages"] if m["message_id"] == "msg_reply_probe")
    assert row["handled_as"] == "replied"
    assert row["handled_reason"] == "状态还行，聊两句"


# ---------------------------------------------------------------- 处置结果按实际校正

# `handled_as` 原本在消息**创建时**按"她的决定"写（决定回 → replied）。
# 但决定回不等于真的回了：用户可能在生成到一半按停止，那时一个字都没发出去。
# 记意图却让外面读成结果，就是假的完整感。


async def _seed_turn(client, messages: list[tuple[str, str]]) -> tuple[str, str]:
    """造一条 turn + 若干消息，返回 (window_id, turn_id)。"""
    from ling.infra.db import session_scope
    from ling.infra.models import Message, Turn

    created = await client.post("/v1/sessions", json={"title": "校正"})
    window_id = created.json()["session_id"]
    turn_id = "turn_correct_probe"
    async with session_scope() as session:
        session.add(
            Turn(
                id=turn_id,
                window_id=window_id,
                client_turn_id=turn_id,
                state="completed",
                config_revision=1,
            )
        )
        for seq, (role, handled) in enumerate(messages, start=1):
            session.add(
                Message(
                    id=f"msg_correct_{seq}",
                    window_id=window_id,
                    # 必须带上：校正是按 turn_id 找用户消息的
                    turn_id=turn_id,
                    role=role,
                    text="x",
                    sequence=seq,
                    state="completed",
                    handled_as=handled or None,
                )
            )
    return window_id, turn_id


async def test_stopped_turn_does_not_claim_replied(logged_in: TestEnv) -> None:
    """被打断且没有内容时，不能标成 replied。"""
    from ling.domains.conversation.service import ConversationService
    from ling.infra.db import session_scope

    _, turn_id = await _seed_turn(logged_in.client, [("user", "replied"), ("actor", None)])
    async with session_scope() as session:
        await ConversationService(session, actor_id="actor_ling").correct_handled_for_turn(
            turn_id, outcome="stopped", reason="被中断"
        )
    async with session_scope() as session:
        from sqlalchemy import select

        from ling.infra.models import Message

        row = (
            await session.execute(
                select(Message).where(Message.id == "msg_correct_1")
            )
        ).scalars().first()
    assert row is not None
    assert row.handled_as == "stopped", f"打断了却仍说回过：{row.handled_as}"
    assert row.handled_reason == "被中断"


async def test_ignored_is_not_overwritten_by_replied(logged_in: TestEnv) -> None:
    """她自己决定不回的那条，不能被后续校正改成"回了"。"""
    from sqlalchemy import select

    from ling.domains.conversation.service import ConversationService
    from ling.infra.db import session_scope
    from ling.infra.models import Message

    _, turn_id = await _seed_turn(logged_in.client, [("user", "ignored")])
    async with session_scope() as session:
        await ConversationService(session, actor_id="actor_ling").correct_handled_for_turn(
            turn_id, outcome="replied"
        )
    async with session_scope() as session:
        row = (
            await session.execute(select(Message).where(Message.id == "msg_correct_1"))
        ).scalars().first()
    assert row is not None
    assert row.handled_as == "ignored", "沉默被改成了回过"
