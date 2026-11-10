# LLM 调用失败重试：AstrBot / MaiBot 源码调研与本项目方案

调研日期：2026-11-11。源码版本：AstrBot `master`（2026-11-10 更新，46617★）、MaiBot `main`（2026-11-10 更新，6966★）。全部结论均标注 `文件:行号`；未能核实的地方写「没查到」。

---

## §0 结论先讲

1. **两家都实现了重试，但都不是「无条件重试」。** 两边都先把错误分成「可重试」和「不可重试」两类：400/401/403/404 这类参数或鉴权错误**一律不重试**，因为重试必然再次失败，只会浪费时间和额度。用户说的「无条件重试」如果按字面实现，会在这类错误上反复无效重试。
2. **数量级：AstrBot 5 次、MaiBot 3 次（均含首次请求）。** AstrBot `REQUEST_RETRY_ATTEMPTS = 5`（`astrbot/core/provider/sources/request_retry.py:19`），MaiBot `max_retry` 默认 `3`（`src/config/model_configs.py:161-169`）。
3. **退避不同：AstrBot 指数退避 0.2s→30s，MaiBot 固定间隔 4s。** AstrBot `wait_exponential(multiplier=1, min=0.2, max=30)`（`request_retry.py:100-104`）；MaiBot `retry_interval` 默认 `4`（`src/config/model_configs.py:180-188`），直接 `await asyncio.sleep(...)`。
4. **本项目最关键的一条：两家的重试都只包住「发起请求」这一瞬间，没有一家会重试「流已经吐了字之后的中断」。** AstrBot 的 `stream = await retry_provider_request(...)`（`openai_source.py:630`）只保护建立流连接；后续 `async for chunk in stream`（同文件 `:640`）在重试包裹之外。MaiBot 的流式循环只在**主动取消**（`interrupt_flag` / `CancelledError`）时保留半截输出，其余流中途错误直接上抛（`src/llm_models/model_client/openai_client.py:1374-1378`）。
5. **AstrBot 在流中途出错时的处理是「记日志然后静默正常结束」**，注释原文：`流式内容已通过 yield 发出，记录错误后正常结束即可`（`openai_source.py:702-704`）。这是一个**已经吐了半句就断掉、且不告诉任何人**的既有实现，本项目**不应照抄**。
6. **MaiBot 有跨模型切换，AstrBot 也有，但顺序都是「先把同一个模型重试到耗尽，再换下一个」。** MaiBot：`except ModelAttemptFailed` → `切换到下一个模型`（`src/llm_models/utils_model.py:1458-1475`）；AstrBot：`fallback_provider_ids` 配置，提示语「主聊天模型请求失败时，按顺序切换到这些模型」（`astrbot/core/config/default.py:3601-3607`）。
7. **前面说的这两家，都没有「熔断」。** AstrBot 在 `astrbot/core/provider/` 下搜 `circuit|breaker|cooldown|连续失败` **零命中**。MaiBot 没有硬熔断，但有**软惩罚**：失败模型 `penalty + 1`，靠惩罚值降低它后续被选中的概率（`utils_model.py:1474`、`:125` 注释）。**这一条比本项目的硬熔断更温和**——本项目 `router.py` 的 `health.is_open()` 是把整个候选直接跳过。

---

## §1 横向对照表

