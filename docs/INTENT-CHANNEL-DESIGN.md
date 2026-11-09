# 行动意图载体：撤回、具身与双工的分界

> 状态：设计复核与前瞻；本文的延迟数字是后续验收目标，不是现有系统的实测指标。
> 术语：M = 回复正文内标记；T = 模型的结构化 tool call；控制通道 = 与正文/音频分离的会话事件流。

## §1 复核结论

**裁决：撤回继续用 M；M 不升级为所有动作的通用载体。**
当前 `ModelChunk` 只有 `delta/done/finish_reason`，`ModelRequest` 没有 `tools`，`ChatMessageIn` 只约定 system/user/assistant；三种在线适配器只提取文本增量，没有把工具事件交给内核（`src/ling/ports/model.py:256-290,326-332`；`src/ling/models/openai_compatible.py:130-137`；`src/ling/models/chat_responses.py:135-150`；`src/ling/models/anthropic.py:142-162`）。
撤回针对过去已送达的消息，能在回复结束后落提议；T 的同轮中途调用优势在此没有抵消端口与适配器改造成本。
但“可事后补救”只说**可以事后发起纠错**，不表示已撤回的消息可以恢复；真正 `delete_msg` 仍是不可逆动作（`src/ling/domains/conversation/recall.py:1-35,410-426`）。
五份盲测的 3:2 是输入背景，不替代下面的代码与时间约束复核。
决策的有效期由动作的时间性质决定，不由“模型会不会调用工具”这一表面形式决定。
对撤回，模型可以先完成这句话，再产生一条待确认提议。
对流中的动作，等一句话完结本身就是错误的时序。

**当前 M 的安全边界并不等于流程已闭环。**
`parse_recall_markers` 从同一段正文提取短号并剥离，无法证明标记是她的操作意图，而非引用、复述或提示注入；记录 `provenance=body_marker` 只能说明技术来源，不能证明“她想撤”（`src/ling/domains/conversation/recall.py:555,586-600`）。
`_collect_recall_proposals` 短号对不上时返回空；若正文同时说“我撤回了”，会形成静默谎报；表达层必须只说“想撤回，等你确认”，并在无提议时禁止成功措辞（`src/ling/kernel/turn.py:1417-1451`）。
`turn.completed` 已携 `recall_proposals`，但前端 `TurnFinalData` 未声明该字段、`applyFinal` 未消费它；现状尚无可用的网页确认提示闭环（`src/ling/kernel/turn.py:1798-1803`；`clients/web-console/src/types.ts:664-682`；`clients/web-console/src/useChat.ts:373-442`）。
另一个更关键的复核结果：`manage.call_tool` 会先跑 `PolicyGate`，撤回确认路由却直接 `registry.call(..., confirmed=True)`；注册表确认闸与撤回五道闸存在，**统一 PolicyGate 并未覆盖该确认入口**（`src/ling/api/routes/manage.py:1507-1559`；`src/ling/api/routes/recall.py:377-395`）。
因此“安全由执行层决定”仍对，但不能据此声称当前每个执行入口已有同一套策略闸。
撤回五道闸包括归属、真实送达回执、平台时间窗、频次和对象可撤性；执行前必须重新取事实（`src/ling/domains/conversation/recall.py:273-381`）。
当前 `resolve_recall_ref` 只做精确短号匹配，匹配失败返回 `None`；它能防止猜错对象，不能防止“说了已撤却没提议”的语义错误（`src/ling/domains/conversation/recall.py:787-803`）。

## §2 动作谱系与延迟预算

延迟从**产生可判定意图**到**设备/平台收到动作命令**计；模型思考、网络及设备耗时应分别观测，下面是设计预算而非保证。

