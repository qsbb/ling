# ling 独立更新器镜像（linux/amd64 + linux/arm64）
#
# 它跑什么：`deploy/updater/compose.yaml` 里那个**独立**的 `ling-updater` Compose
# 项目——常驻轮询请求目录、复用发行包里的 `deploy/release/publish.py` 做发布门禁。
# 镜像里没有应用业务代码，也不 `pip install`：`updater.py` / `publish.py` /
# `catalog.py` 全都只用标准库（见各自文件头的 import 列表）。
#
# 基础镜像：**两段**。`docker:29.9.3-cli` 只作 docker CLI 三件套的来源
# （`COPY --from`），运行时基镜像是 `python:3.13-alpine3.24`。
#
# 为什么不再直接拿 `docker:29.9.3-cli` 当运行时基镜像（2026-11 CI 实跑翻车）：
#   那个镜像基于 `alpine:3.24`，`apk add python3` 现在是 **3.14**。而仓库 CI
#   （`.github/workflows/ci.yml` 的 matrix）只覆盖 **3.12 / 3.13**——一个没被 CI
#   跑过的解释器就是「没验证过的代码」，而设计 §4 要的恰恰是「更新器只保留最小
#   可信代码」。它也确实当场翻车：3.14 的 argparse 在**构造 parser 时**就校验
#   help 字符串，`catalog.py validate`（updater-image job 的内容核对那一步）直接
#   炸掉；根因是 `deploy/release/catalog.py` 的 `--released-at` help 里带了裸 `%`。
#   alpine 3.24 没有 `python3.13` 包（`dl-cdn.alpinelinux.org` 的 v3.24 APKINDEX：
#   main 只有 `python3=3.14.10-r1`，community 无 python3*；2026-11-04 核实），
#   所以「把 Python 钉在 CI 覆盖的版本」只能换基镜像，不能靠 `apk add python3.13`。
#
# 两段怎么拼、为什么这样拼：
#   1) 更新器仍硬依赖三样：docker CLI、compose **插件**（`docker compose` 子命令）、
#      python3。docker CLI + buildx + compose 插件照旧由上游**同一份精确版本**
#      `docker:29.9.3-cli` 提供，构建期用 `COPY --from` 取：**不下载、不自算
#      sha256、不按 apk 架构映射 URL**。所以「CLI 与 compose 插件版本错配」这个
#      构建期看不出来、跑到 `docker compose config` 才炸的问题仍然不存在。
#   2) `COPY --from=<image>` 在多平台构建里按**目标平台**解析该镜像（amd64/arm64
#      各取对应层），所以 `buildx --platform linux/amd64,linux/arm64` 拿到的仍是
#      对应架构的二进制，构建脚本里不需要任何架构分支。官方 `library/docker`
#      清单里 `29.9.3-cli` 的架构是 amd64/arm32v6/arm32v7/arm64v8，覆盖 CI 两个平台。
#   3) 运行时基镜像是官方精确 tag `python:3.13-alpine3.24`：Python 显式钉在
#      **CI 覆盖的 3.13**，alpine 分支与 docker CLI 镜像**相同**（都是 3.24），
#      不混 musl 世代；与 `deploy/Dockerfile.api` 的 python 3.13 同一条线。
#      仍然不用 `latest` / `3-alpine` 这类浮动 tag。
#   4) 「钉住」不只是注释里的承诺：下面的 RUN 在**构建期**断言
#      `(3,12) <= sys.version_info[:2] <= (3,13)`。将来谁把基镜像升到 3.14，
#      构建立刻红，逼着先去扩 CI 矩阵，而不是等某次发版在真机上才发现。
#
# 不放什么：`.env`、任何密钥、docker socket。socket 只能**运行时**挂载（见
# `deploy/updater/compose.yaml`），镜像里放一个 socket 既造不出来也没意义；
# `.dockerignore` 已经在构建上下文层面挡掉 `.env` / `.env.*` / `*.pem` / `*.key`。
#
# 容器内布局：故意与发行包 `deploy/` 目录的相对路径一致，这样 compose 里的
# `LING_UPDATER_PUBLISH_PY=/srv/ling-code/release/publish.py`、
# `LING_UPDATER_BACKUP_SCRIPT=/srv/ling-code/backup/backup.sh` 不做任何额外映射
# 就能用（`publish.py` 又按 `Path(__file__).parent.parent` 推 `deploy_dir`，
# 所以 `release/`、`backup/` 这两级目录名不能改）：
#   /app/updater.py
#   /srv/ling-code/release/{publish.py,catalog.py}
#   /srv/ling-code/backup/{backup.sh,restore_drill.sh}
#   /srv/ling-code/releases.json
#
# 为什么**不**拷 `deploy/compose.image.yaml`：更新器从宿主只读挂载拿**已部署的**
# compose 文件组（`LING_COMPOSE_FILE`），而 `publish.py` 的默认 `--compose-file`
# 恰好是 `<deploy_dir>/compose.image.yaml`。把模板拷进来，只会让「不带参数手工跑
# publish.py」静默去校验一份模板而不是真实拓扑；宁可让它明确报文件不存在。
# 同理不拷 `deploy/web_manifest.py`：那是 `publish.py` 在 **api 容器**里
# （`/app/deploy/web_manifest.py`）跑的东西，更新器自己不用。
#
# 为什么带上 `restore_drill.sh`：更新器自己不调用它，但它是这套发行包里的恢复
# 演练入口；镜像带着同一份代码，维护窗口里可以直接
# `docker run ... -e LING_BACKUP_VIA_COMPOSE=1 bash /srv/ling-code/backup/restore_drill.sh <dump>`
# 对挂进来的备份做演练，不必再从宿主导第二个脚本进去。
# 注意它只有 **compose 模式**能在这个镜像里跑（走 `docker compose exec` 进 db
# 容器）；直接模式需要本机 `psql`，本镜像**不装** postgresql-client——更新器
# 的发布路径只用 compose 模式，多装一个版本要跟服务端对齐的客户端没有收益。