| 维度 | AstrBot | MaiBot | 本项目 ling 现状 |
|---|---|---|---|
| 重试库/实现 | `tenacity.AsyncRetrying` | 手写 `while retry_remain > 0` | **无**（`httpx` 一次调用） |
| 次数（含首次） | `5`（`request_retry.py:19`） | `3`（`model_configs.py:162`） | 1 |
| 退避 | 指数，`0.2s`→`30s` | 固定 `4s` | 无 |
| 可重试：网络 | ✅ `is_connection_error` / `APIConnectionError` / `APITimeoutError`（`request_retry.py:45-50`） | ✅ `NetworkConnectionError`（`utils_model.py:1141`） | 抛出 `retriable=True` 但**无人消费** |
| 可重试：429 | ✅ 可配置是否重试（`request_retry.py:56-57`） | ✅（`utils_model.py:1196`） | 同上 |
| 可重试：5xx | ✅ `{408,409,429,500,502,503,504,529}` ∪ `500-599`（`request_retry.py:22,59`） | ✅ `>= 500`（`utils_model.py:1196`） | 同上 |
| 可重试：空回复 | ❌ 没查到 | ✅ 单独一类 `EmptyResponseException`（`utils_model.py:1109`） | 没查到 |
| 可重试：响应解析失败 | ❌ 没查到 | ✅ `RespParseException`（`utils_model.py:1254`） | 没查到 |
| 不可重试：4xx | ✅ 不在集合内即不重试 | ✅ 非 429/5xx 直接抛硬错误（`utils_model.py:1245-1249`） | — |
| 特殊：413 过大 | 没查到 | ✅ **压缩图片后重试，且不消耗重试次数**（`utils_model.py:1219-1243`） | 没查到 |
| **流已开始后中断** | **只重试建流；流内异常记日志后 `return`**（`openai_source.py:630,702-704`） | **不重试**；仅主动取消时保留半截输出（`openai_client.py:1374-1378`） | 不重试，直接抛错 |
| 备用 provider 池 | ✅ `fallback_provider_ids`，有序 | ✅ 任务级 `model_list` + `selection_strategy`（含 `random`） | ✅ 1 个 fallback |
| 重试与换模型的顺序 | 单 provider 内先重试，耗尽再换 | **先在同一模型内重试到耗尽，`ModelAttemptFailed` 后换下一个**（`utils_model.py:1458`） | 不重试，直接换（且仅未产出文本时） |
| 熔断 | ❌ 无 | 软惩罚 `penalty + 1` | ✅ 硬熔断 `health.is_open()` |
| 幂等 | 没查到显式机制 | 请求失败快照落盘，尝试计数 `attempt`/`model_attempt`（`utils_model.py:1085-1089`） | 后台任务 `MAX_ATTEMPTS=5` |
| SDK 自带重试 | **显式关闭** `max_retries=0`（`openai_source.py:375-377,387-389`） | 没查到 | 不适用 |
| 配置项 | `agent.config.model.request_max_retries`（默认 5，`config/agent_runner.py:14`） | `max_retry`（3）、`retry_interval`（4）、`timeout`（120） | 无 |

---

## §2 AstrBot：实现细节与源码出处

### 2.1 重试封装是独立模块

AstrBot 把重试抽成 `astrbot/core/provider/sources/request_retry.py`，供各 provider 复用。核心参数（`:19-22`）：

```python
REQUEST_RETRY_ATTEMPTS = 5  # default value
REQUEST_RETRY_WAIT_MIN_S = 0.2
REQUEST_RETRY_WAIT_MAX_S = 30
REQUEST_RETRY_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}
```

组装（`:92-112`）：

```python
return AsyncRetrying(
    retry=retry_if_exception(
        lambda error: _is_retryable_provider_request_error(
            error, retry_rate_limits=retry_rate_limits,
        )
    ),
    stop=stop_after_attempt(max_attempts),
    wait=wait_exponential(multiplier=1, min=REQUEST_RETRY_WAIT_MIN_S, max=REQUEST_RETRY_WAIT_MAX_S),
    before_sleep=lambda retry_state: _log_retry(...),
    reraise=True,
)
```

`reraise=True` 很重要：重试耗尽后抛**原始异常**，而不是 tenacity 的包装异常，调用方能按真实错误类型分流。

### 2.2 错误分类逻辑

