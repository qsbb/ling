# 看视频：原生视频输入 vs 抽帧，以及「她自己决定要不要看」怎么落地

调研时间 2026-11-07。目标回答一件事：**给 ling 加「看视频」时，钱该花在哪、什么时候花、由谁决定。**

**核实的网络条件先说清楚**（否则下面的"官方文档"会被误读）：

| 域名 | 状态 | 后果 |
|---|---|---|
| `ai.google.dev` / `cloud.google.com` / `generativelanguage.googleapis.com` | 全部超时（curl exit 28） | Gemini 官方文档**打不开**，数字取自 Google 官方 GitHub 仓库 + 第三方逐字镜像，逐条标注 |
| `platform.openai.com` / `developers.openai.com` | HTTP 403 | OpenAI 平台文档打不开，改用 `openai/openai-python` 类型定义与 `openai/openai-cookbook` |
| `platform.claude.com` / `docs.anthropic.com` | 跳转后 "App unavailable in region" | Anthropic 文档打不开，改用 `anthropics/anthropic-sdk-python` 类型定义 |
| `help.aliyun.com`、`volcengine.com`、`docs.bigmodel.cn`、`platform.kimi.com`、`api-docs.deepseek.com` | 可达 | 这几家是**直接读官方文档**核实的 |
| `platform.minimaxi.com` | 文档页是 JS 壳（6 KB，正文拿不到） | MiniMax **未证实** |

标注约定：**【已核实】** = 我这次真的读到了官方来源；**【未证实】** = 查不到或读不到，不要当结论用；**【经验值】** = 我的判断，没有出处。

---

## 结论速览

1. **原生支持视频输入的**（本次核实到的）：Gemini 全系、阿里 Qwen-VL 系、豆包（火山方舟）、智谱 GLM-4.5V/5.3-Flash、Kimi（`video_url`）。**不支持**：OpenAI（无 video content part）、Anthropic Claude（无 video block）、DeepSeek（只有图片）、MiniMax（未证实）。
2. **"原生"不等于"原生抽帧率"**：Gemini 默认 1 fps，Qwen 默认 **2 fps**，豆包默认 1 fps（上限 5、下限 0.2）。同样是"原生视频"，**1 分钟视频的 token 差 13 倍**（Gemini low ≈ 5.9k vs Qwen 默认 ≈ 76.8k）。所以"原生视频比抽帧便宜"这句话**不成立**，得按厂商算。
3. **【已核实】Qwen 明确写着"不支持对视频文件的音频进行理解"**。这意味着：如果视频信息在语音里（群聊视频里非常常见），**哪家的原生视频都替不掉 ASR**。
4. 对本仓库最要紧的一条：**现在这套是"入站即读、每 60 秒扫一遍、不看她的意愿"**（`worker.py:2348,2546`），正好和用户要的相反；而且**未看的视频在她上下文里完全不存在**（`kernel/context.py:420` 只取已描述的）。所以第一步不是"接视频模型"，是**把"有没有看"和"看没看见"拆开**。
5. 推荐：**抽帧为主，原生视频为可选升级**。理由不是"抽帧更便宜"（在 1 分钟内两者价格相当），而是：① 抽帧不绑厂商，换模型不掉链子；② 原生视频在 Qwen/豆包的默认参数下**贵一个数量级**且**吃掉了音频还是没有**；③ 本仓库连 mp4 都还没拆开（`describe.py:315-320` 自述的局限）。

---

## A. 原生接受视频输入的模型（逐家核实）

### A1. Google Gemini

**【已核实（来源受限）】** 官方域名不可达。以下两条 Google 自家 GitHub 仓库内容可读，且与镜像一致：

- `google-gemini/cookbook` → `quickstarts/Video_understanding.ipynb`：*"By default, static video processing samples frames at a fixed rate of 1 frame per second (FPS). You can override this sampling rate by providing a custom `fps` value in `types.VideoMetadata`."*
  <https://github.com/google-gemini/cookbook/blob/main/quickstarts/Video_understanding.ipynb>
- 同一 notebook 还写了 **agentic video understanding**（模型自己决定看哪段）：*"the model dynamically explores the video timeline, selectively inspecting transcripts and adaptively adjusting frame rates and resolution on the fly"*，并称 *"Up to 88% more token-efficient"*、支持的模型是 "Gemini 4 Argon, 3.8 Flash, 3.7 Flash, 3.6 Flash, 3.5 Flash Lite"。

**【未证实（第三方逐字镜像）】** 官方页 <https://ai.google.dev/gemini-api/docs/video-understanding> 打不开。我读的是 `wenerme/ai` 仓库里对该页的逐字镜像
<https://github.com/wenerme/ai/blob/main/skills/google-ai-docs/references/video-understanding.md>，内容与 cookbook 的"默认 1 FPS"一致，但我**没能在官方域名上确认**。它写的是：

