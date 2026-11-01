"""版本检查的纯逻辑与固定上游地址。

这些测试不访问真实 GitHub：用假的 AsyncClient 验证 URL、超时与各种失败
都被收敛为稳定状态，且不会把异常文本带出去。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator

import httpx
import pytest

from ling.api.routes import version as version_routes

# 合法的 release payload：错误响应用它做 body，才能证明“非 2xx 的 body 不会被
# 当作 release 解析”（若 body 是 `{}`，实现把状态门放宽也只会因为缺 tag_name 失败，
# 断言就没有判别力）。
_RELEASE_BODY = {
    "tag_name": "v9.9.9",
    "html_url": "https://github.com/qsbb/ling/releases/tag/v9.9.9",
    "published_at": "2026-10-30T00:00:00Z",
}


class _ChunkStream(httpx.AsyncByteStream):
    """按给定分片逐块交出的异步字节流（真实 ``httpx`` 流式语义）。

    ``served`` 记录已经被取走的分片数，用来观测“读到哪一块就停了”。
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.served = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.served += 1
            yield chunk


class _StreamedBody:
    """流式响应替身：只暴露真实 streamed response 能用的读接口。

    真实 ``httpx.Response``（``stream=`` 构造）在未 ``read()`` 之前访问
    ``.content`` 会抛 ``httpx.ResponseNotRead``；这里复现同一语义，并记录调用的是
    哪个读方法。因此“必须走 ``aiter_bytes``”和“非 2xx 不读 body”都是可观测的。
    """

    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.read_calls: list[str] = []

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def content(self) -> bytes:
        self.read_calls.append("content")
        # 真实 httpx 的 ResponseNotRead 不接受消息参数，语义见类名。
        raise httpx.ResponseNotRead()

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        self.read_calls.append("aiter_bytes")
        return self._response.aiter_bytes()

    def aiter_lines(self) -> AsyncIterator[str]:
        self.read_calls.append("aiter_lines")
        return self._response.aiter_lines()


def _make_streamed(
    chunks: list[bytes], *, status_code: int = 200
) -> tuple[_StreamedBody, _ChunkStream, httpx.Response]:
    """把分片包成“真实流式响应 + 记录读取行为的探针”。"""
    stream = _ChunkStream(chunks)
    response = httpx.Response(
        status_code, stream=stream, headers={"content-type": "application/json"}
    )
    return _StreamedBody(response), stream, response


