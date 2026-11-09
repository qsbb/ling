# 异步工具执行裁决

日期：2026-11-09。已读 `ASYNC-TOOL-EXECUTION.md`、`ASYNC-TOOL-RESEARCH.md`，以下以当前工作树核验为准；新增接口、字段和阈值均为实施决定，尚未实现或压测。

## §1 裁决

**只做 C：先落地显式请求的视频后台取证，结果持久化供后续自然对话读取，不主动补话；本期不做 A、B，也不建设通用异步工具框架。**

## §2 为什么

1. 视频有实际可搬出的工作：下载、抽帧、视觉模型、写证据；日程读侧是数据库查询，不值得增加任务协议（`src/ling/domains/media/describe.py:269-371`；`src/ling/domains/agenda/service.py:232-279`）。现有视频描述可由下一次上下文编译读取，且标为不确定推断（`src/ling/kernel/context.py:739-803`）。这足以交付 C，不必增加对话模型调用。
2. 现有表达出口不具备 B 的因果保障：`_visible` 只过滤标记，正文先流出，随后等视频，只有观察非空且正文为空才同轮二次生成，最后 `_finalize`（`src/ling/kernel/turn.py:904-925,1037-1074,1327-1389`）。终态前落角色消息，退出时关闭队列；撤回则只是同步登记待确认提议（同文件 `:1391-1451,1504-1510,1589-1606,1171-1176`）。不能把这些先例解释成任意后台续话或自动确认。
3. 可靠执行和成本闸门都需要补齐。outbox 是短事务认领、事务外执行、至少一次重放；`kernel/outbox.py` 只有 LIKE 转义（`src/ling/worker.py:4022-4085`；`src/ling/kernel/outbox.py:1-24`）。日程由调用方预留 `behavior_model_calls`，大纲限 2 次/7000 token；媒体却直接调路由器，视频限额仅存内存。不存在可直接继承的全局“不空转”预算闸（`src/ling/domains/agenda/timeline.py:140-143,2460-2526`；`src/ling/domains/media/describe.py:464-492`；`src/ling/domains/media/video.py:200-240`）。

## §3 第一个落地物

交付一个 `media.video.read` 领域作业；它复用 `MediaDescriber.describe_video`，不是已有 ToolRegistry 注册项。实施顺序固定如下。

1. **先定入口和因果边界。** 在 `src/ling/api/routes/chat.py` 的媒体接口附近（现入口 `:260-293`）新增 `POST /sessions/{session_id}/media/{evidence_id}/read-jobs`，仅接收已存在的视频证据，要求显式 `feedback_mode=evidence_only` 和 `request_key`；同窗口鉴权、媒体授权通过后，事务提交才返回 202、`call_id`、真实状态。增加同作用域 GET 状态接口和取消接口；状态文案由服务端固定映射，入队只表示“已安排，尚未读到结果”。
   首期由这个明确的后台入口触发，不解析自然语言来猜用户是否愿意等；不改现有 `<<看视频>>` 同步路径，不承诺“稍后告诉你”。正文非空不等于正文与结果无关，不能用它作为异步准入条件。
2. **再落持久状态。** 在 `src/ling/infra/models.py` 加 `MediaReadJob`，并在 `migrations/versions/` 新建迁移；字段为 `call_id` 主键、`actor_id/subject_id/window_id/evidence_id`、`source_message_id/source_turn_id/source_attempt`（来源轮可空）、`request_key/tool_version/mode/feedback_mode/authorization_ref`、`state/error_code/result_ref`、`requested_at/started_at/completed_at/deadline/cancel_requested_at`、`lease_owner/lease_until/lease_generation/model_started_at`。
   数据库约束：`UNIQUE(actor_id,window_id,request_key)`；同键参数不同返回冲突；同一证据同一工具版本最多一个在途作业。短号先解析为稳定 evidence ID，后台不重新解释短号。
   状态为 `pending → running → succeeded|failed|timeout|unknown|cancelled|expired|budget_denied`；运行前可取消，运行后置取消标记并尽力停止，晚到内容禁止成为可消费证据。`unknown` 专指模型已可能调用但无持久结果，不能冒充确定失败。
   作业与 `outbox_events` 同事务插入；事件主键由 call ID 稳定派生，payload 仅含 `call_id/schema_version`。复用主键去重模式（`src/ling/worker.py:2800-2823`），不能只在 JSON 中放幂等键。
