"""主动撤回**她自己**发出去的消息：五道闸、回执五态、审计、上下文一致性。

用户拍板「说错了想收回」可以用。这个能力最危险的地方不是"能不能撤"，
而是**撤错了**：撤回不可逆，撤掉别人的话等于替别人改写群聊记录。所以这一组
用例的主体是**拒绝路径**——每一道闸都要能证明"平台一次 action 都没收到"。

五道闸（全部在发 action 之前）：

1. 归属：只有 ``role="actor"``（她自己的发言）；
2. 已送达：必须有 ``operations`` → ``delivery_receipts`` 的真实回执 +
   ``platform_message_id``；
3. 时间窗：平台 2 分钟（本地预检保守上限）；
4. 频次：同一会话每分钟 ≤3；
5. 不可撤回的对象：外部观测 / 模拟 / 空正文。

另有一条独立要求：``uncertain``（没拿到回执）**不能说成功**，也不能写
``recalled_at``——那会让下一次点击看到"已撤回"而跳过。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import func, select

from ling.contracts.v1 import (
    DeliveryState,
    OperationState,
    OutboundRecallState,
)
from ling.domains.conversation.recall import (
    RECALL_LIMITER,
    RECALL_RATE_LIMIT_PER_MINUTE,
    RECALL_WINDOW_SECONDS,
    RecallRejected,
    check_recallable_message,
    recallable_message_ids,
    refusal_note,
    resolve_recall_target,
)
from ling.infra.db import session_scope
from ling.infra.models import (
    ActivityRow,
    BehaviorModelCallRow,
    DeliveryReceiptRow,
    Message,
    Operation,
    SelfExperienceRow,
    Window,
)
from ling.infra.security import new_id
from ling.kernel.context import SELF_RECALLED_NOTE, ContextCompiler
from ling.ports.channel import RecallRequest
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
CHANNEL = "onebot"
INSTANCE = "onebot-recall-out"
WINDOW_REF = f"{INSTANCE}:private:20002"
PLATFORM_ID = "771001"


# ---------------------------------------------------------------- 假 OneBot


class _FakeOneBot:
    """记录 ``delete_msg`` 调用；可注入各种平台回答。

    ``calls`` 是验收里的硬指标：拒绝路径必须让它保持 0。
    """

    def __init__(self, *, mode: str = "ok") -> None:
        self.calls: list[dict] = []
        self.mode = mode

    async def handler(self, request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.calls.append({"action": action, "body": body})
        if action == "delete_msg":
            if self.mode == "ok":
                return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": None})
            if self.mode == "missing":
                return httpx.Response(
                    200,
                    json={"status": "failed", "retcode": 100, "message": "消息不存在"},
                )
            if self.mode == "expired":
                return httpx.Response(
                    200,
                    json={"status": "failed", "retcode": 120, "message": "撤回超时"},
                )
            if self.mode == "no_api":
                return httpx.Response(404)
            if self.mode == "timeout":
                raise httpx.ConnectTimeout("boom", request=request)
            if self.mode == "weird":
                return httpx.Response(
                    200, json={"status": "failed", "retcode": 999, "message": "未知错误"}
                )
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {}})

    def delete_calls(self) -> int:
        return len([call for call in self.calls if call["action"] == "delete_msg"])


def _install(env: TestEnv, fake: _FakeOneBot) -> OneBotV11Connector:
    connector = OneBotV11Connector(
        OneBotConfig(
            api_base="http://onebot.test:5700",
            access_token="tok",
            secret="sec",
            self_id="10001",
            instance_id=INSTANCE,
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    env.app.state.onebot_connector = connector
    return connector


# ---------------------------------------------------------------- 种子数据


async def _seed_window(env: TestEnv) -> str:
    async with session_scope() as session:
        window = Window(
            id=new_id("win"),
            actor_id=ACTOR,
            persona_id="persona_ling",
            subject_id=env.subject_id,
            title=WINDOW_REF,
            channel=CHANNEL,
            visibility="subject_private",
        )
        session.add(window)
        await session.commit()
        return window.id


async def _seed_message(
    window_id: str,
    *,
    role: str = "actor",
    text: str = "我刚才说错话了",
    origin: str = "model_inference",
    turn_id: str | None = None,
    sequence: int = 1,
) -> str:
    async with session_scope() as session:
        row = Message(
            id=new_id("msg"),
            window_id=window_id,
            turn_id=turn_id,
            role=role,
            text=text,
            origin=origin,
            sequence=sequence,
            state="completed",
        )
        session.add(row)
        await session.commit()
        return row.id


async def _seed_delivery(
    *,
    turn_id: str,
    platform_id: str | None = PLATFORM_ID,
    state: str = DeliveryState.delivered.value,
    observed_at: datetime | None = None,
    part_ordinal: int = 1,
) -> str:
    """造一段"真的发出去了"的证据：operation + delivery_receipt。

    走真表结构（不是 mock），因为归属核对查的就是这两张表——用 mock 等于
    把要验的东西替换掉了。
    """
    moment = observed_at or datetime.now(UTC)
    async with session_scope() as session:
        operation = Operation(
            id=new_id("op"),
            kind="delivery",
            actor_id=ACTOR,
            subject_id=None,
            state=OperationState.succeeded.value,
            payload={
                "channel": CHANNEL,
                "instance": INSTANCE,
                "window_ref": WINDOW_REF,
                "reply_plan": f"plan_{turn_id}",
                "part_ordinal": part_ordinal,
                "parts_total": 1,
            },
        )
        session.add(operation)
        await session.flush()
        receipt = DeliveryReceiptRow(
            id=new_id("rcpt"),
            operation_id=operation.id,
            channel=CHANNEL,
            state=state,
            platform_message_id=platform_id,
            observed_at=moment,
        )
        session.add(receipt)
        await session.commit()
        return operation.id


async def _seed_sent_message(
    env: TestEnv,
    *,
    text: str = "我刚才说错话了",
    age_seconds: float = 5.0,
    platform_id: str | None = PLATFORM_ID,
    receipt_state: str = DeliveryState.delivered.value,
    role: str = "actor",
    origin: str = "model_inference",
    turn_id: str | None = None,
) -> tuple[str, str]:
    """一条"发出去过、有回执"的消息（默认在她自己的 2 分钟窗口内）。"""
    window_id = await _seed_window(env)
    tid = turn_id or new_id("turn")
    message_id = await _seed_message(
        window_id, role=role, text=text, origin=origin, turn_id=tid
    )
    await _seed_delivery(
        turn_id=tid,
        platform_id=platform_id,
        state=receipt_state,
        observed_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )
    return window_id, message_id


async def _post(env: TestEnv, window_id: str, message_id: str):
    return await env.client.post(
        f"/v1/sessions/{window_id}/messages/{message_id}/recall"
    )


async def _message(message_id: str) -> Message | None:
    async with session_scope() as session:
        return await session.get(Message, message_id)


async def _counts() -> dict[str, int]:
    async with session_scope() as session:
        async def count(model) -> int:  # noqa: ANN001
            return int(
                (await session.execute(select(func.count()).select_from(model))).scalar_one()
            )

        return {
            "activities": await count(ActivityRow),
            "experiences": await count(SelfExperienceRow),
            "model_calls": await count(BehaviorModelCallRow),
        }


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    """限流器是进程级单例：用例之间必须互不影响。"""
    RECALL_LIMITER.reset()
    RECALL_LIMITER.limit = RECALL_RATE_LIMIT_PER_MINUTE


# ---------------------------------------------------------------- ① 成功


async def test_recall_own_recent_message_succeeds(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """撤回自己 2 分钟内的消息：平台收到**一次** delete_msg、本地标记、返回 recalled。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)

    response = await _post(web_env, window_id, message_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == OutboundRecallState.recalled.value
    assert body["note"] == "撤回了"
    assert body["recalled_at"]
    assert body["receipt_id"]

    assert fake.delete_calls() == 1, "平台必须且只收到一次 delete_msg"
    assert fake.calls[0]["body"] == {"message_id": PLATFORM_ID}

    row = await _message(message_id)
    assert row is not None
    assert row.recalled_at is not None, "确认撤回才写 recalled_at"
    assert row.recall_state == "recalled"
    assert row.recall_receipt_id
    # **行不删**：撤回是"以后别再引用"，不是"过去没发生"。
    assert row.text == "我刚才说错话了"


async def test_recall_does_not_touch_facts_or_call_models(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """撤回不写事实表、不调模型（"计划不是事实"的同一原则）。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)
    before = await _counts()

    await _post(web_env, window_id, message_id)
    after = await _counts()

    assert after == before, f"撤回改变了事实表或调了模型：{before} -> {after}"


async def test_recall_leaves_an_audit_line_without_text(
    web_env: TestEnv, logged_in: TestEnv, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    """每次撤回都要留审计：谁触发、哪条、结果；**不含正文**。"""
    import logging

    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(
        web_env, text="这句里有不该留档的内容"
    )

    with caplog.at_level(logging.INFO, logger="ling.audit"):
        await _post(web_env, window_id, message_id)

    records = [
        r for r in caplog.records if getattr(r, "event_name", "") == "audit_message_recalled"
    ]
    assert records, "撤回必须留一条审计"
    payload = records[-1].attributes
    assert payload["target_id"] == message_id
    assert payload["attributes"]["recall_state"] == "recalled"
    text = json.dumps(payload, ensure_ascii=False)
    assert "不该留档" not in text, "审计里不许出现消息正文"


# ---------------------------------------------------------------- ② 拒绝：零 action


async def test_recall_someone_elses_message_is_refused(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """别人的消息一律拒（那是"撤回别人的话"，不在授权范围内）。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env, role="user")

    response = await _post(web_env, window_id, message_id)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["detail"] == "not_actor_message"
    assert "只能撤回她自己说过的话" in response.json()["error"]["message"]
    assert fake.delete_calls() == 0, "拒绝时平台一次 action 都不许收到"


async def test_recall_outside_time_window_is_refused_without_action(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """超过平台 2 分钟窗：直接拒，**不发 action**（发出去只会白挨风控）。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(
        web_env, age_seconds=RECALL_WINDOW_SECONDS + 30
    )

    response = await _post(web_env, window_id, message_id)

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["error"]["detail"] == "expired"
    assert "超过平台允许的撤回时间" in body["error"]["message"]
    assert fake.delete_calls() == 0
    row = await _message(message_id)
    assert row is not None and row.recalled_at is None


async def test_recall_rate_limit_is_refused_without_action(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """同一会话每分钟超过上限：拒，且**不发 action**（防手滑把对话抹空）。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id = await _seed_window(web_env)
    ids: list[str] = []
    for index in range(RECALL_RATE_LIMIT_PER_MINUTE + 1):
        turn_id = new_id("turn")
        message_id = await _seed_message(
            window_id, text=f"第 {index} 句", turn_id=turn_id, sequence=index + 1
        )
        await _seed_delivery(turn_id=turn_id, platform_id=f"77{index}")
        ids.append(message_id)

    for message_id in ids[:RECALL_RATE_LIMIT_PER_MINUTE]:
        first = await _post(web_env, window_id, message_id)
        assert first.status_code == 200, first.text
    assert fake.delete_calls() == RECALL_RATE_LIMIT_PER_MINUTE

    blocked = await _post(web_env, window_id, ids[-1])
    assert blocked.status_code == 429, blocked.text
    assert blocked.json()["error"]["detail"] == "rate_limited"
    assert fake.delete_calls() == RECALL_RATE_LIMIT_PER_MINUTE, "超频次不许发 action"


async def test_recall_message_without_receipt_is_refused(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """没有平台回执（不知道发没发出去）：拒，零 action。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id = await _seed_window(web_env)
    turn_id = new_id("turn")
    message_id = await _seed_message(window_id, turn_id=turn_id)
    # 只有 operation，没有回执
    async with session_scope() as session:
        session.add(
            Operation(
                id=new_id("op"),
                kind="delivery",
                actor_id=ACTOR,
                state=OperationState.succeeded.value,
                payload={"reply_plan": f"plan_{turn_id}", "part_ordinal": 1},
            )
        )
        await session.commit()

    response = await _post(web_env, window_id, message_id)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["detail"] == "not_delivered"
    assert fake.delete_calls() == 0


async def test_recall_undelivered_message_is_refused(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """回执是 uncertain（不知道发没发出去）：拒——没有平台 id 就没有撤回对象。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(
        web_env, platform_id=None, receipt_state=DeliveryState.uncertain.value
    )

    response = await _post(web_env, window_id, message_id)

    assert response.status_code == 409, response.text
    assert fake.delete_calls() == 0


async def test_recall_non_owner_is_refused(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """非 owner 一律 403（撤回是不可逆动作，只给主人）。"""
    from httpx import ASGITransport, AsyncClient

    from ling.infra.models import User
    from ling.infra.security import hash_password

    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)

    async with session_scope() as session:
        session.add(
            User(
                id=new_id("user"),
                username="recall_member",
                password_hash=hash_password("member-password-r1"),
                role="member",
                subject_id=web_env.subject_id,
            )
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=web_env.app), base_url="http://testserver"
    ) as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": "recall_member", "password": "member-password-r1"},
        )
        assert login.status_code == 200, login.text
        response = await client.post(
            f"/v1/sessions/{window_id}/messages/{message_id}/recall"
        )

    assert response.status_code == 403, response.text
    assert response.json()["error"]["detail"] == "not_owner"
    assert fake.delete_calls() == 0


async def test_recall_already_recalled_is_refused(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """撤过一次再撤：拒（幂等，不重复发 action）。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)

    first = await _post(web_env, window_id, message_id)
    assert first.status_code == 200
    second = await _post(web_env, window_id, message_id)

    assert second.status_code == 409, second.text
    assert second.json()["error"]["detail"] == "already_recalled"
    assert fake.delete_calls() == 1, "第二次不许再发 action"


async def test_recall_unknown_message_is_404(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id = await _seed_window(web_env)

    response = await _post(web_env, window_id, "msg_does_not_exist")

    assert response.status_code == 404, response.text
    assert fake.delete_calls() == 0


async def test_recall_message_from_another_window_is_404(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """跨窗口：按"不存在"处理（不泄露别的会话里有什么）。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    _window_a, message_id = await _seed_sent_message(web_env)
    window_b = await _seed_window(web_env)

    response = await _post(web_env, window_b, message_id)

    assert response.status_code == 404, response.text
    assert fake.delete_calls() == 0


# ---------------------------------------------------------------- ③ uncertain


async def test_platform_timeout_is_uncertain_not_success(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """平台超时：**不许说成功**、不写 recalled_at，但记下"试过"。"""
    fake = _FakeOneBot(mode="timeout")
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)

    response = await _post(web_env, window_id, message_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == OutboundRecallState.uncertain.value
    assert "不确定是否撤回成功" in body["note"]
    assert body["recalled_at"] is None, "没确认就不许写撤回时间"

    row = await _message(message_id)
    assert row is not None
    assert row.recalled_at is None, "uncertain 不许写 recalled_at"
    assert row.recall_attempted_at is not None, "但要记下我们试过"
    assert row.recall_state == "uncertain"
    assert fake.delete_calls() == 1, "试过一次（不重复撤）"

    # 可重试：再点一次会再发一次 action（因为没确认成功）
    retry = await _post(web_env, window_id, message_id)
    assert retry.status_code == 200, retry.text
    assert fake.delete_calls() == 2


async def test_platform_unknown_error_is_uncertain(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """平台业务失败但不是"不存在/超时"：按未知处理（可能已经撤了）。"""
    fake = _FakeOneBot(mode="weird")
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)

    response = await _post(web_env, window_id, message_id)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == OutboundRecallState.uncertain.value
    row = await _message(message_id)
    assert row is not None and row.recalled_at is None


# ---------------------------------------------------------------- ④ 平台侧拒绝


async def test_platform_not_found_maps_to_not_found(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    fake = _FakeOneBot(mode="missing")
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)

    response = await _post(web_env, window_id, message_id)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == OutboundRecallState.not_found.value
    assert "已经不在平台上" in response.json()["note"]


async def test_platform_expired_maps_to_expired(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """平台自己说超时（可能它的窗口比我们预检的短）。"""
    fake = _FakeOneBot(mode="expired")
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)

    response = await _post(web_env, window_id, message_id)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == OutboundRecallState.expired.value


async def test_implementation_without_delete_msg_is_unsupported(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """OneBot 实现没这个 API（404）：unsupported，不假装成功。"""
    fake = _FakeOneBot(mode="no_api")
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)

    response = await _post(web_env, window_id, message_id)

    assert response.status_code == 200, response.text
    assert response.json()["state"] == OutboundRecallState.unsupported.value
    row = await _message(message_id)
    assert row is not None and row.recalled_at is None


# ---------------------------------------------------------------- ⑤ 适配器单测


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("ok", OutboundRecallState.recalled),
        ("missing", OutboundRecallState.not_found),
        ("expired", OutboundRecallState.expired),
        ("no_api", OutboundRecallState.unsupported),
        ("timeout", OutboundRecallState.uncertain),
        ("weird", OutboundRecallState.uncertain),
    ],
)
async def test_onebot_recall_maps_platform_answers(mode: str, expected: object) -> None:
    """OneBot 的 ``delete_msg`` 回答 → 五态（一条都不许猜成成功）。"""
    fake = _FakeOneBot(mode=mode)
    connector = OneBotV11Connector(
        OneBotConfig(
            api_base="http://onebot.test:5700",
            self_id="10001",
            instance_id=INSTANCE,
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )

    receipt = await connector.recall(RecallRequest(platform_message_id="12345"))

    assert receipt.state is expected
    assert receipt.platform_message_id == "12345"


async def test_onebot_without_message_id_sends_no_action() -> None:
    """没有平台 id：不发 action、状态不确定（不猜）。"""
    fake = _FakeOneBot()
    connector = OneBotV11Connector(
        OneBotConfig(api_base="http://onebot.test:5700", instance_id=INSTANCE),
        http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )

    receipt = await connector.recall(RecallRequest(platform_message_id=""))

    assert receipt.state is OutboundRecallState.uncertain
    assert fake.calls == []


def test_onebot_declares_recall_capability() -> None:
    connector = OneBotV11Connector(OneBotConfig(instance_id=INSTANCE))
    assert connector.capabilities().supports_recall is True


def test_qqbot_reports_unsupported_and_declares_no_capability() -> None:
    """QQ 官方机器人：明确 unsupported，且能力位为 False（不发请求）。"""
    from connectors.qqbot.connector import QQBotConnector
    from connectors.qqbot.settings import QQBotConfig

    connector = QQBotConnector(QQBotConfig(app_id="1", client_secret="s"))
    assert connector.capabilities().supports_recall is False

    import asyncio

    receipt = asyncio.run(connector.recall(RecallRequest(platform_message_id="9")))
    assert receipt.state is OutboundRecallState.unsupported
    assert "不支持撤回" in (receipt.detail or "")


# ---------------------------------------------------------------- ⑥ 纯函数闸


def _row(**overrides: object) -> Message:
    base = {
        "id": "msg_x",
        "window_id": "win_x",
        "role": "actor",
        "text": "有内容",
        "origin": "model_inference",
        "state": "completed",
        "sequence": 1,
    }
    base.update(overrides)
    return Message(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"role": "user"}, "not_actor_message"),
        ({"origin": "external_observation"}, "not_recallable_origin"),
        ({"origin": "verified_source"}, "not_recallable_origin"),
        ({"origin": "simulated"}, "not_recallable_origin"),
        ({"text": "   "}, "not_recallable_origin"),
    ],
)
def test_recallable_gate_is_a_pure_function(overrides: dict, code: str) -> None:
    with pytest.raises(RecallRejected) as exc:
        check_recallable_message(_row(**overrides))
    assert exc.value.code == code
    # 空正文有自己的一句更具体的话（"没有正文"比"不是对话里说的"更准确），
    # 但**错误码**仍是同一档：都属于"这条不该撤"。
    if overrides.get("text", "").strip() == "" and "text" in overrides:
        assert "没有正文" in exc.value.note
    else:
        assert exc.value.note == refusal_note(code)


