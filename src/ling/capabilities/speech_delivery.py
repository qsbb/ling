"""语音投递：把一段**已经合成好**的音频变成一次受回执保护的出站。

## 这个模块只做三件事

1. 把 `SpeechArtifact` 换成渠道能吃的形态（URL / 路径 / base64，由 transport 定）；
2. 组一条**只带音频附件、`text=""`** 的 `OutboundMessage`；
3. 交给 `DeliveryService.deliver()`，沿用已有的段级幂等与回执语义。

**它不合成。** 合成在 `domains/speech/orchestration.py`。分开是刻意的，理由写在
那个模块的开头：合成失败可以换供应商重来，投递 `uncertain` 绝不能重发。
把两件事写在一起，迟早会出现"投递超时了，那再合成一遍吧"——重复计费 +
重复消息。

## 「渠道上只发语音，后台显示语音标签 + 文字」

**一条逻辑消息，两个面，但它们落在两个持久层，不是同一条 wire 消息。**

```
   OutboundMessage              ← wire：真的发给平台的只有这个
     ├── text        = ""        ← 空串，带音频时 OneBot 仍走
     └── attachments = [audio]      build_media_segments 的媒体分支
                                   （`if text:` 不进 text 段；
                                    `if not segments` 才补空 text 段，
                                    而这里已经有 record 段了）

   Message.text                 ← 后台：canonical_text（§17.5 已存在那一半）
   operations.payload           ← 对账：只放 text_digest，不落正文
```

**外发不带正文**，设计文档 `docs/design/SPEECH-IO-ARCHITECTURE.md`
§17.5「默认只发语音，后台显示语音标签与文字」原文：

> | 对方的平台消息 | `OutboundMessage(text="", attachments=[audio asset], mentions=[], ...)`；
> 一个语音 part，无 caption、无正文、**无补空格**／额外 @ 文本。平台确实需要
> 装饰时也不能泄漏正文 |

> **`OutboundMessage.text` 是 wire text，不能兼做后台转写。**「后台语音标签＋文字」
> 不等于发送器收到文字副本。

所以：`text` 必须是 `""`，**不是 `" "`**——补空格就是文档点名禁止的"补空格"，
而且 `text=" "` 外发出去是一条正文只有空白的消息。后台与对账各走各的路：
后台读 `Message.text`，对账读 `text_digest`（见 `ports/channel.py` 里
`delivery_text_digest` 的说明：抄正文进 `operations.payload` 等于开第二份副本）。

## 幂等分两层，各自管各自的账

| 层 | 键 | 保证 |
|---|---|---|
| 合成（`SYNTHESIS_SCOPE`） | `(turn_id, message_id)` | 同一条消息不重复合成 → **不重复花钱** |
| 投递（`PART_SCOPE`） | `(plan_id, part_ordinal)` | 同一条消息不重复外发 → **不重复发消息** |

两层都必须有。只有合成幂等挡不住"合成成功、投递重试"；只有投递幂等挡不住
"投递前一步重跑又合成一次"。它们的失败语义也不同：

- 合成 `uncertain` → 可能花了钱 → **不再合成**，直接放弃这次语音（降级成无语音）；
- 投递 `uncertain` → 可能已经发出去 → **不再投递**，交给对账。

## 降级：合成失败时发什么

`SpeechFallback` 有三种，对应三种不同的用户预期：

- ``text``：**静默退回纯文字**。形态拿不到时内容仍然送到。
- ``notice``：文字 + 一句"语音没合成出来"。
- ``silent``（默认）：**什么都不发**。

默认是 `silent`，依据同一节：

> 合成／转码／投递失败默认**不把原话改发文字**；只记录准确状态，后台可重试。
> 对方需要文字时由其明确请求后形成新的文字选择，或以后另设显式降级策略
> （**不在本期偷偷默认开启**）。

"不偷偷"不等于"永远不许"——`text` / `notice` 两个选项本身保留，由调用方显式
传 `fallback=` 就能选到（`tests/integration/test_speech_orchestration.py` 各有一条
守着）。**但当前没有任何配置读点能选到它们**：`resolve_fallback()` 的唯一生产
调用点是 `worker.py:2838` 的 `resolve_fallback(None)`，全仓也没有
`speech_fallback` 配置字段。把它接到 `settings/` 是另一步的工作，**本轮未接线**。

**绝不**降级成"假装发了语音"——那是本仓库反复防的那类事实伪造。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from ling.contracts.v1 import AttachmentKind, OutboundAttachment
from ling.ports.channel import OutboundMessage
from ling.ports.speech import ExpressionIntent

if TYPE_CHECKING:  # pragma: no cover - 只为类型
    from sqlalchemy.ext.asyncio import AsyncSession

    from ling.capabilities.delivery import DeliveryOutcome
    from ling.domains.media.assets import GeneratedMediaService
    from ling.domains.speech.orchestration import SpeechArtifact, SynthesisOutcome

logger = logging.getLogger("ling.capabilities.speech_delivery")


class SpeechFallback(StrEnum):
    """合成失败时的降级方式。

    **当前没有配置读点能选到它**：`resolve_fallback()` 的唯一生产调用点是
    `worker.py:2838` 的 `resolve_fallback(None)`，全仓也没有 `speech_fallback`
    配置字段。三个值都可以由调用方显式传 `fallback=` 用起来；接到 `settings/`
    是另一步的工作。
    """

    text = "text"
    """静默退回纯文字（**需要显式选择**，不是默认）。"""

    notice = "notice"
    """文字 + 一句语音没合成出来的说明。"""

    silent = "silent"
    """什么都不发（**默认**）。"""


#: 非法/缺失的配置值一律按 `silent` 处理。
#:
#: 依据设计文档 §17.5（`docs/design/SPEECH-IO-ARCHITECTURE.md`）：
#:
#: > 合成／转码／投递失败默认**不把原话改发文字**；只记录准确状态，后台可重试。
#: > ……不在本期偷偷默认开启。
#:
#: 原先这里取 `text`，理由是"配置写错时用户至少还能收到话"。那条理由**方向反了**：
#: 它把"替用户决定降级成文字"当成了安全默认，而文档明确禁止偷偷补文字。语音
#: 失败时要记的是**准确状态**（`SpeechDeliveryState.text_only` + detail），由后台
#: 重试或由对方明确请求文字；"话送到了"不能靠偷换表达形态来换。
DEFAULT_FALLBACK = SpeechFallback.silent


def resolve_fallback(value: str | None) -> SpeechFallback:
    """解析降级配置。非法值 → 默认，并记一条 warning（别静默吞掉配置错误）。"""
    if not value:
        return DEFAULT_FALLBACK
    try:
        return SpeechFallback(str(value))
    except ValueError:
        logger.warning("无法识别的 speech_fallback=%r，按 %s 处理", value, DEFAULT_FALLBACK)
        return DEFAULT_FALLBACK


class SpeechDeliveryState(StrEnum):
    """一次语音投递的**结论**。

    这个枚举是给调用方与日志用的**汇总**，不是权威状态——权威状态在
    `operations.state` 与 `delivery_receipts` 里。两者分开是因为：汇总要能
    一句话说清"用户到底收到什么"，而权威记录要能对账。
    """

    delivered = "delivered"
    """语音条真的送达了（有回执）。"""

    text_only = "text_only"
    """语音没成，按降级配置只发了文字（或什么都没发）。"""

    partial = "partial"
    """语音投递返回不确定——**可能发出去了**。绝不重发，等对账。"""

    failed = "failed"
    """确定没发出去。"""

    unsupported = "unsupported"
    """渠道不支持语音，连尝试都没做。"""

    skipped = "skipped"
    """她本来就不打算发语音（决策 `speak=False`）。"""


@dataclass(frozen=True)
class SpeechDeliveryOutcome:
    """语音投递的完整结果。

    `state` 与 `operation_id` / `detail` 一起给出，调用方不必再拼。
    """

    state: SpeechDeliveryState
    detail: str
    operation_id: str | None = None
    #: 合成产物（成功时有）。降级时为 None。
    digest: str | None = None
    #: 合成产物是否命中了幂等（没有重复花钱）。
    reused: bool = False

    @property
    def delivered(self) -> bool:
        """**唯一**的"用户真的收到了语音"判据。

        与 `DeliveryOutcome.delivered` 保持同一个方向：宁可把"其实发了"记成
        没发（会重试/对账），也不要把"没发出去"记成送达。
        """
        return self.state is SpeechDeliveryState.delivered


@dataclass(frozen=True)
class SpeechDeliveryRequest:
    """一次语音投递需要的全部输入。"""

    turn_id: str
    message_id: str
    window_ref: str
    text: str
    is_group: bool = False
    #: 语音正文**是否已经发过**（例如重投时前一次已经送达）。
    #: 与 `DeliveryService` 的段级幂等互补：那一层按 `(plan_id, ordinal)` 挡，
    #: 这一层按调用方自己的判断挡（例如上一条消息已经发过同一段语音）。
    reply_to: str | None = None
    mentions: tuple[str, ...] = ()
    window_id: str | None = None


class SpeechDelivery:
    """语音投递的编排尾巴。

    依赖注入：`delivery`（投递服务）、`media`（资产导出）、`connector`
    由组合根给。本类不自己建会话、不自己找连接器——那些都是组合根的事。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        delivery: object,
        media: GeneratedMediaService,
        connector: object | None = None,
    ) -> None:
        self._session = session
        self._delivery = delivery
        self._media = media
        self._connector = connector

    def build_message(
        self,
        request: SpeechDeliveryRequest,
        *,
        audio_ref: str,
        mime_type: str,
    ) -> OutboundMessage:
        """组一条**只带音频附件**的出站消息（`text=""`）。

        ## 外发 `text=""`

        设计文档 §17.5（`docs/design/SPEECH-IO-ARCHITECTURE.md`）规定的 wire 形态：

        > `OutboundMessage(text="", attachments=[audio asset], mentions=[], ...)`；
        > 一个语音 part，无 caption、无正文、**无补空格**／额外 @ 文本。

        空串不是"随便挑一个空值"：`connectors/onebot/message.py::build_media_segments`
        里 `if text:` 才加 text 段，`text=""` 不进任何 text 段；只有"完全没有任何段"
        时才补一个空 text 段，而这里已经有语音段了，所以整条消息照发。
        **不能写成 `" "`**——那是文档点名禁止的"补空格"，会外发一条只有空白的正文。

        ## 后台投影由 `Message.text` 承担（不是这个参数）

        同一节：**`OutboundMessage.text` 是 wire text，不能兼做后台转写**。
        后台 owner/admin 要看的 `[语音] 完整文字` + 播放器 + 状态，其文字那一半
        已经在 `infra/models.py::Message.text`（`canonical_text`）里了——那是
        `TurnCoordinator` 落的行，**本类不写**。所以这里不需要、也不应该把正文
        塞进 `payload_extra`：`operations.payload` 按 `ports/channel.py` 里
        `delivery_text_digest` 的约定"只存摘要，不存正文"，而且
        `domains/media/playback.py` 已经在读 `Operation.payload`——那是一条
        参与者可达的路径。

        ## §17.5 要求的显式 operator view／普通参与者 403：本轮未做

        那需要 `Message.output_modality` / `audio_asset_id` / `speech_state`
        三个字段（当前 `grep -c output_modality src/ling/infra/models.py` = 0），
        要新迁移。本轮只修正外发形态与降级默认值，**后台投影的权限边界仍未落地**：
        现在没有任何 operator-only 入口，也没有"普通参与者请求 operator 返回 403"。

        `audio_ref` 是渠道能解析的引用（`asset:<digest>` / 签名 URL /
        `file:///…`），由 `resolve_audio_ref()` 按 transport 算出。
        """
        return OutboundMessage(
            operation_id="",  # 由 DeliveryService.deliver 填入真实的 operation id
            window_ref=request.window_ref,
            # 空串，不是 `request.text`，也不是 `" "`：见上面 §17.5 的原文。
            text="",
            reply_to=request.reply_to,
            # **用类型化附件**，不碰 legacy `media_refs`：后者一律被当作图片，
            # 语音走那条路会被当成一张图发出去（见 onebot/voice.py 开头的说明）。
            attachments=[
                OutboundAttachment(
                    kind=AttachmentKind.audio,
                    ref=audio_ref,
                    mime_type=mime_type,
                    # 刻意**不填 caption**：文档要求"无 caption"。正文不在 wire 上，
                    # 加一句说明就变成"语音 + 文字副本"。
                    caption=None,
                )
            ],
            is_group=request.is_group,
            mentions=list(request.mentions),
        )

    async def resolve_audio_ref(
        self,
        artifact: SpeechArtifact,
        *,
        actor_id: str,
        window_id: str,
        transport: str = "signed_url",
        base_url: str = "",
    ) -> str:
        """按 transport 把产物换成交付给渠道的引用。

        三种形态与 `connectors/onebot/voice.py` 的三种 transport 一一对应，
        **没有"自动选择"分支**——猜错会静默发不出去。

        - ``signed_url``：短时签名 URL。跨机部署默认候选。
        - ``asset``：裸 ``asset:<digest>``，由渠道自己认（同一进程内的渠道）。
        - ``shared_file``：映射成本地绝对路径，NapCat 与 ling 共享目录时用。
        - ``local_file``：把受控的本地文件交给 OneBot connector，由它编码为最终 wire transport。
        """
        from ling.domains.media.exports import ExportTransport

        reference = artifact.asset_id or artifact.digest
        if transport == "asset":
            # 不导出也不读文件：渠道拿 digest 自己去问存储（同进程部署）。
            return f"asset:{reference}"
        if transport == "base64":
            target = await self._media.export_for_window(
                reference,
                transport=ExportTransport.base64,
                requester_actor=actor_id,
                requester_window=window_id,
            )
            if not target.data_uri:
                raise RuntimeError("base64 导出没有返回 data_uri")
            return target.data_uri
        if transport in {"shared_file", "local_file"}:
            # 路径由 media 服务自己算（它知道 `data_dir`），**不让调用方猜**。
            target = await self._media.export_for_window(
                reference,
                transport=ExportTransport.local_path,
                requester_actor=actor_id,
                requester_window=window_id,
            )
            if not target.path:
                raise RuntimeError("local_path 导出没有返回路径")
            return target.path
        target = await self._media.export_for_window(
            reference,
            transport=ExportTransport.signed_url,
            requester_actor=actor_id,
            requester_window=window_id,
        )
        if not target.url:
            raise RuntimeError("signed_url 导出没有返回 URL")
        if base_url and not target.url.startswith(base_url.rstrip("/")):
            # `base_url` 只用于核对：`export_as` 用的是服务自己配的那个。
            # 对不上不算错误（反代会改写域名），但值得记一条——签名 URL 的
            # 域名不对时渠道下载会 404，而本地记录会说"已发送"。
            logger.info(
                "签名 URL 的域名与预期 base_url 不同（预期 %s，实际 %s），渠道需能访问实际域名",
                base_url,
                target.url.split("/", 3)[2] if "//" in target.url else "?",
            )
        return target.url

    async def deliver_speech(
        self,
        *,
        request: SpeechDeliveryRequest,
        outcome: SynthesisOutcome,
        intent: ExpressionIntent,
        actor_id: str,
        subject_id: str | None,
        fallback: SpeechFallback = DEFAULT_FALLBACK,
        transport: str = "signed_url",
        fallback_notice: str = "（这条本来想用语音说，但没合成出来，先这样说）",
    ) -> SpeechDeliveryOutcome:
        """完整投递：有音频就发语音，没有就按 `fallback` 降级。

        默认的 `fallback` 是 `silent`——**不把原话改发文字**（§17.5）。降级发字
        必须由调用方显式传 `text` / `notice` 才发生。

        这是本模块唯一的入口。它自己决定"发语音"还是"降级"，
        调用方不需要（也不应该）在两处各判一次——那种重复判断迟早会分叉。
        """
        if intent.output_modality != "voice":
            # 她自己选了文字（没写 `[speak]`）。这不是失败，也不该有任何
            # "语音降级"的痕迹——那会让人以为她本来想说语音却没说成。
            return SpeechDeliveryOutcome(
                state=SpeechDeliveryState.skipped,
                detail=f"她选择了 {intent.output_modality}（source={intent.source}）",
            )

        if not outcome.ready:
            return await self._deliver_fallback(
                request=request,
                outcome=outcome,
                actor_id=actor_id,
                subject_id=subject_id,
                fallback=fallback,
                fallback_notice=fallback_notice,
            )

        artifact = outcome.artifact
        assert artifact is not None  # `ready` 已保证

        try:
            audio_ref = await self.resolve_audio_ref(
                artifact,
                actor_id=actor_id,
                window_id=request.window_id or request.window_ref,
                transport=transport,
            )
        except Exception as exc:  # noqa: BLE001 - 导出失败不该让整轮回复消失
            # 音频取不到（过期/被清理/授权不过）：这是**语音**失败，不是回复失败。
            # 按降级配置走，绝不因此把文字也扣下。
            logger.warning("语音产物导出失败，降级：%s", exc)
            return await self._deliver_fallback(
                request=request,
                outcome=outcome,
                actor_id=actor_id,
                subject_id=subject_id,
                fallback=fallback,
                fallback_notice=fallback_notice,
            )

        message = self.build_message(request, audio_ref=audio_ref, mime_type=artifact.mime_type)
        result = await self._send(
            message=message,
            actor_id=actor_id,
            subject_id=subject_id,
            plan_id=f"plan_{request.turn_id}",
            ordinal=0,
            payload_extra={
                "message_id": request.message_id,
                "speech": {
                    "digest": artifact.digest,
                    "reused": artifact.reused,
                    "duration_ms": artifact.duration_ms,
                    "transport": transport,
                }
            },
        )
        return SpeechDeliveryOutcome(
            state=_state_of(result),
            detail=_detail_of(result),
            operation_id=getattr(result, "operation_id", None),
            digest=artifact.digest,
            reused=artifact.reused,
        )

    async def _deliver_fallback(
        self,
        *,
        request: SpeechDeliveryRequest,
        outcome: SynthesisOutcome,
        actor_id: str,
        subject_id: str | None,
        fallback: SpeechFallback,
        fallback_notice: str,
    ) -> SpeechDeliveryOutcome:
        """语音拿不到时的降级。

        `silent`（默认）：**什么都不发**，只把失败事实（`detail` 里带原因）如实
        记成 `text_only`。依据 §17.5「合成／转码／投递失败默认不把原话改发文字；
        只记录准确状态，后台可重试」。

        `text` / `notice`：调用方**显式**选了才发文字。那时文字仍然要送到。
        """
        reason = outcome.detail or "语音合成未完成"

        if fallback is SpeechFallback.silent:
            logger.info("语音降级为静默（speech_fallback=silent）：%s", reason)
            return SpeechDeliveryOutcome(
                state=SpeechDeliveryState.text_only,
                detail=f"语音未合成，按配置静默不发送（{reason}）",
            )

        text = request.text
        if fallback is SpeechFallback.notice:
            # 说明**加在后面**：先说正事，再说"本来想用语音"。
            # 放前面会让每一句降级的回复都以道歉开头。
            text = f"{text}\n{fallback_notice}" if text else fallback_notice

        if not text.strip():
            # 正文也是空的：什么都没得发。这种情况调用方本来就不该调到这里，
            # 但静默返回比发一条空消息好。
            return SpeechDeliveryOutcome(
                state=SpeechDeliveryState.text_only,
                detail=f"语音未合成且正文为空，无可发送内容（{reason}）",
            )

        message = OutboundMessage(
            operation_id="",
            window_ref=request.window_ref,
            text=text,
            reply_to=request.reply_to,
            is_group=request.is_group,
            mentions=list(request.mentions),
        )
        # 降级投递**不复用语音那条的幂等键**：它是另一条消息（正文不同），
        # 用同一个 `(plan_id, 0)` 会让它被判定成"这一段已发过"而静默丢掉。
        result = await self._send(
            message=message,
            actor_id=actor_id,
            subject_id=subject_id,
            plan_id=None,
            ordinal=None,
            payload_extra={
                "speech_fallback": fallback.value,
                "speech_failed_reason": reason[:300],
                # 可能已经计费：事后统计"花了钱但用户没听到语音"要靠这个字段。
                "speech_billed_uncertain": outcome.billed_uncertain,
            },
        )
        return SpeechDeliveryOutcome(
            state=SpeechDeliveryState.text_only,
            detail=f"语音未合成，按 {fallback.value} 降级只发文字（{reason}）",
            operation_id=getattr(result, "operation_id", None),
        )

    async def _send(
        self,
        *,
        message: OutboundMessage,
        actor_id: str,
        subject_id: str | None,
        plan_id: str | None,
        ordinal: int | None,
        payload_extra: dict,
    ) -> DeliveryOutcome:
        """调投递服务。

        `connector` 缺失时**明确失败**而不是假装已发送：没有渠道就没有"送达"，
        这个判断不该由调用方去猜。
        """
        if self._connector is None:
            logger.error("语音投递没有可用的渠道连接器，无法发送")
            raise RuntimeError("语音投递缺少渠道连接器：组合根必须先注入 connector")

        deliver = getattr(self._delivery, "deliver", None)
        if deliver is None:  # pragma: no cover - 组合根错误
            raise RuntimeError("语音投递缺少 DeliveryService")

        return await deliver(
            connector=self._connector,
            message=message,
            actor_id=actor_id,
            subject_id=subject_id,
            payload_extra=payload_extra,
            plan_id=plan_id,
            part_ordinal=ordinal,
        )


def _state_of(result: object) -> SpeechDeliveryState:
    """把 `DeliveryOutcome` 翻成语音投递的汇总状态。"""
    from ling.contracts.v1 import OperationState

    state = getattr(result, "state", None)
    if state is OperationState.succeeded:
        return SpeechDeliveryState.delivered
    if state is OperationState.uncertain:
        return SpeechDeliveryState.partial
    error_code = str(getattr(result, "error_code", "") or "")
    detail = str(getattr(result, "detail", "") or "")
    if "不支持" in detail or "unsupported" in error_code:
        return SpeechDeliveryState.unsupported
    return SpeechDeliveryState.failed


def _detail_of(result: object) -> str:
    return str(getattr(result, "detail", "") or "")


__all__ = [
    "DEFAULT_FALLBACK",
    "SpeechDelivery",
    "SpeechDeliveryOutcome",
    "SpeechDeliveryRequest",
    "SpeechDeliveryState",
    "SpeechFallback",
    "resolve_fallback",
]
