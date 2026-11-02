"""`scripts/check_compose.py` 的入口端口检查：只认**容器侧** 6520。

## 为什么需要

原实现用 `"6520" in item` 做子串匹配。compose 里 api 的发布端口是
`"${LING_PORT:-6520}:16520"` 这样的形式，**宿主侧的默认值也写着 6520**——
容器侧写错了，子串照样命中，检查继续 PASS。而安装器按 `target=6520` 等待
就绪：端口错了它不会报"配置错了"，只会等 120 秒然后超时，排查成本全落在
用户身上。

所以这里把两件事钉住：

1. `container_side_port()` 能正确取到容器侧端口（短语法 / 只有容器侧 /
   带 host_ip / IPv6 / `/tcp` 协议后缀 / 长语法 dict）；
2. 容器侧写错时 `main()` 必须返回 1（不是"命中了宿主侧的 6520 就放行"）。

既有测试里没有 check_compose 的用例（`grep -rn "check_compose" tests/` 只命中
注释），所以本文件是新建的；`ROOT` 通过 monkeypatch 指到一份临时 deploy/ 副本，
不改动真实仓库文件。
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

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
