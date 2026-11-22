# 对抗验收报告 · 附录 B：source A/B 复核与须修项回归验证

> 本文是 `ADVERSARIAL-REPORT.md` 的附录 B；判据同 `ADVERSARIAL-CRITERIA.md`。
> 写于 2026-11-22（Asia/Shanghai），HEAD 复核仍 `de43cbe79a78e0c01f7821470500eaa04ea79497`。
> 审查对象：source/A-astrbot-upstream.md、source/B-maibot-upstream.md；
> 回归验证：human/03、im/cross-session-async.md、im/private-chat.md、options/02-options.md 的须修项。

---

## B1. 须修项回归验证（全部到位，实测）

| 原发现 | owner 修正 | 我的复核结果 |
|---|---|---|
| human/03 L132/L146 把「？」绑定写成事实+加粗，与 human/01/02"不能唯一判定"冲突 | 改为"两种解读皆可能，真实绑定需提示词快照／回放判定"；L133 给出两种分支的各自后果；一句话结论（L192）同步改写 | **到位**：L132/133/192 三处均改，且不再是加粗断言；与 human/01 L97/L124、human/02 L180 现在一致 |
| im/cross L94 "硬结论"+"最容易召回显著旧事"结果断言 | 拆成"代码路径事实（[代码]）"与"机制推测，非结果断言（[推理]，当前未测）"，明写"仅凭走宽泛召回推不出显著旧事最容易被召回，那会把路径存在误当结果发生" | **到位，且修正文本本身正好复述了我的对抗判据**（A5/V1）；private L99-103 同样重写 |
| options/02 L38 "M0 的信号优先级"悬空引用 + SEP 未列来源 | 删除 M0；改为"会话含义的可取消性……不能写成无条件默认"，并给出 SEP 链接题名与本地转述出处 `docs/evidence/ling-context-im-private.md:23-27` | **到位**：M0 全文零命中；SEP 现已可回溯到具体文档与行号，符合 S1/S7 |

**残留检索**（我执行的 grep）：`错误绑定|硬结论|M0 的信号优先级|最容易召回显著旧事` 在 human/、im/、options/、source/、replay/ 范围内**零命中**。
注意：这只证明字符串级修复到位；我逐段读了新旧上下文（见上表），确认语义也真的改了，不是只换词。

**结论**：原 `ADVERSARIAL-REPORT.md` §5 行动建议第 1、2 条与 `ADVERSARIAL-APPENDIX-A.md` A1.2 的须修项**已关闭**。

---

## B2. source A（AstrBot 上游）审查

### B2.1 能证明什么（实测）

- **S1/S4 版本钉住合规**：我用 GitHub API 复核 `https://api.github.com/repos/AstrBotDevs/AstrBot/git/ref/tags/v4.30.0-beta.1` → SHA `90ba3270f09b857063da68908d80f742042067cd`，与 A §1 表格逐字一致（实测）。A 明确声明 `git clone` 因网络超时失败、改用 raw 文件逐篇获取、API 曾 403——**失败来源如实申报**，符合 S4。
- **配置存在 ≠ 主链接线的正面示范**：A §3.3 逐跳列了 9 步调用链（schema → from_config → execution → runner → compressor），结论"compression.* 确实在主链生效"；同时 §4/§6 Q4 把 `provider_ltm_settings.*`、`unique_session`、dataclass 默认 `50/10` 明确列为"存在但未生效/默认关闭/被覆盖"。这正是我 S/V 判据要的区分。
- **否定结论有搜索口径**：A §6 Q2 给出 grep 模式 `topic.?switch|话题切换|新话题|new topic|drift` 与覆盖范围（20+ 核心文件），并区分"segmented_reply 是输出拆段、MessageSegment 是结构名"两个假命中。按 A5，这比"我搜了没有"强得多。
- **对 Ling 问题的直接回答完整**：Q1 旧内容来自整段 conversation.content；Q2 自然漂移 + `/reset`/`/new` 手动；Q3 上游不保证话题隔离；Q4 未接线配置表——四项都给证据。

### B2.2 不能证明什么

- **A 是上游项目取证，不是 Ling 行为证据**。它证明"上游机制如此"，**不能证明** Ling 采用了、或应当采用该机制；任何"AstrBot 这样做所以 Ling 应该……"的推论都需要额外论证（S2 的外推标注）。
- **声明的未核实项真实存在**：各平台适配器如何生成 `session_id`（A §2.3）、`unique_session` 精确消费分支（§6 Q4）、生产实例实际小版本（§8 第 10 条）——这些恰是判断"群/私聊身份粒度"的关键，缺失使 A 的"群聊所有人共享 conversation"结论**只在核心层成立**，未覆盖适配器差异。
- **无运行期证据**：A 的所有结论是静态源码 + 官方文档，没有一次实际 LLM 请求日志。按 V1/V2，"历史整段进入 req.contexts"是代码事实；"模型实际看到了旧话题并续接"未测。
- **A §3.4 的 dataclass 默认 50/10"不生效"是推理**（置信度高但非实测）：它依赖"`from_config()` 总是显式传值"这一前提；若有自定义调用方绕过，结论反转。文档已标"推断，置信度高"，合规，但下游不得当实测。

**置信度**：版本 pin 与配置链＝**实测（我复核 pin + 文档自述的逐跳链逻辑自洽）**；上游运行期行为＝**未测**；对 Ling 的外推＝**推理**。

---

## B3. source B（MaiBot 上游）审查

### B3.1 能证明什么（实测）

