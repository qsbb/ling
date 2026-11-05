"""检索质量基线：跑 probes.yaml → 输出指标 + 存基线 JSON。

这是"提速必须同时保准确"的可执行护栏。没有它，任何提速改动都无法证明
"没有降低召回质量"——"变快了"和"变差了"就分不开。

指标口径（全部在 k=6 上算，与 kernel/context.py 的 limit=6 对齐）：

- ``recall@6``       每条探针的 expect 里，有多少比例出现在前 6 条（微平均）
- ``nDCG@6``         命中位置越靠前越高（相关性=1，指数增益）
- ``MRR``            第一个相关命中的倒数名次
- ``empty_rate``     检索返回**零条**的探针比例（防"该想起来却空白"）
- ``privacy_hits``   隐私探针里 forbidden 内容出现的次数（**红线：必须 0**）
- ``unrecallable_hits`` 不可召回探针里出现的任何命中数（**红线：必须 0**）

判定阈值见 ``shots/ia-research/SPEEDUP-GUARDRAILS.md``，本脚本只负责**测量**
与**和基线比对**，不内置"多少算红"的业务判断（那要能改，不该编译进脚本）。

用法::

    # 1) 建基线（写入 tests/eval/baseline.json）
    .venv/bin/python tests/eval/probe_baseline.py --write-baseline

    # 2) 改动后回归（与基线比对，超阈值则非零退出）
    .venv/bin/python tests/eval/probe_baseline.py --compare

    # 3) 只看数字，不落盘
    .venv/bin/python tests/eval/probe_baseline.py

设计约束（都很实在，不是洁癖）：
- **固定 seed + 固定评测时钟**：否则每天跑出来的数不一样，基线没有意义。
- **真 PostgreSQL + 独立 schema**：sqlite 的分词/排序会给出另一套数字，
  用 sqlite 测出来的"没退化"不算证据。
- **不写仓库其他目录**：只在 tests/eval/ 下落 baseline JSON。
- **不改仓库代码**：脚本只 import 现有实现（MemoryService / AudienceSnapshot），
  变异实验在 /tmp 副本里做。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent.parent
PROBES_PATH = EVAL_DIR / "probes.yaml"
BASELINE_PATH = EVAL_DIR / "baseline.json"

# 默认测试库。**只碰下面 schema 指定的那一个 schema**，不碰 ling / ling_test。
DEFAULT_DB_URL = os.environ.get(
    "LING_EVAL_DATABASE_URL",
    "postgresql+asyncpg://ling@127.0.0.1:55432/ling_test",
)

# 判定阈值（与 SPEEDUP-GUARDRAILS.md 表格保持一致；改动要同步两处）
GUARDRAIL = {
    # 准确性红线
    "recall_at_6_drop_abs": 0.02,  # 相对基线下降 > 2%（绝对）即红
    "privacy_hits_max": 0,  # 隐私命中 > 0 即红
    "empty_rate_rise_abs": 0.03,  # 空结果率上升 > 3% 即红
    # 以下为参考项：不单独判红，但要进报告（防止"召回靠运气提升"）
    "ndcg_at_6_drop_abs": 0.05,
    "mrr_drop_abs": 0.05,
}


# ---------------------------------------------------------------- 数据结构


@dataclass
class ProbeOutcome:
    """单条探针的检索结果（也是基线 JSON 的最小单元）。"""

    probe_id: str
    type: str
    status: str  # ok | skip | error
    hits: list[str] = field(default_factory=list)  # 命中的语料 key（按名次）
    expect: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    forbid_all: bool = False
    note: str = ""

    # ---- 逐条指标

    def recall_at_k(self, k: int) -> float:
        if not self.expect:
            return float("nan")  # 无期望的探针（不可召回/隐私）不参与 recall
        found = sum(1 for key in self.expect if key in self.hits[:k])
        return found / len(self.expect)

    def ndcg_at_k(self, k: int) -> float:
        if not self.expect:
            return float("nan")
        # 相关性 = 1（在期望集合里）否则 0；理想排序把所有期望项排最前
        gains = [1.0 if key in self.expect else 0.0 for key in self.hits[:k]]
        dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
        ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(self.expect), k)))
        return dcg / ideal if ideal > 0 else 0.0

    def mrr(self) -> float:
        if not self.expect:
            return float("nan")
        for index, key in enumerate(self.hits, start=1):
            if key in self.expect:
                return 1.0 / index
        return 0.0

    def is_empty(self) -> bool:
        return not self.hits

    def privacy_hits(self) -> int:
        if self.forbid_all:
            return len(self.hits)
        return sum(1 for key in self.forbid if key in self.hits)

    def leaked(self) -> list[str]:
        if self.forbid_all:
            return list(self.hits)
        return [key for key in self.forbid if key in self.hits]


# ---------------------------------------------------------------- 语料装配


def load_spec() -> dict:
    spec = yaml.safe_load(PROBES_PATH.read_text(encoding="utf-8"))
    if not isinstance(spec, dict):
        raise SystemExit("probes.yaml 顶层必须是映射")
    for required in ("seed", "eval_now", "schema", "k", "corpus", "probes"):
        if required not in spec:
            raise SystemExit(f"probes.yaml 缺少字段: {required}")
    return spec


def eval_clock(spec: dict) -> datetime:
    raw = spec["eval_now"]
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))


def _visibility(value: object) -> str:
    # YAML 里写的是契约层枚举名，库里存的是字符串值，两者恰好同名。
    return str(getattr(value, "value", value))


# ---------------------------------------------------------------- 确定性

_counter = {"tick": 0}


def _next_tick() -> int:
    """行序号 → 用来把 updated_at 错开到分钟级（见 _backdate）。"""
    _counter["tick"] += 1
    return _counter["tick"]


def install_deterministic_ids() -> None:
    """把 ``new_id`` 换成确定性的递增生成器。

    生产实现是 ``secrets.token_hex``，因此两次运行的内存主键不同；主键又
    参与若干处同分时的稳定排序（``_rank_map`` / 最终 ``ORDER BY ... id``），
    于是"同一个变异跑两次给出两个数"。基线要求**逐位可复现**，
    所以在评测进程内替换成确定性 id。

    这是"在测试里固定随机源"，不改仓库行为：被替换的是评测进程的内存符号，
    跑完即随进程消失。
    """
    import itertools

    from ling.infra import security as security_mod

    counter = itertools.count(1)

    def deterministic_new_id(prefix: str) -> str:
        return f"{prefix}_eval{next(counter):08d}"

    security_mod.new_id = deterministic_new_id

    # memory 域在 import 时按值绑定了 new_id，必须逐个模块改绑定；
    # 只改 security.new_id 不会影响已经 from-import 的模块。
    from ling.domains import awareness as awareness_pkg  # noqa: F401
    from ling.domains.identity import service as identity_mod
    from ling.domains.memory import core as core_mod
    from ling.domains.memory import entities as entities_mod
    from ling.domains.memory import service as service_mod

    for module in (service_mod, core_mod, entities_mod, identity_mod):
        if hasattr(module, "new_id"):
            module.new_id = deterministic_new_id

    from ling.domains.awareness import service as awareness_mod

    if hasattr(awareness_mod, "new_id"):
        awareness_mod.new_id = deterministic_new_id


# ---------------------------------------------------------------- 数据库


async def _prepare_schema(schema: str) -> None:
    """建一个干净的独立 schema（先 drop 再建），只碰这一个。"""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(DEFAULT_DB_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    await engine.dispose()


async def _drop_schema(schema: str) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(DEFAULT_DB_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await engine.dispose()


async def _open_session_factory(schema: str):  # noqa: ANN202
    """在指定 schema 里建表并返回 session 工厂。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from ling.infra.db import create_all_for_dev

    # 这里不构造 Settings：schema 隔离靠下面的 connect_args.search_path，
    # create_all_for_dev 只吃 engine。构造一个不用它的 Settings 只会让读者
    # 以为隔离依赖它（ruff F841 就是替读者发现的）。
    engine = create_async_engine(
        DEFAULT_DB_URL,
        future=True,
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": schema}},
    )
    await create_all_for_dev(engine)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------- 语料写入


