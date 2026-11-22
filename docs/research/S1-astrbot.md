# S1：AstrBot 官方仓库源码取证

本轮为只读研究。核对对象是官方仓库 `github.com/AstrBotDevs/AstrBot`，不是本机运行的 AstrBot 实例，也不是任何插件仓库。所有行号都对应下方固定的 commit；结论区分「实测（读了源码）」「推理」「未核实」，不把推测写成事实。

## 1. 版本、复现命令与运行入口

| 项 | 值 | 来源 |
|---|---|---|
| 默认分支 | `master` | GitHub API `GET /repos/AstrBotDevs/AstrBot`（`default_branch`） |
| 核对 commit | `feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea` | GitHub API `GET /repos/AstrBotDevs/AstrBot/commits/master` |
| commit 日期 / 标题 | 2026-11-19T09:44:37Z / `feat(webui): add animated dual sidebars for chat (#11192)` | 同上 |
| tarball | `d29e2714b3670524fc322f5cad9046316dff07b821ce5911fde96bbeb38a2a00` | `shasum -a 256 astrbot.tar.gz`（隔离副本内） |
| 隔离副本 | `/tmp/astrbot-s1/feb3e82/repo` | 本轮创建；未改动 `/Users/lingxi/project/ling`（除本报告） |
| 运行入口 | `main.py:131-176`，`asyncio.run(main_async(...))` → `InitialLoader.start()`（`main.py:146-148`） | 源码实测 |

复现命令（在任意可联网机器执行；本机直连 github.com 超时，但 `api.github.com` 与 `codeload.github.com` 可达）：

```bash
curl -sS https://api.github.com/repos/AstrBotDevs/AstrBot | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["default_branch"])'
curl -sS https://api.github.com/repos/AstrBotDevs/AstrBot/commits/master | python3 -c 'import sys,json;print(json.load(sys.stdin)["sha"])'
mkdir -p /tmp/astrbot-s1 && cd /tmp/astrbot-s1
curl -sS -L -o astrbot.tar.gz https://codeload.github.com/AstrBotDevs/AstrBot/tar.gz/feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea
shasum -a 256 astrbot.tar.gz
tar -xzf astrbot.tar.gz && mv AstrBot-feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea repo
```

也可直接看网页版：`https://github.com/AstrBotDevs/AstrBot/blob/feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea/<路径>#L<行号>`。

## 2. 会话 / 聊天窗口如何标识与复用

### 2.1 平台聊天标识 → UMO（统一消息来源）

- 单条消息的会话标识由平台适配器写入 `AstrBotMessage.session_id`。OneBot V11（`aiocqhttp`）的规则（`astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:215-226`）：群消息取 `str(event.group_id)`，私聊取 `sender.user_id`。实测代码见同文件 156-160、181-185（请求/通知类事件同样规则）。
- `AstrMessageEvent.__init__` 把它组装成 `MessageSession`（`astrbot/core/platform/astr_message_event.py:128-134`）。`MessageSession.__str__` 输出 `platform_id:message_type:session_id`（`astrbot/core/platform/message_session.py:18-19`），这就是 `event.unified_msg_origin`（UMO，`astr_message_event.py:175-178`）。例：`aiocqhttp:GROUP_MESSAGE:100200300`（示例 QQ 号为文档假数据）。
- `event.session_id` 是裸的会话 ID（群号/好友号），`get_group_id()` 返回 `message_obj.group_id`（`astr_message_event.py:186-194, 319-321`）。`ProviderRequest.session_id` 参数已被标记为废弃（`astrbot/core/provider/provider.py:125,163`；`request_llm` 中亦注明“已经过时”，`astr_message_event.py:571`）。

### 2.2 内部 session / conversation / 最近历史 / 摘要与长期记忆的落点

