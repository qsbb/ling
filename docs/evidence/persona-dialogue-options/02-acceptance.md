# 人设三个对话开关：独立验收报告

验收方：独立第三方（与开发分离）。时间：2026-11-22 13:23–13:27 +08:00。

冻结对象分支：`lingxi/persona-dialogue-options`，HEAD `bd5ac914388abce6b7ae2be721dd58d64d7b7ce7`（`git log -1` 实测为 `bd5ac91 fix(context): 收紧日常对话上下文`）。

判据来源：先只读 `docs/evidence/persona-dialogue-options/01-freeze-criteria.md`，写出判据清单与每条的错误实现失败方式（见本报告 §1），再读业务实现与测试。本文件是该顺序的产物。

---

## 0. 观测对象钉住（实测）

验收开始时（任何变异之前）的 `git status --short`：

```
 M clients/web-console/src/components/PersonaPage.tsx
 M clients/web-console/src/persona.browser.test.tsx
 M clients/web-console/src/types.ts
 M src/ling/api/routes/actor.py
 M src/ling/api/routes/persona.py
 M src/ling/contracts/v1.py
 M src/ling/domains/persona/service.py
 M src/ling/infra/models.py
 M src/ling/kernel/context.py
 M src/ling/kernel/prompt_zones.py
 M tests/unit/test_prompt_zones.py
 M tests/unit/test_w2_rules.py
?? docs/evidence/persona-dialogue-options/
?? migrations/versions/0083_persona_dialogue_options.py
?? tests/integration/test_persona_dialogue_options.py
```

这 14 项是开发方尚未提交的改动，属验收输入。注意 `docs/evidence/persona-dialogue-options/00-baseline-sha.txt` 里记录的 sha 与当前文件 sha **全部不同**——这是基线记录后又迭代过的正常结果；本报告一律以**实测当前 sha** 为准，不用那份旧文件里的值。

被测文件验收开始时的 sha256（实测）：

```
430f40e1ef9220a79e0911af0c7f4fd563f84638072803c3eb6a4330bce09ba2  src/ling/kernel/context.py
4c07e54e491e8c46b15fd923a2f836acfea4d97ca079bab3010e64009061032b  src/ling/api/routes/actor.py
bd38280b40f7aadc034868a7fbf5a6ced2db0ece22614afa1cf4bfc5561f6681  src/ling/domains/persona/service.py
72f0aae320687b886004ca48e534039eacde7cb953744a6146d1572da5cc26b3  tests/integration/test_persona_dialogue_options.py
5acbefd41ba8131e34a0354d8ad2bf35d5bea43a2c80bc2e79d8687c06f6d3f1  migrations/versions/0083_persona_dialogue_options.py
```

环境（实测）：`/Users/lingxi/project/ling/.venv/bin/python` 可用；`/tmp/ling_test_lock.sh` 的 `acquire_test_lock` / `release_test_lock` 可用；每次 pytest 都设了 `LING_TEST_DATABASE_URL` 与 `LING_DATABASE_URL` 两个变量、持锁、输出 `tee` 到文件。启动前 `ps -eo pid,etime,comm | grep "\.venv/bin/python"` 显示两个运行 6h/7h 的长期 python 进程，锁目录 `/tmp/ling_test_lock.d` 当时为空——这两个进程未持该锁，与本验收的测试库互斥无关（**推理**：推测是常驻服务，未核实其具体身份）。

基线（实测）：未改动任何文件先跑 `pytest tests/integration/test_persona_dialogue_options.py -q` → **6 passed**，日志 `/tmp/persona_dialogue_acceptance/baseline.log`。基线绿，具备做变异的前提。

日志目录：`/tmp/persona_dialogue_acceptance/`：

| 文件 | 内容 | 退出码 |
|---|---|---|
| `baseline.log` | 变异前基线 | 0（6 passed） |
| `mut1_no_assembly.log` | 变异 1：删装配 | 1（3 failed, 3 passed） |
| `mut3_bool_default_false.log` | 变异 3：三态改 bool | 1（1 failed, 5 passed） |
| `mut4_guard_outside_empty.log` | 变异 4：空人设外注入 | 1（1 failed, 5 passed） |
| `mut6_update_resets_others.log` | 变异 6：update 重置另两个开关 | 1（1 failed, 5 passed） |
| `final_rerun.log` | 全部恢复后复跑 | 0（6 passed） |

