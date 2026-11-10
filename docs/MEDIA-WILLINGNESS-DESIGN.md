# 媒体意愿、延后回复与真实忙态设计

日期：2026-11-09。本文只作设计；下述延后回复、媒体槽位及联动尚未实现，阈值是待验证的工程初值。
需求原话一：「看视频也不是有人要他看，他就必须得看，也是按他自己的意愿来」。
需求原话二：「等待时间也是按照他意愿来，给他自由度」。
需求原话三：「他在忙，可以给他变成他的真实活动状态，放到“正在看手机”那里，显示当前的状态」。
前情还要求全局仅一个视频观看执行，别的会话可以知道她在忙，以及她可以等一会儿再回复。

## §1 结论

1. **看不看由她选择**：当前以是否输出 `<<看视频 mX>>` 表示主动选择；不输出即可拒绝或改天看，执行前仍须过授权、额度和槽位闸。
2. **何时回由她提议、代码定界**：可明确选择立即或短时延后；延后回复有持久截止和新输入抢占，资源竞争与系统变慢不得包装成人格选择。
3. **忙态以真实媒体活动为主载体**：`ActivityRow` 记录实际在做什么，`media_slots` 校验执行权，`PhoneStateRow` 记录确实在看手机并投影到现有卡片。

## §2 现状与三处事实分裂

4. `ContextCompiler` 已在当前窗口给她「有人发了个视频，你还没看。想看就写 `<<看视频 mX>>`」和短号，因此她有知情后不写标记的选择（`src/ling/kernel/context.py:825-853`）。
5. 现有轮次先流式生成，再从全文解析标记并 `await _watch_videos`，只在清洗后正文为空且有观察/跳过原因时跟进生成（`src/ling/kernel/turn.py:911-927,1036-1075`）。
6. `WatchOutcome` 区分 `observed/skipped/denied_kind/reads`，额度与取不到内容不能混成拒绝意愿（`src/ling/domains/media/watch.py:89-121,226-293`）。
7. `PhoneView` 有 `state/watching_app/watching_since/last_looked_at/next_look_at`；`start_watching` 写 `WATCHING` 并清下次时间，`stop_watching` 写 `IDLE` 并从结束时重新计时（`src/ling/domains/phone/service.py:153-166,210-255`）。
8. 全仓生产调用核查：`rg -n 'start_watching|stop_watching' src clients` 只有服务定义；调用出现在 `tests/integration/test_phone_state.py` 和 `test_dashboard.py`，没有生产调用方。故目前“正在看手机”没有真实执行路径驱动，属未接线状态，不能把现有 `watching` 当媒体事实。
9. 仪表盘 API 直接投影 `PhoneService.current()`，前端 `PhoneBody` 只按 `state=watching` 显示「正在看手机」及应用名、「从 X 起」，否则「手机放下了」（`src/ling/api/routes/dashboard.py:580-620`；`clients/web-console/src/components/DashboardPage.tsx:305-329`）。
10. `ActivityView` 有 `kind/state/started_at/visibility/origin`；`current()` 只按 `updated_at` 取一条，可能漏掉媒体活动（`src/ling/domains/activity/service.py:66-89,237-249`）。
11. `start_for_segment` 按 `schedule_segment_id` 去重，是日程叶段专用；`end_schedule_window` 到点只能 `paused/cancelled/needs_review`，不是完成证据，不能借这两个接口伪造媒体执行（同文件 `:317-392`）。
12. `presence.scene()` 默认 15 分钟 TTL，过期返回 `unknown`；它可辅助描述场景，不是观看锁或当前视频事实源（`src/ling/domains/presence/service.py:47-49,413-479`）。
13. `media_slots` 尚是设计，拟用单行 `media.attention`、`held/active`、240 秒租约、20 秒心跳和代次栅栏；它负责互斥执行权，不能单凭“持有槽位”说她已在看（`docs/MEDIA-EXCLUSIVE-SLOT.md` §1、§3）。
14. 三处事实目前分裂：phone 说明是否拿着手机但没人驱动，activity 能记真实活动但未绑定媒体执行，slot 拟证明占用但未落地；因此本设计要求一次执行 ID 串起三者，并由一个编排器管开始、结束和回收。

