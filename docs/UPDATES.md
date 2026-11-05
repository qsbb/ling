# 升级与自动更新（默认关闭）

> **两条通道，先分清自己在哪条上**
>
> - **源码模式**（`install.sh` 构建当前源码）：§1–§9 描述的就是这条，宿主执行器是
>   `deploy/update/executor.py`。
> - **镜像模式**（固定在 OCI 摘要上的镜像）：§10。API 端点是
>   `POST /v1/version/image-update-requests` / `GET /v1/version/image-update`，
>   宿主侧是独立项目 `ling-updater`。**`executor.py` 不能用于镜像模式**——
>   它下载 tag 源码、构建再调 `install.sh`，跑的是另一套拓扑（见 §4 开头）。

> **先看结论**
>
> - `deploy/install.sh` 只会构建并部署**当前这份源码**。它不会 `git pull`，也不会
>   从 GitHub 下载新版本。它是**源码模式专用**的安装/升级入口，镜像模式的安装与
>   升级见 [DEPLOYMENT.md §11](DEPLOYMENT.md#11-镜像模式部署推荐) 与本文 §10。
> - `v0.0.1` **已经发布**（`docs/IMAGE-DEPLOYMENT-DESIGN.md` §1）。所以“检查更新”
>   现在读到的是它，而不是 `no_release`；仓库当前版本也是 `0.0.1`，远端不比当前
>   新，`check-release` 因此给 `is_newer=false`、`update_eligible=false`
>   （原因码 `not_newer`），提交更新会被拒。
> - 自动更新默认关闭（`LING_UPDATE_ENABLED=0`），**关闭时这套改动不影响已经跑着的
>   服务**：通道目录照常挂载（compose 早就接线了，见 §2），但开关是 0，界面上不会
>   出现可点的“更新”入口；`GET /v1/version/update` 仍会读 status 里的心跳来回答
>   `executor.online`，只是 `reason=disabled`；不会有任何路径去写 requests 或触发
>   部署。现有部署不需要为重装/重启，升级仍走人工路径（见 §7）。
> - 第一次升级必须由人**人工纳管**：把新源码取到宿主机、确认配置与备份、再跑安装器。
> - **演练范围要按位置分开说**：源码模式的完整升级/回滚**尚未**在真实 Docker 上
>   演练；镜像模式已在**隔离项目 `ling-drill`**（端口 6521、独立卷、独立根密钥）
>   上真机跑通新装、备份恢复与三次连续无人值守升级。生产 `ling` **已于 2026-11-05
>   从源码模式迁移到镜像模式**（首装失败过、手工补齐后用 `ling adopt --force` 登记，
>   见 [DEPLOYMENT.md §6](DEPLOYMENT.md#6-验证状态哪些验过哪些没验)），但那是一次
>   人工迁移，不是演练：**两次连续网页升级**（只发布过一个摘要 `42bba216`，缺第二个
>   digest）、head 不一致 / 并发请求 / 旧执行器残留的故障注入、中断中任务的对账
>   （仅单测）、生产库有真实数据时的恢复（生产库 `messages=0`/`memories=0`）、
>   arm64 运行、真实浏览器端到端点一遍**仍未做**（见 §10.8）。
>   本文描述的接线与契约不等于“已验证的无人值守升级”。
> - API 容器只读、非 root、**没有 Docker socket**；它只能写一条固定 JSON 请求，
>   真正部署由宿主上的独立执行器完成（两条通道都遵守这条边界）。

## 1. 源码模式的管线总览

> 本节（以及 §2–§9）只描述**源码模式**通道：宿主执行器下载 tag 源码、在宿主构建、
> 再调用 `install.sh`。镜像模式的管线完全不同（API 写固定 digest 请求 → 独立
> `ling-updater` 拉镜像并切换），见 §10。

```
Web ──POST /v1/version/update-requests──▶ API（容器内 uid 10001）
                                          │ 原子写 pending.json（flock 单飞）
                                          ▼
                          /run/ling-update/requests   (rw，只有 api 挂)
                                          │
                          宿主执行器 deploy/update/executor.py
                                          ▼
                          /run/ling-update/status     (ro，只有 api 挂)
                           ├─ heartbeat.json  心跳 + 实际运行 commit/version
                           └─ current.json    最近/当前任务（固定脱敏字段）
                                          ▲
Web ◀──GET /v1/version/update──────────── API（只读 status）
```

- **没有 Docker socket**：API 容器不接触 Docker；执行器是宿主机上的独立进程。
- **worker 不参与**：`worker` 不挂 requests/status，也不拿 `LING_UPDATE_*` 环境变量
  （镜像模式同样如此，见 `deploy/compose.image.yaml` 的注释）。
- **Web 不接触宿主源码树**：前端只从只读 `/app/web` 挂载取构建产物（源码模式）；
  镜像模式里 Web 产物已烤进镜像，没有这个 bind。
- **源码 tag/commit 就是更新源**：Release 只打 tag、生成说明，不上传可执行资产，
  没有 descriptor、HMAC 或下载器。更新执行器按固定仓库（`qsbb/ling`）的 tag 取
  源码，自己解析 commit。镜像模式的更新源是**镜像摘要**，不是源码 tag。

## 2. 冻结的接线（源码模式的 `deploy/compose.yaml`）

> 镜像模式用的是另一份主文件 `deploy/compose.image.yaml`：宿主路径是**必填插值**
> （缺值 `docker compose config` 直接失败），还要 `LING_UPDATE_SHARED_GID`，
> 并且 `LING_UPDATE_ENABLED` 在那份文件里**写死 `"0"`**。详见 §10 与
> [DEPLOYMENT.md §11](DEPLOYMENT.md#11-镜像模式部署推荐)。

Compose 只给 **api** 注入这三个变量：

```yaml
LING_UPDATE_ENABLED: ${LING_UPDATE_ENABLED:-0}      # 默认 0（关闭）
LING_UPDATE_REQUESTS_DIR: /run/ling-update/requests
LING_UPDATE_STATUS_DIR: /run/ling-update/status
```

并挂两个目录（容器内路径固定，不要用 `.env` 覆盖）：

```yaml
- ${LING_UPDATE_REQUESTS_HOST:-../.run/update/requests}:/run/ling-update/requests:rw
- ${LING_UPDATE_STATUS_HOST:-../.run/update/status}:/run/ling-update/status:ro
```

`deploy/env.example` 保持 `LING_UPDATE_ENABLED=0` 并用注释说明：真实部署要把
`LING_UPDATE_REQUESTS_HOST` / `LING_UPDATE_STATUS_HOST` 显式写成宿主绝对路径，
且与执行器配置里的 `requests_dir` / `status_dir` 完全一致。仓库里的相对默认值
只适合本机试验。

宿主 `init` 负责创建目录与权限（不是 0777）：

- `requests`：`0770`，owner 宿主用户，group 为容器 api 用户的 gid（10001）；
- `status`：`0755`，宿主写、容器只读；
- `state`（锁、journal、备份、git mirror、release 暂存、恢复说明）：`0700`，
  **不挂进任何容器**。

`install.sh` **不会**自动打开更新通道；打开与否完全由宿主显式配置决定。

## 3. API 契约（v1，已冻结）

> 本节描述的是**源码模式**通道。镜像模式下 `POST /v1/version/update-requests`
> 在角色校验之后、读取请求体之前就返回 409 `image_mode_requires_image_channel`
> （`version.py` 的 `create_update_request()` 里 `image_mode_active(settings)` 分支）。
> `image_mode_active()` 认定镜像模式的信号有三个，任一成立即可：容器内有
> `LING_APP_IMAGE`、`LING_RUN_MIGRATIONS=0`（镜像模式 compose 对 api 写死）、
> 或更新器的 `status.json` 存在。`GET /v1/version` 与 `check-release` 是两种模式
> 共用的只读信息；更新相关的读写在镜像模式走 §10 的协议，两者刻意不共用同一个
> `v1` 语义下的更新源。

### `GET /v1/version`

当前进程实际加载的版本、构建提交（读 `LING_COMMIT`，不执行 git）与前端清单摘要。

### `GET /v1/version/check-release`

保留原有字段（`status/current/latest/published_at/html_url/is_newer`），**追加**：

| 字段 | 说明 |
| --- | --- |
| `release_id` | GitHub Release 的 id；不可核验时为 `null` |
| `target_commit` | tag 指向的提交（40 位）；未知为 `null` |
| `update_eligible` | 当前用户/版本/执行器状态下是否可提交更新 |
| `update_reason` | 不可提交时的固定原因码，可提交为 `null` |

### `GET /v1/version/update`

```json
{
  "schema_version": "v1",
  "enabled": false,
  "reason": "disabled",
  "executor": { "online": false, "last_seen_at": null },
  "current_job": null
}
```

`current_job` 非空时：

```json
{
  "request_id": "upd_<32 位 hex>",
  "state": "queued|running|succeeded|failed|needs_manual",
  "stage": "queued|preflight|backup|fetch|verify_source|build|deploy|verify|complete",
  "target_tag": "v0.2.0",
  "target_commit": "40 位 hex",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "error_code": "固定错误码或 null",
  "message": "由服务端固定码表生成，不回传宿主日志/路径",
  "backup_warnings": ["备份不完整/未能校验的告警摘要；没有告警时是 []"]
}
```

#### `backup_warnings`（可选字段，向后兼容）

媒体归档不完整（`media_state=partial`：归档时有文件正在上传，可能漏文件）时，
执行器**仍然判通过**（升级不替换 `app-data` 卷，硬失败会让每次“上传中提交升级”
都失败），但必须留痕。这些告警原先只落 `state_dir/backups/<id>/verify-warnings.txt`
与 `logs/<id>-backup.log`，Web 完全看不到，运维极易忽略——而恢复时缺媒体就是真的缺。
现在执行器把**同样的几条摘要**同时写进 `current.json` 的 `backup_warnings`，
`GET /v1/version/update` 原样透出，AboutPage 在结果区显著位置提示。

约定与边界：

- **纯加法、协议仍是 `v1`**：不新增必需属性、不改已有的 `state/stage/message` 语义。
  旧执行器不写这个键时，API 视为**空数组**（`[]`），不报错、不改变门禁；
  旧 API 读到新 `current.json` 时忽略这个键（`UpdateJob.from_dict` 只取已知字段）。
  **不要**为此升 `SCHEMA_VERSION`：已装机器的执行器固定在 `/srv/ling/deploy/update/executor.py`
  且不随 Release 更新，升版本会让它直接 `protocol_unsupported`，等于把升级通道锁死。
- **只做展示，不参与门禁**：`needs_manual`/进行中/离线等判定完全不受影响；
  告警存在也不代表“可更新”或“需人工确认”。
- **条目是单行摘要、有上限**：最多 8 条、单条 ≤200 字符，含换行/控制字符或超长
  的条目在读取侧被丢弃（只丢那一条，绝不让整个任务变成不可解析），在写入侧由
  `UpdateJob.__post_init__` 直接拒绝。
- **文件落点不变**：`verify-warnings.txt` 与备份日志照旧写（运维仍可 `tail`），
  `backup_warnings` 只是同一份告警的 Web 视图；细节以文件为准。
- 旧执行器（/srv 上未更新的那份）不会写这个字段——升级到带该功能的执行器之前，
  Web 上看不到告警，只有文件里有。

### `POST /v1/version/update-requests`

- `owner` 专属；其他角色 403。
- 必须 `Content-Type: application/json`、请求头 `X-Ling-Update: 1`，且同源
  （Cookie 认证必须带匹配的 `Origin`；Bearer 可省略 Origin，带了也要匹配）。
- 请求体**只允许**这两个键，字段名严格匹配：

```json
{
  "confirm_tag": "v0.2.0",
  "expected": {
    "release_id": 123,
    "tag": "v0.2.0",
    "commit": "40 位 hex",
    "current_version": "0.1.0",
    "current_commit": "40 位 hex"
  }
}
```

- 成功返回 **201** + 上面的 job 对象；只写 `pending.json`，不执行部署。
- 服务端会重新获取远端 Release 并逐项核对 `release_id/tag/commit` 与当前版本；
  任何一项变化都会 409（`release_changed` / `installed_version_changed`）。
- **没有取消/关闭接口**：已经进入构建的任务无法真正回滚，取消只会制造
  “界面说停了、容器其实换了”的假象。失败按阶段分流（见 §4）：构建阶段的
  失败可以记为 `failed`（旧服务仍在，可修复后重试），进入部署/验证之后才转
  `needs_manual` 由人工确认。
- **请求体上限 4096 字节**（`version.py` 的模块常量 `MAX_UPDATE_BODY_BYTES`），两条路径
  都会在**读 body 之前/之中**直接 422，不会先把超长体完整缓冲：
  1. `Content-Length` 已经大于 4096 → **读 body 前**就 422（`_read_update_body()` 里
     先比 `length_header` 的那一段）；
  2. 没有 `Content-Length`（chunked）或 header 被伪造时按 chunk 累计，累计到
     4097 字节立刻停止读取并 422（`_read_update_body()` 里 `async for chunk in request.stream()`
     累计到 `total > MAX_UPDATE_BODY_BYTES` 的那一段）。
  两条路径的 `error.detail` 都是 **`request_body_too_large`**
  （`version.py` 的模块常量 `DETAIL_BODY_TOO_LARGE`），不是文档另起的名字。

提交失败的固定码走 `error.detail`（外层是统一的 `{"error": {...,"detail":...}}`
结构，`src/ling/api/errors.py` 的 `error_response()`）：

| HTTP | `error.detail` | 触发条件（代码位置） |
| --- | --- | --- |
| 403 | `not_owner` | 角色不是 `owner`（`create_update_request()` 开头的 `if auth.user.role != "owner"`） |
| 403 | `origin_required` / `origin_mismatch` | Cookie 会话缺 `Origin`；`Origin` 非裸同源（判定在 `_origin_problem()`，抛错在 `create_update_request()` 里 `origin_problem is not None` 的分支） |
| 422 | `content_type_required` | `Content-Type` 不是 `application/json`（`_require_update_headers()` 的 `media_type != "application/json"` 分支） |
| 422 | `update_header_required` | 缺 `X-Ling-Update: 1`（同一个 `_require_update_headers()` 里的 `x-ling-update != "1"` 分支） |
| 422 | `request_body_too_large` | `Content-Length` > 4096，或 chunk 累计 > 4096（整个 `_read_update_body()` 的两条拒绝路径） |
| 422 | `invalid_request_body` | JSON/schema 不严格匹配：多余或缺失键、`confirm_tag != expected.tag`、tag 不是严格 `vX.Y.Z`、commit 不是 40 位（`_parse_submission()` 及它调用的 `_strict_text()` / `_exact_keys()`；`create_update_request()` 把 `_SubmissionError.detail` 原样转发） |
| 409 | `update_in_progress` | 已有合法 `pending.json`，或 `current.json` 是 `queued/running`（`updates/service.py` 的 `submit()` 里 `PENDING_STATE_OK` 分支、`_blocks_new_submission()` + `_active_job()`；409 状态码由 `_gate_error()` 给） |
| 409 | `release_changed` / `installed_version_changed` | 重新获取的 Release/版本与 `expected` 不一致，或远端不再更新（`create_update_request()` 的「二次核验」段，`raise _conflict(...)`；`_conflict()` 统一给 409） |
| 503 | 其余 gate reason | 见下表；`_gate_error()`（只有 `update_in_progress` 是 409，其它 gate 一律 503） |

`GET /v1/version/update` 的 `reason`、`check-release` 的 `update_reason` 与提交
被拒时的 `error.detail` 共用同一组 **gate** 固定码（`updates/service.py` 的模块常量
`UPDATE_REASONS`，各 reason 常量就定义在它上面）；提交路径另有几个只出现在 `error.detail` 里的码
（`release_changed`、`installed_version_changed`、`invalid_request_body`、
`content_type_required`、`update_header_required`、`origin_required`、
`origin_mismatch`、`request_body_too_large`，见上表）。

前端对 422 detail 的展示：`invalid_request_body` 在提交横幅显示“确认信息不完整或已失效”
（`AboutPage.tsx` 的 `UPDATE_DETAIL_TEXT` 里 `invalid_request_body` 那一项）；其余 422 detail
没有专门文案，仍按 `errors.ts` 的 `CODE_TEXT.invalid_request` 通用“请求参数不合法”显示。

`error.detail` 一律是固定字符串，不回传宿主日志、路径或异常文本；文案有两个来源：

- **协议错误码表**：gate/conflict 类（`not_owner`、`update_in_progress`、
  `release_changed`、`installed_version_changed` 等）来自
  `protocol.ERROR_MESSAGES` 码表（`message_for()` 是它的唯一读取入口）；
- **路由层固定字符串**：`origin_required` / `origin_mismatch`、`content_type_required`、
  `update_header_required`、`request_body_too_large`、`invalid_request_body` 的文案
  直接写在 `version.py` 的校验分支里（origin 判定 `_origin_problem()`、抛错在
  `create_update_request()`；`content_type_required` / `update_header_required` 都在
  `_require_update_headers()`；`request_body_too_large` 在 `_read_update_body()`；
  `invalid_request_body` 由 `create_update_request()` 转发 `_parse_submission()` 的
  detail），不在码表里——`message_for()` 对这些键返回 `None`。

| reason | 触发条件（代码位置） | 前端展示（`AboutPage.tsx`） |
| --- | --- | --- |
| `disabled` | `LING_UPDATE_ENABLED=0`（`updates/service.py` 的 `gate()` 里 `if not self.enabled` 分支） | 有专门文案“更新通道未启用” |
| `not_owner` | 角色不是 `owner`（同一个 `gate()` 里的 `if role != "owner"` 分支） | 有专门文案 |
| `channel_unavailable` | requests/status 目录不可用，或 submit 拿不到 `requests/.lock`（`gate()` 的 `if not state.channel_available` 分支，以及 `submit()` 里四处 `raise UpdateSubmissionError(REASON_CHANNEL_UNAVAILABLE)`） | 有专门文案 |
| `executor_offline` | 心跳缺失/不是 `status=ok`/超过 90s 未更新（模块常量 `EXECUTOR_OFFLINE_AFTER_SECONDS`、`executor_state()` 里算 `online` 的那段、`gate()` 的 `if not state.online` 分支） | 有专门文案 |
| `installed_commit_unknown` | 当前进程拿不到 40 位 `LING_COMMIT`（`gate()` 的 `if not protocol.is_full_commit(current_commit)` 分支；`version.py` 的 `build_commit()`） | 有专门文案 |
| `version_drift` | 心跳里的 `deployed_commit`/`current_version` 与 API 进程不一致（`gate()` 末尾比较 `state.deployed_commit` / `state.current_version` 的那一段） | 有专门文案 |
| `update_in_progress` | `current.json` 是 `queued/running`，或存在合法 `pending.json`（`_blocks_new_submission()` + `_active_job()`、`gate()` 的 `if state.active_job is not None` 分支、`submit()` 的 `PENDING_STATE_OK` 分支）。`needs_manual` 在执行器心跳明确为 `manual` 时先命中 `manual_required`；心跳已过期/缺失时同一个任务也按 `update_in_progress` 挡住（`_blocks_new_submission()` 把 `needs_manual` 也当门禁，见它的 docstring） | 有专门文案 |
| **`manual_required`** | 执行器心跳显式声明 `status=manual`，即 `current.json` 是 `needs_manual`（`gate()` 的 `if state.manual_required` 分支；执行器写这一状态有**三条**路径：`_finish_failure()`（构建之后的阶段失败/超时，`exc.manual=True`）、`_terminal()`（journal 损坏/中断/已消费重放等门禁路径）、`_mark_manual()`（上一轮留下 `running` 遗留任务，真实状态未知））：宿主必须先人工恢复并 `adopt-current` | 有专门文案“更新执行器要求人工核验：需在宿主机处理（adopt-current）后才会解除”（`AboutPage.tsx` 的 `GATE_REASON_TEXT.manual_required`，`REASON_TEXT` / `UPDATE_DETAIL_TEXT` 用 `...GATE_REASON_TEXT` 展开，故是同一句）。`needs_manual` 是**任务终态**，另有“上一次更新需要人工处理，完成后才能再次安装”（`MANUAL_BLOCK_TEXT`）。有 `needs_manual` 阻塞任务时状态栏优先显示 `MANUAL_BLOCK_TEXT`（`describeStatusNote()` 里 `activeJob.state === 'needs_manual'` 的三元）。**不会**被当成可更新，也不会被当成通道关闭 |
| **`protocol_unsupported`** | ① 心跳文件存在但**不可读或不是 JSON 对象**（`read_heartbeat()` 把读取失败折成 `{}`、`protocol.read_json_file()` 读不出对象、`parse_heartbeat()` 把 `{}` 判为协议不支持），或 `status` 缺失/未知、schema 不认识（`EXECUTOR_STATUSES` / `parse_heartbeat()`）；② `pending.json` 存在但不可信（符号链接/坏 JSON/未知 schema/字段非法，`read_pending_file()`）。gate 与提交都会在**执行任何部署前**拒绝（`gate()` 里 `not state.protocol_ok` 的分支、`submit()` 里两处 `REASON_PROTOCOL_UNSUPPORTED`） | 有专门文案“更新协议不兼容：API 与执行器需要同批升级，重试不会生效”（`AboutPage.tsx` 的 `GATE_REASON_TEXT.protocol_unsupported`，503 横幅经 `UPDATE_DETAIL_TEXT` 展开复用同一句），明确说明重试无效；只有未知码才落到 `当前不可更新（${reason}）`（`describeUpdateReason()` 的 `??` 兜底） |
| `release_unverifiable` | Release 不可核验（GitHub 不可达/无 Release/tag 不是严格稳定 tag，`evaluate_release()` 里 `target.status != "ok"` 与 `not target.update_ready` 两个分支） | 有专门文案 |
| `not_newer` | 远端 tag 不比当前版本新（`evaluate_release()` 里 `target.is_newer is False` 的分支） | 有专门文案 |

> 11 个 gate reason 在 `AboutPage.tsx` 里都有专门文案：`describeUpdateReason()`
> 只对**未知码**兜底成 `` `当前不可更新（${reason}）` ``（即 `REASON_TEXT[reason] ??` 的
> 那个模板串）。提交被拒时的错误横幅由 `explainUpdateError()` 按 `error.detail` 取
> 同一组文案：`version.py` 的 `_gate_error()` 把除 `update_in_progress`（409）以外的
> gate reason 都编码成 `provider_unavailable` 503，前端按 `detail` 取门禁文案；
> `detail` 缺失或遇到未知码时用固定兜底串“当前不满足更新条件”（`explainUpdateError()`
> 里 `status === 503 && code === 'provider_unavailable'` 分支的 `detailText ??`），
> 不会显示成“模型供应商不可用”（`errors.ts` 的 `CODE_TEXT.provider_unavailable`
> 通用映射只用于真正的供应商故障）。

## 4. 宿主执行器（**只支持源码模式**）

> **边界先写清楚**：`deploy/update/executor.py` 只能服务源码模式部署——它 `git
> archive` tag 源码、在宿主构建、再调该 release 树里的 `install.sh`（那是
> `deploy/compose.yaml`，带 `build:` 与前端 bind）。**镜像模式必须用 §10 的新通道
> （`/v1/version/image-update-requests` + 独立 `ling-updater`）**，两者不能混用：
>
> - 镜像模式的 API 会直接拒绝这条通道的提交（409 `image_mode_requires_image_channel`）；
> - 反过来，旧执行器在镜像模式拓扑上会去构建并部署**另一套**主文件，不是在升级
>   镜像——设计 §2 要求迁移前先关闭 `LING_UPDATE_ENABLED` 并停用该执行器，
>   状态与配置先备份（见 [DEPLOYMENT.md §12](DEPLOYMENT.md#12-从源码模式迁移到镜像模式)）。

代码在 `deploy/update/executor.py`，配置示例 `deploy/update/config.example.toml`
（建议放 `/etc/ling-update.toml`，不要放在源码 checkout 里）。命令：

| 命令 | 作用 |
| --- | --- |
| `init` | 创建 requests/status/state 并设置安全权限（不部署） |
| `doctor` | 检查 checkout、目录/文件权限、docker、installed 记录 |
| `run --once` | 只执行一轮（cron / 手动排查） |
| `run` | 常驻轮询；构建期间持续写心跳 |
| `status` | 打印 installed/heartbeat/current job（宿主私有输出） |
| `adopt-current` | 把当前实际运行的 commit/version 记为已安装基线（不部署） |

一次更新的阶段：

`queued → preflight → backup → fetch → verify_source → build → deploy → verify → complete`

关键约束：

- **不信任请求**：执行器自己 fetch 固定仓库的 tag、解析 40 位 commit，与 API
  写来的目标逐项比对；不接受分支名或 `target_commitish`。
- **同一把安装锁贯穿“备份 → 安装”**：执行器在预检/备份之前就取得
  `deploy/.env` 真实目标所在目录的 `.ling-install.lock`，再通过 `pass_fds` 把
  FD 以 `LING_INSTALL_LOCK_FD` 交给安装器；安装器不重复抢锁，只校验这个 FD
  确实指向该锁且真的被持有（伪 FD 直接拒绝）。因此“备份 → 构建 → 切换”整段
  插不进第二个安装器，人手工跑 `install.sh` 也会被同一把锁挡住。
- **`deploy/.env` 固定为宿主持有的那份原始 env**：新 release 树的
  `deploy/.env` 指向同一份文件（symlink），不是复制——否则“锁跟着哪份 .env 走”
  会随 release 漂移，密钥也会在 release 树里多出副本。安装器不覆盖已存在的
  `.env`，也不把密钥复制进 release 树。
- **先备份再动手**：复制原 `deploy/.env`（0600）到 state 目录，跑数据库备份并
  校验 dump 头/墓碑清单/alembic 版本；任何一步失败都不调用安装器。
- **独立 release 目录**：`git archive` 到 state 目录下的暂存树（archive 没有
  `.git`），再以 `LING_COMMIT=<目标 sha>` 调该 release 里的 `install.sh`。
- **安装器阶段标记**：执行器传 `LING_INSTALL_PHASE_FILE`；`install.sh` 在
  `build` / `deploy` / `verify` / `complete` 原子写当前阶段。
- **失败（非超时）分类**（`executor.py` 的 `_classify_install_failure()`）：
  - 最后标记是 `build` → `failed` / `build_failed`（旧服务仍在，可修复后重试）；
  - 最后标记是 `deploy` / `verify` / `complete` → `needs_manual` / `deploy_failed`；
  - 没有阶段标记或标记不可读 → `needs_manual` / `deploy_state_unknown`。
- **超时（`installer_timeout`）分类**（`executor.py` 的 `_classify_install_timeout()`）：
  只有**两条同时成立**才判 `failed` / `build_failed`：
  1. 阶段标记可读**且恰好是 `build`**（说明还没触发 deploy/verify/complete 命令）；
  2. 宿主确认安装进程组内**已无存活进程**（`group_terminated`，
     由 `executor.py` 的 `_kill_process_group()` 判定、在 `run_process()`
     捕获 `TimeoutExpired` 的分支里置位）。

  其余一律 `needs_manual` / `installer_timeout`：**没有阶段标记**、标记已是
  `deploy`/`verify`/`complete`、安装进程组仍存活（`docker compose build` 由
  daemon 执行，杀进程组不等于 daemon 停手）、或进程被外部 kill 根本没有超时
  分支可走。最后一种（执行器重启后看到 `running` 残留）走
  `needs_manual` / `interrupted`（`executor.py` 的 `execute_once()` 里
  `current.active` 的 `interrupted` 分支：`_mark_manual(current, "interrupted")`
  → `clear_pending()` → `write_heartbeat()`）。
- **未预期异常也按阶段判定**（`executor.py` 的 `_execute()` 逐段赋 `stage`，
  `_unexpected_failure()` 用 `STAGE_FAILURE_CODES` 映射）：`preflight`/`backup`/
  `fetch`/`verify_source` 内的异常 → `failed`（对应 `config_invalid`/
  `backup_failed`/`fetch_failed`/`source_invalid`）；从 `build` 起一律
  `needs_manual` / `deploy_state_unknown`。绝不在异常时声称成功。
- **不盲回滚**：`needs_manual` 时不碰数据库、不删镜像、不自动回滚，只在
  state 目录写恢复说明；旧 release 树、旧镜像、数据卷都保留。
- 宿主依赖：Python ≥ 3.12、`git`、`docker compose`、`pg_dump`；执行器不依赖
  API 的 venv 或在线服务。

### 4.1 运维前提（同批升级 / 残留清理 / 执行器不随 Release 走）

- **API 与宿主执行器必须同批升级**。两侧唯一的契约是
  `src/ling/updates/protocol.py` 的模块常量 `SCHEMA_VERSION`（当前 `v1`）
  与心跳里的 `status` 字段。旧执行器写的心跳没有 `status`（或 schema 不认识）时，
  API **fail closed**：`parse_heartbeat` 把它当成
  `status=protocol_unsupported`（`EXECUTOR_STATUSES` 与 `parse_heartbeat()`），gate 与提交入口
  都返回 `protocol_unsupported` 并**拒绝更新**（`gate()` 里 `not state.protocol_ok` 的分支、
  `submit()` 里 `state.heartbeat_present and not state.protocol_ok` 的分支），
  Web 会显示“更新协议不兼容：API 与执行器需要同批升级，重试不会生效”。
  执行器侧对过旧/不完整的协议实现同样 fail closed（`executor.py` 的
  `load_protocol()`：要求 `SCHEMA_VERSION == "v1"` 且 `REQUIRED_PROTOCOL_ATTRS` 齐全，
  否则 `init`/`run` 直接报配置错误）。所以：换新 API 镜像的同一批里要同步宿主上的
  执行器文件，不要只升一半。
- **`pending.json` 残留会让 API 一直 409**。API 只要看到合法 `pending.json` 就报
  `update_in_progress`（`submit()` 的 `PENDING_STATE_OK` 分支、`_blocks_new_submission()` /
  `_active_job()`），**绝不覆盖**（覆盖会吃掉宿主在途的 claim）。宿主恢复执行器后
  由它自己做 compare-and-unlink 清理（`executor.py` 的 `clear_pending()`：只在终态/中断
  清理时调用；`claim_pending()` 在 requests 锁内自己做同一套 compare-and-unlink）；
  执行器起不来时只能人工在宿主删除 `requests/pending.json` 或修好执行器。
  `needs_manual` 门禁则是另一扇门：
  必须由宿主核验后跑 `adopt-current` 解除（`executor.py` 的 `adopt_current()`；解除时
  清掉 `current.json`、再重写心跳：先 `remove_file(...CURRENT_FILE)` 再
  `write_heartbeat()`），否则 `manual_required` / `in_progress` 会一直挡着。
- **执行器代码不随 Release 更新**。`install.sh` 只构建/部署当前源码树，从不安装
  或替换 `deploy/update/`（`deploy/install.sh` 里没有任何 `deploy/update` 引用），
  Release 也不上传可执行资产（见 §6）。宿主上的执行器由 systemd 固定在
  `/srv/ling/deploy/update/executor.py`（`deploy/update/ling-update.service` 里那条
  `ExecStart=... executor.py ... run`），
  升级 release **不会**换掉它。
  注意区分两件事：执行器**进程文件**是固定的，但它会按
  `installed.release_root/src → source_root/src → 自身 checkout 的 src` 的顺序
  导入 `ling.updates.protocol`（`executor.py` 的 `load_protocol()`），所以**协议模块**
  会跟着 live release 走——重启执行器就会加载新协议。这正说明“只升级一半”有多容易：
  协议换了、执行器主体还是旧提交（缺新 reason/新阶段处理）时照样能跑，但行为
  与文档不一致。要升级执行器，必须人工把新提交的
  `deploy/update/executor.py` 同步到该路径并 `systemctl restart ling-update`，
  再 `doctor` / `adopt-current` 复核。

## 5. install.sh 的边界（重要）

> 本节只适用于**源码模式**。镜像模式的安装/升级**不用 `install.sh`**：它构建当前
> checkout、用 `deploy/compose.yaml` 并把前端 bind 到 `/app/web`，也不读
> `LING_APP_IMAGE`；镜像模式用 `ling install` / `publish.py`（见
> [DEPLOYMENT.md §11](DEPLOYMENT.md#11-镜像模式部署推荐) 的 §11.4）。

- 只构建**当前 checkout 的源码**。它不是更新客户端，不联网取代码。
- 构建前确定 `LING_COMMIT`：优先显式 `LING_COMMIT`（执行器保证是目标
  release 的 sha），否则用本检出 `git rev-parse HEAD`；源码归档且没有显式值时
  明确告警并留空（`/v1/version` 报 `commit=null`），**不伪造提交号**。
- `install` / `restart` / `down` / `--build-web-only` 共用跨 release 的
  `.ling-install.lock`（`deploy/.env` 真实目标所在目录），拿不到锁直接失败；
  锁随进程退出释放，异常退出也不会留下“假占用”。锁文件**只接受普通文件**：
  符号链接/目录/FIFO 会在打开前被拒绝（`lstat` + `O_NOFOLLOW` + `fstat` inode
  核对），安装器也不会截断或写入它。宿主若已持锁（`LING_INSTALL_LOCK_FD`），
  安装器在**继承来的 FD 本身**上要求 `flock(LOCK_EX|LOCK_NB)` 成功，并用独立
  probe 证明该 inode 确实有持有者：别人持锁时传一个同 inode 但未加锁的 FD 会被
  拒绝，共享锁不会被当成互斥；没有“跳过锁”的开关。
- 对外端口只有一处来源：Compose 解析出的 **api** 发布端口（`target=6520`，
  宿主端口由 `LING_PORT` 决定）。安装器的 `/readyz` 等待、安装摘要与 `--status`
  都用它，不再假定 18080；已有 `.env` 时按真实端口工作，`--port` 与真实端口
  冲突则直接失败（安装器不写已有 env），不会对着错端口等 120 秒。解析不出
  确定的 `target=6520` 端口就 fail closed。
- **会检测已废弃的 edge 键**：`install` 与 `--status` 都**只读**检查
  `deploy/.env` 里是否还有 `LING_EDGE_PORT` / `LING_EDGE_TLS_PORT` /
  `LING_EDGE_CONF` / `LING_EDGE_CERTS` / `LING_NGINX_IMAGE`，命中就逐条告警
  （说明该改成 `LING_PORT`、并同步对外地址），**不打印值、不改写 `.env`**。
  端口归因也改准了：`.env` 与 shell 都没有 `LING_PORT` 时明说“用的是 compose
  默认值”，并指名“从 `LING_EDGE_PORT` 变成了该端口”。升级路径见 §7.1。
- **拓扑变化不会留下孤儿容器（兜底）**：`start_services()` 的 `docker compose up`
  与 `--down` 都带 `--remove-orphans`，只清本项目（`--project-name`，默认 `ling`）
  里已不在 compose 文件中的容器；`down` 不加 `-v`，数据卷不受影响。它**不能**
  替代“先 `down` 清场再升级”：旧容器已经占着宿主端口时，先按 §7.1 第 1 步停掉。
- 宿主可传 `LING_INSTALL_COMPOSE_OVERRIDE=<绝对路径>` 追加一个 compose 文件：
  `docker compose -f deploy/compose.yaml -f <override> …`，始终保留
  `--env-file deploy/.env`。它用于跨独立 release 固定 bind 来源（更新通道目录
  等）。安装器只校验并使用这个路径，**不复制、不改写、不直接解析**
  它的文本（解析交给 Compose；文件可以是 JSON，即 YAML 的子集），也不把它写进
  `deploy/.env`；文件必须存在且是普通文件（拒绝 symlink/目录/FIFO、≤1 MiB）。
  解析结果还要通过不变量：不能把 `/app/web` 指到本次 release 之外（fail-closed，
  报“不是本次校验产物”），也不能搬走/换身份持久命名卷 `db-data`/`app-data`
  （顶层卷 identity + 每一处命名卷挂载的 服务/target/只读/卷身份；匿名卷按
  无跨重启身份处理）。
- **override 是宿主信任输入，不是安全沙箱**：上面的校验只是两条结构不变量
  （`/app/web` 身份、持久命名卷身份），**没有 allowlist**。override 能新增任意
  bind（包括 docker socket）、`privileged`/`cap_add`/`user`/`network_mode`，
  也能覆盖 `image`/`command`/`build`，这些都会原样交给 Compose。它必须由
  root/部署用户持有（建议 `0600`，不要 0777），内容由人核实；不要用 Web、
  半可信脚本或普通用户可写的路径去生成它。更严格的边界请在宿主侧做评审/
  白名单——安装器不提供“无害 override”保证。
- HOME / DOCKER_CONFIG 原样保留（docker context 与 registry 凭据跟着它们走）；
  只有 HOME 缺失/不可写（老 NAS）才回退到仓库内已忽略的 `.run/home` /
  `.run/docker` 并告警。旧版的 `.home`/`.docker` 不再使用，也**不会被删除或
  搬移**（它们会让旧 checkout 变 dirty，还会把 registry 配置带进 build context）。
  显式 `DOCKER_CONFIG` 不可写/建不出来时只告警、不重置——让 docker 自己报错，
  避免把 `--down`/`--logs` 这类恢复路径提前堵死。
- **`deploy/.env` 只读**：安装器不追加、不改写它。基础镜像（python/postgres）
  的解析结果只 `export` 到本次进程环境（Compose 的 shell > env-file
  优先级让 build/up 用到）；显式配置（shell / `--python-base` / env-file）一律
  保留，只有“默认值且官方名不可用”才换**同一 tag** 的镜像源，绝不跨主版本，
  两处都拿不到就 fail closed。
- 生成新 `deploy/.env` 时 `LING_WEB_DIST_HOST` 写相对默认值（`../clients/
  web-console/dist`），不再写本检出绝对路径——canonical env 可能被多个 release
  共享。已存在的绝对值不会被自动修改；**手动升级另一棵树时要显式
  `export LING_WEB_DIST_HOST=<那棵树>/clients/web-console/dist`**，且保留当前
  runtime 其它 bind 的完整上下文（持久 override、更新通道）。
  更新执行器本来就会传自己 release 的 dist，这一条只影响人工升级。
- 发布顺序仍是：构建前端暂存 → 构建镜像 → 起容器 → `/readyz` → 最后发布前端。
  任一阶段失败都不会发布新前端。

## 6. Release 流程

- 只从**稳定版 tag** `vX.Y.Z` 触发；预发布/构建后缀不触发。
- 发布前校验：tag == `pyproject.toml` version == `src/ling/__init__.py`
  `__version__` == `clients/web-console/package.json` version，且 tag 指向的提交
  是 `main` 的祖先（`scripts/check_release.py`，解析 tomllib/ast/json，不执行
  仓库代码，tag 只作为 argv 传给 git）。
- 门禁顺序（`.github/workflows/release.yml`）：`verify`（三处版本 + tag 祖先）→
  `ci`（复用 `ci.yml`）+ `mobile`（复用 `mobile.yml`）→ `image`
  （构建/推送 `ghcr.io/qsbb/ling-api`）→ `updater-image`
  （`ghcr.io/qsbb/ling-updater`）→ `release`。后一个用 `needs` 挡住前一个：
  镜像没发出去就不会建 Release，避免“Release 有了、镜像缺席”。
- **Release 正文是三步拼出来的**（早先文档写的
  `gh release create --generate-notes` 已经不对）：
  1. `gh api --method POST "repos/$GH_REPO/releases/generate-notes" -f
     tag_name=<tag> --jq .body` 取 GitHub 自动生成的 notes；
  2. 在本地拼上「部署用 index digest」段——两个镜像的 `sha256:<64 hex>` index
     digest，以及可直接复制的 `LING_APP_IMAGE=` / `LING_UPDATER_IMAGE=` 两行；
  3. 最后**一次** `gh release create "$RELEASE_TAG" --verify-tag --title ...
     --notes-file <拼好的文件>`。
  顺序是有意的：`generate-notes` 失败时 Release 还没建出来，不会留下“已发布但缺
  摘要”的版本（先 create 再 edit 的写法有这个窗口）。任一 digest 不是合法
  `sha256:<64 hex>` 就 `::error::` 并退出——宁可 job 红，也不发没有摘要的 Release。
- 权限最小化：仓库默认 `contents: read`；`image` / `updater-image` 恰好
  `contents: read` + `packages: write`（没有 `contents: write`，也没有
  `id-token` / `attestations`——本次不用 GitHub 原生构建证明）；只有 `release`
  job 持有 `contents: write`。actions 固定到审核过的完整 commit SHA（文件末尾有
  tag → SHA 对照表），不退回 `@vN`。
- 不构建/上传可执行资产（**因此目前也没有可下载的安装包**），不生成
  descriptor/HMAC。源码模式的更新源就是 GitHub 源码 tag/commit；镜像模式的目标
  摘要来自 CI job summary 与仓库内的 `deploy/releases.json`——**Release 正文可编辑，
  不是摘要的可信来源**，只方便人复制。

## 7. 源码模式怎么升级（人工路径）

> 本节**只适用于源码模式**（`install.sh` 构建当前 checkout）。镜像模式的日常升级
> 是人工跑 `deploy/release/publish.py --digest ...`（见
> [DEPLOYMENT.md §11](DEPLOYMENT.md#11-镜像模式部署推荐) 与 §12），**不是**本节。

1. 备份：默认 Compose 部署没有发布 db 端口、宿主机也没有 DSN，所以用 compose
   模式（`LING_COMPOSE_PROJECT` 必须与真实项目名一致，`install.sh` 默认
   `-p ling`；用 `--project` 改过就填那个）。**项目名是唯一承重的参数**：
   Compose v5 的 `docker compose exec` 按 project + service 的 label 找容器，
   **不看 `-f`**（`-f` 只对 `up`/`config` 有意义），项目名对不上时报
   `service "db" is not running`——那与"db 容器真挂了"文案相同，先怀疑项目名。
   `backup.sh` 会在备份前用 `-f` 做一次 `config -q` 静态校验，compose 文件坏掉
   时以 **exit 2**（配置错误，不是 pg_dump 的 3）fail fast：

   ```bash
   LING_BACKUP_VIA_COMPOSE=1 \
   LING_COMPOSE_PROJECT=ling \
   LING_COMPOSE_FILE="$(pwd)/deploy/compose.yaml" \
   deploy/backup/backup.sh /path/to/backup
   ```

   产物是 `ling-YYYYmmdd-HHMMSS.dump` + `manifest-YYYYmmdd-HHMMSS.txt` +
   `tombstones-YYYYmmdd-HHMMSS.txt` + `keys/root.key` + `media-YYYYmmdd-HHMMSS.tar`
   （`scripts/backup.sh` 只是它的转发入口；直接模式另有 `version-*.txt`，
   compose 模式不产出）。后两件来自 `app-data` 命名卷：
   根密钥丢了解不开 provider 凭据，媒体字节丢了 `media_ref` 全部悬空，所以
   `backup.sh` 会把它们一起流式归档；卷里没有对应内容时 manifest 显式写
   `keys_root=absent` / `media=absent`。再复制 `deploy/.env` 到安全位置，
   确认备份目录不在源码树里，并**把 `keys/root.key` 与 dump 分开存放**。
   `manifest-*.txt` 里的 `keys_root_source` 只说明"密钥字节从哪来"
   （`file`/`env`/`none`），**不**说明"应用用哪一把"：`src/ling/infra/security.py`
   是 `LING_ROOT_KEY` **优先**，所以文件与 env 同时存在时脚本会额外记
   `keys_env_set=true` 并警告——恢复时必须把那份 `LING_ROOT_KEY` 一起恢复，
   否则只回灌 `keys/root.key` 可能解不开（口径见
   [DEPLOYMENT.md §8.1](DEPLOYMENT.md#81-定时备份)）。
   **同时记录当前 api 与 worker 的精确 image ID**（`docker inspect --format
   '{{.Image}}' <project>-api-1` 等，或 `docker image ls --no-trunc`）——没有它
   就没有可信的回旧镜像路径（见 §8）。
   注意 `manifest-*.txt` 里 `tombstones_state=FAILED` 表示墓碑清单导出失败，
   这份备份**不能**用于恢复（恢复演练脚本也会因此直接失败）。
   `media-*.tar` 是**全量**媒体副本，每次备份都完整复制一份，默认**不删**历史。
   `deploy/backup/backup.sh` 提供**默认关闭**的轮转（`--prune-keep N` /
   `--prune-days D` / `--dry-run`，N 含本次、历史回看 N−1 天；零撤回的空墓碑
   也是合法产物，正常参与轮转）；执行器调用时只传位置参数、**不传任何
   `--prune-*`**，所以升级备份目录不会被自动轮转——要轮转得靠 cron 行/人工
   显式加参数（先 `--dry-run` 看计划）。空间与耗时都要自己估（见
   [DEPLOYMENT.md §8.1](DEPLOYMENT.md#81-定时备份)）；执行器备份超时
   （`BACKUP_TIMEOUT_SECONDS=1800`）含当前 `/data/media` 全量打包，媒体特别大时
   应缩小 `/data/media`、调大该上限或换更快存储——减少历史份数不会让本次备份
   变快。
   定时任务用 `deploy/backup/install-cron.sh /mnt/backup/ling 04:30`（幂等，重复
   执行只留下一条）。它会把 `LING_COMPOSE_PROJECT` 一起写进 crontab 行：
   项目名默认**从 `deploy/install.sh` 的 `PROJECT=` 默认值解析**（与安装器用的
   `--project-name` 同源，不写死 `ling`）；用 `--project` 改过项目名时，安装定时
   任务前显式 `export LING_COMPOSE_PROJECT=<那个名字>`。项目名为什么不能省见
   [DEPLOYMENT.md §8.1](DEPLOYMENT.md#81-定时备份)（`-f 路径` 会按目录名推出
   `deploy`；另外 Compose v5 的 `exec` 不看 `-f`——`-f` 只对 `up`/`config` 有意义，
   而 `-p` 是否承重取决于 compose 文件有无顶层 `name:`，本仓库有，但仍应显式给）。装完仍要 `crontab -l | grep backup.sh` 核对那一行确实带着
   `LING_COMPOSE_PROJECT`，并 `tail -f /mnt/backup/ling/backup.log` 看第一次真
   产出了 dump——**“已安装每日备份”不等于“备出来了”**。
2. 在宿主机取到目标版本源码（`git fetch` 后切到对应 tag/commit，或解压归档）。
3. 在该源码树运行 `bash deploy/install.sh`；脚本会保留已有 `deploy/.env`，
   重新构建镜像、滚动重启并发布前端。若这份 checkout 和 canonical env 不在同一
   棵树，且 env 里 `LING_WEB_DIST_HOST` 是旧树的绝对路径，手动运行前先
   `export LING_WEB_DIST_HOST="$(pwd)/clients/web-console/dist"`（并保留
   `LING_INSTALL_COMPOSE_OVERRIDE`、更新通道等其它 bind 上下文）。
4. 检查 `bash deploy/install.sh --status` 与 `/readyz`，登录确认版本
   （`/v1/version` 的 `commit` 应等于目标 sha）**——这是需要登录的用户接口；
   自动探活不持凭据，不要拿它当探针**。

第一次启用自助更新之前，还需要：`deploy/update/executor.py init` →
`doctor` → `adopt-current`，确认 requests/status 权限与执行器识别到的
commit/version。**确认 source_root 已经具备新版的 `.gitignore` /
`.dockerignore`（`.run/` 等）与跨 release 锁支持、且 `git status` 干净**，再
把 `LING_UPDATE_ENABLED` 改成 1。改完必须让 api **重建/重启**（`docker compose
… up -d`，必要时 `--force-recreate`）；`restart` 不会重新读取 `.env`/环境变量，
开关不会生效。

### 7.1 从 edge 拓扑迁移（旧部署升级必做）

这一节只针对**已经跑着旧拓扑**的机器：`docker ps` 里有 `ling-edge-1`，
`deploy/.env` 里是 `LING_EDGE_PORT=20443`（可能还有 `LING_EDGE_TLS_PORT` /
`LING_EDGE_CONF` / `LING_EDGE_CERTS` / `LING_NGINX_IMAGE`）。新版本删掉了
`edge` 服务，api 直接发布 `LING_PORT`：那些键在 compose 里**没有任何引用**，
端口会静默变成 `LING_PORT` 的默认值 6520，而 OneBot 上报、防火墙、外部反代
还指着旧端口。**自助更新走不通这条路**——旧 live release 的 compose 有
`edge`、新 release 没有，服务集合不一致会在 plan 阶段直接判
`compose_bind_changed`（`manual=False`，重试不会成功，这是有意的
fail-closed 判据）；必须先人工迁移。

完整步骤与逐条自查见
[DEPLOYMENT.md §7「从 edge 拓扑迁移」](DEPLOYMENT.md#从-edge-拓扑迁移旧部署升级必读)，
这里只列关键动作与执行器部分：

1. **停旧拓扑并清 orphan**（在更新代码之前跑；若已同步代码，`edge` 不在新
   compose 里，必须靠 `--remove-orphans` 才会被清掉）：

   ```bash
   cd /path/to/ling        # 你的部署目录，见 DEPLOYMENT.md §1
   docker compose -f deploy/compose.yaml --project-name ling --env-file deploy/.env \
     down --remove-orphans
   ```

   `/path/to/ling` 代指仓库根目录（DEPLOYMENT.md §1/§2 的示例写的是 `/opt/ling`），
   实际路径以你的部署为准。

   不带 `--remove-orphans` 时 `edge` 会作为孤儿容器继续运行、继续占着
   20443/8443，而它的上游 api 已经没人监听（表现为 502）。`down` 不加 `-v`
   不删数据卷。项目名用安装时的（默认 `ling`）。

   **风险**：`--remove-orphans` 的作用域是整个 compose 项目（默认 `ling`），
   同一个项目里你自己手工创建、但不在 compose 文件中的容器**也会被删掉**；
   要保留的请换项目名或改用独立 `docker run`。
2. **改 `deploy/.env`**：删掉上面那 5 个键；加 `LING_PORT=20443`（保持对外地址
   不变）或 `LING_PORT=6520`（换新端口）。安装器只读这份文件，不会替你改；`--status`
   与 `install` 都会逐条点名残留的废弃键（只读检测，不回显值）。
3. **更新代码树后跑安装器**：`bash deploy/install.sh`。安装器的 `up` 与 `--down`
   现在都带 `--remove-orphans`（兜底，清本项目孤儿容器，含同一项目里手工创建、
   不在 compose 文件中的容器，见第 1 步的风险）；但旧容器已经占着端口时
   仍要先做第 1 步的 `down`，别指望安装过程替你解决端口冲突。
4. **同步执行器并重建基线**：
   - `/etc/ling-update.toml` 的 `port` 改成新的 `LING_PORT`（它用这个端口探宿主
     `/readyz`，不改就永远 `verify_failed`）；
   - `sudo python3 deploy/update/executor.py --config /etc/ling-update.toml adopt-current`
     —— 核验并**记下**“现在实际运行的是什么”，同时解除 `needs_manual` 门禁；
     不能完全自证时它会拒绝（不写 installed、不解除门禁）。命令语义见
     `deploy/update/README.md` 第三节，失败处理见本文 §8。
   - 被 `compose_bind_changed` 拒过的话，宿主 stdout 与
     `state_dir/recovery-<request_id>.txt` 里有具体差异（例如“服务集合从
     （api、db、edge、worker）变成（api、db、worker）；少了 edge”）；Web 上
     仍只显示固定文案，这是协议设计（见 §3）。
5. **验证**：`curl http://127.0.0.1:<端口>/readyz` 返回 200；`docker ps` 里
   **不应再有** `ling-edge-1`；`bash deploy/install.sh --status` 无废弃键告警。
6. **外部集成自查**：OneBot/NapCat 的 HTTP 上报地址（`/v1/onebot/events`）、
   防火墙/安全组放行的端口、外部反代 upstream、书签与客户端配置。

## 8. 人工恢复步骤（源码模式）

> 本节的“把旧 image ID 打回 `<project>-api:latest`”是**源码模式**的恢复路径：
> 那些镜像名来自宿主 `docker compose build`。镜像模式下容器直接跑固定摘要，
> 没有可被 build 覆盖的本地镜像名，失败语义也不一样（已迁移后**不**切回旧镜像、
> **不**恢复旧 dump）——见 §10.6。

`needs_manual` 表示“无法证明服务仍停在旧版本”。恢复**只在宿主上做**：Web 界面
没有“清除/重置失败任务”的开关，点了也不会让容器回到旧版本，只会制造“界面说好
了、容器其实没动”的假象。不要反复点更新。

**先纠正一个常见误解**：`docker compose … up -d --no-build` **不是**回旧镜像。
`docker compose build` 会把新构建重新打到 `<project>-api` / `<project>-worker`
这些 Compose 使用的镜像名（默认 `latest`）上；`--no-build` 只是“这次不再构建”，
跑的还是**已经被重指到新构建**的那个名字。真正的回旧镜像必须先把**更新前**的
精确 image ID 重新 pin 到 Compose 会用的名字上。

另一个前提：**旧 image ID 必须是更新前核实保存的**（§7 第 1 步）。更新已经失败
之后再从 `latest` / tag 名反推是无效的——那个名字已经指向新镜像。api 与 worker
是两个独立镜像，**必须各自都有旧 ID**；如果只保存了 api 的、worker 的旧 ID
缺失，就**不能**声称可以回旧 worker（只能回 api，或按下面的数据库路径处理）。

1. 先看清现状（**不要**从旧源码树裸跑 `docker compose`：它的相对 bind 会静默
   指向旧树的前端/证书）。所有手工 Compose 命令都用完整上下文：live 基础文件 +
   持久 override + canonical env + 原 project 名：

   ```bash
   PROJECT=<project>                            # install.sh 默认 ling
   LIVE=<正在运行的那棵树>
   ENV_FILE=<canonical env 的绝对路径>
   OVERRIDE=<宿主持久 override 的绝对路径>     # 没有就留空

   # 所有手工 Compose 命令都走这个函数：绝不从旧源码树裸跑。
   compose() {
     local -a args=(docker compose -p "$PROJECT" -f "$LIVE/deploy/compose.yaml")
     [ -n "$OVERRIDE" ] && args+=(-f "$OVERRIDE")
     args+=(--env-file "$ENV_FILE")
     "${args[@]}" "$@"
   }

   compose ps
   compose exec api cat /proc/1/cmdline >/dev/null   # 确认容器可达
   ```

   然后读实际运行的 commit/version（`/v1/version`，或 exec 进 api 打印
   `LING_COMMIT` 与 `ling.__version__`）。容器 `ps` 的 IMAGE 一列只是名字，
   不是证据。**`/v1/version` 需要登录**，这里只是人工排查时用；发布/更新器
   的自动探活改用容器内进程证据，不打这个端点（见 §10.5）。

2. 决定是否回旧镜像。**只有下面两条都成立才手动 pin**：
   - 更新前记录的 api **和** worker 旧 image ID 都还在本机（`docker image
     inspect <id>` 成功）；
   - 数据库 schema 与旧镜像兼容（见第 3 步）。

   先记下当前（新）ID 备查，再把旧 ID 打回 Compose 使用的名字（示例中的
   `<...>` 全部是占位符，必须换成你核实过的真实值；不提供未经核实的一键脚本）：

   ```bash
   OLD_API=<更新前核实的 api image id>
   OLD_WORKER=<更新前核实的 worker image id>
   docker image inspect "$OLD_API" "$OLD_WORKER" >/dev/null   # 必须成功
   docker tag "$OLD_API" "${PROJECT}-api:latest"
   docker tag "$OLD_WORKER" "${PROJECT}-worker:latest"
   compose up -d --no-build        # 复用第 1 步的 compose()
   ```

   **这不是一键恢复脚本**：不要复制后直接执行，每一步都要先核实 ID/路径/schema。
   缺 worker 旧 ID 时不要执行上面两步 `docker tag` 中的 worker 部分，也不要把
   “只回 api”说成“回旧版本”——api 与 worker 版本不一致本身就需要人工确认。

3. **数据库 schema 必须先核对**：新版本可能已经跑过迁移。回旧镜像前检查
   `alembic current` / 备份里记录的 alembic 版本与旧代码是否兼容；旧代码不认识
   新 schema 时，“镜像回旧”不等于“服务回旧”，只能向前修复或按第 4 步恢复备份。
   不要指望自动 schema 回滚（没有实现，也不打算实现）。

4. **逻辑 dump 不是 PITR**：备份是 `pg_dump` 全量逻辑备份，恢复会**丢掉备份之后
   写入的全部数据**（不是“回到某个时间点”）。恢复后必须**重新应用撤回墓碑**，
   而且不能只用备份时的清单：**恢复之前**先从当前库导出墓碑，合并后再重放。
   完整命令与逐条说明见
   [DEPLOYMENT.md §4「重新应用撤回墓碑」](DEPLOYMENT.md#重新应用撤回墓碑恢复后必做)；
   这里只需记住三条约束：

   - 先导出（`select id from memories where retracted = true`）→ 再恢复 → 再重放；
   - 清单里只允许 `mem_<hex>` 这类 id，格式不对先人工看；
   - 备份的 `tombstones_state=FAILED` 时，这份备份不能用于恢复。

   重放后再校验 `/readyz`、`/v1/health/ops` 与关键数据。
   **恢复顺序见 [DEPLOYMENT.md §4「恢复顺序」](DEPLOYMENT.md#恢复顺序先-db--再-rootkey-与-media--最后起服务)**：
   先恢复 DB → 再把 `keys/root.key` 与 `media-*.tar` 放回 `app-data` 卷 →
   最后启动服务。密钥必须先就位：`LING_ROOT_KEY` 为空且 `/data/root.key` 不存在时
   api 会**自己生成新密钥**，那之后旧密文就永久解不开了。

5. 不删除旧 release 树、旧镜像和数据卷，直到确认新版本稳定。

6. 记录实际失败阶段（state 目录里的安装日志与 `recovery-*.txt`），再决定是否
   重试。

7. 处理完后由宿主核验“现在真正在跑的是哪个 commit/version”，再执行
   `deploy/update/executor.py adopt-current` 把它记为新的安装基线（只写
   `installed.json`，不部署、不重启任何东西）。基线之外的重试/再次更新都以这条
   宿主记录为准，而不是界面上的显示。

## 9. 已知限制 / 未验证

> 本节前半段是**源码模式**通道的未验证项；镜像模式（§10）的未验证项在 §10.8。

- 真实 Docker 上源码模式的完整升级演练（备份 → 构建 → 切换 → 失败恢复）**尚未
  完成**；本文所有“能跑通”的证据都来自 stub-only 测试（docker/pnpm/curl 都是
  替身），**不能**当成真实部署通过；
- **从 edge 拓扑迁移的整条链路**（`down --remove-orphans` → 改 `.env` →
  `install.sh` → `adopt-current`）同样**没有在真实 Docker 上演练过**（见 §7.1）；
  迁移前先备份，并按 DEPLOYMENT.md §7 的第 5、6 步逐条核对；
- 升级失败后的数据库自动回滚**没有实现**，也不打算自动做（镜像模式同样：迁移单向）；
- 宿主执行器需要 root 才能创建 `/run/ling-update/*` 与备份/切换容器；
- `v0.0.1` 已发布，所以“检查更新”读到的是它而不是 `no_release`；仓库当前版本也是
  `0.0.1`，远端不比当前新 → `is_newer=false`、`update_eligible=false`
  （原因码 `not_newer`）。下一个正式版本发布后才会变成“有更新”；
- 更新通道默认关闭（`LING_UPDATE_ENABLED=0`）；**目录照常挂**，关闭时 API 仍会
  stat 两个目录、读 status 里的心跳来回答 `GET /v1/version/update` 的
  `executor.online`（`reason=disabled`），但它不写 requests，也没有任何 HTTP 入口
  能触发部署——只有显式开启通道、并由 owner 提交请求后，宿主执行器才会动手；
- 交互上借鉴了 AstrBot 的“控制台里点更新 + 状态可见”这一层体验，但**不是**
  `git pull`。研究已固定在 AstrBot v4.29.3（commit
  `f61a7f42696da6df25dc7a249ec9b8d54ed9da18`）：更新源是官方 mirror
  `api.soulter.top/releases` 的 ZIP（下载后做 CRC 校验），解包到 core/dashboard，
  用 `pip install` 装依赖，最后 `execv` 重启进程；不是从 git 拉代码。ling 只借
  它 `check / apply / progress / staging` 这层交互与状态可见性，**不照搬**它没有
  持久锁、缺少完整回滚路径的部分（ling 用跨 release 文件锁 + 阶段标记 + 人工
  分流，见 §4/§5）。参考：
  <https://github.com/AstrBotDevs/AstrBot/blob/f61a7f42696da6df25dc7a249ec9b8d54ed9da18/astrbot/core/updater.py>
  与
  <https://github.com/AstrBotDevs/AstrBot/blob/f61a7f42696da6df25dc7a249ec9b8d54ed9da18/astrbot/dashboard/services/update_service.py>。
  本文档不需要再做网络操作来复核这一点。

## 10. 镜像模式的更新通道（新协议，隔离项目已真机演练）

> **当前状态先说清楚（按位置分开说）**：
>
> - **已在隔离项目 `ling-drill` 演练**（真机，不是 stub；端口 6521、独立卷、
>   独立 `.env` 与独立根密钥）：首次安装、从真实备份恢复、**三次连续无人值守
>   升级 A→B→C→D**、维护门禁（容器 healthy 时宿主端口确实关闭）、已迁移失败
>   不回滚 / 进人工门禁、更新器 idle 重启对账。记录见
>   `shots/deploy-research/P5-DRILL.md` 与 `DRILL-DIGESTS.md`。
> - **生产 `ling` 已是镜像模式**（2026-11-05 迁移完成），但那是**一次人工迁移**，
>   不是演练：首装曾失败在 `failed_after_migration`、手工补齐后用 `ling adopt --force`
>   登记（记录里 14 项 `unknown`，`ling doctor` = ok=29 warn=14 fail=0，这些 warn 是
>   设计如此）。整个过程未受隔离项目演练影响。
> - **仍未完成**：两次连续网页升级（目前只发布过一个镜像摘要 `42bba216`，没有第二个
>   digest）；故障注入**已做**坏摘要、错仓库、不在目录、错提交、缺确认、多字段、非法
>   JSON、重放、断连重连、端口冲突、API 未认证、拉取失败、迁移跳过、备份篡改，**未做**
>   head 不一致 / 并发请求 / 旧执行器残留（这三项依赖真实升级场景）；中断中任务的真实
>   对账（只有 3 条单测 + 变异验证）；生产库有真实数据时的恢复（生产库 `messages=0`/
>   `memories=0`）；arm64 运行（镜像双架构已构建，宿主是 x86_64）；浏览器端到端点一遍。
> - **不能宣称“网页一键更新可用”**：`LING_UPDATE_ENABLED` 在镜像模式主文件里仍
>   写死 `"0"`，真实原因是**缺第二个镜像摘要、双次升级演练未完成**；
>   `deploy/releases.json` 仍是空列表，真实部署会显示
>   `no_available_target` 并禁用按钮（见 §10.8）。

### 10.1 为什么不复用 `executor.py`

源码模式的执行器（§4）下载 tag 源码、在宿主构建、再调 `install.sh`。镜像模式没有
源码树、没有 `build:`、Web 已烤进镜像，`install.sh` 构建的是另一套主文件
（`deploy/compose.yaml`）。API 侧也把这条通道挡掉了：镜像模式下
`POST /v1/version/update-requests` 在角色校验之后、读取请求体之前就返回 409
`image_mode_requires_image_channel`（见 §3 开头）。镜像模式必须走下面这套新协议。

### 10.2 请求协议（`POST /v1/version/image-update-requests`）

- `owner` 专属；必须 `Content-Type: application/json` + `X-Ling-Update: 1`，
  同源（Cookie 认证必须带匹配 `Origin`，Bearer 可省）；另外要求**最近 15 分钟内
  登录**（服务端按会话 `created_at` 判定），并按用户限流（60 秒 5 次）。
- 请求体**恰好五个键**，多一个少一个都 422：

```json
{
  "request_id": "<32 位小写 hex>",
  "target_digest": "ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>",
  "expected_current_digest": "ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>",
  "expected_current_commit": "<40 位小写 hex>",
  "operator_confirmed": true
}
```

- **只接受 digest**：`latest`、可变 tag（`:v0.0.1`）、空值、非
  `ghcr.io/qsbb/ling-api` 的仓库、非 64 位小写 hex 一律 422；API 不认 tag/URL/
  compose 片段/任意路径。
- `operator_confirmed` 必须是 JSON 的 `true`——字符串 `"true"`、`1`、缺省都拒绝
  （422 `operator_not_confirmed`）：这是“人点过确认”的唯一凭据。
- 目标摘要必须出现在更新器写出的**兼容目标**里，否则 409 `target_not_in_catalog`；
  不能绕过可信发布目录（`deploy/releases.json`）提交任意摘要。
- 期望值要与实测一致：`expected_current_commit` 对不上给
  `installed_version_changed`，`expected_current_digest` 对不上给
  `installed_image_changed`，都是 409；目标等于当前摘要给 `already_on_target`。
- 成功 **201**，只做一件事：在 `<requests>/` 里原子写
  `request-<request_id>.json`（文件里除上面五个值，只多 `schema: 1` 与
  `created_at`，都由 API 写）。**不执行任何部署**。
- 状态码口径：403 权限/来源/最近登录；409 通道与门禁；422 请求体；429 限流。
  固定原因码与文案见 `version.py` 的 `IMAGE_REASON_MESSAGES` /
  `IMAGE_REASON_STATUS` / `IMAGE_REASON_NEXT_ACTIONS`。

### 10.3 状态协议（`GET /v1/version/image-update`）

只读更新器写的 `<status>/status.json`，**逐字段白名单**投影后再返回：`image_mode`、
`enabled`、`installable`、`reason`、`message`、`next_actions`、
`current{digest,commit}`、`updater{online,state,protocol,version,updated_at,age_seconds}`、
`backup_gate`、`catalog`、`target`、`available_targets`、`task`、`request`。

- **离线就说离线**：`updated_at` 超过 120 秒、或状态文件缺失/不认识时
  `updater.online=false`，`reason` 给 `updater_offline` /
  `updater_status_missing` / `updater_status_invalid`，绝不伪装成空闲可更新。
- **恢复点门禁**读 `status.json` 顶层的 `backup_gate`（更新器扫描备份目录的结论，
  与任务无关），没有可用恢复点就是 `backup_gate_missing`；只有该字段缺失时才回退
  到 `task.backup`（兼容旧状态文件）。
- **可用目标**只来自更新器的 `available_targets` / `recommended_target`：API 不读
  发布目录（镜像模式的 API 没有 `deploy/` 挂载），也不接受调用方给的摘要。

### 10.4 更新器：独立项目 `ling-updater`，**只有它持 Docker socket**

- 独立 Compose 项目（`deploy/updater/compose.yaml`），**不随应用 compose 启停**；
  API/worker 不挂 Docker socket、不执行部署。`ling install` 只准备宿主侧前提
  （`<dir>/updater/{requests,state}`、`<dir>/updater/.env`），且**默认不拉起**它。
- 处理顺序：轮询请求目录 → **自己重新校验**（目录内兼容目标、digest 格式、拉取后
  `LING_COMMIT == OCI revision`、平台、当前运行摘要）→ 抢跨工具安装锁
  `.ling-install.lock` → 维护门禁 → 复用 `publish.py` 的预检/拉取/备份/迁移门禁 →
  把目标摘要写回受限 `.env` 的 `LING_APP_IMAGE` → 容器网络内验证 → 才发布宿主端口
  → 写**脱敏**状态文件。
- 它不信任请求里的任何东西，也不跑目标镜像里的部署脚本：发布逻辑只从发行包里那份
  只读 `publish.py` 加载。
- 私有文件（`task.json`、`journal.log`、`release-<id>.json`）留在状态目录，
  密码、原始日志、备份路径都不返回网页。

### 10.5 维护门禁（设计 §4/§5 的硬要求）

- 维护期**先停 api 与 worker**（先停写再备份），公开端口与机器人入站随之消失；
  备份改用一次性只读容器读 `app-data`（不再依赖运行中的 API）。
- 新 API 先用**临时 compose 副本**启动，所有服务的 `ports:` 都被改成 `ports: []`，
  它只存在于容器网络里；用 `docker compose config` 证明临时副本与正式副本
  「除宿主端口外完全一致」，再在**容器内**验证 `/readyz`、Web 清单与**容器内
  进程自报的 version/commit**。
  **不再打 `/v1/version`**：它挂 `CurrentAuth`，更新器/发布流程不持有用户凭据；
  也没有为探活新增免鉴权端点（等于向匿名方公开部署指纹）。HTTP 探活只剩
  `/readyz`；宿主端口那条用一次性 `--network host` 容器打，因为它必须能看见
  宿主回环。
- 全部验证（含 worker 健康、`alembic_version` 等于目标镜像 head、api/worker 同一
  镜像）通过后，才用正式 compose 重新发布宿主端口，并复核宿主 `/readyz`。
- 门禁由更新器在应用生命周期之外控制——不能靠旧 API 里的开关，也不能因为新 API
  已经绑上端口就认为验证通过。

### 10.6 失败语义

| 发生阶段 | 自动行为 | 给网页的结果 |
| --- | --- | --- |
| 预检/拉取/备份失败（**未迁移**） | 不动或恢复旧服务，并**实测**旧服务（image ID + `/readyz`）成功才宣称“仍在旧版本” | “更新未开始/未切换，仍在旧版本”；实测不通过则是 `old_service_unverified`，停在人工门禁 |
| 已迁移，目标启动或验证失败 | **不**切回旧镜像、**不**恢复旧 dump；把维护门禁**重新关上**，公开端口保持关闭 | “更新未完成，需要维护处理” + 人工恢复入口；不提供一键无条件重试 |
| 更新器重启 / 页面断连 / 状态未知 | 按持久任务 ID 与容器/DB 实况对账；不能确认就停在人工门禁，**不**新建第二个任务 | “正在确认状态”；`still_on_old_version` 为 `null` 表示**未实测**，不当成功 |
| 已成功、后续业务出问题 | 重新打开维护，由人按数据兼容性和恢复点决定 | 不展示会误导的“回退版本”按钮 |

**数据库迁移是单向的**：迁移跑过之后，旧镜像可能不再认识新 schema；恢复旧库必须
连同同一恢复点的媒体、根密钥、env 与撤回墓碑一起处理，恢复点之后写入的数据会丢。
两条通道都不做自动 schema 回滚。

### 10.7 开关（`LING_UPDATE_ENABLED` 默认 0）与打开它的前置条件

- 默认 0；镜像模式主文件 `deploy/compose.image.yaml` 里这一项是**字面量 `"0"`**，
  **只改 `.env` 打不开它**——这是有意的：挂上目录 ≠ 打开开关，避免“开关开了却没
  人执行”。
- **不要把两条通道的开关混为一谈**：镜像模式的主文件把 `LING_UPDATE_ENABLED`
  写死为 `0`；源码模式的旧执行器 `deploy/update/executor.py` 会在它自己拉起的
  `install.sh` **子进程环境**里设 `LING_UPDATE_ENABLED=1`（源码模式通道自己的
  开关，只影响那个子进程，不写宿主 `.env`）。两者不共用同一套开关。
- 打开前必须同时满足（设计 §7 第 2 条）：**先在隔离项目完成一次新装 + 两次连续
  网页升级 + 更新器重启后对账**，并且更新器在线、`protocol=1` 兼容、备份/恢复门禁
  齐全、任务失败路径演练通过。**隔离项目 `ling-drill` 已完成**：一次新装、
  三次连续升级（A→B→C→D）、更新器 idle 重启对账；**未完成**的是中断中任务的
  真机对账（仅单测）。**生产侧另有一条独立原因**：目前只发布过一个镜像摘要
  （`42bba216`），没有第二个 digest，两次连续网页升级在真实发布上走不完。
  两条原因都在，所以开关**仍然保持 0**。
- 打开还需要更新器镜像来自发布证据（`--updater-digest` / `LING_UPDATER_IMAGE`），
  以及 `LING_UPDATE_SHARED_GID` 对应的目录权限（`requests` 带 setgid、属组是它）。
- **要真正有可用目标，还必须先做正式发布**：CI 构建并推送镜像后，把条目用
  `python3 deploy/release/catalog.py add ...` 写进发布目录（`deploy/releases.json`
  的 schema 1 列表），随发行包/挂载交给更新器。仓库里这份文件仍是空骨架
  `{"schema": 1, "releases": []}`；只打开开关不会凭空出现目标。

### 10.8 现在真能做什么、还不能宣称什么

- **能**：源码模式通道按 §7 人工升级；镜像模式按
  [DEPLOYMENT.md §11](DEPLOYMENT.md#11-镜像模式部署推荐) 人工跑
  `publish.py <digest>` 升级。**在隔离项目 `ling-drill` 上**，网页更新通道的
  完整链路（请求 → 更新器 → 备份 → 迁移 → 维护门禁切换 → 三项核验）已真机
  跑通三次连续升级；但那条演练用的是**宿主上手写的演练发布目录**
  （`/vol1/@appdata/ling-drill/deploy/releases.json`，3~4 条测试构建摘要），
  **不是仓库里的 `deploy/releases.json`**。
- **不能宣称**：网页一键更新在生产/真实部署里可用。原因全是事实层面的：
  - **缺第二个镜像摘要**：生产只发布过 `42bba216` 这一个 digest，第二次连续网页升级
    无法演练（生产本身已于 2026-11-05 迁到镜像模式，见 §10 开头）；
  - 镜像模式里开关还没打开（设计门槛的"中断中任务对账"未真机证明，head 不一致 /
    并发请求 / 旧执行器残留的故障注入也未做）；
  - `deploy/releases.json` 目前是空列表（`{"schema": 1, "releases": []}`），
    所以**当前仓库产物下的真实行为**是：`GET /v1/version/image-update` 给出
    `no_available_target`、网页按钮禁用。这是**诚实可用**（通道代码就绪、
    没有可用目标），不是 bug；要出现目标，先有正式发布并把条目 `catalog.py add`
    进去。
- **待现场确认**：GHCR package 可见性、宿主架构、真实部署路径与端口、更新器在
  目标 NAS 上的 Docker socket/gid 对应关系（`ling install` 会 `stat` socket 取 gid，
  但实际取值未在现场核对过）。

实现细节以 `deploy/update/executor.py`、`deploy/updater/updater.py`、
`deploy/release/publish.py`、`src/ling/updates/protocol.py` 与
`src/ling/api/routes/version.py` 为准；本文若与代码不一致，按代码修文档。
