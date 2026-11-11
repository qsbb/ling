"""能力标签必须**经过真实入站链路**才能到提示词。

## 这个文件补的是哪一环

`test_capability_poke_e2e.py` 直接构造 `ContextCompiler(session, channel_capabilities=caps)`，
证明的是"**编译器消费参数正确**"。它有一个致命盲区：

    把那 5 处 `channel_capabilities=` 全部删掉，那三条照样全绿。

因为它们根本不经过任何调用点。所以证据链缺的是：

| 层 | 谁证明 |
|---|---|
| 契约默认值正确 | `test_capability_declares_poke_explicitly` |
| 连接器声明被正确读取 | `test_capability_poke_e2e.py` |
| 编译器消费参数 | `test_capability_poke_e2e.py` |
| **调用点真的接线了** | **本文件** |
| **接线后拿到的是这条消息的来源实例** | **本文件** |

## 为什么必须是 HTTP 入站

只有走 `POST /v1/onebot/events` 才能同时经过：

    HTTP → 认领/幂等 → InboundBridge（持有**这条消息的** connector）
         → run_turn → _execute → ContextCompiler → 提示词

**AST 守卫替代不了**：它能证明"参数写在那儿"，证明不了"传进去的值对"。
删掉 `bridge.py` 那一行 `channel_capabilities=self._current_capabilities(),`
时，本文件必须红——这正是它存在的理由。

## 怎么观察提示词

回复已经流出去了，抓不到提示词。所以给 `TurnCoordinator` 包一层：记录
`run_turn` 的入参，再原样交给真实的 coordinator。

**只记录、不改变行为**——如果这层替换改变了传参，那测到的就不是真实链路了。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig

from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.kernel.turn import TurnCoordinator
from tests.integration.conftest import TestEnv

CFG = OneBotConfig(
    api_base="http://onebot.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-poke-e2e",
)
QQ_USER = "20002"
GROUP = "88888"


class FakeOneBot:
    """记录发出的请求并一律返回成功。回复内容不重要，重要的是它别报错。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        self.sent.append({"action": action, "body": body})
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"message_id": 1}})


