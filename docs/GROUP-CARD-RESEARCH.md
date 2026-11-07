# 群名片 / 昵称识别：AstrBot 调研 + ling 改造建议

> 性质：**只读调研**，本文档是唯一产出，未改动任何代码。
> 调研对象：AstrBot `main` @ `86c24e846c0de78830f6a0bc402049edb0f0c671`（2026-11-05）。
> 下文 AstrBot 的 `file:line` 均按该 commit 计；链接给到同 commit 的 GitHub 页面，避免行号漂移。
> 凡没查到实证的，正文里显式写「未证实」，不猜。
>
> **关于行号**：ling 的 `file:line` 取于 2026-11-07 的工作区快照，当时**有其他改动正在进行**，
> 行号会漂。引用时请**以函数名为准**（正文每处行号旁边都给了符号名），行号只作定位提示。

---

## 0. 结论（先看这里）

**AstrBot 那边：**

1. **它不靠"库里的名字"称呼人。** 给模型的人名是**每轮从事件里现取**的
   （`astr_main_agent/request.py:83`），所以名片一改，下一句话就是新名字。
   这跟 ling 现在"`subjects.display_name` 只在建档时写一次"是**相反的设计**。
2. 入站取名的唯一入口是 `card` 优先、`nickname` 兜底：
   `event.sender.get("card") or event.sender.get("nickname", "N/A")`
   （`sources/aiocqhttp/aiocqhttp_platform_adapter.py:213`）。**没有回退到 QQ 号。**
3. 名字的**持久化是旁路**：只在"这条消息唤醒了她"时，用一张 10000 条的进程内 LRU
   做 diff，**名字变了才入队**，再由后台 task 写库（`pipeline/waking_check/umo_auto_name.py`）。
   SQL 层再挡一道：空名不覆盖、同名不写（`db/sqlite.py:2339-2366` 的
   `is_distinct_from`）。**全程零外部接口调用。**
4. 名字的存储粒度是 **`(umo, sender_id)`**（`db/po.py:369-382`），而 `umo` 里带会话
   标识——**同一个人在不同群各存一份名字**。这天然就是"群名片按群隔离"的模型。
5. **它不会为发送者补拉 `get_group_member_info`。** 只有 @ 提人时才拉
   （`aiocqhttp_platform_adapter.py:349-375`），且 card 为空才退到 `get_stranger_info`。
   也**没有定期刷新/定时任务**（`grep -i "refresh|cron|interval"` 在 `waking_check/` 零命中）。

**ling 那边，最小改动是三行（详见 §D.0）：**

- 第一行：`bridge` 把信封里**这条消息的** `sender_name` 透传进本轮上下文，当前这句话立刻用新名字（不写库、不外呼）。
- 第二行：`identity` 增加"变化才写"——用**昵称**（人级）与 `subjects.display_name` 比较，不同才 UPDATE；不给 owner 开特例（当前没有改名接口，特例等于把人永久冻结在首见名字上）。
- 第三行：进程内 diff 缓存 + 失败吞掉，保证入站不比现在慢、且绝不因改名失败拒收消息。

---

## A. AstrBot 的真实做法（含源码位置）

### A.1 入站取名的唯一入口

```python
# astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py:205-213
async def _convert_handle_message_event(self, event: Event, get_reply=True):
    assert event.sender is not None
    abm = AstrBotMessage()
    abm.self_id = str(event.self_id)
    abm.sender = MessageMember(
        str(event.sender["user_id"]),
        event.sender.get("card") or event.sender.get("nickname", "N/A"),
    )
```

- **优先级：`card` > `nickname`**，只此一处，消息事件全走它。
- 精确定义（Python 语义，别记错）：`card` 为空/缺省 → 取 `nickname`；若 `nickname`
  这个键**存在但是空串** → 结果是 `""`；只有 `nickname` **键完全不存在**才得到字面量
  `"N/A"`。即：**空名片 + 空昵称 = 空名字**，不会回退成 QQ 号。
  这也是它不如 ling 现在的地方（ling 至少在 `_sender_name` 里返回 `None`，
  显示层还能回退号码）。
- 读出处统一走 `AstrMessageEvent.get_sender_name()`
  （`platform/astr_message_event.py:297-307`），返回 `""` 而不是 `None`。

**同一仓库里还有两处，顺序是反的**（用的时候要小心，这不是笔误而是不一致）：

| 场景 | 代码 | 顺序 |
| --- | --- | --- |
| 消息发送者 | `aiocqhttp_platform_adapter.py:213` | `card` → `nickname` |
| 群成员列表 | `aiocqhttp_message_event.py:345-348` | `nickname` → `card` |
| 合并转发里的发送者 | `utils/quoted_message/chain_parser.py:370-375` | `nickname` → `card` → `user_id` → `"Unknown User"` |

也就是说：**AstrBot 自己内部对"名片还是昵称"没有统一口径**。ling 若照抄，只会抄到其中一种。

### A.2 提示词里的人名：每轮现取，不读库

```python
# astrbot/core/astr_main_agent/request.py:81-95
if cfg.get("identifier"):
    user_id = event.message_obj.sender.user_id
    user_nickname = event.message_obj.sender.nickname
    system_parts.append(f"User ID: {user_id}, Nickname: {user_nickname}")

if cfg.get("group_name_display") and event.message_obj.group_id:
    ...
    system_parts.append(f"Group name: {group_name}")
```

- `identifier` 与 `group_name_display` **默认都是 `False`**
  （`core/config/default.py:157-158`）——即默认**不告诉模型人名和群名**，要管理员开。
- 一旦开了，模型拿到的就是**这一刻**的名片，改名即时生效。这是 AstrBot
  "改名能跟上"的**真正原因**，不是因为它有个多聪明的缓存。

