"""看一眼手机：她与手机的关系（上次/下次/此刻在不在看）。

`schedule_dependency` / `PhoneInterval` 是"上游（日程）不可用就用自己的
默认节奏"这条契约在本域的落点，见 `ling.domains.fallback`。
"""

from ling.domains.phone.service import (
    ACTIVITY_INTERVAL_MINUTES,
    DEFAULT_IDLE_MINUTES,
    UPSTREAM_SCHEDULE,
    PhoneInterval,
    PhoneService,
    PhoneView,
    ScheduleNow,
    schedule_dependency,
)

__all__ = [
    "ACTIVITY_INTERVAL_MINUTES",
    "DEFAULT_IDLE_MINUTES",
    "UPSTREAM_SCHEDULE",
    "PhoneInterval",
    "PhoneService",
    "PhoneView",
    "ScheduleNow",
    "schedule_dependency",
]
