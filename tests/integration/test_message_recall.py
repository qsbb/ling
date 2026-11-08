"""撤回：让模型忘掉那条消息（AstrBot / MaiBot 都没做的那一层）。

## 这一层要守住什么

1. **撤回不是消息**：不产生 `messages` 行、不触发回复、不调模型；
2. **不物理删除**：`retracted_at` 非空、行还在（可追溯）；
3. **上下文失效**：`retracted_at` 非空的消息不再进 `messages_for_prompt`
   （模型上下文 / 摘要素材），但**界面历史照常返回**它（标"已撤回"）；
4. **幂等**：重复上报同一撤回，数据只变一次、日志只记一条；
5. **未匹配**：撤回的 id 在库里找不到 → 不报错、不建行、只记一行日志；
6. **不写事实**：撤回路径不新增 `activities` / `self_experiences` / `memories`，
   模型调用计数不变。

## 为什么"不删除、不回改记忆"

删行之后"这条消息从来不存在"和"它被撤回了"在库里长得一样，而且
`memories.source_event_ids` / `turn_id` / 审阅页面会变成悬空引用。
已沉淀的记忆也不批量回改：撤回是"以后别再引用"，不是"过去没发生"；
用户要清除记忆有专门的 `retract` / `forget` 入口，那是另一件事。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import func, select

from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    BehaviorModelCallRow,
    Message,
    SelfExperienceRow,
    Window,
)
from ling.kernel.context import RETRACTED_NOTE, ContextCompiler, render_rollup
from ling.logging_setup import clear_log_buffer, recent_logs
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-recall",
)
QQ_USER = "20003"
GROUP = "99999"
SELF_ID = "10001"


# ------------------------------------------------------------------ 装置


class _FakeOneBot:
    """记录出站请求；一律成功。撤回链路本来就不该发任何东西。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(
            {
                "action": str(request.url).rsplit("/", 1)[-1],
                "body": json.loads(request.content or b"{}"),
            }
        )
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9001}}
        )


def _install(env: TestEnv, fake: _FakeOneBot) -> OneBotV11Connector:
    connector = OneBotV11Connector(
        CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    env.app.state.onebot_connector = connector
    return connector


async def _bind(env: TestEnv) -> None:
    async with session_scope() as session:
        await IdentityService(session).bind_account(
            subject_id=env.subject_id,
            connector_instance=CFG.instance_id,
            external_account_id=QQ_USER,
            proof="proven",
        )
        await session.commit()


def _message_body(*, message_id: int = 501, text: str = "这句话我一会儿要撤回") -> bytes:
    payload = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": int(SELF_ID),
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": int(QQ_USER),
        "message": text,
        "raw_message": text,
        "font": 0,
        "sender": {"nickname": "小撤回", "sex": "male", "age": 18},
    }
    return json.dumps(payload).encode()


def _recall_body(
    *, message_id: int = 501, group_id: int | None = None, user_id: int = int(QQ_USER),
    operator_id: int | None = None,
) -> bytes:
    payload: dict = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": int(SELF_ID),
        "post_type": "notice",
        "notice_type": "group_recall" if group_id is not None else "friend_recall",
        "message_id": message_id,
        "user_id": user_id,
    }
    if group_id is not None:
        payload["group_id"] = group_id
    if operator_id is not None:
        payload["operator_id"] = operator_id
    return json.dumps(payload).encode()


def _headers(body: bytes) -> dict[str, str]:
    sig = hmac.new(b"sec", body, hashlib.sha1).hexdigest()
    return {
        "X-Signature": f"sha1={sig}",
        "X-Self-ID": SELF_ID,
        "Content-Type": "application/json",
    }


async def _post(env: TestEnv, body: bytes) -> httpx.Response:
    return await env.client.post("/v1/onebot/events", content=body, headers=_headers(body))


# 撤回相关的行为日志动作名。**按 action 过滤，不要按正文里有没有"撤回"**：
# 被撤回的那句话本身可能带"撤回"两个字（测试用的就是），按正文过滤会把
# "收到/处理"那两行也算进来，断言就再也测不准"日志只有一条"。
RECALL_ACTIONS = frozenset(
    {
        "message_retracted_after_seen",
        "message_retracted_unseen",
        "recall_unmatched",
        "recall_self",
    }
)


