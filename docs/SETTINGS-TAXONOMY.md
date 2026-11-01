# 功能设置分类法（现状）

> **这份文档描述现状，不是许愿池。**
> 每条结论后面都给 `文件:行号`。文档与代码不一致时以代码为准，并修文档。
>
> 本次 IA（Step 4「设置瘦身与重组」）之后，**前端设置中心只剩 3 组**；
> 后端 9 类 taxonomy **没有改**。这两件事的关系见 §1，那是全文最容易读错的地方。

---

## 0. 权威在哪里（以及「两类权威」）

有两套不同的东西，历史上一直被混为一谈，这次分开：

| 权威 | 位置 | 回答什么问题 | 本次 IA 是否改动 |
|---|---|---|---|
| **后端分类法** | `src/ling/settings/taxonomy.py` 的 `CATEGORIES` | 「这个设置项属于哪一类决策」「它从哪个 API 编辑」 | **未改**（仍是 9 类） |
| **前端设置中心** | `clients/web-console/src/components/SettingsHub.tsx:57` 的 `SETTINGS_GROUPS` | 「哪些页面出现在 `/settings` 页面上」 | **改了**（6 类 → 3 组） |

**关键区分：后端分类 ≠ 前端设置入口。**

- `editable_route`（`taxonomy.py:60-71`）表达的是**「这一类从哪进去编辑」**——
  它回答数据/接口归属，**不是**「这个东西应该出现在 `/settings` 页面上」。
- 前端 `/settings` 只渲染**程序与运维**（判据：有领域对象、有列表-详情生命周期的算内容页，
  纯实例配置才算设置 —— `SettingsHub.tsx:2-19` 的注释）。
- 于是 ④记忆与学习、⑤对话与主动性、⑧生活与活动 的 `editable_route` **仍然是**
  `/v1/settings/config` 的对应段落（它们仍然是设置项），但它们的**编辑入口挂在各自的领域页**，
  不在设置中心。**没有删除任何设置项，只是换了住处。**

### 0.1 当前事实（一句话版）

- 后端：**9 类**，其中 `editable_route` 非 None 的 **6 类**。
  被测试锁死：`tests/unit/test_settings_taxonomy.py:164` `test_editable_count_is_exactly_six_today`
  （断言 `labels == ["对话与主动性","权限与隐私","模型与生成","生活与活动","记忆与学习","身份与人格"]`）。
- 前端：`/settings` 下 **3 组 / 5 张入口卡 + 1 个中心页**（`SettingsHub.tsx:57-116`）。
- 迁出 3 类：旧地址有**精确重定向**（`route.ts:53-57`），新地址是 tab 深链。

> **历史矛盾已删除。** 旧版本 §0 写「前端 `/settings` 只渲染这 6 类」，
> 旧 §7 又写「3 类尚无设置面（②⑤⑧）」——同一份文档同时声称 ⑤⑧ 有/没有设置面。
> 现在以代码为准，§7 已重写。

---

## 1. 前端设置中心的真实结构（3 组）

来源：`clients/web-console/src/components/SettingsHub.tsx:57-116`

| 组 `key` | 组名 | 入口 | path | 为什么这个在设置里 |
|---|---|---|---|---|
| `security`<br>(`:59`) | **安全与隐私** | ① 权限与隐私 | `/settings/privacy`<br>(`:68`) | 主体权利：同意可撤回、有有效期、有可见范围。这是**准许裁决**，不是行为偏好，且必须能随时撤销。 |
| | | ② 多用户与账号 | `/settings/identity`<br>(`:75`) | 身份归属：合并只写 `subjects.merged_into` 指针、可拆开。是账号层运维，不是「她是谁」的人设内容。 |
| | | ③ 登录名与密码 | `/settings/account`<br>(`:82`) | 你自己的登录凭据，改完其它设备失效。纯账号运维，与她的行为无关。 |
| `model`<br>(`:88`) | **模型与密钥** | ④ 模型与生成 | `/settings/model`<br>(`:97`) | 实例能力：provider、密钥、职责绑定、草稿→发布→回滚。「谁来算」是程序配置，不属于任何领域页。 |
| `maintenance`<br>(`:103`) | **维护与关于** | ⑤ 关于与版本 | `/settings/about`<br>(`:111`) | 运维动作：看版本、手动查 GitHub Release、按门禁安装。是「服务怎么跑」，不是她怎么表现。 |

