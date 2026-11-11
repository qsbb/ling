# 跨文件测试污染的根因：importlib.reload 造成类身份分裂

> 2026-11-11 排查记录。现象：`pytest tests/unit tests/integration` 出现 1 failed，
> 而两个文件单独跑都通过。

## 现象

```
tests/integration/test_display_name_chain.py 单独跑      → 5 passed（三次验证）
tests/integration 全跑                                   → 2457 passed, 0 failed
pytest tests/unit tests/integration                      → 1 failed
```

失败断言：

```
assert seen, "这一轮没有编译上下文"     →  assert []
```

矛盾点：日志里 `轮次开始` 和 `人设为空（context.py:573）` 都在，
**证明 `compile` 确实执行了**，但 spy 一次都没被触发。

## 根因

`tests/unit/test_w2_rules.py:1077,1087,1097,1102` 有四处 `importlib.reload(ctx)`：

```python
monkeypatch.setenv("LING_CONTEXT_BUDGET_SCALE", "0.5")
reloaded = importlib.reload(ctx)      # 造出第二个 ContextCompiler 类
try:
    ...
finally:
    monkeypatch.delenv(..., raising=False)
    importlib.reload(ctx)             # 想"重载回去"，但没用
```

**`reload` 会重新执行模块体、产生新的类对象，但无法修复其他模块早已绑定的旧引用。**

`src/ling/kernel/turn.py:58` 在首次导入时就把旧的 `ContextCompiler` 钉死了：

```python
from ling.kernel.context import ContextCompiler, estimate_tokens
```

reload 两次之后：

```
context.ContextCompiler id: 526082510864
turn.ContextCompiler    id: 526079321104
同一对象? False
但 __file__ 与 __module__ 都相同   ← 这是 reload 的指纹
```

而 `test_display_name_chain.py:245-253` patch 的是**新**类：

```python
original = context_module.ContextCompiler.compile
monkeypatch.setattr(context_module.ContextCompiler, "compile", _spy)
```

`turn.py` 调用的是**旧**类 → `_spy` 永远不触发 → `assert seen` 得到 `[]`。

## 为什么难查

1. **`__file__` 和 `__module__` 都相同，对象 id 却不同** —— 看起来像"同一个模块被导入两次"，
   但 `sys.modules` 里只有一个，所以不是重复导入。这个矛盾指向 reload。
2. **日志说编译了，spy 说没编译** —— 两个都是真的，因为走的是两个不同的类。
3. **只在特定组合下复现** —— 单独跑谁都对。

## 修法与防复发

### ⚠️ 一句容易照抄错的判断（2026-11-11 更正）

先前这里写的是：

> 实际代码是**调用期**读环境变量（`os.environ.get` 在函数内），
> `monkeypatch.setenv` 之后直接调用就能测，reload 没有必要。

**这句话只对 `_budget_scale()` 成立，对预算表不成立。**

`context.py:256` 紧接着就把系数求值一次并**重算两张表**：

```python
_SCALE = _budget_scale()          # ← 导入期求值一次
if _SCALE != 1.0:
    DEFAULT_BUDGETS = {k: max(80, int(v * _SCALE)) for k, v in DEFAULT_BUDGETS.items()}
    HISTORY_TOKEN_BUDGET = max(400, int(HISTORY_TOKEN_BUDGET * _SCALE))
```

所以：

| 想测什么 | 怎么测 |
|---|---|
| 系数取值（含夹紧 0.1 / 5.0） | 本进程直接调 `_budget_scale()` |
| **系数真的作用到预算表** | **必须在干净解释器里带环境变量 import** |

如果按"直接调用就能测"去改，`DEFAULT_BUDGETS["persona"] == 50_000` 这类断言
会**测不到真实的缩放结果**——那不是修好，是悄悄弱化断言。

实际采用的修法：**两层分开测**。

- 系数 → 本进程调 `_budget_scale()`
- 预算表落点 → `subprocess` 开干净解释器、带环境变量重新 import，读真实导入结果

没有把 `max(80, int(v * scale))` 抄进测试复算——那正是本仓库反复出现的
「同一规则两处各写一份」；也没有只测系数，那会丢掉「确实缩放了」这一层。

**不推荐的修法**：reload 后在 `finally` 里同步刷新所有已绑定引用（`turn.ContextCompiler` 等）。
那要维护一份"谁绑了它"的名单，加模块就漏、漏了不报错。

### 两处补强（原记录没有）

1. **`finally` 里那次 reload 也救不回来。** 每次 reload 造的都是**又一个新类**，
   没有任何一次会让已绑定的旧引用回到原位——所以污染是**永久性**的。
2. **全仓只有 1 个测试受这个机制影响。** 全仓 patch `ContextCompiler` 的地方只有
   `test_display_name_chain.py:253` 一处。这解释了为什么全量只挂 1 个，也说明
   **如果不多挂那一个，这个机制会一直潜伏**到有人新写一个 patch 类方法的测试时才炸。

### 落地位置

修法实际落在 `10d92bc`（该提交主体是「环境事实注入」，这个修复被一并提交，
commit message 未单列）。因为历史不宜改写，特此在此留档：

- `tests/unit/test_w2_rules.py` 去掉全部 `importlib.reload(ctx)`
- 改为「本进程测系数 + 子进程测预算表落点」
- 验证：`pytest tests/unit tests/integration/test_display_name_chain.py`
  从 **1 failed / 123 passed** → **2047 passed, 0 failed**

`test_display_name_chain.py` 本身**不需要改**：它 patch `context_module.ContextCompiler`
的手法是对的，只要类身份不分裂，`turn.py` 用的就是同一个类。它是受害者，不是责任人。

## 通用教训

**测试里出现 `importlib.reload` 就是一个信号**：它在测"模块导入期行为"。
如果被测逻辑在调用期读环境变量，reload 是多余且有害的——它会让模块级的类/函数身份分裂，
而 patch 类方法是常见测试手法，分裂之后 patch 打不中。

**判断方法**：reload 之后 `X.Class is Y.Class` 是否仍为 True。为 False 就说明有人绑了旧引用。