# 阶段 1：docker CLI 三件套（CLI + buildx + compose 插件）的唯一来源。
# 只作 `COPY --from` 的来源，不进最终镜像；它基于 alpine:3.24，与下面的运行时
# 基镜像同一个 alpine 世代。
FROM docker:29.9.3-cli AS dockercli

# 阶段 2：运行时。`python:3.13-alpine3.24` = CI 覆盖的 3.13 + 同世代 alpine 3.24。
# 小版本（3.13）刻意钉死、补丁级（3.13.x）随上游走：补丁要收安全修复，而"这个
# 运行时到底可不可信"的分界线在小版本。换成别的小版本前，先扩
# `.github/workflows/ci.yml` 的 matrix——下面的构建期断言会拦住没被 CI 覆盖过的运行时。
FROM python:3.13-alpine3.24

# 构建身份：CI 从同一次运行的 commit / version / server_url 传入，写进 OCI 标注。
# `updater-image` job 会拉回镜像、读标注并与 `github.sha` 核对（与 api 镜像同一套
# 做法）；默认值只是让本机 `docker build` 不传参也能构建，不代表一次真实发布。
ARG LING_COMMIT=unknown
ARG LING_VERSION=unknown
ARG LING_SOURCE=https://github.com/qsbb/ling

LABEL org.opencontainers.image.title="ling-updater" \
      org.opencontainers.image.description="ling 独立更新器：docker CLI + compose 插件 + python3，非 root" \
      org.opencontainers.image.source="${LING_SOURCE}" \
      org.opencontainers.image.version="${LING_VERSION}" \
      org.opencontainers.image.revision="${LING_COMMIT}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later"

