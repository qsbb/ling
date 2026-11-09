"""看视频额度用完时：把**事实**交给主回复链路，让她按人设自然地回应。

用户拍板的三句原话：

1. 「看视频这个模型 直接使用设定的看视频模型」——不换供应商、不 fallback；
2. 「次数限制去掉」；
3. 「看视频额度如果没了的话，直接把事实告诉主回复链路，让他进行按人设自然的
   回复，例如懒得看或者什么样」/「如果没 token 支持他消耗看视频了，他也可以说
   今天累了或者怎么样，反正根据人设来」。

## 这个文件钉的四件事

1. **额度不足 → 不调视觉模型**（`describer.calls == 0`）：不能先抽帧、编码、
   发请求，最后才发现额度没了。
2. **额度不足 → 走同轮二次生成**，提示词里是「没能看」+「不要说你看过了」，
   而不是让她在"什么都没发生"的情况下沉默。
3. **两类失败的措辞不同**：额度不足偏"今天不想看了/有点累"（这不是故障），
   取不到内容偏"看不了/打不开"（这是真的没读到）。混成一句就等于让其中一种
   在说谎。
4. **`observed` 必须为空**：走了二次生成不等于"看到了"，不能塞假观察。

## 为什么断言的是 fake provider 收到的 messages

用户看到的是她最终说的那句话。"她有没有假装看过"只能从**喂进去的提示词**
与**生成的内容**两头看，而不能只看 `WatchOutcome` 的字段——字段正确、提示词
里却写着观察内容的话，她照样会说"我看到一只猫"。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from ling.domains.conversation.service import ConversationService
from ling.infra.db import session_scope
from ling.kernel.turn import TurnCoordinator
from ling.models.fake import FakeProvider
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from ling.ports.model import ModelChunk
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
PERSONA = "persona_ling"


def _config() -> EffectiveConfig:
    return EffectiveConfig(
        revision=1,
        providers={
            "fake": ProviderConfig(
                provider_id="fake", kind="fake", enabled=True, extra={"chunk_delay": 0.0}
            )
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast", provider_id="fake", model_id="deterministic"
            )
        },
        test_mode=True,
    )


class _ScriptedProvider(FakeProvider):
    """按顺序回放预设文本，并记下**它收到的每一份 messages**。

    记 messages 是关键：二次生成的约束写在**提示词**里，只断输出的话，
    一个"碰巧没提视频"的输出会让测试假绿。
    """

    def __init__(self, replies: list[str]) -> None:
        super().__init__(chunk_delay=0.0)
        self._replies = list(replies)
        self.requests: list[list[object]] = []

    async def stream(self, request):  # noqa: ANN001, ANN201
        self.requests.append(list(request.messages))
        text = self._replies.pop(0) if self._replies else "嗯。"
        for index in range(0, len(text), 4):
            yield ModelChunk(delta=text[index : index + 4])
        yield ModelChunk(done=True, finish_reason="stop")


class _Registry(ProviderRegistry):
    def __init__(self, provider: _ScriptedProvider) -> None:
        super().__init__(lambda _pid: None)
        self.provider = provider

    def get(self, cfg):  # noqa: ANN001, ANN201
        return self.provider


async def _run_turn(
    env: TestEnv,
    *,
    first_reply: str,
    follow_up_reply: str = "",
    watch_video=None,  # noqa: ANN001
) -> tuple[_ScriptedProvider, list[tuple[str, dict]]]:
    """跑一轮真实轮次，返回 (provider, 事件列表)。"""
    provider = _ScriptedProvider([first_reply, follow_up_reply or "嗯。"])
    coordinator = TurnCoordinator(
        router=ModelRouter(_Registry(provider)),
        actor_id=ACTOR,
        persona_id=PERSONA,
        cancels=env.app.state.cancels,
        stream_log=env.app.state.stream_log,
        watch_video=watch_video,
    )
    response = await env.client.post("/v1/sessions", json={"title": "看视频额度"})
    assert response.status_code == 201, response.text
    window_id = response.json()["session_id"]
    async with session_scope() as session:
        conv = ConversationService(session)
        turn = await conv.create_turn(
            window_id, client_turn_id=f"ct-{uuid.uuid4().hex[:8]}", config_revision=1
        )
        await conv.add_message(window_id, role="user", text="那个视频里是什么", turn_id=turn.id)
        turn_id = turn.id

    queue = await coordinator.run_turn(
        window_id=window_id,
        subject_id=env.subject_id,
        turn_id=turn_id,
        user_text="那个视频里是什么",
        config=_config(),
        attempt=1,
    )
    events: list[tuple[str, dict]] = []
    while True:
        item = await asyncio.wait_for(queue.get(), timeout=60)
        if item is None:
            break
        events.append((item.event, item.data))
    return provider, events


def _reply(events: list[tuple[str, dict]]) -> str:
    completed = [data for name, data in events if name == "turn.completed"]
    assert completed, f"轮次没完成：{[n for n, _ in events]}"
    message = completed[-1].get("message") or {}
    return str(message.get("text") or "")


def _last_prompt(provider: _ScriptedProvider) -> str:
    """二次生成那一轮收到的完整提示词。"""
    assert len(provider.requests) >= 2, (
        f"没有走二次生成（只调了 {len(provider.requests)} 次模型）"
    )
    return "\n".join(str(getattr(m, "content", "")) for m in provider.requests[-1])


# ---------------------------------------------------------------- 1. 额度不足


async def test_quota_exhausted_triggers_follow_up_with_honest_constraint(
    logged_in: TestEnv,
) -> None:
    """额度用完：不调视觉模型 → 走同轮二次生成 → 提示词是"没能看"+不许谎称。"""
    watched: list[list[str]] = []

    async def _denied(window_id: str, refs: list[str]) -> dict[str, object]:
        watched.append(list(refs))
        return {
            "observed": "",
            "skipped": "今天的看视频额度用完了（已用 200000 / 上限 200000 tokens）",
            "denied_kind": "quota",
            "reads": 0,
        }

    provider, events = await _run_turn(
        logged_in, first_reply="<<看视频 m3f2a1>>", watch_video=_denied
    )
    assert watched, "标记没有被解析成看视频请求"

    prompt = _last_prompt(provider)
    # (a) 事实：告诉她没看成
    assert "没能" in prompt or "额度" in prompt, prompt[-600:]
    assert "200000" in prompt, "没把已用/上限带进去，她无从解释为什么"
    # (b) 边界：不许声称看过
    assert "不要说你看过了" in prompt
    assert "不要说视频里是什么" in prompt
    # (c) 额度用尽偏"今天没余力"，不是技术故障
    assert "余力" in prompt or "懒得" in prompt or "累" in prompt

    # (d) 走的是同轮二次生成（对外仍只有一条回复）
    assert len(provider.requests) == 2, (
        f"模型被调了 {len(provider.requests)} 次，二次生成没发生"
    )
    reply = _reply(events)
    assert reply, "二次生成之后仍然没有正文"


async def test_quota_reply_does_not_claim_having_watched(logged_in: TestEnv) -> None:
    """生成的结果里**不能**有"我看过了/视频里是"这类声称。

    用一个"照提示词写"的模型：它按 `_watch_follow_up_fact` 的引导回答。
    断言的是**提示词 + 输出**两头都对——只断一头会漏掉"提示词对但模型编了"。
    """

    async def _denied(window_id: str, refs: list[str]) -> dict[str, object]:
        return {
            "observed": "",
            "skipped": "今天的看视频额度用完了（已用 200000 / 上限 200000 tokens）",
            "denied_kind": "quota",
            "reads": 0,
        }

    provider, events = await _run_turn(
        logged_in,
        first_reply="<<看视频 m3f2a1>>",
        follow_up_reply="今天有点累，懒得看了，你直接说给我听吧。",
        watch_video=_denied,
    )
    reply = _reply(events)
    for forbidden in ("我看过了", "我看了", "视频里是", "里面是"):
        assert forbidden not in reply, f"她谎称看过了：{reply!r}"
    # 而她确实按人设说了"不想看/累"
    assert "累" in reply or "懒得" in reply or "不想" in reply
    # 提示词里明确禁止了这件事
    assert "不要说你看过了" in _last_prompt(provider)


# ---------------------------------------------------------------- 2. 其他失败


async def test_other_failure_uses_a_different_wording(logged_in: TestEnv) -> None:
    """取不到内容 → 说"看不了/打不开"，**不是**"今天不想看"。"""

    async def _broken(window_id: str, refs: list[str]) -> dict[str, object]:
        return {
            "observed": "",
            "skipped": "取不到视频内容（ConnectError）",
            "denied_kind": "error",
            "reads": 0,
        }

    provider, _events = await _run_turn(
        logged_in, first_reply="<<看视频 m3f2a1>>", watch_video=_broken
    )
    prompt = _last_prompt(provider)
    # 事实
    assert "取不到视频内容" in prompt
    # 边界照旧
    assert "不要说你看过了" in prompt
    # 措辞是"打不开"类，**不是**"今天没余力"
    assert "打不开" in prompt or "看不到" in prompt
    assert "余力" not in prompt, "取不到内容不该说成'今天没余力'"


async def test_missing_ref_also_uses_look_cannot_read_wording(
    logged_in: TestEnv,
) -> None:
    """短号不存在/过期：同样归"看不了"，不是额度不足。"""

    async def _missing(window_id: str, refs: list[str]) -> dict[str, object]:
        return {
            "observed": "",
            "skipped": "短号不存在或已过期",
            "denied_kind": "missing",
            "reads": 0,
        }

    provider, _events = await _run_turn(
        logged_in, first_reply="<<看视频 mzzzzz>>", watch_video=_missing
    )
    prompt = _last_prompt(provider)
    assert "短号不存在" in prompt
    assert "不要说你看过了" in prompt
    assert "余力" not in prompt


# ---------------------------------------------------------------- 3. 看到了


async def test_successful_read_uses_observation_not_the_skip_path(
    logged_in: TestEnv,
) -> None:
    """额度充足且读到了：提示词里是"看到的是"，**不是**"没能看。"""

    async def _ok(window_id: str, refs: list[str]) -> dict[str, object]:
        return {
            "observed": "- m3f2a1：一只花猫从桌上跳下去",
            "skipped": None,
            "denied_kind": None,
            "reads": 1,
        }

    provider, events = await _run_turn(
        logged_in,
        first_reply="<<看视频 m3f2a1>>",
        follow_up_reply="我刚看了眼，一只花猫从桌上跳下去了。",
        watch_video=_ok,
    )
    prompt = _last_prompt(provider)
    assert "一只花猫从桌上跳下去" in prompt
    assert "没能" not in prompt, "看成了却走了'没看成'的措辞"
    assert "不要说你看过了" not in prompt, "看到了就不该禁止她说"
    assert "跳下去" in _reply(events)


