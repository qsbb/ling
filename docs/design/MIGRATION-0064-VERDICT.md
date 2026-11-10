# 0064 人设默认值迁移裁决

## 1. 裁决

**应在升级时执行 0064 的 `upgrade()`。** 旧版建行把三份默认值直接复制进 `personas`，新版只对空值做读取时回落；不清旧副本，旧提示词中的两句身份立场仍会生效，这次默认值修订对已有的未编辑行等于没有发生。【事实：`HEAD:src/ling/domains/persona/service.py:20-42,65-86`；`src/ling/domains/persona/service.py:40-62,93-123,218-233`】旧值在迁移里硬编码，评审时已与 `HEAD` 中改动前的三个 `DEFAULT_*` 常量逐项比对，结果均相等。【事实：`migrations/versions/0064_persona_defaults_fallback.py:93-120`；`HEAD:src/ling/domains/persona/service.py:20-42`】

这是**正常发布迁移**，不是把生产库当开发或测试环境。镜像模式默认跳过启动时迁移，发布流程才显式执行一次。【事实：`deploy/compose.image.yaml:23-25,189-192`；`deploy/entrypoint.sh:37-43`】裁决只授权该受控升级路径；开发、验收不得连接生产库。三条 UPDATE 以 `revision = 1` 加各字段旧值判等限制影响范围，属于可以接受的前向迁移，但它不能证明每个命中的字段都从未被人手工编辑过，也不能修复所有旧行。【事实：`migrations/versions/0064_persona_defaults_fallback.py:134-158`；推断见下文】

## 2. 判据安全性审查

- **正常写入路径。【事实】** `ensure_default()` 仅在行不存在时建行，设置 `revision = 1`，已有行不改；目前的生产源码中，`Persona` 的建行调用点只有这里，其他生产代码读取该表或使用 `PersonaService`。【`src/ling/domains/persona/service.py:155-172`；`src/ling/infra/models.py:137-153`；`src/ling/app.py:83-89`】`PUT /v1/persona` 调用 `PersonaService.update()`；无论提交哪些字段，正常初值 1 都会加到 **2**，即使传入值与原值相同。【`src/ling/api/routes/actor.py:63-70,104-129`；`src/ling/domains/persona/service.py:181-211`】因此，按当前服务/API 写入的、从 1 开始的已保存人设不会被 0064 命中。【推断】
- **没打开过页面的行。【事实与推断】** API 启动引导就调用 `ensure_default()`，不依赖用户打开页面。若该行在旧版 API 首次启动时创建且从未保存，推断为 `revision = 1`、三个字段是旧默认副本，0064 应清三列；若此前根本未建行，迁移无行可改，新版首次建行会存 `''`、`{}`、`[]`。【`src/ling/app.py:83-89,493-495`；`src/ling/domains/persona/service.py:155-171`；`migrations/versions/0064_persona_defaults_fallback.py:95-120,134-158`】
- **`revision = 1` 不是不可伪造的“未编辑证明”。【事实与推断】** 表结构只要求 `revision INTEGER NOT NULL`，没有 `>= 1` 或“每次外部写入必须递增”的数据库约束；直接 SQL、其他程序或未来新增写入路径能在保留 1 的情况下改字段。若手工把内容写成**恰好等于**旧默认，0064 无法区分这次真实编辑与原始副本，会清掉它；只要内容有差异，该字段就不匹配，但其他仍等于旧默认的字段会独立被清。【`migrations/versions/0001_baseline.py:400-413`；`migrations/versions/0064_persona_defaults_fallback.py:134-158`；`tests/integration/test_migrations.py:810-866`】异常的 `revision = 0` 行也可由无约束的外部写入产生，而当前 `update()` 会把它加成 1；这不是正常建行路径，但说明“任何 API 编辑都不可能留下 1”不能无条件成立。【`migrations/versions/0001_baseline.py:402-403`；`src/ling/domains/persona/service.py:208-210`】
- **漏清比误清更现实。【事实与推断】** 迁移故意跳过所有 `revision > 1`，即使那三列仍是旧默认；正常 API 的部分字段 PUT 也会把整行修订号加到 2，因此只改显示名或场景后保留的旧提示词不会收到此次修订。【`migrations/versions/0064_persona_defaults_fallback.py:134-158`；`src/ling/domains/persona/service.py:195-208`；`src/ling/api/routes/actor.py:63-70,123-129`】更重要的是，人设页 GET 到的是回落后的**生效值**，保存时把提示词、traits、boundaries 全部随表单送回；升级后只改显示名再保存，会把当时的默认值重新冻结在库里。这个前端行为需要单独修复，0064 无法一劳永逸。【`src/ling/domains/persona/service.py:218-233`；`clients/web-console/src/components/PersonaPage.tsx:186-196,209-238`；`clients/web-console/src/api.ts:849-853`】
- **JSONB 判等。【事实与推断】** `traits`、`boundaries` 在表中是 `JSON NOT NULL`，迁移转成 `jsonb` 再与硬编码 JSON 比较。对象键序及 Unicode 转义写法不会造成漏清；`0.6` 与 JSON 数字 `0.60` 在数值意义上相等，虽然原始文本不同；数组顺序仍有意义，值或键有真实差异则不应匹配。故这里是**JSON 结构/数值相等**，不是注释所说的“逐字节精确相等”；`system_prompt` 才是文本判等。【`migrations/versions/0001_baseline.py:405-408`；`migrations/versions/0064_persona_defaults_fallback.py:126-156`】

