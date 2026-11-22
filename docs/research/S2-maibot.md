# S2：MaiBot 官方仓库源码取证

取证时间：2026-11-22（Asia/Shanghai）。只读研究；官方仓库副本下载到 `/tmp/maibot-s2`，未改动 `/Users/lingxi/project/ling` 源码。

## 0. 仓库地址、版本与可复现命令

| 项 | 值 | 证据强度 |
|---|---|---|
| 任务书给的旧址 | `github.com/MaiMwithLove/MaiBot` → **404 Not Found** | 实测（`web_fetch` 调 `https://api.github.com/repos/MaiMwithLove/MaiBot` 返回 `{"message":"Not Found",...}`，HTTP 404） |
| 官方现址 | `https://github.com/Mai-with-u/MaiBot`（组织 `Mai-with-u`，7,094 stars，官网 `https://maibot.chat/`，默认分支 `main`） | 实测（`web_fetch` GitHub 搜索 API：`https://api.github.com/search/repositories?q=MaiBot+in:name&sort=stars&order=desc&per_page=10`，第一条为该仓库；`https://api.github.com/repos/Mai-with-u/MaiBot` 可读） |
| 取证 commit | `edf42446f123149c57fc79765dd5a8b004b8524c`（2026-11-20T08:01:22Z，message：`feat: 发布 1.4.8 与 WebUI 1.10.8 (#2497)`） | 实测（`https://api.github.com/repos/Mai-with-u/MaiBot/commits/main`） |
| 版本 | `pyproject.toml:7` → `version = "1.4.8"`；`src/config/config.py:73-74` → `CONFIG_VERSION = "8.16.37"`，`MODEL_CONFIG_VERSION = "1.17.30"` | 实测（隔离副本文件） |
| 运行入口 | `bot.py:1-8`：`from src.host.launcher import main; raise SystemExit(main(get_instance_root()))`；源码部署与 OneKey 共用 | 实测 |
| 隔离副本取得方式 | 本机直连 `git clone https://github.com/Mai-with-u/MaiBot.git` **超时失败**（`Failed to connect to github.com port 443 after 75003 ms`）；改用 API tarball 成功 | 实测 |

可复现命令：

```bash
# 本机 github.com:443 直连不通，api.github.com 可达；用 API tarball 取快照
curl -fL -o /tmp/maibot-s2.tar.gz \
  "https://api.github.com/repos/Mai-with-u/MaiBot/tarball/main"
mkdir -p /tmp/maibot-s2 && tar xzf /tmp/maibot-s2.tar.gz --strip-components=1 -C /tmp/maibot-s2

# 锁定版本
curl -fsSL "https://api.github.com/repos/Mai-with-u/MaiBot/commits/main" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['sha'], d['commit']['committer']['date'])"
```

下文所有路径如无特别说明，均相对隔离副本根 `/tmp/maibot-s2`。行号对应上述 commit。

---

## 1. 会话/聊天窗口标识与复用

### 1.1 平台聊天标识与内部 session_id

**内部会话 ID 的生成规则**（实测，`src/common/utils/utils_session.py:8-42`）：

```python
def calculate_session_id(platform, *, user_id=None, group_id=None, account_id=None, scope=None) -> str:
    route_components = []
    if account_id: route_components.append(f"account:{account_id}")
    if scope:      route_components.append(f"scope:{scope}")
    if group_id:   components = [platform, *route_components, group_id]
    else:          components = [platform, *route_components, user_id, "private"]
    return hashlib.md5("_".join(components).encode()).hexdigest()
```

即：群聊 `md5(platform[_account:X][_scope:Y]_group_id)`，私聊 `md5(platform[_account:X][_scope:Y]_user_id_private)`。`user_id`/`group_id` 二者皆无时抛 `ValueError`（:29-30）。注：**使用 MD5，但拼装含平台账号与路由 scope，不是单纯平台 user_id**。

**平台身份字段与默认值**（实测，`src/common/database/database_model.py:535-559`，`ChatSession` / 表 `chat_sessions`）：

| 字段 | 类型 | 默认值 | 行号 |
|---|---|---|---|
| `id` | `Optional[int]` 自增主键 | `None` | 540 |
| `session_id` | `str`，unique + index，max 255 | 必填 | 542 |
| `created_timestamp` | `datetime` | `default_factory=datetime.now` | 544-546 |
| `last_active_timestamp` | `Optional[datetime]` | `default_factory=datetime.now` | 547-549 |
| `user_id` | `Optional[str]` | `None`（nullable） | 552 |
| `user_nickname` / `user_cardname` | `Optional[str]` | `None` | 553-554 |
| `group_id` | `Optional[str]` | `None` | 555 |
| `group_name` | `Optional[str]` | `None` | 556 |
| `platform` | `str`，index，max 100 | 必填 | 557 |
| `account_id` | `Optional[str]` | `None` | 558 |
| `scope` | `Optional[str]` | `None` | 559 |

