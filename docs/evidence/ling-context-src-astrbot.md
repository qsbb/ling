# AstrBot 官方仓库上下文 / 会话 / 记忆机制取证

> 本文只记录取证到的**代码事实**与**观测方法**；推测单独标注。不写任何真实凭据，内网地址用 `192.0.2.x` 占位。

## 1. 取证环境与版本 / commit / 入口

### 1.1 本地仓库锚点（观测输出原文）

- 工作目录：`/Users/lingxi/project/ling`
- `git log -1 --format='%H %s'`：`de43cbe79a78e0c01f7821470500eaa04ea79497 feat(live): 合入直播工作台与舞台控制`
- `git branch --show-current`：`main`
- 开始时 `git status --short`：仅见其他 agent 的未跟踪证据文档（`docs/evidence/ling-context-im-*.md`、`docs/evidence/ling-context-research/`、`docs/research/`），**未见本文目标文件**；本文是新增文件，不修改业务代码。

### 1.2 官方仓库锚点

| 项 | 值 | 取证方式 |
|---|---|---|
| 仓库全名 | `AstrBotDevs/AstrBot` | `GET https://api.github.com/repos/AstrBotDevs/AstrBot`（已核实，`full_name` 字段） |
| 默认分支 | `master` | 同上（`default_branch` 字段） |
| master HEAD | `feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea` | `GET https://api.github.com/repos/AstrBotDevs/AstrBot/commits/master`，`sha` 字段 |
| HEAD 提交时间 | `2026-11-19T09:44:37Z` | 同上，`commit.committer.date` |
| HEAD 提交信息首行 | `feat(webui): add animated dual sidebars for chat (#11192)` | 同上 |
| 仓库 `pushed_at` | `2026-11-19T22:21:22Z` | 同上 repo 元数据 |
| Stars（观测时） | 47314 | 同上 `stargazers_count`，会变动，仅作标识 |
| 版本号 | `4.30.0-beta.1` | `astrbot/__init__.py:3` 与 `pyproject.toml:3` 两处一致 |

源码获取：`git clone --filter=blob:none ...` 走 github.com:443 **失败**（`curl 28 Failed to connect to github.com port 443 after 75000 ms`），改用 codeload 一次性取 tarball 成功：

```
curl -sSL --max-time 180 -o astrbot.tar.gz \
  'https://codeload.github.com/AstrBotDevs/AstrBot/tar.gz/feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea'
# http=200 bytes=9138239，解压目录 AstrBot-feb3e82c...
```

所有行号均基于这个 tarball 的 `feb3e82c` 版本。解包后本地参考路径 `/tmp/astrbot-evidence/AstrBot`（临时取证目录，非仓库内容）。

运行入口：`main.py`。`main.py:131` 定义 `async def main_async(webui_dir_arg)`，`main.py:175-176` 注释「只使用一次 asyncio.run()」并执行 `asyncio.run(main_async(args.webui_dir))`，`main.py:151` 为 `if __name__ == "__main__"`。CLI 入口在 `pyproject.toml:90`：`astrbot = "astrbot.cli.__main__:cli"`。

## 2. 会话与历史窗口机制（含行号）

### 2.1 三层标识：platform / message_type / session_id → UMO

`astrbot/core/platform/message_session.py:6-27` 定义 `MessageSession`：

- `:12` `platform_name: str`，注释 `:13` 明确「自 AstrBot v4.0.0 起，该字段实际为 platform_id」。
- `:14` `message_type: MessageType`，`:15` `session_id: str`，`:16` `platform_id: str = field(init=False)`。
- `:18-19` `__str__` 返回 `f"{self.platform_id}:{self.message_type.value}:{self.session_id}"`——这就是统一消息来源 UMO 的字符串形态。
- `:21-22` `__post_init__` 把 `platform_name` 复制给 `platform_id`；`:24-27` `from_str()` 按 `split(":", 2)` 反解。