### A.3 名字的持久化：`UmoAutoNameRecorder`（缓存 diff + 后台写）

`astrbot/core/pipeline/waking_check/umo_auto_name.py` 全文只有 133 行，是这件事最值得抄的部分。

```python
# umo_auto_name.py:14
MAX_UMO_AUTO_NAME_CACHE_SIZE = 10_000

# umo_auto_name.py:41-87（节选）
def schedule(self, event):
    if self.db_helper is None or event.event_kind != "message":
        return
    umo = event.unified_msg_origin
    auto_name = get_event_auto_name(event, fallback_to_id=False)
    sender_id = str(event.get_sender_id() or "")
    sender_name = normalize_umo_name(event.get_sender_name())
    for key, name, cache, pending in (
        (umo, auto_name, self._cache, self._pending),
        ((umo, sender_id), sender_name, self._sender_cache, self._sender_pending),
    ):
        ...
        cached_name = cache.get(key)
        if cached_name == name or (not name and cached_name):
            cache.move_to_end(key); continue        # ← 没变：一次 DB 都不碰
        cache[key] = name
        pending[key] = (sender_id, name)            # ← 变了：只入队
    ...
    task = asyncio.create_task(self._flush(), name=f"umo_auto_name_writer:{...}")
```

要点逐条：

1. **判据是"内存里的上次值 vs 本次值"**，不是"查库对比"。所以零 SELECT。
2. **没变就 continue**，连队列都不进。
3. **空名不覆盖非空**：`not name and cached_name` → 跳过。
4. **后台写**，`asyncio.create_task`，注释原话：*"Persist changed session and sender names
   without blocking waking."*（`:18`）——**入站路径不等 DB**。
5. **有界缓存**：`OrderedDict` 当 LRU，超 10000 丢最旧；`pending` 超限时连 cache 一起清，
   避免"缓存说写过、其实被挤掉了"。
6. **失败回退**（`:110-117`）：写库异常 → 打 warning + **把该 key 从 cache 删掉**，
   这样下一条消息会重新判定并重试。不是静默丢弃，也不是死循环重试。
7. 只在 `is_wake` 时调度（`stage.py:244`、`:281`）——**没唤醒她的普通群消息不记名字**。
   这是它明确的取舍：宁可不更新，也不让每一条路过的话都打一次库。

DB 侧还有第二道保险（幂等）：

```python
# astrbot/core/db/sqlite.py:2359-2366
statement = statement.on_conflict_do_update(
    index_elements=[UmoSender.umo, UmoSender.sender_id],
    set_={"sender_name": statement.excluded.sender_name, "updated_at": now},
    where=(statement.excluded.sender_name != "")
    & col(UmoSender.sender_name).is_distinct_from(statement.excluded.sender_name),
)
```

→ 即使上层缓存判断失误（多进程/重启），SQL 也**不会用空串覆盖**、**同名不产生 UPDATE**。

测试把这些约定钉住了，可以直接对照 ling 的测试设计：
`tests/unit/test_waking_check_umo_alias.py`
（`records_only_awakened_events` / `coalesces_auto_name_changes` /
`skips_missing_group_and_sender_names` / `bounds_auto_name_cache` /
`retries_after_database_failure`）。

### A.4 存储粒度：`(umo, sender_id)` —— 名字是"每个群一份"

```python
# astrbot/core/db/po.py:369-382
class UmoSender(TimestampMixin, SQLModel, table=True):
    """Latest sender names observed within each unified message origin."""
    __tablename__: str = "umo_senders"
    id: int | None = ...
    umo: str = Field(nullable=False, max_length=512)
    sender_id: str = Field(nullable=False, max_length=255)
    sender_name: str = Field(default="", nullable=False, max_length=255)
    __table_args__ = (UniqueConstraint("umo", "sender_id", name="uix_umo_sender"),)
```

`umo` 形如 `platform:GroupMessage:<group_id>`（测试里就是这么断言：
`"test-platform:GroupMessage:group-1"`）。所以：

- **同一个人在两个群有两行、两个名字**——群名片按群隔离，这是"正确答案"的形态。
- AstrBot **对 QQ/OneBot 而言**没有"这个人的统一显示名"这一层概念（`db/po.py` 里没有
  user/member 级的名字表，只有按 `umo` 索引的名字），它把"人"的身份完全交给 `sender_id`，
  名字纯标签。（其它平台如 webchat 有自己的用户表，未纳入本次结论。）这一点跟 ling 的
  `subjects.display_name`（人级）+ 未来的群级名字，是**两套模型**，不能直接照搬。

**同一套机制还有"人工别名"这一层，优先级在自动名之上：**

- `/name <别名>` 内置命令写 `umo_aliases.user_alias`
  （`astrbot/builtin_stars/builtin_commands/commands/name.py:10-48` → `db.upsert_umo_alias`）。
- 显示优先级写死在 `core/umo_alias.py:59-68`：
  `user_alias` → `auto_name` → `umo` 本身。
- **这就是 AstrBot 对"自动采集的名字不能盖掉人定的名字"的答案**：不是靠权限判断，
  而是**分层**——人工层永远在自动层之上。

名字还有第三个落点：**平台消息历史逐条快照**
（`astr_message_event.py:142` 传 `sender_name=self.get_sender_name()` →
`db/po.py:246-265` 的 `PlatformMessageHistory.sender_name`）。
→ **旧消息保留旧名字**，历史上不会集体改名。这一点很关键：它不是 bug，是"当时说了什么、
当时叫什么"的如实记录。

### A.5 它会不会为发送者补拉 `get_group_member_info`？

**不会。** 全仓库只有一处 `get_group_member_info` 调用，在 **@ 提人** 的分支里：

