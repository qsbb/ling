"""Execute the one supported recipe, checking with Ling before each new step."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Protocol

import httpx

from ling.contracts.desktop import (
    CompanionIssue,
    DesktopAssignment,
    DesktopPoll,
    DesktopReport,
    DesktopReportResult,
    normalize_note,
)

logger = logging.getLogger(__name__)


class DesktopExecutionError(Exception):
    def __init__(self, reason: CompanionIssue) -> None:
        super().__init__(reason)
        self.reason = reason


class NotepadEditor(Protocol):
    def ready(self) -> bool: ...
    async def open(self, directory: Path, deadline: float) -> None: ...
    def write(self, text: str, deadline: float) -> None: ...
    def read(self, deadline: float) -> str: ...


class DesktopCompanion:
    def __init__(self, http: httpx.AsyncClient, editor: NotepadEditor) -> None:
        self.http = http
        self.editor = editor

    async def _post(self, path: str, payload: dict) -> httpx.Response:
        async with asyncio.timeout(3):
            response = await self.http.post(f"/v1/desktop/companion{path}", json=payload)
            response.raise_for_status()
            return response

    async def _report(
        self, task: DesktopAssignment, step: DesktopReport, /,
    ) -> bool:
        response = await self._post(
            f"/tasks/{task.id}/report", step.model_dump(mode="json", exclude_none=True),
        )
        return DesktopReportResult.model_validate(response.json()).accepted

    async def _abort(self, task: DesktopAssignment, reason: CompanionIssue) -> None:
        await self._report(task, DesktopReport(
            lease_token=task.lease_token, step="aborted", reason=reason,
        ))

    async def tick(self) -> None:
        sent_at = monotonic()
        response = await self._post("/poll", {"ready": self.editor.ready()})
        assignment = DesktopPoll.model_validate(response.json()).task
        if assignment is None:
            return
        # Measure from before the request so network delay cannot extend the lease.
        deadline = sent_at + assignment.lease_seconds
        try:
            await self._notepad(assignment, deadline)
        except DesktopExecutionError as exc:
            await self._abort(assignment, exc.reason)
        except OSError:
            await self._abort(assignment, "editor_error")
        except (httpx.HTTPError, TimeoutError):
            # Give up this attempt even if reconnecting is immediate.
            with suppress(httpx.HTTPError, TimeoutError):
                await self._abort(assignment, "connection_lost")
            raise

    async def _notepad(self, task: DesktopAssignment, deadline: float) -> None:
        with TemporaryDirectory(prefix="ling-notepad-", ignore_cleanup_errors=True) as folder:
            await self.editor.open(Path(folder), deadline)
            if not await self._report(task, DesktopReport(lease_token=task.lease_token, step="opened")):
                return
            self.editor.write(task.text, deadline)
            if not await self._report(task, DesktopReport(lease_token=task.lease_token, step="written")):
                return
            readback = normalize_note(self.editor.read(deadline))
            if readback != task.text:
                # A changed editor's content is not part of the receipt.
                raise DesktopExecutionError("readback_mismatch")
            await self._report(task, DesktopReport(
                lease_token=task.lease_token, step="verified", readback=readback,
            ))

    async def run(self) -> None:
        connected = False
        while True:
            try:
                await self.tick()
            except (httpx.HTTPError, TimeoutError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise
                if connected:
                    logger.warning("Ling 暂不可达，已放弃当前尝试")
                connected = False
            else:
                if not connected:
                    logger.info("已连接 Ling，等待记事本任务")
                connected = True
            await asyncio.sleep(2)