# docker CLI 与 compose 插件：从阶段 1 取**同一份**上游精确版本的二进制
# （Go 静态链接，alpine/musl 之间可以直接搬）。落点必须是 docker CLI 的默认
# 插件搜索路径 `/usr/local/libexec/docker/cli-plugins`——只有这样
# `docker compose` 才被识别成**插件**（而不是去 PATH 里找一个独立的
# `docker-compose` 命令，那正是版本错配的来源）。
# COPY 保留源文件的权限位（0755），不需要再 chmod。
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/

# 运行时依赖，只补 python 基镜像没有的三样：
#   bash       —— `backup.sh` / `restore_drill.sh` 的 shebang 是
#                 `#!/usr/bin/env bash`（alpine 基础镜像只有 busybox sh）；
#   coreutils  —— 备份脚本要 `sha256sum`/`stat` 等；busybox 也有同名 applet，
#                 但语义与 GNU 版有差异，镜像里给 GNU 版，让它与 CI（Ubuntu）和
#                 常见宿主机跑的是同一套实现。
#   ca-certificates —— 原 `docker:*-cli` 基镜像自带（它自己 `apk add` 过）；
#                 换成 python 基镜像后显式要回来，免得将来某次 HTTPS（例如人工
#                 exec 进去排查 registry 认证）撞上空 CA 库。
# 刻意**不装** openssh-client / git：原基镜像带它们是为了支持 `DOCKER_HOST=ssh://`，
# 而更新器的 `DOCKER_HOST` 在下面被固定成 unix socket，装着一整套 ssh/git 只是把
# 「没用到但可执行的代码」留在镜像里。
#
# 构建期自检：任何一个缺失/不可执行就立刻失败，而不是等到宿主上第一次
# `docker compose pull` 才发现。`docker compose version` 不需要 daemon，能跑。
#
# Python 断言是**版本闸门**，不是"能 import"的烟测：
#   - 必须落在 CI 覆盖区间 `(3,12) <= v <= (3,13)`（pyproject 的 requires-python
#     下限 3.12，CI matrix 3.12/3.13）；
#   - 越界就让**构建**失败：基镜像 tag 写错、被上游重推成 3.14、或有人顺手把
#     tag 升到 3.14，错误都停在 buildx 这一步，而不是变成"更新器用了没验证过的
#     运行时"这种运行期才发现的缺陷；
#   - `test ! -e /usr/bin/python3` 与 PATH 断言防"镜子里有两个 Python"：
#     alpine 的 `apk add python3`（=3.14）落在 /usr/bin，而 PATH 里 /usr/local
#     优先，版本断言仍会绿，但解释器已不可信——这正是本次缺陷的起点。
RUN apk add --no-cache bash coreutils ca-certificates \
 && python3 -c 'import sys; assert (3, 12) <= sys.version_info[:2] <= (3, 13), f"镜像 Python {sys.version_info[:2]} 不在 CI 覆盖范围（3.12/3.13）；请先扩 .github/workflows/ci.yml 的 matrix，再改基镜像"; print(sys.version)' \
 && test ! -e /usr/bin/python3 \
 && test "$(command -v python3)" = /usr/local/bin/python3 \
 && bash --version | head -n 1 \
 && docker --version \
 && docker compose version

# 非 root：固定 UID/GID 10002。**刻意**不复用 API 的 10001：请求目录是两边唯一的
# 共享写面，共用 uid 会让「API 能写」与「更新器能写」在宿主上无法再区分。
# GID 10002 只是镜像内的默认主组，**不是**宿主 docker socket 的组——socket 的
# gid 因发行版而异（systemd 常见 998/999/994）。原 `docker:29.9.3-cli` 基镜像会
# 预建一个 `docker=2375` 组，现在的 python 基镜像不预建任何 docker 组；这没有影响：
# socket 的 gid 由 compose 的 `group_add: ["${LING_DOCKER_GID:?...}"]` 显式给出，
# 镜像内不需要（也不该）猜一个组名。
# 详见文件末尾「非 root 与 docker socket」。
RUN addgroup -S -g 10002 updater \
 && adduser -S -D -H -h /nonexistent -s /sbin/nologin -u 10002 -G updater updater \
 && mkdir -p /app /srv/ling-code/release /srv/ling-code/backup \
 && chown 10002:10002 /app /srv/ling-code /srv/ling-code/release /srv/ling-code/backup

