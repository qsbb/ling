# 0.0.5 后需求对抗性审查（2026-11-09）

## 口径与边界

- 审查对象是本次读取时的工作树，不是某个已发布 tag；代码含未提交改动，结论不能自动外推至线上。证据：`git status --short` 显示 `sessions.py`、`worker.py` 等改动及未跟踪 0059、0060。
- 数据证据来自 `postgresql://ling@127.0.0.1:55432/ling` 的只读事务；它是开发造数库，不代表真实用户。未执行写库测试，也未把“测试文件存在”冒充“本次通过”。
- “完成”指从入口到用户可见结果均接通；只有服务、迁移或设计文档，均不算交付完成。下文 `file:line` 指本次快照。
- 本次静态迁移检查：60 个 revision，编号无重复、`down_revision` 无断链；`alembic heads` 为 `0060_memory_purge`，开发库 `alembic current` 为 `0047_persona_scenarios`。
- 0059、0060 是审查期间出现的未跟踪文件；head 曾在本次审查中由 0059 变成 0060，因此合并前必须对最终提交重新核对。

## A. R1–R24 逐条核对

| 项 | 裁定与可核证据 | 交付缺口 |
|---|---|---|
| R1 模型即点即换 | **代码已接通**：槽位直接 `<select>` 并按 provider `optgroup` 分组，`clients/web-console/src/components/model-settings/RolesPanel.tsx:193-225`；SSR 用例 `model-settings.test.tsx:603-642`。 | 未做本次真浏览器点击与保存回读；不把 SSR 当交互验收。 |
| R2 关系页卡死 | **主要病因已修**：首屏 50 条，只有展开卡才建候选，`IdentityPage.tsx:135-170,230-237`；API 有 `LIMIT/OFFSET`，`manage.py:1182-1209,1212-1287`。 | `identity.browser.test.tsx:307-330` 用 2000 条**模拟接口**但只加载 50；真实 2000 人逐页加载到底的延迟、内存未测。 |
| R3 手动生成日程 | **可见入口与后端有链路**：按钮及轮询 `LifeTimeline.tsx:1070-1151,1227-1288,1399-1438`；API `api.ts:476`。 | 本次未以真实模型验证“点后生成成功”；需端到端验收失败和重试。 |
| R4 上游失效时回退 | **已补全**（2026-11-10）：前端新增 `FallbackHint`，按 `sections.phone.data.fallback`（`used/source/reason`）在回退时明说“这是按默认参数算的，不是她此刻的真实状态”；正常态零噪音。可用性卡也补上 scene 回退提示。真 Chromium 5 项（1440/375 无溢出）。 | 后端 `describe` 一词两义（见下），属语义改进，不影响本需求。 |
| R5 总览布局 | **实现有依据**：12 列自适应与 gutter `dashboard.css:1-68`，卡片跨列 `DashboardPage.tsx:690-749`。 | CSS 注释的尺寸不是本次实测；须在 375/1440 和中间宽度浏览器验收。 |
| R6 接入器配置 | **已完成**（2026-11-10）：前端从只读改为可编辑表单，接 `PUT /v1/settings/connectors/{scope}`，覆盖 202/409/422/503/rolled_back 全部分支；密钥恒空、清空只能走 `clear`；真 Chromium 11 项通过。`SettingsHub` 的“本轮只读”文案已同步改正。 | 无。 |
| R7 设置布局 | **已有实现**：三组结构 `SettingsHub.tsx:118-150`，双列/窄屏单列 `settings.css:163-166,355-360`；浏览器用例 `settings-layout.browser.test.tsx:167,214`。 | 浏览器用例本次未执行，且文件未跟踪；正式交付需跑并确认未 skip。 |
| R8 置顶及好感度排序 | **声称完成但实际断链**：置顶 API `sessions.py:244-275`、字段迁移 0059；`sort_windows_for_display` 有排序键 `conversation/service.py:105-144`。 | `sessions.py:222-233` **先** `list_windows()`，**后**查 `familiarities()`，没有把字典传给前者；`list_windows` 空字典排序 `service.py:357-361`。前端无 pin 调用/按钮（`api.ts`、组件检索）；用户不能置顶，也不会按好感度排。 |
| R9 会话标题 | **后端真接线**：OneBot `get_friend_list` `connector.py:1215-1267`；路由实际调用 `sessions.py:95-129,185-190,222-233`，备注优先 `:163-166`；群名独立采集 `:56-90`。 | 真实 OneBot 返回备注、群名和超时表现未本次在线验收；现有 `test_session_display_fields.py:137-242` 重点测群名/昵称。 |
| R10 消息左右 | **改动生效但有回归**：actor 右、其他人左 `MessageList.tsx:171,473-483`、`styles.css:830-889`；SSR `dialogue.test.tsx:509-553`。 | 抽屉打开后右侧 actor 下的 `prompt-entry` 被面板遮挡，见 C；需产品修复，且私聊方向需用户体验裁决。 |
| R11 撤回时点 | **实现与测试可见**：判定 `conversation/service.py:972-1032`，读侧保留看过的消息 `:1135-1165`，迁移 0056；`test_message_recall.py:323-331,447`。 | 当前开发库的 `retracted_after_seen` 列存在，但迁移版本戳仍 0047，不能由此证明升级流程正确。 |
| R12 撤回仅作 LLM 工具 | **核心链路存在**：工具注册 `capabilities/tools.py:281,330-366`、确认路由 `api/routes/recall.py:216-292`；集成 `test_recall_tool.py:214,716-726`，浏览器卡片 `recall.browser.test.tsx:300-308`。 | 本次未接真实 OneBot/QQ 回执测试；“只有工具”应再核对所有入口权限，不能仅看注册。 |
| R13 人设 10 万 token | **配置上限已提高**：`context.py:123-132` 给 persona 100000；traits 上限 `persona/service.py:157-176`。 | 这里按 2 字符/token **估算** `context.py:1209`；不等于供应商必能接受 10 万输入，需按目标模型上下文验收。 |
| R14 视频模型/额度 | **同步观看路径接通**：`watch.py:373-395` 明确视频职责；次数常量已删 `video.py:60-74`；token 闸 `watch.py:124-185,256-265`；`turn.py:1044-1073,1474-1483` 传失败事实。 | 后台视频读取第 4 步持久预算尚缺，不能把同步 token 余额闸当成全部视频成本保障；见 R24。 |
| R15 全局媒体槽 | **已实现**（2026-11-10）：`media_slots` 单行表 + 迁移 0061 + `domains/media/slot.py`（`FOR UPDATE NOWAIT` 抢占、240 秒租约、代次栅栏、fail-closed）；已接入 `VideoWatchService.watch()` 的耗时区间，生产装配 `enforce_slot=True` 有测试钉住。真 PG 20 并发抢占**恰好 1 个成功**。 | `bilibili_feed`/`livestream` 只留了 kind 常量，尚未接入执行器（设计如此：它们将来争同一行）。 |
| R16 可等待、自由决定回复时机 | **已裁决，实施中**（2026-11-10）：用户原话「做，但是只在她主动选择时才延后，且延后多久让她自由发挥，上限 5 分钟」。设计文档 §4 原有的「0/15/45/90 档位 + 120 秒上限」**已被推翻**，改为「时长由她自己给 + 300 秒硬上限」。与 R15 的冲突已裁定：槽位等待（`cause=resource`）**不是**她的意愿延后（`cause=volition`），两者分开记日志、互不冒充。 | 实现由独立子智能体在 `domains/deferred/` + 新迁移 0063 上做，本表在其交回证据后再更新为「已实现」。 |
| R17 她有权不看 | **机制部分成立**：`context.py:807-860` 明示“想看才写标记，没写当没看见”；`turn.py:1044-1049` 仅见标记才读。 | 这只证明模型可选择不写；未有真实模型对话样本证明她会按人设拒绝，而非总被诱导看。 |
| R18 忙态展示 | **已实现**（2026-11-10）：新增 `GET /v1/dashboard` 的 `media` 段（`dashboard.py::media()`），从全局槽位读 `current_holder()` 投影**真实**媒体活动；前端新增「正在忙什么」卡片。三个关键取舍：**不塞进「正在看手机」**——看视频≠看手机，合并会把具体事实说糊；只报 `active`（`held` 是还在准备）；**别人的会话只给 `kind`/`started_at`，不给 `activity_id`/`window_id`**（受众边界）。真浏览器 1440/375 各一条，无横向溢出。 | `phone` 那套 `start_watching/stop_watching` 仍是生产零调用——它管的是手机这一层，与媒体活动是两件事，不强行接线。 |
| R19 视频留存后总结入记忆 | **已实现**（2026-11-10）：新增 `domains/media/sediment.py`，在每日 `memory.archive` 里把**将要过期/已过期**且"她真的看过"的媒体描述沉降成记忆（走 `MemoryService.propose`，带来源、幂等）。五个筛选条件：真看过（排除 `model_inference`）、有真实 `source_event_id`、有归属主体、已过期、未沉降过。 | 不做"立刻再总结一次"（伪需求：观看结果本来就会复用描述）。模型生成的浓缩摘要未做——那要独立"非事实"存储。 |
| R20 睡眠记忆压缩 | **已实现**（2026-11-10）：新增 `memory.consolidate` 作业，由 `life.schedule` 在**预计入睡后 30 分钟**投递（一晚一条，幂等键 = sleep_date）；`memory_consolidations` 表（迁移 0062）记录每晚做了什么。第一版**确定性、零模型**：复用 `compact_low_value` 按人整理。`skipped` 与 `ok` 分开——空跑不记成功，否则"整理过 N 晚"就只是个心跳。 | 模型生成的"这一天浓缩成一句话"仍未做：那必须新增明确标注"非事实"的存储，不能复用记忆行（否则模型编的话会取代原始记忆参与召回）。 |
| R21 后台真删 | **未交付，正在开发**：现有 HTTP 仍调用软清除 `manage.py:272-283`，`forget_subject` 保留原文 `memory/service.py:1431-1486`。 | 新 `purge.py:145-220,223-362` 和迁移 0060、worker handler 已出现，但路由仍未调用 `request_purge`；未验证代次与历史事件脱敏。 |
| R22 开源记忆设计调研 | **文档产出存在**：`docs/MEMORY-OSS-RESEARCH.md:1-20` 覆盖拟人化写入、召回、睡眠、遗忘（本次工作树未跟踪）。 | 调研产物不是记忆行为改善；随提交确认来源与可追溯性。 |
| R23 注入正确性 | **边界已复现，不是"随机乱注入"**：候选 500 先截 `memory/service.py:559-574`；相对下限 + 保底 `kept or scored[:1]` 见 `:825-835`。实测（`_bm25_scores` 直接调用）：字面零命中的记忆**不会**进候选池（无 embedder 时只留 BM25>0）；但**弱相关的一定会**——只要它是唯一候选，`kept or scored[:1]` 就保底注入，不看绝对分。 | "无关记忆被注入"不是 bug 而是**相对阈值的固有天花板**：需要绝对分下限才能挡弱相关，而那会把"同义改写"也误杀。若要做，应先补 embedder 再谈阈值；当前无 embedder 的部署下这是已知取舍。 |
| R24 异步工具 | **预算闸已接通并修掉两个真 bug**（2026-11-10）：新增 `domains/media/budget.py`，在 `run_read_job` 调模型前做**持久**预留（复用 `behavior_model_calls`，`kind=video_watch`），五条退出路径（认领失败/租约丢失/取消/异常/成功）都结清，失败与 unknown 占额不退款。**次数限制彻底去掉**（`VIDEO_MAX_CALLS_PER_DAY is None`，`reserve_behavior_call` 支持 `max_calls=None`），只守 token 额度。独立 QA 复验抓出两处真回归并已修：(a) 预算闸原在 `claim_read_job` 之后，额度用尽时 `transition(running,budget_denied)` 必抛 `InvalidJobTransition` → outbox 重试 5 次 → 用户看到 `failed` 而非 `budget_denied`；闸已前移到认领之前走 `pending→budget_denied` 合法边；(b) `describer is None`（模型路由未就绪，正常路径）在 `_settle_budget` 定义之前早退，每次重排漏一条永久占额的 `reserved`，已把检查前移。证据：`tests/integration/test_media_read_execution.py::test_budget_denied_settles_without_raising`、`::test_missing_describer_leaks_no_reservation`，两者均已变异测试确认会红。 | 真实用量 `actual_tokens` 仍记 `None`（缺 usage 不虚填 0），要拿到 provider usage 才能填实数。 |

