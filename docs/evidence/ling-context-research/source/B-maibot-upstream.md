# 群组 B：MaiBot 官方仓库取证（上游上下文机制）

> 任务：为 Ling context research 提供 MaiBot 官方上游的一手证据，重点回答「上下文窗口里的旧内容从哪来、是否有话题切换检测、是否需要新窗口」。
> 写作用域：仅本报告文件。未连接 NAS/生产库，未部署插件，未运行 pytest。

## 0. 观测钉住（逐字输出）

在 `/Users/lingxi/project/ling` 执行任务指定的首条命令，输出逐字如下：

```
$ cd /Users/lingxi/project/ling && git status --short && git log -1 --format='%H %ad %s' --date=iso-strict
?? docs/evidence/ling-context-im-group.md
?? docs/evidence/ling-context-im-private.md
?? docs/research/
de43cbe79a78e0c01f7821470500eaa04ea79497 2026-11-22T09:17:29+08:00 feat(live): 合入直播工作台与舞台控制
```

- 基线 HEAD 符合要求：`de43cbe79a78e0c01f7821470500eaa04ea79497`。
- 三个未跟踪路径与本报告无关，未触碰；其中两个 IM 取证文档的 mtime 在本次取证期间变化，系他人并行工作，本报告不引用其内容。
- 观察时刻：2026-11-22 09:28–09:40 +08:00。

## 1. 来源与版本钉住

### 1.1 官方仓库判定

- 最初尝试的 `MaiMaiBT/MaiBot` 返回 **HTTP 404**（GitHub API：`https://api.github.com/repos/MaiMaiBT/MaiBot`）——该 org/repo 不存在。
- 通过 GitHub 搜索 API 得到官方规范仓库：**`Mai-with-u/MaiBot`**（`https://api.github.com/search/repositories?q=MaiBot+in:name&sort=stars&per_page=20`；7094 stars，org account，homepage `https://maibot.chat/`，default branch `main`）。仓库组织下另有官方适配器（`MaiBot-Napcat-Adapter`）、Dashboard、一键包（`MaiBotOneKey`）等仓库。
- 官方文档站：`https://docs.maibot.chat/`（首页明确链接同一 GitHub 仓库）。文档站顶部仍显示「当前版本 1.4.3」，落后于仓库发布（观察：文档站；推测：文档更新滞后，置信度中）。

### 1.2 代码版本钉住

| 项 | 值 | 来源 |
|---|---|---|
| 最新 release tag | **v1.4.8** | `https://api.github.com/repos/Mai-with-u/MaiBot/releases/latest` |
| release target commit | `edf42446f123149c57fc79765dd5a8b004b8524c` | 同上 `target_commitish` |
| main HEAD commit | `edf42446f123149c57fc79765dd5a8b004b8524c` | `https://api.github.com/repos/Mai-with-u/MaiBot/commits/main` |
| commit 日期 | `2026-11-20T08:01:22Z`（committer date） | 同上 |
| release 发布 | `2026-11-20T08:07:28Z` | `releases/latest` |
| 版本号 | `pyproject.toml` 中 `version = "1.4.8"` | 同上 commit 的 diff |
| 本报告的逐行证据基准 | 官方 tarball `https://api.github.com/repos/Mai-with-u/MaiBot/tarball/v1.4.8`，解压目录名 `Mai-with-u-MaiBot-edf4244` | 已下载到本地 `/tmp/maibot-forensics/`（仓库外），只读分析 |

下文所有 `文件:行号` 均指该 pinned commit（除显式标注的官方文档页面）。URL 前缀统一为 `https://github.com/Mai-with-u/MaiBot/blob/edf42446f123149c57fc79765dd5a8b004b8524c/`。

### 1.3 访问失败声明

- `https://docs.maibot.chat/manual/chat-scenes/` 一次 web_fetch 失败（`TypeError: fetch failed`，瞬时网络错误），未据其下结论；其余官方页面/API 均成功。
- 未使用任何第三方镜像、教程或 fork 作为证据来源。

## 2. 会话键 / 聊天身份（session key）