现状核查范围是本工作树的 `src/`、`clients/` 和 `tests/`；这只能证明本仓库生产代码无调用，不能推断部署在仓库外的私有入口。
现有行为日志契约已列 `video_watched/video_watch_skipped/video_quota_exhausted`，但没有“她主动决定晚回”的事件（`docs/BEHAVIOR-LOG-CONTRACT.md` §3）。

## §3 意愿模型

15. **A1 采用**：提示已给出未看视频与短号，她不写标记、自然说「我等会儿再看」或「现在不想看」，即本轮不启动观看；没有标记不能由代码擅自补一个（`context.py:845-853`；`turn.py:1044-1049`）。
16. “有人发视频”只提供选择机会，不是用户命令她必须执行；她也可以回答与视频无关的问题，但未看时不得编视频内容或说「我看过了」。
17. **A2 不采用为意愿判定**：标记已经是她本轮的肯定选择；额度不足、槽位 busy、授权失败是执行拒绝，不能事后改写成「她懒得看」；若尚未对外发送，事实可交给表达层让她自然改口，但原因码仍如实保留。
18. **A3 首期不加第二个“愿不愿意”模型**：现有模型本来就在有选择的上下文里生成标记；第二次判定会与第一决定冲突。若将来要结构化选择，可把 `watch/skip/later` 作为同次生成的意图输出，再由代码验标记一致性，不能让规则按心情替她决定。
19. 人设来自 `PersonaService` 的 `system_prompt/traits/boundaries` 及 `ContextCompiler` 的 persona/constraints 车道；车道预算为 100000 token（`src/ling/domains/persona/service.py:89-125`；`src/ling/kernel/context.py:123-132,508-557`）。
20. 心情可读 `AffectService.current()`；精力、社交负担来自 presence；真实进行中活动来自 activity；“上次看视频距今多久”只能从有回执的观看/证据时间推导，不能把 phone 的 `last_looked_at` 当成上次看视频（`src/ling/domains/affect/service.py:78-101`；`src/ling/domains/phone/service.py:231-255`）。
21. 这些状态给她作**软选择及表达背景**：可影响她说「现在不想看」「稍后聊」的倾向和语气，不是自动拒绝阈值；代码硬闸只管授权、预算、受众、有效证据和槽位。
22. “状态只影响表达，不影响权利”原规则明说精力低仍回答用户直接提问，不能拿累当拒绝借口（`src/ling/domains/presence/service.py:1-9`）。这里的“不想看”是她对可选媒体动作的主动选择，不是系统从低精力推导出的拒答权；直接问题仍须及时回应或明确延后且有截止。
23. 她说「等会儿」后可以改主意：同窗口后续新输入或合格的自主轮次重新编译上下文，视频仍有效且未看过时仍给短号；她再次主动输出标记才尝试观看（`context.py:825-853`）。
24. 首期**不建待看队列**：“稍后”只留下未消费证据与她已说出的意向，不预订槽位、不自动重试、不承诺一定会看；到期也不能因一句「等会儿」就代她执行。
25. `MediaReadJob` 是显式请求后的持久媒体读取作业，有 `call_id/window_id/evidence_id/state/lease_generation`，不是“她可能想看”的愿望表；后台 `pending` 是已授权作业的执行生命周期，不把未标记视频塞进去（`src/ling/infra/models.py:1657-1775`；`src/ling/domains/media/jobs.py:369-404`）。
26. 如果未来要求“到时主动再决定是否看”，需另立有截止、可撤销的意向记录并重新取得她的选择；它仍不得成为抢槽 FIFO 队列，且不复用 `MediaReadJob` 冒充已请求读取。

“我等会儿再看”只有自然语言承诺时，要区分“稍后可能考虑”和“已约好具体时间”；没有明确时间与再次选择，不创建定时观看动作。
同一证据已观察过时可读已有观察，但不能把历史观察伪装为这次重新观看；新的意愿选择只决定是否发起**新**读取。

## §4 延迟模型

27. 三种延迟必须分别归因；用户眼里都像没立刻回，但只有**她主动选择晚点回**是人格动作。

