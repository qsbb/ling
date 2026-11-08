"""媒体授权：主人授权一次，就对全部入站媒体生效。

## 为什么要有这组用例

原来授权判据只认**发言人本人**，而网页上的授权页只能给"登录的那个人"授权。
群里的发言人是别的 QQ 号，他们从没登录过、也没法授权——于是群里的图
**永远**描述不了，日志里只剩一行行「未授权描述入站媒体（scope=media.inbound）」。
用户看到的正是"群里发的图她从来不认识"。

现在的规则：**发言人本人 或 会话主人** 任一授权即放行（主人是
``windows.subject_id``，群窗口里就是主人，私聊窗口里两者同一个人）。
拒绝时的错误信息不变——仍然是"未授权"，只是判据多了一条。
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from ling.domains.consent.service import ConsentService
from ling.domains.conversation.service import ConversationService
from ling.domains.media.service import (
    INBOUND_SCOPE,
    ORIGIN_MODEL,
    MediaEvidenceError,
    MediaEvidenceService,
)
from ling.infra.db import session_scope
from ling.infra.models import MediaEvidenceRow
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
OWNER = "subj_owner"
SPEAKER = "subj_speaker"


async def _grant(subject: str) -> int:
    """授权并返回 revision（用例用它验"记的是实际生效的那一条"）。"""
    async with session_scope() as session:
        view = await ConsentService(session).grant(subject_id=subject, scope=INBOUND_SCOPE)
        await session.commit()
        return view.revision


async def _window(env: TestEnv, *, owner: str = OWNER) -> str:
    """建一个群窗口（``windows.subject_id`` 就是主人）。"""
    async with session_scope() as session:
        window = await ConversationService(session).create_window(
            subject_id=owner, title="onebot-x:group:100200400", channel="onebot"
        )
        window.visibility = "group_scope"
        await session.commit()
        return window.id


@pytest.fixture(autouse=True)
async def _clean(logged_in: TestEnv) -> None:  # noqa: F811
    from sqlalchemy import delete

    from ling.infra.models import ConsentRule

    async def wipe() -> None:
        async with session_scope() as session:
            await session.execute(
                delete(MediaEvidenceRow).where(MediaEvidenceRow.actor_id == ACTOR)
            )
            await session.execute(
                delete(ConsentRule).where(ConsentRule.subject_id.in_([OWNER, SPEAKER]))
            )

    await wipe()
    yield
    await wipe()


# ------------------------------------------------------------------ 判据


async def test_owner_consent_covers_someone_else(logged_in: TestEnv) -> None:  # noqa: F811
    """发言人没授权、主人授权了 → 放行，且记的是**主人的**那条授权。"""
    revision = await _grant(OWNER)
    window_id = await _window(logged_in)

    async with session_scope() as session:
        view = await MediaEvidenceService(session, actor_id=ACTOR).record(
            subject_id=SPEAKER,
            window_id=window_id,
            media_ref="obj/group-photo.jpg",
            consent_subjects=(OWNER,),
        )
        await session.commit()

    assert view.evidence_id
    async with session_scope() as session:
        row = await session.get(MediaEvidenceRow, view.evidence_id)
        assert row is not None
        assert row.subject_id == SPEAKER, "证据仍然挂在真正的上传者身上"
        assert row.consent_revision == revision, "记的必须是实际生效的那条授权"


async def test_speaker_consent_still_works(logged_in: TestEnv) -> None:  # noqa: F811
    """只授权发言人（主人没授权）→ 照旧放行，revision 是发言人的。"""
    revision = await _grant(SPEAKER)
    window_id = await _window(logged_in)

    async with session_scope() as session:
        view = await MediaEvidenceService(session, actor_id=ACTOR).record(
            subject_id=SPEAKER,
            window_id=window_id,
            media_ref="obj/own-photo.jpg",
            consent_subjects=(OWNER,),
        )
        await session.commit()

    async with session_scope() as session:
        row = await session.get(MediaEvidenceRow, view.evidence_id)
        assert row is not None and row.consent_revision == revision


async def test_neither_granted_is_still_refused(logged_in: TestEnv) -> None:  # noqa: F811
    """两边都没授权 → 仍然拒绝，措辞不变（"未授权"必须让人看得懂下一步）。"""
    window_id = await _window(logged_in)

    async with session_scope() as session:
        with pytest.raises(MediaEvidenceError, match=INBOUND_SCOPE) as exc:
            await MediaEvidenceService(session, actor_id=ACTOR).record(
                subject_id=SPEAKER,
                window_id=window_id,
                media_ref="obj/nope.jpg",
                consent_subjects=(OWNER,),
            )
        assert "未授权描述入站媒体" in str(exc.value)

    async with session_scope() as session:
        rows = (await session.execute(select(MediaEvidenceRow))).scalars().all()
        assert rows == [], "拒绝就是**不登记**，不能先落库再报错"


async def test_everyone_is_still_checked_when_owner_revokes(logged_in: TestEnv) -> None:  # noqa: F811
    """主人撤权后不再"一人授权全群通用"：只认各自授权过的人。"""
    await _grant(OWNER)
    async with session_scope() as session:
        await ConsentService(session).revoke(subject_id=OWNER, scope=INBOUND_SCOPE)
        await session.commit()
    window_id = await _window(logged_in)

    async with session_scope() as session:
        with pytest.raises(MediaEvidenceError):
            await MediaEvidenceService(session, actor_id=ACTOR).record(
                subject_id=SPEAKER,
                window_id=window_id,
                media_ref="obj/after-revoke.jpg",
                consent_subjects=(OWNER,),
            )


# ------------------------------------------------------------------ 描述侧


async def test_describe_accepts_the_owner_grant(logged_in: TestEnv) -> None:  # noqa: F811
    """描述发生在 worker 里，那边只知道窗口——所以那里也必须认主人的授权。

    只放宽 ``record`` 是不够的：登记成功、描述被拒，用户看到的仍然是
    "她不认识群里的图"。
    """
    await _grant(OWNER)
    window_id = await _window(logged_in)

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SPEAKER,
            window_id=window_id,
            media_ref="obj/describe-me.jpg",
            consent_subjects=(OWNER,),
        )
        await session.commit()
        served = await svc.describe(
            view.evidence_id, window_id=window_id, description="一张猫的照片"
        )
        await session.commit()

    assert served.description == "一张猫的照片"
    assert served.description_origin == ORIGIN_MODEL


async def test_describe_is_refused_after_owner_revokes(logged_in: TestEnv) -> None:  # noqa: F811
    """撤权之后不能再描述：授权是**当下**有效，不是登记那一刻的永久许可。"""
    await _grant(OWNER)
    window_id = await _window(logged_in)
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(
            subject_id=SPEAKER,
            window_id=window_id,
            media_ref="obj/revoke-later.jpg",
            consent_subjects=(OWNER,),
        )
        await session.commit()
        evidence_id = view.evidence_id

    async with session_scope() as session:
        await ConsentService(session).revoke(subject_id=OWNER, scope=INBOUND_SCOPE)
        await session.commit()

    async with session_scope() as session:
        with pytest.raises(MediaEvidenceError, match="撤"):
            await MediaEvidenceService(session, actor_id=ACTOR).describe(
                evidence_id, window_id=window_id, description="不该写进去"
            )


# ------------------------------------------------------------------ 日志降噪


def _bridge(session):  # noqa: ANN001, ANN202
    """只要 ``_ingest_media`` 需要的两个东西：会话与 actor。"""
    from ling.kernel.bridge import InboundBridge

    return InboundBridge(
        session,
        coordinator=object(),
        connector=object(),
        secret_box=object(),
        actor_id=ACTOR,
    )


def _envelope_with_two_media():  # noqa: ANN202
    from ling.contracts.v1 import InputPart, InteractionEnvelope, Modality

    return InteractionEnvelope(
        connector_instance="connector:onebot:test",
        external_event_id="evt_media_1",
        window_ref="onebot-x:group:100200400",
        account_ref="100200300",
        sender_name="群友",
        parts=[
            InputPart(modality=Modality.text, text="两张图"),
            InputPart(modality=Modality.image, media_ref="https://gchat.qpic.cn/a.jpg"),
            InputPart(modality=Modality.image, media_ref="https://gchat.qpic.cn/b.jpg"),
        ],
    )


async def test_one_message_with_many_media_logs_one_line(
    logged_in: TestEnv,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """一条消息 2 个媒体、都没授权 → 一行汇总，不是每个 ref 一行。"""
    window_id = await _window(logged_in)

    async with session_scope() as session:
        with caplog.at_level(logging.INFO, logger="ling.kernel.bridge"):
            recorded, note = await _bridge(session)._ingest_media(
                _envelope_with_two_media(),
                subject_id=SPEAKER,
                window_id=window_id,
                owner_subject_id=OWNER,
            )
        await session.commit()

    assert recorded == []
    assert note and "未授权" in note, note
    lines = [r.getMessage() for r in caplog.records if r.name == "ling.kernel.bridge"]
    assert [line for line in lines if "跳过" in line] == [
        "收到 2 个媒体，跳过 2 个：未授权描述入站媒体（授权记录 scope=media.inbound）"
    ]
    # 老写法（每个 ref 一行）必须消失
    assert not [line for line in lines if "ref=" in line], lines


async def test_multiple_media_are_all_recorded_when_owner_granted(
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """主人授权后，一条消息里的每个媒体都登记成功（降噪不能变成少登记）。"""
    await _grant(OWNER)
    window_id = await _window(logged_in)

    async with session_scope() as session:
        recorded, note = await _bridge(session)._ingest_media(
            _envelope_with_two_media(),
            subject_id=SPEAKER,
            window_id=window_id,
            owner_subject_id=OWNER,
        )
        await session.commit()

    assert len(recorded) == 2, recorded
    assert note is None


async def test_a_failed_media_write_does_not_kill_the_turn(
    logged_in: TestEnv,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """媒体登记撞 DB 错误（并发重投撞 `uq_media_evidence_dedupe` 就是这么来的）：
    这一条跳过、调用方的事务照常提交。

    修好之前：`MediaEvidenceService.record()` 里的 `flush()` 抛
    ``IntegrityError``，它不在 `_ingest_media` 里那个 ``MediaEvidenceError``
    分支上，于是一路穿透到 `InboundBridge.handle()` 外面 → "事件处理失败，
    撤回认领以便重投"——用户看到的是"发了图，她完全没反应"；而且失败的
    flush 把 session 标成待回滚，后面 ``commit()`` 还会再炸一次。
    """
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    from ling.infra import db as db_module

    await _grant(OWNER)
    window_id = await _window(logged_in)

    fired: list[str] = []

    def _fail_media_insert(  # noqa: ANN001, ARG001
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        if statement.lstrip().upper().startswith("INSERT") and "media_evidence" in statement:
            fired.append(statement)
            raise IntegrityError(statement, parameters, Exception("boom"))

    engine = db_module._engine
    assert engine is not None, "夹具没初始化引擎"
    event.listen(engine.sync_engine, "before_cursor_execute", _fail_media_insert)
    try:
        async with session_scope() as session:
            with caplog.at_level(logging.WARNING, logger="ling.kernel.bridge"):
                recorded, note = await _bridge(session)._ingest_media(
                    _envelope_with_two_media(),
                    subject_id=SPEAKER,
                    window_id=window_id,
                    owner_subject_id=OWNER,
                )
            # SAVEPOINT 回滚之后这个事务仍然干净：这一步抛错就说明没兜住
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _fail_media_insert)

    assert fired, "用例没打到媒体登记那一步"
    assert recorded == []
    assert note and "媒体登记失败" in note, note
    warnings = [
        record
        for record in caplog.records
        if getattr(record, "event_name", "") == "media_evidence_record_failed"
    ]
    assert warnings and warnings[-1].attributes["reason_code"] == "media_evidence_record_failed"

    # 失败的写没有留下半条证据
    from sqlalchemy import func
    from sqlalchemy import select as _select

    async with session_scope() as session:
        count = (
            await session.execute(
                _select(func.count())
                .select_from(MediaEvidenceRow)
                .where(MediaEvidenceRow.actor_id == ACTOR)
            )
        ).scalar_one()
    assert count == 0