**路由侧对应**：`/settings` 下仍归属设置的共 6 条
（`route.ts:70-76` 与 `nav.test.ts:239-246` 的 `settingsPaths` 逐一对应）：

```
/settings          → SettingsHub（中心页）
/settings/model    → SettingsPage（模型与生成）
/settings/privacy  → ConsentPage（权限与隐私）
/settings/identity → IdentityPage（多用户与账号）
/settings/account  → AccountPage（登录名与密码）
/settings/about    → AboutPage（关于与版本）
```

`/settings/about` 仍是版本号 badge 的落点（`Navigation.tsx:70`），
且 `tests/e2e/update_browser.mjs:545` 仍以它为目标——这条地址**不许消失**。

### 1.1 迁出的 3 类：只指路，不是入口

`SettingsHub.tsx:119-123`：

```ts
const MOVED_HINT = [
  { label: '记忆与学习',   path: '/memory?tab=settings' },
  { label: '对话与主动性', path: '/persona?tab=dialogue' },
  { label: '生活节奏',     path: '/life?tab=rhythm' },
];
```

渲染在 `SettingsHub.tsx:211-223`：底部一行灰字 + `link-button`，
**不挂在 `settings-groups` 的卡片网格里**。做成卡片就等于没迁（用户原话是「不要再放到设置里」）。

### 1.2 taxonomy 挂了不挡路

`SettingsHub.tsx:129-149`：`api.taxonomy()` 失败只 `setError` 显示一条 banner（`:163-177`），
入口卡片用**本文件的文案**；只有 `privacy` / `model` 两张卡在成功时用后端 `label`/`verb` 覆盖
（`decorate`，`:144-149`）。旧实现是 taxonomy 一失败，设置中心一个入口都不剩。

---

## 2. 旧 → 新映射与重定向

| # | 旧地址（曾是真实入口） | 新地址（现状） | 新住处的入口 |
|---|---|---|---|
| ④ 记忆与学习 | `/settings/memory` | `/memory?tab=settings` | 记忆页「记忆与学习」tab |
| ⑤ 对话与主动性 | `/settings/dialogue` | `/persona?tab=dialogue` | 人设页「对话与主动性」tab |
| ⑧ 生活与活动（节奏） | `/settings/life` | `/life?tab=rhythm` | 生活页「生活节奏」tab |

### 2.1 重定向表是**精确表**（`route.ts:53-57`）

```ts
export const LEGACY_REDIRECTS: Readonly<Record<string, string>> = {
  '/settings/memory': '/memory?tab=settings',
  '/settings/dialogue': '/persona?tab=dialogue',
  '/settings/life': '/life?tab=rhythm',
};
```

- 精确键查找：`route.ts:63-64` 用 `LEGACY_REDIRECTS[path]`，**不是** `startsWith`、不是正则。
- 表外兜底是 `notFound`：`route.ts:85`。
- 三条旧地址**已从路由表删除**（`route.ts:66-84` 没有它们的 case），否则「迁出」只是加了个跳板。
- **不许改成 `/settings/*` 通配**：通配会把拼错的 `/settings/memroy` 也悄悄导到能用的页面，
  `notFound` 要暴露的东西就再也暴露不出来。锁在 `dashboard.test.ts:281-294`
  与 `nav.test.ts:310-322`（后者是「表外必须 notFound」的逐条版）。

### 2.2 跳转用 `replace`，不是 `push`

改地址栏不在 `route.ts` 里做（它保持纯函数，单测可直接断言），在壳层 effect：

`App.tsx:282-284`

```ts
const legacyTo = route.name === 'legacyRedirect' ? route.to : null;
useEffect(() => {
  if (legacyTo) navigate(legacyTo, { replace: true });
}, [legacyTo]);
```

`navigate` 的 `replace` 分支走 `history.replaceState`（`route.ts:167-173`）。
理由：旧书签不该在后退栈里留一层，否则点一次后退又被重定向回来。

首帧不闪 404：`App.tsx:95-100` 的 `legacyRedirect` 分支直接渲染**目标页内容**。

