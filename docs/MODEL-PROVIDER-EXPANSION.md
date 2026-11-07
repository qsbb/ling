# 模型供应方扩展：嵌入 / 重排 / 语音 也进绑定体系

## 一、问题

`memory.embed` / `memory.rerank` / `speech.*` 是**真在调用模型**的角色，但它们的
配置路径完全在绑定体系之外：

```
domains/memory/providers.py:18   build_embedding()  → LING_EMBEDDING_API_KEY / _ACCOUNT_ID / _MODEL
domains/memory/providers.py:38   build_reranker()   → LING_RERANK_API_KEY / _MODEL / _API_BASE
```

后果：设置页看不到它们配没配、用的什么模型，只能靠日志
（`domains/memory/service.py:365` 会说 rerank 没配）。用户以为配好了，其实语义检索
一直没生效。

## 二、为什么不能直接把它们塞进现在的下拉

现有供应商目录（`ProviderKind`）是 **chat 协议**：`openai_compatible` /
`anthropic` / `chat_responses` / `local` / `fake`。嵌入和重排是**完全不同形状的
接口**：

| 角色 | 协议 | 请求形状 |
|---|---|---|
| `memory.embed` | Cloudflare Workers AI | `POST /accounts/{id}/ai/run/{model}`，返回 `{result:{data:[[...]]}}` |
| `memory.rerank` | SiliconFlow | `POST /v1/rerank`，返回 `[{index,relevance_score}]` |
| `speech.tts` | OpenAI 兼容 | `POST /v1/audio/speech`，返回音频字节流 |
| `speech.asr` | OpenAI 兼容 | `POST /v1/audio/transcriptions`，multipart 上传 |

把嵌入模型放进 chat 供应商的下拉里，用户会选到一个**调不通**的组合。所以必须先有
**能力维度**，再让绑定按能力过滤。

## 三、设计：供应商带「能力」，绑定按能力过滤

### 1. 供应商增加能力标记

`ProviderKind` 增加三个值（与既有 chat 种类并列，不是它的别名）：

```
cloudflare_embedding    # 嵌入
siliconflow_rerank      # 重排
openai_audio            # 语音（TTS/ASR，OpenAI 兼容 /v1/audio/*）
```

每个 kind 声明它支持的能力（`CAPABILITIES: dict[ProviderKind, frozenset[str]]`）：

| kind | chat | embed | rerank | tts | asr |
|---|---|---|---|---|---|
| `openai_compatible` / `anthropic` / `chat_responses` / `local` / `fake` | ✓ | | | | |
| `cloudflare_embedding` | | ✓ | | | |
| `siliconflow_rerank` | | | ✓ | | |
| `openai_audio` | | | | ✓ | ✓ |

### 2. 角色增加能力需求

每个模型角色声明它要哪种能力（`ModelRole → capability`）：

| 角色 | 能力 |
|---|---|
| `dialogue.fast` / `dialogue.group` / `memory.extract` | `chat` |
| `perception.vision` | `chat`（且要求模型支持视觉，见下） |
| `memory.embed` | `embed` |
| `memory.rerank` | `rerank` |
| `speech.tts` | `tts` |
| `speech.asr` | `asr` |

设置页的绑定下拉**只列具备该能力的供应商**。选不到调不通的组合，是这个设计的
核心收益。

### 3. 装配改为「先读绑定，再退回环境变量」

```python
def build_embedding(binding: ProfileBinding | None = None):
    if binding is not None:          # 网页配了就用网页那份
        return CloudflareEmbedding(...)   # 凭据来自供应商条目
    return _from_env()               # 没配就退回环境变量（向后兼容）
```

**必须保留 env 回退**：已有部署是 env 配的，升级后不能突然变成"未配置"，那会让
语义检索静默失效——正是这次要修的毛病。

### 4. 语音：先把 provider 写出来，再谈绑定

`AsrProvider` / `TtsProvider` 全仓只有 `UnconfiguredSpeechProvider`。**在 provider
存在之前，"列出来"就是承诺一个不存在的效果**——和 `dialogue.deep` 是同一个错。

所以语音分两步：
1. 实现 `openai_audio` 适配器（TTS `/v1/audio/speech`、ASR `/v1/audio/transcriptions`）
2. 再把它加进可绑清单

在那之前，设置页对语音只做**只读展示**：「未实现」，不放下拉。

### 5. `dialogue.deep` 移除

审计确认没有任何代码按它发起请求。移进 `RESERVED_PROFILES` 并注明"尚无实现"，
与 `reflection` / `planning` 一致。

## 四、迁移与兼容

- 供应商表新增 kind 值：迁移只放宽校举约束，不改数据
- 角色绑定表不加字段：`memory.embed` 等复用现有的 `profile → (provider, model)`
- 环境变量全部保留为回退路径，`env.example` 里标注"配了网页绑定后此项不再生效"

## 五、不做的事

- 不把嵌入/重排做成"和 chat 同一个下拉"——协议不同，混在一起必然选出调不通的组合
- 不在 provider 实现之前先放出语音绑定入口
- 不引入新的第三方依赖（`openai_audio` 用标准库 + httpx，与既有适配器一致）