```python
def _is_retryable_provider_request_error(error, *, retry_rate_limits) -> bool:
    if is_connection_error(error):
        return True
    error_type_name = type(error).__name__
    if error_type_name in {"APIConnectionError", "APITimeoutError"}:
        return True
    status_code = _get_status_code(error)
    if status_code is None:
        return False
    if status_code == 429 and not retry_rate_limits:
        return False
    return status_code in REQUEST_RETRY_STATUS_CODES or 500 <= status_code <= 599
```

（`request_retry.py:40-59`）

三个可借鉴点：
- **拿不到状态码就当不可重试**（`:53-54`）——避免对未知异常无限重试。
- **429 单独开开关**（`:56-57`），因为限流重试可能加剧限流。
- **`APITimeoutError` 按类名判断**（`:48-50`），不依赖 SDK 的继承层次。

### 2.3 重试只覆盖「发起请求」

非流式（`openai_source.py:570-578`）：

```python
completion = await retry_provider_request(
    "OpenAI",
    lambda: self.client.chat.completions.create(
        **payloads, stream=False, ...
    ),
    max_attempts=request_max_retries,
)
```

流式（`openai_source.py:630-639`）：

```python
stream = await retry_provider_request(
    "OpenAI",
    lambda: self.client.chat.completions.create(
        **payloads, stream=True, ...
    ),
    max_attempts=request_max_retries,
)

llm_response = LLMResponse("assistant", is_chunk=True)
state = ChatCompletionStreamState()

async for chunk in stream:   # ← 这一层没有任何重试
```

**这是全篇最关键的结构**：`retry_provider_request` 的 `request_factory` 返回的是**流对象**，而不是文本。所以重试只会重发「建立流」这次 HTTP 请求；一旦开始 `async for`，后续每个 chunk 的异常**不在重试范围内**。

### 2.4 流中途出错：记日志后静默结束

```python
except Exception as e:
    logger.error("get_final_completion error: " + str(e))
    # 流式内容已通过 yield 发出，记录错误后正常结束即可
    return
```

（`openai_source.py:700-704`，在 `_query_stream` 收尾的 `try` 块）

语义是「已经发出去的收不回，所以当作正常结束」。**问题**：上层无法区分「完整回答」和「半截回答」，用户会看到句子断在半句，而系统认为这轮成功了。本项目若要遵循「不编造、如实告知」的既有原则，就不该照抄。

### 2.5 备用 provider 与选路

配置（`astrbot/core/config/default.py:3601-3607`）：

```python
"agent.config.model.fallback_provider_ids": {
    "_special": "select_providers",
    "hint": "主聊天模型请求失败时，按顺序切换到这些模型。",
    ...
}
```

解析函数 `resolve_fallback_providers`（`astrbot/core/astr_agent_runtime/main_agent/providers.py:107-151`）做三件事：去重（主模型不得重复出现在 fallback 里）、跳过不存在的 id、跳过类型非法的项。docstring 明确 `fallback_ids: Configured provider IDs in **retry order**`（`:115`）——**顺序即优先级**。

另有能力预选：`select_image_provider`（`providers.py:154+`）在请求带图片而主模型不支持图片时，提前切到支持图片的 fallback，而不是等失败再切。

### 2.6 AstrBot 没查到的部分

- **无熔断**：`backend` 目录下 `circuit|breaker|cooldown|连续失败` 零命中。
- **无幂等键**：没查到对同一请求去重/去重复计费的机制。
- **空回复、响应解析失败**：没查到作为独立可重试类别的实现。

---

## §3 MaiBot：实现细节与源码出处

### 3.1 配置项与默认值

```python
max_retry: int = Field(
    default=3, ge=0, ...
)
"""最大尝试次数 (单个模型API调用失败时, 含首次请求在内最多发送的请求总数, 设为 0 或 1 表示不重试)"""

timeout: int = Field(default=120, ge=1, ...)
retry_interval: int = Field(
    default=4, ge=1, ...
)
"""重试间隔 (如果API调用失败, 重试的间隔时间, 单位: 秒)"""
```

