"""语音出站回复链：`[speak]` → 合成 → 投递，在**主回复路径**上真的接通了。

## 这个文件在守什么

`REQ-10` 的缺口是这个形状：链上每一节都写好了，**没有一节调用下一节**：

| 环节 | 接线前 |
|---|---|
| `parse_speak_choice` | 有实现，零调用点 |
| `SpeechOrchestrator.synthesize` | 有实现，零**生产**调用点 |
| `SpeechDelivery.deliver_speech` | 有实现，零**生产**调用点 |

所以这里的每一条都**打穿 `worker._reply_in_group_window`**（真实函数、真实
`DeliveryService`、真实数据库），而不是直接调那三个函数——后者只能证明它们
各自写对了，证明不了有人调它们。

## 四条边界，各打一条

1. `[speak]` + 已绑 TTS → 真的合成 + 语音投递（正文与音频**同一条**消息）；
2. 没用 `[speak]` → 纯文字，**没有**任何语音降级痕迹（TTS 一次都不碰）；
3. `[speak]` + **未绑** TTS → 降级发文字，**不是**静默失败；
4. `[speak]` + 合成失败 → 同样降级发文字。

## 它**不能**证明什么

- 真实三家供应商的协议与音色：这里用假 TTS，只证明"谁在什么时候调它"；
- 音色是否配好：配置层当前**没有音色字段**，所以"真的合成"那条要显式注入音色；
  缺音色时走降级，由 `test_没有音色时降级发文字` 守着；
- OneBot 侧 SILK/record 封装：这里断言的是 `OutboundMessage.attachments` 里有
  `audio` 附件（渠道适配器的输入契约），不是最终平台字节。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import ling.worker as worker_module
from ling.contracts.v1 import (
    CapabilityDescriptor,
    DeliveryReceipt,
    DeliveryState,
    Modality,
)
from ling.domains.speech.orchestration import SYNTHESIS_SCOPE
from ling.infra.db import session_scope
from ling.infra.models import IdempotencyKey, Message, Operation, Window
from ling.infra.security import SecretBox
from ling.kernel.turn import TurnStreamEvent
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.speech import (
    AudioSpec,
    MiniMaxVoice,
    SpeechError,
    SpeechErrorKind,
    SpeechUsage,
    TtsChunk,
    TtsEnd,
    TtsStart,
)
from ling.settings.service import ConfigService

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
WINDOW_REF = "100200300"
AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 4

#: 语音标签。字面量**不从被测模块取**：取的话模块里改坏了标签，测试跟着坏。
SPEAK = "[speak]"


# --------------------------------------------------------------------------- #
# 假件：都不碰网络，但都记账
# --------------------------------------------------------------------------- #


class FakeTts:
    """按端口签名实现的假 TTS。`calls` 是花钱次数。"""

    provider_id = "fake_tts"

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.calls = 0
        self.texts: list[str] = []
        self._fail_with = fail_with

    async def synthesize(self, request, *, call):  # noqa: ANN001,ANN201
        self.calls += 1
        self.texts.append(request.text)
        if self._fail_with is not None:
            raise self._fail_with
        spec = AudioSpec(
            mime_type="audio/mpeg",
            container="mp3",
            codec="mp3",
            sample_rate_hz=24000,
            channels=1,
        )
        yield TtsStart(spec=spec)
        yield TtsChunk(sequence=0, audio=AUDIO)
        yield TtsEnd(
            spec=spec,
            provider_request_id="req_fake_1",
            usage=SpeechUsage(audio_seconds=1.5, usage_source="provider"),
        )

    def capabilities(self, model_id: str):  # noqa: ANN001,ANN201
        from ling.ports.speech import TtsCapabilities

        return TtsCapabilities(formats=frozenset({"mp3"}), streaming="buffered")

    async def cancel(self, *, attempt_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


class FakeConnector:
    """假渠道。`sent` 是**真的**被要求外发的那些消息。"""

    connector_id = "onebot"
    instance_id = "onebot-fake"

    def __init__(self) -> None:
        self.sent: list[object] = []

    def capabilities(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id="onebot",
            version="1.0",
            enabled=True,
            supports_private=True,
            supports_group=True,
            # **必须显式列 audio**：只写 `supports_media=True` 会被解读成
            # "只能发图片"。
            supported_media=[Modality.audio, Modality.image],
            max_text_length=2000,
        )

    async def send(self, message):  # noqa: ANN001,ANN201
        self.sent.append(message)
        return DeliveryReceipt(
            receipt_id=f"rcpt_{len(self.sent)}",
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=DeliveryState.delivered,
            platform_message_id=f"pm_{len(self.sent)}",
            observed_at=datetime.now(UTC),
            detail="",
        )

    async def query_receipt(self, query):  # noqa: ANN001,ANN201
        raise NotImplementedError


class _FakeCoordinator:
    """替换 `TurnCoordinator`：不动模型，直接把"她说的话"吐出来。

    她说什么由 `reply_text` 决定——**这就是被测输入**（`[speak]` 在不在开头）。
    """

    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.calls = 0

    async def run_turn(self, **kwargs):  # noqa: ANN003
        self.calls += 1
        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(
            TurnStreamEvent(
                event="turn.completed",
                data={
                    "state": "completed",
                    "message": {"text": self.reply_text},
                    "expression": {"at": [], "quote": None, "poke": []},
                },
            )
        )
        await queue.put(None)
        return queue


class _Result:
    """调度器决策结果里，本路径只用到这几个字段。"""

    decision = "replied"
    reason = ""
    batch_text = "在吗"
    speaker_subject_id = None


# --------------------------------------------------------------------------- #
# 夹具与工具
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _no_part_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """分段之间的节奏停顿对测试只是等待。

    补丁打在 `chunking` 模块上：`worker._reply_in_group_window` 是**函数体内**
    import 的，打 `ling.worker` 上不生效（那个名字不属于 worker）。
    """
    import ling.capabilities.chunking as chunking

    monkeypatch.setattr(chunking, "PART_DELAY_SECONDS", 0)


class _Registry(ProviderRegistry):
    """只要"有一个 registry"：本文件替换掉了 `TurnCoordinator`，从不真的调模型。

    但 `_reply_in_group_window` 在**建轮次之前**就要求 `_MEMORY_ROUTER` 非空
    （`模型路由未就绪`），所以这条是必需的接线，不是装饰。
    """

    def __init__(self) -> None:
        super().__init__(lambda _pid: None)


@pytest.fixture(autouse=True)
def _router_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_module, "_MEMORY_ROUTER", ModelRouter(_Registry()))


async def _make_window(env, *, channel: str = "onebot") -> Window:  # noqa: ANN001
    """一个群窗口（`channel="onebot"` 才走投递；`web` 会被短路）。"""
    from ling.domains.conversation.service import ConversationService
    from ling.kernel.visibility import GROUP_VISIBILITY

    async with session_scope() as session:
        conv = ConversationService(session, actor_id=ACTOR)
        window = await conv.create_window(
            subject_id=env.subject_id, title=WINDOW_REF, channel=channel
        )
        window.visibility = GROUP_VISIBILITY
        session.add(window)
        await session.commit()
        return window


def _patch_turn_coordinator(monkeypatch: pytest.MonkeyPatch, reply_text: str) -> None:
    """`_reply_in_group_window` 在函数体内 import，所以补丁打在那条 import 路径上。"""
    import ling.kernel.turn as turn_module

    monkeypatch.setattr(turn_module, "TurnCoordinator", lambda **kw: _FakeCoordinator(reply_text))


async def _config(session):  # noqa: ANN001,ANN202
    from ling.config import get_settings

    settings = get_settings()
    return ConfigService(
        session,
        SecretBox(settings),
        test_mode_default=settings.test_mode,
        persona_id=settings.persona_id,
    )


async def _run_reply(
    env,  # noqa: ANN001
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply_text: str,
    connector: FakeConnector,
    channel: str = "onebot",
) -> tuple[bool, str]:
    """走 `_reply_in_group_window`：真实函数、真实投递服务、真实数据库。"""
    monkeypatch.setattr("ling.worker._group_reply_connector", lambda ch: connector)
    _patch_turn_coordinator(monkeypatch, reply_text)

    window = await _make_window(env, channel=channel)
    async with session_scope() as session:
        return await worker_module._reply_in_group_window(
            session=session,
            config=await _config(session),
            window=window,
            result=_Result(),
        )


def _bind_tts(monkeypatch: pytest.MonkeyPatch, provider, *, ok: bool = True, kind: str = "minimax_tts") -> None:  # noqa: ANN001
    """把 `build_tts_provider` 换成"已经造好一个假适配器"，不碰网络。"""

    async def fake_build(config=None, *, secret_reader=None):  # noqa: ANN001,ANN202,ARG001
        from ling.domains.speech.providers import TtsBindingView
        from ling.ports.speech import UnconfiguredSpeechProvider

        if not ok:
            return TtsBindingView(
                provider=UnconfiguredSpeechProvider("没有绑定 speech.tts"),
                ok=False,
                reason="没有绑定 speech.tts",
            )
        return TtsBindingView(
            provider=provider,
            ok=True,
            provider_id="mm",
            model_id="speech-2.8-hd",
            kind=kind,
            source="explicit",
            implementation="MiniMaxTts",
        )

    monkeypatch.setattr("ling.domains.speech.providers.build_tts_provider", fake_build)


def _give_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """给一个音色。

    当前 `EffectiveConfig` **没有任何音色字段**，`worker._voice_for_tts` 恒为
    `None`（不替她选嗓音）。所以"真的合成"这条要在这里把音色补上；生产上缺
    音色走降级，由 `test_没有音色时降级发文字` 守着。
    """

    async def fake_voice(kind: str, *, effective=None):  # noqa: ANN001,ANN202,ARG001
        return MiniMaxVoice(voice_id="voice_1")

    monkeypatch.setattr("ling.worker._voice_for_tts", fake_voice)


def _audio(message) -> list:  # noqa: ANN001
    return [a for a in (getattr(message, "attachments", None) or []) if a.kind.value == "audio"]


async def _deliveries() -> list[Operation]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(select(Operation).where(Operation.kind == "delivery"))
            ).scalars()
        )


# --------------------------------------------------------------------------- #
# 一、`[speak]` + 已绑 TTS → 真的合成 + 语音投递
# --------------------------------------------------------------------------- #


async def test_speak_with_tts_really_synthesizes_and_sends_voice(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """她写了 `[speak]`、TTS 可用 → provider 被调一次，外发的是**带音频**的消息。

    **必须失败于**「忽略 `parse_speak_choice` 的结果」：恒当 text 时 provider
    一次都不会被调（`tts.calls == 0`），`attachments` 里也没有 audio。
    """
    tts = FakeTts()
    _bind_tts(monkeypatch, tts)
    _give_voice(monkeypatch)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in,
        monkeypatch,
        reply_text=f"{SPEAK} 今天有点累，但看到你消息还是挺高兴的。",
        connector=connector,
    )

    assert delivered is True, reason
    assert tts.calls == 1, f"provider 被调用 {tts.calls} 次（期望 1）"
    # 标签**必须**剥掉：念出来等于把说明书读给用户。
    assert tts.texts[0] == "今天有点累，但看到你消息还是挺高兴的。", tts.texts

    assert len(connector.sent) == 1, f"期望一条消息，实际 {len(connector.sent)}"
    message = connector.sent[0]
    audio = _audio(message)
    assert len(audio) == 1, f"外发的消息里没有语音附件：{message.attachments}"
    assert audio[0].ref, "语音附件没有可解析的引用"
    # 「渠道上只发语音，后台显示语音标签 + 文字」：**一条消息两个面**。
    assert message.text == "今天有点累，但看到你消息还是挺高兴的。", (
        f"正文丢了或带了标签：{message.text!r}"
    )


async def test_library_shows_speech_tag_and_text(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """「默认只发语音但后台显示语音标签 + 文字」——库里两个面都落了账。

    operation 的 `speech.digest` 是后台显示"语音"标签的依据，`text_digest` 是
    正文的摘要（对账用）。少了任何一个，后台就只能显示一条空消息或一个播放器。
    """
    _bind_tts(monkeypatch, FakeTts())
    _give_voice(monkeypatch)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 听到了吗", connector=connector
    )
    assert delivered is True, reason

    # **先证明语音真的发了**，否则下面对 operation 的断言在"根本没合成"时
    # 同样成立——那样这条测试接线前就是绿的（实测确实如此），等于白写。
    assert _audio(connector.sent[0]), "没有发出语音：下面的后台字段断言证明不了接线"

    rows = await _deliveries()
    assert len(rows) == 1, f"期望一条投递 operation，实际 {len(rows)}"
    payload = dict(rows[0].payload or {})
    assert "speech" in payload, f"operation 里没有语音信息（后台无法显示语音标签）：{payload}"
    assert payload["speech"]["digest"], payload
    assert payload.get("text_digest"), "正文摘要缺失：后台/对账拿不到她说了什么"


# --------------------------------------------------------------------------- #
# 二、没用 `[speak]` → 纯文字，无降级痕迹
# --------------------------------------------------------------------------- #


async def test_without_speak_is_plain_text_with_no_speech_traces(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """没写 `[speak]` → 纯文字；TTS **一次都不碰**，也没有降级痕迹。

    **必须失败于**「无差别地走语音链」：那样 `tts.calls` ≥1、外发消息带 audio、
    operation 里出现 `speech` / `speech_fallback`。

    「那会让人以为她本来想说语音却没说成」——所以纯文字路径连字段都不能有。
    """
    tts = FakeTts()
    _bind_tts(monkeypatch, tts)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in,
        monkeypatch,
        reply_text="今天有点累，但看到你消息还是挺高兴的。",
        connector=connector,
    )

    assert delivered is True, reason
    assert tts.calls == 0, "她没写 [speak]，provider 却被调了"
    assert len(connector.sent) == 1
    message = connector.sent[0]
    assert not _audio(message), f"纯文字消息里出现了音频：{message.attachments}"
    assert message.text == "今天有点累，但看到你消息还是挺高兴的。"

    rows = await _deliveries()
    assert len(rows) == 1
    payload = dict(rows[0].payload or {})
    for key in ("speech", "speech_fallback", "speech_failed_reason"):
        assert key not in payload, f"纯文字消息留下了语音痕迹 {key}：{payload}"


async def test_web_channel_never_synthesizes(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """Web 渠道：轮次已经写进 messages，**不合成**、也不外发。

    这条防的是"顺手把语音接在渠道判断之前"：那样 Web 会话每轮都会为一条根本
    发不出去的语音花钱。
    """
    tts = FakeTts()
    _bind_tts(monkeypatch, tts)
    _give_voice(monkeypatch)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 你好", connector=connector, channel="web"
    )

    assert delivered is True, reason
    assert tts.calls == 0, "Web 渠道不该合成"
    assert not connector.sent, "Web 渠道没有外部渠道可发"


# --------------------------------------------------------------------------- #
# 三、`[speak]` + 未绑 TTS → 降级发文字
# --------------------------------------------------------------------------- #


async def test_speak_without_tts_degrades_to_text(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """写了 `[speak]` 但**没绑 TTS** → 正文照发，**不能**报错、不能不发。

    **必须失败于**「没绑 TTS 就抛/返回失败」：那种实现下 `delivered` 是 False，
    调用方会把它记成"她决定回却没发出去"。
    """
    _bind_tts(monkeypatch, None, ok=False)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 我没配语音，但话还是要说的。",
        connector=connector,
    )

    assert delivered is True, f"没绑 TTS 不该让整轮回复失败：{reason}"
    assert len(connector.sent) == 1, "正文没有发出去"
    message = connector.sent[0]
    assert not _audio(message)
    # 降级也要剥掉标签：把 `[speak]` 发给用户是把说明书读出来。
    assert message.text == "我没配语音，但话还是要说的。", message.text


async def test_no_voice_configured_degrades_instead_of_guessing(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """TTS 已绑、但**没有音色配置** → 降级发文字，且**不替她选嗓音**。

    一个"随便挑一家默认嗓音"的实现会让 `tts.calls == 1`——那正是
    "她没选却替她选"。
    """
    tts = FakeTts()
    _bind_tts(monkeypatch, tts)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 没有音色也要说话", connector=connector
    )

    assert delivered is True, reason
    assert tts.calls == 0, "没有音色却调了 provider（等于替她选了声音）"
    assert len(connector.sent) == 1
    assert not _audio(connector.sent[0])
    assert connector.sent[0].text == "没有音色也要说话"


# --------------------------------------------------------------------------- #
# 四、`[speak]` + 合成失败 → 降级发文字
# --------------------------------------------------------------------------- #


async def test_synthesis_failure_degrades_to_text(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """合成抛错 → **降级发文字**，不是静默失败。

    **必须失败于**「合成失败就当这一轮失败」：那种实现下 `delivered` 是 False
    且 `connector.sent` 为空——用户什么也收不到。
    """
    failing = FakeTts(fail_with=SpeechError(SpeechErrorKind.unavailable, "上游 503"))
    _bind_tts(monkeypatch, failing)
    _give_voice(monkeypatch)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 合成会失败，但话得送到。",
        connector=connector,
    )

    assert delivered is True, f"合成失败不该把整轮回复判成失败：{reason}"
    assert failing.calls == 1, "合成没有被尝试（那证明的不是降级，是没接线）"
    assert len(connector.sent) == 1, "合成失败后正文没有发出去"
    message = connector.sent[0]
    assert not _audio(message), "合成失败却发了音频附件？"
    assert message.text == "合成会失败，但话得送到。", message.text


# --------------------------------------------------------------------------- #
# 幂等与"不多发"：同样是接线正确性的一部分
# --------------------------------------------------------------------------- #


async def test_synthesis_idempotency_key_is_the_platform_turn_id(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """合成幂等表里留下的是 **platform turn id**，不是每次新建的 `turn.id`。

    幂等键每次都不相同 = 没有幂等键：平台事件重投就会重复合成、重复计费。
    """
    _bind_tts(monkeypatch, FakeTts())
    _give_voice(monkeypatch)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 留个键", connector=connector
    )
    assert delivered is True, reason

    async with session_scope() as session:
        keys = list(
            (
                await session.execute(
                    select(IdempotencyKey).where(IdempotencyKey.scope == SYNTHESIS_SCOPE)
                )
            ).scalars()
        )
    assert len(keys) == 1, f"期望一条合成幂等记录，实际 {len(keys)}"
    turn_part = keys[0].key.split(":")[0]
    assert not turn_part.startswith("turn_"), (
        f"幂等键用了每次新建的 turn.id（{keys[0].key}）：重投会重复合成"
    )


async def test_reply_body_is_not_duplicated_when_degraded(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """降级时**只发一条**正文，不因为"语音没成"再补发一条。"""
    _bind_tts(monkeypatch, None, ok=False)
    connector = FakeConnector()

    await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 只说一次", connector=connector
    )

    assert len(connector.sent) == 1, f"发了 {len(connector.sent)} 条：正文重复了"
    assert connector.sent[0].text == "只说一次"


async def test_tag_never_leaks_into_later_parts(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """超长回复被切成多段时，`[speak]` **不在任何一段**里。

    `plan_parts` 的硬约束是"各段按序拼接 == 原文本"。先分段后剥标签的实现会把
    标签留在第一段开头（并被念出来）。
    """
    _bind_tts(monkeypatch, None, ok=False)

    long_body = "，".join(f"第{i}句话说说最近的事情" for i in range(1, 60))
    connector = FakeConnector()
    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} {long_body}", connector=connector
    )

    assert delivered is True, reason
    assert len(connector.sent) >= 1, "什么都没发出去"
    for message in connector.sent:
        assert SPEAK not in message.text, f"标签漏进了正文：{message.text[:40]!r}"


async def test_voice_reply_leaves_exactly_one_delivery_operation(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """语音投递成功时只多**一条**投递记录（不是"语音一条 + 文字一条"）。

    「一条消息，两个面」：把正文抄成第二条消息的实现会在这里红。
    """
    _bind_tts(monkeypatch, FakeTts())
    _give_voice(monkeypatch)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 一条就够", connector=connector
    )
    assert delivered is True, reason

    rows = await _deliveries()
    assert len(rows) == 1, f"期望一条投递 operation，实际 {len(rows)}"
    async with session_scope() as session:
        messages = list(
            (await session.execute(select(Message).where(Message.role == "actor"))).scalars()
        )
    # 本路径**不落** actor message（那是 `TurnCoordinator` 的事，这里被替换掉了）。
    assert messages == [], f"投递路径自己写了消息行：{messages}"
