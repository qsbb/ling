"""QQ 连接器的入站适配与去重（W1：已实现，未测试）。

职责：
- 把 QQ 回调 payload 落成 ling 的统一入站事件。
- 用 ``(connector_instance, external_event_id)`` 去重（同库事务）。
- 把 QQ 账号解析到已证明的 ``subject_id``；未绑定则进入"待绑定"队列，不猜。

**不做的事**：不写角色人格、不写长期记忆、不直接发消息。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from connectors.qq.connector import QQConnector
from ling.contracts.v1 import InteractionEnvelope
from ling.domains.conversation.service import ConversationService
from ling.domains.identity.service import IdentityService
from ling.infra.models import utcnow
from ling.infra.security import new_id


@dataclass(frozen=True)
class InboundResult:
    accepted: bool
    duplicate: bool
    envelope: InteractionEnvelope | None
    subject_id: str | None
    reason: str | None = None


class QQIngress:
    """QQ 入站处理器。

    W1 边界：本类不与真实平台联调，也不在 W0 回归中被调用。
    """

    def __init__(
        self,
        session: AsyncSession,
        connector: QQConnector,
        *,
        actor_id: str = "actor_ling",
        persona_id: str = "persona_ling",
    ) -> None:
        self._session = session
        self._connector = connector
        self._conversation = ConversationService(
            session, actor_id=actor_id, persona_id=persona_id
        )
        self._identity = IdentityService(session)

    async def handle(self, headers: dict[str, str], body: bytes) -> InboundResult:
        # 1) 签名与重放
        if not await self._connector.verify_inbound(headers, body):
            return InboundResult(
                accepted=False,
                duplicate=False,
                envelope=None,
                subject_id=None,
                reason="签名校验失败或未配置 webhook 公钥（默认拒绝）",
            )

        # 2) 解析
        import json

        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return InboundResult(False, False, None, None, "payload 不是合法 JSON")

        try:
            envelope = self._connector.normalize_inbound(payload)
        except Exception as exc:  # noqa: BLE001 - 渠道错误统一转成拒绝原因
            return InboundResult(False, False, None, None, f"归一化失败: {exc}")

        # 3) 去重：同 (connector_instance, external_event_id) 只处理一次
        scope = f"connector:{self._connector.instance_id}"
        existing = await self._conversation.claim_event(
            scope=scope, key=envelope.external_event_id
        )
        if existing is not None:
            return InboundResult(
                accepted=True,
                duplicate=True,
                envelope=envelope,
                subject_id=None,
                reason="重复事件，已忽略",
            )

        # 4) 身份解析：渠道账号 → subject。未绑定不猜，进入待绑定。
        subject_id = await self._identity.resolve_account(
            self._connector.instance_id, envelope.account_ref or ""
        )
        if subject_id is None:
            return InboundResult(
                accepted=True,
                duplicate=False,
                envelope=envelope,
                subject_id=None,
                reason="QQ 账号尚未绑定到已证明的自然人，需走绑定流程",
            )

        # 5) 确保窗口存在（私聊/群聊各自独立窗口）
        window = await self._ensure_window(
            subject_id=subject_id,
            window_ref=envelope.window_ref,
            is_group=envelope.window_ref.startswith("qq:group:"),
        )
        envelope = envelope.model_copy(update={"window_ref": window.id})
        return InboundResult(
            accepted=True, duplicate=False, envelope=envelope, subject_id=subject_id
        )

    async def _ensure_window(self, *, subject_id: str, window_ref: str, is_group: bool):
        """按渠道窗口标识复用窗口；不存在则创建。

        群聊窗口的 visibility 是 ``group_scope``，私聊是 ``subject_private``：
        这是受众隔离的落点，不允许把群聊内容写进私聊窗口。
        """
        from sqlalchemy import select

        from ling.infra.models import Window

        existing = (
            await self._session.execute(
                select(Window).where(
                    Window.subject_id == subject_id,
                    Window.channel == "qq",
                    Window.title == window_ref,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        window = Window(
            id=new_id("win"),
            actor_id=self._conversation._actor_id,  # noqa: SLF001 - 同域内部约定
            persona_id=self._conversation._persona_id,  # noqa: SLF001
            subject_id=subject_id,
            title=window_ref,
            channel="qq",
            visibility="group_scope" if is_group else "subject_private",
            last_message_at=utcnow(),
        )
        self._session.add(window)
        await self._session.flush()
        return window


__all__ = ["InboundResult", "QQIngress"]
