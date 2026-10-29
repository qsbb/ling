"""OneBot 完整闭环：上报 → 身份 → 窗口 → 决策 → 轮次 → 回复 → 投递。

这是"QQ 能不能真的聊天"的验收。之前 ingress 只做到收下事件就返回，
不创建轮次、不生成回复、不发送 —— 连接器能收消息但聊不了。

用 httpx MockTransport 模拟 OneBot 实现，不需要真实 QQ 账号。
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
from sqlalchemy import select

from ling.domains.consent.service import ConsentService
from ling.domains.identity.service import IdentityService
from ling.domains.media.service import INBOUND_SCOPE
from ling.infra.db import session_scope
from ling.infra.models import MediaEvidenceRow, Message, Window
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-loop",
)
QQ_USER = "20002"
GROUP = "88888"


class FakeOneBot:
    """记录发出的请求，返回成功响应。

    ``fail_after``：前 N 次发送返回失败，用来验证"第一段没回执就不发第二段"。
    """

    def __init__(self, *, fail_after: int | None = None) -> None:
        self.sent: list[dict] = []
        self._fail_after = fail_after

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        action = str(request.url).rsplit("/", 1)[-1]
        self.sent.append({"action": action, "body": body})
        if self._fail_after is not None and len(self.sent) <= self._fail_after:
            return httpx.Response(
                200, json={"status": "failed", "retcode": 100, "data": None}
            )
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9001}}
        )


def _install(env: TestEnv, fake: FakeOneBot) -> OneBotV11Connector:
    connector = OneBotV11Connector(CFG, http=httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler)
    ))
    env.app.state.onebot_connector = connector
    return connector


async def _bind(env: TestEnv, *, proof: str = "proven") -> None:
    async with session_scope() as session:
        await IdentityService(session).bind_account(
            subject_id=env.subject_id,
            connector_instance=CFG.instance_id,
            external_account_id=QQ_USER,
            proof=proof,
        )


def _event_body(**overrides) -> bytes:
    payload = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 777,
        "user_id": int(QQ_USER),
        "message": "你好",
        "raw_message": "你好",
        "font": 0,
        "sender": {"nickname": "小不点", "sex": "male", "age": 18},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _headers(body: bytes, *, self_id: str = "10001") -> dict[str, str]:
    sig = hmac.new(b"sec", body, hashlib.sha1).hexdigest()
    return {
        "X-Signature": f"sha1={sig}",
        "X-Self-ID": self_id,
        "Content-Type": "application/json",
    }


async def _post(env: TestEnv, body: bytes, **kwargs) -> httpx.Response:  # noqa: ANN003
    return await env.client.post("/v1/onebot/events", content=body, headers=_headers(body, **kwargs))


# ------------------------------------------------------------------ 闭环


async def test_private_message_gets_a_reply(logged_in: TestEnv) -> None:
    """核心验收：一条私聊消息最终真的发出了一条 QQ 回复。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    response = await _post(logged_in, _event_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True, body
    assert body["decision"] == "reply"
    assert body["delivered"] is True, body

    # 真的调了 send_private_msg，且目标是这个 QQ 号
    assert fake.sent, "没有任何发送请求 —— 闭环没打通"
    call = fake.sent[0]
    assert call["action"] == "send_private_msg"
    assert str(call["body"]["user_id"]) == QQ_USER


async def test_reply_is_persisted(logged_in: TestEnv) -> None:
    """机器人的回复必须落库（可审计、可重放）。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _post(logged_in, _event_body())

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Message).where(Message.role == "actor")
                )
            ).scalars()
        )
    assert rows and rows[-1].text, "回复没落库"


async def test_window_is_channel_scoped(logged_in: TestEnv) -> None:
    """窗口必须挂在渠道上（channel=onebot），不能混进 Web 会话。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _post(logged_in, _event_body())

    async with session_scope() as session:
        windows = list(
            (
                await session.execute(select(Window).where(Window.channel == "onebot"))
            ).scalars()
        )
    assert len(windows) == 1
    assert windows[0].title == f"onebot-loop:private:{QQ_USER}"


async def test_unbound_account_not_accepted(logged_in: TestEnv) -> None:
    """未绑定账号不进主链（不猜身份）。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    response = await _post(logged_in, _event_body())
    body = response.json()
    assert body["accepted"] is False
    assert "绑定" in body["reason"]
    assert fake.sent == [], "未绑定账号却发出了消息"


async def test_unproven_binding_not_accepted(logged_in: TestEnv) -> None:
    """proof=pending 的绑定不算已证明（此前实现的身份缺口）。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in, proof="pending")
    body = (await _post(logged_in, _event_body())).json()
    assert body["accepted"] is False
    assert fake.sent == []


# ------------------------------------------------------------------ 去重与验签


async def test_duplicate_event_processed_once(logged_in: TestEnv) -> None:
    """OneBot 超时会重投同一事件，不能回两遍。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    body = _event_body()
    await _post(logged_in, body)
    # 一次回复可能分成多条投递（"连发"），所以先记下发了几条，
    # 再断言"重投没有**新增**"——写死 1 条会随分段规则变化而误报。
    sent_after_first = len(fake.sent)
    assert sent_after_first >= 1, "第一次事件就该有投递"
    second = await _post(logged_in, body)
    assert second.json().get("duplicate") is True
    assert len(fake.sent) == sent_after_first, (
        f"重投导致新增了 {len(fake.sent) - sent_after_first} 条"
    )


async def test_bad_signature_rejected(logged_in: TestEnv) -> None:
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    body = _event_body()
    response = await logged_in.client.post(
        "/v1/onebot/events",
        content=body,
        headers={"X-Signature": "sha1=deadbeef", "X-Self-ID": "10001"},
    )
    assert response.status_code == 403
    assert fake.sent == []


async def test_non_message_event_ignored(logged_in: TestEnv) -> None:
    """心跳等事件返回 200（避免重投）但明确标为未受理。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    body = json.dumps({"post_type": "meta_event", "self_id": 10001}).encode()
    response = await _post(logged_in, body)
    assert response.status_code == 200
    assert response.json()["accepted"] is False


# ------------------------------------------------------------------ 群聊


async def test_group_window_shared_by_members(logged_in: TestEnv) -> None:
    """同一个群的不同成员必须落到**同一个窗口**。

    否则每个成员各一个窗口，角色看不到完整群聊，
    无法判断群里的气氛与是否该接话。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)

    # 两个成员绑定到同一自然人（模拟同一人的两个号，或绑定的两个人）
    async with session_scope() as session:
        service = IdentityService(session)
        for qq in ("20002", "30003"):
            await service.bind_account(
                subject_id=logged_in.subject_id,
                connector_instance=CFG.instance_id,
                external_account_id=qq,
                proof="proven",
            )

    await _post(logged_in, _event_body(message_type="group", group_id=int(GROUP),
                                      sub_type="normal", user_id=20002, message_id=1))
    await _post(logged_in, _event_body(message_type="group", group_id=int(GROUP),
                                      sub_type="normal", user_id=30003, message_id=2))

    async with session_scope() as session:
        windows = list(
            (
                await session.execute(
                    select(Window).where(Window.channel == "onebot",
                                         Window.visibility == "group_scope")
                )
            ).scalars()
        )
    assert len(windows) == 1, f"同群产生了 {len(windows)} 个窗口"
    assert windows[0].title == f"onebot-loop:group:{GROUP}"


async def test_group_reply_uses_group_action(logged_in: TestEnv) -> None:
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _post(logged_in, _event_body(message_type="group", group_id=int(GROUP),
                                      sub_type="normal", message="[CQ:at,qq=10001] 你好"))
    group_calls = [c for c in fake.sent if c["action"] == "send_group_msg"]
    assert group_calls, "群消息没走 send_group_msg"
    assert str(group_calls[0]["body"]["group_id"]) == GROUP


# ------------------------------------------------------------------ 失败重投（此前实现的可靠性缺陷）


async def test_failed_turn_releases_claim_so_retry_works(logged_in: TestEnv) -> None:
    """处理失败后必须能重投。

    原实现先 `claim_event` 并**立刻提交**，再跑轮次。如果轮次失败
    （模型报错、发送失败），那条认领就成了"这件事办过了"的错误记录 ——
    OneBot 重投时被当成重复直接丢掉，用户永远收不到这一轮回复。

    这条测试用"第一次让协调器抛错、第二次正常"来验证认领被撤回。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    class Boom:
        calls = 0

        async def run_turn(self, **kwargs):  # noqa: ANN003
            Boom.calls += 1
            if Boom.calls == 1:
                raise RuntimeError("模拟模型侧失败")
            from ling.kernel.turn import TurnCoordinator

            real = TurnCoordinator(
                router=logged_in.app.state.router,
                actor_id="actor_ling",
                persona_id="persona_ling",
                cancels=logged_in.app.state.cancels,
            )
            return await real.run_turn(**kwargs)

    logged_in.app.state.bridge_coordinator = Boom()

    body = _event_body(message_id=4321)
    first = await _post(logged_in, body)
    assert first.status_code == 200
    assert first.json()["accepted"] is False
    assert fake.sent == [], "失败的轮次不该发出消息"

    # 重投同一事件：认领应已撤回，能重新处理
    second = await _post(logged_in, body)
    assert second.json().get("duplicate") is not True, "失败后重投被当成重复，回复永久丢失"
    assert second.json()["accepted"] is True
    assert fake.sent, "重投没有产生回复"


