# 协议能力调研：ling 接入器下一步支持什么

> 调研日期：2026-11-09。范围：OneBot v11、QQ 开放平台 v2、OneBot v12；AstrBot、MaiBot、ling 源码。本文是设计输入，不代表平台权限已开通。

## 读法与证据边界

- **规范能力**是文档定义；**实现能力**是源码有调用/分派；**业务接入**是事件真正进入对话或动作可由产品调用。三者不能互换。
- 「已」=默认路径确实消费/发送；「部分」=仅部分场景、只透传给插件，或需要配置/权限；「未」=所查模块没有该行为。源码缺失结论只限所列模块，不推断全生态插件。
- 固定版本： [OneBot v11](https://github.com/botuniverse/onebot-11/tree/d4456ee706f9ada9c2dfde56a2bcfc69752600e4)、[v12 草案](https://github.com/botuniverse/onebot/tree/d533f0fca3bd14781d4461776dba8d907d9de253)、[AstrBot](https://github.com/AstrBotDevs/AstrBot/tree/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3)、[MaiBot](https://github.com/Mai-with-u/MaiBot/tree/e5391507531706a8bfce011d55ed94628039f2de)、[MaiBot NapCat Adapter](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/tree/d87203e2eff1835895ee34196b17a571dc6bbe24)。QQ 官方页按 2026-11-09 所见。
- 下面的「未实现原因」凡无维护者明确说明，均标为**推断**。NapCat、Lagrange、QQ 官方的特定能力要以实际部署版本和权限测试为最终依据。

## A1. OneBot v11：公开 API 全量（38 项）

来源是 [公开 API 目录](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md)；每个动作名直接链接到规范行。分类为本文用途分类，不是规范术语。

| 动作（逐项规范出处） | 类别 | 能力 |
| --- | --- | --- |
| [`send_private_msg`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L42) | 发送 | 私聊发送 |
| [`send_group_msg`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L58) | 发送 | 群发送 |
| [`send_msg`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L74) | 发送 | 按目标类型发送 |
| [`delete_msg`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L92) | 管理 | 撤回消息 |
| [`get_msg`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L104) | 查询 | 按 ID 取消息 |
| [`get_forward_msg`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L123) | 查询 | 展开合并转发 |
| [`send_like`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L137) | 互动 | 好友赞 |
| [`set_group_kick`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L150) | 管理 | 踢人 |
| [`set_group_ban`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L164) | 管理 | 单人禁言 |
| [`set_group_anonymous_ban`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L178) | 管理 | 匿名用户禁言 |
| [`set_group_whole_ban`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L195) | 管理 | 全员禁言 |
| [`set_group_admin`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L208) | 管理 | 管理员任免 |
| [`set_group_anonymous`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L222) | 管理 | 群匿名开关 |
| [`set_group_card`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L235) | 管理 | 群名片 |
| [`set_group_name`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L249) | 管理 | 群名称 |
| [`set_group_leave`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L262) | 管理 | 退群/解散 |
| [`set_group_special_title`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L275) | 管理 | 专属头衔 |
| [`set_friend_add_request`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L290) | 管理 | 好友申请审批 |
| [`set_group_add_request`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L304) | 管理 | 加群/邀请审批 |
| [`get_login_info`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L319) | 查询 | 自身资料 |
| [`get_stranger_info`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L332) | 查询 | 陌生人资料 |
| [`get_friend_list`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L350) | 查询 | 好友列表 |
| [`get_group_info`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L366) | 查询 | 群信息 |
| [`get_group_list`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L384) | 查询 | 群列表 |
| [`get_group_member_info`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L394) | 查询 | 单个群成员 |
| [`get_group_member_list`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L424) | 查询 | 群成员列表 |
| [`get_group_honor_info`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L436) | 查询 | 群荣誉 |
| [`get_cookies`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L475) | 系统/凭证 | Cookies |
| [`get_csrf_token`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L489) | 系统/凭证 | CSRF token |
| [`get_credentials`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L501) | 系统/凭证 | 组合凭证 |
| [`get_record`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L518) | 媒体 | 获取/转换语音 |
| [`get_image`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L535) | 媒体 | 获取图片 |
| [`can_send_image`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L549) | 媒体/检测 | 图片可发性 |
| [`can_send_record`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L561) | 媒体/检测 | 语音可发性 |
| [`get_status`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L573) | 系统/查询 | 运行状态 |
| [`get_version_info`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L589) | 系统/查询 | 实现版本 |
| [`set_restart`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L604) | 系统/管理 | 重启实现 |
| [`clean_cache`](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L618) | 系统/管理 | 清缓存 |

- 另有**隐藏** `.handle_quick_operation`，供框架/实现处理事件快速操作，不应作为 ling 的公开能力；[规范](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/hidden.md)。
- `send_group_forward_msg`、`send_private_forward_msg`、`send_poke`、历史消息/群文件等 **不是这 38 个 v11 公开动作**；它们是实现扩展或后续生态约定。[AstrBot 扩展调用](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L181-L200)、[MaiBot 扩展暴露](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/apis/message.py#L383-L431)。

### 事件：规范与扩展分开

- `message.private`：`friend/group/other`；`message.group`：`normal/anonymous/notice`；发送者、消息 ID、段数组见 [消息事件](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/event/message.md)。
- `notice.group_upload`：群文件上传；`group_admin`：`set/unset`；`group_decrease`：`leave/kick/kick_me`；`group_increase`：`approve/invite`；[通知规范上半](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/event/notice.md#L1-L97)。
- `notice.group_ban`：`ban/lift_ban`；`friend_add`；`group_recall`；`friend_recall`；[通知规范中段](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/event/notice.md#L99-L165)。
- `notice.notify`：`poke`（戳一戳）、`lucky_king`（红包运气王）、`honor`（群荣誉）；[通知规范后段](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/event/notice.md#L167-L268)。
- `request.friend`：好友申请；`request.group`：`add/invite`；审批需携带事件 `flag`，[请求规范](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/event/request.md)。
- `meta_event.lifecycle`：`enable/disable/connect`（与传输方式有关）；`heartbeat`：`status/interval`；[元事件规范](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/event/meta.md)。
- 群名变更、群名片变更、精华消息、表情回应、`message_sent` **不在上述 v11 规范清单**；NapCat 生态扩展可见 [MaiBot 通知渲染器](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/notice/renderer.py#L21-L63)。不可承诺所有 v11 实现都上报。

### 消息段和格式

- 标准段：`text`、`face`、`image`（含闪照 `type=flash`）、`record`、`video`、`at`；[段规范前部](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/segment.md#L1-L220)。
- 互动/结构：`rps`、`dice`、`shake`、`poke`、`anonymous`、`share`、`contact`、`location`、`music`、`reply`；[段规范中部](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/segment.md)。
- 转发/富结构：`forward`（收）、`node`（发）、`xml`、`json`；`forward` 需 `get_forward_msg` 展开；[段规范后部](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/segment.md)。
- `markdown`、`tts`、`file`、`mface` 等**不在该 v11 标准段目录**，不能把实现扩展误写成规范。`record` 才是标准语音段，`voice` 是一些内部模型/实现叫法；[规范](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/segment.md)。
- 接收/发送有 CQ 字符串（`[CQ:image,file=...]`）和 `{type,data}` 数组；字符串要转义 `&`、`[`、`]`、逗号，数组避免文本被解释成控制段；[字符串](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/string.md)、[数组](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/array.md)。

### NapCat 与 Lagrange：不要当作同一个「v11 后端」

- NapCat 提供大量 v11 外扩展，MaiBot 的 [API 索引](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/docs/README.md)声明对齐 NapCat 文档 162 个底层 action，并通过命名空间暴露 164 个 API；其依据是 [NapCat 官方 API 站](https://napcat.apifox.cn/)。此数字是该适配器快照自述，不是 OneBot v11 动作数，也不保证每个 action 在 ling 部署中可用。
- 具体差异例：NapCat 的 `send_poke`、`get_group_msg_history`、`send_group_forward_msg` 可见 [MaiBot 的调用/透传](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/services/query_service.py#L318-L350)，而 v11 公开目录没有这些动作。扩展事件 `notify.group_name`、`essence`、`group_msg_emoji_like` 见 [渲染器](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/notice/renderer.py)。
- [Lagrange.Core 当前 README](https://github.com/LagrangeDev/Lagrange.Core/blob/fe789c4a23796fbc82fa09e70a9c44c8a5cf2948/README.md)明确 master 是 V2，Web 服务由 `Lagrange.Milky` 提供 **Milky** 协议；其已退役 V1 在另一分支。未找到当前官方维护的 Lagrange OneBot v11 action 清单，因此**无法确认逐项兼容或断言某动作一定缺失**。若 ling 要兼容具体 Lagrange 部署，先取其版本和 `get_version_info`/动作探针；不能照 NapCat 扩展实现。

## A2. QQ 官方机器人 v2

- QQ 群/单聊使用 OpenID：`POST /v2/groups/{group_openid}/messages` 与 `/v2/users/{user_openid}/messages`；收 `GROUP_AT_MESSAGE_CREATE`、获准的 `GROUP_MESSAGE_CREATE`、`C2C_MESSAGE_CREATE`。[官方概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)、[群接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)、[单聊接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)。
- 出站 `msg_type=0` 文本、`2` Markdown、`7` 富媒体；发送接口还列 `8` 图文卡片，单聊还列 `6` 输入中状态。概述/类型页未同步列 6/8，以更细的接口页为准，但须按账号权限实测。[消息类型](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/overview.html)、[群接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)、[单聊接口](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)。
- 自定义 Markdown 在 2026-04-23 后对群/单聊开放，无须另申模板；**模板按钮需申请、自定义按钮内邀**，不能等同默认可用。按钮 `keyboard.id` 或 `content.rows`（至多 5×5），点击发 `INTERACTION_CREATE`，按钮/菜单回调须确认。[Markdown 文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/markdown.html)、[按钮文档](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html)、[互动事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html)。
- 富媒体：图片/语音/视频/文件先按单聊或群上传得有时效的 `file_info`，再 `msg_type=7`；支持 URL 上传或预上传→分片 PUT→确认→合并；跨场景文件凭证不可复用。[富媒体说明](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html)。
- 被动消息带 `msg_id+msg_seq`，事件回复可带 `event_id`；主动消息不带锚点，受用户开关和场景频控。概述写单聊 60 分钟/4 次、群 5 分钟/5 次；**单聊发送接口字段却写 `msg_id` 5 分钟有效，官方页相互矛盾**。ling 应保守对待并用沙箱/真实账号验证，不能擅自宣称 60 分钟一定可用。[概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)、[单聊字段](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)。
- 网关用 `Identify` 的 intents 位订阅；群/C2C 位 `1<<25`，互动 `1<<26`，频道/私信/反应/审核另有位；`READY`、心跳、`RESUME` 用序号补发。[事件订阅](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)、[WebSocket](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/event-emit/websocket.html)。
- 官方群管理已有成员/群信息、入群审批、成员禁言；批量踢人仍标**内邀**。这些不是 OneBot 的 QQ 号与管理员 API 的一对一映射。[禁言](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_restrict_chat_setting.post.html)、[移除成员](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_batch_remove_members.post.html)、[入群审批](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_approval_join_request_member_openid.post.html)。
- 可撤回**自己发出的**群/单聊消息，超 2 分钟不可撤回；群内撤回他人消息不应按 v11 `delete_msg` 能力推断。[概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)、[群撤回](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages_message_id.delete.html)、[私聊撤回](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages_message_id.delete.html)。
- 频道/guild 是**另一产品面**：频道消息/私信、频道表情表态、子频道精华（置顶）、音频控制都存在；表情表态仅频道，音频控制仅音频类机器人并需权限。[频道发送](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/send.html)、[私信](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/dms.html)、[表态](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/emoji.html)、[精华](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/pins/put_pins_message.html)、[音频](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/audio/audio_control.html)。
- **与 OneBot 差别**：官方独有的 `event_id` 被动响应、intents、按钮回调/Markdown 模板、流式单聊、频道管理/表态/音频；v11 有好友赞、QQ 群匿名/名片/头衔、任免管理员、Cookies/CSRF、合并转发节点等，官方群/C2C v2 无等价接口。这里的「无」限当前所查官方 v2 目录，非整个 QQ 平台不可能实现。[官方接口概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)、[v11 API](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md)、[段](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/segment.md)。

### QQ 官方事件订阅核对表

下表是与接入决策有关的 v2 事件；详细负载以链接页为准。`GROUP_AND_C2C_EVENT` 与频道 intents 不应混用；[官方 intents 目录](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)。

| 事件（官方出处） | 场景/意义 | ling 当前消费 |
| --- | --- | --- |
| [`C2C_MESSAGE_CREATE`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html) | 单聊消息 | 已 |
| [`GROUP_AT_MESSAGE_CREATE`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html) | 群中 @ 机器人 | 已 |
| [`GROUP_MESSAGE_CREATE`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html) | 有权限时全量群消息 | 未 |
| [`FRIEND_ADD`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/friend_add.html) / [`FRIEND_DEL`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/friend_del.html) | 好友关系变化 | 未 |
| [`C2C_MSG_REJECT`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_msg_reject.html) / [`C2C_MSG_RECEIVE`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_msg_receive.html) | 用户关闭/开启主动推送 | 未 |
| [`GROUP_ADD_ROBOT`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_add_robot.html) / [`GROUP_DEL_ROBOT`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_del_robot.html) | 机器人入群/离群 | 未 |
| [`GROUP_MEMBER_ADD`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_add.html) / [`GROUP_MEMBER_REMOVE`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_remove.html) | 群成员变化 | 未 |
| [`GROUP_JOIN_REQUEST`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_join_request.html) | 入群申请 | 未 |
| [`GROUP_MSG_REJECT`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_msg_reject.html) / [`GROUP_MSG_RECEIVE`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_msg_receive.html) | 群通知开关变化 | 未 |
| [`INTERACTION_CREATE`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/interaction_create.html) | 按钮、菜单、反馈、授权等互动 | 未 |
| [`SUBSCRIBE_MESSAGE_STATUS`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/subscribe_message_status.html) | 订阅消息状态 | 未 |
| [频道消息事件](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/event.html) | `AT_MESSAGE_CREATE`、`DIRECT_MESSAGE_CREATE` 等 | 明确拒绝 |

官方 [事件订阅页](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)还列频道创建/更新/删除、成员、消息删除、反应和 `MESSAGE_AUDIT` 等 intents；它们与 QQ 群/C2C 的消息创建事件不是同一订阅面。

## A3. OneBot v12：是接口重构，不是 v11 的小修

- 官方仓库自称**最新草案**，不能写成已取代 v11 的通行正式版；[README](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/README.md)。
- 事件从 `post_type/notice_type` 改为 `id,time,type,detail_type,sub_type,self{platform,user_id}`；消息数组成为核心格式，并有 `alt_message`；[事件结构](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/connect/data-protocol/event.md)。
- 平台抽象有 user/group/guild/file；动作可通过 `get_supported_actions` 发现，不支持动作有明确错误码；消息段改 `mention/mention_all`、`file_id` 媒体引用等；[接口规则](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/interface/index.md)、[消息段](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/interface/message/segments.md)。
- **推断**：ling 目前的 `post_type` 分派与 v11 CQ 段模型不能直接兼容 v12；只有确认有目标实现和用户需要时，才考虑另建 v12 接入器；不能在 v11 解析器里悄悄兼容字段。

| 对照点 | v11 | v12 草案依据与接入影响 |
| --- | --- | --- |
| 事件标识 | 依 `post_type` 和具体 `*_type` | `type/detail_type/sub_type`，并要求独立事件 `id`；[事件结构](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/connect/data-protocol/event.md) |
| 机器人身份 | `self_id` QQ 号 | `self.platform` + `self.user_id`；不能沿用 v11 的去重键拼法；[事件结构](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/connect/data-protocol/event.md) |
| 发送/撤回 | `send_private_msg`、`send_group_msg`、`delete_msg` | 统一 `send_message`、`delete_message`；[动作](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/interface/message/actions.md) |
| 媒体 | 段内 `file` 引用/URL 习惯 | `upload_file`/分片上传产生 `file_id`，段使用该 ID；[文件动作](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/interface/file/actions.md)、[段](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/interface/message/segments.md) |
| @ 全体 | v11 `at` 的 `qq=all` | `mention` 与 `mention_all` 分成两段；[段](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/interface/message/segments.md) |
| 群与频道 | v11 规范主要面向 QQ 群 | v12 有通用 group、guild/channel 两组接口；[group](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/interface/group/actions.md)、[guild](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/interface/guild/actions.md) |
| 动作发现 | v11 无统一能力探针 | `get_supported_actions` 要列已实现动作；[元动作](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/specs/interface/meta/actions.md) |

因此 v12 的价值在跨平台一致性和能力发现，而接入成本至少涉及事件、消息段、媒体与身份四条路径；这是按上述 schema 与 ling 当前实现作的**工程推断**。

## B. 开源实现核对

### AstrBot

- [平台 sources 目录](https://github.com/AstrBotDevs/AstrBot/tree/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources)列 `aiocqhttp`、`qqofficial`、`qqofficial_webhook`、`telegram`、`discord`、`kook`、`satori`、`lark`、`dingtalk`、`line`、`slack`、`mattermost`、`misskey`、`webchat`、`wecom`、`wecom_ai_bot`、`weixin_oc`、`weixin_official_account`。其他渠道不是 OneBot/QQ 官方，本文不把其原生 API 混进对照表。
- OneBot 入站：`message.group/private`、`notice`、`request` 注册；通知/请求转为可供插件看见的原始事件，戳一戳转 `Poke`；未看到对所有通知的专用业务逻辑。[平台适配器](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L62-L197)。
- OneBot 出站：群/私聊发送，`get_msg` 展开引用，`get_group_member_info`/`get_stranger_info` 解析 @，`get_group_info/list` 查询，群/私聊合并转发扩展动作；图片、语音、视频、文件段也转换。[消息事件](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L35-L205)、[入站转换](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L196-L365)。
- OneBot `ActionRow/Button` 降级为链接或“按钮不可用”文字；未在该适配器默认路径见 `delete_msg`、群踢/禁言/审批的产品动作，**不排除插件经原始 `CQHttp` 调用**。[降级源码](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L76-L112)。
- QQ 官方：`GROUP_AT_MESSAGE_CREATE`、`GROUP_MESSAGE_CREATE`、`C2C_MESSAGE_CREATE`、频道 `AT_MESSAGE_CREATE`/`DIRECT_MESSAGE_CREATE` 和 `INTERACTION_CREATE`；文本、引用、附件、Markdown、inline keyboard 回调、群/C2C 上传发送已做。[事件/订阅](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py#L231-L360)、[发送](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/qqofficial/qqofficial_message_event.py#L410-L680)。
- `qq_official_webhook` 是另一接入传输：同样分派群 @/全量群、C2C、频道 @/私信和互动点击，发送复用 QQ 官方适配器公共路径；不能把它当成新的 QQ 协议能力集合。[Webhook 适配器](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/qqofficial_webhook/qo_webhook_adapter.py#L26-L147)。
- 未在所查 QQ 官方适配器路径见群审批/踢人/禁言、撤回、频道精华/表态/音频业务动作；按钮**有实现**，但平台模板/内邀权限仍要另核。[QQ 发送适配器](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py)。

### MaiBot

- 主仓内置适配器查到 [QQ 官方](https://github.com/Mai-with-u/MaiBot/tree/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter)、[Telegram](https://github.com/Mai-with-u/MaiBot/tree/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/telegram_adapter)、[微信 Clawbot](https://github.com/Mai-with-u/MaiBot/tree/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/weixin_clawbot_adapter)；OneBot/NapCat 在独立 [MaiBot-Napcat-Adapter](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/tree/d87203e2eff1835895ee34196b17a571dc6bbe24)，不能只搜 MaiBot 主仓得出“没接 NapCat”。
- NapCat 入站路由处理 `message`、`notice`、`meta_event`；`request` 未分派。通知渲染撤回、戳一戳、群名、禁言、文件、入退群、管理员、精华、表情回应等为文本；元事件用于心跳/状态。[路由](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/runtime/router.py#L65-L88)、[渲染](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/notice/renderer.py#L21-L63)、[心跳](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/heartbeat_monitor.py)。
- NapCat 默认出站有文字/@/回复、图/语音/视频/文件/表情/音乐/转发段；另以可调用 API 暴露 `delete_msg`、`send_poke`、群禁言/踢人/全禁及大量查询/管理动作。**API 暴露 ≠ 麦麦自动决策使用**。[出站编码](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/outbound/segment_encoder.py)、[消息 API](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/apis/message.py#L15-L90)、[群 API](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/apis/group.py#L15-L100)。
- QQ 官方内置只消费群 @/获准全量群/C2C 消息（不消费互动/频道事件），发文本、Markdown @、图片/语音/视频/文件，提供群禁言与撤回工具；未见 inline keyboard 或按钮回调、频道表态/精华/音频。[事件集合](https://github.com/Mai-with-u/MaiBot/blob/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter/codec.py#L1-L19)、[发送](https://github.com/Mai-with-u/MaiBot/blob/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter/plugin.py#L1477-L1545)、[工具](https://github.com/Mai-with-u/MaiBot/blob/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter/plugin.py#L1280-L1465)。

### ling 当前（本地源码）

- [OneBot connector](../connectors/onebot/connector.py)：消费 `message.private/group`；`meta_event` 静默忽略、`message_sent` 仅供回显对账；`notice/request` 不进入对话。发送 `send_private_msg/send_group_msg`，另用 `send_poke` 扩展；查询 `get_msg`/群友历史作对账、`get_group_info` 群名、`get_login_info` 探活。
- [OneBot message](../connectors/onebot/message.py)：CQ 字符串/数组均能解析；入站 text、@、reply、image/voice/video/file、face；**标准 `record` 未处理**。出站 text、image、@、reply；无合并转发、文件/语音/视频输出。
- [QQBot connector](../connectors/qqbot/connector.py)与 [message](../connectors/qqbot/message.py)：仅收 C2C 与群 @；附件 URL 入站可取；仅发文本或 `msg_type=2` 的 Markdown `content`，`build_message_body` 虽接受 `keyboard/media`，实际 `send()` 未传；富媒体出站明确报 unsupported。被动 `msg_id/msg_seq` 已管理；主动需显式配置；频道事件明确拒绝。
- [QQBot gateway](../connectors/qqbot/ws_gateway.py)有 Identify/Resume/心跳；[配置](../connectors/qqbot/settings.py)单聊被动 TTL 取概述的 60 分钟，但发送接口又写 5 分钟，须实测。配置注释称 Markdown 模板必申，与 2026-04 后自定义 Markdown 文档不一致；这属于**文档发现**，本次不改代码。

### 能力矩阵（「AstrBot/MaiBot」按各自对应 QQ 适配器默认路径；MaiBot NapCat 的公开 API 另注）

| 协议能力 | AstrBot | MaiBot | ling | 判读 |
| --- | --- | --- | --- | --- |
| OB11 私聊/群消息收发、@/引用 | 已 | 已 | 已 | 基线；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py)、[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/outbound/segment_encoder.py) |
| OB11 图片/语音/视频/文件双向 | 已 | 已 | 部分 | ling 出站只 image，入站 `record` 漏；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)、[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/inbound/message_codec.py) |
| OB11 合并转发接收/发送 | 部分 | 已 | 未 | AstrBot 发扩展节点，MaiBot 还取转发；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L181-L200)、[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/services/query_service.py#L183-L206) |
| OB11 撤回通知 | 部分 | 已 | 未 | A 保留原事件，M 渲染文本；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L168-L195)、[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/notice/renderer.py#L27-L28) |
| OB11 主动撤回 `delete_msg` | 未 | 部分 | 未 | M 只暴露 API；[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/apis/message.py#L63-L90) |
| OB11 戳一戳入站/出站 | 部分 | 已 | 部分 | ling 已发未收；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L191-L195)、[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/apis/message.py#L15-L62) |
| OB11 入退群/禁言/文件/管理员通知 | 部分 | 已 | 未 | A 原事件可见，M 转文本；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L168-L195)、[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/notice/renderer.py) |
| OB11 好友/群申请与审批 | 部分 | 部分 | 未 | A 原事件；M 有审批 API 但 `request` 不路由；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L143-L165)、[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/runtime/router.py#L65-L88) |
| OB11 群踢/禁言/任免等管理 | 未 | 部分 | 未 | M API 透传，不等于自动治理；[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/apis/group.py) |
| QQ 官方群/C2C 文本与被动锚点 | 已 | 已 | 已 | [A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py)、[M](https://github.com/Mai-with-u/MaiBot/blob/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter/plugin.py) |
| QQ 官方富媒体发送 | 已 | 已 | 未 | ling 仅附件入站；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/qqofficial/qqofficial_message_event.py#L519-L680)、[M](https://github.com/Mai-with-u/MaiBot/blob/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter/plugin.py#L1745-L1805) |
| QQ 官方 Markdown / 键盘点击 | 已 | 部分 | 部分 | M 有 Markdown 无按钮；ling Markdown 正文配置可发但无回调；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py#L291-L299)、[M](https://github.com/Mai-with-u/MaiBot/blob/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter/codec.py#L399-L488) |
| QQ 官方撤回自己消息 | 未 | 已 | 未 | [M](https://github.com/Mai-with-u/MaiBot/blob/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter/plugin.py#L1403-L1465) |
| QQ 官方群禁言 | 未 | 已 | 未 | [M](https://github.com/Mai-with-u/MaiBot/blob/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter/plugin.py#L1280-L1402) |
| QQ 官方频道消息/表态/精华/音频 | 部分 | 未 | 未 | A 消息/私信，未见后三项；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py#L259-L280) |

## C. 差距、原因与决策建议

### 协议有而两个产品默认业务路径都未完整接入的项目

1. **OneBot 好友/群请求审批闭环**：AstrBot 接收原事件但未在适配器直接审批；MaiBot NapCat 暴露审批 action 却不路由 `request`。**推断**：涉及用户关系/群成员准入，不能交给聊天模型自动同意；[规范](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/event/request.md)、[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py)、[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/runtime/router.py)。
2. **OneBot 高风险群管理默认产品动作**：AstrBot adapter 未内建踢人/全禁/任免，MaiBot 仅暴露底层 API。**推断**：权限、误操作与滥用面大；[规范](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md)、[M API](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/apis/group.py)。
3. **OneBot 撤回/群名/名片等通知的结构化状态更新**：AstrBot 多保留原事件，MaiBot 多渲染为自然语言；两者未见“撤回后同步删除模型上下文”通用闭环。**推断**：要解决事件与已存消息关联、审计和回滚；[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L168-L195)、[M](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/codecs/notice/renderer.py)。
4. **QQ 官方群加入审批/成员变动的自动化**：所查两套 QQ 官方适配器都主要消费消息，未见对应 dispatch。**推断**：需要群管理权限、OpenID 绑定与授权策略；[事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_join_request.html)、[审批](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_approval_join_request_member_openid.post.html)、[A](https://github.com/AstrBotDevs/AstrBot/blob/26fdd1ba9a8c8d750afe0dd3fe390e5b66a469b3/astrbot/core/platform/sources/qqofficial/qqofficial_platform_adapter.py#L231-L299)、[M](https://github.com/Mai-with-u/MaiBot/blob/e5391507531706a8bfce011d55ed94628039f2de/src/plugins/built_in/qq_official_adapter/codec.py#L1-L19)。
5. **QQ 官方频道精华/表态/音频**：官方有 API，两个产品 QQ 官方适配器默认路径未见；**推断**：产品焦点是 QQ 群/C2C，频道权限与窗口语义不同，音频还需特别权限。[精华](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/pins/put_pins_message.html)、[表态](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/emoji.html)、[音频](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/content/audio/audio_control.html)。
6. **QQ 官方“输入中”、图文卡片与互动召回**：发送接口/概述已有，两套适配器默认发送路径未见；**推断**：格式新、审核/频控与交互价值需确认，先不为清单完整度接入。[单聊 API](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)、[概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)。
7. **v11 旧式匿名、Cookies/CSRF、重启/清缓存等**：MaiBot 对 NapCat 实际支持的动作可透传，但默认聊天行为不用；AstrBot 适配器也未直接触发。**推断**：匿名能力易随 QQ 客户端变动，凭证/重启动作与对话产品无关且高敏；[规范](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md)、[MaiBot API 目录](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/d87203e2eff1835895ee34196b17a571dc6bbe24/docs/README.md)。

### ling 建议顺序（难度为工程估计；验收均可自动断言）

| 优先 | 能力与用户可感知结果 | 难度 | 新授权/安全约束 | 程序化验收 |
| --- | --- | --- | --- | --- |
| P0 | **修标准 `record` 入站**：语音不再静默丢；[规范](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/message/segment.md) | 小 | 沿用媒体 URL 白名单、大小与超时；音频转写仍按现有策略 | 输入 `record` 段得到 `media_refs`/标记；恶意 URL 被拒且正文保留 |
| P1 | **接撤回通知 + 撤回己方消息**：用户撤回后不再引用旧内容，机器人能撤回错发；[官方限制](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html) | 中 | 仅允许己方消息 ID；撤回他人消息另设管理员授权，默认关闭；保留审计而隔离模型上下文 | `group_recall/friend_recall` 命中 ID 后上下文失效；越权/过期请求不发 action；QQ 官方超过 2 分钟拒绝 |
| P1 | **收戳一戳**：能感知用户轻互动 | 小 | 只对指向本 bot 的 poke 作回应；节流/去重，禁止互拍循环 | `target_id!=self_id` 无回应；同事件只触发一次；现有 `send_poke` 失败不改变正文回执 |
| P1 | **展开合并转发**：能读用户转发的多条内容；[规范动作](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md#L123) | 中 | 限节点数、嵌套深度、字节数；转发原作者仅作引用信息，不当成当前发言人 | `forward.id` 恰调用一次 `get_forward_msg`；循环/超限截断且保留来源；失败显示“未能展开” |
| P1 | **QQ 官方富媒体发送**：图片/语音/视频/文件真正送达 | 大 | 按场景上传，限制格式/大小/来源；`file_info` 不跨场景复用，不把上传成功当消息送达 | mock 断言上传→`msg_type=7`→真实 `id` 回执；任一步失败为未送达/不确定 |
| P2 | **QQ 官方 Markdown + 按钮**：用户能点选项；OneBot 回退为文字/链接；[权限条件](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html) | 中 | 自定义按钮须账号获权；回调验签、时效、用户/群与按钮 ID 绑定，避免重放；Markdown 转义 | 无按钮权限退纯文本；`INTERACTION_CREATE` 确认一次且重放无第二次操作 |
| P2 | **低风险群状态通知**：入退群/禁言/群名变化让 ling 知道环境变化 | 中 | 仅观察，不把通知伪装成用户发言；去重、来源身份校验 | 事件落结构化系统事实；不触发普通回复；缺字段拒绝且不污染会话 |
| P3 | **群管理/申请审批**：仅明确受托群可执行禁言或审批 | 大 | 管理员/群主双重校验、群白名单、动作分级与时长上限；踢人/全禁/任免必须逐项授权和审计 | 未授权群/普通成员请求零 API 调用；授权禁言只作用目标且限时；审批需有效 `flag` |

- **明确暂不做**：v11 Cookies/CSRF/凭证导出、`set_restart`/`clean_cache`、匿名操作、`send_like`/红包运气王驱动行为、频道音频/精华/表态、v12 自动兼容。原因依次为高敏凭证与运维权、平台兼容性弱、对 ling 核心交互价值低、频道是独立产品面、v12 草案且无已确认目标实现；[v11 API](https://github.com/botuniverse/onebot-11/blob/d4456ee706f9ada9c2dfde56a2bcfc69752600e4/api/public.md)、[QQ 频道能力](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/channel/message/send.html)、[v12 README](https://github.com/botuniverse/onebot/blob/d533f0fca3bd14781d4461776dba8d907d9de253/README.md)。
- **决策门槛**：先给接入器逐项声明“协议支持/后端实测/本 bot 获权/产品已授权”四层能力，再开放高风险动作；对 QQ 官方单聊 `msg_id` 5 分钟/60 分钟冲突和按钮内邀状态，须保留独立实测记录。[官方概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)、[单聊 API](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_messages.post.html)、[按钮](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/trans/msg-btn.html)。