| 概念 | 落点与字段 | 默认值 | 证据 |
|---|---|---|---|
| 会话窗口 | UMO 字符串（逻辑键），本身不是数据库行 | — | `message_session.py:18-27` |
| 当前选中对话 | 内存 `ConversationManager.session_conversations[umo]` + `data/shared_preferences.json` 的 `umo` scope / `sel_conv_id` | 无选中时 `None` | `conversation_mgr.py:40, 297-312, 349-363`；`shared_preferences.py:31-37, 413-415` |
| 对话元数据 | `conversations_v3` 表：`conversation_id`（uuid4）、`platform_id`、`umo`、`title`、`persona_id`、`created_at/updated_at` | `conversation_id` 默认 `str(uuid.uuid4())`，其余可空 | `astrbot/core/db/po.py:152-170` |
| 对话事件流 | `conversation_events` 表：`conversation_ref`、`seq`、`event_id`、`type`、`version`、`payload`、`created_at` | `version=1`，`payload={}` | `po.py:172-197` |
| 最近历史 | 兼容层 `Conversation.history`（JSON 字符串）；物理上是 `conversation_events` 的 `message.appended` 等事件投影，读取时 `conversation_store.read()` 拼回 `content` | `history=[]` 表示清空；`content=None` | `po.py:95-119`；`conversation_mgr.py:236-265`；`db/sqlite.py:690-714` |
| 摘要 / 压缩摘要 | 没有独立的「摘要表」。LLM 压缩把旧轮次总结为新消息后作为 `context.rebased` 事件写回同一对话事件流 | 触发阈值 0.82（`LLMSummaryCompressor.compression_threshold`） | `agent/context/compressor.py:121-166, 216+`；`db/conversation.py` 中 `conversation_revision_columns` / context.rebased 相关查询（如 `:693, :805`） |
| 群消息长期记录 | `platform_message_history` 表（独立表，非对话历史），由 `PlatformMessageHistoryManager` 管理 | 最多 700 条（配置项 `group_message_history_max_cnt=700`，代码下限 `max(1, ...)`） | `db/po.py:339+`；`platform_message_history_mgr.py`；`builtin_stars/astrbot/main.py:162-184` |

特别说明：`conversation_mgr.py:1-5` 的文档字符串明确写着「会话和对话是独立的」——群号识别的是会话窗口（UMO），一个窗口可有多个对话（不同 `conversation_id`），并可切换/删除。

## 3. 发给模型的完整上下文如何装配

主链路（全部为源码实测调用点，不是配置推测）：

1. `ProcessStage.process()`（`core/pipeline/process_stage/stage.py:33-152`）在唤醒/At 条件下调用 `AgentRequestSubStage` → `InternalAgentSubStage.process()`（`method/agent_sub_stages/internal.py:62-162`）。
2. `InternalAgentSubStage` 调用 `runtime.open_session(event.unified_msg_origin, ...)` 与 `session.run_turn(...)`（`internal.py:148-162`）。
3. 原生 runtime 每轮 `_prepare_turn()` 调用 `conversation_store.read_for_execution(self.id)` 装载**已提交的整个对话历史**，写入 `req.conversation` 与 `req.contexts`（`core/astr_agent_runtime/main_session.py:74-138`；`main_agent/request.py:296-322, 376-395`）。读取时不分页：`req.contexts = conversation.content or []`（`request.py:394`）。`conversation_mgr.get_human_readable_context()` 的分页（`page=1, page_size=10`，`conversation_mgr.py:528-579`）是给人类浏览用的，不是 LLM 上下文分页。
4. `build_main_agent()` 依次装配（`core/astr_agent_runtime/main_agent/builder.py:118-196`）：选择模型 → `request.prepare_request()`（收集当前输入/媒体/引用）→ 人格 system prompt（`persona.apply_persona`）→ 工作区说明 → 知识库 → 网络搜索提示 → 安全提示 → 工具/技能 → 末尾工具调用提示与 live 模式提示。
5. 当前用户消息的前后提示块（`core/agent_input.py`）：
   - 唤醒前缀剥离：`collect_initial_request()` 中 `req.prompt = event.message_str`，若以 `provider_wake_prefix` 开头则去掉前缀（`main_agent/request.py:94-103`）。
   - `prompt_prefix`：有 `{{prompt}}` 占位符则替换，否则前置拼接（`agent_input.py:58-71`，调用点 `:332`）。
   - 引用消息：`append_quoted_content()` 生成 `<Quoted Message>\n{发送者}: {文本}\n</Quoted Message>`，作为 `extra_user_content_parts` 的 `TextPart`（`agent_media.py:190-284`，调用点 `agent_input.py:321-330`）。引用图片/转发解析默认限制：链深 4、转发节点深 6、最多抓取 32 个转发节点（`utils/quoted_message/settings.py:7-9`；`config/default.py:170-176`）。
   - 系统提醒块：`identifier` / `group_name_display` / `datetime_system_prompt` 分别产生 `User ID: …`、`Group name: …`、`Current datetime: …`，合并为 `<system_reminder>…</system_reminder>` 用户内容块（`agent_input.py:74-120`，调用点 `:351`）。
   - 会话 ID 兜底：`req.session_id = event.unified_msg_origin`（`agent_input.py:349-350`）。
