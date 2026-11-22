# P2 现状基线：反例场景下 Ling 实际编译出的提示词

> 目标：记录「用户问前天 → 早上好 → ？」回放时 Ling 实际编译出的提示词分段、旧内容在哪一段、是原文/摘要/记忆/跨窗哪一种。
>
> 证据强度总览：**真实编译器输出（脚本模拟回放）已取得**。本地 PostgreSQL `127.0.0.1:55432/ling_test` 可用，未连生产库或 NAS；未改 `src/` 与 `tests/`。脚本在 `/tmp`，合成数据为假数据。

## 0. 观测纪律记录（先钉住对象）

任务开始时（2026-11-22 09:25+08:00）：

```
$ git status --short
?? docs/research/

$ git log -1
commit de43cbe79a78e0c01f7821470500eaa04ea79497
Merge: 462120c a5bad48
Author: qsbb <86879267+qsbb@users.noreply.github.com>
Date:   Sun Nov 22 09:17:29 2026 +0800

    feat(live): 合入直播工作台与舞台控制
```

六个目标文件的 sha256（开始与结束时逐字节相同，期间未改被测源码与测试）：

```
a32875e6888c3ec327da36fdfb0e3c5d416bec55f53ab160ef63baf00f29b6e4  tests/integration/test_window_policy_replay.py
42d24a21f47866ffd97db6b93fac567ec6e28fec9d7790ecce7d7571b7abde99  tests/regression/golden/prompt-continuous.json
8cec250e55ec1f7359688f085d9d8aa7243c5a164430816a7285cfe2f490bee7  tests/regression/golden/prompt-group-window.json
c20d67df0e32ece78b3be29324485f635862f8d3ec1f8c4d13c0a1acd67f69e8  tests/regression/golden/prompt-native-tool.json
9615677906b05c62d5bcdbcaf9e41cd10d3268c00bd698350bc5ad2492e02d7a  tests/regression/golden/prompt-sealed.json
3caa5fff870acef7fe50d62eef0beb27675489fa7cfd109138221233c3938105  tests/regression/golden/prompt-video-marker.json
c1575350feb23b2ea1b1f8452694e07aa0c986ef76647ff00e8c94dbc9e53752  tests/regression/golden/prompt-web-private.json
d1080e34c60e3d87080fa5f95447976a5df936900d6685a601ee02a400ad024b  tests/unit/test_prompt_snapshot_payload.py
3cff00662536c2a3a896557f1950c7a7696c06ff02ead9a373e4a837a573c13c  src/ling/kernel/prompt_zones.py
ffb573b49e8fa9ba4df941317061a653f6b541d2a9c6122756c484c6399718ba  src/ling/kernel/context.py
334534fe1ea779d72976e41562b949103b4636a9dee0e8a899e7284681a94a70  src/ling/kernel/bridge.py
```

文件写入前确认 `docs/research/P2-baseline.md` **不存在**。结束时的 `git status` 多了若干 `?? docs/evidence/*`（其他 agent 并行产出），本任务未碰 `src/` 与 `tests/`。

## 1. 只读分析：如何构造回放并拿到 PromptSnapshot

### 1.1 现有设施的分工

| 设施 | 作用 | 关键位置 |
|---|---|---|
| `tests/integration/test_window_policy_replay.py` | 展示「同一 `client_turn_id` 重投 → 读已保存轮次，不重跑生成」的幂等回放骨架：`logged_in.client.post(/v1/sessions/{window}/turns, json={client_turn_id, text})`；用 `session_scope()` 直接查 `Turn` / `ConversationAttemptSnapshot` 并改状态 | `:26–88`；它回放的是**已完成的 turn**，不是重跑编译 |
| `tests/regression/test_prompt_golden.py` | **真正相关**：`compile_prompt()` 直接用 `ContextCompiler(...).compile(...)` 拿到 `compiled.messages` 并与 golden JSON 比对；`prompt_env` 固定 `env_facts._utcnow=NOW`、写入测试人格；原生工具回合用 `ScriptedProvider` + `provider.requests` 捕获发给模型的请求 | `:42–97`、`:191–261` |
| `tests/regression/golden/prompt-*.json` | 编译后发给模型的 `messages` 数组的逐字快照（6 份：private/group/continuous/sealed/video-marker/native-tool，后者是两次请求的数组）。每段只含 `role`+`content`（工具回合另有 `tool_calls`/`provider_call_id`） | 全文 |
| `tests/unit/test_prompt_snapshot_payload.py` | 纯函数层：`build_prompt_snapshot_payload(annotated)` 保留 `index/role/key/lane/content/sender/at/tokens/truncated`；`KEY_LABELS`/`KEY_ORDER`/`lane_key` 决定展示分组；256KB 截断保留 system + 尾部 | `:41–64`、`:71–106`、`:125–181` |
| `src/ling/kernel/prompt_zones.py` | 区域/车道注册表：`ZONES` 的 `legacy_ui_key` 派生 `LANE_TO_KEY`→ `lane_key()`；`summary`/`history`/`input` 是结构车道；`OBSERVED_APPEND_ORDER` 记录当前实际 append 顺序；本轮只是 PR1（登记，不改行为） | `:183–310`、`:331–333`、`:421–450` |
| `src/ling/kernel/context.py` | `ContextCompiler.compile()` 装配全部上下文；`CompiledMessage(role,content,key,lane,sender,at)`；`compiled.annotated` 与 `compiled.messages` 逐条同源；历史原文层从新往旧按 token 预算装填；摘要层 `window_ctx.summary` 无条件注入且无 TTL | `:278–360`、`:548–566`、`:1460–1536`、`:1660–1685`、`:1732–1861` |
| `src/ling/kernel/bridge.py` | 入站桥在 `run_turn` 前冻结 `RuntimeWindowSnapshot`，落 `ConversationAttemptSnapshot`（`:851–869`）；窗口按 `(actor_id, channel, connector_window_ref/title)` 复用，与话题无关；本任务无需直接用它构造回放——经 `/v1/sessions` API 建窗即可 | `:502–525`、`:851–869` |