## B. 对抗性审查：需求、真实 bug 与误报

### R15/R16：全局槽位和值得等待吗

- **R15 是有条件的真实需求，不是高频事实**。单用户、单角色部署 `README.md:38` 不等于单会话：开发库有 277 个窗口；同步看视频在 `turn.py:1044-1049`，后台在 `worker.py:1205-1238`，后者的并发 1 不覆盖前者。
- 发言租约在 `turn.py:725-750` 只决定能否对外说，未阻止未持租约的轮次继续生成并调用 `_watch_videos`；因此“她同时在两个窗口看”可由并发请求触发，频率没有现网指标，不能称常见。
- 若交付目标是人格一致，R15 可保留为 P1；先用并发复现和日志频率证明收益。`docs/MEDIA-EXCLUSIVE-SLOT.md:112-113` 的 20 并发是**验收计划**，不是已实测结果。
- **R16 目前更像设计愿望**。当前同步路径会等读完再回复，`turn.py:1044-1073`；没有“稍后再说”的持久状态。设计若要她自己决定延后时长，须先定义取消、超时、跨会话到达时用户看到什么，不能凭槽位直接推导。

### R18：手机状态并非“已经会动”

- `PhoneService.start_watching/stop_watching` 只有实现及测试调用，`phone/service.py:210-255`、`test_phone_state.py:31-105`；生产代码检索没有调用。这是**测试覆盖死路径**，不是已上线功能。
- 当前开发库 `phone_states` 0 行；后端返回 `empty` `dashboard.py:584-599`，前端空态为“她还没拿起过手机” `DashboardPage.tsx:343-344`。故“用户一直看到手机放下了”在此库是误报；“正在看手机从未由生产路径触发”是真问题。
- 视频忙态不是手机态的同义词：`DashboardPage.tsx:305-329` 只认 `watching_app`/`watching_since`；即使接入媒体作业，也应展示真实活动来源与结束，不应伪装为看手机。

