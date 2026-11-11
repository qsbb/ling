"""群聊判定：唯一实现，三处调用者行为一致。

## 这个测试要钉住什么

系统里曾经有 20+ 处各自判断"这是不是群聊"，判据分成两派：读
``Window.visibility``（正确）与 ``":group:" in window_ref`` 字符串匹配（错误）。

字符串匹配那派**已经造成过真实污染**：``bridge.py`` 用它决定往
``Window.visibility`` 列写什么，所以误判会**落进数据库**，之后
``turn.py`` / ``worker.py`` 全都忠实地跟着错。

本测试文件钉住四件事：

1. 判据函数本身正确（含三个会误判的私聊形态）；
2. ``turn.py`` / ``worker.py`` / ``bridge.py`` 三处**用同一个判据**；
3. ``bridge.py`` 那处**不再**做子串匹配；
4. ``source_visibility`` 与 ``Window.visibility`` 的关系（实证过的事实）。

## 为什么第 3 条要单独断言"不再做子串匹配"

只测"私聊含 :group: 不误判"是不够的：如果有人把实现改成
``":group:" in ref and ref.split(":")[1] == "group"``，行为可能仍然正确，
但**脆弱性回来了**（格式一变就全错）。所以要同时钉住行为与实现路径。
"""

from __future__ import annotations

import inspect

import pytest

from ling import worker as worker_module
from ling.contracts.v1 import Visibility
from ling.kernel import bridge as bridge_module
from ling.kernel import turn as turn_module
from ling.kernel.visibility import (
    GROUP_VISIBILITY,
    group_id_from_window_ref,
    is_group_visibility,
    is_group_window_ref,
)

# ---------------------------------------------------------------- 判据本身


def test_group_visibility_is_exactly_group_scope() -> None:
    """只有 ``group_scope`` 是群聊；其余取值都不是。

    ``Visibility`` 的六个取值里，除 ``group_scope`` 外都是"非群聊语境"——
    它们的区别是"谁能看见"，不是"是不是群"。
    """
    for member in Visibility:
        expected = member is Visibility.group_scope
        assert is_group_visibility(member) is expected, f"{member!r} 判错了"
        assert is_group_visibility(member.value) is expected


def test_group_visibility_accepts_both_enum_and_raw_string() -> None:
    """枚举与裸字符串都要能传。

    数据库里存的是 ``String(32)``（``infra/models.py:194``），代码里常常是
    枚举。两种混用是既成事实，判据必须都吃得下——否则"枚举 vs 字符串"
    这种不一致又会变成一个隐蔽的 bug 源。
    """
    assert is_group_visibility(Visibility.group_scope) is True
    assert is_group_visibility(Visibility.group_scope.value) is True


@pytest.mark.parametrize("value", [None, "", "unknown_value", 0, "GROUP_SCOPE"])
def test_unknown_visibility_is_not_group(value: object) -> None:
    """未知/空取值 fail-closed 到"不是群聊"。

    为什么未知值不当成群聊：群聊语义会**收紧**行为（记忆限本群、跨窗召回
    关闭、闸门拒绝某些提炼、提示词加约束）。把未知值猜成群聊 = 凭空给一个
    私聊用户加上群聊限制，表现是"她怎么突然不记得别的事了"。
    """
    assert is_group_visibility(value) is False


def test_group_visibility_constant_matches_contract() -> None:
    assert GROUP_VISIBILITY == Visibility.group_scope.value == "group_scope"


# ---------------------------------------------------------------- 字符串反推（降级路径）


@pytest.mark.parametrize(
    "window_ref",
    [
        "onebot:group:123456",
        "qq:group:abc",
        "wechat:group:xyz@chatroom",
        "web:group:whatever",
        # 群号本身含 "private" 字样也不影响：只看第 2 段
        "onebot:group:private123",
    ],
)
def test_real_group_window_ref_is_group(window_ref: str) -> None:
    """真正的群聊标识判对。"""
    assert is_group_window_ref(window_ref) is True