```python
# aiocqhttp_platform_adapter.py:349-375（节选）
at_info = await self.bot.call_action(
    action="get_group_member_info",
    group_id=event.group_id, user_id=int(m["data"]["qq"]),
    no_cache=False,                      # ← 用缓存，不强制刷新
    **routing_params,
)
if at_info:
    nickname = at_info.get("card", "")
    if nickname == "":
        at_info = await self.bot.call_action(
            action="get_stranger_info", user_id=int(m["data"]["qq"]),
            no_cache=False, **routing_params,
        )
        nickname = at_info.get("nick", "") or at_info.get("nickname", "")
```

- 触发条件：**消息里出现 `at` 段**，且**机器人真的要把这个 at 渲染成名字**。
- 失败处理：`except ActionFailed` → 记日志，**该 at 段的名字留空**，消息照收。
- 注意 `At.name` 用的也是 `card` 优先；card 空才 `get_stranger_info`（陌生人信息里
  **没有 `card` 字段**，见 §E 附录，所以这一步只能拿到昵称）。
- **没有节流**：一条消息里 @ 5 个人就是 5 次 `get_group_member_info`（+ 可能的
  `get_stranger_info`）。仓库里没有任何针对 action 的限流器（见 §B.4）。
- `get_group_member_list` 只在 `get_group()`（`aiocqhttp_message_event.py:263-350`）里拉，
  属于**按需**（插件主动要群成员列表时），**不在消息主链路上**。

### A.6 群名：每条消息带，还是建会话时拉一次？

两条路都有，**都不在入站主链路**：

1. **每条消息带（大多数时候是 None）**：
   `abm.group.group_name = event.get("group_name")`
   （`aiocqhttp_platform_adapter.py:219`）。注意 OneBot v11 的 `group_message` 事件
   **规范里没有 `group_name` 字段**（已核对 §E 附录），所以这里通常是 `None`；
   只有某些实现额外塞了才有值。
2. **按需拉**：插件调 `event.get_group()` 时，才 `get_group_info` + `get_group_member_list`
   （`aiocqhttp_message_event.py:288-350`，带 `try/except`，失败降级成"只有 id 的 Group"）。

→ 没有任何定时/周期刷新。群名改了，除非有插件重新 `get_group()` 并绕过缓存
（`get_group_info` 默认 `no_cache=False` 走缓存），否则一直旧。

### A.7 那么：AstrBot 满足"改名能跟上"吗？

**分三层回答，结论是"能，但靠的是不缓存，不是靠刷新"：**

| 层 | 是否跟得上 | 依据 |
| --- | --- | --- |
| 模型这一轮看到的称呼 | ✅ 立即 | 每轮读 `event.sender.nickname`（`:83`） |
| UI/仪表盘/按名字搜索 | ⚠️ 下一次**唤醒**才跟上 | `umo_senders` 只在 `is_wake` 时更新（`stage.py:244,281`） |
| 历史消息 | ❌ 永不改（设计如此） | `PlatformMessageHistory.sender_name` 是当时快照（`astr_message_event.py:142`） |

它的取舍很清楚：**"当下正确"优先，"历史一致"保留，"库里的名字"降级成旁路索引**。
AstrBot 从不为了一条消息的称呼去查库——所以它根本没有"库里的名字过期"这个问题。

### A.8 已证实的坑（照抄会踩）

1. **顺序不一致**（§A.1 表格）：三处代码两种优先级。抄之前先定自己只认一种。
2. **`"N/A"` 字面量**：会进 `MessageMember.nickname`，进而可能进提示词和库。
   ling 千万不要把占位符写进 `display_name`。
3. **只唤醒时记录**：普通群消息不更新名字。ling 的群是"多人闲聊她可能不接话"，
   照抄会导致"只有 @"她的消息里名字是新的。
4. **@ 提人无节流**：一条消息 N 个 at = N 次 action 调用，没有限流器兜底。

---

## B. 其它实现的共识

### B.1 NoneBot2 + `adapter-onebot`

框架只做**字段搬运**，不做名字解析，更不建名字缓存：

```python
# nonebot/adapters/onebot/v11/event.py:67-76
class Sender(BaseModel):
    user_id: Optional[int] = None
    nickname: Optional[str] = None
    sex: Optional[str] = None
    age: Optional[int] = None
    card: Optional[str] = None
    area: Optional[str] = None
    level: Optional[str] = None
    role: Optional[str] = None
    title: Optional[str] = None
```

所有字段 `Optional`、`extra="allow"` —— 即**框架承认这些字段随时缺失**。
社区插件的标准写法就是一行 `event.sender.card or event.sender.nickname`
（或 `event.get_user_id()` 兜底），**没有插件会为此查库或每条外呼**：
名字就在事件里，白拿的。

### B.2 Koishi（`koishi-plugin-adapter-onebot`）

- **不把群名片当作者名**。`src/bot/index.ts:63-70` 里频道名直接用 id：
  `if (channelId.startsWith('private:')) { ... name: userId }`，群频道走
  `internal.getGroupInfo(channelId)`（`:73-75`）。
- 唯一用到 `nickname` 的地方是**合并转发的节点**：`src/bot/message.ts:255`
  `Object.assign(this.stack[0].author, pick(attrs, ['userId','username','nickname','time']))`
  —— **`card` 根本没被 pick**。
- 结论：Koishi 把"显示名"留给上层/前端，适配器只保证**原始 payload 可取**。

### B.3 三家共识（能直接落成 ling 的设计原则）

