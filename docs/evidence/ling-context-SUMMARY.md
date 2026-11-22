# Ling 上下文研究汇总 · 第一期改什么

> 取证对象：`/Users/lingxi/project/ling`，`main`，HEAD `de43cbe79a78e0c01f7821470500eaa04ea79497`（2026-11-22 09:17 +08）。
> 结束时 `git status --short` 只有未跟踪的证据文档，无 tracked 文件改动；`bridge.py` sha256 `334534fe…a94a70`、`context.py` sha256 `ffb573b4…9718ba` 前后一致。
> 派发 16 组 + 1 份样本/汇总：人类对话 4、即时通讯 4、源码 4、样本与方案 4。全部证据落盘于 `docs/evidence/ling-context-*.md`。
>
> 置信度写法：**【实测】**＝本轮亲自执行函数/命令得到；**【代码路径】**＝读 HEAD 源码得到、未跑回放；**【推理】**＝机制推断，可被反例推翻；**【转述·未核实】**＝引自论文/他人文档但未独立核实。强格式只给【实测】和【代码路径】。

## 一、先回答三个问题

| 层级 | 应放什么 | 依据 |
|---|---|---|
| **常驻** | 人设、关系与自我状态；核心记忆 / 已发布知识；本轮能力声明；当前消息原文（永远最后一块） | 【代码路径】`context.py:773-804`、`806-846`、`1836-1847`；当前消息不参与预算/摘要丢弃 |
| **随当前话题带出** | 最近原文窗口里的完整语段；逐条署名的说话人；最近 12 条可引用消息 id；受 TTL 约束的 `topic` / `open_questions` | 【代码路径】`context.py:1486-1520`、`1713-1717`、`1965`、`230-244` |
| **只在明确需要时找回** | 旧话题原文细节、被引用但已滚出窗口的消息、跨窗/长期记忆证据；由显式锚点（时间词+话题名词、引用/@、找回词）触发 | 【代码路径】`recall_query.py:13-27`、`service.py:1814-1818`、`context.py:1550-1575`；【转述】Clark & Brennan 的 grounding criterion 随「当前目的」变化 |

**会话管理结论：不新建窗口。** 【代码路径】窗口复用键是 `(actor_id, channel, window_ref, visibility, [私聊]subject_id, [可信时]origin_scope+instance_id)`，查找键里没有话题、时间或消息 id（`bridge.py:1556-1635`）。同一窗口里「隔天续聊」和「几秒换题」在时间线上共存，这正是判断所需的信息；分窗会把它拆掉。AstrBot 的 `/reset`、`/new` 与 MaiBot 的 `/clear` 都靠显式命令改对话存储，Ling 侧全仓无对应聊天内命令入口——但这不构成要加命令的理由，见第七节。

## 二、反例线索：最小复现与归因更正

用户线索：「你还记得前天聊了什么吗」→「早上好」→ 对问候回一个「？」→ Ling 解释问候后继续列很久以前的四项产品需求。

【实测】直接调用 `recall_query.py`，结果（本轮亲自执行）：

```
'？'                       skip=True   recent=False
'?'                        skip=True   recent=False
'早上好'                    skip=False  recent=False
'后来呢？'                  skip=False  recent=True
'接着说前天那个方案'          skip=False  recent=False
'你还记得前天聊了什么吗'      skip=False  recent=False
```

因此必须更正一条流传的归因：**「？」不会触发自动记忆召回**。全角问号经 `NFKC` 归一成 `?`，再被 `rstrip(".!?。~")` 剥成空串，空串在 `_STANDALONE_REPLIES` 里（`recall_query.py:6-11,19-22`），`context.py:1552-1554` 写 `memory_recall_skipped="low_information"`。真正未跳过的是「早上好」：它不在跳过集合，会以 `query="早上好"` 调 `_memory.recall()`（是否命中那四项，**未取得快照，不能断言**）。

【代码路径】旧内容进入这一轮输入有三条不互相排斥的通道：

