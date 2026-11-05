"""可见范围规则必须只有一份权威实现。

这条规则曾经有四份独立实现：
1. `kernel/audience.py::visibility_set`（recall 用）
2. `AudienceSnapshot._allowed_visibilities`（行级过滤用）
3. `ConsentService.allowed_visibilities`（实际是死代码，没人调）
4. `PolicyGate.visible_memory_scopes`（裁决用）

每份看起来都一样，但没有任何东西保证它们一致。改一处忘另一处，
结果要么是**泄漏**（某路径允许了不该允许的范围），要么是**锁死**
（某路径少了一项，用户自己的记忆读不出来）。

现在全部委托到 `kernel.audience.readable_visibilities`。
本测试锁死这个不变量：新增取值路径时若忘了委托，这里会失败。
"""

from __future__ import annotations

import itertools

import pytest

from ling.contracts.v1 import Visibility
from ling.domains.consent.service import ConsentService
from ling.kernel.audience import readable_visibilities, visibility_set
from ling.kernel.policy import PolicyContext, PolicyGate

CASES = list(itertools.product((False, True), repeat=2))  # is_group × cross_device


@pytest.mark.parametrize(("is_group", "cross_device"), CASES)
def test_all_paths_agree(is_group: bool, cross_device: bool) -> None:
    """四个入口必须给出完全相同的集合。"""
    authority = readable_visibilities(
        is_group=is_group, cross_device_granted=cross_device
    )
    from_consent = ConsentService(None).allowed_visibilities(  # type: ignore[arg-type]
        is_group=is_group, cross_device_granted=cross_device
    )
    from_policy = PolicyGate().visible_memory_scopes(
        PolicyContext(
            subject_id="s",
            window_id="w",
            visibility=Visibility.public,
            is_group=is_group,
            cross_device_granted=cross_device,
        )
    )
    assert authority == from_consent == from_policy


@pytest.mark.parametrize(("is_group", "cross_device"), CASES)
def test_admin_only_never_readable(is_group: bool, cross_device: bool) -> None:
    """`admin_only` 绝不能出现在普通可读集里。

    管理视图需要单独的授权通道，不能靠"往允许集里加一项"实现——
    那等于把管理数据混进普通召回。
    """
    scopes = readable_visibilities(
        is_group=is_group, cross_device_granted=cross_device
    )
    assert Visibility.admin_only not in scopes


@pytest.mark.parametrize(("is_group", "cross_device"), CASES)
def test_group_never_sees_private(is_group: bool, cross_device: bool) -> None:
    """群聊窗口永远看不到任何私聊级范围。"""
    scopes = readable_visibilities(
        is_group=is_group, cross_device_granted=cross_device
    )
    if is_group:
        for private in (
            Visibility.subject_private,
            Visibility.window_private,
            Visibility.actor_private,
        ):
            assert private not in scopes, f"群聊可读到 {private.value}"


def test_cross_device_adds_only_actor_private() -> None:
    """跨端授权只应多出 actor_private，不能放宽其他范围。"""
    without = readable_visibilities(is_group=False, cross_device_granted=False)
    with_grant = readable_visibilities(is_group=False, cross_device_granted=True)
    assert with_grant - without == {Visibility.actor_private}


def test_visibility_set_delegates() -> None:
    """`visibility_set` 必须与权威实现一致（它是 recall 的实际入口）。"""

    class _Fake:
        is_group = False
        cross_device_granted = True

    assert visibility_set(_Fake()) == readable_visibilities(  # type: ignore[arg-type]
        is_group=False, cross_device_granted=True
    )
