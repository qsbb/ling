"""私聊里没绑定过的号：自动注册成独立自然人，但**不**合并到任何人。

边界是刻意的：

- 注册只是给消息一个主体可挂（否则消息进不了流水线）；
- 合并会共享主人的全部记忆，必须由管理员看过是谁之后手动做
  （`/settings/identity` 的合并/拆开）。

这里走的是**真实入站链路**（`/v1/onebot/events`），所以开关没接上线、
或者注册之后偷偷并到了主人身上，测试都会红。
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import select

from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from ling.infra.models import ActorAccount
from tests.integration.conftest import TestEnv, logged_in  # noqa: F401 - fixture
from tests.integration.test_onebot_loop import (  # noqa: PLC2701 - 复用同一套假 OneBot
    FakeOneBot,
    _event_body,
    _install,
    _post,
)

pytestmark = pytest.mark.w0_web

INSTANCE = "onebot-loop"
UNKNOWN_QQ = 100200300


async def test_unbound_private_is_rejected_when_flag_is_off(logged_in: TestEnv) -> None:  # noqa: F811
    fake = FakeOneBot()
    _install(logged_in, fake)

    response = await _post(logged_in, _event_body(user_id=UNKNOWN_QQ))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is False, body
    assert "绑定" in body["reason"], body


async def test_unbound_private_is_registered_when_flag_is_on(logged_in: TestEnv) -> None:  # noqa: F811
    fake = FakeOneBot()
    _install(logged_in, fake)
    logged_in.app.state.settings = dataclasses.replace(
        logged_in.app.state.settings, auto_register_private=True
    )

    response = await _post(logged_in, _event_body(user_id=UNKNOWN_QQ))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True, body

    # 注册出来了，但**不是**主人：没合并就拿不到主人的记忆。
    async with session_scope() as session:
        row = (
            await session.execute(
                select(ActorAccount).where(
                    ActorAccount.connector_instance == INSTANCE,
                    ActorAccount.external_account_id == str(UNKNOWN_QQ),
                )
            )
        ).scalar_one()
        owner = await IdentityService(session).owner_subject_id()

    assert row.proof == "pending", "自动注册不是身份证明，不能是 proven"
    assert row.subject_id != owner, "未经管理员合并，不能等于主人"
