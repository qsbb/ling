# 媒体注意力唯一槽位设计

日期：2026-11-09。本文是实施设计；下述 `media_slots`、接入与阈值尚未实现。

## §1 结论

1. 用 PostgreSQL **单行 `media_slots` + 短事务条件抢占**作为全系统唯一资源 `media.attention`；不持有跨请求数据库连接或事务。成功抢占才允许开始下载、抽帧、观看、刷流等耗时动作。
2. 槽位是执行权和占用事实的权威；成功抢占并实际开工后建立一条真实的 `running` 媒体活动，供“此刻在做什么”表达。另一会话读到的是经过受众过滤的占用摘要，而不是虚构另一件忙事。
3. 抢不到立即返回 `busy`，不排队、不等槽位；主回复取得“此刻确有媒体活动、但我这次没看”的结构化事实，按人设自然说“我现在正忙着看点东西，等会儿再看你发的”，同时可继续回答不依赖该视频的部分。
4. `video_watch`、未来的 `bilibili_feed`、`livestream` **互斥**；`kind` 只说明持有者做什么，不划分并发额度。普通对话不抢槽，能与占用并行。
5. 现有同步路径按短号解析、每条 `describe_video`、写证据，`WatchOutcome` 记录 `observed/skipped/refs/reads`（`src/ling/domains/media/watch.py:89-121,223-293`）；默认回调独立开数据库会话（同文件 `:352-395,405-456`）。槽位应包住实际读取，已缓存描述直接复用时无需长期占槽。
6. 一轮目前是 `compile → stream → 解析视频标记并 await 观看 → finalize`（`src/ling/kernel/turn.py:786-823,922-925,1037-1074`）；本文的接入不得把“已入队”当成“已看过”。

## §2 为什么不能用进程内锁或事务级 advisory lock

7. `asyncio.Lock` 只约束本进程；Web 请求、多个 worker 及专用媒体 worker 可在不同进程，故不足以给所有入口同一把锁。现有专用 worker 的“并发=1”只管自己的循环，覆盖不到同步回调（`src/ling/worker.py:1097-1153`；`src/ling/kernel/turn.py:1320-1346`）。
8. 现成 `pg_advisory_xact_lock` 只串行一个事务里的“查后写”：日程段按 `segment_id`，意图按 `actor_id`，连接器按 `scope`（`src/ling/domains/activity/service.py:397-419`；`src/ling/domains/agenda/intents.py:1310-1329`；`src/ling/settings/connector_service.py:364-368`）。
9. 事务一提交，`pg_try_advisory_xact_lock` 也释放；观看持续几十秒至数分钟，不能把它当跨请求锁。为等视频而占着事务/连接同样会耗尽连接池、拖长行锁。
10. 会话级 `pg_try_advisory_lock` 虽能跨事务，却绑定物理连接；异步会话归还连接池后，下一请求可能继承锁，释放也未必落在原连接。没有专用连接托管与失效处理，不采用。
11. 单行表可以短事务抢占、提交后释放连接；租约、持有者和代次均可读取，崩溃后有确定回收路径。这是本设计选择，现有三处 advisory 锁仍适合各自的事务内原子区间。

## §3 数据模型、租约与竞态

12. 新表 `media_slots` 预置且只保留一行：`resource_key='media.attention'` 为主键；`holder_id`（一次执行 UUID）、`phase=held|active`、`kind`、`actor_id`、`window_id`、`subject_id`、可空 `call_id/activity_id`、`started_at`、`lease_until`、`generation bigint NOT NULL DEFAULT 0`。
13. 空槽由 `holder_id IS NULL` 表示；`lease_until` 与 `started_at` 使用数据库时间，避免不同 API/worker 的本机时钟差。`holder_id` 与 `generation` 每次新占用均变化，重入仅限相同执行身份且仍有效。
14. 抢占事务对该**预置行**做 `SELECT ... FOR UPDATE NOWAIT`；行暂被别的短事务锁住时立即给 `busy/contended`，不进入等待队列；读到空槽或 `lease_until <= clock_timestamp()` 才写新持有者、`generation + 1` 并提交。
   抢占形状（示意 SQL；占用详情读取与受众过滤由服务层负责）：
