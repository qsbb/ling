# 记忆检索候选入口：从覆盖证明出发

状态：设计稿；本文不改生产实现。范围仅为断言与情节的**候选生成**及其必需的索引、打分口径。每轮返回数、相对下限和保底返回行为另议。

## 1. 问题的定义

给定本轮查询 `q`、主体合并组、受众快照、时间 `now`、历史开关，先定义所有**有权且有效**的行集合 `E(q)`，再按本轮可用信号定义参与排序的 `D(q) ⊆ E(q)`：无 embedder 时断言须字面或实体命中、情节须字面命中；有 embedder 时 `D(q)=E(q)`。候选集 `C(q) ⊆ D(q)` 的职责是让后续排序、近重复抑制和可选重排看见可能进入最终结果的行。它的质量判据不是“看了多少行”，而是：对每个未进入 `C` 的行，能否给出其最高可能分数的上界，并证明它不可能胜过已选中的第 `k` 条。做不到时应明示“近似”，不能把 `LIMIT` 叫作覆盖保证。

现状在 `src/ling/domains/memory/service.py:582-603` 先取 `updated_at DESC, id ASC LIMIT 500`，才在 `:643-745` 算 BM25、向量、实体、时间、重要性及 RRF；上限定义在 `:296-298`。因此第 501 条及更早的行对全部检索信号不可见。已确认的 500/501 对照实验中，旧的“最喜欢紫色”从命中变为 `hits=[]`，唯一变量是总条数。这不是调大 500 能解决的性质：任何固定的“最新 N 条”都可能在第 N+1 条遗漏最高相关行。`updated_at` 也不是纯内容编辑时间：`src/ling/infra/models.py:48-52` 有 `onupdate`，`service.py:1439-1476` 的强化和召回均执行 UPDATE；同一字段已混入使用行为。故**按时间截断本身就是错误的候选入口**。

排序和入口还须分开：时间可以在相关候选内部影响次序，但不能独自决定一条旧事实是否有资格被比较。既有 `last_recalled_at`、`last_injected_at`、`access_count` 位于 `src/ling/infra/models.py:1226-1235`，已能表达“被想起/实际注入”，不必为本设计再造一个“最近提及”字段；这些使用信号同样不等于对当前查询的相关性。

## 2. 可证明覆盖的边界

本设计的证明对象先限定为**重排器调用前**的既有 RRF 顺序及断言近重复抑制。现有融合按参与路的 `1/(60+rank)` 取平均，见 `src/ling/domains/memory/service.py:201-218,715-745`。设各可参与路 `i` 已完整取到名次不大于 `b_i` 的所有行（**含同分**），且这些名次在同一个 `D(q)` 上计算。对不在各路并集的任一行 `x`，其每路名次都大于 `b_i`；所以

```text
RRF(x) ≤ U = max_i 1 / (60 + b_i + 1)
```

RRF 是正项的平均，上式即使实体路只覆盖部分行、无 embedder 时没有向量路也成立。若候选中按同一全局名次算出的第 `k` 个**通过近重复抑制**的分数严格大于 `U`，且同分边界已按既有 `id` 规则处理，就能证明池外行排不到前 `k`：它们只可能在已保留的 `k` 条之后进入贪心去重。若不足 `k` 条、分数相等、某路名次/同分组不完整，继续扩张或走精确回退。证明的是检索分数顺序，不是“模型会认为相关”的语义保证。

这个界要求**各路是全局可排序的单调检索器**，返回完整 top-M 和下一名界限。字面命中可以通过倒排表算精确 BM25；实体关联可直接查询，但现有实体命中共享名次（`service.py:657-673`），极大同分组可能让有界证明失败；时间和重要性可以用有序索引或数据库排序。向量若用近似 ANN，只能得到经验召回，不能声称 top-M 或上述上界；当前向量是 JSON，且只对传入行计算余弦（`src/ling/infra/models.py:959-985`；`service.py:1258-1283`），也不能证明池外不存在更近向量。

更关键的是，**候选内重算名次不等于全局名次**。现有 `_bm25_scores` 用传入语料重算 `N/df/平均长度`（`service.py:84-127`），`_rank_map` 也只在传入行上排（`:206-218`）。落地时必须让候选器和后续 RRF 共用 `D(q)` 上的 BM25 统计与路名次；否则虽然有多路并集，仍无覆盖证明。这会改变现有“最近 500 条内的 BM25”分数口径，须用探针实测回归，不能伪称只换 SQL 就完全等价。

