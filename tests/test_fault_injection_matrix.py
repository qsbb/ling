"""P6 第一部分：故障注入矩阵（``docs/EASY-INSTALL-UPDATE-DESIGN.md`` §7.3）。

这个文件**只做一件事**：把设计 §7.3 列出的 14 条故障逐条映射成用例，并断言
**外部可观察行为**——退出码、状态文件/发布记录里的成功标志、是否执行了某类
命令、网页 API 的拒绝原因与可执行的下一步。它不重新测内部函数，也不为了变绿
放宽断言。

设计原文（§7.3）：

    覆盖拉取失败、恶意/错摘要、发布证据不符、备份缺媒体/根密钥、迁移跳过或
    失败、schema head 不一致、端口冲突、断电/更新器重启、断连重连、旧执行器
    残留和并发请求。每次演练都检查卷身份、密钥可解密、墓碑不复活、cron 的
    实际备份与隔离恢复；不能通过的故障路径应禁用网页更新，不靠提示文案放行。

清单 ↔ 用例（每条用例的 docstring 里都写明对应第几条）：

1. 拉取失败（registry 不可达 / 镜像不存在）——不进入维护、不迁移、不误报成功
2. 恶意/错摘要（格式错、别 registry、latest、大小写、63/65 位、非 hex）
3. 发布证据不符（镜像内 LING_COMMIT ≠ OCI revision；commit 不是 40hex；平台不匹配）
4. 备份缺媒体（media_state=partial）——拒绝迁移
5. 备份缺根密钥（keys_env_set=false / keys_root=absent）——拒绝迁移
6. 迁移跳过（日志有「跳过迁移」）——失败，不启动新 API
7. 迁移失败（退出码非 0）——不启动新 API、不回滚旧镜像
8. schema head 不一致（迁移后 alembic_version ≠ 目标 head）——不启动新 API
9. 端口冲突（API 发布端口 / WS 端口）——预检拒绝，不改动
10. 断电/更新器重启（状态文件在、任务中断；DB 已迁移 vs 未迁移）
11. 断连重连（前端轮询中断后继续）——同一 request_id 延续，不断言失败
12. 旧执行器残留（源码模式 pending.json / 旧 status）——显式拒绝或忽略
13. 并发请求（同时两个请求 / 同一 request_id 重放）——单飞、第二个被拒
14. API 侧门禁（开关关、更新器离线、协议不兼容、无恢复点、目标不在目录）

**复用**：``tests/test_release_publish.py`` 的 publish 工作区与 docker stub、
``tests/test_updater.py`` 的 updater 工作区与 docker stub、
``tests/integration/test_version_image_update.py`` 的 API 夹具与 helper。
本文件只新增用例，不改它们，也不改生产代码。

**已修的两个缺口**（此前用 ``xfail(strict=True)`` 如实标注，现在由独立回归
用例守住；回退修复会让下面两条用例直接 FAIL）：

- 清单第 9 条的「WS 端口」：``publish.py`` 预检现在按 ``docker compose config``
  的**合并后模型**取出所有服务发布到宿主的端口（override 的 ``ports`` 是追加
  语义），逐个核对占用者，不再只检查 api 的 6520。
- 清单第 7 条的「迁移失败码」：更新器 ``failure_code()`` 先判
  ``status=failed_migration``，迁移阶段失败报 ``migration_failed``，不再把所有
  ``after_migration`` 失败一律归成 ``switch_failed``。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from ling.api.routes import version as version_routes
from tests import test_release_publish as pub_harness
from tests import test_updater as upd_harness
from tests.integration import test_version_image_update as api_harness
from tests.integration.conftest import logged_in, web_env
from tests.test_release_publish import http_state as publish_http_state
from tests.test_updater import http_state as updater_http_state

# 这四项是**被 pytest 收集的夹具**（本模块只按名字请求，不直接调用）。
# 写进 __all__ 是给 linter 的明确声明：不要把"夹具 import"当成未使用导入；
# 同时也避免函数参数与模块绑定同名时被误报为重定义。
__all__ = ["logged_in", "publish_http_state", "updater_http_state", "web_env"]

# 两个 harness 里的常量是同一组值（同一批发布证据），这里只取一份并注明来源。
TARGET = upd_harness.TARGET
OLD_REF = upd_harness.OLD_REF
COMMIT = upd_harness.COMMIT
OLD_COMMIT = upd_harness.OLD_COMMIT
OTHER_COMMIT = upd_harness.OTHER_COMMIT
HEAD_OLD = upd_harness.HEAD_OLD
HEAD_NEW = upd_harness.HEAD_NEW
REQUEST_ID = upd_harness.REQUEST_ID
REQUEST_ID_B = upd_harness.REQUEST_ID_B

# 反向 WS override 的宿主端口（与 deploy/compose.image.ws.example.yaml 的示例一致）。
WS_PORT = 6799


# ---------------------------------------------------------------- 夹具接线


@pytest.fixture()
def publish_ws(tmp_path: Path, publish_http_state: Any) -> pub_harness.Workspace:
    """复用 ``test_release_publish.py`` 的工作区（同一套 docker stub / 备份脚本）。

    直接 import 它的 ``ws`` 夹具会丢掉同名依赖 ``http_state``（pytest 按**本模块**
    解析依赖），所以用 ``__wrapped__`` 显式把两个 harness 的 ``http_state`` 分开。
    """
    return pub_harness.ws.__wrapped__(tmp_path, publish_http_state)


@pytest.fixture()
def updater_ws(tmp_path: Path, updater_http_state: Any) -> upd_harness.Workspace:
    return upd_harness.ws.__wrapped__(tmp_path, updater_http_state)


@pytest.fixture(autouse=True)
def _fixed_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """API 用例需要固定的构建提交/当前镜像（与 test_version_image_update.py 一致）。"""
    monkeypatch.setenv("LING_COMMIT", api_harness.CURRENT_COMMIT)
    monkeypatch.setenv("LING_APP_IMAGE", api_harness.CURRENT_DIGEST)
    monkeypatch.delenv("LING_RUN_MIGRATIONS", raising=False)
    monkeypatch.setattr(version_routes, "current_version", lambda: api_harness.CURRENT_VERSION)


# ---------------------------------------------------------------- 断言工具


def _compose(ws: Any, verb: str) -> list[str]:
    return [line for line in ws.calls() if line.startswith("docker compose") and f" {verb} " in line]


def _calls(ws: Any, needle: str) -> list[str]:
    return [line for line in ws.calls() if needle in line]


def _index_of(ws: Any, needle: str) -> int:
    for index, line in enumerate(ws.calls()):
        if needle in line:
            return index
    raise AssertionError(f"未找到调用：{needle}\n" + "\n".join(ws.calls()))


def _assert_publish_failure_record(record: dict[str, Any], *, status: str) -> None:
    """失败记录必须**明确不是成功**，且没有留下任何"新 API 已验证"的证据。"""
    assert record["status"] == status, record["status"]
    assert record["status"] != "success"
    assert record["failure"] is not None
    assert record["result"]["api"] is None
    assert record["result"]["worker"] is None
    assert record["result"]["version_commit"] is None
    assert record["result"]["web_manifest_verified"] is False


def _assert_task_failure(task: dict[str, Any], *, state: str | None = None) -> None:
    """更新器任务视图：没有 succeeded，没有"已装上新版本"的证据。"""
    assert task["state"] != "succeeded"
    assert task["state"] != "resolved"
    assert task["failure"] is not None, task
    if state is not None:
        assert task["state"] == state, task["state"]


_ROLLBACK_COMMANDS = (
    "docker load",
    "docker tag",
    "docker save",
    "docker import",
    "pg_restore",
    "docker run",
)


def _assert_no_rollback_commands(ws: Any, *, start: int = 0) -> None:
    """已迁移后的失败不得出现回滚动作（旧镜像 tag/load、pg_restore、旧镜像 up）。

    归档发生在迁移**之前**（``docker tag/save`` 是发布前的回退材料），所以这里
    从迁移调用之后开始看；更新器的整条流水线不做归档，``start=0`` 即可。
    """
    tail = ws.calls()[start:]
    bad = [line for line in tail if any(marker in line for marker in _ROLLBACK_COMMANDS)]
    bad += [
        line
        for line in tail
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ]
    bad += [
        line
        for line in tail
        if line.startswith("docker compose") and " up " in line and OLD_REF in line
    ]
    assert not bad, "不应出现回滚动作：\n" + "\n".join(bad)


def _assert_failed_before_migration_restores_old(ws: Any, record: dict[str, Any]) -> None:
    """未迁移的失败（§6 第一行）：必须恢复旧 api/worker，且只有实测通过才算。"""
    assert record["migration"]["attempted"] is False
    assert record["rollback"]["attempted"] is True
    assert record["rollback"]["verified"] is True
    ups = _compose(ws, "up")
    assert [line for line in ups if line.endswith(" api")], ups
    assert [line for line in ups if line.endswith(" worker")], ups
    # 恢复必须用**旧摘要**：stub 把子进程 env 单独记成 docker-env 行。
    assert [
        line
        for line in ws.calls()
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ], ws.calls()


def _assert_ui_never_succeeded(view: dict[str, Any]) -> None:
    task = view.get("task")
    if task is not None:
        assert task["state"] != "succeeded", task
    assert view["installable"] is False or view["task"] is None or view["task"]["state"] != "succeeded"


# ================================================================ 清单第 1 条
# 拉取失败（registry 不可达 / 镜像不存在）→ 不进入维护、不迁移、不误报成功。


def test_01_pull_failure_never_enters_maintenance_or_migrates(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 1 条：镜像不存在 / compose pull 失败。

    断言：退出码 14、发布记录 failed_pull、没有 stop（不进维护）、没有备份、
    没有迁移、没有启动新 API，且记录里没有成功标志。
    """
    result = pub_harness.run_publish(publish_ws, env={"STUB_PULL_EXIT": "1"})
    assert result.returncode == 14, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_pull")
    assert record["failure"]["step"] == "pull"
    assert record["migration"]["attempted"] is False
    assert record["backup"]["complete"] is False
    # 不进入维护：没有停 api/worker，也没有跑备份。
    assert _compose(publish_ws, "stop") == []
    assert [line for line in publish_ws.calls() if line.startswith("backup ")] == []
    # 不迁移、不启动新 API。
    assert _compose(publish_ws, "run") == []
    assert _compose(publish_ws, "up") == []
    # 一步都没停过，不需要（也不能）宣称回滚。
    assert record["rollback"]["attempted"] is False


