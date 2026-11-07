"""供应商级额度与协议适配器的行为验证。

覆盖三件事，都是"看起来实现了、其实没有"的高危形状：

1. **-1 真的等于不限制**：不是"小于 0 就当作 0"，也不是"留空就用默认值"。
2. **额度真的会拦调用**：不只是记个数然后照旧放行。
3. **两个新适配器真的解析各自的 SSE 事件**：Anthropic 的
   ``content_block_delta`` 与 Responses 的 ``response.output_text.delta``
   都不是 ``choices[].delta.content``，写错不会报错，只会永远拿到空回复。
"""

from __future__ import annotations

import json

import httpx
import pytest

from ling.models.anthropic import AnthropicProvider
from ling.models.chat_responses import ChatResponsesProvider
from ling.models.openai_compatible import OpenAICompatibleProvider
from ling.models.quota import TokenQuotaStore, local_day
from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.config import EffectiveConfig, ProfileBinding, ProviderConfig
from ling.ports.model import ChatMessageIn, ModelError, ModelErrorKind, ModelRequest

pytestmark = pytest.mark.w0_web


def _request(**overrides) -> ModelRequest:
    base = {
        "model_id": "test-model",
        "messages": [ChatMessageIn(role="user", content="你好")],
        "profile": "dialogue.fast",
    }
    base.update(overrides)
    return ModelRequest(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- -1 语义


def test_negative_limit_means_unlimited() -> None:
    """-1 必须归一成 None（不限制），而不是被当成 0 或"没设置"。"""
    cfg = ProviderConfig(
        provider_id="p",
        kind="openai_compatible",
        max_output_tokens=-1,
        daily_token_limit=-1,
    )
    assert cfg.max_output_effective() is None
    assert cfg.daily_limit_effective() is None


def test_positive_limit_is_kept() -> None:
    cfg = ProviderConfig(
        provider_id="p",
        kind="openai_compatible",
        max_output_tokens=200000,
        daily_token_limit=200000,
    )
    assert cfg.max_output_effective() == 200000
    assert cfg.daily_limit_effective() == 200000


def test_zero_is_treated_as_unset_not_as_zero_budget() -> None:
    """0 不是"零额度"：它是旧修订里"没配过"的常见形状，当成 0 会让这一家
    完全不能调用，而用户从界面上看不出原因。"""
    cfg = ProviderConfig(provider_id="p", kind="openai_compatible", daily_token_limit=0)
    assert cfg.daily_limit_effective() is None


@pytest.mark.asyncio
async def test_openai_compatible_omits_temperature_when_none() -> None:
    """温度不再是可配置项：``None`` 时**不发送**这个字段。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "temperature" not in body, f"不该发送 temperature，实际发了 {body['temperature']}"
        return httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(
        provider_id="c", base_url="http://stub.local/v1", api_key="sk"
    )
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        chunks = [c async for c in provider.stream(_request())]
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
    assert "".join(c.delta for c in chunks) == "好"


@pytest.mark.asyncio
async def test_openai_compatible_sends_temperature_when_given() -> None:
    """显式给了温度就照发——这是给测试与将来的"确实想调"留的路径。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["temperature"] == 0.3
        return httpx.Response(
            200,
            content=(
                'data: {"choices":[{"delta":{"content":"好"},"finish_reason":"stop"}]}\n\n'
                "data: [DONE]\n\n"
            ).encode(),
            headers={"content-type": "text/event-stream"},
        )

    provider = OpenAICompatibleProvider(
        provider_id="c", base_url="http://stub.local/v1", api_key="sk"
    )
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        chunks = [c async for c in provider.stream(_request(temperature=0.3))]
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
    assert "".join(c.delta for c in chunks) == "好"


# ---------------------------------------------------------------- Anthropic


@pytest.mark.asyncio
async def test_anthropic_parses_content_block_delta() -> None:
    """文本在 ``content_block_delta``，不在 ``choices``。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/messages")
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.headers["x-api-key"] == "sk-ant"
        body = json.loads(request.content)
        # system 必须提到顶层，不能留在 messages 里
        assert body["system"] == "你是 ling"
        assert [m["role"] for m in body["messages"]] == ["user"]
        assert body["max_tokens"] == 4096  # 未设上限时的显式值
        lines = [
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"你"}}',
            'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"好"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":11,"output_tokens":2}}',
            'data: {"type":"message_stop"}',
        ]
        return httpx.Response(
            200,
            content=("\n\n".join(lines) + "\n\n").encode(),
            headers={"content-type": "text/event-stream"},
        )

    provider = AnthropicProvider(
        provider_id="ant", base_url="http://stub.local/v1", api_key="sk-ant"
    )
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        chunks = [
            c
            async for c in provider.stream(
                _request(
                    messages=[
                        ChatMessageIn(role="system", content="你是 ling"),
                        ChatMessageIn(role="user", content="你好"),
                    ]
                )
            )
        ]
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert "".join(c.delta for c in chunks) == "你好"
    assert chunks[-1].done is True
    assert provider.last_usage is not None
    assert provider.last_usage.total_tokens == 13


@pytest.mark.asyncio
async def test_anthropic_requires_max_tokens() -> None:
    """Anthropic 的 max_tokens 是必填：不设上限也必须给一个显式数字。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "max_tokens" in body and body["max_tokens"] > 0
        return httpx.Response(
            200,
            content=b'data: {"type":"message_stop"}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    provider = AnthropicProvider(
        provider_id="ant", base_url="http://stub.local/v1", api_key="sk-ant"
    )
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        _ = [c async for c in provider.stream(_request(max_tokens=None))]
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]