| 项 | 值 |
|---|---|
| 默认采样 | 1 FPS（static 模式）；音频按 1 Kbps 单声道处理，每秒加时间戳 |
| 每帧 token | `media_resolution=low` → **66 tokens/帧**；否则 → **258 tokens/帧** |
| 音频 token | **32 tokens/秒**（与画面**一起计入**输入 token） |
| 合计 | 默认（low）≈ **100 tokens/秒**；高分辨率 ≈ **300 tokens/秒** |
| 格式 | `video/mp4`、`video/mpeg`、`video/mov`、`video/avi`、`video/x-flv`、`video/mpg`、`video/webm`、`video/wmv`、`video/3gpp` |
| 时长上限 | 1M 上下文模型：low 分辨率约 **3 小时**，高分辨率约 **1 小时** |
| 请求上限 | Gemini 2.5 及以后：**单请求最多 10 个视频**；内联 < 20 MB 走请求体，更大用 Files API |

网上常引的「**263 tokens/帧**」我没找到任何官方来源（GitHub 全站搜索 "263 tokens per frame" 只有 1 命中，"258 tokens per frame" 有 94 命中），**按未证实处理**。

> 对 ling 的含义：Gemini 的"原生视频"在 low 档下是**目前所有方案里最便宜的**（1 分钟 ≈ 5.9k tokens），而且**音频白送**。如果只挑一家做原生视频，就是它。

### A2. OpenAI（GPT-4o / GPT-5 系列等）

**【已核实：不支持原生视频输入】**

- `openai/openai-python`（main 分支）里 chat 的多模态段只有
  `ChatCompletionContentPartTextParam | ImageParam | InputAudioParam | File`，
  <https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_content_part_param.py>
- Responses API 的输入段同样只有 `text | image | file`，
  <https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_input_content_param.py>
- `openai/openai-openapi` 的 `openapi.yaml` 里确实有 `/videos`，但那是**视频生成**（`createVideo`、"Create a new video generation job from a prompt"），**不是视频输入**。<https://github.com/openai/openai-openapi/blob/master/openapi.yaml>

**【已核实】官方推荐做法就是一帧一帧送图**：`openai/openai-cookbook` 的
`examples/GPT_with_vision_for_video_understanding.ipynb` 开头原话：
*"Although GPT-4.1-mini doesn't take videos as input directly, we can use vision and the 1M token context window to describe the static frames of a whole video at once."*
代码用 OpenCV 逐帧读，然后 `base64Frames[0::25]` —— **每 25 帧取 1 张**（对 25 fps 素材约等于 1 fps）。
<https://github.com/openai/openai-cookbook/blob/main/examples/GPT_with_vision_for_video_understanding.ipynb>

图片 token 公式（官方 vision 文档快照，随 cookbook 一起发布）：
`detail:low` 固定 **85 tokens**；`detail:high` 先缩到 2048² 内、再把短边拉到 768px，
按 512×512 切块，每块 **170 tokens**，另加固定 **85 tokens**。
例：1024×1024 high = `170×4+85 = 765`。
<https://github.com/openai/openai-cookbook/blob/main/examples/data/oai_docs/vision.txt>

### A3. Anthropic Claude

**【已核实：只收图，不收视频】** `anthropics/anthropic-sdk-python`（main）的 Messages 内容块联合类型里
**没有任何 video 块**，只有 text / image / document / tool_use / tool_result / thinking 等：
<https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/content_block_param.py>

图片允许的媒体类型是 `image/jpeg`、`image/png`、`image/gif`、`image/webp`（含 GIF，但只当动图/首帧处理）：
<https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/base64_image_source_param.py>

你的记忆是对的：**Claude 不收视频**。要给它看视频，只能自己抽帧当图片送。

### A4. 阿里 Qwen-VL（含 OpenAI 兼容模式）

**【已核实，直接读官方文档】**

- 入口：<https://help.aliyun.com/zh/model-studio/vision>（"图像与视频理解"）
- 两种传法：**视频文件**（`{"type": "video_url", "video_url": {"url": ...}}`，OpenAI 兼容/HTTP 都是把 `type` 设成 `video_url`）或**图像列表（就是帧序列）**。
- **抽帧参数**：
  - `fps`：**默认 2.0**，取值 `[0.1, 10]`，"每隔 1/fps 秒抽取一帧"。
  - `max_frames`：抽帧上限，超过就均匀重抽；**仅 DashScope SDK 可用**（OpenAI 兼容模式没有）。
- **token 公式**（同页"计算图像的 Token"）：单帧 `h_bar × w_bar / (32×32) + 2`；
  视频先抽帧再"算所有帧的总和"。官方估算脚本给的常量：`qwen3-vl-plus` 单帧像素上限 `640×32×32`
  → **≈640 tokens/帧**；其他模型 `768×32×32` → **≈768 tokens/帧**；最少 4 帧，最多 2000 帧。
- **时长/数量上限**（同页"视频限制"）：按模型分档，
  `qwen3.8/3.7/3.6/3.5` 系列 **2 秒–2 小时**、`qwen3-vl-plus` **2 秒–1 小时**、
  其他 Qwen3-VL 开源 **2 秒–20 分钟**、Qwen2.5-VL/QVQ **2 秒–10 分钟**、其他模型 **2 秒–40 秒**；
  单请求最多 **64 个视频**；图像列表方式最少 4 张、最多 512～8000 张（按模型）。
- **音频**：同页明写「**音频理解：不支持对视频文件的音频进行理解。**」← 这条很关键。
- 价格（官方计费页 <https://help.aliyun.com/zh/model-studio/billing-for-model-studio>，华北2/北京）：
  `qwen3-vl-plus` 输入 **¥1 / 百万 token**（0–32K），¥1.5（32K–128K），¥3（128K–256K）；输出 ¥10/15/30。

