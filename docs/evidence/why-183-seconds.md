# 两条测试为什么各花 183 秒：两个消费者抢同一个终止标记

> 2026-11-11 排查记录。现象：`tests/integration/test_capability_poke_inbound.py`
> 只有 2 条测试，却要跑 366 秒（183 秒/条）。

## 现象

```
tests/integration/test_capability_poke_inbound.py → 2 passed in 366.41s
```

`--durations=0` 显示时间几乎全在 `call` 阶段：

```
182.71s call      test_inbound_onebot_private_passes_capabilities_end_to_end
182.61s call      test_inbound_capabilities_come_from_this_instance
 0.56s setup
 0.32s setup
 0.09s teardown
```

**两条测试各 183 秒，而且两次的数值只差 0.10 秒。** 这个一致性说明
它等待的是一个**固定时长的东西**，不是随机变慢。

## 三次排除，方向全错

排查过程中先后排除了三个候选。**三个排除的结论都对，但都没找到原因**——
因为一直在 `turn.py` 和 `context.py` 里找，而答案在 `bridge.py`。

### 排除一：不是真实模型调用（量级差）

```
conftest.py:64        test_mode=True
registry.py:179       if not test_mode: ...（测试模式不绑真实模型）
```

测试模式走 `FakeProvider`。实测它的回复长度有上界：

```python
build_fake_reply([ChatMessageIn(role="user", content="你好")], model_id="deterministic")
→ 92 字符 → 24 chunks → 1.20 秒
```

输入再长，`snippet` 超过 40 字符就被截断，**单次调用上限约 1.65 秒**。

**183 秒是它的 111 倍。量级差排除了「调优能解决」的可能。**

### 排除二：也不是 `_wait_for_floor` 的累加（守卫置位）

`turn.py` 里 `_wait_for_floor` 的上限是 `ARBITER_WAIT_STEPS(32) × ARBITER_WAIT_SECONDS(0.25) = 8 秒`。
曾怀疑它被调用 23 次（23 × 8 = 184 秒，和 183 很接近）。

```
$ grep -n "floor_settled" src/ling/kernel/turn.py
804:   floor_settled = False       ← 初始化
1115:  if not held_by_me and not floor_settled:
1116:      floor_settled = True    ← 调用前置位
1183:  floor_settled = True
1198:  floor_settled = True
```

**三处置位、零处重置。** 所以它最多执行一次，上限 8 秒。**排除。**

### 排除三：不是历史分页循环（页数算术）

`context.py:1534` 有 `while True` 分页查询，是唯一「能容纳大耗时」的结构。
但算一下页数就知道不可能：

```
HISTORY_FETCH_CAP = 2000
history_limit 默认 = 200
→ page_size = min(200, 2000) = 200

HISTORY_TOKEN_BUDGET = 20000
每条 30 token → 可容纳 666 条 → 需翻 4 页
每条 10 token → 可容纳 2000 条 → 需翻 10 页

本地 PG 一次往返 1~10ms → 10 页 = 0.1 秒
```

**最坏 0.1 秒，离 183 秒差 1830 倍。排除。**

## 关键一步：把可观测范围全部打点，剩下没打点的就是答案

在 `_run_inbound` 的两处加了计时探针（`run_turn` 返回处、queue 消费结束时）：

```
[探针] run_turn 返回耗时 0.00s
[探针] 消费 29 个事件，总耗时 1.25s
[探针] run_turn 返回耗时 0.00s
[探针] 消费 29 个事件，总耗时 1.19s
2 passed in 366.41s
```

**两条测试实际做的事加起来只有 2.44 秒，总时长 366.41 秒，差额 363.97 秒。**

探针覆盖了 `_run_inbound` 的**全部内容**，唯一没覆盖的是：

```python
await _post(env, body)      # ← POST /v1/onebot/events
```

**那 364 秒在这个 HTTP 请求里。**

## 根因：`None` 只有一个，而有两个消费者

`src/ling/kernel/bridge.py:77`：

```python
TURN_WAIT_TIMEOUT_SECONDS = 180.0
```

**183 ≈ 180。** 反查这个常量，机制立刻清楚：

**`InboundBridge` 自己也消费同一个 queue**（`bridge.py:1586`）：

