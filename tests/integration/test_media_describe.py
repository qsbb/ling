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


async def test_media_ref_error_is_permanent_not_retriable(logged_in: TestEnv) -> None:  # noqa: ARG001
    """引用被拒（越界/超限/形式不支持）是**永久错误**，不能标成可重试。

    `MediaRefError` 的 docstring 明确要求上层区分"引用本身不合法（不该重试）"
    与"下载失败（可能重试）"。原实现把所有 fetch 异常统一包成
    `MediaDescribeError(retriable=True)`——于是越界引用每 60 秒被重试一次、
    永远重试不完（`pending` 只按 `description IS NULL` 过滤，捞得回来），
    日志里一条"下载失败"重复到把真问题淹掉。

    对照下一条用例：普通下载失败**仍然**是可重试的。两条一起证明
    "分成两类"真的落地了，而不是把所有失败一律标成永久。
    """
    from ling.domains.media.errors import MediaRefError

    evidence_id = await _make_evidence(ref="file:/etc/passwd")

    async def fetch_rejected(ref: str) -> tuple[bytes, str]:
        raise MediaRefError("file: 引用越界（不在媒体目录内）: /etc/passwd")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        evidence = await svc.get(evidence_id, window_id=WINDOW)
        describer = MediaDescriber(
            service=svc,
            fetch_media=fetch_rejected,
            router=_StubRouter(),
            config_reader=_StubConfig(),
        )
        with pytest.raises(MediaDescribeError) as exc:
            await describer.describe_one(evidence)  # type: ignore[arg-type]

    assert exc.value.retriable is False, "永久错误被标成了可重试"
    assert "引用" in str(exc.value)


async def test_transient_fetch_failure_stays_retriable(logged_in: TestEnv) -> None:  # noqa: ARG001
    """普通下载失败仍然可重试（不能因为上面那条把全部失败都标成永久）。"""
    evidence_id = await _make_evidence()

    async def fetch_fail(ref: str) -> tuple[bytes, str]:
        raise RuntimeError("网络断了")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        evidence = await svc.get(evidence_id, window_id=WINDOW)
        describer = MediaDescriber(
            service=svc, fetch_media=fetch_fail, router=_StubRouter(), config_reader=_StubConfig()
        )
        with pytest.raises(MediaDescribeError) as exc:
            await describer.describe_one(evidence)  # type: ignore[arg-type]

    assert exc.value.retriable is True, "临时故障被标成了永久错误（再也不会重试）"


async def test_describe_pending_counts_permanent_failures(logged_in: TestEnv) -> None:  # noqa: ARG001
    """`describe_pending` 把永久失败单独计数，调用方能据此区分两类失败。"""
    from ling.domains.media.errors import MediaRefError

    await _make_evidence(ref="file:/etc/passwd")
    await _make_evidence(ref="obj/net-down.jpg")

    async def fetch(ref: str) -> tuple[bytes, str]:
        if ref.startswith("file:"):
            raise MediaRefError("引用越界")
        raise RuntimeError("网络断了")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=fetch, router=_StubRouter(), config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id=WINDOW)

    assert outcome.failed == 2
    assert outcome.permanent_failures == 1, (
        f"永久失败计数不对：{outcome.permanent_failures}（可重试那条不该算进来）"
    )
    assert outcome.described == 0