def _behavior_lines() -> list[str]:
    return [
        entry.message
        for entry in recent_logs(kind="behavior")
        if entry.event_name in RECALL_ACTIONS
    ]


@pytest.fixture(autouse=True)
def _fresh_logs() -> None:
    clear_log_buffer()


async def _counts() -> dict[str, int]:
    async with session_scope() as session:
        return {
            "activities": int(
                (
                    await session.execute(select(func.count()).select_from(ActivityRow))
                ).scalar_one()
            ),
            "experiences": int(
                (
                    await session.execute(
                        select(func.count()).select_from(SelfExperienceRow)
                    )
                ).scalar_one()
            ),
            "model_calls": int(
                (
                    await session.execute(
                        select(func.count()).select_from(BehaviorModelCallRow)
                    )
                ).scalar_one()
            ),
        }


async def _messages(window_id: str | None = None) -> list[Message]:
    async with session_scope() as session:
        stmt = select(Message).order_by(Message.sequence)
        if window_id:
            stmt = stmt.where(Message.window_id == window_id)
        return list((await session.execute(stmt)).scalars())


async def _window_for_private(env: TestEnv) -> str:
    """跑一条私聊消息，返回它落到的窗口 id。"""
    response = await _post(env, _message_body())
    assert response.status_code == 200, response.text
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Message).where(Message.role == "user").order_by(Message.sequence.desc())
            )
        ).scalars().first()
    assert row is not None, "入站消息没有落库"
    return str(row.window_id)


# ------------------------------------------------------------------ 连接器映射


def test_connector_maps_group_recall_to_recall_event() -> None:
    """`notice.group_recall` → `RecallEvent`（不是信封）。"""
    connector = OneBotV11Connector(CFG)
    payload = json.loads(
        _recall_body(message_id=777, group_id=int(GROUP), operator_id=555)
    )
    event = connector.normalize_inbound(payload)
    from ling.contracts.v1 import RecallEvent, RecallKind

    assert isinstance(event, RecallEvent), type(event)
    assert event.recall_kind is RecallKind.group
    assert event.platform_message_id == "777"
    assert event.window_ref == f"{CFG.instance_id}:group:{GROUP}"
    assert event.operator_ref == "555"
    assert event.self_recall is False
    # 去重键稳定：同一 message_id 永远同一个 id（平台重投不产生第二条）
    assert event.external_event_id == f"{CFG.instance_id}:recall:777"


def test_connector_maps_friend_recall_and_flags_self_recall() -> None:
    connector = OneBotV11Connector(CFG)
    friend = connector.normalize_inbound(json.loads(_recall_body(message_id=888)))
    from ling.contracts.v1 import RecallEvent, RecallKind

    assert isinstance(friend, RecallEvent)
    assert friend.recall_kind is RecallKind.friend
    assert friend.window_ref == f"{CFG.instance_id}:private:{QQ_USER}"

    mine = connector.normalize_inbound(
        json.loads(_recall_body(message_id=889, user_id=int(SELF_ID)))
    )
    assert isinstance(mine, RecallEvent)
    assert mine.self_recall is True, "自己撤回自己的要单独标，不能错误归属成'对方撤回'"


def test_other_notices_stay_ignored() -> None:
    """戳一戳/入群/群名片等 notice 仍然不进入任何链路（也不该记成 unsupported）。"""
    from ling.ports.channel import ChannelError, ChannelErrorKind

    connector = OneBotV11Connector(CFG)
    for notice_type in ("notify", "group_increase", "group_card", "friend_add"):
        with pytest.raises(ChannelError) as exc:
            connector.normalize_inbound(
                {"post_type": "notice", "notice_type": notice_type, "time": 1}
            )
        assert exc.value.kind is ChannelErrorKind.ignored, notice_type


def test_recall_without_message_id_is_not_guessed() -> None:
    from ling.ports.channel import ChannelError

    connector = OneBotV11Connector(CFG)
    with pytest.raises(ChannelError):
        connector.normalize_inbound(
            {"post_type": "notice", "notice_type": "friend_recall", "user_id": 1}
        )


# ------------------------------------------------------------------ 落库与上下文