async def test_duplicate_still_blocked_after_success(logged_in: TestEnv) -> None:
    """成功之后的重投仍必须被挡住（去重不能因为修了失败路径而失效）。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    body = _event_body(message_id=4322)
    first = await _post(logged_in, body)
    assert first.json().get("duplicate") is not True
    second = await _post(logged_in, body)
    assert second.json().get("duplicate") is True


async def test_group_reply_to_bot_counts_as_addressed(logged_in: TestEnv) -> None:
    """群里"引用机器人上一条消息"也是明确指向，不能因为没 @ 就沉默。

    否则用户接着机器人说的话往下问，它会把这句话当成对空气说的。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    # 不 @ 任何人，只引用一条消息
    body = _event_body(
        message_type="group",
        group_id=int(GROUP),
        sub_type="normal",
        message="[CQ:reply,id=424242] 那你刚才说的是什么意思",
    )
    result = (await _post(logged_in, body)).json()
    assert result["decision"] == "reply", f"引用了机器人却沉默: {result}"
    assert any(c["action"] == "send_group_msg" for c in fake.sent)


# ------------------------------------------------------------------ 媒体入库

# 纯图片消息：OneBot CQ 码，正文为空。以前这种消息会被整条丢掉。
_IMAGE_CQ = "[CQ:image,file=a1b2c3.jpg,sub_type=0]"


async def _grant_inbound(env: TestEnv, *, granted: bool = True) -> None:
    async with session_scope() as session:
        svc = ConsentService(session)
        if granted:
            await svc.grant(subject_id=env.subject_id, scope=INBOUND_SCOPE)
        else:
            await svc.revoke(subject_id=env.subject_id, scope=INBOUND_SCOPE)


async def test_image_without_consent_is_not_silently_dropped(logged_in: TestEnv) -> None:
    """没授权时不能装作没收到——必须报出"缺 media.inbound"。

    静默丢弃会让"发了图角色没反应"变成一个查不出来的怪现象：
    既不知道收到没有，也不知道缺什么。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _grant_inbound(logged_in, granted=False)

    body = (
        await _post(logged_in, _event_body(message=_IMAGE_CQ, raw_message=_IMAGE_CQ))
    ).json()

    assert body["accepted"] is False
    assert body["media_evidence_ids"] == []
    assert body["media_note"] and INBOUND_SCOPE in body["media_note"], body
    assert not fake.sent, "没理解内容就不应该发回复"


async def test_image_with_consent_is_recorded(logged_in: TestEnv) -> None:
    """有授权时留下证据。纯媒体暂不建轮次（还没有描述能力）。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _grant_inbound(logged_in)

    body = (
        await _post(logged_in, _event_body(message=_IMAGE_CQ, raw_message=_IMAGE_CQ))
    ).json()

    assert body["accepted"] is False, "纯媒体还没有描述，不建轮次"
    assert len(body["media_evidence_ids"]) == 1, body
    assert "已记录媒体证据" in body["reason"]
    assert body["media_note"] is None
    assert not fake.sent


async def test_image_evidence_lands_in_db(logged_in: TestEnv) -> None:
    """证据必须真的落库，否则描述任务永远捡不到活。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _grant_inbound(logged_in)

    body = (
        await _post(logged_in, _event_body(message=_IMAGE_CQ, raw_message=_IMAGE_CQ))
    ).json()
    evidence_id = body["media_evidence_ids"][0]

    async with session_scope() as session:
        row = await session.get(MediaEvidenceRow, evidence_id)
        assert row is not None, "响应说记了但库里没有"
        assert row.kind == "image"
        assert row.media_ref.startswith("a1b2c3.jpg") or "a1b2c3" in row.media_ref
        assert row.description is None, "入库时不应该有描述"


async def test_image_plus_text_still_replies(logged_in: TestEnv) -> None:
    """带文字的图片：文本轮次照常，媒体同时登记。

    媒体登记失败不应该让"这图什么意思"也聊不下去。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _grant_inbound(logged_in)

    body = (
        await _post(
            logged_in,
            _event_body(
                message=f"这图什么意思{_IMAGE_CQ}",
                raw_message=f"这图什么意思{_IMAGE_CQ}",
            ),
        )
    ).json()

    assert body["accepted"] is True, body
    assert body["delivered"] is True, body
    assert len(body["media_evidence_ids"]) == 1, body
    assert fake.sent and fake.sent[0]["action"] == "send_private_msg"


async def test_media_does_not_block_text_when_unauthorized(logged_in: TestEnv) -> None:
    """没授权时媒体记不了，但文字照样要能聊——不能因为图被拒就整条不理。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _grant_inbound(logged_in, granted=False)

    body = (
        await _post(
            logged_in,
            _event_body(
                message=f"这图什么意思{_IMAGE_CQ}",
                raw_message=f"这图什么意思{_IMAGE_CQ}",
            ),
        )
    ).json()

    assert body["accepted"] is True, body
    assert body["delivered"] is True, body
    assert body["media_evidence_ids"] == []
    assert body["media_note"] and INBOUND_SCOPE in body["media_note"]


async def test_send_includes_image_segments(logged_in: TestEnv) -> None:
    """带媒体的出站消息必须真的把 image 段发出去。

    原实现只发 build_text_segments(text)——media_refs 被静默丢弃，
    投递却照拿回执。这条测试盯的就是那个洞。
    """
    from ling.ports.channel import OutboundMessage

    fake = FakeOneBot()
    connector = _install(logged_in, fake)

    await connector.send(
        OutboundMessage(
            operation_id="op_test",
            window_ref=f"{CFG.instance_id}:private:{QQ_USER}",
            text="给你看张图",
            media_refs=["https://example.test/a.jpg"],
        )
    )

    assert fake.sent, "没有发出任何请求"
    body = fake.sent[0]["body"]
    segments = body["message"]
    assert [s["type"] for s in segments] == ["text", "image"], segments
    assert segments[1]["data"]["file"] == "https://example.test/a.jpg"


# ------------------------------------------------------------------ 自主选择

# 她能自己决定回不回：群里普通聊天、私聊都交给模型判断。
# 这一条是给"Web 与 QQ 共用同一入口"收尸的——只测 Web 不够，
# QQ 路径以前连模型都不问。


class _ChoiceRouter:
    """只拦截"回不回"的选择调用，其余原样委托给真实路由。

    为什么要委托：轮次生成也走 router，整体替换会让回复路径失效。
    按系统提示鉴别是哪一类调用，是最直接的区分方式。
    """

    def __init__(self, inner, *, respond: bool, reason: str = "测试用理由") -> None:
        self._inner = inner
        self._respond = respond
        self._reason = reason
        self.choice_calls: list[dict] = []

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003
        is_choice = bool(messages) and "只输出一个 JSON 对象" in (messages[0].content or "")
        if not is_choice:
            async for item in self._inner.stream(
                config, profile=profile, messages=messages, **kwargs
            ):
                yield item
            return
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        # 记下 messages：断言"prompt 里有没有关系"需要看真实内容，
        # 只记 profile 断不了这类事。
        self.choice_calls.append({"profile": profile, "messages": messages})
        payload = json.dumps(
            {"respond": self._respond, "reason": self._reason, "length": "normal"},
            ensure_ascii=False,
        )
        yield ModelChunk(delta=payload), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=payload)


async def test_qq_private_can_choose_not_to_reply(logged_in: TestEnv) -> None:
    """QQ 私聊：模型说不回，就真的不发。**

    以前这条路径由确定性规则决定"私聊必回"，模型根本不参与。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=False, reason="不想接")
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _event_body())).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert stub.choice_calls, "QQ 路径没有问模型 —— 接线断了"
    assert body["accepted"] is True
    assert body["decision"] == "silence", body
    assert not fake.sent, "决定不回了却仍然发了消息"


