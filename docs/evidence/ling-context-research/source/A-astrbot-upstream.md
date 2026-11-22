# A. AstrBot 官方仓库取证（上游 Context / 会话 / 历史窗口机制）

> 本文件为 Ling context research 的 Group A 交付物。仅使用官方来源：
> 上游 GitHub 仓库 `AstrBotDevs/AstrBot` 与官方文档站 `docs.astrbot.app`。
> 所有代码引用均钉在同一 commit；**观测（带行号证据）与推断（标注置信度）严格分开**。
> 未连接 NAS / 生产库，未部署插件，未运行 pytest。

---

## 0. 观测基线（逐字抄录）

按任务要求，先执行：

```
$ cd /Users/lingxi/project/ling && git status --short && git log -1 --format='%H %ad %s' --date=iso-strict
?? docs/evidence/ling-context-im-group.md
?? docs/evidence/ling-context-im-private.md
?? docs/research/
de43cbe79a78e0c01f7821470500eaa04ea79497 2026-11-22T09:17:29+08:00 feat(live): 合入直播工作台与舞台控制
```

- **基线 HEAD 符合要求**：`de43cbe79a78e0c01f7821470500eaa04ea79497`（2026-11-22T09:17:29+08:00）。
- **无关未跟踪路径（未触碰）**：`?? docs/evidence/ling-context-im-group.md`、`?? docs/evidence/ling-context-im-private.md`、`?? docs/research/`。
- 观测时间：2026-11-22 上午（Asia/Shanghai）。

---

## 1.  inspected 版本 / 发布 / commit（证据）

| 项 | 值 | 证据 URL |
| --- | --- | --- |
| 官方仓库 | `AstrBotDevs/AstrBot`（canonical） | https://github.com/AstrBotDevs/AstrBot |
| 最新 release（GitHub API，`releases/latest`） | `v4.30.0-beta.1`，`prerelease:false`，`draft:false`，发布 2026-11-18T05:17:05Z | https://api.github.com/repos/AstrBotDevs/AstrBot/releases/latest |
| 本报告的 pinned commit（tag 所指） | `90ba3270f09b857063da68908d80f742042067cd`，`chore: bump version to 4.30.0-beta.1 (#11233)`，作者日期 2026-11-18T05:13:12Z | https://api.github.com/repos/AstrBotDevs/AstrBot/git/ref/tags/v4.30.0-beta.1 、https://api.github.com/repos/AstrBotDevs/AstrBot/git/commits/90ba3270f09b857063da68908d80f742042067cd |
| 观测时 master HEAD（参考，不作为 pin） | `feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea`（2026-11-19T09:44:37Z） | https://api.github.com/repos/AstrBotDevs/AstrBot/commits/master |

**说明（观测）**：本报告中一切源码行号均来自 tag `v4.30.0-beta.1`（= commit `90ba3270...`）的 `raw.githubusercontent.com` 原始文件；每个引用形如
`https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/<path>`，并标注行号。
**限制（声明）**：本地 `git clone --depth 1 --branch v4.30.0-beta.1` 尝试因 `github.com:443` 连接超时失败（已知网络问题），因此代码通过 raw 文件逐个获取；GitHub REST API 曾触发 403 rate limit，但上述关键响应均已成功取得。除此之外无不成功获取的关键来源。

发布说明要点（证据：release body，同上 URL）：v4.30.0-beta.1 的 alpha.1 主要更新含「将会话后端迁移到事件溯源方式」「引入新的 Agent 运行时模型」；beta.1 含「减少上下文 Token 估算与截断的重复计算」「支持 Gemini 历史消息中的 URL 音频处理」等。这意味着**当前上游主线已是 v4 的 event-sourcing 会话 + 新 Agent runtime**，与旧版 v3 的 `provider_ltm_settings` 会话历史机制并存。

---

## 2. 会话键 / 对话身份（Session identity）

### 2.1 统一消息来源 UMO = `platform_id:message_type:session_id`

