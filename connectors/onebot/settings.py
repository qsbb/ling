"""OneBot V11 连接配置。

对应 OneBot 实现的配置项：HTTP API 服务地址与访问令牌，
以及反向上报（HTTP POST）使用的签名密钥。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# OneBot 媒体（图片/语音）的默认域名白名单：QQ 图床。
#
# 匹配规则是 ``host == h or host.endswith("." + h)``（见
# ``connectors/media_url.py``），不能用裸 ``endswith``——否则
# ``evilqpic.cn`` 会因为以 ``qpic.cn`` 结尾而被放行。端口另有一条约束：
# 只接受 http=80 / https=443，``gchat.qpic.cn:8443`` 这类非标准端口会被拒。
#
# ## ``qq.com`` 这一条为什么留着（以及它的风险）
#
# 它**不是**图床域名，放行它等于放行**任意** ``*.qq.com`` 子域
# （``h5.qzone.qq.com``、``qun.qq.com``、``ssl.ptlogin2.qq.com`` …），
# 比"只放确定是媒体服务的域名"宽。保留的理由是**语音与视频**：
# NapCat 的图片 host 是硬编码的（``gchat.qpic.cn`` /
# ``multimedia.nt.qq.com.cn``，见 napcat-core/types/constant.ts），
# 但语音/视频的下载地址是腾讯在 OIDB 响应里**动态返回**的域名
# （``FileApi.getPttUrl`` / ``getVideoUrlPacket`` 的结果直接塞进
# ``url``），我们无法预先枚举。收窄成固定几个 host 会让语音/视频
# 静默下载失败（表现为"发了语音，她没反应"，日志里只有一条白名单拒绝）。
#
# 风险边界：目标仍必须是 ``qq.com`` 名下的腾讯服务，攻击者不能自己
# 注册子域；GET-only、不跟随重定向、限大小、限端口。真要收紧，用
# ``LING_ONEBOT_MEDIA_HOSTS`` 显式覆盖（例如只写
# ``qpic.cn,gchat.qpic.cn,multimedia.nt.qq.com.cn,gxh.vip.qq.com``），
# 代价是语音/视频可能收不到。
DEFAULT_MEDIA_HOSTS: frozenset[str] = frozenset(
    {
        "qpic.cn",  # 微信/QQ 通用图床（gchat.qpic.cn、ugc.qpic.cn 等）
        "gchat.qpic.cn",  # 消息图片（NapCat 硬编码的 IMAGE_HTTP_HOST）
        "multimedia.nt.qq.com.cn",  # NT 版消息附件（IMAGE_HTTP_HOST_NT）
        # 商城表情：NapCat 拼的是 https://gxh.vip.qq.com/...（msg.ts:266）
        "gxh.vip.qq.com",
        # 语音/视频：域名由腾讯动态返回，无法枚举（见上面的说明）。
        "qq.com",
    }
)


@dataclass(frozen=True)
class OneBotConfig:
    """一个 OneBot V11 实现实例的连接参数。

    一个实例对应一个 QQ 号（``self_id``）。多开多个 QQ 就配多个实例，
    各自的 ``instance_id`` 不同，账号绑定按 ``instance_id`` 隔离
    （避免两个机器人共用 QQ 号时身份混淆）。
    """

    # OneBot 实现的 HTTP API 基址，如 http://127.0.0.1:5700
    # 对应 OneBot 配置里的 http.host / http.port
    api_base: str = "http://127.0.0.1:5700"
    # 访问令牌。OneBot 侧配了 access token 时这里必须一致，
    # 否则 API 调用返回 401（未提供）或 403（不匹配）。
    access_token: str | None = None
    # 反向上报签名密钥。配了 secret 后 OneBot 会在每次上报带
    # X-Signature: sha1=<hmac_sha1(secret, body)>，必须校验。
    #
    # **这是入站的必要条件**：为空时 ``verify_inbound`` 一律返回 False。
    # 原实现为空时放行（"依赖网络隔离"），但 ``/v1/onebot/events`` 没有
    # 任何 auth 依赖且端口默认对外发布，等于谁都能冒充白名单里的 QQ 号。
    secret: str | None = None
    # 机器人自己的 QQ 号。用于校验上报的 X-Self-ID 与事件 self_id 一致，
    # 不一致说明上报来自别的实例，应拒绝。
    self_id: str | None = None
    # 实例标识，进入 ActorAccount.connector_instance，做账号命名空间隔离
    instance_id: str = "onebot-default"
    # 单次 API 调用超时（秒）。QQ 平台偶发慢响应，不宜设太短。
    timeout_seconds: float = 15.0
    # 渠道能力声明。OneBot V11 的群聊是常态能力；
    # 主动推送取决于所用实现与账号权限，默认关闭，需探测后打开。
    supports_group: bool = True
    supports_proactive: bool = False
    supports_media: bool = True
    # QQ 单条消息文本上限（实践中约 4500，留余量）
    max_text_length: int = 4000
    # 反向 WS **服务端**监听的地址。默认只听本机：NapCat 通常在同机/同网段，
    # 需要对外时显式改成 0.0.0.0（改之前先确认 token 已配，否则启动会直接失败）。
    ws_host: str = "127.0.0.1"
    # 下载媒体时允许的域名（主机名，不含端口）。见 ``DEFAULT_MEDIA_HOSTS``。
    media_allowed_hosts: frozenset[str] = field(
        default_factory=lambda: DEFAULT_MEDIA_HOSTS
    )
    # 单次媒体下载上限（字节）。超过就中断下载，**不把整个响应读进内存**。
    media_max_bytes: int = 32 * 1024 * 1024


DEFAULT_CONFIG = OneBotConfig()

__all__ = ["DEFAULT_CONFIG", "DEFAULT_MEDIA_HOSTS", "OneBotConfig"]

