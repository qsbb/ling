"""B5：只读健康端点漏鉴权 + `/readyz` 的探针兼容性。

## 这一组要钉住的三件事

1. **四个只读端点必须鉴权**：`/v1/settings/health`、`/v1/health/model`、
   `/v1/onebot/health`、`/v1/qqbot/health`。它们原先都不过 `current_auth`，
   于是首次安装未改密时（`must_change_password` 闸就写在 `current_auth` 里）
   照样能被匿名读取；其中两个还会**外呼**（NapCat / QQ 开放平台）。
2. **`/readyz` 不能加鉴权**：`deploy/compose.yaml` 的 api healthcheck 用
   `urllib.request.urlopen('http://127.0.0.1:6520/readyz')` 且只看状态码，
   加了鉴权容器会永远 unhealthy。所以它是"未鉴权只给状态、已鉴权给详情"。
3. **`/healthz` 永远无鉴权、无敏感信息**：它是存活探针。

变异验证（回退任一处 auth / 把 `/readyz` 的详情无条件返回）必须让对应用例变红。
"""

from __future__ import annotations

import json

import httpx
import pytest
from connectors.onebot.connector import OneBotV11Connector
from connectors.onebot.settings import OneBotConfig
from connectors.qqbot.connector import describe_config
from connectors.qqbot.settings import QQBotConfig

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ONEBOT_CFG = OneBotConfig(
    api_base="http://onebot-health.test:5700",
    access_token="tok",
    secret="sec",
    self_id="10001",
    instance_id="onebot-health",
)

# 四个必须鉴权的只读端点
PROTECTED_ENDPOINTS: tuple[str, ...] = (
    "/v1/settings/health",
    "/v1/health/model",
    "/v1/onebot/health",
    "/v1/qqbot/health",
)

# `/readyz` 未鉴权时**不得出现**的字段（部署指纹）
READYZ_PRIVATE_KEYS = {"backend", "config_revision", "test_mode", "profiles", "detail"}


class FakeOneBot:
    """只记录 `get_login_info` 调用次数；用来证明缓存与"未鉴权不外呼"。"""

    def __init__(self) -> None:
        self.login_info_calls = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        action = str(request.url).rsplit("/", 1)[-1]
        if action == "get_login_info":
            self.login_info_calls += 1
            return httpx.Response(
                200,
                json={"status": "ok", "retcode": 0, "data": {"user_id": 10001}},
            )
        return httpx.Response(404, json={"status": "failed", "retcode": 1404})


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _install_onebot(env: TestEnv) -> tuple[OneBotV11Connector, FakeOneBot, FakeClock]:
    fake = FakeOneBot()
    clock = FakeClock()
    connector = OneBotV11Connector(
        ONEBOT_CFG, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    )
    connector._health_clock = clock
    env.app.state.onebot_connector = connector
    return connector, fake, clock


# ---------------------------------------------------------------- 鉴权


@pytest.mark.parametrize("path", PROTECTED_ENDPOINTS)
async def test_readonly_health_endpoints_require_auth(
    web_env: TestEnv, path: str
) -> None:
    """未登录一律 401（不是"返回数据但没权限"）。"""
    response = await web_env.client.get(path)
    assert response.status_code == 401, (
        f"GET {path} 未要求鉴权（返回 {response.status_code}）"
    )
    assert response.json()["error"]["code"] == "unauthorized"


async def test_onebot_health_does_not_call_napcat_when_unauthenticated(
    web_env: TestEnv,
) -> None:
    """鉴权在路由依赖里先于端点执行：未登录的请求**不能**产生外呼。

    否则"漏鉴权"就不只是读数据，而是把别人家的 NapCat 变成我们的放大器。
    """
    _connector, fake, _clock = _install_onebot(web_env)
    response = await web_env.client.get("/v1/onebot/health")
    assert response.status_code == 401
    assert fake.login_info_calls == 0


async def test_protected_endpoints_work_after_login(logged_in: TestEnv) -> None:
    """鉴权后照常可用——不能为了安全把功能锁死。"""
    model = await logged_in.client.get("/v1/health/model")
    assert model.status_code == 200, model.text
    assert "profiles" in model.json()

    settings_health = await logged_in.client.get("/v1/settings/health")
    assert settings_health.status_code == 200, settings_health.text
    assert "health" in settings_health.json()

    _connector, _fake, _clock = _install_onebot(logged_in)
    onebot = await logged_in.client.get("/v1/onebot/health")
    assert onebot.status_code == 200, onebot.text
    assert onebot.json()["ok"] is True


# ---------------------------------------------------------------- /readyz


async def test_readyz_unauthenticated_returns_status_only(web_env: TestEnv) -> None:
    """容器 healthcheck 不带凭据：必须 200，且**只有** status。

    这条同时保护两件事：
    - 探针不能坏（`deploy/compose.yaml` 只看状态码，200 才算 healthy）
    - 部署指纹（数据库后端、修订号、测试模式、职责列表）不外泄
    """
    response = await web_env.client.get("/readyz")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"status": "ready"}, body
    assert READYZ_PRIVATE_KEYS.isdisjoint(body), body


async def test_readyz_authenticated_returns_full_detail(logged_in: TestEnv) -> None:
    """已鉴权才给完整详情（运维排查要用它）。"""
    response = await logged_in.client.get("/readyz")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["backend"] == "postgresql"
    assert "config_revision" in body
    assert "test_mode" in body
    assert body["profiles"], body


