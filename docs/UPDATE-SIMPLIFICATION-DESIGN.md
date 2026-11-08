# 镜像更新简化设计（设计稿，不含实现）

## §1 结论

用 **GHCR 的稳定版 tag 列表发现版本、用 registry manifest digest 固定安装物**，删除 `deploy/releases.json` 可信目录和 `min_from`。
用户点「检查更新」会真正查询 GHCR；可更新时展示版本、更新时间和更新内容，点「更新」仍执行备份、迁移、切换、校验。
GHCR 不通时明确说「这次查不到」，不把旧缓存说成「已是最新」；GitHub Release 不通只影响更新说明。
以下“现状”来自本仓库代码（2026-11-09 工作树，含未提交改动），外部项目事实与推断分开写。

## §2 同类项目调研对比

表中“失败行为”只写文档或源码能确认的部分；“未确认”不代表项目没有该能力。
“信任锚”指额外的发布版本白名单，不把固定仓库地址、TLS 和 registry 权限误称为白名单。

| 项目 | 事实源 | 版本比较/可更新规则 | 更新说明来源 | 失败时用户可见行为 | 额外信任锚/白名单 |
| --- | --- | --- | --- | --- | --- |
| Watchtower | **事实**：轮询 Docker/OCI registry 中被选中容器的镜像。[W1] | **事实**：`--interval` 控制周期，`--label-enable` 只处理显式标签容器；判断所跟踪 tag 的新镜像，非 SemVer 排序。[W1] | **事实**：通知功能报告更新结果；逐版 Release 正文并非必要输入。[W2] | **事实**：有通知与日志；网页提示不适用（无此类 Web 更新页）。[W2] | **事实**：标签是容器选择开关，不是版本目录；未见额外版本白名单。[W1] |
| Diun | **事实**：`watch_repo` 从 registry 取 tags；Docker Hub 使用 registry manifest GET，须注意限流。[D1][D2] | **事实**：`sort_tags=semver` 可按版本排序；它只通知变化，不安装应用。[D2] | **事实**：通知模板含镜像/tag 等元数据；自动取应用 changelog 未确认。[D2] | **事实**：失败可从日志/通知渠道排查；无安装按钮，页面文案不适用。[D1] | **事实**：watch 配置/过滤是监控范围，不是发行白名单。[D1] |
| Home Assistant Supervisor | **事实**：Supervisor 版本状态与更新实体；组件与渠道决定信息源。[H1][H2] | **事实**：`version`、`version_latest`、`update_available` 表达可更新状态，非简单取所有 tag 最大值。[H1][H2] | **事实**：更新实体有 release notes / changelog 入口；组件 URL 不一。[H2] | **事实**：UI 显示更新实体状态；断网确切句子未确认。[H2] | **推断**：Supervisor 通道/仓库元数据是受控源，但不是手写跨度目录。[H1] |
| Gitea；Immich | **Gitea 事实**：官方发布页/升级文档；**Immich 事实**：应用版本检查与发布信息。[G1][I1][I2] | **Gitea**：管理员选发布版并手动升级；未核实内建 tag→digest 一键判定。**Immich**：有版本检查；比较细节未确认。[G1][I2] | **Gitea**：Release/升级文档。**Immich**：Release Notes/升级说明。[G1][I1][I2] | **Gitea**：统一应用内失败提示未确认。**Immich**：断网精确 UI 文案未确认。 | **推断**：两者未找到 `min_from` 式手维护白名单；Immich 版本服务是信息源，不等于签名锚。[G1][I2] |
| Nextcloud；Vaultwarden | **Nextcloud 事实**：更新服务器/渠道。**Vaultwarden 事实**：官方 Docker 更新说明与 GitHub Releases。[N1][N2][V1] | **Nextcloud**：按 release channel 查更新，updater 执行。**Vaultwarden**：拉镜像重建；应用内比较算法未确认。[N1][N2][V1] | **Nextcloud**：更新服务/发布说明链接。**Vaultwarden**：GitHub Releases。[N1][V1] | **Nextcloud**：管理页/updater 报告失败，精确句子未确认。**Vaultwarden**：一键更新页失败文案未确认。[N2][V1] | **Nextcloud**：受控更新服务与渠道。**Vaultwarden**：未发现额外版本跨度白名单。[N1][V1] |
| AstrBot；MaiBot | **事实**：两者有 GitHub Releases；AstrBot 还提供插件管理/更新，须与宿主更新区分。[A1][A2][M1] | 两者宿主版本比较实现均**未确认**；MaiBot 用 Releases API 检查这一前提未从源码坐实。[A1][M1][M2] | 两者 Release 页有正文；应用内是否读取同一来源**未确认**。[A1][M1] | GitHub 不可达时的确切用户文案**未确认**。 | 未查到本项目的 `min_from` 目录；**不能据此断言完全不校验**。[A1][M1] |
| Flux；Argo CD Image Updater | **Flux 事实**：ImageRepository 扫 registry tags。**Argo 事实**：Image Updater 查 registry tags。[F1][R1] | **Flux**：ImagePolicy 可按 SemVer 选 tag、反映 digest。**Argo**：支持 `semver`/`digest` 策略，后者适合可变 tag。[F1][R1] | 两者不以应用 changelog 为镜像选择必需输入；逐版说明需应用另取（**推断**）。 | 控制器记录条件/事件；本项目页面文案需自行设计（**推断**）。 | 两者配置仓库范围/策略；未见每版手写 `min_from`。[F1][R1] |