6. 发送前的历史压缩（`tool_loop_agent_runner.py:1204-1221` 调用 `ContextManager.process`）：
   - 先按轮次截断：`enforce_max_turns`（默认 -1 不启用）/ `truncate_turns`（默认 1）。
   - 再按 token 压缩：仅当 `max_context_tokens > 0` 且用量超过 82% 触发。默认 `max_context_tokens=0`、`max_turns=-1`、`trim_turns=1`、`keep_recent_ratio=0.15`、`fallback_max_tokens=128000`、`overflow_strategy=llm_compress`（`config/agent_runner.py:21-29, 102-134`；`agent/context/config.py:15-28`；`compressor.py:56-60, 121-166`）。策略 `llm_compress` 会调用 LLM 把旧轮次总结成摘要，保留最近比例（0.15，钳制到 0~0.3）的精确上下文；失败则退化成丢弃最老轮次（`agent/context/manager.py:94-179`）。
7. 「自动记忆」的真实形态：仓库里没有独立的长期记忆子系统（限定 Python 文件的全仓搜索 `long.?term|LTM|auto_memory|memor(y|ies)`，只命中 `agent/context/round_utils.py` 中英语普通词与无关文件，没有任何记忆存储/注入模块）。实际接线的两类机制是：
   - 群聊上下文 ICL：`group_icl_enable`（默认 `False`）开启时，`GroupChatContext.handle_message()` 在内存中按 UMO 缓存群消息（`group_chat_context.py:142-160`，容量默认 1000），`on_req_llm()` 在 LLM 请求钩子里把「上次回复之后的群聊上下文」以 `<system_reminder>…BEGIN CONTEXT…END CONTEXT…</system_reminder>` 注入（`group_chat_context.py:31-34, 162-197`）。钩子注册点：`builtin_stars/astrbot/main.py:324-333`，调用条件 `group_context_enabled()` 为 `group_icl_enable or active_reply.enable`（`main.py:133-140`）。这是**已接线**的自动上下文注入，但缓存是进程内存（重启即丢），不是持久化记忆。
   - 持久化群历史工具：`group_message_history_enable`（默认 `False`）开启时，入站群消息写入 `platform_message_history`（`builtin_stars/astrbot/main.py:146-186`），并给模型注册 `get_group_message_history` 工具（`builder.py:171-182`、`tools/message_tools.py:380-466`）。这是**模型按需查询**，不是每轮自动注入。

配置默认值实测（`astrbot/core/config/default.py:225-235`）：`group_icl_enable=False`、`group_message_max_cnt=1000`、`image_caption=False`、`group_message_history_enable=False`、`group_message_history_max_cnt=700`、`active_reply.enable=False`、`possibility_reply=0.1`。UI schema 的 condition 只在 `group_message_history_enable=True` 时展开上限字段（`default.py:4908-4922`）。

## 4. 「用户显式换题」命令是否真接到主链路

结论：**有，且接线完整**（不是只有配置 schema）。证据链：

| 命令 | 注册 | 处理函数 | 主链路调用 | 效果 |
|---|---|---|---|---|
| `/reset` | `builtin_stars/builtin_commands/main.py:44-48`（`@filter.command("reset")`，权限 `SHARED_GROUP_ADMIN`） | `commands/conversation.py:112-141` | `conversation_manager.update_conversation(umo, cid, history=[])` → `db/sqlite.py:690-714` 的 `conversation_store.append(..., history=content)`；第三方 runtime 走 `_clear_third_party_agent_runner_state()` | 清空当前对话上下文（事件流写入新的历史状态）；同时 `active_event_registry.stop_all(umo)` 停止该会话正在跑的任务 |
| `/new` | `builtin_stars/builtin_commands/main.py:55-59`（`@filter.command("new")`，权限 `SHARED_GROUP_ADMIN`） | `commands/conversation.py:164-197` | `conversation_manager.new_conversation(...)` → `db.create_conversation` + 写 `session_conversations[umo]` + `sp.session_put(umo, "sel_conv_id", cid)`（`conversation_mgr.py:267-299`） | 新建并切换到新对话 UUID，旧对话保留；继承当前人格；同样设定 `_clean_group_context_session=True` |
| `/stop` | `main.py:50-53` | `commands/conversation.py:143-162` | `active_event_registry.request_agent_stop_all(umo)` | 只停止运行，不清上下文 |

补充接线证据：
- 内置命令插件 `builtin_commands` 是核心内置 star（metadata: `astrbot/builtin_stars/builtin_commands/metadata.yaml`），不是用户可选插件；`transformer` 解析到命令 handler 后由 `ProcessStage` 的激活 handler 分支执行（`process_stage/stage.py:119-135`）。
- `/reset` 和 `/new` 都设置 `message.set_extra("_clean_group_context_session", True)`（`commands/conversation.py:138, 187`），`builtin_stars/astrbot/main.py:335-342` 的 `after_message_sent` 钩子据此清空该 UMO 的群聊 ICL 缓存——说明换题时连「自动记忆」的临时缓存也一起清。
- 首个会话建立：`main_agent/request.py:311-321` 中 `_get_session_conv()` 在 UMO 尚无 `sel_conv_id` 时自动 `new_conversation`，所以无需用户先 `/new` 才能聊天。