async def test_qq_private_reply_when_model_says_so(logged_in: TestEnv) -> None:
    """模型说要回，就正常回——证明不是"永远沉默"。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=True, reason="想接话")
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _event_body())).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert stub.choice_calls, "QQ 路径没有问模型"
    assert body["accepted"] is True
    assert body["decision"] == "reply", body
    assert body["delivered"] is True
    assert fake.sent, "说要回却没有发出消息"


def _group_event(message: str) -> bytes:
    return _event_body(
        message_type="group",
        group_id=int(GROUP),
        message=message,
        raw_message=message,
    )


async def test_qq_group_plain_message_defers_attention(logged_in: TestEnv) -> None:
    """群里没点名的普通闲聊：**先不决策**，落库标 unseen 等她刷到。

    用户要求"人不是整天都看着群"。改之前这条会立刻进决策（问模型要不要接话），
    等于她一直在逐条扫描每个群。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=True, reason="想接话")
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _group_event("今天好热"))).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert body["decision"] == "deferred_attention", body
    assert body["handled_as"] is None, "还没看见就不该有处置结果"
    assert stub.choice_calls == [], "普通群消息不该立刻问模型"
    assert not fake.sent, "没看见就不该发消息"

    async with session_scope() as session:
        row = (
            await session.execute(select(Message).where(Message.text == "今天好热"))
        ).scalars().first()
    assert row is not None, "消息必须落库——她迟早要看到"
    assert row.attention_state == "unseen"
    assert row.handled_as is None


async def test_qq_group_mention_still_decides_immediately(logged_in: TestEnv) -> None:
    """被 @ 仍然立刻进入决策——"有人 @ 就看一下"。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=True, reason="叫我了")
    logged_in.app.state.router = stub
    try:
        body = (
            await _post(
                logged_in,
                _event_body(
                    message_type="group",
                    group_id=int(GROUP),
                    message="ling 在吗",
                    raw_message="ling 在吗",
                ),
            )
        ).json()
    finally:
        logged_in.app.state.router = stub._inner

    # 注意这里**不该**断言"问了模型"。默认 `group_mention_requires_reply=True`
    # 时答案是确定的（必须回），问模型"要不要回"没有意义——省一次调用。
    # 要验证的是"没被延后到注意力队列"。
    assert body["decision"] == "reply", body
    assert body["delivered"] is True
    assert body["handled_as"] == "replied"


async def test_qq_group_urgent_still_decides_immediately(logged_in: TestEnv) -> None:
    """群里的紧急内容也不延后——不能因为"没在看群"把求助放着。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=False, reason="先不插话")
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _group_event("救命 有人能帮忙吗"))).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert stub.choice_calls, "紧急内容被延后了"
    assert body["decision"] != "deferred_attention", body


async def test_qq_silence_still_persists_the_message(logged_in: TestEnv) -> None:
    """**沉默不等于没看见**：不回也要把消息落库。

    以前 QQ 路径判定沉默时是在落库**之前**就 return 的，于是
    "她选择不回"和"她根本没收到"在外面完全一样，她也不记得
    有人跟她说过话。这条测试盯的就是那个洞。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=False, reason="只是闲聊")
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _event_body(message="今天好热啊"))).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert body["decision"] == "silence"
    assert body["handled_as"] == "ignored"
    assert not fake.sent

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Message).where(Message.text == "今天好热啊")
            )
        ).scalars().all()
    assert rows, "沉默时消息丢了 —— 她不会记得有人说过话"
    assert rows[0].handled_as == "ignored"
    assert rows[0].handled_reason == "只是闲聊"
    assert rows[0].turn_id is None, "没回就不该捏造一个轮次"


async def test_qq_reply_marks_handled_as_replied(logged_in: TestEnv) -> None:
    """回的路径也要留处置记录，否则两种情况的对比无从谈起。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=True, reason="想接话")
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _event_body(message="在吗"))).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert body["handled_as"] == "replied"

    async with session_scope() as session:
        row = (
            await session.execute(select(Message).where(Message.text == "在吗"))
        ).scalars().first()
    assert row is not None and row.handled_as == "replied"


# ------------------------------------------------------------------ 分段投递

# 「不是一次全倒出去」：一条逻辑回复拆成几段，每段独立投递，
# **只有上一段拿到真实回执，才发下一段**。

# 需要足够长才会真的分段：分段算法按"句子边界 + 目标长度"合并，
# 太短的话发一条是**正确行为**，不是没生效。
_LONG_REPLY = (
    "我今天下午去了一趟图书馆，借了两本书。一本是讲城市史的，"
    "讲的是老城区怎么一步步长起来的，读起来比想象中有意思。"
    "另一本是你上次提过的那个作者的新书，我顺手也拿了，"
    "等看完了可以跟你聊聊他写的那些细节。回来路上买了点菜，"
    "晚上打算做番茄牛腩，炖久一点更入味，你要是想吃我可以多做一份。"
    "对了，你之前说的那件事我后来想了想，觉得还是先别急，等下周再看情况。"
)


class _LongReplyRouter:
    """拦截生成调用，返回一段**足够长**的文本以触发分段。

    只改生成，其余（选择、工具等）委托真实路由，避免把整条链路替换掉。
    """

    def __init__(self, inner, *, choice: bool = True, long_text: str) -> None:
        self._inner = inner
        self._choice = choice
        self._long = long_text
        self.generation_calls = 0

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        is_choice = bool(messages) and "只输出一个 JSON 对象" in (messages[0].content or "")
        if is_choice:
            if not self._choice:
                async for item in self._inner.stream(
                    config, profile=profile, messages=messages, **kwargs
                ):
                    yield item
                return
            payload = json.dumps(
                {"respond": True, "reason": "想细说", "length": "extended"},
                ensure_ascii=False,
            )
            yield ModelChunk(delta=payload), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=payload)
            return
        # 生成：吐一段长文本
        self.generation_calls += 1
        yield ModelChunk(delta=self._long), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=self._long)


async def test_private_reply_is_split_and_reassembles(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """私聊的长回复分多条发出，且**拼起来等于完整原稿**。

    改之前：`split_semantic` 只被用来算 SSE 里的 ``chunks: N``，
    那个数字是装饰性的——消息仍然作为一条发出去。
    """
    import ling.capabilities.chunking as chunking

    # 段间停顿是给真人看的节奏；测试里不要等
    monkeypatch.setattr(chunking, "PART_DELAY_SECONDS", 0.0)

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _LongReplyRouter(logged_in.app.state.router, long_text=_LONG_REPLY)
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _event_body(message="今天过得怎么样"))).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert body["accepted"] is True, body
    assert stub.generation_calls >= 1, "生成链路没被走到"
    assert body["parts_total"] >= 2, f"长回复没有分段：{body}"
    assert body["parts_sent"] == body["parts_total"], body
    assert len(fake.sent) == body["parts_total"], "每条段各应有一次发送请求"

    sent_parts = [call["body"]["message"] for call in fake.sent]
    rejoined = "".join(
        seg["data"]["text"] for part in sent_parts for seg in part if seg["type"] == "text"
    )
    assert rejoined == _LONG_REPLY, f"分段拼不回原稿：{rejoined[:60]!r}"


async def test_group_never_splits(logged_in: TestEnv) -> None:
    """群聊恒为 1 段——拆成多条会绕过防刷屏上限。"""
    from ling.capabilities.chunking import plan_parts

    assert len(plan_parts(_LONG_REPLY, is_group=True)) == 1
    assert len(plan_parts(_LONG_REPLY, is_group=False)) >= 1


