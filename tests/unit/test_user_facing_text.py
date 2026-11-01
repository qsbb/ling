"""面向用户的纯文本不得含 Markdown 语法字符。

后端把这些字段当成纯文本契约交给前端，前端也按纯文本渲染（`{why}`、`{note}`
直接进 JSX）。一旦有人在字符串里写 ``**强调**``，用户屏幕上看到的是带星号的
原始文本，而不是加粗——这是真实的显示缺陷，且 TypeScript 与 ruff 都不会报。

发给模型的 prompt 不在此列：那些字符串会被模型读成 Markdown，是有意的。
"""

from __future__ import annotations

import re

import pytest

# 只覆盖"会被原样渲染到界面"的字段。新增同类字段时请加到这里。
_USER_FACING_SOURCES = [
    ("src/ling/settings/service.py", "PROFILE_DEGRADATIONS"),
    ("src/ling/api/routes/manage.py", "note"),
    ("src/ling/api/routes/qqbot.py", "logger.warning"),
]

_MARKDOWN_EMPHASIS = re.compile(r"\*\*[^*\n]+\*\*")


def _read(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


@pytest.mark.parametrize("path,marker", _USER_FACING_SOURCES)
def test_no_literal_markdown_in_user_facing_text(path: str, marker: str) -> None:
    """源码里面向用户的那段文本不得出现 ``**加粗**``。"""
    text = _read(path)
    assert marker in text, f"{path} 里找不到锚点 {marker}，测试已失效需更新"

    # 只看双引号字符串字面量，跳过注释与文档字符串。
    literals = re.findall(r'"[^"\n]*"', text)
    offenders = [s for s in literals if _MARKDOWN_EMPHASIS.search(s)]
    assert not offenders, (
        f"{path} 有面向用户的字符串含 Markdown 强调语法，前端按纯文本渲染会露出星号：{offenders}"
    )


def test_proactive_note_has_no_asterisks() -> None:
    """生活页的 ``note`` 直接进 JSX，必须干净。"""
    text = _read("src/ling/api/routes/manage.py")
    assert "主动发送已实现" in text
    for literal in re.findall(r'"[^"\n]*主动发送已实现[^"\n]*"', text):
        assert "**" not in literal, f"生活页 note 含星号：{literal}"


def test_degradation_text_has_no_asterisks() -> None:
    """未绑定职责的降级说明直接进 <li>，必须干净。"""
    from ling.settings.service import PROFILE_DEGRADATIONS

    dirty = {
        profile: why
        for profile, why in PROFILE_DEGRADATIONS.items()
        if "**" in why or "__" in why or "`" in why
    }
    assert not dirty, f"降级说明含 Markdown 语法，界面会露出原始字符：{dirty}"
