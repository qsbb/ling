"""入站拒绝日志开关：关掉的只有日志，不是拒绝。

生产现象：机器人被拉进人多的群之后，白名单拒绝每 2 小时刷 180 条 WARNING，
把 500 条内存缓冲里真正的故障日志挤了出去——既看不到故障，也看不出有人
在扫。所以这个开关必须同时满足三件事：

1. 关掉之后**拒绝照旧**（安全边界不能跟着一起关）；
2. 关掉之后**计数照旧**（"最近有没有人在扫"仍然答得上来）；
3. 它是设置页里**真的**开关：`GET /v1/settings/config` 读得到、
   `PUT /v1/settings/draft` 收得下、发布后在本进程立即生效。
"""

from __future__ import annotations

import dataclasses

import pytest

from ling.contracts.v1 import InteractionEnvelope
from ling.kernel import inbound_intake
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

STRANGER = "999999999"


class _StrangerConnector:
    """最小 connector：恒定产出一个名单外账号的信封。"""

    connector_id = "onebot"
    instance_id = "connector:onebot:test"

    def normalize_inbound(self, payload: dict) -> InteractionEnvelope:  # noqa: ARG002
        return InteractionEnvelope(
            connector_instance=self.instance_id,
            external_event_id="evt_stranger",
            window_ref=f"window:private:{STRANGER}",
            account_ref=STRANGER,
        )


def _whitelist_only(env: TestEnv) -> None:
    """把 app 的 settings 换成"只允许另一个人"的白名单模式。"""
    env.app.state.settings = dataclasses.replace(
        env.app.state.settings,
        access_mode="whitelist",
        access_list=frozenset({"100200300"}),
    )


async def _publish(env: TestEnv, payload: dict) -> dict:
    draft = await env.client.put("/v1/settings/draft", json=payload)
    assert draft.status_code == 200, draft.text
    published = await env.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert published.status_code == 200, published.text
    return (await env.client.get("/v1/settings/config")).json()


async def _deliver_stranger(env: TestEnv) -> dict:
    return await inbound_intake.process_inbound_payload(
        env.app, _StrangerConnector(), {"post_type": "message"}
    )


def _spy_security_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(
        "ling.audit.security_event", lambda action, **kw: seen.append(action)  # noqa: ANN003
    )
    return seen


# ---------------------------------------------------------------- 设置面往返


async def test_switch_defaults_on_and_round_trips(logged_in: TestEnv) -> None:
    """默认 True（保持改动前的行为），PUT 之后读得回 False。"""
    body = (await logged_in.client.get("/v1/settings/config")).json()
    assert body["log_access_denials"] is True

    body = await _publish(logged_in, {"log_access_denials": False})
    assert body["log_access_denials"] is False


# ---------------------------------------------------------------- 行为


async def test_off_still_rejects_and_still_counts(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**这条是开关的全部意义**：关日志不能连拒绝或计数一起关掉。"""
    _whitelist_only(logged_in)
    events = _spy_security_events(monkeypatch)
    await _publish(logged_in, {"log_access_denials": False})

    before = inbound_intake.denied_total()
    result = await _deliver_stranger(logged_in)

    assert result["accepted"] is False, result
    assert "白名单" in result["reason"], result
    assert result["sender"] == STRANGER
    assert events == [], f"开关关掉后仍写了安全事件：{events}"
    assert inbound_intake.denied_total() == before + 1, "关掉日志后计数也停了"


async def test_on_writes_the_security_event(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    _whitelist_only(logged_in)
    events = _spy_security_events(monkeypatch)
    await _publish(logged_in, {"log_access_denials": True})

    result = await _deliver_stranger(logged_in)

    assert result["accepted"] is False
    assert events == ["inbound_denied"], f"开关开着却没有安全事件：{events}"


async def test_publish_takes_effect_without_waiting_for_ttl(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布/回滚在本进程**立即**生效。

    受理路径把开关缓存在 `app.state`（TTL 30s，见 inbound_intake），目的是
    让拒绝路径不必每条消息都查库。控制面发布时必须顺手清掉缓存，否则设置页
    的"发布后立即生效"就是假的。
    """
    _whitelist_only(logged_in)
    events = _spy_security_events(monkeypatch)

    await _publish(logged_in, {"log_access_denials": False})
    await _deliver_stranger(logged_in)
    assert events == []

    await _publish(logged_in, {"log_access_denials": True})
    await _deliver_stranger(logged_in)
    assert events == ["inbound_denied"]


async def test_denied_total_is_exposed_in_logs_endpoint(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """关掉逐条日志之后，`/v1/logs` 里的计数是界面上唯一的线索。"""
    _whitelist_only(logged_in)
    _spy_security_events(monkeypatch)
    await _publish(logged_in, {"log_access_denials": False})

    await _deliver_stranger(logged_in)

    body = (await logged_in.client.get("/v1/logs")).json()
    assert body["denied_total"] >= 1
