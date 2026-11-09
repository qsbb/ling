"""记忆真删：墓碑、代次栅栏与后台物理清理。

## 为什么需要这个模块

用户原话三句，合起来是一个明确要求：

1. 「用户说忘了……这种东西不会也不应该存在」
2. 「只能后台管理页删，对话里不能删」
3. 「真删：原文和派生数据都从库里清掉」

原来的 `MemoryService.forget_subject()` 只做**软清除**（打 `retracted` /
`archived` 标记），原文仍在库里；而且它完全不碰 outbox——真库里就有 24 条
pending 的 `memory.extract`，payload 里带 `user_text` / `actor_text` 原文。
清完之后那些事件一执行，就把内容重新写回记忆。这不是"可能"，是必然。

## 两段式：请求阶段与清理阶段

**请求阶段**（同步，秒级，在 HTTP 请求里跑完）：

- 递增 `subjects.forget_generation`（代次栅栏的开关）；
- 写一行墓碑（`state=pending`，不含任何正文）；
- 取消该主体所有 pending 的 outbox 事件，并**擦掉 payload 里的原文**；
- 提交。

这一步完成后，用户刷新页面就已经看不到记忆了（读侧按代次/墓碑过滤），
而**在途任务也已经注定作废**（它们入队时记的旧代次对不上）。

**清理阶段**（后台，可能几十万行）：

- 物理删除 memories / episodes / core / awareness / embeddings / entities；
- 物理删除 retrieval logs（它存着用户原话，是最容易漏的一处）；
- 墓碑置 `done` 并记计数。

为什么必须后台：一是数据量可能很大，不能卡在 HTTP 请求里；二是删除必须
**幂等可重试**（删到一半崩了要能续）；三是删除过程本身要能审计。

## 为什么"取消 outbox"不够，必须有代次

`cancelled` 只能拦住还躺在 `pending` 里的事件。已经**认领**（`in_flight`）
或**正在 handler 里跑**的任务抓不住——它已经不在 pending 里了。代次是唯一
能覆盖"边删边写"的机制：

- 生产者入队时把当前代次写进 `outbox_events.forget_generation`；
- 消费者写库前读一次 `subjects.forget_generation` 比对；
- 不一致 → **静默 no-op**（不是报错：那是预期行为，不该进错误日志）。

## 墓碑为什么不含正文

墓碑会被长期保留（用于备份恢复后重放）。它一旦存了被删的文本，就变成了
一个新的泄漏点——用户要求的是"不存在"，不是"换个地方存"。所以只存数字
计数与状态码。

## 不做物理删除 `messages`

对话消息是**用户与角色的交流记录**，不属于"记忆"。用户说"忘了"指的是
"你记住的关于我的事"，而不是"把我们的聊天记录删了"。两者混为一谈会让
"忘了我"变成"清空聊天"——那是另一个动作、另一个授权级别。这里明确不做，
并在文档里写明，避免以后有人顺手加进去。

## 逐表核对：删什么、留什么

判据只有一条：**这张表里有没有该用户的原文，或能从中推断出原文的内容。**
每个判断都写在 `purge_subject()` 里对应语句的上方；这里是索引。

### 物理删除

| 表 | 为什么删 |
| --- | --- |
| `memories` | 提炼后的断言正文，就是"你记住的关于我的事"本身 |
| `memory_episodes` | 情节摘要，含用户原话的转述 |
| `core_memories` | 常驻核心记忆（称呼/边界/禁忌），含正文 |
| `awareness_items` | 跨会话事实投影，含第三人称转述 |
| `memory_embeddings` | 记忆的向量，**可从向量反推语义**，是派生数据 |
| `memory_entities` | 记忆↔实体关联，暴露"这条记忆关于谁" |
| `memory_retrieval_logs` | `query` 列存**用户原话**，最容易漏的一处 |
| `entities` | `name`/`aliases` 是从原话里抠出来的对象名（"我妈""家里那条狗"） |
| `media_evidence` | `description` 是"用户发来那张图/那段视频里有什么"；只删 `subject_id` 是该用户的（她自产的素材 `subject_id IS NULL` 不动） |
| `knowledge_items` | `source_kind=user_statement` 的 `claim` 就是用户自述；只删 `owner_subject_id == 主体`，`owner_subject_id IS NULL` 的公共知识不删 |
| `prompt_snapshots` | **特别注意**：它存的是整份实际发给模型的提示词，里面必然含记忆正文、检索片段、媒体描述与历史原文。这张表没有 subject 列，按 `window_id ∈ 该主体的窗口` 删 |

### 就地脱敏（保留结构，清掉正文）

| 表 | 为什么这样处理 |
| --- | --- |
| `relationship_events.note` | `note = user_text[:200]`，**就是原文**。但这张表是关系投影（`relationship_states.familiarity/trust`）的可重算来源：删行会让投影再也算不回来，所以只把 `note` 置 NULL，保留 `kind/weight/origin/occurred_at` 这些纯结构化事实 |
| `outbox_events.payload` | 换成 `{"purged": True}`，行留作"为什么这件事没发生"的解释 |

### 保留

| 表 | 为什么留 |
| --- | --- |
| `messages` | 见上一节：对话记录不是"记忆"，删它等于把"忘了我"变成"清空聊天" |
| `window_contexts`（topic/summary/open_questions） | 是对**仍保留的 messages** 的摘要，同生命周期；messages 不删，摘要就没有单独删的理由 |
| `handoff_capsules`（recent_topic/short_quote） | 同上：交接的是"刚才聊到哪"，来源是 messages，30 分钟自然过期 |
| `speech_segments.text` | 她自己的语音分段（TTS 产物），与 messages 同类 |
| `self_experiences` | 形状是 `(actor_id, 来源活动)`，**没有 subject 归属**：那是"她自己做过什么"，不是"关于你的记忆"；内容是活动证据摘要（模拟生活/工具回执），不是从对话里提炼的。删掉会把所有用户的"她的人生"一起抹掉。⚠️ 若将来真的写入 `origin=user_stated` 的活动证据（含用户原话），必须**同时**给它加 subject 归属列，否则这里就漏了 |
| `commitments` / `intents` / `schedule_*` | 是"要做的事"（约定、意图、日程），不是"关于你的记忆"。删掉会让提醒与日程静默消失——用户没要求停掉约定 |
| `proactive_opportunities.draft_text` | 她**要说的话**（出站草稿、短过期），来源是活动/梦/承诺，不是记忆检索 |
| `dream_reports` | 梦的叙事由她自己的经历/情绪/身体状态生成；承诺原文明确**不进**提示词（见 `dream/service.py`） |
| `affect_states.cause` | 固定短语（"用户表达了善意"），不含原话 |
| `media_read_jobs` | 只有 call_id/状态/错误码/租约，没有正文；证据被删后读到 `evidence_unavailable` 直接终结，不会重建描述 |
| `operations` / `delivery_receipts` / `actor_events` / `turns` | 投递与轮次元数据（字符数、模型名、状态码），不含正文 |
| `memory_purge_tombstones` | 墓碑：只存计数与状态码，长期保留用于防复活 |
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from ling.infra.models import (
    AwarenessItemRow,
    CoreMemoryRow,
    EntityRow,
    KnowledgeItem,
    MediaEvidenceRow,
    MemoryEmbeddingRow,
    MemoryEntityRow,
    MemoryEpisode,
    MemoryPurgeTombstone,
    MemoryRetrievalLog,
    MemoryRow,
    OutboxEvent,
    PromptSnapshot,
    RelationshipEvent,
    Subject,
    Window,
    utcnow,
)

logger = logging.getLogger("ling.domains.memory.purge")

#: 会被**取消**的 outbox topic。
#:
#: 只列"会写入用户记忆"的那几个：把别人的 topic（日程、梦、投递）也一并
#: 取消等于"忘了我"顺手停掉了她的生活——那不是用户要的。
PURGEABLE_TOPICS: frozenset[str] = frozenset(
    {"memory.extract", "memory.archive", "relationship.update"}
)

#: 墓碑状态机。`pending → running → done | failed`，终态不可再转移。
PURGE_STATES: tuple[str, ...] = ("pending", "running", "done", "failed")


@dataclass(frozen=True)
class PurgeRequest:
    """请求阶段的产出：给调用方看的那几项。"""

    tombstone_id: str
    subject_id: str
    generation: int
    events_cancelled: int
    requested_at: datetime


@dataclass
class PurgeResult:
    """清理阶段的计数。全部是数字，不含任何被删内容。"""

    tombstone_id: str
    state: str = "done"
    memories_deleted: int = 0
    episodes_deleted: int = 0
    core_deleted: int = 0
    awareness_deleted: int = 0
    embeddings_deleted: int = 0
    entities_deleted: int = 0
    retrieval_logs_deleted: int = 0
    events_scrubbed: int = 0
    error_code: str | None = None
    details: dict[str, int] = field(default_factory=dict)


class MemoryPurgeError(Exception):
    """清理失败。带**原因码**（不含正文），供墓碑记录与前端展示。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def current_generation(session: AsyncSession, subject_id: str) -> int:
    """该主体当前的遗忘代次。主体不存在时返回 0（与"从未遗忘"同义）。"""
    value = await session.scalar(
        select(Subject.forget_generation).where(Subject.id == subject_id)
    )
    return int(value or 0)