async def test_readyz_unauthenticated_db_failure_hides_exception(
    web_env: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库不可用时：503 但**不含异常原文**，只给状态。

    异常里常有 DSN（含账号口令）、主机名、驱动内部细节。这些进日志可以，
    进一个匿名可读的响应体不行。
    """
    import ling.app as app_module

    secret_dsn = "postgresql://ling:hunter2@db.internal:5432/ling"

    async def _boom(_settings):  # noqa: ANN001
        raise RuntimeError(f"cannot connect to {secret_dsn}")

    monkeypatch.setattr(app_module, "ready_check", _boom)

    response = await web_env.client.get("/readyz")
    assert response.status_code == 503, response.text
    body = response.json()
    assert body == {"status": "not-ready"}, body
    assert secret_dsn not in response.text
    assert "hunter2" not in response.text


async def test_readyz_authenticated_db_failure_shows_reason(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已鉴权时给出原因——"为什么没就绪"是运维必须能查的。"""
    import ling.app as app_module

    async def _boom(_settings):  # noqa: ANN001
        raise RuntimeError("数据库不可达")

    monkeypatch.setattr(app_module, "ready_check", _boom)

    response = await logged_in.client.get("/readyz")
    assert response.status_code == 503
    assert "detail" in response.json()
    assert "数据库不可达" in response.json()["detail"]


# ---------------------------------------------------------------- /healthz


async def test_healthz_is_public_and_has_no_sensitive_fields(
    web_env: TestEnv,
) -> None:
    """存活探针不鉴权、也不吐部署细节（它会被监控反复打）。"""
    response = await web_env.client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "alive", "service": "ling-api"}, body
    assert READYZ_PRIVATE_KEYS.isdisjoint(body), body


# ---------------------------------------------------------------- 外呼缓存


async def test_onebot_health_is_cached_between_calls(logged_in: TestEnv) -> None:
    """短 TTL 缓存：10 秒内重复探活**不得**重复外呼 NapCat。

    端点曾经无鉴权也无缓存，任何人可以无限刷它去打 NapCat。
    """
    connector, fake, clock = _install_onebot(logged_in)
    first = await logged_in.client.get("/v1/onebot/health")
    assert first.status_code == 200, first.text
    assert fake.login_info_calls == 1

    second = await logged_in.client.get("/v1/onebot/health")
    assert second.status_code == 200
    assert fake.login_info_calls == 1, "缓存没生效：第二次仍在打 NapCat"

    # 走过 TTL 后重新探活（缓存不能把探活变成"永远不探"）
    clock.value += connector.health_cache_seconds + 1.0
    third = await logged_in.client.get("/v1/onebot/health")
    assert third.status_code == 200
    assert fake.login_info_calls == 2


# ---------------------------------------------------------------- 明文凭据


def test_describe_config_has_no_app_id_plaintext() -> None:
    """`describe_config` 只给指纹。AppID 是机器人身份的一半。"""
    cfg = QQBotConfig(
        app_id="102000001",
        client_secret="fake-client-secret",
        instance_id="qqbot-unit",
        bot_secret="fake-bot-secret",
    )
    summary = describe_config(cfg)
    assert "app_id" not in summary, summary
    assert summary["app_id_configured"] is True
    assert summary["app_id_fingerprint"]
    dumped = json.dumps(summary, ensure_ascii=False)
    assert "102000001" not in dumped
    assert "fake-client-secret" not in dumped
    assert "fake-bot-secret" not in dumped


async def test_qqbot_health_detail_has_no_app_id_plaintext(logged_in: TestEnv) -> None:
    """detail 与 config 一起检查：两处曾经都带 app_id 明文。"""
    import dataclasses

    from connectors.qqbot.connector import QQBotConnector

    from tests.integration.test_qqbot_loop import CFG, CLIENT_SECRET, FakeQQBot

    connector = QQBotConnector(
        dataclasses.replace(CFG, instance_id="qqbot-health-plain"),
        http=httpx.AsyncClient(transport=httpx.MockTransport(FakeQQBot().handler)),
    )
    logged_in.app.state.qqbot_connector = connector

    response = await logged_in.client.get("/v1/qqbot/health")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert CFG.app_id not in json.dumps(body, ensure_ascii=False), body
    assert CLIENT_SECRET not in json.dumps(body, ensure_ascii=False), body


# ---------------------------------------------------------------- 模型健康摘要


async def test_settings_health_does_not_expose_last_error(logged_in: TestEnv) -> None:
    """`health_snapshot()` 的 `last_error` 是供应商原文，不再进汇总响应。"""
    from ling.models.router import HealthRecord

    router_ = logged_in.app.state.router
    router_._health["fake/fake-model"] = HealthRecord(
        consecutive_failures=3,
        last_error="upstream said: invalid api key sk-live-DEADBEEF",
        last_latency_ms=1200,
    )
    response = await logged_in.client.get("/v1/settings/health")
    assert response.status_code == 200, response.text
    body = response.json()
    record = body["health"]["fake/fake-model"]
    assert "last_error" not in record, record
    # 其余诊断字段保留：没有它们，这条端点就只剩"都正常"三个字
    assert record["consecutive_failures"] == 3
    assert record["last_latency_ms"] == 1200
    assert "sk-live-DEADBEEF" not in json.dumps(body, ensure_ascii=False)
