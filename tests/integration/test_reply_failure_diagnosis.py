"""复现「决定回了，但轮次 state=failed、没有可交付文本」。

线上症状（用户 2026-11-07 12:21 的日志）：

    WARNING ling.kernel.bridge  轮次 turn_… 失败: turn.failed
    WARNING ling.kernel.bridge  轮次结束但没有可交付文本
    INFO    ling.behavior       对Co的话：没接话（轮次结束但没有可交付文本（state=failed））

这三行是**同一条因果链**：`TurnCoordinator._execute` 把任何 ``ModelError``
交给 `_finalize` 收成 ``state=failed`` 且正文为空 → `bridge._run_turn` 拿不到
文本 → 行为日志只能说"没接话"。用户看不出**为什么**。

本模块做四件事：

1. 证明链路本身是通的（群消息 → 决策 → 生成 → 投递拿得到文本）；
2. **复现**失败：让生成抛 ``ModelError``，断言日志与线上逐字一致——
   同时证明"额度耗尽 / 鉴权失败 / 职责没有可用模型"这三种**完全不同的原因**
   在日志里长得一模一样（这本身就是缺陷）；
3. 复现「供应商日额度用完」这条具体路径（`TokenQuotaStore` + 真实
   `ModelRouter`）：它不是故障、换家或等明天就好，却和密钥失效一样让轮次失败；
4. 复现「职责解析不到可用模型」：真实 ``ModelRouter._resolved_binding``
   在**没有任何供应商调用、也没有任何日志**的情况下把轮次打成 failed。
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

from ling.models.router import ModelRouter
from ling.ports.model import ModelError, ModelErrorKind
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-diag",
)
QQ_USER = "100200300"
GROUP = "200300400"


class _FakeOneBot:
    """记录出站请求，一律回成功 —— 我们只关心"有没有话要发"。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"")
        self.sent.append({"action": str(request.url).rsplit("/", 1)[-1], "body": body})
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
    from ling.domains.identity.service import IdentityService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        await IdentityService(session).bind_account(
            subject_id=env.subject_id,
            connector_instance=CFG.instance_id,
            external_account_id=QQ_USER,
            proof="proven",
        )


def _event_body(**overrides) -> bytes:  # noqa: ANN003
    payload = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 701,
        "user_id": int(QQ_USER),
        "message": "111",
        "raw_message": "111",
        "font": 0,
        "sender": {"nickname": "Co", "sex": "male", "age": 18},
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _group_event(message: str, *, message_id: int = 702) -> bytes:
    return _event_body(
        message_type="group",
        group_id=int(GROUP),
        message_id=message_id,
        message=message,
        raw_message=message,
    )


async def _post(env: TestEnv, body: bytes) -> httpx.Response:
    sig = hmac.new(b"sec", body, hashlib.sha1).hexdigest()
    return await env.client.post(
        "/v1/onebot/events",
        content=body,
        headers={
            "X-Signature": f"sha1={sig}",
            "X-Self-ID": "10001",
            "Content-Type": "application/json",
        },
    )


def _is_choice_call(messages) -> bool:  # noqa: ANN001
    return bool(messages) and "只输出一个 JSON 对象" in (messages[0].content or "")


class _DecisionRouter:
    """只接管「回不回」的决策调用，生成原样委托给真实 router。

    不整体替换 router 是关键：生成必须真的走到 provider 那一层，
    否则测的就不是"模型调用失败"这条路径了。
    """

    def __init__(self, inner, *, respond: bool = True) -> None:  # noqa: ANN001
        self._inner = inner
        self._respond = respond

    async def stream(self, config, *, profile, messages, **kwargs):  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        if not _is_choice_call(messages):
            async for item in self._inner.stream(
                config, profile=profile, messages=messages, **kwargs
            ):
                yield item
            return
        payload = json.dumps(
            {"respond": self._respond, "reason": "想接话", "length": "normal"},
            ensure_ascii=False,
        )
        yield ModelChunk(delta=payload), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=payload)


class _FailingGenerationRouter:
    """决策正常，**生成**抛指定 ModelError —— 与 `ModelRouter.stream` 的出口一致。

    真实链路里这两类失败都从 `ModelRouter.stream` 抛出：
    - provider 调用失败（鉴权/超时/4xx/5xx/熔断）；
    - 或者根本没调 provider：`_resolved_binding` 判"这个职责没有可用模型"，
      以及 `_quota_block` 判"这一家今天的额度用完了"。
    调用方（`turn._execute`）看到的是同一个 ``ModelError``，没有任何区别。
    """

    def __init__(
        self, inner, *, kind: ModelErrorKind, message: str, retriable: bool = False  # noqa: ANN001
    ) -> None:
        self._inner = inner
        self._kind = kind
        self._message = message
        self._retriable = retriable
        self.generation_calls = 0

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
        raise ModelError(self._kind, self._message, retriable=self._retriable)
        yield ModelChunk(done=True), StreamOutcome()  # pragma: no cover