### R19/R20：重复与缺口在哪里

- **R19 近期重复摘要是伪需求，长期筛选是实需求**。观看结果已有 `media_evidence.description`，`watch.py:247-253` 会直接复用，`context.py:750-805` 下轮注入；再立刻总结一次只增加成本。
- 但 `described_in_window` 只取最近 `limit=3`，`media/service.py:332-357`、`context.py:746-752`；更早内容会退出提示。R19 的真正缺口是经过来源/受众/重要性筛选的长期沉淀和保留期，不是“再描述一遍”。
- `media_evidence.expires_at` 写入参数默认 `None`，`media/service.py:112-123,161-180`；`None` 在读侧被视为不过期 `:479-482`。因此现状也不满足“完整解析只留一小段时间”的生命周期要求。
- **另有真 bug**：`described_in_window` 不按 `kind` 过滤，`media/service.py:343-351`；上下文却把混入的视频统称“图片/语音”，`context.py:778-795`。看过的视频可能被错误标注，须单独修。
- **R20 已有弱形式压缩**：`memory.archive` 调 `compact_low_value()`，`worker.py:2673-2694`；因此“完全没有压缩”是误报。它按 60 天/同槽/低重要性工作 `memory/service.py:951-1021`，不在睡眠时触发，也不整合刚发生的情节；这才是剩余需求。

### R23：不相关记忆是否真的注入

