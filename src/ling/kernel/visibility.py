"""群聊判定的**唯一实现**。

## 为什么需要这个模块

系统里曾经有 **20+ 处**各自判断"这是不是群聊"，而且判据分成两派：

- 一派读 ``Window.visibility == "group_scope"``（正确）；
- 一派用 ``":group:" in window_ref`` 做**字符串匹配**（错误）。

两派并存不是"风格不统一"这种洁癖问题，它已经**炸过一次**：

``worker.py`` 原来用 ``bool(payload.get("is_group"))`` 判断，而生产者
（``kernel/turn.py`` 的 ``memory.extract`` 入队处）从来不写那个字段，于是它
恒为 ``False``——群聊里提炼出的 boundary/health/family/birthday 本该被闸门
拒绝，**实际全部沉淀成了长期记忆**，群聊提示词里的附加约束也永不追加。

同一类问题换个形式又回来了：字符串匹配 ``":group:"`` 会在三种情况下误判。

## 权威来源

**``Window.visibility`` 是唯一权威**（``infra/models.py`` 的 ``Window.visibility``
列，取值见 ``contracts.v1.Visibility``：``actor_private`` / ``subject_private`` /
``window_private`` / ``group_scope`` / ``public`` / ``admin_only``）。

它由 ``kernel/bridge.py::_ensure_window`` 在窗口创建时写入一次，之后
**没有任何代码路径会改它**（已全量核验：``Window.visibility`` 的写入点只有
``bridge.py:1377``、``conversation/service.py:339``、``:1348`` 三处，后两处
固定写 ``subject_private``）。

### ``source_visibility`` 与 ``Window.visibility`` 是否可能不同

**不会——这是实证过的，不是假设。**

``turn.py:2218`` 是 ``memory.extract`` 事件里 ``source_visibility`` 的**唯一**
生产者，写的就是 ``window_row.visibility``；而 ``window_row`` 来自
``conversation.get_window_for_turn()``，该函数只做归属校验、**不修改**
``visibility``。所以两者在**生产**里恒等。

**但有一个真实的例外，它恰恰是本模块要修的 bug**：如果窗口的
``visibility`` 列本身就被**写错了**，那么 ``source_visibility`` 会忠实地
继承那个错误值。写错的来源就是字符串匹配——``bridge.py:1377`` 用
``":group:" in window_ref`` 算出 ``is_group``，再据此决定往列里写
``group_scope`` 还是 ``subject_private``。于是一个私聊窗口
``onebot:private:group:123`` 会被记成 ``group_scope``，之后
``turn.py`` / ``worker.py`` 都忠实地跟着错。

**结论**：两者不会互相矛盾，但会因为"上游算错了"而**一起错**。所以修在
写入口（``bridge.py``）比修在派生处更重要——那是污染的源头。

## 用法

    from ling.kernel.visibility import is_group_visibility, is_group_window_ref

    is_group = is_group_visibility(window.visibility)   # 有 Window 行时**优先用这个**
    is_group = is_group_window_ref(window_row)          # 手上只有 window_ref 字符串时

**不要**再写 ``":group:" in ref`` 或 ``visibility == "group_scope"``。
"""

from __future__ import annotations

from ling.contracts.v1 import Visibility

#: 表示"群聊"的那一个 visibility 取值。
#:
#: 只有它是群聊。其余（``actor_private`` / ``subject_private`` / ``window_private``
#: / ``public`` / ``admin_only``）都是**非群聊**语境，它们的区别是"谁能看见"，
#: 而不是"是不是群"。
GROUP_VISIBILITY = Visibility.group_scope.value

#: ``window_ref`` 里标记群聊会话的那个片段。
#:
#: 仅在**没有 Window 行、必须从字符串反推**时使用，见 :func:`is_group_window_ref`。
GROUP_WINDOW_REF_MARKER = ":group:"


def is_group_visibility(visibility: object) -> bool:
    """这个 visibility 是不是"群聊"。**唯一判据**。

    ``visibility`` 传 ``Visibility`` 枚举或裸字符串都可以：数据库里存的是
    ``String(32)``（``infra/models.py:194``），而代码里常常是枚举，两种都在
    用。这里统一 ``str()`` 之后比较，避免"枚举 vs 字符串"这种不一致再变成
    一个隐蔽的 bug 源。

    ``None`` / 未知取值一律返回 ``False``（fail-closed 到"不是群聊"）。

    为什么未知值不当成群聊：群聊语义会**收紧**行为（记忆只能本群、跨窗召回
    关闭、闸门拒绝某些提炼、提示词加约束）。把一个未知值猜成群聊，等于凭空
    给一个私聊用户加上群聊限制——"她怎么突然不记得别的事了"。宁可让它按
    私聊走（宽松但可见），出错时用户能感觉到并反馈。

    注意这与"安全边界"的直觉相反：通常应 fail-closed 到更严格的一侧。
    但这里两侧都不是安全边界——真正的安全边界在
    ``kernel/audience.py`` 的 ``_source_allowed()``，它按
    ``source_visibility`` 逐个核对来源，与这里无关。
    """
    return str(visibility) == GROUP_VISIBILITY