## 3. 替代方案对比

只改 `ensure_default()` 与 `_to_view()` 不足以修正已有行：前者不改已有行，后者仅对空值回落，旧副本是非空值。【事实：`src/ling/domains/persona/service.py:93-123,131-172,218-233`】可以在 `_to_view()` 额外检测“内容等于旧默认”并回落，但这会在每次读取时永久保留历史旧值识别逻辑，且遇到用户有意保存完全相同文本时也会覆盖其选择；若不加 `revision` 条件，误判范围还会扩大。【推断：`src/ling/domains/persona/service.py:218-233`；`migrations/versions/0064_persona_defaults_fallback.py:93-123,134-158`】迁移一次性清理可识别的原始副本更合适；已编辑行是否要跟随新默认，须另有明确的用户意图标记，不能凭旧文本猜测。【推断：`src/ling/domains/persona/service.py:195-208`；`migrations/versions/0064_persona_defaults_fallback.py:134-158`】

## 4. 迁移质量问题

- **升级语句的关系。【事实】** 三条 UPDATE 分列匹配与清理，没有前一列结果参与后一列的 WHERE，故没有语义顺序依赖；Alembic 在线迁移在一个事务块里运行。每条匹配行都会被 PostgreSQL UPDATE 锁住，跨语句的外部并发写入仍应通过发布时停止写流量来规避。【`migrations/versions/0064_persona_defaults_fallback.py:134-158,186-192`；`migrations/env.py:49-59`】
- **`downgrade()` 不是真正逆操作，存在误填风险。【事实与推断】** 它把所有 `revision = 1` 且为空的字段改回旧默认，无法判断空值是 0064 清掉的、升级后新版创建的，还是外部直接清空的。正常 API 清空会使 revision 增至 2，因而不会被这个回滚命中；但新版初建行仍为 1，确会被误填。现有回滚测试甚至把“本来就空”的行写回旧默认当作预期，未验证数据可逆性。【`migrations/versions/0064_persona_defaults_fallback.py:161-200`；`src/ling/domains/persona/service.py:155-171,195-208`；`tests/integration/test_migrations.py:886-949`】若要求安全回滚，应先有逐行逐字段的变更记录/恢复点；否则应明示此数据迁移不可安全逆转，并用备份恢复，而非把当前 downgrade 当无损回滚。【推断】
- **缺表早退过宽。【事实与推断】** `upgrade()` / `downgrade()` 仅检查表存在；正常迁移链的 baseline 已建 `personas`，所以到 0064 仍缺表意味着非标准或损坏状态。静默返回会让 Alembic 继续标记该版本已执行，后补表时不会自动重跑 0064；建议失败并报告环境异常。即使表存在，目前也没有列存在性预检，但缺列会由 SQL 失败并阻断迁移。【`migrations/versions/0001_baseline.py:400-413`；`migrations/versions/0064_persona_defaults_fallback.py:186-200`；`migrations/env.py:49-52`】
- **可观测性。【事实与建议】** 当前执行结果未记录各语句 `rowcount`。建议迁移前在**非生产测试库**验证匹配数，发布时只记录三列各自影响行数及版本号，不记录提示词或私有内容；这有助于发现旧文案不匹配导致的意外零行，也能审计真实影响范围。【`migrations/versions/0064_persona_defaults_fallback.py:186-192`；`deploy/compose.image.yaml:189-192`】

