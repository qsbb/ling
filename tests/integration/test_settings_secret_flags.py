"""密钥状态必须真的落进修订 payload（``has_secret`` / ``secret_last4``）。

## 这条防的是什么

``ConfigService._sync_secret_flags`` 原本用 ``dict(row.payload)`` 做浅拷贝，
再原地改 ``providers`` 列表里的内层 dict。``ConfigRevision.payload`` 是普通
``JSON`` 列（**没有** ``MutableDict``），SQLAlchemy 判"变没变"看的是
"有没有被赋新对象 + 内容是否相等"——而内容因为共享同一批内层 dict 已经
"相等"，于是**不产生 UPDATE**。

后果全部是静默的：

- ``has_secret`` / ``secret_last4`` 永远停在旧值；
- ``validate_payload`` 里 ``has_secret`` 的 warning 永远为真——用户明明
  配了密钥，系统一直提示"该供应商还没有保存密钥"；
- 前端"已配置/未配置"状态错误；
- ``_bump_for_secret_change`` deepcopy 的是**没同步进去**的 payload，于是
  密钥变更造出来的新已发布修订里标记同样是错的。

## 为什么必须**重新开 session 从库里读**

同一 session 内的 ORM 对象是进程内缓存：即使没有 UPDATE，内存里的
``row.payload`` 也可能已经是新值（浅拷贝后内层 dict 被原地改过）。
只看内存会得到假绿。所以每条断言都跨 ``session_scope()`` 重读。

注意 ``tests/integration/test_w0_web.py`` 里那条 ``has_secret is True``
的断言是绿的，但**不覆盖这里**：它走 API 回显，``settings.py`` 直接查
``secret_status()``（凭据表），不读修订 payload。

变异验证：把 ``_sync_secret_flags`` 换回浅拷贝（``dict(row.payload)`` +
原地改 ``entry`` + ``row.payload = {**payload, "providers": providers}``），
``test_set_secret_persists_flags`` 与 ``test_secret_bump_revision_carries_flags``
必须 FAIL。
"""

from __future__ import annotations

import base64

import pytest
from sqlalchemy import select

from ling.infra.db import session_scope
from ling.infra.models import ConfigRevision
from ling.settings.service import ConfigService
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

PROVIDER = "p1"
SECRET = "sk-test-123"