### 2.3 tab 深链（新地址必须可分享、可刷新、可后退）

`clients/web-console/src/tabs.ts`：

| 函数 | 行 | 行为 |
|---|---|---|
| `personaTabFromHref` | `tabs.ts:35-40` | `?tab=dialogue` → `dialogue`；旧路径 `/settings/dialogue` 也认；**未知值 → `persona`，不抛异常** |
| `personaTabHref` | `tabs.ts:42-44` | `dialogue` → `/persona?tab=dialogue`，否则 `/persona` |
| `lifeTabFromHref` | `tabs.ts:52-57` | `?tab=rhythm` → `rhythm`；旧路径 `/settings/life` 也认；**未知值 → `now`，不抛异常** |
| `lifeTabHref` | `tabs.ts:59-61` | `rhythm` → `/life?tab=rhythm`，否则 `/life` |

页面接入：`PersonaPage.tsx:40-41`、`LifeNowPage.tsx:67-68`。
记忆页的同类函数是 `MemoryPage.tsx:561` `memoryTabFromHref`（`?tab=settings` ↔ `/memory?tab=settings`）。

**为什么 `*TabFromHref` 同时认旧路径**：重定向发生在壳层 effect 里，首帧地址还是旧的；
只认 `?tab=` 会先开错 tab 再跳一次。契约测试见 `tabs.test.ts`（12 用例，含往返一致与「不抛异常」）。

---

## 3. 后端分类树（9 类，未改）

```
功能设置
│
├─① 身份与人格 ────────────── 改「它是谁」
│   ├─ 运行身份 ····· persona_id / actor_id
│   └─ 稳定人格 ····· 名字·自称·system_prompt·traits·boundaries
├─② 语言与声音 ────────────── 改「它说成什么样」
│   ├─ 说话方式 ····· 正式度·分句长度·口语化·颜文字
│   └─ 声音呈现 ····· 语速·音色·何时发语音而非文字
├─③ 模型与生成 ────────────── 改「谁来算」
│   ├─ 职责绑定 ····· ModelRole 全集（见 §6）
│   ├─ 供应商与凭证 · provider(fake/openai_compatible/local) + 密钥
│   └─ 推理与回退 ··· temperature·max_tokens·fallback 顺序
├─④ 记忆与学习 ────────────── 改「它记住/学会什么」
│   ├─ 记忆形成 ····· 抽取阈值·保留期·压缩级联
│   ├─ 召回 ········· top_k·融合权重·重排开关
│   └─ 学习 ········· 黑话采纳阈值·口癖习得·风格漂移上限
├─⑤ 对话与主动性 ──────────── 改「要不要开口」
│   ├─ 对话节奏 ····· 回复延迟·打断策略·分段阈值
│   └─ 主动联系 ····· 每日上限·免打扰时段·各原因开关
├─⑥ 渠道与连接 ────────────── 改「从哪条线走」
│   ├─ 接入配置 ····· OneBot api_base·self_id·token / 微信开关
│   └─ 渠道能力 ····· supports_group·supports_proactive（只读）
├─⑦ 权限与隐私 ────────────── 改「准不准」
│   ├─ 能力授权 ····· camera·location·publish·跨设备读取
│   └─ 信息边界 ····· 隔离模式·记忆保留期·跨窗口可见范围
├─⑧ 生活与活动 ────────────── 改「它自己在干嘛」
│   ├─ 作息与场景 ··· 睡眠时段·专注时长·场景 TTL·精力衰减
│   └─ 活动与目标 ··· 活动配比·目标回顾间隔·日程来源
└─⑨ 系统与部署 ────────────── 改「服务怎么跑」
    ├─ 启动与数据 ··· host·port·database_url·data_dir
    └─ 运行管理 ····· test_mode·告警 webhook·备份·引导管理员
```

**9 个动词**：是谁 → 怎么说 → 谁算 → 记什么 → 开不开口 → 走哪条线 → 准不准 → 在干嘛 → 怎么跑。

分类轴只有一条：**这个设置直接改变哪一个系统决策。**
不是按代码模块分，不是按存储位置分，不是按生效阶段分。

### 3.1 每类的 `editable_route` 与「前端入口在哪」