- 相对阈值**不能判绝对相关**：`floor = top * relative_score_floor`、`kept or scored[:1]`，`memory/service.py:825-835`；测试 `test_floor_never_returns_empty_when_hit_exists` 明确要求“有候选就至少一条”，`test_memory_fusion.py:253-257`。
- 无向量时只保留 BM25 或实体命中，`memory/service.py:650-660`，所以“完全零字面命中也必注入”是误报；有向量时所有授权断言进入排序 `:650-676`，即使向量分很弱，仍可能出现相对最高的一条。
- 可复现的**机制反例**：给唯一候选一个低但非零语义分，`top>0` 时 `floor=0.35*top`，唯一候选必过；即使精排把所有命中打到 0，`kept or scored[:1]` 仍留首条。`_rerank_hits` 可把精排分替换 RRF 分，`memory/service.py:1346-1377`。
- 本次开发库 `memories` 25 行但 live=0，43 条 live episode；对其摘要用仓库 `_bm25_scores` 查询“量子纠缠”“火星探测器发动机”均 0 命中。这是**当前库无反例**，并非机制安全证明；尚无真实轮次“无关注入”的复现证据。
- 候选 500 是按 `updated_at` 先截、相关性后排 `memory/service.py:559-616`，旧重要条目可能被池外淘汰；`reinforce()` 更新注入次数而代码未显式写 `updated_at`，`service.py:1404-1412`。是否由 ORM/onupdate 刷新要看数据库行为，不能说“reinforce 不更新”；本次未写库复测。
- MMR 只筛断言 `service.py:708-718`，情节随后 `:736-814` 直接 append；情节无 SQL LIMIT `:738-746`。合并组只用于断言 `:548-565`，情节仍单 `subject_id` `:741-743`；开发库 `merged_into` 0 行，故跨合并漏召回未实测。
- 写入 `propose()` 仅同一 `turn_id+slot` 幂等，`service.py:439-456`，没有跨轮语义去重；开发库同时存在一条**总括性偏好记忆**与它的四条单项记忆（同一件事被写成 5 行；原文不在此转述）。43 条 live episode 中 25 条属于 7 个完全重复组，多 18 行（只读 GROUP BY）。

### R2/R10：性能与产品语义

- R2 原 n² DOM 病因已去：每张卡只有展开时才计算下拉，`IdentityPage.tsx:230-237`；首屏 50 张、0 `<option>` 的浏览器**模拟接口用例** `identity.browser.test.tsx:307-330`。未见首屏残留 n²。
- 若用户手动加载全部 2000 张，仍会渲染 2000 张卡，且每次展开会 `mergeCandidates(subjects)` 全量筛选排序 `IdentityPage.tsx:80-104,233-237`；约 O(n log n)，不是旧的 2000² options。真库 2000 条端到端时延未知。
- R10 是**产品取向，不应伪称客观修 bug**：群聊中“她右、群成员左”有一致性；网页私聊中“我”是操作者，行业常见“我在右”的期待也合理。`dialogue.test.tsx:509-553` 只证明当前 CSS，不能证明用户不会别扭；至少请真实私聊试用确认方向。
- 抽屉遮挡是客观回归，与方向审美无关：`prompt-entry` 只在 actor 下 `MessageList.tsx:610-620`，抽屉面板 `prompt-drawer.css:41-54` 吃指针，`PromptDrawer.tsx:465-472` 又把“再点入口”作为收起逻辑；入口落在面板内时事件永远到不了入口。

## C. 额外发现与交付风险

- **迁移链连续但版本戳不可信**：0054→0055→0056→0057→0058→0059→0060，`migrations/versions/0054_connector_config.py:48-49` 至 `0060_memory_purge.py:49-50`；`alembic heads` 当前 0060，开发库 `alembic_version` 却是 0047。
- 开发库已经有 `windows.pinned_at`、`messages.retracted_after_seen`、`connector_config_revisions`、`media_read_jobs`、`subjects.forget_generation`，与 0047 版本戳不符（`information_schema` 只读结果）。不能据此测试“从 0047 升到 head”；须用新库实跑迁移链，不能把当前库当干净基线。
- 0054–0059 均有 `downgrade()`，但 0054 删配置表、0058 删作业表、0055–0057/0059 删列 `migrations/versions/0054_connector_config.py:136-149`、`0058_media_read_jobs.py:167-185`、`0059_window_pinned_at.py:55-58`；这是**结构可回退、数据不可逆**。0056 的历史撤回值回填 false `:54-59` 也无法还原当时是否看过。
- **队列确有停滞迹象，但原因未证实**：开发库 `memory.extract` done=407，最后处理于 2026-10-29 12:20:55 UTC；10-31 新增 pending=24、attempts 全 0、均已到 available_at。`relationship.update` 同期也 pending=24、attempts=0，指向**共同消费链未运行/未认领**，不能据此判定提取模型坏。
- 这 24 条 `memory.extract` 的 `payload` 均含 `user_text`、`actor_text`，但新冗余 `outbox_events.subject_id` 均为 NULL（只读查询）；新 `request_purge` 只按该列取消，`purge.py:178-184`，所以**历史 pending 事件无法被请求阶段选中**。这是 R21 新方案的确定性缺口。
- 新 `purge_subject` 也按 `outbox_events.subject_id` 擦历史 done/dead/cancelled 的 payload，`purge.py:314-327`；旧事件该列为空，仍有原文。不能用“已写真删模块”覆盖这个事实；需要历史回填或安全地按 payload 归属筛选。
- 对旧事件的代次防护也失效：worker 将 `claim.subject_id` 绑定进栅栏 `worker.py:4626-4642`，而 `claim_still_valid` 遇空主体直接放行 `purge.py:430-442`；开发库这 24 条旧提取事件 `subject_id` 全空。
- **失败状态会随异常回滚**：`purge.py:357-362` 写 `tombstone.state='failed'` 后抛 `MemoryPurgeError`；调用它的 `session_scope` 在异常时 rollback，`infra/db.py:100-110`。因此“失败留墓碑”当前未实现，须独立事务落状态或改变异常边界。
- 真删时的并发新记忆也需定界：`purge_subject` 对该主体的 `memories`/`episodes` 整表删除 `purge.py:282-291`，而新轮次入队记的是新代次 `turn.py:1774-1803`；若清理与新写入并行，新内容是否应保留及锁边界尚未证明。
- 新清理逻辑 `purge.py:259-327` 只列部分派生表；`media_evidence.description`、知识项、提示词快照是否属于“用户要求的派生数据”须逐表定边界，并验证删除后无可检索原文；当前未见验收测试。
- **前端抽屉超时是产品回归，不是单纯测试问题**：`prompt.browser.test.tsx:308-339` 明测再点入口；按用户给的 1440px 命中实测，入口 x=1244..1338，抽屉 x=950..1440，`elementFromPoint` 命中 `prompt-drawer__body`。本次未重跑浏览器，几何证据与 CSS/事件逻辑一致。
- **标题采集不是死路径**：`sessions.py:187-190,222-233` 确实调到 `fetch_friend_names`；但按 channel 调一次整表 `:95-129`，需真实渠道测试备注变化、缓存到期及故障回退，不能用静态接线代替交付。
- **群窗口按好感度排序没有区分力**：`conversation/service.py:450-454` 明写群窗口 `subject_id` 是主人；多个群窗口同取主人的 familiarity，即使修复 R8 的传参，也会退化按活跃时间排。群排序需另定群级信号。
- **新 R8 还有 schema 运行风险**：代码模型已读取 `Window.pinned_at` `service.py:129-138`，但迁移 0059/0060 均未跟踪且开发库版本戳 0047；部署若只换代码不做真实迁移，会在列不存在的目标库上失败。
- **R13 的巨大人设预算可能挤压其他车道**：`context.py:120-145` 明定受保护车道、persona 100000；应验收真实模型上下文上限、费用及截断提示，而非仅看页面输入成功。
- **R24 并非完全死路径**：媒体 outbox handler 已注册且 `_media_worker_loop` 被 `run_forever` 启动，`worker.py:1062-1102,4687-4696`；`settle_dead_jobs` 也在 dead 分支接线 `:4550-4565`。旧“零调用方”说法在当前工作树已过时。
- **R24 仍有成本竞态**：`jobs.py:599-655` 只有认领、开始标记、执行；无 `behavior_model_calls` 的原子预算预留。同步视频仅查余额 `watch.py:124-185`；并发可以同时看到“还有额度”，需第 4 步验证。
- **设计文档与 R14 冲突**：`ASYNC-TOOL-DECISION.md:31-32` 的第 4 步仍要求“每窗口每小时 2 次、全局每天 20 次”，但 R14 已删次数限制 `video.py:60-74`；实现预算前先裁决究竟只管 token，还是重新引入次数闸。

