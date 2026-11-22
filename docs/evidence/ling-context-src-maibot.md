# MaiBot 官方仓库上下文 / 会话 / 记忆机制取证

> 本文只记录取证到的**代码事实**与**观测方法**；推测单独标注。不写任何真实凭据，内网地址用 `192.0.2.x` 占位。
> 行号均以第 1 节钉住的 commit 为准；行号来自 GitHub API 取回的原始文件内容，非第三方转述。

## 1. 取证环境与版本 / commit / 入口

### 1.1 本地仓库锚点（观测输出原文）

- 工作目录：`/Users/lingxi/project/ling`
- `git log -1 --format='%H %s'`：`de43cbe79a78e0c01f7821470500eaa04ea79497 feat(live): 合入直播工作台与舞台控制`
- `git branch --show-current`：`main`
- 开始时 `git status --short`：仅见其他 agent 的未跟踪证据文档（`docs/evidence/ling-context-im-*.md`、`docs/evidence/ling-context-research/`、`docs/research/`），**未见本文目标文件**；本文是新增文件，不修改业务代码、不 `git add`、不提交。

### 1.2 官方仓库锚点

| 项 | 值 | 取证方式 |
|---|---|---|
| 仓库全名 | `Mai-with-u/MaiBot` | `GET https://api.github.com/repos/Mai-with-u/MaiBot`（已核实 `full_name` 字段） |
| 默认分支 | `main` | 同上 `default_branch` 字段 |
| 主页 | `https://maibot.chat/` | 同上 `homepage` 字段 |
| `main` HEAD | `edf42446f123149c57fc79765dd5a8b004b8524c` | `GET /repos/Mai-with-u/MaiBot/commits/main`，`sha` 字段 |
| HEAD 提交时间 | `2026-11-20T08:01:22Z` | 同上 `commit.committer.date` |
| HEAD 提交信息首行 | `feat: 发布 1.4.8 与 WebUI 1.10.8 (#2497)` | 同上 `commit.message` |
| tag `v1.4.8` 指向 | `edf42446f123149c57fc79765dd5a8b004b8524c`（annotated? 否，object.type=`commit`） | `GET /repos/Mai-with-u/MaiBot/git/ref/tags/v1.4.8` |
| 最新 release | `v1.4.8`，`published_at=2026-11-20T08:07:28Z` | `GET /repos/Mai-with-u/MaiBot/releases/latest` |
| 源码版本 | `1.4.8` | `pyproject.toml:7` `version = "1.4.8"`；`src/common/version.py:14-32` 从 `pyproject.toml` 读取 |
| 许可证 / fork / archived | `GPL-3.0` / `false` / `false` | repo 元数据 |
| 描述 | MaiSaka, an LLM-based intelligent agent… | repo 元数据 `description` |

同名项目排除依据：本仓库 `full_name=Mai-with-u/MaiBot`、主页 `maibot.chat`、描述为 LLM 智能体 MaiSaka；不是 Discord Music Bot 的同名项目。

### 1.3 源码获取方式与失败记录

- 匿名 `curl https://api.github.com/repos/Mai-with-u/MaiBot` → `API rate limit exceeded for 157.18.193.150`（未取到内容）。
- `git clone --depth 1 --branch main https://github.com/Mai-with-u/MaiBot.git /tmp/maibot-src` → `curl 28 Failed to connect to github.com port 443 after 75001 ms`；`fatal: expected flush after ref listing`，克隆失败。
- 改用本机已认证的 `gh` CLI（`gh auth status` 显示登录 `github.com` 账号 `qsbb`，token scopes `repo` 等；**未记录任何真实凭据**）：
  - 元数据：`gh api repos/Mai-with-u/MaiBot`
  - 文件内容：`gh api 'repos/Mai-with-u/MaiBot/contents/<path>?ref=edf42446f123149c57fc79765dd5a8b004b8524c' --jq '.content' | base64 -d`
  - 代码搜索：`gh api 'search/code?q=<keyword>+repo:Mai-with-u/MaiBot'`
  - 文件下载到临时目录 `/tmp/maibot-api/`（非仓库内容），所有行号来自这些文件。

### 1.4 运行入口（配置声明 vs 代码实际）