| 类别 | 可判定触发 | 用户可见事实 | 行为日志 `action` 与关键 `attributes` |
| --- | --- | --- | --- |
| 意愿延后 | 她明确给出 `reply_later` 与目标时刻 | 「我想过一会儿回」，或安静等到截止 | `reply_deferred`，`cause=volition,source=actor_choice,chosen_delay_ms,deadline_at,source_turn_id` |
| 槽位等待 | `try_acquire=busy/contended` | 「这次没看；正处理别的媒体」仅在确认有效占用时 | `media_slot_busy`，`cause=resource,wait_ms=0,attempt_id,reason=occupied` |
| 系统慢 | 网络/模型请求超过预算或失败 | 如实说暂时处理不了，不能说她故意晾着 | 模型侧故障本身不记行为日志；运行日志/指标记 `cause=system,stage,elapsed_ms,error_kind`，实际用了领域回退才记既有 `upstream_fallback` |

28. 日志字段使用稳定 snake_case；`message` 写人话，`attributes` 放枚举/时长/ID；行为日志仅 500 条环形缓冲，不作持久事实或精确计数（`docs/BEHAVIOR-LOG-CONTRACT.md` §1–§3；`src/ling/audit.py:174-213`）。
    若系统超时导致回复未送达，投递/轮次状态与运行日志负责可追责记录；不能为凑“三类行为事件”把供应商 5xx 写成她的经历。
29. **（2026-11-10 用户裁决推翻原文，此处为修订版）** 意愿延后由她在生成正文**之前**提出结构化标记 `<<等会回 90>>`（秒数由她自己给，**不设固定档位**）。
    原文的「可选立即、15、45、90 秒；代码只接受 `0–120` 秒」**已作废**。用户原话：「做，但是只在她主动选择时才延后，且延后多久让她自由发挥，上限 5 分钟」。
    现行规则：**任意秒数**（含 `2分` / `1分30秒` 写法），代码强制 `0 < applied_delay_ms ≤ 300000`；超限**钳到 300 秒**，并**如实记录被钳过**（见第 29a 条）。模型不能延长上限，也不能自定无穷时刻。
29a. **被钳过必须对用户可见**。`DeferredReply` 同时存 `requested_delay_ms`（她要的，可能是 999000）、`applied_delay_ms`（实际用的，≤300000）、`clamped`（两者不等）。
    只存钳后值等于把**系统的上限说成她的选择**——用户会以为她只想等 5 分钟，而她想等 16 分钟。这是与「不许把槽位忙包装成人格动作」（第 27 条）同一条底线。
    因此 SSE `turn.deferred` 必须同时下发 `requested_seconds` / `applied_seconds` / `clamped`，前端要能说「她本来想等更久」。实现见 `src/ling/domains/deferred/markers.py`、`src/ling/kernel/turn.py`、`clients/web-console/src/useChat.ts::describeDeferral`。
29b. **生成宽限有硬上界，且短于「再等一轮」的直觉**。`hard_deadline_at = chosen_due_at + 60 秒`。
    没有这个上界，「她选了 300 秒」会变成「等 300 秒 + 生成超时（不知多久）+ 再等」——用户面对的是**无上界**的等待，而这正是延后回复最容易变成「消息石沉大海」的地方。到 `hard_deadline_at` 无论生成到哪一步都发兜底短句（第 33 条）。
30. 这一步是延迟意图，不是 A3 的“再判一次看不看”：只在需要决定是否晚回复时运行；模型缺席、解析失败或预算不足则立即回复，**不静默挂起**。
    **坏格式的处置**：`<<等会回>>`（缺秒数）、`<<等会回 abc>>`（非数字）、`<<等会回 -5>>`（负数）、多个标记并存 → 全部剥掉标记、正文照发、立即回复，并记 `reply_defer_rejected`（`reason` 分别落 `duration_empty` / `duration_unparsable` / `multiple_markers`）。
    **标记 + 正文同时出现**也按坏格式处理（`reason=body_present`，不延后、正文照发）：她既然写了回复，那就是回了；反过来（保留延后、丢掉正文）会让她生成的内容凭空消失，用户看到的是一轮空回复——那是**可观察的损坏**，比语气不完美严重得多。
    `reply_defer_rejected` **绝不**记成 `reply_deferred`：后者是「她真的延后了」的统计，混进坏格式会让这个数字失去意义。
