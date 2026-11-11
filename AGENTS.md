# ling 项目协作约定

> 这些规则来自实际踩过的坑，每条都注明了代价。
> 面向在同一个工作区协作的人和子智能体。

## 提交纪律

### 报告「我改了什么」之前，先看 `git status`

**在报告里声明改动之前，`git status --short` 必须能看到自己那几个文件。**

看不到 = 被别人的批量 `git add` 卷走了，**必须报告，不能当成「已经提交好了」**。

代价来源（2026-11-11）：一个 agent 修完跨文件测试污染，`git status` 发现工作区是
空的——它的 +96/−27 被另一个 agent 的批量 `git add` 裹进了「环境事实注入」那个
commit，message 里一个字没提。内容没丢，但提交历史里查不到这次修复。

**批量 `git add` 没有「这个文件是不是我该提交的」这个概念。** 只能用 `git add <明确路径>`。

### 同一文件同一行只能派给一个人

分工时按**文件**切，不按"改动的逻辑"切。如果一个文件里有改动 A 和改动 B 分给两个人，
就是**覆盖式冲突**：后写的人会把先写的人的注释、上下文全盖掉，而且**编辑工具不会报错**。

代价来源（2026-11-11）：一个 agent 拿到"改 `recall.py:348`"的任务卡，另一个 agent
的任务卡里**也**有同一条（派发时没删）。两人相差 9 秒改同一个文件的同一行。
这次两人都改对了所以没出事，但只是运气。

派发前检查一遍：**我要给的改动，是不是已经在别人的任务卡里了。**

### 开发与验收必须分开

写代码的人和验收的人不能是同一个。自测只能证明「实现和测试一致」，
证明不了「实现是对的」。

### 不要 `git stash`

多人共享工作区里 `git stash` 会撤掉所有人的未提交改动。宁可等，不要 stash。

### 设计文档留在本地，不推送

`docs/design/` 已 gitignore。设计讨论不进公开仓库。

## 测试纪律

### 跑 pytest 必须持锁

这个会话里有多个 agent 共用固定测试库 `ling_test`。靠「先查有没有人在跑，再启动」
来协调是 TOCTOU 竞态——check 和 use 之间有几秒时隙，必然被抢。

```bash
source /tmp/ling_test_lock.sh
acquire_test_lock          # 排队等，会打印进度
# ... pytest ...
release_test_lock          # 一定要执行
```

macOS 没有 `flock`，这是 mkdir 原子锁。**一个 agent 同时只跑一个 pytest。**

### 两个数据库环境变量都要设

```bash
export LING_TEST_DATABASE_URL="postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"
export LING_DATABASE_URL="postgresql+asyncpg://ling@127.0.0.1:55432/ling_test"
```

**两个名字不同，只设一个会漏**：
- `tests/integration/conftest.py` 读 `LING_TEST_DATABASE_URL`
- `migrations/env.py` 和 `src/ling/config.py` 读 `LING_DATABASE_URL`

漏设的后果是某条路径拿到外部残留值，表现成
`role "xxx" does not exist` 之类的错误，看起来像测试污染，其实是环境问题。

### 检查有没有 pytest 在跑，用 `ps` 不用 `pgrep`

```bash
ps -eo pid,etime,comm | grep "\.venv/bin/python"      # 正确
pgrep -f "python -m pytest"                            # 错误：匹配到自己
```

`pgrep -f` 匹配整条命令行，**执行中的 `pgrep` 命令本身**就包含那个字符串，
永远至少匹配到 1 个。

### 报告要 tee 到文件，不要只 `tail`

```bash
pytest ... 2>&1 | tee /tmp/result.log | tail -40
```

代价来源（2026-11-11）：一次全量 `tail -45` 只看到 ERROR 行，28 条 FAILED 全被
挤出屏幕，白跑 15 分钟。

## 写测试的纪律

### 守卫测试必须用 AST 过滤注释

```python
# 错的：被注释骗
source = inspect.getsource(module)
assert 'forbidden_call()' in source

# 对的：只看代码行
code = "\n".join(_code_lines(path))     # ast 解析，跳过注释与 docstring
assert 'forbidden_call()' in code
```

**这不是技巧，是必要条件。** 因为**解释一条禁令必须引用被禁的词**——
注释越写清楚，朴素字符串搜索越容易命中：

```python
# 原来的 `bool(payload.get("is_group"))` 恒为 False，所以必须显式传
```

删掉真代码后注释还在，断言照样通过。**写注释的自由和守卫的严格，只有在语法树层面才能共存。**

代价来源（2026-11-11）：同一个坑一天内被踩两次。一个守卫测试在被守卫的代码
删掉后仍然 `30 passed`——它从来没有起过保护作用，而看起来一直是绿的。

### 守卫本身要有测试，并且要用变异验证

```python
def test_guard_would_catch_a_reintroduction():
    """故意造一个违规文件，验证守卫真能抓到。"""
```

**一个永远为真的守卫比没有守卫更糟**——它给人虚假的安全感。

### 一个断言能证明什么，取决于「错误的实现会给出什么值」

如果**正确实现和错误实现在某个输入上给出相同结果**，那个输入就证明不了任何事。

这条是可操作的——它直接告诉你**要写几条、怎么选输入**：

| 字段形状 | 要几条 | 怎么选输入 |
|---|---|---|
| 三态 `bool \| None` | **三条**（每态一条） | 造出每个值的路径各一条 |
| 布尔 | 两条 | 一条 `True` + 一条 **形态相反**的 `False` |
| 有一个"降级/推断"分支 | 至少三条 | 显式值 + 缺值（走推断）+ **显式值与推断结果相反** |

