"""媒体上下文块必须**能逐条指认**，否则她会把上一条的内容说成本条的。

## 缺陷来源（R17 取证，2026-11-10，真实模型 10 轮）

A/B 共 10 轮里有 3 轮的**首次生成**（读视频之前）说出了**上一轮**视频的内容：

- A2（本轮 blue/A2）→ 首答「看了，红底白字一个 **A1**」
- A4（本轮 yellow/A4）→ 首答「看了，绿底白字一个 **A3**」
- B3（本轮 pink/B3）→ 首答「看了，就个青底白字 **B2** 的静态画面」

三次全部指向上一轮；而这三轮读到观察之后的第二次生成都正确。
反证：内容来自上下文里的**旧描述**，不是本轮视频。

根因在 `context.py` 的三处结构性缺陷：
1. 已描述条目不带任何标识，而"待看"那条带 `<<看视频 mXXXXXX>>`；
2. 时间只到分钟，同分钟多条渲染成逐字相同的前缀；
3. 文案写死"图片/语音"，列表里装的却是视频。

本文件钉住的正是这三条，以及"别把短号用在不能看的媒体上"。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ling.domains.media.service import MediaEvidenceService
from ling.domains.media.video import short_ref_for
from ling.infra.db import session_scope
from ling.infra.models import MediaEvidenceRow
from ling.kernel.context import ContextCompiler
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _window(env: TestEnv) -> str:
    r = await env.client.post("/v1/sessions", json={"title": "ctx-media-id"})
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


async def _seed_media(
    env: TestEnv,
    window_id: str,
    *,
    kind: str,
    description: str | None,
    created_at: datetime | None = None,
    media_ref: str | None = None,
) -> str:
    """落一条媒体证据；``description`` 非空 = 已看过。返回 evidence_id。"""
    async with session_scope() as session:
        media = MediaEvidenceService(session, actor_id="actor_ling")
        row = await media.record(
            subject_id=env.subject_id,
            window_id=window_id,
            media_ref=media_ref or f"https://example.invalid/{uuid.uuid4().hex}.bin",
            kind=kind,
            consent_subjects=[env.subject_id],
        )
        if description is not None:
            await media.describe(row.evidence_id, window_id=window_id, description=description)
        if created_at is not None:
            # 直接改时间：要构造"同一分钟内多条"，不能靠 sleep。
            orm = await session.get(MediaEvidenceRow, row.evidence_id)
            orm.created_at = created_at
            session.add(orm)
        return row.evidence_id


async def _compile(env: TestEnv, window_id: str, *, text: str = "你看看这个视频"):
    async with session_scope() as session:
        compiled = await ContextCompiler(session).compile(
            subject_id=env.subject_id, window_id=window_id, user_text=text
        )
    return compiled


def _media_text(compiled) -> str:
    lanes = [c.text for c in compiled.contributions if c.lane == "media"]
    assert lanes, "媒体车道没有进上下文"
    return "\n".join(lanes)


async def _grant_consent(env: TestEnv) -> None:
    """入站媒体要有 `media.inbound` 授权（隐私门，测试也要过）。"""
    from ling.domains.consent.service import ConsentService
    from ling.domains.media.service import INBOUND_SCOPE

    async with session_scope() as session:
        await ConsentService(session).grant(subject_id=env.subject_id, scope=INBOUND_SCOPE)


# --------------------------------------------------------------- 用例 1/2


async def test_same_minute_videos_are_distinguishable_and_map_to_their_ids(
    logged_in: TestEnv,
) -> None:
    """同一分钟内的两条视频：两行必须不同，且各自能对上自己的 evidence_id。

    **这是本任务的核心用例。** 缺陷下两行是：
        - 主人 12:21 发的视频：红底白字 A1
        - 主人 12:21 发的视频：绿底白字 A3
    除描述外逐字相同，模型只有"最近一条"这一个可用的启发式。
    """
    await _grant_consent(logged_in)
    window_id = await _window(logged_in)
    same_minute = datetime(2026, 11, 10, 12, 21, 5, tzinfo=UTC)

    ev_a = await _seed_media(
        logged_in,
        window_id,
        kind="video",
        description="红底白字一个 A1",
        created_at=same_minute,
    )
    ev_b = await _seed_media(
        logged_in,
        window_id,
        kind="video",
        description="绿底白字一个 A3",
        created_at=same_minute + timedelta(seconds=1),
    )

    text = _media_text(await _compile(logged_in, window_id))

    ref_a = short_ref_for(ev_a)
    ref_b = short_ref_for(ev_b)
    assert ref_a in text, f"已描述视频没带短号 {ref_a}：\n{text}"
    assert ref_b in text, f"已描述视频没带短号 {ref_b}：\n{text}"

    # 逐行核对：带 ref_a 的那行必须是 A1 的描述，ref_b 那行必须是 A3 的。
    lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    line_a = next(ln for ln in lines if ref_a in ln)
    line_b = next(ln for ln in lines if ref_b in ln)
    assert "A1" in line_a, line_a
    assert "A3" in line_b, line_b
    assert line_a != line_b


async def test_described_video_ref_equals_the_watch_marker_ref(
    logged_in: TestEnv,
) -> None:
    """已描述视频的短号必须 **等于** `<<看视频 mXXXXXX>>` 用的那个短号。

    两套短号必然分叉，分叉的表现是"她写的短号对不上任何视频"——
    比原缺陷更难查。所以这里直接比字符串。
    """
    await _grant_consent(logged_in)
    window_id = await _window(logged_in)

    watched = await _seed_media(logged_in, window_id, kind="video", description="蓝底白字 B2")
    unseen = await _seed_media(logged_in, window_id, kind="video", description=None)

    text = _media_text(await _compile(logged_in, window_id))
    watched_ref = short_ref_for(watched)
    unseen_ref = short_ref_for(unseen)

    # 已描述那条带短号；待看那条用同一个函数算出的短号做标记。
    assert watched_ref in text, text
    assert f"<<看视频 {unseen_ref}>>" in text, text
    # 两个短号不是同一个（不同证据），但**规则相同**：都能从 evidence_id 算回。
    assert watched_ref != unseen_ref
    assert watched_ref == short_ref_for(watched)


# ------------------------------------------------------------------ 用例 3


async def test_wording_matches_actual_kind(logged_in: TestEnv) -> None:
    """只有视频时，块里**不得**出现"图片/语音"这类与内容不符的措辞。"""
    await _grant_consent(logged_in)
    window_id = await _window(logged_in)
    await _seed_media(logged_in, window_id, kind="video", description="一段视频画面")

    text = _media_text(await _compile(logged_in, window_id))
    assert "图片/语音" not in text, f"文案与 kind 错配：\n{text}"
    assert "视频" in text


async def test_mixed_kinds_list_every_kind_present(logged_in: TestEnv) -> None:
    """混合时只列**真实存在**的种类，不列没出现的。"""
    await _grant_consent(logged_in)
    window_id = await _window(logged_in)
    await _seed_media(logged_in, window_id, kind="video", description="一段视频画面")
    await _seed_media(logged_in, window_id, kind="image", description="一张照片")

    text = _media_text(await _compile(logged_in, window_id))
    assert "视频" in text and "图片" in text
    assert "语音" not in text.split("\n")[0], f"首行列了不存在的语音：\n{text}"


# ------------------------------------------------------------------ 用例 4


async def test_non_video_media_do_not_get_a_watch_style_short_ref(
    logged_in: TestEnv,
) -> None:
    """图片/语音**不给短号**——给了她会以为也能"看"，写出对不上视频的标记。

    它们仍要能区分同分钟多条（用序号），但标识不能长得像可执行标记。
    """
    await _grant_consent(logged_in)
    window_id = await _window(logged_in)
    same_minute = datetime(2026, 11, 10, 12, 21, 5, tzinfo=UTC)
    ev_img = await _seed_media(
        logged_in,
        window_id,
        kind="image",
        description="一张照片",
        created_at=same_minute,
    )
    ev_aud = await _seed_media(
        logged_in,
        window_id,
        kind="audio",
        description="一段语音",
        created_at=same_minute + timedelta(seconds=1),
    )

    text = _media_text(await _compile(logged_in, window_id))
    # 不出现任何图片/语音的短号
    assert short_ref_for(ev_img) not in text, f"图片不该有短号：\n{text}"
    assert short_ref_for(ev_aud) not in text, f"语音不该有短号：\n{text}"
    # 但仍要可区分：两行不完全相同
    lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
    img_line = next(ln for ln in lines if "一张照片" in ln)
    aud_line = next(ln for ln in lines if "一段语音" in ln)
    assert img_line != aud_line


async def test_described_block_is_marked_as_history(logged_in: TestEnv) -> None:
    """已描述块必须明说"这是之前收发过的、不是这一轮新发的"。"""
    await _grant_consent(logged_in)
    window_id = await _window(logged_in)
    await _seed_media(logged_in, window_id, kind="video", description="旧视频画面")
    await _seed_media(logged_in, window_id, kind="video", description=None)

    text = _media_text(await _compile(logged_in, window_id))
    assert "之前" in text, f"没标明这是历史：\n{text}"
    assert "不是" in text and "这一轮" in text, f"没和新发的区分开：\n{text}"
