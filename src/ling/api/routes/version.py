"""版本信息、GitHub Release 检查，以及受控 Web 更新通道的 HTTP 入口。

这里刻意把边界划清楚：

1. ``GET /v1/version`` 与 ``GET /v1/version/check-release`` 是**只读**的；
   版本检查只请求固定的 GitHub API，不执行 git、不下载、不更新。
2. ``GET /v1/version/update`` 只读宿主执行器写出的固定状态文件。
3. ``POST /v1/version/update-requests`` 只把一条经过二次核验的请求原子写进
   宿主的 requests 目录。API 进程**没有 Docker socket**，也不执行部署命令：
   真正的构建/备份/切换由宿主上的 ``deploy/update/executor.py`` 独立完成。

所有响应都带 ``Cache-Control: no-store``；错误只使用契约里的固定 detail 码。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request, Response

from ling.api.deps import CurrentAuth, SettingsDep
from ling.api.errors import LingError
from ling.config import Settings
from ling.contracts.v1 import ErrorCode
from ling.updates import protocol as update_protocol
from ling.updates.service import (
    REASON_IN_PROGRESS,
    REASON_NOT_NEWER,
    REASON_RELEASE_UNVERIFIABLE,
    ReleaseTarget,
    UpdateChannel,
    UpdateSubmissionError,
)

logger = logging.getLogger("ling.api.version")

# 固定地址：不接受客户端传入的 URL，也不跟随重定向。
GITHUB_RELEASE_URL = "https://api.github.com/repos/qsbb/ling/releases/latest"
# tag → commit 解析也只允许这一个仓库的 API host（不接受任何用户提供的 URL）。
GITHUB_TAG_REF_URL = "https://api.github.com/repos/qsbb/ling/git/ref/tags/{tag}"
GITHUB_TAG_OBJECT_URL = "https://api.github.com/repos/qsbb/ling/git/tags/{sha}"
RELEASE_TIMEOUT_SECONDS = 5.0
_MAX_RELEASE_BODY_BYTES = 256 * 1024
# annotated tag 指向另一个 tag 对象时最多递归几层；超过就视为不可核验。
_MAX_TAG_DEPTH = 4
_RELEASE_URL_PREFIX = "https://github.com/qsbb/ling/releases/"
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_TAG_BYTES = 200

# 更新请求体的上限（远小于状态文件上限：请求只有几个字段）。
MAX_UPDATE_BODY_BYTES = 4096

# POST 校验失败时使用的固定 detail 码（前端按码显示文案，不解析自然语言）。
DETAIL_CONTENT_TYPE_REQUIRED = "content_type_required"
DETAIL_UPDATE_HEADER_REQUIRED = "update_header_required"
DETAIL_ORIGIN_REQUIRED = "origin_required"
DETAIL_ORIGIN_MISMATCH = "origin_mismatch"
DETAIL_INVALID_BODY = "invalid_request_body"
DETAIL_BODY_TOO_LARGE = "request_body_too_large"

ReleaseStatus = Literal["ok", "no_release", "unavailable"]
_FetchResult = tuple[ReleaseStatus, dict[str, Any] | None]

_SEMVER = re.compile(
    r"^[vV]?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)

router = APIRouter(tags=["version"])


def current_version() -> str:
    """已安装包的版本优先；没安装（例如直接跑源码）时回退到源码版本。"""
    try:
        value = importlib_metadata.version("ling").strip()
        if value:
            return value
    except (importlib_metadata.PackageNotFoundError, ValueError):
        pass
    from ling import __version__

    return __version__


def build_commit() -> str | None:
    """构建时注入的提交号；未注入就是 null，绝不在请求里执行 git。"""
    value = os.environ.get("LING_COMMIT", "").strip()
    return value or None


def _default_web_dist() -> Path:
    # src/ling/api/routes/version.py → 仓库根
    return Path(__file__).resolve().parents[4] / "clients" / "web-console" / "dist"


def web_manifest_digest(web_dist: Path | None = None) -> str | None:
    """读部署生成的 web 清单并返回其 SHA-256。

    清单不存在（开发模式只构建了 dist）时返回 null。只读普通文件、限制
    大小、验证 schema，不遍历目录，也不执行 git 或构建命令。
    """
    manifest = (web_dist or _default_web_dist()) / ".ling-web-manifest.json"
    try:
        if manifest.is_symlink() or not manifest.is_file():
            return None
        if manifest.stat().st_size > _MAX_MANIFEST_BYTES:
            return None
        raw = manifest.read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return None
    return hashlib.sha256(raw).hexdigest()


def _parse_semver(value: str) -> tuple[int, int, int, tuple[int | str, ...]] | None:
    """解析常见 semver（可带 v 前缀与预发布/构建后缀）。

    不接受任意字符串：``main``、``release-latest`` 这类标签返回 None，
    由调用方把“无法比较”明确表达出来，而不是猜成一次升级。
    """
    match = _SEMVER.fullmatch(value.strip())
    if match is None:
        return None
    core = (int(match["major"]), int(match["minor"]), int(match["patch"]))
    prerelease_raw = match["prerelease"]
    if prerelease_raw is None:
        return (*core, ())
    prerelease: list[int | str] = []
    for part in prerelease_raw.split("."):
        if not part or not re.fullmatch(r"[0-9A-Za-z-]+", part):
            return None
        prerelease.append(int(part) if part.isdigit() else part)
    return (*core, tuple(prerelease))


def _compare_prerelease(
    left: tuple[int | str, ...], right: tuple[int | str, ...]
) -> int:
    if not left and not right:
        return 0
    # 没有预发布后缀的版本高于任何预发布版本。
    if not left:
        return 1
    if not right:
        return -1
    for a, b in zip(left, right, strict=False):
        if a == b:
            continue
        if isinstance(a, int) and isinstance(b, int):
            return 1 if a > b else -1
        if isinstance(a, int):
            return -1
        if isinstance(b, int):
            return 1
        return 1 if a > b else -1
    if len(left) == len(right):
        return 0
    return 1 if len(left) > len(right) else -1


def compare_versions(current: str, latest: str) -> int | None:
    """比较两个版本；任意一侧不是可识别 semver 时返回 None。

    返回值 >0 表示 latest 更新，0 表示相同，<0 表示 latest 更旧。
    """
    left = _parse_semver(current)
    right = _parse_semver(latest)
    if left is None or right is None:
        return None
    left_core, left_pre = left[:3], left[3]
    right_core, right_pre = right[:3], right[3]
    if left_core != right_core:
        return 1 if right_core > left_core else -1
    return _compare_prerelease(right_pre, left_pre)


def is_newer_release(current: str, latest: str) -> bool | None:
    comparison = compare_versions(current, latest)
    return None if comparison is None else comparison > 0


def _safe_release_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if not value.startswith(_RELEASE_URL_PREFIX):
        return None
    # 只允许普通 release 页面 URL 字符，避免把奇怪内容交给前端当链接。
    if len(value) > 500 or any(ch.isspace() for ch in value):
        return None
    return value


def _github_client() -> httpx.AsyncClient:
    """固定的 GitHub API 客户端：直连、无代理、无凭据、不跟随重定向。

    ``httpx`` 默认 ``trust_env=True``：会读 ``HTTP(S)_PROXY``/``ALL_PROXY`` 与
    ``~/.netrc``，并可能把其中的凭据发出去。更新核验只允许访问固定 URL 本身，
    所以这里显式关掉环境信任（``trust_env=False``）并再显式 ``proxy=None``，
    与宿主执行器 ``urllib.request.ProxyHandler({})`` + ``_NoRedirect`` 保持一致。
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(RELEASE_TIMEOUT_SECONDS),
        follow_redirects=False,
        trust_env=False,
        proxy=None,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "ling-version-check",
        },
    )