# 只拷发行包里真正用得到的代码。四层分开是为了让「改了哪份文件」在缓存与
# 层历史上看得出来；也方便将来核对镜像内容（updater-image job 会逐条断言存在）。
COPY --chown=10002:10002 deploy/updater/updater.py /app/updater.py
# 同目录的 registry 客户端：更新器 `import registry` 找的就是 /app/registry.py
# （脚本目录=sys.path[0]）。它只用标准库，与 updater.py 同一条"最小可信代码"口径。
COPY --chown=10002:10002 deploy/updater/registry.py /app/registry.py
COPY --chown=10002:10002 deploy/release/publish.py deploy/release/catalog.py /srv/ling-code/release/
COPY --chown=10002:10002 deploy/backup/backup.sh deploy/backup/restore_drill.sh /srv/ling-code/backup/
COPY --chown=10002:10002 deploy/releases.json /srv/ling-code/releases.json

# 脚本可执行；数据/代码文件 0644（非 root 只读即可）。
# 这份 `releases.json` 是**构建时提交里的**目录，只作 `docker run` 无宿主挂载时的
# 回退；正式部署按 compose 把发行包目录只读挂到 /srv/ling-code（已评审的那份）。
RUN chmod 0755 /srv/ling-code/backup/backup.sh /srv/ling-code/backup/restore_drill.sh \
 && chmod 0644 /app/updater.py /app/registry.py /srv/ling-code/release/publish.py \
              /srv/ling-code/release/catalog.py /srv/ling-code/releases.json

# 只读根文件系统下必须成立的两件事：
#   1) 不写 .pyc：importlib 从 /srv/ling-code 加载 publish.py / catalog.py 时会尝试
#      写 __pycache__（只读挂载/只读根 → 写入失败，Python 会静默跳过，但没必要
#      每次都试）。compose 也设了同名变量，两处一致。
#   2) 不写 /tmp：更新器与 publish.py **不用** tempfile（它们刻意在目标目录就地
#      建临时文件 + rename，见 updater.py 的 `write_env_in_place`）；compose 仍然
#      给 /tmp 挂了 tmpfs，覆盖人工 exec 进去的场景。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DOCKER_HOST=unix:///var/run/docker.sock

# 固定入口：全部参数走环境变量（`LING_*`，见 updater.py 的 build_parser），
# compose 不传 `command:`，所以这里也**清空**基镜像继承的 `CMD ["python3"]`——
# 否则 ENTRYPOINT 会被追加一个 `python3` 参数，argparse 直接拒绝。
USER 10002:10002
ENTRYPOINT ["python3", "/app/updater.py"]
CMD []

