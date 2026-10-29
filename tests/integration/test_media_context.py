"""媒体证据必须真正喂进上下文——而且推断不能伪装成事实。

第 34 轮把描述写进了库，但**没有任何人读它**：`fact_descriptions()` 无调用方，
`described_in_window()` 也还没被上下文用上。本轮把它接上。

四条硬约束：

1. 模型推断进上下文，但**标注为不确定**，`origin=model_inference`
2. 混合（有人说的也有推断的）取**较弱**保证：整块按推断标注
3. 别的窗口的证据不串进来（媒体是 window_private）
4. 推断仍然**不进** `fact_descriptions`——上下文里出现过不等于成为事实
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.consent.service import ConsentService
from ling.domains.media.service import (
    INBOUND_SCOPE,
    ORIGIN_HUMAN,
    ORIGIN_MODEL,
    MediaEvidenceService,
)
from ling.infra.db import session_scope
from ling.kernel.context import ContextCompiler
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"


def subject_of(env: TestEnv) -> str:
    """窗口归属与授权都必须用**登录者**的 subject，否则 get_window 会 404。"""
    return env.subject_id

INFERRED = "桌上有一只橘猫（推断）"
STATED = "这是我家的猫"


async def _clean() -> None:
    from sqlalchemy import delete

    from ling.infra.models import ConsentRule, MediaEvidenceRow

    async with session_scope() as session:
        await session.execute(
            delete(MediaEvidenceRow).where(MediaEvidenceRow.actor_id == ACTOR)
        )
        await session.execute(
            delete(ConsentRule).where(ConsentRule.subject_id.like("subj_media_ctx%"))
        )


@pytest.fixture(autouse=True)
async def _reset(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _clean()
    yield
    await _clean()


async def _window(env: TestEnv, title: str = "media") -> str:
    r = await env.client.post("/v1/sessions", json={"title": title})
    return r.json()["session_id"]


async def _evidence(
    *,
    subject: str,
    window_id: str,
    description: str,
    origin: str = ORIGIN_MODEL,
    ref: str = "obj/pic.jpg",
    expires_in: int | None = None,
) -> str:
    async with session_scope() as session:
        await ConsentService(session).grant(subject_id=subject, scope=INBOUND_SCOPE)
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=subject,
            window_id=window_id,
            media_ref=ref,
            expires_at=(
                datetime.now(UTC) + timedelta(minutes=expires_in)
                if expires_in is not None
                else None
            ),
        )
        await svc.describe(
            view.evidence_id,
            window_id=window_id,
            description=description,
            description_origin=origin,
        )
        return view.evidence_id


def _lane(compiled: object, name: str):  # noqa: ANN202
    for contribution in compiled.contributions:  # type: ignore[attr-defined]
        if contribution.lane == name:
            return contribution
    return None


async def _compile(subject: str, window_id: str, text: str = "这图是什么"):  # noqa: ANN202
    async with session_scope() as session:
        return await ContextCompiler(session, actor_id=ACTOR).compile(
            subject_id=subject, window_id=window_id, user_text=text
        )


# ---------------------------------------------------------------- 注入


async def test_inferred_description_enters_context_labeled(logged_in: TestEnv) -> None:
    """推断进上下文，但必须标成不确定、且 origin=model_inference。"""
    window_id = await _window(logged_in)
    await _evidence(subject=logged_in.subject_id, window_id=window_id, description=INFERRED, origin=ORIGIN_MODEL)

    compiled = await _compile(logged_in.subject_id, window_id)
    media = _lane(compiled, "media")
    assert media is not None, "媒体证据没进上下文"
    assert INFERRED in media.text
    # 断言**紧跟在描述后面**的那条标注，而不是全文里出现过"推断"二字——
    # 末尾的说明句本身就含"推断"，只查包含关系的话，
    # 把每条的具体标注删掉测试依然是绿的（实测过）。
    assert f"{INFERRED}（你自己看图推断的，不确定）" in media.text, (
        f"推断没有被逐条标注：{media.text}"
    )
    assert "不要当成对方告诉过你的事实" in media.text
    assert media.origin.value == "model_inference"
    assert media.visibility.value == "window_private"


async def test_stated_description_enters_as_user_statement(logged_in: TestEnv) -> None:
    """人给的描述按用户陈述进上下文——这是推断的对照组。"""
    window_id = await _window(logged_in)
    await _evidence(subject=logged_in.subject_id, window_id=window_id, description=STATED, origin=ORIGIN_HUMAN)

    compiled = await _compile(logged_in.subject_id, window_id)
    media = _lane(compiled, "media")
    assert media is not None
    assert STATED in media.text
    assert media.origin.value == "user_statement"
    assert "不确定" not in media.text


async def test_mixed_takes_weaker_guarantee(logged_in: TestEnv) -> None:
    """只要有一张是推断的，整块按推断标注。

    不能因为"其余是人说的"就放宽——那样模型会把推断那部分一起当事实。
    """
    window_id = await _window(logged_in)
    await _evidence(subject=logged_in.subject_id, window_id=window_id, description=STATED, origin=ORIGIN_HUMAN, ref="a.jpg")
    await _evidence(subject=logged_in.subject_id, window_id=window_id, description=INFERRED, origin=ORIGIN_MODEL, ref="b.jpg")

    compiled = await _compile(logged_in.subject_id, window_id)
    media = _lane(compiled, "media")
    assert media is not None
    assert media.origin.value == "model_inference", "混合时取了较强保证"


# ---------------------------------------------------------------- 边界


async def test_other_window_media_does_not_leak(logged_in: TestEnv) -> None:
    """媒体是 window_private：别的窗口的证据不能串进来。"""
    mine = await _window(logged_in, "mine")
    other = await _window(logged_in, "other")
    await _evidence(subject=logged_in.subject_id, window_id=other, description="别人的猫", origin=ORIGIN_MODEL)

    compiled = await _compile(logged_in.subject_id, mine)
    assert _lane(compiled, "media") is None, "别的窗口的媒体泄漏了"


async def test_expired_media_is_not_injected(logged_in: TestEnv) -> None:
    """过期证据不进上下文。

    注意构造顺序：不能先建一条已过期的再描述——`describe` 会（正确地）
    拒绝过期证据。所以先正常建+描述，再把 expires_at 拨到过去，
    这样测的是**读取侧**的门，而不是创建侧的门。
    """
    from ling.infra.models import MediaEvidenceRow

    window_id = await _window(logged_in)
    evidence_id = await _evidence(
        subject=logged_in.subject_id,
        window_id=window_id,
        description=INFERRED,
        origin=ORIGIN_MODEL,
    )
    async with session_scope() as session:
        row = await session.get(MediaEvidenceRow, evidence_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)

    compiled = await _compile(logged_in.subject_id, window_id)
    assert _lane(compiled, "media") is None, "过期证据进了上下文"


async def test_no_media_no_lane(logged_in: TestEnv) -> None:
    window_id = await _window(logged_in)
    compiled = await _compile(logged_in.subject_id, window_id)
    assert _lane(compiled, "media") is None


# ---------------------------------------------------------------- 事实边界


async def test_inference_in_context_is_still_not_fact(logged_in: TestEnv) -> None:
    """上下文里出现过 ≠ 成为事实。这是整条设计成立的前提。"""
    window_id = await _window(logged_in)
    evidence_id = await _evidence(
        subject=logged_in.subject_id,
        window_id=window_id, description=INFERRED, origin=ORIGIN_MODEL
    )

    compiled = await _compile(logged_in.subject_id, window_id)
    assert _lane(compiled, "media") is not None, "前置条件：确实进了上下文"

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=window_id)
        assert view is not None
        assert view.fact_eligible is False
        assert await svc.fact_descriptions(window_id=window_id) == [], (
            "推断在进了上下文之后变成了事实"
        )
