"""`scripts/check_compose.py` 的静态校验测试。

## 第一部分：入口端口只认**容器侧** 6520

原实现用 `"6520" in item` 做子串匹配。compose 里 api 的发布端口是
`"${LING_PORT:-6520}:16520"` 这样的形式，**宿主侧的默认值也写着 6520**——
容器侧写错了，子串照样命中，检查继续 PASS。而安装器按 `target=6520` 等待
就绪：端口错了它不会报"配置错了"，只会等 120 秒然后超时，排查成本全落在
用户身上。

所以这里把两件事钉住：

1. `container_side_port()` 能正确取到容器侧端口（短语法 / 只有容器侧 /
   带 host_ip / IPv6 / `/tcp` 协议后缀 / 长语法 dict），且不切入
   `${VAR:?信息}` 内部；
2. 容器侧写错时 `main()` 必须返回 1（不是"命中了宿主侧的 6520 就放行"）。

## 第二部分：镜像模式主文件 + WS override 的结构断言

`deploy/compose.image.yaml`（镜像模式，设计文档 §4）与
`deploy/compose.image.ws.example.yaml` 的每条契约都要有**变异测试**：
把服务集/项目名/卷名/镜像引用/`build:`/`/app/web` bind/Docker socket/
发布端口/只读根/`LING_RUN_MIGRATIONS` 改坏，`main()` 必须返回 1。
只断言 PASS 不断言 FAIL 的校验等于没校验（改错了照样绿）。

既有测试里没有 check_compose 的用例（`grep -rn "check_compose" tests/` 只命中
注释），所以本文件是新建的；`ROOT` 通过 monkeypatch 指到一份临时 deploy/ 副本，
不改动真实仓库文件。
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_compose.py"

PORTS_ENTRY = '- "${LING_PORT:-6520}:6520"'


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ling_check_compose", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def compose_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, Path]:
    """把 check_compose 的 ROOT 指到一份 deploy/ 临时副本（真实仓库只读）。"""
    module = _load_module()
    repo = tmp_path / "repo"
    shutil.copytree(
        REPO_ROOT / "deploy",
        repo / "deploy",
        ignore=shutil.ignore_patterns(".env", "__pycache__"),
    )
    monkeypatch.setattr(module, "ROOT", repo)
    return module, repo


def _replace_api_ports(repo: Path, replacement: str) -> None:
    """把 compose 里 api 的那一条 ports 条目换成 `replacement`（原样一行）。"""
    path = repo / "deploy" / "compose.yaml"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines) if line.strip() == PORTS_ENTRY]
    assert len(hits) == 1, f"compose 里应有且只有一条 api ports 条目，实际命中 {hits}"
    lines[hits[0]] = f"      - {replacement}\n"
    path.write_text("".join(lines), encoding="utf-8")


# ------------------------------------------------------------------ 容器侧解析


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("8080:6520", "6520"),  # 短语法
        ("6520", "6520"),  # 只有容器侧
        ("127.0.0.1:8080:6520", "6520"),  # 带 host_ip
        ("[::1]:8080:6520", "6520"),  # IPv6 host_ip
        ("8080:6520/tcp", "6520"),  # 协议后缀
        ("${LING_PORT:-6520}:6520", "6520"),  # 变量默认值里也含 6520
        ({"target": 6520, "published": "8080"}, "6520"),  # 长语法（int）
        ({"target": "6520", "published": 18080, "protocol": "tcp"}, "6520"),  # 长语法（str）
        # 回归：容器侧写错，宿主侧默认值仍是 6520 —— 子串匹配会误判为通过。
        ("${LING_PORT:-6520}:16520", "16520"),
    ],
)
def test_container_side_port_is_parsed(
    compose_check: tuple[ModuleType, Path], entry: object, expected: str
) -> None:
    module, _repo = compose_check

    assert module.container_side_port(entry) == expected


# ------------------------------------------------------------------ 挂载源解析
#
# FG-1：卷短语法原先用裸 `split(":")`，切点落在 `${VAR:-/path}` 的 `:-` 上，
# source/target 错位后 `is_host_bind()` 判不出来——`${WEB_DIST:-/srv/dist}:
# /app/web:ro` 这种 danger bind 一路 PASS。这里钉住三件事：切分不切入插值、
# 默认值参与宿主路径判定、无默认值的插值算"静态未知"。


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("app-data:/data", ("app-data", "/data")),  # 命名卷
        ("${WEB_DIST:-/srv/dist}:/app/web:ro", ("${WEB_DIST:-/srv/dist}", "/app/web")),
        (
            "${DOCKER_SOCK:-/var/run/docker.sock}:/var/run/docker.sock",
            ("${DOCKER_SOCK:-/var/run/docker.sock}", "/var/run/docker.sock"),
        ),
        ("${MEDIA_HOST:-/data/media}:/data/media", ("${MEDIA_HOST:-/data/media}", "/data/media")),
        ({"source": "app-data", "target": "/data", "type": "volume"}, ("app-data", "/data")),
        ("/data", None),  # 匿名卷
    ],
)
def test_volume_mount_keeps_interpolation_intact(
    compose_check: tuple[ModuleType, Path], entry: object, expected: tuple[str, str] | None
) -> None:
    module, _repo = compose_check

    assert module.volume_mount(entry) == expected


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("${WEB_DIST:-/srv/dist}", "/srv/dist"),
        ("${MEDIA_HOST-/data/media}", "/data/media"),
        ("${DOCKER_SOCK:?socket 必须配置}", "socket 必须配置"),
        ("${VAR:+alternate}", None),  # 有值时的替代，静态拿不到挂载源
        ("${VAR}", None),
        ("app-data", None),
        ("${A:-${B:-/x}}", "${B:-/x}"),
    ],
)
def test_interpolation_default_is_parsed(
    compose_check: tuple[ModuleType, Path], expr: str, expected: str | None
) -> None:
    module, _repo = compose_check

    assert module.interpolation_default(expr) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("/srv/dist", True),
        ("./dist", True),
        ("~/media", True),
        ("app-data", False),
        ("${WEB_DIST:-/srv/dist}", True),  # 默认值就是实际会挂的宿主路径
        ("${SOME:-app-data}", False),  # 默认值是命名卷
        ("${A:-${B:-/x}}", True),
        ("${VAR}", False),  # 无默认值 → 交给 mount_source_unknown
    ],
)
def test_is_host_bind_uses_interpolation_default(
    compose_check: tuple[ModuleType, Path], source: str, expected: bool
) -> None:
    module, _repo = compose_check

    assert module.is_host_bind(source) is expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("${VAR}", True),
        ("${VAR:+alternate}", True),
        ("${A:-${B}}", True),  # 外层有默认值，但默认值本身没有
        ("${A:-/x}", False),
        ("${A:-app-data}", False),
        ("app-data", False),
    ],
)
def test_mount_source_unknown_only_for_unresolvable_interpolation(
    compose_check: tuple[ModuleType, Path], source: str, expected: bool
) -> None:
    module, _repo = compose_check

    assert module.mount_source_unknown(source) is expected


# ------------------------------------------------------------------ 退出码


def test_real_compose_passes(compose_check: tuple[ModuleType, Path]) -> None:
    """基线：仓库里的 compose.yaml 本身必须 PASS（否则下面的 FAIL 无意义）。"""
    module, _repo = compose_check

    assert module.main() == 0


def test_wrong_container_port_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """容器侧写成 16520、宿主侧默认值仍是 6520 → 必须 FAIL。

    这正是原实现的漏洞：`"6520" in "${LING_PORT:-6520}:16520"` 为真。
    """
    module, repo = compose_check
    _replace_api_ports(repo, '"${LING_PORT:-6520}:16520"')

    assert module.main() == 1

    out = capsys.readouterr().out
    assert "容器侧" in out, out
    assert "16520" in out, out


def test_wrong_long_syntax_target_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """长语法同样只看 target：`{target: 16520, published: 6520}` 必须 FAIL。"""
    module, repo = compose_check
    _replace_api_ports(repo, "{target: 16520, published: 6520}")

    assert module.main() == 1

    out = capsys.readouterr().out
    assert "容器侧" in out, out
    assert "16520" in out, out


def test_correct_long_syntax_target_passes(
    compose_check: tuple[ModuleType, Path],
) -> None:
    """长语法写对了（target: 6520）必须通过，证明上面两条不是一律 FAIL。"""
    module, repo = compose_check
    _replace_api_ports(repo, '{target: 6520, published: "${LING_PORT:-6520}", protocol: tcp}')

    assert module.main() == 0


def test_expose_mismatch_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """ports 容器侧 6520 但 expose 写别的：两边不一致，必须 FAIL。"""
    module, repo = compose_check
    path = repo / "deploy" / "compose.yaml"
    text = path.read_text(encoding="utf-8")
    assert '\n    expose:\n      - "6520"\n' in text
    path.write_text(
        text.replace('\n    expose:\n      - "6520"\n', '\n    expose:\n      - "8080"\n', 1),
        encoding="utf-8",
    )

    assert module.main() == 1

    out = capsys.readouterr().out
    assert "expose" in out and "6520" in out, out


def test_source_expose_removed_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """FG-2：删掉源码模式 api 的 expose 必须 FAIL，不能只是 warning。

    expose 是刻意写死的容器内端口（entrypoint/healthcheck/expose 三处一致），
    删掉它 `docker compose config` 照样通过，所以静态校验必须硬断言。
    """
    module, repo = compose_check
    _edit(repo, "compose.yaml", '\n    expose:\n      - "6520"\n', "\n")

    assert module.main() == 1

    out = capsys.readouterr().out
    assert "expose" in out and "6520" in out, out


def test_image_expose_removed_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """FG-2：镜像模式删掉 api 的 expose 同样必须 FAIL（原逻辑空列表直接跳过）。"""
    module, repo = compose_check
    _edit(repo, IMAGE, '\n    expose:\n      - "6520"\n', "\n")

    assert module.main() == 1

    out = capsys.readouterr().out
    assert "expose" in out and "6520" in out, out


# ============================================================ 镜像模式主文件
#
# 下面每条测试都做一次**变异**：把镜像主文件（或 WS override）里的一条契约
# 改坏，`main()` 必须返回 1。只断言基线 PASS 的校验，在改错后照样绿——那等于
# 没有校验。

IMAGE = "compose.image.yaml"
IMAGE_WS = "compose.image.ws.example.yaml"
IMAGE_REF_RE = re.compile(r"^    image: \$\{LING_APP_IMAGE[^\n]*$", re.M)


def _edit(repo: Path, name: str, old: str, new: str, *, count: int = 1) -> None:
    """在 deploy/<name> 里把 old 换成 new（必须至少命中 count 次）。"""
    path = repo / "deploy" / name
    text = path.read_text(encoding="utf-8")
    assert text.count(old) >= count, (name, old, text.count(old))
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def _replace_image_ref(repo: Path, replacement: str, *, which: int = 0) -> None:
    """替换第 which 条 `image: ${LING_APP_IMAGE:...}`（0=api，1=worker）。"""
    path = repo / "deploy" / IMAGE
    text = path.read_text(encoding="utf-8")
    matches = list(IMAGE_REF_RE.finditer(text))
    assert len(matches) == 2, matches
    target = matches[which]
    path.write_text(text[: target.start()] + replacement + text[target.end() :], encoding="utf-8")


def _replace_all_image_refs(repo: Path, replacement: str) -> None:
    """两条 `image: ${LING_APP_IMAGE:...}` 一起换（逐条换会让第二次只剩 1 处）。"""
    path = repo / "deploy" / IMAGE
    text = path.read_text(encoding="utf-8")
    text, count = IMAGE_REF_RE.subn(replacement, text)
    assert count == 2, count
    path.write_text(text, encoding="utf-8")


def _replace_ws_ports_line(repo: Path, replacement: str) -> None:
    """把 WS override 里那条 ports 条目整行替换掉。"""
    path = repo / "deploy" / IMAGE_WS
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines) if line.strip().startswith('- "${LING_ONEBOT_WS')]
    assert len(hits) == 1, hits
    lines[hits[0]] = f"      - {replacement}\n"
    path.write_text("".join(lines), encoding="utf-8")


def test_main_checks_both_main_files(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """基线：输出里必须同时出现两个主文件与 WS override（不是只校验源码模式）。"""
    module, _repo = compose_check

    assert module.main() == 0

    out = capsys.readouterr().out
    for label in ("compose.yaml", "compose.image.yaml", "compose.image.ws.example.yaml"):
        assert label in out, out


def test_image_services_must_be_exact(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """多一个服务（比如把 nginx/edge 加回来）必须 FAIL：备份与门禁按服务集工作。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "\nvolumes:\n",
        "\n  nginx:\n    image: nginx:alpine\n    logging: *default-logging\n\nvolumes:\n",
    )

    assert module.main() == 1
    assert "服务集" in capsys.readouterr().out