`astrbot/core/platform/astr_message_event.py:128-132` 构造 `MessageSession(platform_name=platform_meta.id, message_type=message_type, session_id=session_id)`；`:175-178` `unified_msg_origin` 属性直接 `return str(self.session)`；`:180-184` setter 用 `MessageSession.from_str` 反解；`:186-194` `session_id` getter/setter 直接读写 `self.session.session_id`。

会话 ID 由各平台适配器决定，例如 `astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py:215` `session_id = self.get_group_id() if is_group else self.get_sender_id()`；`:222` 传给 `MessageSession`。即：群聊用群号、私聊用发送者 ID（OneBot 例）。其他平台同理（Discord 用 channel.id，`discord_platform_adapter.py:304`；KOOK 用 target_id/user_id，`kook_adapter.py:520-527`）。

**群组内按人隔离是配置项，默认关闭**：`astrbot/core/config/default.py:97` `"unique_session": False`。开启后 `astrbot/core/event_bus.py:118-140` `_apply_session_isolation()` 在 `:133-135` 判断 `unique_session` 且 `MessageType.GROUP_MESSAGE`，`:136-139` 用 `UNIQUE_SESSION_ID_BUILDERS` 按平台改写 `event.session_id`，并置 `_session_isolated=True`。注意 `astrbot/core/platform/manager.py:31-38` 特别说明：`self.settings = config["platform_settings"]`，「unique_session 必须使用默认配置」——也就是说平台启动时的设置取的是默认档，与按 UMO 覆盖的配置档并非同一处取值（这一点是从注释读到的，是否构成运行时不一致未做实测）。

### 2.2 会话（session）与对话（conversation）分离

`astrbot/core/conversation_mgr.py:1-5` 模块 docstring 明说：「在 AstrBot 中，会话和对话是独立的，会话用于标记对话窗口……在一个会话中可以建立多个对话，并且支持对话的切换和删除」。

- `conversation_mgr.py:40` `self.session_conversations: dict[str, str]`，进程内「UMO → 当前 cid」映射。
- `:349-363` `get_curr_conversation_id(umo)`：先查进程内映射，未命中则 `sp.session_get(umo, "sel_conv_id", None)` 回填（`:360-362`），即选择状态持久化在 shared_preferences。
- `:267-299` `new_conversation()`：`:290-296` `db.create_conversation(user_id=umo, platform_id=..., content=content, ...)`，`:297-298` 写进程内映射与 `sp.session_put(umo, "sel_conv_id", cid)`。
- `:301-312` `switch_conversation()` 同样写两处。

DB 侧 `astrbot/core/db/conversation.py:28-35` 定义事件类型：`CONTEXT_TYPES = {"message.appended", "context.rebased"}`；`EVENT_TYPES` 还含 `conversation.created/updated`、`turn.started/finished`；`REBASE_REASONS = {"compaction", "reset", "legacy_replace", "migration", "snapshot"}`。`:35` 明确把 `reset` 与 `compaction` 都归为 rebase 原因。

### 2.3 历史窗口如何进入 prompt

主入口 `astrbot/core/astr_agent_runtime/main_session.py:71-79`：从事件取 UMO 对应的会话，`:76` `loaded = await manager.db.conversation_store.read_for_execution(self.id)`，`:79` 得到 `conversation, snapshot`。

`main_session.py:114-138`：普通轮次（非显式 SDK 请求）把 `req.conversation` 设为 legacy 投影并重建 `req.contexts`：

- `:118` `req.contexts = []`
- `:120` `for message in [*(conversation.content or []), {"role": "user"}]`
- `:121-136` 补齐未闭合的 tool_calls 对（插入 `content="Execution was interrupted; ..."` 的 tool 消息）
- `:137` `req.contexts.append(message)`
- `:138` `req.contexts.pop()` 去掉末尾 sentinel

即：**没有「最近 N 条」的硬截断**；窗口由持久化层 `conversation.content` 加上压缩策略共同决定。请求准备侧还有一条兼容路径 `astrbot/core/astr_agent_runtime/main_agent/request.py:296-322` `_get_session_conv()`：`:312-313` 取 UMO 的当前 cid，`:314-315` 无则新建，`:316` `db.get_conversation_by_id(cid)`；`request.py:390-395` 把 `conversation.content or []` 赋给 `req.contexts`。`request.py:346-353` 处理「是否重新采集引用」的判定。

