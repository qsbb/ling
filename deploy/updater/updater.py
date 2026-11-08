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

刷新请求（「检查更新」/按需刷新）
================================
同一个请求目录里还有**第二种、前缀不同**的一次性文件（同样是临时文件 +
``rename``）::

    <requests-dir>/refresh-<32 位小写 hex>.json
    {"schema": 1, "request_id": "<32 位小写 hex>", "kind": "refresh",
     "created_at": "2026-11-04T00:00:00Z"}

- 它**只**触发一次只读刷新：查一次 GHCR 的 ``tags/list``、解析最新稳定版 tag 的
  digest、读运行中 api 镜像的版本标注、读更新说明。不拉镜像、不备份、不迁移、
  不切容器，也不改任何 compose/env。
- 与安装请求的区别只有一处：多余的键**忽略并记键名**（刷新是只读的，多一个无关
  字段不该让用户的检查失败）；``schema``/``kind``/ID/时间格式仍然严格。
  设计文档里的 ``check_id``/``requested_at`` 与这里的 ``request_id``/``created_at``
  是同一件事的两个名字，两种写法都认（API 与更新器是两个镜像、并行开发，认错一个
  名字的后果是"点了检查没反应"）。
- 幂等：处理过的 ID 记在 ``<state-dir>/refresh.json``（0600），同一个 ID 再写回来
  不会再查一次；处理完请求文件**删除**。
- 限流：请求触发的刷新在 ``--refresh-throttle-seconds``（默认 10 秒）内最多真正
  处理一次；多余的请求文件直接删掉，并把它记成
  ``release_check.reason_detail="throttled"``——但 **``state``/``latest``/``current``
  一个字节都不动**（这次没查，就不能改上一次的结论）。自动刷新（启动/每 300 秒）
  **不**计入这个窗口。
- 旧更新器（0.0.3）只匹配 ``request-*``，所以它看见 ``refresh-*`` 会原样忽略：
  不认领、不消费、不阻塞安装通道。

状态协议
========
``<status-file>``（默认 ``<state-dir>/status.json``，原子写、0644）字段固定，
**不含**密码、原始日志、备份路径、compose/env 路径、registry 主机或 tag::

    {
      "schema": 1, "protocol": 1, "updated_at": "...",
      "updater": {"state": "idle|busy|needs_manual", "version": "1.0.0"},
      "task": null | { ... 见下 ... },
      "backup_gate": { ... 见下 ... },
      "release_catalog": {            # **只读兼容**：不再读目录文件，见下节
        "state": "unused", "reason": null,
        "entries": 0, "current_version": "0.0.3" | null,
        "source": "registry", "checked_at": "2026-11-09T01:00:00Z"
      },
      "release_check": {              # 版本发现的**唯一**事实来源
        "state": "ok|failed|unknown",
        "checked_at": "2026-11-09T00:00:00Z" | null,
        "check_id": "<32 位小写 hex>" | null,   # 这次检查由哪条刷新请求触发
        "source": "registry",
        "reason": null | "registry_unreachable|auth_failed|rate_limited|timeout|
                 no_stable_tags|manifest_failed|digest_missing|platform_missing",
        "reason_detail": null | "throttled",
        "current": {"version": "0.0.3" | null, "digest": "sha256:..." | null},
        "latest": {"version": "0.0.4", "digest": "sha256:...",
                   "commit": null} | null,
        "tag_count": 12
      },
      "available_targets": [          # 旧形状，内容由 registry 结果推导
        {"version": "0.0.4",
         "digest": "ghcr.io/qsbb/ling-api@sha256:..." | null,
         "commit": null,
         "has_migration": true,       # 恒 true：迁移前向，入口总跑 migrate
         "compatible": true|false,
         "incompatible_reason": null | "current_version_unknown|digest_unresolved"}
      ],
      "recommended_target": null | {"version": "...", "digest": "...",
                                    "commit": null, "has_migration": true},
      "release_notes": {              # 只放 latest 那一版
        "state": "ok|missing|invalid",
        "reason": null | "notes_invalid|notes_unreadable|notes_too_large|
                 notes_tool_missing",
        "notes": {"0.0.4": {"summary": "...", "items": ["..."],
                            "url": "https://..."|null}}
      }
    }

``release_check`` 的硬规则（设计 §3/§6）
--------------------------------------
1. **只有**完整拿到 tags（允许空页）且选定 tag 的 digest 解析成功，``state`` 才能
   是 ``ok``；任何一步失败都是 ``failed`` + 固定 ``reason``。**绝不**把失败写成
   "已是最新"：``ok`` 且 ``available_targets=[]`` 才等于「已是最新」。
2. ``current.version`` 只来自**运行中 api 镜像**的 ``org.opencontainers.image.version``
   标注，读不到就是 ``null``（**不**拿 pyproject/``.env`` 猜）。``current.digest``
   同理来自该镜像的 ``RepoDigests``（``sha256:<hex>`` 形状）。
3. ``release_check.latest.digest`` 是 registry 的 digest（``sha256:<hex>``）；
   ``available_targets[].digest`` 是**旧形状**的完整镜像引用
   （``ghcr.io/qsbb/ling-api@sha256:<hex>``），因为 API 侧按那个形状校验并直接
   写进安装请求。
4. ``check_id`` 只在这次检查由 ``refresh-<id>.json`` 触发时回显那个 ID；自动
   刷新写 ``null``。被限流丢弃的请求也会把 ``check_id`` 回显上去（页面据此知道
   "我这次点击有结论了"），但 ``state`` 保持上一次的值。

``available_targets`` / ``recommended_target`` 仍是旧 API 读得懂的形状
--------------------------------------------------------------------
内容改为由 registry 结果推导（见「版本发现」一节），但字段名/类型不变：
``compatible`` = 版本比 ``current`` 新**且** digest 非空；``has_migration`` 恒
``true``（迁移是前向的，更新入口总会跑 migrate，不再由发布目录人工开关）；
``commit`` 恒 ``null``（registry 的 tag 列表不带提交号，拉取后由 publish 用镜像
标注核 ``LING_COMMIT == OCI revision``）。``commit=null`` 会让 **0.0.3 的 API**
把这一条整条丢掉（它要求 40 位 hex），结果是旧页面显示"暂无可用目标"——
**不崩、不误报已是最新**，这正是旧 API 该有的降级行为。

``release_catalog`` 是**只读兼容**字段
--------------------------------------
目录（``deploy/releases.json``）不再参与任何判定，也不再被读取。为了不把旧 API
的目录状态码伪造为"目录正常"（设计 §7），这个键仍然存在，但 ``state`` 是
``unused``（两边码表都不认的值 → 都按"未知"处理），``entries`` 恒 0，
``current_version``/``checked_at`` 照实给。``catalog.py`` 与
``deploy/releases.json`` 的解析/校验代码暂时留着（桥接期旧更新器还要用），
但**更新器不再依赖它们**。

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

版本从哪来（GHCR，不再有发布目录）
==================================
「有没有新版本」只问**一处**：固定的 ``ghcr.io/qsbb/ling-api``（
:mod:`deploy.updater.registry`，纯标准库）。没有 ``deploy/releases.json``、没有
``min_from``、没有版本跨度校验，也没有"谁能改仓库 main 谁就能授权升级"的
间接信任锚：能往这个包里推 stable tag 的人，本来就能决定用户装什么。

发现规则（:func:`read_release_check`）：

1. ``GET /v2/qsbb/ling-api/tags/list``，401 时按 ``WWW-Authenticate`` 的 Bearer
   challenge 取**匿名** token 重试，分页按 ``Link`` 跟（最多 5 页），整个检查
   共用一条总超时（默认 8 秒）。
2. 只接受**规范化**的 ``vMAJOR.MINOR.PATCH``：``latest``、``sha-*``、预发布
   （``-rc.1``）、前导零写法一律忽略；按 semver 取最大。
3. 对选定的 tag 发 ``HEAD .../manifests/v<ver>``（Accept 同时带 OCI index /
   Docker manifest list / OCI manifest / Docker manifest），取
   ``Docker-Content-Digest``；HEAD 不可用（405/501）才退回 GET（限流读）。多架构
   index 再核「宿主平台存在」（缺 → ``platform_missing``）。
4. **只有拿全了 tag 且 digest 解析成功**才返回 ``state=ok``；任何一步失败都是
   ``failed`` + 固定原因码。部分结果一律不许当成"完整"。

**当前版本**读**运行中 api 容器**的镜像标注 ``org.opencontainers.image.version``
（:func:`current_running_image`），``current.digest`` 取同一镜像的 ``RepoDigests``。
为什么不是 ``.env`` 里的 ``LING_APP_IMAGE``：它可以被手改、也可能改了还没重建
容器；而运行中的容器本来就是"现在到底跑的是哪一版"的实测事实。读不到标注 /
容器不在运行 / 标注不是严格 semver → ``current.version=null``（**不猜**），
目标一律 ``compatible=false`` + ``current_version_unknown``。

