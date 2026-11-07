# 模型职责绑定完整性审计

审计目标：**有没有"需要调用模型、但没列进可绑清单"的地方**。

结论：**没有漏列**。当前所有**实际会路由**的模型调用都能在可绑清单里找到对应角色。
但审计发现一个**反向问题**（列了却没有调用方）和一类**结构性问题**（真在调模型、但
配置路径完全在绑定体系之外）。

## 一、实际会路由的调用点（全部可绑）

| 调用点 | 角色 | 说明 |
|---|---|---|
| `kernel/turn.py:241` | `dialogue.fast`（默认） | 主对话 |
| `kernel/autonomy.py:369` | `dialogue.group` / `dialogue.fast` | 群聊走 group、私聊走 fast |
| `domains/media/describe.py:45` | `perception.vision` | 图片转描述 |
| `worker.py:2041` | `memory.extract` | 记忆提炼（未绑时退回 fast） |
| `domains/proactive/draft.py:53` | `dialogue.fast` | 主动联系草稿，复用 fast |

五个可绑角色里，四个有真实调用方。

## 二、反向问题：`dialogue.deep` 绑了没有任何效果

`_OFFERABLE_PROFILES`（`api/routes/settings.py:26`）里列了 `dialogue.deep`，
设置页会显示「深度对话」。但**全仓没有任何代码按这个角色发起请求**：

```
$ grep -rn "dialogue.deep" --include=*.py src/ tests/
src/ling/models/registry.py:152        # 只是遍历三个 dialogue 角色做预热
src/ling/api/routes/settings.py:29     # 列出可绑
tests/integration/test_real_model.py:61,163   # 测试
```

`turn.py` 的 `profile` 参数默认 `dialogue.fast`，**没有任何调用方传 deep**。

这正是这个文件自己要避免的错（`settings.py:20-25`）：
> 只列**绑定后真的会生效**的……展示出来等于承诺一个不存在的能力。

两条出路，选一条：
1. **实现深/浅路由**：按上下文长度或任务类型在 fast/deep 之间挑（要给出判定阈值，
   且必须可解释——用户得知道"为什么这次走了 deep"）。
2. **移进 `RESERVED_PROFILES`**：和 `reflection`/`planning` 一样，注明"尚无实现"。

在选之前，设置页显示它是有误导性的。

## 三、结构性缺口：真在调模型，但配置路径在绑定体系之外

| 角色 | 现状 | 为什么没纳入绑定 |
|---|---|---|
| `memory.embed` | `LING_EMBEDDING_*` 环境变量，`domains/memory/providers.py:20` | 走 Cloudflare 嵌入协议，与 chat 供应商不是同一套接口 |
| `memory.rerank` | `LING_RERANK_*` 环境变量 | 走 SiliconFlow 重排协议，同上 |
| `speech.asr` / `speech.tts` / `realtime.voice` | `AsrProvider`/`TtsProvider` 全仓只有 `UnconfiguredSpeechProvider` | 没有按模型职责构造的 provider，绑了不会有代码读 |

这三类都已写进 `RESERVED_PROFILES`（`settings/service.py:82`），所以不是"漏了"，
而是**配置入口在环境变量、不在界面**。

代价是：用户在设置页看不到"嵌入/重排到底配没配"。记忆检索的 rerank 没配时，
日志里会说（`domains/memory/service.py:365`），但界面不说。

## 四、建议

1. **`dialogue.deep` 必须先裁决**（实现或移走）——这是唯一会误导用户的一项。
2. 嵌入/重排**值得一个只读展示位**（配没配、用的什么模型），不必做成可绑——
   它们的协议与 chat 供应商不同，硬塞进同一个下拉只会让人选到不可用的东西。
