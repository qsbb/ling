"""入站主流程里的**辅助写入**：写不进去也不能把这条消息带死。

`InboundBridge.handle()` 里除了"身份 / 窗口 / 消息 / 轮次"这些主流程，还有三处
**旁路写入**：

- ``_remember_platform_message_id``：本地消息 id → 平台 message id（引用要用）
- ``_record_followup``：她说的"稍后再提"（一条备忘意图）
- ``_ingest_media``：媒体证据登记（见 `test_media_consent_owner.py`）

它们的共同点是：**失败只该让那一项能力缺失**，而原来都没有保护。一次异常会
穿透到 `inbound_intake.process_inbound_payload` 的 ``except Exception``
（"事件处理失败，撤回认领以便重投"）：

- 用户看到的是**这条消息没收到回复**（outbound 与 /healthz 都正常，所以最像
  "收不到消息"）；
- 失败的 flush 还会把 session 标成待回滚，随后的 ``commit()`` 抛
  ``PendingRollbackError``——同一个故障换个地方再炸一次；
- 平台重投会让它在同一处反复失败。

这里钉住修好之后的两条性质：**并发/重投当成功**、**写失败不影响消息**。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from sqlalchemy import func, select

from ling.infra.db import session_scope
from ling.infra.models import IdempotencyKey
from ling.kernel.bridge import InboundBridge
from ling.kernel.context import PLATFORM_MESSAGE_ID_SCOPE
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-msgid",
)
GROUP = "88888"
MEMBER = "100200302"
LOCAL_MESSAGE_ID = "msg_local_7701"


# ---------------------------------------------------------------- 入站夹具


class _FakeOneBot:
    """收下发出的请求，回成功。这里断言的是"有没有被拒收"，不涉及真实 QQ。"""

    async def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: ARG002
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9001}}
        )


def _install(env: TestEnv) -> None:
    env.app.state.onebot_connector = OneBotV11Connector(  # type: ignore[attr-defined]
        CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(_FakeOneBot().handler))
    )


def _group_body(*, message_id: int) -> bytes:
    """一条**@ 了她**的群消息：只有被 @ 才会走到落消息 + 写旁路那一步。"""
    text = "[CQ:at,qq=10001] 在吗"
    payload = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": int(GROUP),
        "user_id": int(MEMBER),
        "message": text,
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": int(MEMBER), "nickname": "阿岚", "sex": "male", "age": 18},
    }
    return json.dumps(payload).encode()


def _headers(body: bytes) -> dict[str, str]:
    sig = hmac.new(b"sec", body, hashlib.sha1).hexdigest()
    return {
        "X-Signature": f"sha1={sig}",
        "X-Self-ID": "10001",
        "Content-Type": "application/json",
    }


async def _post(env: TestEnv, body: bytes) -> dict:
    response = await env.client.post(
        "/v1/onebot/events", content=body, headers=_headers(body)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _envelope(external_event_id: str) -> object:
    """只带 `external_event_id`：`_platform_message_id` 只需要它。"""
    return type("Env", (), {"external_event_id": external_event_id})()


def _bridge(session: object) -> InboundBridge:
    return InboundBridge(
        session,  # type: ignore[arg-type]
        coordinator=object(),
        connector=object(),  # 没有 platform_message_id_for_event → 走兜底解析
        secret_box=object(),
    )


# ------------------------------------------------------- 并发/重投：当成功


async def test_same_message_id_twice_is_success_and_last_value_wins(
    web_env: TestEnv,
) -> None:
    """同一个本地 message_id 写两次（两个事务，模拟并发/重投）：不抛、以最后一次为准。"""
    first = _envelope("10001:500100")
    second = _envelope("10001:500200")

    for envelope in (first, second):
        async with session_scope() as session:
            await _bridge(session)._remember_platform_message_id(  # noqa: SLF001
                LOCAL_MESSAGE_ID, envelope
            )

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(IdempotencyKey).where(
                        IdempotencyKey.scope == PLATFORM_MESSAGE_ID_SCOPE,
                        IdempotencyKey.key == LOCAL_MESSAGE_ID,
                    )
                )
            )
            .scalars()
            .all()
        )

    # 一次冲突既不能变成两行，也不能把第二次的值丢掉：重投拿到的平台 id
    # 才是最新的那个。
    assert len(rows) == 1
    assert rows[0].result_ref == "500200"


# ------------------------------------------------- 写失败不能把消息拒了


async def test_a_failed_mapping_write_does_not_reject_the_message(
    logged_in: TestEnv,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """真写库失败（INSERT idempotency_keys 抛错）：消息照收、照常决策，只记一条警告。"""
    from ling.infra import db as db_module

    _install(logged_in)
    fired = _fail_writes_to(
        db_module, table="idempotency_keys", scope=PLATFORM_MESSAGE_ID_SCOPE
    )

    try:
        # INFO 一档：既收 WARNING（runtime 警告），也收行为日志（INFO）
        with caplog.at_level(logging.INFO):
            result = await _post(logged_in, _group_body(message_id=7701))
    finally:
        fired.remove()

    assert fired.statements, "用例没打到平台 id 映射那一步"
    # 修好之前这里是 accepted=False / reason="处理失败，可重投: IntegrityError"
    assert result["accepted"] is True, result
    assert result["decision"] == "reply", result

    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "platform_message_id_record_failed"
        and record.levelno >= logging.WARNING
    ]
    assert warnings, [record.getMessage() for record in caplog.records]
    assert warnings[-1].attributes["reason_code"] == "platform_message_id_record_failed"

    # 行为日志也要有：这是"她这一侧"少了一项能力（说不清引用的是哪条）
    behavior = [
        record
        for record in caplog.records
        if record.name == "ling.behavior"
        and getattr(record, "event_name", "") == "platform_message_id_record_failed"
    ]
    assert behavior, [record.getMessage() for record in caplog.records]

    # 失败的写没有留下半条记录
    async with session_scope() as session:
        saved = (
            await session.execute(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.scope == PLATFORM_MESSAGE_ID_SCOPE)
            )
        ).scalar_one()
    assert saved == 0


async def test_a_failed_followup_write_does_not_reject_the_message(
    web_env: TestEnv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """「稍后再提」的意图写失败：不抛，调用方的事务仍然可以提交。

    `record_followup` 内部会 `flush()`，并发/重投撞同一个 `dedupe_key` 时
    抛 `IntegrityError`；它原来是裸调用的，所以"丢一句备忘"会变成"这条消息
    的处置也丢了"。
    """
    from ling.infra import db as db_module

    decision = type(
        "Decision",
        (),
        {"followup": type("Followup", (), {"after_minutes": 30, "goal": "问问她今天怎么样"})()},
    )()
    fired = _fail_writes_to(db_module, table="intents")

    try:
        async with session_scope() as session:
            with caplog.at_level(logging.WARNING, logger="ling.kernel.bridge"):
                await _bridge(session)._record_followup(  # noqa: SLF001
                    decision,
                    subject_id="subj_nobody",
                    window_id="win_nobody",
                    message_id=LOCAL_MESSAGE_ID,
                )
            # SAVEPOINT 回滚之后这个事务仍然干净：这一步抛错就说明没兜住
            await session.commit()
    finally:
        fired.remove()

    assert fired.statements, "用例没打到意图写入那一步"
    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "followup_record_failed"
    ]
    assert warnings and warnings[-1].attributes["reason_code"] == "followup_record_failed"


# ---------------------------------------------------------------- 故障注入


class _FailingWrites:
    """在 `before_cursor_execute` 上掐断某张表的写入，模拟真实的 DB 失败。"""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self._engine: object | None = None
        self._listener: object | None = None

    def remove(self) -> None:
        from sqlalchemy import event

        if self._engine is not None and self._listener is not None:
            event.remove(self._engine, "before_cursor_execute", self._listener)  # type: ignore[arg-type]
            self._engine = None
            self._listener = None


def _fail_writes_to(
    db_module: object, *, table: str, scope: str | None = None
) -> _FailingWrites:
    """让写入 `table` 的语句抛 `IntegrityError`；给了 `scope` 就只掐含该值的绑定参数。

    `scope` 用来区分同一张表上的两种写：入站认领（``claim_event``）也写
    ``idempotency_keys``，掐它就先死在主流程上了，用例会失去意义。
    """
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    fired = _FailingWrites()

    def _listener(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        if not statement.lstrip().upper().startswith(("INSERT", "UPDATE")):
            return
        if table not in statement:
            return
        if scope is not None and scope not in str(parameters):
            return
        fired.statements.append(statement)
        raise IntegrityError(statement, parameters, Exception("boom"))

    engine = db_module._engine  # type: ignore[attr-defined]
    assert engine is not None, "夹具没初始化引擎"
    event.listen(engine.sync_engine, "before_cursor_execute", _listener)
    fired._engine = engine.sync_engine  # noqa: SLF001 - 测试辅助
    fired._listener = _listener  # noqa: SLF001 - 测试辅助
    return fired