内存模型 `MaiChatSession`（`src/common/data_models/chat_session_data_model.py:9-73`）：构造时 `created_timestamp = created_timestamp or datetime.now()`（:33-34），`last_active_timestamp = last_active_timestamp`（可为 `None`，:35）；**群聊强制清空用户身份字段**——`user_id/user_nickname/user_cardname = None if group_id else ...`（:27-29），`group_name = group_name if group_id else None`（:30）；断言 `platform` 非空且 `user_id or group_id` 至少其一（:38-39）；`is_group_session = bool(group_id)`（:42）。`from_db_instance`/`to_db_instance`（:44-73）做双向映射。

### 1.2 会话复用（真实调用点）

`src/chat/message_receive/chat_manager.py`：

- `ChatManager.__init__`（:86-89）：`self.sessions: Dict[session_id, BotChatSession]`、`self.last_messages: Dict[session_id, SessionMessage]`、`_session_creation_lock = threading.RLock()`。
- `get_or_create_session`（:99-110）：`_get_or_create_session` 在锁内执行；注释明确「聊天和 WebUI 上传共用正式创建链路」。
- `_get_or_create_session`（:193-271）：先 `calculate_session_id`（:214-220）→ 内存命中则更新活跃时间、按 last_message 补身份、必要时落库并返回（:221-229）→ 内存未命中查 DB（`select(ChatSession).filter_by(session_id=...)`，:233-252）→ 都没有才新建并入库（:257-271）。**同 session_id 复用，不新建第二份。**
- `register_message`（:273-292）：按入站消息重算 session_id 写回 `message.session_id`，并存 `last_messages[session_id]`。
- `_update_session_identity`（:299-330）：群聊只更新群名并清空用户身份字段；私聊更新昵称/名片。
- `get_session_name`（:365-386）：群名优先，否则私聊 `{nickname}的私聊`。
- `load_all_sessions_from_db`（:332-340）启动时把 DB 会话全量载入内存；`regularly_save_sessions`（:342-353）默认每 300 秒落盘。

**结论（实测）**：会话复用是「平台聊天标识 → 确定性 MD5 session_id → 内存/DB 二级查找 → 命中则复用」。`account_id`/`scope` 参与 hash，因此**同一群在不同 bot 账号或不同路由 scope 下会得到不同 session_id**（这是路由隔离，不是换题机制）。

### 1.3 最近历史、摘要、长期记忆的存放

| 项 | 存放位置/字段 | 默认值/形态 | 证据强度 |
|---|---|---|---|
| 最近历史（DB 持久） | `Messages.session_id`（`src/common/database/database_model.py:77`）等，`src/common/message_repository.py:22` 将别名 `chat_id` 映射到 `Messages.session_id`；查最近消息用 `find_messages(..., limit=window_size, limit_mode="latest", filter_command=True)`（`heuristic_injector.py:92-100`） | 按条数取最新 | 实测 |
| 最近历史（运行时） | `MaisakaRuntime._chat_history: list[LLMContextMessage]`（`reasoning_engine.py` 多处，如 :220、:1421） | 内存列表，随轮次裁切 | 实测 |
| 摘要 | `ContextSummaryMessage`（`src/maisaka/context/compaction.py:73-83`），`display_prefix = "【上下文状态摘要-内部参考】"`，`source = "context_compaction"`，`remaining_uses_value: int \| None = None`（不占近期消息条数） | 内容由 `ConversationState`（:48-70）五类数组 `topics/background/progress/pending/agreements` 渲染 | 实测 |
| 长期记忆 | A_Memorix 子系统（`Config.a_memorix`，`src/config/config.py:137-138`；host service `src/A_memorix/host_service.py`；工具 `search_memory`/`ingest_summary`/`maintain_memory`/`memory_stats`，`src/A_memorix/plugin.py:93、135、239、261`） | 独立 SQLite/向量库，非 ChatSession 字段 | 实测（字段与调用面）；「长期记忆文件内部 schema」未逐项核实 |

---

## 2. 发给模型的上下文装配

### 2.1 历史分页与裁切（实测）