31. 代码写持久 `DeferredReply`（表 `deferred_replies`，迁移 `0063`），字段分组见 `src/ling/infra/models.py`：身份与因果（`window_id`/`source_turn_id`/`input_sequence`）、时长三个数（第 29a 条）、截止两个时刻（第 29b 条）、状态与栅栏（`state`/`reason`/`lease_owner`/`lease_until`/`claim_generation`/`model_started_at`）。
    以 `UNIQUE(source_turn_id)` 唯一去重（库级，不靠进程内变量——多副本时那种去重等于没有）；新轮次再用 `turns.uq_turn_client`（`client_turn_id = deferred:{reply_id}`）挡第二道。
    不把它写成 `MediaReadJob` 或 `activities.running`，因为等待不是观看活动。
31a. **抢占栅栏是派生的，没有写入点**。判据是「这个窗口里出现了比 `input_sequence` 更大的 **`role='user'`** 消息」，不是某个「取消代次」计数器。
    理由：新输入入口分散在 web 路由、连接器入站桥、注意力轮询三处，任何「新输入时顺手 +1」的钩子都必然漏掉一处——而漏掉的表现是「她插进新话题补了一句」，极难复现、极难归因。派生没有写入点，也就没有漏写的可能。
    只数 `role='user'`：她自己的回复不算新输入，否则一条延后回复会把自己抢占掉。判据实现在 `domains/deferred/service.py::input_preempted`。
32. 到 `chosen_due_at` 新建**逻辑轮次**，重新编译当前上下文和授权，取得该窗口发言权才生成及投递；原轮先明确终结，不能重开 completed 轮或向关闭的 SSE 推 delta（`docs/ASYNC-TOOL-EXECUTION.md:92-104`）。
33. 到 `hard_deadline_at` 仍无可交付正文，代码终结延后状态并发确定性短句。现行文案是**模块级常量** `src/ling/worker.py::DEFERRED_FALLBACK_TEXT`（不在函数里拼字符串：它会被测试断言、也可能被前端引用）：
    「刚才想晚点回你，但没能及时整理好；你刚才的问题我还没有回答。」
    这句话只陈述可核实的事实：她想晚回是真的（登记过）、没能及时整理好是真的（超时/失败）、问题还没答也是真的。它**不**说「我马上回」「刚才在忙」这类无从核实的话。
    若投递回执不明，落 `failed`（`reason=..._fallback_undeliverable`）而**不写** `delivered_turn_id`——「状态 failed 但带着轮次 id」是自相矛盾的记录，会让对账无从判断。绝不声称已送达或盲重发。
34. 新输入到达同窗口时默认取消旧延后回复的**主动投递资格**，新轮把仍有效的旧输入纳入上下文一起处理；旧事实/已完成媒体观察可保存，但旧迟到回复不插进新话题（`docs/ASYNC-TOOL-EXECUTION.md:114-118`）。
35. 用第 31a 条的派生栅栏在**三个时点**各比一次：认领时、生成前、外发前；新输入与截止同时发生时只允许一方取得发言权。用户显式要求“哪怕我又说话也提醒”才另审持续通知，本期没有该授权。
35a. **外发前那次比较必须在「消息落库之前」**，不能在流式阶段、也不能在轮次结束之后。
    为什么：`_finalize` 里消息一旦落库，Web 路径上就等于已经送达了（Web 面板本身就是投递面）。在流式阶段判只能停掉一半正文；在轮次结束后判则什么都拦不住——那时 `turn.completed` 已经带着正文发给前端了。
    实现：`TurnCoordinator(deliver_gate=...)`，在 `_finalize` 内、消息落库与事件发出**之前**问一次；不通过则正文不落库不外发，但轮次仍终结为 `completed`（她确实说完了这一轮，只是这句话作废了——改成 `failed` 会谎报「生成失败」）。