# ------------------------------------------------------------ Responses API


@pytest.mark.asyncio
async def test_chat_responses_parses_output_text_delta() -> None:
    """文本在 ``response.output_text.delta``，输入结构是 item 数组。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/responses")
        body = json.loads(request.content)
        assert body["instructions"] == "你是 ling"
        assert body["input"][0]["content"][0]["type"] == "input_text"
        lines = [
            'data: {"type":"response.output_text.delta","delta":"你"}',
            'data: {"type":"response.output_text.delta","delta":"好"}',
            'data: {"type":"response.completed","response":{"usage":{"input_tokens":9,"output_tokens":2}}}',
        ]
        return httpx.Response(
            200,
            content=("\n\n".join(lines) + "\n\n").encode(),
            headers={"content-type": "text/event-stream"},
        )

    provider = ChatResponsesProvider(
        provider_id="resp", base_url="http://stub.local/v1", api_key="sk"
    )
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        chunks = [
            c
            async for c in provider.stream(
                _request(
                    messages=[
                        ChatMessageIn(role="system", content="你是 ling"),
                        ChatMessageIn(role="user", content="你好"),
                    ]
                )
            )
        ]
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]

    assert "".join(c.delta for c in chunks) == "你好"
    assert chunks[-1].done is True
    assert provider.last_usage is not None
    assert provider.last_usage.total_tokens == 11


@pytest.mark.asyncio
async def test_chat_responses_surfaces_failed_event() -> None:
    """``response.failed`` 必须变成错误，而不是安静地结束。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=(
                'data: {"type":"response.failed","response":{"error":{"message":"模型不可用"}}}\n\n'
            ).encode(),
            headers={"content-type": "text/event-stream"},
        )

    provider = ChatResponsesProvider(
        provider_id="resp", base_url="http://stub.local/v1", api_key="sk"
    )
    original = httpx.AsyncClient

    def factory(*args, **kwargs):  # noqa: ANN001
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(*args, **kwargs)

    httpx.AsyncClient = factory  # type: ignore[misc]
    try:
        with pytest.raises(ModelError) as exc:
            async for _ in provider.stream(_request()):
                pass
    finally:
        httpx.AsyncClient = original  # type: ignore[misc]
    assert "模型不可用" in exc.value.message