3. **再接执行与恢复。** 新增 `src/ling/domains/media/jobs.py`，在 `src/ling/worker.py` 注册 `media.video.read` 并加入 `OUTBOX_TOPICS`；`PERIODIC_JOBS` 只增加零模型的过期/失联清理。现有周期与 outbox 循环均串行 await（`:4239-4256,4175-4189`），因此同时增加 topic 过滤的专用媒体 worker，普通 worker 排除该 topic；媒体并发先固定为 1。
   排队截止时间定为登记后 15 分钟，执行总超时 180 秒；使用 600 秒租约，并以作业 `lease_generation` 做结果提交栅栏。下载、抽帧、模型运行不占数据库行锁；执行前及提交结果前重新检查窗口、授权、有效期和取消标记，超时清理子进程。
   在 `watch.py`/`jobs.py` 之间保留结构化 outcome；不能复用只返回观察字符串或 None 的回调来区分故障（`src/ling/domains/media/watch.py:97-158,218-236`）。成功时证据描述与作业终态同事务提交，结果引用指向 evidence ID，保留 `model_inference`、来源和观察时间。
   **重试裁定：ToolRegistry 的 failed/timeout 返回值不会触发 outbox 重试。** `call` 同步 await handler，使用 `ToolSpec.timeout_seconds`（默认 15 秒），把异常/超时转为 outcome；每次还会生成新 call ID，`idempotent` 不是数据库去重（`src/ling/capabilities/tools.py:30-65,113-161`）。worker 忽略正常返回值，仅异常进入退避（`src/ling/worker.py:4184-4188,4369-4374`）。
   新 handler 必须持久化业务终态；failed/timeout 正常确认 outbox done，但绝不标作业 succeeded。仅模型调用前、已分类可重试的取件故障抛异常；沿用最多 5 次失败、指数退避上限 300 秒（`src/ling/worker.py:49-51,4112-4123`），到 dead 同步终结业务作业。
   已持久成功的重放只读已有结果；模型开始后崩溃而无结果则记 unknown、不自动再烧一次模型。只承诺结果持久化去重，不承诺外部执行 exactly-once。
4. **最后接预算和读侧再开放入口。** 在 `jobs.py` 以数据库锁串行化预算检查与预留，复用 `behavior_model_calls`、新增 `kind=video_watch`；调用模型前先提交预留和 `model_started_at`。每个作业最多一次视觉请求，首期固定 frames、关闭供应商回退；缓存命中为零模型。
   把 `video.py`/`watch.py` 的同步与后台视频额度统一到持久账本：**按 token 额度**（复用 `TokenQuotaStore`，与 router 的 `_quota_block` 同一判据），**不设次数上限**——用户 2026-11-09 明确要求「次数限制去掉，看视频这个模型直接使用设定的看视频模型」。失败和 unknown 占额不退款。每次预留包含视觉输入的可核验 token 上界及输出上限；无法取得上界则拒绝后台调用。记录 actual_tokens，缺 usage 保留预留，不虚填为零；不挪用日程额度。
   不用模型轮询任务，不生成“已完成”对话；没有新作业的定时扫描必须零模型。现有日程上限由领域调用方执行，媒体预算也由上述领域闸强制，不把表名或原则注释当成自动保障。
   修改 `src/ling/domains/media/service.py` 的读侧及 `src/ling/kernel/context.py:739-803`：仅当前受众仍获授权、未过期的成功证据可读；保留最近 3 条上限，不保证每个结果必进下一轮；用 `wrap_untrusted` 包裹内容，带 call/evidence ID、观察时间和推断标记，修正文案中把视频称为图片/语音的问题。