35b. **被抢占的那一轮是「作废」，不是「失败」**：作废不发兜底短句（新轮已经把旧输入一起处理了，补一句「刚才没能及时回你」就是插进新话题的迟到补话）。因此抢占判定必须在「没有可交付正文」判定**之前**——顺序反了会把作废误判成失败，于是走兜底路径**又发一句**，闸白装了。
35c. **外部渠道当前不支持延后**：`deferred.service.SUPPORTED_CHANNELS = {"web", ""}`，非白名单渠道到点直接 `cancelled(reason=channel_unsupported)`，不发任何东西。
    理由：外发闸（第 35a 条）拦的是「消息落库」，而 QQ/OneBot 的投递发生在 `kernel/bridge.py::_deliver`，那道闸**管不到它**。宁可诚实拒绝（用户与日志都能看到 `channel_unsupported`），也不要静默走一条没被闸保护的投递路径。将来要支持群聊时，做的是「加渠道 + 补 `_deliver` 前的抢占检查 + 一组群聊用例」，而不是删掉一个碍事的断言。
36. 资源竞争不参与 `reply_later` 倒计时：`try_acquire` 仍立即 `busy/contended/unavailable`，不保持数据库事务/连接或自动等槽；模型也不得把“槽忙”重新标成自愿晚回（`docs/MEDIA-EXCLUSIVE-SLOT.md` §3–§4）。
37. 当前 `_visible` 先推正文、后解析视频标记；已推「我看到了」无法再靠晚些补一句修正。凡可能依赖观看结果的正文须在首次外发前暂扣，直到观察或拒绝结果确定；普通与媒体无关的正文可继续流式（`src/ling/kernel/turn.py:911-927,1036-1075`）。
38. 延后回复不采用“当前 SSE 卡住两分钟再续写”；它是原轮终结后的新轮次。若同步观看仍在未终结轮内等待真实结果，可在硬超时内暂扣媒体依赖正文，但不能先发不成立的因果声明。
39. 跟进生成只有 `observed` 非空才可说“看过”；`quota/missing/error/slot_busy` 均给无观察事实，按来源自然措辞，跟进失败用不含虚假观看声称的确定性短句（`src/ling/kernel/turn.py:1049-1073,1455-1492`）。

`DeferredReply` 的截止以数据库时间判定，worker 只在短事务认领到期项，生成阶段不占行锁；每次外发仍经过会话授权与发言权栅栏。
若延后期间已有视频后台作业完成，新轮只能读**已持久成功且当前受众仍有权**的证据；`pending/running/unknown` 不能写成她看到了（`docs/ASYNC-TOOL-DECISION.md` §3）。
原轮若选择静默延后，接口必须给客户端可区分的 `deferred` 状态与截止，不能把“没有 SSE delta”伪装成连接卡死；渠道端可按授权表达一句不含完成承诺的等待提示。
**已实现（2026-11-10）**：SSE 事件 `turn.deferred`，**先于** `turn.completed` 发出（前端靠这个顺序决定显示「她说等会儿」还是「她回了一条空的」），载荷含 `delay_id` / `requested_seconds` / `applied_seconds` / `clamped` / `due_at` / `hard_deadline_at`。
轮次状态**不新增枚举成员**：`TurnState` 是「这一轮执行到哪了」，而「她延后了」是**她的意愿**，不是轮次没跑完。硬塞进枚举会让「turn 是 completed 但用户还没收到东西」变成不可表达的矛盾。对外用 `messages.handled_as='deferred'` + SSE `turn.deferred` 表达。

## §5 忙态载体与展示