@pytest.mark.parametrize(
    "window_ref",
    [
        "onebot:private:100200300",
        "web:private:notes",
        # 下面三个是**旧实现会误判**的形态（已实测为 True）
        "onebot:private:group:123",
        "onebot:private:100200300:group:",
        "web:private:group:notes",
        "onebot:private:mygroup",
    ],
)
def test_private_window_ref_is_never_group(window_ref: str) -> None:
    """私聊标识绝不判成群聊——**包括标识里带 ``group`` 字样的**。

    ## 这三个是本次修复的核心

    旧实现 ``":group:" in window_ref`` 是**子串**检查，于是：

    ========================================== ============
    ``window_ref``                             旧写法
    ========================================== ============
    ``onebot:private:group:123``               群聊 ← 错
    ``onebot:private:100200300:group:``        群聊 ← 错
    ``web:private:group:notes``                群聊 ← 错
    ========================================== ============

    后果不是"分支走错"而是**数据污染**：``bridge.py`` 用这个判定决定往
    ``Window.visibility`` 写 ``group_scope`` 还是 ``subject_private``，
    写错了之后 ``turn.py`` / ``worker.py`` 全都跟着错。
    """
    assert is_group_window_ref(window_ref) is False


@pytest.mark.parametrize("value", [None, "", "onebot", "onebot:group"])
def test_degenerate_window_ref_is_not_group(value: object) -> None:
    """残缺标识不当成群聊（拿不到群号就不是一个可用的群窗口）。"""
    assert is_group_window_ref(value) is False


def test_group_id_extraction() -> None:
    """群号固定是第 3 段；取不到返回 ``None`` 而不是抛异常。"""
    assert group_id_from_window_ref("onebot:group:123456") == "123456"
    assert group_id_from_window_ref("qq:group:ABC-x") == "ABC-x"
    # 私聊/残缺 → None
    assert group_id_from_window_ref("onebot:private:100200300") is None
    assert group_id_from_window_ref("onebot:private:group:123") is None
    assert group_id_from_window_ref(None) is None
    assert group_id_from_window_ref("onebot:group:") is None


# ---------------------------------------------------------------- 三处调用者一致性


def test_all_three_call_sites_use_the_single_implementation() -> None:
    """``turn.py`` / ``worker.py`` / ``bridge.py`` 都调用唯一实现。

    用源码检查而不是运行时检查：这几处的判定结果要跑完整链路才能观察，
    而"有没有走统一实现"是源码级事实。
    """
    for module, name in [
        (turn_module, "turn.py"),
        (worker_module, "worker.py"),
        (bridge_module, "bridge.py"),
    ]:
        source = inspect.getsource(module)
        assert "kernel.visibility import" in source, (
            f"{name} 没有导入唯一实现（可能又自己写了一遍判据）"
        )
        assert (
            "is_group_visibility(" in source or "is_group_window_ref(" in source
        ), f"{name} 导入了但没调用"


def test_no_string_match_group_detection_remains_in_bridge() -> None:
    """``bridge.py`` 里**不能**再残留 ``":group:" in`` 的判定。

    ## 为什么这条要和"行为正确"分开

    只测行为不够：有人可以把实现改成
    ``":group:" in ref and ref.split(":")[1] == "group"``——行为仍然正确，
    但**脆弱性回来了**（``window_ref`` 格式一变就全错，而这正是本任务要根治的）。

    真正需要 ``:group:`` 字样的是"从标识里切出片段"这类操作（例如
    ``inbound_intake`` 取群号），那类用法不在本断言范围内——本断言只针对
    **判定"是不是群聊"** 这一件事。
    """
    source = inspect.getsource(bridge_module)

    # 排除注释行后再查
    code_lines = [
        line
        for line in source.splitlines()
        if not line.lstrip().startswith("#")
    ]
    offenders = [
        line.strip()
        for line in code_lines
        if '":group:" in' in line or "':group:' in" in line
    ]
    assert offenders == [], (
        "bridge.py 里仍有子串匹配判定群聊：\n" + "\n".join(offenders)
    )


