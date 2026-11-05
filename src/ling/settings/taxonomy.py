"""设置分类法：**产品代码里的唯一权威**。

为什么要在代码里而不是只写文档：分类法一旦只存在于文档里，它就没法被检查。
测试要抄一份常量、前端要抄一份名称、文档自己再维护一份——三份必然漂。
这里定义一次，文档由测试比对，接口由这里生成，前端只读接口。

分类轴只有一条：**这个设置直接改变哪一个系统决策。**
不是按代码模块分，不是按存储位置分，不是按生效阶段分。
详见 ``docs/SETTINGS-TAXONOMY.md``。

``editable_route`` 是这一类的**可编辑入口**。为 ``None`` 表示"这一类还没有
任何设置面"——前端不应为它渲染空页面（见 ``docs/SETTINGS-TAXONOMY.md`` §7
"不建空壳"）。这个字段也是诚实性的锚：测试会验证每个非 None 的路由
真的存在于 FastAPI 路由表里，防止分类法声称自己可用而实际没实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class StorageKind(StrEnum):
    """一个设置项的值存在哪里。分类与存储**分开判定**。"""

    config_revision = "config_revision"
    """DB 配置修订：草稿→校验→发布→回滚。用户选的持续策略。"""

    env = "env"
    """环境变量：必须在配置服务可用前提供，且不能覆盖已发布配置。"""

    business_table = "business_table"
    """业务表：运行中产生的事实、事件、学习结果、一次具体授权记录。"""

    secret_store = "secret_store"
    """加密凭证库：provider/渠道运行时用的秘密。修订只存密文+后四位+指纹。"""


@dataclass(frozen=True)
class SubGroup:
    """二级分组。不超过 2 层，否则用户要找第三次。"""

    key: str
    label: str
    defaults_to: StorageKind


@dataclass(frozen=True)
class Category:
    """一级类。``index`` 同时是归属判定序列里的问序。"""

    index: int
    key: str
    label: str
    verb: str
    """一句话：改这一类，是在改什么。"""
    boundary: str
    """硬边界问题。答"是"才归这一类。必须是可执行的判别，不是形容词。"""
    groups: tuple[SubGroup, ...]
    editable_route: str | None = None
    """可编辑入口。``None`` = 尚无设置面，前端不得渲染空页面。

    必须是一个 **GET 可达**的端点——用户进这一类时先读它。
    不要填"发布"之类的子步骤（那只接受 POST），否则前端取不到初值。
    """
    status_route: str | None = None
    """只读状态入口（能力探测、健康、统计）。"""

    @property
    def available(self) -> bool:
        return self.editable_route is not None


CATEGORIES: tuple[Category, ...] = (
    Category(
        index=1,
        key="identity",
        label="身份与人格",
        verb="改「它是谁」",
        boundary="固定模型、表达格式与当前情境后，改它是否仍改变角色身份或跨情境的稳定行为倾向？",
        groups=(
            SubGroup("runtime_identity", "运行身份", StorageKind.config_revision),
            SubGroup("stable_persona", "稳定人格", StorageKind.business_table),
        ),
        editable_route="/v1/persona",
    ),
    Category(
        index=2,
        key="expression",
        label="语言与声音",
        verb="改「它说成什么样」",
        boundary="已决定要交互、且引擎与渠道已定后，改它是否改变措辞、输出模态或声音呈现？",
        groups=(
            SubGroup("speaking_style", "说话方式", StorageKind.config_revision),
            SubGroup("voice_presentation", "声音呈现", StorageKind.config_revision),
        ),
        # 声音呈现整组还没实现；说话方式也还没有独立设置面。
    ),
    Category(
        index=3,
        key="model",
        label="模型与生成",
        verb="改「谁来算」",
        boundary="期望表达不变时，改它是否改变 provider/model/凭证引用或推理预算、解码、回退决策？",
        groups=(
            SubGroup("role_binding", "职责绑定", StorageKind.config_revision),
            SubGroup("provider_secret", "供应商与凭证", StorageKind.secret_store),
            SubGroup("inference", "推理与回退", StorageKind.config_revision),
        ),
        editable_route="/v1/settings/config",
        status_route="/v1/settings/health",
    ),
    Category(
        index=4,
        key="memory",
        label="记忆与学习",
        verb="改「它记住/学会什么」",
        boundary="允许处理的数据范围不变时，改它是否改变经历的提取、保存、召回或学习决策？",
        groups=(
            SubGroup("formation", "记忆形成", StorageKind.config_revision),
            SubGroup("recall", "召回", StorageKind.config_revision),
            SubGroup("learning", "学习", StorageKind.business_table),
        ),
        # 策略面走 /v1/settings/config 的 memory 段（召回下限、重排头部、
        # 时间衰减半衰期、最长保留）。/v1/memory 与 /v1/core-memory 是
        # **内容浏览与删除**，不是设置，别把它们当编辑入口。
        editable_route="/v1/settings/config",
    ),
    Category(
        index=5,
        key="dialogue",
        label="对话与主动性",
        verb="改「要不要开口」",
        boundary="生活状态与表达形式不变时，改它是否改变是否、何时、以何种频率发起或接续交流？",
        groups=(
            SubGroup("rhythm", "对话节奏", StorageKind.config_revision),
            SubGroup("proactive", "主动联系", StorageKind.config_revision),
        ),
        # 策略面走 /v1/settings/config 的 dialogue 段（群聊发言预算、
        # 主动联系日限额、免打扰时段）。/v1/proactive 是**只读状态**，
        # 不是设置入口。
        editable_route="/v1/settings/config",
        status_route="/v1/proactive",
    ),
    Category(
        index=6,
        key="channel",
        label="渠道与连接",
        verb="改「从哪条线走」",
        boundary="交流意图与内容不变时，改它是否只改变适配器、账号、端点或渠道可承载的能力？",
        groups=(
            SubGroup("adapter", "接入配置", StorageKind.env),
            SubGroup("capability", "渠道能力", StorageKind.business_table),
        ),
        # 能力位是平台探测结果，只读。接入配置仍在 env，没有 DB 设置面。
        status_route="/v1/channels",
    ),
    Category(
        index=7,
        key="privacy",
        label="权限与隐私",
        verb="改「准不准」",
        boundary="功能策略不变时，改它是否改变调用能力/读写数据/可见范围的准许结果？",
        groups=(
            SubGroup("capability_grant", "能力授权", StorageKind.business_table),
            SubGroup("information_boundary", "信息边界", StorageKind.business_table),
        ),
        editable_route="/v1/consents",
    ),
    Category(
        index=8,
        key="life",
        label="生活与活动",
        verb="改「它自己在干嘛」",
        boundary="完全不发送消息时，改它是否仍改变日程、presence、activity 或 goals 的状态转移？",
        groups=(
            SubGroup("routine_scene", "作息与场景", StorageKind.config_revision),
            SubGroup("activity_goal", "活动与目标", StorageKind.business_table),
        ),
        # 策略面走 /v1/settings/config 的 life 段（场景 TTL、精力衰减、
        # 社交负担、同时挂几件小事）。/v1/activity 与 /v1/goals 是**内容管理**，
        # 不是设置。作息（几点醒/几点睡）不在这里——它按 actor 存在
        # actor_routines 表，再放一份到配置就是第二份权威。
        editable_route="/v1/settings/config",
    ),
    Category(
        index=9,
        key="system",
        label="系统与部署",
        verb="改「服务怎么跑」",
        boundary="换掉角色及其交流策略后，改它是否仍决定监听、数据库连接、test_mode 或告警行为？",
        groups=(
            SubGroup("startup", "启动与数据", StorageKind.env),
            SubGroup("operations", "运行管理", StorageKind.env),
        ),
        status_route="/v1/settings/health",
    ),
)


BY_KEY: dict[str, Category] = {c.key: c for c in CATEGORIES}
BY_INDEX: dict[int, Category] = {c.index: c for c in CATEGORIES}


def editable_categories() -> tuple[Category, ...]:
    """已经有设置面的一级类。前端只应渲染这些。"""
    return tuple(c for c in CATEGORIES if c.available)


def summary() -> dict[str, int]:
    return {
        "total": len(CATEGORIES),
        "editable": len(editable_categories()),
        "pending": sum(1 for c in CATEGORIES if not c.available),
    }