40. 选 `ActivityRow` 为“她真实在做什么”的**语义主载体**：媒体真正开工才建 `kind=video_watch/bilibili_feed`、`state=running`、`origin=tool_receipt` 的活动，绑定执行 ID、槽位代次与 `visibility`；计划、标记、排队和 `held` 都不算开始（`activity/service.py:127-161,169-210`）。
41. `media_slots` 仍是**执行权与占用有效性的权威**；必须 `phase=active`、租约未过期且 holder/generation 与活动匹配，活动才可作为“正在看”的当前事实。两种“权威”分管可执行与做了什么，不准只读一张过期表。
42. `PhoneStateRow` 只负责“确实拿着手机”及上/下次看手机的时间；媒体执行进入 active 时在同一短事务接入 `start_watching`，结束/取消/失权时以执行 ID 与代次条件调用 `stop_watching`，不要让旧执行者收掉新观看（`phone/service.py:210-271`）。
43. 现有 `start_watching/stop_watching` 不废弃：phone 是跨活动的持续手机关系，表模型明确不同于活动进度（`src/ling/infra/models.py:1341-1360`）；需要新增来源执行键/代次或等效关联，不能盲用当前无条件 `stop_watching`。
44. 同一开始事务完成 `slot held→active + activity running + phone watching`；任一步失败全回滚。结束事务按同代次关 activity、phone、slot；若出于技术限制分事务，读侧先以有效槽位和活动校验，再显示 phone 投影，修复任务补齐残缺。
45. `ActivityService.current()` 按更新时间只取一条，媒体显示必须按槽位 `activity_id` 精确取；可同时有其他活动，媒体槽不使普通聊天或日程活动全局互斥（`activity/service.py:237-249`；`docs/MEDIA-EXCLUSIVE-SLOT.md` §5）。
46. “正在看手机”卡片的主行仍表示手机关系，副行显示经授权、有效的当前活动；只在实际 running 时显示起始时刻，`lease_until` 不展示成预计结束。

| 状态 | 卡片主行 / 副行 | 可显示的时间 |
| --- | --- | --- |
| 空闲，曾看过手机 | 「手机放下了」/「暂无正在进行的手机活动」 | 「上次看是 X」及有依据的下次时间 |
| 正在看视频 | 「正在看手机」/「正在看视频」 | 「从 X 起」；不显示片名或预计结束 |
| 正在刷内容 | 「正在看手机」/「正在刷内容」 | 「从 X 起」；不显示账号、频道、链接 |
| 槽位被占但未开工，或其他会话被挡 | 不为被挡会话另写 phone 状态；owner 可见「正在准备媒体内容」，其他会话只见「正在处理媒体内容」 | 不显示观看起始时间 |
| 额度不足，本次没看 | 不改变原卡片；本次回复可说「今天看视频额度用完了」 | 不制造「从 X 起」 |

47. 从未看过手机且无有效活动仍保持现有 `SECTION_EMPTY`，不强行显示「手机放下了」；API 已特意不投影虚构的 idle（`src/ling/api/routes/dashboard.py:584-599`）。
48. owner 控制台可见经过授权的活动类别；另一会话只给「她正处理媒体内容」或更粗的「她正忙」，不见标题、链接、证据 ID、来源窗口、对方身份；与槽位设计 §4 的过滤一致。
49. 即使同窗口，也只给其有权读的观察/证据；另一会话的 `watching_app` 不得含「QQ · 某群」等可识别来源。API 在投影时做受众过滤，前端不靠隐藏 DOM 保密（`src/ling/infra/models.py:1364-1373`；`dashboard.py:603-619`）。
50. 崩溃或超时后读侧立即以租约与代次判失效，返回 unknown/空闲，不能等清理才停止显示“正在看”；≤60 秒零模型清理将孤儿活动标 `failed/cancelled`、条件清 phone 与槽位（`docs/MEDIA-EXCLUSIVE-SLOT.md` §3）。
51. 看完必须有真实观察/回执才可记 `completed` 和 `video_watched`；仅到 180 秒硬截止、计划到点、作业入队或 worker 失联都只能标失败/中断。presence 的过期即 unknown 同样适用于媒体投影（`activity/service.py:169-210,363-389`；`presence/service.py:453-479`）。
52. 对表达层只交结构化 `{activity_kind,occupancy_valid,watch_outcome,source,visibility}`；如 `FallbackNotice` 把 `used/state/source/reason` 分开，未知就说未知，不把回退文案写成活动事实（`src/ling/domains/fallback.py:158-197`；`src/ling/contracts/v1.py:1005-1025`）。

卡片的“正在看视频”也须与回复用同一份受众过滤投影；不能让 owner 卡片显示视频、外部会话却从未过滤的 `watching_app` 猜出来源。
phone 的 `watching_since` 用实际 `active` 时刻，不用标记生成、作业登记或槽位 `held` 时刻；下次看手机从真正收起时重新计算（`phone/service.py:218-255`）。
若 slot 有效但关联 activity 缺失，读侧降为「正在处理媒体内容」并报警修复，不猜具体 `kind`；若 slot 不可用，则不得凭孤儿 activity/phone 行断言正在看。

