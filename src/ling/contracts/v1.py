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
from typing import Annotated, Literal
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
    "HandoffCapsule",
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
    "RuntimeScope",
    "SessionRecord",
    "TimelineState",
    "TodayOutlineItem",
    "TodayOutlineOut",
    "TurnState",
    "Visibility",
    "new_id",
]