### A5. 豆包 / 火山方舟（Doubao）

**【已核实，直接读官方文档】** <https://www.volcengine.com/docs/82379/1895586>

- 形式：`{"type": "video_url", "video_url": {"url": ...}}`（也支持 `data:video/mp4;base64,...`、Files API 的 `file_id`），可在段内带 `fps`。
- **`fps` 默认 1，最高 5，最低 0.2**（"当视频画面变化剧烈…可调高 fps（最高 5）…可调低 fps（最低 0.2）"）。
- **抽帧策略**（同页"抽帧策略"）：抽帧数区间 **[16, 1280]** 帧；
  `min_frames` 默认 **16**；`min_frame_tokens` 在 **[64, 384]** 连续可调、**默认 384**；
  `max_video_tokens` 默认 **81920**（80×1024）。
  - 帧太多（>1280）：按 64 tokens/帧、均匀抽 1280 帧。
  - 帧太少（<16）：视频总帧数 ≥16 就均匀抽 16 帧，否则全取。
- 时序：每帧前面插时间戳文本（`4.0 second` 这种格式）。

> 对 ling 的含义：豆包是**唯一把"每帧 token 上限"和"整段 token 上限"做成公开旋钮**的家（`min_frame_tokens` / `max_video_tokens`）。要控成本，它最可控。

### A6. 智谱 GLM-4.5V / GLM-5.3-Flash

**【已核实，直接读官方文档】**

- 传法：`{"type": "video_url", "video_url": {"url": "https://..."}}`，
  官方 OpenAPI 示例里 `model` 是 `glm-5.3-flash`。
  <https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash>、<https://docs.bigmodel.cn/openapi/openapi.json>
- 能力自述："原生多模态输入，支持图片，视频，文件"（GLM-5.3-Flash）；GLM-4.5V 同页写"支持解析长时视频内容"。
- **限制**：GLM-4.5V 页明写「**不支持同时理解文件、视频和图像**」。
- **抽帧率、单帧 token、时长上限：文档里没写 →【未证实】**。它没有 `fps` 参数（OpenAPI 里 `fps` 只出现在**文生视频** `cogvideox` 的参数上）。

### A7. Kimi（Moonshot）

**【已核实，直接读官方文档】**

- 传法：chat completions 的 content 段支持 `{"type":"video_url","video_url":{"url":...}}`，
  也接受字符串简写；`data:video/mp4;base64,...` 或 **`ms://<file_id>`**（Moonshot 自己的文件协议）。
  <https://platform.kimi.com/docs/api/chat>
- 支持格式：`video/mp4, mpeg, mov, avi, x-flv, mpg, webm, wmv, 3gpp`。
- **只收 base64 或 file_id，不收公网 URL**——文档"功能支持与限制"里写「URL 格式的图片：不支持，目前仅支持使用 base64 编码的图片内容和通过文件 ID 上传的图片/视频」。这对 ling 是很实际的约束（QQ 的媒体链接通常不是公开可长期访问的）。
- 建议分辨率：图片 ≤4K、**视频 ≤1080p**，"再高的分辨率只会增加处理时间"。
- 顺带一条：GIF/WebP 动图走 `image_url` 传，但"底层可能会**按视频解码**，token 消耗也按视频方式计算"。
- **抽帧率与单帧 token：文档未给公式（只给 `/api/estimate` 估算接口）→【未证实】**。

### A8. DeepSeek

**【已核实：不支持视频】** 官方 Vision 页只讲图片：*"The `deepseek-flash` model accepts images alongside text"*，
支持格式 JPEG/PNG/GIF/WebP，单图上限 1024 tokens，全文**没有 video 字样**。
<https://api-docs.deepseek.com/guides/vision>

### A9. MiniMax

**【未证实】** 文档站是 JS 壳（`https://platform.minimaxi.com/document/...` 返回 6.4 KB 骨架），
正文拿不到，我不写猜测。

### A10. 一张表

| 厂商 | 原生视频 | 传法 | 默认 fps（可调范围） | 单帧 token | 视频音频 | 依据 |
|---|---|---|---|---|---|---|
| Gemini | ✅ | Files API / 内联 / YouTube | **1**（可自定义） | 66（low）/ 258 | **计入**，32 tok/s | 官方 cookbook + 镜像【部分未证实】 |
| OpenAI | ❌ | 抽帧当图片（`input_image`） | 自己做（cookbook 用 1/25 帧） | 85（low）/ 170×块+85 | 无（要另走 ASR） | 官方 SDK 类型 + cookbook【已核实】 |
| Claude | ❌ | 抽帧当图片 | — | 按图计（未核实具体数） | 无 | 官方 SDK 类型【已核实】 |
| Qwen-VL | ✅ | `video_url` 或帧列表 | **2.0**（0.1–10） | ≈640（vl-plus）/≈768 | **不支持** | 官方文档【已核实】 |
| 豆包 | ✅ | `video_url` / file_id | **1**（0.2–5） | 64–384 可调，默认 384 | 未提及 | 官方文档【已核实】 |
| 智谱 | ✅ | `video_url` | 未公开 | 未公开 | 未公开 | 官方文档【部分未证实】 |
| Kimi | ✅ | `video_url`（base64 / `ms://`） | 未公开 | 未公开（有 estimate 接口） | 未公开 | 官方文档【部分未证实】 |
| DeepSeek | ❌ | 只收图 | — | ≤1024/图 | — | 官方文档【已核实】 |
| MiniMax | **未证实** | — | — | — | — | 文档不可读 |

