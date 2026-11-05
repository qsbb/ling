"""命令行入口：``python -m ling`` 或 ``ling-api``。"""

from __future__ import annotations

import os

import uvicorn

from ling.config import get_settings
from ling.logging_setup import setup_logging


def main() -> None:
    setup_logging()
    settings = get_settings()
    uvicorn.run(
        "ling.app:app",
        host=settings.host,
        port=settings.port,
        reload=os.environ.get("LING_RELOAD", "0") == "1",
        log_level=os.environ.get("LING_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