async def test_A_recall_after_she_saw_it_keeps_it_with_a_note(logged_in: TestEnv) -> None:  # noqa: F811
    """**A：她看手机之后才撤回** —— 上下文里仍在，但带「已被撤回」的注记。

    用户原话：「如果刚好看到之后再撤回的话会经过正常的处理，并且在之后会
    知道被撤回了」。所以：

    - 原文**不抹掉**（她已经据此回过话，抹掉等于篡改她见过的事实）；
    - 加一句旁白，让她之后知道这条被撤回了；
    - 已发出的回复、已产生的事实一概不回滚。
    """
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    window_id = await _window_for_private(logged_in)  # 私聊会被回复 → 她已看过
    before = await _messages(window_id)
    target = [m for m in before if m.role == "user"][-1]
    assert target.retracted_at is None
    # 前置条件：她确实已经看过（这正是"看手机之后才撤回"那一档的事实依据）
    assert target.attention_state == "seen", target.attention_state
    assert target.handled_as == "replied", target.handled_as

    facts_before = await _counts()
    replies_before = [m for m in before if m.role == "actor"]

    body = (await _post(logged_in, _recall_body(message_id=501))).json()
    assert body["accepted"] is True, body
    assert body["recalled"] is True, body
    assert body["after_seen"] is True, body

    async with session_scope() as session:
        row = await session.get(Message, target.id)
        assert row is not None, "**不许物理删除**"
        assert row.retracted_at is not None
        assert row.retracted_after_seen is True, "判定没被冻结下来"
        assert row.text == target.text, "原文仍在（可追溯）"

    # 上下文里**仍然可见**，且带注记
    async with session_scope() as session:
        prompt = await ConversationService(session).messages_for_prompt(window_id)
    kept = [v for v in prompt if v.message_id == target.id]
    assert kept, "她看过的消息被抹掉了：那是篡改她见过的事实"
    assert kept[0].retracted_after_seen is True

    async with session_scope() as session:
        compiled = await ContextCompiler(
            session, actor_id="actor_ling", persona_id="persona_ling"
        ).compile(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            user_text="接着说",
            is_group=False,
            history_limit=50,
        )
    history = [m for m in compiled.annotated if m.lane == "history" and m.role == "user"]
    hit = [m for m in history if "这句话我一会儿要撤回" in m.content]
    assert hit, f"她看过的消息不在历史里：{[m.content[:40] for m in history]}"
    assert RETRACTED_NOTE in hit[0].content, (
        f"没有留下「被撤回了」的痕迹：{hit[0].content!r}"
    )

    # 已发出的回复不受影响
    after = await _messages(window_id)
    replies_after = [m for m in after if m.role == "actor"]
    assert [m.id for m in replies_after] == [m.id for m in replies_before], (
        "撤回回滚了她已经发出的回复"
    )

    # 事实表不变
    assert await _counts() == facts_before, "撤回写了事实或调了模型"


