"""ling 对外契约 v1（冻结）。

只放 设计文档 §10 冻结的少数公开 DTO 与运行期作用域。所有对象带 ``schema_version``；
破坏性变化升 major；未知必需字段失败关闭（``extra="forbid"``）。

本模块不得 import 任何领域实现，保证契约与实现解耦。
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION: Literal["v1"] = "v1"

IdStr = Annotated[str, Field(min_length=1, max_length=128)]

# 媒体引用：与 ``IdStr`` **故意不同**。
#
# ``media_refs`` 装的不是内部 ID，而是渠道给的外部引用。QQ 的图片 URL 实测
# 300+ 字符（``https://multimedia.nt.qq.com.cn/download?appid=...``），用
# 128 字符的 ``IdStr`` 会**在入站校验处整条消息被拒**——用户发"一张图 +
# 一句话"，机器人毫无反应，而且正文也一起丢了。长度上限按 URL 的实际需要
# 给到 2048（HTTP 客户端与 CDN 常见实现的上限）。
#
# 但**不能**放任成任意字符串：这个值最终会交给 ``fetch_media(media_ref)``
# 去取字节（见 ``domains/media/describe.py`` 与各 connector 的 fetch_media），
# ``file:``/``data:`` 之类等于开 SSRF 与本地文件读取的口子。规则：
#
#   * 带 scheme 的（``xxx://…`` 或 ``xxx:…``）**只允许 http/https**，
#     且必须能解析出主机名；
#   * **不带 scheme 的**视为不透明 id / 文件名，原样放行。这不是"宽松"，
#     而是 OneBot v11 的真实形态：image/voice/video/file 段的 ``file`` 字段
#     是"文件名或文件 id"，``url`` 是可选的——
#     ``connectors/onebot/message.py`` 的 ``ref = data.get("url") or data.get("file")``
#     就是为这种实现兜底的；CQ 码字符串 ``[CQ:image,file=abc.jpg]`` 也只有
#     file、没有 url。把这些拒掉，等于对另一批部署重新制造同一个 bug。
#     不认识的形态由 ``fetch_media`` 自己拒绝（它只认 http/https）。
_MEDIA_REF_SCHEME = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*):")


def _validate_media_ref(value: str) -> str:
    """媒体引用校验：有 scheme 的只放行 http/https，其余按不透明 id 放行。

    只校验、不改写：改写会在入库与下载之间引入两份不同的值。
    """
    text = value.strip()
    match = _MEDIA_REF_SCHEME.match(text)
    if match is None:
        return value
    scheme = match.group("scheme").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(
            f"媒体引用只允许 http/https，或不带 scheme 的 id，收到 {scheme!r}"
        )
    if not urlsplit(text).hostname:
        raise ValueError("媒体引用的 http(s) URL 缺少主机名")
    return value


MediaRef = Annotated[
    str,
    Field(min_length=1, max_length=2048),
    AfterValidator(_validate_media_ref),
]


def media_ref_is_valid(value: str) -> bool:
    """这条外部媒体引用是否满足 ``MediaRef``？

    给**渠道层**用的：一条坏引用（``file:``、超长 URL）只该让那个附件失效，
    不该让整条消息连正文一起被契约拒掉。渠道在构造信封前用它逐条过滤，
    ``MediaRef`` 仍然是最后一道闸（防止别的生产者绕过过滤）。
    """
    try:
        _validate_media_ref(value)
    except ValueError:
        return False
    return 1 <= len(value) <= 2048


def new_id(prefix: str) -> str:
    """生成带语义前缀的稳定 ID（便于日志阅读，不保证全局排序）。"""
    return f"{prefix}_{uuid.uuid4().hex}"


class StrictModel(BaseModel):
    """契约基类：未知字段失败关闭，版本字段必填。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CompatModel(BaseModel):
    """**加法**契约基类：多出来的字段原样保留（``extra="allow"``）。

    只给「新契约 + 旧字段还要活一个版本」这类响应体用：顶层字段必须逐个**声明**
    （否则读 schema 的人不知道响应里有什么），嵌套里的历史字段允许透传，不必为了
    一个过渡期把运行期字段全搬进契约。新代码不要用它——新契约一律 ``StrictModel``。
    """

    model_config = ConfigDict(extra="allow", frozen=True)


# ---------------------------------------------------------------- 枚举


class Visibility(StrEnum):
    actor_private = "actor_private"
    subject_private = "subject_private"
    window_private = "window_private"
    group_scope = "group_scope"
    public = "public"
    admin_only = "admin_only"


class Origin(StrEnum):
    """经历来源。六者永不混写为同一种事实。"""

    simulated = "simulated"
    user_statement = "user_statement"
    external_observation = "external_observation"
    verified_source = "verified_source"
    tool_receipt = "tool_receipt"
    model_inference = "model_inference"


class Modality(StrEnum):
    text = "text"
    audio = "audio"
    image = "image"
    video = "video"
    structured = "structured"


class TurnState(StrEnum):
    """一个聊天轮次的服务端权威状态。"""

    pending = "pending"
    streaming = "streaming"
    stopping = "stopping"
    completed = "completed"
    stopped = "stopped"
    failed = "failed"


class OperationState(StrEnum):
    """外部动作/投递状态。计划不等于完成。"""

    proposed = "proposed"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    uncertain = "uncertain"
    cancelled = "cancelled"


class DeliveryState(StrEnum):
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    uncertain = "uncertain"
    unsupported = "unsupported"


class ErrorCode(StrEnum):
    invalid_request = "invalid_request"
    unauthorized = "unauthorized"
    forbidden = "forbidden"
    not_found = "not_found"
    conflict = "conflict"
    rate_limited = "rate_limited"
    provider_unavailable = "provider_unavailable"
    provider_timeout = "provider_timeout"
    provider_error = "provider_error"
    cancelled = "cancelled"
    unsupported_capability = "unsupported_capability"
    config_invalid = "config_invalid"
    secret_missing = "secret_missing"
    quota_exhausted = "quota_exhausted"
    internal = "internal"