1. 滚动摘要 `window_ctx.summary` 只要非空就无条件注入，**不判话题相关性、不判 TTL**（`context.py:1673-1685`；`_window_topic_live:230-244` 只管短记忆）。
2. 最近原文窗口按 sequence 从新往旧装到 `HISTORY_TOKEN_BUDGET=20000` 用尽（`context.py:81,1486-1520`）；四项需求若仍在窗口内就是原文。
3. 「早上好」的宽泛召回，证据车道上限 `min(DEFAULT_BUDGETS["evidence"], 420×scale)`（`context.py:270,1576`）。

**归因边界**：截图只说明回复表现。三条消息是三次独立 `compile()`（`bridge.py:863-877` → `turn.py` → `context.py:548`），不继承彼此状态；要判定四项需求来自哪一条，必须抓 `prompt_snapshots`（`turn.py:1897-1958` 保存 `compiled.annotated`，与 messages 逐条对应）。本轮**没有真实模型输入/回复**，所以「摘要常驻是主因」目前是【推理】，不是结论。

## 三、论文与源码证据

### 3.1 人类对话与 IM（详见 8 份证据文档）

- **轮次与修复**【转述·部分原文】：经典 Sacks / Schegloff / Jefferson 全文在本环境取不到（DOI 403/404、OpenAlex 全文需 key），只核到 1977 repair 摘要「self-repair predominates over other-repair」。可逐字引用的是 Stivers et al. 2009（PNAS/PMC，10 语言 101 段会话）、Meyer 2023（uhu/yeah 属 backchannel 且常不算一个 turn）、Goddard & Gillespie 2025（Reddit 异步文本 58.48% 互动出现修复发起、44.80% 无人回复）。含义：「？」是一次**局部修复**，不是回看旧话题的许可。人类时长数字不能当 Ling 阈值。
- **话题建立与结束**【原文】：Drew & Holt 1998（*Language in Society* 27:4, 495–522，White Rose 开放 PDF）给出可观察序列——总结性评价收束 → 对方极简「同意但不展开」→ 任一方开新话题；第 3 步可省略，双方同时开新话题是共享换题点的强证据。其 p.518 注 9 回答「旧话题凭什么被继续」：需要**可重识别的旧内容 + 明确再进入动作 + 此前发生过转换**，不是「记忆没被清空」。
- **共同背景与指代**【原文】：Clark & Brennan 1991（作者自存档）——共同背景要 moment-by-moment 更新；grounding 标准是「对当前目的足够」；含糊时人用替代描述、指认、指称分批、试探性指称主动邀请确认。Grosz & Sidner 1986（Illinois 技术报告原文）——注意力状态只保留「对后续处理关键的 salient 信息」；段内倾向代词、跨段更常用完整描述。
- **事件边界与未完成意图**【原文/摘要】：Kurby & Zacks 2008、Zacks et al. 2007、Swallow et al. 2009、Ezzyat & Davachi 2011（PMC 开放全文）——边界处编码更好、跨边界旧信息可接近性下降，方向与「旧话题容易被继续」相反，不能拿来直接解释。最贴合主问题的是 Seifert & Patalano 2001（OpenAlex 摘要）：未完成目标与「预期环境特征」绑定，当前情境匹配时旧目标自动回到意识；2026 Zeigarnik 元分析显示关联存在但受调节、且是相关非因果。
- **IM 四组**【合成 + 少量平台文档】：QQ 机器人官方文档确认单聊/群聊/频道三类场景、被动回复带 `msg_id`、`msg_id+msg_seq` 去重；用户侧 QQ 功能说明与 CSCW 论文原文未取得。异步续聊的关键结论：隔天是否续旧取决于**有无锚点**而非间隔；「看见后延迟回复」与「晚看见」的 Δt 单靠时间戳不可判定。Drew & Holt 与这些样本同向：锚点优先于时间邻接，在线状态最弱。
- **LLM 长上下文**【转述·仅作对照】：Liu et al. 2023 *Lost in the Middle* 只用来提「位置偏置」这个对照假设；其数字不是 Ling 参数，人类工作记忆容量（7±2）和 0.1/1/10 秒同样不进方案。

