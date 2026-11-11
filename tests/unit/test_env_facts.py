"""环境事实（`kernel/env_facts.py`）的单元测试。

这里测的是**纯函数**：给定一组事实，渲染出来的那段中文长什么样。
取数（查库、读时区）放在集成测试里，因为那里才有真数据库。

## 为什么要逐句钉住措辞

这段文字会原样进入提示词，模型会当自然语言读。所以「在 QQ 群聊」和
「在 QQ 的群聊」的区别不只是好不好听——少一个空格，它就会变成一个
生造的复合词。措辞在本项目里是可测试的一部分（同 `scenarios` 的做法）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from ling.kernel.env_facts import EnvFacts, channel_label, render

pytestmark = pytest.mark.w0_web


def _facts(**kw: object) -> EnvFacts:
    base: dict[str, object] = {
        "channel": "onebot",
        "window_ref": "100200300",
        "local_now": datetime(2026, 11, 11, 21, 30),
        "timezone": "Asia/Shanghai",
    }
    base.update(kw)
    return EnvFacts(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 渲染出的实际文本
# --------------------------------------------------------------------------- #


def test_群聊的渲染文本() -> None:
    """群聊那一句的**逐字**样子。将来改措辞必须同步改这里。"""
    text = render(_facts(is_group=True))
    assert text.splitlines()[0] == "当前在 QQ 群聊（100200300）"


def test_私聊的渲染文本() -> None:
    text = render(_facts(is_group=False))
    assert text.splitlines()[0] == "当前在 QQ 私聊（100200300）"


def test_平台与会话类型之间有空格() -> None:
    """中英混排要有空格。

    「QQ群聊」会变成一个生造词；模型读到它时不会自动切分。这条单独测，
    因为它曾经真的少过——改 `f"{where}{kind}"` 时漏了空格。
    """
    text = render(_facts(is_group=True))
    assert "QQ群聊" not in text
    assert "QQ 群聊" in text


def test_时间按本地时区渲染且带星期() -> None:
    text = render(_facts())
    assert "2026-11-11 21:30" in text
    assert "周三" in text
    assert "Asia/Shanghai" in text


def test_时间用中文星期而不是英文缩写() -> None:
    """`%a` 会给 "Wed"，而那段话是中文旁白。"""
    text = render(_facts(local_now=datetime(2026, 11, 15, 9, 0)))  # 周日
    assert "周日" in text
    assert "Sun" not in text


# --------------------------------------------------------------------------- #
# 缺数据时的行为：少说一句，绝不说"未知"
# --------------------------------------------------------------------------- #


def test_没有会话标识时省掉括号而不是写未知() -> None:
    """`window_ref` 为空 → 只说「当前在 QQ 群聊」，不留空括号。

    「（未知）」会被模型当成一个已知道的事实去推理。
    """
    text = render(_facts(is_group=True, window_ref=None))
    assert text.splitlines()[0] == "当前在 QQ 群聊"
    assert "未知" not in text
    assert "（" not in text.splitlines()[0]


def test_没有时区时不注入时间而不是退回UTC() -> None:
    """时区读不到 → **不讲时间**。

    退回 UTC 更糟：她会照着 UTC 说"才下午三点"，而用户那边是深夜。
    一条错误的事实比没有事实有害得多。
    """
    text = render(_facts(is_group=True, timezone=None))
    assert "此刻" not in text
    assert text.splitlines()[0] == "当前在 QQ 群聊（100200300）"


def test_全部为空时渲染成空串() -> None:
    """没有任何事实 → 空串，由调用方决定不注入。

    不能渲染成"环境未知：暂无信息"——那是把一个数据缺口说成了事实。
    """
    assert render(EnvFacts()) == ""
    assert EnvFacts().has_anything is False


def test_只有时区没有时间是空() -> None:
    """`timezone` 单独存在没有意义（换算不出钟点）。"""
    f = EnvFacts(timezone="Asia/Shanghai")
    assert f.has_anything is False
    assert render(f) == ""


# --------------------------------------------------------------------------- #
# 只阐述事实，不含行为指令
# --------------------------------------------------------------------------- #


def test_不包含任何行为指令() -> None:
    """用户原话是「**只阐述**目前环境事实」。

    这条测的是界限而不是措辞：一旦混进"应该""要""注意"之类的祈使词，
    人设的作用就被环境事实盖住了，而且用户改人设会发现改不动。
    """
    text = render(_facts(is_group=True))
    for banned in ("应该", "需要", "请", "不要", "务必", "建议", "记得"):
        assert banned not in text, f"环境事实里出现了行为指令：{banned}"


def test_不表态关系() -> None:
    """关系由 `relationship` 车道说，这里再说一遍就是两个来源。

    环境事实只说"在哪、跟谁"，不评价"关系怎么样"。
    """
    text = render(_facts(is_group=True))
    for banned in ("熟悉", "信任", "亲近", "陌生", "关系"):
        assert banned not in text, f"环境事实里出现了关系评价：{banned}"


def test_不说自己在现实或打电话() -> None:
    """渠道是什么就说什么。

    `Window.channel` 里没有"现实场景""打电话"这些值，凭空写出来就是编。
    """
    text = render(_facts(is_group=True))
    for banned in ("现实", "打电话", "通话", "见面"):
        assert banned not in text


# --------------------------------------------------------------------------- #
# 渠道名映射
# --------------------------------------------------------------------------- #


def test_已知渠道映射成平台名() -> None:
    assert channel_label("onebot") == "QQ"
    assert channel_label("qqbot") == "QQ"
    assert channel_label("wechat") == "微信"
    assert channel_label("web") == "网页控制台"


def test_没见过的渠道原样返回而不是猜() -> None:
    """猜一个名字比显示内部标识更糟——猜错就是一句假话。"""
    assert channel_label("telegram") == "telegram"
    assert channel_label("") is None
    assert channel_label(None) is None


def test_未知渠道不会渲染成未知字样() -> None:
    text = render(_facts(channel="telegram"))
    assert "telegram" in text
    assert "未知" not in text


# --------------------------------------------------------------------------- #
# 群聊判定必须走唯一实现（task-42）
# --------------------------------------------------------------------------- #


def test_群聊判定复用visibility唯一实现() -> None:
    """`env_facts` 不得自己写 `visibility == "group_scope"`。

    这个仓库里"群聊判定"曾经有 20+ 处各自为政，判据分两派，并且已经炸过
    一次（`worker.py` 里恒为 False 的判定让群聊提炼的记忆全部沉淀成长期
    记忆）。权威实现是 `kernel/visibility.py::is_group_visibility`。

    这条测的是**依赖关系**而不是结果：即使两边当下结果相同，只要
    `env_facts` 自己写一份，下一次 `visibility.py` 改了语义它就跟不上。
    """
    import inspect

    import ling.kernel.env_facts as env_facts_mod
    from ling.kernel.visibility import is_group_visibility

    source = inspect.getsource(env_facts_mod.collect)
    # 注释里提到 `== "group_scope"` 是解释历史，不算违规；只检查**代码**。
    code_lines = [
        ln for ln in source.splitlines() if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert "is_group_visibility" in code, (
        "collect 里没有调用 is_group_visibility——又自己写了一份群聊判定"
    )
    assert '== "group_scope"' not in code, (
        "collect 里出现了硬写的群聊判定，应当走 is_group_visibility"
    )

    # 权威实现的行为（含 fail-closed：未知值不算群聊）
    assert is_group_visibility("group_scope") is True
    assert is_group_visibility("subject_private") is False
    assert is_group_visibility(None) is False
    assert is_group_visibility("不认识的值") is False
