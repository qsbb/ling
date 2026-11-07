"""上下文窗口：**长会话不能漏、最近一条不能丢、摘要不能跟着短记忆过期**。

这几条都是实测复现过的真实缺陷（详见 ``shots/diag-context-2026-11-08.md``）：

1. `turn._execute` 曾经写死 `history_limit=20`：第 21 条往回的消息既不在原文、
   也不在编译器的 `dropped` 里，**永远压不进摘要**——31 条消息的窗口里，
   第 1~11 条在提示词里整块消失，而 `summary_through_sequence` 仍是 NULL。
2. 最近一条一超预算，装填循环直接 `break`：`usable` 为空 → 整块历史被压进摘要，
   而摘要这一轮还没进提示词，于是她一句话上文都看不到。
3. 滚动摘要和短记忆共用一行 `window_contexts`，`expires_at` 到期时
   `read_window_context` 把整行当不存在：闲置两小时后摘要一起消失，
   下一轮还会用空的 `prev_summary` 把旧摘要覆盖掉。
4. 群名片与库里的人级昵称不一致时，同一个人在同一个提示里顶着两个名字。
5. 图片描述块没有"谁发的/什么时候"，没法对应到历史里那条 `[image]`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.contracts.v1 import Origin, TurnState
from ling.domains.conversation.service import ConversationService
from ling.domains.media.service import MediaEvidenceService
from ling.infra.db import session_scope
from ling.infra.models import Message, Subject
from ling.kernel.context import (
    HISTORY_TOKEN_BUDGET,
    ContextCompiler,
    estimate_tokens,
)
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _window(env: TestEnv, *, visibility: str = "subject_private") -> str:
    r = await env.client.post("/v1/sessions", json={"title": "ctx-window"})
    assert r.status_code == 201, r.text
    window_id = r.json()["session_id"]
    if visibility != "subject_private":
        async with session_scope() as session:
            conv = ConversationService(session)
            window = await conv.get_window(window_id, subject_id=env.subject_id)
            window.visibility = visibility
            session.add(window)
    return window_id


async def _seed(env: TestEnv, window_id: str, *, sender: str | None, texts: list[str]) -> None:
    async with session_scope() as session:
        conv = ConversationService(session)
        for text in texts:
            await conv.add_message(
                window_id, role="user", text=text, sender_subject_id=sender
            )


async def _compile(
    env: TestEnv,
    window_id: str,
    *,
    text: str = "然后呢",
    subject_id: str | None = None,
    is_group: bool = False,
    sender_name: str | None = None,
    current_turn_id: str | None = None,
    **kwargs,
) -> object:
    async with session_scope() as session:
        return await ContextCompiler(session).compile(
            subject_id=subject_id or env.subject_id,
            window_id=window_id,
            user_text=text,
            is_group=is_group,
            current_turn_id=current_turn_id,
            user_sender_name=sender_name,
            **kwargs,
        )


def _blob(compiled) -> str:  # noqa: ANN001
    return "\n".join(m.content for m in compiled.messages)


def _history_texts(compiled) -> list[str]:  # noqa: ANN001
    return [m.content for m in compiled.messages if m.role != "system"]


# ------------------------------------------------------- ① 长会话不许有缺口


async def test_history_beyond_one_page_is_complete(logged_in: TestEnv) -> None:
    """一次查询的上限**不是**上下文的边界：31 条要一条不少地进来。

    这就是生产形状的回归（页大小 20 也正是 `turn.py` 曾经写死的值）。
    """
    window_id = await _window(logged_in)
    texts = [f"第{i}句" for i in range(1, 32)]
    await _seed(logged_in, window_id, sender=None, texts=texts)

    compiled = await _compile(logged_in, window_id, text="第32句", history_limit=20)
    joined = _blob(compiled)
    missing = [t for t in texts if t not in joined]
    assert not missing, f"长会话里漏掉了 {len(missing)} 条：{missing[:5]}"
    # 顺序必须和说话顺序一致（最后一条是当前这句，不属于历史）
    history = _history_texts(compiled)
    assert history[:-1] == texts


async def test_paging_walks_back_past_the_page_size(logged_in: TestEnv) -> None:
    """页大小 5 也要把 12 条都读出来（按页往回走，而不是只取最后一页）。"""
    window_id = await _window(logged_in)
    texts = [f"消息{i}" for i in range(1, 13)]
    await _seed(logged_in, window_id, sender=None, texts=texts)

    compiled = await _compile(logged_in, window_id, text="收尾", history_limit=5)
    joined = _blob(compiled)
    for text in texts:
        assert text in joined


# ------------------------------------------------ ② 最近一条超长不能吃掉历史


async def test_oversized_message_does_not_wipe_older_context(logged_in: TestEnv) -> None:
    """一条超长消息只能占**自己那一份**预算，不许把整块历史挤掉。"""
    window_id = await _window(logged_in)
    await _seed(
        logged_in,
        window_id,
        sender=None,
        texts=["第一句：我们约好周六去看展", "第二句：几点集合"],
    )
    huge = "粘贴的长文：" + "很长的内容。" * (HISTORY_TOKEN_BUDGET * 3)
    await _seed(logged_in, window_id, sender=None, texts=[huge, "第三句：我说完了"])

    compiled = await _compile(logged_in, window_id, text="嗯")
    joined = _blob(compiled)
    assert "约好周六" in joined, "最近一条超长把更早的历史整块挤掉了"
    assert "几点集合" in joined
    assert "我说完了" in joined
    # 超长那条被截断保留（留痕），而不是整条消失
    assert "…（这条太长，后文被截掉了）" in joined
    # 历史总量仍在预算内
    history_tokens = sum(estimate_tokens(t) for t in _history_texts(compiled))
    assert history_tokens <= HISTORY_TOKEN_BUDGET + 50, history_tokens


async def test_budget_overflow_rolls_up_without_losing_the_joint(logged_in: TestEnv) -> None:
    """预算溢出时：溢出的进摘要，摘要覆盖位置与保留窗口**严丝合缝**。

    不能出现"既不在原文也不在摘要"的缝，也不能两头重复。
    """
    window_id = await _window(logged_in)
    body = "比较长的内容，用来把历史车道撑满。" * 40
    per = estimate_tokens(body)
    # 按当前预算算条数：写死条数在预算放大后会静默变成"没撞到上限"的空跑
    texts = [f"第{i}条{body}" for i in range((HISTORY_TOKEN_BUDGET // per) + 4)]
    await _seed(logged_in, window_id, sender=None, texts=texts)

    compiled = await _compile(logged_in, window_id, text="刚才说到哪了")
    assert compiled.meta.get("rolled_up", 0) > 0, "应有消息被滚动进摘要"

    async with session_scope() as session:
        ctx = await ConversationService(session).read_window_context(
            window_id, expired_ok=True
        )
    assert ctx is not None and ctx.summary
    assert int(ctx.summary_through_sequence) > 0
    # 摘要里每条只保留前 120 字符（`render_rollup`），所以用"第N条"这个前缀核对
    assert "第0条" in ctx.summary, "最早那条没有进摘要"

    again = await _compile(logged_in, window_id, text="继续")
    assert "较早的对话摘要" in _blob(again)
    # 只看**原文层**：摘要块本身也在这条 blob 里，拿它做"有没有重复"的判据
    # 永远为真（自己跟自己重复）
    history = "\n".join(_history_texts(again))

    # **没有缝**：每条消息要么在原文里、要么在摘要里，不能两头都没有；
    # 也不能两头都有（那就是重复注入）
    for i in range(len(texts)):
        needle = f"第{i}条"
        assert (needle in history) or (needle in ctx.summary), f"两头都没有：{needle}"
        assert not (needle in history and needle in ctx.summary), f"重复注入：{needle}"


# ------------------------------------------- ③ 摘要不跟着短记忆的 TTL 过期


async def test_summary_survives_short_memory_expiry(logged_in: TestEnv) -> None:
    """闲置超过短记忆 TTL 之后，摘要**仍然**要进上下文。

    摘要和"本窗口正在聊"共用一行 `window_contexts`，但只有后者有时效。
    按整行 TTL 判过期，会让隔夜回来的对话完全接不上。
    """
    window_id = await _window(logged_in)
    await _seed(logged_in, window_id, sender=None, texts=["我们约好周六去看展"])
    async with session_scope() as session:
        conv = ConversationService(session)
        await conv.upsert_window_context(window_id, topic="周六看展", ttl_minutes=120)
        await conv.save_summary(
            window_id,
            summary="对方说过「我们约好周六去看展」",
            through_sequence=1,
            message_count=1,
        )
        row = await conv.read_window_context(window_id, expired_ok=True)
        row.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        session.add(row)

    compiled = await _compile(logged_in, window_id, text="周六几点")
    joined = _blob(compiled)
    assert "较早的对话摘要" in joined, "摘要跟着短记忆一起过期了"
    assert "我们约好周六去看展" in joined
    # 过期的短记忆**不该**再注入
    assert "本窗口正在聊" not in joined


# ------------------------------------------------- ④ 群名片与人级昵称一致


async def test_group_card_and_history_name_point_to_the_same_person(
    logged_in: TestEnv,
) -> None:
    """群名片改了，当前这句用新名片，但**必须带上历史里那个名字**。

    历史署名读 `subjects.display_name`（人级昵称），当前这句读本次名片。
    两者不一致时同一个提示里会出现两个名字，模型会把"小阿岚"当成另一个人。
    """
    window_id = await _window(logged_in, visibility="group_scope")
    async with session_scope() as session:
        subject = await session.get(Subject, logged_in.subject_id)
        subject.display_name = "阿岚"
        session.add(subject)

    compiled = await _compile(
        logged_in,
        window_id,
        text="那我先问个事",
        is_group=True,
        sender_name="小阿岚",
    )
    assert compiled.messages[-1].content == "小阿岚（阿岚）：那我先问个事"

    # 没传名片时退回库里的名字（不做无意义的括号）
    plain = await _compile(logged_in, window_id, text="再说一句", is_group=True)
    assert plain.messages[-1].content == "阿岚：再说一句"


async def test_private_chat_has_no_name_prefix(logged_in: TestEnv) -> None:
    """私聊只有一个"你"，加名字前缀只是噪音（也不该因为透传而漏进来）。"""
    window_id = await _window(logged_in)
    compiled = await _compile(
        logged_in, window_id, text="今天有点累", is_group=False, sender_name="阿岚"
    )
    assert compiled.messages[-1].content == "今天有点累"


# ------------------------------------------------------- ⑤ 图片描述要能对上人


async def test_media_description_names_the_sender(logged_in: TestEnv) -> None:
    """图片描述必须带**谁发的、什么时候**，否则对不上历史里那条 [image]。"""
    window_id = await _window(logged_in, visibility="group_scope")
    sender_id = "subj_media_sender"
    async with session_scope() as session:
        from ling.domains.consent.service import ConsentService
        from ling.domains.media.service import INBOUND_SCOPE

        # 群窗口的"描述入站媒体"授权挂在会话主人（= 登录者）身上
        await ConsentService(session).grant(
            subject_id=logged_in.subject_id, scope=INBOUND_SCOPE
        )
        session.add(
            Subject(id=sender_id, display_name="阿岚", trust_level="known")
        )
        conv = ConversationService(session)
        await conv.add_message(
            window_id,
            role="user",
            text="看我拍的 [image]",
            sender_subject_id=sender_id,
        )
        media = MediaEvidenceService(session, actor_id="actor_ling")
        row = await media.record(
            subject_id=sender_id,
            window_id=window_id,
            media_ref="https://example.invalid/shot.jpg",
            kind="image",
            source_event_id="evt-media-1",
            consent_subjects=[logged_in.subject_id],
        )
        await media.describe(
            row.evidence_id,
            window_id=window_id,
            description="一张傍晚的街景",
        )

    compiled = await _compile(
        logged_in, window_id, text="好看吗", is_group=True, sender_name="阿岚"
    )
    media_lanes = [c.text for c in compiled.contributions if c.lane == "media"]
    assert media_lanes, "媒体证据没有进上下文"
    assert "阿岚" in media_lanes[0], media_lanes[0]
    assert "一张傍晚的街景" in media_lanes[0]
    # 图片那一行也还在历史里（占位符不是"用户说的话"，但保留为提示）
    assert "看我拍的 [image]" in _blob(compiled)


# ----------------------------------------------------- ⑥ 未完成消息不假冒历史


async def test_unfinished_messages_stay_out_of_history(logged_in: TestEnv) -> None:
    """`state != completed` 的消息不进历史——半截回复不能被当成"她说过的"。"""
    window_id = await _window(logged_in)
    async with session_scope() as session:
        session.add(
            Message(
                id="msg_partial_1",
                window_id=window_id,
                role="actor",
                text="半截的回复",
                state=TurnState.stopped.value,
                sequence=1,
                origin=Origin.model_inference.value,
            )
        )
    compiled = await _compile(logged_in, window_id, text="你刚才说啥")
    assert "半截的回复" not in _blob(compiled)
