# 工具调用与结果回灌架构（设计稿）

> 结论：**标签保留给不需要本轮观察结果的表达／调度；凡是回答必须依赖执行结果的能力，使用原生结构化 tool call，执行后在同一模型轮次回灌，再生成对用户可见的正文。** `ToolRegistry` 是执行与风险闸，不是模型调用协议；需要补模型端口、供应商适配器、轮次编排和结果账本。本文只提出方案，不声称已经实现。本文代码位置按 2026-11-11 当前工作树核对。
>
> 用语：**本仓库事实**＝可由所列源码直接验证；**业界事实**＝所列官方协议／项目文档所载行为；**项目做法**＝某个外部项目的具体选择；**我的推断／设计判断**＝为 ling 提出的取舍，不能误当现行能力。

## 1. 现状诊断

| 结论 | 证据与含义 |
|---|---|
| **本仓库事实**：`[at:…]`、`[quote:…]`、`[poke:…]` 是正文正则，`parse_expression()` 在最终收尾才剥离，产出 `Directive`；不形成给模型的执行回执。 | `src/ling/capabilities/expression_choice.py:43-87`；`src/ling/kernel/turn.py:2026-2049`。流式出口现在只挡尖括号标记，方括号表达标记仍可能提前显示：`src/ling/kernel/turn.py:1017-1052`。 |
| **本仓库事实**：`<<等会回 90>>` 是延后调度；撤回是提议再确认，不是直接回灌型读取。 | `src/ling/domains/deferred/markers.py:106-181,191-225`；`src/ling/kernel/turn.py:1183-1263,1852-1885`；`src/ling/api/routes/recall.py:282-305`。 |
| **本仓库事实**：`ToolSpec` 有风险、确认、超时、幂等和 `input_schema`；`ToolResult` 有结果码、`call_id`、`observed_at` 和 `untrusted=True`。`ToolRegistry.call()` 自行生成内部 `call_id`，成功输出仅保留前 8000 字符；尚不能直接承担模型供应商 `tool_call_id` 的往返配对。 | `src/ling/capabilities/tools.py:23-62,68-162`。默认集含 `clock.now`、`diagnostics.echo`、`shell.readonly`、`message.recall`：`src/ling/capabilities/tools.py:284-375`。 |
| **本仓库事实**：撤回确认端点确实调用注册表；管理 API 也有工具调用入口，但请求体不能自证 `confirmed`。其他默认工具没有接入主对话轮次。 | `src/ling/api/routes/recall.py:282-305`；`src/ling/api/routes/manage.py:1516-1562`；主循环只消费文本 `chunk.delta`：`src/ling/kernel/turn.py:1054-1066`。 |
| **本仓库事实**：`ChatMessageIn` 只表示 `system/user/assistant` 文本及媒体，`ModelRequest` 无 `tools/tool_choice`，`ModelChunk` 无工具事件。路由器构造请求并只转发文本／完成事件。 | `src/ling/ports/model.py:256-290,326-359`；`src/ling/models/router.py:283-307`。因此模型现在**从原生 API 的意义上不知道注册表里的工具**；提示词中的表达说明不等于 `tools` 字段。 |
| **本仓库事实**：OpenAI 兼容 Chat Completions 适配器只发送 `messages`、解析 `delta.content` 和 `finish_reason`；Anthropic 适配器只解析文本块；Responses 适配器只解析 `response.output_text.delta`。三者均未发送 `tools`，也未装配工具结果。 | `src/ling/models/openai_compatible.py:72-85,123-137,180-204`；`src/ling/models/anthropic.py:84-106,133-159`；`src/ling/models/chat_responses.py:76-99,124-150,215-230`。**不是所有模型接口都只有纯文本输入**：现有图片／视频输入仍在，缺的是结构化工具回路。 |
| **本仓库事实（重要例外）**：看视频已经有一次专用的“标记 → 执行 → 必要时补生成”。失败／没看成时即使模型先写了正文也会重写，并把观察事实作为追加的 `user` 文本交回模型；成功且已有正文时则不补生成，观察留给下一轮媒体证据。它证明回灌有雏形，**但并非通用 tool result 协议**。 | `src/ling/kernel/turn.py:968-1015,1264-1307,1708-1769`；媒体证据注入 `src/ling/kernel/context.py:805-913`。 |
| **本仓库事实**：知识库已按用户正文自动搜索，命中以 `knowledge` 车道注入并标为外部资料；`evidence` 是记忆检索，`media` 是已感知／待查看媒体。模型不能按需迭代搜索或打开一条命中。 | `src/ling/kernel/context.py:690-726,763-803,805-975`。因此近期最值得工具化的是“知识命中后再查详情／读来源”“按需看图片／视频”及未来搜索与读网页；现有自动检索可继续作为首轮背景。 |