def test_image_project_name_must_be_ling(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """项目名变了，卷的实际名称就变（会挂到空卷上）→ 必须 FAIL。"""
    module, repo = compose_check
    _edit(repo, IMAGE, "name: ling\n", "name: ling-dev\n")

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "name 必须是 ling" in out or "卷的实际名称" in out, out


def test_image_volume_actual_name_cannot_be_overridden(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """给卷加 name:/external: 会换到另一个卷 → 必须 FAIL（历史数据在旧卷里）。"""
    module, repo = compose_check
    _edit(repo, IMAGE, "  db-data:\n", "  db-data:\n    name: ling-db-data\n")

    assert module.main() == 1
    assert "name:" in capsys.readouterr().out


def test_image_build_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """镜像模式不允许 build:（否则宿主会就地重建，摘要失去意义）→ 必须 FAIL。"""
    module, repo = compose_check
    _edit(repo, IMAGE, '    image: ${LING_APP_IMAGE:', '    build: ..\n    image: ${LING_APP_IMAGE:', count=1)

    assert module.main() == 1
    assert "不允许 build" in capsys.readouterr().out


def test_image_app_web_bind_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """/app/web 是镜像内的 Web 产物，bind 会把它遮蔽 → 必须 FAIL。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "      - app-data:/data\n",
        "      - app-data:/data\n      - ../clients/web-console/dist:/app/web:ro\n",
    )

    assert module.main() == 1
    assert "/app/web" in capsys.readouterr().out


def test_image_docker_socket_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """挂 Docker socket = 容器拿到宿主 root → 必须 FAIL。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "      - app-data:/data\n",
        "      - app-data:/data\n      - /var/run/docker.sock:/var/run/docker.sock:ro\n",
    )

    assert module.main() == 1
    assert "Docker socket" in capsys.readouterr().out


def test_image_source_bind_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """镜像主文件不允许任何宿主 bind（宿主要能删掉源码树）→ 必须 FAIL。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "      - app-data:/data\n",
        "      - app-data:/data\n      - ..:/app-src:ro\n",
    )

    assert module.main() == 1
    assert "宿主 bind 挂载" in capsys.readouterr().out


# ---------------------------------------------------------------- FG-1：插值挂载源


def test_image_interpolated_web_dist_bind_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`${WEB_DIST:-/srv/dist}:/app/web:ro`：默认值就是宿主 dist → 必须 FAIL。

    裸 `split(":")` 会切在 `:-` 上，target 解析成 `-/srv/dist}:/app/web`，
    `is_host_bind()` 看到的 source 是 `${WEB_DIST`，于是这条危险 bind 一路 PASS。
    """
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "      - app-data:/data\n",
        '      - app-data:/data\n      - "${WEB_DIST:-/srv/dist}:/app/web:ro"\n',
    )

    assert module.main() == 1
    assert "/app/web" in capsys.readouterr().out