持久化写回：`astrbot/core/astr_agent_runtime/history.py:59-139` `save_agent_history()`；`:88-98` 跳过首个 system 与 `_no_save` 消息；`:100-102` `dump_messages(..., include_temporary=writer is not None)`；`:135-138` 走 `commit` 或 `writer.save_history()`。`main_session.py:185-196` `_commit_history()` 调 `conversation_manager.update_conversation(umo, self.id, history=history, token_usage=...)`。

### 2.4 压缩 / 截断的触发与默认值

`astrbot/core/agent/context/manager.py:49-92` `ContextManager.process()`：

- `:63-69` 第 1 步按轮截断：仅当 `config.enforce_max_turns != -1` 时调 `truncator.truncate_by_turns(...)`。
- `:74-85` 第 2 步按 token 压缩：仅当 `config.max_context_tokens > 0` 且 `compressor.should_compress(...)` 为真时执行。
- `:94-179` `_run_compression()`：`:127-135` 压缩后仍超限则按轮丢弃最旧轮次；`:154` `while len(rounds) - dropped_rounds > 1 ...` **保证最后一轮（当前请求）永远保留**；`:172-178` 只剩一轮仍超限时原样发出。

`astrbot/core/agent/context/config.py:17-32` 定义 `ContextConfig` 默认：`max_context_tokens=0`（`:17-18`，`<=0` 表示无限制）、`enforce_max_turns=-1`（`:19-20`）、`truncate_turns=1`（`:21-27`）、`llm_compress_keep_recent_ratio=0.15`（`:30`）、`llm_compress_provider=None`（`:32`，为 None 时用截断策略）。

`astrbot/core/agent/context/truncator.py:100-144` `truncate_by_turns()`：`:119-120` `keep_most_recent_turns == -1` 直接原样返回；`:124-125` 轮数未超限也原样返回；`:127-131` `num_to_keep = keep_most_recent_turns - drop_turns + 1`，取 `non_system_messages[-num_to_keep*2:]`；`:133-139` 截到第一个 user 起；`:141-144` `_ensure_user_message()` + `fix_messages()` 修 tool 配对。`:146-160` 另有 `truncate_by_dropping_oldest_turns()`。

**默认值的两层来源要分清（这是配置声明 vs 代码默认的关键区别）**：

1. 出厂配置 `astrbot/core/config/agent_runner.py:21-29` `AGENT_RUNNER_CONFIG_DEFAULTS["local"]["compression"]`：`max_turns=-1`、`trim_turns=1`、`overflow_strategy="llm_compress"`、`instruction=""`、`keep_recent_ratio=0.15`、`provider_id=""`、`fallback_max_tokens=128000`。
2. 解析函数 `agent_runner.py:102-134` `resolve_context_compression_config()`：`:115` `max_turns = compression_config.get("max_turns", -1)`，`:116` `trim_turns = ... 1`，`:121-123` strategy 缺省 `truncate_by_turns`，`:125-127` ratio 缺省 0.15，`:131-133` fallback 缺省 128000。
3. 构建配置 `astrbot/core/astr_agent_runtime/main_agent/config.py:48-62` 的 dataclass 字段默认：`context_limit_reached_strategy="truncate_by_turns"`（`:48`）、`max_context_length=50`（`:56-58`）、`dequeue_context_length=10`（`:59-60`）、`fallback_max_context_tokens=128000`（`:61-62`）。但 `:127` `**resolve_context_compression_config(runner_config.get("compression", {}))` 会把解析结果覆盖进来——**dataclass 的 50/10/truncate 只是缺少 `compression` 段时的兜底，不是出厂配置的生效值**。
4. 执行侧 `astrbot/core/astr_agent_runtime/execution.py:94-99` 把上述值传入 runner；`tool_loop_agent_runner.py:326-338` 组装 `ContextConfig`，`:328-334` `max_context_tokens` 由 fallback 与模型元数据推导，`enforce_max_turns=self.enforce_max_turns`、`truncate_turns=self.truncate_turns`。
5. UI schema 声明 `astrbot/core/config/default.py:4242-4314`（`truncate_and_compress` 分组）：`:4247-4251` max_turns 的 hint 写「超过该轮数后才……请求发送前也会先按该值约束上下文，-1 表示不按轮数限制」；`:4256-4260` trim_turns；`:4265-4274` overflow_strategy 选项 `truncate_by_turns` / `llm_compress`；`:4285-4295` keep_recent_ratio 范围 0–0.3；`:4306-4311` fallback 默认 128000。**schema 只声明字段与 hint，实际默认值由 agent_runner.py / config.py 决定。**

