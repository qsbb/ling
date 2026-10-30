"""Memory 域：分层记忆、证据、检索、纠正与遗忘。

唯一写入方。分层（设计文档 §6.2）：
- Episode：具体互动的证据化摘要
- Assertion：偏好、稳定事实、知识（带置信度与有效期）
- Procedure：技能经验

硬规则：
1. 检索**先**按 owner + visibility + 有效期 + 撤回状态过滤，**再**排序相关性。
2. 每条记忆必须有 ``source_event_ids``；模型推断不能当事实。
3. 用户纠正生成 supersede/retract，立即让派生内容失效。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ling.contracts.v1 import MemoryProposal, Origin, Visibility
from ling.infra.models import (
    MemoryEpisode,
    MemoryRetrievalLog,
    MemoryRow,
    utcnow,
)
from ling.infra.security import new_id
from ling.ports.config import MemoryPolicy

logger = logging.getLogger("ling.domains.memory")

MAX_RETENTION_DAYS = 3650


@dataclass(frozen=True)
class MemoryHit:
    memory_id: str
    kind: str
    text: str
    origin: Origin
    visibility: Visibility
    confidence: float
    source_event_ids: list[str]
    score: float
    valid_until: datetime | None


@dataclass
class RecallResult:
    hits: list[MemoryHit] = field(default_factory=list)
    filtered_out: int = 0
    strategy: str = "fulltext+structured"


def _tokenize(text: str) -> set[str]:
    """中英文混合分词：英文按词，中文按 2-gram。够用且可解释。"""
    lowered = text.lower()
    tokens: set[str] = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", lowered)
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i : i + 2])
    if len(cjk) == 1:
        tokens.add(cjk)
    return tokens


# BM25 参数（k1/b 取通用默认值；中文 2-gram 下表现稳定）
_BM25_K1 = 1.5
_BM25_B = 0.75
# 时间衰减半衰期（天）：越久远的记忆排序越靠后，但不影响"是否可见"
_RECENCY_HALF_LIFE_DAYS = 120.0


def _bm25_scores(query: str, docs: list[tuple[str, str]]) -> dict[str, float]:
    """计算 BM25 分数。

    ``docs`` 是 (memory_id, text) 列表。BM25 比裸 Jaccard 更合理：
    - 词频饱和（k1）：同一词出现 10 次不会比出现 2 次相关 5 倍
    - 长度归一（b）：长记忆不会因为更长就天然得分更高
    - IDF：区分度高的词权重更大

    参考资料：mem0 的 keyword_search + normalize_bm25 融合策略。
    """
    query_tokens = _tokenize(query)
    if not query_tokens or not docs:
        return {}
    tokenized: dict[str, list[str]] = {}
    for mem_id, text in docs:
        tokenized[mem_id] = list(_tokenize(text))
    total_docs = len(tokenized)
    avg_len = sum(len(t) for t in tokenized.values()) / max(1, total_docs)

    # 文档频率
    df: dict[str, int] = {}
    for tokens in tokenized.values():
        for token in set(tokens):
            df[token] = df.get(token, 0) + 1

    scores: dict[str, float] = {}
    for mem_id, tokens in tokenized.items():
        if not tokens:
            scores[mem_id] = 0.0
            continue
        tf: dict[str, int] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        doc_len = len(tokens)
        score = 0.0
        for token in query_tokens:
            freq = tf.get(token, 0)
            if freq == 0:
                continue
            idf = math.log(1 + (total_docs - df.get(token, 0) + 0.5) / (df.get(token, 0) + 0.5))
            denom = freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc_len / max(1e-9, avg_len))
            score += idf * (freq * (_BM25_K1 + 1)) / max(1e-9, denom)
        scores[mem_id] = score
    return scores


_SUMMARY_PREFIX = {
    "name": "用户的名字",
    "likes": "用户喜欢",
    "dislikes": "用户不喜欢",
    "lives_in": "用户住在",
    "works_at": "用户在",
    "has_pet": "用户养了",
    "family": "用户的家人",
    "birthday": "用户的生日",
    "health": "用户的健康情况",
    "habit": "用户的习惯",
    "ongoing_project": "用户在做",
    "important_date": "用户的重要日子",
    "boundary": "用户的边界",
}


def _summary_text(slot: str, values: list[str]) -> str:
    prefix = _SUMMARY_PREFIX.get(slot, "用户提到")
    return f"{prefix}：{'、'.join(values)}"


# 单值槽位：同一主体同时只能有一个有效值。
# 住址、名字、职业、生日这类改了就是改了；不关闭旧值的话，
# 「我住杭州」和「我住苏州」会同时有效，模型可能挑旧的回答
# （"改口后仍按旧事实回答"这类问题）。
# 多值槽位（likes / dislikes / habit / has_pet）**绝不**互相取代。
SINGLE_VALUE_SLOTS = {
    "name",
    "lives_in",
    "works_at",
    "birthday",
    "health",
}


def is_single_value(slot: str | None) -> bool:
    return str(slot or "") in SINGLE_VALUE_SLOTS


# 相关性下限（相对）：RRF 分数没有绝对标度，不能用固定阈值，
# 所以按"与第一名的比例"过滤。低于这个比例说明它只是凑数，
# 塞进证据车道会让角色提起不相关的事。
# 取 0.35：真实数据上第一名与噪声的差距通常远大于此。
RELATIVE_SCORE_FLOOR = 0.35

# 精排只作用于前 N 条：重排 API 比 embedding 贵且慢
RERANK_TOP_N = 20

# 一次检索最多**同步补**几条向量。
#
# 为什么需要上限：补向量要调外部 embedding API。积压 500 条时，一次召回
# 会串行打 500 次 HTTP，用户等的是这条回复——这是最坏的性能悬崖。
# 超出的留给后台 `embedding.backfill` 预计算。
#
# 8 是经验初值：正常积压远小于此，真积压时宁可这次语义路不完整，
# 也不要把回复拖死。
MAX_LAZY_EMBEDS = 8


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# RRF 常数：60 是检索文献里的常用取值（Cormack et al.）。
# 越大则各路排名越平均，越小则越偏向"某一路的第一名"。
RRF_K = 60


def _rank_map(rows: list, *, key) -> dict:  # noqa: ANN001
    """按 key 排序后给出 1-based 名次；同分项共享名次，ID 只作稳定排序。"""
    ordered = sorted(rows, key=lambda row: (key(row), row.id))
    ranks: dict = {}
    previous_score = None
    rank = 0
    for index, row in enumerate(ordered):
        score = key(row)
        if index == 0 or score != previous_score:
            rank = index + 1
            previous_score = score
        ranks[row.id] = rank
    return ranks


def _tokens(text: str) -> set[str]:
    """粗粒度切词（中文 2-gram + 英文整词），用于 MMR 相似度。"""

    return set(_query_like_tokens(text))


def _mmr_filter(rows: list, tokens_of, *, max_similarity: float = 0.7) -> list:  # noqa: ANN001
    """贪心近重复抑制：与已选项相似度超阈值的后来者被丢弃。

    不做完整 MMR（那要连续优化相关性/多样性权重），只做"去重"这一步——
    实测这一步就足以解决"证据车道被近似重复占满"的问题，
    而且不会因为多样性惩罚把高相关记忆排到后面。
    """
    kept: list = []
    kept_tokens: list[set[str]] = []
    for row in rows:
        cur = tokens_of(row)
        if cur and any(
            _jaccard(cur, prev) >= max_similarity for prev in kept_tokens if prev
        ):
            continue
        kept.append(row)
        kept_tokens.append(cur)
    return kept


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _query_like_tokens(text: str) -> list[str]:
    """与检索切词保持一致的粗切（中文 2-gram + 英文整词）。"""
    import re

    out: list[str] = []
    out.extend(w.lower() for w in re.findall(r"[A-Za-z0-9_]{2,}", text or ""))
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        if len(run) <= 2:
            out.append(run)
        else:
            out.extend(run[i : i + 2] for i in range(len(run) - 1))
    return out


def _recency_factor(
    created_at: datetime | None,
    now: datetime,
    half_life_days: float | None = None,
) -> float:
    """时间衰减因子（0.5 ~ 1.0）。新记忆略占优，但不会让旧的重要事实消失。"""
    if created_at is None:
        return 0.8
    created = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - created).total_seconds() / 86400.0)
    # 半衰期衰减，但下限 0.5：旧记忆仍然可被检索到
    half_life = half_life_days or _RECENCY_HALF_LIFE_DAYS
    return max(0.5, 0.5 ** (age_days / half_life))


def _score(query_tokens: set[str], text: str, confidence: float) -> float:
    """保留给单条调用的简化打分（BM25 语料级打分见 ``_bm25_scores``）。"""
    if not query_tokens:
        return 0.0
    doc_tokens = _tokenize(text)
    if not doc_tokens:
        return 0.0
    overlap = len(query_tokens & doc_tokens)
    if overlap == 0:
        return 0.0
    jaccard = overlap / len(query_tokens | doc_tokens)
    return round(jaccard * (0.5 + 0.5 * confidence), 6)


# 召回候选池上界。取"最近更新的 N 条"再精排：既保住近期相关性，
# 又让每轮成本与记忆总量解耦（原来是无 LIMIT 全量扫）。
RECALL_CANDIDATE_LIMIT = 500


class MemoryService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        embedder: object | None = None,
        reranker: object | None = None,
        policy: MemoryPolicy | None = None,
    ) -> None:
        self._session = session
        # 记忆策略来自已发布配置的 memory 段。不传时用默认值，
        # 因此未配置时行为与抽取常量之前完全一致。
        self._policy = policy or MemoryPolicy()
        # 语义能力可选：未配置时检索退回 BM25+RRF（字面路径），功能不残缺。
        # 参照实现也是"语义路径默认关闭，明确配置后才启用"。
        if embedder is None or reranker is None:
            from ling.domains.memory.providers import build_embedding, build_reranker

            embedder = embedder or build_embedding()
            reranker = reranker or build_reranker()
        self._embedder = embedder
        self._reranker = reranker

    # ---------------------------------------------------------- 写入

    async def propose(
        self,
        proposal: MemoryProposal,
        *,
        subject_id: str,
        source_window_id: str | None = None,
        source_account: str | None = None,
        source_visibility: str = "subject_private",
        turn_id: str | None = None,
        slot: str | None = None,
        value: str | None = None,
    ) -> str | None:
        """接受一条记忆候选。无证据或来源是模型推测时拒绝写入长期记忆。"""
        if not proposal.source_event_ids:
            return None
        if proposal.origin is Origin.model_inference:
            # 模型推测可以进 Episode 的草稿区，但不能作为可检索事实
            return None
        # 单值槽位：新值生效即关闭旧值（双时间轴的事实侧）。
        # 保留旧行（不删）以便回答"以前住哪"——这正是 valid_until 的意义。
        # superseded_by 指向新行，链可追溯。
        if slot and is_single_value(slot) and proposal.kind == "assertion":
            await self._close_previous_values(
                subject_id=subject_id,
                slot=slot,
                new_value=(value or proposal.text.strip()),
            )

        if turn_id and slot:
            # 幂等：按 (turn_id, slot) 判重，与文本无关。
            # 这样提取规则换措辞、换模板都不会重复长出记忆。
            existing = (
                await self._session.execute(
                    select(MemoryRow).where(
                        MemoryRow.turn_id == turn_id,
                        MemoryRow.slot == slot,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                # 规则升级后同一槽位的新措辞可以更新既有条目，
                # 而不是新增一条（保持"一条事实一条记录"）。
                if existing.text != proposal.text.strip():
                    existing.text = proposal.text.strip()
                    await self._session.flush()
                return existing.id
        row = MemoryRow(
            id=new_id("mem"),
            subject_id=subject_id,
            kind=proposal.kind,
            text=proposal.text.strip(),
            origin=proposal.origin.value,
            visibility=proposal.visibility.value,
            confidence=proposal.confidence,
            source_event_ids=list(proposal.source_event_ids),
            valid_from=utcnow(),
            valid_until=proposal.valid_until,
            consent_revision=0,
            retracted=False,
            source_window_id=source_window_id,
            source_account=source_account,
            source_visibility=source_visibility,
            turn_id=turn_id,
            slot=slot,
            value=(value or None),
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def record_episode(
        self,
        *,
        subject_id: str,
        window_id: str,
        summary: str,
        source_event_ids: list[str],
        origin: Origin = Origin.user_statement,
        visibility: Visibility = Visibility.subject_private,
        occurred_at: datetime | None = None,
        source_visibility: str | None = None,
        turn_id: str | None = None,
        idempotent: bool = True,
    ) -> str:
        """写一条情节记忆。必须有来源事件。"""
        if not source_event_ids:
            raise ValueError("Episode 必须有 source_event_ids（经历有来源）")
        if idempotent and turn_id:
            # worker 至少一次处理：同一轮重试时直接返回既有情节，
            # 不重复写、也不抛异常（注意：靠数据库唯一约束兜底，
            # 但要先把"已存在"翻译成正常返回）。
            existing = (
                await self._session.execute(
                    select(MemoryEpisode).where(MemoryEpisode.turn_id == turn_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing.id
        row = MemoryEpisode(
            id=new_id("ep"),
            subject_id=subject_id,
            window_id=window_id,
            summary=summary.strip()[:4000],
            origin=origin.value,
            visibility=visibility.value,
            source_event_ids=list(source_event_ids),
            occurred_at=occurred_at or utcnow(),
            consent_revision=0,
            source_visibility=source_visibility or visibility.value,
            turn_id=turn_id,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    # ---------------------------------------------------------- 检索

    async def recall(
        self,
        *,
        subject_id: str,
        query: str,
        visibility_allow: set[Visibility],
        limit: int = 8,
        now: datetime | None = None,
        trace_window_id: str | None = None,
        source_filter: object | None = None,
        include_history: bool = False,
    ) -> RecallResult:
        """先过滤权限/有效期/撤回，再按相关性排序。"""
        now = now or datetime.now(UTC)
        result = RecallResult()

        # 合并过的账号要一起召回：A 和 B 是同一个人时，问 A 也该想起
        # B 说过的话。记忆行本身**不搬动**（那是有损操作），
        # 靠把 subject 集合展开来读。
        from ling.domains.identity.service import IdentityService

        subjects = await IdentityService(self._session).subject_group(subject_id)

        # **必须有上界**。原实现按 subject 全量取回再在 Python 里算相关度：
        # 记忆只有 3 条时看不出问题，涨到几千条就是每轮对话扫全表 + 全量 BM25。
        # 这里按最近更新取一个候选池——真正的排序仍在下面做，
        # 池子只是保证"每轮成本不随记忆总量增长"。
        fetched_rows = list(
            (
                await self._session.execute(
                    select(MemoryRow)
                    .where(
                        MemoryRow.subject_id.in_(list(subjects)),
                        MemoryRow.retracted.is_(False),
                        # 归档 = 保留期到期，不再参与检索（数据仍在，可审计）
                        MemoryRow.archived_at.is_(None),
                    )
                    # 先确定最近更新的有界候选池；池内语义排序不依赖此行序。
                    .order_by(MemoryRow.updated_at.desc(), MemoryRow.id.asc())
                    .limit(RECALL_CANDIDATE_LIMIT)
                )
            ).scalars()
        )
        # 防御性按主键对齐和去重，确保向量输入/各路 rank 仅含唯一记录。
        rows = list({row.id: row for row in fetched_rows}.values())
        allowed = {v.value for v in visibility_allow}
        # 第一步：**只做过滤**（权限、有效期、保留期、撤回），不参与打分
        candidates: list[MemoryRow] = []
        for row in rows:
            if row.visibility not in allowed:
                result.filtered_out += 1
                continue
            # 保留期是**硬门**，不是排序因素（此前实现的缺口）：
            # 即使归档任务没跑或停摆，到期记忆也绝不能进模型输入。
            if self._retention_expired(row, now):
                result.filtered_out += 1
                continue
            # 来源核对（ADR-0004）：群 A 的内容不得进群 B。
            # 这一步原先只对 Episode 做了、漏了 MemoryRow——那是一个真实的
            # 跨群泄漏，被输出等价性验收抓出来。
            if source_filter is not None and not source_filter(row):  # type: ignore[operator]
                result.filtered_out += 1
                continue
            if row.valid_until is not None:
                valid_until = row.valid_until
                if valid_until.tzinfo is None:
                    valid_until = valid_until.replace(tzinfo=UTC)
                if valid_until <= now and not include_history:
                    # 默认只给"现在仍然成立"的事实。
                    # 用户问「以前住哪」时才带上历史（include_history），
                    # 否则改了住址之后旧值会一直干扰当前回答。
                    result.filtered_out += 1
                    continue
            candidates.append(row)

        # 第二步：在**已授权的候选集**上做多路排序，再用 RRF 融合。
        # 相关性别名永远不会把无权内容"排上来"——它们根本没进候选集。
        #
        # 为什么改 RRF（Reciprocal Rank Fusion）而不是乘法加权：
        # 乘法融合要把"相关性/新近度/重要性"压到同一个尺度上，权重怎么定都是拍的，
        # 而且任一路信号为 0 就把整条打成 0（长尾记忆永远出不来）。
        # RRF 只看**排名**，天然免疫量纲差异，是参照实现（某同类实现）
        # 与通用检索实践的共同选择：score = Σ 1/(k + rank)。
        bm25 = _bm25_scores(query, [(row.id, row.text) for row in candidates])
        # 注意：这里**不能 return** —— 下面是情节（Episode）的独立检索路径。
        # 之前写成提前 return，导致"只有情节、没有断言"的窗口召回全空
        # （被跨群隔离测试抓出来）。
        #
        # 候选范围取决于有没有语义路：
        # - 无 embedding：只用 BM25 命中的（与旧行为等价，不必扫全表）
        # - 有 embedding：用**全部已授权候选**。否则语义路永远救不了
        #   "字面不命中但语义相关"的记忆——而那正是它存在的意义
        #   （"我有什么忌口" vs "用户不吃香菜" 没有共同 2-gram）。
        # 实体路（**先算**，因为候选范围要参考它）：
        # 查询里提到的实体（含别名）→ 沿关联扩展候选。
        # 这是"我妈提过的那个人后来怎样"能串起来的关键：
        # 字面可能完全不重叠，但沿实体关系能找到相关记忆。
        rank_entity: dict[str, int] = {}
        _entity_hit_ids: set[str] = set()
        try:
            from ling.domains.memory.entities import EntityService

            _svc = EntityService(self._session)
            _entities = await _svc.resolve(subject_id=subject_id, text=query)
            if _entities:
                _related = await _svc.related_memory_ids([e.id for e in _entities])
                if _related:
                    # 只在**已授权候选集**里标实体命中：关联不能绕过权限过滤。
                    # 命中的作为独立一路参与 RRF，让"沿关系找到"的记忆有额外支持。
                    entity_hits = [r for r in candidates if r.id in _related]
                    _entity_hit_ids = {r.id for r in entity_hits}
                    # 关联查询没有语义上的先后；同一实体命中的记录共享名次，
                    # 避免数据库返回行序影响 RRF。
                    rank_entity = _rank_map(entity_hits, key=lambda _row: 0)
        except Exception:  # noqa: BLE001 - 实体路故障不影响其他路
            rank_entity = {}

        if self._embedder is not None:
            ordered = list(candidates)
        else:
            # 没有向量时默认只用字面命中——但**实体命中的必须加进来**。
            # 否则实体路形同虚设：查询"我妈提过的那个人"与记忆字面零重叠，
            # 这些记忆早被 BM25 滤掉，实体关联再准也无处加分。
            ordered = [
                row
                for row in candidates
                if bm25.get(row.id, 0.0) > 0 or row.id in _entity_hit_ids
            ]

        rank_bm25 = _rank_map(ordered, key=lambda r: -bm25.get(r.id, 0.0))
        rank_recency = _rank_map(
            ordered,
            key=lambda r: -_recency_factor(
                r.created_at, now, self._policy.recency_half_life_days
            ),
        )

        # 语义路（可选）：配置了 embedding 就多一路向量排名。
        # 这就是参照实现说的"双路检索"——字面命中不到但语义相关时，
        # 单靠 BM25 会整条漏掉（"我有什么忌口" vs "用户不吃香菜" 字面无交集）。
        rank_vector: dict[str, int] = {}
        vector_scores: dict[str, float] = {}
        if self._embedder is not None:
            vector_scores = await self._vector_scores(query, ordered)
            if vector_scores:
                rank_vector = _rank_map(
                    ordered, key=lambda r: -vector_scores.get(r.id, 0.0)
                )
        rank_importance = _rank_map(
            ordered,
            # 重要性 = 置信度 × 强化（对数压缩：注入 100 次不该比 10 次优先 10 倍）
            key=lambda r: -(
                float(r.confidence) * (1.0 + math.log1p(int(r.access_count or 0)))
            ),
        )
        # RRF 分数**按参与信号数归一化**。
        # 不归一化的话，信号多的来源天然占优：断言有 3~4 路信号、
        # 情节只有 2 路时，断言会永远压在情节上面（反之亦然）。
        # 归一化后两者都落在 1/(K+1) ~ 1/(K+n) 同一区间，可比。
        def _rrf(ranks: dict[str, int], key: str) -> float:
            return 1.0 / (RRF_K + ranks[key]) if key in ranks else 0.0

        fused_scores: dict[str, float] = {}
        for row in ordered:
            terms = [
                _rrf(rank_bm25, row.id),
                _rrf(rank_recency, row.id),
                _rrf(rank_importance, row.id),
            ]
            if rank_vector:
                terms.append(_rrf(rank_vector, row.id))
            if rank_entity:
                terms.append(_rrf(rank_entity, row.id))
            present = [t for t in terms if t > 0]
            fused_scores[row.id] = sum(present) / len(present) if present else 0.0
        # MMR 近重复抑制：同一件事被记成多条时只留最相关那条，
        # 否则证据车道会被近似重复占满（参照实现用 MMR，这里用词集 Jaccard 近似）。
        # 融合分同分时优先语义分，再以 ID 保证确定性，避免候选池 SQL 行序泄漏。
        ordered.sort(
            key=lambda r: (
                -fused_scores[r.id],
                -vector_scores.get(r.id, 0.0) if self._embedder is not None else 0.0,
                r.id,
            )
        )
        ordered = _mmr_filter(ordered, lambda r: _tokens(r.text), max_similarity=0.7)

        scored: list[MemoryHit] = []
        for row in ordered:
            fused = fused_scores[row.id]
            scored.append(
                MemoryHit(
                    memory_id=row.id,
                    kind=row.kind,
                    text=row.text,
                    origin=Origin(row.origin),
                    visibility=Visibility(row.visibility),
                    confidence=float(row.confidence),
                    source_event_ids=list(row.source_event_ids or []),
                    score=round(fused, 6),
                    valid_until=row.valid_until,
                )
            )
        # 情节记忆（Episode）：同样是"先过滤受众，再排序"。
        # 长期承接"上次那个事"依赖它，而不是靠窗口短记忆（那个 2 小时就过期）。
        episode_rows = list(
            (
                await self._session.execute(
                    select(MemoryEpisode).where(
                        MemoryEpisode.subject_id == subject_id,
                        MemoryEpisode.archived_at.is_(None),
                    )
                )
            ).scalars()
        )
        allowed = {v.value for v in visibility_allow}
        episode_candidates = [
            ep
            for ep in episode_rows
            if ep.visibility in allowed
            and (source_filter is None or source_filter(ep))  # type: ignore[operator]
        ]
        result.filtered_out += len(episode_rows) - len(episode_candidates)
        ep_bm25 = _bm25_scores(
            query, [(ep.id, ep.summary) for ep in episode_candidates]
        )
        # 语义路存在时情节也要看全部（理由同断言：否则救不了字面不命中的经历）
        ep_pool = (
            list(episode_candidates)
            if self._embedder is not None
            else [ep for ep in episode_candidates if ep_bm25.get(ep.id, 0.0) > 0]
        )
        # 情节查询没有 ORDER BY；先把池子按 id 定序，后面同分同置信度时
        # 稳定排序不会把数据库行序泄漏到最终结果里。
        ep_pool = sorted(ep_pool, key=lambda e: e.id)
        rank_ep_bm25 = _rank_map(ep_pool, key=lambda e: -ep_bm25.get(e.id, 0.0))
        rank_ep_recency = _rank_map(
            ep_pool,
            key=lambda e: -_recency_factor(
                getattr(e, "occurred_at", None), now, self._policy.recency_half_life_days
            ),
        )
        rank_ep_vector: dict[str, int] = {}
        if self._embedder is not None and ep_pool:
            ep_vec = await self._vector_scores_for(
                query, ep_pool, text_of=lambda e: e.summary  # type: ignore[arg-type]
            )
            if ep_vec:
                rank_ep_vector = _rank_map(ep_pool, key=lambda e: -ep_vec.get(e.id, 0.0))

        for ep in ep_pool:
            keyword = ep_bm25.get(ep.id, 0.0)
            # 也与断言用同一套 RRF + 归一化，否则两者分数尺度差上百倍，
            # 情节会永远压过断言（真实数据上实测过）。
            ep_terms = [
                1.0 / (RRF_K + rank_ep_bm25[ep.id]),
                1.0 / (RRF_K + rank_ep_recency[ep.id]),
            ]
            if rank_ep_vector:
                ep_terms.append(1.0 / (RRF_K + rank_ep_vector.get(ep.id, len(ep_pool))))
            present = [t for t in ep_terms if t > 0]
            fused = sum(present) / len(present) if present else 0.0
            if keyword <= 0 and not rank_ep_vector:
                continue
            scored.append(
                MemoryHit(
                    memory_id=ep.id,
                    kind="episode",
                    text=ep.summary,
                    origin=Origin(ep.origin),
                    visibility=Visibility(ep.visibility),
                    confidence=1.0,
                    source_event_ids=list(ep.source_event_ids or []),
                    score=round(fused, 6),
                    valid_until=None,
                )
            )

        # 精排（可选）：**在合并断言与情节之后**统一做。
        # 之前先精排 rows 再 `scored.sort` 按旧 RRF 分重排，精排等于无效
        # （实测：让精排器明确颠倒前两名，返回顺序仍是原 RRF 顺序）；
        # 而且情节完全没参与精排。
        if self._reranker is not None and len(scored) > 1:
            scored = await self._rerank_hits(query, scored)

        # 稳定排序：同分时保留上面已用向量分/ID 定好的顺序；
        # 情节池已按 id 预排序，所以数据库行序不会泄漏到这里。
        scored.sort(key=lambda h: (-h.score, -h.confidence))
        # 相对相关性下限：只保留与第一名同量级的结果。
        # 不做这一步的话，"limit=6" 会把第 6 名的凑数记忆也塞进证据车道，
        # 角色就容易在不相干的话题上提起旧事。
        if scored:
            floor = scored[0].score * self._policy.relative_score_floor
            kept = [h for h in scored if h.score >= floor]
            result.filtered_out += len(scored) - max(1, len(kept))
            scored = kept or scored[:1]
        result.strategy = "rrf+mmr (assertion+episode)"
        result.hits = scored[:limit]

        if trace_window_id:
            self._session.add(
                MemoryRetrievalLog(
                    id=new_id("mlog"),
                    window_id=trace_window_id,
                    query=query[:500],
                    hit_ids=[h.memory_id for h in result.hits],
                    filtered_out=result.filtered_out,
                    strategy=result.strategy,
                )
            )
            await self._session.flush()
        return result

    # ---------------------------------------------------------- 纠正与遗忘

    async def retract(self, memory_id: str, *, reason: str, superseded_by: str | None = None) -> bool:
        """撤回一条记忆。立即失效（检索会跳过），保留证据链。"""
        row = await self._session.get(MemoryRow, memory_id)
        if row is None:
            return False
        row.retracted = True
        row.retract_reason = reason[:500]
        row.superseded_by = superseded_by
        row.retracted_at = utcnow()
        await self._session.flush()
        return True

    async def supersede(self, old_id: str, new_text: str, *, reason: str) -> str | None:
        """用户纠正：旧记忆失效，新记忆建立 supersede 链。"""
        old = await self._session.get(MemoryRow, old_id)
        if old is None:
            return None
        new_id_value = new_id("mem")
        new_row = MemoryRow(
            id=new_id_value,
            subject_id=old.subject_id,
            kind=old.kind,
            text=new_text.strip(),
            origin=Origin.user_statement.value,
            visibility=old.visibility,
            confidence=min(1.0, float(old.confidence) + 0.1),
            source_event_ids=list(old.source_event_ids or []),
            valid_from=utcnow(),
            valid_until=old.valid_until,
            consent_revision=int(old.consent_revision or 0) + 1,
            retracted=False,
            supersedes=old.id,
        )
        self._session.add(new_row)
        await self.retract(old.id, reason=reason, superseded_by=new_id_value)
        return new_id_value

    async def _close_previous_values(
        self, *, subject_id: str, slot: str, new_value: str
    ) -> int:
        """把同槽位仍有效的旧值关闭（valid_until=now）。

        只动 ``valid_until``，**不删行**：旧值必须留下，
        否则"你以前住哪"永远答不出来
        （参照 Graphiti/Zep 的双时间轴：事实有效期与系统记录时间分开）。
        """
        now = datetime.now(UTC)
        rows = list(
            (
                await self._session.execute(
                    select(MemoryRow).where(
                        MemoryRow.subject_id == subject_id,
                        MemoryRow.slot == slot,
                        MemoryRow.archived_at.is_(None),
                        MemoryRow.retracted.is_(False),
                        or_(
                            MemoryRow.valid_until.is_(None),
                            MemoryRow.valid_until > now,
                        ),
                    )
                )
            ).scalars()
        )
        closed = 0
        for row in rows:
            # 值相同就是重复写入，不算改口，不关闭
            if (row.value or "").strip() == new_value.strip():
                continue
            if (row.text or "").strip() == new_value.strip():
                continue
            row.valid_until = now
            closed += 1
        await self._session.flush()
        return closed

    @staticmethod
    def importance(
        row: MemoryRow, *, now: datetime, half_life_days: float | None = None
    ) -> float:
        """一条记忆的"重要性"（决定衰减快慢）。

        参照实现的口径：**高置信、高访问频率、近期**的默认更不容易衰减。
        用对数压缩访问次数——注入 100 次不该比 10 次抗衰减 10 倍，
        否则早期偶然被召回的记忆会永久免疫衰减。
        """
        created = row.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        recency = _recency_factor(created, now, half_life_days)
        access = int(getattr(row, "access_count", 0) or 0)
        return round(
            float(row.confidence) * (1.0 + math.log1p(access)) * recency, 6
        )

    async def compact_low_value(
        self,
        *,
        now: datetime | None = None,
        min_age_days: int = 60,
        importance_floor: float = 0.25,
        min_group: int = 3,
    ) -> dict[str, int]:
        """压缩级联：把同槽位的多条低价值记忆并成一条高层摘要。

        参照实现："将即将衰退的一组记忆压缩为更高层摘要"，且
        "低价值、长期未访问的记忆可在睡眠维护中压缩或归档"。

        规则（全部确定性，不调模型）：
        - 只处理**有 value** 的行（没有值就无法可靠合并，不猜）
        - 年龄 ≥ ``min_age_days``、重要性 < ``importance_floor``
        - 同 slot 达到 ``min_group`` 条才合并（少于这个数并了反而丢信息）
        - 合并后原行**归档**并指向摘要（保留 superseded 链，不物理删除）

        返回 {"groups": 合并组数, "archived": 归档行数}。
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=min_age_days)
        rows = list(
            (
                await self._session.execute(
                    select(MemoryRow).where(
                        MemoryRow.archived_at.is_(None),
                        MemoryRow.retracted.is_(False),
                        MemoryRow.value.is_not(None),
                        MemoryRow.slot.is_not(None),
                        MemoryRow.created_at < cutoff,
                        # 已过期的事实（valid_until 已过）**不能**参与合并：
                        # 合并出的新摘要没有 valid_until，等于把过期事实
                        # 复活成"现行事实"（实测：三条过期的居住地
                        # 压缩后变成可召回的"用户住在：北京、上海、广州"）。
                        or_(
                            MemoryRow.valid_until.is_(None),
                            MemoryRow.valid_until > now,
                        ),
                    )
                )
            ).scalars()
        )
        # 分组键必须包含**全部受众边界**：主体、可见范围、来源窗口。
        #
        # 这不是"记得更准"，是隐私边界：
        # - 只按 slot 分组 → Alice 一条 + Bob 两条会合成 Alice 的摘要，
        #   Bob 的偏好出现在 Alice 的记忆列表里（实测复现）
        # - 只按 (slot, origin) 分组 → 群里可见的和私聊可见的会被并到一起，
        #   等于把私聊内容带进群受众
        # - 不同来源窗口同理：跨窗口的内容不该在压缩时被"洗"成一个来源
        #
        # origin 也必须在键里：混来源合并会伪造证据等级
        # （把"外部观察"并成"用户亲口说"，等于给推断盖上用户的章）。
        by_slot: dict[tuple, list[MemoryRow]] = {}
        for row in rows:
            if (
                self.importance(
                    row, now=now, half_life_days=self._policy.recency_half_life_days
                )
                < importance_floor
            ):
                key = (
                    row.subject_id,
                    row.slot,
                    row.origin,
                    row.visibility,
                    row.source_window_id or "",
                )
                by_slot.setdefault(key, []).append(row)

        groups = 0
        archived = 0
        for key, group in by_slot.items():
            if len(group) < min_group:
                continue
            _subject_id, slot, origin_value, _visibility, _window = key
            values: list[str] = []
            for row in group:
                v = (row.value or "").strip()
                if v and v not in values:
                    values.append(v)
            if len(values) < 2:
                # 全是同一个值 → 是重复而不是"一组事实"，交给归档处理
                continue
            summary_text = _summary_text(str(slot or ""), values)
            summary = MemoryRow(
                id=new_id("mem"),
                subject_id=group[0].subject_id,
                kind="assertion",
                text=summary_text,
                # 沿用组内真实来源，不抬高也不降低证据等级
                origin=origin_value,
                visibility=group[0].visibility,
                confidence=max(float(r.confidence) for r in group),
                source_event_ids=[
                    eid for r in group for eid in (r.source_event_ids or [])
                ][:40],
                source_window_id=group[0].source_window_id,
                source_visibility=group[0].source_visibility,
                slot=slot,
                value=None,
                retention_days=max(int(r.retention_days or 365) for r in group),
                turn_id=None,
            )
            self._session.add(summary)
            await self._session.flush()
            for row in group:
                row.archived_at = now
                row.archive_reason = f"compacted into {summary.id}"
                row.superseded_by = summary.id
                archived += 1
            groups += 1
        await self._session.flush()
        return {"groups": groups, "archived": archived}

    @staticmethod
    def _retention_expired(row: MemoryRow, now: datetime) -> bool:
        """保留期是否已过（``created_at + retention_days <= now``）。

        这是"内容保存上限"的判定。它与 ``valid_until`` 是两件事：
        - valid_until：这条事实**现在还算不算数**
        - retention_days：这条记录**最多存多久**
        两者任一到期都不该进入当轮模型输入。
        """
        created = row.created_at
        if created is None:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        days = int(row.retention_days or 365)
        return created + timedelta(days=days) <= now

    async def archive_expired(self, *, now: datetime | None = None, limit: int = 500) -> int:
        """把保留期已到的记忆与情节归档（不物理删除）。

        判定：``created_at + retention_days < now``。
        ``valid_until`` 到期只是"不再作事实使用"（recall 已过滤），
        与"保留期到期"是两件事——前者仍可能被审计，后者进入归档。

        返回归档条数。
        """

        now = now or datetime.now(UTC)
        archived = 0

        rows = list(
            (
                await self._session.execute(
                    select(MemoryRow).where(
                        MemoryRow.archived_at.is_(None),
                        MemoryRow.retracted.is_(False),
                        MemoryRow.created_at.is_not(None),
                    )
                )
            ).scalars()
        )
        for row in rows:
            created = row.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            days = int(row.retention_days or 365)
            if created + timedelta(days=days) <= now:
                row.archived_at = now
                row.archive_reason = f"retention_days={days} 到期"
                archived += 1
                if archived >= limit:
                    break
        await self._session.flush()
        return archived

    async def archive_episodes(self, *, older_than_days: int = 180, now: datetime | None = None) -> int:
        """归档很久之前且**没有未撤回断言引用**的情节。

        情节本身也有价值（承接"上次那个事"），所以阈值比断言宽。
        """
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=max(1, older_than_days))
        rows = list(
            (
                await self._session.execute(
                    select(MemoryEpisode).where(
                        MemoryEpisode.archived_at.is_(None),
                        MemoryEpisode.occurred_at < cutoff,
                    )
                )
            ).scalars()
        )
        if not rows:
            return 0

        # 真正兑现注释里的"没有未撤回断言引用"：
        # 被仍在使用的断言引用的情节不能归档，否则"上次那件事"的证据链就断了。
        # （之前注释这么写但代码没做——文档与实现不符。）
        referenced = set(
            (
                await self._session.execute(
                    select(MemoryRow.turn_id).where(
                        MemoryRow.turn_id.is_not(None),
                        MemoryRow.archived_at.is_(None),
                        MemoryRow.retracted.is_(False),
                    )
                )
            ).scalars()
        )
        archived = 0
        for row in rows:
            if row.turn_id and row.turn_id in referenced:
                continue
            row.archived_at = now
            archived += 1
        await self._session.flush()
        return archived

    # ---------------------------------------------------------- 语义路（可选）

    async def _vector_scores_for(
        self, query: str, rows: list, *, text_of=None  # noqa: ANN001
    ) -> dict[str, float]:
        """通用向量打分：``text_of`` 允许情节用 summary 而非 text。"""
        from ling.ports.embedding import EmbeddingError

        if not rows:
            return {}
        get_text = text_of or (lambda r: r.text)
        try:
            await self._ensure_embeddings_for(rows, text_of=get_text)
            vectors = await self._load_vectors(rows)
            query_vec = (await self._embedder.embed([query]))[0]  # type: ignore[union-attr]
        except EmbeddingError:
            return {}
        except Exception:  # noqa: BLE001
            return {}
        return {
            row.id: _cosine(query_vec, vectors[row.id])
            for row in rows
            if row.id in vectors
        }

    async def _vector_scores(self, query: str, rows: list) -> dict[str, float]:
        """候选集上的余弦相似度。

        没有 pgvector，所以在候选集上现算。个人规模（千级）可接受；
        上万条时应换 pgvector 或外部向量库——这是有意的降级。
        失败一律返回空字典（检索退回字面路径），不让语义能力故障拖垮召回。
        """
        from ling.ports.embedding import EmbeddingError

        if not rows:
            return {}
        try:
            vectors = await self._ensure_embeddings(rows)
            if not vectors:
                return {}
            query_vec = (await self._embedder.embed([query]))[0]  # type: ignore[union-attr]
        except EmbeddingError:
            return {}
        except Exception:  # noqa: BLE001 - 语义路故障不能影响召回
            return {}
        scores: dict[str, float] = {}
        for row in rows:
            vec = vectors.get(row.id)
            if vec:
                scores[row.id] = _cosine(query_vec, vec)
        return scores

    async def _ensure_embeddings_for(self, rows: list, *, text_of=None) -> None:  # noqa: ANN001
        """补齐缺失向量（不返回，便于行/情节共用）。"""
        from sqlalchemy import select as sa_select

        from ling.infra.models import MemoryEmbeddingRow
        from ling.ports.embedding import EmbeddingError

        get_text = text_of or (lambda r: r.text)
        model_id = getattr(self._embedder, "model_id", "")
        ids = [r.id for r in rows]
        existing = set(
            (
                await self._session.execute(
                    sa_select(MemoryEmbeddingRow.memory_id).where(
                        MemoryEmbeddingRow.memory_id.in_(ids),
                        MemoryEmbeddingRow.model_id == model_id,
                    )
                )
            ).scalars()
        )
        missing = [r for r in rows if r.id not in existing]
        if not missing:
            return
        # **必须有上限**：这里的每次调用都是一次外部 HTTP，
        # 而它发生在召回路径上（用户正在等回复）。
        # 超出部分交给后台 embedding.backfill。
        if len(missing) > MAX_LAZY_EMBEDS:
            logger.info(
                "向量缺口 %d 条，本次只同步补 %d 条，其余留给 backfill",
                len(missing),
                MAX_LAZY_EMBEDS,
            )
            missing = missing[:MAX_LAZY_EMBEDS]
        try:
            embedded = await self._embedder.embed([get_text(r) for r in missing])  # type: ignore[union-attr]
        except EmbeddingError:
            return
        dim = len(embedded[0]) if embedded else 0
        for row, vec in zip(missing, embedded, strict=False):
            self._session.add(
                MemoryEmbeddingRow(
                    memory_id=row.id, model_id=model_id, dim=dim, vector=vec
                )
            )
        await self._session.flush()

    async def _load_vectors(self, rows: list) -> dict[str, list[float]]:
        from sqlalchemy import select as sa_select

        from ling.infra.models import MemoryEmbeddingRow

        model_id = getattr(self._embedder, "model_id", "")
        return {
            row.memory_id: list(row.vector)
            for row in (
                await self._session.execute(
                    sa_select(MemoryEmbeddingRow).where(
                        MemoryEmbeddingRow.memory_id.in_([r.id for r in rows]),
                        MemoryEmbeddingRow.model_id == model_id,
                    )
                )
            ).scalars()
        }

    async def _ensure_embeddings(self, rows: list) -> dict[str, list[float]]:
        """补齐缺失的向量并返回本批全部向量。

        懒补齐而不是写入时同步向量化：写入路径（worker）不该被外部 API
        的可用性拖住。代价是首次检索多一次往返。
        """
        from sqlalchemy import select as sa_select

        from ling.infra.models import MemoryEmbeddingRow
        from ling.ports.embedding import EmbeddingError

        model_id = getattr(self._embedder, "model_id", "")
        ids = [r.id for r in rows]
        existing = {
            row.memory_id: list(row.vector)
            for row in (
                await self._session.execute(
                    sa_select(MemoryEmbeddingRow).where(
                        MemoryEmbeddingRow.memory_id.in_(ids),
                        MemoryEmbeddingRow.model_id == model_id,
                    )
                )
            ).scalars()
        }
        missing = [r for r in rows if r.id not in existing]
        if missing:
            try:
                embedded = await self._embedder.embed([r.text for r in missing])  # type: ignore[union-attr]
            except EmbeddingError:
                # 补不齐就用已有的：部分语义路也好过完全没有
                return existing
            dim = len(embedded[0]) if embedded else 0
            for row, vec in zip(missing, embedded, strict=False):
                existing[row.id] = vec
                self._session.add(
                    MemoryEmbeddingRow(
                        memory_id=row.id,
                        model_id=model_id,
                        dim=dim,
                        vector=vec,
                    )
                )
            await self._session.flush()
        return existing

    async def _rerank_hits(self, query: str, hits: list) -> list:
        """对**最终候选（含情节）**统一精排，并用精排分数替换 RRF 分。

        重排分数是"与本次查询的相关性"，比 RRF 的排名融合更直接，
        因此由它决定最终顺序与相关性下限。
        失败时原样返回（不让精排故障影响召回）。
        """
        from ling.ports.embedding import EmbeddingError

        top_n = min(len(hits), self._policy.rerank_top_n)
        head = hits[:top_n]
        try:
            pairs = await self._reranker.rerank(  # type: ignore[union-attr]
                query, [h.text for h in head], top_n=top_n
            )
        except EmbeddingError:
            return hits
        except Exception:  # noqa: BLE001
            return hits
        if not pairs:
            return hits
        from dataclasses import replace as dc_replace

        reordered: list = []
        for idx, score in pairs:
            if 0 <= idx < top_n:
                # MemoryHit 是 frozen dataclass：用 replace 造带新分数的新对象
                reordered.append(
                    dc_replace(head[idx], score=round(float(score), 6))
                )
        # **只保留被精排过的候选**，不把尾部接回来。
        # 原因：精排分是 0~1 的相关性，RRF 分约 0.016，两者尺度不同；
        # 把未精排的尾部（仍是 RRF 分）接在后面，会因为数值更大而排到前面去。
        # 超出 top_n 的候选本来 RRF 排名就靠后，精排也判不到，丢掉是安全的。
        return reordered

    async def _rerank(self, query: str, rows: list) -> list:
        """用重排模型精排。失败时原样返回（不让精排故障影响召回）。"""
        from ling.ports.embedding import EmbeddingError

        top_n = min(len(rows), self._policy.rerank_top_n)
        try:
            pairs = await self._reranker.rerank(  # type: ignore[union-attr]
                query, [r.text for r in rows[:top_n]], top_n=top_n
            )
        except EmbeddingError:
            return rows
        except Exception:  # noqa: BLE001
            return rows
        if not pairs:
            return rows
        head = [rows[i] for i, _score in pairs if 0 <= i < top_n]
        tail = rows[top_n:]
        return head + tail

    async def reinforce(self, memory_ids: list[str], *, now: datetime | None = None) -> int:
        """记录"这些记忆**真正进了提示词**"，累加强化分。

        只应由调用方在**预算截断之后**传入实际注入的 id。
        参照实现明确要求这一点：把"检索到但被截掉"也算进去，
        被强化的记忆会越来越优先，形成马太效应，长尾永远出不来。

        用单条 UPDATE 完成，避免逐条往返。
        """
        if not memory_ids:
            return 0
        from sqlalchemy import update as sa_update

        result = await self._session.execute(
            sa_update(MemoryRow)
            .where(MemoryRow.id.in_(memory_ids))
            .values(
                access_count=MemoryRow.access_count + 1,
                last_injected_at=now or datetime.now(UTC),
            )
        )
        await self._session.flush()
        return int(getattr(result, "rowcount", 0) or 0)

    async def mark_recalled(self, memory_ids: list[str], *, now: datetime | None = None) -> None:
        """记录"这些记忆刚被想起"，供后续判断"长期不用"。

        只在命中非空时写一次批量 UPDATE，不逐条写（避免召回路径变慢）。
        """
        if not memory_ids:
            return
        from sqlalchemy import update as sa_update

        await self._session.execute(
            sa_update(MemoryRow)
            .where(MemoryRow.id.in_(memory_ids))
            .values(last_recalled_at=now or datetime.now(UTC))
        )
        await self._session.flush()

    async def forget_subject(self, subject_id: str) -> int:
        """清除：撤回断言**并归档情节**（保留墓碑，不物理删除）。

        此前实现的缺口：原先只处理 MemoryRow，Episode 仍可被召回——
        用户说"清除我的记忆"，但情节里的话还能被检索出来。

        保留记录不删：supersede 链与 source_event_ids 引用 id，
        物理删除会断链；墓碑同时是对抗"备份恢复后复活"的一层。
        """
        now = utcnow()
        result = await self._session.execute(
            update(MemoryRow)
            .where(MemoryRow.subject_id == subject_id, MemoryRow.retracted.is_(False))
            .values(retracted=True, retract_reason="用户请求清除", retracted_at=now)
        )
        affected = int(getattr(result, "rowcount", 0) or 0)

        # 情节同样退出检索
        episodes = await self._session.execute(
            update(MemoryEpisode)
            .where(
                MemoryEpisode.subject_id == subject_id,
                MemoryEpisode.archived_at.is_(None),
            )
            .values(archived_at=now)
        )
        affected += int(getattr(episodes, "rowcount", 0) or 0)

        # 核心记忆也一并失效：它是"每轮常驻"的，不清掉的话
        # 用户说了"忘记我"之后，角色仍会每轮把它念出来——这是最直接的失信。
        from ling.infra.models import CoreMemoryRow

        core = await self._session.execute(
            update(CoreMemoryRow)
            .where(
                CoreMemoryRow.subject_id == subject_id,
                CoreMemoryRow.active.is_(True),
            )
            .values(active=False, updated_at=now)
        )
        affected += int(getattr(core, "rowcount", 0) or 0)

        # 意识条目也一并失效（派生投影必须随源失效，§4.2）
        from ling.infra.models import AwarenessItemRow

        awareness = await self._session.execute(
            update(AwarenessItemRow)
            .where(
                AwarenessItemRow.subject_id == subject_id,
                AwarenessItemRow.invalidated_at.is_(None),
            )
            .values(invalidated_at=now)
        )
        affected += int(getattr(awareness, "rowcount", 0) or 0)

        await self._session.flush()
        return affected

    async def list_memories(self, subject_id: str, *, limit: int = 50) -> list[MemoryRow]:
        return list(
            (
                await self._session.execute(
                    select(MemoryRow)
                    .where(MemoryRow.subject_id == subject_id)
                    .order_by(MemoryRow.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    async def default_retention(self, days: int = 365) -> datetime:
        return datetime.now(UTC) + timedelta(days=min(days, self._policy.max_retention_days))


def classify_usage(hit: object) -> str:
    """一条记忆该**怎么用**，而不只是"用不用"。

    参照表达策略的分层做法
    （mention_memory / tone_memory / uncertain_memory）：把所有记忆平铺成
    "你记得的相关事实"，模型就会把不确定的也当事实说出来——
    这比"少记一件事"更容易伤害信任。

    - ``mention``：可以明确说出来（"你上次说……"）
    - ``tone``：只作语气/态度底色，不点明出处
    - ``uncertain``：隐约有印象，对方提起时试探确认，不主动断言
    """
    origin = getattr(hit, "origin", None)
    origin_value = getattr(origin, "value", str(origin or ""))
    confidence = float(getattr(hit, "confidence", 0.0) or 0.0)

    # 模型推测出来的内容**永远**只能算隐约印象，哪怕置信度标得很高：
    # 那是它自己想的，不是对方说过的。
    if origin_value == "model_inference":
        return "uncertain"
    if confidence >= 0.75:
        return "mention"
    if confidence >= 0.45:
        return "tone"
    return "uncertain"


# 提问里出现这些词，说明用户问的是**过去**的状态，
# 需要把已关闭的旧值也带上，否则"你以前住哪"永远答不出。
HISTORY_QUERY_MARKERS = ("以前", "之前", "原来", "过去", "上次说过", "曾经", "旧")


def wants_history(text: str) -> bool:
    return any(m in (text or "") for m in HISTORY_QUERY_MARKERS)


def _is_historical(hit: object) -> bool:
    """已经不再成立的旧事实（valid_until 已过）。"""
    valid_until = getattr(hit, "valid_until", None)
    if valid_until is None:
        return False
    from datetime import UTC, datetime

    if valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=UTC)
    return valid_until <= datetime.now(UTC)


def render_evidence(hits: list) -> str:
    """按表达策略分组渲染证据车道。

    分组本身就是给模型的指令：不同组能说的话不一样。
    """
    groups: dict[str, list] = {"mention": [], "tone": [], "uncertain": [], "history": []}
    for hit in hits:
        # 历史事实单独成组：它们**现在不成立**，只能用于回答"以前怎样"，
        # 混进 mention 会让角色拿旧住址当现状说。
        if _is_historical(hit):
            groups["history"].append(hit)
        else:
            groups[classify_usage(hit)].append(hit)

    blocks: list[str] = []
    if groups["mention"]:
        lines = [
            f"- {h.text}（来源 {getattr(getattr(h, 'origin', None), 'value', '')}"
            f"，置信度 {float(getattr(h, 'confidence', 0.0)):.2f}）"
            for h in groups["mention"]
        ]
        blocks.append("你确实记得的事（可以直接说出来）：\n" + "\n".join(lines))
    if groups["tone"]:
        lines = [f"- {h.text}" for h in groups["tone"]]
        blocks.append(
            "你有印象、但不必点明出处的事（当作语气的底色即可，"
            "不要当成确凿事实复述）：\n" + "\n".join(lines)
        )
    if groups["uncertain"]:
        lines = [f"- {h.text}" for h in groups["uncertain"]]
        blocks.append(
            "你只是隐约觉得的事（对方提起时可以试探确认，"
            "不要主动断言）：\n" + "\n".join(lines)
        )
    if groups["history"]:
        lines = [f"- {h.text}" for h in groups["history"]]
        blocks.append(
            "这些是**以前**的情况，现在已经不是这样了"
            "（只在对方问起过去时提到，不要说成现状）：\n" + "\n".join(lines)
        )
    if blocks:
        # 这一条是「核心记忆证据权限」：
        # 光把记忆分类还不够，还得说清**冲突时的优先级**。
        # 没有这句时，用户说"我搬去成都了"而记忆写着"住在杭州"，
        # 模型会拿记忆去反驳对方——那是把记忆当成比当事人更权威的证据。
        blocks.append(
            "**以上都是你记得的事。如果对方这一轮说了不同的情况，"
            "以对方现在说的为准**——不要拿记忆去反驳他，也不要追问"
            "「你不是说……」。"
        )
    return "\n\n".join(blocks)


__all__ = [
    "HISTORY_QUERY_MARKERS",
    "MemoryHit",
    "MemoryService",
    "RecallResult",
    "classify_usage",
    "wants_history",
    "render_evidence",
]