可选重排器是另一个边界：它只处理选出的头部，且可任意重排（`service.py:1394-1435`），无法由 RRF 上界证明池外行不会被它提拔。若需要对**重排后**结果的严格保证，必须对所有可行行重排，或取得重排器可证明的分数上界；目前两者都没有。本方案只承诺重排前覆盖，并用标注集评估重排后的实际质量。

## 3. 候选方案与配额推导

### 3.1 先确定可检索域

同一主体合并组使用 `subject_id IN subjects`，先构造 `E(q)`。断言排除撤回、归档、无权可见、来源不符、已过 `valid_until`（非历史查询）和过保留期；情节排除归档、无权可见、来源不符和过保留期。现有断言过滤在 `service.py:586-633`，情节在 `:772-794`；真实受众规则在 `src/ling/kernel/audience.py:66-120`，保留期计算在 `service.py:1116-1143`。**先过滤再按各路取头部**，否则无权行可占用配额，甚至让“已证明覆盖”失效。取回完整行后仍重复执行既有 Python 硬门，作为防泄漏的第二道校验。

`source_filter` 当前是任意 Python 回调（`service.py:559-560,617-622`），数据库不能从回调推导权限条件。生产调用增加可编译的冻结受众 `retrieval_scope`（主体组、可见范围、群/私聊、来源窗口、策略修订），由 `ContextCompiler` 的 `AudienceSnapshot` 生成；保留 `source_filter` 做最终复验。若调用方只有任意回调，**不能声称 SQL 已精确过滤**：小库走全域精确路径；大库须回退并标记“未认证”，不得把被回调拒绝的行算入覆盖证明。撤权/身份变化与索引刷新须处在一致快照内，不能以陈旧索引结果绕过读时硬门。

### 3.2 各路并集

| 记忆类型 | 入口路 | 为什么需要；何时参与 |
| --- | --- | --- |
| 断言 | 精确字面 BM25 | 对查询词有交集的旧事实必须能从倒排表进入；零交集不冒充字面命中。 |
| 断言 | 实体关联 | 别名解析及 `memory_entities` 关联可找到字面零交集行；当前只对已取到的断言标实体命中（`service.py:657-673`），需改为直接从关联取 ID。`EntityService.resolve` 目前只接收单个 `subject_id`（`src/ling/domains/memory/entities.py:104-127`），主体合并组也须覆盖实体解析。 |
| 断言 | 精确向量 top-M（配置且索引完整时） | 处理无共同词的改写；当前 JSON 向量没有全域 top-M 能力，缺向量行不能悄悄当作已覆盖。 |
| 断言 | `created_at` 新近度、`confidence × (1+log1p(access_count))` 重要性 | **仅为保持现有 RRF 可证明覆盖而纳入各路头部**，不使用 `updated_at`；无 embedder 时只在字面/实体已命中的域上排，不能把全库无关新行带进来。依据 `service.py:677-714`。 |
| 情节 | 精确字面、精确向量（如可用）、`occurred_at` 新近度 | 当前情节融合恰为这些路（`service.py:795-835`）。时间路只在可检索域中排；无 embedder 时域限定为字面命中。 |

没有 embedder 时，断言域是“BM25 > 0 **或**实体关联”，情节域是“BM25 > 0”；这是现有准入条件（`service.py:677-687,798-803`）。有 embedder 时域是全部有权有效行，但要先确认模型版本相符、向量覆盖完整；缺向量、嵌入失败或索引滞后时，该路不可出具证明。实体是断言路；`memory_entities` 只有 `memory_id`，关联到情节的可靠性未在当前代码中查到，故不把它写成情节入口。

### 3.3 配额不是固定比例

业务输出 `k=6` 来自 `src/ling/kernel/context.py:629-643`，既有 RRF 常数为 60。各路的**首批配额**取 `M_i = k = 6`：若某一路独自决定前六，至少取到它自己的前六才有机会证明；少于六条没有一般性依据。之后只扩张当前上界最高、且未耗尽的路，目标配额按 `6 → 12 → 24 → 48 → 96 → …` 翻倍；每次重新计算 `U` 与第六个去重后候选的分数，达到严格不等式就停。翻倍只是减少往返次数的分页策略，不是假称“取 96 条必然安全”。若某一路无命中，其上界为 0，不占配额。

