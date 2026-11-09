"""「她决定回了、但消息没到对方手上」——**四种成因必须能分开**（端到端）。

2026-11-07 那次报障的根因是"日志说不出原因"（`turn.failed` 只读 `detail`、
router 失败不记日志、quota 记账 KeyError 刷假警告），已修，见
`test_reply_failure_diagnosis.py`。本模块处理的是**另一条路**：
模型完全正常，正文也生成了，但一个字都没到对方手上。它们以前共用同一句
「轮次结束但没有可交付文本（state=completed）」，或者更糟——对外声称
「已回复」。三条用例分别钉住：

1. 正文被出口清洗**洗空**（`<<看视频 m3>>` 之类）→ 不投递，但要说"洗空了"；
2. 发言权被**另一个窗口**占着 → 生成了 99 字却没对外交付，要说清是被压住的；
3. 分段投递**只发出去一段** → 必须说 `1/3`，不能写"已回复"。

三种情况下，那条**用户消息**都不能记成 `handled_as=replied`：库里说
"回了"、对方什么都没收到，是最难查的一种组合。
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

from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import Message, Window
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-outbound",
)
QQ_USER = "100200300"
WINDOW_REF = f"onebot-outbound:private:{QQ_USER}"


class _FakeOneBot:
    """记录出站请求；``fail_at`` 之后一律回 retcode=100（= 平台拒收）。"""

    def __init__(self, *, fail_at: int | None = None) -> None:
        self.sent: list[dict] = []
        self._fail_at = fail_at

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        action = str(request.url).rsplit("/", 1)[-1]
        self.sent.append({"action": action, "body": body})
        if self._fail_at is not None and len(self.sent) >= self._fail_at:
            return httpx.Response(
                200, json={"status": "failed", "retcode": 100, "data": None}
            )
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": 9000 + len(self.sent)},
            },
        )

    def texts(self) -> list[str]:
        out: list[str] = []
        for call in self.sent:
            out.append(
                "".join(
                    seg["data"].get("text", "")
                    for seg in call["body"].get("message") or []
                    if seg.get("type") == "text"
                )
            )
        return out


def _install(env: TestEnv, fake: _FakeOneBot) -> None:
    env.app.state.onebot_connector = OneBotV11Connector(
        CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )


async def _bind(env: TestEnv) -> None:
    async with session_scope() as session:
        await IdentityService(session).bind_account(
            subject_id=env.subject_id,
            connector_instance=CFG.instance_id,
            external_account_id=QQ_USER,
            proof="proven",
        )


def _event_body(message: str, *, message_id: int) -> bytes:
    payload = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": int(QQ_USER),
        "message": message,
        "raw_message": message,
        "font": 0,
        "sender": {"nickname": "Co"},
    }
    return json.dumps(payload).encode()


async def _post(env: TestEnv, body: bytes) -> dict:
    sig = hmac.new(b"sec", body, hashlib.sha1).hexdigest()
    response = await env.client.post(
        "/v1/onebot/events",
        content=body,
        headers={
            "X-Signature": f"sha1={sig}",
            "X-Self-ID": "10001",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _is_choice_call(messages) -> bool:  # noqa: ANN001
    return bool(messages) and "只输出一个 JSON 对象" in (messages[0].content or "")


class _TextRouter:
    """决策交给真实 router（fake provider），生成换成**固定文本**。"""

    def __init__(self, inner, text: str) -> None:  # noqa: ANN001
        self._inner = inner
        self._text = text
        self.generation_calls = 0

    @property
    def quota(self):  # noqa: ANN201
        """转发给被包裹的真 router。

        不是为了让桩"不报错"才加的：`kernel/turn.py` 会读 `router.quota` 去查
        今天的看视频额度（那是**真实**的调用路径，每轮都会走）。桩少了这个
        属性，这条路就只能测到 AttributeError，测不到它本来的行为。

        转发而不是伪造一个空账本：额度判断的语义（有账本 = 可查）必须和
        生产一致，否则"额度用完了"这条分支在集成测试里永远测不到。
        """
        return self._inner.quota

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        if _is_choice_call(messages):
            async for item in self._inner.stream(
                config, profile=profile, messages=messages, **kwargs
            ):
                yield item
            return
        self.generation_calls += 1
        for index in range(0, len(self._text), 4):
            yield ModelChunk(delta=self._text[index : index + 4]), StreamOutcome()
        yield ModelChunk(done=True, finish_reason="stop"), StreamOutcome(text=self._text)


async def _post_with_text(env: TestEnv, text: str, message: str) -> dict:
    stub = _TextRouter(env.app.state.router, text)
    env.app.state.router = stub
    try:
        body = await _post(env, _event_body(message, message_id=801))
    finally:
        env.app.state.router = stub._inner
    assert stub.generation_calls == 1, "生成没被调用，测到的不是出站链路"
    return body


async def _user_rows() -> list[Message]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(Message).where(
                        Message.role == "user", Message.text != ""
                    )
                )
            ).scalars()
        )


async def _window_id(title: str) -> str:
    async with session_scope() as session:
        row = (
            await session.execute(select(Window).where(Window.title == title))
        ).scalars().first()
    assert row is not None
    return row.id


# ---------------------------------------------------------------- 1. 洗空


async def test_washed_empty_body_is_reported_not_delivered(logged_in: TestEnv) -> None:
    """正文被出口清洗洗空：不投递（不是投空串），而且要说清是"洗空了"。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    # 用网页没开视频的标记形式：这一轮她只写了一个标记，剥完正文就成了空串，
    # 正是"被出口清洗洗空"的场景。
    #
    # 注意标记本身会触发**第二次**生成（`turn.py` 的
    # `_follow_up_after_watch`：看不到视频时也要按人设自然回一句），所以这里
    # 不能用 `_post_with_text` 的"生成恰好一次"断言——那是媒体链路出现之前
    # 的旧前提。改为直接投递、只断言**出口**的行为。
    stub = _TextRouter(logged_in.app.state.router, "<<看视频 m3>>")
    logged_in.app.state.router = stub
    try:
        body = await _post(logged_in, _event_body("那个视频呢", message_id=801))
    finally:
        logged_in.app.state.router = stub._inner
    assert stub.generation_calls >= 1, "生成没被调用，测到的不是出站链路"

    assert body["decision"] == "reply"
    assert body["delivered"] is False
    assert body["parts_total"] == 0, "洗空之后不该规划出任何段"
    assert not fake.sent, "洗空之后仍然调了 send —— 那就成了投递空串"
    reason = str(body["reason"])
    assert "洗空" in reason, reason
    assert "state=completed" not in reason, reason

    # 用户消息不能记成"回过"：库里说回了、对方没收到，是最难查的组合
    rows = await _user_rows()
    target = rows[-1]
    assert target.handled_as == "failed", target.handled_as
    assert "洗空" in (target.handled_reason or "")