借鉴点只有三项：固定仓库与稳定版选择规则、registry digest、把“发现更新”和“执行更新”分开。
Watchtower 的自动替换容器、GitOps 控制器的持续写回都不适合本项目要求的人工点击与可回滚迁移。

## §3 选定方案的数据流

当前代码：`ImageUpdateChannel` 只读 `status.json` 并写请求文件；更新器持有 Docker 能力，当前版本来自运行中 API 镜像的 OCI `version` 标注。[L1]
当前更新器通过目录决定目标，`docker manifest inspect ghcr.io/qsbb/ling-api:v<version>` 现场解析本机平台 digest；摘要本来就不在目录里。[L2]
当前发版链在 `vX.Y.Z` tag 的 CI/镜像成功后发布 GHCR 镜像和 GitHub Release，镜像带 `version`、`revision` 标注。[L3]

拟改为：浏览器 → API 写 `refresh-<id>.json` → 更新器查询 GHCR → 原子写脱敏 `status.json` → API 投影给浏览器。
保留现有 API/更新器职责边界；API 不新增 Docker socket，也不由浏览器直连 GHCR。
选择更新器查询，因为它所在的容器/宿主 Docker CLI 是实际拉镜像的一侧；API 容器出网成功不证明更新器能拉取。
检查请求单飞；同一实例同时只查一次；手动触发短节流（建议 10 秒），周期检查可保留但不能替代手动即时检查。
一次检查设总超时（建议 10–15 秒）、响应大小上限、tag 数上限；记录 `checked_at`、来源、失败码和本次检查 ID。
只接受固定 `ghcr.io/qsbb/ling-api`，不让请求传 registry URL、镜像名或任意 tag。