async def _allow_immediate_group_decision(env: TestEnv) -> None:
    """群里关掉"先放着等她刷到"，让普通群消息立即进决策（= 线上那一刻的形态）。"""
    from ling.infra.db import session_scope
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    async with session_scope() as session:
        svc = ConfigService(
            session,
            SecretBox(env.settings),
            test_mode_default=env.settings.test_mode,
            persona_id=env.settings.persona_id,
        )
        draft = await svc.save_draft(dialogue={"group_attention_deferred": False})
        await svc.publish(draft.revision)


def _behavior_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.name == "ling.behavior"
    ]


def _bridge_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "ling.kernel.bridge"]


# ---------------------------------------------------------------- 1. 链路本身是通的


async def test_group_message_full_chain_delivers_text(logged_in: TestEnv) -> None:
    """群消息走完 bridge 全链路（决策→生成→分段→投递）必须拿到可交付文本。

    这条是**对照组**：同一套装置下只要 provider 正常，文本就送得出去。
    之后两条失败用例因此可以排除"链路本身不通"。
    """
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)
    await _allow_immediate_group_decision(logged_in)

    stub = _DecisionRouter(logged_in.app.state.router, respond=True)
    logged_in.app.state.router = stub
    try:
        body = (await _post(logged_in, _group_event("今天好热"))).json()
    finally:
        logged_in.app.state.router = stub._inner

    assert body["decision"] == "reply", body
    assert body["delivered"] is True, body
    assert body.get("handled_as") == "replied", body
    # 真的发出去了，且是群里；正文里带 fake 的测试标记（证明文本来自 provider）
    assert fake.sent, "链路没打通：一条消息都没发"
    assert fake.sent[0]["action"] == "send_group_msg", fake.sent[0]
    sent_text = json.dumps(fake.sent[0]["body"], ensure_ascii=False)
    assert "测试模式" in sent_text, sent_text


# ---------------------------------------------------------------- 2. 复现：同一条日志，三种原因


@pytest.mark.parametrize(
    ("kind", "message", "retriable"),
    [
        # 供应商侧故障：密钥失效（401）
        (ModelErrorKind.auth, "供应商 main 鉴权失败（密钥无效或权限不足）", False),
        # 供应商侧故障：模型名不存在（404 一类）
        (ModelErrorKind.error, "供应商返回 HTTP 404: model not found", False),
        # 额度：这一家今天用完了（不是故障）
        (
            ModelErrorKind.quota_exhausted,
            "供应商 main 今天的 token 额度已用完（已用 200431 / 上限 200000）。"
            "换一家供应商、提高额度，或等明天重置。",
            False,
        ),
        # 配置侧：这个职责根本没解析到模型（一次 provider 调用都没发生）
        (
            ModelErrorKind.unsupported,
            "职责 dialogue.fast 没有可用模型：「快速」槽位还没有配模型。请在设置里配置后再试",
            False,
        ),
    ],
)
async def test_model_error_reproduces_production_logs(
    logged_in: TestEnv,
    caplog: pytest.LogCaptureFixture,
    kind: ModelErrorKind,
    message: str,
    retriable: bool,
) -> None:
    """任何 ``ModelError`` 都收敛成同一句"轮次结束但没有可交付文本"。

    修复前：**四种完全不同的原因，日志里一个字都不差**（只有 ``turn.failed |``
    和一个 ``state=failed``）。这就是用户截图那三行，也是他无从下手的原因。
    修复后：``code`` 与原因必须出现在同一行里——但状态机本身没变
    （仍然是 failed、仍然没有可交付文本），变的只是**它说出来**。
    """
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _FailingGenerationRouter(
        logged_in.app.state.router, kind=kind, message=message, retriable=retriable
    )
    logged_in.app.state.router = stub
    try:
        with caplog.at_level(logging.INFO):
            response = await _post(logged_in, _event_body())
    finally:
        logged_in.app.state.router = stub._inner

    body = response.json()
    assert stub.generation_calls == 1, "生成没有被调用，测到的不是模型失败路径"
    assert body["decision"] == "reply", body
    assert body["delivered"] is False, body
    assert "reply_text" not in body, body  # 接口只在真的生成出正文时才带它
    assert not fake.sent, "轮次失败却仍然发了消息"

    # 用户看到的那句话：状态 + **原因代码**（修复前只有 state=failed）
    assert body["reason"] == (
        f"轮次结束但没有可交付文本（state=failed，error={kind.value}）"
    ), body

    # ---- 与线上截图对齐的三行（第 2、3 行逐字相同）
    bridge = _bridge_lines(caplog)
    failures = [line for line in bridge if "失败（failed）：" in line]
    assert failures, bridge
    no_text = [line for line in bridge if line == "轮次结束但没有可交付文本"]
    assert no_text, bridge
    behaviors = _behavior_lines(caplog)
    assert (
        f"对 Co 的话：没接话（轮次结束但没有可交付文本（state=failed，"
        f"error={kind.value}））" in behaviors
    ), behaviors
    # 原因写在失败那一行里，不用再去别处猜
    assert kind.value in failures[-1], failures[-1]
    assert message in failures[-1], failures[-1]
    assert f"retriable={retriable}" in failures[-1], failures[-1]


