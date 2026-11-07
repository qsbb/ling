"""窗口标识 → (渠道, 会话类型, 对端 id)。

``SessionRecord.peer_id`` 是前端副标题里那个号码（群号 / 对方 QQ），而仓库里
**没有**单独存它的列：渠道窗口的 ``title`` 里就存着平台窗口标识
（``onebot-x:group:100200400``，投递侧一直按它定位）。这里钉住解析规则，
以及——更要紧的——那些**不该**解析成号码的情况：解析错了界面会把一个
莫名其妙的数字当群号显示出来。
"""

from __future__ import annotations

import pytest

from ling.domains.conversation.service import split_window_ref, window_target
from ling.infra.models import Window


def _window(title: str, channel: str = "onebot") -> Window:
    return Window(
        id="win_1",
        actor_id="actor_ling",
        persona_id="persona_ling",
        subject_id="subj_1",
        title=title,
        channel=channel,
        visibility="group_scope",
    )


def test_group_ref_yields_group_id() -> None:
    assert window_target(_window("onebot-default:group:100200400")) == (
        "onebot",
        "group",
        "100200400",
    )


def test_private_ref_yields_peer_id() -> None:
    assert window_target(_window("onebot-default:private:100200301")) == (
        "onebot",
        "private",
        "100200301",
    )


def test_web_window_never_has_peer_id() -> None:
    """web 会话的标题是用户随手起的名字：长得像 ref 也不能当群号。"""
    assert window_target(_window("a:group:999", channel="web")) == ("web", "", None)
    assert window_target(_window("新的对话", channel="web")) == ("web", "", None)


@pytest.mark.parametrize(
    "title",
    [
        "",
        "新的对话",
        "group:107",  # 少了实例前缀
        "onebot:room:107",  # 不认识的类型
        "onebot:group:",  # 空目标
        "onebot:group",  # 少一段
    ],
)
def test_malformed_refs_are_not_parsed(title: str) -> None:
    assert split_window_ref(title) is None
    assert window_target(_window(title)) == ("onebot", "", None)


def test_instance_with_dashes_survives() -> None:
    """实例名里有连字符/点都正常，只有冒号是分隔符。"""
    assert split_window_ref("obj-1.2:group:42") == ("obj-1.2", "group", "42")


def test_channel_comes_from_the_window_not_from_the_ref() -> None:
    """渠道以 ``windows.channel`` 为准：它是既有字段，ref 前缀只是实例名。"""
    assert window_target(_window("weird-instance:group:42", channel="qqbot"))[0] == "qqbot"
