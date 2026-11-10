# 能力标签系统：现状评审与设计建议

> **与工具调用方案的关系**：本文处理 `at/quote/poke/defer` 等文本标记的可用性、解析、流式遮挡和投递回执；需要“执行后把观察结果交回模型”的搜索、读网页、看媒体等能力，按 [工具调用与结果回灌架构](TOOL-CALLING-ARCHITECTURE.md) 走原生结构化 tool call。撤回保留本文的标签提议兼容路径，目标是结构化提议加现有服务端确认。两份文档并行适用；本文第 2d 节“短期继续用标签”仅指轻动作和过渡期，不应解读为用搜索标签替代结果回灌。

本评审以仓库当前实现为基线；第 5 节另以**其他项目做法**、**业界通识**、**我的推断／设计建议**区分证据与方案。前文的**代码事实**表示从 ling 代码可直接确认的行为，**设计判断**表示建议或由代码推导的风险。本文中的“标签”泛指模型写出的动作标记；`[msg_id:…]`、`[recall:…]` 是输入侧引用，不是模型应原样输出的动作。依据：`src/ling/kernel/context.py:357-373,1593-1602`。

## 1. 现状诊断

**代码事实：模型目前靠提示词获知能力，没有原生 function calling。** `render_expression_help()` 列举 @、引用、拍一拍；`ContextCompiler.compile()` 把它作为 `capabilities` 系统内容加入每轮上下文；回复完毕后 `turn._finalize()` 才调用 `parse_expression()`，把三种标记剥离并转成 `Directive`，经事件传给投递层。`ModelRequest` 没有 `tools` 或结构化动作字段。依据：`src/ling/capabilities/expression_choice.py:43-116`、`src/ling/kernel/context.py:1259-1281`、`src/ling/kernel/turn.py:2026-2049,2312-2325`、`src/ling/kernel/bridge.py:1673-1699`、`src/ling/ports/model.py:273-290`。

**代码事实：至少有四组不同的输入/输出标记，不能把示例 `[defer:90]` 当作已实现语法。**

| 用途 | 模型应写的语法 | 真正的解析位置 | 告知模型的位置 |
|---|---|---|---|
| @、引用、拍一拍 | `[at:QQ号]`、`[quote:消息ID]`、`[poke:QQ号]` | `src/ling/capabilities/expression_choice.py:43,61-87` | 同文件 `90-116`，由 `src/ling/kernel/context.py:1263-1281` 注入 |
| 延后 | `<<等会回 90>>`；**不是** `[defer:90]` | `src/ling/domains/deferred/markers.py:69,106-181` | 示例在同文件 `223-225`，完整说明在 `src/ling/kernel/context.py:1313-1346` |
| 看视频 | `<<看视频 m3>>` | `src/ling/domains/media/video.py:47-48,104-116` | 按未读视频逐条生成，`src/ling/kernel/context.py:915-969` |
| 撤回 | `<<撤回 r…>>` 或 `<<撤回>>` | `src/ling/domains/conversation/recall.py:554-555,586-600` | `src/ling/capabilities/expression_choice.py:119-147`，由 `src/ling/kernel/context.py:1282-1296` 注入 |

**代码事实：输入侧引用另有两种。** `[msg_id:…]` 是可引用的入站平台消息 ID，`[recall:…]` 是可撤回的本地消息短号；两种 ID 不能混用。媒体提示中的 `[image]`、`[voice]`、`[video]`、`[file]` 是内容占位，不是动作；OneBot 的 `[CQ:…]` 是连接器入站格式。依据：`src/ling/kernel/context.py:357-373,1593-1602`、`src/ling/kernel/bridge.py:1799-1805`、`connectors/onebot/message.py:22`。

**代码事实：告知与执行条件已漂移。** `render_expression_help()` 在群聊才列引用，但 `context.py` 又无条件加入一段 `[quote:那个 id]` 说明；私聊仍会看到“可以引用”的文字。延后提示按 `not is_group` 给所有私聊，实际只允许 `web` 或空渠道；外部渠道的延后请求在登记前被拒。`supports_poke` 默认 `True`，全仓只有 `context.py` 的读取点，没有给 `_supports_poke` 赋值的接线；OneBot 实现了 `send_poke`，QQ 官方连接器构造发送载荷时则没有处理 `poke_targets`。依据：`src/ling/capabilities/expression_choice.py:90-111`、`src/ling/kernel/context.py:1271-1275,1297-1310,1330-1346`、`src/ling/domains/deferred/service.py:68-85`、`src/ling/kernel/turn.py:1604-1614`、`connectors/onebot/connector.py:258-269`、`connectors/qq/connector.py:315-345`。

**代码事实：加一个可外发的方括号标签至少要触及六类接线。** 语法正则、`Directive` 字段、解析分支、提示词说明、轮次事件载荷、投递层/连接器处理分别在 `expression_choice.py`、`turn.py`、`bridge.py` 和连接器里；如果还要在流式中隐藏，则要改流式出口。尖括号动作另有领域解析器、`context.py` 告知、`marker_stream.py`/`turn.py` 流式处理及业务服务。这里“六类”是按现有路径盘点的维护面，不是规定每个新动作一定新增六个文件。依据：`src/ling/capabilities/expression_choice.py:43-116`、`src/ling/kernel/turn.py:1017-1052,2026-2049,2312-2325`、`src/ling/kernel/bridge.py:1673-1699`、`src/ling/kernel/marker_stream.py:62-90`、`src/ling/domains/deferred/markers.py:63-69,139-181`。

