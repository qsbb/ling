"""`GET /v1/logs`：鉴权、过滤参数、响应形状，以及**脱敏真的到了接口出口**。

这一组只测 HTTP 这一层；缓冲本身的容量/顺序/过滤在
`tests/unit/test_log_buffer.py`。两边刻意分开：缓冲区是纯函数式的模块级组件，
可以脱离 app 测；而"谁能看到它"是接口的事，必须在真实 app + 真实登录态上测。

三条硬要求各有对应用例，不能只断言 200：

1. **普通成员被拒**——日志里有内部路径与账号标识，不该给所有人看。断言
   具体是 403 + `not_log_viewer`，不是"反正不是 200"。
2. **脱敏在接口出口仍然成立**——写入一条带 `api_key=sk-secret` 的日志，
   把整个响应体当字符串扫一遍，明文不能出现。只看 `message` 字段不够：
   `attributes` 嵌套里也可能带。
3. **顺序与参数语义**——正序返回、`level` 是「不低于」、`logger` 是前缀、
   `q` 不区分大小写。这些语义错了界面不会报错，只会显示错的东西。
"""

from __future__ import annotations

import logging

import pytest

from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.logging_setup import clear_log_buffer, recent_logs
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

PROBE_LOGGER = "ling.test.logs_api"


@pytest.fixture(autouse=True)
def _clean_buffer() -> None:
    """每条用例从空缓冲开始：缓冲是进程级单例，不隔离就会带上别的用例的日志。"""
    clear_log_buffer()
    yield
    clear_log_buffer()


def _emit(level: int, message: str, *, logger_name: str = PROBE_LOGGER, **kwargs: object) -> None:
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.log(level, message, **kwargs)


async def _login_as_member(env: TestEnv) -> None:
    """建一个普通成员并登录（覆盖掉当前 cookie）。"""
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="member_logs", password="member-password", role="member"
        )
    response = await env.client.post(
        "/v1/auth/login", json={"username": "member_logs", "password": "member-password"}
    )
    assert response.status_code == 200, response.text


async def _login_as_owner(env: TestEnv) -> None:
    response = await env.client.post(
        "/v1/auth/login", json={"username": "tester", "password": "tester-password"}
    )
    assert response.status_code == 200, response.text


# ------------------------------------------------------------------ 鉴权


async def test_anonymous_is_rejected(web_env: TestEnv) -> None:
    response = await web_env.client.get("/v1/logs")
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthorized"


async def test_member_is_forbidden_not_given_an_empty_list(logged_in: TestEnv) -> None:
    """普通成员拿到 403，而不是空数组。

    空数组会让人以为"后端确实没有日志"，那是完全不同的结论——回退成 200 + []
    等于把"你没权限"伪装成"这里什么都没有"。
    """
    _emit(logging.ERROR, "成员不该看到这条")
    await _login_as_member(logged_in)

    response = await logged_in.client.get("/v1/logs")
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "forbidden"
    assert body["error"]["detail"] == "not_log_viewer"
    assert "items" not in body


async def test_owner_can_read_after_member_was_rejected(logged_in: TestEnv) -> None:
    """拒绝不能把功能锁死：换回 owner 立刻可读。"""
    _emit(logging.WARNING, "owner 能看到的")
    await _login_as_member(logged_in)
    assert (await logged_in.client.get("/v1/logs")).status_code == 403

    await _login_as_owner(logged_in)
    response = await logged_in.client.get("/v1/logs")
    assert response.status_code == 200, response.text
    messages = [item["message"] for item in response.json()["items"]]
    assert "owner 能看到的" in messages


async def test_admin_role_is_accepted(logged_in: TestEnv) -> None:
    """`admin` 与 `owner` 同等（运维身份），不能只认 owner 一个字面量。"""
    async with session_scope() as session:
        await IdentityService(session).create_user(
            username="ops_admin", password="admin-password", role="admin"
        )
    login = await logged_in.client.post(
        "/v1/auth/login", json={"username": "ops_admin", "password": "admin-password"}
    )
    assert login.status_code == 200, login.text

    response = await logged_in.client.get("/v1/logs")
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"items", "buffered", "capacity"}


