"""提示词里的「能力标签」必须和**这个渠道真能做**一致。

## 这个文件要防的是什么

`capabilities` 车道里有一句「拍一拍」，写着：

    # 拍一拍要连接器支持；没能力就**不要列出来**——
    # 列了做不到的，她会选一个做不出来的动作。

而它原来的实现是：

    supports_poke=bool(getattr(self, "_supports_poke", True))

`_supports_poke` **全仓没有任何赋值点**，所以恒为 `True`。结果是提示词对
QQ 官方、微信、Web 面板都宣告"可以拍一拍"——而那些渠道没有 `send_poke`。
她写了 `[poke:100200300]`，投递层静默丢弃，用户看到的是"她说要拍你但没拍"。

**注释和代码直接矛盾**：写注释的人知道该怎么做，但当时没有数据源，
兜底值又选错了方向（默认 `True` = 没探测就宣称能做）。

## 为什么必须是端到端断言

这个 bug 的修复横跨两层：

1. `CapabilityDescriptor.supports_poke` —— 连接器**声明**自己能不能
2. `ContextCompiler` 把它读出来 —— 编译器**使用**这个声明

**只做第 1 步**：字段加了没人传 → 提示词行为完全不变 → 白做。
**只做第 2 步**：传了个没有 poke 信息的对象 → `getattr(..., False)` 恒为
`False` → **OneBot 上从"错误地显示"变成"错误地不显示"**，只是把 bug 换了
个方向。

两种半成品都会让"只测字段默认值"或"只测 `render_expression_help`"的测试
全绿。所以这里的断言**必须是编译出来的提示词文本**，不是字段值。

## 三条断言各自的区分力

| 用例 | 输入 | 期望 | 哪个错误实现会让它红 |
|---|---|---|---|
| OneBot | `supports_poke=True` 的 caps | **出现** `[poke:` | 第 2 步没做（恒 False） |
| qqbot | `supports_poke=False` 的 caps | **不出现** | 第 1 步没做（默认 True）/ 旧 `getattr` |
| Web | `channel_capabilities=None` | **不出现** | 同上 |

前两条**必须一真一假**。如果两条都用 `True`（或都用 `False`），那么
"永远返回同一个值"的实现也能通过——**那个输入证明不了任何事**。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select  # noqa: F401  (留给将来的窗口查询)

from ling.contracts.v1 import CapabilityDescriptor, Modality
from ling.infra.db import session_scope
from ling.infra.models import Window
from ling.kernel.context import ContextCompiler
from tests.integration.conftest import TestEnv


def _caps(*, supports_poke: bool, capability_id: str) -> CapabilityDescriptor:
    """造一个最小能力声明。只关心 `supports_poke`，其余取合理默认。"""
    return CapabilityDescriptor(
        capability_id=capability_id,
        version="1.0.0",
        modalities=[Modality.text],
        supports_private=True,
        supports_group=True,
        supports_poke=supports_poke,
        enabled=True,
    )


async def _seed_private_window(env: TestEnv) -> str:
    """建一个私聊窗口。`is_group=False` 时她的表达方式说明里不该有 @/引用。"""
    from ling.contracts.v1 import new_id

    async with session_scope() as session:
        window = Window(
            id=new_id("win"),
            actor_id="actor_ling",
            persona_id="persona_ling",
            subject_id=env.subject_id,
            title="web:private:poke-e2e",
            channel="web",
            visibility="subject_private",
        )
        session.add(window)
        await session.commit()
        return window.id


async def _capabilities_text(window_id: str, subject_id: str, caps) -> str:
    """编译一轮上下文，取出 `capabilities` 车道的正文。

    只看 **system** 消息：用户自己的输入会作为历史出现，那不是注入。
    """
    async with session_scope() as session:
        compiled = await ContextCompiler(
            session, channel_capabilities=caps
        ).compile(
            subject_id=subject_id,
            window_id=window_id,
            user_text="在吗",
            is_group=False,
        )
    return "\n".join(m.content for m in compiled.messages if m.role == "system")


async def test_onebot_capabilities_list_poke(logged_in: TestEnv) -> None:  # noqa: F811
    """OneBot 声明 `supports_poke=True` → 提示词里**必须**出现 `[poke:`。

    这是"两步都做了"的上半部分。如果 `ContextCompiler` 没把声明读出来
    （比如退回 `getattr(self, "_supports_poke", ...)` 那种写法），这条会红。
    """
    window_id = await _seed_private_window(logged_in)
    text = await _capabilities_text(
        window_id, logged_in.subject_id, _caps(supports_poke=True, capability_id="onebot.test")
    )
    assert "[poke:" in text, (
        "OneBot 支持拍一拍，提示词却没列出 `[poke:…]`——"
        "说明 `ContextCompiler` 没有读 `CapabilityDescriptor.supports_poke`"
    )


async def test_qqbot_capabilities_do_not_list_poke(logged_in: TestEnv) -> None:  # noqa: F811
    """qqbot 声明 `supports_poke=False` → 提示词里**不能**出现 `[poke:`。

    这是"两步都做了"的下半部分，也是这个 bug 的原始症状：
    旧实现 `getattr(self, "_supports_poke", True)` 恒为 True，这条会红。
    """
    window_id = await _seed_private_window(logged_in)
    text = await _capabilities_text(
        window_id, logged_in.subject_id, _caps(supports_poke=False, capability_id="qqbot.test")
    )
    assert "[poke:" not in text, (
        "qqbot 没有拍一拍能力，提示词却列出了 `[poke:…]`——"
        "她会写一个投递层会静默丢弃的标记"
    )


async def test_web_without_connector_does_not_list_poke(logged_in: TestEnv) -> None:  # noqa: F811
    """Web 面板没有连接器（`channel_capabilities=None`）→ 不列 `[poke:`。

    ## 这一条和上一条结果相同，为什么还要单独写

    `None`（没有渠道）与 `False`（有渠道但没这个能力）**结果相同、语义不同**。
    结果相同意味着这一条**单独证明不了**"`None` 被当成 False 处理"以外的任何
    东西——但它能证明**另一件事**：传 `None` 不会抛异常、不会被当成"没传"
    而退回到某个默认 `True`。

    旧实现下 `None` 根本不存在（参数都没有），所以这条在旧代码里无从谈起；
    它是为了锁住新参数的语义边界。**未来若有人把 `None` 的语义改成
    "走默认值"，这条会红，而上面两条不会。**
    """
    window_id = await _seed_private_window(logged_in)
    text = await _capabilities_text(window_id, logged_in.subject_id, None)
    assert "[poke:" not in text, (
        "Web 渠道没有外部连接器，拍一拍无处执行，提示词不该列出 `[poke:…]`"
    )


def test_capability_descriptor_defaults_poke_to_false() -> None:
    """`supports_poke` 的默认值必须是 `False`。

    ## 为什么这条要单独测

    默认 `True` 看起来"更安全"（少拒绝一次），但它**正是这个 bug 的成因**：
    没探测就宣称能做。而 `CapabilityDescriptor` 有十几个字段，逐个显式声明
    很啰嗦，后来的连接器很可能依赖默认值——**默认值就是事实上的能力声明**。

    把默认值改回 `True` 时这条会红。它是给"下一个人觉得 True 更安全"准备的
    护栏，注释里也写了同一句话。
    """
    caps = CapabilityDescriptor(capability_id="x.test", version="1.0.0")
    assert caps.supports_poke is False, (
        "`supports_poke` 默认 True 会让所有没显式声明的连接器都宣称能拍一拍"
    )


@pytest.mark.parametrize(
    ("connector_path", "expected"),
    [
        ("connectors/onebot/connector.py", True),
        ("connectors/qq/connector.py", False),
        ("connectors/qqbot/connector.py", False),
        ("connectors/wechat/connector.py", False),
    ],
)
def test_real_connectors_declare_poke_explicitly(connector_path: str, expected: bool) -> None:
    """每个连接器都**显式**声明了 `supports_poke`，且值与真实实现一致。

    ## 为什么用源码检查而不是 `capabilities()`

    构造真实连接器需要配置、凭据、HTTP 客户端，而这些与本测试要证明的事
    （"声明存在且方向正确"）无关。用 AST 只读 `capabilities()` 里的关键字
    参数，能证明"**写了**"，不能证明"**跑起来**是那个值"——后者由上面三条
    端到端用例覆盖。两者合起来才完整。

    ## 为什么必须显式写，不能靠默认值

    默认值将来若被改动（比如有人觉得 `True` 更安全），靠默认值的连接器会
    **静默**跟着变，而真正知道实情的那个文件却一个字没改。能力声明要写在
    知道实情的地方。

    ## 为什么 OneBot 是 True 而其余是 False

    OneBot V11 有 `send_poke` action（NapCat/Lagrange 都实现），连接器
    `send()` 里真的会对 `poke_targets` 发这个 action。QQ 官方接口、微信、
    都没有对应能力——它们连 `poke` 这个词都不出现。
    """
    import ast
    from pathlib import Path

    source = Path(connector_path).read_text(encoding="utf-8")
    tree = ast.parse(source)

    found: list[bool] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "capabilities":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            for kw in inner.keywords:
                if kw.arg != "supports_poke":
                    continue
                # 只接受字面量 True/False——不接受表达式，否则"写了什么"
                # 和"值是多少"之间又隔了一层，源码检查就失去意义。
                found.append(ast.literal_eval(kw.value))

    assert found, (
        f"{connector_path} 的 capabilities() 没有显式声明 supports_poke——"
        "靠字段默认值等于把能力判断交给一个可能被改动的常量"
    )
    assert found == [expected], f"{connector_path} 应声明 supports_poke={expected}，实际 {found}"


def test_no_getattr_default_true_for_poke_anywhere() -> None:
    """全仓不应再有"取不到就当能做"的 poke 兜底。

    这条守的是**修法**而不是行为：`getattr(x, "_supports_poke", True)` 这种
    写法即使当下行为正确（因为属性真的有值），也会在下一次改名时**静默**
    退回 `True`。而 `True` 的含义是"她可以去拍"，错了没有报错、只有用户在
    群里看不到那只手。

    **用 AST 而非字符串匹配**：本文件和其他文件的注释里都要引用
    `getattr(self, "_supports_poke", True)` 才能解释这个 bug，朴素字符串
    搜索会被自己的解释命中。
    """
    import ast
    from pathlib import Path

    offenders: list[str] = []
    for path in Path("src/ling").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Name) and node.func.id == "getattr"):
                continue
            if len(node.args) != 3:
                continue
            attr = node.args[1]
            default = node.args[2]
            if not (isinstance(attr, ast.Constant) and attr.value == "_supports_poke"):
                continue
            if isinstance(default, ast.Constant) and default.value is True:
                offenders.append(f"{path}:{node.lineno}")

    assert not offenders, (
        f"仍有 `getattr(..., '_supports_poke', True)` 兜底：{offenders}——"
        "取不到能力时应当按**不能做**处理（fail-closed），"
        "否则属性一旦改名就会静默退回'她可以去拍'"
    )