入口 `src/maisaka/context/post_processor.py:31-85` `process_chat_history_after_cycle(...)`，由 `src/maisaka/reasoning_engine.py:1413-1475` `_post_process_chat_history_after_cycle` 每轮调用（**已接线**）：

- 常量：:15-17 `TRIM_TARGET_RATIO = 0.2`、`MIN_RETAINED_CHAT_MESSAGES = 5`。
- 触发条件（:57-60）：`prompt_tokens >= max_context_tokens`（当 `max_context_tokens is not None`）或 `remaining_context_count >= max_context_size`。
- 裁切目标（:61-68）：`target_context_count = max(5, int(retained_base * 0.2))`，即达到上限后**只保留约 20%（至少 5 条真实聊天消息）**。
- `_trim_history_to_context_target`（:113-177）：取连续历史前缀；保护最近 5 条真实收发消息、最近用户回合、未完成的工具调用；摘要消息（ContextSummaryMessage）不被裁掉，保留在历史头部（:175-176）。
- 配置默认值（`src/config/official_configs.py:850-896`）：群聊 `max_context_size=50`（min 30/max 300）、私聊 `max_private_context_size=75`；`use_token_limit=False`、`max_context_tokens=128000`（ge 16000/le 1000000）；私聊同构 `private_use_token_limit=False`、`private_max_context_tokens=128000`。场景读取见 `src/maisaka/scenes/group/behavior.py:31-32`、`src/maisaka/scenes/private/behavior.py:32-33`。
- `reasoning_engine.py:225、267、379、1424-1426` 把 `_max_context_size`/`_max_context_tokens` 传入 `chat_loop_step` 与 `select_llm_context_messages`——**已接线**。

### 2.2 摘要

- 生成：`src/maisaka/context/compaction.py:253-277` `build_context_summary(...)`；请求由被裁掉的旧消息 + 上一份摘要 + 近期消息组成（:184-222），任务提示词 `prompts/.../context_compaction.prompt`（`load_prompt("context_compaction")`，:209-211）；模型输出必须是 5 类 JSON（:31-45，:242 `validate_llm_json(result.response, ConversationState).render()`）；失败时追加 `COMPACTION_RETRY_INSTRUCTION` 再试一次（:270-276）。
- 接线：`reasoning_engine.py:1435-1475`。条件：有被裁消息且 `enable_compaction=True` 且 `planner_snapshot` 存在；结果作为摘要置于历史最前（:1464-1467）。压缩失败时直接裁掉旧原文、保留上一份有效摘要（:1453-1454、:1473）。**已接线（实测有调用点与落库动作）**。
- 注意：`enable_compaction` 是函数参数，默认 `True`（:1417）；调用方是否在某些路径传 `False` 未逐调用点穷举 —— 此处标为**未核实**（但主路径 `_post_process_chat_history_after_cycle` 的常规调用不带该参数，故默认开启）。

### 2.3 长期记忆注入

- 实现：`src/maisaka/memory/heuristic_injector.py`。`build_injection_message(session_id)`（:47-137）：受 `global_config.a_memorix.integration.heuristic_memory_recall_enabled` 控制；消息数需 ≥ `heuristic_memory_recall_window_size`；按 `min_interval_seconds`/`min_new_messages` 节流（:148-161）；取最近 N 条（`find_messages(..., limit=window_size, limit_mode="latest", filter_command=True)`）→ fast_model 生成「聊天印象」→ `memory_service.search(impression, ...)` → 按 scope/person 过滤 → 格式化为 `HEURISTIC_MEMORY_REFERENCE_MARKER = "【启发式记忆-内部参考】"` + 英文指令「Internal long-term memory recalled ... do not quote it verbatim」（:367-387），每条内容截断 180 字符，总长受 `heuristic_memory_recall_max_chars` 限制。
- **已接线**：`src/maisaka/reasoning_engine.py:89` 导入 `heuristic_memory_injector`；:280-370 `_build_planner_injected_user_messages` 中 :292-299 调用 `build_injection_message`，:338-342 与人物画像/图片记忆并发执行，:365-369 把结果 append 到 `injected_messages`；随后 `injected_user_messages` 经 `chat_loop_step`（`reasoning_engine.py:222`）→ `src/maisaka/chat_loop_service.py:1009-1010、1113-1115` 作为 User 角色消息进入发给模型的 context items。
- 图片记忆同构（`src/maisaka/memory/image_injector.py`，调用点 `reasoning_engine.py:311-319`）；人物画像注入 `:301-309`。

### 2.4 回复提示词：历史、引用、当前消息前后提示块

核心文件 `src/chat/replyer/reply_prompt.py`：

