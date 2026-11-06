#!/usr/bin/env python3
"""ling 独立更新器（容器内常驻进程，纯标准库）。

设计依据
========
- ``docs/EASY-INSTALL-UPDATE-DESIGN.md`` §4（更新器与信任边界）、§5（一次网页
  更新的实际顺序）、§6（故障时给用户什么结果）。
- ``docs/IMAGE-DEPLOYMENT-DESIGN.md`` §4（镜像模式的运行配置）。

它做什么
========
轮询请求目录 → **自己重新校验** → 抢跨工具安装锁 → 维护门禁 → 复用
``deploy/release/publish.py`` 的发布门禁（预检/拉取/身份/备份/迁移）→ **把目标
摘要真正写回宿主**的受限 ``.env`` → 在容器网络内验证新 API → 验证通过后才发布
宿主端口 → 写**脱敏**状态文件。

它不做什么
==========
- 不信任请求里的任何 tag/URL/compose 片段/路径/源提交：请求只有固定字段，多
  一个就拒绝；目标是 digest 还是 latest 由更新器自己判断。
- 不跑目标镜像里的部署脚本，也不跑本仓库以外的发布实现：发布逻辑只从
  ``--publish-py``（发行包里那份只读 copy）加载。
- 不提供网页 UI（下一批）；不自动升级更新器自己；不自动降级数据库。
- 已迁移后失败**绝不**切回旧镜像、**绝不**恢复旧 dump（设计 §6）。

请求协议
========
请求方（API）写临时文件再 ``rename`` 成 ``<requests-dir>/request-<request_id>.json``
（同目录 rename 才原子）。更新器只读这几个固定字段，多一个都拒绝::

    {
      "schema": 1,
      "request_id": "<32 位小写 hex>",            # 不可重复的任务 ID
      "target_digest": "ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>",
      "expected_current_digest": "ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>",
      "expected_current_commit": "<40 位小写 hex>",
      "operator_confirmed": true,                  # 必须是 JSON true，不是 "true"
      "created_at": "2026-11-04T00:00:00Z"         # 可选
    }

状态协议
========
``<status-file>``（默认 ``<state-dir>/status.json``，原子写、0644）字段固定，
**不含**密码、原始日志、备份路径、compose/env 路径::

    {
      "schema": 1, "protocol": 1, "updated_at": "...",
      "updater": {"state": "idle|busy|needs_manual", "version": "1.0.0"},
      "task": null | {
        "request_id": "...", "state": "...", "stage": "prepare|backup|migrate|switch|verify",
        "started_at": "...", "updated_at": "...", "finished_at": ...,
        "target": {"digest": "...", "commit": "..."},
        "previous": {"digest": "...", "commit": "..."},
        "migration": {"attempted": bool, "current_before": ..., "current_after": ...,
                      "target_head": ...},
        "backup": {"id": "...", "complete": bool, "tombstones_state": ...,
                   "media_state": ..., "keys_root": ..., "keys_env_set": ...},
        "maintenance": {"state": "active|released|unknown", "verified": bool},
        "rollback": {"attempted": bool, "verified": bool},
        "still_on_old_version": true|false|null,     # null = 未实测，不敢断言
        "failure": null | {"code": "...", "stage": "...", "message": "码表文案"},
        "warnings": ["..."], "next_actions": ["码表文案"]
      },
      "backup_gate": {
        "restore_point": bool,        # 现在是否**真有**一个可用的恢复点
        "backup_id": "..." | null,    # manifest 文件名里的时间戳，不含路径
        "complete": bool,             # manifest 自身结构完整、产物在位
        "tombstones_state": ..., "media_state": ...,
        "keys_root": ..., "keys_env_set": ...,
        "reason": "ok|no_manifest|backup_dir_unreadable|manifest_unreadable|...",
        "checked_at": "..."
      },
      "release_catalog": {
        "state": "ok|missing|invalid",
        "reason": null | "catalog_missing|catalog_unreadable|catalog_invalid|
                 catalog_schema_unsupported|catalog_tool_missing",
        "entries": 3,                       # 目录里的发布条数（只用于展示）
        "current_version": "0.0.1" | null   # 按**当前镜像摘要**在目录里反查到的版本
      },
      "available_targets": [
        {"version": "0.0.2", "digest": "...", "commit": "...",
         "has_migration": true, "compatible": true|false,
         "incompatible_reason": null | "not_in_min_from|current_version_unknown|..."}
      ],
      "recommended_target": null | {"version": "...", "digest": "...",
                                    "commit": "...", "has_migration": ...}
    }

恢复点门禁为什么必须是**独立顶层字段**
======================================
``task`` 只在更新器**处理过一次任务之后**才存在，而第一次任务又要先过恢复点
门禁：只看 ``task.backup`` 就是死锁——全新镜像部署的网页更新会永远显示"不可
安装"。所以 ``backup_gate`` 与任务无关，直接**扫描备份目录**里最近的 manifest
（cron/手动备份在备份根目录，更新器自己的快照在 ``release-<id>/`` 里，两者都看）。

它只是**廉价的静态核对**（字段齐全、产物在位、状态允许恢复），不重算哈希：
真正的哈希/身份门禁在复用实现里，每次更新都会把 manifest 与产物逐字节核一遍。
``checked_at`` 记录这份判断的时间；更新器**启动时算一次、每处理完一个任务再算
一次**并写进状态文件，idle 时也在（否则 API 又只能看 task）。

``reason`` 是码表码，不是文案：``backup_dir_unreadable`` / ``no_manifest`` /
``manifest_unreadable`` / ``manifest_incomplete`` / ``tombstones_not_ok`` /
``media_partial`` / ``keys_missing`` / ``ok``。API 按自己那份码表翻译，绝不
回显状态文件里的自由文本。

私有文件（0600，只在状态目录里，不返回网页）：``task.json``（持久任务：任务
ID/阶段/前后 digest/备份标识/维护门禁副本）、``journal.log``（含命令 argv 与
原始失败原因的私有日志）、``release-<id>.json``（复用 publish 的发布记录）。

可信发布目录（``deploy/releases.json``）
========================================
「更新」按钮需要一个**可用的目标**，而 ``target_digest`` 不能由调用方随口给。
设计原文：可信发布目录只列已通过门禁、可从已知版本升级的稳定目标；同时明确
「可编辑的 Release 正文**不是**摘要可信来源」。CI 构建证明在 Stage 1 被关掉
（``provenance: false``），给更新器塞 ``gh``/Sigstore 又与「只保留最小可信代码」
冲突——所以**信任锚是操作员装的那份代码**：目录文件随代码提交（GitHub 签名提交
+ 评审），更新器从**自己的发行包目录**只读加载：``--release-catalog``（默认
``<--publish-py 的上级目录>/../releases.json``，容器里就是 ``/srv/ling-code/
releases.json``），校验与排序复用发行包里那份 ``deploy/release/catalog.py``。

判定规则（都在 :func:`release_targets` 里，**不依赖文件顺序**）：

1. **当前版本**由 ``受限 env`` 里的 ``LING_APP_IMAGE`` 在目录里**反查**得到
   （目录就是"版本 ↔ 摘要"的可信映射）。反查不到 → 当前版本未知，所有目标
   ``compatible=false`` + ``incompatible_reason="current_version_unknown"``，
   ``recommended_target=null``（**不猜**当前版本，也不拿"看起来像"的目标放行）。
2. **只允许升级**：目标 ``version`` 必须**严格大于**当前版本，否则该条**不出现在**
   ``available_targets`` 里（设计要求「不提供随意选旧版本或一键降级」）。
3. **版本跨度**：当前版本必须在该目标的 ``min_from`` 里；不在 → 兼容=false +
   原因（``not_in_min_from``）。
4. ``recommended_target`` = **版本最大的那一条兼容目标**；没有兼容目标就是 null。

目录缺失 / 不可解析 / 校验失败 → ``available_targets=[]`` + 明确原因（``state``
与 ``reason``），**绝不**假装有目标，也**绝不**静默。

即使目标来自目录，更新器仍**独立复核**：请求里的 ``target_digest`` 必须是自己
目录里的**兼容目标**之一（否则 ``target_not_in_catalog``，一个字节都不改），
之后照旧核 digest 格式、拉取后核 ``LING_COMMIT == OCI revision``、平台、当前
运行 digest，并复用 publish 的全部门禁。

维护门禁（设计 §4/§5 的硬要求）
===============================
「维护门禁必须由更新器在应用生命周期之外控制，并在新 API 启动后仍能强制阻断
公开端口及机器人输入」。这里的做法：

1. 维护期先停 api 与 worker（沿用 publish 的**先停写再备份**顺序：捕获取数镜像 →
   停 api → 停 worker → 确认两者进入 exited/created → 一致备份），公开端口和
   机器人入站随之消失；备份读 `app-data` 时 api 已不在，backup.sh 改用一次性只读
   容器（`docker run --rm --network none --read-only -v <卷>:/data:ro`），镜像用
   停之前捕获的运行中 api image ID（`LING_BACKUP_APP_IMAGE`）；
2. 启动**目标** API 时不用正式 compose，而是用更新器生成的**临时副本**：把每个
   服务里的 ``ports:`` 都改成 ``ports: []``，因此新 API 只存在于容器网络里；
3. 用 ``docker compose config --format json`` 证明临时副本与正式副本「除宿主
   端口外完全一致」，再用 ``compose exec`` 在**容器内**访问
   ``127.0.0.1:6520/readyz``、让应用自报版本/提交（``python -c``，**不**打任何
   需要鉴权的 HTTP 端点）并核对 Web 清单；
4. 全部验证（含 worker 健康、alembic head、api/worker 同一镜像）通过后，才用
   正式 compose ``up -d --force-recreate api`` 重新发布宿主端口，并核对宿主
   ``/readyz`` 200、容器内进程自报的提交、容器实际镜像；
5. 第 4 步失败（已迁移）→ 不切旧镜像，但把维护门禁**重新关上**（用临时副本
   重启 api），状态写「更新未完成，需要维护处理」。

为什么用「临时副本 + ``ports: []``」而不是 ``!reset []``
---------------------------------------------------------
Compose 的 ``ports`` 是「按唯一键合并」的序列：override 里写空列表**不会**清掉
主文件里的条目（那是 append/unique 合并语义），``!reset`` 需要 Compose 2.24+
而本仓库没有声明版本下限。生成整份副本既不受版本限制，也能用 ``config`` 断言
「除端口外模型没变」，比相信一条 override 更强。

失败语义（设计 §6）
===================
- 拉取/预检/备份失败且**没有迁移**：可以恢复旧 api/worker，但只有**实测**
  旧服务正常（旧 image ID 未变 + 宿主端口占用者是旧 api + ``/readyz`` 200）才写
  ``still_on_old_version: true``；否则写 ``null``（未实测）。
- **已迁移后失败**：不切旧镜像、不恢复旧 dump，状态 ``needs_manual``，维护门禁
  保持/重新关上。
- 更新器重启/状态未知：按 ``task.json`` 里的任务 ID 与容器/DB 实况对账；能确认
  没迁移才恢复并实测旧服务；不能确认就停在人工门禁，**不新建第二次任务**，也不
  接受新请求（``--acknowledge-manual`` 后才恢复）。

两把锁
======
1. **单飞**（更新器自己）：同一时刻只处理一个任务：``<state-dir>/updater.lock``
   的 ``flock`` 包住「读任务、认领请求、处理」整段；请求文件用 ``rename`` 认领
   （并发认领只有一个成功），处理完归档到 ``<requests-dir>/archive/``，同一个
   ``request_id`` 不会被处理两次。
2. **跨工具安装锁**（与别人）：复用 ``publish.InstallLock``，路径口径不变——
   ``<受限 env 的 realpath 所在目录>/.ling-install.lock``，容器里就是安装器预建
   并单文件挂载的 ``/srv/ling/.ling-install.lock``。它与 ``install.sh`` /
   ``ling install`` / 手动 ``publish.py`` / 宿主更新执行器是同一把锁；从处理请求
   前一直持有到验证结束，拿不到就**立即** ``needs_manual``（``install_lock_busy``），
   不等待、不跳过、不动任何容器。

受限 env 的写入（缺陷①的修法）
==============================
``.env`` 是**单文件 bind mount**，所以容器里那个路径就是挂载点：既不能在它的
目录里建临时文件（只读根，EROFS），也不能把 ``/tmp`` 的临时文件 rename 过来
（跨设备 EXDEV / rename 挂载点 EBUSY）。于是写入改为「原地截断重写 + 私有副本
回滚」，见 :func:`write_env_in_place`；原子性由「写前备份 + 读回校验 + 失败
回滚 + 全程持锁（没有别的工具在同时读它）」保证。**不再**用 ``LING_APP_IMAGE``
覆盖插值绕过：那会让宿主 ``.env`` 永远停在旧摘要，下一次 ``docker compose up``
就把服务退回旧镜像。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

# ---------------------------------------------------------------- 协议常量

UPDATER_VERSION = "1.0.0"
PROTOCOL = 1
REQUEST_SCHEMA = 1
STATUS_SCHEMA = 1
TASK_SCHEMA = 1

REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
REQUEST_KEYS = frozenset(
    {
        "schema",
        "request_id",
        "target_digest",
        "expected_current_digest",
        "expected_current_commit",
        "operator_confirmed",
        "created_at",
    }
)
REQUEST_REQUIRED = (
    "schema",
    "request_id",
    "target_digest",
    "expected_current_digest",
    "expected_current_commit",
    "operator_confirmed",
)

STAGES = ("prepare", "backup", "migrate", "switch", "verify")

TASK_RUNNING = "running"
TASK_SUCCEEDED = "succeeded"
TASK_FAILED_NOT_STARTED = "failed_not_started"
TASK_NEEDS_MANUAL = "needs_manual"
TASK_RESOLVED = "resolved"

MAINT_ACTIVE = "active"
MAINT_RELEASED = "released"
MAINT_UNKNOWN = "unknown"

# 可信发布目录（见模块 docstring「可信发布目录」一节的硬规则）。
RELEASE_CATALOG_NAME = "releases.json"
# 联网取目录的超时（秒）。更新器是常驻轮询进程，一次卡死会拖住整个通道。
CATALOG_FETCH_TIMEOUT_SECONDS = 10.0
RELEASE_CATALOG_TOOL = "catalog.py"
# 读受限 env 时的大小上限：只为一个 KEY= 行，不需要把任意大的文件读进内存。
ENV_FILE_MAX_BYTES = 64 * 1024
CATALOG_STATE_OK = "ok"
CATALOG_STATE_MISSING = "missing"
CATALOG_STATE_INVALID = "invalid"
# 状态文件里只放**固定码**：文案由 API 自己那份码表翻译（同失败码表的做法）。
CATALOG_REASON_MISSING = "catalog_missing"
CATALOG_REASON_UNREADABLE = "catalog_unreadable"
CATALOG_REASON_INVALID = "catalog_invalid"
CATALOG_REASON_SCHEMA = "catalog_schema_unsupported"
CATALOG_REASON_TOOL_MISSING = "catalog_tool_missing"
# 目录工具（deploy/release/catalog.py）的固定错误码 → 状态文件里的 reason。
# 未列出的码一律 `catalog_invalid`：宁可说"目录不可信"，也不要漏一种畸形输入。
CATALOG_REASON_BY_CODE = {
    "not_found": CATALOG_REASON_MISSING,
    "not_a_file": CATALOG_REASON_UNREADABLE,
    "unreadable": CATALOG_REASON_UNREADABLE,
    "too_large": CATALOG_REASON_UNREADABLE,
    "unsafe_permissions": CATALOG_REASON_UNREADABLE,
    "schema_unsupported": CATALOG_REASON_SCHEMA,
}
# 目标不可用的原因（API 按自己那份码表翻译）。注意这里**没有** not_newer：
# 版本不高于当前的目标根本不出现在 available_targets 里（只允许升级），而不是
# 出现在列表里标成"不兼容"——否则页面会给出一个可以直接点降级的目标。
INCOMPATIBLE_NOT_IN_MIN_FROM = "not_in_min_from"
INCOMPATIBLE_CURRENT_VERSION_UNKNOWN = "current_version_unknown"
INCOMPATIBLE_VERSION_UNCOMPARABLE = "version_uncomparable"

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_BUSY = 3
EXIT_PREFLIGHT = 10
EXIT_BACKUP = 13
EXIT_PULL = 14
EXIT_MIGRATE = 15
EXIT_POST_MIGRATION = 16
EXIT_NEEDS_MANUAL = 17

# 状态文件里只允许出现码表文案；原始 message 只进私有 journal.log。
FAILURE_TEXT = {
    "request_rejected": "请求不符合固定格式，未执行任何操作",
    "already_on_target": "请求的目标摘要与当前运行摘要相同，未执行任何操作",
    "duplicate_request": "该任务 ID 已处理过，不重复执行",
    "expected_current_mismatch": "当前运行 digest/提交与请求的期望当前值不一致，未执行任何操作",
    "preflight_failed": "预检未通过，未做任何变更",
    "maintenance_gate_failed": "维护门禁的临时 compose 未通过校验，未做任何变更",
    "pull_failed": "拉取镜像或镜像身份复核失败，未进入迁移",
    "backup_failed": "一致备份未通过校验，未执行迁移",
    "migration_failed": "迁移失败或迁移后版本不匹配，未启动新 API",
    "switch_failed": "已迁移：目标版本启动或验证失败，更新未完成，需要维护处理",
    "old_service_unverified": "未迁移，但未能实测旧服务正常，停在人工门禁",
    "reconcile_needs_manual": "更新器重启后无法确认状态，停在人工门禁",
    "state_unreadable": "任务状态文件无法读取，停在人工门禁",
    "previous_task_needs_manual": "上一个任务需要人工处理，暂不接受新任务",
    "install_lock_busy": "另一个安装/发布/更新任务正持有安装锁，本次未执行任何操作",
    "env_write_failed": "受限 env 写入失败或未指向目标摘要，需要维护处理",
    "internal_error": "更新器内部错误，已记录私有日志，请人工核对",
}

NEXT_ACTION_TEXT = {
    "check_old_service": "人工确认旧服务（容器、宿主端口、/readyz）是否正常",
    "inspect_maintenance": "维护门禁仍在：公开端口保持关闭，按任务 ID 人工核对后再恢复",
    "review_private_log": "查看更新器状态目录里的私有日志与发布记录，再决定重试或人工恢复",
    "acknowledge_manual": "人工处理完成后用 --acknowledge-manual <request_id> 解除门禁",
    "no_action_needed": "无需操作",
}

WARNING_TEXT = {
    "docker_free_unverified": "更新器容器看不到宿主 Docker 根目录，镜像侧磁盘余量未核对",
    "maintenance_reenforced": "已迁移后验证失败：维护门禁已重新关上（未切回旧镜像）",
}

# 恢复点门禁的 reason 是**码表码**（文案由 API 自己那份码表翻译，见
# src/ling/api/routes/version.py 的 IMAGE_BACKUP_GATE_REASONS）。
# 码表：ok / backup_dir_unreadable / no_manifest / manifest_unreadable /
#       manifest_incomplete / tombstones_not_ok / media_partial / keys_missing
#
# manifest 文件名的固定前缀/后缀（backup.sh 写 `manifest-<stamp>.txt`）。
BACKUP_MANIFEST_PREFIX = "manifest-"
BACKUP_MANIFEST_SUFFIX = ".txt"
BACKUP_MANIFEST_GLOB = f"{BACKUP_MANIFEST_PREFIX}*{BACKUP_MANIFEST_SUFFIX}"
# backup_id 只允许这些字符：它来自文件名，会被写进状态并可能显示在网页上。
BACKUP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BACKUP_GATE_TOMBSTONE_STATES = frozenset({"ok", "FAILED"})
BACKUP_GATE_MEDIA_STATES = frozenset({"present", "partial", "absent"})
BACKUP_GATE_KEYS_ROOT_STATES = frozenset({"present", "absent"})
BACKUP_GATE_KEYS_ENV_STATES = frozenset({"true", "false"})

MAINTENANCE_ROOT_NAME = "maintenance"

# 更新器自己的 HTTP 探活同样只允许**免鉴权**端点（见
# ``deploy/release/publish.py`` 的 ``HTTP_PROBE_PATHS`` 与那里的说明：发布/更新
# 流程不持有用户凭据）。更新器复用 ``publish.Context`` 的探活实现，这里再声明
# 一份是为了让 ``tests/integration/test_version_image_update.py`` 能直接核对
# 本文件——两边不一致（其中一边自造了 HTTP 探活）应当立刻测试红。
#
# 曾经这里有一条 ``VERSION_PROBE``：容器内 urllib 打 ``/v1/version``。该端点挂
# ``CurrentAuth``，未鉴权恒 401，真机演练 P5 直接卡在
# ``failed_after_migration / version_container``。现在改成容器内进程证据
# （``publish.CONTAINER_VERSION_PROBE``），不再有任何 HTTP 探活版本。
HTTP_PROBE_PATHS: tuple[str, ...] = ("/readyz",)


class UpdaterError(Exception):
    """更新器自己的拒绝/配置错误（还没进入复用实现的门禁流程）。"""

    def __init__(
        self,
        exit_code: int,
        code: str,
        stage: str,
        detail: str,
        next_actions: Sequence[str] = (),
    ) -> None:
        super().__init__(detail)
        self.exit_code = exit_code
        self.code = code
        self.stage = stage
        self.detail = detail
        self.next_actions = list(next_actions)


# ---------------------------------------------------------------- 复用发布实现


def load_publish(path: Path) -> ModuleType:
    """按**文件路径**加载发行包里的 ``publish.py``（不复用 site-packages）。"""
    if not path.is_file():
        raise UpdaterError(
            EXIT_REJECTED,
            "publish_module_missing",
            "bootstrap",
            f"找不到发布模块：{path}",
            ["把 deploy/release/publish.py 以只读方式挂进更新器容器。"],
        )
    spec = importlib.util.spec_from_file_location("ling_release_publish", path)
    if spec is None or spec.loader is None:
        raise UpdaterError(
            EXIT_REJECTED,
            "publish_module_unloadable",
            "bootstrap",
            f"无法为发布模块建立加载器：{path}",
        )
    module = importlib.util.module_from_spec(spec)
    # 必须先登记到 sys.modules：publish.py 用了 ``from __future__ import
    # annotations`` 的 dataclass，dataclasses 会通过 cls.__module__ 反查模块
    # 字典解析字符串注解；未登记时 exec_module 直接 AttributeError。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 加载失败必须显式报错，不能静默降级
        raise UpdaterError(
            EXIT_REJECTED,
            "publish_module_unloadable",
            "bootstrap",
            f"加载发布模块失败（{path}）：{type(exc).__name__}: {exc}",
        ) from exc
    required = (
        "Context",
        "Options",
        "PublishError",
        "DIGEST_RE",
        "COMMIT_RE",
        "EXIT_USAGE",
        "EXIT_PREFLIGHT",
        "EXIT_PULL",
        "EXIT_BACKUP",
        "EXIT_MIGRATE",
        "EXIT_POST_MIGRATION",
        "DEFAULT_COMMAND_TIMEOUT",
        "ARCH_ALIASES",
        "write_atomic",
        "utc_now",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise UpdaterError(
            EXIT_REJECTED,
            "publish_module_incompatible",
            "bootstrap",
            f"发布模块缺少更新器依赖的属性：{missing}",
            ["更新器与发行包必须来自同一次受信任构建。"],
        )
    return module


def load_catalog_tool(path: Path) -> ModuleType:
    """按**文件路径**加载发行包里的 ``catalog.py``（发布目录的校验/排序工具）。

    与 :func:`load_publish` 同一个做法、同一个理由：更新器用的必须是**操作员装的
    那份代码**（信任锚），不是 site-packages 里的同名模块，也不是自己另写一套
    校验——两套规则迟早会不一致，而不一致的那一份就是后门。

    加载失败**不**在启动时退出：目录不可用只影响"有没有可用目标"，不该让更新器
    连状态都不写（网页会因此看不到任何解释）。这里返回 None，由
    :func:`read_release_catalog` 记 ``catalog_tool_missing``。
    """
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("ling_release_catalog", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # 必须先登记 sys.modules（同 load_publish：dataclass 注解解析需要模块字典）。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - 加载失败按"目录不可用"处理，不静默降级成放行
        sys.modules.pop(spec.name, None)
        return None
    required = ("SCHEMA", "CatalogError", "load_catalog", "version_key")
    if any(not hasattr(module, name) for name in required):
        return None
    return module


# ---------------------------------------------------------------- 维护门禁产物


def _skip_value_block(lines: Sequence[str], start: int, indent: int) -> int:
    """跳过 ``start`` 行之后属于同一个键的缩进块，返回下一个同级键的行号。"""
    cursor = start + 1
    while cursor < len(lines):
        raw = lines[cursor]
        if not raw.strip():
            cursor += 1
            continue
        if len(raw) - len(raw.lstrip(" ")) > indent:
            cursor += 1
            continue
        break
    return cursor


def strip_service_ports(text: str) -> str:
    """把 compose 里**服务级**的 ``ports:`` 全部改成 ``ports: []``。

    只动 ``services:`` 直接子项（服务名下一层）的 ``ports`` 键；注释、锚点、
    环境变量、卷、网络与其它键逐字符保留。改不了的结构（多行 flow 序列）一律
    抛错而不猜：调用方还会用 ``docker compose config`` 复核「除端口外没变」。
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_services = False
    service_indent: int | None = None
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("- "):
            out.append(raw)
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, _, rest = stripped.partition(":")
        key = key.strip()
        if indent == 0 and key == "services":
            in_services = True
            service_indent = None
            out.append(raw)
            index += 1
            continue
        if in_services:
            if indent == 0:
                # 顶层下一个键：services 段结束（``ports`` 不会出现在这里）。
                in_services = False
                service_indent = None
                out.append(raw)
                index += 1
                continue
            if service_indent is None:
                service_indent = indent
            if indent == service_indent or key != "ports":
                out.append(raw)
                index += 1
                continue
            value = rest.split("#", 1)[0].strip()
            if value and value != "[]" and not (
                value.startswith("[") and value.endswith("]")
            ):
                raise UpdaterError(
                    EXIT_PREFLIGHT,
                    "maintenance_gate_failed",
                    "prepare",
                    "compose 里的服务 ports 是多行 flow 序列，更新器不猜它的边界",
                    ["把 compose 的 ports 写成块序列或单行 [] 后重试。"],
                )
            out.append(" " * indent + "ports: []\n")
            index = _skip_value_block(lines, index, indent)
            continue
        out.append(raw)
        index += 1
    return "".join(out)


