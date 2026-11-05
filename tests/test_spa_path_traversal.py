"""SPA 兜底路由的路径穿越回归测试（``ling.app._mount_web``）。

## 背景

P0：``_mount_web`` 里那条手写兜底路由把 Starlette 捕获的**已解码**路径直接和
dist 拼接、只判 ``is_file()``，没有归一化、没有包含性校验。``../``（含
``%2e%2e``、``..%2f``）可以逃出 dist。生产实测
``curl --path-as-is http://<host>:6520/../../data/root.key`` 能拿到 AES 根密钥，
进而解密库里全部模型供应商密钥。

## 为什么不能只用 ``TestClient.get("/../x")``

httpx 的 URL 解析会做 RFC 3986 的 dot-segment 归一化（``httpx.URL("http://h/../x")``
的 ``raw_path`` 已经是 ``/x``），``TestClient`` 内部又用 ``urljoin`` 拼 base_url，
同样会吃掉字面量 ``..``。也就是说**未编码的 ``/../x`` 压根到不了服务端**，
这样的用例是假绿。所以这里两条路都覆盖：

- 百分号编码形式（``/%2e%2e/...``、``/..%2f...``）走 ``TestClient``：客户端
  看起来是个普通路径，uvicorn/Starlette 解码后 ``path`` 就是 ``../...``——
  这正是真实攻击面（浏览器、curl 都能发出来）；
- ``_as_is_get()`` 直接构造 ASGI scope，``raw_path`` 保留字面量 ``..``，等价于
  ``curl --path-as-is``；断言依据是 ``scope["path"]``（Starlette 用它做路由匹配）
  是解码后的 ``/../...``。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ling.app import create_app
from ling.config import Settings

INDEX_HTML = "<!doctype html><html><body>ling-index</body></html>"
ASSET_JS = "console.log('ling-app')"
SECRET = "TOP-SECRET-OUTSIDE-DIST"
ROOT_KEY_MATERIAL = "cm9vdC1rZXktbWF0ZXJpYWwtZm9yLXRlc3Q="


@dataclass
class Site:
    """一套真实 app + 临时 dist + dist 外的敏感文件。"""

    app: FastAPI
    client: TestClient
    dist: Path
    secret: Path
    root_key: Path


def _build_site(tmp_path: Path, *, symlink_dist: bool = False) -> Site:
    real = tmp_path / ("web-real" if symlink_dist else "web")
    (real / "assets").mkdir(parents=True)
    (real / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (real / "assets" / "app.js").write_text(ASSET_JS, encoding="utf-8")
    dist = real
    if symlink_dist:
        # 生产里 dist 是 bind mount，可能是符号链接；包含性判断必须对着
        # 解析后的真实目录做。
        dist = tmp_path / "web"
        dist.symlink_to(real, target_is_directory=True)

    # 与生产同形：容器里 /app/web 是 dist，/data 是数据卷，两者相邻。
    # 所以 ``<dist>/../secret.txt`` 和 ``<dist>/../data/root.key`` 就是
    # 真实可利用的落点。
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET, encoding="utf-8")
    root_key = tmp_path / "data" / "root.key"
    root_key.parent.mkdir(parents=True)
    root_key.write_text(ROOT_KEY_MATERIAL, encoding="utf-8")

    settings = Settings(
        env="test",
        data_dir=tmp_path / "data",
        web_dist=dist,
        root_key="test-root-key-spa-traversal",
        # 显式关掉出入许可，不依赖开发机 shell 里的 LING_ACCESS_MODE
        access_mode="off",
        access_list=frozenset(),
        cookie_secure=False,
        # 不绑端口、不连库：这个测试只打静态路由，不需要 lifespan
        onebot_ws_port=None,
        onebot_ws_url=None,
    )
    app = create_app(settings)
    return Site(app=app, client=TestClient(app), dist=dist, secret=secret, root_key=root_key)


@pytest.fixture()
def site(tmp_path: Path) -> Site:
    return _build_site(tmp_path)


@dataclass
class _Raw:
    status_code: int
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def _as_is_get(app: FastAPI, raw_path: str) -> _Raw:
    """按 ``curl --path-as-is`` 的语义发 GET：字面量 ``..`` 段保持原样。

    直接构造 ASGI scope，模拟 uvicorn：``raw_path`` 是原始字节，
    ``path`` 是解码后的路径。Starlette 用 ``path`` 做路由匹配，所以服务端
    看到的 ``full_path`` 正是 ``../...``——与真实请求一致。
    """
    events: list[dict[str, Any]] = []

    async def main() -> None:
        body_sent = False
        # 与 httpx ASGITransport / uvicorn 一致：body 读完后 receive 只能在
        # 响应结束时返回 ``http.disconnect``。文件响应会并发监听断开，直接
        # 立刻回 disconnect 会把还没发完的 body 取消掉。
        response_complete = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal body_sent
            if body_sent:
                await response_complete.wait()
                return {"type": "http.disconnect"}
            body_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            events.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                response_complete.set()

        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": unquote(raw_path),
            "raw_path": raw_path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 45678),
            "server": ("testserver", 80),
        }
        await app(scope, receive, send)  # type: ignore[arg-type]

    asyncio.run(main())

    start = next(event for event in events if event["type"] == "http.response.start")
    body = b"".join(
        event.get("body", b"") for event in events if event["type"] == "http.response.body"
    )
    return _Raw(status_code=int(start["status"]), body=body)


def _assert_blocked(response: _Raw) -> None:
    """被挡住：要么回 index.html（SPA 兜底），要么 404。绝不能是外部文件。"""
    assert response.status_code in (200, 404), response.status_code
    if response.status_code == 200:
        assert response.text == INDEX_HTML


def _assert_not_leaked(text: str, *forbidden: str) -> None:
    for payload in forbidden:
        assert payload not in text, f"泄露了 dist 之外的内容：{payload!r}"


def _encoded_dotdot(request_path: str) -> str:
    """把已经解出的 ``..`` 段编码，避免 httpx 在客户端把它归一化掉。"""
    return "/" + request_path.lstrip("/").replace("..", "%2e%2e")


# ---------------------------------------------------------------------------
# 1) 字面量 ``..``：curl --path-as-is 的语义
# ---------------------------------------------------------------------------


def test_literal_dotdot_cannot_read_sibling_file(site: Site) -> None:
    response = _as_is_get(site.app, "/../secret.txt")
    _assert_not_leaked(response.text, SECRET)
    _assert_blocked(response)


def test_literal_dotdot_assets_prefix(site: Site) -> None:
    # ``/assets`` 是 StaticFiles 挂载（自带越界保护）；这条断言防它退化
    response = _as_is_get(site.app, "/assets/../../secret.txt")
    _assert_not_leaked(response.text, SECRET)
    assert response.status_code in (200, 404)


# ---------------------------------------------------------------------------
# 2) 百分号编码：真实可发的攻击请求（服务端解码后才看到 ``..``）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/%2e%2e%2fsecret.txt",
        "/%2e%2e/%2e%2e/secret.txt",
    ],
)
def test_encoded_traversal_cannot_read_sibling_file(site: Site, path: str) -> None:
    response = site.client.get(path)
    _assert_not_leaked(response.text, SECRET)
    _assert_blocked(_Raw(response.status_code, response.content))


@pytest.mark.parametrize(
    "path",
    [
        "/assets/..%2f..%2fsecret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
    ],
)
def test_encoded_traversal_under_assets_prefix(site: Site, path: str) -> None:
    response = site.client.get(path)
    _assert_not_leaked(response.text, SECRET)


# ---------------------------------------------------------------------------
# 3) 生产落点：``<dist>/../data/root.key``（容器里 /app/web 与 /data 相邻）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/%2e%2e/data/root.key",
        "/%2e%2e%2fdata%2froot.key",
        "/..%2fdata%2froot.key",
    ],
)
def test_root_key_cannot_be_read(site: Site, path: str) -> None:
    response = site.client.get(path)
    _assert_not_leaked(response.text, ROOT_KEY_MATERIAL)
    _assert_blocked(_Raw(response.status_code, response.content))


def test_root_key_cannot_be_read_as_is(site: Site) -> None:
    response = _as_is_get(site.app, "/../data/root.key")
    _assert_not_leaked(response.text, ROOT_KEY_MATERIAL)
    _assert_blocked(response)


# ---------------------------------------------------------------------------
# 4) 深层穿越：按真实相对深度构造，确保修复前真的能到 /etc/hosts
# ---------------------------------------------------------------------------


def test_deep_traversal_cannot_read_etc_hosts(site: Site) -> None:
    hosts = Path("/etc/hosts")
    if not hosts.is_file():
        pytest.skip("本机没有 /etc/hosts")
    relative = os.path.relpath(hosts, site.dist)
    assert relative.startswith(".."), "夹具不在 /etc/hosts 下方，测不出穿越"

    response = site.client.get(_encoded_dotdot(relative.replace(os.sep, "/")))
    _assert_not_leaked(response.text, hosts.read_text(encoding="utf-8", errors="replace"))
    _assert_blocked(_Raw(response.status_code, response.content))


def test_literal_four_level_dotdot_cannot_read_etc_hosts(site: Site) -> None:
    response = _as_is_get(site.app, "/../../../../etc/hosts")
    if Path("/etc/hosts").is_file():
        _assert_not_leaked(
            response.text,
            Path("/etc/hosts").read_text(encoding="utf-8", errors="replace"),
        )
    _assert_blocked(response)


# ---------------------------------------------------------------------------
# 5) dist 内部的符号链接不能成为逃逸通道；dist 本身是符号链接时也一样
# ---------------------------------------------------------------------------


def test_symlink_inside_dist_cannot_escape(site: Site) -> None:
    link = site.dist / "escape.txt"
    try:
        link.symlink_to(site.secret)
    except (OSError, NotImplementedError):  # pragma: no cover - 仅系统不支持时
        pytest.skip("本机不支持创建符号链接")

    response = site.client.get("/escape.txt")
    _assert_not_leaked(response.text, SECRET)
    _assert_blocked(_Raw(response.status_code, response.content))


def test_symlinked_dist_still_serves_and_still_blocks(tmp_path: Path) -> None:
    # 生产 dist 是 bind mount，可能是符号链接：包含性判断必须对着解析后的
    # 真实目录做，否则 ``<dist>/../secret.txt`` 会按"字面父目录"算，误判成
    # dist 之外但……正好仍然越界。两种情况都得挡住。
    try:
        site = _build_site(tmp_path, symlink_dist=True)
    except OSError:  # pragma: no cover - 仅系统不支持符号链接时
        pytest.skip("本机不支持创建符号链接")

    index = site.client.get("/")
    assert index.status_code == 200
    assert INDEX_HTML in index.text
    asset = site.client.get("/assets/app.js")
    assert asset.status_code == 200
    assert ASSET_JS in asset.text

    for path in ("/%2e%2e/secret.txt", "/..%2fdata%2froot.key"):
        response = site.client.get(path)
        _assert_not_leaked(response.text, SECRET, ROOT_KEY_MATERIAL)
        _assert_blocked(_Raw(response.status_code, response.content))


# ---------------------------------------------------------------------------
# 6) 字符串前缀比较的假阳性：``<dist>-evil`` 以 ``<dist>`` 开头，但它不在里面
# ---------------------------------------------------------------------------


def test_sibling_directory_with_common_prefix_is_not_inside_dist(site: Site) -> None:
    sibling = site.dist.parent / (site.dist.name + "-evil")
    sibling.mkdir()
    (sibling / "planted.txt").write_text(SECRET, encoding="utf-8")

    response = site.client.get(f"/%2e%2e/{sibling.name}/planted.txt")
    _assert_not_leaked(response.text, SECRET)
    _assert_blocked(_Raw(response.status_code, response.content))


# ---------------------------------------------------------------------------
# 7) 畸形/绝对路径不能变成 500，也不能绕过包含性判断
# ---------------------------------------------------------------------------


def test_malformed_path_is_treated_as_not_found(site: Site) -> None:
    # ``%00`` 解码出 NUL：``Path.resolve()`` 会抛 ValueError，旧实现里
    # ``is_file()`` 吞掉它并回 index.html——修复后不能退化成一个 500。
    response = site.client.get("/%00")
    assert response.status_code == 200
    assert INDEX_HTML in response.text


def test_absolute_path_cannot_escape(site: Site) -> None:
    # 路径拼接遇到绝对路径会**丢掉** root：``root / "/etc/hosts"`` ==
    # ``/etc/hosts``。所以 ``//etc/hosts`` 的 full_path 是 ``/etc/hosts``，
    # 必须在包含性检查里被拒。
    response = _as_is_get(site.app, "//etc/hosts")
    if Path("/etc/hosts").is_file():
        _assert_not_leaked(
            response.text,
            Path("/etc/hosts").read_text(encoding="utf-8", errors="replace"),
        )
    _assert_blocked(response)


# ---------------------------------------------------------------------------
# 8) 正常路径与保留前缀不能因为修复而退化
# ---------------------------------------------------------------------------


def test_normal_paths_still_served(site: Site) -> None:
    index = site.client.get("/")
    assert index.status_code == 200
    assert INDEX_HTML in index.text

    asset = site.client.get("/assets/app.js")
    assert asset.status_code == 200
    assert ASSET_JS in asset.text

    # 真实存在的静态文件仍然从 dist 里取（不是一律回 index）
    (site.dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    favicon = site.client.get("/favicon.svg")
    assert favicon.status_code == 200
    assert favicon.text == "<svg/>"

    # SPA 客户端路由（不存在的静态文件）仍然回 index.html
    spa_route = site.client.get("/settings/provider")
    assert spa_route.status_code == 200
    assert INDEX_HTML in spa_route.text

    nested = site.client.get("/sessions/01HXYZ/detail")
    assert nested.status_code == 200
    assert INDEX_HTML in nested.text


@pytest.mark.parametrize("path", ["/v1/nope", "/api/nope", "/healthz-nope", "/readyz-nope"])
def test_reserved_prefixes_still_404(site: Site, path: str) -> None:
    response = site.client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}
