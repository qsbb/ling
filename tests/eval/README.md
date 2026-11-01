# tests/eval — 检索质量基线（提速护栏的"准确性"那一半）

> 前情：`tests/eval/` 此前**不存在**。也就是说，在这个目录出现之前，仓库里
> 没有任何东西能证明"某次提速改动没有降低召回质量"。**"变快了"和"变差了"
> 无法区分**，任何"优化"都只能靠读代码说服自己。
>
> 这个目录就是那个缺失的证据层。

## 一句话

```
改动前：python tests/eval/probe_baseline.py --write-baseline   # 存基线
改动后：python tests/eval/probe_baseline.py --compare          # 非零退出 = 红了
```

判定阈值与"哪些优化项该跑哪些探针"见
[`shots/ia-research/SPEEDUP-GUARDRAILS.md`](../../shots/ia-research/SPEEDUP-GUARDRAILS.md)。

## 文件

| 文件 | 作用 |
|---|---|
| `probes.yaml` | 44 条探针 + 固定 seed 合成语料（六类全覆盖）。**唯一事实源** |
| `probe_baseline.py` | 跑探针 → 算指标 → 写/比对基线 JSON。只测量，不内置业务判断 |
| `baseline.json` | 当前基线（落盘的数字，供 `--compare` 用） |
| `README.md` | 本文件 |

## 六类探针（为什么必须都有）

| 类型 | 条数 | 它在防什么 |
|---|---:|---|
| `fact` 事实召回 | 12 | 最基础：问法贴近原句必须召回。掉这里=改动伤了根本 |
| `paraphrase` 同义改写 | 8 | 换说法仍能召回。掉这里=退化成了纯字面匹配 |
| `temporal` 时间/关系 | 5 | "我们第一次见面"靠**情节路径**；实体路靠"我妈提过的那个人" |
| `unrecallable` 不可召回 | 6 | 库里没有的必须返回空。**这一类的命中=乱编风险** |
| `privacy` 隐私隔离 | 9 | 群 A 不进群 B。**命中恒须为 0**，且配同组正向对照防"全空假绿" |
| `alias` 黑话/别名 | 4 | 别名归并（妈/母亲/老妈）+ 词条层未上线时 `skip` |

共 44 条：43 条真跑、1 条 `skip`（黑话需词条层，仓库里没有
`ling.domains.glossary`，脚本按 `find_spec` 自动 skip，**不冒充通过**）。

## 指标口径（全部 k=6，与 `kernel/context.py` 的 `limit=6` 对齐）

- **`recall@6`** — expect 里有多少比例进了前 6（微平均）
- **`nDCG@6`** — 命中位置越靠前越高
- **`MRR`** — 第一个相关命中的倒数名次
- **`empty_rate`** — 有期望的探针里返回**零条**的比例（只在有 expect 的探针上算；
  不可召回探针本来就该空，算进分母会让"到处返回空"看起来像达标）
- **`privacy_hits`** — 隐私探针里 forbidden 内容出现次数。**红线：必须 0**
- **`unrecallable_hits`** — 不可召回探针里的任何命中数。**红线：必须 0**

## 当前基线（2026-11-01，真跑）

| 指标 | 值 |
|---|---|
| `recall@6` | **1.0000** |
| `nDCG@6` | **0.9881** |
| `MRR` | **0.9839** |
| `empty_rate` | **0.0000** |
| `privacy_hits` | **0** |
| `unrecallable_hits` | **0** |
| 探针 | 44 条（ok 43 / skip 1 / error 0） |

按类型：`fact` / `paraphrase` / `privacy` / `alias` 全项 1.0；`temporal`
recall 1.0 但 nDCG 0.9262（"以前住哪"里旧值 `old_town` 排第 2、当前值第 1
——这是**已知的、可接受的**行为：问"现在"时旧值不该压过现值，
`time-current-town` 另有探针把这个方向钉住）。

## 可复现性（不是可选项）

基线必须逐位可复现，否则"这次换了个数"会被误读成"这次没退化"。
三条保证：

