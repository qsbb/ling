"""语音输出编排与幂等（真实 PostgreSQL + 真实磁盘）。

## 这个文件在守什么

语音这条链路上有三种**花钱且不可逆**的动作：

1. **合成**——每调一次 TTS 就是一次计费；
2. **外发**——每发一次用户就收到一条消息；
3. **重试**——前两者都会在"看起来失败了"的时候被重试。

三者里最贵的是**重试**：它不是一次新的花费，而是**把已经花掉的钱再花一遍**。
所以本文件的核心不是"功能能用"，而是「**同一个意图只发生一次**」。

## 为什么每个用例都数调用次数

`FakeTts.calls` 是一个计数器。断言 `calls == 1` 与断言 `result == xxx` 的区别在于：
后者在"合成了两次、返回第二次的结果"时**仍然通过**，而前者会红。重复计费
恰恰就是这个形态——结果完全正确，只是钱花了两遍。

同理，投递侧断言 `send_calls == 1`：重复外发的表现是"用户收到两条一样的消息"，
而单看返回值分辨不出来。

## 为什么用真实数据库

幂等靠的是 `idempotency_keys` 的 `UNIQUE(scope, key)` 约束 + SAVEPOINT。
这是**数据库**的行为，不是我们的 Python 逻辑：mock 掉会话之后，
"唯一约束会炸"这件事变成我想象的。所以这里每条路径都真的 flush 到 PostgreSQL。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from ling.capabilities.speech_delivery import (
    SpeechDelivery,
    SpeechDeliveryRequest,
    SpeechDeliveryState,
    SpeechFallback,
    resolve_fallback,
)
from ling.contracts.v1 import (
    AttachmentKind,
    CapabilityDescriptor,
    DeliveryReceipt,
    DeliveryState,
    Modality,
)
from ling.domains.media.assets import GeneratedMediaService
from ling.domains.speech.orchestration import (
    SYNTHESIS_SCOPE,
    SpeechOrchestrator,
    SynthesisRequest,
    SynthesisState,
    decode_state,
    encode_state,
    to_speech_error,
)
from ling.infra.db import session_scope
from ling.infra.media_assets import MediaAssetRow
from ling.infra.models import IdempotencyKey
from ling.ports.channel import ChannelError
from ling.ports.speech import (
    AudioSpec,
    ExpressionIntent,
    MiniMaxVoice,
    SpeechError,
    SpeechErrorKind,
    SpeechUsage,
    TtsChunk,
    TtsEnd,
    TtsStart,
)

pytestmark = pytest.mark.w0_web

ROOT_KEY = b"k" * 32
ACTOR = "actor_ling"
WINDOW = "onebot:private:100200300"
BASE_URL = "http://127.0.0.1:8080"

#: 一段"像音频"的字节。测试不关心它能不能播，只关心它被原样存取。
AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 8


# --------------------------------------------------------------------------- #
# 假 provider / 假渠道：都不碰网络，但**都记账**
# --------------------------------------------------------------------------- #


class FakeTts:
    """按端口签名（`synthesize(TtsRequest, *, call=)`）实现的假 TTS。

    `calls` 是花钱次数。`fail_with` 用来构造失败路径。
    """

    provider_id = "fake_tts"

    def __init__(self, *, audio: bytes = AUDIO, fail_with: Exception | None = None) -> None:
        self.calls = 0
        self.audio = audio
        self.fail_with = fail_with
        self.last_text: str | None = None

    async def synthesize(self, request, *, call):  # noqa: ANN001,ANN201
        self.calls += 1
        self.last_text = request.text
        if self.fail_with is not None:
            raise self.fail_with
        spec = AudioSpec(
            mime_type="audio/mpeg",
            container="mp3",
            codec="mp3",
            sample_rate_hz=24000,
            channels=1,
        )
        yield TtsStart(spec=spec)
        yield TtsChunk(sequence=0, audio=self.audio)
        yield TtsEnd(
            spec=spec,
            provider_request_id="req_fake_1",
            usage=SpeechUsage(audio_seconds=1.5, usage_source="provider"),
        )

    def capabilities(self, model_id: str):  # noqa: ANN201
        from ling.ports.speech import TtsCapabilities

        return TtsCapabilities(
            formats=frozenset({"mp3", "wav"}), streaming="buffered"
        )

    async def cancel(self, *, attempt_id: str) -> None:
        self.cancelled = getattr(self, "cancelled", []) + [attempt_id]

    async def aclose(self) -> None:
        self.closed = True


class AdapterStyleTts:
    """**适配器形态**的假 TTS（`synthesize(text, *, voice_id=)` → 一次性结果）。

    存在的意义：证明编排层真的能驱动 `models/` 里那三家（它们的签名**不是**
    端口签名）。用端口形态的假 provider 测不到这条翻译路径。
    """

    provider_id = "fake_adapter"

    def __init__(self, *, audio: bytes = AUDIO) -> None:
        self.calls = 0
        self.audio = audio
        self.seen_voice: str | None = None

    async def synthesize(self, text: str, *, voice_id: str = ""):  # noqa: ANN201
        self.calls += 1
        self.seen_voice = voice_id

        class _Result:
            audio = self.audio
            container = "mp3"
            duration_ms = 900
            sample_rate = 24000
            provider_request_id = "req_adapter"

        return _Result()

    def capabilities(self, model_id: str):  # noqa: ANN201
        from ling.ports.speech import TtsCapabilities

        return TtsCapabilities(formats=frozenset({"mp3"}), streaming="buffered")

    async def cancel(self, *, attempt_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


class TruncatedTts:
    """只发 chunk、**不发 TtsEnd** 的假 provider（模拟流中途断掉）。"""

    provider_id = "fake_truncated"
    calls = 0

    async def synthesize(self, request, *, call):  # noqa: ANN001,ANN201
        TruncatedTts.calls += 1
        spec = AudioSpec(
            mime_type="audio/mpeg", container="mp3", codec="mp3",
            sample_rate_hz=24000, channels=1,
        )
        yield TtsStart(spec=spec)
        yield TtsChunk(sequence=0, audio=AUDIO)

    def capabilities(self, model_id: str):  # noqa: ANN201
        from ling.ports.speech import TtsCapabilities

        return TtsCapabilities(formats=frozenset({"mp3"}), streaming="buffered")

    async def cancel(self, *, attempt_id: str) -> None:
        return None

    async def aclose(self) -> None:
        return None


class FakeConnector:
    """假渠道。`send_calls` 是外发次数。"""

    connector_id = "fake"
    instance_id = "fake-1"

    def __init__(self, *, state: DeliveryState = DeliveryState.delivered,
                 error: ChannelError | None = None) -> None:
        self.send_calls = 0
        self.sent: list[object] = []
        self._state = state
        self._error = error

    def capabilities(self) -> CapabilityDescriptor:
        return CapabilityDescriptor(
            capability_id="fake",
            version="1.0",
            enabled=True,
            supports_private=True,
            supports_group=True,
            supports_proactive=True,
            # **必须显式列 audio**：只写 `supports_media=True` 会被
            # `media_supported()` 解读成"只能发图片"，语音请求在发送前就被
            # 拒成 unsupported——而那个失败看起来像"渠道不支持"，很容易误判。
            supported_media=[Modality.audio, Modality.image],
            max_text_length=2000,
        )

    async def send(self, message):  # noqa: ANN001,ANN201
        self.send_calls += 1
        self.sent.append(message)
        if self._error is not None:
            raise self._error
        return DeliveryReceipt(
            receipt_id=f"rcpt_{self.send_calls}",
            # 真实投递层会用 `replace(message, operation_id=operation.id)`
            # 把 operation id 替进去；这里没有那一层，所以自己编一个。
            # 关键是**不能用 message.operation_id**（那是空串，契约要求非空）。
            operation_id=f"op_fake_{self.send_calls}",
            channel=self.connector_id,
            state=self._state,
            platform_message_id=f"pm_{self.send_calls}",
            observed_at=datetime.now(UTC),
            detail="",
        )

    async def query_receipt(self, query):  # noqa: ANN001,ANN201
        raise NotImplementedError


class FakeDelivery:
    """假投递服务：只数调用、不碰数据库。

    幂等本身由 `DeliveryService` 保证，那部分已有既有测试覆盖；本文件要证明的
    是**编排层把正确的键传下去了**、以及"uncertain 不再发第二条"。
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def deliver(self, **kwargs):  # noqa: ANN003,ANN201
        self.calls.append(kwargs)
        from ling.capabilities.delivery import DeliveryOutcome
        from ling.contracts.v1 import OperationState

        connector = kwargs["connector"]
        await connector.send(kwargs["message"])
        return DeliveryOutcome(
            operation_id="op_fake_1",
            state=OperationState.succeeded,
            receipt=None,
            detail="已送达（假投递）",
        )


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def voice_intent_for_test() -> ExpressionIntent:
    """她会写 `[speak]` 时得到的意图。"""
    return ExpressionIntent(output_modality="voice", source="model", config_revision=1)


