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

1. `[speak]` + 已绑 TTS → 真的合成 + 语音投递（外发**只有音频**，`text=""`）；
2. 没用 `[speak]` → 纯文字，**没有**任何语音降级痕迹（TTS 一次都不碰）；
3. `[speak]` + **未绑** TTS → 这一轮没语音，但仍在装配阶段，**正文照发**；
4. `[speak]` + 合成失败 → **默认什么都不发**（不偷偷补文字），状态如实记录。

**3 与 4 不是同一件事，判据也不同。** 3 发生在 `_prepare_speech_reply` 返回
`None`（装配没成），那一轮压根没有表达形态可选，正文照发；4 发生在她**已经选了
语音**、意图已经在途之后才失败——§17.5 要求这种失败"不把原话改发文字"。
把 3 也改成不发，会让"没配音色"这种运维状态吞掉她的整条回复。

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
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import ling.worker as worker_module
from ling.capabilities.speech_delivery import SpeechDeliveryRequest, SpeechDeliveryState
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

#: 空串（以及纯空白）的 `delivery_text_digest`。语音消息的 `text=""` 让它成为
#: 每条语音 operation 的 `text_digest`——见 `test_正文在Message里_...` 的说明。
EMPTY_TEXT_DIGEST = "sha256:e3b0c44298fc1c149afbf4c8996fb924"


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
    monkeypatch.setattr("ling.worker._group_reply_connector", AsyncMock(return_value=connector))
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
    # **外发不带正文**（§17.5：「无 caption、无正文、无补空格」）。
    #
    # 这条断言原来写的是 `message.text == "今天有点累…"` —— 那一条把
    # 「正文与语音同时外发」断言成了正确行为。判据一直错，不是实现变了。
    assert message.text == "", f"外发消息里带了正文（应为空串）：{message.text!r}"