async def test_permanent_failure_leaves_the_queue_for_real(logged_in: TestEnv) -> None:  # noqa: ARG001
    """**回归锁**：永久失败的引用必须真的离开待办队列，不是只多一个计数。

    这是 L-3 的核心。只把 `retriable` 设成 False 是没用的——那个标志原来
    没有任何读者，`pending()` 只按 `description IS NULL` 过滤。于是越界
    引用每 60 秒被重试一次、永远重试不完（`media.describe` 是周期任务）。

    所以这条用例跑**两轮** `describe_pending`，断言第二轮不再碰那条引用
    （`fetch` 调用次数不再增长），而可重试的那条**仍然**留在队列里重试。
    """
    from ling.domains.media.errors import MediaRefError

    await _make_evidence(ref="file:/etc/passwd")
    await _make_evidence(ref="obj/net-down.jpg")
    calls: list[str] = []

    async def fetch(ref: str) -> tuple[bytes, str]:
        calls.append(ref)
        if ref.startswith("file:"):
            raise MediaRefError("引用越界")
        raise RuntimeError("网络断了")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=fetch, router=_StubRouter(), config_reader=_StubConfig()
        )
        first = await describer.describe_pending(window_id=WINDOW)
        assert first.permanent_failures == 1

        second = await describer.describe_pending(window_id=WINDOW)

    assert second.permanent_failures == 0, "第二轮还在重试永久失败的引用（无限重试没修好）"
    assert calls.count("file:/etc/passwd") == 1, (
        f"越界引用被重试了：{calls}（它应该只被尝试一次）"
    )
    assert calls.count("obj/net-down.jpg") == 2, (
        f"可重试的失败没有被继续重试：{calls}（临时故障必须留在队列里）"
    )

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        still = await svc.pending(window_id=WINDOW)
        assert [v.media_ref for v in still] == ["obj/net-down.jpg"], (
            f"待办队列里应只剩可重试那条：{[v.media_ref for v in still]}"
        )
        windows = await svc.windows_with_pending()
        assert WINDOW in windows, "还有可重试的证据，这个窗口不该从队列里消失"


async def test_describe_failure_does_not_fake_a_description(logged_in: TestEnv) -> None:  # noqa: ARG001
    """终态**不能**伪装成描述：`description` 仍然为空，`fact_eligible` 仍 False。

    写一句"（无法描述）"会把"没看到"伪装成"看到了但没内容"——既污染证据，
    又让 `fact_eligible` 误判。终态只写在 `describe_failed_at` /
    `describe_failed_reason` 这两列。
    """
    from ling.domains.media.errors import MediaRefError

    evidence_id = await _make_evidence(ref="file:/etc/passwd")

    async def fetch(ref: str) -> tuple[bytes, str]:
        raise MediaRefError("引用越界")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=fetch, router=_StubRouter(), config_reader=_StubConfig()
        )
        await describer.describe_pending(window_id=WINDOW)

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None
        assert view.description is None, f"终态被写成了描述：{view.description!r}"
        assert view.fact_eligible is False
        assert view.describe_failed_at is not None, "终态没落库，队列不会停下来"
        assert view.describe_failed_reason and "引用" in view.describe_failed_reason
        assert view.needs_description is False, "标了终态却还说需要描述"

        facts = await svc.fact_descriptions(window_id=WINDOW)
        assert facts == [], f"终态漏进了事实：{facts}"


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


# ---------------------------------------------------------------- 模型侧失败不标终态
#
# P2 回归锁。验收发现：`describe_pending` 原来按 `retriable` 判断要不要标
# 永久终态，而模型侧的 `auth`(401) 与 `missing_secret` 都是
# `retriable=False`。后果是运维把视觉模型密钥配错**一次**，队列里所有待描述
# 证据被一次性标死；修好密钥后它们已不在 `pending()` 队列里，**再也不会重试**。
#
# 终态只该表示「**这个引用本身**不可用」（越界/超限/形式不支持），不该表示
# 「模型/配置暂时有问题」。判据必须是**错误来源**，不是 `retriable` 布尔值。
#
# 下面三条用例分别锁：401 不标终态、密钥缺失不标终态、修好后能成功描述；
# 以及"整轮中止"不把剩下的证据也白试一遍。