def test_turn_py_does_not_compare_visibility_directly() -> None:
    """``turn.py`` 里不再直接写 ``visibility == "group_scope"``。

    直接比较本身不算错（值是对的），但它意味着"判据又散开了一处"。
    收敛的价值在于以后要改判据时只改一个地方。
    """
    source = inspect.getsource(turn_module)
    code_lines = [
        line
        for line in source.splitlines()
        if not line.lstrip().startswith("#")
    ]
    offenders = [
        line.strip()
        for line in code_lines
        if 'visibility == "group_scope"' in line
        or "visibility == 'group_scope'" in line
    ]
    assert offenders == [], (
        "turn.py 里仍有直接比较：\n" + "\n".join(offenders)
    )


def test_worker_keeps_fail_closed_on_explicit_is_group() -> None:
    """``worker.py`` 保留 ``payload["is_group"]`` 的**追加**语义（fail-closed）。

    生产者目前不写这个字段，但"任何一方说这是群聊，就按群聊处理"是安全的一侧：
    多收紧不会泄漏，漏判才会（那次事故就是漏判）。

    这条用源码断言是因为：要触发它得构造一个带 ``is_group`` 的 outbox payload
    并跑完整 handler，而设想的"将来生产者开始写它"还没发生。钉住这个语义
    防止有人在收敛时把它当成"死代码"删掉。
    """
    # **必须只看代码行**：worker.py 的注释里也写着 `payload.get("is_group")`
    # （解释这个字段的来历），用 `inspect.getsource` 直接断言会被注释骗过——
    # 删掉真代码后断言仍然通过。这正是 qa-be 守卫第一版踩的坑，我复现了一次：
    # 变异 `is_group = is_group_visibility(source_visibility)`（删掉 or 分支）
    # 后 39 条测试**全绿**，因为注释还在。
    code = "\n".join(_code_lines("src/ling/worker.py"))
    assert 'payload.get("is_group")' in code, (
        "worker.py 丢掉了 is_group 的追加语义——群聊判定会变成单源，"
        "一旦 source_visibility 算错就没有第二道保险"
    )


# ---------------------------------------------------------------- 污染链（实证）


def test_private_ref_with_group_marker_does_not_become_group_scope() -> None:
    """完整污染链已断：私聊标识 → 不判群聊 → 不会写 ``group_scope``。

    这条复现 ``bridge.py`` 的决策链：
    ``is_group = <判定>`` → ``visibility = "group_scope" if is_group else ...``。

    旧实现的产物是"私聊窗口被记成 ``group_scope``"，之后：
    - ``turn.py`` 把该私聊的话当群聊组装上下文
    - ``worker.py`` 从 ``source_visibility`` 派生 ``is_group=True``，群聊闸门
      对私聊生效、跨窗召回被关掉

    全部错误都源自这一步。
    """
    for ref in [
        "onebot:private:group:123",
        "onebot:private:100200300:group:",
        "web:private:group:notes",
    ]:
        is_group = is_group_window_ref(ref)
        visibility = "group_scope" if is_group else "subject_private"
        assert visibility == "subject_private", f"{ref!r} 被写成了 {visibility!r}"


