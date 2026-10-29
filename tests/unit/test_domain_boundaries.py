"""域边界闸门：`domains/<A>` 不得 import `ling.domains.<B>`。

为什么需要：17 个域平铺时，"谁能调谁"只靠口头约定。一旦某个域开始直接
读别人的表，边界会在几次迭代内消失——而且**不会报错**，只会慢慢变成
一个所有东西都依赖所有东西的大泥球。

这条规则可机械检查，因此能进 CI。当前基线是 0 违规（实测），
所以不需要白名单；将来若确有跨域需求，应通过 `kernel/self_view`
或版本化事件传递，而不是加白名单把问题藏起来。

参照「一条可机械检查的边界」的约定。
"""

from __future__ import annotations

import ast
from pathlib import Path

DOMAINS_ROOT = Path(__file__).resolve().parents[2] / "domains"


def _domain_of(path: Path, base: Path | None = None) -> str | None:
    """从路径取出所属域。``base`` 可传入（负例测试用临时目录）。"""
    root = base or DOMAINS_ROOT
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    # 必须至少有 <域>/<文件>.py 两层，否则不算域内文件
    return parts[0] if len(parts) > 1 else None


def _imported_domains(tree: ast.AST) -> list[str]:
    """收集这个文件 import 的 `ling.domains.X` 里的 X。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("ling.domains."):
                found.append(node.module.split(".")[2])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("ling.domains."):
                    found.append(alias.name.split(".")[2])
    return found


def find_violations(root: Path | None = None) -> list[tuple[str, str, str]]:
    """返回 [(文件, 自己的域, 被 import 的域)]。"""
    base = root or DOMAINS_ROOT
    violations: list[tuple[str, str, str]] = []
    for path in sorted(base.rglob("*.py")):
        own = _domain_of(path, base)
        if own is None:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法错误由其他检查负责
            continue
        for other in _imported_domains(tree):
            if other != own:
                violations.append((str(path.relative_to(base.parent.parent)), own, other))
    return violations


def test_no_cross_domain_imports() -> None:
    """真实代码里不允许跨域直接 import。"""
    violations = find_violations()
    assert not violations, (
        "发现跨域 import（应改为经 kernel/self_view 或版本化事件传递）:\n"
        + "\n".join(f"  {f}: {own} → {other}" for f, own, other in violations)
    )


def test_gate_catches_a_real_violation(tmp_path: Path) -> None:
    """**负例**：故意写一个违规 import，闸门必须报错。

    没有这条，上面那个测试可能因为"根本没扫到文件"而假绿。
    """
    fake_domains = tmp_path / "domains"
    (fake_domains / "alpha").mkdir(parents=True)
    (fake_domains / "beta").mkdir(parents=True)
    (fake_domains / "alpha" / "service.py").write_text(
        "from ling.domains.beta.service import something\n", encoding="utf-8"
    )
    violations = find_violations(fake_domains)
    assert violations, "闸门没有抓到明显的跨域 import——检查逻辑本身失效了"
    assert violations[0][1] == "alpha"
    assert violations[0][2] == "beta"


def test_gate_allows_same_domain(tmp_path: Path) -> None:
    """同域内互相 import 是正常的，不能误报。"""
    fake_domains = tmp_path / "domains"
    (fake_domains / "alpha").mkdir(parents=True)
    (fake_domains / "alpha" / "service.py").write_text(
        "from ling.domains.alpha.models import thing\n", encoding="utf-8"
    )
    assert find_violations(fake_domains) == []


def test_gate_allows_non_domain_imports(tmp_path: Path) -> None:
    """import kernel / contracts / ports 不算跨域。"""
    fake_domains = tmp_path / "domains"
    (fake_domains / "alpha").mkdir(parents=True)
    (fake_domains / "alpha" / "service.py").write_text(
        "from ling.kernel.audience import readable_visibilities\n"
        "from ling.contracts.v1 import Origin\n"
        "from ling.infra.models import MemoryRow\n",
        encoding="utf-8",
    )
    assert find_violations(fake_domains) == []
