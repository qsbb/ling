"""One observation per inbound message, with the actual handling outcome."""

from __future__ import annotations

import pytest

from ling.contracts.v1 import InputPart, InteractionEnvelope, Modality
from ling.kernel.bridge import BridgeResult
from ling.kernel.inbound_intake import _log_inbound_behavior
from ling.logging_setup import clear_log_buffer, recent_logs, setup_logging


@pytest.fixture(autouse=True)
def _fresh_logs() -> None:
    setup_logging("api")
    clear_log_buffer()


@pytest.mark.parametrize(("platform", "window", "group", "scene"), [
    ("onebot", "arbitrary-id:group:100200400", True, "QQ 群"),
    ("qqbot", "qq:c2c:100200300", False, "QQ 私聊"),
    ("qq", "qq:channel:room", False, "QQ 房间"),
    ("wechat", "wechat:group:room", True, "微信群"),
    (None, "other:group:room", True, "群聊"),
])
def test_image_keeps_scene_and_deferred_action(platform, window, group, scene) -> None:
    envelope = InteractionEnvelope(
        connector_instance="arbitrary-id", external_event_id="evt_1", window_ref=window,
        sender_name="夜落星河", parts=[InputPart(modality=Modality.image)],
    )
    result = BridgeResult(
        accepted=True, decision="deferred_attention", action="先放着，等她刷到",
        reason="群里的普通消息", window_id="win_1", turn_id="turn_1",
    )
    _log_inbound_behavior(envelope, result, is_group=group, platform=platform)
    entry, = recent_logs(kind="behavior")
    assert entry.message == f"{scene}里夜落星河说：「[image]」 · 先放着，等她刷到"
    assert entry.event_name == "inbound_received"
    assert entry.session_id == "win_1"
    assert entry.turn_id == "turn_1"
    assert entry.attributes["attributes"]["reason"] == "群里的普通消息"


@pytest.mark.parametrize(("accepted", "decision", "delivered", "sent", "total", "status"), [
    (True, "reply", True, 2, 2, "已回复"),
    (True, "reply", False, 1, 2, "部分送达（1/2）"),
    (True, "reply", False, 0, 2, "决定回复，未送达"),
    (True, "silence", False, 0, 0, "未接话"),
    (False, None, False, 0, 0, "未受理"),
])
def test_delivery_summary(accepted, decision, delivered, sent, total, status) -> None:
    envelope = InteractionEnvelope(
        connector_instance="onebot-test", external_event_id="evt_2", window_ref="onebot:user:1",
        parts=[InputPart(modality=Modality.text, text="在吗")],
    )
    _log_inbound_behavior(envelope, BridgeResult(
        accepted=accepted, decision=decision, reason="api_key=dummy-private-value",
        delivered=delivered, parts_sent=sent, parts_total=total,
    ), is_group=False)
    entry, = recent_logs(kind="behavior")
    assert entry.message == f"QQ 私聊里有人说：「在吗」 · {status}"
    detail = entry.attributes["attributes"]
    assert detail["delivered"] is delivered
    assert detail["parts_sent"] == sent
    assert "dummy-private-value" not in detail["reason"]


# ---------------------------------------------------------------- 失败原因必须说清


def test_describe_turn_failure_reads_model_error_fields() -> None:
    """``ModelError`` 那条路填的是 code/message（**没有 detail**）。

    修复前调用方只读 ``detail``，于是最常见的那条失败在日志里只剩
    「轮次 x 失败：turn.failed |」——用户截图那行就是这么来的。
    """
    from ling.kernel.turn import describe_turn_failure

    data = {
        "turn_id": "turn_x",
        "state": "failed",
        "error": {
            "code": "quota_exhausted",
            "message": "供应商 main 今天的 token 额度已用完（已用 200431 / 上限 200000）",
            "retriable": False,
        },
    }
    line = describe_turn_failure(data)
    assert line.startswith("code=quota_exhausted ")
    assert "额度已用完" in line
    assert "retriable=False" in line
    assert "detail=" not in line, "没有 detail 时不该凭空造一个"


def test_describe_turn_failure_reads_internal_detail() -> None:
    """未捕获异常那条路：原因只存在 ``detail`` 里，丢了就等于没记。"""
    from ling.kernel.turn import describe_turn_failure

    line = describe_turn_failure(
        {
            "error": {
                "code": "internal",
                "message": "服务端内部错误，请重试",
                "retriable": True,
                "detail": "RuntimeError: boom",
            }
        }
    )
    assert "code=internal" in line
    assert "detail=RuntimeError: boom" in line
    assert "retriable=True" in line


def test_describe_turn_failure_never_pretends_to_know() -> None:
    from ling.kernel.turn import describe_turn_failure

    assert describe_turn_failure({}) == "没有留下原因（error 字段为空）"
    assert describe_turn_failure(None) == "没有留下原因（error 字段为空）"
