#!/usr/bin/env python3
"""可信发布目录（``deploy/releases.json``）的校验与追加工具（纯标准库）。

设计依据
========
``docs/EASY-INSTALL-UPDATE-DESIGN.md``：

> 可信发布目录只列已通过门禁、可从已知版本升级的稳定目标，记录最低更新器协议
> 版本、是否有迁移、预计下载大小及必要的版本跨度。

**目录里没有 digest**（2026-11 去掉）。原因不是嫌它多余，而是它原来放错了地方：
目标摘要是「构建**之后**才存在」的值，而目录条目是「构建**之前**就要提交」的发布
元数据。两者顺序相反，于是每发一版都得走一遍

    构建镜像 → 等 CI 吐出 digest → 手工改 releases.json → 再提交一次

而且 ``catalog.py add`` 遇到「同版本不同 digest」还会直接拒绝（旧码
``version_digest_conflict``）——重新打包镜像（digest 变了、版本号没变）会被自己的
工具挡住，逼着人改 JSON，而人工改的东西正好是信任锚。**这是设计缺陷，不是操作
失误**：把「构建产物的事实」写进「构建前的元数据」，顺序天然矛盾。

现在目录只回答「有哪些版本、能从哪升上来、有没有迁移」这三件**发布决策**；目标
摘要改由**更新器在刷新目录时自己解析**（``docker manifest inspect
ghcr.io/qsbb/ling-api:v<version>``，取本机架构的平台 manifest digest），也就是
"要用的时候才去问 registry"。这样：

- 发版只需一次提交（版本/commit/跨度是构建前就确定的事实）；
- 重新打包同一版本不再需要改目录——digest 由 registry 现场回答；
- 信任锚仍是**随代码提交 + 评审**的那份目录（版本、commit、跨度），digest 来自
  registry 的不可变内容寻址，走的是更新器**本来就有的** docker 凭据通道
  （与 ``docker pull`` 同一条），不新增密钥、不引入签名/attestation。

代价写在这里，免得下一个人重新踩：目录不再"自带"目标摘要，所以更新器每次刷新
目录要打 1~2 次 registry（按 ``CATALOG_REFRESH_INTERVAL_SECONDS`` 节流，见
``updater.py``）；解析失败必须 fail closed（该目标 ``compatible=false`` +
``digest_unresolved``），**绝不**猜一个 digest。``provenance: false`` 的前提没变：
digest 的可信度来自「registry 内容寻址 + 拉取后核 ``LING_COMMIT == OCI
revision`` + 平台 / 当前运行摘要复核」，不是来自签名。

本批只落更新器真正用得到的字段：``min_from``（版本跨度）与 ``has_migration``
（是否有迁移）。最低更新器协议版本 / 预计下载大小目前没有任何使用方，**不预留
空字段**——空字段会被当成"已支持"的假象。

文件格式（schema 1）
====================
::

    {
      "schema": 1,
      "releases": [
        {
          "version": "0.0.2",                   # 严格 semver，不带 v 前缀
          "commit": "<40 位小写 hex>",
          "min_from": ["0.0.1"],                # 允许从哪些版本升到它
          "has_migration": true,
          "released_at": "2026-11-04T00:00:00Z" # 严格 UTC，Z 结尾
        }
      ]
    }

硬规则（校验与追加共用同一套，见 :func:`parse_catalog`）：

- 根对象只有 ``schema`` / ``releases`` 两个键，``schema`` 必须是整数 ``1``；
- 每条记录**恰好**五个键，缺一个、多一个都算非法（拼错字段名比缺字段更危险）。
  多出来的 ``digest`` 就是非法字段——旧目录文件必须被**显式拒绝**，不能"读的时候
  顺手忽略"，否则"目录里到底还有没有 digest 在生效"又会变成两种事实；
- ``version`` 是严格 semver；``commit`` 是 40 位小写 hex；``min_from`` 是**非空**、
  无重复、不含自己的 semver 数组；``has_migration`` 是 JSON 布尔；
  ``released_at`` 是 ``YYYY-MM-DDTHH:MM:SSZ``；
- 全文件 ``version`` 不重复；条目数有上限（避免畸形文件放大内存）。

**不按发布时间排序**：``validate`` 只校验内容、不看顺序；``add`` 写出时按 version
语义排序只是为了让 diff 稳定。使用方（更新器、API）必须自己按 semver 比较，
不能依赖文件里的先后——排错序的目录文件仍然是一条合法目录。

权限：目录文件不涉密，``add`` 写成 ``0644``；``validate`` 只拒绝**其他人可写**
（``0o002``）的文件。目录文件被改坏时更新器必须 fail closed（``available_targets=[]``
+ 明确原因），绝不"猜"一个目标。

更新内容（``deploy/update-notes.json``）
======================================
「这一版改了什么」**不塞进** ``releases.json``：旧更新器（0.0.3）对目录字段是严格
白名单（见 ``RELEASE_KEYS``），往目录里加任何新键都会让还在跑旧更新器的机器判定
**整份目录不可信**——那会把它们的更新通道一起打哑。所以更新内容是**并列的另一个
文件**，旧更新器完全不读它::

    {
      "schema": 1,
      "notes": {
        "0.0.4": {
          "summary": "日程子系统与发送链路的可靠性修复",   # 必填，≤120 字
          "items": ["她的一天：…", "…"],                    # ≤8 条，每条 ≤120 字
          "url": "https://github.com/qsbb/ling/blob/main/CHANGELOG.md"  # 可选
        }
      }
    }

硬规则（与目录同一套风格：多一个键就拒绝，不"读的时候顺手忽略"）：

- 根对象只有 ``schema`` / ``notes`` 两个键，``schema`` 必须是整数 ``1``；
- ``notes`` 的**键**必须是严格 semver（与目录条目同一套 ``SEMVER_RE``）；条目数有上限；
- 每条 note 只允许 ``summary`` / ``items`` / ``url``，``summary`` 必填；
- ``summary`` ≤ 120 字、``items`` ≤ 8 条且每条 ≤ 120 字、``url`` 只允许 http/https
  且 ≤ 512 字符；文本里不允许控制字符（换行会把网页卡片排版弄坏）；
- 长度单位是**字符**（``len(str)``），不是字节：中文摘要按字数算。

**校验失败／文件缺失只有一个后果：没有更新内容**。它绝不影响"能不能更新"的判定——
更新内容不是信任锚，也不参与目标选择；所以 :func:`load_notes` 在文件缺失时返回
**空结构**而不是抛错，调用方也不需要为它 fail closed。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = 1
CATALOG_NAME = "releases.json"
# 目录文件随代码提交、人工评审：上限只是防止畸形文件把更新器内存放大。
MAX_CATALOG_BYTES = 256 * 1024
MAX_RELEASES = 512
MAX_MIN_FROM = 64
WRITE_MODE = 0o644

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# 严格 semver：数值段不允许前导 0；预发布/构建段按 semver 的字符集。
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
RELEASED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# 字段顺序只影响写出的文件长相（便于人工评审），不参与任何语义。
# 没有 ``digest``：目标摘要在更新器刷新目录时从 registry 解析（见文件头）。
RELEASE_KEYS = ("version", "commit", "min_from", "has_migration", "released_at")
ROOT_KEYS = ("schema", "releases")

# 更新内容：**另一个文件**，与目录并列（见文件头「更新内容」一节）。
# 它不参与"能不能更新"的判定，所以这里的上限只需要防畸形文件把网页卡片弄坏。
NOTES_NAME = "update-notes.json"
MAX_NOTES_BYTES = 64 * 1024
MAX_NOTES = 256
MAX_NOTE_ITEMS = 8
MAX_NOTE_SUMMARY_CHARS = 120
MAX_NOTE_ITEM_CHARS = 120
MAX_NOTE_URL_CHARS = 512
# 可选字段的**取值集合**：多一个键就拒绝（拼错字段名比缺字段更危险）。
NOTE_KEYS = ("summary", "items", "url")
NOTES_ROOT_KEYS = ("schema", "notes")
# 只允许 http/https：更新内容里的链接会被网页渲染成可点的「完整更新日志」，
# 不接受 `javascript:` / `data:` 这类协议。
NOTE_URL_RE = re.compile(r"^https?://\S+$")
# 控制字符（含换行/制表）会把卡片排版弄坏，也不该出现在发布文案里。
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

DEFAULT_CATALOG = Path(__file__).resolve().parents[1] / CATALOG_NAME
DEFAULT_NOTES = Path(__file__).resolve().parents[1] / NOTES_NAME


class CatalogError(ValueError):
    """目录文件不可用；``code`` 是机器可读的固定码（文案不参与判断）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _fail(code: str, message: str) -> CatalogError:
    return CatalogError(code, message)


