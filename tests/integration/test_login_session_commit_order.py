"""A login cookie must be usable as soon as the login response is sent."""

from __future__ import annotations

from http.cookies import SimpleCookie

import pytest
from httpx import ASGITransport, AsyncClient

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


async def test_login_cookie_is_usable_before_response_scope_finishes(
    logged_in: TestEnv,
) -> None:
    app = logged_in.app
    cookie_name = logged_in.settings.cookie_name
    dashboard_statuses: list[int] = []

    async def interleaved_app(scope, receive, send):  # noqa: ANN001
        if scope.get("path") != "/v1/auth/login":
            await app(scope, receive, send)
            return

        login_cookie: str | None = None

        async def send_and_interleave(message):  # noqa: ANN001
            nonlocal login_cookie
            await send(message)

            if message["type"] == "http.response.start":
                cookies = SimpleCookie()
                for key, value in message.get("headers", []):
                    if key.lower() == b"set-cookie":
                        cookies.load(value.decode("latin-1"))
                morsel = cookies.get(cookie_name)
                login_cookie = morsel.value if morsel else None

            if message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                assert login_cookie, "login response did not set its session cookie"
                async with AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url="http://testserver",
                ) as followup:
                    dashboard = await followup.get(
                        "/v1/dashboard",
                        headers={"cookie": f"{cookie_name}={login_cookie}"},
                    )
                dashboard_statuses.append(dashboard.status_code)

        await app(scope, receive, send_and_interleave)

    async with AsyncClient(
        transport=ASGITransport(app=interleaved_app),
        base_url="http://testserver",
    ) as client:
        login = await client.post(
            "/v1/auth/login",
            json={"username": "tester", "password": "tester-password"},
        )

    assert login.status_code == 200, login.text
    assert dashboard_statuses == [200], (
        "a dashboard request sent immediately after the login response body "
        f"received HTTP {dashboard_statuses!r}; the session must be committed "
        "before the response makes its cookie usable"
    )