def text_intent() -> ExpressionIntent:
    """她没写 `[speak]`（来源是 default，不是她主动选文字）。"""
    return ExpressionIntent(output_modality="text", source="default", config_revision=1)


async def _orchestrate(tmp_path, tts, request: SynthesisRequest):  # noqa: ANN001,ANN202
    """在一段 session_scope 里跑一次合成，返回 (outcome, 会话外可见的键状态)。"""
    async with session_scope() as session:
        media = GeneratedMediaService(
            session, data_dir=tmp_path, root_key=ROOT_KEY, base_url=BASE_URL
        )
        orchestrator = SpeechOrchestrator(session, tts=tts, media=media)
        outcome = await orchestrator.synthesize(request)
        await session.commit()
        key = await session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.scope == SYNTHESIS_SCOPE,
                IdempotencyKey.key == f"{request.turn_id}:{request.message_id}",
            )
        )
        row = key.scalar_one_or_none()
        return outcome, (row.result_ref if row is not None else None)


def make_request(**overrides) -> SynthesisRequest:  # noqa: ANN003
    payload = {
        "turn_id": f"turn_{uuid.uuid4().hex[:10]}",
        "message_id": f"msg_{uuid.uuid4().hex[:10]}",
        "text": "我今天有点累，但看到你消息还是挺高兴的。",
        "window_id": WINDOW,
        "actor_id": ACTOR,
        "subject_id": "subject_ling",
        "voice": MiniMaxVoice(voice_id="voice_1"),
        "model_id": "speech-2.8-hd",
        "intent": voice_intent_for_test(),
        "producer": "tts.fake",
    }
    payload.update(overrides)
    return SynthesisRequest(**payload)