# ---------------------------------------------------------------------------
# 非 root 与 docker socket（镜像默认非 root，compose 也**强制**非 root）
# ---------------------------------------------------------------------------
# 更新器必须能读写宿主 docker socket，而 socket 是 `root:<dockerd 组>` `0660`。
# 能不能非 root，取决于**宿主**给的是哪个 gid，镜像构建期无法知道，所以：
#
#   - 镜像默认 `USER 10002:10002`：`docker run` 不加 `--user` 时是非 root，最小
#     权限；读代码、`--help`、`catalog.py validate` 这类动作不需要 socket。
#   - `deploy/updater/compose.yaml` 现在**已经强制非 root**（本批改动）：
#       user:      "${LING_UPDATER_UID:-10002}:${LING_UPDATER_GID:?...}"
#       group_add: ["${LING_DOCKER_GID:?...}"]
#     两个 gid 都是必填插值：没有默认 0、没有 root 回退，缺了 `config` 直接失败。
#     进程 = uid LING_UPDATER_UID + 主组 LING_UPDATER_GID + 补充组 socket gid，
#     所以 socket 的 group 位生效，而 `user:` 的主组不必等于 dockerd 组。
#   - 宿主文件属主由 `ling install` 负责（不需要人工 recipe）：`.env`（0600）、
#     `.ling-install.lock`（0600）、`<dir>/updater/{requests,state}`（0700）与
#     备份目录都 chown 给更新器运行身份，并保持 0700/0600（**不**放宽）。
#     root 安装时该身份是 10002:10002；以普通用户安装时是安装者自己的 uid/gid
#     （否则非 root 进程读不到 0600 `.env`）。安装状态与 `updater/.env` 记录实际值。
#   - 手工排查（没有安装器时）：`LING_UPDATER_UID` / `LING_UPDATER_GID` /
#     `LING_DOCKER_GID` 三个值都在更新器 env 里，用同一份 `--env-file` 启动即可；
#     不要用 `--user 0:0` 或 `privileged` 绕过权限问题。
#   - 私有 GHCR 包需要宿主凭据：非 root 时 `$HOME/.docker` 不可写也读不到，
#     用只读 override 挂一份 `DOCKER_CONFIG` 指向的目录即可（compose 文件头
#     已经写明「需要时用受控 override 只读追加，不要写进仓库」）。
# ---------------------------------------------------------------------------
# read_only 根文件系统：镜像内没有任何运行期需要写的位置——状态/请求/备份是 rw
# 挂载，`.env` 与安装锁是单文件 rw 挂载，代码与 compose 组只读。`--read-only`
# 下唯一可能想写的是 `/tmp`（人工 exec 用），compose 已给 tmpfs；CI 的
# updater-image job 也会用 `--read-only --tmpfs /tmp` 实跑一次 `--help` 与
# `catalog.py validate` 来证明这一点。
#
# 基础镜像 digest：**两个 FROM 都不钉**（与 `deploy/Dockerfile.api` 的
# `python:3.13-slim` / `node:22-slim` 保持一致）。取舍：
#   - 好处：升级不依赖一次人工 digest 解析；tag 本身已经是精确版本
#     （`29.9.3-cli` / `3.13-alpine3.24`），不是浮动 `cli`/`latest`/`3-alpine`。
#   - 代价：基础层理论上可被上游重推（同一 tag 换内容）。这**不影响已部署的
#     更新器**——更新器按 index digest 部署，digest 变了就不会被拉取；影响的是
#     「同一提交重跑 CI 得到的镜像字节可能不同」。
#   - 注意「tag 被重推成别的 Python 小版本」这一种：它已经被构建期的
#     `(3,12) <= sys.version_info[:2] <= (3,13)` 拦住了，不会静默生效。
#   - 为什么不现在就钉：本机到 registry-1.docker.io / auth.docker.io 不通
#     （2026-11-04 实测超时），拿不到权威 digest。镜像站能回一个 content-addressed
#     digest，但它不是权威来源，钉错会让整条构建在 CI 上 `manifest unknown` 失败。
#     下一步（有 registry 访问时做一次）：
#       docker buildx imagetools inspect docker:29.9.3-cli --format '{{.Manifest.Digest}}'
#       docker buildx imagetools inspect python:3.13-alpine3.24 --format '{{.Manifest.Digest}}'
#     再把两行 FROM 改成 `FROM docker:29.9.3-cli@sha256:<digest> AS dockercli` 与
#     `FROM python:3.13-alpine3.24@sha256:<digest>`。
#     这一步单独提一个改动，不要在无法验证的网络环境里顺手改。
# ---------------------------------------------------------------------------