**观察（代码路径，置信度高）**：session_id 是一个 MD5 哈希，输入由「平台 + 可选路由元数据（account/scope）+ 群 ID 或用户 ID」拼接而成：

- `src/common/utils/utils_session.py:8-38` `SessionUtils.calculate_session_id()`：
  - 群聊：`components = [platform, *route_components, group_id]`（`:34`）
  - 私聊：`components = [platform, *route_components, user_id, "private"]`（`:36`）
  - `route_components` 仅在提供 `account_id`/`scope` 时追加 `account:<id>` / `scope:<scope>`（`:28-32`）
  - 返回 `hashlib.md5("_".join(components).encode()).hexdigest()`（`:38`）；`user_id` 和 `group_id` 都不给时抛 `ValueError`（`:25-26`）
- `src/chat/message_receive/chat_manager.py:214-220`（`get_or_create_session`）、`:281-288`（`register_message`）调用该函数；`message.session_id = session_id` 在 `:288` 被显式写回消息。
- `src/common/data_models/chat_session_data_model.py:9-40` `MaiChatSession`：`is_group_session = bool(self.group_id)`（`:40`）；群聊时 user 相关字段被清空（`:27-29`），即身份粒度是「群」而不是「群内发言人」。
- 同一 `session_id` 优先复用内存会话，其次查数据库，最后才新建（`chat_manager.py:221-271`）。

**结论（观察 + 推理，置信度高）**：聊天身份 = (platform, account?, scope?, group_id) 或 (platform, account?, scope?, user_id, "private")；同一群/同一私聊跨天、换话题都得到**同一个 session_id**。会话身份与话题无关。群内不同发言人不产生不同 session。

## 3. 历史窗口：配置、默认值、实际读取与应用位置

### 3.1 配置项与默认值（官方配置 schema）

| 键（TOML 路径） | 默认值 | 定义位置 |
|---|---|---|
| `chat.max_context_size` | **50**（群聊，范围 30–300） | `src/config/official_configs.py:855-870`，类 `ChatConfig`（`:833`） |
| `chat.max_private_context_size` | **75**（私聊） | `src/config/official_configs.py:872-887` |
| `chat.use_token_limit` | `False` | `src/config/official_configs.py:889-897` |
| `chat.max_context_tokens` | `128000` | `src/config/official_configs.py:899-912` |
| `chat.private_use_token_limit` / `chat.private_max_context_tokens` | `False` / `128000` | `src/config/official_configs.py:914-937` |

配置文档也说明这些文件在 `config/bot_config.toml`，保存后热重载（`https://docs.maibot.chat/manual/configuration/`；观察）。

### 3.2 实际读取与应用（调用链，非仅 schema）

1. **场景取值**：`src/maisaka/runtime.py:217-221` `_max_context_size` → `self.scene.max_context_size`；`src/maisaka/scenes/group/behavior.py:31-32` 返回 `max(1, int(global_config.chat.max_context_size))`；私聊走 `src/maisaka/scenes/private/behavior.py:32-33` 的 `max_private_context_size`。Token 阈值见 `runtime.py:225-228`。
2. **请求前选窗（进入 LLM 的地方）**：`src/maisaka/chat_loop_service.py:1479-1574` `select_llm_context_messages()`：
   - `base_context_size = max(1, int(max_context_size or global_config.chat.max_context_size))`（`:1494`）；
   - `effective_context_size = max(MIN_RETAINED_CHAT_MESSAGES, int(base_context_size * CONTEXT_SELECTION_CACHE_STABILITY_RATIO))`（`:1495-1498`）；常量 `CONTEXT_SELECTION_CACHE_STABILITY_RATIO = 2.0`（`:103`）、`MIN_RETAINED_CHAT_MESSAGES = 5`（`src/maisaka/context/post_processor.py:17`）；
   - 从最新消息倒序挑选，按 `message.count_in_context` 计数，达到 `effective_context_size` 即停（`:1519-1534`）；
   - 结果由 `chat_loop_step()`（`:1170-1200`）传给 `_build_request_messages`，再由 `src/maisaka/reasoning_engine.py:199-230` `_run_interruptible_planner()` 发起 Planner LLM 请求；另一处调用在 `reasoning_engine.py:264`（工具发现）、`:376` 与 `runtime.py:1349`（子智能体路径）。
   - **重要**：默认群聊配置 50 条，实际选窗容量是 `50 × 2 = 100` 条（私聊 `75 × 2 = 150`），目的是 prompt-cache 稳定（日志文案 `cache_window 50->100`，`:1561-1564`）。Token 模式下（`:1499-1505`）改为保留全部活动历史，由模型返回的实际输入 token 触发裁切。