结论（代码接线层面）：出厂默认 `max_turns=-1` ⇒ `enforce_max_turns=-1` ⇒ 第 1 步按轮截断不生效；`max_context_tokens` 由模型元数据 / fallback 128000 决定并启用 token 压缩；策略默认 `llm_compress`。

## 3. 摘要 / 记忆 / 引用机制

### 3.1 LLM 摘要压缩（有实际调用）

`astrbot/core/agent/context/compressor.py`：

- `LLMSummaryCompressor.__call__`（`:216-372`）是实际生成摘要的函数。`:225-233` 用 `split_into_rounds` 把消息切成轮，按 token 比例留最近轮。
- `:186-214` `_split_recent_rounds_by_token_ratio()`：`:203` `budget = max(1, int(total_tokens * keep_recent_ratio))`，`:207-213` 从最新往前加整轮，`:214` 返回旧轮/最近轮；注释 `:194-198` 说明「正比例至少保留最新整轮，轮不被拆分」。
- `:235-242` 若最后一条是 user 且它落在 old_rounds，强制把该轮移到 recent（当前请求不摘要）。
- `:269-275` 摘要输入末尾补一条 assistant "Acknowledged."；`:276-285` 追加 user 指令 `Generate a summary of our previous conversation history.` + `instruction_text` + `TASK_CONTINUATION_INSTRUCTION`；`:280-284` 要求只输出摘要正文。
- `:301-304` **真的调用** `self.provider.text_chat(contexts=sanitized_summary_contexts, request_max_retries=1)`；`:305-315` 失败返回原消息，让调用方回退截断。
- `:350-364` 组装结果：`_extract_system_messages(messages)` + user `Our previous history conversation summary: {summary_content}` + assistant `Acknowledged the summary...` + 最近的整轮。
- 默认摘要指令在 `:158-166`：要求覆盖所有核心主题、工具调用次数、读过的材料路径、初始目标与进度，并用用户语言写。
- 摘要事件上报 `:317-344`：向 `request_event_emitter` 发 `AgentResponse("message.appended", payload, event_id)`，`purpose="compaction"`，`:322` 消息带 `_no_save=True`（不进用户可见历史）。
- 策略选择接线：`manager.py:34-47`——有 `custom_compressor` 用它，否则有 `llm_compress_provider` 用 `LLMSummaryCompressor`，否则 `TruncateByTurnsCompressor`；provider 接线在 `astrbot/core/astr_agent_runtime/main_agent/providers.py:86-94`。`execution.py:79` 与 `providers.py:86` 都以 `context_limit_reached_strategy == "llm_compress"` 判断。

### 3.2 事件存储层的 rebase / 摘要落库

`astrbot/core/db/conversation.py:128-244` `reconcile_history()`：`:162-179` 投影消息并识别临时内容；`:182-209` 前缀未变时追加 `message.appended` 事件；`:210-242` 前缀变化时生成一个 `context.rebased` 事件，`:236` `reason = "reset" if not projected else reason`（即清空历史会记为 rebase/reset，而不是物理删除事件）。`:35` 的 `REBASE_REASONS` 含 `compaction` 与 `reset`，两类共用同一机制。

`conversation_mgr.py:236-260` `to_legacy_conversation()` `:258` `history=json.dumps(record.content or [])`：DB 投影仍是 OpenAI 形态的 `content` 列表，供上面的 `req.contexts` 使用。