| 问题 | AstrBot | NoneBot2 | Koishi | 共识 |
| --- | --- | --- | --- | --- |
| 名字从哪来 | 事件 `card`/`nickname` | 事件字段原样给插件 | 事件 payload | **事件里现成的，别查库** |
| 每条消息都外呼？ | 否 | 否 | 否 | **入站路径零外呼** |
| 名字落库？ | 是（`umo_senders`，旁路） | 否 | 否 | 可选，但**必须旁路** |
| 落库粒度 | `(umo, sender_id)` 即按会话/群 | — | — | 群名片就该按群存 |
| 变化才写？ | 是（内存 diff + SQL 双保险） | — | — | **是** |
| 空值策略 | 空名不覆盖、不写 | 全 Optional | — | **绝不写空/占位符** |
| 定期刷新 | 无 | 无 | 无 | **没人做定时刷新** |
| 人工改名 | `/name` → `user_alias`，**优先于**自动名 | 留给自己实现 | 留给上层 | **人工层压住自动层** |

**一句话共识**：群名片是**随消息免费携带的易变标签**；正确做法是"跟着消息走、变了才落库"，
而不是"建会话时快照 + 永久复用"，更不是"每次外呼查一遍"。

### B.4 实现侧的限频证据（含一个反直觉发现）

**先说结论：NapCat / Lagrange.Core 都没有内置的 action 限流器；真正的风险来自 QQ 服务端
风控，而这两个仓库里没有可引用的公开阈值。**

- NapCat `main` 全树 1295 个条目 / 1111 个文件（`truncated=false`），无
  `rate/limit/throttle/frequency` 相关模块（只有无关的 `global-migrate` 命中）。
- Lagrange.Core `master` 全树 851 个条目 / 734 个文件（`truncated=false`），同样零命中。
- 所以"NapCat 限频 X 次/分钟""Lagrange 限频 Y"这类数字：**未证实，不要写进代码注释或文档**。

**但有件确实存在、且直接影响成本的事**（这条值得单独记一笔）：

```ts
// NapCatQQ/packages/napcat-onebot/action/group/GetGroupMemberInfo.ts:63-68
async _handle (payload: PayloadType) {
  const isNocache = this.parseBoolean(payload.no_cache ?? true);   // ← 默认 true
  const member = await this.getGroupMemberInfo(payload, isNocache);
  ...
}
```

- OneBot v11 规范里 `no_cache` **默认 `false`**（用缓存，快）；
- **NapCat 把缺省当成 `true`**（`payload.no_cache ?? true`）→ 调用方不传 `no_cache`
  就是在**强制刷新**，走 `getGroupMemberEx(..., true)` 的慢路径。
- 所以：**ling 若真要调 `get_group_member_info`，必须显式传 `no_cache: false`**，
  否则每次都是"最贵的那一档"。这是个 5 个字符成本、能省一个数量级延迟的点。

---

## C. 给 ling 的具体建议

### C.1 先把三种"名字"拆开（当前 ling 混成了一种）

| 名字 | 语义 | 变不变 | 现在存在哪 |
| --- | --- | --- | --- |
| 群名片 | **这个人在这一个群里的名字** | 随时改 | ❌ 没有位置（被塞进 `subjects.display_name`） |
| 昵称 | **这个 QQ 号自己的名字**（跨群） | 偶尔改 | ❌ 同上，被名片覆盖 |
| 主体显示名 | **"这个人"的正式名字**（后台/私聊标题用） | 建档时定，**目前改不了** | `subjects.display_name`（`models.py:91`） |

现状（`bridge.py:128-151` → `identity/service.py:787-856`）：
**只有第一次见这个人时**写 `display_name`（`:836`），之后永不更新；而且写进去的是
`card` 优先——**群名片把"人"的名字占了**，且只有第一个群的份。

**另一个必须一起承认的缺口：ling 现在没有任何"改 subject 显示名"的入口。**
`Subject(...)` 在全仓库只有两处构造（`identity/service.py:286` 建用户、`:833` 自动建号），
`grep display_name` 在 `api/routes/` 里全是**读**（`sessions.py:83`、`manage.py:1139`），
没有 rename 接口。所以任何"人工定的名字优先、自动采集不许覆盖"的规则，
在补改名接口之前都等于"把这个人**永久冻结**在第一次见到的名字上"。
→ 这就是为什么 P0 的方案里**不给 owner 开特例**（见下），而是换判据。

### C.2 ① 每条入站消息"不同才更新" —— 是，但分两级，不要一步到位

**建议采纳，理由是成本为零**：`card/nickname` 已经在 `envelope.sender_name` 里
（`connectors/onebot/connector.py:375`、`contracts/v1.py:259-262`），拿它跟库里比不需要
任何外部接口。但要分两级，否则会引入新问题：

**第一级（立刻做，零 DB、零外呼）：当前这句话用最新名字。**
`ContextCompiler.compile()` 早就有 `user_sender_name` 参数（`kernel/context.py:213`），
但**全仓库没有任何调用方传它**（`grep -rn user_sender_name` 只有定义和回落分支）。
结果 `context.py:907-913` 永远回落到 `Subject.display_name` —— 也就是说
**信封里那个最新的名片，现在被直接丢弃了**。

把 `bridge._run_turn`（`bridge.py:808-817`）→ `turn.run_turn`（`turn.py:251-264`）→
`turn._execute`（`turn.py:303-316`）→ `compile`（`turn.py:404-412`）这条链补一个
`sender_name` 参数即可（`source_account` 就是这么穿的，照抄路径）。
**收益**：改名后她**下一句**就换称呼，不需要任何写库，多副本也不会不一致。

**第二级（要"库里的名字"也跟上才做）：变化才 UPDATE `subjects.display_name`。**
放在 `bridge.handle` 身份解析之后（而不是塞进 `resolve_or_provision`），因为
**`resolve_or_provision` 只在"未证明"分支被调**（`bridge.py:128-151`）——
**已绑定的主人/管理员走的是 `resolve_account`，永远不会进那个函数**，
把更新放进去等于"主人改名永远不生效"。这是个容易踩的坑。