- 证据 `astrbot/core/platform/message_session.py:6-27`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/platform/message_session.py ）：
  - `:7` `class MessageSession`，字段 `platform_name`、`message_type`、`session_id`、派生 `platform_id`；
  - `:18-19` `def __str__` 返回 `f"{self.platform_id}:{self.message_type.value}:{self.session_id}"`；
  - `:21-22` `__post_init__` 令 `platform_id = platform_name`（docstring `:13` 明说「自 v4.0.0 起该字段实际为 platform_id」）；
  - `:24-27` `from_str()` 以 `split(":", 2)` 解析。
- 证据 `astrbot/core/platform/astr_message_event.py:127-132`（同 raw 基址）：事件构造时
  `self.session = MessageSession(platform_name=platform_meta.id, message_type=message_type, session_id=session_id)`。
- 证据 `astrbot/core/platform/astr_message_event.py:176-179`：`unified_msg_origin` property 返回 `str(self.session)`，docstring 明写格式 `platform_name:message_type:session_id`。

**观测结论**：AstrBot 用一个三段字符串统一标识「会话」= **平台适配器实例 id + 消息类型（群/好友/…）+ 平台侧 session id**。`user_id` 不是键的独立一段；它在平台侧 session_id 里体现（例如群消息 session_id 可为群号、好友消息为 QQ 号——此具体映射由各平台适配器决定，见 2.3 的限制说明）。

### 2.2 会话键在持久化与运行时的使用

- 证据 `astrbot/core/conversation_mgr.py:269-299`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/conversation_mgr.py ）：`new_conversation(unified_msg_origin, ...)` 里 `parts = unified_msg_origin.split(":")`，并把 `user_id=unified_msg_origin` 写入 conversation 记录，最后写入 `session_conversations[unified_msg_origin]` 与 `sp.session_put(unified_msg_origin, "sel_conv_id", conv.conversation_id)`。
- 证据 `astrbot/core/conversation_mgr.py:349-363`：`get_curr_conversation_id(unified_msg_origin)` 先从内存映射取，再从持久化 `sel_conv_id` 读。
- 证据 `astrbot/core/agent/runtime.py`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/agent/runtime.py:324-378 ，grep 行号）：`AgentSession` 以 `(umo, session_id)` 为身份，`self.id = session_id`；`input.event.unified_msg_origin != self.umo` 时拒绝混用。
- 证据 `astrbot/core/astr_agent_runtime/runtime.py:156-252`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/astr_agent_runtime/runtime.py ）：`open_session()` 绑定当前 main conversation 或按显式 `session_id` 恢复；`:251-252` 检查 `conversation.umo != umo` 则拒绝。

**默认行为（观测）**：
- 同一 UMO（同一平台实例 + 同消息类型 + 同 session_id）默认复用同一条「当前对话」conversation（由 `sel_conv_id` 记录）。
- `platform_settings.unique_session` 默认 `False`（`astrbot/core/config/default.py:82`），即**默认不按消息发送者再细分会话**（群聊里所有人共享一个 conversation）；若开启则按更细粒度隔离（该开关的消费点在平台配置层，未在本次 pin 文件中逐行追到——标为未核实）。

### 2.3 限制说明

- 「每个平台的 session_id 具体如何取值（群号 / 用户 id / 频道 id）」由各平台适配器构造，本次只取证了核心 `MessageSession` 与事件的入参，**没有逐个平台适配器核对**（未核实，置信度低；如需请以各 `*platform*.py` 的 `session_id=` 传参为准）。

---

## 3. 历史窗口：默认多少条、配置键、默认值、主链路读取点

### 3.1 关键区分：AstrBot 不是「固定 N 条消息窗口」模型

**观测（强证据）**：上游 v4 主链路传给 LLM 的是「该 conversation 当前已提交的全部 history」，再在**请求发送前**按 **token 窗口（~82% 阈值）** 或「轮数上限」做压缩/截断；并不存在「只取最近 K 条消息」的硬编码滑动窗口默认值。

- 证据 `astrbot/core/astr_agent_runtime/main_session.py:114-138`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/astr_agent_runtime/main_session.py ）：
  - `:116` `if not explicit or req.conversation is not None:`；
  - `:117` `req.conversation = manager.to_legacy_conversation(conversation)`；
  - `:118` `req.contexts = []`；
  - `:120` `for message in [*(conversation.content or []), {"role": "user"}]` —— **遍历整段 `conversation.content`**，逐条追加到 `req.contexts`（`:137`），`:138` 弹出哨兵。