async def test_part_two_not_sent_when_part_one_fails(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第一段没拿到成功回执 → **第二段绝不能发**。

    "不是一次全倒出去"的前提是逐段确认。如果第一段就失败了还继续发，
    等于把一段失败变成两段噪音。
    """
    import ling.capabilities.chunking as chunking

    monkeypatch.setattr(chunking, "PART_DELAY_SECONDS", 0.0)

    fake = FakeOneBot(fail_after=1)  # 第 1 次发送失败
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _LongReplyRouter(logged_in.app.state.router, long_text=_LONG_REPLY)
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _event_body(message="今天过得怎么样"))).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert body["parts_total"] >= 2, body
    assert body["parts_sent"] == 0, f"第一段失败却记了送达：{body}"
    assert len(fake.sent) == 1, f"第一段失败后仍然发了 {len(fake.sent)} 条"


async def test_delay_sits_between_real_receipts(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """段间停顿发生在**上一段拿到回执之后**，且它只是节奏、不是回执。"""
    import ling.capabilities.chunking as chunking

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(chunking, "PART_DELAY_SECONDS", 1.2)
    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _LongReplyRouter(logged_in.app.state.router, long_text=_LONG_REPLY)
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _event_body(message="今天过得怎么样"))).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert body["parts_sent"] == body["parts_total"] >= 2
    assert slept.count(1.2) == body["parts_total"] - 1, (
        f"段间停顿次数不对：slept={slept}"
    )


async def test_group_attention_can_be_turned_off(logged_in: TestEnv) -> None:
    """关掉开关就恢复"每条群消息都即时判断"（有人可能不想要延后）。"""
    from ling.infra.db import session_scope as _ss
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    async with _ss() as session:
        svc = ConfigService(
            session, SecretBox(logged_in.settings),
            test_mode_default=logged_in.settings.test_mode,
            persona_id=logged_in.settings.persona_id,
        )
        draft = await svc.save_draft(dialogue={"group_attention_deferred": False})
        await svc.publish(draft.revision)

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=False, reason="不插话")
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _group_event("今天好热"))).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert body["decision"] != "deferred_attention", body
    assert stub.choice_calls, "关掉延后却没有问模型"


# ------------------------------------------------------------------ 未读队列


async def _msg_row(text: str):  # noqa: ANN202
    async with session_scope() as session:
        return (
            await session.execute(select(Message).where(Message.text == text))
        ).scalars().first()


async def test_unseen_marks_and_leaves_queue(logged_in: TestEnv) -> None:
    """`mark_seen` 之后这些消息离开未读队列，且**是有范围地**标记。"""
    from ling.domains.conversation.service import ConversationService

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=False, reason="x")
    logged_in.app.state.router = stub
    try:
        # 必须用不同的 message_id，否则第二条会被当成重复事件丢掉
        first = _event_body(
            message_type="group", group_id=int(GROUP), message_id=8001,
            message="群里的第一句", raw_message="群里的第一句",
        )
        second = _event_body(
            message_type="group", group_id=int(GROUP), message_id=8002,
            message="群里的第二句", raw_message="群里的第二句",
        )
        await _post(logged_in, first)
        await _post(logged_in, second)
    finally:
        logged_in.app.state.router = stub._inner

    row1 = await _msg_row("群里的第一句")
    row2 = await _msg_row("群里的第二句")
    assert row1 is not None and row2 is not None
    assert row1.attention_state == "unseen"

    async with session_scope() as session:
        svc = ConversationService(session, actor_id="actor_ling")
        unseen = await svc.unseen_messages(row1.window_id)
        texts = [m.text for m in unseen]
        assert "群里的第一句" in texts and "群里的第二句" in texts

        # 只标其中一条：看见必须是**有范围**的事实
        changed = await svc.mark_seen(row1.window_id, message_ids=[row1.id])
        assert changed == 1
        left = [m.text for m in await svc.unseen_messages(row1.window_id)]
        assert "群里的第一句" not in left
        assert "群里的第二句" in left


async def test_legacy_null_attention_is_not_unseen(logged_in: TestEnv) -> None:
    """历史记录 attention_state 为 NULL 的**不算未读**。

    那时还没有这个字段，无法判断。武断当成未读会让她突然"补看"一批老消息。
    """
    from ling.domains.conversation.service import ConversationService
    from ling.infra.models import Window

    created = await logged_in.client.post("/v1/sessions", json={"title": "历史"})
    window_id = created.json()["session_id"]

    async with session_scope() as session:
        window = await session.get(Window, window_id)
        assert window is not None
        session.add(
            Message(
                id="msg_legacy_test",
                window_id=window_id,
                role="user",
                text="很久以前的一条",
                sequence=99999,
                state="completed",
                attention_state=None,
            )
        )

    async with session_scope() as session:
        svc = ConversationService(session, actor_id="actor_ling")
        unseen = await svc.unseen_messages(window_id)
    assert all(m.text != "很久以前的一条" for m in unseen), "历史消息被当成未读了"


# ------------------------------------------------------------------ @ 等补充正文

# QQ 里「先 @ 再发正文」很常见：立刻决策只能看到孤零零一个 @。
_AT_ONLY = "[CQ:at,qq=10001]"


async def test_group_at_without_text_waits_for_supplement(logged_in: TestEnv) -> None:
    """只有 @ 没有正文：不立刻决策，落库并安排 8 秒后看。"""
    from ling.domains.attention.service import (
        MENTION_HARD_DEADLINE_SECONDS,
        MENTION_WAIT_SECONDS,
    )
    from ling.infra.models import AttentionCursorRow

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=True, reason="x")
    logged_in.app.state.router = stub
    try:
        body = (
            await _post(
                logged_in,
                _event_body(
                    message_type="group", group_id=int(GROUP),
                    message=_AT_ONLY, raw_message=_AT_ONLY,
                ),
            )
        ).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert body["decision"] == "deferred_attention", body
    assert "补充" in body["reason"]
    assert stub.choice_calls == [], "只有 @ 就立刻问模型了"
    assert not fake.sent

    async with session_scope() as session:
        cursor = await session.get(AttentionCursorRow, body.get("window_id") or "")
        if cursor is None:
            from sqlalchemy import select as _select

            cursor = (await session.execute(_select(AttentionCursorRow))).scalars().first()
        assert cursor is not None, "没有建立调度游标"
        assert cursor.mention_deadline is not None, "没有硬截止"
        gap = (cursor.next_due_at - cursor.mentioned_at) if False else None  # noqa: F841
        now = __import__("datetime").datetime.now(__import__("datetime").UTC)
        assert 0 < (cursor.next_due_at - now).total_seconds() <= MENTION_WAIT_SECONDS + 2
        assert (
            0 < (cursor.mention_deadline - now).total_seconds()
            <= MENTION_HARD_DEADLINE_SECONDS + 2
        )
        assert cursor.focused_until is not None, "被 @ 应进入聚焦"


async def test_repeated_at_does_not_extend_forever(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连续被 @ 不能把等待无限推后——只允许延长到第一次的硬截止。"""
    from datetime import UTC, datetime, timedelta

    from ling.domains.attention.service import GroupAttentionScheduler
    from ling.infra.models import AttentionCursorRow

    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "连点群"})
    ).json()["session_id"]
    from sqlalchemy import update as _update

    from ling.infra.models import Window as _W

    async with session_scope() as session:
        await session.execute(
            _update(_W).where(_W.id == window_id).values(visibility="group_scope")
        )

    t0 = datetime.now(UTC)
    async with session_scope() as session:
        s = GroupAttentionScheduler(session, actor_id="actor_ling")
        first_due = await s.await_supplement(window_id, now=t0)
    async with session_scope() as session:
        row = await session.get(AttentionCursorRow, window_id)
        deadline = row.mention_deadline

    # 5 秒后又 @ 一次
    async with session_scope() as session:
        s = GroupAttentionScheduler(session, actor_id="actor_ling")
        second_due = await s.await_supplement(window_id, now=t0 + timedelta(seconds=5))
    async with session_scope() as session:
        row = await session.get(AttentionCursorRow, window_id)
        assert row.mention_deadline == deadline, "硬截止被推后了"

    assert second_due <= deadline, "等待超过了硬截止"
    assert first_due <= deadline