```sql
BEGIN;
SELECT holder_id, lease_until, generation
  FROM media_slots WHERE resource_key = 'media.attention'
  FOR UPDATE NOWAIT;
-- 空槽或 lease_until <= clock_timestamp() 才执行下面的 UPDATE；否则返回 busy。
UPDATE media_slots
   SET holder_id = :new_holder, phase = 'held', kind = :kind,
       actor_id = :actor, window_id = :window, subject_id = :subject,
       call_id = :call_id, activity_id = NULL,
       generation = generation + 1, started_at = clock_timestamp(),
       lease_until = clock_timestamp() + interval '240 seconds'
 WHERE resource_key = 'media.attention'
 RETURNING generation, lease_until;
COMMIT;
```
   `NOWAIT` 报行锁竞争只回滚当前短事务；不能按“空槽”重试无上限。
15. 若需把竞争中的 `contended` 解释成“有人在忙”，先用独立只读查询确认仍有有效持有者；不能查明时返回 `unavailable`，不能编造占用者。无预置行、数据库异常也属于 `unavailable`。
   `unavailable` 一律 **fail closed**：不开始媒体动作，且不把“DB 坏了”假装成“别人占槽”。当作已拿到会破坏全局单飞；当作确定 busy 会误报她的活动。普通对话可继续，媒体请求如实说明暂时无法确认/观看。
16. 正常租约 **240 秒**，每 **20 秒**由独立短事务续至数据库当前时间加 240 秒；一次观看执行硬截止 **180 秒**，取消下载/抽帧子进程与模型请求后再收尾。阈值是本设计初值，须压测。
   同一 `watch()` 最多读取两条（`src/ling/domains/media/watch.py:70-71,247-293`）：先解析并筛出需真实读取的证据，再为这一批抢**一次**槽；不能每条释放再抢，造成一轮中途被别的会话插入。只命中已有描述时不抢。
17. 续租必须 `WHERE resource_key=? AND holder_id=? AND generation=? AND lease_until > clock_timestamp()`；更新行数非 1 即失权，执行者马上停止后续取件/调用并取消可取消的任务，不得继续写证据。
18. 正常结束仅持有者按同一组条件释放；释放先使活动转出 `running`，再清空槽位，两步放在同一短事务。失败不写“完成”，按实际回执置 `failed` 或 `cancelled`。
   `phase='held'` 仅表示已预留执行权；执行器准备好且核对持有者后，原子切到 `active` 并启动活动。准备失败则释放，不能声称“已经在看”。
   `phase='active'` 且租约有效，才可向另一个会话提供“正在看/刷”的事实；`held` 只能说“正在处理媒体请求”，仍不得透露源窗口细节。
19. 崩溃回收无需等待旧进程释放：新抢占者在租约到期后持行锁替换持有者并将代次加一；旧活动按原 `activity_id` 标失败/中断，不能靠到点标 `completed`。零模型清理任务每 ≤60 秒修复孤儿活动与状态。
20. **栅栏覆盖所有持久副作用**：结果、证据描述、活动终态与作业终态在提交事务中核对槽位 `holder_id/generation` 且租约有效；旧代次更新为 0，整笔回滚。旧执行者的 `finally` 也不能释放新代次。
21. 复用 `MediaReadJob.lease_generation` 的“每次认领加一、结果提交带代次”模式，但槽位代次独立；两者在后台作业结果事务中都要核对（`src/ling/domains/media/jobs.py:369-404,454-496`；`src/ling/infra/models.py:1757-1764`）。
22. **现有实现还不满足上一条**：`run_read_job` 调 `describe_video` 写证据，再另开事务 `_settle` 写作业终态（`src/ling/domains/media/jobs.py:691-728,732-747`）。实施时须将证据发布与双重栅栏原子化，或先写不可消费暂存结果后由带栅栏事务发布。
23. 租约防止永久占住，代次防止旧者晚到写库；它们不能单独证明外部模型/下载在网络分区或进程长暂停时绝无物理重叠。执行硬超时、失权取消、可终止子进程、租约大于硬超时加宽限是必要条件；无法中止的外部调用须如实记录“不确定”，不能宣称物理 exactly-once。

## §4 抢占失败时的表达链路