async def test_auth_error_does_not_mark_evidence_terminal(logged_in: TestEnv) -> None:  # noqa: ARG001
    """401（`auth`，retriable=False）**不该**把证据标成永久失败。

    密钥配错一次就把整批证据标死是这次验收的核心 bug：标死之后它们离开了
    `pending()` 队列，修好密钥也再不会重试。模型侧错误只该让这一轮跳过。
    """
    evidence_id = await _make_evidence(ref="obj/photo.jpg")
    router = _StubRouter(
        error=ModelError(
            ModelErrorKind.auth,
            "供应商 vision 鉴权失败（密钥无效或权限不足）",
            # **必须显式传 False**：生产代码（openai_compatible.py:90-95）就是这么
            # 抛的。`ModelError` 的默认值是 `retriable=True`，用默认值构造等于
            # 在测一个**不存在**的场景——旧的"按 retriable 标终态"逻辑下这条
            # 用例依然全绿（实测过），锁不住 bug。
            retriable=False,
        )
    )

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=router, config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id=WINDOW)

    assert outcome.failed == 1
    assert outcome.permanent_failures == 0, (
        "401 被标成了永久失败——修好密钥后这条证据再也不会重试"
    )
    assert outcome.aborted is True, "配置类错误应该整轮中止，而不是逐条白试"
    assert outcome.described == 0

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None
        assert view.description is None
        assert view.describe_failed_at is None, (
            f"401 把证据标成了终态：{view.describe_failed_reason!r}（应该留在队列里）"
        )
        assert view.needs_description is True, "401 之后这条证据应该还等着被描述"
        # 队列里还在，下一轮（密钥修好后）能被重新捞出来
        still = await svc.pending(window_id=WINDOW)
        assert [v.evidence_id for v in still] == [evidence_id], (
            f"401 之后证据离开了待办队列：{[v.media_ref for v in still]}"
        )


async def test_missing_secret_does_not_mark_evidence_terminal(logged_in: TestEnv) -> None:  # noqa: ARG001
    """`missing_secret`（根密钥不匹配 / 没存过密钥）同样**不**标终态。

    这条比 401 更容易发生：数据目录换过、`root.key` 丢了，都会让
    `SecretBox` 解不开已存的密钥，于是每条视觉调用都拿到 `missing_secret`。
    """
    evidence_id = await _make_evidence(ref="obj/photo.jpg")
    router = _StubRouter(
        error=ModelError(
            ModelErrorKind.missing_secret,
            "供应商 vision 的密钥无法解密：根密钥不匹配",
            # 同 401：生产代码（openai_compatible.py:71）显式传 False。
            retriable=False,
        )
    )

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=router, config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id=WINDOW)

    assert outcome.permanent_failures == 0, (
        "密钥缺失被标成了永久失败——补上密钥后这条证据再也不会重试"
    )
    assert outcome.aborted is True

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None
        assert view.description is None
        assert view.describe_failed_at is None, "密钥缺失把证据标成了终态"
        assert [v.evidence_id for v in await svc.pending(window_id=WINDOW)] == [
            evidence_id
        ], "密钥缺失之后证据离开了待办队列"


async def test_config_error_then_fixed_still_describes(logged_in: TestEnv) -> None:  # noqa: ARG001
    """**端到端回归**：401 → 修好密钥 → 同一批证据真的被成功描述。

    只断言"没标终态"是不够的：那只能证明它没被移出队列，证明不了
    "修好之后真的还能跑通"。这条用例跑两轮 `describe_pending`，第一轮
    模拟密钥配错、第二轮换成正常路由器，断言描述**真的写进去了**。

    这是 P2 的验收口径：`auth` 错误不该是单向门。
    """
    evidence_id = await _make_evidence(ref="obj/photo.jpg")

    bad_router = _StubRouter(
        error=ModelError(
            ModelErrorKind.auth,
            "供应商 vision 鉴权失败",
            retriable=False,  # 与生产一致，见 openai_compatible.py:90-95
        )
    )
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc,
            fetch_media=_fetch_ok,
            router=bad_router,
            config_reader=_StubConfig(),
        )
        first = await describer.describe_pending(window_id=WINDOW)
        assert first.described == 0 and first.permanent_failures == 0

    # 运维修好密钥：下一轮用正常路由器
    good_router = _StubRouter(text="一张桌面照片，上面有一只橘猫")
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc,
            fetch_media=_fetch_ok,
            router=good_router,
            config_reader=_StubConfig(),
        )
        second = await describer.describe_pending(window_id=WINDOW)

    assert second.described == 1, (
        "修好密钥后这条证据没能重新被描述（它已经离开了队列，这是单向门）"
    )
    assert second.failed == 0

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None
        assert view.description == "一张桌面照片，上面有一只橘猫"
        assert view.description_origin == "model_inference"
        assert view.describe_failed_at is None