> **第二级用什么名字更新？建议 `nickname` 优先，`card` 只在昵称缺失时兜底**，而不是沿用
> 首见时的 `card` 优先。理由：`subjects.display_name` 的语义是**"人"**，`nickname` 才是人级
> 字段（跨群唯一）；`card` 是群级字段，改了群就变，拿它写人级列必然抖动。
> 而"群里此刻该叫什么"已经由第一级当场解决，不依赖这一列。
>
> 也**不要**给 owner 开"永不更新"的特例：没有改名接口（见 §C.1），特例 = 永久冻结。
> 正确的"人工层"是 P1 加 `display_name_source`/`display_name_locked` 列 + 一个改名接口；
> 在那之前，`nickname` 变化就是真改名，让它生效是对的。

伪代码（P0 全部逻辑）：

```python
# 3 个约束：非空、变了才写、只用"人级"名字（nickname）
async def observe_display_name(session, *, subject_id, connector, account, name):
    name = (name or "").strip()[:128]
    if not name:
        return
    if _NAME_CACHE.get((connector, account)) == name:
        return                      # 内存 diff：绝大多数消息到此为止
    _NAME_CACHE[(connector, account)] = name
    await session.execute(
        update(Subject)
        .where(Subject.id == subject_id,
               Subject.display_name.is_distinct_from(name))
        .values(display_name=name)
    )
```

用 `is_distinct_from` 而不是 `!=`（NULL 安全），跟 AstrBot `sqlite.py:2363` 同一个理由。

### C.3 ② 什么时候才该补拉 `get_group_member_info`

**默认不拉，四条里只有后三条才真的调接口**；真要调时必须显式 `no_cache: false`（§B.4）：

| 场景 | 为什么值得 | 建议做法 |
| --- | --- | --- |
| `sender.card` **和** `sender.nickname` 都空，且库里已有名字 | 空名不该覆盖旧名，但可能真改成了空 | **不拉**，直接跳过（保持旧值）。空名不是"改名"的信号 |
| 用户明确要求"叫准点"/管理员手动刷新 | 人主动要的准确性 | 走**读侧**接口（像 `fetch_group_name` 那样），不在入站链路上 |
| 建会话首屏 / 群成员列表页 | 一次拉一批，摊薄成本 | 用 `get_group_member_list`（一次一个群），别逐人 `get_group_member_info` |
| 收到 `notice`：`group_card` 变更事件 | 这是**唯一权威的"名片变了"信号** | 见下 |

**最该被利用、而现在 ling 直接丢掉的是 `notice/group_card` 事件。**
`connector.py:339-346` 里 `notice`/`request` 一律 `ChannelError(unsupported)` 拒掉：

```python
raise ChannelError(ChannelErrorKind.unsupported, f"暂不处理 OneBot 事件类型: {post_type}")
```

而 OneBot v11 有 `notice_type=group_card`（群名片变更）事件，字段含 `group_id`/`user_id`/`card_new`/`card_old`
（**本条的字段名我按规范记忆写出，未在本次调研中打开规范原文核对 → 标"未证实"，落地前请核
`botuniverse/onebot-11/event/notice.md`**）。如果核实，这就是**零外呼、零轮询、事件驱动**的
最优解：名片一变，NapCat 主动推给你。优先级应高于 AstrBot 的"下一条消息 diff"。

> 取舍：`group_card` 事件要额外的连接器分支 + 幂等处理（事件可能重投），
> 比"消息里 diff"复杂。所以 P0 仍用消息 diff，**P2 再上事件**（§D）。

### C.4 ③ 缓存 / 限频 / 失败回退

**ling 已经有现成的、写得比 AstrBot 更细的模板：`OneBotConnector.fetch_group_name`**
（`connectors/onebot/connector.py:531-593`）。直接照它的形状写，别另发明：

| 做法 | 现成实现 | 建议用于成员名 |
| --- | --- | --- |
| 成功缓存 TTL | `group_name_ttl = 6*3600`（`:83`） | 群名片变，用 **300 s** 更合适 |
| 失败缓存 TTL（短） | `group_name_miss_ttl = 300`（`:84`） | 失败 **60 s** 静默，别一直打 NapCat |
| 单次超时 | `group_name_timeout = 2.0`（`:87`）+ `asyncio.wait_for` | 同 2.0 s |
| 有界 | `group_name_cache_max = 512` + 按时间戳裁剪（`:585-593`） | 成员名量级更大，**4096** 起，按 `(群,人)` 时间戳裁 |
| 失败绝不抛 | `except Exception` → 返回 `None`（`:572-578`） | 同：名字拿不到只是"叫不出名字"，不能让消息失败 |
| 纯内存 vs 落库 | 群名**只内存** | 成员名**要落库**（历史消息要能回显当时称呼） |

**失败回退三条硬规则**（缺一条都会把"旧名字"换成"坏数据"）：

1. 拿不到/为空 → **什么都不写**，保留旧值（绝不是写空串）。
2. 写库失败 → **吞掉并降级为 debug 日志**，消息必须照收（现在的 `resolve_or_provision`
   若因新逻辑抛异常，整条消息会被 `bridge` 拒绝——这是回归风险，必须包 `try/except`）。
3. 内存缓存里**不要缓存失败结果去做"名字"判定**（AstrBot 的做法是失败就删 cache key，
   让下一条重试；照抄）。

**限频**：进程内 diff 已经消掉了 99% 的写。剩下的是"真改名"——一个人一天改不了几次
名片，不需要额外限频。**不要**为此引入 Redis/分布式锁：多副本同时写同一条 UPDATE 是幂等的，
代价只是多一次写。

### C.5 ④ 与 `ActorAccount` / `Subject` 的关系：要不要分表？

**先说取舍，再说最小方案。**

