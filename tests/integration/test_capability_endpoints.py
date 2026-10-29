"""能力端点接线：Together / Publishing / Speech 必须可被真实调用。

审计发现这三个模块"实现了但零调用点"——等于死代码。
本测试验证它们已接进 API，且各自的安全约束真的生效（不是摆设）。
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


# ------------------------------------------------------------------ 共处


async def test_together_session_lifecycle(logged_in: TestEnv) -> None:
    created = await logged_in.client.post(
        "/v1/together", json={"kind": "read", "title": "一起读同一本书"}
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session_id"]

    listing = await logged_in.client.get("/v1/together")
    assert listing.status_code == 200
    sessions = listing.json()["sessions"]
    assert any(s["session_id"] == session_id for s in sessions)


async def test_together_rejects_unknown_kind(logged_in: TestEnv) -> None:
    response = await logged_in.client.post(
        "/v1/together", json={"kind": "fly", "title": "x"}
    )
    assert response.status_code == 422


# ------------------------------------------------------------------ 发布


async def test_publishing_disabled_by_default(logged_in: TestEnv) -> None:
    """发布状态必须把两层分开报，不能用一个布尔糊过去。

    缺陷背景（docs/SETTINGS-TAXONOMY.md §5.1）：此前只有宿主能力这一层，
    且被当作授权层用，导致「宿主有能力」被误读成「我已授权」。
    """
    status = await logged_in.client.get("/v1/publishing")
    assert status.status_code == 200
    body = status.json()
    assert body["capability_enabled"] is False, "宿主发布能力默认必须关闭"
    assert body["consent_granted"] is False, "默认不得有发布授权"
    assert body["can_publish"] is False, "两层缺一不可"
    # 旧的单布尔字段不应继续存在，避免调用方又把它当成授权结论
    assert "enabled" not in body


async def test_publishing_capability_does_not_imply_consent(logged_in: TestEnv) -> None:
    """宿主能力打开但没授权时，仍必须拒绝。

    这是原缺陷的核心：env 一旦为 1 就绕过了 ConsentRule。
    """
    original = logged_in.app.state.settings
    try:
        # Settings 是 frozen dataclass，只能整体替换
        logged_in.app.state.settings = dataclasses.replace(
            original, allow_public_publish=True
        )
        logged_in.app.state.publishing = None  # 丢弃缓存的 service，让它按新能力重建

        status = await logged_in.client.get("/v1/publishing")
        body = status.json()
        assert body["capability_enabled"] is True
        assert body["consent_granted"] is False
        assert body["can_publish"] is False, "有能力不等于已授权"

        response = await logged_in.client.post(
            "/v1/publishing/drafts",
            json={"channel": "web", "title": "公告", "body": "内容"},
        )
        assert response.status_code == 403, "没有 ConsentRule 时必须拒绝"
        assert "public_publish" in response.json()["error"]["message"]
    finally:
        logged_in.app.state.settings = original
        logged_in.app.state.publishing = None


async def test_publishing_draft_blocked_when_disabled(logged_in: TestEnv) -> None:
    """默认关闭时创建草稿必须被拒（不能只靠 UI 隐藏按钮）。"""
    response = await logged_in.client.post(
        "/v1/publishing/drafts",
        json={"channel": "web", "title": "公告", "body": "内容"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


# ------------------------------------------------------------------ 语音


async def test_speech_final_segment_is_recorded(logged_in: TestEnv) -> None:
    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "语音"})
    ).json()["session_id"]
    response = await logged_in.client.post(
        "/v1/speech/segments",
        json={"window_id": window_id, "text": "这是识别出的正式文本", "kind": "final"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "final"
    assert body["text"] == "这是识别出的正式文本"


async def test_speech_partial_does_not_become_main_chain(logged_in: TestEnv) -> None:
    """partial 只做字幕，不能进主链（服务端硬编码，不靠调用方自觉）。"""
    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "语音partial"})
    ).json()["session_id"]
    response = await logged_in.client.post(
        "/v1/speech/segments",
        json={"window_id": window_id, "text": "半截话", "kind": "partial"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "partial"
    assert "不进入认知管线" in body["note"]

    # 主链（消息表）里不应出现 partial 文本
    messages = await logged_in.client.get(f"/v1/sessions/{window_id}/messages")
    texts = [m["text"] for m in messages.json()["messages"]]
    assert "半截话" not in texts


async def test_speech_empty_final_rejected(logged_in: TestEnv) -> None:
    """空 final 不能进主链（ASR 噪声不该变成空消息）。"""
    window_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "空语音"})
    ).json()["session_id"]
    response = await logged_in.client.post(
        "/v1/speech/segments",
        json={"window_id": window_id, "text": "   ", "kind": "final"},
    )
    assert response.status_code == 422


async def test_capability_endpoints_require_auth(web_env: TestEnv) -> None:
    for path in ("/v1/together", "/v1/publishing"):
        response = await web_env.client.get(path)
        assert response.status_code == 401, path
    response = await web_env.client.post(
        "/v1/speech/segments", json={"window_id": "w", "text": "x", "kind": "final"}
    )
    assert response.status_code == 401


# ------------------------------------------------------------------ 表达层分段


async def test_expression_splits_by_channel_limit() -> None:
    """表达层是渠道文本上限的切分点。

    `_finalize` 用 MAX_TEXT_PER_MESSAGE 计算 chunks 数记进事件 meta。
    Web 上限很大所以通常是 1 段，但 QQ/微信接入后这里就是真正的切分点，
    不能等到投递阶段才发现超长。
    """
    from ling.capabilities.expression import ExpressionService

    svc = ExpressionService(max_text_length=100)
    assert len(svc.plan_text("短句").chunks) == 1

    long_text = "这是一句话。" * 60  # 360 字
    plan = svc.plan_text(long_text)
    texts = [c.text for c in plan.chunks]
    assert len(texts) == 4
    assert all(len(t) <= 100 for t in texts), "有段超过渠道上限"
    assert "".join(texts) == long_text, "分段后拼不回原文（丢字）"


async def test_turn_completed_reports_chunk_count(logged_in: TestEnv) -> None:
    """turn.completed 必须带 chunks 字段，投递层才能判断要发几条。"""
    from tests.integration.conftest import read_sse

    session_id = (
        await logged_in.client.post("/v1/sessions", json={"title": "chunks"})
    ).json()["session_id"]
    response = await logged_in.client.post(
        f"/v1/sessions/{session_id}/turns",
        json={"client_turn_id": str(uuid.uuid4()), "text": "你好"},
    )
    events = await read_sse(response)
    completed = [payload for name, payload in events if name == "turn.completed"]
    assert completed, "没有 turn.completed"
    assert "chunks" in completed[-1]
    assert completed[-1]["chunks"] >= 1


# ------------------------------------------------------------------ 设置分类法


async def test_taxonomy_lists_nine_categories(logged_in: TestEnv) -> None:
    """分类法接口是前端导航的唯一来源，必须报全 9 类。"""
    response = await logged_in.client.get("/v1/settings/taxonomy")
    assert response.status_code == 200
    body = response.json()

    assert body["summary"]["total"] == 9
    labels = [c["label"] for c in body["categories"]]
    assert labels == [
        "身份与人格",
        "语言与声音",
        "模型与生成",
        "记忆与学习",
        "对话与主动性",
        "渠道与连接",
        "权限与隐私",
        "生活与活动",
        "系统与部署",
    ], f"一级类顺序变了：{labels}"

    for cat in body["categories"]:
        assert cat["groups"], f"{cat['label']} 没有二级分组"
        assert cat["boundary"].endswith("？"), f"{cat['label']} 的边界必须是可回答的问题"


async def test_taxonomy_available_categories_are_reachable(logged_in: TestEnv) -> None:
    """声称 available 的类，其 editable_route 必须真的能访问（不是 404）。

    GET 对只接受 POST 的端点会返回 405——那仍然证明路径存在。
    只有 404 才是"分类法在撒谎"。
    """
    body = (await logged_in.client.get("/v1/settings/taxonomy")).json()
    available = [c for c in body["categories"] if c["available"]]
    assert available, "至少应有一类可编辑"

    for cat in available:
        route = cat["editable_route"]
        assert route, f"{cat['label']} 声称可用但没有 editable_route"
        response = await logged_in.client.get(route)
        assert response.status_code != 404, (
            f"{cat['label']} 的 editable_route {route} 返回 404，前端会拿到空页面"
        )


async def test_taxonomy_unavailable_categories_have_no_entry(logged_in: TestEnv) -> None:
    """没有设置面的类不得给 editable_route，否则前端会渲染空壳页面。"""
    body = (await logged_in.client.get("/v1/settings/taxonomy")).json()
    for cat in body["categories"]:
        if cat["available"]:
            continue
        assert cat["editable_route"] is None, (
            f"{cat['label']} 未实现却被标成有编辑入口：{cat['editable_route']}"
        )


# ---------------------------------------------------------------- 共处持久化

# 原实现是纯内存字典：重启即全丢，多 worker 互相看不见。
# 共处是"正在一起做一件事"的状态，丢了用户只看到"刚才那个共读没了"。


async def test_together_survives_service_restart(logged_in: TestEnv) -> None:  # noqa: ARG001
    """换一个服务实例（等价于进程重启）仍能读到会话。"""
    from ling.capabilities.together import TogetherService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        created = await TogetherService(session).create(
            kind="watch", title="一起看纪录片", host_subject_id="subj_host", host_name="宿主"
        )
        sid = created.session_id

    async with session_scope() as session:
        again = await TogetherService(session).get(sid)
    assert again is not None, "重启后会话丢了"
    assert again.title == "一起看纪录片"
    assert [p.subject_id for p in again.participants] == ["subj_host"]


async def test_together_progress_and_end(logged_in: TestEnv) -> None:  # noqa: ARG001
    from ling.capabilities.together import TogetherService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        svc = TogetherService(session)
        s = await svc.create(
            kind="read", title="共读", host_subject_id="subj_a", host_name="A"
        )
        sid = s.session_id
        await svc.join(sid, subject_id="subj_b", display_name="B")
        await svc.report_progress(sid, subject_id="subj_a", position={"page": 12})
        await svc.report_progress(sid, subject_id="subj_b", position={"page": 30})

    async with session_scope() as session:
        svc = TogetherService(session)
        view = await svc.get(sid)
        assert view is not None
        assert view.progress == {"subj_a": {"page": 12}, "subj_b": {"page": 30}}
        assert len(view.participants) == 2
        ended = await svc.end(sid)
        assert ended.state == "ended"

    async with session_scope() as session:
        assert (await TogetherService(session).get(sid)).state == "ended"  # type: ignore[union-attr]


async def test_together_rejects_non_participant_progress(logged_in: TestEnv) -> None:  # noqa: ARG001
    """非参与者不能上报进度。

    不只是礼貌：不校验的话任何人都能替别人"看到第 30 分钟"，
    而进度是她据以判断"我们到哪了"的唯一依据。
    """
    import pytest as _pytest

    from ling.capabilities.together import TogetherService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        svc = TogetherService(session)
        s = await svc.create(kind="read", title="x", host_subject_id="subj_a", host_name="A")
        with _pytest.raises(PermissionError):
            await svc.report_progress(s.session_id, subject_id="subj_stranger", position={"page": 1})


async def test_together_cannot_join_ended(logged_in: TestEnv) -> None:  # noqa: ARG001
    import pytest as _pytest

    from ling.capabilities.together import TogetherService
    from ling.infra.db import session_scope

    async with session_scope() as session:
        svc = TogetherService(session)
        s = await svc.create(kind="read", title="y", host_subject_id="subj_a", host_name="A")
        await svc.end(s.session_id)
        with _pytest.raises(ValueError):
            await svc.join(s.session_id, subject_id="subj_c", display_name="C")
