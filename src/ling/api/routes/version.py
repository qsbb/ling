"""版本信息、GitHub Release 检查，以及受控 Web 更新通道的 HTTP 入口。

这里刻意把边界划清楚：

1. ``GET /v1/version`` 与 ``GET /v1/version/check-release`` 是**只读**的；
   版本检查只请求固定的 GitHub API，不执行 git、不下载、不更新。
2. ``GET /v1/version/update`` 只读宿主执行器写出的固定状态文件。
3. ``POST /v1/version/update-requests`` 只把一条经过二次核验的请求原子写进
   宿主的 requests 目录。API 进程**没有 Docker socket**，也不执行部署命令：
   真正的构建/备份/切换由宿主上的 ``deploy/update/executor.py`` 独立完成。
   这条通道是**源码模式**的（下载 tag 源码 → 构建 → ``install.sh``），在镜像
   模式下**显式拒绝**，绝不在同一个 ``v1`` 语义下偷偷换成另一个更新源。
4. ``POST /v1/version/image-update-requests`` / ``GET /v1/version/image-update``
   是**镜像模式**的新协议：只接受固定格式的 digest 请求、只读独立更新器
   （``deploy/updater/updater.py``）写出的脱敏状态文件。见文末「镜像更新」一节。

所有响应都带 ``Cache-Control: no-store``；错误只使用契约里的固定 detail 码。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.api.deps import CurrentAuth, DbSession, SettingsDep
from ling.api.errors import LingError
from ling.config import Settings
from ling.contracts.v1 import ErrorCode
from ling.infra.models import AuthSession
from ling.infra.security import hash_token
from ling.updates import protocol as update_protocol
from ling.updates.service import (
    REASON_IN_PROGRESS,
    REASON_NOT_NEWER,
    REASON_RELEASE_UNVERIFIABLE,
    SUBMIT_LOCK_TIMEOUT_SECONDS,
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
    """更新通道视图：只读固定 JSON，不发起任何网络请求。

    ``current_job.backup_warnings`` 是**可选**的加法字段（协议仍 ``v1``）：执行器
    把「备份不完整」的告警同时写进 ``current.json``，这里原样透出，缺失即空数组。
    它只影响展示，不参与门禁；owner 权限、请求体 4096 上限与 422 分支都不受影响
    （那些只属于 ``POST /version/update-requests``，见下方）。
    """
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
    """owner 提交一次更新请求；只写 pending.json，不执行任何部署。

    **镜像模式显式拒绝**：这条通道的语义是「下载 tag 源码 → 构建 → 跑
    ``install.sh``」，而镜像模式没有源码树、也没有 ``build:``。把同一路径悄悄
    改成「按 digest 拉镜像」等于在同一个 ``v1`` 语义下换掉更新源，所以这里直接
    返回 409 + 固定 detail，让调用方去用 ``image-update-requests``。
    """
    response.headers["Cache-Control"] = "no-store"
    if auth.user.role != "owner":
        raise LingError(
            ErrorCode.forbidden,
            "只有所有者可以发起更新",
            status_code=403,
            detail="not_owner",
        )
    if image_mode_active(settings):
        raise LingError(
            ErrorCode.conflict,
            IMAGE_REASON_MESSAGES["image_mode_requires_image_channel"],
            status_code=409,
            detail="image_mode_requires_image_channel",
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



# ---------------------------------------------------------------- 镜像更新（新协议）
#
# 设计依据：docs/EASY-INSTALL-UPDATE-DESIGN.md §4（更新器与信任边界）、§5（一次网页
# 更新的实际顺序）、§6（故障时给用户什么结果）；docs/IMAGE-DEPLOYMENT-DESIGN.md §2
# 最后一行（旧 ``executor.py`` 不兼容新拓扑）。
#
# 与源码模式的老通道**刻意分成两套协议**：
#
# 1. ``POST /v1/version/image-update-requests`` 只接受固定格式请求体，原子写入
#    ``<requests>/request-<32 位小写 hex>.json``。API 不碰 Docker、不认 tag/URL/
#    compose 片段；真正的拉取/备份/迁移/切换由独立的 ``deploy/updater`` 复核后执行。
# 2. ``GET /v1/version/image-update`` 只读更新器的 ``status.json``，**逐字段白名单**
#    投影。更新器离线 / 状态文件缺失 / 格式不认识时显式表达「未知」，绝不伪装成
#    ``idle``，也绝不转发原始日志、备份路径、口令或更新器自己写的 message 文本。
# 3. 镜像模式下老端点 ``POST /v1/version/update-requests`` **显式拒绝**（见上面
#    ``create_update_request``）：不能在同一 ``v1`` 语义下把更新源换成 digest。
# 4. 恢复点门禁读 ``status.json`` 的**顶层** ``backup_gate``（更新器扫描备份目录
#    得出，与任务无关）；只有该字段**缺失**时才回退到 ``task.backup``（兼容旧状态
#    文件）。原因是死锁：``task`` 要等第一次任务处理完才存在，而第一次任务要先过
#    恢复点门禁——只看 ``task.backup`` 的话，全新镜像部署的网页更新永远不可安装。
# 5. **可用目标**（"更新"按钮到底能更新到什么）只来自更新器写的
#    ``available_targets`` / ``recommended_target``：API **不读**发布目录文件
#    （镜像模式的 API 根本没有 ``deploy/`` 挂载），也**不接受**调用方给的摘要。
#    这里仍按白名单逐字段重建，并且自己再核一遍「只允许升级」：目录/状态文件都
#    跨了一条信任边界，更新器算过不等于 API 可以盲信。``recommended_target`` 必须
#    与某条**兼容**目标逐字段一致，否则丢弃并退回"版本最大的兼容目标"——这样
#    「可安装 ⇔ 有目标」永远成立。没有兼容目标 → 不可安装，原因是固定码表文案。
# 6. 提交时的 ``target_digest`` 必须在该目录的**兼容目标**里，否则 409
#    ``target_not_in_catalog``：不能绕过目录提交任意摘要。
#
# 状态码口径（本文件对镜像通道的约定）：
#
# - 权限与来源（非所有者、Origin、最近登录）→ 403
# - 通道/拓扑状态（未启用、离线、协议不兼容、备份门禁、进行中、重复…）→ 409
# - 请求体格式 → 422；限流 → 429

IMAGE_UPDATER_PROTOCOL = 1
IMAGE_UPDATER_STATUS_SCHEMA = 1
IMAGE_UPDATER_STATUS_FILE = "status.json"
IMAGE_UPDATE_REQUEST_PREFIX = "request-"
IMAGE_UPDATE_REQUEST_SUFFIX = ".json"
IMAGE_UPDATE_REQUEST_ARCHIVE = "archive"

# 更新器自己的 ``REQUEST_ID_RE`` / ``publish.DIGEST_RE`` 是同一组口径：这里只认
# 32 位小写 hex 的任务 ID，与 ``ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>``。
IMAGE_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
IMAGE_DIGEST_RE = re.compile(r"^ghcr\.io/qsbb/ling-api@sha256:[0-9a-f]{64}$")
IMAGE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# 任务视图里的短标记（backup id / 阶段 / 迁移 revision）：只允许安全字符。
IMAGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:-]*$")

# HTTP 请求体**只有这五个字段**。``schema``/``created_at`` 由 API 自己写进请求
# 文件，客户端提交它们也算多余字段（fail closed，不给未来留后门）。
IMAGE_UPDATE_BODY_KEYS = frozenset(
    {
        "request_id",
        "target_digest",
        "expected_current_digest",
        "expected_current_commit",
        "operator_confirmed",
    }
)

# 更新器写请求文件时的固定结构（与 deploy/updater/updater.py 的 REQUEST_KEYS 一致）。
IMAGE_UPDATE_REQUEST_SCHEMA = 1

# ``status.json`` 的 ``updated_at`` 超过这个年龄就认为更新器离线。更新器默认 5 秒
# 轮询一次，空闲时每次都刷新 ``updated_at``；长阶段（备份/迁移）只在阶段边界写，
# 所以阈值比轮询间隔宽得多，宁可短暂说「离线」也不要谎称在线。
IMAGE_UPDATER_ONLINE_SECONDS = 120.0

# 提交限流：每个进程、每个用户 60 秒内最多 5 次（不假装是全局/分布式限流，
# 与 LoginRateLimiter 的已知取舍一致）。
IMAGE_UPDATE_RATE_LIMIT = 5
IMAGE_UPDATE_RATE_WINDOW_SECONDS = 60.0
IMAGE_UPDATE_RATE_MAX_KEYS = 1024

# 「最近登录」窗口：更新是能改宿主状态的不可逆动作，要求当前会话在 15 分钟内
# 建立（服务端按 ``auth_sessions.created_at`` 判定，不看 cookie 的 max_age）。
RECENT_LOGIN_MAX_AGE_SECONDS = 15 * 60

IMAGE_UPDATER_STATES = ("idle", "busy", "needs_manual")
IMAGE_TASK_STATE_MESSAGES: dict[str, str] = {
    # 与更新器的 TASK_* 一一对应；文案只来自这张码表。
    "running": "更新进行中",
    "succeeded": "更新完成",
    "failed_not_started": "更新未开始，未进入迁移",
    "needs_manual": "更新未完成，需要维护处理",
    "resolved": "已人工确认",
}
IMAGE_STAGE_MESSAGES: dict[str, str] = {
    # 只用**真实阶段**，不给假的百分比/剩余时间（设计 §4 末段）。
    "prepare": "正在下载并预检目标镜像",
    "backup": "正在做一致备份（恢复点）",
    "migrate": "正在执行数据库迁移",
    "switch": "正在切换并启动新版本",
    "verify": "正在验证新版本",
}
IMAGE_MAINTENANCE_STATES = frozenset({"active", "released", "unknown"})
IMAGE_TOMBSTONE_STATES = frozenset({"ok"})
# ``present`` 是 manifest 的原值（publish 原样记录），``ok`` 是早期/测试写法；
# 两者都是"媒体归档完整"。``partial`` 只归档了一部分，不能作恢复点。
IMAGE_MEDIA_STATES = frozenset({"ok", "present", "absent", "partial"})
IMAGE_KEYS_ROOT_STATES = frozenset({"present", "absent"})
IMAGE_KEYS_ENV_STATES = frozenset({"true", "false"})

# 更新器 ``status.json`` 顶层 ``release_catalog.reason`` 的码表（可信发布目录）。
# 同上：状态文件跨信任边界，只翻译认识的码，未知码丢弃（不回显原文）。
IMAGE_CATALOG_STATE_MESSAGES: dict[str, str] = {
    "ok": "已读到可信发布目录",
    "missing": "可信发布目录缺失（发行包里没有 deploy/releases.json）",
    "invalid": "可信发布目录无法验证（内容或权限不符合固定格式）",
}
IMAGE_CATALOG_REASON_MESSAGES: dict[str, str] = {
    "catalog_missing": "发行包里没有可信发布目录（deploy/releases.json），无法确定可用目标",
    "catalog_unreadable": "可信发布目录无法读取：不是普通文件、过大或权限过宽",
    "catalog_invalid": "可信发布目录内容不符合固定格式，已按不可信处理",
    "catalog_schema_unsupported": "可信发布目录的 schema 不是 1，需要人工核对",
    "catalog_tool_missing": "更新器发行包里缺少目录校验工具（deploy/release/catalog.py）",
}
# 「这条目标为什么不能用」的码表。**故意与更新器各存一份**。
IMAGE_INCOMPATIBLE_MESSAGES: dict[str, str] = {
    "not_in_min_from": "当前版本不在这条目标的升级跨度内（min_from 不含当前版本）",
    "not_newer": "目标版本不高于当前版本（只允许升级，不提供降级）",
    "current_version_unknown": "无法确认当前安装版本（当前镜像摘要不在发布目录里）",
    "version_uncomparable": "目标版本或当前版本不是可比较的 semver",
}

# 更新器 ``status.json`` 顶层 ``backup_gate.reason`` 的码表。**故意与更新器各存
# 一份**（同失败码表）：状态文件跨信任边界，只翻译认识的码，未知码丢弃。
IMAGE_BACKUP_GATE_REASONS: dict[str, str] = {
    "ok": "备份目录里有可用的恢复点",
    "no_manifest": "备份目录里还没有任何 manifest（没有可用的恢复点）",
    "backup_dir_unreadable": "备份目录不存在或不可读（没有可用的恢复点）",
    "manifest_unreadable": "最近的备份 manifest 无法解析（没有可用的恢复点）",
    "manifest_incomplete": "最近的备份 manifest 字段不全或产物不在位（没有可用的恢复点）",
    "tombstones_not_ok": "最近的备份墓碑状态不可信（没有可用的恢复点）",
    "media_partial": "最近的备份媒体只归档了一部分（没有可用的恢复点）",
    "keys_missing": "最近的备份缺少根密钥材料（没有可用的恢复点）",
}

# 失败/告警/下一步码表。**故意与更新器各存一份**：状态文件跨信任边界，API 只按
# 自己这一份翻译，绝不把文件里的 message 文本（可能含 argv、路径、日志）转出去。
IMAGE_FAILURE_MESSAGES: dict[str, str] = {
    "request_rejected": "请求不符合固定格式，未执行任何操作",
    "already_on_target": "请求的目标摘要与当前运行摘要相同，未执行任何操作",
    "duplicate_request": "该任务 ID 已处理过，不重复执行",
    "expected_current_mismatch": "当前运行 digest/提交与请求的期望当前值不一致，未执行任何操作",
    "preflight_failed": "预检未通过，未做任何变更",
    "maintenance_gate_failed": "维护门禁的临时 compose 未通过校验，未做任何变更",
    "pull_failed": "拉取镜像或镜像身份复核失败，未进入迁移",
    "backup_failed": "一致备份未通过校验，未执行迁移",
    "migration_failed": "迁移失败或迁移后版本不匹配，未启动新 API",
    "switch_failed": "已迁移：目标版本启动或验证失败，更新未完成，需要维护处理",
    "old_service_unverified": "未迁移，但未能实测旧服务正常，停在人工门禁",
    "reconcile_needs_manual": "更新器重启后无法确认状态，停在人工门禁",
    "state_unreadable": "任务状态文件无法读取，停在人工门禁",
    "previous_task_needs_manual": "上一个任务需要人工处理，暂不接受新任务",
    "install_lock_busy": "另一个安装/发布/更新任务正持有安装锁，本次未执行任何操作",
    "env_write_failed": "受限 env 写入失败或未指向目标摘要，需要维护处理",
    "internal_error": "更新器内部错误，已记录私有日志，请人工核对",
}
IMAGE_FAILURE_FALLBACK = "更新失败（原因见更新器私有日志）"
IMAGE_WARNING_MESSAGES: dict[str, str] = {
    "docker_free_unverified": "更新器容器看不到宿主 Docker 根目录，镜像侧磁盘余量未核对",
    "maintenance_reenforced": "已迁移后验证失败：维护门禁已重新关上（未切回旧镜像）",
}
IMAGE_NEXT_ACTION_MESSAGES: dict[str, str] = {
    "check_old_service": "人工确认旧服务（容器、宿主端口、/readyz）是否正常",
    "inspect_maintenance": "维护门禁仍在：公开端口保持关闭，按任务 ID 人工核对后再恢复",
    "review_private_log": "查看更新器状态目录里的私有日志与发布记录，再决定重试或人工恢复",
    "acknowledge_manual": "人工处理完成后用 --acknowledge-manual <request_id> 解除门禁",
    "no_action_needed": "无需操作",
}
IMAGE_MAX_WARNINGS = 8
# 可用目标列表的上限：状态文件跨信任边界，不为它提供内存/渲染放大器。
IMAGE_MAX_TARGETS = 50

# 不可安装/拒绝时给网页看的原因（码表文案）与「下一步该做什么」。
IMAGE_REASON_MESSAGES: dict[str, str] = {
    "image_mode_required": (
        "无法确认这是镜像模式部署：缺少 LING_APP_IMAGE 标记，也没有更新器状态文件；"
        "源码模式请改用源码更新通道"
    ),
    "update_disabled": "镜像更新通道未启用（LING_UPDATE_ENABLED 不是 1）",
    "not_owner": "只有所有者可以发起更新",
    "update_channel_unavailable": "更新通道目录不可用：API 读不到状态目录或写不了请求目录",
    "updater_status_missing": "没有更新器状态文件：更新器可能未启动，或状态目录没有挂载",
    "updater_status_invalid": "更新器状态文件无法识别，需要人工核对更新器版本与状态",
    "protocol_unsupported": "更新器协议不兼容，需要人工核对更新器版本",
    "updater_offline": "更新器离线：状态已过期，无法确认它还能执行更新",
    "manual_required": "更新器要求人工处理（上一个任务未收尾），暂不接受新任务",
    "update_in_progress": "已有更新请求在进行中，不能重复提交",
    "backup_gate_missing": (
        "没有可用的恢复点：备份目录里看不到完整的备份 manifest，"
        "更新前的备份/恢复门禁未就绪"
    ),
    "current_image_unknown": "无法确认当前运行镜像摘要或提交，暂不允许更新",
    "no_available_target": "暂无可用的更新目标（发布目录里没有适用于当前版本的新版本）",
    "target_not_in_catalog": "目标摘要不在可信发布目录的可用目标里，拒绝提交",
    "installed_image_changed": "当前运行镜像已变化，请重新检查后再提交",
    "installed_version_changed": "当前运行提交已变化，请重新检查后再提交",
    "already_on_target": "目标摘要与当前运行摘要相同，不需要更新",
    "duplicate_request_id": "该任务 ID 已使用过，不重复执行",
    "recent_login_required": "该操作要求最近 15 分钟内重新登录",
    "rate_limited": "提交过于频繁，请稍后再试",
    "image_mode_requires_image_channel": (
        "源码模式的更新通道不适用于镜像模式，请用 /v1/version/image-update-requests"
    ),
    "invalid_request_body": "更新请求必须是固定格式的 JSON 对象，且不含多余字段",
    "invalid_request_id": "request_id 必须是 32 位小写十六进制",
    "invalid_target_digest": "target_digest 必须是 ghcr.io/qsbb/ling-api@sha256:<64 位小写十六进制>",
    "invalid_expected_digest": (
        "expected_current_digest 必须是 ghcr.io/qsbb/ling-api@sha256:<64 位小写十六进制>"
    ),
    "invalid_expected_commit": "expected_current_commit 必须是 40 位小写十六进制提交号",
    "operator_not_confirmed": "必须由所有者显式确认（operator_confirmed 为 JSON true）",
}
IMAGE_REASON_STATUS: dict[str, int] = {
    "image_mode_required": 409,
    "update_disabled": 409,
    "not_owner": 403,
    "update_channel_unavailable": 409,
    "updater_status_missing": 409,
    "updater_status_invalid": 409,
    "protocol_unsupported": 409,
    "updater_offline": 409,
    "manual_required": 409,
    "update_in_progress": 409,
    "backup_gate_missing": 409,
    "current_image_unknown": 409,
    "no_available_target": 409,
    "target_not_in_catalog": 409,
    "installed_image_changed": 409,
    "installed_version_changed": 409,
    "already_on_target": 409,
    "duplicate_request_id": 409,
    "recent_login_required": 403,
    "rate_limited": 429,
    "image_mode_requires_image_channel": 409,
    "invalid_request_body": 422,
    "invalid_request_id": 422,
    "invalid_target_digest": 422,
    "invalid_expected_digest": 422,
    "invalid_expected_commit": 422,
    "operator_not_confirmed": 422,
}
IMAGE_REASON_NEXT_ACTIONS: dict[str, tuple[str, ...]] = {
    "image_mode_required": (
        "确认部署拓扑：镜像模式请在宿主注入 LING_APP_IMAGE 并启动更新器；"
        "源码模式继续用源码更新通道",
    ),
    "update_disabled": (
        "只在更新器在线、协议兼容、备份/恢复门禁齐全并完成真机演练后，"
        "由宿主把 LING_UPDATE_ENABLED 设为 1",
    ),
    "not_owner": ("用所有者账号重新登录后再操作",),
    "update_channel_unavailable": (
        "检查宿主是否把 requests 目录（可写）与 status 目录（只读）挂给 API 容器",
    ),
    "updater_status_missing": ("确认 ling-updater 容器在运行，并已写出 status.json",),
    "updater_status_invalid": ("核对更新器镜像版本与状态目录内容，必要时重启更新器容器",),
    "protocol_unsupported": ("把更新器升级到支持 protocol=1 的版本，再打开网页更新",),
    "updater_offline": ("检查 ling-updater 容器状态与状态目录写权限，等它重新写状态",),
    "manual_required": ("在宿主按任务 ID 人工核对后执行 --acknowledge-manual",),
    "update_in_progress": ("等待当前任务结束；不要重复提交，也不要手工改动请求目录",),
    "backup_gate_missing": (
        "在宿主跑一次备份（ling backup 或每日 cron），确认备份目录里有完整的"
        "manifest；更新器写出 restore_point=true 的 backup_gate 后即可网页更新",
    ),
    "current_image_unknown": ("在宿主把 LING_APP_IMAGE 与 LING_COMMIT 注入 API 容器后重启",),
    "no_available_target": (
        "核对发行包里的 deploy/releases.json（catalog.py validate）与更新器状态里的"
        " release_catalog；确认目录里有比当前版本新、且 min_from 含当前版本的目标",
    ),
    "target_not_in_catalog": (
        "从网页展示的可用目标里重新提交；目标只能来自可信发布目录，不能手填摘要",
    ),
    "installed_image_changed": ("重新打开检查更新，按最新的当前摘要重新提交",),
    "installed_version_changed": ("重新打开检查更新，按最新的当前提交重新提交",),
    "already_on_target": ("当前已是该摘要，无需操作",),
    "duplicate_request_id": ("换一个新的 request_id 重新提交（不要重放旧任务）",),
    "recent_login_required": ("退出后重新登录（15 分钟内）再发起更新",),
    "rate_limited": ("等待一分钟后重试",),
    "image_mode_requires_image_channel": (
        "改用 POST /v1/version/image-update-requests（固定 digest 请求）",
    ),
}
# 请求体错误不配「下一步」（前端直接改请求，不需要恢复操作）。
IMAGE_REASON_NEXT_ACTIONS.update(
    {
        reason: ("按固定格式重新提交；不要附加自定义字段",)
        for reason in (
            "invalid_request_body",
            "invalid_request_id",
            "invalid_target_digest",
            "invalid_expected_digest",
            "invalid_expected_commit",
            "operator_not_confirmed",
        )
    }
)

_IMAGE_FORBIDDEN_REASONS = frozenset({"not_owner", "recent_login_required"})
_IMAGE_INVALID_REASONS = frozenset(
    {
        "invalid_request_body",
        "invalid_request_id",
        "invalid_target_digest",
        "invalid_expected_digest",
        "invalid_expected_commit",
        "operator_not_confirmed",
    }
)
_IMAGE_RETRIABLE_REASONS = frozenset({"rate_limited", "updater_offline", "updater_status_missing"})

# 状态文件读取结果：区分「没有文件」与「有文件但不可信」。
_IMAGE_STATUS_MISSING = "missing"
_IMAGE_STATUS_INVALID = "invalid"
_IMAGE_STATUS_OK = "ok"


def _image_error(reason: str) -> LingError:
    """把固定 reason 翻成契约错误（码表文案 + 固定 detail + 稳定状态码）。"""
    message = IMAGE_REASON_MESSAGES.get(reason, "镜像更新暂不可用")
    if reason in _IMAGE_FORBIDDEN_REASONS:
        code = ErrorCode.forbidden
    elif reason in _IMAGE_INVALID_REASONS:
        code = ErrorCode.invalid_request
    elif reason == "rate_limited":
        code = ErrorCode.rate_limited
    else:
        code = ErrorCode.conflict
    return LingError(
        code,
        message,
        status_code=IMAGE_REASON_STATUS.get(reason, 409),
        retriable=reason in _IMAGE_RETRIABLE_REASONS,
        detail=reason,
    )


def current_image_ref() -> str | None:
    """宿主为镜像模式固定的应用镜像引用（``LING_APP_IMAGE``）。

    只有格式完全是 ``ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>`` 时才返回。
    ``latest``、可变 tag、别的 registry 一律当作「当前镜像未知」——拿一个不是
    digest 的字符串去比对期望值，等于把摘要绑定变成摆设。
    """
    value = os.environ.get("LING_APP_IMAGE", "").strip()
    if not value or IMAGE_DIGEST_RE.fullmatch(value) is None:
        return None
    return value


def image_mode_active(settings: Settings) -> bool:
    """当前进程是否处于镜像模式部署（API 读不到宿主 compose，只能看容器内信号）。

    三个信号任一成立即按镜像模式处理（宁可保守）：

    1. ``LING_APP_IMAGE`` 非空：镜像模式 compose 的必填插值（设计指定用它固定摘要）；
    2. ``LING_RUN_MIGRATIONS=0``：镜像模式 compose 对 api **写死**这一项（迁移改成
       一次显式动作），源码模式 compose 从不设置，entrypoint 缺省为 1；
    3. 更新器 ``status.json`` 存在：独立更新器只在镜像模式运行，它写的状态即证据。
    """
    if os.environ.get("LING_APP_IMAGE", "").strip():
        return True
    if os.environ.get("LING_RUN_MIGRATIONS", "").strip() == "0":
        return True
    state, _ = _read_image_status(Path(settings.update_status_dir) / IMAGE_UPDATER_STATUS_FILE)
    return state != _IMAGE_STATUS_MISSING


def _read_image_status(path: Path) -> tuple[str, dict[str, Any] | None]:
    """读更新器状态：普通文件、有上限、不跟随符号链接。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _IMAGE_STATUS_MISSING, None
    except OSError:
        return _IMAGE_STATUS_INVALID, None
    if not stat.S_ISREG(info.st_mode):
        return _IMAGE_STATUS_INVALID, None
    payload = update_protocol.read_json_file(path)
    if payload is None:
        return _IMAGE_STATUS_INVALID, None
    return _IMAGE_STATUS_OK, payload


