# 出站投递调研：AstrBot / MaiBot / OneBot v11，以及 ling 的修复清单

> 调研时间：2026-11-08。所有外部结论都钉在具体 commit 上，链接形如
> `.../blob/<sha>/<path>#Lx`；查不到的写「未证实」，不做推断式断言。
>
> 阅读顺序建议：**§0 结论** → **§5 修复清单**（要动手的部分）→ 其余当依据。

被调研的仓库快照：

| 仓库 | commit |
| --- | --- |
| `AstrBotDevs/AstrBot` | `86c24e846c0de78830f6a0bc402049edb0f0c671` |
| `MaiM-with-u/MaiBot` | `8e18e306436101cf890cecff489f59675f48baea` |
| `MaiM-with-u/MaiBot-Napcat-Adapter` | `d87203e2eff1835895ee34196b17a571dc6bbe24` |
| `NapNeko/NapCatQQ` | `98d781d19815d1625b24d717ce280ecf0a4f6d0c` |
| `botuniverse/onebot-11` | `d4456ee706f9ada9c2dfde56a2bcfc69752600e4` |
| `nonebot/aiocqhttp`（AstrBot 的 QQ 传输层） | `2520928d2373f5be71059600b832e230a39ccdb9` |

---

## 0. 结论

### 0.1 三处最大的差异（AstrBot / MaiBot 相对 ling）