（`src/config/model_configs.py:161-188`）注意 MaiBot 选的是**固定间隔**而不是指数退避——实现简单，但对限流（429）不如指数退避友好。

默认 provider 模板同样给这组值（`src/config/default_model_config.py:12-19`）：

```python
{"name": "DeepSeek", "base_url": "https://api.deepseek.com", ...,
 "max_retry": 3, "timeout": 120, "retry_interval": 4}
```

### 3.2 重试循环：按异常类型分派

主循环在 `src/llm_models/utils_model.py:1084-1292`：

```python
retry_remain = retry_limit if retry_limit is not None else api_provider.max_retry
retry_remain = max(1, retry_remain)
max_attempts = retry_remain
...
while retry_remain > 0:
    ...
    try:
        response = await client.get_response(active_request)
        ...
        return response
    except EmptyResponseException as e:      # :1109
        ...
    except NetworkConnectionError as e:       # :1141
        ...
    except RespNotOkException as e:           # :1192
        ...
    except RespParseException as e:           # :1254
        ...
    except ReqAbortException:                 # :1284
        raise
    except Exception as e:                    # :1286
        ...
        raise ModelAttemptFailed(..., original_exception=e) from e
```

各分支的行为：

| 异常 | 次数消耗 | 是否 sleep | 出处 |
|---|---|---|---|
| `EmptyResponseException` | −1 | ✅ `retry_interval` | `:1117-1137` |
| `NetworkConnectionError` | −1 | ✅ | `:1161-1187` |
| `RespNotOkException` 429/5xx | −1 | ✅ `continue` | `:1196-1221` |
| `RespNotOkException` 413 + 可压缩 | **不消耗** | ❌ 直接 `continue` | `:1219-1243` |
| `RespNotOkException` 其他 4xx | — | — | 抛硬错误 `:1245-1249` |
| `RespParseException` | −1 | ✅ | `:1261-1279` |
| `ReqAbortException` | — | — | **直接 `raise`，不重试** `:1284` |
| 其他未知 | — | — | 抛硬错误 `:1286-1290` |

**值得借鉴的两条**：

1. **413 压缩重试不消耗配额**（`:1219-1243`）。注释写明 `压缩消息本身不消耗重试次数`。因为这不是「抖了一下」，而是请求本身超限，压缩后有很大概率成功，不应该挤占真正的重试预算。
2. **`ReqAbortException` 直接透传**（`:1284`）。用户主动取消/被抢占，重试毫无意义。

### 3.3 单模型重试耗尽后换下一个模型

注释已经写清设计意图（`utils_model.py:1305-1310`）：

> 单次模型尝试超时时把 TimeoutError 转成 LLMTaskTimeoutError（继承 ModelAttemptFailed），由 `_execute_request` 内既有的 `except ModelAttemptFailed` 分支接住，按"**切下一个模型**"的既有语义处理（penalty +1、usage_penalty -1、failed_models_this_request 登记）。

实现（`utils_model.py:1458-1479`）：

```python
except ModelAttemptFailed as e:
    last_exception = e.original_exception or e
    ...
    logger.warning(f"模型 '{model_info.name}' 尝试失败，切换到下一个模型。原因: {e}")
    self._adjust_model_usage(model_info.name, penalty_delta=1, usage_penalty_delta=-1)
    failed_models_this_request.add(model_info.name)
    if model_index < max_attempts - 1:
        await asyncio.to_thread(update_failed_request_attempt, last_exception, status="switching_model")

    if isinstance(last_exception, RespNotOkException) and last_exception.status_code == 400:
        logger.warning("收到客户端错误 (400)，跳过当前模型并继续尝试其他模型。")
        continue
```

全部模型失败后（`:1482-1493`）抛最后一个异常并上报事件。

### 3.4 软惩罚替代硬熔断