async def test_supplement_is_read_in_same_batch(logged_in: TestEnv) -> None:
    """补充的正文要和 @ 一起被看到——这才是"等补充"的意义。"""
    from sqlalchemy import update as _update

    from ling.domains.attention.service import GroupAttentionScheduler
    from ling.infra.models import Window as _W

    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "补充群"})
    ).json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            _update(_W).where(_W.id == window_id).values(visibility="group_scope")
        )

    # @ 本身（无正文）+ 紧接着的正文
    async with session_scope() as session:
        from ling.infra.models import Message as _M

        session.add(_M(id="msg_at_only", window_id=window_id, role="user",
                       text="", sequence=1, state="completed",
                       attention_state="unseen"))
        session.add(_M(id="msg_at_body", window_id=window_id, role="user",
                       text="你上次说的那个事怎么样了", sequence=2, state="completed",
                       attention_state="unseen"))

    seen: list[str] = []

    async def chooser(batch: str, speaker_subject_id: str | None = None):  # noqa: ANN202
        seen.append(batch)
        from types import SimpleNamespace

        return SimpleNamespace(respond=True, reason="叫我了")

    async with session_scope() as session:
        r = await GroupAttentionScheduler(session, actor_id="actor_ling").poll_window(
            window_id, chooser=chooser
        )

    assert r.llm_calls == 1, "应只调一次"
    assert len(seen) == 1
    assert "那个事怎么样了" in seen[0], "补充的正文没进去"
    assert r.decision == "replied"


async def test_mention_only_batch_gets_a_hint(logged_in: TestEnv) -> None:  # noqa: ARG001
    """只有 @ 没有正文时，给决策一句说明，而不是空字符串。"""
    from sqlalchemy import update as _update

    from ling.domains.attention.service import MENTION_ONLY_HINT, GroupAttentionScheduler
    from ling.infra.models import Message as _M
    from ling.infra.models import Window as _W

    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "空@群"})
    ).json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            _update(_W).where(_W.id == window_id).values(visibility="group_scope")
        )
        session.add(_M(id="msg_at_empty", window_id=window_id, role="user",
                       text="", sequence=1, state="completed",
                       attention_state="unseen"))

    seen: list[str] = []

    async def chooser(batch: str, speaker_subject_id: str | None = None):  # noqa: ANN202
        seen.append(batch)
        from types import SimpleNamespace

        return SimpleNamespace(respond=False, reason="只有 @ 没内容")

    async with session_scope() as session:
        await GroupAttentionScheduler(session, actor_id="actor_ling").poll_window(
            window_id, chooser=chooser
        )
    assert seen and seen[0] == MENTION_ONLY_HINT


# ---------------------------------------------------------------- 关系进决策


def _prompt_text(call: dict) -> str:
    return "\n".join(m.content for m in call["messages"])


async def test_private_choice_includes_relationship(logged_in: TestEnv) -> None:
    """私聊的判断依据要带"我和这个人的关系"。

    原来关系只影响"说什么"（上下文里有 external_hint），不影响"要不要说"。
    对熟人更愿意搭理是真实的人类行为。
    """
    from ling.domains.relationship.service import RelationshipService
    from ling.infra.db import session_scope

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    async with session_scope() as session:
        svc = RelationshipService(session, actor_id="actor_ling")
        state = await svc.state(logged_in.subject_id)
        state.familiarity = 0.8  # 熟人

    stub = _ChoiceRouter(logged_in.app.state.router, respond=False, reason="先不回")
    logged_in.app.state.router = stub
    try:
        await _post(logged_in, _event_body(message="在吗"))
    finally:
        logged_in.app.state.router = stub._inner

    assert stub.choice_calls, "没问模型"
    prompt = _prompt_text(stub.choice_calls[0])
    assert "我和这个人的关系" in prompt, f"私聊没带关系：{prompt[:300]}"
    assert "熟悉" in prompt, prompt[:300]


async def test_group_choice_omits_relationship(logged_in: TestEnv) -> None:
    """群聊**不能**带关系。

    群窗口的 `subject_id` 指向的是角色的主人，而同一批消息可能来自不同的人。
    把"对主人的熟悉度"当成"对说话人的熟悉度"是错误归属——
    宁可空着，不要给她一个错的关系判断。
    """
    from ling.domains.relationship.service import RelationshipService
    from ling.infra.db import session_scope

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    async with session_scope() as session:
        svc = RelationshipService(session, actor_id="actor_ling")
        state = await svc.state(logged_in.subject_id)
        state.familiarity = 0.9

    # 默认 `group_mention_requires_reply=True` 时 @ 必回，模型根本不被问——
    # 那样这条测试就是空的（第一次写出来就是这个毛病）。关掉它让模型参与。
    await _set_group_mention_not_required(logged_in)

    stub = _ChoiceRouter(logged_in.app.state.router, respond=False, reason="先不回")
    logged_in.app.state.router = stub
    try:
        await _post(
            logged_in,
            _event_body(
                message_type="group", group_id=int(GROUP),
                message="ling 在吗", raw_message="ling 在吗",
            ),
        )
    finally:
        logged_in.app.state.router = stub._inner

    assert stub.choice_calls, "群聊策略已放开，模型却仍没被问"
    prompt = _prompt_text(stub.choice_calls[0])
    assert "我和这个人的关系" not in prompt, (
        f"群聊带上了关系——那是错误归属：{prompt[:300]}"
    )


async def _set_group_mention_not_required(env: TestEnv) -> None:
    from ling.infra.db import session_scope as _ss
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    async with _ss() as session:
        svc = ConfigService(
            session,
            SecretBox(env.settings),
            test_mode_default=env.settings.test_mode,
            persona_id=env.settings.persona_id,
        )
        draft = await svc.save_draft(dialogue={"group_mention_requires_reply": False})
        await svc.publish(draft.revision)


# ---------------------------------------------------------------- 出入许可


async def test_stranger_is_rejected_before_any_write(logged_in: TestEnv) -> None:
    """名单外的人：200 + accepted=false，且**不建窗口、不留消息**。

    在边界上拦，不是在下游拦——否则会建窗口、建会话、跑决策、调模型，
    全流程走完才发现不该理。
    """
    from sqlalchemy import func, select

    from ling.infra.db import session_scope
    from ling.infra.models import Message, Window

    logged_in.app.state.settings = _replace_settings(
        logged_in, access_mode="whitelist", access_list=frozenset({"10001"})
    )
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    async with session_scope() as session:
        before = (await session.execute(select(func.count(Window.id)))).scalar()

    body = (
        await _post(
            logged_in,
            _event_body(message_id=91001, user_id=55555, message="陌生人来了"),
        )
    ).json()

    assert body["accepted"] is False, body
    assert "白名单" in body["reason"], body
    assert body["sender"] == "55555"

    async with session_scope() as session:
        after = (await session.execute(select(func.count(Window.id)))).scalar()
        msgs = (
            await session.execute(
                select(func.count(Message.id)).where(Message.text == "陌生人来了")
            )
        ).scalar()
    assert after == before, "名单外的人不该建窗口"
    assert msgs == 0, "名单外的人不该留下消息记录"
    assert not fake.sent, "不该给名单外的人发任何东西"


async def test_whitelisted_sender_is_processed(logged_in: TestEnv) -> None:
    # 名单里放**已绑定的身份**（QQ_USER），否则会卡在身份绑定而不是许可
    logged_in.app.state.settings = _replace_settings(
        logged_in, access_mode="whitelist", access_list=frozenset({QQ_USER})
    )
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    body = (
        await _post(
            logged_in,
            _event_body(message_id=91002, user_id=int(QQ_USER), message="我在名单里"),
        )
    ).json()
    assert body["accepted"] is True, body


def _replace_settings(env: TestEnv, **changes):  # noqa: ANN003, ANN202
    """复制一份 settings 换掉访问相关字段。

    Settings 是 frozen dataclass，只能整份替换；直接改字段会抛异常。
    """
    import dataclasses

    return dataclasses.replace(env.app.state.settings, **changes)


# ---------------------------------------------------------------- WS 入站


def _ws_client(env: TestEnv):
    from connectors.onebot.connector import OneBotV11Connector
    from connectors.onebot.ws_client import OneBotWsClient

    from ling.capabilities.channels import onebot_config

    return OneBotWsClient(
        url="ws://127.0.0.1:1", app=env.app, connector=OneBotV11Connector(onebot_config())
    )


async def test_ws_bad_json_is_dropped_not_raised(logged_in: TestEnv) -> None:
    """坏帧只丢一条，不能让连接崩掉。

    单条消息处理失败就断开重连的话，同一条坏消息会把连接打成死循环
    —— NapCat 会一直重推，我们一直断。
    """
    client = _ws_client(logged_in)
    assert await client.handle_raw("这不是 json") is None
    assert await client.handle_raw(b"{not json}") is None
    assert await client.handle_raw("[1,2,3]") is None  # 不是对象