def test_source_visibility_equals_window_visibility_in_production() -> None:
    """``source_visibility`` 的唯一生产者就是 ``window_row.visibility``。

    ## 这条是"实证"的可执行版本

    task 要求"``source_visibility`` 与 ``Window.visibility`` 是否可能不同，
    **要实证不要假设**"。结论是**在生产里恒等**，依据：

    - ``turn.py`` 是 ``memory.extract`` 事件里 ``source_visibility`` 的**唯一**
      生产者，写的就是 ``window_row.visibility``
    - ``window_row`` 来自 ``conversation.get_window_for_turn()``，该函数只做
      归属校验、**不修改** ``visibility``

    **但两者会一起错**：如果窗口的 ``visibility`` 列本身被写错（旧
    ``bridge.py`` 的字符串匹配就会写错），``source_visibility`` 会忠实地
    继承那个错误值。所以修复必须落在**写入口**，这就是本任务改
    ``bridge.py`` 的原因。

    本测试用源码断言"只有一个生产者"，防止将来出现第二个写入点让两者真的分叉。
    """
    source = inspect.getsource(turn_module)
    # 生产者写的是 window_row.visibility，不是别的派生值
    assert '"source_visibility": window_row.visibility' in source, (
        "turn.py 里 source_visibility 的生产方式变了——"
        "如果它不再直接来自 window_row.visibility，两者就可能分叉"
    )


# ---------------------------------------------------------------- 守卫测试（结构级）


#: 已完成收敛、不允许再出现硬写判据的文件。
#:
#: **只登记"判据与 `is_group_visibility` / `is_group_window_ref` 完全等价"
#: 的那些文件。** 语义不同的地方不能机械替换，见
#: `docs/GROUP-VISIBILITY-AUDIT.md` 的分类清单——把它们一起纳入守卫会让守卫
#: 变成"为了绿而加白名单"，失去意义。
CONVERGED_FILES = (
    "src/ling/kernel/turn.py",
    "src/ling/kernel/bridge.py",
    "src/ling/worker.py",
    "src/ling/kernel/inbound_intake.py",
    "src/ling/domains/goals/delivery_reconcile.py",
)


