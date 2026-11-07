"""对话页需要的数据：群名 / 群号 / 发言人名片（后端部分）。

界面上要显示「群名为标题、群号作副标题、发言人名字与号码分层」。这些
字段**都不新增数据库列**：

- 群名：读侧向连接器采集（OneBot ``get_group_info``），进程内缓存；
  采集不到就返回 ``null``，由前端回退成「群 100…」。
- 群号 / 渠道：渠道窗口的 ``title`` 里本来就存着平台窗口标识
  （``onebot-x:group:100…``），就地解析。
- 发言人名字：入站时已经写进 ``subjects.display_name``（群名片优先），
  读侧 join；号码回查 ``actor_accounts``。

所以这里的用例重点在**回退**：没有采集能力、采集失败、历史消息没有
发言人时，接口必须照常 200，字段是 ``null`` 而不是编出来的值。
"""

from __future__ import annotations

import dataclasses
import logging

import pytest
from sqlalchemy import select

from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import Subject, Window
from ling.kernel import inbound_intake
from ling.logging_setup import clear_log_buffer
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture
from tests.integration.test_onebot_loop import (  # noqa: PLC2701
    CFG,
    QQ_USER,
    FakeOneBot,
    _bind,
    _event_body,
    _install,
    _post,
)

pytestmark = pytest.mark.w0_web

GROUP_ID = "100200400"
GROUP_REF = f"{CFG.instance_id}:group:{GROUP_ID}"
SENDER_QQ = "100200301"
SENDER_CARD = "阿懒"