# --------------------------------------------------------------------------- #
# 一、幂等：同一条消息不重复合成（不重复花钱）
# --------------------------------------------------------------------------- #


async def test_同一条消息合成两次只花钱一次(web_env, tmp_path) -> None:
    """核心用例：重复触发同一段语音，provider 只能被调一次。

    这是**重复计费**的直接反证。若这条变红，说明幂等闸没拦住——
    在真实部署里表现是"同一句话被收两次费"。
    """
    tts = FakeTts()
    request = make_request()

    first, ref1 = await _orchestrate(tmp_path, tts, request)
    second, ref2 = await _orchestrate(tmp_path, tts, request)

    assert tts.calls == 1, f"同一条消息合成了 {tts.calls} 次，重复计费"
    assert first.ready and second.ready
    assert first.artifact is not None and second.artifact is not None
    assert first.artifact.digest == second.artifact.digest, "第二次必须复用同一个产物"
    # `reused` 的语义是"这次**没有**真的调 provider"。第一次调了 → False；
    # 第二次命中合成幂等、一分钱没花 → True。
    assert second.artifact.reused is True, "第二次必须标成复用（没花钱）"
    assert ref1 == ref2, "幂等键的记录不能被第二次覆盖掉"

    # 库里只有一行资产、一个键：没有重复产物
    async with session_scope() as session:
        assets = (await session.execute(select(func.count()).select_from(MediaAssetRow))).scalar()
        keys = (
            await session.execute(
                select(func.count())
                .select_from(IdempotencyKey)
                .where(IdempotencyKey.scope == SYNTHESIS_SCOPE)
            )
        ).scalar()
    assert assets == 1, f"生成了 {assets} 份资产，应该是 1 份"
    assert keys == 1, f"幂等键有 {keys} 行，应该是 1 行"


async def test_不同消息各自合成互不干扰(web_env, tmp_path) -> None:
    """同一轮里的两段语音是两次合成——幂等键含 message_id，不能互相顶掉。

    反向的坑：若键只用 `turn_id`，第二段会以为"这一轮已经合成过了"
    然后复用第一段的音频——用户听到两遍第一句话。
    """
    tts = FakeTts()
    turn = f"turn_{uuid.uuid4().hex[:10]}"
    first, _ = await _orchestrate(
        tmp_path, tts, make_request(turn_id=turn, message_id="msg_a", text="第一段")
    )
    second, _ = await _orchestrate(
        tmp_path, tts, make_request(turn_id=turn, message_id="msg_b", text="第二段")
    )

    assert tts.calls == 2, "两段不同的语音必须各合成一次"
    assert first.artifact is not None and second.artifact is not None
    # 两次都真的调了 provider → 两次都标 True（这个字段说的是"有没有花钱"，
    # 不是"存储层有没有新建文件"）。
    assert second.artifact.reused is True


async def test_合成成功后产物真的落在磁盘上(web_env, tmp_path) -> None:
    """幂等返回的 digest 必须指向一个**真实可读**的文件，不是一条悬空记录。"""
    tts = FakeTts()
    outcome, ref = await _orchestrate(tmp_path, tts, make_request())
    assert outcome.artifact is not None

    from ling.domains.media.storage import GeneratedMediaStorage

    storage = GeneratedMediaStorage(tmp_path)
    digest = outcome.artifact.digest
    assert digest == hashlib.sha256(AUDIO).hexdigest(), "digest 必须就是内容的 sha256"
    assert storage.exists(digest)
    assert storage.read(digest) == AUDIO, "存进去的字节必须原样"
    assert ref is not None and digest in ref