# ---------------------------------------------------------------- 额度拦截


class _MemoryQuota:
    """内存版额度，避免为这个断言去连数据库。"""

    def __init__(self, used: dict[str, int]) -> None:
        self._used = used
        self.recorded: list[tuple[str, int, bool]] = []

    def today(self):  # noqa: ANN201
        return local_day()

    async def used(self, provider_id: str) -> int:
        return self._used.get(provider_id, 0)

    async def record(self, provider_id: str, tokens: int, *, estimated: bool = False) -> int:
        self.recorded.append((provider_id, tokens, estimated))
        self._used[provider_id] = self._used.get(provider_id, 0) + tokens
        return self._used[provider_id]


def _config(*, daily_limit: int | None, provider_id: str = "fake") -> EffectiveConfig:
    return EffectiveConfig(
        revision=1,
        providers={
            provider_id: ProviderConfig(
                provider_id=provider_id,
                kind="fake",
                enabled=True,
                extra={"chunk_delay": 0.001},
                daily_token_limit=daily_limit,
            )
        },
        profiles={
            "dialogue.fast": ProfileBinding(
                profile="dialogue.fast",
                provider_id=provider_id,
                model_id="deterministic",
            )
        },
        test_mode=True,
    )


@pytest.mark.asyncio
async def test_exhausted_quota_blocks_the_call() -> None:
    """额度用完时**不调用 provider**，并给出可执行提示。"""
    quota = _MemoryQuota({"fake": 200000})
    router = ModelRouter(
        ProviderRegistry(lambda _pid: None),  # type: ignore[arg-type]
        quota,  # type: ignore[arg-type]
    )
    with pytest.raises(ModelError) as exc:
        async for _ in router.stream(_config(daily_limit=200000), profile="dialogue.fast", messages=[]):
            pass
    assert exc.value.kind is ModelErrorKind.quota_exhausted
    assert exc.value.retriable is False
    assert "额度" in exc.value.message


@pytest.mark.asyncio
async def test_unlimited_quota_does_not_block() -> None:
    """-1（不限制）时哪怕用量很大也照常调用。"""
    quota = _MemoryQuota({"fake": 10_000_000})
    router = ModelRouter(
        ProviderRegistry(lambda _pid: None),  # type: ignore[arg-type]
        quota,  # type: ignore[arg-type]
    )
    text = ""
    async for chunk, _outcome in router.stream(
        _config(daily_limit=-1), profile="dialogue.fast", messages=[]
    ):
        text += chunk.delta
    assert text, "不限制额度时应该正常拿到回复"


@pytest.mark.asyncio
async def test_usage_is_recorded_after_a_successful_call() -> None:
    """调用成功后必须记账；fake provider 没有 usage 回执，应记成估算值。"""
    quota = _MemoryQuota({})
    router = ModelRouter(
        ProviderRegistry(lambda _pid: None),  # type: ignore[arg-type]
        quota,  # type: ignore[arg-type]
    )
    async for _chunk, _outcome in router.stream(
        _config(daily_limit=200000), profile="dialogue.fast", messages=[]
    ):
        pass
    assert quota.recorded, "成功调用之后应该有记账"
    provider_id, tokens, estimated = quota.recorded[0]
    assert provider_id == "fake"
    assert tokens > 0
    # fake provider 不返回 usage，所以这条账是估算的——界面要能说清这点。
    assert estimated is True


def test_quota_store_roundtrip_uses_local_day() -> None:
    """``local_day`` 用本地日历日：东八区早上八点仍是"今天"。"""
    from datetime import UTC, datetime

    # 东八区 08:30 的同一时刻：按 UTC 算还是前一天，按上海算已经是 6 号。
    moment = datetime(2026, 11, 5, 20, 30, tzinfo=UTC)  # = 东八区 11-06 04:30
    assert local_day(moment, "Asia/Shanghai").isoformat() == "2026-11-06"
    assert moment.astimezone(UTC).date().isoformat() == "2026-11-05"

    # 非法时区名不能让记账崩掉：退回系统本地时区。
    assert local_day(moment, "Not/AZone") is not None