def _ports_published(service: Any) -> bool:
    return isinstance(service, dict) and bool(service.get("ports"))


def compare_compose_models(real: dict[str, Any], maintenance: dict[str, Any]) -> None:
    """证明临时副本与正式副本「除宿主端口外完全一致」，且端口确实为空。"""
    real_services = real.get("services")
    maint_services = maintenance.get("services")
    if not isinstance(real_services, dict) or not isinstance(maint_services, dict):
        raise UpdaterError(
            EXIT_PREFLIGHT,
            "maintenance_gate_failed",
            "prepare",
            "维护门禁：compose config 里没有 services",
        )
    if set(real_services) != set(maint_services):
        raise UpdaterError(
            EXIT_PREFLIGHT,
            "maintenance_gate_failed",
            "prepare",
            "维护门禁：临时副本的服务集合与正式副本不同",
        )
    for name, service in real_services.items():
        stripped_real = {k: v for k, v in service.items() if k != "ports"}
        stripped_maint = {k: v for k, v in maint_services[name].items() if k != "ports"}
        if stripped_real != stripped_maint:
            raise UpdaterError(
                EXIT_PREFLIGHT,
                "maintenance_gate_failed",
                "prepare",
                f"维护门禁：临时副本里 {name} 服务除端口外与原模型不一致",
                ["检查 compose 是否含相对路径（build/env_file/bind）：更新器不搬动它们。"],
            )
    leaked = sorted(name for name, svc in maint_services.items() if _ports_published(svc))
    if leaked:
        raise UpdaterError(
            EXIT_PREFLIGHT,
            "maintenance_gate_failed",
            "prepare",
            f"维护门禁：临时副本仍在发布宿主端口（{', '.join(leaked)}）",
        )
    if not any(_ports_published(svc) for svc in real_services.values()):
        raise UpdaterError(
            EXIT_PREFLIGHT,
            "maintenance_gate_failed",
            "prepare",
            "维护门禁：正式 compose 里没有可阻断的宿主端口，门禁无法证明有效",
        )
    for key in set(real) | set(maintenance):
        if key == "services":
            continue
        if real.get(key) != maintenance.get(key):
            raise UpdaterError(
                EXIT_PREFLIGHT,
                "maintenance_gate_failed",
                "prepare",
                f"维护门禁：临时副本的顶层 {key} 与正式副本不一致",
            )