```python
queue = await self._coordinator.run_turn(...)     # ← 拿到 queue
...
while True:
    try:
        event = await asyncio.wait_for(queue.get(), timeout=TURN_WAIT_TIMEOUT_SECONDS)
    except TimeoutError:
        timed_out = True
        logger.warning("轮次 %s 超过 %.0f 秒未结束…", turn_id, TURN_WAIT_TIMEOUT_SECONDS)
        break
    if event is None:      # ← 终止标记
        break
```

而测试替换的 `RecordingCoordinator` **也消费了同一个 queue**，把 `None` 吃掉了：

```python
queue = await self._inner.run_turn(**kwargs)
while True:
    event = await queue.get()     # ← 抢到了 None
    if event is None:
        break
return queue
```

```
                queue = [...]  ──┬──> 测试的 _run_inbound：拿到 None，1.25 秒结束
                                  └──> bridge 的 while True：None 已被吃掉
                                       → wait_for 等满 180 秒 → TimeoutError → break
```

**`None` 是队列里唯一的终止标记。** 测试拿走之后，bridge 只能靠超时脱身。

### 算术闭合

```
182.71 = 180.0（超时） + 1.25（测试消费的 turn） + ~1.46（启动与清理）
182.61 = 180.0（超时） + 1.19（测试消费的 turn） + ~1.42
```

**两次独立测量都对得上。**

## 结论

| 判断 | 结论 |
|---|---|
| 是性能问题吗 | **不是**。跑完整 turn 只要 1.25 秒，完全合理 |
| 是产品 bug 吗 | **不是**。生产环境没有第二个消费者，180 秒超时是给「模型真卡住」准备的兜底 |
| 是测试 bug 吗 | **是**。测试抢走了 bridge 的终止标记 |
| `assert recorder.compiled` 是假的吗 | **不是**。它确实拿到了编译结果，只是代价是让 bridge 空等 180 秒 |

## 修法：从「抢着消费」改成「旁观」

**最简方案其实不需要额外机制**：测试只要**不消费 queue**，`_post` 返回时一切就绪——
因为 `bridge.handle()` 会消费 queue 到底才返回，那时 `_execute` 已跑完、`compile` 已被调用。

```python
recorder = RecordingCoordinator(inner)
await _post(env, body)        # bridge 消费完 queue 才返回（约 1.25 秒）
assert recorder.compiled      # 此时 compile 已经跑过了
```

**如果确实需要在测试侧观察 queue 内容**，用 tee（记录但不 pop），让 bridge 正常消费：

```python
class TeeQueue(asyncio.Queue):
    def __init__(self):
        super().__init__()
        self.seen = []
    async def get(self):
        item = await super().get()
        self.seen.append(item)
        return item
```

**两种方案都从「两个消费者抢一个标记」变成「一个消费者 + 一个观察者」。**

## 方法论

这次排查暴露了三个可复用的方法，也暴露了一个反复出现的思维错误。

### 1. 量级差 → 排除

`1.65 秒 vs 183 秒`，**111 倍**。不是「我觉得模型不会那么慢」，是**量级差
排除了「调优能解决」的可能**。

### 2. 常量反查 → 命中

观测到 `183` 后，**立刻 grep 这个数字附近的常量**：

```bash
grep -rnE "180|TIMEOUT|timeout" src/ling/ | grep -v test
```

`TURN_WAIT_TIMEOUT_SECONDS = 180.0` 一出现，机制当场就通了。

**两个方向都用「已知的数字」校准观察**：一个用数字排除，一个用数字命中。

**这次花了十几轮才想到 grep 常量。第一步就该做。**

### 3. 穷举循环结构 → 把开放问题变成有界枚举

「哪个步骤慢？」是开放的。但「`turn.py` 里有几个循环？」是可数的：

```bash
grep -nE "^\s+(while |for )" src/ling/kernel/turn.py
```

只有 4 个，逐个排除即可。**把「想不出来」变成「能算出来」。**

### 4. 反复出现的错误：用「应该在哪」代替「实际在哪」

三次同型错误，都发生在这一天：

| 事件 | 「应该在哪」 |
|---|---|
| 行号开窗定位 | 以为窗口内就是那个调用点 |
| `git diff` 判断生成脚本 | 以为 diff 显示的是自己造成的 |
| **183 秒定位** | **以为测 turn 的慢就在 turn 里** |

**第 3 次最难自查**，因为前提听起来天经地义：测试叫 `..._inbound`、跑的是 turn，
所以慢一定在 turn 里。