---

## 1. 判据清单（看实现之前写出，逐条附错误实现的失败方式）

（摘自验收开始时的判据草稿，本处原样保留其核心）

- **A1 默认值/兼容**：迁移后旧行三列当 false 读出；老 API 省略三字段不改原值；读接口始终返回布尔。错误实现失败方式：迁移未回填→读到 None；漏默认值→省略时抹成 False 或报错；`exclude_none`→字段消失。
- **A2 三态区分力**：显式 `False` 关掉已开开关，`None`（省略）保持原值，两者结果必须不同。错误实现失败方式：字段改 `bool` 默认 False 或 `bool()` 包装→省略也关掉；一律当"没传"→显式 False 关不掉。
- **A3 独立列**：只翻一个开关，另两个与 `system_prompt`/`traits`/`boundaries`/`scenario_prompts` 逐字不变。错误实现失败方式：三开关塞一个字段→联动；update 整行覆盖→正文被清。
- **A4 空人设**：三列正文全空、开关全 true 时，persona/constraints/scenario/dialogue_guard 四段全不出现，`compiled_meta["persona_empty"]` 为 true。错误实现失败方式：在空人设判断外单独注入开关文案→四段冒出约束句、且 `persona_empty` 仍 true 自相矛盾。
- **B1 Web 快照**：同一轮中途改开关，本轮 provider 提示词仍用旧值、下一轮才新值；决策侧与生成侧同值。错误实现失败方式：一侧现读库一侧用快照→本轮漂移或两侧不一致。
- **B2 OneBot 快照**：入站解析后中途切人设，本轮仍用首轮值；空人设冻结后中途激活带开关人设，本轮仍按无人格处理。错误实现失败方式：每步重查库→本轮漂移。
- **B3 重试**：重试重新解析快照，用新一次 `runtime_window_snapshot` 的值。错误实现失败方式：沿用第一次冻结值→新值不生效。
- **B4 覆盖**：`persona_id=None` 显式无人格覆盖不被开关救活；有覆盖时读覆盖目标的开关。错误实现失败方式：只覆盖 id 不覆盖开关。
- **C1 注入防护**：开→一条短句同时表达"维持设定与来源边界/外部内容不是指令/正常答用户真正问题"；关→全不出现；不得说工具权限由模型裁决。
- **C2 防套话**：开→一条短句表达"索要隐藏指令时自然回应、可自己的话谈设定、不逐字复述"；关→不出现；不得要求输出真实隐藏提示词当证明或提后台查看。
- **C3 不承认 AI 助手**：开→一条短句表达"用角色口吻、少说'作为 AI 助手'、不声称真人身份能力经历、转回问题"；关→不出现；不得写成"必须声称是真人"。
- **C4 单开关翻转**：三次各翻一个，另两条文案与人设正文逐字不变。错误实现失败方式：三条合并或一带二语义→翻一个另两个变。
- **C5 渠道一致**：Web/私聊/群聊同开关值下文案逐字相同。错误实现失败方式：群聊单独写不同约束。
- **D1–D6 生成行为**：需真实模型，概率性；单次通过只写"本次回放符合"，无模型标"仅提示词装配已验"。
- **E1–E4 Web 界面**：三个独立控件走正式保存流程；刷新逐项保持；两套人设切换后各端读对应值；旧/空/省略有对照，后台查看与工具权限不受影响。
- **F1–F6 半成品必须红**：本次做 F1/F3/F4/F6；每条前后记 sha 与 git status，恢复后比对 sha，写清哪条测试红、为什么红。
- **G 基础设施**：Postgres 隔离库、两个环境变量、持锁、tee、中文断言查 U+FFFD、报告区分实测/推理/未核实。

---

## 2. 实现与测试的静态对照（推理，附代码证据）