- 证据 `astrbot/core/astr_agent_runtime/main_agent/request.py:389-395`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/astr_agent_runtime/main_agent/request.py ）：`prepare_request()` 中 `req.contexts = conversation.content or []`（`:394`），同样取整段历史。
- 证据 `astrbot/core/agent/runners/tool_loop_agent_runner.py:400`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/agent/runners/tool_loop_agent_runner.py ）：`messages = load_messages(request.contexts or [])` —— 把上述整段历史加载为 working messages。

### 3.2 压缩/截断配置键与默认值（配置层）

| 配置键（点分路径） | 默认值 | 含义 | 证据 |
| --- | --- | --- | --- |
| `agent.runtime` | `"astrbot"` | 内置 Agent runtime | `astrbot/core/config/default.py:207-210` |
| `agent.config.compression.max_turns` | `-1` | 「压缩前最多保留对话轮数」；`-1` 表示不按轮数限制 | `astrbot/core/config/agent_runner.py:21-28`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/config/agent_runner.py ） |
| `agent.config.compression.trim_turns` | `1` | 超限一次丢弃轮数 | 同上 `:23` |
| `agent.config.compression.overflow_strategy` | `"llm_compress"` | 压缩策略（`llm_compress` / `truncate_by_turns`） | 同上 `:24` |
| `agent.config.compression.keep_recent_ratio` | `0.15` | LLM 压缩时保留最近上下文比例 | 同上 `:26` |
| `agent.config.compression.provider_id` | `""`（空=用当前对话模型） | 压缩模型 | 同上 `:27` |
| `agent.config.compression.fallback_max_tokens` | `128000` | 模型窗口未知时的兜底 | 同上 `:28` |
| 模型侧 `max_context_tokens` | `0` | 模型上下文窗口；0=查元数据或兜底 | `astrbot/core/config/default.py:3044-3056` 附近 metadata；官方文档 https://docs.astrbot.app/use/context-compress.html |

官方文档交叉印证（**明确说明默认行为与本次 pin 代码一致**）：https://docs.astrbot.app/use/context-compress.html
- 「新配置文件默认使用『由 LLM 压缩上下文』」
- 「『压缩前最多保留对话轮数』默认 `-1` 表示不按轮数限制」
- 「『压缩时保留最近上下文比例』默认 `0.15`」
- 「AstrBot 会在每次向模型发送请求之前检查上下文……当上下文 Token 用量超过模型窗口的约 **82%** 时，会触发所选处理方式」
- 该 82% 在代码中亦有硬证据：`astrbot/core/agent/context/compressor.py:72-99` 与 `:137-184`，`compression_threshold: float = 0.82`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/agent/context/compressor.py ）。

### 3.3 WHERE：配置在主请求路径中「真正被读取并应用」的调用链（逐跳证据）

这是本任务最关键的一问：**配置不是只在 schema 里声明，而是确实接在主链上**。逐跳列举（每条均有文件+行号+URL）：

1. **配置解析（schema → 结构）**
   `astrbot/core/config/agent.py:15-38` `get_runtime_config_default()` 走 `get_agent_runner_config_default("astrbot")` 生成带 compression 默认值的 dict（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/config/agent.py ）。
2. **映射为 runner 构建参数**
   `astrbot/core/config/agent_runner.py:102-134` `resolve_context_compression_config(compression_config)`：
   - `:115` `max_turns = compression_config.get("max_turns", -1)`
   - `:116` `trim_turns = compression_config.get("trim_turns", 1)`
   - `:118-120` 计算 `dequeue_turns`
   - `:121-134` 返回 dict：`context_limit_reached_strategy`、`llm_compress_instruction`、`llm_compress_keep_recent_ratio`、`llm_compress_provider_id`、`max_context_length=max_turns`、`dequeue_context_length=max(1,dequeue_turns)`、`fallback_max_context_tokens`。
