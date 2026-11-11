"""OneBot V11 连接器：实现 ChannelConnector 端口。

协议要点（依据 OneBot V11 规范）：
- **事件上报**（OneBot → ling）：HTTP POST JSON，头部带 ``X-Self-ID``；
  配了 secret 时带 ``X-Signature: sha1=<hmac_sha1(secret, body)>``
- **API 调用**（ling → OneBot）：``POST http://host:port/<action>``，
  头部 ``Authorization: Bearer <access_token>``
  响应 ``{"status":"ok","retcode":0,"data":{...}}``
  401=未提供令牌，403=令牌不符，404=无此 API，其余一律 200（含业务失败）
- **回执**：``send_private_msg`` / ``send_group_msg`` 的
  ``data.message_id`` 是平台消息 id，可用于对账

与官方 QQ Bot API 的区别：官方 API 用 openid（每个机器人不同），
OneBot 直接用 QQ 号。因此账号绑定的 ``external_account_id`` 是 QQ 号。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ling.contracts.v1 import (
    CapabilityDescriptor,
    DeliveryReceipt,
    DeliveryState,
    InputPart,
    InteractionEnvelope,
    Modality,
    OutboundRecallState,
    RecallEvent,
    RecallKind,
    RecallReceipt,
    new_id,
)
from ling.ports.channel import (
    ChannelConnector,
    ChannelError,
    ChannelErrorKind,
    OutboundMessage,
    RecallRequest,
    ReceiptQuery,
    delivery_text_digest,
    iter_outbound_attachments,
)

from .message import (
    ParsedMessage,
    build_outbound_segments,
    parse_message,
    window_ref_for,
)
from .settings import OneBotConfig
from .silk import default_codec
from .voice import (
    SignUrl,
    VoiceAsset,
    VoiceSendError,
    build_voice_file_async,
    ensure_silk,
    silk_tmp_root,
    suggest_alternative,
    voice_segment,
    voice_summary,
)

logger = logging.getLogger("ling.connectors.onebot")

# ---------------------------------------------------------------- 对账常量
#
# **全部是工程初值，未经真实 NapCat 长跑验证。** 依据见
# `docs/OUTBOUND-DELIVERY-RESEARCH.md` §4.2/§4.3/§4.5。

# 回显环形缓冲：`message_sent`（我们自己发出的消息的回显）只用来给
# "没有拿到 message_id 的那一段"补 id。**不是必需依赖**——NapCat 的
# `reportSelfMessage` 默认是 false，大多数部署里这条事件根本不会来。
ECHO_MAX_ENTRIES = 64

# 见过的入站 message_id 上限。用于出站引用段的**本地白名单**：
# 引用一个从没见过的 id，在 NapCat 上是"静默丢掉引用段、正文照发"（§4.4），
# 本地能先挡住就没必要让平台去丢。
KNOWN_MESSAGE_ID_LIMIT = 512

# `get_group_msg_history` / `get_friend_msg_history` 一次取多少条。
# 取太多会把 NapCat 打满（这是外呼），20 条覆盖几十秒的窗口足够。
HISTORY_LOOKBACK = 20

# 内容比对的时间窗：`[sent_at - 30s, sent_at + 180s]`。
# 下限留 30 秒是给"本地时间比平台快"的时钟偏差留余量。
HISTORY_MATCH_BEFORE_SECONDS = 30.0
HISTORY_MATCH_AFTER_SECONDS = 180.0

# "查不到 ≠ 没发出"（§4.2）的例外门槛：只有消息**足够旧**时，
# "平台查无此 id + 历史里也没有同文消息"这两个否证才足以判 failed。
# 太新的消息还可能在投递队列里，早判就会误记失败。
EXPLICIT_MISSING_GRACE_SECONDS = 180.0

# 平台明确说"消息不存在"的字样（NapCat 的 GetMsg 抛 `Error('消息不存在')`）。
_MISSING_HINTS = ("消息不存在", "not found", "消息不存在或已撤回")


# 已知、且**按设计**不进入任何链路的 notice 类型（静默丢弃，不记日志）。
#
# 判据只有一条：它不改变"我们手里的知识是否还成立"。撤回会改（所以它单独走
# `_recall_from_notice`），入群/戳一戳/改名片都不会。
#
# 不在这里的类型一律记一条「暂不处理」——静默掉未知类型会让"平台加了新的
# 撤回类事件"这类情况永远看不见。
_IGNORED_NOTICE_TYPES: frozenset[str] = frozenset(
    {
        "notify",  # 戳一戳 / 群签到 / 运气王，sub_type 区分
        "group_increase",
        "group_decrease",
        "group_admin",
        "group_card",
        "group_ban",
        "group_upload",
        "friend_add",
        "group_msg_emoji_like",  # NapCat 扩展：表情回应
    }
)


class OneBotV11Connector(ChannelConnector):
    """一个 OneBot V11 实现（NapCat / Lagrange / go-cqhttp 等）的连接器。"""

    connector_id = "onebot"

    def __init__(
        self,
        config: OneBotConfig,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._http = http
        self.instance_id = config.instance_id
        # 探活结果的短缓存。``health()`` 会**外呼** NapCat（``get_login_info``），
        # 之前端点没有鉴权也没缓存：任何能访问 6520 的人都能用它当放大器，
        # 一秒几十次地打 NapCat。缓存只是限流，不是能力——鉴权在路由层。
        self._health_cache: tuple[float, tuple[bool, str]] | None = None
        # 语音能力（SILK 链路）的探测缓存。
        #
        # 为什么缓存：``capabilities()`` 会被投递层在**每条消息**上调用，
        # 而 readiness 要查 ffmpeg 二进制 + import pysilk（一次磁盘 stat +
        # 一次 C 扩展导入）。不缓存就是每条消息两次 syscall，30 秒一轮的
        # worker 循环叠上来更明显。
        #
        # 为什么必须能失效：依赖是运维**后装**的（``apt-get install ffmpeg``、
        # ``pip install 'ling[speech-codecs]'``）。进程启动时探一次、然后永远
        # 记着"没有语音"，运维装完还得重启才有语音——而"为什么装了还是不能发"
        # 是最难排查的一类问题。所以给 ``refresh_voice_readiness()`` 手动失效，
        # 并在缓存条目里带时间戳让自动过期也能生效。
        self._voice_ready_cache: tuple[float, tuple[bool, str]] | None = None
        # 语音 readiness 的缓存时长：比 health 长得多。装依赖是低频操作，
        # 而探测本身不像外呼 NapCat 那样有成本。
        self.voice_readiness_cache_seconds = 60.0
        # 缓存有效期（秒）。探活是运维动作，10 秒的陈旧度完全够用。
        self.health_cache_seconds = 10.0
        self._health_clock = time.monotonic
        # 群名缓存：``group_id -> (monotonic 时间戳, 群名或 None)``。
        #
        # 为什么不落库：群名是**平台侧的展示信息**，不是业务事实。为它加一个
        # 数据库列要迁移、要回填、还要处理"群改名了什么时候失效"——而它
        # 丢了的代价只是界面显示「群 100200400」。进程内缓存 + 前端回退
        # 已经够用，并且重启后第一次读会自动重新采集。
        self._group_name_cache: dict[str, tuple[float, str | None]] = {}
        self.group_name_ttl = 6 * 3600.0  # 采集成功后的复用时长
        self.group_name_miss_ttl = 300.0  # 采集失败后的静默时长（别一直打 NapCat）
        # 单次采集超时。**故意比 `config.timeout_seconds` 短**：这条外呼挂在
        # 读接口上，等 15 秒的群名不如先显示「群 123」。
        self.group_name_timeout = 2.0
        self.group_name_cache_max = 512

        # 好友名/备注缓存：**一次 `get_friend_list` 拿全量**，不是逐个查。
        #
        # 为什么整表缓存而不是按需查：OneBot 没有"给我这个人的备注"这种动作
        # （`get_stranger_info` 只给昵称，不给备注——备注是**好友关系**的属性，
        # 只有 `get_friend_list` 才有）。所以要么整表拿一次，要么拿不到。
        #
        # 为什么要缓存：会话列表每开一次页面就会问一遍，而好友列表在几百人的
        # 号上是几十 KB 的响应。10 分钟一次足够——改备注不会有人指望立刻生效，
        # 而"每开一次页面打一次 NapCat"是实打实的负担。
        self._friend_names_cache: tuple[float, dict[str, tuple[str | None, str | None]]] | None = None
        self.friend_names_ttl = 600.0  # 成功后的复用时长（10 分钟）
        self.friend_names_miss_ttl = 60.0  # 失败后的静默时长（别一直打）
        self.friend_names_timeout = 3.0  # 比群名宽一点：它是整表响应

        # ---- 对账/引用用到的进程内证据（都不落库） ----
        #
        # 1) 出站回显：`message_sent` 带上我们自己发出的消息的 `message_id`，
        #    与 `send_*_msg` 回执同一个 id 空间（NapCat 的
        #    `MessageUnique.createUniqueMsgId`），所以可以给"没拿到回执 id"
        #    的那一段补 id。
        self._echoes: deque[tuple[datetime, str, str, str]] = deque(
            maxlen=ECHO_MAX_ENTRIES
        )
        # 2) 发出但平台没回 message_id 的那几条：`operation_id -> (window_ref,
        #    正文, 发出时间)`。只在**同一进程**里能把回显对回 operation；
        #    worker 是另一个进程，所以对账不能只依赖它（见 `query_receipt`）。
        self._unidentified: dict[str, tuple[str, str, datetime]] = {}
        # 3) 见过的入站 message_id（引用白名单）。有界：只留最近的，
        #    因为 NapCat 的短 id 表本身也是 5000 条 FIFO（§4.2）。
        self._known_message_ids: deque[str] = deque(maxlen=KNOWN_MESSAGE_ID_LIMIT)
        self._known_message_set: set[str] = set()
        # 4) 外部事件 id → 平台 message_id。上下文要给模型**真实 id**（§5 P0-4），
        #    而 id 只有连接器知道（`f"{self_id}:{message_id}"` 是 OneBot 的拼法）。
        self._event_message_ids: dict[str, str] = {}

    # ------------------------------------------------------------ 出站

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """发送消息（群聊/私聊按 ``message.is_group`` 选 API）。

        ## 三件必须分开的事

        1. **引用白名单**：`reply_to` 必须是**我们真的见过**的入站 message_id，
           否则本地就去掉引用段（降级为普通消息），不让平台去静默丢它
           （NapCat 对无效 reply 是"丢弃该段、正文照发"，§4.4）。
        2. **装饰段兜底**：平台明确拒收（``rejected``）时去掉引用/@ 重发一次，
           正文不能跟着丢。**注意**：调研（§4.5）里 NapCat 全仓没有
           ``retcode=100``，无效引用走的是"静默丢段"，所以这条路基本不会触发；
           真正治本的是给模型真实 id（上下文里的 `[msg_id:…]`）。
        3. **回执语义**：`retcode=0` 但 `data` 为空 / 没有 ``message_id`` 时，
           平台只是"没说失败"，**不是**"确认送达"。这种情况记
           ``uncertain``（进对账），绝不记 ``sent``——`sent` 在上层被算成
           "已送达"，于是"平台没给 id"被当成"发完了"。
        """
        if message.is_group:
            action = "send_group_msg"
            params: dict = {"group_id": self._target_id(message.window_ref)}
        else:
            action = "send_private_msg"
            params = {"user_id": self._target_id(message.window_ref)}
        # 出站 payload 只有目标 id + message 段，**不含 operation_id**：
        # ``message.operation_id`` 是本地 operation 行 ID，只用来回填下面
        # 回执里的 ``operation_id``（本地关联）；平台侧收不到这个字段，也不能
        # 按它去重或对账——平台侧凭据是响应里的 ``data.message_id``。
        mentions = list(getattr(message, "mentions", None) or [])
        reply_to = getattr(message, "reply_to", None)
        # 语音段在**构造阶段**就确定（可能是 base64 内联，几 MB），之后
        # 两条发送路径（首次 / 去装饰重发）都用同一份：重算会读两次文件，
        # 也可能因为签名 URL 的 TTL 在两份里不同而对不上。
        voice_segments = await self._voice_segments_for(message)
        dropped_reply: str | None = None
        if reply_to and self._known_message_set and str(reply_to) not in self._known_message_set:
            # 只丢**引用段**，不丢正文、也不丢 @：引用是装饰，正文才是话本身。
            dropped_reply = str(reply_to)
            logger.info(
                "引用目标不在见过的入站消息里，按普通消息发送（不发送引用段）：%s",
                dropped_reply,
            )
            reply_to = None
        params["message"] = build_outbound_segments(
            message.text,
            list(message.media_refs),
            mentions,
            reply_to,
            voice_segments=voice_segments,
        )

        # 装饰段（引用 / @）被平台明确拒收时，**不能连正文一起丢掉**。
        #
        # 只在 ``rejected`` 上重试：``uncertain``（异步提交/响应不是 JSON/
        # NapCat 的 1200、200）可能**已经发出去了**，重发就是重复消息；
        # ``unreachable`` / ``unauthorized`` 重发也不会有不同结果。
        #
        # 按调研（§4.4/§4.5）：NapCat 对**无效 reply 是静默丢弃该段、正文照发**，
        # 只有"所有段都被丢掉"才会变成错误。所以这条分支在 NapCat 上几乎不会
        # 触发——**别把它当成引用失效的主要修法**，治本是把真实 id 放进上下文。
        degraded: str | None = None
        try:
            data = await self._call(action, params)
        except ChannelError as exc:
            if not (mentions or reply_to) or exc.kind is not ChannelErrorKind.rejected:
                raise
            degraded = exc.message
            logger.warning(
                "装饰段被平台拒收，去掉引用/@ 重发一次（正文不丢）：%s", exc.message
            )
            # **语音段必须跟着走**：这条路径是"去掉装饰再发一次同一条消息"，
            # 不是"发一条更简的"。把 record 丢掉等于让一次拒收变成一次静默的
            # 语音丢失，而回执仍记 delivered——用户什么也没收到，日志里只有
            # 一条"已去掉引用重发"。
            params["message"] = build_outbound_segments(
                message.text,
                list(message.media_refs),
                voice_segments=voice_segments,
            )
            data = await self._call(action, params)

        # 拍一拍：**独立动作**，不是消息段。发完再拍。
        #
        # 失败只记日志、不影响已经送达的正文——拍一拍是"轻"的表达，
        # 因为它失败就把整条回复做成失败，是本末倒置。
        for target in list(getattr(message, "poke_targets", None) or []):
            try:
                poke: dict = {"user_id": str(target)}
                if message.is_group:
                    poke["group_id"] = self._target_id(message.window_ref)
                await self._call("send_poke", poke)
            except Exception as exc:  # noqa: BLE001 - 轻动作失败不致命
                logger.warning("拍一拍失败（user=%s）：%s", target, type(exc).__name__)

        platform_message_id = None
        if isinstance(data, dict):
            mid = data.get("message_id")
            if mid is not None:
                platform_message_id = str(mid)
        notes = []
        if degraded:
            # 降级重发过就如实写进回执：事后对账时能看出"这条为什么少了引用"。
            notes.append(f"已去掉引用/@ 重发（平台拒收装饰段：{degraded}）")
        if dropped_reply:
            notes.append(f"引用目标不在可见范围（{dropped_reply}），已降级为普通消息")
        if platform_message_id is None:
            # 平台回了成功但没有 id：**不能算已送达**，只能算"提交了、未确认"。
            self._remember_unidentified(message)
            notes.append("平台未返回 message_id（提交结果无法确认，进对账而不是记已送达）")
        return DeliveryReceipt(
            receipt_id=new_id("rcpt"),
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=(
                DeliveryState.delivered
                if platform_message_id
                else DeliveryState.uncertain
            ),
            platform_message_id=platform_message_id,
            observed_at=_now(),
            detail="；".join(notes) if notes else None,
        )

    async def send_voice(
        self,
        *,
        window_ref: str,
        asset: VoiceAsset,
        is_group: bool = False,
        sign_url: SignUrl | None = None,
        operation_id: str | None = None,
    ) -> DeliveryReceipt:
        """发一条**纯语音条**（payload 里只有一个 ``record`` 段）。

        .. deprecated:: 语音返工
           **正式路径是 ``send()`` + ``OutboundMessage.attachments``** 里放一个
           ``OutboundAttachment(kind=AttachmentKind.audio, ref=<本地路径>)``。
           ``send()`` 会做同样的 SILK 转换、同样的单条 payload 保证，而且
           走投递层（有计划幂等、节奏、能力检查、operation 落库）。

           保留这个方法是为了**过渡期**：SP07 编排层还没接 ``send()`` 的
           类型化附件路径，现在删掉会让语音完全发不出去。等那边接上之后再删。

          两者不冲突的原因：``send()`` 保证「一个消息最多一个 audio 附件」，
          所以两条路产出的 payload 是**同一个形状**（一个 record 段）。
          区别只在 ``send()`` 会额外带一个空 text 占位段（既有
          ``build_media_segments`` 行为），而这里不带。

        ## 为什么当初是独立方法而不是 ``send(media_refs=[...])``

        ``OutboundMessage.media_refs`` 是 ``list[str]``，构造出来的每一段都是
        ``image``。语音塞进去 = 用户收到一张打不开的图 + 本地回执
        ``delivered``。类型信息必须**显式**传，不能靠扩展名猜。
        （后来 SP00 用 ``attachments`` 解决了这个问题，所以现在能用
        ``media_refs`` 之外的类型化路径。）

        而且语音条通常**不带正文**：``{"type":"text","data":{"text":""}}``
        这种空 text 段一部分实现会报错。这里直接不塞 text 段。

        ## 回执语义与 ``send`` 一致

        ``retcode=0`` 且拿到 ``message_id`` 才是 ``delivered``；平台回了成功
        但没给 id 记 ``uncertain``（进对账）。**绝不**因为"请求没抛异常"
        就记已送达。

        ## 失败一律抛 ``VoiceSendError`` / ``ChannelError``

        不回退成图片、不回退成文本、不截断。caller 要么展示"语音没发出去 + 原因"，
        要么自己决定降级成文字——那是**它**的决定，不是连接器偷偷做的。
        """
        options = self._config.voice_options()
        # 构造 `file` 值（可能要读文件、可能要 await 签名）。失败发生在
        # **任何网络动作之前**：没拿到可用 file 就一个字都不发。
        #
        # ## 为什么这里要先 ensure_silk 再调 build
        #
        # `build_voice_file_async` 内部**也会**调 `ensure_silk`（保证每个
        # transport 都拿到 SILK），但它不返回临时目录——清理得由调用方做。
        # 所以这里先转一次拿到句柄，把转好的 asset 交给 build，最后无论成功
        # 还是失败都删临时文件。
        #
        # 不这么做的后果很实在：每发一条语音就在 /tmp 留一个目录，长期跑下来
        # 会把容器磁盘填满，而且是**静默**的（日志里没有任何错误）。
        silk_handle = ensure_silk(asset, tmp_root=silk_tmp_root(options))
        try:
            file_value = await build_voice_file_async(
                silk_handle.asset, options, sign_url=sign_url
            )
            asset = silk_handle.asset
            size = asset.size_bytes()
            if size > options.max_bytes:
                # 路径/URL 形态不占 JSON 体积，但超大音频在 QQ 侧仍会被拒或
                # 转码失败。早点拒绝，别让它走到平台再变成一句含糊的错误。
                raise VoiceSendError(
                    "too_large",
                    f"{asset.display_name()} 大小 {size} 字节，超过语音上限 "
                    f"{options.max_bytes} 字节。{suggest_alternative(options)}",
                    transport=options.transport.value,
                )
            if is_group:
                action = "send_group_msg"
                params: dict = {"group_id": self._target_id(window_ref)}
            else:
                action = "send_private_msg"
                params = {"user_id": self._target_id(window_ref)}
            segment = voice_segment(file_value)
            # **一个语音条 = 一个 part**：message 里只有这一个 record。
            params["message"] = [segment]
            logger.info("发送语音：%s", voice_summary(asset, options))
            data = await self._call(action, params)

            platform_message_id = None
            if isinstance(data, dict):
                mid = data.get("message_id")
                if mid is not None:
                    platform_message_id = str(mid)
            notes = [voice_summary(asset, options)]
            if platform_message_id is None:
                notes.append("平台未返回 message_id（语音提交结果无法确认，进对账）")
            return DeliveryReceipt(
                receipt_id=new_id("rcpt"),
                operation_id=operation_id or new_id("op"),
                channel=self.connector_id,
                state=(
                    DeliveryState.delivered
                    if platform_message_id
                    else DeliveryState.uncertain
                ),
                platform_message_id=platform_message_id,
                observed_at=_now(),
                detail="；".join(notes),
            )
        finally:
            # **无论成功失败都清临时文件。** 不清理的后果是每发一条语音就在
            # /tmp 留一个目录，长期跑把容器磁盘填满——而且是静默的，日志里
            # 没有任何错误。放在 finally 而不是成功分支，因为失败路径留下的
            # 临时文件更没价值。
            silk_handle.cleanup()

    async def _voice_segments_for(self, message: OutboundMessage) -> list[dict]:
        """从**类型化附件**里取出语音，转成 ``record`` 段。

        ## 为什么不再用 ``getattr(message, "voice_segments")``

        那是 SP00 定稿前的临时契约（``OutboundMessage`` 那时还没有类型化附件）。
        现在 ``OutboundMessage.attachments`` 是正式字段，再留一条
        ``getattr`` 通道就是**第二条不设防的路**：它绕过附件类型检查，
        任何能往对象上挂属性的地方都能偷偷塞一个语音段进来。

        ## 一个消息最多一个语音条

        「一个语音条 = 一个 payload」在这里是**结构性**保证的，不靠运行时
        侥幸：多于一个 audio 附件直接报错。

        为什么不是"循环发 N 次"：``send()`` 的契约是**一次调用 = 一次
        ``_call`` = 一个 ``platform_message_id``**。循环发 N 次却只有一个
        回执 id，意味着另外 N-1 条的回执只能塞进人类可读的 ``detail``
        字符串里——以后对账要再解析它，那是技术债。多段语音由上游拆成
        多条消息（各自有独立 ``part_ordinal``），与 ``kernel/bridge.py``
        已经按 part 循环投递的设计一致（§17.5：一条语音、零附带正文）。
        """
        audios = [
            attachment
            for attachment in iter_outbound_attachments(message)
            if attachment.modality is Modality.audio
        ]
        if not audios:
            return []
        if len(audios) > 1:
            # 错误信息要说**怎么做**，不是只说"参数错误"。
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"一个 OutboundMessage 只能带一个语音附件，收到 {len(audios)} 个。"
                "多条语音请拆成多条消息（各自有独立的 part_ordinal）："
                "OneBot 的每条语音必须是独立的一次 send，"
                "否则 N 条语音只会有 1 个 message_id，其余无法对账",
            )
        return await self._build_voice_segment(audios[0], message)

    async def _build_voice_segment(
        self, attachment: object, message: OutboundMessage
    ) -> list[dict]:
        """把一个 audio 附件变成 ``[record 段]``。

        ``ref`` 的三种形态都走同一套 transport（见 ``voice.py``）：
        本地路径、``base64://``、``http(s)://``。其中只有本地路径能被我们
        转成 SILK——远程 URL 若已是 SILK，平台能直接用；不是 SILK 则明确失败
        （见 ``ensure_silk``），绝不把 mp3 的 URL 发出去让它去转码。
        """
        ref = str(getattr(attachment, "ref", "") or "").strip()
        if not ref:
            raise ChannelError(
                ChannelErrorKind.rejected,
                "语音附件的 ref 为空（需要本地路径、base64:// 或 http(s):// URL）",
            )
        options = self._config.voice_options()
        if ref.startswith("base64://"):
            # 调用方已经把字节编好了（少见：通常是本地路径）。
            # 这里不重新编码——无法确认那串字节是不是 SILK，而"看起来能发"
            # 正是最危险的状态。要求调用方给本地文件，语义才明确。
            raise ChannelError(
                ChannelErrorKind.rejected,
                "语音附件的 ref 不支持 base64:// 形态：连接器需要本地文件才能"
                "校验/转成 SILK。请给本地路径，或用 send_voice(asset=...)",
            )
        asset = VoiceAsset(path=Path(ref), mime_type="audio/silk", name=Path(ref).name)
        return [voice_segment(await build_voice_file_async(asset, options))]

    def build_voice_segments(
        self, asset: VoiceAsset, *, sign_url: SignUrl | None = None
    ) -> list[dict]:
        """给 ``OutboundMessage.voice_segments`` 用的同步版构造。

        ``signed_url`` transport 需要 ``await``，这种情况调用方应该直接用
        ``send_voice`` 或 ``voice.build_voice_segments_async``。
        """
        from .voice import build_voice_segments as _build

        return _build(asset, self._config.voice_options(), sign_url=sign_url)

    def _remember_unidentified(self, message: OutboundMessage) -> None:
        """记住"发出去了但平台没给 id"的那一条，供同进程的对账补 id 用。

        有界：只留最近 128 条（一次长回复最多 5 段，够覆盖正常会话）。
        """
        if len(self._unidentified) >= 128:
            oldest = sorted(self._unidentified.items(), key=lambda kv: kv[1][2])[:32]
            for key, _ in oldest:
                self._unidentified.pop(key, None)
        self._unidentified[str(message.operation_id)] = (
            message.window_ref,
            message.text,
            _now(),
        )

    async def recall(self, request: RecallRequest) -> RecallReceipt:
        """**主动撤回**我们发出去的那条（OneBot ``delete_msg``）。

        ## 只有 ``message_id`` 一条判据

        撤回是不可逆的破坏性动作：撤错了等于把别人的话从群里抹掉。所以这里
        只认 ``request.platform_message_id``——那是我们发送时拿到的**真实回执
        id**，不是正文、不是时间、不是本地 id。上层（
        `domains/conversation/recall.py`）已经在发 action 之前核对过
        "这条确实是她自己发的、还在时间窗内"，连接器只负责把平台的回答翻译成
        五个状态之一。

        ## 平台错误 → 状态映射（**不许猜成成功**）

        | 平台表现 | 我们的状态 | 理由 |
        | --- | --- | --- |
        | ``retcode=0`` | ``recalled`` | 平台明确接受 |
        | 404（实现没有这个 API） | ``unsupported`` | 这个 OneBot 实现不支持 |
        | 业务失败且文案是"消息不存在/已撤回" | ``not_found`` | 目标已经不在平台上 |
        | 业务失败且文案是"超过时间/权限" | ``expired`` | 平台明确拒绝（时间窗） |
        | 其它业务失败 | ``uncertain`` | 可能已经撤了，不能谎报 |
        | 超时/断连/非 JSON | ``uncertain`` | 结果未知，**可重试但不重复撤** |

        **注意**：`expired` 与 `not_found` 都来自平台自己的说法。我们**不**
        用本地时间猜——平台的时间窗会变，而我们的时钟不可信。
        """
        message_id = str(request.platform_message_id or "").strip()
        if not message_id:
            # 走到这里说明上层没做校验（不该发生）。不猜、不发 action。
            return RecallReceipt(
                receipt_id=new_id("rcpt"),
                channel=self.connector_id,
                state=OutboundRecallState.uncertain,
                observed_at=_now(),
                detail="没有可用的平台消息 id，未发起撤回",
            )
        try:
            await self._call("delete_msg", {"message_id": message_id})
        except ChannelError as exc:
            return self._recall_from_error(message_id, exc)
        return RecallReceipt(
            receipt_id=new_id("rcpt"),
            channel=self.connector_id,
            state=OutboundRecallState.recalled,
            platform_message_id=message_id,
            observed_at=_now(),
        )

    def _recall_from_error(self, message_id: str, exc: ChannelError) -> RecallReceipt:
        """把 ``delete_msg`` 的失败翻译成对外状态（见 :meth:`recall` 的映射表）。"""
        kind = exc.kind
        text = exc.message or ""
        lowered = text.lower()
        if kind is ChannelErrorKind.unsupported:
            state = OutboundRecallState.unsupported
            detail = "这个 OneBot 实现没有 delete_msg（不支持撤回）"
        elif kind in (ChannelErrorKind.unreachable, ChannelErrorKind.uncertain):
            # **先按错误类别判，再按文案判**：断连/超时/非 JSON 是"我们没拿到
            # 回答"，绝不能因为文案里恰好有 "timeout" 就被读成"平台说超时"——
            # 那会把一次网络抖动说成"过了撤回时间窗"，用户就再也不会重试了。
            state = OutboundRecallState.uncertain
            detail = f"撤回结果未知（{text[:120]}）"
        elif _looks_recall_expired(lowered):
            state = OutboundRecallState.expired
            detail = "平台拒绝了撤回：超过允许的撤回时间或没有权限"
        elif _looks_missing(lowered):
            state = OutboundRecallState.not_found
            detail = "平台上已经找不到这条消息"
        else:
            # 其余一律"结果未知"：可能已经撤掉了，说失败会让用户重试，
            # 而重试一次撤回只会得到一个"找不到"——那反而像故障。
            state = OutboundRecallState.uncertain
            detail = f"撤回结果未知（{text[:120]}）"
        return RecallReceipt(
            receipt_id=new_id("rcpt"),
            channel=self.connector_id,
            state=state,
            platform_message_id=message_id,
            observed_at=_now(),
            detail=detail,
        )

    async def query_receipt(self, query: ReceiptQuery) -> DeliveryReceipt:
        """对账：OneBot 没有独立的"查送达"API。

        三级证据，**只能单向确认**（§4.2）：

        1. 有 ``platform_message_id`` → ``get_msg``。查到 → ``delivered``；
           平台明确说"消息不存在" → **一个否证**，还要第二个才敢判 failed。
        2. 没有 id → 先看同进程的 ``message_sent`` 回显（回显带真实
           ``message_id``，与发送回执同一 id 空间）；再看
           ``get_group_msg_history`` / ``get_friend_msg_history`` 的**内容比对**
           （自己发的 + 同文 + 时间窗内，且必须**唯一命中**）。
        3. 两个否证（平台查无此 id + 近期历史里没有同文消息）**且**消息已过
           宽限期 → ``failed``；否则一律 ``uncertain``。

        **查不到 ≠ 没发出去**（§4.2）：NapCat 的短 id 表是 5000 条 FIFO，
        进程重启/超量/撤回都会"消息不存在"。所以不允许因为一次查不到就记失败。
        这里的 ``failed`` 只表示"两个独立证据都说没到平台"，而且仍然**不重发**。
        """
        if query.platform_message_id:
            try:
                data = await self._call(
                    "get_msg", {"message_id": query.platform_message_id}
                )
            except ChannelError as exc:
                if _looks_missing(exc.message):
                    return await self._negative_evidence(
                        query, reason=f"get_msg: {exc.message}"
                    )
                return self._receipt(
                    query,
                    DeliveryState.uncertain,
                    detail=f"查询失败，结果未知（{exc.message}）",
                )
            found = isinstance(data, dict) and data.get("message_id") is not None
            if found:
                return self._receipt(
                    query,
                    DeliveryState.delivered,
                    platform_message_id=query.platform_message_id,
                )
            return await self._negative_evidence(query, reason="get_msg: 空 data")

        # 没有平台 id：先看回显（同进程内最快、最准的证据）
        echo_id = self._match_echo(query)
        if echo_id:
            return self._receipt(
                query,
                DeliveryState.delivered,
                platform_message_id=echo_id,
                detail="按 message_sent 回显确认（回显里的 message_id 与发送回执同 id 空间）",
            )
        return await self._negative_evidence(query, reason="无平台 id")

    async def _negative_evidence(
        self, query: ReceiptQuery, *, reason: str
    ) -> DeliveryReceipt:
        """一个否证之后：能再拿到第二个否证、且消息已过宽限期，才判 failed。"""
        scanned = await self._scan_history(query)
        if scanned.matched_id:
            return self._receipt(
                query,
                DeliveryState.delivered,
                platform_message_id=scanned.matched_id,
                detail=f"按历史内容比对确认（{reason}）",
            )
        if scanned.ambiguous:
            return self._receipt(
                query,
                DeliveryState.uncertain,
                detail=f"{reason}；历史里有不止一条同文消息，无法唯一命中",
            )
        if not scanned.ok:
            return self._receipt(
                query,
                DeliveryState.uncertain,
                detail=f"{reason}；历史查询不可用，无法取得第二个否证",
            )
        if not scanned.saw_self:
            # **历史里一条自己的消息都没有** = 还不能证明"这份历史会包含我发的
            # 消息"。调研 §7.6 正是未证实项（`get_group_msg_history` 是否含
            # 机器人自己发的消息，没有实机验证）。在这种情况下"没找到同文消息"
            # 不构成否证——否则整个 NapCat 的行为差异会被我们误判成"没送达"。
            return self._receipt(
                query,
                DeliveryState.uncertain,
                detail=(
                    f"{reason}；历史里看不到任何自己发出的消息，"
                    "无法据此判定（平台历史可能不含自身消息）"
                ),
            )
        # 两个否证都拿到了：还要过了宽限期才敢说"没到平台"
        sent_at = _aware(query.sent_at)
        if sent_at is not None and (
            _now() - sent_at
        ).total_seconds() < EXPLICIT_MISSING_GRACE_SECONDS:
            return self._receipt(
                query,
                DeliveryState.uncertain,
                detail=f"{reason}；历史里也没有同文消息，但消息太新（可能仍在投递）",
            )
        return self._receipt(
            query,
            DeliveryState.failed,
            detail=(
                f"{reason}；近期历史里也没有同文消息（两个独立否证）——"
                "按未到达平台记录。注意：平台已发出但被撤回/超出保留窗口也会长这样"
            ),
        )

    async def _scan_history(self, query: ReceiptQuery) -> _HistoryScan:
        """在窗口历史里找"自己发的、同文的、时间窗内"的消息。"""
        window_ref = query.window_ref or ""
        digest = query.text_digest or ""
        if not window_ref or not digest:
            # 拿不到会话/正文就没法比对：当作"没有证据"，不当作否证。
            return _HistoryScan(ok=False)
        sent_at = _aware(query.sent_at) or _now()
        since = sent_at - timedelta(seconds=HISTORY_MATCH_BEFORE_SECONDS)
        until = sent_at + timedelta(seconds=HISTORY_MATCH_AFTER_SECONDS)
        # 群聊判定：**调用方给了就用，没给才反推**。
        #
        # `ReceiptQuery.is_group` 是三态的（`None` = 调用方不知道）。这里必须
        # 用 `is not None` 而不是 `or`——后者会把明确的 `False`（"确认是私聊"）
        # 当成"没给"，于是去反推，等于丢掉调用方给的确切事实。
        #
        # 反推用 `is_group_window_ref()`（`kernel/visibility.py`），不是
        # `":group:" in window_ref` 的子串匹配：后者会把
        # `onebot:private:group:123` 这类私聊标识误判成群聊，然后对私聊窗口
        # 调 `get_group_msg_history`——查不到，那条消息永远停在 uncertain。
        #
        # 注意分层：这里**不 import kernel 模块**（方向是 kernel → connectors）。
        # 反推逻辑放在本连接器内联，但语义与 `is_group_window_ref` 一致：
        # 按 `:` 切段、只看第 2 段是否为 `group`、且第 3 段非空。
        if query.is_group is not None:
            is_group = query.is_group
        else:
            _parts = str(window_ref or "").split(":")
            is_group = len(_parts) >= 3 and _parts[1] == "group" and bool(_parts[2])
        action = "get_group_msg_history" if is_group else "get_friend_msg_history"
        target_key = "group_id" if is_group else "user_id"
        try:
            data = await self._call(
                action,
                {target_key: self._target_id(window_ref), "count": HISTORY_LOOKBACK},
            )
        except ChannelError as exc:
            logger.debug("历史查询失败（对账保持 uncertain）：%s", exc.message)
            return _HistoryScan(ok=False)
        if isinstance(data, dict):
            rows = data.get("messages") or data.get("message") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        if not isinstance(rows, list):
            return _HistoryScan(ok=False)
        self_id = str(self._config.self_id or "")
        hits: list[str] = []
        saw_self = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            sender = row.get("sender") or {}
            sender_id = str((sender or {}).get("user_id") or "")
            if not self_id or sender_id != self_id:
                continue
            saw_self = True
            parsed = parse_message(row.get("message"), self_id=self_id)
            if delivery_text_digest(parsed.text) != digest:
                continue
            at = _from_ts(row.get("time"))
            if at is not None and not (since <= at <= until):
                continue
            mid = row.get("message_id")
            if mid is not None:
                hits.append(str(mid))
        if len(hits) == 1:
            return _HistoryScan(ok=True, matched_id=hits[0], saw_self=True)
        if len(hits) > 1:
            return _HistoryScan(ok=True, ambiguous=True, saw_self=True)
        return _HistoryScan(ok=True, saw_self=saw_self)

    def _match_echo(self, query: ReceiptQuery) -> str | None:
        """用同进程收到的 ``message_sent`` 回显给这一段补 id。

        只在这个连接器实例**自己发过**那一段时才用（`_unidentified`），
        所以不会把别人发的消息当成自己的。
        """
        seen = self._unidentified.get(str(query.operation_id))
        if seen is None:
            return None
        window_ref, text, at = seen
        wanted = delivery_text_digest(text)
        since = at - timedelta(seconds=HISTORY_MATCH_BEFORE_SECONDS)
        until = at + timedelta(seconds=HISTORY_MATCH_AFTER_SECONDS)
        target = self._target_id(window_ref)
        hits = [
            message_id
            for observed, echo_target, message_id, echo_text in self._echoes
            if echo_target == target
            and delivery_text_digest(echo_text) == wanted
            and since <= observed <= until
        ]
        # 唯一命中才算证据（§5 P0-2 风险：同一句话连发两遍会误命中）
        return hits[0] if len(hits) == 1 else None

    def _receipt(
        self,
        query: ReceiptQuery,
        state: DeliveryState,
        *,
        platform_message_id: str | None = None,
        detail: str | None = None,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            receipt_id=new_id("rcpt"),
            operation_id=query.operation_id,
            channel=self.connector_id,
            state=state,
            platform_message_id=platform_message_id or query.platform_message_id,
            observed_at=_now(),
            detail=detail,
        )

    def platform_message_id_for_event(self, external_event_id: str) -> str | None:
        """外部事件 id → 平台 message_id（给上下文注入 `[msg_id:…]` 用）。

        只有连接器知道这个映射：OneBot 的 `external_event_id` 是
        ``f"{self_id}:{message_id}"``，别的渠道不是这个拼法。所以内核不该自己
        拆字符串，而是问连接器——不支持就返回 ``None``，上下文里就不带 id。
        """
        return self._event_message_ids.get(str(external_event_id or ""))

    def _recall_from_notice(self, payload: dict) -> RecallEvent | None:
        """``notice.group_recall`` / ``notice.friend_recall`` → `RecallEvent`。

        ## 只认这两种，其余返回 None

        OneBot V11 的 notice 有十几种（poke / group_increase /
        group_card / group_admin / friend_add…）。**只有撤回**会改变"我们手里
        的知识是否还成立"，其余一律不接。不接不等于报错：调用方会把它们归到
        ``ignored``，不刷日志。

        ## 字段映射（按 OneBot V11 规范）

        - ``group_recall``：``group_id`` / ``user_id``（原发言人）/
          ``operator_id``（谁点的撤回）/ ``message_id``
        - ``friend_recall``：``user_id``（对方）/ ``message_id``
          （规范里没有 ``operator_id``，撤回人就是对方本人）

        ## 为什么 ``self_recall`` 要单独标

        机器人自己撤回自己的消息时也会来一条 ``*_recall``（``user_id`` 等于
        ``self_id``）。它不是"对方收回了"，而是"我们自己收回了"。混在一起会
        让行为日志说成"对方撤回了一条消息"——那是错误归属。本轮对它的处置
        只是记录（原文照样不再进上下文，这一点对两边都成立）。
        """
        notice_type = str(payload.get("notice_type") or "")
        if notice_type not in {"group_recall", "friend_recall"}:
            return None
        message_id = str(payload.get("message_id") or "").strip()
        if not message_id:
            # 没有 message_id 就无从对应任何本地消息：**不猜**。
            logger.debug(
                "撤回通知缺少 message_id，按未匹配丢弃：notice=%s", notice_type
            )
            return None
        is_group = notice_type == "group_recall"
        self_id = str(payload.get("self_id") or self._config.self_id or "")
        user_id = str(payload.get("user_id") or "") or None
        operator_id = str(payload.get("operator_id") or "") or None
        group_id = str(payload.get("group_id") or "") if is_group else None
        target_id = group_id or user_id
        if is_group and not group_id:
            logger.debug("群撤回通知缺少 group_id，按未匹配丢弃：message_id=%s", message_id)
            return None

        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        sender_name = _clean_name((sender or {}).get("card")) or _clean_name(
            (sender or {}).get("nickname")
        )
        return RecallEvent(
            connector_instance=self.instance_id,
            # 去重键：同一个 message_id 只会被撤回一次；平台重投时 id 相同。
            external_event_id=f"{self.instance_id}:recall:{message_id}",
            recall_kind=RecallKind.group if is_group else RecallKind.friend,
            platform_message_id=message_id,
            operator_ref=operator_id or user_id,
            target_ref=target_id,
            window_ref=(
                window_ref_for(
                    is_group=True,
                    target_id=str(group_id),
                    instance_id=self.instance_id,
                )
                if is_group and group_id
                else (
                    window_ref_for(
                        is_group=False,
                        target_id=str(user_id),
                        instance_id=self.instance_id,
                    )
                    if user_id
                    else None
                )
            ),
            occurred_at=_from_ts(payload.get("time")),
            sender_name=sender_name,
            self_recall=bool(self_id) and (user_id == self_id or operator_id == self_id),
        )

    def _remember_message_id(self, message_id: str, external_event_id: str) -> None:
        """记下一条见过的入站 message_id（引用白名单 + 上下文 id）。"""
        if message_id not in self._known_message_set:
            self._known_message_ids.append(message_id)
            # 白名单直接由环形缓冲重建：`deque(maxlen=…)` 自动淘汰最旧的，
            # 重建一次 512 个元素的集合比维护"谁被淘汰了"的账更不容易错。
            self._known_message_set = set(self._known_message_ids)
        if len(self._event_message_ids) >= KNOWN_MESSAGE_ID_LIMIT * 4:
            # 事件 id 表只服务"当前这一轮"的上下文注入，可以有界地丢最旧的。
            for key in list(self._event_message_ids)[: KNOWN_MESSAGE_ID_LIMIT]:
                self._event_message_ids.pop(key, None)
        self._event_message_ids[str(external_event_id)] = message_id

    def _record_echo(self, payload: dict) -> None:
        """把一条 ``message_sent`` 回显记进有界环形缓冲。

        只记**目标 + 正文 + message_id + 观察时间**：对账靠这四项做
        "自己发的、同文的、时间窗内的唯一命中"。
        """
        message_id = payload.get("message_id")
        if message_id is None:
            return
        # 记账失败**不能**让这条回显变成错误：`_record_echo` 在
        # `normalize_inbound` 里、被抛 ``ignored`` 之前调用，异常一穿出去，
        # ``process_inbound_payload`` 只会把 ChannelError/ValueError 收成
        # ``accepted=False``，其它异常会一路冒到上报路由 → 平台按"投递失败"
        # 重投同一条回显。可以吞的理由：这只是一个对账用的**加速证据**
        # （`reportSelfMessage` 默认关闭），丢了它并不影响送达判定——送达仍由
        # `send()` 回执与 `get_msg` 决定。
        try:
            target = str(
                payload.get("target_id")
                or payload.get("group_id")
                or payload.get("user_id")
                or ""
            )
            parsed = parse_message(payload.get("message"), self_id=self._config.self_id)
            self._echoes.append((_now(), target, str(message_id), parsed.text))
        except Exception as exc:  # noqa: BLE001 - 辅助记账失败不改变事件语义
            logger.debug("回显记账失败（不影响对账，只少一条加速证据）：%s", type(exc).__name__)


    async def _call(self, action: str, params: dict) -> object:
        """调用 OneBot HTTP API。"""
        url = f"{self._config.api_base.rstrip('/')}/{action}"
        headers = {"Content-Type": "application/json"}
        if self._config.access_token:
            headers["Authorization"] = f"Bearer {self._config.access_token}"
        client = self._http
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        try:
            response = await client.post(url, json=params, headers=headers)
        except httpx.HTTPError as exc:
            raise ChannelError(
                ChannelErrorKind.unreachable,
                f"OneBot 不可达: {type(exc).__name__}",
                retriable=True,
            ) from exc
        finally:
            if owns:
                await client.aclose()

        # 状态码语义按规范：401/403 是鉴权问题，404 是 action 不存在，
        # 其余一律 200（业务失败靠 retcode 表达）。
        if response.status_code in {401, 403}:
            raise ChannelError(
                ChannelErrorKind.unauthorized,
                f"OneBot 鉴权失败（{response.status_code}）：检查 access_token",
                retriable=False,
            )
        if response.status_code == 404:
            raise ChannelError(
                ChannelErrorKind.unsupported,
                f"OneBot 实现不支持该 API: {action}",
                retriable=False,
            )
        if response.status_code != 200:
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"OneBot 返回 {response.status_code}",
                retriable=False,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelError(
                ChannelErrorKind.uncertain,
                "OneBot 响应不是 JSON（可能已发送但无法确认）",
            ) from exc

        status = str(body.get("status") or "")
        retcode = body.get("retcode")
        # **retcode 是权威字段**：成功必须 retcode == 0 且 status 不是 failed。
        # 原实现写的是 `status == "ok" or retcode == 0`——任一成立就返回成功，
        # 于是 `status="ok" + retcode=100`（平台明确报错）被当成发送成功，
        # 失败被记成"已送达"，既不会重试也不会告警。
        # 两个字段冲突时按失败处理（宁可多一次重投，也不能谎报送达）。
        if retcode == 0 and status != "failed":
            return body.get("data")
        if status == "async" or retcode == 1:
            # 异步提交：结果未知，不能算成功也不能算失败
            raise ChannelError(
                ChannelErrorKind.uncertain, detail_or("异步处理，结果未知", body)
            )
        # ---- NapCat 的实际错误码（`OneBotAction.ts`，见调研 §4.5）----
        #
        # | retcode | 含义                          | 分类      |
        # |---------|-------------------------------|-----------|
        # | 400/1400| 参数 schema 校验失败（没发）    | rejected  |
        # | 1404    | 不支持的 API                   | unsupported |
        # | 1200/200| **动作内部抛异常**（可能是发送超时、也可能"消息体无法解析"）| uncertain |
        #
        # 关键：**1200/200 把"发送前失败"和"发送中超时"混成同一个码**，
        # 只能保守按"可能已发出"处理——记 uncertain 进对账，**绝不重发**
        # （重发就是重复消息）。原实现把它们都归成 ``rejected``，
        # 而 ``rejected`` 会触发"去掉装饰重发"，等于把超时当"肯定没发出"。
        #
        # 另：全仓检索 NapCat 的 ``OB11Response.error(...)`` 只出
        # 200/400/1200/1400/1404，**没有 100**。所以线上若真见到
        # ``retcode=100``，那一定来自别的实现（如 Lagrange），按兜底的
        # ``rejected`` 处理是安全的；但别拿它当"引用失效"的证据——NapCat 对
        # 无效 reply 是**静默丢掉该段、正文照发**（§4.4），根本不会报错。
        if retcode in {1200, 200}:
            raise ChannelError(
                ChannelErrorKind.uncertain,
                detail_or(f"OneBot 动作内部异常 retcode={retcode}（可能已发送）", body),
            )
        if retcode == 1404:
            raise ChannelError(
                ChannelErrorKind.unsupported,
                detail_or("OneBot 实现不支持该 API retcode=1404", body),
                retriable=False,
            )
        raise ChannelError(
            ChannelErrorKind.rejected, detail_or(f"OneBot 调用失败 retcode={retcode}", body)
        )

    def _target_id(self, window_ref: str) -> str:
        """从窗口标识取出目标 id（群号或 QQ 号）。"""
        # window_ref 形如 "<instance>:group:<gid>" / "<instance>:private:<uid>"
        parts = window_ref.rsplit(":", 1)
        return parts[-1] if len(parts) == 2 else window_ref

    # ------------------------------------------------------------ 入站

    async def verify_inbound(self, headers: dict[str, str], body: bytes) -> bool:
        """校验上报来源。**没配 secret 就一律拒绝**（fail-closed）。

        配了 secret 就必须验 HMAC-SHA1；配了 self_id 就要求
        ``X-Self-ID`` 与之一致（防止别的实例误配到同一入口）。

        ## 为什么没配 secret 时不再放行

        原实现按 OneBot 规范允许"未加密上报"（只要求带 ``X-Self-ID``），
        理由是"依赖网络隔离"。这个前提在默认部署下**不成立**：

        - ``POST /v1/onebot/events`` 这条路由**没有任何 auth 依赖**
          （见 ``src/ling/api/routes/onebot.py``），端口 6520 默认对外发布；
        - ``deploy/env.example`` 里 ``LING_ONEBOT_SECRET=`` 默认为空；
        - 正常使用必须配 ``LING_ACCESS_LIST``，而名单里就是 QQ 号。

        于是任何能访问 6520 的人只要伪造 ``X-Self-ID`` 和
        ``user_id=<白名单里的 QQ 号>``，就能以该用户身份对话、写记忆、
        让她向真实好友发言。这是完整的账号接管，不是理论风险。

        因此：secret 是这条入口的**必要条件**。没配 = 全部 403，
        启动时另打一条 ERROR 日志说明怎么配（见 ``ling/app.py``）。
        """
        secret = self._config.secret
        if not secret:
            # 没有密钥就没有任何可信来源的判据。X-Self-ID 是请求方自己填的，
            # 不构成凭据。
            return False

        normalized = {k.lower(): v for k, v in headers.items()}
        self_id = normalized.get("x-self-id")
        if self._config.self_id:
            if not self_id or str(self_id).strip() != str(self._config.self_id):
                return False
        elif not self_id:
            return False

        signature = normalized.get("x-signature") or ""
        if not signature.startswith("sha1="):
            return False
        expected = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha1
        ).hexdigest()
        return hmac.compare_digest(signature[len("sha1=") :], expected)

    def normalize_inbound(self, payload: dict) -> InteractionEnvelope | RecallEvent:
        """把 OneBot 事件翻译成交互信封；**撤回**除外（返回 `RecallEvent`）。

        只翻译，不改写语义：文本原样进 ``parts``，
        @ 与引用作为结构化字段保留，供决策层判断"是不是在跟我说话"。
        """
        post_type = str(payload.get("post_type") or "")
        if post_type == "meta_event":
            # 心跳和生命周期**静默丢弃**，不记日志。
            #
            # 心跳每 30 秒一条，两天就能把 500 条内存缓冲里的有效日志全挤出去；
            # 而它证明的是"连接还活着"，这件事连接层已经在做（WS 断开、异常
            # 结束、鉴权失败都有 WARNING）。用 ``ignored`` 而不是 ``unsupported``：
            # 后者会让 inbound_intake 记一条"忽略事件"，那正是刷屏的来源。
            raise ChannelError(
                ChannelErrorKind.ignored,
                f"meta_event:{payload.get('meta_event_type') or ''}",
            )
        if post_type == "message_sent":
            # **机器人自己发出去的消息的回显**（NapCat/LLOneBot 的
            # ``reportSelfMessage``）。按设计没有要处理的东西：
            #
            # 正文、收件人、平台消息 id 都在 ``send()`` 的返回值里，
            # 再把它当成入站消息送进对话链路，等于让她对着自己说话。
            # 但它**每发一条就跟着来一条**，按"未处理事件"记一条 INFO
            # （浏览器行为日志里显示成「忽略事件: 暂不处理 OneBot 事件类型:
            # message_sent」）会把真正有用的日志挤出去。
            #
            # 所以归到 ``ignored``：inbound_intake 对它**不记行为日志**。
            # 想确认"这条到底发出去没有"时看下面这行 DEBUG——里面的
            # ``message_id`` 就是 ``send()`` 回执里的 ``platform_message_id``，
            # 两条日志对得上就说明平台确实收下了。
            #
            # 这里**不补写回执**：``DeliveryReceiptRow`` 必须挂在
            # ``operation_id`` 上，而回显事件里没有这个值；拿它凑一条
            # "已送达"只能证明平台存在这条消息，属于伪造送达证据。
            #
            # 但**要把 id 记下来**（调研 §4.3 / §5 P0-2）：回显里的
            # ``message_id`` 与 ``send_*_msg`` 回执的 ``message_id`` 是
            # 同一个 id 空间，所以它能给"发出去了但平台没给 id"的那一段补 id。
            # 只在同进程有效（worker 是另一个进程），所以它只是对账的
            # **加速证据**，不是必需依赖——`reportSelfMessage` 默认是关的。
            self._record_echo(payload)
            message_id = payload.get("message_id")
            logger.debug(
                "出站回显（自己发出的消息，按设计忽略）：message_id=%s target=%s",
                message_id,
                payload.get("target_id"),
            )
            raise ChannelError(
                ChannelErrorKind.ignored, f"message_sent:{message_id or ''}"
            )
        if post_type == "notice":
            # **撤回**是唯一需要内核处理的 notice：对方把他的那条收回了，
            # 我们不能再把原文喂给模型。其余 notice（戳一戳、入群、群名片
            # 更新…）仍然不进入任何链路——它们没有需要撤回的知识。
            recall = self._recall_from_notice(payload)
            if recall is not None:
                return recall  # 类型故意与信封不同：它不是一条消息

            notice_type = str(payload.get("notice_type") or "")
            if notice_type in _IGNORED_NOTICE_TYPES:
                # 已知且**按设计**不进任何链路：戳一戳、入群、群名片…
                # 它们在活跃群里很频繁，逐条记日志会把真正要看的东西挤出去。
                raise ChannelError(
                    ChannelErrorKind.ignored,
                    f"notice:{notice_type}:{payload.get('sub_type') or ''}",
                )
            # 不认识的 notice 类型仍然记一条：那是"出现了新东西"，
            # 与"已知的不重要"不是一回事（把两者都静默掉，将来平台加了
            # 新的撤回类事件我们会毫无察觉）。
            raise ChannelError(
                ChannelErrorKind.unsupported,
                f"暂不处理 OneBot notice 类型: {notice_type or '（空）'}",
            )
        if post_type != "message":
            # request（加好友/入群邀请）不进入对话链路，也不该被当成
            # "不支持"打日志——那是设计如此，不是缺口。
            raise ChannelError(
                ChannelErrorKind.unsupported, f"暂不处理 OneBot 事件类型: {post_type}"
            )

        message_type = str(payload.get("message_type") or "")
        is_group = message_type == "group"
        user_id = str(payload.get("user_id") or "")
        group_id = str(payload.get("group_id") or "")
        if not user_id:
            raise ChannelError(ChannelErrorKind.rejected, "事件缺少 user_id")
        if is_group and not group_id:
            raise ChannelError(ChannelErrorKind.rejected, "群消息缺少 group_id")

        parsed: ParsedMessage = parse_message(
            payload.get("message"), self_id=self._config.self_id
        )
        # 字符串格式时 raw_message 更完整（含 CQ 码）；
        # 数组格式时 message 已经是结构化，直接用解析结果。
        if not parsed.text.strip() and isinstance(payload.get("raw_message"), str):
            parsed = parse_message(payload["raw_message"], self_id=self._config.self_id)

        message_id = str(payload.get("message_id") or "")
        self_id = str(payload.get("self_id") or self._config.self_id or "")
        # 去重键必须包含 self_id 与 message_id：同一 QQ 号下 message_id 唯一，
        # 但多实例共库时只靠 message_id 会互相覆盖。
        external_event_id = f"{self_id}:{message_id}" if message_id else new_id("obe")
        # 这条入站消息的**真实平台 id**有两个用处：
        # 1. 出站引用白名单（引用一个没见过的 id，NapCat 只会静默丢段，§4.4）；
        # 2. 上下文里给模型看 `[msg_id:…]`（§5 P0-4）。
        if message_id:
            self._remember_message_id(message_id, external_event_id)

        occurred = payload.get("time")
        # 本次称呼（card 优先）与昵称（人级）分开带走：显示用前者，
        # 落库更新 `subjects.display_name` 只用后者。
        sender_name, sender_nickname = _sender_fields(payload)
        return InteractionEnvelope(
            connector_instance=self.instance_id,
            external_event_id=external_event_id,
            account_ref=user_id,
            sender_name=sender_name,
            sender_nickname=sender_nickname,
            window_ref=window_ref_for(
                is_group=is_group,
                target_id=group_id if is_group else user_id,
                instance_id=self.instance_id,
            ),
            occurred_at=_from_ts(occurred),
            # **到达时间**与 `occurred_at`（平台事件时间）是两件事：
            # 前者是我们收到它的时刻，后者是平台说它发生的时刻。
            # 不记录到达时间就没法算"这条消息在队列里等了多久"。
            received_at=datetime.now(UTC),
            modality=parsed.modality(),
            # **永远带一个 text 段，哪怕是空串。**
            # 契约要求 `parts` 至少一个片段；而"只有 @ 没有正文"是真实场景
            # （QQ 里先 @ 再发正文很常见）。原来这里是 `if parsed.text else []`，
            # 于是空正文的 @ 直接被契约校验拒掉，连 bridge 都到不了——
            # 用户看到的是"@ 了她却毫无反应"。
            parts=[InputPart(modality=Modality.text, text=parsed.text)],
            reply_to=parsed.reply_to_message_id,
            media_refs=parsed.media_refs,
            mentions_actor=parsed.mentions_self,
        )

    async def fetch_media(self, media_ref: str) -> tuple[bytes, str]:
        """下载媒体。OneBot 的图片/语音给的是 http(s) url 或本地 file 路径。

        ## 为什么要有域名白名单与大小上限

        原实现是「任意 http(s) URL + ``response.content`` 整体入内存」：
        既能被拿来探测内网（SSRF），也能被一个超大响应把进程内存吃光。
        媒体 URL 来自**平台事件正文**，也就是外部可控输入，不能当可信。

        白名单匹配用 ``host == h or host.endswith("." + h)``，**不是裸
        ``endswith``**——``evilqpic.cn`` 以 ``qpic.cn`` 结尾，裸匹配会放行。

        **端口也要校验**（``connectors/media_url.py``）：白名单里全是图床域名，
        腾讯图床不走非标准端口。只校验 host 时 ``gchat.qpic.cn:8443`` 会被放行，
        一台把该域名反代到别处的机器就能借非标准端口把请求引到内网。
        """
        from urllib.parse import urlparse

        from connectors.media_url import host_allowed, port_is_standard

        parsed = urlparse(media_ref)
        if parsed.scheme not in {"http", "https"}:
            # file:// 或本地路径：规范允许 OneBot 直接给本地文件路径，
            # 但那要求 OneBot 与 ling 同机。这里明确不支持，避免误读别的机器的路径。
            raise ChannelError(
                ChannelErrorKind.unsupported,
                "仅支持 http(s) 媒体地址；本地路径需 OneBot 与本服务同机",
            )
        host = (parsed.hostname or "").lower()
        if not host_allowed(host, self._config.media_allowed_hosts):
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"OneBot 媒体域名不在白名单内: {host or '(空)'}",
            )
        if not port_is_standard(parsed):
            raise ChannelError(
                ChannelErrorKind.rejected,
                f"OneBot 媒体端口不是该协议的默认端口: {parsed.netloc}",
            )

        limit = self._config.media_max_bytes
        client = self._http
        owns = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        try:
            # **流式读**，边读边计数：``response.content`` 会先把整个响应体
            # 读进内存，等发现超限时内存已经吃掉了。
            #
            # `follow_redirects=False`：白名单只校验了**第一跳**。允许跟随
            # 重定向的话，一个白名单域名只要 302 到 127.0.0.1 或内网地址，
            # 白名单就形同虚设（SSRF 的常见绕过）。httpx 默认不跟随，
            # 这里显式写出来，避免调用方注入的 client 打开了跟随。
            async with client.stream("GET", media_ref, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ChannelError(
                        ChannelErrorKind.rejected,
                        f"拉取 OneBot 媒体 HTTP {response.status_code}",
                    )
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > limit:
                            raise ChannelError(
                                ChannelErrorKind.rejected,
                                f"媒体声明大小 {declared} 字节，超过上限 {limit}",
                            )
                    except ValueError:
                        # 头部不是数字：不因此放行，交给下面的流式计数兜底
                        pass
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        raise ChannelError(
                            ChannelErrorKind.rejected,
                            f"媒体超过上限 {limit} 字节，已中断下载",
                        )
                    chunks.append(chunk)
                mime = response.headers.get(
                    "content-type", "application/octet-stream"
                )
                return b"".join(chunks), mime
        except httpx.HTTPError as exc:
            raise ChannelError(
                ChannelErrorKind.unreachable, f"媒体下载失败: {exc}", retriable=True
            ) from exc
        finally:
            if owns:
                await client.aclose()

    def render_output(self, text: str, *, modalities: list[Modality]) -> list[str]:
        """按 QQ 文本上限切分。"""
        limit = self._config.max_text_length
        if len(text) <= limit:
            return [text]
        return [text[i : i + limit] for i in range(0, len(text), limit)]

    def capabilities(self) -> CapabilityDescriptor:
        """渠道能力声明。

        ## 语音能力必须**实测**过才声明

        ``supports_media`` 是一个笼统的 bit，而 ``media_supported()`` 在
        ``supported_media is None`` 时**只把 audio 判成不支持**（见
        ``contracts/v1.py:808``）。所以想让语音通过投递层的检查，必须显式
        在 ``supported_media`` 里列出 ``Modality.audio``。

        但**不能无条件列**：发语音需要 ffmpeg（解码/重采样）+ pysilk
        （编码），缺一样就发不出去。无条件声明等于让投递层放行、然后连接器
        报错——用户看到的是"她试着说话但失败了"，而不是"这个渠道不支持语音"。
        这正是 §17.6 点名的"用总 supports_media 宣称已支持语音"。

        所以：**readiness 通过才声明 audio**，并把原因记在
        :meth:`voice_readiness` 里供运维查"为什么没有语音"。
        """
        ready, _reason = self.voice_readiness()
        supported_media = [Modality.image]
        if ready:
            supported_media.append(Modality.audio)
        return CapabilityDescriptor(
            capability_id=self.connector_id,
            version="11",
            modalities=[Modality.text],
            supports_private=True,
            supports_group=self._config.supports_group,
            supports_proactive=self._config.supports_proactive,
            # 细分的媒体能力：让"能发图"与"能发语音"分开。audio 只在
            # SILK 链路实测就绪时出现，见上面的 docstring。
            supported_media=supported_media,
            supports_media=self._config.supports_media,
            # OneBot V11 有 ``delete_msg``（NapCat/Lagrange/go-cqhttp 都实现）。
            # 具体能不能撤、撤多久以内，由平台在调用时回答——这里只声明
            # "我们会去试"，不声明"一定能成功"。
            supports_recall=True,
            # OneBot V11 的 ``send_poke``（NapCat / Lagrange 都实现）。
            # 见下面 ``send`` 里对 ``poke_targets`` 的处理——**真的会发**，
            # 所以这里如实声明 True，提示词才会把 ``[poke:…]`` 列给她。
            supports_poke=True,
            max_text_length=self._config.max_text_length,
            receipt_reliability="best_effort",
            enabled=bool(self._config.api_base),
        )

    def voice_readiness(self) -> tuple[bool, str]:
        """能不能**真的**发一条语音条；返回 (可用, 不可用原因)。

        ## 这里查什么

        语音链路要三样东西同时就位：``ffmpeg``（解码+重采样）、``pysilk``
        （编码）、以及一个可用的 transport 配置。少任何一样，发出去就会
        在连接器这一层失败——所以能力声明必须以它为准。

        ## 为什么是"查依赖"而不是"编一个试试"

        真正的自检（编一个 100ms 正弦）在 ``silk.SilkCodec.selftest()`` 里，
        它要跑一次完整的解码+编码，代价是几十毫秒。``capabilities()`` 会被
        投递层**每条消息**调用一次，不能在那里跑真实编码。

        所以分工是：``capabilities()`` 用这里（廉价、缓存）；部署自检/运维
        命令用 ``selftest()``（真实、贵）。§17.3 要求"真实编码一个 fixture，
        不能仅 which ffmpeg"——那条要求对应的是自检入口，不是每次能力查询。

        ## 返回的原因要能指导操作

        运维要能回答"为什么这个渠道没有语音"。所以原因里要指出缺什么、
        怎么装，而不是一句"语音不可用"。
        """
        now = self._health_clock()
        cached = self._voice_ready_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        result = self._probe_voice_readiness()
        self._voice_ready_cache = (now + self.voice_readiness_cache_seconds, result)
        if not result[0]:
            logger.info("语音能力未就绪（不声明 audio）：%s", result[1])
        return result

    def refresh_voice_readiness(self) -> tuple[bool, str]:
        """丢掉缓存重新探测。

        **给运维用**：依赖是后装的（``apt-get install ffmpeg``、
        ``pip install 'ling[speech-codecs]'``）。不提供这个入口，运维装完之后
        只能重启进程才能拿到语音能力，而"装好了还是不能发"是最难查的一类问题。
        """
        self._voice_ready_cache = None
        return self.voice_readiness()

    def _probe_voice_readiness(self) -> tuple[bool, str]:
        """实际探测（不含缓存）。"""
        if not self._config.supports_media:
            return False, "配置里 supports_media=False（这个渠道没有开通媒体发送）"
        codec = default_codec()
        ok, reason = codec.readiness()
        if not ok:
            return False, reason
        return True, ""

    async def health(self) -> tuple[bool, str]:
        """探活：``get_login_info`` 是最轻的可用性探测。

        **结果短缓存（默认 10 秒）**：这条探活是**外呼** NapCat 的。之前端点
        既没鉴权也没缓存，任何能访问 API 端口的人都能拿它当放大器打 NapCat
        （一台个人机器上的 NapCat 撑不住这种"健康检查"）。缓存把上限压到
        "每 10 秒一次"，鉴权在路由层另算。

        失败结果同样缓存：NapCat 挂掉时反复重试只会更糟。代价是恢复最多晚
        10 秒被发现，对运维探活可以接受。
        """
        now = self._health_clock()
        cached = self._health_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        result = await self._probe_login()
        self._health_cache = (now + self.health_cache_seconds, result)
        return result

    async def fetch_group_name(self, group_id: str) -> str | None:
        """采集群名（``get_group_info``）。**拿不到就返回 ``None``，绝不抛**。

        ## 为什么采集放在连接器里

        群名是平台侧的事实，只有连接器知道怎么问（OneBot 的
        ``get_group_info``；换成 QQ 官方机器人就是另一个动作）。上层的
        对话/接口层只该问"这个群叫什么"，不该认识 ``group_id`` 以外的协议细节。

        ## 为什么带缓存、还给失败也缓存

        调用点是**读侧**（会话列表），没有缓存的话每开一次页面就打一遍
        NapCat；群多的时候就是一阵外呼风暴。成功缓存久一点（群名很少改），
        失败缓存短一点（NapCat 刚恢复时别让界面长时间空着）。

        ## 为什么失败要吞掉

        群名只是标题，不是对话必需信息。``get_group_info`` 在不少 OneBot
        实现里根本没实现（404 → ``unsupported``），也可能因为断连超时。
        为了一个标题让会话列表 500，是拿"次要信息"换"主功能"。
        调用方拿到 ``None`` 时前端回退成「群 {id}」。
        """
        gid = str(group_id or "").strip()
        if not gid:
            return None
        now = time.monotonic()
        cached = self._group_name_cache.get(gid)
        if cached is not None:
            stamped, cached_name = cached
            ttl = self.group_name_ttl if cached_name else self.group_name_miss_ttl
            if now - stamped < ttl:
                return cached_name

        name: str | None = None
        try:
            data = await asyncio.wait_for(
                self._call("get_group_info", {"group_id": gid}),
                timeout=self.group_name_timeout,
            )
            if isinstance(data, dict):
                raw = data.get("group_name")
                if isinstance(raw, str) and raw.strip():
                    # 截到与 windows.title / subjects.display_name 同级的长度：
                    # 契约里这不是长文本字段，超长只会在界面上被裁一次。
                    name = raw.strip()[:128]
        except (ChannelError, TimeoutError) as exc:
            # 超时也走这里：``asyncio.wait_for`` 抛的是内建 TimeoutError。
            logger.debug("采集群名失败（回退为无名）：group=%s %s", gid, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - 采集是尽力而为，不能影响读侧
            logger.debug("采集群名异常（回退为无名）：group=%s %s", gid, type(exc).__name__)

        self._group_name_cache[gid] = (now, name)
        self._trim_group_name_cache()
        return name

    async def fetch_friend_names(
        self,
    ) -> dict[str, tuple[str | None, str | None]]:
        """好友列表 → ``{user_id: (remark, nickname)}``。**拿不到返回空字典，绝不抛**。

        ## 为什么必须调 `get_friend_list`

        会话标题要显示"好友备注"。但 OneBot V11 里**备注不是消息事件的一部分**：
        `sender` 只带 `nickname` / `card`；`get_stranger_info` 也只给昵称。
        `remark`（备注）是**好友关系**上的字段，只有 `get_friend_list` 返回它。

        所以这一项只能整表拉一次——没有"查单个好友备注"的接口。

        ## 缓存

        会话列表是**读侧**、每开一次页面都会问。不缓存就是每次开页面都让
        NapCat 吐一遍全量好友列表。成功的 10 分钟内复用，失败 1 分钟内静默
        （NapCat 刚重启时别让它挨一轮风暴）。

        ## 失败为什么不抛

        标题是次要信息。`get_friend_list` 在某些 OneBot 实现里没实现
        （404 → ``unsupported``），也可能超时。为了一个标题让会话列表 500，
        是拿主功能换次要信息。调用方拿到空字典就按"没有备注/昵称"处理，
        前端回退成「私聊 {id}」。
        """
        now = time.monotonic()
        cached = self._friend_names_cache
        if cached is not None:
            stamped, cached_names = cached
            ttl = (
                self.friend_names_ttl
                if cached_names
                else self.friend_names_miss_ttl
            )
            if now - stamped < ttl:
                return cached_names

        names: dict[str, tuple[str | None, str | None]] = {}
        try:
            data = await asyncio.wait_for(
                self._call("get_friend_list", {}), timeout=self.friend_names_timeout
            )
            rows = data if isinstance(data, list) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                user_id = row.get("user_id")
                if user_id is None:
                    continue
                remark = _clean_name(row.get("remark"))
                nickname = _clean_name(row.get("nickname"))
                if remark or nickname:
                    names[str(user_id)] = (remark, nickname)
        except (ChannelError, TimeoutError) as exc:
            logger.debug(
                "采集好友列表失败（标题回退为无名）：%s", type(exc).__name__
            )
        except Exception as exc:  # noqa: BLE001 - 采集是尽力而为，不能影响读侧
            logger.debug("采集好友列表异常（回退为无名）：%s", type(exc).__name__)

        self._friend_names_cache = (now, names)
        return names

    def _trim_group_name_cache(self) -> None:
        """缓存只保留最近的若干群，防止长跑进程里字典无限长。"""
        if len(self._group_name_cache) <= self.group_name_cache_max:
            return
        # 按时间戳丢最旧的：群名的价值随"最近是否活跃"衰减，不需要 LRU。
        for gid, _ in sorted(self._group_name_cache.items(), key=lambda kv: kv[1][0])[
            : len(self._group_name_cache) - self.group_name_cache_max
        ]:
            self._group_name_cache.pop(gid, None)

    async def _probe_login(self) -> tuple[bool, str]:
        try:
            data = await self._call("get_login_info", {})
        except ChannelError as exc:
            return False, f"{exc.kind.value}: {exc.message}"
        if isinstance(data, dict):
            return True, f"ok self_id={data.get('user_id')}"
        return True, "ok"


def _now():  # noqa: ANN202
    from datetime import UTC, datetime

    return datetime.now(UTC)


def _aware(value):  # noqa: ANN001, ANN202
    """把可能 naive 的时间统一成带时区的 UTC。拿不到就返回 None。"""
    from datetime import UTC, datetime

    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _looks_recall_expired(message: str) -> bool:
    """平台是否在说"超时/没权限撤回"。

    关键词取自 NapCat / go-cqhttp 的常见文案（"timeout" / "超过" /
    "权限" / "not allowed" / "expired"）。**只在撤回路径上用**：这里宁可
    把一次失败归成 ``expired``（用户看到"超时了撤不回"），也不要归成
    ``uncertain`` 让用户反复重试——重试一次撤回不可能成功。
    """
    lowered = (message or "").lower()
    return any(
        token in lowered
        for token in (
            "timeout",
            "time out",
            "超时",
            "超过",
            "过期",
            "expired",
            "not allowed",
            "权限",
        )
    )


def _looks_missing(message: str) -> bool:
    """平台的话里有没有"这条消息不存在"。"""
    lowered = (message or "").lower()
    return any(hint in lowered for hint in _MISSING_HINTS)


class _HistoryScan:
    """一次历史内容比对的结果。

    ``ok`` 只是"查询成功"，**不等于**"没有命中"：
    - ``matched_id`` 唯一命中 → 可以补 id、记 delivered
    - ``ambiguous`` 命中多条 → 只能保持 uncertain（同一句话连发两遍）
    - ``ok=False`` → 查询不可用，**不能**当成否证
    - ``saw_self=False`` → 这份历史里**一条自己的消息都没有**。调研 §7.6 没
      证实"平台历史是否包含机器人自己发的消息"，所以"没找到同文消息"在这种
      情况下不构成否证（否则平台行为差异会被误判成"没送达"）。
    """

    __slots__ = ("ambiguous", "matched_id", "ok", "saw_self")

    def __init__(
        self,
        *,
        ok: bool,
        matched_id: str | None = None,
        ambiguous: bool = False,
        saw_self: bool = False,
    ) -> None:
        self.ok = ok
        self.matched_id = matched_id
        self.ambiguous = ambiguous
        self.saw_self = saw_self


def _sender_fields(payload: dict) -> tuple[str | None, str | None]:
    """返回 (本次称呼, 昵称)。

    - 本次称呼 = ``card or nickname``：**这个群里此刻该叫她/他什么**。
      群名片是群级字段，私聊事件里根本不存在，所以私聊退回昵称是对的。
    - 昵称 = 只有 ``nickname``：**这个 QQ 号自己的名字**（跨群）。
      它是"人级"字段，落库更新 ``subjects.display_name`` 只用它——用群名片
      写人级列会被"这个人今天在 A 群改了个名片"带着抖（见 docs/GROUP-CARD-RESEARCH.md）。

    两者**分开传**，因为用途不同：显示用前者（改名下一句就生效），
    落库用后者（跨群稳定）。合成一个值就没法在落库时选择"只用昵称"。

    拿不到就是 ``None``——不要编 "N/A" 之类的占位符（AstrBot 有个 ``"N/A"``
    字面量的坑，照抄会把占位符写进库）。
    """
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return None, None
    card = _clean_name(sender.get("card"))
    nickname = _clean_name(sender.get("nickname"))
    return (card or nickname), nickname


def _clean_name(value: object) -> str | None:
    """名字清洗：空白即"没有"，长度对齐 ``subjects.display_name`` 的 128。"""
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text[:128]
    return None


def _from_ts(value: object):  # noqa: ANN202
    from datetime import UTC, datetime

    try:
        # 平台字段可能是 int/str/float，也可能是 None。
        # 显式走 float 再转 timestamp：int() 对 None 抛 TypeError 是对的，
        # 但类型检查器不接受 object 直接进 int()。
        return datetime.fromtimestamp(float(value), tz=UTC)  # type: ignore[arg-type]
    except (TypeError, ValueError, OSError):
        return None


def detail_or(default: str, body: dict) -> str:
    for key in ("wording", "message", "msg"):
        value = body.get(key)
        if value:
            return f"{default}（{value}）"
    return default


__all__ = ["OneBotV11Connector"]