| # | 类 | `editable_route` | taxonomy.py 行 | 前端编辑入口（现状） |
|---|---|---|---|---|
| ① | 身份与人格 | `/v1/persona` | `:85` | `/persona`（人设页，**不在设置**） |
| ② | 语言与声音 | `None` | `:90` | 无（见 §6） |
| ③ | 模型与生成 | `/v1/settings/config` | `:110` | **`/settings/model`** |
| ④ | 记忆与学习 | `/v1/settings/config` | `:127` | `/memory?tab=settings`（**不在设置**） |
| ⑤ | 对话与主动性 | `/v1/settings/config` | `:142` | `/persona?tab=dialogue`（**不在设置**） |
| ⑥ | 渠道与连接 | `None` | `:148` | 无 |
| ⑦ | 权限与隐私 | `/v1/consents` | `:168` | **`/settings/privacy`** |
| ⑧ | 生活与活动 | `/v1/settings/config` | `:184` | `/life?tab=rhythm`（**不在设置**） |
| ⑨ | 系统与部署 | `None` | `:189` | 无（部署/env） |

加粗的只有 2 类（③⑦）真正出现在设置中心；①④⑤⑧ 的编辑面在各自领域页；
②⑥⑨ 还没有前端设置面。

> `editable_route` 为 `None` 表示这一类**还没有设置面**，前端不得为它渲染入口
> （`taxonomy.py:11-17`、`:60-71`）。这个字段是诚实性锚：
> `tests/unit/test_settings_taxonomy.py:112` 验证每个非 None 的路由真的存在于 FastAPI 路由表。

### 3.2 为什么 ③④⑤⑧ 共用 `/v1/settings/config`

那是同一份修订的不同段落，不是四份权威——它们本来就该在同一份配置里原子发布、一起回滚。
`editable_route` 表达的是「从哪进去」，不是「存在哪张表」。

④⑤⑧ 的表单共用 `PolicyForm` 组件：结构一样，复制三份会让上一轮踩过的坑
（输入框 `min=1 step=5` 让默认值 120 非法，浏览器静默拦截提交）在其中一份里重演。
⑤ 的免打扰用 `kind: 'minutes'`——存的是「当天第几分钟」，但界面上是时间选择器。

---

## 4. 每类的硬边界

边界必须是**可执行的判别问题**，不是形容词。

| # | 一级类 | 它控制的决策 | 硬边界问题 |
|---|---|---|---|
| ① | 身份与人格 | 由哪个角色行动，其跨情境的稳定行为倾向 | 固定模型、表达格式、当前情境后，改它是否仍改变角色身份或稳定偏好？ |
| ② | 语言与声音 | 一次交互如何被表述 / 呈现为文字或语音 | 已决定要交互、引擎与渠道已定后，改它是否改变措辞或声音呈现？ |
| ③ | 模型与生成 | 哪个模型执行职责、推理预算、解码与回退 | 期望表达不变时，改它是否改变 provider/model/凭证引用或推理控制？ |
| ④ | 记忆与学习 | 哪些经历被提取、保存、召回或转化为习得内容 | 允许处理的数据范围不变时，改它是否改变记忆形成/召回/学习决策？ |
| ⑤ | 对话与主动性 | 是否、何时、以何频率开始或接续交流 | 生活状态与表达形式不变时，改它是否改变发起/应答交流的决策？ |
| ⑥ | 渠道与连接 | 经哪个适配器、账号、端点、协议能力传递 | 交流意图与内容不变时，改它是否只改变连接或渠道可承载能力？ |
| ⑦ | 权限与隐私 | 某项能力或数据流在当前范围内是否被允许 | 功能策略不变时，改它是否改变调用能力/读写数据/可见范围的准许结果？ |
| ⑧ | 生活与活动 | 角色何时处于什么场景、做什么活动、推进什么目标 | **完全不发送消息时**，改它是否仍改变日程/presence/activity/goals 的状态转移？ |
| ⑨ | 系统与部署 | 服务如何启动、连接数据源、运行管理 | 换掉角色及其交流策略后，改它是否仍决定监听/DB连接/test_mode/告警？ |

### 几个容易混的实例

