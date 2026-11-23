"""Resolve the current, proven target of an opted-in private OneBot window."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession

from ling.config import Settings
from ling.domains.identity.service import IdentityService
from ling.infra.models import Window
from ling.kernel.access import check_access
from ling.settings.connector_overrides import current_access_snapshot

_QQ_ACCOUNT = re.compile(r"[1-9][0-9]{0,19}")


async def current_onebot_target(
    session: AsyncSession, *, window_id: str, subject_id: str, settings: Settings | None = None,
) -> dict[str, str] | None:
    window = await session.get(Window, window_id, populate_existing=True)
    if (
        window is None or window.channel != "onebot" or window.visibility == "group_scope"
        or window.archived or not window.onebot_defer_enabled or window.subject_id != subject_id
    ):
        return None
    origin = window.trusted_instance_key
    if origin is None or origin[0] != "onebot":
        return None
    namespace, sep, account = window.platform_reference.rpartition(":private:")
    if not namespace or not sep or _QQ_ACCOUNT.fullmatch(account) is None:
        return None
    if await IdentityService(session).resolve_account(namespace, account) != subject_id:
        return None
    access = await current_access_snapshot(session, settings=settings)
    if not check_access(account, mode=access.mode, allow_list=access.allow_list).allowed:
        return None
    return {"instance_id": origin[1], "window_ref": window.platform_reference}