### 3.3 引用消息进入 prompt（真接线）

`astrbot/core/agent_media.py:193-203` `append_quoted_content()` 签名与 docstring「Append quoted text and optionally describe an explicitly supplied image」；`:220-226` 从 `event.message_obj.message` 找第一个 `Reply` 组件，没有就返回；`:230` 取发送者昵称；`:231-239` `extract_quoted_message_text(...)` 取引用正文，回退 `quote.message_str` / `[Empty Text]`；`:240` 拼进 `content_parts`。

`:283-285` 是注入点：

```python
quoted_content = "\n".join(content_parts)
quoted_text = f"<Quoted Message>\n{quoted_content}\n</Quoted Message>"
req.extra_user_content_parts.append(TextPart(text=quoted_text))
```

即引用消息以**明确标签包裹的 user 内容部分**进入 prompt，不是塞进 `req.contexts`。图片分支 `:242-280`：若主模型支持图片则跳过描述；否则调用图片描述 provider（`:269-272` `text_chat(prompt="Please describe the image content.", image_urls=[image_ref])`），把结果作为 `[Image Caption in quoted message]` 加入。嵌套转发引用由 `astrbot/core/utils/quoted_message/extractor.py:16-62` 按 `max_fetch` 抓取（`:56-60` 超 hops 记 warning）。

入口接线：`astrbot/core/astr_agent_runtime/main_agent/request.py:160-175` 收集 `Reply` 组件与 `get_quoted_message_settings(config.provider_settings)`；`:238-246` 调 `extract_quoted_message_images(...)`；`:346-353` 若已有 `<Quoted Message>\n` 前缀则不再重复采集；`main_session.py:92-99` 同样判定 `parse_quoted`。

**文档声称 vs 代码**：`docs/zh/providers/agent-runners/deerflow.md:57` 声称 `/reset`、`/new`、`/del` 会尝试同步清理 DeerFlow 远端 thread；代码里确实有对应实现：`conversation.py:26-72` `_cleanup_deerflow_thread_if_present()` 通过 `DeerFlowAPIClient.delete_thread(thread_id)`（`:58`）清理，`:67-72` 失败仅 warning——与文档「网关不可达只清理本地标识」一致。

## 4. 显式换题命令接线核查

结论：**`/reset` 与 `/new` 都真实接在主链路上**（不是只有配置声明或文档声称）。证据链如下。

### 4.1 命令注册

`astrbot/builtin_stars/builtin_commands/main.py:44-48`：

```python
@filter.command("reset")
@filter.permission_type(filter.PermissionType.SHARED_GROUP_ADMIN)
async def reset(self, message: AstrMessageEvent) -> None:
    """Clear the context of the current conversation."""
    await self.conversation_c.reset(message)
```

`:55-59` 同样注册 `@filter.command("new")` → `conversation_c.new_conv(message)`。同文件还注册 `stop`（`:50-53`）、`stats`（`:61-64`）、`sid`（`:33-36`）等。命令名与权限由 `@filter.command` / `@filter.permission_type` 装饰器提供，过滤实现见 `astrbot/core/star/filter/command.py:32-53`（`CommandFilter`，含 alias 与 parent_command_names）。帮助文案 `astrbot/builtin_stars/builtin_commands/i18n.py:38` `command.reset`「清空当前对话的上下文」、`:40` `command.new_conv`「创建新对话」。

未发现 `/clear` 命令：对 `astrbot/builtin_stars/` 全量 grep `reset|clear|new.?session|新会话|重置|清空` 只命中 `reset`（`main.py:44`、`conversation.py:112`）、`new`（`main.py:55`、`conversation.py:164`）、`stop`、`_clear_third_party_agent_runner_state` 及无关 `records.clear()` 等。**`/clear` 在 builtin 里未取得证据**（第三方插件可能有，未取证）。

### 4.2 命令处理体确实改了会话状态

`astrbot/builtin_stars/builtin_commands/commands/conversation.py`：