| 设置 | 归 | 理由 |
|---|---|---|
| 主动联系的频率 | ⑤ | 行为偏好 |
| 是否允许用摄像头 | ⑦ | 准许裁决，独立于偏好 |
| 记忆最大保留期限 | ⑦（**实现寄存在 ④ 的 memory 段**，见 §5.1） | 信息边界（数据能留多久） |
| 为提升召回调整的记忆衰减 | ④ | 检索质量，不是保留边界 |
| `speech.tts` 用哪个 provider | ③ | 决定*谁执行*语音职责 |
| 语速 / 音色 / 何时发语音 | ② | 决定*这次语音交互怎么进行* |
| 日程何时结束 | ⑧ | 生活事件 |
| 日程结束后要不要联系用户 | ⑤ | 把状态转成交流的决策 |
| 学到的口癖、群聊黑话 | ④ | 习得内容，不是配置 |
| 明确设定的长期偏好 | ① | 人格配置 |
| 当前情绪 / 当前目标 | —— | **业务事实，不是设置** |
| `supports_proactive` | ⑥ 只读 | 平台能力探测结果，不是用户策略 |

---

## 5. 归属判定序列

**先原子化，再按序问，第一个「是」即答案。**

```
0. 原子化
   ├─ 复合开关拆成原子项
   └─ 排除业务事实（当前情绪/当前目标/学到的口癖/一次授权记录）
   ↓
1. 直接改变"准不准"？                  → ⑦ 权限与隐私
2. 期望表达不变，改变执行引擎？          → ③ 模型与生成
3. 交流意图不变，改变通路？              → ⑥ 渠道与连接
4. 允许处理范围不变，改变记忆/学习？      → ④ 记忆与学习
5. 完全不对外说话，仍改变生活状态？      → ⑧ 生活与活动
6. 生活状态不变，改变"何时开口"？        → ⑤ 对话与主动性
7. 引擎已定，改变措辞或声音呈现？        → ② 语言与声音
8. 脱离具体交互，改变身份/稳定倾向？     → ① 身份与人格
9. 改变启动/连接/告警？                → ⑨ 系统与部署
   ↓
都不是 → 不要硬塞进⑨。先查它是不是根本不是设置，或分类法缺了一种决策
```

> 这个序列判的是**归属**，不是**入口**。判出「这是 ⑤ 对话与主动性」之后，
> 它的前端入口仍可能在 `/persona?tab=dialogue`——归属与入口是两件事（见 §0）。

### 顺序本身是防线：三个最容易误判的位置

| 位置 | 错法 | 为什么必须保持这个顺序 |
|---|---|---|
| **⑦ 在 ④ 之前** | 「允许跨窗口读记忆」被放进记忆类 | 它是数据边界，不是记忆策略。读的对象是记忆不代表归类是记忆 |
| **③ 在 ② 之前** | `speech.tts` 因名字带 `speech` 被放进声音类 | 名字不是证据，决策点才是 |
| **⑧ 在 ⑤ 之前** | 「日程结束」被主动性吞掉 | 若先按触发结果分类，一切生活设置都会被主动性吸走 |

**⑨ 放最后**有实际理由：防止「今天存在 env 里」变成归类理由。
`LING_HOST` 是部署类，但一个值来自 env **并不使它**成为部署类。

---

## 5.1 存储与生命周期

功能分类与存储位置**分开判定**。对每个原子值按序处理：

| 序 | 判定问题 | 存放位置 |
|---|---|---|
| 1 | 是运行中产生/改变的事实、事件、学习结果、一次具体授权记录？ | **业务表** |
| 2 | 必须在配置服务可用前提供，且不覆盖已发布配置？ | **环境变量** |
| 3 | 是 provider/渠道运行时使用的秘密值？ | **加密凭证库** |
| 4 | 是你选择的、需要草稿→校验→发布→回滚的持续策略？ | **DB 配置修订** |

配置修订的不变式（`src/ling/settings/service.py`）：同一时刻只有一个 `published`；
已发布不可变，改动一律新建草稿；密钥只存密文 + 后四位 + 指纹，快照里只有
`has_secret` / `secret_last4`。

### 为什么权限授权不能只存环境变量