- `bot.py:1-7`：源码部署和 OneKey 的启动入口，`raise SystemExit(main(get_instance_root()))`，`main` 来自 `src.host.launcher.main`。
- `src/runtime.py:1-40`：`python -m src.runtime` 入口；开头有 legacy runner 交接逻辑（`:12-24`）。
- `src/main.py:287-340`：异步 `main()` 组装 `MainSystem`（`:287-295`），`:339-340` `asyncio.run(main())`；`src/main.py:133` `await self.app.run()`。
- `pyproject.toml:7` 版本源；`src/common/version.py:14-32` 运行时读取。

上述三处入口都在代码中，未发现「声明了但没人调用」的入口。

## 2. 会话与历史窗口机制（含行号）

### 2.1 会话标识：`platform` + 账号/作用域 + `group_id|user_id` → MD5

`src/common/utils/utils_session.py:8-42` `SessionUtils.calculate_session_id`：

- `:29-30` 未提供 `user_id`/`group_id` 抛 `ValueError`。
- `:32-36` 可选路由段：`account:<account_id>` 与 `scope:<scope>`。
- `:38-41` 群聊 components = `[platform, *route_components, group_id]`；私聊 = `[platform, *route_components, user_id, "private"]`。
- `:42` `hashlib.md5("_".join(components).encode()).hexdigest()`。

调用点（已核实实际接线，不是只声明）：

- `src/chat/message_receive/chat_manager.py:214-219` `get_or_create_session` 用它，`:234` `select(ChatSession).filter_by(session_id=...)` 命中 DB，`:258-270` 新建会话；`:281-290` `register_message` 对每条入站消息计算并写 `message.session_id = session_id`。
- `src/chat/message_receive/bot.py:870,893` 也在入站链路计算 session_id。

结论：群聊以 **群号** 为会话粒度，私聊以 **用户 ID + 字面量 "private"** 为粒度；同平台多账号/多路由作用域由 `account:`/`scope:` 段隔离。会话 ID 是 32 位 MD5 十六进制，不是明文三元组。

### 2.2 原始历史从 DB 取「最近 N 条」

`src/common/message_repository.py:132-242` `find_messages`：

- `:145-146` 参数 `limit: int = 0`、`limit_mode: str = "latest"`。
- `:104-105` `session_id` 过滤条件 `Messages.session_id == session_id`。
- `:207` 默认 `Messages.is_command == False`（非 command 消息；`:234-240` 另有 `intercept_message_level` 过滤）。
- `:211-220` `limit>0` 且 `limit_mode="latest"` 时按时间倒序取 `limit` 条再 `reversed` 回正序——即「最近 N 条」。

上层读取位置：

- `src/maisaka/runtime.py:330-342` 启动时 `_restore_recent_context_from_db`：`find_messages(session_id=..., limit=self._get_context_restore_limit(), limit_mode="latest")`。
- `src/maisaka/runtime.py:384-388` `_get_context_restore_limit`：`context_size = 300 if token 模式 else self._max_context_size`；`return max(1, ceil(context_size * CONTEXT_RESTORE_FILL_RATIO))`；`:94` `CONTEXT_RESTORE_FILL_RATIO = 0.5`。
- `src/maisaka/runtime.py:304-328` `build_reply_history`：回复器独立取数，`context_limit = max(1, int(self._max_context_size * 0.6))`（`:310`），同样 `find_messages(limit_mode="latest")`（`:311-316`），再经 `select_messages_after_latest_clear_marker` 过滤。

### 2.3 送入模型的历史窗口：从新往旧扫，`count_in_context` 计数

核心函数 `src/maisaka/chat_loop_service.py:1480-1574` `select_llm_context_messages`：