3. **轮次结束后裁切**：`src/maisaka/reasoning_engine.py:1413-1479` `_post_process_chat_history_after_cycle()` 调用 `src/maisaka/context/post_processor.py:31-85` `process_chat_history_after_cycle()`：
   - 触发条件 `remaining_context_count >= max_context_size`（`:57-60`，条数模式），即按 50/75 触发，而不是按 100/150；
   - 裁切目标 `max(5, int(retained_base * TRIM_TARGET_RATIO))`，`TRIM_TARGET_RATIO = 0.2`（`:16`、`:63`），默认群聊压到约 **10 条**、私聊约 **15 条**；
   - 保留最近 5 条真实聊天消息与完整工具轮次（`:119-176`）。
4. **启动恢复窗口**：`src/maisaka/runtime.py:384-388` `_get_context_restore_limit()` = `max(1, ceil(context_size * CONTEXT_RESTORE_FILL_RATIO))`，`CONTEXT_RESTORE_FILL_RATIO = 0.5`（`:94`）；条数模式默认恢复 `ceil(50*0.5)=25` 条（私聊 38 条），token 模式恢复 150 条。`_restore_recent_context_from_db()`（`:328-382`）从消息库按 session_id 取最新消息重建短期历史；`/clear` 之后的旧消息被 `select_messages_after_latest_clear_marker` 排除。
5. **回复器（第二 LLM 阶段）独立窗口**：`src/maisaka/runtime.py:305-325` `build_reply_history()` 用 `context_limit = max(1, int(self._max_context_size * 0.6))`（群聊默认 30 条）从 DB 读取独立回复窗口，避免 Planner 裁切影响回复器。

**结论（观察，置信度高）**：配置键是 `chat.max_context_size`（50）/ `chat.max_private_context_size`（75）；它既控制启动恢复与轮末裁切阈值，也（经 2× 系数）决定单次 Planner 请求实际可选取的消息上限。默认群聊一次请求最多选取约 100 条上下文消息，但正常轮末裁切会把运行时历史压到约 10 条 + 常驻摘要，因此稳态下实际可见消息远少于 100。

## 4. 记忆机制：短记忆、摘要、长期记忆/画像、人设

### 4.1 短期上下文（运行时窗口）

- 载体是 `MaisakaHeartFlowChatting._chat_history`（`src/maisaka/runtime.py`），进程内维护，随 `/clear` 或实例淘汰失效（`src/chat/heart_flow/heartflow_manager.py:177-188` `clear_chat_history_context()`：「停止并移除当前会话运行时，使其短期历史上下文立即失效」）。
- 数据库消息长期留存，但进入 LLM 需经 §3.4 的恢复窗口或工具检索。（注：此处指 §3 第 4 点「启动恢复窗口」。）

### 4.2 滚动摘要（compaction）——「常驻内部状态」