async def test_library_shows_speech_tag_and_text(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """「默认只发语音但后台显示语音标签 + 文字」——库里两个面都落了账。

    operation 的 `speech.digest` 是后台显示"语音"标签的依据。语音 operation
    **刻意没有** `text_digest`（正文为空，见
    `test_正文在Message里_外发正文为空_不落payload` —— 空的摘要是用来匹配
    所有语音的万能通行证，§17.5 明令禁止），正文那一半由 `Message.text` 守。

    **这条的判据改过两次，两次都是判据错、不是实现变。**

    1. 它原来断言「正文在 operation 的摘要里」，暗示正文该跟着语音一起走；
       但 §17.5 说的是**摘要**（不能还原），完整正文在 `Message.text` 里。
    2. 于是改成"摘要必须是**非空**的"——而 `text=""` 时
       `delivery_text_digest` 恒为 `sha256:e3b0c442…`（空输入的 sha256），
       这个"非空"只是字符串非空，它**不区分任何两条消息**。那条断言等于
       把「空摘要匹配所有语音」这个缺陷断言成了正确行为。

    现在 operation 侧只守语音标签，并在**负向**断言里钉住空摘要不出现。
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
    # 正向配对：这条 operation 确实是那条语音的（否则"没有摘要"在"根本没建
    # operation"时也成立）。
    assert payload["media_count"] == 1, f"语音没被记成媒体意图：{payload}"
    # 空正文不落摘要：那个常量对任何两条语音都相同，会把别人的消息认成自己的。
    assert payload.get("text_digest") is None, (
        f"语音 operation 落了正文摘要（空正文摘要会匹配所有语音）："
        f"{payload.get('text_digest')!r}"
    )


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


async def test_synthesis_failure_sends_nothing_by_default(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """合成抛错 → **默认什么都不发**，也不是把这一轮判成失败。

    **判据当初写反了。** 这条原来叫 `..._degrades_to_text`，断言「正文必须
    发出去」；§17.5 明写「合成／转码／投递失败默认**不把原话改发文字**；只记录
    准确状态，后台可重试」。所以现在守着的是三件事同时成立：

    - 合成**真的被尝试过**（`failing.calls == 1`）——否则证明的不是降级，是没接线；
    - 外发**零条**（没有偷偷补文字，也没有补一条空消息）；
    - 这一轮**没有被判成失败**（`delivered is True`）——"没发"不等于"投递故障"。

    降级发文字仍然可选，由 `test_显式text降级才补文字` 守着。
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
    assert not connector.sent, (
        f"合成失败后偷偷补发了 {len(connector.sent)} 条："
        f"{[getattr(m, 'text', None) for m in connector.sent]}"
    )


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


# --------------------------------------------------------------------------- #
# 五、配置里真的配了音色 → 真的出声（**不 patch `_voice_for_tts`**）
#
# 这一节与上面几节的区别只有一处：上面用 `_give_voice` 把音色**注入**进去，
# 所以它们证明的是"有音色就能出声"；这里从**配置层**配一遍（真实
# `save_draft` → 真实 `effective()` → 真实 `_voice_for_tts`），证明的是
# "用户在设置里填的音色，最终能被那一环读出来"。
#
# 缺了这一节，"配置读不进运行时"这个缺陷在所有测试里都是不可见的——
# 因为注入绕过了读取。
# --------------------------------------------------------------------------- #


async def _draft_voice(session, voice: dict) -> None:  # noqa: ANN001
    """走**真实**的 `save_draft` + `publish` 把音色写进已发布修订。

    为什么要发布：`ConfigService.effective()` 只认**已发布**那一行，草稿
    对运行时不可见。只写草稿的话 `_voice_for_tts` 读到的是引导配置，这条
    e2e 就在"什么都没配"的状态下跑——那证明不了任何事（实测过：它的症状
    正是日志里那句"还没配置音色"）。

    为什么先断言没有音色校验错误：合法配置被拦下时 `publish` 抛 422，
    失败信息指向"发布失败"，而真正的原因在一个不该报错的字段上。
    """
    config = await _config(session)
    result = await config.save_draft(dialogue={"voice": voice}, note="配一个音色")
    voice_errors = [
        i for i in result.validation_errors if i.field.startswith("dialogue.voice")
    ]
    assert not voice_errors, f"合法的音色配置被校验拦下了：{voice_errors}"
    await config.publish(result.revision)


async def test_configured_minimax_voice_really_synthesizes(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """设置里配好 MiniMax 音色 + 写 `[speak]` → **真的合成**，不是降级。

    **必须失败于**：
    - `_voice_for_tts` 恒返回 `None`（补音色之前的状态）→ `tts.calls == 0`；
    - 配置读不进运行时（serde 漏字段 / 补丁丢掉 voice）→ 同上；
    - 校验把合法配置拦下 → `save_draft` 里那条断言先红。

    这条**不 patch** `_voice_for_tts`，所以它走的是生产上那条路：
    `effective.dialogue.voice` → 值对象 → `synthesize(voice=...)`。
    """
    tts = FakeTts()
    _bind_tts(monkeypatch, tts)
    connector = FakeConnector()

    window = await _make_window(logged_in, channel="onebot")
    async with session_scope() as session:
        await _draft_voice(
            session,
            {"kind": "minimax_tts", "voice_id": "voice_from_settings", "speed": 1.1},
        )

    monkeypatch.setattr("ling.worker._group_reply_connector", AsyncMock(return_value=connector))
    _patch_turn_coordinator(monkeypatch, f"{SPEAK} 配了音色就该出声。")
    async with session_scope() as session:
        delivered, reason = await worker_module._reply_in_group_window(
            session=session,
            config=await _config(session),
            window=window,
            result=_Result(),
        )

    assert delivered is True, reason
    assert tts.calls == 1, f"provider 被调用 {tts.calls} 次：配好的音色没有走到合成"
    assert len(connector.sent) == 1
    message = connector.sent[0]
    assert _audio(message), f"配了音色却发了纯文字（降级了）：{message.attachments}"
    # 2026-11-12 修正：原来的 `message.text == "配了音色就该出声。"` 把
    # 「语音 + 正文同时外发」当成了正确形态。§17.5 的 wire 形态是
    # `OutboundMessage(text="", attachments=[audio], ...)`——外发**没有**正文。
    # 判据一直错，不是因为实现变了。
    assert message.text == "", f"外发消息里带了正文（应为空串）：{message.text!r}"


async def test_configured_voice_of_another_provider_does_not_guess(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """配的是 MiniMax 音色，而这一轮 TTS 是 FishAudio → 降级发文字。

    与上一条**配对**：只测"配好了会出声"，一个"不管配的是哪家都硬塞进去"的
    实现同样全绿。这条要的是"两边不一致时**不猜**"。
    """
    tts = FakeTts()
    _bind_tts(monkeypatch, tts, kind="fishaudio_tts")
    connector = FakeConnector()

    window = await _make_window(logged_in, channel="onebot")
    async with session_scope() as session:
        await _draft_voice(session, {"kind": "minimax_tts", "voice_id": "voice_1"})

    monkeypatch.setattr("ling.worker._group_reply_connector", AsyncMock(return_value=connector))
    _patch_turn_coordinator(monkeypatch, f"{SPEAK} 别猜我的声音。")
    async with session_scope() as session:
        delivered, reason = await worker_module._reply_in_group_window(
            session=session,
            config=await _config(session),
            window=window,
            result=_Result(),
        )

    assert delivered is True, reason
    assert tts.calls == 0, "跨厂商硬塞了音色（§17.2 撤销了映射表，不该猜）"
    assert not _audio(connector.sent[0])
    assert connector.sent[0].text == "别猜我的声音。"


# --------------------------------------------------------------------------- #
# 六、外发形态与后台投影：§17.5 的 wire 契约（REQ-12）
#
# 这一节守的是设计文档 `docs/design/SPEECH-IO-ARCHITECTURE.md` §17.5 的两句话：
#
#   > `OutboundMessage(text="", attachments=[audio asset], mentions=[], ...)`；
#   > 一个语音 part，无 caption、无正文、**无补空格**／额外 @ 文本。
#
#   > 合成／转码／投递失败默认**不把原话改发文字**；只记录准确状态，后台可重试。
#
# 在此之前，实现与测试**两边都反着**：外发带正文、失败默认补文字。所以这里的
# 每条都同时是「新行为」与「旧判据写反了」的证据。
# --------------------------------------------------------------------------- #


async def test_外发的语音消息正文是空串(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """§17.5：外发 `OutboundMessage.text` **精确等于空串**。

    **必须失败于**「外发带正文」（`text=request.text`）：那时它是"听到了吗"。
    **必须也失败于**「补空格」（`text=" "`）：所以断言是 `== ""`，不是
    `not text.strip()`——后者会让 `" "` 通过，而设计文档点名禁止"补空格"。
    """
    _bind_tts(monkeypatch, FakeTts())
    _give_voice(monkeypatch)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 听到了吗", connector=connector
    )
    assert delivered is True, reason
    assert len(connector.sent) == 1, f"期望一条消息，实际 {len(connector.sent)}"

    message = connector.sent[0]
    # 先钉住"这确实是语音消息"：没有音频时下面断言正文为空也成立，
    # 那样它证明不了任何与语音有关的事。
    assert len(_audio(message)) == 1, f"没有发出语音：{message.attachments}"
    assert message.text == "", (
        f"外发消息的 text 应为空串，实际 {message.text!r}（带正文 = 泄漏；"
        f"' ' = 补空格，两者都被 §17.5 禁止）"
    )


async def test_外发段里语音前面没有正文段(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """§17.5：平台上那条消息**没有正文段**（也不靠一个空 text 段占位）。

    这条走 OneBot **真实**的 `build_outbound_segments`，而不是看我们自己的
    `OutboundMessage` 长什么样——两者不是一回事：构造出 `text=""` 之后，
    如果渠道适配器自己补了东西，用户的平台上仍然会出现文字。

    **必须失败于**「外发带正文」：`text="听到了吗"` 会多出一个
    `{'type': 'text', 'data': {'text': '听到了吗'}}` 段。
    **必须失败于**「补空格」：`text=" "` 会多出一个 `text == " "` 的段。
    """
    from connectors.onebot.message import build_outbound_segments

    from ling.ports.channel import iter_outbound_attachments

    _bind_tts(monkeypatch, FakeTts())
    _give_voice(monkeypatch)
    connector = FakeConnector()

    delivered, reason = await _run_reply(
        logged_in, monkeypatch, reply_text=f"{SPEAK} 听到了吗", connector=connector
    )
    assert delivered is True, reason

    message = connector.sent[0]
    voice_segments = [
        {"type": "record", "data": {"file": a.ref}} for a in iter_outbound_attachments(message)
    ]
    assert len(voice_segments) == 1, f"语音段没构造出来：{voice_segments}"

    segments = build_outbound_segments(
        message.text,
        media_refs=list(message.media_refs),
        # §17.5 的 wire 形态里 `mentions=[]`：带 @ 时 `build_outbound_segments`
        # 会为了排版垫一个空格 text 段（那是它处理 @ 的既有行为，与正文无关），
        # 所以另有一条 `..._带艾特时也只多一个空格段` 把 @ 的情形钉在"只有空格"上。
        mention_ids=[],
        reply_to=None,
        voice_segments=voice_segments,
    )
    # 纯语音的 wire 只保留 record，不附带空 text 占位。
    text_segments = [s for s in segments if s["type"] == "text"]
    assert text_segments == [], f"语音消息不应带 text 段：{segments}"
    # 正向配对：语音段**确实**在，且排在最后。否则"没有正文"在"什么都没发"
    # 的实现下也成立。
    assert segments[-1] == voice_segments[0], segments
    assert [s["type"] for s in segments if s["type"] != "text"] == ["record"], segments


def test_语音消息带艾特时也只多一个空格段() -> None:
    """带 @ 时 `build_outbound_segments` 会垫一个空格段——但**只有空格**。

    这是"补空格"唯一允许出现的位置（为了 @ 与正文不黏连的排版），而且此时
    **没有正文**。断言整条消息里所有 text 段都是空白（`" "` 与 `""`），
    并且**没有一段含正文**——`data.text` 为空串/空格不等于"没泄漏"，要另配
    一条正向对照证明"真带正文时它会出现在某一段里"。

    **必须失败于**「外发 `text` 带正文」：那样会多出一个 `text == "听到我了吗"`
    的段，下面的正向对照正是同一个形态。
    """
    from connectors.onebot.message import build_outbound_segments

    body = "听到我了吗"
    voice = [{"type": "record", "data": {"file": "asset:abc"}}]

    # 空串（= 实现现在的 `text=""`）：@ 只带来"@ 段 + 空格段"，没有正文。
    segments = build_outbound_segments(
        "", mention_ids=["100200300"], voice_segments=voice
    )
    assert [s["type"] for s in segments] == ["at", "text", "record"], segments
    assert all(s["data"]["text"].strip() == "" for s in segments if s["type"] == "text"), (
        segments
    )
    assert body not in str(segments)
    assert segments[-1] == voice[0], "语音段必须在最后"

    # 反向配对：真的带正文时，正文**会**作为独立 text 段出现——证明上面
    # "没有正文"不是一句同义反复。
    leaked = build_outbound_segments(
        body, mention_ids=["100200300"], voice_segments=voice
    )
    assert {"type": "text", "data": {"text": body}} in leaked, leaked


async def test_正文在Message里_外发正文为空_不落payload(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """正文落在 `Message.text`（后台那一面），**不进** `operations.payload`。

    判据的两侧各管一件事，缺一半就没有区分力：

    - `Message.text` 有完整正文 → "外发不带正文"**不是**把内容弄丢了；
      §17.5 的 Message 行写的是 `text=canonical_text`。
    - `operations.payload` **没有**那个非空 text 段（没有 `text` 键、没有
      `text_digest`）→ 不落第二份正文，也不落它的摘要。依据
      `ports/channel.py::delivery_text_digest`：「把回复正文抄进
      `operations.payload` 等于给内容开第二份副本」。

    `Message` 那一行由 `TurnCoordinator` 落，而本文件把 `TurnCoordinator`
    替换成了假件（它不写库），所以这里**直接构造**那条 actor 消息作为前置
    条件——这条测试守的是"投递 operation 不落正文"，不是"谁写了 Message"。

    **必须失败于**「把正文塞进 payload_extra」（例如为了"后台能显示"而补一份）：
    那时 `payload` 里会出现 `text` 或非空 `text_digest`。
    """
    from ling.infra.security import new_id

    _bind_tts(monkeypatch, FakeTts())
    _give_voice(monkeypatch)
    connector = FakeConnector()

    window = await _make_window(logged_in, channel="onebot")
    # 前置条件：后台那一面已经有完整正文（`canonical_text`，剥掉 `[speak]`）。
    async with session_scope() as session:
        session.add(
            Message(
                id=new_id("msg"),
                window_id=window.id,
                role="actor",
                text="听到了吗",
                state="completed",
            )
        )
        await session.commit()

    monkeypatch.setattr("ling.worker._group_reply_connector", AsyncMock(return_value=connector))
    _patch_turn_coordinator(monkeypatch, f"{SPEAK} 听到了吗")
    async with session_scope() as session:
        delivered, reason = await worker_module._reply_in_group_window(
            session=session, config=await _config(session), window=window, result=_Result()
        )
    assert delivered is True, reason
    assert len(_audio(connector.sent[0])) == 1, "没有发出语音：下面的断言证明不了接线"

    async with session_scope() as session:
        actors = list(
            (await session.execute(select(Message).where(Message.role == "actor"))).scalars()
        )
    assert len(actors) == 1, f"期望一条 actor 消息，实际 {len(actors)}"
    assert actors[0].text == "听到了吗", (
        f"后台拿不到她说的完整正文：Message.text={actors[0].text!r}"
    )

    rows = await _deliveries()
    assert len(rows) == 1, f"期望一条投递 operation，实际 {len(rows)}"
    payload = dict(rows[0].payload or {})
    assert "text" not in payload, f"正文被写进了 operations.payload：{payload['text']!r}"
    # 空正文**不落摘要**（§17.5：「语音对账使用平台 message_id 或真实媒体证据，
    # 禁止空 text_digest 匹配所有语音」）。
    #
    # 这条断言原来写的是 `in (None, EMPTY_TEXT_DIGEST)`，并把空摘要标成"本轮未修的
    # 已知缺陷"——也就是说，它当时**把缺陷同时当成了允许值和豁免理由**。而
    # `connectors/onebot/connector.py::_scan_history` 用摘要逐条比对历史，空摘要
    # 会命中任何一条正文为空的消息，把别人的 message_id 认成自己的。
    #
    # 现在 `EMPTY_TEXT_DIGEST` 只作为**反例常量**留着：它出现在 payload 里就是缺陷。
    assert payload.get("text_digest") is None, (
        f"空正文落了摘要（它匹配所有语音）：{payload.get('text_digest')!r}"
    )
    assert "text_digest" not in payload, (
        f"空正文时连键都不该出现（存量旧行与新语音行同一种表示）：{payload.get('text_digest')!r}"
    )
    # 正向配对：这条 operation 确实是那条语音的。否则"没落正文"在"根本没建
    # operation"时也成立。
    assert payload["speech"]["digest"], payload


async def test_显式text降级才补文字(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """合成失败 + **显式** `fallback=SpeechFallback.text` → 才补发正文。

    §17.5 说的是"不**偷偷**默认开启"，不是"永远不许"。所以默认改为不发之后，
    这个选项必须仍然可用、仍然真的会发——否则修 bug 修成了删功能。

    **必须失败于**「把 `text` 选项一起删掉/无视」：那时 `connector.sent` 为空。
    与 `test_synthesis_failure_sends_nothing_by_default` **配对**：同一份缺陷
    （降级分支恒不发）会让那一条绿、这一条红；反之（恒发）则相反。

    形态走的是 `deliver_speech` 的**真实入口**（真实 `DeliveryService`、真实
    数据库、真实 `Message` 落库），在这里显式传 `fallback=`：`resolve_fallback`
    目前没有任何配置读点，所以"能不能选到"只能在这一层验。
    """
    from ling.capabilities.delivery import DeliveryService
    from ling.capabilities.speech_delivery import SpeechDelivery, SpeechFallback
    from ling.config import get_settings
    from ling.domains.media.assets import GeneratedMediaService
    from ling.domains.speech.orchestration import SynthesisOutcome
    from ling.infra.security import load_signing_root_key
    from ling.ports.speech import ExpressionIntent

    settings = get_settings()

    async def _deliver_with(fallback):  # noqa: ANN001,ANN202
        connector = FakeConnector()
        window = await _make_window(logged_in, channel="onebot")
        async with session_scope() as session:
            media = GeneratedMediaService(
                session,
                data_dir=settings.data_dir,
                root_key=load_signing_root_key(settings),
                actor_id=ACTOR,
            )
            service = SpeechDelivery(
                session,
                delivery=DeliveryService(session),
                media=media,
                connector=connector,
            )
            # 两个场景各用一个**新窗口**：`silent` 那次不建 operation，但
            # `text` 那次会；共用一个窗口容易踩 `plan_id`/幂等键的边。
            intent = ExpressionIntent(
                output_modality="voice", source="model", config_revision=0
            )
            result = await service.deliver_speech(
                request=SpeechDeliveryRequest(
                    turn_id=f"turn_fallback_{fallback.value}",
                    message_id=f"attn:fallback:{fallback.value}",
                    window_ref=str(window.id),
                    text="合成会失败，但话得送到。",
                    is_group=True,
                ),
                outcome=SynthesisOutcome(
                    artifact=None,
                    intent=intent,
                    failed=True,
                    detail="上游 503",
                ),
                intent=intent,
                actor_id=ACTOR,
                subject_id=logged_in.subject_id,
                fallback=fallback,
                transport="asset",
            )
            await session.commit()
        return result, connector

    text_result, text_connector = await _deliver_with(SpeechFallback.text)
    assert text_result.state is SpeechDeliveryState.text_only
    assert len(text_connector.sent) == 1, f"显式选了 text 却没发：{text_connector.sent}"
    assert text_connector.sent[0].text == "合成会失败，但话得送到。", (
        text_connector.sent[0].text
    )
    assert not _audio(text_connector.sent[0]), "降级时不该带音频"

    silent_result, silent_connector = await _deliver_with(SpeechFallback.silent)
    assert silent_result.state is SpeechDeliveryState.text_only, "静默要如实记成降级，不是失败"
    assert not silent_connector.sent, f"显式选了 silent 却发了：{[m.text for m in silent_connector.sent]}"


# --------------------------------------------------------------------------- #
# 七、默认降级的**接线**：`DEFAULT_FALLBACK` 必须真的到达 `deliver_speech`
#
# 上面那条 `test_显式text降级才补文字` 证明的是"显式传参有效"；它**证明不了**
# 默认值是什么——它自己就把 `fallback=` 传了进去。于是"`DEFAULT_FALLBACK` 退回
# `text`"这个缺陷在它那里是不可见的（实测：M2 变异下它仍然绿）。
#
# 这一节补的就是那个缺口，两半必须同时成立：
#   1. 常量本身是 `silent`（§17.5）；
#   2. `worker._deliver_speech_reply` 交给 `deliver_speech` 的**就是**它。
# 只有 1 没有 2：常量写对了但没人用；只有 2 没有 1：接线对、默认错。
# --------------------------------------------------------------------------- #


def test_默认降级是不补文字() -> None:
    """`DEFAULT_FALLBACK` 是 `silent`——§17.5「失败默认不把原话改发文字」。

    **必须失败于**「默认退回 `text`」（M2 变异）：那是老实现的方向。
    `text` / `notice` 仍然**可以解析**到，只是不再是默认——同一条断言的正反两面。
    """
    from ling.capabilities.speech_delivery import DEFAULT_FALLBACK, SpeechFallback

    assert DEFAULT_FALLBACK is SpeechFallback.silent
    # 反方向配对：不是"把三个都变成 silent"，`text` 仍然可选。
    assert SpeechFallback("text") is SpeechFallback.text
    assert SpeechFallback("notice") is SpeechFallback.notice


async def test_生产路径传给投递的降级就是默认值(
    logged_in, monkeypatch: pytest.MonkeyPatch  # noqa: ANN001
) -> None:
    """`_deliver_speech_reply` 传给 `deliver_speech` 的 `fallback` == `DEFAULT_FALLBACK`。

    看的是**生产调用点**真的交出去的那个值，而不是常量自己长什么样——两者可以
    分别正确、组合错误（例如接线处写死 `SpeechFallback.text`）。

    **必须失败于**「接线处写死 `text`」或「常量退回 `text`」：两者都会让实际
    传出去的值不是 `silent`。
    """
    from ling.capabilities.speech_delivery import DEFAULT_FALLBACK, SpeechDelivery

    _bind_tts(monkeypatch, FakeTts())
    _give_voice(monkeypatch)
    connector = FakeConnector()
    seen: list[object] = []
    real = SpeechDelivery.deliver_speech

    async def spy(self, **kwargs):  # noqa: ANN001,ANN003,ANN202
        seen.append(kwargs.get("fallback"))
        return await real(self, **kwargs)

    monkeypatch.setattr(SpeechDelivery, "deliver_speech", spy)
    monkeypatch.setattr("ling.worker._group_reply_connector", AsyncMock(return_value=connector))
    _patch_turn_coordinator(monkeypatch, f"{SPEAK} 默认该是什么")

    window = await _make_window(logged_in, channel="onebot")
    async with session_scope() as session:
        delivered, reason = await worker_module._reply_in_group_window(
            session=session, config=await _config(session), window=window, result=_Result()
        )

    assert delivered is True, reason
    assert len(seen) == 1, f"deliver_speech 被调用 {len(seen)} 次（期望 1）"
    assert seen[0] is DEFAULT_FALLBACK, (
        f"生产路径交给投递的 fallback 是 {seen[0]!r}，不是 DEFAULT_FALLBACK"
        f"（{DEFAULT_FALLBACK!r}）——接线处自己写死了值？"
    )
