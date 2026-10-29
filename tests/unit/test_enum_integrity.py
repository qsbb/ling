"""枚举完整性：StrEnum 成员的值必须唯一。

实际抓到过：`ChannelErrorKind.uncertain` 与 `.rejected` 都被设成
`"provider_error"`。后果很严重：

- `uncertain == rejected` 为 True
- `ChannelErrorKind("provider_error")` 只会得到 `rejected`
- 于是"结果未知（可能已送达）"被降级成"明确失败"，
  重试就会给用户发重复消息

str 枚举一旦有两成员同值，从值反序列化的路径就必然丢失其中一个的语义，
而这种丢失是静默的。所以这里对所有 StrEnum 做通用检查。
"""

from __future__ import annotations

import enum
import importlib
import pkgutil

import pytest


def _iter_enums():
    """遍历 ling 包里所有 StrEnum 子类。"""
    import ling

    seen: set[str] = set()
    for mod_info in pkgutil.walk_packages(ling.__path__, prefix="ling."):
        try:
            module = importlib.import_module(mod_info.name)
        except Exception:  # noqa: BLE001 - 导入失败的模块跳过
            continue
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, enum.StrEnum)
                and obj is not enum.StrEnum
                and obj.__module__.startswith("ling")
            ):
                qual = f"{obj.__module__}.{obj.__qualname__}"
                if qual not in seen:
                    seen.add(qual)
                    yield obj


ALL_ENUMS = sorted(_iter_enums(), key=lambda e: f"{e.__module__}.{e.__qualname__}")


def test_enums_are_discovered() -> None:
    """至少要发现一批枚举，否则说明遍历逻辑坏了（假绿）。"""
    assert len(ALL_ENUMS) >= 10, f"只发现 {len(ALL_ENUMS)} 个枚举，遍历可能失效"


@pytest.mark.parametrize(
    "enum_cls", ALL_ENUMS, ids=[f"{e.__module__}.{e.__qualname__}" for e in ALL_ENUMS]
)
def test_str_enum_has_no_aliases(enum_cls: type[enum.StrEnum]) -> None:
    """成员值必须唯一——**必须查 `__members__`，不能迭代枚举**。

    Python 把同值的第二个成员变成**别名**（alias）：
        class K(StrEnum): a = "x"; b = "x"
        list(K)        → [K.a]           # b 不出现
        K.b is K.a     → True            # 两者是同一个对象
        K.__members__  → {a: K.a, b: K.a}

    所以「迭代后统计重复值」永远查不出这个问题（假绿）。
    必须比较 `__members__` 的键数与去重后的值的数量。
    """
    declared = list(enum_cls.__members__)
    distinct_values = {m.value for m in enum_cls.__members__.values()}
    aliases = [
        name
        for name, member in enum_cls.__members__.items()
        if name != member.name
    ]
    assert not aliases, (
        f"{enum_cls.__qualname__} 有别名成员 {aliases}："
        "同值成员会变成别名，`is`/`==` 判定与从值反序列化都会静默丢失语义"
    )
    assert len(distinct_values) == len(declared), (
        f"{enum_cls.__qualname__} 声明 {len(declared)} 个成员但只有 "
        f"{len(distinct_values)} 个不同值"
    )


@pytest.mark.parametrize(
    "enum_cls", ALL_ENUMS, ids=[f"{e.__module__}.{e.__qualname__}" for e in ALL_ENUMS]
)
def test_str_enum_members_round_trip(enum_cls: type[enum.StrEnum]) -> None:
    """每个成员都能从自己的值反序列化回自己（别名会让这一步错位）。"""
    for name, member in enum_cls.__members__.items():
        restored = enum_cls(member.value)
        assert restored is member, (
            f"{enum_cls.__qualname__}.{name} 从值 {member.value!r} "
            f"反序列化得到 {restored.name}，语义丢失"
        )


def test_channel_error_uncertain_is_distinct() -> None:
    """具体回归：uncertain 不能与 rejected 同值。

    uncertain 表示"可能已送达"，必须走对账；rejected 表示"明确没送达"，
    可以安全重试。混同会导致重复发送。
    """
    from ling.ports.channel import ChannelErrorKind

    assert ChannelErrorKind.uncertain != ChannelErrorKind.rejected
    assert ChannelErrorKind.uncertain.value != ChannelErrorKind.rejected.value
    assert ChannelErrorKind("delivery_uncertain") is ChannelErrorKind.uncertain