## 5. 验收清单

以下断言只能在指定的 `postgresql://ling@127.0.0.1:55432/ling_test` 上执行。本次是静态评审，**未运行数据库测试**。仓库现有 `test_migrations.py` fixture 实际会另建 `ling_mig_<uuid>` 数据库，不符合本次“实验只能用 `ling_test`”的限定，不能原样运行；执行验收前须使用只连 `ling_test` 的测试方式，绝不能把生产 DSN 传给 fixture。【事实：`tests/integration/test_migrations.py:16-17,43-49,266-283`】

1. **旧值快照与迁移链：** 断言 0064 硬编码的 prompt、traits、boundaries 与改动前源码常量一致，不 import 当前默认；`down_revision` 是 0063，升级后版本是 0064。【`migrations/versions/0064_persona_defaults_fallback.py:87-120`；`tests/integration/test_migrations.py:735-769,839-840`】
2. **升级命中：** 插入 `revision=1` 且三列为旧默认的行，断言原始列变成 `''`、`{}`、`[]`，读取视图变成**新**默认提示词且事实纪律仍在、两句身份立场不在；再执行一次升级语句，断言数据不变。【`migrations/versions/0064_persona_defaults_fallback.py:134-158`；`src/ling/domains/persona/service.py:40-62,218-233`】
3. **保护用户值：** `revision=2` 且三列恰为旧默认的行完全不动；`revision=1` 但 prompt 差一个空格/字符、traits 多一键、boundaries 改一项或换序时，各自不匹配的字段原样保留，其他匹配字段按规则独立清空。【`migrations/versions/0064_persona_defaults_fallback.py:134-158`；`tests/integration/test_migrations.py:803-866`】
4. **JSONB 边界：** 测键序反转、Unicode 转义、`0.60` 与 `0.6` 的语义相等；测实际不同的数字、键、数组内容或数组顺序不相等，并记录“相等非字节相等”的预期。【`migrations/versions/0064_persona_defaults_fallback.py:126-156`】
5. **写入契约与未打开页面：** 旧版引导建行后不打开页面即迁移，断言清理发生；从 revision 1 经 `PUT /v1/persona` 保存任一字段，断言 revision 2，迁移不动该行；新版新建行三列为空而 GET 返回新默认。【`src/ling/app.py:83-89,493-495`；`src/ling/api/routes/actor.py:104-129`；`src/ling/domains/persona/service.py:155-171,195-211,218-233`】
6. **回滚风险必须显式验出：** 造一行“升级后新版创建、revision 1、三列本来为空”的行，执行 downgrade，断言当前实现会把它误填为旧默认；在修正回滚方案前，不应把现有 `test_0064_downgrade_restores_old_defaults` 当作无损回滚证明。【`migrations/versions/0064_persona_defaults_fallback.py:161-200`；`tests/integration/test_migrations.py:886-949`】
7. **前端防再次冻结：** 升级后只改显示名或场景并保存，断言未编辑的三列原始存储仍为空、下一次改 `DEFAULT_*` 后 GET 能跟随；按当前前端的全量 PUT，此项预计失败，应作为后续修复的验收条件。【`clients/web-console/src/components/PersonaPage.tsx:186-196,209-238`；`src/ling/domains/persona/service.py:195-208,218-233`】