24. 读取有效槽位得到 `Occupancy{resource,phase,kind,started_at,lease_until,holder_window,is_same_window}`；详情须按窗口授权过滤，另一会话默认只见“她正处理媒体内容”，不见视频标题、链接、证据 ID 或对方身份。
25. `lease_until` 是**防死锁租约截止**，不是预计完成时刻；无可信任务进度时不说“还要 2 分钟”。若以后有进度估计，只能给明确标为估计的区间并允许未知。
26. 同一会话请求观看而槽位被占：`WatchOutcome` 扩充稳定拒绝码 `slot_busy` 与可空占用摘要；`observed=''`、`reads=0`，不能把 `quota/missing/error` 文案套过来（现有结果类型在 `src/ling/domains/media/watch.py:89-121`）。
27. 另一个会话只来聊天、不请求看视频：在 `ContextCompiler` 或轮次编排的受众过滤层读有效槽位与活动，给主回复一个短事实“此刻正占用媒体注意力，仍可回应普通问题”；**不**让回复先抢媒体槽。
28. 这条忙碌事实属于持续的真实活动，而非只影响失败轮次的瞬时提示：否则另一个会话尚未尝试看视频时根本不知道她在忙。`ActivityView` 已有 `kind/state/started_at/visibility`，但 `current()` 仅按更新时间取一条活动，不能保证取到媒体活动，需按槽位 `activity_id` 定位（`src/ling/domains/activity/service.py:66-89,237-249`）。
29. 活动须在成功抢占并实际开工时才置 `running`，用真实执行来源，不能把打算看、入队或仅仅持有空闲租约写成“正在看”。`start_for_segment` 专用于日程叶段去重，不拿 `schedule_segment_id` 伪造媒体执行 ID（同文件 `:317-361,397-419`）。
30. `presence.scene()` 的 `focus` 可作粗粒度展示，不能当锁或视频事实源：场景只含 `state/activity_id`、15 分钟默认 TTL，过期返回 `unknown`；续期仅限合法边界或活动确在运行（`src/ling/domains/presence/service.py:34,47-49,413-479`）。不为抢占失败的新会话另写场景。
31. 回复层接到的是“占用有效、这次没有读取”两项事实，才按人设说“我正看点东西，暂时没顾上这个视频；你先说说想聊什么”；若占用来自自己同窗口，可说“我还在看刚才那个”，但必须有相应窗口证据。
32. 明确禁止：“我看完了/我看到了/视频里是……”（本次无 `observed`）；禁止把槽位竞争说成额度耗尽、技术打不开；`unavailable` 时禁止说“我在忙别的”或泄露另一会话内容。
   回复事实矩阵只由确定性结果码选择，模型只决定口吻：
| 槽位/观看结果 | 可以给主回复的事实 | 禁止的声称 |
| --- | --- | --- |
| 已占且 `active`，本次 `slot_busy` | 她正做某类媒体活动；本次没读 | “我看过你这条” |
| 已占但 `held`，本次 `slot_busy` | 她正在处理媒体请求；本次没读 | “我已经在看” |
| 槽位空闲，本次不请求观看 | 无“忙”的槽位事实 | “我在忙看视频” |
| 数据库 `unavailable` | 当前无法确认/执行媒体读取 | “别人占着”“我看过了” |
| 读出有效 `observed` | 限定在真实观察与授权范围内 | 超出观察的细节 |
33. 参考现有“事实交给主回复、由人设表达”的 `_follow_up_after_watch` / `_watch_follow_up_fact`（`src/ling/kernel/turn.py:1360-1419,1455-1490`）及 `FallbackNotice` 的来源与原因区分（`src/ling/domains/fallback.py:158-197`）；结构化事实是表达输入，不是新用户发言或成功证据。
34. **接入前必须修正现状**：当前调用把 `_watch_videos()` 返回的字典整体作为 `observation` 传给跟进生成，且只在正文空时跟进（`src/ling/kernel/turn.py:1037-1055,1320-1358`）；字典非空会走“刚看了”分支，失败时也可能误报。应按 `observed/skipped/denied_kind` 解包，`slot_busy` 单列。
35. 当前 `_visible` 在看视频结果出来**之前**已流出正文（同文件 `:904-925,1037-1055`）。实施需暂扣可能依赖观看结果的正文至判明 outcome，失败则重生成或安全改写后再外发；已流出的“我看到了”无法靠收尾补事实纠正。普通非媒体回复维持即时流式。
36. 若跟进模型失败，也必须给出基于事实的确定性短句，避免失败时空回复；这条短句可按人设模板，但不得描述未看的内容或虚构 ETA。