class _FakeStream:
    """``client.stream(...)`` 的替身：和 httpx 一样是**异步上下文管理器**。

    httpx 的 ``stream()`` 本身是同步方法，只返回上下文管理器；真正发请求是在
    ``__aenter__``，连接/超时错误也从那里抛出。这里保持同样的时序：错误在进入
    上下文时抛，响应对象则原样交回，``aiter_bytes`` / ``aiter_lines`` 用的是真实
    ``httpx.Response`` 的实现。
    """

    def __init__(
        self, response: httpx.Response | _StreamedBody | None, error: Exception | None
    ) -> None:
        self._response = response
        self._error = error

    async def __aenter__(self) -> httpx.Response | _StreamedBody:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeClient:
    """最小 AsyncClient 替身：记录构造参数与请求 URL，只提供流式接口。

    生产代码用 ``client.stream("GET", ...)`` 按 chunk 限长读取响应体；这里刻意
    **不**提供 ``get``，这样一旦有人把流式读取改回缓冲式 ``get``，用例会立刻
    变红，而不是悄悄通过。
    """

    last_kwargs: dict[str, object] = {}
    last_url = ""
    last_method = ""

    def __init__(self, **kwargs: object) -> None:
        type(self).last_kwargs = kwargs
        self._response: httpx.Response | _StreamedBody | None = getattr(
            type(self), "response", None
        )
        self._error: Exception | None = getattr(type(self), "error", None)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def stream(self, method: str, url: str) -> _FakeStream:
        type(self).last_method = method
        type(self).last_url = url
        return _FakeStream(self._response, self._error)


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: httpx.Response | _StreamedBody | None = None,
    error: Exception | None = None,
) -> type[_FakeClient]:
    _FakeClient.response = response  # type: ignore[attr-defined]
    _FakeClient.error = error  # type: ignore[attr-defined]
    monkeypatch.setattr(version_routes.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _release_body_json() -> bytes:
    """合法 release payload 的 JSON 字节（带多字节文本，便于在 UTF-8 中间切片）。"""
    payload = dict(_RELEASE_BODY, body="更新说明：修复多字节文本被截断的问题")
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _three_chunks(raw: bytes) -> list[bytes]:
    third = len(raw) // 3
    chunks = [raw[:third], raw[third : 2 * third], raw[2 * third :]]
    assert all(chunks)
    return chunks


def _padded_release_body(total: int) -> bytes:
    """构造**恰好** ``total`` 字节、且仍是合法 release JSON 的响应体。"""
    prefix = b'{"tag_name": "v9.9.9", "pad": "'
    suffix = b'"}'
    pad_len = total - len(prefix) - len(suffix)
    assert pad_len > 0
    raw = prefix + b"x" * pad_len + suffix
    assert len(raw) == total
    return raw


@pytest.mark.parametrize(
    ("current", "latest", "expected"),
    [
        ("0.1.0", "v0.2.0", True),
        ("v1.2.3", "v1.2.3", False),
        ("1.2.3", "1.2.4", True),
        ("1.2.3", "2.0.0", True),
        ("2.0.0", "1.9.9", False),
        ("1.0.0", "1.0.0-rc.1", False),
        ("1.0.0-rc.1", "1.0.0", True),
        ("1.0.0-alpha", "1.0.0-beta", True),
        ("1.0.0-alpha.1", "1.0.0-alpha.2", True),
        ("1.0.0-alpha.10", "1.0.0-alpha.2", False),
        ("1.0.1", "v1.0.1+build.5", False),
    ],
)
def test_is_newer_release_handles_semver(
    current: str, latest: str, expected: bool
) -> None:
    assert version_routes.is_newer_release(current, latest) is expected


@pytest.mark.parametrize("value", ["main", "latest", "release-0.2.0", "0.2", "1.2.3.4", "v1.2.x", ""])
def test_unrecognized_versions_are_not_treated_as_upgrades(value: str) -> None:
    assert version_routes.is_newer_release("0.1.0", value) is None
    assert version_routes.is_newer_release(value, "9.9.9") is None


def test_current_version_prefers_installed_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version_routes.importlib_metadata, "version", lambda _name: "9.9.9")
    assert version_routes.current_version() == "9.9.9"


def test_current_version_falls_back_to_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> str:
        raise version_routes.importlib_metadata.PackageNotFoundError(_name)

    monkeypatch.setattr(version_routes.importlib_metadata, "version", missing)
    from ling import __version__

    assert version_routes.current_version() == __version__


def test_build_commit_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LING_COMMIT", raising=False)
    assert version_routes.build_commit() is None
    monkeypatch.setenv("LING_COMMIT", "  abc1234  ")
    assert version_routes.build_commit() == "abc1234"


def test_web_manifest_digest_reads_manifest(tmp_path) -> None:
    payload = {"schema": 1, "release_inputs": {}, "dist_files": {"index.html": "a" * 64}}
    raw = json.dumps(payload).encode("utf-8")
    (tmp_path / ".ling-web-manifest.json").write_bytes(raw)
    assert version_routes.web_manifest_digest(tmp_path) == hashlib.sha256(raw).hexdigest()


def test_web_manifest_digest_absent_or_invalid(tmp_path) -> None:
    assert version_routes.web_manifest_digest(tmp_path) is None
    (tmp_path / ".ling-web-manifest.json").write_text("not json", encoding="utf-8")
    assert version_routes.web_manifest_digest(tmp_path) is None
    (tmp_path / ".ling-web-manifest.json").write_text(
        json.dumps({"schema": 2}), encoding="utf-8"
    )
    assert version_routes.web_manifest_digest(tmp_path) is None