# ---------------------------------------------------------------- semver


def version_key(version: str) -> tuple[int, int, int, tuple[Any, ...]]:
    """semver 排序键：``0.0.10`` > ``0.0.9``；正式版 > 同一核心的预发布版。

    不接受无法解析的版本：目录里的 ``version`` 必须严格 semver，调用方不该
    用"猜"的方式比较字符串。
    """
    if not isinstance(version, str):
        raise _fail("version_invalid", "version 必须是字符串")
    match = SEMVER_RE.fullmatch(version)
    if match is None:
        raise _fail("version_invalid", f"version 不是严格 semver：{version!r}")
    if match["pre"] is None:
        pre: tuple[Any, ...] = (1,)
    else:
        # 数值标识符按数值比较且小于字母标识符（semver §11）。
        pre = (
            0,
            tuple(
                (0, int(part), "") if part.isdigit() else (1, 0, part)
                for part in str(match["pre"]).split(".")
            ),
        )
    return (int(match["major"]), int(match["minor"]), int(match["patch"]), pre)


def sort_releases(releases: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 version 语义排序（返回新列表，不改入参、不改文件）。"""
    items = list(releases)
    return sorted(items, key=lambda item: version_key(item["version"]))


# ---------------------------------------------------------------- 校验


def _validate_release(raw: object, index: int) -> dict[str, Any]:
    where = f"releases[{index}]"
    if not isinstance(raw, dict):
        raise _fail("release_not_object", f"{where} 必须是 JSON 对象")
    keys = set(raw)
    missing = [key for key in RELEASE_KEYS if key not in keys]
    if missing:
        raise _fail("release_field_missing", f"{where} 缺字段：{missing}")
    extra = sorted(keys - set(RELEASE_KEYS))
    if extra:
        raise _fail("release_field_unknown", f"{where} 有未知字段：{extra}")

    version = raw["version"]
    if not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None:
        raise _fail("version_invalid", f"{where}.version 不是严格 semver：{version!r}")

    commit = raw["commit"]
    if not isinstance(commit, str) or COMMIT_RE.fullmatch(commit) is None:
        raise _fail("commit_invalid", f"{where}.commit 必须是 40 位小写 hex")

    min_from = raw["min_from"]
    if not isinstance(min_from, list):
        raise _fail("min_from_invalid", f"{where}.min_from 必须是字符串数组")
    if not min_from:
        raise _fail("min_from_empty", f"{where}.min_from 不能为空（必须说明从哪些版本可升到它）")
    if len(min_from) > MAX_MIN_FROM:
        raise _fail("min_from_invalid", f"{where}.min_from 条目过多（上限 {MAX_MIN_FROM}）")
    seen: set[str] = set()
    normalized_min_from: list[str] = []
    for item in min_from:
        if not isinstance(item, str) or SEMVER_RE.fullmatch(item) is None:
            raise _fail("min_from_invalid", f"{where}.min_from 里有非 semver 项：{item!r}")
        if item == version:
            raise _fail("min_from_self", f"{where}.min_from 不能包含自己（{version}）")
        if item in seen:
            raise _fail("min_from_duplicate", f"{where}.min_from 有重复项：{item}")
        seen.add(item)
        normalized_min_from.append(item)

    has_migration = raw["has_migration"]
    if not isinstance(has_migration, bool):
        raise _fail("has_migration_invalid", f"{where}.has_migration 必须是 JSON 布尔")

    released_at = raw["released_at"]
    if not isinstance(released_at, str):
        raise _fail("released_at_invalid", f"{where}.released_at 必须是字符串")
    try:
        datetime.strptime(released_at, RELEASED_AT_FORMAT)
    except ValueError as exc:
        raise _fail(
            "released_at_invalid",
            f"{where}.released_at 必须是 UTC 时间 {RELEASED_AT_FORMAT}Z 形式：{released_at!r}",
        ) from exc

    return {
        "version": version,
        "commit": commit,
        "min_from": normalized_min_from,
        "has_migration": has_migration,
        "released_at": released_at,
    }


def parse_catalog(raw: bytes) -> dict[str, Any]:
    """解析并严格校验目录文件的字节；返回规范化结构（只含已知字段）。

    抛 :class:`CatalogError`；不做任何 I/O、不改任何文件、不看条目顺序。
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise _fail("not_bytes", "目录内容必须是字节")
    if len(raw) > MAX_CATALOG_BYTES:
        raise _fail("too_large", f"目录文件超过 {MAX_CATALOG_BYTES} 字节")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail("not_utf8", "目录文件必须是 UTF-8") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise _fail("not_json", "目录文件不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise _fail("not_object", "目录根必须是 JSON 对象")
    missing = [key for key in ROOT_KEYS if key not in payload]
    if missing:
        raise _fail("root_field_missing", f"目录根缺字段：{missing}")
    extra = sorted(set(payload) - set(ROOT_KEYS))
    if extra:
        raise _fail("root_field_unknown", f"目录根有未知字段：{extra}")
    schema = payload["schema"]
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise _fail("schema_invalid", "schema 必须是整数 1")
    if schema != SCHEMA:
        raise _fail("schema_unsupported", f"不认识的目录 schema：{schema}")

    releases_raw = payload["releases"]
    if not isinstance(releases_raw, list):
        raise _fail("releases_not_list", "releases 必须是数组")
    if len(releases_raw) > MAX_RELEASES:
        raise _fail("too_many_releases", f"releases 条目过多（上限 {MAX_RELEASES}）")

    releases: list[dict[str, Any]] = []
    versions: set[str] = set()
    for index, item in enumerate(releases_raw):
        entry = _validate_release(item, index)
        if entry["version"] in versions:
            raise _fail("version_duplicate", f"version 重复：{entry['version']}")
        versions.add(entry["version"])
        releases.append(entry)
    return {"schema": SCHEMA, "releases": releases}


# ---------------------------------------------------------------- 文件 I/O


def load_catalog(path: Path) -> dict[str, Any]:
    """读取并校验目录文件：普通文件、有大小上限、不接受其他人可写。"""
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise _fail("not_found", f"发布目录文件不存在：{path}") from exc
    except OSError as exc:
        raise _fail("unreadable", f"发布目录文件不可读：{path}（{type(exc).__name__}）") from exc
    if not stat.S_ISREG(info.st_mode):
        raise _fail("not_a_file", f"发布目录必须是普通文件（不接受符号链接/目录）：{path}")
    if info.st_size > MAX_CATALOG_BYTES:
        raise _fail("too_large", f"发布目录文件超过 {MAX_CATALOG_BYTES} 字节：{path}")
    if stat.S_IMODE(info.st_mode) & 0o002:
        raise _fail("unsafe_permissions", f"发布目录不能是其他人可写（应 0644/0600）：{path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _fail("unreadable", f"发布目录文件不可读：{path}（{type(exc).__name__}）") from exc
    return parse_catalog(raw)


# ---------------------------------------------------------------- 更新内容


def _note_text(raw: object, *, where: str, limit: int) -> str:
    """note 里的一段文案：非空字符串、不超字数、不含控制字符。"""
    if not isinstance(raw, str):
        raise _fail("note_text_invalid", f"{where} 必须是字符串")
    if not raw.strip():
        raise _fail("note_text_invalid", f"{where} 不能为空")
    if _CONTROL_CHARS_RE.search(raw):
        raise _fail("note_text_invalid", f"{where} 含控制字符（换行/制表等）")
    # 「字数」= 字符数，不按字节：中文摘要按字数算才符合设计文档的 120 字上限。
    if len(raw) > limit:
        raise _fail("note_text_too_long", f"{where} 超过 {limit} 字（{len(raw)} 字）")
    return raw


def _validate_note(raw: object, version: str) -> dict[str, Any]:
    where = f"notes[{version!r}]"
    if not isinstance(raw, dict):
        raise _fail("note_not_object", f"{where} 必须是 JSON 对象")
    keys = set(raw)
    if "summary" not in keys:
        raise _fail("note_field_missing", f"{where} 缺字段：summary")
    extra = sorted(keys - set(NOTE_KEYS))
    if extra:
        raise _fail("note_field_unknown", f"{where} 有未知字段：{extra}")
    summary = _note_text(
        raw["summary"], where=f"{where}.summary", limit=MAX_NOTE_SUMMARY_CHARS
    )
    items_raw = raw.get("items", [])
    if not isinstance(items_raw, list):
        raise _fail("note_items_invalid", f"{where}.items 必须是字符串数组")
    if len(items_raw) > MAX_NOTE_ITEMS:
        raise _fail(
            "note_items_invalid",
            f"{where}.items 条目过多（上限 {MAX_NOTE_ITEMS}）",
        )
    items = [
        _note_text(item, where=f"{where}.items[{index}]", limit=MAX_NOTE_ITEM_CHARS)
        for index, item in enumerate(items_raw)
    ]
    url = raw.get("url")
    if url is not None:
        if not isinstance(url, str) or len(url) > MAX_NOTE_URL_CHARS:
            raise _fail(
                "note_url_invalid",
                f"{where}.url 必须是 ≤{MAX_NOTE_URL_CHARS} 字符的字符串",
            )
        if NOTE_URL_RE.fullmatch(url) is None:
            raise _fail("note_url_invalid", f"{where}.url 只接受 http/https 链接")
    return {"summary": summary, "items": items, "url": url}


def parse_notes(raw: bytes) -> dict[str, Any]:
    """解析并严格校验 ``update-notes.json`` 的字节；返回规范化结构。

    抛 :class:`CatalogError`（与目录共用同一个异常类型：调用方只需要一套错误码
    处理）。不做任何 I/O，也不看条目顺序。

    ``notes`` 里的版本**不要求**出现在 ``releases.json`` 里：两个文件各自提交、
    各自评审，把更新内容绑在目录条目上只会让发布顺序变成新的坑。更新器只挑它
    真正要展示的那一两条（当前可用目标、最高新版本）。
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise _fail("not_bytes", "更新内容必须是字节")
    if len(raw) > MAX_NOTES_BYTES:
        raise _fail("too_large", f"更新内容文件超过 {MAX_NOTES_BYTES} 字节")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _fail("not_utf8", "更新内容文件必须是 UTF-8") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise _fail("not_json", "更新内容文件不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise _fail("not_object", "更新内容根必须是 JSON 对象")
    missing = [key for key in NOTES_ROOT_KEYS if key not in payload]
    if missing:
        raise _fail("root_field_missing", f"更新内容根缺字段：{missing}")
    extra = sorted(set(payload) - set(NOTES_ROOT_KEYS))
    if extra:
        raise _fail("root_field_unknown", f"更新内容根有未知字段：{extra}")
    schema = payload["schema"]
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise _fail("schema_invalid", "schema 必须是整数 1")
    if schema != SCHEMA:
        raise _fail("schema_unsupported", f"不认识的更新内容 schema：{schema}")

    notes_raw = payload["notes"]
    if not isinstance(notes_raw, dict):
        raise _fail("notes_not_object", "notes 必须是「版本 -> 条目」的对象")
    if len(notes_raw) > MAX_NOTES:
        raise _fail("notes_too_many", f"notes 条目过多（上限 {MAX_NOTES}）")
    notes: dict[str, Any] = {}
    for version, item in notes_raw.items():
        if not isinstance(version, str) or SEMVER_RE.fullmatch(version) is None:
            raise _fail("notes_version_invalid", f"notes 的键不是严格 semver：{version!r}")
        notes[version] = _validate_note(item, version)
    return {"schema": SCHEMA, "notes": notes}


def load_notes(path: Path) -> dict[str, Any]:
    """读取并校验更新内容文件；**文件缺失返回空结构**（不是异常）。

    为什么缺失不抛错：更新内容只是"有没有说明"，缺了只该显示"这一版没有提供
    更新说明"，绝不该让调用方（更新器）在刷新目录时崩掉或把目录判成不可用。
    其它坏情况（不是普通文件、太大、读不了、结构不对）仍然抛
    :class:`CatalogError`：那些是"文件在、但内容不可信"，调用方要记 ``invalid``。
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"schema": SCHEMA, "notes": {}}
    except OSError as exc:
        raise _fail("unreadable", f"更新内容文件不可读：{path}（{type(exc).__name__}）") from exc
    if not stat.S_ISREG(info.st_mode):
        raise _fail("not_a_file", f"更新内容必须是普通文件（不接受符号链接/目录）：{path}")
    if info.st_size > MAX_NOTES_BYTES:
        raise _fail("too_large", f"更新内容文件超过 {MAX_NOTES_BYTES} 字节：{path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _fail("unreadable", f"更新内容文件不可读：{path}（{type(exc).__name__}）") from exc
    return parse_notes(raw)


def render_catalog(catalog: dict[str, Any]) -> str:
    """稳定序列化：固定字段顺序 + 缩进 + 结尾换行（便于人工评审 diff）。"""
    releases = [{key: entry[key] for key in RELEASE_KEYS} for entry in catalog.get("releases", [])]
    payload = {"schema": SCHEMA, "releases": releases}
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_catalog(path: Path, catalog: dict[str, Any]) -> None:
    """原子写回目录文件（同目录临时文件 + ``os.replace``），权限固定 0644。"""
    directory = path.parent
    if not directory.is_dir():
        raise _fail("not_found", f"发布目录所在目录不存在：{directory}")
    data = render_catalog(catalog).encode("utf-8")
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(directory))
    except OSError as exc:
        raise _fail("write_failed", f"无法创建临时文件（{type(exc).__name__}）") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, WRITE_MODE)
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise _fail("write_failed", f"写入发布目录失败（{type(exc).__name__}）") from exc


def utc_now() -> str:
    return datetime.now(UTC).strftime(RELEASED_AT_FORMAT)


# ---------------------------------------------------------------- 追加


def add_release(
    path: Path,
    *,
    version: str,
    commit: str,
    min_from: Sequence[str],
    has_migration: bool,
    released_at: str | None = None,
) -> tuple[dict[str, Any], str]:
    """往目录里追加一条；返回 ``(catalog, state)``，``state`` 是 ``added``/``unchanged``。

    幂等：同 ``version`` 已存在且 ``commit``/``min_from``/``has_migration`` 全同 →
    **不写文件**、返回 ``unchanged``（``released_at`` 不参与比较：那是"第一次记录
    的时间"，重跑一次不该因为时间戳变了就报冲突）。同 ``version`` 但字段不同 →
    ``version_conflict``：已发布的条目**不允许**被脚本静默改写，必须人工评审后改
    目录文件——目录是信任锚，"同版本换了源提交/跨度"是要上评审的事，不是一次
    ``add`` 能定的。

    为什么不再有 ``--digest`` / ``version_digest_conflict`` / ``digest_duplicate``：
    目录里没有 digest 了（见文件头）。重新打包同一版本**本来就该是允许的、无需
    改目录的操作**——正是旧的 ``version_digest_conflict`` 把它变成了人工事故。
    """
    entry = {
        "version": version,
        "commit": commit,
        "min_from": list(min_from),
        "has_migration": has_migration,
        "released_at": released_at or utc_now(),
    }
    # 新条目与整份目录走**同一套**校验：不允许"新增一条绕过校验"。
    parse_catalog(render_catalog({"schema": SCHEMA, "releases": [entry]}).encode("utf-8"))

    if path.exists():
        catalog = load_catalog(path)
    else:
        if not path.parent.is_dir():
            raise _fail("not_found", f"发布目录所在目录不存在：{path.parent}")
        catalog = {"schema": SCHEMA, "releases": []}

    for existing in catalog["releases"]:
        if existing["version"] != version:
            continue
        same = (
            existing["commit"] == commit
            and existing["min_from"] == list(min_from)
            and existing["has_migration"] is has_migration
        )
        if not same:
            raise _fail(
                "version_conflict",
                f"{version} 已存在且 commit/min_from/has_migration 与本条不同；"
                "已发布的条目不要用脚本改写，请人工评审后改目录文件"
                "（同版本重新打包镜像不需要改目录：digest 由更新器解析）",
            )
        return catalog, "unchanged"

    merged = {"schema": SCHEMA, "releases": [*catalog["releases"], entry]}
    merged["releases"] = sort_releases(merged["releases"])
    merged = parse_catalog(render_catalog(merged).encode("utf-8"))
    write_catalog(path, merged)
    return merged, "added"


# ---------------------------------------------------------------- CLI


def _describe(entry: dict[str, Any]) -> str:
    migration = "yes" if entry["has_migration"] else "no"
    return (
        f"  {entry['version']}  commit={entry['commit']}  "
        f"migration={migration}  min_from={','.join(entry['min_from'])}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catalog.py",
        description="可信发布目录（deploy/releases.json）的校验与追加工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="校验目录文件（CI 门禁）")
    p_validate.add_argument(
        "--catalog", default=str(DEFAULT_CATALOG), help=f"目录文件路径（默认 {CATALOG_NAME}）"
    )

    p_add = sub.add_parser("add", help="把一条发布记录加进目录（幂等）")
    p_add.add_argument(
        "--catalog", default=str(DEFAULT_CATALOG), help=f"目录文件路径（默认 {CATALOG_NAME}）"
    )
    p_add.add_argument("--version", required=True, help="严格 semver，例如 0.0.2")
    p_add.add_argument("--commit", required=True, help="40 位小写 hex 源提交")
    p_add.add_argument(
        "--min-from",
        dest="min_from",
        action="append",
        required=True,
        help="允许从哪个版本升级到它；可重复或用逗号分隔（至少一个）",
    )
    migration = p_add.add_mutually_exclusive_group(required=True)
    migration.add_argument("--has-migration", action="store_true", help="目标版本含数据库迁移")
    migration.add_argument("--no-migration", action="store_true", help="目标版本不含数据库迁移")
    # 帮助文本里**不能**出现裸 `%`：argparse 会对 help 做 `%` 格式化
    # （`HelpFormatter._expand_help` 里的 `help % params`）。把 strftime 用的
    # `RELEASED_AT_FORMAT`（`%Y-%m-%dT%H:%M:%SZ`）插进来，`%Y` 就成了格式符：
    # Python 3.12/3.13 下 `add --help` 抛
    # `ValueError: unsupported format character 'Y'`；Python 3.14 起 argparse 把
    # 这个校验提前到**构造 parser 时**，于是连 `catalog.py validate` 都直接炸
    # （2026-11 CI 的 updater-image job 实跑抓到的那次）。
    # 也不要用 `%%` 转义——那只会让 `--help` 里显示成 `%%`。这里直接写人类可读的
    # 字面格式，取值与 `RELEASED_AT_FORMAT` 的有效输出一致（`tests` 里有扫描用例
    # 保证 parser 里所有展示字符串都不含 `%`）。
    p_add.add_argument(
        "--released-at",
        default="",
        help="UTC 发布时间 YYYY-MM-DDTHH:MM:SSZ（默认现在）",
    )
    return parser


def _min_from_values(raw: Sequence[str]) -> list[str]:
    values: list[str] = []
    for item in raw:
        for part in item.split(","):
            text = part.strip()
            if text:
                values.append(text)
    return values


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    path = Path(args.catalog).expanduser()
    notes: dict[str, Any] | None = None
    try:
        if args.command == "validate":
            catalog = load_catalog(path)
            # 同目录下的更新内容文件：**在就校验，不在就跳过**（它是可选的加法
            # 文件）。同一条 CI 命令顺带守住"发布时把 update-notes.json 写坏"——
            # 那份文件一旦结构不对，更新器只会记 `release_notes.state=invalid`，
            # 页面从此看不到更新说明，而且没有任何别的地方会报错。
            notes_path = path.parent / NOTES_NAME
            if notes_path.exists():
                notes = load_notes(notes_path)
        else:
            catalog, state = add_release(
                path,
                version=args.version,
                commit=args.commit,
                min_from=_min_from_values(args.min_from),
                has_migration=bool(args.has_migration),
                released_at=args.released_at.strip() or None,
            )
    except CatalogError as error:
        print(f"{args.command.upper()} FAILED {error.code}: {error.message}", file=sys.stderr)
        return 2
    if args.command == "add":
        print(f"{state.upper()} {args.version}")
    print(f"OK schema={SCHEMA} releases={len(catalog['releases'])}")
    for entry in sort_releases(catalog["releases"]):
        print(_describe(entry))
    if notes is not None:
        print(f"OK {NOTES_NAME} notes={len(notes['notes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
