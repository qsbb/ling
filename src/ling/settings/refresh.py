"""跨进程配置刷新：让每个进程都能拿到最新的密钥与绑定。

**为什么需要**：密钥缓存是进程内存（provider 构造时要同步读取）。
默认部署有两个 API 副本 + 一个 worker，配置变更只刷新命中的那个进程——
其余进程继续用旧密钥，worker 更是只在启动时预热一次。

实测表现：换密钥后一部分请求成功、一部分报 `secret_missing`，
worker 的记忆提炼静默退回正则。用户感知是"它有时能聊有时不能，还记不住话"。

做法：按**配置修订号**懒刷新——每个进程记住自己加载到哪一版，
处理请求前比对一次修订号（一个很小的查询），变了就重载。
比定时轮询简单，也不会在无请求时白跑。
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.infra.models import ConfigRevision

logger = logging.getLogger(__name__)


async def current_revision(session: AsyncSession) -> int:
    """当前**生效**的配置修订号（只算 ``published``，没有已发布时为 0）。

    为什么必须过滤 ``state``：草稿也占修订号，但草稿从未生效。
    按最大值取的话，只要有人在设置页存过一次草稿（没发布），
    标记就被抬到一个"发布也不会再变"的数字上：进程刷新一轮后把
    ``loaded_revision`` 记成草稿号，之后正式发布只改行 state、不新增号，
    于是 ``stale()`` 永远是 False——**所有副本与 worker 从此不再重载**。

    迁移：旧实现记录的是"含草稿的最大号"，可能大于已发布号。这不是问题——
    每个进程的 ``loaded_revision`` 都是启动时（``app.py``）或刷新后
    （``chat.py``）用**本函数当前语义**算出来再写入的，代码升级必然伴随重启，
    不会留下旧语义的值。比较用 ``!=`` 而不是 ``>``：回滚会把一个更旧的号
    重新发布，号会**变小**，必须同样被感知。收敛性：刷新后 ``mark_loaded``
    写入同一个号，下一次比对即相等，不会反复重载。
    """
    value = (
        await session.execute(
            select(ConfigRevision.revision)
            .where(ConfigRevision.state == "published")
            .order_by(ConfigRevision.revision.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return int(value or 0)


async def refresh_secrets(
    session: AsyncSession, *, cache: dict[str, str], box: object,
    persona_id: str | None = None,
    test_mode_default: bool, diagnosis: dict[str, str] | None = None,
) -> int:
    """把已发布的密钥重新解密进 ``cache``（原地更新）。

    解不开的密钥**保留旧值**而不是清空：一次解密失败不该让整个进程失能。
    返回本次写入的条目数。
    """
    from ling.settings.service import ConfigService

    svc = ConfigService(
        session, box, test_mode_default=test_mode_default, persona_id=persona_id  # type: ignore[arg-type]
    )
    effective = await svc.effective()
    written = 0
    for pid in effective.providers:
        # 先记诊断（区分"没存过"与"解不开"），再取密钥
        if diagnosis is not None:
            try:
                diagnosis[pid] = await svc.secret_diagnosis(pid)
            except Exception:  # noqa: BLE001
                diagnosis[pid] = "missing"
        try:
            secret = await svc.read_secret(pid)
        except Exception as exc:  # noqa: BLE001 - 单个 provider 失败不影响其他
            logger.info("刷新密钥跳过 %s: %s", pid, type(exc).__name__)
            continue
        if secret:
            cache[pid] = secret
            written += 1
    return written


class RevisionWatcher:
    """记录"本进程加载到哪一版"，供各处按需比对。"""

    def __init__(self) -> None:
        self.loaded_revision = -1

    async def stale(self, session: AsyncSession) -> bool:
        latest = await current_revision(session)
        return latest != self.loaded_revision

    def mark_loaded(self, revision: int) -> None:
        self.loaded_revision = revision


__all__ = ["RevisionWatcher", "current_revision", "refresh_secrets"]
