"""首屏的 `media` 段：她此刻是不是正在看/刷媒体。

## 为什么单独一段，不塞进 `phone`

用户原话：「他在忙，可以给他变成他的真实活动状态，放到"正在看手机"那里，
显示当前的状态」。

「正在看手机」讲的是**手机**（解锁、刷 app、放下）；「正在看视频/刷流」讲的是
**媒体注意力**。两者可以同时成立，也常常不同时成立——她完全可能在看 B 站而
根本没在"看手机"。塞进一起，就把"她在看手机"这个**具体事实**换成了"她可能
在做任何耗时媒体动作"这样一句更含糊的话。

## 最要紧的一条：受众

槽位是**全局**的，但细节不能跨会话泄漏。观看发生在别人那里时，本窗口只该
知道"她此刻正忙着媒体动作"——不该知道她在看哪个视频、哪个窗口。这与记忆的
受众边界是同一条原则，所以下面专门有对应用例。
"""

from __future__ import annotations

from ling.domains.media import slot
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture


async def _reset_slot() -> None:
    async with session_scope() as session:
        from sqlalchemy import text

        await session.execute(
            text(
                """
                UPDATE media_slots
                   SET holder_id = NULL, phase = 'held', kind = '',
                       actor_id = '', window_id = '', subject_id = '',
                       call_id = NULL, activity_id = NULL,
                       started_at = NULL, lease_until = NULL
                 WHERE resource_key = :key
                """
            ),
            {"key": slot.RESOURCE_KEY},
        )


def _media_section(body: dict) -> dict:
    return body["sections"]["media"]


async def test_no_media_activity_is_empty_not_idle(logged_in: TestEnv) -> None:  # noqa: F811
    """没有有效占用时是 **empty**，不编一个 idle 状态出来。

    说"她这会儿没在看媒体"和"她此刻闲置"是两件事：后者是关于她整个人的断言，
    而这一块只知道媒体这一层。空就是空。
    """
    await _reset_slot()
    body = (await logged_in.client.get("/v1/dashboard")).json()
    section = _media_section(body)
    assert section["status"] == "empty" or section.get("state") == "empty", section


async def test_held_phase_is_not_reported_as_watching(logged_in: TestEnv) -> None:  # noqa: F811
    """`held`（还在准备）不能说成"她正在看"。

    拿到执行权 ≠ 已经在看：还要解析短号、看缓存、准备下载。把 `held` 也报成
    "正在看"就是编造她的状态——这条与 `slot.current_holder()` 只认 `active`
    是同一个约束在接口层的体现。
    """
    await _reset_slot()
    async with session_scope() as session:
        got = await slot.acquire(
            session,
            kind=slot.KIND_VIDEO_WATCH,
            window_id="win_probe",
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
        )
    assert got.acquired

    body = (await logged_in.client.get("/v1/dashboard")).json()
    section = _media_section(body)
    assert section["status"] == "empty" or section.get("state") == "empty", (
        f"held 阶段就报成了正在看：{section}"
    )


async def test_active_watch_is_reported(logged_in: TestEnv) -> None:  # noqa: F811
    """`active` + 自己的主体 → 报出来，并且带 kind 与开始时间。"""
    await _reset_slot()
    async with session_scope() as session:
        got = await slot.acquire(
            session,
            kind=slot.KIND_VIDEO_WATCH,
            window_id="win_mine",
            actor_id=logged_in.settings.actor_id,
            subject_id=logged_in.subject_id,
        )
        assert got.acquired and got.handle is not None
        assert await slot.mark_active(session, got.handle)

    body = (await logged_in.client.get("/v1/dashboard")).json()
    section = _media_section(body)
    assert section["status"] == "ok", section
    data = section["data"]
    assert data["kind"] == slot.KIND_VIDEO_WATCH
    assert data["in_my_session"] is True
    assert data["started_at"], "开始时间必须给出，否则界面上说不出'从什么时候起'"


async def test_other_subjects_activity_hides_the_details(logged_in: TestEnv) -> None:  # noqa: F811
    """**受众**：观看发生在别人的主体上时，只说"忙"，不透露细节。

    槽位是全局的，但"她在看谁的视频、在哪个窗口"是别人的会话内容。
    这条与记忆的受众边界是同一条原则——状态可以共享，细节不行。
    """
    await _reset_slot()
    async with session_scope() as session:
        got = await slot.acquire(
            session,
            kind=slot.KIND_BILIBILI_FEED,
            window_id="win_someone_else",
            actor_id=logged_in.settings.actor_id,
            subject_id="subj_someone_else",
        )
        assert got.acquired and got.handle is not None
        assert await slot.mark_active(session, got.handle)

    body = (await logged_in.client.get("/v1/dashboard")).json()
    section = _media_section(body)
    assert section["status"] == "ok", section
    data = section["data"]
    # 仍然报"她正忙着媒体"——这是全局事实，本窗口有权知道
    assert data["kind"] == slot.KIND_BILIBILI_FEED
    # 但**不**是"我的会话"，也不给 activity_id
    assert data["in_my_session"] is False
    assert "activity_id" not in data, "别人的活动 id 泄漏了"
    assert "window_id" not in data, "别人的窗口 id 泄漏了"
