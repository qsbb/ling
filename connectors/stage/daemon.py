"""Run the OBS stage on the broadcasting computer."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from connectors.stage.obs import ObsStage, Playback, StageError

logger = logging.getLogger(__name__)


class StageAgent:
    def __init__(
        self, http: httpx.AsyncClient, *, instance: str, state_dir: Path, stream_server: str | None,
    ) -> None:
        self.http = http
        self.path = f"/v1/custom/{instance}/stage"
        self.journal = state_dir / f"{instance}.json"
        if self.journal.is_file():
            self.state = json.loads(self.journal.read_text(encoding="utf-8"))
        else:
            self.state = {"device_id": os.urandom(16).hex()}
            self._save()
        self.session_id = self.state.get("session_id")
        self.stream_server = stream_server
        self.playback: asyncio.Task[dict] | None = None
        self.report: dict | None = None
        self.control: str | None = None
        self.deadline: str | None = None
        self.ready = False
        self.broadcast_ready = False
        self.companion_attached = False
        self.attachment_id: str | None = None
        self._attachment: tuple[ObsStage, int] | None = None
        self._output: tuple[ObsStage, int] | None = None
        self._stopping_output: tuple[ObsStage, int] | None = None

    def _save(self) -> None:
        temp = self.journal.with_suffix(".tmp")
        with os.fdopen(os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as stream:
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.journal)

    async def run(self, stage: ObsStage) -> None:
        try:
            while True:
                try:
                    await self.tick(stage)
                except httpx.HTTPError:
                    await self._cancel_speech()
                    logger.warning("Ling 暂不可达，直播电脑仍按本场时限监控 OBS")
                    await asyncio.sleep(2)
                await asyncio.sleep(0.4 if self.session_id else 2)
        finally:
            await self._cancel_speech()

    async def tick(self, stage: ObsStage) -> None:
        status = await stage.stream_status()
        if self._attachment and not self._still_attached(stage, status):
            await self._cancel_speech()
            self.ready = False
            self.control = "uncertain"
        if self._stopping_output and not status["outputActive"]:
            self.control = "stopped"
            self._forget_stream()
        elif self._stopping_output and self._stopping_output != (stage, stage.output_serial):
            self.control = "uncertain"
            self._forget_stream()
        elif self.state.get("deadline") and not self._stopping_output:
            if not self._owns(stage, status):
                self.control = "uncertain" if status["outputActive"] else "stopped"
                self._forget_stream()
            elif self._seconds_left(self.state["deadline"]) <= 0:
                self.control = await self._stop(stage)
                status = await stage.stream_status()
        if self.playback is not None and self.playback.done():
            self.report = self.playback.result()
            self.playback = None
        response = await self.http.post(self.path, json={
            "session_id": self.session_id, "device_id": self.state["device_id"],
            "obs_active": status["outputActive"],
            "obs_reconnecting": status.get("outputReconnecting", False), "ready": self.ready,
            "broadcast_ready": self.broadcast_ready,
            "scene": stage.program.scene, "attachment_id": self.attachment_id,
            "control": self.control or ("started" if self._owns(stage, status) else None),
            "playback": self.report,
        })
        response.raise_for_status()
        self.report = None
        self.control = None
        order = response.json()
        session_id = order["session_id"]
        if session_id != self.session_id:
            await self._cancel_speech()
            self.report = None  # A receipt cannot move to another session.
            self.session_id = session_id
            self.ready = False
            self.broadcast_ready = False
            self.attachment_id = None
            self._attachment = None
        self.deadline = order.get("deadline_at")
        action = order["action"]
        self.companion_attached = order.get("role") == "companion" and order.get("mode") == "broadcast"
        if action in {"stop", "hold", "prepare", "detach"}:
            await self._cancel_speech()
        if action == "stop" and not self.companion_attached and not self._stopping_output:
            self.control = await self._stop(stage)
        elif action == "start" and not self.companion_attached:
            self.control = await self._start(stage)
        elif action == "detach":
            self.control = "detached"
            self.attachment_id = None
            self._attachment = None
            self.ready = False
        elif action == "prepare":
            self.broadcast_ready = False
            if self.stream_server and await stage.stream_server() == self.stream_server:
                try:
                    await stage.prepare()
                except StageError as exc:
                    logger.warning("%s", exc)
                else:
                    self.broadcast_ready = True
        elif action == "speak" and self.playback is None:
            self.playback = asyncio.create_task(self._speak(stage, order))
        if self.session_id and not self.ready and action not in {"stop", "detach"} and (
            (self.companion_attached and self._attachment is None and status["outputActive"]
             and not status.get("outputReconnecting") and not stage.output_stopping)
            or (not self.companion_attached and not status["outputActive"] and not status.get("outputReconnecting"))
        ):
            try:
                output_serial = stage.output_serial
                await stage.prepare(switch_scene=not self.companion_attached)
                if self.companion_attached:
                    if stage.output_serial != output_serial or stage.output_stopping:
                        return
                    self._attachment = (stage, output_serial)
                    self.attachment_id = os.urandom(16).hex()
                self.ready = True
            except StageError as exc:
                logger.warning("%s", exc)
        elif self._owns(stage, status):
            self.ready = True

    async def _start(self, stage: ObsStage) -> str:
        if not self.stream_server or not self.deadline or self._seconds_left(self.deadline) <= 0:
            return "blocked"
        if await stage.stream_server() != self.stream_server:
            return "blocked"
        before = stage.output_serial
        try:
            await stage.start_stream()
            async with asyncio.timeout(10):
                while True:
                    remote = await stage.stream_status()
                    if remote["outputActive"] and not remote.get("outputReconnecting", False):
                        if stage.output_serial > before + 1:
                            return "uncertain"
                        if stage.output_generation == before + 1:
                            self._output = (stage, before + 1)
                            self.state.update(session_id=self.session_id, deadline=self.deadline)
                            self._save()
                            return "started"
                    await asyncio.sleep(0.25)
        except Exception as exc:
            logger.warning("OBS 开播回执待核对，类型=%s", type(exc).__name__)
            return "uncertain"

    def _owns(self, stage: ObsStage, status: dict) -> bool:
        return bool(
            stage.output_generation is not None and status["outputActive"]
            and self._output == (stage, stage.output_generation)
            and self.state.get("session_id") == self.session_id
        )

    def _still_attached(self, stage: ObsStage, status: dict) -> bool:
        return bool(
            self._attachment == (stage, stage.output_serial)
            and status["outputActive"] and not stage.output_stopping
        )

    def _forget_stream(self) -> None:
        self._output = None
        self._stopping_output = None
        for key in ("session_id", "started_at", "deadline"):
            self.state.pop(key, None)
        self._save()

    async def _stop(self, stage: ObsStage) -> str | None:
        target_matches = self.stream_server and await stage.stream_server() == self.stream_server
        status = await stage.stream_status()
        if not status["outputActive"]:
            self._forget_stream()
            return "stopped"
        if self._owns(stage, status) and target_matches:
            self._stopping_output = (stage, stage.output_serial)
            self._output = None
            await stage.stop_stream()
            if not (await stage.stream_status())["outputActive"]:
                self._forget_stream()
                return "stopped"
            return None
        return "uncertain"

    async def _cancel_speech(self) -> None:
        if self.playback is not None and not self.playback.done():
            if not self.playback.cancelling():
                self.playback.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.playback

    @staticmethod
    def _seconds_left(expiry: str) -> float:
        return (datetime.fromisoformat(expiry) - datetime.now(UTC)).total_seconds()

    async def _speak(self, stage: ObsStage, order: dict) -> dict:
        identity = order["speech_id"]
        session_id = order["session_id"]
        result = Playback("failed", 0)
        try:
            if not re.fullmatch(r"speech_[a-f0-9]{24}", identity) or order["format"] not in {"wav", "mp3"}:
                raise StageError("舞台音频引用格式有误")
            remaining = self._seconds_left(order["expires_at"])
            if remaining <= 0:
                return {"speech_id": identity, "state": "interrupted"}
            async with asyncio.timeout(remaining):
                response = await self.http.get(f"{self.path}/audio/{session_id}/{identity}")
                response.raise_for_status()
                if len(response.content) > 10 * 1024 * 1024:
                    raise StageError("舞台音频超出文件上限")
                status = await stage.stream_status()
                if status.get("outputReconnecting") or (
                    order["mode"] == "preview" and status["outputActive"]
                ) or (
                    order["mode"] == "broadcast"
                    and not (self._still_attached(stage, status) if self.companion_attached else self._owns(stage, status))
                ):
                    return {"speech_id": identity, "state": "interrupted"}
                with TemporaryDirectory(prefix="ling-stage-") as folder:
                    audio = Path(folder) / f"{identity}.{order['format']}"
                    audio.write_bytes(response.content)
                    result = await stage.play(audio, order["text"], timeout=remaining)
        except asyncio.CancelledError:
            result = Playback("interrupted", 0)
        except Exception as exc:
            logger.warning("播放中断，类型=%s", type(exc).__name__)
        return {"speech_id": identity, "state": result.state}