# --------------------------------------------------------------------------- #
# 二、失败分类：可重来 vs 绝不再合成
# --------------------------------------------------------------------------- #


async def test_合成失败不重复计费的类型允许重来(web_env, tmp_path) -> None:
    """`invalid_request`（400/参数错）在上游执行前就被拒，**没花钱**，可以重来。"""
    tts = FakeTts(
        fail_with=SpeechError(SpeechErrorKind.unsupported, "文本过长", retriable=False)
    )
    request = make_request()
    first, ref1 = await _orchestrate(tmp_path, tts, request)

    assert first.failed is True
    assert first.billed_uncertain is False, "请求校验失败不该被记成可能计费"
    assert first.artifact is None
    state, _ = decode_state(ref1)
    assert state is SynthesisState.failed, f"应为 failed，实际 {state}"

    # 允许重来：把 provider 修好，同一对键可以再合成一次
    tts.fail_with = None
    second, _ref2 = await _orchestrate(tmp_path, tts, request)
    assert tts.calls == 2, "failed 状态必须允许重新合成"
    assert second.ready is True


async def test_超时绝不重新合成避免重复计费(web_env, tmp_path) -> None:
    """**本任务最重要的一条**：超时 = 上游可能已经合成并计费。

    再合成一遍就是花第二遍钱。所以第二次调用必须**直接返回**，
    provider 的调用次数不能增加。
    """
    tts = FakeTts(
        fail_with=SpeechError(SpeechErrorKind.timeout, "上游超时", retriable=True)
    )
    request = make_request()
    first, ref1 = await _orchestrate(tmp_path, tts, request)

    assert first.failed is True
    assert first.billed_uncertain is True, "超时必须被标成可能已计费"
    state, _ = decode_state(ref1)
    assert state is SynthesisState.uncertain

    # 第二次：即使 provider 已经"恢复正常"，也不许再合成
    tts.fail_with = None
    second, _ref2 = await _orchestrate(tmp_path, tts, request)

    assert tts.calls == 1, "uncertain 状态下又合成了——这就是重复计费"
    assert second.failed is True
    assert second.billed_uncertain is True
    assert second.artifact is None, "绝不给出一段可能重复计费的音频"
    assert "不确定" in second.detail


async def test_限流与协议错误同样不再重试(web_env, tmp_path) -> None:
    """限流、上游 200 但内容不符、5xx 都属于"上游可能已经处理过"。"""
    for kind in (
        SpeechErrorKind.rate_limited,
        SpeechErrorKind.protocol,
        SpeechErrorKind.unavailable,
    ):
        tts = FakeTts(fail_with=SpeechError(kind, f"失败-{kind}", retriable=False))
        request = make_request()
        await _orchestrate(tmp_path, tts, request)
        tts.fail_with = None
        second, _ = await _orchestrate(tmp_path, tts, request)
        assert tts.calls == 1, f"{kind} 之后又合成了一次"
        assert second.billed_uncertain is True, f"{kind} 应被记成可能已计费"


async def test_没有TtsEnd的流不发布不完整音频(web_env, tmp_path) -> None:
    """只有 chunk、没有 End = 流断了。**不能**把半段音频存下来发出去。

    端口的硬规则：没有 `End` 就不发布完整产物。存半段会让用户听到
    半句话然后突然断掉，而本地记录说已送达。
    """
    TruncatedTts.calls = 0
    outcome, ref = await _orchestrate(tmp_path, TruncatedTts(), make_request())

    assert outcome.failed is True
    assert outcome.artifact is None
    assert "TtsEnd" in outcome.detail
    async with session_scope() as session:
        assets = (await session.execute(select(func.count()).select_from(MediaAssetRow))).scalar()
    assert assets == 0, "流断了不能留下任何资产"
    state, _ = decode_state(ref)
    # 协议不符 → 上游已经调用过一次 → uncertain（可能计费），不再重试
    assert state is SynthesisState.uncertain


async def test_空正文不值得合成(web_env, tmp_path) -> None:
    """空正文连 provider 都不该碰——空音频发出去是一条没有声音的语音。"""
    tts = FakeTts()
    outcome, ref = await _orchestrate(tmp_path, tts, make_request(text="   "))

    assert tts.calls == 0, "空正文不该调用 provider"
    assert outcome.failed is True
    assert ref is None, "压根没开始，不该留下幂等键"