@pytest.mark.asyncio
async def test_quota_survives_a_failing_write() -> None:
    """写库失败时额度判断仍然生效。

    这是实测抓到的缺陷：sqlite 下写账和本轮对话的事务撞锁，记账失败被
    静默吞掉，于是"今日已用"永远是 0，额度形同虚设——而回复看起来一切正常。
    现在增量先进内存，落库失败只记一条 warning。
    """

    class _BrokenSession:
        async def __aenter__(self):  # noqa: ANN204
            raise RuntimeError("database is locked")

        async def __aexit__(self, *args):  # noqa: ANN002, ANN003
            return False

    class _BrokenFactory:
        def __call__(self):  # noqa: ANN204
            return _BrokenSession()

    store = TokenQuotaStore(_BrokenFactory())  # type: ignore[arg-type]
    await store.record("p1", 500)
    # 落库失败，但内存里的账已经在：这一家应当立刻被算作"已用 500"。
    assert await store.used("p1") == 500
    snapshot = await store.snapshot(["p1"])
    assert snapshot["p1"]["used_tokens"] == 500
    await store.aclose()


def test_token_quota_store_accepts_factory() -> None:
    """构造签名稳定：app 与 worker 都按 (session_factory, timezone) 构造它。"""
    import inspect

    params = inspect.signature(TokenQuotaStore.__init__).parameters
    assert "session_factory" in params
    assert "timezone" in params


# ---------------------------------------------------------------- 记账返回值


class _QuotaRow:
    """一张 `provider_token_usage` 行（够用就好）。"""

    def __init__(self) -> None:
        self.tokens = 0
        self.estimated = False
        self.updated_at = None


class _QuotaResult:
    def __init__(self, row: _QuotaRow | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> _QuotaRow | None:
        return self._row


class _QuotaSession:
    def __init__(self, db: dict) -> None:
        self._db = db
        self._pending_row: _QuotaRow | None = None

    async def __aenter__(self):  # noqa: ANN204
        return self

    async def __aexit__(self, *args):  # noqa: ANN002, ANN003
        return False

    async def execute(self, stmt):  # noqa: ANN001, ANN201
        return _QuotaResult(self._db["row"])

    def add(self, row):  # noqa: ANN001
        self._pending_row = row

    async def commit(self) -> None:
        if self._pending_row is not None:
            self._db["row"] = self._pending_row
            self._pending_row = None


class _QuotaFactory:
    def __init__(self, db: dict) -> None:
        self._db = db

    def __call__(self):  # noqa: ANN204
        return _QuotaSession(self._db)


@pytest.mark.asyncio
async def test_record_returns_running_total_after_a_successful_write() -> None:
    """``record()`` 成功落库后必须返回**当天累计**，且不能抛异常。

    实测缺陷（2026-11-07）：``_flush`` 成功时会把这一笔从内存里扣掉、扣到 0
    就删键，紧接着的 ``self._pending[key]`` 于是抛 ``KeyError``。后果不是
    "账不准"（库已经写进去了），而是**每一次成功的生成都往日志里扔一条**
    ``token 用量记账失败（x）：('x', date(...))``——一条假警报。运维看到它
    会去查数据库，而那里一切正常；真正的失败反而被这句噪声盖住。
    """
    db: dict = {"row": None}
    store = TokenQuotaStore(_QuotaFactory(db))  # type: ignore[arg-type]

    assert await store.record("p1", 300) == 300
    assert await store.record("p1", 200) == 500
    # 内存里已经没有待写增量了（都写进去了），读数仍然对
    assert store._pending == {}
    assert await store.used("p1") == 500
    # 收掉后台补写任务：否则 pytest 退出时会报 "Task was destroyed"
    await store.aclose()