async def test_fetch_uses_fixed_github_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    response = httpx.Response(
        200,
        json={
            "tag_name": "v0.2.0",
            "published_at": "2026-10-30T00:00:00Z",
            "html_url": "https://github.com/qsbb/ling/releases/tag/v0.2.0",
        },
    )
    fake = _patch_client(monkeypatch, response=response)

    status, payload = await version_routes._fetch_latest_release()

    assert status == "ok"
    assert payload is not None
    assert payload["tag_name"] == "v0.2.0"
    assert fake.last_method == "GET"
    assert fake.last_url == "https://api.github.com/repos/qsbb/ling/releases/latest"
    assert fake.last_kwargs["follow_redirects"] is False
    assert fake.last_kwargs["headers"] == {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ling-version-check",
    }


async def test_fetch_404_is_no_release(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, response=httpx.Response(404, json={"message": "Not Found"}))
    assert await version_routes._fetch_latest_release() == ("no_release", None)


@pytest.mark.parametrize("status_code", [403, 429, 500])
async def test_fetch_http_error_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    # 错误响应体故意是**合法 release JSON**：只有“非 2xx 根本不进解析”才能收敛成
    # unavailable；把状态门放宽后这里会变成 ("ok", payload)，用例立刻变红。
    raw = _release_body_json()
    assert json.loads(raw)["tag_name"] == "v9.9.9"
    probe, stream, response = _make_streamed(_three_chunks(raw), status_code=status_code)
    _patch_client(monkeypatch, response=probe)

    result = await version_routes._fetch_latest_release()

    assert result == ("unavailable", None)
    assert result[0] == "unavailable"  # 不是 no_release，也不是 ok
    assert probe.read_calls == []  # 没有走 aiter_bytes / aiter_lines
    assert stream.served == 0  # 一个 chunk 都没被取走
    assert response.is_stream_consumed is False