# --------------------------------------------------------------------------- #
# 三、适配器形态：真正驱动 models/ 里那三家
# --------------------------------------------------------------------------- #


async def test_适配器签名也能被编排层驱动(web_env, tmp_path) -> None:
    """`models/` 里三家是 `synthesize(text, *, voice_id=)`，**不是**端口签名。

    这条证明编排层真的能接上它们——只测端口形态的假 provider 测不到这条翻译路径，
    而那正是"接上了但跑不通"最可能发生的地方。
    """
    tts = AdapterStyleTts()
    outcome, _ref = await _orchestrate(tmp_path, tts, make_request())

    assert tts.calls == 1
    assert tts.seen_voice == "voice_1", "音色 ID 必须被翻译成适配器的关键字"
    assert outcome.ready is True
    assert outcome.artifact is not None
    assert outcome.artifact.mime_type == "audio/mpeg"
    # 适配器返回 `duration_ms=900`，端口把时长装在 `usage.audio_seconds`。
    # 编排层必须把它搬过去（丢掉的话 media_assets.duration_ms 永远为空）。
    assert outcome.artifact.duration_ms == 900, "时长必须来自适配器报的值"
    assert outcome.artifact.sample_rate == 24000
    assert outcome.artifact.provider_request_id == "req_adapter"


async def test_适配器错误被翻译成端口错误类型(web_env, tmp_path) -> None:
    """Codex 裁决要求统一用 `SpeechError`；适配器抛的 `TtsError` 必须在这里转。

    新端口把前七类做成与 `TtsErrorKind` **取值逐字一致**，所以映射是
    **同名直通**、不是有损转换。`authentication`（401）必须保持是
    `authentication` 而不是被归进 `unavailable`——后者会让前端说
    "稍后重试"，而 401 重试一万次还是 401。
    """
    from ling.models.tts_common import TtsError, TtsErrorKind

    tts = FakeTts(fail_with=TtsError(TtsErrorKind.authentication, "密钥无效（401）"))
    outcome, _ref = await _orchestrate(tmp_path, tts, make_request())

    assert outcome.failed is True
    assert "401" in outcome.detail
    # 401 在请求校验阶段被拒，没花钱 → 允许重来
    assert outcome.billed_uncertain is False

    converted = to_speech_error(TtsError(TtsErrorKind.authentication, "x"))
    assert converted.kind is SpeechErrorKind.authentication
    assert to_speech_error(TtsError(TtsErrorKind.timeout, "x")).kind is SpeechErrorKind.timeout
    # 端口没有的 adapter kind → `unsupported`（"做不到"），不是 `unavailable`
    # （"暂时不行，等等再试"）——后者会诱导上层无限重试。
    assert (
        to_speech_error(TtsError("我们没见过的kind", "x")).kind
        is SpeechErrorKind.unsupported
    )
    # 已经是端口类型就原样返回（幂等，不丢 kind）
    already = SpeechError(SpeechErrorKind.rate_limited, "y", retriable=True)
    assert to_speech_error(already) is already


# --------------------------------------------------------------------------- #
# 四、投递：一条消息两个面 + uncertain 不重发
# --------------------------------------------------------------------------- #


async def _run_delivery(
    tmp_path,
    outcome,  # noqa: ANN001
    *,
    intent=None,  # noqa: ANN001
    text: str = "我今天有点累，但看到你消息还是挺高兴的。",
    fallback: SpeechFallback = SpeechFallback.text,
    connector: FakeConnector | None = None,
):  # noqa: ANN202
    """跑一次投递，返回 (结果, 假渠道, 假投递服务)。"""
    connector = connector or FakeConnector()
    delivery = FakeDelivery()
    async with session_scope() as session:
        media = GeneratedMediaService(
            session, data_dir=tmp_path, root_key=ROOT_KEY, base_url=BASE_URL
        )
        service = SpeechDelivery(
            session, delivery=delivery, media=media, connector=connector
        )
        result = await service.deliver_speech(
            request=SpeechDeliveryRequest(
                turn_id="turn_x", message_id="msg_x", window_ref=WINDOW, text=text
            ),
            outcome=outcome,
            intent=intent or voice_intent_for_test(),
            actor_id=ACTOR,
            subject_id="subject_ling",
            fallback=fallback,
            transport="asset",
        )
        await session.commit()
    return result, connector, delivery


