"""承诺到期投递的**端到端**验证：从调度方到真实发送，全程不手工塞 payload。

## 为什么这条链路必须单独测

`tests/integration/test_commitments.py` 里原有的投递用例手工传了
``{"proactive_window_ref": "commit-test:private:12345"}``，于是把真正的缺陷
整条绕过去了：

- 生产调度方 ``enqueue_maintenance`` 只投 ``{"bucket": hour_bucket}``；
- handler 只从 payload 读 ``proactive_window_ref`` → 恒为空；
- 空 ref 走 ``mark_delivery(delivery_state="unsupported")``；
- ``unsupported`` 映射成 ``row.state = "undeliverable"``；
- 而 ``due_commitments()`` 只取 ``state == "active"``。

净效果：**任何到期承诺被扫到一次就永久失效**，再也提醒不了。用户把它当
提醒器用时，失灵是静默的——状态栏里只会出现一条"无法投递"，原因写着
"缺少会话定位"，看上去像是没配渠道。

这里刻意从 ``enqueue_maintenance`` 出发，再走 ``drain_outbox``，让
**outbox payload 的形状**成为被测对象：将来若有人再往 payload 里增删
字段，这条测试仍按生产实际投递的内容跑。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from ling.domains.goals.service import GoalsService
from ling.infra.db import session_scope
from ling.infra.models import OutboxEvent, Window
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def _private_window(env: TestEnv, *, ref: str) -> str:
    """建真实私聊窗口，返回 `Window.id`。

    `id` 是本地主键，`title` 才是平台 `window_ref`（连接器按它解析收件人）。
    把它们混为一谈，就是"把 `win_xxx` 当 QQ 号发出去"那类事故。
    """
    window_id = f"win_e2e_{abs(hash(ref)) % 99999}"
    async with session_scope() as session:
        session.add(
            Window(
                id=window_id,
                actor_id=env.settings.actor_id,
                persona_id=env.settings.persona_id,
                subject_id=env.subject_id,
                title=ref,
                channel="onebot",
                visibility="subject_private",
                archived=False,
                last_message_at=datetime.now(UTC),
            )
        )
    return window_id


async def _due_commitment(env: TestEnv, *, window_id: str | None) -> str:
    async with session_scope() as session:
        view = await GoalsService(session).create_commitment(
            subject_id=env.subject_id,
            text="该吃药了",
            due_at=datetime.now(UTC) - timedelta(minutes=1),
            window_id=window_id,
        )
    return view.commitment_id


async def _view(commitment_id: str):
    async with session_scope() as session:
        return await GoalsService(session).current_commitment(commitment_id)


def _connector(sent: list[dict]):
    """OneBot 连接器 + MockTransport，只记录出站 payload。"""
    import httpx
    from connectors.onebot.connector import OneBotV11Connector
    from connectors.onebot.settings import OneBotConfig

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "ok", "retcode": 0, "data": {"message_id": 1}}
        )

    return OneBotV11Connector(
        OneBotConfig(
            api_base="http://onebot.test:5700",
            access_token="t",
            self_id="1",
            instance_id="e2e-test",
            supports_proactive=True,
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _run_production_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> list[dict]:
    """跑一次**生产调度路径**：enqueue_maintenance → drain_outbox。

    不用 `handle_commitment_dispatch({...})` 直接调用：那样 payload 由测试
    自己拼，测不到"调度方到底投了什么"。这里让 outbox 里**真实的 payload**
    流到 handler，与生产完全一致。

    维护任务用小时桶做幂等键，所以先清掉本桶已有的事件——否则
    `enqueue_maintenance` 会认为"已投递"而直接跳过（那会让测试假绿）。
    """
    import ling.capabilities.channels as channels_mod
    from ling.worker import drain_outbox, enqueue_maintenance

    sent: list[dict] = []
    monkeypatch.setattr(
        channels_mod, "proactive_connector", lambda: _connector(sent)
    )

    hour_bucket = datetime.now(UTC).strftime("%Y-%m-%dT%H")
    async with session_scope() as session:
        await session.execute(
            delete(OutboxEvent).where(OutboxEvent.id == f"maint_commitment_{hour_bucket}")
        )

    await enqueue_maintenance()
    await drain_outbox()
    return sent


async def test_scheduled_dispatch_delivers_instead_of_killing_commitment(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺陷回归：走生产调度路径的到期承诺必须**真的投递**，不能变 undeliverable。

    回退修复（handler 只读 payload 的 `proactive_window_ref`）时，这条会以
    "承诺到期却没有任何实际发送" 失败——那正是生产上的行为。
    """
    window_id = await _private_window(logged_in, ref="e2e-test:private:10001")
    commitment_id = await _due_commitment(logged_in, window_id=window_id)

    sent = await _run_production_dispatch(monkeypatch)

    assert sent, "承诺到期却没有任何实际发送（调度→投递链路断了）"
    # 收件人必须是窗口的平台 ref，而不是本地 `Window.id`
    assert sent[0]["user_id"] == "10001", f"发给了错误的目标: {sent[0]}"

    view = await _view(commitment_id)
    assert view.state != "undeliverable", (
        "到期承诺被标 undeliverable：到期即永久失效，再也提醒不了"
    )
    assert view.state == "delivered", f"承诺没有被真正投递: {view.state}"
    assert view.delivery_state == "sent"


async def test_each_commitment_goes_to_its_own_window(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次调度扫到多条承诺时，每条都必须发到**它自己记的那个窗口**。

    这是"会话定位跟着承诺走、不跟调度走"的直接验证：调度 payload 里没有
    任何窗口信息（只有一个小时桶），若 handler 依赖它，多条承诺就都发往
    同一个地方——要么全发不出去，要么把 A 的提醒发给 B。
    """
    win_a = await _private_window(logged_in, ref="e2e-test:private:20001")
    win_b = await _private_window(logged_in, ref="e2e-test:private:20002")
    cmt_a = await _due_commitment(logged_in, window_id=win_a)
    cmt_b = await _due_commitment(logged_in, window_id=win_b)

    sent = await _run_production_dispatch(monkeypatch)

    targets = sorted(str(item.get("user_id")) for item in sent)
    assert targets == ["20001", "20002"], f"承诺没发到各自的窗口: {targets}"
    for cid in (cmt_a, cmt_b):
        view = await _view(cid)
        assert view.state == "delivered", f"{cid} 没被投递: {view.state}"
        assert view.delivery_state == "sent"