`utils_model.py:125` 注释：

> 模型使用量记录，用于进行负载均衡，对应为(total_tokens, penalty, usage_penalty)，**惩罚值是为了能在某个模型请求不给力或正在被使用的时候进行调整**

失败 → `penalty_delta=1`（`:1474`）；成功/中断 → `usage_penalty_delta=-1`（`:1443,1451`）。**累计惩罚只影响排序，不会把模型彻底排除**，恢复也是自动的。对比本项目的硬熔断（`health.is_open()` 直接跳过），软惩罚对「偶发一次失败」更宽容。

### 3.5 流中途中断：只在主动取消时保留半截输出

```python
except (asyncio.CancelledError, ReqAbortException) as exc:
    if interrupt_flag is not None and interrupt_flag.is_set():
        raise ReqAbortException(
            "请求被外部信号中断", partial_output=accumulator.snapshot_interrupted_output()
        ) from exc
    raise
```

（`src/llm_models/model_client/openai_client.py:1374-1378`）

半截输出的语义（`src/llm_models/exceptions.py:8-29`）：

```python
@dataclass
class InterruptedStreamOutput:
    """用于历史展示和继续推理的流式片段，不作为可执行的工具响应。"""
    ...
    def display_text(self) -> str:
        """保留未完成的原始文本和工具参数，不尝试修复或执行。"""
```

**这是本任务最重要的一条设计参考**：MaiBot 明确区分「保留」和「可用」——半截文本可以进历史、可以给下一轮当上下文，但**不解析、不执行其中的工具调用**（`snapshot_interrupted_output` 注释 `:1303`：`避免用完整响应解析器解析半截工具参数`）。它**不会**因为收到半截就重发请求。

### 3.6 MaiBot 没查到的部分

- **没有硬熔断**（只有软惩罚）。
- **没有指数退避**（固定 `retry_interval`）。
- **流中途的网络错误不重试**：`_default_stream_response_handler` 只捕获 `CancelledError`/`ReqAbortException`，其他异常自然冒泡。
- **幂等**：有失败请求快照与 attempt 计数（`utils_model.py:1085-1089` 递增 `trace_context.attempt` / `model_attempt`），但没查到「同一请求去重」的机制。

---

## §4 本项目 ling 的现状（复核）

| 事实 | 出处 |
|---|---|
| LLM 请求无重试，单次 `httpx.AsyncClient(timeout=...)` | `src/ling/models/openai_compatible.py:90` |
| 超时抛 `ModelErrorKind.timeout`，`retriable=True` | 同文件 `:138-141` |
| 连接错误抛 `unavailable`，`retriable=True` | 同文件 `:142-145` |
| 4xx 抛 `retriable=False` | 同文件 `:95-98` |
| 5xx 抛 `retriable=response.status_code >= 500` | 同文件 `:100-110` |
| **`retriable` 在 LLM 调用链上无人消费** | `router.py` 全文未读取该字段 |
| 回退仅在尚未产出文本时 | `src/ling/models/router.py:229` 注释 |
| 硬熔断：`health.is_open()` 跳过候选 | `router.py:262-265` |
| 后台任务重试 5 次 | `worker.py:50` `MAX_ATTEMPTS = 5` |

**结论**：ling 已经把「哪些错误值得重试」的判断做完了（`retriable` 字段），只是**没有执行重试的那一层**。这是本次改动最省力的切入点——不需要重新设计错误分类，只需要消费已有的 `retriable`。

---

## §5 对 ling 的方案建议

### 5.1 先说清「无条件重试」的风险

用户要求「无条件重试」。**按字面实现是不安全的**，有两个理由：