async def test_语音投递只发一条且同时带正文与音频(web_env, tmp_path) -> None:
    """用户原话的核心：「只发语音（渠道），后台显示语音标签 + 文字」。

    一条消息，两个面：`text` 给后台渲染，`attachments=[audio]` 是渠道上
    真的发出去的东西。**不是文字发两遍。**
    """
    tts = FakeTts()
    outcome, _ = await _orchestrate(tmp_path, tts, make_request())
    result, connector, delivery = await _run_delivery(tmp_path, outcome)

    assert result.state is SpeechDeliveryState.delivered
    assert result.delivered is True
    assert connector.send_calls == 1, f"发了 {connector.send_calls} 条，应该只有 1 条"

    sent = connector.sent[0]
    assert sent.text.strip(), "正文不能为空：后台要靠它显示「语音标签 + 文字」"
    assert len(sent.attachments) == 1
    attachment = sent.attachments[0]
    assert attachment.kind is AttachmentKind.audio, "语音必须用类型化 audio 附件"
    assert attachment.ref.startswith("asset:"), attachment.ref
    assert result.digest is not None
    assert attachment.ref == f"asset:{result.digest}", "附件引用必须指向这次合成的产物"
    # legacy 字段必须为空：走那条路语音会被当成图片发出去
    assert sent.media_refs == [], "不能同时塞 legacy media_refs（会被当图片）"

    # **最强的一条**：用渠道能力判定的唯一入口再走一遍，确认它被当成 audio。
    # 前几条断言只检查我们**自己构造**的对象长什么样；这一条检查**投递层
    # 实际会怎么理解它**。两者不是一回事——把语音塞进 legacy `media_refs`
    # 时前面的断言能过（那个列表里确实有东西），而渠道会把它发成一张图。
    from ling.ports.channel import iter_outbound_attachments

    resolved = iter_outbound_attachments(sent)
    assert len(resolved) == 1, f"渠道会看到 {len(resolved)} 个附件，应该 1 个"
    assert resolved[0].kind is AttachmentKind.audio, "渠道会把它当图片发出去"
    caps = connector.capabilities()
    assert caps.media_supported(resolved[0].modality) is True, "渠道必须认这个模态"

    # 幂等键传对了：plan_id 由 turn 派生（不是 uuid），段序号 0
    assert delivery.calls[0]["plan_id"] == "plan_turn_x"
    assert delivery.calls[0]["part_ordinal"] == 0


async def test_合成失败默认静默降级为纯文字(web_env, tmp_path) -> None:
    """语音没合成出来时，**内容仍然要送到**——语音是形态，不是内容。"""
    tts = FakeTts(
        fail_with=SpeechError(SpeechErrorKind.unsupported, "文本过长", retriable=False)
    )
    outcome, _ = await _orchestrate(tmp_path, tts, make_request())
    result, connector, delivery = await _run_delivery(tmp_path, outcome)

    assert result.state is SpeechDeliveryState.text_only
    assert connector.send_calls == 1
    sent = connector.sent[0]
    assert sent.attachments == [], "降级时不该带任何音频附件"
    assert "我今天有点累" in sent.text, "正文必须原样送到"
    assert "没合成出来" not in sent.text, "默认 text 降级不该加说明"
    assert delivery.calls[0]["payload_extra"]["speech_fallback"] == "text"


async def test_notice降级会加一句说明(web_env, tmp_path) -> None:
    """`speech_fallback=notice` 时加一句可执行的说明，且**加在后面**。"""
    tts = FakeTts(
        fail_with=SpeechError(SpeechErrorKind.unsupported, "文本过长", retriable=False)
    )
    outcome, _ = await _orchestrate(tmp_path, tts, make_request())
    result, connector, _ = await _run_delivery(
        tmp_path, outcome, fallback=SpeechFallback.notice
    )

    assert result.state is SpeechDeliveryState.text_only
    sent = connector.sent[0]
    assert sent.text.index("我今天有点累") < sent.text.index("没合成出来"), \
        "说明必须在正文之后——否则每句降级的回复都以道歉开头"


async def test_silent降级什么都不发(web_env, tmp_path) -> None:
    """`silent` 是极端配置：连文字都不发，但状态要如实说是降级而非失败。"""
    tts = FakeTts(
        fail_with=SpeechError(SpeechErrorKind.unsupported, "文本过长", retriable=False)
    )
    outcome, _ = await _orchestrate(tmp_path, tts, make_request())
    result, connector, _ = await _run_delivery(
        tmp_path, outcome, fallback=SpeechFallback.silent
    )

    assert result.state is SpeechDeliveryState.text_only
    assert connector.send_calls == 0, "silent 不该发出任何东西"