async def test_fetch_reassembles_chunked_body_across_utf8_and_string_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """body 分 3 块、切点落在多字节 UTF-8 与字符串中间时也必须拼回完整 JSON。"""
    payload = {
        "tag_name": "v0.2.0",
        "html_url": "https://github.com/qsbb/ling/releases/tag/v0.2.0",
        "published_at": "2026-10-30T00:00:00Z",
        "body": "更新说明：修复多字节文本被截断的问题",
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    cjk = "更".encode()
    assert len(cjk) == 3
    cut_string = raw.index(b'"v0.2.0"') + 4  # tag 值 "v0.2.0" 中间（字符串中间）
    cut_utf8 = raw.index(cjk) + 1  # "更" 的 3 字节中间（多字节 UTF-8 中间）
    assert 0x80 <= raw[cut_utf8] < 0xC0  # 切点确实是 UTF-8 续字节
    assert cut_string < cut_utf8 < len(raw)

    chunks = [raw[:cut_string], raw[cut_string:cut_utf8], raw[cut_utf8:]]
    assert b"".join(chunks) == raw
    assert len(chunks) == 3
    probe, stream, _ = _make_streamed(chunks)
    _patch_client(monkeypatch, response=probe)

    result = await version_routes._fetch_latest_release()

    assert result == ("ok", payload)
    assert probe.read_calls == ["aiter_bytes"]
    assert stream.served == len(chunks)


async def test_fetch_reads_body_only_through_streamed_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fetch 只能经 ``aiter_bytes`` 读 body；``_StreamedBody.content`` 会像真实流式
    响应那样抛 ``ResponseNotRead``，把 ``_read_limited_body(response)`` 改回
    ``response.content`` 会让本用例直接失败。"""
    chunks = [
        b'{"tag_name": "v0.3.',
        b'0", "published_at": "2026-10-30T00:00:00Z", ',
        b'"html_url": "https://github.com/qsbb/ling/releases/tag/v0.3.0"}',
    ]
    probe, stream, _ = _make_streamed(chunks)
    _patch_client(monkeypatch, response=probe)

    status, payload = await version_routes._fetch_latest_release()

    assert status == "ok"
    assert payload is not None
    assert payload["tag_name"] == "v0.3.0"
    assert probe.read_calls == ["aiter_bytes"]  # 恰好一次，且没有 aiter_lines/content
    assert stream.served == len(chunks) == 3


async def test_read_limited_body_returns_all_chunks_in_order() -> None:
    """``_read_limited_body`` 必须按序拼接**每一块**（不是只交第一块）。"""
    chunks = [
        b'{"tag_name"',
        b': "v0.2.0", "published_at": ',
        b'"2026-10-30T00:00:00Z"}',
    ]
    probe, stream, _ = _make_streamed(chunks)

    raw = await version_routes._read_limited_body(probe)

    assert raw == b"".join(chunks)
    assert stream.served == len(chunks)


async def test_fetch_accepts_body_exactly_at_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """恰好 ``_MAX_RELEASE_BODY_BYTES`` 字节仍可解析：上限是“超过才拒”。"""
    limit = version_routes._MAX_RELEASE_BODY_BYTES
    raw = _padded_release_body(limit)
    assert len(raw) == limit
    probe, stream, _ = _make_streamed([raw])
    _patch_client(monkeypatch, response=probe)

    status, payload = await version_routes._fetch_latest_release()

    assert status == "ok"
    assert payload is not None
    assert payload["tag_name"] == "v9.9.9"
    assert stream.served == 1


async def test_fetch_rejects_body_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 ``_MAX_RELEASE_BODY_BYTES`` 的 body 判“不可核验”，且不读完整个响应。"""
    limit = version_routes._MAX_RELEASE_BODY_BYTES
    raw = _padded_release_body(limit + 128 * 1024)
    chunks = [raw[i : i + 64 * 1024] for i in range(0, len(raw), 64 * 1024)]
    assert len(raw) > limit
    assert len(chunks) > 1
    probe, stream, _ = _make_streamed(chunks)
    _patch_client(monkeypatch, response=probe)

    result = await version_routes._fetch_latest_release()

    assert result == ("unavailable", None)
    assert probe.read_calls == ["aiter_bytes"]
    assert stream.served < len(chunks)  # 超限立刻中止，不把剩余内容读进内存


async def test_read_limited_body_returns_none_over_limit() -> None:
    """直接锁 ``_read_limited_body`` 的超限语义（返回 None，而不是截断后的字节）。"""
    limit = version_routes._MAX_RELEASE_BODY_BYTES
    raw = _padded_release_body(limit + 2 * 64 * 1024)
    chunks = [raw[i : i + 64 * 1024] for i in range(0, len(raw), 64 * 1024)]
    probe, stream, _ = _make_streamed(chunks)

    assert await version_routes._read_limited_body(probe) is None
    assert stream.served < len(chunks)


async def test_fetch_bad_json_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        response=httpx.Response(
            200, content=b"not-json", headers={"content-type": "application/json"}
        ),
    )
    assert await version_routes._fetch_latest_release() == ("unavailable", None)


@pytest.mark.parametrize("payload", [[], {}, {"tag_name": ""}, {"tag_name": 123}])
async def test_fetch_invalid_payload_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    _patch_client(monkeypatch, response=httpx.Response(200, json=payload))
    assert await version_routes._fetch_latest_release() == ("unavailable", None)


async def test_fetch_network_error_is_unavailable_without_exception_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_client(monkeypatch, error=httpx.ConnectError("secret token in error"))
    assert await version_routes._fetch_latest_release() == ("unavailable", None)


async def test_fetch_timeout_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, error=httpx.ReadTimeout("timed out"))
    assert await version_routes._fetch_latest_release() == ("unavailable", None)


def test_release_url_is_restricted_to_this_repository() -> None:
    assert (
        version_routes._safe_release_url(
            "https://github.com/qsbb/ling/releases/tag/v0.2.0"
        )
        == "https://github.com/qsbb/ling/releases/tag/v0.2.0"
    )
    assert version_routes._safe_release_url("http://evil.example/release") is None
    assert version_routes._safe_release_url("javascript:alert(1)") is None
    assert version_routes._safe_release_url(123) is None
