"""Start the companion inside the signed-in Windows user's desktop."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys

import httpx

from connectors.desktop.companion import DesktopCompanion


async def _run(url: httpx.URL, token: str) -> None:
    from connectors.desktop.windows import WindowsNotepad

    async with httpx.AsyncClient(
        base_url=url, headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False, trust_env=False,
    ) as http:
        await DesktopCompanion(http, WindowsNotepad()).run()


def main() -> int:
    parser = argparse.ArgumentParser(description="Ling 的 Windows 记事本伴生端")
    parser.add_argument("--url", required=True, help="Ling API 地址，如 https://ling.example")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("请在已登录的 Windows 桌面中启动伴生端")
    try:
        url = httpx.URL(args.url)
    except httpx.InvalidURL:
        parser.error("Ling 地址无效")
    if (
        url.scheme not in {"http", "https"} or not url.host or url.userinfo
        or url.path not in {"", "/"} or url.query or url.fragment
    ):
        parser.error("请输入 Ling 实例的 HTTP 或 HTTPS 根地址")
    token = os.environ.get("LING_DESKTOP_TOKEN") or getpass.getpass("配对令牌：")
    if len(token) != 43 or not token.isascii() or any(char.isspace() for char in token):
        parser.error("请提供 Ling 生成的 Windows 配对令牌")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger(__name__).info("正在连接 Ling，按 Ctrl+C 可退出")
    try:
        asyncio.run(_run(url, token))
    except KeyboardInterrupt:
        return 0
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            print("配对已失效。请核对 Ling 地址和令牌。", file=sys.stderr)
        else:
            print(f"Ling 拒绝伴生端请求（HTTP {exc.response.status_code}）。", file=sys.stderr)
    except (ValueError, OSError):
        print("伴生端无法启动或收到不兼容的响应。", file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