- 入口 `_build_reply_request_messages`（:92-209）：产出 2 条上下文——System（人设 + 注意力/漂移块）与 User（回复模板展开）。System 模板按场景选 `reply_{group|private}_system` 或 `reply_mai_replier_{...}_system`；Mai Replier 路径人设为唯一 System 内容（:138-145）。
- 聊天记录：`_build_reply_dialogue_block`（:434-450）逐条 `_render_reply_history_line`（:452-481）渲染为 `[时间] 说话人说：内容`；`guided_reply` 标为 `{bot_name}(你)`，自己的发言标为「你」（:469-473）；时间格式 `mode="normal_no_YMD"`（:475）。
- 引用消息：`_build_reply_references`（:483-514）。遍历 `ReplyComponent`，优先从当前上下文按 `target_message_id` 找原消息（:494-499）；找不到则用组件自带 `target_message_sender_*` / `target_message_content`（:500-506），**不查数据库**；输出 `[回复了{name}的消息: {content}]`。同一函数同时服务历史行（:465）与当前回复目标（:417）。
- 当前消息：`_build_reply_target_block`（:397-432），`REPLY_TARGET_CONTENT_LIMIT = 300`（:58-59）截断；输出「现在{sender}{reference} 说：{target} 引起了你的注意」；机器人自己的发言走「补充说明你刚刚自己的发言」分支（:426-429）。
- 前后提示块（User 模板展开，`_build_reply_template_context` :308-342）：
  - 模板头 `{extra_info_block}{expression_habits_block}`（`prompts/zh-CN/main/reply/group/reply_group.prompt:1`）——`_build_reply_extra_info_block`（:362-373）用「以下是你在回复时需要参考的信息…」包裹本轮回复要求/重试约束/插件追加信息；`expression_habits_block` 在 Mai Replier 路径被置空（:334-336）。
  - `{time_block}` = `当前时间：YYYY-MM-DD HH:MM:SS`（:339）。
  - `{dialogue_prompt}` = 历史对话块（:338、:434-450）。
  - `{reply_target_block}`、 `{planner_reasoning}`（= 回复原因 + `chat_anlyze` + 表达倾向 guidance，:322-324、:341）、`{reply_guidance}`（:161-174）、`{reply_style}`（:329、:333）。
  - 模板尾（zh-CN group prompt :11-12）：「请注意不要输出多余内容…只输出发言内容就好。现在，你说：」——这是模型输出约束块。
  - 视觉输入开启时（`enable_visual_message`，:117-119、:176-203）：图片按 `visual.max_image_num` 限额，最近 3 张长边 640、更早 128（`REPLY_RECENT_IMAGE_COUNT=3`，:61-63；:235-247），图片插在对应历史发言处，模板后半段指令始终在最后（:197-202）。
- 已过滤项：`_is_replyer_filtered_history_message`（:111-116）丢弃 Planner 内部输出；保留 `guided_reply` 这类真实已发消息。
- 证据强度：以上均为**实测**（代码与模板原文）；「线上实际请求是否包含某块」取决于运行时开关，未做实机抓包，属**未核实**。

---

## 3. 用户显式换题/话题切换命令与配置

### 3.1 结论

**存在显式命令 `/clear`，且已完整接入主链路（不是"有配置但未接线"）。** 它清空的是「当前聊天流的 Maisaka 短期上下文/运行时」，并在数据库留清理边界；**不是改 session_id、不是新建会话、也不删长期记忆**。未发现名为"换话题/话题切换"的独立用户命令或专门配置。

### 3.2 接线证据链（实测，逐跳）

1. 命令定义：`src/maisaka/context/clear_context.py:7-9` `CLEAR_CONTEXT_COMMAND = "/clear"`、`CLEAR_CONTEXT_MARKER_KEY = "maisaka_context_cleared"`、`CLEAR_CONTEXT_INTERCEPT_LEVEL = 1`。`is_clear_context_command`（:14-17）要求整条消息文本恰为 `/clear`；`mark_clear_context_command`（:20-25）置 `is_command=True` 并写 marker 到 `additional_config`；`is_clear_context_marker`（:28-31）用于识别 DB 中的清理边界。
2. 注册/启用开关：`src/chat/message_receive/bot.py:57` `CLEAR_COMMAND_ID = "core.clear"`；:60-70 `is_command_disabled()` 查 `global_config.plugin.disabled_commands`；`src/config/official_configs.py:5600-5604` `disabled_commands: List[str] = Field(default_factory=list)` —— **默认空，即默认启用**。权限用 `core.clear`（bot.py:491-500），未授权时回「你没有权限使用此命令。」（:503-506）。
3. 主链路调用点：`src/chat/message_receive/bot.py:1010-1012`（`_process_clear_context_command(message)` 在命令分发之前执行，`return` 后不再落入 HeartFlow/MaiSaka）；实现在 :471-574：
   - :484-489 校验命令文本；
   - :559-561 普通用户路径：`mark_clear_context_command(message)` → `MessageUtils.store_message_to_db_async(message)`（写 marker 入 DB）→ `await heartflow_manager.clear_chat_history_context(message.session_id)`；
   - :563-567 回复「我暂时失忆了」；
   - 控制台路径 :511-557 支持 `/clear <聊天名>`，按 `chat_manager.get_named_session_options()` 解析目标 session_id，写 marker 到目标会话并清空其运行时。