首选 registry v2 流程：`GET /v2/qsbb/ling-api/tags/list`；若收到 `401` Bearer challenge，只向 challenge 指定的 GHCR 授权服务申请 `repository:qsbb/ling-api:pull` token 后重试。[O1][O2]
公开包匿名拉取可行，但**匿名 token 能否在目标 NAS 网络实际取到**须验收；私有包需要只读包凭据，不能把 token 给网页。[O2]
`tags/list` 比 GitHub Releases API 少一个站点依赖，且它与最终 `docker pull` 共用 GHCR 出口；但 GHCR 也可能在 NAS 上不可达，必须现场测，不能推断内网一定稳定。
GitHub Releases API 的优点是直接给 `tag_name`、`body` 等发行信息；缺点是它不证明 GHCR 中该 tag 已存在，匿名 REST API 通常受每 IP 每小时 60 次主限额约束。[G2][O2]
私有仓库/包要分别配置 GitHub 仓库读取权限与 GHCR 包拉取权限，两套认证不能互相假定；若 GHCR 私有，则无凭据的匿名查询明确报认证失败。[O2]
tags/list 可能分页，必须处理 `Link` 后续页并设最大页数；只有获取完整结果才可声称“已是最新”。[O1]
过滤后选最高的 `vMAJOR.MINOR.PATCH`，忽略 `latest`、`sha-*`、预发布和所有非规范 tag。
目标解析：对选定 tag 发 `HEAD /v2/qsbb/ling-api/manifests/<tag>`，带 OCI index、Docker manifest list、单平台 OCI/Docker manifest 的 `Accept`；取 `Docker-Content-Digest`。[O1]
该 header 返回的是 tag 当前所指的 **index digest 或单平台 manifest digest**，取决于响应表示；请求与复核须保持相同 `Accept`。
若 HEAD 在实际 GHCR/代理上不可靠，可对同一 URL 用 GET 并限流读取；不从 `docker manifest inspect` 的 JSON 猜顶层 index 摘要。
多架构 index 再核存在宿主平台（本项目发布 `linux/amd64`、`linux/arm64`）；单平台 manifest 则核配置平台或拉取后核对。[L3][O1]
检查返回候选版本、解析出的 digest、平台、`checked_at`、当前版本、说明状态；不返回认证信息或原始异常。
用户点击安装时 API 仍只提交固定 digest 请求；更新器在 `prepare` 前重查 **该版本 tag → digest**，与请求 digest 相同才继续。
页面已有候选时，点一次「更新」即可触发上述执行前重查与安装，不要求先另点「检查更新」；候选过期时停下并提示重查。
命令行入口可设计成一次 `ling update`：先执行同一检查逻辑、展示版本/说明、确认后提交同一 digest 安装请求；无交互环境需显式确认参数，避免意外升级。
随后以 `ghcr.io/qsbb/ling-api@sha256:…` 拉取/安装，核本地 image ID、平台、`version`/`revision` 与期望一致，不用可变 tag 覆盖运行容器。
若 tag 在检查与安装之间漂移：拒绝旧请求并请用户重查；不能静默改用新 digest。
单靠 GHCR 可以完成“检查 + 更新”；以当前镜像标注，**不能**可靠取得尚未拉取版本的逐版更新内容。

## §4 版本比较与“可更新”判定

版本只接受严格 SemVer 核心 `X.Y.Z`，tag 必须精确 `vX.Y.Z`；建议禁止前导零和构建后缀，避免等价 tag 歧义。[S1]
当前版本只读运行中 API 容器所用镜像的 `org.opencontainers.image.version`；`revision` 用于发版身份核对。[L1][L3]
读不到当前标注、容器不在运行或标注不合规：状态为“无法确认当前版本”，禁止一键安装；不拿 `.env`、包版本或目录猜。
候选只有严格大于当前版本才“有新版”；相同/更旧为“已是最新”（前提是 registry 查询完整成功）。
再满足：目标 digest 已解析、宿主平台可用、更新器在线、无并行任务、恢复点门禁通过、磁盘预检通过，才“可安装”。
“有新版”与“可安装”分开：备份条件不满足时仍能告诉用户有新版，但按钮不能执行。
去掉 `min_from` 后，跨版本升级的依据是**迁移必须支持从任意已支持的旧 schema 前向到目标 schema**，且执行前做一致备份。
这不是无条件安全保证：发版 CI 应用最旧支持版本的数据库快照跨级升级，并验证回滚恢复；缺样本/失败就不发布稳定版。
运行时迁移失败停在维护态并从备份回滚；回滚未验证成功则保持人工门禁，不能宣称成功。
如果产品确实停止支持某个旧 schema，应由迁移器在备份后给出明确“不支持此库版本”，而非恢复手写 `min_from` 列表。

## §5 更新说明来源与降级链