# ---------------------------------------------------------------- 2. 被发言仲裁压住


async def test_floor_suppressed_reply_says_it_was_suppressed(logged_in: TestEnv) -> None:
    """另一个窗口正在说话：内容生成了、落库了，但一个字都没发出去。"""
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    # 先正常发一条，把窗口建出来
    first = await _post(logged_in, _event_body("先建一个窗口", message_id=800))
    assert first["delivered"] is True, first
    window_id = await _window_id(WINDOW_REF)
    sent_before = len(fake.sent)

    # 占住发言租约（= 另一个窗口正在说）
    lease = logged_in.app.state.speak_lease
    held = await lease.acquire(
        actor_id="actor_ling", window_id=window_id, turn_id="turn_other_window"
    )
    assert held.acquired
    try:
        body = await _post_with_text(logged_in, "我记着呢，等你说完。", "刚才那句你听到了吗")
    finally:
        await lease.release(
            actor_id="actor_ling", window_id=window_id, turn_id="turn_other_window"
        )

    assert body["delivered"] is False
    assert len(fake.sent) == sent_before, "被压住却仍然发出去了"
    reason = str(body["reason"])
    assert "没有对外交付" in reason, reason
    assert "另一个窗口" in reason, reason
    assert "state=completed" not in reason, reason

    rows = await _user_rows()
    target = rows[-1]
    assert target.handled_as == "failed", "生成了却没交付，不能记成 replied"
    assert "没有对外交付" in (target.handled_reason or "")

    # 内容本身**必须落库**（可审计、可重放），只是没对着外发
    async with session_scope() as session:
        actor_rows = list(
            (
                await session.execute(
                    select(Message).where(
                        Message.role == "actor", Message.window_id == window_id
                    )
                )
            ).scalars()
        )
    assert any("我记着呢" in (row.text or "") for row in actor_rows)


# ---------------------------------------------------------------- 3. 分段只发出去一段


async def test_partial_delivery_reports_how_many_parts_arrived(logged_in: TestEnv) -> None:
    """第 2 段被平台拒收：前面那段已经发出去了，后面被丢弃——必须说 `1/3`。"""
    fake = _FakeOneBot(fail_at=2)
    _install(logged_in, fake)
    await _bind(logged_in)

    long_text = (
        "第一句：今天风很大，我把窗关上了。\n"
        "第二句：你要是出门记得穿厚一点。\n"
        "第三句：回来我给你煮点热的。\n"
    )
    body = await _post_with_text(logged_in, long_text, "我出门了")

    assert body["parts_total"] == 3
    assert body["parts_sent"] == 1
    assert body["delivered"] is False
    # 第 1 段真的发出去了，第 3 段**没有**发（停在第 2 段的失败上）
    assert len(fake.sent) == 2
    assert "第一句" in fake.texts()[0]
    assert "第三句" not in "".join(fake.texts())

    reason = str(body["reason"])
    assert "1/3" in reason, reason
    assert "已回复" not in reason, "只发出去一段却写成已回复"

    rows = await _user_rows()
    target = rows[-1]
    assert target.handled_as == "failed", target.handled_as
    assert "1/3" in (target.handled_reason or "")
