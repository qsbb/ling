# 她的一天：日程、状态与梦境设计

> 架构决策稿，供实现与验收使用；本文不代表这些功能已经上线。依据是 `docs/DREAM-SCHEDULE-RESEARCH.md` 与当前源码。文中阈值、预算和权重均为**工程初值**，不是论文测出的系数。所有时间戳入库为带时区的 UTC 瞬时值；用户看到的钟点由 IANA 时区投影。

## 0. 先质疑需求，再定最小形态

长期陪伴感的必要条件不是“她每分钟都有一段故事”，而是**可预期但可改变的意向、与事件相符的状态、可信的记忆边界**。若把日程直接喂给“我做过什么”，系统很快会编造人生。现有 `AgendaService` 明确规定计划不是事实；`ActivityService` 要求完成证据。新增能力必须先服从这两个约束。

| 对原需求的质疑 | 建议与理由 | 坚持原话的代价 |
|---|---|---|
| 真需要全天分钟级吗？ | 默认只排 5–8 个大块；未来 2–3 小时才按需细化，只有事件密集块用 5–15 分钟。分钟级是**计划精度**，不是按分钟制造经历。[Generative Agents §4.3](https://arxiv.org/abs/2304.03442) 证明分层规划可行，但“只保留近窗细节”是本项目的成本选择。 | 约 24 小时 × 每小时多条；token、校验、重排成本上升，且细节被现实聊天频繁推翻。 |
| 为什么固定 04:00？她可能还在睡。 | 保留 04:00 作为**后台排程截点**，不是角色“醒来思考”的剧情事件；睡眠场景与梦另算。 | 若要求她在 04:00 亲自醒来排计划，会与 `wake_minute`、睡眠叙事冲突。 |
| “每天生成一整天”和“她决定要不要去”能同时成立吗？ | 大纲是可变意向，只有睡眠、已确认承诺、正在执行的活动是锚点；提议触发局部修订。 | 把全天排成不可变脚本，会让拒绝/临时起意失去意义；每次全量重生则容易破坏承诺。多轮约束难度见 [Flex-TravelPlanner](https://arxiv.org/abs/2506.04649)。 |
| 梦是不是可以直接复述前几天经历？ | 不可以。先提取人、地点、动作、情绪碎片再重组，梦只作虚构叙事；[Malinowski & Horton 2014](https://pubmed.ncbi.nlm.nih.gov/24635722/) 支持碎片性，未支持“日程原样入梦”。 | 把行程摘要直接生成梦，会得到伪装成回忆的流水账，污染自我经历。 |
| 天气是否一定改变心情？ | 先让模拟天气影响可选活动与场景，不默认驱动情绪。天气对情绪的总体效应小且个体差异大，见调研 §4.3 所引 [Denissen 2008](https://doi.org/10.1037/a0013497)、[Klimstra 2011](https://doi.org/10.1037/a0024649)。 | 强行“下雨就难过”会使人格公式化；接真实天气 API 又要定义地理位置、来源与有效期。 |

**最小可行形态**：一个按角色本地 04:00 划分的日计划版本、非等长大块、未来窗口细化、基于事件的局部重排、独立的梦报告、状态惰性结算、只读时间轴。宁可显示“今天暂时没什么特别安排”，也不把计划时间的经过算作事情已做。首片先有可查看的大纲与确定性兜底，后片再开细化、梦、临时提议。**不做**：全天预生成分钟剧本、每个 tick 调模型、真实人体健康/地理轨迹、REM 检测、梦的现实回执、把 tentative 计划说成“约好”、自动推送日程/梦给聊天用户。

模型预算按**角色的计划日**计，调用前预算预留、实际 token 由既有 provider quota 再把关；输入/输出是硬上限估算，不是价格承诺。使用现有已发布 `memory.extract` 模型职责，无法解析时走规则兜底；不悄悄借用 `dialogue.fast`。未来若有专用 `planning` 职责，再显式迁移配置，不假装当前已有该职责。

| 调用 | 触发及次数上限 | 每次输入/输出上限 | 每计划日上限 |
|---|---|---|---|
| 大纲 | 04:00 仅一次；非法结果最多一次修复重试 | 2,500 / 1,000 | 2 次，7,000 token |
| 近窗细化 | 窗口接近且确有粗块；每滚动 3 小时最多 1 次 | 1,000 / 800 | 4 次，7,200 token |
| 临时重排 | 有新提议且硬门允许；同一 `turn_id` 最多一次，同一聊天窗口每小时最多一次 | 1,500 / 800 | 2 次，4,600 token |
| 梦境 | 有候选素材且靠近醒时；超时不做第二次模型调用 | 2,000 / 700 | 1 次，2,700 token |

上界 **9 次、21,500 token/角色/计划日**；典型日约大纲 1 次 + 细化 2 次 + 梦 1 次 = 4 次、9,800 token。无候选、无新边界、无可用模型、预算耗尽均 **0 次**。聊天窗口“每小时一次”只限制重排模型调用，不限制她回应、拒绝或记录提议；另有全角色每日 2 次重排上限。预算闸是成本控制，不是精确审计，精确 usage 继续由 `TokenQuotaStore` 记录；多 worker 下预算预留必须落库，不能直接照搬进程内 `WatchBudget`。

## 1. 概念与写入边界

```mermaid
flowchart LR
  R[作息 / 人设 / 承诺 / 已证实经历] --> P[计划日及版本]
  P --> B[大块 约5–8个]
  B --> H[近窗小时段]
  H --> M[必要时 5–15分钟段]
  B & H & M -->|到边界且允许| A[Activity proposed / running]
  A -->|可验证的完成证据| E[SelfExperience]
  A --> S[SceneCommit 有效期]
  E -->|抽取碎片、打分| D[Dream 模拟叙事]
  D -. 禁止反向写入 .-> E
```

日程三层都是**意向**：大块是“想做什么”，小时/分钟是“若无新事，可能怎样做”。只有叶段用于启动活动；父段与子段有意重叠，不能把三层都当并列任务。`running` 是模拟世界当前进程，不自动证明真实世界发生；`completed + completion_evidence` 才能进入 `SelfTimelineService`，并保留 `origin`。睡眠块只表达预期休息窗；不能仅凭时钟写“我睡着了/做了梦”。梦是独立的**模拟叙事**，不是活动、计划或记忆事实。

复用与取舍：

| 所有者 | 本设计动作 | 不选的路及原因 |
|---|---|---|
| `agenda/service.py`、`actor_routines` | 保留软作息和旧 `agenda_plans` 的最多 4 件小事；由 `AgendaService` 作为时间轴唯一写入入口，新增 `agenda/timeline.py` 处理版本树。作息表不加列。 | **不把全天段塞进 `agenda_plans`**：其 `MAX_LIVE_PLANS=4`、`state=done` 语义和旧 API 都是“小事”；塞 20–40 段会破坏现有约束。`AgendaPlanRow.revision` 是单条小事版本，不能代替整日修订号。 |
| `activity/service.py`、`activities` | 加可空 `schedule_segment_id`；活动状态机和证据规则继续唯一写入。旧 `plan_id` 保留指向 `agenda_plans`。 | 不复用 `plan_id` 偷指新表；同一个字符串指两张表会失去外键和审计能力。 |
| `presence/service.py`、`presence_states` | 加 `satiety` (0–100) 与同一 `settled_at` 惰性结算；`scene_commits` 仍是当前状态且带 `valid_until`。饥饿低只影响选择，不阻断直接答复。 | 不建“饥饿 tick”或独立位置表；否则与已有精力结算、当前活动地点冲突。 |
| `affect/service.py`、`affect_states` | 保留现有 `emotion/intensity/decay_per_hour` 的**线性**衰减和 global/window 隔离；加 `valence/arousal` 可空列，写入时由 OCC 映射规则确定，并新建短期 `affect_signals` 留住梦需要的日内峰值。 | 调研 §4.1 建议二维状态与“指数衰减”，但源码实际是线性；不能宣称复用指数衰减。第一阶段不换衰减公式，免得旧情绪突然变长或变短。单靠当前 `affect_states` 无法回看日内峰值。 |
| `world/service.py`、`world_places` | 地点继续是模拟地点。活动当前位置取运行中活动/有效 scene 的 `place_id`，计划地点取段的 `place_id`。后期新建 `world_weather_states`，只存有来源、有效期的模拟天气。 | 不往 `world_places` 塞“当前位置/天气”：地点是静态词典，天气是随时间变的投影；现实天气必须另有可信来源，首版不接。 |
| `goals/service.py`、`commitments`；`phone/service.py`、`proactive` | 对人的承诺是不可移动锚点；看手机与主动联系保留各自状态机和门控，只读取日程的“忙/休息”提示。 | 不用 `commitment_level=confirmed` 的日程条目替代 `CommitmentRow`，后者才有履约与投递状态。日程不直接发送消息。 |
| `memory/self_timeline.py` | 继续只从有证据的完成活动派生。新增过滤：时间到点/仅模型自述不构成 `completion_evidence`；梦不可进入 backfill。 | 不把梦写 `self_experiences` 再贴 `origin=simulated`：检索者容易漏过滤；从结构上隔离更可靠。 |

情绪事件映射采用有限规则，例如“用户欢迎提议”→ `warm`、负性聊天事件→ `uneasy`，按事件有利/不利、归因、确定性决定类别（[OCC](https://doi.org/10.1017/cbo9780511571299)）；二维值只是连续强弱与语气辅助（[Russell 1980](https://doi.org/10.1037/h0077714)）。低精力、低饱腹和坏天气可降低**可选活动**的可行性，不能改变安全/隐私/答复义务。模型只能提议“这顿饭是否令人满足”；最终加饱腹必须有活动完成与吃饭证据，不能因为计划了早餐就 +35。[Humanoid Agents](https://arxiv.org/abs/2310.05418) 只支持粗需求变量能影响活动分布，不提供本项目的 `+35` 或阈值。

## 2. 时间轴语义：两个“日”，一个真实时钟

**展示日**是用户有效时区的本地 `[D 00:00, D+1 00:00)`；**计划日**是角色规范时区的 `[D 04:00, D+1 04:00)`，以起点日期 D 命名。`load_user_timezone` / `effective_user_timezone`（`settings/user_preferences.py`，DB > env > `Asia/Shanghai`）为唯一时区解析入口；单角色部署取 owner subject 的有效时区，写进计划日快照。`ActorRoutineRow.timezone` 作为旧作息值必须在迁移/设置写入时对齐或显式标为冲突，不能让 worker 偷用它算另一套“今天”。多用户只共享角色的 UTC 计划，按各用户自己的有效时区投影展示；非 owner 的展示时区不能反过来改角色计划。

04:00 为 D 生成的是**D 04:00 至 D+1 04:00 的可变部分**，并建立 D 夜里从 `rest_minute` 到 D+1 `wake_minute` 的**跨夜睡眠锚点**（可越过 D+1 04:00）。D 的 00:00–04:00 来自 **D−1 计划日**跨夜段，绝不是 D 04:00 回填的“已经睡过”。D 04:00 若上夜睡眠锚点仍覆盖到 wake，它先沿用这段，再从 wake 起生成新可选活动；不生成与锚点重叠的内容。用户例“00:00–03:31 正在睡觉”可作为展示日 D 对上夜锚点的裁剪显示，不能由 04:00 的模型追认。若首次启用没有 D−1：00:00–04:00 只显示“无已登记计划”；当前仍在作息睡眠窗可显示规则推断的“预计休息”，`source=baseline`，不写睡眠事实。若 D−1 已有时间轴但 worker 停过，恢复时只生成 **now 之后**，过去段显示缺口。

内部全用左闭右开 `[start_at,end_at)`；时间粒度 1 分钟，开始/结束可在任意分钟，不强制整点。示例的“00:00–03:31”若把 03:31 当最后一个包含的分钟，入库结束是 **03:32**。叶段互不重叠；允许空档，它表示自由/未决定，UI 不得虚构“正在做某事”。父子段的重叠合法；每个子段必须落在父段内。睡眠/confirmed 锚点之间有空档也合法。只要求 `start < end`、时长合理、夜间睡眠与承诺覆盖、外出地点存在、不能跨不可移动锚点，不要求全日覆盖。模型返回的“早餐吃了什么、想到了什么”只能是**预计细节/意向**；真正吃了什么、想到了什么需活动证据或明确的聊天事件。

DST：计划日边界按本地墙钟组合、再转 UTC，不用 `+24h`；一天可为 23/25 小时。生成的本地时刻若不存在，顺延到该日下一个有效分钟；若重复，选第一次出现的瞬时值（`fold=0`）并记录 `offset/fold`；若碰到已存在锚点，以锚点的 UTC 瞬时值为准。校验和去重均用 UTC 半开区间，不用 `HH:MM` 字符串。改用户/角色时区、`wake_minute` 或 `rest_minute` 后，**过去与 running/confirmed 不改**；原计划以旧时区和作息 revision 留档，新规范时区下用同一 `plan_id` 增 revision，只重排 `max(now,新计划起点)` 之后的可变段；有跨夜锚点冲突就保留已开始部分、给未来接缝做规则修补。若新时区把当前瞬时值投到另一个 `plan_day`，先按 UTC 相交区间查找当前日计划并修订它，不立刻插入新日期行；下一个无重叠的 04:00 才创建新 `day_plans`。以 `window_start_at` 和新旧计划日边界比对并记 `timezone_changed`，不能只靠本地日期唯一键防重复。这是工程决策，不是文献结论。

## 3. 调度、状态机与成本边界

`worker.py` 保留 `PERIODIC_JOBS`，增加**只检查 DB 到期边界**的 `life.schedule` 每 5 分钟任务；检查 04:00、细化窗口、梦窗口、sleep settlement 与失效 lease。真正模型调用由登记的 `@handler` 接受 outbox 事件；`OUTBOX_TOPICS` 与 worker dispatch 测试同步更新。事件由一次事务写入到期作业与 outbox，worker 重启后扫描 DB 补投。旧 `life.plan` 现为“醒后排 1–3 件”、`_LAST_PLAN_DATE` 为进程内去重；新时间轴启用后必须对同 actor 关闭其自动生成，不能并行生成两套；旧 `agenda_plans` 和手工小事继续可读。旧 `life.tick` 300 秒可保留，但必须区分旧小事与新时间轴段，按下列规则处理。

大纲模型输入是 `{plan_day, timezone, now_utc, routine, immutable_anchors, persona_summary, evidenced_experience_summary, current_energy_satiety, valid_places}`，输出仅接受 `{"segments":[{"kind","title","start_at","end_at","place_id","flexibility"}]}`：5–8 个非睡眠大块是目标，不足时可以更少；`start_at/end_at` 必须是带偏移量的 ISO 时间，模型不得写过去、梦、完成状态或新地点 ID。代码先插入/保留睡眠与承诺锚点，再验证模型可选段。细化输入只给一个父块、近窗与锚点，输出 `{parent_id,children:[{layer,start_at,end_at,title,kind,place_id}]}`；必须完整落在父块与未来窗口内，但允许父块剩余部分保持粗粒度叶段。细化到哪一层由活动复杂度决定：普通块小时级足够，只有需要具体动作的临近块才落到 5–15 分钟。模型只产候选 JSON，任何最终时间裁剪、重叠判定与 `is_leaf` 切换都由 `AgendaService` 一次事务完成。04:00 到期本身是有意义的调度边界，允许调用模型；其余空 tick 不调用。

| 事件与幂等键 | 何时执行 | 状态变化与失败处置 |
|---|---|---|
| `outline:{actor}:{plan_day}:{timezone_revision}` | 本地 04:00 之后首次检查；宽限至当天 rest 前 | `pending→leased→validated→committed`；按 `actor+plan_day` 唯一，锁计划日行，模型调用在事务外；结果提交时 CAS `revision`、核对时区/作息快照。非法 JSON/越界可同预算内修复一次；仍失败写 `degraded` 规则大纲。 |
| `refine:{plan_id}:{revision}:{window_start_utc}` | 距最早未细化段开始 60 分钟、且未来 2–3 小时存在粗段 | 一次只细化该窗口，输出小时子段与必要的 5–15 分钟叶段；父段保留，CAS 失败丢弃旧响应，未来窗口再试。不得为了填补空档而调用。 |
| `replan:{turn_id}` | 新提议被硬门放行 | 提议记录唯一 `source_turn_id`，模型决定意愿与局部替换；同轮重复 outbox 返回原决定。 |
| `dream:{actor}:{sleep_date}` | 预计醒前 30–60 分钟；候选为空则跳过 | 一晚最多一份；醒后 30 分钟仍未跑到则标 `missed`，不编“昨晚梦到”。模型失败显示“没有可记录的梦”。 |
| `sleep_settle:{actor}:{sleep_date}` | 醒时或之后首次检查，与梦独立 | 唯一键只结算一次；无模型。进程停机后可补数值结算，但不补写“睡着了”的事实。 |

数据库 `behavior_model_calls` 在调用前预留次数和估算 token，`actor+plan_day` 的事务级 advisory lock 串行检查总额及窗口额；预留与 job lease 同事务写，重启后旧预留仍计入当天上限，避免重复烧钱。模型超时 60 秒，lease 90 秒；超时后只能换新 attempt，旧 attempt 的预算不退款。提交结果需验证 lease owner、输入版本和唯一键，过期响应不能覆盖新计划。outbox 本身至少一次；并发 worker 的 `SKIP LOCKED` + 唯一键 + CAS 三层配合，不用 `_LAST_PLAN_DATE` 做权威。最多按现有 outbox 退避；达到上限后保留 dead/失败原因，提供手动重试，但重试仍受预算闸控制。排程、模型调用、提交分短事务，不能持行锁等待网络。

`life.tick` 只在到期边界或事件有动作：睡眠窗/已确认承诺可暂停当前模拟活动、场景设 `rest` 或 `unknown`；非睡眠的近窗叶段可 `propose→running`，由 `PresenceService.commit_scene` 设 15 分钟有效期。睡眠锚点只产生“预计休息”的计划投影，不创建睡眠活动。若活动仍 running，可在真实边界事件刷新 scene TTL；若服务停机，过期场景读为 unknown。**计划结束只允许暂停/取消/待核实，不允许自动 `completed`。** 现有 `_plan_window_ended` 分支用“计划时段已结束”充当完成证据，必须对新时间轴禁用；后续若要新时间轴的模拟活动完成，需要 action-specific 的世界状态变化、工具回执或用户明确确认，并在 `origin` 中区分模拟与真实。`SelfTimelineService` 对 clock-only 证据也应拒绝，避免旧路径再污染经历。当前已有的旧经历不在本迁移中重写。

`PresenceService.settle(now)` 已用 `settled_at` 按经过时间结算精力/社交负担；饱腹沿用这个时钟：醒时建议 `−5/小时`、睡时 `−2/小时`，`clamp(0,100)`，上限 24 小时跨度以避免重启后极端跳变；餐食**完成且有证据**才按种类 `+20..35`，模型最多给 `satisfied` 布尔建议，代码验来源、动作、一次性事件键。情绪仍是现有按读时线性衰减；`affect_signals` 记录由规则映射的带 scope/事件 id 的峰值，窗口内容不得跨受众注入。看手机由 `PhoneService` 决定“在忙别的”以及下次查看；日程只能提供 busy/rest 提示，不能直接改 `phone_states.next_look_at`。`proactive` 保留勿扰、预算、可达性门，时间轴仅作为一个软抑制信号，不能自动制造“想你了”的主动消息。

**catch-up**：04:00 错过但尚在同一计划日且离休息至少 40 分钟时，生成从 `now` 后第一个整分钟开始的剩余大纲，过去段永远保持空/历史；若接近休息，只建规则睡眠锚点并标 `degraded_late`。超过计划日不追补旧大纲。细化窗口已过去即跳过；梦超醒后 30 分钟即跳过；睡眠数值结算可按唯一键补做。时区变更或长期停机时每次最多追一个**当前**计划日，不按停机天数连环调用模型。

## 4. 临时提议：她的选择与局部修订

完整链路是 `聊天事件/turn_id → 意图提取为提议（不自动承诺）→ 硬门 → 意愿模型 → 重新校验 → 同事务提交 revision+1 → 对用户说明决定`。同一轮只决策一次；同一天反复提议可分别记录，但超预算后用确定性答复“现在不重新安排/晚些再决定”，不得假称同意。若提议含真实购票、发消息等外部动作，另走相应工具权限/确认，日程同意不等于执行。

硬门由代码执行：计划窗口仍在未来；`CommitmentRow.state=active` 的到期/履约缓冲、已有 `AgendaPlanRow.commitment_level=confirmed` 的约定、正在 running 的不可中断真实活动、睡眠下限（建议 `LifePolicy.min_sleep_minutes=420`，配置可改）、`rest_minute`、地点存在、食材等必要资源、精力/饱腹的不可行阈值、隐私/权限、预算与重排频率。若用户现有 wake/rest 作息本身不足 420 分钟，保留用户设定并标 `routine_below_min_sleep`，不擅自改作息；重排至少不能再压缩现有睡眠窗。7 小时是工程保护值，文献的成人 7–9 小时建议见调研 §3.1 [Hirshkowitz 2015](https://doi.org/10.1016/j.sleh.2014.12.010)；模拟角色不是医学个体。精力/饱腹对普通聊天绝不能成为拒绝理由，只限制可选外出等计划。

硬门通过后，模型只输出 `{decision: accept|decline|defer, reason_code, proposed_segments:[...]}`，可参考人设、关系、情绪、当前活动与已有日程判断“想不想去”，不能修改门本身。接受时可修改范围为 `[max(now,活动安全停止点), 下一个不可移动锚点)`；已过去的段、运行/已完成活动、confirmed 计划、承诺窗口、睡眠锚点和范围外段原样保留。若范围太小，选 `defer/decline`；不为了答应而悄悄挪承诺。入库时重新读取最新状态，在 `day_plans` 行锁下检查 `revision` 与所有锚点，旧叶段置 `superseded`（不删除），新叶段同事务插入、整日 `revision+1`，`plan_id` 不变，记录 source turn 与决定理由。若 CAS/校验失败，最多在剩余预算内一次重算；否则保留旧计划并向用户明确“今天安排没有改成”。子段重排时对应父段/`is_leaf` 一并原子更新，历史活动的 `schedule_segment_id` 仍指旧段。

不采用“模型每次重写当天余下全部时间”：长程约束多且多轮变化顺序敏感（[Flex-TravelPlanner](https://arxiv.org/abs/2506.04649)、[TravelPlanner](https://arxiv.org/abs/2402.01622)）；局部重排减少可破坏的锚点。也不把模型拒绝当硬规则：她可以出于性格拒绝，但原因必须是可向用户说明的软选择，系统级资源/承诺冲突则由代码明说。

## 5. 梦：有来源的虚构，绝不回灌事实

梦的“睡眠日”取**醒来所在本地日期**，在预计醒前 30–60 分钟生成；若角色跨日作息，以实际预计醒点定义 `sleep_date`。只取截至睡前已发生、允许 actor 私有访问的 `SelfExperienceRow` 与活动/承诺状态，**不取未到的日程当经历**。梦能受日间情绪影响，文献支持方向见 [Williams et al. 2026](https://pubmed.ncbi.nlm.nih.gov/41986553/)；不绑定 REM，[Oudiette 2012](https://pubmed.ncbi.nlm.nih.gov/22647346/) 对“REM=做梦”提出反例。这里的“梦”是产品中的模拟叙事，不能据论文推断角色真的经历睡眠分期。TMR 的线索选择只启发显式候选打分；[Rasch 2007](https://pubmed.ncbi.nlm.nih.gov/17347444/) 关于慢波睡眠的实验不等于软件可以做记忆巩固。

候选从最近 72 小时的有证据经历（去掉梦/计划）、`affect_signals`、paused/failed 活动、未履行承诺和人设核心抽取，每条候选规范化为 0–1 特征。候选 `c` 的工程初值：

```text
recency(c) = 2 ^ (-age_hours / 18) × (age_hours > 24 ? 0.3 : 1)
emotion(c) = clamp(max_signaled_intensity × (negative ? 1.3 : 1), 0, 1)
score(c) = 0.30·recency(c) + 0.25·emotion(c)
         + 0.20·unfinished(c) + 0.15·persona_relevance(c)
         + 0.05·body_ambience(c) + 0.05·stable_jitter(c)
```

`recency` 来自 `self_experiences.occurred_at`，`emotion` 来自同源事件关联的 `affect_signals`，`unfinished` 来自 `activities.paused/failed` 与 `commitments.active` 且逾期状态，`persona_relevance` 从 `PersonaService` 的核心特征/经许可关系摘要作有限标签匹配，`body_ambience` 来自睡前 `presence_states.energy/satiety` 快照，`stable_jitter` 用 `hash(actor_id,sleep_date,candidate_id)` 固定在 `[0,1]`。没有来源就记 0，不能让模型补证据。选 top 6–8，按来源去重，至少 2 条非同源碎片才生成；人设是底色，不作为自传事实。`0.30/0.25/...` 与 18 小时半衰期均来自调研 §2.3 的**建议初值**，须日志回放与人工盲评校准，并非研究估计值。威胁梦约 20% 的参考来自 [Malcolm-Smith 2012](https://doi.org/10.1016/j.concog.2011.11.004)；产品初始设定为最多约每 5 晚 1 晚含威胁、威胁梦成功脱身最多 1/3，按角色长期计数做软分布约束，不把单晚写死，也不把受威胁当默认基调。

在模型前先做可审计的重组：每段经历抽 `(人, 地点, 动作, 情绪)`，以固定种子跨**至少两段**交换地点或人物角色，再选“时间倒错/物品拟人/场景拼接”之一；不把经历原文、私聊原文放进 prompt。输入契约为 `{persona_traits, sleep_date, affect_summary, cues:[{cue_id, tuple, score, provenance}], transform, prohibited_claims}`，只传经受众过滤的最小摘要。输出 JSON `{title, narrative, mood, used_cue_ids, transforms_used, threat_present}`；限制叙事长度，所有 `used_cue_ids` 必须在输入中、至少两源、含明确变形，不能逐段照搬原经历；解析/安全/受众校验失败即丢弃。prompt 明示“这是虚构梦景，不是活动回执；禁止断言现实世界已发生、禁止给用户做承诺”。梦与记忆/总结检索物理隔离：只存 `dream_reports(origin='simulated', visibility='actor_private')`，不能写 `activities`、`self_experiences`、`MemoryEpisode` 或事实抽取 outbox；上下文若允许主动谈梦，也只能用“我构想的梦/模拟梦景”，并经过受众过滤。默认只在 owner 控制台展示。

跨夜情绪结算**独立于梦成功与否**。睡前保存 global 情绪峰值快照；醒时按时间推算现有线性衰减值 `I_linear`，再令 `I_wake = min(0.35, max(I_linear, 0.6 × I_bed))`（仅睡前仍显著且负性/高唤醒的 global 分量），`arousal` 朝 0 回归 40%，`valence` 朝 0 回归 40%；以 `sleep_settlements(actor_id,sleep_date)` 唯一键防重复。为什么要显式 `I_bed`：现有 `decay_per_hour≈0.15` 线性衰减下，8 小时后许多情绪已归零，简单对醒时值乘 0.6 完全不起作用。0.6 是调研 §2.3 的工程规则；[Walker & van der Helm 2009](https://pubmed.ncbi.nlm.nih.gov/19702380/) 仅给隔夜情绪加工的理论依据，未给这个系数。window 情绪不跨窗口搬运，梦的情绪摘要只能保持 actor 私有。若梦模型超时/返回空，只记录 `failed`/`no_dream`，照常结算情绪，不补写梦，也不称“忘记了梦”作为事实。

## 6. 数据模型、迁移与保留

建议迁移沿用现有 Alembic 版本链，`src/ling/infra/models.py` 与迁移同一提交更新。所有新枚举用 `CHECK` 或服务层白名单双重约束；`*_at` 为 `timestamptz`。表与必要约束如下：

| 表/变更 | 字段与约束（省略通用 `created_at/updated_at`） | 写入方与保留 |
|---|---|---|
| 新 `day_plans` | `id` PK（稳定 plan_id）、`actor_id`,`plan_day` date、`timezone`,`timezone_source`,`timezone_revision`,`routine_revision`,`window_start_at`,`window_end_at`,`revision>=0`,`state`(`pending/ready/degraded`),`generation_source`,`last_error_code`；`UNIQUE(actor_id,plan_day)`，`INDEX(actor_id,window_start_at)` | `AgendaService`；摘要/版本头保留 1 年。D 04:00 的日键不随本日修订而变。 |
| 新 `schedule_segments` | `id` PK、`plan_id` FK、`actor_id`,`parent_id` 自 FK 可空、`parent_key` 非空（根段为 `''`，子段为父 ID）、`layer`(`block/hour/minute`),`kind`,`title`,`start_at`,`end_at`,`place_id` FK 可空、`commitment_level`,`status`(`current/superseded/cancelled`),`is_leaf`,`anchor_kind`,`source`,`revision_created`,`revision_ended` 可空、`source_event_ids`；`CHECK(end_at>start_at)`、分钟边界、`UNIQUE(plan_id,revision_created,layer,start_at,parent_key)` 防止 PG NULL 绕过；`INDEX(actor_id,start_at,end_at)`。仅 `status=current AND is_leaf` 的同 actor 时间范围用 PostgreSQL `btree_gist` + `EXCLUDE USING gist (actor_id WITH =, tstzrange(start_at,end_at,'[)') WITH &&)` 防重叠；父子包含与跨日锚点由事务验证。 | `AgendaService`；superseded 正文 90 天后可归档成摘要，但被活动/决定引用的行不删，至少 1 年。需要扩展 `btree_gist` 的部署权限，迁移前检查。 |
| `activities.schedule_segment_id` | 可空 FK `schedule_segments.id`、索引；旧 `plan_id` 不改；新增的时间轴活动填它。 | `ActivityService`。先加可空列，再逐步接入，历史不猜关联。 |
| `presence_states.satiety` | `integer default 70 not null CHECK(0<=satiety<=100)`；沿用 `settled_at`、`revision`。 | `PresenceService`；既有行回填 70，标为模拟初值，不伪造过去餐食。 |
| `affect_states.valence/arousal` | 可空 `float`，各 `[-1,1]`；旧行保留 null，由读取层按 emotion 映射，下一次写入再填。新 `affect_signals(id,actor_id,source_event_id,scope,window_id,emotion,intensity,valence,arousal,occurred_at)`，`UNIQUE(actor_id,source_event_id,scope,window_id,emotion)`，window_id 空串代替 NULL。 | `AffectService`；信号 30 天归档/清除原始细节，梦只保留 cue id。 |
| 新 `schedule_change_requests` | `id`,`actor_id`,`source_turn_id`,`window_id`,`plan_id`,`base_revision`,`decision`,`reason_code`,`range_start/end`,`committed_revision`；`UNIQUE(actor_id,source_turn_id)`；不存原始聊天文本。 | `AgendaService`；90 天详细记录，审计摘要 1 年。 |
| 新 `behavior_model_calls` | `id`,`actor_id`,`plan_day`,`kind`,`window_id` 空串、`idempotency_key`,`attempt`,`state`,`reserved_input/output_tokens`,`actual_tokens`,`lease_owner`,`lease_until`,`provider_id`,`error_code`；`UNIQUE(actor_id,kind,idempotency_key,attempt)`，索引 `(actor_id,plan_day,kind,state)` 与 `(actor_id,window_id,created_at)`。 | 预算服务；90 天明细，provider token usage 不替代此表。 |
| 新 `dream_reports` | `id`,`actor_id`,`sleep_date`,`status`(`ready/failed/missed/no_dream`),`title`,`narrative`,`mood`,`cue_ids`,`transform_ids`,`origin='simulated'`,`visibility='actor_private'`,`plan_id` 可空；`UNIQUE(actor_id,sleep_date)`，CHECK 固定 origin/visibility。 | `DreamService`；梦正文默认 90 天后归档或清理，保留去重 tombstone 1 年。 |
| 新 `sleep_settlements` | `actor_id`,`sleep_date` 复合 PK，`bed_at`,`wake_at`,`global_affect_before/after`,`source_routine_revision`；不依赖梦 FK。 | `AffectService`；1 年。 |
| 后期新 `world_weather_states` | `actor_id` PK、`weather_code`,`intensity`,`source`(`simulated/observed`),`observed_at`,`valid_until`,`revision`；`valid_until>observed_at`。观测来源未接入时仅模拟值。 | `WorldService`；只存现值及 30 天变更摘要，不存现实地理坐标。 |

补一条数据库与服务层共同约束：`schedule_segments` 的同一 plan 必须属于同 actor，`parent_id` 必须同 plan；可用复合 FK 或在单一写入方的事务中锁定校验，不能只相信模型 ID。exclusion 只防**同一角色当前叶段**重叠，不限制历史 superseded 或父子。若原有 confirmed `agenda_plans` 与新时间轴重叠，插入前服务层把其区间作为锚点；DB exclusion 不能跨表，所以还需事务锁和最终校验。查询“今天”用 UTC 区间相交，不沿用旧 `AgendaService.upcoming(on_date)` 的“按 `start_at` 归日”口径——跨夜睡眠否则会消失。

冷启动：迁移只增表/可空列/默认值，先部署读取兼容；已存在的 `agenda_plans` 保持原值、`revision` 不重释，不倒推成日段。第一次 04:00 创建 `day_plans`；若当天启动较晚，只排剩余时间。没有既往自我经历，梦候选不足就 `no_dream`，不拿计划补素材。旧 `self_experiences` 不批量删改；新梦检索从上线起只接有来源的经历，排除仅“计划窗口结束”的 clock-only 证据，后续数据治理另做审计。回滚应用代码时，新表可闲置、旧小事仍可工作；数据库迁移不自动删表。每阶段先以 feature flag 控制新写入，验证后关闭旧 `life.plan` 对同一 actor 的自动生成。

## 7. 用户可见性与可观测性

`ling.behavior` 的“她的一天”应该是一串**可解释的决定**，调用 `ling.audit.behavior_event`，不能只在 `ling.worker` 上 `logger.info`，那会落成 runtime 分类。日志不记录 prompt、原始聊天或梦候选私密文本。建议事件：`schedule_outline_started/committed/degraded`、`schedule_refined`、`schedule_change_decided/committed/rejected`、`activity_started/paused`、`dream_ready/failed`、`sleep_settled`、`behavior_budget_denied`。结构字段统一含 `actor_id, plan_id, plan_day, revision, source_event_id, reason_code, model_profile, call_id, tokens_estimated/actual, interval_start/end, latency_ms`；拒绝/失败也有 `reason_code`。行为日志显示例：“04:03 排出今天 6 个意向大块（模拟计划）”“08:02 细化 09:00–11:00，花费约 1,240 token”“14:10 她接受电影提议，保留 18:00 已确认约定，只调整 14:30–17:30”“07:20 梦生成失败；睡后心情结算已完成”。行为日志是进程内环形缓冲，不是事实源或持久审计；排障时凭行 ID 查库。

后端在 `api/routes/dashboard.py` 为总览增**今日时间轴摘要**：按当前登录者的有效时区返回展示日、角色计划日、时区来源、计划状态、revision、下一件**可能**做的事、当前活动/scene 的来源、梦是否可查看；保持原有 `agenda`（最多几件小事）、承诺和目标独立。另建 `api/routes/life_timeline.py` 的只读端点，返回相交叶段、层级、`planned/active/superseded`、`source=baseline/model`、地点、置信边界、梦卡片与补救状态；由现有登录/受众鉴权，不把 actor 私有梦放进群聊 API。`clients/web-console/src/components/DashboardPage.tsx` 显示“今天她可能做什么”的 2–3 条摘要与“查看生活”；`LifeNowPage.tsx` 的 `/life?tab=now` 显示整日非等长时间轴、已过去/现在/未到、计划改动标记、事实与意向不同样式、梦卡片（只标模拟）；`/life?tab=rhythm` 仍放作息和软节奏，并给计划日界与时区解释。`api.ts`、`types.ts` 新增类型与接口，前端按后端给的 IANA 时区展示，不能用浏览器本地时区二次判断“今天”。空态写“尚无安排/梦没有记录”，故障写“暂时无法读取”，不能合并。

聊天问“今天她要干什么”时，读取同一个只读投影，只说“她**打算/可能**在 09:00 左右吃早餐；下午还没决定”；若有真实 `ActivityService.current()` 且 scene 有效，可说“模拟生活中正在……”，若过期或缺证据就说“不确定她此刻在做什么”。`tentative` 不能说约好，`confirmed` 必须附承诺来源；梦只有用户明确问且受众允许时，以“模拟梦景”呈现。控制台是最稳定的发现入口，不自动发每日播报，以免把计划变成扰民消息或假承诺。

## 8. 失败模式及可接受降级

| 故障 | 检测 / 降级 |
|---|---|
| 模型非法 JSON、缺字段、越界、空日程 | 严格 schema 解析、字段限长与枚举白名单；大纲可在预算内修复一次，仍错则规则大纲 + `degraded`；细化/重排直接保留原叶段；梦记 `failed/no_dream`。不把原文直接展示。 |
| 段重叠、跨锚点、父子越界、DST 错位 | 事务前纯函数验证 + DB exclusion/CHECK；失败回滚整版，保留旧 revision。绝不“只提交前半段”。 |
| 预算耗尽或 provider quota 用尽 | 跳过模型，主计划只用作息/承诺确定性兜底；近窗保持粗块，重排答复“暂未改动”，梦不生成；额度恢复也不追补过去。 |
| 模型超时、worker 重启、多副本抢同一任务 | lease + 唯一键 + 输入 revision CAS，旧响应失效；启动扫描当前计划日与未完成状态；超时尝试仍占预算。 |
| 时区/作息变化 | 记录快照 revision，未到段局部修订；过去、运行和承诺不动。冲突无法解时暂留空档并显示 `needs_review`，不编造新活动。 |
| 同一天反复提议 | 同轮去重，窗口小时限额与全日上限；可继续聊天和记录提议，但不静默改计划，明确本轮决定。 |
| 活动没有完成证据、睡眠/餐食只因时钟过去 | 暂停/标记未核实，`scene` 过期为 unknown，饱腹不补，`self_experiences` 不新增。 |
| 梦素材不足、梦越权引用私聊、模型把梦写成真实经历 | `no_dream`/拒收并记录 `reason_code`；不进事实链；已准备的日程与睡后情绪结算独立继续。 |
| 天气数据过期或地点不可达 | 过期天气为 unknown，地点不可用则只保留室内/无地点大块；天气不单独改心情。 |

## 9. 分阶段落地与验收

每片独立合并/回滚；以下是**实施清单**而不是本设计稿已完成的改动。测试目录按当前项目的 `tests/unit`、`tests/integration` 扩展，涉及 PostgreSQL exclusion/并发的用集成库验证，不能拿 SQLite 通过冒充通过。每片都跑相关测试、全量 pytest、`git diff --check`。

| 阶段 | 文件级工作 | 可程序化验收与负向测试 | 风险 |
|---|---|---|---|
| 1. 日界与大纲 | Alembic 新迁移、`infra/models.py`、`domains/agenda/timeline.py`/`service.py`、`worker.py`、`settings/user_preferences.py`、`api/routes/dashboard.py` | 04:00 同 actor 同计划日唯一；D 00–04 只来自 D−1 或显示无记录；23/25 小时 DST 日、跨午夜睡眠、进程重启各只建一日；模型坏 JSON 有规则兜底；**没有新增 activity/self_experience**。 | 旧 `life.plan` 并行与时区双权威；先 feature flag，关闭同 actor 的旧自动生成。 |
| 2. 按需细化与活动边界 | `agenda/timeline.py`、`activity/service.py`、`memory/self_timeline.py`、`presence/service.py`、`worker.py`、迁移 | 8 点可细化 9–11 点但 14 点仍粗；叶段不重叠、父子包含、只在边界开活动；计划窗口结束**不**产生 completed/证据/经历；停机后 scene 过期为 unknown；饱腹在餐食仅计划时不增加。 | 现有 life.tick 的 clock-only 完成路径，需隔离旧数据与新段。 |
| 3. 提议与局部重排 | `agenda/timeline.py`、`domains/conversation/service.py` 或意图接入层、`worker.py`、`goals/service.py` 只读锚点、迁移 | 同 `turn_id` 只一次决定；从过去起的段、confirmed 约定、`CommitmentRow`、running 活动字节级不变；修订只在下一锚点前，`plan_id` 稳定且旧段可查；预算满时拒绝修改；**接受提议不产生“看过电影”经历**。 | 用户话语意图误判；先只识别明确邀请，歧义时追问或 defer。 |
| 4. 梦与跨夜情绪 | `domains/dream/service.py` 新文件、`domains/affect/service.py`、`memory/self_timeline.py` 检索过滤、`worker.py`、迁移 | 同睡眠日梦/结算各最多一次；梦失败仍结算；输出每 cue 均可追溯且来自至少两源；**梦不在 activities/self_experiences/事实检索中**；window 情绪不跨会话；无证据旧日程不得入梦。 | 旧模拟经历含 clock-only 证据，需过滤；梦叙事隐私需最小化 prompt。 |
| 5. 可见性与模拟天气 | `api/routes/life_timeline.py`、`api/routes/dashboard.py`、`domains/world/service.py`/`weather.py`、`clients/web-console/src/{api,types}.ts`、`components/{DashboardPage,LifeNowPage}.tsx`、迁移 | 用户时区“今天”与 API 一致，跨夜块两天正确裁剪；计划/事实/梦标识不同；梦仅 owner/允许受众可见；过期天气 unknown，天气不直接 nudge affect；空态与故障分开。 | 前端旧 `start_at` 归日与浏览器时区兜底；逐端点对照测试。 |

实施前先确认产品文案是否接受“模拟生活/模拟梦景”的明确标识。若不接受，系统仍必须在数据和对话层保持事实隔离；不能为了拟人感删除来源标签。文献还没有“日程→梦”对照基准（调研 §2.4、§6），此部分的质量只能通过本项目盲评与回放逐步调参，不能把工程初值包装成科学定律。
