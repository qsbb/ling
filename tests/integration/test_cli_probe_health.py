"""安装器声明的探活路径在真实 PostgreSQL 应用中保持免鉴权。"""

from deploy.cli.ling import HTTP_PROBE_PATHS

from .conftest import TestEnv


async def test_declared_http_probe_paths_are_unauthenticated_on_real_app(web_env: TestEnv) -> None:
    assert HTTP_PROBE_PATHS, "探活路径表不能是空的"
    for probe in HTTP_PROBE_PATHS:
        response = await web_env.client.get(probe)
        assert response.status_code not in (401, 403), (
            f"探活 {probe} 需要鉴权（{response.status_code}）：安装/状态/诊断"
            f"不持有用户凭据，这样的探活永远失败。响应：{response.text[:200]}"
        )
        assert response.status_code == 200, (probe, response.status_code, response.text[:200])
    version = await web_env.client.get("/v1/version")
    assert version.status_code == 401, version.text
    assert version.json()["error"]["code"] == "unauthorized"


async def test_v1_version_still_requires_auth(web_env: TestEnv) -> None:
    for path in (
        "/v1/version",
        "/v1/version/check-release",
        "/v1/version/update",
        "/v1/version/image-update",
        "/v1/health/model",
    ):
        response = await web_env.client.get(path)
        assert response.status_code == 401, (path, response.status_code, response.text[:200])
    assert (await web_env.client.get("/healthz")).status_code == 200