---

## B. 不原生支持时的通行抽帧策略

### B1. 固定 fps 采样：不同速率各适合什么

| fps | 1 分钟帧数 | 适合 | 依据 |
|---|---|---|---|
| 0.2 | 12 | 静态画面、长镜头、Vlog 式慢节奏；只想知道"这是什么视频" | 【经验值】；豆包允许下限正好是 0.2【已核实】 |
| 0.5 | 30 | 群聊短视频（10–60 秒）的默认档；能看出"在干嘛"，看不出"怎么变的" | 【经验值】 |
| 1 | 60 | **多家默认值**：Gemini static 模式、豆包默认 | Gemini cookbook + 豆包文档【已核实】 |
| 2 | 120 | 动作/游戏/舞蹈/教程步骤；**Qwen 默认值** | Qwen 文档【已核实】 |
| ≥4 | 240+ | 只在要数动作次数、看手势/字幕逐帧时用；豆包上限 5，Qwen 上限 10 | 【经验值】+ 两家文档 |

**"1 fps 是 Gemini 默认"有官方说明**：cookbook 原句（见 A1）。同时它也给了取舍：
*"Setting a higher FPS captures finer temporal details in fast-paced videos, while a lower FPS (such as 0.5 FPS) reduces token consumption for long or relatively static footage."* —— 这句可以直接引给用户看，省得我们自己论证。

### B2. 场景切换抽帧 vs 固定 fps

- ffmpeg 的做法是 `select` 滤镜的 `scene` 变量，常见写法 `-vf "select='gt(scene,0.3)',showinfo"`。
  **【已核实】** 官方滤镜文档对该变量的原文是：「`scene` (video only) value between 0 and 1 to indicate a
  new scene; a low value reflects a low probability for the current frame to introduce a new scene,
  while a higher value means the current frame is more likely to be one」
  —— 取值范围 0~1 是官方的；**具体取 0.3 属【经验值】**，官方没给推荐值。
  <https://ffmpeg.org/ffmpeg-filters.html#select>
- 差别（这部分是机理，不是参数）：固定 fps 保证**时间轴均匀**，可以放心说"第 3 秒在干嘛"；
  场景切换保证**信息量均匀**，但**时间戳不均匀**，而且**静止但对白密集的片段会被整段丢掉**
  （两个人坐着说话的镜头，"画面没变"，恰恰是群聊里最常见也最需要理解的内容）。
- 结论：**群聊场景用固定 fps**；场景切换只在"监控/游戏录屏/幻灯片式视频"这类里有优势。
  这也符合各家的默认：**没有任何一家厂商把场景检测当默认**（Gemini/豆包/Qwen 全是固定 fps）。

### B3. 帧数上限与降采样分辨率

- 帧数：豆包给了硬区间 **[16, 1280]**，且 `min_frames` 默认 16 —— 说明业界认为"**16 帧**"是"够看懂"的下限锚点【已核实】。
  Qwen 帧列表最少 **4 张**【已核实】。综合：**4 帧=认得出是什么；8 帧=看得出大概过程；16 帧=看得出先后顺序；32 帧以上只有动作分析才需要**【经验值】。
- 分辨率：三家的"经济档"都指向 **512px 长边**——
  OpenAI `detail:low` 就是把图缩到 512×512、固定 85 tokens【已核实】；
  Qwen 的 512×512 正好 `512×512/(32×32)+2 = 258 tokens`【按官方公式算】；
  豆包 `min_frame_tokens` 默认 384、下限 64【已核实】。
- **代价要写明**：512px 下**字幕、车牌、小字全部糊掉**。所以"画面里的文字"不能靠视觉模型读，
  要么单独截高清帧，要么走 OCR。这是抽帧方案最容易被忽略的失真点。

### B4. 音频轨单独走 ASR 再和画面合并

价值极大，而且有硬证据：

- **Qwen 明确不支持视频里的音频**【已核实】（A4），所以用 Qwen 时"原生视频"只等于"原生画面"，音频必须自己补。
- Gemini 的音频白送（32 tok/s，A1），但**只有 Gemini 一家**这样。
- 群聊视频的信息分布：口播、对话、BGM 歌词、旁白——**大量语义在语音里**，画面常常只是"一个人在说话"。
- 做法：`ffmpeg -vn` 抽音轨 → ASR → 把转写和帧描述一起喂给对话模型，形如
  `画面：…（0s/3s/6s…）；语音：“…”`。
- **本仓库现在做不到**：`AsrProvider` 只有一个 `UnconfiguredSpeechProvider`（`src/ling/ports/speech.py:71,95`），
  见 `docs/MODEL-ROLES-AUDIT.md` 第三节。这条应该明确写"依赖未就绪"。

### B5. 长视频的分段摘要（map-reduce）

- 触发条件很具体：**单次请求装不下**。Qwen `qwen3-vl-plus` 上下文 256K、豆包单视频硬顶 80k tokens、
  Gemini 1M 上下文 low 档能到 3 小时【均已核实】。所以 5 分钟视频在 Qwen 默认 2 fps 下
  就要 384k tokens —— **已经超上下文了**，不 map 也得 map。
