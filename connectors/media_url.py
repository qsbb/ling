"""媒体 URL 的**域名与端口**判定：OneBot 与微信两个连接器共用一份实现。

## 为什么单独一个模块

两个渠道的白名单判定原本各写一遍（`connectors/onebot/connector.py` 与
`connectors/wechat/connector.py`），修 SSRF 时两处都要改成
``host == h or host.endswith("." + h)``——同一段判定抄两遍，下一次只会
改到其中一处。判定规则集中在这里，两个连接器只负责各自的错误文案与
scheme 要求。

## 两条规则

1. **域名**：``host == h or host.endswith("." + h)``。
   **不能是裸 ``endswith``**：``evilqpic.cn`` 也以 ``qpic.cn`` 结尾，
   裸匹配会把它当成图床放行，于是任意域名都能变成读取/探测的跳板。
2. **端口**：只接受 scheme 的默认端口（http=80 / https=443），
   不写端口也接受（等价于默认端口）。``gchat.qpic.cn:8443`` 这类
   非标准端口一律拒绝——白名单里的域名是"图床"，腾讯的图床不会把
   媒体挂在 8443；而端口维度不设约束时，一台把腾讯域名反代到内网的
   机器就能用非标准端口把请求引到别处。
"""

from __future__ import annotations

from collections.abc import Collection
from urllib.parse import ParseResult

# 各 scheme 的默认端口。白名单只认这些端口（不写端口即默认端口）。
DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def host_allowed(host: str, allowed_hosts: Collection[str]) -> bool:
    """``host`` 是否命中白名单（本身相等，或是以 ``.<域名>`` 结尾的子域）。"""
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


def port_is_standard(parsed: ParseResult) -> bool:
    """端口是否是 scheme 的默认端口（没写端口也算）。

    ``parsed.port`` 在端口越界（如 ``:99999``）时会抛 ``ValueError``——
    那种 URL 不是"端口恰好等于默认值"，按拒绝处理，不要让它顺着异常
    路径变成 500。
    """
    default = DEFAULT_PORTS.get(parsed.scheme)
    if default is None:
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return port is None or port == default


__all__ = ["DEFAULT_PORTS", "host_allowed", "port_is_standard"]