4. 运行时清理：`src/chat/heart_flow/heartflow_manager.py:177-189` `clear_chat_history_context(session_id)`：若会话在运行 → `_evict_chat(..., reason="clear_context_command")`（:170-172 调 `focus_mode_manager.clear_context`）→ `await reset_planner_session(session_id)`；若不在运行也清 focus + planner state（:182-185）。`src/agent/planner_state.py:88-96` `reset_planner_session` 删除 planner 状态文件，注释「解除绑定后下一次输入建立新的空会话」。
5. 历史恢复过滤：`src/maisaka/runtime.py:43` 导入 `select_messages_after_latest_clear_marker`；:317、:347、:427 三处调用。`clear_context.py:34-42` 该函数只保留**最后一次 `/clear` marker 之后**的非命令消息。因此：DB 里旧消息仍在，但**恢复到 Maisaka 上下文时被截断**，短期记忆不会复活。
6. 命令补全：`src/cli/bot_console.py:134、162、245` 控制台支持 `/clear <聊天名>` Tab 补全。

### 3.3 与"有配置但未接线"的区分

| 形态 | 本仓库实例 | 判定 |
|---|---|---|
| 有配置但未接线 | 本任务未发现这种形态的换题功能。反例：`src/config/official_configs.py:5600` 的 `disabled_commands` 只是**开关**，若只看它会以为"只是配置"；但调用点 `bot.py:457/481/1012` 实打实读取并分发 → 已接线 | 已接线（实测） |
| 已接线 | `/clear`：定义 → 注册开关 → 入站拦截 → DB marker → 清运行时 → 历史恢复过滤，六跳均有代码调用点 | 已接线（实测） |
| 相关但非换题 | `src/maisaka/attention_drift.py:30-47` `ANCHOR_POLICY_RULES`（strict/balanced/loose）写「不能凭空换话题」；`official_configs.py:963` `更容易被有趣的新话题…吸引`。这是**风格/注意力实验配置**，不是用户显式换题命令 | 有配置，但语义不是话题切换（实测代码文本）；其是否接线未逐项核实 |

未核实项：是否存在第三方插件提供的"换话题"命令（不在本仓库核心代码范围）；`/clear` 之外是否有 WebUI 一键清空入口的端到端调用（`src/webui/routers/chat/routes.py:1788` 有 `chat_history.clear_history` 调用，但其与 Maisaka 运行时的关系未逐条追踪）——均标为**未核实**，不作事实陈述。

---

## 4. 证据强度汇总

| 结论 | 强度 | 依据 |
|---|---|---|
| 官方现址、commit、版本、入口 | 实测 | GitHub API + 隔离副本文件 |
| session_id MD5 规则、ChatSession 字段/默认值、复用链路 | 实测 | `utils_session.py`、`database_model.py`、`chat_manager.py` 原文 |
| 历史分页阈值（50/75、20%、min 5）与接线 | 实测 | `official_configs.py`、`post_processor.py`、`reasoning_engine.py` |
| 摘要 5 类 JSON 与接线 | 实测 | `compaction.py`、`reasoning_engine.py:1435-1475` |
| 长期记忆 marker 与注入接线 | 实测 | `heuristic_injector.py`、`reasoning_engine.py:280-370`、`chat_loop_service.py:1113` |
| 引用消息不查 DB、目标消息 300 字截断 | 实测 | `reply_prompt.py:397-514` |
| `/clear` 全链路已接线、默认启用 | 实测 | 定义、注册、入站调用、运行时清理、恢复过滤五处调用点 |
| `/clear` 之外的换题命令/配置、线上实际请求内容 | 未核实 | 未穷举插件/未抓包 |
| attention_drift 配置是否接线 | 未核实 | 仅见配置与提示词文本，未追调用点 |

（报告完）