**理由一：参数/鉴权错误重试必然失败。** 400（参数不合法）、401（key 错）、403（无权限）、404（模型名错）重试 3 次不会变成功，只会：多耗 3 倍时间和额度、把一次清晰的错误变成 4 次、严重时触发供应商风控。AstrBot（`request_retry.py:53-59`）和 MaiBot（`utils_model.py:1245-1249`）**都明确排除**了这些。用户说「无条件重试」，实际想表达的应该是「**别让我手动再发一遍**」，而不是「对 401 也重试三遍」。

**理由二：流已开始后的重试会让用户看到重复文字。** 这是本项目自己的约束（`router.py:229` 注释已写明「回退只发生在尚未产出任何文本时」），AstrBot/MaiBot 也都不在流内重试。原因很直白：用户已经看到「今天天气不错，我们」这半句，重发请求会从头再来一遍「今天天气不错，我们」，屏幕上就是：

> 今天天气不错，我们
> 今天天气不错，我们去公园吧

在 QQ/Web 前端是两条消息，读起来像复读。**如果非要在流开始后重试，唯一可接受的做法是先把已发内容撤回或声明作废**，而很多平台不支持撤回。

所以建议把用户的「无条件」理解为：**覆盖所有「重试可能成功」的错误类别（网络/超时/429/5xx/空回复），不再让单次抖动直接变成失败**——而不是「对所有错误、包括已吐出文字的流」。

### 5.2 分层方案

**Layer 0：错误分类（复用现有 `retriable`）**

| 现有 kind | `retriable` | 建议行为 |
|---|---|---|
| `timeout` | `True` | 重试 |
| `unavailable` | `True` | 重试 |
| 5xx | `True` | 重试 |
| 429 | 目前落入 4xx `retriable=False` | **改为 `True`**，需单独退避 |
| 400/401/403/404 | `False` | **不重试**，直接换候选或报错 |
| 缺 secret | `False` | 不重试 |

**Layer 1：同候选内重试**

- 次数：**3 次（含首次）**。理由：MaiBot 用 3，AstrBot 用 5。ling 是聊天场景，用户在线等，5 次 × 最长 30s 退避会让人以为死机。3 次配合下面的退避，最坏等待约 `1+2=3s`，可接受。
- 退避：**指数，基准 1s，上限 8s，带抖动**。`sleep = min(8, 1 * 2**(n-1)) * (0.5 + random()*0.5)`。
  - 抖动的理由：多会话同时失败时不希望它们同步重试造成脉冲。
  - 上限 8s 而不是 AstrBot 的 30s：聊天场景等 30 秒不如直接换模型。
- **429 特殊**：若响应带 `Retry-After`，按它退避（但不超过上限）；否则用更长的基准（如 2s）。

**Layer 2：换候选（已在 `router.py` 实现，保持一致）**

- 顺序：**先在同一候选内重试到耗尽，再换 fallback**。这与 MaiBot 的语义一致（`utils_model.py:1458`）。
  - 理由：换模型意味着不同的风格、可能不同的上下文限制；能用原模型答就用原模型答，保持人格一致。
- 保持现有约束：**只在尚未产出文本时换**。

**Layer 3：流已开始后的失败处理（关键设计）**

一旦已经 yield 过至少一个 chunk，**不再重试、不再换模型**。按以下顺序处理：

1. **能优雅收尾就收尾**：如果中断位置在句子边界附近，补一个终止标点后正常结束这轮，并在内部标记 `truncated=True`。
2. **否则如实告知**：不要像 AstrBot 那样 `return` 静默成功。应当把这一轮标记为「部分完成」，让上层决定是否追加一句短说明（措辞交给人格化的兜底文案模块，见 `DEFERRED-FALLBACK-DESIGN.md`）。
3. **绝不重发**：不重新构建请求，避免重复文字与重复扣费。
4. **保留半截文本**：照 MaiBot 的做法进历史，但**不解析其中的工具调用/结构化字段**（`openai_client.py:1303` 注释的理由：半截 JSON 解析必然失败或产出错误参数）。

**Layer 4：熔断改造（可选，建议做）**