def _code_lines(path: str) -> list[str]:
    """返回文件里**非注释、非 docstring** 的代码行。

    ## 为什么必须排除注释和 docstring

    qa-be 的第一版守卫用 grep 直接搜源码字符串，结果被**自己注释里**写的
    ``== "group_scope"`` 误判成违规——守卫测试红了，但实际代码是对的。

    docstring 更隐蔽：本文件的 ``visibility.py`` 里大段解释"原来的
    ``":group:" in window_ref`` 是错的"，那些字面量会命中任何朴素 grep。

    所以这里真的去解析语法树：只保留**不是**字符串常量的行。这样
    "在注释里解释这个坑"和"在代码里再踩一次这个坑"能被区分开。
    """
    import ast
    from pathlib import Path

    source = Path(path).read_text()
    tree = ast.parse(source)
    # 收集所有字符串常量表达式的位置（docstring 也是 Expr(Constant)）
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                doc_lines.add(line)
    out = []
    for index, line in enumerate(source.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if index in doc_lines:
            continue
        out.append(line)
    return out


@pytest.mark.parametrize("path", CONVERGED_FILES)
def test_no_hardcoded_group_literal_in_converged_files(path: str) -> None:
    """已收敛的文件里不能再出现硬写的群聊判据字面量。

    ## 这是"结构测试"而不是"行为测试"

    行为测试只能证明**当前**三处结果一致；它证明不了"以后有人又写一份"。
    而分叉正是本任务要根治的病——所以除了测行为，还要断言**实现路径**：
    这些文件里不该再有独立的判据字面量。

    允许 ``":group:"`` 出现在**非判据**用途（例如拼接/切分字符串），
    但那种情况本就不该用这个字面量——``visibility.py`` 提供了
    ``GROUP_WINDOW_REF_MARKER`` 与 ``group_id_from_window_ref()``。
    """
    offenders = []
    for line in _code_lines(path):
        if '":group:" in' in line or "':group:' in" in line or '== "group_scope"' in line or "== 'group_scope'" in line:
            offenders.append(line.strip())
    assert offenders == [], (
        f"{path} 里仍有硬写的群聊判据（请调用 kernel/visibility.py）：\n  "
        + "\n  ".join(offenders)
    )


def test_guard_would_catch_a_reintroduction() -> None:
    """守卫本身有效：故意写一行违规代码，`_code_lines` + 断言能抓到。

    ## 为什么守卫也需要测试

    一个永远为真的守卫比没有守卫更糟：它给人"有保护"的错觉。这里对
    守卫的核心机制（``_code_lines`` 过滤 + 字面量匹配）做正向验证。
    """
    import tempfile
    from pathlib import Path

    bad = Path(tempfile.mkdtemp()) / "bad.py"
    bad.write_text(
        'def f(window_ref):\n'
        '    """docstring 里提到 `":group:" in ref` 不算违规。"""\n'
        '    # 注释里提到 == "group_scope" 也不算\n'
        '    return ":group:" in window_ref\n'
    )
    lines = _code_lines(str(bad))
    offenders = [
        line.strip()
        for line in lines
        if '":group:" in' in line or '== "group_scope"' in line
    ]
    assert len(offenders) == 1, f"守卫没抓到违规（或误判了注释）：{offenders}"
    assert offenders[0].startswith("return")


def test_visibility_module_exports_marker_that_nobody_hardcodes() -> None:
    """``GROUP_WINDOW_REF_MARKER`` 存在，但**不该有人**拿它做判定。

    它导出的目的是"需要这个字面量时（例如拼标识）从这里取，别各写一份"。
    如果有人写 ``GROUP_WINDOW_REF_MARKER in ref``，那是把子串匹配换了个
    马甲——本条与上面那条守卫一起覆盖这个口子。
    """
    from ling.kernel.visibility import GROUP_WINDOW_REF_MARKER

    assert GROUP_WINDOW_REF_MARKER == ":group:"

    offenders = []
    for path in CONVERGED_FILES:
        for line in _code_lines(path):
            if "GROUP_WINDOW_REF_MARKER in" in line:
                offenders.append((path, line.strip()))
    assert offenders == [], f"用常量换了马甲做子串匹配：{offenders}"


# ---------------------------------------------------------------- 具体案例（lead 指定）


def test_concrete_case_onebot_private_group_123() -> None:
    """**lead 指定的具体案例**：`onebot:private:group:123` 必须判为私聊。

    ## 为什么要把这一个例子单独钉住

    抽象描述（"子串匹配会误判"）说服力弱于一个具体标识。这个例子同时满足
    三个条件，让它成为最有代表性的样本：

    1. 它是**私聊**（第 2 段是 ``private``）；
    2. 它**包含** ``:group:`` 子串；
    3. 它的结构看起来"像"群聊（``group`` 后面还跟着数字 123）。

    旧实现 ``":group:" in "onebot:private:group:123"`` 返回 ``True``，
    于是 ``bridge.py`` 会把这个私聊窗口的 ``Window.visibility`` 写成
    ``group_scope``——**错误落进数据库**，之后 ``turn.py`` / ``worker.py``
    全都忠实地跟着错。这就是"不是一次误判，是持久污染"。
    """
    ref = "onebot:private:group:123"

    # 旧实现的判定（保留成可执行记录，说明这个 bug 真的存在）
    # 注意括号：`x in ref is True` 会被解析成 `x in (ref is True)`，
    # 是 Python 比较运算符链的经典陷阱。
    assert (":group:" in ref) is True, "前提：这个标识确实包含 :group: 子串"

    # 新实现必须判为私聊
    assert is_group_window_ref(ref) is False

    # 完整污染链：不会写成 group_scope
    visibility = "group_scope" if is_group_window_ref(ref) else "subject_private"
    assert visibility == "subject_private"


def test_concrete_case_contrast_with_real_group() -> None:
    """对照：只差第二段，真群聊必须判对。

    ``onebot:private:group:123`` vs ``onebot:group:123`` —— 唯一的差别是
    第 2 段。这保证新实现不是"一律判 False"那种简单化的错误。
    """
    assert is_group_window_ref("onebot:group:123") is True
    assert is_group_window_ref("onebot:private:group:123") is False