class MaintenanceGate:
    """生成并校验「不含宿主端口」的临时 compose 副本（维护门禁的物理形态）。"""

    def __init__(
        self,
        publish_module: ModuleType,
        state_dir: Path,
        request_id: str,
        sources: Sequence[Path],
    ) -> None:
        self.pub = publish_module
        self.root = state_dir / MAINTENANCE_ROOT_NAME / request_id
        self.sources = list(sources)
        self.files: list[Path] = []

    def render(self) -> list[Path]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        files: list[Path] = []
        for index, source in enumerate(self.sources):
            text = source.read_text(encoding="utf-8")
            try:
                rendered = strip_service_ports(text)
            except UpdaterError as error:
                raise self.pub.PublishError(
                    self.pub.EXIT_PREFLIGHT,
                    "failed_preflight",
                    "maintenance_gate",
                    f"维护门禁：{error.detail}",
                    error.next_actions,
                ) from error
            destination = self.root / f"{index}-{source.name}"
            self.pub.write_atomic(destination, rendered.encode("utf-8"), 0o600)
            files.append(destination)
        self.files = files
        return files

    def verify(self, context: Any) -> dict[str, Any]:
        """用 ``docker compose config`` 对照正式模型；不通过就拒绝继续。"""
        maintenance = context.compose_config_for(
            self.files,
            context.o.target,
            step="preflight",
            exit_code=self.pub.EXIT_PREFLIGHT,
            status="failed_preflight",
        )
        try:
            compare_compose_models(context.config, maintenance)
        except UpdaterError as error:
            raise self.pub.PublishError(
                self.pub.EXIT_PREFLIGHT,
                "failed_preflight",
                "maintenance_gate",
                error.detail,
                error.next_actions,
            ) from error
        return maintenance


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True)
class Config:
    requests_dir: Path
    state_dir: Path
    status_file: Path
    compose_file: Path
    compose_override: Path | None
    env_file: Path
    backup_dir: Path
    backup_script: Path
    publish_py: Path
    # 可信发布目录的数据文件与它的**校验工具**分开：
    # - 代码永远来自发行包（``<publish-py 所在目录>/catalog.py``，信任锚）；
    # - 数据路径可由 ``--release-catalog`` 覆盖（默认 ``<publish-py>/../../
    #   releases.json``，容器里就是 /srv/ling-code/releases.json），
    #   这样离线演练/测试可以用一份自己的目录而不动发行包。
    release_catalog: Path
    catalog_tool: Path
    project: str
    poll_seconds: float
    ready_timeout: float
    health_timeout: float
    migrate_timeout: float
    backup_timeout: float
    once: bool
    acknowledge: str
    # 可选的联网目录地址（``--catalog-url`` / ``LING_UPDATER_CATALOG_URL``）。
    #
    # ## 为什么需要它
    #
    # 目录文件是随发行包交付、只读挂载的，而升级流程只换 app 镜像、不动更新器
    # 自己。于是宿主上那份目录永远停在安装时的版本：升到 0.0.3 之后里面没有
    # 0.0.4，下次发版网页更新照旧「暂无可用的更新目标」——一键更新只能成功
    # **一次**，之后每次都得人工换更新器镜像。
    #
    # 打开这个地址后，更新器先从它取目录（仍然交给发行包的 ``catalog.py``
    # 做同一套严格校验），取不到再退回本地那份。
    #
    # ## 为什么默认留空
    #
    # 打开它等于把「谁能改 GitHub 上的仓库」当成升级授权依据。更新器是个无凭据
    # 的容器，它没法验证"这份内容是 owner 批准的"，只能验证 HTTPS 连到了 GitHub。
    # 这是运维要显式做的信任决定，不能因为一次代码更新就让已部署实例突然出网。
    #
    # 放在最后是因为 dataclass 要求：有默认值的字段不能排在无默认值的字段前面。
    catalog_url: str | None = None


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="updater.py",
        description="ling 独立更新器：按固定请求轮询、自己复核、维护门禁、复用发布门禁",
    )
    parser.add_argument(
        "--requests-dir",
        default=_env("LING_UPDATER_REQUESTS_DIR"),
        help="请求目录（API 可写；请求方写临时文件再 rename）",
    )
    parser.add_argument(
        "--state-dir",
        default=_env("LING_UPDATER_STATE_DIR"),
        help="更新器状态目录（持久任务/私有日志/维护门禁副本）",
    )
    parser.add_argument(
        "--status-file",
        default=_env("LING_UPDATER_STATUS_FILE"),
        help="脱敏状态文件（默认 <state-dir>/status.json）",
    )
    parser.add_argument(
        "--compose-file",
        default=_env("LING_COMPOSE_FILE"),
        help="应用 compose 主文件（安装器生成的稳定副本）",
    )
    parser.add_argument(
        "--compose-override",
        default=_env("LING_COMPOSE_OVERRIDE"),
        help="可选 override（必须与主文件同一组 -f，主文件在前）",
    )
    parser.add_argument(
        "--env-file",
        default=_env("LING_COMPOSE_ENV_FILE"),
        help=(
            "应用受限 env（**单文件 rw 挂载**：更新成功后必须把 "
            "LING_APP_IMAGE 真正写回宿主；同目录还有跨工具安装锁）"
        ),
    )
    parser.add_argument(
        "--project",
        default=_env("LING_COMPOSE_PROJECT", "ling"),
        help="应用 Compose 项目名（默认 ling）",
    )
    parser.add_argument(
        "--backup-dir",
        default=_env("LING_BACKUP_DIR"),
        help="备份根目录（与 cron 同一个；更新器在其中创建本次快照）",
    )
    parser.add_argument(
        "--backup-script",
        default=_env("LING_UPDATER_BACKUP_SCRIPT"),
        help="复用 deploy/backup/backup.sh（不重写备份实现）",
    )
    parser.add_argument(
        "--publish-py",
        default=_env("LING_UPDATER_PUBLISH_PY"),
        help="发行包里的 deploy/release/publish.py（只读挂载）",
    )
    parser.add_argument(
        "--catalog-url",
        default=_env("LING_UPDATER_CATALOG_URL"),
        help=(
            "可选的联网目录地址（https）。配了它就先从该地址取 releases.json，"
            "取不到再退回本地那份。留空（默认）= 只读本地，行为与从前完全一样。"
            "示例：https://raw.githubusercontent.com/qsbb/ling/main/deploy/releases.json"
        ),
    )
    parser.add_argument(
        "--release-catalog",
        default=_env("LING_UPDATER_RELEASE_CATALOG"),
        help=(
            "可信发布目录数据文件（默认 <publish-py 的上级目录>/../releases.json，"
            "容器里就是 /srv/ling-code/releases.json）；校验工具固定从 "
            "<publish-py 所在目录>/catalog.py 加载"
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(_env("LING_UPDATER_POLL_SECONDS", "5")),
        help="轮询间隔秒数（默认 5）",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=float(_env("LING_RELEASE_READY_TIMEOUT_SECONDS", "180")),
        help="等待宿主 /readyz 的秒数（默认 180）",
    )
    parser.add_argument(
        "--health-timeout",
        type=float,
        default=float(_env("LING_RELEASE_HEALTH_TIMEOUT_SECONDS", "90")),
        help="等待 worker healthy 的秒数（默认 90）",
    )
    parser.add_argument(
        "--migrate-timeout",
        type=float,
        default=float(_env("LING_RELEASE_MIGRATE_TIMEOUT_SECONDS", "600")),
        help="一次性迁移命令超时秒数（默认 600）",
    )
    parser.add_argument(
        "--backup-timeout",
        type=float,
        default=float(_env("LING_RELEASE_BACKUP_TIMEOUT_SECONDS", "3600")),
        help="backup.sh 超时秒数（默认 3600）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="最多处理一个任务（或一次对账）后退出；便于人工执行与测试",
    )
    parser.add_argument(
        "--acknowledge-manual",
        default="",
        help="人工处理完 needs_manual 任务后，用它的 request_id 解除门禁",
    )
    return parser


def parse_config(argv: Sequence[str] | None) -> Config:
    args = build_parser().parse_args(argv)
    missing = [
        name
        for name, value in (
            ("--requests-dir", args.requests_dir),
            ("--state-dir", args.state_dir),
            ("--compose-file", args.compose_file),
            ("--env-file", args.env_file),
            ("--backup-dir", args.backup_dir),
            ("--backup-script", args.backup_script),
            ("--publish-py", args.publish_py),
        )
        if not value
    ]
    if missing:
        raise UpdaterError(
            EXIT_REJECTED,
            "missing_arguments",
            "bootstrap",
            "缺少必填路径：" + "、".join(missing),
            ["更新器容器的挂载点与这些路径必须一一对应（见 deploy/updater/compose.yaml）。"],
        )
    for name, value in (
        ("--poll-seconds", args.poll_seconds),
        ("--ready-timeout", args.ready_timeout),
        ("--health-timeout", args.health_timeout),
        ("--migrate-timeout", args.migrate_timeout),
        ("--backup-timeout", args.backup_timeout),
    ):
        if value <= 0:
            raise UpdaterError(
                EXIT_REJECTED, "invalid_arguments", "bootstrap", f"{name} 必须 > 0（{value}）"
            )
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project):
        raise UpdaterError(
            EXIT_REJECTED,
            "invalid_arguments",
            "bootstrap",
            f"项目名不合法：{args.project!r}",
        )
    state_dir = Path(args.state_dir).expanduser().resolve()
    override_raw = args.compose_override.strip()
    publish_py = Path(args.publish_py).expanduser().resolve()
    catalog_raw = args.release_catalog.strip()
    return Config(
        requests_dir=Path(args.requests_dir).expanduser().resolve(),
        state_dir=state_dir,
        status_file=(
            Path(args.status_file).expanduser().resolve()
            if args.status_file.strip()
            else state_dir / "status.json"
        ),
        compose_file=Path(args.compose_file).expanduser().resolve(),
        compose_override=Path(override_raw).expanduser().resolve() if override_raw else None,
        env_file=Path(args.env_file).expanduser().resolve(),
        backup_dir=Path(args.backup_dir).expanduser().resolve(),
        backup_script=Path(args.backup_script).expanduser().resolve(),
        publish_py=publish_py,
        # 默认：发行包 deploy/ 目录下的 releases.json（容器里 /srv/ling-code/releases.json）。
        release_catalog=(
            Path(catalog_raw).expanduser().resolve()
            if catalog_raw
            else publish_py.parent.parent / RELEASE_CATALOG_NAME
        ),
        catalog_tool=publish_py.parent / RELEASE_CATALOG_TOOL,
        catalog_url=args.catalog_url.strip() or None,
        project=args.project,
        poll_seconds=args.poll_seconds,
        ready_timeout=args.ready_timeout,
        health_timeout=args.health_timeout,
        migrate_timeout=args.migrate_timeout,
        backup_timeout=args.backup_timeout,
        once=args.once,
        acknowledge=args.acknowledge_manual.strip(),
    )


def ensure_layout(cfg: Config) -> None:
    """状态/请求/归档目录必须存在且只对属主开放（API 只拿到请求目录的写权限）。"""
    for path in (cfg.state_dir, cfg.requests_dir):
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise UpdaterError(
                EXIT_REJECTED,
                "layout_unavailable",
                "bootstrap",
                f"无法创建目录 {path}：{exc}",
            ) from exc
        if not path.is_dir():
            raise UpdaterError(
                EXIT_REJECTED, "layout_unavailable", "bootstrap", f"不是目录：{path}"
            )
    try:
        (cfg.requests_dir / "archive").mkdir(exist_ok=True, mode=0o700)
    except OSError as exc:
        raise UpdaterError(
            EXIT_REJECTED,
            "layout_unavailable",
            "bootstrap",
            f"无法创建归档目录：{exc}",
        ) from exc


# ---------------------------------------------------------------- 协议：请求/任务/状态


def request_id_from_name(name: str) -> str | None:
    for prefix in ("request-", "claimed-"):
        if name.startswith(prefix) and name.endswith(".json"):
            candidate = name[len(prefix) : -len(".json")]
            return candidate if REQUEST_ID_RE.fullmatch(candidate) else None
    return None


def load_request(path: Path) -> dict[str, Any]:
    """固定格式请求；多一个字段、少一个字段、类型不对都拒绝。"""
    name_id = request_id_from_name(path.name)
    if name_id is None:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            f"请求文件名不合约定：{path.name}",
            ["请求方必须写成 request-<32 位小写 hex>.json（先写临时文件再 rename）。"],
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            f"请求文件不可读或不是 JSON：{type(exc).__name__}",
        ) from exc
    if not isinstance(data, dict):
        raise UpdaterError(
            EXIT_REJECTED, "request_rejected", "prepare", "请求主体必须是 JSON 对象"
        )
    unknown = sorted(set(data) - REQUEST_KEYS)
    if unknown:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            f"请求含未定义字段：{unknown}",
            ["请求只允许固定字段：目标/期望当前 digest、期望当前提交、任务 ID、确认标记。"],
        )
    missing = [key for key in REQUEST_REQUIRED if key not in data]
    if missing:
        raise UpdaterError(
            EXIT_REJECTED, "request_rejected", "prepare", f"请求缺字段：{missing}"
        )
    if data.get("schema") != REQUEST_SCHEMA:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            f"不支持的请求 schema：{data.get('schema')!r}（只接受 {REQUEST_SCHEMA}）",
        )
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or REQUEST_ID_RE.fullmatch(request_id) is None:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            "request_id 必须是 32 位小写 hex",
        )
    if request_id != name_id:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            "request_id 与文件名不一致",
        )
    for key in ("target_digest", "expected_current_digest", "expected_current_commit"):
        if not isinstance(data.get(key), str):
            raise UpdaterError(
                EXIT_REJECTED, "request_rejected", "prepare", f"{key} 必须是字符串"
            )
    if data.get("operator_confirmed") is not True:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            "operator_confirmed 必须是 JSON true",
            ["网页必须在所有者二次确认后才写请求；更新器不接受缺省/字符串形式。"],
        )
    return data


def validate_request_identity(pub: ModuleType, request: dict[str, Any]) -> tuple[str, str, str]:
    """只用**格式**判断，不读任何 tag/Release 正文；返回 (target, target_hex, commit)。"""
    target = request["target_digest"]
    match = pub.DIGEST_RE.fullmatch(target)
    if match is None:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            "拒绝非 digest 目标：只接受 ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>"
            "（latest、可变 tag、其他 registry、带额外路径、非 64 位 hex 一律拒绝）",
            ["从 CI 的不可变记录里取 index digest，不要用 latest 或 tag 代替。"],
        )
    expected = request["expected_current_digest"]
    if pub.DIGEST_RE.fullmatch(expected) is None:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            "expected_current_digest 必须是 ghcr.io/qsbb/ling-api@sha256:<64 位小写 hex>",
        )
    commit = request["expected_current_commit"]
    if pub.COMMIT_RE.fullmatch(commit) is None:
        raise UpdaterError(
            EXIT_REJECTED,
            "request_rejected",
            "prepare",
            "expected_current_commit 必须是 40 位小写 hex 的完整提交号",
        )
    if target == expected:
        raise UpdaterError(
            EXIT_REJECTED,
            "already_on_target",
            "prepare",
            "目标摘要与期望当前摘要相同，拒绝创建空任务",
        )
    return target, match.group(1), commit


def new_task(
    pub: ModuleType, request: dict[str, Any], target_hex: str, expected_commit: str
) -> dict[str, Any]:
    request_id = request["request_id"]
    now = pub.utc_now()
    return {
        "schema": TASK_SCHEMA,
        "protocol": PROTOCOL,
        "request_id": request_id,
        "state": TASK_RUNNING,
        "stage": "prepare",
        "started_at": now,
        "updated_at": now,
        "finished_at": None,
        "target": {
            "digest": request["target_digest"],
            "digest_hex": target_hex,
            "commit": None,
            "image_id": None,
        },
        "previous": {
            "digest": request["expected_current_digest"],
            "commit": expected_commit,
            "head": None,
            "services": {},
        },
        "migration": {
            "attempted": False,
            "current_before": None,
            "current_after": None,
            "target_head": None,
        },
        "backup": {
            "id": f"backup-{request_id[:12]}",
            "complete": False,
            "tombstones_state": None,
            "media_state": None,
            "keys_root": None,
            "keys_env_set": None,
        },
        "maintenance": {"state": MAINT_RELEASED, "verified": False, "files": []},
        "rollback": {"attempted": False, "verified": False},
        "failure": None,
        "still_on_old_version": None,
        "warnings": [],
        "next_actions": [],
    }