## D. 交付前动作（按优先级）

| 优先级 | 必做动作与完成判据 | 为什么必须做 |
|---|---|
| P0 阻断交付 | 修 R8：先批量取好感度再排序，前端提供置顶/取消置顶；对私聊及群聊分别验收。证据：`sessions.py:222-233`、`service.py:357-361,450-454`。 | 现在 API 回了 familiarity，但顺序不按它排，用户可感知承诺失效。 |
| P0 阻断交付 | 完成 R21 后台真删入口、历史 outbox 清理及失败状态持久化；构造含旧事件的库验收“请求后不可见、后台后不存在”。证据：`manage.py:272-283`、`purge.py:178-184,314-327,357-362`、开发库 24 条 subject_id=NULL。 | 当前软删留原文，新模块漏旧队列，且失败状态会被事务回滚。 |
| P0 阻断交付 | 以干净 0047 快照升级到当前 head，并以迁移后读写烟测 R6/R8/R11/R12/R24；核实最终 head 与提交文件一致。证据：当前 `alembic current=0047`、`heads=0060`，但当前库已有后续表列。 | 开发库版本戳与 schema 分叉，现有测试无法证明升级过程可部署。 |
| P0 阻断交付 | 修 `prompt-entry` 被右抽屉遮挡，并在 1440/375 真 Chromium 跑 `prompt.browser.test.tsx:308-339`，确认测试未 skip。 | 用户可见“再点入口收起”失效，已有命中测试与 CSS 路径相互印证。 |
| P1 应当修 | 把 R6 的后端可编辑字段、保存/回滚/失败状态做成前端表单；证据 `settings.py:847-1034` 对比 `ConnectorsPage.tsx:223-260`。 | 原始诉求就是“点进去能配置”，目前界面还告诉用户去改 `.env`。 |
| P1 应当修 | 给 R4 前端展示回退来源；验证未生成日程、读取超时、睡眠态三个用户场景。证据 `dashboard.py:574-607` 对比 `DashboardPage.tsx:305-333`。 | 静默回退虽能运行，却让用户误以为日程驱动正在生效。 |
| P1 应当修 | 查 worker 是否实际运行、为什么 10-31 后 24+24 件均零次认领；观察消费日志和只读队列状态，恢复后确认 backlog 消退。证据：开发库 outbox 分组查询、`worker.py:388-511,4687-4696`。 | 记忆与关系均不再更新；不能把“提取代码存在”当服务正常。 |
| P1 应当修 | 给 R23 做可重复的无关查询、500 池外旧事实、重复情节、合并主体测试，并对绝对相关性设可为空门槛；证据 `memory/service.py:559-574,708-835`。 | 注入错误会直接改变她的回答，现有测试反而保护“至少一条”的行为。 |
| P1 应当修 | 修已看视频被写成“图片/语音”的媒体车道，区分来源；证据 `context.py:778-805`、`media/service.py:343-351`。 | 角色可能把视频内容说成看图所得，破坏事实来源。 |
| P1 应当修 | 对 R14/R24 后台视频接数据库预算预留，做同步+异步并发超额验收；证据 `chat.py:536-540`、`jobs.py:599-655`。 | 第 4 步未做时无法保证并发成本上限。 |
| P2 可以后置 | R15 先收集并发冲突频率，再决定跨进程全局槽；R16 先定“等待/稍后回复”产品语义。证据 `turn.py:1044-1073`、`docs/MEDIA-EXCLUSIVE-SLOT.md:112-113`。 | 真实可触发但高复杂度，当前缺频率与等待体验证据。 |
| P2 可以后置 | R17、R10 找真实私聊样本做选择权与左右方向体验验收；证据 `context.py:807-860`、`dialogue.test.tsx:509-553`。 | 单靠标记和 CSS 测试无法裁决拟人化意愿与用户方向感。 |
| P2 可以后置 | R18 接真实活动态，不复用死的手机 API 冒充视频；R19 只做筛选后的长期沉淀；R20 按睡眠事件触发新增工作。证据 `phone/service.py:210-255`、`context.py:750-805`、`worker.py:2673-2694`。 | 这些是体验增强；当前已有状态/描述/弱压缩，应先避免重复实现。 |

