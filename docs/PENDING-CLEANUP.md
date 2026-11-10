# 交付前清理项（等 R16 收工后一次性处理）

> **这是一份临时待办，不是设计文档。** 放在 `docs/` 根目录而不是 `docs/evidence/`，
> 以免和证据文档混在一起（证据文档回答"当时验证了什么"，这里回答"还剩什么没做"）。
> **清单执行完毕后请删除本文件**，不要让它成为又一份会过期的文档。

**建立时间**：2026-11-10
**触发条件**：R16（task-23）的实施方交回、`turn.py` / `context.py` 不再被其他人改动时。
**为什么延后**：这批改动全部落在 R16 正在编辑的文件里。现在动会撞车，而且行号会在
R16 合入后**再次漂移**，等于白改一遍。

> 这份清单只记录**已经核实过、确实需要动代码/文档**的事项。每一条都带当前的
> 定位方法与验收标准，不依赖对话上下文。

---

## 1. 归因纠正：`mark` → `media_mark`（含注释改写）

**性质**：不影响功能（mypy 现在是绿的），是**归因正确性**问题。

**背景（为什么责任是反的）**：
task-26 在媒体块引入了局部变量 `mark = f"（短号 …）"`（类型 `str`），它出现在
`compile()` 里比历史块更早的位置。mypy 对整个函数作用域把 `mark` 定型为 `str`，
于是历史块原有的 `mark = history_marks.get(...)`（`str | None`）报
`Incompatible types in assignment`。

R16 的实施方被这条报错牵连，把**自己的** `mark` 改成了 `history_mark`，并在注释里
写「媒体块还用了一个 `mark`……改名是唯一诚实的修法」。

**正确做法**：新引入的变量让路。所以：

| 位置 | 现在 | 改成 |
|---|---|---|
| 媒体块（我的 task-26） | `mark = f"（短号 {short_ref_for(...)}）"` / `mark = f"（第 {index} 条）"` | `media_mark` |
| 媒体块使用处 | `lines.append(f"- {who}{when}发的{label}{mark}：…")` | `{media_mark}` |
| 历史块（R16 改的） | `history_mark = history_marks.get(...)` | 还原为 `mark` |
| 历史块使用处 | `if history_mark: content = f"{MESSAGE_ID_MARKER.format(token=history_mark)} …"` | `mark` |
| 历史块注释 | 「媒体块还用了一个 `mark`……改名是唯一诚实的修法」（约 5–6 行） | **删除或改写**——代码改回来了、注释还在讲一个已不成立的故事，比不改更糟 |

**⚠️ 改名陷阱（必须注意）**：`context.py` 里还有一个**复数**变量 `history_marks`
（`dict[str, str]`，存整轮的标记映射，出现在 `:1500, :1661, :1670, :1677`）。
它**不是**要改的那个，**绝对不要动**——改了会破坏 `_history_marks` 的返回契约。

要改的只是**单数** `history_mark`（当前在 `:1539, :1540, :1541` 三处）。
所以验收命令必须用**词边界**，否则 `grep "history_mark"` 会把复数变量也算进来
（实测：无 `-w` 时 7 处，加 `-w` 后正确为 3 处）。

**验收**：
```bash
.venv/bin/python -m mypy src/ling/kernel/context.py   # 必须 Success
.venv/bin/python -m ruff check src/ling/kernel/context.py

# 单数已清零（-w 必须带，否则 history_marks 会被误计）
grep -wc "history_mark" src/ling/kernel/context.py    # 必须 0
# 复数仍在（证明没误伤）
grep -wc "history_marks" src/ling/kernel/context.py   # 必须 5

grep -nw "media_mark" src/ling/kernel/context.py      # 应有 3 处（2 赋值 + 1 使用）
```
同时确认 `context.py` 里**不再有任何**提及「媒体块还用了 mark」的注释。

**定位方法**：`grep -n "history_mark\|media_mark\|（短号 {short_ref_for" src/ling/kernel/context.py`

---

## 2–4. 证据文档行号漂移（3 处）

**性质**：只影响可核性，不影响结论。下面所有"现状"都是 2026-11-10 核过的。

### 2. `docs/evidence/r17-watch-willingness.md:24`
- **原文**：`→ context.compile（含 context.py:807-860 的「想看就写 <<看视频 mX>>」提示行）`
- **现状**：`context.py:807-860` 现在是 task-26 新增的媒体标识注释，**不是**那条提示行
- **应改为**：`context.py:904`（`想看就写 <<看视频 {ref}>>` 所在行）
- **重定位方法**：`grep -n "想看就写" src/ling/kernel/context.py`

### 3. `docs/evidence/r17-watch-willingness.md:26`
- **原文**：`→ parse_watch_markers（turn.py:1044）`
- **现状**：`turn.py:1044` 现在是发言租约续约的 `logger.warning`（R16 改动导致整体下移）
- **应改为**：`turn.py:1170`（`delivered, watch_refs = parse_watch_markers(delivered)`）
- **重定位方法**：`grep -n "delivered, watch_refs = parse_watch_markers" src/ling/kernel/turn.py`

### 4. `docs/evidence/r17-watch-willingness.md:588`
- **原文**：`三处结构性缺陷（context.py:788、796、846）`
- **现状**：三个行号**全部失准**（788=串人注释、796=session.execute、846=混合取值注释），
  且那段代码本身已被 task-26 改写
- **处理建议**：**不要**改成新行号——那三处缺陷已不存在，指到任何新行号都是错的。
  改为说明性质：「（取证当时的三处位置现已全部改写，见本节顶部状态标注）」
- **注意**：该节顶部已有状态标注（2026-11-10 加），这条只是让行号引用自洽

---

## 5. 其他可能同批处理的事项

- **开发库版本戳对齐**：开发库 `ling` 实测 `0062`，代码 `heads` = `0063_deferred_replies`。
  这是 `0063_deferred_replies.py` 未跟踪导致的（`git status` 显示 `?? `）。交付时统一
  `git add` 后跑一次 `alembic upgrade head` 对齐即可。**不需要改代码。**
- **全量验收**（Lead 指定的第 2 项）：unit/contract/integration **全跑**，不要只跑相关文件。

---

## 处理完毕后的验收

```bash
# 1. 归因纠正（-w 词边界不可省，见第 1 条的复数陷阱）
grep -wc "history_mark" src/ling/kernel/context.py         # → 0
grep -wc "history_marks" src/ling/kernel/context.py        # → 5（未误伤）
.venv/bin/python -m mypy src/ling/kernel/context.py        # → Success

# 2. 行号引用逐条回核（文档里每个 file.py:行号 都要能对上）
grep -n "想看就写" src/ling/kernel/context.py              # → 907 领近（以实际为准）
grep -n "delivered, watch_refs = parse_watch_markers" src/ling/kernel/turn.py

# 3. 全量
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src connectors
LING_TEST_DATABASE_URL=... .venv/bin/python -m pytest tests/unit tests/contract -q -o addopts=""
LING_TEST_DATABASE_URL=... .venv/bin/python -m pytest tests/integration -q -o addopts=""
```

> **注意**：R16 合入后所有行号可能再次变化——执行本清单时**必须先重跑上面的
> `grep` 定位**，不要直接套用本文档里记的行号。