**设计判断：当前“有实现”只意味着模型可能在这一轮看到一段自然语言；并不意味着此实例、此会话、此时的动作可执行。** 现有 `CapabilityDescriptor` 只有私聊、群聊、主动发送、编辑、媒体、撤回等粗粒度位，缺少 @、引用、拍一拍；`DeliveryService` 也只据它拦群/私聊、主动发送和媒体，无法据此拒绝不支持的表达标签。依据：`src/ling/contracts/v1.py:699-717`、`src/ling/capabilities/delivery.py:125-147`。

## 2. 应该怎么解决

### a) 建立统一能力目录，但保留动作的领域执行器

**设计判断：需要一个统一的“模型可请求能力目录”，同时驱动可用性筛选、提示词、最终解析与结果分类。** 目录登记的是动作协议，不是把视频读取、撤回、消息投递的业务实现搬进一个大文件。每个动作仍由原领域执行器完成；目录只提供唯一的语法/参数/前置条件/结果契约。它应将“可声明给模型”与“平台实际执行成功”分开：前者来自当前连接器实例的能力快照，后者来自执行回执。现有提示词各写在 `expression_choice.py`、`context.py` 和媒体路径，是新增动作必须修改多处的直接原因。依据：`src/ling/capabilities/expression_choice.py:90-147`、`src/ling/kernel/context.py:915-969,1259-1346`、`src/ling/kernel/bridge.py:1673-1706`。

建议接口形状如下，**是方案示意，不是现有代码**：

```python
@dataclass(frozen=True)
class TurnEnvironment:
    channel: str                 # web / onebot / qqbot / wechat / phone / physical
    connector_instance: str | None
    account_ref: str | None
    conversation_kind: str       # private / group / call / in_person
    relationship: str | None     # 已核实的关系事实；未知则 None
    channel_features: frozenset[str]  # 当前实例经适配器证实的能力
    visible_message_ids: frozenset[str]
    recallable_refs: frozenset[str]
    unread_video_refs: frozenset[str]
    allowed_targets: frozenset[str]    # 本轮真实可指向的账号/成员

@dataclass(frozen=True)
class TagSpec:
    key: str                     # message.at / message.quote / message.poke / reply.defer ...
    syntax: str                  # 给模型看的唯一模板，如 [quote:{message_id}]
    pattern: Pattern[str]        # 静态、完整的合法形状；不能单独决定是否执行
    candidate_pattern: Pattern[str]  # 仅用于诊断/安全清洗的宽松候选式
    parse_arg: Callable[[str], object | None]  # 语法和类型校验
    available: Callable[[TurnEnvironment], bool]
    validate_target: Callable[[object, TurnEnvironment], bool]
    model_help: str              # 用途、条件、参数来源；与 syntax 同一条定义
    result_semantics: str        # delivered / proposed / scheduled / observed ...
    user_result: Mapping[str, str]  # succeeded / failed / unsupported / uncertain 的用户文案
    risk: str                    # read_only / outbound_effect / irreversible
    reversible: bool
    requires_confirmation: bool
    max_occurrences: int = 1
    placement: str = "inline"   # inline / only_line / standalone
    literal_policy: str = "markdown_code_or_backslash"
    invalid_policy: str = "strip_candidate_and_record"
    stream_prefixes: tuple[str, ...] = ()  # 同一规格驱动暂扣器
    tool_id: str | None = None   # 已有 ToolRegistry 执行器的可选映射
```

**设计判断（第 5 节修订）：`pattern` 收紧到合法参数形状，但仍不能证明目标真实、当前可执行，且单条正则无法分辨“执行标记”与“举例引用”。** `[at:abc]` 目前会被剥掉并进入 @ 列表；QQ 号要经数字形状校验及本轮允许对象校验。引用必须在 `visible_message_ids` 中；视频、撤回只接受本轮发给模型的短号；延后用已有 `parse_duration_seconds()` 加 300 秒策略，上限与“只写标记”约束必须同时从同一条规格渲染。候选识别、字面量保护、流式暂扣和结果留痕也应由同一规格驱动，不能只放一个 `pattern`。依据：`src/ling/capabilities/expression_choice.py:43,67-87`、`src/ling/kernel/turn.py:2032-2049`、`src/ling/domains/deferred/markers.py:106-181`、`src/ling/kernel/context.py:1313-1341`。

目录应区分 `advertise()`、`parse()`、`validate()`、`execute()` 四步，最后一步仍复用现有领域服务；失败须有结构化原因码，不能靠“标记消失”代表成功。`<<撤回>>` 的待确认语义、`<<等会回 90>>` 的单独成行语义和视频读取后补生成语义应作为动作策略保留，而非用一条通用正则覆盖。依据：`src/ling/kernel/turn.py:1183-1269,1852-1885`、`src/ling/domains/deferred/markers.py:31-38,139-181`。

### b) 能力快照怎样生成

