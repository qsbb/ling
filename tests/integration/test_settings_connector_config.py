"""接入器可配置（真 PostgreSQL）：覆盖优先级、脱敏、生效/回滚、门禁。

需求来自 `docs/CONNECTOR-CONFIG-DESIGN.md`。这一组用例守的是**"保存成功"
必须等于"真的生效"**：

1. 空覆盖时 GET 与升级前等价（``source=env/default``，generation=0）；
2. 写入后 ``source=db``、generation+1，DB 里只有密文与预览；
3. 清除 = 删除覆盖回退 env（不是写空串），DB 不复制一份 env 明文；
4. 密钥预览：7 字符最多露 6 个 + 固定遮罩；全量 JSON / 审计搜不到完整值；
5. 校验失败（任意 env 名、非法端口、URL 夹 token）→ 422 且 **stop() 零次**；
6. 切换失败 → ``rolled_back``、旧配置与旧连接恢复、generation 不变；
7. 门禁：非 owner / 跨站 Origin → 403（会话年龄不作门禁）；代际冲突 → 409；
8. 白名单保留前导零。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import (
    DEFAULT_INSTANCE_ID,
    AuthSession,
    ConnectorConfigHead,
    ConnectorConfigRevision,
    ConnectorOverrideValue,
)
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture

pytestmark = pytest.mark.w0_web

ENDPOINT = "/v1/settings/connectors"
ORIGIN = {"Origin": "http://testserver"}

# 一眼能认出来的测试密钥：出现在 JSON / 日志 / 审计里就一定抓得到。
TOKEN_A = "onebot-token-AAAA-SHOULD-NEVER-APPEAR-1a2b"
TOKEN_B = "onebot-token-BBBB-SHOULD-NEVER-APPEAR-3c4d"
SHORT_SECRET = "sk-1234a"  # 8 个字符 → 预览最多 7 个
SEVEN = "abcdefg"  # 7 个字符 → 预览最多 6 个


# ---------------------------------------------------------------- 假切换器


class FakeRuntime:
    """替身切换器：只记账，不建真连接。

    ``stop()`` 调用次数是验收里的硬指标：**校验阶段必须为 0**——用户填错一个
    端口号不该让她的 QQ 掉线。
    """

    def __init__(self) -> None:
        self.applies: list[tuple[str, dict[str, Any]]] = []
        self.stop_calls = 0
        self.fail_next = False
        self.health = "ok"

    async def apply(self, scope: str, *, values: dict[str, Any], operation_id: str,
                    instance_id: str = "default") -> Any:
        from ling.settings.connector_service import SwapOutcome

        self.applies.append((scope, values))
        if self.fail_next:
            self.fail_next = False
            self.stop_calls += 1  # 真实现里"停旧的"发生在失败之前
            return SwapOutcome(
                ok=False,
                health="degraded",
                error_code="connector_start_failed",
                error_safe="新配置没起起来，已经恢复上一份配置和连接",
                stop_calls=1,
            )
        return SwapOutcome(
            ok=True,
            health=self.health,
            timings={"disconnect_started_at": 0.0, "listener_ready_at": 0.01},
            stop_calls=0,
        )


@pytest.fixture
def fake_runtime(web_env: TestEnv) -> FakeRuntime:
    runtime = FakeRuntime()
    web_env.app.state.connector_runtime = runtime
    return runtime


async def _get(env: TestEnv) -> dict:
    response = await env.client.get(ENDPOINT)
    assert response.status_code == 200, response.text
    return response.json()


def _scope(body: dict, scope: str) -> dict:
    if scope == "access":
        return body["access"]
    for item in body["connectors"]:
        if item["id"] == scope:
            return item
    raise AssertionError(f"没有 scope {scope}")


def _field(body: dict, scope: str, key: str) -> dict:
    for item in _scope(body, scope)["fields"]:
        if item["key"] == key:
            return item
    raise AssertionError(f"{scope} 没有字段 {key}")


async def _put(env: TestEnv, scope: str, body: dict, *, headers: dict | None = None):
    merged = {**ORIGIN, "Content-Type": "application/json"}
    if headers:
        merged.update(headers)
    return await env.client.put(f"{ENDPOINT}/{scope}", json=body, headers=merged)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.append(str(key))
            out.extend(_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_strings(item))
        return out
    return []


def _generation(body: dict, scope: str) -> int:
    return int(_scope(body, scope)["generation"])


# ---------------------------------------------------------------- ① 空覆盖


async def test_empty_overrides_match_pre_upgrade(web_env: TestEnv, logged_in: TestEnv) -> None:  # noqa: F811
    """迁移后没有覆盖：所有字段都从 env/default 来，generation 为 0。"""
    body = await _get(web_env)

    assert body["schema_version"] == "v2"
    assert body["can_edit"] is True
    for scope in ("onebot", "qqbot", "access"):
        assert _generation(body, scope) == 0
        assert _scope(body, scope)["activation"]["state"] == "active"
        for field in _scope(body, scope)["fields"]:
            assert field["source"] in {"env", "default"}, field
            assert field["preview"] is None

    # OneBot 以前根本不在视图里的两项，现在必须在（设计 §3）。
    keys = {f["key"] for f in _scope(body, "onebot")["fields"]}
    assert {"LING_ONEBOT_ACCESS_TOKEN", "LING_ONEBOT_SELF_ID"} <= keys
    assert len(_scope(body, "onebot")["fields"]) == 12
    assert len(_scope(body, "qqbot")["fields"]) == 12
    assert len(_scope(body, "access")["fields"]) == 2


# ---------------------------------------------------------------- ② 写入


async def test_set_secret_persists_ciphertext_only(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    response = await _put(
        web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_A}}
    )
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["state"] == "active"
    assert result["changed"] is True
    assert result["generation"] == 1
    assert result["ack_count"] == 1

    body = await _get(web_env)
    field = _field(body, "onebot", "LING_ONEBOT_ACCESS_TOKEN")
    assert field["source"] == "db"
    assert field["configured"] is True
    assert field["value"] is None, "密钥永远不回 value"
    # 前 7 个 Unicode 字符 + 固定遮罩：TOKEN_A 以 "onebot-" 开头
    assert field["preview"] == "onebot-••••", "前 7 个 Unicode 字符 + 固定遮罩"
    assert _generation(body, "onebot") == 1

    # 库里：只有密文 + 预览 + 指纹，没有明文。
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(ConnectorOverrideValue).where(
                        ConnectorOverrideValue.field_key == "LING_ONEBOT_ACCESS_TOKEN"
                    )
                )
            ).scalars()
        )
    assert rows, "覆盖值没有落库"
    row = rows[-1]
    assert row.ciphertext and TOKEN_A not in row.ciphertext
    assert row.plain_value is None, "密钥不许出现在可读列"
    assert row.preview == "onebot-••••"
    assert row.audit_fingerprint and TOKEN_A not in row.audit_fingerprint

    # 整份响应里都没有完整密钥。
    haystack = _strings(body)
    assert not any(TOKEN_A in text for text in haystack)


async def test_clear_falls_back_to_env_without_copying_it(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """清除 = 删覆盖回退 env；env 的原值**不**被复制进库。"""
    monkeypatch.setenv("LING_ONEBOT_SECRET", "env-secret-value-xyz")
    web_env.app.state.settings = __import__("dataclasses").replace(
        web_env.app.state.settings, onebot_ws_token=None
    )
    await _put(web_env, "onebot", {"set": {"LING_ONEBOT_SECRET": "db-secret-value-123"}})
    body = await _get(web_env)
    assert _field(body, "onebot", "LING_ONEBOT_SECRET")["source"] == "db"
    assert _field(body, "onebot", "LING_ONEBOT_SECRET")["preview"] == "db-secr••••"

    cleared = await _put(web_env, "onebot", {"clear": ["LING_ONEBOT_SECRET"]})
    assert cleared.status_code == 202, cleared.text
    body = await _get(web_env)
    field = _field(body, "onebot", "LING_ONEBOT_SECRET")
    assert field["source"] == "env", "清除后必须回退到 env"
    assert field["preview"] == "env-sec••••", "preview 显示的是**有效来源**的前缀"

    # DB 里不能出现 env 明文（哪怕只是密文副本也不行：那算复制了一份凭据）。
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(ConnectorOverrideValue).where(
                        ConnectorOverrideValue.field_key == "LING_ONEBOT_SECRET"
                    )
                )
            ).scalars()
        )
    # 新修订里**没有**这个 key（clear 的语义是"这一版没有覆盖"，不是写空串）
    async with session_scope() as session:
        head = await session.get(ConnectorConfigHead, ("onebot", DEFAULT_INSTANCE_ID))
        assert head is not None
        active_rows = [
            r for r in rows if r.revision_id == head.active_revision_id
        ]
    assert active_rows == [], "clear 之后新修订里不该还有这个 key"
    # 旧修订仍留着密文（回退要用），但它不是 active——这是刻意的保留。
    assert any(r.revision_id != head.active_revision_id for r in rows)


# ---------------------------------------------------------------- ③ 预览


async def test_secret_preview_rules(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    await _put(web_env, "qqbot", {"set": {"LING_QQBOT_CLIENT_SECRET": SEVEN}})
    body = await _get(web_env)
    field = _field(body, "qqbot", "LING_QQBOT_CLIENT_SECRET")
    preview = field["preview"]
    assert preview is not None
    shown = preview.replace("••••", "")
    assert len(shown) <= 6, f"7 字符密钥最多露 6 个字符，实际露了 {len(shown)}"
    assert preview.endswith("••••")
    assert SEVEN not in preview

    # 1 字符：只显示遮罩，不露任何字符（设计 §5）
    await _put(web_env, "qqbot", {"set": {"LING_QQBOT_BOT_SECRET": "x"}})
    body = await _get(web_env)
    assert _field(body, "qqbot", "LING_QQBOT_BOT_SECRET")["preview"] == "••••"


async def test_short_secret_never_leaks_in_json_or_audit(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    caplog.set_level(logging.DEBUG)
    await _put(
        web_env,
        "onebot",
        {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_B, "LING_ONEBOT_SECRET": SHORT_SECRET}},
    )
    body = await _get(web_env)
    response = await _put(web_env, "qqbot", {"set": {"LING_QQBOT_CLIENT_SECRET": TOKEN_B}})
    assert response.status_code == 202

    everything = _strings(body) + _strings(response.json()) + list(caplog.text.splitlines())
    for secret in (TOKEN_B, SHORT_SECRET):
        assert not any(secret in text for text in everything), f"泄漏了密钥：{secret}"


# ---------------------------------------------------------------- ④ 校验


async def test_arbitrary_env_name_is_rejected_with_zero_stops(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """**任意 env 名一律 422**，且此时代码路径上**一次 stop 都没发生**。"""
    response = await _put(web_env, "onebot", {"set": {"LING_ROOT_KEY": "pwned"}})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["detail"] == "invalid_field"
    assert fake_runtime.applies == [], "校验失败不许进入切换"
    assert fake_runtime.stop_calls == 0

    # 其他 scope 的 key 也不许从 onebot 写进去（跨 scope 混淆）。
    cross = await _put(web_env, "onebot", {"set": {"LING_QQBOT_CLIENT_SECRET": "x"}})
    assert cross.status_code == 422
    assert fake_runtime.applies == []


@pytest.mark.parametrize(
    ("scope", "key", "value"),
    [
        ("onebot", "LING_ONEBOT_WS_PORT", "70000"),
        ("onebot", "LING_ONEBOT_WS_PORT", "abc"),
        ("onebot", "LING_ONEBOT_API_BASE", "ftp://napcat:5700"),
        ("onebot", "LING_ONEBOT_API_BASE", "http://user:pass@napcat:5700"),
        ("onebot", "LING_ONEBOT_API_BASE", "http://napcat:5700/?access_token=abc"),
        ("onebot", "LING_ONEBOT_WS_HOST", "127.0.0.1; rm -rf /"),
        ("access", "LING_ACCESS_MODE", "everyone"),
        ("qqbot", "LING_QQBOT_TOKEN_URL", "ws://api.qq.com/token"),
    ],
)
async def test_field_validation_rejects_bad_values(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime, scope: str, key: str, value: str  # noqa: F811
) -> None:
    response = await _put(web_env, scope, {"set": {key: value}})
    assert response.status_code == 422, (key, response.text)
    assert fake_runtime.stop_calls == 0
    assert fake_runtime.applies == []


async def test_control_chars_in_token_are_422(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    response = await _put(
        web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": "abc\ndef"}}
    )
    assert response.status_code == 422
    assert fake_runtime.stop_calls == 0


async def test_set_and_clear_same_key_is_422(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    response = await _put(
        web_env,
        "onebot",
        {"set": {"LING_ONEBOT_SECRET": "a"}, "clear": ["LING_ONEBOT_SECRET"]},
    )
    assert response.status_code == 422


async def test_ws_port_without_token_is_invalid_config(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """单看每一项都合法、合起来起不来 → 422（在切换之前拦住）。"""
    response = await _put(web_env, "onebot", {"set": {"LING_ONEBOT_WS_PORT": "6700"}})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["detail"] == "invalid_config"
    assert fake_runtime.stop_calls == 0


async def test_empty_secret_means_no_change(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """密码框为空串 = 不改（不能因此把已有覆盖删掉）。"""
    await _put(web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_A}})
    body = await _get(web_env)
    generation = _generation(body, "onebot")

    response = await _put(web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": ""}})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "unchanged"
    assert _generation(await _get(web_env), "onebot") == generation


# ---------------------------------------------------------------- ⑤ 生效与回滚


async def test_start_failure_rolls_back_and_keeps_old_generation(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    await _put(web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_A}})
    before = await _get(web_env)
    generation_before = _generation(before, "onebot")

    fake_runtime.fail_next = True
    response = await _put(
        web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_B}}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "rolled_back", "失败必须如实说 rolled_back"
    assert body["changed"] is False
    assert body["generation"] == generation_before, "失败不许推进代际"
    assert body["error_code"] == "connector_start_failed"

    after = await _get(web_env)
    assert _generation(after, "onebot") == generation_before
    # 有效值仍是 A（回退了），不是 B
    assert _field(after, "onebot", "LING_ONEBOT_ACCESS_TOKEN")["preview"] == "onebot-••••"
    async with session_scope() as session:
        head = await session.get(ConnectorConfigHead, ("onebot", DEFAULT_INSTANCE_ID))
        assert head is not None and head.candidate_revision_id is None
        assert head.active_revision_id is not None
        failed = await session.get(ConnectorConfigRevision, body["revision_id"])
        assert failed is not None
        assert failed.state == "rolled_back"
        assert failed.error_code == "connector_start_failed"


async def test_unchanged_effective_value_is_not_a_new_revision(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """把 ``ACCESS_MODE`` 覆盖成 env 里同样的值 → unchanged，不重连、不写修订。"""
    first = await _put(web_env, "access", {"set": {"LING_ACCESS_MODE": "off"}})
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "unchanged"
    assert fake_runtime.applies == [], "有效值没变就不该触发切换"


async def test_access_change_applies_without_stopping_connections(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    response = await _put(
        web_env,
        "access",
        {"set": {"LING_ACCESS_MODE": "whitelist", "LING_ACCESS_LIST": ["0123456", "77"]}},
    )
    assert response.status_code == 202, response.text
    assert fake_runtime.stop_calls == 0

    body = await _get(web_env)
    values = _field(body, "access", "LING_ACCESS_LIST")["value"]
    assert values == ["0123456", "77"], "前导零必须保留"
    redis = await _put(
        web_env, "access", {"set": {"LING_ACCESS_LIST": ["1", "0", "0x0a"]}}
    )
    assert redis.status_code == 202
    assert _field(await _get(web_env), "access", "LING_ACCESS_LIST")["value"] == [
        "1",
        "0",
        "0x0a",
    ]


# ---------------------------------------------------------------- ⑥ 门禁


async def test_member_cannot_write(web_env: TestEnv, logged_in: TestEnv) -> None:  # noqa: F811
    from httpx import ASGITransport, AsyncClient

    from ling.infra.models import User
    from ling.infra.security import hash_password, new_id

    async with session_scope() as session:
        session.add(
            User(
                id=new_id("user"),
                username="connector_member",
                password_hash=hash_password("member-password-9"),
                role="member",
                subject_id="subj_connector_member",
            )
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=web_env.app), base_url="http://testserver"
    ) as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": "connector_member", "password": "member-password-9"},
        )
        assert login.status_code == 200, login.text
        response = await client.put(
            f"{ENDPOINT}/onebot",
            json={"set": {"LING_ONEBOT_ACCESS_TOKEN": "x"}},
            headers={**ORIGIN, "Content-Type": "application/json"},
        )
        read = await client.get(ENDPOINT)
    assert response.status_code == 403
    assert response.json()["error"]["detail"] == "not_owner"
    # 非 owner 仍然能看诊断，但看不到敏感预览、也不能编辑。
    body = read.json()
    assert body["can_edit"] is False
    assert _scope(body, "onebot")["editable"] is False
    assert all(f["preview"] is None for f in _scope(body, "onebot")["fields"])


async def test_cross_site_origin_is_rejected(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    response = await _put(
        web_env,
        "onebot",
        {"set": {"LING_ONEBOT_ACCESS_TOKEN": "x"}},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["detail"] == "bad_origin"
    assert fake_runtime.applies == []


async def test_missing_origin_is_rejected(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """cookie 会话必须带 Origin；缺了就是 fail-closed（不是"默认放行"）。"""
    response = await web_env.client.put(
        f"{ENDPOINT}/onebot",
        json={"set": {"LING_ONEBOT_ACCESS_TOKEN": "x"}},
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert fake_runtime.applies == []


async def test_old_session_can_still_write(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """登录 2 小时的老会话照样能改接入器：会话年龄不再是写入门禁。"""
    from datetime import UTC, datetime, timedelta

    from ling.infra.security import hash_token

    token = logged_in.client.cookies.get(logged_in.settings.cookie_name)
    assert token
    async with session_scope() as session:
        session_row = (
            await session.execute(
                select(AuthSession).where(AuthSession.token_hash == hash_token(token))
            )
        ).scalar_one()
        session_row.created_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()

    response = await _put(
        web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_A}}
    )
    assert response.status_code == 202, response.text
    assert response.json()["state"] == "active"


async def test_generation_conflict_is_409(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    await _put(web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_A}})
    stale = await _put(
        web_env,
        "onebot",
        {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_B}},
        headers={"If-Match": "0"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["detail"] == "generation_conflict"


async def test_activation_in_progress_is_409(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """候选已落库、还没确认时，第二个写入者拿到 409（不并发切换）。"""
    async with session_scope() as session:
        head = ConnectorConfigHead(
            scope="onebot", instance_id=DEFAULT_INSTANCE_ID, generation=0
        )
        session.add(head)
        revision = ConnectorConfigRevision(
            id="crev_inflight", scope="onebot", state="activating"
        )
        session.add(revision)
        head.candidate_revision_id = "crev_inflight"
        await session.commit()

    response = await _put(web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": "x"}})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["detail"] == "activation_in_progress"
    assert fake_runtime.applies == []


# ---------------------------------------------------------------- ⑦ 操作与重置


async def test_operation_endpoint_reports_terminal_state(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    response = await _put(web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_A}})
    operation_id = response.json()["operation_id"]
    status = await web_env.client.get(
        f"{ENDPOINT}/operations/{operation_id}"
    )
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["state"] == "active"
    assert body["scope"] == "onebot"
    assert body["operation_id"] == operation_id

    missing = await web_env.client.get(f"{ENDPOINT}/operations/crev_nope")
    assert missing.status_code == 404


async def test_reset_clears_all_scopes(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    await _put(web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_A}})
    await _put(
        web_env, "access", {"set": {"LING_ACCESS_MODE": "whitelist", "LING_ACCESS_LIST": ["1"]}}
    )
    response = await web_env.client.post(
        f"{ENDPOINT}/reset", headers={**ORIGIN, "Content-Type": "application/json"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    states = {item["scope"]: item["state"] for item in body["results"]}
    assert states["onebot"] == "active"
    assert states["qqbot"] == "unchanged"

    after = await _get(web_env)
    assert _field(after, "onebot", "LING_ONEBOT_ACCESS_TOKEN")["source"] in {
        "env",
        "default",
    }
    assert _field(after, "access", "LING_ACCESS_MODE")["source"] in {"env", "default"}


# ---------------------------------------------------------------- ⑧ 审计


async def test_audit_records_fields_and_fingerprints_not_values(
    web_env: TestEnv, logged_in: TestEnv, fake_runtime: FakeRuntime, caplog: pytest.LogCaptureFixture  # noqa: F811
) -> None:
    with caplog.at_level(logging.INFO, logger="ling.audit"):
        await _put(web_env, "onebot", {"set": {"LING_ONEBOT_ACCESS_TOKEN": TOKEN_A}})
    records = [
        r for r in caplog.records if getattr(r, "event_name", "") == "audit_connector_config_changed"
    ]
    assert records, "保存必须留一条审计"
    # `audit_event` 把业务字段挂在 payload["attributes"] 下（外层是 log_kind /
    # action / outcome / actor / target_*）。
    record = records[-1]
    assert record.attributes["target_id"] == "onebot"
    attrs = record.attributes["attributes"]
    assert attrs["scope"] == "onebot"
    assert "LING_ONEBOT_ACCESS_TOKEN" in attrs["fields"]
    assert attrs["fingerprints"]["LING_ONEBOT_ACCESS_TOKEN"]
    text = json.dumps(record.attributes, ensure_ascii=False)
    assert TOKEN_A not in text, "审计里不许出现值"
    assert "onebot-••••" not in text, "审计里也不许出现预览"


# ---------------------------------------------------------------- ⑨ 入站一致性


async def test_inbound_reads_the_same_effective_snapshot(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """改白名单之后，**入站判断读的是同一份已生效快照**。

    设计 §4 的原话：OneBot HTTP 入站与白名单判断必须通过同一已生效快照，
    不能验签读旧 token、授权读新名单。这里用真 app 的切换器（不是假替身）
    改一次白名单，再打一条 OneBot 上报，断言"新名单立刻生效"。
    """
    import dataclasses

    from ling.kernel.access import check_access

    # 先设成"只允许 10001"：这是被替换掉的旧快照。
    web_env.app.state.settings = dataclasses.replace(
        web_env.app.state.settings,
        access_mode="whitelist",
        access_list=frozenset({"10001"}),
    )
    old = check_access(
        "100200300",
        mode=web_env.app.state.settings.access_mode,
        allow_list=web_env.app.state.settings.access_list,
        group_id=None,
        allow_groups=frozenset(),
    )
    assert old.observable is False, "前置条件：旧名单里没有这个人"

    # 走真实切换器（`app.state.connector_runtime`），不注入替身。
    response = await _put(
        web_env,
        "access",
        {"set": {"LING_ACCESS_MODE": "whitelist", "LING_ACCESS_LIST": ["100200300"]}},
    )
    assert response.status_code == 202, response.text

    new = check_access(
        "100200300",
        mode=web_env.app.state.settings.access_mode,
        allow_list=web_env.app.state.settings.access_list,
        group_id=None,
        allow_groups=frozenset(),
    )
    assert new.observable is True, "新名单必须立刻对入站生效"
    # 快照对象也同步更新（诊断页与入站读的是同一个来源）。
    snapshot = web_env.app.state.access_snapshot
    assert "100200300" in snapshot.allow_list
    assert snapshot.mode == "whitelist"


async def test_access_change_does_not_restart_connections(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """白名单变更**不重建**任何长连接（设计 §3：白名单无需重连）。"""
    calls: list[str] = []

    class _Recorder:
        async def stop(self) -> None:
            calls.append("stop")

        def start(self) -> None:
            calls.append("start")

    web_env.app.state.onebot_ws_server = _Recorder()
    response = await _put(
        web_env, "access", {"set": {"LING_ACCESS_MODE": "off"}}
    )
    assert response.status_code in (200, 202), response.text
    assert calls == [], "白名单变更不该停/启连接器"