async def request_purge(
    session: AsyncSession,
    *,
    subject_id: str,
    requested_by: str | None = None,
    now: datetime | None = None,
) -> PurgeRequest:
    """请求阶段：递增代次、写墓碑、取消并脱敏在途事件。**调用方负责提交**。

    为什么递增代次放在最前面：它一旦提交，所有旧代次的写入就注定作废。
    放在最后（等清理完再递增）会让"请求到清理之间"的写入仍然生效——
    那正是用户最不能接受的窗口。
    """
    from ling.infra.security import new_id

    moment = now or utcnow()

    # 1) 代次 +1：从这一刻起，旧代次的写入全部作废。
    #
    #    ## 为什么用原子 UPDATE ... RETURNING 而不是"读出来再赋值"
    #
    #    原来的写法是 `session.get(Subject, ...)` 之后 `subject.forget_generation
    #    += 1`。两个并发的删除请求（用户连点两次、或两个标签页）会**都读到
    #    generation=0**，各自算出 1 写回，结果是 1 而不是 2：一次请求的代次
    #    被吃掉了。后果不是"少加一个数字"，而是**第一次请求之后、第二次请求
    #    之前入队的那些写入不会被作废**——它们带着代次 1，而库里也是 1，
    #    栅栏放行，内容被写回来。
    #
    #    `UPDATE ... SET generation = generation + 1 RETURNING generation` 把
    #    "读"和"写"合成一条语句，由数据库保证原子性：并发时 PostgreSQL 会
    #    让后到的那条等前一条提交，然后基于**新值**再 +1（行锁语义），结果
    #    必然是 2。不额外用 `SELECT ... FOR UPDATE` 是因为这条语句已经拿到
    #    行锁，再锁一次只是多一次往返。
    #
    #    `RETURNING` 在 sqlite（3.35+）与 PostgreSQL 上都能用，所以这条语句
    #    不需要按方言分叉。
    generation = await session.scalar(
        update(Subject)
        .where(Subject.id == subject_id)
        .values(forget_generation=Subject.forget_generation + 1)
        .returning(Subject.forget_generation)
    )
    if generation is None:
        # 主体不存在 = 没有记忆可删。不建墓碑（否则会积一堆空墓碑），
        # 也不报错——调用方按"无事可做"处理即可。
        #
        # 判据用 UPDATE 的返回行而不是先 `session.get()`：先查再更新是两次
        # 往返，且中间主体被删掉时会写出一行没有主体的墓碑。
        raise MemoryPurgeError("subject_not_found", "这个账号没有记忆记录")
    generation = int(generation)

    # 2) 取消并**脱敏**该主体在途的事件。
    #
    #    为什么不是直接删：留一行 `cancelled` 可以解释"为什么这件事没发生"
    #    （排查时能看出是遗忘导致的，不是故障）。但 payload 必须清空——
    #    它带着用户原话，留着等于没删。
    #
    #    ## 为什么不能只按 `subject_id` 列过滤
    #
    #    `outbox_events.subject_id` 是这次新加的列，**所有历史行都是 NULL**
    #    （开发库实测：`memory.extract` 407 条 done + 24 条 pending，无一有
    #    该列），而它们 payload 里带着 `user_text` / `actor_text` 原文。
    #    只按列过滤会一条都选不中：删除后原文仍在库里，而那 24 条 pending
    #    一旦被消费，就会把内容重新写成记忆——直接违背用户要求。
    #
    #    所以两条路取并集：新事件走列（有索引、快），旧事件退回按 payload
    #    里的 `subject_id` 字段匹配。后者没有索引，但只在删除这个低频动作里
    #    跑一次，代价可以接受。
    events = (
        await session.execute(
            select(OutboxEvent).where(
                _belongs_to_subject(subject_id),
                OutboxEvent.state.in_(("pending", "in_flight")),
                OutboxEvent.topic.in_(tuple(PURGEABLE_TOPICS)),
            )
        )
    ).scalars()
    cancelled = 0
    for event in events:
        event.state = "cancelled"
        event.payload = {"purged": True}
        event.last_error = "memory_purged"
        cancelled += 1

    # 3) 墓碑。
    tombstone = MemoryPurgeTombstone(
        id=new_id("purge"),
        subject_id=subject_id,
        requested_by=requested_by,
        generation=generation,
        state="pending",
        requested_at=moment,
        events_cancelled=cancelled,
    )
    session.add(tombstone)
    await session.flush()

    logger.info(
        "记忆清理已请求 subject=%s generation=%s 取消事件=%d tombstone=%s",
        subject_id,
        generation,
        cancelled,
        tombstone.id,
    )
    return PurgeRequest(
        tombstone_id=tombstone.id,
        subject_id=subject_id,
        generation=generation,
        events_cancelled=cancelled,
        requested_at=moment,
    )