async def seed_corpus(spec: dict, session) -> dict[str, str]:  # noqa: ANN001
    """把合成语料写进库，返回 语料 key → 库内 id 的映射。

    **必须走真实的写入路径**（MemoryService.propose / record_episode），
    不能直接 INSERT：否则测的是"另一套写入逻辑"，护栏就骗人了。
    """
    from ling.contracts.v1 import MemoryProposal, Origin, Visibility
    from ling.domains.memory.entities import EntityService
    from ling.domains.memory.service import MemoryService

    corpus = spec["corpus"]
    now = eval_clock(spec)
    subject_id = corpus["subjects"]["alpha"]
    windows = {name: w["id"] for name, w in corpus["windows"].items()}
    key_to_id: dict[str, str] = {}

    # 先落 Subject（IdentityService.subject_group 会查它；不存在也不致命，
    # 但缺行会让"合并链"路径走另一条分支，测出来的不是生产形状）
    from ling.infra.models import Subject

    session.add(Subject(id=subject_id, display_name="评测用合成用户", proof_level="password"))
    await session.flush()

    service = MemoryService(session, embedder=None, reranker=None)

    for index, item in enumerate(corpus.get("memories") or []):
        window_name = item.get("source_window")
        visibility = Visibility(_visibility(item.get("visibility", "subject_private")))
        source_visibility = _visibility(item.get("source_visibility", visibility.value))
        proposal = MemoryProposal(
            proposal_id=f"probe-{index}-{item['key']}",
            kind="assertion",
            text=item["text"],
            origin=Origin.user_statement,
            source_event_ids=[f"evt-eval-{index}"],
            visibility=visibility,
            confidence=float(item.get("confidence", 0.8)),
        )
        memory_id = await service.propose(
            proposal,
            subject_id=subject_id,
            source_window_id=windows.get(window_name) if window_name else None,
            source_visibility=source_visibility,
            turn_id=f"turn-eval-{index}",
            slot=item.get("slot"),
        )
        if memory_id:
            key_to_id[item["key"]] = memory_id
            await _backdate(
                session, memory_id, now, int(item.get("age_days", 0)), tick=_next_tick()
            )

    # 历史事实（已失效的旧值）：写入时带 valid_until，并把 created_at 拨旧
    for index, item in enumerate(corpus.get("historical") or []):
        proposal = MemoryProposal(
            proposal_id=f"probe-hist-{index}-{item['key']}",
            kind="assertion",
            text=item["text"],
            origin=Origin.user_statement,
            source_event_ids=[f"evt-eval-hist-{index}"],
            visibility=Visibility.subject_private,
            confidence=float(item.get("confidence", 0.8)),
            valid_until=now + timedelta(days=int(item.get("valid_until_offset_days", 0))),
        )
        memory_id = await service.propose(
            proposal,
            subject_id=subject_id,
            turn_id=f"turn-eval-hist-{index}",
            slot=item.get("slot"),
        )
        if memory_id:
            key_to_id[item["key"]] = memory_id
            await _backdate(
                session, memory_id, now, int(item.get("age_days", 0)), tick=_next_tick()
            )

    for index, item in enumerate(corpus.get("episodes") or []):
        window = corpus["windows"][item["window"]]
        episode_id = await service.record_episode(
            subject_id=subject_id,
            window_id=window["id"],
            summary=item["summary"],
            source_event_ids=[f"evt-eval-ep-{index}"],
            origin=Origin.user_statement,
            visibility=Visibility(_visibility(item.get("visibility", "subject_private"))),
            occurred_at=now - timedelta(days=int(item.get("occurred_age_days", 0))),
            source_visibility=_visibility(
                item.get("source_visibility", item.get("visibility", "subject_private"))
            ),
            turn_id=f"turn-eval-ep-{index}",
        )
        key_to_id[item["key"]] = episode_id

    # 实体与关联：实体路（entity）是"我妈提过的那个人"能串起来的关键
    entities = EntityService(session)
    for item in corpus.get("entities") or []:
        row = await entities.upsert(
            subject_id=subject_id,
            name=item["name"],
            kind=item.get("kind", "other"),
            aliases=list(item.get("aliases") or []),
        )
        for linked_key in item.get("links") or []:
            memory_id = key_to_id.get(linked_key)
            if memory_id:
                await entities.link(memory_id=memory_id, entity_id=row.id)

    # 噪声 filler：**按固定 seed 的顺序插入**，且插入时间分散在近期。
    # 数量要明显大于变异后的候选池上界，否则"池子砍小"测不出来。
    filler = corpus.get("filler") or {}
    count = int(filler.get("count", 0))
    low, high = filler.get("age_days_range", [1, 12])
    template = filler.get("template", "用户提过编号 {n:02d} 的琐事")
    for n in range(count):
        age = low + (n * (high - low)) // max(1, count - 1)
        proposal = MemoryProposal(
            proposal_id=f"filler-{n}",
            kind="assertion",
            text=template.format(n=n),
            origin=Origin.user_statement,
            source_event_ids=[f"evt-filler-{n}"],
            visibility=Visibility.subject_private,
            confidence=0.5 + ((n % 5) * 0.05),
        )
        memory_id = await service.propose(
            proposal,
            subject_id=subject_id,
            turn_id=f"turn-filler-{n}",
            slot=f"{filler.get('prefix', 'filler')}_{n:02d}",
        )
        if memory_id:
            key_to_id[f"filler_{n:02d}"] = memory_id
            await _backdate(session, memory_id, now, age, tick=_next_tick())

    await session.flush()
    return key_to_id