3. **进入会话级构建配置**
   `astrbot/core/astr_agent_runtime/main_agent/config.py:84-140` `MainAgentBuildConfig.from_config(config)`:
   - `:98` `runner_config = config.get("agent", {}).get("config", {})`
   - `:127` `**resolve_context_compression_config(runner_config.get("compression", {}))` —— **配置真正被展开进 build config**。
4. **Pipeline 入口（platform 消息 → Agent 子阶段）**
   `astrbot/core/pipeline/process_stage/method/agent_request.py`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/pipeline/process_stage/method/agent_request.py ）：`AgentRequestSubStage.initialize/process` 选择 `InternalAgentSubStage`（默认 runtime=`astrbot`），`config = self.ctx.plugin_manager.context.get_config(umo=event.unified_msg_origin)` 取**会话级配置**。
5. **Agent 子阶段把配置交给 runtime**
   `astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py:127-154`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py ）：
   - `:127-137` `prepare_agent_input(...)`；
   - `:145-154` `session = await runtime.open_session(event.unified_msg_origin, input=..., options={"config": deepcopy(agent_config.get("config", {}))} ...)`；`turn = await session.run_turn(...)`。
6. **主会话装载历史**
   `main_session.py:57-160` `MainAgentSession._prepare_turn()`（见 3.1）：`read_for_execution(self.id)` 读已提交 history → 填入 `req.contexts` → `build_main_agent(...)`。
7. **构建 Agent**
   `astrbot/core/astr_agent_runtime/main_agent/builder.py:87-127` `build_main_agent()`：`:125-127` `prepared = await request.prepare_request(event, plugin_context, config, provider, req, collected_input)`。
8. **最终把压缩参数传给 runner**
   `astrbot/core/astr_agent_runtime/execution.py:63-109` `create_native_runner()`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/astr_agent_runtime/execution.py ）：
   - `:63-70` 取 `provider.provider_config["max_context_tokens"]`，≤0 时查 `LLM_METADATAS` 或 `config.fallback_max_context_tokens`；
   - `:77-82` 解析/兜底 compression provider；
   - `:94` `max_context_tokens=max_context_tokens`
   - `:95` `llm_compress_instruction=config.llm_compress_instruction`
   - `:96` `llm_compress_keep_recent_ratio=config.llm_compress_keep_recent_ratio`
   - `:97` `llm_compress_provider=compression_provider`
   - `:98` `truncate_turns=config.dequeue_context_length`
   - `:99` `enforce_max_turns=config.max_context_length`
9. **runner 应用**
   `astrbot/core/agent/runners/tool_loop_agent_runner.py:274-345` `ToolLoopAgentRunner.__init__`：
   - `:286` 注释「enforce max turns, will discard older turns when exceeded BEFORE compression」；
   - `:315-320` 保存 `enforce_max_turns`、`truncate_turns`、`llm_compress_*`；
   - `:327-340` 构造 `ContextConfig(max_context_tokens=..., enforce_max_turns=..., truncate_turns=..., llm_compress_*, ...)`；
   - `:341-343` `ContextManager(config)`；
   - `:400` `messages = load_messages(request.contexts or [])`。
   压缩器阈值：`astrbot/core/agent/context/compressor.py:72-99`（`compression_threshold=0.82`；`should_compress` 用 `usage_rate > threshold`）。

**结论（观测，强）**：`agent.config.compression.*` 与 `max_context_tokens` **确实在主 LLM 请求路径中被读取并应用**（配置 → from_config → execution → ToolLoopAgentRunner → ContextManager/Compressor），不是「配置存在但未接线」。

### 3.4 值得注意的实现细节 / 潜在陷阱

- `MainAgentBuildConfig` 的 dataclass 字段默认值（`:56` `max_context_length: int = 50`、`:59` `dequeue_context_length: int = 10`）与 runner 配置默认值（`max_turns=-1`、`trim_turns=1`）**不一致**；但由于 `from_config()` 总是通过 `resolve_context_compression_config(...)` 显式传入这两个值，正常主链下 dataclass 默认 `50/10` 不会生效（推断，置信度高：基于 `:127` 总是展开该 dict；除非有自定义调用方绕过 `from_config()` 直接构造 dataclass）。
- 官方文档明确「`-1` 不会关闭基于 Token 的自动压缩」；即**默认仍有基于窗口的自动压缩**，只是不按轮数主动截断。

