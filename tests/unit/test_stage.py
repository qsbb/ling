import asyncio
from pathlib import Path

import pytest
from connectors.stage.obs import ObsStage, Program, StageError


class Obs:
    def __init__(self):
        self.path = ""
        self.caption_changes = []
        self.status = "STOPPED"
        self.playing = asyncio.Event()
        self.started = False
        self.streaming = False
        self.scene = "Studio"
        self.settings_updates = 0
        self.initial_state = "PLAYING"

    async def call(self, method, data):
        name = data.get("inputName")
        if method == "GetStreamStatus":
            return {"outputActive": self.streaming}
        if method == "GetSceneItemList":
            return {"sceneItems": [
                {"sourceName": n, "sceneItemEnabled": True} for n in ("Voice", "Caption")
            ]}
        if method == "GetInputSettings":
            if name == "Voice":
                return {"inputKind": "ffmpeg_source", "inputSettings": {"local_file": self.path}}
            return {"inputKind": "text_ft2_source_v2", "inputSettings": {}}
        if method == "GetInputMute":
            return {"inputMuted": False}
        if method == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.scene}
        if method == "SetCurrentProgramScene":
            self.scene = data["sceneName"]
        elif method == "SetInputSettings":
            if name == "Voice":
                self.path = data["inputSettings"]["local_file"]
                if self.path:
                    self.settings_updates += 1
                    self.started = True
                    self.status = self.initial_state
            else:
                self.caption_changes.append((self.status, data["inputSettings"]["text"]))
        elif method == "GetMediaInputStatus":
            if self.started and self.status == "PLAYING":
                self.playing.set()
            return {"mediaState": f"OBS_MEDIA_STATE_{self.status}", "mediaCursor": 800}
        elif method == "TriggerMediaInputAction":
            assert data["mediaAction"] == "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_STOP"
            self.status = "STOPPED"
        else:
            raise AssertionError(method)
        return {}


async def test_preview_caption_and_mouth_follow_playback(tmp_path: Path):
    clip = tmp_path / "clip.wav"
    clip.touch()
    obs = Obs()
    mouth = []

    async def set_mouth(value):
        mouth.append(value)

    stage = ObsStage(obs.call, Program("Studio", "Voice", "Caption"), mouth=set_mouth)
    running = asyncio.create_task(stage.preview(clip, "Public speech"))
    await asyncio.wait_for(obs.playing.wait(), timeout=1)
    assert obs.caption_changes[-1] == ("PLAYING", "Public speech")
    await stage.on_meters({"inputs": [{"inputName": "Music", "inputLevelsMul": [[1, 1, 1]]}]})
    await asyncio.sleep(0.07)
    assert mouth and max(mouth) == 0
    await stage.on_meters({"inputs": [{"inputName": "Voice", "inputLevelsMul": [[0.5, 0.5, 0.5]]}]})
    await asyncio.sleep(0.07)
    assert mouth[-1] > 0
    obs.status = "ENDED"
    assert (await running).state == "completed"
    assert mouth[-1] == 0
    assert obs.caption_changes[-1][1] == ""
    assert obs.settings_updates == 1 and obs.path == ""


async def test_preview_rejects_an_existing_stream(tmp_path: Path):
    obs = Obs()
    obs.streaming = True
    stage = ObsStage(obs.call, Program("Studio", "Voice", "Caption"))
    with pytest.raises(StageError, match="停播"):
        await stage.preview(tmp_path / "clip.wav", "Private rehearsal")
    assert obs.caption_changes == []
    assert obs.settings_updates == 0


async def test_cancel_preview_clears_caption_and_audio(tmp_path: Path):
    clip = tmp_path / "clip.wav"
    clip.touch()
    obs = Obs()
    stage = ObsStage(obs.call, Program("Studio", "Voice", "Caption"))
    running = asyncio.create_task(stage.preview(clip, "Caption"))
    await asyncio.wait_for(obs.playing.wait(), timeout=1)
    assert obs.caption_changes[-1] == ("PLAYING", "Caption")
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert obs.caption_changes[-1][1] == ""
    assert obs.status == "STOPPED" and obs.path == ""


async def test_obs_output_change_interrupts_playback_without_waiting_for_stage_poll(tmp_path: Path):
    clip = tmp_path / "clip.wav"
    clip.touch()
    obs = Obs()
    stage = ObsStage(obs.call, Program("Studio", "Voice", "Caption"))
    playing = asyncio.create_task(stage.play(clip, "Prior stream"))
    await asyncio.wait_for(obs.playing.wait(), timeout=1)
    assert obs.caption_changes[-1] == ("PLAYING", "Prior stream")

    await stage.on_stream_state({"outputState": "OBS_WEBSOCKET_OUTPUT_STOPPING"})
    await stage.on_stream_state({"outputState": "OBS_WEBSOCKET_OUTPUT_STARTING"})
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(playing, timeout=1)
    assert obs.caption_changes[-1][1] == ""
    assert obs.status == "STOPPED" and obs.path == ""


async def test_old_end_and_source_replacement_do_not_complete_new_clip(tmp_path: Path):
    clip = tmp_path / "clip.wav"
    clip.touch()
    obs = Obs()
    obs.initial_state = "ENDED"
    stage = ObsStage(obs.call, Program("Studio", "Voice", "Caption"))
    running = asyncio.create_task(stage.preview(clip, "Caption"))
    await asyncio.sleep(0.07)
    assert not running.done()
    assert all(text == "" for _, text in obs.caption_changes)
    obs.status = "PLAYING"
    await asyncio.wait_for(obs.playing.wait(), timeout=1)
    assert obs.caption_changes[-1] == ("PLAYING", "Caption")
    obs.path = "/elsewhere/other.wav"
    obs.status = "ENDED"
    assert (await running).state == "interrupted"
    assert obs.caption_changes[-1][1] == ""
    assert obs.path == "/elsewhere/other.wav"