- 常规做法（【经验值】）：
  1. 按固定时长切段（如 60 秒/段），段间留 1–2 秒重叠，避免动作被切断；
  2. 每段 8–16 帧 → 一段"这一段发生了什么"；
  3. 段摘要再 reduce 成整段摘要；
  4. **保留段的时间戳**，否则后面问"第 3 分钟那个"就答不上。
- 成本：N 段 × (16 帧 + 提示) 是线性的，但**每段的输出摘要很短**，比一次性塞满上下文更省。

---

## C. 成本量级（算给你看）

假设：720p 素材；美元按 1 USD ≈ **7.2 CNY**【假设，非实时汇率】；只算**输入** token（输出通常 50–200 tokens，可忽略）。

### C1. 原生视频（按各家官方公式）

| 场景 | 计算 | tokens | 单价 | 钱 |
|---|---|---|---|---|
| Gemini 1 分钟，默认 low | 60×(66+32) | **5,880** | Gemini 3.8 Flash $0.75/M | **$0.0044 ≈ ¥0.03** |
| Gemini 1 分钟，高分辨率 | 60×(258+32) | **17,400** | 同上 | $0.013 ≈ **¥0.09** |
| Gemini 5 分钟，默认 low | 300×(66+32) | **29,400** | 同上 | $0.022 ≈ **¥0.16** |
| Gemini 5 分钟，高分辨率 | 300×(258+32) | **87,000** | 同上 | $0.065 ≈ **¥0.47** |
| Gemini 5 分钟，高分辨率 | 同上 | 87,000 | 3.5 Flash-Lite $0.30/M | $0.026 ≈ **¥0.19** |
| Qwen 1 分钟，**默认 2 fps** | 120×640 | **76,800** | qwen3-vl-plus ¥1.5/M（32K–128K 档） | **¥0.12** |
| Qwen 1 分钟，1 fps | 60×640 | **38,400** | ¥1.5/M | **¥0.06** |
| Qwen 5 分钟，1 fps | 300×640 | **192,000** | ¥3/M（128K–256K 档） | **¥0.58** |
| Qwen 5 分钟，2 fps | 600×640 | 384,000 | **超出 256K 上下文，跑不了** | — |
| 豆包 1 分钟，默认 1 fps | 60 帧 × 384 | 23,040（未触 81920 上限） | 未取价目表 | 未算 |
| 豆包 任意短视频下限 | 16 帧 × 384 | **6,144**（地板价） | 未取价目表 | 未算 |

（Gemini 价格来自镜像的 pricing 页：`gemini-3.8-flash` 输入 $0.75/M，2026-12-31 前的价；`gemini-3.5-flash-lite` 输入 $0.30/M。**属第三方镜像，未在官方域名核实**。豆包单价我没抓到价目表，只有 token 量，**不编价格**。）

### C2. 抽帧后当图片送

| 发法 | 单帧 token | 8 帧 | 16 帧 |
|---|---|---|---|
| OpenAI `detail:low`（512²） | 85 | 680 | 1,360 |
| OpenAI `detail:high`（1280×720 → 6 块） | 170×6+85 = **1,105** | 8,840 | 17,680 |
| Gemini 图片（非 low 分辨率） | 258 | 2,064 | 4,128 |
| Qwen 512×512（`/1024+2`） | 258 | 2,064 | 4,128 |
| Qwen 720p 帧（`640×32×32` 上限截断） | 640 | 5,120 | 10,240 |
| 豆包（默认 384/帧） | 384 | 3,072 | 6,144 |

### C3. 结论

1. **1 分钟以内：抽帧和原生视频价格同一个量级**（Gemini 原生 low 5.9k vs 抽 16 帧 4.1k），
   **原生还多送你音频**。这种情况下**用原生更划算**——前提是这家是 Gemini。
2. **抽 16 帧高分辨率送给 OpenAI（17,680）比 Gemini 原生读完整 1 分钟（17,400）还贵**。
   所以"抽帧省钱"不是普适真理，**分辨率才是主要成本项**，不是"原生 vs 抽帧"。
3. **5 分钟以上，原生开始明显变贵且会撞上下文**（Qwen 默认 2 fps 直接跑不了）。
   这时候必须降 fps 或 map-reduce——**两家做法殊途同归**。
4. **该原生还是抽帧，取决于你说的"看懂"要求多细**：
   - 只想知道"这是啥视频、什么氛围" → **抽 8 帧（≈1–2k tokens）就够，任何一家都行**；
   - 要"第 3 秒谁做了什么"、要数次数、要判断动作对错 → **原生视频**（它自己有 1–2 fps 的时序采样）；
   - 信息在语音里 → **不管原生不原生，都得 ASR**，否则白花。

---

## D. 「她自己决定要不要看」的工程做法

先给一个行业依据：**Google 自己在往"按需加载"走**。Gemini 的 agentic video understanding 就是
"模型自己探索时间轴、只加载它需要的内容"，官方称对长视频**最多省 88% token**（A1）。
也就是说：**"不是每个视频都读"不只是省钱技巧，它是厂商自己在做的方向**。

### D1. 三种可落地形态