class TaskStore:
    def __init__(self, cfg: Config, pub: ModuleType) -> None:
        self.cfg = cfg
        self.pub = pub
        self.path = cfg.state_dir / "task.json"

    def read(self) -> dict[str, Any] | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise UpdaterError(
                EXIT_NEEDS_MANUAL,
                "state_unreadable",
                "reconcile",
                f"任务状态文件不可读：{type(exc).__name__}",
            ) from exc
        try:
            task = json.loads(raw)
        except ValueError as exc:
            raise UpdaterError(
                EXIT_NEEDS_MANUAL,
                "state_unreadable",
                "reconcile",
                "任务状态文件不是 JSON",
            ) from exc
        if not isinstance(task, dict) or task.get("schema") != TASK_SCHEMA:
            raise UpdaterError(
                EXIT_NEEDS_MANUAL,
                "state_unreadable",
                "reconcile",
                "任务状态文件 schema 不支持",
            )
        if not isinstance(task.get("request_id"), str):
            raise UpdaterError(
                EXIT_NEEDS_MANUAL, "state_unreadable", "reconcile", "任务状态缺少 request_id"
            )
        return task

    def write(self, task: dict[str, Any]) -> None:
        task["updated_at"] = self.pub.utc_now()
        payload = json.dumps(task, ensure_ascii=False, indent=2, sort_keys=True)
        self.pub.write_atomic(self.path, (payload + "\n").encode("utf-8"), 0o600)


def task_view(task: dict[str, Any] | None) -> dict[str, Any] | None:
    """状态文件里的任务视图：**白名单**逐字段复制，绝不整份转述任务文件。"""
    if task is None:
        return None
    backup = task.get("backup") or {}
    migration = task.get("migration") or {}
    maintenance = task.get("maintenance") or {}
    rollback = task.get("rollback") or {}
    failure = task.get("failure")
    return {
        "request_id": task.get("request_id"),
        "state": task.get("state"),
        "stage": task.get("stage"),
        "started_at": task.get("started_at"),
        "updated_at": task.get("updated_at"),
        "finished_at": task.get("finished_at"),
        "target": {
            "digest": (task.get("target") or {}).get("digest"),
            "commit": (task.get("target") or {}).get("commit"),
        },
        "previous": {
            "digest": (task.get("previous") or {}).get("digest"),
            "commit": (task.get("previous") or {}).get("commit"),
        },
        "migration": {
            "attempted": bool(migration.get("attempted")),
            "current_before": migration.get("current_before"),
            "current_after": migration.get("current_after"),
            "target_head": migration.get("target_head"),
        },
        "backup": {
            "id": backup.get("id"),
            "complete": bool(backup.get("complete")),
            "tombstones_state": backup.get("tombstones_state"),
            "media_state": backup.get("media_state"),
            "keys_root": backup.get("keys_root"),
            "keys_env_set": backup.get("keys_env_set"),
        },
        "maintenance": {
            "state": maintenance.get("state"),
            "verified": bool(maintenance.get("verified")),
        },
        "rollback": {
            "attempted": bool(rollback.get("attempted")),
            "verified": bool(rollback.get("verified")),
        },
        "still_on_old_version": task.get("still_on_old_version"),
        "failure": None
        if not failure
        else {
            "code": failure.get("code"),
            "stage": failure.get("stage"),
            "message": FAILURE_TEXT.get(
                str(failure.get("code")), "更新失败（原因见私有日志）"
            ),
        },
        "warnings": [
            WARNING_TEXT.get(str(code), str(code)) for code in task.get("warnings") or []
        ],
        "next_actions": [
            NEXT_ACTION_TEXT.get(str(code), str(code))
            for code in task.get("next_actions") or []
        ],
    }


def _empty_backup_gate(pub: ModuleType, reason: str) -> dict[str, Any]:
    return {
        "restore_point": False,
        "backup_id": None,
        "complete": False,
        "tombstones_state": None,
        "media_state": None,
        "keys_root": None,
        "keys_env_set": None,
        "reason": reason,
        "checked_at": pub.utc_now(),
    }


def backup_manifests(backup_dir: Path) -> tuple[list[Path], bool]:
    """备份目录里的 manifest：根目录（cron/手动）与**一层**子目录（更新器快照）。

    返回 ``(manifests, readable)``。目录不可读时 ``readable=False``——调用方据此
    写"备份目录不可读"的码表原因，而不是假装"没有备份可用"（两者处置不同）。
    """
    try:
        entries = list(backup_dir.iterdir())
    except OSError:
        return [], False
    manifests: list[Path] = []
    for entry in entries:
        try:
            if entry.is_file() and entry.name.startswith(
                BACKUP_MANIFEST_PREFIX
            ) and entry.name.endswith(BACKUP_MANIFEST_SUFFIX):
                manifests.append(entry)
            elif entry.is_dir() and not entry.is_symlink():
                # 更新器自己的快照目录 `release-<id>/` 里有 manifest；**不**跟随
                # 符号链接目录（备份目录是可写的，别把扫描变成任意路径读取）。
                manifests.extend(entry.glob(BACKUP_MANIFEST_GLOB))
        except OSError:
            continue
    return manifests, True


def _manifest_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _latest_manifest(backup_dir: Path) -> tuple[Path | None, str | None]:
    manifests, readable = backup_manifests(backup_dir)
    if not readable:
        return None, "backup_dir_unreadable"
    if not manifests:
        return None, "no_manifest"
    # mtime 相同按文件名兜底，保证同一目录的结论稳定（不随 iterdir 顺序抖动）。
    return max(manifests, key=lambda path: (_manifest_mtime(path), path.name)), None


def _backup_id(manifest: Path) -> str | None:
    name = manifest.name
    if not name.startswith(BACKUP_MANIFEST_PREFIX) or not name.endswith(
        BACKUP_MANIFEST_SUFFIX
    ):
        return None
    stamp = name[len(BACKUP_MANIFEST_PREFIX) : -len(BACKUP_MANIFEST_SUFFIX)]
    return stamp if BACKUP_ID_RE.fullmatch(stamp) else None


def _manifest_dump_present(manifest: Path, fields: dict[str, str]) -> bool:
    name = fields.get("dump") or ""
    if not name or "/" in name or name in {".", ".."}:
        return False
    try:
        info = (manifest.parent / name).stat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


def _manifest_gate(manifest: Path, pub: ModuleType) -> dict[str, Any]:
    """把一份 manifest 翻成恢复点门禁；任何不确定都按"没有恢复点"处理。"""
    parser = getattr(pub, "parse_manifest", None)
    if parser is None:
        # 发布模块是运行时按路径加载的；缺这个函数说明更新器与发行包不是同一次
        # 受信任构建。fail closed：不给恢复点，也不猜 manifest 格式。
        return _empty_backup_gate(pub, "manifest_unreadable")
    try:
        fields = parser(manifest)
    except (OSError, ValueError):
        return _empty_backup_gate(pub, "manifest_unreadable")
    if not isinstance(fields, dict):
        return _empty_backup_gate(pub, "manifest_unreadable")

    required = (
        "created_at",
        "mode",
        "dump",
        "dump_sha256",
        "tombstones",
        "tombstones_state",
        "keys_root",
        "keys_env_set",
        "media",
        "media_state",
    )
    tombstones = fields.get("tombstones_state")
    media = fields.get("media")
    media_state = fields.get("media_state")
    keys_root = fields.get("keys_root")
    keys_env = fields.get("keys_env_set")
    complete = (
        all(key in fields for key in required)
        # 镜像模式只跑 compose；direct 模式的 manifest 指向别的取数通道
        # （`LING_DATABASE_URL`），不是这套部署的恢复点。
        and fields.get("mode") == "compose"
        and _manifest_dump_present(manifest, fields)
        and tombstones in BACKUP_GATE_TOMBSTONE_STATES
        and media_state in BACKUP_GATE_MEDIA_STATES
        and keys_root in BACKUP_GATE_KEYS_ROOT_STATES
        and keys_env in BACKUP_GATE_KEYS_ENV_STATES
        # 自洽：media 文件名是否 absent 必须与 media_state 一致（与 publish 同判据）。
        and ((media == "absent") == (media_state == "absent"))
    )
    gate = _empty_backup_gate(pub, "manifest_incomplete")
    gate.update(
        {
            "backup_id": _backup_id(manifest),
            "complete": complete,
            "tombstones_state": tombstones,
            "media_state": media_state,
            "keys_root": keys_root,
            "keys_env_set": keys_env,
        }
    )
    if not complete or gate["backup_id"] is None:
        return gate
    if tombstones != "ok":
        gate["reason"] = "tombstones_not_ok"
        return gate
    if media_state == "partial":
        gate["reason"] = "media_partial"
        return gate
    if keys_env == "false" and keys_root != "present":
        gate["reason"] = "keys_missing"
        return gate
    gate["restore_point"] = True
    gate["reason"] = "ok"
    return gate


def backup_gate(cfg: Config, pub: ModuleType) -> dict[str, Any]:
    """扫描备份目录里**最近**的 manifest，得出与 task 无关的恢复点门禁。

    与 task 解耦是关键：第一次任务之前没有 ``task``，不能因此说"没有恢复点"
    （那会把全新部署的网页更新永久锁死）。这里只看备份目录的事实。
    """
    manifest, reason = _latest_manifest(cfg.backup_dir)
    if manifest is None:
        return _empty_backup_gate(pub, reason or "no_manifest")
    return _manifest_gate(manifest, pub)


def release_catalog_block(
    state: str, reason: str | None, entries: int, current_version: str | None
) -> dict[str, Any]:
    """``status.json`` 顶层 ``release_catalog``（**只放码与计数**，不放文案/路径）。"""
    return {
        "state": state,
        "reason": reason,
        "entries": entries,
        "current_version": current_version,
    }


def env_image_ref(env_file: Path, digest_re: re.Pattern[str]) -> str | None:
    """从受限 env 里读 ``LING_APP_IMAGE``（当前安装的镜像摘要，只读）。

    为什么读文件而不是问 API/容器：``.env`` 就是"现在装的是哪个摘要"的**宿主
    事实**（更新成功后也是更新器自己写回去的），读它不需要 Docker、不需要 HTTP
    鉴权，也不会在 idle 轮询里产生任何外部调用。解析口径与 publish 的写入口径
    一致：``KEY=`` 行、可带 ``export`` 前缀、重复时**最后一行生效**（与 compose
    的 env 文件语义一致）。摘要格式仍走发行包的 ``DIGEST_RE``（不另立一套）。
    """
    try:
        info = env_file.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > ENV_FILE_MAX_BYTES:
        return None
    try:
        text = env_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    pattern = re.compile(r"^\s*(?:export\s+)?LING_APP_IMAGE\s*=(.*)$")
    value: str | None = None
    for line in text.splitlines():
        match = pattern.match(line)
        if match is None:
            continue
        raw = match.group(1).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1].strip()
        value = raw or None
    if value is None or digest_re.fullmatch(value) is None:
        return None
    return value


def current_catalog_version(catalog: dict[str, Any], image_ref: str | None) -> str | None:
    """按**当前镜像摘要**在目录里反查版本（目录就是可信的"版本 <-> 摘要"映射）。

    反查不到（摘要不在目录里）返回 None：调用方必须按"当前版本未知"处理，
    **不能**猜成某个版本——猜错就会把一次跨度过大的升级放行。
    """
    if image_ref is None:
        return None
    for entry in catalog.get("releases", []):
        if not isinstance(entry, dict) or entry.get("digest") != image_ref:
            continue
        version = entry.get("version")
        return version if isinstance(version, str) else None
    return None


