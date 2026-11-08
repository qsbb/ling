#!/usr/bin/env python3
"""GHCR 只读发现客户端（纯标准库，无第三方依赖）。

「有没有新版本」直接问 registry，不再经过任何发布目录
===================================================
数据流只有两跳：

1. ``GET /v2/qsbb/ling-api/tags/list`` —— 拿到全部 tag。``401`` 时按
   ``WWW-Authenticate`` 的 Bearer challenge 去 challenge 指定的授权服务取**匿名**
   token 后重试；分页按 ``Link: <...>; rel="next"`` 跟，最多 :data:`MAX_PAGES` 页；
   整个检查共用一条总超时（:data:`DEFAULT_TIMEOUT_SECONDS`）。
2. ``HEAD /v2/qsbb/ling-api/manifests/v<ver>`` —— 取 ``Docker-Content-Digest``。
   ``Accept`` 同时带 OCI index / Docker manifest list / OCI manifest / Docker
   manifest 四种；HEAD 不可用（``405``/``501``，某些代理会这样）才退回 ``GET``
   （**限流读**：最多读 :data:`MAX_MANIFEST_BYTES` 字节，读完即止）。多架构 index
   再核一层「宿主平台存在」，缺了就是 :data:`REASON_PLATFORM_MISSING`。

为什么不用 ``docker manifest inspect``
-------------------------------------
它的输出里**没有 index 自己的摘要**（OCI index 文档不含自身哈希），旧实现只能
退而取「本机平台那层 manifest」的 digest；``buildx imagetools --raw`` 又要在只读
根、非 root、``HOME=/nonexistent`` 的更新器容器里初始化 builder。版本发现本来
也不该经过 docker daemon：这里只发 HTTPS 只读请求，装什么/拉什么仍由原有流水线
（``docker compose pull`` + publish 门禁）负责。

固定仓库，没有可配置项
======================
registry 主机与镜像名是常量，请求方（网页/API/请求文件）**无法**影响它们。唯一
的注入点是环境变量 :data:`REGISTRY_ENV`：只给测试把 base URL 指向本地 stub，
生产默认值写死 ``https://ghcr.io``（没有"用户填一个 registry"的入口）。

失败一律抛 :class:`RegistryError`，``code`` 是固定的原因码（写进 ``status.json``
的 ``release_check.reason``）；异常原文只进私有日志，绝不进状态文件。
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------- 固定身份

# 固定仓库：不接受任何配置（见文件头）。镜像引用仍由调用方用发行包的
# ``IMAGE_REPOSITORY`` 拼，这里只负责把 tag/digest 问出来。
REGISTRY_HOST = "ghcr.io"
REPOSITORY = "qsbb/ling-api"

# **只给测试用**的 base URL 注入点（例如 ``http://127.0.0.1:<port>``）。
# 生产不设这个变量 → ``https://ghcr.io``。它不接受镜像名/tag/仓库，只换主机。
REGISTRY_ENV = "LING_UPDATER_REGISTRY"

# 一次检查的总超时（秒）。分页与 token 都算在这条预算里。
DEFAULT_TIMEOUT_SECONDS = 8.0
# tags/list 最多跟几页。超过就**判失败**：没拿全的 tag 列表不能用来断言"已是最新"。
MAX_PAGES = 5
# 响应体上限（字节）。tags/list 正常只有几 KB；manifest index 也就几十 KB。
MAX_TAGS_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 512 * 1024
MAX_TOKEN_BYTES = 64 * 1024

# 稳定版 tag：``vMAJOR.MINOR.PATCH``（与设计 §4 一致）。下面 :func:`stable_version`
# 另外要求**规范化**（无前导零）：``v1.2.03`` 与 ``v1.2.3`` 会映射到同一个版本号，
# 两个 tag → 一个版本是歧义（到底装哪个摘要？），所以非规范写法一律忽略。
STABLE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# registry 的摘要（``Docker-Content-Digest``）。只认小写 hex 的 sha256。
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# ``Accept``：四种表示一起要，让 registry 按它自己的升级顺序给（OCI 优先）。
ACCEPT_MANIFEST = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
SINGLE_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)

# 原因码：**锁定**这 8 个（写进 ``release_check.reason``，API 按自己的码表翻译）。
REASON_UNREACHABLE = "registry_unreachable"
REASON_AUTH = "auth_failed"
REASON_RATE_LIMITED = "rate_limited"
REASON_TIMEOUT = "timeout"
REASON_NO_STABLE_TAGS = "no_stable_tags"
REASON_MANIFEST_FAILED = "manifest_failed"
REASON_DIGEST_MISSING = "digest_missing"
REASON_PLATFORM_MISSING = "platform_missing"
REASONS = frozenset(
    {
        REASON_UNREACHABLE,
        REASON_AUTH,
        REASON_RATE_LIMITED,
        REASON_TIMEOUT,
        REASON_NO_STABLE_TAGS,
        REASON_MANIFEST_FAILED,
        REASON_DIGEST_MISSING,
        REASON_PLATFORM_MISSING,
    }
)

# 请求的 User-Agent：GHCR 的日志与支持工单靠它认客户端；也让"这不是用户浏览器
# 直连"在抓包里一目了然。版本号只用于区分世代，不参与任何判定。
USER_AGENT = "ling-updater-registry/1"

# ``WWW-Authenticate: Bearer realm="...",service="...",scope="..."`` 的键值对。
_CHALLENGE_PARAM_RE = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)\s*=\s*"([^"]*)"')
# ``Link: <https://...>; rel="next"``（只认 next）。
_LINK_RE = re.compile(r"<([^>]+)>\s*;\s*rel\s*=\s*\"?next\"?", re.IGNORECASE)


class RegistryError(Exception):
    """一次 registry 查询失败的固定原因（``code`` 属于 :data:`REASONS`）。

    ``detail`` 只进**私有日志**：它可能带状态码/主机名，绝不写进 ``status.json``。
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code if code in REASONS else REASON_UNREACHABLE
        self.detail = detail