async def _read_limited_body(response: httpx.Response) -> bytes | None:
    """流式读取响应体，超过 ``_MAX_RELEASE_BODY_BYTES`` 立刻中止。

    先 ``response.content`` 再判大小，等于把任意大的响应完整缓冲进内存
    （GitHub 侧异常/被劫持时就是一次内存 DoS）。这里按 chunk 累计并提前返回
    ``None``，由调用方判“不可核验”。
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_RELEASE_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _json_object_bytes(raw: bytes | None) -> dict[str, Any] | None:
    """把已限长的字节解析成 JSON 对象；失败一律 None（错误脱敏）。"""
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


async def _fetch_latest_release() -> _FetchResult:
    """请求固定 GitHub 地址，把所有失败收敛为可展示的稳定状态。

    Returns:
        ("ok", payload) / ("no_release", None) / ("unavailable", None)。

    这里不读取、不发送任何凭据，不走环境代理、不跟随重定向，并流式限长；
    失败时不向上抛异常，也不把上游响应体或异常文本带出去。
    """
    status_code: int | None = None
    raw: bytes | None = None
    try:
        async with _github_client() as client, client.stream(
            "GET", GITHUB_RELEASE_URL
        ) as response:
                status_code = response.status_code
                if 200 <= status_code < 300:
                    raw = await _read_limited_body(response)
    except httpx.TimeoutException:
        return "unavailable", None
    except httpx.HTTPError:
        return "unavailable", None

    if status_code == 404:
        return "no_release", None
    if status_code is None or not 200 <= status_code < 300:
        return "unavailable", None
    payload = _json_object_bytes(raw)
    if payload is None:
        return "unavailable", None
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.strip() or len(tag.encode("utf-8")) > _MAX_TAG_BYTES:
        return "unavailable", None
    return "ok", payload


async def _resolve_tag_commit(tag: str) -> str | None:
    """把严格 ``vX.Y.Z`` tag 解析成 40 位 commit。

    只访问固定的 GitHub API；不支持重定向、不带凭据、不走环境代理，每个响应
    都流式限长。tag 可能是 annotated tag（ref → tag 对象 → 可能再嵌套），这里
    最多递归 ``_MAX_TAG_DEPTH`` 层，解析不出来就返回 None——宁可判“不可核验”，
    也不猜分支/``target_commitish``。
    """
    if update_protocol.parse_stable_tag(tag) is None:
        return None
    try:
        async with _github_client() as client:
            payload = await _github_get_object(client, GITHUB_TAG_REF_URL.format(tag=tag))
            if payload is None:
                return None
            obj = payload.get("object")
            for _ in range(_MAX_TAG_DEPTH + 1):
                if not isinstance(obj, dict):
                    return None
                sha = obj.get("sha")
                obj_type = obj.get("type")
                if not update_protocol.is_full_commit(sha):
                    return None
                if obj_type == "commit":
                    return sha
                if obj_type != "tag":
                    return None
                payload = await _github_get_object(client, GITHUB_TAG_OBJECT_URL.format(sha=sha))
                if payload is None:
                    return None
                obj = payload.get("object")
    except httpx.HTTPError:
        return None
    except Exception:  # noqa: BLE001 - 外部解析失败不能变成 500
        logger.warning("解析 tag 提交失败（已按不可核验处理）")
        return None
    return None


async def _github_get_object(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    """固定 URL 的 GET；2xx 之外、超长或非 JSON 对象都返回 None。"""
    async with client.stream("GET", url) as response:
        if not 200 <= response.status_code < 300:
            return None
        raw = await _read_limited_body(response)
    return _json_object_bytes(raw)


@dataclass(frozen=True)
class LatestRelease:
    """一次 Release 检查的规范化结果（不包含任何上游原文/异常文本）。"""

    status: ReleaseStatus
    release_id: int | None
    tag: str | None
    commit: str | None
    published_at: str | None
    html_url: str | None
    stable: bool
    is_newer: bool | None


def _empty_release(status: ReleaseStatus = "unavailable") -> LatestRelease:
    return LatestRelease(
        status=status,
        release_id=None,
        tag=None,
        commit=None,
        published_at=None,
        html_url=None,
        stable=False,
        is_newer=None,
    )


async def _fetch_update_release() -> LatestRelease:
    """拉取 latest release 并在必要时解析 tag 提交。

    只有同时满足“有 release id / 不是 draft / 不是 prerelease / tag 严格
    vX.Y.Z / 比当前版本新”时才去解析 tag。旧版 payload（测试与历史 API 模拟）
    没有 ``id`` 时一律视为不可核验，不会发起额外网络请求。
    """
    status, payload = await _fetch_latest_release()
    if status != "ok" or payload is None:
        return _empty_release(status)

    raw_tag = payload.get("tag_name")
    tag = raw_tag.strip() if isinstance(raw_tag, str) and raw_tag.strip() else None
    raw_id = payload.get("id")
    release_id = raw_id if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
    if release_id is not None and release_id <= 0:
        release_id = None
    stable = (
        tag is not None
        and update_protocol.parse_stable_tag(tag) is not None
        and release_id is not None
        and payload.get("draft") is not True
        and payload.get("prerelease") is not True
    )
    newer = is_newer_release(current_version(), tag) if tag is not None else None
    commit: str | None = None
    if stable and newer is True and tag is not None:
        commit = await _resolve_tag_commit(tag)
    raw_published = payload.get("published_at")
    published_at = raw_published if isinstance(raw_published, str) else None
    return LatestRelease(
        status="ok",
        release_id=release_id,
        tag=tag,
        commit=commit,
        published_at=published_at,
        html_url=_safe_release_url(payload.get("html_url")),
        stable=stable,
        is_newer=newer,
    )


def _as_target(latest: LatestRelease) -> ReleaseTarget:
    return ReleaseTarget(
        status=latest.status,
        release_id=latest.release_id,
        tag=latest.tag,
        commit=latest.commit,
        published_at=latest.published_at,
        html_url=latest.html_url,
        stable=latest.stable,
        is_newer=latest.is_newer,
    )


async def _latest_for_submission() -> LatestRelease:
    """POST 用的二次检查：任何异常都收敛为 ``unavailable``。"""
    try:
        return await _fetch_update_release()
    except Exception:  # noqa: BLE001 - 外部失败不能变成 500，也不能带出原文
        logger.warning("更新前核验 Release 失败（已按不可用处理）")
        return _empty_release()


@router.get("/version")
async def version_info(
    auth: CurrentAuth, settings: SettingsDep, response: Response
) -> dict[str, object]:
    """当前运行版本、构建提交与前端清单摘要。"""
    del auth  # 依赖只用于认证；版本信息本身与具体用户无关。
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema_version": "v1",
        "current": current_version(),
        "commit": build_commit(),
        "web_manifest_sha256": web_manifest_digest(settings.web_dist),
    }


@router.get("/version/check-release")
async def check_release(
    auth: CurrentAuth, settings: SettingsDep, response: Response
) -> dict[str, object]:
    """手动检查 GitHub 最新 Release；只读，不下载、不更新。

    旧字段（status/current/latest/published_at/html_url/is_newer）保持原样，
    只**追加** ``release_id`` / ``target_commit`` / ``update_eligible`` /
    ``update_reason``。旧客户端不看新字段也能正常工作。
    """
    response.headers["Cache-Control"] = "no-store"
    latest = await _latest_for_submission()
    current = current_version()
    commit = build_commit()
    channel = UpdateChannel(settings)
    eligible, reason = channel.evaluate_release(
        _as_target(latest),
        role=auth.user.role,
        current_version=current,
        current_commit=commit,
    )
    return {
        "schema_version": "v1",
        "status": latest.status,
        "current": current,
        "latest": latest.tag,
        "published_at": latest.published_at,
        "html_url": latest.html_url,
        "is_newer": latest.is_newer,
        "release_id": latest.release_id,
        "target_commit": latest.commit,
        "update_eligible": eligible,
        "update_reason": reason,
    }


@router.get("/version/update")
async def update_status(
    auth: CurrentAuth, settings: SettingsDep, response: Response
) -> dict[str, object]:
    """更新通道视图：只读固定 JSON，不发起任何网络请求。"""
    response.headers["Cache-Control"] = "no-store"
    channel = UpdateChannel(settings)
    return channel.view(
        role=auth.user.role,
        current_version=current_version(),
        current_commit=build_commit(),
    )


# ---------------------------------------------------------------- 更新请求


@dataclass(frozen=True)
class ExpectedInstall:
    release_id: int
    tag: str
    commit: str
    current_version: str
    current_commit: str


@dataclass(frozen=True)
class UpdateSubmission:
    confirm_tag: str
    expected: ExpectedInstall


class _SubmissionError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def _strict_text(value: object, field: str, *, max_length: int = 64) -> str:
    if not isinstance(value, str):
        raise _SubmissionError(DETAIL_INVALID_BODY)
    text = value.strip()
    if not text or len(text) > max_length:
        raise _SubmissionError(DETAIL_INVALID_BODY)
    return text


def _exact_keys(payload: dict[str, Any], expected: set[str]) -> None:
    # 严格禁止多余字段：拼错字段名比缺字段更危险（会被静默忽略）。
    if set(payload.keys()) != expected:
        raise _SubmissionError(DETAIL_INVALID_BODY)


def _parse_submission(raw: bytes) -> UpdateSubmission:
    if not raw:
        raise _SubmissionError(DETAIL_INVALID_BODY)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _SubmissionError(DETAIL_INVALID_BODY) from exc
    if not isinstance(payload, dict):
        raise _SubmissionError(DETAIL_INVALID_BODY)
    _exact_keys(payload, {"confirm_tag", "expected"})
    expected_raw = payload["expected"]
    if not isinstance(expected_raw, dict):
        raise _SubmissionError(DETAIL_INVALID_BODY)
    _exact_keys(
        expected_raw,
        {"release_id", "tag", "commit", "current_version", "current_commit"},
    )

    confirm_tag = _strict_text(payload["confirm_tag"], "confirm_tag")
    if update_protocol.parse_stable_tag(confirm_tag) is None:
        raise _SubmissionError(DETAIL_INVALID_BODY)
    release_id = expected_raw["release_id"]
    if not isinstance(release_id, int) or isinstance(release_id, bool) or release_id <= 0:
        raise _SubmissionError(DETAIL_INVALID_BODY)
    tag = _strict_text(expected_raw["tag"], "tag")
    if update_protocol.parse_stable_tag(tag) is None:
        raise _SubmissionError(DETAIL_INVALID_BODY)
    commit = _strict_text(expected_raw["commit"], "commit", max_length=64)
    if not update_protocol.is_full_commit(commit):
        raise _SubmissionError(DETAIL_INVALID_BODY)
    current_version_value = _strict_text(
        expected_raw["current_version"], "current_version", max_length=64
    )
    current_commit = _strict_text(expected_raw["current_commit"], "current_commit", max_length=64)
    if not update_protocol.is_full_commit(current_commit):
        raise _SubmissionError(DETAIL_INVALID_BODY)
    # confirm_tag 是“用户确认的目标”，必须和 expected 里的目标完全一致。
    if confirm_tag != tag:
        raise _SubmissionError(DETAIL_INVALID_BODY)
    return UpdateSubmission(
        confirm_tag=confirm_tag,
        expected=ExpectedInstall(
            release_id=release_id,
            tag=tag,
            commit=commit,
            current_version=current_version_value,
            current_commit=current_commit,
        ),
    )


def _origin_key(scheme: str, netloc: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)``：同源判定必须包含 scheme。

    ``http://host:443`` 与 ``https://host:443`` 同端口但不同源；只看 host/port
    会把一次明文请求当成 TLS 请求放行。默认端口按 scheme 归一化。
    """
    lowered = scheme.strip().lower()
    if lowered not in ("http", "https"):
        return None
    try:
        parsed = urlsplit(f"{lowered}://{netloc}")
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    if port is None:
        port = 443 if lowered == "https" else 80
    if not 1 <= port <= 65535:
        return None
    return lowered, host, port