- **版本 pin 合规**：我用 `https://api.github.com/repos/Mai-with-u/MaiBot/releases/latest` 复核：`tag_name=v1.4.8`、`target_commitish=edf42446f123149c57fc79765dd5a8b004b8524c`、发布日期 `2026-11-20T08:07:28Z`，与 B §1.2 表格逐字一致（实测）。B 还如实记录最初 `MaiMaiBT/MaiBot` 404、通过搜索 API 定位规范仓库，以及 `docs.maibot.chat/manual/chat-scenes/` 一次 fetch 失败未据其下结论——**失败申报完整**（S4）。
- **否定结论有搜索口径**：B §6 Q2 对"无话题切换检测"给出具体反例排除（`behavior_generic_tags.py:279` 是学习标签、`attention_drift.py` 是默认关的 prompt 文案、跨日提示只告知日期），不是空泛断言（A5）。
- **"配置存在 ≠ 主链生效"处理得好**：§4.3/§6 Q4 把 `a_memorix.plugin.enabled=False`（总开关默认关）、启发式记忆默认关且关闭时直接返回空串、画像注入依赖总开关有数据——分三层讲清楚，没有把 schema 默认值当运行期行为。
- **tarball 逐行取证**：B 明确"下文所有 文件:行号 均指 pinned commit"，且下载到 `/tmp/maibot-forensics/`（仓库外），不污染工作区。
- **与 Ling 的同构/差异对照有分寸**：B 结论 7 指出"MaiBot 摘要只在运行时内常驻、重启/`/clear` 丢弃，而 Ling 摘要落库长期保留"——这是**基于 D/C 组源码的对照**，边界说明到位。

### B3.2 不能证明什么

- **同样是上游取证，不是 Ling 行为证据**。B 的"常驻摘要无条件注入"证明的是 MaiBot 的机制；Ling 的对应结论来自我复核过的 `context.py:1674-1685`（D 组），两条独立代码库的事实，不能互相替代。
- **B §3.2 的 2× 系数（选窗 100/150）与轮末裁切到 10/15 是代码推导，不是观测到的稳态**。文档自己写"稳态下实际可见消息远少于 100"（§3.2 结论）——这是**推理**，没有运行日志支撑。
- **B §4.2 "摘要不写数据库"是负向 grep**（置信度中），证明力限于所查目录 `src/common/database`、`src/services/`；若持久化在别的层（如缓存/导出），本结论不覆盖。按 A5 应标"未发现"而非"不存在"——B 已标"负向观测，置信度中"，合规。
- **无真实模型输出**，同 A。

**置信度**：版本 pin 与配置链＝**实测（pin 我复核）**；运行期稳态窗口/记忆行为＝**推理或未测**；对 Ling 的外推＝**推理**。

---

## B4. 上游三份（A/B）+ Ling 两份（C/D）的交叉裁决

| 维度 | AstrBot (A) | MaiBot (B) | Ling (C/D) |
|---|---|---|---|
| 会话/窗口身份 | UMO 三段串，默认一 UMO 一 conversation | MD5(平台+路由+群/用户)，群不含发言人 | `(actor,channel,window_ref,instance_key)`，群不含发言人 |
| 旧原文来源 | 整段 conversation.content（无固定 N 条） | 运行窗口选 2×50，轮末裁到 ~10 | `messages_for_prompt` 按摘要水位 + 20k token 预算 |
| 摘要常驻 | token 82% 触发 LLM 压缩（按需） | `ContextSummaryMessage` 每轮无条件注入 | `window_contexts.summary` 每轮无条件注入、不受 TTL |
| 显式话题切换 | 无；靠 `/reset`/`/new` 手动 | 无；`/clear` 不换 session | 无；入站无新窗入口，sealed 是隔离档位 |
| 配置接线 | `compression.*` 真接主链（逐跳） | 窗口配置真接主链；记忆多为默认关旁路 | `group_mention_on_reply` 只存不用；其他主路径配置真消费 |

**对抗结论（实测）**：四份取证在"**没有话题级自动切窗、旧话题靠自然漂移与常驻摘要留在上下文**"这一点上相互印证。但按 A3 的区分力要求，必须指出：**这不是四次独立实验，而是四个代码库（其中 C/D 是同一库）的静态观察**。它们能共同支持的只是"这是此类系统的普遍设计形态"，**不能**替代"Ling 在某次具体回放中因摘要常驻而误接旧事"的证据——后者仍需 annotated 快照。

**对竞争解释排序的影响（小）**：A/B 提供了"常驻摘要无条件注入"在两个上游项目中的同构证据，使 E1（召回/上下文驱动）的可信度略升；但它们都没有解决我在主报告 §4 指出的核心区分问题（片段驱动 vs 意图驱动 vs 模型惯性），因此**排名不变**，2×2（现 2×2×2）补实验仍是最优下一步。

---

## B5. 当前状态与待续

- 本附录写毕：`git log -1` = `de43cbe...`；我方文件仅 `docs/evidence/ling-context-research/review/` 下 4 份（判据、主报告、附录 A、附录 B）。
- 已关闭须修项：human/03、im/cross、im/private、options/02（B1 逐条验证）。
- **仍待**：samples-options 的 task-4 基线/变异产物落盘。收到路径后，我会重点核对：
  1. 真实模型输出 vs SIMULATED 是否分栏；
  2. C1–C10（尤其 C2 评分协议、C5 输入长度、C6 额外调用）是否填了有口径的数；
  3. 2×2×2 四格是否覆盖 E1/E3/E5/E6 的区分；
  4. 半成品红（B-half-1/2、C-half-1/2）是否真的红，且变异后 sha 恢复、`git status` 干净。