## 只读复核清单

- 数据查询口径：`SELECT version_num FROM alembic_version`；按 `topic,state` 聚合 `outbox_events`；按 `subject_id,summary` 聚合 live `memory_episodes`；按 `merged_into IS NOT NULL` 计数 `subjects`；按 `archived_at IS NULL AND retracted=false` 计数 `memories`。
- 本次结果：`memories` 25 行、live 0；live episode 43 行、重复涉及 25 行/7 组/多 18 行；`subjects.merged_into` 0；`core_memories` 0；`knowledge_items` 0；`media_evidence` 仅 4 张 image，无 video；`phone_states` 0。
- 未运行 pytest 或浏览器测试：用户限定只读代码与真库，现有测试可能写库/临时文件；测试名仅作为仓库已有覆盖线索，不记作本次通过。

## E. 按用户可感知行为复核的最小场景

以下是**尚未执行的交付验收场景**，不是测试通过记录。每项都绑定现有入口或风险代码，供实现者修复后逐项复验。

### 模型、设置、总览

1. 首次打开模型设置，未点击“更换默认”直接改槽位；保存、刷新后值保持。依据：`RolesPanel.tsx:193-225`、`model-settings.test.tsx:603-642`。
2. 同一槽位列出两家供应商的模型，分组标签与禁用状态准确。依据：`RolesPanel.tsx:209-225`、`model-settings.test.tsx:615-642`。
3. 模型尚未配置时，选择器应给可操作的说明，而非空白。依据：`RolesPanel.tsx:167-174,193-225`。
4. 1440px 与 375px 打开设置页，分别两列和一列，卡片不横向溢出。依据：`settings.css:163-166,355-360`。
5. 总览在 1440px、900px、375px 验证卡片边界、文字折行、无横向滚动。依据：`dashboard.css:1-68`。
6. 日程尚未生成时打开总览，手机节奏应标“默认参数”及原因。依据：`dashboard.py:574-599`、`DashboardPage.tsx:305-333`。
7. 日程读取超时但手机卡仍可用，应把“上游不可用”呈现给人。依据：`dashboard.py:115-137,574-583`。
8. 接入器页保存端口/凭据后回读生效状态；当前 UI 无输入/PUT 路径。依据：`ConnectorsPage.tsx:223-260`、`settings.py:847-1034`。
9. 接入器失败回滚后用户应看到旧连接仍有效，而非只提示 `.env`。依据：`settings.py:856-861`、`SettingsHub.tsx:248-257`。
10. 人设贴近 10 万 token 时以目标供应商实发一轮，并查看 prompt snapshot 的真实截断。依据：`context.py:123-132,1209`。

### 关系、会话列表与标题

11. 2000 人关系页首次打开只请求 50 条、DOM 不超过测试设定阈值。依据：`identity.browser.test.tsx:307-330`、`manage.py:1212-1287`。
12. 连续点击“加载更多”直至 2000 人，记录每页耗时、DOM 数、内存；现有 120 人翻页测试不足。依据：`identity.browser.test.tsx:402-427`。
13. 在第 40 页找合并目标，服务端搜索应定位目标，不能只搜本地页。依据：`IdentityPage.tsx:80-104,135-170`、`identity.browser.test.tsx:429-443`。
14. 展开第 2000 张卡时只存在一张下拉；候选总量随已加载页增长但不回到 n²。依据：`IdentityPage.tsx:230-237`。
15. 两个不同好感度的私聊窗口，未置顶时高者应在上；当前参数时序保证会失败。依据：`sessions.py:222-233`、`conversation/service.py:357-361`。
16. 置顶 A、置顶 B、取消 B，检查顺序、刷新保持；当前前端无 pin 入口。依据：`sessions.py:244-275`、前端 `api.ts` 无 `/pin` 调用。
17. 两个群窗口的 `subject_id` 同指主人时，明确显示/解释按活跃时间排，而非伪称有不同好感度。依据：`conversation/service.py:450-454`。
18. 私聊好友备注、昵称、库内名同时存在时显示备注；备注为空退昵称。依据：`sessions.py:132-166`、`connector.py:1253-1267`。
19. OneBot `get_friend_list` 超时或未实现时，会话列表仍正常返回。依据：`connector.py:1234-1251`、`sessions.py:124-129`。
20. 群名采集失败时显示明确的群号回退，不应误用主人名。依据：`sessions.py:170-190`、`test_session_display_fields.py:164-177,242`。

### 对话、撤回与抽屉