async def test_ws_event_goes_through_the_same_intake(logged_in: TestEnv) -> None:
    """WS 收到的消息必须走**同一套**受理（白名单/去重/身份/决策/投递）。

    两条入站分两份实现的话，迟早有一条忘了某个检查，而且是静默的。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    logged_in.app.state.settings = _replace_settings(
        logged_in, access_mode="whitelist", access_list=frozenset({"99999"})
    )

    client = _ws_client(logged_in)
    import json as _json

    result = await client.handle_raw(
        _json.dumps(
            {
                "time": int(datetime.now(UTC).timestamp()),
                "self_id": 10001,
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": 92001,
                "user_id": 88888,
                "message": [{"type": "text", "data": {"text": "陌生人"}}],
                "raw_message": "陌生人",
                "font": 0,
                "sender": {"nickname": "路人", "sex": "unknown", "age": 0},
            }
        )
    )
    assert result is not None
    assert result["accepted"] is False, result
    assert "白名单" in result["reason"], result
    assert client.received == 1


async def test_ws_meta_event_is_ignored_quietly(logged_in: TestEnv) -> None:
    """lifecycle/connect 这类事件要接受但不当消息处理。"""
    client = _ws_client(logged_in)
    import json as _json

    result = await client.handle_raw(
        _json.dumps(
            {
                "time": int(datetime.now(UTC).timestamp()),
                "self_id": 10001,
                "post_type": "meta_event",
                "meta_event_type": "lifecycle",
                "sub_type": "connect",
            }
        )
    )
    assert result is not None
    assert result["accepted"] is False
    assert "meta_event" in result["reason"], result


# ---------------------------------------------------------------- 多用户识别


async def test_group_stranger_is_provisioned_as_known(logged_in: TestEnv) -> None:
    """群里没走过绑定的人：自动建 known 级 subject，**能听见**。

    原来是整条被拒（"渠道账号尚未绑定到已证明的自然人"），等于她在有别人的
    群里是聋的。现在建一个"只认识账号"的 subject：记忆只限本群、跨窗召回不开放。
    """
    from sqlalchemy import select

    from ling.infra.db import session_scope
    from ling.infra.models import ActorAccount, Subject

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    await _post(
        logged_in,
        _event_body(
            message_type="group", group_id=int(GROUP), user_id=770011,
            message="群里新来的", raw_message="群里新来的",
            sender={"nickname": "小明", "sex": "unknown", "age": 0},
        ),
    )

    async with session_scope() as session:
        acct = (
            await session.execute(
                select(ActorAccount).where(ActorAccount.external_account_id == "770011")
            )
        ).scalars().first()
    assert acct is not None, "群里未绑定的人没被建成 subject，消息会被丢掉"
    assert acct.proof == "pending", "自动建的不该宣称已证明"
    async with session_scope() as session:
        subj = await session.get(Subject, acct.subject_id)
    assert subj is not None and subj.trust_level == "known"
    # 昵称要能取到，否则她只能叫对方「群友」，叫不出名字
    assert subj.display_name == "小明", (
        f"发送者昵称没进信封，只能叫{subj.display_name!r}"
    )


async def test_two_people_with_same_nickname_are_two_subjects(logged_in: TestEnv) -> None:
    """两个不同 QQ 号、同样叫「小明」→ 必须是**两个** subject，记忆互不可见。

    同名不同人不是理论风险：QQ 群昵称可以随便改，也可以完全重名。
    身份键是账号，不是名字。
    """
    from sqlalchemy import select

    from ling.infra.db import session_scope
    from ling.infra.models import ActorAccount

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    for qq in (880001, 880002):
        await _post(
            logged_in,
            _event_body(
                message_type="group", group_id=int(GROUP), user_id=qq,
                message_id=qq, message="我叫小明", raw_message="我叫小明",
                sender={"nickname": "小明", "sex": "unknown", "age": 0},
            ),
        )

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ActorAccount).where(
                    ActorAccount.external_account_id.in_(["880001", "880002"])
                )
            )
        ).scalars().all()
    assert len(rows) == 2, f"两个账号被合并成一个了：{rows}"
    assert rows[0].subject_id != rows[1].subject_id, "同名不同人拿到了同一个 subject"


async def test_group_window_belongs_to_owner_not_first_speaker(logged_in: TestEnv) -> None:
    """群窗口归属**主人**，不是"谁先说话谁拥有这个群"。

    原来用发言人的 subject 建群窗口，第一个说话的人（可能是陌生人）就成了
    窗口主人，之后任何读 window.subject_id 的地方都会用错人。
    """
    from sqlalchemy import select

    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope
    from ling.infra.models import Window

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    # 让一个**未绑定**的人第一个在群里说话
    await _post(
        logged_in,
        _event_body(
            message_type="group", group_id=99001, user_id=660001,
            message="我先说话", raw_message="我先说话",
        ),
    )

    async with session_scope() as session:
        owner = await IdentityService(session).owner_subject_id()
        win = (
            await session.execute(
                select(Window).where(Window.title.contains("99001"))
            )
        ).scalars().first()
    assert win is not None, "群窗口没建出来"
    assert win.subject_id == owner, (
        f"群窗口归了发言人 {win.subject_id}，应该归主人 {owner}"
    )


async def test_memory_records_source_account(logged_in: TestEnv) -> None:
    """记忆必须记下「是哪个号说的」。

    一个自然人天然能挂多个账号（QQ 大号/小号/微信）。合并成同一个 subject
    之后，`subject_id` 只能说明"这是关于这个人的记忆"，说不出是哪个号说的——
    而没有这一项，合并就**拆不回去**：拆开时不知道该把哪条记忆还给哪个号。

    群窗口事后反推不出来（一个窗口里很多人），所以必须写入时就记。
    """
    from sqlalchemy import select

    from ling.infra.db import session_scope
    from ling.infra.models import MemoryRow
    from ling.worker import handle_memory_extract

    await _bind(logged_in)

    # 直接跑提取任务，payload 里带来源账号（与 bridge 透传的形状一致）
    await handle_memory_extract(
        {
            "subject_id": logged_in.subject_id,
            "window_id": "win_src_acct",
            "turn_id": "turn_src_acct",
            "user_text": "我叫阿澄",
            "source_visibility": "subject_private",
            "source_account": "napcat-2:10001",
        }
    )

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(MemoryRow).where(MemoryRow.turn_id == "turn_src_acct")
            )
        ).scalars().all()
    assert rows, "没提取出记忆，这条测试失去意义"
    assert all(r.source_account == "napcat-2:10001" for r in rows), (
        f"记忆没记来源账号：{[r.source_account for r in rows]}"
    )


async def test_bridge_puts_source_account_into_memory_outbox(logged_in: TestEnv) -> None:
    """QQ 一轮走完之后，记忆任务的 payload 里必须带来源账号。

    上一条测试直接构造 payload，绕过了 bridge——负例没响就说明它没覆盖接线。
    这条走真实链路：发一条 QQ 消息，然后读 outbox 里 memory.extract 的 payload。
    """
    import json as _json

    from sqlalchemy import select

    from ling.infra.db import session_scope
    from ling.infra.models import OutboxEvent

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    await _post(logged_in, _event_body(message="记住我叫阿澄"))

    async with session_scope() as session:
        events = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.topic == "memory.extract")
            )
        ).scalars().all()
    assert events, "没有产生记忆任务"
    payload = events[-1].payload
    if isinstance(payload, str):
        payload = _json.loads(payload)
    assert payload.get("source_account"), (
        f"记忆任务里没有来源账号，合并后无法拆回：{payload}"
    )
    assert QQ_USER in str(payload["source_account"]), payload["source_account"]


# ---------------------------------------------------------------- 合并与拆分


async def test_merge_is_lossless_and_reversible(logged_in: TestEnv) -> None:
    """合并只加指针，数据一个字节都不动；拆开立刻还原。

    为什么要这样：按账号把记忆"搬"到一起，一行写错就再也分不清谁是谁。
    合并成"视图"之后，拆开只是清一个字段——这是唯一敢让人手动操作的形态。
    """
    from sqlalchemy import select

    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope
    from ling.infra.models import MemoryRow

    owner = logged_in.subject_id
    async with session_scope() as session:
        ident = IdentityService(session)
        b = await ident.resolve_or_provision("napcat-2", "5550001", display_name="小号")
        assert b is not None
        # B 名下先写一条记忆
        from ling.contracts.v1 import Origin, Visibility
        from ling.domains.memory.service import MemoryProposal

        await ident._session.flush()
        from ling.domains.memory.service import MemoryService

        await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="p_b1", kind="assertion", text="用户叫小号君",
                origin=Origin.user_statement, source_event_ids=["e1"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=b, source_account="napcat-2:5550001",
        )

    # 合并前：两个 subject 各自独立
    async with session_scope() as session:
        ident = IdentityService(session)
        assert await ident.canonical_subject(b) == b
        assert await ident.subject_group(owner) == {owner}

    async with session_scope() as session:
        out = await IdentityService(session).merge_subjects(b, owner)
    assert out["moved"] == 0, "合并动了数据——应该只写指针"

    async with session_scope() as session:
        ident = IdentityService(session)
        assert await ident.canonical_subject(b) == owner, "解析没跟随合并指针"
        assert (await ident.subject_group(owner)) == {owner, b}, "召回集合没含进来"
        # 记忆**仍在 B 名下**（没被搬动）
        rows = (
            await session.execute(select(MemoryRow).where(MemoryRow.text == "用户叫小号君"))
        ).scalars().all()
        assert rows and rows[0].subject_id == b, "记忆被搬走了，那就拆不干净了"

    # 拆开：指针清掉，一切回到原样
    async with session_scope() as session:
        await IdentityService(session).unmerge_subject(b)
    async with session_scope() as session:
        ident = IdentityService(session)
        assert await ident.canonical_subject(b) == b
        assert await ident.subject_group(owner) == {owner}


async def test_unmerge_returns_memories_written_during_merge(logged_in: TestEnv) -> None:
    """合并期间写的记忆，拆开时要按 `source_account` 送回原主。

    合并那段时间解析出的主体是 target，新记忆记在 target 名下——但它带着
    "是哪个号说的"。按它归位，拆开之后两边都不串。
    """
    from sqlalchemy import select

    from ling.contracts.v1 import Origin, Visibility
    from ling.domains.identity.service import IdentityService
    from ling.domains.memory.service import MemoryProposal, MemoryService
    from ling.infra.db import session_scope
    from ling.infra.models import MemoryRow

    owner = logged_in.subject_id
    async with session_scope() as session:
        b = await IdentityService(session).resolve_or_provision(
            "napcat-2", "5550002", display_name="小号2"
        )

    async with session_scope() as session:
        await IdentityService(session).merge_subjects(b, owner)

    # 合并期间：记忆记在 owner 名下，但 source_account 指向 B 的号
    async with session_scope() as session:
        await MemoryService(session).propose(
            MemoryProposal(
                proposal_id="p_merged", kind="assertion", text="合并期间说的话",
                origin=Origin.user_statement, source_event_ids=["e2"],
                visibility=Visibility.subject_private, confidence=0.9,
            ),
            subject_id=owner, source_account="napcat-2:5550002",
        )

    async with session_scope() as session:
        out = await IdentityService(session).unmerge_subject(b)
    assert out["restored_memories"] >= 1, f"没归位：{out}"

    async with session_scope() as session:
        row = (
            await session.execute(
                select(MemoryRow).where(MemoryRow.text == "合并期间说的话")
            )
        ).scalars().first()
    assert row is not None and row.subject_id == b, (
        f"这条记忆留在 {row.subject_id}，拆开后 A 还会想起 B 的话"
    )


async def test_merge_rejects_self_and_cycle(logged_in: TestEnv) -> None:
    """自己并自己、合并成环、目标已被合并——都必须在写入前拒绝。"""
    import pytest as _pytest

    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        ident = IdentityService(session)
        a = await ident.resolve_or_provision("napcat-2", "5550010")
        b = await ident.resolve_or_provision("napcat-2", "5550011")
    assert a and b

    async with session_scope() as session:
        with _pytest.raises(ValueError):
            await IdentityService(session).merge_subjects(a, a)
    async with session_scope() as session:
        await IdentityService(session).merge_subjects(b, a)
    async with session_scope() as session:
        with _pytest.raises(ValueError):
            # a 已经是被合并的目标，不能再被并到别处（否则链成环）
            await IdentityService(session).merge_subjects(a, b)


async def test_resolve_account_follows_merge_pointer(logged_in: TestEnv) -> None:
    """合并之后，B 的账号必须解析成 A。

    这条是**生产路径**：bridge 就是靠 `resolve_account` 认人的。
    前面那条只断言 `canonical_subject`，把 resolve_account 改成不跟随指针
    也照样绿——负例没响就是证据。
    """
    from sqlalchemy import select

    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope
    from ling.infra.models import ActorAccount

    async with session_scope() as session:
        b = await IdentityService(session).resolve_or_provision(
            "napcat-2", "5550003", display_name="小号3"
        )
        owner = await IdentityService(session).owner_subject_id()
    assert b and owner and b != owner

    # `resolve_account` 只认已证明的绑定（不猜身份）。要测合并指针就得先证明，
    # 否则测到的是"未证明被拒"，不是"合并不生效"。
    async with session_scope() as session:
        acct = (
            await session.execute(
                select(ActorAccount).where(ActorAccount.external_account_id == "5550003")
            )
        ).scalars().first()
        assert acct is not None
        acct.proof = "proven"
        await session.flush()

    async with session_scope() as session:
        await IdentityService(session).merge_subjects(b, owner)

    async with session_scope() as session:
        resolved = await IdentityService(session).resolve_account("napcat-2", "5550003")
    assert resolved == owner, (
        f"B 的号解析成了 {resolved}——没跟随合并指针，合并等于没做"
    )


# ---------------------------------------------------------------- 群里的非主人


async def test_group_reply_from_non_owner_does_not_fail(logged_in: TestEnv) -> None:
    """群里**不是主人**的人说话，她也能正常回复。

    这是一个真 bug 的回归测试（2026-10-29 实测复现）：
    群窗口归属主人（这个群属于谁），而轮次是用**发言人的** subject 跑的。
    `get_window` 要求 `window.subject_id == subject_id`，于是群里任何人
    一开口，走到生成阶段就 `not_found("会话不存在")`，整轮失败——
    对外表现是 `{"decision":"reply","delivered":false,"state":"failed"}`，
    **她决定了回复却什么都没发出来**，而日志里一个字都没有。

    群窗口本来就是共享的（同一个群所有人落到一个窗口），放行不降低隐私；
    私聊窗口仍然只有本人能读（见 test_private_window_still_owner_only）。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    body = (
        await _post(
            logged_in,
            _event_body(
                message_type="group", group_id=881001, user_id=770130,
                message="救命，帮我看看这个", raw_message="救命，帮我看看这个",
                sender={"nickname": "求助的人", "sex": "unknown", "age": 0},
            ),
        )
    ).json()

    # 不必强求"决定回复"——假模型下也可能选择沉默，那是正常行为。
    # 这个 bug 的签名是：「决定回复」+ 产出 0 段 + 失败原因里带"没有可交付文本"。
    assert "没有可交付文本" not in str(body.get("reason") or ""), f"整轮失败了：{body}"
    if body.get("decision") == "reply":
        assert body.get("parts_total", 0) >= 1, f"决定了回复却产出 0 段：{body}"