同时启用五条断言路和三条情节路时，首批最多 `8 × 6 = 48` 个 ID；只开无向量路时最多 `6 × 6 = 36` 个 ID。实际以 `(kind,id)` 去重，重复行不会吃两次内存预算。**去重后的内存候选软上限暂定 500**，来源仅是现有 `RECALL_CANDIDATE_LIMIT=500` 所代表的单轮取行预算，不是相关性定理，也不是各路固定配额之和。超过 500 仍未认证时，不丢弃池外行并假称完成：改用数据库端精确求全域名次/融合分、按分数分页取行，直到有六条通过去重或全域耗尽；若向量只能逐行 JSON 扫描，这条精确回退可能很慢，必须记录延迟并在大库上线前建设向量检索能力。任何性能预算迫使它提前停止时，结果标记 `approximate`，不可对外宣称无漏召。

时间路与重要性路各有独立页/界限，不能先把所有路的 ID 混在一个 `LIMIT 500` 里。这样近期行不会挤掉旧的字面/向量命中；反过来，最新但无关的行在无 embedder 路径也不能独自进入。实体一对多、时间衰减下限 0.5、重要性同值都可产生巨大同分组；当前 `_rank_map` 同分共享名次（`service.py:206-218`），不能随便截断同分。出现跨越预算的同分组时精确回退，或在未来单独评审“稳定唯一名次”的排序契约变更。

## 4. SQL 形态和索引

以下是接口级伪 SQL，`:scope_predicate` 必须与冻结受众规则等价，`:retention_cutoff` 用当前策略与行级保留期计算，且最终再用现有 Python 门复验。只传 ID、各路分数/名次，直到去重后才按主键批量取完整行。

```sql
WITH eligible AS NOT MATERIALIZED (
  SELECT m.id, m.created_at, m.confidence, m.access_count
  FROM memories m
  WHERE m.subject_id = ANY(:subjects)
    AND NOT m.retracted AND m.archived_at IS NULL
    AND m.visibility = ANY(:allowed)
    AND (m.valid_until IS NULL OR m.valid_until > :now OR :history)
    AND m.created_at +
        (GREATEST(1, LEAST(COALESCE(m.retention_days, :policy_days), :policy_days))
         * INTERVAL '1 day') > :now
    AND (:scope_predicate)
), rank_domain AS (
  -- :has_embedder 时全域；否则只保留有字面/实体线索的行。
  -- lexical_match_ids 从查询 token 的倒排 posting 得到，不依赖 BM25 统计。
  SELECT d.* FROM eligible d
  WHERE :has_embedder OR d.id IN (SELECT id FROM lexical_match_ids)
        OR d.id IN (SELECT memory_id FROM entity_match_ids)
), lexical AS (
  -- memory_terms(token, memory_id) 倒排；只汇总查询 token 的 posting。
  -- 以同一 rank_domain 的 N、df、avgdl 计算现有 BM25 公式。
  SELECT s.id, s.score
  FROM exact_bm25_for_domain(:query_tokens, rank_domain) AS s
  WHERE s.score > 0
  ORDER BY s.score DESC, s.id ASC
  LIMIT :lex_page
), entity AS (
  SELECT e.memory_id AS id
  FROM memory_entities e JOIN rank_domain d ON d.id = e.memory_id
  WHERE e.entity_id = ANY(:resolved_entity_ids)
  ORDER BY e.memory_id
  LIMIT :entity_page  -- 同分/完整性须另查总数，不能仅凭 LIMIT 认证
)
SELECT DISTINCT id FROM (
  SELECT id FROM lexical UNION ALL SELECT id FROM entity
  -- 向量、新近度、重要性各自分页后追加；情节用独立 eligible_ep。
) routes;
```

