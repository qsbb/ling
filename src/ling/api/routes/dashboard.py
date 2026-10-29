"""仪表盘：一次请求返回首屏需要的所有片段。

## 为什么是一个聚合接口

首屏要显示六七块数据（她此刻在干嘛、情绪、有没有想找你、最近记住什么、
关系、渠道通不通）。如果每块各发一个请求，首屏就会被最慢的那块拖住，
而且任何一块失败都会让整页变成错误页。

所以这里一次返回，并且**每块单独标状态**：某块查失败只影响它自己。

## 分段结构

```json
{
  "schema_version": "v1",
  "generated_at": "...",
  "timezone": "Asia/Shanghai",
  "sections": {
    "activity":   {"status": "ok|empty|unavailable", "as_of": "...", "data": {...}},
    ...
  }
}
```

`status` 三态的意义：

- `ok`：查到了且非空
- `empty`：查到了，但确实没有内容（"她此刻没有登记中的活动"）
- `unavailable`：查询本身失败

**空数据和查询失败不能混在一起**：前者是事实，后者是故障。混在一起会让
"她没在做什么"和"读不到她在做什么"长得一模一样，而这两件事的处理方式完全不同。

`as_of` 是这块数据的采集时刻，前端据此标注"最近更新于"，不要写成"实时"。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter

from ling.api.deps import ConfigDep, CurrentAuth, DbSession, SettingsDep
from ling.domains.activity.service import ActivityService
from ling.domains.affect.service import AffectService
from ling.domains.memory.service import MemoryService
from ling.domains.proactive.service import ProactivityService
from ling.domains.relationship.service import RelationshipService

logger = logging.getLogger("ling.api.dashboard")

router = APIRouter(tags=["dashboard"])

# 首屏只需要摘要，不需要整页数据
MEMORY_LIMIT = 3
SECTION_OK = "ok"
SECTION_EMPTY = "empty"
SECTION_UNAVAILABLE = "unavailable"


def _section(status: str, data: Any = None) -> dict:
    return {
        "status": status,
        "as_of": datetime.now(UTC).isoformat(),
        "data": data,
    }


async def _guard(name: str, coro: Any, session: Any) -> dict:
    """跑一个片段的查询；失败只标记这一块，不影响整页。

    **失败后必须 rollback**：所有片段共用同一个请求级 session，一条失败的
    查询会让它进入"需要回滚"状态，后面每一块都会跟着报错——表面上像
    "每块都读不到"，实际只坏了一块（实测在部署实例上：记忆那块的失败把
    会话搞脏，于是模型绑定也被读成空）。
    """
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - 单块失败不能拖垮首屏
        logger.warning(
            "仪表盘片段 %s 查询失败：%s: %s", name, type(exc).__name__, exc
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 - 回滚都失败就交给上层的请求生命周期
            logger.warning("仪表盘片段 %s 回滚失败", name)
        return _section(SECTION_UNAVAILABLE)


@router.get("/dashboard")
async def get_dashboard(
    auth: CurrentAuth,
    session: DbSession,
    settings: SettingsDep,
    config: ConfigDep,
) -> dict:
    actor_id = settings.actor_id
    subject_id = auth.user.subject_id

    async def activity() -> dict:
        current = await ActivityService(session, actor_id=actor_id).current()
        if current is None:
            return _section(SECTION_EMPTY)
        return _section(
            SECTION_OK,
            {
                "activity_id": current.activity_id,
                "kind": current.kind,
                "state": current.state,
                "place_id": current.place_id,
                # 来源必须带上：simulated（她自己的生活模拟）与 tool_receipt
                # （真实回执）在界面上不能长得一样
                "origin": current.origin,
                "started_at": current.started_at.isoformat()
                if current.started_at
                else None,
            },
        )

    async def affect() -> dict:
        view = await AffectService(session, actor_id=actor_id).current()
        return _section(
            SECTION_OK,
            {
                "emotion": view.emotion,
                "intensity": view.intensity,
                "cause": view.cause,
                "tone_hint": view.tone_hint(),
                "updated_at": view.updated_at.isoformat() if view.updated_at else None,
            },
        )

    async def proactive() -> dict:
        service = ProactivityService(session, actor_id=actor_id)
        pending = await service.pending(subject_id)

        # **不能写死 delivery_enabled**：原来写死 False，于是门控永远说
        # "当前渠道不支持主动投递"，无论渠道配得多完整都不可能主动联系。
        delivery_enabled = False
        gate_reason = "未探测渠道能力"
        try:
            from ling.capabilities.channels import proactive_connector_status

            connector, reason = proactive_connector_status()
            delivery_enabled = connector is not None
            gate_reason = reason or ""
        except Exception as exc:  # noqa: BLE001 - 探测失败按不可投递
            gate_reason = f"渠道探测失败（{type(exc).__name__}）"

        if not pending:
            return _section(SECTION_EMPTY, {"delivery_enabled": delivery_enabled,
                                            "gate_reason": gate_reason})
        top = pending[0]
        return _section(
            SECTION_OK,
            {
                "pending_count": len(pending),
                "delivery_enabled": delivery_enabled,
                "gate_reason": gate_reason,
                # 候选不是"已经联系过"，文案必须区分
                "top": {
                    "opportunity_id": top.opportunity_id,
                    "kind": top.kind,
                    "reason": top.reason,
                    "state": top.state,
                    # None = 还没生成（不是"生成了空的"），前端要能区分
                    "draft_text": top.draft_text,
                    "expires_at": top.expires_at.isoformat(),
                },
            },
        )

    async def memories() -> dict:
        rows = await MemoryService(session).list_memories(
            subject_id, limit=MEMORY_LIMIT
        )
        live = [r for r in rows if not r.retracted]
        if not live:
            return _section(SECTION_EMPTY)
        return _section(
            SECTION_OK,
            {
                "items": [
                    {
                        "memory_id": r.id,
                        "kind": r.kind,
                        "text": r.text,
                        "origin": r.origin,
                        "confidence": r.confidence,
                    }
                    for r in live
                ]
            },
        )

    async def relationship() -> dict:
        service = RelationshipService(session, actor_id=actor_id)
        state = await service.state(subject_id)
        view = service.view(state)
        return _section(
            SECTION_OK,
            {
                "familiarity": view.familiarity,
                "trust": view.trust,
                "boundaries": view.boundaries,
                "last_interaction_at": view.last_interaction_at.isoformat()
                if view.last_interaction_at
                else None,
            },
        )

    async def connectivity() -> dict:
        from ling.capabilities.channels import (
            qqbot_config,
            qqbot_connector_status,
        )

        # **"配置齐备"与"能主动推送"是两件事**。
        # `qqbot_connector_status()` 在"凭据齐全但没申请主动推送"时会返回
        # (None, 原因)——用它判断 configured 会把配好的渠道误报成未配置
        # （实测在部署实例上就报错了）。configured 只看凭据，主动能力单独说。
        channels: list[dict] = []
        try:
            config = qqbot_config()
            _connector, status_reason = qqbot_connector_status()
            channels.append(
                {
                    "channel": "qqbot",
                    "configured": config.configured,
                    "reason": "" if config.configured else "凭据不全（APP_ID / CLIENT_SECRET）",
                    # 主动推送能力是平台权限，不是配置
                    "proactive_ready": _connector is not None,
                    "proactive_reason": status_reason or "",
                }
            )
        except Exception as exc:  # noqa: BLE001
            channels.append(
                {
                    "channel": "qqbot",
                    "configured": False,
                    "reason": f"探测失败（{type(exc).__name__}）",
                }
            )

        # OneBot 的配置是纯环境变量，没有"连接器对象"可探测——这里只报
        # 配置是否齐备，**不冒充连通性**（真正能不能收消息只有日志知道）。
        onebot_configured = bool(
            settings.onebot_ws_port or settings.onebot_ws_url or settings.onebot_ws_token
        )
        channels.append(
            {
                "channel": "onebot",
                "configured": onebot_configured,
                "reason": "" if onebot_configured else "未配置 LING_ONEBOT_*",
            }
        )

        # 模型职责有没有绑定：没绑 = 她说不出话，这是首屏必须能看出来的
        bound: list[str] = []
        try:
            effective = await config.effective()
            bound = [
                name
                for name, binding in effective.profiles.items()
                if getattr(binding, "provider_id", None)
            ]
        except Exception as exc:  # noqa: BLE001 - 读不到就按未绑定报
            # 不静默：这条一旦吞掉，界面会显示"没绑定模型"，
            # 而真实原因是读配置出错——两者排查方向完全相反。
            logger.warning(
                "仪表盘读模型绑定失败：%s: %s", type(exc).__name__, exc
            )
            bound = []

        return _section(
            SECTION_OK,
            {"channels": channels, "bound_model_roles": sorted(bound)},
        )

    sections = {
        "activity": await _guard("activity", activity(), session),
        "affect": await _guard("affect", affect(), session),
        "proactive": await _guard("proactive", proactive(), session),
        "memories": await _guard("memories", memories(), session),
        "relationship": await _guard("relationship", relationship(), session),
        "connectivity": await _guard("connectivity", connectivity(), session),
    }

    return {
        "schema_version": "v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "timezone": settings.user_timezone or "UTC",
        "sections": sections,
    }