## §5 与 `MediaReadJob`、activity 域的关系

37. **槽位独立，活动是“她正在做什么”的表达载体，`MediaReadJob` 只是其中一种视频后台请求的生命周期载体。** `media_slots` 不存观察正文；证据仍归 media 域，作业仍归 jobs 域。
38. 现有 `MediaReadJob` 有 `call_id/window_id/evidence_id/state/lease_*`，并有 `(evidence_id,tool_version)` 对 `pending/running` 的部分唯一索引；它防同一证据重复在途，**不防不同证据、同步观看或未来刷流同时运行**（`src/ling/infra/models.py:1645-1700`；`migrations/versions/0058_media_read_jobs.py:151-164`）。
39. 可另建 `UNIQUE INDEX ON media_read_jobs ((1)) WHERE state='running'` 实现“至多一条运行作业”，并能查 running 行获持有者与 `started_at/lease_until`；但它只覆盖这张视频作业表，过期行仍占索引位，不能直接纳入同步回调或没有 `evidence_id` 的刷流。
40. 唯一冲突是异常路径：并发事务可能等对方提交，需 savepoint 捕获指定索引 `IntegrityError`、回滚子事务后再查持有者；不能把任意唯一冲突都翻成 busy。独立单行 `NOWAIT` 能直接表达 `acquired/busy/unavailable`，读写同一资源身份。
41. 后台 `pending` 仍可表示“已登记但未执行”，却不能因槽位忙而排队等它空：登记时能确认占用就立刻拒绝；若登记后到执行才碰撞，则在短事务中终结为新的 `busy_denied` 状态并正常确认 outbox。需同步扩展状态机、迁移 0058 的 CHECK、GET 文案与审计，不能把它冒充 `succeeded` 或无限重试（`src/ling/domains/media/jobs.py:88-151`；`src/ling/worker.py:954-999`）。
42. 现有工作树已有 handler、专用媒体 worker、失联清理（`src/ling/worker.py:954-999,1071-1153,4627-4630`）；但无统一槽位，且 `MediaReadJob` 当前只在认领与模型开始时设置 600 秒租约，未见周期心跳（`src/ling/domains/media/jobs.py:356-362,369-451`）。`docs/ASYNC-TOOL-DECISION.md:24-34` 的目标与现状不能混写成已完成。
43. activity 活动在 `held→active` 的短事务创建/切换，并带槽位 `holder_id/generation` 关联；失权或回收关闭对应 `activity_id`。活动可供有权限会话了解状态，跨会话摘要仍由槽位与受众门禁确定，不能只读默认 `subject_private` 活动或把私聊视频标题暴露给其他人（`src/ling/infra/models.py:1385-1414`）。
   两条写入边界分别由一个服务负责：媒体槽服务裁定可执行，activity 域写活动；任一侧失败时整笔开始事务回滚，不留下 `active` 却无活动的状态。
   后台作业的 `call_id` 只在持有者是 `media.video.read` 时填写；同步回调使用本次轮次与执行 ID，不伪造一条 `MediaReadJob`。
   旧活动可能与媒体活动并存，`ActivityService.current()` 按更新时间选一条并不代表全局排他；回复时按槽位引用的活动取值，必要时明确“同时还有别的状态”。

## §6 未来多 kind 扩展

44. `resource_key='media.attention'` 保持不变；`kind` 可取 `video_watch`、`bilibili_feed`、`livestream`，均争同一行。第二种接入只新增 kind 校验、执行适配器、可见摘要及自己的结果/取消语义，不新增一把锁或一个独立 worker 并发豁免。
45. 每个长占用入口统一走 `try_acquire → start activity → execute/heartbeat → fenced commit → release`；缓存命中或只读取已完成证据不抢槽。新 kind 若有自己的作业表，仍复用槽位，不强塞进只适合视频证据的 `MediaReadJob`。
46. `kind` 不是权限：先验授权、窗口可见性和来源，再抢槽；跨会话只传允许公开的粗粒度占用状态。以后若确需可并行的独立能力，应另立资源名并单独评审，不能偷偷让两个媒体 kind 共用名称却绕过互斥。

## §7 可观测与验收

