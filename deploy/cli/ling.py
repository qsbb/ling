#!/usr/bin/env python3
"""ling 安装 / 状态 / 日志 CLI（镜像模式，纯标准库）。

这是 ``docs/EASY-INSTALL-UPDATE-DESIGN.md`` §3「首次安装的交互与交付」的宿主
入口，与 ``deploy/release/publish.py``（日常发布）分工不同：

- ``ling install``：**全新**安装。写受限目录与配置、按固定 OCI 摘要拉镜像、
  起 db、单次显式迁移、起 api/worker、验证 ``/readyz`` 恰好 200，最后写下
  安装状态并打印摘要。
- ``ling status``：以**实况为准**读容器、运行镜像、``/readyz``、容器内**进程
  自报**的版本/提交（``compose exec``，零凭据）、卷身份、数据库
  ``alembic_version`` 与最近一次备份；安装状态只作参考，记录与实况不一致时
  逐条指出。
- ``ling logs``：代理到 ``docker compose ... logs``（同一文件组、项目名与
  ``--env-file``）。
- ``ling adopt``：把**现存部署**只读登记成安装记录。设计 §5.2 规定"从源码模式
  切到镜像模式"是一次手工迁移，迁移完成后没有 ``install-state.json``，于是
  ``status``/``doctor``/``backup``/``restore-drill`` 全部拒绝工作。``adopt`` 采集
  运行中 api/worker 的 image ID 与运行摘要、容器内进程自报的版本/提交、卷身份、
  ``alembic_version``、``/readyz`` 恰好 200 与备份目录，核对一致后用**同一套**
  ``new_state``/``write_state_file`` 写出与 ``ling install`` 同一 schema 的记录，
  并额外记 ``adopted``/``adopted_at``；任何一项核对不上就拒绝（不写错基线）。
  **只读**：不碰 compose、不 up/pull、不动容器，也绝不开启更新通道。
- ``ling doctor``：**只读**诊断这台机器上的这套安装（Docker/Compose 版本与
  权限、镜像与备份两处磁盘余量、安装目录/``.env``/状态/Compose 文件组及权限、
  记录 vs 实况（运行镜像摘要与 image ID）、``/readyz`` 恰好 200（不跟随 3xx）、
  容器内进程自报的提交、卷身份、``alembic_version`` vs 镜像内 head、最近备份与
  恢复点数量、备份 cron、更新通道、GHCR 可达性），按 ``ok``/``warn``/``fail``
  分级，有 ``fail`` 退出非 0。**本命令不修改任何东西**：不 run/up/pull、不建
  容器/卷、不写文件（测试以调用日志 + 目录快照断言）。
- ``ling backup``：复用 ``deploy/backup/backup.sh``，用安装时生成的**同一组**
  ``-f``、同一 ``--env-file``、同一项目名调用它；备份目录默认取安装记录里的
  ``backup.dir``（``--dir`` 可覆盖），备份后校验 manifest 的关键字段
  （``dump_sha256``/``keys_root_sha256``/``media_state``/``tombstones_state``/
  ``keys_env_set``），不完整则非 0。
- ``ling restore-drill``：复用 ``deploy/backup/restore_drill.sh``，把指定备份
  （默认最近一份）恢复到**隔离库**（库名以 ``_drill`` 结尾）并核对墓碑与媒体
  覆盖；默认使用与 ``backup`` 相同的文件组。脚本非 0 则本命令非 0。

为什么是 Python（纯标准库）
==========================
与 ``publish.py`` 同一理由：宿主已有 ``python3``，本脚本需要 JSON、flock、
HTTP 与精确的子进程参数，用 bash 会增加转义/解析风险。**不要**加第三方
import（安装包不装依赖）。需要 Python 3.9+。

**本命令不下载任何东西，也不提供 ``curl | sh``。**
-----------------------------------------------
安装包与固定版本的安装入口应由官方发行渠道提供，并在发布页展示来源、版本、
签名/证明与校验方法（设计 §3.1）。``ling`` 只接受一个**已经由发行包/发布
证据给出**的 index digest，绝不接受 ``latest``、可变 tag、空值或其他仓库；
它是"按已核验的摘要安装"，不是"帮你找最新版本"。

交互与非交互
============
交互模式**只问用户能判断的项目**：安装目录、备份目录、监听地址与端口、
是否已有反向代理、管理员用户名、是否测试模式（会显著说明**不会调用真实
模型**）。默认本机 ``127.0.0.1``；选 ``0.0.0.0`` 会打印 TLS/反代/防火墙
提示，**绝不自动开放公网**。

非交互模式（``--non-interactive``，或 stdin 不是 TTY）全部走参数/环境变量，
可在 CI 或脚本里跑。任何一项都可以用参数或环境变量给出：

===========================  ================================================
参数                         环境变量（同义）
===========================  ================================================
``--dir``                    ``LING_INSTALL_DIR``（默认 ``~/ling``）
``--backup-dir``             ``LING_BACKUP_DIR``（默认 ``<dir>-backups``）
``--host``                   ``LING_INSTALL_HOST``（默认 ``127.0.0.1``）
``--port``                   ``LING_INSTALL_PORT`` / ``LING_PORT``（默认 6520）
``--reverse-proxy``          ``LING_INSTALL_REVERSE_PROXY``（none/http/https）
``--admin-user``             ``LING_BOOTSTRAP_ADMIN`` / ``ADMIN_USER``
``--test-mode``              ``LING_TEST_MODE``（0/1）
``--digest``                 ``LING_INSTALL_DIGEST`` / ``LING_APP_IMAGE``
``--source-commit``          ``LING_INSTALL_SOURCE_COMMIT``
``--updater-digest``         ``LING_UPDATER_IMAGE``（可选；缺失=不配置更新器）
``--with-updater``           （无；显式拉起独立项目 ``<项目名>-updater``）
``--enable-updates``         （无；写 ``LING_UPDATE_ENABLED=1``，必须配合
                             ``--with-updater``；不给时显式写 0）
``--docker-socket``          ``LING_DOCKER_SOCKET`` / ``DOCKER_HOST=unix://``
``--docker-gid``             ``LING_DOCKER_GID``（默认从 socket ``stat`` 读出）
``--project``                ``LING_COMPOSE_PROJECT``（默认 ``ling``）
``--compose-file``           ``LING_INSTALL_COMPOSE_FILE``
``--non-interactive``        （无）
===========================  ================================================

更新器（独立 Compose 项目 ``<项目名>-updater``，默认 ``ling-updater``）
========================================================================
更新器**不随应用 Compose 项目启动**（设计 §4）。安装器负责准备好它的宿主侧
前提，但**默认不拉起**（``--with-updater`` 才拉）。网页更新开关独立记录在
``update.enabled``：默认 ``false``，只有同时给 ``--with-updater
--enable-updates`` 才为 ``true``（那时受限 .env 里显式写
``LING_UPDATE_ENABLED=1``）。默认关是**发布证据/演练门槛**，不是“没实现”：

- 创建 ``<dir>/updater/{requests,state}``（0700）与 ``<dir>/updater/.env``
  （0600，更新器 Compose 的 ``--env-file``；只含宿主路径/socket/gid/运行身份，
  **不含**应用口令或根密钥）；
- 检测 Docker socket 路径与 gid（``stat``；``--docker-gid`` 可显式覆盖），
  写进状态与更新器 env；**不假装知道** gid，也绝不回退 root；
- 把 ``.env``、安装锁、上述目录与备份目录 chown 给更新器运行身份：
  ``LING_UPDATER_UID/GID`` 显式给出时用它；否则 root 安装用镜像默认 10002:10002，
  非 root 安装用安装者自己的 uid/gid（这样不需 root 也能让非 root 更新器读到
  0600 ``.env``）；
- 更新器镜像只能来自发布证据（``--updater-digest`` / ``LING_UPDATER_IMAGE``）：
  没给就**不配置**、不写假 digest，输出与状态都明确“网页更新不可用”；
- 「更新器在不在跑、协议版本、运行 digest 是否与记录一致」由 ``ling doctor``
  以实况核对（本命令不新增 privilege、不放开 ``read_only``）。

口令与密钥
==========
初始管理员口令用 :mod:`secrets` 生成，写进 0600 的受限 ``.env``，**只在
受限终端（stdin/stdout 都是 TTY）显示一次**；非交互终端不显示，请用受限方式
读取 ``.env`` 的 ``LING_BOOTSTRAP_ADMIN_PASSWORD``。根密钥独立生成、写入同一
受限文件，**不默认明文回显**；``.env`` 是恢复材料的一部分，请另行离线保管。
**已有受限 ``.env`` 时绝不覆盖**：本命令只做全新安装，不是升级/修复命令。

安装前列明与检查（设计 §3.3）
============================
列出将创建的目录、项目名、容器、两个数据卷的**实际名称**（来自
``docker compose config`` 解析结果，不猜）、端口、备份位置与将拉取的镜像
摘要；检查端口占用、磁盘余量、Docker 权限、Compose 兼容性、GHCR 可达性与
宿主架构/镜像平台匹配。**若发现同名 Compose 项目/容器/卷已存在，立即停下**
并给出"现有实例迁移"提示：绝不空白覆盖，也绝不另建同名空卷。

执行顺序（与设计 §3.4/§5 一致）
==============================
1. 参数校验（不跑任何 docker 命令）
2. 预检（只读，不建目录/容器/卷）：路径、Docker/Compose、端口（真实 bind）、
   磁盘、GHCR manifest 与平台、compose 解析（项目名/镜像/卷名/端口）、
   同名项目/容器/卷；随后打印计划与检查结果
3. 抢安装锁（``<dir>/.ling-install.lock``，与 ``publish.py``/``install.sh``/
   宿主执行器同一约定：**受限 env 所在目录**下的 ``.ling-install.lock``，
   flock 非阻塞、拿不到立即失败）；锁要落在安装目录里，所以这一步才创建
   该目录——被预检拒绝的安装不会留下任何东西
4. **按需**拉取镜像：``api``/``worker``/``db`` 逐个判定，本地已能
   ``docker image inspect`` 到就跳过（不因连不上 registry 而阻断安装），
   缺哪个才拉哪个；随后核验应用镜像身份（同一摘要、平台、完整源提交）
5. 写受限目录与配置：``<dir>/``、``<dir>/compose/``（稳定 compose 文件组）、
   ``<dir>/.env``（0600）、``<dir>/install-state.json``（0600）
6. ``docker compose config -q`` + ``config --format json`` 结构化断言
   （含 api 的**宿主绑定地址**必须是所选的 ``127.0.0.1``，不能悄悄绑 0.0.0.0）
7. 起 db 并等健康 → **显式单次迁移**
   ``run --rm --no-deps -e LING_RUN_MIGRATIONS=1 api migrate``，
   核对日志有"执行迁移"、无"跳过迁移"、``alembic_version`` == 镜像内 head
8. 起 api → 验证 ``/readyz`` **恰好 200**（**不跟随 3xx**），再用
   ``compose exec`` 让容器内应用**自报**版本/提交（零凭据；``/v1/version``
   挂 ``CurrentAuth``，未鉴权恒 401，**不能**当探活）
   → 起 worker → 两个容器同一 image ID/摘要、worker healthy、最终 head 不变
9. 写完整安装状态并打印摘要

失败时绝不误报
==============
失败会写下失败状态（0600）并打印下一步；**已迁移后失败不会自动切回/重建**。
若失败发生在启动 db 之前，安装器只清理**本次自己生成**的配置文件
（安装锁文件按约定保留；不碰任何容器/卷），以便修复后重跑。

阶段标签以**实际做到哪一步**为准：db 健康检查/就绪探针/读 ``alembic_version``
失败记 ``failed_db_ready``，迁移命令失败记 ``failed_migration``，只有真正
执行过迁移（``migration.attempted=true``）之后的失败才可能是
``failed_after_migration``——没执行过迁移就绝不这么标。

"db 就绪"不只看 ``pg_isready``
==============================
全新空卷首装时 postgres 官方镜像的 entrypoint 是**两段式**启动：先起一个
**临时服务器**做 initdb/CREATE DATABASE（此时 ``pg_isready -U ling -d ling``
已返回 healthy——它只看"接受连接"，不看库是否存在），再关掉它、起正式服务器。
安装器曾经正好撞在"临时服务器正在关闭"的窗口里：

    [ling] db healthy
    psql: error: … FATAL:  the database system is shutting down

于是**本会成功的安装**被误判为 ``failed_after_migration`` 并留下半成品。
现在认定 db 可用之前必须先跑一次真实的 ``psql -U ling -d ling -tAc 'select 1'``，
并与 ``alembic_version`` 读取共用**同一个** ``--db-timeout`` 有界重试循环；
只重试"还没就绪"的瞬时错误（shutting down / starting up / 库还不存在 /
连接被拒），口令类配置错误立刻失败、不重试。

退出码
======
====  ============================================================
0     成功（安装）／一致且健康（status）／logs 正常
1     ``ling status``：实况与安装记录不一致，或有服务不健康
2     参数/用法错误，或没有安装记录
10    预检失败——未做任何变更
11    拿不到安装锁——立即失败，不等待、不跳过
12    写受限目录/配置失败——未启动任何容器
14    拉取/镜像身份/迁移链核验失败——未创建容器
15    迁移失败、迁移前发现数据库已有 schema，或迁移后 head 不匹配——
      **不启动 api/worker**
16    启动/验证失败（db/api/worker、``/readyz``、版本、卷身份）
====  ============================================================

本命令**不做**的事
==================
- 不下载安装包、不校验发布签名/证明：摘要只证明"取到了这组字节"（设计 §3.1）。
- 网页更新的**执行者不在本命令里**：更新器是独立 Compose 项目，由
  ``--with-updater`` 拉起；网页更新开关由 ``--enable-updates`` 决定（两者必须
  同时给，否则退出码 2）。默认仍是关：受限 ``.env`` 里**显式**写
  ``LING_UPDATE_ENABLED=0``（留空看不出意图），不会因为"更新器在跑"就打开。
- 安装阶段**不**执行备份/恢复演练：``ling backup``/``ling restore-drill``/
  ``ling doctor`` 是安装之后的独立救援入口（本批已实现），安装器只检查备份
  目录与最近恢复点，并如实说明"可使用但不可一键更新"。
- 不碰宿主已有实例：发现同名项目/卷/数据库即停，不执行 ``down -v``/
  ``prune``/``volume rm``。
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import errno
import fcntl
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INSTALLER_VERSION = "0.1.0"
# 安装器协议：宿主安装包与未来的更新器据此协商（当前只有 1）。
INSTALLER_PROTOCOL = 1
UPDATER_PROTOCOL = 1
STATE_SCHEMA = 1

STATE_FILE_NAME = "install-state.json"
COMPOSE_SOURCE_NAME = "compose.image.yaml"
COMPOSE_SUBDIR = "compose"
LOG_SUBDIR = "logs"
# 与 publish.py `InstallLock` 的路径口径一致：<安装根>/.ling-install.lock。
INSTALL_LOCK_NAME = ".ling-install.lock"
# 更新器容器里的固定挂载点（见 deploy/updater/compose.yaml）。安装状态按图记录
# 宿主侧路径；`--with-updater` 时安装器**显式**拉起 `<项目名>-updater` 项目，
# 否则只生成配置、不启动，并在摘要与状态里如实说明「网页更新未启用」。
UPDATER_CONTAINER_DIR = "/srv/ling"
# 更新器是**独立** Compose 项目，项目名**由应用项目名派生**：默认 `ling` →
# `ling-updater`（与旧常量逐字相同，现有部署不受影响）。不能写死：同一宿主上
# 的第二个 `--with-updater` 安装若沿用同一个项目名，Compose 会按不同的
# `--env-file` **重建**第一个实例的更新器容器、把挂载改成新实例的路径——实例 A
# 的网页更新通道于是静默开始操作实例 B 的数据。派生名与归属判据见
# :func:`updater_project_for` / :func:`updater_project_of`。


def updater_project_for(project: str) -> str:
    """更新器 Compose 项目名：``<应用项目名>-updater``（默认 ``ling-updater``）。"""
    return f"{project}-updater"


def updater_project_of(state: dict[str, Any] | None) -> str:
    """本次部署**实际**使用的更新器项目名：优先安装记录，记录缺失才派生。

    ``doctor`` / ``status`` / ``adopt`` 走这里。记录里存了 ``updater.project``
    就用它、绝不重新派生——否则应用项目名一旦改过，命令会去核对另一个项目：
    看起来一切正常，实际指向的是别人的更新器。
    """
    record = state if isinstance(state, dict) else {}
    updater = record.get("updater")
    recorded = updater.get("project") if isinstance(updater, dict) else None
    if isinstance(recorded, str) and recorded.strip():
        return recorded.strip()
    project = record.get("project")
    if isinstance(project, str) and project.strip():
        return updater_project_for(project.strip())
    return updater_project_for(DEFAULT_PROJECT)


# 安装根下的更新器目录（请求/状态/更新器专用 env）。
UPDATER_SUBDIR = "updater"
UPDATER_REQUESTS_SUBDIR = "requests"
UPDATER_STATE_SUBDIR = "state"
# 请求目录里更新器认领后就近归档的子目录（与 deploy/updater/updater.py 的
# `archive_claim` 一致）。API 会 stat 它来判断"这个 request_id 是否已用过"，
# 所以它必须让 API 的组位可读——安装器**预先**建好（更新器只会 mkdir(exist_ok)
# 0700，缺了的话 API 读不到就 fail closed，所有新请求都会被判成"已用过"）。
UPDATER_ARCHIVE_SUBDIR = "archive"
UPDATER_ENV_NAME = ".env"
# Compose 给每个容器打的"项目工作目录"标签：冲突守卫与 doctor 用它 + 挂载源
# 路径判断"这个更新器项目属于哪个安装"（见 :func:`_updater_container_owner`）。
UPDATER_WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
# --- 更新通道的宿主权限方案（P5 接线）---
#
# 两个容器是不同 uid（api 10001 / 更新器 10002 或安装者 uid），所以共享靠
# **共享组 + 组权限位**，而不是 chmod 0777：
#
# | 目录                    | 属主(uid)   | 属组(gid)   | 模式  | 谁能写 |
# | ----------------------- | ----------- | ----------- | ----- | ------ |
# | updater/requests        | 更新器 uid  | 共享组      | 2770  | 更新器（属主）+ API（组） |
# | updater/requests/archive| 更新器 uid  | 共享组      | 2750  | 仅更新器；API 只 stat 名字 |
# | updater/state           | 更新器 uid  | 共享组      | 0750  | 仅更新器；API 只读 status.json |
#
# 共享组 = **更新器的 gid**（`LING_UPDATE_SHARED_GID`）：安装器创建这些目录时
# 属组天然就是它（root 安装时 10002，普通用户安装时是安装者自己的 gid），
# 不需要 root 也能设上。API 容器靠 compose 的 `group_add` 加入同一个组。
#
# requests 带 **setgid**（2770）：API 原子写出的 `request-*.json` 是 0640，
# 文件继承目录属组 = 共享组，更新器按"组"读得到；否则更新器读不了 API 写的
# 文件（不同 uid），请求永远处理不了。
#
# state 是 0750 而不是 0770：status.json 由更新器独占写（0644），API 只读；
# 组位不给写才伪造不了状态。state 里的私有文件（journal.log/task.json/锁）
# 都是 0600，API 即使在组里也读不到内容。
UPDATE_REQUESTS_MODE = 0o2770
UPDATE_ARCHIVE_MODE = 0o2750
UPDATE_STATUS_MODE = 0o0750
# --- 跨工具安装锁（`<安装根>/.ling-install.lock`）---
#
# 属主永远是**安装者**（宿主 `install.sh` / `publish.py` / `ling` 都以属主身份
# 打开它）；只有更新器要**以另一个 uid** 访问时，才把属组设成共享组并放开组位：
#
# | 情况                                    | 属主   | 属组   | 模式 |
# | --------------------------------------- | ------ | ------ | ---- |
# | 更新器与安装者同一 uid / 未配更新器      | 安装者 | 安装者 | 0600 |
# | 更新器是另一个 uid，且安装者能设这个组   | 安装者 | 共享组 | 0660 |
#
# 为什么组位必须**可写**：flock 用 `O_RDWR` 打开锁文件，0600（连组读都没有）
# 会让另一个 uid 的更新器直接 EACCES；更新器把 EACCES 归类成
# `install_lock_busy`，看上去像"有人持锁"，实际没有任何进程持锁（P5 真机演练）。
INSTALL_LOCK_MODE = 0o600
INSTALL_LOCK_MODE_SHARED = 0o660
# 更新器代码：发行包 deploy 目录（含 release/publish.py、backup/backup.sh）。
UPDATER_COMPOSE_SUBDIR = "updater"
UPDATER_COMPOSE_NAME = "compose.yaml"
DEFAULT_DOCKER_SOCKET = "/var/run/docker.sock"
# 镜像的默认运行身份（deploy/updater/Dockerfile 的 `USER 10002:10002`）。
# compose 允许覆盖，但**必须显式**：见 deploy/updater/compose.yaml 的 user/group_add。
DEFAULT_UPDATER_UID = 10002
DEFAULT_UPDATER_GID = 10002

IMAGE_REPOSITORY = "ghcr.io/qsbb/ling-api"
DIGEST_RE = re.compile(r"^ghcr\.io/qsbb/ling-api@sha256:([0-9a-f]{64})$")
UPDATER_REPOSITORY = "ghcr.io/qsbb/ling-updater"
UPDATER_DIGEST_RE = re.compile(r"^ghcr\.io/qsbb/ling-updater@sha256:([0-9a-f]{64})$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ADMIN_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")

EXIT_OK = 0
EXIT_STATUS_MISMATCH = 1
EXIT_USAGE = 2
EXIT_PREFLIGHT = 10
EXIT_LOCK = 11
EXIT_LAYOUT = 12
EXIT_PULL = 14
EXIT_MIGRATE = 15
EXIT_POST_MIGRATION = 16

ARCH_ALIASES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}

DEFAULT_PORT = 6520
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PROJECT = "ling"
DEFAULT_ADMIN = "ling"
DEFAULT_POSTGRES_IMAGE = "postgres:16-alpine"
# 安装器必须保证本地可用的三个服务，顺序即"逐个判定/逐个拉取"的顺序：
# 先把应用镜像备好（api/worker 是同一个 digest 引用），再去备库镜像。
PULL_SERVICES = ("api", "worker", "db")
APP_SERVICES = ("api", "worker")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

DEFAULT_READY_TIMEOUT = 120.0
DEFAULT_DB_TIMEOUT = 180.0
# db 就绪/读版本循环的重试间隔上限（秒）。探测本身也要时间（每次一次
# `compose exec`），所以实际节奏是 min(这个值, 剩余时间)。
DB_READY_POLL_INTERVAL = 2.0
DEFAULT_WORKER_TIMEOUT = 120.0
DEFAULT_MIGRATE_TIMEOUT = 600.0
DEFAULT_COMMAND_TIMEOUT = 120.0
DEFAULT_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_CAPTURED_BODY = 2 * 1024 * 1024
MAX_ERROR_TAIL = 2000
# 一条 compose ``published`` 值最多展开多少个端口。预检是逐个端口核对的，范围
# 大到没法逐个核对时宁可拒绝，也不静默跳过（与 publish.py 同一口径）。
MAX_PUBLISHED_PORT_RANGE = 64

# 备份/恢复演练复用发行包里的脚本（设计 §4「独立部署包」）：路径相对本脚本，
# 不 import、不重写备份逻辑。安装时生成的 compose 副本作为唯一主文件。
BACKUP_SCRIPT_SUBDIR = "backup"
BACKUP_SCRIPT_NAME = "backup.sh"
RESTORE_DRILL_SCRIPT_NAME = "restore_drill.sh"
DRILL_DB_SUFFIX = "_drill"
DUMP_NAME_RE = re.compile(r"^ling-(.+)\.dump$")
DEFAULT_BACKUP_TIMEOUT = 1800.0
DEFAULT_BACKUP_MAX_AGE_DAYS = 7

# 镜像模式的 compose 主文件把宿主侧绑定写成 `"${LING_PORT:-6520}:6520"`（所有
# 网卡）。安装器默认只允许本机访问，因此**在安装目录里生成的副本**把这一行
# 换成带 `${LING_HOST}` 的形式；锚点必须逐字符匹配且只出现一次，否则拒绝安装
# （绝不静默产出一份"看起来对、实际绑 0.0.0.0"的文件）。生成后还会用
# `docker compose config --format json` 断言 host_ip == 所选地址。
COMPOSE_PORTS_LINE = '- "${LING_PORT:-6520}:6520"'
COMPOSE_PORTS_REPLACEMENT = '- "${LING_HOST:-127.0.0.1}:${LING_PORT:-6520}:6520"'

# ---------------------------------------------------------------- db 就绪判据
#
# 真机缺陷（全新空卷首装）：postgres 官方镜像的 entrypoint 在**空卷**上是两段式
# 启动——先用一个**临时服务器**做初始化（此时 `pg_isready -U ling -d ling`
# 就会返回"接受连接"，因为 pg_isready 不看库是否存在），再建库、关掉临时服务器、
# 才起正式服务器。于是 compose 的 healthcheck（pg_isready）在初始化中途就报
# healthy，而安装器紧接着的 `psql ... select version_num` 正好撞在
# "waiting for server to shut down" 的窗口里：
#
#   [ling] db healthy
#   psql: error: … FATAL:  the database system is shutting down
#
# 修法两条（都只在本文件里）：
# 1. 不再只信 `pg_isready`：认定 db 可用之前先跑一次真实
#    `psql -U ling -d ling -tAc 'select 1'`（**就绪探针**），它同时覆盖
#    "临时服务器还在初始化"与"正式服务器还没起来"两种情形；
# 2. 就绪探针与 `alembic_version` 读取走**同一个**有界重试循环，
#    只把**明确属于"还没就绪"**的瞬时错误当可重试；其它错误（例如
#    `password authentication failed`）立刻失败，绝不重试掩盖配置错误。
#
# 重试上限来自 ``--db-timeout``（同一个窗口，不再另设一个魔法数字）。
DB_READY_PROBE_SQL = "select 1"
# "还没就绪"的瞬时错误特征（小写匹配 psql 的 stderr+stdout）。逐条对应现场：
DB_NOT_READY_PATTERNS = (
    # ④⑤ 临时服务器正在关闭（真机抓到的那一条）
    "the database system is shutting down",
    # ⑥ 之后正式服务器还没接受连接
    "the database system is starting up",
    # ③ 临时服务器阶段库还没建出来（`database "ling" does not exist`）——
    # 由 DB_DATABASE_MISSING_RE 覆盖（任何库名，不只是 ling）
    # 连接被拒 / socket 不存在（容器内端口还没监听、entrypoint 还没走完）
    "could not connect to server",
    "connection refused",
    "no such file or directory",
    "server closed the connection unexpectedly",
)
# 明确的**配置错误**：这类错误重试多少次都不会好，必须立刻失败。
DB_FATAL_PATTERNS = (
    "password authentication failed",
    'role "ling" does not exist',
    "permission denied for database",
)
# 库**本身**还不存在（entrypoint 初始化阶段常见）：这是"还在初始化"，不是
# "空实例"。必须与 `relation "alembic_version" does not exist`（= 空白实例，
# 可以迁移）区分开，否则会把"db 没就绪"误判成"这是空白库，直接迁移"。
# 官方镜像的 psql 可能是英文或本地化文案，两种都认。
DB_DATABASE_MISSING_RE = re.compile(
    r'database "[^"]+" does not exist|数据库 "[^"]+" 不存在'
)
# 空白实例的判据：库在、但 alembic_version 表还没有（迁移前就是这个样子）。
DB_BLANK_RE = re.compile(
    r'relation "[^"]*alembic_version" does not exist'
    r'|关系 "[^"]*alembic_version" 不存在'
)


def db_error_kind(text: str) -> str:
    """把 psql 的错误文本归成 ``retry``/``fatal``/``blank``/``unknown``。

    判定顺序**重要**：先认明确的配置错误（fatal），再认"还没就绪"（retry），
    最后才认"空白实例"（blank）。反过来会把 `database "ling" does not exist`
    （= 还在初始化）当成空白实例放过去，直接去跑迁移。
    """
    lower = text.lower()
    if any(pattern in lower for pattern in DB_FATAL_PATTERNS):
        return "fatal"
    if DB_DATABASE_MISSING_RE.search(lower) or any(
        pattern in lower for pattern in DB_NOT_READY_PATTERNS
    ):
        return "retry"
    if DB_BLANK_RE.search(lower):
        return "blank"
    return "unknown"


ALEMBIC_HEAD_SCRIPT = """\
import os
from alembic.config import Config
from alembic.script import ScriptDirectory

root = os.environ.get("LING_PROJECT_ROOT", "/app")
cfg = Config(os.path.join(root, "alembic.ini"))
cfg.set_main_option("script_location", os.path.join(root, "migrations"))
script = ScriptDirectory.from_config(cfg)
for head in script.get_heads():
    print("HEAD=" + head)
for revision in script.walk_revisions():
    print("REV=" + revision.revision)
    parents = revision.down_revision
    if isinstance(parents, str):
        parents = (parents,)
    if parents:
        for parent in parents:
            print("PARENT=" + revision.revision + ":" + str(parent))
"""


# ---------------------------------------------------------------- 小工具
#
# 下面这些 helper 与 ``deploy/release/publish.py`` 的同名实现保持一致
# （同一套约定、同一套行为）。这里**复制**而不是 import publish.py：安装包
# 可能只带 ``ling`` 与 compose/备份脚本（设计 §4「独立部署包」），让全新安装
# 依赖日常发布脚本既不必要也脆弱。共享的硬约定由
# ``tests/test_cli_install.py::test_shared_lock_and_redirect_invariants`` 钉住：
# 锁路径公式与"3xx 绝不跟随"两处必须逐字节等价。宿主发布端口的解析
# （``parse_published_port`` / ``service_published_ports``）同样是复制关系，
# 由 ``test_published_ports_match_publish_script`` 钉住两边结果一致。


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def local_stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def host_arch() -> str:
    machine = platform.machine().strip().lower()
    return ARCH_ALIASES.get(machine, machine)


def tail(text: str, limit: int = MAX_ERROR_TAIL) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """把 3xx 当错误抛出，**绝不**跟随 Location。

    ``urllib.request.urlopen`` 默认跟随 301/302/303/307/308：一个 302→200 的
    响应会被读成 200，``/readyz`` 的"恰好 200"门禁就被洗掉了。这里覆盖所有
    重定向分支，让 ``http_get`` 拿到真实状态码。
    """

    def _reject(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
    ) -> None:
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def http_get(url: str, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "ling-cli/1"})
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
            return response.getcode(), response.read(MAX_CAPTURED_BODY)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(MAX_CAPTURED_BODY)
    except (urllib.error.URLError, OSError) as exc:
        return 0, str(exc).encode("utf-8")


# --- HTTP 探活的唯一出口（**这张表必须与真实应用的鉴权一致**）-------------
#
# 安装/状态/诊断**不持有用户凭据**：一条能改宿主状态的通道不应该同时是凭据的
# 另一个持有者（攻击面只会变大）。所以这里只允许打**免鉴权**的端点。
#
# ``/readyz`` 是唯一被允许的那个：它的鉴权是被**刻意降级**的——容器 healthcheck
# 必须能探它，未登录时它只回 ``{"status": "ready"}``，不含任何部署指纹（见
# ``src/ling/api/routes/health.py`` 的模块 docstring）。
#
# ``/v1/version`` **不在这张表里**：它挂 ``CurrentAuth``，未鉴权永远 401。
# 真机演练 P5 第 6 个缺陷的同一类问题：``ling install`` 的 verify 与
# ``ling status`` / ``ling doctor`` 曾拿它当探活，于是真机上安装最后一步必失败、
# 状态与诊断永远报错。"运行容器是不是目标构建"改用容器内**进程证据**
# （``CONTAINER_VERSION_PROBE``，见 ``Installer.verify_running_commit``）。
#
# 防回归：``tests/test_cli_install.py`` 会
#   1) 对着**真实 FastAPI 应用**逐条核这张表（未鉴权不是 401/403）；
#   2) 用 AST 扫描本文件：所有 HTTP 探活必须走 ``http_probe`` 且路径是字面量、
#      并 ⊆ 这张表；绕过 helper 直接 ``http_get``、或把 ``/v1/version`` 登记进来
#      → 测试红。
HTTP_PROBE_PATHS: tuple[str, ...] = ("/readyz",)

# 容器内**进程证据**探针：让应用自己报出版本与构建提交，不发任何 HTTP、不读
# 任何配置/秘密。``LING_COMMIT`` 由 ``deploy/Dockerfile.api`` 在构建时写进镜像
# ENV，与 OCI 标注 ``org.opencontainers.image.revision`` 同源；``ling.__version__``
# 来自镜像里装好的 wheel。两者都不需要凭据。字符串与
# ``deploy/release/publish.py`` 的 ``CONTAINER_VERSION_PROBE`` 逐字节一致，由
# ``tests/test_cli_install.py`` 钉住（安装包不 import publish.py，只能复制）。
CONTAINER_VERSION_PROBE = (
    "import json, os; from ling import __version__; "
    "print(json.dumps({'version': __version__, "
    "'commit': os.environ.get('LING_COMMIT', '').strip()}))"
)
CONTAINER_VERSION_EXEC_TIMEOUT = 60.0


def parse_container_version_probe(stdout: str) -> dict[str, Any]:
    """取容器内探针打印的 JSON（从后往前找第一条可解析的对象）。

    容器里跑的是应用自己的 Python，镜像启动脚本可能先打印别的东西；所以从
    输出**末尾**往前找第一条合法 JSON，绝不把整段 stdout 当 JSON 解析。
    """
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def http_probe(base: str, path: str, *, timeout: float) -> tuple[int, bytes]:
    """本机 HTTP 探活的**唯一出口**：只允许 ``HTTP_PROBE_PATHS`` 里的路径。

    参数是两个部分（``base`` + 字面量 ``path``）而不是整条 URL，是为了让
    "探了哪个端点"在源码里可被静态核对：``tests/test_cli_install.py`` 用 AST
    收集每一处调用的第二个实参，要求它是字面量且已登记。绕开本函数直接
    ``http_get(f"{base}/v1/version")`` 在同一处测试里会直接红。
    """
    if path not in HTTP_PROBE_PATHS:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "probe_path",
            (
                f"未登记的 HTTP 探活路径 {path!r}：安装/状态/诊断不持有用户凭据，"
                f"只能探免鉴权端点 {list(HTTP_PROBE_PATHS)}。"
            ),
            ["改用容器内进程证据（CONTAINER_VERSION_PROBE），不要新增免鉴权端点。"],
        )
    return http_get(f"{base}{path}", timeout=timeout)


def container_version_probe(
    runner: Runner,
    group: ComposeGroup,
    *,
    timeout: float = CONTAINER_VERSION_EXEC_TIMEOUT,
    env: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str]:
    """在运行中的 api 容器里问应用自己：``(payload, error)``。

    与 ``deploy/release/publish.py`` 的 ``verify_running_commit`` 同一口径：
    零 HTTP、零凭据。``ling status`` / ``ling doctor`` 是**只读报告**，所以失败
    不抛异常，把原因作为第二项返回（调用方降级成一条 fail 行）。
    """
    result = runner.run(
        [
            *group.argv(),
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            CONTAINER_VERSION_PROBE,
        ],
        env=env if env is not None else os.environ.copy(),
        timeout=timeout,
    )
    if result.returncode != 0:
        return {}, tail(result.stderr or result.stdout) or f"rc={result.returncode}"
    payload = parse_container_version_probe(result.stdout)
    if not payload:
        return {}, "容器内探针没有打印可解析的 JSON"
    return payload, ""


def write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """同目录临时文件 + fsync + rename；创建瞬间就带目标权限位。"""
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def state_payload(state: dict[str, Any]) -> str:
    """安装记录的**唯一**序列化口径（``install`` 与 ``adopt`` 共用）。"""
    return json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_state_file(path: Path, state: dict[str, Any]) -> None:
    """安装记录的**唯一**写入口；权限恒 0600，原子替换。

    ``ling install`` 与 ``ling adopt`` 都走这里：adopt 不另造 schema/写盘方式，
    否则"记录结构相同"只能靠人肉维护。
    """
    write_atomic(path, state_payload(state).encode("utf-8"), 0o600)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def existing_ancestor(path: Path) -> Path:
    current = path
    while True:
        if current.exists():
            return current
        if current.parent == current:
            return current
        current = current.parent


def bind_source_error(
    path: Path, want: str, *, symlink: bool = False, at_preflight: bool = False
) -> LingError:
    """Docker bind mount 源类型不对时的错误（必须带可执行的修复指引）。

    Docker 在 bind mount 源**缺失时会自动建一个空目录**：`.env` 变成目录后
    compose 的 `--env-file` 读不了，锁变成目录后 flock 打不开，`compose/`
    少了更新器找不到文件组。所以安装器要先预建、再核对类型；类型不对就**现在**
    失败，并把"删掉那个空壳再重跑"写进 next_actions（不静默继续）。
    """
    code, status = (
        (EXIT_PREFLIGHT, "failed_preflight") if at_preflight else (EXIT_LAYOUT, "failed_layout")
    )
    if symlink:
        message = (
            f"bind mount 源不能是符号链接：{path}；"
            "Docker 会按链接目标挂载，安装器/更新器看到的就不是同一个 inode。"
        )
        actions = [
            f"确认链接指向：ls -la {path}",
            f"换成真实路径（删掉链接，让安装器自己建）：rm {path}",
            "然后重新运行 ling install。",
        ]
    elif want == "dir":
        message = (
            f"{path} 必须是目录，但现在是文件或特殊文件；"
            "更新器要把整个 compose 文件组只读挂进容器。"
        )
        actions = [
            f"确认它不是别的实例的文件：ls -la {path}",
            f"没用就移走：mv {path} {path}.bak（不要覆盖别人的文件）",
            "然后重新运行 ling install（安装器会建目录并写入 compose 文件组）。",
        ]
    else:
        message = (
            f"{path} 必须是普通文件，但现在是目录或特殊文件——"
            "Docker 在 bind mount 源缺失时会**自动建一个空目录**，"
            "而 compose 的 --env-file 与 flock 都读不了目录。"
        )
        actions = [
            f"确认它确实是空的：ls -la {path}",
            f"空目录就删掉：rmdir {path}",
            "然后重新运行 ling install（安装器会重新预建普通文件）。",
            "不要用 chmod/chown 绕过：类型不对，Docker 与 flock 都用不了。",
        ]
    return LingError(code, status, "bind_source", message, actions)


class Logger:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self._file: Path | None = None

    def emit(self, message: str) -> None:
        line = f"[ling] {message}"
        print(line, file=sys.stderr, flush=True)
        self.lines.append(line)
        if self._file is not None:
            try:
                with self._file.open("a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
            except OSError:
                pass

    def command(self, argv: Sequence[str]) -> None:
        # 只记录 argv；绝不打印 env（可能含密钥）。
        self.emit("$ " + " ".join(argv))

    def attach(self, path: Path) -> None:
        self._file = path
        try:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                for line in self.lines:
                    stream.write(line + "\n")
            os.chmod(path, 0o600)
        except OSError:
            self._file = None


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class Runner:
    def __init__(self, logger: Logger) -> None:
        self.logger = logger

    def run(
        self,
        argv: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        cwd: Path | None = None,
    ) -> Result:
        self.logger.command(argv)
        try:
            proc = subprocess.run(
                list(argv),
                env=env,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            return Result(tuple(argv), 127, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return Result(tuple(argv), 124, stdout, stderr + f"\n超时（{timeout}s）")
        if proc.returncode != 0:
            detail = proc.stderr or proc.stdout
            if detail.strip():
                self.logger.emit(f"（rc={proc.returncode}）{tail(detail)}")
        return Result(tuple(argv), proc.returncode, proc.stdout, proc.stderr)


class LingError(Exception):
    def __init__(
        self,
        exit_code: int,
        status: str,
        step: str,
        message: str,
        next_actions: Sequence[str] = (),
        *,
        after_migration: bool = False,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.status = status
        self.step = step
        self.message = message
        self.next_actions = list(next_actions)
        self.after_migration = after_migration


class Cancelled(Exception):
    """交互确认时用户选择"不继续"。"""


class InstallLock:
    """与 ``publish.py`` / ``install.sh`` / 宿主执行器同一把锁的同一规则。

    路径 = **受限 env 解析后真实目标**所在目录下的 ``.ling-install.lock``。
    用 fcntl.flock 非阻塞独占；拿不到立即失败，不等待、不跳过。
    """

    def __init__(self, env_file: Path) -> None:
        self.path = Path(os.path.realpath(env_file)).parent / ".ling-install.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise LingError(
                EXIT_LOCK,
                "failed_lock",
                "lock",
                "当前平台不支持 O_NOFOLLOW，拒绝打开安装锁（避免跟随符号链接）",
            )
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
        except OSError as exc:
            raise LingError(
                EXIT_LOCK,
                "failed_lock",
                "lock",
                f"无法打开安装锁 {self.path}：{exc}",
                ["检查该路径的权限/类型（锁必须是普通文件，不能是符号链接）。"],
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise LingError(
                    EXIT_LOCK,
                    "failed_lock",
                    "lock",
                    f"安装锁必须是普通文件：{self.path}",
                )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LingError(
                    EXIT_LOCK,
                    "failed_lock",
                    "lock",
                    (
                        f"另一个安装/发布/更新任务正持有安装锁：{self.path}。"
                        "本命令不等待、不跳过。"
                    ),
                    [
                        "等待 install.sh/发布脚本/宿主执行器结束；确认没有残留进程后重跑。",
                        "不要手工删除锁文件来绕过互斥（flock 随进程释放）。",
                    ],
                ) from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None


# ---------------------------------------------------------------- 布局与状态


@dataclass(frozen=True)
class ComposeGroup:
    """一组 ``-f`` + 项目名 + ``--env-file``；所有 compose 调用必须整组一致。"""

    project: str
    files: tuple[Path, ...]
    env_file: Path

    def argv(self) -> list[str]:
        argv = ["docker", "compose", "-p", self.project]
        for path in self.files:
            argv += ["-f", str(path)]
        argv += ["--env-file", str(self.env_file)]
        return argv


@dataclass(frozen=True)
class Layout:
    root: Path
    project: str

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def state_file(self) -> Path:
        return self.root / STATE_FILE_NAME

    @property
    def compose_dir(self) -> Path:
        return self.root / COMPOSE_SUBDIR

    @property
    def compose_file(self) -> Path:
        return self.compose_dir / COMPOSE_SOURCE_NAME

    @property
    def install_lock(self) -> Path:
        """跨工具安装锁（与 ``publish.py`` / ``install.sh`` 同一把）。

        属主=安装者；更新器是另一个 uid 时由 :meth:`Installer.ensure_install_lock`
        设成 0660 + 共享组属组（flock 需要组写位），否则 0600。
        """
        return self.root / INSTALL_LOCK_NAME

    @property
    def logs_dir(self) -> Path:
        return self.root / LOG_SUBDIR

    @property
    def updater_dir(self) -> Path:
        """更新器专用目录（请求/状态/更新器 `.env`），0700。"""
        return self.root / UPDATER_SUBDIR

    @property
    def updater_requests_dir(self) -> Path:
        """API 写请求、更新器读/认领的目录（2770：共享组可写，属主是更新器 uid）。"""
        return self.updater_dir / UPDATER_REQUESTS_SUBDIR

    @property
    def updater_archive_dir(self) -> Path:
        """请求归档目录（2750：只有更新器写；API 只 stat 名字判重）。"""
        return self.updater_requests_dir / UPDATER_ARCHIVE_SUBDIR

    @property
    def updater_state_dir(self) -> Path:
        """更新器持久状态/日志目录（0750：API 只读，读到的只有 status.json）。"""
        return self.updater_dir / UPDATER_STATE_SUBDIR

    @property
    def updater_env_file(self) -> Path:
        """更新器 Compose 项目的 `--env-file`（0600，含宿主路径/socket/gid）。"""
        return self.updater_dir / UPDATER_ENV_NAME


def updater_mounts(layout: Layout, *, with_updater: bool = False) -> dict[str, Any]:
    """更新器容器需要的挂载（与 ``deploy/updater/compose.yaml`` 一一对应）。

    ``<应用项目名>-updater`` 是**独立** Compose 项目；``mounts`` 只列**安装根
    内部**的三条（``.env``/安装锁单文件 rw、``compose/`` 目录 ro）——不能退回
    「整个安装根只读挂进去」（那样更新器写不了 ``.env``，也拿不到跨工具锁，
    更新等于没做）。另外五条（socket、发行包代码、请求、状态、备份）的宿主
    路径在安装根之外，由 :func:`updater_host_paths` 记录；两者在
    :func:`new_state` 里合并。
    """
    project = updater_project_for(layout.project)
    return {
        "project": project,
        "compose_yaml": "deploy/updater/compose.yaml",
        "install_dir": str(layout.root),
        "env_file": f"{UPDATER_CONTAINER_DIR}/.env",
        "install_lock_file": f"{UPDATER_CONTAINER_DIR}/{INSTALL_LOCK_NAME}",
        "compose_dir": f"{UPDATER_CONTAINER_DIR}/{COMPOSE_SUBDIR}",
        "mounts": [
            {
                "source": str(layout.env_file),
                "target": f"{UPDATER_CONTAINER_DIR}/.env",
                "mode": "rw",
                "kind": "file",
            },
            {
                "source": str(layout.install_lock),
                "target": f"{UPDATER_CONTAINER_DIR}/{INSTALL_LOCK_NAME}",
                "mode": "rw",
                "kind": "file",
            },
            {
                "source": str(layout.compose_dir),
                "target": f"{UPDATER_CONTAINER_DIR}/{COMPOSE_SUBDIR}",
                "mode": "ro",
                "kind": "dir",
            },
        ],
        "note": (
            f"更新器由独立项目 {project} 运行；"
            + (
                "本次安装用 --with-updater 显式拉起"
                if with_updater
                else "本次安装不自动拉起（未用 --with-updater）"
            )
            + "（网页更新开关见 update.enabled，由 --enable-updates 决定）"
        ),
    }


def update_channel_plan(options: InstallOptions) -> dict[str, Any]:
    """API ↔ 更新器更新通道的宿主路径与权限方案（写进安装状态）。

    这是**计划值**（方案本身，创建目录时按它 chmod）；实际 stat 由
    :meth:`Installer.create_updater_layout` 在创建后补进 ``stat``。
    字段口径与 ``deploy/compose.image.yaml`` 的必填插值一致：

    - ``requests_host`` → ``LING_UPDATE_REQUESTS_HOST``（api 挂 rw）
    - ``status_host``   → ``LING_UPDATE_STATUS_HOST``（api 挂 ro；就是更新器的
      state 目录：``status.json`` 由更新器原子写在那里。单文件 bind 不行——
      rename 换 inode，挂进去的文件会永远停在旧内容）
    - ``shared_gid``    → ``LING_UPDATE_SHARED_GID``（api 的 ``group_add``）
    """
    layout = options.layout
    return {
        "shared_gid": options.updater_gid,
        "shared_gid_source": options.updater_user_source,
        "requests_host": str(layout.updater_requests_dir),
        "status_host": str(layout.updater_state_dir),
        "archive_host": str(layout.updater_archive_dir),
        "modes": {
            "requests": f"{UPDATE_REQUESTS_MODE:04o}",
            "archive": f"{UPDATE_ARCHIVE_MODE:04o}",
            "status": f"{UPDATE_STATUS_MODE:04o}",
        },
        "setgid": True,
        "stat": {},
        "note": (
            "API 仅写请求/读状态：requests 2770 + 共享组（setgid 让请求文件继承组），"
            "state 0750（API 只读 status.json，写不了状态）；"
            f"网页更新开关 {UPDATE_ENABLED_VAR}="
            f"{'1' if options.enable_updates else '0'}"
        ),
    }


def updater_host_paths(options: InstallOptions) -> dict[str, Any]:
    """更新器在**安装根之外**的宿主路径、socket gid 与非 root 运行身份。

    这些值同时写进 ``<dir>/updater/.env``（compose 的 ``--env-file``）与安装状态：
    安装器不猜 socket 的 gid（各宿主不同），也不默认 root——解析规则见
    :func:`resolve_updater_user` 与 :func:`resolve_docker_gid`。
    """
    layout = options.layout
    code_dir = options.updater_code_dir
    deploy_files = {
        "publish_py": (code_dir / "release" / "publish.py").is_file(),
        "backup_sh": (code_dir / "backup" / "backup.sh").is_file(),
        "releases_json": (code_dir / "releases.json").is_file(),
        "compose_yaml": options.updater_compose_file.is_file(),
    }
    return {
        "env_file": str(layout.updater_env_file),
        "requests_dir": str(layout.updater_requests_dir),
        "state_dir": str(layout.updater_state_dir),
        "backup_dir": str(options.backup_dir),
        "code_dir": str(code_dir),
        "compose_file": str(options.updater_compose_file),
        "update_channel": update_channel_plan(options),
        "docker": {
            "socket": str(options.docker_socket),
            "socket_source": options.docker_socket_source,
            "gid": options.docker_gid,
            "gid_source": options.docker_gid_source,
        },
        "user": {
            "uid": options.updater_uid,
            "gid": options.updater_gid,
            "source": options.updater_user_source,
        },
        "code_files": deploy_files,
        "extra_mounts": [
            {
                "source": str(options.docker_socket),
                "target": "/var/run/docker.sock",
                "mode": "rw",
                "kind": "socket",
            },
            {
                "source": str(code_dir),
                "target": "/srv/ling-code",
                "mode": "ro",
                "kind": "dir",
            },
            {
                "source": str(layout.updater_requests_dir),
                "target": "/run/ling-updater/requests",
                "mode": "rw",
                "kind": "dir",
            },
            {
                "source": str(layout.updater_state_dir),
                "target": "/var/lib/ling-updater",
                "mode": "rw",
                "kind": "dir",
            },
            {
                "source": str(options.backup_dir),
                "target": "/var/lib/ling-backups",
                "mode": "rw",
                "kind": "dir",
            },
        ],
    }


UPDATE_ENABLED_VAR = "LING_UPDATE_ENABLED"
# 打开网页更新要满足的三条（安装摘要/status/doctor 共用同一句话，避免各写一套）：
# 更新器镜像已配置、发布目录里已有条目、有完整恢复点。
UPDATE_ENABLE_HOWTO = (
    "打开方法：`ling install --with-updater --enable-updates`；"
    "或在现有部署上备好更新器镜像/发布目录条目/完整恢复点后，"
    f"把受限 .env 的 {UPDATE_ENABLED_VAR} 改成 1 并 `docker compose ... up -d api`"
)
# 默认关是**门槛**，不是“没实现”——这句话在摘要/status/doctor 里一字不改地复用。
UPDATE_NOT_IMPLEMENTED_NOTE = (
    "更新器本身是已交付的（deploy/updater/）：默认关只是发布证据/演练门槛，"
    "不是缺功能"
)


def update_reason_for(options: InstallOptions) -> str:
    """安装记录 ``update.reason``：说明开关是开还是关、以及为什么。"""
    if options.enable_updates:
        return (
            "已按 --with-updater --enable-updates 启用：受限 .env 写 "
            f"{UPDATE_ENABLED_VAR}=1，API 把更新请求写进发布目录，由 "
            f"{updater_project_for(options.project)} 项目执行"
        )
    if options.with_updater:
        return (
            "更新器已拉起，但没给 --enable-updates：受限 .env 显式写 "
            f"{UPDATE_ENABLED_VAR}=0；{UPDATE_ENABLE_HOWTO}"
        )
    return (
        "本次安装未启用网页更新（未给 --with-updater --enable-updates）：受限 .env 显式写 "
        f"{UPDATE_ENABLED_VAR}=0；{UPDATE_ENABLE_HOWTO}"
    )


def new_state(options: InstallOptions) -> dict[str, Any]:
    layout = Layout(options.dir, options.project)
    return {
        "schema": STATE_SCHEMA,
        "status": "installing",
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "finished_at": None,
        "exit_code": None,
        "installer": {
            "name": "ling",
            "version": INSTALLER_VERSION,
            "protocol": INSTALLER_PROTOCOL,
            "updater_protocol": UPDATER_PROTOCOL,
        },
        "project": options.project,
        "layout": {
            "dir": str(layout.root),
            "env_file": str(layout.env_file),
            "install_lock": str(layout.install_lock),
            "state_file": str(layout.state_file),
            "logs_dir": str(layout.logs_dir),
            "compose": {
                "dir": str(layout.compose_dir),
                "files": [],
                "source": str(options.compose_source),
                "source_sha256": None,
                "sha256": None,
                "files_group_sha256": None,
                "host_bind": options.host,
                "modifications": ["api.ports host ip -> ${LING_HOST}"],
            },
            # 更新器（独立 Compose 项目 `ling-updater`）需要的挂载路径与运行身份。
            # 只记录事实；只有 `--with-updater` 时才会拉起（见 Installer.start_updater）。
            "updater": {
                **updater_mounts(layout, with_updater=options.with_updater),
                **updater_host_paths(options),
            },
        },
        "listen": {
            "host": options.host,
            "port": options.port,
            "url": f"http://{options.host}:{options.port}",
            "reverse_proxy": options.reverse_proxy,
            "cookie_secure": options.reverse_proxy == "https",
        },
        "admin": {
            "username": options.admin_user,
            "password_source": "generated",
            "password_shown_once": False,
            # 口令本身绝不写进状态文件。
            "password_in_restricted_env": str(layout.env_file),
        },
        "test_mode": options.test_mode,
        "image": {
            "requested_digest": options.digest,
            "index_digest_hex": options.digest_hex,
            "platform_digest": None,
            "repository": IMAGE_REPOSITORY,
            "image_id": None,
            "architecture": None,
            "os": None,
            "source_commit": None,
            "source_commit_source": None,
            "version": None,
        },
        "database": {
            "image": DEFAULT_POSTGRES_IMAGE,
            "target_head": None,
            "alembic_version": None,
        },
        "volumes": {},
        "containers": {},
        "migration": {
            "target_head": None,
            "attempted": False,
            "executed_marker": False,
            "skip_marker": False,
            "exit_code": None,
            "log": None,
            "current_before": None,
            "current_after": None,
        },
        "backup": {
            "dir": str(options.backup_dir),
            "container_visible": False,
            "latest": None,
            "restore_point_ready": False,
            "requirements": {},
            "reason": "本次安装尚未产生恢复点",
        },
        "update": {
            "enabled": bool(options.enable_updates),
            "protocol": UPDATER_PROTOCOL,
            "reason": update_reason_for(options),
        },
        # 更新器运行时事实（与 `layout.updater` 的路径/身份分开记）：
        # 镜像 digest 只能来自发行包/CI 的发布证据（`--updater-digest` 或
        # `LING_UPDATER_IMAGE`）；缺失时**如实记 None**，绝不编一个 digest。
        # `update.enabled` 记的是**网页更新开关**（由 --enable-updates 决定，
        # 见 :func:`update_reason_for`），不是"更新器起了没有"——后者在下面这块。
        # `project` 记**实际用的**项目名：doctor/status 后续优先读它（见
        # :func:`updater_project_of`），不再按应用项目名重新派生。
        "updater": {
            "project": updater_project_for(options.project),
            "image": options.updater_digest,
            "image_configured": options.updater_digest is not None,
            "protocol": UPDATER_PROTOCOL,
            "requested": options.with_updater,
            "started": False,
            "started_at": None,
            "ownership": {},
            "env_file_sha256": None,
            "reason": (
                "更新器镜像未配置（缺 --updater-digest / LING_UPDATER_IMAGE）：网页更新不可用"
                if options.updater_digest is None
                else "更新器镜像已记录；本次安装未拉起更新器（网页更新开关单独记录）"
            ),
        },
        "checks": {},
        "result": {"readyz_http": None, "version": None, "version_commit": None},
        "failure": None,
        "next_actions": [],
    }


@dataclass
class InstallOptions:
    dir: Path
    backup_dir: Path
    project: str
    compose_source: Path
    digest: str
    digest_hex: str
    source_commit: str | None
    host: str
    port: int
    reverse_proxy: str
    admin_user: str
    test_mode: bool
    interactive: bool
    ready_timeout: float
    db_timeout: float
    worker_timeout: float
    migrate_timeout: float
    min_free_bytes: int
    # 更新器（独立 Compose 项目）：镜像 digest 只能来自发布证据，缺失就是 None。
    updater_digest: str | None
    updater_digest_hex: str | None
    with_updater: bool
    updater_code_dir: Path
    updater_compose_file: Path
    docker_socket: Path
    docker_socket_source: str
    docker_gid: int | None
    docker_gid_source: str
    updater_uid: int
    updater_gid: int
    updater_user_source: str
    # 网页更新开关（写进受限 .env 的 LING_UPDATE_ENABLED）。只可能为 True 当
    # with_updater 也为 True：参数解析阶段已拒绝"只有开关没有执行者"。
    enable_updates: bool = False
    host_arch: str = field(default_factory=host_arch)

    @property
    def layout(self) -> Layout:
        return Layout(self.dir, self.project)


# ---------------------------------------------------------------- 参数解析


def _env_text(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"{name} 不是数字：{raw!r}"
        ) from exc
    if value <= 0:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"{name} 必须 > 0（拿到 {value}）"
        )
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"{name} 不是整数：{raw!r}"
        ) from exc
    if value <= 0:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"{name} 必须 > 0（拿到 {value}）"
        )
    return value


def _parse_posix_id(raw: str, name: str) -> int:
    """解析 uid/gid（正整数）。**拒绝 0**：0 就是 root，与本批的非 root 目标相反。"""
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"{name} 必须是整数：{raw!r}"
        ) from exc
    if value == 0:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"{name} 不允许是 0（root）：更新器必须非 root 运行",
            ["改用镜像默认的 10002，或宿主上一个专用的非 root uid/gid。"],
        )
    if not 1 <= value <= 2**31 - 1:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"{name} 超出范围：{value}"
        )
    return value


def resolve_docker_socket(raw: str) -> tuple[Path, str]:
    """宿主 Docker socket 路径与来源：``--docker-socket`` → ``DOCKER_HOST`` → 默认值。

    只解析路径，不连 daemon；不存在也不算错（安装器随后按 stat 判断能否拿到 gid）。
    ``DOCKER_HOST`` 是 ``tcp://``/``ssh://`` 时**不**阻断应用安装（应用根本不用
    socket）：更新器只支持 unix socket，这种情况回落到默认路径并把来源记成
    ``default``，``--with-updater`` 会因为没有真实 socket 而明确拒绝。
    """
    value = (raw or "").strip()
    source = "argument"
    if not value:
        docker_host = os.environ.get("DOCKER_HOST", "").strip()
        if docker_host.startswith("unix://"):
            value = docker_host[len("unix://") :]
            source = "docker-host"
    if not value:
        value = DEFAULT_DOCKER_SOCKET
        source = "default"
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"--docker-socket 必须是绝对路径：{value!r}",
        )
    return path, source


def resolve_docker_gid(raw: str, socket_path: Path) -> tuple[int | None, str]:
    """socket 的 gid：显式值优先，否则 ``stat`` 宿主的 socket。

    拿不到就返回 ``(None, "unavailable")``——**不猜 0**。普通安装只记录这个事实；
    ``--with-updater`` 时由调用方拒绝启动（compose 的 ``group_add`` 是必填插值）。
    """
    value = (raw or "").strip() or _env_text("LING_DOCKER_GID")
    if value:
        return _parse_posix_id(value, "--docker-gid / LING_DOCKER_GID"), "explicit"
    try:
        info = os.stat(socket_path)
    except OSError:
        return None, "unavailable"
    return int(info.st_gid), "stat"


def resolve_updater_user() -> tuple[int, int, str]:
    """更新器的 (uid, gid, 来源)。

    1. 显式 ``LING_UPDATER_UID`` / ``LING_UPDATER_GID``（与 compose 的插值同名）；
    2. 否则若安装器不是 root：用**安装者自己**的 uid/gid（只有这些值能拥有刚写出的
       0600 ``.env`` 与 0700 目录，无需 root 也能让非 root 更新器真正跑起来）；
    3. 否则（root 安装）：镜像默认 10002:10002，由安装器 chown 宿主文件。

    任何情况下都**不会**回落到 0（root）。
    """
    raw_uid = _env_text("LING_UPDATER_UID")
    raw_gid = _env_text("LING_UPDATER_GID")
    if raw_uid or raw_gid:
        uid = _parse_posix_id(raw_uid or str(DEFAULT_UPDATER_UID), "LING_UPDATER_UID")
        gid = _parse_posix_id(raw_gid or str(DEFAULT_UPDATER_GID), "LING_UPDATER_GID")
        return uid, gid, "env"
    euid = os.geteuid() if hasattr(os, "geteuid") else 0
    if euid != 0:
        return euid, os.getegid(), "installer-user"
    return DEFAULT_UPDATER_UID, DEFAULT_UPDATER_GID, "default"


def build_parser() -> argparse.ArgumentParser:
    deploy_dir = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        prog="ling",
        description=(
            "ling 安装/状态/日志 CLI（镜像模式）。\n"
            "**不提供 curl | sh，也不下载任何东西**：本命令应由官方发行包提供，"
            "并只接受发行包/发布证据给出的固定 OCI 摘要（设计 §3.1）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "子命令：install（全新安装）、adopt（把现存部署登记成安装记录）、"
            "status（以实况为准核对）、logs（compose 日志）、doctor（只读诊断）、"
            "backup（复用 backup.sh）、restore-drill（隔离库恢复演练）。\n"
            "退出码：0 成功；1 status/doctor 不一致或失败；2 用法错误；10 预检；11 锁；"
            "12 写配置；14 拉取/身份；15 迁移；16 启动/验证。\n"
            "详细约束见 docs/EASY-INSTALL-UPDATE-DESIGN.md §3 与本文件 docstring。"
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command", metavar="{install,adopt,status,logs,doctor,backup,restore-drill}"
    )

    install = subparsers.add_parser(
        "install",
        help="从固定 OCI 摘要全新安装（不覆盖已有实例/配置）",
        description=(
            "全新安装 ling：写受限目录与配置、拉固定摘要镜像、起 db、单次显式迁移、"
            "起 api/worker 并验证 /readyz 恰好 200。\n"
            "已有受限 .env 或同名 Compose 项目/卷时**拒绝**执行（这不是升级命令）。\n"
            "交互模式只问用户能判断的项目；--non-interactive 全部走参数/环境变量。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "本命令**不下载安装包、不提供 curl | sh**：镜像摘要必须由官方发行包/\n"
            "发布证据给出；本命令只按该摘要安装并核对镜像身份（不校验发布签名）。\n"
            "\n"
            "非交互示例：\n"
            "  ling install --non-interactive \\\n"
            "    --digest 'ghcr.io/qsbb/ling-api@sha256:<64 位 hex>' \\\n"
            "    --dir /opt/ling --backup-dir /vol1/ling-backups \\\n"
            "    --host 127.0.0.1 --port 6520 --admin-user ling\n"
        ),
    )
    install.add_argument(
        "--digest",
        default=_env_text("LING_INSTALL_DIGEST") or _env_text("LING_APP_IMAGE"),
        help=(
            "目标镜像 index digest（必填）：ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>；"
            "也读 LING_INSTALL_DIGEST / LING_APP_IMAGE。绝不接受 latest 或 tag。"
        ),
    )
    install.add_argument(
        "--source-commit",
        default=_env_text("LING_INSTALL_SOURCE_COMMIT"),
        help="可选：期望的完整源提交（40 位 hex），与镜像 OCI 标注交叉核对",
    )
    install.add_argument(
        "--updater-digest",
        default=_env_text("LING_UPDATER_IMAGE"),
        help=(
            "更新器镜像 index digest（可选）："
            "ghcr.io/qsbb/ling-updater@sha256:<64 位小写 hex>；"
            "也读 LING_UPDATER_IMAGE。没给就**不配置更新器**（网页更新不可用），"
            "绝不猜 digest / 不写 latest。与 --with-updater 一起用才会真正拉起更新器。"
        ),
    )
    install.add_argument(
        "--with-updater",
        action="store_true",
        help=(
            "安装完成后显式拉起独立 Compose 项目 <项目名>-updater（默认 ling-updater；"
            "项目名由 --project 派生，多实例不会互相接管；需要 --updater-digest 与"
            "可确定的 docker socket gid）。默认**不拉起**。"
            "要真正打开网页更新，还要同时给 --enable-updates。"
        ),
    )
    install.add_argument(
        "--enable-updates",
        action="store_true",
        help=(
            "打开网页一键更新：与 --with-updater 一起用时在受限 .env 显式写 "
            "LING_UPDATE_ENABLED=1。**只给本参数、不给 --with-updater 会直接失败**："
            "没有更新器就没有执行者，开了开关只会让 POST 永远没人接。"
            "默认（不给本参数）仍写 LING_UPDATE_ENABLED=0，且是**显式写**，"
            "不留空——留空看不出意图。"
        ),
    )
    install.add_argument(
        "--docker-socket",
        default=_env_text("LING_DOCKER_SOCKET"),
        help=(
            f"宿主 Docker socket 路径（默认取 DOCKER_HOST=unix://… 或 {DEFAULT_DOCKER_SOCKET}）；"
            "只用于读取 gid 与写进更新器 env，不探测 daemon"
        ),
    )
    install.add_argument(
        "--docker-gid",
        default=_env_text("LING_DOCKER_GID"),
        help=(
            "宿主 docker socket 的 gid（更新器 group_add 用；默认用 stat 读 socket）。"
            "socket 尚不存在/读不到时必须显式给出，否则 --with-updater 会被拒绝"
        ),
    )
    install.add_argument(
        "--dir",
        default=_env_text("LING_INSTALL_DIR"),
        help="安装目录（默认 ~/ling；必须为空或不存在）",
    )
    install.add_argument(
        "--backup-dir",
        default=_env_text("LING_BACKUP_DIR"),
        help="备份目录（默认 <安装目录>-backups；不能位于安装目录内）",
    )
    install.add_argument(
        "--project",
        default=_env_text("LING_COMPOSE_PROJECT", DEFAULT_PROJECT),
        help="Compose 项目名（默认 ling）",
    )
    install.add_argument(
        "--compose-file",
        default=_env_text("LING_INSTALL_COMPOSE_FILE")
        or str(deploy_dir / COMPOSE_SOURCE_NAME),
        help="镜像模式主 Compose 文件（默认本脚本同级的 compose.image.yaml）",
    )
    install.add_argument(
        "--host",
        default=_env_text("LING_INSTALL_HOST"),
        help="对外监听地址（默认 127.0.0.1；0.0.0.0 或具体 IP 会提示 TLS/反代/防火墙）",
    )
    install.add_argument(
        "--port",
        default=_env_text("LING_INSTALL_PORT") or _env_text("LING_PORT"),
        help=f"宿主发布端口（默认 {DEFAULT_PORT}；容器内固定 6520）",
    )
    install.add_argument(
        "--reverse-proxy",
        choices=("none", "http", "https"),
        default=_env_text("LING_INSTALL_REVERSE_PROXY") or None,
        help=(
            "是否已有反向代理：none（默认）/ http / https；"
            "https 会写入 LING_COOKIE_SECURE=1"
        ),
    )
    install.add_argument(
        "--admin-user",
        default=_env_text("LING_BOOTSTRAP_ADMIN") or _env_text("ADMIN_USER"),
        help=f"初始管理员用户名（默认 {DEFAULT_ADMIN}）",
    )
    install.add_argument(
        "--test-mode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="测试模式：允许 fake provider，**不会调用真实模型**（默认关闭）",
    )
    install.add_argument(
        "--non-interactive",
        action="store_true",
        help="不提问：全部走参数/环境变量（stdin 不是 TTY 时也自动如此）",
    )
    install.add_argument(
        "--ready-timeout",
        type=float,
        default=_env_float("LING_INSTALL_READY_TIMEOUT_SECONDS", DEFAULT_READY_TIMEOUT),
        help=f"等待 /readyz 的秒数（默认 {DEFAULT_READY_TIMEOUT:g}）",
    )
    install.add_argument(
        "--db-timeout",
        type=float,
        default=_env_float("LING_INSTALL_DB_TIMEOUT_SECONDS", DEFAULT_DB_TIMEOUT),
        help=(
            "等待 db 就绪（healthcheck + 真实 psql 就绪探针/读版本的重试窗口）"
            f"的秒数（默认 {DEFAULT_DB_TIMEOUT:g}）"
        ),
    )
    install.add_argument(
        "--worker-timeout",
        type=float,
        default=_env_float("LING_INSTALL_WORKER_TIMEOUT_SECONDS", DEFAULT_WORKER_TIMEOUT),
        help=f"等待 worker healthy 的秒数（默认 {DEFAULT_WORKER_TIMEOUT:g}）",
    )
    install.add_argument(
        "--migrate-timeout",
        type=float,
        default=_env_float("LING_INSTALL_MIGRATE_TIMEOUT_SECONDS", DEFAULT_MIGRATE_TIMEOUT),
        help=f"一次性迁移命令的超时秒数（默认 {DEFAULT_MIGRATE_TIMEOUT:g}）",
    )
    install.add_argument(
        "--min-free-bytes",
        type=int,
        default=_env_int("LING_INSTALL_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES),
        help=f"安装/备份/Docker 目录各自的最小余量（默认 {DEFAULT_MIN_FREE_BYTES}）",
    )

    status = subparsers.add_parser(
        "status",
        help="以实况为准核对安装记录、容器、镜像、/readyz、卷与备份",
        description=(
            "读安装状态 + 实况（容器健康、运行镜像摘要、版本/提交、/readyz、卷身份、"
            "最近备份时间）。**以实况为准**：记录与实况不一致时逐条指出。\n"
            "退出码：0 一致且健康；1 有服务不健康或记录与实况不一致；2 没有安装记录；"
            "10 Docker 不可用。"
        ),
    )
    status.add_argument(
        "--dir",
        default=_env_text("LING_INSTALL_DIR"),
        help="安装目录（默认读取状态文件确定；无状态时默认 ~/ling）",
    )
    status.add_argument("--state", default="", help="安装状态 JSON 路径（默认 <dir>/install-state.json）")
    status.add_argument("--env-file", default="", help="受限 env（默认取状态/布局）")
    status.add_argument("--compose-file", default="", help="主 Compose 文件（默认取状态/布局）")
    status.add_argument(
        "--project",
        default=_env_text("LING_COMPOSE_PROJECT", ""),
        help="Compose 项目名（默认取状态，缺省 ling）",
    )
    status.add_argument(
        "--ready-timeout",
        type=float,
        default=_env_float("LING_INSTALL_READY_TIMEOUT_SECONDS", 5.0),
        help="本次 /readyz 探测的超时秒数（默认 5）",
    )

    logs = subparsers.add_parser(
        "logs",
        help="代理到 docker compose logs（同一文件组/项目名/--env-file）",
        description="代理到 `docker compose ... logs`。--follow 时按 Ctrl-C 退出。",
    )
    logs.add_argument(
        "--dir",
        default=_env_text("LING_INSTALL_DIR"),
        help="安装目录（默认读取状态文件确定；无状态时默认 ~/ling）",
    )
    logs.add_argument("--state", default="", help="安装状态 JSON 路径（默认 <dir>/install-state.json）")
    logs.add_argument("--env-file", default="", help="受限 env（默认取状态/布局）")
    logs.add_argument("--compose-file", default="", help="主 Compose 文件（默认取状态/布局）")
    logs.add_argument(
        "--project",
        default=_env_text("LING_COMPOSE_PROJECT", ""),
        help="Compose 项目名（默认取状态，缺省 ling）",
    )
    logs.add_argument(
        "--service",
        choices=("api", "worker", "db"),
        default="api",
        help="要查看的服务（默认 api）",
    )
    logs.add_argument("--tail", type=int, default=200, help="显示最后 N 行（默认 200）")
    logs.add_argument("-f", "--follow", action="store_true", help="持续跟随输出")

    doctor = subparsers.add_parser(
        "doctor",
        help="只读诊断这台机器上的这套安装（分级 ok/warn/fail）",
        description=(
            "只读诊断：Docker/Compose 版本与权限、镜像与备份两处磁盘余量、安装目录/"
            ".env/install-state.json/Compose 文件组及权限、记录 vs 实况（运行镜像摘要与"
            "image ID）、/readyz 恰好 200（不跟随 3xx）、容器内进程自报的提交、卷身份、"
            "alembic_version vs 镜像内 head、最近备份与恢复点数量、备份 cron、更新通道、"
            "GHCR 可达性。\n"
            "**本命令不修改任何东西**：不 run/up/pull、不建容器/卷、不写文件；"
            "也不会打开网页更新通道。\n"
            "退出码：0 没有 fail；1 存在 fail（含没有安装记录）；2 参数错误。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：ling doctor --dir /opt/ling\n"
            "      ling doctor --state /opt/ling/install-state.json --min-free-bytes 1073741824\n"
            "镜像内 Alembic head 通过在**运行中的 api 容器**里执行只读脚本算出，"
            "不会新建容器。"
        ),
    )
    doctor.add_argument(
        "--dir",
        default=_env_text("LING_INSTALL_DIR"),
        help="安装目录（默认读取状态文件确定；无状态时默认 ~/ling）",
    )
    doctor.add_argument(
        "--state", default="", help="安装状态 JSON 路径（默认 <dir>/install-state.json）"
    )
    doctor.add_argument("--env-file", default="", help="受限 env（默认取状态/布局）")
    doctor.add_argument("--compose-file", default="", help="主 Compose 文件（默认取状态/布局）")
    doctor.add_argument(
        "--project",
        default=_env_text("LING_COMPOSE_PROJECT", ""),
        help="Compose 项目名（默认取状态，缺省 ling）",
    )
    doctor.add_argument(
        "--ready-timeout",
        type=float,
        default=_env_float("LING_INSTALL_READY_TIMEOUT_SECONDS", 5.0),
        help="本次 /readyz 探测的超时秒数（默认 5；进程证据另有 60s 上限）",
    )
    doctor.add_argument(
        "--min-free-bytes",
        type=int,
        default=_env_int("LING_INSTALL_MIN_FREE_BYTES", DEFAULT_MIN_FREE_BYTES),
        help=(
            "镜像（Docker 根目录）与备份目录各自的最小余量（默认 "
            f"{DEFAULT_MIN_FREE_BYTES}）"
        ),
    )
    doctor.add_argument(
        "--max-backup-age-days",
        type=int,
        default=_env_int("LING_DOCTOR_MAX_BACKUP_AGE_DAYS", DEFAULT_BACKUP_MAX_AGE_DAYS),
        help=f"最近一次备份超过 N 天则告警（默认 {DEFAULT_BACKUP_MAX_AGE_DAYS}）",
    )

    def add_ops_common(subparser: argparse.ArgumentParser, *, dir_help: str) -> None:
        subparser.add_argument(
            "--dir",
            default=_env_text("LING_BACKUP_DIR"),
            help=dir_help,
        )
        subparser.add_argument(
            "--install-dir",
            default=_env_text("LING_INSTALL_DIR"),
            help="安装目录（默认读取状态文件确定；无状态时默认 ~/ling）",
        )
        subparser.add_argument(
            "--state", default="", help="安装状态 JSON 路径（默认 <安装目录>/install-state.json）"
        )
        subparser.add_argument("--env-file", default="", help="受限 env（默认取状态/布局）")
        subparser.add_argument(
            "--compose-file", default="", help="主 Compose 文件（默认取状态/布局）"
        )
        subparser.add_argument(
            "--project",
            default=_env_text("LING_COMPOSE_PROJECT", ""),
            help="Compose 项目名（默认取状态，缺省 ling）",
        )
        subparser.add_argument(
            "--timeout",
            type=float,
            default=_env_float("LING_BACKUP_TIMEOUT_SECONDS", DEFAULT_BACKUP_TIMEOUT),
            help=f"脚本超时秒数（默认 {DEFAULT_BACKUP_TIMEOUT:g}）",
        )

    backup = subparsers.add_parser(
        "backup",
        help="复用 deploy/backup/backup.sh 做一致备份并校验 manifest 关键字段",
        description=(
            "用安装时生成的**同一组** Compose 文件、同一 --env-file、同一项目名调用 "
            "deploy/backup/backup.sh（不重写备份逻辑）。备份目录默认取安装记录里的 "
            "backup.dir，可用 --dir 覆盖。\n"
            "备份完成后核对 manifest 的 dump_sha256 / keys_root_sha256 / media_state / "
            "tombstones_state / keys_env_set，并重算产物哈希；不完整则非 0。\n"
            "退出码：0 备份与校验通过；非 0 透传 backup.sh 的退出码；2 参数/安装记录错误；"
            "1 脚本返回 0 但产物不完整；124 超时。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：ling backup --install-dir /opt/ling\n"
            "      ling backup --install-dir /opt/ling --dir /vol1/ling-backups\n"
            "本命令不会修改安装状态文件；备份目录里的锁/轮转仍由 backup.sh 负责。"
        ),
    )
    add_ops_common(
        backup,
        dir_help="备份目录（默认取安装记录的 backup.dir；覆盖时务必与 cron 使用同一目录）",
    )

    drill = subparsers.add_parser(
        "restore-drill",
        help="复用 deploy/backup/restore_drill.sh 恢复到隔离库（库名以 _drill 结尾）",
        description=(
            "调用 deploy/backup/restore_drill.sh，把指定备份（默认最近一份）恢复到"
            "**隔离数据库**（库名必须以 _drill 结尾）并核对墓碑与媒体覆盖。\n"
            "默认使用与 backup 相同的 Compose 文件组 / --env-file / 项目名；"
            "脚本非 0 则本命令非 0。绝不覆盖生产库。\n"
            "退出码：0 演练通过；非 0 透传 restore_drill.sh 的退出码；2 参数/安装记录错误；"
            "124 超时。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：ling restore-drill --install-dir /opt/ling\n"
            "      ling restore-drill --install-dir /opt/ling \\\n"
            "        --dump /vol1/ling-backups/ling-20261104-043000.dump --db ling_drill\n"
            "演练只重建隔离库；生产库/生产容器不受影响。"
        ),
    )
    add_ops_common(
        drill,
        dir_help="备份目录（默认取安装记录的 backup.dir；--dump 给绝对路径时以它为准）",
    )
    drill.add_argument(
        "--dump",
        default="",
        help="指定备份的 dump 路径（默认取备份目录里最近一份 manifest 的 dump）",
    )
    drill.add_argument(
        "--db",
        default="",
        help=f"隔离库名（必须以 {DRILL_DB_SUFFIX} 结尾；默认 <project>{DRILL_DB_SUFFIX}）",
    )

    adopt = subparsers.add_parser(
        "adopt",
        help="把现存部署只读登记成安装记录（手工迁移后没有 install-state.json 时用）",
        description=(
            "只读采集运行中的 api/worker/db：运行镜像摘要与 image ID、容器内进程自报的"
            "版本/提交、卷身份、alembic_version、/readyz 恰好 200、备份目录；"
            "核对一致后用与 `ling install` **同一 schema** 写出 install-state.json，"
            "并额外记 adopted/adopted_at。\n"
            "任何一项核对不上就**拒绝**（宁可拒绝，不写错基线）：已有记录（除非 --force，"
            "且覆盖前先把旧文件备份并核对 sha256）、api/worker 不是同一 image ID、"
            "容器内 LING_COMMIT 与镜像 OCI 标注 revision 不一致、找不到 api 容器、"
            "/readyz 不是恰好 200、卷身份解析不出来、没有备份目录。\n"
            "本命令**只读**：不碰 compose、不 up/pull、不动容器，也绝不开启更新通道。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：ling adopt --install-dir /vol1/@appdata/ling-drill\n"
            "      ling adopt --install-dir /opt/ling --backup-dir /vol1/ling-backups\n"
            "备份目录默认取 <安装目录>/updater/.env 的 LING_BACKUP_DIR；都没有时必须给\n"
            "--backup-dir（adopt 不猜备份目录）。\n"
            "下一步：立刻 `ling doctor` 只读核对，再 `ling backup` 建立第一份恢复点。"
        ),
    )
    adopt.add_argument(
        "--install-dir",
        default=_env_text("LING_INSTALL_DIR"),
        help="安装目录（含受限 .env 与 compose/ 文件组；默认 ~/ling）",
    )
    adopt.add_argument(
        "--dir", default="", help="同 --install-dir（兼容 status/doctor 的参数名）"
    )
    adopt.add_argument(
        "--state", default="", help="安装状态 JSON 路径（默认 <安装目录>/install-state.json）"
    )
    adopt.add_argument("--env-file", default="", help="受限 env（默认 <安装目录>/.env）")
    adopt.add_argument(
        "--compose-file",
        action="append",
        default=None,
        help=(
            "主 Compose 文件（可重复给主文件 + 一个 override；"
            f"默认 <安装目录>/{COMPOSE_SUBDIR}/{COMPOSE_SOURCE_NAME}）"
        ),
    )
    adopt.add_argument(
        "--project",
        default=_env_text("LING_COMPOSE_PROJECT", ""),
        help="Compose 项目名（默认 ling）",
    )
    adopt.add_argument(
        "--port",
        default="",
        help=(
            "api 的宿主发布端口（可选）：必须与 docker compose config 的实况一致；"
            "adopt 不按它猜端口"
        ),
    )
    adopt.add_argument(
        "--backup-dir",
        default=_env_text("LING_BACKUP_DIR"),
        help=(
            "备份目录（默认取 <安装目录>/updater/.env 的 LING_BACKUP_DIR；"
            "都没有就拒绝，adopt 不猜）"
        ),
    )
    adopt.add_argument(
        "--ready-timeout",
        type=float,
        default=_env_float("LING_INSTALL_READY_TIMEOUT_SECONDS", 5.0),
        help="本次 /readyz 探测的超时秒数（默认 5）",
    )
    adopt.add_argument(
        "--force",
        action="store_true",
        help=(
            "覆盖已有 install-state.json：覆盖前先把它复制成 <name>.bak-<时间戳>、"
            "核对 sha256 并打印路径"
        ),
    )
    return parser


# ---------------------------------------------------------------- 交互


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{prompt}{suffix}: ").strip()
    except EOFError as exc:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "prompt",
            "交互输入结束（EOF）；非交互环境请用 --non-interactive 并给出参数",
            ["见 ling install --help 的参数/环境变量表。"],
        ) from exc
    return answer or default


def _ask_bool(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = _ask(f"{prompt} ({hint})").lower()
        if not raw:
            return default
        if raw in ("y", "yes", "是"):
            return True
        if raw in ("n", "no", "否"):
            return False
        print("请输入 y 或 n。")


def _ask_choice(prompt: str, choices: Sequence[tuple[str, str]], default: str) -> str:
    print(prompt)
    for index, (value, label) in enumerate(choices, start=1):
        mark = "（默认）" if value == default else ""
        print(f"  {index}) {label}{mark}")
    while True:
        raw = _ask("选择")
        if not raw:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1][0]
        values = [value for value, _label in choices]
        if raw in values:
            return raw
        print("请输入序号。")


def resolve_install_options(args: argparse.Namespace) -> InstallOptions:
    interactive = not args.non_interactive and sys.stdin.isatty() and sys.stdout.isatty()

    digest = (args.digest or "").strip()
    match = DIGEST_RE.fullmatch(digest)
    if match is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            (
                "拒绝非 digest 目标：只接受 "
                f"{IMAGE_REPOSITORY}@sha256:<64 位小写 hex>；拿到 {digest!r}。"
                "latest、可变 tag、空值、其他 registry/仓库、带额外路径或非 64 位 hex "
                "都会被拒绝。"
            ),
            [
                "摘要由官方发行包/CI 发布证据给出（设计 §3.1）；本命令不下载、"
                "不查 latest，也不代替你选择版本。",
            ],
        )
    source_commit = (args.source_commit or "").strip()
    if source_commit and COMMIT_RE.fullmatch(source_commit) is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"--source-commit 必须是 40 位小写 hex 的完整提交号；拿到 {source_commit!r}",
        )

    # 更新器镜像：只能来自发行包/发布证据。缺失不是错误（安装本身照样完成），
    # 但会如实记录「更新器镜像未配置、网页更新不可用」；绝不编一个 digest。
    raw_updater = (args.updater_digest or "").strip()
    updater_digest: str | None = None
    updater_digest_hex: str | None = None
    if raw_updater:
        updater_match = UPDATER_DIGEST_RE.fullmatch(raw_updater)
        if updater_match is None:
            raise LingError(
                EXIT_USAGE,
                "failed_usage",
                "arguments",
                (
                    "拒绝非 digest 的更新器镜像：只接受 "
                    f"{UPDATER_REPOSITORY}@sha256:<64 位小写 hex>；拿到 {raw_updater!r}。"
                    "latest、可变 tag、空值、其他仓库都会被拒绝。"
                ),
                [
                    "更新器镜像 digest 由 CI 发布证据给出；安装器不查 latest、不猜摘要。",
                ],
            )
        updater_digest = raw_updater
        updater_digest_hex = updater_match.group(1)

    with_updater = bool(getattr(args, "with_updater", False))
    enable_updates = bool(getattr(args, "enable_updates", False))
    # 开关与执行者必须成对：`LING_UPDATE_ENABLED=1` 只是"许可"，真正部署的是
    # `<项目名>-updater` 那个独立项目。只给开关不给更新器 = 网页上的按钮永远
    # 没人接（POST 写进 requests 目录却没人消费），是**明确失败**而不是警告。
    if enable_updates and not with_updater:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            (
                "--enable-updates 必须与 --with-updater 一起用："
                "没有更新器就没有执行者，只打开开关会让网页更新永远停在"
                "“已提交、没人执行”。"
            ),
            [
                "加上 --with-updater（并给出 --updater-digest / LING_UPDATER_IMAGE）后重试；"
                "只想装应用就别给 --enable-updates（.env 会显式写 LING_UPDATE_ENABLED=0）。",
                "打开前请先备好更新器镜像、发布目录条目与完整恢复点"
                "（`ling backup` + `ling restore-drill`）。",
            ],
        )
    if with_updater and updater_digest is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            (
                "--with-updater 需要 --updater-digest（或 LING_UPDATER_IMAGE）："
                "没有更新器镜像摘要就无法按固定 digest 启动，网页更新不可用。"
            ),
            [
                "从发布证据取得 ghcr.io/qsbb/ling-updater@sha256:<64 位 hex> 后重试；"
                "不加 --with-updater 也可以只装应用（安装器不会拉起更新器）。",
            ],
        )

    deploy_dir = Path(__file__).resolve().parent.parent
    updater_code_dir = Path(
        _env_text("LING_UPDATER_CODE_DIR") or str(deploy_dir)
    ).expanduser()
    if not updater_code_dir.is_absolute():
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"LING_UPDATER_CODE_DIR 必须是绝对路径：{updater_code_dir}",
        )
    updater_compose_file = (
        deploy_dir / UPDATER_COMPOSE_SUBDIR / UPDATER_COMPOSE_NAME
    )
    docker_socket, docker_socket_source = resolve_docker_socket(args.docker_socket)
    docker_gid, docker_gid_source = resolve_docker_gid(args.docker_gid, docker_socket)
    if with_updater and docker_gid is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            (
                f"无法从 {docker_socket} 读出 docker socket 的 gid（文件不存在或不可 stat）："
                "compose 的 group_add 是必填插值，不能猜，也不能回退 root。"
            ),
            [
                "确认 Docker 已安装并给出真实 socket 路径，或显式 --docker-gid "
                "（取法：stat -c %g <socket>）。",
            ],
        )
    updater_uid, updater_gid, updater_user_source = resolve_updater_user()

    project = (args.project or DEFAULT_PROJECT).strip()
    if PROJECT_RE.fullmatch(project) is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"项目名不合法：{project!r}（只允许小写字母/数字/_-，且以字母数字开头）",
        )

    compose_source = Path(args.compose_file).expanduser()
    if not compose_source.is_absolute():
        compose_source = (Path.cwd() / compose_source).resolve()
    else:
        compose_source = compose_source.resolve()

    home = Path.home()
    raw_dir = (args.dir or "").strip()
    if not raw_dir:
        if interactive:
            raw_dir = _ask("安装目录（项目数据/配置所在）", str(home / "ling"))
        else:
            raw_dir = str(home / "ling")
    install_dir = Path(raw_dir).expanduser().resolve()

    raw_backup = (args.backup_dir or "").strip()
    if not raw_backup:
        default_backup = install_dir.parent / f"{install_dir.name}-backups"
        if interactive:
            raw_backup = _ask("备份目录（建议放在另一块盘）", str(default_backup))
        else:
            raw_backup = str(default_backup)
    backup_dir = Path(raw_backup).expanduser().resolve()

    raw_host = (args.host or "").strip()
    if not raw_host:
        if interactive:
            raw_host = _ask_choice(
                "对外监听地址：",
                [
                    ("127.0.0.1", "仅本机（推荐；需要通过反代/隧道访问时也选这个）"),
                    ("0.0.0.0", "所有网卡（局域网可直连；不会自动开公网）"),
                ],
                DEFAULT_HOST,
            )
        else:
            raw_host = DEFAULT_HOST
    host = _validate_host(raw_host)
    if host not in LOOPBACK_HOSTS:
        print(
            "\n".join(
                [
                    "警告：你选择了非本机监听地址。",
                    "  * 本安装器**不会**替你开放公网，也不会配置 TLS/防火墙；",
                    "  * 请确认前面有 TLS 终止的反向代理，或在宿主防火墙上限定来源；",
                    "  * 没有 TLS 时 Cookie 可能明文传输；确认后再继续。",
                ]
            ),
            file=sys.stderr,
            flush=True,
        )

    raw_port = (args.port or "").strip() if isinstance(args.port, str) else args.port
    if raw_port in (None, ""):
        raw_port = _ask("宿主端口", str(DEFAULT_PORT)) if interactive else str(DEFAULT_PORT)
    port = _validate_port(raw_port)

    reverse_proxy = (args.reverse_proxy or "").strip()
    if not reverse_proxy:
        if interactive:
            reverse_proxy = _ask_choice(
                "是否已有反向代理（转发到本机端口）？",
                [
                    ("none", "没有：浏览器直接访问本机端口"),
                    ("http", "有，但对外仍是 HTTP（Cookie 不能设 Secure）"),
                    ("https", "有，且对外是 HTTPS/TLS 终结（写 LING_COOKIE_SECURE=1）"),
                ],
                "none",
            )
        else:
            reverse_proxy = "none"
    if reverse_proxy not in ("none", "http", "https"):
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"--reverse-proxy 只能是 none/http/https；拿到 {reverse_proxy!r}",
        )

    admin_user = (args.admin_user or "").strip()
    if not admin_user:
        admin_user = _ask("管理员用户名", DEFAULT_ADMIN) if interactive else DEFAULT_ADMIN
    if ADMIN_RE.fullmatch(admin_user) is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            (
                f"管理员用户名不合法：{admin_user!r}（只允许字母/数字/._@-，1–64 字符；"
                "它会写进受限 env，不能含空白/换行/引号）"
            ),
        )

    test_mode = args.test_mode
    if test_mode is None:
        env_test = _env_text("LING_TEST_MODE")
        if env_test:
            test_mode = env_test not in ("0", "", "false", "False", "no")
        elif interactive:
            print(
                "\n".join(
                    [
                        "",
                        "测试模式（test mode）说明：",
                        "  * 打开后允许 fake/内置 provider：**不会调用真实模型**；",
                        "  * 只适合离线试用、界面流程验收，不能用于真实对话；",
                        "  * 之后可在受限 .env 里把 LING_TEST_MODE 改成 0 并重启 api/worker。",
                    ]
                )
            )
            test_mode = _ask_bool("启用测试模式", False)
        else:
            test_mode = False

    for name, value in (
        ("--ready-timeout", args.ready_timeout),
        ("--db-timeout", args.db_timeout),
        ("--worker-timeout", args.worker_timeout),
        ("--migrate-timeout", args.migrate_timeout),
    ):
        if value <= 0:
            raise LingError(
                EXIT_USAGE, "failed_usage", "arguments", f"{name} 必须 > 0（拿到 {value}）"
            )
    if args.min_free_bytes <= 0:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"--min-free-bytes 必须 > 0（拿到 {args.min_free_bytes}）",
        )

    return InstallOptions(
        dir=install_dir,
        backup_dir=backup_dir,
        project=project,
        compose_source=compose_source,
        digest=digest,
        digest_hex=match.group(1),
        source_commit=source_commit or None,
        host=host,
        port=port,
        reverse_proxy=reverse_proxy,
        admin_user=admin_user,
        test_mode=test_mode,
        interactive=interactive,
        ready_timeout=args.ready_timeout,
        db_timeout=args.db_timeout,
        worker_timeout=args.worker_timeout,
        migrate_timeout=args.migrate_timeout,
        min_free_bytes=args.min_free_bytes,
        updater_digest=updater_digest,
        updater_digest_hex=updater_digest_hex,
        with_updater=with_updater,
        enable_updates=enable_updates,
        updater_code_dir=updater_code_dir,
        updater_compose_file=updater_compose_file,
        docker_socket=docker_socket,
        docker_socket_source=docker_socket_source,
        docker_gid=docker_gid,
        docker_gid_source=docker_gid_source,
        updater_uid=updater_uid,
        updater_gid=updater_gid,
        updater_user_source=updater_user_source,
    )


def _validate_host(raw: str) -> str:
    value = raw.strip()
    if value == "localhost":
        return "127.0.0.1"
    if value in LOOPBACK_HOSTS and value != "::1":
        return value
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"监听地址必须是 127.0.0.1、0.0.0.0 或一个 IPv4 地址；拿到 {raw!r}",
            ["IPv6 的 Compose 端口语法不同，本安装器暂不支持。"],
        ) from exc
    if address.version != 4:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"暂不支持 IPv6 监听地址：{raw!r}",
            ["使用 127.0.0.1 或 0.0.0.0，或具体 IPv4 地址。"],
        )
    return str(address)


def _validate_port(raw: Any) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"端口必须是整数：{raw!r}"
        ) from exc
    if not 1 <= value <= 65535:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"端口必须在 1–65535：{value}"
        )
    return value


# ---------------------------------------------------------------- 环境文件


def generate_secrets() -> dict[str, str]:
    return {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "LING_BOOTSTRAP_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "LING_ROOT_KEY": secrets.token_urlsafe(32),
    }


def render_env(options: InstallOptions, secret_values: dict[str, str]) -> str:
    cookie_secure = "1" if options.reverse_proxy == "https" else "0"
    lines = [
        "# 由 ling install 生成（受限配置，0600）。**不要提交到仓库**。",
        "# 已有本文件时安装器拒绝覆盖：升级/迁移不是覆盖安装（设计 §3.2）。",
        "# 下面三项是恢复材料的一部分：LING_ROOT_KEY 改变后已加密的通道密钥",
        "# 将无法解开；备份与根密钥必须按同一恢复点分别保管。",
        f"POSTGRES_PASSWORD={secret_values['POSTGRES_PASSWORD']}",
        f"LING_BOOTSTRAP_ADMIN={options.admin_user}",
        f"LING_BOOTSTRAP_ADMIN_PASSWORD={secret_values['LING_BOOTSTRAP_ADMIN_PASSWORD']}",
        f"LING_ROOT_KEY={secret_values['LING_ROOT_KEY']}",
        "",
        "# 0 = 只用真实模型；1 = 允许 fake provider（**不会调用真实模型**）。",
        f"LING_TEST_MODE={'1' if options.test_mode else '0'}",
        "",
        "# 反向代理在 https 上终结 TLS 时设 1（否则凭证可能走明文）。",
        f"LING_COOKIE_SECURE={cookie_secure}",
        "",
        "# 宿主侧监听：api 容器内固定 6520，这里只改宿主绑定与端口。",
        f"LING_HOST={options.host}",
        f"LING_PORT={options.port}",
        "",
        "# 固定 OCI index digest（api/worker 同一个镜像，靠 entrypoint 区分角色）。",
        f"LING_APP_IMAGE={options.digest}",
        f"LING_POSTGRES_IMAGE={DEFAULT_POSTGRES_IMAGE}",
        "LING_API_REPLICAS=1",
        "LING_WORKER_REPLICAS=1",
        "",
        "# 更新通道的宿主目录（compose.image.yaml 里是**必填**插值：缺一条",
        "# `docker compose config` 直接失败，不会静默少挂）。api 只挂这两个：",
        "# requests 读写（原子写请求文件）、status 只读（更新器写的 status.json）。",
        "# 目录由本安装器创建并设权限：requests 2770、state 0750，属组都是共享组。",
        f"LING_UPDATE_REQUESTS_HOST={options.layout.updater_requests_dir}",
        f"LING_UPDATE_STATUS_HOST={options.layout.updater_state_dir}",
        "# 共享组 gid：api 容器用 group_add 加入它才写得进 requests、读得到状态。",
        f"LING_UPDATE_SHARED_GID={options.updater_gid}",
        "",
        "# 网页一键更新的开关（compose.image.yaml 里是 ${LING_UPDATE_ENABLED:-0}，",
        "# 所以**这里显式写值**：0=关、1=开）。不写空：留空看不出意图，",
        "# 也让 compose 落回默认值、与本文件的说法不一致。",
        "# 1 只由 `--with-updater --enable-updates` 写出：开关需要执行者，",
        "# 且打开前应有完整恢复点（ling backup + ling restore-drill）。",
        f"LING_UPDATE_ENABLED={'1' if options.enable_updates else '0'}",
        "",
    ]
    return "\n".join(lines)


def preflight_env_values(options: InstallOptions) -> dict[str, str]:
    """预检 ``--env-file`` 的内容：**会改变 compose 解析结果**的渲染期值。

    预检必须对着**将要部署的那份模型**做门禁。此前这里写的是**空文件**，于是
    ``compose.image.yaml`` 里的 ``${LING_PORT:-6520}`` 落回 6520：``--port 6522``
    根本没进模型，端口占用检查对着 6520 去做、命中同机另一个实例，换端口安装
    永远被拒（「不同项目名/端口/空白卷并存安装」因此做不到）。

    这里只放**改变模型结构**的值（宿主绑定/端口/镜像/卷/副本）：

    - ``LING_HOST`` / ``LING_PORT``：api 的宿主绑定与发布端口；
    - ``LING_APP_IMAGE`` / ``LING_POSTGRES_IMAGE``：``${VAR:?}`` 必填插值与库镜像；
    - ``LING_API_REPLICAS`` / ``LING_WORKER_REPLICAS``：``deploy.replicas``；
    - ``LING_UPDATE_REQUESTS_HOST`` / ``LING_UPDATE_STATUS_HOST`` /
      ``LING_UPDATE_SHARED_GID``：更新通道的**必填**插值（宿主路径 + 共享组）；
    - ``LING_UPDATE_ENABLED``：网页更新开关的**最终值**（0/1）。它改变 api 容器
      的环境，预检模型必须与落盘后那份一致——否则"预检通过、落盘后 config 才
      发现开关不是 0"这类差异只会在装完之后才暴露。

    口令/根密钥类变量（``POSTGRES_PASSWORD``/``LING_ROOT_KEY`` 等）**刻意不在这里**：
    它们只填容器环境变量、不改变模型结构，而预检阶段不生成密钥；落盘之后
    :meth:`Installer.check_installed_compose_config` 会用真实的 ``.env`` 再核一遍。
    """
    layout = options.layout
    return {
        "LING_HOST": options.host,
        "LING_PORT": str(options.port),
        "LING_APP_IMAGE": options.digest,
        "LING_POSTGRES_IMAGE": DEFAULT_POSTGRES_IMAGE,
        "LING_API_REPLICAS": "1",
        "LING_WORKER_REPLICAS": "1",
        "LING_UPDATE_REQUESTS_HOST": str(layout.updater_requests_dir),
        "LING_UPDATE_STATUS_HOST": str(layout.updater_state_dir),
        "LING_UPDATE_SHARED_GID": str(options.updater_gid),
        "LING_UPDATE_ENABLED": "1" if options.enable_updates else "0",
    }


def render_preflight_env(options: InstallOptions) -> str:
    """把 :func:`preflight_env_values` 渲染成临时 ``--env-file``。

    键序固定（dict 插入序）：便于安装日志与测试逐项核对。
    """
    lines = [
        "# 由 ling install 生成的**预检**临时 env（安装结束即删除）。",
        "# 只含改变 compose 解析结果的渲染期值：预检模型必须与将要部署的模型一致。",
    ]
    lines += [
        f"{key}={value}" for key, value in preflight_env_values(options).items()
    ]
    return "\n".join(lines) + "\n"


def render_updater_env(options: InstallOptions) -> str:
    """生成更新器 Compose 项目的 ``--env-file``（``<dir>/updater/.env``，0600）。

    **只放更新器需要的宿主事实**：不复制应用口令/根密钥（那些仍只在
    ``<dir>/.env``，容器里以 ``/srv/ling/.env`` 读写）。缺 ``LING_UPDATER_IMAGE``
    时只写注释、不写空值或假 digest——compose 的 ``:?`` 会因此直接失败，这正是
    「更新器镜像未配置 ⇒ 网页更新不可用」的可执行表述。
    """
    layout = options.layout
    lines = [
        "# 由 ling install 生成（更新器专用，0600）。**不要提交到仓库**。",
        (
            f"# {updater_project_for(options.project)} 是独立 Compose 项目"
            "（项目名由应用项目名派生），本文件是它的 --env-file（变量插值）；"
        ),
        "# 它只含宿主路径/socket/运行身份，不含任何口令或根密钥。",
        "",
    ]
    if options.updater_digest:
        lines += [
            "# 固定 OCI index digest（更新器镜像；不接受 latest/可变 tag）。",
            f"LING_UPDATER_IMAGE={options.updater_digest}",
        ]
    else:
        lines += [
            "# LING_UPDATER_IMAGE 未配置：本次安装没有更新器镜像，网页更新不可用。",
            "# 补法：重新安装时给 --updater-digest "
            f"'{UPDATER_REPOSITORY}@sha256:<64 位 hex>'（或导出 LING_UPDATER_IMAGE）。",
            "# 缺这一行时 deploy/updater/compose.yaml 的 ${LING_UPDATER_IMAGE:?} 直接失败。",
        ]
    lines += [
        "",
        "# 应用安装根（含 .env、.ling-install.lock 与 compose/ 文件组）。",
        f"LING_INSTALL_DIR={layout.root}",
        "",
        "# 宿主 Docker socket 与它的组 gid（compose 的 group_add 必填插值）。",
        f"LING_DOCKER_SOCKET={options.docker_socket}",
    ]
    if options.docker_gid is not None:
        lines.append(f"LING_DOCKER_GID={options.docker_gid}")
    else:
        lines += [
            "# LING_DOCKER_GID 未配置：socket 不存在或不可 stat，且安装时没给 --docker-gid。",
            "# 缺这一行时 compose 的 group_add 插值直接失败（不会回退 root）。",
        ]
    lines += [
        "",
        "# 非 root 运行身份（compose 的 user: 必填插值；0 会被安装器拒绝）。",
        "# 这里的 gid 同时是更新通道的**共享组**：requests(2770)/state(0750) 的",
        "# 属组就是它，api 容器以 group_add 加入同一个 gid。",
        f"LING_UPDATER_UID={options.updater_uid}",
        f"LING_UPDATER_GID={options.updater_gid}",
        "",
        "# 受限目录（安装器创建：requests 2770 + setgid、state 0750、archive 2750；",
        "# 备份目录与 cron 同一个）。status.json 由更新器原子写在 state 里。",
        f"LING_UPDATER_REQUESTS_DIR={layout.updater_requests_dir}",
        f"LING_UPDATER_STATE_DIR={layout.updater_state_dir}",
        f"LING_BACKUP_DIR={options.backup_dir}",
        "",
        "# 发行包 deploy 目录（只读挂进容器：release/publish.py、backup/backup.sh）。",
        f"LING_UPDATER_CODE_DIR={options.updater_code_dir}",
        "",
        "# 本文件不含 LING_UPDATE_ENABLED：那个开关属于**应用**的受限 .env",
        "# （<dir>/.env），由 api 容器读取；更新器只消费请求目录，不该自己开它。",
        "",
    ]
    return "\n".join(lines)


def render_compose_copy(
    source_text: str, source_path: Path, source_sha: str, project: str
) -> str:
    lines: list[str] = []
    replaced = 0
    for raw in source_text.splitlines():
        if raw.strip() == COMPOSE_PORTS_LINE:
            indent = raw[: len(raw) - len(raw.lstrip())]
            lines.append(f"{indent}{COMPOSE_PORTS_REPLACEMENT}")
            replaced += 1
        else:
            lines.append(raw)
    if replaced != 1:
        raise LingError(
            EXIT_PREFLIGHT,
            "failed_preflight",
            "compose_source",
            (
                f"源 Compose 里必须恰好有一行 {COMPOSE_PORTS_LINE!r}，实际找到 "
                f"{replaced} 处；拒绝生成宿主绑定不明确的文件。"
            ),
            ["确认 --compose-file 指向镜像模式主文件 deploy/compose.image.yaml。"],
        )
    header = [
        "# 由 ling install 生成：稳定 Compose 文件组（项目 " + project + "）。",
        f"# 源文件：{source_path}（sha256 {source_sha}）。",
        "# 唯一改动：api 的宿主端口绑定加上 ${LING_HOST}（默认 127.0.0.1），",
        "# 避免 compose 默认把端口发布到 0.0.0.0。不要手工编辑；重新安装会拒绝覆盖。",
    ]
    return "\n".join(header + lines) + "\n"


# ---------------------------------------------------------------- 备份视图


def read_manifest(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return fields
    for raw in text.splitlines():
        if not raw or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        fields[key.strip()] = value.strip()
    return fields


def latest_manifest(backup_dir: Path) -> Path | None:
    if not backup_dir.is_dir():
        return None
    candidates = sorted(backup_dir.glob("manifest-*.txt"), key=lambda item: item.name)
    return candidates[-1] if candidates else None


def backup_status(backup_dir: Path, volume_mountpoints: Sequence[Path] = ()) -> dict[str, Any]:
    """读备份目录与最近一次 manifest；**不**声称"备份可用"。"""
    info: dict[str, Any] = {
        "dir": str(backup_dir),
        "exists": backup_dir.is_dir(),
        "writable": False,
        "free_bytes": None,
        "latest": None,
        "restore_point_ready": False,
        "requirements": {
            "dir_writable": False,
            "separate_filesystem": None,
            "restore_point_ready": False,
        },
        "reason": None,
    }
    probe = backup_dir if backup_dir.is_dir() else existing_ancestor(backup_dir.parent)
    try:
        usage = shutil.disk_usage(probe)
        info["free_bytes"] = usage.free
    except OSError:
        pass
    info["writable"] = os.access(probe, os.W_OK)
    info["requirements"]["dir_writable"] = bool(info["writable"])

    separate: bool | None = None
    try:
        backup_dev = os.stat(probe).st_dev
    except OSError:
        backup_dev = None
    if backup_dev is not None:
        for mountpoint in volume_mountpoints:
            try:
                if os.stat(mountpoint).st_dev == backup_dev:
                    separate = False
                    break
                separate = True if separate is None else separate
            except OSError:
                continue
    info["requirements"]["separate_filesystem"] = separate

    manifest = latest_manifest(backup_dir)
    if manifest is None:
        info["reason"] = "备份目录里没有 manifest-*.txt（尚无恢复点）"
        return info
    fields = read_manifest(manifest)
    latest = {
        "manifest": str(manifest),
        "manifest_sha256": sha256_file(manifest) if manifest.is_file() else None,
        "created_at": fields.get("created_at"),
        "dump": fields.get("dump"),
        "dump_sha256": fields.get("dump_sha256"),
        "media_state": fields.get("media_state"),
        "tombstones_state": fields.get("tombstones_state"),
        "keys_root": fields.get("keys_root"),
        "keys_env_set": fields.get("keys_env_set"),
    }
    info["latest"] = latest
    problems: list[str] = []
    if not latest["dump"]:
        problems.append("manifest 没有 dump")
    elif not (backup_dir / str(latest["dump"])).is_file():
        problems.append(f"dump 文件缺失：{latest['dump']}")
    if not latest["dump_sha256"]:
        problems.append("manifest 没有 dump_sha256")
    if latest["media_state"] != "present":
        problems.append(f"media_state={latest['media_state']!r}")
    has_keys = latest["keys_root"] == "present" or latest["keys_env_set"] == "true"
    if not has_keys:
        problems.append(
            f"keys_root={latest['keys_root']!r}（且 keys_env_set={latest['keys_env_set']!r}）"
        )
    if latest["tombstones_state"] != "ok":
        problems.append(f"tombstones_state={latest['tombstones_state']!r}")
    ready = not problems
    info["restore_point_ready"] = ready
    info["requirements"]["restore_point_ready"] = ready
    if not ready:
        info["reason"] = "最近恢复点不完整：" + "；".join(problems)
    return info


# ---------------------------------------------------------------- 安装


def parse_alembic_script_output(
    text: str,
) -> tuple[list[str], set[str], dict[str, list[str]]]:
    """解析 ``ALEMBIC_HEAD_SCRIPT`` 的 ``HEAD=/REV=/PARENT=`` 输出。"""
    heads: list[str] = []
    revisions: set[str] = set()
    parents: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("HEAD=") and "=" in line:
            heads.append(line.split("=", 1)[1].strip())
        elif line.startswith("REV=") and "=" in line:
            revisions.add(line.split("=", 1)[1].strip())
        elif line.startswith("PARENT=") and ":" in line:
            child, parent = line.split("=", 1)[1].split(":", 1)
            parents.setdefault(child.strip(), []).append(parent.strip())
    unique_heads = sorted({head for head in heads if head})
    return unique_heads, revisions, parents


# ---------------------------------------------------------------- 宿主发布端口
#
# 与 ``deploy/release/publish.py`` 的 ``parse_published_port`` /
# ``service_published_ports`` 保持**同一套语义**（这里同样复制而不是 import：
# 安装包与发布脚本都要能独立交付）。一致性由
# ``tests/test_cli_install.py::test_published_ports_match_publish_script`` 钉住。


def parse_published_port(raw: Any) -> list[int] | None:
    """解析一条 compose ``ports`` 条目的**宿主侧**值（``published``）。

    ``docker compose config --format json`` 给的是长语法，``published`` 通常是
    数字字符串（如 ``"6520"``）；Compose 也允许 ``"8000-8005"`` 这样的范围。
    返回展开后的宿主端口列表；**空值**表示由 Docker 随机分配（没有固定宿主机
    端口要核对），返回空列表；**无法解析返回 None**——调用方必须拒绝，不能把
    解析不了的端口当成"没有端口"。
    """
    if raw is None:
        return []
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return [raw] if 1 <= raw <= 65535 else None
    text = str(raw).strip()
    if not text:
        return []
    start_text, sep, end_text = text.partition("-")
    if sep:
        try:
            start, end = int(start_text), int(end_text)
        except ValueError:
            return None
        if not (1 <= start <= end <= 65535):
            return None
        if end - start + 1 > MAX_PUBLISHED_PORT_RANGE:
            return None
        return list(range(start, end + 1))
    try:
        port = int(text)
    except ValueError:
        return None
    return [port] if 1 <= port <= 65535 else None


def service_published_ports(services: dict[str, Any]) -> list[dict[str, Any]]:
    """从**合并后**的 compose 模型取出所有服务发布到宿主的端口。

    ``docker compose config --format json`` 已经应用了 ``-f`` override，而 Compose
    的 ``ports`` 是**追加**语义（override 只加条目，不覆盖主文件）。所以必须按
    合并后的模型对**每个服务**取并集：只盯 api 的 6520 会漏掉
    ``compose.image.ws.example.yaml`` 追加的 WS 端口——那类冲突要到切换阶段的
    ``up -d api`` 才由 Docker 报错，那时已经迁移过了，违反"预检拒绝、不改动"。

    返回 ``[{"service", "target", "published", "protocol"}]``（范围已展开），与
    publish.py 的同名函数逐字段一致；两边都不 import 对方，一致性由测试钉住。
    """
    result: list[dict[str, Any]] = []
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        for entry in service.get("ports") or []:
            if not isinstance(entry, dict):
                continue
            published = parse_published_port(entry.get("published"))
            if published is None:
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "compose_ports",
                    (
                        f"{name} 服务的 ports 条目宿主端口无法解析：{entry!r}；"
                        f"端口范围也最多展开 {MAX_PUBLISHED_PORT_RANGE} 个。"
                        "预检要逐个核对宿主端口，解析不了就不能放行。"
                    ),
                    ["检查 compose 的 ports 段（长格式 published）。"],
                )
            for port in published:
                result.append(
                    {
                        "service": str(name),
                        "target": entry.get("target"),
                        "published": port,
                        "protocol": str(entry.get("protocol") or "tcp"),
                    }
                )
    return result


class Installer:
    def __init__(self, options: InstallOptions) -> None:
        self.o = options
        self.layout = options.layout
        self.log = Logger()
        self.runner = Runner(self.log)
        self.state = new_state(options)
        self.config: dict[str, Any] = {}
        self.preflight_group: ComposeGroup | None = None
        self.preflight_model_group: ComposeGroup | None = None
        self.install_group: ComposeGroup | None = None
        self.tmpdir: Path | None = None
        self.lock: InstallLock | None = None
        self.created = {
            "root": False,
            "compose_dir": False,
            "logs_dir": False,
            "updater_dir": False,
            "updater_requests_dir": False,
            "updater_archive_dir": False,
            "updater_state_dir": False,
            "backup_dir": False,
        }
        self.target_image: dict[str, Any] = {}
        self.target_head: str | None = None
        self.target_revisions: set[str] = set()
        self.target_parents: dict[str, list[str]] = {}
        self.platform_digest: str | None = None
        self.published_port: int | None = None
        self.volume_names: dict[str, str] = {}
        self.container_started = False
        # 真实 psql 就绪探针是否已通过（``ensure_db_ready``）：重试耗尽时用来
        # 区分"db 始终没就绪"与"db 已就绪、但这次查询读不出"。
        self.db_ready = False
        # 就绪探针 + 迁移前读版本共用的重试截止时刻（monotonic）。
        self.db_deadline: float | None = None
        self.layout_created = False
        self.updater_started = False
        self.docker_root: Path | None = None
        self.source_sha256: str | None = None
        self.phase = "init"
        self.secret_values: dict[str, str] = {}
        self.password_shown = False
        self.say_lines: list[str] = []

    # ------------------------------------------------------------ 输出
    def say(self, line: str = "") -> None:
        print(line, flush=True)

    def section(self, title: str) -> None:
        self.say("")
        self.say(title)
        self.say("-" * max(20, len(title)))

    # ------------------------------------------------------------ 流程
    def run(self) -> int:
        try:
            self.phase = "preflight"
            self.preflight()
            self.phase = "lock"
            self.lock_and_verify()
            self.phase = "pull"
            self.pull()
            self.phase = "layout"
            self.create_layout()
            self.phase = "compose_config"
            self.check_installed_compose_config()
            self.phase = "database"
            self.start_db()
            self.phase = "migrate"
            self.migrate_once()
            self.phase = "start"
            self.start_api_worker()
            self.phase = "verify"
            self.verify()
            self.phase = "updater"
            self.start_updater()
            self.phase = "finish"
            self.finish()
        except Cancelled:
            self.say("已取消：未创建任何目录、容器或卷。")
            return EXIT_OK
        except LingError as exc:
            return self.handle_failure(exc)
        except OSError as exc:
            mapped = {
                "preflight": (EXIT_PREFLIGHT, "failed_preflight"),
                "lock": (EXIT_PREFLIGHT, "failed_preflight"),
                "pull": (EXIT_PULL, "failed_pull"),
                "layout": (EXIT_LAYOUT, "failed_layout"),
                "compose_config": (EXIT_LAYOUT, "failed_layout"),
                "database": (EXIT_POST_MIGRATION, "failed_db_ready"),
                "migrate": (EXIT_MIGRATE, "failed_migration"),
                "start": (EXIT_POST_MIGRATION, "failed_after_migration"),
                "verify": (EXIT_POST_MIGRATION, "failed_after_migration"),
                "updater": (EXIT_POST_MIGRATION, "failed_after_migration"),
                "finish": (EXIT_POST_MIGRATION, "failed_after_migration"),
            }
            code, status = mapped.get(self.phase, (EXIT_PREFLIGHT, "failed_preflight"))
            return self.handle_failure(
                LingError(
                    code,
                    status,
                    f"{self.phase}_io",
                    f"文件/目录操作失败（{self.phase}）：{exc}",
                    ["检查磁盘空间、权限与路径；先看安装日志与容器实况。"],
                    after_migration=self.migration_attempted(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 未预期异常也必须留下记录
            after = self.migration_attempted()
            status = "failed_after_migration" if after else "failed_preflight"
            if not after and self.phase in {"database", "migrate"}:
                status = "failed_db_ready"
            return self.handle_failure(
                LingError(
                    EXIT_POST_MIGRATION if after else EXIT_PREFLIGHT,
                    status,
                    "unexpected",
                    f"未预期异常：{type(exc).__name__}: {exc}",
                    ["保留安装目录与日志；不要根据退出码猜测状态，先核对容器/数据库实况。"],
                    after_migration=after,
                )
            )
        else:
            return EXIT_OK
        finally:
            self.release_lock()
            self.cleanup_tmpdir()

    # ------------------------------------------------------------ 锁
    def lock_and_verify(self) -> None:
        try:
            if not self.layout.root.exists():
                self.layout.root.mkdir(mode=0o700, parents=True)
                self.created["root"] = True
            else:
                os.chmod(self.layout.root, 0o700)
        except OSError as exc:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "paths",
                f"无法创建安装目录 {self.layout.root}：{exc}",
                ["检查父目录权限/磁盘；安装器不在不可写路径上落盘。"],
            ) from exc
        self.log.emit(f"抢安装锁：{self.layout.root / '.ling-install.lock'}")
        self.lock = InstallLock(self.layout.env_file)
        self.lock.acquire()
        self.state["locks"] = {"install": str(self.lock.path)}
        # 预检与抢锁之间可能已有别的安装完成：再核对一次（绝不覆盖）。
        self.check_layout_free()
        # `.env`/状态/compose 目录之外，容器/卷也可能刚好在预检之后被创建
        # （另一个安装器、或人手起的 `compose up`）。这两条 inspect 很便宜，
        # 抢到锁后再查一次，把并发窗口关掉。
        self.check_no_existing_instance(after_lock=True)

    def release_lock(self) -> None:
        if self.lock is not None:
            self.lock.release()
            self.lock = None

    def cleanup_tmpdir(self) -> None:
        if self.tmpdir is not None:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self.tmpdir = None

    # ------------------------------------------------------------ 预检
    def check_paths(self) -> None:
        root = self.layout.root
        if not root.is_absolute():
            raise LingError(
                EXIT_USAGE, "failed_usage", "arguments", f"安装目录必须是绝对路径：{root}"
            )
        if root == Path(root.anchor) or root == Path.home():
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "paths",
                f"拒绝把安装目录设为 {root}（根目录或家目录本身）",
                ["换一个专用目录，例如 ~/ling。"],
            )
        if root.exists() and not root.is_dir():
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "paths",
                f"安装目录已存在但不是目录：{root}",
            )
        self.check_layout_free()
        if self.o.backup_dir == root or root in self.o.backup_dir.parents:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "backup_dir",
                (
                    f"备份目录不能位于安装目录内（备份={self.o.backup_dir}，"
                    f"安装目录={root}）；同一棵目录树不能算独立故障域。"
                ),
                ["换到另一块盘的独立目录，例如 /vol1/backups/ling。"],
            )
        self.check_compose_source()
        if not self.layout.root.exists():
            ancestor = existing_ancestor(self.layout.root.parent)
            if not os.access(ancestor, os.W_OK):
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "paths",
                    f"安装目录的上级不可写：{ancestor}",
                )
        ancestor = existing_ancestor(self.o.backup_dir.parent)
        if not os.access(ancestor, os.W_OK):
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "backup_dir",
                f"备份目录的上级不可写：{ancestor}",
                ["先创建/修正备份目录（与未来 cron 的 LING_BACKUP_DIR 一致）。"],
            )

    def check_layout_free(self) -> None:
        """已有受限配置/状态/compose 组：一律拒绝（设计 §3.2「绝不覆盖」）。

        例外只有一种"不是安装产物"的形状：Docker 给缺失 bind 源自动建的**空目录**。
        `.env` / 锁的类型不对 → 报错并给出 rmdir 指引；`compose/` 是空目录 →
        本次安装直接接管（空目录不可能是另一个实例的部署目录）。
        """
        env_file = self.layout.env_file
        if os.path.lexists(env_file):
            if env_file.is_dir() and not env_file.is_symlink():
                # Docker 把缺失的 bind 源建成了目录：不是"已有的 .env"，
                # 也不能当配置覆盖——给出可执行的修复指引，别让人去查密钥。
                raise bind_source_error(env_file, "file", at_preflight=True)
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "env_exists",
                (
                    f"检测到已有受限配置：{env_file}。"
                    "安装器**绝不覆盖**已有 .env，也不会轮换其中的密钥/口令。"
                ),
                [
                    "这是一次全新安装，不是升级：现有实例请用发布脚本/迁移流程处理。",
                    "确认要重装时，先离线备份该 .env（含根密钥），再人工移走它。",
                ],
            )
        if self.layout.state_file.exists():
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "state_exists",
                f"检测到已有安装状态：{self.layout.state_file}；拒绝覆盖现有安装记录",
                ["用 `ling status` 检查现状；需要重装时先人工移走该目录（确认无数据卷）。"],
            )
        lock = self.layout.install_lock
        if os.path.lexists(lock) and (lock.is_symlink() or not lock.is_file()):
            # 锁必须是普通文件；目录/链接会让 flock 打不开，还可能被误报成"别人持锁"。
            raise bind_source_error(lock, "file", at_preflight=True)
        compose_dir = self.layout.compose_dir
        if os.path.lexists(compose_dir):
            if compose_dir.is_symlink() or not compose_dir.is_dir():
                raise bind_source_error(compose_dir, "dir", at_preflight=True)
            if any(compose_dir.iterdir()):
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "compose_dir_exists",
                    f"安装目录里已有 compose 文件组：{compose_dir}；拒绝覆盖",
                    ["确认这不是另一个实例的部署目录；本命令只做全新安装。"],
                )
            # 空目录就是 Docker 给缺失 bind 源建的壳：直接接管，不逼用户 rmdir。
            self.log.emit(f"接管 Docker 预建的空 compose 目录：{compose_dir}")

    def check_compose_source(self) -> None:
        source = self.o.compose_source
        if not source.is_file():
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_source",
                f"Compose 主文件不存在：{source}",
                ["确认 --compose-file 指向发行包里的镜像模式主文件。"],
            )
        if source.stat().st_size > 1024 * 1024:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_source",
                f"Compose 主文件过大（>1 MiB）：{source}",
            )
        text = source.read_text(encoding="utf-8")
        self.source_sha256 = sha256_file(source)
        count = sum(1 for line in text.splitlines() if line.strip() == COMPOSE_PORTS_LINE)
        if count != 1:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_source",
                (
                    f"Compose 主文件里 {COMPOSE_PORTS_LINE!r} 出现 {count} 次（应为 1）；"
                    "拒绝安装（宿主绑定不明确）。"
                ),
                ["确认是 deploy/compose.image.yaml，而不是源码模式 compose.yaml。"],
            )

    def check_docker(self) -> None:
        server = self.runner.run(["docker", "info", "--format", "{{.ServerVersion}}"])
        if server.returncode != 0:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "docker",
                (
                    "Docker 不可用或当前用户无权限："
                    f"{tail(server.stderr or server.stdout)}"
                ),
                [
                    "确认 docker 服务在运行；把用户加入 docker 组（重新登录后生效），"
                    "不要用 sudo 跑整条安装命令来绕过权限。",
                ],
            )
        compose = self.runner.run(["docker", "compose", "version", "--short"])
        if compose.returncode != 0:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose",
                f"缺少 Docker Compose v2：{tail(compose.stderr or compose.stdout)}",
                ["安装 docker compose v2 插件后重跑；镜像模式需要 config --format json。"],
            )
        raw_version = compose.stdout.strip().lstrip("v")
        match = re.match(r"^(\d+)\.(\d+)", raw_version)
        if match is None:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose",
                f"无法解析 Compose 版本：{raw_version!r}；无法证明兼容性",
            )
        major = int(match.group(1))
        minor = int(match.group(2))
        if major < 2:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose",
                f"需要 Docker Compose v2（当前 {raw_version}）；v1 不支持 compose spec",
            )
        # 与 doctor 的 compose_version_row 同一个口径：只有 major == 2 才比 minor。
        # 旧实现只比 minor，会把 5.3.1（更新的 v5）报成"较旧（建议 2.20+）"。
        if major == 2 and minor < 20:
            self.log.emit(
                f"注意：Compose {raw_version} 较旧；本安装器要求 config --format json 与 "
                "pull/up 的 --pull=never，建议升级到 2.20+"
            )
        root_dir = self.runner.run(["docker", "info", "--format", "{{.DockerRootDir}}"])
        docker_root: Path | None = Path(root_dir.stdout.strip() or "/var/lib/docker")
        if root_dir.returncode != 0 or docker_root is None or not docker_root.is_dir():
            self.log.emit(
                f"注意：无法读取 Docker 根目录（{docker_root}）；本次不检查 Docker 侧余量"
            )
            docker_root = None
        self.state["checks"]["docker"] = {
            "server_version": server.stdout.strip(),
            "compose_version": raw_version,
            "docker_root": str(docker_root) if docker_root is not None else None,
        }
        self.docker_root = docker_root

    def published_port_checks(self) -> list[dict[str, Any]]:
        """预检要逐个核对的宿主端口：命令给的 API 端口 + 合并模型里所有发布端口。

        ``docker compose config`` 已经应用了 ``-f`` override，而 Compose 的
        ``ports`` 是**追加**语义：只看 ``--port`` / api 的 6520 会漏掉
        ``compose.image.ws.example.yaml`` 追加的 WS 端口，那类冲突要到迁移后的
        ``up -d api`` 才由 Docker 报错。这里先按 (port, protocol) 去重，再逐个
        核对；API 端口即使模型里解析不到也一定在内（随后
        :meth:`check_compose_model` 会核对模型与命令一致）。
        """
        services = self.config.get("services")
        entries = service_published_ports(services) if isinstance(services, dict) else []
        checks: list[dict[str, Any]] = [
            {
                "service": "api",
                "target": DEFAULT_PORT,
                "published": self.o.port,
                "protocol": "tcp",
            }
        ]
        seen = {(int(self.o.port), "tcp")}
        for item in entries:
            key = (int(item["published"]), str(item["protocol"]))
            if key in seen:
                continue
            seen.add(key)
            checks.append(item)
        return checks

    def probe_free_port(self, host: str, port: int) -> None:
        """真实 socket bind：端口被**宿主进程**占着时这里就失败（docker ps 看不出）。

        只用于命令给的 API 端口：compose 里它的 ``host_ip`` 必须逐字符等于
        ``--host``，绑定同一地址才能证明空闲。其余发布端口（override 追加的 WS
        端口）的宿主地址可能是 ``0.0.0.0``；``SO_REUSEADDR`` 下"绑定通配地址成功"
        并不代表其它网卡上没人监听，所以不用它冒充保证，那类端口按
        :meth:`check_port` 的容器占用者核对（与 publish.py 同一口径）。
        """
        probe_host = host
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((probe_host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRNOTAVAIL:
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "port",
                    f"监听地址 {probe_host} 不属于本机（{exc.strerror or exc}）",
                    ["改用 127.0.0.1/0.0.0.0，或一个确实配置在本机接口上的地址。"],
                ) from exc
            if exc.errno == errno.EACCES:
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "port",
                    (
                        f"没有权限绑定 {probe_host}:{port}（{exc.strerror or exc}），"
                        "无法证明端口空闲"
                    ),
                    ["换一个 >=1024 的宿主端口，或用有权限的账号安装。"],
                ) from exc
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "port",
                f"端口 {probe_host}:{port} 已被占用（{exc.strerror or exc}）",
                [
                    "找出占用者（docker ps / ss -ltnp）后换端口或停掉它；"
                    "不要用 kill/prune 猜测处理。",
                ],
            ) from exc
        finally:
            sock.close()

    def check_port(self) -> None:
        """逐个核对**所有**宿主发布端口（api 端口与 override 追加的 WS 端口）。

        端口集合来自 ``docker compose config`` 的**合并后模型**：Compose 的
        ``ports`` 是追加语义，只看 ``--port`` 会漏掉 ``compose.image.ws.example.yaml``
        追加的 WS 端口，那类冲突要到迁移之后的 ``up -d api`` 才由 Docker 报错。
        对每个端口查 ``docker ps --filter publish=PORT/PROTOCOL``（按条目的
        protocol，不一律 tcp），占用者一律预检失败；此时还没有创建安装目录/卷/
        容器。API 端口另外做真实 bind，能发现宿主进程。
        """
        checks = self.published_port_checks()
        self.probe_free_port(self.o.host, self.o.port)
        for item in checks:
            port = int(item["published"])
            protocol = str(item["protocol"])
            result = self.runner.run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    f"publish={port}/{protocol}",
                    "--format",
                    '{{.ID}}|{{.Label "com.docker.compose.project"}}|{{.Names}}',
                ]
            )
            if result.returncode != 0:
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "port",
                    f"检查端口 {port}/{protocol} 的容器占用失败：{tail(result.stderr or result.stdout)}",
                )
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "port",
                    (
                        f"宿主端口 {port}/{protocol} 已被容器占用：{line.strip()}"
                        f"（{item['service']} target={item['target']}）"
                    ),
                    ["确认占用者；安装器不会替你停掉别人的容器。"],
                )
        self.log.emit(
            "宿主端口全部可用："
            + "、".join(
                f"{item['service']}:{item['published']}/{item['protocol']}" for item in checks
            )
        )
        self.state["checks"]["port"] = {
            "host": self.o.host,
            "port": self.o.port,
            "free": True,
            "published": [
                {
                    "service": item["service"],
                    "target": item["target"],
                    "published": item["published"],
                    "protocol": item["protocol"],
                }
                for item in checks
            ],
        }

    def check_disk(self) -> None:
        targets = {
            "docker_root": self.docker_root,
            "install_dir": existing_ancestor(self.layout.root),
            "backup_dir": existing_ancestor(self.o.backup_dir),
        }
        budget: dict[str, Any] = {}
        problems: list[str] = []
        for name, path in targets.items():
            if path is None:
                budget[name] = None
                continue
            try:
                usage = shutil.disk_usage(path)
            except OSError as exc:
                if name == "docker_root":
                    budget[name] = {"path": str(path), "error": str(exc)}
                    continue
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "disk",
                    f"无法读取 {name} 的磁盘余量（{path}）：{exc}",
                ) from exc
            budget[name] = {"path": str(path), "free": usage.free, "need": self.o.min_free_bytes}
            if usage.free < self.o.min_free_bytes:
                problems.append(
                    f"{name}（{path}）余量 {usage.free} B < 需要 {self.o.min_free_bytes} B"
                )
        self.state["checks"]["disk"] = budget
        if problems:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "disk",
                "磁盘余量不足：" + "；".join(problems),
                [
                    "清理/扩容后重跑；不要为了腾空间 prune 正在使用的卷。",
                    "预算可用 --min-free-bytes 按现场调整（仍会与真实余量比较）。",
                ],
            )
        self.log.emit(f"磁盘检查通过（每个目标至少需要 {self.o.min_free_bytes} B）")

    def check_registry(self) -> None:
        result = self.runner.run(["docker", "manifest", "inspect", self.o.digest], timeout=60.0)
        if result.returncode != 0:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "registry",
                (
                    "无法读取目标摘要的 manifest（GHCR 不可达或无权限）："
                    f"{tail(result.stderr or result.stdout)}"
                ),
                [
                    "确认网络可达 ghcr.io；私有包需要宿主受限凭据（docker login ghcr.io）。",
                    "核对 digest 确实由 CI 推送；不要改用 latest 或 tag 试探。",
                ],
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "registry",
                f"docker manifest inspect 输出不是 JSON：{exc}",
            ) from exc
        platforms = manifest_platforms(data)
        wanted = f"linux/{self.o.host_arch}"
        if platforms:
            for entry in platforms:
                if entry.get("platform") == wanted:
                    digest = entry.get("digest")
                    if isinstance(digest, str) and HEX64_RE.fullmatch(digest):
                        self.platform_digest = digest
                    break
            else:
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "registry_platform",
                    (
                        f"目标摘要没有 {wanted} 平台；镜像平台："
                        f"{sorted(str(entry.get('platform')) for entry in platforms)}"
                    ),
                    [
                        f"宿主架构是 {self.o.host_arch}（uname -m）；"
                        "确认 CI 构建的平台，或换用对应架构的发布摘要。",
                    ],
                )
            self.log.emit(
                f"GHCR 可达；匹配平台 {wanted}"
                + (f"（manifest digest {self.platform_digest}）" if self.platform_digest else "")
            )
        else:
            self.log.emit(
                "GHCR 可达；该摘要不是多架构 index（平台将在 pull 后按 image inspect 核对）"
            )
        self.state["image"]["platform_digest"] = self.platform_digest
        self.state["checks"]["registry"] = {
            "reachable": True,
            "platforms": [str(entry.get("platform")) for entry in platforms],
            "matched": wanted if platforms else None,
        }

    def compose_config_json(self, group: ComposeGroup, *, step: str) -> dict[str, Any]:
        exit_code = EXIT_PREFLIGHT if step == "preflight" else EXIT_LAYOUT
        status = "failed_preflight" if step == "preflight" else "failed_layout"
        result = self.runner.run(
            [*group.argv(), "config", "--format", "json"],
            env=self.docker_env(),
            timeout=DEFAULT_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            raise LingError(
                exit_code,
                status,
                "compose_config",
                (
                    "docker compose config 解析失败"
                    f"（rc={result.returncode}）：{tail(result.stderr or result.stdout)}"
                ),
                [
                    "检查 compose 文件、--env-file、项目名与 LING_APP_IMAGE 插值；"
                    "不要用另一份 compose 文件替代。",
                ],
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise LingError(
                exit_code,
                status,
                "compose_config",
                f"docker compose config 输出不是 JSON：{exc}",
                ["需要 Compose v2（支持 config --format json）。"],
            ) from exc
        if not isinstance(data, dict):
            raise LingError(
                exit_code, status, "compose_config", "docker compose config 输出不是对象"
            )
        return data

    def check_preflight_model_ports(self, config: dict[str, Any]) -> None:
        """硬校验：预检模型里的 api 宿主端口/绑定必须**就是** ``--port`` / ``--host``。

        为什么单独一刀、且放在端口占用检查**之前**：预检 env 漏一个渲染期变量
        （最典型是 ``LING_PORT``）时 Compose 会落回模板默认值，模型与命令指向的
        不是同一条拓扑。旧顺序是"先按错模型查占用、再核对模型"，于是错的那一步
        先抛错，真正的不一致永远看不到——报出来的是"6520 被别的实例占着"，而
        ``--port 6522`` 其实是干净的（"并发实例装不上"就是这么来的）。

        这里只核对"模型 == 命令"，不代替 :meth:`check_port` 的占用检查。
        """
        api = (config.get("services") or {}).get("api")
        if not isinstance(api, dict):
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_model",
                "预检模型里没有 api 服务；无法证明 --port/--host 已生效",
                ["确认 --compose-file 指向镜像模式主文件 deploy/compose.image.yaml。"],
            )
        matched: dict[str, Any] | None = None
        for entry in api.get("ports") or []:
            if isinstance(entry, dict) and str(entry.get("target")) == str(DEFAULT_PORT):
                matched = entry
                break
        if matched is None:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_model",
                f"预检模型里 api 没有发布容器侧 {DEFAULT_PORT}；无法证明 --port/--host 已生效",
                [
                    "确认 compose 的 api.ports 仍是 "
                    '"${LING_HOST:-127.0.0.1}:${LING_PORT:-6520}:6520" 这种插值形式。',
                ],
            )
        published = str(matched.get("published") or "")
        if published != str(self.o.port):
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_model",
                (
                    "预检模型与 --port 不一致：docker compose config 解析出的 api "
                    f"宿主端口是 {published or '（空）'}，命令给的是 {self.o.port}；"
                    "预检 env 里的 LING_PORT 没有进模型。"
                ),
                [
                    "检查宿主 shell 是否导出了 LING_PORT（Compose 的 shell 环境优先于 --env-file）。",
                    "确认 compose 的 api.ports 用 ${LING_PORT:-6520} 插值，不要写死端口。",
                ],
            )
        host_ip = str(matched.get("host_ip") or "")
        if host_ip != self.o.host:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "compose_model",
                (
                    "预检模型与 --host 不一致：docker compose config 解析出的 api "
                    f"宿主绑定是 {host_ip or '0.0.0.0（所有网卡）'}，命令给的是 "
                    f"{self.o.host}；预检 env 里的 LING_HOST 没有进模型。"
                ),
                [
                    "检查宿主 shell 是否导出了 LING_HOST（Compose 的 shell 环境优先于 --env-file）。",
                    "确认生成的 compose 副本里 api.ports 带 ${LING_HOST:-127.0.0.1}。",
                ],
            )
        self.log.emit(
            f"预检模型与命令一致：api 发布 {host_ip}:{published}"
            f"（容器内固定 {DEFAULT_PORT}）"
        )

    def check_compose_model(
        self, config: dict[str, Any], *, require_host_bind: bool
    ) -> None:
        def fail(message: str, next_actions: Sequence[str] = ()) -> None:
            raise LingError(
                EXIT_PREFLIGHT, "failed_preflight", "compose_model", message, next_actions
            )

        if config.get("name") != self.o.project:
            fail(
                f"compose 解析出的项目名是 {config.get('name')!r}，不是 "
                f"{self.o.project!r}；会建到另一套项目/卷上。"
            )
        services = config.get("services")
        if not isinstance(services, dict):
            fail("compose 配置里没有 services")
        for name in ("db", "api", "worker"):
            if name not in services:
                fail(f"compose 缺少服务 {name}")
        api = services["api"]
        worker = services["worker"]
        db = services["db"]
        if api.get("image") != self.o.digest or worker.get("image") != self.o.digest:
            fail(
                "compose 解析出的 api/worker 镜像不是同一个目标 digest："
                f"api={api.get('image')!r} worker={worker.get('image')!r} "
                f"target={self.o.digest!r}；镜像模式下两者必须逐字符相同。"
            )
        for name in ("api", "worker"):
            if "build" in services[name]:
                fail(
                    f"{name} 服务仍有 build:；这不是镜像模式的主文件",
                    ["使用 compose.image.yaml，不要与源码模式 compose.yaml 叠加。"],
                )
        api_env = service_environment(api)
        # 开关的**唯一来源**是受限 .env（compose 里是 ${LING_UPDATE_ENABLED:-0}）。
        # 这里核对"解析结果 == 本次安装要的值"：安装器不给开关，就不许 compose
        # 模型里出现 1（宿主 shell 里残留的同名变量会被 docker_env/--env-file
        # 的实际值覆盖，真出现了说明有人手工改过 compose 或 env）。
        expected_updates = "1" if self.o.enable_updates else "0"
        if api_env.get("LING_UPDATE_ENABLED") != expected_updates:
            fail(
                "compose 的 api 环境里 LING_UPDATE_ENABLED 是 "
                f"{api_env.get('LING_UPDATE_ENABLED')!r}，本次安装要的是 "
                f"{expected_updates!r}；开关只能由 --enable-updates（0/1）决定。",
                [
                    "检查是否有宿主 shell 导出的 LING_UPDATE_ENABLED（Compose 里"
                    "进程环境优先于 --env-file），以及 compose 文件是否被改成字面量。",
                ],
            )
        if api_env.get("LING_RUN_MIGRATIONS") != "0":
            fail(
                "compose 的 api 环境里 LING_RUN_MIGRATIONS 不是 0；"
                "迁移必须由安装器单次显式执行"
            )
        for name, service in (("db", db), ("api", api), ("worker", worker)):
            healthcheck = service.get("healthcheck")
            if (
                not isinstance(healthcheck, dict)
                or healthcheck.get("disable") is True
                or not healthcheck.get("test")
            ):
                fail(
                    f"{name} 没有可用的 healthcheck；安装器不能把"
                    "“进程起来”当成“服务可用”。",
                    ["恢复 compose 里的 healthcheck，不要用 disable 绕过。"],
                )

        volumes = config.get("volumes") or {}
        names: dict[str, str] = {}
        for key in ("app-data", "db-data"):
            entry = volumes.get(key)
            if not isinstance(entry, dict) or not entry.get("name"):
                fail(f"compose 解析结果缺少卷 {key} 的实际名称")
            name = str(entry["name"])
            expected = f"{self.o.project}_{key}"
            if name != expected:
                fail(
                    f"卷 {key} 的实际名称是 {name!r}，期望 {expected!r}；"
                    "会用到另一个卷或创建同名空卷。"
                )
            names[key] = name
        self.volume_names = names

        ports = api.get("ports") or []
        if not isinstance(ports, list) or not ports:
            fail("api 没有发布宿主端口（安装器无法验证 /readyz）")
        matched = None
        for entry in ports:
            if not isinstance(entry, dict):
                continue
            try:
                target = int(entry.get("target"))
            except (TypeError, ValueError):
                continue
            if target == DEFAULT_PORT:
                matched = entry
                break
        if matched is None:
            fail(
                "api 没有发布容器侧 6520；安装器按 target=6520 验证 /readyz，"
                "端口不对时不会猜。"
            )
        published = str(matched.get("published") or "")
        if published != str(self.o.port):
            fail(
                f"compose 实际发布的宿主端口是 {published!r}，命令给的是 "
                f"{self.o.port}；安装器不按猜测端口验证。",
                ["改 --port 或检查 .env 的 LING_PORT；不要只改一处。"],
            )
        if require_host_bind:
            host_ip = str(matched.get("host_ip") or "")
            if host_ip != self.o.host:
                fail(
                    f"compose 实际绑定的宿主地址是 {host_ip or '0.0.0.0（所有网卡）'!r}，"
                    f"期望 {self.o.host!r}；拒绝在未声明的监听地址上启动。",
                    ["检查生成的文件组与 LING_HOST；安装器不会替你放宽为 0.0.0.0。"],
                )
        self.published_port = int(published)
        self.state["checks"]["compose"] = {
            "project": config.get("name"),
            "image": api.get("image"),
            "ports": ports,
            "volumes": names,
            "database_image": db.get("image"),
        }
        self.state["database"]["image"] = db.get("image")

    def check_no_existing_instance(self, *, after_lock: bool = False) -> None:
        """同名项目/容器/卷：预检查一次，抢到锁后再查一次（``after_lock``）。

        ``after_lock`` 只因**并发窗口**而存在：预检与抢锁之间别人可能刚建好同名
        容器/卷；抢锁后复核 `.env`/状态/compose 目录并不覆盖这一层。报错文案要
        说明"预检之后出现了同名资源"，避免用户以为第一次检查漏了。
        """
        prefix = "预检之后出现了同名资源：" if after_lock else ""
        containers = self.list_project_containers(all_states=True)
        if containers:
            detail = "、".join(
                f"{item['name']}（{item['state']}）" for item in containers[:5]
            )
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "existing_project",
                (
                    f"{prefix}项目 {self.o.project!r} 下已存在容器：{detail}。"
                    "这看起来是**现有实例**，不是空白机器。"
                ),
                self.instance_migration_actions(),
            )
        for key, name in self.volume_names.items():
            result = self.runner.run(["docker", "volume", "inspect", name])
            if result.returncode == 0:
                raise LingError(
                    EXIT_PREFLIGHT,
                    "failed_preflight",
                    "existing_volume",
                    (
                        f"{prefix}命名卷 {name}（{key}）已存在。安装器**绝不**另建/复用同名"
                        "空卷，也不会覆盖现有数据。"
                    ),
                    self.instance_migration_actions(),
                )
        self.log.emit("未发现同名 Compose 项目/容器/卷（不会覆盖任何现有实例）")

    def instance_migration_actions(self) -> list[str]:
        return [
            "这是**现有实例迁移**，不是新装：请先按 "
            "docs/IMAGE-DEPLOYMENT-DESIGN.md §6 核对现场拓扑、备份与卷身份。",
            "已有部署的日常发布用 deploy/release/publish.py（按 digest 切换）；"
            "不要在保留数据的路径上重跑本安装命令。",
            "确需全新安装时，必须另选项目名/端口/空白卷（隔离实例），"
            "并确认不会与现有 ling_app-data / ling_db-data 同名。",
        ]

    def list_project_containers(self, *, all_states: bool) -> list[dict[str, str]]:
        argv = ["docker", "ps"]
        if all_states:
            argv.append("-a")
        argv += [
            "--filter",
            f"label=com.docker.compose.project={self.o.project}",
            "--format",
            "{{.ID}}|{{.Names}}|{{.State}}",
        ]
        result = self.runner.run(argv)
        if result.returncode != 0:
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "docker_ps",
                f"docker ps 失败：{tail(result.stderr or result.stdout)}",
            )
        containers: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            containers.append({"id": parts[0], "name": parts[1], "state": parts[2]})
        return containers

    def list_service_containers(
        self, service: str, *, all_states: bool, exit_code: int, status: str
    ) -> list[dict[str, str]]:
        argv = ["docker", "ps"]
        if all_states:
            argv.append("-a")
        argv += [
            "--filter",
            f"label=com.docker.compose.project={self.o.project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--format",
            "{{.ID}}|{{.Names}}|{{.State}}",
        ]
        result = self.runner.run(argv)
        if result.returncode != 0:
            raise LingError(
                exit_code,
                status,
                "docker_ps",
                f"docker ps 失败：{tail(result.stderr or result.stdout)}",
            )
        containers: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            containers.append({"id": parts[0], "name": parts[1], "state": parts[2]})
        return containers

    def docker_inspect(
        self, kind: str, ref: str, *, exit_code: int, status: str
    ) -> dict[str, Any]:
        result = self.runner.run(["docker", "inspect", "--type", kind, ref])
        if result.returncode != 0:
            raise LingError(
                exit_code,
                status,
                "docker_inspect",
                f"docker inspect {kind} {ref} 失败：{tail(result.stderr or result.stdout)}",
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise LingError(
                exit_code,
                status,
                "docker_inspect",
                f"docker inspect {kind} {ref} 输出不是 JSON：{exc}",
            ) from exc
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise LingError(
                exit_code, status, "docker_inspect", f"docker inspect {kind} {ref} 返回空"
            )
        return data[0]

    def docker_image_inspect(
        self, ref: str, *, exit_code: int, status: str
    ) -> dict[str, Any]:
        result = self.runner.run(["docker", "image", "inspect", ref])
        if result.returncode != 0:
            raise LingError(
                exit_code,
                status,
                "image_inspect",
                f"docker image inspect {ref} 失败：{tail(result.stderr or result.stdout)}",
            )
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise LingError(
                exit_code,
                status,
                "image_inspect",
                f"docker image inspect {ref} 输出不是 JSON：{exc}",
            ) from exc
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise LingError(
                exit_code, status, "image_inspect", f"docker image inspect {ref} 返回空"
            )
        return data[0]

    def print_plan(self) -> None:
        layout = self.layout
        containers = ", ".join(
            f"{self.o.project}-{service}-1" for service in ("db", "api", "worker")
        )
        volumes = ", ".join(
            f"{self.volume_names.get(key, f'{self.o.project}_{key}')}（{key}）"
            for key in ("db-data", "app-data")
        )
        self.section("将创建 / 使用")
        self.say(f"  安装目录    {layout.root}（新建 0700 目录；不含源码）")
        self.say(f"  备份目录    {self.o.backup_dir}")
        self.say(f"  Compose    项目 {self.o.project}；文件组 {layout.compose_file}")
        self.say(f"  容器        {containers}（Compose 命名，实际以 `ling status` 为准）")
        self.say(f"  数据卷      {volumes}")
        self.say(f"  监听        {self.o.host}:{self.o.port} -> 容器 6520/tcp")
        self.say(f"  镜像        {self.o.digest}（linux/{self.o.host_arch}）")
        self.say(f"  管理员      {self.o.admin_user}")
        self.say(f"  反向代理    {self.o.reverse_proxy}（Cookie Secure={'1' if self.o.reverse_proxy == 'https' else '0'}）")
        if self.o.test_mode:
            self.say("  测试模式    **已启用：不会调用真实模型**（仅适合界面/流程验收）")
        else:
            self.say("  测试模式    关闭（使用真实模型；未配置模型时会明确失败）")

        checks = self.state.get("checks") or {}
        docker = checks.get("docker") or {}
        registry = checks.get("registry") or {}
        disk = checks.get("disk") or {}
        self.section("预检结果")
        self.say(
            f"  Docker      {docker.get('server_version') or '?'}；"
            f"Compose {docker.get('compose_version') or '?'}（兼容）"
        )
        self.say(
            "  GHCR        "
            + (
                f"可达；平台匹配 linux/{self.o.host_arch}"
                if registry.get("reachable")
                else "未确认"
            )
        )
        self.say(f"  端口        {self.o.host}:{self.o.port} 空闲")
        disk_bits = []
        for name, entry in disk.items():
            if isinstance(entry, dict) and entry.get("free") is not None:
                disk_bits.append(f"{name}={entry['free']} B")
        self.say("  磁盘        " + ("；".join(disk_bits) if disk_bits else "未检查"))
        self.say("  已有实例    未发现同名项目/容器/卷（不会覆盖）")
        self.say("  已有 .env   无（安装器不会覆盖已有受限配置）")
        self.say("")
        self.say("  网页一键更新："
                 + (
                     "**将启用**（.env 写 LING_UPDATE_ENABLED=1，"
                     f"请求由 {self.updater_project} 执行）"
                     if self.o.enable_updates
                     else "将保持关闭（.env 显式写 LING_UPDATE_ENABLED=0；"
                     "要开加 --with-updater --enable-updates）"
                 ))
        if self.o.updater_digest is None:
            self.say(
                "  更新器      未配置（缺 --updater-digest）：网页更新不可用，本次不会拉起"
            )
        else:
            self.say(
                f"  更新器      固定摘要 {self.o.updater_digest}；"
                + (
                    f"本次将拉起独立项目 {self.updater_project}"
                    if self.o.with_updater
                    else "本次不拉起（未用 --with-updater）"
                )
            )
        self.say("")

    def confirm_plan(self) -> None:
        if not self.o.interactive:
            return
        if not _ask_bool("确认按上面的计划继续安装", False):
            raise Cancelled()

    def check_updater_request(self) -> None:
        """`--with-updater` 的前置条件（只读；不建目录、不启动任何东西）。

        缺更新器 compose 文件时**现在**就停：不能等应用装完、迁移做完才发现
        「更新器根本起不来」。`--updater-digest` / socket gid 的缺失已在参数解析
        阶段（退出码 2）拒绝。
        """
        if not self.o.with_updater:
            return
        compose = self.o.updater_compose_file
        if not compose.is_file():
            raise LingError(
                EXIT_PREFLIGHT,
                "failed_preflight",
                "updater_compose",
                (
                    f"更新器 Compose 文件不存在：{compose}；"
                    f"--with-updater 无法启动 {self.updater_project} 项目"
                ),
                [
                    "确认发行包带 deploy/updater/compose.yaml；"
                    "或去掉 --with-updater（只装应用，不拉起更新器）。",
                ],
            )
        code_files = updater_host_paths(self.o)["code_files"]
        missing = [name for name, present in code_files.items() if not present]
        if missing:
            self.log.emit(
                "注意：更新器代码目录缺 "
                + "、".join(missing)
                + f"（{self.o.updater_code_dir}）；更新器能启动，但发布/备份门禁会缺件"
            )
        # 冲突守卫：派生出的项目名**不能**已经属于另一个安装。同一宿主上的并存
        # 安装必须各有各的项目名，否则 Compose 会按本次 `--env-file` 重建对方的
        # 更新器容器、把挂载改成本安装目录（A 的网页更新静默操作 B 的数据）。
        self.check_updater_project_owner(exit_code=EXIT_PREFLIGHT)
        self.log.emit(
            f"预检：将显式拉起独立项目 {self.updater_project}（"
            f"uid/gid {self.o.updater_uid}:{self.o.updater_gid}，"
            f"socket gid {self.o.docker_gid}；网页更新开关 "
            f"{'1' if self.o.enable_updates else '0'}）"
        )

    def preflight(self) -> None:
        self.log.emit(
            "预检：只读检查（不创建安装目录/容器/卷；"
            "预检 env 与模型副本只写在 0700 临时目录）"
        )
        self.check_updater_request()
        self.check_paths()
        self.check_docker()
        # compose 模型要在端口预检**之前**解析：端口检查要逐个核对合并后模型里
        # **所有**服务的宿主发布端口（override 追加的 WS 端口也在里面）。
        if self.tmpdir is None:
            self.tmpdir = Path(tempfile.mkdtemp(prefix="ling-install-"))
            os.chmod(self.tmpdir, 0o700)
        # 预检临时 env **不能是空文件**：`${LING_PORT:-6520}` / `${LING_HOST:-...}`
        # 会落回模板默认值，模型与 --port/--host 指向的不是同一条拓扑（曾因此把
        # 6520 当成"要用的端口"，与同机另一个实例撞车）。这里写的就是随后进
        # `.env` 的同一批渲染期值，逐字符一致。
        preflight_env = self.tmpdir / "preinstall.env"
        preflight_env.write_text(render_preflight_env(self.o), encoding="utf-8")
        os.chmod(preflight_env, 0o600)
        values = preflight_env_values(self.o)
        self.log.emit(
            "预检 env（临时 0600）："
            + "、".join(f"{key}={value}" for key, value in values.items())
        )
        # 模型要解析**将要部署的那份副本**：宿主绑定 `${LING_HOST}` 只存在于
        # render_compose_copy 生成的副本里（源文件是 `"${LING_PORT:-6520}:6520"`，
        # 等于 0.0.0.0）。拿源文件解析得到的是另一套模型。副本写在 0700 临时
        # 目录里，不碰安装目录；`pull` 仍用经过校验的源文件。
        rendered = render_compose_copy(
            self.o.compose_source.read_text(encoding="utf-8"),
            self.o.compose_source,
            sha256_file(self.o.compose_source),
            self.o.project,
        )
        model_file = self.tmpdir / "preflight.compose.yaml"
        model_file.write_text(rendered, encoding="utf-8")
        os.chmod(model_file, 0o600)
        self.preflight_group = ComposeGroup(
            project=self.o.project,
            files=(self.o.compose_source,),
            env_file=preflight_env,
        )
        self.preflight_model_group = ComposeGroup(
            project=self.o.project,
            files=(model_file,),
            env_file=preflight_env,
        )
        self.config = self.compose_config_json(self.preflight_model_group, step="preflight")
        # "模型 == 命令"必须在**任何**端口检查之前钉死：拿错端口去查占用，报出来
        # 的是"别人的实例占了 6520"，而 --port 其实是干净的。
        self.check_preflight_model_ports(self.config)
        self.check_port()
        self.check_disk()
        self.check_registry()
        # 副本里带 `${LING_HOST}`，因此预检就能（也必须）核对宿主绑定；落盘之后
        # check_installed_compose_config 会用真实 .env 再核一遍。
        self.check_compose_model(self.config, require_host_bind=True)
        self.check_no_existing_instance()
        self.print_plan()
        self.confirm_plan()
        self.log.emit("预检通过")

    # ------------------------------------------------------------ 拉取
    def docker_env(self) -> dict[str, str]:
        """compose 子进程环境：安装器自己算出来的插值值优先于宿主 shell。

        `LING_APP_IMAGE` 从第一版就在这里；更新通道的三个值同样是 compose 的
        **必填**插值，预检阶段安装器还没写出 ``.env``——不注入它们，解析源
        compose 会直接失败（那正是"缺值即失败"的设计）。这里注入的就是随后写进
        ``.env`` 的同一批值，逐字符一致；``LING_HOST``/``LING_PORT`` 则**只**经
        ``--env-file``（预检临时 env / 落盘 ``.env``）进入模型，避免宿主的同名
        变量悄悄盖掉 ``--port``/``--host``——真出现了会由
        :meth:`check_preflight_model_ports` 明确拒绝。
        """
        env = os.environ.copy()
        env["LING_APP_IMAGE"] = self.o.digest
        env["LING_UPDATE_REQUESTS_HOST"] = str(self.layout.updater_requests_dir)
        env["LING_UPDATE_STATUS_HOST"] = str(self.layout.updater_state_dir)
        env["LING_UPDATE_SHARED_GID"] = str(self.o.updater_gid)
        return env

    def required_images(self) -> dict[str, str]:
        """从**预检已核对的渲染后模型**取出必须本地可用的镜像引用。

        api / worker 必须逐字符等于目标 digest 引用（compose 里两处 ``image:``
        相同；预检的 :meth:`check_compose_model` 已核过一次，这里再钉一次——
        这条判据直接决定"跳过拉取"是不是安全的，不能靠"看着像"）。

        db 的引用照模型原样取（默认 ``${LING_POSTGRES_IMAGE:-postgres:16-alpine}``
        解析结果）；它是可变 tag，没有 digest 可比对，只能按引用本身核对存在性。
        """
        services = self.config.get("services")
        if not isinstance(services, dict):
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "pull_refs",
                "预检模型里没有 services；无法确定要拉取哪些镜像。",
                ["确认 --compose-file 是镜像模式主文件，且预检的 compose config 成功。"],
            )
        refs: dict[str, str] = {}
        for name in PULL_SERVICES:
            service = services.get(name)
            image = service.get("image") if isinstance(service, dict) else None
            if not isinstance(image, str) or not image.strip():
                raise LingError(
                    EXIT_PULL,
                    "failed_pull",
                    f"pull_refs_{name}",
                    f"compose 模型里 {name} 没有可用的 image 引用；不能靠猜镜像名继续。",
                    ["检查 compose 的 image: 插值；缺值时 config 应当直接失败。"],
                )
            refs[name] = image.strip()
        if refs["api"] != self.o.digest or refs["worker"] != self.o.digest:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "pull_refs_app",
                (
                    "compose 模型里的 api/worker 镜像不是同一个目标 digest："
                    f"api={refs['api']!r} worker={refs['worker']!r} "
                    f"target={self.o.digest!r}；不能按“看着像同一个”跳过核验。"
                ),
                ["用 compose.image.yaml 让两处 image: 逐字符相同（同一个 LING_APP_IMAGE）。"],
            )
        return refs

    def image_present(self, ref: str) -> bool:
        """本地是否**真能按这个引用** inspect 到镜像（只认 docker 的回答）。

        这是"要不要拉"的唯一判据：不猜 tag 解析、不解析 ``docker images`` 的
        模糊输出、也不把某次 pull 的 rc 当成结论。应用镜像的 ref 本身就是目标
        digest 引用，因此这里成功 == 目标摘要的镜像确实在本地。
        """
        result = self.runner.run(["docker", "image", "inspect", ref])
        return result.returncode == 0

    def pull(self) -> None:
        """按需拉取：本地已能 inspect 到的不拉，缺哪个才拉哪个（逐个服务）。

        旧实现无条件 ``docker compose pull api worker db``：``compose pull`` 会
        去 registry 核对**每个**服务，哪怕镜像就在本地。Docker Hub 连不上时
        （国内 NAS / 受限网络），只因为 db 的 ``postgres:16-alpine`` 就整体
        rc=1，把一次本来可以完成的安装阻断——真机现场是
        ``Image ghcr.io/... Pulled`` 紧接着
        ``Image postgres:16-alpine Error Get "https://registry-1.docker.io/v2/"``。

        现在的判定：

        - 本地 ``docker image inspect <ref>`` 成功 → 跳过该服务（**不发 pull**）；
        - 失败 → 只拉这个服务；拉取报错时**按本地实况复核**：现在能 inspect 到
          就继续（拉取中途成功 / 本地本来就有），拿不到才失败；
        - 失败文案点名**哪个镜像、哪个 registry**，并给可执行的下一步；
        - 进入下一步之前统一复核：三个镜像都必须能按**确切引用** inspect 到。
          应用镜像（api/worker）的 ref 就是目标 digest，且随后
          :meth:`verify_image_identity` 还要求 RepoDigests 对得上目标摘要——
          "跳过拉取"没有放宽任何一条身份门禁。
        """
        assert self.preflight_group is not None
        refs = self.required_images()
        self.log.emit(
            "核对所需镜像（本地已能 inspect 到的不再拉取；此步不影响任何在线服务）"
        )
        decisions: dict[str, str] = {}
        ensured: set[str] = set()
        for service in PULL_SERVICES:
            ref = refs[service]
            if self.image_present(ref):
                if ref in ensured:
                    first = next(
                        name
                        for name in PULL_SERVICES
                        if name != service and refs[name] == ref
                    )
                    decisions[service] = "same_ref"
                    self.log.emit(
                        f"跳过拉取 {service}：与 {first} 是同一个镜像引用（{ref}）"
                    )
                else:
                    decisions[service] = "local"
                    self.log.emit(f"跳过拉取 {service}：本地已能 inspect 到 {ref}")
                ensured.add(ref)
                continue
            self.log.emit(f"拉取 {service}：{ref}")
            result = self.runner.run(
                [*self.preflight_group.argv(), "pull", service],
                env=self.docker_env(),
                timeout=DEFAULT_MIGRATE_TIMEOUT,
            )
            if result.returncode != 0:
                # 拉取报错 ≠ 镜像没到：compose pull 可能已经写完镜像才报错
                # （同一批里别的层/标签失败）。只按**现在能不能 inspect 到**
                # 判定——既不忽略失败，也不冤枉一份已经就位的本地镜像。
                if self.image_present(ref):
                    decisions[service] = "recovered"
                    self.log.emit(
                        f"拉取 {service} 报错（rc={result.returncode}），但本地已能 "
                        f"inspect 到 {ref}；按本地实况继续"
                    )
                    ensured.add(ref)
                    continue
                raise LingError(
                    EXIT_PULL,
                    "failed_pull",
                    f"pull_{service}",
                    self.pull_failure_message(service, ref, result),
                    self.pull_failure_actions(service, ref),
                )
            decisions[service] = "pulled"
            ensured.add(ref)
            self.log.emit(f"已拉取 {service}：{ref}")
        self.verify_required_images(refs, decisions)
        image = self.docker_image_inspect(
            self.o.digest, exit_code=EXIT_PULL, status="failed_pull"
        )
        self.target_image = image
        self.verify_image_identity(image)
        self.extract_target_head()
        self.log.emit(
            f"镜像身份核验通过：id={image.get('Id')} commit={image_commit(image)} "
            f"head={self.target_head}"
        )

    def verify_required_images(
        self, refs: dict[str, str], decisions: dict[str, str]
    ) -> None:
        """统一复核：进入下一步之前，**所有**需要的镜像都必须能按确切引用 inspect 到。

        单独一刀而不是只信"刚拉过没拉过"：pull 的 rc、缓存、并发与 Compose 自己
        的判定都可能与本地实况不一致，只有 docker 的 inspect 是事实。db 的
        image ID 也在这里打进日志——tag 可变，事后核对"跑的到底是哪一份库镜像"
        只能靠这条记录。
        """
        for service in PULL_SERVICES:
            ref = refs[service]
            if not self.image_present(ref):
                raise LingError(
                    EXIT_PULL,
                    "failed_pull",
                    f"image_missing_{service}",
                    self.missing_image_message(service, ref),
                    self.pull_failure_actions(service, ref),
                )
        pulled = [name for name in PULL_SERVICES if decisions.get(name) == "pulled"]
        local = [
            name for name in PULL_SERVICES if decisions.get(name) in ("local", "same_ref")
        ]
        recovered = [
            name for name in PULL_SERVICES if decisions.get(name) == "recovered"
        ]
        parts: list[str] = []
        if pulled:
            parts.append(
                "已拉取 " + "、".join(f"{name}（{refs[name]}）" for name in pulled)
            )
        if local:
            parts.append(
                "跳过拉取 " + "、".join(f"{name}（{refs[name]}）" for name in local)
            )
        if recovered:
            parts.append(
                "拉取报错但本地可用 "
                + "、".join(f"{name}（{refs[name]}）" for name in recovered)
            )
        if not pulled and not recovered:
            parts.append("本次未发起任何 pull")
        self.log.emit("镜像结论：" + "；".join(parts))
        db_image = self.docker_image_inspect(
            refs["db"], exit_code=EXIT_PULL, status="failed_pull"
        )
        self.log.emit(
            "db 镜像已就位："
            f"{refs['db']}（id={db_image.get('Id')}，"
            f"RepoDigests={[str(item) for item in db_image.get('RepoDigests') or []]}；"
            "可变 tag，本地这份内容就是将要运行的那份）"
        )

    def pull_failure_message(self, service: str, ref: str, result: Result) -> str:
        kind = "应用镜像" if service in APP_SERVICES else "数据库镜像"
        return (
            f"{kind} {ref} 无法就位（服务 {service}；来源 registry "
            f"{image_registry(ref)}）：docker compose pull {service} 失败"
            f"（rc={result.returncode}）：{tail(result.stderr or result.stdout)}"
        )

    def missing_image_message(self, service: str, ref: str) -> str:
        kind = "应用镜像" if service in APP_SERVICES else "数据库镜像"
        return (
            f"复核失败：{kind} {ref}（服务 {service}；来源 registry "
            f"{image_registry(ref)}）不在本地，也无法按确切引用 inspect 到；"
            "不会带着缺失的镜像进入部署。"
        )

    def pull_failure_actions(self, service: str, ref: str) -> list[str]:
        registry = image_registry(ref)
        if service in APP_SERVICES:
            actions = [
                f"确认宿主能连到 {registry} 且凭据可用（私有包先 docker login "
                f"{registry}），或在可达网络里 `docker pull {ref}` 预加载后重跑。",
                "镜像加速/中转仓库必须给出**同一个 digest**：安装器要求能按这个"
                "确切摘要 inspect 到，换成同名 tag 不算数。",
            ]
        else:
            actions = [
                f"给 Docker 配镜像加速后重跑（daemon.json 的 registry-mirrors 指向"
                f"可达的 Docker Hub 镜像站），或在可达网络里 `docker pull {ref}`"
                "预加载到本机。",
                "或把 LING_POSTGRES_IMAGE（compose 的 db.image）换成可达仓库的同版本"
                "镜像后重跑，例如内网镜像站上的 postgres:16-alpine。",
            ]
        actions.append(self.no_changes_statement())
        return actions

    def no_changes_statement(self) -> str:
        """失败时**如实**说明"到底改了什么"——"未做任何变更"在 pull 阶段已经不准。

        pull 发生在抢锁之后：安装目录与安装锁文件**已经**创建。说清楚它们，
        并说明重跑会续用，否则用户会怀疑要手工清目录。
        """
        if self.created.get("root"):
            root = f"安装目录 {self.layout.root}（本次新建，0700）"
        else:
            root = f"安装目录 {self.layout.root}（已存在，本次只校正为 0700）"
        created = [root]
        if self.lock is not None:
            created.append(
                f"安装锁文件 {self.lock.path}（flock 随本进程释放，文件按约定保留）"
            )
        return (
            "已创建：" + "、".join(created) + "；"
            "尚未创建容器/卷，也未写 .env、compose 副本与安装状态；"
            "修复后**直接重跑**同一条命令即可续用这个目录与同一把锁"
            "（安装器只在 .env 已存在时拒绝覆盖，本次还没有 .env）。"
        )

    def verify_image_identity(self, image: dict[str, Any]) -> None:
        arch = image.get("Architecture")
        os_name = image.get("Os")
        if os_name != "linux" or arch != self.o.host_arch:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "image_platform",
                (
                    f"目标镜像平台是 {os_name}/{arch}，宿主是 linux/{self.o.host_arch}；"
                    "平台不匹配。"
                ),
                ["确认拉取的是对应平台；拒绝运行错误架构的镜像。"],
            )
        env_value = image_env(image).get("LING_COMMIT", "")
        label_commit = image_labels(image).get("org.opencontainers.image.revision", "")
        if not env_value or not label_commit:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "image_commit",
                (
                    "目标镜像缺少 LING_COMMIT 或 OCI 标注 "
                    f"org.opencontainers.image.revision（env={env_value!r} "
                    f"label={label_commit!r}）；不能证明部署的是哪个源提交。"
                ),
                ["由 CI 用完整源 SHA 构建并注入两者；不要部署无身份的镜像。"],
            )
        if env_value != label_commit:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "image_commit",
                (
                    f"镜像内 LING_COMMIT={env_value!r} 与 OCI 标注 revision="
                    f"{label_commit!r} 不一致。"
                ),
            )
        if COMMIT_RE.fullmatch(label_commit) is None:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "image_commit",
                f"镜像标注的提交不是 40 位小写 hex：{label_commit!r}",
            )
        if self.o.source_commit and label_commit != self.o.source_commit:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "image_commit",
                (
                    f"镜像标注提交 {label_commit!r} ≠ --source-commit "
                    f"{self.o.source_commit!r}；摘要与期望源提交不符。"
                ),
                ["核对发布证据；不要用错误的 digest 安装。"],
            )
        self.verify_repo_digests(image)
        self.state["image"].update(
            {
                "image_id": str(image.get("Id")),
                "repo_digests": [str(item) for item in image.get("RepoDigests") or []],
                "architecture": arch,
                "os": os_name,
                "source_commit": label_commit,
                "source_commit_source": "operator+image" if self.o.source_commit else "image",
                "version": image_labels(image).get("org.opencontainers.image.version"),
            }
        )

    def verify_repo_digests(self, image: dict[str, Any]) -> None:
        digests = [str(item) for item in image.get("RepoDigests") or []]
        suffixes = {
            item.rsplit("@sha256:", 1)[1]
            for item in digests
            if "@sha256:" in item
        }
        accepted = {self.o.digest_hex}
        if self.platform_digest:
            accepted.add(self.platform_digest)
        if not suffixes & accepted:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "image_digest",
                (
                    "本地镜像的 RepoDigests 与目标摘要对不上："
                    f"期望 {sorted(accepted)}，实际 {digests}。"
                ),
                ["确认 pull 的是同一 digest；不要用同名 tag 或缓存镜像冒充。"],
            )

    def extract_target_head(self) -> None:
        result = self.runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "-e",
                "LING_PROJECT_ROOT=/app",
                "--entrypoint",
                "python",
                self.o.digest,
                "-c",
                ALEMBIC_HEAD_SCRIPT,
            ],
            timeout=DEFAULT_COMMAND_TIMEOUT,
        )
        if result.returncode != 0:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "target_head",
                (
                    "无法从目标镜像读取 Alembic head（缺少 alembic/migrations "
                    f"或运行失败）：{tail(result.stderr or result.stdout)}"
                ),
                ["不要跳过 head 比对；确认 CI 构建的镜像包含 migrations 与 alembic.ini。"],
            )
        heads, revisions, parents = parse_alembic_script_output(result.stdout)
        if len(heads) != 1:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "target_head",
                f"目标镜像的 Alembic head 不唯一或为空：{heads!r}",
                ["多 head 需要先合并迁移；不要带着歧义安装。"],
            )
        if not revisions:
            raise LingError(
                EXIT_PULL,
                "failed_pull",
                "target_head",
                "目标镜像没有可读取的 Alembic 修订；无法核对迁移链",
            )
        self.target_head = heads[0]
        self.target_revisions = revisions
        self.target_parents = parents
        self.state["migration"]["target_head"] = self.target_head
        self.state["database"]["target_head"] = self.target_head

    # ------------------------------------------------------------ 布局
    def ensure_bind_sources(self) -> None:
        """预建/核对三个 Docker bind mount 源的类型（缺了会被 Docker 建成目录）。

        - ``<根>/.env``：普通文件（compose 的 ``--env-file`` + 更新器单文件挂载）
        - ``<根>/.ling-install.lock``：普通文件（flock 要 ``O_RDWR`` 打开）
        - ``<根>/compose``：目录（更新器只读挂载整个 compose 文件组）

        缺失就按正确类型预建（``compose/`` 在这里建；``.env`` 与锁由随后的
        :meth:`write_file` / :meth:`ensure_install_lock` 创建），类型不对就**现在**
        失败并给出 ``rmdir`` 之类的修复指引——不静默继续到 compose/flock 才炸。
        """
        layout = self.layout
        for path, want in (
            (layout.env_file, "file"),
            (layout.install_lock, "file"),
            (layout.compose_dir, "dir"),
        ):
            if path.is_symlink():
                raise bind_source_error(path, want, symlink=True)
            if not path.exists():
                continue
            right = path.is_file() if want == "file" else path.is_dir()
            if not right:
                raise bind_source_error(path, want)
        if not layout.compose_dir.is_dir():
            layout.compose_dir.mkdir(mode=0o700)
            self.created["compose_dir"] = True
            self.log.emit(f"预建 compose 文件组目录：{layout.compose_dir}（0700）")

    def verify_bind_sources(self) -> None:
        """写盘后再核对一次：三者都在、类型正确，且 compose 文件组确实有主文件。"""
        layout = self.layout
        for path, want in (
            (layout.env_file, "file"),
            (layout.install_lock, "file"),
            (layout.compose_dir, "dir"),
            (layout.compose_file, "file"),
        ):
            if path.is_symlink():
                raise bind_source_error(path, want, symlink=True)
            right = path.is_file() if want == "file" else path.is_dir()
            if not right:
                raise bind_source_error(path, want)

    def create_layout(self) -> None:
        layout = self.layout
        self.log.emit(f"写受限目录与配置：{layout.root}")
        if not layout.root.exists():
            layout.root.mkdir(mode=0o700, parents=True)
            self.created["root"] = True
        os.chmod(layout.root, 0o700)
        # Docker 的 bind mount 在源缺失时会**自动建目录**；安装器必须先建对类型，
        # 否则用户拿到的是一棵"看起来装了、实际跑不起来"的目录树。
        self.ensure_bind_sources()
        if not layout.logs_dir.exists():
            layout.logs_dir.mkdir(mode=0o700)
            self.created["logs_dir"] = True
        self.log.attach(layout.logs_dir / f"install-{local_stamp()}.log")

        source_text = self.o.compose_source.read_text(encoding="utf-8")
        source_sha = sha256_file(self.o.compose_source)
        if self.source_sha256 is not None and source_sha != self.source_sha256:
            raise LingError(
                EXIT_LAYOUT,
                "failed_layout",
                "compose_source",
                (
                    "Compose 主文件在预检与写盘之间被改动"
                    f"（预检 sha256 {self.source_sha256} → 现在 {source_sha}）；"
                    "拒绝按未核对的配置安装。"
                ),
                ["确认发布包没有被并发替换；重新运行安装。"],
            )
        rendered = render_compose_copy(
            source_text, self.o.compose_source, source_sha, self.o.project
        )
        self.write_file(layout.compose_file, rendered.encode("utf-8"), 0o600)

        self.secret_values = generate_secrets()
        env_text = render_env(self.o, self.secret_values)
        self.write_file(layout.env_file, env_text.encode("utf-8"), 0o600)
        # 更新器要把这两个文件**单文件**挂进容器：`.env` 已写，锁文件必须现在
        # 就存在（Docker 的 bind mount 不会替你创建缺失的源文件）。锁本身就是
        # 安装器正在持有的那把（flock 在 inode 上），这里只校正权限、不换 inode。
        lock_info = self.ensure_install_lock(layout.install_lock)
        self.verify_bind_sources()

        self.install_group = ComposeGroup(
            project=self.o.project,
            files=(layout.compose_file,),
            env_file=layout.env_file,
        )
        files = [str(layout.compose_file)]
        self.state["layout"]["compose"].update(
            {
                "files": files,
                "source_sha256": source_sha,
                "sha256": sha256_file(layout.compose_file),
                "files_group_sha256": sha256_bytes("\n".join(files).encode("utf-8")),
            }
        )
        self.state["layout"]["compose"]["env_file_sha256"] = sha256_file(layout.env_file)
        self.state["layout"].update(
            {
                "install_lock": str(layout.install_lock),
                "install_lock_mode": lock_info["mode"],
                "install_lock_gid": lock_info["gid"],
                "install_lock_share": lock_info["reason"],
            }
        )
        # 更新器一侧的宿主目录、更新器 env 与属主：都在这一步完成（应用仍是最小
        # 可信面；更新器只是独立项目，不等它启动才准备）。
        self.create_updater_layout()
        self.layout_created = True
        self.state["status"] = "layout_created"
        self.write_state()
        self.log.emit(
            f"受限配置已写入（0600）：{layout.env_file}；compose 文件组：{layout.compose_file}；"
            f"跨工具安装锁：{layout.install_lock}（{lock_info['mode']}；"
            f"{lock_info['reason']}）"
        )

    # ------------------------------------------------------------ 更新器布局
    def create_updater_layout(self) -> None:
        """创建更新器目录与 env，并把权限位设成更新通道方案（见常量注释）。

        目录就建在安装根里：

        - ``<dir>/updater``：0700（只给更新器；API 不需要进这个父目录——
           bind mount 由宿主机按路径挂，容器里看到的是被挂目录本身）；
        - ``<dir>/updater/requests``：**2770**（属主=更新器 uid、属组=共享组，
          setgid 让 API 写出的请求文件继承共享组）；
        - ``<dir>/updater/requests/archive``：**2750**（只有更新器写；API 只
          stat 名字判重，缺了它 API 会 fail closed 把所有新请求判成"已用过"）；
        - ``<dir>/updater/state``：**0750**（更新器写 status.json，API 只读）；
        - ``<dir>/updater/.env``：0600（compose 的 ``--env-file``）。

        属主/属组由 :meth:`prepare_updater_ownership` 对齐；这里只负责 mode 与
        创建——两个门禁分开，"换个属主"和"改个权限位"不会互相掩护。
        """
        layout = self.layout
        plan = self.state["layout"]["updater"]["update_channel"]
        for path, flag, mode in (
            (layout.updater_dir, "updater_dir", 0o700),
            (layout.updater_requests_dir, "updater_requests_dir", UPDATE_REQUESTS_MODE),
            (layout.updater_archive_dir, "updater_archive_dir", UPDATE_ARCHIVE_MODE),
            (layout.updater_state_dir, "updater_state_dir", UPDATE_STATUS_MODE),
        ):
            if path.is_symlink():
                raise LingError(
                    EXIT_LAYOUT,
                    "failed_layout",
                    "updater_permissions",
                    f"更新器目录不能是符号链接：{path}",
                    ["移走该链接后重跑；安装器只在自己创建的目录上 chmod/chown。"],
                )
            if not path.is_dir():
                path.mkdir(mode=mode)
                self.created[flag] = True
            os.chmod(path, mode)
            info = path.stat()
            if stat.S_IMODE(info.st_mode) != mode:
                raise LingError(
                    EXIT_LAYOUT,
                    "failed_layout",
                    "updater_permissions",
                    f"更新器目录权限不是 {mode:04o}：{path}"
                    f"（mode={stat.S_IMODE(info.st_mode):04o}）",
                )
            plan["stat"][path.name] = {
                "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                "uid": info.st_uid,
                "gid": info.st_gid,
            }
        env_text = render_updater_env(self.o)
        self.write_file(layout.updater_env_file, env_text.encode("utf-8"), 0o600)
        self.state["updater"]["env_file_sha256"] = sha256_file(layout.updater_env_file)
        self.prepare_updater_ownership()
        self.log.emit(
            f"更新器布局已准备：{layout.updater_dir}（0700）；"
            f"requests {plan['modes']['requests']}（含 archive {plan['modes']['archive']}）、"
            f"state {plan['modes']['status']}，共享组 {plan['shared_gid']}；"
            f"更新器 env：{layout.updater_env_file}（0600）"
        )

    def prepare_updater_ownership(self) -> None:
        """把更新器要读写的宿主路径 chown 给它运行时的 uid/gid（失败如实记录）。

        - **只换属主/属组，不放宽权限位**：0700/2770/0750/0600 由
          :meth:`create_updater_layout` 与 ``write_file`` 负责；
        - 属组就是更新通道的**共享组**（更新器 gid）：API 容器用 compose 的
          ``group_add`` 加入同一个 gid，于是 requests 的组写位、state 的组读位
          对它生效。共享组选更新器 gid 而不是 API 的 10001，是为了让普通用户
          安装也能设上（非 root 只能 chown 到自己所属的组）；
        - **安装锁不在这里 chown**：它的属主必须留在安装者（宿主的 install.sh /
          publish.py / ling 都用属主位），跨 uid 的更新器靠
          :meth:`ensure_install_lock` 设的「共享组 + 0660」访问。把它 chown 给
          更新器反而会让宿主的非 root 工具失去属主位；
        - **compose 文件组（目录 + 里面的文件）单独核算**：更新器要以另一个 uid
          读 ``/srv/ling/compose/compose.image.yaml`` 跑 ``docker compose
          config``，只 chown 目录不够（文件是 0600、属主是安装者 ⇒ EACCES）。
          见 :meth:`compose_access_for_updater`；
        - 备份目录已存在才 chown（不改它的 mode）；``--with-updater`` 且目录缺失时
          才创建（父目录必须已在，避免在没挂载的挂载点上静默落到根盘）；
        - 非 root 安装器 + 显式 10002 会 EPERM：普通安装记下来、应用照常；
          ``--with-updater`` 直接失败（不启动一个读不到 0600 ``.env`` 的更新器）。
        """
        layout = self.layout
        uid, gid = self.o.updater_uid, self.o.updater_gid
        applied: list[str] = []
        unchanged: list[str] = []
        errors: list[dict[str, str]] = []

        # 只换属主；权限位由前面的 mkdir/chmod/write_file/ensure_install_lock 负责，
        # 这里再 chmod 一遍既冗余、又会让「权限门禁」出现两个可分别改坏的副本。
        targets: list[tuple[str, Path]] = [
            ("dir", layout.updater_dir),
            ("dir", layout.updater_requests_dir),
            ("dir", layout.updater_archive_dir),
            ("dir", layout.updater_state_dir),
            ("file", layout.updater_env_file),
            ("file", layout.env_file),
        ]
        backup_dir = self.o.backup_dir
        if not backup_dir.is_dir() and self.o.with_updater:
            if not backup_dir.parent.is_dir():
                raise LingError(
                    EXIT_LAYOUT,
                    "failed_layout",
                    "updater_backup_dir",
                    (
                        f"--with-updater 需要备份根目录可写，但它的父目录不存在："
                        f"{backup_dir.parent}"
                    ),
                    ["先挂载/创建备份盘与目录，再重跑；安装器不替你在根盘上建目录。"],
                )
            backup_dir.mkdir(mode=0o700)
            self.created["backup_dir"] = True
        if backup_dir.is_dir():
            # 不改 mode：备份目录可能是用户按自己的策略建的（0750/0770 都常见）。
            targets.append(("dir", backup_dir))

        for kind, path in targets:
            try:
                info = path.stat()
                if (info.st_uid, info.st_gid) == (uid, gid):
                    unchanged.append(str(path))
                    continue
                os.chown(path, uid, gid)
                applied.append(str(path))
            except OSError as exc:
                errors.append({"path": str(path), "kind": kind, "error": str(exc)})

        compose = self.compose_access_for_updater()
        errors.extend(compose["errors"])

        self.state["updater"]["ownership"] = {
            "uid": uid,
            "gid": gid,
            "source": self.o.updater_user_source,
            "applied": applied,
            "unchanged": unchanged,
            "compose": compose,
            "errors": errors,
        }
        if errors:
            detail = "；".join(f"{item['path']}（{item['error']}）" for item in errors[:4])
            if self.o.with_updater:
                raise LingError(
                    EXIT_LAYOUT,
                    "failed_layout",
                    "updater_ownership",
                    (
                        "无法把更新器要读写的宿主路径 chown 到 "
                        f"{uid}:{gid}：{detail}。拒绝启动一个读不到 0600 .env、"
                        "写不了 state/requests 的更新器。"
                    ),
                    [
                        "以 root 重跑安装（会把属主设为 10002:10002），或"
                        "导出 LING_UPDATER_UID/LING_UPDATER_GID 为当前安装用户后重跑；"
                        "不要用 chmod 0777 或 privileged 绕过。",
                    ],
                )
            self.log.emit(
                f"注意：更新器宿主路径未能全部 chown 到 {uid}:{gid}（{detail}）；"
                "应用安装不受影响，但更新器要等属主修正后才能跑"
            )
        else:
            self.log.emit(
                f"更新器宿主路径属主：{uid}:{gid}（来源 {self.o.updater_user_source}；"
                f"新改 {len(applied)} 个、原本已匹配 {len(unchanged)} 个）"
            )

    def compose_access_for_updater(self) -> dict[str, Any]:
        """让**另一个 uid** 的更新器读得到 compose 目录与里面的文件。

        缺陷现场：``prepare_updater_ownership`` 只 chown 了 ``compose/`` 目录，
        而 ``compose.image.yaml`` 是 ``write_file(..., 0o600)`` 写出的、属主是
        安装者；更新器（默认 10002）在容器里读它跑 ``docker compose config``
        ⇒ EACCES。真实 ``ling install --with-updater`` 会装出一个跑不起来的更新器。

        两条路线，**都不碰世界权限位**（永不 0777/0775，不新增 world 位）：

        - **属主路线**（安装者是 root，或更新器 uid 就是安装者）：把目录与文件
          chown 给更新器 ``uid:gid``；0700/0600 的属主位已经够读，权限位一个都
          不改——与 ``.env`` 等其它 file target 同一口径；
        - **共享组路线**（非 root 安装器 + 另一个 uid，chown uid 必然 EPERM）：
          只把**属组**换成共享组（= 更新器 gid），并只补上组位里缺的"进入/读"
          （目录 0750、文件 0640）。属主仍是安装者，宿主工具照样能读；更新器按
          组读。这与安装锁的「共享组 + 0660」是同一条路线，也是非 root 安装
          唯一能成立的方案（非 root 只允许 chgrp 到自己所属的组）。

        失败与其它 target 同语义：记进 ``errors``；``--with-updater`` 时由调用方
        失败（拒绝启动一个读不到 compose 的更新器）。
        """
        layout = self.layout
        uid, gid = self.o.updater_uid, self.o.updater_gid
        euid = os.geteuid() if hasattr(os, "geteuid") else 0
        owner_route = euid == 0 or uid == euid

        targets: list[tuple[str, Path]] = [("dir", layout.compose_dir)]
        applied: list[str] = []
        unchanged: list[str] = []
        errors: list[dict[str, str]] = []
        stats: dict[str, dict[str, Any]] = {}
        try:
            entries = sorted(layout.compose_dir.iterdir())
        except OSError as exc:
            # 目录读不了（例如被并发移走/无权限）：记为错误，别让安装器在这里
            # 抛未捕获的 OSError；--with-updater 会按既有语义失败。
            entries = []
            errors.append({"path": str(layout.compose_dir), "kind": "dir", "error": str(exc)})
        targets.extend(
            ("file", path) for path in entries if path.is_file() and not path.is_symlink()
        )
        if layout.compose_file not in [path for _kind, path in targets]:
            # 目录为空说明前面的写盘步骤没做完；这里仍把主文件列进来，让错误
            # 指向真正要读的那个文件，而不是只报一个"目录没权限"。
            targets.append(("file", layout.compose_file))

        for kind, path in targets:
            wanted = 0o050 if kind == "dir" else 0o040
            try:
                info = path.stat()
                if owner_route:
                    if (info.st_uid, info.st_gid) != (uid, gid):
                        os.chown(path, uid, gid)
                        applied.append(str(path))
                    else:
                        unchanged.append(str(path))
                else:
                    if info.st_gid != gid:
                        os.chown(path, -1, gid)
                        applied.append(str(path))
                    else:
                        unchanged.append(str(path))
                    mode = stat.S_IMODE(info.st_mode)
                    if mode & wanted != wanted:
                        # 只加"属组可进入/可读"，不碰属主位、不碰世界位。
                        os.chmod(path, mode | wanted)
                info = path.stat()
                stats[str(path)] = {
                    "mode": f"{stat.S_IMODE(info.st_mode):04o}",
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                }
            except OSError as exc:
                errors.append({"path": str(path), "kind": kind, "error": str(exc)})

        return {
            "route": "owner" if owner_route else "shared_group",
            "uid": uid,
            "gid": gid,
            "applied": applied,
            "unchanged": unchanged,
            "stat": stats,
            "errors": errors,
        }

    def install_lock_group(self) -> tuple[int | None, str]:
        """安装锁要不要与共享组共享：返回 ``(gid 或 None, 原因)``。

        只有在**更新器以另一个 uid 运行**、且安装者真能把文件设到那个组时才共享：

        - 同一 uid（普通用户安装、未配更新器）：属主位已经够用，保持 0600；
        - 更新器是 root(0)：拒绝把锁共享给 root 组，保持 0600；
        - 非 root 且不属于共享组：``chgrp`` 会 EPERM，保持 0600 并如实说明
          （否则安装在一半失败，比现在更难查）。

        共享组的取值与 compose 的 ``LING_UPDATE_SHARED_GID`` 完全一致
        （``options.updater_gid``，root 安装默认 10002，显式 env 时用给的值）。
        """
        gid = self.o.updater_gid
        euid = os.geteuid() if hasattr(os, "geteuid") else 0
        if self.o.updater_uid == euid:
            return None, "更新器与安装者同一 uid，属主权限已足够"
        if gid == 0:
            return None, "共享组是 root(0)，拒绝把安装锁共享给 root 组"
        groups = set(os.getgroups()) if hasattr(os, "getgroups") else set()
        if hasattr(os, "getegid"):
            groups.add(os.getegid())
        if euid == 0 or gid in groups:
            return gid, f"更新器是另一个 uid，安装锁与共享组 {gid} 共享"
        return None, (
            f"安装者不属于共享组 {gid} 且不是 root，无法 chgrp；"
            "锁保持 0600（更新器仍打不开它：以 root 重跑安装，或把安装者加入该组）"
        )

    def ensure_install_lock(self, path: Path) -> dict[str, Any]:
        """预建/校正安装锁（默认 0600；与共享组共享时 0660 且属组=共享组）。

        **不能用** ``write_atomic``：rename 会换 inode，本进程已经持有的 flock
        会从路径上被摘掉（另一个进程立刻还能在同一路径上再拿一把锁）。所以这里
        只打开**同一个** inode（必要时创建）并校正权限位，不写内容、不改名。

        为什么需要 0660：更新器在**另一个 uid** 下运行，只能靠组位访问；flock
        用 ``O_RDWR`` 打开，0600（连组读都没有）会让它直接 ``EACCES``——更新器把
        ``EACCES`` 归类成 ``install_lock_busy``，看着像"别人持锁"，实际没人持锁。
        属主始终是安装者（宿主 ``install.sh`` / ``publish.py`` / ``ling`` 都用属主位）。
        """
        gid, reason = self.install_lock_group()
        mode = INSTALL_LOCK_MODE_SHARED if gid is not None else INSTALL_LOCK_MODE
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, mode)
        except OSError as exc:
            raise LingError(
                EXIT_LAYOUT,
                "failed_layout",
                "install_lock",
                (
                    f"无法预建安装锁 {path}：{exc}；"
                    "更新器要把这个文件单文件挂进容器，缺了更新器起不来。"
                ),
                ["检查安装目录权限/挂载选项（不能在只读文件系统上安装）。"],
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise LingError(
                    EXIT_LAYOUT,
                    "failed_layout",
                    "install_lock",
                    f"安装锁必须是普通文件（不能是符号链接/目录）：{path}",
                    [
                        f"确认它是空目录：ls -la {path}",
                        f"空目录就删掉：rmdir {path}",
                        "然后重新运行 ling install。",
                    ],
                )
            if gid is not None:
                try:
                    os.fchown(fd, -1, gid)
                except OSError as exc:
                    # 拿不到共享组就**回落**到安装者独占，并如实说明原因。
                    gid, mode = None, INSTALL_LOCK_MODE
                    reason = f"无法把安装锁属组改成 {self.o.updater_gid}（{exc}），回落 0600"
            if stat.S_IMODE(os.fstat(fd).st_mode) != mode:
                os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        info = path.stat()
        actual = stat.S_IMODE(info.st_mode)
        if actual != mode:
            raise LingError(
                EXIT_LAYOUT,
                "failed_layout",
                "install_lock",
                f"安装锁权限不是 {mode:04o}：{path}（mode={actual:04o}）",
            )
        if gid is not None and info.st_gid != gid:
            raise LingError(
                EXIT_LAYOUT,
                "failed_layout",
                "install_lock",
                f"安装锁属组不是共享组 {gid}：{path}（gid={info.st_gid}）",
            )
        return {"mode": f"{mode:04o}", "gid": gid, "reason": reason}

    def write_file(self, path: Path, data: bytes, mode: int) -> None:
        write_atomic(path, data, mode)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise LingError(
                EXIT_LAYOUT,
                "failed_layout",
                "permissions",
                f"写入后的 {path} 权限不符合要求（mode={stat.S_IMODE(info.st_mode):04o}）",
            )
    def write_state(self) -> None:
        self.state["updated_at"] = utc_now()
        write_state_file(self.layout.state_file, self.state)

    def migration_attempted(self) -> bool:
        """迁移**真的开跑过**没有？这是 ``after_migration`` 的唯一判据。

        真机缺陷：安装器在迁移前读版本就失败了，失败记录却写着
        ``after_migration: true`` / ``status: failed_after_migration``，
        把排障引向"schema 已改、要人工回滚"的错误方向。事实来源只有
        ``state.migration.attempted``（在真正执行迁移命令前才置 True）。
        """
        return bool((self.state.get("migration") or {}).get("attempted"))

    def check_installed_compose_config(self) -> None:
        assert self.install_group is not None
        self.run_compose(
            ["config", "-q"],
            exit_code=EXIT_LAYOUT,
            status="failed_layout",
            step="compose_config_q",
            message="docker compose config -q 失败（持久化的受限 env 解析不出来）",
            next_actions=["检查 .env 的插值与权限；不要用 shell 变量覆盖绕过。"],
        )
        config = self.compose_config_json(self.install_group, step="installed")
        self.check_compose_model(config, require_host_bind=True)
        env_real = Path(os.path.realpath(self.layout.env_file))
        if stat.S_IMODE(env_real.stat().st_mode) & 0o077:
            raise LingError(
                EXIT_LAYOUT,
                "failed_layout",
                "permissions",
                f"受限 env 权限过宽：{env_real}",
            )
        self.state["status"] = "compose_verified"
        self.write_state()
        self.log.emit(
            f"compose 解析通过：项目 {self.o.project}，宿主绑定 {self.o.host}:{self.o.port}，"
            f"卷 {', '.join(self.volume_names.values())}"
        )

    # ------------------------------------------------------------ 运行
    def run_compose(
        self,
        args: Sequence[str],
        *,
        exit_code: int,
        status: str,
        step: str,
        message: str,
        next_actions: Sequence[str] = (),
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        after_migration: bool = False,
    ) -> Result:
        assert self.install_group is not None
        result = self.runner.run(
            [*self.install_group.argv(), *args],
            env=self.docker_env(),
            timeout=timeout,
        )
        if result.returncode != 0:
            raise LingError(
                exit_code,
                status,
                step,
                f"{message}（rc={result.returncode}）"
                + (f"：{tail(result.stderr or result.stdout)}" if (result.stderr or result.stdout).strip() else ""),
                next_actions,
                after_migration=after_migration,
            )
        return result

    def start_db(self) -> None:
        self.container_started = True
        self.log.emit("启动 db（--no-deps --no-build --pull=never）")
        self.run_compose(
            ["up", "-d", "--no-deps", "--no-build", "--pull=never", "db"],
            exit_code=EXIT_POST_MIGRATION,
            status="failed_db_ready",
            step="up_db",
            message="启动 db 容器失败",
            next_actions=["检查 db 配置/端口/卷；未迁移，不需要回滚 schema。"],
        )
        self.state["status"] = "database_started"
        self.write_state()
        self.wait_service_healthy(
            "db",
            timeout=self.o.db_timeout,
            status="failed_db_ready",
            after_migration=False,
        )
        self.log.emit("db healthy（healthcheck；随后还要过真实 psql 就绪探针）")
        # 就绪探针与随后的 alembic_version 读取共用**同一个** ``--db-timeout``
        # 窗口（不是各开一个窗口），总等待上界因此仍是 db-timeout。
        self.db_deadline = time.monotonic() + self.o.db_timeout
        self.ensure_db_ready()

    def wait_service_healthy(
        self,
        service: str,
        *,
        timeout: float,
        status: str = "failed_after_migration",
        after_migration: bool = True,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last = "unknown"
        while time.monotonic() < deadline:
            containers = self.list_service_containers(
                service,
                all_states=False,
                exit_code=EXIT_POST_MIGRATION,
                status=status,
            )
            if len(containers) == 1:
                info = self.docker_inspect(
                    "container",
                    containers[0]["id"],
                    exit_code=EXIT_POST_MIGRATION,
                    status=status,
                )
                health = ((info.get("State") or {}).get("Health") or {}).get("Status")
                if health is None:
                    raise LingError(
                        EXIT_POST_MIGRATION,
                        status,
                        f"{service}_health_missing",
                        (
                            f"{service} 没有可读的 healthcheck 状态（State.Health 缺失）；"
                            "不能把“进程起来”当成“服务可用”。"
                        ),
                        ["核对 compose 的 healthcheck 是否被 disable/覆盖，以及容器实况。"],
                        after_migration=after_migration,
                    )
                last = str(health)
                if health == "healthy":
                    return info
                if health == "unhealthy":
                    raise LingError(
                        EXIT_POST_MIGRATION,
                        status,
                        f"{service}_health",
                        f"{service} 的 healthcheck 是 unhealthy",
                        [f"查看 {service} 日志与依赖（数据库/网络/凭据）。"],
                        after_migration=after_migration,
                    )
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        raise LingError(
            EXIT_POST_MIGRATION,
            status,
            f"{service}_health",
            f"{service} 在 {timeout}s 内未变为 healthy（最后 {last}）",
            [f"查看 {service} 日志；不要跳过健康门禁。"],
            after_migration=after_migration,
        )

    # ------------------------------------------------------------ db 就绪 / 读版本
    def psql_once(self, sql: str) -> tuple[int, str]:
        """在 db 容器里跑一次只读 ``psql``；返回 (rc, 合并后的错误文本)。"""
        assert self.install_group is not None
        result = self.runner.run(
            [
                *self.install_group.argv(),
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "ling",
                "-d",
                "ling",
                "-tAc",
                sql,
            ],
            env=self.docker_env(),
        )
        return result.returncode, (result.stderr + "\n" + result.stdout)

    def db_query_with_retry(
        self,
        sql: str,
        *,
        step: str,
        strict: bool,
        deadline: float | None = None,
    ) -> tuple[int, str]:
        """在 ``--db-timeout`` 窗口内**有界重试**一次 db 查询，直到真正就绪。

        为什么需要它（真机缺陷）：全新空卷首装时 postgres entrypoint 是两段式
        启动，compose 的 ``pg_isready`` healthcheck 在**临时服务器**阶段就报
        healthy，而紧接着的 ``psql`` 会撞在"临时服务器正在关闭"的窗口里
        （``FATAL: the database system is shutting down``），安装直接失败并留下
        半成品。这里把**明确属于"还没就绪"**的瞬时错误当可重试。

        门禁没有放宽：
        - 只重试 ``db_error_kind(...) == "retry"`` 的那几种（shutting down /
          starting up / 库还不存在 / 连接被拒）；
        - ``password authentication failed`` 之类**立刻失败**，不重试；
        - 其它未知错误也**不重试**（不做"什么错都重试"的盲目放行）；
        - 窗口用完仍失败时，错误信息区分"超时内始终没就绪"与"就绪了但读不出"。

        ``deadline`` 缺省时新开一个 ``--db-timeout`` 窗口；安装路径上就绪探针与
        迁移前的版本读取共用 ``self.db_deadline``（同一个窗口）。
        """
        if deadline is None:
            deadline = time.monotonic() + self.o.db_timeout
        attempts = 0
        history: list[str] = []
        last_text = ""
        while True:
            attempts += 1
            rc, text = self.psql_once(sql)
            if rc == 0:
                if attempts > 1:
                    self.log.emit(
                        f"db 已就绪：{step} 第 {attempts} 次尝试成功"
                        f"（前 {attempts - 1} 次是“还没就绪”的瞬时错误："
                        + "；".join(history) + "）"
                    )
                return rc, text
            kind = db_error_kind(text)
            detail = tail(text).replace("\n", " ")
            history.append(f"#{attempts} {detail}")
            last_text = text
            if kind == "blank":
                # 库在、alembic_version 表还没有 ⇒ **db 已就绪**，这是空白实例。
                # 不重试、不算失败：由调用方（迁移阶段）决定要不要建表。
                self.log.emit(f"{step}：db 已就绪，alembic_version 表还不存在（空白实例）")
                return rc, text
            if kind == "fatal":
                # 配置错误：重试只会拖延排障。立刻失败，并如实说明**没有**重试。
                raise LingError(
                    EXIT_MIGRATE if strict else EXIT_POST_MIGRATION,
                    "failed_migration" if strict else "failed_db_ready",
                    step,
                    (
                        "db 凭据/角色/权限配置错误（这类错误**不重试**，"
                        f"共尝试 {attempts} 次）：{tail(text)}"
                    ),
                    [
                        "核对 .env 的 POSTGRES_PASSWORD 与 db 容器实际口令一致、"
                        "POSTGRES_USER=ling 存在；这是配置问题，不是等待能解决的。",
                    ],
                    after_migration=strict,
                )
            if kind != "retry":
                # 未知错误：不重试（宁可立刻暴露，也不做盲目放行）。
                raise LingError(
                    EXIT_MIGRATE if strict else EXIT_POST_MIGRATION,
                    "failed_migration" if strict else "failed_db_ready",
                    step,
                    (
                        "db 查询失败（非“还没就绪”的瞬时错误，**不重试**，"
                        f"共尝试 {attempts} 次）：{tail(text)}"
                    ),
                    ["确认 db 容器健康、项目名与 compose 文件组正确；不猜 schema 版本。"],
                    after_migration=strict,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.log.emit(
                f"db 还没就绪（{step} 第 {attempts} 次：{detail}）；"
                f"重试（窗口还剩 {remaining:.1f}s / 共 {self.o.db_timeout:g}s）"
            )
            time.sleep(min(DB_READY_POLL_INTERVAL, max(0.05, remaining)))

        # 窗口用完：区分"超时内始终没就绪"与"就绪了但读不出"。
        # 这里最后一条仍是 retry 类错误 ⇒ 始终没就绪。
        assert history, "重试窗口内至少尝试过一次"
        last_detail = tail(last_text).replace("\n", " ")
        # 区分两种失败：**始终没就绪** vs **就绪了但读不出**（后者说明 db 探针
        # 已过、问题在本次查询本身，排障方向完全不同）。
        if self.db_ready:
            message = (
                "db 已就绪（select 1 就绪探针通过），但读取 "
                f"`{sql}` 在 {self.o.db_timeout:g}s 超时内仍**读不出**"
                f"（共尝试 {attempts} 次，每次都是“还没就绪”的瞬时错误；"
                f"最后一次：{last_detail}）"
            )
        else:
            message = (
                f"db 在 {self.o.db_timeout:g}s 超时内**始终没有就绪**（{step}："
                f"`{sql}`）"
                f"（共尝试 {attempts} 次，每次都是“还没就绪”的瞬时错误；"
                f"最后一次：{last_detail}）"
            )
        raise LingError(
            EXIT_MIGRATE if strict else EXIT_POST_MIGRATION,
            "failed_migration" if strict else "failed_db_ready",
            step,
            message,
            [
                "db 可能仍在 postgres entrypoint 的两段式初始化里，或容器根本没起来："
                "看 `docker logs`（应出现 `PostgreSQL init process complete; "
                "ready for start up.`）与容器实况；不要因为 pg_isready=healthy 就跳过。",
            ],
            after_migration=strict,
        )

    def ensure_db_ready(self) -> None:
        """真实 ``psql`` 就绪探针（不再只信 ``pg_isready``）。

        全新空卷首装时 ``pg_isready -U ling -d ling`` 会在 entrypoint 的**临时
        服务器**阶段返回 healthy（pg_isready 只探"服务器接受连接"，不看库是否
        存在、也不看是不是即将关闭的临时服务器）。这里用 ``select 1`` 作为真正
        的判据，并纳入**同一个** ``--db-timeout`` 重试循环。
        """
        rc, _text = self.db_query_with_retry(
            DB_READY_PROBE_SQL,
            step="db_ready",
            strict=False,
            deadline=self.db_deadline,
        )
        assert rc == 0
        self.db_ready = True
        self.log.emit("db 就绪探针通过（psql -U ling -d ling -tAc 'select 1'）")

    def read_db_version(self, *, strict: bool) -> str | None:
        """读 ``alembic_version``；同样在 ``--db-timeout`` 窗口内重试"还没就绪"。

        ``strict`` 只在迁移**之后**的核验里为 True：那时 db 一定已经就绪，
        再出现"还没就绪"就是真故障，按 ``failed_migration`` 如实失败；
        迁移前（``strict=False``）的读版本属于"db 就绪阶段"，失败一律记
        ``failed_db_ready`` —— 迁移根本没跑过，绝不能标成 after_migration。
        """
        rc, text = self.db_query_with_retry(
            "select version_num from alembic_version",
            step="alembic_version",
            strict=strict,
            deadline=None if strict else self.db_deadline,
        )
        if rc != 0:
            if not strict and db_error_kind(text) == "blank":
                # 库在、alembic_version 表还没建：空白实例，交给迁移阶段建表。
                return None
            raise LingError(
                EXIT_MIGRATE if strict else EXIT_POST_MIGRATION,
                "failed_migration" if strict else "failed_db_ready",
                "alembic_version",
                f"无法读取 alembic_version：{tail(text)}",
                ["确认 db 容器健康、项目名与 compose 文件组正确；不猜 schema 版本。"],
                after_migration=strict,
            )
        # 成功时 psql 只在 stdout 打值；stderr 里若有 psql: 前缀的告警不算值。
        values = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.startswith("psql:")
        ]
        return values[-1] if values else None

    def migrate_once(self) -> None:
        current = self.read_db_version(strict=False)
        self.state["migration"]["current_before"] = current
        if current is not None:
            self.container_started = True
            raise LingError(
                EXIT_MIGRATE,
                "failed_migration",
                "existing_database",
                (
                    f"数据库里已有 alembic_version={current!r}：这不是空白实例。"
                    "安装器不会在已有 schema 上做“空白安装”。"
                ),
                self.instance_migration_actions(),
            )
        if self.target_head is None:
            raise LingError(
                EXIT_MIGRATE, "failed_migration", "target_head", "目标 Alembic head 未知；拒绝迁移"
            )
        self.log.emit(f"单次显式迁移：空 → {self.target_head}")
        self.state["migration"]["attempted"] = True
        self.container_started = True
        assert self.install_group is not None
        result = self.runner.run(
            [
                *self.install_group.argv(),
                "run",
                "--rm",
                "--no-deps",
                "-e",
                "LING_RUN_MIGRATIONS=1",
                "api",
                "migrate",
            ],
            env=self.docker_env(),
            timeout=self.o.migrate_timeout,
        )
        combined = (result.stdout + "\n" + result.stderr).strip()
        log_path = self.layout.logs_dir / f"migrate-{local_stamp()}.log"
        write_atomic(log_path, (combined + "\n").encode("utf-8"), 0o600)
        self.state["migration"].update(
            {
                "exit_code": result.returncode,
                "log": str(log_path),
                "executed_marker": "执行迁移" in combined,
                "skip_marker": "跳过迁移" in combined,
            }
        )
        if result.returncode != 0:
            raise LingError(
                EXIT_MIGRATE,
                "failed_migration",
                "migrate",
                (
                    f"迁移命令失败（rc={result.returncode}）；日志 {log_path}；"
                    f"{tail(combined)}"
                ),
                [
                    "迁移可能已部分执行：不要自动重建容器或恢复 dump；"
                    "按设计 §6 人工判断（日志与 schema 实况）。",
                ],
                after_migration=True,
            )
        if self.state["migration"]["skip_marker"]:
            raise LingError(
                EXIT_MIGRATE,
                "failed_migration",
                "migrate_skipped",
                "迁移日志显示“跳过迁移”（LING_RUN_MIGRATIONS=0 生效），这不是一次真实迁移。",
                ["检查 run 的 -e LING_RUN_MIGRATIONS=1 是否被 compose 覆盖；不要启动 api。"],
                after_migration=True,
            )
        if not self.state["migration"]["executed_marker"]:
            raise LingError(
                EXIT_MIGRATE,
                "failed_migration",
                "migrate_not_executed",
                f"迁移日志没有“执行迁移”标记；日志 {log_path}：{tail(combined)}",
                ["无法证明迁移实际执行；不要启动 api。"],
                after_migration=True,
            )
        after = self.read_db_version(strict=True)
        self.state["migration"]["current_after"] = after
        if after != self.target_head:
            raise LingError(
                EXIT_MIGRATE,
                "failed_migration",
                "migrate_head",
                (
                    f"迁移后 alembic_version={after!r} ≠ 目标镜像 head={self.target_head!r}；"
                    "拒绝启动 api。"
                ),
                ["保留日志与数据库，人工核对 schema；不要自动恢复 dump。"],
                after_migration=True,
            )
        self.state["database"]["alembic_version"] = after
        self.state["status"] = "migrated"
        self.write_state()
        self.log.emit(f"迁移核验通过：alembic_version={after}")

    def start_api_worker(self) -> None:
        self.log.emit("启动 api（--no-deps --no-build --pull=never）")
        self.run_compose(
            ["up", "-d", "--no-deps", "--no-build", "--pull=never", "api"],
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            step="up_api",
            message="启动 api 容器失败",
            next_actions=["已迁移：不要自动重建/恢复；人工检查 api 日志与端口。"],
            after_migration=True,
        )
        self.state["status"] = "api_started"
        self.write_state()

    def verify(self) -> None:
        assert self.published_port is not None
        ready_status = self.wait_readyz()
        self.state.setdefault("result", {})["readyz_http"] = ready_status
        version = self.verify_running_commit()
        self.state["result"]["version"] = version
        api_state = self.verify_container_identity("api")
        self.state["containers"]["api"] = api_state
        self.log.emit("api 验证通过；启动 worker")
        self.run_compose(
            ["up", "-d", "--no-deps", "--no-build", "--pull=never", "worker"],
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            step="up_worker",
            message="启动 worker 容器失败",
            next_actions=["worker 未启动：保持现状并人工检查；不要自动回滚。"],
            after_migration=True,
        )
        worker_state = self.verify_container_identity("worker")
        self.state["containers"]["worker"] = worker_state
        self.wait_service_healthy("worker", timeout=self.o.worker_timeout)
        db_state = self.verify_container_identity("db", image_optional=True)
        self.state["containers"]["db"] = db_state
        final_head = self.read_db_version(strict=True)
        self.state["database"]["alembic_version"] = final_head
        if final_head != self.target_head:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "head_after_start",
                (
                    f"api 启动后 alembic_version={final_head!r} ≠ 目标 head="
                    f"{self.target_head!r}；可能有隐式迁移或外部写入。"
                ),
                ["不要以 Compose 退出 0 为成功；人工核对 schema 与写者。"],
                after_migration=True,
            )
        if api_state["image_id"] != worker_state["image_id"]:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "same_digest",
                (
                    f"api/worker 运行镜像 ID 不同：api={api_state['image_id']} "
                    f"worker={worker_state['image_id']}"
                ),
                ["不要以 Compose 退出 0 为成功；人工核对两个容器的镜像。"],
                after_migration=True,
            )
        self.verify_volumes()
        self.state["status"] = "verified"
        self.write_state()

    # ------------------------------------------------------------ 更新器启动
    @property
    def updater_project(self) -> str:
        """本次安装**实际**使用的更新器项目名（派生，状态里记的也是它）。

        拉起、日志、错误提示与摘要都读这一处，避免"某处又派生一次、名字对不上"。
        """
        return updater_project_of(self.state)

    def updater_compose_group(self) -> ComposeGroup:
        return ComposeGroup(
            project=self.updater_project,
            files=(self.o.updater_compose_file,),
            env_file=self.layout.updater_env_file,
        )

    def check_updater_project_owner(
        self, *, exit_code: int, after_migration: bool = False
    ) -> None:
        """拉起前核对：派生出的项目名**不能**已经属于另一个安装。

        同一宿主上第二个 ``--with-updater`` 安装若复用同一个项目名，Compose 会
        按不同的 ``--env-file`` **重建**第一个实例的更新器容器、把挂载改成本次
        安装的路径——实例 A 的网页更新通道于是静默开始操作实例 B 的数据。所以
        只要项目里已有不属于本安装目录的容器，就在这里拒绝（预检一次、拉起前
        再核对一次，关掉"预检之后别人刚起了同名项目"的窗口）。
        """
        project = self.updater_project
        foreign = updater_project_owner_elsewhere(
            self.runner, project, self.layout.root
        )
        if not foreign:
            return
        owners = "、".join(f"{item['name']}（{item['owner']}）" for item in foreign[:3])
        raise LingError(
            exit_code,
            "failed_after_migration" if after_migration else "failed_preflight",
            "updater_project_conflict",
            (
                f"更新器项目 {project} 已属于另一个安装（路径 {foreign[0]['owner']}）："
                f"{owners}；本安装是 {self.layout.root}。"
                "同名项目会让 Compose 按本次 --env-file 重建对方容器、"
                "把挂载改成这个安装目录——对方实例的网页更新会开始操作本次的数据。"
            ),
            [
                f"先确认这个项目属于哪个实例："
                f"docker ps -a --filter label=com.docker.compose.project={project}",
                "若那是已废弃的实例，从它的安装目录核对后执行 "
                f"`docker compose -p {project} down --remove-orphans`，再重跑本次安装。",
                "若两个实例都要保留，给本次安装换一个 --project"
                "（例如 ling-verify），更新器项目名会跟着变成 ling-verify-updater。",
            ],
            after_migration=after_migration,
        )

    def updater_docker_env(self) -> dict[str, str]:
        """拉起更新器时的子进程环境：与写进更新器 env 的值**逐个一致**。

        compose 的插值优先级是「进程环境 > ``--env-file``」，而安装器的入参本身
        常来自宿主同名环境变量（``LING_INSTALL_DIR`` 等）。先把这些键从宿主环境
        里摘掉，再填安装器解析后的值，避免「宿主 shell 里另一套路径/摘要」悄悄
        覆盖掉刚落盘的配置。``DOCKER_HOST`` 等 docker CLI 自己的变量保留。
        """
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("LING_UPDATER_", "COMPOSE_")) or key in (
                "LING_INSTALL_DIR",
                "LING_BACKUP_DIR",
                "LING_DOCKER_SOCKET",
                "LING_DOCKER_GID",
            ):
                env.pop(key, None)
        env.update(
            {
                "LING_INSTALL_DIR": str(self.layout.root),
                "LING_BACKUP_DIR": str(self.o.backup_dir),
                "LING_DOCKER_SOCKET": str(self.o.docker_socket),
                "LING_UPDATER_REQUESTS_DIR": str(self.layout.updater_requests_dir),
                "LING_UPDATER_STATE_DIR": str(self.layout.updater_state_dir),
                "LING_UPDATER_CODE_DIR": str(self.o.updater_code_dir),
                "LING_UPDATER_UID": str(self.o.updater_uid),
                "LING_UPDATER_GID": str(self.o.updater_gid),
            }
        )
        if self.o.updater_digest:
            env["LING_UPDATER_IMAGE"] = self.o.updater_digest
        if self.o.docker_gid is not None:
            env["LING_DOCKER_GID"] = str(self.o.docker_gid)
        return env

    def start_updater(self) -> None:
        """`--with-updater` 时**显式**拉起独立项目 `<应用项目名>-updater`；否则什么都不做。

        默认不拉起（设计 §4/§7）。拉起更新器 ≠ 打开网页更新：开关是独立的
        ``update.enabled``（由 ``--enable-updates`` 决定）。更新器启动失败是
        真实的安装失败（应用已迁移，退出码 16），不静默吞掉。
        """
        if not self.o.with_updater:
            self.log.emit(
                "未使用 --with-updater：不拉起更新器（网页更新开关 "
                f"{'1' if self.o.enable_updates else '0'}）"
            )
            return
        assert self.o.updater_digest is not None
        assert self.o.docker_gid is not None
        if self.o.docker_gid == 0:
            self.log.emit(
                "注意：宿主 docker socket 的属组是 root(0)；更新器仍以非 root uid 运行，"
                "但补充组 0 的权限较宽，建议给 dockerd 换专用组后重装（--docker-gid）"
            )
        if not self.o.updater_compose_file.is_file():
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "updater_compose",
                f"更新器 Compose 文件不存在：{self.o.updater_compose_file}",
                ["去掉 --with-updater 或补齐发行包；应用本身已就绪。"],
                after_migration=True,
            )
        # 拉起前再核对一次归属：预检之后别人可能刚起了同名项目（并发窗口）。
        self.check_updater_project_owner(
            exit_code=EXIT_POST_MIGRATION, after_migration=True
        )
        group = self.updater_compose_group()
        argv = [*group.argv(), "up", "-d"]
        self.log.emit(
            f"拉起更新器项目 {self.updater_project}（{self.o.updater_digest}；"
            f"uid/gid {self.o.updater_uid}:{self.o.updater_gid}；"
            f"socket gid {self.o.docker_gid}）"
        )
        result = self.runner.run(
            argv, env=self.updater_docker_env(), timeout=DEFAULT_MIGRATE_TIMEOUT
        )
        if result.returncode != 0:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "updater_start",
                (
                    f"docker compose -p {self.updater_project} up -d 失败"
                    f"（rc={result.returncode}）：{tail(result.stderr or result.stdout)}"
                ),
                [
                    "应用本身已迁移并验证通过；先看更新器日志与 `ling doctor`，"
                    "不要因此重跑整个安装。",
                    f"手工核对：{self.layout.updater_env_file}（0600）与宿主目录属主。",
                ],
                after_migration=True,
            )
        self.updater_started = True
        self.state["updater"].update(
            {
                "started": True,
                "started_at": utc_now(),
                "compose_argv": argv,
                "reason": "更新器已按固定摘要拉起；网页更新开关见 update.enabled",
            }
        )
        self.state["status"] = "updater_started"
        self.write_state()
        self.log.emit(
            f"更新器项目 {self.updater_project} 已启动（网页更新开关 "
            f"{'1' if self.o.enable_updates else '0'}；"
            "是否在跑以 `ling doctor` 的实况为准）"
        )

    def base_url(self) -> str:
        host = self.o.host
        if host == "0.0.0.0":
            # 所有网卡：从本机用回环地址探测。
            host = "127.0.0.1"
        return f"http://{host}:{self.published_port}"

    def display_url(self) -> str:
        host = self.o.host
        if host == "0.0.0.0":
            return f"http://<本机IP>:{self.o.port}"
        return f"http://{host}:{self.o.port}"

    def wait_readyz(self) -> int:
        base = self.base_url()
        url = f"{base}/readyz"
        deadline = time.monotonic() + self.o.ready_timeout
        last = "未尝试"
        while time.monotonic() < deadline:
            code, _body = http_probe(base, "/readyz", timeout=5.0)
            last = str(code)
            if code == 200:
                self.log.emit(f"宿主 /readyz 200（{url}）")
                return code
            if 300 <= code < 400:
                raise LingError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "readyz_redirect",
                    (
                        f"宿主 {url} 返回 {code}（重定向）；设计要求 /readyz 恰好 200，"
                        "不接受 3xx。"
                    ),
                    [
                        "已迁移：不要自动回滚；检查端口/反向代理是否指到 api，"
                        "以及 /readyz 是否被改写成重定向。",
                    ],
                    after_migration=True,
                )
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        raise LingError(
            EXIT_POST_MIGRATION,
            "failed_after_migration",
            "readyz",
            f"宿主 {url} 在 {self.o.ready_timeout}s 内没有返回 200（最后 {last}）",
            ["已迁移：查看 api 日志、端口与健康检查；不要自动重建。"],
            after_migration=True,
        )

    def verify_running_commit(self) -> str | None:
        """运行容器内**进程证据**：应用自报的版本/提交必须与镜像标注一致。

        为什么不再打 HTTP ``/v1/version``（旧实现就是因此永远失败）：
        ``GET /v1/version`` 挂 ``CurrentAuth``，未鉴权恒 401。旧实现把它的
        非 200 当 ``failed_after_migration / version``，于是**真机上
        ``ling install`` 的最后一步必然失败**，而单元测试的 HTTP stub 对它
        无条件回 200，所以一直是绿的（P5 抓到的同一类缺陷）。

        为什么**不给探活加 token**：安装/状态/诊断一旦持有用户凭据，能改宿主
        状态的通道就多了一个凭据持有者，攻击面只增不减。这里改成在容器里问
        应用自己（``CONTAINER_VERSION_PROBE``），零凭据、零 HTTP。

        证据等价性：``wait_readyz``（宿主端口，恰好 200）已经证明"HTTP 栈
        可用"；``verify_container_identity`` 按 OCI 标注复核"运行容器是目标
        镜像"；``exec`` 补上最后一块"跑着的那个进程就是目标构建"。三者合起来
        与"HTTP 拿到 commit"等价，而且比它更强（HTTP 那层还要靠客户端与凭据）。

        没选那条路（新增一个只回 version/commit 的免鉴权端点）：它与"只读端点
        一律鉴权"的既有决定冲突。``/readyz`` 能免鉴权只是因为容器 healthcheck
        必须探它，而且它已经降级成只回状态；为安装流程再开一个公开端点，等于
        让匿名调用方读到部署指纹（版本/提交＝精确的补丁级情报）。
        """
        result = self.run_compose(
            [
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                CONTAINER_VERSION_PROBE,
            ],
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
            step="version",
            message="容器内读取应用自报的版本/提交失败（exec 失败）",
            next_actions=[
                "已迁移：不要自动回滚；确认运行容器就是目标摘要，并检查容器日志与镜像内 Python。",
            ],
            timeout=CONTAINER_VERSION_EXEC_TIMEOUT,
            after_migration=True,
        )
        probe = parse_container_version_probe(result.stdout)
        if not probe:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "version",
                f"容器内探针没有打印可解析的 JSON：{tail(result.stdout) or '（空输出）'}",
                ["已迁移：不要自动回滚；确认镜像内 `ling` 可 import、LING_COMMIT 已注入。"],
                after_migration=True,
            )
        version = probe.get("version")
        commit = probe.get("commit")
        expected = self.state["image"].get("source_commit")
        if isinstance(commit, str) and expected and commit != expected:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                "version",
                (
                    f"容器内进程报告的提交 {commit!r} ≠ 镜像标注源提交 {expected!r}"
                    f"（应用自报版本 {version!r}）"
                ),
                ["运行容器可能不是目标镜像；不要按成功处理。"],
                after_migration=True,
            )
        if not isinstance(commit, str) or not commit:
            self.log.emit("注意：容器内进程报告的 commit 为空（镜像未注入 LING_COMMIT？）")
        return str(version) if version is not None else None

    def verify_container_identity(
        self, service: str, *, image_optional: bool = False
    ) -> dict[str, Any]:
        containers = self.list_service_containers(
            service, all_states=False, exit_code=EXIT_POST_MIGRATION, status="failed_after_migration"
        )
        if len(containers) != 1:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                f"{service} 正在运行的容器数量是 {len(containers)}，期望 1",
                ["人工检查 Compose 副本数/残留容器；不要自动重建。"],
                after_migration=True,
            )
        info = self.docker_inspect(
            "container",
            containers[0]["id"],
            exit_code=EXIT_POST_MIGRATION,
            status="failed_after_migration",
        )
        state = info.get("State") or {}
        if state.get("Running") is not True:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                f"{service} 容器 {containers[0]['name']} 不在 running 状态",
                after_migration=True,
            )
        image_id = info.get("Image")
        image_ref = (info.get("Config") or {}).get("Image")
        if not image_id:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                f"无法读取 {service} 容器的 image ID",
                after_migration=True,
            )
        record: dict[str, Any] = {
            "container_id": containers[0]["id"],
            "container_name": containers[0]["name"],
            "image_id": str(image_id),
            "image_ref": str(image_ref) if image_ref else None,
            "health": ((state.get("Health") or {}).get("Status")),
        }
        if image_optional:
            return record
        if image_ref != self.o.digest:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                f"{service} 容器镜像引用是 {image_ref!r}，不是目标 digest {self.o.digest!r}",
                after_migration=True,
            )
        expected_id = self.state["image"].get("image_id")
        if expected_id and image_id != expected_id:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                f"{service} 容器 image ID={image_id!r} ≠ 拉取到的目标 ID={expected_id!r}",
                after_migration=True,
            )
        image = self.docker_image_inspect(
            str(image_id), exit_code=EXIT_POST_MIGRATION, status="failed_after_migration"
        )
        try:
            self.verify_repo_digests(image)
        except LingError as exc:
            raise LingError(
                EXIT_POST_MIGRATION,
                "failed_after_migration",
                f"verify_{service}",
                exc.message,
                after_migration=True,
            ) from exc
        record["repo_digests"] = [str(item) for item in image.get("RepoDigests") or []]
        record["commit"] = image_commit(image)
        return record

    def verify_volumes(self) -> None:
        mountpoints: list[Path] = []
        for key, name in self.volume_names.items():
            info = self.docker_inspect(
                "volume", name, exit_code=EXIT_POST_MIGRATION, status="failed_after_migration"
            )
            labels = info.get("Labels") or {}
            project = labels.get("com.docker.compose.project")
            if info.get("Driver") != "local":
                raise LingError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "volumes",
                    f"卷 {name} 的 driver 是 {info.get('Driver')!r}，期望 local",
                    after_migration=True,
                )
            if project != self.o.project:
                raise LingError(
                    EXIT_POST_MIGRATION,
                    "failed_after_migration",
                    "volumes",
                    (
                        f"卷 {name} 的 Compose 项目标签是 {project!r}，"
                        f"不是 {self.o.project!r}；卷身份不一致。"
                    ),
                    after_migration=True,
                )
            mountpoint = info.get("Mountpoint")
            if isinstance(mountpoint, str) and mountpoint:
                mountpoints.append(Path(mountpoint))
            self.state["volumes"][key] = {
                "name": str(info.get("Name") or name),
                "driver": info.get("Driver"),
                "compose_name": f"{self.o.project}_{key}",
                "project_label": project,
                "created_at": info.get("CreatedAt"),
            }
        self._volume_mountpoints = mountpoints

    # ------------------------------------------------------------ 收尾
    def finish(self) -> None:
        self.state["containers"].setdefault("db", {})
        backup = backup_status(
            self.o.backup_dir, getattr(self, "_volume_mountpoints", [])
        )
        self.state["backup"].update(
            {
                "latest": backup.get("latest"),
                "restore_point_ready": bool(backup.get("restore_point_ready")),
                "requirements": backup.get("requirements") or {},
                "free_bytes": backup.get("free_bytes"),
                "reason": backup.get("reason"),
            }
        )
        self.state["update"] = {
            "enabled": bool(self.o.enable_updates),
            "protocol": UPDATER_PROTOCOL,
            "reason": update_reason_for(self.o),
        }
        # 更新器：只记录「配置了什么、起没起」；`update.enabled` 记的是开关本身。
        self.state["updater"]["started"] = self.updater_started
        if not self.updater_started and self.state["updater"].get("started_at") is None:
            self.state["updater"]["reason"] = (
                "更新器镜像未配置（缺 --updater-digest / LING_UPDATER_IMAGE）：网页更新不可用"
                if self.o.updater_digest is None
                else "更新器镜像已记录，但本次安装未拉起（未用 --with-updater）"
            )
        self.state["status"] = "complete"
        self.state["finished_at"] = utc_now()
        self.state["exit_code"] = EXIT_OK
        self.write_state()
        self.print_summary()

    def print_summary(self) -> None:
        image = self.state["image"]
        result = self.state.get("result") or {}
        backup = self.state["backup"]
        url = self.display_url()
        self.section("ling 安装摘要")
        self.say(f"  对外地址    {url}")
        self.say(
            "  版本        "
            + (f"{result.get('version') or image.get('version') or '未知'}"
               f"（提交 {image.get('source_commit') or '未知'}）")
        )
        self.say(f"  镜像        {self.o.digest}")
        self.say(f"              运行 image ID {image.get('image_id')}")
        self.say(
            f"  管理员      {self.o.admin_user}（首次登录后请立即修改口令）"
        )
        self.say(
            "  服务        api/worker 同一摘要；/readyz 200；"
            f"schema={self.state['database'].get('alembic_version')}"
        )
        self.say(f"  配置        {self.layout.env_file}（0600）")
        self.say(f"  安装状态    {self.layout.state_file}（0600）")
        self.say(
            f"  更新器目录  {self.layout.updater_dir}（0700；requests/state 0700；"
            "更新器 env 0600）"
        )
        latest = backup.get("latest")
        if latest:
            self.say(
                f"  备份        {self.o.backup_dir}；最近 {latest.get('created_at') or '未知'}"
            )
        else:
            self.say(f"  备份        {self.o.backup_dir}；尚无恢复点")
        if self.o.test_mode:
            self.say("  测试模式    **已启用：不会调用真实模型**")
        self.say("")
        if self.o.enable_updates:
            self.say(
                "  一键更新    网页更新已启用（LING_UPDATE_ENABLED=1）："
                "更新请求由 "
                f"{self.updater_project} 项目执行；是否在跑以 `ling doctor` 为准。"
            )
        else:
            self.say(
                "  一键更新    网页更新未启用（LING_UPDATE_ENABLED=0，"
                "已显式写进受限 .env）。"
            )
            self.say("              打开方法（本机重装时）：")
            self.say(
                f"                  ling install --with-updater "
                f"--updater-digest {UPDATER_REPOSITORY}@sha256:<64 位 hex> "
                "--enable-updates"
            )
            self.say(
                "              或在现有部署上确认 ① 更新器镜像已配置且 "
                f"{self.updater_project} 在跑 ② <dir>/updater/requests "
                "里已有发布条目 ③ 有完整恢复点"
                "（`ling backup` + `ling restore-drill`）后，"
            )
            self.say(
                f"              把 {self.layout.env_file} 的 LING_UPDATE_ENABLED 改成 1"
                "，再 `docker compose ... up -d api`。"
            )
            self.say("              " + UPDATE_NOT_IMPLEMENTED_NOTE + "。")
        if self.o.updater_digest is None:
            self.say(
                "  更新器      未配置（缺 --updater-digest / LING_UPDATER_IMAGE）："
                "没有执行者，网页更新不可用；安装器不会声称它可用。"
            )
        elif self.updater_started:
            self.say(
                f"  更新器      已按固定摘要拉起（项目 {self.updater_project}）："
                f"{self.o.updater_digest}"
                f"（协议 {UPDATER_PROTOCOL}；是否在跑以 `ling doctor` 为准）。"
            )
        else:
            self.say(
                f"  更新器      固定摘要已记录：{self.o.updater_digest}；"
                "本次安装未拉起（未用 --with-updater）。"
            )
        if not backup.get("restore_point_ready"):
            if self.o.enable_updates:
                # 开关开着却没有恢复点 = 能点更新但回不来：必须说清，别只说“不可一键更新”。
                self.say(
                    "  备份验收    **尚无完整恢复点**：更新开关已打开，但更新前请先完成"
                    "`ling backup` + `ling restore-drill`（无恢复点的更新等于无法回滚）。"
                )
            else:
                self.say(
                    "  备份验收    尚无完整恢复点 → **可使用但不可一键更新**；"
                    "请先运行 `ling backup` 并做一次 `ling restore-drill`（隔离库，不碰生产库）。"
                )
        elif self.o.enable_updates:
            self.say("  备份验收    最近恢复点四个部件齐全（网页更新已启用，可回滚）。")
        else:
            self.say(
                "  备份验收    最近恢复点四个部件齐全；网页更新开关仍是 0，"
                "打开方法见上。"
            )
        self.say("")
        self.say(
            "  未完成项    定期备份 + 恢复演练（`ling backup` / `ling restore-drill`）。"
        )
        if not self.o.enable_updates:
            self.say(
                "              网页更新开关（默认关；要开按上面的命令加 --enable-updates）。"
            )
        self.print_password_once()
        self.say("")
        self.say("下一步：")
        self.say(f"  * 浏览器打开 {url} 登录；用户名 {self.o.admin_user}。")
        self.say("  * `ling status` 复核实况；`ling logs --service api` 看日志。")
        self.say("  * 请把受限 .env（含 LING_ROOT_KEY）与备份作为同一恢复点离线保管。")
        if self.o.updater_digest is None:
            self.say(
                "  * 网页更新要等更新器镜像的发布证据：取得 "
                f"{UPDATER_REPOSITORY}@sha256:<64 位 hex> 后写入 "
                f"{self.layout.updater_env_file} 的 LING_UPDATER_IMAGE（本次不猜摘要）。"
            )
        elif not self.updater_started:
            self.say("  * 更新器已配置但未拉起；需要时用同一组身份手工启动：")
            self.say(
                f"      docker compose -p {self.updater_project} "
                f"-f {self.o.updater_compose_file} "
                f"--env-file {self.layout.updater_env_file} up -d"
            )
        else:
            self.say(
                "  * `ling doctor` 会核对更新器容器是否在跑、协议版本与镜像 digest"
                "（不在跑会如实列为不一致）。"
            )

    def print_password_once(self) -> None:
        password = self.secret_values.get("LING_BOOTSTRAP_ADMIN_PASSWORD")
        if not password:
            return
        if sys.stdin.isatty() and sys.stdout.isatty():
            self.password_shown = True
            self.state["admin"]["password_shown_once"] = True
            with contextlib.suppress(OSError):
                self.write_state()
            self.say("")
            self.say(f"  初始口令    {password}")
            self.say("              （只显示这一次；已写入受限 .env（0600）。请立即修改。）")
            self.say("              不要把口令粘贴到聊天/工单/日志里。")
        else:
            self.say("")
            self.say(
                "  初始口令    未在非交互终端显示；请用受限方式读取 "
                f"{self.layout.env_file} 的 LING_BOOTSTRAP_ADMIN_PASSWORD"
            )

    # ------------------------------------------------------------ 失败
    def handle_failure(self, error: LingError) -> int:
        # 不变式：没执行过迁移，就不许把失败标成 after_migration。
        # 任何报告 after_migration 的错误都必须以 migration.attempted 背书；
        # 不一致时以**记录的事实**为准，并纠正状态名（真机缺陷的教训）。
        attempted = self.migration_attempted()
        if error.after_migration and not attempted:
            self.log.emit(
                "注意：本次失败发生在迁移执行之前（migration.attempted=False）；"
                "不按“已迁移后失败”处理。"
            )
            error.after_migration = False
            if error.status == "failed_after_migration":
                error.status = (
                    "failed_db_ready"
                    if self.phase in {"database", "migrate"}
                    else "failed_preflight"
                )
        self.state["status"] = error.status
        self.state["finished_at"] = utc_now()
        self.state["exit_code"] = error.exit_code
        self.state["failure"] = {
            "step": error.step,
            "message": error.message,
            "after_migration": bool(error.after_migration),
        }
        self.state["next_actions"] = list(error.next_actions)
        self.log.emit(f"失败（{error.status}/{error.step}）：{error.message}")
        for action in error.next_actions:
            self.log.emit(f"下一步：{action}")
        cleaned = False
        if self.layout_created or self.layout.state_file.exists():
            try:
                self.write_state()
            except OSError as exc:
                self.log.emit(f"严重：失败状态也写不进去（{self.layout.state_file}）：{exc}")
        if not self.container_started and self.phase in ("layout", "compose_config"):
            cleaned = self.cleanup_layout()
        if cleaned:
            self.log.emit(
                "已清理本次生成但未使用的配置文件（未创建任何容器/卷）；"
                "安装锁文件按约定保留，修复后可直接重跑"
            )
        elif self.container_started:
            self.log.emit(
                "已创建容器/数据：不要直接重跑（会因 .env 已存在被拒）；先看状态与日志。"
            )
        return error.exit_code

    def cleanup_layout(self) -> bool:
        """只删除**本次自己创建**、且内容未被改动的文件/空目录。"""
        removed = False
        try:
            if self.layout.compose_file.exists():
                self.layout.compose_file.unlink()
                removed = True
            if os.path.lexists(self.layout.env_file):
                real = Path(os.path.realpath(self.layout.env_file))
                if real.parent == self.layout.root:
                    real.unlink()
                    removed = True
            if self.layout.state_file.exists():
                self.layout.state_file.unlink()
                removed = True
            # 更新器 env 与目录：同样是本次安装生成的配置，失败回滚时一起清掉
            # （state/requests 里有更新器日志/任务时不为空，只删空目录，绝不递归删）。
            if os.path.lexists(self.layout.updater_env_file):
                self.layout.updater_env_file.unlink()
                removed = True
            for path, flag in (
                (self.layout.logs_dir, self.created["logs_dir"]),
                (self.layout.compose_dir, self.created["compose_dir"]),
                (self.layout.updater_requests_dir, self.created["updater_requests_dir"]),
                (self.layout.updater_state_dir, self.created["updater_state_dir"]),
                (self.layout.updater_dir, self.created["updater_dir"]),
            ):
                if flag and path.exists() and path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
                    removed = True
        except OSError as exc:
            self.log.emit(f"注意：清理未完成的安装文件失败：{exc}")
            return False
        return removed


# ---------------------------------------------------------------- 镜像/清单工具


def manifest_platforms(data: Any) -> list[dict[str, Any]]:
    raw: Any = data
    if isinstance(data, dict) and isinstance(data.get("manifests"), list):
        raw = data["manifests"]
    elif isinstance(data, dict) and isinstance(data.get("platform"), dict):
        raw = [data]
    if not isinstance(raw, list):
        return []
    platforms: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        platform = item.get("platform")
        if platform is None and isinstance(item.get("Descriptor"), dict):
            platform = item["Descriptor"].get("platform")
        if not isinstance(platform, dict):
            continue
        os_name = platform.get("os") or platform.get("OS")
        arch = platform.get("architecture") or platform.get("Architecture")
        if not os_name or not arch:
            continue
        digest = item.get("digest")
        if digest is None and isinstance(item.get("Descriptor"), dict):
            digest = item["Descriptor"].get("digest")
        platforms.append(
            {
                "platform": f"{str(os_name).lower()}/{str(arch).lower()}",
                "digest": str(digest).removeprefix("sha256:") if digest else None,
            }
        )
    return platforms


def service_environment(service: dict[str, Any]) -> dict[str, str]:
    """compose config 的 environment 可能是映射或 ``KEY=VALUE`` 列表。"""
    raw = service.get("environment") or {}
    if isinstance(raw, list):
        values: dict[str, str] = {}
        for item in raw:
            if isinstance(item, str) and "=" in item:
                key, value = item.split("=", 1)
                values[key] = value
        return values
    if isinstance(raw, dict):
        return {str(key): "" if value is None else str(value) for key, value in raw.items()}
    return {}


def image_env(image: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in (image.get("Config") or {}).get("Env") or []:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            values[key] = value
    return values


def image_labels(image: dict[str, Any]) -> dict[str, str]:
    raw = (image.get("Config") or {}).get("Labels") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value is not None}


def image_commit(image: dict[str, Any]) -> str | None:
    return image_labels(image).get("org.opencontainers.image.revision") or None


def image_registry(ref: str) -> str:
    """镜像引用所属的仓库主机——失败文案必须**点名**该连谁。

    规则与 Docker 一致：名字的第一段只有含 ``.`` / ``:`` 或等于 ``localhost``
    时才是仓库主机；否则是 Docker Hub 的官方镜像（默认 ``registry-1.docker.io``）。
    所以 ``ghcr.io/qsbb/ling-api@sha256:…`` → ``ghcr.io``，而
    ``postgres:16-alpine`` → ``registry-1.docker.io（Docker Hub）``。
    """
    name = ref.split("@", 1)[0]
    first, sep, _rest = name.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        return first
    return "registry-1.docker.io（Docker Hub）"


def repo_digest_suffixes(image: dict[str, Any]) -> set[str]:
    return {
        str(item).rsplit("@sha256:", 1)[1]
        for item in image.get("RepoDigests") or []
        if "@sha256:" in str(item)
    }


# ---------------------------------------------------------------- status


@dataclass
class StatusOptions:
    dir: Path
    state_path: Path
    env_file: Path
    compose_files: tuple[Path, ...]
    project: str
    ready_timeout: float


@dataclass
class CompareRow:
    label: str
    recorded: str
    live: str
    ok: bool
    note: str = ""
    # 「警告」= 未能与实况核对（不是"记录 vs 实况"的硬差异）；不当作 OK，但也不
    # 单独把 status 的退出码拉成 1——与 doctor 的 warn 同一语义。
    warn: bool = False


def load_status_options(
    args: argparse.Namespace, *, dir_field: str = "dir"
) -> tuple[StatusOptions, dict[str, Any] | None]:
    raw_state = (args.state or "").strip()
    if raw_state:
        state_path = Path(raw_state).expanduser().resolve()
        default_dir = state_path.parent
    else:
        raw_dir = (getattr(args, dir_field, "") or "").strip()
        default_dir = Path(raw_dir).expanduser().resolve() if raw_dir else Path.home() / "ling"
        state_path = default_dir / STATE_FILE_NAME
    state: dict[str, Any] | None = None
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LingError(
                EXIT_USAGE, "failed_usage", "state", f"安装状态不是 JSON：{state_path}（{exc}）"
            ) from exc
    install_dir = default_dir
    project = (args.project or "").strip()
    env_file = (args.env_file or "").strip()
    compose_files = [str(args.compose_file)] if (args.compose_file or "").strip() else []
    if isinstance(state, dict):
        layout = state.get("layout") or {}
        if layout.get("dir"):
            install_dir = Path(str(layout["dir"]))
        if not project:
            project = str(state.get("project") or "")
        if not env_file and layout.get("env_file"):
            env_file = str(layout["env_file"])
        if not compose_files:
            compose = layout.get("compose") or {}
            files = compose.get("files") or []
            compose_files = [str(item) for item in files if isinstance(item, str)]
    if not project:
        project = DEFAULT_PROJECT
    layout_obj = Layout(install_dir, project)
    if not env_file:
        env_file = str(layout_obj.env_file)
    if not compose_files:
        compose_files = [str(layout_obj.compose_file)]
    options = StatusOptions(
        dir=install_dir,
        state_path=state_path,
        env_file=Path(env_file),
        compose_files=tuple(Path(item) for item in compose_files),
        project=project,
        ready_timeout=float(getattr(args, "ready_timeout", 5.0)),
    )
    return options, state


def status_command(args: argparse.Namespace) -> int:
    options, state = load_status_options(args)
    log = Logger()
    runner = Runner(log)
    rows: list[CompareRow] = []

    def say(line: str = "") -> None:
        print(line, flush=True)

    no_record = state is None
    say(f"安装目录：{options.dir}")
    if state is None:
        say(f"安装记录：**没有** {options.state_path}")
        if not options.compose_files[0].is_file():
            say("没有安装记录，也没有可用的 Compose 文件组；先运行 `ling install`。")
            return EXIT_USAGE
        say("（继续按实况检查；没有记录时无法核对 digest/commit 基线）")
    else:
        say(
            f"安装记录：{options.state_path}（schema={state.get('schema')}，"
            f"状态={state.get('status')}，安装于 {state.get('started_at')}）"
        )
        if state.get("adopted"):
            say(
                "记录来源：**adopt 现场登记**"
                f"（{state.get('adopted_at') or '时间未知'}）；"
                "started_at 等无法从实况推断的项在下方按 unknown 列出"
            )
        if state.get("failure"):
            failure = state["failure"]
            say(f"记录里的失败：{failure.get('step')}：{failure.get('message')}")
        # 更新器项目名**优先读记录**（updater.project），记录缺失才派生。同机
        # 多实例时，这一行就是"这个更新器到底服务哪个安装"的直接答案。
        updater_record = state.get("updater")
        if isinstance(updater_record, dict) and (
            updater_record.get("project") or updater_record.get("image")
        ):
            recorded_project = updater_record.get("project")
            source = (
                "来自安装记录 updater.project"
                if isinstance(recorded_project, str) and recorded_project.strip()
                else "旧记录没有 updater.project：按应用项目名派生"
            )
            say(f"更新器项目：{updater_project_of(state)}（{source}）")

    docker = runner.run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if docker.returncode != 0:
        say(f"Docker 不可用或无权限：{tail(docker.stderr or docker.stdout)}")
        return EXIT_PREFLIGHT

    missing = [str(path) for path in options.compose_files if not path.is_file()]
    if missing:
        say(f"Compose 文件缺失：{', '.join(missing)}")
        return EXIT_USAGE
    group = ComposeGroup(options.project, options.compose_files, options.env_file)
    if not options.env_file.is_file():
        say(f"受限 env 缺失：{options.env_file}（无法解析 compose；改看容器实况）")
        config, config_problem = None, f"受限 env 缺失：{options.env_file}"
    else:
        config, config_problem = _compose_config_model(runner, options)
        if config is None:
            say(config_problem)

    live_image = None
    live_update_enabled: bool | None = None
    if config is not None:
        services = config.get("services") or {}
        api = services.get("api") or {}
        live_image = api.get("image")
        # 开关的**实况**只能从 compose 模型取：api 的 LING_UPDATE_ENABLED 是
        # ${LING_UPDATE_ENABLED:-0}，解析出来的值就是容器真正收到的值。
        live_switch = service_environment(api).get("LING_UPDATE_ENABLED")
        if live_switch is not None:
            live_update_enabled = str(live_switch).strip() not in {"", "0"}
    if config is None:
        live_port, live_problem = None, config_problem
    else:
        live_port, live_problem = _published_api_port(config)
    # 与 `ling doctor` 共用同一套优先级与标注：实况 → 记录 → 默认值；
    # 不是实况值时行里必须写"未与实况核对"，且 200 只能算警告。
    target = api_probe_target(state, live_port=live_port, live_problem=live_problem)
    url = target.url

    row_image_id = ""
    row_commit = ""
    recorded_image = (state or {}).get("image") or {}
    recorded_digest = recorded_image.get("requested_digest")
    recorded_image_id = recorded_image.get("image_id")
    recorded_commit = recorded_image.get("source_commit")

    if live_image:
        rows.append(
            CompareRow(
                "compose 镜像引用",
                str(recorded_digest or "（无记录）"),
                str(live_image),
                bool(recorded_digest) and str(live_image) == str(recorded_digest),
                "来自 docker compose config",
            )
        )

    containers = {}
    for service in ("api", "worker", "db"):
        found = _status_containers(runner, options.project, service)
        if len(found) != 1:
            rows.append(
                CompareRow(
                    f"{service} 容器",
                    "1 个运行中",
                    f"{len(found)} 个",
                    False,
                    "数量不对",
                )
            )
            continue
        info = _status_inspect(runner, found[0]["id"])
        containers[service] = {"container": found[0], "info": info}
        if info is None:
            rows.append(CompareRow(f"{service} 容器", found[0]["name"], "无法 inspect", False))
            continue
        state_info = info.get("State") or {}
        health = (state_info.get("Health") or {}).get("Status") or "-"
        running = state_info.get("Running") is True
        rows.append(
            CompareRow(
                f"{service} 运行状态",
                "running",
                f"{state_info.get('Status') or '?'}（health={health}）",
                bool(running) and health in ("healthy", "-"),
            )
        )

    api_info = (containers.get("api") or {}).get("info")
    if api_info is not None:
        live_image_id = api_info.get("Image")
        row_image_id = str(live_image_id or "")
        if recorded_image_id:
            rows.append(
                CompareRow(
                    "api 运行 image ID",
                    str(recorded_image_id),
                    row_image_id or "（未知）",
                    row_image_id == str(recorded_image_id),
                )
            )
        if live_image_id:
            image_info = _status_image_inspect(runner, str(live_image_id))
            if image_info is not None:
                suffixes = repo_digest_suffixes(image_info)
                hex_digest = str(recorded_image.get("index_digest_hex") or "")
                platform_digest = str(recorded_image.get("platform_digest") or "")
                accepted = {item for item in (hex_digest, platform_digest) if item}
                live_suffix = sorted(suffixes)[0] if suffixes else "（无 RepoDigests）"
                rows.append(
                    CompareRow(
                        "api 运行镜像摘要",
                        str(recorded_digest or "（无记录）"),
                        "sha256:" + live_suffix if suffixes else live_suffix,
                        bool(accepted) and bool(suffixes & accepted),
                        "以容器实际镜像的 RepoDigests 为准",
                    )
                )
                row_commit = image_commit(image_info) or ""
                if recorded_commit:
                    rows.append(
                        CompareRow(
                            "镜像源提交",
                            str(recorded_commit),
                            row_commit or "（镜像未标注）",
                            row_commit == str(recorded_commit),
                        )
                    )

    worker_info = (containers.get("worker") or {}).get("info")
    if api_info is not None and worker_info is not None:
        api_live_id = str(api_info.get("Image") or "")
        worker_live_id = str(worker_info.get("Image") or "")
        rows.append(
            CompareRow(
                "api/worker 同一运行镜像",
                str(recorded_image_id or "（无记录）"),
                f"api={api_live_id or '?'} worker={worker_live_id or '?'}",
                bool(api_live_id)
                and api_live_id == worker_live_id
                and (not recorded_image_id or api_live_id == str(recorded_image_id)),
                "镜像模式下两者必须同一 image ID",
            )
        )

    code, body = http_probe(
        f"http://{target.host}:{target.port}", "/readyz", timeout=options.ready_timeout
    )
    rows.append(
        CompareRow(
            "/readyz",
            "恰好 200",
            f"{code}" + ("（重定向，不跟随）" if 300 <= code < 400 else ""),
            # 端口不是实况值时，200 只证明"记录/默认端口上的那个目标活着"，
            # 不能证明是本部署——只能给警告（与 doctor 同口径）。
            ok=code == 200 and target.verified,
            warn=code == 200 and not target.verified,
            note=(
                f"{url}/readyz；端口来自{target.source}"
                + ("" if target.verified else "；未与实况核对，200 只算警告")
            ),
        )
    )
    if (
        target.live_port is not None
        and target.recorded_port is not None
        and target.recorded_port != target.live_port
    ):
        rows.append(
            CompareRow(
                "宿主端口（记录 vs 实况）",
                f"记录 {target.recorded_port}",
                f"实况 {target.live_port}",
                False,
                "记录已过期；/readyz 按实况探",
                warn=True,
            )
        )
    live_version = None
    live_version_commit = None
    version_probe, version_probe_error = container_version_probe(runner, group)
    if not version_probe_error:
        live_version = version_probe.get("version")
        live_version_commit = version_probe.get("commit")
    recorded_version = ((state or {}).get("result") or {}).get("version") or recorded_image.get(
        "version"
    )
    rows.append(
        CompareRow(
            "运行容器自报版本/提交",
            f"{recorded_version or '（无记录）'} / {recorded_commit or '（无提交）'}",
            (
                f"读不到（{version_probe_error}）"
                if version_probe_error
                else (
                    f"{live_version or '（不可用）'} / {live_version_commit or '（无提交）'}"
                )
            ),
            not version_probe_error
            and (not recorded_version or str(live_version) == str(recorded_version))
            and (not recorded_commit or str(live_version_commit) == str(recorded_commit)),
            "容器内进程证据（compose exec，不经过 HTTP）",
        )
    )

    for key in ("app-data", "db-data"):
        name = f"{options.project}_{key}"
        info = _status_volume_inspect(runner, name)
        recorded_volume = ((state or {}).get("volumes") or {}).get(key) or {}
        if info is None:
            rows.append(
                CompareRow(f"卷 {key}", str(recorded_volume.get("name") or name), "不存在", False)
            )
            continue
        labels = info.get("Labels") or {}
        live_desc = f"{info.get('Name')}（driver={info.get('Driver')}, 项目={labels.get('com.docker.compose.project')}）"
        recorded_desc = (
            f"{recorded_volume.get('name')}（driver={recorded_volume.get('driver')}, "
            f"项目={recorded_volume.get('project_label')}）"
            if recorded_volume
            else "（无记录）"
        )
        ok = (
            bool(recorded_volume)
            and str(info.get("Name")) == str(recorded_volume.get("name"))
            and labels.get("com.docker.compose.project")
            == recorded_volume.get("project_label")
        )
        rows.append(CompareRow(f"卷 {key} 身份", recorded_desc, live_desc, ok))

    live_head = None
    if options.env_file.is_file() and "api" in containers:
        result = runner.run(
            [
                *group.argv(),
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "ling",
                "-d",
                "ling",
                "-tAc",
                "select version_num from alembic_version",
            ],
            env=os.environ.copy(),
        )
        if result.returncode == 0:
            values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            live_head = values[-1] if values else None
    recorded_head = ((state or {}).get("database") or {}).get("alembic_version")
    rows.append(
        CompareRow(
            "数据库 alembic_version",
            str(recorded_head or "（无记录）"),
            str(live_head or "（读不到）"),
            bool(recorded_head) and live_head == recorded_head,
        )
    )

    backup_dir_raw = ((state or {}).get("backup") or {}).get("dir")
    if backup_dir_raw:
        backup_dir = Path(str(backup_dir_raw))
        latest = latest_manifest(backup_dir)
        recorded_backup = (state or {}).get("backup") or {}
        recorded_latest = recorded_backup.get("latest") or {}
        live_desc = (
            f"{latest.name}（{read_manifest(latest).get('created_at') or '时间未知'}）"
            if latest is not None
            else "无恢复点"
        )
        recorded_desc = (
            f"{Path(str(recorded_latest.get('manifest'))).name}"
            f"（{recorded_latest.get('created_at') or '时间未知'}）"
            if recorded_latest.get("manifest")
            else "无恢复点"
        )
        rows.append(
            CompareRow(
                "最近备份",
                recorded_desc,
                live_desc,
                (latest is None and not recorded_latest.get("manifest"))
                or (
                    latest is not None
                    and recorded_latest.get("manifest") == str(latest)
                ),
                str(backup_dir),
            )
        )
    update = (state or {}).get("update") or {}
    if update:
        recorded_update = "1（已启用）" if update.get("enabled") else "0（未启用）"
        if live_update_enabled is not None:
            live_update = "1（已启用）" if live_update_enabled else "0（未启用）"
            update_ok = live_update_enabled == bool(update.get("enabled"))
            update_note = "来自 docker compose config 的 api 环境"
        else:
            live_update = recorded_update
            update_ok = False
            update_note = f"读不到实况（{config_problem or 'compose 模型不可用'}）；按记录显示"
        rows.append(
            CompareRow(
                "网页一键更新（update.enabled）",
                recorded_update,
                live_update,
                update_ok,
                update_note,
                warn=live_update_enabled is None,
            )
        )

    say("")
    say("核对（以实况为准；记录只作参考）")
    say("-" * 40)
    mismatches = 0
    warns = 0
    for row in rows:
        if row.ok:
            mark = "OK  "
        elif row.warn:
            mark = "警告"
            warns += 1
        else:
            mark = "差异"
            mismatches += 1
        say(f"  [{mark}] {row.label}")
        say(f"         记录：{row.recorded}")
        say(f"         实况：{row.live}" + (f"  ({row.note})" if row.note else ""))
        if row.warn:
            say("         结论：未能与实况核对；这一项不按 OK 处理（不是记录 vs 实况的硬差异）")
        elif not row.ok:
            say("         结论：以实况为准；记录已过期或部署与记录不一致")

    unverified = (state or {}).get("unverified") if isinstance(state, dict) else None
    if isinstance(unverified, list) and unverified:
        say("")
        say("未能核实（adopted：没有实况可推断，按 **unknown** 处理，不是 OK）")
        say("-" * 40)
        for item in unverified:
            if not isinstance(item, dict) or not item.get("field"):
                continue
            say(f"  [unknown] {item.get('field')}：{item.get('reason') or '无法从实况推断'}")

    say("")
    if no_record:
        say("结论：**没有安装记录**，无法核对 digest/commit/卷身份基线；以上实况仅供参考。")
        say("     先确认 --dir 是否指向真正的安装目录；手工迁移来的部署用 `ling adopt` 登记实况。")
        return EXIT_USAGE
    if mismatches:
        say(f"结论：{mismatches} 项与安装记录不一致或有服务不健康（以实况为准）。")
        say("     不要按记录宣称版本/摘要；先核对容器、镜像与数据库实况。")
        return EXIT_STATUS_MISMATCH
    if warns:
        say(
            f"结论：实况与安装记录一致；另有 {warns} 项未能与实况核对"
            "（警告，不按 OK 处理）。"
        )
    else:
        say("结论：实况与安装记录一致，/readyz 200，卷身份匹配。")
    if update_enabled_for_display(state, live_update_enabled):
        say("     网页一键更新：**已启用**（update.enabled=1）——请求由")
        say(
            f"     {updater_project_of(state)} 项目执行；它没在跑时请求会停在发布目录里。"
        )
    else:
        say("     网页一键更新：未启用（update.enabled=0）——不影响当前使用。")
        say("     更新器是**已实现**的（默认关是发布证据/演练门槛），打开方法：")
        say(
            "       ling install --with-updater --enable-updates"
            "（或在现有部署上备好更新器/发布目录/恢复点后把 .env 的 "
            "LING_UPDATE_ENABLED 改成 1 并 up -d api）。"
        )
    return EXIT_OK


def update_enabled_for_display(
    state: dict[str, Any] | None, live: bool | None
) -> bool:
    """网页更新开关该按什么显示：**实况优先**，取不到才退回安装记录。

    ``live`` 来自 ``docker compose config`` 里 api 的 ``LING_UPDATE_ENABLED``
    （``None`` = 读不到实况）。status/doctor 共用这一口径，避免两处各说一套。
    """
    if live is not None:
        return live
    return bool(((state or {}).get("update") or {}).get("enabled"))


def _status_containers(runner: Runner, project: str, service: str) -> list[dict[str, str]]:
    result = runner.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
            "--format",
            "{{.ID}}|{{.Names}}|{{.State}}",
        ]
    )
    if result.returncode != 0:
        return []
    containers: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        containers.append({"id": parts[0], "name": parts[1], "state": parts[2]})
    return containers


def _status_project_containers(
    runner: Runner, project: str, *, all_states: bool = True
) -> list[dict[str, str]]:
    """项目下的容器（默认含已退出）；**只读**，供 doctor 核对更新器实况。"""
    argv = ["docker", "ps"]
    if all_states:
        argv.append("-a")
    argv += [
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--format",
        "{{.ID}}|{{.Names}}|{{.State}}",
    ]
    result = runner.run(argv)
    if result.returncode != 0:
        return []
    containers: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) != 3:
            continue
        containers.append({"id": parts[0], "name": parts[1], "state": parts[2]})
    return containers


def _status_inspect(runner: Runner, ref: str) -> dict[str, Any] | None:
    result = runner.run(["docker", "inspect", "--type", "container", ref])
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


# --- 更新器项目的归属（安装冲突守卫 + doctor 共用同一判据）--------------------
#
# 更新器的 compose 文件通常放在**共享的发行包代码目录**里，所以 Compose 的
# `working_dir` 标签对同一宿主上的所有安装都是同一个路径；真正区分实例的是
# **挂载源**里的安装根路径（`.env` / 安装锁 / `compose/` / `updater/*`）。
# 因此"属于本次安装"= working_dir 或任一挂载源落在安装根内；两者都不成立
# （或 inspect 不到）就是**别人的**，必须拒绝而不是接管。


def _path_within(path: str, root: Path) -> bool:
    """``path`` 是否就是 ``root`` 或在它下面（按解析后的绝对路径比较）。"""
    if not path:
        return False
    try:
        candidate = Path(path).expanduser().resolve()
        base = root.expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return candidate == base or candidate.is_relative_to(base)


def _updater_container_owner(info: dict[str, Any], install_dir: Path) -> str | None:
    """容器若属于 ``install_dir`` 这次安装，返回证据路径；否则返回 ``None``。"""
    labels = (info.get("Config") or {}).get("Labels") or {}
    working_dir = str(labels.get(UPDATER_WORKING_DIR_LABEL) or "")
    if _path_within(working_dir, install_dir):
        return working_dir
    for mount in info.get("Mounts") or []:
        if not isinstance(mount, dict):
            continue
        source = str(mount.get("Source") or "")
        if _path_within(source, install_dir):
            return source
    return None


def updater_project_owner_elsewhere(
    runner: Runner,
    project: str,
    install_dir: Path,
    *,
    containers: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """``project`` 下**不属于** ``install_dir`` 的容器（空列表 = 可安全拉起）。

    每个条目 ``{"name", "id", "owner"}``，``owner`` 是它**挂载的**另一个安装目录
    （没有可读挂载时才退回 ``working_dir``）——冲突时必须把"它到底是谁的"说清楚，
    否则用户只能靠猜。挂载源优先于 working_dir：更新器的 compose 文件通常在
    **共享代码目录**里，working_dir 对所有实例都一样，指向不了具体实例。
    ``containers`` 已由调用方查过时传进来，避免重复 ``docker ps``。
    """
    foreign: list[dict[str, str]] = []
    listing = (
        containers
        if containers is not None
        else _status_project_containers(runner, project)
    )
    for container in listing:
        info = _status_inspect(runner, container.get("id", ""))
        if info is not None and _updater_container_owner(info, install_dir) is not None:
            continue
        owner = ""
        if info is None:
            owner = "无法核对（docker inspect 失败）"
        else:
            labels = (info.get("Config") or {}).get("Labels") or {}
            sources = [
                str((mount or {}).get("Source") or "")
                for mount in info.get("Mounts") or []
                if isinstance(mount, dict)
            ]
            owner = next((source for source in sources if source), "")
            if not owner:
                owner = str(labels.get(UPDATER_WORKING_DIR_LABEL) or "")
            if not owner:
                owner = "未知（容器没有挂载/working_dir 标签）"
        foreign.append(
            {
                "name": str(container.get("name") or "?"),
                "id": str(container.get("id") or ""),
                "owner": owner,
            }
        )
    return foreign


def _status_image_inspect(runner: Runner, ref: str) -> dict[str, Any] | None:
    result = runner.run(["docker", "image", "inspect", ref])
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def _status_volume_inspect(runner: Runner, name: str) -> dict[str, Any] | None:
    result = runner.run(["docker", "volume", "inspect", name])
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


# ---------------------------------------------------------------- logs


def logs_command(args: argparse.Namespace) -> int:
    options, _state = load_status_options(args)
    if args.tail < 1 or args.tail > 1000000:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"--tail 必须在 1–1000000：{args.tail}"
        )
    if not options.env_file.is_file():
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "env_file",
            f"受限 env 不存在：{options.env_file}",
            ["`ling logs` 必须与安装/发布使用同一份 --env-file。"],
        )
    for path in options.compose_files:
        if not path.is_file():
            raise LingError(
                EXIT_USAGE,
                "failed_usage",
                "compose_file",
                f"Compose 文件不存在：{path}",
            )
    group = ComposeGroup(options.project, options.compose_files, options.env_file)
    argv = [*group.argv(), "logs", "--tail", str(args.tail)]
    if args.follow:
        argv.append("--follow")
    argv.append(args.service)
    result = subprocess.run(argv, check=False)
    return result.returncode


# ---------------------------------------------------------------- doctor / backup / restore-drill
#
# 三个命令都**只读安装记录**：``doctor`` 完全不写；``backup`` 只让备份脚本写
# 备份目录（并重算产物哈希）；``restore-drill`` 只重建隔离库。三者都用安装时
# 生成的那一组 compose 身份（``-p`` / ``-f`` / ``--env-file``），绝不猜。


LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_FAIL = "fail"


@dataclass
class DoctorRow:
    level: str
    label: str
    detail: str


def _deploy_script(name: str, *, purpose: str) -> Path:
    """定位发行包里的备份/演练脚本（相对本脚本，不 import、不重写）。"""
    path = Path(__file__).resolve().parent.parent / BACKUP_SCRIPT_SUBDIR / name
    if not path.is_file():
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "script",
            f"{purpose}脚本不存在：{path}",
            ["独立部署包必须同时带 deploy/backup/backup.sh 与 restore_drill.sh。"],
        )
    if not os.access(path, os.X_OK):
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "script",
            f"{purpose}脚本不可执行：{path}",
            [f"chmod +x {path} 后重试；不要复制成另一个脚本绕过。"],
        )
    return path


def parse_env_file(path: Path) -> dict[str, str]:
    """只读解析受限 env 的 ``KEY=VALUE``；不插值、不打印任何值。

    口径与 ``deploy/release/publish.py`` 的 ``parse_env_file_value`` 一致（这里
    返回整份映射）：同一键以**最后一行**为准，忽略空行/注释，值两端空白去掉，
    再剥掉一层成对引号；``export KEY=VALUE`` 也认。另外允许 ``KEY = VALUE``
    （等号两侧有空白）——用户手改 ``.env`` 时很常见，不能因此漏读开关。
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and len(line) > 6 and line[6] in " \t":
            line = line[6:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            continue
        value = value.strip()
        if value[:1] in {"'", '"'} and len(value) >= 2 and value.endswith(value[0]):
            value = value[1:-1]
        values[key] = value
    return values


def ops_env(options: StatusOptions, *, root_key: str | None = None) -> dict[str, str]:
    """backup/restore-drill 的子进程环境：只给**同一组** compose 身份。

    **不**继承宿主上可能存在的 ``LING_COMPOSE_*``/``LING_BACKUP_*``：那些值可能
    指向另一套拓扑，备份/演练命中的就不是这套安装。数据库口令等仍只通过
    ``--env-file`` 交给 compose，不复制进本进程环境。
    """
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("LING_COMPOSE_", "LING_BACKUP_")) or key in (
            "LING_DATABASE_URL",
            "LING_DB_NAME",
            "LING_DB_USER",
            "LING_ROOT_KEY",
        ):
            env.pop(key, None)
    env["LING_BACKUP_VIA_COMPOSE"] = "1"
    env["LING_COMPOSE_PROJECT"] = options.project
    env["LING_COMPOSE_FILE"] = str(options.compose_files[0])
    if len(options.compose_files) > 2:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "compose_file",
            "compose 文件组超过两个 -f；deploy/backup/*.sh 只支持主文件 + 一个 override",
            ["核对安装记录里的 layout.compose.files；不要额外叠加文件。"],
        )
    if len(options.compose_files) == 2:
        env["LING_COMPOSE_OVERRIDE"] = str(options.compose_files[1])
    env["LING_COMPOSE_ENV_FILE"] = str(options.env_file)
    if root_key:
        # backup.sh 用「宿主环境里 LING_ROOT_KEY 是否非空」写 keys_env_set。ling
        # 的安装布局把根密钥放在受限 .env（compose 以环境变量注入容器），容器里
        # 因此没有 /data/root.key；只把这一把密钥交给备份脚本，manifest 才会如实
        # 记 keys_env_set=true，而不是误报"没有根密钥材料"。只在子进程环境里出现：
        # 不打印、不落盘、不写状态文件。
        env["LING_ROOT_KEY"] = root_key
    return env


def _require_ops_layout(
    options: StatusOptions, state: dict[str, Any] | None, command: str
) -> dict[str, Any]:
    if state is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "state",
            f"没有安装记录：{options.state_path}",
            [
                f"`ling {command}` 必须与安装用同一组 compose 文件/项目名/--env-file；"
                "先确认 --install-dir（或 --state）指向真正的安装目录。",
                "手工从源码模式迁移到镜像模式的部署没有安装记录：先运行 "
                "`ling adopt --install-dir <安装目录>` 只读登记实况，再重试 "
                f"`ling {command}`。",
            ],
        )
    if not options.env_file.is_file():
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "env_file",
            f"受限 env 不存在：{options.env_file}",
            [f"`ling {command}` 必须与安装使用同一份 --env-file。"],
        )
    missing = [str(path) for path in options.compose_files if not path.is_file()]
    if missing:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "compose_file",
            f"Compose 文件缺失：{', '.join(missing)}",
        )
    return state


# ---------------------------------------------------------------- recovery point


RECOVERY_POINT_FIELDS = (
    "dump",
    "dump_sha256",
    "tombstones",
    "tombstones_state",
    "media",
    "media_state",
    "keys_root",
    "keys_root_sha256",
    "keys_env_set",
)


@dataclass
class RecoveryPoint:
    manifest: Path
    fields: dict[str, str]
    problems: list[str]

    @property
    def complete(self) -> bool:
        return not self.problems


def inspect_recovery_point(
    backup_dir: Path, manifest: Path, *, verify_hashes: bool
) -> RecoveryPoint:
    """核对一份 manifest 的关键字段；**只读**。

    ``verify_hashes=False``（doctor）：只看结构（文件在不在、字段形状），避免
    每次诊断都把整份媒体归档重新哈希一遍。
    ``verify_hashes=True``（backup 之后）：重算 dump/媒体/根密钥的 sha256，
    证明落盘字节就是 manifest 记下的那份。
    """
    fields = read_manifest(manifest)
    problems: list[str] = []
    for key in RECOVERY_POINT_FIELDS:
        if not fields.get(key):
            problems.append(f"manifest 缺 {key} 字段")

    dump_name = fields.get("dump") or ""
    dump_digest = fields.get("dump_sha256") or ""
    if dump_name:
        if Path(dump_name).name != dump_name:
            problems.append(f"dump 不是纯文件名：{dump_name}")
        dump_path = backup_dir / dump_name
        if not dump_path.is_file() or dump_path.stat().st_size == 0:
            problems.append(f"dump 缺失或为空：{dump_name}")
        elif dump_digest and HEX64_RE.fullmatch(dump_digest) is None:
            problems.append("dump_sha256 不是 64 位小写 hex")
        elif verify_hashes and dump_digest and sha256_file(dump_path) != dump_digest:
            problems.append("dump_sha256 与 dump 文件内容不符")

    tombs_name = fields.get("tombstones") or ""
    tombs_state = fields.get("tombstones_state") or ""
    if tombs_state != "ok":
        problems.append(f"tombstones_state={tombs_state or '缺失'}（必须 ok）")
    if tombs_name and not (backup_dir / tombs_name).is_file():
        problems.append(f"墓碑清单缺失：{tombs_name}")

    media_state = fields.get("media_state") or ""
    media_name = fields.get("media") or ""
    media_digest = fields.get("media_sha256") or ""
    if media_state == "present":
        if not media_name or media_name == "absent":
            problems.append("media_state=present 但 media 字段没有文件名")
        else:
            media_path = backup_dir / media_name
            if not media_path.is_file() or media_path.stat().st_size == 0:
                problems.append(f"媒体归档缺失或为空：{media_name}")
            elif media_digest and HEX64_RE.fullmatch(media_digest) is None:
                problems.append("media_sha256 不是 64 位小写 hex")
            elif verify_hashes and media_digest and sha256_file(media_path) != media_digest:
                problems.append("media_sha256 与媒体归档内容不符")
    elif media_state == "absent":
        # 备份时容器里还没有 /data/media：结构上是完整的一份（没有媒体可备）。
        # 库里是否真的没有 media_ref 由 backup 的调用方核对（doctor 只看结构）。
        if media_name and media_name != "absent":
            problems.append(f"media_state=absent 但 media={media_name}")
    else:
        problems.append(f"media_state={media_state or '缺失'}（不是 present/absent）")

    keys_root = fields.get("keys_root") or ""
    keys_env = fields.get("keys_env_set") or ""
    keys_digest = fields.get("keys_root_sha256") or ""
    if keys_env not in ("true", "false"):
        problems.append(f"keys_env_set={keys_env or '缺失'}（必须是 true/false）")
    if keys_root == "present":
        key_path = backup_dir / "keys" / "root.key"
        if not key_path.is_file():
            problems.append("keys_root=present 但 keys/root.key 缺失")
        elif keys_digest and HEX64_RE.fullmatch(keys_digest) is None:
            problems.append("keys_root_sha256 不是 64 位小写 hex")
        elif verify_hashes and keys_digest and sha256_file(key_path) != keys_digest:
            problems.append("keys_root_sha256 与 keys/root.key 内容不符")
    elif keys_root == "absent":
        if keys_env != "true":
            problems.append(
                "根密钥既没有归档（keys_root=absent）也没有 env 来源（keys_env_set≠true）；"
                "恢复到新卷后 provider 凭据解不开"
            )
    else:
        problems.append(f"keys_root={keys_root or '缺失'}（必须是 present/absent）")
    return RecoveryPoint(manifest=manifest, fields=fields, problems=problems)


def _manifest_age_seconds(created_at: str | None) -> float | None:
    if not created_at:
        return None
    # 不用 datetime.UTC（3.11+）：本脚本要能在宿主自带的 3.9 上运行。
    for fmt, is_utc in (("%Y-%m-%dT%H:%M:%SZ", True), ("%Y%m%d-%H%M%S", False)):
        try:
            parsed = time.strptime(created_at, fmt)
        except ValueError:
            continue
        epoch = calendar.timegm(parsed) if is_utc else time.mktime(parsed)
        return max(0.0, time.time() - epoch)
    return None


# ---------------------------------------------------------------- doctor


def compose_version_row(raw_version: str) -> DoctorRow:
    """把 ``docker compose version --short`` 的版本号映射成 doctor 的分级。

    **不能假设版本号是 2.x**：真机上报过 ``v5.3.1 较旧（建议 2.20+）``——旧实现
    只比较 ``minor < 20``，把 major 5 的 minor 3 当成了 2.3。Compose v5 是
    **更新**，不是更旧。所以：

    - major < 2 → fail（需要 v2 的 ``config --format json``）；
    - major ≥ 3 → ok（minor 不再参与判断）；
    - major == 2 且 minor < 20 → warn（缺安装器依赖的 v2 能力）；
    - 解析不了 → warn（不知道就别报 ok，也别报 fail）。
    """
    match = re.match(r"^(\d+)\.(\d+)", raw_version)
    if match is None:
        return DoctorRow(LEVEL_WARN, "Docker Compose", f"版本无法解析：{raw_version!r}")
    major, minor = int(match.group(1)), int(match.group(2))
    if major < 2:
        return DoctorRow(LEVEL_FAIL, "Docker Compose", f"需要 v2（当前 {raw_version}）")
    if major == 2 and minor < 20:
        return DoctorRow(LEVEL_WARN, "Docker Compose", f"v{raw_version} 较旧（建议 2.20+）")
    return DoctorRow(LEVEL_OK, "Docker Compose", f"v{raw_version}")


def _doctor_docker(runner: Runner, rows: list[DoctorRow]) -> tuple[bool, Path | None]:
    """Docker/Compose 版本与权限；返回 (当前用户能否直连 daemon, docker root)。"""
    server = runner.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15.0)
    available = server.returncode == 0
    if available:
        rows.append(
            DoctorRow(
                LEVEL_OK,
                "Docker daemon",
                f"可达（Server {server.stdout.strip()}；当前用户无需 sudo）",
            )
        )
    else:
        detail = tail(server.stderr or server.stdout) or f"rc={server.returncode}"
        if os.geteuid() == 0:
            rows.append(DoctorRow(LEVEL_FAIL, "Docker daemon", f"不可达（root 也无权限）：{detail}"))
        else:
            probe = runner.run(
                ["sudo", "-n", "docker", "info", "--format", "{{.ServerVersion}}"],
                timeout=15.0,
            )
            if probe.returncode == 0:
                rows.append(
                    DoctorRow(
                        LEVEL_FAIL,
                        "Docker daemon",
                        (
                            f"当前用户不能直连（{detail}）；`sudo -n docker info` 可用"
                            f"（{probe.stdout.strip()}）。doctor 不提升权限，"
                            "下面的实况检查会失败"
                        ),
                    )
                )
            else:
                rows.append(
                    DoctorRow(
                        LEVEL_FAIL,
                        "Docker daemon",
                        f"当前用户不能直连，sudo -n 也不可用：{detail}",
                    )
                )
    compose = runner.run(["docker", "compose", "version", "--short"], timeout=15.0)
    raw_version = compose.stdout.strip().lstrip("v")
    if compose.returncode != 0:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "Docker Compose",
                f"v2 不可用：{tail(compose.stderr or compose.stdout)}",
            )
        )
    else:
        rows.append(compose_version_row(raw_version))
    docker_root: Path | None = None
    if available:
        root = runner.run(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=15.0)
        if root.returncode == 0 and root.stdout.strip():
            candidate = Path(root.stdout.strip())
            if candidate.is_dir():
                docker_root = candidate
    return available, docker_root


def _compose_permission_problem(path: Path, allowed_gid: int | None) -> str | None:
    """compose 文件的权限判定：给更新器按组读是**允许**的，世界位不是。

    普通安装是 0600；更新器是另一个 uid、安装器又不是 root 时，
    ``compose_access_for_updater`` 会走共享组路线改成 0640（属组=更新器 gid）。
    旧判定 ``mode & 0o077`` 会把这种**有意为之**的 0640 报成"权限过宽" fail，
    反而逼用户改回 0600 把更新器弄坏。所以这里按语义判：

    - 任何世界位（other）→ 过宽；
    - 属组位只允许"读"，且只有当属组确实是记录里的更新器共享组时；
    - 属组可写/可执行 → 过宽。
    """
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o007:
        return f"mode {mode:04o}，有世界权限位"
    group_bits = mode & 0o070
    if group_bits:
        if allowed_gid is None or info.st_gid != allowed_gid:
            return (
                f"mode {mode:04o}，属组 {info.st_gid} 不是更新器共享组 "
                f"{allowed_gid if allowed_gid is not None else '（记录里没有）'}"
            )
        if group_bits & 0o030:
            return f"mode {mode:04o}，属组可写/可执行"
    return None


def _doctor_layout(
    rows: list[DoctorRow],
    options: StatusOptions,
    state: dict[str, Any] | None,
) -> None:
    root = options.dir
    if not root.is_dir():
        rows.append(DoctorRow(LEVEL_FAIL, "安装目录", f"不存在：{root}"))
    else:
        mode = stat.S_IMODE(root.stat().st_mode)
        level = LEVEL_OK if mode & 0o077 == 0 else LEVEL_WARN
        rows.append(DoctorRow(level, "安装目录", f"{root}（mode {mode:04o}）"))

    for label, path in (("受限 .env", options.env_file), ("安装状态", options.state_path)):
        if not path.is_file():
            rows.append(DoctorRow(LEVEL_FAIL, label, f"缺失：{path}"))
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            rows.append(
                DoctorRow(
                    LEVEL_FAIL,
                    label,
                    f"权限过宽：{path}（mode {mode:04o}，要求 0600）",
                )
            )
        else:
            rows.append(DoctorRow(LEVEL_OK, label, f"{path}（mode {mode:04o}）"))

    missing = [str(path) for path in options.compose_files if not path.is_file()]
    # 更新器共享组：非 root 安装器走"共享组 + 组读"时，0640 是**设计**而不是过宽。
    shared_gid: int | None = None
    if isinstance(state, dict):
        updater_layout = (state.get("layout") or {}).get("updater") or {}
        for candidate in (
            (updater_layout.get("update_channel") or {}).get("shared_gid"),
            (updater_layout.get("user") or {}).get("gid"),
        ):
            if isinstance(candidate, int):
                shared_gid = candidate
                break
    wide: list[str] = []
    for path in options.compose_files:
        if not path.is_file():
            continue
        problem = _compose_permission_problem(path, shared_gid)
        if problem is not None:
            wide.append(f"{path}（{problem}）")
    if missing:
        rows.append(DoctorRow(LEVEL_FAIL, "Compose 文件组", f"缺失：{', '.join(missing)}"))
    elif wide:
        rows.append(DoctorRow(LEVEL_FAIL, "Compose 文件组", "权限过宽：" + "；".join(wide)))
    else:
        rows.append(
            DoctorRow(
                LEVEL_OK,
                "Compose 文件组",
                "、".join(str(path) for path in options.compose_files),
            )
        )

    if state is None:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "安装记录",
                f"没有 {options.state_path}（无法核对 digest/提交/卷身份基线）",
            )
        )
    elif state.get("failure"):
        failure = state["failure"]
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "安装记录",
                f"记录里有失败：{failure.get('step')}：{failure.get('message')}",
            )
        )
    elif state.get("status") != "complete":
        rows.append(
            DoctorRow(LEVEL_WARN, "安装记录", f"status={state.get('status')!r}（不是 complete）")
        )


def _doctor_disk(
    rows: list[DoctorRow],
    docker_root: Path | None,
    backup_dir: Path | None,
    min_free: int,
) -> None:
    if docker_root is None:
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "磁盘余量 镜像",
                "Docker 根目录未知（daemon 不可达）；未检查镜像侧余量",
            )
        )
    if docker_root is not None:
        try:
            usage = shutil.disk_usage(docker_root)
        except OSError as exc:
            rows.append(DoctorRow(LEVEL_WARN, "磁盘余量 镜像", f"读不到 {docker_root}：{exc}"))
        else:
            level = LEVEL_OK if usage.free >= min_free else LEVEL_FAIL
            rows.append(
                DoctorRow(
                    level,
                    "磁盘余量 镜像",
                    f"{docker_root} 剩余 {usage.free} B（阈值 {min_free} B）",
                )
            )
    if backup_dir is not None:
        probe = existing_ancestor(backup_dir)
        try:
            usage = shutil.disk_usage(probe)
        except OSError as exc:
            rows.append(DoctorRow(LEVEL_FAIL, "磁盘余量 备份", f"读不到 {probe}：{exc}"))
        else:
            level = LEVEL_OK if usage.free >= min_free else LEVEL_FAIL
            rows.append(
                DoctorRow(
                    level,
                    "磁盘余量 备份",
                    f"{probe} 剩余 {usage.free} B（阈值 {min_free} B）",
                )
            )


def _doctor_image(
    runner: Runner,
    options: StatusOptions,
    state: dict[str, Any] | None,
    rows: list[DoctorRow],
) -> None:
    recorded = (state or {}).get("image") or {}
    infos: dict[str, dict[str, Any] | None] = {}
    for service in ("api", "worker", "db"):
        found = _status_containers(runner, options.project, service)
        if len(found) != 1:
            rows.append(
                DoctorRow(
                    LEVEL_FAIL,
                    f"容器 {service}",
                    f"运行中的容器数量 {len(found)}（期望 1）",
                )
            )
            infos[service] = None
            continue
        info = _status_inspect(runner, found[0]["id"])
        if info is None:
            rows.append(
                DoctorRow(LEVEL_FAIL, f"容器 {service}", f"{found[0]['name']} 无法 inspect")
            )
            infos[service] = None
            continue
        state_info = info.get("State") or {}
        health = (state_info.get("Health") or {}).get("Status") or "-"
        running = state_info.get("Running") is True
        if not running:
            rows.append(
                DoctorRow(
                    LEVEL_FAIL,
                    f"容器 {service}",
                    f"{found[0]['name']} 未运行（{state_info.get('Status')}）",
                )
            )
        elif health not in ("healthy", "-"):
            rows.append(
                DoctorRow(LEVEL_FAIL, f"容器 {service}", f"{found[0]['name']} health={health}")
            )
        else:
            rows.append(
                DoctorRow(
                    LEVEL_OK,
                    f"容器 {service}",
                    f"{found[0]['name']} running（health={health}）",
                )
            )
        infos[service] = info

    api = infos.get("api")
    recorded_id = str(recorded.get("image_id") or "")
    recorded_digest = str(recorded.get("requested_digest") or "")
    recorded_commit = str(recorded.get("source_commit") or "")
    if api is not None:
        live_id = str(api.get("Image") or "")
        if not recorded_id:
            rows.append(DoctorRow(LEVEL_WARN, "镜像 image ID", "安装记录里没有 image ID"))
        elif live_id != recorded_id:
            rows.append(
                DoctorRow(LEVEL_FAIL, "镜像 image ID", f"记录 {recorded_id} ≠ 实况 {live_id}")
            )
        else:
            rows.append(DoctorRow(LEVEL_OK, "镜像 image ID", f"{live_id}（与记录一致）"))

        image_info = _status_image_inspect(runner, live_id) if live_id else None
        if image_info is None:
            rows.append(
                DoctorRow(LEVEL_FAIL, "运行镜像摘要", f"无法 inspect 运行镜像 {live_id or '（未知）'}")
            )
        else:
            suffixes = repo_digest_suffixes(image_info)
            accepted = {
                str(recorded.get("index_digest_hex") or ""),
                str(recorded.get("platform_digest") or ""),
            } - {""}
            live_desc = (
                "、".join(sorted("sha256:" + item for item in suffixes))
                or "（无 RepoDigests）"
            )
            if not accepted:
                rows.append(
                    DoctorRow(
                        LEVEL_WARN,
                        "运行镜像摘要",
                        f"{live_desc}；安装记录里没有摘要基线",
                    )
                )
            elif not suffixes & accepted:
                rows.append(
                    DoctorRow(
                        LEVEL_FAIL,
                        "运行镜像摘要",
                        f"记录 {recorded_digest or sorted(accepted)} ≠ 实况 {live_desc}",
                    )
                )
            else:
                rows.append(
                    DoctorRow(
                        LEVEL_OK,
                        "运行镜像摘要",
                        f"{live_desc}（与记录的 {recorded_digest or '摘要'} 一致）",
                    )
                )
            live_commit = image_commit(image_info) or ""
            if not recorded_commit:
                rows.append(
                    DoctorRow(
                        LEVEL_WARN,
                        "镜像源提交",
                        f"记录里没有源提交基线（实况 {live_commit or '未标注'}）",
                    )
                )
            elif live_commit != recorded_commit:
                rows.append(
                    DoctorRow(
                        LEVEL_FAIL,
                        "镜像源提交",
                        f"记录 {recorded_commit} ≠ 实况 {live_commit or '未标注'}",
                    )
                )
            else:
                rows.append(DoctorRow(LEVEL_OK, "镜像源提交", live_commit))

    worker = infos.get("worker")
    if api is not None and worker is not None:
        api_id = str(api.get("Image") or "")
        worker_id = str(worker.get("Image") or "")
        if api_id and api_id == worker_id:
            rows.append(DoctorRow(LEVEL_OK, "api/worker 同一镜像", api_id))
        else:
            rows.append(
                DoctorRow(
                    LEVEL_FAIL,
                    "api/worker 同一镜像",
                    f"api={api_id or '?'} worker={worker_id or '?'}",
                )
            )


def _doctor_volumes(
    runner: Runner,
    options: StatusOptions,
    state: dict[str, Any] | None,
    rows: list[DoctorRow],
) -> None:
    recorded_volumes = (state or {}).get("volumes") or {}
    for key in ("app-data", "db-data"):
        name = f"{options.project}_{key}"
        info = _status_volume_inspect(runner, name)
        recorded = recorded_volumes.get(key) or {}
        if info is None:
            rows.append(DoctorRow(LEVEL_FAIL, f"卷 {key}", f"不存在：{name}"))
            continue
        labels = info.get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        live = f"{info.get('Name')}（driver={info.get('Driver')}, 项目={project}）"
        if not recorded:
            rows.append(
                DoctorRow(LEVEL_WARN, f"卷 {key} 身份", f"{live}；安装记录里没有该卷基线")
            )
            continue
        problems: list[str] = []
        if str(info.get("Name")) != str(recorded.get("name")):
            problems.append(f"名字 {info.get('Name')} ≠ 记录 {recorded.get('name')}")
        if info.get("Driver") != "local":
            problems.append(f"driver={info.get('Driver')}（要求 local）")
        if project != options.project or project != recorded.get("project_label"):
            problems.append(
                f"项目标签 {project} ≠ 记录 {recorded.get('project_label')}/{options.project}"
            )
        if problems:
            rows.append(
                DoctorRow(LEVEL_FAIL, f"卷 {key} 身份", live + "；" + "；".join(problems))
            )
        else:
            rows.append(DoctorRow(LEVEL_OK, f"卷 {key} 身份", live))


def _doctor_database(
    runner: Runner, options: StatusOptions, rows: list[DoctorRow]
) -> None:
    group = ComposeGroup(options.project, options.compose_files, options.env_file)
    db_head: str | None = None
    if options.env_file.is_file():
        result = runner.run(
            [
                *group.argv(),
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "ling",
                "-d",
                "ling",
                "-tAc",
                "select version_num from alembic_version",
            ],
            env=os.environ.copy(),
            timeout=30.0,
        )
        if result.returncode == 0:
            values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            db_head = values[-1] if values else None
    if db_head is None:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "数据库 alembic_version",
                "读不到（db 容器/项目名/env 不对，或库未迁移）",
            )
        )

    image_head: str | None = None
    head_result = runner.run(
        [
            *group.argv(),
            "exec",
            "-T",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "api",
            "python",
            "-c",
            ALEMBIC_HEAD_SCRIPT,
        ],
        env=os.environ.copy(),
        timeout=60.0,
    )
    if head_result.returncode == 0:
        heads, _revisions, _parents = parse_alembic_script_output(head_result.stdout)
        if len(heads) == 1:
            image_head = heads[0]
    if image_head is None:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "镜像内 Alembic head",
                "无法在运行中的 api 容器里算出 head（doctor 不新建容器）",
            )
        )
    elif db_head is None:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "alembic_version vs 镜像 head",
                f"镜像 head={image_head}；数据库读不到",
            )
        )
    elif db_head == image_head:
        rows.append(
            DoctorRow(
                LEVEL_OK,
                "alembic_version vs 镜像 head",
                f"{db_head} == {image_head}",
            )
        )
    else:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "alembic_version vs 镜像 head",
                f"数据库 {db_head} ≠ 镜像 head {image_head}",
            )
        )


def _compose_config_model(
    runner: Runner, options: StatusOptions
) -> tuple[dict[str, Any] | None, str]:
    """取 ``docker compose config --format json`` 的**合并后**模型。

    返回 ``(model, problem)``：失败时 model 为 ``None``、problem 说明原因。
    这是 status/doctor/adopt 读取同一条实况的**唯一**入口。
    """
    group = ComposeGroup(options.project, options.compose_files, options.env_file)
    result = runner.run(
        [*group.argv(), "config", "--format", "json"],
        env=os.environ.copy(),
        timeout=DEFAULT_COMMAND_TIMEOUT,
    )
    if result.returncode != 0:
        return None, f"docker compose config 失败：{tail(result.stderr or result.stdout)}"
    try:
        config = json.loads(result.stdout)
    except ValueError:
        return None, "docker compose config 输出不是 JSON"
    if not isinstance(config, dict):
        return None, "docker compose config 输出不是 JSON 对象"
    services = config.get("services")
    if not isinstance(services, dict):
        return None, "compose 模型里没有 services"
    return config, ""


def _published_api_port(config: dict[str, Any]) -> tuple[int | None, str]:
    """从**已取到的** compose 模型取 api 发布到宿主的**唯一**端口。"""
    services = config.get("services") or {}
    try:
        entries = service_published_ports(services)
    except LingError as exc:
        return None, f"compose 端口无法解析：{exc.message}"
    ports = sorted(
        {
            int(item["published"])
            for item in entries
            if item["service"] == "api" and str(item["target"]) == str(DEFAULT_PORT)
        }
    )
    if not ports:
        return None, f"compose 模型里没有 api 的 {DEFAULT_PORT} 发布端口"
    if len(ports) > 1:
        return None, f"compose 里 api 的 {DEFAULT_PORT} 对应多个宿主端口 {ports}"
    return ports[0], ""


def api_published_host(config: dict[str, Any]) -> str:
    """api 的 6520 条目上的宿主绑定地址（空串 = 所有网卡，即 0.0.0.0）。"""
    services = config.get("services") or {}
    api = services.get("api") if isinstance(services, dict) else None
    if not isinstance(api, dict):
        return ""
    for entry in api.get("ports") or []:
        if isinstance(entry, dict) and str(entry.get("target")) == str(DEFAULT_PORT):
            return str(entry.get("host_ip") or "")
    return ""


def _live_api_published_port(
    runner: Runner, options: StatusOptions
) -> tuple[int | None, str]:
    """从 ``docker compose config`` 的**实况模型**取 api 发布到宿主的端口。

    复用 ``service_published_ports`` / ``parse_published_port``（与
    ``deploy/release/publish.py`` 同一套语义，已由测试钉住），不另写一份解析。

    返回 ``(port, problem)``：拿不到**唯一**端口时 port 为 ``None``、problem 说明
    原因。调用方必须把"未核对"写进诊断行——不许拿安装记录或默认值冒充实况。
    """
    config, problem = _compose_config_model(runner, options)
    if config is None:
        return None, problem
    return _published_api_port(config)


@dataclass(frozen=True)
class ApiProbeTarget:
    """``/readyz`` 的探测目标 + **端口来源**（status / doctor 共用一套口径）。"""

    host: str
    port: int
    source: str
    verified: bool
    live_port: int | None
    live_problem: str
    recorded_port: int | None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def _recorded_listen(state: dict[str, Any] | None) -> tuple[str, int | None]:
    recorded = (state or {}).get("listen") or {}
    host = str(recorded.get("host") or DEFAULT_HOST)
    if host == "0.0.0.0":
        host = "127.0.0.1"
    raw_recorded_port = recorded.get("port")
    try:
        recorded_port: int | None = (
            int(raw_recorded_port) if raw_recorded_port not in (None, "") else None
        )
    except (TypeError, ValueError):
        recorded_port = None
    if recorded_port is not None and not (1 <= recorded_port <= 65535):
        # 记录里的 0/负数/越界不是"端口"，不能拿它当探测目标。
        recorded_port = None
    return host, recorded_port


def api_probe_target(
    state: dict[str, Any] | None,
    *,
    live_port: int | None,
    live_problem: str,
) -> ApiProbeTarget:
    """端口来源优先级：**实况 → 安装记录 → 默认值**（status / doctor 同一实现）。

    旧实现里 ``status`` 直接"记录 → 6520"且不标来源：compose config 失败又没有
    记录时，它会静默去探生产端口 6520，并把别人的 200 当成本部署的健康。
    这里把口径钉死：凡不是实况值，``source`` 必须写明"未与实况核对"，调用方
    据此把 200 降级为警告（不算 OK）。
    """
    host, recorded_port = _recorded_listen(state)
    if live_port is not None:
        port = live_port
        source = "实况（docker compose config）"
        verified = True
    elif recorded_port is not None:
        port = recorded_port
        source = f"安装记录，未与实况核对（{live_problem}）"
        verified = False
    else:
        port = DEFAULT_PORT
        source = f"默认值，未与实况核对（{live_problem}）"
        verified = False
    return ApiProbeTarget(
        host=host,
        port=port,
        source=source,
        verified=verified,
        live_port=live_port,
        live_problem=live_problem,
        recorded_port=recorded_port,
    )


def resolve_api_probe_target(
    runner: Runner, options: StatusOptions, state: dict[str, Any] | None
) -> ApiProbeTarget:
    """取实况端口并套用统一优先级；status/doctor 都调用它。"""
    live_port, live_problem = _live_api_published_port(runner, options)
    return api_probe_target(state, live_port=live_port, live_problem=live_problem)


def _doctor_http(
    runner: Runner,
    rows: list[DoctorRow],
    options: StatusOptions,
    state: dict[str, Any] | None,
) -> None:
    # 端口来源优先级：**实况（docker compose config）→ 安装记录 → 默认值**，
    # 与 `ling status` 共用 `api_probe_target`（同一实现，不是两份）。
    # 旧实现直接拿记录的 port、没有就退回 6520，于是在 ling-drill（实况 6521）
    # 上拿生产端口 6520 探通了另一个部署，还报成本部署的 [ok]。诊断工具不能把
    # 未核对的目标当成事实：凡不是实况值，行里必须写明"未与实况核对"，且 200
    # 也只给 warn。
    target = resolve_api_probe_target(runner, options, state)
    detail = f"{target.url}/readyz，端口来自{target.source}"

    code, _body = http_probe(
        f"http://{target.host}:{target.port}", "/readyz", timeout=options.ready_timeout
    )
    if code == 200:
        rows.append(
            DoctorRow(
                LEVEL_OK if target.verified else LEVEL_WARN,
                "/readyz",
                f"200（{detail}）",
            )
        )
    elif 300 <= code < 400:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "/readyz",
                f"{code}（重定向；doctor 不跟随 3xx）：{detail}",
            )
        )
    else:
        rows.append(DoctorRow(LEVEL_FAIL, "/readyz", f"{code}（要求恰好 200）：{detail}"))

    if (
        target.live_port is not None
        and target.recorded_port is not None
        and target.recorded_port != target.live_port
    ):
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "宿主端口（记录 vs 实况）",
                f"记录 {target.recorded_port} ≠ 实况 {target.live_port}"
                "（/readyz 按实况探；记录已过期）",
            )
        )

    # 版本/提交来自**容器内进程证据**（compose exec 问应用自己），不走 HTTP：
    # `/v1/version` 挂 CurrentAuth，doctor 不持有凭据，未鉴权恒 401。
    # 曾经这里就是打它，于是真机上 doctor 永远有一条假 fail。
    group = ComposeGroup(options.project, options.compose_files, options.env_file)
    payload, probe_error = container_version_probe(runner, group)
    if probe_error:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "运行容器自报版本/提交",
                f"读不到（{probe_error}）",
            )
        )
        return
    live_commit = str(payload.get("commit") or "")
    live_version = str(payload.get("version") or "")
    recorded_image = (state or {}).get("image") or {}
    expected_commit = str(recorded_image.get("source_commit") or "")
    if expected_commit and live_commit != expected_commit:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "运行容器自报提交",
                f"记录 {expected_commit} ≠ 实况 {live_commit or '空'}",
            )
        )
    elif not expected_commit:
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "运行容器自报提交",
                f"记录里没有提交基线（实况 {live_commit or '空'}）",
            )
        )
    else:
        rows.append(DoctorRow(LEVEL_OK, "运行容器自报提交", live_commit))
    recorded_version = str(
        ((state or {}).get("result") or {}).get("version")
        or recorded_image.get("version")
        or ""
    )
    if recorded_version and live_version != recorded_version:
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "运行容器自报版本",
                f"记录 {recorded_version} ≠ 实况 {live_version or '空'}",
            )
        )
    else:
        rows.append(DoctorRow(LEVEL_OK, "运行容器自报版本", live_version or "（未提供）"))


def _doctor_backup(
    runner: Runner,
    state: dict[str, Any] | None,
    rows: list[DoctorRow],
    max_age_days: int,
) -> None:
    recorded = (state or {}).get("backup") or {}
    raw_dir = str(recorded.get("dir") or "")
    backup_dir: Path | None = Path(raw_dir) if raw_dir else None
    if backup_dir is None:
        rows.append(DoctorRow(LEVEL_WARN, "备份目录", "安装记录里没有备份目录"))
    elif not backup_dir.is_dir():
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "备份目录",
                f"不存在：{backup_dir}（`ling backup` 会创建它）",
            )
        )
    elif not os.access(backup_dir, os.W_OK):
        rows.append(DoctorRow(LEVEL_FAIL, "备份目录", f"不可写：{backup_dir}"))
    else:
        rows.append(DoctorRow(LEVEL_OK, "备份目录", f"{backup_dir}（可写）"))

    manifests = (
        sorted(backup_dir.glob("manifest-*.txt"))
        if backup_dir is not None and backup_dir.is_dir()
        else []
    )
    points = [
        inspect_recovery_point(backup_dir, manifest, verify_hashes=False)
        for manifest in manifests
        if backup_dir is not None
    ]
    complete = [point for point in points if point.complete]
    if not points:
        rows.append(DoctorRow(LEVEL_WARN, "最近备份", "无恢复点（没有 manifest-*.txt）"))
    else:
        latest = points[-1]
        created_at = latest.fields.get("created_at") or "时间未知"
        detail = f"{latest.manifest.name}（{created_at}）"
        age = _manifest_age_seconds(latest.fields.get("created_at"))
        if latest.problems:
            rows.append(
                DoctorRow(
                    LEVEL_WARN,
                    "最近备份",
                    detail + "；不完整：" + "；".join(latest.problems[:4]),
                )
            )
        elif age is not None and age > max_age_days * 86400:
            rows.append(
                DoctorRow(
                    LEVEL_WARN,
                    "最近备份",
                    detail + f"；已 {age / 86400:.1f} 天（阈值 {max_age_days} 天）",
                )
            )
        else:
            rows.append(DoctorRow(LEVEL_OK, "最近备份", detail))
    level = LEVEL_OK if complete else LEVEL_WARN
    rows.append(
        DoctorRow(level, "恢复点数量", f"{len(complete)} 份完整 / {len(points)} 份 manifest")
    )

    cron = runner.run(["crontab", "-l"], timeout=10.0)
    cron_text = cron.stdout
    if cron.returncode == 127:
        rows.append(DoctorRow(LEVEL_WARN, "备份 cron", "本机没有 crontab 命令，无法确认"))
    elif cron.returncode != 0:
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "备份 cron",
                f"当前用户没有 crontab 条目（rc={cron.returncode}）",
            )
        )
    elif "backup.sh" not in cron_text:
        rows.append(DoctorRow(LEVEL_WARN, "备份 cron", "crontab 里没有 backup.sh"))
    elif raw_dir and raw_dir not in cron_text:
        rows.append(
            DoctorRow(LEVEL_WARN, "备份 cron", "有 backup.sh 行，但没有包含记录的备份目录")
        )
    else:
        rows.append(DoctorRow(LEVEL_OK, "备份 cron", "已安装（crontab 含 backup.sh 与记录目录）"))


def _doctor_update(
    state: dict[str, Any] | None,
    env_values: dict[str, str],
    rows: list[DoctorRow],
    live_switch: bool | None = None,
) -> None:
    """更新通道：**显示 update.enabled 的当前值**，关着就说怎么开。

    ``live_switch`` 来自 ``docker compose config`` 里 api 的
    ``LING_UPDATE_ENABLED``（``None`` = 读不到实况，退回 .env/记录）。

    三件事必须分清（旧实现把它们糊成一句“执行器没做”，是错的）：

    1. **开关关着是正常状态**，不是故障 → ``ok``，并给出打开方法；
    2. 开关关着但 ``.env`` 里写了 1 → ``fail``（写了却没进模型 = 部署不一致）；
    3. 开关开着但更新器没配置/没在跑 → ``fail``（没有执行者，网页按钮永远没人接）。
    """
    if state is None:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "更新通道",
                "没有安装记录，无法确认 update.enabled；本命令不会开启它",
            )
        )
        return
    # 按**解析后的键值**判断，不是子串匹配：`LING_UPDATE_ENABLED = 1`、
    # `export LING_UPDATE_ENABLED=1`、`LING_UPDATE_ENABLED="1"` 都必须发现。
    env_switch_on = env_values.get("LING_UPDATE_ENABLED") == "1"
    recorded = bool((state.get("update") or {}).get("enabled"))
    enabled = env_switch_on if live_switch is None else live_switch
    env_desc = f"LING_UPDATE_ENABLED={'1' if env_switch_on else '0'}"
    value_desc = f"update.enabled={'true' if enabled else 'false'}"
    source = (
        "来自 docker compose config 的 api 环境"
        if live_switch is not None
        else "来自受限 .env（compose 模型读不到）"
    )
    updater = state.get("updater") or {}
    updater_image = str(updater.get("image") or "")
    updater_started = bool(updater.get("started"))
    if live_switch is not None and live_switch != env_switch_on:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "更新通道",
                f"{value_desc}（{source}），但受限 .env 里是 {env_desc}：两处不一致，"
                "多半是 compose 被手改成了字面量（应写 ${LING_UPDATE_ENABLED:-0}）",
            )
        )
        return
    if enabled:
        if not updater_image:
            rows.append(
                DoctorRow(
                    LEVEL_FAIL,
                    "更新通道",
                    f"{value_desc}（{source}，{env_desc}），"
                    "但没有配置更新器镜像（LING_UPDATER_IMAGE）："
                    "没有执行者，网页更新会一直是“已提交、没人执行”",
                )
            )
        elif not updater_started:
            rows.append(
                DoctorRow(
                    LEVEL_WARN,
                    "更新通道",
                    f"{value_desc}（{source}，{env_desc}）；"
                    f"更新器 {updater_project_of(state)} 按记录未拉起，"
                    "请确认容器在跑（`ling doctor` 的“更新器”一行是实况）",
                )
            )
        else:
            rows.append(
                DoctorRow(
                    LEVEL_OK,
                    "更新通道",
                    f"{value_desc}（{source}，{env_desc}）；"
                    f"请求由 {updater_project_of(state)} 项目执行",
                )
            )
        return
    if recorded:
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "更新通道",
                f"安装记录写 update.enabled=true，但实况/受限 .env 是 {env_desc}："
                "以实况为准，按未启用处理",
            )
        )
        return
    rows.append(
        DoctorRow(
            LEVEL_OK,
            "更新通道",
            f"{value_desc}（{source}，{env_desc}）。网页更新未启用——这是**默认**且"
            "不是故障：更新器是已实现的，默认关是发布证据/演练门槛。"
            f"要打开：`ling install --with-updater --enable-updates`，"
            "或在现有部署上备好更新器镜像/发布目录条目/完整恢复点后，"
            "把受限 .env 的 LING_UPDATE_ENABLED 改成 1 并 `docker compose ... up -d api`"
            "（本命令只读，绝不在此开启）",
        )
    )


def _doctor_updater(
    runner: Runner,
    state: dict[str, Any] | None,
    rows: list[DoctorRow],
    *,
    docker_available: bool,
) -> None:
    """更新器实况（**只读**）：容器在不在跑、镜像 digest、协议版本、项目归属。

    级别约定：安装时**没有**配置更新器 → ``ok``（网页更新不可用是如实状态）；
    配置了但按设计未拉起 → ``warn``（不影响应用）；用过 ``--with-updater`` 却没有
    运行中的容器，或运行镜像与记录不一致、协议不兼容 → ``fail``；同名项目里的
    容器指向**另一个安装** → 也是 ``fail``（绝不按 ok 报告，那正是"两个实例
    静默互相接管"的样子）。
    """
    recorded = (state or {}).get("updater") or {}
    layout_updater = ((state or {}).get("layout") or {}).get("updater") or {}
    # 项目名**优先读安装记录**（updater.project）：记录缺失才按应用项目名派生。
    project = updater_project_of(state)
    image = str(recorded.get("image") or "")
    if not recorded:
        rows.append(
            DoctorRow(
                LEVEL_OK,
                "更新器",
                "安装记录里没有更新器信息（旧版安装器）；网页更新未启用",
            )
        )
        return
    if not image:
        rows.append(
            DoctorRow(
                LEVEL_OK,
                "更新器",
                "本次安装未配置更新器镜像（缺 --updater-digest / LING_UPDATER_IMAGE）："
                "网页更新不可用",
            )
        )
        return
    protocol = recorded.get("protocol") or UPDATER_PROTOCOL
    if not docker_available:
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "更新器",
                f"无法核对容器（Docker daemon 不可达）；记录摘要 {image}、协议 {protocol}",
            )
        )
        return
    requested = bool(recorded.get("requested"))
    containers = _status_project_containers(runner, project)
    install_dir = str(((state or {}).get("layout") or {}).get("dir") or "")
    if install_dir:
        foreign = updater_project_owner_elsewhere(
            runner, project, Path(install_dir), containers=containers
        )
        if foreign:
            owners = "、".join(
                f"{item['name']}（{item['owner']}）" for item in foreign[:3]
            )
            rows.append(
                DoctorRow(
                    LEVEL_FAIL,
                    "更新器",
                    f"项目 {project} 下的容器不属于本安装（{owners}；"
                    f"本安装 {install_dir}）：同名项目会让两个实例共用一个更新器，"
                    "网页更新可能操作另一个实例的数据",
                )
            )
            return
    running = [item for item in containers if item.get("state", "").startswith("running")]
    if not containers or not running:
        names = "、".join(item.get("name", "?") for item in containers) or "无容器"
        detail = (
            f"项目 {project} 现在没有运行中的更新器（{names}）；"
            f"记录摘要 {image}、协议 {protocol}"
        )
        if requested:
            rows.append(
                DoctorRow(
                    LEVEL_FAIL,
                    "更新器",
                    detail + "。安装时用 --with-updater 拉起过，实况与记录不一致",
                )
            )
        else:
            rows.append(
                DoctorRow(
                    LEVEL_WARN,
                    "更新器",
                    detail + "。本次安装未用 --with-updater，网页更新未启用",
                )
            )
        return

    info = _status_inspect(runner, running[0]["id"])
    if info is None:
        rows.append(
            DoctorRow(LEVEL_FAIL, "更新器", f"{running[0]['name']} 无法 inspect")
        )
        return
    live_ref = str((info.get("Config") or {}).get("Image") or "")
    problems: list[str] = []
    if live_ref != image:
        problems.append(f"镜像 记录 {image} ≠ 实况 {live_ref or '未记录'}")
    status_dir = str(layout_updater.get("state_dir") or "")
    live_protocol: int | None = None
    status_reason = ""
    if status_dir:
        status_path = Path(status_dir) / "status.json"
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            status_reason = f"状态文件不可读（{status_path}）：{type(exc).__name__}"
        else:
            if isinstance(payload, dict) and isinstance(payload.get("protocol"), int):
                live_protocol = payload["protocol"]
            else:
                status_reason = f"状态文件没有 protocol 字段（{status_path}）"
    else:
        status_reason = "安装记录里没有更新器状态目录"
    if live_protocol is not None and live_protocol != UPDATER_PROTOCOL:
        problems.append(f"协议 记录 {UPDATER_PROTOCOL} ≠ 更新器 {live_protocol}")
    detail_bits = [
        f"项目 {project} 运行中（{running[0]['name']}）",
        f"镜像 {live_ref or '未记录'}",
        "协议 " + (str(live_protocol) if live_protocol is not None else f"未知（{status_reason}）"),
    ]
    if problems:
        rows.append(DoctorRow(LEVEL_FAIL, "更新器", "；".join([*detail_bits, *problems])))
    elif live_protocol is None:
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "更新器",
                "；".join(detail_bits) + "（digest 与记录一致，但协议版本无法确认）",
            )
        )
    else:
        rows.append(
            DoctorRow(
                LEVEL_OK,
                "更新器",
                "；".join(detail_bits) + "（digest 与记录一致；网页更新开关见 update.enabled）",
            )
        )


def _doctor_adopted(state: dict[str, Any] | None, rows: list[DoctorRow]) -> None:
    """adopted 记录：**无法从实况推断**的项按 unknown 报告，绝不当作 ok。

    ``ling adopt`` 只登记能从运行中的部署核实的事实；``started_at`` /
    ``migration.*`` / "备份 cron 是谁装的" 这类没有实况可推断的项写进
    ``state["unverified"]``。这里逐条给出 ``warn``（不是 ``ok``），避免把
    "没有基线"悄悄读成"核对通过"。
    """
    if not isinstance(state, dict) or not state.get("adopted"):
        return
    rows.append(
        DoctorRow(
            LEVEL_OK,
            "安装记录",
            f"adopt 现场登记（{state.get('adopted_at') or '时间未知'}；"
            f"schema={state.get('schema')}，与 ling install 同一 schema）",
        )
    )
    entries = state.get("unverified") or []
    if not isinstance(entries, list):
        return
    for item in entries:
        if not isinstance(item, dict) or not item.get("field"):
            continue
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                f"未核实 {item.get('field')}",
                f"unknown：{item.get('reason') or '无法从实况推断'}"
                "（adopted 记录不把它当作已核对）",
            )
        )


def _doctor_registry(
    runner: Runner, state: dict[str, Any] | None, rows: list[DoctorRow]
) -> None:
    digest = str(((state or {}).get("image") or {}).get("requested_digest") or "")
    if not digest:
        rows.append(DoctorRow(LEVEL_WARN, "GHCR 可达性", "安装记录里没有镜像摘要"))
        return
    result = runner.run(["docker", "manifest", "inspect", digest], timeout=60.0)
    if result.returncode == 0:
        rows.append(DoctorRow(LEVEL_OK, "GHCR 可达性", f"可读 {digest}"))
    else:
        rows.append(
            DoctorRow(
                LEVEL_WARN,
                "GHCR 可达性",
                f"不可达：{tail(result.stderr or result.stdout)}",
            )
        )


def _render_doctor(rows: Sequence[DoctorRow]) -> int:
    counts = {LEVEL_OK: 0, LEVEL_WARN: 0, LEVEL_FAIL: 0}
    print("")
    print("检查结果")
    print("-" * 40)
    for row in rows:
        counts[row.level] = counts.get(row.level, 0) + 1
        print(f"  [{row.level}] {row.label}：{row.detail}")
    print("")
    print(f"合计：ok={counts[LEVEL_OK]} warn={counts[LEVEL_WARN]} fail={counts[LEVEL_FAIL]}")
    print("本命令只读：没有执行 docker run/up/pull/create/rm，也没有写任何文件。")
    if counts[LEVEL_FAIL]:
        print("结论：存在 fail —— 这套安装**现在不能按“一切正常”对待**；先处理上面的 fail 项。")
        return EXIT_STATUS_MISMATCH
    if counts[LEVEL_WARN]:
        print("结论：服务可用，但有 warn 项（不影响本命令退出码）；按需处理后再宣称“全部正常”。")
        return EXIT_OK
    print("结论：全部检查通过（实况与安装记录一致，备份与更新通道状态如实）。")
    return EXIT_OK


def doctor_command(args: argparse.Namespace) -> int:
    options, state = load_status_options(args)
    min_free = int(getattr(args, "min_free_bytes", DEFAULT_MIN_FREE_BYTES))
    max_age = int(getattr(args, "max_backup_age_days", DEFAULT_BACKUP_MAX_AGE_DAYS))
    if min_free <= 0:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"--min-free-bytes 必须 > 0（拿到 {min_free}）",
        )
    if max_age <= 0:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "arguments",
            f"--max-backup-age-days 必须 > 0（拿到 {max_age}）",
        )
    runner = Runner(Logger())
    rows: list[DoctorRow] = []
    print("ling doctor：只读诊断（不 run/up/pull，不建容器/卷，不写文件）")
    print(f"安装目录：{options.dir}")
    print(
        f"安装记录：{options.state_path}"
        + ("（缺失）" if state is None else f"（status={state.get('status')}）")
    )

    available, docker_root = _doctor_docker(runner, rows)
    _doctor_layout(rows, options, state)
    _doctor_adopted(state, rows)
    recorded_backup = str(((state or {}).get("backup") or {}).get("dir") or "")
    backup_dir = Path(recorded_backup) if recorded_backup else None
    _doctor_disk(rows, docker_root, backup_dir, min_free)
    if available:
        _doctor_image(runner, options, state, rows)
        _doctor_volumes(runner, options, state, rows)
        _doctor_database(runner, options, rows)
    else:
        rows.append(
            DoctorRow(
                LEVEL_FAIL,
                "实况核对",
                "跳过容器/镜像/卷/数据库：当前用户无法直连 Docker daemon",
            )
        )
    _doctor_http(runner, rows, options, state)
    _doctor_backup(runner, state, rows, max_age)
    env_values = parse_env_file(options.env_file)
    _doctor_update(state, env_values, rows, live_switch=_doctor_live_update_switch(runner, options))
    _doctor_updater(runner, state, rows, docker_available=available)
    _doctor_registry(runner, state, rows)
    return _render_doctor(rows)


def _doctor_live_update_switch(
    runner: Runner, options: StatusOptions
) -> bool | None:
    """api 的 ``LING_UPDATE_ENABLED`` 实况（来自 compose 模型）；读不到返回 ``None``。

    只读：与 status 走同一个 :func:`_compose_config_model` 入口。读不到时不猜，
    由调用方退回受限 ``.env`` 并按“未与实况核对”的口径说明。
    """
    if not options.env_file.is_file():
        return None
    config, _problem = _compose_config_model(runner, options)
    if config is None:
        return None
    api = (config.get("services") or {}).get("api") or {}
    value = service_environment(api).get("LING_UPDATE_ENABLED")
    if value is None:
        return None
    return str(value).strip() not in {"", "0"}


# ---------------------------------------------------------------- adopt
#
# 设计 §5.2：从源码模式切到镜像模式是一次**手工迁移**（publish.py 拒绝处理首次
# 切换），迁移完成后没有 ``install-state.json``。于是 status/doctor/backup/
# restore-drill 全部拒绝工作，doctor 也没有基线可核对。``ling adopt`` 就是把
# **现存部署的事实**现场登记成安装记录的那一环：只读采集，核对一致才写；
# 任何一项核对不上就拒绝——写错基线比不写更糟。


ADOPT_STATE_BACKUP_SUFFIX = ".bak-"


@dataclass
class AdoptOptions:
    install_dir: Path
    state_path: Path
    env_file: Path
    compose_files: tuple[Path, ...]
    project: str
    backup_dir: Path | None
    port: int | None
    ready_timeout: float
    force: bool


def load_adopt_options(args: argparse.Namespace) -> AdoptOptions:
    raw_dir = (getattr(args, "install_dir", "") or "").strip() or (
        getattr(args, "dir", "") or ""
    ).strip()
    raw_state = (getattr(args, "state", "") or "").strip()
    if raw_dir:
        install_dir = Path(raw_dir).expanduser().resolve()
    elif raw_state:
        install_dir = Path(raw_state).expanduser().resolve().parent
    else:
        install_dir = Path.home() / "ling"
    state_path = (
        Path(raw_state).expanduser().resolve()
        if raw_state
        else install_dir / STATE_FILE_NAME
    )
    project = (getattr(args, "project", "") or "").strip() or DEFAULT_PROJECT
    if PROJECT_RE.fullmatch(project) is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "project",
            f"Compose 项目名不合法：{project!r}",
            ["项目名只允许小写字母/数字/下划线/短横，且不以符号开头。"],
        )
    raw_env = (getattr(args, "env_file", "") or "").strip()
    env_file = (
        Path(raw_env).expanduser().resolve() if raw_env else install_dir / ".env"
    )
    raw_files = [
        str(item).strip()
        for item in (getattr(args, "compose_file", None) or [])
        if str(item).strip()
    ]
    compose_files = (
        tuple(Path(item).expanduser().resolve() for item in raw_files)
        if raw_files
        else (install_dir / COMPOSE_SUBDIR / COMPOSE_SOURCE_NAME,)
    )
    if len(compose_files) > 2:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "compose_file",
            "compose 文件组最多两个 -f（主文件 + 一个 override）",
            ["deploy/backup/*.sh 也只支持主文件 + 一个 override；不要额外叠加文件。"],
        )
    raw_port = (getattr(args, "port", "") or "").strip()
    port = _validate_port(raw_port) if raw_port else None
    raw_backup = (getattr(args, "backup_dir", "") or "").strip()
    backup_dir = Path(raw_backup).expanduser().resolve() if raw_backup else None
    return AdoptOptions(
        install_dir=install_dir,
        state_path=state_path,
        env_file=env_file,
        compose_files=compose_files,
        project=project,
        backup_dir=backup_dir,
        port=port,
        ready_timeout=float(getattr(args, "ready_timeout", 5.0)),
        force=bool(getattr(args, "force", False)),
    )


def adopt_backup_dir(options: AdoptOptions) -> Path | None:
    """备份目录：``--backup-dir`` → ``<dir>/updater/.env`` 的 LING_BACKUP_DIR。

    都没有就返回 ``None``（调用方拒绝）——adopt **不猜**备份目录：它可能是
    cron 正在写的那个目录，写错基线会让 ``ling backup`` 备份到别处。
    """
    if options.backup_dir is not None:
        return options.backup_dir
    updater_env = options.install_dir / UPDATER_SUBDIR / UPDATER_ENV_NAME
    raw = parse_env_file(updater_env).get("LING_BACKUP_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def backup_existing_state(path: Path) -> Path:
    """把旧记录复制成 ``<name>.bak-<时间戳>``（0600）并核对 sha256。

    覆盖前的**唯一**保险：复制后字节必须与旧文件逐字节相同（重算 sha256），
    否则拒绝覆盖。
    """
    data = path.read_bytes()
    expected = sha256_bytes(data)
    stamp = local_stamp()
    candidate = path.with_name(f"{path.name}{ADOPT_STATE_BACKUP_SUFFIX}{stamp}")
    counter = 2
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}{ADOPT_STATE_BACKUP_SUFFIX}{stamp}-{counter}"
        )
        counter += 1
    write_atomic(candidate, data, 0o600)
    actual = sha256_file(candidate)
    if actual != expected:
        with contextlib.suppress(OSError):
            candidate.unlink()
        raise LingError(
            EXIT_PREFLIGHT,
            "failed_preflight",
            "state_backup",
            (
                f"旧安装记录的备份校验失败：{candidate}（sha256 {actual} ≠ 旧文件 "
                f"{expected}）；拒绝覆盖。"
            ),
        )
    return candidate


def adopt_command(args: argparse.Namespace) -> int:
    options = load_adopt_options(args)
    runner = Runner(Logger())
    say = print

    def refuse(
        step: str, message: str, actions: Sequence[str] = ()
    ) -> LingError:
        return LingError(EXIT_PREFLIGHT, "failed_preflight", step, message, actions)

    say("ling adopt：把**现存部署**登记成安装记录（只读采集；不碰 compose/容器/镜像）")
    say(f"安装目录：{options.install_dir}")
    say(f"安装记录：{options.state_path}")
    say(f"项目名：{options.project}")
    say(f"文件组：{', '.join(str(path) for path in options.compose_files)}")
    say(f"受限 env：{options.env_file}")

    if not options.install_dir.is_dir():
        raise refuse(
            "install_dir",
            f"安装目录不存在：{options.install_dir}",
            ["--install-dir（或 --dir）必须指向真正在跑的部署目录。"],
        )
    if options.state_path.exists() and not options.force:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "state_exists",
            f"安装记录已存在：{options.state_path}",
            [
                "adopt 不覆盖已有记录（它可能是 install 或上一次 adopt 写的基线）。",
                "确需重建：先人工备份，再用 `ling adopt --force`"
                "（覆盖前本命令会先把旧文件复制成 <name>.bak-<时间戳> 并核对 sha256）。",
            ],
        )
    if options.state_path.exists() and not options.state_path.is_file():
        raise refuse(
            "state",
            f"安装记录路径不是普通文件：{options.state_path}",
            ["先人工处理那个路径（目录/链接都不是安装记录）。"],
        )
    if not options.env_file.is_file():
        raise refuse(
            "env_file",
            f"受限 env 不存在：{options.env_file}",
            ["adopt 需要与部署同一份 --env-file 才能解析 compose 模型；用 --env-file 指定。"],
        )
    for path in options.compose_files:
        if not path.is_file():
            raise refuse(
                "compose_file",
                f"Compose 文件不存在：{path}",
                [
                    "手工迁移的文件组不在默认位置时用 --compose-file 指定"
                    "（可给主文件 + 一个 override）。",
                ],
            )

    docker = runner.run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if docker.returncode != 0:
        raise refuse(
            "docker",
            f"Docker 不可用或无权限：{tail(docker.stderr or docker.stdout)}",
            ["用能访问本机 Docker daemon 的账号运行；adopt 不会改用别的 daemon。"],
        )

    env_values = parse_env_file(options.env_file)
    config, config_problem = _compose_config_model(runner, options)
    if config is None:
        raise refuse(
            "compose_config",
            f"无法解析 compose 模型：{config_problem}",
            [
                "卷的实际名称与 api 的发布端口都只能来自 docker compose config；"
                "adopt 不猜。",
                "核对 --compose-file / --env-file / --project 是否与部署一致。",
            ],
        )
    services = config.get("services") or {}
    for name in ("api", "worker", "db"):
        if not isinstance(services.get(name), dict):
            raise refuse(
                "compose_services",
                f"compose 模型里没有 {name} 服务：这个文件组不是这套部署的。",
                ["用部署实际使用的 compose 文件组（主文件 + override）。"],
            )
    api_service = services["api"]
    api_env = service_environment(api_service)
    compose_api_image = str(api_service.get("image") or "")
    if not compose_api_image:
        raise refuse(
            "compose_image",
            "compose 模型里 api 没有 image 引用：这不是镜像模式的部署，adopt 不登记。",
            ["用手工迁移后实际在用的镜像模式 compose 文件组（主文件 + override）。"],
        )
    if api_env.get("LING_UPDATE_ENABLED") == "1":
        raise refuse(
            "update_channel",
            "compose 模型里 api 的 LING_UPDATE_ENABLED=1：更新通道是开着的，"
            "adopt 只登记**默认关**的部署，不会替一个已打开的通道背书。",
            [
                "关掉开关（受限 .env 写 LING_UPDATE_ENABLED=0）后重跑 adopt；"
                "要开着用更新，请走 `ling install --with-updater --enable-updates`。",
            ],
        )

    volume_names: dict[str, str] = {}
    model_volumes = config.get("volumes") or {}
    for key in ("app-data", "db-data"):
        entry = model_volumes.get(key)
        name = entry.get("name") if isinstance(entry, dict) else None
        expected = f"{options.project}_{key}"
        if not name:
            raise refuse(
                "volumes",
                f"compose 解析结果缺少卷 {key} 的实际名称；卷身份解析不出来。",
                ["核对 compose 的 volumes 段；adopt 不猜卷名。"],
            )
        if str(name) != expected:
            raise refuse(
                "volumes",
                f"卷 {key} 的实际名称是 {name!r}，不是 {expected!r}；"
                "status/doctor 只按 <项目>_<卷> 找卷，登记它会造成永久不一致。",
                [
                    "用部署实际使用的项目名（--project）重试；"
                    "或先按安装布局把卷名对齐，再 adopt。",
                ],
            )
        volume_names[key] = str(name)

    # 容器：必须各一个、都在跑、都 healthy；adopt 只登记健康的部署。
    containers: dict[str, dict[str, Any]] = {}
    for service in ("api", "worker", "db"):
        found = _status_containers(runner, options.project, service)
        if len(found) != 1:
            raise refuse(
                f"container_{service}",
                f"{service} 运行中的容器数量是 {len(found)}（期望 1）；"
                "adopt 只登记一套在跑的部署。",
                [
                    "核对 --project 与 compose 文件组；数量不对时不要登记基线。",
                ],
            )
        info = _status_inspect(runner, found[0]["id"])
        if info is None:
            raise refuse(
                f"container_{service}",
                f"{service} 容器 {found[0]['name']} 无法 inspect。",
                ["确认当前账号能读 Docker；adopt 不跳过无法核对的容器。"],
            )
        state_info = info.get("State") or {}
        health = (state_info.get("Health") or {}).get("Status") or "-"
        if state_info.get("Running") is not True:
            raise refuse(
                f"container_{service}",
                f"{service} 容器 {found[0]['name']} 未运行"
                f"（{state_info.get('Status') or '未知'}）。",
                ["先把它跑起来并等健康；adopt 不会替你 up。"],
            )
        if health not in ("healthy", "-"):
            raise refuse(
                f"container_{service}",
                f"{service} 容器 {found[0]['name']} health={health}；"
                "adopt 只登记健康的部署。",
                ["等 health 变成 healthy 再跑 adopt；不要用临时基线掩盖不健康。"],
            )
        containers[service] = {"found": found[0], "info": info}

    api_id = str(containers["api"]["info"].get("Image") or "")
    worker_id = str(containers["worker"]["info"].get("Image") or "")
    if not api_id or not worker_id:
        raise refuse(
            "image_id",
            f"读不到 api/worker 的 image ID（api={api_id!r} worker={worker_id!r}）。",
            ["确认容器由 docker 运行且可 inspect。"],
        )
    if api_id != worker_id:
        raise refuse(
            "api_worker_image",
            f"api 与 worker 不是同一 image ID：api={api_id} worker={worker_id}；"
            "镜像模式要求两者同一 image ID。",
            [
                "先用同一组 compose 文件把 api/worker 重建到同一镜像，再 adopt。",
            ],
        )
    # compose 模型里的镜像引用必须与**运行容器**的引用一致：否则按 compose 记下
    # 的摘要基线一写就与实况对不上（tag 漂移/改了 compose 没重新 up）。
    for service_name in ("api", "worker"):
        container_ref = str(
            (containers[service_name]["info"].get("Config") or {}).get("Image") or ""
        )
        if not container_ref:
            raise refuse(
                f"container_{service_name}",
                f"读不到 {service_name} 容器的镜像引用（Config.Image）；"
                "无法核对 compose 模型与实况。",
            )
        if container_ref != compose_api_image:
            raise refuse(
                "compose_image_drift",
                f"compose 模型里的 api 镜像引用 {compose_api_image!r} 与 "
                f"{service_name} 容器的镜像引用 {container_ref!r} 不一致；"
                "按 compose 记会把摘要基线写错。",
                [
                    "先用同一组 compose 文件重新 up，或修正 compose 的 image，"
                    "再 adopt。",
                ],
            )

    image = _status_image_inspect(runner, api_id)
    if image is None:
        raise refuse(
            "image_inspect",
            f"无法 inspect api 的运行镜像 {api_id}。",
            ["确认镜像还在本地、当前账号能读 Docker。"],
        )
    label_commit = image_commit(image) or ""
    image_env_commit = image_env(image).get("LING_COMMIT", "").strip()
    label_version = image_labels(image).get("org.opencontainers.image.version") or None
    if not label_commit:
        raise refuse(
            "image_commit",
            "运行镜像没有 OCI 标注 org.opencontainers.image.revision；"
            "adopt 不能登记一个无法核对的提交基线。",
            ["用 CI 构建并注入 revision 标注的镜像；不要部署无身份的镜像。"],
        )
    if COMMIT_RE.fullmatch(label_commit) is None:
        raise refuse(
            "image_commit",
            f"镜像标注的提交不是 40 位小写 hex：{label_commit!r}",
        )
    if image_env_commit and image_env_commit != label_commit:
        raise refuse(
            "image_commit",
            f"镜像内 LING_COMMIT={image_env_commit!r} 与 OCI 标注 revision="
            f"{label_commit!r} 不一致。",
            ["镜像本身就不是同一个构建；换正确的镜像后重试。"],
        )

    group = ComposeGroup(options.project, options.compose_files, options.env_file)
    probe, probe_error = container_version_probe(runner, group)
    if probe_error:
        raise refuse(
            "container_probe",
            f"无法从 api 容器内进程取得版本/提交：{probe_error}",
            ["确认运行容器里 `ling` 可 import、LING_COMMIT 已注入；adopt 不猜提交。"],
        )
    probe_commit = str(probe.get("commit") or "").strip()
    probe_version = probe.get("version")
    if not probe_commit:
        raise refuse(
            "container_probe",
            "api 容器内进程没有自报 LING_COMMIT（空值）；"
            "adopt 不能登记一个无法核对的提交基线。",
        )
    if probe_commit != label_commit:
        raise refuse(
            "commit_mismatch",
            f"容器内 LING_COMMIT={probe_commit} 与镜像 OCI 标注 "
            f"revision={label_commit} 不一致。",
            ["运行容器可能不是你以为的那个镜像；不要按成功处理。"],
        )

    # 运行摘要：compose 引用（固定摘要）优先；否则取运行镜像的 ling-api RepoDigest。
    digest_match = DIGEST_RE.fullmatch(compose_api_image)
    ling_suffixes = {
        str(item).rsplit("@sha256:", 1)[1]
        for item in image.get("RepoDigests") or []
        if DIGEST_RE.fullmatch(str(item))
    }
    if len(ling_suffixes) > 1:
        raise refuse(
            "image_digest",
            f"运行镜像对应多个 ling-api 摘要 {sorted(ling_suffixes)}；"
            "无法确定要登记的基线。",
            ["清理多余 tag/摘要，只保留一个 ling-api 摘要后重试。"],
        )
    platform_hex = next(iter(ling_suffixes)) if ling_suffixes else None
    if digest_match is not None:
        requested_digest: str | None = compose_api_image
        index_hex: str | None = digest_match.group(1)
    elif platform_hex is not None:
        requested_digest = f"{IMAGE_REPOSITORY}@sha256:{platform_hex}"
        index_hex = platform_hex
    else:
        requested_digest = None
        index_hex = None

    # 端口：只认 compose 实况；--port 只能用来**核对**，不能用来猜。
    live_port, port_problem = _published_api_port(config)
    if options.port is not None:
        published = {
            int(item["published"])
            for item in service_published_ports(services)
            if item["service"] == "api"
            and str(item["target"]) == str(DEFAULT_PORT)
            and item["protocol"] == "tcp"
        }
        if options.port not in published:
            raise refuse(
                "port",
                f"--port {options.port} 不在 compose 实况发布端口里 {sorted(published)}；"
                "adopt 不按参数猜端口。",
                ["去掉 --port，或修正 compose 的 ports 后重试。"],
            )
        live_port = options.port
    if live_port is None:
        raise refuse(
            "port",
            f"无法从 compose 模型确定 api 的宿主发布端口：{port_problem}",
            ["给出与 compose 实况一致的 --port，或修正 compose 的 ports。"],
        )

    host_ip = api_published_host(config)
    listen_host = host_ip or "0.0.0.0"
    probe_host = "127.0.0.1" if host_ip in ("", "0.0.0.0") else host_ip
    readyz_url = f"http://{probe_host}:{live_port}/readyz"
    code, _body = http_probe(
        f"http://{probe_host}:{live_port}", "/readyz", timeout=options.ready_timeout
    )
    if code != 200:
        raise refuse(
            "readyz",
            f"/readyz 不是恰好 200（{code}）：{readyz_url}；"
            "adopt 只登记能正常服务的部署。",
            [
                "先让服务真正可用（不要用 302/404 的端口冒充当部署）；"
                "3xx 不会被跟随。",
            ],
        )

    volume_records: dict[str, dict[str, Any]] = {}
    mountpoints: list[Path] = []
    for key, name in volume_names.items():
        info = _status_volume_inspect(runner, name)
        if info is None:
            raise refuse(
                f"volume_{key}",
                f"卷 {key}（{name}）不存在：卷身份解析不出来。",
                ["核对 --project 与 compose 的 volumes 段；adopt 不新建卷。"],
            )
        labels = info.get("Labels") or {}
        project_label = labels.get("com.docker.compose.project")
        if info.get("Driver") != "local":
            raise refuse(
                f"volume_{key}",
                f"卷 {name} 的 driver 是 {info.get('Driver')!r}，期望 local。",
            )
        if project_label != options.project:
            raise refuse(
                f"volume_{key}",
                f"卷 {name} 的 Compose 项目标签是 {project_label!r}，"
                f"不是 {options.project!r}；卷身份不一致。",
                ["用部署实际使用的项目名（--project）重试。"],
            )
        volume_records[key] = {
            "name": str(info.get("Name") or name),
            "driver": info.get("Driver"),
            "compose_name": f"{options.project}_{key}",
            "project_label": project_label,
            "created_at": info.get("CreatedAt"),
        }
        mountpoint = info.get("Mountpoint")
        if isinstance(mountpoint, str) and mountpoint:
            mountpoints.append(Path(mountpoint))

    db_result = runner.run(
        [
            *group.argv(),
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "ling",
            "-d",
            "ling",
            "-tAc",
            "select version_num from alembic_version",
        ],
        env=os.environ.copy(),
        timeout=30.0,
    )
    db_head: str | None = None
    if db_result.returncode == 0:
        values = [line.strip() for line in db_result.stdout.splitlines() if line.strip()]
        db_head = values[-1] if values else None
    if db_head is None:
        raise refuse(
            "database",
            "读不到数据库 alembic_version（db 容器/项目名/env 不对，或库未迁移）。",
            ["先确认这套部署的数据库能读；adopt 不记一个空的迁移基线。"],
        )

    head_result = runner.run(
        [
            *group.argv(),
            "exec",
            "-T",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "api",
            "python",
            "-c",
            ALEMBIC_HEAD_SCRIPT,
        ],
        env=os.environ.copy(),
        timeout=60.0,
    )
    image_head: str | None = None
    head_problem = ""
    if head_result.returncode == 0:
        heads, _revisions, _parents = parse_alembic_script_output(head_result.stdout)
        if len(heads) == 1:
            image_head = heads[0]
        else:
            head_problem = f"镜像内 head 不唯一：{heads or '（空）'}"
    else:
        head_problem = tail(head_result.stderr or head_result.stdout) or (
            f"rc={head_result.returncode}"
        )

    backup_dir = adopt_backup_dir(options)
    if backup_dir is None:
        raise refuse(
            "backup_dir",
            "没有备份目录：adopt 不猜它。",
            [
                "用 --backup-dir 指定（与安装器/cron 使用同一目录）。",
                "若 <安装目录>/updater/.env 存在，本命令会自动读它的 LING_BACKUP_DIR。",
            ],
        )

    # 更新器：只登记"配了什么、在不在跑"，绝不开启更新通道。
    updater_env_path = options.install_dir / UPDATER_SUBDIR / UPDATER_ENV_NAME
    updater_values = parse_env_file(updater_env_path)
    configured_updater = str(updater_values.get("LING_UPDATER_IMAGE") or "").strip()
    updater_match = UPDATER_DIGEST_RE.fullmatch(configured_updater)
    if updater_match is None:
        configured_updater = ""
        updater_hex: str | None = None
    else:
        updater_hex = updater_match.group(1)
    # 更新器项目名：adopt 没有安装记录可读，按**应用项目名**派生（与 install
    # 同一个函数；记录落盘后 doctor/status 一律优先读记录里的值）。
    updater_project = updater_project_for(options.project)
    updater_containers = _status_project_containers(runner, updater_project)
    running_updater = [
        item for item in updater_containers if item.get("state", "").startswith("running")
    ]
    updater_live_ref = ""
    if running_updater:
        updater_info = _status_inspect(runner, running_updater[0]["id"])
        if updater_info is not None:
            updater_live_ref = str((updater_info.get("Config") or {}).get("Image") or "")
    live_updater_match = UPDATER_DIGEST_RE.fullmatch(updater_live_ref)
    updater_image = (
        updater_live_ref
        if live_updater_match is not None
        else (configured_updater or None)
    )
    updater_protocol: int | None = None
    updater_status_problem = ""
    updater_status_path = (
        options.install_dir / UPDATER_SUBDIR / UPDATER_STATE_SUBDIR / "status.json"
    )
    try:
        status_payload = json.loads(updater_status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        updater_status_problem = f"{type(exc).__name__}：{updater_status_path}"
    else:
        if isinstance(status_payload, dict) and isinstance(
            status_payload.get("protocol"), int
        ):
            updater_protocol = int(status_payload["protocol"])
        else:
            updater_status_problem = f"没有 protocol 字段：{updater_status_path}"
    updater_started = bool(running_updater)

    raw_test_mode = env_values.get("LING_TEST_MODE") or api_env.get("LING_TEST_MODE", "")
    test_mode = raw_test_mode == "1"
    cookie_secure = api_env.get("LING_COOKIE_SECURE") == "1"
    admin_user = (
        env_values.get("LING_BOOTSTRAP_ADMIN", "").strip()
        or api_env.get("LING_BOOTSTRAP_ADMIN", "").strip()
    )
    reverse_proxy = "https" if cookie_secure else "unknown"

    docker_socket_raw = updater_values.get("LING_DOCKER_SOCKET", "").strip()
    docker_socket = Path(docker_socket_raw) if docker_socket_raw else Path(DEFAULT_DOCKER_SOCKET)
    docker_socket_source = (
        "adopt：更新器 .env" if docker_socket_raw else "默认值（未观察到更新器 .env）"
    )
    raw_docker_gid = updater_values.get("LING_DOCKER_GID", "").strip()
    try:
        docker_gid: int | None = int(raw_docker_gid) if raw_docker_gid else None
    except ValueError:
        docker_gid = None
    docker_gid_source = (
        "adopt：更新器 .env"
        if docker_gid is not None
        else "unknown（未观察到更新器 .env 的 gid）"
    )
    try:
        updater_uid = int(updater_values.get("LING_UPDATER_UID", "") or DEFAULT_UPDATER_UID)
    except ValueError:
        updater_uid = DEFAULT_UPDATER_UID
    try:
        updater_gid = int(updater_values.get("LING_UPDATER_GID", "") or DEFAULT_UPDATER_GID)
    except ValueError:
        updater_gid = DEFAULT_UPDATER_GID
    updater_user_source = (
        "adopt：更新器 .env"
        if updater_values.get("LING_UPDATER_UID") or updater_values.get("LING_UPDATER_GID")
        else "默认值（未观察到更新器 .env 的运行身份）"
    )
    deploy_dir = Path(__file__).resolve().parent.parent
    code_dir_raw = updater_values.get("LING_UPDATER_CODE_DIR", "").strip()
    updater_code_dir = Path(code_dir_raw) if code_dir_raw else deploy_dir
    updater_compose_file = deploy_dir / UPDATER_SUBDIR / UPDATER_COMPOSE_NAME

    state_options = InstallOptions(
        dir=options.install_dir,
        backup_dir=backup_dir,
        project=options.project,
        compose_source=options.compose_files[0],
        digest=requested_digest or "",
        digest_hex=index_hex or "",
        source_commit=label_commit,
        host=listen_host,
        port=live_port,
        reverse_proxy=reverse_proxy,
        admin_user=admin_user or DEFAULT_ADMIN,
        test_mode=test_mode,
        interactive=False,
        ready_timeout=options.ready_timeout,
        db_timeout=DEFAULT_DB_TIMEOUT,
        worker_timeout=DEFAULT_WORKER_TIMEOUT,
        migrate_timeout=DEFAULT_MIGRATE_TIMEOUT,
        min_free_bytes=DEFAULT_MIN_FREE_BYTES,
        updater_digest=updater_image,
        updater_digest_hex=updater_hex,
        with_updater=False,
        updater_code_dir=updater_code_dir,
        updater_compose_file=updater_compose_file,
        docker_socket=docker_socket,
        docker_socket_source=docker_socket_source,
        docker_gid=docker_gid,
        docker_gid_source=docker_gid_source,
        updater_uid=updater_uid,
        updater_gid=updater_gid,
        updater_user_source=updater_user_source,
    )
    # 与 `ling install` **同一个**构造器与写函数：schema 不另造。
    state = new_state(state_options)
    now = utc_now()
    state["status"] = "complete"
    state["started_at"] = "unknown"
    state["finished_at"] = "unknown"
    state["exit_code"] = None
    state["updated_at"] = now
    state["adopted"] = True
    state["adopted_at"] = now
    state["installer"]["version"] = "unknown"
    state["layout"]["dir"] = str(options.install_dir)
    state["layout"]["env_file"] = str(options.env_file)
    state["layout"]["state_file"] = str(options.state_path)
    state["layout"]["logs_dir"] = str(options.install_dir / LOG_SUBDIR)
    state["layout"]["compose"].update(
        {
            "files": [str(path) for path in options.compose_files],
            "source": str(options.compose_files[0]),
            "source_sha256": sha256_file(options.compose_files[0]),
            "sha256": sha256_file(options.compose_files[0]),
            "files_group_sha256": sha256_bytes(
                "\n".join(str(path) for path in options.compose_files).encode("utf-8")
            ),
            "modifications": [],
            "env_file_sha256": sha256_file(options.env_file),
        }
    )
    lock_path = options.install_dir / INSTALL_LOCK_NAME
    # install 在抢锁时记 locks.install；adopt 不抢锁，但把**同一路径**记下来
    # （它可能在，也可能不在——不在时下面会如实标注）。
    state["locks"] = {"install": str(lock_path)}
    if lock_path.is_file():
        lock_info = lock_path.stat()
        state["layout"].update(
            {
                "install_lock": str(lock_path),
                "install_lock_mode": f"{stat.S_IMODE(lock_info.st_mode):04o}",
                "install_lock_gid": lock_info.st_gid,
                "install_lock_share": "unknown（adopt 未参与当初的权限协商）",
            }
        )
    state["layout"]["updater"]["note"] = (
        "adopt 现场登记：更新器挂载按安装布局推导；是否在跑见 updater.started；"
        "网页更新开关见 update.enabled（adopt 只登记，默认关）"
    )
    state["listen"].update(
        {
            "host": listen_host,
            "port": live_port,
            "url": f"http://{listen_host}:{live_port}",
            "reverse_proxy": reverse_proxy,
            "cookie_secure": cookie_secure,
        }
    )
    state["admin"] = {
        "username": admin_user or "unknown",
        "password_source": "unknown",
        "password_shown_once": "unknown",
        "password_in_restricted_env": (
            str(options.env_file)
            if "LING_BOOTSTRAP_ADMIN_PASSWORD" in env_values
            else "unknown"
        ),
    }
    state["test_mode"] = test_mode
    state["image"].update(
        {
            "requested_digest": requested_digest,
            "index_digest_hex": index_hex,
            "platform_digest": platform_hex,
            "repository": IMAGE_REPOSITORY,
            "image_id": api_id,
            "architecture": image.get("Architecture"),
            "os": image.get("Os"),
            "source_commit": label_commit,
            "source_commit_source": "image+container",
            "version": label_version,
            "repo_digests": [str(item) for item in image.get("RepoDigests") or []],
        }
    )
    state["database"].update(
        {
            "image": services["db"].get("image"),
            "target_head": image_head,
            "alembic_version": db_head,
        }
    )
    state["volumes"] = volume_records
    state["containers"] = {}
    for service_name, entry in containers.items():
        info = entry["info"]
        found = entry["found"]
        record: dict[str, Any] = {
            "container_id": found["id"],
            "container_name": found["name"],
            "image_id": str(info.get("Image") or ""),
            "image_ref": str((info.get("Config") or {}).get("Image") or "") or None,
            "health": ((info.get("State") or {}).get("Health") or {}).get("Status"),
        }
        if service_name != "db":
            record["repo_digests"] = [
                str(item) for item in image.get("RepoDigests") or []
            ]
            record["commit"] = label_commit
        state["containers"][service_name] = record
    state["migration"].update(
        {
            "target_head": image_head,
            "attempted": None,
            "executed_marker": None,
            "skip_marker": None,
            "exit_code": None,
            "log": None,
            "current_before": None,
            "current_after": db_head,
        }
    )
    backup = backup_status(backup_dir, mountpoints)
    state["backup"].update(
        {
            "dir": str(backup_dir),
            "container_visible": False,
            "latest": backup.get("latest"),
            "restore_point_ready": bool(backup.get("restore_point_ready")),
            "requirements": backup.get("requirements") or {},
            "free_bytes": backup.get("free_bytes"),
            "reason": backup.get("reason"),
        }
    )
    state["update"] = {
        "enabled": False,
        "protocol": UPDATER_PROTOCOL,
        "reason": (
            "adopt 只登记**默认关**的部署：登记的这份 compose 解析出 "
            "LING_UPDATE_ENABLED=0（开关要打开请走 "
            "`ling install --with-updater --enable-updates`）"
        ),
    }
    state["updater"] = {
        "project": updater_project,
        "image": updater_image,
        "image_configured": bool(updater_image),
        "protocol": updater_protocol if updater_protocol is not None else UPDATER_PROTOCOL,
        "requested": updater_started,
        "started": updater_started,
        "started_at": "unknown" if updater_started else None,
        "ownership": {},
        "env_file_sha256": (
            sha256_file(updater_env_path) if updater_env_path.is_file() else None
        ),
        "reason": (
            "adopt：更新器容器在运行（网页更新开关仍是 0，见 update.enabled）"
            if updater_started
            else (
                "adopt：更新器镜像已配置但未在运行"
                if updater_image
                else "adopt：未观察到更新器镜像/容器（网页更新不可用）"
            )
        ),
    }
    state["checks"]["compose"] = {
        "project": config.get("name") or options.project,
        "image": api_service.get("image"),
        "ports": api_service.get("ports") or [],
        "volumes": volume_names,
        "database_image": services["db"].get("image"),
    }
    state["result"] = {
        "readyz_http": code,
        "version": probe_version if isinstance(probe_version, str) else None,
        "version_commit": probe_commit,
    }
    state["failure"] = None
    state["next_actions"] = [
        "`ling doctor`：只读核对这份基线（不应再出现“安装记录里没有基线”）。",
        "`ling backup`：立刻建立第一份恢复点，并做一次 `ling restore-drill`"
        "（隔离库，不碰生产库）。",
        "adopted 记录里标为 unknown 的项无法从实况推断：不要把它们当成已核对。",
    ]

    unverified: list[dict[str, str]] = [
        {
            "field": "started_at",
            "reason": "adopt 无法知道这套部署当初是什么时候安装/迁移的",
        },
        {
            "field": "finished_at",
            "reason": "adopt 没有当初那次安装的完成时间",
        },
        {
            "field": "exit_code",
            "reason": "adopt 没有当初那次安装的退出码",
        },
        {
            "field": "installer.version",
            "reason": "adopt 不知道当初是哪个安装器版本写的记录（这份记录由 adopt 写）",
        },
        {
            "field": "image.build_run",
            "reason": "镜像由哪个 CI run / 谁构建：镜像标注只有源提交，没有 run 号",
        },
        {
            "field": "migration.attempted",
            "reason": "当初的迁移是哪个 run / 谁执行的：没有迁移日志",
        },
        {
            "field": "migration.executed_marker",
            "reason": "没有当初的迁移日志，无法证明迁移真的执行过",
        },
        {
            "field": "migration.skip_marker",
            "reason": "没有当初的迁移日志，无法证明迁移没有被跳过",
        },
        {
            "field": "migration.log",
            "reason": "adopt 没有当初的迁移日志可引用",
        },
        {
            "field": "migration.current_before",
            "reason": "adopt 只看得到现在的 alembic_version",
        },
        {
            "field": "admin.password_source",
            "reason": "初始口令是生成还是外部提供：受限 .env 里只有口令本身",
        },
        {
            "field": "admin.password_shown_once",
            "reason": "当初是否在终端显示过初始口令：没有记录",
        },
        {
            "field": "backup.cron_owner",
            "reason": "备份 cron 是谁装的、何时装的：记录不含 cron 事实（doctor 现查 crontab）",
        },
    ]
    if reverse_proxy == "unknown":
        unverified.append(
            {
                "field": "listen.reverse_proxy",
                "reason": "当初是否已有反向代理：只能从 LING_COOKIE_SECURE 推出 TLS，推不出代理拓扑",
            }
        )
    if requested_digest is None:
        unverified.append(
            {
                "field": "image.requested_digest",
                "reason": "compose 引用不是固定摘要，运行镜像也没有 ling-api 的 RepoDigests",
            }
        )
    elif DIGEST_RE.fullmatch(compose_api_image) is None:
        unverified.append(
            {
                "field": "image.requested_digest",
                "reason": (
                    f"compose 引用不是固定摘要（{compose_api_image or '空'}）；"
                    "摘要基线取自运行镜像的 RepoDigests"
                ),
            }
        )
    if platform_hex is None:
        unverified.append(
            {
                "field": "image.platform_digest",
                "reason": "运行镜像没有 ling-api 的 RepoDigests，无法登记平台摘要",
            }
        )
    if image_head is None:
        unverified.append(
            {
                "field": "database.target_head",
                "reason": f"读不到镜像内 Alembic head：{head_problem}",
            }
        )
    if raw_test_mode == "":
        unverified.append(
            {
                "field": "test_mode",
                "reason": "受限 .env 与 compose 模型里都没有 LING_TEST_MODE",
            }
        )
    if updater_started and updater_protocol is None:
        unverified.append(
            {
                "field": "updater.protocol",
                "reason": f"更新器在跑但读不到协议版本：{updater_status_problem}",
            }
        )
    if updater_started and configured_updater and updater_live_ref != configured_updater:
        unverified.append(
            {
                "field": "updater.image",
                "reason": (
                    f"运行中的更新器镜像 {updater_live_ref or '（未知）'} ≠ "
                    f"更新器 .env 配置 {configured_updater}；按实况登记"
                ),
            }
        )
    state["unverified"] = unverified

    say("")
    say("采集（只读；不跑 up/pull，不改 compose/容器）：")
    for service_name in ("api", "worker", "db"):
        entry = containers[service_name]
        state_info = entry["info"].get("State") or {}
        health = (state_info.get("Health") or {}).get("Status") or "-"
        say(
            f"  {service_name:8s} {entry['found']['name']}"
            f"（running，health={health}，image ID {entry['info'].get('Image')}）"
        )
    say(f"  运行镜像   {api_id}")
    say(
        "  镜像摘要   "
        + (requested_digest or "unknown（compose 未固定摘要，且镜像无 ling-api RepoDigest）")
        + (
            f"；平台摘要 sha256:{platform_hex}"
            if platform_hex and platform_hex != index_hex
            else ""
        )
    )
    say(f"  源提交     {label_commit}（OCI 标注 = 容器内 LING_COMMIT）")
    say(f"  版本       {probe_version or label_version or 'unknown'}")
    say(f"  宿主端口   {live_port}（来自 docker compose config；绑定 {listen_host}）")
    say(f"  /readyz    {code}（{readyz_url}）")
    for key in ("app-data", "db-data"):
        record = volume_records[key]
        say(
            f"  卷 {key:9s} {record['name']}"
            f"（driver={record['driver']}，项目={record['project_label']}）"
        )
    say(
        f"  alembic    {db_head}"
        + (f"（镜像内 head {image_head}）" if image_head else f"（镜像内 head 读不到：{head_problem}）")
    )
    say(f"  备份目录   {backup_dir}")
    say(
        "  更新器     "
        + (
            f"在运行（{running_updater[0]['name']}，镜像 {updater_live_ref or '未知'}）"
            if updater_started
            else "未在运行（是否该在跑见 update.enabled）"
        )
    )

    if options.state_path.exists():
        backup_path = backup_existing_state(options.state_path)
        say("")
        say(
            f"旧记录已先备份：{backup_path}"
            f"（0600；sha256 {sha256_file(backup_path)}，已与旧文件逐字节核对）"
        )
    write_state_file(options.state_path, state)
    written = options.state_path.stat()
    if stat.S_IMODE(written.st_mode) & 0o077:
        raise LingError(
            EXIT_LAYOUT,
            "failed_layout",
            "state",
            f"写出的安装记录权限过宽：{options.state_path}"
            f"（mode {stat.S_IMODE(written.st_mode):04o}）",
        )

    say("")
    say("登记结果（与 `ling install` 同一 schema；额外记 adopted/adopted_at）：")
    say(f"  安装记录   {options.state_path}（0600）")
    say(f"  sha256     {sha256_file(options.state_path)}")
    say(f"  adopted    {now}")
    say(
        f"  unknown    {len(unverified)} 项无法从实况推断（status/doctor 会按 unknown 显示，"
        "不会算 OK）"
    )
    say(
        "  更新通道   update.enabled=0（本命令只登记默认关的部署，"
        "绝不写 LING_UPDATE_ENABLED=1）"
    )
    say("")
    say("下一步：")
    say("  * `ling doctor`：只读核对这份基线（不应再出现“安装记录里没有基线”）。")
    say("  * `ling backup`：立刻建立第一份恢复点；随后 `ling restore-drill` 做隔离演练。")
    say("  * adopted 记录里 started_at / migration.* 等标为 unknown：不要把它们当成已核对。")
    return EXIT_OK


# ---------------------------------------------------------------- backup / restore-drill


def _count_media_refs(runner: Runner, options: StatusOptions) -> int | None:
    """从运行中的 db 读 ``media_evidence.media_ref`` 条数（只读）。"""
    group = ComposeGroup(options.project, options.compose_files, options.env_file)
    result = runner.run(
        [
            *group.argv(),
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "ling",
            "-d",
            "ling",
            "-tAc",
            "select count(*) from media_evidence where media_ref is not null",
        ],
        env=os.environ.copy(),
        timeout=30.0,
    )
    if result.returncode != 0:
        return None
    values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not values or not values[-1].isdigit():
        return None
    return int(values[-1])


def _run_script(
    argv: Sequence[str], env: dict[str, str], *, timeout: float, what: str
) -> int:
    """执行备份/演练脚本并**原样**透传退出码；超时返回 124。"""
    try:
        proc = subprocess.run(list(argv), env=env, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise LingError(EXIT_USAGE, "failed_usage", "script", f"无法执行{what}：{exc}") from exc
    except OSError as exc:
        raise LingError(EXIT_USAGE, "failed_usage", "script", f"无法执行{what}：{exc}") from exc
    except subprocess.TimeoutExpired:
        print("")
        print(f"{what}超时（{timeout:g}s）：已中断；请人工核对目录里的半成品与锁。")
        return 124
    return proc.returncode


def backup_command(args: argparse.Namespace) -> int:
    options, state = load_status_options(args, dir_field="install_dir")
    state = _require_ops_layout(options, state, "backup")
    timeout = float(getattr(args, "timeout", DEFAULT_BACKUP_TIMEOUT))
    if timeout <= 0:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"--timeout 必须 > 0（拿到 {timeout}）"
        )
    script = _deploy_script(BACKUP_SCRIPT_NAME, purpose="备份")
    recorded_dir = str(((state or {}).get("backup") or {}).get("dir") or "")
    raw_dir = (args.dir or "").strip() or recorded_dir
    if not raw_dir:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "backup_dir",
            "没有备份目录：安装记录里没有 backup.dir，也没有给 --dir",
            ["用 --dir 指定备份目录（与安装器/cron 使用同一目录，便于恢复演练对齐）。"],
        )
    backup_dir = Path(raw_dir).expanduser().resolve()
    # compose 插值时**宿主环境变量优先于 .env**；备份脚本也按同一把密钥记
    # keys_env_set，所以这里按同一优先级取值（都只传给子进程，不回显）。
    host_root_key = os.environ.get("LING_ROOT_KEY", "").strip()
    env_root_key = parse_env_file(options.env_file).get("LING_ROOT_KEY", "")
    root_key = host_root_key or env_root_key
    env = ops_env(options, root_key=root_key or None)
    print("ling backup：复用 deploy/backup/backup.sh（同一组 compose 文件 / --env-file / 项目名）")
    print(f"  备份目录    {backup_dir}")
    print(f"  项目名      {options.project}")
    print(f"  文件组      {', '.join(str(path) for path in options.compose_files)}")
    print(f"  --env-file  {options.env_file}")
    if host_root_key:
        key_source = "宿主环境变量 LING_ROOT_KEY（优先于 .env；只传给备份脚本，不回显）"
    elif env_root_key:
        key_source = "受限 .env 的 LING_ROOT_KEY（只传给备份脚本，不回显）"
    else:
        key_source = "宿主环境与受限 .env 里都没有 LING_ROOT_KEY"
    print(f"  根密钥      {key_source}")
    print("")
    code = _run_script([str(script), str(backup_dir)], env, timeout=timeout, what="备份")
    print("")
    if code != 0:
        print(f"备份失败：backup.sh 返回 {code}（退出码表见 deploy/backup/backup.sh 头部）。")
        print("      未声称备份可用；不要根据“目录里有文件”判断成功。")
        return code

    manifest = latest_manifest(backup_dir)
    if manifest is None:
        print("备份失败：脚本返回 0，但备份目录里没有 manifest-*.txt（无法证明完整性）。")
        return EXIT_STATUS_MISMATCH
    point = inspect_recovery_point(backup_dir, manifest, verify_hashes=True)
    problems = list(point.problems)
    if (point.fields.get("media_state") or "") == "absent":
        # media=absent 只有在库里确实没有 media_ref 时才不算缺陷。
        refs = _count_media_refs(Runner(Logger()), options)
        if refs is None:
            problems.append("media_state=absent，且无法确认库里没有 media_ref（查询失败）")
        elif refs > 0:
            problems.append(
                f"media_state=absent，但库里有 {refs} 条 media_ref（恢复后引用全部悬空）"
            )

    print("备份产物校验")
    print("-" * 40)
    for key in (
        "created_at",
        "dump",
        "dump_sha256",
        "tombstones_state",
        "media_state",
        "keys_root",
        "keys_root_sha256",
        "keys_env_set",
    ):
        print(f"  {key:16s} {point.fields.get(key, '（缺失）')}")
    print("")
    if problems:
        print(f"备份产物不完整（{len(problems)} 项）：")
        for problem in problems:
            print(f"  - {problem}")
        print("结论：这份备份**不能**当作可恢复的恢复点（`ling backup` 非 0）。")
        return EXIT_STATUS_MISMATCH
    print("结论：备份完成，manifest 关键字段与产物哈希一致。")
    print("      下一步：`ling restore-drill` 做一次隔离恢复演练（不要在生产库上试）。")
    return EXIT_OK


def restore_drill_command(args: argparse.Namespace) -> int:
    options, state = load_status_options(args, dir_field="install_dir")
    state = _require_ops_layout(options, state, "restore-drill")
    timeout = float(getattr(args, "timeout", DEFAULT_BACKUP_TIMEOUT))
    if timeout <= 0:
        raise LingError(
            EXIT_USAGE, "failed_usage", "arguments", f"--timeout 必须 > 0（拿到 {timeout}）"
        )
    script = _deploy_script(RESTORE_DRILL_SCRIPT_NAME, purpose="恢复演练")
    recorded_dir = str(((state or {}).get("backup") or {}).get("dir") or "")
    raw_dir = (args.dir or "").strip() or recorded_dir
    backup_dir = Path(raw_dir).expanduser().resolve() if raw_dir else None

    raw_dump = (args.dump or "").strip()
    if raw_dump:
        dump = Path(raw_dump).expanduser().resolve()
    else:
        if backup_dir is None:
            raise LingError(
                EXIT_USAGE,
                "failed_usage",
                "backup_dir",
                "没有备份目录：安装记录里没有 backup.dir，也没有给 --dir/--dump",
            )
        manifest = latest_manifest(backup_dir)
        if manifest is None:
            raise LingError(
                EXIT_USAGE,
                "failed_usage",
                "dump",
                f"备份目录里没有 manifest-*.txt：{backup_dir}",
                ["先运行 `ling backup` 产生一份恢复点，再演练。"],
            )
        dump_name = read_manifest(manifest).get("dump") or ""
        if not dump_name or Path(dump_name).name != dump_name:
            raise LingError(
                EXIT_USAGE,
                "failed_usage",
                "dump",
                f"最近一份 manifest 的 dump 字段不可用：{dump_name!r}",
            )
        dump = backup_dir / dump_name
    if not dump.is_file():
        raise LingError(
            EXIT_USAGE, "failed_usage", "dump", f"备份 dump 不存在：{dump}"
        )
    if DUMP_NAME_RE.match(dump.name) is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "dump",
            f"备份文件名必须是 ling-<时间戳>.dump：{dump.name}",
            ["restore_drill.sh 从文件名推导 manifest/墓碑路径；不要手工改名。"],
        )

    db_name = (args.db or "").strip()
    if not db_name:
        db_name = re.sub(r"[^a-z0-9_]", "_", options.project.lower()) + DRILL_DB_SUFFIX
    if not db_name.endswith(DRILL_DB_SUFFIX):
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "db",
            f"隔离库名必须以 {DRILL_DB_SUFFIX} 结尾：{db_name}",
        )
    if re.fullmatch(r"[A-Za-z0-9_]+", db_name) is None:
        raise LingError(
            EXIT_USAGE,
            "failed_usage",
            "db",
            f"隔离库名只允许字母/数字/下划线：{db_name}",
        )

    env = ops_env(options)
    print("ling restore-drill：复用 deploy/backup/restore_drill.sh（与 backup 相同的文件组）")
    print(f"  备份        {dump}")
    print(f"  隔离库      {db_name}（绝不碰生产库）")
    print(f"  项目名      {options.project}")
    print(f"  文件组      {', '.join(str(path) for path in options.compose_files)}")
    print(f"  --env-file  {options.env_file}")
    print("")
    code = _run_script(
        [str(script), str(dump), db_name], env, timeout=timeout, what="恢复演练"
    )
    print("")
    if code != 0:
        print(f"恢复演练失败：restore_drill.sh 返回 {code}；这份备份不能按“可恢复”对待。")
        print("      查看上面的失败原因（墓碑/媒体/关键表）；不要用生产库重试。")
        return code
    print(f"结论：恢复演练通过（隔离库 {db_name}；墓碑一致；媒体覆盖已核对）。")
    print("      演练不改变生产库；备份不会因此自动恢复上线。")
    return EXIT_OK


# ---------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        if not args.command:
            parser.print_help()
            return EXIT_USAGE
        if args.command == "install":
            options = resolve_install_options(args)
            return Installer(options).run()
        if args.command == "adopt":
            return adopt_command(args)
        if args.command == "status":
            return status_command(args)
        if args.command == "logs":
            return logs_command(args)
        if args.command == "doctor":
            return doctor_command(args)
        if args.command == "backup":
            return backup_command(args)
        return restore_drill_command(args)
    except LingError as exc:
        print(f"[ling] {exc.message}", file=sys.stderr)
        for action in exc.next_actions:
            print(f"[ling] 下一步：{action}", file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
