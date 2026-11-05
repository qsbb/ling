"""准入三态：能看见 ≠ 能开口。

改造前的真实故障：白名单在入站第一道闸就把群消息杀了，于是群聊轮询
（写得挺完整）永远拿不到内容——她**看不见**群里在聊什么，"偶尔看一眼"
无从发生。这组测试钉住六个场景，任何一个错都会让那条链路静默失效。
"""

from __future__ import annotations

import pytest

from ling.kernel.access import (
    DENIED,
    MAY_REPLY,
    OBSERVE_ONLY,
    check_access,
)

OWNER = "100200300"
STRANGER = "999999999"
KNOWN_GROUP = "700000001"
OTHER_GROUP = "777777777"

ALLOW = frozenset({OWNER})
GROUPS = frozenset({KNOWN_GROUP})


def _decide(sender: str, group: str | None = None, **kw):  # noqa: ANN003, ANN202
    return check_access(
        sender, mode="whitelist", allow_list=ALLOW, group_id=group,
        allow_groups=GROUPS, **kw,
    )


def test_owner_in_private_may_reply() -> None:
    d = _decide(OWNER)
    assert d.verdict == MAY_REPLY
    assert d.observable and d.allowed


def test_stranger_in_private_is_denied_entirely() -> None:
    """私聊是安全边界：陌生人连上下文都不该进。

    谁都能给她发私信的话，"看一眼手机"就等于把门敞开。
    """
    d = _decide(STRANGER)
    assert d.verdict == DENIED
    assert not d.observable and not d.allowed


def test_stranger_in_known_group_is_observed_not_answered() -> None:
    """白名单群里的人：她看得见，但不主动插话。"""
    d = _decide(STRANGER, KNOWN_GROUP)
    assert d.verdict == OBSERVE_ONLY
    assert d.observable, "必须进上下文，否则群聊轮询拿不到内容"
    assert not d.allowed, "不该主动接话"
    # 但被点名时可以回——那是"有人跟她说话"，不是"她在插嘴"
    assert d.reply_when_addressed


def test_owner_in_unknown_group_still_may_reply() -> None:
    """主人在不熟悉的群里，被 @ / 引用时也能回。"""
    d = _decide(OWNER, OTHER_GROUP)
    assert d.verdict == MAY_REPLY
    assert d.reply_when_addressed


def test_stranger_in_unknown_group_is_observed_but_never_answers() -> None:
    """不熟悉的群也看得见（人本来就会潜水），但跟这群人没关系，被 @ 也不回。"""
    d = _decide(STRANGER, OTHER_GROUP)
    assert d.verdict == OBSERVE_ONLY
    assert d.observable
    assert not d.allowed
    assert not d.reply_when_addressed


def test_group_whitelist_does_not_leak_into_private() -> None:
    """白名单群**只**影响群聊。不能因为群在白名单里，就把私聊陌生人也放进来。"""
    assert _decide(STRANGER).verdict == DENIED
    assert _decide(STRANGER, KNOWN_GROUP).verdict == OBSERVE_ONLY


def test_broken_mode_is_strictest() -> None:
    """配置写错时按最严处理——连 observe 也不给。

    配置坏了就该停下，而不是让陌生人的话先灌进她的记忆。
    """
    d = check_access(STRANGER, mode="white_list", allow_list=ALLOW,
                     group_id=KNOWN_GROUP, allow_groups=GROUPS)
    assert d.verdict == DENIED
    assert not d.observable


def test_blacklist_mode_unchanged() -> None:
    """黑名单模式语义不变：不在名单里的都能回。"""
    d = check_access(STRANGER, mode="blacklist", allow_list=frozenset({STRANGER}))
    assert d.verdict == DENIED
    d2 = check_access(OWNER, mode="blacklist", allow_list=frozenset({STRANGER}))
    assert d2.verdict == MAY_REPLY


@pytest.mark.parametrize("mode", ["off"])
def test_off_mode_allows_everything(mode: str) -> None:
    d = check_access(STRANGER, mode=mode, allow_list=frozenset())
    assert d.verdict == MAY_REPLY
    assert d.reply_when_addressed