21. Web 私聊请操作者辨别“我/她”气泡；方向正确性以用户测试裁决。依据：`dialogue.test.tsx:509-524`。
22. 群聊至少三名发言者时，其他人都在左，角色在右，名字/QQ 号仍可辨。依据：`MessageList.tsx:171,473-483`、`test_session_display_fields.py:260-271`。
23. 1440px 打开右侧 prompt 抽屉，再点同一 actor 消息入口应收起。依据：`prompt.browser.test.tsx:308-339`、`prompt-drawer.css:41-54`。
24. 375px 下抽屉全宽时，不能声称“再点入口”可用；应以关闭键/Esc 等明确出口验收。依据：`prompt-drawer.css:351-380`、`PromptDrawer.tsx:475-479`。
25. 消息撤回发生于已看前后两种时序，回放和 UI 结果须分别验收。依据：`conversation/service.py:972-1032,1135-1165`。
26. LLM 只能提议撤回她自己的已送达消息；四态确认不能绕过。依据：`capabilities/tools.py:218-281`、`test_recall_tool.py:401-451`。
27. 后台撤回失败时卡片应显示真实状态而非假装已撤；依据：`recall.browser.test.tsx:300-308,431`。
28. 旧库升级后对历史 `retracted_after_seen=NULL` 应核实“保守按未看”是否符合用户预期。依据：`0056_recall_after_seen.py:54-59`。

### 日程、回退与手机

29. 无日程时手动点击“生成今天的日程”，观察 job 从排队到成功/失败的完整可见状态。依据：`LifeTimeline.tsx:1070-1151,1227-1288`。
30. 已有日程点“重新生成”需先确认，并在后端失败时保留旧安排。依据：`LifeTimeline.tsx:1227-1288,1399-1438`。
31. 当日计划处于 degraded 时仍允许“重新生成”，而非禁用入口。依据：`LifeTimeline.tsx:921-928`。
32. 上游可用、未就绪、超时各一次，对比手机间隔来源与状态。依据：`phone/service.py:338-342`、`dashboard.py:574-607`。
33. 当前手机 UI 若无状态行，必须显示“还没拿起过”，不能误显示“手机放下了”。依据：`dashboard.py:584-599`、`DashboardPage.tsx:335-345`。
34. 真正接入看手机动作时才允许出现 `watching`；目前生产零调用者。依据：`phone/service.py:210-255`、全仓生产调用检索结果。
35. 在看手机期间不排下次时间，停止后从停止时重新算。依据：`phone/service.py:218-255`、`test_phone_state.py:31-63`。
36. 视频忙态应有独立生命周期，不应仅靠 `phone_states` 的旧字段推断。依据：`DashboardPage.tsx:305-329`、`MEDIA-EXCLUSIVE-SLOT.md:61-69`。

### 视频：意愿、额度与并发

37. 给角色一个可看的视频但不要求观看，观察她有权不写标记；记录真实模型输出。依据：`context.py:807-860`。
38. 角色写 `<<看视频 m…>>` 时才调用读取；未写时 token 用量不得增加。依据：`turn.py:1044-1049`、`video.py:47-48`。
39. 缓存命中直接复用描述，不重新调用视频模型。依据：`watch.py:247-253`、`test_video_watch.py:314-321`。
40. 视频职责绑定与图片职责绑定不同供应商时，应命中视频那一家。依据：`watch.py:373-395`、`test_video_watch.py:174-184`。
41. 额度耗尽时不声称看过，也不能谎报“打不开”。依据：`turn.py:1474-1483`、`test_video_watch_quota.py:161-199`。
42. 视频引用不存在时与额度耗尽时文案不同，且都不能编造内容。依据：`watch.py:239-245,256-280`。
43. 两个窗口同时触发同步看视频，记录实际模型调用重叠数；当前无全局槽。依据：`turn.py:1327-1355`、`MEDIA-EXCLUSIVE-SLOT.md:10-16`。
44. 同步看视频与后台 `media.video.read` 同时运行，也应测是否重叠。依据：`worker.py:1204-1238`、`turn.py:1327-1355`。
45. 一个后台作业进入 dead 时，业务 job 应同步终结为可见终态。依据：`worker.py:4544-4557`。
46. 无新请求的 worker 清理循环不得发模型调用。依据：`worker.py:1178-1200`、`ASYNC-TOOL-DECISION.md:31-33`。
47. 用真实额度并发触发同步和异步读取，验证预算预留阻止超额；当前尚无该机制。依据：`jobs.py:599-655`、`chat.py:536-540`。
48. 视频被描述后，下轮提示应称“视频”，当前 `context.py:793-795` 会称“图片/语音”。依据：`media/service.py:343-351`。

### 记忆召回与保留

49. 造 501 条授权断言，最旧一条高度相关，验证是否被 `updated_at` 候选池排除。依据：`memory/service.py:559-574`。
50. 强化旧记忆后复读 `updated_at`、`access_count`，先确定 onupdate 的真实行为。依据：`memory/service.py:1391-1413`。
51. 只给一个弱语义候选，提问无关主题，检查是否仍注入首名。依据：`memory/service.py:650-676,825-835`。
52. 无 embedder 且无 BM25/实体命中时应返回空，不要误报“一定注入”。依据：`memory/service.py:650-660`。
53. reranker 对所有候选给零分时应允许空结果；现状保底首名。依据：`memory/service.py:1346-1377,825-835`。
54. 断言和情节内容完全相同的窗口，检查是否各注入一次。依据：`memory/service.py:708-718,736-814`。
55. 单主体 1 万条 episode 复测查询量/延迟；当前 SQL 无 LIMIT。依据：`memory/service.py:738-746`。
56. 合并 A→B 后在 A 窗口问 B 的 episode，应读到；现状仅查 A。依据：`memory/service.py:553-565,741-743`。
57. 对同一偏好不同轮次提取，防止“总括+四单项”多次写入。依据：`memory/service.py:439-456`、开发库五条偏好文本。
58. 验证 7 组重复 episode 的来源 turn 是否同一重放，避免误归因给提取模型。依据：开发库 live episode 重复组查询；本次未比对 `turn_id`。
59. `core_memories` 与 `knowledge_items` 目前均 0，不能用空表断言检索质量。依据：开发库两表 `COUNT(*)=0`。
60. 过期与撤回内容不得进提示，且旧证据仍需可审计；依据：`memory/service.py:563-605,1431-1486`。