| 形态 | 怎么做 | 优点 | 代价 |
|---|---|---|---|
| **① 工具调用**（她调 `watch_video(ref)` 才真读） | 把"看视频"做成一个只读工具；上下文里只给"有个视频，ref=xxx，时长 12s"；她自己决定调不调；调用时下载→抽帧→视觉模型→把结果作为工具结果回灌 | 语义最干净：**决策和执行是同一次生成里的两步**，不需要猜她的意思；天然可审计（有 call 记录） | **需要真工具调用机制**：模型要能吐 tool_calls、要能把结果回灌、要能多轮循环。本仓库三样都没有（见 E2） |
| **② 延迟队列 + 决策点** | 收到视频只登记"未读"；她在正常回复时，上下文里带一行"群里有人发了个视频（12s，还没看）"；她若表达想看，就在**下一轮**之前异步读 | 不需要工具机制，**只加一层上下文 + 一个解析器**；和现有 `ActionIntent`、`ContextCompiler` 的车道模型吻合 | "她想看"要靠**她的输出里带一个可解析的标记**；解析脆、可能污染正文；**要等下一轮**才看得到（延迟换简单） |
| **③ 预算闸**（不是触发方式，是必须叠在①②上的约束） | 每窗口/每小时/每天限次数或限 token；同一条视频只读一次；超限就明说"今天看太多了" | 防止一个刷屏群把额度烧光；**可解释**（能告诉她"为什么这次没看"） | 自己做账；和现有 `TokenQuotaStore`（按供应商/天）不是同一维度，要新建 |

**取舍要点**：
- ① 是终局，② 是能在现有代码里**一周内**落地的过渡。**先 ② 后 ①**，因为 ② 产出的"未读池 + 读一次缓存"在 ① 里原样复用。
- ③ 无论选哪个都必须有。**没有闸的"按需读取"在群聊里等于没有上限**——她"想不想看"是模型判断，而模型对"这个视频可能有意思"的判断会系统性偏高。
- 另外一条来自本仓库自己的设计文档（`docs/ATTENTION-DESIGN.md:332-345` §8）：
  「不让普通群每条消息都调用模型判断"看不看"——那只是把逐条扫描换个名字」。
  同一条道理适用于视频：**别为了决定要不要看，先调用一次模型**。

### D2. 反例（要避免的形态）

- ❌ "每条视频都先调一个小模型判断值不值得看" → 等于每条都花一次钱，只是把钱花在更小的模型上，且引入一个新的故障点。
- ❌ "随机抽 10% 的视频看" → 她会在被问到"你看了吗"时给出不可解释的回答。
- ❌ "先全读、再摘要进上下文" → 就是现在的做法（E1），token 花在没人问的视频上。

---

## E. 给本仓库的建议

### E1. 现状（读代码得到的事实，不是猜测）

| # | 事实 | 位置 |
|---|---|---|
| 1 | 视频入站就登记成媒体证据（`kind="video"` 是合法 kind 之一） | `src/ling/domains/media/service.py:39` |
| 2 | **每 60 秒**自动扫"有待描述证据"的窗口，每窗口最多 5 条，逐条送视觉模型 | `src/ling/worker.py:2348`（`MEDIA_DESCRIBE_SECONDS = 60.0`）、`:951`、`:2546` |
| 3 | 也就是说：**现在没有任何"她想不想看"的环节**——所有视频都会被读，只是排队 | 同上 |
| 4 | **未看的视频在她眼里完全不存在**：上下文媒体车道只取 `described_in_window`（`description IS NOT NULL`） | `src/ling/kernel/context.py:411-448`，取数在 `:420` |
| 5 | 真视频（mp4）**现在其实读不了**：抽帧靠 Pillow，mp4 打不开 → 原样返回字节 → 以 `data:video/mp4;base64` 塞进一个 `image_url` 段。代码自己写明了这个局限 | `src/ling/domains/media/describe.py:84`、`:311-325`（局限注释在 `:315-320`）；序列化在 `src/ling/models/openai_compatible.py:197` |
| 6 | 动图（GIF/WebP）走的是另一条路：拆 **4 帧** PNG，`MAX_FRAME_BYTES = 1.5MB` | `describe.py:77,81,113` |
| 7 | `perception.video` 职责与 `video` 槽位已经存在（工作区**未提交**的改动），但只是"路由到另一个 profile"，**没有任何视频专用的传输形式** | `src/ling/ports/model.py:95,146,176,194`；`contracts/v1.py:189` |
| 8 | 端口层只有 `ImagePart` 和 `ChatMessageIn.images`，**没有 video part** | `src/ling/ports/model.py:211-238` |
| 9 | **没有工具调用机制**：`ToolRegistry` 全仓只有自己那个文件引用它，`ModelRequest` 没有 `tools` 字段，provider 层没有 `tool_calls` 解析 | `src/ling/capabilities/tools.py`（`grep -rn ToolRegistry src/` 只命中该文件）；`ports/model.py:241-258` |
| 10 | `ActionIntent` 有契约、有 `PolicyEngine.evaluate`，但**模型侧没有任何解析器**——目前只有内核自己造的 `reply.text` 和 manage 路由造的 `tool.*` | `contracts/v1.py:435`；`kernel/policy.py:89`；`kernel/turn.py:411` |
| 11 | 已有按"供应商/天"的 token 记账，可做总闸 | `src/ling/models/quota.py:63`（`TokenQuotaStore`） |
| 12 | ASR 未实现（只有一个 `UnconfiguredSpeechProvider`），所以**音频那条路现在走不通** | `src/ling/ports/speech.py:71,95` |