投影成旧形状（:func:`registry_targets`）：**只允许升级**（版本不高于当前版本的
不出现在 ``available_targets`` 里），摘要非空才 ``compatible=true``，
``has_migration`` 恒 true（迁移前向，入口总跑 migrate），``commit`` 恒 null。

安装前复核（设计 §3 末段）
--------------------------
``prepare`` 阶段在真正 ``pull`` 之前**重查 registry**：重新列一次 tag，从最新
稳定版往下逐个解析 digest（最多 :data:`MAX_DRIFT_PROBE_TAGS` 个），要求请求里的
digest **完全相同**才继续——直接拒绝的失败码是 ``target_digest_drift``（
"安装包已变化，请重新检查更新"），**绝不**退回"按 tag 拉"。安装引用自始至终是
``ghcr.io/qsbb/ling-api@sha256:...``，日志与发布记录里不出现 ``:vX.Y.Z`` 形式的
安装引用（tag 只在只读查询的 URL/参数里出现）。

更新说明（``deploy/update-notes.json`` + 可选 GitHub Release）
============================================================
说明**只是展示**，不参与"能不能更新"的判定：

1. 读发行包自带的那份 ``update-notes.json``（与 ``--release-catalog`` 同目录，
   容器里就是 ``/srv/ling-code/update-notes.json``），用发行包
   ``catalog.py`` 的 ``parse_notes`` 严格校验（结构/长度/URL 协议）；
2. 那份文件里没有 **latest** 这一版时，可选再试 GitHub Release 的**精确 tag**
   （``/repos/qsbb/ling/releases/tags/v<ver>``，超时 5 秒）——失败只影响说明；
3. 结果只写 ``release_notes.notes`` 里**latest 那一版**，形状固定。

更新说明取不到**绝不**改变 ``release_check``/目标判定。

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

# 同目录的 registry 客户端（``deploy/updater/registry.py``，纯标准库）。要动
# ``sys.path`` 的原因：这个文件既被当脚本跑（``python3 /app/updater.py``，脚本目录
# 天然在 ``sys.path[0]``），也被测试按路径用 ``importlib`` 加载（那时**不在**
# sys.path 里）。把脚本自己的目录补进去是两种加载方式都成立的最小做法。
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import registry  # noqa: E402  （必须在 sys.path 调整之后）

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

# ---------------------------------------------------------------- 刷新请求协议
#
# 「检查更新」必须让更新器**现在**去问一次 registry；只重读 ``status.json`` 不算
# 检查（设计 ``docs/UPDATE-UX-DESIGN.md``）。API 不碰 Docker/registry，它只往
# **同一个**请求目录写一个刷新请求文件，更新器在同一把单飞锁里认领它。
#
# 文件名 ``refresh-<32 hex>.json``（与安装请求 ``request-<32 hex>.json`` 同目录、
# 同原子写：临时文件 + ``rename``；安装请求的匹配规则**只有** ``request-*``，
# 所以旧更新器看见 ``refresh-*`` 会原样忽略，安装通道完全不受影响）。
# 内容白名单极简::
#
#     {"schema": 1, "request_id": "<32 位小写 hex>", "kind": "refresh",
#      "created_at": "2026-11-04T00:00:00Z"}
#
# 两种字段写法都接受（同一件事的两个名字）：设计文档的按需检查协议写的是
# ``check_id`` / ``requested_at``，更新器的旧协议写的是 ``request_id`` /
# ``created_at``。**刻意不在这两个名字之间做二选一**：API 与更新器是两个镜像、
# 由不同的人并行改，认错一个名字的后果是「点了检查没反应」，而那是最难排查的一类
# 故障。除了这两个别名，其它键一律**忽略**并把键名记进私有日志（不崩、不拒）：
# 刷新是只读动作，多一点无关字段不该让用户的检查失败。
REFRESH_PREFIX = "refresh-"
REFRESH_KIND = "refresh"
REFRESH_SCHEMA = 1
REFRESH_KEYS = frozenset(
    {"schema", "request_id", "kind", "created_at", "check_id", "requested_at"}
)
# 严格 RFC3339（允许小数秒与 Z/偏移两种时区写法）。时间戳不参与任何判定，
# 但**不能是自由文本**：状态文件里的字段形状必须确定。
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
# 请求触发的刷新也要限流：页面被连点时，每一下都去列一次 tag + 解析一次 manifest
# 会把 GHCR 通道拖死（匿名也有速率上限）。10 秒内最多真正处理一次；多余的请求
# 文件直接删掉，结果记在 ``release_check.reason_detail="throttled"``——
# **不改 state**：这次没查，就不能拿它改写上一次的结论。
REFRESH_THROTTLE_SECONDS = 10.0
# 幂等用的记忆：处理过的 request_id 记在 ``<state-dir>/refresh.json`` 里。
# 只留最近这些个（请求文件本来就会被删掉，这里只是防"同一 ID 又被写回来"）。
REFRESH_STATE_NAME = "refresh.json"
REFRESH_STATE_SCHEMA = 1
MAX_REMEMBERED_REFRESHES = 64

# ``release_check.reason_detail`` 目前只有这一个值（其余情况是 null）。
REASON_DETAIL_THROTTLED = "throttled"
# 刷新请求本身畸形时的**内部**错误码（只进私有日志与 ``UpdaterError.code``，
# **不**写进状态文件：``release_check`` 只放 registry 的 8 个原因码）。
REFRESH_REASON_MALFORMED = "refresh_malformed"

STAGES = ("prepare", "backup", "migrate", "switch", "verify")

TASK_RUNNING = "running"
TASK_SUCCEEDED = "succeeded"
TASK_FAILED_NOT_STARTED = "failed_not_started"
TASK_NEEDS_MANUAL = "needs_manual"
TASK_RESOLVED = "resolved"

MAINT_ACTIVE = "active"
MAINT_RELEASED = "released"
MAINT_UNKNOWN = "unknown"

# ---------------------------------------------------------------- 版本发现（GHCR）
#
# 见模块 docstring「版本从哪来（GHCR，不再有发布目录）」。这里**没有**可配置的
# registry/镜像名/tag：请求方（网页/API/请求文件）无法影响发现目标。唯一注入点
# 是 ``registry.REGISTRY_ENV``（测试把 base URL 指到本地 stub）。
#
# 一次检查的 ``state``：只有完整拿到 tag 列表**且**选定 tag 的 digest 解析成功
# 才是 ``ok``；``failed`` 带固定 ``reason``；``unknown`` 表示"还没有查过"
# （构造完必然立刻查一次，所以它只出现在进程内的占位状态）。
RELEASE_CHECK_OK = "ok"
RELEASE_CHECK_FAILED = "failed"
RELEASE_CHECK_UNKNOWN = "unknown"
# ``release_check.source``：现在是、将来也只有 registry 这一个事实源。
RELEASE_CHECK_SOURCE = "registry"
# 原因码锁定在 registry 客户端的 8 个（不在这里另立一套）。
RELEASE_CHECK_REASONS = registry.REASONS
# 空闲时重查 registry 的间隔（秒）。发版是低频事件，5 分钟足够；用户点
# 「检查更新」走 refresh 请求，**绕过**这个节流。
CHECK_INTERVAL_SECONDS = 300.0
# 安装前复核最多从最新 tag 往下探多少个（见 :func:`Updater.assert_target_digest_current`）。
# 只允许升级 → 正常情况第一次就命中；8 个是"旧页面上的候选"能容忍的纵深上限。
MAX_DRIFT_PROBE_TAGS = 8

# 发行包里目录文件/校验工具的名字。**目录不再参与判定**，但这两条路径还在用：
# `--release-catalog` 的默认值与 `update-notes.json` 的同目录定位（发行包布局）。
RELEASE_CATALOG_NAME = "releases.json"
RELEASE_CATALOG_TOOL = "catalog.py"
# ``release_catalog`` 这个顶层键是**只读兼容**：目录不再被读取，所以它的 state 既
# 不能写 "ok"（那是伪造"目录正常"），也不用旧码表的任何值——两边码表都不认
# ``unused``，都按"未知"处理。
RELEASE_CATALOG_UNUSED = "unused"
# 「现在跑的是哪一版」只认这个 OCI 标注（`deploy/Dockerfile.api` 从
# `LING_VERSION` 写进去，与 pyproject / `ling.__version__` 同源）。
VERSION_LABEL = "org.opencontainers.image.version"

