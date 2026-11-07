"""用户时区的解析规则：DB 偏好 > 环境变量 > `Asia/Shanghai`。

## 这个文件守什么

原来时区只有一个来源（环境变量），四个读者各写各的兜底：proactive 的
"取不到就整条停掉"、总览的 `or "UTC"`、worker 的额度日界、设置页根本
没有这一项。同一份事实在四处有四种缺省，用户看到的三个数字互相矛盾。

现在只有一条规则，实现在 `ling/settings/user_preferences.py`。这里钉住
三件容易在"顺手加个兜底"时被破坏的事：

1. **优先级顺序**：库里设过的必须赢环境变量，反之就是"设置页保存成功、
   实际不生效"；
2. **坏值要往下一层退**，不能当成一个能用的时区传下去（`ZoneInfo` 会在
   很远的地方抛异常，没人看得出是输入错了）；
3. **默认值不是 UTC**：默认值必须是一个真实可用的时区，且全仓只有一处定义。
"""

from __future__ import annotations

import pytest

from ling.config import DEFAULT_USER_TIMEZONE
from ling.settings.user_preferences import (
    COMMON_TIMEZONES,
    TimezoneSource,
    normalize_timezone,
    resolve_timezone,
)


def test_default_is_asia_shanghai() -> None:
    """默认中国时区。理由见 `ling/config.py::DEFAULT_USER_TIMEZONE`。"""
    assert DEFAULT_USER_TIMEZONE == "Asia/Shanghai"
    name, source = resolve_timezone(None, None)
    assert name == DEFAULT_USER_TIMEZONE
    assert source is TimezoneSource.default


def test_env_beats_default() -> None:
    name, source = resolve_timezone(None, "Europe/London")
    assert (name, source) == ("Europe/London", TimezoneSource.env)


def test_preference_beats_env() -> None:
    """设置页保存的必须压过 `.env`：否则"保存成功、实际不生效"。"""
    name, source = resolve_timezone("America/New_York", "Europe/London")
    assert (name, source) == ("America/New_York", TimezoneSource.preference)


@pytest.mark.parametrize(
    "bad",
    [
        "",  # 空串 = 没设过
        "   ",  # 只有空白
        "UTC+8",  # 常见误解：不是 IANA 名
        "东八区",  # 中文也不能当日志名用
        "Asia/Shanghia",  # 拼错
        "America/Nowhere",
        "/etc/passwd",  # 路径穿越：zoneinfo 必须拒绝
        "../../etc/passwd",
        "A" * 65,  # 超过列宽，一定是垃圾输入
    ],
)
def test_invalid_names_are_none(bad: str) -> None:
    assert normalize_timezone(bad) is None


@pytest.mark.parametrize("name", ["Asia/Shanghai", "UTC", "America/Los_Angeles"])
def test_valid_names_survive(name: str) -> None:
    assert normalize_timezone(name) == name


def test_whitespace_is_trimmed() -> None:
    """粘贴带空格的值不该判成非法。"""
    assert normalize_timezone("  Asia/Shanghai  ") == "Asia/Shanghai"


def test_bad_stored_value_falls_through_to_env() -> None:
    """库里存了坏值（手工改库/旧版本写的）→ 退到环境变量，而不是失败关闭。

    失败关闭在这里没有意义：时区错了顶多判断时段偏，直接没有时区则整条
    主动联系停掉——那是更糟的结果。
    """
    name, source = resolve_timezone("Asia/Shanghia", "Europe/London")
    assert (name, source) == ("Europe/London", TimezoneSource.env)


def test_bad_env_falls_through_to_default() -> None:
    name, source = resolve_timezone(None, "UTC+8")
    assert (name, source) == (DEFAULT_USER_TIMEZONE, TimezoneSource.default)


def test_common_timezones_are_all_valid() -> None:
    """下拉里的建议项自己必须是合法的：列一个 tzdata 里没有的值，
    用户选中后保存会被拒，而错误信息指向他做对的事。"""
    invalid = [tz for tz in COMMON_TIMEZONES if normalize_timezone(tz) != tz]
    assert not invalid, f"常用时区里有非法项：{invalid}"
    assert DEFAULT_USER_TIMEZONE in COMMON_TIMEZONES, (
        "默认值不在常用列表里：用户打开页面会看到一个不在建议中的值"
    )