def release_targets(
    tool: ModuleType | None,
    catalog: dict[str, Any] | None,
    current_version: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """把目录投影成"可用目标"列表 + 推荐目标（供状态文件与网页使用）。

    规则（与模块 docstring 一致，且**不依赖文件顺序**）：

    1. 目标 ``version`` 必须**严格大于**当前版本，否则该条**不出现**（不提供降级）；
    2. 当前版本必须在该目标的 ``min_from`` 里，否则 ``compatible=false`` + 原因；
    3. 当前版本未知 -> 所有条目 ``compatible=false`` + ``current_version_unknown``；
    4. ``recommended`` = 版本最大的**兼容**目标（没有则 None）。

    目录缺失/不可用时 ``catalog`` 是 None，结果同样是空列表——"没有可用目标"，
    绝不是"随便给一个"。
    """
    if tool is None or catalog is None:
        return [], None
    releases = [entry for entry in catalog.get("releases", []) if isinstance(entry, dict)]

    def sort_key(entry: dict[str, Any]) -> tuple[Any, ...]:
        try:
            return tool.version_key(entry["version"])
        except Exception:  # noqa: BLE001 - 目录已校验过；兜底成"排最后"，绝不猜
            return (1 << 62,)

    ordered = sorted(releases, key=sort_key)
    current_key: tuple[Any, ...] | None = None
    if current_version is not None:
        try:
            current_key = tool.version_key(current_version)
        except Exception:  # noqa: BLE001 - 当前版本不可比较 -> 当作未知
            current_key = None
    targets: list[dict[str, Any]] = []
    recommended: dict[str, Any] | None = None
    for entry in ordered:
        try:
            key: tuple[Any, ...] | None = tool.version_key(entry["version"])
        except Exception:  # noqa: BLE001
            key = None
        if current_key is not None and key is not None and key <= current_key:
            # 只允许升级：同版本/更旧的目标不是"可用目标"，直接不出现。
            continue
        if current_key is None:
            compatible = False
            reason: str | None = INCOMPATIBLE_CURRENT_VERSION_UNKNOWN
        elif key is None:
            compatible = False
            reason = INCOMPATIBLE_VERSION_UNCOMPARABLE
        elif current_version not in entry.get("min_from", ()):
            compatible = False
            reason = INCOMPATIBLE_NOT_IN_MIN_FROM
        else:
            compatible = True
            reason = None
        target = {
            "version": entry["version"],
            "digest": entry["digest"],
            "commit": entry["commit"],
            "has_migration": entry["has_migration"],
            "compatible": compatible,
            "incompatible_reason": reason,
        }
        targets.append(target)
        if compatible:
            recommended = {
                "version": target["version"],
                "digest": target["digest"],
                "commit": target["commit"],
                "has_migration": target["has_migration"],
            }
    return targets, recommended


def _load_catalog_bytes(cfg: Config, tool: ModuleType) -> dict[str, Any]:
    """取目录：配了 ``catalog_url`` 就先联网取，取不到再读本地那份。

    联网取回的内容**不落盘**，直接交给发行包的 ``tool.parse_catalog()``
    校验——和本地那份走**同一套**门禁（schema、digest 格式、commit 格式、
    min_from、semver）。这里不新增任何校验规则：多一套规则只会多一处会和
    ``catalog.py`` 走散的地方。

    联网失败**不是错误**：退回本地那份，行为与没配这个地址时完全一样。
    部署机上的更新器本来就带着一份（可能过期的）本地目录，能升到哪儿就
    升到哪儿，总比整条通道变红好。
    """
    url = (cfg.catalog_url or "").strip()
    if url:
        # 大小上限直接取发行包 ``catalog.py`` 的那个常量：本地与联网两份
        # 用同一个数字，不另立一套。
        raw = _fetch_catalog_bytes(url, getattr(tool, "MAX_CATALOG_BYTES", 256 * 1024))
        if raw is not None:
            return tool.parse_catalog(raw)
    return tool.load_catalog(cfg.release_catalog)


def _fetch_catalog_bytes(url: str, max_bytes: int) -> bytes | None:
    """取回目录字节；任何问题都返回 ``None``（让调用方退回本地）。

    只用标准库：更新器容器里没有 HTTP 客户端依赖，它连查注册表都靠 docker
    CLI，为这一个 GET 引第三方库不值得。

    只允许 https：http 会让链路上任何人替换目录内容。
    """
    import urllib.error
    import urllib.request

    if not url.startswith("https://"):
        return None
    try:
        with urllib.request.urlopen(url, timeout=CATALOG_FETCH_TIMEOUT_SECONDS) as response:
            data = response.read(max_bytes + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if len(data) > max_bytes:
        return None
    return data


def read_release_catalog(
    cfg: Config, pub: ModuleType, tool: ModuleType | None
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    """读目录 -> ``(block, available_targets, recommended_target)``。

    任何失败都收敛成固定的 ``state``/``reason`` 码（**不**把异常文本、路径写进
    状态文件），并且 ``available_targets=[]``：宁可明说"没有可用目标"，也不要
    静默或假装有目标。
    """
    if tool is None:
        empty = release_catalog_block(
            CATALOG_STATE_INVALID, CATALOG_REASON_TOOL_MISSING, 0, None
        )
        return empty, [], None
    try:
        catalog = _load_catalog_bytes(cfg, tool)
    except Exception as error:  # noqa: BLE001 - 目录不可用一律 fail closed
        code = getattr(error, "code", "")
        reason = CATALOG_REASON_BY_CODE.get(code, CATALOG_REASON_INVALID)
        state = (
            CATALOG_STATE_MISSING
            if reason == CATALOG_REASON_MISSING
            else CATALOG_STATE_INVALID
        )
        return release_catalog_block(state, reason, 0, None), [], None
    releases = catalog.get("releases", [])
    if not isinstance(releases, list):
        return (
            release_catalog_block(CATALOG_STATE_INVALID, CATALOG_REASON_INVALID, 0, None),
            [],
            None,
        )
    current_version = current_catalog_version(
        catalog, env_image_ref(cfg.env_file, pub.DIGEST_RE)
    )
    targets, recommended = release_targets(tool, catalog, current_version)
    return (
        release_catalog_block(CATALOG_STATE_OK, None, len(releases), current_version),
        targets,
        recommended,
    )


def status_payload(
    pub: ModuleType,
    state: str,
    task: dict[str, Any] | None,
    gate: dict[str, Any],
    catalog: dict[str, Any],
    targets: list[dict[str, Any]],
    recommended: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "schema": STATUS_SCHEMA,
        "protocol": PROTOCOL,
        "updated_at": pub.utc_now(),
        "updater": {"state": state, "version": UPDATER_VERSION},
        "task": task_view(task),
        "backup_gate": gate,
        "release_catalog": catalog,
        "available_targets": targets,
        "recommended_target": recommended,
    }


def assert_status_sanitized(cfg: Config, text: str) -> None:
    """写之前自查：状态文件里不许出现路径、密码/日志字样与堆栈。"""
    forbidden = [
        str(cfg.backup_dir),
        str(cfg.state_dir),
        str(cfg.requests_dir),
        str(cfg.compose_file),
        str(cfg.env_file),
        str(cfg.backup_script),
        str(cfg.publish_py),
        "journal.log",
        "backup.log",
        "migrate.log",
        "POSTGRES_PASSWORD",
        "LING_ROOT_KEY",
        "LING_BOOTSTRAP_ADMIN_PASSWORD",
        "password",
        "Traceback",
    ]
    for token in forbidden:
        if token and token in text:
            raise UpdaterError(
                EXIT_NEEDS_MANUAL,
                "internal_error",
                "status",
                "状态文件自查发现敏感内容（已拒绝写入）",
            )


class StatusWriter:
    """原子写脱敏状态；内容不变时不重写（避免轮询空转刷盘）。

    ``backup_gate`` **不在每次写状态时重算**：``checked_at`` 每次都变，内容就
    永远不同，空闲轮询会每 5 秒刷一次盘（这个类存在的意义正好相反）。所以恢复
    点门禁只在明确的时机重算：更新器启动时一次（:class:`Updater` 构造）、每处理
    完一个任务一次（:meth:`Updater.refresh_status_after_task`）。本类的
    ``write`` 在**从未算过**时兜底算一次，覆盖 ``--acknowledge-manual`` 这类
    不进主循环的入口。

    ``release_catalog`` / ``available_targets`` / ``recommended_target`` 同理：
    它们**不含时间戳**（只有状态码、计数、当前版本与目标列表），所以重算也不会
    让内容抖动；刷新时机与恢复点门禁完全一致（启动、每处理完一个任务）。读目录
    与读受限 env 都是本地只读文件，idle 轮询**不产生任何 Docker/网络调用**。
    """

    def __init__(self, cfg: Config, pub: ModuleType, tool: ModuleType | None = None) -> None:
        self.cfg = cfg
        self.pub = pub
        self.tool = tool
        self._last: str | None = None
        self._gate: dict[str, Any] | None = None
        self._catalog: dict[str, Any] | None = None
        self._targets: list[dict[str, Any]] = []
        self._recommended: dict[str, Any] | None = None

    def refresh_backup_gate(self) -> dict[str, Any]:
        self._gate = backup_gate(self.cfg, self.pub)
        return self._gate

    def refresh_release_catalog(self) -> dict[str, Any]:
        """重读可信发布目录并重算可用目标/推荐目标（本地只读，不联网）。"""
        self._catalog, self._targets, self._recommended = read_release_catalog(
            self.cfg, self.pub, self.tool
        )
        return self._catalog

    def write(self, state: str, task: dict[str, Any] | None, *, force: bool = False) -> None:
        gate = self._gate if self._gate is not None else self.refresh_backup_gate()
        catalog = (
            self._catalog if self._catalog is not None else self.refresh_release_catalog()
        )
        payload = status_payload(
            self.pub, state, task, gate, catalog, self._targets, self._recommended
        )
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        assert_status_sanitized(self.cfg, text)
        if text == self._last and not force:
            return
        self.pub.write_atomic(self.cfg.status_file, text.encode("utf-8"), 0o644)
        self._last = text


# ---------------------------------------------------------------- 单飞与认领


class TaskLock:
    """单飞锁：包住「读任务 + 认领请求 + 处理」，拿不到就让另一个实例继续。"""

    def __init__(self, cfg: Config) -> None:
        self.path = cfg.state_dir / "updater.lock"
        self._fd: int | None = None

    def acquire(self) -> bool:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | nofollow, 0o600)
        except OSError as exc:
            raise UpdaterError(
                EXIT_NEEDS_MANUAL,
                "layout_unavailable",
                "bootstrap",
                f"无法打开单飞锁：{type(exc).__name__}",
            ) from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UpdaterError(
                    EXIT_NEEDS_MANUAL,
                    "layout_unavailable",
                    "bootstrap",
                    "单飞锁必须是普通文件",
                )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def pending_requests(cfg: Config) -> list[Path]:
    try:
        entries = [
            path
            for path in cfg.requests_dir.iterdir()
            if path.is_file()
            and path.name.startswith("request-")
            and request_id_from_name(path.name) is not None
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda item: item.name)


def claim_request(cfg: Config, path: Path) -> Path | None:
    """``rename`` 认领：并发认领只有一个成功，失败者拿到 None。"""
    request_id = request_id_from_name(path.name)
    if request_id is None:
        return None
    claimed = cfg.requests_dir / f"claimed-{request_id}.json"
    try:
        os.rename(path, claimed)
    except OSError:
        return None
    return claimed


def archive_claim(cfg: Config, claimed: Path) -> None:
    archive_dir = cfg.requests_dir / "archive"
    try:
        archive_dir.mkdir(exist_ok=True, mode=0o700)
        os.replace(claimed, archive_dir / claimed.name)
    except OSError:
        pass


def already_processed(cfg: Config, request_id: str) -> bool:
    return (cfg.requests_dir / "archive" / f"claimed-{request_id}.json").exists()


# ---------------------------------------------------------------- 受限 env 写入


class EnvWriteError(OSError):
    """受限 env 写入失败（回滚是否成功由 ``restored`` 说明）。"""

    def __init__(self, detail: str, *, restored: bool) -> None:
        super().__init__(detail)
        self.restored = restored


def truncate_rewrite(path: Path, data: bytes, mode: int = 0o600) -> None:
    """**原地**截断重写（不换 inode、不建临时文件）。

    只能这么做：``.env`` 是单文件 bind mount，容器里的路径就是挂载点——
    同目录建临时文件会被只读根拒绝（EROFS），把 ``/tmp`` 的临时文件 rename
    过来则是跨设备 ``EXDEV``，rename 到挂载点本身是 ``EBUSY``。
    """
    flags = os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    # 不带 O_CREAT：挂载点必须由安装器预建；缺文件是配置错误，不能悄悄新建。
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(
                f"受限 env 必须是普通文件（mode={stat.S_IMODE(info.st_mode):04o}）"
            )
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("受限 env 写入返回 0 字节")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def restore_env(path: Path, before: bytes, mode: int = 0o600) -> bool:
    """把旧字节写回；内容本来就一致时视为已恢复（写入根本没落地）。"""
    try:
        if path.read_bytes() == before:
            return True
        truncate_rewrite(path, before, mode)
        return path.read_bytes() == before
    except OSError:
        return False


def write_env_in_place(
    path: Path,
    data: bytes,
    *,
    backup_path: Path,
    save_backup: Callable[[Path, bytes, int], None],
    mode: int = 0o600,
) -> None:
    """把 ``data`` 原地写进受限 env；失败时保证不留半写状态。

    1. 先读旧字节，用 ``save_backup``（``publish.write_atomic``）在状态目录留一份
       0600 私有副本——跨进程、崩溃后仍可用；
    2. 原地截断重写 + ``fsync``；
    3. 读回逐字节校验；
    4. 任何一步失败：按**实际内容**决定是否回滚（``open`` 就被拒时内容没变，
       不必写回）；回滚成功删副本并抛原异常，回滚失败保留副本并抛
       :class:`EnvWriteError`（``restored=False``，人工按副本恢复）；
    5. 成功也删副本（旧值就是状态记录里的 ``previous.digest``，不留密钥副本）。

    窗口期由跨工具安装锁保护：``install.sh`` / ``ling install`` / ``publish.py``
    都要先拿同一把 ``.ling-install.lock``，不会有人在我们写到一半时读它。
    """
    before = path.read_bytes()
    if before == data:
        return
    save_backup(backup_path, before, 0o600)
    try:
        truncate_rewrite(path, data, mode)
        if path.read_bytes() != data:
            raise OSError("受限 env 写入后读回不一致")
    except BaseException as error:
        restored = restore_env(path, before, mode)
        if restored:
            with contextlib.suppress(OSError):
                backup_path.unlink()
            raise
        raise EnvWriteError(
            f"受限 env 写入失败且回滚失败（旧内容保存在私有副本里）：{error}",
            restored=False,
        ) from error
    with contextlib.suppress(OSError):
        backup_path.unlink()


def acquire_install_lock(pub: ModuleType, env_file: Path) -> Any:
    """拿**跨工具**安装锁（与 install.sh / ling install / publish.py 同一把）。

    路径口径由 ``publish.InstallLock`` 决定：``<env realpath 所在目录>`` 下的
    ``.ling-install.lock``。容器里 env 是单文件挂载，所以就是
    ``/srv/ling/.ling-install.lock``（安装器预建、0600、单文件 rw 挂载）。

    ``flock`` 是非阻塞的：拿不到**立即**失败并转成 ``needs_manual``，
    不等待、不跳过、不做任何 compose 变更。
    """
    lock = pub.InstallLock(env_file)
    try:
        lock.acquire()
    except pub.PublishError as error:
        raise UpdaterError(
            EXIT_NEEDS_MANUAL,
            "install_lock_busy",
            "prepare",
            f"安装锁不可用（另一个安装/发布/更新任务正在运行）：{error.message}",
            ["acknowledge_manual"],
        ) from error
    return lock


# ---------------------------------------------------------------- 任务上下文


class _UpdaterContextMixin:
    """把复用实现（``publish.Context``）接到更新器的任务/维护门禁上。

    必须是 mixin：``publish.py`` 是运行时按路径加载的模块，不能在 import 期
    继承它。``make_context_class()`` 负责拼出真正的类，MRO 是
    ``(TaskContext, _UpdaterContextMixin, publish.Context)``，所以本类里的
    ``super()`` 调用都落到复用实现上。
    """

    def _init_updater_fields(
        self,
        publish_module: ModuleType,
        gate: MaintenanceGate,
        task: dict[str, Any],
        persist: Callable[[], None],
        logger: Any,
    ) -> None:
        self.pub = publish_module
        self.gate = gate
        self.task = task
        self.persist = persist
        self.real_files = [self.o.compose_file]
        if self.o.compose_override is not None:
            self.real_files.append(self.o.compose_override)
        self._group: list[Path] | None = None
        self.host_ports_published = False
        self.install_lock: Any = None
        self.log = logger
        self.runner.logger = logger

    # -------------------------------------------------- publish.Context 的对接
    def acquire_locks(self) -> None:
        """跨工具安装锁：与 install.sh / ling install / publish.py 互斥。

        不复用 ``publish.Context.acquire_locks()``：那条路径还会去
        ``<compose 目录>/.ling-install.lock`` 抢第二把锁，而 compose 文件组在
        更新器容器里是**只读**挂载（建不出锁文件）。这里只用「env 所在目录」这
        一条口径，与 install.sh 有 ``deploy/.env`` 时、以及宿主执行器完全一致。
        """
        self.install_lock = acquire_install_lock(self.pub, self.o.env_file)

    def release_locks(self) -> None:
        lock, self.install_lock = self.install_lock, None
        if lock is not None:
            lock.release()

    def compose_argv(self) -> list[str]:
        files = self._group if self._group is not None else self.real_files
        argv = ["docker", "compose", "-p", self.o.project]
        for path in files:
            argv += ["-f", str(path)]
        argv += ["--env-file", str(self.o.env_file)]
        return argv

    def compose_config_for(
        self, files: Sequence[Path], image: str | None, **kwargs: Any
    ) -> dict[str, Any]:
        previous = self._group
        self._group = list(files)
        try:
            return self.compose_config(image, **kwargs)
        finally:
            self._group = previous

    def check_disk(self) -> None:
        """复用发布脚本的磁盘门禁；更新器容器通常看不到宿主 Docker 根目录。"""
        pub = self.pub
        result = self.runner.run(["docker", "info", "--format", "{{.DockerRootDir}}"])
        if result.returncode != 0:
            raise pub.PublishError(
                pub.EXIT_PREFLIGHT,
                "failed_preflight",
                "disk",
                "无法读取 Docker 根目录（更新器与 docker socket 不在同一台机器？）",
            )
        docker_root = Path(result.stdout.strip() or "/var/lib/docker")
        if docker_root.is_dir():
            super().check_disk()
            return
        old_size = max(
            int(self.previous.get(name, {}).get("size") or 0) for name in ("api", "worker")
        )
        previous_backup = self.previous_backup_bytes()
        backup_need = self.disk_budget(
            "LING_RELEASE_BACKUP_BUDGET_BYTES",
            old_size + previous_backup + 256 * 1024 * 1024,
        )
        usage = shutil.disk_usage(self.o.backup_dir)
        self.record["disk"] = {
            "docker_root": str(docker_root),
            "docker_free": None,
            "docker_need": None,
            "docker_check": "unavailable_in_updater_container",
            "backup_dir": str(self.o.backup_dir),
            "backup_free": usage.free,
            "backup_need": backup_need,
            "old_image_size": old_size,
            "previous_backup_bytes": previous_backup,
        }
        if "docker_free_unverified" not in self.task["warnings"]:
            self.task["warnings"].append("docker_free_unverified")
            self.persist()
        self.log.emit(
            "警告：更新器容器看不到宿主 Docker 根目录，镜像侧余量无法核对"
            "（status.warnings=docker_free_unverified）；备份目录余量照常核对"
        )
        if usage.free < backup_need:
            raise pub.PublishError(
                pub.EXIT_PREFLIGHT,
                "failed_preflight",
                "disk",
                f"备份目录余量 {usage.free} B < 需要 {backup_need} B",
                ["先清理/轮转旧备份或扩容；不要 prune 正在使用的卷/镜像。"],
            )

    def update_env_file(self) -> None:
        """把 ``LING_APP_IMAGE`` 真正写回**宿主**的受限 env。

        缺陷①的教训：只改进程环境会让宿主 ``.env`` 永远停在旧摘要，任何一次手动
        ``docker compose up`` 都把服务退回旧镜像——等于更新没做。所以这里直接跑
        ``publish.Context.update_env_file``（realpath → 校验没被第三方改动 →
        重写 → 验权限位 → **不带 shell 覆盖**重新解析 compose 确认指向目标），
        只替换它的**写入方式**：env 是单文件 bind mount，``write_atomic`` 的
        「同目录临时文件 + rename」在挂载点上做不到（见
        :func:`write_env_in_place`），改用原地重写 + 私有副本回滚。
        """
        pub = self.pub
        request_id = str(self.task["request_id"])
        backup = self.o.record_path.with_name(f"env-before-{request_id}.env")
        # 先抓住**原始**实现：私有副本必须用真正的原子写，不能递归到下面的替身。
        save_backup = pub.write_atomic

        def in_place(path: Path, data: bytes, mode: int = 0o600) -> None:
            write_env_in_place(
                path,
                data,
                backup_path=backup,
                save_backup=lambda target, payload, bits: save_backup(target, payload, bits),
                mode=mode,
            )

        self.log.emit(
            "写回宿主受限 env：复用 publish 的更新逻辑，写入改为"
            "「原地重写 + 私有副本回滚」（单文件挂载无法 rename）"
        )
        original = pub.write_atomic
        pub.write_atomic = in_place
        try:
            super().update_env_file()
        except OSError as error:
            raise pub.PublishError(
                pub.EXIT_POST_MIGRATION,
                "failed_after_migration",
                "env_update",
                f"受限 env 写入失败（{type(error).__name__}）：{error}",
                [
                    "已迁移：不要回滚镜像、不要启动新容器；先按状态目录里的"
                    "env-before-*.env 私有副本核对/恢复受限 env。",
                ],
                after_migration=True,
            ) from error
        finally:
            pub.write_atomic = original
        self.env_updated = True
        self.record["compose"]["env_image_source"] = "env_file"
        self.persist()
        self.log.emit("宿主受限 env 已指向目标摘要（原地重写 + 读回校验通过）")

    def switch_and_verify(self) -> None:
        raise RuntimeError(
            "更新器必须用带维护门禁的 switch_to_target()/verify_after_switch()："
            "publish 默认顺序会在验证前就发布宿主端口"
        )

    # -------------------------------------------------- 拉取与复核
    def pull_and_prepare(self) -> None:
        pub = self.pub
        self.log.emit("拉取目标镜像（此步不影响在线服务）")
        self.run_checked(
            [*self.compose_argv(), "pull", "api", "worker"],
            env=self.target_env(),
            timeout=self.o.backup_timeout,
            exit_code=pub.EXIT_PULL,
            status="failed_pull",
            step="pull",
            message="docker compose pull api worker 失败",
            next_actions=["检查 GHCR 凭据/网络与 digest；旧服务未进入维护窗口。"],
        )
        image = self.docker_image_inspect(
            self.o.target, exit_code=pub.EXIT_PULL, status="failed_pull"
        )
        self.target_image = image
        # 更新器自己的复核必须先跑：否则复用实现会先把错误吞掉，更新器自己的
        # 证据（拒绝原因/退出码）就看不到、也测不到。
        self.recheck_pulled_image(image)
        self.verify_target_image_identity(image)
        self.extract_target_head()
        self.verify_current_revision_in_target(self.record["previous"]["alembic_version"])
        self.record["status"] = "pulled"
        self.write_record()
        self.log.emit(
            f"目标镜像身份核验通过：id={image.get('Id')} "
            f"commit={self.target_commit(image)} head={self.target_head}"
        )

    def recheck_pulled_image(self, image: dict[str, Any]) -> None:
        """更新器**自己**的复核：不依赖页面/请求/Release 正文的任何字段。

        失败统一标 ``step="updater_self_check"``：发布脚本 ``publish.py`` 的
        ``verify_target_image_identity`` 是同一口径的**第二份**检查（更新器复用
        它的 Context），但两者的失败阶段必须可区分——否则删掉这一份，测试仍会
        被 publish 的同源副本兜住，看起来是绿的。``failure_code`` 仍按
        ``failed_pull`` 归到 ``pull_failed``（网页文案不变），只有 ``stage``
        不同，供测试/排查判断到底是哪一层拒绝的。
        """
        pub = self.pub
        arch = image.get("Architecture")
        os_name = image.get("Os")
        if os_name != "linux" or arch != self.o.host_arch:
            raise pub.PublishError(
                pub.EXIT_PULL,
                "failed_pull",
                "updater_self_check",
                f"更新器复核：镜像平台是 {os_name}/{arch}，宿主是 linux/{self.o.host_arch}",
                ["确认拉取的是对应平台；拒绝运行错误架构的镜像。"],
            )
        labels = (image.get("Config") or {}).get("Labels") or {}
        env_values = self.parse_env_list((image.get("Config") or {}).get("Env") or [])
        env_commit = env_values.get("LING_COMMIT", "")
        label_commit = str(labels.get("org.opencontainers.image.revision") or "")
        if not env_commit or not label_commit:
            raise pub.PublishError(
                pub.EXIT_PULL,
                "failed_pull",
                "updater_self_check",
                "更新器复核：镜像缺少 LING_COMMIT 或 OCI 标注 "
                "org.opencontainers.image.revision，无法证明部署的是哪个源提交",
            )
        if env_commit != label_commit:
            raise pub.PublishError(
                pub.EXIT_PULL,
                "failed_pull",
                "updater_self_check",
                f"更新器复核：镜像内 LING_COMMIT={env_commit!r} 与 OCI 标注 "
                f"revision={label_commit!r} 不一致",
            )
        if pub.COMMIT_RE.fullmatch(label_commit) is None:
            raise pub.PublishError(
                pub.EXIT_PULL,
                "failed_pull",
                "updater_self_check",
                f"更新器复核：镜像标注的提交不是 40 位小写 hex：{label_commit!r}",
            )
        suffixes = {
            str(item).rsplit("@sha256:", 1)[1]
            for item in image.get("RepoDigests") or []
            if "@sha256:" in str(item)
        }
        if self.o.target_hex not in suffixes:
            raise pub.PublishError(
                pub.EXIT_PULL,
                "failed_pull",
                "updater_self_check",
                "更新器复核：本地镜像的 RepoDigests 与目标摘要对不上",
                ["确认 pull 的是同一 digest；不要用同名 tag 或缓存镜像冒充。"],
            )
        if not image.get("Id"):
            raise pub.PublishError(
                pub.EXIT_PULL, "failed_pull", "updater_self_check", "更新器复核：镜像没有 Id"
            )

    # -------------------------------------------------- 维护门禁阶段
    def _start_service(self, service: str, *, force_recreate: bool) -> None:
        pub = self.pub
        argv = [
            *self.compose_argv(),
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull=never",
        ]
        if force_recreate:
            argv.append("--force-recreate")
        argv.append(service)
        self.run_checked(
            argv,
            env=self.target_env(),
            timeout=pub.DEFAULT_COMMAND_TIMEOUT,
            exit_code=pub.EXIT_POST_MIGRATION,
            status="failed_after_migration",
            step=f"up_{service}",
            message=f"启动目标 {service} 容器失败",
            next_actions=[
                "已迁移：不要自动切旧镜像/恢复旧 dump；保持维护并人工检查容器日志。",
            ],
            after_migration=True,
        )

    def published_port_holders(self) -> list[str]:
        """宿主端口的占用者（container ID|project）；读不到就按失败处理。"""
        if self.published_port is None:
            raise self.pub.PublishError(
                self.pub.EXIT_POST_MIGRATION,
                "failed_after_migration",
                "maintenance_gate",
                "宿主端口未知，不能证明维护门禁有效",
                after_migration=True,
            )
        result = self.runner.run(
            [
                "docker",
                "ps",
                "--filter",
                f"publish={self.published_port}/tcp",
                "--format",
                '{{.ID}}|{{.Label "com.docker.compose.project"}}',
            ]
        )
        if result.returncode != 0:
            raise self.pub.PublishError(
                self.pub.EXIT_POST_MIGRATION,
                "failed_after_migration",
                "maintenance_gate",
                "无法读取宿主端口占用者，不能证明维护门禁有效",
                ["已迁移：保持维护，人工核对端口后继续。"],
                after_migration=True,
            )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def assert_maintenance_closed(self, step: str) -> None:
        holders = self.published_port_holders()
        if holders:
            raise self.pub.PublishError(
                self.pub.EXIT_POST_MIGRATION,
                "failed_after_migration",
                "maintenance_gate",
                f"维护门禁未生效：宿主端口仍被占用（{step}）",
                ["已迁移：不要开放端口；人工核对该占用者是哪套容器。"],
                after_migration=True,
            )
        self.log.emit(f"维护门禁实测：宿主端口 {self.published_port} 无占用者（{step}）")

    def assert_host_port_owned_by(self, container_id: str) -> None:
        holders = self.published_port_holders()
        if required_owner(holders, container_id, self.o.project):
            self.log.emit(f"宿主端口 {self.published_port} 的占用者是目标 api 容器")
            return
        raise self.pub.PublishError(
            self.pub.EXIT_POST_MIGRATION,
            "failed_after_migration",
            "host_port",
            "宿主端口占用者不是目标 api 容器",
            ["已迁移：人工核对端口占用与容器实况。"],
            after_migration=True,
        )

    def verify_version_endpoint_in_container(self) -> str:
        """容器内**进程证据**：维护期不能靠宿主端口，也不能靠需要鉴权的端点。

        旧实现是容器内 urllib 打 ``127.0.0.1:6520/v1/version``：该端点挂
        ``CurrentAuth``，未鉴权恒 401 —— 真机演练 P5 就卡在这里
        （``failed_after_migration / version_container``）。发布/更新流程不该持有
        用户凭据，所以改成让应用自己报出版本与构建提交（零 HTTP、零凭据），
        探针字符串与解析函数都复用 ``publish`` 的那一份，避免两边漂移。
        证据等价性见 ``publish.Context.verify_running_commit`` 的说明。
        """
        result = self.run_checked(
            [
                *self.compose_argv(),
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                self.pub.CONTAINER_VERSION_PROBE,
            ],
            env=self.target_env(),
            timeout=self.pub.CONTAINER_VERSION_EXEC_TIMEOUT,
            exit_code=self.pub.EXIT_POST_MIGRATION,
            status="failed_after_migration",
            step="version_container",
            message="容器内读取应用自报的版本/提交失败（exec 失败）",
            next_actions=["已迁移：不要自动回滚；人工检查新 api 日志与镜像内 Python。"],
            after_migration=True,
        )
        probe = self.pub.parse_container_version_probe(result.stdout)
        commit = probe.get("commit")
        expected = self.record["target"]["source_commit"]
        if not isinstance(commit, str) or commit != expected:
            raise self.pub.PublishError(
                self.pub.EXIT_POST_MIGRATION,
                "failed_after_migration",
                "version_container",
                (
                    f"容器内进程报告的提交 {commit!r} ≠ 发布记录源提交 {expected!r}"
                    f"（应用自报版本 {probe.get('version')!r}）"
                ),
                ["已迁移：不要自动回滚；确认运行容器确实是目标镜像。"],
                after_migration=True,
            )
        return commit

    def verify_final_head(self) -> str:
        head = self.read_current_alembic_version(
            record_previous=False,
            exit_code=self.pub.EXIT_POST_MIGRATION,
            status="failed_after_migration",
            after_migration=True,
        )
        self.record["result"]["alembic_version_after_start"] = head
        if head != self.target_head:
            raise self.pub.PublishError(
                self.pub.EXIT_POST_MIGRATION,
                "failed_after_migration",
                "head_after_start",
                f"新 API 启动后 alembic_version={head!r} ≠ 目标 head={self.target_head!r}",
                ["已迁移：不要自动回滚；保持维护并人工核对 schema 与写者。"],
                after_migration=True,
            )
        return head

    def assert_same_image(self, api: dict[str, Any], worker: dict[str, Any]) -> None:
        if api["image_id"] == worker["image_id"]:
            return
        raise self.pub.PublishError(
            self.pub.EXIT_POST_MIGRATION,
            "failed_after_migration",
            "same_digest",
            "api/worker 运行镜像 ID 不同，不是同一摘要",
            ["不要以 Compose 退出 0 为成功；人工核对两个容器的镜像。"],
            after_migration=True,
        )

    def switch_to_target(self) -> None:
        """维护门禁下的切换：先在容器网络里验证，验证通过才发布宿主端口。"""
        self.update_env_file()
        self.task["stage"] = "switch"
        self.task["maintenance"]["state"] = MAINT_ACTIVE
        self.task["maintenance"]["files"] = [str(path) for path in self.gate.files]
        self.persist()
        self._group = list(self.gate.files)
        self.log.emit(
            "维护门禁生效：用临时 compose（api 的 ports 为空）启动目标 api，"
            "只存在于容器网络"
        )
        self._start_service("api", force_recreate=False)
        self.assert_maintenance_closed("api 启动后")
        self.verify_container_readyz()
        self.record["result"]["readyz_container"] = 200
        self.verify_web_manifest()
        self.record["result"]["version_commit"] = self.verify_version_endpoint_in_container()
        self.log.emit("目标 api 在容器网络内验证通过；启动 worker（仍不发布宿主端口）")
        self._start_service("worker", force_recreate=False)
        worker_state = self.verify_container_identity("worker")
        self.wait_worker_health(worker_state)
        self.record["result"]["worker"] = worker_state
        self.verify_final_head()
        api_state = self.verify_container_identity("api")
        self.record["result"]["api"] = api_state
        self.assert_same_image(api_state, worker_state)
        self.log.emit(
            "全部验证通过（容器内 readyz/清单/提交、worker、head、同一镜像）："
            "改用正式 compose（带宿主端口）重启 api"
        )
        self._group = None
        self.task["maintenance"]["verified"] = True
        self.persist()
        self._start_service("api", force_recreate=True)
        self.host_ports_published = True

    def verify_after_switch(self) -> None:
        self.task["stage"] = "verify"
        self.persist()
        self.record["result"]["readyz_http"] = self.wait_readyz()
        self.record["result"]["version_commit"] = self.verify_running_commit()
        api_state = self.verify_container_identity("api")
        worker_state = self.verify_container_identity("worker")
        self.record["result"]["api"] = api_state
        self.record["result"]["worker"] = worker_state
        self.assert_same_image(api_state, worker_state)
        self.assert_host_port_owned_by(str(api_state["container_id"]))
        self.verify_final_head()
        self.task["maintenance"]["state"] = MAINT_RELEASED
        self.persist()
        self.record["status"] = "success"
        self.record["finished_at"] = self.pub.utc_now()
        self.record["exit_code"] = EXIT_OK
        self.write_record()
        self.log.emit(
            f"更新成功：digest={self.o.target} commit={api_state['commit']} "
            f"image_id={api_state['image_id']} head={self.target_head}"
        )

    # -------------------------------------------------- 失败处理
    def enforce_maintenance_gate(self) -> None:
        """已迁移后失败：不切旧镜像，但把维护门禁重新关上（不是回滚）。"""
        if not self.gate.files:
            self.record["maintenance"] = {"enforced": "unavailable"}
            self.log.emit("警告：没有可用的维护门禁副本，无法自动关上宿主端口")
            return
        if not self.host_ports_published:
            self.record["maintenance"] = {"enforced": "already_closed"}
            self.log.emit("维护门禁仍生效（宿主端口从未发布），不执行任何 compose 变更")
            return
        self.log.emit("已迁移后失败：用临时 compose（无宿主端口）重启 api，重新关上维护门禁")
        self._group = list(self.gate.files)
        try:
            self._start_service("api", force_recreate=True)
            self.assert_maintenance_closed("失败后重新关门")
            self.record["maintenance"] = {"enforced": "closed"}
            if "maintenance_reenforced" not in self.task["warnings"]:
                self.task["warnings"].append("maintenance_reenforced")
        except self.pub.PublishError as exc:
            self.record["maintenance"] = {"enforced": "failed"}
            self.log.emit(f"警告：重新关上维护门禁失败：{exc.message}")
        finally:
            self.persist()

    def handle_failure(self, error: Any) -> int:
        if not (error.after_migration or self.migration_attempted):
            # 未迁移的失败允许恢复旧服务：恢复必须用正式文件组（带宿主端口）。
            self._group = None
        code = super().handle_failure(error)
        if error.after_migration or self.migration_attempted:
            self.enforce_maintenance_gate()
        return code


def daemon_arch(pub: ModuleType) -> str:
    """宿主架构以 **docker daemon** 报告为准（容器内 ``platform.machine()`` 可能是
    模拟平台的架构，用它比会放过错误平台的镜像）。拿不到才退回本进程架构。
    """
    machine = ""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.Architecture}}"],
            capture_output=True,
            text=True,
            timeout=20.0,
            check=False,
        )
        if proc.returncode == 0:
            machine = proc.stdout.strip().lower()
    except (OSError, subprocess.SubprocessError):
        machine = ""
    if not machine:
        machine = platform.machine().strip().lower()
    aliases = getattr(pub, "ARCH_ALIASES", {})
    return aliases.get(machine, machine)