# ---------------------------------------------------------------- 4. 次数限制已删


def test_count_limits_are_gone() -> None:
    """次数限制（每窗口每小时 2 / 每天 20）已删：模块里不该再有那套符号。"""
    import ling.domains.media.video as video
    import ling.domains.media.watch as watch

    for module in (video, watch):
        for name in (
            "WATCH_BUDGET",
            "WINDOW_HOURLY_LIMIT",
            "DAILY_LIMIT",
            "WatchBudget",
        ):
            assert not hasattr(module, name), f"{module.__name__} 还残留 {name}"


async def test_many_reads_in_one_window_are_not_rate_limited(
    logged_in: TestEnv,
) -> None:
    """连看 5 条视频不再被"每小时 2 次"拦（额度充足时全部读成）。

    这是"次数限制去掉"的行为级证据：字段/常量删掉容易，而**真的不再拦**
    要跑一遍才知道。
    """
    from ling.domains.media.service import EvidenceView
    from ling.domains.media.watch import VideoWatchService

    class _Describer:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def describe_video(self, evidence, *, mode: str = "frames"):  # noqa: ANN001, ANN201
            from ling.domains.media.describe import VideoRead

            self.calls.append(evidence.evidence_id)
            return VideoRead(text="一段很短的生活记录", mode="frames", frames=4, fps=0.5)

    class _Service:
        def __init__(self, videos: list[EvidenceView]) -> None:
            self._videos = videos

        async def undescribed_videos(self, *, window_id: str, limit: int = 5):  # noqa: ANN001, ANN201
            return [v for v in self._videos if v.description is None]

        async def described_in_window(self, *, window_id: str, limit: int = 5):  # noqa: ANN001, ANN201
            return [v for v in self._videos if v.description is not None]

    def _video(evidence_id: str) -> EvidenceView:
        return EvidenceView(
            evidence_id=evidence_id,
            window_id="w1",
            kind="video",
            media_ref="https://example.test/a.mp4",
            origin="user_upload",
            visibility="window_private",
            description=None,
            description_origin=None,
            expires_at=None,
        )

    videos = [_video(f"media_{index:04d}aaaa") for index in range(5)]
    describer = _Describer()
    service = VideoWatchService.__new__(VideoWatchService)
    service._actor_id = ACTOR  # noqa: SLF001
    service._describer = describer  # noqa: SLF001
    service._quota_denial = None  # noqa: SLF001
    service._service = _Service(videos)  # noqa: SLF001
    service._session = None  # noqa: SLF001

    # 一轮最多读 2 条（`MAX_READS_PER_REPLY`，那是"她一次写五个标记多半是幻觉"，
    # 与次数额度无关）。连跑多轮，每次换一条，验证**跨轮不再累积次数**。
    from ling.domains.media.video import short_ref_for

    for video in videos:
        outcome = await service.watch(
            window_id="w1", refs=[short_ref_for(video.evidence_id)], mode="frames"
        )
        assert outcome.ok, f"{video.evidence_id} 被拦了：{outcome.skipped}"
        assert outcome.denied_kind is None

    assert len(describer.calls) == 5, f"只读了 {len(describer.calls)} 条"
