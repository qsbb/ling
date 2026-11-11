"""环境事实：她现在「在哪、以什么身份、跟谁说话」。

## 为什么单独一个模块

`context.py` 已经 1400+ 行，而这里要写的是**纯组装逻辑**——输入几个既有的
数据源，输出一段事实文本。把它放在 `context.py` 里会让"取数"和"措辞"缠
在一起，而措辞恰恰是这个功能唯一需要仔细斟酌的部分（见下面「只陈述事实」）。

## 只陈述事实，不编造倾向

用户原话是「**只阐述**目前环境事实」。这条界限必须守住：

- ✅ 「当前在 QQ 群聊（群 1xxxx），登录账号 2xxxx」
- ❌ 「你应该表现得像在 QQ 上」

**行为倾向由人设和分区规范决定。** 一旦把"该怎么表现"混进环境事实，
人设的作用就被这段话盖住了——而且用户改人设时会发现改不动，因为另一处
写得比它更具体、更靠前。

所以本模块里**一个"应该"都没有**，只有"是"。这条由
`tests/unit/test_env_facts.py::test_不包含任何行为指令` 钉住。

## 数据来源：全部复用既有事实，不新建第二套

| 事实 | 来源 | 说明 |
|---|---|---|
| 渠道 / 平台 | `Window.channel`（`infra/models.py:193`） | 窗口建立时由桥接层写入 |
| 会话类型 | `Window.visibility`（`infra/models.py:194`） | 判定走 `kernel/visibility.py` 唯一实现 |
| 会话标识 | `Window.title`（`infra/models.py:192`） | 存着 `envelope.window_ref` |
| 账号 / 实例 | `actor_accounts`（`infra/models.py:120`） | 渠道外部账号 → subject 绑定 |
| 时区 | `settings/user_preferences.py` | DB 偏好 > `LING_USER_TIMEZONE` > 默认 |

**没有新建任何表、任何列。** 这个仓库最忌讳"两处各说一套"
（`kernel/worker.py:414-426` 记录过一次真实事故：`is_group` 被两处维护，
其中一处永远不写入，导致群聊记忆闸门全线失效）。

## 关系数据为什么**不**在这里注入

`relationship` 车道**已经有生产者**（`context.py` 里那段 `RelationshipService`
读取，注入"你和这个人：认识这个人"）。环境事实若再说一遍关系，就是同一个
事实两个来源——而两处的更新时机和受众规则都不一样，迟早会互相矛盾。

所以分工是：**本模块说"在哪、跟谁"，`relationship` 车道说"关系怎么样"。**
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime


def _utcnow() -> datetime:
    return datetime.now(UTC)

#: 渠道标识 → 给人看的平台名。
#:
#: ## 为什么要这张表而不是直接显示 `window.channel`
#:
#: 数据库里存的是内部标识（`onebot` / `web` / `wechat`），而模型看到
#: 「onebot」不会知道那是 QQ。但**这张表只做重命名，不发明分类**：没有
#: 映射的渠道原样显示内部标识，而不是猜一个像"现实场景"之类的名字——
#: 猜错会让她说出"我们现在在现实中见面"这种明显的假话。
_CHANNEL_LABELS: dict[str, str] = {
    "onebot": "QQ",
    "qqbot": "QQ",
    "wechat": "微信",
    "web": "网页控制台",
    "cli": "命令行",
    "api": "接口调用",
}

#: 用户原话里提到的两种「非聊天渠道」。
#:
#: 它们**不是** `Window.channel` 的取值（数据库里没有"打电话"这个渠道），
#: 而是被渠道层另加标记的场景。这里留作将来的挂点，目前不产出。
_VOICE_CHANNELS = frozenset({"call", "phone", "realtime"})


@dataclass(frozen=True)
class EnvFacts:
    """一轮对话的**环境事实**。全部是可核验的客观状态，不含任何判断。

    每个字段都允许为 `None`：那表示"系统里没有这个事实"，而**不是**
    "这个事实是空值/未知"。渲染时缺哪个就少说哪一句，绝不补
    "未知"——见 :func:`render` 的说明。
    """

    #: 内部渠道标识（`onebot` / `web` / ……）。直接来自 `Window.channel`。
    channel: str | None = None
    #: 是否群聊。由 `kernel/visibility.py::is_group_visibility` 判定（唯一实现）。
    is_group: bool = False
    #: 会话标识（群号 / 好友 id）。来自 `Window.title`。
    window_ref: str | None = None
    #: 我们这侧的账号 / 实例标识。来自 `actor_accounts.external_account_id`。
    account: str | None = None
    #: 渠道实例名（一个部署可能配两个 MiniMax/两个 QQ 号）。
    instance: str | None = None
    #: 发言人此刻的本地时间（已按她的时区换算）。
    local_now: datetime | None = None
    #: 时区名（IANA）。与 `local_now` 一起出现才有意义。
    timezone: str | None = None

    @property
    def has_anything(self) -> bool:
        """有没有任何一条值得说的事实。

        全空时调用方**不注入这条 contribution**（而不是注入一段
        "环境未知"）。理由同上：说"未知"等于陈述了一个事实。
        """
        return bool(
            self.channel
            or self.window_ref
            or self.account
            or (self.local_now is not None and self.timezone)
        )


def channel_label(channel: str | None) -> str | None:
    """渠道标识 → 平台名。**没见过的渠道原样返回**，不猜。"""
    if not channel:
        return None
    return _CHANNEL_LABELS.get(channel, channel)


def render(facts: EnvFacts) -> str:
    """把环境事实渲染成一段给模型看的中文。

    ## 为什么缺数据时是「少说一句」而不是「说未知」

    用户要求"只阐述事实"。而「关系未知」「账号未知」**看起来也是事实
    陈述**——模型会把它当成一个已知状态去推理（"她还不知道我是谁"），
    而真相只是"这个字段没接上"。前者会导致她做出错误的社交判断，后者
    什么都不影响。

    所以：**有的说，没有的不提**。整段全空时由调用方决定不注入
    （`EnvFacts.has_anything`）。

    ## 为什么不用「你」开头

    这段话是**旁白**，不是第二人称指令。写成"你现在在 QQ 群聊"会和
    `relationship` 车道里那句"你在群聊里"重复，而两处的更新时机不同。
    用「当前」开头明确这是状态描述，不是对她说的话。
    """
    if not facts.has_anything:
        return ""

    lines: list[str] = []

    # --- 平台 + 会话类型 + 会话标识 ---
    #
    # 这三件合成一句：「当前在 QQ 群聊」比「平台：QQ\n会话类型：群聊」
    # 省 10 个字符，而且读起来是一件事而不是三行表单。
    platform = channel_label(facts.channel)
    if platform or facts.window_ref:
        where = platform or "未知渠道"
        kind = "群聊" if facts.is_group else "私聊"
        # 「QQ 群聊」而不是「QQ 的群聊」：中文里平台名直接修饰会话类型
        # 更顺，而这段是要被模型当自然语言读的，不是表单。
        head = f"当前在 {where} {kind}"
        if facts.window_ref:
            head += f"（{facts.window_ref}）"
        lines.append(head)

    # --- 我们这侧的账号 / 实例 ---
    #
    # 「哪个号在说话」在群聊里尤其重要：一个部署可能挂两个 QQ 号，
    # 而她需要知道"现在是我在说"而不是"另一个我在说"。
    if facts.account or facts.instance:
        parts: list[str] = []
        if facts.account:
            parts.append(f"账号 {facts.account}")
        if facts.instance:
            parts.append(f"实例 {facts.instance}")
        lines.append("登录身份：" + "，".join(parts))

    # --- 时间 ---
    #
    # `worker.py:2854` 记过同类教训：不给模型当前时间，它会顺着"今天"
    # 排出已经过去的时段。这里只说钟点，**不说"该休息了"**——那是判断。
    if facts.local_now is not None and facts.timezone:
        stamp = facts.local_now.strftime("%Y-%m-%d %H:%M")
        weekday = "一二三四五六日"[facts.local_now.weekday()]
        lines.append(f"此刻：{stamp}（周{weekday}，{facts.timezone}）")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 取数（全部读既有表，不新建事实来源）
# --------------------------------------------------------------------------- #


# 账号 / 实例这一项**目前没有数据源**，所以不实现。
#
# 我逐个查过了：
#   - `Window`（`infra/models.py:180`）只有 `channel` / `title` / `visibility`，
#     没有"我们用哪个号在说话"；
#   - `Window.channel` 存的是 `connector_id`（`bridge.py:1358` 取
#     `self._connector.connector_id`），它是**类级常量**（`onebot`/`qq`/`qqbot`），
#     一个部署挂两个 QQ 号时两个窗口的 channel 完全相同；
#   - `InteractionEnvelope.connector_instance`（`contracts/v1.py:294`）是**入站
#     时**才有的实例名，落库时只写进 `actor_accounts.connector_instance`——
#     而 `actor_accounts`（`infra/models.py:120`）记的是**对方**的外部账号，
#     不是我们自己的；
#   - 我们自己的 QQ 号只存在于 `OneBotConfig.self_id` 或 `get_login_info` 的返回
#     里（`connectors/onebot/connector.py:773` / `:1607`），而 `ContextCompiler`
#     拿不到 connector 实例。
#
# 所以「账号 xxx」这件事**当前无法如实陈述**。按"只阐述事实"的要求，
# 宁可不写，也不写一个猜的值（`Window.title` 是群号/好友号，把它当账号念
# 出来会让她说"我这个号是 12345"，那是**事实错误**）。
#
# 要补这件事，需要在 `windows` 上增加 `connector_instance` 列并在建窗时写入——
# 那是 `infra/models.py` + 迁移的改动，超出本任务写作用域，已列入报告。


async def collect(
    session: object,
    *,
    window: object,
    subject_id: str,
    now: datetime | None = None,
) -> EnvFacts:
    """从既有数据源组装一轮的环境事实。

    `window` 就是 `context.py` 里已经取到的那一行（`get_window_for_turn`），
    **不重新查**：那个函数已经处理了"群窗口对所有成员开放"的归属规则，
    再查一次等于把同一条规则维护两遍。
    """
    from ling.infra.models import Window

    channel = getattr(window, "channel", None) if isinstance(window, Window) else None
    title = getattr(window, "title", None) if isinstance(window, Window) else None
    visibility = getattr(window, "visibility", None) if isinstance(window, Window) else None
    # 群聊判定走**唯一实现** `kernel/visibility.py`，不自己写
    # `visibility == "group_scope"`。
    #
    # 这个仓库里"群聊判定"曾经有 20+ 处各自为政，判据分两派（读
    # `Window.visibility` 的 / 拿 `":group:" in window_ref` 做字符串匹配的），
    # 并且已经炸过一次：`worker.py` 里那个恒为 False 的判定让群聊提炼的
    # boundary/health/family 全部沉淀成了长期记忆（见 `worker.py:414-426`）。
    # 我这一处若自己再写一遍，就是在重造那个坑——哪怕现在的写法刚好是对的。
    from ling.kernel.visibility import is_group_visibility

    is_group = is_group_visibility(visibility)

    # 时区：走**唯一实现**（DB 偏好 > `LING_USER_TIMEZONE` > 默认），
    # 不自己 `os.environ.get(...) or "UTC"`——`config.py:173` 明确要求
    # 不要在调用点各自解析，否则"设置页填了时区但对这句话不生效"。
    tz_name: str | None = None
    local_now = now
    try:
        from ling.settings.user_preferences import load_user_timezone

        tz_name, _source = await load_user_timezone(
            session,  # type: ignore[arg-type]
            subject_id=subject_id,
            env=os.environ.get("LING_USER_TIMEZONE") or None,
        )
        if tz_name:
            from zoneinfo import ZoneInfo

            local_now = (now or _utcnow()).astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 - 时区读不到不该阻断对话
        # 拿不到就不说时间。**不退回 UTC**：把 UTC 当本地时间念出来，
        # 她会说"才下午三点呢"，而用户那边已是深夜——比不说更糟。
        tz_name = None
        local_now = None

    return EnvFacts(
        channel=channel,
        is_group=is_group,
        window_ref=title,
        local_now=local_now,
        timezone=tz_name,
    )