**设计判断：渠道特性必须由运行中的连接器实例明确声明，未知即不可用。** 在现有 `CapabilityDescriptor` 上新增或由适配器派生 `supports_mention`、`supports_quote`、`supports_poke` 等细项；“能发图/语音/表情”也应分别表述，不能用 `supports_media=True` 一概代替。再把 `enabled`、群/私聊、账号权限、当前可用目标和资源短号求交，得到本轮 `TurnEnvironment`。不能仅从“QQ/微信”平台名称推出支持度：同仓库已有 OneBot、QQ 官方和 qqbot 三种 QQ 实现，其发送路径不同。依据：`src/ling/contracts/v1.py:699-717`、`connectors/onebot/connector.py:205-269,1123-1139`、`connectors/qq/connector.py:315-345,407-421`、`connectors/qqbot/connector.py:239-255,612-634`。

### c) 提示词用短而结构化的事实清单

**设计判断：自然语言列表可以保留，但应改成固定、紧凑的“当前环境 + 可用动作”块；只列本轮真正可用的动作。** 示例是“已核实支持 @/引用/拍一拍的 OneBot 群，且上下文实际给出了目标和消息 ID”的**假设渲染结果**：

```text
当前环境：OneBot / 实例 qq-main / 群聊；当前对象 770140（本群成员）。
本轮可用：
- [at:770140] 点名此人；QQ号只能取本轮给出的成员号。
- [quote:771] 引用消息；ID只能取上文 [msg_id:…] 中出现的值。
- [poke:770140] 拍一拍；仅在本实例确认支持时出现。
标记须与要发的正文同写；它们会从最终消息中剥离。可不用。动作是否成功以后续回执为准。
```

若是 Web 私聊且确认支持延后，则单列 `<<等会回 90>>：只写这一行，最长 300 秒，本轮不立即回复`；若没有可撤回消息，就不出现撤回条目；没有视频短号，就不出现看视频条目。不要写 `[defer:90]`，因为现有解析器只认 `<<等会回 …>>`。依据：`src/ling/kernel/context.py:915-969,1282-1296,1313-1346`、`src/ling/domains/deferred/markers.py:69,223-225`。建议把目录块作为单条能力贡献，避免当前引用说明重复，并给它整体预算；现有 `capabilities` 单条预算 300、后续可能被总预算裁剪，最终提示词快照应核对它是否真的被送入模型。依据：`src/ling/kernel/context.py:125-149,1268-1309,1364-1377,1391-1407,1861-1893`。

### d) 标签与 function calling 的选择

**代码事实：现阶段模型端口不承载工具 schema；`ToolRegistry` 存在不等于模型已能直接调用它。** 撤回路径也明确通过正文标记提出待确认提议。依据：`src/ling/ports/model.py:273-290`、`src/ling/capabilities/tools.py:38-50,68-162`、`src/ling/domains/conversation/recall.py:537-543`、`src/ling/kernel/turn.py:1852-1862`。

**设计判断：短期继续用标签可行，但上限是“模型按提示词尽量遵守格式”，不是可靠的参数协议。** 它会在参数臆造、当前条件过期、正则不识别、流式先泄露标记、一个输出混杂正文和动作时失效；服务端必须二次验证并给可观测结果。已有真实例子是历史上的 `[quote:3]` 假 ID；当前 `[at:abc]` 也会通过粗正则。依据：`src/ling/kernel/context.py:357-365`、`src/ling/capabilities/expression_choice.py:43,67-87`、`src/ling/kernel/turn.py:1017-1052,2026-2049`。

**设计判断：先修能力真值与校验，再考虑结构化输出。** 多动作组合、不可逆动作、准确回执要求提高时，引入 `ModelRequest` 的可选 JSON Schema / constrained decoding 或原生 tools；同一 `TagSpec` 的参数规则可导出 schema，但执行仍要服务端确认及运行时校验。不能把 JSON 合法等同于权限合法。保留纯文本标签作为不支持结构化输出的 provider 的兼容路径，并记录每种 provider 的失败率。依据：`src/ling/ports/model.py:273-290`、`src/ling/capabilities/tools.py:96-162`、`src/ling/kernel/turn.py:2032-2049`。

### e) 与 `tools.py`、`embodiment.py` 的边界

**代码事实：`ToolSpec` 是工具执行端口，含风险、可逆、确认、超时、幂等和输入 schema；`ToolRegistry.call()` 负责确认门与执行结果归类。`ALLOWED_AVATAR_ACTIONS` 则是具身协议允许的动作名集合，`avatar_intent()` 还要求会话在场证据有效。** 这两者都不是目前方括号标签的解析注册表。依据：`src/ling/capabilities/tools.py:23-162`、`src/ling/capabilities/embodiment.py:22-35,263-292`、`src/ling/capabilities/expression_choice.py:43-87`。

**设计判断：统一“向模型声明能力”的目录和结果语义，保留不同执行端口。** 撤回条目映射现有 `message.recall` 的 `ToolSpec`，沿用其确认门，不能再造一条宽松执行路径。@、引用是消息发送的组成部分，拍一拍是发送后的独立平台动作；若强行塞进当前 `ToolRegistry`，`external_publish` 会触发“必须确认”规则，需要先重做批量发送和确认边界，收益不大。具身动作可作为另一类目录条目，但只有会话有效、客户端支持并能回报真实执行时才宣告；不应仅因动作名在白名单里就显示给模型。依据：`src/ling/capabilities/tools.py:96-106,284-375`、`connectors/onebot/connector.py:227-269`、`src/ling/capabilities/embodiment.py:92-113,265-292`。