@dataclass(frozen=True)
class TagListing:
    """完整的稳定版 tag 列表（**只有拿全了才返回**）。"""

    # 按 semver 升序：``versions[i]`` 对应 ``tags[i]``。
    versions: tuple[tuple[int, int, int], ...]
    tags: tuple[str, ...]

    @property
    def count(self) -> int:
        return len(self.tags)

    @property
    def latest_version(self) -> str | None:
        return None if not self.versions else version_text(self.versions[-1])


@dataclass(frozen=True)
class Manifest:
    """一个 tag 现在指向的 manifest。"""

    version: str
    # registry 给的摘要（``Docker-Content-Digest``）。它就是安装时固定的那个值。
    digest: str
    # ``index`` / ``single`` / ``unknown``（registry 没给 Content-Type 且是 HEAD）。
    kind: str
    # 是否真的核过宿主平台。``False`` 表示单平台 manifest 或无法判断——那种情况
    # 由拉取后的 image inspect 复核（``recheck_pulled_image``），不在这里猜。
    platform_checked: bool


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


def version_text(version: Sequence[int]) -> str:
    return ".".join(str(int(part)) for part in version)


def stable_version(tag: object) -> tuple[int, int, int] | None:
    """``v1.2.3`` -> ``(1, 2, 3)``；``latest``/``sha-*``/预发布/非规范写法 -> ``None``。

    "非规范"指前导零（``v01.2.3`` / ``v1.2.03``）：同一个版本号会对应多个 tag，
    那就有"到底哪个摘要才是这一版"的歧义，直接不认（见 :data:`STABLE_TAG_RE`）。
    """
    if not isinstance(tag, str):
        return None
    match = STABLE_TAG_RE.fullmatch(tag)
    if match is None:
        return None
    parts = tuple(int(part) for part in match.groups())
    if tag != f"v{version_text(parts)}":
        return None
    return parts


def default_base_url() -> str:
    """registry 的 base URL：生产写死 ``https://ghcr.io``，测试可注入本地 stub。"""
    injected = os.environ.get(REGISTRY_ENV, "").strip()
    return injected.rstrip("/") if injected else f"https://{REGISTRY_HOST}"