上述 `:scope_predicate` 表示由 `RetrievalScope` 编译进 SQL 的条件，不能作为普通布尔绑定参数使用；`lexical_match_ids`、`entity_match_ids` 与 `exact_bm25_for_domain` 都是待实现的查询部件，**不是现有 SQL 表或函数**。`eligible` 也不得无条件物化整个人的百万行；实际查询计划须分别验证倒排、实体和有序路能使用索引。`_tokenize` 实际把英文整词、删去非中文字符后串接所得的中文 2-gram 放进集合（`service.py:65-74`），`_bm25_scores` 的词频因此至多为 1（`:97-126`）；不能直接换 PostgreSQL 内置英语全文检索而声称等价。建议事务内维护 `memory_terms(memory_id, token)`、`episode_terms(episode_id, token)` 与各行 `token_count`，主键为 `(memory_id,token)` / `(episode_id,token)`，再加 `(token,memory_id)` / `(token,episode_id)` 倒排索引。写入、修订、撤回/归档与索引更新同事务；上线先回填并校验每行 token 集一致。`N/df/avgdl` 必须按本轮完整的 `D(q)` 算，不能按第一页算。若这一步在百万级过贵，需要另行评测缓存/预计算策略及权限分组；错误缓存统计会破坏精确证明。

现有索引依据：`migrations/versions/0001_baseline.py:331-351` 有主体、主体+类型及情节窗口/主体索引；`0014_memory_archive.py:39-49` 加归档索引；`0022_entities.py:42-51` 加实体关联索引；`0019_memory_embeddings.py:35-44` 建 JSON 向量和模型索引。`0062_memory_consolidate.py:65-81` 是睡眠整理表，`0063_deferred_replies.py:1-24` 是延后回复，**两者不是候选索引**。

新增索引建议：`memories(subject_id, created_at DESC, id) WHERE retracted=false AND archived_at IS NULL`、`memory_episodes(subject_id, occurred_at DESC, id) WHERE archived_at IS NULL` 支持时间路；`memories(subject_id, visibility, source_visibility, source_window_id, id)` 及相应情节组合索引在 `EXPLAIN (ANALYZE, BUFFERS)` 证实有益后建立，避免盲目堆索引；实体路复用 `ix_memory_entities_entity`，必要时扩成 `(entity_id,memory_id)`。重要性表达式索引若采用 `log1p` 须先验证数据库表达式与 Python 计算、类型及更新成本一致；否则在有权域内精确排序。向量百万级需要按 `model_id/dim/kind` 组织可查询索引：精确 top-M 可认证，ANN/HNSW 只能给近似结果，不能给覆盖证明。保留 JSON 方案可用于小库精确扫描，但不可声称百万级低延迟。

## 5. 与现有实现的接合点

未来实现时把 `recall` 中 `service.py:582-603` 的单路取数替换成 `_candidate_ids(...)`，建议签名：

```python
async def _candidate_ids(
    *, subjects: set[str], query: str, now: datetime,
    visibility_allow: set[Visibility], include_history: bool,
    retrieval_scope: RetrievalScope | None,
    k: int, embedder_model_id: str | None,
) -> CandidateBatch:  # assertion_ids, episode_ids, rank_maps, certified, reason
    ...
```

`recall(..., retrieval_scope: RetrievalScope | None = None)` 保持旧调用兼容；`src/ling/kernel/context.py:616-643` 将已冻结的 `AudienceSnapshot` 转成该结构。探针执行器将来同样从 `_audience_for` 的快照构造它（`tests/eval/probe_baseline.py:475-495`）。主体展开及清除硬门保留在 `service.py:566-580`。两种记忆都先取 ID 再批量取行，现有 Python 权限/保留期复验保留。`service.py:643-745,795-835` 改为消费全域 rank/分数，保留 RRF 常数与公式；情节 `:772-781` 的无序无上限读取一并被有界 ID 入口替换。`_vector_scores/_vector_scores_for`（`:1235-1283`）不能再只对随意截出的候选池补向量并把结果叫作语义 top-M。

本设计**不需要新增时间字段，也不改 `updated_at`**：直接停用它作为候选入口；沿用 `created_at`、`occurred_at`、`last_recalled_at`、`last_injected_at` 的既有含义。需要迁移的是倒排表/索引；仅当启用可认证的向量大库路径时才迁移向量存储/索引。若未来确需“内容最后编辑时间”，再单独新增 `content_changed_at` 并审计所有写路径，不能把现有 `updated_at` 重新解释成它。

## 6. 退化、验收与分步落地