def test_image_interpolated_docker_socket_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`${DOCKER_SOCK:-/var/run/docker.sock}`：默认值是宿主 socket → 必须 FAIL。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "      - app-data:/data\n",
        '      - app-data:/data\n      - "${DOCKER_SOCK:-/var/run/docker.sock}:'
        '/var/run/docker.sock"\n',
    )

    assert module.main() == 1
    assert "Docker socket" in capsys.readouterr().out


def test_image_interpolated_host_bind_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`${MEDIA_HOST:-/data/media}:/data/media`：默认值是宿主绝对路径 → 必须 FAIL。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "      - app-data:/data\n",
        '      - app-data:/data\n      - "${MEDIA_HOST:-/data/media}:/data/media"\n',
    )

    assert module.main() == 1

    out = capsys.readouterr().out
    assert "宿主 bind" in out, out
    assert "${MEDIA_HOST:-/data/media}" in out, out


def test_image_interpolated_without_default_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`${SOME_VAR}:/data`：无默认值 → 静态无法判定，镜像模式必须报错。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "      - app-data:/data\n",
        '      - app-data:/data\n      - "${SOME_VAR}:/data-extra"\n',
    )

    assert module.main() == 1

    out = capsys.readouterr().out
    assert "静态无法判定" in out, out


def test_image_interpolated_named_volume_passes(
    compose_check: tuple[ModuleType, Path],
) -> None:
    """合法写法不误伤：插值默认值是已声明的命名卷 → 仍 PASS。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "      - app-data:/data\n",
        '      - app-data:/data\n      - "${SOME:-app-data}:/data2"\n',
    )

    assert module.main() == 0


# ---------------------------------------------------------------- 更新通道挂载
#
# P5 接线改变了契约：P1 时镜像主文件按设计"初期不挂 update 通道"，旧断言是
# 「api/worker 一律不许出现 /run/ling-update*」。P5 交付了写请求/读状态的端点
# 之后，api **必须**挂这两个目录，所以那条断言改成精确断言（不是删掉）：
# api 只许 requests(rw) + status(ro)、源必须是必填插值、worker 一条都不许挂。
# 下面每条用例都做一次变异，证明新的精确断言真的会 FAIL。

UPDATE_REQUESTS_TARGET = "/run/ling-update/requests"
UPDATE_STATUS_TARGET = "/run/ling-update/status"
UPDATE_REQUESTS_MOUNT = (
    "${LING_UPDATE_REQUESTS_HOST:?请给宿主 requests 目录（安装器写入 .env）}"
    ":/run/ling-update/requests:rw"
)
UPDATE_STATUS_MOUNT = (
    "${LING_UPDATE_STATUS_HOST:?请给宿主状态目录（安装器写入 .env）}"
    ":/run/ling-update/status:ro"
)


def _replace_update_mount(repo: Path, target: str, replacement: str) -> None:
    """把镜像主文件里目标为 target 的那条挂载整行换成 replacement。"""
    path = repo / "deploy" / IMAGE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    hits = [
        index
        for index, line in enumerate(lines)
        if line.strip().startswith("- ") and f":{target}" in line
    ]
    assert len(hits) == 1, (target, hits)
    lines[hits[0]] = f"      - {replacement}\n"
    path.write_text("".join(lines), encoding="utf-8")


def _drop_update_mount(repo: Path, target: str) -> None:
    """删掉目标为 target 的那条挂载（模拟"少挂一条"）。"""
    path = repo / "deploy" / IMAGE
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    hits = [
        index
        for index, line in enumerate(lines)
        if line.strip().startswith("- ") and f":{target}" in line
    ]
    assert len(hits) == 1, (target, hits)
    del lines[hits[0]]
    path.write_text("".join(lines), encoding="utf-8")


def _add_worker_mount(repo: Path, entry: str) -> None:
    """给 worker 的 volumes 追加一条（worker 段里唯一一条是 `- app-data:/data`）。"""
    path = repo / "deploy" / IMAGE
    text = path.read_text(encoding="utf-8")
    worker_start = text.index("\n  worker:\n")
    anchor = "      - app-data:/data\n"
    index = text.index(anchor, worker_start)
    end = index + len(anchor)
    path.write_text(text[:end] + f"      - {entry}\n" + text[end:], encoding="utf-8")


def test_image_api_update_channel_mounts_are_exact(
    compose_check: tuple[ModuleType, Path],
) -> None:
    """基线：api 有 requests(rw) + status(ro)，源是必填插值；worker 一条都没有。

    旧用例断言的是"挂回来必须 FAIL"——那是在"初期不挂"的前提下写的。现在
    POST/GET 两个端点都做完，不挂等于真实部署里必然失败，所以这里断言**精确
    的允许集**，并且把 worker 的"一条都不许"单独钉住。
    """
    module, repo = compose_check
    data = yaml.safe_load((repo / "deploy" / IMAGE).read_text(encoding="utf-8"))

    api_mounts: dict[str, tuple[str, str]] = {}
    for vol in data["services"]["api"]["volumes"]:
        mount = module.volume_mount(vol)
        assert mount is not None
        source, target = mount
        if target.startswith("/run/ling-update"):
            api_mounts[target] = (source, module.volume_mode(vol))

    assert set(api_mounts) == {UPDATE_REQUESTS_TARGET, UPDATE_STATUS_TARGET}, api_mounts
    assert api_mounts[UPDATE_REQUESTS_TARGET][1] == "rw", "requests 必须 rw（API 要写请求）"
    assert api_mounts[UPDATE_STATUS_TARGET][1] == "ro", "status 必须 ro（更新器是唯一写者）"
    for source, _mode in api_mounts.values():
        assert module.REQUIRED_INTERPOLATION_RE.match(source), source

    worker_targets = [
        module.volume_mount(vol)[1] for vol in data["services"]["worker"]["volumes"]
    ]
    assert not [t for t in worker_targets if t.startswith("/run/ling-update")], worker_targets

    group_add = data["services"]["api"]["group_add"]
    assert len(group_add) == 1, group_add
    assert module.REQUIRED_INTERPOLATION_RE.match(group_add[0]), group_add
    assert "LING_UPDATE_SHARED_GID" in group_add[0], group_add
    # 真实文件必须 PASS（变异用例都在临时副本里改）。
    assert module.main() == 0


@pytest.mark.parametrize(
    "source",
    [
        "/opt/ling/requests",  # 写死宿主路径：镜像模式不允许
        "${LING_UPDATE_REQUESTS_HOST:-/opt/ling/requests}",  # 可选默认值：缺配置会静默少挂
        "${LING_UPDATE_REQUESTS_HOST}",  # 裸插值：静态不可判定，且缺值不报错
        "requests-vol",  # 命名卷：不是安装器算出来的那个宿主目录
    ],
)
def test_image_update_mount_source_must_be_required_interpolation(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str], source: str
) -> None:
    """挂载源不是 `${VAR:?...}` → 必须 FAIL（缺值让 config 直接失败）。"""
    module, repo = compose_check
    _replace_update_mount(repo, UPDATE_REQUESTS_TARGET, f"{source}:{UPDATE_REQUESTS_TARGET}:rw")

    assert module.main() == 1
    assert "必填插值" in capsys.readouterr().out


def test_image_update_requests_must_be_rw(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """requests 改成 ro（目录在但不让写）→ 必须 FAIL。"""
    module, repo = compose_check
    _replace_update_mount(
        repo, UPDATE_REQUESTS_TARGET, UPDATE_REQUESTS_MOUNT.replace(":rw", ":ro")
    )

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "必须以 rw 挂载" in out, out


def test_image_update_status_must_be_ro(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """status 改成 rw（API 能伪造更新器状态）→ 必须 FAIL。"""
    module, repo = compose_check
    _replace_update_mount(
        repo, UPDATE_STATUS_TARGET, UPDATE_STATUS_MOUNT.replace(":ro", ":rw")
    )

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "必须以 ro 挂载" in out, out


def test_image_update_mount_missing_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """少挂 status（GET 永远读不到状态）→ 必须 FAIL，不能"少挂更安全"。"""
    module, repo = compose_check
    _drop_update_mount(repo, UPDATE_STATUS_TARGET)

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "必须挂更新通道目录" in out, out
    assert UPDATE_STATUS_TARGET in out, out


def test_image_worker_update_mount_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """worker 也挂 requests（后台任务能触发部署）→ 必须 FAIL。"""
    module, repo = compose_check
    _add_worker_mount(repo, UPDATE_REQUESTS_MOUNT)

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "worker 不允许挂更新通道目录" in out, out


def test_image_api_extra_update_mount_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """api 多挂一个 /run/ling-update/archive → 必须 FAIL（最小权限）。"""
    module, repo = compose_check
    path = repo / "deploy" / IMAGE
    text = path.read_text(encoding="utf-8")
    anchor = f"      - {UPDATE_STATUS_MOUNT}\n"
    assert text.count(anchor) == 1, text.count(anchor)
    extra = (
        "      - ${LING_UPDATE_EXTRA_HOST:?请给宿主目录}:"
        "/run/ling-update/archive:rw\n"
    )
    path.write_text(text.replace(anchor, anchor + extra), encoding="utf-8")

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "只能挂" in out, out


@pytest.mark.parametrize(
    "replacement",
    [
        "",  # 整条 group_add 删掉：API 不在共享组里
        '    group_add: ["10001"]\n',  # 写死 gid：安装器按更新器 gid 设的属组失效
    ],
)
def test_image_api_shared_group_required(
    compose_check: tuple[ModuleType, Path],
    capsys: pytest.CaptureFixture[str],
    replacement: str,
) -> None:
    """api 的共享组必须来自 `LING_UPDATE_SHARED_GID` 的必填插值 → 否则 FAIL。"""
    module, repo = compose_check
    path = repo / "deploy" / IMAGE
    text = path.read_text(encoding="utf-8")
    anchor = (
        "    group_add:\n"
        '      - "${LING_UPDATE_SHARED_GID:?请给共享组 gid'
        '（安装器写入 .env：更新器与请求/状态目录的属组）}"\n'
    )
    assert text.count(anchor) == 1, text.count(anchor)
    path.write_text(text.replace(anchor, replacement), encoding="utf-8")

    assert module.main() == 1
    assert "group_add" in capsys.readouterr().out


def test_image_worker_image_must_match_api(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """worker 换成另一个引用（不同摘要/可变 tag）→ 必须 FAIL。"""
    module, repo = compose_check
    _replace_image_ref(repo, "    image: ghcr.io/qsbb/ling-api:latest", which=1)

    assert module.main() == 1
    out = capsys.readouterr().out
    assert "完全相同" in out, out


def test_image_must_use_required_interpolation(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """固定 tag 不是必填插值（会悄悄起一个别的镜像）→ 两条都改也必须 FAIL。"""
    module, repo = compose_check
    _replace_all_image_refs(repo, "    image: ghcr.io/qsbb/ling-api:latest")

    assert module.main() == 1
    assert "必填插值" in capsys.readouterr().out


def test_image_migrations_must_be_zero(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """删掉 api 的 LING_RUN_MIGRATIONS=0 会退回 entrypoint 默认（启动即迁移）→ FAIL。"""
    module, repo = compose_check
    _edit(repo, IMAGE, '      LING_RUN_MIGRATIONS: "0"\n', "", count=1)

    assert module.main() == 1
    assert "LING_RUN_MIGRATIONS" in capsys.readouterr().out


def test_image_services_expose_current_digest_in_environment(
    compose_check: tuple[ModuleType, Path],
) -> None:
    """api/worker 的 environment 必须注入 ``LING_APP_IMAGE``，且是**同一条**必填插值。

    改前 ``LING_APP_IMAGE`` 只出现在 ``image:`` 插值里，没有写进 api 的
    ``environment``（实测 api 容器 env 47 项里没有它）：API 既判断不出镜像模式，
    也拿不到当前摘要 → ``GET /v1/version/image-update`` 报 ``current_image_unknown``、
    ``POST`` 直接 409，网页更新等于不可用。

    这里断言三件事（都是契约，不是实现细节）：

    1. ``api`` 与 ``worker`` 的 environment 里**都有**它；
    2. 值是**必填**插值 ``${LING_APP_IMAGE:?...}``（``:-`` 会悄悄回退到别的镜像）；
    3. 与同一服务的 ``image:`` 逐字符相同 —— "跑的是哪个摘要"对进程可见。
    """
    module, repo = compose_check
    data = yaml.safe_load((repo / "deploy" / IMAGE).read_text(encoding="utf-8"))
    for service in ("api", "worker"):
        env = module.service_environment(data["services"][service])
        value = env.get("LING_APP_IMAGE")
        assert isinstance(value, str), f"{service}.environment 缺 LING_APP_IMAGE"
        assert module.IMAGE_APP_REQUIRED.match(value), value
        assert data["services"][service]["image"] == value, service


def test_image_update_enabled_must_be_zero(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """镜像模式初期必须 LING_UPDATE_ENABLED=0 → 改成 1 必须 FAIL。"""
    module, repo = compose_check
    _edit(repo, IMAGE, '      LING_UPDATE_ENABLED: "0"\n', '      LING_UPDATE_ENABLED: "1"\n')

    assert module.main() == 1
    assert "LING_UPDATE_ENABLED" in capsys.readouterr().out


def test_image_published_port_must_stay_6520(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """容器侧端口改成别的（实测直连 6520）→ 必须 FAIL。"""
    module, repo = compose_check
    _edit(repo, IMAGE, '- "${LING_PORT:-6520}:6520"', '- "${LING_PORT:-6500}:6500"')

    assert module.main() == 1
    assert "容器侧" in capsys.readouterr().out


def test_image_read_only_and_tmpfs_required(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """去掉只读根（或 tmpfs）→ 必须 FAIL。"""
    module, repo = compose_check
    _edit(repo, IMAGE, "    read_only: true\n", "", count=2)

    assert module.main() == 1
    assert "read_only" in capsys.readouterr().out


def test_image_rejects_source_only_vars(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """源码构建变量（LING_COMMIT 等）不属于镜像主文件 → 引用即 FAIL。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        "      LING_UPDATE_ENABLED: \"0\"\n",
        "      LING_UPDATE_ENABLED: \"0\"\n      LING_COMMIT: ${LING_COMMIT:-}\n",
    )

    assert module.main() == 1
    assert "源码模式变量" in capsys.readouterr().out


