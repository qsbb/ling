from datetime import UTC, datetime, timedelta

import httpx
from connectors.stage.daemon import StageAgent


class Stage:
    def __init__(self):
        self.output = False
        self.target = "rtmp://127.0.0.1:19365/live"
        self.output_serial = 0
        self.output_generation = None
        self.starts = 0
        self.stops = 0
        self.delay_stop = False

    async def stream_status(self):
        return {
            "outputActive": self.output,
            "outputDuration": 1000 if self.output else 0,
        }

    async def start_stream(self):
        self.starts += 1
        self.output_serial += 1
        self.output_generation = self.output_serial
        self.output = True

    async def stop_stream(self):
        self.stops += 1
        self.output_generation = None
        if not self.delay_stop:
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
        assert await restarted._stop(stage) == "uncertain"
        assert stage.stops == 0 and stage.output
        assert await agent._stop(stage) == "stopped"
        assert stage.stops == 1

        assert await agent._start(stage) == "started"
        stage.output_generation = None
        await stage.start_stream()
        assert await agent._stop(stage) == "uncertain"
        assert stage.stops == 1 and stage.output


async def test_daemon_does_not_reissue_stop_after_fast_manual_restart(tmp_path):
    async def server(_):
        return httpx.Response(200, json={"session_id": "test-session", "action": "stop"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(server), base_url="http://test") as http:
        stage = Stage()
        agent = StageAgent(http, instance="live-test", state_dir=tmp_path, stream_server=stage.target)
        agent.session_id = "test-session"
        agent.deadline = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        assert await agent._start(stage) == "started"
        stage.delay_stop = True
        assert await agent._stop(stage) is None
        assert stage.stops == 1
        await stage.start_stream()
        await agent.tick(stage)
        assert stage.stops == 1 and stage.output