### 3.2 源码取证

| 项目 | 版本 / commit | 关键事实 |
|---|---|---|
| Ling | HEAD `de43cbe` | 入站窗口复用是身份键；`reply_to` 只作「冲她说的」布尔，不进提示词；消息级引用走 `idempotency_keys → [msg_id:token] → [quote:token]`，只标最近 12 条（`context.py:1965`）；`group_mention_on_reply` 是死配置（零读取点） |
| AstrBot | `4.30.0-beta.1` / `feb3e82c` | UMO=`platform_id:message_type:session_id`，会话与对话分离；历史无「最近 N 条」硬截，靠轮数/token 压缩；摘要真调 LLM；引用真注入；`/reset`、`/new` 真写穿 DB，无 `/clear` |
| MaiBot | `v1.4.8` / `edf4244` | session_id=`MD5(platform[_account][_scope]_(群号｜用户ID_private))`；模型层从新往旧按 `count_in_context` 计数，窗口=`max_context_size×2.0`；`/clear` 写 DB marker、清运行时、重启仍有效；启发式记忆注入默认关 |

**跨项目共识**：三家都把「会话身份」与「话题/对话内容」分开；MaiBot 与 AstrBot 都提供**显式**换题命令，都不靠自动判断话题边界来切窗口。这支持 Ling 不做新窗口，也提示「加一个聊天内换题命令」是可选补充而非第一期核心。

## 四、可复现的最小反例

样本集在 [ling-context-replay-samples.md](docs/evidence/ling-context-replay-samples.md)，A1–A8 覆盖：明确续聊 / 刚换题 / 异步引用 / 简单问候 / 短问号两位置 / 多相似旧话题 / 群聊交错 / 显式换题；判据 C1–C8。另一个更细的 308 行回放设计在 `docs/evidence/ling-context-research/replay/01-samples.md`。

判定纪律：**REAL_MODEL**＝带模型名/参数/完整 messages/response body；**SCRIPTED**＝规则推演，只能证明规则产生某字符串；**UNKNOWN**＝截图或口述，不得进判据。本轮**没有任何 REAL_MODEL 记录**，所以所有「当前行为」都是静态预测。

## 五、面向人的行为规则（第一期的目标行为）

1. 有引用 / @ / 时间+话题锚点 → 回看被引那条或锚点话题，展开目标进输入；目标已滚出可引用窗口就**标注悬空**，不静默接最近一条。
2. 短「？」「嗯」紧接一句 → 做局部修复，接眼前那句；同一句在隔天首条且无锚点 → 新开场，不续旧话。不设固定秒数阈值，用同句两位置配对校准。
3. 「后来呢？」→ 接最近话题（现状 `needs_recent_topic` 已做）；「接着说前天那个方案」→ 显式找回，锚点必须时间词与名词共现；只有「接着说」不算找回。
4. 问候 → 回应问候，不因问候把旧事拉进来；但**不能把问候从输入里删掉**，否则模型看不见要回应的那句。
5. 群聊 → 逐条 `sender_subject_id` 绑定；@ 是目标字段不是说话人；引用是消息→消息，不推出人；同名不同人靠人级 id 分开。
6. 旧话题恢复靠「可重识别内容 + 明确再进入动作」；有来源记录的承诺才主动带回，不凭相似度发明承诺。
7. 自然结束信号是「放弃展开 + 开新话题」，不是沉默时长；用户显式换题（「先别说那个」）应抑制旧题续接。

## 六、A / B / C 的取舍