class RecordingCoordinator:
    """记录 `run_turn` 的入参**和编译出来的提示词**，执行交给真实 coordinator。

    **不能只记录不执行**：那一轮就不会真的编译上下文，而我们要看的正是
    编译出来的提示词。也不能改写参数——改了测到的就不是真实链路。

    ## 为什么要截 `ContextCompiler.compile`

    光断言"`run_turn` 收到了 `supports_poke=True` 的能力对象"是不够的：
    那证明的是**参数传到了 `run_turn`**，不是**提示词里真的有 `[poke:`**。
    中间还隔着 `_execute` → `ContextCompiler(...)` → `capabilities` 车道
    三个环节，任何一环断了，参数照样在 `run_turn` 的入参里。

    所以这里在 `compile` 外层包一层，把**真正编译出来的 `CompiledContext`**
    抓下来——那才是模型看到的东西。

    用 `unittest.mock.patch` 而不是替换类：`turn.py` 在模块顶层
    `from ling.kernel.context import ContextCompiler`，替换类不会影响那个
    已绑定的引用（这正是 `docs/evidence/test-pollution-reload.md` 记的坑）。
    patch 属性本身可以工作，因为 `_execute` 里是模块内引用。

    ## 为什么必须消费 queue 到结束（这里踩过一次）

    `run_turn` 内部是：

    ```python
    task = asyncio.create_task(self._execute(...))    # turn.py:737
    ...
    return queue                                      # 立刻返回
    ```

    **`run_turn` 返回时 `_execute` 还没跑。** 所以第一版这样写是错的：

    ```python
    with patch.object(ContextCompiler, "compile", _spy):
        return await self._inner.run_turn(**kwargs)   # ← 返回时 compile 还没被调用
    # with 退出，spy 卸载
    ```

    `_execute` 要等事件循环下一轮才 `compile`，那时 **spy 已经不在了**，
    `recorder.compiled` 必然是空的 `[]`。**这是必然，不是偶发**——两条测试
    因此在干净工作区上稳定失败（而在被污染的中间状态上"通过"，因为那时
    改动让断言走了别的分支）。

    ## 正确做法：在 patch 内把这一轮跑完

    从 `run_turn` 拿到的 `queue` 里消费到 `None`（流结束标记）。那时
    `_execute` 一定已经跑完、`compile` 一定已经调用过，`compiled` 才有值。
    """

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self.calls: list[dict] = []
        self.compiled: list[object] = []

    async def run_turn(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(dict(kwargs))
        from unittest.mock import patch

        from ling.kernel import turn as turn_module

        real_compile = turn_module.ContextCompiler.compile

        async def _spy(self_, *args, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
            result = await real_compile(self_, *args, **kw)
            self.compiled.append(result)
            return result

        with patch.object(turn_module.ContextCompiler, "compile", _spy):
            queue = await self._inner.run_turn(**kwargs)
            # **必须消费到 `None`**：`run_turn` 起完后台 task 就返回了，
            # 不把这一轮跑完，spy 会在 `_execute` 调用 `compile` 之前卸载。
            # 见类 docstring 里那段事故说明。
            while True:
                event = await queue.get()
                if event is None:
                    break
            return queue

    def __getattr__(self, name: str):  # noqa: ANN204
        # `request_stop` / `partial_text` 这些仍要走真实实现
        return getattr(self._inner, name)


def _install(env: TestEnv, fake: FakeOneBot) -> OneBotV11Connector:
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


def _event_body(*, message_type: str = "private", msg_id: int = 777) -> bytes:
    payload = {
        "time": int(datetime.now(UTC).timestamp()),
        "self_id": 10001,
        "post_type": "message",
        "message_type": message_type,
        "message_id": msg_id,
        "user_id": int(QQ_USER),
        "message": "你好",
        "raw_message": "你好",
        "font": 0,
        "sender": {"nickname": "小不点", "sex": "male", "age": 18},
    }
    if message_type == "group":
        payload.update({"group_id": int(GROUP), "sub_type": "normal"})
    else:
        payload["sub_type"] = "friend"
    return json.dumps(payload).encode()


def _headers(body: bytes) -> dict[str, str]:
    mac = hmac.new(CFG.secret.encode(), body, hashlib.sha1).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Self-ID": CFG.self_id,
        "X-Signature": f"sha1={mac}",
    }


async def _post(env: TestEnv, body: bytes) -> httpx.Response:
    return await env.client.post("/v1/onebot/events", content=body, headers=_headers(body))


async def _run_inbound(env: TestEnv, body: bytes) -> RecordingCoordinator:
    """跑一条真实入站事件，返回记录了 `run_turn` 入参的 coordinator。"""
    # 用 `_state` 读：starlette 的 `State` 在属性不存在时抛 `AttributeError`，
    # `getattr(..., None)` 那套默认值写法对它不生效（`__getattr__` 直接抛）。
    inner = env.app.state._state.get("bridge_coordinator")  # noqa: SLF001
    if inner is None:
        inner = TurnCoordinator(
            router=env.app.state.router,
            actor_id="actor_ling",
            persona_id="persona_ling",
            cancels=env.app.state.cancels,
        )
    recorder = RecordingCoordinator(inner)
    # `starlette.datastructures.State` 在属性**不存在**时抛 `AttributeError`，
    # 而 `delattr` 也不被支持（它是 `__setattr__` 写进 `_state` 字典的）。
    # 所以用 `_state` 字典判断/还原，别用 `getattr(..., None)` + `delattr`。
    state = env.app.state._state  # noqa: SLF001 - starlette State 的唯一真实存储
    had_previous = "bridge_coordinator" in state
    previous = state.get("bridge_coordinator")
    env.app.state.bridge_coordinator = recorder
    try:
        response = await _post(env, body)
        assert response.status_code == 200, response.text
    finally:
        if had_previous:
            env.app.state.bridge_coordinator = previous
        else:
            state.pop("bridge_coordinator", None)
    return recorder


async def test_inbound_onebot_private_passes_capabilities_end_to_end(logged_in: TestEnv) -> None:  # noqa: F811
    """一条真实 OneBot 私聊消息 → 提示词里**必须**有 `[poke:`。

    这一条同时证明两件事：

    1. **调用点真的接线了**（删掉 `bridge.py` 那行 `channel_capabilities=`，它红）
    2. **传的是这个渠道的能力**（OneBot 声明 `supports_poke=True`）

    用**私聊**而不是群聊：私聊的 `render_expression_help` 不列 `[at:]`/`[quote:]`，
    所以 `[poke:` 在这个上下文里只可能来自 `supports_poke` 这一个判据。
    群聊会同时受 `is_group` 影响，两个变量混在一起，红了也分不清是谁的错。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    recorder = await _run_inbound(logged_in, _event_body(message_type="private"))

    assert recorder.calls, "入站消息没有走到 run_turn —— 链路断了，这条测不到接线"
    caps = recorder.calls[0].get("channel_capabilities")
    assert caps is not None, (
        "`run_turn` 收到的 channel_capabilities 是 None——"
        "说明 bridge 没接线，提示词会按'无渠道'处理，OneBot 用户看不到 [poke:…]"
    )
    assert getattr(caps, "supports_poke", False) is True, (
        f"OneBot 应当声明 supports_poke=True，实际能力声明是 {caps!r}"
    )

    # **关键断言**：看模型实际收到的提示词，而不是只看参数对象。
    # 中间还隔着 `_execute` → `ContextCompiler(...)` → `capabilities` 车道，
    # 任何一环断了，上面那句仍然会过。
    assert recorder.compiled, "这一轮没有编译上下文——链路没走到提示词"
    system_text = "\n".join(
        m.content for m in recorder.compiled[0].messages if m.role == "system"
    )
    assert "[poke:" in system_text, (
        "这条入站消息在 OneBot（支持拍一拍）上，但提示词里没有 `[poke:…]`——"
        "参数传到了 run_turn，却没进到模型看到的文本里"
    )


async def test_inbound_capabilities_come_from_this_instance(logged_in: TestEnv) -> None:  # noqa: F811
    """能力必须来自**这条消息的来源实例**，不是"随便某个连接器"。

    判据：把 `app.state.onebot_connector` 换成一个 `supports_poke=False` 的
    连接器，重复同一条入站消息——`run_turn` 收到的能力必须跟着变。

    ## 这条在防什么

    `InboundBridge` 是每条事件构造一次的，`connector=connector` 自然对应。
    但将来做多实例运行时改造时，如果改成"持有渠道→连接器表"或复用实例，
    就可能取到别的实例的能力。**取错号是静默的**：提示词里多一个 `[poke:…]`
    没有任何日志、也不报错，只是她会在 A 号的对话里做只有 B 号能做的动作。

    这条测试把"能力随实例变"这个不变量钉住。
    """
    fake = FakeOneBot()
    _install(logged_in, fake)
    await _bind(logged_in)

    # 换成一个"没有拍一拍"能力的连接器
    class _NoPokeConnector(OneBotV11Connector):
        def capabilities(self):  # noqa: ANN201
            caps = super().capabilities()
            return caps.model_copy(update={"supports_poke": False})

    logged_in.app.state.onebot_connector = _NoPokeConnector(
        CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )

    recorder = await _run_inbound(logged_in, _event_body(message_type="private", msg_id=778))

    assert recorder.calls, "入站消息没有走到 run_turn"
    caps = recorder.calls[0].get("channel_capabilities")
    assert caps is not None, "bridge 没把能力传给 run_turn"
    assert caps.supports_poke is False, (
        "当前实例声明 supports_poke=False，但 run_turn 收到的是 True——"
        "说明能力不是从这个实例取的"
    )

    # 和正向用例**配对**的负向断言，必须在同一次运行里同时成立。
    # 只有负向断言的话，"功能完全没接线"时它也会绿（因为什么都不出现）。
    assert recorder.compiled, "这一轮没有编译上下文"
    system_text = "\n".join(
        m.content for m in recorder.compiled[0].messages if m.role == "system"
    )
    assert "[poke:" not in system_text, (
        "这个实例声明不支持拍一拍，提示词却列出了 `[poke:…]`"
    )
