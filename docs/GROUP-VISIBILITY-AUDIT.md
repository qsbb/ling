# 群聊判定：24 处盘点与分类

> 本文是 task-42 的调查产出。任务是"统一群聊判定"，但全量 grep 后发现
> **实际有 24 处**（任务卡原本估计 3 处）。本轮只收敛了判据等价的部分，
> 其余按下表分类，供后续按域推进。
>
> 生成日期：2026-11-11。数据来源：`grep -rn '== "group_scope"\|!= "group_scope"\|":group:" in ' src/ling/ connectors/ --include=*.py`（排除 `kernel/visibility.py` 与注释行）。

## 唯一实现

`src/ling/kernel/visibility.py`：

| 函数 | 用途 |
|---|---|
| `is_group_visibility(visibility) -> bool` | **权威判据**。读 `Window.visibility`（或它的派生值）时用这个 |
| `is_group_window_ref(window_ref) -> bool` | **降级路径**。只有 `window_ref` 字符串、拿不到 Window 行时用 |
| `group_id_from_window_ref(window_ref) -> str \| None` | 从群标识里取群号 |
| `GROUP_VISIBILITY` / `GROUP_WINDOW_REF_MARKER` | 常量，避免各处硬写字面量 |

## 分类定义

| 类 | 含义 | 处置 |
|---|---|---|
| **A** | 判据等价，可直接换 | 换成 `is_group_visibility()` |
| **B** | 语义不同，**不能**直接换 | 保持原样，但要在代码里说明它判的不是"是否群聊" |
| **C** | 判据本身就是错的（字符串匹配） | 换成 `is_group_window_ref()` |
| **D** | 已通过参数拿到 `is_group: bool`，不需要再判 | 删掉重复判断，直接用参数 |

## 本轮已收敛（12 处）

| 位置 | 原判据 | 分类 | 改成 |
|---|---|---|---|
| `kernel/turn.py:864` | `window.visibility == "group_scope"` | A | `is_group_visibility(window.visibility)`，并抽成 `is_group_window` 局部变量供同函数内第二处复用 |
| `kernel/turn.py:881` | 同上（`user_sender_name`） | **D** | 复用 `is_group_window`（同一行内两处判据必须同源） |
| `kernel/bridge.py:369` | `":group:" in envelope.window_ref` | **C** | `is_group_window_ref()` |
| `kernel/bridge.py:1145` | 同上（`record_pending`） | C | `is_group_window_ref()` |
| `kernel/bridge.py:1657` | 同上（`plan_parts`） | C | `is_group_window_ref()` |
| `worker.py:436` | `source_visibility == "group_scope" or bool(payload.get("is_group"))` | A | `is_group_visibility(source_visibility) or bool(...)`（保留 fail-closed 追加语义） |
| `worker.py:778`（原 `:773`） | `":group:" in window_ref` | **C** | `is_group_window_ref()` |
| `worker.py:675` | `row.visibility != "group_scope"` | A（反向） | `not is_group_visibility(row.visibility)` |
| `worker.py:1682` | `str(window.visibility or "") == "group_scope"` | A | `is_group_visibility(window.visibility)` |
| `worker.py:2289` | 同上 | A | 同上 |
| `worker.py:2449` | SQL `Window.visibility == "group_scope"` | **B**（SQL） | `Window.visibility == GROUP_VISIBILITY`（取值同源，不是第二份判据） |
| `kernel/inbound_intake.py:171` | 内联切分 `scene.split(":group:")[1]` | C（重复实现） | `group_id_from_window_ref(scene)` |
| `kernel/inbound_intake.py:301` | `":group:" in envelope.window_ref` | C | `is_group_window_ref()` |
| `kernel/inbound_intake.py:373` | 同上 | C | `is_group_window_ref()` |
| `domains/goals/delivery_reconcile.py:280` | `":group:" in window_ref` | C | `is_group_window_ref()` |

> 说明：`worker.py:1678`/`2285` 在 `grep` 里原本是 `:1678`/`:2285`，加注释后行号有漂移。

## 本轮**未**收敛（12 处）

### A 类：判据等价，可直接换（低风险，建议下一轮）

| 位置 | 原判据 | 理由 / 建议 |
|---|---|---|
| `kernel/audience.py:51` | `self.window_visibility == "group_scope"` | `is_group` 属性。**这个文件是安全边界**（受众准入），换之前要确认 `is_group` 的所有调用点语义一致。建议单独一轮 + 完整回归 |
| `kernel/audience.py:114` | `source_visibility == "group_scope" and source_window_id != self.window_id` | 同上。这是"别的群的内容不许进"，判的是**来源**窗口而非当前窗口——换成 `is_group_visibility(source_visibility)` 语义等价，但必须放在受众测试的覆盖下 |
| `domains/awareness/service.py:244` | `window_visibility == "group_scope"` | 意识视图装配。A 类，可直接换 |
| `api/routes/chat.py:173` | `getattr(window, "visibility", "subject_private") == "group_scope"` | A 类。注意这里的默认值 `"subject_private"` 是有意的降级 |
| `api/routes/chat.py:917` | `getattr(window, "visibility", "") == "group_scope"` | A 类。默认值 `""` 与上面不同——两处默认值不一致本身值得留意 |
| `api/routes/chat.py:938` | 同上 | A 类 |