### f) 对接“当前环境事实”

**设计判断：每轮只采集一次不可变环境快照，既用于环境事实段，也用于标签目录筛选和最终执行校验。** `ContextCompiler.compile()` 目前只收 `is_group` 与可选平台消息 ID，没有连接器实例能力快照；建议由入站桥传入 `TurnEnvironment`，再渲染“平台 / 实例 / 群私 / 已核实关系 / 此渠道能做什么”。“能做什么”直接来自 `available(spec, environment)` 的结果，不再维护第二份手写清单。投递前重新核实时间敏感条件（消息是否仍可撤回、连接器是否仍可用、目标是否仍在范围）；环境快照只证明提示生成当时知道什么。依据：`src/ling/kernel/context.py:495-513,1259-1346`、`src/ling/kernel/bridge.py:1650-1699`、`src/ling/domains/conversation/recall.py:705-707`。

## 3. 具体缺陷清单

以下“后果”区分已由代码直接确定的行为与尚需线上观测的影响。

| 位置 | 代码事实与后果 | 修法（设计判断） |
|---|---|---|
| `src/ling/kernel/context.py:1271-1275`；`src/ling/capabilities/expression_choice.py:90,107-111` | `_supports_poke` 未接线时默认 `True`（全仓检索仅见此读取点）；提示会声称可拍。`connectors/qq/connector.py:315-345` 的发送路径未处理 `poke_targets`，所以在该渠道会“选了但没发生”。这是 bug。 | 默认 `False`；由当前连接器实例的明确能力位决定是否展示和执行。 |
| `src/ling/kernel/context.py:1330-1346`；`src/ling/domains/deferred/service.py:68-85`；`src/ling/kernel/turn.py:1604-1614` | 任意私聊都会收到延后说明，但白名单只有 `web`/空渠道；外部私聊选择延后后会被拒登记。 | 渲染前按 `channel in SUPPORTED_CHANNELS` 筛选，并在最终解析后保留同一运行时闸门。 |
| `src/ling/capabilities/expression_choice.py:97-106`；`src/ling/kernel/context.py:1297-1310` | 表达说明在私聊不列引用，另一段却无条件教 `[quote:…]`。提示词自相矛盾。 | 引用说明只由一个 `TagSpec` 渲染，条件同解析/投递。 |
| `src/ling/capabilities/expression_choice.py:43,67-78`；`connectors/onebot/message.py:268-272` | 正则把任意 1–40 个非空白、非 `]` 字符当 @/拍一拍参数；`[at:abc]` 会被剥掉并传入 OneBot 的 `at.qq` 字段。 | 分离语法识别与 QQ 号校验、对象白名单；无效值不执行并记录原因。 |
| `src/ling/capabilities/expression_choice.py:43,80-87` | 大小写标签、全角括号、带空格或 `]` 的参数以及超过 40 字符不匹配，原样留在正文；只有标记无正文时又恢复原文，如 `[at:123]`。 | 定义唯一规范语法；对“像标签但不合法”的文本做安全降级和告警，不能无声外发说明书。 |
| `src/ling/capabilities/expression_choice.py:45,67-80` | 重复或超过 `MAX_TARGETS=3` 的标记都会从正文删除，但只保留前三个目标、第一条引用；模型写出的其余选择没有执行结果。 | 显式返回 `ignored_limit`/`ignored_duplicate`，并约束提示词最大个数。 |
| `src/ling/kernel/turn.py:1045-1052,1163-1169,2026-2031` | SSE 流式过滤只剥尖括号标记，方括号表达标签到收尾才解析；若标签出现在流式 delta 中，客户端可先看到它，最终正文才消失。 | 流式出口共用标签目录的完整标记及半截过滤；做逐字符分片验证。 |
| `src/ling/kernel/context.py:1301-1305`；`src/ling/kernel/turn.py:2039-2049` | 引用无效时会去掉引用、正文照发；此处已有日志/事件留痕，但模型说明和用户结果仍需区分“正文送达”和“引用成功”。 | 统一结果语义，用户侧仅在真实回执支持时声明引用成功。 |
| `connectors/onebot/connector.py:258-269,271-290` | 拍一拍发生在正文发送之后；失败只记日志，不改变消息回执。用户可能看到正文已送达却不知道拍一拍没有发生。 | 给拍一拍单独的动作结果，不能借正文送达回执宣称拍一拍成功。 |
| `src/ling/kernel/context.py:1268-1281`；`src/ling/kernel/turn.py:2031-2049`；`src/ling/capabilities/delivery.py:125-147` | 提示只按群私、拍一拍粗筛；最终仅校验引用 ID，投递闸也未校验 @/拍一拍是否为本渠道能力。 | 同一能力快照驱动告知和执行前校验，未知能力一律不宣告。 |

**代码事实：上述边界用本地 `parse_expression()` 复现过。** `[at:abc] 你好` → @ 参数 `abc` 且正文只剩“你好”；`[AT:123] 你好`、`[at:12 34] 你好`、`[quote:消息 id] 你好` → 标签保留；`[at:123]` → 原文保留且无指令。对应实现：`src/ling/capabilities/expression_choice.py:43,61-87`。

## 4. 优先级建议

