"""版本检查的纯逻辑与固定上游地址。

这些测试不访问真实 GitHub：用假的 AsyncClient 验证 URL、超时与各种失败
都被收敛为稳定状态，且不会把异常文本带出去。
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from ling.api.routes import version as version_routes


class _FakeClient:
    """最小 AsyncClient 替身：记录构造参数与请求 URL。"""

    last_kwargs: dict[str, object] = {}
    last_url = ""

    def __init__(self, **kwargs: object) -> None:
        type(self).last_kwargs = kwargs
        self._response: httpx.Response | None = getattr(type(self), "response", None)
        self._error: Exception | None = getattr(type(self), "error", None)

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str) -> httpx.Response:
        type(self).last_url = url
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    response: httpx.Response | None = None,
    error: Exception | None = None,
) -> type[_FakeClient]:
    _FakeClient.response = response  # type: ignore[attr-defined]
    _FakeClient.error = error  # type: ignore[attr-defined]
    monkeypatch.setattr(version_routes.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


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
    _patch_client(monkeypatch, response=httpx.Response(status_code, json={}))
    assert await version_routes._fetch_latest_release() == ("unavailable", None)


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