# ---------------------------------------------------------------- 更新说明
#
# 说明**只是展示**，不参与"能不能更新"的判定。首选发行包自带那份
# （``update-notes.json``，与 ``--release-catalog`` 同目录），它有就用；里面没有
# latest 这一版时，可选再试 GitHub Release 的**精确 tag**（不用 releases/latest：
# 那会把"最新发布"冒充成"你要装的那一版"）。
NOTES_NAME = "update-notes.json"
NOTES_STATE_OK = "ok"
NOTES_STATE_MISSING = "missing"
NOTES_STATE_INVALID = "invalid"
# ``release_notes.reason`` 的固定码（文案由 API 自己那份码表翻译）。
NOTES_REASON_TOOL_MISSING = "notes_tool_missing"
NOTES_REASON_UNREADABLE = "notes_unreadable"
NOTES_REASON_TOO_LARGE = "notes_too_large"
NOTES_REASON_INVALID = "notes_invalid"
NOTES_REASON_BY_CODE = {
    "too_large": NOTES_REASON_TOO_LARGE,
    "unreadable": NOTES_REASON_UNREADABLE,
}
# GitHub Release 的固定 endpoint（仓库固定；**不接受**请求方给 URL/仓库/tag）。
RELEASE_NOTES_API_BASE = "https://api.github.com"
RELEASE_NOTES_REPO = "qsbb/ling"
# **只给测试用**的 API base 注入点（把请求指向本地 stub）。生产不设 → 上面那个。
RELEASE_NOTES_API_ENV = "LING_UPDATER_RELEASE_API"
# 取 Release 的超时（秒）与响应上限。它只影响"有没有说明"，卡住不能拖慢检查。
RELEASE_NOTES_API_TIMEOUT_SECONDS = 5.0
MAX_RELEASE_BODY_BYTES = 256 * 1024
# 上限与 ``deploy/release/catalog.py`` 的 update-notes 门禁一致：一条摘要 ≤120 字，
# 最多 8 条、每条 ≤120 字，URL ≤512 字符。**不是**为了"好看"，而是状态文件整个
# 要压进 API 的 16 KiB 读取上限里。
MAX_NOTE_ITEMS = 8
MAX_NOTE_SUMMARY_CHARS = 120
MAX_NOTE_ITEM_CHARS = 120
MAX_NOTE_URL_CHARS = 512

# 状态文件的读取上限：API 用 ``read_json_file`` 读 ``status.json``，硬上限是
# ``MAX_JSON_BYTES = 16 KiB``（``src/ling/updates/protocol.py``，0.0.3 起就有）。
# 超了不是"部分字段坏了"，而是**整份状态对 API 不可读**——所以在写之前给自己一个
# 比它低的预算，留出余量。
#
# 当前上限下这份文件不会接近预算：一条最大说明（120 字摘要 + 8×120 字条目 +
# 512 字符链接）加一份最大 task 视图，见 ``tests/test_updater.py`` 的
# ``test_status_file_stays_under_the_api_read_limit``。
# 预算是**兜底**：将来谁放宽条目上限或往状态里塞胖字段，这里会把更新内容丢掉，
# 而不是让整份状态消失。
STATUS_READ_LIMIT_BYTES = 16 * 1024
STATUS_NOTES_BUDGET_BYTES = 12 * 1024
# 目标不可用的原因（API 按自己那份码表翻译）。注意这里**没有** not_newer：
# 版本不高于当前的目标根本不出现在 available_targets 里（只允许升级），而不是
# 出现在列表里标成"不兼容"——否则页面会给出一个可以直接点降级的目标。
# ``not_in_min_from`` 随目录一起删除了（不再有版本跨度校验）。
INCOMPATIBLE_CURRENT_VERSION_UNKNOWN = "current_version_unknown"
# 版本可比较、确实更新，但**摘要解析不出来**：只这一个码，不为"tag 不存在 /
# index 里没有本机平台 / digest 格式不对"各开一个——对用户是同一件事：这条目标
# 现在装不了。
INCOMPATIBLE_DIGEST_UNRESOLVED = "digest_unresolved"
# 请求的目标摘要**不是**该 tag 现在指向的摘要（tag 被重推 / 用户拿的是过期候选）。
# 安装前复核直接拒绝，绝不"按 tag 拉"。
FAILURE_TARGET_DIGEST_DRIFT = "target_digest_drift"
# 安装前需要重新问 registry，但这次没问成（不可达/限流/超时…）：无法证明就**不装**。
FAILURE_REGISTRY_UNAVAILABLE = "registry_unavailable"

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
    # ``target_digest_drift`` / ``registry_unavailable`` 刻意**不**进这张表：
    # 它们只可能在 ``new_task`` 之前抛出（复核不通过就没有任务），所以没有任何
    # 任务视图会拿它们查文案；而这张表与 API 侧 ``IMAGE_FAILURE_MESSAGES`` 由
    # ``test_failure_code_tables_match_api_side`` 强制逐字一致——API 那边还没有这两个
    # 码，先加在这里只会把一条跨侧一致性用例打红。API 侧落地这两个码时，两边一起加。
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
        # 目录里没有 digest 之后，摘要解析要复用发行包的仓库常量与 Runner：
        # 「去哪个仓库问」和「怎么跑 docker」都不该在更新器里另立一套。
        "IMAGE_REPOSITORY",
        "Runner",
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
    """按**文件路径**加载发行包里的 ``catalog.py``（现在只用它校验更新说明）。

    与 :func:`load_publish` 同一个做法、同一个理由：更新器用的必须是**操作员装的
    那份代码**（信任锚），不是 site-packages 里的同名模块，也不是自己另写一套
    校验——两套规则迟早会不一致，而不一致的那一份就是后门。

    **目录不再参与更新判定**（版本的唯一事实源是 GHCR，见模块 docstring），所以
    这个模块现在只提供 ``parse_notes``/``load_notes``：``update-notes.json`` 的
    结构/长度/URL 门禁。目录解析与 ``min_from`` 校验代码暂时留在发行包里（桥接期
    旧更新器还要用），但更新器不再调用它们。

    加载失败**不**在启动时退出：没有更新说明只影响"这一版改了什么"，不该让更新器
    连状态都不写。这里返回 None，由 :func:`read_release_notes` 记
    ``notes_tool_missing``。
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
    required = ("parse_notes", "load_notes")
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
    # 发行包里 ``releases.json`` 的路径。**目录已经不被读取**：这个路径现在只用来
    # 定位同目录的 ``update-notes.json``（更新说明），并保留发行包的目录布局。
    # 数据路径可由 ``--release-catalog`` 覆盖（默认 ``<publish-py>/../../
    # releases.json``，容器里就是 /srv/ling-code/releases.json）。
    release_catalog: Path
    # 发行包里的 ``catalog.py``（按文件路径加载，信任锚来自"操作员装的那份代码"）。
    # 现在只用它的 ``parse_notes``/``load_notes`` 校验更新说明。
    catalog_tool: Path
    project: str
    poll_seconds: float
    ready_timeout: float
    health_timeout: float
    migrate_timeout: float
    backup_timeout: float
    once: bool
    acknowledge: str
    # **没有** registry/镜像名/tag 的配置项：发现目标固定是
    # ``ghcr.io/qsbb/ling-api``（见模块 docstring「版本从哪来」）。请求方
    # （网页/API/请求文件）无法影响它，配置也不能——否则"registry 地址"就变成
    # 新的信任面，而它本来只是一个常量。测试通过
    # ``registry.REGISTRY_ENV``（``LING_UPDATER_REGISTRY``）把 base URL 指到本地
    # stub，生产不设这个变量。

    # 「检查更新」限流窗口（秒）：请求触发的刷新在窗口内最多真正处理一次，
    # 多余的请求文件直接删掉（``release_check.reason_detail="throttled"``，
    # **不改** ``state``）。自动刷新（启动/周期）**不**计入这个窗口——否则一次
    # 启动就会把用户刚点的检查吃掉。
    refresh_throttle_seconds: float


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
        "--release-catalog",
        default=_env("LING_UPDATER_RELEASE_CATALOG"),
        help=(
            "发行包里 releases.json 的路径（默认 <publish-py 的上级目录>/../"
            "releases.json，容器里就是 /srv/ling-code/releases.json）。"
            "**目录已不参与更新判定**：这个路径现在只用来定位同目录的 "
            "update-notes.json（更新说明）与发行包布局核对。"
        ),
    )
    parser.add_argument(
        "--refresh-throttle-seconds",
        type=float,
        default=float(_env("LING_UPDATER_REFRESH_THROTTLE_SECONDS", "10")),
        help="请求触发的刷新限流窗口秒数（默认 10；窗口内多余的请求直接丢弃）",
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


def local_notes_path(cfg: Config) -> Path:
    """发行包里那份更新说明：与 ``releases.json`` **同目录**（容器里 /srv/ling-code/）。

    为什么还用 ``--release-catalog`` 的路径定位：发行包的目录布局没变（README/
    Dockerfile 都把它当"deploy 目录"），换一个独立路径参数只会多一个要同步的挂载
    点。它指向的文件本身**不读**。
    """
    return cfg.release_catalog.parent / NOTES_NAME


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
        ("--refresh-throttle-seconds", args.refresh_throttle_seconds),
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
        # 发行包里的 ``catalog.py``：现在只用它校验 ``update-notes.json``
        # （``parse_notes``/``load_notes``）。目录解析/校验代码还在，但更新器不再
        # 依赖它。
        catalog_tool=publish_py.parent / RELEASE_CATALOG_TOOL,
        refresh_throttle_seconds=args.refresh_throttle_seconds,
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


def utc_now_text() -> str:
    """状态文件里的 UTC 时间戳（秒级、``Z`` 结尾，与 ``publish.utc_now`` 同形）。

    只用于状态文件的"什么时候"字段（``release_check.checked_at`` 等）：形状固定、
    可比对，不是给人看的文案。
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def release_catalog_block(current_version: str | None, checked_at: str) -> dict[str, Any]:
    """``status.json`` 顶层 ``release_catalog``（**只读兼容**，不再读目录）。

    为什么还要写这个键：仍在跑 0.0.3 的 API 会读它。但目录已经**不参与**任何判定，
    所以**不能**写 ``state="ok"``（那是伪造"目录正常"，设计 §7 明令禁止），也
    不该留旧码表的 ``missing``/``invalid``（那会把"我们改成了 registry"说成"目录
    坏了"）。``unused`` 是两边码表都不认的值 → 都按"未知"处理，既不崩也不撒谎。
    """
    return {
        "state": RELEASE_CATALOG_UNUSED,
        "reason": None,
        "entries": 0,
        "current_version": current_version,
        "source": RELEASE_CHECK_SOURCE,
        "checked_at": checked_at,
    }


