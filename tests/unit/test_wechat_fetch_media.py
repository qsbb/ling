"""微信连接器 `fetch_media` 的域名/端口白名单：**回归锁**。

## 为什么单独一个文件

`WeChatConnector` 原来**一个测试都没有**（`grep WeChatConnector tests/` 为空）。
`fetch_media` 的白名单修复（裸 `endswith("qpic.cn")` → `host == h or
host.endswith("." + h)`）因此完全没有覆盖：一旦回退，测试不会红。

裸 `endswith` 的后果是具体的：`evilqpic.cn` 同样以 `qpic.cn` 结尾，会被当成
微信图床放行——任意域名都能变成读取/探测的跳板（SSRF）。所以这四条用例是
**安全回归**，不是"顺便测一下"：

- `evilqpic.cn` 被拒
- `qpic.cn` 放行
- `gchat.qpic.cn`（子域）放行
- `qpic.cn.evil.com` 被拒

另外锁住端口约束：白名单只校验 host 时 `gchat.qpic.cn:8443` 会被放行，
而微信图床只有 443。判定实现在 `connectors/media_url.py`，与 OneBot 共用。

这些用例不需要数据库、不需要网络：`WeChatConnector` 接受注入的 `http`，
这里给一个只记录调用并返回固定响应的假客户端。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from connectors.wechat.connector import WeChatAccountType, WeChatConfig, WeChatConnector

from ling.ports.channel import ChannelError, ChannelErrorKind


@dataclass
class _FakeResponse:
    status_code: int = 200
    content: bytes = b"png-bytes"
    headers: dict[str, str] = field(
        default_factory=lambda: {"content-type": "image/png"}
    )


class _FakeHttp:
    """只记下被请求的 URL；真实请求一次都不发（否则测试会依赖外网）。"""

    def __init__(self, response: _FakeResponse | None = None) -> None:
        self.response = response or _FakeResponse()
        self.calls: list[str] = []

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:  # noqa: ANN401
        self.calls.append(url)
        return self.response


def _connector(http: Any = None) -> WeChatConnector:  # noqa: ANN401
    return WeChatConnector(
        WeChatConfig(
            account_type=WeChatAccountType.official_account,
            app_id="wx-app",
            app_secret="wx-secret",
        ),
        http=http,
    )


# ------------------------------------------------------------------ 域名白名单


@pytest.mark.asyncio
async def test_fetch_media_rejects_suffix_only_lookalike() -> None:
    """`evilqpic.cn` **以 qpic.cn 结尾**，但不是它的子域——必须拒。

    这是裸 `endswith("qpic.cn")` 的经典绕过：只看"结尾像"就会把任意域名
    当成微信图床，于是 `fetch_media` 变成可被任意 URL 驱动的读取/探测跳板。
    """
    http = _FakeHttp()
    connector = _connector(http)

    with pytest.raises(ChannelError) as exc:
        await connector.fetch_media("https://evilqpic.cn/a.png")

    assert exc.value.kind is ChannelErrorKind.rejected
    assert "evilqpic.cn" in exc.value.message
    assert http.calls == [], "被拒的地址竟然发出了真实请求"


@pytest.mark.asyncio
async def test_fetch_media_allows_bare_host() -> None:
    """白名单根域名本身要放行（`qpic.cn` 就是图床域，不是只有子域能用）。"""
    http = _FakeHttp()
    connector = _connector(http)

    data, mime = await connector.fetch_media("https://qpic.cn/a.png")

    assert data == b"png-bytes"
    assert mime == "image/png"
    assert http.calls == ["https://qpic.cn/a.png"]


@pytest.mark.asyncio
async def test_fetch_media_allows_subdomain() -> None:
    """`gchat.qpic.cn` 是真实图床子域，必须放行。

    判定用 `host.endswith("." + h)`，所以子域自动包含——这条与上一条一起
    证明"拒 evilqpic.cn"不是靠收紧成"只允许根域名"来实现的（那会把
    正常图片全挡掉）。
    """
    http = _FakeHttp()
    connector = _connector(http)

    data, mime = await connector.fetch_media("https://gchat.qpic.cn/a.png")

    assert data == b"png-bytes"
    assert mime == "image/png"
    assert http.calls == ["https://gchat.qpic.cn/a.png"]


@pytest.mark.asyncio
async def test_fetch_media_rejects_host_suffix_hijack() -> None:
    """`qpic.cn.evil.com`：白名单域名出现在**前缀**位置，实际主机是 evil.com。"""
    http = _FakeHttp()
    connector = _connector(http)

    with pytest.raises(ChannelError) as exc:
        await connector.fetch_media("https://qpic.cn.evil.com/a.png")

    assert exc.value.kind is ChannelErrorKind.rejected
    assert http.calls == []


@pytest.mark.asyncio
async def test_fetch_media_rejects_plain_http() -> None:
    """微信媒体只走 https：明文传输等于把媒体内容暴露在链路上。"""
    connector = _connector(_FakeHttp())

    with pytest.raises(ChannelError) as exc:
        await connector.fetch_media("http://qpic.cn/a.png")

    assert exc.value.kind is ChannelErrorKind.rejected


# ------------------------------------------------------------------ 端口


@pytest.mark.asyncio
async def test_fetch_media_rejects_non_default_port() -> None:
    """`gchat.qpic.cn:8443` 必须拒：白名单域 + 非标准端口 = 可被引到别处。

    只校验 host 时这条会被放行（实测过）。端口维度的约束补上之后，
    一台把该域名反代到内网的机器就不能借 8443 把请求引过去。
    """
    http = _FakeHttp()
    connector = _connector(http)

    with pytest.raises(ChannelError) as exc:
        await connector.fetch_media("https://gchat.qpic.cn:8443/a.png")

    assert exc.value.kind is ChannelErrorKind.rejected
    assert http.calls == []


@pytest.mark.asyncio
async def test_fetch_media_allows_explicit_443() -> None:
    """显式写 `:443` 是默认端口，不是"非标准端口"，要放行。

    这条防的是"一刀切拒绝所有带冒号的 URL"——图床地址带不带端口都合法，
    只要端口是 https 的默认值。
    """
    http = _FakeHttp()
    connector = _connector(http)

    data, _mime = await connector.fetch_media("https://gchat.qpic.cn:443/a.png")

    assert data == b"png-bytes"
    assert http.calls == ["https://gchat.qpic.cn:443/a.png"]
