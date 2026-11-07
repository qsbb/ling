"""群名片同步的**生产链路**：入站消息 → identity 旁路 → 当轮上下文。

`IdentityService.observe_display_name` 与 `InteractionEnvelope.sender_nickname`
各自都有测试，但一直**没有任何生产调用方**（属于"写好了的死代码"）。这里
测的就是"接进去之后是不是真的生效"：

1. 一条群消息进来后 ``subjects.display_name`` 被同步成本次的**昵称**；
2. 同一轮生成时上下文用的是**本次名片**（``compile(user_sender_name=...)``）；
3. **首条消息建档**时写的是昵称，不是群名片（跨群污染的源头）；
4. 昵称为空时**不覆盖**旧名。

走的是真实入站路径（``POST /v1/onebot/events`` → `inbound_intake` →
`InboundBridge.handle` → `TurnCoordinator`），只有 OneBot 实现本身是假的。
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

from ling.domains.identity.service import reset_display_name_cache
from ling.infra.db import session_scope
from ling.infra.models import ActorAccount, Subject
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-card",
)
GROUP = "88888"
# 群里一个**没绑过**的号：正是"自动建 known 级 subject"的那条路
MEMBER = "100200301"


class _FakeOneBot:
    """收下发出的请求，回成功。名字相关的断言不需要真实 QQ。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(
            {"action": str(request.url).rsplit("/", 1)[-1], "body": json.loads(request.content or b"{}")}
        )
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 9001}}
        )


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    """进程内的"上次见到的名字"缓存是全局的，用例之间必须互不影响。"""
    reset_display_name_cache()
    yield
    reset_display_name_cache()