现在 `health.is_open()` 是硬跳过。建议参考 MaiBot 的软惩罚：连续失败 N 次后**降权**而不是**排除**，并在成功一次后快速恢复。理由：单人使用的角色系统里，fallback 可能只有 1 个，硬熔断会把用户直接锁在「两个都不可用」的状态。

### 5.3 幂等与重复扣费

重试必然带来重复请求。需要注意：

- **非流式**：失败后重试不会产生「重复的可见结果」，只是可能重复计费（如果供应商在超时前已生成并计费）。无法完全避免，只能靠限制重试次数控制成本。
- **流式**：本方案在流开始后不重试，因此不会出现「同一轮回复发两条」的问题。
- **对外发送**：`worker.py` 已经用 `MAX_ATTEMPTS=5` 做任务级重试，如果 LLM 层也重试，**两层会相乘**（5×3=15 次）。建议：任务级重试只负责「任务调度失败」，LLM 层重试只负责「单次调用失败」，并在任务级把 `retriable=False` 的错误标记为不可重试直接失败，避免叠乘。

### 5.4 建议的配置项

```toml
[model.retry]
enabled = true
max_attempts = 3          # 含首次
backoff_base_s = 1.0
backoff_max_s = 8.0
jitter = true
retry_on_rate_limit = true
respect_retry_after = true
retry_after_streaming_started = false   # 关键：流已开始不重试
```

`retry_after_streaming_started` 建议**永远保持 false 且不暴露给用户配置**，因为它一旦为 true 必然导致重复文字。

---

## §6 风险与边界

1. **两层重试叠乘**（`worker.py` × LLM 层）会把最坏等待放大到分钟级。必须显式约束。
2. **流式重试=重复文字**，无解，只能禁止。AstrBot 的「静默 return」是反面教材。
3. **429 重试可能加剧限流**。AstrBot 为此留了 `retry_rate_limits` 开关（`request_retry.py:56`）。本项目应支持单 provider 关闭 429 重试。
4. **换模型影响人格一致性**。同一段对话里换到另一个模型，语气可能突变。建议只在「用户还没看到任何输出」时换（现状已如此），不要为了成功率牺牲一致性。
5. **重试会延长用户等待**。聊天场景里「等 10 秒拿到回复」有时不如「3 秒后说一句抱歉」。建议给整轮 LLM 调用设总时间上限（MaiBot 用 `hard_timeout`，见 `utils_model.py:1311`），超出就直接走兜底文案而不是继续重试。
6. **没查到的部分**：两家的「幂等去重」机制都没查到；AstrBot 的空回复/解析失败重试没查到；MaiBot 的指数退避没查到。这三处属于「实现里没有」，不是「我没找到」。

---

## §7 参考资料

- AstrBot 仓库：<https://github.com/AstrBotDevs/AstrBot>（`master`，46617★）
  - `astrbot/core/provider/sources/request_retry.py`（重试核心，185 行）
  - `astrbot/core/provider/sources/openai_source.py`（调用点 `:570`、`:630`；流内异常 `:700-704`）
  - `astrbot/core/config/default.py:3601-3620`（配置项与提示语）
  - `astrbot/core/config/agent_runner.py:10-18`（默认值）
  - `astrbot/core/astr_agent_runtime/main_agent/providers.py:107-176`（fallback 解析）
- MaiBot 仓库：<https://github.com/Mai-with-u/MaiBot>（`main`，6966★）
  - `src/llm_models/utils_model.py`（重试循环 `:1084-1292`，换模型 `:1458-1493`）
  - `src/llm_models/exceptions.py`（`InterruptedStreamOutput` 语义）
  - `src/llm_models/model_client/openai_client.py:1303-1378`（流中断快照）
  - `src/config/model_configs.py:161-188`（`max_retry` / `timeout` / `retry_interval`）
  - `src/config/default_model_config.py:12-19`（默认模板）