def make_context_class(publish_module: ModuleType) -> type:
    """拼出 ``TaskContext``：mixin 在前，复用实现在后。

    ``publish.py`` 是运行时按路径加载的模块，不能在 import 期继承它，所以这里
    返回一个真正的子类；MRO 是 ``(TaskContext, _UpdaterContextMixin, Context)``，
    mixin 里的 ``super()`` 因此都落到复用实现上。
    """

    class TaskContext(_UpdaterContextMixin, publish_module.Context):
        def __init__(
            self,
            options: Any,
            gate: MaintenanceGate,
            task: dict[str, Any],
            persist: Callable[[], None],
            logger: Any,
        ) -> None:
            publish_module.Context.__init__(self, options)
            self._init_updater_fields(publish_module, gate, task, persist, logger)

    return TaskContext


def required_owner(holders: Sequence[str], container_id: str, project: str) -> bool:
    for line in holders:
        parts = line.split("|", 1)
        if parts[0] == container_id and len(parts) == 2 and parts[1] == project:
            return True
    return False


def published_port_from_config(config: dict[str, Any]) -> tuple[int | None, str]:
    services = config.get("services") or {}
    api = services.get("api") if isinstance(services, dict) else None
    for entry in (api or {}).get("ports") or []:
        if not isinstance(entry, dict):
            continue
        try:
            target = int(entry.get("target"))
        except (TypeError, ValueError):
            continue
        if target != 6520:
            continue
        try:
            published = int(entry.get("published"))
        except (TypeError, ValueError):
            return None, "127.0.0.1"
        host_ip = entry.get("host_ip")
        host = (
            host_ip.strip()
            if isinstance(host_ip, str) and host_ip.strip() not in {"", "0.0.0.0", "::"}
            else "127.0.0.1"
        )
        return published, host
    return None, "127.0.0.1"


