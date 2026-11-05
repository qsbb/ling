#!/usr/bin/env python3
"""生成或校验前端发布清单（从 deploy/install.sh 抽出）。

    python3 deploy/web_manifest.py create <root> <web_dir> <target> <manifest_name>
    python3 deploy/web_manifest.py verify <root> <web_dir> <target> <manifest_name>

字段名、排序、哈希算法、schema 版本、跳过目录与错误信息均与内嵌版本一致。
"""

import argparse
import hashlib
import json
import os
import pathlib
import sys


def parse_args():
    parser = argparse.ArgumentParser(description="生成或校验前端发布清单")
    parser.add_argument("mode", help="create 或 verify")
    parser.add_argument("root", help="仓库根目录")
    parser.add_argument("web_dir", help="相对 root 的前端目录")
    parser.add_argument("target", help="dist 目录")
    parser.add_argument("manifest_name", help="清单文件名")
    return parser.parse_args()


args = parse_args()
mode = args.mode
root_raw = args.root
web_raw = args.web_dir
target_raw = args.target
manifest_name = args.manifest_name
root = pathlib.Path(root_raw)
web = root / web_raw
target = pathlib.Path(target_raw)
manifest = target / manifest_name

web_inputs = ["package.json", "pnpm-lock.yaml", "index.html", "tsconfig.json", "vite.config.ts"]
backend_files = [
    "pyproject.toml",
    "README.md",
    "alembic.ini",
    "deploy/Dockerfile.api",
    "deploy/entrypoint.sh",
]
backend_dirs = ["src", "connectors", "migrations"]
skip_names = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "node_modules",
    ".git",
    ".DS_Store",
}


def fail(message):
    print(f"前端校验失败：{message}", file=sys.stderr)
    raise SystemExit(1)


def skipped(name):
    return name in skip_names or name.endswith((".pyc", ".pyo")) or name.startswith("._")


def checked_regular(base, relative):
    rel = pathlib.PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(p in ("", ".", "..") for p in rel.parts):
        fail("非法路径")
    cur = base
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            fail("不接受符号链接")
    try:
        cur.resolve(strict=True).relative_to(base.resolve(strict=True))
    except (OSError, ValueError):
        fail("路径越界或文件不存在")
    if not cur.is_file():
        fail("缺少普通文件")
    return cur


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def add_tree(base, relative, required, prefix):
    tree = base / relative
    if tree.is_symlink():
        fail("不接受符号链接")
    if not tree.exists():
        if required:
            fail(f"缺少 {relative} 目录")
        return
    if not tree.is_dir():
        fail(f"{relative} 必须是目录")
    for current, dirs, files in os.walk(tree, followlinks=False):
        current_path = pathlib.Path(current)
        dirs[:] = sorted(name for name in dirs if not skipped(name))
        files.sort()
        for name in dirs:
            if (current_path / name).is_symlink():
                fail(f"{relative} 内不接受符号链接")
        for name in files:
            if skipped(name):
                continue
            full = current_path / name
            tree_relative = full.relative_to(tree).as_posix()
            checked_regular(tree, tree_relative)
            source[prefix + relative + "/" + tree_relative] = sha(full)


def scan_dist(base):
    if base.is_symlink() or not base.is_dir():
        fail("缺少普通 dist 目录")
    found = {}
    for current, dirs, files in os.walk(base, followlinks=False):
        current_path = pathlib.Path(current)
        dirs.sort()
        files.sort()
        for name in dirs:
            if (current_path / name).is_symlink():
                fail("dist 内不接受符号链接")
        for name in files:
            full = current_path / name
            relative = full.relative_to(base).as_posix()
            checked_regular(base, relative)
            found[relative] = sha(full)
    return found


source = {}
for rel in web_inputs:
    source[rel] = sha(checked_regular(web, rel))
add_tree(web, "src", True, "")
add_tree(web, "public", False, "")
for rel in backend_files:
    source["backend/" + rel] = sha(checked_regular(root, rel))
for rel in backend_dirs:
    add_tree(root, rel, True, "backend/")

if mode == "create":
    if manifest.is_symlink():
        fail("清单不能是符号链接")
    if target.is_symlink() or not target.is_dir():
        fail("缺少普通 dist 目录")
    assets = scan_dist(target)
    assets.pop(manifest_name, None)
    if "index.html" not in assets or len(assets) < 2:
        fail("dist 必须含 index.html 和至少一个构建资源")
    data = {"schema": 1, "release_inputs": source, "dist_files": assets}
    manifest.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
elif mode == "verify":
    if manifest.is_symlink() or not manifest.is_file():
        fail("缺少前端清单，请在同一发布源码上重新构建")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fail("清单格式无效")
    if not isinstance(data, dict) or data.get("schema") != 1:
        fail("清单 schema 不支持")
    if data.get("release_inputs") != source:
        fail("源码/锁文件与预构建产物不是同一发布版本")
    assets = data.get("dist_files")
    if not isinstance(assets, dict) or "index.html" not in assets or len(assets) < 2:
        fail("清单缺少 index 或构建资源")
    actual = {}
    for rel, expected in assets.items():
        if not isinstance(rel, str) or not isinstance(expected, str) or len(expected) != 64:
            fail("清单文件项无效")
        actual[rel] = sha(checked_regular(target, rel))
        if actual[rel] != expected:
            fail(f"产物哈希不匹配：{rel}")
    disk = set(scan_dist(target))
    disk.discard(manifest_name)
    if disk != set(actual):
        fail("dist 文件集合与清单不一致")
else:
    fail("未知清单操作")