### 1.2 三气泡回放的构造步骤（可复现配方）

1. 复用 `tests/integration/conftest.py` 的 `logged_in` 夹具：真 PostgreSQL、每个测试独立 schema（`t_<uuid>`）、httpx ASGI 客户端。
2. `new_window(logged_in, "标题")` 建一个私聊窗口（`tests/regression/chat_fixture.py:45`）。
3. 用一个 `Turn`（`create_turn(client_turn_id, config_revision)`）作种子，调 `ConversationService.add_message()` 按顺序写 4 条消息：
   `role="user"` 旧需求（例如 `产品方案四项需求：A 登录、B 导出、C 通知、D 报表`）→ `role="actor"` 确认 → `role="user"`「早上好」→ `role="actor"`「早呀。」
   - **角色必须是 `actor` 而不是 `assistant`**：`messages_for_prompt()` 只取 `role.in_(["user","actor"])`（`service.py:1330`），我第一版用 `assistant` 导致 Ling 自己的回复被过滤掉，已纠正并重跑。
4. `ContextCompiler(session, actor_id, persona_id).compile(subject_id, window_id, user_text="？")` 得到 `CompiledContext`。
   - `compiled.messages` = 实际发给 provider 的 `ChatMessageIn` 列表；
   - `compiled.annotated` = 同样内容 + `key`/`lane` 标签，正是快照的数据源；
   - `compiled.meta` 含 `memory_recall_skipped`、`rolled_up`、`budget_*`、`audience`。
5. PromptSnapshot 两条取得路径：
   - **端到端**：走 `/v1/sessions/{window}/turns` 真跑一轮，`turn.py:1201 _capture_prompt_snapshot()` 在调模型**之前**用 `build_prompt_snapshot_payload(compiled.annotated)` 写 `PromptSnapshot` 表（独立事务、best-effort），再用 `GET` 快照接口读（`tests/integration/test_prompt_snapshot.py` 有端到端断言）。
   - **直接取 payload**：如本任务，调用纯函数 `build_prompt_snapshot_payload(compiled.annotated, model=...)`（不需要真模型），即得与落库同结构的 `payload`（`schema_version=1`、`token_total`、`messages[]` 带 lane）。这不是数据库里的行，但内容与 `_capture_prompt_snapshot` 写进去的相同。
   - 注意触发位置：显式工具指令（`tool_ledger.explicit_guidance`）存在时不写首份快照（`turn.py:1200`）。

## 2. 真实运行结果（脚本模拟回放 + 真实编译器输出）

标注：**以下分段是「真实输出」——由未改动的 `ContextCompiler.compile()` 在真 DB 上编译得出；「前天需求/早上好/？」序列是「脚本模拟」的合成数据，不是真实用户会话。** 未调用任何外部模型，快照 payload 由真实纯函数生成。

脚本：`/tmp/p2_baseline_probe.py`（短历史）与 `/tmp/p2_baseline_probe_summary.py`（把旧事压入摘要）。
原始证据：`/tmp/p2_baseline_evidence.json`、`/tmp/p2_baseline_evidence_summary.json`；运行日志 `/tmp/p2_baseline_run.log`、`/tmp/p2_baseline_summary_run.log`。

### 2.1 场景 A：旧需求仍在原文窗口（短历史）

