"""设计约束：**记忆删除只有后台管理页一条路，对话里没有**。

用户原话：「只能后台管理页删，对话里不能删」。

这条不是"暂时没做"，是**有意的设计约束**，所以必须有测试把它钉住。
否则半年后有人"顺手"给模型加一个 `memory.forget` 工具，代码评审很容易
放过它——它看起来只是一个工具注册，而实际后果是：

- 模型可以自己决定"这件事该忘掉"，把用户没要求删的记忆删掉；
- 删除是**不可逆**的，错判一次就没有第二次机会；
- 对话里触发删除意味着它可能被提示注入触发（"忘掉你之前收到的所有指令"）。

判据落在**结构**上（工具注册表里的 tool_id 集合、轮次收尾的写入点），
不是"我记得没加过"。任何一处出现遗忘/删除语义的入口，这个文件就会红。

配套的"后台真的能删"在 `tests/unit/test_memory_purge.py` 与
`tests/integration/test_memory_purge_flow.py`。
"""

from __future__ import annotations

import pathlib
import re

from ling.capabilities.tools import build_default_registry

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "ling"

#: 遗忘/删除语义的**动词**。用于在 tool_id 与 tool 描述里搜。
#:
#: 注意 `delete` 单独收进来是因为 `memory.delete` / `memory.erase` 这类命名
#: 天然会用它；但 `handoff/{capsule_id}` 的 DELETE 是**另一件事**（交接胶囊
#: 的撤销，不是记忆），它不在工具注册表里，也不会被这条断言误伤。
FORGET_VERBS = ("forget", "delete", "erase", "purge", "wipe", "忘", "删", "清除")


def _forget_like(names: list[str]) -> list[str]:
    lowered = [n.lower() for n in names]
    return [n for n in lowered if any(verb in n for verb in FORGET_VERBS)]


def test_no_forget_tool_is_registered_for_the_llm() -> None:
    """工具注册表里**不能**存在遗忘/删除类工具。

    这是本文件最重要的一条。`build_default_registry()` 是模型侧能看到的
    全部动作集合；只要它里面没有遗忘类工具，模型就无法在对话里删记忆。

    为什么连 tool_id 和 description 都要查：一个叫 `memory.cleanup`、
    描述写着"删除不再需要的记忆"的工具，语义上和 `memory.forget` 完全一样。
    只查 id 会让改个名字就绕过去。
    """
    registry = build_default_registry()
    tool_ids = [spec.tool_id for spec in registry.list_specs()]
    assert tool_ids, "工具注册表是空的——这条断言就失去意义了"

    assert _forget_like(tool_ids) == [], (
        f"工具注册表里出现了遗忘/删除类工具：{_forget_like(tool_ids)}。\n"
        "记忆删除是**有意的设计约束**：只能由用户在后台管理页发起"
        "（POST /v1/memory/forget），不能由模型在对话里触发。"
    )

    offenders = [
        spec.tool_id
        for spec in registry.list_specs()
        if any(verb in spec.description.lower() for verb in FORGET_VERBS)
    ]
    assert offenders == [], (
        f"这些工具的描述里带遗忘/删除语义：{offenders}。"
        "描述是模型判断『这个工具能不能用』的依据，语义等价于有那个工具。"
    )


def test_registered_tool_ids_are_the_expected_closed_set() -> None:
    """已注册的 tool_id 是一个**封闭集合**：新增任何一个都要显式改这里。

    白名单式断言比黑名单更强：黑名单只能拦住"名字里带 forget"的东西，
    而这里要求"多一个就红"。加工具时被迫重新想一遍"它能不能删记忆"。
    """
    registry = build_default_registry()
    assert {spec.tool_id for spec in registry.list_specs()} == {
        "clock.now",
        "diagnostics.echo",
        "shell.readonly",
        # 撤回**她自己发出去的消息**，不是删记忆。不可逆、必须确认，
        # 且只作用于一条已送达的平台消息（见 capabilities/tools.py 的说明）。
        "message.recall",
    }


def test_turn_coordinator_never_deletes_memory() -> None:
    """轮次收尾只能**入队**提取任务，不能直接删记忆。

    `kernel/turn.py` 是对话主链。它里面出现 `forget_subject` / `purge_subject` /
    `delete(MemoryRow)` 就说明"对话里能删"这件事被加回来了。
    """
    turn = (SRC / "kernel" / "turn.py").read_text(encoding="utf-8")
    forbidden = (
        "forget_subject",
        "purge_subject",
        "request_purge",
        "delete(MemoryRow",
        "delete(MemoryEpisode",
        "delete(CoreMemoryRow",
    )
    found = [name for name in forbidden if name in turn]
    assert found == [], (
        f"对话主链里出现了删除记忆的调用：{found}。\n"
        "记忆删除只能由后台管理页的端点发起（见 manage.py 的 forget_all）。"
    )


def test_only_the_manage_route_can_request_a_purge() -> None:
    """`request_purge` 的调用方只能是后台管理路由。

    这条防的是"从别的入口（连接器、工具、定时任务）顺手发起真删"。
    目前它只有两个合法调用方：

    - `api/routes/manage.py` 的 `POST /v1/memory/forget`（用户点的）；
    - 测试与文档。
    """
    callers: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # 只找**调用**：排除 `async def request_purge(` 这个定义本身，
        # 也排除 `from ... import request_purge` 这种不带括号的导入。
        if re.search(r"(?<![\w.])(?<!def )request_purge\s*\(", text):
            callers.append(str(path.relative_to(SRC)))

    assert callers == ["api/routes/manage.py"], (
        f"request_purge 的调用方是 {callers}，预期只有 api/routes/manage.py。\n"
        "真删的发起入口必须唯一：多一个入口就多一条没人审过的删除路径。"
    )