async def test_B_unseen_recall_leaves_the_unread_queue(logged_in: TestEnv) -> None:  # noqa: F811
    """**未读队列也要剔除**：她刷到时不能把已撤回的消息读进决策。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    group_body = json.dumps(
        {
            "time": int(datetime.now(UTC).timestamp()),
            "self_id": int(SELF_ID),
            "post_type": "message",
            "message_type": "group",
            "group_id": int(GROUP),
            "message_id": 751,
            "user_id": int(QQ_USER),
            "message": "这条会留在未读队列里然后被撤回",
            "raw_message": "这条会留在未读队列里然后被撤回",
            "font": 0,
            "sender": {"nickname": "小撤回", "card": "撤回君"},
        }
    ).encode()
    assert (await _post(logged_in, group_body)).status_code == 200

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Message).where(Message.text == "这条会留在未读队列里然后被撤回")
            )
        ).scalars().first()
        window_id = str(row.window_id)
        before = await ConversationService(session).unseen_messages(window_id)
    assert any(v.message_id == row.id for v in before), "前置条件：它在未读队列里"

    await _post(logged_in, _recall_body(message_id=751, group_id=int(GROUP)))

    async with session_scope() as session:
        after = await ConversationService(session).unseen_messages(window_id)
    assert all(v.message_id != row.id for v in after), (
        "已撤回（她没看过）的消息仍留在未读队列里——刷到时会被读进决策"
    )


async def test_B_recall_before_she_saw_it_is_removed_from_context(logged_in: TestEnv) -> None:  # noqa: F811
    """**B：她还没看到就被撤回** —— 不进任何后续模型上下文（等同没看见）。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    # 群里没点名的普通消息：落库为 `unseen`（等她刷到），不是"已看过"
    group_body = json.dumps(
        {
            "time": int(datetime.now(UTC).timestamp()),
            "self_id": int(SELF_ID),
            "post_type": "message",
            "message_type": "group",
            "group_id": int(GROUP),
            "message_id": 701,
            "user_id": int(QQ_USER),
            "message": "这句话她还没看到就被撤回了",
            "raw_message": "这句话她还没看到就被撤回了",
            "font": 0,
            "sender": {"nickname": "小撤回", "card": "撤回君"},
        }
    ).encode()
    assert (await _post(logged_in, group_body)).status_code == 200

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Message).where(Message.text == "这句话她还没看到就被撤回了")
            )
        ).scalars().first()
    assert row is not None, "前置条件：消息已入库"
    assert row.attention_state == "unseen", row.attention_state
    assert not row.handled_as, row.handled_as
    window_id = str(row.window_id)

    body = (
        await _post(logged_in, _recall_body(message_id=701, group_id=int(GROUP)))
    ).json()
    assert body["recalled"] is True, body
    assert body["after_seen"] is False, body

    async with session_scope() as session:
        fresh = await session.get(Message, row.id)
        assert fresh is not None and fresh.retracted_at is not None
        assert fresh.retracted_after_seen is False

    # 不进上下文
    async with session_scope() as session:
        prompt = await ConversationService(session).messages_for_prompt(window_id)
    assert all(v.message_id != row.id for v in prompt), "她没看过的撤回消息仍在上下文素材里"

    async with session_scope() as session:
        compiled = await ContextCompiler(
            session, actor_id="actor_ling", persona_id="persona_ling"
        ).compile(
            subject_id=logged_in.subject_id,
            window_id=window_id,
            user_text="群里刚才说啥了",
            is_group=True,
            history_limit=50,
        )
    joined = "\n".join(m.content for m in compiled.messages)
    assert "这句话她还没看到就被撤回了" not in joined, (
        "她没看过的撤回消息进了提示词"
    )


async def test_A_rollup_summary_keeps_the_note(logged_in: TestEnv) -> None:  # noqa: F811
    """消息被挤出窗口压进摘要后，注记也要跟着走（否则痕迹会消失）。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    window_id = await _window_for_private(logged_in)
    await _post(logged_in, _recall_body(message_id=501))

    async with session_scope() as session:
        views = await ConversationService(session).messages_for_prompt(window_id)
    target = [v for v in views if "这句话我一会儿要撤回" in v.text]
    assert target, "前置条件：她看过的撤回消息仍在上下文素材里"

    text = render_rollup([target[0]])
    assert "后来被撤回了" in text, text


async def test_recall_is_idempotent(logged_in: TestEnv) -> None:  # noqa: F811
    """重复上报同一撤回：数据只变一次，行为日志只记一条。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    window_id = await _window_for_private(logged_in)

    first = (await _post(logged_in, _recall_body(message_id=501))).json()
    assert first["recalled"] is True, first
    async with session_scope() as session:
        row = (
            await session.execute(
                select(Message).where(
                    Message.window_id == window_id, Message.role == "user"
                )
            )
        ).scalars().all()[-1]
        stamp_first = row.retracted_at
    assert stamp_first is not None
    lines_first = _behavior_lines()
    assert len(lines_first) == 1, lines_first

    second = (await _post(logged_in, _recall_body(message_id=501))).json()
    assert second["accepted"] is True
    assert second.get("duplicate") is True, second
    async with session_scope() as session:
        row = await session.get(Message, row.id)
        assert row is not None and row.retracted_at == stamp_first, (
            "重复撤回改了时间戳：说明不是幂等更新"
        )
    # 日志只一条（重复通知走事件级去重，连"已处理过"都不再记）
    lines_second = _behavior_lines()
    assert lines_second == lines_first, lines_second