# ------------------------------------------------------------ 响应形状


async def test_response_shape_and_ascending_order(logged_in: TestEnv) -> None:
    """`items` 时间正序（最新在最后），并报告缓冲占用与容量。"""
    for index in range(3):
        _emit(logging.INFO, f"顺序探针 {index}")

    response = await logged_in.client.get("/v1/logs", params={"limit": 1000})
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == {"items", "buffered", "capacity"}
    assert body["capacity"] >= 1
    assert body["buffered"] >= 1

    probes = [item for item in body["items"] if item["message"].startswith("顺序探针")]
    assert [item["message"] for item in probes] == [
        "顺序探针 0",
        "顺序探针 1",
        "顺序探针 2",
    ]
    # 时间戳可解析且有 UTC 偏移
    first = probes[0]
    assert first["timestamp"].endswith("+00:00")
    for key in ("timestamp", "level", "logger", "message"):
        assert key in first, key
    assert first["logger"] == PROBE_LOGGER
    assert first["level"] == "info"


async def test_level_filter_is_not_lower_than(logged_in: TestEnv) -> None:
    _emit(logging.DEBUG, "级别探针 debug")
    _emit(logging.INFO, "级别探针 info")
    _emit(logging.WARNING, "级别探针 warning")
    _emit(logging.ERROR, "级别探针 error")

    async def messages(level: str | None) -> list[str]:
        params: dict[str, object] = {"limit": 1000}
        if level:
            params["level"] = level
        response = await logged_in.client.get("/v1/logs", params=params)
        assert response.status_code == 200, response.text
        return [
            item["message"]
            for item in response.json()["items"]
            if item["message"].startswith("级别探针")
        ]

    assert await messages("warning") == ["级别探针 warning", "级别探针 error"]
    assert await messages("error") == ["级别探针 error"]
    # 不传 = 全部（默认级别 INFO 的 root logger 会滤掉 debug，这里只断言 info 起）
    assert "级别探针 info" in await messages(None)


async def test_unknown_level_is_422(logged_in: TestEnv) -> None:
    response = await logged_in.client.get("/v1/logs", params={"level": "fatal"})
    assert response.status_code == 422, response.text


async def test_logger_prefix_filter(logged_in: TestEnv) -> None:
    _emit(logging.INFO, "前缀命中", logger_name="ling.test.logs_api.child")
    _emit(logging.INFO, "前缀不命中", logger_name="ling.other.module")

    response = await logged_in.client.get(
        "/v1/logs", params={"logger": "ling.test.logs_api", "limit": 1000}
    )
    assert response.status_code == 200, response.text
    messages = [item["message"] for item in response.json()["items"]]
    assert "前缀命中" in messages
    assert "前缀不命中" not in messages


async def test_query_is_case_insensitive(logged_in: TestEnv) -> None:
    _emit(logging.INFO, "Provider TIMEOUT after 30s")
    _emit(logging.INFO, "连接正常")

    response = await logged_in.client.get(
        "/v1/logs", params={"q": "provider time", "limit": 1000}
    )
    assert response.status_code == 200, response.text
    messages = [item["message"] for item in response.json()["items"]]
    assert messages == ["Provider TIMEOUT after 30s"]


async def test_limit_bounds(logged_in: TestEnv) -> None:
    for index in range(5):
        _emit(logging.INFO, f"条数探针 {index}")

    tail = await logged_in.client.get(
        "/v1/logs", params={"logger": PROBE_LOGGER, "limit": 2}
    )
    assert tail.status_code == 200, tail.text
    messages = [item["message"] for item in tail.json()["items"]]
    assert messages == ["条数探针 3", "条数探针 4"]

    for bad in (0, -1, 1001):
        response = await logged_in.client.get("/v1/logs", params={"limit": bad})
        assert response.status_code == 422, (bad, response.text)