现状（`models.py:85-116`）：

```
subjects(id, display_name, proof_level, trust_level, merged_into, ...)
actor_accounts(id, connector_instance, external_account_id, subject_id, proof, revoked_at, ...)
```

- `actor_accounts` 的语义是**绑定**（谁是谁），本来就不该塞名字。
- `subjects.display_name` 的语义在代码注释里已经写明是"标签"
  （`identity/service.py:833-836`、`conversation/service.py:657-666` 都说"群名片优先"），
  **但它只有一列，装不下"多个群多个名片"**。

三种方案：

| 方案 | 改动 | 优点 | 缺点 | 判定 |
| --- | --- | --- | --- | --- |
| **单列方案：只改 `subjects.display_name`（变化才写）** | 0 迁移 | 立刻修掉"永远旧名字"；读侧全都不改 | 多群用户会**抖动**（A 群名片 → 覆盖成 B 群名片 → 再覆盖回来）；人级名字被群级名字污染 | ✅ **P0，先上** |
| **账号列方案：`actor_accounts` 加一列 `display_name`** | 1 列 | 名字跟账号走，比 subject 更贴近"平台事实" | 同一个人两个群、**一个 QQ 号仍只有一列** → 抖动照旧；且 subject 级显示名还得回查 account | ❌ 不上不下 |
| **独立表方案：新表 `channel_member_names(connector_instance, scope_id, external_account_id, display_name, source, updated_at)`** | 1 表 + 迁移 | 群名片按群隔离（`scope_id=群号`），昵称按人（`scope_id=''`）；读侧可按窗口挑名字 | 读侧要 join（3 处：`context.py:907`、`attention/service.py:669`、`conversation/service.py:657`） | ✅ **P1，需要"按群准确"时再上** |

**为什么"单列方案"的多群抖动可以接受（P0 阶段）**：ling 的群窗口是"她在一个群里"，
一轮对话里她只需要当前群的名片；抖动只影响**别的群**与她**私聊**时用的名字。
而私聊用"群名片"本身就不对——这正是**独立表方案**要修的。**先单列、后独立表，是安全的渐进**。

**读侧回退链（P1 定稿，P0 就用后三档）**：

```
当前群的名片(channel_member_names, scope=group_id)
  → 账号昵称(channel_member_names, scope='')
    → subjects.display_name（人级/管理员改的）
      → QQ 号（只在 UI 显示层，绝不入库）
```

**P1 表结构（照 `0049_user_preferences` 的写法，独立成表不进 config）：**

```sql
CREATE TABLE channel_member_names (
  connector_instance   VARCHAR(64)  NOT NULL,
  scope_id             VARCHAR(64)  NOT NULL DEFAULT '',  -- '' = 账号级昵称；否则群号
  external_account_id  VARCHAR(128) NOT NULL,
  display_name         VARCHAR(128) NOT NULL,
  source               VARCHAR(16)  NOT NULL,             -- 'card' | 'nickname'
  updated_at           TIMESTAMP    NOT NULL,
  PRIMARY KEY (connector_instance, scope_id, external_account_id)
);
```

（**不加**外键到 `actor_accounts.id`：账号会被解绑/重绑，名字记录不该跟着级联消失；
用 `(connector_instance, external_account_id)` 做逻辑关联，和现有 `uq_account_external` 一致。）

---

## D. 实现清单

### D.0 最小改动方案（三行）

- **第一行**：`bridge._run_turn` → `run_turn` → `_execute` → `compile(user_sender_name=...)` 透传信封里的 `sender_name`——**当前这句话立刻用新名片，零写库零外呼**（这一行单独就能解决 90% 的"叫错名字"）。
- **第二行**：信封补 `sender_nickname`（连接器本来就拿得到），`bridge.handle` 解析身份后调 `identity.observe_display_name(...)`——**用 `nickname`（人级）**、与库里不同才 UPDATE，`is_distinct_from` 保证 NULL 安全，**不给 owner 开特例**（没有改名接口，特例=永久冻结）。
- **第三行**：该函数自带进程内 `(connector, account) → name` diff 缓存（有界）+ `try/except` 吞异常——**入站不变慢，改名失败绝不拒收消息**。

### D.1 文件级清单

| 阶段 | 文件 | 做什么 | 风险 |
| --- | --- | --- | --- |
| **P0** | `src/ling/kernel/bridge.py` | `handle`(`:122`) 解析出 `subject_id` 后调 `identity.observe_display_name(...)`；`_run_turn` 增 `sender_name` 参数并透传 | 低 |
| **P0** | `src/ling/kernel/turn.py` | `run_turn`(`:251`) / `_execute`(`:303`) 增 `sender_name`，`compile(user_sender_name=...)`(`:404`) | 低（纯新增可选参数） |
| **P0** | `src/ling/domains/identity/service.py` | 新增 `observe_display_name()`（约 30 行）；**不动** `resolve_or_provision` 的建号逻辑 | 中：写库失败必须吞 |
| **P0** | `connectors/onebot/connector.py` | `_sender_name`（`:611-625`）拆出 `_sender_nickname`；`sender_name` 维持 `card > nickname`（群内称呼不变），**新增** `sender_nickname` 字段 | 低（纯新增） |
| **P0** | `src/ling/contracts/v1.py` | `InteractionEnvelope` 增可选字段 `sender_nickname`（`str` 或 `None`，放在 `:259-262` 旁边，`StrictModel` 加可选字段向后兼容：旧连接器不发也能跑） | 低 |
| **P1** | `src/ling/infra/models.py` | 新增 `ChannelMemberName`（§C.5 独立表方案） | 中：schema 变更 |
| **P1** | `migrations/versions/0051_channel_member_names.py` | 建表（照 `0049` 风格，含"为什么需要"注释） | 中：需人工核对 autogenerate |
| **P1** | `src/ling/kernel/context.py:907-913` | 回退链改成"群名片 → 昵称 → subject.display_name" | 中：多一次查询（可按 `(window, subject)` 缓存） |
| **P1** | `src/ling/domains/attention/service.py:669-681` | `_sender_names` 同上；注意它只拿得到 `subject_id`，需 join `actor_accounts` | 中：群窗口每人多一次 join |
| **P1** | `src/ling/domains/conversation/service.py:657-690` | `_sender_labels` 同上（会话页"发言人名片"） | 中 |
| **P2** | `connectors/onebot/connector.py` | ①`notice_type=group_card` 分支（**字段名待核**，见 §C.3）；②`fetch_member_card(group, user)` 读侧采集，形状照 `fetch_group_name`（`:531-593`），**必须 `no_cache: false`** | 高：事件语义/重投幂等 |