async def test_group_window_is_readable_by_any_member(logged_in: TestEnv) -> None:
    """群窗口对任何成员可读——这条是上一条的确定性命中版。

    与"决定回复"不同，窗口语义是确定的：群窗口共享，私聊窗口封闭。
    """
    from sqlalchemy import update

    from ling.domains.conversation.service import ConversationService
    from ling.infra.db import session_scope
    from ling.infra.models import Window

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "群窗口共享"})
    ).json()["session_id"]
    async with session_scope() as session:
        await session.execute(
            update(Window).where(Window.id == session_id).values(visibility="group_scope")
        )

    async with session_scope() as session:
        conv = ConversationService(session, actor_id="actor_ling")
        # 不是窗口主人，但因为是群窗口，可读
        win = await conv.get_window_for_turn(session_id, subject_id="subj_any_member")
    assert win.id == session_id


async def test_private_window_still_owner_only(logged_in: TestEnv) -> None:
    """放行群窗口**不能**顺带放行私聊窗口——那是隐私边界。

    同一个人读别人的私聊窗口必须 404，而且不泄露存在性。
    """
    import pytest as _pytest

    from ling.api.errors import LingError
    from ling.domains.conversation.service import ConversationService
    from ling.infra.db import session_scope

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "私聊边界"})
    ).json()["session_id"]

    async with session_scope() as session:
        conv = ConversationService(session, actor_id="actor_ling")
        # 本人可读
        await conv.get_window_for_turn(session_id, subject_id=logged_in.subject_id)
        # 别人不可读
        with _pytest.raises(LingError):
            await conv.get_window_for_turn(session_id, subject_id="subj_someone_else")