def test_image_compose_missing_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """镜像主文件缺失（只校验源码模式）也必须 FAIL，不能默默跳过。"""
    module, repo = compose_check
    (repo / "deploy" / IMAGE).unlink()

    assert module.main() == 1
    assert "缺少 deploy/compose.image.yaml" in capsys.readouterr().out


def test_output_does_not_leak_secrets(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """输出只放结构摘要：不能打印完整 config（含口令/密钥）。"""
    module, _repo = compose_check

    assert module.main() in {0, 1}

    out = capsys.readouterr().out
    assert "ling-dev-password" not in out, out
    assert "postgresql+asyncpg://" not in out, out
    assert "POSTGRES_PASSWORD=" not in out, out


# ============================================================ 镜像模式 WS override


def test_image_ws_fixed_port_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """override 写固定端口（漏配也照开）→ 必须 FAIL，要求必填插值。"""
    module, repo = compose_check
    _replace_ws_ports_line(repo, '"6799:6799"')

    assert module.main() == 1
    assert "必填插值" in capsys.readouterr().out


def test_image_ws_extra_service_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """override 只允许 api；多一个服务必须 FAIL。"""
    module, repo = compose_check
    path = repo / "deploy" / IMAGE_WS
    path.write_text(
        path.read_text(encoding="utf-8") + "\n  db:\n    ports:\n      - \"5432:5432\"\n",
        encoding="utf-8",
    )

    assert module.main() == 1
    assert "只能覆盖 api" in capsys.readouterr().out


def test_image_ws_extra_keys_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """override 里加 image: 等于绕开主文件 → 必须 FAIL。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE_WS,
        "  api:\n    ports:\n",
        "  api:\n    image: ghcr.io/qsbb/ling-api:latest\n    ports:\n",
    )

    assert module.main() == 1
    assert "只允许出现 api.ports" in capsys.readouterr().out


# --------------------------------------------------- 变量插值的端口切分（新解析）


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        # 两侧都是变量、且 `:?` 信息里还带冒号：直接 rsplit(":") 会切成 "?端口}"。
        ("${WS:?需要: 端口}:${WS:?需要: 端口}", "${WS:?需要: 端口}"),
        # `:?` 信息里带斜杠也不能被当成 /tcp 后缀剥掉。
        ("${WS:?见 /tmp/ws}:${WS:?见 /tmp/ws}", "${WS:?见 /tmp/ws}"),
        ("8080:6520/udp", "6520"),
    ],
)
def test_container_side_port_handles_nested_interpolation(
    compose_check: tuple[ModuleType, Path], entry: str, expected: str
) -> None:
    module, _repo = compose_check

    assert module.container_side_port(entry) == expected


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("${LING_PORT:-6520}:6520", "${LING_PORT:-6520}"),
        ("${WS:?需要: 端口}:${WS:?需要: 端口}", "${WS:?需要: 端口}"),
        ("[::1]:8080:6520", "[::1]:8080"),
        ("6520", None),
        ({"target": 6520, "published": "${LING_PORT:-6520}"}, "${LING_PORT:-6520}"),
    ],
)
def test_host_side_port_is_parsed(
    compose_check: tuple[ModuleType, Path], entry: object, expected: str | None
) -> None:
    module, _repo = compose_check

    assert module.host_side_port(entry) == expected


# ============================================================ FG-3：x-app-env 回归锁
#
# 原实现只把 `x-app-env` 当锚点用，没有任何逐条比对：从镜像主文件里删掉一个
# 变量、把默认值改掉、多写一个变量，`main()` 一律返回 0。而 `x-app-env` 是
# **逐项白名单**——少一行就等于用户在 `.env` 里配了也进不了容器，表现为
# "配了却没生效"，日志里一句异常都没有。所以对真实镜像主文件做变异。


@pytest.mark.parametrize(
    ("line", "var"),
    [
        ("  LING_MAX_BODY_BYTES: ${LING_MAX_BODY_BYTES:-}\n", "LING_MAX_BODY_BYTES"),
        ("  LING_ONEBOT_WS_TOKEN: ${LING_ONEBOT_WS_TOKEN:-}\n", "LING_ONEBOT_WS_TOKEN"),
        ("  LING_QQBOT_ENABLED: ${LING_QQBOT_ENABLED:-0}\n", "LING_QQBOT_ENABLED"),
    ],
)
def test_app_env_missing_var_fails(
    compose_check: tuple[ModuleType, Path],
    capsys: pytest.CaptureFixture[str],
    line: str,
    var: str,
) -> None:
    """镜像主文件比源码主文件少一个变量 → 必须 FAIL。"""
    module, repo = compose_check
    _edit(repo, IMAGE, line, "", count=1)

    assert module.main() == 1

    out = capsys.readouterr().out
    assert var in out, out
    assert "逐条相同: False" in out, out


def test_app_env_changed_value_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`PYTHONUNBUFFERED` 的默认值 `"1"` 改成 `"0"` → 必须 FAIL。

    值被改不会让任何结构断言失败（服务/卷/端口都还是对的），但容器里的默认
    行为已经变了；这类变异只有逐条比对才拦得住。
    """
    module, repo = compose_check
    _edit(repo, IMAGE, '  PYTHONUNBUFFERED: "1"\n', '  PYTHONUNBUFFERED: "0"\n')

    assert module.main() == 1

    out = capsys.readouterr().out
    assert "PYTHONUNBUFFERED" in out, out
    assert "不同" in out, out
    # 只列变量名：值里有 `${POSTGRES_PASSWORD:-ling-dev-password}` 这类口令引用。
    assert "ling-dev-password" not in out, out


def test_app_env_extra_var_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """镜像版多一个源码版没有的变量 → 必须 FAIL（镜像模式不该偷偷多出配置）。"""
    module, repo = compose_check
    _edit(
        repo,
        IMAGE,
        '  PYTHONUNBUFFERED: "1"\n',
        '  PYTHONUNBUFFERED: "1"\n  LING_ONLY_IN_IMAGE: ${LING_ONLY_IN_IMAGE:-}\n',
    )

    assert module.main() == 1

    out = capsys.readouterr().out
    assert "LING_ONLY_IN_IMAGE" in out, out
    assert "多了" in out, out


def test_app_env_value_type_difference_detected(compose_check: tuple[ModuleType, Path]) -> None:
    """`True` 与 `1`、`"1"` 与 `1` 必须算不同（只比 `==` 会把它们当成一样）。"""
    module, _repo = compose_check
    errors: list[str] = []

    module.check_app_env_parity(
        {"x-app-env": {"LING_A": True, "LING_B": "1"}},
        {"x-app-env": {"LING_A": 1, "LING_B": 1}},
        "compose.yaml",
        "compose.image.yaml",
        errors,
    )

    assert len(errors) == 1, errors
    assert "LING_A" in errors[0] and "LING_B" in errors[0], errors
    # 只列变量名，不打印值
    assert "True" not in errors[0], errors[0]


def test_app_env_summary_printed(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """基线：必须打印一行 x-app-env 摘要（数量 + 逐条相同的布尔）。"""
    module, _repo = compose_check

    assert module.main() == 0

    out = capsys.readouterr().out
    assert re.search(r"x-app-env: \d+ 个变量，与 compose.yaml 逐条相同: True", out), out


# ============================================================ FG-4：卷的**实际名称**
#
# 改前：per-key 先查 `name:`/`external:` 报一条，然后"实际卷名"是用**键名**推的，
# 永远跟着顶层 `name` 走 —— 那条"卷名必须还是 ling_db-data"的断言在 `name:`
# 变异下根本不会被触发，等于没有独立证明能力。


@pytest.mark.parametrize(
    ("conf", "expected"),
    [
        ({}, "ling_db-data"),  # 默认派生 `<项目>_<键>`
        (None, "ling_db-data"),
        ({"name": "ling_db-data"}, "ling_db-data"),  # 显式 name 优先
        ({"name": "other"}, "other"),
        ({"external": True}, "db-data"),  # 外部卷没有前缀，用键名
        ({"external": True, "name": "shared"}, "shared"),
    ],
)
def test_effective_volume_name(
    compose_check: tuple[ModuleType, Path], conf: object, expected: str
) -> None:
    module, _repo = compose_check

    assert module.effective_volume_name("ling", "db-data", conf) == expected


def test_image_volume_actual_name_is_a_single_independent_error(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`name: ling-db-data` 变异只产生**一条**错误，而且就是卷实际名断言。"""
    module, repo = compose_check
    _edit(repo, IMAGE, "  db-data:\n", "  db-data:\n    name: ling-db-data\n")

    assert module.main() == 1

    out = capsys.readouterr().out
    errors = [line for line in out.splitlines() if line.startswith("  ✗")]
    assert len(errors) == 1, out
    assert "卷的实际名称" in errors[0], out
    assert "name:" in errors[0], out
    assert "不能指定 name:" not in out, out


def test_image_volume_external_still_rejected(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`external:` 是另一件事（卷不归本项目管理），必须继续单独报错。"""
    module, repo = compose_check
    _edit(repo, IMAGE, "  db-data:\n", "  db-data:\n    external: true\n")

    assert module.main() == 1
    assert "不能指定 external:" in capsys.readouterr().out


def test_image_volume_explicit_correct_name_passes(
    compose_check: tuple[ModuleType, Path],
) -> None:
    """取舍：显式写 `name: ling_db-data`（最终名与派生名一致）→ PASS。

    实际挂的还是同一个卷，多写一行只是啰嗦；断言的是**最终名**，不是写法。
    """
    module, repo = compose_check
    _edit(repo, IMAGE, "  db-data:\n", "  db-data:\n    name: ling_db-data\n")

    assert module.main() == 0


# ============================================================ 禁止叠加主文件（设计 §4）
#
# `docker compose -f compose.yaml -f compose.image.yaml` 会把 build:、/app/web
# bind、更新通道 bind 一起带回来（实测叠加后 api 同时有 build 与镜像摘要）。
# 两份主文件是**择一**关系，必须在"叠加"这一步拦住，而不是只看单份文件。


def _pair(module: ModuleType, *paths: str) -> int:
    return module.main(["--check-override-pair", *paths])


@pytest.mark.parametrize(
    "order",
    [
        ("deploy/compose.yaml", "deploy/compose.image.yaml"),
        ("deploy/compose.image.yaml", "deploy/compose.yaml"),
    ],
)
def test_override_pair_both_main_files_fails(
    compose_check: tuple[ModuleType, Path],
    capsys: pytest.CaptureFixture[str],
    order: tuple[str, str],
) -> None:
    """两种叠加顺序都必须 FAIL（谁在前都是把两份主文件叠起来）。"""
    module, _repo = compose_check

    assert _pair(module, *order) == 1

    out = capsys.readouterr().out
    assert "禁止把 compose.yaml 与 compose.image.yaml 叠在一起" in out, out


@pytest.mark.parametrize(
    "paths",
    [
        ("deploy/compose.image.yaml", "deploy/compose.image.ws.example.yaml"),
        ("deploy/compose.yaml", "deploy/compose.image.ws.example.yaml"),
    ],
)
def test_override_pair_main_file_plus_ws_override_passes(
    compose_check: tuple[ModuleType, Path], paths: tuple[str, str]
) -> None:
    """主文件 + WS override 是正常组合（两份主文件都各配一次）→ PASS。"""
    module, _repo = compose_check

    assert _pair(module, *paths) == 0


def test_override_pair_renamed_image_copy_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """改名副本（`cp compose.image.yaml renamed.yaml`）也能认出来 → FAIL。"""
    module, repo = compose_check
    shutil.copy(repo / "deploy" / IMAGE, repo / "deploy" / "renamed-image.yaml")

    assert _pair(module, "deploy/renamed-image.yaml", "deploy/compose.yaml") == 1

    out = capsys.readouterr().out
    assert "禁止叠加主文件" in out, out


def test_override_pair_build_with_tag_override_passes(
    compose_check: tuple[ModuleType, Path],
) -> None:
    """`build:` + 普通 `image: mytag` 是合法的 tag override → 不能误报。"""
    module, repo = compose_check
    (repo / "deploy" / "tag-override.yaml").write_text(
        "services:\n  api:\n    image: ghcr.io/qsbb/ling-api:dev-local\n", encoding="utf-8"
    )

    assert _pair(module, "deploy/compose.yaml", "deploy/tag-override.yaml") == 0


def test_override_pair_missing_file_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    module, _repo = compose_check

    assert _pair(module, "deploy/compose.yaml", "deploy/does-not-exist.yaml") == 1
    assert "文件不存在" in capsys.readouterr().out


def test_override_pair_single_file_fails(
    compose_check: tuple[ModuleType, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """只给一个文件无法判断是否叠加 → FAIL（而不是默默 PASS）。"""
    module, _repo = compose_check

    assert _pair(module, "deploy/compose.yaml") == 1
    assert "至少需要两个文件" in capsys.readouterr().out


def test_override_pair_duplicate_same_file_not_counted(compose_check: tuple[ModuleType, Path]) -> None:
    """同一个文件传两次不算"两份主文件"（按真实路径去重）。"""
    module, _repo = compose_check

    assert _pair(module, "deploy/compose.yaml", "deploy/compose.yaml") == 0


def test_resolve_compose_path_relative_to_root(compose_check: tuple[ModuleType, Path]) -> None:
    """相对路径按 ROOT 解析（测试 monkeypatch ROOT 才能生效），绝对路径原样。"""
    module, repo = compose_check

    assert module._resolve_compose_path("deploy/compose.yaml") == repo / "deploy" / "compose.yaml"
    absolute = Path("/tmp/abs-compose.yaml")
    assert module._resolve_compose_path(str(absolute)) == absolute


def test_main_does_not_consume_sys_argv(
    compose_check: tuple[ModuleType, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`main()` 默认参数必须是空列表，**绝不能**默认成 `sys.argv[1:]`。

    否则测试里 `main()` 会把 pytest 的参数（`--check-override-pair` 之类）当成
    文件，现有全部用例一起变红。
    """
    module, _repo = compose_check
    monkeypatch.setattr(sys, "argv", ["check_compose.py", "--check-override-pair", "nope.yaml"])

    assert module.main() == 0
