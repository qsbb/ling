"""Read one public webpage selected by the caller for an authorized lookup."""

from __future__ import annotations

import asyncio
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from urllib.parse import urljoin, urlsplit

import httpx

_MAX_URL_CHARS = 2048
_MAX_REDIRECTS = 3
_TOTAL_TIMEOUT_SECONDS = 6
_CONNECT_TIMEOUT_SECONDS = 2
_MAX_CONNECT_ADDRESSES = 4
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_TEXT_CHARS = 6000
_MAX_TITLE_CHARS = 200

_DEFAULT_PORTS = {"http": 80, "https": 443}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_REQUEST_HEADERS = {
    "Accept": "text/html, application/xhtml+xml, text/plain",
    "Accept-Encoding": "identity",
    "User-Agent": "Ling-Web-Reader",
}
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?")
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")
_IPV4_PROTOCOL_ASSIGNMENTS = IPv4Network("192.0.0.0/24")
_IPV6_GLOBAL_UNICAST = IPv6Network("2000::/3")

_ERROR_MESSAGES = {
    "invalid_url": "请使用有效的 HTTP(S) 网页地址和标准端口。",
    "restricted_url": "带查询参数或凭据的网页地址无法读取。",
    "blocked_address": "网页地址不在可访问的公网范围内。",
    "invalid_redirect": "网页返回了无效的跳转地址。",
    "redirect_limit": "网页跳转次数超过上限。",
    "access_denied": "网页限制访问，可能需要登录。",
    "not_found": "网页不存在或已被移除。",
    "http_error": "网页服务未返回可读取的响应。",
    "unsupported_content": "网页未提供可读取的静态 HTML 或纯文本。",
    "too_large": "网页响应超过读取大小上限。",
    "empty": "网页没有可读取的正文。",
    "timeout": "读取网页超时。",
    "network_error": "无法解析或连接网页地址。",
}


@dataclass(frozen=True, slots=True)
class PublicWebPage:
    """Untrusted text and its actual source URL after redirects."""

    title: str
    url: str
    text: str
    truncated: bool


class PublicWebReadError(ValueError):
    """A stable reason and a message safe to relay without URL or response data."""

    def __init__(self, reason: str) -> None:
        super().__init__(_ERROR_MESSAGES[reason])
        self.reason = reason


async def read_public_page(url: str) -> PublicWebPage:
    """Fetch an anonymous static page using GET and return its text as external data.

    The caller supplies a public article URL from the current authorized lookup.
    Page text does not authorize further requests or actions.
    """
    target = _checked_url(url)
    try:
        async with (
            asyncio.timeout(_TOTAL_TIMEOUT_SECONDS),
            httpx.AsyncHTTPTransport(
                trust_env=False,
                # IP-based pools do not distinguish different TLS names at one address.
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as transport,
        ):
            for hop in range(_MAX_REDIRECTS + 1):
                response = await _request_page(transport, target)
                try:
                    if response.status_code not in _REDIRECT_STATUSES:
                        return await _read_response(response, target)
                    if hop == _MAX_REDIRECTS:
                        raise PublicWebReadError("redirect_limit")
                    locations = response.headers.get_list("location")
                    if len(locations) != 1:
                        raise PublicWebReadError("invalid_redirect")
                    target = _checked_url(locations[0], base=target)
                finally:
                    await response.aclose()
    except (TimeoutError, httpx.TimeoutException):
        raise PublicWebReadError("timeout") from None
    except (OSError, httpx.HTTPError):
        raise PublicWebReadError("network_error") from None
    raise PublicWebReadError("redirect_limit")


def _checked_url(value: str, *, base: httpx.URL | None = None) -> httpx.URL:
    invalid = "invalid_url" if base is None else "invalid_redirect"
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_URL_CHARS
        or "\\" in value
        or any(ch.isspace() or ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value)
    ):
        raise PublicWebReadError(invalid)
    try:
        reference = urlsplit(value)
        url = httpx.URL(value if base is None else urljoin(str(base), value))
    except (ValueError, httpx.InvalidURL):
        raise PublicWebReadError(invalid) from None
    if "@" in reference.netloc or url.userinfo or reference.query or url.query:
        raise PublicWebReadError("restricted_url")
    port = _DEFAULT_PORTS.get(url.scheme)
    host = url.raw_host.decode("ascii").removesuffix(".")
    if (
        not host
        or "%" in host
        or port is None
        or url.port not in {None, port}
        or ((reference.scheme or value.startswith("//")) and not reference.netloc)
    ):
        raise PublicWebReadError(invalid)
    url = url.copy_with(host=host, query=None, fragment=None)
    if len(str(url)) > _MAX_URL_CHARS:
        raise PublicWebReadError(invalid)
    return url


def _is_public_address(address: IPv4Address | IPv6Address) -> bool:
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, IPv6Address):
        return address in _IPV6_GLOBAL_UNICAST and address.sixtofour is None
    # Keep the special-purpose block excluded on early Python 3.12 releases too.
    return address not in _IPV4_PROTOCOL_ASSIGNMENTS