### 真删、迁移和运行

61. 删除请求必须使用新 `request_purge`，不能继续调用软清除。依据：`manage.py:272-283`、`purge.py:145-220`。
62. 删除前建一条 `outbox_events.subject_id=NULL` 的旧 `memory.extract`；请求后它必须失效且 payload 不留原文。依据：开发库 24 条、`purge.py:178-184`。
63. 已 done 的旧事件同样需擦正文；按空 subject_id 的当前更新不会命中。依据：`purge.py:314-327`、开发库 done 407 条。
64. 已在途的旧提取任务须用代次栅栏阻止落库；当前空 subject_id 被视为有效。依据：`purge.py:430-442`、`worker.py:4626-4642`。
65. 人为造成清理 SQL 失败后刷新墓碑状态，必须能看见 failed；当前异常回滚。依据：`purge.py:357-362`、`infra/db.py:100-110`。
66. 同一 subject 同时提交两次删除请求，验收代次递增 2；当前 `session.get` 后赋值无显式行锁。依据：`purge.py:162-170`。
67. 清理完成后查 memories、episodes、core、awareness、embeddings、entities、retrieval logs、outbox 原文。依据：`purge.py:259-327`。
68. 从纯 0047 schema 逐步升级 0054–0060，确保每个对象恰好创建一次。依据：`alembic current=0047`、`heads=0060`、各迁移 `down_revision`。
69. 对 0054/0058/0059 做有数据的降级演练，只能宣称 schema 可回退，不能宣称内容恢复。依据：`0054_connector_config.py:136-149`、`0058_media_read_jobs.py:167-185`、`0059_window_pinned_at.py:55-58`。
70. 检查 24+24 件 pending 是否在 worker 恢复后开始认领；若仍 attempts=0，先查调度/连接/版本，再查提取器。依据：开发库 outbox 分组、`worker.py:4653-4731`。

## D. 2026-11-10 补做与当前未完成项

第一轮审查后补掉的三项，以及仍然明确未做的项。**没做完的写没做完**，不用"已有设计"冒充交付。

### 已补完

| 项 | 证据 |
|---|---|
| **R6 接入器可编辑** | 表单接 `PUT /v1/settings/connectors/{scope}`；`rolled_back`（HTTP 202）单独文案且不含"已生效"；密钥恒空、清空走 `clear`；`src/connectors.browser.test.tsx` 11 项真 Chromium |
| **R15 全局媒体槽** | `media_slots` + `0061_media_slot` + `domains/media/slot.py`；20 并发抢占恰好 1 个成功；代次栅栏让旧持有者写入落空；`test_default_watch_callback_enforces_the_slot` 钉住生产装配。**容器内验证**：NAS 上 `postgres:16-alpine` 容器（PG 16.15）跑完整迁移链到 `0061_media_slot`、预置行正确、`downgrade -1`→`upgrade head` 往返成功，10 条槽位用例跨网络全部通过 |
| **R4 回退来源可见** | `FallbackHint` 按 `fallback.used/source/reason` 明说"这是默认参数，不是她的真实状态"；`src/dashboard-fallback.browser.test.tsx` 5 项（1440/375 无溢出） |

### 仍未完成（不掩饰）

| 项 | 现状 | 缺什么 |
|---|---|---|
| **R16 可等待/自主决定回复时机** | **未实现**。`docs/MEDIA-WILLINGNESS-DESIGN.md` 有完整设计（`DeferredReply` + 到期新轮次 + 新输入取消旧投递） | 持久化的延后回复载体与调度；且与 R15"忙即拒绝、不排队"需要产品裁决先后 |
| **R18 忙态真实活动** | **已补完**（见上表） | — |
| **R19 视频摘要长期沉淀** | **已补完**（见上表）：`sediment.py` 在每日归档里把将过期的"真看过"证据沉降成记忆，五个筛选条件、幂等、带来源 | 模型浓缩摘要（须独立"非事实"存储） |
| **R20 睡眠期记忆压缩** | **已补完**（见上表）：`life.schedule` 按预计入睡 +30 分钟投 `memory.consolidate`，一晚一次、按人、零模型；`memory_consolidations` 留痕 | 模型摘要（须独立"非事实"存储） |
| **R17 她有权不看** | **机制成立、未验证**。上下文已给她"想看才写标记"的选择 | 真实模型对话样本，证明她确实会拒绝而不是总被诱导 |
| **R24 异步工具预算** | **已补完并修掉 QA 发现的两个真回归**（见上表）：预算闸前移到认领之前（`pending→budget_denied` 合法边）、`describer is None` 检查前移消除 `reserved` 泄漏、五条退出路径全结清、次数限制彻底去掉 | `actual_tokens` 待接 provider usage |

### 本轮新发现、未修的后端小问题（如实记录）

1. `dashboard.py:324` 的 `describe` **一词两义**：它本意是"可用性说明"，却在场景 unknown/过期时补一句"状态正常，可以随时说话"，读起来像在说她的场景正常。前端只能把两块分开讲，语义应由后端用独立字段表达。
2. `presence.resolve_scene()` 在"从没提交过场景"（最常见的默认状态）时也会 `log_fallback(consumer="presence.scene")`，把正常空状态记成 `upstream_fallback` 行为日志。与 `fallback.py` 里"上游为空是正常状态"的取向不一致，日志容易被刷满。
