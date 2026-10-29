"""提炼职责的行为测试：`memory.extract` 绑定后必须真的被用上。

## 为什么需要这个文件

`worker._extract_candidates` 原来硬编码 `profile="dialogue.fast"`，
于是设置页里的 `memory.extract` 是个**幽灵**：绑了没有任何效果，因为
从来没人按这个职责请求模型。用户以为自己换了提炼模型，其实没换。

静态审计（`tests/unit/test_model_roles.py`）抓不到它，因为
`@handler("memory.extract")` 里的 **outbox topic 名**和**模型职责名**
撞了同一个字符串——"字符串出现过"被判成了"职责被请求过"。

所以这条用**行为**验：给一个记录 profile 的假路由，看它实际按哪个职责
去请求。这比正则可靠。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


class _RecordingRouter:
    """记录每次请求用的 profile。"""

    def __init__(self) -> None:
        self.profiles: list[str] = []

    async def stream(self, config, *, profile, messages, **kw):  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome
        from ling.ports.model import ModelChunk

        self.profiles.append(profile)
        payload = '[{"slot": "health", "value": "对花生过敏", "confidence": 0.9, "attribution": "user", "reason": "用户明说"}]'
        yield ModelChunk(delta=payload), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=payload)


def _bind(monkeypatch: pytest.MonkeyPatch, *, bind_extract: bool) -> None:
    """让 ConfigService.effective() 返回"绑了 / 没绑 memory.extract"的配置。"""
    from ling.settings import service as settings_service

    base = {"dialogue.fast": SimpleNamespace(profile="dialogue.fast", enabled=True)}

    async def fake_effective(self):  # noqa: ANN001, ANN202
        profiles = dict(base)
        if bind_extract:
            profiles["memory.extract"] = SimpleNamespace(
                profile="memory.extract", enabled=True
            )
        return SimpleNamespace(
            profiles=profiles,
            dialogue=SimpleNamespace(),
        )

    monkeypatch.setattr(settings_service.ConfigService, "effective", fake_effective)


async def test_extract_uses_memory_extract_when_bound(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """绑了 memory.extract，就应该按这个职责请求模型。

    修之前这里永远是 dialogue.fast——设置页那个绑定项设了等于没设。
    """
    import ling.worker as w

    _bind(monkeypatch, bind_extract=True)
    router = _RecordingRouter()
    monkeypatch.setattr(w, "_MEMORY_ROUTER", router)

    await w._extract_candidates(  # noqa: SLF001
        user_text="我对花生过敏", actor_text="记住了", is_group=False
    )

    assert router.profiles == ["memory.extract"], (
        f"绑定了 memory.extract 却按 {router.profiles} 请求"
    )


async def test_extract_falls_back_to_dialogue_fast_when_unbound(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """没绑专用模型时退回主对话模型，而不是整段失效。

    提炼质量差点好过完全不提炼。
    """
    import ling.worker as w

    _bind(monkeypatch, bind_extract=False)
    router = _RecordingRouter()
    monkeypatch.setattr(w, "_MEMORY_ROUTER", router)

    await w._extract_candidates(  # noqa: SLF001
        user_text="我对花生过敏", actor_text="记住了", is_group=False
    )

    assert router.profiles == ["dialogue.fast"], (
        f"没绑专用模型时应退回 dialogue.fast，实际 {router.profiles}"
    )


async def test_extract_still_returns_candidates(
    logged_in: TestEnv, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ARG001
    """换了职责不影响返回结果——别把接线修好了、功能弄坏了。"""
    import ling.worker as w

    _bind(monkeypatch, bind_extract=True)
    monkeypatch.setattr(w, "_MEMORY_ROUTER", _RecordingRouter())

    cands = await w._extract_candidates(  # noqa: SLF001
        user_text="我对花生过敏", actor_text="记住了", is_group=False
    )
    assert len(cands) == 1
    assert getattr(cands[0], "slot", None) == "health"