**一句话**：现在缺的不是"视频模型槽位"（第 7 条已经有了），缺的是
**(a) 让没看的视频在她上下文里可见**、**(b) 一个"她要求才读"的触发点**、**(c) 真正能拆 mp4 的手**。

### E2. 触发方式推荐：**先"延迟队列 + 决策点"，后"工具调用"**

理由（都基于 E1 的事实）：

- **工具调用现在不可用**：`ToolRegistry` 没接进任何地方（第 9 条）、`ModelRequest` 没有 `tools` 字段、
  provider 不解析 `tool_calls`。要做成"她调 `watch_video`"，等于同时改：端口契约 + 3 个 provider 适配器 +
  轮次循环（工具结果回灌）+ 策略引擎。这是**一个新子系统**，不是"加一个工具"。
- **`ActionIntent` 也不是现成的**：契约和策略有，**模型→intent 的解析器没有**（第 10 条）。
  所以"做成意图"和"做成工具"的工作量差别没有想象中大——**差别在稳健性**：
  意图是"从自然语言回复里抠一个标记"，工具是"结构化返回"。

推荐路径：

1. **现在（Phase 1）**：用**极简标记**实现 ②。
   上下文里给她一行带短 id 的未读清单（`m1: 12 秒视频，来自群「xx」`），
   并把"想看就说 `<<看 m1>>`"写进能力提示。kernel 在**回复完成后**用正则摘标记
   （`kernel/bridge.py:920` 的 `_MEDIA_MARKERS` 正则就是同类先例），命中就**异步**排队读，
   结果写回证据（`description`），下一轮自然出现在媒体车道里。
   **摘不到就当没看**——绝不因为"她可能想看"而读。
2. **以后（Phase 3）**：等 `tools` 进了端口契约，把同一个 `watch_video(ref)` 换成正经工具，
   标记解析器退役。**Phase 1 产出的"未读池 + 读一次缓存 + 预算闸"在 Phase 3 原样复用**，不会白做。

### E3. 建议的默认参数（附理由）

| 参数 | 建议值 | 理由 |
|---|---|---|
| 抽帧 fps | **0.5**，且 `帧数 = clamp(ceil(时长×0.5), 4, 16)` | 群聊视频多在 10–60 秒 → 5–30 帧，正好落在"看得出过程"区间；4 帧下限对齐 Qwen 帧列表最少 4 张【已核实】；16 帧上限对齐豆包 `min_frames` 默认 16 这个业界锚点【已核实】 |
| 帧数上限 | **16** | 512px 下 16 帧 ≈ 4.1k tokens（C2），单次读取可控；32 帧只有动作分析才需要 |
| 单帧分辨率 | **长边 512px，JPEG q=80** | 对齐 OpenAI `detail:low`(512²/85 tok) 与 Qwen 的 32×32 网格（512²=258 tok）【按官方公式】；再大收益低（Kimi 文档：超过 1080p"只会增加处理时间"） |
| 单次视频时长上限 | **120 秒**；超过只读前 120 秒，并在描述里注明"后面还有 N 秒没看" | 【经验值】。群聊/私聊视频绝大多数 <2 分钟；更长的应该**再要一次**（分段），而不是第一次就烧掉 |
| 单次输入 token 预算 | **≤ 6,000**（帧 + 提示），超了自动降帧（先降到 8，再降到 4） | 与 C2 实测对照：16×(258~384) 落在 4.1k–6.1k |
| 单条视频读取次数 | **1 次**（命中 `description` 就直接用） | 库里已有 `description` / `describe_failed_at`（`service.py:171,209`），幂等天然成立 |
| 未读池 TTL | **72 小时**后不再可读，只保留"当时有个视频" | 更久以后 QQ 的媒体链接多半已失效，读了也是失败重试 |

### E4. 必须的预算保护（缺一不可）

1. **每条视频只读一次**：读过的直接命中缓存，不重复下载、不重复调模型。
2. **每窗口每小时 ≤ 2 次、每天 ≤ 20 次**（【经验值】）。群聊刷屏时这是唯一挡住"她连看十个视频"的东西。
3. **单次硬上限**：≤16 帧、≤6k 输入 token、≤120 秒；超了降档而不是失败。
4. **总闸**：复用 `TokenQuotaStore`（`models/quota.py:63`）的日账，或按"视频"这一路单独记一列。
   **不要把视频读取记进对话模型的额度**——两件事混在一份额度过里，用户只会看到"对话突然不能用了"。
5. **可解释**：被闸挡住时必须留下"为什么这次没看"，否则她会说"我看了"（编造），
   而这是本仓库最不能接受的一类错误（`describe.py:14-17` 的第三条硬规则：拿不到就留空，绝不编造）。
6. **失败语义照抄现有约定**：模型侧错误**不标终态**（`describe.py:357-376` 已论证过为什么），
   引用侧错误（越界/超限/形式不支持）**标终态**（`describe.py:198-204`）。视频要遵守同一套，
   否则"每 60 秒重试一个坏 mp4"会重演。

