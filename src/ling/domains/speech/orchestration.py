"""语音输出编排：从「她决定说给你听」到「音频已经落在存储里、可以投递」。

这个模块**不投递**。投递是 `capabilities/speech_delivery.py` 的事，分开是刻意的：
合成失败与投递失败的正确处置完全相反——前者可以换个供应商重试，后者一旦
`uncertain` 就**绝不能重发**。把两者写在同一个函数里，迟早会出现"投递超时了，
那再合成一遍吧"这种重复计费 + 重复消息。

## 一次合成的一生

```
   SpeechDecision(speak=True)
        │
        │  synthesize()  ← 幂等闸在这里
        ▼
   TtsOutcome.audio  ── save_bytes() ──> media_assets 一行
        │                                  (turn_id / message_id / producer 都写上)
        ▼
   SpeechArtifact(digest, mime, duration_ms, …)
```

## 幂等：为什么键是 `(turn_id, message_id)` 而不是内容哈希

内容哈希（`digest`）是**存储层**的幂等，它保证"同样的字节只存一份"。
但它挡不住**重复计费**：合成是花钱的动作，同样的正文用同样的音色再合成一次，
产出的字节可能不同（供应商侧有随机性、模型版本会变），digest 也就不同 ——
于是存储层认为这是两份新内容，钱已经花了两遍。

所以这里另设一道**合成幂等**，键是 `(turn_id, message_id)`：

- `turn_id` 决定"哪一轮"
- `message_id` 决定"这一轮里的哪一条"（一轮可能拆成几段各自发语音）

两者一起才唯一确定一次合成意图。同一对键**只合成一次**，第二次直接返回
第一次的结果（含 `reused=True`），一分钱不花。

## 为什么用 `idempotency_keys` 而不是新表

那张表（`UNIQUE(scope, key)`）已经是本仓库里"抢占一次不可重复动作"的标准做法，
`DeliveryService._claim_part` 用同一套 SAVEPOINT 抢占逻辑。新开一张表意味着
第二套并发语义、第二个清理任务、以及"两套幂等谁说了算"的争论。

`result_ref` 存 **digest**，于是"合成过了吗" → "产物在哪"是一跳。

## 状态机：为什么 `in_progress` 和 `uncertain` 都不是失败

```
                    ┌──────────────┐
        抢占成功     │ in_progress  │  另一个 worker 正在合成
      ─────────────>│              │
                    └──────┬───────┘
                           │ 合成完成
                           ▼
                    ┌──────────────┐
                    │   succeeded  │  result_ref = digest
                    └──────────────┘

                    ┌──────────────┐
   合成抛了「确定   │   failed     │  允许下次重来（换供应商/重试）
   没花钱」的错     └──────────────┘
                           ▲
                    ┌──────┴───────┐
   合成抛了「可能   │  uncertain   │  **绝不再合成**
   已经计费」的错   └──────────────┘
```

`uncertain` 的判据是**错误是否可重试**（`SpeechError.retriable`）以及它是否
属于「上游可能已经处理了」的那一类（超时、限流、协议不符）。超时是最典型的：
请求发出去了、对方可能已经合成并计费，只是我们没等到响应。这时候再合成一遍
就是**花第二遍钱**，而且用户可能听到两段。

这一点与 `DeliveryService` 的取向一致（`uncertain` 不重发，交给对账），
只是这里不确定的是**计费**而不是**送达**。

## 与延迟回复标记的关系

`<<等会回 90>>` 由 `domains/deferred/markers.py` 处理，发生在轮次层。
语音编排只看**已经落定的正文**：一条延后的回复到点后由 worker 建新轮次，
那个轮次同样会走这里，`turn_id` 是新的——所以"延后 + 语音"天然共存，
不需要特意协调。唯一需要注意的是标记本身不能念出来，所以这里的正文已经
被调用方洗过（`strip_all_markers`）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import IdempotencyKey, utcnow
from ling.infra.security import new_id
from ling.ports.speech import (
    ExpressionIntent,
    SpeechError,
    SpeechErrorKind,
    SpeechUsage,
    TtsChunk,
    TtsEnd,
    TtsProvider,
    TtsRequest,
    TtsStart,
    TtsVoice,
)

if TYPE_CHECKING:  # pragma: no cover - 只为类型
    from typing import Protocol as _Protocol

    class MediaSink(_Protocol):
        """本模块真正需要的**两个**媒体能力，用 Protocol 描述而不是 import 实现。

        ## 为什么不用 `from ling.domains.media.assets import GeneratedMediaService`

        域边界闸门（`tests/unit/test_domain_boundaries.py`）拦下了这个 import。
        它的建议是「改成经 ports 端口注入」。这里就按那个做：speech 域**根本
        不需要知道** media 域有哪几个类，它只需要"存一段字节"和"查一份元数据"。

        ## 顺带的好处

        - 不 import 实现模块，media 域重构不会牵连 speech；
        - 测试可以直接塞一个只有两个方法的假件，不必造整个 media 服务；
        - 这两个签名就是**契约本身**，写在需要它们的地方。

        真实实现（`GeneratedMediaService`）天然满足这个协议，不需要继承。
        """

        async def save_bytes(self, data: bytes, **kwargs: Any) -> Any: ...

        async def view(self, digest: str) -> Any: ...

logger = logging.getLogger("ling.domains.speech.orchestration")

#: 合成幂等的键作用域。
#:
#: 与 `delivery.PART_SCOPE`（`"delivery_part"`）分开：同一个 `(turn, message)`
#: 一次是"合成"，一次是"投递"，两件不同的事、两套不同的重试语义。共用一个
#: scope 会让投递的失败状态被读成合成的失败状态。
SYNTHESIS_SCOPE = "speech_synthesis"

#: `in_progress` 超过这么久视为**前任死了**，允许重新抢占。
#:
#: 为什么不永久占着：一个 worker 在合成中途被 kill，键就永远停在 `in_progress`，
#: 这条消息**永远发不出语音**，而且没有任何报错——用户只会觉得"她今天怎么不
#: 发语音了"。给一个远大于最长合成耗时的租期（TTS 一般几秒，取 5 分钟），
#: 过期即视为放弃。
STALE_CLAIM_SECONDS = 300


class SynthesisState(StrEnum):
    """一次合成在幂等表里的状态。"""

    in_progress = "in_progress"
    """已抢占、正在合成。**不代表失败**，别的 worker 看到它就等着。"""

    succeeded = "succeeded"
    """合成成功，`result_ref` 是 digest。"""

    failed = "failed"
    """确定没花成钱（参数错、密钥没配、文本为空）。允许重来。"""

    uncertain = "uncertain"
    """可能已经计费（超时/限流/协议不符）。**绝不重新合成**。"""


#: 状态在 `idempotency_keys.result_ref` 里的**编码前缀**。
#:
#: ## 为什么不加一列 `state`
#:
#: `idempotency_keys` 只有 ``id / scope / key / result_ref / created_at``
#: （`0001_baseline.py` 就是这五列，之后再没加过）。加列要写迁移，而
#: `migrations/` 不在本任务的写作用域里；更重要的是——这张表是**共享**的
#: （投递段级幂等也在用），为一个新消费者加一个专属列，等于让所有消费者
#: 都背着它。
#:
#: ## 编码方式
#:
#: ```
#:   result_ref = "v1:in_progress"               正在合成
#:   result_ref = "v1:succeeded:<digest>"        成功，digest 是产物
#:   result_ref = "v1:failed:<原因摘要>"          确定没花钱，可重来
#:   result_ref = "v1:uncertain:<原因摘要>"       可能已计费，绝不再合成
#: ```
#:
#: 前缀 `v1:` 让将来真的加列时能识别旧格式并迁移。**`result_ref` 是
#: `VARCHAR(128)`**，所以原因摘要要截断——这里只存"在哪一步、什么 kind"，
#: 不存整条错误消息（那属于日志）。
_RESULT_PREFIX = "v1:"

#: 原因摘要的长度上限：`128 - len("v1:uncertain:")` 再留点余量。
_MAX_REASON_CHARS = 80


def encode_state(state: SynthesisState, *, detail: str = "") -> str:
    """把状态（+可选摘要）编成 `result_ref` 的值。"""
    if state is SynthesisState.succeeded:
        return f"{_RESULT_PREFIX}{state.value}:{detail}"
    if not detail:
        return f"{_RESULT_PREFIX}{state.value}"
    return f"{_RESULT_PREFIX}{state.value}:{detail[:_MAX_REASON_CHARS]}"


def decode_state(result_ref: str | None) -> tuple[SynthesisState, str]:
    """把 `result_ref` 解回 `(状态, 摘要/digest)`。

    解不出来时按 **`uncertain`** 返回：这张表在我们之前的使用者
    （`DeliveryService`）里 `result_ref` 存的是裸 operation id，没有前缀。
    若那个 key 恰好与本模块的 key 相同（不可能，但代码要按"可能"写），
    保守解释成"结果未知、不要再合成"永远比"当它没做过、重来一次"安全
    ——前者只是不发语音，后者是重复计费。
    """
    raw = str(result_ref or "")
    if not raw.startswith(_RESULT_PREFIX):
        return SynthesisState.uncertain, ""
    body = raw[len(_RESULT_PREFIX) :]
    head, _, tail = body.partition(":")
    try:
        return SynthesisState(head), tail
    except ValueError:
        return SynthesisState.uncertain, tail


# --------------------------------------------------------------------------- #
# 错误映射：适配器内部类型 → 端口类型
# --------------------------------------------------------------------------- #


#: `TtsErrorKind` → `SpeechErrorKind` 的查表。
#:
#: ## 前七类是**同名直通**，不是有损转换
#:
#: SP00 冻结端口时把 `SpeechErrorKind` 的前七类做成与 `TtsErrorKind`
#: **取值逐字一致**（`authentication` / `rate_limited` / `invalid_request` /
#: `unavailable` / `timeout` / `protocol` / `not_configured`），端口 docstring
#: 的原话是「映射是一张一眼能对上的表，不是有损转换」。
#:
#: 所以这里不需要"猜哪一类对应哪一类"——直接按名字取。这也意味着将来
#: `models/tts_common.py` 里的 `TtsError` 被删掉时（Codex 裁决要求），
#: 这张表就是**唯一**的删除点，三家 adapter 一行都不用动。
#:
#: ## 为什么转换在编排层而不是 adapter 里
#:
#: adapter 不该知道"端口这一版有几个 kind"。端口加一个成员，这张表跟着变。
#:
#: ## `_CONVERSION_LOSSY`：哪些映射会**丢掉**信息
#:
#: 同名的七类无损。其余（adapter 出现了端口没有的 kind）落到 `unsupported`：
#: 它表达的是"这个 provider 做不到被要求的事"，比 `unavailable` 更接近真相
#: ——后者会让上层去重试，而这类问题重试不会好。
_TTS_KIND_TO_SPEECH: dict[str, SpeechErrorKind] = {
    "authentication": SpeechErrorKind.authentication,
    "rate_limited": SpeechErrorKind.rate_limited,
    "invalid_request": SpeechErrorKind.invalid_request,
    "unavailable": SpeechErrorKind.unavailable,
    "timeout": SpeechErrorKind.timeout,
    "protocol": SpeechErrorKind.protocol,
    "not_configured": SpeechErrorKind.not_configured,
}

#: adapter 出现了端口没有的 kind 时落到哪里。
#:
#: `unsupported` 而不是 `unavailable`：后者语义是"暂时不可用，等等再试"，
#: 而上层据此重试只会得到同一个结果。前者明确说"做不到"，上层会停手。
_UNKNOWN_TTS_KIND = SpeechErrorKind.unsupported

#: **可能已经让上游花钱**的失败类型。这几种一律记 `uncertain`，不再合成。
#:
#: - `timeout`：请求发出去了，对方可能已经合成并计费，只是响应没等到；
#: - `rate_limited`：上游已经处理到"限流"这一步，说明它收到了请求；
#: - `protocol`：对方 200 了但内容不符，**已经算过一次调用**；
#: - `unavailable`（5xx）：上游可能已经执行完才崩，无法确定。
#:
#: 反过来，`invalid_request` / `authentication` / `not_configured` 都是**在
#: 上游执行之前**就被拒的（400/401 是请求校验阶段），不会计费，允许重来。
_BILLING_MAYBE_KINDS = frozenset(
    {
        SpeechErrorKind.timeout,
        SpeechErrorKind.rate_limited,
        SpeechErrorKind.protocol,
        SpeechErrorKind.unavailable,
    }
)


def to_speech_error(exc: Exception) -> SpeechError:
    """把适配器抛出的任意异常转成端口的 `SpeechError`。

    **已经是对口类型就原样返回**（幂等）：调用方可能已经把错误转换过一次，
    再转一遍会丢掉原始的 `kind`。

    未识别的异常一律 `error` + `retriable=False`：一个我们不认识的异常，
    不该被当成"重试一下就好"——那会在真正的 bug 上无限重试。
    """
    if isinstance(exc, SpeechError):
        return exc
    # 延迟 import：`ling.models` 是可选的运行时依赖（没配 TTS 就没有），
    # 而且域层不该在模块加载期依赖某个 provider 实现包。
    try:
        from ling.models.tts_common import TtsError
    except ImportError:  # pragma: no cover - models 包永远存在
        TtsError = None  # type: ignore[assignment,misc]
    if TtsError is not None and isinstance(exc, TtsError):
        raw_kind = getattr(exc.kind, "value", str(exc.kind))
        kind = _TTS_KIND_TO_SPEECH.get(str(raw_kind), _UNKNOWN_TTS_KIND)
        return SpeechError(kind, exc.message, retriable=bool(exc.retriable))
    return SpeechError(
        # 未识别的异常：`unsupported` 表达"我们不知道它做不到什么"，
        # 上层会停手而不是无限重试。用 `unavailable` 会诱导重试。
        SpeechErrorKind.unsupported,
        # **带上原始消息**：只给类型名（"TypeError"）时排障等于从零开始——
        # 而这条消息最终会进日志和 `result_ref`（截断到 80 字符）。
        # `SpeechError.message` 会脱敏，所以这里可以带。
        f"语音合成失败（{type(exc).__name__}: {exc}）"[:300],
        retriable=False,
    )


# --------------------------------------------------------------------------- #
# 编排用的输入/输出值对象
# --------------------------------------------------------------------------- #


def voice_intent(*, config_revision: int = 0, voice_config_hash: str | None = None) -> ExpressionIntent:
    """构造一个「她选了语音」的意图。

    只在**测试与默认路径**上用。真实链路里这个对象由
    `capabilities/expression_choice.py` 解析 `[speak]` 标签得到——
    决策权在她，不在编排层（§17.4）。
    """
    return ExpressionIntent(
        output_modality="voice",
        source="model",
        config_revision=config_revision,
        voice_config_hash=voice_config_hash,
    )


@dataclass(frozen=True)
class SynthesisRequest:
    """一次语音合成的完整意图。

    `turn_id` + `message_id` 是**幂等键**，两个都必须给：只给 `turn_id` 时
    一轮里的多段语音会互相顶掉（第二段以为第一段已经合成过），只给
    `message_id` 时同一段被重投两次无法与"另一段的巧合 id"区分。
    """

    turn_id: str
    message_id: str
    text: str
    window_id: str
    actor_id: str
    subject_id: str | None = None
    #: **她自己的表达选择**（§17.4）。到编排层这一步时已经解析完 `[speak]`，
    #: 所以这里必须是 `output_modality="voice"`——不是 voice 的请求不该进来。
    #: 默认给一个 model 来源的语音意图，方便测试与"确定要发语音"的调用点。
    intent: ExpressionIntent = field(default_factory=voice_intent)
    #: 三家各自的音色值对象（`MiniMaxVoice | FishAudioVoice | MimoReferenceVoice`）。
    #:
    #: **没有统一的 `voice_key`**：SP00 的 §17.2 撤销了跨厂商映射表，
    #: 同一个"温柔"在三家是三种完全不同的东西，强行对应会产生
    #: "换了一家听起来完全变了"还不报错。所以音色是每家自己的值对象。
    voice: TtsVoice | None = None
    #: 走哪家的模型 id。与 `voice` 的类型必须匹配（由调用方保证）。
    model_id: str = ""
    #: 产出方标识，写进 `media_assets.producer`，用于排查"哪个后端出的文件"。
    producer: str = "tts"
    #: 期望的容器格式。端口限制为 wav / mp3 / pcm16。
    response_format: Literal["wav", "mp3", "pcm16"] = "mp3"


@dataclass(frozen=True)
class SpeechArtifact:
    """合成完成后的产物描述——**投递层需要的一切**，不含字节、不含正文。

    `digest` 是 `media_assets` 的主键，也是文件名。投递层拿它去
    `export_for_window()` 换出 transport 需要的东西（URL / 路径 / base64）。
    """

    digest: str
    mime_type: str
    format: str
    size_bytes: int
    duration_ms: int | None
    sample_rate: int | None
    #: 这次是真的合成了（False = 命中**合成幂等**，一分钱没花）。
    #:
    #: 注意与"存储层去重"分开：`media_assets` 以内容 sha256 为主键，两段不同
    #: 文本若产出**完全相同的字节**（同一音色、同一句"嗯"），存储层会返回
    #: `created=False`，但那是**两次独立合成**、两次都花了钱。
    #: 所以这个字段只反映"有没有真的调过 provider"，不反映存储层是否新建文件。
    reused: bool
    #: 上游给的请求 id（排障用）。命中幂等时为 None——那一次不是这次合成的。
    provider_request_id: str | None = None
    usage: SpeechUsage | None = None
    asset_id: str = ""


@dataclass(frozen=True)
class SynthesisOutcome:
    """一次编排的结果。

    `skipped` 与 `failed` **同时为假**才表示"有一段可以投递的音频"。
    这样写是为了让调用方不可能只检查其中一个：三个字段都必须被面对。
    """

    artifact: SpeechArtifact | None
    intent: ExpressionIntent
    #: 决策是"不说语音"，所以压根没合成。
    skipped: bool = False
    #: 合成失败（含"可能已计费，不再重试"）。
    failed: bool = False
    detail: str = ""
    #: 失败是否可能已经花过钱。True 时调用方**不得**再触发一次合成。
    billed_uncertain: bool = False

    @property
    def ready(self) -> bool:
        """有一段可以投递的音频。"""
        return self.artifact is not None and not self.failed and not self.skipped


# --------------------------------------------------------------------------- #
# 编排服务
# --------------------------------------------------------------------------- #


class SpeechOrchestrator:
    """把「要发语音」变成「存储里有一段音频」。

    依赖注入而不是自己构造：`tts`（端口实现）、`media`（存储服务）都由
    组合根给。测试因此可以塞一个假 provider，不必碰网络，也不必造一整套设置。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        tts: TtsProvider,
        media: MediaSink,
    ) -> None:
        self._session = session
        self._tts = tts
        self._media = media

    async def synthesize(self, request: SynthesisRequest) -> SynthesisOutcome:
        """合成一段语音（幂等）。

        ## 意图是**输入**，不是这里算的

        §17.4 把"这条用文字还是语音"的决定权交给了她自己（她在最终回复开头
        写 `[speak]`），由 `capabilities/expression_choice.py` 解析成
        `ExpressionIntent`。所以本方法**不判断要不要发语音**——它被调用就
        表示"已经决定要发"。

        确定性代码仍然可以在这里说**做不到**（没配音色、供应商不支持该格式），
        那记成失败，但**不能替她抛硬币**：旧版的 `decide_speech(weight, roll)`
        已经连同概率一起被删掉了。
        """
        if request.intent.output_modality != "voice":
            # 调用方传了一个"文字"意图进来，说明上游判断有 bug。**不合成**
            # ——为一个她没选的形态花钱，比报错更糟。
            return SynthesisOutcome(
                artifact=None,
                intent=request.intent,
                skipped=True,
                detail=f"意图是 {request.intent.output_modality}，不合成语音",
            )

        text = (request.text or "").strip()
        if not text:
            return SynthesisOutcome(
                artifact=None,
                intent=request.intent,
                failed=True,
                detail="正文为空，不值得合成（空音频发出去是一条没有声音的语音）",
            )

        claim = await self._claim(request)
        if claim.replay is not None:
            return claim.replay
        key_row = claim.row
        assert key_row is not None  # 抢占失败且无 replay 时不会返回到这里

        try:
            artifact = await self._run_synthesis(request, text)
        except asyncio.CancelledError:
            # The provider may have accepted and billed the request before the
            # local task was cancelled. Keep the idempotency record fail-closed.
            key_row.result_ref = encode_state(
                SynthesisState.uncertain,
                detail=SpeechErrorKind.cancelled.value,
            )
            await self._session.flush()
            raise
        except SpeechError as exc:
            return await self._settle_error(key_row, exc, request.intent)
        except Exception as exc:  # noqa: BLE001 - 未识别异常也要落状态，否则键永远卡住
            return await self._settle_error(key_row, to_speech_error(exc), request.intent)

        key_row.result_ref = encode_state(SynthesisState.succeeded, detail=artifact.asset_id or artifact.digest)
        await self._session.flush()
        return SynthesisOutcome(artifact=artifact, intent=request.intent)

    # -------------------------------------------------------------- 幂等闸

    @dataclass(frozen=True)
    class _Claim:
        """抢占结果：要么拿到 `row`（可以合成），要么拿到 `replay`（直接返回）。"""

        row: IdempotencyKey | None = None
        replay: SynthesisOutcome | None = None

    async def _claim(self, request: SynthesisRequest) -> _Claim:
        """抢占 `(turn_id, message_id)` 这一次合成。

        三种走向：

        1. **没有键** → 插入并合成（正常路径）；
        2. **有键、状态可继续**（`failed`，或过期的 `in_progress`）→ 重置后合成；
        3. **有键、状态不可继续**（`succeeded` / `uncertain` / 新鲜的
           `in_progress`）→ **不合成**，返回一个表达该状态的 outcome。
        """
        key = f"{request.turn_id}:{request.message_id}"
        existing = (
            await self._session.execute(
                select(IdempotencyKey).where(
                    IdempotencyKey.scope == SYNTHESIS_SCOPE, IdempotencyKey.key == key
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            row = IdempotencyKey(
                id=new_id("spk"),
                scope=SYNTHESIS_SCOPE,
                key=key,
                result_ref=encode_state(SynthesisState.in_progress),
            )
            try:
                # SAVEPOINT：并发下另一个 worker 可能刚好抢先插入同一个键。
                # 唯一约束一炸就把这一次插入单独回滚，**不能**把外层事务标脏
                # ——外层还带着这一轮已落库的消息。
                async with self._session.begin_nested():
                    self._session.add(row)
                    await self._session.flush()
            except IntegrityError:
                existing = (
                    await self._session.execute(
                        select(IdempotencyKey).where(
                            IdempotencyKey.scope == SYNTHESIS_SCOPE, IdempotencyKey.key == key
                        )
                    )
                ).scalar_one_or_none()
                if existing is None:  # pragma: no cover - 约束炸了但查不到，理论上不发生
                    raise
            else:
                return SpeechOrchestrator._Claim(row=row)

        return await self._resume_or_replay(existing, key, request.intent)

    async def _resume_or_replay(
        self, row: IdempotencyKey, key: str, intent: ExpressionIntent
    ) -> _Claim:
        """键已存在时决定"接着合成"还是"直接返回"。"""
        state, detail = decode_state(row.result_ref)

        if state is SynthesisState.succeeded:
            digest = detail
            artifact = await self._artifact_for_digest(digest)
            if artifact is not None:
                logger.info("语音合成命中幂等，跳过（不重复计费）key=%s", key)
                return SpeechOrchestrator._Claim(
                    replay=SynthesisOutcome(
                        artifact=SpeechArtifact(
                            digest=artifact.digest,
                            asset_id=getattr(artifact, "asset_id", ""),
                            mime_type=artifact.mime_type,
                            format=artifact.format,
                            size_bytes=artifact.size_bytes,
                            duration_ms=artifact.duration_ms,
                            sample_rate=artifact.sample_rate,
                            reused=True,
                        ),
                        intent=intent,
                        detail="同一条消息已经合成过，复用上次的产物",
                    )
                )
            # 状态说成功、产物却没了（被清理任务删了）：这是"记录在、文件没了"。
            # 允许重新合成——用户的音频确实不存在了，而复用一条指向不存在文件
            # 的 digest 会让投递层拿到一个必然失败的结果。
            logger.warning("合成幂等记录指向的产物已消失，重新合成 key=%s digest=%s", key, digest)
            _reset_claim(row)
            await self._session.flush()
            return SpeechOrchestrator._Claim(row=row)

        if state is SynthesisState.uncertain:
            # **绝不重新合成**。上游可能已经计费，再合成一次就是花第二遍钱。
            # 也不能说它失败：我们不知道它成没成。
            logger.warning("语音合成上次结果不确定，不再重试（避免重复计费）key=%s", key)
            return SpeechOrchestrator._Claim(
                replay=SynthesisOutcome(
                    artifact=None,
                    intent=intent,
                    failed=True,
                    billed_uncertain=True,
                    detail=(
                        "上一次合成结果不确定（可能已经计费），不再重新合成；"
                        f"原因：{detail or '未记录'}"
                    ),
                )
            )

        if state is SynthesisState.in_progress and not _is_stale(row):
            # 另一个 worker 正在合成同一段。不抢、不重复合成。
            logger.info("语音合成已被其他执行者处理中，跳过 key=%s", key)
            return SpeechOrchestrator._Claim(
                replay=SynthesisOutcome(
                    artifact=None,
                    intent=intent,
                    failed=True,
                    detail="同一段语音正在被另一个执行者合成，本次跳过（不重复计费）",
                )
            )

        # `failed`，或过期的 `in_progress`：可以重来。
        _reset_claim(row)
        await self._session.flush()
        return SpeechOrchestrator._Claim(row=row)

    # -------------------------------------------------------------- 真正合成

    async def _run_synthesis(
        self, request: SynthesisRequest, text: str
    ) -> SpeechArtifact:
        """调 provider → 落媒体资产。**这里是唯一花钱的地方。**"""
        voice = request.voice
        if voice is None:
            raise SpeechError(
                SpeechErrorKind.not_configured,
                "没有可用的音色配置，无法合成语音",
                retriable=False,
            )

        tts_request = TtsRequest(
            model_id=request.model_id,
            text=text,
            # **直接把值对象交给端口**（§17.2 定案签名）：不再拆成字符串。
            # `speed` / `instructions` 在端口里已经删掉——它们本来就是
            # MiniMax 一家的字段，被塞进通用请求会让另两家"收到但不理解"。
            # 那类字段现在待在 `MiniMaxVoice` 里，与它同源。
            voice=voice,
            response_format=request.response_format,
        )
        start, payload, end = await self._collect(tts_request, voice)

        # 时长/采样率优先取上游给的。上游没给就**留 None**，不估算：
        # 假时长会让"她说完了没"的判断建立在编造的数字上。
        container = _container_of(start)
        fmt = _format_for(container, request.response_format)

        saved = await self._media.save_bytes(
            payload,
            actor_id=request.actor_id,
            window_id=request.window_id,
            kind="audio",
            fmt=fmt,
            mime_type=_mime_for(fmt),
            sample_rate=start.spec.sample_rate_hz,
            duration_ms=_duration_of(end),
            subject_id=request.subject_id,
            turn_id=request.turn_id,
            message_id=request.message_id,
            producer=request.producer,
            # 让存储层自己校验"我算出的 hash 就是上游声明的那个"——
            # 传 None 时它照常算，这里显式给出是因为上游若提供了 digest
            # 就能在**落盘前**发现"内容在半路被改了"。
            expected_digest=None,
        )
        return SpeechArtifact(
            digest=saved.digest,
            asset_id=getattr(saved, "asset_id", ""),
            mime_type=_mime_for(fmt),
            format=fmt,
            size_bytes=saved.size_bytes,
            duration_ms=_duration_of(end),
            sample_rate=start.spec.sample_rate_hz,
            # **恒为 True**：能走到这里就一定刚刚调过 provider。
            # 不能用 `not saved.created` —— 那是存储层的去重结果（内容相同
            # 就不新建行），而"内容相同"恰恰可能来自两次独立的合成。
            # 把两者混成一个字段会让"两段不同的话各自合成、但字节恰好一样"
            # 被误报成"复用了上次的合成"，从而掩盖真实的重复计费。
            reused=True,
            provider_request_id=end.provider_request_id,
            usage=end.usage,
        )

    async def _collect(
        self, tts_request: TtsRequest, voice: TtsVoice
    ) -> tuple[TtsStart, bytes, TtsEnd]:
        """跑完 `TtsStart → TtsChunk* → TtsEnd`，返回 (start, 拼接后的字节, end)。

        ## 没有 `TtsEnd` 就不算成功

        这是端口的硬规则（`ports/speech.py` 的模块注释）：「**至少一个非空
        chunk 才算成功**」，且「**没有 End 就不发布完整产物**」。所以一个
        只有 chunk、没有 End 的流是**失败**，不是"部分成功"——把半段音频存下来
        发出去，用户听到的是半句话然后突然断掉，而本地记录说已送达。

        ## 为什么不用 `isinstance` 分派

        适配器的 `synthesize()` 是本仓库自己的实现，但 `TtsProvider` 是
        Protocol，第三方实现可能是别的类型。按 `isinstance` 认会静默丢掉事件。
        """
        chunks: list[bytes] = []
        start: TtsStart | None = None
        end: TtsEnd | None = None

        stream = await self._call_provider(tts_request, voice)
        async for event in stream:
            if isinstance(event, TtsStart):
                start = event
            elif isinstance(event, TtsEnd):
                end = event
            elif isinstance(event, TtsChunk):
                if event.audio:
                    chunks.append(event.audio)
            else:  # pragma: no cover - 端口只有三种事件
                logger.warning("忽略未知的 TTS 事件类型：%s", type(event).__name__)

        if start is None:
            raise SpeechError(
                SpeechErrorKind.protocol,
                "语音合成没有返回 TtsStart，无法得知输出规格",
                retriable=False,
            )
        if not chunks:
            raise SpeechError(
                SpeechErrorKind.protocol,
                "语音合成没有产出任何音频字节",
                retriable=False,
            )
        if end is None:
            raise SpeechError(
                SpeechErrorKind.protocol,
                "语音合成没有返回 TtsEnd（流可能中途断了），不发布不完整的音频",
                retriable=False,
            )
        return start, b"".join(chunks), end

    async def _call_provider(self, tts_request: TtsRequest, voice: TtsVoice) -> Any:
        """拿到 provider 的事件流。

        ## 三家适配器的签名不同，这是事实不是缺陷

        `MiniMaxTts.synthesize(text, *, voice_id=, language_boost=)`,
        `FishAudioTts.synthesize(text, *, reference_id=)`,
        `MimoTts.synthesize(text, *, voice_audio=, voice_mime=, style_context=)`。
        它们**不是**端口协议（端口是 `synthesize(TtsRequest, *, call=)`），
        所以必须在这里翻译。

        ## 为什么不要求适配器改成端口签名

        Codex 裁决要求把 `TtsError` 换成端口的 `SpeechError`，但**没有**要求
        改 `synthesize` 的签名——那是 SP04-05 的地盘，且这三家的 wire 差异
        （参考音频 vs 音色 ID vs 语种增强）本来就不该被抹平成同一个参数表。
        所以翻译留在编排层：适配器保持简单，编排层承担差异。

        ## 同时接受端口签名

        如果传进来的已经是 `TtsProvider`（`synthesize(TtsRequest, *, call=)`），
        就直接按端口调用。判据是"能不能只用一个位置参数 + `call` 关键字调用"，
        而不是 `isinstance`——第三方实现没有共同基类。
        """
        from ling.ports.speech import SpeechCall

        call = SpeechCall(
            operation_id=tts_request.model_id,
            attempt_id="1",
            window_id="",
            turn_id=None,
            config_revision=0,
        )
        if _looks_like_port_provider(self._tts):
            port: Any = self._tts
            return port.synthesize(tts_request, call=call)

        # 音色是**三家各自的值对象**（§17.2 撤销了跨厂商映射），所以按类型分派，
        # 不是按属性名猜。旧版用 `inspect.signature` 探关键字名，那种写法在
        # 某一家的参数名变化时会**静默**传不进去（音色悄悄失效、用默认嗓音合成）。
        from ling.ports.speech import FishAudioVoice, MimoReferenceVoice, MiniMaxVoice

        kwargs: dict[str, Any] = {}
        if isinstance(voice, MiniMaxVoice):
            kwargs["voice_id"] = voice.voice_id
        elif isinstance(voice, FishAudioVoice):
            kwargs["reference_id"] = voice.reference_id
        elif isinstance(voice, MimoReferenceVoice):
            # MiMo 走 voiceclone：参考音频以**原始字节**传进 adapter
            # （adapter 不读文件、不查库，见端口 §3.2 的分工）。
            kwargs["voice_audio"] = voice.audio.content
            kwargs["voice_mime"] = voice.audio.spec.mime_type
            if voice.context:
                kwargs["style_context"] = voice.context
        # 走到这里说明它**不是**端口签名（上一行已按参数表判过）。三家适配器
        # 的 `synthesize` 形状各不相同，所以这里只能是 `Any`——用 `cast` 而不写
        # `# type: ignore`：前者说明"我知道类型是什么"，后者只是把检查关掉。
        adapter: Any = self._tts
        return await _wrap_single_result(adapter.synthesize(tts_request.text, **kwargs))

    # -------------------------------------------------------------- 状态落库

    async def _settle_error(
        self,
        key_row: IdempotencyKey,
        exc: SpeechError,
        intent: ExpressionIntent,
    ) -> SynthesisOutcome:
        """把一次合成失败落到幂等键上，并给出**可执行**的结论。"""
        billed_uncertain = exc.kind in _BILLING_MAYBE_KINDS or exc.retriable
        # 只存 **kind**，不存整条消息：`result_ref` 只有 128 字符，而且
        # 错误正文属于日志（那里有脱敏）。这里要回答的是"下次能不能重来"。
        key_row.result_ref = encode_state(
            SynthesisState.uncertain if billed_uncertain else SynthesisState.failed,
            detail=str(exc.kind),
        )
        await self._session.flush()
        if billed_uncertain:
            logger.warning(
                "语音合成结果不确定（kind=%s），标记为不再重试：%s", exc.kind, exc.message
            )
        else:
            logger.info("语音合成失败（kind=%s）：%s", exc.kind, exc.message)
        return SynthesisOutcome(
            artifact=None,
            intent=intent,
            failed=True,
            billed_uncertain=billed_uncertain,
            detail=exc.message,
        )

    async def _artifact_for_digest(self, digest: str) -> Any:
        """按 digest 取资产元数据；不存在或无返回 None。"""
        if not digest:
            return None
        try:
            return await self._media.view(digest)
        except Exception:  # noqa: BLE001 - AssetNotFound 等，一律当"没有了"
            return None


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def _is_stale(row: IdempotencyKey) -> bool:
    """`in_progress` 的抢占是否已经过期（前任大概率死了）。"""
    created = getattr(row, "created_at", None)
    if created is None:
        return True
    if created.tzinfo is None:
        from datetime import UTC

        created = created.replace(tzinfo=UTC)
    return utcnow() - created > timedelta(seconds=STALE_CLAIM_SECONDS)


def _reset_claim(row: IdempotencyKey) -> None:
    """把一行重置成"可以重新合成"。"""
    row.result_ref = encode_state(SynthesisState.in_progress)
    # `created_at` 刷新，`_is_stale` 才会按本次抢占重新计时。
    row.created_at = utcnow()


def _accepts(provider: object, param: str) -> bool:
    """provider 的 `synthesize` 是否接受 `param` 这个关键字。"""
    import inspect

    try:
        sig = inspect.signature(provider.synthesize)  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):  # pragma: no cover
        return False
    return param in sig.parameters


def _looks_like_port_provider(provider: object) -> bool:
    """是否是端口形态（`synthesize(TtsRequest, *, call=)`）。

    判据是**参数表**而不是类型：端口是 Protocol，第三方实现没有共同基类。
    要求参数表里有 `call` 这个关键字——那是端口签名独有的。
    """
    return _accepts(provider, "call")


async def _wrap_single_result(raw: Any) -> Any:
    """把适配器的 `SynthesizedAudio` 包装成端口的 `TtsStart/Chunk/End` 事件流。

    三家适配器都返回**一次性结果**（`SynthesizedAudio`），而端口是事件流。
    包装而不是要求适配器改成流：端口允许"供应商一次性返回完整音频，也可以只发
    一个 chunk"（`ports/speech.py` 模块注释），所以一个 chunk 是**合规**的。

    容器格式取适配器**嗅探过**的 `container`，不信请求参数——供应商会在不支持的
    组合上静默回退，照抄请求会让下游转码器拿到假信息。

    ## 为什么先 `await`

    适配器的 `synthesize` 是 `async def`，返回的是**协程**（不是结果）。
    必须先把协程跑完才拿得到 `SynthesizedAudio`。这一层以前漏了，症状是
    `coroutine ... was never awaited` 加一个"没有 TtsStart"的假失败。
    """
    import inspect

    from ling.ports.speech import AudioSpec, TtsChunk

    if inspect.isawaitable(raw):
        raw = await raw

    # 已经是一次性结果（有 audio 字节 + container）
    audio = getattr(raw, "audio", None)
    if audio is None:
        # 是异步迭代器（端口形态但没声明 call）——原样返回
        return raw

    container = str(getattr(raw, "container", "") or "")
    sample_rate = getattr(raw, "sample_rate", None)
    start = TtsStart(
        spec=AudioSpec(
            mime_type=_mime_for(container),
            container=container or "mp3",
            codec=container or "mp3",
            sample_rate_hz=sample_rate,
            channels=None,
        )
    )

    # 适配器的 `SynthesizedAudio` 里有 `duration_ms`，而端口把时长装在
    # `TtsEnd.usage.audio_seconds` 里。**必须搬过去**：适配器已经拿到了这个值，
    # 丢掉它会让 `media_assets.duration_ms` 永远为空——而那个字段是"她说完了
    # 没"的依据（播放回执、语音条时长显示都靠它）。
    #
    # 仍然遵守"不估算"：`duration_ms` 为 None 时就是 None，不按字节数除码率。
    duration_ms = getattr(raw, "duration_ms", None)
    if duration_ms is None:
        usage = SpeechUsage(usage_source="unknown")
    else:
        usage = SpeechUsage(
            audio_seconds=round(float(duration_ms) / 1000.0, 3),
            # 这个时长是**上游适配器报的**，不是我们按文本长度估的。
            usage_source="provider",
        )

    async def _stream() -> Any:
        yield start
        yield TtsChunk(sequence=0, audio=bytes(audio))
        yield TtsEnd(
            # `spec` 是端口要求的**二次校验**依据：adapter 说 24k 不代表它
            # 真的重采样了，下游还要拿这个 spec 再验一次实际字节。
            spec=start.spec,
            provider_request_id=getattr(raw, "provider_request_id", None),
            usage=usage,
        )

    return _stream()


def _container_of(start: TtsStart) -> str:
    return start.spec.container or "mp3"


#: 容器 → 存储层认识的格式名。`assets._FORMAT_MIME` 是白名单，对不上会抛错。
_CONTAINER_FORMAT = {
    "mp3": "mp3",
    "wav": "wav",
    "ogg": "ogg",
    "opus": "opus",
    "m4a": "m4a",
    "amr": "amr",
    "silk": "silk",
    # 裸 PCM：存储层没有 pcm 白名单（它发出去没人能播），退回 wav 容器声明
    # 会撒谎（wav 有头，PCM 没有）。所以**明确拒绝**而不是静默改名——
    # 见 `_format_for`。
    "pcm": "pcm",
}


def _format_for(container: str, requested: str) -> str:
    """决定写进 `media_assets.format` 的值。

    **以实际容器为准**，不用请求参数：供应商回错格式时，"请求什么就说什么"
    会让下游拿到假信息。认不出的容器退回请求格式，但那要求调用方的事后排查
    ——所以记一条 warning。
    """
    actual = _CONTAINER_FORMAT.get(container)
    if actual is None:
        logger.warning("无法识别的音频容器 %r，按请求格式 %r 记录", container, requested)
        return requested
    if actual == "pcm":
        # 裸 PCM 不是可投递的音频格式（没有头，播放器解不开）。存成 pcm
        # 会让投递层把它当音频发出去，用户听到噪音或什么都听不到。
        raise SpeechError(
            SpeechErrorKind.invalid_audio,
            "上游返回裸 PCM（无容器头），无法作为可播放音频投递",
            retriable=False,
        )
    return actual


_MIME_BY_FORMAT = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "silk": "audio/silk",
    "amr": "audio/amr",
    "m4a": "audio/mp4",
}


def _mime_for(fmt: str) -> str:
    return _MIME_BY_FORMAT.get(fmt, "audio/mpeg")


def _duration_of(end: TtsEnd | None) -> int | None:
    """从 `TtsEnd.usage` 里取时长（毫秒）。拿不到就是 None，**不估算**。"""
    if end is None:
        return None
    seconds = end.usage.audio_seconds
    if seconds is None:
        return None
    return int(round(float(seconds) * 1000))


def audio_digest_of(data: bytes) -> str:
    """一段音频的 digest（与 `media_assets` 主键同一算法）。

    给测试与排障用：能独立算出"这段字节应该落在哪个 digest"。
    """
    return hashlib.sha256(data).hexdigest()


__all__ = [
    "STALE_CLAIM_SECONDS",
    "SYNTHESIS_SCOPE",
    "SpeechArtifact",
    "SpeechOrchestrator",
    "SynthesisOutcome",
    "SynthesisRequest",
    "SynthesisState",
    "TtsVoice",
    "audio_digest_of",
    "to_speech_error",
]