async def purge_subject(
    session: AsyncSession,
    *,
    tombstone_id: str,
    now: datetime | None = None,
) -> PurgeResult:
    """清理阶段：**物理删除**该主体的记忆与全部派生数据。**调用方负责提交**。

    幂等：重复跑同一个墓碑只会删到 0 行，计数归零但状态仍是 `done`。
    这不是"重复执行有副作用"，而是"删干净了就是删干净了"。
    """
    moment = now or utcnow()
    tombstone = await session.get(MemoryPurgeTombstone, tombstone_id)
    if tombstone is None:
        raise MemoryPurgeError("tombstone_not_found", "清理记录不存在")
    if tombstone.state == "done":
        # 已完成：不重复删（幂等），直接返回既有计数。
        return _result_from_tombstone(tombstone)

    tombstone.state = "running"
    await session.flush()

    subject_id = tombstone.subject_id
    result = PurgeResult(tombstone_id=tombstone.id)

    try:
        # 这个主体名下的所有窗口。三张表要它：`memory_retrieval_logs`、
        # `prompt_snapshots`、以及（下面注释里说明的）为什么 `messages` 不删。
        #
        # **`messages` 明确不删**：对话记录是用户与角色的交流记录，不属于
        # "记忆"。删它等于把"忘了我"变成"清空聊天"——另一个动作、另一个授权
        # 级别。同理 `window_contexts` / `handoff_capsules`（对保留中 messages
        # 的摘要）与 `speech_segments`（她自己的语音）也留。
        window_ids = list(
            (
                await session.execute(
                    select(Window.id).where(Window.subject_id == subject_id)
                )
            ).scalars()
        )

        # 1) 记忆行与它们的向量/实体关联。
        #
        #    - `memories`：提炼后的断言正文，就是"你记住的关于我的事"本身；
        #    - `memory_embeddings`：记忆的向量，**能从向量反推语义**，是派生
        #      数据。只删 memories 而留下向量，等于留下一个可反解的语义指纹；
        #    - `memory_entities`：关联行暴露"这条记忆关于谁"。
        #
        #    向量与实体关联都靠 memory_id 关联，所以**必须先取 id 再删**，
        #    不能靠 subject 过滤（那两张表没有 subject 列）。
        memory_ids = list(
            (
                await session.execute(
                    select(MemoryRow.id).where(MemoryRow.subject_id == subject_id)
                )
            ).scalars()
        )
        if memory_ids:
            embeddings = await session.execute(
                delete(MemoryEmbeddingRow).where(
                    MemoryEmbeddingRow.memory_id.in_(memory_ids)
                )
            )
            result.embeddings_deleted = int(getattr(embeddings, "rowcount", 0) or 0)
            links = await session.execute(
                delete(MemoryEntityRow).where(MemoryEntityRow.memory_id.in_(memory_ids))
            )
            result.entities_deleted = int(getattr(links, "rowcount", 0) or 0)

        memories = await session.execute(
            delete(MemoryRow).where(MemoryRow.subject_id == subject_id)
        )
        result.memories_deleted = int(getattr(memories, "rowcount", 0) or 0)

        # 2) 情节：`memory_episodes.summary` 是记忆的叙事化摘要，含用户原话
        #    的转述（"他说小时候住在外婆家"），属于派生数据。
        episodes = await session.execute(
            delete(MemoryEpisode).where(MemoryEpisode.subject_id == subject_id)
        )
        result.episodes_deleted = int(getattr(episodes, "rowcount", 0) or 0)

        # 3) 核心记忆（`core_memories`：称呼/边界/禁忌正文）与意识条目
        #    （`awareness_items`：跨会话事实投影，第三人称转述）。两者都是
        #    从记忆行再提炼一层的投影，属于"存在"的一部分。
        core = await session.execute(
            delete(CoreMemoryRow).where(CoreMemoryRow.subject_id == subject_id)
        )
        result.core_deleted = int(getattr(core, "rowcount", 0) or 0)

        awareness = await session.execute(
            delete(AwarenessItemRow).where(AwarenessItemRow.subject_id == subject_id)
        )
        result.awareness_deleted = int(getattr(awareness, "rowcount", 0) or 0)

        # 4) 检索日志。**这一处最容易漏**：它存着用户原话（`query` 列）与
        #    命中过的记忆 id。只删 memories 而留着它，等于原文还在库里。
        if window_ids:
            logs = await session.execute(
                delete(MemoryRetrievalLog).where(
                    MemoryRetrievalLog.window_id.in_(window_ids)
                )
            )
            result.retrieval_logs_deleted = int(getattr(logs, "rowcount", 0) or 0)

        # 4.5) 实体本身。上一步删的只是"记忆↔实体"的关联行；`entities.name`
        #      存的是「我妈」「家里那条狗」这类从用户原话里抠出来的对象名。
        #      只删关联、留下实体表，等于把"她认识这些人"这件事留在库里——
        #      那同样是派生数据，同样属于"不存在"。
        entities = await session.execute(
            delete(EntityRow).where(EntityRow.subject_id == subject_id)
        )
        result.details["entity_rows_deleted"] = int(getattr(entities, "rowcount", 0) or 0)

        # 4.6) 媒体证据：`description` 是"用户发来那张图/那段视频里有什么"。
        #
        #      为什么属于"原文"：它是对用户私人物料的**文字化**，用户要求
        #      "原文和派生数据都清掉"，而这是最直接的一种派生——图片文件还在
        #      磁盘上（那是另一个删除动作），但"她描述过这张图什么内容"必须
        #      从库里消失，否则模型随时能读回来复述。
        #
        #      为什么按 `subject_id` 而不是按 `window_id`：她自产素材
        #      （`origin=model_generated`）的 `subject_id` 是 NULL，按窗口删
        #      会把那些一起删掉——那是她的东西，不是用户的。
        media = await session.execute(
            delete(MediaEvidenceRow).where(MediaEvidenceRow.subject_id == subject_id)
        )
        result.details["media_evidence_deleted"] = int(getattr(media, "rowcount", 0) or 0)

        # 4.7) 私有知识条目：`source_kind=user_statement` 的 `claim` 就是
        #      用户自述（"我住在杭州"），`owner_subject_id` 指向本人。
        #
        #      **只删 `owner_subject_id == 主体`**：`owner_subject_id IS NULL`
        #      是公共知识（后台录入、来源可核验、任何人可读），删它等于因为
        #      一个人退出而清空全站知识库。
        knowledge = await session.execute(
            delete(KnowledgeItem).where(KnowledgeItem.owner_subject_id == subject_id)
        )
        result.details["knowledge_items_deleted"] = int(
            getattr(knowledge, "rowcount", 0) or 0
        )

        # 4.8) 提示词快照。**这一处是最容易漏、也最严重的**：
        #      `prompt_snapshots.payload` 存的是**整份实际发给模型的 messages**，
        #      里面必然包含记忆正文、检索片段、媒体描述、历史原文——它就是
        #      用户原话的完整副本。只删 memories 而留着它，等于原文还在库里，
        #      而且是最完整的那一份。
        #
        #      为什么按 `window_id` 删：这张表没有 subject 列（快照是旁路观测，
        #      写的时候只知道窗口），而 `windows.subject_id` 是权威归属。
        #      用 `message_id` 反查不行：快照可能还没回填 message_id。
        #
        #      没有窗口时也记一个 0：审计里"这张表查过了、是 0 条"与"没查过"
        #      必须能区分，否则以后又会有人说"提示词快照好像没清"。
        result.details["prompt_snapshots_deleted"] = 0
        if window_ids:
            snapshots = await session.execute(
                delete(PromptSnapshot).where(PromptSnapshot.window_id.in_(window_ids))
            )
            result.details["prompt_snapshots_deleted"] = int(
                getattr(snapshots, "rowcount", 0) or 0
            )

        # 4.9) 关系事件的 `note`：`note = user_text[:200]`，**就是用户原话**。
        #
        #      为什么只脱敏不删行：`relationship_events` 是 `relationship_states`
        #      （熟悉度/信任度）的**可重算来源**——删行之后投影再也算不回来，
        #      而且"这个人曾经对我表达过善意"这类事实本身不含内容。所以把
        #      `note` 置 NULL，保留 kind/weight/origin/occurred_at。
        notes = await session.execute(
            update(RelationshipEvent)
            .where(
                RelationshipEvent.subject_id == subject_id,
                RelationshipEvent.note.is_not(None),
            )
            .values(note=None)
        )
        result.details["relationship_notes_scrubbed"] = int(
            getattr(notes, "rowcount", 0) or 0
        )

        # 5) 兜底：把该主体所有事件的 payload 脱敏（包括已经 cancelled 的）。
        #
        #    为什么连 cancelled 也要清：上一轮请求可能只把 payload 换成
        #    `{"purged": True}`，但更早的历史事件（done 状态）payload 里
        #    仍有原文。它们不再被消费，但**原文还在库里**。
        #
        #    这里同样要 `_belongs_to_subject`：开发库有 407 条历史 done 事件
        #    的 `subject_id` 是 NULL，只按列更新一条都改不到，而它们 payload
        #    里全是用户原话。
        scrubbed = await session.execute(
            update(OutboxEvent)
            .where(
                _belongs_to_subject(subject_id),
                OutboxEvent.state.in_(("done", "dead", "cancelled")),
            )
            .values(payload={"purged": True})
        )
        result.events_scrubbed = int(getattr(scrubbed, "rowcount", 0) or 0)

        tombstone.state = "done"
        tombstone.completed_at = moment
        tombstone.memories_deleted = result.memories_deleted
        tombstone.episodes_deleted = result.episodes_deleted
        tombstone.core_deleted = result.core_deleted
        tombstone.awareness_deleted = result.awareness_deleted
        tombstone.embeddings_deleted = result.embeddings_deleted
        tombstone.entities_deleted = result.entities_deleted
        # 这两列原先没写回：墓碑上永远是 0，前端看到"检索日志删了 0 条"，
        # 而那句用户原话其实已经被删了——一个只会让人误判的计数。
        tombstone.retrieval_logs_deleted = result.retrieval_logs_deleted
        tombstone.events_scrubbed = result.events_scrubbed
        tombstone.error_code = None
        await session.flush()

        logger.info(
            "记忆清理完成 subject=%s generation=%s 记忆=%d 情节=%d 核心=%d "
            "意识=%d 向量=%d 实体=%d 检索日志=%d 事件脱敏=%d",
            subject_id,
            tombstone.generation,
            result.memories_deleted,
            result.episodes_deleted,
            result.core_deleted,
            result.awareness_deleted,
            result.embeddings_deleted,
            result.entities_deleted,
            result.retrieval_logs_deleted,
            result.events_scrubbed,
        )
        return result
    except MemoryPurgeError:
        raise
    except Exception as exc:  # noqa: BLE001 - 清理失败要留痕，但不能带正文
        # **失败状态必须落到库里**，否则"失败可查、可重试"就是空话。
        #
        # 为什么不能在当前 session 里写再抛：调用方是 `session_scope`，
        # 它在异常时 `rollback()`（`infra/db.py:100-110`），这次写入会被
        # 一起丢掉。墓碑停在 `running`，`memory.purge.sweep` 扫不到 `failed`，
        # 用户看到的是"一直清理中"——比直接报错更糟，因为它看起来像还在干活。
        #
        # ## 为什么必须先 `rollback()` 再开独立事务
        #
        # 上面那句 `tombstone.state = "running"` 已经**锁住了墓碑这一行**
        # （UPDATE 拿行锁，直到本事务结束）。另一个事务去更新同一行会**一直等**
        # ——实测确认过：PostgreSQL 上后到的那条 UPDATE 会阻塞到前一个事务结束。
        # 所以"在 except 里直接开新 session 写 failed"会永久挂住，比原来更糟。
        #
        # 先回滚本事务：一是释放行锁，二是丢掉半截删除（本来也没提交，回滚是
        # 正确语义——失败就是失败，不留部分结果）。回滚之后调用方的
        # `session_scope` 再 rollback 一次是安全的空操作。
        #
        # 代价：`purge_subject` 会回滚**调用方**的事务。调用方只能把它当作
        # 一个独立的工作单元（现在的两个调用点都是这样），不能和别的写入
        # 混在同一事务里。
        await session.rollback()
        await _record_purge_failure(
            session, tombstone_id=tombstone_id, error_code=type(exc).__name__[:48]
        )
        logger.warning("记忆清理失败 subject=%s：%s", subject_id, type(exc).__name__)
        raise MemoryPurgeError("purge_failed", "清理失败，可以重试") from exc