# ---------------------------------------------------------------- 更新器主体


class Updater:
    def __init__(
        self, cfg: Config, pub: ModuleType, catalog_tool: ModuleType | None = None
    ) -> None:
        self.cfg = cfg
        self.pub = pub
        self.tool = catalog_tool
        self.store = TaskStore(cfg, pub)
        self.status = StatusWriter(cfg, pub, catalog_tool)
        # **启动时算一次**恢复点门禁：idle 也要有（否则 API 只能看 task，
        # 第一次更新之前永远"不可安装"）。
        self.status.refresh_backup_gate()
        # 可信发布目录同理：idle 也要有"可用目标"，否则网页的「更新」按钮永远
        # 「暂无可用目标」。读目录/读受限 env 都是本地只读文件，不碰 Docker。
        self.status.refresh_release_catalog()
        self.context_class = make_context_class(pub)
        self.task: dict[str, Any] | None = None
        self.log = pub.Logger()
        self.log.attach(cfg.state_dir / "journal.log")

    # -------------------------------------------------- 基础设施
    def emit(self, message: str) -> None:
        self.log.emit(message)

    def persist(self) -> None:
        if self.task is not None:
            self.store.write(self.task)

    def compose_sources(self) -> list[Path]:
        files = [self.cfg.compose_file]
        if self.cfg.compose_override is not None:
            files.append(self.cfg.compose_override)
        return files

    def build_options(self, request: dict[str, Any], target_hex: str) -> Any:
        cfg = self.cfg
        request_id = str(request["request_id"])
        host_arch = daemon_arch(self.pub)
        self.emit(f"宿主架构（docker daemon 报告）：{host_arch}")
        return self.pub.Options(
            target=request["target_digest"],
            target_hex=target_hex,
            compose_file=cfg.compose_file,
            compose_override=cfg.compose_override,
            env_file=cfg.env_file,
            project=cfg.project,
            backup_dir=cfg.backup_dir,
            backup_script=cfg.backup_script,
            record_path=cfg.state_dir / f"release-{request_id}.json",
            snapshot_dir=cfg.backup_dir / f"release-{request_id}",
            source_commit=None,
            ready_timeout=cfg.ready_timeout,
            health_timeout=cfg.health_timeout,
            migrate_timeout=cfg.migrate_timeout,
            backup_timeout=cfg.backup_timeout,
            host_arch=host_arch,
        )

    def build_context(
        self,
        options: Any,
        gate: MaintenanceGate,
    ) -> Any:
        assert self.task is not None
        return self.context_class(
            options, gate, self.task, self.persist, self.log
        )

    # -------------------------------------------------- 单次处理
    def assert_target_in_catalog(self, target: str) -> None:
        """请求的目标必须是**自己目录**里的兼容目标之一（不信任 API/页面/请求）。

        这里**重新读一遍目录**，不用 :class:`StatusWriter` 的缓存：缓存可能是几
        分钟前的，而"执行前再核一次"必须是真的再核一次。目录不可用也算拒绝
        （``catalog_unavailable``）：无法证明的目标一律不执行，一个字节都不改。
        """
        block, targets, _ = read_release_catalog(self.cfg, self.pub, self.tool)
        if block["state"] != CATALOG_STATE_OK:
            raise UpdaterError(
                EXIT_REJECTED,
                "catalog_unavailable",
                "prepare",
                f"可信发布目录不可用（{block['reason']}）：无法证明目标是可用目标",
                [
                    "在发行包里校正 deploy/releases.json（catalog.py validate），"
                    "再重新提交；不要手改请求文件。",
                ],
            )
        if any(item["digest"] == target and item["compatible"] for item in targets):
            return
        raise UpdaterError(
            EXIT_REJECTED,
            "target_not_in_catalog",
            "prepare",
            "目标摘要不在可信发布目录的可用目标里（未列出，或与当前版本不兼容）",
            [
                "从网页「目标版本」里重新提交；目标只能来自可信发布目录。",
                "不要手改请求文件：更新器只执行目录里的兼容目标。",
            ],
        )

    def process(self, request_path: Path) -> int:
        request = load_request(request_path)
        target, target_hex, expected_commit = validate_request_identity(self.pub, request)
        request_id = str(request["request_id"])
        if already_processed(self.cfg, request_id):
            raise UpdaterError(
                EXIT_REJECTED, "duplicate_request", "prepare", "该任务 ID 已处理过"
            )
        # 目标必须来自目录：digest 格式合格只是必要条件，**可用性**由目录决定。
        # 这一步在任何 Docker 调用之前（拉取/备份/迁移都还没开始）。
        self.assert_target_in_catalog(target)
        self.task = new_task(self.pub, request, target_hex, expected_commit)
        self.persist()
        self.status.write("busy", self.task, force=True)
        options = self.build_options(request, target_hex)
        gate = MaintenanceGate(
            self.pub, self.cfg.state_dir, request_id, self.compose_sources()
        )
        context = self.build_context(options, gate)
        try:
            return self.run_pipeline(context, gate, request_id)
        finally:
            # **每处理完一个任务再算一次**：成功时备份目录里多了一份新 manifest，
            # 失败时也可能留下不完整产物；下一次请求前网页必须看到最新事实。
            self.refresh_status_after_task()

    def refresh_status_after_task(self) -> None:
        """重算恢复点门禁/可用目标并按任务实况重写状态（含失败/人工门禁的收尾）。"""
        self.status.refresh_backup_gate()
        # 成功后受限 env 已指向目标摘要：当前版本随之变化，目标列表必须重算
        # （否则刚装上的那个版本会继续显示成"可用目标"）。
        self.status.refresh_release_catalog()
        if self.task is None:
            return
        state = {
            TASK_NEEDS_MANUAL: "needs_manual",
            TASK_RUNNING: "busy",
        }.get(str(self.task.get("state")), "idle")
        self.status.write(state, self.task, force=True)

    def run_pipeline(self, context: Any, gate: MaintenanceGate, request_id: str) -> int:
        assert self.task is not None
        pub = self.pub
        self.emit(f"任务 {request_id} 开始：target={context.o.target}")
        try:
            # 跨工具互斥必须是**第一件事**：拿不到就什么都不做（连 docker 变更
            # 探测都不做），也不等待。锁一直持有到验证结束。
            context.acquire_locks()
            self.emit("已拿到跨工具安装锁（.ling-install.lock），直到验证结束才释放")
            context.inspect_previous_containers()
            self.task["previous"]["services"] = {
                name: dict(info) for name, info in context.previous.items()
            }
            self.persist()
            self.verify_expected_current(context)
            context.preflight()
            self.task["previous"]["head"] = context.record["previous"]["alembic_version"]
            self.task["migration"]["current_before"] = self.task["previous"]["head"]
            self.persist()
            gate.render()
            gate.verify(context)
            self.task["maintenance"]["files"] = [str(path) for path in gate.files]
            self.persist()
            self.emit(
                "维护门禁副本已生成并校验："
                + "、".join(path.name for path in gate.files)
                + "（api ports 为空，模型与正式副本一致）"
            )
            context.pull_and_prepare()
            self.task["target"]["commit"] = context.record["target"]["source_commit"]
            self.task["target"]["image_id"] = context.record["target"]["image_id"]
            self.task["migration"]["target_head"] = context.target_head
            self.persist()
            self.task["stage"] = "backup"
            self.task["maintenance"]["state"] = MAINT_ACTIVE
            self.persist()
            context.backup()
            backup = context.record["backup"]
            self.task["backup"].update(
                {
                    "complete": bool(backup.get("complete")),
                    "tombstones_state": backup.get("tombstones_state"),
                    "media_state": backup.get("media_state"),
                    "keys_root": backup.get("keys_root"),
                    "keys_env_set": backup.get("keys_env_set"),
                }
            )
            self.persist()
            self.task["stage"] = "migrate"
            # 写前标记：崩溃在迁移中途时按「可能已迁移」对账（不乐观假设）。
            self.task["migration"]["attempted"] = True
            self.persist()
            context.migrate()
            self.task["migration"]["current_after"] = context.record["migration"][
                "current_after"
            ]
            self.task["migration"]["target_head"] = context.target_head
            self.persist()
            context.switch_to_target()
            context.verify_after_switch()
        except pub.PublishError as error:
            return self.finalize_publish_failure(context, error)
        except UpdaterError as error:
            return self.finalize_updater_failure(error)
        except OSError as error:
            self.emit(f"任务 {request_id} 文件操作失败：{type(error).__name__}")
            wrapped = pub.PublishError(
                pub.EXIT_PREFLIGHT,
                "failed_preflight",
                "io",
                f"文件/目录操作失败：{type(error).__name__}",
                ["检查磁盘空间、权限与挂载；不要根据退出码猜测容器状态。"],
                after_migration=self.task["migration"]["attempted"],
            )
            return self.finalize_publish_failure(context, wrapped)
        except Exception as error:  # noqa: BLE001 - 未预期异常也必须留下记录
            self.emit(f"任务 {request_id} 未预期异常：{type(error).__name__}: {error}")
            after = bool(self.task["migration"]["attempted"])
            wrapped = pub.PublishError(
                pub.EXIT_POST_MIGRATION if after else pub.EXIT_PREFLIGHT,
                "failed_after_migration" if after else "failed_preflight",
                "unexpected",
                f"未预期异常：{type(error).__name__}",
                ["保留任务记录与私有日志；先核对容器/数据库实况。"],
                after_migration=after,
            )
            return self.finalize_publish_failure(context, wrapped)
        finally:
            context.release_locks()
        self.finish_success()
        return EXIT_OK

    def verify_expected_current(self, context: Any) -> None:
        """请求里的「期望当前值」必须与**实测**的当前运行状态一致。"""
        assert self.task is not None
        expected_ref = str(self.task["previous"]["digest"])
        expected_commit = str(self.task["previous"]["commit"])
        match = self.pub.DIGEST_RE.fullmatch(expected_ref)
        expected_hex = match.group(1) if match is not None else ""
        problems: list[str] = []
        for service in ("api", "worker"):
            info = context.previous.get(service)
            if not isinstance(info, dict):
                problems.append(f"{service} 容器实况不可读")
                continue
            digests = {
                str(item).rsplit("@sha256:", 1)[1]
                for item in info.get("repo_digests") or []
                if "@sha256:" in str(item)
            }
            if info.get("image_ref") != expected_ref and expected_hex not in digests:
                problems.append(f"{service} 当前 digest 与请求的期望当前值不一致")
            if info.get("commit") != expected_commit:
                problems.append(f"{service} 当前提交与请求的期望当前值不一致")
        if problems:
            raise self.pub.PublishError(
                self.pub.EXIT_USAGE,
                "failed_request",
                "expected_current",
                "拒绝更新：" + "；".join(problems),
                [
                    "重新检查当前运行版本身份后再提交请求；"
                    "更新器不接受与实况不符的期望值。",
                ],
            )

    def finish_success(self) -> None:
        assert self.task is not None
        self.task["state"] = TASK_SUCCEEDED
        self.task["finished_at"] = self.pub.utc_now()
        self.task["still_on_old_version"] = False
        self.task["next_actions"] = []
        self.persist()
        self.status.write("idle", self.task, force=True)

    def finalize_publish_failure(self, context: Any, error: Any) -> int:
        assert self.task is not None
        self.emit(
            f"任务 {self.task['request_id']} 失败"
            f"（{error.status}/{error.step}）：{error.message}"
        )
        passed = context.handle_failure(error)
        after_migration = bool(
            getattr(error, "after_migration", False)
            or context.migration_attempted
            or self.task["migration"]["attempted"]
        )
        rollback = context.record.get("rollback") or {}
        self.task["rollback"] = {
            "attempted": bool(rollback.get("attempted")),
            "verified": bool(rollback.get("verified")),
        }
        if after_migration:
            enforced = (context.record.get("maintenance") or {}).get("enforced")
            self.task["state"] = TASK_NEEDS_MANUAL
            self.task["still_on_old_version"] = False
            self.task["maintenance"]["state"] = (
                MAINT_ACTIVE
                if enforced in {"closed", "already_closed"} or context.host_ports_published
                else MAINT_UNKNOWN
            )
            self.task["failure"] = {
                # 已迁移后的失败基本都是"启动/验证"，但 env 写入失败要单独说清
                # （处置方式不同），由 failure_code 区分。
                "code": failure_code(error),
                "stage": str(getattr(error, "step", "switch")),
            }
            self.task["next_actions"] = ["inspect_maintenance", "review_private_log"]
        else:
            verified = bool(rollback.get("verified")) and context.published_port is not None
            unmeasured = context.published_port is None
            # 「没动过任何服务」的失败（请求被拒、预检不通过）不需要人工门禁：
            # 既没停容器也没迁移，下一次请求可以正常再试。状态仍然不写
            # still_on_old_version=true —— 没实测就不能那样说（null=未实测）。
            no_changes = not (
                getattr(context, "stopped_api", False)
                or getattr(context, "stopped_worker", False)
                or context.migration_attempted
                or self.task["migration"]["attempted"]
            )
            settled = verified or no_changes
            self.task["state"] = TASK_FAILED_NOT_STARTED if settled else TASK_NEEDS_MANUAL
            self.task["still_on_old_version"] = (
                True if verified else (None if unmeasured else False)
            )
            self.task["maintenance"]["state"] = (
                MAINT_RELEASED if settled else MAINT_UNKNOWN
            )
            self.task["failure"] = {
                "code": failure_code(error),
                "stage": str(getattr(error, "step", "prepare")),
            }
            self.task["next_actions"] = (
                ["no_action_needed"]
                if verified
                else ["check_old_service", "review_private_log"]
            )
        self.task["finished_at"] = self.pub.utc_now()
        self.persist()
        self.status.write(
            "needs_manual" if self.task["state"] == TASK_NEEDS_MANUAL else "idle",
            self.task,
            force=True,
        )
        return passed

    def finalize_updater_failure(self, error: UpdaterError) -> int:
        assert self.task is not None
        self.emit(f"任务 {self.task['request_id']} 被更新器拒绝：{error.detail}")
        manual = error.exit_code == EXIT_NEEDS_MANUAL
        self.task["state"] = TASK_NEEDS_MANUAL if manual else TASK_FAILED_NOT_STARTED
        self.task["failure"] = {"code": error.code, "stage": error.stage}
        self.task["next_actions"] = list(error.next_actions) or ["review_private_log"]
        self.task["finished_at"] = self.pub.utc_now()
        self.persist()
        self.status.write("needs_manual" if manual else "idle", self.task, force=True)
        return error.exit_code

    # -------------------------------------------------- 重启对账
    def reconcile(self) -> int:
        """上次进程没写完任务：**持跨工具安装锁**再按实况对账。

        对账会重新关门/恢复旧服务，同样是「改容器」的动作，必须和
        install.sh / publish.py 互斥；拿不到锁就一轮报错退出（不等待），
        下一轮再试——期间一步都不动。
        """
        lock = acquire_install_lock(self.pub, self.cfg.env_file)
        try:
            return self._reconcile_locked()
        finally:
            lock.release()

    def _reconcile_locked(self) -> int:
        """只按实况对账，绝不继续原来的阶段。"""
        assert self.task is not None
        task = self.task
        pub = self.pub
        self.emit(
            f"发现未完成任务 {task['request_id']}（阶段 {task['stage']}），开始按实况对账"
        )
        self.status.write("busy", task, force=True)
        gate = MaintenanceGate(
            pub, self.cfg.state_dir, str(task["request_id"]), self.compose_sources()
        )
        gate.files = [Path(item) for item in (task["maintenance"].get("files") or [])]
        previous = task.get("previous") or {}
        target = str((task.get("target") or {}).get("digest") or "")
        target_hex = str((task.get("target") or {}).get("digest_hex") or "")
        options = self.build_options(
            {
                "request_id": task["request_id"],
                "target_digest": target,
                "expected_current_digest": previous.get("digest") or target,
                "expected_current_commit": previous.get("commit") or "",
            },
            target_hex,
        )
        context = self.build_context(options, gate)
        services = previous.get("services") or {}
        if isinstance(services, dict) and services:
            context.previous = {str(k): dict(v) for k, v in services.items()}
        context.previous_ref = previous.get("digest")
        context.previous_commit = previous.get("commit")
        context.record["previous"]["api"] = context.previous.get("api")
        context.record["previous"]["worker"] = context.previous.get("worker")
        context.record["previous"]["alembic_version"] = previous.get("head")

        head: str | None = None
        head_unknown = False
        try:
            head = context.read_current_alembic_version(record_previous=False)
        except pub.PublishError as error:
            head_unknown = True
            self.emit(f"对账：数据库版本不可读（{error.step}）")

        running: dict[str, bool] = {}
        for service in ("api", "worker"):
            try:
                containers = context.list_containers(service=service, all_states=True)
            except pub.PublishError:
                containers = []
            running[service] = bool(
                [item for item in containers if item.get("state") not in {"exited", "created"}]
            )
        try:
            config = context.compose_config(
                None,
                step="preflight",
                exit_code=pub.EXIT_PREFLIGHT,
                status="failed_preflight",
            )
            context.published_port, context.ready_host = published_port_from_config(config)
        except pub.PublishError:
            self.emit("对账：compose 模型不可读，宿主端口未知")

        migrated = bool(task["migration"]["attempted"]) or (
            head is not None
            and previous.get("head") is not None
            and head != previous.get("head")
        )
        if head_unknown and not task["migration"]["attempted"]:
            self.emit("对账：无法确认是否迁移过，停在人工门禁（不做任何变更）")
            return self.mark_manual(
                "reconcile_needs_manual",
                "reconcile",
                ["inspect_maintenance", "review_private_log"],
                maintenance=MAINT_UNKNOWN,
                rollback={"attempted": False, "verified": False},
            )
        if migrated:
            self.emit("对账：确认迁移已开始/完成，不切旧镜像；保持维护并转人工")
            context.host_ports_published = False
            if context.published_port is not None:
                try:
                    context.host_ports_published = bool(context.published_port_holders())
                except pub.PublishError:
                    context.host_ports_published = False
            try:
                context.enforce_maintenance_gate()
            except pub.PublishError as error:
                self.emit(f"对账：重新关门失败（{error.step}）")
            enforced = (context.record.get("maintenance") or {}).get("enforced")
            task["maintenance"]["state"] = {
                "closed": MAINT_ACTIVE,
                "already_closed": MAINT_ACTIVE,
            }.get(str(enforced), MAINT_UNKNOWN)
            # 已迁移：旧代码很可能不认识新 schema，绝不是"仍在旧版本"。
            task["still_on_old_version"] = False
            return self.mark_manual(
                "switch_failed",
                "reconcile",
                ["inspect_maintenance", "review_private_log"],
                maintenance=task["maintenance"]["state"],
                rollback={"attempted": False, "verified": False},
            )

        # 确认没迁移：允许恢复并**实测**旧服务（设计 §6 第一行）。
        self.emit("对账：确认未迁移，尝试恢复/实测旧服务")
        context.stopped_api = running.get("api", False)
        context.stopped_worker = running.get("worker", False)
        try:
            rollback = context.attempt_restore_previous()
        except Exception as error:  # noqa: BLE001 - 恢复异常也要落地成人工门禁
            rollback = {
                "attempted": True,
                "verified": False,
                "detail": f"恢复异常：{type(error).__name__}",
            }
        task["rollback"] = {
            "attempted": bool(rollback.get("attempted")),
            "verified": bool(rollback.get("verified")),
        }
        self.emit(f"对账：恢复结果={rollback.get('detail')}")
        verified = bool(rollback.get("verified")) and context.published_port is not None
        if not verified:
            return self.mark_manual(
                "old_service_unverified",
                "reconcile",
                ["check_old_service", "review_private_log"],
                maintenance=MAINT_UNKNOWN,
                rollback=task["rollback"],
            )
        task["state"] = TASK_FAILED_NOT_STARTED
        task["still_on_old_version"] = True
        task["maintenance"]["state"] = MAINT_RELEASED
        task["failure"] = {"code": "reconcile_needs_manual", "stage": "reconcile"}
        task["next_actions"] = ["review_private_log"]
        task["finished_at"] = pub.utc_now()
        self.persist()
        self.status.write("idle", task, force=True)
        self.emit("对账：旧服务实测正常，任务按「仍在旧版本」收口（未新建任务）")
        return EXIT_OK

    def mark_manual(
        self,
        code: str,
        stage: str,
        next_actions: Sequence[str],
        *,
        maintenance: str,
        rollback: dict[str, Any],
    ) -> int:
        assert self.task is not None
        self.task["state"] = TASK_NEEDS_MANUAL
        self.task["failure"] = {"code": code, "stage": stage}
        self.task["next_actions"] = list(next_actions)
        self.task["maintenance"]["state"] = maintenance
        self.task["rollback"] = rollback
        self.task["finished_at"] = self.pub.utc_now()
        self.persist()
        self.status.write("needs_manual", self.task, force=True)
        return EXIT_NEEDS_MANUAL