def release_check_block(
    *,
    state: str,
    checked_at: str | None,
    check_id: str | None,
    reason: str | None,
    reason_detail: str | None,
    current: dict[str, Any],
    latest: dict[str, Any] | None,
    tag_count: int,
) -> dict[str, Any]:
    """``status.json`` 顶层 ``release_check``：**版本发现的唯一事实来源**。

    只放固定码与实测值：``state`` 三值、8 个原因码、当前/最新版本与摘要、tag 计数。
    没有主机名、没有 URL、没有异常原文（那些只进私有日志）。

    ``latest`` 为 ``null`` 的条件是"这次没解析出最新版"（失败、或只有预发布）——
    它与 ``state`` 一起被 API 读：``state=ok`` 且 ``latest=None`` 在语义上不可能
    （``no_stable_tags`` 就是 ``failed``），所以两者不会互相矛盾。
    """
    normalized_state = (
        state if state in (RELEASE_CHECK_OK, RELEASE_CHECK_FAILED, RELEASE_CHECK_UNKNOWN)
        else RELEASE_CHECK_UNKNOWN
    )
    normalized_reason = reason if reason in RELEASE_CHECK_REASONS else None
    normalized_id = (
        check_id
        if isinstance(check_id, str) and REQUEST_ID_RE.fullmatch(check_id)
        else None
    )
    normalized_latest: dict[str, Any] | None = None
    if isinstance(latest, dict):
        version = latest.get("version")
        version_key = _semver_key(version) if isinstance(version, str) else None
        if version_key is not None:
            normalized_latest = {
                "version": version,
                "digest": _registry_digest(latest.get("digest")),
                # registry 的 tag 列表不带提交号：拉取后由 publish 用镜像标注核
                # `LING_COMMIT == OCI revision`。这里**不猜**。
                "commit": None,
            }
    return {
        "state": normalized_state,
        "checked_at": checked_at if isinstance(checked_at, str) else None,
        "check_id": normalized_id,
        "source": RELEASE_CHECK_SOURCE,
        "reason": normalized_reason,
        "reason_detail": reason_detail if isinstance(reason_detail, str) else None,
        "current": {
            "version": _semver_text(current.get("version")),
            "digest": _registry_digest(current.get("digest")),
        },
        "latest": normalized_latest,
        "tag_count": max(0, int(tag_count)),
    }


def _semver_key(value: str | None) -> tuple[int, int, int] | None:
    """``"0.0.4"`` -> ``(0, 0, 4)``；不是**规范化**的严格 semver 就返回 None。

    复用 registry 客户端的 tag 规则（``v`` 前缀 + 无前导零）：版本号在状态文件里
    只有一种写法，比较才不会出现 ``0.0.04`` 与 ``0.0.4`` 这种等价物。
    """
    if not isinstance(value, str) or not value:
        return None
    return registry.stable_version(f"v{value}")


def _semver_text(value: object) -> str | None:
    """当前版本只允许**严格 semver** 进状态文件；其它（含空串）一律 null。"""
    if not isinstance(value, str):
        return None
    key = _semver_key(value)
    return None if key is None else registry.version_text(key)


def _registry_digest(value: object) -> str | None:
    """registry 的摘要形状（``sha256:<64 位小写 hex>``），其它一律 null。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if registry.DIGEST_RE.fullmatch(text) else None


def docker_runner(pub: ModuleType) -> Any:
    """状态刷新用的 docker Runner：**复用**发行包的实现，不自己拼 subprocess。

    与 publish 用同一个 ``Runner``（同一套超时/rc/输出语义），所以「更新器读容器
    实况」和「publish 读容器实况」不会变成两种事实。日志用一个独立的 ``Logger``：
    状态刷新的 argv 只进 stderr，不混进 ``journal.log``——那里放的是"这次更新做了
    什么"，而状态刷新是每几分钟一次的后台旁路。
    """
    return pub.Runner(pub.Logger())


def _docker_json_object(runner: Any, argv: Sequence[str]) -> dict[str, Any] | None:
    """跑一条**只读** docker 命令，取 JSON 数组里的第一个对象；任何异常都返回 None。

    调用方一律按"读不到"处理（当前版本未知 / 当前摘要未知）：这里不是唯一定义
    错误的地方，任何一条读不出来都不该让更新器崩，也不该让它猜。
    """
    result = runner.run(list(argv))
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    return data[0]


def current_running_image(
    cfg: Config, pub: ModuleType, runner: Any
) -> tuple[str | None, str | None]:
    """读**运行中 api 容器**所用镜像的 ``(version, digest)``。

    ``version`` 来自 ``org.opencontainers.image.version`` 标注，``digest`` 来自同一
    镜像的 ``RepoDigests``（``sha256:<hex>`` 形状）。为什么不从 ``.env`` 的
    ``LING_APP_IMAGE`` 反查：它可以被手改、也可能改了还没重建容器；运行中的容器
    本来就是"现在到底跑的是哪一版"的实测事实。命令序列与 publish 的
    ``inspect_previous_containers`` 一致（``docker ps`` 按 compose 标签找唯一容器 →
    ``docker inspect --type container`` 拿 image ID → ``docker image inspect`` 读
    标注），只是这里只读、只关心 api。

    任何一个环节不成立都返回 ``(None, None)``（→ 调用方按"当前版本未知"处理，
    目标不兼容）：0 个/多个 api 容器、容器不在 running、镜像没标注、标注不是严格
    semver。**不**退回 ``.env``、**不**猜。
    """
    listed = runner.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={cfg.project}",
            "--filter",
            "label=com.docker.compose.service=api",
            "--format",
            "{{.ID}}|{{.Names}}|{{.State}}",
        ]
    )
    if listed.returncode != 0:
        return None, None
    rows = [line for line in listed.stdout.splitlines() if line.strip()]
    if len(rows) != 1:
        return None, None
    fields = rows[0].split("|")
    if len(fields) != 3 or fields[2].strip() != "running":
        return None, None
    container = _docker_json_object(
        runner, ["docker", "inspect", "--type", "container", fields[0].strip()]
    )
    if container is None:
        return None, None
    image_id = container.get("Image")
    if not isinstance(image_id, str) or not image_id:
        return None, None
    image = _docker_json_object(runner, ["docker", "image", "inspect", image_id])
    if image is None:
        return None, None
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    version = labels.get(VERSION_LABEL) if isinstance(labels, dict) else None
    digest: str | None = None
    for reference in image.get("RepoDigests") or []:
        match = pub.DIGEST_RE.fullmatch(str(reference))
        if match is not None:
            digest = f"sha256:{match.group(1)}"
            break
    return (
        version if isinstance(version, str) and version.strip() else None,
        digest,
    )


def registry_targets(
    pub: ModuleType,
    current_version: str | None,
    latest_version: str | None,
    latest_digest: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """把「一次 registry 检查的结论」投影成旧形状的目标列表 + 推荐目标。

    规则（**只允许升级**，与模块 docstring 一致）：

    1. 没有最新版（这次没查到 / 没有稳定 tag）→ 空列表，绝不编一个；
    2. 当前版本未知 → 有最新版也 ``compatible=false`` +
       ``current_version_unknown``（不能证明"比当前新"，就不许装）；
    3. 最新版**不高于**当前版本 → 该条**不出现**（不提供降级、也不提供"重装");
    4. 摘要为空 → ``compatible=false`` + ``digest_unresolved``；
    5. ``has_migration`` 恒 ``true``：迁移是前向的，更新入口总会跑 migrate，
       不再由任何发布目录人工开关决定。

    只放**最新**这一版：其它 tag 的 digest 没有解析（每个都要打一次 registry），
    把它们列出来只会是一串"版本在、摘要取不到"的假失败。
    """
    if latest_version is None:
        return [], None
    latest_key = _semver_key(latest_version)
    if latest_key is None:
        return [], None
    reference = (
        f"{pub.IMAGE_REPOSITORY}@{latest_digest}"
        if latest_digest is not None
        and pub.DIGEST_RE.fullmatch(f"{pub.IMAGE_REPOSITORY}@{latest_digest}")
        else None
    )
    current_key = _semver_key(current_version)
    if current_key is None:
        compatible, reason = False, INCOMPATIBLE_CURRENT_VERSION_UNKNOWN
    elif latest_key <= current_key:
        # 只允许升级：同版本/更旧的目标不是"可用目标"，直接不出现。
        return [], None
    else:
        compatible = reference is not None
        reason = None if compatible else INCOMPATIBLE_DIGEST_UNRESOLVED
    target = {
        "version": latest_version,
        "digest": reference,
        "commit": None,
        "has_migration": True,
        "compatible": compatible,
        "incompatible_reason": reason,
    }
    recommended = (
        None
        if not compatible
        else {
            "version": target["version"],
            "digest": target["digest"],
            "commit": target["commit"],
            "has_migration": target["has_migration"],
        }
    )
    return [target], recommended


def read_release_check(
    cfg: Config, pub: ModuleType, runner: Any | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
    """查一次更新 → ``(release_check, available_targets, recommended_target)``。

    顺序：先读**本机事实**（运行中 api 镜像的版本/摘要），再问 registry。这样即使
    这次查不到新版，状态里仍然有"当前跑的是哪一版"（页面不会连这个都说不出来）。

    任何失败都收敛成固定的 ``state``/``reason`` 码（**不**把异常文本、主机名写进
    状态文件），并且 ``available_targets=[]``：宁可明说"这次查不到"，也不要静默、
    也不要拿上一次的缓存冒充这次的结果。

    这个函数**会碰 Docker**（只读）与网络：``docker ps`` / ``inspect`` /
    ``image inspect`` 读当前版本，``docker info`` 读宿主架构，再加 1~3 次 registry
    请求。调用方按 5 分钟节流（见 :meth:`StatusWriter.refresh_release_check_if_due`），
    不要放进 5 秒的主轮询里。
    """
    docker = runner if runner is not None else docker_runner(pub)
    version, digest = current_running_image(cfg, pub, docker)
    current = {"version": _semver_text(version), "digest": digest}
    try:
        client = registry.RegistryClient()
        listing = client.stable_tags()
        latest = listing.latest_version
        if latest is None:  # pragma: no cover - stable_tags 保证非空
            raise registry.RegistryError(registry.REASON_NO_STABLE_TAGS, "没有稳定 tag")
        manifest = client.manifest_digest(latest, host_arch=daemon_arch(pub))
    except registry.RegistryError as error:
        block = release_check_block(
            state=RELEASE_CHECK_FAILED,
            checked_at=utc_now_text(),
            check_id=None,
            reason=error.code,
            reason_detail=None,
            current=current,
            latest=None,
            tag_count=0,
        )
        return block, [], None
    block = release_check_block(
        state=RELEASE_CHECK_OK,
        checked_at=utc_now_text(),
        check_id=None,
        reason=None,
        reason_detail=None,
        current=current,
        latest={"version": latest, "digest": manifest.digest, "commit": None},
        tag_count=listing.count,
    )
    targets, recommended = registry_targets(
        pub, current["version"], latest, manifest.digest
    )
    return block, targets, recommended


# ---------------------------------------------------------------- 更新说明


def _notes_api_base() -> str:
    """GitHub API 的 base URL：生产固定 ``https://api.github.com``。

    ``LING_UPDATER_RELEASE_API`` 只给测试把请求指到本地 stub（与 registry 的注入点
    同一条纪律：不接受请求方给 URL）。
    """
    injected = os.environ.get(RELEASE_NOTES_API_ENV, "").strip().rstrip("/")
    return injected or RELEASE_NOTES_API_BASE


def _fetch_json(url: str, *, timeout: float) -> Any | None:
    """GET 一个 JSON（**只**用于 GitHub Release）；任何失败都返回 ``None``。

    只用标准库：更新器镜像很小，为一个可选的展示请求引 HTTP 客户端不值得。
    失败**不影响** ``release_check``：说明是锦上添花（设计 §5）。
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", f"ling-updater/{UPDATER_VERSION}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RELEASE_BODY_BYTES)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def _note_line(value: object, limit: int) -> str | None:
    """一段发布文案：折成单行、去掉控制字符、按字数截断（不吐半句 markdown）。"""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    text = "".join(char for char in text if char.isprintable())
    text = text.strip()
    if not text:
        return None
    return text[:limit]