### B 类：语义不同，不能直接换

| 位置 | 判据 | 为什么不能换 |
|---|---|---|
| `api/routes/chat.py:93` | `window_visibility == "group_scope"` 用于 `group_budget` 记账 | 判的是"要不要计群聊预算"，虽然当前等价于"是否群聊"，但它是**计费语义**。将来若"群聊但私密模式"不该计入，这里会分叉。**建议保留，并在此处注明它判的是预算不是群聊身份** |
| `domains/goals/service.py:137` | `window.visibility == "group_scope"` 用于拒绝"承诺提醒发到群" | 判的是**投递目标合法性**（承诺提醒只能进私聊）。这是策略，不是身份判断。换实现不会错，但它属于"业务规则"而非"群聊判定"，改动应由该域负责人评估 |
| `domains/conversation/service.py:434` | `window.visibility == "group_scope"` 用于放行非成员的 `get_window_for_turn` | 判的是**归属放行规则**（群窗口对所有成员开放）。这是权限语义，改动风险最高，**强烈建议保持原样** |
| `domains/awareness/service.py:194` | `source_visibility == "group_scope"` 决定 `Audience.group_scoped` | 判的是**来源受众映射**，输入是 `source_visibility` 而非窗口 visibility。语义相关但职责不同 |
| `worker.py:2449` | SQL `Window.visibility == GROUP_VISIBILITY` | SQL 表达式不能调 Python 函数。已改为引用常量，取值同源 |
| `connectors/onebot/connector.py:754` | `":group:" in window_ref` | **C 类**，但属连接器内部：它据此在 `get_group_msg_history` / `get_friend_msg_history` 之间选 OneBot action。**连接器不该依赖 kernel 模块**（分层方向是 kernel → connectors），所以不能直接 import `visibility.py`。建议：把判据下沉成连接器自己的私有 helper，或由调用方通过参数传入 |

### C 类：字符串匹配（**错误**，但本轮不修）

| 位置 | 判据 | 说明 |
|---|---|---|
| `connectors/onebot/connector.py:754` | `":group:" in window_ref` | 见上。同样的子串误判机理，但受分层约束 |
| `domains/conversation/recall.py:348` | `":group:" in str(window.title or "")` | **注意**：这里 `window.title` 存的就是 `window_ref`（见 `bridge.py::_ensure_window` 的 `title=envelope.window_ref`）。有 Window 行在手却没读 `visibility` 列——**这是最该先修的一处**：它本可以用权威来源，却选了字符串 |

## 需要特别报告的两类问题

### 1. 「已经拿到 `is_group` 参数，内部又判一次」

本轮已消除 1 处：

- **`kernel/turn.py`**：`is_group=` 与 `user_sender_name=` 在同一函数调用里各自写了一遍
  `window.visibility == "group_scope"`。已抽成 `is_group_window` 局部变量。
  **风险**：两处不一致不会报错，只会让"上下文按群聊组装、署名按私聊省略"这类
  矛盾输出看起来莫名其妙。

**未发现**其他"收到 `is_group` 参数后又内部重判"的情况。逐项核对过：
`plan_parts(text, is_group=...)`、`OutboundMessage(is_group=...)`、
`_log_inbound_behavior(..., is_group=...)` 的**接收方**都不重判。

### 2. 默认值不一致（隐藏的不一致源）

`api/routes/chat.py` 三处读 `window.visibility`，默认值分别是
`"subject_private"`（2 处）与 `""`（1 处，`:917`）。三者对"缺 visibility 的
老数据"会给出不同结论：前两者判为私聊，后者判为"既非私聊也非群聊"。
目前不影响群聊判定（都非 `group_scope`），但**这是同一类隐患的温床**。

## 守卫测试

`tests/unit/test_group_visibility.py` 里有结构级守卫：

- `test_no_hardcoded_group_literal_in_converged_files`：对**已收敛文件**断言
  不再出现 `":group:" in` / `== "group_scope"` / `!= "group_scope"`
- 用 AST 排除注释与 docstring（qa-be 的第一版守卫被自己注释误判过）
- `test_guard_would_catch_a_reintroduction`：验证守卫**真的能抓到**重新引入，
  防止"永远为真的守卫"给人虚假安全感
- `test_visibility_module_exports_marker_that_nobody_hardcodes`：防止把
  `GROUP_WINDOW_REF_MARKER in ref` 当作子串匹配的马甲

**登记在 `CONVERGED_FILES` 里的只有已收敛的文件**。未收敛的 12 处**故意不纳入**
守卫——把它们加进去然后加白名单，会让守卫变成形式主义。

## 建议的推进顺序

1. `domains/conversation/recall.py:348` —— 有 Window 行却用字符串，改造成本最低、收益最直接
2. `api/routes/chat.py` 三处 A 类 —— 顺带统一默认值
3. `domains/awareness/service.py:244` —— 单点 A 类
4. `kernel/audience.py` 两处 —— **必须独立一轮**，这是安全边界，要有完整受众回归
5. `domains/goals/`、`domains/conversation/service.py` 三处 B 类 —— 由域负责人评估
6. `connectors/onebot/connector.py:754` —— 需先定分层方案（判据下沉 vs 参数传入）