47. 每次成功占用记 `media_slot_acquired`：“开始看这段视频”；字段 `resource/kind/holder_id/generation/window_id/started_at`。续租只打聚合指标，不每 20 秒写一条人话；释放记时长与 outcome。
48. 每次竞争记 `media_slot_busy`：“想看这段视频，但她正忙着看别的内容，这次没有看”；字段 `resource/request_kind/holder_kind/request_window/holder_window_same/duration_ms/reason=occupied`，不含媒体原文。
   不设置“等待媒体槽”的业务状态：争用计数记录 `wait_ms=0`（仅有短事务耗时），`contended` 单独计数，不伪装成真的排队后放弃；后台 `pending` 的队列等待另计，不与槽位等待混淆。
49. `contended`、`unavailable`、超时、栅栏拒绝分别记稳定原因码与计数；用户侧只有已确认占用才说“忙”，数据库异常说“这会儿没法确认或打开”，不猜他人状态。
50. 行为日志用稳定 snake_case action、人话 message、结构化 attributes（`docs/BEHAVIOR-LOG-CONTRACT.md:25-49`；`src/ling/audit.py:174-213`）。它只有 500 条进程内环形缓冲，**不能**用于 24 小时准确计数（合同 `:1-21`）。
51. 另以持久审计/计数事件记录每次**真实观看尝试被占用拒绝**，键含 `attempt_id` 防重试重复计数、时间戳、`reason=occupied`、kind 与窗口匿名标识；查询近 24 小时 `COUNT(DISTINCT attempt_id) WHERE reason='occupied'`。普通聊天只是提及“忙”不算“没能看”。
   `attempt_id` 对同步路径可由 `turn_id + watch_marker_ordinal` 稳定派生；后台路径用 `call_id`。同一次请求重放只覆盖同一审计键，不再次累计。
   `held` 因准备失败而释放、`active` 正常完成、失权回收、数据库不可用，分别记录 `prepare_failed/completed/lease_lost/db_unavailable`；指标按 `kind` 和原因码聚合，不以行为日志环形缓冲计数。
52. 验收 1：20 个跨 API/worker 并发视频请求与一个刷流请求，数据库任一时刻只有一个有效 `media.attention` 持有者，耗时动作实际开工数最多 1；其余立即得 `busy`，不进入待槽队列。
53. 验收 2：A 挂起观看时，B 的普通对话继续完成并可说真实的粗粒度“正忙”；B 不见 A 的标题、链接、受众 ID，不能声称 B 的视频已看。非媒体对话不因媒体槽排队。
54. 验收 3：未占用时另一会话不得凭旧 activity/过期 scene 说“正在看”；释放、失权、崩溃清理后均不残留可见的 running 媒体活动。
55. 验收 4：固定时钟推进 20 秒续租、240 秒过期；旧代次的续租、释放、证据/作业/活动提交均影响 0 行，新持有者保持有效。崩溃后按时回收且没有永久占位。
56. 验收 5：注入 DB 不可用与行短暂竞争；前者零媒体启动且回复不谎称“她在忙”，后者不长等、不误判成功；恢复后可正常抢占。
57. 验收 6：缓存命中不产生新视觉调用；失败、额度不足、busy 时 `observed=''`，外发文本不含“我看过/我看到了/视频内容是”，不新增 `video_watched` 成功日志。
58. 验收 7：后台作业忙时终态为 `busy_denied`，outbox 不重排；成功时证据、作业终态与槽位栅栏同事务。重复投递、过期旧执行者不会二次发布内容。
59. 验收 8：制造 24 小时内 3 次真实 occupied 拒绝、2 次普通聊天提及忙、1 次幂等重试，审计查询结果必须为 3；行为日志被环形覆盖后结果仍为 3。

## §8 不做什么

60. 不把普通聊天全局串行化，不把“媒体注意力”误作发言权；发言仲裁仍按自己的轮次协议运行。
61. 不用进程锁、事务 advisory 锁或专用 worker 的并发数冒充全局跨进程互斥；不长期持有数据库连接、事务或行锁等模型返回。
62. 不做等待队列、自动重试占槽、主动补话或“稍后我会告诉你”的承诺；`ASYNC-TOOL-DECISION.md:5-7,36-43` 对后台证据和不主动补话的边界继续有效。
63. 不把租约截止说成预计完成，不把计划/入队/失联中的作业说成她正在看，不以模型自由文本代替授权、槽位栅栏或成功回执。
