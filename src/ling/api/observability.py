"""请求级可观测性：给每个请求一个 trace_id，并让它出现在响应头与日志里。

## 为什么需要

排查"她为什么没回这条消息"时，最先遇到的困难不是没有日志，而是**日志和
用户看到的请求对不上**：用户说"我刚才发消息报错了"，日志里有几十条记录，
没法确定哪几条属于他那次请求。

## trace_id 的规则

- 客户端可以带 `X-Trace-Id` 进来（便于前端把 ID 打在错误页上再回来查），
  但只接受 8–64 位十六进制的值；其它一切字符串都丢弃、由服务端生成。
  不校验的话，用户可以在 header 里塞任意长字符串，把日志字段撑爆。
- 生成后写回响应头 `X-Trace-Id`，错误响应体里也带 `trace_id`。
- 只在请求处理期间绑定到 contextvars，请求结束就 reset——**绝不让上一个
  请求的身份字段漏进下一个请求**。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount

from ling.logging_setup import (
    bind_context,
    current_context,
    emit_event,
    new_trace_id,
    reset_context,
)

logger = logging.getLogger("ling.api.access")

TRACE_HEADER = "X-Trace-Id"
_VALID_TRACE = re.compile(r"^[0-9a-fA-F]{8,64}$")

# 这些路径每个健康检查都会打，没必要逐条记
_QUIET_PATHS = frozenset(
    {"/healthz", "/readyz", "/v1/auth/me", "/v1/health", "/v1/health/model"}
)

# 这些路径是**界面在轮询**的，成功时静默、失败时照记。
#
# `/v1/logs` 必须在这里，否则会形成一个自我观察的死循环：日志页每 2 秒拉一次，
# 每次请求都写一条访问日志，而那条访问日志又进了它自己正在读的环形缓冲。
# 默认 500 条的缓冲大约 17 分钟就会被这一条自己刷满，把真正的故障日志全挤掉——
# 表现为"打开日志页看了一会儿，之前的错误就消失了"。
#
# 只静默**成功**的请求：失败（4xx/5xx）仍然要留痕。一个一直 500 的日志接口
# 恰恰是最需要被看见的——把它一起静掉，问题就只剩界面上的一句"读取失败"。
_POLLING_PATHS = frozenset({"/v1/logs"})

# ---------------------------------------------------------------- 访问日志的措辞
#
# 日志页只显示 message，细节全在 attributes 里、要展开才看得到。于是原来那条
# 固定的「请求完成」实际上什么也没说——用户的原话是「一长串的请求完成，到底
# 请求完成了什么，能不能详细点」。这里把它改成能自解释的一行：
#
#   读取会话列表 → 200（12ms）
#   GET /v1/world/places → 200（4ms）
#   读取日志 → 200（2ms）
#   GET /v1/messages/{message_id}/prompt → 404（3ms，未找到）
#   POST /v1/sessions/{session_id}/turns → 500（8ms，服务端错误）
#
# 三条硬约束：
# 1. **只用路由模板**，不用真实请求路径：日志页长期保留，`/v1/sessions/sess_ab12`
#    里的 id 既能直接打开一个会话（隐私），也让同一个接口在日志里碎成上万个
#    不同的行（没法聚合）。模板 `/v1/sessions/{session_id}` 才既能聚合、又能
#    说清"是哪个接口"。
# 2. query、请求体、响应体一律不进 message：它们的体积与内容都不可控。
# 3. 2xx 不写任何判断（只在括号里给状态码与耗时）；4xx/5xx 用一句中文说明
#    "没成在哪"（见 `_STATUS_HINTS`）。
#
# 加路由就往这两张表里加一行，不用改拼装逻辑。
_ROUTE_ACTIONS: dict[tuple[str, str], str] = {
    # 会话与消息（界面最热的一条链）
    ("GET", "/v1/sessions"): "读取会话列表",
    ("POST", "/v1/sessions"): "新建会话",
    ("GET", "/v1/sessions/{session_id}"): "读取会话详情",
    ("GET", "/v1/sessions/{session_id}/messages"): "读取会话消息",
    ("POST", "/v1/sessions/{session_id}/turns"): "发送消息",
    ("POST", "/v1/sessions/{session_id}/media"): "上传媒体",
    # SP03 媒体读取：签名 URL 是渠道/`<audio>` 的取流路径（无登录态），
    # 另外三条是会话页的语音列表、单条语音与播放上报。
    ("GET", "/v1/media/generated/{file_id}"): "读取生成媒体",
    ("GET", "/v1/sessions/{session_id}/voice-messages"): "读取会话语音",
    ("GET", "/v1/messages/{message_id}/voice"): "读取消息语音",
    ("POST", "/v1/messages/{message_id}/playback"): "记录语音播放",
    # 入站入口：所有渠道消息都从这里进来，出问题时最需要一眼看见
    ("POST", "/v1/onebot/events"): "接收 OneBot 事件",
    ("GET", "/v1/onebot/health"): "读取 OneBot 健康状态",
    ("POST", "/v1/qqbot/events"): "接收 QQBot 事件",
    # 日志页与主要看板（都是会被反复打开的页面）
    ("GET", "/v1/logs"): "读取日志",
    ("GET", "/v1/dashboard"): "读取仪表盘",
    ("GET", "/v1/life/timeline"): "读取时间轴",
    ("GET", "/v1/activity"): "读取活动",
    ("GET", "/v1/goals"): "读取目标",
    ("GET", "/v1/proactive"): "读取主动消息",
    ("GET", "/v1/attention/stats"): "读取注意力统计",
    ("GET", "/v1/together"): "读取共处会话",
    ("GET", "/v1/awareness"): "读取意识条目",
    # 设置：改配置是"用户以为自己改了"的重灾区，留一条能读懂的记录
    ("GET", "/v1/settings/config"): "读取配置",
    ("PUT", "/v1/settings/draft"): "保存配置草稿",
    ("POST", "/v1/settings/publish"): "发布配置",
    ("GET", "/v1/settings/connectors"): "读取连接器状态",
    # 其余高频只读页
    ("GET", "/v1/memory"): "读取记忆",
    ("GET", "/v1/relationship"): "读取关系状态",
    ("GET", "/v1/affect"): "读取情绪状态",
    ("GET", "/v1/persona"): "读取人格设定",
    ("GET", "/v1/speech"): "读取语音状态",
    ("GET", "/v1/channels"): "读取渠道列表",
    ("GET", "/v1/accounts"): "读取账号列表",
    ("GET", "/v1/version"): "读取版本",
}

# 4xx/5xx 的一句话解释。表里没有的状态码按类别兜底（见 `_status_hint`）。
_STATUS_HINTS: dict[int, str] = {
    400: "请求不合法",
    401: "未登录",
    403: "无权限",
    404: "未找到",
    405: "方法不允许",
    409: "状态冲突",
    413: "请求体过大",
    422: "参数不合法",
    429: "请求太频繁",
    500: "服务端错误",
    502: "上游不可用",
    503: "服务不可用",
    504: "上游超时",
}

# SPA 兜底路由的模板。它匹配**任何** GET，所以模板本身不携带信息——
# 命中它就等于"没有路由"，退回 `_safe_path`。
_CATCH_ALL_TEMPLATE = "/{full_path:path}"

# 未命中路由时的判据：含数字且不算短的段、或超长段，一律当 id
# （`sess_ab12cd34`、`1728394`、uuid、一长串 token 都落在这里）。
_ID_LIKE_SEGMENT = re.compile(r"^(?=.*\d)[^/]{7,}$|^[^/]{24,}$")


def _status_hint(status: int) -> str:
    """状态码的一句话解释；2xx/3xx 返回空串（不写"成功"之外的判断）。"""
    if status in _STATUS_HINTS:
        return _STATUS_HINTS[status]
    if 400 <= status < 500:
        return "请求被拒绝"
    if status >= 500:
        return "服务端错误"
    return ""


def _access_message(
    *, method: str, template: str, status: int, elapsed_ms: float
) -> str:
    """访问日志的 message：`动作名/方法+模板路径 → 状态（耗时，解释）`。"""
    method = method.upper()
    action = _ROUTE_ACTIONS.get((method, template))
    subject = action or f"{method} {template}"
    hint = _status_hint(status)
    if hint:
        return f"{subject} → {status}（{elapsed_ms:g}ms，{hint}）"
    return f"{subject} → {status}（{elapsed_ms:g}ms）"


def _safe_path(path: str) -> str:
    """没有路由模板时的兜底：把像 id 的段折成 `{id}` 并就此截断。

    用于 SPA 兜底路由与真正的 404——这两种情况都没有模板可用，而真实路径
    可能整段是用户给的 id。宁可少说一段，也不把 id 写进长期保留的日志里。
    """
    kept: list[str] = []
    for segment in path.split("/"):
        if not segment:
            continue
        if _ID_LIKE_SEGMENT.match(segment):
            kept.append("{id}")
            break
        kept.append(segment)
    return "/" + "/".join(kept)


def _template_from_path(path: str, path_params: Mapping[str, Any]) -> str:
    """把原始路径里的参数值换成 `{name}`，得到完整路由模板。

    **不**拿 `scope["route"].path` 去拼前缀。FastAPI 0.162 起
    `include_router(prefix=...)` 用 `_IncludedRouter` 包裹子路由，
    `route.path` 只是子路由自己的 `/sessions`，`/v1` 那一层不在里面；而这一层
    前缀该怎么补、补在哪，取决于挂载方式（`app.include_router(prefix=)`、
    `APIRouter(prefix=)`、sub-app 各不相同）。照 `route.path` 拼前缀，一旦
    挂载方式或框架版本变了，模板就会掉成 `/sessions`，日志里再也对不上真实
    入口。

    原始路径本来就已经包含全部前缀，参数值也原样出现在里面，把值换成占位符
    就得到模板，与怎么挂载无关：

        /v1/sessions/abc123  →  /v1/sessions/{session_id}

    按**段**比对而不是 `str.replace`：值恰好是某个静态段的子串时（比如
    `/v1/logs/{log_id}` 里 log_id 是 `log`），整串替换会把静态的 `log` 一起
    改掉，模板从此匹配不上任何别的请求。
    """
    if not path_params:
        return path
    # 参数值 → 占位符名。值重复（同一段里出现两次）时留先出现的那个。
    by_value: dict[str, str] = {}
    for param_name, param_value in path_params.items():
        text = "" if param_value is None else str(param_value)
        if text:
            by_value.setdefault(text, param_name)
    if not by_value:
        return path

    segments = path.split("/")
    used: set[str] = set()
    for index, segment in enumerate(segments):
        if not segment:
            continue
        placeholder = by_value.get(segment)
        if placeholder is not None:
            segments[index] = "{" + placeholder + "}"
            used.add(placeholder)
    template = "/".join(segments)

    # 值里带 `/` 的参数（`:path` 转换器、`%2F`）分段时对不上，上面没换掉；
    # 剩下的值在整条路径里再换一次（长的先换，免得短值是长值的一段）。宁可
    # 模板粗一点，也不让真实值进长期保留的日志。
    for value, placeholder in sorted(by_value.items(), key=lambda item: -len(item[0])):
        if placeholder not in used and value in template:
            template = template.replace(value, "{" + placeholder + "}")
    return template


def route_template(request: Request) -> str:
    """路由模板（`/v1/sessions/{session_id}`），恒为带前缀的完整路径。

    路由发生在 `call_next` 内部，所以只能在它返回**之后**取
    `scope["route"]`（Starlette 匹配成功时写进去的）。这里只用它判断"有没有
    命中路由"，模板本身从原始路径反推（见 `_template_from_path`）。

    没有模板可用的两种情况（SPA 兜底路由、未命中任何路由）退回脱敏路径，
    见 `_safe_path`。
    """
    route = request.scope.get("route")
    local = getattr(route, "path", None)
    if not isinstance(local, str) or not local or local == _CATCH_ALL_TEMPLATE:
        return _safe_path(request.url.path)
    # 静态挂载（`app.mount("/assets", StaticFiles(...))`）没有"模板"可言，
    # 用挂载点当接口名，免得每个文件名在日志里各成一行。
    if isinstance(route, Mount):
        return local
    return _template_from_path(request.url.path, request.scope.get("path_params") or {})


def _trace_from_request(request: Request) -> str:
    incoming = request.headers.get(TRACE_HEADER) or ""
    if _VALID_TRACE.match(incoming):
        return incoming.lower()
    return new_trace_id()


async def observability_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    trace_id = _trace_from_request(request)
    request.state.trace_id = trace_id

    # 请求开始就知道的字段先绑上。**这里不绑路径**：此刻还没路由，手上只有
    # 原始路径（带真实 id）。模板要等 `call_next` 返回后才拿得到，而且它只
    # 进访问事件——contextvars 里的字段会挂在这次请求的**每一条**日志上。
    token = bind_context(
        trace_id=trace_id,
        process_role="api",
        http_method=request.method,
    )
    started = time.perf_counter()
    # reset 放在最外层：访问事件要用到 contextvars 里的 trace_id/turn_id，
    # 先 reset 再记，那条日志就成了唯一没有 trace_id 的一条。
    try:
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - 记录后交给上层异常处理器
            emit_event(
                logger,
                logging.ERROR,
                event_name="http_request_failed",
                message="请求处理抛出异常",
                attributes={
                    "http.request.method": request.method,
                    # 同样是模板路径：这条日志会长期保留
                    "http.route": route_template(request),
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    "error_type": type(exc).__name__,
                },
            )
            # 错误响应由异常处理器生成，trace 头在那边补（见 errors.py）
            raise

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers[TRACE_HEADER] = trace_id

        path = request.url.path
        # 轮询端点成功时不记访问日志（见 `_POLLING_PATHS` 的说明）；失败照记。
        # 这里用**原始路径**判断，与展示用的模板互不影响。
        quiet = path in _QUIET_PATHS or (
            path in _POLLING_PATHS and response.status_code < 400
        )
        if not quiet:
            template = route_template(request)
            level = logging.WARNING if response.status_code >= 400 else logging.INFO
            emit_event(
                logger,
                level,
                event_name="http_request_completed",
                message=_access_message(
                    method=request.method,
                    template=template,
                    status=response.status_code,
                    elapsed_ms=elapsed_ms,
                ),
                attributes={
                    "http.request.method": request.method,
                    "http.route": template,
                    "http.response.status_code": response.status_code,
                    "duration_ms": elapsed_ms,
                },
            )
        return response
    finally:
        reset_context(token)


def trace_id_of(request: Request) -> str | None:
    """取当前请求的 trace_id（没有就返回 None，不编一个）。"""
    value = getattr(request.state, "trace_id", None)
    if isinstance(value, str):
        return value
    return current_context().get("trace_id")


__all__ = ["TRACE_HEADER", "observability_middleware", "trace_id_of"]