## §6 对两份既有裁决的修订

53. `MEDIA-EXCLUSIVE-SLOT.md` §1 原文：「抢不到立即返回 `busy`，不排队、不等槽位」；**保留**资源裁决。她可选择晚回，但这不授予等待占槽的权利，因全局单飞和短事务仍必须成立。
54. 同文 §8 原文：「不做等待队列、自动重试占槽、主动补话或‘稍后我会告诉你’的承诺」；**拆分修订**：不做媒体槽等待队列和自动重试继续有效；她明确选择的有界延后回复可承诺“稍后回”，须按 §4 持久截止、新轮发言权与送达回执兑现。
55. 同文 §7 的 `wait_ms=0` **保留**为槽位竞争指标；另记 `chosen_delay_ms`，不能把自愿等待计入槽位队列等待，也不能把处理耗时记作她的意愿。
56. `ASYNC-TOOL-DECISION.md` §1 原文：「只做 C……不主动补话；本期不做 A、B」；**保留**视频后台取证的 C 默认：结果自然在后续合格输入消费，不因作业完成自动补话。
57. 同文 §4 原文：「不做 B、在途流注入……不做 A 的待答队列及主动结果通知」；**仅替换 A 的一小部分范围**：允许她在本次输入明确选择有截止的晚回，并为此建新轮；这不是任意工具完成后的主动通知，未作出该选择时依旧 C。
58. **B 仍否决**：B 是同一未终结轮先说一段、工具结果回来后在同轮补依赖段；这里原轮已终结，截止时重新编译并取得发言权的新逻辑轮次。若要同轮卡住并补话，须另审分段因果与 SSE，不能借本设计绕过（`docs/ASYNC-TOOL-EXECUTION.md:21-23,92-104`）。
59. 旧裁决日期同为 2026-11-09；本文是后续需求的**设计修订建议**，不声称两份旧文件已改或现有代码已实现。实施时需同步修正文档与测试，避免并存相反规范。

先前 C 裁决谈的是**工具取证完成后的自动说话**；本设计的延后对象是她已接收的那条输入，来源、截止、取消代次均可追踪，故不会让每个媒体作业都生成通知。
如果产品后来要求“看完后一定主动报告”，那是跨轮结果通知 A 的另一次授权与验收，不由一句「等会儿」自动扩展出来。

## §7 分阶段实施与可程序化验收

60. 阶段一：给现有上下文与回复加明确 `watch/skip/later` 语义和 outcome 防线；暂扣依赖视频结果的流式正文，观察失败时不虚报。
61. 断言 1：有未看视频的上下文带正确短号；模型自然说「等会儿」且无标记，视觉调用数、`MediaReadJob` 新行、`video_watched` 均为 0。
62. 断言 2：单条新视频有标记但额度不足、missing、error 时 `observed=''`，记录真实拒绝码，最终文本不含「我看过」或视频内容；不写“她自愿拒绝”。
63. 断言 3：在 `_visible` 故意先生成「我看到了」再出标记且观看失败，任何 SSE delta 和外发正文都不含这句；与视频无关的普通回复仍即时流出。
64. 阶段二：实现单槽位、活动与 phone 的原子接线；含 20 秒心跳、240 秒租约、180 秒硬截止和代次栅栏（细则沿用 `MEDIA-EXCLUSIVE-SLOT.md` §3）。
65. 断言 4：20 个跨进程媒体请求只有一个 active；其余立即 busy，`slot_wait_ms=0`，没有 FIFO 待槽行，普通聊天不被串行化。
66. 断言 5：`held` 未开工、额度不足、缓存命中均不产生新的 running 媒体活动或 phone watching；active 后 activity/phone/slot 持同一执行键。
67. 断言 6：20 秒心跳续租，240 秒过期后旧代次更新、释放、证据提交均影响 0 行；读侧立即不显示“正在看”，清理后无残留 running 活动。
68. 断言 7：活动 `current()` 被另一条较新活动覆盖时，卡片仍按有效槽位 `activity_id` 得到媒体活动；另一会话只见粗粒度忙态，不见标题/链接/来源身份。
69. 阶段三：增加显式有界延后回复的持久调度、新轮次、抢占栅栏与投递回执；先在普通不依赖媒体结果的回复路径试运行，再评估视频结果依赖路径。
    **落地状态（2026-11-10）**：`domains/deferred/`（状态机 + 标记解析 + service）、迁移 `0063`、`worker.py` 周期扫描 `deferred.reply.sweep`、`kernel/turn.py` 产生意图与 `turn.deferred`、前端 `useChat.describeDeferral` 均已实现。