- `:1490-1493` 先按 request_kind 过滤（`:1640-1654` `_filter_history_for_request_kind`：expression_selector 只留 `SessionBackedMessage`；非 planner 去掉 `ContextSummaryMessage`）。
- `:1494` `base_context_size = max(1, int(max_context_size or global_config.chat.max_context_size))`；调用方 `src/maisaka/reasoning_engine.py:218-227` 明确传 `max_context_size=self._runtime._max_context_size`。
- `:1495-1498` `effective_context_size = max(MIN_RETAINED_CHAT_MESSAGES, int(base_context_size * CONTEXT_SELECTION_CACHE_STABILITY_RATIO))`；常量在 `:91` `CONTEXT_SELECTION_CACHE_STABILITY_RATIO = 2.0`，`MIN_RETAINED_CHAT_MESSAGES` 来自 `src/maisaka/context/post_processor.py:17`（值 5）。
- `:1499-1505` token 模式：group 用 `global_config.chat.use_token_limit`、private 用 `private_use_token_limit`，且仅 `planner`/`replyer` 生效；此时 `effective_context_size = max(5, len(filtered_history)+1)`，即**保留完整活动历史**，裁切交给模型返回的 token 用量触发。
- `:1514-1517` 常驻选中索引：`_collect_always_selected_reference_indices`（`:1620-1638`）把所有 `ContextSummaryMessage` 和 `CONTEXT_RESTORE` 类型 `ReferenceMessage` 长期随请求发送。
- `:1519-1534` **从最新往最旧遍历**（`range(len(filtered_history)-1, -1, -1)`），跳过 `build_context_items_from_history_entry(...) == ()` 的条目；每遇到 `message.count_in_context` 就计数，达到 `effective_context_size` 即停。
- `:1536` 把常驻索引与本次选中索引合并后排序，`:1541` 按索引还原为 `selected_history`。
- `:1545-1548` `_expand_selected_tool_turns`（`:1576-1608`）：预算命中工具循环时按 `logical_turn_id` 补回整轮，并补回该轮前最近的 user 锚点（`:1598-1601`）。
- `:1549-1558` 规范化 tool/result 配对后，若补齐导致超出窗口则记日志并允许超出（`tool_turn_overflow`）。
- `:1566-1574` 返回选中历史和可读的 `selection_reason`（实际发送条数 / 聊天消息数 / tool 数 / cache_window 或 token_limit / overflow）。

调用点：`src/maisaka/chat_loop_service.py:1185-1205` `chat_loop_step` 调 `select_llm_context_messages` 再 `_build_request_messages`，`selected_history_count` 在 `:1267`、`:1374`、`:1463` 写入 hook payload。

### 2.4 每条历史如何渲染进 prompt

`src/maisaka/chat_loop_service.py:1002-1139` `_build_request_messages`：

- `:1014-1057` 构造 system item（人设/昵称提示、工具目录、todo/探索上下文）。
- `:1074-1102` 逐条 `build_context_items_from_history_entry` 展开历史；`:1108` `history_item_count = len(items) - 1`。
- `:1110-1127` 历史之后追加当轮即时 user 消息：`injected_user_messages`、当前时间、`tail_user_messages`、当前聊天注意事项；每项都变成 `RoleType.User` 的 `ContextItem`。
- `:1129-1135` 末尾再追加可选的 final user message。

`src/maisaka/context/message_adapter.py:27-37` `format_speaker_content` 生成 `HH:MM:SS[msg_id:ID][说话人]正文` 前缀；`:71-131` `build_visible_text_from_sequence` 把各组件渲染为可见文本（见第 3 节引用消息）。

### 2.5 配置声明 vs 代码实际接线

| 配置项（声明） | 默认值与出处 | 代码实际是否调用 |
|---|---|---|
| `chat.max_context_size`（群聊） | `50`，范围 30–300，`src/config/official_configs.py:855-870` | 是。`src/maisaka/scenes/group/behavior.py:31-32` `max_context_size` → `global_config.chat.max_context_size` → runtime 与 chat_loop_service |
| `chat.max_private_context_size`（私聊） | `75`，范围 30–300，`src/config/official_configs.py:872-887` | 是。`src/maisaka/scenes/private/behavior.py:32-33` |
| `chat.use_token_limit` / `private_use_token_limit` | 默认 `False`，`src/config/official_configs.py:889-897`、`:914-922` | 是。`chat_loop_service.py:1499-1502`、`src/maisaka/runtime.py:224-228` |
| `chat.max_context_tokens` / `private_max_context_tokens` | `128000`，范围 16K–1M，`src/config/official_configs.py:899-912`、`:924-937` | 是。token 模式的阈值；`post_processor.py:57-63` 用 `prompt_tokens >= max_context_tokens` 触发裁切 |
| `plugin.disabled_commands`（含 `core.clear`） | 默认空列表，`src/config/official_configs.py:5600-5604` | 是。`src/chat/message_receive/bot.py:457,481` 用 `is_command_disabled(CLEAR_COMMAND_ID)`（`CLEAR_COMMAND_ID = "core.clear"`，`:57`） |