class ModelRole(StrEnum):
    """按职责选择模型的 profile 键（设计文档 §8.1）。"""

    dialogue_fast = "dialogue.fast"
    dialogue_deep = "dialogue.deep"
    dialogue_group = "dialogue.group"
    perception_vision = "perception.vision"
    # 看视频（媒体证据 kind=video）。与 perception.vision 分开：同一个供应商
    # kind 下既有只认图片的模型，也有能读多帧/视频的模型，合成一个职责就没法
    # 分别绑定。能力仍按 ``chat`` 判（见 ports/model.py::PROFILE_CAPABILITIES）。
    perception_video = "perception.video"
    speech_asr = "speech.asr"
    speech_tts = "speech.tts"
    realtime_voice = "realtime.voice"
    memory_extract = "memory.extract"
    memory_embed = "memory.embed"
    memory_rerank = "memory.rerank"
    reflection = "reflection"
    planning = "planning"
    moderation_advisory = "moderation.advisory"


class TimelineState(StrEnum):
    """只读生活时间轴的对外状态（设计 §7/§8）。

    这几种状态**必须互相区分**，否则"今天没安排"和"读不到今天"在界面上
    长得一模一样：

    - ``empty``：这一天没有计划行——真的没有，不是故障；
    - ``pending``：行已建、大纲还没生成（例如 21:00 到期前）；
    - ``ready``：模型或规则大纲已提交，内容可当计划读；
    - ``degraded``：有行，但内容是规则兜底（模型不可用/输出被拒收）；
    - ``failed``：最近一次生成失败，原因在 ``last_error_code``；
    - ``unavailable``：这一次读取出错，投影拿不到。
    """

    ready = "ready"
    pending = "pending"
    empty = "empty"
    degraded = "degraded"
    failed = "failed"
    unavailable = "unavailable"


class OutlineRelation(StrEnum):
    """仪表盘「她今天可能做什么」里一条摘要与此刻的关系。

    ``now`` 是正在进行的段，``next`` 是接下来最近的一段，``anchor`` 是已确认的
    不可移动锚点（约定）。三者**不合并**：把"正在做"和"已约好"混成一条，
    用户会以为计划等于承诺。
    """

    now = "now"
    next = "next"
    anchor = "anchor"


# ---------------------------------------------------------------- 作用域


class RuntimeScope(StrictModel):
    """服务器解析出的本轮权威作用域。客户端传入的同名字段只作提示。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    actor_id: IdStr
    persona_id: IdStr
    subject_id: IdStr
    window_id: IdStr
    scene_id: IdStr | None = Field(
        default=None, description="保留位：场景由 presence 域持有，不在作用域里"
    )
    device_id: IdStr | None = None
    account_id: IdStr | None = None
    visibility: Visibility = Visibility.subject_private
    consent_revision: int = Field(default=0, ge=0)
    config_revision: int = Field(default=0, ge=0)
    trace_id: IdStr | None = None


class AudienceRule(StrictModel):
    """受众规则：谁能看到哪一类内容。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    visibility: Visibility
    owner_subject_id: IdStr | None = None
    allow_cross_window: bool = False
    requires_explicit_consent: bool = True
    note: str | None = None


# ---------------------------------------------------------------- 输入


class InputPart(StrictModel):
    modality: Modality
    text: str | None = None
    media_ref: MediaRef | None = None
    mime_type: str | None = None


class InteractionEnvelope(StrictModel):
    """入站交互信封。渠道只做协议翻译，不改写语义。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    connector_instance: IdStr
    external_event_id: IdStr
    event_id: IdStr | None = None
    received_at: datetime | None = None
    occurred_at: datetime | None = None
    account_ref: IdStr | None = None
    # 发送者在渠道上的显示名（QQ 群昵称等）。
    #
    # **只是标签，绝不是身份**：QQ 群昵称可以随时改，也可能完全重名。
    # 身份解析永远用 (connector_instance, account_ref)，这一项只用于
    # 让她能叫出对方名字，以及让不同人的名字可区分地显示。
    sender_name: str | None = Field(
        default=None,
        description="发送者显示名，仅作标签；同名不同人靠 account_ref 区分",
    )
    # 发送者的**昵称**（账号自己的名字，跨群稳定），与 ``sender_name`` 分开。
    #
    # 为什么要两档：
    # - ``sender_name`` 是**本次称呼**（群名片优先）——显示用，改名下一句就生效；
    # - ``sender_nickname`` 是**人级**字段——落库更新 ``subjects.display_name``
    #   只能用它。用群名片写人级列，会被"他在某个群改了个名片"带着在群之间抖动，
    #   而且别人的私聊标题也会跟着变（见 docs/GROUP-CARD-RESEARCH.md）。
    #
    # 旧连接器不发这个字段也能跑（默认 None），此时落库侧退回用 ``sender_name``。
    sender_nickname: str | None = Field(
        default=None,
        description="发送者昵称（人级、跨群稳定）；缺省时落库退回使用 sender_name",
    )
    device_ref: IdStr | None = Field(
        default=None, description="保留位：目前没有设备概念，读它永远得到 None"
    )
    window_ref: IdStr
    scene_ref: IdStr | None = Field(
        default=None, description="保留位：场景由 presence 域推断，不从信封传"
    )
    turn_ref: IdStr | None = Field(
        default=None,
        description="保留位：入站时轮次还不存在（由 bridge 创建）",
    )
    audience_ref: IdStr | None = Field(
        default=None,
        description="保留位：受众由 window_ref + visibility 决定，不从信封传",
    )
    modality: Modality = Modality.text
    parts: list[InputPart] = Field(default_factory=list)
    reply_to: IdStr | None = None
    media_refs: list[MediaRef] = Field(
        default_factory=list,
        description="外部媒体引用（URL 或渠道不透明 id）；长度与协议见 MediaRef",
    )
    # 渠道层能直接判断的"是不是在跟机器人说话"（@、引用机器人消息）。
    # 必须由渠道提供：正文里 @ 已被剥离，靠文本反推会永远得到"没点名"，
    # 群聊就永远不会接话。
    mentions_actor: bool = False
    origin: Origin = Origin.user_statement
    consent_revision: int | None = Field(default=None, ge=0)
    trace_id: IdStr | None = None

    @field_validator("parts")
    @classmethod
    def _at_least_one_part(cls, v: list[InputPart]) -> list[InputPart]:
        if not v:
            raise ValueError("parts 至少需要一个输入片段")
        return v


# ---------------------------------------------------------------- 会话与轮次


class HandoffCapsule(StrictModel):
    """同人跨端交接胶囊：默认 30 分钟、仅本人已绑定私聊端。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    capsule_id: IdStr
    subject_id: IdStr
    source_window_id: IdStr
    recent_topic: str | None = None
    open_questions: list[str] = Field(default_factory=list)
    short_quote: str | None = None
    visibility: Visibility = Visibility.subject_private
    consent_revision: int = Field(default=0, ge=0)
    created_at: datetime
    expires_at: datetime