**明确不建议的事**：

- ❌ 不改 `subjects.display_name` 的 `String(128)`：`_sender_name` 已经 `[:128]`，长度是对齐的。
- ❌ 不在入站路径加任何 `get_group_member_info`（违反"入站是最不能慢的一条路"，
  `api/routes/sessions.py:37-48` 已经把这条原则写下来了）。
- ❌ 不引入定期全量刷新任务（三家实现都没做，收益低、风控风险高）。
- ❌ 不把 QQ 号/空串/`"N/A"` 写进 `display_name`。

### D.2 测试清单

**P0（新建 `tests/unit/test_identity_display_name.py`）**：

1. 首次消息 → 建 subject，`display_name == card`（沿用现有规则）；
2. 第二条同名 → **零 UPDATE**（用 SQLAlchemy `before_cursor_execute` 计数，或断言缓存命中）；
3. 改名（card A → B）→ `display_name == "B"`；
4. card 空、nickname 空 → **保持旧值，不写空**；
5. owner 改**昵称**→ 跟着更新（没有改名接口时不能冻结）；但 owner 只改**群名片**时 `subjects.display_name` 不变；
6. 写库抛异常 → `bridge.handle` 仍 `accepted=True`（故障注入，照 `tests/test_fault_injection_matrix.py` 的风格）；
7. 有界缓存：超过上限时丢最旧（照 `test_waking_check_umo_alias.py::..._bounds_...`）。

**P0（补现有集成测试）**：

8. `tests/integration/test_session_display_fields.py`：发一条 card=`阿懒` → 会话页名字=`阿懒`；
   再发一条 card=`懒懒` → **同一个接口立刻变 `懒懒`**（这条就是需求本身）；
9. `tests/integration/test_onebot_loop.py`：`sender.card` 与 `sender.nickname` 都齐全时 `sender_name` 取 card、`sender_nickname` 取 nickname；
   `card=""` 时 `sender_name` 取 nickname；两者都空时两个字段都是 `None`（**不写 "N/A"**）。

**P1**：

10. 同一个人两个群不同名片 → 两个群窗口各显示各的名字（核心回归）；
11. 迁移：`upgrade` / `downgrade` 各跑一次（照现有迁移测试）；
12. 读侧没有 scoped 记录时，正确回退到 `subjects.display_name`。

### D.3 风险（分级）

| 风险 | 说明 | 处置 |
| --- | --- | --- |
| **主人称呼被群名片污染**（高） | 若用 `card` 写人级列：主人 `subjects.display_name` 会被某个群的名片覆盖 → 私聊标题、`peer_display_names`（`conversation/service.py:275-317`）跟着变，且多群抖动 | **用 `nickname` 而非 `card` 写人级列**（群名片只走 P0 第一行、当轮生效，不落库）；P1 才把群名片落进 scoped 表 |
| **多群抖动**（中） | 同一个人 A 群名片 ↔ B 群名片来回覆盖 | P0 接受并写进文档；P1 用 scoped 表根治 |
| **写放大**（中） | 每条消息一次 UPDATE | 进程内 diff 缓存（AstrBot 同法）；多副本只是多写一次，幂等 |
| **回归：改名失败导致消息被拒**（高） | 新逻辑若在 `bridge` 主链路上抛异常，整条消息失败 | `observe_display_name` 内部 `try/except` 全包，失败只 debug 日志 |
| **占位名污染**（中） | 写进 `"N/A"`/空串后，因为"变了才写"永远不会自愈 | 白名单校验：strip 后非空、长度 ≤128、不等于 `N/A` |
| **PII**（中） | 群名片是个人信息，会进库与日志 | 日志沿用 `hash_account_ref`（`bridge.py:164` 已有先例），不打印原名；scoped 表按 `updated_at` 可清理 |
| **测试环境 vs 生产**（低） | 生产是 NAS 上的容器，迁移要在升级流程里跑 | 按现有 alembic 流程走，别手改库 |

---

## E. 附录：OneBot v11 字段语义（官方规范，已核对原文）

来源：`botuniverse/onebot-11` 的 `event/message.md` 与 `api/public.md`（链接见 §G）。

**群消息事件的 `sender`：**

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `user_id` | int64 | 发送者 QQ 号 |
| `nickname` | string | 昵称 |
| `card` | string | **群名片／备注** |
| `sex` / `age` / `area` | string / int32 / string | 性别、年龄、地区 |
| `level` | string | 成员等级 |
| `role` | string | 角色：`owner` / `admin` / `member` |
| `title` | string | 专属头衔 |

**私聊消息的 `sender`** 只有 `user_id` / `nickname`（+ `sex`/`age`）——**没有 `card`**。
→ 私聊里"名片"这个概念根本不存在，`_sender_name` 在私聊就是取昵称，是对的。

**规范原文（最关键的一句，直接支撑本文所有结论）：**