- `src/maisaka/context/compaction.py:1` 文件 docstring：「将较早的对话压缩为常驻的滚动状态，近期原文继续保留」。
- `ContextSummaryMessage`（`:73-83`）继承 `ReferenceMessage`，docstring：「常驻 Planner 的内部状态，不伪装成用户消息，也不占近期消息条数」，显示前缀「【上下文状态摘要-内部参考】」。
- 生成：`src/maisaka/reasoning_engine.py:1435-1472` 在裁切后调用 `build_context_summary()`；摘要结构为五分类 `topics/background/progress/pending/agreements`（`compaction.py:39-70`），提示词见 `prompts/zh-CN/main/context/context_compaction.prompt:1-15`（「以新事实更新旧状态」「合并重复内容和同一事件的过程」）。
- **无条件注入主链**：`src/maisaka/chat_loop_service.py:1620-1638` `_collect_always_selected_reference_indices()` 把 `ContextSummaryMessage`（以及 `CONTEXT_RESTORE` 参考消息）加入「需要长期随请求发送的参考消息索引」，该调用发生在 `:1514-1517` 的选窗流程中；`:1651-1652` 只对非 planner 请求过滤掉摘要，planner 请求一律保留。**没有任何话题相关性或时间有效性判断**。
- **裁切时摘要永不被移除**：`src/maisaka/context/post_processor.py:167` 与 `:175-176`——计数和移除都显式跳过 `ContextSummaryMessage`，旧摘要对象原样留在历史前缀；新摘要由 `reasoning_engine.py:1464-1467` 以 `[summary, *其余消息]` 替换旧摘要。
- 摘要**不写数据库**：全仓 `src/common/database`、`src/services/` 中无 `ContextSummaryMessage` 持久化引用（grep 零命中；负向观测，置信度中）；重启后 `_restore_recent_context_from_db()` 只从 DB 恢复消息并生成一条 `restore_reference_message`（`runtime.py:375-379`），不恢复摘要对象。`/clear` 移除运行时也会丢弃摘要，但 DB 消息仍在（下一次进入窗口由 clear-marker 规则决定）。

### 4.3 长期记忆 A-Memorix

- **总开关默认关闭**：`src/config/official_configs.py:1832-1847` `AMemorixPluginConfig.enabled = False`；官方文档一致：「总开关，默认关闭」（`https://docs.maibot.chat/manual/configuration/amemorix-config`）。
- 记忆按聊天流隔离：官方同页「默认情况下，每个聊天流（每个群、每个私聊）只能检索到自己产生的记忆」；代码默认 `global_memory_sharing_enabled=False`、`shared_memory_groups=[]`（`official_configs.py:3941-3959`）。
- 集成开关（`src/config/official_configs.py:1304-1570`，类 `AMemorixIntegrationConfig`）：
  - `enable_memory_query_tool=True`（`:1309-1319`）：作为 Planner 工具 `query_memory` 暴露（`src/maisaka/builtin_tool/__init__.py:75-78`、`:104`；工具定义 `src/maisaka/builtin_tool/query_memory.py:25-30`），由模型**主动调用**才进入上下文；这是工具能力，不是每轮自动注入。
  - `enable_person_profile_injection=True`（`:1350-1361`）：每轮 Planner 前自动注入画像；代码接线 `src/maisaka/reasoning_engine.py:97`、`:301-309`、`:321`（`_build_planner_injected_user_messages`），最终进入 `chat_loop_service.py:1113` 的 injected messages。注意：总开关 `plugin.enabled=False` 时画像/记忆入口的实际产出由记忆服务门控（「配置存在」≠「总开关打开后才有数据」）。
  - `heuristic_memory_recall_enabled=False`（`:1378-1389`）：启发式「自然拉起」默认关闭；代码 `src/maisaka/memory/heuristic_injector.py:65-68` 在开关关闭时直接返回空串；接线在 `reasoning_engine.py:292-299`。配套节流键 `heuristic_memory_recall_window_size=20`、`:limit=3`、`:max_chars=900`、`:min_interval_seconds=180`、`:min_new_messages=60`（`official_configs.py:1404-1495`）；跨聊天流拉起默认关（`:1391-1401`）。官方文档同页说明这些是「每个 Planner 轮次根据最近聊天内容生成印象，自动检索并注入相关记忆」。
  - 写回：`person_fact_writeback_enabled=True`、`chat_summary_writeback_enabled=True`（`:1525-1549`），阈值 `chat_summary_writeback_message_threshold=36`、`chat_summary_writeback_context_length=36`（`:1551-1570`）——写回是**异步后台链路**，不是把长期记忆原文塞进每轮主请求。
- **区分（推理，置信度高）**：A-Memorix 的记忆通过「工具调用 / 画像注入 / 默认关闭的启发式注入」进入 Planner，属于**可开关旁路**；它不属于 `select_llm_context_messages` 的窗口裁切链路。

### 4.4 人设 / 风格