1. **"引用谁"是真实 ID，不是序号。**
   - AstrBot 直接把入站事件里的平台 id 塞进 reply 段：`Reply(id=event.message_obj.message_id)`，
     而 `message_obj.message_id = str(event.message_id)`（[result_decorate/stage.py#L447](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/result_decorate/stage.py#L447)、
     [aiocqhttp_platform_adapter.py#L228](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L228)）。
   - MaiBot 不仅有真实 id，还把它**贴在每条消息前面**：上下文里每条消息渲染成
     `[msg_id:<短别名>] 内容`（长 id 缩成 `m` + 6 位 base32，NapCat 这种短数字 id 原样显示），
     模型调工具时再按别名映射换回原始 id
     （[message_adapter.py#L35-L40](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/context/message_adapter.py#L35-L40)、
     [reasoning_engine.py#L1500-L1510](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/reasoning_engine.py#L1500-L1510)、
     [message_id_alias.py#L1-L50](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/context/message_id_alias.py#L1-L50)）。
   - ling 的提示词里写的是 `[quote:消息序号]`，而上下文（`{name}：{content}`）**从来没给过任何 id**
     （[../src/ling/capabilities/expression_choice.py:102](../src/ling/capabilities/expression_choice.py#L102)、
     [../src/ling/kernel/context.py:1198](../src/ling/kernel/context.py#L1198)）。
     这一条是"整条拒收 / 引用静默消失"的病根。

2. **分段发送有一等公民的"段身份"，而且失败不毁掉已送达的段。**
   - MaiBot：每段一次 `send_service._send_to_target_with_message(...)`，逐段记录
     `sent_message_ids` / `send_results`，失败就 `break`（不重试、不重发前面的段），
     后续段还可以"引用上一段"（上一段的真实回执 id）（[reply.py#L807-L886](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/builtin_tool/reply.py#L807-L886)、
     [reply.py#L333-L346](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/builtin_tool/reply.py#L333-L346)）。
   - MaiBot 还有**在途幂等**：同一条内部消息 + 同一个 driver 正在发时，第二次
     `begin_tracking` 直接抛错（[outbound_tracker.py#L140-L146](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/platform_io/outbound_tracker.py#L140-L146)）。
   - ling 的 `_deliver` 已经把 `reply_plan` / `part_ordinal` 写进 operation payload
     （`src/ling/kernel/bridge.py:1124-1128`），**数据和幂等只差一步**：没有任何代码按它去重。
     而 `src/ling/kernel/inbound_intake.py:279-284` 在"部分送达"时也会 `release_event`，
     于是重投 = 重新生成 + 从第 1 段再发一遍。

3. **对"不确定"的处理方式不同：一个不承认有这种状态，一个把它当查询题。**
   - AstrBot：`aiocqhttp` 只在 `status == "failed"` 时抛 `ActionFailed`
     （[api_impl.py#L28-L39](https://github.com/nonebot/aiocqhttp/blob/2520928d2373f5be71059600b832e230a39ccdb9/aiocqhttp/api_impl.py#L28-L39)），
     适配器层既没有重试也没有"已发送但未确认"的状态，失败就 `logger.error` 然后**继续下一段**
     （[respond/stage.py#L340-L368](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/respond/stage.py#L340-L368)）。
     它连 `message_sent` 回显都不处理（全仓只有 hook 名字里带 `message_sent`）。
   - MaiBot 的出站状态只有 `pending / sent / failed / dropped`，**没有 uncertain**
     （[types.py#L24-L31](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/platform_io/types.py#L24-L31)），
     但适配器把 `external_message_id` 回传并建"外部 id → 回执"索引
     （[outbound_tracker.py#L150-L190](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/platform_io/outbound_tracker.py#L150-L190)），
     超时/异常一律记 failed、**绝不自动重发**（[plugin.py#L232-L310](https://github.com/MaiM-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/plugin.py#L232-L310)）。
   - ling 已经把 `uncertain` 建模得很干净（`DeliveryState.uncertain` + `query_receipt`），
     **但 `reconcile()` 没有生产调用方**（只有 `tests/integration/test_delivery.py:210`），
     所以这个状态在线上等于"永久悬空"。

### 0.2 最该先修的三件事

1. **段级幂等 + 部分送达不再释放认领**（P0-1）。不修这条，任何重投都会重复第 1 段。
2. **`uncertain` 落进周期对账**（P0-2）。`DeliveryService.reconcile()` 已经写好了，
   缺的是一个 `PERIODIC_JOBS` 入口和"没有 platform_message_id 时怎么查"的兜底。
3. **把真实 message_id 放进上下文，`[quote:序号]` 从提示词里删掉**（P0-4）。
   顺带纠正一个诊断：在 NapCat 上无效 reply **不会**整条拒收，只会**静默丢掉引用段**（§4.4），
   ling 现在那条"retcode=100 → 去掉引用重发"的分支在 NapCat 上大概率**从未触发**。

---

## 1. ling 现状基线（对照用）

| 关注点 | 位置 | 现状 |
| --- | --- | --- |
| 发送 | [../connectors/onebot/connector.py:92-179](../connectors/onebot/connector.py#L92) | `send_group_msg` / `send_private_msg`，回执取 `data.message_id` |
| 装饰段降级 | [../connectors/onebot/connector.py:127-142](../connectors/onebot/connector.py#L127) | 仅当 `ChannelErrorKind.rejected` 时去掉 reply/@ 重发一次 |
| 错误分类 | [../connectors/onebot/connector.py:260-276](../connectors/onebot/connector.py#L260) | `retcode==0 and status!="failed"` 成功；`async`/`retcode==1` → uncertain；**其余全部 rejected** |
| 对账 | [../connectors/onebot/connector.py:181-208](../connectors/onebot/connector.py#L181) | `get_msg`；查不到 → uncertain（正确，但没人调） |
| 分段 | [../src/ling/capabilities/chunking.py:273-331](../src/ling/capabilities/chunking.py#L273) | 群聊 1 段；私聊按换行/语义切，按渠道硬上限兜底 |
| 投递编排 | [../src/ling/kernel/bridge.py:1057-1178](../src/ling/kernel/bridge.py#L1057) | 每段一条 Operation + 回执；某段失败 `break` |
| 认领释放 | [../src/ling/kernel/inbound_intake.py:279-284](../src/ling/kernel/inbound_intake.py#L279) | `reply_text and not delivered` → **释放认领**（含部分送达） |
| 超时 | [../src/ling/kernel/bridge.py:1005-1010](../src/ling/kernel/bridge.py#L1005)、[../src/ling/worker.py:1326-1330](../src/ling/worker.py#L1326) | 180 s `wait_for` 超时 → `break` / 返回失败，正文丢弃 |
| 引用来源 | [../src/ling/capabilities/expression_choice.py:95-107](../src/ling/capabilities/expression_choice.py#L95) | 提示词 `[quote:消息序号]`，无 id 可给 |
| 回显 | [../connectors/onebot/connector.py:349-375](../connectors/onebot/connector.py#L349) | `message_sent` 按 `ignored` 丢弃，**不补写回执**（这条判断是对的） |
| 周期任务 | [../src/ling/worker.py:3628-3651](../src/ling/worker.py#L3628) | `PERIODIC_JOBS` 是唯一合法调度入口，并有闸门测试 |
| 现成幂等表 | [../src/ling/infra/models.py:472-484](../src/ling/infra/models.py#L472) | `IdempotencyKey(scope,key)` 唯一，目前只用于入站 |

---

## 2. A. AstrBot 的发送链路

> 传输层是 `nonebot/aiocqhttp`（WS 反向/正向 + `echo` 配对），适配器目录
> `astrbot/core/platform/sources/aiocqhttp/`（**没有独立的 napcat 适配器**；
> `k8s/astrbot_with_napcat` 只是部署清单）。全文结论按"aiocqhttp"路径成立。

### A1. 一条回复怎么变成 OneBot 调用；长文本会不会拆分

链路：`LLM/插件产出 MessageChain` → `ResultDecorateStage`（清洗、分段、@/引用装饰、超长折叠）
→ `RespondStage`（逐段 `event.send()`，段间 sleep）→ `AiocqhttpMessageEvent.send`
→ `send_message` → `_dispatch_send` → `bot.send_group_msg / send_private_msg`
（若消息里带 `Node/Nodes/File` 则退化成**逐段单发** + `send_*_forward_msg`）。

拆分规则有**三条，彼此独立**：

1. **分段回复（句子级）**：仅当某个 `Plain` 段 **`len <= words_count_threshold`（默认 150）** 时才切；
   超过 150 字**不切**，整段发（[result_decorate/stage.py#L205-L250](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/result_decorate/stage.py#L205-L250)，
   默认值 [config/default.py#L116](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/config/default.py#L116)）。
   切法两种：`split_mode="regex"`（默认 `.*?[。？！~…]+|.+$`）或 `"words"`
   （按 `split_words` 词表，实现见 [stage.py#L104-L123](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/result_decorate/stage.py#L104-L123)）。
2. **超长折叠成合并转发**：`Plain` 字数合计 > `forward_threshold`（默认 **1500**）时，
   整条链塞进一个 `Node`，走 `send_group_forward_msg`（[stage.py#L414-L422](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/result_decorate/stage.py#L414-L422)、
   [default.py#L101](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/config/default.py#L101)）。
3. **按"消息段"逐条**：链里出现 `Node | Nodes | File` 时，逐段单独发，段间 `await asyncio.sleep(0.5)`
   （[aiocqhttp_message_event.py#L153-L208](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L153-L208)）。

**关键差异（对 ling 有用）**：AstrBot **没有**"按平台字符上限切"的概念。QQ 侧不存在
"4000 字切成 N 条"的逻辑；它靠的是"短则按句切、长则合并转发"。
对 ling 的启示：`plan_parts(max_part_chars=4000)` 那一层是 ling 特有的**物理保险**，不要删；
但"长回复"更好的做法（AstrBot 已验证可行）是**折叠成合并转发**而不是切成 5 条刷屏。

### A2. 发送失败怎么处理：没有重试、没有退避、没有队列、只有"段间随机停顿"

- 适配器层：`_dispatch_send` 直接 `await bot.send_group_msg(...)`，无 try/except、无重试
  （[aiocqhttp_message_event.py#L117-L150](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L117-L150)）。
- 编排层：`RespondStage` 的逐段循环里 `try/except` **包住每一段**，
  失败只 `logger.error("Failed to send the message chain")`，**循环继续**（后续段照发）
  （[respond/stage.py#L340-L368](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/respond/stage.py#L340-L368)）。
- **"限速"= 每段发送前的 random sleep（含第 1 段，所以首条会凭空晚 1.5–3.5 s）**：
  `interval` 默认 `1.5,3.5`（秒），或 `interval_method="log"`：
  `y = log_base 底的对数(字数)`，再 `random.uniform(i, i+0.5)`
  （[respond/stage.py#L100-L133](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/respond/stage.py#L100-L133)，
  默认值 [default.py#L114-L115](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/config/default.py#L114-L115)）。
  流式回退路径另有硬编码 `sleep(1.5)`
  （[aiocqhttp_message_event.py#L246-L261](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L246-L261)）。
- 传输层：`aiocqhttp` 的 `call_action` 用 `echo:{seq}` + `ResultStore.fetch(seq, timeout)` 等回执，
  超时抛错；`_handle_api_result` **只在 `status=="failed"` 时**抛 `ActionFailed`
  （[api_impl.py#L129-L155](https://github.com/nonebot/aiocqhttp/blob/2520928d2373f5be71059600b832e230a39ccdb9/aiocqhttp/api_impl.py#L129-L155)、
  [api_impl.py#L28-L39](https://github.com/nonebot/aiocqhttp/blob/2520928d2373f5be71059600b832e230a39ccdb9/aiocqhttp/api_impl.py#L28-L39)）。
  → **`status:"ok"` + `retcode:100` 会被 AstrBot 当成发送成功**。这正是 ling 在
  `connector.py:262-268` 修掉的那个坑，AstrBot 至今没修。
- 没有发送队列：出站是 `await` 直连，没有 outbox / 补发。

### A3. 去重 / 幂等

**没有平台级幂等，也没有段级幂等**。只有两处"弱去重"：

1. `RespondStage` 的**文本相等**闸：本轮若已经通过 `send_message_to_user` 工具发过同样的纯文本，
   最终回复就跳过（[respond/stage.py#L207-L233](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/respond/stage.py#L207-L233)，
   记录点在 [message_tools.py#L344-L363](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/tools/message_tools.py#L344-L363)）。
   纯文本比较，不做归一化，跨轮无效。
2. `event.set_extra("_streaming_finished")` 防"流式收尾后又按普通结果再发一次"
   （[respond/stage.py#L200-L206](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/respond/stage.py#L200-L206)）。

不处理 `message_sent` 回显（`grep message_sent` 只命中 hook 名），所以**没有**"按平台 id 去重"的能力。

### A4. 引用回复

- 填的是**入站事件的平台 `message_id`**：`Reply(id=event.message_obj.message_id)`，
  而 `abm.message_id = str(event.message_id)`（[stage.py#L446-L447](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/result_decorate/stage.py#L446-L447)、
  [aiocqhttp_platform_adapter.py#L228](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L228)）。
- 序列化就是 OneBot 标准段 `{"type":"reply","data":{"id":str(id)}}`
  （[components.py#L725-L752](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/message/components.py#L725-L752)）。
- **只有第一段带引用**：分段发送时把 `Reply/At` 抽成 `header_comps`，第一段带上后立刻 `clear()`
  （[respond/stage.py#L344-L365](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/respond/stage.py#L344-L365)）。
- **入站侧反倒做了"引用内容的补齐"**：收到 `reply` 段时先 `get_msg` 取被引消息，
  再用它构造带 `chain/sender/time/message_str` 的 `Reply`；
  `get_msg` 失败就降级成裸 `Reply(id=...)`（[aiocqhttp_platform_adapter.py#L305-L341](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L305-L341)）。
- 出站**没有**"引用失效就降级为普通发送"的显式逻辑——它靠的是 NapCat 侧的静默丢弃（§4.4）。

### A5. 长消息 / 富媒体

- 图片/语音在出站前被转成 **base64**（`_from_segment_to_dict`）；
  `File`/`Video` 走 `to_dict()`（[aiocqhttp_message_event.py#L38-L70](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L38-L70)）。
  base64 内联的代价是单条报文可能极大（未做体积上限，**未证实**有保护）。
- 合并转发/文件**不能与普通段混发**，所以含 `Node|Nodes|File` 时整条链退化为"逐段单发"
  （同上 L172-L208）。这类退化**不保证原子性**：第 3 段失败，前 2 段已在群里。
- 长文本见 A1（>1500 字折叠转发；否则一条直发）。

### A6. 有没有"已发送但不确定"

**没有。** AstrBot 的失败就是 `logger.error`，无状态机、无补偿、无对账。
`get_msg` 只用于**入站引用补全**（[quoted_message/extractor.py#L120-L140](https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/utils/quoted_message/extractor.py#L120-L140)），
不用于"这条到底发出去没有"。

---

## 3. B. MaiBot（麦麦）的发送链路

> **架构前提**：MaiBot 主进程**不直接说 OneBot**。它通过 `maim_message` 把消息发给独立适配器进程
> （`MaiBot-Napcat-Adapter`），适配器再对 NapCat 讲 OneBot v11（正向 WS + `echo`）。
> 所以"QQ 侧怎么发"要同时看两边。

### B1. 主进程：分段、逐段发送、每段一条回执

- 分段在生成后做：按句子切（`_split_into_sentence_segments`），
  且"中文占比高且长度 > `max_length*2`（默认 512×2）"时**直接换成一句随机默认回复**
  ——即**主动丢弃超长正文**（[utils.py#L460-L500](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/chat/utils/utils.py#L460-L500)，
  默认值 [official_configs.py#L4632-L4640](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/config/official_configs.py#L4632-L4640)）。
- 段数上限：`max_sentence_num=8`（超了要么换默认回复、要么原文整条），
  再合并到 `max_split_num=3`
  （[official_configs.py#L4645-L4666](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/config/official_configs.py#L4645-L4666)、
  [utils.py#L498-L520](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/chat/utils/utils.py#L498-L520)）。
- 发送循环：**逐段 `await`**，每段一次独立 send；`sent_message_ids` / `send_results`
  逐段累加；某段失败 → 记录 `success=False` 后 `break`，**不重试、不回滚已发的段**
  （[reply.py#L807-L886](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/builtin_tool/reply.py#L807-L886)）。
- 节奏：第 2 段起 `typing=True` → `calculate_typing_time(text)`（中文 0.3 s/字、英文 0.15 s/字，
  再乘 `typing_speed`）后 `asyncio.sleep`；**不是**固定间隔
  （[uni_message_sender.py#L298-L311](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/chat/message_receive/uni_message_sender.py#L298-L311)、
  [utils.py#L555-L600](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/chat/utils/utils.py#L555-L600)）。

### B2. 失败怎么记

- 主进程：`DeliveryReceipt(status=sent/failed/dropped)`；
  失败原因字符串进 `error`；没有 `uncertain`
  （[types.py#L24-L31](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/platform_io/types.py#L24-L31)、
  [legacy_driver.py#L52-L92](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/platform_io/drivers/legacy_driver.py#L52-L92)）。
- 适配器：`status != "ok"` → 返回 `{"success": false, "error": wording, "metadata": {"retcode": ...}}`
  （[plugin.py#L252-L262](https://github.com/MaiM-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/plugin.py#L252-L262)）；
  发送网关直接调 `transport.call_action`，超时/断连抛异常被 `except Exception` 收成 `success:false`；
  其它 `api_*` 端点走 `NapCatActionService`，那里 `status != ok` 也抛 `RuntimeError`
  （[action_service.py#L34-L62](https://github.com/MaiM-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/services/action_service.py#L34-L62)）。
- **全程无自动重试**（`grep -i retry src/platform_io src/services/send_service.py` 只命中注释）。

### B3. 去重 / 幂等（这是 ling 最该抄的一块）

1. **入站去重**：`MessageDeduplicator`，`dict + heapq` 的 TTL 缓存，
   键是外部消息 id（[dedupe.py#L1-L60](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/platform_io/dedupe.py#L1-L60)）。
2. **出站在途幂等**：`OutboundTracker.begin_tracking((internal_message_id, driver_id))`，
   已存在未完成记录就抛 `ValueError`，broker 直接返回 `FAILED` 而不发第二次
   （[manager.py#L566-L600](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/platform_io/manager.py#L566-L600)、
   [outbound_tracker.py#L140-L146](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/platform_io/outbound_tracker.py#L140-L146)）。
3. **回执索引**：拿到 `external_message_id` 后按它建索引，可反查
   （[outbound_tracker.py#L150-L190](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/platform_io/outbound_tracker.py#L150-L190)）。
4. **平台 id 回传**：适配器发现 `internal_message_id != external_message_id` 时，
   通过 `message_id_echo` 回调把真实 id 告诉 Host
   （[plugin.py#L264-L290](https://github.com/MaiM-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/plugin.py#L264-L290)）。

> 注意：这套 tracker 是**内存态、TTL 30 分钟**，重启即失。它是"防并发重复"，
> 不是"跨重启的持久幂等"。ling 要的更强，得落库（§5.1）。

### B4. 引用怎么做

- 模型侧拿到的是**真实消息 id**（长 id 走短别名，见 §0.1），
  工具参数 `set_reply + reply_message_id` → `ReplyComponent` 插到消息段 0 位
  （[uni_message_sender.py#L272-L305](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/chat/message_receive/uni_message_sender.py#L272-L305)）。
- **是否引用是概率决策**：`decide_quote(...)` 按场景 `quote_frequency` 决定
  （[reply.py#L349-L362](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/builtin_tool/reply.py#L349-L362)）。
- **多段时"引用上一段"**：`_resolve_segment_reply_context` 返回
  `(True, previous_sent_message)`——用**上一段真实回执**当引用目标
  （[reply.py#L333-L346](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/builtin_tool/reply.py#L333-L346)）。
  但注意注释：`quote_message` 只改"显示目标"，被动回复额度仍算在原入站消息上（同文件 L845-L849）。
- 适配器出站：`{"type":"reply","data":{"id": target_message_id}}`
  （[segment_encoder.py#L121-L137](https://github.com/MaiM-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/outbound/segment_encoder.py#L121-L137)）。
- 适配器入站：把 reply 段**富化**成 `{target_message_id, target_message_content,
  target_message_sender_id/nickname/cardname}`，还顺带 `get_msg` 取被引消息正文
  （[message_codec.py#L338-L382](https://github.com/MaiM-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/inbound/message_codec.py#L338-L382)）。
  **这是"给模型看懂引用"的标准做法**，ling 目前只把 reply id 当"是不是在跟我说话"的布尔用。

### B5. 富媒体 / 合并转发

- 含 `forward` 段 → 拆成 `node` 节点走 `send_group_forward_msg` / `send_private_forward_msg`；
  夹在中间普通段会被包成一个 `node`（name 固定 `"MaiBot"`）
  （[message_codec.py#L17-L122](https://github.com/MaiM-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/outbound/message_codec.py#L17-L122)）。
- 文件：出站前由 FileService 落盘并填 `name/size/mime/url`
  （[send_service.py#L670-L700](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/services/send_service.py#L670-L700)）。
- 失败语义：**整条消息**（一次 action）成功或失败；段内多个元素不单独重试。

### B6. "不确定"状态

- 显式**不存在**（`DeliveryStatus` 只有 4 态）。适配器 `call_action` 超时 → 抛 `RuntimeError`
  → `{"success": false}` → 记 `FAILED`。**它把"可能已送达"当成"失败"，但从不重发**，
  所以不会重复；代价是"发送失败"日志里混进了"其实已发出"（[transport.py#L116-L145](https://github.com/MaiM-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/transport.py#L116-L145)）。
- 文件服务那条路倒是有 `unknown` 语义（`docs/file-service.md`：「`unknown` 表示没有取得确定的发送成功回执，
  平台仍可能稍后投递，必须核对聊天，不自动重发」）——**理念和 ling 的 `uncertain` 一致**，
  但没用在普通消息上（`grep unknown src/platform_io` 零命中）。

---

## 4. C. OneBot v11 的硬事实

### C1. `send_group_msg` / `send_private_msg` 的请求与响应；`message_id` 何时可靠；`echo`

- 参数：`send_group_msg{group_id, message, auto_escape?}`、`send_private_msg{user_id, message, auto_escape?}`；
  `message` 支持字符串（CQ 码）/ 数组两种形态；`auto_escape` **只在字符串形态生效**
  （[api/public.md#L42-L73](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L42-L73)）。
- 响应 `data.message_id`，规范标注类型是 **`number (int32)`**（同上 L72）。
  实现里实际是 int32 范围内的整数，**不是** QQ 原生长整型 msgId —— NapCat 的
  `message_id` 是 `md5(msgId|chatType|peerUid)` 取前 4 字节并清掉符号位得到的**短 id**
  （[message-unique.ts#L97-L108](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-common/src/message-unique.ts#L97-L108)）。
  推论：`message_id` **只在协议层可靠**，不要拿它去比 QQ 客户端里看到的"消息序号"。
- 成功判据（规范）：`status == "ok"` 且 `retcode == 0`；
  `status == "async"` ⇒ `retcode == 1`，**结果无法获知**（官方原话）；
  `status == "failed"` ⇒ `retcode` 既非 0 也非 1
  （[communication/http.md#L47-L82](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/communication/http.md#L47-L82)）。
  异步 API 的 `data` **永远是 null**（同上）→ 所以 `async` 时连 `message_id` 都没有，只能记 uncertain。
- `echo`：**仅 WS 调用形态**的请求标识，"类似 JSON RPC 的 id"，OneBot 原样回传
  （[communication/ws.md#L21-L35](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/communication/ws.md#L21-L35)）。
  HTTP 形态没有 `echo`，请求-响应靠连接本身配对。**ling 走 HTTP，所以不需要 echo**；
  若将来改 WS，`echo` 是唯一能把"回执"和"哪一段"对上的东西。
  参考实现用法：MaiBot 适配器 `{"action","params","echo": uuid4().hex}` + `_pending_actions` Future
  （[transport.py#L116-L145](https://github.com/MaiM-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/transport.py#L116-L145)）。

### C2. `get_msg` 能查到什么、查不到什么

- 规范：入参 `message_id`，返回 `time / message_type / message_id / real_id / sender / message`
  （[api/public.md#L107-L124](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L107-L124)）。
- NapCat 实现里 **`get_msg` 只是"内存短 id 表里找得到就查得到"**：
  先 `MessageUnique.getShortIdByMsgId()`，再 `getMsgIdAndPeerByShortId()`，
  查不到直接 `throw Error('消息不存在')`
  （[GetMsg.ts#L57-L72](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/msg/GetMsg.ts#L57-L72)）。
  那张表是 `LimitedHashTable(maxSize=5000)`，按**插入顺序 FIFO 淘汰**
  （[message-unique.ts#L78-L95](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-common/src/message-unique.ts#L78-L95)）。
- 因此：
  - ✅ 能证明"**平台侧存在这条 id 的消息**"（在窗口内）。
  - ❌ 不能证明"对方收到了 / 被读了"。
  - ❌ **查不到 ≠ 没发出去**：进程重启、超过 5000 条、被撤回，都会 "消息不存在"。
  - ❌ `sent` 但**没有 `platform_message_id`** 的 operation 永远对不上账（`get_msg(None)` 必错）。
- 结论：`get_msg` 可以做**单向确认**（查到→升级为 delivered），
  **绝不能**反过来把"查不到"记成 failed。ling 现在的 `query_receipt` 已经这么写了
  （`connector.py:199-208`），这是对的。

### C3. `message_sent` 事件

- **不是 OneBot v11 规范事件**（`event/message.md` 里没有；v11 只有 `message / notice / request / meta_event`）。
  它是实现扩展：NapCat 有 `EventType.MESSAGE_SENT = 'message_sent'`
  （[OneBotEvent.ts#L3-L9](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/event/OneBotEvent.ts#L3-L9)），
  走的是和普通消息**同一套** `OB11Message` 结构，额外字段：
  `message_sent_type: 'self'`、`target_id`、`real_seq`、`message_seq`、`message_id`、`real_id`
  （[message.ts#L355-L410](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/types/message.ts#L355-L410)、
  [msg.ts#L1116-L1120](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/api/msg.ts#L1116-L1120)）。
- **默认不发**：`reportSelfMessage` 默认 `false`，且按 adapter 配置逐连接过滤
  （[config.ts#L26](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/config/config.ts#L26)、
  [index.ts#L584-L592](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/index.ts#L584-L592)）。
- 能不能当送达证据：**能，但只能当"平台侧存在"的证据**，和 `get_msg` 同级；
  且它的 `message_id` 与 `send_*_msg` 回执的 `message_id` 同一 id 空间
  （都出自 `MessageUnique.createUniqueMsgId`）。所以它**可以**用来给"我们没有拿到回执的那一段"补 id。
- 能不能去重：能——`message_id` 在一个 NanCat 进程内唯一、跨重启稳定（哈希确定性），
  可以当"这条我发过了"的键。**前提**是你得让用户在 NapCat 侧打开 `reportSelfMessage`。
- ling 现在按 `ignored` 丢弃它（`connector.py:349-375`），**判断合理但因为默认关闭，
  它在大多数部署里根本不会来** —— 别把对账设计成"依赖回显"。

### C4. `reply` 段在群聊里的语义；引用已过期消息会怎样

- 段格式：`{"type":"reply","data":{"id":"123456"}}`，CQ 形态 `[CQ:reply,id=123456]`；
  `id` 收发都支持
  （[message/segment.md#L392-L408](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/segment.md#L392-L408)）。
  NapCat 的 schema 里还多一个 `seq`（"消息序列号，**优先使用**"）
  （[types/message.ts#L69-L75](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/types/message.ts#L69-L75)）。
- **NapCat 的实际行为（重点，和 ling 的假设不同）**：
  1. 有 `seq` 就用 `getMsgsBySeqAndCount(peer, seq)` 反查；否则用
     `MessageUnique.getMsgIdAndPeerByShortId(parseInt(id))`
     （[msg.ts#L667-L700](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/api/msg.ts#L667-L700)）。
  2. **反查不到 → `logWarn('回复消息不存在')` → 返回 `undefined`**，即**这一段被丢掉**，
     不抛错、不拒收整条（同上 L680-L690）。
  3. 外层 `createSendElements` 对每个段 `.catch(undefined)` 后再 `filter(Boolean)`
     （[msg.ts#L1258-L1292](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/api/msg.ts#L1258-L1292)）。
  4. **只有"所有段都被丢掉"时**才 `throw new Error('消息体无法解析, 请检查是否发送了不支持的消息类型')`
     （[msg.ts#L1295-L1298](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/api/msg.ts#L1295-L1298)）
     → 这时才变成 `status:"failed"`。
  5. 同理，`at` 段解析不出 UID 会 `throw Error('Get Uid Error')`，也被 `.catch(undefined)` 吞掉、该段丢弃
     （[msg.ts#L658-L665](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/api/msg.ts#L658-L665)）。
- **结论**：在 NapCat 上，"引用一个不存在的 id"最可能的表现是**引用静默消失、正文照发**，
  而不是 `retcode=100` 整条拒收。ling 的 `connector.py:117` 注释说的 `retcode=100`
  **在 NapCat 代码里找不到出处**（见 C5），建议下次遇到时把 `wording/message` 原样记下来。
- Lagrange：其 OneBot11 实现已从 `Lagrange.Core` 拆出，`LagrangeDev/Lagrange.OneBot` 仓库
  在本次调研时**已 404**，无法核对它是否拒收整条 → **未证实**。

### C5. NapCat / Lagrange 的频率限制与常见错误码

- **NapCat 自己不设发送频率限制**：`send_*_msg` 路径上没有任何 `sleep`/令牌桶
  （`grep -n "sleep(|setTimeout(" packages/napcat-onebot/api/msg.ts` 只有两处与发送无关的注释/延迟删文件）；
  唯一的"节流"是 `timeout`——按文件体积估的上传超时，可被 payload 里的 `timeout` 覆盖
  （[SendMsg.ts#L24-L26](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/msg/SendMsg.ts#L24-L26)、
  [msg.ts#L1295-L1330](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/api/msg.ts#L1295-L1330)）。
  QQ 服务端侧的频控（"发送过快"之类）**未证实**，它只会以 `wording` 的形式出现。
- **NapCat 的 retcode 语义（按代码，不按传言）**

  动作层（[OneBotAction.ts#L90-L115](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/OneBotAction.ts#L90-L115)）：

  | 通道 | 情形 | status | retcode |
  | --- | --- | --- | --- |
  | HTTP | 参数 schema 校验失败 | `failed` | **400** |
  | HTTP | 抛异常（含发送超时、"消息体无法解析"） | `failed` | **200** ← 坑 |
  | WS | 参数校验失败 | `failed` | **1400** |
  | WS | 抛异常 | `failed` | **1200** |
  | 两种 | 成功 | `ok` | **0** |

  网络层（更外层）：`json 解析失败` → `1400`；`不支持的 API` → `1404`
  （[http-server.ts#L234-L242](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/network/http-server.ts#L234-L242)、
  [websocket-server.ts#L184-L192](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/network/websocket-server.ts#L184-L192)）；
  HTTP 侧连"不支持的 API""服务器已关闭"也是 **200**
  （[http-server.ts#L296-L308](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/network/http-server.ts#L296-L308)）。
  `1401`（权限不足）只出现在**文档样例**里（`ActionExamples.Common.errors`），
  `grep -rn "OB11Response.error("` 全仓只出 `200/400/1200/1400/1404`，**没有 100**。
  → `retcode==200` **不代表成功**，必须同时看 `status`；ling 现在是
  `retcode == 0 and status != "failed"`，正好躲过这一刀。
- 关键缺陷（直接影响 ling 的重试策略）：**1200/200 把"发送前失败"和"发送中超时"混成同一个码**。
  "消息体无法解析"（所有段都被丢掉）其实一定没发出去，而超时可能已经发出去了，
  但两者都是 `1200`/`200`。所以**只能保守按"可能已发出"处理**（§5 P1-1）。
- Lagrange 的 retcode 语义：**未证实**（仓库已 404）。

### C6. 文本长度上限、CQ 码与消息段混用的坑

- OneBot v11 **不规定文本长度上限**，规范里没有这个字段；`api/public.md` 全文没有长度约束。
  各实现各自为政（MaiBot 侧默认 512 字上限是**它自己的**策略，不是协议）。
- CQ 码字符串形态必须转义：纯文本要转 `& [ ]`，**CQ 参数值里还要额外转 `,`**
  （[message/string.md#L40-L77](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/string.md#L40-L77)）。
  → 所以"用户正文里带 `[CQ:...]` 或 `]`"在字符串形态下会被截断/误解析；
  **数组形态没有这个问题**（`text` 段内容原样）。
  ling 一直用数组形态（`connectors/onebot/message.py:137-165`、`188-224`），**这是对的，别改回字符串**。
- `auto_escape` 只在字符串形态有效（C1）——数组形态不需要它，也别指望它救场。

---

## 5. D. 给 ling 的修复清单（按优先级）

> 每条格式：**改哪里 → 怎么改 → 怎么验证（可程序化断言）→ 风险**。
> 全部只读调研，本文不含代码改动。

### P0-1 段级幂等：部分送达不再重复第 1 段

**改哪里**
- `src/ling/kernel/inbound_intake.py:279-284`（释放认领的条件）
- `src/ling/capabilities/delivery.py:88-227`（`deliver` 建 operation 处）
- `src/ling/kernel/bridge.py:1057-1178`（`_deliver` 的段循环）
- 新表/新索引：`src/ling/infra/models.py:472-484`（`IdempotencyKey` 已够用）

**怎么改**（三步，缺一不可）
1. **段键落库**。每段在 `deliver()` 建 operation 之前，用
   `IdempotencyKey(scope="delivery_part", key=f"{plan_id}:{ordinal}")` 抢占：
   - 抢占成功 → 正常发；
   - 抢占失败且已有 `result_ref`（= 上次的 operation_id）→ **查那条 operation 的状态**：
     `succeeded` → **跳过这一段**（已经发过），`uncertain` → 交给对账（P0-2），`failed` → 重发。
   这一条把"MaiBot 的 `begin_tracking`"（§3.3）翻译成持久化版本。
2. **`plan_id` 必须由本轮 turn 派生、可重建**。现在是随机
   `plan_id = f"plan_{uuid4...}"`（`bridge.py:1102`），重投会拿到新 plan_id，幂等直接失效。
   改成 `plan_id = f"plan_{turn_id}"`（turn 是 `_ensure_turn(client_turn_id=external_event_id)`
   幂等复用的，`bridge.py:504-509`）。
3. **部分送达不释放认领**。`inbound_intake.py:279-284` 的条件从
   `reply_text and not delivered` 收紧为
   `reply_text and not delivered and parts_sent == 0`。
   部分送达时**保留**认领（并写一条 `delivery_partial` 事件），让"重投"变成"续发剩下的段"，
   而不是"从头再来"。续发由 P0-2 的对账 / 续发任务负责（同一张段键表就能表达「哪些段还没发」）；如果没有续发机制，
   保留认领至少能**阻止**重复第 1 段（代价是尾段放弃，但这已经是当前行为）。

**验证**
```python
# tests/unit 或 tests/integration
# 1) 第 1 段成功、第 2 段 rejected → 重投同一个 external_event_id：
#    fake connector 的调用序列里，第 1 段正文只出现一次
assert [m.text for m in fake.sent].count(parts[0]) == 1
# 2) 认领未释放
assert await _idem_row(scope=f"connector:{inst}", key=ext_id) is not None
# 3) 段键唯一：同一 (plan_id, ordinal) 只能有一条 succeeded operation
```
另外可加一条**不变量测试**：对任意 `parts`，"发送次数 == len(parts) + 重试次数"，
且重试只发生在 `failed/uncertain` 的段上。

**风险**
- 幂等键算错（plan_id 不稳定）→ 全部段被跳过，等于不发言。**必须**有断言覆盖"首次发送不被跳过"。
- 把 `uncertain` 当 `failed` 重发 = 重复消息。这条是红线：`uncertain` 只能进对账，不能进重发。

### P0-2 对账：让 `uncertain` 真的收敛

**改哪里**
- 新增 handler + 周期任务：`src/ling/worker.py`（`@handler("delivery.reconcile")` + `PERIODIC_JOBS` 加一条，`worker.py:3628-3651`）
- 复用 `src/ling/capabilities/delivery.py:229-270`（`reconcile` 已实现）
- `connectors/onebot/connector.py:181-208`（`query_receipt` 加"无 id"分支）

**怎么改**
1. **触发者**：周期任务扫 `operations.kind='delivery' AND state='uncertain' AND created_at > now-1h`，
   逐条 `DeliveryService(session).reconcile(connector, op_id)`；每条之间 `sleep`（见 P2-1 限速）。
   注意 `tests/unit/test_worker_dispatch.py` 有闸门：新 topic 必须出现在
   `PERIODIC_JOBS` / `OUTBOX_TOPICS` / `LEGACY_MANUAL_TOPICS` 之一，否则测试红——这是好事，照做。
2. **有 `platform_message_id`**：走现有 `get_msg`（一对一确认，C2）。
3. **没有 `platform_message_id`**（`sent` 无 id、`async`、响应不是 JSON）：`get_msg` 永远查不了。补一条
   **内容比对**兜底：`get_group_msg_history(group_id, count=20)` / `get_friend_msg_history`，
   找 `sender.user_id == self_id` 且正文等于该段文本、时间在 `[sent_at-30s, sent_at+120s]` 的消息
   （NapCat 有该 action：[GetGroupMsgHistory.ts#L59-L82](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts#L59-L82)）。
   命中 → 补 `platform_message_id` + `delivered`；**未命中 → 保持 `uncertain`，绝不改 failed**（C2）。
4. **终态封顶**：`uncertain` 超过 N 次对账（或 24 h）→ 标 `unknown_expired` 并进行为日志，
   避免无限扫描。**不要**改成 failed（会让上游误判"没发"）。

**验证**
```python
# 1) uncertain + get_msg 命中 → succeeded
# 2) uncertain + get_msg 抛 ChannelError → 仍是 uncertain（不是 failed）
# 3) 没有 platform_message_id 的 uncertain，在 fake 的 history 里放一条同文本 →
#    succeeded 且 platform_message_id 被回填
# 4) 周期任务确实被调度：assert "delivery.reconcile" in {j.topic for j in PERIODIC_JOBS}
```

**风险**
- 内容比对可能**误命中**（同一句话连发两遍，例如"好"）。限定 `self_id` + 时间窗 + **最多匹配一次**
  （匹配到 2 条以上就保持 uncertain）。
- `get_msg`/`get_*_msg_history` 是外呼，扫太多会把 NapCat 打满 → 每轮限条数（如 20 条/次）。

### P0-3 180 秒超时不再丢正文

**改哪里**
- `src/ling/kernel/bridge.py:1005-1010`（超时 `break`）
- `src/ling/worker.py:1326-1330`（`return False, "生成超时"`）
- 落库点其实已经存在：`src/ling/kernel/turn.py:1386-1412`（`actor_message` 在 `_finalize` 里先落库）

**关键事实（先看清再改）**：`turn.completed` 之前，正文**已经写进 `messages`（role=actor）**；
180 s 超时后 bridge 只是**不再收事件**，生成协程仍在跑，最终**还是会落库**。
所以"丢正文"准确说是"**正文在库里，但没人发**"——不要用"重跑模型"来修（会二次生成、语气漂移）。

**怎么改**（先落库再发 + 断点续发）
1. 超时分支不丢弃：拿到 `turn_id` 后**不 break 出整个流程**，而是
   `task = asyncio.create_task(_deliver_after_finalize(turn_id))`——由后台任务等
   `messages` 里出现该 turn 的 actor 行（轮询 DB，最多再等 N 分钟）后投递。
2. 若超时就彻底放弃，则**至少把"有正文未投递"写进库**：给 `turns` 或 `operations`
   落一条 `delivery_pending`，由 P0-2 的周期任务扫出来续发（同样是段级幂等，天然安全）。
3. **不要**把"超时"映射成 `handled_as=failed` 后再让上游重投——那会重新生成。
   正确语义是 `handled_as="pending_delivery"`（新枚举）或至少 `reason="生成超时，正文已落库未投递"`。

**验证**
```python
# fake coordinator：先 yield 一个 delta，然后 sleep > 180s 再 yield turn.completed
# 断言 1：180s 后 bridge 不返回 "no_text"
# 断言 2：DB 里 messages(role=actor, turn_id=t) 最终存在
# 断言 3：delivery_pending/续发任务最终把这段发出去（fake connector 收到 1 次）
# 断言 4：整个过程只调用一次模型（router.calls == 1）
```

**风险**
- 后台等 DB 会引入"孤儿任务"：进程重启就没了 → 必须**以 DB 为准**（P0-2 扫描），
  内存任务只是加速。
- 若模型真的永不结束，续发会在超时后放弃 → 至少要留 `delivery_pending` 行可查。

### P0-4 引用回复根治：给上下文真实 message_id

**改哪里**
- `src/ling/kernel/context.py:1150-1210`（历史渲染 `f"{name}：{content}"`）
- `src/ling/capabilities/expression_choice.py:95-107`（提示词措辞）
- `connectors/onebot/connector.py:127-142`（保留降级兜底，但改判据，见 P1-2）

**怎么改**
1. **带 id 进上下文**（照抄 MaiBot 的 `[msg_id:...]` 做法）。入站已经算出了真实平台 id：
   `external_event_id = f"{self_id}:{message_id}"`（`connector.py:401-405`），
   `message_id` 就是 NapCat 的 reply 段需要的**短 id**（C4）。把它渲染在每条历史旁：
   `[msg_id:1234567890] 张三：……`。**长 id 抄 MaiBot 的别名方案**（`m` + 6 位 base32 哈希
   + 反向映射表 `expand_message_id_aliases`），别把 100 字符的 `ROBOT1.0_...` 塞进上下文
   （[message_adapter.py#L35](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/context/message_adapter.py#L35)、
   [message_id_alias.py#L1-L50](https://github.com/MaiM-with-u/MaiBot/blob/8e18e306436101cf890cecff489f59675f48baea/src/maisaka/context/message_id_alias.py#L1-L50)）。
2. **提示词改成 `[quote:消息ID]`**，并明确"只能用上面出现过的 ID"。
   现在写的是"消息序号"，而模型手里**没有序号表**，只能瞎编（这就是 `[quote:3]` 的来源）。
3. **发送前本地校验**（省钱省事）：`build_outbound_segments` 收 `allowed_reply_ids: set[str]`，
   不在集合里就**不带 reply 段**（并在回执 detail 里记"引用目标不在可见范围，已降级"）。
   这比"发给平台让平台丢"更可观测。
4. **保留平台级兜底**，但要按 C5 重新分类（P1-2）。

**验证**
```python
# 1) 编译上下文后，抽出的 [quote:X] 全部能在 allowed_reply_ids 里找到
def test_every_quote_id_is_visible(ctx_text, allowed): ...
# 2) 模型给出不在集合里的 id → 发出的段里没有 {"type":"reply"}
# 3) 合法 id → 段里 {"type":"reply","data":{"id": <入站 message_id>}}
# 4) 端到端：真实 NapCat 上引用一条历史消息，回执 delivered 且群里能看到引用
#    （这一条只能人工/实机验证，写进验收清单）
```

**风险**
- NapCat 的短 id 表是 **5000 条 FIFO**（C2），历史窗口太长时"上下文里曾有、发送时已淘汰" →
  引用仍可能静默消失。所以上下文里只保留**最近 N 条**的 id，并接受它会失效（有兜底）。
- 别用 `seq` 代替 `id`：NapCat 自己的入站事件里 `message_seq == message_id`（都是短 id），
  而 reply 转换器把 `seq` 当**真实序号**去 `getMsgsBySeqAndCount`
  （[msg.ts#L667-L682](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/api/msg.ts#L667-L682)、
  [msg.ts#L1144-L1149](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/api/msg.ts#L1144-L1149)）。
  拿 `message_seq` 填 `seq` 很可能反查失败 → 引用被丢。**目前用 `id` 是对的**。

### P1-1 失败语义重分类（比"重试"更重要）

**改哪里**：`connectors/onebot/connector.py:260-276`（`_call` 的 retcode 分支）

**为什么**：现在除 `async`/`retcode==1` 外**全部** `rejected`，而 `rejected` 会触发
"去掉装饰段重发一次"（`connector.py:130-142`）。但 NapCat 抛异常时 HTTP 回的是
**`retcode=200`**（C5）——那可能是**发送超时**，消息**可能已经在群里**。这时重发 = **重复**。

**怎么改**（按 C5 的表）
| 条件 | 分类 | 依据 |
| --- | --- | --- |
| `retcode in {400, 1400}` | `rejected`（明确未发出） | schema / json 无效，发送前就失败了 |
| `retcode == 1404` | `unsupported`（不是 `rejected`） | 「不支持的 API」，重发无意义 |
| `retcode in {1200, 200}` | **`uncertain`** | 异常：无法区分"发送前失败"与"发送中超时" |
| `status == "async"` or `retcode == 1` | `uncertain` | 规范：结果无法获知 |
| 其它非 0 | `rejected` | 兜底 |

并且"去掉装饰段重发"**只对 `rejected` 生效**（已是现状），`uncertain` 一律进对账（P0-2）。
另外把 `detail` 里**保留 `wording/message`**（`detail_or` 已有这个能力，`connector.py:696-701`）——
线上判断 `retcode=100` 到底是什么、以及"1200 到底是超时还是消息体无法解析"，全靠这行字。

> 注意一个副作用：把 1200/200 归为 `uncertain` 之后，ling 那套"去掉引用重发"
> 在 NapCat 上**几乎永远不会触发**了。这不是退步——C4 已经证明无效引用是**静默丢弃该段**，
> 根本走不到错误分支。真正该做的是 P0-4（给真 id）。

**验证**
```python
@pytest.mark.parametrize("retcode,expect", [
    (400, ChannelErrorKind.rejected), (1400, ChannelErrorKind.rejected),
    (1200, ChannelErrorKind.uncertain), (200, ChannelErrorKind.uncertain),
    (1404, ChannelErrorKind.unsupported),
    (1, ChannelErrorKind.uncertain), (100, ChannelErrorKind.rejected),
])
async def test_retcode_classification(retcode, expect): ...
# 并且：uncertain 时 fake connector 的调用次数 == 1（不重发）
```

**风险**
- 现有 `tests/unit/test_onebot_send_degradation.py:109` 用的是 `retcode=100` → `rejected`，会保持绿；
  但 `tests/integration/test_onebot_negative.py:187` 那类"任意 retcode"的用例可能要改。
- 若某实现（比如 Lagrange）确实用 100 表示"参数错误"，映射不变仍是 `rejected`，安全。

### P1-2 分段之间的限速：固定 1 秒足够，别抄 AstrBot 的随机 1.5–3.5 s

**改哪里**：`src/ling/capabilities/chunking.py::PART_DELAY_SECONDS`（`bridge.py:1106-1108` 使用）

**依据**：AstrBot 的 1.5–3.5 s 是**拟人节奏**（`respond/stage.py:100-113`），不是限速；
MaiBot 用"打字时长"（`calculate_typing_time`，中文 0.3 s/字）拟人。
两家都**没有**真正的令牌桶。NapCat 侧也没有频控（C5）。
所以：**ling 现在 1 s 固定间隔不算差**，需要补的是
1. 段间隔加**抖动**（`uniform(0.8, 1.6)`），避免机械节奏被识别；
2. 若一次要发 >5 段（长回复），**先考虑折叠成合并转发**（AstrBot 的 `forward_threshold` 思路），
   而不是连发 8 条——这同时解决刷屏与频控风险；
3. 真要限速就做**每窗口令牌桶**（如 3 条/10 s），放在 `DeliveryService.deliver` 外层，
   所有渠道共用。

**验证**：`assert 0.5 <= delta <= 2.0`（相邻两次 fake send 的时间差），
以及"折叠分支"的单测：`len(parts)>5 → 走 forward`。
**风险**：抖动会让集成测试变慢且不稳 → 用可注入的 `delay_fn`。

### P1-3 `sent`（无 message_id）不该算"已送达"

**改哪里**：`connectors/onebot/connector.py:157-179`（`platform_message_id` 缺失 → `DeliveryState.sent`）
+ `src/ling/capabilities/delivery.py:37-59`（`delivered` 判据）

**为什么**：`sent` 被算作 `delivered`（`delivery.py:204-205` 把 `sent` 也映射成 `succeeded`），
于是"平台没给 id"被当成"发完了"。`sent` 语义上应该等于"**提交了但未确认**"，
应该进对账（P0-2）而不是直接成功。

**怎么改**：`DeliveryState.sent` → `OperationState.uncertain`（或新增 `submitted` 态并对账），
保留"没有 id 也能成功"的唯一例外：`status=="ok" && retcode==0 && data==null` 且渠道声明
`receipt_reliability="best_effort"`（`connector.py:548`）——此时仍对账一次。
**验证**：`retcode=0, data=None` → 落 `uncertain` 且被 `delivery.reconcile` 扫到。
**风险**：会把"其实成功"的投递变多一轮查询；灰度期建议只记录不阻塞。

### P2-1 `render_output` 是死代码且会腰斩句子

`connectors/onebot/connector.py:531-536` 按字符硬切，不看句子边界；生产路径用的是
`chunking.plan_parts`（`bridge.py:1098`），`render_output` 只在测试里被调用
（`tests/integration/test_onebot.py:362`）。建议让 `render_output` 直接委托 `plan_parts`，
或删掉以免有人误用。**风险**：低。**验证**：`grep -rn "render_output" src` 只剩定义。

### P2-2 `message_sent` 回显：保持忽略，但把 id 记下来

现在 DEBUG 里已经打了 `message_id`（`connector.py:367-372`）。可选增强：
在 `_deliver` 拿不到 id 的那一段，允许用"最近 N 秒内同目标的 `message_sent`"补 id。
**但默认 `reportSelfMessage=false`（C3），不能把它变成必需依赖**。
优先级低，等 P0-2 的 history 兜底验证过再说。

---

## 6. 照抄清单（按优先级，5–8 条）

| # | 一句话 | 预计改动面 |
| --- | --- | --- |
| 1 | **段级幂等**：`plan_id` 改成由 `turn_id` 派生，每段发前用 `IdempotencyKey("delivery_part", f"{plan_id}:{ordinal}")` 抢占，`succeeded` 就跳过 | `bridge.py:1102`、`delivery.py:144-164`、`models.py`（复用现表）；约 60–90 行 + 单测 |
| 2 | **部分送达不再释放认领**：`inbound_intake.py` 的释放条件加 `parts_sent == 0` | 1 个条件 + 2 条测试；约 10 行 |
| 3 | **`uncertain` 进周期对账**：`worker.py` 加 `delivery.reconcile` topic 到 `PERIODIC_JOBS`，扫 1 h 内的 uncertain，`get_msg` 有 id 时确认、无 id 时用 `get_group_msg_history` 内容比对兜底 | `worker.py`（handler+job）、`connector.py`（`query_receipt` 无 id 分支）；约 120–150 行 |
| 4 | **失败语义按 NapCat 实际码重分类**：400/1400 → `rejected`、1404 → `unsupported`；**1200/200** → `uncertain`（不再降级重发）；`detail` 保留 `wording` | `connector.py:260-276`；约 20 行 + 参数化测试 |
| 5 | **引用根治**：上下文每条历史带真实 `message_id`（长 id 用短别名），提示词从 `[quote:消息序号]` 改为 `[quote:消息ID]`，发送前本地校验白名单，兜底仍保留降级 | `context.py`（渲染）、`expression_choice.py`（措辞）、`message.py::build_outbound_segments`（白名单）；约 100–140 行 |
| 6 | **180 s 超时不丢正文**：超时后不 break，改为落 `delivery_pending` 行并由第 3 条的周期任务续发（段级幂等保证不重复） | `bridge.py:1005-1010`、`worker.py:1326-1330`、（新）pending 查询；约 80–120 行 |
| 7 | **长回复优先折叠成合并转发**（>5 段或 >1500 字），而不是连发 8 条；`PART_DELAY_SECONDS` 加抖动 | `chunking.py::plan_parts` + `connector.py`（`send_*_forward_msg`）；约 80 行 + 3 条测试 |
| 8 | **`sent`（无 message_id）不再算已送达**，改为待对账 | `connector.py:166-170`、`delivery.py:204-205`；约 10 行 |

---

## 7. 未证实 / 需要实机确认

1. **`retcode=100` 的真实来源**。NapCat 代码里没有 100（C5），Lagrange 仓库 404 无法核对。
   → 下次线上出现时，把 `status/wording/message/retcode` 四个字段原样贴进 issue，再定分类。
2. **NapCat 引用"已过期消息"是否整条拒收**。代码路径显示是"静默丢弃该段"（C4），
   但我**没有实机验证**（需要一台跑着 NapCat 的环境 + 一条超出 5000 条窗口的旧消息）。
3. **QQ 服务端的发送频控**（"发送太快"之类）的阈值与报错文案。未见任何实现文档化。
4. **AstrBot 对 base64 图片的体积保护**。`_from_segment_to_dict` 直接内联 base64，
   没看到上限检查，但可能有上游限制未读全。
5. **Lagrange** 的 reply 语义与 retcode 表：仓库已迁走，本轮无法核对。
6. **`get_group_msg_history` 是否包含机器人自己发的消息**。实现是
   `MsgApi.getMsgHistory(peer, ...)`（[GetGroupMsgHistory.ts#L59-L72](https://github.com/NapNeko/NapCatQQ/blob/98d781d19815d1625b24d717ce280ecf0a4f6d0c/packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts#L59-L72)），
   理论上包含（历史记录里有自己的消息），但**没有实机验证**——这一条直接决定 P0-2 的兜底是否可行，
   建议先用一条脚本打一次这个 API 看返回。