小库（例如 10 条有效行）先用计数判断 `|D| ≤ k` 或已低于单轮预算，直接按现有完整路径读全域并精排，避免多路 SQL 往返；输出应与原路径相同。无 embedder 时按完整字面/实体域运行，向量路上界为 0，旧的高度相关字面行仍可被发现；语义改写且零字面、零实体时仍可能空，这是“没有该信号”的能力边界，不伪称解决。百万级时倒排查询与有序头部能避免把全文载入 Python，但常见词、巨大同分组、无向量索引、精确全域统计及回退可能仍随命中量线性增长；要有 p50/p95/p99、扫描行数、各路扩张次数和 `certified/approximate` 比例的容量验收。数学上不能同时承诺任意查询严格不漏、固定 500 行、百万级低延迟，除非每一路都有足够强的精确上界索引。

质量回归使用**同一份** `tests/eval/probes.yaml` 和 `tests/eval/baseline.json`，执行 `tests/eval/probe_baseline.py --compare`，不重写基线。当前 YAML 列出 44 个探针（`probes.yaml:257-523`），基线也记录 44 个、43 ok、1 skip（`baseline.json:14-25`）；“42 条”是旧口径，验收以文件实际数为准。执行器在真 PostgreSQL 独立 schema 中用固定时钟、真实写入路径、`recall(limit=k)`（`probe_baseline.py:20-35,254-305,437-496`）；`k=6`（`probes.yaml:29-30`）。红线是 recall@6 绝对下降不超过 0.02、隐私命中 0、空结果率上升不超过 0.03、不可召回命中 0（`probe_baseline.py:64-73,631-659`）。此外逐条比对命中/禁入项，并分无 embedder、嵌入完整、嵌入缺失测量，不能只看均分。

新增测试须至少覆盖：同一主体的 500/501 对照（旧紫色事实在第 501 条仍进候选和前六）；旧高相关与大量近期无关行；两个主体合并及身份拆分；群 A/群 B/私聊来源隔离与同分边界；`valid_until`、行级与策略级保留期、撤回、归档和清除待处理；实体别名零字面命中；向量零字面命中、模型版本错配和缺向量；近重复项占满前页后的扩张；1000/100 万情节的旧事件命中、取行数及延迟。用小库全量计算作 oracle，对随机语料逐查询断言 `certified=True` 时前六和近重复抑制结果相同；对未认证路径断言必须有可观测的回退/近似标记。`filtered_out` 现仅统计已取回行（`service.py:604-633,794,863-867`），不得拿它当全域过滤数。

落地顺序：① 先加 `CandidateBatch`、全域小库路径、认证/近似指标与 500/501、情节深页测试；这可独立上线，且无需新时间字段。② 建字面倒排与精确统计、实体直接取 ID、受众 SQL 条件及双重读时校验；先影子比对再切无 embedder 路径。③ 把断言与情节的时间/重要性路及动态界限接入，做全量 oracle 对照后切大库。④ 向量迁移与回填、模型版本校验；只有取得精确 top-M 的实现才打开“认证”标志，ANN 单独做质量与延迟评估。每步都能回退到上一条已验证的入口，不改每轮返回策略。

## 7. 明确不做的事与风险

本任务**不改** `ContextCompiler` 的 `limit=6`（`context.py:629-643`），也**不改** `kept or scored[:1]`（`service.py:860-869`）。独立性验收：在相同 `k=6`、相同下限/保底代码下只替换候选 ID，断言 500/501 故障消失；另测空候选依然为空，并以受控极端分数覆盖保底分支；正常非负 RRF 分数下第一名本来就会通过相对下限。保底行为可能返回不相关内容，但那是另一项设计裁决，不能把候选覆盖改善当作绝对相关性门。

不采用旧设计 `docs/design/MEMORY-REDESIGN.md:187-206` 的“最近更新 200 / 热度 150 / 字面语义 150 / 结构化 100”固定份额：这些数字无法从覆盖条件推导，且把时间继续设成入口。也不靠增加 `RECALL_CANDIDATE_LIMIT`、篡改 `updated_at`、仅用 ANN 冒充精确语义 top-M、或在权限过滤前取每路 LIMIT。已知残余风险是旧排序的 RRF、MMR 和可选重排器本身未必符合人的相关性判断；本文只保证候选阶段不因任意时间窗漏掉**可由现有检索信号发现**的行，并明确证明在哪些条件下成立。
