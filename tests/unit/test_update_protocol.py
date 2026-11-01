"""共享更新协议的纯逻辑测试（无网络、无 Docker、不写仓库外路径）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ling.updates import protocol

REPO_SRC = Path(__file__).resolve().parents[2] / "src"


# ---------------------------------------------------------------- 版本/标签


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v1.2.3", (1, 2, 3)),
        ("v0.0.1", (0, 0, 1)),
        ("v10.20.30", (10, 20, 30)),
    ],
)
def test_strict_stable_tags(tag: str, expected: tuple[int, int, int]) -> None:
    assert protocol.parse_stable_tag(tag) == expected
    assert protocol.tag_version(tag) == ".".join(str(part) for part in expected)


@pytest.mark.parametrize(
    "tag",
    ["1.2.3", "v1.2", "v1.2.3-rc.1", "v1.2.3+build", "main", "v01.2.3", "", None, 123],
)
def test_non_stable_tags_rejected(tag: object) -> None:
    assert protocol.parse_stable_tag(tag) is None
    if isinstance(tag, str):
        assert protocol.tag_version(tag) is None


def test_release_is_newer_compares_core_versions() -> None:
    assert protocol.release_is_newer("1.0.0", "v1.2.0") is True
    assert protocol.release_is_newer("v1.2.0", "v1.2.0") is False
    assert protocol.release_is_newer("2.0.0", "v1.9.9") is False
    assert protocol.release_is_newer("1.0.0-rc.1", "v1.0.0") is False
    assert protocol.release_is_newer("not-a-version", "v1.0.0") is None


def test_commit_validation_requires_full_sha() -> None:
    assert protocol.is_full_commit("a" * 40)
    assert not protocol.is_full_commit("abc1234")
    assert not protocol.is_full_commit("A" * 40)
    assert not protocol.is_full_commit(None)


def test_request_ids_are_prefixed_and_hex() -> None:
    request_id = protocol.new_request_id()
    assert protocol.is_request_id(request_id)
    assert request_id.startswith("upd_")
    assert len(request_id) == 4 + 32
    assert not protocol.is_request_id("upd_xyz")


# ---------------------------------------------------------------- 任务模型


def _job(**overrides: object) -> protocol.UpdateJob:
    payload: dict[str, object] = {
        "request_id": protocol.new_request_id(),
        "state": "queued",
        "stage": "queued",
        "target_tag": "v1.2.3",
        "target_commit": "b" * 40,
        "created_at": protocol.iso_now(),
        "updated_at": protocol.iso_now(),
    }
    payload.update(overrides)
    return protocol.UpdateJob(**payload)  # type: ignore[arg-type]


def test_job_roundtrip_and_fixed_messages() -> None:
    job = _job(state="failed", stage="build", error_code="build_failed")
    payload = job.to_dict()
    assert payload["message"] == protocol.ERROR_MESSAGES["build_failed"]
    parsed = protocol.UpdateJob.from_dict(payload)
    assert parsed is not None
    assert parsed.request_id == job.request_id
    assert parsed.state == "failed"


def test_job_message_ignores_host_written_text() -> None:
    """宿主可以在文件里写任意 message；API 只使用固定码表重新生成。"""
    payload = _job(state="failed", stage="deploy", error_code="deploy_failed").to_dict()
    payload["message"] = "rm -rf /srv/ling && echo secret"
    parsed = protocol.UpdateJob.from_dict(payload)
    assert parsed is not None
    assert parsed.to_dict()["message"] == protocol.ERROR_MESSAGES["deploy_failed"]
    assert "rm -rf" not in json.dumps(parsed.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "exploded"},
        {"stage": "rm -rf"},
        {"target_tag": "nightly"},
        {"target_commit": "abc"},
        {"error_code": "raw-log-line"},
        {"request_id": "upd_short"},
        {"created_at": "not-a-time"},
    ],
)
def test_invalid_jobs_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(protocol.ProtocolError):
        _job(**overrides)
    payload = {**_job().to_dict(), **overrides}
    assert protocol.UpdateJob.from_dict(payload) is None


# -------------------------------------------------- 备份告警（可选字段，task-76）


def test_job_backup_warnings_default_to_empty_when_absent() -> None:
    """旧执行器不写该字段：API 必须当成空数组，不得报错。"""
    payload = _job(state="succeeded", stage="complete").to_dict()
    payload.pop("backup_warnings", None)
    parsed = protocol.UpdateJob.from_dict(payload)
    assert parsed is not None
    assert parsed.backup_warnings == ()
    assert parsed.to_dict()["backup_warnings"] == []


def test_job_backup_warnings_are_round_tripped() -> None:
    notes = (
        "manifest: media_state=partial（media.tar.zst 归档时有文件正在写入，可能漏掉刚上传的媒体）",
        "manifest 缺少 dump_sha256（旧格式）：未能校验 dump 字节",
    )
    job = _job(state="succeeded", stage="complete", backup_warnings=notes)
    payload = job.to_dict()
    assert payload["backup_warnings"] == list(notes)
    parsed = protocol.UpdateJob.from_dict(payload)
    assert parsed is not None
    assert parsed.backup_warnings == notes
    # 状态机语义不变：告警不改变 state/stage/message。
    assert parsed.state == "succeeded"
    assert parsed.to_dict()["message"] == protocol.STAGE_MESSAGES["complete"]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not-a-list",
        {"a": 1},
        123,
        ["ok-note", 5, ""],
        ["x" * (protocol.MAX_BACKUP_WARNING_LEN + 1)],
        ["two\nlines"],
    ],
)
def test_job_backup_warnings_sanitized_on_read(raw: object) -> None:
    """类型错/超长/多行/非字符串条目：只丢那一条，绝不把整个任务判为不可解析。"""
    payload = _job(state="succeeded", stage="complete").to_dict()
    payload["backup_warnings"] = raw
    parsed = protocol.UpdateJob.from_dict(payload)
    assert parsed is not None
    assert parsed.state == "succeeded"
    assert all(isinstance(note, str) and note for note in parsed.backup_warnings)
    assert len(parsed.backup_warnings) <= 1
    assert not any("\n" in note for note in parsed.backup_warnings)


def test_job_backup_warnings_are_capped() -> None:
    raw = [f"note-{index}" for index in range(protocol.MAX_BACKUP_WARNINGS + 4)]
    assert len(protocol.sanitize_backup_warnings(raw)) == protocol.MAX_BACKUP_WARNINGS


def test_job_unknown_fields_are_ignored_for_forward_compat() -> None:
    """新执行器的多余字段不能把旧 api 的视图变成“无法解析”。"""
    payload = _job(state="succeeded", stage="complete").to_dict()
    payload["future_field"] = "whatever"
    payload["backup_warnings"] = ["warn"]
    parsed = protocol.UpdateJob.from_dict(payload)
    assert parsed is not None
    assert parsed.backup_warnings == ("warn",)


def test_with_progress_keeps_backup_warnings_unless_overridden() -> None:
    job = _job(state="running", stage="backup", backup_warnings=("warn",))
    assert job.with_progress(state="running", stage="fetch").backup_warnings == ("warn",)
    assert job.with_progress(state="succeeded", stage="complete").backup_warnings == ("warn",)
    assert (
        job.with_progress(state="succeeded", stage="complete", backup_warnings=[]).backup_warnings
        == ()
    )


def test_job_rejects_illegal_backup_warnings() -> None:
    """写入侧严格：非法条目直接拒绝，绝不让脏数据落到 current.json。"""
    with pytest.raises(protocol.ProtocolError):
        _job(backup_warnings=["two\nlines"])
    with pytest.raises(protocol.ProtocolError):
        _job(backup_warnings=["x" * (protocol.MAX_BACKUP_WARNING_LEN + 1)])
    with pytest.raises(protocol.ProtocolError):
        _job(backup_warnings=[5])


def test_unknown_error_code_has_no_message() -> None:
    assert protocol.message_for("nope") is None
    assert protocol.message_for(None) is None


# ---------------------------------------------------------------- 请求模型


def test_request_roundtrip_and_expiry() -> None:
    request = protocol.build_request(
        target_tag="v1.2.3",
        target_commit="c" * 40,
        current_version="1.0.0",
        current_commit="d" * 40,
    )
    payload = request.to_dict()
    assert payload["schema_version"] == "v1"
    assert payload["expires_at"] > payload["created_at"]
    parsed = protocol.UpdateRequest.from_dict(payload)
    assert parsed == request
    assert parsed is not None and not parsed.expired()


def test_expired_request_detected() -> None:
    request = protocol.build_request(
        target_tag="v1.2.3",
        target_commit="c" * 40,
        current_version="1.0.0",
        current_commit="d" * 40,
        created_at="2020-01-01T00:00:00Z",
    )
    assert request.expired()
    assert request.expired(now=1577836800.0 + protocol.REQUEST_TTL_SECONDS + 1)


@pytest.mark.parametrize("schema", [None, "v2", 1])
def test_request_requires_supported_schema(schema: object) -> None:
    """M1：未知 schema 必须抛错，不能返回 None 被当成“没有请求”。"""
    payload = protocol.build_request(
        target_tag="v1.2.3",
        target_commit="c" * 40,
        current_version="1.0.0",
        current_commit="d" * 40,
    ).to_dict()
    payload["schema_version"] = schema
    with pytest.raises(protocol.ProtocolError) as info:
        protocol.UpdateRequest.from_dict(payload)
    assert str(info.value) == protocol.PENDING_REASON_SCHEMA


def test_request_rejects_unknown_fields() -> None:
    """未来 schema 多出来的字段：不能猜着执行，必须 fail closed。"""
    payload = protocol.build_request(
        target_tag="v1.2.3",
        target_commit="c" * 40,
        current_version="1.0.0",
        current_commit="d" * 40,
    ).to_dict()
    payload["dry_run"] = True
    with pytest.raises(protocol.ProtocolError) as info:
        protocol.UpdateRequest.from_dict(payload)
    assert str(info.value) == protocol.PENDING_REASON_SCHEMA


def test_read_pending_file_distinguishes_absent_from_invalid(tmp_path: Path) -> None:
    """「文件不存在」与「文件存在但不可信」必须是两种状态。"""
    target = tmp_path / protocol.PENDING_FILE
    absent = protocol.read_pending_file(target)
    assert absent.state == protocol.PENDING_STATE_ABSENT
    assert absent.request is None

    target.write_text("{not json", encoding="utf-8")
    broken = protocol.read_pending_file(target)
    assert broken.state == protocol.PENDING_STATE_INVALID
    assert broken.reason == protocol.PENDING_REASON_FORMAT
    assert broken.request is None

    target.write_text(json.dumps({"schema_version": "v9"}), encoding="utf-8")
    future = protocol.read_pending_file(target)
    assert future.state == protocol.PENDING_STATE_INVALID
    assert future.reason == protocol.PENDING_REASON_SCHEMA

    request = protocol.build_request(
        target_tag="v1.2.3",
        target_commit="c" * 40,
        current_version="1.0.0",
        current_commit="d" * 40,
    )
    protocol.atomic_write_json(target, request.to_dict(), mode=0o640)
    ok = protocol.read_pending_file(target)
    assert ok.state == protocol.PENDING_STATE_OK
    assert ok.request == request


def test_read_pending_file_rejects_symlink_and_directory(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    protocol.atomic_write_json(
        real,
        protocol.build_request(
            target_tag="v1.2.3",
            target_commit="c" * 40,
            current_version="1.0.0",
            current_commit="d" * 40,
        ).to_dict(),
    )
    link = tmp_path / protocol.PENDING_FILE
    link.symlink_to(real)
    linked = protocol.read_pending_file(link)
    assert linked.state == protocol.PENDING_STATE_INVALID
    assert linked.reason == protocol.PENDING_REASON_FILE
    link.unlink()
    link.mkdir()
    directory = protocol.read_pending_file(link)
    assert directory.state == protocol.PENDING_STATE_INVALID


# ---------------------------------------------------------------- 心跳状态


def _heartbeat_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = protocol.heartbeat_payload(
        last_seen_at=protocol.iso_now(),
        deployed_commit="a" * 40,
        current_version="1.0.0",
        status=protocol.EXECUTOR_STATUS_OK,
        pending_state=protocol.PENDING_STATE_ABSENT,
    )
    payload.update(overrides)
    return payload


def test_heartbeat_payload_requires_explicit_status() -> None:
    with pytest.raises(protocol.ProtocolError):
        protocol.heartbeat_payload(
            last_seen_at=protocol.iso_now(),
            deployed_commit=None,
            current_version=None,
            status="whatever",
            pending_state=protocol.PENDING_STATE_ABSENT,
        )
    payload = _heartbeat_payload()
    assert payload["status"] == protocol.EXECUTOR_STATUS_OK
    assert payload["pending_state"] == protocol.PENDING_STATE_ABSENT


def test_parse_heartbeat_fails_closed_on_missing_or_unknown_status() -> None:
    """M1：没有 status / 未知 schema 的心跳绝不能算“在线空闲”。"""
    parsed = protocol.parse_heartbeat(_heartbeat_payload())
    assert parsed is not None
    assert parsed.protocol_ok is True

    manual = protocol.parse_heartbeat(_heartbeat_payload(status="manual"))
    assert manual is not None and manual.manual_required is True

    for payload in (
        _heartbeat_payload(status=None),
        _heartbeat_payload(status="weird"),
        _heartbeat_payload(status=None, schema_version="v2"),
    ):
        parsed = protocol.parse_heartbeat(payload)
        assert parsed is not None
        assert parsed.protocol_ok is False
        assert parsed.status == protocol.EXECUTOR_STATUS_PROTOCOL_UNSUPPORTED

    assert protocol.parse_heartbeat(["not", "a", "dict"]) is None


def test_parse_heartbeat_sanitizes_fields() -> None:
    parsed = protocol.parse_heartbeat(
        _heartbeat_payload(
            last_seen_at="yesterday",
            deployed_commit="short",
            current_version=123,
            pending_state="nope",
        )
    )
    assert parsed is not None
    assert parsed.last_seen_at is None
    assert parsed.deployed_commit is None
    assert parsed.current_version is None
    assert parsed.pending_state == protocol.PENDING_STATE_UNKNOWN


# ---------------------------------------------------------------- 文件 I/O


def test_atomic_write_and_read_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "pending.json"
    protocol.atomic_write_json(target, {"hello": "world"}, mode=0o640)
    assert protocol.read_json_file(target) == {"hello": "world"}
    assert (target.stat().st_mode & 0o777) == 0o640


def test_read_rejects_symlink_and_oversize(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    protocol.atomic_write_json(real, {"ok": True})
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert protocol.read_json_file(link) is None
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"x": "a" * (protocol.MAX_JSON_BYTES + 10)}), encoding="utf-8")
    assert protocol.read_json_file(big) is None
    assert protocol.read_json_file(tmp_path / "missing.json") is None


def test_atomic_write_rejects_symlinked_directory(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(protocol.ProtocolError):
        protocol.atomic_write_json(link_dir / "pending.json", {"x": 1})


def test_ensure_directory_rejects_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(protocol.ProtocolError):
        protocol.ensure_directory(link, mode=0o700)
    assert protocol.directory_ready(real)


def test_remove_file_does_not_follow_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert protocol.remove_file(link) is False
    assert real.exists()


def test_file_lock_creates_private_lock_file(tmp_path: Path) -> None:
    lock_path = tmp_path / ".lock"
    with protocol.FileLock(lock_path):
        assert (lock_path.stat().st_mode & 0o777) == 0o600
    lock = protocol.try_lock(lock_path)
    assert lock is not None
    lock.release()


def test_file_lock_is_cross_process(tmp_path: Path) -> None:
    lock_path = tmp_path / ".lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(REPO_SRC)!r})\n"
                "import time\n"
                "from pathlib import Path\n"
                "from ling.updates import protocol\n"
                f"lock = protocol.FileLock(Path({str(lock_path)!r}))\n"
                "assert lock.acquire()\n"
                "print('locked', flush=True)\n"
                "time.sleep(5)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        deadline = time.time() + 5
        observed = None
        while time.time() < deadline and observed is None:
            observed = protocol.try_lock(lock_path)
            if observed is None:
                break
            observed.release()
            time.sleep(0.05)
        assert observed is None, "另一个进程持锁时不应拿到锁"
    finally:
        holder.kill()
        holder.wait(timeout=5)


def test_iso_timestamp_helpers() -> None:
    value = protocol.iso_now()
    assert protocol.parse_iso_timestamp(value) is not None
    assert protocol.parse_iso_timestamp("2026-01-01T00:00:00+00:00") is not None
    assert protocol.parse_iso_timestamp("yesterday") is None
    assert protocol.seconds_since(value, now=time.time()) is not None


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX only")
def test_directory_ready_is_true_for_directory(tmp_path: Path) -> None:
    assert protocol.directory_ready(tmp_path)
    assert not protocol.directory_ready(tmp_path / "nope")