| 时间性质 | 典型动作 | 可接受的决策/下发预算 | 适合载体与确认 |
|---|---|---|---|
| ① 非交错、事后纠错 | 撤回她自己以前发的消息；看一眼已有视频证据 | 提议可在回复结束后数秒产生；人工确认可等数十秒，但不得越过平台窗口 | M 可用；逐次确认；窗口届满即拒绝 |
| ② 非交错、结果不可补救 | 结束一通电话、发出整条语音消息、对外发布内容 | 意图确定后约 0.5–3 秒；若必须确认，以确认完成时重新计时 | 结构化事件优先；确认或严格的会话预授权 |
| ③ 交错、实时 | 点头、眨眼、看向用户、边说边动、停止旧轮音频 | 客户端本地反射/打断约 50–150 ms；模型驱动动作约 150–500 ms | 独立控制事件；低风险按会话授权，不逐次弹窗 |
| ④ 长时连续流 | 通话的双向音频帧、VAD、插话、持续播报、静音/恢复 | 音频帧约 20–40 ms；插话停播目标 <200 ms；新一轮首个可听输出目标 <1 秒 | 媒体流 + 控制通道；不能按“完整回复”提交 |

② 的“挂断”不可撤销的是**这一通会话**；重新拨号是新会话，不能当作 undo。
③/④ 的数值是目标上限，需在实际设备与网络上测 p95 再调；无法满足时先降低动作频率，不能放松身份或回执判据。
人工确认可能花几十秒，所以它只适合允许等待的动作；撤回还受本地 120 秒预检窗口约束（`src/ling/domains/conversation/recall.py:71-79,325-332`）。
同为“结束后可处理”，看视频是读证据，撤回是不可逆外部动作；时间类别不能替代风险类别。
语音消息一旦送达也不可收回其已被收听的事实，即使平台提供删除功能。
`embodiment.py` 已列出 `nod/shake_head/tilt_head/smile/blink/look_at_user/wave/idle_breath` 和 `phone_call` 在场类型（`src/ling/capabilities/embodiment.py:22-51`）。
注意 `phone/service.py` 是“看一眼手机”的分钟级状态机，不是电话呼叫栈；`start_watching/stop_watching/schedule_next` 不能被当成拨号、挂断能力（`src/ling/domains/phone/service.py:1-16,210-271`）。

## §3 双工/实时场景对载体的硬要求

**完整生成后再解析 M，在双工中失效。**
用户可能在她说到一半时插话；若等文本完成才看到 `<<点头>>` 或 `<<挂断>>`，动作与音频已错位，已播出的声音也无法收回。
目前 `_visible` 只把 `chunk.delta` 暂扣、剥离后发给文字前端，`_finalize` 才取撤回短号；`_follow_up_after_watch` 也是完成首次生成后的同轮二次调用，均非实时控制环（`src/ling/kernel/turn.py:904-933,1037-1053,1391-1451,1492-1510`）。
`marker_stream.py` 为两类标记共用半截暂扣；新增类别会改变 `PENDING_MARKER_RE` 与每个出口的保留字节，不能把它当成零成本通道（`src/ling/kernel/marker_stream.py:1-64`；`src/ling/domains/media/video.py:77`）。