裁切触发与保留量：`src/maisaka/context/post_processor.py:17` `MIN_RETAINED_CHAT_MESSAGES = 5`、`:16` `TRIM_TARGET_RATIO = 0.2`；`:57-63` 条数模式在 `remaining_context_count >= max_context_size` 时触发，token 模式在 `prompt_tokens >= max_context_tokens` 时触发，目标保留量 `max(5, int(retained_base * 0.2))`。实际执行 `process_chat_history_after_cycle` 由 `src/maisaka/reasoning_engine.py:1435-1464` 在每轮后调用。

## 3. 摘要 / 记忆 / 引用机制

### 3.1 短期上下文压缩摘要（`ContextSummaryMessage`）

- `src/maisaka/context/compaction.py:74-83` `ContextSummaryMessage(ReferenceMessage)`，`source` 返回 `"context_compaction"`。
- `:184-223` `_build_compaction_request`：把 `previous_summary` 放在最前（`:197-198`），`message_text_for_compaction`（`:85`）逐条渲染被移除消息，prompt 名按是否有 source_context 选 `context_compaction_focus` 或 `context_compaction`（`:209`）。
- `:225-251` `_generate_compaction_summary`：用 `LLMServiceClient(task_name="planner", request_type="maisaka.context_compaction")` 生成。
- `:253-277` `build_context_summary`：生成后封装 `ContextSummaryMessage(content=rendered, timestamp=removed_messages[-1].timestamp)`。
- 触发在 `src/maisaka/reasoning_engine.py:1435-1464`：仅当 `process_result.removed_messages` 且 `enable_compaction` 时生成；`:1437-1439` 冷启动/摘要提交后必须先完成一次 Planner 请求（有 `planner_request_snapshot`）才生成；`:1457-1463` 生成期间若 `/clear` 改变了历史则丢弃旧快照。
- 注入：`chat_loop_service.py:1620-1638` 把所有 `ContextSummaryMessage` 作为 always-selected 随每次请求发送；非 planner 请求在 `:1651-1652` 被过滤掉。

### 3.2 长期记忆：A_memorix 写回 + 查询工具 + 启发式注入

**写回（默认开启）**：

- `src/services/memory_flow_service.py:524-619` `ChatSummaryWritebackService`：`:551-552` `enqueue` 检查 `global_config.a_memorix.integration.chat_summary_writeback_enabled`；`:605-606` `memory_service.ingest_summary(external_id=f"chat_auto_summary:{session_id}:{total_message_count}", ...)`。
- `:747-752` 阈值/上下文长度读取配置：`max(1, int(...chat_summary_writeback_message_threshold))` 与 `..._context_length`。
- 配置声明：`src/config/official_configs.py:1538-1549` `chat_summary_writeback_enabled` 默认 `True`；`:1551-1559` `chat_summary_writeback_message_threshold` 默认 `36`；`:1561-1570` `chat_summary_writeback_context_length` 默认 `36`。**这 36/36 是代码里声明的默认值，不是某个论文或人的工作记忆数字。**

**启发式拉起（默认关闭）**：

