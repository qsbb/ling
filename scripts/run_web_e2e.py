"""真实联调：真后端（独立进程）+ 真插件服务 + 真 Chromium。

## 为什么后端必须跑在**独立进程**里

第一版把 uvicorn 用 `asyncio.create_task(server.serve())` 塞进同一个进程，
结果 Chromium 的 `page.goto` 一律超时：服务端日志显示 `GET / → 200`，浏览器却
一个字节都收不到，原始 socket 与 httpx 都正常。用最小 FastAPI 应用对照、
再用 `uvicorn.Server(...).serve()` 独立进程跑同一个 Ling 应用，Chromium 85ms
就完整加载（含静态资源）。所以这是联调脚手架的问题，不是产品问题；
这里改成独立进程，与生产部署（`uvicorn ling.app:app`）也一致。

跑法::

    LING_DATABASE_URL=postgresql+asyncpg://ling@127.0.0.1:55432/ling_test \\
      .venv/bin/python scripts/run_web_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SCHEMA = f"epweb_{uuid.uuid4().hex[:10]}"
HOST_PORT = 6532
PLUGIN_PORT = 8198
BASE = f"http://127.0.0.1:{HOST_PORT}"
PLUGIN = f"http://127.0.0.1:{PLUGIN_PORT}"
SHOTS = Path("/tmp/ep-shots")
BOOTSTRAP_PASSWORD = "probe-password"


async def _prepare_schema(url: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
    await engine.dispose()


def _write_backend_script() -> Path:
    """把后端启动脚本写成文件，交给独立进程执行（不 import 本脚本的任何状态）。"""
    script = Path(f"/tmp/ep_backend_{SCHEMA}.py")
    script.write_text(
        f'''"""联调用后端：真实 create_app + 独立 uvicorn 进程（schema={SCHEMA}）。"""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, {str(ROOT / "src")!r})
from ling.config import Settings
from ling.app import create_app
import uvicorn

settings = Settings(
    env="dev",
    database_url=os.environ["LING_DATABASE_URL"],
    db_search_path={SCHEMA!r},
    data_dir=Path("/tmp/ep-e2e-{SCHEMA}"),
    bootstrap_admin_user="probe",
    bootstrap_admin_password={BOOTSTRAP_PASSWORD!r},
    root_key="probe-root-key-{SCHEMA}",
    test_mode=True,
    cookie_secure=False,
    host="127.0.0.1",
    port={HOST_PORT},
    access_mode="off",
    access_list=frozenset(),
    onebot_ws_port=None,
    onebot_ws_url=None,
    qqbot_enabled=False,
)
app = create_app(settings)

if __name__ == "__main__":
    # 与生产一致：uvicorn 自己持有事件循环。
    uvicorn.run(app, host="127.0.0.1", port={HOST_PORT}, log_level="warning")
''',
        encoding="utf-8",
    )
    return script


async def _wait_http(url: str, *, attempts: int = 300) -> bool:
    for _ in range(attempts):
        try:
            async with httpx.AsyncClient() as probe:
                if (await probe.get(url, timeout=0.5)).status_code < 500:
                    return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.1)
    return False


async def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    await _prepare_schema(os.environ["LING_DATABASE_URL"])
    backend_script = _write_backend_script()

    env = dict(os.environ)
    backend = subprocess.Popen(
        [sys.executable, str(backend_script)], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    plugin = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts/test_plugin_fixture.py"),
            "--host",
            "127.0.0.1",
            "--port",
            str(PLUGIN_PORT),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        host_ready = await _wait_http(f"{BASE}/v1/version")
        plugin_ready = await _wait_http(f"{PLUGIN}/v1/manifest")
        print(f"[e2e] host_ready={host_ready} plugin_ready={plugin_ready}")
        if not (host_ready and plugin_ready):
            return 3

        driver_env = dict(os.environ)
        driver_env.update({"EP_BASE": BASE, "EP_PLUGIN": PLUGIN, "EP_SHOTS": str(SHOTS)})
        run = subprocess.run(
            ["node", str(ROOT / "clients" / "web-console" / "scripts" / "web_e2e_driver.mjs")],
            env=driver_env,
            cwd=str(ROOT / "clients" / "web-console"),
            capture_output=True,
            text=True,
        )
        sys.stdout.write(run.stdout)
        sys.stderr.write(run.stderr)
        print(json.dumps({"screenshots": str(SHOTS), "schema": SCHEMA}, ensure_ascii=False))
        return run.returncode
    finally:
        for proc in (plugin, backend):
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