环境变量只负责启动必需项，且**不能覆盖已发布配置**。它无法表达：
可发布/可回滚的运行策略、`per_window` 这类范围化授权、带 `valid_until` 有效期且可撤销的授权记录。

一次界面动作可能涉及不同记录，但**不应压成一个键**。例：「允许摄像头」可能同时涉及
宿主是否提供该能力 / 已发布的授权策略 / 某次交互中的具体授权记录——三者生命周期不同。

### 5.2 ④ 记忆与学习 的实际实现与生效口径

④ 的 memory 段（`GET/PUT /v1/settings/config` 的 `memory`）有 4 个参数。
**2026-11 之前它们能存、能发布、能回显，但运行时根本不读**——`MemoryService.__init__`
支持 `policy=`，所有生产构造点却都是 `MemoryService(session)`，于是永远用代码默认值。
现在改成下面这样，改任何一条都要同时改本节。

| 参数 | 真正作用于 | 生效时机 | 没配置时的默认 |
|---|---|---|---|
| `relative_score_floor` | `MemoryService.recall` 的最终相关性下限：低于 `第一名分数 × floor` 的命中被丢掉 | 下一轮对话 | 0.35 |
| `recency_half_life_days` | 召回排序的「新近度」一路（RRF 的一个排名信号） | 下一轮对话 | 120 |
| `rerank_top_n` | **只在配置了重排模型时**决定前 N 条送去精排 | 下一轮对话 | 20 |
| `max_retention_days` | 保留期硬上限：召回过滤 + `memory.archive` 归档 + 写入时盖章 | 立刻对「上限下调」生效；上调只影响之后写入的行 | 3650 |

**生效路径（谁把策略交给谁）**

| 进程/入口 | 构造点 | 策略来源 | 时机 |
|---|---|---|---|
| api / web 轮次 | `kernel/turn.py` → `ContextCompiler(..., memory_policy=config.memory)` | 本轮 `EffectiveConfig` | **下一轮**生效；进行中的轮次用旧快照 |
| worker `memory.extract` | `worker.py` → `MemoryService(policy=await published_memory_policy(session))` | 已发布修订 | 下一个 outbox 事件 |
| worker `memory.archive` | 同上 | 已发布修订 | 下一次维护事件 |
| 手工写入 / 纠正 | `api/routes/manage.py` 的 `POST /v1/memory`、`.../supersede` | 请求时的 `ConfigService.effective()` | 立刻 |
| 列表/看板读取 | `GET /v1/memory` 等 | 不消费策略 | —— |

**多进程可见性**：设置没有进程内缓存。每个进程在自己那一轮/那个事件里
`SELECT ... WHERE state='published' ORDER BY revision DESC LIMIT 1`，
所以 "api×2 + worker" 三个进程都在**下一次查库**时看到新修订，不需要重启、也不需要缓存失效广播。
已开始的轮次不会中途换策略。

**`max_retention_days` 与行级 `retention_days` 的裁决**

1. **策略是硬上限**：实际保留期 = `min(行内 retention_days, max_retention_days)`。
   调小上限**立即**对所有存量行生效（隐私边界要能立刻收紧）。
2. **行内值是写入时的快照 + 可以更短的请求**：行内只能更短，不能更长。
3. **写入时按当时的策略盖章**：`propose()` 写 `retention_days = max_retention_days`。
   不盖的话列默认 365 会把策略默认 3650 永远压住——这正是「设置里填 3650 永远不生效」的另一半原因。
4. **上调上限不改写存量行**：已写入的行保留期是审计事实，不回头改。
5. **情节（Episode）没有行级保留期**：它的**召回**受同一上限约束（按 `occurred_at` 起算），
   **归档**仍由 `memory.archive` payload 的 `episode_days`（默认 180 天）决定。两个旋钮管两件事。

**`rerank_top_n` 在没配重排模型时不生效（已明标）**

重排提供方走环境变量 `LING_RERANK_*`（`domains/memory/providers.py`），
**不经过设置页的模型绑定**。没配时：检索只有 BM25 与向量两路融合排序，`rerank_top_n` 是纯空转；
`MemoryService` 构造时每个进程打一条 INFO；记忆页「记忆与学习」明确写「这一项在未配置重排模型时不生效」；
`GET /v1/settings/config` 的 `memory` 段**形状不变**（值照常回显），假的是「存下来就等于生效」——
后者由 UI/文档澄清。要让它真生效：配 `LING_RERANK_API_KEY` 后重启进程。
`memory.rerank` 这个模型职责仍是保留角色（见 `RESERVED_PROFILES`），绑它不会改变这里的行为。