- 人设/人格在 `src/config/official_configs.py:148-193`（`PersonalityConfig`），官方配置文档说明「机器人人设、人格…热重载」。
- 注意力漂移 `experimental.group_attention_drift` / `private_attention_drift` **默认 `enabled=False`**（`official_configs.py:952-963`、`:1135-1149`）；开启后只向 system prompt 追加风格文本（`src/maisaka/attention_drift.py:82-110`，`chat_loop_service.py:809-810`），不是话题检测算法。

## 5. 运行时入口与主 LLM 调用位置

- 源码启动：`bot.py:1-8` → `src.host.launcher.main(get_instance_root())` → `src/host/launcher.py:24-61` 用子进程分阶段 `bootstrap`/`updater` 启动。
- Docker：`docker-compose.yml` 使用镜像 `sengokucola/maibot:latest`，WebUI 映射 `18001:8001`；`docker-entrypoint.sh` 最后 `exec /MaiMBot/.venv/bin/python bot.py "$@"`。官方文档亦有 Docker/uv/Windows/Linux 部署页。
- 消息入口：`src/chat/message_receive/bot.py:1032-1035` → `src/chat/heart_flow/heartflow_message_processor.py:26-60` `process_message()`（落库、注册会话、触发 `memory_automation_service`）。
- **主 LLM 调用**：`src/maisaka/reasoning_engine.py:199-230` `_run_interruptible_planner()` → `src/maisaka/chat_loop_service.py:1160-1200` `chat_loop_step()`（`:1185` 选窗）→ `_build_request_messages()` → 模型请求；一次「思考循环」的多轮由 `MaisakaReasoningEngine` 驱动（同文件 `:640-710`）。
- 上下文组装的三类输入：① 选中的聊天历史（§3）；② 常驻摘要（§4.2）；③ 一次性注入（启发式记忆/画像/图片记忆，`reasoning_engine.py:280-324`）。

## 6. 针对 Ling 关切的直接回答

**Q1：同一窗口里的「旧内容」从何而来？**

观察（置信度高）：在 MaiBot 中，进入同一 Planner 请求的旧内容有四个来源：

1. 运行时 `_chat_history` 中尚未被裁切的近期原文（轮末裁切目标约 10/15 条，§3）；
2. **常驻滚动摘要**——所有被裁切掉的旧原文压成 `ContextSummaryMessage`，随后每轮无条件随 planner 请求发送（`chat_loop_service.py:1620-1638`），且裁切永不移除它（`post_processor.py:167,175-176`）；这是「隔了很久的旧事仍出现在上下文」的最直接机制；
3. 启动时从 DB 回灌的最近消息（`runtime.py:328-388`，默认群聊 25 条）；
4. 可选旁路：画像注入、模型主动调用 `query_memory`、默认关闭的启发式记忆（§4.3）。

**Q2：是否有显式话题切换检测？**

观察（置信度高）：在官方仓库全源码检索中，没有运行时话题切换检测器：

- `src/common/database/migrations/behavior_generic_tags.py:279` 的「群聊话题切换」是一个**学习用通用标签**（标签词表），不是检测/切窗代码；
- `src/maisaka/attention_drift.py:7-64` 只有提示词文案（「不要凭空换话题」等），默认关闭；
- 唯一的结构性时间信号是跨日提示 `src/maisaka/chat_loop_service.py:1091-1105`：当相邻上下文消息日期不同，插入一条 `时间：YYYY-MM-DD HH:MM:SS` 的 user 消息（`:870-872`）——它只是告知日期，不触发话题切分；
- 摘要提示词中的 `topics` 字段只是对「当前话题」的文本归纳（`prompts/zh-CN/main/context/context_compaction.prompt:7`），不是事件触发器。

结论（推理，置信度中高）：话题连续性依赖**自然漂移**——模型只看当前窗口与常驻摘要，由提示词和模型自身判断如何处理旧内容；上游没有「检测到新话题→开新上下文」的运行时逻辑。

**Q3：按上游设计是否需要「新窗口」？**