def _image_text(value: object, *, max_length: int = 64) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > max_length or not IMAGE_TOKEN_RE.fullmatch(text):
        return None
    return text


def _image_digest(value: object) -> str | None:
    if not isinstance(value, str) or IMAGE_DIGEST_RE.fullmatch(value) is None:
        return None
    return value


def _image_commit(value: object) -> str | None:
    if not isinstance(value, str) or IMAGE_COMMIT_RE.fullmatch(value) is None:
        return None
    return value


def _image_request_id(value: object) -> str | None:
    if not isinstance(value, str) or IMAGE_REQUEST_ID_RE.fullmatch(value) is None:
        return None
    return value


def _image_timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if update_protocol.parse_iso_timestamp(value) is None:
        return None
    return value


def _image_choice(value: object, allowed: frozenset[str]) -> str | None:
    if isinstance(value, str) and value in allowed:
        return value
    return None


def _image_stage(value: object) -> str | None:
    """``stage`` 与 ``failure.stage`` 共用的白名单：只认 ``IMAGE_STAGE_MESSAGES`` 的键。

    两处**必须同一口径**。曾经 ``failure.stage`` 只过长度/字符集校验，于是更新器
    写进来的非阶段值（``reconcile``、``bootstrap``、``status``，以及复用
    ``publish.py`` 时的 ``pull``/``preflight``/``up_api`` 等步骤名）会原样透传，
    前端翻成「处理中（阶段不可识别）」——任务明明已经停了，看起来却像还在跑。

    未知值投影成 ``None``（前端显示「尚未开始」），也不回显原文：状态文件跨信任
    边界，阶段名同样不是自由文本。
    """
    if isinstance(value, str) and value in IMAGE_STAGE_MESSAGES:
        return value
    return None