class ContextContribution(StrictModel):
    """ContextCompiler 的一条车道贡献，含来源、受众、有效期与预算。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    lane: Literal[
        "persona",
        "self_state",
        "scene",
        "relationship",
        "world",
        "short_memory",
        # 核心记忆：每轮常驻、不依赖检索命中的少量要点
        "core_memory",
        "evidence",
        # 外部可核验知识（来源 URL/时效/置信度）。与 evidence（本人记忆）
        # 分开：二者可信等级不同，"别人说的"不能和"我对你的了解"混写。
        "knowledge",
        # 媒体证据：用户发来的图片/语音被看到或被推断出什么。
        # 与 evidence 分开：媒体有自己的有效期，而且"模型看图推断的"
        # 必须能和"用户说的"区分开（origin 字段承载这个区分）。
        "media",
        "goals",
        "capabilities",
        "constraints",
        # 场景提示：人设里写的"这种场合怎么处理"。单独一条车道而不是
        # 并进 persona，因为它可能为空、也可能被单独截断——混进人格车道
        # 之后，"人格被截了一半"和"这个场景没写"在日志里长得一样。
        "scenario",
    ]
    text: str
    origin: Origin
    visibility: Visibility
    source_event_ids: list[IdStr] = Field(default_factory=list)
    valid_until: datetime | None = None
    token_budget: int = Field(default=0, ge=0)


class SessionRecord(StrictModel):
    """会话窗口的权威记录（不是浏览器缓存）。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    session_id: IdStr
    actor_id: IdStr
    subject_id: IdStr
    window_id: IdStr
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = Field(default=0, ge=0)
    archived: bool = False
    # 这个会话**叫什么**（群名 / 渠道侧昵称）。``title`` 不承担这个职责：
    # 渠道窗口的 title 里存的是平台窗口标识（``onebot-x:group:100…``），
    # 那是投递用的坐标，不是给人看的名字。
    #
    # 采集不到就是 ``null``——不要拿"群 100…"或裸 subject id 冒充名字：
    # 前端能按 ``channel`` + ``peer_id`` 自己回退，编一个假名字反而盖掉了
    # "我们没采集到"这个事实。
    display_name: str | None = None
    # 窗口所在的渠道（``web`` / ``onebot`` / ``qqbot`` …）。前端据它决定
    # 副标题怎么显示（web 会话没有平台对端）。
    channel: str = "web"
    # 平台侧对端 id：群聊是群号、私聊是对方 QQ 号；web 窗口为 ``null``。
    peer_id: str | None = None