**我的推断**：单靠新增 `[search:关键词]` 会重现视频专用补生成的分支，并让搜索失败、过期网页和多次查询分别再造回灌代码。应先建立共同协议，视频作为迁移样本，而不是把所有标签一次性改掉。

## 2. 业界方案调研

以下链接均为本次查阅的官方文档；外部项目做法另行注明。各协议形状不同，不能把 OpenAI 的 `role: "tool"` 生搬到 Anthropic 或 Responses。

| 方案 | 已核实的协议事实 | 对 ling 的判断 |
|---|---|---|
| OpenAI Chat Completions function calling | **业界事实**：请求 `tools: [{type:"function",function:{name,description,parameters,strict}}]`；响应 assistant `tool_calls[]`，每项有 `id`、函数名和 JSON 字符串参数。应用执行后先保留 assistant 工具调用消息，再逐项追加 `{role:"tool",tool_call_id:id,content:...}` 并再次请求模型。`tool_choice` 可设 `auto/required/none` 或指定函数；`parallel_tool_calls:false` 可限制本次至多一个函数。并行能力视模型／提供商而异。 | **设计判断**：先在兼容适配器做单工具最小闭环，再启用只读工具并行；不能由 `finish_reason` 代替真正解析 `tool_calls`。[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) |
| OpenAI Responses API | **业界事实**：顶层同样可传 `tools`，响应 `output` 中出现 `type:"function_call"`、`call_id`、`name`、`arguments`；回灌 `type:"function_call_output",call_id,output`，再次调用模型。它不是 Chat Completions 的 `role:"tool"`。 | **设计判断**：`chat_responses.py` 应独立编码／解码工具 item，不能复用 Chat Completions wire 对象。[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) |
| Anthropic Messages tool use | **业界事实**：请求 `tools` 中定义 `name/description/input_schema`；assistant `content` 可同时含文本和一个或多个 `{type:"tool_use",id,name,input}`，`stop_reason:"tool_use"`；下一条 **user** 消息的 content 放相应 `{type:"tool_result",tool_use_id,content,is_error}`，再请求模型。并行时同一 assistant 消息的多个 `tool_use` 都要在下一条 user 消息中配齐结果，工具结果块排在普通文本之前；工具调用轮次须保留原 assistant 消息。 | **设计判断**：`anthropic.py` 必须用 content block 模型，不能伪造一个文本 `user` 结果消息。[Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)、[Parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use) |
| MCP | **业界事实**：`tools` 是供模型发现并调用的有 schema 操作（`tools/list`、`tools/call`）；`resources` 是由宿主应用选择如何纳入上下文的 URI 数据（`resources/list/read`）；`prompts` 是用户显式选择的模板（`prompts/list/get`）。三分法区分“执行动作”“提供材料”“选择工作流”，不是三个等价工具名空间。 | **设计判断**：ling 先做内建注册表和模型协议桥，未来若接 MCP，MCP tool 通过受审的能力目录映射；资源进知识／媒体素材链，prompt 不自动获得执行权。[MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[resources](https://modelcontextprotocol.io/specification/2025-11-25/server/resources)、[prompts](https://modelcontextprotocol.io/specification/2025-11-25/server/prompts) |
| 结构化输出／约束解码 | **业界事实**：OpenAI 的 strict function schema／Structured Outputs 和 Anthropic 的 strict tool use 可约束生成的参数形状；Anthropic 明确采用 grammar-constrained sampling，并说明 JSON Schema 仅支持子集。 | **设计判断**：它们降低类型／缺字段错误，**不能证明参数引用真实、URL 安全、用户已授权、资料属实，亦不能替代执行与结果回灌**。仍须校验 schema、可见短号、当前权限与出处。[OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)、[Anthropic strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use) |
| 文本标记与原生工具共存 | **外部项目做法**：Open WebUI 会解析流中的 `<think>…</think>` 等文本标记供 UI 展示，同时在 native mode 使用模型原生工具循环，回放工具结果。这是**文本标记负责呈现、工具负责执行**的真实近例；`<think>` 并不是 `at/poke` 这种轻动作，因此不能冒充“同类动作标签已被业界标准化”的证据。未找到可信官方资料证明“模型输出动作标签 + 原生工具”是统一标准。 | **我的推断**：ling 可有自己的混合约定，但要靠服务端能力目录、互斥语法和验收测试维持，而不能因 Open WebUI 的近例声称此组合天然可靠。[Open WebUI reasoning tags](https://docs.openwebui.com/features/chat-conversations/chat-features/reasoning-models/)、[tools](https://docs.openwebui.com/features/extensibility/plugin/tools/) |

**业界事实＋设计推断**：工具输出可能很长、包含第三方指令或失败信息；Anthropic 官方明确要求把第三方数据放 `tool_result` 而非 system 指令，并用 `is_error` 报错。并行结果要按调用 ID 配对，不能凭返回先后猜。Open WebUI 的 Native Mode 将 RAG 知识检索暴露为模型可选择的工具；ling 迁移时必须明确是否保留现有首轮自动检索。[Anthropic handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)、[Open WebUI tools](https://docs.openwebui.com/features/extensibility/plugin/tools/)

## 3. 标签与工具的明确分工

**设计判断：判据不是“看起来像动作”或“只读／写入”，而是“模型当前这句话或下一步是否必须知道执行结果”。**

1. **表达层标签**：参数来自本轮已给定的安全短号；执行只影响本条消息的表达、投递或延后；模型无需本轮再观察结果才能决定正文。保留 `[at]`、`[quote]`、`[poke]`、`<<等会回…>>`。即使模型不需要回灌，系统仍应分别记录送达／失败回执，下一轮若相关再以真实事实呈现。依据：`src/ling/capabilities/expression_choice.py:43-87`、`src/ling/kernel/turn.py:1183-1263,2026-2049`；**这是设计判据**。
2. **感知／查询工具**：执行结果决定回答的事实、可继续筛选、可能失败或要引用来源。`knowledge.search`／`knowledge.open`、`web.search`／`web.read_url`、`media.look_at_image`／`media.watch_video` 一律结构化调用并同轮回灌。图像若已随当前请求附上且模型直接可见，不必再调用；需要另取存储素材时才调用。依据：现有图片输入 `src/ling/ports/model.py:256-266`、自动知识检索 `src/ling/kernel/context.py:763-803`、视频专用回灌 `src/ling/kernel/turn.py:1264-1307`；**目标工具名是提案**。
3. **不可逆／外发动作**：无论是否需要返回数据，都走服务端受审的提议／确认执行流程。`recall` 保留现有 `<<撤回…>>` 作为**兼容提议语法**，目标迁移为结构化 `message.recall` 提议；模型发 tool call 不能自带 `confirmed=true`，确认必须来自用户操作后的服务端状态。依据：`src/ling/capabilities/tools.py:96-106,130-136,338-375`、`src/ling/api/routes/manage.py:1516-1562`、`src/ling/api/routes/recall.py:282-305`；**迁移选择是设计判断**。

能力目录对模型仅声明**当轮、当实例、当权限**可用项。提示词的 `capabilities` 区说明：`[at/quote/poke]` 只修饰将发出的消息；`<<等会回…>>` 只决定何时回复；获取新事实用提供的原生工具，**不要写 `[search:…]`、`[read_url:…]` 或编造查询结果**；撤回只提出待确认请求。若当前 provider 不支持可靠工具调用，需要结果的能力在该轮标为不可用并诚实说明，不能退化成没有回灌的“搜索标签”。现有表达标签还须按 `CAPABILITY-TAG-SYSTEM.md` 的能力快照、目标校验、流式遮挡方案加固；这不是工具回路的替代品。依据：`src/ling/kernel/context.py:1259-1346`、`src/ling/kernel/turn.py:1017-1052`；**上述提示词策略是设计判断**。

## 4. 目标架构

### 4.1 端口、适配器和模型能力

**设计提案**：在 `ports/model.py` 增加供应商无关的 `ToolDefinition`、`ToolCall{id,name,arguments}`、`ModelStep{text,tool_calls,finish_reason}`；`ModelRequest` 加 `tools`、`tool_choice`、`parallel_tool_calls` 及本轮临时 transcript。流式事件采用有类型的 `text_delta`、`tool_call_start`、`tool_arguments_delta`、`step_done`；仅在一组参数完整且 JSON 校验通过后调用 handler。`ToolSpec.input_schema` 是 schema 来源，但必须补齐真实参数 schema（当前只有撤回明确配置）：`src/ling/capabilities/tools.py:38-50,303-375`。供应商能力探测应区分 `chat`、`native_tools`、`parallel_tools`、`strict_schema`，不能从“OpenAI 兼容”这个 kind 推定全支持：`src/ling/models/openai_compatible.py:1-5`；现有能力枚举只有 `chat/embed/rerank/tts/asr`：`src/ling/ports/model.py:39-71`。

三个适配器各自编码请求和解码工具事件：Chat Completions 处理流式 `delta.tool_calls` 的 index/id/name/arguments 片段；Responses 处理 `response.output_item.*` 与 function-call 参数完成事件；Anthropic 处理 `content_block_start/delta/stop` 中 `tool_use` 与 `input_json_delta`。要以官方 wire fixture 对每种适配器做协议测试；兼容供应商逐型号探测，不识别者不给工具。当前只解析文本的证据：`src/ling/models/openai_compatible.py:123-137`、`src/ling/models/chat_responses.py:124-150`、`src/ling/models/anthropic.py:133-159`。**具体事件名和可用性随供应商版本须在实施时以对应官方文档和 fixture 再核实**。

### 4.2 同轮编排与上限

**设计提案**：`turn.py` 在一次轮次中执行 `编译上下文 → 模型步骤 → 校验工具调用 → Registry/领域执行 → 归一化结果 → 原协议回灌 → 模型步骤`，直到返回无工具调用的最终文本。初始硬上限：**最多 3 批工具、每批最多 3 项、合计最多 6 项、整轮 45 秒工具墙钟时间**；这是待压测的策略常数，不是当前配置或行业标准。每项另受 `ToolSpec.timeout_seconds` 限制：`src/ling/capabilities/tools.py:48,138-147`。超限/取消时为已提出但未执行的 ID 生成 `failed/timeout/skipped` 回执，允许一次 `tool_choice:none` 的收尾生成；它只可说“没查成”，不得报成功。收尾也失败则回明确错误，不拿半截候选正文充成事实。

只有**独立、只读、权限已过**的调用可并行；有副作用或共享状态的调用按模型提出的顺序串行。所有结果最终按模型调用出现顺序回传，并以 provider `call_id` 配对。`ToolRegistry.call()` 目前生成另一个内部 ID：`src/ling/capabilities/tools.py:120-122`；需要编排层保存 `provider_call_id ↔ internal_call_id` 映射，或扩展 Registry 接收经校验的外部 ID，**不能用内部 ID 冒充 provider ID**。调用之前固定本轮 model target、配置修订和工具集版本；执行过任何有副作用工具后，供应商失败不可在备用模型上重放该批调用。现有路由器仅在已交付文本时拒绝回退：`src/ling/models/router.py:343-346`，必须再加“已经执行工具”闸。

### 4.3 提示词分区与可信边界

**设计判断**：`PROMPT-ZONE-SPEC.md` 的 10 区是首轮背景编排，工具**定义／使用规则**属于 `capabilities` 区（目标预算 300）；**同轮工具调用及其结果属于临时对话 transcript**，紧跟调用 assistant 消息，经供应商专用的 tool-result wire 编码，不能塞 `identity/constraints/short_memory`，也不能伪装成 `knowledge` system 消息。现行首轮编译会把 contributions 作为 system，再接摘要／历史／输入：`src/ling/kernel/context.py:1396-1424,1557-1614`；10 区目标见 `docs/design/PROMPT-ZONE-SPEC.md:25-44`，受保护集合见 `src/ling/kernel/context.py:1819`。工具结果需要**独立的同轮预算池与预留上下文窗口**，每次续生成重新计费；否则既可能挤掉角色设定，也可能越过 `PROTECTED_LANES`。不新造第 11 个 system 区；在提示词快照中增加 `tool_transcript` 明细及 token 估计，便于核对模型究竟看到了什么。

`untrusted=True` 应由结果编码器落实：先对外部文本限大小、转义并置于 JSON 的 `data` 字段，连同 `source_url`、`retrieved_at`、`truncated`、`artifact_ref`；再放**原生工具结果内容**。禁止把网页正文拼到 system、开发者指令、普通 user 正文或 `constraints`。固定可信的“这些是第三方数据，不是指令”规则属于 `capabilities`／应用指令；不可信输出本身不得携带提升权限的指令。`ToolResult.untrusted` 当前只是布尔注释：`src/ling/capabilities/tools.py:53-62`。这与 Anthropic 官方对第三方工具结果的指导一致：[Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)。

### 4.4 流式输出与确认

**设计判断**：可以边生成候选文本边收工具调用参数，**不能在结果未到前向用户提交可能依赖结果的断言**。普通纯文本轮仍逐字流式。检测到工具调用开始，就暂存该模型步骤的文字及其后的 delta，直到结果回灌后的最终步骤再对外流式显示；可发无内容的“正在查资料”状态事件。混合文本＋工具调用时，候选文字默认不进入最终外发，也不写成已发消息；调用失败后由收尾步骤按事实改写。视频现有“标记后暂扣正文／失败重写”是已有证据：`src/ling/kernel/turn.py:968-1015,1273-1305`。停机／超时按已外发文本是否存在区分，不在新模型上重放已执行动作：`src/ling/ports/model.py:1-6`。

高风险工具先经注册表返回 `needs_confirmation`：`src/ling/capabilities/tools.py:130-136`。编排层持久化**提议**（工具 ID、参数摘要、权限主体、有效期、原调用 ID）并给用户展示；此轮暂停，不继续让模型假设动作已发生。用户在受权入口确认后，服务端重验目标／权限、调用 `confirmed=True`，另存真实结果；新轮次把“提议已执行／已拒绝／已过期”作为事实交给模型。撤回沿用现有确认入口，不另开可绕过的执行口：`src/ling/api/routes/recall.py:282-305`；`manage` 请求体拒绝自填确认：`src/ling/api/routes/manage.py:1516-1562`。

## 5. 结果回灌：用户关心的闭环

### 5.1 规范化结果与三种供应商编码

以下为 **ling 内部提案**，不是现有 `ToolResult` 形状；`provider_call_id` 用于协议配对，`internal_call_id` 用于执行审计。结果正文一律是数据，不准在其中写新的系统指令。

```json
{
  "provider_call_id": "call_A",
  "internal_call_id": "call_01...",
  "tool_id": "web.read_url",
  "outcome": "succeeded",
  "observed_at": "2026-11-11T08:30:00Z",
  "source": {"url": "https://example.org/page", "title": "页面标题"},
  "data": {"excerpt": "正文节选", "artifact_ref": "artifact_01..."},
  "truncated": true,
  "untrusted": true,
  "error": null
}
```

**设计提案**：Chat Completions 先回放原 assistant `{role:"assistant",tool_calls:[...]}`，再为 `call_A` 回放 `{role:"tool",tool_call_id:"call_A",content:"<上面 JSON 的序列化字符串>"}`。Responses 在 `input` 保留 function-call item，再追加 `{type:"function_call_output",call_id:"call_A",output:"<JSON 字符串>"}`。Anthropic 保留原 assistant `tool_use` block，下一条 `{role:"user",content:[{type:"tool_result",tool_use_id:"call_A",content:"<JSON 字符串>",is_error:false}]}`。前两者的错误亦编码在 JSON `outcome/error`；Anthropic 再置 `is_error:true`。官方 wire 依据：[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)、[Anthropic handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)。

### 5.2 配对、长度、失败、跨轮

- **设计判断：配对**。接收时检查本模型步骤的 `provider_call_id` 唯一、工具名在本轮白名单、参数 JSON 完整；每个提议的 ID 恰好有一条结果（包括拒绝、失败、未执行）。并行执行可乱序完成，归档与回灌按模型提出顺序；不能因先完成就覆盖另一条结果。Anthropic 对并行 tool result 的要求见 [Parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use)。
- **设计判断：长度**。不要沿用 `ToolRegistry` 无提示的 `output[:8000]` 作为最终回灌策略（现状：`src/ling/capabilities/tools.py:157-162`）。按工具给 token／字符双限和总池：搜索返回标题、URL、时间与少量摘要；读网页保留前后相关片段、章节锚点与可再读的 `artifact_ref`；图片／视频回传感知摘要与证据 ID，原始媒体留在媒体存储，不把 base64 嵌入文本。超长时 `truncated=true`，模型可按需调用 `open_section`；压缩前原文受限留存，禁止摘要伪装成原文。**同轮结果在 tool transcript**；经核验、需要长期复用的网页事实才按来源／时效进入 `knowledge`，媒体描述进入 `media`，个人记忆证据进入 `evidence`，不可简单把每个 tool result 永久写进这些车道。现有三条车道及来源语义：`src/ling/kernel/context.py:763-803,805-913`、`docs/design/PROMPT-ZONE-SPEC.md:29-40`。
- **设计判断：失败**。`failed/timeout/unsupported/needs_confirmation` 的 `data` 为空，`error={code,message,retryable}`；不可把“无结果”编码成空成功。超时后的副作用状态可能不确定时额外用 `outcome:"unknown"`（需扩展现有枚举 `src/ling/capabilities/tools.py:30-35`），先核验执行回执，**不自动重试非幂等工具**。下一模型步骤被要求说明“未取得资料／未确认”，不得编出查到的内容。
- **设计判断：跨轮记忆**。新增按 `turn_id/window_id/provider_call_id/internal_call_id` 索引的工具调用审计表，保存工具版本、参数摘要、状态、时间、来源、可见性和结果 artifact 引用，原始网页全文受限存储并按保留期清理。最近轮次可把短结果事实摘要带入 `history`／`summary`，但只有已校验且需要复用的事实才晋升到 `knowledge/media/evidence`。现有 `Message`、`PromptSnapshot` 与媒体证据分别存在：`src/ling/infra/models.py:237,367,1669`；**建议的新表尚不存在**。不能只保存“她说已查过”，要保存真实调用回执及它提供给模型的精确版本。
- **设计判断：可观测性**。每个调用记录 `proposed/validated/started/completed/injected/answer_checked`，以调用 ID 串起来；快照记录工具结果哈希、截断标记、来源 ID、送入哪个模型步骤、输出是否引用来源。对“回答声称已看图／已读网页却没有对应成功结果”“声称具体数值但结果缺失／失败”“结果已注入而模型用了相矛盾的来源”做确定性告警与评估集；语义矛盾检测只能提供信号，不能保证识别所有编造。对需要实时事实的请求，可按工具成功状态强制最终回答只作可核实的有限声明。现有 prompt snapshot 在首次调模型前存一次：`src/ling/kernel/turn.py:954-966`，目标须覆盖各次续生成。

## 6. 可独立验收的迁移路径

1. **基线与能力真值**：记录当前 provider 的工具支持探测结果，补严标签目标校验与流式过滤；用 `[at:abc]`、跨分片标签、视频失败、知识检索命中／失败跑回归。此步不引入模型工具，也不改变已有对话主路径。证据入口：`src/ling/capabilities/expression_choice.py:43-87`、`src/ling/kernel/turn.py:1017-1052,1264-1307`、`src/ling/kernel/context.py:763-803`。
2. **端口＋只读单工具闭环**：加抽象工具事件与 Chat Completions／Responses／Anthropic fixture 适配；先只让 `clock.now`／受限 `knowledge.search` 在测试模型上调用，强制单调用；断言模型请求有 schema、收到 call、Registry 执行、配对回灌、第二步答案引用结果，失败明确可见。生产开关默认关闭。现状端口：`src/ling/ports/model.py:256-359`；默认工具：`src/ling/capabilities/tools.py:303-337`。
3. **接入真实信息能力**：优先 `knowledge.open` 与已存媒体的 `look_at_image/watch_video`，再引入受限 `web.search/read_url`；保留首轮自动知识检索，明确用户若问“再查”则由工具补查。把视频专用回灌迁到统一结果模型时，用“读到／额度不足／文件丢失／已有正文”四组回归保证行为不倒退。现有视频机制：`src/ling/kernel/turn.py:1264-1307,1708-1769`。
4. **并行、预算和持久化**：只读并行、每 ID 恰好一条结果、长结果 artifact 分页、`untrusted` 包裹、每步 prompt snapshot、跨轮结果引用及审计表。验证乱序返回、超时、提示注入样本、角色设定不被工具文本挤掉。预算基线：`src/ling/kernel/context.py:1349-1394,1819`。
5. **风险动作迁移**：`message.recall` 原生调用只产生待确认提议；用户确认后同一 Registry handler 执行，兼容旧标签提议。验证模型自己无法 `confirmed=true`、过期／越权／重复确认均拒绝。现有确认硬门：`src/ling/capabilities/tools.py:96-106,130-136`；`src/ling/api/routes/manage.py:1516-1562`。

## 7. 风险与未决问题

| 项目 | 判断与待决策 |
|---|---|
| 兼容 provider 支持度 | **本仓库事实**：OpenAI 兼容适配器明确说能力要逐型号探测：`src/ling/models/openai_compatible.py:1-5`。**未决**：最小探针与用户界面的“工具不可用”提示如何配置，不能宣称所有兼容端点都能原生调用。 |
| 模型接口差异 | **业界事实**：OpenAI Chat Completions、Responses、Anthropic 的结果回灌消息不同（第 2 节官方文档）。**未决**：是否首期只开放一个 provider，再逐个适配；不应把其余接口静默转为文本标签。 |
| 预算值 | **我的推断**：第 4 节 3 批／6 项／45 秒及结果 token 池是首版上限建议，需用真实延迟和角色上下文压测调整。现有上下文软硬上限位置：`src/ling/kernel/context.py:1822-1831`。 |
| 知识时效与隐私 | **本仓库事实**：知识车道含来源／置信度，媒体有可见性和来源 ID：`src/ling/kernel/context.py:763-803,895-913`。**未决**：外部网页 artifact 的保留期、跨主体隔离与重查策略；不能因为“工具调用成功”就把网页主张永久当真。 |
| 提示注入与确认 | **业界事实**：第三方工具结果有间接提示注入风险，不能升级到 system 指令：[Anthropic handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls)。**我的推断**：先仅开放窄范围读工具，网页读取要限制网络范围／重定向／内容大小；高风险动作继续走现有服务端确认。 |
| 部分流式文本与重试 | **本仓库事实**：现有路由器只在已交付文本后阻止回退：`src/ling/models/router.py:343-346`。**未决**：工具已执行但尚未交付文本时的供应商失败如何恢复；方案是记录已执行状态，禁止副作用重放，改由同一 target 收尾或明确失败。 |