> "需要注意的是，`sender` 中的各字段是尽最大努力提供的，也就是说，不保证每个字段都一定存在，
> 也不保证存在的字段都是完全正确的（**缓存可能过期**）。尤其对于匿名消息，此字段不具有参考价值。"

→ 也就是说：**`sender.card` 本身可能就是过期值**。想更准只有两条路：等平台推 `group_card`
notice，或显式调 `get_group_member_info`。这条也解释了为什么"只靠消息里的 card"永远有残差。

**`get_group_member_info`**（参数 `group_id` / `user_id` / `no_cache`，`no_cache` 默认 `false`）：

返回 `group_id` / `user_id` / `nickname` / `card` / `sex` / `age` / `area` / `join_time` /
`last_sent_time` / `level` / `role` / `unfriendly` / `title` / `title_expire_time` / `card_changeable`。

**`get_stranger_info`**（参数 `user_id` / `no_cache`）：

返回 `user_id` / `nickname` / `sex` / `age` —— **没有 `card`**。所以"名片为空时退到
`get_stranger_info`"只能拿回昵称，拿不回名片（AstrBot 正是这么退的，§A.5）。

**`get_group_info`**（参数 `group_id` / `no_cache`）：返回 `group_id` / `group_name` /
`member_count` / `max_member_count`。**群消息事件规范里没有 `group_name` 字段**，
所以 AstrBot 的 `event.get("group_name")` 通常拿不到值（§A.6）。

---

## F. 未证实 / 需人工核实

1. **`notice_type=group_card` 的确切字段名**（`card_new`/`card_old`？）——本次未打开
   `botuniverse/onebot-11/blob/master/event/notice.md` 原文核对，落地前必须核。
2. **NapCat / Lagrange 的具体限频阈值**——两个仓库里都**没有**限流模块（全树搜索零命中），
   网上流传的数字无出处。**不要引用具体数值。**
3. **NapCat 的 `get_group_member_ex(..., no_cache=true)` 内部是否还有自己的缓存**——
   `GetGroupMemberInfo.ts:41-61` 只看到"优先按 QQ 号在本群查询，缓存未命中会刷新一次"，
   更下层（NTQQ 内核）行为不可见。
4. **QQ 服务端对 `get_group_member_info` 的风控行为**——无公开文档，只能靠实测。
5. **AstrBot 其它平台适配器**（KOOK/Discord/Lark）的名字策略只扫了一遍，未逐行核实；
   本文 A 部分的结论**只对 aiocqhttp(OneBot v11) 成立**。

---

## G. 参考

**AstrBot（commit `86c24e8`）**

- 入站取名：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L205-L220
- @ 提人补拉：同文件 `#L349-L375`
- `get_sender_name()`：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/platform/astr_message_event.py#L297-L307
- 名字旁路记录：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/waking_check/umo_auto_name.py
- 调度点：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/pipeline/waking_check/stage.py#L240-L285
- DB upsert：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/db/sqlite.py#L2339-L2366
- 表定义：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/db/po.py#L369-L382
- 提示词注入名字：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/astr_main_agent/request.py#L81-L95
- 相关测试：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/tests/unit/test_waking_check_umo_alias.py
- 人工别名 `/name`：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/builtin_stars/builtin_commands/commands/name.py#L10-L48
- 名字显示优先级：https://github.com/AstrBotDevs/AstrBot/blob/86c24e846c0de78830f6a0bc402049edb0f0c671/astrbot/core/umo_alias.py#L59-L68

**OneBot v11 规范**

- 消息事件 `sender` 字段：https://github.com/botuniverse/onebot-11/blob/master/event/message.md
  （群消息 `sender` = `user_id / nickname / card / sex / age / area / level / role / title`；
  原文："`sender` 中的各字段是尽最大努力提供的……**不保证存在的字段都是完全正确的（缓存可能过期）**。
  尤其对于匿名消息，此字段不具有参考价值。"）
- `get_group_member_info` / `get_stranger_info` / `get_group_info`：
  https://github.com/botuniverse/onebot-11/blob/master/api/public.md
  （`no_cache` 默认 `false`；`get_stranger_info` 返回 **`nickname`，没有 `card`**；
  `get_group_member_info` 返回含 `card`/`role`/`card_changeable`）

**其它实现**

- NoneBot2 `adapter-onebot` Sender 模型：https://github.com/nonebot/adapter-onebot/blob/master/nonebot/adapters/onebot/v11/event.py#L67-L76
- Koishi OneBot 适配器：https://github.com/koishijs/koishi-plugin-adapter-onebot/blob/main/src/bot/index.ts#L63-L75
- NapCat `GetGroupMemberInfo`（`no_cache` 默认 true）：https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/group/GetGroupMemberInfo.ts#L63-L68
- Lagrange.Core（无限流模块，全树 851 条目 / 734 文件）：https://github.com/LagrangeDev/Lagrange.Core

**ling 内部相关**

- `connectors/onebot/connector.py:611`（`_sender_name`，`card > nickname`）
- `connectors/onebot/connector.py:531-593`（`fetch_group_name`：TTL/超时/有界/失败吞掉的**模板**）
- `src/ling/kernel/bridge.py:128-151`（身份解析两个分支）、`:808-817`（`_run_turn`）
- `src/ling/domains/identity/service.py:787-856`（`resolve_or_provision`，"只写一次"的根源）
- `src/ling/infra/models.py:85-116`（`Subject` / `ActorAccount`）
- `src/ling/kernel/context.py:907-913`（当前发言人的名字，`user_sender_name` **从未被传入**）
- `src/ling/domains/attention/service.py:669-681`、`src/ling/domains/conversation/service.py:657-690`
- `docs/IDENTITY-MULTIUSER-DESIGN.md`（`known`/`owner` 分级的既有设计）