def is_group_window_ref(window_ref: object) -> bool:
    """从 ``window_ref`` 字符串反推是否群聊。

    .. warning::

       **这是降级路径，不是首选。** 有 ``Window`` 行时请用
       :func:`is_group_visibility`。``window_ref`` 是拼接出来的
       （``"<instance>:group:<gid>"`` / ``"<instance>:private:<uid>"``），
       它的格式是**约定**而不是受约束的类型，所以字符串匹配天然脆弱。

       本函数之所以仍然存在，是因为有几处在**拿到 Window 行之前**就要做
       判定（例如 ``bridge.handle()`` 还要据此决定窗口怎么查/怎么建，
       那时窗口可能根本不存在），以及几个只持有 ``window_ref`` 字符串的
       领域服务。这些位置目前无法改成读数据库列。

    ## 为什么必须是"按段切分"而不是 ``":group:" in ref``

    原来的写法 ``":group:" in window_ref`` 是个**子串**检查，会被下面这些
    **私聊**标识误判成群聊（已实测）：

    ========================================== ==============================
    ``window_ref``                             旧写法判定
    ========================================== ==============================
    ``onebot:private:group:123``               群聊 ← **错**
    ``onebot:private:100200300:group:``        群聊 ← **错**
    ``web:private:group:notes``                群聊 ← **错**
    ========================================== ==============================

    后果不只是"分支走错"：``bridge.py`` 用这个判定决定往
    ``Window.visibility`` 写什么，所以**错误会落进数据库**，之后
    ``turn.py`` / ``worker.py`` 全都忠实地跟着错。

    现在的判据是"**按 ``:`` 切段之后，第 2 段恰好等于 ``group``，且第 3 段
    （群号）非空**"。上面三个
    例子的段是 ``["onebot", "private", "group", "123"]``——第 3 段是 ``group``
    但第 2 段是 ``private``，所以按"渠道类型段"判定为私聊。

    真正的群聊形如 ``"<instance>:group:<gid>"``，**第 2 段**就是 ``group``。
    """
    parts = _split_window_ref(window_ref)
    # 第 0 段是实例/渠道名，第 1 段是会话类型（private/group），第 2 段是群号。
    #
    # 只认第 1 段，不认"任意一段出现过 group"——后者正是误判的来源。
    #
    # **还要求第 2 段非空**：``"onebot:group"``（没有群号）不是一个可用的群窗口。
    # 这条不是为了严格，而是为了**保住旧行为**：旧实现
    # ``":group:" in "onebot:group"`` 是 ``False``（缺尾部冒号），如果新实现
    # 返回 ``True``，那就是静默的行为变更——而本任务的前提是"行为不能变"。
    # 一个没有群号的"群聊"本来也无法走到任何有意义的发送路径。
    return len(parts) >= 3 and parts[1] == "group" and bool(parts[2])


def _split_window_ref(window_ref: object) -> list[str]:
    """把 ``window_ref`` 切成段；空值返回空列表。"""
    if window_ref is None:
        return []
    text = str(window_ref)
    if not text:
        return []
    return text.split(":")


def group_id_from_window_ref(window_ref: object) -> str | None:
    """从群聊 ``window_ref`` 里取群号；私聊/无法解析时返回 ``None``。

    群号固定是第 3 段（``"<instance>:group:<gid>"``）。取不到时返回 ``None``
    而不是抛异常：调用方（入站解析、日志）在拿不到群号时要么降级、要么不记，
    为这个抛错会让一条正常消息整条失败。
    """
    parts = _split_window_ref(window_ref)
    if len(parts) >= 3 and parts[1] == "group" and parts[2]:
        return parts[2]
    return None


__all__ = [
    "GROUP_VISIBILITY",
    "GROUP_WINDOW_REF_MARKER",
    "group_id_from_window_ref",
    "is_group_visibility",
    "is_group_window_ref",
]