- `reset()` `:112-141`：`:118` 取 `umo`，`:119-120` 读当前 runtime，`:122` `active_event_registry.stop_all(umo, exclude=message)` 停掉在跑的任务；`:123` 取 cid；`:124-129` 第三方 runtime 清外部状态；**`:130-136` 原生 runtime 下 `update_conversation(umo, cid, history=[])`**——这是真正的清历史调用，不是只发提示。`:138` 置 `_clean_group_context_session=True`，`:139-141` 返回「上下文已清空」消息。
- `new_conv()` `:164-197`：`:172` 停任务；`:180` `_get_current_persona_id()`；**`:181-185` `conversation_manager.new_conversation(umo, platform_id, persona_id=cpersona)`**——新建对话并切换当前 cid（见 §2.2 `:290-298`），旧对话不删；`:187` 同样置 `_clean_group_context_session=True`。

`update_conversation()` 的写穿：`conversation_mgr.py:454-494`，`:484-486` 补 cid，`:487-494` `await self.db.update_conversation(cid=..., content=history, ...)`——`history=[]` 确实写到 DB（同时经 `reconcile_history` 记为 rebase/reset）。

### 4.3 命令不会顺带污染群聊上下文，且会触发清理

`astrbot/builtin_stars/astrbot/main.py:214-220`：群聊 ICL 记录时 `:215-217` 注释「Skip recording if a command handler matched (e.g. /reset, /help, /new)」，`:218` `if not event.get_extra("handlers_parsed_params", {})` 才记录——命令已被 handler 解析，故不写入群聊上下文。

`astrbot/builtin_stars/astrbot/main.py:335-344` `@filter.after_message_sent()` `after_message_sent()`：`:340` 读 `_clean_group_context_session`，`:341-342` 真为 True 则 `await self.group_chat_context.remove_session(event)`。这是该标志的**唯一**消费点（全仓 grep 只有两处：写入在 `conversation.py:138,187`，读取在 `main.py:340`）。

### 4.4 接线强度判断

- 配置声明：§2.4 的 schema/default 只是声明；**命令不是配置**。
- 代码接线：装饰器注册 → handler 调 `reset`/`new_conv` → `update_conversation(history=[])` / `new_conversation()` → DB 写 → 下一轮 `read_for_execution` 读到新状态。这条链是完整的。
- 文档声称：仅对 DeerFlow 外部 thread 清理找到文档（`deerflow.md:57`），且与代码一致。
- 未验证：命令触发后的**运行时行为**（例如 `/reset` 后 pending 的流式请求是否完全停止、`sp.session_put` 与 DB 写入的原子性）没有跑真实 AstrBot 实例，属于「代码上接线存在」而非「端到端实测」。

## 5. 与 Ling 对比

对比基于 Ling HEAD `de43cbe`（同一工作区，只读）：

| 维度 | AstrBot `feb3e82c` | Ling `de43cbe` |
|---|---|---|
| 会话标识 | `platform_id:message_type:session_id` 组成 UMO（`message_session.py:18-19`），会话与对话分离 | 窗口（`Window`）为会话单元，`src/ling/kernel/bridge.py:1503,1556` `_find_or_create_window` 按渠道与 peer 归属找/建窗口；`src/ling/api/routes/sessions.py:1` 明确「会话（窗口）路由」 |
| 群内按人隔离 | `unique_session` 默认 `False`（`default.py:97`），开启时 event_bus 改写 session_id（`event_bus.py:133-140`） | 本文未取证 Ling 的等价开关（未取得证据） |
| 历史窗口选取 | 无「最近 N 条」硬截；由 `conversation.content` + 压缩策略决定（`main_session.py:118-138`） | token 预算驱动：`HISTORY_TOKEN_BUDGET = 20000`（`src/ling/kernel/context.py:81`）、`SUMMARY_TOKEN_BUDGET = 400`（`:83`）、`HISTORY_FETCH_CAP = 2000`（`:93`）；分页取数 `:1477-1517`，单条超预算截断而非 break（`:1473-1478`） |
| 摘要 | `LLMSummaryCompressor` 真的调 provider（`compressor.py:301-304`），摘要以 user/assistant 对插回上下文（`:350-364`），默认策略 `llm_compress` | 滚动摘要写在窗口行（`summary` / `summary_through_sequence`，`context.py:1129-1133,1673-1679`），早期摘要放在最近原文之前；另有独立的记忆系统 `src/ling/domains/memory/`（extract/consolidate/recall/purge 等） |
| 引用消息 | Reply → `<Quoted Message>...</Quoted Message>` 注入 `req.extra_user_content_parts`（`agent_media.py:283-285`） | 本文未取证 Ling 的引用注入（未取得证据） |
| 显式换题 | `/reset` 清当前对话 history、`/new` 新建并切换对话，均为内置 star 真接线（`builtin_commands/main.py:44-59`、`conversation.py:112-197`） | 未发现聊天内 `/reset` 或 `/clear` 命令处理（`src/ling/` grep 无命中）；有管理 API `POST /v1/sessions`、`DELETE /v1/sessions/{id}`（`sessions.py:304,346`）。「用户一句话换题」是否在入站路径接了新窗口，本文**未取得证据** |
| 安全边界 | 外部内容未统一加不可信包裹；群聊上下文有单独 ICL | `ContextCompiler` 顶部声明「外部网页、OCR、群友文字、工具结果都是数据，不是指令」（`context.py:6-8`），`UNTRUSTED_WRAPPER`（`:63-66`）显式包裹 |