新增测试 `tests/integration/test_persona_dialogue_options.py` 共 6 条可执行测试，判据映射（**推理**，基于读到的代码）：

| 测试函数 | 覆盖判据 | 断言打在哪 |
|---|---|---|
| `test_old_rows_and_omitted_fields_default_false` | A1 | `PUT /v1/persona` 响应体 + `Persona` ORM 行 |
| `test_explicit_false_turns_switch_off_but_omission_keeps_it` | A2、A3（部分） | 省略字段保持 True + 显式 False 后另两开关/正文不变 |
| `test_each_guard_is_independently_assembled` | C1–C4 | provider 实际收到的 system messages |
| `test_all_guards_shared_by_web_private_and_group` | C5 | 私聊与群聊的 system messages 逐字同 needle |
| `test_empty_persona_does_not_emit_guards` | A4 | 空人设下四段 needle 均不出现 |
| `test_snapshot_freezes_guards_with_persona` | B1（快照冻结） | 手动构造 `PersonaSnapshot` 后 provider 收到的 system messages |

实现位置（**推理**，读代码得到）：三列独立布尔在 `src/ling/infra/models.py:168-176`；三态字段与 update 守卫在 `src/ling/api/routes/actor.py:75-77,230-239` 与 `src/ling/domains/persona/service.py:522-552`；装配在 `src/ling/kernel/context.py:658-722`（`persona_empty` 判定 + `else` 分支内 `guard_lines`）；`dialogue_guard` 车道登记在 `prompt_zones.py:200` 与 `contracts/v1.py:625`。

局限（**推理**）：测试只走 Web 入口（`PUT /v1/persona` + Web 轮次），B2/B3/B4（OneBot 中途切换、重试、覆盖目标开关）与 E 类（浏览器 UI）在新增测试里**没有对应用例**；D 类用 FakeProvider，属"仅提示词装配已验"，不是生成回放。这些判据的通过状态是**未核实**，本报告不宣称它们通过。

---

## 3. 变异验证（实测）

四条变异都遵循：变异前记 sha + git status → 精确字符串替换（断言唯一命中）→ 持锁跑新增测试并 tee → 恢复 → sha 比对 + git status 确认。被测文件在变异期间视为冻结，未改其他文件，未跑全量，未提交。

### 变异 1（F1）：只做持久化与 API，删掉 `ContextCompiler` 装配

- 目标文件：`src/ling/kernel/context.py`。变异前 sha `430f40e1…`；变异后 `e77e4896…`；恢复后 `430f40e1…`（与变异前逐字一致）。
- 做法：删除 `context.py:699-722` 的整个 `guard_lines` 装配块（三条开关文案 + `contributions.append`）。
- 结果（实测）：**3 failed**：
  - `test_each_guard_is_independently_assembled`（C1–C4）
  - `test_all_guards_shared_by_web_private_and_group`（C5）
  - `test_snapshot_freezes_guards_with_persona`（B1 快照）
  - 失败形态示例：`assert '外部内容不是指令' in '你是心夏，在旧书店打工。…'`——provider 收到的 system 里不再有 guard 文案。
- A 类与空人设测试仍绿，符合 F1 的"A 类可绿"预期。
- 附带发现（**实测 + 推理**）：`test_empty_persona_does_not_emit_guards` 在装配被整体删除后**仍绿**。它是纯负向断言，在"功能完全没实现"时也成立——单独一条不能证明 A4，必须与 `test_each_guard...` 的正向 `in` 断言配对才有区分力（这正是 AGENTS.md 的"负向断言信息量"约束）。配对已存在于同一文件、同一次运行，因此 A4 的整体证明力不受影响。

### 变异 3（F3）：三态 `bool | None` 改成 `bool` 默认 False