首选按选中 **精确 tag** 读 GitHub Releases API 对应 Release 的 `body`，不使用 `releases/latest` 代替所选版本。[G2]
Release body 只作展示，不是镜像摘要或发布身份的信任源；HTML/Markdown 渲染做清理、长度限制与纯文本回退。
只查 GHCR 时，当前已有 `version`/`revision` 两项 OCI 标注，无更新内容；页面应写“版本可更新，更新内容暂时取不到”。[L3]
可选第二层：未来在镜像的 OCI index annotation 或镜像配置 label 放**简短、由 CI 从 `CHANGELOG.md` 提取的摘要**；这不是手写白名单，须实际验证 GHCR 暴露与长度限制。[O3]
若说明仅在镜像配置 label，未拉镜像时需通过 registry 取 config blob；不能声称 HEAD manifest 就能拿到 Dockerfile `LABEL`。
第三层：更新器发行包内随镜像带 `CHANGELOG.md`，只可展示**本机已安装版本**及更早内容；不能用旧安装包承诺未来版本说明。
现有 `deploy/update-notes.json` 可在过渡期只读显示旧已知版本，但新版本不再要求维护它，也不影响更新资格。[L2]
`raw.githubusercontent.com` 上的 CHANGELOG 或单独 notes 文件不进入必经链；raw 不通时不能用它作唯一说明源。
本地导入目标镜像时，可从导入镜像 label/随包 CHANGELOG 取说明；没有就提示“更新内容暂缺”，仍可在 digest 校验后更新。

## §6 失败语义

| 原因 | 给用户的一句话 | 下一步 |
| --- | --- | --- |
| GHCR 不可达/认证失败 | “现在连不上镜像仓库，暂时查不到有没有新版。” | 稍后重试；检查 NAS 到 GHCR；已下载镜像可走本地导入。 |
| tags/list 不完整或被限流 | “这次没查完整，暂时不能判断是不是最新版。” | 按服务端重试时间再试；配置只读凭据或检查代理。 |
| tag 存在，manifest 取不到 | “发现新版本，但暂时取不到它的安装包摘要。” | 稍后重查，或检查该版本镜像是否发布完整。 |
| 本机平台不存在 | “新版本没有适合这台机器的镜像。” | 让发布方补对应架构镜像。 |
| 当前版本标注缺失 | “无法确认现在运行的版本，暂时不能一键更新。” | 检查当前镜像来源/标注，必要时按人工升级流程处理。 |
| 本机没有目标镜像且 GHCR 不通 | “本机还没有新版镜像，也无法下载。” | 网络恢复后重试，或导入带可核对 digest 的镜像包。 |
| 磁盘不足 | “空间不足，更新尚未开始。” | 清理磁盘并保证镜像、备份与回滚余量后重试。 |
| 备份/迁移/校验失败 | “更新没有完成，正在恢复旧版本。” | 看恢复结果；失败时保持维护态并按诊断处理。 |
| GitHub Release API 不通 | “已找到新版本，更新内容暂时取不到。” | 仍可按已锁定 digest 更新；稍后再看说明。 |
| 检查后 tag 的 digest 改变 | “安装包已变化，请重新检查更新。” | 重查并重新确认，不自动替换目标。 |

离线导入不是“信任任意 tar”：要求导入镜像的 RepoDigest/清单 digest 可核、版本与 revision 标注一致，并沿用原有备份迁移门禁。
完全离线且没有预先获得可核对 digest 时，只能列为人工安装路径，不能把镜像 tag 当可信摘要。

## §7 迁移与兼容

