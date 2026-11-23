"""在 E2E 的一次性测试库里建一个**普通用户**（`role="member"`）。

为什么需要这个脚本：验收要求里有一档是「普通用户」，而后端**没有**建用户的 HTTP
接口——账号只在首次安装时由 `ling.app` 的引导逻辑创建（`IdentityService.create_user`）。
所以 E2E 无法通过 API 造出第二个用户。

这个脚本走的是**同一套服务层**（`IdentityService.create_user`），不是直接拼 SQL：
口令哈希、subject 绑定、`must_change_password` 的语义都和被测代码一致。

用法（由 `tests/e2e/live_nav_browser.mjs` 调用，指向它自己新建的那个库）：

    python tests/e2e/create_member.py --database-url postgresql+asyncpg://... --username u --password p

它只写这一行用户，不迁移、不清库、不碰别的库。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from ling.config import Settings
from ling.domains.identity.service import IdentityService
from ling.infra import db


async def create(database_url: str, username: str, password: str, display_name: str | None) -> None:
    # `Settings.database_url` 是 dataclass 字段，直接传它即可（不读环境变量）。
    settings = Settings(database_url=database_url)
    db.init_engine(settings)
    factory = db.get_session_factory()
    async with factory() as session:
        service = IdentityService(session)
        existing = await service.count_users()
        if existing == 0:
            raise SystemExit("这个库里一个用户都没有：先让 API 启动一次完成引导，再建普通用户")
        await service.create_user(
            username=username,
            password=password,
            role="member",
            display_name=display_name or username,
            must_change_password=False,
        )
        await session.commit()
    await db.dispose_engine()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--display-name", default=None)
    args = parser.parse_args()
    asyncio.run(create(args.database_url, args.username, args.password, args.display_name))
    print(f"created member {args.username}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
