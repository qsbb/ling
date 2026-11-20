"""Freeze existing forbidden import edges from refactor plan §7.2.

Only explicit forbidden edges are checked here. Whether a settings import is
used read-only, or an import refers to a domain's public API, needs review.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from importlib.util import resolve_name
from pathlib import Path

LING = Path(__file__).resolve().parents[2] / "src" / "ling"
ALLOWLIST = Path(__file__).with_name("import_boundary_allowlist.json")
GLOBAL = frozenset({"config", "audit", "logging_setup"})
FORBIDDEN = {
    "api": {"worker"},
    "worker": {"api"},
    "kernel": {"api", "worker"},
    "capabilities": {"api", "worker", "kernel"},
    "domains": {"api", "worker", "kernel", "capabilities"},
    "settings": {"api", "kernel"},
    "models": {"kernel", "capabilities"},
}

Edge = tuple[str, str, str]


class Imports(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path.as_posix()
        self.layer = path.parts[0] if len(path.parts) > 1 else path.stem
        self.package = ".".join(("ling", *path.parts[:-1]))
        self.scope: list[str] = []
        self.edges: Counter[Edge] = Counter()

    def visit_FunctionDef(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def check(self, target: str) -> None:
        root, _, rest = target.partition(".")
        layer = rest.split(".", 1)[0]
        if root != "ling" or not layer or layer in GLOBAL or layer == self.layer:
            return
        if self.layer == "infra" or layer in FORBIDDEN.get(self.layer, ()):
            self.edges[(self.path, ".".join(self.scope) or "<module>", target)] += 1

    def visit_Import(self, node: ast.Import) -> None:
        for name in node.names:
            self.check(name.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            module = resolve_name("." * node.level + module, self.package)
        for name in node.names:
            self.check(f"{module}.{name.name}")


def find_violations(root: Path) -> Counter[Edge]:
    result: Counter[Edge] = Counter()
    for path in sorted(root.rglob("*.py")):
        visitor = Imports(path.relative_to(root))
        visitor.visit(ast.parse(path.read_bytes(), filename=str(path)))
        result.update(visitor.edges)
    return result


def compare_violations(actual: Counter[Edge], allowed: Counter[Edge]) -> list[str]:
    return [
        f"{problem}: {file}:{scope} -> {target} ({count} occurrence(s))"
        for problem, difference in (("new", actual - allowed), ("stale", allowed - actual))
        for (file, scope, target), count in sorted(difference.items())
    ]


def test_import_boundaries() -> None:
    allowed = Counter(tuple(entry) for entry in json.loads(ALLOWLIST.read_text(encoding="utf-8")))
    problems = compare_violations(find_violations(LING), allowed)
    assert not problems, "\n".join(problems)


def test_guard_detects_absolute_relative_and_nested_edges(tmp_path: Path) -> None:
    path = tmp_path / "domains" / "demo" / "service.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        '"""import ling.capabilities.tool"""\n'
        "# import ling.worker\n"
        "import ling.api as blocked\n"
        "from ling import worker\n"
        "from ling.settings import user_preferences\n"
        "from ling import config, audit, logging_setup\n"
        "from . import model\n"
        "async def run():\n"
        "    from ...kernel import bridge\n"
        "    from ... import capabilities\n",
        encoding="utf-8",
    )
    assert find_violations(tmp_path) == Counter(
        ("domains/demo/service.py", scope, target)
        for scope, target in (
            ("<module>", "ling.api"),
            ("<module>", "ling.worker"),
            ("run", "ling.kernel.bridge"),
            ("run", "ling.capabilities"),
        )
    )


def test_guard_distinguishes_extra_occurrences_and_stale_allowlist() -> None:
    known = ("domains/demo.py", "run", "ling.api.errors")
    assert compare_violations(Counter({known: 2}), Counter({known: 1})) == [
        "new: domains/demo.py:run -> ling.api.errors (1 occurrence(s))"
    ]
    assert compare_violations(Counter(), Counter({known: 1})) == [
        "stale: domains/demo.py:run -> ling.api.errors (1 occurrence(s))"
    ]