实时需要“边产生、边校验、边调度”的**事件**：带 `session_id/turn_id/sequence/deadline` 的动作事件，服务端策略裁决后发给设备，设备回 `ActionResult`；插话由输入侧控制事件取消旧轮，过期事件直接丢弃。
流式 T 可承载离散的模型动作意图，但只收到完整 `name + args` 后才可执行；分片参数不能先执行后校验。
带内信号也能被增量解析，**并非技术上绝无可能**；它仍与字幕/朗读正文同源，遇到引用、跨 chunk、重试与取消时无法可靠表达控制和 provenance，不作为双工主方案。
长时全双工音频本身更不能变成连续 `tool_call`：媒体帧走媒体流，`start/stop/interrupt/mute/avatar` 走有序控制事件；网络实现可以选择 WebSocket 或 WebRTC 数据通道，协议语义与底层传输解耦。
`SpeechService` 的规则已要求 ASR partial 只用于字幕/诊断、final 才进入认知，TTS 需要实际播放回执，插话取消旧轮且丢迟到音频；这正是控制事件必须带轮次与序号的依据（`src/ling/domains/speech/service.py:1-7,48-74,115-130,149-166`）。
`EmbodimentGateway.begin_turn/interrupt_turn/accept_audio` 已有取消旧轮与迟到片段拒绝的雏形；`avatar_intent` 限白名单和 10 秒有效期，`record_action_result/record_playback` 分别承认动作和播放事实（`src/ling/capabilities/embodiment.py:215-241,265-309`）。
打断应由客户端或服务端的确定性控制逻辑立刻发出，不能等待模型先生成“我要打断”的文本。
点头等姿态可由模型提出，但设备只能接收经过白名单、授权和时效裁决的事件。
连续音频帧也不应每帧调用一次 `PolicyGate`：会话授权和流控制需分层，权限变更即时切断后续帧。
回复文本、字幕、TTS 与动作事件应共享轮次标识，却保有各自的成功回执。
结论是 **M 只适合非交错、可等收尾的动作**；双工上线时，离散模型动作迁 T 或等价结构化事件，设备/媒体控制走独立通道，不能把“换成 T”误写成双工的全部方案。

## §4 载体无关的动作协议层

目标链：`M 解析器 / T 适配器 / 会话控制源 → ActionIntent → PolicyGate → 授权/确认 → 执行 → 回执/审计`。
两种模型载体只负责**提出**同一语义对象；目标解析、身份、预算、幂等与回执均由服务端执行链拥有。
建议逻辑结构（字段名属于设计提案，**当前代码没有这个完整类型**）：

```text
ActionIntent {
  schema_version, tool_id, args, call_id,
  provenance: {channel, provider_id?, model_id?, turn_id, source_ref?, span?, trust},
  subject_id, window_id?, session_id?, turn_id?, sequence?,
  created_at, expires_at?, target_ref?, idempotency_key?
}
```

`tool_id` 必须命中一张 `ACTIONS`/`ToolRegistry` 白名单；`args` 按 `ToolSpec.input_schema` 验证，不能让模型传 `session`、`connector`、授权布尔值或平台 message id 等服务端对象。
`call_id` 由 T 的原生 call id 映射，M 则由服务端签发；同一候选重试须保留同一 id，另以 `(tool_id, target, session)` 等域键查重。
现有 `ToolRegistry.call` 每次新建 `call_id`，若要跨重试审计/去重，需让调度层先持有稳定 id 再传入执行层（`src/ling/capabilities/tools.py:113-125`）。
`provenance.channel` 至少区分 `body_marker`、`structured_tool_call`、`session_control`、`user_command`；`source_ref/span` 指向原始模型事件或正文偏移，原文只短期留痕、日志避免复制敏感正文。
`trust` 是来源可信度而非“模型意愿证明”：M 标记只能记 `untrusted_text`；T 记 `model_structured`，仍可能受提示注入；用户命令需独立鉴权，不能由模型自称。
消费端必须核对事件确属本次模型输出，不能解析用户消息、工具结果或被引用的历史文本；M 的引号/代码块误判无法单凭标记完全排除，高风险动作继续要求确认。
`created_at/expires_at/sequence` 用于拒绝迟到动作；参数还需与服务端重取的最新窗口/会话事实核对，不能信模型提供的旧在场信息。
同一 `call_id` 不能被两个不同的 `tool_id/args` 复用；参数变了就是新候选，原候选须终止或拒绝。
`source_ref/span` 是诊断用溯源位置，不是授权凭据；即使引用了可信 prompt 快照，也不提升 M 的可信度。
从 T 收到的参数若跨多个 chunk，调度器应先完成组装与 schema 校验，再生成一次内部意图。
对执行超时应保留 `uncertain`，不能因网络重试自动生成第二个不可逆外部动作。
执行结果至少记录 `call_id、proposal_id?、授权依据、策略版本、实际回执、失败/不确定状态`；成功只能由平台或设备回执证明，不能由模型文案推断。