def _origin_problem(request: Request, settings: Settings) -> str | None:
    """cookie 请求必须同源；Bearer 可省略 Origin，但带了就一样要校验。

    这里用 ``request.url``（uvicorn 的 ``--proxy-headers`` 已按可信代理配置
    还原 scheme/host），**不**自己解析 ``X-Forwarded-*``：盲信任请求头等于
    把同源校验交给攻击者。

    浏览器发出的 Origin 只有 ``scheme://host[:port]``。任何带 path/query/
    fragment/userinfo 的 Origin 都不是浏览器同源请求，直接拒绝——否则
    ``http://host/v1/../../`` 这类字符串会绕过粗粒度的前缀比较。
    """
    origin = request.headers.get("origin")
    cookie_auth = bool(request.cookies.get(settings.cookie_name))
    if origin is None:
        return DETAIL_ORIGIN_REQUIRED if cookie_auth else None
    if not origin or any(ch.isspace() for ch in origin):
        # 浏览器不会在 Origin 里带空白；带空白的一律不是可信同源请求。
        return DETAIL_ORIGIN_MISMATCH
    raw = origin
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return DETAIL_ORIGIN_MISMATCH
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return DETAIL_ORIGIN_MISMATCH
    if parsed.username or parsed.password:
        return DETAIL_ORIGIN_MISMATCH
    # 只接受裸 origin：空 path 或单个 "/"；query/fragment 一律拒绝。
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return DETAIL_ORIGIN_MISMATCH
    origin_key = _origin_key(parsed.scheme, parsed.netloc)
    request_key = _origin_key(request.url.scheme, request.url.netloc)
    if origin_key is None or request_key is None or origin_key != request_key:
        return DETAIL_ORIGIN_MISMATCH
    return None