**设计判断（第 5 节修订）：实施时应把“严格解析＋本轮目标校验＋流式出口”与“本轮渠道能力快照＋默认关闭的可用性闸”列为同一优先级。** 旧版只把能力快照放在第一位，低估了现有宽正则可能让 `[at:abc]` 进入动作列表、让正文举例变成真实动作的风险。快照仍负责止住 `supports_poke=True` 和“所有私聊都可延后”的误告知；语法与目标校验负责止住误执行。依据：`src/ling/capabilities/expression_choice.py:43,67-87`、`src/ling/kernel/context.py:1271-1275,1330-1346`、`src/ling/domains/deferred/service.py:68-85`。

## 建议的实施顺序

1. **先定真值。** 给当前连接器实例增加逐项能力声明；未知为不可用，修正拍一拍默认值和延后渠道条件。复用同一快照生成环境事实。验收：QQ 官方/qqbot/微信不再收到 OneBot 专属拍一拍说明，非 Web 私聊不再收到延后说明。依据：`src/ling/kernel/context.py:1271-1275,1330-1346`、`connectors/onebot/connector.py:258-269`、`src/ling/domains/deferred/service.py:68-85`。
2. **收敛协议。** 建 `TagSpec` 目录并迁入四组动作的语法、参数、可用性与说明；输入侧 `[msg_id:…]`/`[recall:…]` 只登记为参数来源。保留领域解析器实现，但由目录调用；删除重复引用说明。验收：每条展示的语法都有解析器，每个解析器能识别的模型动作都有条件说明。依据：`src/ling/capabilities/expression_choice.py:43-116`、`src/ling/kernel/context.py:915-969,1297-1310`。
3. **统一出口与回执。** 解析后再按能力和目标校验，流式与最终外发都只清理未受字面量保护的控制候选与已知半截尾巴；为 ignored、unsupported、uncertain、succeeded 建可观测结果。对 `[at:abc]`、非法 quote、只写标记、跨分片标记和多标签组合做针对性回归。依据：`src/ling/capabilities/expression_choice.py:43,67-87`、`src/ling/kernel/turn.py:1045-1052,2026-2049`、`connectors/onebot/connector.py:258-269`。

## 5. 标签语法与抗误触发

### 5.1 调研事实与适用边界

**本仓库事实。** `expression_choice.py` 用 `\[(at|quote|poke)\s*:\s*([^\]\s]{1,40})\]` 在全文搜索；`_take()` 对 `at`/`poke` 只做去重和最多三个的限制，对 `quote` 只取第一条，**无参数形状校验**，而且不论是否超限都会返回空串，因而候选标记均从正文消失。仅在清洗后整个正文为空时才恢复原文并清空指令；所以单独 `[poke:123]` 目前不会拍，`[at:abc] 你好` 却会把 `abc` 当目标。空格合并与逐行 `strip()` 还会改动原始排版。`src/ling/capabilities/expression_choice.py:43-45,61-87`。引用虽在收尾时核对本轮 `replyable_ids`，@ 和拍一拍尚无同等目标校验；方括号标记也未进入 `_visible()` 的流式过滤。`src/ling/kernel/turn.py:1017-1052,2026-2049`。

**其他项目做法 A（完整文件）。** `conversation_flow/core/silence_judge.py` 的默认沉默标记是 `<SILENCE/>`；返回值明确分 `matched`、`variant`、`no_match`，都带 `reason`。配置标记只能处在回复开头的“控制位”；前缀容许空白、有限的 Markdown 围栏和前导符号，普通正文里引用标记不会触发。默认标记才容忍 `SILENT`、`[SILENCE]`、大小写、HTML 转义等变体，自定义标记不走这条宽容路径。尾部有正文时，超过 32 字、含换行，或不是“好像／maybe”一类短解释，就判 `no_match`；`SilenceJudge` 还负责策略选择、白名单、提示注入和预判断。这是**整轮沉默决策**，不能直接套到 ling 的行内 @。`/Users/lingxi/project/astrbot_plugin_conversation_flow/core/silence_judge.py:22-120,123-241`。

**其他项目做法 B。** `voice_hub` 用 `^\s*(?:\[CQ:[^\]]+\]|\[[A-Z][A-Z0-9_-]{0,24}\])\s*$` 识别**整行**控制文本，清洗 TTS 时只剔除这样的行；它同时先移除代码块、行内代码、URL、HTML 标签，最后按是否保留换行清理空白。这是朗读前的清洗规则，不是模型动作授权规则；其“全大写、整行独占”可借鉴为降低误删的策略，不能推导为 ling 必须整行。`/Users/lingxi/project/astrbot_plugin_voice_hub/core/text_processing.py:6-10,19-42`。

**其他项目做法 C（只读抽查）。** `companion_phone` 的 `/phone` 是 AstrBot 命令组并有管理员权限门，不是从模型回复中挖行内标签。`/Users/lingxi/project/astrbot_plugin_companion_phone/main.py:118-175`。`embodiment_bridge` 既有针对**入站用户明确动作请求**的 `matched/rejected/ambiguous/not_explicit` 解析和引用／否定／假设语气排除，也会仅向带可信事件标记、确有支持动作的请求注入带参数 schema 的 `FunctionTool`；没有发现同 ling 一样从模型正文解析 `[at:…]` 的做法。`/Users/lingxi/project/astrbot_plugin_embodiment_bridge/core/explicit_action_parser.py:9-31,94-155`、`/Users/lingxi/project/astrbot_plugin_embodiment_bridge/core/avatar_action_tool.py:1-5,41-57,148-172,209-230,303-341`。`orchestration_hub` 的 `tags` 是服务发现的集合筛选，`_RANGE_RE` 是版本范围校验，不是模型输出指令；所查路径未见同类正文标签解析。`/Users/lingxi/project/astrbot_plugin_orchestration_hub/core/resolver.py:10-32,68-81`。