仓库**已经**有 `contracts/v1.ActionIntent(intent_id, kind, target_ref, payload, requires_confirmation, reversible)`，供 `PolicyGate` 判动作类别，但没有 `tool_id/args/call_id/provenance`（`src/ling/contracts/v1.py:646-658`；`src/ling/kernel/policy.py:86-140`）。
实施时应先以内部 `ResolvedActionIntent` 表达上面的协议，再投影成现有 v1 `ActionIntent(kind="tool.<risk>")` 给 `PolicyGate`；若以后公开该协议，另起契约版本，避免把两个同名概念混用。
`ToolSpec` 的 `risk/reversible/timeout_seconds/idempotent/input_schema` 提供动作元数据，`ToolOutcome` 提供执行结果分类；它们不负责证明用户确认，更不负责解析 M/T（`src/ling/capabilities/tools.py:23-62`）。
所谓“迁载体时执行层零改动”是**完成此收敛后的目标**：现在撤回路由、`manage.call_tool`、`ToolRegistry` 并未汇到同一调度器，不能把目标描述为现状。
迁移时替换的是“模型事件 → 内部意图”的入口；`PolicyGate`、确认记录、handler 和回执存储不因 M/T 改变。
用户手动发起的同名动作也要通过同一调度器，只是 `provenance.channel=user_command`，授权来源不同。

## §5 风险分级授权模型

风险描述“动作后果”，确认模式描述“谁在何时授权”，两者不能压成 `requires_confirmation: bool` 一个开关。

| 级别与例子 | 建议授权 | 与当前 `ToolRisk` 的关系 |
|---|---|---|
| A：读时间、眨眼/点头/轻微看向用户 | 会话有效且白名单内免逐次确认；限制强度、频率、有效期 | 读取为 `read_only`；可停止的本地 avatar 动作为 `reversible`，但仍查在场与设备能力 |
| B：启用麦克风/相机/位置/空间感知；允许短时实时语音 | 用户在设备上逐项、限时、可撤销地授权一次；每次动作复核 grant | 感知是**能力授权**而非 `ToolRisk.read_only`；语音外发另计动作风险 |
| C：撤回、发整条语音消息、主动挂断、公开发布 | 默认逐次确认；仅明确限定的通话会话可按 §6 预授权 | `irreversible`；对公众外发为 `external_publish`，两类都需更严闸 |

`embodiment.py` 的感知能力默认不授予，`grant_perception` 有独立 TTL，设备离线/会话结束撤销授权；这些约束要保留（`src/ling/capabilities/embodiment.py:34-35,92-113,129-193`）。
`PolicyGate` 已把 `avatar.action` 登记为无需额外 scope，把 `tool.irreversible` 映射到 `device` scope 且强制确认；这不足以表达“仅本设备本会话的点头可免确认”和“电话里的有限预授权”（`src/ling/kernel/policy.py:54-80`）。
当前 `ToolRegistry.register` 对 `irreversible/external_publish` 默认拒注册，获准注册仍强制 `requires_confirmation=True`；`call(confirmed=False)` 在 handler 前返回 `needs_confirmation`（`src/ling/capabilities/tools.py:68-106,113-137`）。
因此 B/C 的会话授权**不能**假装成 `confirmed=True` 复用现有布尔闸；未来应增加服务端可验证的 `authorization_mode=none/session/per_call` 与 grant 引用，由 PolicyGate 独立决定，注册表只接受经策略签发的执行许可。
点头频率高但不等于无边界：离线、过期、旧轮、无设备实现或超强度时拒绝；设备未回 `ActionResult.executed` 就不能说“我点头了”（`src/ling/capabilities/embodiment.py:265-295`）。
`look_at_user` 若只是把虚拟角色朝固定屏幕方向转动，可按 A 类；若要靠相机定位真人，须先有 B 类 `camera` 授权。
“说话”也按外发事实拆分：未播放的生成片段可取消，已播放的部分不可撤销；回执以 `played_ms/completed` 记录（`src/ling/domains/speech/service.py:149-166`）。