def test_01_registry_unreachable_fails_preflight_without_any_change(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 1 条：registry 不可达（manifest 读不到）→ 预检失败，什么都不动。"""
    result = pub_harness.run_publish(publish_ws, env={"STUB_MANIFEST_MODE": "fail"})
    assert result.returncode == 10, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_preflight")
    assert record["failure"]["step"] == "registry"
    # 连 pull/stop/backup/migrate/up 都没有发生，旧服务 untouched。
    pub_harness.assert_no_changes(publish_ws)
    assert record["migration"]["attempted"] is False
    assert record["rollback"]["attempted"] is False


def test_01_updater_pull_failure_is_failed_not_started_without_success(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 1 条（更新器侧）：拉取失败必须落成「未开始」而不是成功/已迁移。"""
    upd_harness.write_request(updater_ws)
    result = upd_harness.run_updater(updater_ws, env={"STUB_PULL_EXIT": "1"})
    assert result.returncode == 14, (result.stdout, result.stderr)
    task = updater_ws.task()
    _assert_task_failure(task, state="failed_not_started")
    assert task["failure"]["code"] == "pull_failed"
    assert task["migration"]["attempted"] is False
    assert task["backup"]["complete"] is False
    # 没有进入维护：没有 stop、没有备份脚本、没有迁移、没有起新容器。
    assert _compose(updater_ws, "stop") == []
    assert [line for line in updater_ws.calls() if line.startswith("backup ")] == []
    assert _compose(updater_ws, "run") == []
    assert _compose(updater_ws, "up") == []
    assert task["maintenance"]["state"] == "released"
    # 状态文件里的成功标志：不是 succeeded。
    status = updater_ws.status()
    assert status["updater"]["state"] == "idle"
    assert status["task"]["state"] == "failed_not_started"


# ================================================================ 清单第 2 条
# 恶意/错摘要 → 拒绝且零 compose 调用。

_BAD_DIGESTS = [
    "latest",
    "ghcr.io/qsbb/ling-api:latest",
    "ghcr.io/qsbb/ling-api:v0.0.1",
    f"ghcr.io/other/ling-api@sha256:{'a' * 64}",
    f"docker.io/qsbb/ling-api@sha256:{'a' * 64}",
    f"ghcr.io/qsbb/ling-api@sha256:{'A' * 64}",
    f"ghcr.io/qsbb/ling-api@sha256:{'a' * 63}",
    f"ghcr.io/qsbb/ling-api@sha256:{'a' * 65}",
    f"ghcr.io/qsbb/ling-api@sha256:{'g' * 64}",
    f"ghcr.io/qsbb/ling-api/extra@sha256:{'a' * 64}",
]


@pytest.mark.parametrize("digest", _BAD_DIGESTS)
def test_02_updater_rejects_bad_digest_with_zero_compose_calls(
    updater_ws: upd_harness.Workspace, digest: str
) -> None:
    """清单第 2 条：格式错/别 registry/latest/大写/63/65 位/非 hex → 不产生任何变更动作。

    ## 为什么只读探测是允许的（811ffa6 起）

    更新器**启动时**会刷新一次可信发布目录：读运行中 api 镜像的版本标注
    （``docker ps`` → ``inspect`` → ``image inspect``），并对自己目录里那些
    **固定**的 ``v<version>`` tag 做 ``docker manifest inspect``。这一组调用
    （`test_updater._READ_ONLY_CALLS`）与请求内容无关、也不改任何东西——
    811ffa6 把 ``test_updater.py`` 里同族用例改成
    ``mutating_calls(ws) == []``，这里当时漏改了。

    ## 这里真正守住的两件事（比"零调用"更强）

    1. **没有任何变更类调用**：不 compose up/down/pull、不备份、不发 HTTP、
       不写任务文件（``task.json`` 不存在、目录里也没有新任务）；
    2. **坏摘要一次都没被转发给 docker**：它连 argv 都没进过——格式校验在
       ``validate_request_identity`` 里就结束，早于任何目录/摘要解析。
       机器可验证的形式是：启动刷新问 registry 的那些引用**全部来自可信目录**
       （``ghcr.io/qsbb/ling-api:v<version>``），没有任何一条是请求里的字符串。
       这比"零调用"更该守——请求值压根没被使用，而不是"只读所以无所谓"。
    """
    upd_harness.write_request(updater_ws, target=digest)
    result = upd_harness.run_updater(updater_ws)
    assert result.returncode == 2, (digest, result.stdout, result.stderr)
    assert upd_harness.mutating_calls(updater_ws) == [], (digest, updater_ws.calls())
    # 只读探测只问可信目录里的 tag；坏目标没有变成任何一条 docker 参数。
    for ref in upd_harness.manifest_inspect_refs(updater_ws):
        assert ref.startswith("ghcr.io/qsbb/ling-api:v"), (digest, ref)
    assert not (updater_ws.state_dir / "task.json").exists()
    assert updater_ws.status()["task"] is None
    # 被拒绝后认领归档，避免下一轮无限重试同一个坏请求。
    assert not (updater_ws.requests_dir / f"request-{REQUEST_ID}.json").exists()
    assert (updater_ws.requests_dir / "archive" / f"claimed-{REQUEST_ID}.json").exists()


@pytest.mark.parametrize(
    "digest",
    [
        "latest",
        "ghcr.io/other/ling-api@sha256:" + "a" * 64,
        "ghcr.io/qsbb/ling-api@sha256:" + "A" * 64,
        "ghcr.io/qsbb/ling-api@sha256:" + "a" * 63,
        "ghcr.io/qsbb/ling-api@sha256:" + "g" * 64,
    ],
)
@pytest.mark.w0_web
async def test_02_api_rejects_bad_digest_without_writing_request(
    logged_in: Any, tmp_path: Path, digest: str
) -> None:
    """清单第 2 条（API 侧）：坏摘要 422，且请求目录里一个文件都不写。"""
    base = api_harness._configure(logged_in, tmp_path)
    api_harness._write_status(base)
    response = await api_harness._post(logged_in, api_harness._body(target_digest=digest))
    assert response.status_code == 422, (digest, response.text)
    assert response.json()["error"]["detail"] == "invalid_target_digest"
    assert api_harness._request_files(base) == []


# ================================================================ 清单第 3 条
# 发布证据不符 → 拒绝（镜像内 LING_COMMIT ≠ OCI revision / commit 非 40hex / 平台不符）。


def test_03_publish_rejects_source_commit_not_matching_image(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 3 条：操作者声明的源提交 ≠ 镜像 OCI 标注 → 拉取阶段拒绝。"""
    result = pub_harness.run_publish(
        publish_ws, extra=["--source-commit", OTHER_COMMIT]
    )
    assert result.returncode == 14, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_pull")
    assert record["failure"]["step"] == "image_commit"
    assert record["target"]["source_commit"] == OTHER_COMMIT
    assert record["migration"]["attempted"] is False
    assert _compose(publish_ws, "stop") == []
    assert [line for line in publish_ws.calls() if line.startswith("backup ")] == []
    assert _compose(publish_ws, "run") == []


def test_03_publish_rejects_non_hex_image_commit(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 3 条：镜像标注的 commit 不是 40 位小写 hex → 拒绝且不迁移。"""
    result = pub_harness.run_publish(publish_ws, env={"STUB_COMMIT": "not-a-commit"})
    assert result.returncode == 14, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_pull")
    assert record["failure"]["step"] == "image_commit"
    assert "40 位" in record["failure"]["message"]
    assert [line for line in publish_ws.calls() if line.startswith("backup ")] == []
    assert _compose(publish_ws, "run") == []


def test_03_publish_rejects_platform_mismatch(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 3 条：拉到的镜像架构 ≠ 宿主 → 拒绝，不备份不迁移。"""
    result = pub_harness.run_publish(
        publish_ws, env={"STUB_IMAGE_ARCH": pub_harness.OTHER_ARCH}
    )
    assert result.returncode == 14, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_pull")
    assert record["failure"]["step"] == "image_platform"
    assert record["migration"]["attempted"] is False
    assert [line for line in publish_ws.calls() if line.startswith("backup ")] == []
    assert _compose(publish_ws, "run") == []


def test_03_updater_rejects_env_commit_not_matching_oci_revision(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 3 条（更新器侧）：镜像内 LING_COMMIT ≠ OCI revision → 拒绝。"""
    upd_harness.write_request(updater_ws)
    result = upd_harness.run_updater(
        updater_ws, env={"STUB_LABEL_COMMIT": OTHER_COMMIT}
    )
    assert result.returncode == 14, (result.stdout, result.stderr)
    task = updater_ws.task()
    _assert_task_failure(task, state="failed_not_started")
    assert task["failure"]["code"] == "pull_failed"
    assert task["failure"]["stage"] == "updater_self_check"
    assert [line for line in updater_ws.calls() if line.startswith("backup ")] == []
    assert _compose(updater_ws, "run") == []
    assert _compose(updater_ws, "up") == []
    assert updater_ws.status()["task"]["state"] == "failed_not_started"


def test_03_updater_self_check_is_observably_distinct_from_publish_fallback(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 3 条的覆盖缺口（M3）：更新器自己那份检查必须**单独可观测**。

    ``publish.verify_target_image_identity`` 有一份同源检查，更新器复用它的
    Context；如果两层用同一个 failure stage，删掉更新器那一份后 publish 仍会
    兜住，用例继续绿（行为无洞，但测试有缺口）。

    这里断言失败 stage 是更新器专用的 ``updater_self_check``。publish 兜底时
    step 是 ``image_commit``；所以只删更新器那份检查，本用例会红，而
    ``test_03_updater_rejects_env_commit_not_matching_oci_revision`` 仍可能绿。
    """
    upd_harness.write_request(updater_ws)
    result = upd_harness.run_updater(
        updater_ws, env={"STUB_LABEL_COMMIT": OTHER_COMMIT}
    )
    assert result.returncode == 14, (result.stdout, result.stderr)
    assert "更新器复核" in result.stderr, result.stderr
    task = updater_ws.task()
    assert task["failure"]["code"] == "pull_failed"
    assert task["failure"]["stage"] == "updater_self_check", task["failure"]
    assert updater_ws.status()["task"]["failure"]["stage"] == "updater_self_check"
    # 没有进入任何会改变服务/数据的阶段。
    assert _compose(updater_ws, "run") == []
    assert _compose(updater_ws, "up") == []
    assert [line for line in updater_ws.calls() if line.startswith("backup ")] == []


# ================================================================ 清单第 4 条
# 备份缺媒体（media_state=partial）→ 拒绝迁移。


def test_04_publish_refuses_migration_when_media_partial(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 4 条：备份只归档了部分媒体 → 退出 13、不迁移；未迁移可恢复旧服务。"""
    result = pub_harness.run_publish(
        publish_ws, env={"FAKE_BACKUP_MODE": "media_partial"}
    )
    assert result.returncode == 13, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_backup")
    assert record["failure"]["step"] == "backup_manifest"
    assert "media_state=partial" in record["failure"]["message"]
    assert record["backup"]["complete"] is False
    assert _compose(publish_ws, "run") == []
    # 未迁移：允许并应当恢复旧 api/worker（用旧镜像），且只有实测通过才算。
    _assert_failed_before_migration_restores_old(publish_ws, record)


@pytest.mark.w0_web
async def test_04_api_blocks_request_when_media_partial(
    logged_in: Any, tmp_path: Path
) -> None:
    """清单第 4 条（API 侧）：恢复点媒体不完整 → 不可安装、拒绝提交、原因可执行。"""
    base = api_harness._configure(logged_in, tmp_path)
    api_harness._write_status(
        base,
        task=None,
        # reason 与字段必须同源：真实更新器遇到 partial 时会写 media_partial。
        backup_gate=api_harness._gate(media="partial", reason="media_partial"),
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "backup_gate_missing"
    assert view["backup_gate"]["restore_point"] is False
    assert view["backup_gate"]["media_state"] == "partial"
    assert "媒体" in (view["backup_gate"]["reason_message"] or "")
    assert view["next_actions"]
    response = await api_harness._post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "backup_gate_missing"
    assert api_harness._request_files(base) == []


def test_04_updater_backup_gate_reports_media_partial(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 4 条（更新器侧）：状态文件的 backup_gate 必须如实说媒体不完整。"""
    upd_harness.write_manifest(updater_ws, media="partial")
    result = upd_harness.run_updater(updater_ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    gate = updater_ws.status()["backup_gate"]
    assert gate["restore_point"] is False
    assert gate["complete"] is True  # manifest 结构完整，但媒体不完整
    assert gate["media_state"] == "partial"
    assert gate["reason"] == "media_partial"
    # 幂等：只写状态，不迁移、不 compose、不写任务。启动时的目录刷新会做只读
    # 探测（读容器镜像版本 + 对目录里固定的 tag 做 manifest inspect），
    # 那是读事实，不是变更——见 `test_updater._READ_ONLY_CALLS` 的注释。
    assert upd_harness.mutating_calls(updater_ws) == [], updater_ws.calls()


# ================================================================ 清单第 5 条
# 备份缺根密钥（keys_env_set=false / keys_root=absent）→ 拒绝迁移。


def test_05_publish_refuses_migration_when_root_key_absent(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 5 条：keys_env_set=false 且 keys_root=absent → 退出 13、不迁移。"""
    result = pub_harness.run_publish(publish_ws, env={"FAKE_BACKUP_MODE": "keys_absent"})
    assert result.returncode == 13, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_backup")
    assert record["failure"]["step"] == "backup_manifest"
    assert "根密钥" in record["failure"]["message"] or "keys_env_set=false" in record["failure"]["message"]
    assert record["backup"]["complete"] is False
    assert _compose(publish_ws, "run") == []
    _assert_failed_before_migration_restores_old(publish_ws, record)


@pytest.mark.w0_web
async def test_05_api_blocks_request_when_root_key_missing(
    logged_in: Any, tmp_path: Path
) -> None:
    """清单第 5 条（API 侧）：缺根密钥 → 恢复点无效、不可安装、拒绝提交。"""
    base = api_harness._configure(logged_in, tmp_path)
    api_harness._write_status(
        base,
        task=None,
        backup_gate=api_harness._gate(
            keys_root="absent", keys_env="false", reason="keys_missing"
        ),
    )
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "backup_gate_missing"
    assert view["backup_gate"]["restore_point"] is False
    assert view["backup_gate"]["keys_env_set"] == "false"
    assert view["backup_gate"]["keys_root"] == "absent"
    assert "根密钥" in (view["backup_gate"]["reason_message"] or "")
    response = await api_harness._post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "backup_gate_missing"
    assert api_harness._request_files(base) == []


def test_05_updater_backup_gate_reports_keys_missing(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 5 条（更新器侧）：状态文件必须给出 keys_missing，而不是假装可用。"""
    upd_harness.write_manifest(updater_ws, keys_root="absent", keys_env="false")
    result = upd_harness.run_updater(updater_ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    gate = updater_ws.status()["backup_gate"]
    assert gate["restore_point"] is False
    assert gate["keys_root"] == "absent"
    assert gate["keys_env_set"] == "false"
    assert gate["reason"] == "keys_missing"
    # 同上：只读探测之外零调用（不迁移、不 compose、不写任务）。
    assert upd_harness.mutating_calls(updater_ws) == [], updater_ws.calls()


# ================================================================ 清单第 6 条
# 迁移跳过（日志有「跳过迁移」）→ 失败，不启动新 API。


def test_06_migration_skip_marker_fails_without_starting_api(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 6 条：日志出现「跳过迁移」→ 退出 15、不启动新 API、不回滚。"""
    result = pub_harness.run_publish(publish_ws, env={"STUB_MIGRATION_SKIP": "1"})
    assert result.returncode == 15, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_migration")
    assert record["failure"]["step"] == "migrate_skipped"
    assert record["migration"]["skip_marker"] is True
    assert record["migration"]["attempted"] is True
    # 不启动新 API：没有 up（也没有带新镜像的 up）。
    assert _compose(publish_ws, "up") == []
    # 迁移日志被留存，供人工核对（不是只靠提示文案）。
    log_path = Path(record["migration"]["log"])
    assert log_path.is_file()
    assert "跳过迁移" in log_path.read_text(encoding="utf-8")
    # 已迁移后失败：不自动切回旧镜像、不恢复旧 dump。
    assert record["rollback"]["attempted"] is False
    _assert_no_rollback_commands(publish_ws, start=_index_of(publish_ws, "migration-run"))


# ================================================================ 清单第 7 条
# 迁移失败（退出码非 0）→ 不启动新 API、不回滚旧镜像。


def test_07_publish_migration_failure_never_starts_api_or_rolls_back(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 7 条：迁移命令退出码 7 → 退出 15、没有 up、没有旧镜像回滚动作。"""
    result = pub_harness.run_publish(publish_ws, env={"STUB_MIGRATE_EXIT": "7"})
    assert result.returncode == 15, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_migration")
    assert record["failure"]["step"] == "migrate"
    assert record["migration"]["exit_code"] == 7
    assert record["migration"]["attempted"] is True
    assert _compose(publish_ws, "up") == []
    assert record["rollback"]["attempted"] is False
    # 「不回滚旧镜像」= 迁移之后没有 docker tag/load/save、没有 pg_restore、
    # 没有用旧摘要启动 api/worker。
    _assert_no_rollback_commands(publish_ws, start=_index_of(publish_ws, "migration-run"))


def test_07_updater_migration_failure_stays_in_maintenance_without_rollback(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 7 条（更新器侧）：迁移失败 → needs_manual、维护保持、不回滚、不谎称旧版。"""
    upd_harness.write_request(updater_ws)
    result = upd_harness.run_updater(updater_ws, env={"STUB_MIGRATE_EXIT": "7"})
    assert result.returncode == 15, (result.stdout, result.stderr)
    task = updater_ws.task()
    _assert_task_failure(task, state="needs_manual")
    # 迁移命令失败落在 migrate 阶段；失败码由 failure_code 单独分类（见下一条）。
    assert task["failure"]["stage"] == "migrate"
    assert task["still_on_old_version"] is False  # 已迁移：绝不宣称仍在旧版本
    assert task["maintenance"]["state"] == "active"
    assert task["rollback"]["attempted"] is False
    # 不启动新 api/worker；没有旧镜像 up，也没有镜像归档类回滚命令。
    # 迁移前的 `docker run` 只读探测（读目标 head）不算回滚，所以从迁移调用之后看。
    assert _compose(updater_ws, "up") == []
    _assert_no_rollback_commands(updater_ws, start=_index_of(updater_ws, "migration-run"))
    status = updater_ws.status()
    assert status["updater"]["state"] == "needs_manual"
    assert status["task"]["state"] == "needs_manual"


def test_07_updater_migration_failure_reports_migration_failed_code(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 7 条（可见性）：迁移失败保留 migration_failed 码，便于页面给对处置。

    ``deploy/updater/updater.py::failure_code`` 先判 ``status=failed_migration``；
    否则 ``after_migration`` 会把迁移命令失败也归成 ``switch_failed``，网页就会
    把「迁移失败」显示成「目标版本启动或验证失败」，让人查错方向。
    """
    upd_harness.write_request(updater_ws)
    result = upd_harness.run_updater(updater_ws, env={"STUB_MIGRATE_EXIT": "7"})
    assert result.returncode == 15, (result.stdout, result.stderr)
    task = updater_ws.task()
    assert task["state"] == "needs_manual"
    assert task["failure"]["code"] == "migration_failed", task["failure"]
    assert task["failure"]["code"] != "switch_failed"


def test_07_updater_migration_skip_reports_migration_failed_code(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 7 条（独立回归）：迁移阶段的**另一条**失败路径（跳过迁移）也必须是
    ``migration_failed``。

    只修「migrate 退出码非 0」这一条不算修好：分类必须按阶段
    （``status=failed_migration``）而不是按具体 step。回退 ``failure_code`` 的
    顺序会让这里变成 ``switch_failed``。
    """
    upd_harness.write_request(updater_ws)
    result = upd_harness.run_updater(updater_ws, env={"STUB_MIGRATION_SKIP": "1"})
    assert result.returncode == 15, (result.stdout, result.stderr)
    task = updater_ws.task()
    _assert_task_failure(task, state="needs_manual")
    assert task["failure"]["code"] == "migration_failed", task["failure"]
    assert task["failure"]["stage"] == "migrate_skipped"
    assert task["still_on_old_version"] is False
    assert task["maintenance"]["state"] == "active"
    assert task["rollback"]["attempted"] is False
    # 跳过迁移 = 没有真的迁移：不得启动新 API，也不得回滚。
    assert _compose(updater_ws, "up") == []
    _assert_no_rollback_commands(updater_ws, start=_index_of(updater_ws, "migration-run"))


# ================================================================ 清单第 8 条
# schema head 不一致（迁移后 alembic_version ≠ 目标 head）→ 不启动新 API。


def test_08_schema_head_mismatch_never_starts_api(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 8 条：迁移命令成功但 alembic_version 没到目标 head → 拒绝启动。"""
    result = pub_harness.run_publish(publish_ws, env={"STUB_MIGRATE_SET_VERSION": ""})
    assert result.returncode == 15, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_migration")
    assert record["failure"]["step"] == "migrate_head"
    assert record["migration"]["current_after"] == HEAD_OLD
    assert record["migration"]["target_head"] == HEAD_NEW
    assert record["migration"]["current_after"] != record["migration"]["target_head"]
    assert _compose(publish_ws, "up") == []
    assert record["rollback"]["attempted"] is False
    _assert_no_rollback_commands(publish_ws, start=_index_of(publish_ws, "migration-run"))


def test_08_schema_head_drift_to_other_revision_never_starts_api(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 8 条：迁移后 head 漂到**另一个**修订（不是没前进）也必须拒绝。"""
    result = pub_harness.run_publish(
        publish_ws, env={"STUB_MIGRATE_SET_VERSION": "9999_other_branch"}
    )
    assert result.returncode == 15, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_migration")
    assert record["failure"]["step"] == "migrate_head"
    assert record["migration"]["current_after"] == "9999_other_branch"
    assert _compose(publish_ws, "up") == []


# ================================================================ 清单第 9 条
# 端口冲突（API 发布端口 / WS 端口）→ 预检拒绝，不改动。


def test_09_preflight_rejects_foreign_holder_on_api_publish_port(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 9 条（API 发布端口）：被别的项目占用 → 预检失败，stop/backup 都不发生。"""
    owner = json.dumps({"id": "other-id", "project": "other", "name": "other-api"})
    result = pub_harness.run_publish(publish_ws, env={"STUB_PORT_OWNER": owner})
    assert result.returncode == 10, (result.stdout, result.stderr)
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_preflight")
    assert record["failure"]["step"] == "port"
    assert "被其他进程占用" in record["failure"]["message"]
    pub_harness.assert_no_changes(publish_ws)
    assert record["migration"]["attempted"] is False


def _install_foreign_holder_for_port(ws: pub_harness.Workspace, port: int) -> str:
    """在 docker stub 前面加一层包装：只对 ``publish=<port>/tcp`` 返回外部占用者。

    stub 本身不区分端口（任何 publish 过滤器都返回当前 api），所以这里用包装脚本
    只伪造 WS 端口的占用，避免把 6520 也一起弄脏。
    """
    stub_dir = Path(ws.env["PATH"].split(os.pathsep)[0])
    real_stub = stub_dir / "docker"
    wrapper_dir = stub_dir.parent / f"bin-holder-{port}"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / "docker"
    wrapper.write_text(
        f"""#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[:1] == ["ps"] and f"publish={port}/tcp" in args:
    print("foreign-id|other|other-api")
    sys.exit(0)
os.execv(sys.executable, [sys.executable, {str(real_stub)!r}] + args)
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return f"{wrapper_dir}{os.pathsep}{ws.env['PATH']}"


def _write_ws_override(ws: pub_harness.Workspace) -> Path:
    override = ws.root / "deploy" / "compose.override.ws.yaml"
    override.write_text(
        "services:\n  api:\n    ports:\n      - \"6799:6799\"\n", encoding="utf-8"
    )
    return override


def _install_merged_ws_docker(ws: pub_harness.Workspace, port: int) -> str:
    """包装 docker stub：``compose config`` 的**合并结果**里才有 WS 端口。

    真实 Compose 会把 ``-f override`` 的 ``ports`` **追加**到 api；stub 只会原样
    打印主文件那份 JSON。这里在包装层模拟合并：主文件里没有 WS 端口，只有
    ``config`` 的输出里才有——预检若只看主文件/只看 6520 就会放行。同时对
    ``publish=<port>/tcp`` 伪造外部占用者。
    """
    stub_dir = Path(ws.env["PATH"].split(os.pathsep)[0])
    real_stub = stub_dir / "docker"
    wrapper_dir = stub_dir.parent / f"bin-merged-ws-{port}"
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / "docker"
    wrapper.write_text(
        f"""#!/usr/bin/env python3
import json
import os
import subprocess
import sys

REAL = {str(real_stub)!r}
PORT = {port}
args = sys.argv[1:]

if args[:1] == ["ps"] and f"publish={{PORT}}/tcp" in args:
    print("foreign-id|other|other-api")
    sys.exit(0)

if "compose" in args and "config" in args:
    proc = subprocess.run(
        [sys.executable, REAL] + args, capture_output=True, text=True
    )
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        sys.exit(proc.returncode)
    model = json.loads(proc.stdout)
    model["services"]["api"]["ports"].append(
        {{"mode": "ingress", "target": PORT,
          "published": str(PORT), "protocol": "tcp"}}
    )
    print(json.dumps(model))
    sys.exit(0)

os.execv(sys.executable, [sys.executable, REAL] + args)
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return f"{wrapper_dir}{os.pathsep}{ws.env['PATH']}"


def test_09_preflight_rejects_foreign_holder_on_ws_port(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 9 条（WS 端口）：WS 端口被外部占用也应**预检拒绝**，不改动。

    ``compose.image.ws.example.yaml`` 给 api 追加一条 WS 端口；这条冲突只会在
    切换阶段的 ``up -d api`` 由 Docker 报错（那时已经迁移过了）。预检必须逐个
    宿主端口核对占用者，冲突时退出 10、零改动。
    """
    config = json.loads(publish_ws.config_path.read_text(encoding="utf-8"))
    config["services"]["api"]["ports"].append(
        {"mode": "ingress", "target": WS_PORT, "published": str(WS_PORT), "protocol": "tcp"}
    )
    publish_ws.config_path.write_text(json.dumps(config), encoding="utf-8")
    override = _write_ws_override(publish_ws)
    env = {"PATH": _install_foreign_holder_for_port(publish_ws, WS_PORT)}
    result = pub_harness.run_publish(
        publish_ws,
        extra=["--compose-override", str(override)],
        env=env,
    )
    assert result.returncode == 10, (
        "WS 端口被外部占用时预检仍放行（应退出 10）",
        result.returncode,
        result.stdout,
        result.stderr,
        publish_ws.calls(),
    )
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_preflight")
    assert record["failure"]["step"] == "port"
    assert str(WS_PORT) in record["failure"]["message"]
    published = {item["published"] for item in record["published_ports"]}
    # 记录的是**合并后模型**的全部宿主端口：api 的 6520 与追加的 WS 端口都在。
    assert record["published_port"] in published
    assert WS_PORT in published
    pub_harness.assert_no_changes(publish_ws)


def test_09_preflight_rejects_ws_port_only_visible_in_merged_model(
    publish_ws: pub_harness.Workspace,
) -> None:
    """清单第 9 条（独立回归）：WS 端口**只**出现在 override 的合并结果里。

    主 compose 文件的 JSON 原样不动（没有 WS 端口），包装层按真实 Compose 的
    追加语义只在 ``config`` 输出里加 6799。只看主文件、只看 api 的 6520、或把
    override 漏出 ``-f`` 文件组，这一条都会失败——它是缺口 1 的独立看门狗。
    """
    override = _write_ws_override(publish_ws)
    env = {"PATH": _install_merged_ws_docker(publish_ws, WS_PORT)}
    result = pub_harness.run_publish(
        publish_ws,
        extra=["--compose-override", str(override)],
        env=env,
    )
    assert result.returncode == 10, (
        "合并模型里的 WS 端口冲突没有在预检被拒绝（应退出 10）",
        result.returncode,
        result.stdout,
        result.stderr,
        publish_ws.calls(),
    )
    record = publish_ws.record()
    _assert_publish_failure_record(record, status="failed_preflight")
    assert record["failure"]["step"] == "port", record["failure"]
    assert str(WS_PORT) in record["failure"]["message"]
    assert [item for item in record["published_ports"] if item["published"] == WS_PORT]
    pub_harness.assert_no_changes(publish_ws)
    # 预检失败发生在任何维护动作之前：旧 api/worker 仍在跑，没有被停过。
    state = json.loads(Path(publish_ws.state_path).read_text(encoding="utf-8"))
    assert state["running"] == {"api": True, "worker": True}


# ================================================================ 清单第 10 条
# 断电/更新器重启（状态文件存在但任务中断；DB 已迁移 vs 未迁移）。


def test_10_restart_after_migration_stops_at_manual_gate_without_new_task(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 10 条（DB 已迁移）：按实况对账 → 人工门禁，不新建第二个任务。

    更新器重启后持久任务停在 switch 阶段、migration.attempted=true。此时**不能**
    继续原阶段、不能回滚，也不能把页面上的 pending 请求当成新任务执行。
    """
    upd_harness.seed_task(updater_ws, stage="switch", attempted=True)
    upd_harness.write_request(updater_ws)
    result = upd_harness.run_updater(updater_ws)
    assert result.returncode == 17, (result.stdout, result.stderr)
    task = updater_ws.task()
    _assert_task_failure(task, state="needs_manual")
    assert task["failure"]["code"] == "switch_failed"
    assert task["still_on_old_version"] is False
    assert task["request_id"] == REQUEST_ID_B
    # 没有第二个任务：请求文件仍在，没有认领/归档。
    assert (updater_ws.requests_dir / f"request-{REQUEST_ID}.json").exists()
    assert not (updater_ws.requests_dir / "archive").exists() or not list(
        (updater_ws.requests_dir / "archive").glob("claimed-*.json")
    )
    # 不继续迁移、不切旧镜像；若重新关门，只能用维护副本。
    assert _compose(updater_ws, "run") == []
    for line in _compose(updater_ws, "up"):
        assert "/maintenance/" in line, line
    assert _calls(updater_ws, f"LING_APP_IMAGE={OLD_REF}") == []
    _assert_no_rollback_commands(updater_ws)
    status = updater_ws.status()
    assert status["updater"]["state"] == "needs_manual"
    assert status["task"]["state"] == "needs_manual"
    assert status["task"]["request_id"] == REQUEST_ID_B

    # 再跑一轮（模拟更新器 watchdog 重启）：人工门禁仍在时必须一步都不动，
    # 也不能把排队中的请求变成第二个任务。
    updater_ws.reset()
    again = upd_harness.run_updater(updater_ws)
    assert again.returncode == 17, (again.stdout, again.stderr)
    # 「一步都不动」= 没有任何**变更**动作。人工门禁要重写状态文件，写之前会
    # 刷一次目录（只读：容器镜像版本 + 固定 tag 的 manifest inspect），
    # 那不改环境；`test_updater._READ_ONLY_CALLS` 是这把尺子。
    assert upd_harness.mutating_calls(updater_ws) == [], updater_ws.calls()
    assert updater_ws.task()["request_id"] == REQUEST_ID_B
    assert (updater_ws.requests_dir / f"request-{REQUEST_ID}.json").exists()


def test_10_restart_before_migration_reconciles_and_measures_old_version(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 10 条（DB 未迁移）：对账后恢复并**实测**旧服务，不谎称成功。

    设计 §6 第三行：「按持久任务 ID 和容器/DB 实况对账」。可确认未迁移时允许
    恢复旧服务；但 ``still_on_old_version=true`` 只能来自实测（/readyz + 容器 image）。
    """
    upd_harness.seed_task(updater_ws, stage="backup", attempted=False, maintenance="active")
    result = upd_harness.run_updater(updater_ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    task = updater_ws.task()
    _assert_task_failure(task, state="failed_not_started")
    assert task["still_on_old_version"] is True
    assert task["maintenance"]["state"] == "released"
    assert task["request_id"] == REQUEST_ID_B
    # 没有新建第二个任务（没有执行任何 pending 请求）。
    assert list(updater_ws.requests_dir.glob("request-*.json")) == []
    # 没有继续迁移；恢复旧服务并实测（旧摘要由 docker-env 行记录）。
    assert _compose(updater_ws, "run") == []
    assert [
        line
        for line in updater_ws.calls()
        if line.startswith("docker-env up ") and f"LING_APP_IMAGE={OLD_REF}" in line
    ], updater_ws.calls()
    assert _calls(updater_ws, "http GET /readyz")


def test_10_restart_with_unknown_db_state_does_not_mutate(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 10 条（无法确认）：DB 版本读不到 → 停在人工门禁，一步都不改。"""
    upd_harness.seed_task(updater_ws, stage="migrate", attempted=False)
    result = upd_harness.run_updater(updater_ws, env={"STUB_DB_EXEC_EXIT": "1"})
    assert result.returncode == 17, (result.stdout, result.stderr)
    task = updater_ws.task()
    _assert_task_failure(task, state="needs_manual")
    assert task["failure"]["code"] == "reconcile_needs_manual"
    assert task["still_on_old_version"] is None  # 未实测：不能说是，也不能说不是
    assert upd_harness.mutating_compose_calls(updater_ws) == []
    assert _calls(updater_ws, "docker-env up") == []
    assert _calls(updater_ws, "http GET") == []
    assert updater_ws.status()["updater"]["state"] == "needs_manual"


# ================================================================ 清单第 11 条
# 断连重连（前端轮询中断后继续）→ 同一 request_id 延续，不断言失败。


@pytest.mark.w0_web
async def test_11_api_reconnect_keeps_same_request_and_does_not_report_failure(
    logged_in: Any, tmp_path: Path
) -> None:
    """清单第 11 条：页面断连不产生第二个任务，重连后仍读同一任务 ID/阶段。

    服务端没有"轮询会话"概念，断连/重连的可观察差异就是：重复 GET 不改变任务、
    不写请求文件、不把未知状态断言成失败；新的提交被「进行中」挡住。
    """
    base = api_harness._configure(logged_in, tmp_path)
    task_payload = api_harness._task(state="running", stage="migrate")
    api_harness._write_status(base, state="busy", task=task_payload)

    first = (await logged_in.client.get("/v1/version/image-update")).json()
    # 模拟断连期间没有任何写入；重连后继续轮询。
    second = (await logged_in.client.get("/v1/version/image-update")).json()

    for view in (first, second):
        assert view["task"]["request_id"] == task_payload["request_id"]
        assert view["task"]["state"] == "running"
        assert view["task"]["stage"] == "migrate"
        assert view["task"]["failure"] is None
        assert view["reason"] == "update_in_progress"
        assert view["installable"] is False
        assert view["message"] and "进行中" in view["message"]
    assert first["task"]["request_id"] == second["task"]["request_id"]
    assert api_harness._request_files(base) == []

    # 重连后用户再点一次：应被「进行中」拒绝，而不是新建第二个任务。
    response = await api_harness._post(logged_in, api_harness._body(request_id=uuid.uuid4().hex))
    assert response.status_code == 409, response.text
    assert response.json()["error"]["detail"] == "update_in_progress"
    assert api_harness._request_files(base) == []
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    _assert_ui_never_succeeded(view)


# ================================================================ 清单第 12 条
# 旧执行器残留（源码模式 pending.json / 旧 status）→ 显式拒绝或忽略。


def test_12_updater_ignores_legacy_source_mode_artifacts(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 12 条（更新器侧）：旧 pending.json/current.json/heartbeat.json 不被执行。

    「忽略」是设计允许的处置，但必须真的不执行源码通道：没有任何**变更类**
    docker 调用（只读的目录/版本探测除外）、没有新任务、原文件原样保留。
    """
    pending = updater_ws.requests_dir / "pending.json"
    pending.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "request_id": "upd_" + "f" * 32,
                "confirm_tag": "v1.2.3",
                "expected": {"release_id": 1, "tag": "v1.2.3", "commit": OLD_COMMIT},
            }
        ),
        encoding="utf-8",
    )
    legacy = {
        "current.json": json.dumps({"schema_version": "v1", "job": {"state": "queued"}}),
        "heartbeat.json": json.dumps({"schema_version": "v1", "status": "ok"}),
    }
    legacy_paths = []
    for name, payload in legacy.items():
        path = updater_ws.state_dir / name
        path.write_text(payload, encoding="utf-8")
        legacy_paths.append(path)
    all_legacy = [pending, *legacy_paths]
    snapshot = {path.name: path.read_bytes() for path in all_legacy}

    result = upd_harness.run_updater(updater_ws)
    assert result.returncode == 0, (result.stdout, result.stderr)
    # 没走错通道：源码模式的 pending 不会变成 docker 任务。
    assert upd_harness.mutating_calls(updater_ws) == [], updater_ws.calls()
    assert not (updater_ws.state_dir / "task.json").exists()
    status = updater_ws.status()
    assert status["updater"]["state"] == "idle"
    assert status["task"] is None
    # 旧文件原样保留（忽略，而不是"顺手清理"掉可审计的现场）。
    for path in all_legacy:
        assert path.is_file()
        assert path.read_bytes() == snapshot[path.name]


@pytest.mark.w0_web
async def test_12_api_image_channel_ignores_legacy_pending_and_old_status(
    logged_in: Any, tmp_path: Path
) -> None:
    """清单第 12 条（API 侧）：旧源码通道文件不参与镜像通道门禁，也不能被顺手覆盖。

    同时断言源码模式端点被显式拒绝（不是静默改走另一条路）。
    """
    base = api_harness._configure(logged_in, tmp_path)
    pending = base / "requests" / "pending.json"
    pending_payload = json.dumps(
        {"schema_version": "v1", "request_id": "upd_" + "f" * 32, "confirm_tag": "v1.2.3"}
    )
    pending.write_text(pending_payload, encoding="utf-8")
    for name in ("current.json", "heartbeat.json"):
        (base / "status" / name).write_text(
            json.dumps({"schema_version": "v1", "status": "ok", "job": {"state": "succeeded"}}),
            encoding="utf-8",
        )
    api_harness._write_status(base)

    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["image_mode"] is True
    assert view["installable"] is True, view
    assert view["reason"] is None
    assert view["request"] is None  # pending.json 不是镜像通道请求

    request_id = uuid.uuid4().hex
    response = await api_harness._post(
        logged_in, api_harness._body(request_id=request_id)
    )
    assert response.status_code == 201, response.text
    # 新请求写的是镜像通道固定文件名；旧 pending.json 原样保留。
    assert api_harness._request_files(base) == [f"request-{request_id}.json"]
    assert pending.read_text(encoding="utf-8") == pending_payload

    # 源码模式端点必须显式拒绝，而不是在这份目录上悄悄写 pending.json。
    legacy = await logged_in.client.post(
        "/v1/version/update-requests",
        json={"confirm_tag": "v1.2.0", "expected": {}},
        headers={"Origin": "http://testserver", "X-Ling-Update": "1"},
    )
    assert legacy.status_code == 409, legacy.text
    assert legacy.json()["error"]["detail"] == "image_mode_requires_image_channel"
    assert pending.read_text(encoding="utf-8") == pending_payload


# ================================================================ 清单第 13 条
# 并发请求（同一时刻两个请求 / 同一 request_id 重放）→ 单飞、第二个被拒。


def test_13_updater_single_flight_processes_only_one_request(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 13 条（更新器单飞）：两个请求同时在场 → 只拉一次、只建一个任务。"""
    upd_harness.write_request(updater_ws, REQUEST_ID)
    upd_harness.write_request(updater_ws, REQUEST_ID_B)
    result = upd_harness.run_updater(updater_ws)
    assert result.returncode in {0, 14, 16}, (result.stdout, result.stderr)
    pulls = _compose(updater_ws, "pull")
    assert len(pulls) == 1, pulls
    task = updater_ws.task()
    assert task["request_id"] == REQUEST_ID
    # 第二个请求没有被第二个任务消费：文件还在，也没有第二个 task。
    remaining = sorted(path.name for path in updater_ws.requests_dir.glob("request-*.json"))
    assert remaining == [f"request-{REQUEST_ID_B}.json"]
    archived = sorted(path.name for path in (updater_ws.requests_dir / "archive").iterdir())
    assert archived == [f"claimed-{REQUEST_ID}.json"]
    # 状态文件只描述一个任务。
    assert updater_ws.status()["task"]["request_id"] == REQUEST_ID


def test_13_updater_duplicate_request_id_replay_is_rejected_without_second_task(
    updater_ws: upd_harness.Workspace,
) -> None:
    """清单第 13 条（同 ID 重放）：已处理过的任务 ID 不得再执行一次。"""
    upd_harness.write_request(updater_ws, REQUEST_ID)
    first = upd_harness.run_updater(updater_ws)
    assert first.returncode == 0, (first.stdout, first.stderr)
    first_task = updater_ws.task()
    assert first_task["state"] == "succeeded"
    pulls_after_first = len(_compose(updater_ws, "pull"))
    assert pulls_after_first == 1

    updater_ws.reset()
    upd_harness.write_request(updater_ws, REQUEST_ID)
    second = upd_harness.run_updater(updater_ws)
    assert second.returncode == 2, (second.stdout, second.stderr)
    # 重放没有触发任何**变更**调用（只读的启动目录刷新除外），也没有第二个
    # 任务/第二次成功。重放判定读的是 archive 目录，早于任何 docker 解析。
    assert upd_harness.mutating_calls(updater_ws) == [], updater_ws.calls()
    assert len(_compose(updater_ws, "pull")) == 0
    task = updater_ws.task()
    assert task["request_id"] == REQUEST_ID
    assert task["state"] == "succeeded"
    assert task["failure"] is None  # 旧任务记录不被重放污染
    assert not (updater_ws.requests_dir / f"request-{REQUEST_ID}.json").exists()
    # 重放请求被明确归档（不是静默丢弃）。
    assert (updater_ws.requests_dir / "archive" / f"claimed-{REQUEST_ID}.json").exists()


@pytest.mark.w0_web
async def test_13_api_concurrent_submissions_accept_one_and_reject_other(
    logged_in: Any, tmp_path: Path
) -> None:
    """清单第 13 条（API 并发）：两个提交同一时刻到达 → 只落一个请求文件。

    局限（如实说明）：ASGI transport 下这批代码的临界区没有 ``await``，两个请求
    实际会串行执行，所以本用例证明的是**外部结果**（恰好一个请求文件、另一个 409），
    不是跨进程 flock 的竞态；跨进程单飞由 ``updater`` 的 ``updater.lock`` 用例覆盖。
    """
    base = api_harness._configure(logged_in, tmp_path)
    api_harness._write_status(base)
    ids = [uuid.uuid4().hex, uuid.uuid4().hex]
    responses = await asyncio.gather(
        *(api_harness._post(logged_in, api_harness._body(request_id=rid)) for rid in ids)
    )
    codes = sorted(response.status_code for response in responses)
    assert codes == [201, 409], [(r.status_code, r.text) for r in responses]
    rejected = next(response for response in responses if response.status_code == 409)
    assert rejected.json()["error"]["detail"] == "update_in_progress"
    files = api_harness._request_files(base)
    assert len(files) == 1, files
    assert files[0] in {f"request-{rid}.json" for rid in ids}


@pytest.mark.w0_web
async def test_13_api_duplicate_request_id_replay_writes_only_one_file(
    logged_in: Any, tmp_path: Path
) -> None:
    """清单第 13 条（API 同 ID 重放）：第二次 409 duplicate_request_id，不覆盖在途请求。"""
    base = api_harness._configure(logged_in, tmp_path)
    api_harness._write_status(base)
    request_id = uuid.uuid4().hex
    first = await api_harness._post(logged_in, api_harness._body(request_id=request_id))
    assert first.status_code == 201, first.text
    second = await api_harness._post(logged_in, api_harness._body(request_id=request_id))
    assert second.status_code == 409, second.text
    assert second.json()["error"]["detail"] == "duplicate_request_id"
    assert api_harness._request_files(base) == [f"request-{request_id}.json"]


# ================================================================ 清单第 14 条
# API 侧门禁（开关关、更新器离线、协议不兼容、无恢复点、目标不在目录）。


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("disabled", "update_disabled"),
        ("offline", "updater_offline"),
        ("protocol", "protocol_unsupported"),
        ("no_restore_point", "backup_gate_missing"),
        ("target_outside_catalog", "target_not_in_catalog"),
    ],
)
@pytest.mark.w0_web
async def test_14_api_gates_reject_with_actionable_reason(
    logged_in: Any, tmp_path: Path, case: str, expected_reason: str
) -> None:
    """清单第 14 条：五类门禁都必须拒绝，并给出可执行原因（文案 + 下一步）。"""
    target_digest = api_harness.TARGET_DIGEST
    if case == "disabled":
        base = api_harness._configure(logged_in, tmp_path, enabled=False)
        api_harness._write_status(base)
    elif case == "offline":
        base = api_harness._configure(logged_in, tmp_path)
        api_harness._write_status(base, updated_at=api_harness._STALE)
    elif case == "protocol":
        base = api_harness._configure(logged_in, tmp_path)
        api_harness._write_status(base, protocol=2)
    elif case == "no_restore_point":
        base = api_harness._configure(logged_in, tmp_path)
        api_harness._write_status(
            base,
            task=None,
            backup_gate=api_harness._gate(
                restore_point=False, reason="manifest_incomplete"
            ),
        )
    else:
        base = api_harness._configure(logged_in, tmp_path)
        api_harness._write_status(base)
        target_digest = api_harness.OTHER_DIGEST

    response = await api_harness._post(
        logged_in, api_harness._body(target_digest=target_digest)
    )
    assert response.status_code == 409, (case, response.text)
    error = response.json()["error"]
    assert error["detail"] == expected_reason, (case, error)
    assert error["message"], case
    # 拒绝必须可执行：页面上的 view 给出原因码表文案 + 至少有一步可操作建议。
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    if case == "target_outside_catalog":
        # 其余门禁都过，能安装；但目录之外的摘要仍被拒绝（GET 不谎报不可安装）。
        assert view["installable"] is True
        assert view["reason"] is None
        assert "可信发布目录" in error["message"]
    else:
        assert view["installable"] is False
        assert view["reason"] == expected_reason
        assert view["message"]
        assert view["next_actions"], case
    assert api_harness._request_files(base) == []


@pytest.mark.w0_web
async def test_14_api_reports_no_available_target_when_catalog_empty(
    logged_in: Any, tmp_path: Path
) -> None:
    """清单第 14 条补充：目录里没有可用目标 → 不可安装，且原因是 no_available_target。"""
    base = api_harness._configure(logged_in, tmp_path)
    api_harness._write_status(base, available_targets=[], recommended_target=None)
    view = (await logged_in.client.get("/v1/version/image-update")).json()
    assert view["installable"] is False
    assert view["reason"] == "no_available_target"
    assert view["next_actions"]
    response = await api_harness._post(logged_in)
    assert response.status_code == 409
    assert response.json()["error"]["detail"] == "no_available_target"
    assert api_harness._request_files(base) == []