async def test_不确定的投递不重发(web_env, tmp_path) -> None:
    """**投递 uncertain 绝不重发**：可能已经到用户手里了，再发就是两条。

    这里直接构造一个 `uncertain` 的投递结果，断言编排层**不会**再发第二条。
    """
    tts = FakeTts()
    outcome, _ = await _orchestrate(tmp_path, tts, make_request())

    connector = FakeConnector()
    delivery = FakeDelivery()

    async def deliver_uncertain(**kwargs):  # noqa: ANN003,ANN201
        delivery.calls.append(kwargs)
        from ling.capabilities.delivery import DeliveryOutcome
        from ling.contracts.v1 import OperationState

        await connector.send(kwargs["message"])
        return DeliveryOutcome(
            operation_id="op_uncertain",
            state=OperationState.uncertain,
            receipt=None,
            detail="投递结果不确定（平台没给回执）",
            skipped=True,
        )

    delivery.deliver = deliver_uncertain  # type: ignore[method-assign]

    async with session_scope() as session:
        media = GeneratedMediaService(
            session, data_dir=tmp_path, root_key=ROOT_KEY, base_url=BASE_URL
        )
        service = SpeechDelivery(session, delivery=delivery, media=media, connector=connector)
        result = await service.deliver_speech(
            request=SpeechDeliveryRequest(
                turn_id="turn_x", message_id="msg_x", window_ref=WINDOW, text="正文"
            ),
            outcome=outcome,
            intent=voice_intent_for_test(),
            actor_id=ACTOR,
            subject_id="subject_ling",
            transport="asset",
        )
        await session.commit()

    assert result.state is SpeechDeliveryState.partial
    assert result.delivered is False, "uncertain 不能被当成送达"
    assert connector.send_calls == 1, "uncertain 之后又发了一条——那是重复消息"
    assert "不确定" in result.detail


async def test_她决定不发语音时完全不投递(web_env, tmp_path) -> None:
    """决策 `speak=False` 时连合成都不该发生，投递也不该被调用。"""
    tts = FakeTts()
    outcome, _ = await _orchestrate(tmp_path, tts, make_request())
    result, connector, delivery = await _run_delivery(
        tmp_path, outcome, intent=text_intent()
    )

    assert result.state is SpeechDeliveryState.skipped
    assert connector.send_calls == 0
    assert delivery.calls == [], "不发语音时不该调用投递"


# --------------------------------------------------------------------------- #
# 五、状态编码：不加列，用 result_ref 前缀
# --------------------------------------------------------------------------- #


async def test_状态编码往返可逆且陌生值按不确定处理() -> None:
    """`idempotency_keys` 只有五列，状态编在 `result_ref` 里。

    陌生值（例如别的消费者留下的裸 operation id）必须解释成 `uncertain`
    ——"结果未知、不要再合成"永远比"当它没做过、重来一次"安全。
    """
    assert decode_state(encode_state(SynthesisState.in_progress)) == (
        SynthesisState.in_progress,
        "",
    )
    assert decode_state(encode_state(SynthesisState.succeeded, detail="a" * 64)) == (
        SynthesisState.succeeded,
        "a" * 64,
    )
    # 截断：result_ref 是 VARCHAR(128)
    encoded = encode_state(SynthesisState.uncertain, detail="x" * 500)
    assert len(encoded) <= 128, f"编码后 {len(encoded)} 字符，超过列宽 128"

    # 陌生/空值 → uncertain（保守）
    assert decode_state("op_abc123")[0] is SynthesisState.uncertain
    assert decode_state(None)[0] is SynthesisState.uncertain
    assert decode_state("")[0] is SynthesisState.uncertain


async def test_降级配置非法值退回文字而不是静默() -> None:
    """配置写错时用户不该**完全收不到回复**——退回 text，并记 warning。"""
    assert resolve_fallback(None) is SpeechFallback.text
    assert resolve_fallback("silent") is SpeechFallback.silent
    assert resolve_fallback("不存在的值") is SpeechFallback.text


# --------------------------------------------------------------------------- #
# 六、[speak] 解析：她自己在回复里选（§17.4）
# --------------------------------------------------------------------------- #


async def test_开头的speak标签被认成语音且从正文剥掉() -> None:
    """她写 `[speak]` → 语音。

    标签**必须从正文里剥掉**，否则 TTS 会把「左方括号 speak 右方括号」念出来。
    """
    from ling.capabilities.expression_choice import parse_speak_choice

    choice = parse_speak_choice("[speak] 我今天有点累。")
    assert choice.speak is True
    assert choice.explicit is True
    assert choice.text == "我今天有点累。", "标签必须剥掉，不能被念出来"
    assert choice.rejected == ""


async def test_没有speak标签就是文字且不算她主动选的() -> None:
    """什么都没写 = 文字，且 `explicit=False`。

    这个区分不是吹毛求疵：统计"她多久选一次语音"时，若把"没表达"
    算成"主动选了文字"，那个比例会被稀释到没有意义。
    """
    from ling.capabilities.expression_choice import parse_speak_choice

    choice = parse_speak_choice("我今天有点累。")
    assert choice.speak is False
    assert choice.explicit is False, "没写标签不等于她主动选了文字"
    assert choice.text == "我今天有点累。"