## §6 双工下的确认降级

通话开始前，在**本机可信 UI**逐项展示可自动执行的动作、对象、有效期和撤销入口；例如“允许本通电话在用户明确说再见或连续静默 30 秒时自动挂断”，只对当前 `session_id` 生效，通话结束即失效。
对外发语音可预授“仅当前联系人、最长 10 秒/条、最多 3 条、总计 30 秒、仅本通会话”；超出任一额度即停播/拒发并请求逐次确认。额度只是示例，应由产品验收确定。
通话中的逐次确认可改为语音挑战：先播报具体动作与对象，等待当前 owner 的 ASR **final** 明确肯定，限定 5–10 秒与当前 `turn_id`，模糊、无回复、插话或过期均不执行；partial 不授权。
用户自己说“挂断”属于已鉴权的用户命令，可在确认其语义后立即执行；模型主动想挂断若没有覆盖该条件的预授权，必须等语音确认，不能靠模型复述“你同意了”。
紧急失联/设备断开可按系统连接超时结束媒体链路，但须标为**系统故障终止**，不可审计成“她主动挂断”。

风险是 ASR 误识、旁人代答、播放回声被识成同意、授权跨会话复用、额度竞态和确认期间动作过时。
控制手段是设备/说话人绑定、回声隔离、ASR final、一次性 challenge、服务器原子扣额、短 TTL、用户可随时撤销、动作前再验在场；无法核验时失败关闭。
“事后撤销”只适用于仍持续的权限/状态（停止后续麦克风采集、撤销 grant、取消未发音频）；已挂断或已经播出的声音不能撤销，只能给事实回执并提供补救动作。
预授权不是一张永久的“同意挂断”通行证：会话、对象、条件、额度和有效期任一不符都退回逐次确认。
额度扣减必须与动作领取在服务端同一原子边界完成，两个并发模型事件不能各自看到“还有一次”。
如果确认挑战播放后用户插话，旧 challenge 随旧 `turn_id` 失效；下一轮不能沿用它的“是”。
`FallbackNotice` 的“用了默认值”也不是动作成功证据：场景过期时只能用 `unknown`，不可把旧场景写成她仍在某处（`src/ling/contracts/v1.py:932-952`；`src/ling/domains/presence/service.py:453-509`；`src/ling/domains/fallback.py:158-197`）。

## §7 现在该做的最小预留

本节是**后续实施顺序**；本次任务只写文档，不修改任何代码。

| 优先级 | 预留（下一次代码任务） | 现在不提前实现 |
|---|---|---|
| P0 | `src/ling/capabilities/tools.py`：整理 `ACTIONS` 白名单，使 `tool_id → ToolSpec/handler` 唯一；内部动作类型保留 `call_id/provenance/session_id/expires_at` | 不把所有 avatar/音频动作塞入 `ToolRegistry` 并宣称已能实时执行 |
| P0 | `src/ling/kernel/turn.py`：M 解析只产出同一动作意图；收尾记录提议及拒绝原因，不静默承认成功；`clients/web-console/src/types.ts/useChat.ts` 接通 `recall_proposals` | 不扩大 M 标记语法 |
| P0 | `src/ling/api/routes/recall.py`、`manage.py`：确认与通用工具入口收敛为共享 PolicyGate→授权→执行入口，确认凭据由服务器签发 | 不让请求体/模型提供 `confirmed=true` |
| P1 | `src/ling/ports/model.py`：先记下未来 `ModelRequest.tools?`、`ModelChunk.tool_call_deltas?`、`ChatMessageIn` 的 tool-result 事件与 `call_id` 关联规则；事件要有开始/参数增量/完成三态 | 不立即给现有 provider 加“永远为空”的字段，也不允许尚不能编码的 `tool` 角色被静默忽略 |
| P1 | `src/ling/capabilities/embodiment.py` 与 `domains/speech/service.py`：明确 `session_id/turn_id/sequence/deadline` 控制事件及 `ActionResult/PlaybackReceipt` 回执边界 | 不在文本轮次里伪造电话媒体控制 |

