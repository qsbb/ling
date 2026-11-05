"""媒体证据：图片被看到还是被猜的。

阶段 6 的两条硬规则，本文件逐条钉住：

1. **只在授权窗口被描述** —— 没 ``media.inbound`` 授权就拒绝，不是静默忽略；
   别的窗口读不到、也描述不了
2. **图像猜测不写事实** —— 模型描述 ``fact_eligible=False``，哪怕它看起来
   再确定，也不能变成"用户养了一只猫"这样的记忆

外加：过期即失效、撤权后不能再描述、同一来源事件不多记。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.consent.service import ConsentService
from ling.domains.media.service import (
    INBOUND_SCOPE,
    ORIGIN_HUMAN,
    ORIGIN_MODEL,
    MediaEvidenceError,
    MediaEvidenceService,
    dedupe_key_for,
)
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
SUBJECT = "subj_media_test"
WINDOW = "win_media_a"
OTHER_WINDOW = "win_media_b"


async def _grant(subject: str = SUBJECT, *, granted: bool = True) -> None:
    async with session_scope() as session:
        svc = ConsentService(session)
        if granted:
            await svc.grant(subject_id=subject, scope=INBOUND_SCOPE)
        else:
            await svc.revoke(subject_id=subject, scope=INBOUND_SCOPE)


async def _clean() -> None:
    """清掉本测试的授权与证据，保证可重复运行。"""
    from sqlalchemy import delete

    from ling.infra.models import ConsentRule, MediaEvidenceRow

    async with session_scope() as session:
        await session.execute(
            delete(MediaEvidenceRow).where(MediaEvidenceRow.actor_id == ACTOR)
        )
        await session.execute(
            delete(ConsentRule).where(ConsentRule.subject_id.like("subj_media%"))
        )


@pytest.fixture(autouse=True)
async def _reset(logged_in: TestEnv) -> None:
    await _clean()
    yield
    await _clean()


# ---------------------------------------------------------------- 硬规则 1：授权


async def test_record_without_consent_is_refused(logged_in: TestEnv) -> None:
    """没有 media.inbound 授权就拒绝登记。

    **不能静默忽略**：那样"用户发了图但角色没反应"会变成查不出来的怪现象。
    """
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        with pytest.raises(MediaEvidenceError, match=INBOUND_SCOPE):
            await svc.record(
                subject_id=SUBJECT,
                window_id=WINDOW,
                media_ref="obj/photo-1.jpg",
            )


async def test_record_with_consent_succeeds(logged_in: TestEnv) -> None:
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SUBJECT, window_id=WINDOW, media_ref="obj/photo-1.jpg"
        )
        assert view.description is None, "刚登记时还没有描述"
        assert not view.fact_eligible


async def test_recording_requires_subject(logged_in: TestEnv) -> None:
    """入站媒体必须带上传者，否则无法校验授权。"""
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        with pytest.raises(MediaEvidenceError):
            await svc.record(subject_id=None, window_id=WINDOW, media_ref="x.jpg")


# ---------------------------------------------------------------- 硬规则 2：猜测


async def test_model_description_is_never_fact(logged_in: TestEnv) -> None:
    """模型看图说"这是一只猫"只是猜测，不能变成事实。"""
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SUBJECT, window_id=WINDOW, media_ref="obj/cat.jpg"
        )
        described = await svc.describe(
            view.evidence_id,
            window_id=WINDOW,
            description="这是一只橘猫，大概两岁",
            description_origin=ORIGIN_MODEL,
        )
        assert described.description is not None
        assert described.fact_eligible is False, "模型推断绝不能当事实"

        facts = await svc.fact_descriptions(window_id=WINDOW)
        assert facts == [], f"模型推断漏进了事实：{facts}"


async def test_human_description_is_fact(logged_in: TestEnv) -> None:
    """人给的描述可以作为事实——这是模型推断的对照组。"""
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SUBJECT, window_id=WINDOW, media_ref="obj/cat.jpg"
        )
        described = await svc.describe(
            view.evidence_id,
            window_id=WINDOW,
            description="我家橘猫，叫团子",
            description_origin=ORIGIN_HUMAN,
        )
        assert described.fact_eligible is True

        facts = await svc.fact_descriptions(window_id=WINDOW)
        assert facts == ["我家橘猫，叫团子"]


async def test_empty_description_is_rejected(logged_in: TestEnv) -> None:
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SUBJECT, window_id=WINDOW, media_ref="obj/x.jpg"
        )
        with pytest.raises(MediaEvidenceError):
            await svc.describe(view.evidence_id, window_id=WINDOW, description="   ")


# ---------------------------------------------------------------- 窗口隔离


async def test_other_window_cannot_read(logged_in: TestEnv) -> None:
    """别的窗口读不到——返回 None，不是抛错（不给存在性探测口）。"""
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SUBJECT, window_id=WINDOW, media_ref="obj/x.jpg"
        )
        assert await svc.get(view.evidence_id, window_id=WINDOW) is not None
        assert await svc.get(view.evidence_id, window_id=OTHER_WINDOW) is None


async def test_other_window_cannot_describe(logged_in: TestEnv) -> None:
    """别的窗口连"存在"都不该知道：报错文案统一为"不存在"。"""
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SUBJECT, window_id=WINDOW, media_ref="obj/x.jpg"
        )
        with pytest.raises(MediaEvidenceError, match="不存在"):
            await svc.describe(
                view.evidence_id, window_id=OTHER_WINDOW, description="偷看"
            )


# ---------------------------------------------------------------- 时效与撤权


async def test_expired_evidence_is_invisible(logged_in: TestEnv) -> None:
    """过期后读不到，也不出现在待描述列表里。"""
    await _grant()
    past = datetime.now(UTC) - timedelta(minutes=1)
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SUBJECT,
            window_id=WINDOW,
            media_ref="obj/old.jpg",
            expires_at=past,
        )
        assert await svc.get(view.evidence_id, window_id=WINDOW) is None
        assert await svc.pending(window_id=WINDOW) == []


async def test_revoked_consent_blocks_describe(logged_in: TestEnv) -> None:
    """撤销授权后不能再描述新内容——授权在描述时重新检查。"""
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SUBJECT, window_id=WINDOW, media_ref="obj/x.jpg"
        )
        await _grant(granted=False)
        with pytest.raises(MediaEvidenceError, match="撤销"):
            await svc.describe(view.evidence_id, window_id=WINDOW, description="补描述")


async def test_pending_lists_undescribed_only(logged_in: TestEnv) -> None:
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        first = await svc.record(
            subject_id=SUBJECT, window_id=WINDOW, media_ref="obj/a.jpg"
        )
        await svc.record(subject_id=SUBJECT, window_id=WINDOW, media_ref="obj/b.jpg")

        assert len(await svc.pending(window_id=WINDOW)) == 2

        await svc.describe(first.evidence_id, window_id=WINDOW, description="一张图")
        remaining = await svc.pending(window_id=WINDOW)
        assert len(remaining) == 1
        assert remaining[0].media_ref == "obj/b.jpg"


# ---------------------------------------------------------------- 去重


async def test_same_source_event_does_not_duplicate(logged_in: TestEnv) -> None:
    """同一来源事件重复回调只产生一条证据。"""
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        first = await svc.record(
            subject_id=SUBJECT,
            window_id=WINDOW,
            media_ref="obj/same.jpg",
            source_event_id="evt-42",
        )
        second = await svc.record(
            subject_id=SUBJECT,
            window_id=WINDOW,
            media_ref="obj/same.jpg",
            source_event_id="evt-42",
        )
        assert first.evidence_id == second.evidence_id
        assert len(await svc.pending(window_id=WINDOW)) == 1
        assert dedupe_key_for("evt-42") == "media:evt-42"


async def test_unknown_kind_is_rejected(logged_in: TestEnv) -> None:
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        with pytest.raises(MediaEvidenceError, match="未知媒体类型"):
            await svc.record(
                subject_id=SUBJECT, window_id=WINDOW, media_ref="x.bin", kind="hologram"
            )


async def test_same_media_ref_in_two_windows_are_two_evidences(logged_in: TestEnv) -> None:  # noqa: ANN001
    """去重必须按窗口。

    原来 `_by_dedupe` 全局按 key 查：同一张图在两个窗口里，后一个窗口拿到的
    是**前一个窗口的** evidence——`window_id` 对不上，描述任务按窗口取待办
    永远取不到，而且跨窗口引用了别人的证据。

    这是上传链路的真 bug：占位 ref 固定成 "pending:upload" 时，从第二张图起
    全都返回第一张图的证据。
    """
    from ling.domains.media.service import MediaEvidenceService
    from ling.infra.db import session_scope

    # 这条测试不打 HTTP，所以自己把授权补上（record() 会重新检查授权）
    await _grant(SUBJECT)

    a = (
        await logged_in.client.post("/v1/sessions", json={"title": "去重A"})
    ).json()["session_id"]
    b = (
        await logged_in.client.post("/v1/sessions", json={"title": "去重B"})
    ).json()["session_id"]

    ids = []
    for window in (a, b):
        async with session_scope() as session:
            view = await MediaEvidenceService(session, actor_id="actor_ling").record(
                subject_id=SUBJECT,
                window_id=window,
                media_ref="pending:upload:same-content-hash",
                kind="image",
            )
        ids.append(view.evidence_id)

    assert ids[0] != ids[1], (
        f"两个窗口拿到了同一条证据 {ids[0]}——后一个窗口会引用别人的图"
    )