---

## 4. 记忆机制（Memory systems）

| 机制 | 类型 | 默认 | 是否接在主 LLM 链路上 | 证据 |
| --- | --- | --- | --- | --- |
| **短期：conversation history（事件溯源）** | 结构化消息历史，随每轮 LLM 交互持久化 | 开启（主路径核心） | **是** | `main_session.py:76-138`；`request.py:389-395`；`tool_loop:400`；release body「会话后端迁移到事件溯源」 |
| **上下文压缩 / LLM 摘要** | 超 82% 窗口时：LLM 总结旧内容（保留最近 15%）或按轮截断 | `overflow_strategy="llm_compress"`, `max_turns=-1`, `keep_recent_ratio=0.15` | **是**（见 3.3 完整链） | `config/agent_runner.py:21-28`；`execution.py:94-99`；`compressor.py:72-99`；官方文档 https://docs.astrbot.app/use/context-compress.html |
| **群聊上下文感知 ICL（`provider_ltm_settings.group_icl_enable`）** | 群消息环形缓存 + 作为 system reminder 注入 LLM 请求 | **默认 False**（关闭） | **有条件接入**：关闭时不注入；开启时通过 `@filter.on_llm_request()` hook 注入 | `config/default.py:224-232`；`builtin_stars/astrbot/main.py:127-135, 205-215, 320-330`；`group_chat_context.py:1-40` |
| **群历史持久化（`group_message_history_enable` / `max_cnt=700`）** | 把群消息/回复写入 `message_history_manager` | **默认 False** | 关闭时不写；开启时由 built-in star 事件钩子写；并为主 agent 提供 `GetGroupMessageHistoryTool`（仅群消息且开关打开时挂载，可由模型按需调用） | `config/default.py:229-231`；`main.py:142-180, 283-317`；`main_agent/builder.py:173-177` |
| **主动回复（`active_reply.enable`）** | 概率触发主动发言 | 默认 False | 通过 built-in star 钩子，非主 conversation 历史 | `config/default.py:233-237`；`main.py:128-135` |
| **知识库（KB）/ 长期资料** | 向量库 + 检索结果注入 | `kb_names=[]`（默认空） | 主链中有 `knowledge_base.apply_knowledge_base(...)`（builder.py:149），但默认无知识库 | `config/default.py`（`kb_*`）；`builder.py:149`；官方文档「需要长期保存的资料应放入知识库」 |
| **用户/会话偏好持久化（`sel_conv_id` 等）** | 会话状态 KV | 内部使用 | 是（会话选择），非 LLM 上下文内容 | `conversation_mgr.py:297-298, 358-362` |

**重要区分（观测 + 强调）**：
- `provider_ltm_settings.*` 这一组键**存在于配置 schema / 内置插件中，但默认全关**；它们**不是** v4 主 conversation 历史的一部分，而是额外的群聊上下文增强层。
- 真正默认开启、且无条件在主链上的「记忆」是 **conversation history + token-触发的上下文压缩/LLM 摘要**。
- 证据显示：`group_icl_enable` 只在 `builtin_stars/astrbot/main.py:209-215` 决定要不要记录群消息；`decorate_llm_req`（`:320-330`）通过 `on_llm_request` hook 把群上下文注入请求——这是官方 built-in star 插件的一部分，随安装包一起加载，但受默认关闭的开关控制。

---

## 5. 运行时入口与主 LLM 调用位置

### 5.1 启动方式（官方文档，证据）

- **Docker / Compose**（推荐生产）：官方文档 https://docs.astrbot.app/deploy/astrbot/docker.html ：
  - `git clone https://github.com/AstrBotDevs/AstrBot && cd AstrBot && sudo docker compose up -d`
  - `docker run -itd -p 6185:6185 -p 6199:6199 -v $PWD/data:/AstrBot/data ... --name astrbot soulter/astrbot:latest`
