"""Connect the desktop stage to installed OBS and VTube Studio instances."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from connectors.stage.obs import ObsStage, Program, StageError

# obs-websocket v5 event subscription flags.
OUTPUT_EVENTS = 1 << 6
INPUT_VOLUME_METERS = 1 << 16


class VTubeStudio:
    def __init__(self, client: Any, mouth_parameter: str) -> None:
        self.client = client
        self.builder = client.vts_request
        self.mouth_parameter = mouth_parameter

    async def _request(self, request: dict) -> dict:
        response = await asyncio.wait_for(self.client.request(request), timeout=3)
        if response["messageType"] == "APIError":
            raise StageError("VTube Studio 拒绝请求，请检查模型和口型参数")
        return response["data"]

    async def require_model(self, model_id: str) -> None:
        model = await self._request(self.builder.BaseRequest("CurrentModelRequest", {}))
        if not model["modelLoaded"] or model["modelID"] != model_id:
            raise StageError("请在 VTube Studio 加载本场指定的皮套")

    async def mouth(self, level: float) -> None:
        await self._request(self.builder.requestSetParameterValue(self.mouth_parameter, level))


@asynccontextmanager
async def connect_stage(
    program: Program,
    *,
    obs_port: int,
    obs_password: str,
    vts_port: int,
    vts_model: str | None,
    vts_token_file: Path,
    mouth_parameter: str,
) -> AsyncIterator[ObsStage]:
    try:
        import simpleobsws
    except ImportError as exc:
        raise StageError("请先安装项目的 stage 依赖") from exc

    obs = simpleobsws.WebSocketClient(
        url=f"ws://127.0.0.1:{obs_port}",
        password=obs_password,
        identification_parameters=simpleobsws.IdentificationParameters(
            eventSubscriptions=OUTPUT_EVENTS | (INPUT_VOLUME_METERS if vts_model else 0),
        ),
    )
    async with AsyncExitStack() as stack:
        await asyncio.wait_for(obs.connect(), timeout=5)
        stack.push_async_callback(obs.disconnect)
        if not await obs.wait_until_identified(timeout=5):
            raise StageError("OBS WebSocket 认证失败")

        async def call(method: str, data: dict) -> dict:
            reply = await obs.call(simpleobsws.Request(method, data), timeout=3)
            if not reply.ok():
                raise StageError(f"OBS 请求失败（{method}，错误码 {reply.requestStatus.code}）")
            return reply.responseData or {}

        vts = None
        if vts_model:
            vts = await stack.enter_async_context(_connect_vts(
                port=vts_port, token_file=vts_token_file, mouth_parameter=mouth_parameter,
            ))
            await vts.require_model(vts_model)
        stage = ObsStage(call, program, mouth=vts.mouth if vts else None)
        obs.register_event_callback(stage.on_stream_state, "StreamStateChanged")
        if vts:
            obs.register_event_callback(stage.on_meters, "InputVolumeMeters")
        yield stage


@asynccontextmanager
async def _connect_vts(
    *, port: int, token_file: Path, mouth_parameter: str
) -> AsyncIterator[VTubeStudio]:
    try:
        import pyvts
    except ImportError as exc:
        raise StageError("请先安装项目的 stage 依赖") from exc

    client = pyvts.vts(
        plugin_info={
            "plugin_name": "Ling Stage", "developer": "Ling",
            "authentication_token_path": str(token_file),
        },
        vts_api_info={"name": "VTubeStudioPublicAPI", "version": "1.0", "host": "127.0.0.1", "port": port},
    )
    await asyncio.wait_for(client.connect(), timeout=5)
    if client.websocket is None:
        raise StageError("VTube Studio 插件 API 未连接")
    try:
        builder = client.vts_request
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
        else:
            response = await asyncio.wait_for(client.request(builder.authentication_token()), timeout=60)
            token = response["data"].get("authenticationToken")
            if not token:
                raise StageError("请在 VTube Studio 批准 Ling Stage")
            token_file.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
                stream.write(token)
        authenticated = await asyncio.wait_for(client.request(builder.authentication(token)), timeout=5)
        if not authenticated["data"].get("authenticated"):
            raise StageError("VTube Studio 授权已失效，请移除旧令牌后重新授权")
        yield VTubeStudio(client, mouth_parameter)
    finally:
        await client.close()