端口字段**在首个 T 适配器立项时**再落地：同时完成 provider 解码、路由透传、分片聚合、取消和契约测试；仅加 `ModelChunk.tool_calls` 的空字段不会产生双工能力。
预留的可交付物是**稳定的内部字段与语义**，不是让尚无消费者的可选字段进入生产契约。
第一个结构化适配器应验证混合文本/工具事件、断流、重复 call id 和取消旧轮，才允许被路由到动作调度器。
`ChatMessageIn(role="tool")` 若将来进入类型约定，必须同步定义 `tool_call_id` 和结果不可信包裹，并让不支持的适配器显式报 `unsupported`。
电话栈尚未在上述 `phone/service.py` 中出现；先定义媒体/控制会话契约，不把“看手机”状态机重命名为通话实现。

## §8 迁移判据

**以下任一条件满足，就停止新增 M 动作语法，并立项 T/控制通道；不是等所有条件都满足。**

1. 行动标记种类达到 **3 种**（当前看视频、撤回共 2 种）；第三种只允许为兼容现有离线流程的过渡，第四种禁止。这里按“需在回复正文解析/剥离的动词种类”计，不按短号个数计。
2. 任一新动作要求在回复完成前执行，或从完整意图到设备收到命令的 p95 目标 **<500 ms**；该动作立即走结构化事件，不能靠完整回复解析。
3. 出现真实通话/模拟具身联调：客户端连续送入音频，同时消费她的音频，允许插话并要求 **<200 ms 停播**；这就是“双工到来”，先建媒体与控制通道，离散模型动作随后接 T。
4. 一轮需要两个以上有先后依赖的动作，或模型必须看到前一个动作回执才继续生成；M 的轮末提议无法表达这种交错。
5. 流式标记泄漏、正文误吞或引用误触发任一高风险动作，哪怕只有 **1 次可复现**，即冻结该动作的 M 路径并修复/迁移；安全问题不等统计显著性。
6. 同一动作在两个 provider 上需要不同的标记提示或剥离规则，导致动作语义随供应商改变；应收敛到统一 `ActionIntent`，随后换结构化输入。

迁移验收：同一 `tool_id/args` 经 M 与 T 适配后产生等价的内部意图；策略拒绝、用户确认、幂等、执行回执和审计结果完全复用同一测试矩阵。
测延迟时从完整、可校验的结构化意图到设备接收计 p95；另外单列模型首事件耗时，防止用漂亮的调度数据掩盖首音超时。
达到第 3 条时，即使还没有任何模型主动工具动作，也必须先建会话控制通道；这不是等 T provider 做完才能开始的工作。

## §9 不做什么

不把 `<<撤回…>>` 扩展成电话/具身的通用 DSL；不靠更复杂的正则修复 provenance。
不为低风险每次点头弹确认；不以“免确认”推导为免授权、免白名单或免回执。
不让模型输出的 `confirmed`、`device_granted`、`platform_message_id` 充当执行事实。
不把 `ToolRegistry.call(confirmed=True)` 当作完整 PolicyGate，也不在确认路由复制第二套安全判据。
不把 ASR partial、过期场景、TTS 合成完成或 `FallbackNotice.used` 当成用户同意、在场或实际播放。
不在没有真实双工联调前实现空壳 T/provider 字段、电话媒体流或跨设备永久授权。