当前轮 `user_text="？"`；种子窗口 P2 反例回放（`win_cb38b874edf578fa964a54c8`）。实际发给模型的 8 段：

| # | role | key / lane | 内容（摘要） | 字符数 |
|---|---|---|---|---|
| 0 | system | memory / `world` | 当前在 网页控制台 私聊（P2 反例回放）／此刻时间 | 59 |
| 1 | system | memory / `relationship` | 你和这个人：还不太熟。 | 11 |
| 2 | system | system / `capabilities` | 「等会回」标记说明 | 138 |
| 3 | user | **history / `history`** | **产品方案四项需求：A 登录、B 导出、C 通知、D 报表** | 28 |
| 4 | assistant | history / `history` | 好，我记下了，之后接着聊。 | 13 |
| 5 | user | history / `history` | 早上好 | 3 |
| 6 | assistant | history / `history` | 早呀。 | 3 |
| 7 | user | input / `input` | ？ | 1 |

- **旧需求落在第 3 段：`key="history"`、`lane="history"`，是原文逐字（原文里没有 `[msg_id]` 等标记，因为合成消息无平台 id）。** 不是摘要、不是记忆召回、不是跨窗口。
- `meta.memory_recall_skipped = "low_information"`：**「？」触发了自动召回跳过，根本没有发生记忆检索**。根因是 `recall_query.py:21` 的 `rstrip(" \t\r\n.!?。~")` 在 NFKC 下剥掉「？」→ 空串 → 命中 `_STANDALONE_REPLIES`。
- `meta.persona_empty = true`（该测试环境人设为空），故无 `persona`/`constraints` 段；`meta.budget_state="within"`；无 `summary` 段（窗口无摘要）。
- 快照 payload：`schema_version=1`、`token_total=186`、`truncated=false`、`dropped_items=0`。

### 2.2 场景 B：旧需求被挤出原文、压入滚动摘要

在 A 的基础上插入 40 条长 filler 消息，使 `HISTORY_TOKEN_BUDGET` 用尽触发 `_rollup_summary()`（`meta.rolled_up=30`）。**第一次编译触发 rollup，但 `window_ctx` 在该次编译开头已缓存，摘要要到下一次编译才进提示词**——这是重要的时序事实：`window_contexts` 里已有摘要（实测 `summary_through_sequence=30`、`summary_message_count=30`，末行是 `对方说过「产品方案四项需求：A 登录、B 导出、C 通知、D 报表」`），但第一次编译的 `annotated` 无 `summary` 段。第二次编译（真实编译器）的 19 段中：

| # | role | key / lane | 内容 | 字符数 |
|---|---|---|---|---|
| 0–2 | system | memory/world、memory/relationship、system/capabilities | 同场景 A | 59/11/138 |
| 3 | system | **summary / `summary`** | **「本窗口较早的对话摘要（截至第 30 条，共 30 条）：\n… 对方说过「产品方案四项需求：A 登录、B 导出、C 通知、D 报表」」** | ~由 30 条压缩，末尾含旧需求 |
| 4–17 | user/assistant | history / `history` | 14 条 filler + 早上好 + 早呀 | 每条约 1613 |
| 18 | user | input / `input` | ？ | 1 |

- **旧需求此时出现在第 3 段：`key="summary"`、`lane="summary"`，是旧消息的确定性压缩文本（原文逐字被 `对方说过「…」` 包装后进入摘要），无 TTL、无话题相关性判断。**
- 旧需求**不在** history 段（`old_needs_in_history=false`），也不在 memory 召回段。
- 快照 payload：`token_total=20156`、`message_count=19`、`truncated=false`。

### 2.3 两种通道的区分力

| 场景 | 旧需求所在段 | 形态 | 是否记忆召回 | 是否跨窗 |
|---|---|---|---|---|
| A（短历史） | `lane=history`，user 原文 | 原文逐字 | 否（「？」被跳过） | 否（同一窗口） |
| B（长历史） | `lane=summary`，system 摘要 | 摘要文本（确定性 rollup，非模型生成） | 否 | 否 |

这直接回答 LEAD-notes §6 的待验证判据 1/3：
- 判据 1「构造 user_text='？' 检查是否出现 memory_recall_skipped」→ **出现 `low_information`，说明「？」不会触发自动召回**，与 LEAD-notes 的推理（「？走宽泛召回」）相反；「早上好」单独测 `should_skip_automatic_recall=False`（会触发召回），但它在场景 A/B 中只是历史原文，不是当前轮。
- 判据 3「lane='summary' 是否无条件存在」→ 有摘要时每轮都注入；但**被 rollup 的那一轮本身看不到新摘要**，要到下一轮。