**但真实数据流是**：

```
HTTP → bridge → turn → queue → bridge 消费 → HTTP 返回
```

**测试的 `call` 阶段覆盖整条链路**，而「慢在哪」被从「测试在验什么」推断了——
**这个推断本身没有依据。**

## 未解决的设计问题：这是个「总时长超时」，不是「静默超时」

排查过程中读到 `bridge.py` 的超时处理，发现一个**与本次缺陷无关、但更严重**的问题。
**本次未处理，记录在此以免重新推导。**

### 当前行为

```python
TURN_WAIT_TIMEOUT_SECONDS = 180.0

while True:
    try:
        event = await asyncio.wait_for(queue.get(), timeout=TURN_WAIT_TIMEOUT_SECONDS)
    except TimeoutError:
        # **超时不等于没内容**：已经流出来的部分还在（`CancelRegistry` 逐 delta 记着），
        # 而且生成任务仍在跑、最终会把正文写进 `messages`。所以这里**不能**当作
        # "没有可交付文本"收场。
        timed_out = True
        logger.warning("轮次 %s 超过 %.0f 秒未结束（已生成 %d 字），转交收尾/续发", ...)
        break
```

### 问题：超时语义错了

`180.0` 在这里的作用是**「等这一轮全部生成完，最多等 180 秒」**——一个**总时长**上限。

但对一个正在和用户对话的系统，**用户感知的是另一件事**：**「多久没有新内容出来」**。

这两个量在正常和异常情况下差别极大：

| 情况 | 总时长 | 最长静默 | 用户感受 |
|---|---|---|---|
| 正常长回复，持续流式 | 可能 60 秒 | 几十毫秒 | 正常，看得到字在出 |
| 模型卡住，一个字不出 | 180 秒后才失败 | **180 秒** | **完全卡死，无任何反馈** |
| 模型先出 5 字然后卡住 | 180 秒后才收场 | **175 秒** | **像死机**（已有 5 字却不再更新） |

**当前实现把「卡住」和「在慢慢生成」当成同一件事处理**——两者都要等满 180 秒。

### 而代码自己的注释已经指出了矛盾

注释写得很清楚：

> **超时不等于没内容**：已经流出来的部分还在……所以这里**不能**当作"没有可交付文本"收场。

**既然「已有部分内容」是可以交付的，为什么还要等满 180 秒才交？**

**这正是「总时长超时」和「静默超时」混用的症状**：设计者意识到「超时了也有内容」，
于是在超时之后补了一个交付路径；**但如果超时判据本身是对的（静默 N 秒），
就根本轮不到「180 秒后才发现」这个场景。**

### 正确的超时语义

**应该区分两个独立的判断**：

```
静默超时：距上一个 delta 超过 N 秒（建议 5~15 秒）→ 判定卡住，立即交付已有内容
总时长超时：单轮总时长超过 M 秒（兜底，防止无限长）→ 强制结束
```

**判据不一样，处置也不一样**：

- **静默超时**意味着「生成端没有进展」——**立刻**把已有内容交付，并告知用户"生成中断"
- **总时长超时**意味着「它还在生成，但太长了」——**这是正常的长回复**，不该当故障处理

**现在的实现只有后者，没有前者。**

### 为什么这个问题值得单独处理

1. **它对用户是致命的**：180 秒无反馈，用户会以为服务挂了，反复重发
2. **它没法靠调小常量解决**：把 180 改成 30，会误伤正常的长回复（流式长文可能超过 30 秒）
3. **它和本次的测试缺陷同源**：两者都是「**把『等终止标记』当成『等固定时长』**」

**第 3 点尤其值得注意**——本次 183 秒的测试缺陷，根因也是 `None` 这个终止标记
被抢走后只能靠超时脱身。**同一个模式在代码和测试里各出现了一次。**

### 需要回答的设计问题

在动手之前，有几个问题需要先定：

- **静默多少秒算卡住？** 取决于模型首字延迟和流式间隔的分布，需要真实数据
- **静默超时后交付什么？** 已有的部分内容 + 一个明确的「生成中断」状态？
- **要不要告诉用户？** 部分内容 + 隐式截断，用户可能以为是完整回复
- **`CancelRegistry` 里已生成的内容怎么续发？** 注释提到「转交收尾/续发」，这条路径现在存在吗？

**这几个问题任何一个都会改变实现形状，所以不建议直接改常量。**
