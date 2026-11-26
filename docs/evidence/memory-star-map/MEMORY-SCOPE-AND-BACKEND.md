# 记忆范围与后端扩展方案

## 一、范围定义

“所有记忆”指本人可核对的长期记忆，不是只有私聊：

| 维度 | 收 | 不收 |
| --- | --- | --- |
| 会话 | 私聊、群聊中的本人可核对记忆/摘要 | 群友的私人资料、与本人无关的群聊噪声 |
| 来源 | AstrBot Memory Companion 原生导出（含群聊摘要） | embedding、注入日志、凭据（导出已排除） |
| 时间 | 有发生时间；没有发生时间的导入条目单独成列 | 拿记下时间冒充发生时间 |
| 关系 | 共享具体事件、纠正链、同一天/相邻三天 | 同地点、同主题、同群本身不等于同一件事 |
| 可见范围 | 保留原范围：私聊 private_pair、群聊 group_scope | 把群聊内容写成“仅本人” |

## 二、当前真实数据的事实

本机 `ling` 库 25 条全部已撤回；`ling_memimport` 是导入夹具；真正可用的用户导出是：

`/Users/lingxi/Downloads/memory-companion-20261121-013110.jsonl`

- 45,347 条 memory，其中 `private_pair` 516 条、`group_public` 41,360 条。
- 稳定记忆（`lifecycle=stable_memory`）：私聊 398 条、群聊 30,159 条。
- 用当前后端导入器预览 398 条私聊：**118 accepted**，266 `outside_private_pair`，13 `invalid_record`，1 `unsupported_memory`。
- 118 条真实记忆里：8 月 52、7 月 47、11 月 18、9 月 1；**共享事件 0、纠正链 0**。所以真实数据下强关联为 0，不能用“同月”硬造强关联。
- 同一天最多 9–13 条；整月全连没有区分度，所以中关联收窄为“同一天 + 最近两条相邻三天”。

## 三、前端已经落地的部分

1. 记忆范围筛选：全部 / 仅私聊 / 仅群聊（`filters.originScope`）。
2. 卡片显示会话范围：群聊/私聊，不再把群聊伪装成私聊。
3. 放大镜：选中卡片后自动连出强/中关联，弱关联淡出；镜片固定在可见视口右侧，避免盖住关联弧线。
4. 镜片顺序：标题 → 时间 → 关联（最多 2 条）→ 折叠原文 → 看完整详情。默认不展开长摘要。
5. 详情默认收起；点卡片只开镜片，“看完整详情”才进入既有详情、纠正、撤回。

## 四、后端必须改的点（提示词给后端）

见 `BACKEND-MEMORY-ALL-SCOPES.md`。核心不是“加个筛选”，而是导入链路从转换到写入都要保留范围：

1. `export_astrbot_memories.py` 的 SQL 目前写死 `scope='private' AND visibility='private_pair'`，群聊根本进不了 Ling 格式。
2. `astrbot_import.py` 的 `belongs_to` 只接受私聊会话、`private_pair`，群聊会被判 `outside_private_pair`。
3. `_insert_memories` 写入固定 `Visibility.subject_private` / `source_visibility=subject_private`，群聊的 `group_scope` 会丢失。
4. 需要保留：群名/群号、会话 id、原可见范围、发生时间；前端已有 `group_scope` 标签，但后端必须真的写这个值。
5. 需要区分“群聊摘要可读”和“群友隐私”：只导入本人参与/本人相关且可核对的摘要，不能把整群聊天都当成本人记忆。

## 五、本轮真实预览的边界

- 后端当前只接受私聊：398 条私聊样本预览出 **118 accepted**，群聊全部被判 `outside_private_pair`。
- 因此本轮前端真实预览只有 118 条私聊；群聊卡片/筛选已经做好，但要等后端扩展才能看到群聊数据。
- 不能声称“群聊已导入”。前端已按真实字段（`visibility=group_scope`、`import_source.session_id`）显示“群聊”，等后端写入后即可生效。

## 六、验收判据

- 私聊样本：398 → 118 accepted（当前），扩展后群聊样本也应进入 preview，`outside_private_pair` 不再吞掉群聊。
- 群聊记录写入后 `visibility=group_scope`、`source_visibility=group_scope`，前端卡片标“群聊”。
- 同一份导出重复导入不产生重复（幂等键仍按 source record id）。
- 没有任何共享事件/纠正链时，强关联为 0；不能把同月全量标成强关联。
- 详情/纠正/撤回仍可用；群聊记忆不因可见范围变宽而获得编辑权限。
