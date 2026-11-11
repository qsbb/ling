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

**修法**：`test_w2_rules.py` 不再 reload。实际代码是**调用期**读环境变量
（`os.environ.get` 在函数内），`monkeypatch.setenv` 之后直接调用就能测，reload 没有必要。

**不推荐的修法**：reload 后在 `finally` 里同步刷新所有已绑定引用（`turn.ContextCompiler` 等）。
那要维护一份"谁绑了它"的名单，加模块就漏、漏了不报错。

## 通用教训

**测试里出现 `importlib.reload` 就是一个信号**：它在测"模块导入期行为"。
如果被测逻辑在调用期读环境变量，reload 是多余且有害的——它会让模块级的类/函数身份分裂，
而 patch 类方法是常见测试手法，分裂之后 patch 打不中。

**判断方法**：reload 之后 `X.Class is Y.Class` 是否仍为 True。为 False 就说明有人绑了旧引用。