@pytest.fixture(autouse=True)
def _fresh_behavior_logs(monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """日志缓冲和"处置降噪"的记账都是进程级全局，用例之间必须互不影响。"""
    clear_log_buffer()
    monkeypatch.setattr(inbound_intake, "_handled_notes", {})


class _GroupNameConnector:
    """只会回答群名的连接器桩：证明"能采集"与"采集失败"两条路。"""

    connector_id = "onebot"

    def __init__(self, name: str | None = "测试群", *, boom: bool = False) -> None:
        self._name = name
        self._boom = boom
        self.asked: list[str] = []

    async def fetch_group_name(self, group_id: str) -> str | None:
        self.asked.append(group_id)
        if self._boom:
            raise RuntimeError("采集超时")
        return self._name


async def _group_window(env: TestEnv) -> str:
    """建一个"渠道群窗口"：title 就是平台窗口标识（bridge 也这么建）。"""
    async with session_scope() as session:
        window = await ConversationService(session).create_window(
            subject_id=env.subject_id, title=GROUP_REF, channel="onebot"
        )
        await session.commit()
        return window.id


PRIVATE_REF = f"{CFG.instance_id}:private:{SENDER_QQ}"


async def _private_window(env: TestEnv) -> str:
    """建一个"渠道私聊窗口"：``subject_id`` 就是**对端**（群窗口才是主人）。"""
    async with session_scope() as session:
        window = await ConversationService(session).create_window(
            subject_id=env.subject_id, title=PRIVATE_REF, channel="onebot"
        )
        await session.commit()
        return window.id


async def _rename_subject(subject_id: str, name: str) -> None:
    """直接改 ``subjects.display_name``：入站时身份域就是这么写的。"""
    async with session_scope() as session:
        subject = await session.get(Subject, subject_id)
        assert subject is not None
        subject.display_name = name
        await session.commit()


async def _sender_subject(display_name: str | None = SENDER_CARD) -> str:
    """按入站身份域的做法建一个群友（名字来自群名片）。"""
    async with session_scope() as session:
        subject_id = await IdentityService(session).resolve_or_provision(
            CFG.instance_id, SENDER_QQ, display_name=display_name
        )
        await session.commit()
        assert subject_id
        return subject_id


async def _add_message(
    window_id: str, text: str, *, sender_subject_id: str | None = None
) -> None:
    async with session_scope() as session:
        await ConversationService(session).add_message(
            window_id, role="user", text=text, sender_subject_id=sender_subject_id
        )
        await session.commit()


def _session(body: dict, session_id: str) -> dict:
    for row in body["sessions"]:
        if row["session_id"] == session_id:
            return row
    raise AssertionError(f"会话列表里没有 {session_id}：{body}")


# ------------------------------------------------------------------ 窗口字段


async def test_group_window_exposes_name_channel_and_peer(logged_in: TestEnv) -> None:  # noqa: F811
    """群窗口：标题用采集到的群名，副标题用群号。"""
    window_id = await _group_window(logged_in)
    logged_in.app.state.onebot_connector = _GroupNameConnector()

    body = (await logged_in.client.get("/v1/sessions")).json()

    row = _session(body, window_id)
    assert row["display_name"] == "测试群"
    assert row["channel"] == "onebot"
    assert row["peer_id"] == GROUP_ID
    # 老字段不动：title 仍然是平台标识（投递侧靠它定位）
    assert row["title"] == GROUP_REF


async def test_group_name_is_asked_for_the_right_group(logged_in: TestEnv) -> None:  # noqa: F811
    """每次读会话都按群号问连接器（去重/缓存在连接器里，见单测）。"""
    await _group_window(logged_in)
    stub = _GroupNameConnector()
    logged_in.app.state.onebot_connector = stub

    await logged_in.client.get("/v1/sessions")
    await logged_in.client.get("/v1/sessions")

    assert stub.asked == [GROUP_ID, GROUP_ID]


async def test_group_name_falls_back_to_null_when_collection_fails(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """采集抛异常：接口照样 200，display_name 为 null（前端回退「群 100…」）。"""
    window_id = await _group_window(logged_in)
    logged_in.app.state.onebot_connector = _GroupNameConnector(boom=True)

    response = await logged_in.client.get("/v1/sessions")

    assert response.status_code == 200, response.text
    assert _session(response.json(), window_id)["display_name"] is None


async def test_group_name_is_null_when_no_connector(logged_in: TestEnv) -> None:  # noqa: F811
    """没有连接器（只跑网页版）：不编名字，也不报错。"""
    window_id = await _group_window(logged_in)
    logged_in.app.state.onebot_connector = None

    row = _session((await logged_in.client.get("/v1/sessions")).json(), window_id)
    assert row["display_name"] is None
    assert row["peer_id"] == GROUP_ID


async def test_web_window_has_no_peer(logged_in: TestEnv) -> None:  # noqa: F811
    """web 会话：channel=web、peer_id=null。title 是用户起的名字，不是平台标识。"""
    created = await logged_in.client.post("/v1/sessions", json={"title": "随便聊聊"})
    row = created.json()

    assert row["channel"] == "web"
    assert row["peer_id"] is None
    assert row["display_name"] is None
    # 就算用户把标题起成平台标识的样子，也不能解析出群号来
    weird = (
        await logged_in.client.patch(
            f"/v1/sessions/{row['session_id']}", json={"title": "a:group:999"}
        )
    ).json()
    assert weird["peer_id"] is None


async def test_group_name_failure_does_not_break_other_endpoints(logged_in: TestEnv) -> None:  # noqa: F811
    """单条会话读取走同一条采集逻辑，失败同样是 null。"""
    window_id = await _group_window(logged_in)
    logged_in.app.state.onebot_connector = _GroupNameConnector(name=None)

    row = (await logged_in.client.get(f"/v1/sessions/{window_id}")).json()
    assert row["display_name"] is None


async def test_private_window_uses_peer_nickname_as_title(logged_in: TestEnv) -> None:  # noqa: F811
    """QA B1：私聊窗口的标题要用**昵称**，不能只回退成「私聊 号码」。

    同一页的气泡上已经写着「阿岚 100200301」，标题却是「私聊 100200301」——
    数据一直都在 ``subjects.display_name`` 里，只是读侧没去取。
    """
    await _rename_subject(logged_in.subject_id, "阿岚")
    window_id = await _private_window(logged_in)
    # 私聊不需要外呼：连接器在不在都不该影响昵称
    logged_in.app.state.onebot_connector = None

    row = _session((await logged_in.client.get("/v1/sessions")).json(), window_id)

    assert row["display_name"] == "阿岚"
    assert row["peer_id"] == SENDER_QQ
    assert row["channel"] == "onebot"


async def test_private_window_without_nickname_stays_null(logged_in: TestEnv) -> None:  # noqa: F811
    """拿不到昵称（名字是空白）就返回 null，由前端回退「私聊 号码」——不编。"""
    await _rename_subject(logged_in.subject_id, "   ")
    window_id = await _private_window(logged_in)

    row = _session((await logged_in.client.get("/v1/sessions")).json(), window_id)

    assert row["display_name"] is None
    assert row["peer_id"] == SENDER_QQ


async def test_group_window_never_falls_back_to_the_owner_name(logged_in: TestEnv) -> None:  # noqa: F811
    """群名只能来自平台：采集不到时是 null，**不能**拿窗口主人的昵称顶上。

    群窗口的 ``subject_id`` 也是主人，所以"按 subject_id 取名字"这条捷径
    一旦用到群窗口上，标题就会变成主人自己的名字——那是另一种编造。
    """
    await _rename_subject(logged_in.subject_id, "阿岚")
    window_id = await _group_window(logged_in)
    logged_in.app.state.onebot_connector = _GroupNameConnector(boom=True)

    row = _session((await logged_in.client.get("/v1/sessions")).json(), window_id)

    assert row["display_name"] is None


# ------------------------------------------------------------------ 发言人


async def test_message_exposes_sender_card_and_number(logged_in: TestEnv) -> None:  # noqa: F811
    """群消息：名字（群名片）与号码（QQ）分层给前端。"""
    window_id = await _group_window(logged_in)
    subject_id = await _sender_subject()
    await _add_message(window_id, "今晚吃啥", sender_subject_id=subject_id)

    body = (await logged_in.client.get(f"/v1/sessions/{window_id}/messages")).json()

    message = body["messages"][-1]
    assert message["text"] == "今晚吃啥"
    assert message["sender_name"] == SENDER_CARD
    assert message["sender_id"] == SENDER_QQ


async def test_message_without_sender_stays_null(logged_in: TestEnv) -> None:  # noqa: F811
    """历史消息/网页消息没有发言人：返回 null，**不编**占位名字。"""
    window_id = await _group_window(logged_in)
    await _add_message(window_id, "没有发言人的老消息")

    message = (await logged_in.client.get(f"/v1/sessions/{window_id}/messages")).json()[
        "messages"
    ][-1]
    assert message["sender_name"] is None
    assert message["sender_id"] is None


async def test_sender_id_falls_back_to_subject_when_account_is_ambiguous(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """一个人在这个实例下绑了两个号：号码无从判断，退回 subject id 而不是猜。"""
    window_id = await _group_window(logged_in)
    subject_id = await _sender_subject()
    async with session_scope() as session:
        identity = IdentityService(session)
        await identity.bind_account(
            subject_id=subject_id,
            connector_instance=CFG.instance_id,
            external_account_id="200000002",
            proof="pending",
        )
        await session.commit()
    await _add_message(window_id, "我是谁", sender_subject_id=subject_id)

    message = (await logged_in.client.get(f"/v1/sessions/{window_id}/messages")).json()[
        "messages"
    ][-1]
    assert message["sender_name"] == SENDER_CARD
    assert message["sender_id"] == subject_id


# ------------------------------------------------- 延后处置：理由与降噪（真实链路）


def _behavior(caplog: pytest.LogCaptureFixture, event: str) -> list[str]:
    """从本次用例捕获的日志里取某类行为事件。

    用 caplog 而不是进程级环形缓冲：缓冲会被同进程里别的用例写满/清空，
    断言就成了"看运气"。caplog 只装**本用例**发出来的记录。
    """
    return [
        record.getMessage()
        for record in caplog.records
        if getattr(record, "event_name", "") == event
    ]


def _handled(caplog: pytest.LogCaptureFixture) -> list[str]:
    return _behavior(caplog, "inbound_handled")


def _received(caplog: pytest.LogCaptureFixture) -> list[str]:
    return _behavior(caplog, "inbound_received")


def _allow_owner_in_group(env: TestEnv) -> None:
    env.app.state.settings = dataclasses.replace(
        env.app.state.settings,
        access_mode="whitelist",
        access_list=frozenset({QQ_USER}),
        access_groups=frozenset({str(700000001)}),
    )


async def test_deferred_reason_is_only_the_reason(
    logged_in: TestEnv,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """群里的普通消息 → reason 只写理由，动作不重复（日志层会加动作）。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    _allow_owner_in_group(logged_in)

    with caplog.at_level(logging.INFO, logger="ling.behavior"):
        body = (
            await _post(
                logged_in,
                _event_body(
                    post_type="message",
                    message_type="group",
                    sub_type="normal",
                    group_id=700000001,
                    message="今晚吃啥",
                    raw_message="今晚吃啥",
                ),
            )
        ).json()

    assert body["decision"] == "deferred_attention", body
    assert body["reason"] == "群里的普通消息", body
    assert _handled(caplog) == ["对 小不点 的话：先放着，等她刷到（群里的普通消息）"]


async def test_repeated_plain_group_messages_log_one_handled_line(
    logged_in: TestEnv,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """连着三条普通群消息：处置行只留一条，收件行一条不少。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    _allow_owner_in_group(logged_in)

    with caplog.at_level(logging.INFO, logger="ling.behavior"):
        for index in range(3):
            await _post(
                logged_in,
                _event_body(
                    post_type="message",
                    message_type="group",
                    sub_type="normal",
                    group_id=700000001,
                    message_id=800 + index,
                    message=f"随便说说 {index}",
                    raw_message=f"随便说说 {index}",
                ),
            )

    assert _handled(caplog) == ["对 小不点 的话：先放着，等她刷到（群里的普通消息）"]
    # **不丢信息**：全部消息本身仍然逐条记（正文在那里）
    assert len(_received(caplog)) == 3


async def test_observe_only_group_message_says_why_it_cannot_speak(logged_in: TestEnv) -> None:  # noqa: F811
    """陌生人进群：理由必须是"没资格开口"，不能笼统说成"群里的普通消息"。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    _allow_owner_in_group(logged_in)

    body = (
        await _post(
            logged_in,
            _event_body(
                message_type="group",
                sub_type="normal",
                group_id=700000001,
                user_id=888888888,
                message="有人吗",
                raw_message="有人吗",
            ),
        )
    ).json()

    assert body["decision"] == "deferred_attention", body
    assert "资格" in body["reason"], body


async def test_group_window_title_is_not_used_as_display_name(logged_in: TestEnv) -> None:  # noqa: F811
    """真实入站建的群窗口：title 是平台标识，采集不到群名时 display_name 为 null。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    _allow_owner_in_group(logged_in)

    await _post(
        logged_in,
        _event_body(
            message_type="group",
            sub_type="normal",
            group_id=700000001,
            message="今晚吃啥",
            raw_message="今晚吃啥",
        ),
    )

    async with session_scope() as session:
        window = (
            await session.execute(select(Window).where(Window.title == f"{CFG.instance_id}:group:700000001"))
        ).scalars().first()
        assert window is not None
        window_id = window.id

    # FakeOneBot 对 get_group_info 回的是 data=None → 拿不到群名
    row = _session((await logged_in.client.get("/v1/sessions")).json(), window_id)
    assert row["display_name"] is None
    assert row["peer_id"] == "700000001"
    assert row["title"] == f"{CFG.instance_id}:group:700000001"