async def _public_addresses(url: httpx.URL) -> tuple[IPv4Address | IPv6Address, ...]:
    host = url.raw_host.decode("ascii")
    try:
        literal = ip_address(host)
    except ValueError:
        if len(host) > 253 or not _DOMAIN.fullmatch(host) or host.endswith(_LOCAL_SUFFIXES):
            raise PublicWebReadError("blocked_address") from None
        # An absolute DNS name avoids the machine's DNS search suffixes.
        answers = await asyncio.get_running_loop().getaddrinfo(
            host + ".", _DEFAULT_PORTS[url.scheme], type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
        )
        addresses = tuple(dict.fromkeys(ip_address(answer[4][0]) for answer in answers))
    else:
        addresses = (literal,)
    if not addresses:
        raise PublicWebReadError("network_error")
    if any(not _is_public_address(address) for address in addresses):
        raise PublicWebReadError("blocked_address")
    return addresses[:_MAX_CONNECT_ADDRESSES]


async def _request_page(transport: httpx.AsyncHTTPTransport, url: httpx.URL) -> httpx.Response:
    addresses = await _public_addresses(url)
    for address in addresses:
        # Dial the inspected IP while retaining HTTP Host and TLS certificate identity.
        # The transport handles a single request and has no cookie jar or redirect policy.
        request = httpx.Request(
            "GET",
            url.copy_with(host=str(address)),
            headers={**_REQUEST_HEADERS, "Host": url.netloc.decode("ascii")},
            extensions={
                "sni_hostname": url.raw_host.decode("ascii"),
                "timeout": {"connect": _CONNECT_TIMEOUT_SECONDS},
            },
        )
        try:
            return await transport.handle_async_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if address == addresses[-1]:
                raise
    raise PublicWebReadError("network_error")


async def _read_response(response: httpx.Response, url: httpx.URL) -> PublicWebPage:
    status = response.status_code
    if status in {401, 403, 407}:
        raise PublicWebReadError("access_denied")
    if status in {404, 410}:
        raise PublicWebReadError("not_found")
    if status == 204:
        raise PublicWebReadError("empty")
    if status != 200:
        raise PublicWebReadError("http_error")

    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    disposition = response.headers.get("content-disposition", "").partition(";")[0].strip().lower()
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if (
        (media_type not in _HTML_TYPES and media_type != "text/plain")
        or disposition == "attachment"
        or encoding != "identity"
    ):
        raise PublicWebReadError("unsupported_content")

    try:
        declared_size = int(response.headers.get("content-length", "0"))
    except ValueError:
        raise PublicWebReadError("http_error") from None
    if declared_size > _MAX_RESPONSE_BYTES:
        raise PublicWebReadError("too_large")
    body = bytearray()
    async for chunk in response.aiter_raw():
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            raise PublicWebReadError("too_large")
        body.extend(chunk)

    try:
        decoded = body.decode(response.charset_encoding or "utf-8-sig", errors="replace")
    except (LookupError, UnicodeError):
        decoded = body.decode("utf-8-sig", errors="replace")
    title = ""
    if media_type in _HTML_TYPES:
        parser = _PageHTML()
        parser.feed(decoded)
        parser.close()
        title = " ".join("".join(parser.title).split())
        text = " ".join("".join(parser.main).split()) or " ".join("".join(parser.visible).split())
        if not text and parser.has_password_form:
            raise PublicWebReadError("access_denied")
    else:
        text = " ".join(decoded.split())
    if not text:
        raise PublicWebReadError("empty")
    return PublicWebPage(
        title=(title or url.host)[:_MAX_TITLE_CHARS],
        url=str(url),
        text=text[:_MAX_TEXT_CHARS],
        truncated=len(text) > _MAX_TEXT_CHARS,
    )


class _PageHTML(HTMLParser):
    _SECTIONS = {"main", "article"}
    _SKIP = {
        "aside", "canvas", "footer", "form", "iframe", "nav", "noscript", "object",
        "script", "select", "style", "svg", "template", "textarea",
    }
    _BLOCKS = {
        "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "ol", "p", "pre",
        "section", "table", "td", "th", "tr", "ul",
    }
    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.title: list[str] = []
        self.main: list[str] = []
        self.visible: list[str] = []
        self.has_password_form = False
        self._in_head = False
        self._in_title = False
        self._section_depth = 0
        self._skip_tag = ""
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth += 1
            if self._skip_tag == "form" and tag == "input":
                self.has_password_form |= (dict(attrs).get("type") or "").lower() == "password"
            return
        if tag in self._SKIP or any(key == "hidden" for key, _ in attrs):
            if tag not in self._VOID:
                self._skip_tag, self._skip_depth = tag, 1
            return
        if tag == "head":
            self._in_head = True
        elif tag == "title":
            self._in_title = True
        if tag in self._SECTIONS:
            self._section_depth += 1
        if tag in self._BLOCKS:
            self.handle_data(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_tag:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = ""
            return
        if tag in self._BLOCKS:
            self.handle_data(" ")
        if tag in self._SECTIONS:
            self._section_depth = max(0, self._section_depth - 1)
        if tag == "head":
            self._in_head = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_tag:
            return
        if self._in_title:
            self.title.append(data)
        elif not self._in_head:
            self.visible.append(data)
            if self._section_depth:
                self.main.append(data)
