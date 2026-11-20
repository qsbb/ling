from collections.abc import AsyncIterator, Callable

from ling.models.registry import ProviderRegistry
from ling.models.router import ModelRouter
from ling.ports.model import ModelChunk, ModelRequest, ProviderKind
from tests.integration.conftest import TestEnv

FIRST = "这是第一段。"
SECOND = "这是第二段。"


class ScriptedProvider:
    def __init__(
        self,
        script: Callable[[ModelRequest], AsyncIterator[ModelChunk]],
        *,
        native_tools: bool = False,
    ) -> None:
        self.script = script
        self.requests: list[ModelRequest] = []
        self.kind = ProviderKind.openai_compatible if native_tools else ProviderKind.fake
        self.supports_native_tools = native_tools

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        self.requests.append(request)
        async for chunk in self.script(request):
            yield chunk


class ScriptedRegistry(ProviderRegistry):
    def __init__(self, provider: ScriptedProvider) -> None:
        super().__init__(lambda _: None)
        self.provider = provider

    def get(self, cfg):
        return self.provider


def install_provider(env: TestEnv, provider: ScriptedProvider) -> ModelRouter:
    router = ModelRouter(ScriptedRegistry(provider))
    env.app.state.router = router
    return router


async def new_window(env: TestEnv, title: str = "WP-00 sample") -> str:
    response = await env.client.post("/v1/sessions", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def publish(env: TestEnv, config: dict) -> None:
    draft = await env.client.put("/v1/settings/draft", json=config)
    assert draft.status_code == 200, draft.text
    response = await env.client.post(
        "/v1/settings/publish", json={"revision": draft.json()["revision"]}
    )
    assert response.status_code == 200, response.text
