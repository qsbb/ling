"""上下文里的"你还没看这个视频"提示。

视频默认**不自动读**（见 `domains/media/watch.py`），所以这一行是她唯一
知道"有个视频可以看"的途径。两条要求：

1. 没看过的视频要出现在上下文里，并给出**可执行**的标记（短号）；
2. 已经看过的**不重复提示**（否则每一轮都在提醒她"你还没看"，而那件事
   早就过去了）。
"""

from __future__ import annotations

import pytest

from ling.domains.conversation.service import ConversationService
from ling.domains.media.service import MediaEvidenceService
from ling.infra.db import session_scope
from ling.kernel.context import ContextCompiler
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


async def _window(env: TestEnv) -> str:
    async with session_scope() as session:
        window = await ConversationService(session, actor_id=ACTOR).create_window(
            subject_id=env.subject_id
        )
        await session.commit()
        return window.id


async def _record_video(env: TestEnv, window_id: str, ref: str) -> str:
    async with session_scope() as session:
        from ling.domains.consent.service import ConsentService
        from ling.domains.media.service import INBOUND_SCOPE

        await ConsentService(session).grant(subject_id=env.subject_id, scope=INBOUND_SCOPE)
        view = await MediaEvidenceService(session, actor_id=ACTOR).record(
            subject_id=env.subject_id, window_id=window_id, media_ref=ref, kind="video"
        )
        await session.commit()
        return view.evidence_id


async def _compile(env: TestEnv, window_id: str) -> str:
    async with session_scope() as session:
        compiled = await ContextCompiler(
            session, actor_id=ACTOR, persona_id="persona_ling"
        ).compile(
            subject_id=env.subject_id,
            window_id=window_id,
            user_text="在吗",
            is_group=False,
            history_limit=5,
        )
    return "\n".join(message.content for message in compiled.messages)


async def test_unseen_video_shows_up_with_a_usable_marker(logged_in: TestEnv) -> None:  # noqa: F811
    from ling.domains.media.video import short_ref_for

    window_id = await _window(logged_in)
    evidence_id = await _record_video(logged_in, window_id, "https://example.test/a.mp4")

    text = await _compile(logged_in, window_id)

    assert "你还没看" in text
    assert f"<<看视频 {short_ref_for(evidence_id)}>>" in text
    # 也要说清标记不会发出去，否则她不敢用
    assert "不会发出去" in text


async def test_described_video_is_not_prompted_again(logged_in: TestEnv) -> None:  # noqa: F811
    window_id = await _window(logged_in)
    evidence_id = await _record_video(logged_in, window_id, "https://example.test/b.mp4")

    async with session_scope() as session:
        await MediaEvidenceService(session, actor_id=ACTOR).describe(
            evidence_id, window_id=window_id, description="一只猫在跳"
        )
        await session.commit()

    text = await _compile(logged_in, window_id)

    assert "你还没看" not in text, "看过的就别再提示了"
    assert "一只猫在跳" in text, "看过的要按描述进上下文"