async def _backdate(
    session,  # noqa: ANN001
    memory_id: str,
    now: datetime,
    age_days: int,
    tick: int,
) -> None:
    """把一条记忆的 created_at / updated_at 拨到 now - age_days（再按 tick 错开）。

    直接 UPDATE 而不是 sleep：探针要的是"新旧分布"，不是真的等 600 天。
    这不算"绕过写入路径"——写入已经发生过，这里只调整时间戳这一列事实。

    ``tick`` 按**分钟**叠加，保证任意两行的 updated_at 不相等。
    为什么较真到分钟：召回的候选池是 ``ORDER BY updated_at DESC LIMIT n``，
    一旦出现并列，边界取谁就由 ``id`` 的字典序决定。第一版变异实验因此
    跑出 0.3226 / 0.2903 两个不同的数——**不可复现的基线比没有基线更危险**，
    它会让人把"这次换了个数"误读成"这次没退化"。
    """
    from sqlalchemy import update

    from ling.infra.models import MemoryRow

    stamp = now - timedelta(days=age_days, minutes=tick)
    await session.execute(
        update(MemoryRow)
        .where(MemoryRow.id == memory_id)
        .values(created_at=stamp, updated_at=stamp)
    )


# ---------------------------------------------------------------- 探针执行


async def _audience_for(session, *, subject_id: str, window: dict) -> object:  # noqa: ANN001
    """按窗口构造 AudienceSnapshot（与 kernel/context.py 同一条路径）。"""
    from ling.kernel.audience import AudienceSnapshot

    return await AudienceSnapshot.freeze(
        session,
        subject_id=subject_id,
        window_id=window["id"],
        window_visibility=window["visibility"],
    )