### E5. 分阶段落地（每步可独立验收）

#### Phase 0：让它"可见但不花钱"（半天，改动最小）

- `kernel/context.py` 媒体车道**增加一段"未看的视频"**：从 `service.pending()`（`service.py:289`）
  取 `kind="video"` 的行，只渲染"有人发了个视频（约 N 秒，还没看）"，
  **不带 description、不触模型**。同时保留原有的"已描述"渲染。
- `worker.py` 的 `media.describe`：**跳过 `kind="video"`**（图片/动图照旧）。
- **验收标准**：
  - 发一个 mp4 → 60 秒内**零模型调用**（provider usage 增量为 0，日志无 `media.describe` 成功记录）；
  - 她的上下文里出现那一行未读提示（可用现有 context 快照测试断言）；
  - 发一张 GIF → 行为**与现在完全一致**（回归不破）。

#### Phase 1：她自己决定要不要看（2–3 天）

- 上下文：未读清单带短 id（`m1`），能力提示里给一句格式范例。
- 新代码：`MediaEvidenceService` 加"按短 id 取一条未读证据"；kernel 在回复后解析标记
  （正则，`bridge.py:920` 同款）；命中 → 入队 `media.describe`（payload 指定 evidence_id），
  **不走 60 秒全量扫描**。
- 预算：E4 的 1/2/3/5 条。
- **验收标准**：
  - 构造用例 A：她说想看一眼 → 只有**那一条**被读，别的未读视频仍是未读；
  - 用例 B：她没提 → 24 小时内该视频**零调用**；
  - 用例 C：同一条视频第二次要 → **直接命中缓存**，无第二次下载/调用；
  - 用例 D：超预算 → 有明确拒绝日志 + 上下文里她说"今天不看了"这类可解释表述（不是静默）；
  - 用例 E：标记写错/模型不遵守格式 → **静默退化为"没看"**，不报错、不误读别的视频。

#### Phase 2：真正会拆 mp4（3–5 天，含依赖与降级）

- 加 `ffmpeg` 依赖（Docker 镜像 + 本地开发都要有），新模块 `extract_frames()`：
  `ffmpeg -ss/-vf fps / -vf scale='min(512,iw)':-2`，**去掉 `expand_animation` 对 mp4 的伪处理**。
- 端口层：视频**不再伪装成 `ImagePart`**——要么明确"视频=多张图 + 一句说明"（这是抽帧方案的诚实表达），
  要么加 `VideoPart`。**建议前者**：抽帧就是抽帧，别在契约里假装有视频能力。
- **验收标准**：
  - mp4 不再以 `data:video/mp4` 出现在 `image_url` 里（加一条断言）；
  - 单测覆盖：0 帧（坏文件）、<16 帧的极短视频、>120 秒（截断 + 注明）、无 ffmpeg（**降级为"看不了"而不是崩**）；
  - 一条 60 秒 720p 视频的输入 token 落在 6k 预算内（打印实测值）。

#### Phase 3：原生视频 + 真工具（以后再说，先别做）

- 端口加 `VideoPart`（或明确"provider 原生视频"能力标记），至少接 Gemini 一家；
- `ModelRequest` 加 `tools`，`watch_video` 换成正经工具调用，退役标记解析器；
- **验收标准**：同一段 fixture 在"抽帧"与"原生"两条路上跑同一 prompt，
  产出 `tokens / 描述准确度 / 延迟` 三列对照表，**用数据决定默认走哪条**。

#### 明确"以后再说"的

- **长视频分段 map-reduce**：先把 Phase 0–2 的 <2 分钟场景做对，5 分钟以上等真有需求。
- **音频 ASR 合并**：卡在 `AsrProvider` 没实现（E1 第 12 条），不是设计问题。
- **场景切换抽帧**：B2 已论证对群聊内容反而更差，不做。
- **OpenAI/Claude 的原生视频**：官方根本没有（A2/A3），不是"以后接"，是"不存在"。

---

## 附：核实方法与不可达清单

- 官方直读（本次真的打开了）：`help.aliyun.com/zh/model-studio/vision`、
  `help.aliyun.com/zh/model-studio/billing-for-model-studio`、
  `volcengine.com/docs/82379/1895586`、`docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash`、
  `docs.bigmodel.cn/openapi/openapi.json`、`platform.kimi.com/docs/api/chat`、
  `platform.kimi.com/docs/guide/use-kimi-vision-model`、`api-docs.deepseek.com/guides/vision`。
- 官方仓库直读：`google-gemini/cookbook`、`openai/openai-cookbook`、`openai/openai-python`、
  `openai/openai-openapi`、`anthropics/anthropic-sdk-python`、`ffmpeg/ffmpeg` 的滤镜文档页
  （`https://ffmpeg.org/ffmpeg-filters.html`，首次抓取超时、重抓 1.75 MB 成功）。
- 打不开：`ai.google.dev`（超时）、`platform.openai.com`（403）、`platform.claude.com`（地区限制）、
  `platform.minimaxi.com`（文档是 SPA 壳）。
- 因此有三处必须按"未证实"读：**Gemini 的每帧 token / 时长上限 / 价格**（第三方镜像）、
  **MiniMax 的全部**、**各家的"未公开抽帧率"**（豆包/智谱/Kimi 的 token 公式）。
