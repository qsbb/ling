"""运行日志查询：把本进程内存里的最近 N 条日志读给 Web 控制台。

## 为什么读内存而不读文件

容器里的日志由 stdout 交给 Docker 收集（`deploy/compose.yaml` 的 json-file
驱动），API 进程**看不到** `docker logs`，也读不到宿主机上的 json 日志文件
（容器里没有 Docker socket，也没有挂载 `/var/lib/docker`）。所以只能由进程
自己留一份——见 `ling/logging_setup.py` 里的环形缓冲。

## 这个接口只回答"本进程最近发生了什么"

- 它是**进程内**的：多副本部署时每个副本各有一份，负载均衡会把请求分到
  不同进程。界面上看到的是"某一个副本的日志"，不是全集群的。
- **不读历史**：没有分页、没有游标、没有持久化。缓冲区被覆盖的旧日志再也
  取不回来——要长历史请继续用容器日志采集。界面文案必须照这个说，不能
  暗示这里能翻到更早的记录。
- 条目在**写入缓冲时**就已经脱敏（`redact_mapping` + `redact_value`），
  所以这里的鉴权决定的是"谁能看"，不是"能看到什么明文"。

## 只给 owner / admin

日志里有内部路径、账号标识、第三方地址与凭据尾号。这些对运维有用，对普通
成员是**不必要的信息暴露**。普通成员拿到的是 403，不是空列表——空列表会让
人以为"后端没有日志"，而那是另一回事。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query

from ling.api.deps import CurrentAuth
from ling.api.errors import LingError
from ling.contracts.v1 import ErrorCode
from ling.logging_setup import LEVEL_ORDER, log_buffer_stats, recent_logs

router = APIRouter(tags=["logs"])

# 允许看日志的角色。`admin` 与 `owner` 同等：两者都是运维身份，
# 界面上没有"只给 owner 看日志"的需求（更新通道那种才是 owner 专属）。
LOG_VIEWER_ROLES = frozenset({"owner", "admin"})

LOG_LIMIT_DEFAULT = 200
LOG_LIMIT_MAX = 1000

LogLevel = Literal["debug", "info", "warning", "error"]


@router.get("/logs")
async def list_logs(
    auth: CurrentAuth,
    # 上限 1000 而不是"随便传"：每条都是一段完整消息 + attributes，前端每 2 秒
    # 拉一次，没有上限时一个人开两个标签页就能把接口变成放大攻击面。
    limit: Annotated[
        int,
        Query(description=f"最多返回条数，范围 1..{LOG_LIMIT_MAX}；超出范围返回 422。"),
    ] = LOG_LIMIT_DEFAULT,
    level: Annotated[
        LogLevel | None,
        Query(description="只返回**不低于**该级别的日志；不传表示全部。"),
    ] = None,
    logger: Annotated[
        str | None,
        Query(description="logger 名前缀匹配，例如 ling.api。"),
    ] = None,
    q: Annotated[
        str | None,
        Query(description="对 message 做不区分大小写的子串匹配。"),
    ] = None,
) -> dict[str, Any]:
    """返回本进程最近 `limit` 条日志，**时间正序**。

    `items` 正序（最新在最后）：前端可以直接按顺序渲染并在末尾追加，不必
    自己反转让每一行都 index 变化——那样滚动位置与 key 都会抖。

    `buffered` / `capacity` 一起返回，界面才能说清"你看到的是 200 条里的
    最后 200 条"还是"缓冲已经被写满了"。只给 `items` 长度是不够的：过滤
    之后它已经不反映缓冲的真实占用。
    """
    if auth.user.role not in LOG_VIEWER_ROLES:
        raise LingError(
            ErrorCode.forbidden,
            # 文案要与 `LOG_VIEWER_ROLES` 一致：只说"所有者"的话，管理员被拒
            # 时会以为自己权限不够，而其实是这一页只认这两个角色。
            "只有所有者或管理员可以查看运行日志",
            status_code=403,
            detail="not_log_viewer",
        )

    if limit < 1 or limit > LOG_LIMIT_MAX:
        raise LingError(
            ErrorCode.invalid_request,
            f"limit 必须在 1..{LOG_LIMIT_MAX} 之间",
            status_code=422,
        )

    # `recent_logs` 对未知级别会抛 ValueError；这里的 level 由 Literal 把关，
    # 走不到那条路。**不做 try/except 兜底**：真出现了说明类型与实现脱节，
    # 那是 500 也该暴露的 bug，不是可以吞掉的输入问题。
    entries = recent_logs(limit=limit, level=level, logger=logger, query=q)
    stats = log_buffer_stats()
    return {
        "items": [entry.to_dict() for entry in entries],
        "buffered": stats.buffered,
        "capacity": stats.capacity,
    }


# 允许的级别列表作为模块常量导出：前端下拉框的取值应与后端**同一份**来源，
# 手抄一份到前端就会漂（例如后端加了 critical，前端下拉里没有）。
LEVELS: tuple[str, ...] = tuple(LEVEL_ORDER)

__all__ = ["LEVELS", "LOG_LIMIT_DEFAULT", "LOG_LIMIT_MAX", "LOG_VIEWER_ROLES", "router"]