- **uv / pip 类**：README https://github.com/AstrBotDevs/AstrBot/blob/v4.30.0-beta.1/README.md:73-83 ：`uv tool install astrbot --python 3.12` → `astrbot init` → `astrbot run`（更新 `uv tool upgrade astrbot --python 3.12`）。
- **Dockerfile**：`Dockerfile:1,36`（https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/Dockerfile ）：`FROM python:3.12-slim`，`WORKDIR /AstrBot`，`EXPOSE 6185`，`CMD ["python", "main.py"]`。
- 其他：桌面客户端、Launcher、源码部署（README:85-101 与文档侧栏）。

### 5.2 主 LLM 调用 / 上下文组装位置（代码定位）

按执行顺序（全部为 pinned commit 的文件+行号）：

| 环节 | 文件:函数 | 行号 | URL |
| --- | --- | --- | --- |
| Pipeline 装配 | `astrbot/core/pipeline/bootstrap.py` / `process_stage/stage.py` | — | https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/pipeline/process_stage/stage.py |
| 选择 runtime / 进入 Agent 子阶段 | `process_stage/method/agent_request.py: AgentRequestSubStage.process` | 全文（文件仅 83 行） | https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/pipeline/process_stage/method/agent_request.py |
| 建输入 / 开会话 / 跑一轮 | `process_stage/method/agent_sub_stages/internal.py: InternalAgentSubStage.process` | `:127,145,154` | https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py |
| 装载历史 → `req.contexts` | `astrbot/core/astr_agent_runtime/main_session.py: MainAgentSession._prepare_turn` | `:76,114-138,144-154` | https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/astr_agent_runtime/main_session.py |
| 组装主 Agent（人格/知识库/工具/图片） | `astrbot/core/astr_agent_runtime/main_agent/builder.py: build_main_agent` | `:87-127,138-196,225-253` | https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/astr_agent_runtime/main_agent/builder.py |
| 真正的 LLM 请求循环（工具循环、流式、压缩后请求） | `astrbot/core/agent/runners/tool_loop_agent_runner.py: ToolLoopAgentRunner` | `:274-345`（压缩配置）；`:400`（加载 history）；`:781`（`req` payload 发起调用） | https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/agent/runners/tool_loop_agent_runner.py |
| 每轮历史落库 | `astrbot/core/astr_agent_runtime/history.py: save_agent_history` + `conversation_events.py` | `:59-139` | https://raw.githubusercontent.com/AstrBotDevs/AstrBot/v4.30.0-beta.1/astrbot/core/astr_agent_runtime/history.py |

**归一化结论（观测）**：「上下文组装」发生在 `main_session._prepare_turn()`（装载整段 conversation history）+ `build_main_agent()`（system prompt / 工具 / 知识库）；「实际 LLM 调用 + 发送前压缩」发生在 `ToolLoopAgentRunner`（`ContextManager` 在每轮请求前执行 `should_compress` 与截断/摘要）。

---

## 6. 对 Ling 关注问题的直接回答

### Q1: 同一窗口里的旧内容从哪来？

**观测（强证据）**：来自**同一个 UMO 绑定的 conversation 的完整已提交历史**，不是某个固定条数的最近消息。
- `main_session.py:120` 遍历 `conversation.content or []`；`request.py:394` `req.contexts = conversation.content or []`。
- conversation 由 UMO 选中（`conversation_mgr.py:349-363`），每轮结束由 `save_agent_history` / `ConversationEventWriter` 追加（history.py:59-139）。
- 因此同一会话里，用户几个月前聊的内容只要还在该 conversation 中，就会进入 working context；只有当 token 用量 > 窗口 82% 或 `max_turns > 0` 时，压缩器才会删/摘要旧轮次。
- **例外（观测）**：群聊场景下若开启 `group_icl_enable`，额外的 `GroupChatContext` 环形缓存（上限 `group_message_max_cnt=1000`）会以 system reminder 形式注入群聊上下文（`builtin_stars/astrbot/main.py:320-330`；`group_chat_context.py:27-40` 的 `GROUP_HISTORY_HEADER/FOOTER`）；若开启 `group_message_history_enable`，可通过 `GetGroupMessageHistoryTool` 让模型按需读取历史群消息（`builder.py:173-177`）。

