"""ling 对外契约 v1（冻结）。

只放 设计文档 §10 冻结的少数公开 DTO 与运行期作用域。所有对象带 ``schema_version``；
破坏性变化升 major；未知必需字段失败关闭（``extra="forbid"``）。

本模块不得 import 任何领域实现，保证契约与实现解耦。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION: Literal["v1"] = "v1"

IdStr = Annotated[str, Field(min_length=1, max_length=128)]


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
    internal = "internal"


class ModelRole(StrEnum):
    """按职责选择模型的 profile 键（设计文档 §8.1）。"""

    dialogue_fast = "dialogue.fast"
    dialogue_deep = "dialogue.deep"
    dialogue_group = "dialogue.group"
    perception_vision = "perception.vision"
    speech_asr = "speech.asr"
    speech_tts = "speech.tts"
    realtime_voice = "realtime.voice"
    memory_extract = "memory.extract"
    memory_embed = "memory.embed"
    memory_rerank = "memory.rerank"
    reflection = "reflection"
    planning = "planning"
    moderation_advisory = "moderation.advisory"


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
    media_ref: IdStr | None = None
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
    media_refs: list[IdStr] = Field(default_factory=list)
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
    "DeliveryReceipt",
    "DeliveryState",
    "ErrorBody",
    "ErrorCode",
    "ErrorResponse",
    "HandoffCapsule",
    "InputPart",
    "InteractionEnvelope",
    "MemoryProposal",
    "Modality",
    "ModelRole",
    "OperationResult",
    "OperationState",
    "Origin",
    "RuntimeScope",
    "SessionRecord",
    "TurnState",
    "Visibility",
    "new_id",
]