**业界通识（官方文档）。** OpenAI function calling 将工具名与 JSON 参数放进独立 tool-call 结构，工具定义有 JSON Schema，`strict` 模式可约束生成参数满足 schema；Anthropic tool use 也用 `tools` 的 `input_schema` 和独立 `tool_use` 内容块。因而普通正文里出现 `<tool_use>` 或 `[at:123]` 不会仅凭文字就自动变为 API 工具调用。这里的优势是**协议通道分离＋schema 校验**，不是 XML 尖括号本身有魔法。官方出处：[OpenAI Function calling](https://platform.openai.com/docs/guides/function-calling)、[Anthropic Implement tool use](https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use)。**我的推断：** `<ling:at target="123"/>` 比短 `[at:123]` 更不容易偶然出现在聊天中，但二者若仍由正文正则搜索，引用、代码块和参数幻觉都仍需另行处理；不能把“换成 XML”当成安全修复。

### 5.2 位置、字面量与正文边界

**我的判断：`at`/`quote` 保留行内，`poke` 允许行内或单独成行；不要把三者一律改成整行。** `[at:123] 你说得对`、`好的[at:123]，我看到了` 能自然指向当前话语对象；整行控制符迫使模型把动作和语句拆成两行，既改变说话节奏，也容易在分段发送时把动作与正文拆散。`poke` 是独立动作，可单独成行但不强制。与之相反，延后是“本轮不回复”，必须整条回复只有标记；撤回涉及不可逆平台动作，建议只在独占控制行提出并继续走确认门。现有延后已要求整条只写标记；撤回目前仍在全文搜索，因此将撤回收紧为独占行是**协议变更建议**，需同步提示词与回归。`src/ling/domains/deferred/markers.py:31-38,139-181`、`src/ling/domains/conversation/recall.py:537-555,586-600`、`src/ling/kernel/turn.py:1844-1902`。

**我的判断：不为 `at`/`quote` 引入 conversation_flow 式“只认句首／行首控制位”。** 它会误拒合法的「好的[at:123]，我看到了」，还会鼓励模型为附带动作人为换行。位置约束只能降低部分误触发，无法识别行首的「`[at:123]` 是什么意思」。较合适的组合是**严格参数形状、本轮目标白名单、字面量屏蔽、出口暂扣和留痕**。最大的剩余风险是**未转义、未加代码标记的自然句子“你可以用 [at:123] 这个标记”与真实行内指令在字符层面完全相同**；任何单条正则都无法无损地区分两者。高风险动作必须转结构化工具通道或更强显式控制格式。`src/ling/capabilities/expression_choice.py:43,61-87`；此处风险判断为我的推断。

**建议的字面量协议。** 只认 ASCII 小写 `at/quote/poke`、半角 `[]`、半角冒号，标签内不允许空白；不启用 `IGNORECASE`，不做 NFKC／全角归一化。要**说出**标记时用 Markdown 行内代码／围栏（例：`` `[at:123]` ``）或反斜杠 `\[at:123]`；解析前先识别并保护这些字面量区间，转义时仅去掉用于转义的反斜杠。不要用「包含『标记』二字」之类的语义正则猜测意图。`[AT:123]`、`［at:123］` 不执行；前者若是未保护的 ASCII 方括号候选，可清洗并记 `invalid_case`，后者保留原文、只记低优先级变体统计。普通 `[1]`、`[链接](url)`、`[CQ:at,qq=123]` 不进入动作候选。此段全部是**设计建议**，不是当前行为。

**剥离只作用于确认的标记 span，不能再全文逐行 `strip()`。** 建议先保护代码／转义区间，再扫描候选，完成语法与本轮白名单校验，最后只删除决定作为控制语法的原 span；局部把删除处两侧的水平空白缩成一个，保留原换行、标点和其他空白。例如 `好的[at:123]，我看到了` → `好的，我看到了`；`[at:123] 你说得对` → `你说得对`；被保护的 `` `[at:123]` `` 原样发出。删除若导致正文为空，**不要回退发送原始标记**：`poke` 可作为纯动作请求执行而不发送空消息；单独的 `at/quote` 无可附着的文字时不执行、不发空消息，记 `empty_body`。这修正了现有“清洗后空就恢复原文”的行为。`src/ling/capabilities/expression_choice.py:80-87`；其余为设计建议。

### 5.3 参数语法、坏格式与候选处置

以下是**目标 Python 正则，不是现行实现**。实际扫描前先遮罩字面量区间；`search/finditer` 用于行内规则，`fullmatch` 用于整条规则。`[0-9]` 明确限定 ASCII 数字，避免 Python `\d` 接受其他 Unicode 数字。括号和关键字大小写都按字面匹配。

```python
AT_RE = re.compile(r"\[at:(?P<qq>[1-9][0-9]{0,19})\]")
POKE_RE = re.compile(r"\[poke:(?P<qq>[1-9][0-9]{0,19})\]")
QUOTE_RE = re.compile(r"\[quote:(?P<id>(?:[0-9]{1,20}|m[a-z2-7]{6}))\]")
DEFER_RE = re.compile(
    r"\A[ \t]*<<[ \t]*等会回[ \t]+(?P<duration>"
    r"(?:[0-9]+(?:\.[0-9]+)?[ \t]*(?:秒钟|秒|分钟|分)?|"
    r"[0-9]+[ \t]*(?:分钟|分)[ \t]*[0-9]+[ \t]*(?:秒钟|秒))"
    r")[ \t]*>>[ \t]*\Z"
)
RECALL_RE = re.compile(
    r"\A[ \t]*<<[ \t]*撤回(?:[ \t]+(?P<ref>r(?:[0-9]{1,20}|[a-z0-9]{5,6})))?"
    r"[ \t]*>>[ \t]*\Z"
)
# 只用于发现并记录“像动作但不合法”的 ASCII 候选；绝不能据此执行。
EXPRESSION_CANDIDATE_RE = re.compile(
    r"\[(?P<kind>at|quote|poke)[ \t]*:[^\]\n]{0,80}\]", re.IGNORECASE
)
```

**参数约束的理由（我的判断）。** `at/poke` 的 1–20 位非零起始十进制串只完成形状校验，不证明它是 QQ 成员；必须再查本轮 `allowed_targets`、渠道能力和实际发送前状态。`quote` 接收 1–20 位数字短 ID 或 `m` 加 6 位小写 base32 别名，随后必须在本轮 `reply_ids` 中查到原始平台 ID；现行 `message_id_token()` 对**所有**不超过 20 字的原始 ID 原样保留，因此若存在短的非数字平台 ID，这条新正则会拒绝它。实施前应改为“数字短号可原样，其他平台 ID 一律生成 `m…` 别名”并处理别名碰撞，否则改用本轮 ID 集合动态编译 `re.escape` 白名单，而不能声称静态式已经兼容。`src/ling/kernel/context.py:363-374,401-412,1688-1731`、`src/ling/kernel/turn.py:2032-2049`。`recall` 的 `r…` 同样要经本轮 `recallable_refs` 解析、归属／时窗／频次检查和用户确认；无参形式只在存在明确“最近一条”时成立。`r` 后允许 5–6 位小写字母数字是兼容既有文档示例 `r3f2a1` 的过渡范围（示例含 `1`，并非严格 base32），真正的 `m` 加 6 位 base32 哈希 token 应得到 6 位；必须以本轮短号白名单而不是位数决定是否能提议。当前短号从消息 token 的 `m` 改成 `r`，宽松现行正则会接收任何 1–32 位字母数字下划线／连字符，收紧前须先收敛 token 生成。`src/ling/domains/conversation/recall.py:550-600`、`src/ling/kernel/turn.py:1852-1902`。`defer` 只接受整条回复、明确时长；正则约束字符组合后仍须调用 `parse_duration_seconds()`，要求 `> 0`，按既有 300 秒上限钳制并记录原始秒数与 `clamped`，不能把 `999` 悄悄变成她主动选了 300。现行解析允许无空格、正文并存时剥标记并报 `body_present`；上述“动词后须空格＋整条独占”是更严的目标语法，迁移时需让提示词、解析、流式与最终出口同步。`src/ling/domains/deferred/markers.py:63-81,106-181,191-225`。

**坏格式处置（我的判断）。** “静默忽略但保留原文”会把 `[at:abc]` 或半截 `[quote:` 作为说明书发给用户；“直接报错”会为了一个附属动作挡住整条正常回复；“无论是否有效都剥掉”又会吞掉原本要讨论的字面内容。建议区分**是否进入控制候选**：受保护的代码／转义、普通方括号、全角符号一律作为正文；未保护、ASCII 方括号且名称为已知动作的候选（含 `[AT:123]`、`[at:abc]`）不执行，剥其 span、保留其余正文并记录原因；未闭合的已知前缀只在**最终输出末尾**清掉，非末尾原样保留并记 `incomplete_marker`。如果正文只剩坏候选，不发空消息，也不把原标记补回。重复或超限的合法标记也不执行多余动作，剥掉控制 span 并记 `duplicate`／`limit_exceeded`。这些规则需同样用于落库、外发和 SSE，且最终动作失败不能被“正文已送达”掩盖。现行行为参见 `src/ling/capabilities/expression_choice.py:67-87`、`src/ling/kernel/turn.py:2312-2328`。

### 5.4 流式、留痕与反例

**流式设计建议。** 延续现有 `(ready, hold)` 接口，但统一由标签目录生成已知前缀的状态机：出现 `[` 时暂扣**一个**字符；继续成为 `[a`、`[at:`、`[quo` 等已知前缀才继续暂扣；一旦变成 `[1]`、`[链接]`、`[CQ:` 或不可能的前缀，立刻**原样释放**，不能让普通正文丢 `[`。闭合后先按与最终解析相同的字面量、语法、白名单和清洗规则决定可见文本；分片跨越代码围栏／反斜杠时也要保留遮罩状态。末尾仍是 `[quote:` 这类已知半截前缀时不送 SSE、不落库；末尾只有 `[` 且流结束，原样放出，因为还不能证明是动作。常态文本不缓存。已有 `stream_safe_deferred()` 用“只扣可能的前缀”处理 `<<等会回`，`marker_stream` 曾因宽松半截规则把 `1<<2` 误删；`turn._visible()` 明确要求各过滤器对同一份原文并行取更保守暂扣，而不能串行组合。方括号过滤器接入时也必须遵守这个不变量，并验证 `delivered`、SSE、最终落库一致。`src/ling/domains/deferred/markers.py:191-220`、`src/ling/kernel/marker_stream.py:41-90`、`src/ling/kernel/turn.py:1000-1052`。

**留痕建议。** 每个候选给出 `matched`／`invalid_syntax`／`invalid_target`／`unavailable`／`duplicate`／`limit_exceeded`／`escaped_literal`／`incomplete_marker` 等 reason，以及动作名、所在位置、是否从正文剥离、是否执行、后续平台回执；放进行为 meta 并按原因聚合计数，同时打可检索日志。正常 `[1]`、Markdown 链接、CQ 码无需逐个记日志。不要把完整正文、QQ 号、平台消息 ID 原样写日志；目标可截断或哈希，保留 turn_id、能力 key 和原因即可。超预算／超时长同样记录“原请求与实际应用”的差异。现有 quote 失败已有 `quote_dropped` 事件字段与日志，defer 已有 `requested_seconds`／`clamped`；建议扩展而不是另起无关协议。`src/ling/kernel/turn.py:2039-2049,2317-2326`、`src/ling/domains/deferred/markers.py:84-103,169-181`。

下表是**目标行为**；“匹配”指经字面量遮罩与正则后成为动作候选，“执行”还要求本轮白名单、能力和正文条件满足。示例中的 `123/456` 假定属于本轮允许目标，供语法回归使用。

| 输入 | 目标匹配／执行 | 原因 |
|---|---|---|
| ``你可以用 `[at:123]` 这个标记`` | 不匹配／不执行 | 行内代码是字面量，完整保留。 |
| `[at:123] 你说得对` | 匹配／执行 @ | 合法句首行内动作，剥后正文仍完整。 |
| `好的[at:123]，我看到了` | 匹配／执行 @ | 合法句中动作，不能被句首规则误拒。 |
| `[at:abc] 你好` | 候选但不合法／不执行 | 非数字，剥候选、正文仍为“你好”、记 `invalid_syntax`。 |
| `[AT:123] 你好` | 候选但不合法／不执行 | 固定小写；记 `invalid_case`。 |
| `［at:123］ 你好` | 不匹配／不执行 | 不自动全角归一化；原样保留。 |
| `[1] 参考文献` | 不匹配／不执行 | 普通引用，`[` 不应丢失。 |
| `[链接](url)` | 不匹配／不执行 | Markdown 链接不是已知动作名。 |
| `[CQ:at,qq=123]` | 不匹配／不执行 | CQ 码归连接器协议。 |
| `[quote:`（流结束） | 不匹配／不执行 | 已知半截前缀，末尾清除并记 `incomplete_marker`。 |
| `[poke:123][poke:456]` | 两个都匹配／可执行两次 | 各自通过白名单且未超 `MAX_TARGETS=3`；无正文时走纯动作路径，不发空消息。 |
| `\[at:123]` | 不匹配／不执行 | 反斜杠转义；发出的文字为 `[at:123]`。 |
| `[quote:3] 你好`（本轮无 id `3`） | 形状匹配／不执行引用 | ID 幻觉；去掉引用候选、正文照发、记 `invalid_target`。 |
| `<<等会回 90>> 你好` | 不作为延后执行 | 延后须整条独占；正文照发、记 `body_present`，不静默吞回复。 |
| `<<撤回 r3f2a1>>` | 形状匹配／仅建待确认提议 | 仍须本轮短号、归属和确认门；不能直接撤回。 |
| `1<<2 就是 4` | 不匹配／不执行 | 普通左移表达式不应被半截过滤吞掉。 |

### 5.5 与两个注册表的衔接

**我的建议。** 上述合法式、候选式、参数解析、`placement`、字面量／坏格式策略、流式前缀、次数上限及动态目标校验，都放入第 2 节 `TagSpec` 目录；提示词示例从同一目录的 `syntax/model_help` 渲染，不能另写一套“允许全角／可在句中／可只写标记”的矛盾说明。每轮可用性快照只挑真正可执行的规格；解析与最终执行仍二次核验。`PROMPT-ZONE-SPEC.md` 的 `capabilities` 区应**引用这份动作协议作为规范来源**，并要求注入“参数只用本轮展示值、字面量用代码标记或转义、defer 独占整条、recall 独占控制行且需确认”的紧凑约束；该分区规范只负责注入位置、预算和可信来源，不复制正则、候选清洗或目标校验逻辑。当前分区确实将 `capabilities` 定为可执行能力说明区，且明确它不是原生 tool schema。`docs/design/PROMPT-ZONE-SPEC.md:19,25-37,145-147,314-325`、`src/ling/capabilities/expression_choice.py:90-116`。
4. **再评估结构化动作。** 以目录导出 JSON Schema / 原生 tools 适配，先用于撤回等高风险动作；保持旧标签兼容，并对比格式错误率、动作成功率与提示词成本。依据：`src/ling/ports/model.py:273-290`、`src/ling/capabilities/tools.py:38-50,113-162`。
