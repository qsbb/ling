"""配置与引导设置。

环境变量只负责**启动必需项**（数据库、管理员引导、根加密密钥、监听端口）；
日常模型设置由 ConfigService 在数据库中维护，环境变量不是第二套覆盖层。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """进程级启动配置（不含日常业务设置）。"""

    # 运行环境
    env: str = field(default_factory=lambda: os.environ.get("LING_ENV", "dev"))
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("LING_DATA_DIR", REPO_ROOT / ".data"))
    )
    host: str = field(default_factory=lambda: os.environ.get("LING_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("LING_PORT", 8080))

    # 数据库：默认本地 sqlite 便于零依赖启动，生产必须给 PostgreSQL URL
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "LING_DATABASE_URL", "sqlite+aiosqlite:///./.data/ling.db"
        )
    )
    db_require_postgres: bool = field(default_factory=lambda: _bool("LING_REQUIRE_POSTGRES", False))
    # 测试隔离：把连接固定到一个 schema（仅 PostgreSQL 生效）
    db_search_path: str | None = field(
        default_factory=lambda: os.environ.get("LING_DB_SEARCH_PATH") or None
    )

    # 引导管理员
    bootstrap_admin_user: str = field(
        default_factory=lambda: os.environ.get("LING_BOOTSTRAP_ADMIN", "ling")
    )
    bootstrap_admin_password: str = field(
        default_factory=lambda: os.environ.get("LING_BOOTSTRAP_ADMIN_PASSWORD", "")
    )

    # 根加密密钥（用于加密供应商密钥）。缺省时使用数据目录下的本地文件，
    # 生产必须通过 Docker secret / 环境注入。
    root_key: str = field(default_factory=lambda: os.environ.get("LING_ROOT_KEY", ""))

    # 会话 cookie
    cookie_name: str = field(default_factory=lambda: os.environ.get("LING_COOKIE_NAME", "ling_sid"))
    session_ttl_seconds: int = field(default_factory=lambda: _int("LING_SESSION_TTL", 60 * 60 * 24 * 14))
    cookie_secure: bool = field(default_factory=lambda: _bool("LING_COOKIE_SECURE", False))
    # 告警推送目标（通用 webhook，POST JSON）。留空则不推送，只保留查询端点。
    alert_webhook_url: str = field(
        default_factory=lambda: os.environ.get("LING_ALERT_WEBHOOK_URL", "")
    )

    # 测试/开发模式：允许 fake provider 与测试登录
    test_mode: bool = field(default_factory=lambda: _bool("LING_TEST_MODE", True))

    # 角色默认身份（单角色）
    actor_id: str = field(default_factory=lambda: os.environ.get("LING_ACTOR_ID", "actor_ling"))
    persona_id: str = field(default_factory=lambda: os.environ.get("LING_PERSONA_ID", "persona_ling"))
    # **用户**的时区（IANA 名，如 Asia/Shanghai），用于"勿扰时段"和
    # "今日已主动发了几个"这类按当地作息判断的逻辑。
    #
    # 不设默认值、不猜：猜错会让勿扰时段在错误的时间生效（东八区用户
    # 会变成"白天静音、深夜可以打扰"），而这种错很隐蔽——用户只会觉得
    # "她怎么这个点找我"。未设置时 `gate()` 会明说按 UTC 算。
    # 群里回复时是否 @ 发言人。
    #
    # 默认开：群里同时好几个人说话时，"回你"三个字谁也听不懂是对谁说的，
    # 真人会用 @ 消歧。这**不是礼貌问题，是可理解性**。
    # 觉得太吵可以关掉。
    group_mention_on_reply: bool = field(
        default_factory=lambda: os.environ.get("LING_GROUP_MENTION", "1") != "0"
    )
    # OneBot WS **服务端**（推荐）：ling 监听，NapCat 作为客户端连过来。
    # 实测这个方向能稳定收到消息事件；反向（ling 当客户端连 NapCat）
    # 只收得到心跳，收不到消息事件。
    onebot_ws_port: int | None = field(
        default_factory=lambda: (
            int(os.environ["LING_ONEBOT_WS_PORT"])
            if os.environ.get("LING_ONEBOT_WS_PORT")
            else None
        )
    )
    onebot_ws_token: str | None = field(
        default_factory=lambda: os.environ.get("LING_ONEBOT_WS_TOKEN") or None
    )
    # OneBot 反向 WebSocket 入站地址（如 ws://192.0.2.10:13001）。
    # **配了它就不需要 HTTP 上报，也不需要 LING_HOST=0.0.0.0**——
    # ling 主动连出去，本机不必开放任何入站端口。
    onebot_ws_url: str | None = field(
        default_factory=lambda: os.environ.get("LING_ONEBOT_WS_URL") or None
    )
    # 出入许可：谁可以跟她说话。默认白名单——陌生人默认不该进对话链路。
    # 默认 `off`（谁都能说话）——这与改动前的行为一致，不悄悄改变部署表现。
    # 要收紧就显式设 `LING_ACCESS_MODE=whitelist`（dev_env.sh 里已给）。
    # 用 whitelist 当默认会让所有既有部署/测试突然拒收，那是更坏的意外。
    access_mode: str = field(
        default_factory=lambda: os.environ.get("LING_ACCESS_MODE", "off")
    )
    # 逗号分隔。**按字符串比较**：QQ 号可能带前导零，转 int 会丢。
    access_list: frozenset[str] = field(
        default_factory=lambda: frozenset(
            part.strip()
            for part in (os.environ.get("LING_ACCESS_LIST") or "").split(",")
            if part.strip()
        )
    )
    user_timezone: str | None = field(
        default_factory=lambda: os.environ.get("LING_USER_TIMEZONE") or None
    )

    # 静态前端目录（构建产物）。为空表示由 Vite 开发服务器代理。
    web_dist: Path | None = field(
        default_factory=lambda: (
            Path(os.environ["LING_WEB_DIST"]) if os.environ.get("LING_WEB_DIST") else None
        )
    )

    # 单个请求体大小上限（字节）。默认 32 MiB，与原来 nginx 的
    # `client_max_body_size 32m` 对齐 —— 删掉反代之后这条限制必须由应用
    # 自己承担，否则一个超大上传就能把进程内存吃光（uvicorn 会把 body
    # 读进内存）。见 ling/api/body_limit.py。
    max_body_bytes: int = field(
        default_factory=lambda: _int("LING_MAX_BODY_BYTES", 32 * 1024 * 1024)
    )

    # --- 受控 Web 更新通道（默认关闭）---
    #
    # API 进程**不接触 Docker socket**：它只把请求写进宿主挂进来的 requests
    # 目录，并读取宿主执行器写的 status 目录。真正的部署由宿主上的
    # deploy/update/executor.py 独立完成（见 deploy/update/README.md）。
    update_enabled: bool = field(default_factory=lambda: _bool("LING_UPDATE_ENABLED", False))
    update_requests_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("LING_UPDATE_REQUESTS_DIR", "/run/ling-update/requests")
        )
    )
    update_status_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("LING_UPDATE_STATUS_DIR", "/run/ling-update/status")
        )
    )

    # --- 渠道开关（默认全关：未验证的入口不向用户开放）---
    qq_enabled: bool = field(default_factory=lambda: _bool("LING_QQ_ENABLED", False))
    wechat_enabled: bool = field(default_factory=lambda: _bool("LING_WECHAT_ENABLED", False))

    # --- QQ 官方机器人（QQ 开放平台 / QQ Bot）---
    #
    # 与 OneBot 并行的另一条接入路径，默认全关：没有 app_id/secret 就什么都做不了，
    # 打开开关却没凭据只会让入站验签全部失败（而且不会有人知道原因）。
    qqbot_enabled: bool = field(default_factory=lambda: _bool("LING_QQBOT_ENABLED", False))
    qqbot_app_id: str | None = field(
        default_factory=lambda: os.environ.get("LING_QQBOT_APP_ID") or None
    )
    qqbot_client_secret: str | None = field(
        default_factory=lambda: os.environ.get("LING_QQBOT_CLIENT_SECRET") or None
    )
    # Webhook 签名密钥（开放平台里的 BotSecret）。没有它就拒绝一切入站事件——
    # 无法验签等于无法确认来源。
    qqbot_bot_secret: str | None = field(
        default_factory=lambda: os.environ.get("LING_QQBOT_BOT_SECRET") or None
    )
    # 直接给 Ed25519 公钥（hex）时优先用它验签，不再从 bot_secret 派生。
    qqbot_webhook_public_key: str | None = field(
        default_factory=lambda: os.environ.get("LING_QQBOT_WEBHOOK_PUBLIC_KEY") or None
    )
    # 实例标识：账号绑定按它隔离。openid 是**每个机器人下独立**的，
    # 两个机器人下同一个真人是两个不同 openid。
    qqbot_instance_id: str = field(
        default_factory=lambda: os.environ.get("LING_QQBOT_INSTANCE_ID", "qqbot-default")
    )
    # 沙箱环境。**默认为真**：没有正式资质时先打沙箱，避免把测试消息
    # 发给正式用户。上正式环境必须显式设 0。
    qqbot_sandbox: bool = field(default_factory=lambda: _bool("LING_QQBOT_SANDBOX", True))
    # 显式覆盖 API 基址（自建代理/平台改域名时用）
    qqbot_api_base: str | None = field(
        default_factory=lambda: os.environ.get("LING_QQBOT_API_BASE") or None
    )
    qqbot_token_url: str | None = field(
        default_factory=lambda: os.environ.get("LING_QQBOT_TOKEN_URL") or None
    )
    # 主动推送能力位。**这是平台权限探测结果，不是偏好开关**：
    # QQ 官方默认只允许被动回复，主动消息要单独申请。设成 1 但平台没开通，
    # 照样发不出去，系统会如实标记不可达。
    qqbot_proactive: bool = field(
        default_factory=lambda: _bool("LING_QQBOT_PROACTIVE", False)
    )
    qqbot_supports_group: bool = field(
        default_factory=lambda: _bool("LING_QQBOT_SUPPORTS_GROUP", True)
    )
    # 回调地址校验请求是否允许不带签名头。官方是否对 op=13 签名**未核实**，
    # 默认要求签名；平台不签时显式打开，而不是把验签降级成宽松通过。
    qqbot_allow_unsigned_validation: bool = field(
        default_factory=lambda: _bool("LING_QQBOT_ALLOW_UNSIGNED_VALIDATION", False)
    )
    # 现实设备/相机/位置/公开发布：默认关闭，需用户显式授权
    allow_camera: bool = field(default_factory=lambda: _bool("LING_ALLOW_CAMERA", False))
    allow_location: bool = field(default_factory=lambda: _bool("LING_ALLOW_LOCATION", False))
    allow_public_publish: bool = field(
        default_factory=lambda: _bool("LING_ALLOW_PUBLIC_PUBLISH", False)
    )

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql") or self.database_url.startswith("postgres")

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "root.key"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """测试用：环境变量改动后重新读取。"""
    get_settings.cache_clear()