1. **固定评测时钟** `eval_now: 2026-01-01T00:00:00Z` —— 所有
   `created_at` / `occurred_at` / `valid_until` 都相对它，不相对"现在"。
2. **确定性 id** —— 生产 `new_id()` 是 `secrets.token_hex`，随机主键会参与
   同分时的稳定排序。脚本在评测进程内替换成递增生成器
   （`install_deterministic_ids()`，跑完随进程消失，不改仓库行为）。
3. **时间戳分钟级错开** —— 候选池是 `ORDER BY updated_at DESC LIMIT n`，
   有并列时边界取谁由 id 决定。`_backdate(..., tick=)` 让任意两行不相等。

> **这条是实测踩出来的**：第一版变异实验跑出 `0.3226` 和 `0.2903` 两个不同的
> 召回率。修完上述三点后连跑两次均为 `0.3226`。

## 数据与隔离

- **真 PostgreSQL**（`127.0.0.1:55432/ling_test`），不用 sqlite ——
  sqlite 的分词/排序是另一套数字，用它测出的"没退化"不算证据。
- **独立 schema**（`probes.yaml` 的 `schema:` 字段，默认 `eval_probe_baseline`），
  每次运行先 `DROP SCHEMA IF EXISTS ... CASCADE` 再 `CREATE`，
  **只碰这一个 schema**，不碰 `ling` / `ling_test` 里已有的表。
- 可用 `LING_EVAL_DATABASE_URL` 覆盖库地址。
- **合成语料**：所有文本都是编的（"阿澄""团子""青梧里"…），但
  `subject / window / visibility / source_window_id / slot` 的关系与真实写入
  完全同形。探针与语料里**没有**任何真实人名、群号、聊天原句。
- 语料走**真实写入路径**（`MemoryService.propose` / `record_episode` /
  `EntityService.upsert`），不直接 INSERT —— 否则测的是另一套写入逻辑。
- 噪声 filler 60 条，刻意**多于**变异后的候选池上界（50），否则
  "把候选池 500 砍到 50"这种退化在探针上根本看不见。

## 用法

```bash
# 建/更新基线
.venv/bin/python tests/eval/probe_baseline.py --write-baseline

# 回归比对（红线失败 → 退出码 1）
.venv/bin/python tests/eval/probe_baseline.py --compare

# 只看数字，不落盘（--json 追加机器可读指标）
.venv/bin/python tests/eval/probe_baseline.py --json
```

`--compare` 的退出码：`0` 通过、`1` 红线失败、`2` 环境/基线缺失。
报告里除红线外还列**参考警告**（nDCG/MRR 下降、以及逐条"从命中退化为零命中"
的探针 id）——退化时能立刻看出是哪几条探针掉了，而不是只有一个总分让人猜。

## 加/改探针

1. 改 `probes.yaml`（语料与探针在同一个文件，改一处即可）。
2. **必须重跑 `--write-baseline`**：探针 id 是基线 JSON 的键，改名等于新建基线。
3. 新语料文本要和探针查询保持"该重合的重合、不该重合的零重合"：
   - 事实/改写类：查询与目标记忆要有词面交点（2-gram），否则测的是切词缺陷；
   - `unrecallable` 类：查询与**全语料**必须零 2-gram 交点，否则那 1 个脏
     命中会让红线永远红（第一版踩过：`同事的工号` 撞上 `的工作`）。
4. 隐私类写成 `expect: []` + `forbid: [...]`，并配一条**同组正向对照**
   （同一句话在它自己的窗口必须召回）。**不要**同时把同一项写进 expect 和
   forbid —— 自相矛盾的探针会把 recall 和泄漏数同时算错。

## 边界（这份基线测不到什么）

- **只测检索层**（`MemoryService.recall` 的输出），不测生成质量。
- 只覆盖**无 embedding / 无 rerank** 的配置（`embedder=None`，与未配置
  `LING_EMBEDDING_API_KEY` 的部署一致）。语义路的退化需要另建一套基线。
- 语料规模是"个人助理"量级（约 80 条记忆），不是压力测试；
  逐条延迟由 `shots/ia-research/SPEEDUP-GUARDRAILS.md` 规定的方式另测。