## 3. 量化入口

| 指标 | 读取点 | 本次实测值 |
|---|---|---|
| 输入字符数（全部消息正文） | `compiled.summary()["chars"]` = `sum(len(m.content) for m in compiled.messages)`（`context.py:443–451`）；每段单独 `len(content)` 在快照 `messages[].content` 上算 | A: 256 字符；B: 见证据 `annotated[].chars` 之和 |
| 输入 token 数 | `estimate_tokens()`（`context.py:105`）；快照 `payload["token_total"]` = 每条 `estimate_tokens(content)` 之和（`turn.py:229,238`）；`compiled.meta["budget_total_chars"]`、`budget_soft/hard/state` 给预算余量 | A: `token_total=186`；B: `token_total=20156` |
| 分段长度/归属 | `compiled.annotated` 的 `key`/`lane`/`content` → 快照 `messages[]` 的 `key`/`lane`/`tokens`/`content` | 见 §2 表 |
| 是否截断 | 快照 `payload["truncated"]`、`dropped_items`；单条 `messages[].truncated`（256KB 上限 `PROMPT_SNAPSHOT_MAX_BYTES`） | 两次均 `false` |
| 额外调用次数 | ① **Provider 请求数**：`ScriptedProvider.requests`（`tests/regression/chat_fixture.py:24`）——原生工具回合里 golden `prompt-native-tool.json` 是两个请求的数组，第二请求带 `role=tool` 结果，即额外调用 1 次（`test_prompt_golden.py:248–260`）。② **行为模型调用账本**：`behavior_model_calls` 表（`src/ling/infra/models.py:3034–3079`），`kind` ∈ outline/refine/replan/dream，字段 `state/actual_tokens/reserved_input_tokens`，经 `AgendaService.reserve_model_call/finish_model_call` 记账（`domains/agenda/service.py:155–214`）；`AttentionPollLogRow.llm_calls` 是「要不要说」判断的调用数（`api/routes/manage.py:2150`）。③ 摘要 rollup **不调用模型**（`context.py:1989`「确定性压缩，不调用模型」），所以场景 B 的旧需求入摘要不产生额外模型调用。 | 本任务两个脚本未跑完整 turn，`provider.requests` 为 0；额外调用次数的读取点如上，未实测非零值 |

## 4. 最小可复现命令清单

```bash
cd /Users/lingxi/project/ling
source /tmp/ling_test_lock.sh
acquire_test_lock
export LING_TEST_DATABASE_URL="postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"
export LING_DATABASE_URL="postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"

# 场景 A/B 的 /tmp 脚本（本任务已跑，1 passed each）
.venv/bin/python -m pytest /tmp/p2_baseline_probe.py -q -s -p no:cacheprovider \
  --confcutdir=/Users/lingxi/project/ling 2>&1 | tee /tmp/p2_baseline_run.log
.venv/bin/python -m pytest /tmp/p2_baseline_probe_summary.py -q -s -p no:cacheprovider \
  --confcutdir=/Users/lingxi/project/ling 2>&1 | tee /tmp/p2_baseline_summary_run.log

release_test_lock
```

若 `/tmp` 脚本已清理，仓库内可跑的最小现成命令（不改代码，只读 golden）：

```bash
source /tmp/ling_test_lock.sh && acquire_test_lock
export LING_TEST_DATABASE_URL="postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"
export LING_DATABASE_URL="postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"
.venv/bin/python -m pytest tests/regression/test_prompt_golden.py -q \
  2>&1 | tee /tmp/golden.log
release_test_lock
```

## 5. 边界与置信度

- **实测（高置信）**：六个文件的只读分析、DB 连通性、两次真实编译的分段/lane/token/字符数、`should_skip_automatic_recall("？")=True`、`window_contexts` 摘要落库内容。原始输出在 `/tmp/p2_baseline_evidence*.json` 与日志文件。
- **推理（中置信）**：「短历史走 history 原文、长历史走 summary 摘要」是这两个合成场景的因果解释；真实线上反例是否同时走了其他通道（例如当前轮不是单字「？」而是带标点的变体）未验证。
- **未取得**：真实用户会话的 PromptSnapshot（无生产权限，已按纪律禁连）；真实模型对这段提示词的响应（脚本未接外部模型）；额外调用次数的非零实测（本任务没跑完整工具回合）。
- 未改 `src/`、`tests/`；临时脚本只在 `/tmp`。结束时锁已释放，两个被测源文件的 sha256 与开始时相同。

## 6. 编码检查

对本报告与两份原始证据 JSON 做了 Unicode 替换字符扫描（命令用 `grep` 匹配该码点，不在正文里重复写出以免自查自命中），结果：**零命中**。