def failure_code(error: Any) -> str:
    status = str(getattr(error, "status", ""))
    step = str(getattr(error, "step", ""))
    # env 写入失败要和「启动/验证失败」分开：处置方式不同（先修受限 env 再重启），
    # 网页也要能直接说清是哪一步。
    if step == "env_update":
        return "env_write_failed"
    # 迁移阶段的失败（status=failed_migration：迁移命令非 0、跳过迁移、迁移后
    # head 不符…）发生在**启动新 API 之前**，处置是核对迁移日志/schema，不是查
    # 新版本为什么起不来。这一支必须先于 after_migration，否则所有已迁移失败都
    # 被归成 switch_failed，migration_failed 不可达。
    if status == "failed_migration":
        return "migration_failed"
    if getattr(error, "after_migration", False) or status == "failed_after_migration":
        return "switch_failed"
    # 更新器自己的两个门禁有专用码表：它们不是"发布预检"的一部分，
    # 网页/操作员需要看到具体是「维护门禁」还是「期望当前值不符」。
    if step in {"maintenance_gate", "maintenance_gate_render"}:
        return "maintenance_gate_failed"
    if step == "expected_current":
        return "expected_current_mismatch"
    return {
        "failed_request": "expected_current_mismatch",
        "failed_usage": "request_rejected",
        "failed_preflight": "preflight_failed",
        "failed_pull": "pull_failed",
        "failed_backup": "backup_failed",
    }.get(status, "internal_error")


# ---------------------------------------------------------------- 主循环


def acknowledge_manual(
    cfg: Config, pub: ModuleType, tool: ModuleType | None, request_id: str
) -> int:
    store = TaskStore(cfg, pub)
    status = StatusWriter(cfg, pub, tool)
    try:
        task = store.read()
    except UpdaterError as error:
        print(f"[updater] {error.detail}", file=sys.stderr)
        return error.exit_code
    if task is None:
        print("[updater] 没有任务记录，无需解除", file=sys.stderr)
        return EXIT_REJECTED
    if task.get("state") != TASK_NEEDS_MANUAL:
        print(
            f"[updater] 任务 {task.get('request_id')} 不在 needs_manual 状态",
            file=sys.stderr,
        )
        return EXIT_REJECTED
    if task.get("request_id") != request_id:
        print("[updater] request_id 不匹配，拒绝解除", file=sys.stderr)
        return EXIT_REJECTED
    task["state"] = TASK_RESOLVED
    task["finished_at"] = pub.utc_now()
    task["next_actions"] = []
    store.write(task)
    status.write("idle", task, force=True)
    print(f"[updater] 已解除人工门禁：{request_id}")
    return EXIT_OK


def run_once(updater: Updater) -> int:
    """单飞：整个「读任务 + 认领 + 处理 + 对账」都在同一把锁里。"""
    cfg = updater.cfg
    lock = TaskLock(cfg)
    if not lock.acquire():
        updater.emit("另一个更新器实例正在处理任务（单飞），本次不做任何事")
        return EXIT_BUSY
    try:
        try:
            task = updater.store.read()
        except UpdaterError as error:
            updater.emit(f"任务状态不可读：{error.detail}")
            updater.status.write("needs_manual", None, force=True)
            return error.exit_code
        updater.task = task
        if task is not None and task.get("state") == TASK_RUNNING:
            return updater.reconcile()
        if task is not None and task.get("state") == TASK_NEEDS_MANUAL:
            updater.status.write("needs_manual", task)
            return EXIT_NEEDS_MANUAL
        candidates = pending_requests(cfg)
        if not candidates:
            updater.status.write("idle", task)
            return EXIT_OK
        claimed = claim_request(cfg, candidates[0])
        if claimed is None:
            updater.emit("请求已被另一个实例认领")
            return EXIT_BUSY
        updater.emit(f"认领请求 {claimed.name}")
        try:
            return updater.process(claimed)
        except UpdaterError as error:
            updater.emit(f"拒绝请求：{error.detail}")
            updater.status.write(
                "needs_manual" if error.exit_code == EXIT_NEEDS_MANUAL else "idle",
                updater.task,
                force=True,
            )
            return error.exit_code
        except Exception as error:  # noqa: BLE001 - 认领后的未预期异常不能静默
            updater.emit(f"处理请求时出现未预期异常：{type(error).__name__}: {error}")
            updater.status.write("needs_manual", updater.task, force=True)
            return EXIT_NEEDS_MANUAL
        finally:
            archive_claim(cfg, claimed)
    finally:
        lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    try:
        cfg = parse_config(argv)
        ensure_layout(cfg)
        pub = load_publish(cfg.publish_py)
    except UpdaterError as error:
        print(f"[updater] {error.detail}", file=sys.stderr)
        for action in error.next_actions:
            print(f"[updater] 下一步：{action}", file=sys.stderr)
        return error.exit_code
    # 目录工具加载失败**不**让更新器退出：只影响"有没有可用目标"，状态文件仍要
    # 写出来并带上 catalog_tool_missing 的原因（否则网页只能看到"暂无目标"）。
    tool = load_catalog_tool(cfg.catalog_tool)
    if cfg.acknowledge:
        return acknowledge_manual(cfg, pub, tool, cfg.acknowledge)
    updater = Updater(cfg, pub, tool)
    while True:
        try:
            code = run_once(updater)
        except UpdaterError as error:
            updater.emit(f"更新器无法继续：{error.detail}")
            with contextlib.suppress(UpdaterError):
                updater.status.write("needs_manual", updater.task, force=True)
            code = error.exit_code
        if cfg.once:
            return code
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