- `src/maisaka/memory/heuristic_injector.py:58-132` `build_injection_message`：`:65-68` 检查 `heuristic_memory_recall_enabled`，关闭则直接返回空串；`:75-79` 要求 `count_messages(session_id) >= window_size`；`:97-102` `find_messages(session_id=..., limit=window_size, limit_mode="latest", filter_command=True)`；`:114-118` 先用 fast_model 生成聊天印象，再 `memory_service.search(...)`；`:119-122` 格式化并限长。
- `:172-183` `_search_related_memory`：`limit = config.heuristic_memory_recall_limit`，`search_limit = limit*4`，`memory_service.search(limit=min(20, search_limit))`。
- `:368-376` `_format_reference` 的提示词文本明确标注为 "Internal long-term memory recalled from the current chat impression. Use it only as reasoning context; do not quote it verbatim to the user."。
- 配置声明与默认值（`src/config/official_configs.py`）：`:1378-1389` `heuristic_memory_recall_enabled = False`；`:1391-1402` `cross_chat_enabled = False`；`:1404-1418` `window_size = 20`；`:1420-1434` `limit = 3`；`:1436-1450` `max_chars = 900`；`:1452-1465` `min_interval_seconds = 180`；`:1467-1480` `min_new_messages = 60`；`:1482-1495` `cache_ttl_seconds = 300`。
- 实际接线（已核实）：`src/maisaka/reasoning_engine.py:280-299` 在 `_build_planner_injected_user_messages` 里调用 `heuristic_memory_injector.build_injection_message(session_id=...)`（含异常兜底）；注入消息经 `chat_loop_service.py:1199,1113,1122-1127` 进入请求的 user 消息。默认关闭时这条路径返回空串，不会注入。
- 另有 `query_memory` 内置工具（`src/maisaka/builtin_tool/query_memory.py`，Planner 可主动调用）与人物画像注入 `src/maisaka/memory/person_profile.py`（`reasoning_engine.py:97,301-309` 接线）。

### 3.3 引用消息如何进入 prompt（用户引用的消息）

两条独立路径，均已核实接线：

1. **引用目标正文进入可见消息文本**：`src/maisaka/context/message_adapter.py:125-130` 遇到 `ReplyComponent` 时先输出 `[引用消息]<display_id>`，并置 `pending_reply_body_prefix=True`，随后正文前加 `\n[发言内容]`。正文来源：`src/chat/message_receive/message.py:454-501` `process_reply_component`——`:459-465` 组件自带内容直接渲染 `[回复了<name>的消息: <content>]`；`:466-476` 先查 ID 映射缓存；`:477-495` 回退到 DB 查询 `select(Messages).filter_by(platform=self.platform, session_id=self.session_id, message_id=component.target_message_id).limit(2)`，命中则填 `target_message_content` 并渲染；`:499-501` 无法访问时渲染占位文本。`:260-261` 在组件处理分派中调用该方法。
2. **引用 ID 进入 Planner XML 元信息**：`src/maisaka/context/planner_messages.py:94-108` `extract_quote_ids_from_message_sequence` 提取所有 `ReplyComponent.target_message_id`；`:77-91` `_format_quote_ids` 去重后用逗号拼接；`:111-142` `build_planner_user_prefix_from_session_message` 把 `quote_ids` 传入 `build_planner_prefix`（`:138`），最终出现在 `<message ... quote_ids="...">` 属性中（`:31-40` 参数说明、`:74` 拼接）。

需要区分的第三个概念：`src/maisaka/quote/counter.py:19-80` `ReplyQuoteCounter` 与 `src/maisaka/quote/rules.py:15-26` `decide_quote` 决定的是**机器人自己是否引用某条历史消息**（`target_distance`、连发数、概率抽签），不是用户引用消息进入 prompt 的机制；二者不要混为一谈。

## 4. 显式换题命令接线核查（`/clear`）

结论：**`/clear` 是真正接到主链路的内置命令，不是文档里写着但没接线的声明。** 证据链完整：

