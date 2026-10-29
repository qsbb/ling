"""媒体描述链路：把图交给视觉模型，但**绝不编造**。

阶段 6 的验收里有两条在这里落地：

1. 描述是推断，不是事实（`fact_eligible` 恒 False）
2. 授权在描述时重新检查（中途撤权就不许再写）

第三条是本模块自己的：**拿不到就留空**。模型不可用、返回空、抛错，
一律保持 `description=None` 让它下次重试。写一句"（无法描述）"会把
"没看到"伪装成"看到了但没内容"——既污染证据，又让重试永远发生不了。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ling.domains.consent.service import ConsentService
from ling.domains.media.describe import (
    MediaDescribeError,
    MediaDescriber,
)
from ling.domains.media.service import (
    INBOUND_SCOPE,
    ORIGIN_HUMAN,
    MediaEvidenceService,
)
from ling.infra.db import session_scope
from ling.ports.model import ModelChunk, ModelError, ModelErrorKind
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_ling"
SUBJECT = "subj_describe_test"
WINDOW = "win_describe_a"


class _StubConfig:
    async def effective(self):  # noqa: ANN202
        from types import SimpleNamespace

        return SimpleNamespace(revision=1)


class _StubRouter:
    """按预设产出 chunk；也可抛 ModelError 模拟"没绑定"。"""

    def __init__(
        self,
        *,
        text: str = "一张桌面照片，上面有一只橘猫",
        error: ModelError | None = None,
        seen: list | None = None,
    ) -> None:
        self._text = text
        self._error = error
        self.seen = seen if seen is not None else []

    async def stream(self, config, *, profile, messages, **kwargs) -> AsyncIterator:  # noqa: ANN001, ANN003
        from ling.models.router import StreamOutcome

        self.seen.append({"profile": profile, "messages": messages})
        if self._error is not None:
            raise self._error
        if self._text:
            yield ModelChunk(delta=self._text), StreamOutcome()
        yield ModelChunk(done=True), StreamOutcome(text=self._text)


async def _fetch_ok(media_ref: str) -> tuple[bytes, str]:
    return b"\x89PNG\r\n\x1a\nfake", "image/png"


async def _grant(granted: bool = True) -> None:
    async with session_scope() as session:
        svc = ConsentService(session)
        if granted:
            await svc.grant(subject_id=SUBJECT, scope=INBOUND_SCOPE)
        else:
            await svc.revoke(subject_id=SUBJECT, scope=INBOUND_SCOPE)


async def _clean() -> None:
    from sqlalchemy import delete

    from ling.infra.models import ConsentRule, MediaEvidenceRow

    async with session_scope() as session:
        await session.execute(
            delete(MediaEvidenceRow).where(MediaEvidenceRow.actor_id == ACTOR)
        )
        await session.execute(
            delete(ConsentRule).where(ConsentRule.subject_id.like("subj_describe%"))
        )


@pytest.fixture(autouse=True)
async def _reset(logged_in: TestEnv) -> None:  # noqa: ARG001
    await _clean()
    yield
    await _clean()


async def _make_evidence(*, ref: str = "obj/pic.jpg") -> str:
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.record(subject_id=SUBJECT, window_id=WINDOW, media_ref=ref)
        return view.evidence_id


# ---------------------------------------------------------------- 成功路径


async def test_describe_writes_model_inference(logged_in: TestEnv) -> None:  # noqa: ARG001
    evidence_id = await _make_evidence()
    router = _StubRouter()

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        evidence = await svc.get(evidence_id, window_id=WINDOW)
        assert evidence is not None
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=router, config_reader=_StubConfig()
        )
        text = await describer.describe_one(evidence)

    assert text == "一张桌面照片，上面有一只橘猫"

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None
        assert view.description == text
        assert view.description_origin == "model_inference"
        assert view.fact_eligible is False, "模型描述被当成事实了"

        facts = await svc.fact_descriptions(window_id=WINDOW)
        assert facts == [], f"模型描述漏进了事实：{facts}"


async def test_describe_sends_image_to_vision_profile(logged_in: TestEnv) -> None:  # noqa: ARG001
    """图真的作为 image 段送到了 perception.vision，不是只发文字。"""
    evidence_id = await _make_evidence()
    router = _StubRouter()
    fetched: list[str] = []

    async def fetch(ref: str) -> tuple[bytes, str]:
        fetched.append(ref)
        return await _fetch_ok(ref)

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        evidence = await svc.get(evidence_id, window_id=WINDOW)
        describer = MediaDescriber(
            service=svc, fetch_media=fetch, router=router, config_reader=_StubConfig()
        )
        await describer.describe_one(evidence)  # type: ignore[arg-type]

    assert fetched == ["obj/pic.jpg"]
    assert router.seen, "没有调用模型"
    call = router.seen[0]
    assert call["profile"] == "perception.vision"
    message = call["messages"][0]
    assert len(message.images) == 1, "图没送进去"
    assert message.images[0].url.startswith("data:image/png;base64,")


async def test_already_described_is_not_redone(logged_in: TestEnv) -> None:  # noqa: ARG001
    evidence_id = await _make_evidence()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        await svc.describe(evidence_id, window_id=WINDOW, description="人给的说法", description_origin=ORIGIN_HUMAN)
        evidence = await svc.get(evidence_id, window_id=WINDOW)

        router = _StubRouter(text="模型想覆盖")
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=router, config_reader=_StubConfig()
        )
        text = await describer.describe_one(evidence)  # type: ignore[arg-type]

    assert text == "人给的说法", "已经有的描述被覆盖了"
    assert router.seen == [], "不该再调模型"


# ---------------------------------------------------------------- 绝不编造


async def test_empty_model_reply_leaves_description_empty(logged_in: TestEnv) -> None:  # noqa: ARG001
    """模型回了空串：留空待重试，不写占位符。"""
    evidence_id = await _make_evidence()
    router = _StubRouter(text="")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        evidence = await svc.get(evidence_id, window_id=WINDOW)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=router, config_reader=_StubConfig()
        )
        with pytest.raises(MediaDescribeError):
            await describer.describe_one(evidence)  # type: ignore[arg-type]

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None
        assert view.description is None, "空回复被写成了描述"
        # 仍在待办里，下次还能试
        assert len(await svc.pending(window_id=WINDOW)) == 1


async def test_unbound_vision_does_not_fabricate(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没绑定 perception.vision：报错留空，绝不退回 fake 编一段。"""
    evidence_id = await _make_evidence()
    router = _StubRouter(
        error=ModelError(
            ModelErrorKind.unsupported, "职责 perception.vision 尚未绑定模型"
        )
    )

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        evidence = await svc.get(evidence_id, window_id=WINDOW)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=router, config_reader=_StubConfig()
        )
        with pytest.raises(MediaDescribeError):
            await describer.describe_one(evidence)  # type: ignore[arg-type]

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None
        assert view.description is None