class _Box:
    """可控的加解密容器：测试只关心"能不能解密出密钥"，不关心算法。"""

    def encrypt(self, plaintext: str) -> str:
        return base64.b64encode(b"x" * 12 + plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return base64.b64decode(ciphertext)[12:].decode()


def _svc(session) -> ConfigService:
    return ConfigService(session, _Box(), test_mode_default=True, persona_id="persona_ling")


async def _publish_baseline() -> int:
    """发布一份带 provider 的基线配置，返回其修订号。"""
    async with session_scope() as session:
        svc = _svc(session)
        draft = await svc.save_draft(
            providers=[
                {
                    "provider_id": PROVIDER,
                    "kind": "openai_compatible",
                    "base_url": "https://example.test/v1",
                }
            ],
            note="baseline",
        )
        return await svc.publish(draft.revision)


async def _flags_in_db(revision: int) -> tuple[bool, str]:
    """**新开 session** 从库里读出某个修订里 provider 的密钥标记。"""
    async with session_scope() as session:
        row = (
            await session.execute(
                select(ConfigRevision).where(ConfigRevision.revision == revision)
            )
        ).scalar_one()
        for entry in (row.payload or {}).get("providers") or []:
            if entry.get("provider_id") == PROVIDER:
                return bool(entry.get("has_secret")), str(entry.get("secret_last4") or "")
    raise AssertionError(f"修订 {revision} 里没有 provider {PROVIDER}")


async def _published_flags() -> tuple[int, bool, str]:
    async with session_scope() as session:
        row = (
            await session.execute(
                select(ConfigRevision)
                .where(ConfigRevision.state == "published")
                .order_by(ConfigRevision.revision.desc())
                .limit(1)
            )
        ).scalar_one()
        for entry in (row.payload or {}).get("providers") or []:
            if entry.get("provider_id") == PROVIDER:
                return row.revision, bool(entry.get("has_secret")), str(
                    entry.get("secret_last4") or ""
                )
    raise AssertionError(f"已发布修订里没有 provider {PROVIDER}")


async def test_set_secret_persists_flags(web_env: TestEnv) -> None:
    """核心回归：``set_secret`` 之后库里读到的 ``has_secret`` 必须是 True。

    浅拷贝版在这里返回 ``False``：UPDATE 根本没发生，库里的 payload 没变。
    """
    published = await _publish_baseline()
    assert await _flags_in_db(published) == (False, ""), "基线前提：还没存密钥"

    async with session_scope() as session:
        await _svc(session).set_secret(PROVIDER, SECRET)

    # 原修订（已 superseded）与新造的密钥变更修订都必须同步
    assert await _flags_in_db(published) == (True, SECRET[-4:]), (
        "set_secret 之后库里的 has_secret 仍是 False：密钥状态没写进 payload"
    )
    revision, has_secret, last4 = await _published_flags()
    assert (has_secret, last4) == (True, SECRET[-4:]), (
        f"已发布修订 {revision} 的 has_secret 仍是 {has_secret}"
    )


async def test_delete_secret_clears_flags(web_env: TestEnv) -> None:
    """``delete_secret`` 之后库里 ``has_secret`` 必须变回 False，``last4`` 清空。"""
    await _publish_baseline()
    async with session_scope() as session:
        await _svc(session).set_secret(PROVIDER, SECRET)
    async with session_scope() as session:
        assert (await _published_flags())[1:] == (True, SECRET[-4:])

    async with session_scope() as session:
        await _svc(session).delete_secret(PROVIDER)

    async with session_scope() as session:
        _, has_secret, last4 = await _published_flags()
    assert (has_secret, last4) == (False, ""), "删密钥后 payload 里的标记没清掉"


async def test_secret_bump_revision_carries_flags(web_env: TestEnv) -> None:
    """A5 联动：``set_secret`` 新造的已发布修订里 ``has_secret`` 也必须是 True。

    ``_bump_for_secret_change`` 的 ``deepcopy`` 取自**同步之后**的已发布修订；
    同步没落库的话，bump 出来的是"看起来一切正常、其实没有密钥"的修订，
    而所有其他副本与 worker 恰恰读的是这一版。

    变异验证：回退成浅拷贝后这条 FAIL——新修订里是 False。
    """
    await _publish_baseline()
    async with session_scope() as session:
        before_rev = await _svc(session).published_revision()
        assert before_rev is not None
        before_number = before_rev.revision

    async with session_scope() as session:
        await _svc(session).set_secret(PROVIDER, SECRET)

    async with session_scope() as session:
        new_rev = await _svc(session).published_revision()
        assert new_rev is not None
    assert new_rev.revision > before_number, "密钥变更必须新造一个已发布修订（前提没变）"

    has_secret, last4 = await _flags_in_db(new_rev.revision)
    assert (has_secret, last4) == (True, SECRET[-4:]), (
        f"密钥变更修订 {new_rev.revision} 里的 has_secret 是 {has_secret}："
        "bump 时 deepcopy 拿到的是没同步进去的 payload"
    )


async def test_draft_also_synced(web_env: TestEnv) -> None:
    """同步要覆盖**草稿**修订：浅拷贝版对草稿同样不产生 UPDATE。"""
    await _publish_baseline()
    async with session_scope() as session:
        draft = await _svc(session).save_draft(note="草稿也带 provider")
    async with session_scope() as session:
        await _svc(session).set_secret(PROVIDER, SECRET)

    has_secret, last4 = await _flags_in_db(draft.revision)
    assert (has_secret, last4) == (True, SECRET[-4:]), "草稿修订里的密钥标记没同步"