def _require_update_headers(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise LingError(
            ErrorCode.invalid_request,
            "更新请求必须是 application/json",
            status_code=422,
            detail=DETAIL_CONTENT_TYPE_REQUIRED,
        )
    if request.headers.get("x-ling-update") != "1":
        raise LingError(
            ErrorCode.invalid_request,
            "缺少 X-Ling-Update: 1 请求头",
            status_code=422,
            detail=DETAIL_UPDATE_HEADER_REQUIRED,
        )


async def _read_update_body(request: Request) -> bytes:
    """流式读请求体，最多 ``MAX_UPDATE_BODY_BYTES + 1`` 就中止。

    先 ``await request.body()`` 再比大小等于先把 chunked/超长体完整缓冲，认证
    用户也能用一个大 body 让 API 分配任意内存。这里两条路都堵：

    1. ``Content-Length`` 已经超限 → 读取前直接拒绝；
    2. 没有/伪造了 header 时按 chunk 累计，超限立刻停止读取并报错。
    """
    length_header = request.headers.get("content-length", "").strip()
    if length_header.isdigit() and int(length_header) > MAX_UPDATE_BODY_BYTES:
        raise LingError(
            ErrorCode.invalid_request,
            "更新请求体过大",
            status_code=422,
            detail=DETAIL_BODY_TOO_LARGE,
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_UPDATE_BODY_BYTES:
            raise LingError(
                ErrorCode.invalid_request,
                "更新请求体过大",
                status_code=422,
                detail=DETAIL_BODY_TOO_LARGE,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _gate_error(reason: str) -> LingError:
    message = update_protocol.message_for(reason) or "当前不满足更新条件"
    if reason == REASON_IN_PROGRESS:
        return LingError(ErrorCode.conflict, message, status_code=409, detail=reason)
    return LingError(ErrorCode.provider_unavailable, message, status_code=503, detail=reason)


def _conflict(detail: str) -> LingError:
    message = update_protocol.message_for(detail) or "更新目标已变化"
    return LingError(ErrorCode.conflict, message, status_code=409, detail=detail)


@router.post("/version/update-requests", status_code=201)
async def create_update_request(
    auth: CurrentAuth,
    settings: SettingsDep,
    request: Request,
    response: Response,
) -> dict[str, object]:
    """owner 提交一次更新请求；只写 pending.json，不执行任何部署。"""
    response.headers["Cache-Control"] = "no-store"
    if auth.user.role != "owner":
        raise LingError(
            ErrorCode.forbidden,
            "只有所有者可以发起更新",
            status_code=403,
            detail="not_owner",
        )
    _require_update_headers(request)
    origin_problem = _origin_problem(request, settings)
    if origin_problem is not None:
        raise LingError(
            ErrorCode.forbidden,
            "更新请求来源校验失败",
            status_code=403,
            detail=origin_problem,
        )
    raw = await _read_update_body(request)
    try:
        submission = _parse_submission(raw)
    except _SubmissionError as exc:
        raise LingError(
            ErrorCode.invalid_request,
            "更新请求格式无效",
            status_code=422,
            detail=exc.detail,
        ) from exc

    channel = UpdateChannel(settings)
    current = current_version()
    commit = build_commit()
    gate_reason = channel.gate(role=auth.user.role, current_version=current, current_commit=commit)
    if gate_reason is not None:
        raise _gate_error(gate_reason)

    latest = await _latest_for_submission()
    eligible, reason = channel.evaluate_release(
        _as_target(latest),
        role=auth.user.role,
        current_version=current,
        current_commit=commit,
    )
    if not eligible:
        if reason == REASON_RELEASE_UNVERIFIABLE:
            raise LingError(
                ErrorCode.provider_unavailable,
                update_protocol.message_for(reason) or "无法核验发布目标",
                status_code=503,
                detail=reason,
            )
        if reason == REASON_NOT_NEWER:
            raise _conflict("release_changed")
        if reason is not None:
            raise _gate_error(reason)

    # 二次核验：expected 必须与刚刚重新获取的 latest/tag commit 逐项一致。
    expected = submission.expected
    if (
        latest.tag is None
        or latest.release_id is None
        or latest.commit is None
        or submission.confirm_tag != latest.tag
        or expected.tag != latest.tag
        or expected.release_id != latest.release_id
        or expected.commit != latest.commit
    ):
        raise _conflict("release_changed")
    if expected.current_version != current or expected.current_commit != commit:
        raise _conflict("installed_version_changed")
    if latest.is_newer is not True:
        raise _conflict("release_changed")

    record = update_protocol.build_request(
        target_tag=latest.tag,
        target_commit=latest.commit,
        current_version=current,
        current_commit=commit,
    )
    try:
        job = channel.submit(record)
    except UpdateSubmissionError as exc:
        raise _gate_error(exc.detail) from exc
    return job.to_dict()