已安装 0.0.3 的旧更新器只认识旧目录与 `request-*`，不认识新发现逻辑；新 API 单独部署**不能**让旧更新器学会查 tags。[L2]
过渡发一版“桥接”发布：旧 `releases.json` 暂保留**已有旧更新器可读的固定字段**，只放通向桥接版的条目；不新增字段、不让新更新器依赖它。
但旧机若本地目录只到 0.0.3，且 GitHub raw 一直不通，网页按钮仍无法升级到桥接版；必须提供一次性、文档化的宿主引导命令。
引导命令在宿主上安装/替换**新更新器**及其固定发行包，先验证 Docker/compose/目录权限，保留现有持久 state、请求队列、回滚材料；然后让新更新器查 GHCR。
引导命令自身的更新器镜像也须以已取得的 digest 固定；下载失败可用本地导入包，不能以 `latest` 作为引导信任来源。
桥接版之后，确认在 0.0.3 → 桥接版及新装机路径均可成功，再删除目录及旧兼容代码；不能在旧安装仍依赖目录时提前删远端文件。
旧 API 若只识别 `release_catalog` 状态，可让新更新器短期写同 schema 的**只读兼容字段**（标注来源 registry），但旧 API 的目录原因码不能伪造为“目录正常”。
优先同步交付新 API 与新更新器；协议不兼容时明确提示“需要先更新更新服务”，不把空列表说成最新。
旧请求文件只在新更新器明确校验 digest、当前镜像身份和请求权限后继续处理；无法判明来源的请求拒绝并要求重新点击。

## §8 删除与保留清单

| 对象 | 处置 |
| --- | --- |
| `deploy/releases.json` | 桥接期只供旧更新器读取；完成旧机迁移验收后删除文件、挂载、默认 raw URL 与文档叙述。 |
| `deploy/release/catalog.py` | 删除目录解析、`RELEASE_KEYS`、`min_from`、`has_migration` 和 `catalog.py validate`；若还承载 notes 解析，迁出最小展示解析后删除。 |
| 更新器目录查询/校验 | 换成固定 GHCR tags/manifest 查询与执行前 digest 复核；保留 `status.json`、单飞请求、阶段/回滚/维护门禁。 |
| API `ImageUpdateChannel` | 删 `not_in_min_from`、`catalog_*`、`target_not_in_catalog` 等目录 reason 与 UI 文案；保留旧 schema 只读兼容窗口。 |
| `has_migration` 字段 | 从目标与推荐目标移除；迁移是否需要由实际迁移器判断，不能依赖发布目录人工布尔值。 |
| 目录相关测试 | 删除仅验证 schema/权限/min_from 的用例；替换为 tags 分页/过滤、401 token、manifest Accept/digest、漂移、离线及跨版迁移验收。 |
| `deploy/update-notes.json` | 过渡期可选只读；明确不作为目标清单，后续在 Release/OCI 说明链稳定后删除。 |
| 源码模式更新 | 本设计不更改 `check-release` 与源码更新协议，避免误伤另一条通道。 |

## §9 分阶段实施与可程序化验收

1. **阶段 A：发现层**。在更新器中实现固定仓库 tags/list（含分页、Bearer token）、严格 SemVer 和 HEAD/GET manifest；此阶段不开放安装。
   验收：fixture 覆盖 `latest`/预发布/乱序/分页/401/429/超时、index/单平台与缺架构；只有全量查询成功才能返回 `latest`。
2. **阶段 B：安装层**。接现有刷新与安装请求，执行前复核版本 tag 指向的 digest，固定 `@sha256:` 交给原有流水线。
   验收：模拟 tag 漂移拒绝；Docker 调用记录中安装引用无 `:vX.Y.Z`；备份→迁移→切换→校验、失败回滚测试通过。
3. **阶段 C：说明与页面**。精确 tag 的 GitHub Release body 只用于展示；来源失败、空正文、超限内容均降级为固定人话。
   验收：断开 GitHub 但 GHCR 可达时仍显示可更新；断开 GHCR 时不显示“已是最新”；所有失败码映射一句话与下一步。
4. **阶段 D：旧机桥接与清理**。先验证旧 0.0.3 通过旧目录或一次性引导进入新更新器，再删除目录与相关 CI/Dockerfile 绑定。
   验收：0.0.3 冷启动副本、旧缓存目录、raw 断网三种场景均能通过引导命令到新检查链；新装机无 `releases.json` 仍完整更新。