1. 定义：`src/maisaka/context/clear_context.py:7` `CLEAR_CONTEXT_COMMAND = "/clear"`；`:8` marker key `maisaka_context_cleared`；`:14-17` `is_clear_context_command` 只认完整 `text.strip() == "/clear"`；`:20-25` `mark_clear_context_command` 写 `is_command=True` + marker + `intercept_message_level=1`；`:28-31` `is_clear_context_marker`；`:34-42` `select_messages_after_latest_clear_marker` 只保留最后一个 marker 之后且 `not is_command` 的消息。
2. 入站识别与权限：`src/chat/message_receive/bot.py:38-42` 导入三个符号；`:57` `CLEAR_COMMAND_ID = "core.clear"`；`:445-458` 命令识别分支（本地 operator 允许 `/clear <聊天名>`，普通用户必须完整 `/clear` 且未被 `disabled_commands` 停用）；`:471-574` `_process_clear_context_command` 完整实现：权限校验 `:491-507`（`has_command_permission("core.clear", ...)`）、`:484-489` 文本校验、`:559-561` 写 marker 到 DB + `heartflow_manager.clear_chat_history_context(message.session_id)`、`:563-567` 回复「我暂时失忆了」；控制台变体 `:511-557` 可对指定会话执行。
3. **主链路确实调用**：`src/chat/message_receive/bot.py:1010-1013`——在 `_process_adapter_lifecycle_command` 之后、插件命令分发（`:1017`）与 `heartflow_message_receiver.process_message`（`:1029-1037`）之前，`if await self._process_clear_context_command(message): return`。这是硬接线，不是靠插件命令目录转发（`src/core/commands/command_catalog.py` 未列出 `/clear`）。
4. 运行时生效：`src/chat/heart_flow/heartflow_manager.py:177-189` `clear_chat_history_context`：会话存在则 `_evict_chat(session_id, reason="clear_context_command")`，随后 `reset_planner_session(session_id)`；不存在时也清 focus 与 planner 状态。`:169-172` 淘汰时 `focus_mode_manager.clear_context`。
5. 重启后仍生效：DB 中持久化的 marker 会被 `src/maisaka/context/clear_context.py:34-42` 的过滤函数读取，调用点 `src/maisaka/runtime.py:317`（回复器历史）、`:347`（启动恢复）、`:427`（主动回合回填）。即清理边界跨进程重启仍有效。

是否有「用户显式换题但真正没接线」的命令：本次取证中**未发现**插件命令目录里有名为「换话题/新话题」的声明；`/clear` 是唯一找到的显式上下文清理命令，且已接线。配置侧 `plugin.disabled_commands` 可以把它停用（默认空）；这属于可配置的关闭开关，不是未接线。

## 5. 与 Ling 对比的可用 / 不可用结论

### 5.1 可直接借鉴（代码事实支撑）

- **会话 ID 由结构段显式构造**：MaiBot `utils_session.py:32-42` 把 platform、account、scope、群/私聊目标拼成 MD5；Ling 的 `window_id`/`subject_id` 体系也可对照检查「同一群不同账号/路由是否隔离」。可借鉴点是「ID 生成函数单一、所有入站点都调用它」这一接线强度（chat_manager / bot 均有调用）。
- **最近历史的「两层窗口」思路**：DB 层 `find_messages(limit=N, limit_mode="latest")` 先取最近 N 条（`message_repository.py:211-220`），模型层再从新往旧按 `count_in_context` 取窗口（`chat_loop_service.py:1519-1534`），并且 token 模式改为「保留完整活动历史 + 用真实输入 token 触发裁切」（`:1499-1505`，`post_processor.py:57-63`）。这个「条数窗口 / token 窗口双模式」对 Ling 的 `kernel/context.py` token 预算车道设计有对照价值。
- **清理边界持久化**：`/clear` 不只是清内存，还写 DB marker，重启恢复时过滤（`clear_context.py:34-42` + `runtime.py:317,347,427`）。Ling 若做「显式新话题」，这是可借用的关键做法：清理边界必须跨重启有效。
- **一次性注入项与历史项分界**：`chat_loop_service.py:1107-1127` 明确 `history_item_count` 之后的都是当轮即时提示（记忆、时间、注意事项），且工具调用锚点校验只针对历史段（`:1137-1147`）。Ling 的 prompt zone 也可对照「哪些是结构车道、哪些是注入车道」。

### 5.2 不可直接照搬 / 需谨慎

