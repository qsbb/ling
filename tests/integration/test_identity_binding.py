"""渠道账号绑定：绑定 / 解绑 / 误绑拆分 / 证明等级。

这些逻辑不需要真实 QQ/微信账号——用合成 connector_instance +
external_account_id 就能完整验证。状态文档里"绑定/解绑/误绑拆分"的
专项缺口由此补上。

核心安全不变量（设计文档 §200）：
- 渠道账号解析到 natural person 必须基于**已证明**的绑定
- 同一渠道账号不能同时属于两个人（误绑必须拆分，不猜）
- 未证明的绑定（proof=pending）只能待确认，不能拿到身份
"""

from __future__ import annotations

import pytest

from ling.api.errors import LingError
from ling.domains.identity.service import IdentityService
from ling.infra.db import session_scope
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

CONNECTOR = "qq:test-bot"
OTHER_CONNECTOR = "wechat:test-bot"


async def _bind(
    subject_id: str, external_id: str, *, proof: str = "proven", connector: str = CONNECTOR
) -> str:
    async with session_scope() as session:
        row = await IdentityService(session).bind_account(
            subject_id=subject_id,
            connector_instance=connector,
            external_account_id=external_id,
            proof=proof,
        )
        return row.id


async def _other_subject() -> str:
    """造第二个自然人（真实写入 User + Subject，满足外键）。"""
    import uuid

    async with session_scope() as session:
        view = await IdentityService(session).create_user(
            username=f"other_{uuid.uuid4().hex[:8]}",
            password="pw-not-used",
        )
        return view.subject_id


async def test_bind_then_resolve(logged_in: TestEnv) -> None:
    await _bind(logged_in.subject_id, "qq_10001")
    async with session_scope() as session:
        resolved = await IdentityService(session).resolve_account(CONNECTOR, "qq_10001")
    assert resolved == logged_in.subject_id


async def test_pending_proof_does_not_resolve(logged_in: TestEnv) -> None:
    """proof=pending 的绑定不能解析成身份。

    bind_account 的文档写"pending 不能用于跨端召回"，
    但最初的 resolve_account 只查了 revoked_at，没查 proof——
    仅"自称"某账号就能拿到该人的 private 记忆。
    """
    await _bind(logged_in.subject_id, "qq_pending", proof="pending")

    async with session_scope() as session:
        resolved = await IdentityService(session).resolve_account(CONNECTOR, "qq_pending")
    assert resolved is None, "未完成控制证明的绑定被当成了真实身份"

    # 但能查到"存在但未证明"，供待绑定队列展示
    async with session_scope() as session:
        proof = await IdentityService(session).binding_proof(CONNECTOR, "qq_pending")
    assert proof == "pending"


async def test_admin_proof_resolves(logged_in: TestEnv) -> None:
    await _bind(logged_in.subject_id, "qq_admin", proof="admin")
    async with session_scope() as session:
        resolved = await IdentityService(session).resolve_account(CONNECTOR, "qq_admin")
    assert resolved == logged_in.subject_id


async def test_rebinding_same_subject_is_idempotent(logged_in: TestEnv) -> None:
    first = await _bind(logged_in.subject_id, "qq_20002", proof="pending")
    second = await _bind(logged_in.subject_id, "qq_20002", proof="proven")
    assert first == second, "同一账号重复绑定产生了新记录"
    async with session_scope() as session:
        resolved = await IdentityService(session).resolve_account(CONNECTOR, "qq_20002")
    assert resolved == logged_in.subject_id, "重新绑定后应可解析"


async def test_binding_another_person_raises_conflict(logged_in: TestEnv) -> None:
    """误绑拆分：一个渠道账号不能同时属于两个人。"""
    await _bind(logged_in.subject_id, "qq_30003")
    other = await _other_subject()
    with pytest.raises(LingError) as exc:
        await _bind(other, "qq_30003")
    assert exc.value.status_code == 409
    assert "已绑定到其他自然人" in exc.value.message

    # 原绑定不受影响（冲突操作不能破坏既有状态）
    async with session_scope() as session:
        resolved = await IdentityService(session).resolve_account(CONNECTOR, "qq_30003")
    assert resolved == logged_in.subject_id


async def test_revoked_binding_does_not_resolve(logged_in: TestEnv) -> None:
    await _bind(logged_in.subject_id, "qq_40004")
    async with session_scope() as session:
        service = IdentityService(session)
        accounts = await service.list_accounts(logged_in.subject_id)
        account = next(a for a in accounts if a.external_account_id == "qq_40004")
        await service.revoke_account(account.id)

    async with session_scope() as session:
        resolved = await IdentityService(session).resolve_account(CONNECTOR, "qq_40004")
    assert resolved is None, "解绑后仍能解析身份"


async def test_revoke_then_rebind_restores(logged_in: TestEnv) -> None:
    """解绑后可以重新绑定（不能永久锁死）。"""
    await _bind(logged_in.subject_id, "qq_50005")
    async with session_scope() as session:
        service = IdentityService(session)
        accounts = await service.list_accounts(logged_in.subject_id)
        account = next(a for a in accounts if a.external_account_id == "qq_50005")
        await service.revoke_account(account.id)

    await _bind(logged_in.subject_id, "qq_50005")
    async with session_scope() as session:
        resolved = await IdentityService(session).resolve_account(CONNECTOR, "qq_50005")
    assert resolved == logged_in.subject_id


async def test_same_external_id_on_different_connectors(logged_in: TestEnv) -> None:
    """不同渠道的同名账号是不同身份（命名空间隔离）。"""
    await _bind(logged_in.subject_id, "10001", connector=CONNECTOR)
    async with session_scope() as session:
        service = IdentityService(session)
        on_qq = await service.resolve_account(CONNECTOR, "10001")
        on_wechat = await service.resolve_account(OTHER_CONNECTOR, "10001")
    assert on_qq == logged_in.subject_id
    assert on_wechat is None, "渠道命名空间被混淆"


async def test_unknown_account_resolves_to_none(logged_in: TestEnv) -> None:
    """未绑定账号不猜身份（进待绑定队列）。"""
    async with session_scope() as session:
        assert await IdentityService(session).resolve_account(CONNECTOR, "qq_nobody") is None
        assert await IdentityService(session).binding_proof(CONNECTOR, "qq_nobody") is None


async def test_ingress_rejects_unproven_binding(logged_in: TestEnv) -> None:
    """QQ ingress 端到端：未证明的绑定不进主链，走待绑定。

    ingress 的注释写"解析到**已证明的** subject_id；未绑定则进入待绑定队列"。
    这条测试证明实现与文档一致——用合成 config，不需要真实 QQ 凭据。
    """
    from connectors.qq.connector import QQConfig, QQConnector
    from connectors.qq.ingress import QQIngress

    await _bind(logged_in.subject_id, "qq_unproven", proof="pending")
    await _bind(logged_in.subject_id, "qq_proven_ok", proof="proven")

    config = QQConfig(app_id="test", app_secret="not-used", instance_id=CONNECTOR)

    async with session_scope() as session:
        ingress = QQIngress(session, QQConnector(config))
        # 未证明 → 解析不到（进待绑定队列）
        assert await ingress._identity.resolve_account(CONNECTOR, "qq_unproven") is None  # noqa: SLF001

    async with session_scope() as session:
        ingress = QQIngress(session, QQConnector(config))
        # 已证明 → 能解析（证明这道门不是"一律拒绝"）
        assert (
            await ingress._identity.resolve_account(CONNECTOR, "qq_proven_ok")  # noqa: SLF001
            == logged_in.subject_id
        )
