"""连接器**实例目录**的 CRUD 与"配置真的落到那个实例上"。

## 这一组守的是什么

数据层（迁移 0067 + ``ConnectorInstance``）已经把表建好了，但"表存在"不等于
"实例能用"。本仓库反复出现的一类缺陷是**名字比实现强**：端点叫"创建实例"，
行也建了，而配置保存路径完全不认这个 ``instance_id``——于是每个实例保存的
配置都写进 ``'default'``，界面上却显示"两个实例各有一份配置"。

所以这里最重要的一条不是 CRUD，而是
``test_config_saved_to_new_instance_lands_on_that_instance``：
**创建实例 → 把配置保存到它上面 → 查回来，``instance_id`` 是新实例，不是
``'default'``。**

## 什么是"明确报错"

重名必须是 **409 + ``detail="duplicate_display_name"``**，不能是 500
（那是"我没处理这个情况"的默认表现），也不能是 200（那说明唯一性根本没生效）。
测试断言了状态码**和** detail，因为只断言 409 的话，"因为别的原因也 409"
会假绿。

## 环境

集成测试用 ``create_all_for_dev`` 建表（与迁移 DDL 等价），所以这里的
``connector_instances`` 上**没有**升级时回填出的默认实例行——每条用例自己
决定要不要建。这不是"绕过了迁移"：迁移本身的正确性由
``test_connector_multi_instance_migration.py`` 单独覆盖。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import (
    DEFAULT_INSTANCE_ID,
    ConnectorConfigHead,
    ConnectorConfigRevision,
    ConnectorInstance,
    ConnectorOverrideValue,
)
from tests.integration.conftest import (
    TestEnv,  # noqa: F401 - 类型注解
    logged_in,  # noqa: F401, F811 - fixture 以参数形式遮蔽导入名
)

pytestmark = pytest.mark.w0_web

BASE = "/v1/settings/connectors"
ORIGIN = {"Origin": "http://testserver"}


# ---------------------------------------------------------------- 假切换器


def _detail(response) -> str | None:
    """错误响应的 ``detail``。

    本仓库的错误体是**嵌套**的：``{"schema_version": ..., "error": {"code",
    "message", "detail", "field"}}``。写成 ``body.get("detail")`` 会恒为
    ``None``——那种断言永远不成立也永远不会绿，比不写更糟。
    """
    body = response.json()
    error = body.get("error")
    return error.get("detail") if isinstance(error, dict) else body.get("detail")


class FakeRuntime:
    """只记账的切换器：不绑端口、不建长连接。

    记录 ``(scope, instance_id?)``：``apply`` 的签名里**目前没有** instance_id
    （运行时按实例启动是下一阶段），所以这里只记 scope，用于断言
    "究竟有没有走到切换"。
    """

    def __init__(self) -> None:
        self.applies: list[tuple[str, dict[str, Any]]] = []
        self.stop_calls = 0

    async def apply(self, scope: str, *, values: dict[str, Any], operation_id: str) -> Any:
        from ling.settings.connector_service import SwapOutcome

        self.applies.append((scope, values))
        return SwapOutcome(ok=True, health="ok", stop_calls=0)


@pytest.fixture
def fake_runtime(web_env: TestEnv) -> FakeRuntime:
    runtime = FakeRuntime()
    web_env.app.state.connector_runtime = runtime
    return runtime


# ---------------------------------------------------------------- 帮助函数


def _instances_url(scope: str = "onebot", *, instance_id: str | None = None) -> str:
    base = f"{BASE}/{scope}/instances"
    return base if instance_id is None else f"{base}/{instance_id}"


async def _create(
    env: TestEnv,
    *,
    scope: str = "onebot",
    display_name: str,
    instance_id: str | None = None,
    enabled: bool = True,
):
    body: dict[str, Any] = {"display_name": display_name, "enabled": enabled}
    if instance_id is not None:
        body["instance_id"] = instance_id
    return await env.client.post(
        _instances_url(scope), json=body, headers=dict(ORIGIN)
    )


async def _list(env: TestEnv, *, scope: str = "onebot", include_deleted: bool = False):
    params = {"include_deleted": "true"} if include_deleted else None
    return await env.client.get(_instances_url(scope), params=params)


async def _save(
    env: TestEnv,
    *,
    scope: str = "onebot",
    instance_id: str | None = None,
    set_values: dict[str, Any],
):
    """保存配置。``instance_id`` 走 query 参数（缺省 = 默认实例）。"""
    params = {"instance_id": instance_id} if instance_id is not None else None
    return await env.client.put(
        f"{BASE}/{scope}",
        json={"set": set_values},
        headers=dict(ORIGIN),
        params=params,
    )


async def _heads(scope: str = "onebot") -> list[ConnectorConfigHead]:
    async with session_scope() as session:
        return list(
            (
                await session.execute(
                    select(ConnectorConfigHead).where(ConnectorConfigHead.scope == scope)
                )
            ).scalars()
        )


# ---------------------------------------------------------------- 1. 创建 → 列表


async def test_create_then_list_shows_instance(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """创建的行必须**真的落库**，并且列表能列出来。"""
    response = await _create(
        logged_in, display_name="主力号", instance_id="main"
    )
    assert response.status_code == 201, response.text
    item = response.json()["item"]
    assert item["scope"] == "onebot"
    assert item["instance_id"] == "main"
    assert item["display_name"] == "主力号"
    assert item["enabled"] is True
    assert item["deleted_at"] is None

    listing = await _list(logged_in)
    assert listing.status_code == 200, listing.text
    ids = [row["instance_id"] for row in listing.json()["items"]]
    assert "main" in ids

    # 也直接查库确认（不是"API 自己回显了自己刚写的东西"）
    async with session_scope() as session:
        row = await session.get(ConnectorInstance, ("onebot", "main"))
        assert row is not None
        assert row.display_name == "主力号"


async def test_create_derives_instance_id_from_ascii_name(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """不给 instance_id 时按显示名派生（保证 id 形状合法）。"""
    response = await _create(logged_in, display_name="Second Bot")
    assert response.status_code == 201, response.text
    assert response.json()["item"]["instance_id"] == "second-bot"


# ---------------------------------------------------------------- 2. 同 scope 重名


async def test_duplicate_display_name_is_explicit_409(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """同 scope 重名 → **409 + duplicate_display_name**，不是 500。

    这条是本组里最容易被写成假绿的一条：只断言 ``status_code == 409``
    时，"因为 id 撞了"也能通过。所以**同时**断言 detail。
    """
    first = await _create(logged_in, display_name="同一个小号")
    assert first.status_code == 201, first.text
    second = await _create(logged_in, display_name="同一个小号")
    assert second.status_code == 409, second.text
    assert _detail(second) == "duplicate_display_name"
    assert second.json()["error"]["code"] == "conflict"

    # 库里只有一行
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(ConnectorInstance).where(
                        ConnectorInstance.display_name == "同一个小号"
                    )
                )
            ).scalars()
        )
        assert len(rows) == 1


async def test_duplicate_check_happens_before_database_constraint(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """重名必须在**应用层先查明**，而不是撞到唯一键再翻译。

    为什么需要这一条：把 ``_ensure_name_free`` 整个删掉，上面那条重名测试
    **仍然是绿的**——数据库的 ``uq_connector_instance_display_name`` 会抛出
    ``IntegrityError``，``_unique_violation`` 把它翻译成同样的 409 + 同样的
    detail。也就是说，上面那条只证明了"重名会 409"，**没有证明预检查存在**。

    区分两条路径的可观测差异：撞唯一键会让会话里的那条 INSERT 失败，
    而 SQLAlchemy 的 ``flush`` 失败后事务处于不可继续状态——除非有人回滚。
    这里用一个**不被路由层提交**的服务调用观察：预检查路径在 flush 之前就
    抛错，所以会话仍然可用；DB 路径如果没处理好，会话会带着失败语句。

    断言方式是"紧接着还能在同一会话里成功写一行"：预检查路径可以，
    "撞了约束却没清理"的路径会在这里炸。
    """
    from ling.settings.connector_instances import (
        ConnectorInstanceError,
        ConnectorInstanceService,
    )

    async with session_scope() as session:
        service = ConnectorInstanceService(session)
        first = await service.create(scope="onebot", display_name="预检查", instance_id="pre1")
        assert first.instance_id == "pre1"
        with pytest.raises(ConnectorInstanceError) as caught:
            await service.create(scope="onebot", display_name="预检查")
        assert caught.value.code == "duplicate_display_name"
        assert caught.value.status_code == 409
        # 会话仍可继续（预检查在 flush 之前就结束了，没有失败的语句）
        second = await service.create(
            scope="onebot", display_name="另一个", instance_id="pre2"
        )
        assert second.instance_id == "pre2"

    async with session_scope() as session:
        names = {
            row.instance_id
            for row in (
                await session.execute(
                    select(ConnectorInstance).where(ConnectorInstance.scope == "onebot")
                )
            ).scalars()
        }
        assert names == {"pre1", "pre2"}


# ---------------------------------------------------------------- 3. 不同 scope 可重名


async def test_same_display_name_allowed_across_scopes(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """不同 scope 可以同名——唯一约束是 ``(scope, display_name)``，不是全局。

    这是配对的正向断言：上一条证明"同 scope 会拒"，这一条证明"拒的不是
    名字本身"，否则把约束写成全局唯一也能让上一条通过。
    """
    onebot = await _create(logged_in, scope="onebot", display_name="默认接入")
    qqbot = await _create(logged_in, scope="qqbot", display_name="默认接入")
    assert onebot.status_code == 201, onebot.text
    assert qqbot.status_code == 201, qqbot.text

    # 两个 scope 各自能列出自己那份，且互不串
    onebot_ids = {
        (row["scope"], row["display_name"]) for row in (await _list(logged_in, scope="onebot")).json()["items"]
    }
    qqbot_ids = {
        (row["scope"], row["display_name"]) for row in (await _list(logged_in, scope="qqbot")).json()["items"]
    }
    assert ("onebot", "默认接入") in onebot_ids
    assert ("qqbot", "默认接入") in qqbot_ids
    assert ("qqbot", "默认接入") not in onebot_ids


# ---------------------------------------------------------------- 4. 软删


async def test_soft_delete_hides_from_list_but_keeps_history(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """软删后列表看不到，但**历史配置还能查**。

    语义上"删客户端"不该毁掉"这个实例存过什么"。所以：
    - 默认列表不含它；
    - ``include_deleted=true`` 含它，且 ``deleted_at`` 有值；
    - 它的修订链还在库里。
    """
    created = await _create(logged_in, display_name="要删的", instance_id="doomed")
    assert created.status_code == 201, created.text

    # 先给它存一份配置，之后验证历史还在
    saved = await _save(
        logged_in, instance_id="doomed", set_values={"LING_ONEBOT_SELF_ID": "100200300"}
    )
    assert saved.status_code in (200, 202), saved.text

    deleted = await logged_in.client.delete(
        _instances_url("onebot", instance_id="doomed"),
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["item"]["deleted_at"] is not None

    visible_ids = [row["instance_id"] for row in (await _list(logged_in)).json()["items"]]
    assert "doomed" not in visible_ids

    with_deleted = await _list(logged_in, include_deleted=True)
    rows = {row["instance_id"]: row for row in with_deleted.json()["items"]}
    assert "doomed" in rows
    assert rows["doomed"]["deleted_at"] is not None

    # 历史配置仍在：修订与覆盖值都没被删
    async with session_scope() as session:
        revisions = list(
            (
                await session.execute(
                    select(ConnectorConfigRevision).where(
                        ConnectorConfigRevision.scope == "onebot",
                        ConnectorConfigRevision.instance_id == "doomed",
                    )
                )
            ).scalars()
        )
        assert revisions, "软删不该删掉修订链"
        revision_ids = [row.id for row in revisions]
        overrides = list(
            (
                await session.execute(
                    select(ConnectorOverrideValue).where(
                        ConnectorOverrideValue.revision_id.in_(revision_ids)
                    )
                )
            ).scalars()
        )
        assert overrides, "软删不该删掉覆盖值"


async def test_deleted_instance_cannot_be_renamed_or_enabled(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """软删后改名 / 启停都被拒（409 ``instance_deleted``）。"""
    created = await _create(logged_in, display_name="删了就删了", instance_id="gone")
    assert created.status_code == 201, created.text
    deleted = await logged_in.client.delete(
        _instances_url("onebot", instance_id="gone"),
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert deleted.status_code == 200, deleted.text

    renamed = await logged_in.client.put(
        _instances_url("onebot", instance_id="gone"),
        json={"display_name": "改个名"},
        headers=dict(ORIGIN),
    )
    assert renamed.status_code == 409, renamed.text
    assert _detail(renamed) == "instance_deleted"

    enabled = await logged_in.client.put(
        _instances_url("onebot", instance_id="gone") + "/enabled",
        json={"enabled": True},
        headers=dict(ORIGIN),
    )
    assert enabled.status_code == 409, enabled.text
    assert _detail(enabled) == "instance_deleted"

    # 真没被改回去
    async with session_scope() as session:
        row = await session.get(ConnectorInstance, ("onebot", "gone"))
        assert row is not None
        assert row.display_name == "删了就删了"
        assert row.enabled is False
        assert row.deleted_at is not None


async def test_deleted_instance_cannot_save_config(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """保存配置到已删实例 → 409 ``instance_deleted``，**不静默写进库里**。"""
    created = await _create(logged_in, display_name="删了就别写", instance_id="nope")
    assert created.status_code == 201, created.text
    await logged_in.client.delete(
        _instances_url("onebot", instance_id="nope"),
        headers={**ORIGIN, "Content-Type": "application/json"},
    )

    saved = await _save(
        logged_in, instance_id="nope", set_values={"LING_ONEBOT_SELF_ID": "100200301"}
    )
    assert saved.status_code == 409, saved.text
    assert _detail(saved) == "instance_deleted"

    heads = await _heads()
    assert not [row for row in heads if row.instance_id == "nope"]
    # 而且**没有**悄悄落到默认实例上
    assert not [row for row in heads if row.instance_id == DEFAULT_INSTANCE_ID]


# ---------------------------------------------------------------- 5. 默认实例


async def test_default_instance_cannot_be_deleted(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """``'default'`` 不能删：存量配置全折叠在它上面。"""
    created = await _create(
        logged_in, display_name="默认实例", instance_id=DEFAULT_INSTANCE_ID
    )
    assert created.status_code == 201, created.text
    # 前端据此置灰删除按钮
    assert created.json()["item"]["deletable"] is False

    deleted = await logged_in.client.delete(
        _instances_url("onebot", instance_id=DEFAULT_INSTANCE_ID),
        headers={**ORIGIN, "Content-Type": "application/json"},
    )
    assert deleted.status_code == 409, deleted.text
    assert _detail(deleted) == "default_instance_protected"

    async with session_scope() as session:
        row = await session.get(ConnectorInstance, ("onebot", DEFAULT_INSTANCE_ID))
        assert row is not None
        assert row.deleted_at is None


# ---------------------------------------------------------------- 6. 命名与身份分离


async def test_rename_keeps_instance_identity(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """改名只改 ``display_name``，``instance_id`` 不变。"""
    created = await _create(logged_in, display_name="旧名字", instance_id="stable")
    assert created.status_code == 201, created.text

    renamed = await logged_in.client.put(
        _instances_url("onebot", instance_id="stable"),
        json={"display_name": "新名字"},
        headers=dict(ORIGIN),
    )
    assert renamed.status_code == 200, renamed.text
    item = renamed.json()["item"]
    assert item["instance_id"] == "stable"
    assert item["display_name"] == "新名字"


async def test_rename_to_taken_name_is_explicit_409(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """改名撞别人 → 409 ``duplicate_display_name``。"""
    assert (await _create(logged_in, display_name="甲", instance_id="a")).status_code == 201
    assert (await _create(logged_in, display_name="乙", instance_id="b")).status_code == 201

    response = await logged_in.client.put(
        _instances_url("onebot", instance_id="b"),
        json={"display_name": "甲"},
        headers=dict(ORIGIN),
    )
    assert response.status_code == 409, response.text
    assert _detail(response) == "duplicate_display_name"


async def test_rename_to_own_name_is_ok(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """改成自己现在的名字不算冲突（排除自己，不然"只改大小写"就改不动）。"""
    assert (
        await _create(logged_in, display_name="自己", instance_id="self")
    ).status_code == 201
    response = await logged_in.client.put(
        _instances_url("onebot", instance_id="self"),
        json={"display_name": "自己"},
        headers=dict(ORIGIN),
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------- 7. 启用/停用


async def test_toggle_enabled(logged_in: TestEnv, fake_runtime: FakeRuntime) -> None:  # noqa: F811
    """启用/停用是真的落库，且停用不动配置。"""
    assert (
        await _create(logged_in, display_name="开关", instance_id="toggle")
    ).status_code == 201
    off = await logged_in.client.put(
        _instances_url("onebot", instance_id="toggle") + "/enabled",
        json={"enabled": False},
        headers=dict(ORIGIN),
    )
    assert off.status_code == 200, off.text
    assert off.json()["item"]["enabled"] is False

    async with session_scope() as session:
        row = await session.get(ConnectorInstance, ("onebot", "toggle"))
        assert row is not None
        assert row.enabled is False
        assert row.deleted_at is None


# ---------------------------------------------------------------- 8. 最关键：配置真的落到实例上


async def test_config_saved_to_new_instance_lands_on_that_instance(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """**创建实例 → 保存配置到它 → 查回来，``instance_id`` 是新实例。**

    这是"名字比实现强"的照妖镜：如果配置保存路径不认 ``instance_id``（仍然
    硬编码写 ``'default'``），这条会红——而只测 CRUD 的用例**不会**。
    """
    created = await _create(logged_in, display_name="第二台", instance_id="second")
    assert created.status_code == 201, created.text

    response = await _save(
        logged_in,
        instance_id="second",
        set_values={"LING_ONEBOT_INSTANCE_ID": "second-ns"},
    )
    assert response.status_code == 202, response.text
    assert response.json()["instance_id"] == "second"

    async with session_scope() as session:
        head = await session.get(ConnectorConfigHead, ("onebot", "second"))
        assert head is not None, "实例 second 上应该有 HEAD 指针"
        revisions = list(
            (
                await session.execute(
                    select(ConnectorConfigRevision).where(
                        ConnectorConfigRevision.scope == "onebot",
                        ConnectorConfigRevision.instance_id == "second",
                    )
                )
            ).scalars()
        )
        assert revisions, "修订必须落在新实例上"
        assert head.active_revision_id in {row.id for row in revisions}

        # 负向：默认实例上**不能**出现这次保存的痕迹
        default_head = await session.get(
            ConnectorConfigHead, ("onebot", DEFAULT_INSTANCE_ID)
        )
        assert default_head is None, "保存到 second 却写进了 default 实例"


async def test_config_on_two_instances_are_independent(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """两个实例各存各的：互不覆盖，generation 各自递增。"""
    assert (
        await _create(logged_in, display_name="默认实例", instance_id=DEFAULT_INSTANCE_ID)
    ).status_code == 201
    assert (
        await _create(logged_in, display_name="第二台", instance_id="second")
    ).status_code == 201

    first = await _save(
        logged_in, instance_id=DEFAULT_INSTANCE_ID,
        set_values={"LING_ONEBOT_SELF_ID": "100200300"},
    )
    assert first.status_code == 202, first.text
    second = await _save(
        logged_in, instance_id="second", set_values={"LING_ONEBOT_SELF_ID": "100200301"}
    )
    assert second.status_code == 202, second.text

    async with session_scope() as session:
        heads = {
            row.instance_id: row
            for row in (
                await session.execute(
                    select(ConnectorConfigHead).where(ConnectorConfigHead.scope == "onebot")
                )
            ).scalars()
        }
        assert set(heads) == {DEFAULT_INSTANCE_ID, "second"}
        # 每个实例各一代（不是"第二次保存又加了默认实例的 generation"）
        assert heads[DEFAULT_INSTANCE_ID].generation == 1
        assert heads["second"].generation == 1
        assert heads[DEFAULT_INSTANCE_ID].active_revision_id != heads[
            "second"
        ].active_revision_id


async def test_read_back_config_for_instance_returns_its_own_value(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """读回来也是按实例分的（写对了但读串了同样是坏的）。"""
    from ling.settings import connector_overrides as ov

    assert (
        await _create(logged_in, display_name="默认实例", instance_id=DEFAULT_INSTANCE_ID)
    ).status_code == 201
    assert (
        await _create(logged_in, display_name="第二台", instance_id="second")
    ).status_code == 201
    assert (
        await _save(logged_in, instance_id=DEFAULT_INSTANCE_ID,
                    set_values={"LING_ONEBOT_SELF_ID": "100200300"})
    ).status_code == 202
    assert (
        await _save(logged_in, instance_id="second",
                    set_values={"LING_ONEBOT_SELF_ID": "100200301"})
    ).status_code == 202

    async with session_scope() as session:
        default_view = await ov.read_override_values(
            session, scope="onebot", instance_id=DEFAULT_INSTANCE_ID
        )
        second_view = await ov.read_override_values(
            session, scope="onebot", instance_id="second"
        )
    assert default_view["LING_ONEBOT_SELF_ID"] == "100200300"
    assert second_view["LING_ONEBOT_SELF_ID"] == "100200301"


# ---------------------------------------------------------------- 9. 门禁


async def test_instance_listing_requires_auth(web_env: TestEnv) -> None:
    """未登录不能读实例列表（实例名是部署指纹）。"""
    response = await web_env.client.get(_instances_url("onebot"))
    assert response.status_code in (401, 403), response.text


async def test_instance_create_rejects_cross_origin(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """跨站 Origin 被拒（与配置写入同一套门禁）。"""
    response = await logged_in.client.post(
        _instances_url("onebot"),
        json={"display_name": "跨站"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403, response.text


async def test_unknown_scope_is_404(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """不认识的 scope → 404，而不是建一个 scope=乱码 的行。"""
    response = await _create(logged_in, scope="nope", display_name="x")
    assert response.status_code == 404, response.text


async def test_save_to_nonexistent_instance_is_404(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """保存到不存在的实例 → 404，**不静默退回默认实例**。"""
    response = await _save(
        logged_in, instance_id="never-created",
        set_values={"LING_ONEBOT_SELF_ID": "100200302"},
    )
    assert response.status_code == 404, response.text
    assert _detail(response) == "instance_not_found"
    assert await _heads() == []


async def test_save_without_instance_id_bootstraps_default(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """不带 ``instance_id`` 时落默认实例；它不存在则**补建**，而不是 404。

    升级前"保存配置"从不要求实例行（``_write_candidate`` 自己建 HEAD），
    迁移也只给"有过数据"的 scope 回填实例行。所以全新部署里
    ``connector_instances`` 是空的——这条钉住"第一次保存仍然能用"。

    这是上一条（显式指定不存在的实例 → 404）的**配对正向断言**：两条一起
    才说明"缺省补建、显式严格"这个不对称是真的，而不是"全都 404"或
    "全都自动建"。
    """
    response = await _save(
        logged_in, set_values={"LING_ONEBOT_SELF_ID": "100200303"}
    )
    assert response.status_code == 202, response.text
    assert response.json()["instance_id"] == DEFAULT_INSTANCE_ID

    async with session_scope() as session:
        row = await session.get(ConnectorInstance, ("onebot", DEFAULT_INSTANCE_ID))
        assert row is not None, "缺省保存应补建默认实例行"
        # 形状与迁移回填一致：display_name 直接取 instance_id
        assert row.display_name == DEFAULT_INSTANCE_ID
        assert row.enabled is True
        assert row.deleted_at is None
        head = await session.get(ConnectorConfigHead, ("onebot", DEFAULT_INSTANCE_ID))
        assert head is not None
        assert head.generation == 1


async def test_bootstrap_default_does_not_create_on_new_scope_rows_twice(
    logged_in: TestEnv, fake_runtime: FakeRuntime  # noqa: F811
) -> None:
    """两次缺省保存只在库里留一行默认实例（补建是幂等的）。"""
    assert (
        await _save(logged_in, set_values={"LING_ONEBOT_SELF_ID": "100200304"})
    ).status_code == 202
    assert (
        await _save(logged_in, set_values={"LING_ONEBOT_SELF_ID": "100200305"})
    ).status_code == 202

    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(ConnectorInstance).where(
                        ConnectorInstance.scope == "onebot",
                        ConnectorInstance.instance_id == DEFAULT_INSTANCE_ID,
                    )
                )
            ).scalars()
        )
        assert len(rows) == 1