class ChatMessage(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    message_id: IdStr
    turn_id: IdStr | None = None
    role: Literal["user", "actor", "system"]
    text: str
    state: TurnState = TurnState.completed
    created_at: datetime
    sequence: int = Field(default=0, ge=0)
    origin: Origin = Origin.user_statement
    error_code: ErrorCode | None = None
    model_label: str | None = None
    # 同一轮的第几次尝试。重试不删除已交付文本，前端按 (turn_id, attempt) 编组展示
    attempt: int = Field(default=1, ge=1)
    # 她**怎么处置**这条消息：replied / ignored / missed。
    # 与"看没看见"（attention_state）是两件事。
    #
    # 为什么必须暴露给前端：她选择不回时，用户看到的是一条没有回应的消息。
    # 没有这个字段，那看起来像故障；有了它才能显示"她没回，因为……"——
    # DESIGN 的原则是沉默和失败都要能被解释。
    handled_as: str | None = None
    handled_reason: str | None = None
    # **谁说的**（人话名字）。群聊里是发言人的群名片，没有名片才用昵称。
    # ``null`` 表示我们确实不知道——历史消息没有采到名字，
    # 或者这条是她自己说的。不要用 "用户" / "群友" 之类的占位符填空：
    # 那会让"不知道"看起来像"知道"。
    sender_name: str | None = None
    # 发言人的平台标识（QQ 号；解析不到时退回 subject id）。
    # 前端用它把人分组，所以它必须对同一个人**始终是同一个值**。
    sender_id: str | None = None


# ---------------------------------------------------------------- 能力与回执


class CapabilityDescriptor(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    capability_id: IdStr
    version: str
    modalities: list[Modality] = Field(default_factory=list)
    supports_private: bool = True
    supports_group: bool = False
    supports_proactive: bool = False
    supports_edit: bool = False
    supports_media: bool = False
    max_text_length: int = Field(default=4096, ge=1)
    receipt_reliability: Literal["none", "best_effort", "confirmed"] = "best_effort"
    enabled: bool = False


class ActionIntent(StrictModel):
    """模型提出的行动候选；确定性代码决定是否执行。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    intent_id: IdStr
    kind: str
    target_ref: IdStr | None = Field(
        default=None,
        description="保留位：动作对象由 payload 内的字段表达，目前不由这里指定",
    )
    payload: dict[str, object] = Field(default_factory=dict)
    requires_confirmation: bool = True
    reversible: bool = False


class OperationResult(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    operation_id: IdStr
    kind: str
    state: OperationState
    created_at: datetime
    updated_at: datetime
    detail: str | None = None
    receipt_ref: IdStr | None = Field(
        default=None,
        description="保留位：回执按 operation_id 查，不从这里引用",
    )
    error_code: ErrorCode | None = None


class DeliveryReceipt(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    receipt_id: IdStr
    operation_id: IdStr
    channel: IdStr
    state: DeliveryState
    platform_message_id: IdStr | None = None
    observed_at: datetime
    detail: str | None = None


class MemoryProposal(StrictModel):
    """模型提出的记忆候选，必须带证据；由 Memory 域决定是否落库。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    proposal_id: IdStr
    kind: Literal["episode", "assertion", "procedure"]
    text: str
    origin: Origin
    source_event_ids: list[IdStr] = Field(min_length=1)
    visibility: Visibility
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    valid_until: datetime | None = None


# ---------------------------------------------------------------- 生活时间轴（只读）

# 这一组是**只读投影**的契约（设计 §7）：`GET /v1/life/timeline` 与
# `GET /v1/dashboard` 的 `today_outline` / `intent_teaser` 两块。
#
# 三条不可让步的语义，都在下面字段名里体现：
#
# 1. **双时区**：`display_*` 是按**当前登录者**的有效时区裁剪的展示窗口，
#    `plan_*` / `actor_timezone` 是**角色**的计划日与规范时区。两者可以不同，
#    前端不许用浏览器本地时区二次判断"今天"。
# 2. **计划不是事实**：`segments` 是"打算/可能"，`facts` 是已发生的活动进程。
#    计划时段结束**不会**把段改成"已做"，所以两层分开列，字段也不共用。
# 3. **空态与故障分开**：`state` 见 :class:`TimelineState`；梦被受众裁决隐藏时
#    `dream.restricted=true` 而不是 `dream=null`（后者表示"没有记录"）。


class LifeTimelineSegment(StrictModel):
    """时间轴上的一个段（大块/小时/分钟）。父子段都会返回，前端据此折叠层级。

    ``start_at`` / ``end_at`` 是**原始**区间；``display_*`` 是裁剪到展示日窗口
    后的区间。跨午夜的段在两天各自被裁剪，并用 ``continues_*`` 说明还有
    另一半——**不要**把裁剪后的时间当成段的真实起止。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    segment_id: IdStr
    plan_id: IdStr
    # 这个段属于哪一天的**角色计划日**。通常等于顶层 `plan_day`；
    # 两套时区不一致、或段跨过午夜时会等于相邻那一天。
    plan_day: date
    parent_id: IdStr | None = None
    layer: str
    is_leaf: bool
    status: str = Field(description="current / superseded / cancelled")
    revision: int = Field(default=0, ge=0, description="创建该段的计划版本号")
    source: str = Field(description="model / rule / baseline / user")
    anchor_kind: str = Field(description="'' 可变段；sleep/commitment 是不可移动锚点")
    commitment_level: str = Field(description="tentative / confirmed")
    place_id: IdStr | None = None
    title: str
    start_at: datetime
    end_at: datetime
    display_start_at: datetime
    display_end_at: datetime
    continues_from_previous_day: bool = False
    continues_next_day: bool = False


class LifeTimelineFact(StrictModel):
    """已发生的活动进程（**不是**计划段）。

    ``evidence_kind`` 是完成证据的种类：``clock_only`` 只证明"时间到了"，
    不能当成她做过；``observed`` / ``user_confirmed`` 才是事实。
    ``scene_valid_until`` 只在情景提交仍有效时给出（过期即 unknown）。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    activity_id: IdStr
    state: str
    origin: str
    evidence_kind: str
    scene_valid_until: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    place_id: IdStr | None = None


class DayIntentSummary(StrictModel):
    """未来 7 天展望里的一条长程意向（**不是**那一天的日计划）。

    ``state=confirmed`` 只表示用户对这个目标/日期作过明确确认，**不是**
    "某个时段已经约好"：`schedule_intentions` 永远不提供硬锚点。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    intention_id: IdStr
    target_start_day: date
    target_end_day: date
    state: str
    source_kind: str
    visibility: str
    priority: int = Field(default=50, ge=0, le=100)


class DreamCard(StrictModel):
    """一晚的梦（模拟叙事）。**不是事实**，且受众受限。

    梦在库里固定 ``origin=simulated`` / ``visibility=actor_private``。非 owner
    读到时只给状态与来源，``title`` / ``narrative`` 为空且 ``restricted=true``
    ——"存在但你看不到"与"没有记录"（``dream=null``）是两件事。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    status: str = Field(description="ready / failed / missed / no_dream")
    origin: str
    visibility: str
    title: str | None = None
    narrative: str | None = None
    restricted: bool = False


class LifeTimelineOut(StrictModel):
    """`GET /v1/life/timeline` 的响应：一天的只读时间轴投影。

    ``generated_at`` 是**当前版本提交时刻**（`day_plans.updated_at`）；
    ``outline_due_at`` 是生成到期时刻（D−1 角色本地 21:00）。两者分开：
    "应当什么时候生成"和"实际上什么时候生成"不是同一件事。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    state: TimelineState
    plan_state: str | None = Field(
        default=None, description="库里 day_plans.state 原值：pending/ready/degraded；无行为 null"
    )
    revision: int = Field(default=0, ge=0, description="计划版本号；0 = 没有计划行")
    last_error_code: str | None = Field(
        default=None, description="最近一次失败/降级/待复核的原因码（不含正文）"
    )
    display_date: date
    display_timezone: str
    display_timezone_source: str = Field(description="preference / env / default")
    display_window_start_at: datetime
    display_window_end_at: datetime
    actor_timezone: str
    actor_timezone_source: str = Field(description="preference / env / default")
    plan_day: date
    plan_window_start_at: datetime
    plan_window_end_at: datetime
    outline_due_at: datetime
    timezone_source: str = Field(description="计划行记录的时区来源；无行时取角色侧解析结果")
    generated_at: datetime | None = None
    segments: list[LifeTimelineSegment] = Field(default_factory=list)
    facts: list[LifeTimelineFact] = Field(default_factory=list)
    intents: list[DayIntentSummary] = Field(default_factory=list)
    horizon_unplanned: bool = Field(
        default=True,
        description="未来日**尚未生成大纲**：intents 是展望，不是 D+1…D+7 的日计划",
    )
    dream: DreamCard | None = None
    superseded_segment_count: int = Field(
        default=0, ge=0, description="同一窗口内已被新版替代的段数（计划改动过的证据）"
    )
    hidden_segment_count: int = Field(default=0, ge=0)
    hidden_fact_count: int = Field(default=0, ge=0)
    hidden_intent_count: int = Field(default=0, ge=0)
    has_private_content: bool = Field(
        default=False, description="有内容因受众裁决被隐藏（参见三个 hidden_* 计数）"
    )
    note: str | None = Field(default=None, description="给人看的一句状态说明（纯文本）")


class TodayOutlineItem(StrictModel):
    """仪表盘上的一条今日摘要。``relation`` 说明它和"此刻"的关系。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    segment_id: IdStr | None = None
    relation: OutlineRelation
    title: str
    start_at: datetime
    end_at: datetime


class TodayOutlineOut(StrictModel):
    """`GET /v1/dashboard` 的 `sections.today_outline.data`。

    与旧的 `sections.agenda` 并列：`agenda` 是"最多几件小事"的旧语义，
    这里是她**这一天时间轴**的 2–3 条摘要（当前段 + 下一条 + 已确认锚点）。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    state: TimelineState
    display_date: date
    display_timezone: str
    plan_day: date
    actor_timezone: str
    items: list[TodayOutlineItem] = Field(default_factory=list)
    note: str | None = None


class IntentTeaserItem(StrictModel):
    """仪表盘上的一条未来意向摘要（只给有来源的）。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    intention_id: IdStr
    title: str
    target_start_day: date
    target_end_day: date
    state: str
    source_kind: str


class IntentTeaserOut(StrictModel):
    """`GET /v1/dashboard` 的 `sections.intent_teaser.data`。

    ``horizon_unplanned=true`` 是给人看的那句话的依据：未来日**没有**生成大纲，
    这里的条目是意向，不是那一天的安排。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    horizon_unplanned: bool = True
    items: list[IntentTeaserItem] = Field(default_factory=list)
    note: str | None = None


# ---------------------------------------------------------------- 上游回退标记


class FallbackState(StrEnum):
    """回退标记里"上游当时是什么状态"。

    与 :class:`TimelineState` 刻意分开、取值不完全相同：这里还多一个
    ``unknown``（状态不明），并且**只有** `ready` 算"能用上游的值"。
    把两者合成一个枚举会让"我们不知道"被迫冒充成某一个具体状态。
    """

    ready = "ready"
    degraded = "degraded"
    empty = "empty"
    pending = "pending"
    failed = "failed"
    unavailable = "unavailable"
    unknown = "unknown"


class FallbackSource(StrEnum):
    """这个值是上游给的，还是本领域自己的默认参数。**只有这两种**。"""

    upstream = "upstream"
    domain_default = "domain_default"


class FallbackNotice(StrictModel):
    """一个消费点上"这次的值从哪来"的紧凑标记。

    **它不是事实**，也不能被读成事实：

    - ``used=true`` 只说明"上游这次没给出可用的值，我们用了自己的默认参数"，
      不代表上游坏了、更不代表她做了什么；
    - ``reason`` 是给人看的一句人话（界面直接显示），不是错误码表；
    - 值本身（默认值）**绝不**写进 activities / experiences / 位置等事实表。

    没有回退时也要给这个标记（``used=false``、``source=upstream``）：
    "这次没用默认值"本身就是要回答的问题，缺字段会让前端分不清
    "没用回退"和"后端没实现"。
    """

    upstream: str = Field(description="上游是谁，如 schedule / scene")
    used: bool = Field(description="这次是否用了领域默认参数")
    state: FallbackState = Field(description="上游当时的状态")
    source: FallbackSource = Field(description="值的来源")
    reason: str | None = Field(
        default=None, description="用默认值的人话原因；没回退时为 null"
    )


# ---------------------------------------------------------------- 提示词快照（只读）


class PromptSnapshotState(StrEnum):
    """「查看本次提示词」的三种状态。**必须互相区分**。

    - ``available``：这一轮的快照还在保留期内，`sections` 是真正发出去的内容；
    - ``expired``：**曾经有过**快照，但已过保留期或正文已被清理任务清空。
      元数据（`created_at` / `expires_at` / `model` / `token_total`）仍在，
      `sections` 为空——内容确实没了，不是没存过；
    - ``unavailable``：**从来没存下来**（捕获失败，或这条消息早于本功能）。

    三者混成一种的话，用户看到"打不开"时分不清该重试、该关掉功能、还是
    本来就该没有——而"沉默/失败都要能被解释"是这个项目的基本要求。
    """

    available = "available"
    expired = "expired"
    unavailable = "unavailable"


class PromptBlock(StrEnum):
    """快照里一段内容的语义块。由**编译器在装配时标注**，不是事后猜的。

    ``key`` 是闭集：新车道没写进映射表时落到 ``other``，不会静默算进别的组。
    """

    system = "system"
    persona = "persona"
    memory = "memory"
    plan = "plan"
    media = "media"
    summary = "summary"
    history = "history"
    input = "input"
    other = "other"


class PromptSnapshotModel(StrictModel):
    """这一轮**实际解析到**的模型（与 `resolve_profile` 同一份结论）。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    provider_id: str | None = None
    model_id: str | None = None
    profile: str | None = None
    config_revision: int | None = None
    # 解析来源：explicit / slot / legacy_fallback / unavailable。
    # 同一份 provider/model 可能来自"单独指定"或"跟随槽位"，处置完全不同。
    source: str | None = None


class PromptSnapshotItem(StrictModel):
    """快照里的一条消息。``index`` 是它在**真实 messages 数组**里的下标。

    截断后下标会跳号——这是**故意的**：跳号说明中间被丢掉了，
    而不是"她当时只看到这些"。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    index: int = Field(ge=0)
    role: str
    content: str
    tokens: int = Field(default=0, ge=0)
    # 历史条目专有：谁说的、什么时候（群里最需要这两项）
    sender: str | None = None
    at: datetime | None = None
    # 这条正文因快照超上限被截过（与整份快照的 `truncated` 是两件事）
    truncated: bool = False
    # 原始车道名（persona / evidence / media …），便于排查"这块是谁注入的"
    lane: str | None = None


class PromptSnapshotSection(StrictModel):
    """一个语义块的展示分组。``label`` 直接给前端显示，**不要在前端再翻译**。"""

    schema_version: Literal["v1"] = SCHEMA_VERSION
    key: PromptBlock
    label: str
    # 组内所有条目同角色时就是那个角色；混合（如历史里 user/assistant）时
    # 为 ``mixed``——不要自己挑一个代表角色，那会让读者误判这一段的结构。
    role: str
    tokens: int = Field(default=0, ge=0)
    # 默认折叠（目前只有 history）：内容长、但通常不需要一眼看完。
    collapsed: bool = False
    items: list[PromptSnapshotItem] = Field(default_factory=list)


class PromptSnapshotOut(StrictModel):
    """`GET /v1/messages/{message_id}/prompt` 的响应。

    ``token_total`` 是**这一轮实际发出去**的估算总量；截断时它仍包含没存进
    `sections` 的部分，所以**不必等于** `sum(section.tokens)`。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    state: PromptSnapshotState
    message_id: IdStr
    # 这条消息属于哪一轮。没有（旧数据）时为 null。
    turn_id: IdStr | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    model: PromptSnapshotModel | None = None
    token_total: int = Field(
        default=0,
        ge=0,
        description="这一轮实际发出去的 token 估算总量（含因超限未存进 sections 的部分）",
    )
    truncated: bool = False
    dropped_items: int = Field(default=0, ge=0)
    sections: list[PromptSnapshotSection] = Field(default_factory=list)
    # 给人看的一句状态说明（expired / unavailable 时解释原因，纯文本）
    note: str | None = None


# ---------------------------------------------------------------- 关系（分页，2026-11）


class SubjectAccountOut(StrictModel):
    """`GET /v1/accounts/subjects` 的 `subjects[].accounts[]`：一个渠道账号绑定。

    ``revoked`` 是**派生字段**（``revoked_at`` 非空），只读接口不回吐时间戳：
    页面要说的是"这个号还能不能用"，不是"哪一刻解绑的"。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    account_id: IdStr
    connector_instance: str
    external_account_id: str
    proof: str
    revoked: bool = False


class SubjectRow(StrictModel):
    """一个人：昵称、亲疏等级、被合并到谁，以及他/她绑定的账号。

    ``merged_into`` 非空表示"已被当作另一个人"。合并**只写这一个指针**，
    账号与记忆都留在原处（所以拆开只是清指针）。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    subject_id: IdStr
    display_name: str
    trust_level: str
    merged_into: IdStr | None = None
    accounts: list[SubjectAccountOut] = Field(default_factory=list)


class SubjectsPage(StrictModel):
    """`GET /v1/accounts/subjects` 的响应（**分页**，docs/API-WEB-V1.md §13）。

    分页是契约的一部分，不是可选优化：``subjects`` 默认只有 50 条，
    调用方必须看 ``has_more`` / ``total``，不能假设"一次就是全部"。
    ``total`` 是**筛选后**的总行数（``q`` 生效时它不是全表行数）。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    subjects: list[SubjectRow] = Field(default_factory=list)
    # 服务端实际生效的分页参数（回显）：客户端不必猜默认值。
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    has_more: bool = False
    # 服务端实际生效的筛选词（已 strip）；没筛选时为 null。
    q: str | None = None


# ---------------------------------------------------------------- 镜像更新（四态）


class ImageUpdateState(StrEnum):
    """「检查更新」卡片的结论只有四类，优先级由服务端算（前端不自己比较版本）。

    判定顺序（docs/UPDATE-UX-DESIGN.md）：``unknown`` → ``newer_unavailable``
    → ``available`` → ``latest``。活动安装任务**不**新增第五种状态：``state``
    仍按发现结果给，进度由 ``task`` 与 ``message`` 表达。
    """

    latest = "latest"
    available = "available"
    newer_unavailable = "newer_unavailable"
    unknown = "unknown"


# 下面这些是卡片里的**片段**对象（不是一个能单独获取的资源），所以不重复带
# ``schema_version``：版本号只在最外层 ``ImageUpdateView`` 出现一次，和
# docs/UPDATE-UX-DESIGN.md 给出的响应形状逐字段一致。


class ImageUpdateCurrent(StrictModel):
    """当前运行版本/提交/镜像摘要；读不到就是 null（不拿 pyproject 猜）。"""

    version: str | None = None
    commit: str | None = None
    digest: str | None = None


class ImageNewerRelease(CompatModel):
    """本次检查发现的更高版本（``compatible=false`` 的也保留）。

    事实源是更新器的 ``release_check.latest``（查 GHCR tags/manifest 得出），
    所以列表通常只有一条。与"可提交目标"的**关键差别**：``digest`` 允许为 null。
    「有新版但拿不到安装包」是必须能显示给用户的事实（否则页面只剩一句
    "暂无可更新的版本"，用户分不清是没发版还是没构建产物）；摘要为空的条目
    永远进不了 ``target``，不能用它构造安装请求。
    """

    version: str
    # registry 的 tag 元数据里通常没有目标版本的构建提交（更新器写 null），
    # 所以这里可空；它只用于展示/核对，不参与"能不能装"。
    commit: str | None = None
    has_migration: bool | None = Field(
        default=None,
        deprecated="旧字段：发布目录已删除，迁移是否需要由迁移器判断；一个版本后移除",
    )
    # null = 更新器还没解析出这一版的构建产物摘要（registry 无 tag /
    # manifest 取不到 / 没有本机平台都归到这里）。
    digest: str | None = None
    compatible: bool
    # 固定码（``digest_unresolved`` / ``platform_unsupported`` …）；只进详情。
    incompatible_reason: str | None = None
    incompatible_reason_message: str | None = None
    # 发布方原话：``summary`` 一行、``notes`` 逐条（≤8 条）；缺失就是 null / []。
    summary: str | None = None
    notes: list[str] = Field(default_factory=list)
    notes_url: str | None = None


class ImageUpdateTarget(StrictModel):
    """唯一可提交的目标：只能来自「兼容 + 摘要已解析」的发现条目。"""

    version: str
    digest: str
    # 目标版本的构建提交通常拿不到（registry tag 元数据里没有），如实给 null。
    commit: str | None = None
    has_migration: bool | None = Field(
        default=None,
        deprecated="旧字段：发布目录已删除，迁移是否需要由迁移器判断；一个版本后移除",
    )


class ImageUpdateTaskRow(CompatModel):
    """最近一次安装任务（只给卡片与详情用，**不参与**发现结论）。

    旧任务的全部字段（``request_id``/``target``/``migration``/``backup``/
    ``failure``/``warnings``/``next_actions`` …）仍按白名单原样透传一个版本。
    """

    state: str
    stage: str | None = None
    # 任务目标版本：只有发现条目的摘要对得上（或目标就是当前运行摘要）时才给，
    # 否则 null —— 前端显示「目标版本未记录」。
    version: str | None = None
    # 失败码（白名单内）；成功/未失败为 null。
    failed_code: str | None = None


class ImageUpdateCatalogDetails(CompatModel):
    """**旧形状**：「目录」诊断，保留一个版本给已在读它的前端。

    可信发布目录（``deploy/releases.json``）已经删除，这里的值全部从
    ``release_check`` **派生**：``state`` 就是 ``release_check.state``（``ok`` /
    ``failed`` / ``unknown``），``entries`` 映射 ``tag_count``，``source`` 是
    ``"registry"``。新代码请读 ``details.release_check``。
    """

    state: str | None = None
    state_message: str | None = None
    # 固定码（``registry_unreachable`` / ``digest_unresolved`` …），只进详情，
    # 不进首屏那句话。
    reason: str | None = None
    reason_message: str | None = None
    # "registry" = 这次检查真的问了镜像仓库；null = 没有来源证据
    # （这时不能声称「已是最新」）。
    source: str | None = None
    checked_at: str | None = None
    entries: int | None = None
    current_version: str | None = None
    # 本次/上次**按需检查**的终态：
    # ``check_id`` 与 POST /v1/version/image-update-checks 返回的 ``request_id``
    # 是同一个值（更新器回填进 ``release_check.check_id``），前端据此确认这次
    # 检查真的完成了。
    check: dict[str, Any] | None = None


class ImageReleaseCheckCurrent(CompatModel):
    """更新器实测的**当前运行版本**（读运行中 api 镜像的 OCI ``version`` 标注）。"""

    version: str | None = None
    # registry 侧的裸摘要（``sha256:<64hex>``）；API 对外统一成完整引用。
    digest: str | None = None


class ImageReleaseCheckLatest(CompatModel):
    """本次检查发现的最高稳定版（registry ``tags/list`` + manifest 解析结果）。"""

    version: str | None = None
    digest: str | None = None
    # registry tag 元数据里通常没有这个版本对应的构建提交，允许 null。
    commit: str | None = None


class ImageReleaseCheckDetails(CompatModel):
    """更新器 ``status.json`` 顶层 ``release_check`` 的白名单投影。

    这是「有没有新版本」的**唯一**事实源（设计
    docs/UPDATE-SIMPLIFICATION-DESIGN.md §3）。``reason_detail`` 只投影固定枚举
    ``null | "throttled"``——更新器那个字段只有这两个值，别的值（自由文本，可能带
    registry URL、凭据片段或原始异常）一律丢弃。
    """

    # ok = 这次完整查成功；failed = 这次失败；unknown = 还没查/上次被打断。
    state: str | None = None
    checked_at: str | None = None
    # 本次检查 ID：与 POST /v1/version/image-update-checks 的 ``request_id`` 同值。
    check_id: str | None = None
    # "registry"（固定）；别的值等于没有来源证据。
    source: str | None = None
    # **归一后**的固定码（``registry_unreachable`` / ``registry_auth_failed`` /
    # ``registry_rate_limited`` / ``registry_timeout`` / ``no_stable_tags`` /
    # ``digest_unresolved`` / ``platform_unsupported``）；只进详情。
    #
    # 更新器写的是另一套拼写（``auth_failed`` / ``rate_limited`` / ``timeout`` /
    # ``manifest_failed`` / ``digest_missing`` / ``platform_missing``），API 侧按别名
    # 表收敛后**才**回显，所以这里永远只出现上面这几个码（前端码表按这个查）。
    reason: str | None = None
    reason_message: str | None = None
    # ``null`` 或 ``"throttled"``（同一 check_id 的刷新请求在限流窗口内被丢弃）。
    reason_detail: str | None = None
    current: ImageReleaseCheckCurrent = Field(default_factory=ImageReleaseCheckCurrent)
    latest: ImageReleaseCheckLatest | None = None
    tag_count: int = Field(default=0, ge=0)


class ImageUpdateUpdaterDetails(CompatModel):
    """更新器诊断：在线、状态、协议、版本。"""

    online: bool | None = None
    state: str | None = None
    protocol: int | None = None
    version: str | None = None


class ImageUpdateBackupGateDetails(CompatModel):
    """恢复点诊断：是否真有可用备份、原因与检查时间。"""

    restore_point: bool | None = None
    complete: bool | None = None
    reason: str | None = None
    reason_message: str | None = None
    checked_at: str | None = None


class ImageUpdateDetails(StrictModel):
    """「详情/诊断」折叠区；``backup_gate`` 没有任何事实时为 null。"""

    # 旧形状（一个版本）：从 release_check 派生，前端仍在读 `catalog.check`。
    catalog: ImageUpdateCatalogDetails
    updater: ImageUpdateUpdaterDetails
    backup_gate: ImageUpdateBackupGateDetails | None = None
    # 新字段：更新器 release_check 的白名单原始投影（唯一发现事实源）。
    release_check: ImageReleaseCheckDetails | None = None


class ImageUpdateView(CompatModel):
    """``GET /v1/version/image-update``：四态卡片 + 诊断。

    ``message`` 是服务端算好的一句话人话（不含原因码/摘要/路径），前端直接显示。
    活动任务进行中时它讲进度（「更新正在进行中…」），``state`` 仍是发现结论。

    下半部分是**旧字段**：保留一个版本给尚未迁移的调用方，新代码不要读。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    state: ImageUpdateState
    message: str
    checked_at: str | None = None
    current: ImageUpdateCurrent
    newer: list[ImageNewerRelease] = Field(default_factory=list)
    target: ImageUpdateTarget | None = None
    task: ImageUpdateTaskRow | None = None
    details: ImageUpdateDetails

    # ---- 旧字段（一个版本后移除）----
    image_mode: bool = Field(deprecated="旧字段：用 `state` 判断卡片的四态")
    enabled: bool = Field(deprecated="旧字段：安装开关不再是结论的一部分")
    installable: bool = Field(deprecated="旧字段：owner 视角下等价于 `state == available`")
    reason: str | None = Field(deprecated="旧字段：安装门禁的码；新版不进首屏")
    reason_message: str | None = Field(
        deprecated="旧字段：安装门禁码表文案；新版一句话正文在 `message`"
    )
    next_actions: list[str] = Field(
        default_factory=list, deprecated="旧字段：新版把下一步写进 `message`"
    )
    updater: dict[str, Any] = Field(deprecated="旧字段：见 `details.updater`")
    backup_gate: dict[str, Any] = Field(deprecated="旧字段：见 `details.backup_gate`")
    catalog: dict[str, Any] = Field(deprecated="旧字段：见 `details.catalog`")
    available_targets: list[dict[str, Any]] = Field(
        default_factory=list,
        deprecated="旧字段：安装提交仍按这里校验；展示请用 `newer`",
    )
    request: dict[str, Any] | None = Field(
        default=None, deprecated="旧字段：排队中的安装请求（详情区）"
    )


class ImageUpdateCheckRequest(StrictModel):
    """``POST /v1/version/image-update-checks`` 的固定请求体。

    多一个字段、少一个字段、ID 不是 32 位小写十六进制都会被拒绝（422）。
    """

    request_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")


class ImageUpdateCheckAccepted(StrictModel):
    """检查请求回执（202 已排队 / 200 幂等去重）。

    **只表示"已写入"**，不表示已经检查完：结果要看随后 GET 的 ``checked_at``
    与 ``details.catalog.check``。200 时返回的是**已经在排队/刚刚完成**的那个
    ``request_id``，前端要按返回的 ID 核对（可能与请求里的不同）。
    """

    schema_version: Literal["v1"] = SCHEMA_VERSION
    request_id: str
    accepted: bool


# ---------------------------------------------------------------- 错误


class ErrorBody(StrictModel):
    """错误详情。版本号只在最外层 ``ErrorResponse`` 出现一次。"""

    code: ErrorCode
    message: str
    retriable: bool = False
    detail: str | None = None
    trace_id: IdStr | None = None


class ErrorResponse(StrictModel):
    schema_version: Literal["v1"] = SCHEMA_VERSION
    error: ErrorBody


__all__ = [
    "SCHEMA_VERSION",
    "ActionIntent",
    "AudienceRule",
    "CapabilityDescriptor",
    "ChatMessage",
    "ContextContribution",
    "DayIntentSummary",
    "DeliveryReceipt",
    "DeliveryState",
    "DreamCard",
    "ErrorBody",
    "ErrorCode",
    "ErrorResponse",
    "FallbackNotice",
    "FallbackSource",
    "FallbackState",
    "CompatModel",
    "HandoffCapsule",
    "ImageNewerRelease",
    "ImageReleaseCheckCurrent",
    "ImageReleaseCheckDetails",
    "ImageReleaseCheckLatest",
    "ImageUpdateBackupGateDetails",
    "ImageUpdateCatalogDetails",
    "ImageUpdateCheckAccepted",
    "ImageUpdateCheckRequest",
    "ImageUpdateCurrent",
    "ImageUpdateDetails",
    "ImageUpdateState",
    "ImageUpdateTarget",
    "ImageUpdateTaskRow",
    "ImageUpdateUpdaterDetails",
    "ImageUpdateView",
    "InputPart",
    "IntentTeaserItem",
    "IntentTeaserOut",
    "InteractionEnvelope",
    "LifeTimelineFact",
    "LifeTimelineOut",
    "LifeTimelineSegment",
    "MemoryProposal",
    "Modality",
    "ModelRole",
    "OperationResult",
    "OperationState",
    "Origin",
    "OutlineRelation",
    "PromptBlock",
    "PromptSnapshotItem",
    "PromptSnapshotModel",
    "PromptSnapshotOut",
    "PromptSnapshotSection",
    "PromptSnapshotState",
    "RuntimeScope",
    "SessionRecord",
    "SubjectAccountOut",
    "SubjectRow",
    "SubjectsPage",
    "TimelineState",
    "TodayOutlineItem",
    "TodayOutlineOut",
    "TurnState",
    "Visibility",
    "new_id",
]