| | A 现状 | B 当前轮取数改进 | C 明确上下文分段 |
|---|---|---|---|
| 做什么 | 保留现状做配对基线 | 信号识别 + 原文取数 + 召回 query + 摘要可见性标注 | 在 `annotated` 加话题/时间 lane，或物理分窗 |
| 收益 | 提供归因基准；零改造 | 直接针对显式找回、问候误召回、异步引用、群聊指人 | 理论上能降无锚点误续，但无因果证据 |
| 成本 | 0 | 信号识别、可能额外检索、输入长度变化、反例验收 | 数据模型/表达层改动；引用 id 有效期不会因此变长 |
| 当前证据 | 三次独立 compile、摘要常驻、问号跳过、问候不跳过均为【代码路径】/【实测】 | 【推理】收益方向明确但未跑回放 | 【推理】证据只支持「问题存在」，不支持「分段是解法」 |

**C 的结论：第一期不做。** 【实测】本轮核查 `replay/` 无任何结果文件、全目录 `REAL_MODEL` 零命中、无模型 API 环境变量；无话题边界标注、无误续/漏续人工标签、无 A/B/C 对照数值。按对抗判据的区分力要求，此时 A/B/C 在六个指标上不可区分，比较会退化成偏好陈述。C 的准入条件：B 的半成品红暴露「信号有但落点错」，再加 2×2 回放证明片段驱动存在。

## 七、推荐的最小改动（第一期）

前置动作不可跳过：**先跑 A 基线回放并抓 `prompt_snapshots`**，否则下面每一步的收益都无法归因。

1. **入站引用展开进提示词**（收益最明确，风险最低）。把 `envelope.reply_to` 解析出的被引用消息正文与说话人带进 `ContextCompiler` 的取数；保持群聊逐条署名不变；引用目标不可见时输出明确标注。落点：`bridge.py:863-877` 透传 → `context.py:548` / `1486-1520` 取数。
2. **问候不触发长记忆召回，但保留在输入里**。把「早上好/早/晚上好」类问候纳入 low-information 跳过集合，同时确认最近原文窗口仍含该问候。注意：这一步**不能**替代第 3 步——M2 变异（只加问候跳过、不改问号）必须红。
3. **显式找回锚点识别 + 精确原文定位**。识别「时间词 + 话题名词」与元话语「刚看到/回晚了 + 名词」，用它定位原文而不是跑相似度；「接着说」单独出现不触发。落点：`context.py:1550-1575` 的 query 构造、`conversation/service.py` 的取数。
4. **摘要只加来源/时间标注，不删、不改 TTL**。在摘要块上显式标「较早、非当前指代对象」，只在有回看锚点时强调。`context.py:230-244` 的注释记录了旧实现「摘要跟短记忆一起过期」导致「她完全不记得聊过什么」的回归，不要重犯。
5. **不做**：新窗口、话题分段（C）、固定秒数阈值、凭 `group_mention_on_reply` 改 @ 行为（已确认是死配置）。聊天内换题命令可作为可选补充，但 AstrBot/MaiBot 的做法是显式命令，不是自动分段——先有回放再决定。

这些是**改动提案**，不是已验证的修复。没有 REAL_MODEL 证据前，不能声称任一问题已解决。

## 八、量化验收与最值得补的实验

六个评分维度：误提旧事、主动换题自然度、真需续聊却漏掉、指错人/指错消息、输入长度、额外调用。全部写成条件式（例如「3 秒→bridge 正常消费；8.7 秒→有额外开销」这种写法），不抄某次运行值当期望值。

三个**半成品必须红**的变异（设计在 [ling-context-adversarial.md](docs/evidence/ling-context-adversarial.md)，尚未执行）：

- M1 只改最近轮取数、不改摘要 → A2/A4 的误提旧事与输入长度应红；
- M2 只给问候加跳过、不改问号 → A5 两位置的区分力应红；
- M3 只加分段标签、摘要仍无条件注入 → A2/A8 应红。

每条负向断言都配了正向断言，防止「功能完全没接线」时假绿。**变异→跑→恢复→sha256→git status** 的固定收尾动作尚未做，因为无被测改动。

