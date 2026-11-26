# 给后端开发的提示词：把记忆范围扩到群聊，并保留可见范围

你在 Ling 仓库工作。目标：让“所有记忆”包含本人可核对的私聊与群聊长期记忆，同时保留每条记忆原来的可见范围。不要把它做成前端筛选；导入链路现在从转换到写入都排除了群聊。

## 现状（已核实）

- `scripts/export_astrbot_memories.py` 的 `_SELECT` 写死：
  `scope = 'private' AND group_id = '' AND visibility = 'private_pair'`
  以及 `session_id` 只匹配 friend/private。群聊记录不会进入 Ling JSONL。
- `src/ling/domains/memory/astrbot_import.py`：
  - `_PRIVATE_SESSIONS` 只允许 friend/private；
  - `ArchiveRecord.belongs_to` 要求 `scope='private'`、`not group_id`、`visibility='private_pair'`、`subject_kind='user'`、`subject_id=owner_qq`、object 是 owner/bot；
  - 群聊记录会被判 `outside_private_pair`。
- `_insert_memories` 固定写入：
  `"visibility": Visibility.subject_private.value`、
  `"source_visibility": Visibility.subject_private.value`。
  即使放宽过滤，群聊的 `group_scope` 也会被写成“仅本人”。
- 真实导出：45,347 条 memory，private_pair 516，group_public 41,360；稳定记忆私聊 398、群聊 30,159。
- 当前后端预览 398 条私聊只接受 118：266 `outside_private_pair`、13 `invalid_record`、1 `unsupported_memory`。

## 必须实现

### 1. 转换脚本

- 新增范围选项（默认仍只导私聊，避免行为突变）：
  `--include-group`（或 `--scope private|group|all`）。
- 群聊选择条件至少包含：
  - `platform in ('aiocqhttp','onebot','qq')`
  - `session_id` 是 `GroupMessage:<group_id>`（或插件实际使用的群会话格式）
  - `scope='group'`、`group_id` 非空
  - `visibility='group_public'`（或实际枚举）
  - `owner_bot_id = bot_qq`
  - 只导本人参与/本人相关的摘要或陈述，不导群友私人资料
- `subject` 可以为 bot（群聊摘要的 subject 常是 bot），`object` 是群；不要沿用私聊的 subject=owner 约束。
- header 仍是 `ling-astrbot-memory-jsonl` v1；如需新字段，加版本并保持旧文件可读。

### 2. 导入器

- `ArchiveRecord.belongs_to` 拆成 `belongs_to_private` / `belongs_to_group`：
  - 私聊保持现有严格规则；
  - 群聊要求 session/group/bot/visibility 匹配，且记录确实来自该 bot 的群。
- `skip_reason` 不再用 `outside_private_pair` 表示群聊；新增 `outside_known_scope` 或复用但改名，并在 `skip_reasons` 里区分私聊/群聊。
- 写入时按来源设置可见范围：
  - 私聊 → `subject_private`
  - 群聊摘要 → `group_scope`，`source_visibility=group_scope`
  - 若原记录是 `window_private`，保留 `window_private`。
- `import_source` 增加 `scope`、`group_id`、`session_id`、`visibility`，前端才能显示“群聊”。
- 幂等键仍按 `subject_id + bot_qq + owner_qq + source_id`，重复导入不重复。
- 群聊不能因为可见范围是 `group_scope` 就放宽编辑权限；`can_edit` 仍按当前 subject/owner 规则。

### 3. 预览与计数

- `POST /v1/memory/import/preview` 返回按 scope 的计数：
  `scope_counts: {private, group}`，以及 `skip_reasons`。
- `sample` 要包含群聊样例，并能显示群名/群号（不要泄露群友隐私）。
- `GET /v1/memory?view=library` 的 `include` 过滤仍生效；群聊记忆默认可见，但前端可按“全部/仅私聊/仅群聊”筛。

### 4. 真实验收

用 `/Users/lingxi/Downloads/memory-companion-20261121-013110.jsonl`：

1. 转换 `--include-group`，生成文件应同时含私聊和群聊。
2. 预览：群聊记录不再全部 `outside_private_pair`；给出 accepted/skipped 与 scope_counts。
3. 提交后查库：群聊行 `visibility=group_scope`、`source_visibility=group_scope`，`import_source.scope='group'`，`group_id` 保留。
4. 重复提交同一文件：`imported=0` 或只新增未导入项，不产生重复。
5. 前端 `/memory` 卡片能标“群聊”，筛选“仅群聊”只显示群聊；详情/纠正/撤回仍可用。

## 不要做

- 不要只放宽 `skip_reason` 却仍写 `subject_private`。
- 不要把群聊内容标成“仅本人”。
- 不要用同地点/同群/同主题自动生成强关联；强关联只来自共享具体事件或纠正链。
- 不要为了“所有记忆”导入群友的私人资料或与本人无关的群聊噪声。

## 交付

- 后端改动 + 迁移（如新增 `scope`/`group_id` 字段）。
- 测试：私聊回归、群聊 preview/commit、可见范围、幂等、权限。
- 真实导出的 preview/commit 数字与查库结果。
- 给前端的字段说明：`visibility`、`import_source.scope/group_id/session_id`。