结论（观察+推理，置信度高）：**不需要，也不存在自动的「按话题新建窗口」**。session_id 只由平台/账号/群或用户 ID 决定（§2），同一聊天流复用同一会话；上游用「滚动裁切 + 常驻摘要」在单窗口内管理长期对话，`/clear` 是唯一显式重置短期上下文的用户命令（`src/chat/message_receive/bot.py:471-570`：「已清空…Maisaka 历史上下文」/「我暂时失忆了」），且它不生成新 session_id，只淘汰运行时并重放 clear marker 之后的消息。对 Ling 的启示：若产品需要「话题边界」，那是 Ling 在上游机制之外的增补，不能归因于 MaiBot 设计。

**Q4：存在但与主链未连接 / 默认不生效的配置**

| 配置 | 状态 | 证据 |
|---|---|---|
| `a_memorix.plugin.enabled` | 默认 `False`，长期记忆整体关闭 | `official_configs.py:1832-1847` + 官方文档 |
| `a_memorix.integration.heuristic_memory_recall_enabled` | 默认 `False`；关闭时注入函数直接返回空串 | `official_configs.py:1378-1389`；`heuristic_injector.py:65-68` |
| `a_memorix.integration.heuristic_memory_cross_chat_enabled` | 默认 `False` | `official_configs.py:1391-1401` |
| `experimental.group_attention_drift.enabled` / private | 默认 `False`；仅 prompt 文案 | `official_configs.py:952-963,1135-1149`；`attention_drift.py:82-110` |
| `chat.use_token_limit` / `private_use_token_limit` | 默认 `False`，即默认按消息条数裁切而非 token | `official_configs.py:889-897,914-922`；`chat_loop_service.py:1499-1505` |
| `a_memorix.integration.chat_summary_writeback_*` | 默认 `True` 但属**后台写回**链路，不在主 LLM 请求内直接注入 | `official_configs.py:1538-1570` |
| `feedback_correction_*` | 默认 `False`（实验性） | `official_configs.py:1633` + 官方文档 |

## 7. 证据表（URL + 文件 + 行号）

| # | 结论 | 证据（官方 URL / 文件:行 / commit） | 置信度 |
|---|---|---|---|
| 1 | 官方规范仓库是 `Mai-with-u/MaiBot`，v1.4.8 为最新 release | `https://api.github.com/repos/Mai-with-u/MaiBot/releases/latest`；`https://api.github.com/repos/Mai-with-u/MaiBot/commits/main` | 观察（高） |
| 2 | 代码基准 commit `edf42446…`，2026-11-20T08:01:22Z | 同上；tarball 解压目录 `Mai-with-u-MaiBot-edf4244` | 观察（高） |
| 3 | session_id = MD5(平台+路由+群/用户)，跨话题稳定 | `https://github.com/Mai-with-u/MaiBot/blob/edf42446.../src/common/utils/utils_session.py#L8-L38`；`src/chat/message_receive/chat_manager.py:214-220,281-288` | 观察（高） |
| 4 | 群聊身份不含发言人 | `src/common/data_models/chat_session_data_model.py:24-40` | 观察（高） |
| 5 | `max_context_size=50` / `max_private_context_size=75` | `src/config/official_configs.py:855-887` | 观察（高） |
| 6 | 请求选窗 = 配置 × 2（cache 稳定），最小 5 | `src/maisaka/chat_loop_service.py:103,1494-1498,1561-1564`；`src/maisaka/context/post_processor.py:17` | 观察（高） |
| 7 | 选窗在 `select_llm_context_messages` 完成并进入 LLM 请求 | `src/maisaka/chat_loop_service.py:1185-1200,1519-1534`；`src/maisaka/reasoning_engine.py:199-230` | 观察（高） |
| 8 | 轮末裁切阈值=50，目标=20%（约 10 条），保留最近 5 条 | `src/maisaka/reasoning_engine.py:1413-1429`；`src/maisaka/context/post_processor.py:16,57-68,119-149` | 观察（高） |
| 9 | 启动恢复 0.5×窗口；回复器窗口 0.6× | `src/maisaka/runtime.py:94,305-325,384-388` | 观察（高） |
| 10 | 摘要常驻、无条件进入 planner 请求 | `src/maisaka/context/compaction.py:1,73-83`；`src/maisaka/chat_loop_service.py:1514-1517,1620-1638,1651-1652` | 观察（高） |
| 11 | 裁切永不移除摘要对象；新摘要替换旧摘要 | `src/maisaka/context/post_processor.py:167,175-176`；`src/maisaka/reasoning_engine.py:1441-1467` | 观察（高） |
| 12 | A-Memorix 总开关默认关、按聊天流隔离 | `src/config/official_configs.py:1832-1847,3941-3959`；`https://docs.maibot.chat/manual/configuration/amemorix-config` | 观察（高） |
| 13 | 启发式记忆默认关、关闭时直接返回空串 | `src/config/official_configs.py:1378-1389`；`src/maisaka/memory/heuristic_injector.py:65-68` | 观察（高） |
| 14 | 话题切换只是学习标签；无运行时检测器 | `src/common/database/migrations/behavior_generic_tags.py:279`；全仓 grep `topic.?switch|话题切换|…` 仅命中该标签与 UI 注释 | 观察（高，负向搜索） |
| 15 | 注意力漂移是 prompt 文案、默认关 | `src/config/official_configs.py:952-963,1135-1149`；`src/maisaka/attention_drift.py:7-64,82-110`；`src/maisaka/chat_loop_service.py:809-810` | 观察（高） |
| 16 | 唯一时间信号是跨日时间提示，不切窗 | `src/maisaka/chat_loop_service.py:870-872,1091-1105` | 观察（高） |
| 17 | `/clear` 是唯一显式短期清空命令，不换 session_id | `src/chat/message_receive/bot.py:471-570`；`src/chat/heart_flow/heartflow_manager.py:177-188`；`src/maisaka/context/clear_context.py:14-42` | 观察（高） |
| 18 | Docker 启动入口 | `bot.py:1-8`；`src/host/launcher.py:24-61`；`docker-compose.yml`；`docker-entrypoint.sh` | 观察（高） |