未核实：我没有在本机运行 AstrBot 实例做端到端实测（本轮限定只读源码取证，且按约定不连生产 NAS/容器）。上述「真接线」指源码层面命令→管理器→存储/运行时的完整调用链，不含运行期行为验证。

## 5. 证据强度表

表内的「实测」= 本轮在固定 commit 的隔离副本中读到源码/调用点；「推理」= 基于源码的逻辑推断；「未核实」= 本轮没有取证。

| # | 结论 | 强度 | 关键证据（commit `feb3e82` 内路径:行号） |
|---|---|---|---|
| 1 | 版本/commit/入口 | 实测 | `main.py:131-176`；GitHub API 与 tarball sha256（第 1 节命令） |
| 2 | OneBot 群号/好友号 → `session_id` → UMO `platform_id:message_type:session_id` | 实测 | `aiocqhttp_platform_adapter.py:215-226`；`astr_message_event.py:128-134,175-189`；`message_session.py:18-27` |
| 3 | UMO 选当前对话，`sel_conv_id` 持久化于 `shared_preferences.json`，对话 UUID 独立 | 实测 | `conversation_mgr.py:40,267-312,349-363`；`shared_preferences.py:31-37,413-415` |
| 4 | 历史存于 `conversation_events`，兼容层 `Conversation.history`；`conversation_id` 默认 uuid4 | 实测 | `db/po.py:95-119,152-197`；`conversation_mgr.py:236-265` |
| 5 | LLM 上下文从对话全量读取进 `req.contexts`，无服务端分页 | 实测 | `main_session.py:74-138`；`main_agent/request.py:376-395` |
| 6 | 人类可读上下文分页 `page=1/page_size=10` 不用于 LLM | 实测 | `conversation_mgr.py:528-579`；与第 5 条调用点不同 |
| 7 | 压缩/摘要策略接到发送前主链路；默认不压缩（`max_context_tokens=0`, `max_turns=-1`），阈值 0.82、保留比 0.15、兜底 128000 | 实测 | `tool_loop_agent_runner.py:1204-1221`；`config/agent_runner.py:21-29,102-134`；`agent/context/compressor.py:121-166`；`manager.py:94-179` |
| 8 | 引用消息以 `<Quoted Message>` 文本块进入用户内容 | 实测 | `agent_media.py:190-284`；`agent_input.py:321-330` |
| 9 | `<system_reminder>` 与 `prompt_prefix` 前后提示块接到主链路 | 实测 | `agent_input.py:58-71,74-120,332,351` |
| 10 | 群 ICL 注入经 `OnLLMRequestEvent` 接主链路；默认关闭；缓存仅内存 | 实测（默认值为实测；「重启即丢」为推理，依据 `defaultdict(deque)` 内存结构） | `builtin_stars/astrbot/main.py:133-140,324-333`；`group_chat_context.py:31-49,142-197`；`config/default.py:225-235` |
| 11 | 持久化群历史以工具形式供模型查询，默认关闭 | 实测 | `builder.py:171-182`；`tools/message_tools.py:380-466`；`builtin_stars/astrbot/main.py:146-186` |
| 12 | `/reset` 清空当前对话 history 并真正写存储 | 实测 | `builtin_commands/main.py:44-48`；`commands/conversation.py:112-141`；`conversation_mgr.py:454-494`；`db/sqlite.py:690-714` |
| 13 | `/new` 新建并切换对话，旧对话保留 | 实测 | `builtin_commands/main.py:55-59`；`commands/conversation.py:164-197`；`conversation_mgr.py:267-299` |
| 14 | 换题同时清群 ICL 临时缓存 | 实测 | `commands/conversation.py:138,187`；`builtin_stars/astrbot/main.py:335-342` |
| 15 | 运行期端到端行为（实际发一条消息看上下文拼成什么） | 未核实 | 本轮未运行实例；按约定未连生产 NAS/容器 |

## 6. 易误读点备忘

- `ProviderRequest.session_id` 出现多处，但源码注明废弃；真正主键是事件 UMO 与 conversation UUID（`provider.py:125,163`；`astr_message_event.py:571`）。
- 「有配置项」≠「接到主链路」：本轮对 `group_icl_enable`、`group_message_history_enable`、`compression.*`、`quoted_message_parser.*` 都找到了调用点；对 `/reset`、`/new` 找到了命令注册到存储的完整链。
- 另有 `enable_memory` 之类的通用记忆开关：在本 commit 中未找到（Python 文件全仓搜索），因此不要把第三方插件的记忆能力算到官方核心头上。