async def test_recall_of_unknown_id_is_harmless(logged_in: TestEnv) -> None:  # noqa: F811
    """撤回一个库里没有的 id：不报错、不建行、只记一行"未匹配"。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _window_for_private(logged_in)
    before = await _messages()

    response = await _post(logged_in, _recall_body(message_id=999999))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True, body
    assert body["recalled"] is False
    assert body["reason"] == "unmatched", body

    after = await _messages()
    assert len(after) == len(before), "撤回未匹配却建了消息行"
    lines = _behavior_lines()
    assert len(lines) == 1, lines
    assert "没有对应的原文" in lines[0], lines


async def test_recall_does_not_touch_facts_or_call_models(logged_in: TestEnv) -> None:  # noqa: F811
    """负向：撤回不新增 activities / self_experiences，不增加模型调用。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _window_for_private(logged_in)
    before = await _counts()
    sent_before = len(fake.sent)

    await _post(logged_in, _recall_body(message_id=501))

    after = await _counts()
    assert after == before, f"撤回路径写了事实或调了模型：{before} → {after}"
    assert len(fake.sent) == sent_before, "撤回竟然往外发了消息"


async def test_recall_does_not_create_a_turn_or_window_message(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """撤回不产生新的 messages 行（它不是一条发言）。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    window_id = await _window_for_private(logged_in)
    before = len(await _messages(window_id))

    await _post(logged_in, _recall_body(message_id=501))

    after = len(await _messages(window_id))
    assert after == before, "撤回产生了新消息行"


async def test_group_recall_targets_the_group_window(logged_in: TestEnv) -> None:  # noqa: F811
    """群撤回只影响那个群窗口里的那条消息。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    group_body = json.dumps(
        {
            "time": int(datetime.now(UTC).timestamp()),
            "self_id": int(SELF_ID),
            "post_type": "message",
            "message_type": "group",
            "group_id": int(GROUP),
            "message_id": 601,
            "user_id": int(QQ_USER),
            "message": "群里这句话会被撤回",
            "raw_message": "群里这句话会被撤回",
            "font": 0,
            "sender": {"nickname": "小撤回", "card": "撤回君"},
        }
    ).encode()
    assert (await _post(logged_in, group_body)).status_code == 200

    async with session_scope() as session:
        row = (
            await session.execute(select(Message).where(Message.text == "群里这句话会被撤回"))
        ).scalars().first()
    assert row is not None, "群消息没落库"
    window_id = str(row.window_id)

    response = await _post(
        logged_in, _recall_body(message_id=601, group_id=int(GROUP))
    )
    assert response.json()["recalled"] is True, response.text

    async with session_scope() as session:
        row = await session.get(Message, row.id)
        assert row is not None and row.retracted_at is not None
        window = await session.get(Window, window_id)
        assert window is not None and window.visibility == "group_scope"


async def test_unseen_recall_never_reaches_rollup(logged_in: TestEnv) -> None:  # noqa: F811
    """**她没看过**的撤回消息连摘要素材都不进（`render_rollup` 的输出里没有它）。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    group_body = json.dumps(
        {
            "time": int(datetime.now(UTC).timestamp()),
            "self_id": int(SELF_ID),
            "post_type": "message",
            "message_type": "group",
            "group_id": int(GROUP),
            "message_id": 801,
            "user_id": int(QQ_USER),
            "message": "这条她没看到就被撤回了",
            "raw_message": "这条她没看到就被撤回了",
            "font": 0,
            "sender": {"nickname": "小撤回", "card": "撤回君"},
        }
    ).encode()
    assert (await _post(logged_in, group_body)).status_code == 200
    await _post(logged_in, _recall_body(message_id=801, group_id=int(GROUP)))

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Message).where(Message.text == "这条她没看到就被撤回了")
            )
        ).scalars().first()
    assert row is not None and row.retracted_after_seen is False
    window_id = str(row.window_id)

    # 取数函数不再返回它 → 它也就永远进不了摘要素材
    async with session_scope() as session:
        prompt = await ConversationService(session).messages_for_prompt(window_id, limit=200)
    assert all("这条她没看到就被撤回了" not in v.text for v in prompt)
    assert "这条她没看到就被撤回了" not in render_rollup(list(prompt))
