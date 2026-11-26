"""Convert a self-contained Memory Companion SQLite backup to Ling JSONL."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

_QQ = re.compile(r"[1-9][0-9]{4,19}", re.ASCII)
_PRIVATE_MATCH = """
       instr(session_id, ':') > 1
       AND lower(substr(session_id, instr(session_id, ':'))) IN (
            ':friendmessage:' || :owner_qq,
            ':privatemessage:' || :owner_qq,
            ':friend:' || :owner_qq,
            ':private:' || :owner_qq
       )
       AND scope = 'private' AND group_id = '' AND visibility = 'private_pair'
       AND owner_bot_id = :bot_qq AND subject_kind = 'user' AND subject_id = :owner_qq
       AND ((object_kind = 'bot' AND object_id = :bot_qq)
          OR (object_kind = 'user' AND object_id = :owner_qq))
"""
_GROUP_MATCH = """
       instr(session_id, ':') > 1
       AND lower(substr(session_id, instr(session_id, ':'))) IN (
            ':groupmessage:' || group_id,
            ':group:' || group_id
       )
       AND scope = 'group' AND group_id <> '' AND visibility = 'group_public'
       AND owner_bot_id = :bot_qq AND subject_kind = 'bot' AND subject_id = :bot_qq
       AND object_kind = 'group' AND object_id = group_id
"""
_SELECT = f"""
    SELECT id, memory_type, subject_kind, subject_id, object_kind, object_id, scope,
           session_id, platform, group_id, owner_bot_id, visibility, sayability,
           reality_level, lifecycle, sensitivity, review_status, validity_status,
           content, confidence, created_at, updated_at, occurred_at, valid_from,
           valid_to, message_id, metadata
      FROM memories
     WHERE platform IN ('aiocqhttp', 'onebot', 'qq')
       AND (({_PRIVATE_MATCH})
            OR (:include_group = 1 AND ({_GROUP_MATCH})))
     ORDER BY id
     LIMIT :limit OFFSET :offset