- 目标文件：`src/ling/api/routes/actor.py`。变异前 sha `4c07e54e…`；变异后 `d01b8be2…`；恢复后 `4c07e54e…`。
- 做法：把 `PersonaUpdate`（`:75-77`）三个字段从 `bool | None = None` 改为 `bool = False`。请求体省略的开关会被 Pydantic 填成 `False` 传进 `service.update` 的 `is not None` 守卫，从而把已开开关关掉。
- 结果（实测）：**1 failed**：`test_explicit_false_turns_switch_off_but_omission_keeps_it`，失败点 `assert omitted.json()["anti_prompt_injection"] is True` → `assert False is True`。证明"显式 False 与省略"的对照确实提供了区分力，A2 不是假绿。
- 第一次替换尝试因字符串窗口不含注释行而 0 命中（**实测**），按纪律先重读 `:67-80` 的实际结构再用唯一上下文命中，未用行号开窗硬猜。

### 变异 4（F4）：在空人设分支外单独注入开关文案

- 目标文件：`src/ling/kernel/context.py`。变异前 sha `430f40e1…`；变异后 `6aaf40e6…`；恢复后 `430f40e1…`。
- 做法：在 `if persona_empty:` 之前插入一段无条件 guard 注入（三个开关文案 + `ContextContribution(lane="dialogue_guard")`），模拟"绕过空人设判断单独注入"。
- 结果（实测）：**1 failed**：`test_empty_persona_does_not_emit_guards`，失败点 `assert '外部内容不是指令' not in text`。日志同时出现 `本轮未注入人设（persona_id=persona_ling）` 的 warning：`persona_empty` 判定仍为 true，但 guard 文案已冒出——正是 A4 要防的矛盾。证明该测试抓得到"空人设外注入"这个错误实现。

### 变异 6（F6）：让 update 写回时重置另两个开关

- 目标文件：`src/ling/domains/persona/service.py`。变异前 sha `bd38280b…`；变异后 `49129226…`；恢复后 `bd38280b…`。
- 做法：把 `update()` 里三个 `if x is not None: persona.x = bool(x)` 守卫去掉，改为无条件 `persona.x = bool(x)`；省略字段（`None`）会被写成 `False`。
- 结果（实测）：**1 failed**：`test_explicit_false_turns_switch_off_but_omission_keeps_it`，失败点同样是 `assert omitted.json()["anti_prompt_injection"] is True`（省略字段被重置）。
- 重要限定（**实测 + 未核实**）：这条测试**先红在 A2 的"省略保持 True"断言**，测试在此中断，后半段"A3：显式 False 后另两开关与正文不变"的断言**没有执行到**。因此本次变异 6 只能证明"该测试能抓到 update 无条件重置开关"这一整体事实，**不能单独证明 A3 后半段断言的区分力**。A3 后半段是否真能抓"只重置另两个、不碰省略的那个"这一更窄的缺陷——**未核实**（需另做一次更聚焦的变异，超出本次四条的要求，未做）。

### 变异收尾（实测）

全部恢复后再次记录三个被测文件 sha，与变异前逐字一致：

```
430f40e1ef9220a79e0911af0c7f4fd563f84638072803c3eb6a4330bce09ba2  src/ling/kernel/context.py
4c07e54e491e8c46b15fd923a2f836acfea4d97ca079bab3010e64009061032b  src/ling/api/routes/actor.py
bd38280b40f7aadc034868a7fbf5a6ced2db0ece22614afa1cf4bfc5561f6681  src/ling/domains/persona/service.py
```

最终 `git status --short` 与验收开始时逐字一致（14 项，见 §0），无我的变异残留。最终复跑 `pytest tests/integration/test_persona_dialogue_options.py -q` → **6 passed**（`/tmp/persona_dialogue_acceptance/final_rerun.log`），与基线一致，排除变异间污染。

按 AGENTS.md 的纪律用 grep 搜 U+FFFD 替换字符（U+FFFD）对测试与三个被测实现文件 → **无损坏字符**（实测）。

---

## 4. 每条判据：能证明什么 / 不能证明什么