---

## 6. 缺陷记录

### 6.1 公开发布授权两套并行机制

**原状**：`LING_ALLOW_PUBLIC_PUBLISH` 在 `capabilities/publishing.py` 里被当作最终授权闸门；
同时 `ConsentRule(scope="public_publish")` 被 `domains/consent/service.py` 真实读写。
两者互不知情——env=1 绕过一切授权记录，consent 授权了又因 env=0 发不出去。

**修法**：把两层拆开，各自只答自己的问题。

| 层 | 存哪 | 答什么 | 实现 |
|---|---|---|---|
| 宿主能力 | 启动配置 `LING_ALLOW_PUBLIC_PUBLISH` | 这台部署是否提供发布能力 | `PublishingService(capability_enabled=...)` |
| 主体授权 | 业务表 `ConsentRule(scope="public_publish")` | 这个 subject 是否被允许发布 | `create_draft(..., consent=ConsentService)` 内 `is_active` |

- `create_draft` 由 sync 改 async，**必须**传 consent；传 None 失败关闭
- 错误文案改为指路 `scope=public_publish`，不再误导人去改环境变量
- `GET /v1/publishing` 改报三态：`capability_enabled` / `consent_granted` / `can_publish`，
  并**移除**旧的单布尔 `enabled`

**回归**：4 项单元 + 2 项集成。已实测把 consent 检查去掉后两条测试变红。

### 6.2 `LING_ONEBOT_PROACTIVE` 是探测结果而非用户设置

**原状**：`capabilities/channels.py` 里有两处把它读成 `supports_proactive`：
`onebot_config()` 的 `OneBotConfig(...)` 调用里读 `LING_ONEBOT_PROACTIVE`，
`qqbot_config()` 的 `supports_proactive=bool(get("qqbot_proactive"))`，
但这个事实没有暴露在任何接口上，设置面看起来像一个可以随便开关的偏好。

**修法**：`GET /v1/channels` 新增 `onebot.capabilities_readonly`
（`supports_group` / `supports_proactive`），并注明「只读展示、不可在设置里开关；
要改请调环境变量后重启」。设置界面不得把它做成开关。

---

## 7. ModelRole 全集（③ 职责绑定的实际内容）

来源 `src/ling/contracts/v1.py`，共 13 个：

```
dialogue.fast        dialogue.deep        dialogue.group
memory.extract       memory.embed         memory.rerank
perception.vision    speech.asr           speech.tts
realtime.voice       moderation.advisory
reflection           planning
```

**注意**：`reflection` / `planning` 暂无独立配置入口，是否有真实调用点待验证。

---

## 8. 未验证 / 待补（现状）

| 项 | 状态 |
|---|---|
| ②语言与声音、⑥渠道与连接、⑨系统与部署 | `editable_route` 为 `None`，**没有前端设置面**，刻意留空（不建空壳）。② 的语音呈现整组是设计不是现状 |
| ①④⑤⑧ 的编辑面 | **有，但不在 `/settings`**：①在 `/persona`、④在 `/memory?tab=settings`、⑤在 `/persona?tab=dialogue`、⑧在 `/life?tab=rhythm`（见 §3.1） |
| 9 类是否最优、两级导航是否有依据 | **未验证**。现有事实只证明「13 个绑定一页放得下」和「748 扁平键会烂」，两者都不能推出 9 或 2。验证方法见下 |
| 出站 TTS | 完全未实现。`domains/speech` 只有入站。② 声音呈现整组是设计，不是现状 |
| 隔离模式能否运行中切换 | 未验证。`awareness/service.py` 有连续↔按窗口迁移的约束注释 |
| `reflection` / `planning` | 是否有真实调用点待验证 |
| `moderation` 未来策略 | 若出现独立内容裁决设置，需确认它改的是准许裁决/对话决策/表达决策，**不能因同属一个代码域就预先归类** |