## 关键结论

- **版本钉住**：官方仓库 `Mai-with-u/MaiBot`，最新 release **v1.4.8 / commit `edf42446f123149c57fc79765dd5a8b004b8524c`（2026-11-20）**；本报告逐行证据均基于该 commit 的官方 tarball。
- **会话身份稳定**：session_id 由「平台 + (account/scope) + 群 ID 或用户 ID」MD5 得到，同一群/私聊跨天与换话题都复用同一会话；群内发言人不分会话。
- **窗口配置与实际值不同**：`chat.max_context_size=50`（群）/`max_private_context_size=75`（私）；请求选窗按 **2×** 放大（约 100/150 条），轮末裁切在 50/75 触发并压到约 10/15 条，启动仅回灌 0.5×，回复器另用 0.6×；token 模式默认关闭。
- **旧内容的主因是常驻摘要**：被裁切的旧消息压成 `ContextSummaryMessage`，每轮 planner **无条件注入**、裁切从不移除；这是「很久以前的事仍在同一上下文」的结构性来源，而非 bug。
- **没有显式话题切换检测**：`群聊话题切换` 只是学习标签；`attention_drift` 是默认关闭的 prompt 风格；跨日时间提示只告知日期。话题连续性靠自然漂移。
- **不需要新窗口**：上游用单 session + 滚动裁切 + 常驻摘要管理长期对话；唯一显式重置是 `/clear`（淘汰运行时，不换 session_id）。若 Ling 需要话题边界，属上游之外的增补。
- **长期记忆是可选旁路**：A-Memorix 总开关默认关、按聊天流隔离；画像注入与 `query_memory` 工具默认开但依赖总开关有数据，启发式记忆默认关；写回阈值 36 条属后台链路。不要把「配置存在」当成「主链已接线生效」。
- **Ling 对照价值**：Ling 的 `Window` 复用与话题无关、`summary` 不过期且无条件注入（见 Lead notes）与 MaiBot 的「session 稳定 + 摘要常驻」同构；差异在于 MaiBot 摘要只在运行时内常驻、重启/`/clear` 即丢弃，而 Ling 的摘要落库长期保留——这是移植上游思路到 Ling 时必须显式处理的边界。