async def test_fetch_failure_leaves_description_empty(logged_in: TestEnv) -> None:  # noqa: ARG001
    evidence_id = await _make_evidence()

    async def fetch_fail(ref: str) -> tuple[bytes, str]:
        raise RuntimeError("网络断了")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        evidence = await svc.get(evidence_id, window_id=WINDOW)
        describer = MediaDescriber(
            service=svc, fetch_media=fetch_fail, router=_StubRouter(), config_reader=_StubConfig()
        )
        with pytest.raises(MediaDescribeError, match="下载失败"):
            await describer.describe_one(evidence)  # type: ignore[arg-type]

        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None and view.description is None


async def test_revoked_consent_blocks_description(logged_in: TestEnv) -> None:  # noqa: ARG001
    """记录后撤权：描述时重新检查，不许写。"""
    evidence_id = await _make_evidence()
    await _grant(granted=False)

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        evidence = await svc.get(evidence_id, window_id=WINDOW)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=_StubRouter(), config_reader=_StubConfig()
        )
        with pytest.raises(MediaDescribeError):
            await describer.describe_one(evidence)  # type: ignore[arg-type]
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None and view.description is None


# ---------------------------------------------------------------- 批量


async def test_describe_pending_continues_after_failure(logged_in: TestEnv) -> None:  # noqa: ARG001
    """一条失败不影响其余的——队列不能因为一张坏图卡住。"""
    await _make_evidence(ref="obj/bad.jpg")
    await _make_evidence(ref="obj/good.jpg")

    async def fetch(ref: str) -> tuple[bytes, str]:
        if "bad" in ref:
            raise RuntimeError("坏链")
        return await _fetch_ok(ref)

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=fetch, router=_StubRouter(), config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id=WINDOW)

    assert outcome.described == 1
    assert outcome.failed == 1
    assert outcome.last_error is not None

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        still = await svc.pending(window_id=WINDOW)
        assert len(still) == 1, "失败那条应该还在队列里"
        assert "bad" in still[0].media_ref


async def test_describe_pending_empty_queue(logged_in: TestEnv) -> None:  # noqa: ARG001
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=_StubRouter(), config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id="win_empty")
    assert outcome.described == 0 and outcome.failed == 0


# ---------------------------------------------------------------- 队列入口


async def test_windows_with_pending_lists_windows(logged_in: TestEnv) -> None:  # noqa: ARG001
    """后台任务需要知道"哪些窗口有活"，否则永远只能被动等某个窗口触发。"""
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        await svc.record(subject_id=SUBJECT, window_id="win_a", media_ref="obj/a.jpg")
        await svc.record(subject_id=SUBJECT, window_id="win_b", media_ref="obj/b.jpg")
        await svc.record(subject_id=SUBJECT, window_id="win_b", media_ref="obj/c.jpg")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        windows = await svc.windows_with_pending()
    assert windows == ["win_a", "win_b"], f"窗口列表不对：{windows}"


async def test_described_evidence_leaves_queue(logged_in: TestEnv) -> None:  # noqa: ARG001
    """描述过的窗口不该继续占用队列。"""
    evidence_id = await _make_evidence()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        evidence = await svc.get(evidence_id, window_id=WINDOW)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=_StubRouter(), config_reader=_StubConfig()
        )
        await describer.describe_one(evidence)  # type: ignore[arg-type]

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        assert await svc.windows_with_pending() == []


def test_worker_registers_media_describe() -> None:
    """处理器必须真的注册了，否则事件投进去没人接。"""
    from ling.worker import HANDLERS

    assert "media.describe" in HANDLERS