### Q2: 有显式话题切换检测吗，还是自然漂移？

**观测**：在本次 pin 的源码检索（`grep -rniE "topic.?switch|话题切换|新话题|new topic|drift"` 覆盖已下载的 20+ 核心文件）中，**未发现任何基于话题切换的自动分段/窗口重置逻辑**。命中的只有输出层的 `segmented_reply`（把长回复拆段发送，与话题无关）和消息组件 `MessageSegment`（结构名，与话题无关）。
- 官方提供的「换话题」手段是**用户显式命令**：官方文档 https://docs.astrbot.app/use/context-compress.html 「想从头开始一段对话怎么办？」→「在聊天中使用 `/reset` 清空当前对话上下文，或使用 `/new` 创建新对话。」
- **推断（置信度中高）**：因此上游设计里，旧话题残留于同一窗口属于**自然漂移（natural drift）**；系统没有在检测到话题变化时自动开新窗口。压缩/摘要只在 token 超阈值时发生，而摘要的目标是「保留任务进度/关键结论」，并非「识别并隔离话题」（官方文档「默认摘要会关注用户目标、任务进度、关键结论、工具结果……」，以及 `compressor.py` 的 `llm_compress_instruction` 参数）。

### Q3: 按上游设计，需要「每个新窗口」吗？

- **观测**：上游内置的是「一个 UMO → 一个当前 conversation（`sel_conv_id`）」，压缩/摘要在**同一个 conversation 内**进行；创建新 conversation 需要用户 `/new`（或 `/reset` 清空）。
- **推断（置信度中高）**：如果 Ling 的业务语义要求「不同话题绝不互相污染」，上游默认机制**不能自动保证**；需要在 Ling 侧自己决定何时 `new_conversation`（换 UMO 或在适当时机调用 `/new` 等价逻辑），或调整 `max_turns` 为较小正数让旧轮次更早被丢弃。注意：即使 `max_turns` 设小，也只是按轮截断、不是话题级隔离。

### Q4: 有哪些配置「存在但未接主链」？

| 配置 | 状态 | 依据 |
| --- | --- | --- |
| `provider_ltm_settings.group_icl_enable` / `group_message_history_enable` 等 | 配置存在、代码存在（built-in star），但**默认 False**；接在 `on_llm_request` hook 上而非核心 conversation 装载链 | `config/default.py:224-237`；`main.py:127-135,320-330` |
| `platform_settings.unique_session` | 配置存在（默认 False）；消费点在平台配置层，本次未逐行追到具体分支 | `config/default.py:82`；未核实 |
| `MainAgentBuildConfig.max_context_length=50` / `dequeue_context_length=10` 的 dataclass 默认 | 存在但正常主链被 `from_config()` 的显式值覆盖，不会生效 | `config.py:56,59` vs `:127` |
| `CONFIG_METADATA_2` / 旧 v3 配置元数据 | 文件注释明确「v3 时代配置元数据，目前仅承担类型验证/WebUI 模板……未来逐步淘汰」 | `config/default.py:389` 附近 docstring |

---

## 7. 证据表（汇总）

