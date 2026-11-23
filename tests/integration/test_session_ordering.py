"""会话列表顺序：**置顶 → 手动顺序 → 最近收到消息**（参考 QQ/微信）。

## 2026-11-24 的规则变更

用户原话：「这个对话的顺序按照时间处理，最新收到消息的对话自动往上排，参考
qq 微信的这种逻辑」。此前的主排序键是**好感度**（更早的原话「其他按好感度」），
查记录见 `docs/evidence/session-list-availability-20261124.md`。

**好感度不再是排序键**：它会让安静三个月的会话压在刚说过话的上面，而用户
打开列表想要的永远是"谁刚找过我"。因此本文件里两条把"按好感度排"当规范
的用例已被**改写为验证新规则**——它们不是被删掉，而是被翻转：如果哪天排序
键又被改回好感度，它们会立刻红。


## 为什么这个文件必须存在

这里曾经有一个**看起来完全正确、实际从未生效**的 bug：

```python
windows = await conversation.list_windows(...)      # 排序时没有 familiarity
familiarity = await conversation.familiarities(windows)   # 查完只用于展示
```

`list_windows` 的排序键是"置顶 → 好感度 → 最近活跃"，而好感度要按窗口的
subject_id 集合去查。于是存在一个循环依赖：

    排序需要 familiarity → familiarity 需要窗口列表 → 列表已排好序

原来的顺序把 familiarity 排除在排序之外：中间那个键全体相等，实际一直是
按 `updated_at` 排。两件事都做了，只是顺序错了——所以代码审查看不出来，
接口返回的 `familiarity` 字段也确实是真值，只有**顺序**是错的。

这类 bug 只有"造两个 updated_at 与 familiarity 顺序相反的数据"才能抓到。
所以下面的用例都刻意让两者**相反**：如果排序退化，断言立刻红。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ling.domains.conversation.service import (
    ConversationService,
    sort_windows_for_display,
)
from ling.infra.db import session_scope
from ling.infra.models import RelationshipState, Subject, User, Window
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture


def _actor_id(env: TestEnv) -> str:
    """当前进程的 actor_id（与路由用的是同一个设置源）。"""
    return env.settings.actor_id


async def _make_window(
    env: TestEnv,
    *,
    title: str,
    updated_at: datetime,
    pinned_at: datetime | None = None,
) -> str:
    """建一个窗口，返回 window_id。

    `updated_at` 直接写：ORM 的 `onupdate` 只在 UPDATE 时生效，插入时用
    显式值即可。
    """
    async with session_scope() as session:
        # 走服务层建窗口：`Window` 有 actor_id / persona_id 等必填列，
        # 手搓容易漏（例如 persona_id），而这三列又和本用例的排序无关。
        service = ConversationService(session)
        window = await service.create_window(
            subject_id=env.subject_id, title=title
        )
        # 排序键要的 updated_at / pinned_at 再显式覆盖：创建时是"现在"，
        # 而这里需要的是"很久以前"与"置顶"。
        window.updated_at = updated_at
        window.pinned_at = pinned_at
        await session.flush()
        return str(window.id)


async def _set_familiarity(env: TestEnv, subject_id: str, value: float) -> None:
    async with session_scope() as session:
        existing = await session.get(
            RelationshipState, {"actor_id": _actor_id(env), "subject_id": subject_id}
        )
        if existing is None:
            session.add(
                RelationshipState(
                    actor_id=_actor_id(env),
                    subject_id=subject_id,
                    familiarity=value,
                )
            )
        else:
            existing.familiarity = value


async def test_manual_order_survives_activity_and_respects_pin_groups(logged_in: TestEnv) -> None:  # noqa: F811
    now = datetime.now(UTC)
    oldest = await _make_window(logged_in, title="oldest", updated_at=now - timedelta(days=3))
    middle = await _make_window(logged_in, title="middle", updated_at=now - timedelta(days=2))
    newest = await _make_window(logged_in, title="newest", updated_at=now - timedelta(days=1))
    pinned = await _make_window(logged_in, title="pinned", updated_at=now, pinned_at=now)
    path = "/v1/sessions"

    async def listed() -> list[str]:
        response = await logged_in.client.get(path)
        assert response.status_code == 200, response.text
        return [row["session_id"] for row in response.json()["sessions"]]

    selected = {oldest, middle, newest, pinned}
    assert [row for row in await listed() if row in selected] == [pinned, newest, middle, oldest]
    move = await logged_in.client.post(
        f"{path}/{oldest}/move", json={"anchor_id": newest, "before": True}
    )
    assert move.status_code == 204, move.text
    async with session_scope() as session:
        old_window = await session.get(Window, oldest)
        middle_window = await session.get(Window, middle)
        assert old_window is not None and old_window.updated_at == now - timedelta(days=3)
        assert middle_window is not None
        middle_window.updated_at = now + timedelta(days=1)
    assert [row for row in await listed() if row in selected] == [pinned, oldest, newest, middle]

    cross_group = await logged_in.client.post(
        f"{path}/{oldest}/move", json={"anchor_id": pinned, "before": True}
    )
    assert cross_group.status_code == 422, cross_group.text
    pin = await logged_in.client.post(f"{path}/{middle}/pin", json={"pinned": True})
    assert pin.status_code == 200, pin.text
    assert [row for row in await listed() if row in selected] == [middle, pinned, oldest, newest]
    move = await logged_in.client.post(
        f"{path}/{middle}/move", json={"anchor_id": pinned, "before": False}
    )
    assert move.status_code == 204, move.text
    assert [row for row in await listed() if row in selected] == [pinned, middle, oldest, newest]
    repin = await logged_in.client.post(f"{path}/{middle}/pin", json={"pinned": True})
    assert repin.status_code == 200, repin.text
    assert [row for row in await listed() if row in selected] == [middle, pinned, oldest, newest]


async def test_presentation_edits_keep_existing_permissions(logged_in: TestEnv) -> None:  # noqa: F811
    async with session_scope() as session:
        service = ConversationService(session)
        channel = await service.create_window(
            subject_id=logged_in.subject_id, title="onebot-default:group:100200400", channel="onebot"
        )
        web = await service.create_window(subject_id=logged_in.subject_id, title="原名")
        foreign = await service.create_window(subject_id="subj_unrelated", title="其他会话")
        channel_id, web_id, foreign_id = channel.id, web.id, foreign.id

    path = "/v1/sessions"
    unavailable = await logged_in.client.post(
        f"{path}/{web_id}/move", json={"anchor_id": foreign_id, "before": True}
    )
    assert unavailable.status_code == 404, unavailable.text

    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.subject_id == logged_in.subject_id))
        assert user is not None
        user.role = "member"

    renamed_person = await logged_in.client.patch(
        f"/v1/accounts/subjects/{logged_in.subject_id}", json={"display_name": "未授权"}
    )
    rename_channel = await logged_in.client.patch(f"{path}/{channel_id}", json={"title": "未授权"})
    pin_channel = await logged_in.client.post(f"{path}/{channel_id}/pin", json={"pinned": True})
    reorder = await logged_in.client.post(
        f"{path}/{web_id}/move", json={"anchor_id": channel_id, "before": True}
    )
    assert all(response.status_code == 403 for response in (
        renamed_person, rename_channel, pin_channel, reorder
    ))
    rename_web = await logged_in.client.patch(f"{path}/{web_id}", json={"title": "网页新名"})
    assert rename_web.status_code == 200 and rename_web.json()["title"] == "网页新名"


async def test_list_is_ordered_by_recent_message_not_by_familiarity(  # noqa: F811
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """最近收到消息的排前面——**哪怕它对端的好感度更低**。

    这条是 2026-11-24 规则变更的直接回归。造数让两个排序键**相反**：
    `recent-low` 刚来过消息，"好感度"却低；`stale-high` 三十天没动静。
    按好感度排会得到 [stale-high, recent-low]，按最近消息排应得到
    [recent-low, stale-high]。所以只要有人把好感度键加回去，这里立刻红。

    ## 这条用例能证明什么、不能证明什么

    能证明：列表顺序跟随 `updated_at`。
    不能证明：好感度完全不参与排序——单用户单 subject 时好感度是常量，
    常量键与"没有这个键"在结果上不可区分。区分力由纯函数用例
    `test_familiarity_does_not_override_recency` 提供（那里两个 subject 不同）。
    """
    now = datetime.now(UTC)
    await _make_window(logged_in, title="recent-low", updated_at=now)
    await _make_window(
        logged_in, title="stale-high", updated_at=now - timedelta(days=30)
    )
    response = await logged_in.client.get("/v1/sessions")
    assert response.status_code == 200, response.text
    titles = [item["title"] for item in response.json()["sessions"]]
    assert titles.index("recent-low") < titles.index("stale-high")


async def test_pinned_windows_come_first(logged_in: TestEnv) -> None:  # noqa: F811
    """置顶的排在最前，哪怕它很旧。"""
    now = datetime.now(UTC)
    await _make_window(
        logged_in,
        title="old-pinned",
        updated_at=now - timedelta(days=60),
        pinned_at=now - timedelta(days=60),
    )
    await _make_window(logged_in, title="new-unpinned", updated_at=now)

    response = await logged_in.client.get("/v1/sessions")
    assert response.status_code == 200, response.text
    titles = [item["title"] for item in response.json()["sessions"]]
    assert titles[0] == "old-pinned", f"置顶没排最前：{titles}"


async def test_familiarity_does_not_override_recency(logged_in: TestEnv) -> None:  # noqa: F811
    """不同 subject 的窗口：**好感度高也不能盖过"更近的消息"**。

    上一轮这条用例断言的正好相反（它曾经是"按好感度排"的直接回归）。
    现在要求反过来：`subj_close` 好感度 0.9 但九十天没说话，`subj_distant`
    好感度 0.1 却是刚刚——列表必须把刚刚那个放前面。

    ## 为什么这条必须有

    单 subject 的接口层用例里，好感度是常量，"按好感度排"和"按时间排"
    结果相同，**它证明不了任何事**。只有让两个键指向相反顺序，才能区分
    两种实现。这就是这条用例的全部价值：它是"好感度不再是排序键"这件事
    唯一有区分力的证据。
    """
    now = datetime.now(UTC)
    stale_but_close = Window(
        id="win_a",
        actor_id="actor",
        subject_id="subj_close",
        title="close",
        channel="web",
        visibility="subject_private",
        updated_at=now - timedelta(days=90),
    )
    recent_but_distant = Window(
        id="win_b",
        actor_id="actor",
        subject_id="subj_distant",
        title="distant",
        channel="web",
        visibility="subject_private",
        updated_at=now,
    )
    ordered = sort_windows_for_display(
        [stale_but_close, recent_but_distant],
        {"subj_close": 0.9, "subj_distant": 0.1},
    )
    assert [w.id for w in ordered] == ["win_b", "win_a"], (
        "好感度又盖过了最近消息：刚说过话的会话被压到九十天没动静的后面"
    )


async def test_missing_updated_at_sorts_last(logged_in: TestEnv) -> None:  # noqa: F811
    """`updated_at` 缺失（`None`）视为最旧，排在所有有时间戳的后面。

    这是排序键的缺失值契约，与好感度无关。原用例用"未知好感度排最后"表达了
    同一件事，现在换个维度表达——因为好感度不再参与定序，用它能证明的东西
    只剩"它没参与"。
    """
    now = datetime.now(UTC)
    timed = Window(
        id="win_timed",
        actor_id="actor",
        subject_id="subj_timed",
        title="timed",
        channel="web",
        visibility="subject_private",
        updated_at=now - timedelta(days=10),
    )
    missing = Window(
        id="win_missing",
        actor_id="actor",
        subject_id="subj_missing",
        title="missing",
        channel="web",
        visibility="subject_private",
        updated_at=None,
    )
    ordered = sort_windows_for_display([missing, timed], {})
    assert [w.id for w in ordered] == ["win_timed", "win_missing"]


def test_list_route_passes_familiarity_into_the_sort() -> None:
    """接口层必须**先查好感度、再排序**。

    这是原 bug 的形状级回归：两个动作都在，只是顺序反了，所以行为测试
    （上面那三条纯函数用例）抓不到接线错误。这里直接看源码里两者的相对
    位置——`familiarities(...)` 必须出现在 `sort_windows_for_display(...)`
    之前，且 `sort_windows_for_display` 真的在路由里被调用过。
    """
    import pathlib
    import re

    src = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "ling"
        / "api"
        / "routes"
        / "sessions.py"
    ).read_text(encoding="utf-8")

    fam_at = src.find("await conversation.familiarities(")
    sort_at = src.find("sort_windows_for_display(")
    assert fam_at != -1, "路由里没有查好感度"
    assert sort_at != -1, "路由里没有调用排序——好感度又只用于展示了"
    assert fam_at < sort_at, (
        "排序发生在查好感度之前：中间那个排序键会全体相等，"
        "实际退化成按 updated_at 排（这就是原 bug）"
    )
    # 防止"调了但没用返回值"：排序结果必须被赋回 windows。
    assert re.search(r"windows\s*=\s*sort_windows_for_display\(", src), (
        "调用了排序但没把结果赋回 windows，排序等于没做"
    )


async def test_subject_and_window_exist_for_the_fixture(  # noqa: F811
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """兜底：确认 fixture 造出来的主体与窗口是真的落库了。

    这条不是业务断言，是给上面几条造数用例一个自检——如果 fixture 变了
    （例如 subject_id 来源改了），上面那些"看起来通过"的用例可能其实在测
    空列表。
    """
    async with session_scope() as session:
        subject = await session.get(Subject, logged_in.subject_id)
        assert subject is not None
    response = await logged_in.client.get("/v1/sessions")
    assert response.status_code == 200
    assert response.json()["sessions"], "会话列表为空，上面的排序用例没有意义"


async def test_pin_endpoint_round_trips_and_exposes_the_field(logged_in: TestEnv) -> None:  # noqa: F811
    """`POST /sessions/{id}/pin` 往返，且 `SessionRecord.pinned` 真的出现。

    ## 为什么单独测这一个端点

    排序本身有 `test_pinned_windows_come_first` 覆盖，但那条是**直接写库**
    （`_make_window(pinned_at=…)`）。用户实际走的路径是 **API**：
    `POST /pin` → 返回整条记录 → 前端拿 `pinned` 决定图钉状态与分组。

    这两段之间断过一次：后端一直返回 `SessionRecord.pinned`、也一直支持
    `/pin`，但前端从没消费（`clients/web-console/src/` 搜 `pinned` 零命中），
    于是"能按好感度排"却"不能置顶"。所以这里断言的是**字段出现在响应里**，
    不只是"库里那个时间是新的"——那正是当初漏掉的一环。

    同时钉住幂等：重复置顶不报错（只刷新 `pinned_at`，于是"刚置顶的"
    排到置顶组第一位），取消一个没置顶的也不报错。
    """
    import uuid

    window_id = await _make_window(
        logged_in, title=f"pin-{uuid.uuid4().hex[:8]}", updated_at=datetime.now(UTC)
    )

    # 1) 置顶：返回整条记录，`pinned` 为真
    response = await logged_in.client.post(
        f"/v1/sessions/{window_id}/pin", json={"pinned": True}
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["pinned"] is True, record
    assert record["session_id"] == window_id

    # 2) 列表里也带这个字段，且它排在前面
    listed = (await logged_in.client.get("/v1/sessions")).json()["sessions"]
    mine = [item for item in listed if item["session_id"] == window_id]
    assert len(mine) == 1, listed
    assert mine[0]["pinned"] is True
    assert listed[0]["session_id"] == window_id, "置顶后没排到最前"

    # 3) 幂等：重复置顶不报错
    again = await logged_in.client.post(
        f"/v1/sessions/{window_id}/pin", json={"pinned": True}
    )
    assert again.status_code == 200, again.text

    # 4) 取消置顶：`pinned` 变回假
    unpinned = await logged_in.client.post(
        f"/v1/sessions/{window_id}/pin", json={"pinned": False}
    )
    assert unpinned.status_code == 200, unpinned.text
    assert unpinned.json()["pinned"] is False

    # 5) 幂等：取消一个没置顶的也不报错
    noop = await logged_in.client.post(
        f"/v1/sessions/{window_id}/pin", json={"pinned": False}
    )
    assert noop.status_code == 200, noop.text
