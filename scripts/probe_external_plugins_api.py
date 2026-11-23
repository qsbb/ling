"""实机探针：对真实 Ling 服务 + 仓内示例插件演练外部插件管理 API。

目的不是测试，而是**观测真实响应形状**，为 Web 适配层提供事实来源：

1. 在独立 schema 上启动真实 ``create_app``（uvicorn 监听回环端口）；
2. 启动 ``examples/external-plugin/server.py`` 子进程；
3. 用 httpx 走真实 HTTP：登录 → 登记 → 列表 → 状态 → 停用 → 启用 →
   同步 → 标识变更 → 取消登记，逐条打印状态码与响应体（已脱敏）。

用法::

    LING_DATABASE_URL=... .venv/bin/python scripts/probe_external_plugins_api.py

输出写到 ``/tmp/ep-probe.json``，供设计取舍引用。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

SCHEMA = f"probe_{uuid.uuid4().hex[:10]}"
HOST_PORT = 6531
PLUGIN_PORT = 8196
BASE = f"http://127.0.0.1:{HOST_PORT}"
PLUGIN = f"http://127.0.0.1:{PLUGIN_PORT}"


async def _prepare_schema(url: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{SCHEMA}"'))
    await engine.dispose()


def _settings():
    from ling.config import Settings

    root = Path(os.environ["LING_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    return Settings(
        env="dev",
        database_url=os.environ["LING_DATABASE_URL"],
        db_search_path=SCHEMA,
        data_dir=Path(f"/tmp/ep-probe-{SCHEMA}"),
        bootstrap_admin_user="probe",
        bootstrap_admin_password="probe-password",
        root_key="probe-root-key-" + SCHEMA,
        test_mode=True,
        cookie_secure=False,
        host="127.0.0.1",
        port=HOST_PORT,
        access_mode="off",
        access_list=frozenset(),
        onebot_ws_port=None,
        onebot_ws_url=None,
        qqbot_enabled=False,
    ) if root else None


def _record(steps: list[dict[str, Any]], name: str, response: httpx.Response) -> Any:
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:400]
    steps.append(
        {
            "step": name,
            "status": response.status_code,
            "body": body,
        }
    )
    print(f"\n### {name} -> {response.status_code}")
    print(json.dumps(body, ensure_ascii=False, indent=2)[:2000])
    return body


async def main() -> int:
    url = os.environ["LING_DATABASE_URL"]
    await _prepare_schema(url)

    import uvicorn

    from ling.app import create_app
    from ling.infra.db import dispose_engine, init_engine

    settings = _settings()
    init_engine(settings)
    app = create_app(settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=HOST_PORT, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    plugin = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts/test_plugin_fixture.py"),
         "--host", "127.0.0.1", "--port", str(PLUGIN_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    steps: list[dict[str, Any]] = []
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.1)
        plugin_ready = False
        for _ in range(100):
            try:
                async with httpx.AsyncClient() as probe:
                    ready = await probe.get(f"{PLUGIN}/v1/manifest", timeout=0.5)
                if ready.status_code == 200:
                    plugin_ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.1)
        print(f"[probe] plugin_ready={plugin_ready}")

        async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as client:
            login = await client.post("/v1/auth/login", json={
                "username": "probe", "password": "probe-password",
            })
            _record(steps, "login", login)

            empty = await client.get("/v1/plugins")
            _record(steps, "list empty", empty)

            registered = await client.post("/v1/plugins/register", json={
                "endpoint": PLUGIN,
                "expected_id": "example.notes",
                "granted_capabilities": ["tools", "commands"],
                "enabled": False,
            })
            _record(steps, "register partial grant", registered)

            listed = await client.get("/v1/plugins")
            _record(steps, "list after register", listed)

            status = await client.get("/v1/plugins/example.notes/status")
            _record(steps, "status", status)

            # 授权不完整时调用工具：commands 未授权
            disabled = await client.post("/v1/plugins/example.notes/disable")
            _record(steps, "disable", disabled)

            enabled = await client.post("/v1/plugins/example.notes/enable", json={"enabled": True})
            _record(steps, "enable", enabled)

            sync = await client.post("/v1/plugins/example.notes/sync")
            _record(steps, "sync", sync)

            tool_status = await client.get("/v1/settings/tool-status")
            _record(steps, "tool-status", tool_status)

            dup = await client.post("/v1/plugins/register", json={
                "endpoint": PLUGIN, "granted_capabilities": ["tools"],
            })
            _record(steps, "register duplicate (no force)", dup)

            bad_endpoint = await client.post("/v1/plugins/register", json={
                "endpoint": "http://127.0.0.1:1/dead", "granted_capabilities": ["tools"],
            })
            _record(steps, "register unreachable", bad_endpoint)

            missing = await client.post("/v1/plugins/nope.none/enable", json={"enabled": True})
            _record(steps, "enable missing", missing)

            # 失败分支 1：运行中改 id，再同步 -> 应停用并置 identity_mismatch
            async with httpx.AsyncClient() as ctl:
                await ctl.post(f"{PLUGIN}/__control", json={"id": "example.renamed"})
            mismatch = await client.post("/v1/plugins/example.notes/sync")
            _record(steps, "sync with changed id", mismatch)
            after_mismatch = await client.get("/v1/plugins")
            _record(steps, "list after identity mismatch", after_mismatch)

            # 失败分支 2：manifest 返回非 JSON
            async with httpx.AsyncClient() as ctl:
                await ctl.post(f"{PLUGIN}/__control", json={"id": "example.notes", "mode": "bad_json"})
            bad_manifest = await client.post("/v1/plugins/example.notes/sync")
            _record(steps, "sync with bad json", bad_manifest)

            # 失败分支 3：端点不可达时启用
            async with httpx.AsyncClient() as ctl:
                await ctl.post(f"{PLUGIN}/__control", json={"mode": "normal"})
            gone = await client.post("/v1/plugins/example.notes/enable", json={"enabled": True})
            _record(steps, "enable after back-to-normal", gone)

            status2 = await client.get("/v1/plugins/example.notes/status")
            _record(steps, "status after failures", status2)

            removed = await client.delete("/v1/plugins/example.notes")
            _record(steps, "unregister", removed)

            after = await client.get("/v1/plugins")
            _record(steps, "list after unregister", after)
    finally:
        plugin.terminate()
        try:
            plugin.wait(timeout=5)
        except subprocess.TimeoutExpired:
            plugin.kill()
        server.should_exit = True
        await serve_task
        await dispose_engine()

    Path("/tmp/ep-probe.json").write_text(
        json.dumps({"schema": SCHEMA, "steps": steps}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\nwrote /tmp/ep-probe.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
