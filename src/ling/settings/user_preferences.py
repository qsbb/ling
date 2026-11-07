"""用户偏好（当前只有时区）的**唯一解析出处**。

## 为什么必须只有一处

时区有四个读者，每个都有自己的"取不到怎么办"：

- 主动联系门控（`domains/proactive/service.py`）：决定勿扰时段落在哪几小时；
- 总览的「今天」边界（`api/routes/dashboard.py`）与日程日界；
- worker 的主动投递与 token 额度的「本地日」；
- 设置页（读取现值 + 写回）。

原来的实现里四处各写一次 `settings.user_timezone or "UTC"`，而门控更狠：
取不到就**整条停掉主动联系**。于是"没设环境变量"这一个原因，同时表现为
"生活页说时区未设置"、"勿扰时段按 UTC 算"和"她再也不主动找你"。

现在只有一条规则，实现在本模块：

    数据库偏好 > 环境变量 ``LING_USER_TIMEZONE`` > ``DEFAULT_USER_TIMEZONE``

## 为什么保留环境变量这一层

旧部署在 `.env` 里配过 `LING_USER_TIMEZONE`，不该因为升级就变时区。
它是**兜底层**，不是第二份权威：数据库里一旦有值，环境变量就不再参与。

## 默认值为什么不是 UTC

见 `ling/config.py::DEFAULT_USER_TIMEZONE` 的注释：默认值必须是一个真实
可用的时区，否则"不知道"迟早退化成一个没人检查的兜底。

## 为什么偏好按 subject 存

登录账号本身就绑一个 subject（`users.subject_id`），时区是**这个人**的属性；
不同人的"今天"本来就可能不在同一天。多用户部署下按各自 subject 取，
单用户部署下就是你自己的那一行。
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.config import DEFAULT_USER_TIMEZONE
from ling.infra.models import User, UserPreferenceRow

__all__ = [
    "COMMON_TIMEZONES",
    "DEFAULT_VIDEO_MODE",
    "VIDEO_MODES",
    "DEFAULT_USER_TIMEZONE",
    "TimezoneSource",
    "clear_user_timezone",
    "clear_video_mode",
    "effective_user_timezone",
    "load_video_mode",
    "load_user_timezone",
    "normalize_timezone",
    "owner_subject_id",
    "resolve_timezone",
    "save_user_timezone",
    "save_video_mode",
    "stored_timezone",
    "timezone_by_subject",
    "video_mode_by_subject",
]

# 设置页下拉里的常用项。只是**建议**，不是白名单：任何合法的 IANA 名都能填，
# 因为"常用"这件事因人而异，写死会逼着在一张不完整的列表里选一个错的。
COMMON_TIMEZONES: tuple[str, ...] = (
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Asia/Singapore",
    "Asia/Bangkok",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Moscow",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Australia/Sydney",
    "Pacific/Auckland",
    "UTC",
)


class TimezoneSource(StrEnum):
    """这个时区是从哪一层取到的。界面上要能说出"这是默认值，不是你设的"。"""

    preference = "preference"
    env = "env"
    default = "default"


def normalize_timezone(raw: str | None) -> str | None:
    """校验并规范化一个 IANA 时区名；空值/非法值一律返回 ``None``。

    ``None`` 的语义是"没有可用值"，调用方据此继续往下一层回退——
    **不要**把非法值当成一个能用的时区传下去：``ZoneInfo`` 会在真正算
    勿扰时段时抛异常，那时已经离开这里很远，没人看得出是输入错了。
    """
    if raw is None:
        return None
    name = raw.strip()
    if not name:
        return None
    if len(name) > 64:  # 列宽 64；超长一定是垃圾输入，不必进 zoneinfo
        return None
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - 不存在的键、路径穿越、无 tzdata 都算非法
        return None
    return name


def resolve_timezone(
    stored: str | None, env: str | None
) -> tuple[str, TimezoneSource]:
    """按优先级解析时区，返回 ``(IANA 名, 来源)``。

    纯函数：不碰数据库、不读全局。DB 那一层由 ``load_user_timezone`` 提供。
    """
    preference = normalize_timezone(stored)
    if preference is not None:
        return preference, TimezoneSource.preference
    from_env = normalize_timezone(env)
    if from_env is not None:
        return from_env, TimezoneSource.env
    return DEFAULT_USER_TIMEZONE, TimezoneSource.default


async def stored_timezone(session: AsyncSession, subject_id: str | None) -> str | None:
    """库里存的原值（未规范化）。``None`` = 没设过。

    与"生效值"分开：设置页要能显示"你填过什么"，而不是把环境变量或默认值
    当成用户自己设的。
    """
    if not subject_id:
        return None
    row = await session.get(UserPreferenceRow, subject_id)
    # 直接返回原值而不是规范化后的：让 resolve_timezone 统一处理非法值，
    # 这样"库里存了坏值"与"环境变量里有坏值"走同一条回退路径。
    return row.timezone if row is not None else None


async def load_user_timezone(
    session: AsyncSession, *, subject_id: str | None, env: str | None
) -> tuple[str, TimezoneSource]:
    """解析某个 subject 的时区（含来源）。所有读时区的地方都从这里取。"""
    return resolve_timezone(await stored_timezone(session, subject_id), env)


async def effective_user_timezone(
    session: AsyncSession, *, subject_id: str | None, env: str | None
) -> str:
    """同上，只要名字——给那些不向用户解释来源的调用点（额度日界等）。"""
    name, _source = await load_user_timezone(session, subject_id=subject_id, env=env)
    return name


async def save_user_timezone(
    session: AsyncSession, *, subject_id: str, raw: str | None
) -> tuple[str, TimezoneSource]:
    """写入偏好并返回**生效值**（含来源）。

    ``raw`` 为空/非法时抛 ``ValueError``——校验是路由的责任，这里不静默吞掉：
    静默吞掉会让"我明明填了 America/New_York"变成一个查不出来的失败。
    要清掉偏好（回退环境变量/默认）请用 ``clear_user_timezone``。
    """
    name = normalize_timezone(raw)
    if name is None:
        raise ValueError(f"不是合法的 IANA 时区名: {raw!r}")
    row = await session.get(UserPreferenceRow, subject_id)
    if row is None:
        session.add(UserPreferenceRow(subject_id=subject_id, timezone=name, revision=1))
    else:
        row.timezone = name
        row.revision = int(row.revision or 0) + 1
    await session.flush()
    return name, TimezoneSource.preference


async def clear_user_timezone(
    session: AsyncSession, *, subject_id: str
) -> None:
    """把偏好清回"没设过"，让解析继续走环境变量 → 默认。"""
    row = await session.get(UserPreferenceRow, subject_id)
    if row is None:
        return
    row.timezone = None
    row.revision = int(row.revision or 0) + 1
    await session.flush()


async def owner_subject_id(session: AsyncSession) -> str | None:
    """部署所有者的 subject（第一个 ``role=owner`` 的账号）。

    用在**没有登录身份**的进程级判断上：token 额度的"日"与 worker 的投递
    兜底。个人部署里这就是唯一那个人；多用户部署里它只是"实例主人"的时区，
    每个 subject 自己的偏好仍然优先（见 ``timezone_by_subject``）。
    """
    row = (
        await session.execute(
            select(User.subject_id)
            .where(User.role == "owner")
            .order_by(User.created_at)
            .limit(1)
        )
    ).first()
    return str(row[0]) if row is not None else None


async def timezone_by_subject(session: AsyncSession) -> dict[str, str]:
    """所有**有偏好**的 subject → 生效时区。给逐个 subject 处理的批量任务用
    （主动投递要按每个人自己的"今天"和勿扰时段判断）。

    没有偏好（或偏好是坏值）的 subject 不出现在这里：调用方应该再回退到
    实例级默认（``resolve_timezone(None, env)``），而不是给所有人套同一个
    人的时区。
    """
    rows = (
        await session.execute(
            select(UserPreferenceRow.subject_id, UserPreferenceRow.timezone)
        )
    ).all()
    out: dict[str, str] = {}
    for subject_id, stored in rows:
        name = normalize_timezone(stored)
        if name is not None:
            out[str(subject_id)] = name
    return out


# ---------------------------------------------------------------- 视频读取模式

# 两种模式（用户拍板：设置里可选，**不放模型页**）：
#   frames  抽帧——任何供应商都能用，按固定间隔抽几帧交给视频模型；
#   native  原生视频输入——把视频原样交给模型，只有部分供应商支持。
# **默认 frames**：默认值必须是一个"任何部署都能用"的选择，否则新装的机器
# 会在第一次看视频时撞上"这个供应商不支持"。
DEFAULT_VIDEO_MODE = "frames"
VIDEO_MODES: tuple[str, ...] = ("frames", "native")


def normalize_video_mode(raw: str | None) -> str | None:
    """校验取值；不认识就返回 ``None``（调用方回退默认）。"""
    if raw is None:
        return None
    mode = raw.strip().lower()
    return mode if mode in VIDEO_MODES else None


async def load_video_mode(session: AsyncSession, *, subject_id: str | None) -> str:
    """某个 subject 生效的视频读取模式（坏值/没设过 → 默认 ``frames``）。"""
    if not subject_id:
        return DEFAULT_VIDEO_MODE
    row = await session.get(UserPreferenceRow, subject_id)
    stored = row.video_mode if row is not None else None
    return normalize_video_mode(stored) or DEFAULT_VIDEO_MODE


async def save_video_mode(
    session: AsyncSession, *, subject_id: str, raw: str | None
) -> str:
    """写入视频模式并返回生效值。非法取值抛 ``ValueError``（校验是路由的责任）。"""
    mode = normalize_video_mode(raw)
    if mode is None:
        raise ValueError(f"不是合法的视频读取模式: {raw!r}（可选: {'/'.join(VIDEO_MODES)}）")
    row = await session.get(UserPreferenceRow, subject_id)
    if row is None:
        session.add(UserPreferenceRow(subject_id=subject_id, video_mode=mode, revision=1))
    else:
        row.video_mode = mode
        row.revision = int(row.revision or 0) + 1
    await session.flush()
    return mode


async def clear_video_mode(session: AsyncSession, *, subject_id: str) -> None:
    """清掉视频模式，回退默认 ``frames``。

    与时区的空串语义对齐：``None`` 是"这次不改"，``""`` 才是"清掉"。
    没有这一条时，前端想"恢复默认"只能写 ``frames``——那会让"从没选过"和
    "选过又选回默认"在库里变成同一件事，以后改默认值就改不动了。
    """
    row = await session.get(UserPreferenceRow, subject_id)
    if row is None:
        return
    row.video_mode = None
    row.revision = int(row.revision or 0) + 1
    await session.flush()


async def video_mode_by_subject(session: AsyncSession) -> dict[str, str]:
    """所有显式设过（且合法）的 subject → 模式。批量任务用。"""
    rows = (
        await session.execute(
            select(UserPreferenceRow.subject_id, UserPreferenceRow.video_mode)
        )
    ).all()
    out: dict[str, str] = {}
    for subject_id, stored in rows:
        mode = normalize_video_mode(stored)
        if mode is not None:
            out[str(subject_id)] = mode
    return out