一句话差异：AstrBot 的「换题」是**内置斜杠命令直接改对话存储**；Ling 的窗口管理在 **API 层**，聊天内的显式换题触发是否存在尚需另行取证。

## 6. 未取得证据与失败 URL

明确记录为「未取得」的项：

1. `/clear` 命令：在 `astrbot/builtin_stars/` 内 grep 无命中；不能据此断言整个生态（第三方插件）不存在。
2. AstrBot 的「用户自然语言中说换题（而非斜杠命令）是否触发新对话」：未找到对应代码路径，未取证。
3. Ling 侧引用消息注入与群内按人隔离的等价机制：本文只做了 AstrBot 侧的详尽取证，Ling 侧未展开。
4. 运行时行为（命令是否真的清空了正在流式输出中的请求、DB 与 sp 的一致性）：没有启动 AstrBot 实例，属于「代码接线已核实，端到端行为未核实」。
5. GitHub API 递归 tree：`GET https://api.github.com/repos/AstrBotDevs/AstrBot/git/trees/feb3e82c...?recursive=1` 返回 `API rate limit exceeded for 157.18.193.150`（匿名限流）——已改用 codeload tarball 取证，不影响行号可靠性。
6. `git clone` 直连：`https://github.com/AstrBotDevs/AstrBot.git` 超时（`curl 28 Failed to connect to github.com port 443 after 75000 ms`，exit code 128），已由 codeload 替代。
7. 取到的实现版本为 `master` HEAD `feb3e82c`（`4.30.0-beta.1`）。若 lead 关心的是生产端实际部署版本（任务卡未给出），本文**未取得**生产端版本证据，也**未连接生产机**（遵守生产/服务端分离约束）。

### 复现命令

```bash
# 元数据（API 匿名可能限流）
curl -sS -H 'Accept: application/vnd.github+json' https://api.github.com/repos/AstrBotDevs/AstrBot
curl -sS -H 'Accept: application/vnd.github+json' https://api.github.com/repos/AstrBotDevs/AstrBot/commits/master

# 源码
curl -sSL 'https://codeload.github.com/AstrBotDevs/AstrBot/tar.gz/feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea' -o astrbot.tar.gz
tar xzf astrbot.tar.gz

# 关键检索
grep -rn 'unified_msg_origin' astrbot/core/platform/astr_message_event.py
grep -rn 'class LLMSummaryCompressor\|text_chat' astrbot/core/agent/context/compressor.py
grep -rn '<Quoted Message>' astrbot/core/agent_media.py
grep -rn '@filter.command("reset")\|@filter.command("new")' astrbot/builtin_stars/builtin_commands/main.py
grep -rn 'update_conversation' astrbot/builtin_stars/builtin_commands/commands/conversation.py
```