async def test_正文中间的speak标签不被采信(web_env) -> None:
    """**最重要的一条**：不从正文中间提取。

    不锚定的后果很具体：她解释语法时写「`[speak]` 表示要发语音」，
    会被当成她真的要发语音——一条解释文字变成一条语音。
    更糟的是用户发来的内容里带这四个字也能触发。
    """
    from ling.capabilities.expression_choice import parse_speak_choice

    # 注意：这里 `[speak]` **前面必须有非空白正文**，否则这条用例测不到锚定
    # ——只有空白时，一个没有 `^` 的正则也会匹配成功。我第一次就是这么写的，
    # 变异测试（去掉 `^`）仍然是绿的，才发现用例是空的。
    choice = parse_speak_choice("他说「[speak] 就是发语音的意思」。")
    assert choice.speak is False, "正文中间的标签不该被采信"
    assert choice.text == "他说「[speak] 就是发语音的意思」。", "正文不能被改动"
    assert "不在起始控制区" in choice.rejected, "要被如实报告，便于发现模型用错"


async def test_只写speak没有正文时无有效外发() -> None:
    """只写 `[speak]` 没有正文 → **明确无有效外发**。

    这里与 `parse_expression` 的兜底方向**故意相反**：那里剥空要还原原文
    （否则用户收到空消息），这里剥空说明她只写了个标签——
    发一条没有内容的语音毫无意义，而还原成 `[speak]` 会把说明书念给用户听。
    """
    from ling.capabilities.expression_choice import parse_speak_choice

    choice = parse_speak_choice("[speak]   ")
    assert choice.speak is False
    assert choice.explicit is True
    assert choice.text == "", "不能还原成 [speak]"
    assert "没有正文" in choice.rejected


async def test_重复的speak标签按非法处理() -> None:
    """写了两个 → 不猜哪个算，按非法处理。"""
    from ling.capabilities.expression_choice import parse_speak_choice

    choice = parse_speak_choice("[speak] [speak] 正文")
    assert choice.speak is False
    assert "重复" in choice.rejected


async def test_正文后面跟的speak标签不被采信() -> None:
    """**真正能验证 `^` 锚定的一条**：非空白正文之后紧跟 `[speak]`。

    没有 `^` 的正则会把这条判成"要发语音"，于是她一段解释文字变成一条语音。
    上一个用例（`他说「…」`）里 `[speak]` 前面是非空白字符，去掉 `^` 也匹配不上，
    所以它验证的是"标签在散文里"，不是"锚定"。
    """
    from ling.capabilities.expression_choice import parse_speak_choice

    choice = parse_speak_choice("我说完了[speak]")
    assert choice.speak is False, "标签在正文之后，不是起始控制区"
    assert choice.rejected, "要被如实报告"
    # 反过来：真的在开头时仍然认（防止把功能改没了）
    assert parse_speak_choice("  [speak] 我说完了").speak is True


async def test_标签前有空白和换行仍然认() -> None:
    """模型常在标记前留空行。允许前导空白，但只认一个标签。"""
    from ling.capabilities.expression_choice import parse_speak_choice

    choice = parse_speak_choice("\n\n  [speak]\n  正文在这里")
    assert choice.speak is True
    assert choice.text == "正文在这里"


async def test_解析出语音意图后编排层能真的合成(web_env, tmp_path) -> None:
    """把解析结果接到编排层：`[speak]` → 合成 → 有产物。端到端串一次。"""
    from ling.capabilities.expression_choice import parse_speak_choice

    choice = parse_speak_choice("[speak] 我今天有点累。")
    intent = ExpressionIntent(
        output_modality="voice" if choice.speak else "text",
        source="model" if choice.explicit else "default",
        config_revision=1,
    )
    tts = FakeTts()
    outcome, _ = await _orchestrate(
        tmp_path, tts, make_request(text=choice.text, intent=intent)
    )
    assert tts.calls == 1
    assert outcome.ready is True
    assert tts.last_text == "我今天有点累。", "念出来的必须是剥掉标签后的正文"


async def test_文字意图进来时编排层拒绝合成(web_env, tmp_path) -> None:
    """上游传了文字意图进来 = 判断有 bug。**不合成**——为一个她没选的形态花钱更糟。"""
    tts = FakeTts()
    outcome, ref = await _orchestrate(
        tmp_path, tts, make_request(intent=text_intent())
    )
    assert tts.calls == 0, "文字意图不该触发合成"
    assert outcome.skipped is True
    assert outcome.failed is False, "跳过不是失败"
    assert ref is None, "没开始就不该留幂等键"
