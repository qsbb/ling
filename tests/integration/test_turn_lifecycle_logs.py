"""轮次生命周期与投递结果：只看日志能不能回答「为什么没送达」。

`turn_started` → `reply_decision_made` → `turn_completed` / `delivery_failed`
是一条完整链路。缺任何一环，排查时就得回去读代码。
"""

from __future__ import annotations

import logging

import pytest

pytestmark = pytest.mark.w0_web


def _events(caplog: pytest.LogCaptureFixture, *names: str) -> list[logging.LogRecord]:
    wanted = set(names)
    return [r for r in caplog.records if getattr(r, "event_name", "") in wanted]


# ---------------------------------------------------------------- Web 路径


async def test_web_turn_emits_start_and_decision(logged_in, caplog) -> None:
    """一轮对话至少要有"开始了"和"为什么这么决定"两条。"""
    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "生命周期"})
    ).json()["session_id"]

    with caplog.at_level(logging.INFO, logger="ling.api.chat"):
        await logged_in.client.post(
            f"/v1/sessions/{session_id}/turns",
            json={"client_turn_id": "life-1", "text": "在吗"},
        )

    started = _events(caplog, "turn_started")
    decided = _events(caplog, "reply_decision_made")
    assert started, [r.getMessage() for r in caplog.records]
    assert decided, "决策事件缺失"

    attrs = started[-1].attributes
    assert attrs["source"] == "web"
    assert attrs["turn_id"]
    assert attrs["config_revision"] >= 0


# ---------------------------------------------------------------- 投递失败


class _FailConnector:
    """总是返回"没送达"的连接器。"""

    connector_id = "fake-fail"
    instance_id = "fake:fail"

    def capabilities(self):  # noqa: ANN201
        from ling.ports.channel import CapabilityDescriptor

        return CapabilityDescriptor(
            capability_id="fake-fail",
            version="1",
            supports_private=True,
            supports_group=True,
            supports_proactive=False,
            supports_media=False,
            max_text_length=500,
            enabled=True,
        )

    async def send(self, message):  # noqa: ANN001, ANN202
        from datetime import UTC, datetime

        from ling.capabilities.delivery import DeliveryState
        from ling.ports.channel import DeliveryReceipt

        return DeliveryReceipt(
            receipt_id="rcpt_fail",
            operation_id=message.operation_id,
            channel=self.connector_id,
            state=DeliveryState.failed,
            platform_message_id=None,
            observed_at=datetime.now(UTC),
        )


async def test_delivery_failure_is_explained_by_logs(web_env, caplog) -> None:
    """投递没成功时，日志要给出状态、第几段、总段数——不记正文。"""
    from ling.kernel.bridge import InboundBridge

    bridge = InboundBridge(
        web_env.app.state._session_factory()  # type: ignore[attr-defined]
        if hasattr(web_env.app.state, "_session_factory")
        else None,
        coordinator=object(),
        connector=_FailConnector(),
        secret_box=object(),
    )

    # 只测投递这一段，所以直接借一个 session
    from ling.infra.db import session_scope

    async with session_scope() as session:
        bridge._session = session  # noqa: SLF001 - 单测里替换协作者
        envelope = type(
            "Env",
            (),
            {"window_ref": "fake:private:10001", "connector_instance": "fake"},
        )()
        with caplog.at_level(logging.WARNING, logger="ling.kernel.bridge"):
            delivered, sent, total = await bridge._deliver(  # noqa: SLF001
                envelope=envelope, text="一二三四五。六七八九十。", subject_id="subj_x"
            )

    assert delivered is False
    assert sent == 0
    assert total >= 1

    events = _events(caplog, "delivery_failed")
    assert events, [r.getMessage() for r in caplog.records]
    attrs = events[-1].attributes
    assert attrs["part_ordinal"] == 1
    assert attrs["parts_total"] >= 1
    assert attrs["delivery_state"] == "failed"
    # 正文绝不出现在投递事件里
    assert "一二三四五" not in str(attrs)