def _independent_session_factory(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """给"另起一个事务"用的会话工厂。

    优先跟随**当前 session 绑定的引擎**：那样不管调用方用的是全局引擎还是
    测试里自建的 sqlite 引擎，另起的会话都落在同一个库上。只有 `bind` 拿不到
    时才退回全局工厂（`ling.infra.db.get_session_factory`）。

    session 被绑到**连接**上时（`sessionmaker(bind=conn)`）不能直接复用那个
    连接——那样"独立事务"其实是同一条连接、同一个事务，等于什么都没做。
    这种情况下取连接所属的引擎另开一条。
    """
    bind = session.bind
    if bind is None:
        from ling.infra.db import get_session_factory

        return get_session_factory()
    # `AsyncConnection`（`sessionmaker(bind=conn)`）：用它的引擎另开一条。
    # 判断必须按类型做，不能 `getattr(bind, "engine", ...)`——`AsyncEngine`
    # 自己也有 `.engine`（返回同步引擎），那样会把引擎误当成连接。
    if isinstance(bind, AsyncConnection):
        return async_sessionmaker(bind.engine, expire_on_commit=False)
    return async_sessionmaker(bind, expire_on_commit=False)


async def _record_purge_failure(
    session: AsyncSession, *, tombstone_id: str, error_code: str
) -> None:
    """用**独立事务**把墓碑置为 failed（含原因码）。

    不能复用传进来的 session：它的事务已经被回滚（或者已不可用），而且
    "失败"这个事实必须**独立提交**——它要能在调用方回滚之后仍然留在库里。

    这一步自己失败也不能掩盖原始异常：吞掉并记日志。墓碑会停在 `running`，
    而 `pending_tombstones()` 也扫 `running`，所以 sweep 下一轮还会重试它。
    """
    factory = _independent_session_factory(session)
    try:
        async with factory() as fresh:
            # PostgreSQL 上再兜一层：万一还有别人持着这一行（例如另一个
            # worker 正在清理同一个墓碑），`lock_timeout` 会让这次写入在
            # 3 秒后失败并记日志，而不是把 purge worker 永久挂在这里。
            if fresh.bind is not None and fresh.bind.dialect.name == "postgresql":
                await fresh.execute(text("SET LOCAL lock_timeout = '3s'"))
            await fresh.execute(
                update(MemoryPurgeTombstone)
                .where(MemoryPurgeTombstone.id == tombstone_id)
                .values(state="failed", error_code=error_code)
            )
            await fresh.commit()
    except Exception as exc:  # noqa: BLE001 - 记账失败不能掩盖原始异常
        logger.error(
            "记忆清理失败状态写不进去 tombstone=%s：%s",
            tombstone_id,
            type(exc).__name__,
        )


def _result_from_tombstone(tombstone: MemoryPurgeTombstone) -> PurgeResult:
    return PurgeResult(
        tombstone_id=tombstone.id,
        state=tombstone.state,
        memories_deleted=int(tombstone.memories_deleted or 0),
        episodes_deleted=int(tombstone.episodes_deleted or 0),
        core_deleted=int(tombstone.core_deleted or 0),
        awareness_deleted=int(tombstone.awareness_deleted or 0),
        embeddings_deleted=int(tombstone.embeddings_deleted or 0),
        entities_deleted=int(tombstone.entities_deleted or 0),
        retrieval_logs_deleted=int(tombstone.retrieval_logs_deleted or 0),
        events_scrubbed=int(tombstone.events_scrubbed or 0),
        error_code=tombstone.error_code,
    )


def _belongs_to_subject(subject_id: str):
    """事件属于这个主体的判据：**新列或旧 payload 任一说它是**。

    ## 为什么要两条路

    `outbox_events.subject_id` 是随真删一起新增的列，历史行全是 NULL。
    旧事件的归属信息只存在于 payload 里（`memory.extract` 的 payload 有
    `{"subject_id": ...}`）。只按列过滤，历史事件一条都选不中——原文留在
    库里，用户以为删了。

    反过来只按 payload 过滤也不行：payload 的键名各 topic 不同，JSON 上
    查询没有索引，新事件明明有列可用却在做全表扫描。

    所以用 `OR` 取并集：列命中走索引，payload 命中兜住历史。两边都命中
    同一行不会重复（SQL 的 OR 不会产生重复行）。

    ## 为什么用 `as_string()` 而不是 `->>`

    SQLAlchemy 的 JSON 类型在不同后端上语法不同；`.as_string()` 会编译成
    当前后端正确的取文本表达式（PostgreSQL 上是 `->>`）。写死 `->>` 会把
    这个模块绑到 PostgreSQL 上，测试用别的后端就会炸。
    """
    return or_(
        OutboxEvent.subject_id == subject_id,
        OutboxEvent.payload["subject_id"].as_string() == subject_id,
    )


async def write_allowed(session: AsyncSession, *, subject_id: str, generation: int) -> bool:
    """这次写入还允许吗？消费侧在**写库前**调用。

    代次一致才允许写。不一致意味着期间发生过遗忘请求，本次写入必须作废。
    """
    current = await current_generation(session, subject_id)
    return int(generation) == current


# ---------------------------------------------------------------- 在途任务栅栏
#
# 为什么用 `ContextVar` 而不是改 handler 签名：
#
# 所有 handler 的签名都是 `dict -> None`，payload 是**业务参数**。代次是
# 调度层的元信息，塞进 payload 有两个问题：一是污染每个 handler 的入参，
# 二是清理阶段会把 payload 换成 `{"purged": True}`，正好把作废依据一起
# 抹掉——代次必须独立于 payload 存活。
#
# `ContextVar` 的语义刚好吻合：它随 `asyncio` 任务传播，drain 循环在调用
# handler 前 set、之后 reset，handler 内部任何 await 点都读得到同一个值，
# 而并发的另一个事件各有各的上下文，不会串。

_active_claim: ContextVar[ClaimFence | None] = ContextVar(
    "ling_memory_claim_fence", default=None
)


@dataclass(frozen=True)
class ClaimFence:
    """当前正在执行的 outbox 事件的遗忘代次快照。"""

    topic: str
    subject_id: str | None
    generation: int


def bind_claim_fence(*, topic: str, subject_id: str | None, generation: int):
    """调度层在调用 handler 前绑定。返回值交给 `release_claim_fence`。"""
    return _active_claim.set(
        ClaimFence(topic=topic, subject_id=subject_id, generation=int(generation or 0))
    )


def release_claim_fence(token) -> None:  # noqa: ANN001
    _active_claim.reset(token)


def active_claim_fence() -> ClaimFence | None:
    return _active_claim.get()


async def claim_still_valid(session: AsyncSession) -> bool:
    """当前在途任务还有效吗？**写库前**调用。

    - 没有绑定（直接调用 handler 的测试/脚本）：视为有效，不改变旧行为；
    - 事件没有主体（与主体无关的维护任务）：视为有效；
    - 有主体：代次必须与库里一致。
    """
    fence = _active_claim.get()
    if fence is None or not fence.subject_id:
        return True
    return await write_allowed(
        session, subject_id=fence.subject_id, generation=fence.generation
    )


async def guard_write(session: AsyncSession) -> bool:
    """`claim_still_valid` 的语义化别名，给 handler 写库前用。"""
    return await claim_still_valid(session)


async def list_tombstones(
    session: AsyncSession, *, subject_id: str, limit: int = 5
) -> list[MemoryPurgeTombstone]:
    """该主体最近的清理记录（给前端查"删到哪一步了"）。"""
    rows = (
        await session.execute(
            select(MemoryPurgeTombstone)
            .where(MemoryPurgeTombstone.subject_id == subject_id)
            .order_by(MemoryPurgeTombstone.requested_at.desc())
            .limit(limit)
        )
    ).scalars()
    return list(rows)


async def pending_tombstones(session: AsyncSession, *, limit: int = 20) -> list[str]:
    """还没清理完的墓碑 id（后台任务扫这个）。"""
    rows = (
        await session.execute(
            select(MemoryPurgeTombstone.id)
            .where(MemoryPurgeTombstone.state.in_(("pending", "running", "failed")))
            .order_by(MemoryPurgeTombstone.requested_at)
            .limit(limit)
        )
    ).scalars()
    return list(rows)


# ------------------------------------------------------------------ 读侧硬门
#
# ## 为什么光有"物理删除"不够
#
# 两段式里，请求阶段与清理阶段之间有一个**真实的窗口**：墓碑刚写、代次刚
# 加一，但行还在库里（后台可能要几十秒才轮到它）。这段时间里：
#
# - `GET /v1/memory` 会照常列出那些行——用户刚点完"删除"，刷新还看得见；
# - `MemoryService.recall()` 会照常召回，**下一轮回复就会把她刚被要求忘掉
#   的事说出来**。这是最糟的一种：用户以为删了，而她当场复述。
#
# 代次栅栏管的是**写入**，管不到**读取**：读路径根本不看 outbox。所以读侧
# 必须有自己的门，而且判据不能是"行还在不在"（那时还在），只能是"这个主体
# 有没有未完成的墓碑"。
#
# ## 为什么失败状态也算"未完成"
#
# `failed` 意味着"用户以为删了但数据还在"，后台还会重试。这时放行读取，
# 等于用一次清理失败换一次泄漏。宁可多黑一段时间。


async def has_open_purge(session: AsyncSession, *, subject_id: str) -> bool:
    """该主体是否有**未清理完**的墓碑（有就一律不返回任何记忆）。"""
    count = await session.scalar(
        select(func.count())
        .select_from(MemoryPurgeTombstone)
        .where(
            MemoryPurgeTombstone.subject_id == subject_id,
            MemoryPurgeTombstone.state.in_(("pending", "running", "failed")),
        )
    )
    return bool(count)


async def open_purge_subjects(
    session: AsyncSession, *, subject_ids: list[str]
) -> set[str]:
    """批量版：这些主体里哪些正处在未完成的清理中（一次查询）。"""
    if not subject_ids:
        return set()
    rows = (
        await session.execute(
            select(MemoryPurgeTombstone.subject_id)
            .where(
                MemoryPurgeTombstone.subject_id.in_(subject_ids),
                MemoryPurgeTombstone.state.in_(("pending", "running", "failed")),
            )
            .distinct()
        )
    ).scalars()
    return {str(s) for s in rows}



def now_utc() -> datetime:
    """当前 UTC 时间（测试要冻结时钟时替换这一个入口）。"""
    return datetime.now(UTC)


__all__ = [
    "PURGEABLE_TOPICS",
    "PURGE_STATES",
    "ClaimFence",
    "MemoryPurgeError",
    "PurgeRequest",
    "PurgeResult",
    "active_claim_fence",
    "bind_claim_fence",
    "claim_still_valid",
    "current_generation",
    "guard_write",
    "has_open_purge",
    "list_tombstones",
    "now_utc",
    "open_purge_subjects",
    "pending_tombstones",
    "purge_subject",
    "release_claim_fence",
    "request_purge",
    "write_allowed",
]