**最值得补的一组实验**：2×2 对照——旧事片段（有 / 无）× 本轮是否真延续（是 / 否），同一模型、同一模板、同一历史，只改锚点。四格走向分别区分「片段驱动 / 意图驱动 / 模型惯性 / 模板驱动」；缺的是空记忆对照组。前置条件：真实模型输入/回复、人工话题边界与误续标签、双评分者、A/B/C 长度与调用口径单次标 N=1。

## 九、未取得数据与偏差

- **无真实模型 API 与 prompt 快照**：所有回复归因都是 SCRIPTED / UNKNOWN。四项需求来自摘要、原文还是「早上好」召回，未定论。
- **无真实用户 IM 数据**：全部样本 synthetic，QQ 号 `100200300` 形态，未连生产库/NAS。
- **论文原文缺口**：Sacks 1974、Schegloff 1977（仅摘要）、Grosz & Sidner 1990、Clark 1996 章节、three floors、中文自然会话 CA 语料；Wikipedia/ACM/Google 多处 403/404/超时，均已列在各证据文档失败项。
- **动态验证缺口**：AstrBot/MaiBot 只做静态代码取证，未起实例；MaiBot token 裁切未端到端实测；Ling 未跑 pytest/回放。
- **子智能体路由偏差**：运行时 `list_subagent_models` 只返回 `nasfree`，显式 `nasfree/nasfree` 被拒绝，实际派发的成员显示 `model=hy4flash`（会话默认路由），未按旧约定走 DeepSeek 白名单。这是运行时约束，未绕过。
- **未用批量 git add / commit；无 stash**；所有产出均为未跟踪证据文档。

## 十、文档索引与关键路径

- 汇总与判据：[replay-samples.md](docs/evidence/ling-context-replay-samples.md)、[baseline.md](docs/evidence/ling-context-baseline.md)、[adversarial.md](docs/evidence/ling-context-adversarial.md)、[plan-b.md](docs/evidence/ling-context-plan-b.md)、[plan-c.md](docs/evidence/ling-context-plan-c.md)
- 人类对话：`ling-context-human-{turns,topics,grounding,events}.md`
- 即时通讯：`ling-context-im-{private,group,async,offline}.md`
- 源码：`ling-context-src-{astrbot,maibot,ling-inbound,ling-outbound}.md`
- 补充设计材料（未跟踪）：`docs/evidence/ling-context-research/`、`docs/research/`

关键源码行号（HEAD `de43cbe`）：`bridge.py:1479-1635`（窗口复用）、`bridge.py:863-877`（编译调用）、`context.py:81/83/93/384`（预算与引用上限）、`context.py:1486-1520`（原文分页）、`context.py:1550-1575`（召回 query）、`context.py:1673-1685`（摘要注入）、`context.py:1965`（引用 id）、`turn.py:1897-1958`（prompt 快照）。

可复现命令：

```bash
.venv/bin/python -c "from ling.domains.memory.recall_query import *; \
print([(s, should_skip_automatic_recall(s), needs_recent_topic(s)) \
for s in ['？','早上好','后来呢？','接着说前天那个方案']])"
git log -1 --format='%H %s'
shasum -a 256 src/ling/kernel/bridge.py src/ling/kernel/context.py
```

外部来源：AstrBot `https://github.com/AstrBotDevs/AstrBot/tree/feb3e82c3ed1a1d7fdd202f7b7711657e34b81ea`；MaiBot `https://github.com/Mai-with-u/MaiBot/tree/edf42446f123149c57fc79765dd5a8b004b8524c`；Clark & Brennan `http://www.psychology.sunysb.edu/sbrennan-/papers/old_clarkbrennan.pdf`；Grosz & Sidner `https://www.ideals.illinois.edu/items/17595/bitstreams/63256/data.pdf`；Sarkar et al. 2025 `https://aclanthology.org/2025.acl-long.161.pdf`；事件边界四篇见 `ling-context-human-events.md` §5 的 PMC 链接清单。
