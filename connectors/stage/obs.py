"""Use OBS's native media and text sources to play one speech segment at a time."""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ObsCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
Mouth = Callable[[float], Awaitable[None]]


class StageError(RuntimeError):
    pass


@dataclass(frozen=True)
class Program:
    scene: str
    audio: str
    captions: str
    avatar: str | None = None


@dataclass(frozen=True)
class Playback:
    state: str
    played_ms: int


class ObsStage:
    def __init__(self, call: ObsCall, program: Program, *, mouth: Mouth | None = None) -> None:
        self._call = call
        self.program = program
        self._mouth = mouth
        self._level = 0.0
        self._playing = False
        self._lock = asyncio.Lock()

    async def on_meters(self, data: dict[str, Any]) -> None:
        for source in data.get("inputs", ()):
            if source.get("inputName") == self.program.audio:
                self._level = max(
                    (float(channel[1]) for channel in source["inputLevelsMul"]), default=0.0
                )
                return
        self._level = 0.0

    async def stream_status(self) -> dict[str, Any]:
        return await self._call("GetStreamStatus", {})

    async def prepare(self, *, switch_scene: bool = False) -> None:
        items = await self._call("GetSceneItemList", {"sceneName": self.program.scene})
        required = {self.program.audio, self.program.captions}
        if self.program.avatar:
            required.add(self.program.avatar)
        enabled = {item["sourceName"] for item in items["sceneItems"] if item["sceneItemEnabled"]}
        if not required <= enabled or self.program.audio == self.program.captions:
            raise StageError("节目场景缺少启用的语音、字幕或皮套源")
        audio = await self._input(self.program.audio)
        captions = await self._input(self.program.captions)
        if audio["inputKind"] != "ffmpeg_source" or not captions["inputKind"].startswith(
            ("text_gdiplus", "text_ft2_source")
        ):
            raise StageError("请为直播配置独立的 OBS 媒体源和文本源")
        if (await self._call("GetInputMute", {"inputName": self.program.audio}))["inputMuted"]:
            raise StageError("直播语音源已静音")
        if switch_scene:
            await self._call("SetCurrentProgramScene", {"sceneName": self.program.scene})
        await self._check_scene()

    async def start_stream(self) -> None:
        if (await self.stream_status())["outputActive"]:
            raise StageError("OBS 已有推流")
        await self.prepare(switch_scene=True)
        await self._call("StartStream", {})

    async def stop_stream(self) -> None:
        if (await self.stream_status())["outputActive"]:
            await self._call("StopStream", {})

    async def preview(self, audio: Path, caption: str, *, timeout: float = 30) -> Playback:
        if (await self.stream_status())["outputActive"]:
            raise StageError("请在停播后运行本机预览")
        await self.prepare(switch_scene=True)
        return await self.play(audio, caption, timeout=timeout)

    async def play(self, audio: Path, caption: str, *, timeout: float = 30) -> Playback:
        audio = audio.resolve(strict=True)
        if not audio.is_file() or audio.suffix.lower() not in {".wav", ".mp3"}:
            raise StageError("语音必须是本机 WAV 或 MP3 文件")
        async with self._lock:
            await self._check_scene()
            await self._caption("")
            if (await self._media())["mediaState"] in {
                "OBS_MEDIA_STATE_PLAYING", "OBS_MEDIA_STATE_OPENING", "OBS_MEDIA_STATE_BUFFERING"
            }:
                raise StageError("OBS 语音源正在播放")
            if (await self._input(self.program.audio))["inputSettings"].get("local_file") == str(audio):
                # OBS does not restart an unchanged file on a settings update.
                raise StageError("请为每个播出段使用独立音频文件")
            self._level = 0.0
            played_ms = 0
            try:
                async with asyncio.timeout(timeout):
                    await self._set(self.program.audio, {
                        "is_local_file": True,
                        "local_file": str(audio),
                        "looping": False,
                        "restart_on_activate": False,
                    })
                    while True:
                        status = await self._media()
                        state = status["mediaState"]
                        if state == "OBS_MEDIA_STATE_PLAYING":
                            await self._check_scene()
                            if not self._playing:
                                await self._caption(caption)
                                self._playing = True
                            played_ms = max(played_ms, status.get("mediaCursor") or 0)
                            if self._mouth:
                                await self._mouth(min(1.0, math.sqrt(max(self._level, 0.0))))
                        elif state == "OBS_MEDIA_STATE_ENDED":
                            if not self._playing:
                                raise StageError("未观察到语音开始播放")
                            return Playback("completed", status.get("mediaDuration") or played_ms)
                        elif state == "OBS_MEDIA_STATE_ERROR":
                            raise StageError("OBS 无法播放这段语音")
                        elif self._playing:
                            return Playback("interrupted", played_ms)
                        await asyncio.sleep(0.05)
            except TimeoutError as exc:
                raise StageError("等待 OBS 播放超时") from exc
            finally:
                self._playing = False
                self._level = 0.0
                await self._cleanup(str(audio))

    async def _check_scene(self) -> None:
        current = await self._call("GetCurrentProgramScene", {})
        if current["currentProgramSceneName"] != self.program.scene:
            raise StageError("OBS 已切离直播节目场景")

    async def _input(self, name: str) -> dict[str, Any]:
        return await self._call("GetInputSettings", {"inputName": name})

    async def _media(self) -> dict[str, Any]:
        return await self._call("GetMediaInputStatus", {"inputName": self.program.audio})

    async def _set(self, name: str, settings: dict[str, Any]) -> None:
        await self._call("SetInputSettings", {
            "inputName": name, "inputSettings": settings, "overlay": True,
        })

    async def _caption(self, text: str) -> None:
        await self._set(self.program.captions, {
            "text": text, "from_file": False, "read_from_file": False,
        })

    async def _cleanup(self, file: str) -> None:
        with contextlib.suppress(Exception):
            settings = (await self._input(self.program.audio))["inputSettings"]
            if settings.get("local_file") == file:
                await self._call("TriggerMediaInputAction", {
                    "inputName": self.program.audio, "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP",
                })
                await self._set(self.program.audio, {"local_file": ""})
        with contextlib.suppress(Exception):
            await self._caption("")
        if self._mouth:
            with contextlib.suppress(Exception):
                await self._mouth(0.0)