| # | 主张 | 类型 | 证据（URL + file:line / commit） |
| --- | --- | --- | --- |
| 1 | 最新 release = v4.30.0-beta.1 | 观测 | https://api.github.com/repos/AstrBotDevs/AstrBot/releases/latest |
| 2 | pin commit = 90ba3270…（2026-11-18） | 观测 | https://api.github.com/repos/AstrBotDevs/AstrBot/git/ref/tags/v4.30.0-beta.1 |
| 3 | UMO = `platform_id:message_type:session_id` | 观测 | `message_session.py:18-27` |
| 4 | 事件构造 session = (platform id, msg type, session id) | 观测 | `astr_message_event.py:127-132,176-179` |
| 5 | conversation 以 UMO 为 user_id 并记 `sel_conv_id` | 观测 | `conversation_mgr.py:269-299,349-363` |
| 6 | 默认 unique_session=False | 观测 | `config/default.py:82` |
| 7 | 传给 LLM 的是整段 conversation.content（非固定 N 条） | 观测 | `main_session.py:114-138`；`request.py:389-395`；`tool_loop:400` |
| 8 | `compression.max_turns=-1 / trim_turns=1 / strategy=llm_compress / keep_recent_ratio=0.15 / fallback=128000` | 观测 | `config/agent_runner.py:21-28` |
| 9 | 82% token 阈值触发压缩 | 观测 | `agent/context/compressor.py:72-99,137-184`；官方文档 https://docs.astrbot.app/use/context-compress.html |
| 10 | 配置→主链完整调用链 | 观测 | `config/agent.py:15-38` → `config/agent_runner.py:102-134` → `main_agent/config.py:98,127` → `internal.py:127,145,154` → `main_session.py:76-138` → `builder.py:87-127` → `execution.py:63-99` → `tool_loop:274-345` |
| 11 | 群聊 ICL / 历史持久化默认关闭，经 hook 注入 | 观测 | `config/default.py:224-237`；`main.py:127-135,205-215,320-330` |
| 12 | 无话题切换检测；用 `/reset`、`/new` 手动开新窗 | 观测+推断 | grep 全仓未命中；官方文档 https://docs.astrbot.app/use/context-compress.html |
| 13 | Docker / uv 启动方式 | 观测 | 官方文档 https://docs.astrbot.app/deploy/astrbot/docker.html ；README:73-90；Dockerfile:1,36 |
| 14 | 主 LLM 调用在 ToolLoopAgentRunner | 观测 | `tool_loop:274-345,400,781`；`execution.py:71-109` |

---

## 8. 关键结论

1. **版本**：上游最新 release 为 `v4.30.0-beta.1`；本报告源码证据钉在 commit `90ba3270f09b857063da68908d80f742042067cd`（2026-11-18），master 观测时为 `feb3e82…`。
2. **会话键**：`unified_msg_origin = platform_id:message_type:session_id`（`message_session.py:18-19`），默认同一 UMO 复用一条 conversation（`sel_conv_id`），`unique_session` 默认 False。
3. **不是固定 N 条窗口**：主链把整段 `conversation.content` 载入 `req.contexts`（`main_session.py:120` / `request.py:394`），旧内容来自同一会话的完整已提交历史。
4. **真正的「窗口」是 token 触发的压缩**：默认 `overflow_strategy=llm_compress`、`max_turns=-1`、`keep_recent_ratio=0.15`、`fallback_max_tokens=128000`；请求前 token 用量 > 窗口 ~82% 即压缩/截断（代码 `compressor.py:72-99` + 官方文档交叉印证）。
5. **配置确实接在主链**（非仅声明）：`compression.*` 经 `resolve_context_compression_config` → `MainAgentBuildConfig.from_config` → `create_native_runner` → `ToolLoopAgentRunner` 实际生效（`execution.py:94-99`）。
6. **记忆分层**：默认开启的是 conversation history + LLM 摘要；`provider_ltm_settings.*`（群聊 ICL/历史持久化/主动回复）默认全关，属于内置 star 插件的增强层，经 `on_llm_request` hook 注入，不是核心 conversation 的一部分。
7. **知识库**是官方建议的长期记忆手段，但默认 `kb_names=[]`；`builder.py:149` 有 `apply_knowledge_base` 接线。
8. **无话题切换检测**：全仓检索未发现自动 topic-switch 分段；新窗口靠用户 `/reset`（清空上下文）或 `/new`（新对话）手动触发——旧话题残留属自然漂移（推断，置信度中高）。
9. **对 Ling 的含义**：若需要话题级隔离，上游默认设计不提供；需在 Ling 侧自行决定 new conversation 时机或调小 `max_turns`（注意后者只是按轮截断，不是话题隔离）。
10. **未核实项**：各平台适配器如何具体生成 `session_id`、`unique_session` 的精确消费分支、以及 NAS 生产实例实际在跑哪一个小版本——本次均未核实（不涉及生产连接）。

---

*报告结束。所有 URL 均为官方来源；无凭据、无内网地址。*
