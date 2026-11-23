# 会话列表：置灰 + 按最近消息排序（2026-11-24）

用户要求：「这里的对话，把已退出或者已不在的群聊，或者已删除的好友置灰处理，
并且这个对话的顺序按照时间处理，最新收到消息的对话自动往上排，参考 qq 微信的这种逻辑」。

## 一、为什么排序键从「好感度」改成「最近消息」

原实现（`sort_windows_for_display`）的主排序键是**好感度**，依据是更早的一条
用户原话「可置顶，其他按好感度」（见 `tests/integration/test_session_ordering.py`
的旧 docstring）。本次用户明确要求改成 QQ/微信那种「谁最近来过消息谁在上」。

两者冲突时以后一次为准，所以：

- 排序键变为 `pinned_at`（分组）→ `manual_order` → `pinned_at` 时刻 → **`updated_at`**；
- **好感度不再参与定序**，但仍随记录返回（关系页与展示要用）；
- 管理员的上移/下移仍在（用户没要求取消手动排序）。

被翻转的测试（不是删除，是把断言方向反过来，谁改回去谁红）：

| 用例 | 以前断言 | 现在断言 |
|---|---|---|
| `test_list_is_ordered_by_recent_message_not_by_familiarity` | — | 近期消息在前 |
| `test_familiarity_does_not_override_recency` | 好感度盖过活跃 | 活跃盖过好感度 |
| `test_missing_updated_at_sorts_last` | 未知好感度排最后 | 缺 `updated_at` 排最后 |

后两条之所以成对存在：**单 subject 的接口层用例里好感度是常量**，「按好感度排」
与「按时间排」结果相同，它证明不了任何事。只有让两个键指向相反顺序（两个不同
subject 的窗口），才有区分力。

## 二、置灰的判定从哪来（调研结论）

先说清楚**协议能提供什么、不能提供什么**，因为这决定了要不要加数据库列。

| 事实 | 来源 | 结论 |
|---|---|---|
| `notice.group_decrease` 带 `sub_type=leave/kick/kick_me` 与 `group_id/user_id` | [OneBot v11 notice.md](https://github.com/botuniverse/onebot-11/blob/master/event/notice.md#1) | 机器人自己离开群**会主动推送** |
| `kick_me` 表示「登录号被踢」 | 同上 | 可用于判定"我们不在这个群了" |
| NapCat 实现 `get_friend_list`（整表） | [NapCat API 兼容表](https://napneko.pages.dev/develop/api#%e8%bf%bd%e5%89%a7%e4%b8%ad) | 好友侧可**读侧比对**，不需要事件 |
| v11 通知目录里**没有** `friend_decrease` | 同上 notice.md | 好友被删不会推事件 |
| `get_group_list` 是 v11 动作，但各实现自定义 | `docs/PROTOCOL-CAPABILITY-RESEARCH.md` A1 | 群侧**不能用读侧兜底**：查不到分不清"退群"还是"没采到" |

所以两边走相反的路：

- **群 → 事件 + 落库**。事件只来一次，不存就永远丢了。
- **好友 → 读侧现查**。没有事件，且状态随时会变（加回来、改备注），写库等于
  把一次瞬时采集固化成事实。

改动前 `group_decrease` 在 `_IGNORED_NOTICE_TYPES` 里被**静默丢弃**——这是「置灰」
在代码里根本没有数据来源的根因。

## 三、落点

| 层 | 文件 | 做了什么 |
|---|---|---|
| 契约 | `src/ling/contracts/v1.py` | 新增 `ContactAvailability`（left/kicked/friend_deleted）、`SessionRecord.contact_unavailable`、`ContactStatusEvent`（"不是消息"的类型，与 `RecallEvent` 同构） |
| 存储 | `src/ling/infra/models.py` + `migrations/versions/0090_window_contact_unavailable.py` | `windows.contact_unavailable_reason/at` |
| 连接器 | `connectors/onebot/connector.py` | `_contact_change_from_notice`：只放行 `kick_me` 与"自己 leave"，其余仍归 ignored |
| 内核 | `src/ling/kernel/inbound_intake.py` | `_process_contact_status`：事件级去重 + 写窗口；**不动 `updated_at`** |
| 领域 | `src/ling/domains/conversation/service.py` | `mark_contact_unavailable`；排序键改时间；`to_session_record` 带出状态 |
| 接口 | `src/ling/api/routes/sessions.py` | `_contact_availability`：群读库、私聊现查好友表 |
| 前端 | `SessionSidebar.tsx` / `session-sidebar.css` / `types.ts` | 整行降透明度、徽标换状态词、**仍可点** |

## 四、几条被刻意钉住的判断

1. **别人退群不能灰掉我们的群**。`group_decrease` 在活跃群里每天都有，
   把 `leave/kick`（离开者不是机器人）也记成不可用会让整列变灰。
   测试 `test_connector_ignores_decrease_of_other_members` 覆盖。
2. **不写 `updated_at`**。它是排序键，改它会让刚被踢的群跳到列表最前。
   变异验证：只把 `updated_at` 改成 `utcnow()`，`test_..._without_touching_activity`
   单独红，其余绿——说明这条断言精确覆盖了这个不变量。
3. **空好友表不判「全删了」**。`fetch_friend_names` 采集失败返回空字典，
   据此判定会让一次超时把整列私聊灰掉。`test_friend_list_missing_peer_marks_friend_deleted`
   的三种输入（有别人/空表/有本人）缺一不可。
4. **找不到窗口不是错误**。机器人可能在建窗口之前就被踢；判失败会让平台反复重投。

## 五、验证记录

对象：`git log -1` = `60efdee`，worktree 分支 `session-list-availability-grey`。

| 命令 | 结果 |
|---|---|
| `pytest tests/integration/test_session_contact_availability.py` | 11 passed |
| `pytest tests/integration/test_session_ordering.py test_message_recall.py` | 23 passed |
| `pytest`（onebot/negative/inbound_auxiliary/ordering/contact/display_fields/recall/origin_binding） | 101 passed |
| `pytest tests/unit` | 全绿 |
| `ruff check src connectors tests migrations` | All checks passed |
| `alembic upgrade head` → `downgrade -1` → `upgrade head` | 三次都成功 |
| `vitest run`（web-console） | 1393 passed / 1 failed（`external-plugins.browser` 并发超时，**单跑 12 passed**，与本改动无关） |
| `tsc --noEmit` | 无错误 |

变异验证（固定收尾：恢复 → sha256 比对 → `git status`）：

| 变异 | 预期 | 实际 |
|---|---|---|
| `contact_unavailable_reason=reason[:32]` → `None`（去掉落库） | 写入/幂等/列表三条红 | 三条红，sha 恢复一致 |
| `updated_at=Window.updated_at` → `utcnow()` | 只有"不该改活跃时间"红 | 正是这一条红，其余绿 |
| 前端去掉 `session--unavailable` 类 | 灰行断言红 | 该条红，sha 恢复一致 |

## 六、没做的事（写清楚，避免被当成已完成）

- **好友侧的灰色不会持久化**：每次打开列表都现查一次 `get_friend_list`（连接器内
  缓存 10 分钟）。群聊离线不影响它，但机器人离线时这一侧**不会**变灰——因为
  离线时"采集失败"与"被删"不可区分，宁可不变灰也不误判。
- **入站被踢后不会自动把窗口归档**：用户仍能在列表里看到灰行并翻历史，
  这是刻意的（与 QQ/微信一致）。
- 前端只做了静态渲染断言；真浏览器下的观感（透明度、悬停回抬）未截图核对。