async def run_probes(spec: dict, session, key_to_id: dict[str, str]) -> list[ProbeOutcome]:  # noqa: ANN001
    from ling.contracts.v1 import Visibility
    from ling.domains.memory.service import MemoryService, wants_history
    from ling.kernel.audience import visibility_set

    corpus = spec["corpus"]
    subject_id = corpus["subjects"]["alpha"]
    now = eval_clock(spec)
    k = int(spec.get("k", 6))
    id_to_key = {value: key for key, value in key_to_id.items()}
    outcomes: list[ProbeOutcome] = []

    # 词条层是否存在：决定 alias 探针是 skip 还是真跑
    glossary_available = _glossary_available()

    for probe in spec["probes"]:
        skip_if = probe.get("skip_if") or {}
        if skip_if.get("capability") == "glossary" and not glossary_available:
            outcomes.append(
                ProbeOutcome(
                    probe_id=probe["id"],
                    type=probe["type"],
                    status="skip",
                    expect=list(probe.get("expect") or []),
                    forbid=list(probe.get("forbid") or []),
                    forbid_all=bool(probe.get("forbid_all")),
                    note="词条层未上线（skip，不计入通过）",
                )
            )
            continue

        window = corpus["windows"][probe.get("window", "dm")]
        mode = probe.get("mode", "per_window")
        if mode != "per_window":
            # sealed / continuous 用同一套冻结逻辑，模式来自已发布的策略行。
            # 探针需要非默认模式时显式写进库，避免"测了但模式没生效"。
            await _set_isolation(session, subject_id, mode)

        try:
            snapshot = await _audience_for(session, subject_id=subject_id, window=window)
            allowed = visibility_set(snapshot)
            service = MemoryService(session, embedder=None, reranker=None)
            result = await service.recall(
                subject_id=subject_id,
                query=probe["query"],
                visibility_allow=allowed,
                limit=k,
                now=now,
                include_history=bool(probe.get("history")) or wants_history(probe["query"]),
                source_filter=(
                    # 显式绑定 snapshot：与 context.py 的 lambda 等价，
                    # 但不依赖"循环变量恰好没被改写"这个隐含前提。
                    lambda r, _snap=snapshot: (
                        _snap.memory_row_visible(r)
                        if hasattr(r, "kind")
                        else _snap.episode_visible(r)
                    )
                ),
            )
            hits = [id_to_key.get(h.memory_id, h.memory_id) for h in result.hits]
            outcomes.append(
                ProbeOutcome(
                    probe_id=probe["id"],
                    type=probe["type"],
                    status="ok",
                    hits=hits,
                    expect=list(probe.get("expect") or []),
                    forbid=list(probe.get("forbid") or []),
                    forbid_all=bool(probe.get("forbid_all")),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单条探针故障不能拖垮整份基线
            outcomes.append(
                ProbeOutcome(
                    probe_id=probe["id"],
                    type=probe["type"],
                    status="error",
                    expect=list(probe.get("expect") or []),
                    forbid=list(probe.get("forbid") or []),
                    forbid_all=bool(probe.get("forbid_all")),
                    note=f"{type(exc).__name__}: {exc}",
                )
            )
        finally:
            if mode != "per_window":
                await _set_isolation(session, subject_id, "per_window")

    # 防御：visibility 变量在闭包里被用到，显式引用一次避免误报未使用
    _ = Visibility
    return outcomes


async def _set_isolation(session, subject_id: str, mode: str) -> None:  # noqa: ANN001
    from ling.domains.awareness.service import AwarenessService, IsolationMode

    await AwarenessService(session).set_mode(subject_id, IsolationMode(mode))


def _glossary_available() -> bool:
    """词条层是否已上线。

    现在仓库里没有 glossary/词条模块（`grep -rn glossary src/ling` 为空），
    所以黑话探针一律 skip。**不硬编码 False**：将来模块落地，这里自动变真，
    探针自动开始真跑——否则护栏会永远漏测那一类。
    """
    try:
        import importlib.util

        return importlib.util.find_spec("ling.domains.glossary") is not None
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 指标汇总


def summarize(outcomes: list[ProbeOutcome], k: int) -> dict:
    ok = [o for o in outcomes if o.status == "ok"]
    skipped = [o for o in outcomes if o.status == "skip"]
    errored = [o for o in outcomes if o.status == "error"]

    def _mean(values: list[float]) -> float:
        vals = [v for v in values if not math.isnan(v)]
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    recallable = [o for o in ok if o.expect]
    metrics = {
        "k": k,
        "probes_total": len(outcomes),
        "probes_ok": len(ok),
        "probes_skip": len(skipped),
        "probes_error": len(errored),
        "recall_at_k": _mean([o.recall_at_k(k) for o in recallable]),
        "ndcg_at_k": _mean([o.ndcg_at_k(k) for o in recallable]),
        "mrr": _mean([o.mrr() for o in recallable]),
        # 空结果率：只看"有期望"的探针。不可召回探针本来就该空，
        # 把它算进分母会让"到处返回空"看起来像达标。
        "empty_rate": round(sum(1 for o in recallable if o.is_empty()) / len(recallable), 4)
        if recallable
        else 0.0,
        "privacy_hits": sum(o.privacy_hits() for o in ok),
        "unrecallable_hits": sum(len(o.hits) for o in ok if o.forbid_all),
    }

    by_type: dict[str, dict] = {}
    grouped: dict[str, list[ProbeOutcome]] = defaultdict(list)
    for o in ok:
        grouped[o.type].append(o)
    for ptype, items in sorted(grouped.items()):
        with_expect = [o for o in items if o.expect]
        by_type[ptype] = {
            "count": len(items),
            "recall_at_k": _mean([o.recall_at_k(k) for o in with_expect]),
            "ndcg_at_k": _mean([o.ndcg_at_k(k) for o in with_expect]),
            "mrr": _mean([o.mrr() for o in with_expect]),
            "empty_rate": round(sum(1 for o in with_expect if o.is_empty()) / len(with_expect), 4)
            if with_expect
            else 0.0,
            "privacy_hits": sum(o.privacy_hits() for o in items),
            "unrecallable_hits": sum(len(o.hits) for o in items if o.forbid_all),
        }
    metrics["by_type"] = by_type
    return metrics


def baseline_document(spec: dict, metrics: dict, outcomes: list[ProbeOutcome]) -> dict:
    return {
        "probes_file": PROBES_PATH.name,
        "seed": spec["seed"],
        "eval_now": str(spec["eval_now"]),
        "schema": spec["schema"],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "guardrail": GUARDRAIL,
        "metrics": metrics,
        # 逐条结果也存下来：退化时能立刻看出"是哪几条探针掉了"，
        # 而不是只有一个总分让人猜。
        "probes": [
            {
                "id": o.probe_id,
                "type": o.type,
                "status": o.status,
                "hits": o.hits,
                "expect": o.expect,
                "leaked": o.leaked(),
                "note": o.note,
            }
            for o in outcomes
        ],
    }


# ---------------------------------------------------------------- 比对


def compare(current: dict, baseline: dict) -> tuple[list[str], list[str]]:
    """返回 (红线失败项, 参考警告项)。"""
    failures: list[str] = []
    warnings: list[str] = []
    cur, base = current["metrics"], baseline["metrics"]
    limits = baseline.get("guardrail", GUARDRAIL)

    drop = base["recall_at_k"] - cur["recall_at_k"]
    if drop > limits["recall_at_6_drop_abs"]:
        failures.append(
            f"recall@{cur['k']} 下降 {drop:.4f}（绝对），"
            f"超过红线 {limits['recall_at_6_drop_abs']}"
            f"（基线 {base['recall_at_k']:.4f} → 当前 {cur['recall_at_k']:.4f}）"
        )
    if cur["privacy_hits"] > limits["privacy_hits_max"]:
        failures.append(
            f"隐私命中 {cur['privacy_hits']} 次（红线：必须 {limits['privacy_hits_max']}）"
        )
    empty_rise = cur["empty_rate"] - base["empty_rate"]
    if empty_rise > limits["empty_rate_rise_abs"]:
        failures.append(
            f"空结果率上升 {empty_rise:.4f}（绝对），"
            f"超过红线 {limits['empty_rate_rise_abs']}"
            f"（基线 {base['empty_rate']:.4f} → 当前 {cur['empty_rate']:.4f}）"
        )
    if cur["unrecallable_hits"] > 0:
        failures.append(
            f"不可召回项命中 {cur['unrecallable_hits']} 次（库里没有的东西被召回了=乱编风险）"
        )

    ndcg_drop = base["ndcg_at_k"] - cur["ndcg_at_k"]
    if ndcg_drop > limits["ndcg_at_6_drop_abs"]:
        warnings.append(f"nDCG@{cur['k']} 下降 {ndcg_drop:.4f}（参考项，未判红）")
    mrr_drop = base["mrr"] - cur["mrr"]
    if mrr_drop > limits["mrr_drop_abs"]:
        warnings.append(f"MRR 下降 {mrr_drop:.4f}（参考项，未判红）")

    # 逐条退化：基线里能召回、现在召回不到的探针（最有用的一条线索）
    base_map = {p["id"]: p for p in baseline.get("probes", [])}
    for probe in current["probes"]:
        old = base_map.get(probe["id"])
        if not old or probe["status"] != "ok" or old["status"] != "ok":
            continue
        if old["expect"] and not old["leaked"]:
            was = sum(1 for key in old["expect"] if key in old["hits"])
            now_hits = sum(1 for key in probe["expect"] if key in probe["hits"])
            if was > 0 and now_hits == 0:
                warnings.append(
                    f"探针 {probe['id']}（{probe['type']}）从命中 {old['hits'][:3]} 退化为零命中"
                )
    return failures, warnings


# ---------------------------------------------------------------- 主流程


async def run(spec: dict) -> tuple[dict, list[ProbeOutcome]]:
    schema = spec["schema"]
    install_deterministic_ids()
    _counter["tick"] = 0
    await _prepare_schema(schema)
    engine, factory = await _open_session_factory(schema)
    try:
        async with factory() as session:
            key_to_id = await seed_corpus(spec, session)
            await session.commit()
            outcomes = await run_probes(spec, session, key_to_id)
            await session.commit()
    finally:
        await engine.dispose()
        await _drop_schema(schema)
    return summarize(outcomes, int(spec.get("k", 6))), outcomes


def _print_metrics(metrics: dict) -> None:
    print(f"  recall@{metrics['k']}   {metrics['recall_at_k']:.4f}")
    print(f"  nDCG@{metrics['k']}     {metrics['ndcg_at_k']:.4f}")
    print(f"  MRR            {metrics['mrr']:.4f}")
    print(f"  empty_rate     {metrics['empty_rate']:.4f}")
    print(f"  privacy_hits   {metrics['privacy_hits']}")
    print(f"  unrecallable   {metrics['unrecallable_hits']}")
    print(
        f"  探针 ok/skip/error = {metrics['probes_ok']}/{metrics['probes_skip']}"
        f"/{metrics['probes_error']}"
    )
    print("  按类型：")
    for ptype, row in metrics["by_type"].items():
        print(
            f"    {ptype:12s} n={row['count']:2d} recall={row['recall_at_k']:.4f} "
            f"ndcg={row['ndcg_at_k']:.4f} mrr={row['mrr']:.4f} "
            f"empty={row['empty_rate']:.4f} leak={row['privacy_hits']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索质量基线（准确性护栏）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write-baseline", action="store_true", help="写 baseline.json")
    group.add_argument("--compare", action="store_true", help="与 baseline.json 比对")
    parser.add_argument("--json", action="store_true", help="把当前指标打到 stdout（JSON）")
    parser.add_argument("--keep-schema", action="store_true", help="跑完保留 schema（排障用）")
    args = parser.parse_args(argv)

    spec = load_spec()
    print(f"[eval] probes={PROBES_PATH}  schema={spec['schema']}  k={spec.get('k', 6)}")
    print(f"[eval] db={DEFAULT_DB_URL}")

    try:
        metrics, outcomes = asyncio.run(run(spec))
    except Exception as exc:  # noqa: BLE001
        print(f"[eval] 运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    document = baseline_document(spec, metrics, outcomes)
    print("[eval] 当前指标：")
    _print_metrics(metrics)

    if args.json:
        print(json.dumps(document["metrics"], ensure_ascii=False, indent=2))

    if args.write_baseline:
        BASELINE_PATH.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"[eval] 已写入基线 {BASELINE_PATH}")
        return 0

    if args.compare:
        if not BASELINE_PATH.exists():
            print(f"[eval] 没有基线文件 {BASELINE_PATH}，先跑 --write-baseline", file=sys.stderr)
            return 2
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        failures, warnings = compare(document, baseline)
        if warnings:
            print("[eval] 参考警告：")
            for item in warnings:
                print(f"  - {item}")
        if failures:
            print("[eval] 红线失败：")
            for item in failures:
                print(f"  ✗ {item}")
            return 1
        print("[eval] 护栏通过：准确性红线未触发")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