async def test_failure_log_does_not_say_why(caplog: pytest.LogCaptureFixture, logged_in: TestEnv) -> None:
    """失败日志必须带上 error_code / 原因（否则运维只能读代码）。

    这是本模块唯一断言"修复后行为"的用例：原来这里只有 ``turn.failed |``
    （``detail`` 只在**未捕获异常**那条路上有值，``ModelError`` 这条路上是空的），
    用户因此看不到 ``quota_exhausted``/``unauthorized`` 这类可执行线索。
    """
    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    stub = _FailingGenerationRouter(
        logged_in.app.state.router,
        kind=ModelErrorKind.quota_exhausted,
        message="供应商 main 今天的 token 额度已用完（已用 200431 / 上限 200000）",
        retriable=False,
    )
    logged_in.app.state.router = stub
    try:
        with caplog.at_level(logging.INFO):
            await _post(logged_in, _event_body())
    finally:
        logged_in.app.state.router = stub._inner

    failures = [line for line in _bridge_lines(caplog) if "失败（failed）：" in line]
    assert failures, _bridge_lines(caplog)
    line = failures[-1]
    # 原因与错误码都要落在这一行里
    assert "quota_exhausted" in line, line
    assert "额度已用完" in line, line
    # 行为日志同样要能回答"为什么没接话"
    behaviors = _behavior_lines(caplog)
    assert any("quota_exhausted" in item for item in behaviors), behaviors


# ---------------------------------------------------------------- 3. 额度耗尽（真实 router + 真实记账）


async def test_quota_exhausted_blocks_every_turn_for_the_day(
    logged_in: TestEnv, caplog: pytest.LogCaptureFixture
) -> None:
    """日额度用完 → 这一家当天不再被调用 → 每一轮都 state=failed。

    覆盖 ``ModelRouter._quota_block`` 这条**真实**路径（不是 stub）：
    配置里给供应商一个日限额，先记一笔超过它，再让入站消息跑一轮。
    """
    from ling.infra.db import session_scope
    from ling.infra.security import SecretBox
    from ling.settings.service import ConfigService

    async with session_scope() as session:
        svc = ConfigService(
            session,
            SecretBox(logged_in.settings),
            test_mode_default=logged_in.settings.test_mode,
            persona_id=logged_in.settings.persona_id,
        )
        draft = await svc.save_draft(
            providers=[
                {
                    "provider_id": "fake",
                    "kind": "fake",
                    "enabled": True,
                    "daily_token_limit": 1000,
                }
            ]
        )
        await svc.publish(draft.revision)

    # 先记一笔超额用量（相当于今天早些时候把额度聊完了）
    await logged_in.app.state.quota.record("fake", 5000)

    fake = _FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    with caplog.at_level(logging.INFO):
        body = (await _post(logged_in, _event_body())).json()

    assert body["decision"] == "reply", body
    assert body["delivered"] is False, body
    # 额度耗尽**不是故障**（换家/等明天就好），必须能从这一句里看出来
    assert body["reason"] == (
        "轮次结束但没有可交付文本（state=failed，error=quota_exhausted）"
    ), body
    assert not fake.sent

    # 路由层必须喊出来（这条日志救过命：它把"额度"与"故障"分开）
    quota_lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "ling.models.router" and "额度已用完" in r.getMessage()
    ]
    assert quota_lines, [r.name for r in caplog.records]


# ---------------------------------------------------------------- 4. 职责解析不到模型（零调用）


async def test_unresolved_profile_fails_without_any_provider_call(
    logged_in: TestEnv, caplog: pytest.LogCaptureFixture
) -> None:
    """「快速」槽位没配、也没有显式绑定 → 一次 provider 调用都不发生。

    这条路**没有任何前置日志**（`_resolved_binding` 在调 provider 之前就抛了），
    与"密钥失效"在用户眼里完全一样。用真实 ``ModelRouter`` 复现。
    """
    from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
    from ling.ports.model import ChatMessageIn

    config = EffectiveConfig(
        revision=99,
        providers={
            "main": ProviderConfig(provider_id="main", kind="openai_compatible", enabled=True)
        },
        # 显式绑定指向一个**没有登记**的供应商：与"槽位没配"同一条解析出口
        profiles={"dialogue.fast": ProfileBinding(profile="dialogue.fast", provider_id="gone", model_id="m")},
        test_mode=False,
    )
    router = ModelRouter(logged_in.app.state.registry, logged_in.app.state.quota)
    with pytest.raises(ModelError) as exc:  # noqa: PT012 - 只为收错误
        async for _chunk in router.stream(
            config, profile="dialogue.fast", messages=[ChatMessageIn(role="user", content="111")]
        ):
            pass
    assert exc.value.kind is ModelErrorKind.unsupported
    assert "没有可用模型" in exc.value.message, exc.value.message
    # 关键：走到这一步**没有任何日志**——用户只能看到一个 state=failed
    assert not [
        r for r in caplog.records if r.name.startswith("ling.models.router")
    ], [r.getMessage() for r in caplog.records]