async def test_config_error_stops_the_round_instead_of_retrying_each(
    logged_in: TestEnv,  # noqa: ARG001
) -> None:  # noqa: ARG001
    """配置类错误**整轮中止**：三条证据只该尝试第一条，不该把 limit 条全试一遍。

    逐条跳过等于把同样的失败调用打三遍——每次都必然以同样方式失败，纯属
    浪费，而且日志里三条一模一样的 401 反而看不出"整批都没跑"。
    """
    for i in range(3):
        await _make_evidence(ref=f"obj/photo{i}.jpg")

    router = _StubRouter(
        error=ModelError(
            ModelErrorKind.auth, "鉴权失败", retriable=False  # 与生产一致
        )
    )

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=router, config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id=WINDOW, limit=3)

    assert outcome.aborted is True, "配置类错误没有整轮中止"
    assert outcome.failed == 1, (
        f"配置类错误逐条重试了：failed={outcome.failed}（应该只试第一条就停）"
    )
    assert len(router.seen) == 1, (
        f"模型被白调用了 {len(router.seen)} 次（配置类错误第二次必然同样失败）"
    )
    assert outcome.permanent_failures == 0

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        still = await svc.pending(window_id=WINDOW, limit=3)
        assert len(still) == 3, (
            f"整轮中止后证据不该离开队列：{[v.media_ref for v in still]}"
        )
        assert all(v.describe_failed_at is None for v in still)


async def test_model_empty_reply_is_not_terminal(logged_in: TestEnv) -> None:  # noqa: ARG001
    """模型回空串是**模型侧**的临时状态，不是"这个引用不可用"。

    原来 `retriable=False` 的空回复也会被标终态。限流、被截断、只回了空白，
    都可能在下一轮就正常——不该永久放弃这张图。
    """
    evidence_id = await _make_evidence(ref="obj/photo.jpg")
    router = _StubRouter(text="")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=router, config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id=WINDOW)

    assert outcome.permanent_failures == 0, "空回复被标成了永久失败"
    assert outcome.aborted is False, "空回复是单条问题，不该整轮中止"

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None and view.describe_failed_at is None
        assert len(await svc.pending(window_id=WINDOW)) == 1


async def test_non_config_model_error_is_not_terminal_either(
    logged_in: TestEnv,  # noqa: ARG001
) -> None:  # noqa: ARG001
    """**非配置类**的模型错误即使 ``retriable=False`` 也**不**标终态。

    这条锁的是 ``_model_failure`` 里那个 ``terminal=False``。上面两条用例
    （401、密钥缺失）走的是 `is_config_failure` 的提前 ``break`` 分支，**碰不到**
    `terminal` 标志——把 ``terminal=False`` 改回 ``not error.retriable``，它们
    仍然全绿（实测过）。所以需要一条**不触发整轮中止**、但 ``retriable=False``
    的模型错误来单独锁住它。

    `HTTP 400/403` 映射成 ``ModelErrorKind.error`` 且 ``retriable=False``
    （见 `openai_compatible.py:102-108`）。它可能是"这张图对这个模型太大"，
    也可能是配置问题——无论哪种，都**不该**把证据永久标死。
    """
    evidence_id = await _make_evidence(ref="obj/photo.jpg")
    router = _StubRouter(
        error=ModelError(
            ModelErrorKind.error, "供应商返回 HTTP 400: image too large", retriable=False
        )
    )

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=_fetch_ok, router=router, config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id=WINDOW)

    assert outcome.failed == 1
    assert outcome.permanent_failures == 0, (
        "非配置类模型错误（retriable=False）被标成了永久失败"
    )
    assert outcome.aborted is False, "单条模型错误不该中止整轮"

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        view = await svc.get(evidence_id, window_id=WINDOW)
        assert view is not None
        assert view.description is None
        assert view.describe_failed_at is None, (
            f"模型侧错误把证据标成了终态：{view.describe_failed_reason!r}"
        )
        assert len(await svc.pending(window_id=WINDOW)) == 1, "证据离开了待办队列"