def test_recallable_gate_accepts_her_own_normal_message() -> None:
    check_recallable_message(_row())  # 不抛即通过


def test_refusal_notes_are_human_and_never_leak_codes() -> None:
    """每一档拒绝都有人话，而且**人话里不出现原因码**（界面不显示码）。"""
    from ling.domains.conversation.recall import RECALL_REFUSAL_NOTES

    for code in RECALL_REFUSAL_NOTES:
        text = refusal_note(code)
        assert text, code
        assert code not in text, f"{code} 的人话里出现了原因码"
        assert "_" not in text, f"{code} 的人话里还带着下划线（像原因码）"


# ---------------------------------------------------------------- ⑦ 上下文一致


async def test_recalled_message_gets_a_first_person_note_in_context(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """她自己撤回的那条：上下文里保留原文 + 第一人称注记（不是"被撤回"）。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(
        web_env, text="我刚才说了一句不该说的话"
    )
    await _post(web_env, window_id, message_id)

    async with session_scope() as session:
        compiled = await ContextCompiler(
            session, actor_id=ACTOR, persona_id="persona_ling"
        ).compile(
            subject_id=web_env.subject_id,
            window_id=window_id,
            user_text="继续说",
            is_group=False,
            history_limit=10,
        )
    text = "\n".join(message.content for message in compiled.messages)

    assert "我刚才说了一句不该说的话" in text, "她说过的话不该被抹掉"
    assert SELF_RECALLED_NOTE in text
    assert "（这条消息后来被撤回了）" not in text, "不能和入站撤回的措辞混用"


async def test_recallable_message_ids_only_lists_her_own(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """前端画按钮用的列表：只列她的、有回执的、没撤过的。"""
    _install(web_env, _FakeOneBot())
    window_id, own_id = await _seed_sent_message(web_env)
    _window, user_id = await _seed_sent_message(web_env, role="user", text="我问的")

    async with session_scope() as session:
        ids = await recallable_message_ids(session, window_id=window_id)

    assert own_id in ids
    assert user_id not in ids


# ---------------------------------------------------------------- ⑧ 目标解析


async def test_resolve_target_prefers_the_last_segment(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """分段回复：撤最后一段（用户看到的是整条回复的尾部），不按正文猜。"""
    window_id = await _seed_window(web_env)
    turn_id = new_id("turn")
    message_id = await _seed_message(window_id, turn_id=turn_id)
    await _seed_delivery(turn_id=turn_id, platform_id="seg1", part_ordinal=1)
    await _seed_delivery(
        turn_id=turn_id,
        platform_id="seg2",
        part_ordinal=2,
        observed_at=datetime.now(UTC) + timedelta(milliseconds=10),
    )

    async with session_scope() as session:
        target = await resolve_recall_target(
            session, window_id=window_id, message_id=message_id
        )

    assert target.platform_message_id == "seg2"


async def test_resolve_target_rejects_expired_without_action(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    window_id, message_id = await _seed_sent_message(
        web_env, age_seconds=RECALL_WINDOW_SECONDS + 1
    )
    async with session_scope() as session:
        with pytest.raises(RecallRejected) as exc:
            await resolve_recall_target(
                session, window_id=window_id, message_id=message_id
            )
    assert exc.value.code == "expired"


# ---------------------------------------------------------------- ⑨ 与入站撤回并存


async def test_inbound_and_outbound_recall_are_independent_columns(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """两条撤回路径写在**不同列**上，互不覆盖。

    入站撤回（对方收回）写 `retracted_at`；出站撤回（她收回自己的话）写
    `recalled_at`。合并成一列会让"谁撤回的"永久丢失，而撤回不可逆。
    """
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env)

    # 模拟"对方撤回了另一条"：只动 retracted_at。
    user_window, user_message = await _seed_sent_message(
        web_env, role="user", text="对方说的话"
    )
    async with session_scope() as session:
        row = await session.get(Message, user_message)
        assert row is not None
        row.retracted_at = datetime.now(UTC)
        row.retracted_after_seen = True
        await session.commit()

    response = await _post(web_env, window_id, message_id)
    assert response.status_code == 200, response.text

    async with session_scope() as session:
        mine = await session.get(Message, message_id)
        theirs = await session.get(Message, user_message)
    assert mine is not None and theirs is not None
    assert mine.recalled_at is not None, "她自己撤的要写 recalled_at"
    assert mine.retracted_at is None, "她自己撤的不该写 retracted_at"
    assert theirs.retracted_at is not None, "对方撤回的仍是 retracted_at"
    assert theirs.recalled_at is None


async def test_recall_does_not_rollback_her_reply_history(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """撤回不删行、不改正文：她已经说过的话仍是既成事实。"""
    fake = _FakeOneBot()
    _install(web_env, fake)
    window_id, message_id = await _seed_sent_message(web_env, text="原话")
    before = await _message(message_id)
    assert before is not None

    await _post(web_env, window_id, message_id)

    after = await _message(message_id)
    assert after is not None
    assert after.id == before.id
    assert after.text == before.text
    assert after.sequence == before.sequence
    assert after.turn_id == before.turn_id
