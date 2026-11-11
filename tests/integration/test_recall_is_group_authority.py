"""撤回目标解析里的**群聊判定**：必须读权威列 `Window.visibility`。

## 这里原来错在哪

`recall.py::resolve_recall_target` 之前这样判：

```python
is_group=":group:" in str(window.title or "")
```

而同一个函数上面几行刚从库里取回了**完整的** `Window` ORM 对象，`visibility`
就在手边。**明明有权威数据却用了间接的**——拿拼接出来的字符串约定去反推业务语义。

`window.title` 存的就是 `window_ref`（`kernel/bridge.py::_ensure_window` 的
`title=envelope.window_ref`），所以 `"onebot:private:group:123"` 这类**私聊**
标识会因为含 `:group:` 子串被判成群聊。

## 为什么这一处比别处更要紧

撤回是**不可撤销的破坏性动作**。判错群/私聊会走到错的平台接口，而
`ports/channel.py` 里 `RecallRequest` 的文档写明「猜错就是撤掉别人的消息」。
所以这里对判据准确性的要求比任何地方都高，却偏偏用了最脆弱的判据。

## 判据的权威定义

`kernel/visibility.py::is_group_visibility` 是**唯一判据**：
接受枚举或裸字符串（库里是 `String(32)`），`None`/未知值 fail-closed 到 `False`。

## ⚠️ 这四条挂了，先看是不是前置闸

`resolve_recall_target` 里除了群聊判定，还有**前四道闸**（归属 / 已送达 /
时间窗 / 频次，见它的 docstring）。其中"已送达"要求 `operations` 里有一行
`kind="delivery"` 且 `payload.reply_plan` 对得上，还要有一条
`delivery_receipts`。本文件的 `_seed` 把这几张表都造齐了。

**之所以用真表而不是 mock**：归属核对查的就是这两张表，mock 掉等于把要验的
前置条件替换掉了——那会让这四条变成"测我自己的 mock"。

**但代价是**：将来任何人改动那几道闸，这四条会**一起红**，而报错是
`no_receipt` / `not_delivered` / `expired`，**不是**"群聊判定错了"。
所以看到这四条同时失败时，**先按报错的 `code` 判断是前置闸还是判据**：

| 报错 code | 含义 | 该看哪 |
|---|---|---|
| `no_receipt` / `not_delivered` / `expired` / `rate_limited` / `not_found` | 前置闸没过 | `_seed` 是否还满足新前置条件 |
| 断言 `is False` / `is True` 失败 | **判据本身错了** | `recall.py` 的 `is_group=` 那行 |

判据本身只有 `test_private_visibility_wins_over_group_like_title`（第 2 条）
有能力抓到——把 `is_group=` 改回 `":group:" in title` 时，只有它会红
（实测：1 failed, 3 passed）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ling.contracts.v1 import OperationState
from ling.domains.conversation.recall import resolve_recall_target
from ling.infra.db import session_scope
from ling.infra.models import DeliveryReceiptRow, Message, Operation, Window
from ling.infra.security import new_id
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

ACTOR = "actor_owner"
CHANNEL = "onebot"


async def _seed(
    env: TestEnv,
    *,
    visibility: str,
    title: str,
) -> tuple[str, str]:
    """建一个**发过言且有回执**的窗口，返回 ``(window_id, message_id)``。

    `visibility` 与 `title` 都可控——这正是本文件要区分的那两列：
    权威的（`visibility`）和间接的（`title` 里能不能反推出群聊）。
    """
    async with session_scope() as session:
        window = Window(
            id=new_id("win"),
            actor_id=ACTOR,
            persona_id="persona_ling",
            subject_id=env.subject_id,
            title=title,
            channel=CHANNEL,
            visibility=visibility,
        )
        session.add(window)
        await session.commit()
        window_id = window.id

        row = Message(
            id=new_id("msg"),
            window_id=window_id,
            turn_id=new_id("turn"),
            role="actor",
            text="我刚才说错话了",
            origin="model_inference",
            sequence=1,
            state="completed",
        )
        session.add(row)
        await session.commit()

        # 撤回的第四道闸要求"这条真的发出去过"：必须有 `reply_plan` 的
        # operation 和一条回执。**走真表结构而不是 mock**——归属核对查的
        # 就是这两张表，mock 掉等于把要验的前置条件替换掉了。
        plan_id = f"plan_{row.turn_id}"
        operation = Operation(
            id=new_id("op"),
            kind="delivery",
            actor_id=ACTOR,
            subject_id=None,
            state=OperationState.succeeded.value,
            payload={
                "channel": CHANNEL,
                "instance": "onebot-card",
                "window_ref": title,
                "reply_plan": plan_id,
                "part_ordinal": 1,
                "parts_total": 1,
            },
        )
        session.add(operation)
        await session.flush()
        session.add(
            DeliveryReceiptRow(
                id=new_id("rcpt"),
                operation_id=operation.id,
                channel=CHANNEL,
                state="delivered",
                platform_message_id="9001",
                observed_at=datetime.now(UTC),
            )
        )
        await session.commit()
        return window_id, row.id


async def _is_group_for(window_id: str, message_id: str) -> bool:
    async with session_scope() as session:
        target = await resolve_recall_target(
            session,
            window_id=window_id,
            message_id=message_id,
            # 频次闸门是进程级全局，用例之间会互相计数；这里给一个新鲜的不限额
            # 闸门，免得"第 N 条"影响判定（本文件测的是群聊判定，不是限流）。
            limiter=_UnlimitedLimiter(),
            now=datetime.now(UTC) + timedelta(seconds=1),
        )
    return target.is_group


class _UnlimitedLimiter:
    """永远放行——把限流从被测行为里摘出去。"""

    def allow(self, window_id: str) -> bool:  # noqa: ARG002
        return True


# ---------------------------------------------------------------- 4 条判据


async def test_group_scope_visibility_is_group(web_env: TestEnv, logged_in: TestEnv) -> None:  # noqa: F811
    """1. 正常群窗口（`visibility="group_scope"`）→ 群聊。"""
    window_id, message_id = await _seed(
        web_env,
        visibility="group_scope",
        title="onebot:group:88888",
    )

    assert await _is_group_for(window_id, message_id) is True


async def test_private_visibility_wins_over_group_like_title(web_env: TestEnv, logged_in: TestEnv) -> None:  # noqa: F811
    """2. **核心用例**：私聊 visibility + 群聊形态的 title → 仍然是私聊。

    旧实现 `":group:" in title` 在这里判成 **True**（误判），
    新实现读 `visibility` 判成 **False**。两个判据在这一点上**结论相反**，
    所以这条断言只有真读了 `visibility` 才能通过——不是"碰巧对了"。
    """
    window_id, message_id = await _seed(
        web_env,
        visibility="subject_private",
        title="onebot:private:group:123",
    )

    assert await _is_group_for(window_id, message_id) is False


async def test_real_group_window_is_group(web_env: TestEnv, logged_in: TestEnv) -> None:  # noqa: F811
    """3. 真群聊窗口（title 与 visibility **都**指向群）→ 群聊。

    与第 2 条配对：证明判据不是"永远返回 False"。
    """
    window_id, message_id = await _seed(
        web_env,
        visibility="group_scope",
        title="onebot:group:100200300",
    )

    assert await _is_group_for(window_id, message_id) is True


async def test_private_visibility_with_uninferable_title_is_private(
    web_env: TestEnv, logged_in: TestEnv  # noqa: F811
) -> None:
    """4. 私聊 visibility + title 里**根本看不出**群聊 → 私聊，且不抛异常。

    为什么不是"群聊但 title 不含 `:group:`"：按
    `ports/channel.py` 的约定（`"<instance>:group:<gid>"`），所有合法群标识
    都必然含 `:group:`，那种窗口构造不出来。所以这里测的是相反的方向——
    title 完全无法反推时，新的判据照样给得出答案（旧实现给 False 只是
    因为"没匹配到"，那是运气，不是判据）。
    """
    window_id, message_id = await _seed(
        web_env,
        visibility="subject_private",
        title="web:private:notes",
    )

    assert await _is_group_for(window_id, message_id) is False