class RegistryClient:
    """一次检查用一个实例：token 与超时预算都在实例里。

    ``timeout`` 是**总量**（不是每个请求）：一个实例共用一条预算，
    :meth:`stable_tags` 之后 :meth:`manifest_digest` 只剩"总预算里剩下的那点"。
    超预算一律 ``RegistryError(REASON_TIMEOUT)``——宁可这次明说"超时"，也不要让
    一次检查在慢网络上挂几十秒（它是常驻进程的一部分）。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = (base_url or default_base_url()).rstrip("/")
        self.timeout = float(timeout)
        self._token: str | None = None
        self._deadline: float | None = None
        self._opener = urllib.request.build_opener()

    # ------------------------------------------------------------ 时间预算
    def _start(self) -> None:
        """起一次检查的总预算（**每实例只起一次**）。

        :meth:`stable_tags` 与 :meth:`manifest_digest` 都调它，但只有第一次真正
        落定 deadline：一次检查（列 tag + 解析 manifest，可能含 token 与分页）共用
        一条 8 秒的总预算，而不是"每个请求各 8 秒"。
        """
        if self._deadline is None:
            self._deadline = time.monotonic() + self.timeout

    def _remaining(self) -> float:
        if self._deadline is None:
            return self.timeout
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise RegistryError(REASON_TIMEOUT, "registry 检查超过总超时")
        return left

    # ------------------------------------------------------------ 单次请求
    @staticmethod
    def _headers(source: Any) -> dict[str, str]:
        items = getattr(source, "headers", None)
        if items is None:
            return {}
        try:
            return {str(key).lower(): str(value) for key, value in items.items()}
        except (AttributeError, TypeError):  # pragma: no cover - 非 HTTP 响应
            return {}

    def _read(self, stream: Any, limit: int) -> bytes:
        """限流读：最多读 ``limit`` 字节；读超限就判失败（partial JSON 不可解析）。"""
        if limit <= 0:
            return b""
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = stream.read(min(65536, limit - total))
            except TimeoutError as error:
                raise RegistryError(REASON_TIMEOUT, "读取 registry 响应超时") from error
            except (OSError, http.client.HTTPException) as error:
                raise RegistryError(
                    REASON_UNREACHABLE, f"读取 registry 响应失败：{type(error).__name__}"
                ) from error
            if not chunk:
                return b"".join(chunks)
            chunks.append(bytes(chunk))
            total += len(chunk)
            if total >= limit:
                raise RegistryError(
                    REASON_UNREACHABLE, f"registry 响应超过上限 {limit} 字节"
                )

    def _fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        accept: str | None = None,
        read_limit: int = 0,
        allow_retry: bool = True,
    ) -> Response:
        """发一次请求；``401`` 只在 ``allow_retry`` 时按 challenge 取 token 重试一次。

        ``allow_retry=False`` 只关掉"再去换一次 token"：已经拿到的 token 仍然会随
        请求发出去（否则换完 token 的那次重试又会被拒，等于永远 401）。
        """
        request = urllib.request.Request(url, method=method)
        request.add_header("User-Agent", USER_AGENT)
        if accept:
            request.add_header("Accept", accept)
        if self._token:
            request.add_header("Authorization", f"Bearer {self._token}")
        try:
            with self._opener.open(request, timeout=self._remaining()) as response:
                body = self._read(response, read_limit) if method != "HEAD" else b""
                return Response(int(response.status), self._headers(response), body)
        except urllib.error.HTTPError as error:
            headers = self._headers(error)
            # 401：只有"带认证的那次"才去换 token，换完的那次不再递归（防死循环）。
            if error.code == 401 and allow_retry:
                error.close()
                self._authorize(headers.get("www-authenticate", ""))
                return self._fetch(
                    url,
                    method=method,
                    accept=accept,
                    read_limit=read_limit,
                    allow_retry=False,
                )
            try:
                body = self._read(error, read_limit) if method != "HEAD" else b""
            finally:
                error.close()
            return Response(int(error.code), headers, body)
        except TimeoutError as error:
            raise RegistryError(REASON_TIMEOUT, "registry 请求超时") from error
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", None)
            if isinstance(reason, TimeoutError):
                raise RegistryError(REASON_TIMEOUT, "registry 请求超时") from error
            raise RegistryError(
                REASON_UNREACHABLE, f"registry 不可达：{type(reason).__name__}"
            ) from error
        except (OSError, http.client.HTTPException) as error:
            raise RegistryError(
                REASON_UNREACHABLE, f"registry 请求失败：{type(error).__name__}"
            ) from error

    def _authorize(self, challenge: str) -> None:
        """按 Bearer challenge 取**匿名** token（只读 pull 作用域）。"""
        scheme, _, params_text = challenge.partition(" ")
        if scheme.strip().lower() != "bearer":
            raise RegistryError(REASON_AUTH, "registry 要求不支持的认证方案")
        params = dict(_CHALLENGE_PARAM_RE.findall(params_text))
        realm = params.get("realm") or f"{self.base_url}/token"
        service = params.get("service") or REGISTRY_HOST
        scope = params.get("scope") or f"repository:{REPOSITORY}:pull"
        query = urllib.parse.urlencode({"scope": scope, "service": service})
        separator = "&" if "?" in realm else "?"
        response = self._fetch(
            f"{realm}{separator}{query}",
            method="GET",
            accept="application/json",
            read_limit=MAX_TOKEN_BYTES,
            allow_retry=False,
        )
        if response.status in (401, 403):
            raise RegistryError(REASON_AUTH, f"token 服务拒绝：HTTP {response.status}")
        if response.status == 429:
            raise RegistryError(REASON_RATE_LIMITED, "token 服务限流")
        if response.status != 200:
            raise RegistryError(REASON_AUTH, f"token 服务返回 HTTP {response.status}")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RegistryError(REASON_AUTH, "token 响应不是 JSON") from error
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            # GHCR 用 ``token``；distribution 规范里也有 ``access_token`` 写法。
            token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RegistryError(REASON_AUTH, "token 响应里没有 token")
        self._token = token

    @staticmethod
    def _raise_for_status(response: Response, *, what: str) -> None:
        status = response.status
        if status == 200:
            return
        if status in (401, 403):
            raise RegistryError(REASON_AUTH, f"{what} 返回 HTTP {status}")
        if status == 429:
            raise RegistryError(REASON_RATE_LIMITED, f"{what} 被限流（HTTP 429）")
        # 其余（404/5xx/代理错误页…）一律归"这次没查成"：列表类失败**不能**被
        # 读成"没有新版"（设计 §6 前两行）。
        raise RegistryError(REASON_UNREACHABLE, f"{what} 返回 HTTP {status}")

    # ------------------------------------------------------------ tag 列表
    @staticmethod
    def _next_link(response: Response) -> str | None:
        link = response.headers.get("link") or ""
        match = _LINK_RE.search(link)
        return match.group(1).strip() if match else None

    def _absolute(self, url: str) -> str:
        """只允许同 base 或相对路径的下一页：不接受被指向别的主机。"""
        absolute = urllib.parse.urljoin(f"{self.base_url}/", url)
        if not absolute.startswith(f"{self.base_url}/"):
            raise RegistryError(
                REASON_UNREACHABLE, "tags/list 的下一页指向了别的 registry"
            )
        return absolute

    def stable_tags(self) -> TagListing:
        """拿**全部** tag 后按 semver 排序；一处失败就抛（绝不返回部分结果）。

        "部分结果"是最危险的：少了最后一页就可能把旧版本当成最新版。所以这里
        宁可在第 4 页失败，也不返回前 3 页。
        """
        self._start()
        url = f"{self.base_url}/v2/{REPOSITORY}/tags/list"
        collected: dict[tuple[int, int, int], str] = {}
        pages = 0
        while url:
            pages += 1
            if pages > MAX_PAGES:
                raise RegistryError(
                    REASON_UNREACHABLE, f"tags/list 超过 {MAX_PAGES} 页（未取全）"
                )
            response = self._fetch(
                url, method="GET", accept="application/json", read_limit=MAX_TAGS_BYTES
            )
            self._raise_for_status(response, what="tags/list")
            try:
                payload = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise RegistryError(REASON_UNREACHABLE, "tags/list 不是 JSON") from error
            if not isinstance(payload, dict):
                raise RegistryError(REASON_UNREACHABLE, "tags/list 不是 JSON 对象")
            tags = payload.get("tags")
            if tags is not None:
                if not isinstance(tags, list):
                    raise RegistryError(REASON_UNREACHABLE, "tags/list 的 tags 不是数组")
                for tag in tags:
                    version = stable_version(tag)
                    if version is None or version in collected:
                        continue
                    collected[version] = tag
            next_link = self._next_link(response)
            url = self._absolute(next_link) if next_link else ""
        if not collected:
            raise RegistryError(
                REASON_NO_STABLE_TAGS, "registry 里没有规范的稳定版 tag（vX.Y.Z）"
            )
        versions = tuple(sorted(collected))
        return TagListing(versions=versions, tags=tuple(collected[v] for v in versions))

    # ------------------------------------------------------------ manifest
    def _manifest_kind(self, response: Response) -> str:
        content_type = (response.headers.get("content-type") or "").split(";")[0]
        content_type = content_type.strip().lower()
        if content_type in INDEX_MEDIA_TYPES:
            return "index"
        if content_type in SINGLE_MEDIA_TYPES:
            return "single"
        # Content-Type 缺失/不认识（代理常见）：退回 body 里的 mediaType。HEAD 没有
        # body 时只好返回 unknown —— 平台留给拉取后的 image inspect 复核。
        try:
            payload = json.loads(response.body.decode("utf-8")) if response.body else None
        except (UnicodeDecodeError, ValueError):
            payload = None
        media_type = payload.get("mediaType") if isinstance(payload, dict) else None
        if isinstance(media_type, str):
            if media_type in INDEX_MEDIA_TYPES:
                return "index"
            if media_type in SINGLE_MEDIA_TYPES:
                return "single"
        return "unknown"

    @staticmethod
    def _assert_platform(body: bytes, host_arch: str) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RegistryError(REASON_MANIFEST_FAILED, "index 不是 JSON") from error
        manifests = payload.get("manifests") if isinstance(payload, dict) else None
        if not isinstance(manifests, list):
            raise RegistryError(REASON_MANIFEST_FAILED, "index 里没有 manifests")
        for item in manifests:
            if not isinstance(item, dict):
                continue
            platform = item.get("platform")
            if not isinstance(platform, dict):
                continue
            if platform.get("os") == "linux" and platform.get("architecture") == host_arch:
                return
        raise RegistryError(
            REASON_PLATFORM_MISSING, f"index 里没有 linux/{host_arch} 的清单"
        )

    def _get_manifest(self, url: str) -> Response:
        response = self._fetch(
            url, method="GET", accept=ACCEPT_MANIFEST, read_limit=MAX_MANIFEST_BYTES
        )
        if response.status in (401, 403):
            raise RegistryError(REASON_AUTH, f"manifest 返回 HTTP {response.status}")
        if response.status == 429:
            raise RegistryError(REASON_RATE_LIMITED, "manifest 被限流（HTTP 429）")
        if response.status == 404:
            raise RegistryError(REASON_MANIFEST_FAILED, "tag 不存在（HTTP 404）")
        if response.status != 200:
            raise RegistryError(
                REASON_MANIFEST_FAILED, f"manifest 返回 HTTP {response.status}"
            )
        return response

    def manifest_digest(self, version: str, *, host_arch: str) -> Manifest:
        """``v<version>`` 现在指向的摘要（多架构 index 再核宿主平台）。"""
        self._start()
        url = f"{self.base_url}/v2/{REPOSITORY}/manifests/v{version}"
        response = self._fetch(url, method="HEAD", accept=ACCEPT_MANIFEST, read_limit=0)
        if response.status in (405, 501):
            # 有的 registry/代理不支持 HEAD：退回 GET，**限流读**（上限见常量）。
            response = self._get_manifest(url)
        elif response.status in (401, 403):
            raise RegistryError(REASON_AUTH, f"manifest 返回 HTTP {response.status}")
        elif response.status == 429:
            raise RegistryError(REASON_RATE_LIMITED, "manifest 被限流（HTTP 429）")
        elif response.status == 404:
            raise RegistryError(REASON_MANIFEST_FAILED, "tag 不存在（HTTP 404）")
        elif response.status != 200:
            raise RegistryError(
                REASON_MANIFEST_FAILED, f"manifest 返回 HTTP {response.status}"
            )
        digest = (response.headers.get("docker-content-digest") or "").strip()
        if not digest and response.body:
            # GET 回退时会话里已经有整段字节：按内容算（OCI 的摘要定义就是内容哈希）。
            digest = "sha256:" + hashlib.sha256(response.body).hexdigest()
        if DIGEST_RE.fullmatch(digest) is None:
            raise RegistryError(
                REASON_DIGEST_MISSING, "manifest 响应没有合法的 Docker-Content-Digest"
            )
        kind = self._manifest_kind(response)
        platform_checked = False
        if kind == "index":
            body = response.body
            if not body:
                body = self._get_manifest(url).body
            self._assert_platform(body, host_arch)
            platform_checked = True
        return Manifest(
            version=version,
            digest=digest,
            kind=kind,
            platform_checked=platform_checked,
        )

