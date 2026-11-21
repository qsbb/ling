from datetime import UTC, datetime, timedelta
from time import time

import httpx
from connectors.stage.daemon import StageAgent


class Stage:
    def __init__(self):
        self.output = False
        self.target = "rtmp://127.0.0.1:19365/live"
        self.started_at = None
        self.starts = 0
        self.stops = 0

    async def stream_status(self):
        return {
            "outputActive": self.output,
            "outputDuration": round((time() - self.started_at) * 1000) if self.output else 0,
        }

    async def start_stream(self):
        self.starts += 1
        self.started_at = time()
        self.output = True

    async def stop_stream(self):
        self.stops += 1
        self.output = False

    async def stream_server(self):
        return self.target


async def test_daemon_only_controls_the_expected_owned_output(tmp_path):
    async with httpx.AsyncClient() as http:
        stage = Stage()
        agent = StageAgent(http, instance="live-test", state_dir=tmp_path, stream_server="other")
        agent.session_id = "test-session"
        agent.deadline = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        assert await agent._start(stage) == "blocked"
        assert stage.starts == 0

        agent.stream_server = stage.target
        assert await agent._start(stage) == "started"
        assert stage.starts == 1
        restarted = StageAgent(http, instance="live-test", state_dir=tmp_path, stream_server=stage.target)
        assert restarted.session_id == agent.session_id
        assert restarted._owns(await stage.stream_status())
        assert await restarted._stop(stage, await stage.stream_status()) == "stopped"
        assert stage.stops == 1

        assert await agent._start(stage) == "started"
        stage.started_at -= 30
        assert await agent._stop(stage, await stage.stream_status()) == "uncertain"
        assert stage.stops == 1 and stage.output