async def test_empty_buffer_says_zero_not_hidden(logged_in: TestEnv) -> None:
    """缓冲为空时 `items` 为空、`buffered=0`——这是**事实**，不是错误。"""
    clear_log_buffer()
    response = await logged_in.client.get("/v1/logs")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["buffered"] == 0
    assert body["capacity"] >= 1


# ------------------------------------------------------------------ 脱敏


async def test_secrets_never_leave_through_the_endpoint(logged_in: TestEnv) -> None:
    """写入带 `api_key=sk-secret` 的日志，整个响应体的字符串里不能有明文。

    扫整个响应（而不是只看 `message`）：`attributes` 嵌套、`event_name`、
    以后新加的字段都在同一条出口上，只盯一个字段的测试会随字段增减失效。
    """
    _emit(logging.ERROR, "调用失败 api_key=sk-secret 请检查密钥")
    _emit(
        logging.ERROR,
        "上游拒绝",
        extra={"attributes": {"access_token": "tok-plaintext-value", "attempt": 1}},
    )

    response = await logged_in.client.get("/v1/logs", params={"limit": 1000})
    assert response.status_code == 200, response.text
    raw = response.text

    assert "sk-secret" not in raw
    assert "tok-plaintext-value" not in raw
    assert "[REDACTED]" in raw

    # 打码不影响其它字段：这条日志其余部分仍可用来排查
    items = response.json()["items"]
    probe = next(item for item in items if item["message"].startswith("调用失败"))
    assert "请检查密钥" in probe["message"]


async def test_secrets_do_not_survive_in_the_buffer_either(logged_in: TestEnv) -> None:
    """内存里那份也必须是打码后的——接口只是它的一条读路径。"""
    _emit(logging.ERROR, "凭据 api_key=sk-secret")

    entries = recent_logs(limit=50, logger=PROBE_LOGGER)
    assert entries
    assert "sk-secret" not in "\n".join(entry.message for entry in entries)


# ------------------------------------------------- 轮询端点不给自己灌日志


async def _access_entries() -> list:
    return recent_logs(limit=1000, logger="ling.api.access")


async def test_successful_log_poll_does_not_log_itself(logged_in: TestEnv) -> None:
    """成功的 `/v1/logs` 请求**不写访问日志**。

    否则就是自我观察的死循环：页面每 2 秒拉一次，每次都在它自己正在读的缓冲里
    加一条，500 条的缓冲十几分钟就被自己刷满，真正的故障日志全被挤掉——表现是
    "打开日志页看了一会儿，之前那条错误就不见了"。
    """
    clear_log_buffer()
    for _ in range(5):
        response = await logged_in.client.get("/v1/logs")
        assert response.status_code == 200, response.text

    paths = [
        entry.attributes.get("http.request.path")
        for entry in await _access_entries()
    ]
    assert "/v1/logs" not in paths, paths


async def test_failing_log_poll_is_still_logged(logged_in: TestEnv) -> None:
    """失败的轮询仍要留痕——一个一直 500 的日志接口最需要被看见。

    只静默成功，不能连失败一起静：那样排查时就只剩界面上一句"读取失败"，
    服务端一个字都没有。
    """
    clear_log_buffer()
    # 422（limit 越界）走的是真实的错误路径，不是我们伪造的响应
    response = await logged_in.client.get("/v1/logs", params={"limit": 0})
    assert response.status_code == 422, response.text

    paths = [
        entry.attributes.get("http.request.path")
        for entry in await _access_entries()
    ]
    assert "/v1/logs" in paths, paths


async def test_other_endpoints_still_log_access(logged_in: TestEnv) -> None:
    """静默名单只对轮询端点生效，别的接口照旧留痕（不能顺手把它们一起静掉）。"""
    clear_log_buffer()
    assert (await logged_in.client.get("/v1/sessions")).status_code == 200

    paths = [
        entry.attributes.get("http.request.path")
        for entry in await _access_entries()
    ]
    assert "/v1/sessions" in paths, paths