# ---------------------------------------------------------------- 群里 @ 回应


async def test_group_reply_does_not_mention_unless_she_chooses(logged_in: TestEnv) -> None:
    """群里**不再默认 @ 发言人**——那是代码在替人设做决定。

    用户的原话：「把能怎么做告诉她，怎么做让她自己选择」。
    默认只发文字；要不要 @、要不要引用、要不要拍一拍，由她在正文里写
    `[at:...]` 之类的标记来选。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    await _post(
        logged_in,
        _event_body(
            message_type="group", group_id=881010, user_id=770140,
            message="[CQ:at,qq=10001] 帮我看看这个",
            raw_message="[CQ:at,qq=10001] 帮我看看这个",
            sender={"nickname": "求助的人", "sex": "unknown", "age": 0},
        ),
    )

    sends = [s for s in fake.sent if s["action"] == "send_group_msg"]
    assert sends, "没有发群消息"
    segments = sends[0]["body"]["message"]
    assert not [seg for seg in segments if seg.get("type") == "at"], (
        f"她没选 @ 却 @ 了：{segments}"
    )


async def test_she_can_choose_to_mention_and_it_happens(logged_in: TestEnv) -> None:
    """她写 `[at:QQ]` 就真的要 @ 出去，而且标记不能残留在正文里。

    这是「告诉她能怎么做、她自己选」的另一半：选了就要真做到。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    inner = logged_in.app.state.router

    class _MarkerRouter:
        """生成时返回带标记的文本；其余原样委托。"""

        async def stream(self, config, *, profile, messages, **kw):  # noqa: ANN001, ANN003, ARG002
            from ling.models.router import StreamOutcome
            from ling.ports.model import ModelChunk

            is_choice = bool(messages) and "只输出一个 JSON 对象" in (
                messages[0].content or ""
            )
            if is_choice:
                async for item in inner.stream(
                    config, profile=profile, messages=messages, **kw
                ):
                    yield item
                return
            payload = "[at:770140] 我在，你说"
            yield ModelChunk(delta=payload), StreamOutcome()
            yield ModelChunk(done=True), StreamOutcome(text=payload)

    logged_in.app.state.router = _MarkerRouter()
    try:
        body = (
            await _post(
                logged_in,
                _event_body(
                    message_type="group", group_id=881011, user_id=770141,
                    message="[CQ:at,qq=10001] 在吗",
                    raw_message="[CQ:at,qq=10001] 在吗",
                    sender={"nickname": "问的人", "sex": "unknown", "age": 0},
                ),
            )
        ).json()
    finally:
        logged_in.app.state.router = inner

    assert body.get("decision") == "reply", body
    sends = [s for s in fake.sent if s["action"] == "send_group_msg"]
    assert sends, "没有发群消息"
    segments = sends[0]["body"]["message"]
    ats = [seg for seg in segments if seg.get("type") == "at"]
    assert ats and ats[0]["data"]["qq"] == "770140", f"她选了 @ 却没做到：{segments}"
    texts = "".join(
        seg["data"].get("text", "") for seg in segments if seg.get("type") == "text"
    )
    assert "[at:" not in texts, f"标记残留在正文里：{texts!r}"


async def test_private_reply_has_no_mention(logged_in: TestEnv) -> None:
    """私聊不 @——那里只有一个"你"，加了是噪音。"""
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    await _post(logged_in, _event_body(message="在吗"))

    sends = [s for s in fake.sent if s["action"] == "send_private_msg"]
    assert sends, "没有发私聊消息"
    segments = sends[0]["body"]["message"]
    assert not [seg for seg in segments if seg.get("type") == "at"], (
        f"私聊加了 @：{segments}"
    )


def test_at_segment_is_a_separate_segment_not_text() -> None:
    """@ 必须是**独立段**，不能拼进文本。

    拼字符串的话，用户文本里恰好出现 `[CQ:at,qq=...]` 就会被平台执行——
    那是一条注入路径。数组格式里 text 段的内容不参与 CQ 解析。
    """
    from connectors.onebot.message import build_outbound_segments

    segments = build_outbound_segments("回你", None, ["770140"])
    assert segments[0] == {"type": "at", "data": {"qq": "770140"}}
    assert segments[1]["type"] == "text" and segments[1]["data"]["text"] == " "
    assert segments[-1] == {"type": "text", "data": {"text": "回你"}}
    # 没有 @ 时不额外插空格段
    plain = build_outbound_segments("回你", None, [])
    assert plain == [{"type": "text", "data": {"text": "回你"}}]


# ---------------------------------------------------------------- WS 服务端


def _ws_server(env: TestEnv, *, token: str | None = None):
    from connectors.onebot.connector import OneBotV11Connector
    from connectors.onebot.ws_server import OneBotWsServer

    from ling.capabilities.channels import onebot_config

    return OneBotWsServer(
        host="127.0.0.1", port=1, app=env.app,
        connector=OneBotV11Connector(onebot_config()), token=token,
    )


async def test_ws_server_shares_the_same_intake(logged_in: TestEnv) -> None:
    """WS 服务端收到的事件走**同一套**受理（白名单/去重/身份/决策/投递）。

    客户端模式与服务端模式只是"谁发起连接"的差别，受理逻辑只有一份。
    """
    import json as _json

    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    logged_in.app.state.settings = _replace_settings(
        logged_in, access_mode="whitelist", access_list=frozenset({"99999"})
    )

    server = _ws_server(logged_in)
    result = await server.handle_raw(
        _json.dumps(
            {
                "time": int(datetime.now(UTC).timestamp()),
                "self_id": 10001,
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": 93001,
                "user_id": 88880,
                "message": [{"type": "text", "data": {"text": "陌生人"}}],
                "raw_message": "陌生人",
                "font": 0,
                "sender": {"nickname": "路人", "sex": "unknown", "age": 0},
            }
        )
    )
    assert result is not None
    assert result["accepted"] is False and "白名单" in result["reason"], result
    assert server.received == 1


def test_ws_server_token_is_enforced() -> None:
    """配了 token 就必须对上，两种携带方式都认。

    反向 WS 的鉴权约定通常是 header 或 query 二选一，这里两者都接受。
    不校验的话，任何能连上这个端口的人都能往对话链路里推事件。
    """

    class _Req:
        def __init__(self, path: str, headers: dict) -> None:
            self.path = path
            self.headers = headers

    from connectors.onebot.ws_server import OneBotWsServer

    server = OneBotWsServer(
        host="127.0.0.1", port=1, app=object(), connector=object(), token="s3cret"
    )
    ok_header = _Req("/", {"Authorization": "Bearer s3cret"})
    ok_query = _Req("/?access_token=s3cret", {})
    bad = _Req("/", {"Authorization": "Bearer nope"})
    missing = _Req("/", {})
    assert server._authorized(ok_header.path, ok_header.headers)
    assert server._authorized(ok_query.path, ok_query.headers)
    assert not server._authorized(bad.path, bad.headers)
    assert not server._authorized(missing.path, missing.headers)


def test_ws_server_without_token_allows_all() -> None:
    """没配 token 时不校验（但会 warning，让"谁都能连"看得见）。"""

    class _Req:
        path = "/"
        headers: dict = {}

    from connectors.onebot.ws_server import OneBotWsServer

    server = OneBotWsServer(
        host="127.0.0.1", port=1, app=object(), connector=object(), token=None
    )
    assert server._authorized("/", {})


async def test_ws_server_bad_frame_does_not_raise(logged_in: TestEnv) -> None:
    """坏帧只丢一条，不断连接——同一条坏消息不能把连接打成重连循环。"""
    server = _ws_server(logged_in)
    assert await server.handle_raw("不是 json") is None
    assert await server.handle_raw(b"{broken") is None
    assert await server.handle_raw("[1,2]") is None