> 与旧版的差别：旧版这里写「3 类尚无设置面」并把 ⑤⑧ 也列进去，与 §0 自相矛盾。
> 现在如实写：⑤⑧ **有设置面**，只是入口在领域页。

### 9 类是否可用的验证方法

拿一份接近未来规模的设置清单（数百项），做「我要改 X」的定位任务，记录：首次选中的类别、
最终是否找到、回退次数、用时。与「合并争议类别」的版本对比。
若 9 类持续使定位更慢或首次归属更不一致，就合并或改名。
单用户产品以该用户的任务表现为准。**在拿到这组数据之前，本文档的 9 类是一个待验证方案，不是已验证结论。**

---

## 9. 语言与声音的界面约束

语音设置概念上分处两类（③ 引擎绑定 / ② 呈现参数），这是正确的，但**不能要求用户记住两个位置**。

约束：

- ②「语言与声音」内显示当前 TTS / ASR 绑定，并提供跳转到 ③ 的编辑入口
- ③「模型与生成」内反向链接回 ②
- 全局搜索「语音」必须同时命中两类结果
- **只有一个编辑归属，不复制两份权威值**

> 现状提醒：② 目前**没有前端页面**（`editable_route=None`），所以这条约束现在只能部分成立——
> ③ 页面（`/settings/model`）是唯一的语音绑定编辑处。等 ② 有页面时再补另一侧。

---

## 10. 新增设置项时的检查清单

1. 先用 §5 的判定序列确定**归属**（9 类里哪一类）。
2. 再问**入口该在哪**：是纯实例配置/运维（→ `/settings`），还是领域行为调参（→ 该领域页的 tab）？
3. 若放 `/settings`：必须在 `SettingsHub.tsx:57` 的 `SETTINGS_GROUPS` 里加卡，并写清「为什么这个在设置里」。
   不要新建空壳页——Ling 没有语言/主题/CORS/限流这些配置项。
4. 若放领域页：用 `tabs.ts` 的 `*TabHref` 约定，`?tab=` 深链，**未知值回默认 tab 且不抛异常**。
5. 后端 `taxonomy.py` 的 `editable_route` 指向**真实存在**的 GET 端点（`test_settings_taxonomy.py:112` 会验）。
   若因此改变了「可编辑类数」，**主动**更新 `test_editable_count_is_exactly_six_today`（`:164`）。
6. 加/改设置项要同时改本文件对应小节，并给 `文件:行号`。

---

## 11. 依据索引（本文件所有断言的出处）

| 断言 | 出处 |
|---|---|
| 设置中心 3 组 | `clients/web-console/src/components/SettingsHub.tsx:57-116` |
| 迁出项只指路 | `SettingsHub.tsx:119-123`、`:211-223` |
| taxonomy 失败不挡路 | `SettingsHub.tsx:129-149`、`:163-177` |
| 重定向表三条 | `clients/web-console/src/route.ts:53-57` |
| 精确查找 + notFound 兜底 | `route.ts:63-64`、`:85` |
| `/settings/about` 仍在 | `route.ts:72`、`Navigation.tsx:70`、`tests/e2e/update_browser.mjs:545` |
| `replace: true` | `App.tsx:282-284`、`route.ts:167-173` |
| 首帧渲染目标页 | `App.tsx:95-100` |
| tab 深链 | `clients/web-console/src/tabs.ts:35-61`、`PersonaPage.tsx:40`、`LifeNowPage.tsx:67`、`MemoryPage.tsx:561` |
| 后端 9 类 | `src/ling/settings/taxonomy.py:74-198`（`CATEGORIES`；各 `label` 在 :78/:90/:102/:116/:132/:148/:161/:173/:189） |
| 可编辑恰好 6 类（测试锁） | `tests/unit/test_settings_taxonomy.py:164` |
| `editable_route` 诚实性锚 | `taxonomy.py:11-17`、`:60-71`、`test_settings_taxonomy.py:112` |
| 表外必须 notFound（测试） | `dashboard.test.ts:281-294`、`nav.test.ts:310-322`、`about.test.ts:108-118` |
| tab 契约测试 | `clients/web-console/src/tabs.test.ts`（12 用例） |
</content>