70. 断言 8：选择 45 秒延后时 44 秒前无回复，45 秒后恰好一个新轮；重启后仍按原截止，重复任务只产生一个轮次和一个投递计划。
    **验证**：`tests/integration/test_deferred_reply_flow.py::test_no_reply_before_due_then_exactly_one_new_turn`（注入固定 `now`，不 sleep 真实 45 秒）与 `::test_duplicate_sweeps_produce_only_one_new_turn`（连扫三次仍只一个新轮、只调一次模型）。
71. 断言 9（**原文的上限 120 秒已按第 29 条作废，此处为修订版**）：模型给出 999 秒时**钳到 300 秒且如实标注 `clamped`**（不挂起、不静默改写她的要求）；坏格式（缺秒数/非数字/负数/多标记/标记+正文）**一律不挂起**，剥掉标记、正文照发、立即回复；到 `hard_deadline_at`（上限 + 60 秒）无条件给确定性「未能及时回答」事实，不假装送达。
    **验证**：`test_deferred_reply_intent.py::test_clamped_values_reach_the_client`（999 → `requested=999, applied=300, clamped=true`）与 `test_bad_format_replies_immediately`（5 个参数化坏格式）。
72. 断言 10：延后期间同窗口新输入递增序号并取消旧投递资格；新输入与到点同时发生时旧回复外发次数为 0 或先已合法送达 1 次，绝无乱序迟到补话。
    **验证**：`test_deferred_reply_flow.py::test_new_input_preempts_before_generation`（生成前）、`::test_new_input_during_generation_still_blocks_delivery`（**生成期间**——这条是变异测试逼出来的：删掉外发闸后原有用例全绿，说明当时只覆盖了"抢占发生之后"，没覆盖"抢占发生在生成中"）。
    实现位置见第 35a/35b 条。
73. 断言 11：日志区分 `cause=volition/resource/system`；资源 busy 不生成 `DeferredReply`，系统慢不生成 `reply_deferred`，环形日志删除后业务行为不变。
    **验证**：`test_deferred_reply_intent.py::test_valid_defer_logs_reply_deferred`（`cause=volition`）与 `::test_bad_format_logs_rejected_not_deferred`（坏格式记 `reply_defer_rejected`，**不许**记 `reply_deferred`）。
    **注意**：日志缓冲是进程全局的 500 条环形缓冲，用例必须用"前后差集"断言，不能查全量快照——否则会被别的用例挤出的旧事件污染。
74. 断言 12：前端五类展示与 API 受众投影一致；过期槽位、未知 scene、仅有旧 phone watching 时均不显示“正在看视频”。

验收时用固定时钟和跨 worker 集成测试验证截止、租约与代次；仅用模型文案快照不能证明状态真实或投递成功。
可观测验收还应分别统计自愿延后时长、资源拒绝次数、系统处理耗时；三项不共用名为 `wait_ms` 的单一指标。

## §8 不做什么

75. 不因别人发送视频自动观看；不从低精力、心情差或“她在忙”自动推出拒绝直接提问的权利。
76. 不建待看 FIFO、占槽等待、后台自动重试，也不把 `MediaReadJob.pending`、`slot.held` 或一句「等会儿」说成已看。
77. 不把媒体租约截止当预计回话时间；不把模型/网络慢解释成她故意晚回；不承诺无法核实送达的消息已经发送。
78. 不向已完成轮次或关闭的 SSE 追加正文，不把工具结果伪装成新用户输入，不让模型决定无限等待或绕过授权/额度/受众门。
79. 不用旧 phone/activity/scene 状态冒充此刻；不在跨会话卡片或主回复泄漏视频标题、链接、来源窗口或对方身份。