**反例（真实案例）**：`ReceiptQuery.is_group=True` 配**群聊形态**的 ref，推断也会得出
`True`——这条测试在"用调用方给的值"和"自己推断"两种实现下都通过，**它什么都没证明**。
必须配**私聊形态**的 ref 才有区分力。

三次独立验证（同一天，三个不同的人，三个不同的问题）都得出这个结论——所以它不是
谁的灵感，是这类测试的客观要求。

### 变异测试是唯一能证明测试有效的手段

写完测试后，故意制造它应该抓到的 bug，确认测试会红。

- 变异**没红**时，第一反应不能是「这个变异不适用」，而应该是**「我的测试可能是假的」**
- 按上面那条判据选输入：如果变异和原实现在该输入上结果相同，换输入，别换结论

### 别用 `bool()` 包装三态字段

```python
is_group=payload.get("is_group")        # 对：缺 key → None → 退回推断
is_group=bool(payload.get("is_group"))  # 错：bool(None) = False → 旧数据全被当成"确认私聊"
```

`bool()` 看起来在「规范化类型」，实际在**丢弃三态信息**。

### 测试里出现 `importlib.reload` 就是坏味道

`reload` 产生**新的类对象**，但**修不好其他模块早先绑定的旧引用**：

```python
# turn.py:58
from ling.kernel.context import ContextCompiler      # 导入时钉死旧类
```

之后 reload 两次，`turn.ContextCompiler` 仍指向最初那个类。于是 patch 新类、
代码用旧类 → spy 静默不触发。

**判断方法**：reload 之后 `X.Class is Y.Class` 是否仍为 True。为 False 就说明有人绑了旧引用。

细节见 `docs/evidence/test-pollution-reload.md`。

## 验证的纪律

### 「全绿」不等于「测到了东西」

今天出现过三种「假通过」：

| 形状 | 案例 |
|---|---|
| 看起来在恢复，实际造成身份分裂 | `importlib.reload` |
| 看起来在守卫，实际永远为真 | 被注释骗过的字符串断言 |
| 看起来在脱敏，实际靠猜格式 | 按 `sk-` 前缀匹配密钥 |

**共同点：验证手段本身失效，但不报警。**

**失败有声音，假通过没有。** 所以：全绿之后，挑 2-3 个关键测试做变异验证。

### 先找到共同根因，再谈逐条确认

2148 条 ERROR 不可能是 2148 个 bug。先问「它们的共同根因是什么」。

### 单跑复现法

「全量失败、单跑通过」时：

```bash
pytest tests/integration/test_something.py -q     # 单独跑一遍
```

- 单跑通过 → 大概率是跨文件污染或环境问题
- 单跑也失败 → 真回归

这个方法有效，但它**只证明「单独跑没问题」，不定位污染源**。要定位仍需二分。

## 安全纪律

### 生产端与服务端分离

- **仓库**：`/Users/lingxi/project/ling`，只放代码与模板
- **生产端**：NAS `/vol1/@appdata/ling`，只放运行期数据

**不要连生产库或 NAS。** 开发只用 `127.0.0.1:55432/ling_test`。

### 脱敏按字段名，不按值的形态

不要猜「密钥长什么样」（`sk-` 前缀之类）——有些 key 是可自定义的，
按前缀匹配会漏掉真正的密钥，也会把普通文本误判成密钥。

**字段名（`SENSITIVE_KEYS`）才是真正的门。**

### 文档和测试里用假数据

内网地址写 `192.0.2.x`（RFC 5737 文档专用段），QQ 号用 `100200300` 这类，
用户名写 `deploy`。

发布前必查：`git grep -nIE "Zyb@|sk-|192\.168\.|真实QQ号"` 应该零命中。

## 文档纪律

### 决策的理由写不下来，等于没做

`git log` 只回答「改了什么文件」，`docs/evidence/` 回答「为什么这么改」。

如果一次修复被卷进了不相关的 commit（message 未单列），**在 `docs/evidence/`
里留档说明落地位置**，不要为了补一句话去改写历史。

### 澄清后再动手

需求有歧义、需要用户偏好、或仓库里查不到且猜错代价高的关键事实，
**先问，不要先猜再改**。

## 代码布局

### `ports/` 放请求，`contracts/` 放响应

| 位置 | 类型 | 形态 | 语义 |
|---|---|---|---|
| `ports/channel.py` | `OutboundMessage` / `ReceiptQuery` / `RecallRequest` | `frozen dataclass` | **请求**：我要让平台做什么 |
| `contracts/v1.py` | `DeliveryReceipt` / `RecallReceipt` | pydantic `StrictModel` | **响应**：平台回了什么 |

分界线是**请求 / 响应**，不是「通道相关」。响应要过 JSON 边界、要 `schema_version`、
要序列化，所以是 pydantic；请求由调用方在进程内构造，所以是 dataclass。

**判据：我要构造它当参数传进去 → `ports/`；我从平台收到它 → `contracts/`。**

**名字相近不代表同处**：`ReceiptQuery` 是请求（`ports/`），`RecallReceipt` 是响应
（`contracts/`），两者相邻的名字里都有 `Receipt` 但本来就不该放一起。

代价来源（2026-11-11）：写测试时按名字找 `OutboundMessage`，在 `contracts/v1.py`
里找了一圈——它在 `ports/channel.py`。