def _image_choices(value: object, table: dict[str, str], *, limit: int = IMAGE_MAX_WARNINGS) -> list[str]:
    """把码数组转成文案数组：只保留认识的码，未知码**丢弃**（不回显原文）。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        return []
    texts: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = table.get(item)
        if text is None:
            continue
        texts.append(text)
        if len(texts) >= limit:
            break
    return texts


def _image_available_targets(
    raw: object, current_version: str
) -> list[dict[str, object]]:
    """投影 ``status.json`` 的 ``available_targets``（逐条白名单 + 去重 + 限长）。"""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        return []
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in raw:
        entry = _image_target_entry(item, current_version)
        if entry is None:
            continue
        digest = str(entry["digest"])
        if digest in seen:
            continue
        seen.add(digest)
        entries.append(entry)
        if len(entries) >= IMAGE_MAX_TARGETS:
            break
    return entries


def _image_catalog_view(raw: object) -> dict[str, object]:
    """``status.json`` 顶层 ``release_catalog`` 的白名单视图（状态文件不可信）。

    逐字段重建；``state``/``reason`` 只认 API 自己那份码表里的码，未知码丢弃
    （不回显原文）。目录缺失/坏掉时这里给出**明确原因**，绝不静默。
    """
    if not isinstance(raw, dict):
        return {
            "state": None,
            "state_message": None,
            "reason": None,
            "reason_message": None,
            "entries": 0,
            "current_version": None,
        }
    state = _image_choice(raw.get("state"), frozenset(IMAGE_CATALOG_STATE_MESSAGES))
    reason = _image_choice(raw.get("reason"), frozenset(IMAGE_CATALOG_REASON_MESSAGES))
    entries = raw.get("entries")
    if isinstance(entries, bool) or not isinstance(entries, int) or entries < 0:
        entries = 0
    return {
        "state": state,
        "state_message": IMAGE_CATALOG_STATE_MESSAGES.get(state) if state else None,
        "reason": reason,
        "reason_message": IMAGE_CATALOG_REASON_MESSAGES.get(reason) if reason else None,
        "entries": min(entries, IMAGE_MAX_TARGETS),
        "current_version": _image_text(raw.get("current_version"), max_length=64),
    }


def _image_target_entry(raw: object, current_version: str) -> dict[str, object] | None:
    """把更新器的一条可用目标投影成白名单视图；结构不认识就整条丢弃。

    除了照抄更新器的 ``compatible``，这里**自己再核一遍「只允许升级」**：目标版本
    必须严格大于当前版本。状态文件跨了一条信任边界，更新器算过的结论 API 仍要复核
    （与恢复点门禁的做法一致）。
    """
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    if not isinstance(version, str) or _parse_semver(version) is None:
        return None
    digest = _image_digest(raw.get("digest"))
    commit = _image_commit(raw.get("commit"))
    has_migration = raw.get("has_migration")
    compatible_raw = raw.get("compatible")
    if (
        digest is None
        or commit is None
        or not isinstance(has_migration, bool)
        # `compatible` 必须是 JSON 布尔：缺字段/字符串都算状态文件不合固定结构，
        # 整条丢弃——不能把"不知道能不能用"显示成一条目标。
        or not isinstance(compatible_raw, bool)
    ):
        return None
    compatible = compatible_raw
    reason = _image_choice(
        raw.get("incompatible_reason"), frozenset(IMAGE_INCOMPATIBLE_MESSAGES)
    )
    if compatible:
        comparison = compare_versions(current_version, version)
        if comparison is None:
            compatible, reason = False, "version_uncomparable"
        elif comparison <= 0:
            compatible, reason = False, "not_newer"
    return {
        "version": version,
        "digest": digest,
        "commit": commit,
        "has_migration": has_migration,
        "compatible": compatible,
        "incompatible_reason": reason,
        "incompatible_reason_message": IMAGE_INCOMPATIBLE_MESSAGES.get(reason) if reason else None,
    }


def _image_max_compatible(available: list[dict[str, object]]) -> dict[str, object] | None:
    """兼容目标里版本最大的那条（没有就 None）。"""
    best: dict[str, object] | None = None
    for item in available:
        if item.get("compatible") is not True:
            continue
        if best is None:
            best = item
            continue
        if (compare_versions(str(best["version"]), str(item["version"])) or 0) > 0:
            best = item
    return best


def _image_target_payload(item: dict[str, object] | None) -> dict[str, object] | None:
    """``view.target`` 的固定四字段（只给 version/digest/commit/has_migration）。"""
    if item is None:
        return None
    return {
        "version": item["version"],
        "digest": item["digest"],
        "commit": item["commit"],
        "has_migration": item["has_migration"],
    }


def _image_backup_view(backup: dict[str, Any]) -> dict[str, object]:
    return {
        "id": _image_text(backup.get("id"), max_length=64),
        "complete": backup.get("complete") is True,
        "tombstones_state": _image_choice(backup.get("tombstones_state"), IMAGE_TOMBSTONE_STATES),
        "media_state": _image_choice(backup.get("media_state"), IMAGE_MEDIA_STATES),
        "keys_root": _image_choice(backup.get("keys_root"), IMAGE_KEYS_ROOT_STATES),
        "keys_env_set": _image_choice(backup.get("keys_env_set"), IMAGE_KEYS_ENV_STATES),
    }


def _image_failure_view(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    code = raw.get("code")
    text = IMAGE_FAILURE_MESSAGES.get(code) if isinstance(code, str) else None
    if text is None:
        # 未知失败码：code/message 都不转发，只给固定兜底文案。
        return {"code": None, "stage": None, "message": IMAGE_FAILURE_FALLBACK}
    return {
        "code": code,
        "stage": _image_stage(raw.get("stage")),
        "message": text,
    }


def _image_task_view(raw: object) -> dict[str, object] | None:
    """把更新器的任务块投影成白名单视图；结构不认识返回 None。

    这里**逐字段重建**，绝不 ``dict(raw)``：状态文件虽然来自更新器容器，API 仍按
    「不可信输入」处理——多出来的键（路径、日志、口令）一律进不了响应。
    """
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    if not isinstance(state, str) or state not in IMAGE_TASK_STATE_MESSAGES:
        return None
    target_raw = raw.get("target")
    target = target_raw if isinstance(target_raw, dict) else {}
    previous_raw = raw.get("previous")
    previous = previous_raw if isinstance(previous_raw, dict) else {}
    migration_raw = raw.get("migration")
    migration = migration_raw if isinstance(migration_raw, dict) else {}
    backup_raw = raw.get("backup")
    backup = backup_raw if isinstance(backup_raw, dict) else {}
    maintenance_raw = raw.get("maintenance")
    maintenance = maintenance_raw if isinstance(maintenance_raw, dict) else {}
    rollback_raw = raw.get("rollback")
    rollback = rollback_raw if isinstance(rollback_raw, dict) else {}
    stage_value = _image_stage(raw.get("stage"))
    still_on_old = raw.get("still_on_old_version")
    return {
        "request_id": _image_request_id(raw.get("request_id")),
        "state": state,
        "state_message": IMAGE_TASK_STATE_MESSAGES[state],
        "stage": stage_value,
        "stage_message": IMAGE_STAGE_MESSAGES.get(stage_value) if stage_value else None,
        "started_at": _image_timestamp(raw.get("started_at")),
        "updated_at": _image_timestamp(raw.get("updated_at")),
        "finished_at": _image_timestamp(raw.get("finished_at")),
        "target": {
            "digest": _image_digest(target.get("digest")),
            "commit": _image_commit(target.get("commit")),
        },
        "previous": {
            "digest": _image_digest(previous.get("digest")),
            "commit": _image_commit(previous.get("commit")),
        },
        "migration": {
            "attempted": migration.get("attempted") is True,
            "current_before": _image_text(migration.get("current_before"), max_length=32),
            "current_after": _image_text(migration.get("current_after"), max_length=32),
            "target_head": _image_text(migration.get("target_head"), max_length=32),
        },
        "backup": _image_backup_view(backup),
        "maintenance": {
            "state": _image_choice(maintenance.get("state"), IMAGE_MAINTENANCE_STATES),
            "verified": maintenance.get("verified") is True,
        },
        "rollback": {
            "attempted": rollback.get("attempted") is True,
            "verified": rollback.get("verified") is True,
        },
        "still_on_old_version": still_on_old if isinstance(still_on_old, bool) else None,
        "failure": _image_failure_view(raw.get("failure")),
        "warnings": _image_choices(raw.get("warnings"), IMAGE_WARNING_MESSAGES),
        "next_actions": _image_choices(raw.get("next_actions"), IMAGE_NEXT_ACTION_MESSAGES),
    }


def _image_restore_point(backup: dict[str, object] | None) -> dict[str, object] | None:
    """API 侧再核一遍「恢复点是否齐全」（更新器已经核过，但两边都要 fail closed）。

    判据与 ``deploy/release/publish.py`` 的备份门禁同源：墓碑状态必须是 ``ok``、
    媒体不能是 ``partial``、根密钥材料必须存在（或 ``keys_env_set`` 为真）。任何
    一项不满足都不算恢复点——「更新成功但备份不完整」不能成为放行条件。

    ``media_state`` 同时接受 ``present`` 与 ``ok``：更新器的任务记录沿用 publish
    的结果（manifest 原值 ``present``），早期测试/旧状态文件用 ``ok``。两者都表示
    "媒体归档完整"；只认 ``ok`` 会把每一次真实更新的恢复点判成不完整。
    """
    if not isinstance(backup, dict):
        return None
    if backup.get("complete") is not True or not backup.get("id"):
        return None
    if backup.get("tombstones_state") != "ok":
        return None
    if backup.get("media_state") not in ("ok", "present", "absent"):
        return None
    keys_root = backup.get("keys_root")
    if keys_root not in ("present", "absent"):
        return None
    keys_env = backup.get("keys_env_set")
    if keys_env not in ("true", "false"):
        return None
    if keys_env == "false" and keys_root != "present":
        return None
    return backup


def _image_backup_gate_view(raw: object) -> dict[str, object] | None:
    """``status.json`` 顶层 ``backup_gate`` 的白名单视图（状态文件不可信）。

    逐字段重建：多出来的键（路径、日志、自由文本）一律进不了响应；``reason``
    只认 API 自己那份码表里的码，未认识的码丢弃（不回显原文）。
    """
    if not isinstance(raw, dict):
        return None
    reason = _image_choice(raw.get("reason"), frozenset(IMAGE_BACKUP_GATE_REASONS))
    return {
        "restore_point": raw.get("restore_point") is True,
        "complete": raw.get("complete") is True,
        "backup_id": _image_text(raw.get("backup_id"), max_length=64),
        "tombstones_state": _image_choice(raw.get("tombstones_state"), IMAGE_TOMBSTONE_STATES),
        "media_state": _image_choice(raw.get("media_state"), IMAGE_MEDIA_STATES),
        "keys_root": _image_choice(raw.get("keys_root"), IMAGE_KEYS_ROOT_STATES),
        "keys_env_set": _image_choice(raw.get("keys_env_set"), IMAGE_KEYS_ENV_STATES),
        "reason": reason,
        "reason_message": IMAGE_BACKUP_GATE_REASONS.get(reason) if reason else None,
        "checked_at": _image_timestamp(raw.get("checked_at")),
    }


def _image_gate_backup(gate: dict[str, object] | None) -> dict[str, object] | None:
    """把 ``backup_gate`` 当成 ``task.backup`` 再核一遍（不盲信 ``restore_point``）。

    更新器说 ``restore_point=true`` 只是**它的**结论；API 仍按同一套判据重算，
    这样"字段被改坏/半写"的状态文件不会把放行条件变松。
    """
    if gate is None or gate.get("restore_point") is not True:
        return None
    return _image_restore_point(
        {
            "id": gate.get("backup_id"),
            "complete": gate.get("complete"),
            "tombstones_state": gate.get("tombstones_state"),
            "media_state": gate.get("media_state"),
            "keys_root": gate.get("keys_root"),
            "keys_env_set": gate.get("keys_env_set"),
        }
    )


def _image_pending_request_ids(requests_dir: Path) -> tuple[str, ...]:
    """请求目录里还没被更新器认领/归档的任务 ID（不读 ``archive/`` 子目录）。"""
    if not update_protocol.directory_ready(requests_dir):
        return ()
    try:
        entries = sorted(requests_dir.iterdir(), key=lambda item: item.name)
    except OSError:
        return ()
    ids: list[str] = []
    for path in entries:
        name = path.name
        for prefix in (IMAGE_UPDATE_REQUEST_PREFIX, "claimed-"):
            if name.startswith(prefix) and name.endswith(IMAGE_UPDATE_REQUEST_SUFFIX):
                candidate = name[len(prefix) : -len(IMAGE_UPDATE_REQUEST_SUFFIX)]
                if IMAGE_REQUEST_ID_RE.fullmatch(candidate):
                    ids.append(candidate)
    return tuple(ids)


class ImageUpdateRateLimiter:
    """镜像更新提交的限流：**每进程、按用户**的固定窗口计数。

    语义诚实：状态在进程内存里，多副本时每个副本各算一份（与
    ``LoginRateLimiter`` 的已知取舍相同），不假装是全局限流。键数量有上限，
    避免「任何人对任何账号提交」变成内存放大器。
    """

    MAX_ATTEMPTS = IMAGE_UPDATE_RATE_LIMIT
    WINDOW_SECONDS = IMAGE_UPDATE_RATE_WINDOW_SECONDS
    MAX_KEYS = IMAGE_UPDATE_RATE_MAX_KEYS

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        cutoff = moment - self.WINDOW_SECONDS
        with self._lock:
            hits = [stamp for stamp in self._attempts.get(key, []) if stamp > cutoff]
            if len(hits) >= self.MAX_ATTEMPTS:
                self._attempts[key] = hits
                return False
            hits.append(moment)
            self._attempts[key] = hits
            if len(self._attempts) > self.MAX_KEYS:
                for stale in [k for k, v in self._attempts.items() if not v or v[-1] <= cutoff]:
                    self._attempts.pop(stale, None)
            return True


def _image_limiter(request: Request) -> ImageUpdateRateLimiter:
    """取本进程的镜像更新限流器（惰性挂到 ``app.state``，每个 app 一份）。"""
    limiter = getattr(request.app.state, "image_update_limiter", None)
    if limiter is None:
        limiter = ImageUpdateRateLimiter()
        request.app.state.image_update_limiter = limiter
    return limiter


async def _require_recent_login(auth: CurrentAuth, session: AsyncSession) -> None:
    """要求当前会话是**最近 15 分钟内**建立的（服务端按会话表判定）。

    只看 cookie 的 ``max_age`` 是不够的：会话被窃后 token 仍能用到自然过期。
    ``created_at`` 在服务端，改不了；取不到会话行时同样 fail closed。
    """
    row = (
        await session.execute(
            select(AuthSession).where(AuthSession.token_hash == hash_token(auth.token))
        )
    ).scalar_one_or_none()
    created = None if row is None else row.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    age = None if created is None else (datetime.now(UTC) - created).total_seconds()
    if age is None or age > RECENT_LOGIN_MAX_AGE_SECONDS:
        raise _image_error("recent_login_required")


@dataclass(frozen=True)
class ImageUpdateSubmission:
    """已通过固定格式校验的镜像更新请求（只有这四项来自客户端）。"""

    request_id: str
    target_digest: str
    expected_current_digest: str
    expected_current_commit: str


def _parse_image_submission(raw: bytes) -> ImageUpdateSubmission:
    """严格解析固定格式请求体：多一个字段、少一个字段、类型不对都拒绝。"""
    if not raw:
        raise _SubmissionError("invalid_request_body")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _SubmissionError("invalid_request_body") from exc
    if not isinstance(payload, dict):
        raise _SubmissionError("invalid_request_body")
    # 多余字段一律拒绝：拼错字段名/夹带 shell/compose 片段都不给入口。
    if set(payload) != IMAGE_UPDATE_BODY_KEYS:
        raise _SubmissionError("invalid_request_body")
    request_id = payload["request_id"]
    if not isinstance(request_id, str) or IMAGE_REQUEST_ID_RE.fullmatch(request_id) is None:
        raise _SubmissionError("invalid_request_id")
    target_digest = payload["target_digest"]
    if not isinstance(target_digest, str) or IMAGE_DIGEST_RE.fullmatch(target_digest) is None:
        raise _SubmissionError("invalid_target_digest")
    expected_current_digest = payload["expected_current_digest"]
    if (
        not isinstance(expected_current_digest, str)
        or IMAGE_DIGEST_RE.fullmatch(expected_current_digest) is None
    ):
        raise _SubmissionError("invalid_expected_digest")
    expected_current_commit = payload["expected_current_commit"]
    if (
        not isinstance(expected_current_commit, str)
        or IMAGE_COMMIT_RE.fullmatch(expected_current_commit) is None
    ):
        raise _SubmissionError("invalid_expected_commit")
    # 必须是 JSON ``true``：字符串 "true"、1、缺省都不算「操作者确认」。
    if payload["operator_confirmed"] is not True:
        raise _SubmissionError("operator_not_confirmed")
    return ImageUpdateSubmission(
        request_id=request_id,
        target_digest=target_digest,
        expected_current_digest=expected_current_digest,
        expected_current_commit=expected_current_commit,
    )


class ImageUpdateChannel:
    """镜像更新通道的只读视图 + 单飞提交。

    只读 ``status.json``、只写 ``request-<id>.json``；不碰 Docker、不执行部署。
    所有路径来自进程配置，不接受任何请求参数。
    """

    def __init__(self, settings: Settings, *, now: float | None = None) -> None:
        self.settings = settings
        self._now = time.time() if now is None else now
        self.requests_dir = Path(settings.update_requests_dir)
        self.status_dir = Path(settings.update_status_dir)
        self.status_path = self.status_dir / IMAGE_UPDATER_STATUS_FILE
        state, payload = _read_image_status(self.status_path)
        # 状态文件存在本身就是镜像模式的证据（更新器只在镜像模式运行）。
        presence = state != _IMAGE_STATUS_MISSING
        if payload is not None and payload.get("schema") != IMAGE_UPDATER_STATUS_SCHEMA:
            # 不认识的 schema：整份状态都不可信，按「有文件但不可解析」处理。
            state, payload = _IMAGE_STATUS_INVALID, None
        self.status_state = state
        self._status = payload
        self.image_mode = (
            bool(os.environ.get("LING_APP_IMAGE", "").strip())
            or os.environ.get("LING_RUN_MIGRATIONS", "").strip() == "0"
            or presence
        )
        raw_task = None if payload is None else payload.get("task")
        self.task = _image_task_view(raw_task)
        self.task_unreadable = raw_task is not None and self.task is None
        self.pending_ids = _image_pending_request_ids(self.requests_dir)
        # **恢复点门禁优先读顶层 ``backup_gate``**：它由更新器扫描备份目录得出，
        # 与 task 无关。只看 ``task.backup`` 会死锁——第一次更新之前根本没有 task，
        # 而第一次更新又要先过这道门（全新镜像部署的网页更新永远"不可安装"）。
        # 只有状态文件里**没有**这个字段时才回退到 ``task.backup``（兼容旧更新器
        # 写出的状态文件）。字段存在但内容不认识/自相矛盾时**不**回退：门禁是
        # fail closed 的，宁可说"没有恢复点"也不拿旧任务记录蒙混过关。
        raw_gate = None if payload is None else payload.get("backup_gate")
        self.backup_gate_present = raw_gate is not None
        self.backup_gate = _image_backup_gate_view(raw_gate) if raw_gate is not None else None
        if self.backup_gate_present:
            self.restore_point = _image_gate_backup(self.backup_gate)
        else:
            task_backup = None if self.task is None else self.task.get("backup")
            self.restore_point = _image_restore_point(
                task_backup if isinstance(task_backup, dict) else None
            )
        self.updater = self._updater_view()
        raw_catalog = None if payload is None else payload.get("release_catalog")
        raw_targets = None if payload is None else payload.get("available_targets")
        raw_recommended = None if payload is None else payload.get("recommended_target")
        self.catalog = _image_catalog_view(raw_catalog)
        self.available_targets = _image_available_targets(raw_targets, current_version())
        self.target = self._resolve_target(raw_recommended)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.update_enabled)

    def _updater_view(self) -> dict[str, object]:
        state = "unknown"
        version: str | None = None
        updated_at: str | None = None
        protocol: int | None = None
        if self._status is not None:
            raw_updater = self._status.get("updater")
            if isinstance(raw_updater, dict):
                raw_state = raw_updater.get("state")
                if isinstance(raw_state, str) and raw_state in IMAGE_UPDATER_STATES:
                    state = raw_state
                version = _image_text(raw_updater.get("version"), max_length=32)
            raw_protocol = self._status.get("protocol")
            if isinstance(raw_protocol, int) and not isinstance(raw_protocol, bool):
                protocol = raw_protocol
            updated_at = _image_timestamp(self._status.get("updated_at"))
        age = update_protocol.seconds_since(updated_at, now=self._now)
        online = (
            self.status_state == _IMAGE_STATUS_OK
            and protocol == IMAGE_UPDATER_PROTOCOL
            and state != "unknown"
            and age is not None
            and 0 <= age <= IMAGE_UPDATER_ONLINE_SECONDS
        )
        return {
            "online": online,
            "state": state,
            "protocol": protocol,
            "version": version,
            "updated_at": updated_at,
            "age_seconds": None if age is None else round(age, 1),
        }

    def _resolve_target(self, raw_recommended: object) -> dict[str, object] | None:
        """``view.target``：更新器推荐的那条，但必须与兼容目标逐字段一致。

        ``recommended_target`` 与 ``available_targets`` 来自同一个状态文件；如果一个
        说"推荐"另一个说"不兼容/不存在"，正确的做法不是二选一，而是**不盲信推荐**
        并按同一份可用目标重新算最大兼容版本——这样「可安装 ⇔ 有目标」永远成立，
        页面不会出现"后端说能装、但没有目标可提交"的自相矛盾状态。
        """
        if isinstance(raw_recommended, dict):
            version = raw_recommended.get("version")
            digest = _image_digest(raw_recommended.get("digest"))
            commit = _image_commit(raw_recommended.get("commit"))
            has_migration = raw_recommended.get("has_migration")
            for item in self.available_targets:
                if (
                    item["compatible"] is True
                    and item["version"] == version
                    and item["digest"] == digest
                    and item["commit"] == commit
                    and item["has_migration"] is has_migration
                ):
                    return item
        return _image_max_compatible(self.available_targets)

    def processed(self, request_id: str) -> bool:
        """该任务 ID 是否已被使用过（在目录里，或已被更新器归档）。"""
        if request_id in self.pending_ids:
            return True
        archive = self.requests_dir / IMAGE_UPDATE_REQUEST_ARCHIVE
        for name in (
            f"claimed-{request_id}{IMAGE_UPDATE_REQUEST_SUFFIX}",
            f"{IMAGE_UPDATE_REQUEST_PREFIX}{request_id}{IMAGE_UPDATE_REQUEST_SUFFIX}",
        ):
            try:
                if (archive / name).is_file():
                    return True
            except OSError:
                # 读不到归档目录就按「可能已用过」处理（fail closed）。
                return True
        return False

    def pending_request(self) -> dict[str, object] | None:
        """当前排队中的请求（更新器按名字排序处理第一个）。"""
        if not self.pending_ids:
            return None
        request_id = self.pending_ids[0]
        path = self.requests_dir / f"{IMAGE_UPDATE_REQUEST_PREFIX}{request_id}.json"
        payload = update_protocol.read_json_file(path)
        readable = (
            payload is not None
            and payload.get("request_id") == request_id
            and _image_digest(payload.get("target_digest")) is not None
        )
        return {
            "request_id": request_id,
            "readable": readable,
            "target_digest": _image_digest(payload.get("target_digest")) if payload else None,
            "created_at": _image_timestamp(payload.get("created_at")) if payload else None,
        }

    def reason(self, *, role: str, request_id: str | None = None) -> str | None:
        """返回第一个不适用的 reason；全部通过返回 None。

        顺序是刻意排的：拓扑 → 开关 → 权限 → 通道目录 → 状态 → 协议 → 人工门禁
        → 进行中/重复 → 离线 → 恢复点 → 当前镜像。已经明确「进行中」或「重复」
        时不再报「离线」——那会让正在跑任务的更新器看起来像是死了。
        """
        if not self.image_mode:
            return "image_mode_required"
        if not self.enabled:
            return "update_disabled"
        if role != "owner":
            return "not_owner"
        if not update_protocol.directory_ready(
            self.status_dir
        ) or not update_protocol.directory_ready(self.requests_dir):
            return "update_channel_unavailable"
        if self.status_state == _IMAGE_STATUS_MISSING:
            return "updater_status_missing"
        if self.status_state == _IMAGE_STATUS_INVALID or self.task_unreadable:
            return "updater_status_invalid"
        if self.updater["protocol"] != IMAGE_UPDATER_PROTOCOL:
            return "protocol_unsupported"
        if self.updater["state"] == "needs_manual":
            return "manual_required"
        if self.updater["state"] == "busy":
            return "update_in_progress"
        if request_id is not None and self.processed(request_id):
            return "duplicate_request_id"
        if self.task is not None and self.task.get("state") == "running":
            return "update_in_progress"
        if self.pending_ids:
            return "update_in_progress"
        if not self.updater["online"]:
            return "updater_offline"
        if self.restore_point is None:
            return "backup_gate_missing"
        if current_image_ref() is None or _image_commit(build_commit()) is None:
            return "current_image_unknown"
        if self.target is None:
            # 没有可用目标就**不可安装**：否则网页只能提交一个由调用方编的摘要。
            return "no_available_target"
        return None

    def view(self, *, role: str) -> dict[str, object]:
        """``GET /v1/version/image-update`` 的响应体（只读、白名单、可解释）。"""
        reason = self.reason(role=role)
        gate = self._backup_gate_view()
        return {
            "schema_version": "v1",
            "image_mode": self.image_mode,
            "enabled": self.enabled,
            "installable": reason is None,
            "reason": reason,
            "message": IMAGE_REASON_MESSAGES.get(reason) if reason is not None else None,
            "next_actions": list(IMAGE_REASON_NEXT_ACTIONS.get(reason, ()))
            if reason is not None
            else [],
            "current": {
                "digest": current_image_ref(),
                "commit": _image_commit(build_commit()),
            },
            "updater": self.updater,
            "backup_gate": gate,
            "catalog": self.catalog,
            "target": _image_target_payload(self.target),
            "available_targets": self.available_targets,
            "task": self.task,
            "request": self.pending_request(),
        }

    def _backup_gate_view(self) -> dict[str, object]:
        """``backup_gate`` 的响应视图：`source` 说明结论来自新字段还是旧任务记录。

        - ``source == "backup_gate"``：更新器扫描备份目录的结论（应有字段）；
        - ``source == "task"``：旧状态文件没有 ``backup_gate``，回退到任务记录；
        - ``source is None``：两处都没有可用事实（没有恢复点）。

        ``restore_point`` 一律用 API 自己核实后的结论，不用状态文件里的布尔值。
        """
        if self.backup_gate is not None:
            return {
                **self.backup_gate,
                "restore_point": self.restore_point is not None,
                "source": "backup_gate",
            }
        if self.backup_gate_present:
            # 字段存在但结构不认识：按「没有恢复点」表达，**不**回退 task.backup
            # （门禁是 fail closed 的；回退会让一份坏状态文件把放行条件变松）。
            return {
                "restore_point": False,
                "complete": False,
                "backup_id": None,
                "tombstones_state": None,
                "media_state": None,
                "keys_root": None,
                "keys_env_set": None,
                "reason": None,
                "reason_message": None,
                "checked_at": None,
                "source": "backup_gate",
            }
        backup: dict[str, object] | None = None
        if self.task is not None:
            raw_backup = self.task.get("backup")
            backup = raw_backup if isinstance(raw_backup, dict) else None
        return {
            "restore_point": self.restore_point is not None,
            "complete": bool(backup.get("complete")) if backup else False,
            "backup_id": backup.get("id") if backup else None,
            "tombstones_state": backup.get("tombstones_state") if backup else None,
            "media_state": backup.get("media_state") if backup else None,
            "keys_root": backup.get("keys_root") if backup else None,
            "keys_env_set": backup.get("keys_env_set") if backup else None,
            "reason": None,
            "reason_message": None,
            "checked_at": None,
            "source": "task" if backup is not None else None,
        }


def _image_request_payload(
    submission: ImageUpdateSubmission, *, created_at: str
) -> dict[str, object]:
    """API 写进请求文件的**固定结构**（与更新器的 REQUEST_KEYS 对齐）。"""
    return {
        "schema": IMAGE_UPDATE_REQUEST_SCHEMA,
        "request_id": submission.request_id,
        "target_digest": submission.target_digest,
        "expected_current_digest": submission.expected_current_digest,
        "expected_current_commit": submission.expected_current_commit,
        "operator_confirmed": True,
        "created_at": created_at,
    }


@router.get("/version/image-update")
async def image_update_status(
    auth: CurrentAuth, settings: SettingsDep, response: Response
) -> dict[str, object]:
    """镜像更新通道视图：只读更新器状态，不可安装时给出码表原因与下一步。"""
    response.headers["Cache-Control"] = "no-store"
    return ImageUpdateChannel(settings).view(role=auth.user.role)


@router.post("/version/image-update-requests", status_code=201)
async def create_image_update_request(
    auth: CurrentAuth,
    settings: SettingsDep,
    request: Request,
    response: Response,
    session: DbSession,
) -> dict[str, object]:
    """owner 提交一次**镜像**更新请求；只原子写请求文件，不执行任何部署。

    顺序（每一步失败都 fail closed，且不写任何文件）：

    1. 所有者权限 → 来源/CSRF → 限流 → 最近登录；
    2. 请求体上限 + 固定格式（多余字段/非 digest/未确认一律 422）；
    3. 通道门禁（开关、更新器在线、协议、恢复点、当前镜像…）→ 409/403；
    4. 期望当前值与实测的 ``LING_COMMIT`` / ``LING_APP_IMAGE`` 逐项比对；
    5. 在 ``requests/.lock`` 内复查重复/进行中，再临时文件 + rename 原子写入。
    """
    response.headers["Cache-Control"] = "no-store"
    if auth.user.role != "owner":
        raise _image_error("not_owner")
    _require_update_headers(request)
    origin_problem = _origin_problem(request, settings)
    if origin_problem is not None:
        raise LingError(
            ErrorCode.forbidden,
            "更新请求来源校验失败",
            status_code=403,
            detail=origin_problem,
        )
    if not _image_limiter(request).allow(auth.user.id):
        raise _image_error("rate_limited")
    await _require_recent_login(auth, session)
    raw = await _read_update_body(request)
    try:
        submission = _parse_image_submission(raw)
    except _SubmissionError as exc:
        raise _image_error(exc.detail) from exc

    channel = ImageUpdateChannel(settings)
    gate_reason = channel.reason(role=auth.user.role, request_id=submission.request_id)
    if gate_reason is not None:
        raise _image_error(gate_reason)

    current_commit = _image_commit(build_commit())
    if current_commit is None or submission.expected_current_commit != current_commit:
        raise _image_error("installed_version_changed")
    current_digest = current_image_ref()
    if current_digest is None or submission.expected_current_digest != current_digest:
        raise _image_error("installed_image_changed")
    if submission.target_digest == submission.expected_current_digest:
        # 更新器会以 already_on_target 拒绝空任务；API 先给出同样的结论。
        raise _image_error("already_on_target")
    if submission.target_digest not in {
        str(item["digest"]) for item in channel.available_targets if item["compatible"] is True
    }:
        # 目录之外的摘要一律拒绝：不能绕过可信发布目录提交任意 digest。
        # （更新器执行前还会用**自己**的目录再核一遍，两边都不放行。）
        raise _image_error("target_not_in_catalog")

    created_at = update_protocol.iso_now()
    payload = _image_request_payload(submission, created_at=created_at)
    target = channel.requests_dir / (
        f"{IMAGE_UPDATE_REQUEST_PREFIX}{submission.request_id}{IMAGE_UPDATE_REQUEST_SUFFIX}"
    )
    lock = update_protocol.FileLock(
        channel.requests_dir / ".lock",
        blocking=True,
        timeout=SUBMIT_LOCK_TIMEOUT_SECONDS,
    )
    try:
        acquired = lock.acquire()
    except update_protocol.ProtocolError as exc:
        raise _image_error("update_channel_unavailable") from exc
    if not acquired:
        # 拿不到锁说明另一个提交正在写：按「进行中」拒绝，绝不并发覆盖。
        raise _image_error("update_in_progress")
    try:
        # 锁内复查：宿主靠 rename 认领请求，覆盖会吞掉在途任务。
        pending_now = _image_pending_request_ids(channel.requests_dir)
        if submission.request_id in pending_now or channel.processed(submission.request_id):
            raise _image_error("duplicate_request_id")
        if pending_now:
            raise _image_error("update_in_progress")
        try:
            update_protocol.atomic_write_json(target, payload, mode=0o640)
        except update_protocol.ProtocolError as exc:
            raise _image_error("update_channel_unavailable") from exc
    finally:
        lock.release()
    return {
        "schema_version": "v1",
        "request_id": submission.request_id,
        "state": "requested",
        "state_message": "更新请求已写入，等待更新器接手",
        "target_digest": submission.target_digest,
        "expected_current_digest": submission.expected_current_digest,
        "expected_current_commit": submission.expected_current_commit,
        "created_at": created_at,
    }
