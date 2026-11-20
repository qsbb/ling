# WP-00 重构前行为基准

## 观测对象

2026-11-20，从干净工作树记录 `git status --short`（无输出）。
`git log -1`：`3197b0df1e60b63aab0133f2fad317fd4469c147 chore(release): 准备 Ling 0.0.12`。
`alembic heads`：`0079_manual_names_and_order (head)`，唯一 head。

基线文件 SHA-256：

```text
9d6a3bdd256e929ef9f010314daf57e3b086246f1369d646f7a8875809a729a2  tests/integration/test_output_equivalence.py
5dc4f7649aa2841e1fcce3447f776c201937f11d9cb922c16b1deddf6505d96f  tests/integration/test_deferred_reply_intent.py
ca0db03e0e2ffbbaf7b020b661e66c5e69450c7e459caca1c9562ac768bf788c  tests/integration/test_w0_web.py
6cf97c636c3406ad4da796ed58a04a2c8d6768e6d08510c38c6160290dda2db4  tests/integration/conftest.py
a55e802dc2c030a2099d22bd57692e78ae3a68223dccf9170dfc9ed752341a1f  src/ling/kernel/context.py
83b5805395c2b5e9687970248367a59f3ed2b9ab4fef9f3f79311e5f37aa7519  src/ling/api/routes/chat.py
```

## 基线运行

- Python 3.13、隔离 PostgreSQL，`pytest --collect-only -q` 默认收集 6,701 条。
- `test_output_equivalence.py`、`test_deferred_reply_intent.py`、`test_w0_web.py`：39 passed in 41.17s。
- 同一提交的 [发布流水线](https://github.com/qsbb/ling/actions/runs/44674276456) 成功；
  2026-11-20 04:25:15–04:56:54 UTC，共 31 分 39 秒。Python 3.12 作业 27 分 57 秒，
  Python 3.13 作业 26 分 50 秒，Web 作业 2 分 10 秒，移动端作业 39 秒。
  该流水线只证明原提交通过 CI，不代表加入 WP-00 后的阶段门禁已通过。

## WP-00 回归基准

- **提示词**：Web 私聊、群窗、sealed、continuous、含工具、含视频标记。
  逐条比较有序的 `role` / `content`，不比较模型生成结果。工具夹具执行实际工具轮，
  保存续步的工具名、参数与供应商配对 ID；
  群窗、隔离模式和视频夹具都有「应该出现」与「不应出现」的正反样本。
- **SSE**：实际 HTTP 连接上的发送、幂等重放、生成中停止、同轮重试及延后回复。
  逐事件比较名称、字段与类型，另验首段在第二段放行前到达，延后任务未到期不发、到期后持久化。
- **客户端 API**：`tests/regression/client_routes.json` 固定 Web 的 102 个调用点、101 个不同
  方法/路径，Flutter 的 9 个方法/路径都在其中。对照方案 §12.4 的匹配思路，另外人工核对
  动态接入器路径、语音媒体地址和前端调用点；不把未调用的旧 `updatePersona` 包装器计入。
  127 份实测样本覆盖 98 个非流式工作流的方法/路径，剩余的发送、重试、停止由 SSE 用例覆盖。
  JSON 主体只记固定键与类型；动态字典保留值的形状，允许任意 JSON 的字段在任意深度均不保留键名。
  日志响应夹具验证任意键与样本值出现在 HTTP JSON 中，但不进入基准。二进制只验媒体类型和非空，
  带 Range 请求另验状态码。样本不代表服务端所有可选字段、错误或状态分支已经穷尽。
- **导入守卫**：AST 识别常规绝对/相对导入和函数内导入。白名单锁定 47 处存量违规（文件、
  所在符号、目标符号及出现次数）；新增、额外重复或白名单未随违规消失而清理都会失败。
  不检查动态导入，也不能证明配置仅被只读使用或跨领域调用属于公开服务。

已知现状：Web 的接入器实例删除、恢复默认请求不带 `Content-Type`，实际响应为 `422`；
同一 API 以 JSON 类型头发起的成功响应另有快照。这里记录两种行为，不改现有接口或客户端。

正常运行不写金标准。设置本地 PostgreSQL 的 `LING_TEST_DATABASE_URL`、`LING_DATABASE_URL`
及 `LING_TEST_MODE=1`，按项目约定持测试锁后，可用
`python -m pytest tests/regression tests/unit/test_import_boundaries.py`
复验。仅在有意更新已审查契约时用 `LING_RECORD_WP00=1` 加上要录制的具体用例；之后必须取消该变量、
审查 JSON 差异并重跑。不要用录制覆盖意外失败。

## 候选验证

2026-11-20 22:30:27（UTC+8）冻结候选：HEAD 仍为 `3197b0df1e60`，`git status --short`
只有本页、`tests/regression/`、`tests/unit/import_boundary_allowlist.json` 和
`tests/unit/test_import_boundaries.py` 四条新增路径。32 个新增测试及夹具在全量运行结束后
逐文件核对 SHA-256 一致。按路径排序的 `shasum -a 256` 清单摘要为
`3ff76fa97a85cac660ea9a098bdd0a9f8822839b25196285d6969c78e01e61b1`。

| 本地检查 | 结果 |
| --- | --- |
| Python 3.13 + 隔离 PostgreSQL，默认标记的并行主套件 | 6,699 通过、14 跳过、3 个已知 `xfail`，8 分 41 秒 |
| Python 3.13 登录专项（单独运行） | 7 通过 |
| Python 3.12 和 3.13，WP-00 回归及导入守卫 | 各 22 通过，未开启录制 |
| Ruff、mypy、可信发布目录校验 | 通过 |
| Web | 类型检查、构建通过；1,061 通过、1 跳过 |
| Flutter | 静态分析未发现问题；3 通过 |

未验证真实模型。Web 检查使用已有依赖；本地 `pnpm` 在类型检查前尝试重装时报告
`esbuild` 构建脚本未获批准，随后 TypeScript 检查通过，不作为干净依赖安装的证据。

### 独立变异验收

独立验收方在隔离副本预先写好判据、确认基准通过后，逐一施加下列变异：

| 变异 | 观察到的失败 |
| --- | --- |
| 改记忆提示词车道标题；分别删除原生工具回放的调用和参数 | `prompt-web-private` 或 `prompt-native-tool` 金标准不符 |
| 删除 `turn.completed.chunks` | `sse-send` 金标准不符 |
| 删除仪表盘响应字段；仅清理日志响应的第一层动态键 | `api-account` 金标准不符；日志用例捕获残留的嵌套键 |
| 在 `domains/` 新加 `import ling.api`；移除一处旧导入但不更新白名单 | 守卫分别报 `new` 和 `stale` |

每次都在指定断言失败；恢复后核对哈希并复跑对应基准。结果说明守卫能抓到这些变异，
不代表未采样的客户端分支或真实供应商已经验证。

阶段 0 → 1：`73a89082bbce557d9ca705fe64699fd139ae5e12` 推送 `main` 触发的
[GitHub CI](https://github.com/qsbb/ling/actions/runs/44759566313) 于 2026-11-20 15:48:04 UTC
成功完成。Python 3.12、Python 3.13 和 Web 作业均通过，移动端未改动且不属于这次推送触发的工作流。
