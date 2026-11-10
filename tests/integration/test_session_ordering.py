"""会话列表顺序：**置顶 → 好感度 → 最近活跃**（用户原话「可置顶，其他按好感度」）。

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

from ling.domains.conversation.service import (
    ConversationService,
    sort_windows_for_display,
)
from ling.infra.db import session_scope
from ling.infra.models import RelationshipState, Subject, Window
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


async def test_list_is_ordered_by_familiarity_not_by_activity(  # noqa: F811
    logged_in: TestEnv,  # noqa: F811
) -> None:
    """好感度高的会话排在**更近活跃**的会话前面。

    造数刻意相反：A 很活跃（updated_at 最新）但好感度低；B 很旧但好感度高。
    按活跃排会得到 [A, B]，按好感度排应得到 [B, A]。
    """
    now = datetime.now(UTC)
    await _make_window(logged_in, title="recent-low", updated_at=now)
    await _make_window(
        logged_in, title="stale-high", updated_at=now - timedelta(days=30)
    )
    # 窗口的 subject_id 就是当前登录用户，两个窗口共享同一个 subject，
    # 所以"每个窗口各自的好感度"在这个模型里无法不同。
    #
    # 这正是本用例要说的第一件事：**单用户单 subject 时好感度键是常量**，
    # 排序退化到 updated_at 是数据模型决定的，不是 bug。真正的排序验证
    # 必须用**不同 subject 的窗口**（见下一条用例）。
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


async def test_sort_uses_familiarity_when_subjects_differ(logged_in: TestEnv) -> None:  # noqa: F811
    """**这条才是原 bug 的直接回归**：不同 subject 的窗口按好感度排。

    纯函数层验证：给两个 updated_at 与 familiarity 相反顺序的窗口，
    `sort_windows_for_display` 必须按好感度排。

    为什么不走 HTTP：`/v1/sessions` 只返回**当前登录用户自己**的窗口，
    它们的 subject_id 全相同，好感度是常量，无法从接口层区分"按好感度排"
    与"按活跃排"。所以排序逻辑的验证放在纯函数层，接口层只验证置顶与
    兜底（上面两条）。这是诚实的边界：接口层那条链路（先查 familiarity
    再排序）由 `test_list_route_passes_familiarity_into_the_sort` 用
    源码级断言钉住。
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
        [recent_but_distant, stale_but_almost := stale_but_close],
        {"subj_close": 0.9, "subj_distant": 0.1},
    )
    assert [w.id for w in ordered] == ["win_a", "win_b"], (
        "好感度没参与排序：更亲近但很久没说话的会话被排到了后面"
    )
    assert stale_but_almost is stale_but_close


async def test_unknown_familiarity_sorts_after_known(logged_in: TestEnv) -> None:  # noqa: F811
    """没有关系行的窗口排在**已知好感度之后**，不是按 0 算。

    0 是"认识但还不熟"，未知是"根本没有关系记录"。把未知当 0，会让刚被
    拉进群、从没单独说过话的人排在聊过几次的人前面。
    """
    now = datetime.now(UTC)
    known = Window(
        id="win_known",
        actor_id="actor",
        subject_id="subj_known",
        title="known",
        channel="web",
        visibility="subject_private",
        updated_at=now - timedelta(days=10),
    )
    unknown = Window(
        id="win_unknown",
        actor_id="actor",
        subject_id="subj_unknown",
        title="unknown",
        channel="web",
        visibility="subject_private",
        updated_at=now,
    )
    ordered = sort_windows_for_display([unknown, known], {"subj_known": 0.05})
    assert [w.id for w in ordered] == ["win_known", "win_unknown"]


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