- **具体数字不能当 Ling 参数**：群 50 / 私聊 75（`official_configs.py:855-887`）、缓存稳定系数 2.0（`chat_loop_service.py:91`）、回复器 0.6（`runtime.py:310`）、压缩目标 0.2 / 最少 5（`post_processor.py:16-17`）、摘要写回阈值 36/36（`official_configs.py:1551-1570`）、启发式窗口 20 / 上限 3 / 900 字 / 180 秒 / 60 条 / TTL 300（`official_configs.py:1404-1495`）——这些都是 MaiBot 自己的默认配置，**不是 Ling 的判据**，也不来自任何论文或人的工作记忆。
- **启发式记忆默认关闭**（`official_configs.py:1378-1380`）。若把 MaiBot 当作「每轮自动注入长期记忆」的范例会误判：默认只走 `query_memory` 工具由 Planner 主动调用，自动拉起需显式开启且有冷却/新增消息阈值（`heuristic_injector.py:87-105`）。
- **摘要压缩依赖「先有一轮真实 Planner 请求」**（`reasoning_engine.py:1437-1439`）；冷启动时不会凭空生成摘要。对比 Ling 的滚动摘要写入时机时要保留这个前提。
- **引用计数器不是 prompt 注入**：`quote/counter.py` + `quote/rules.py` 是机器人自身引用行为的概率机制，不能当成「用户引用消息进入 prompt」的实现（后者在 `message_adapter.py:125-130` 与 `planner_messages.py:94-142`）。
- 本次为只读 API 取证，**未运行 MaiBot、未做动态验证**；上述都是静态代码接线结论，「配置默认关闭」与「函数存在」都已在文中区分标注。

## 6. 未取得证据与失败 URL

| 项 | 状态 | 说明 |
|---|---|---|
| 匿名 GitHub REST API | 失败 | `curl https://api.github.com/repos/Mai-with-u/MaiBot` → `API rate limit exceeded for 157.18.193.150`（已用 `gh api` 认证替代，所有元数据已取得） |
| `git clone` 仓库 | 失败 | `https://github.com/Mai-with-u/MaiBot.git` 直连 443 超时（`curl 28`, 75s），未取得完整本地工作树；改用 GitHub API 逐文件 base64 取回 |
| 动态运行验证 | 未做 | 未部署/运行 MaiBot；`/clear`、token 裁切、记忆注入均为静态代码路径核查，没有运行时日志或 trace |
| 历史窗口的实际 token 裁切效果 | 未实测 | `post_processor.py:57-63` 的触发条件已读到，但 `prompt_tokens` 来自模型实际返回用量，未做端到端请求验证 |
| `src/core/commands/pm_aliases.py`、`command_catalog.py` 全文 | 未逐行读完 | 只通过代码搜索确认 `/clear` 不在插件命令目录、且 `bot.py:1012` 直接调用；未穷举所有命令别名 |
| `src/maisaka/builtin_tool/query_memory.py` 全文 | 未逐行取证 | 仅确认文件存在及 `reasoning_engine.py`/Planner 工具注册路径；未在本文给出其内部差距/上限行号 |
| `docs/README_CN.md`、`README.md` | 已取回但无相关命中 | 搜索 `memory/记忆/上下文/context/摘要/clear/引用` 零命中；没有可引用的「文档声称」与代码对照点。官方 docs（如 `docs/chat-scenes.md:34-36`）提到「窗口计数、记忆查询规则、引用概率共用」，与代码结构一致，未发现明显文档与代码冲突 |

### 取证命令清单（可复现）

```bash
gh api repos/Mai-with-u/MaiBot --jq '{full_name,default_branch,homepage}'
gh api repos/Mai-with-u/MaiBot/commits/main --jq '.sha'
gh api repos/Mai-with-u/MaiBot/git/ref/tags/v1.4.8 --jq '.object.sha'
gh api 'repos/Mai-with-u/MaiBot/contents/src/common/utils/utils_session.py?ref=edf42446f123149c57fc79765dd5a8b004b8524c' --jq '.content' | base64 -d
gh api 'repos/Mai-with-u/MaiBot/contents/src/maisaka/chat_loop_service.py?ref=edf42446f123149c57fc79765dd5a8b004b8524c' --jq '.content' | base64 -d
gh api 'repos/Mai-with-u/MaiBot/contents/src/maisaka/runtime.py?ref=edf42446f123149c57fc79765dd5a8b004b8524c' --jq '.content' | base64 -d
gh api 'repos/Mai-with-u/MaiBot/contents/src/maisaka/context/clear_context.py?ref=edf42446f123149c57fc79765dd5a8b004b8524c' --jq '.content' | base64 -d
gh api 'repos/Mai-with-u/MaiBot/contents/src/chat/message_receive/bot.py?ref=edf42446f123149c57fc79765dd5a8b004b8524c' --jq '.content' | base64 -d
gh api 'search/code?q=is_clear_context_command+repo:Mai-with-u/MaiBot' --jq '.items[].path'
```