| 判据 | 本次能证明什么（实测/推理） | 不能证明什么（未核实/局限） |
|---|---|---|
| A1 默认值/兼容 | 实测：`test_old_rows...` 基线绿；变异 3、6 均不动它（绿）。迁移文件 `0083` 用 `server_default=sa.false()` + `nullable=False`（推理：旧行回填 false 由 DDL 保证） | 未单独变异"迁移未回填/读接口 exclude_none"这些错误实现；旧库真实升级路径未跑；A1 的"完整证明"仅限本测试覆盖的形态 |
| A2 三态区分力 | **实测强证据**：变异 3（字段改 bool 默认 False）与变异 6（update 无条件写回）都使 `test_explicit_false...` 红在"省略保持 True"。证明显式 False 与省略确实被区分 | 无法区分变异 3 与变异 6 各自错在哪一层（都在同一断言红）；这是错误实现的归因问题，不影响 A2"测试能抓错"的结论 |
| A3 独立列 | 实测：`test_explicit_false...` 后半段有"另两开关仍 True + 正文逐字不变"的断言；基线绿。**变异 6 未走到这段** | **未核实**：该后半段断言单独能否抓到"只重置另两个开关"的更窄缺陷；三开关塞进一个字段的错误实现未变异；update 整行覆盖正文的错误实现未变异 |
| A4 空人设 | **实测强证据**：变异 4（空人设外注入）使 `test_empty_persona...` 红；且该测试在变异 1（装配整体删除）时仍绿——说明它是纯负向断言，靠同文件正向断言配对才有区分力 | 未断言 `compiled_meta["persona_empty"] is True` 本身（测试只看文案不出现）；"空人设四段全不出现"里的 scenario 段未在测试 needle 里直接体现（推理：空人设下 scenario 本就不发，测试用 `"角色名："/"叙事边界："` 覆盖 constraints，未显式构造场景覆盖） |
| B1 Web 快照 | 实测：`test_snapshot_freezes_guards...` 基线绿；变异 1（删装配）使它红——证明它依赖真实装配、且快照冻结的开关值进入了 provider 提示词 | 该测试是手动构造 `PersonaSnapshot` 并注入 `persona_id=snapshot`，**不是**"同一轮中途经 HTTP 改开关再观察本轮"的真实路径；"下一轮才含新值""决策侧与生成侧同值"这两半**未核实** |
| B2 OneBot 快照 | 无对应测试；新增测试全走 Web 入口 | **未核实**。不在本次实测范围 |
| B3 重试 | 无对应测试 | **未核实** |
| B4 覆盖 | 无对应测试 | **未核实** |
| C1/C2/C3 文案语义 | 实测：`test_each_guard...` 用 `INJECTION_LINE/LEAK_LINE/AI_LINE` 三个短 needle 做逐开关的 `in`/`not in`；变异 1 使它红——证明三条文案确实由各自开关控制、且打在 provider 实际 messages 上 | 测试只匹配短子串，不断言"一条短句同时表达三件事""关时该句完全不出现（而非换行拆分）""不把工具权限说成模型裁决"等完整语义；这些细节**未核实**，只能靠读实现（推理：`context.py:702-711` 的文案形状符合判据） |
| C4 单开关翻转 | 实测：变异 1 使 `test_each_guard...` 红；测试每轮先把三开关显式归零再只翻一个（`:178-184`），避免了依赖上一轮状态 | "文案合并成一条/一个开关带另一语义"的 F2 变异本次**未做**（冻结判据 F2 明确要求，但用户本次只点名 F1/F3/F4/F6）；C4 对这种特定缺陷的区分力**未核实** |
| C5 渠道一致 | 实测：`test_all_guards_shared...` 分别取私聊与群聊 system messages、对三 needle 双向断言；变异 1 使它红 | 测试用 `visibility="group_public"` 改会话可见性，**未把 window.channel 真正改成 onebot/qqbot**（推理：群聊路径仍走 Web 轮次）；"F5 群聊文案改不同字符串"的变异本次未做；跨真实渠道的逐字一致**未核实** |
| D1–D6 生成行为 | 无真实模型；`FakeProvider` 只回显 | **仅提示词装配已验**，生成行为全部未核实 |
| E1–E4 Web 界面 | 无浏览器测试运行；`clients/web-console/src/persona.browser.test.tsx` 有改动但本次未跑 | **未核实** |
| F1 | 实测：删装配 → C1–C5 与快照测试红、A 类绿，符合冻结判据 F1 | — |
| F3 | 实测：三态改 bool → A2 测试红，符合 F3 | — |
| F4 | 实测：空人设外注入 → A4 测试红，符合 F4 | — |
| F6 | 实测：update 无条件重置 → 该测试红；但红在 A2 断言而非 A3 后半段 | A3 后半段的独立区分力未核实（见上） |
| G 基础设施 | 实测：两个 DB 变量、持锁、tee、U+FFFD 检查均执行；6 份日志落盘 | 未跑全量（用户明确要求不跑）；并发持锁争用场景未触发 |

