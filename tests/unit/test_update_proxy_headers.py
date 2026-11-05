"""受控更新接口的 POST 会比对 Origin 与 Host，反代不能把外部端口抹掉。

**注意**：`deploy/nginx.conf` / `nginx-tls.conf` 现在只是**给自建前置反代的
示例配置**，compose 里的 `edge` 服务已经删除、api 直连 6520。这两份配置仍然
必须正确——用户照抄它们上 TLS 时，端口被抹掉就会让更新请求 403。所以下面的
静态断言继续有效。

历史上 edge 对外发布在 18080（明文）/ 8443（TLS）这类非默认端口上。
`proxy_set_header Host $host` 会把端口丢掉（$host 只有主机名），
于是应用侧看到的 Host 是 `host` 而 Origin 是 `http://host:18080`，
same-origin 判定变成跨域，正常更新请求被 403 挡住。

修法是改用 `$http_host`（保留客户端请求头里的 Host，含端口）。
本测试只做静态断言，不需要 nginx / Docker（也不需要 compose 里真有 edge）：

1. 两份配置的 Host 头必须是 `$http_host`，且各只有一处；
2. 其他转发头（X-Real-IP / X-Forwarded-For / X-Forwarded-Proto）仍在——
   它们分别决定客户端 IP 与 cookie Secure / 回调地址，不能被顺手删掉；
3. TLS 版的 HTTP→HTTPS 跳转保持用 `$host`（跳转不需要端口，且不属本次改动）；
4. upstream 必须指到 `api:6520`（api 容器内固定监听端口），不能漂回旧的 8080。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
CONFIGS = ("nginx.conf", "nginx-tls.conf")

HOST_HEADER = re.compile(r"^\s*proxy_set_header\s+Host\s+(\S+)\s*;\s*$", re.MULTILINE)


@pytest.mark.parametrize("name", CONFIGS)
def test_proxy_host_keeps_external_port(name: str) -> None:
    """Host 必须转发 $http_host（含端口），而不是丢掉端口的 $host。"""
    text = (DEPLOY / name).read_text(encoding="utf-8")

    values = HOST_HEADER.findall(text)

    assert values == ["$http_host"], (
        f"{name} 的 proxy_set_header Host 应恰为 $http_host（保留 :18080/:8443 等外部端口），"
        f"实际为 {values}"
    )


@pytest.mark.parametrize("name", CONFIGS)
def test_forwarded_headers_still_present(name: str) -> None:
    """其余转发头不能因为改 Host 而被删掉。"""
    text = (DEPLOY / name).read_text(encoding="utf-8")

    for header, value in (
        ("X-Real-IP", "$remote_addr"),
        ("X-Forwarded-For", "$proxy_add_x_forwarded_for"),
        ("X-Forwarded-Proto", "$scheme"),
    ):
        assert re.search(
            rf"^\s*proxy_set_header\s+{header}\s+{re.escape(value)}\s*;\s*$",
            text,
            re.MULTILINE,
        ), f"{name} 缺少 proxy_set_header {header} {value}"


def test_tls_redirect_untouched() -> None:
    """TLS 版的跳转仍走 $host$request_uri（本次只改 proxy Host）。"""
    text = (DEPLOY / "nginx-tls.conf").read_text(encoding="utf-8")

    assert re.search(
        r"^\s*return\s+301\s+https://\$host\$request_uri\s*;\s*$", text, re.MULTILINE
    ), "nginx-tls.conf 的 HTTP→HTTPS 跳转不应被改动"


@pytest.mark.parametrize("name", CONFIGS)
def test_upstream_points_at_api_6520(name: str) -> None:
    """upstream 必须指到 `api:6520`，不能留删掉 edge 之前的 8080。

    两份配置开头都写着「upstream 指到 api 的 6520（不再是 8080）」——配置里
    却留 8080 的话，用户照着挂上去只会得到 502，而且很难想到是示例写错了。
    8080 是旧值，最容易在后续改动里被顺手抄回来，所以这里做静态锁定。
    """
    text = (DEPLOY / name).read_text(encoding="utf-8")

    assert "server api:6520;" in text, (
        f"{name} 的 upstream 必须指到 api:6520（api 容器内固定端口），否则用户照抄会 502"
    )
    assert "server api:8080" not in text, (
        f"{name} 仍有旧的 api:8080 upstream：edge 已删除，api 在容器内监听 6520"
    )