def _install(env: TestEnv) -> _FakeOneBot:
    fake = _FakeOneBot()
    env.app.state.onebot_connector = OneBotV11Connector(  # type: ignore[attr-defined]
        CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    return fake


def _group_body(
    *,
    message_id: int,
    message: str = "在吗",
    nickname: str | None = "阿岚",
    card: str | None = None,
) -> bytes:
    sender: dict[str, object] = {
        "user_id": int(MEMBER),
        "sex": "male",
        "age": 18,
    }
    if nickname is not None:
        sender["nickname"] = nickname
    if card is not None:
        sender["card"] = card
    payload = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": int(GROUP),
        "user_id": int(MEMBER),
        "message": message,
        "raw_message": message,
        "font": 0,
        "sender": sender,
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


async def _member_subject_id() -> str:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(ActorAccount).where(
                        ActorAccount.connector_instance == CFG.instance_id,
                        ActorAccount.external_account_id == MEMBER,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, f"应该正好一个账号绑定，实际 {len(rows)}"
    return rows[0].subject_id


async def _display_name(subject_id: str) -> str:
    async with session_scope() as session:
        row = await session.get(Subject, subject_id)
        assert row is not None
        return row.display_name


# --------------------------------------------------- ③ 首条消息建档不用群名片


async def test_first_message_files_the_member_under_the_nickname(
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首条群消息建档：``display_name`` 取**昵称**，不是这个群的名片。

    这是跨群污染的源头：用名片建号 = "他在本群叫什么"被写成了"他是谁"，
    之后他在别的群改个名片，其他群和私聊的称呼就一起被改了。

    **把旁路同步关掉**再断言，否则分不清两种实现：建档时写对了，还是
    建档写了名片、紧接着被 `observe_display_name` 在同一条消息里纠正。
    关掉之后剩下的就只有"建档写什么"这一个决定。
    """
    from ling.domains.identity.service import IdentityService

    async def _no_sync(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return None

    monkeypatch.setattr(IdentityService, "observe_display_name", _no_sync)
    _install(logged_in)

    await _post(
        logged_in,
        _group_body(message_id=7001, nickname="阿岚", card="本群名片甲"),
    )

    subject_id = await _member_subject_id()
    assert await _display_name(subject_id) == "阿岚"


# ------------------------------------------------------ ① 昵称变了就同步进库


async def test_renamed_nickname_is_synced_into_the_subject(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """第二句带来新昵称 → ``subjects.display_name`` 跟着变（人级列）。

    顺带钉住"名片不进人级列"：这次名片也换了，但库里必须是昵称。
    """
    _install(logged_in)
    await _post(logged_in, _group_body(message_id=7101, nickname="阿岚", card="名片甲"))
    subject_id = await _member_subject_id()
    assert await _display_name(subject_id) == "阿岚"

    await _post(
        logged_in,
        _group_body(message_id=7102, nickname="懒懒", card="完全不同的名片乙"),
    )

    assert await _display_name(subject_id) == "懒懒"


# ------------------------------------------------------ ④ 空昵称不覆盖旧名字


async def test_empty_nickname_keeps_the_old_name(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """名片/昵称都为空的旧连接器消息：**保持旧值**，空名不是改名信号。"""
    _install(logged_in)
    await _post(logged_in, _group_body(message_id=7201, nickname="阿岚"))
    subject_id = await _member_subject_id()
    assert await _display_name(subject_id) == "阿岚"

    # sender 里既没有 nickname 也没有 card
    await _post(logged_in, _group_body(message_id=7202, nickname=None))

    assert await _display_name(subject_id) == "阿岚"


# --------------------------------- ② 同一轮生成时，上下文用的是**本次**名片


async def test_this_turn_context_uses_the_live_card(
    logged_in: TestEnv,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """群消息这一轮里，最后那句的署名是**这条消息**的名片，不是库里的旧名。

    链路：`InboundBridge._run_turn` → `TurnCoordinator.run_turn` →
    `_execute` → `ContextCompiler.compile(user_sender_name=...)`。
    抓的是 `compile` 实际收到的参数与它返回的最后一句，而不是"代码里写了"。
    """
    from ling.kernel import context as context_module

    seen: list[dict] = []
    compiled: list[object] = []
    original = context_module.ContextCompiler.compile

    async def _spy(self, **kwargs):  # noqa: ANN001, ANN003, ANN202
        seen.append(kwargs)
        result = await original(self, **kwargs)
        compiled.append(result)
        return result

    monkeypatch.setattr(context_module.ContextCompiler, "compile", _spy)
    _install(logged_in)

    # 先有一句历史（用旧昵称建档），再让他在本群改名并 @ 她
    await _post(logged_in, _group_body(message_id=7301, nickname="阿岚", card="旧名片"))
    subject_id = await _member_subject_id()

    result = await _post(
        logged_in,
        _group_body(
            message_id=7302,
            message="[CQ:at,qq=10001] 你好",
            nickname="阿岚",
            card="新名片",
        ),
    )
    assert result["decision"] == "reply", result

    assert seen, "这一轮没有编译上下文"
    assert seen[-1]["user_sender_name"] == "新名片", (
        f"compile 没拿到本次名片：{seen[-1].get('user_sender_name')!r}"
    )
    last = compiled[-1].messages[-1]
    assert last.content == "新名片：你好", last.content
    # 落库的仍是**人级**昵称——当轮显示与人级列是两件事
    assert await _display_name(subject_id) == "阿岚"


# ------------------------------------------------- 旁路失败不能把消息拒了


async def test_a_failed_name_write_does_not_reject_the_message(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """真写库失败（UPDATE subjects 抛错）：消息照收，旧名保留，下一条重试。

    这条守的是"成了生产调用方之后"的前提：`observe_display_name` 内部用
    SAVEPOINT 包住写库，一次失败的 flush 不会把 bridge 这个 session 的事务
    标成待回滚（否则随后 ``commit`` 抛 ``PendingRollbackError``——「改名失败
    把用户消息拒了」，正是需求里明确不允许的那一条）。
    """
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    from ling.infra import db as db_module

    _install(logged_in)
    await _post(logged_in, _group_body(message_id=7401, nickname="阿岚", card="名片甲"))
    subject_id = await _member_subject_id()
    assert await _display_name(subject_id) == "阿岚"

    fired: list[str] = []

    def _fail_update(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        if statement.lstrip().upper().startswith("UPDATE SUBJECTS"):
            fired.append(statement)
            raise IntegrityError(statement, parameters, Exception("boom"))

    engine = db_module._engine
    assert engine is not None, "夹具没初始化引擎"
    event.listen(engine.sync_engine, "before_cursor_execute", _fail_update)
    try:
        result = await _post(
            logged_in, _group_body(message_id=7402, nickname="懒懒", card="名片乙")
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _fail_update)

    assert fired, "用例没打到写名字那一步"
    assert result["accepted"] is True, result
    # 旧名没被写坏；失败没被记成"已写过"，所以下一条消息会重试
    assert await _display_name(subject_id) == "阿岚"

    await _post(logged_in, _group_body(message_id=7403, nickname="懒懒", card="名片乙"))
    assert await _display_name(subject_id) == "懒懒"
