"""统一日志配置。**两个入口都要调**，否则生产环境静默丢日志。

## 为什么单独一个模块

API 有两种启动方式：

- 开发：``python -m ling`` → 走 ``ling/__main__.py``
- 容器：``python -m uvicorn ling.app:app`` → 走 ``deploy/entrypoint.sh``

`uvicorn.run(log_level=...)` 只调 **uvicorn 自己**的 logger，不碰 root logger。
所以如果只在 `__main__.py` 里配日志，容器那条路就**一个 ling 日志都没有**：
`logging.getLogger("ling.app").info(...)` 找不到 handler，落到 lastResort
只输出 WARNING 及以上。

实测踩过：容器部署后 qqbot 网关到底启没启动、为什么没启动，
日志里一个字都看不到——两个入口差异造成的，代码本身没问题。
"""

from __future__ import annotations

import logging
import os

# 太吵的第三方库：httpx 每个请求一行，websockets 连上后也会刷
NOISY_LOGGERS = ("httpx", "httpcore", "websockets.client", "websockets.server")


def setup_logging() -> None:
    """装 root handler，尊重 LING_LOG_LEVEL。**幂等**：已有 handler 就不重复加。"""
    level_name = os.environ.get("LING_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    root.setLevel(level)
    for noisy in NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


__all__ = ["NOISY_LOGGERS", "setup_logging"]
