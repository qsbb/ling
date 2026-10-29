#!/usr/bin/env bash
# 本地开发环境变量（不含任何真实密钥）。
# 用法：source scripts/dev_env.sh
export LING_ENV=dev
# 默认只绑回环；要让 NapCat 反向上报进来，见文末 OneBot 段（会改成 0.0.0.0）
export LING_HOST=${LING_HOST:-127.0.0.1}
export LING_PORT=${LING_PORT:-8080}
export LING_DATABASE_URL=${LING_DATABASE_URL:-postgresql+asyncpg://ling@127.0.0.1:55432/ling}
export LING_BOOTSTRAP_ADMIN=${LING_BOOTSTRAP_ADMIN:-ling}
export LING_BOOTSTRAP_ADMIN_PASSWORD=${LING_BOOTSTRAP_ADMIN_PASSWORD:-ling-dev-password}
export LING_TEST_MODE=${LING_TEST_MODE:-1}
export LING_DATA_DIR=${LING_DATA_DIR:-$PWD/.data}
# **用户的**时区，用于"勿扰时段"和"今日主动发了几个"这类按当地作息的判断。
# 不设的话主动联系会被暂停（而不是按 UTC 硬算——那会让勿扰时段
# 在错误的时间生效，方向可能正好反了）。
export LING_USER_TIMEZONE=${LING_USER_TIMEZONE:-Asia/Shanghai}
# 主动推送能力位。**这是平台权限探测结果，不是偏好开关**：
# 即使设成 1，平台不给权限照样发不出去，系统会如实标记不可达。
# 默认 0 = 不主动找你。
export LING_ONEBOT_PROACTIVE=${LING_ONEBOT_PROACTIVE:-0}
# 出入许可：谁能跟她说话。
#   off        谁都能说
#   whitelist  **只有名单里的人**（默认）
#   blacklist  名单外的人都能说
export LING_ACCESS_MODE=${LING_ACCESS_MODE:-whitelist}
# 逗号分隔的 QQ 号。按字符串比较（QQ 号可能带前导零）。
export LING_ACCESS_LIST=${LING_ACCESS_LIST:-10001}

# ---------------------------------------------------------------- OneBot / NapCat
# 真实 QQ 接入（OneBot）。下面是示例值，按自己的部署改；
# 本机私密值请写进 .data/local_secrets.env（已被 gitignore），不要改这个文件。
#
# LING_HOST 必须是 0.0.0.0：NapCat 的反向上报要能访问到这台机器。
# 只绑 127.0.0.1 时 NapCat 连不上（已实测）。
# 用 WS 入站时**保持回环**：ling 主动连 NapCat，不需要任何人连进来。
# 只有退回 HTTP 上报（NapCat httpClients）时才需要改成 0.0.0.0。
export LING_HOST=${LING_HOST:-127.0.0.1}
# OneBot(NapCat) 已停用：改走 QQ 官方机器人，见文件末尾。
export LING_QQ_ENABLED=${LING_QQ_ENABLED:-0}
# NapCat 的 OneBot HTTP API（容器 3000 映射到宿主 13000）
export LING_ONEBOT_API_BASE=${LING_ONEBOT_API_BASE:-http://127.0.0.1:3000}
# 机器人自己的 QQ 号。配了它，入站上报必须带匹配的 X-Self-ID，否则拒绝。
export LING_ONEBOT_SELF_ID=${LING_ONEBOT_SELF_ID:-10000}
# **必须与 NapCat 配置里的 instance 一致**：账号绑定是按 instance 存的。
export LING_ONEBOT_INSTANCE_ID=${LING_ONEBOT_INSTANCE_ID:-napcat-main}
export LING_ONEBOT_SUPPORTS_GROUP=${LING_ONEBOT_SUPPORTS_GROUP:-1}
# NapCat 本次没有配 token/secret，所以留空。配了就必须两边一致。
export LING_ONEBOT_ACCESS_TOKEN=${LING_ONEBOT_ACCESS_TOKEN:-}
export LING_ONEBOT_SECRET=${LING_ONEBOT_SECRET:-}
# 入站：WS 客户端（推荐）。配了它就不需要 HTTP 上报，
# 也不需要把 LING_HOST 开成 0.0.0.0 —— ling 主动连出去。
# NapCat 的 wsServer 在容器 3001 → 宿主 13001。
# **推荐用服务端模式**：ling 监听，NapCat 连过来。
# 入站方向取「NapCat 主动连 ling」这一侧：实测该方向能稳定收到消息事件。
export LING_ONEBOT_WS_PORT=${LING_ONEBOT_WS_PORT:-}
export LING_ONEBOT_WS_TOKEN=${LING_ONEBOT_WS_TOKEN:-}
# 客户端模式（本环境实测只收到心跳，默认不启用）
export LING_ONEBOT_WS_URL=${LING_ONEBOT_WS_URL:-}

# ---------------------------------------------------------------- QQ 官方机器人（另一条接入路径）
# 这是 **QQ 开放平台（QQ Bot）** 的官方接口，与上面的 OneBot/NapCat 是两条
# 互相独立的路：官方用 openid（不是 QQ 号），只能被动回复（有次数与时效），
# 主动推送要单独申请权限。**不要**把它当成 OneBot 的替代品，也不要同时
# 用两套去接同一个 QQ 号。
#
# 默认全关：没有真实应用凭据时，打开只会让入站验签全部失败。
export LING_QQBOT_ENABLED=${LING_QQBOT_ENABLED:-0}
# 开放平台「机器人 → 开发设置」里的 AppID / AppSecret
export LING_QQBOT_APP_ID=${LING_QQBOT_APP_ID:-}
export LING_QQBOT_CLIENT_SECRET=${LING_QQBOT_CLIENT_SECRET:-}
# Webhook 签名密钥（BotSecret）。没有它一律拒绝入站事件。
export LING_QQBOT_BOT_SECRET=${LING_QQBOT_BOT_SECRET:-}
# 账号绑定按实例隔离。openid 是**每个机器人下独立**的，换机器人就是换身份。
export LING_QQBOT_INSTANCE_ID=${LING_QQBOT_INSTANCE_ID:-qqbot-default}
# 沙箱环境（默认 1）。沙箱只能给"沙箱名单"里的测试用户发消息；
# 上正式环境并完成资质审核后设 0。
export LING_QQBOT_SANDBOX=${LING_QQBOT_SANDBOX:-1}
# 主动推送能力位：**平台权限探测结果，不是偏好开关**。默认 0。
export LING_QQBOT_PROACTIVE=${LING_QQBOT_PROACTIVE:-0}
# 群聊能力（官方群机器人需要在开放平台开通"群聊"能力）
export LING_QQBOT_SUPPORTS_GROUP=${LING_QQBOT_SUPPORTS_GROUP:-1}
# 可选：平台不签 op=13 回调校验请求时（**未核实**）才打开，
# 这是唯一允许"不带签名"的例外，不要用它绕开正常验签。
export LING_QQBOT_ALLOW_UNSIGNED_VALIDATION=${LING_QQBOT_ALLOW_UNSIGNED_VALIDATION:-0}
# 回调地址填：https://<你的域名>/v1/qqbot/events
# （QQ 官方要求 HTTPS 且能被公网访问；本机开发需要内网穿透。）
echo "[dev_env] db=$LING_DATABASE_URL test_mode=$LING_TEST_MODE port=$LING_PORT"

# ---------------------------------------------------------------- 本机私密配置
# .data/ 已被 gitignore：里面的凭据不会入库。
# 换机器/换机器人时改这个文件即可，不要把密钥写进本脚本（本脚本是入库的）。
if [ -f .data/local_secrets.env ]; then
  . .data/local_secrets.env
fi