async def test_windows_with_pending_excludes_permanently_failed_only(
    logged_in: TestEnv,  # noqa: ARG001
) -> None:  # noqa: ARG001
    """**回归锁（P3）**：只剩永久失败证据的窗口不出现在 ``windows_with_pending()``。

    ## 为什么需要这条

    验收做**变异验证**发现：删掉 `windows_with_pending()` 里的
    `describe_failed_at.is_(None)` 过滤，17 条 media-describe 测试**全绿**。
    也就是说这条过滤没有任何测试保护，回退不会被发现。

    后果不是崩溃，而是**空转**：一个只剩永久失败证据的窗口每一轮都被列出来，
    `describe_pending` 又按 `pending()` 捞不到任何东西，于是每一轮都空跑一次、
    日志里一个字都没有。真问题（"这些引用永远不可用"）反而看不见了。

    ## 怎么锁

    两个窗口：`WINDOW` 里只有一条越界引用（会被标终态），`win_retry` 里有一条
    可重试失败的证据。跑完一轮后断言 `WINDOW` **不在**列表里，而 `win_retry`
    **在**——一条过滤删掉就会 FAIL，且不会误伤"可重试的窗口必须留下"。
    """
    from ling.domains.media.errors import MediaRefError

    await _make_evidence(ref="file:/etc/passwd")
    await _grant()
    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        await svc.record(
            subject_id=SUBJECT, window_id="win_retry", media_ref="obj/net-down.jpg"
        )

    async def fetch(ref: str) -> tuple[bytes, str]:
        if ref.startswith("file:"):
            raise MediaRefError("引用越界")
        raise RuntimeError("网络断了")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=fetch, router=_StubRouter(), config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id=WINDOW)
        assert outcome.permanent_failures == 1

        windows = await svc.windows_with_pending()

    assert WINDOW not in windows, (
        f"只剩永久失败证据的窗口仍在队列里：{windows}"
        "（`windows_with_pending` 的 describe_failed_at 过滤被删了？）"
    )
    assert "win_retry" in windows, (
        f"可重试失败的窗口被误伤了：{windows}（临时故障必须留在队列里）"
    )
    assert windows == ["win_retry"], f"窗口列表不对：{windows}"


async def test_windows_with_pending_empty_when_all_permanently_failed(
    logged_in: TestEnv,  # noqa: ARG001
) -> None:  # noqa: ARG001
    """**P3 的最强形式**：窗口里**全部**证据都永久失败时，列表必须为空。

    比上一条更直接：上一条还留着一条可重试证据做对照，这条窗口里一条
    可重试的都没有。删掉过滤的话 `windows_with_pending()` 会返回
    `[WINDOW]`，这条立刻 FAIL——而且报错信息直接指向"过滤被删"。
    """
    from ling.domains.media.errors import MediaRefError

    await _make_evidence(ref="file:/etc/passwd")
    await _make_evidence(ref="file:/etc/shadow")

    async def fetch(ref: str) -> tuple[bytes, str]:
        raise MediaRefError("引用越界")

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        describer = MediaDescriber(
            service=svc, fetch_media=fetch, router=_StubRouter(), config_reader=_StubConfig()
        )
        outcome = await describer.describe_pending(window_id=WINDOW)

    assert outcome.permanent_failures == 2
    assert outcome.failed == 2

    async with session_scope() as session:
        svc = MediaEvidenceService(session, actor_id=ACTOR)
        assert await svc.pending(window_id=WINDOW) == [], "永久失败证据没离开待办队列"
        assert await svc.windows_with_pending() == [], (
            "全部永久失败的窗口仍在 windows_with_pending() 里——"
            "`describe_failed_at.is_(None)` 过滤被删了？"
        )