def _release_body_note(body: object) -> tuple[str | None, list[str]]:
    """把 Release 正文压成 ``(summary, items)``：首行当摘要，列表项当条目。

    **不改写、不总结、不加重要程度**：这是发布方原话（设计 §附「文案纪律」）。
    只做折行归一、长度上限与条目数上限——超限的那条按字数截断，不整条丢掉
    （整条丢掉会让"他写了但页面没有"变成新的困惑）。
    """
    summary: str | None = None
    items: list[str] = []
    if not isinstance(body, str):
        return None, items
    for raw in body.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", "```", "|")):
            continue
        is_bullet = stripped[0] in "-*+"
        text = _note_line(stripped.lstrip("-*+> \t"), MAX_NOTE_ITEM_CHARS)
        if text is None:
            continue
        if is_bullet:
            if len(items) >= MAX_NOTE_ITEMS:
                continue
            items.append(text)
        elif summary is None:
            summary = _note_line(text, MAX_NOTE_SUMMARY_CHARS)
    return summary, items


def github_release_note(version: str) -> dict[str, Any] | None:
    """读 ``v<version>`` 这个**精确 tag** 的 GitHub Release；取不到返回 ``None``。

    为什么不用 ``releases/latest``：那会把"最近一次发布"冒充成"你要装的这一版"。
    草稿/预发布不算（我们只发稳定版）。正文与链接只作展示，不参与任何判定。
    """
    payload = _fetch_json(
        f"{_notes_api_base()}/repos/{RELEASE_NOTES_REPO}/releases/tags/v{version}",
        timeout=RELEASE_NOTES_API_TIMEOUT_SECONDS,
    )
    if not isinstance(payload, dict):
        return None
    if payload.get("draft") is True or payload.get("prerelease") is True:
        return None
    summary, items = _release_body_note(payload.get("body"))
    url = _note_line(payload.get("html_url"), MAX_NOTE_URL_CHARS)
    if url is not None and not url.startswith("https://"):
        url = None
    if summary is None and not items:
        return None
    return {"summary": summary, "items": items, "url": url}


def _local_notes(
    cfg: Config, pub: ModuleType, tool: ModuleType | None
) -> tuple[str, str | None, dict[str, Any]]:
    """读发行包里那份 ``update-notes.json`` → ``(state, reason, notes)``。"""
    if tool is None or not hasattr(tool, "parse_notes") or not hasattr(tool, "load_notes"):
        # 发行包的 catalog.py 太旧（没有更新说明校验器）：按"没有说明"处理。
        # **不**把它算成检查失败——否则一次更新器/发行包版本错配会把"能不能更新"
        # 一起打哑。
        return NOTES_STATE_INVALID, NOTES_REASON_TOOL_MISSING, {}
    path = local_notes_path(cfg)
    try:
        if not path.is_file():
            return NOTES_STATE_MISSING, None, {}
        parsed = tool.load_notes(path)
    except Exception as error:  # noqa: BLE001 - 坏文件只影响更新说明
        return (
            NOTES_STATE_INVALID,
            NOTES_REASON_BY_CODE.get(getattr(error, "code", ""), NOTES_REASON_INVALID),
            {},
        )
    return NOTES_STATE_OK, None, dict(parsed.get("notes") or {})


def read_release_notes(
    cfg: Config,
    pub: ModuleType,
    tool: ModuleType | None,
    version: str | None,
) -> dict[str, Any]:
    """取**latest 那一版**的更新说明 → ``{"state", "reason", "notes"}``（绝不抛错）。

    数据流（设计 §5）：

    1. 发行包自带那份 ``update-notes.json`` 里有这一版 → 用它（**存在即用**）；
    2. 没有（文件缺失/坏/没这一版）→ 可选再试 GitHub Release 的精确 tag
       （超时 5 秒，失败只影响说明）；
    3. 都没有 → ``state`` 如实反映本地那份的状态（``missing``/``invalid``），
       ``notes={}``。

    更新说明**不参与**"能不能更新"的判定，所以这里永远不抛错、也不 fail closed。
    """
    state, reason, table = _local_notes(cfg, pub, tool)
    if version is None:
        return {"state": state, "reason": reason, "notes": {}}
    note = table.get(version)
    if isinstance(note, dict):
        summary = _note_line(note.get("summary"), MAX_NOTE_SUMMARY_CHARS)
        items = [
            line
            for line in (
                _note_line(item, MAX_NOTE_ITEM_CHARS) for item in note.get("items") or []
            )
            if line is not None
        ][:MAX_NOTE_ITEMS]
        url = _note_line(note.get("url"), MAX_NOTE_URL_CHARS)
        return {
            "state": state,
            "reason": reason,
            "notes": {version: {"summary": summary, "items": items, "url": url}},
        }
    remote = github_release_note(version)
    if remote is not None:
        return {"state": NOTES_STATE_OK, "reason": None, "notes": {version: remote}}
    return {"state": state, "reason": reason, "notes": {}}


def status_payload(
    pub: ModuleType,
    state: str,
    task: dict[str, Any] | None,
    gate: dict[str, Any],
    catalog: dict[str, Any],
    check: dict[str, Any],
    targets: list[dict[str, Any]],
    recommended: dict[str, Any] | None,
    notes: dict[str, Any],
) -> dict[str, Any]:
    """状态文件全文（``schema``/``protocol`` 不变，只加/换顶层键）。

    ``release_check`` / ``release_notes`` 都是**加法**字段：旧 API（0.0.3）对状态
    文件只按已知键 ``payload.get(...)`` 取值、对 ``release_catalog`` 逐字段重建，
    多出来的顶层键与子键它根本不看——所以加这些键不会让还在跑旧 API 的机器崩。
    唯一要守住的是**文件大小**（见 ``STATUS_NOTES_BUDGET_BYTES``）。

    ``release_catalog`` 仍然写（``state="unused"``），因为 0.0.3 的 API 会读它；
    目录本身已经不参与判定（见 :func:`release_catalog_block`）。
    """
    return {
        "schema": STATUS_SCHEMA,
        "protocol": PROTOCOL,
        "updated_at": pub.utc_now(),
        "updater": {"state": state, "version": UPDATER_VERSION},
        "task": task_view(task),
        "backup_gate": gate,
        "release_catalog": catalog,
        "release_check": check,
        "available_targets": targets,
        "recommended_target": recommended,
        "release_notes": notes,
    }


def serialize_status(payload: dict[str, Any]) -> str:
    """序列化状态文件正文；超预算时丢掉更新内容（见 ``STATUS_NOTES_BUDGET_BYTES``）。

    唯一的兜底：更新内容是"锦上添花"，而整份状态对 API 不可读意味着页面连"当前版本
    /有没有新版/恢复点"都看不到。所以宁可丢说明，也不让文件越过读取上限——
    丢了要说清楚（``state=invalid`` + ``notes_too_large``），不假装"这一版没有说明"。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if len(text.encode("utf-8")) > STATUS_NOTES_BUDGET_BYTES:
        payload["release_notes"] = {
            "state": NOTES_STATE_INVALID,
            "reason": NOTES_REASON_TOO_LARGE,
            "notes": {},
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    return text


def sanitize_scan_text(payload: dict[str, Any]) -> str:
    """状态自查用的文本：**不含更新内容的正文**。

    ``release_notes`` 里的 ``summary``/``items`` 是**发布方写的公开文案**（随
    ``update-notes.json`` 交付，或从发布目录同源取回）。它天然可能包含
    "password"/"journal.log" 这类词——发布日志本来就会写「修复密码重置」、
    「入队日志」——把这些词送进"疑似敏感内容"的扫描，会让一次正常发版把**整份
    状态写入**拒掉（网页从此看不到任何状态）。

    所以自查只扫我们自己构造的部分：更新内容已经过 ``parse_notes`` 的结构/长度/
    字符集约束，且它不携带本机路径或密钥（不是我们拼进去的）。
    """
    trimmed = dict(payload)
    notes = trimmed.get("release_notes")
    if isinstance(notes, dict):
        trimmed["release_notes"] = {
            "state": notes.get("state"),
            "reason": notes.get("reason"),
            "notes": {},
        }
    return json.dumps(trimmed, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


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

    ``release_check`` / ``available_targets`` / ``recommended_target`` /
    ``release_notes`` 的刷新时机与恢复点门禁完全一致（启动、每处理完一个任务），
    外加「用户点了检查更新」。**这一组刷新会碰 Docker（只读）与网络**——当前版本
    读运行中 api 镜像的 OCI 标注，新版本要查 GHCR 的 ``tags/list`` 与 manifest
    digest。所以它被 :meth:`refresh_release_check_if_due` 的 5 分钟节流挡着，
    **绝不能**放进 5 秒的主轮询：那样所有部署实例每 5 秒就打一次 GHCR。

    失败**不清空**、也不改写上一次的结论：失败的这一轮就是 ``state=failed`` +
    ``reason``，目标列表为空（"这次查不到"），页面据此说"暂时无法确认"，而
    **不会**把空列表读成「已是最新」。
    """

    def __init__(self, cfg: Config, pub: ModuleType, tool: ModuleType | None = None) -> None:
        self.cfg = cfg
        self.pub = pub
        self.tool = tool
        self._last: str | None = None
        self._gate: dict[str, Any] | None = None
        self._check: dict[str, Any] | None = None
        # 上次查 registry 的时间（monotonic）；0 表示还没查过。
        self._checked_at = 0.0
        self._targets: list[dict[str, Any]] = []
        self._recommended: dict[str, Any] | None = None
        self._notes: dict[str, Any] = {
            "state": NOTES_STATE_MISSING,
            "reason": None,
            "notes": {},
        }

    # ------------------------------------------------------------ 恢复点门禁
    def refresh_backup_gate(self) -> dict[str, Any]:
        self._gate = backup_gate(self.cfg, self.pub)
        return self._gate

    # ------------------------------------------------------------ 版本发现
    def refresh_release_check(
        self, *, requested: bool = False, request_id: str | None = None
    ) -> dict[str, Any]:
        """查一次更新并重算目标/推荐/更新说明。

        会发这些**只读**调用：``docker ps``/``inspect``/``image inspect``（当前版本
        与摘要）、``docker info``（宿主架构，只有真的解析 manifest 时才问）、
        GHCR 的 ``tags/list`` 与 manifest、以及（可选）一次 GitHub Release。

        ``requested=True``（用户点了「检查更新」）时给这一块带上 ``check_id``，让
        前端确认"我这次检查完成了"；自动刷新写 ``null``。无论哪种，这一轮的结果
        都**覆盖**上一次：``state=failed`` + 空目标就是"这次查不到"，不是"已是最新"。
        """
        block, targets, recommended = read_release_check(self.cfg, self.pub)
        if requested and request_id is not None:
            block = {**block, "check_id": request_id}
        self._check = block
        self._targets, self._recommended = targets, recommended
        latest = block.get("latest")
        self._notes = read_release_notes(
            self.cfg,
            self.pub,
            self.tool,
            latest.get("version") if isinstance(latest, dict) else None,
        )
        self._checked_at = time.monotonic()
        return self._check

    def record_throttled(self, request_id: str) -> None:
        """被限流丢弃的刷新请求：回显 ``check_id`` + ``reason_detail=throttled``。

        **不改** ``state``/``latest``/``current``/``checked_at``：这次没有真的去查，
        拿它改写上一次的结论就是把"没查"说成"查过了"。前端靠 ``check_id`` 匹配
        到自己那次点击、靠 ``reason_detail`` 知道"刚查过，稍后再看"。
        """
        block = dict(self._check) if self._check is not None else empty_release_check()
        block["check_id"] = request_id
        block["reason_detail"] = REASON_DETAIL_THROTTLED
        self._check = block

    def summary(self) -> dict[str, Any]:
        """给私有日志用的一句话摘要（只有码与计数，不含路径/文案/主机名）。"""
        block = self._check or {}
        latest = block.get("latest") if isinstance(block.get("latest"), dict) else {}
        return {
            "state": str(block.get("state")),
            "reason": block.get("reason"),
            "current": (block.get("current") or {}).get("version"),
            "latest": latest.get("version"),
            "tags": block.get("tag_count"),
            "targets": len(self._targets),
            "notes": str(self._notes.get("state")),
        }

    def refresh_release_check_if_due(self) -> None:
        """空闲时低频查一次 registry，让"刚发布的版本"能被看到。

        为什么不是每轮都查：主循环 5 秒一轮，那是查**请求目录**（有没有人要更新），
        不是查 GHCR。每轮都查等于所有部署实例每 5 秒打一次 GHCR——没必要，也很可能
        被限流。

        为什么又必须重查：只在启动和任务结束时查的话，发版之后没有新任务、也没重启
        的机器会一直显示"没有新版本"——那正是"一键更新只能成功一次"的另一半。

        用户点「检查更新」走的是**另一条**路（``refresh-<id>.json``，由主循环立刻
        处理并绕过这里的闸门）。任务执行前还有一条独立的即时复核（
        :meth:`Updater.assert_target_digest_current`），不受这里影响。
        """
        if time.monotonic() - self._checked_at >= CHECK_INTERVAL_SECONDS:
            self.refresh_release_check()

    # ------------------------------------------------------------ 写
    def write(self, state: str, task: dict[str, Any] | None, *, force: bool = False) -> None:
        gate = self._gate if self._gate is not None else self.refresh_backup_gate()
        check = (
            self._check
            if self._check is not None
            else self.refresh_release_check()
        )
        current = (check.get("current") or {}).get("version")
        payload = status_payload(
            self.pub,
            state,
            task,
            gate,
            release_catalog_block(
                current if isinstance(current, str) else None,
                str(check.get("checked_at") or utc_now_text()),
            ),
            check,
            self._targets,
            self._recommended,
            self._notes,
        )
        text = serialize_status(payload)
        assert_status_sanitized(self.cfg, sanitize_scan_text(payload))
        if text == self._last and not force:
            return
        self.pub.write_atomic(self.cfg.status_file, text.encode("utf-8"), 0o644)
        self._last = text


def empty_release_check() -> dict[str, Any]:
    """还没有查过时的占位块（``state=unknown``，其它字段全 null/0）。

    构造 :class:`StatusWriter` 之后第一次 :meth:`StatusWriter.write` 之前会被真实
    结果覆盖；它的存在只是让"从未查过"这件事也有确定的形状。
    """
    return release_check_block(
        state=RELEASE_CHECK_UNKNOWN,
        checked_at=None,
        check_id=None,
        reason=None,
        reason_detail=None,
        current={"version": None, "digest": None},
        latest=None,
        tag_count=0,
    )


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


# ---------------------------------------------------------------- 刷新请求


def refresh_id_from_name(name: str) -> str | None:
    """``refresh-<32 hex>.json`` 里的 ID；不合约定返回 None（调用方直接删掉）。"""
    if not name.startswith(REFRESH_PREFIX) or not name.endswith(".json"):
        return None
    candidate = name[len(REFRESH_PREFIX) : -len(".json")]
    return candidate if REQUEST_ID_RE.fullmatch(candidate) else None


def pending_refreshes(cfg: Config) -> list[Path]:
    """待处理的刷新请求（按文件名排序，一轮只处理一个；上限由 API 侧保证）。"""
    try:
        entries = [
            path
            for path in cfg.requests_dir.iterdir()
            if path.is_file()
            and path.name.startswith(REFRESH_PREFIX)
            and refresh_id_from_name(path.name) is not None
        ]
    except OSError:
        return []
    return sorted(entries, key=lambda item: item.name)


def _discard(path: Path) -> None:
    """删掉处理完的请求文件：它是一次性请求，结论在 ``status.json`` 里。

    删不掉（权限/别人已删）也继续：留着它只会让下一轮再看到同一个 ID，而幂等
    记录会把它当重复请求再次丢弃。
    """
    with contextlib.suppress(OSError):
        path.unlink()


def load_refresh_request(path: Path) -> dict[str, Any]:
    """严格解析刷新请求；返回 ``{"request_id", "created_at", "ignored"}``。

    与安装请求的区别**只有一个**：多余的键不拒绝，只是忽略并记下键名（见常量处
    的说明）。真正决定"这条请求是什么"的字段（schema / kind / id / 时间格式）
    仍然严格——畸形请求不刷新、但也不崩：调用方只记私有日志并删文件。
    """
    name_id = refresh_id_from_name(path.name)
    if name_id is None:
        raise UpdaterError(
            EXIT_REJECTED,
            REFRESH_REASON_MALFORMED,
            "refresh",
            f"刷新请求文件名不合约定：{path.name}",
            ["请求方必须写成 refresh-<32 位小写 hex>.json（先写临时文件再 rename）。"],
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdaterError(
            EXIT_REJECTED,
            REFRESH_REASON_MALFORMED,
            "refresh",
            f"刷新请求不可读或不是 JSON：{type(exc).__name__}",
        ) from exc
    if not isinstance(data, dict):
        raise UpdaterError(
            EXIT_REJECTED, REFRESH_REASON_MALFORMED, "refresh", "刷新请求主体必须是 JSON 对象"
        )
    if data.get("schema") != REFRESH_SCHEMA:
        raise UpdaterError(
            EXIT_REJECTED,
            REFRESH_REASON_MALFORMED,
            "refresh",
            f"不支持的刷新 schema：{data.get('schema')!r}（只接受 {REFRESH_SCHEMA}）",
        )
    kind = data.get("kind")
    if kind is not None and kind != REFRESH_KIND:
        raise UpdaterError(
            EXIT_REJECTED,
            REFRESH_REASON_MALFORMED,
            "refresh",
            f"刷新请求的 kind 必须是 {REFRESH_KIND!r}",
        )
    # 同一件事的两个名字（见常量处）必须一致；只给一个就两处都用它。
    request_id = data.get("request_id", data.get("check_id"))
    if not isinstance(request_id, str) or REQUEST_ID_RE.fullmatch(request_id) is None:
        raise UpdaterError(
            EXIT_REJECTED,
            REFRESH_REASON_MALFORMED,
            "refresh",
            "刷新请求缺 request_id/check_id，或它不是 32 位小写 hex",
        )
    if request_id != name_id:
        raise UpdaterError(
            EXIT_REJECTED,
            REFRESH_REASON_MALFORMED,
            "refresh",
            "刷新请求里的 ID 与文件名不一致",
        )
    for key in ("request_id", "check_id"):
        if key in data and data[key] != request_id:
            raise UpdaterError(
                EXIT_REJECTED,
                REFRESH_REASON_MALFORMED,
                "refresh",
                f"刷新请求的 {key} 与另一个 ID 字段不一致",
            )
    created_at: str | None = None
    for key in ("created_at", "requested_at"):
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or RFC3339_RE.fullmatch(value) is None:
            raise UpdaterError(
                EXIT_REJECTED,
                REFRESH_REASON_MALFORMED,
                "refresh",
                f"刷新请求的 {key} 必须是 RFC3339 时间",
            )
        if created_at is not None and created_at != value:
            raise UpdaterError(
                EXIT_REJECTED,
                REFRESH_REASON_MALFORMED,
                "refresh",
                "刷新请求同时给了两个时间字段且不一致",
            )
        created_at = value
    return {
        "request_id": request_id,
        "created_at": created_at,
        "ignored": sorted(set(data) - REFRESH_KEYS),
    }


# 刷新请求的幂等/限流状态（``<state-dir>/refresh.json``，0600）。
#
# 为什么要**落盘**：更新器是常驻进程，但它随时可能重启（容器重建、宿主重启），
# 而 ``refresh-<id>.json`` 是"一次性请求"。没有这份记录时，「同一个 ID 再写一次」
# 在重启后会被当成新请求再刷一遍——用户连点两下就多打两次 registry。
# 时间用**墙钟**（``time.time()``）而不是 ``monotonic``：重启后 monotonic 归零，
# 限流窗口会失效。
def _empty_refresh_state() -> dict[str, Any]:
    return {"schema": REFRESH_STATE_SCHEMA, "last_refresh_at": None, "processed": []}


def _refresh_state_path(cfg: Config) -> Path:
    return cfg.state_dir / REFRESH_STATE_NAME


def load_refresh_state(cfg: Config) -> dict[str, Any]:
    """读幂等/限流状态；坏文件按"没记录过"处理（绝不因此崩）。"""
    try:
        data = json.loads(_refresh_state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_refresh_state()
    if not isinstance(data, dict) or data.get("schema") != REFRESH_STATE_SCHEMA:
        return _empty_refresh_state()
    last = data.get("last_refresh_at")
    processed = data.get("processed")
    last_at = (
        float(last)
        if isinstance(last, int | float) and not isinstance(last, bool)
        else None
    )
    return {
        "schema": REFRESH_STATE_SCHEMA,
        "last_refresh_at": last_at,
        "processed": [item for item in processed if isinstance(item, str)]
        if isinstance(processed, list)
        else [],
    }


def save_refresh_state(pub: ModuleType, cfg: Config, state: dict[str, Any]) -> None:
    """原子写（0600）：状态目录是持久的，半写的文件会让下一轮读不出幂等记录。"""
    state["processed"] = list(state.get("processed") or [])[-MAX_REMEMBERED_REFRESHES:]
    payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    pub.write_atomic(_refresh_state_path(cfg), (payload + "\n").encode("utf-8"), 0o600)


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
        # 版本发现同理：idle 也要有"有没有新版本"，否则网页的「更新」按钮永远
        # 「暂无可用目标」。这一刷只发**只读**调用（docker ps/inspect 读运行中
        # api 的版本标注 + 一次 GHCR 查询），不发任何变更命令；启动时算一次也是
        # 状态文件里「当前版本」的唯一来源。
        self.status.refresh_release_check()
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
    def assert_target_digest_current(self, target: str) -> None:
        """安装前复核：请求里的 digest **必须**是某个稳定 tag 现在指向的摘要。

        请求里只有 ``target_digest``，没有版本号，所以"复核该版本 tag"的落法是：
        重新列一次 tag（从最新稳定版往下，最多 :data:`MAX_DRIFT_PROBE_TAGS` 个），
        逐个解析 digest，要求与请求里的 digest **完全相同**。

        为什么不用 :class:`StatusWriter` 的缓存：缓存可能是几分钟前的，而"执行前
        再核一次"必须是真的再核一次。为什么**不**按 tag 拉：tag 是可变的，检查与
        安装之间被重推就会装上一个没人确认过的镜像——那正是 ``target_digest_drift``
        要挡住的事（设计 §6「安装包已变化，请重新检查更新」）。

        registry 现在不可达也算拒绝（``registry_unavailable``）：无法证明的目标一律
        不执行，一个字节都不改——连 ``docker pull`` 都不会发（它本来也拉不到）。
        """
        client = registry.RegistryClient()
        try:
            listing = client.stable_tags()
            host_arch = daemon_arch(self.pub)
            for version in reversed(listing.versions[-MAX_DRIFT_PROBE_TAGS:]):
                manifest = client.manifest_digest(
                    registry.version_text(version), host_arch=host_arch
                )
                if f"{self.pub.IMAGE_REPOSITORY}@{manifest.digest}" == target:
                    self.emit(
                        f"安装前复核通过：v{registry.version_text(version)} 现在仍指向"
                        f"请求里的摘要（{target}）"
                    )
                    return
        except registry.RegistryError as error:
            raise UpdaterError(
                EXIT_REJECTED,
                FAILURE_REGISTRY_UNAVAILABLE,
                "prepare",
                f"安装前无法复核目标摘要（{error.code}）：连不上镜像仓库"
                "或这次没查成，拒绝按未知状态安装",
                [
                    "稍后重试；检查 NAS 到 ghcr.io 的出网与 DNS。",
                    "不要按 tag 安装：安装引用只能是已核验的 @sha256:<digest>。",
                ],
            ) from error
        raise UpdaterError(
            EXIT_REJECTED,
            FAILURE_TARGET_DIGEST_DRIFT,
            "prepare",
            "请求的目标摘要不等于任何稳定 tag 此刻指向的摘要"
            "（tag 被重推、或你拿的是过期候选）",
            [
                "回网页重新「检查更新」后再提交；安装包变过就不能沿用旧确认。",
                "不要手改请求文件：更新器只执行与当前 tag 一致的那个摘要。",
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
        # 目标必须**现在**仍然成立：digest 格式合格只是必要条件，"这个摘要是不是
        # 某个稳定 tag 此刻指向的那一个"由 registry 现场复核。这一步在任何变更类
        # Docker 调用之前（拉取/备份/迁移都还没开始），只发只读查询
        # （GHCR 的 tags/list + manifest、docker info）。
        self.assert_target_digest_current(target)
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
        self.status.refresh_release_check()
        if self.task is None:
            return
        state = {
            TASK_NEEDS_MANUAL: "needs_manual",
            TASK_RUNNING: "busy",
        }.get(str(self.task.get("state")), "idle")
        self.status.write(state, self.task, force=True)

    # -------------------------------------------------- 按需刷新（检查更新）
    def process_refresh(self, path: Path) -> int:
        """处理一个 ``refresh-<id>.json``：**立刻**查一次 registry + 读更新说明。

        这条路**只做只读动作**（GHCR 的 tags/list + manifest、``docker
        ps/inspect`` 读当前版本、可选一次 GitHub Release）：不拉镜像、不备份、
        不迁移、不切容器。所以它不需要跨工具安装锁——但仍在**单飞锁**里
        （``run_once`` 拿着），因此不会与安装任务并发。

        顺序：读文件 → 判重（幂等）→ 判限流 → 记账 → 刷新 → 写状态 → 删文件。
        "记账"在"刷新"之前：中途崩溃时下一次只会把它当重复请求删掉，而不会再来
        一次（幂等优先于"多查一次"——检查是用户点出来的，重复查没有收益）。

        返回码一律 ``EXIT_OK``：查不到新版本是**记录在状态文件里的结果**，不是更新器
        进程失败（常驻进程不该因为一次网络故障就认为自己在报错循环里）。

        请求文件在 ``finally`` 里删（无论成功、限流、畸形还是意外）：它是**一次性**
        请求，留着它只会让下一轮反复看到同一个文件；结论已经写进状态文件了。
        """
        try:
            return self._process_refresh(path)
        finally:
            _discard(path)

    def _process_refresh(self, path: Path) -> int:
        name_id = refresh_id_from_name(path.name)
        if name_id is None:
            self.emit(f"删除文件名不合约定的刷新请求：{path.name}")
            return EXIT_OK
        state = load_refresh_state(self.cfg)
        if name_id in state["processed"]:
            self.emit(f"刷新请求 {name_id} 已处理过（幂等），不再重新查 registry")
            # 仍然写一次状态：重复请求不改结论，但状态文件必须存在且是最新的
            # （API 只读它；没有文件时页面只能说"更新器还没写出状态"）。
            self.status.write("idle", self.task)
            return EXIT_OK
        try:
            request = load_refresh_request(path)
        except UpdaterError as error:
            # 畸形请求**不改** ``release_check``：这次既没查 registry，也没有可信的
            # check_id 可回显（请求体自己就不合法）。只记私有日志、删文件。
            self.emit(f"刷新请求被拒绝（{error.code}）：{error.detail}")
            self.status.write("idle", self.task)
            return EXIT_OK
        ignored = request.get("ignored") or []
        if ignored:
            # 只记**键名**：值可能很大/很怪，私有日志里留个可解释的线索就够了。
            self.emit(f"刷新请求含未定义字段（已忽略）：{ignored}")

        now = time.time()
        last = state.get("last_refresh_at")
        window = self.cfg.refresh_throttle_seconds
        if isinstance(last, int | float) and now - float(last) < window:
            self.emit(
                f"刷新请求 {name_id} 被限流丢弃（{window:g} 秒内只处理一次），未查 registry"
            )
            # 回显 check_id（前端要知道"我这次点击有结论了"）+ reason_detail=
            # throttled；**state/latest/current 一个字节都不动**：这次没查。
            self.status.record_throttled(name_id)
            self.status.write("idle", self.task, force=True)
            return EXIT_OK

        state["processed"] = [*state["processed"], name_id]
        state["last_refresh_at"] = now
        try:
            save_refresh_state(self.pub, self.cfg, state)
        except OSError as error:
            # 记不下幂等状态就**不查**：宁可这次检查没做，也不要变成"每次点击都
            # 真的去打一次 GHCR"。
            self.emit(f"无法写入刷新状态（{type(error).__name__}），本次不查 registry")
            self.status.write("idle", self.task, force=True)
            return EXIT_OK
        try:
            # 绕过 5 分钟自动刷新节流：用户点了检查，就必须**现在**去查。
            self.status.refresh_release_check(requested=True, request_id=name_id)
            self.status.write("idle", self.task, force=True)
            summary = self.status.summary()
            self.emit(
                f"按需刷新 {name_id} 完成：state={summary['state']}"
                f"（{summary['reason'] or 'ok'}），current={summary['current']}，"
                f"latest={summary['latest']}，tags={summary['tags']}，"
                f"目标 {summary['targets']} 条，更新内容={summary['notes']}"
            )
        except Exception as error:  # noqa: BLE001 - 刷新失败必须落地成可读状态
            # 走到这里说明是更新器自己的 bug（registry 的失败已经被收敛成
            # ``state=failed``）。状态文件里不留自由文本：只记异常类型进私有日志，
            # 状态保持上一次检查的结论（页面至少还能看到它，而不是被清空）。
            self.emit(f"按需刷新 {name_id} 出现未预期异常：{type(error).__name__}")
            with contextlib.suppress(UpdaterError, OSError):
                self.status.write("idle", self.task, force=True)
        return EXIT_OK

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
    """单飞：整个「读任务 + 认领 + 处理（安装/刷新）+ 对账」都在同一把锁里。

    处理顺序（先后有意义）：

    1. 未完成任务先对账（``running``）、``needs_manual`` 先写状态并停止——安装
       任务的收尾优先于一切；
    2. **安装请求先于刷新请求**：设计原文「有安装任务时 refresh 排队」。刷新会打
       registry 与容器实况，但它读到的"当前版本"马上就会被安装改掉，先刷只会给出
       一个立刻过期的结论；
    3. 没有安装请求时处理**一个**刷新请求（``refresh-<id>.json``）；
    4. 都没有 → 空闲路径（低频重读目录 + 写状态）。

    旧更新器（0.0.3）不认识 ``refresh-*.json``，它只匹配 ``request-*``，所以这些
    文件在旧更新器眼里是不存在的普通文件：不认领、不消费、不阻塞安装通道。
    """
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
        if candidates:
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
        refreshes = pending_refreshes(cfg)
        if refreshes:
            return updater.process_refresh(refreshes[0])
        # 空闲也要低频重读目录：否则发版之后页面一直显示"没有新版本"。
        updater.status.refresh_release_check_if_due()
        updater.status.write("idle", task)
        return EXIT_OK
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