5. **阶段 E：真实 NAS 冒烟**。在与用户同类的 NAS 出口实测 GHCR 匿名 token、tags/list、HEAD/GET、镜像 pull 与 GitHub API；记录各自耗时和失败码。
   验收：单击检查产生真实 registry 请求且返回时间/来源；本地导入路径在断网时仍以 digest 完成一次可回滚演练。

## §10 不做什么

- 不维护新的 JSON 版本白名单、`min_from` 图、`has_migration` 人工开关或“可信目录 2.0”。
- 不拿 `latest` 或 GitHub Release 正文里的 digest 决定安装物；Release 可编辑，只能提供说明。
- 不让前端/API 任意输入 registry URL、tag 或 digest；不把 Docker socket 给 API。
- 不引入 Watchtower 式无人值守自动重启、Flux/Argo 控制器、额外发布服务或签名基础设施。
- 不为更新说明阻断安装；也不把旧缓存、旧本地目录或旧 CHANGELOG 误报为已完成的实时检查。

## 出处（外部资料为官方文档或项目源码；访问日期 2026-11-09）

- [W1] Watchtower [Arguments](https://containrrr.dev/watchtower/arguments/)（`--interval`、`--label-enable`）；[W2] [Notifications](https://containrrr.dev/watchtower/notifications/)。
- [D1] Diun [Docker provider](https://crazymax.dev/diun/providers/docker/) 与 [registry options](https://crazymax.dev/diun/config/regopts/)；[D2] [FAQ：Docker Hub / watch_repo](https://crazymax.dev/diun/faq/#tags-sorting-when-using-watch_repo) 与 [通知配置](https://crazymax.dev/diun/config/notif/)。
- [H1] Home Assistant Supervisor [API endpoints](https://developers.home-assistant.io/docs/api/supervisor/endpoints/)；[H2] Home Assistant [Update entity](https://contributors.home-assistant.io/core/entities/update/)。
- [G1] Gitea [Upgrade from Gitea](https://docs.gitea.com/installation/upgrade-from-gitea)；[G2] GitHub [REST releases API](https://docs.github.com/en/rest/releases/releases#get-a-release-by-tag-name)。
- [I1] Immich [Upgrading](https://docs.immich.app/install/upgrading/)；[I2] [Releases 源码/说明](https://github.com/immich-app/immich/releases)。
- [N1] Nextcloud [更新检查配置](https://docs.nextcloud.com/server/stable/admin_manual/configuration_server/config_sample_php_parameters.html)；[N2] [Update 文档](https://docs.nextcloud.com/server/stable/admin_manual/maintenance/update.html)。
- [V1] Vaultwarden [README/部署说明](https://github.com/dani-garcia/vaultwarden) 与 [Releases](https://github.com/dani-garcia/vaultwarden/releases)。
- [A1] AstrBot [Releases](https://github.com/AstrBotDevs/AstrBot/releases)；[A2] [项目源码](https://github.com/AstrBotDevs/AstrBot)。
- [M1] MaiBot [Releases](https://github.com/Mai-with-u/MaiBot/releases)；[M2] [项目文档](https://docs.mai-mai.org/)。
- [F1] Flux [ImagePolicy](https://fluxcd.io/flux/components/image/imagepolicies/)；[R1] Argo CD Image Updater [Update strategies](https://argocd-image-updater.readthedocs.io/en/stable/basics/update-strategies/)。
- [O1] OCI [Distribution Specification](https://github.com/opencontainers/distribution-spec/blob/main/spec.md)；[O2] GitHub [Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry) 与 [REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)；[O3] OCI [Image specification: annotations](https://github.com/opencontainers/image-spec/blob/main/annotations.md)。
- [S1] [Semantic Versioning 2.0.0](https://semver.org/spec/v2.0.0.html)。
- [L1] 本仓库 `src/ling/api/routes/version.py`；[L2] `deploy/updater/updater.py`、`deploy/release/catalog.py`、`deploy/releases.json`；[L3] `.github/workflows/release.yml`、`deploy/Dockerfile.api`。