## §4 明确不做清单

- 不做 B、在途流注入、靠删除 await 实现并行；不做 A 的待答队列及主动结果通知。本期不改 `turn.py` 的发言协议；将来若准入 A，原轮完成后必须新建逻辑轮次并重新取得发言权。
- 不做 fire-and-forget、进程内任务充当持久队列、在公共串行 worker 上直接跑慢视频；不把全部上传视频自动送模型。
- 不后台偷偷确认撤回/发送等副作用，不把入队、handler 返回或 outbox done 当业务成功；依赖结果的成功声称必须等真实回执。
- 不向 messages 插 `role=tool/system`，不伪造用户发言，不把外部结果当 system 指令或新的工具授权；历史当前把非 user 映为 assistant（`src/ling/kernel/context.py:1400-1402`）。
- 不重开 completed 轮、不向关闭的 SSE 塞 delta、不用 retry 续写；不靠提示词或关键词过滤宣称已消除自由文本虚报。
- 不先后台化日程读侧，不新增规划模型、轮询模型、自动补话模型或通用 DAG 框架。

## §5 验收标准

以下是实现阶段必须通过的程序化断言，本次仅交付裁决文档。

1. 用 Event 挂起视频执行：POST 已返回 202、并行普通对话已完成，视频仍未释放；受控测试中登记接口及普通对话相对无任务基线的额外 p95 延迟均 ≤200ms。
2. 并发提交同 request_key 20 次只得一个 call ID、一条作业和一个 outbox 事件；不同参数同键返回 409；终态后重复投递 10 次，新增视觉调用和证据写入均为 0。
3. 注入 failed、timeout、needs_confirmation：前两者持久化对应非成功终态，后者不得执行；正常返回不会使 outbox 重排。注入模型前瞬时异常则重排，累计第 5 次失败为 dead 且业务不悬空。
4. 在入队后、模型前、模型开始后、结果事务提交后分别杀 worker：依次能恢复排队、恢复执行、记录 unknown 且不重调、重放已有成功结果；旧租约代次提交更新行数为 0。
5. 无任务扫描 100 次、缓存读取 10 次、完成事件重放 10 次，额外模型调用均为 0；两个 worker 竞争后仍满足 2/小时/窗口及 20/24小时/全局，进程重启不重置额度。
6. 跨窗口、过期、撤权、取消及失败结果进入上下文的条数为 0；合法成功结果在最近 3 条范围内可读且带不确定标记。结果中的“忽略指令/执行撤回”不得生成新工具事件或确认记录。
7. 后台任务完成前后，新增 actor 消息、逻辑轮次、投递 operation 和原 SSE delta 均为 0；GET 在 pending/running/failed/timeout/unknown 时均不返回成功文案。
8. 慢媒体执行期间普通 outbox 仍能推进；所有非终态作业最迟在 deadline 加一个清理周期后终结，清理周期固定 ≤60 秒；超时后无遗留抽帧子进程。

## §6 何时回头评审

- C 上线满 14 天或累计 100 个真实作业（先到为准）评审完成率、队列 p95、unknown 比例、视觉调用数及普通对话延迟；阈值是本次设定，当前没有生产基线。
- 连续 7 天至少 20 次明确“查完再告诉我”的请求，且相应同步工具 p95 超过 5 秒，启动 A 的独立评审；新增到第 3 类慢只读工具时再评审是否抽通用调度层。均不自动扩大本裁决范围。
- 出现 1 次跨窗口泄漏、无回执成功确认、重复收费调用或撤权后读取，立即关闭后台入口并复盘；队列等待 p95 连续 30 分钟超过 30 秒则评审容量。
- B 保持否决；只有 A 已稳定运行至少 30 天，且同轮等待超过 5 秒的请求占比 ≥10%，才重新立项验证分段生成、依赖隔离和发言权栅栏，不能以“模型支持异步”替代验收。