---

## 5. 结论

- **四条指定变异（F1/F3/F4/F6）全部产生了预期的红**，且恢复后 sha 逐字一致、git status 与验收开始时逐字一致、复跑全绿。就"这些测试不是假绿、能抓到它声称要抓的错误实现"这一点，本次验收给出**实测支持**。
-  verdict（**推理**）：新增测试对 A2、A3（部分）、A4、B1（部分）、C1–C5 的装配与持久化形态提供了有效覆盖；对 A3 后半段的独立区分力、C1–C3 的完整文案语义、C4 的"合并文案"缺陷、C5 的真实跨渠道、B2/B3/B4、D 类、E 类**未给出实测结论**。
- 冻结判据里点名但本次未做的 **F2（合并文案→C4 红）** 与 **F5（群聊不同文案→C5 红）** 仍待补做；本报告不把"未做的变异"写成通过。
- 全程未提交、未跑全量、未在变异完成前改其他文件。

---

## 6. 开发方补做的 F2 / F5 变异（2026-11-22 13:28–13:31 +08:00）

验收报告 §5 指出 F2/F5 未做。开发方随后补做，并将结果写入本节。变异文件 `src/ling/kernel/context.py` 每次前后记录 sha256，最终恢复为
`430f40e1ef9220a79e0911af0c7f4fd563f84638072803c3eb6a4330bce09ba2`。

### F2：三条文案合并成一条（只保留防注入句）

- 变异：删除 `guard_lines` 里防套话与不承认 AI 两个分支，使三开关只输出同一条防注入句。
- 变异前 sha `430f40e1…` → 变异后 `dc0f3f00…` → 恢复后 `430f40e1…`。
- 结果：**红**。`/tmp/persona-mut-F2.log`：`test_each_guard_is_independently_assembled` FAILED。证明该测试能抓到“一个开关带另一个语义 / 文案合并”缺陷。
- 结论：冻结判据 F2 已补做，C4 区分力由“未核实”升级为“实测支持”。

### F5：群聊输出不同 guard 文案

- 变异：在 `dialogue_guard` 文本后追加 `\n群聊补充约束。`（仅 `is_group=True` 时）。
- 第一次尝试：现有测试只核对三条 needle 在两边都出现，**假绿**（`/tmp/persona-mut-F5.log`：1 passed）。
- 按 AGENTS 纪律处理“变异没红先怀疑测试是假的”：补强 `test_all_guards_shared_by_web_private_and_group`，直接以 `ContextCompiler(..., persona_id=...).compile(is_group=False/True)` 两次编译，提取 guard 段断言逐字相等。
- 补强后基线：`/tmp/persona-c5-baseline5.log`，6 passed。
- 重放变异：`/tmp/persona-mut-F5c.log`，`test_all_guards_shared_by_web_private_and_group` FAILED。
- 变异前 sha `430f40e1…` → 变异后 `75d6619b…`（第一次）/ 重放后见日志 → 恢复后 `430f40e1…`。
- 结论：冻结判据 F5 已补做；C5 从“仅 needle 存在”升级为“guard 段逐字一致”，假绿已修复。

### 补做后的剩余局限

B2/B3/B4、D1–D6 仍未实测；浏览器 E 类由开发方 `persona.browser.test.tsx` 运行 10/10 通过（`docs/evidence/persona-dialogue-options/browser-final.log`），但验收方没有独立重跑。该结论按“开发方实测”而非“验收方复核”标注。