"""


def _references(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        item for item in value if isinstance(item, str) and 0 < len(item) <= 200
    ))


def _record(row: sqlite3.Row) -> dict:
    record = dict(row)
    metadata_text = record.pop("metadata")
    try:
        metadata = json.loads(metadata_text)
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    event_ids = _references(metadata.get("source_event_ids"))
    message_ids = _references(metadata.get("source_message_ids"))
    message_id = record.pop("message_id")
    if message_id and message_id not in message_ids:
        message_ids.insert(0, message_id)
    record.update(source_event_ids=event_ids, source_message_ids=message_ids)
    for key in ("occurred_at", "valid_from", "valid_to"):
        if record[key] == "":
            record[key] = None
    return record


def _timeline_evidence(db: sqlite3.Connection, record: dict):
    """Carry only cited speaker attribution from the snapshot."""
    refs = record["source_event_ids"]
    if not refs:
        return
    peer = record["group_id"] or record["subject_id"]
    for start in range(0, len(refs), 800):
        batch = refs[start:start + 800]
        ids = ", ".join("?" for _ in batch)
        for event in db.execute(f"""
            SELECT id, event_type, scope, session_id, subject_id, object_id, metadata
            FROM timeline
            WHERE id IN ({ids}) AND event_type = 'user_message'
              AND scope = ? AND session_id = ? AND object_id = ?
        """, (*batch, record["scope"], record["session_id"], peer)):
            try:
                metadata = json.loads(event["metadata"])
            except (TypeError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            name = metadata.get("sender_name")
            if (
                metadata.get("owner_bot_id") != record["owner_bot_id"]
                or metadata.get("platform") != record["platform"]
                or not isinstance(name, str) or not 2 <= len(name.strip()) <= 64
                or name.strip() not in record["content"]
            ):
                continue
            data = dict(event)
            data["metadata"] = {
                field: metadata[field] for field in (
                    "sender_name", "owner_bot_id", "platform", "participant_user_id",
                    "source_group_id", "source_state", "retracted_at", "forgotten_at", "deleted_at",
                ) if field in metadata
            }
            yield {"record_type": "timeline", "data": data}


def convert(
    snapshot: Path, output: Path, *, bot_qq: str, owner_qq: str, offset: int = 0, limit: int | None = None,
    include_group: bool = False,
) -> tuple[int, bool]:
    if not _QQ.fullmatch(bot_qq) or not _QQ.fullmatch(owner_qq) or bot_qq == owner_qq:
        raise ValueError("请填写两个不同的 QQ 号，分别对应旧 Bot 和你本人")
    if not 0 <= offset <= 2**63 - 1 or (limit is not None and not 1 <= limit < 2**63 - 1):
        raise ValueError("--offset 需要非负整数，--limit 需要正整数，且应在 SQLite 整数范围内")
    snapshot = snapshot.expanduser().resolve(strict=True)
    if Path(f"{snapshot}-wal").exists() or Path(f"{snapshot}-journal").exists():
        raise ValueError("请使用独立的 SQLite 备份，当前文件还有 WAL 或日志文件")
    output = output.expanduser()

    header = {
        "format": "ling-astrbot-memory-jsonl", "version": 1, "bot_qq": bot_qq, "owner_qq": owner_qq,
    }
    with closing(sqlite3.connect(f"{snapshot.as_uri()}?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        timeline_columns = {column["name"] for column in db.execute("PRAGMA table_info(timeline)")}
        has_timeline = {"id", "event_type", "scope", "session_id", "subject_id", "object_id", "metadata"} <= timeline_columns
        cursor = db.execute(_SELECT, {
            "bot_qq": bot_qq, "owner_qq": owner_qq,
            "offset": offset, "limit": -1 if limit is None else limit + 1,
            "include_group": 1 if include_group else 0,
        })
        with output.open("xb") as handle:
            try:
                def write(payload: dict) -> None:
                    handle.write((json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode())

                write(header)
                count = 0
                for row in cursor:
                    if count == limit:
                        return count, True
                    record = _record(row)
                    write(record)
                    if has_timeline:
                        for evidence in _timeline_evidence(db, record):
                            write(evidence)
                    count += 1
                return count, False
            except BaseException:
                handle.close()
                output.unlink()
                raise


def main() -> None:
    parser = argparse.ArgumentParser(description="将独立的 AstrBot 记忆库备份转换成 Ling 导入文件")
    parser.add_argument("snapshot", type=Path, help="SQLite 备份路径")
    parser.add_argument("output", type=Path, help="要创建的 JSONL 文件")
    parser.add_argument("--bot-qq", required=True, help="旧机器人 QQ 号")
    parser.add_argument("--owner-qq", required=True, help="本人的 QQ 号")
    parser.add_argument("--offset", type=int, default=0, help="同一份备份中从第几条之后开始")
    parser.add_argument("--limit", type=int, help="可选：最多处理多少条，默认导出所有匹配记录")
    parser.add_argument(
        "--include-group", action="store_true",
        help="同时导出 Bot 在群里的共同记忆（默认只导私聊）",
    )
    args = parser.parse_args()
    try:
        count, more = convert(
            args.snapshot, args.output, bot_qq=args.bot_qq, owner_qq=args.owner_qq,
            offset=args.offset, limit=args.limit, include_group=args.include_group,
        )
    except sqlite3.Error:
        parser.exit(1, "转换失败：请核对 SQLite 备份和 Memory Companion 的表结构。\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"转换失败：{exc}\n")
    print(f"已生成 {args.output}，包含 {count} 条匹配记录（{'私聊+群聊' if args.include_group else '仅私聊'}）。")
    if more:
        print(f"还有记录，用同一份备份和 --offset {args.offset + count} 输出下一份文件。")


if __name__ == "__main__":
    main()
