"""权威数据库 schema（W0 基线 + 后续批次增量）。

设计原则（设计文档 §3）：
- 每个领域只写自己的表；本文件只定义结构，不写业务规则。
- 事件、投影、身份、任务、outbox、回执、审计同库同迁移。
- 主键用带语义前缀的字符串 ID，便于日志阅读；时间一律 UTC aware。

表分组：
  identity  : users / auth_sessions / subjects / actor_accounts / actor_events
  persona   : personas
  converse  : windows / turns / messages / handoff_capsules / window_contexts
  settings  : config_revisions / provider_credentials
  ops       : operations / delivery_receipts / idempotency_keys / outbox
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ling.infra.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ------------------------------------------------------------------ identity


class User(Base, TimestampMixin):
    """管理/登录账号。W0 的一个 user 对应一个已证明的 subject。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="owner")  # owner | member
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # 首次安装生成的是随机口令，只打印在启动日志里。置 True 时除 /v1/auth/*
    # 之外的接口一律拒绝，逼用户先改成自己记得的密码——否则那个随机串
    # 会永远留在日志和 .env 里，成为长期后门。
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0"
    )
    subject_id: Mapped[str] = mapped_column(String(64), index=True)


class AuthSession(Base):
    """浏览器登录会话。只存 token 哈希，原文只在 cookie 中。"""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)


class Subject(Base, TimestampMixin):
    """经证明的自然人。渠道账号/设备都绑定到这里，但不等于这里。"""

    __tablename__ = "subjects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), default="用户")
    proof_level: Mapped[str] = mapped_column(String(32), default="password")  # password|device|admin
    # 亲疏等级：限制的是**记忆范围**，不是权限。
    #   owner    主人，全部私聊记忆，可触发跨窗召回
    #   known    聊过但未验证身份，记忆只限本群相关
    #   stranger 第一次见，不写记忆
    # 跨窗事实（Awareness）对非 owner 一律不注入。
    trust_level: Mapped[str] = mapped_column(String(32), default="known")
    merged_into: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ActorAccount(Base, TimestampMixin):
    """渠道外部账号 → subject 的绑定（QQ/微信/设备）。绑定需证明，可撤销。"""

    __tablename__ = "actor_accounts"
    __table_args__ = (
        UniqueConstraint("connector_instance", "external_account_id", name="uq_account_external"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    connector_instance: Mapped[str] = mapped_column(String(64), index=True)
    external_account_id: Mapped[str] = mapped_column(String(128))
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    proof: Mapped[str] = mapped_column(String(32), default="pending")  # pending|proven|admin
    consent_revision: Mapped[int] = mapped_column(Integer, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Persona(Base, TimestampMixin):
    """角色设定与人格版本。切换人格不自动继承他人私聊。"""

    __tablename__ = "personas"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(64), default="ling")
    display_name: Mapped[str] = mapped_column(String(64), default="ling")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    traits: Mapped[dict] = mapped_column(JSON, default=dict)
    boundaries: Mapped[list] = mapped_column(JSON, default=list)
    published: Mapped[bool] = mapped_column(Boolean, default=True)


class ActorEvent(Base):
    """权威角色事件时间线（ActorSequencer 唯一写入）。"""

    __tablename__ = "actor_events"
    __table_args__ = (Index("ix_actor_events_actor_time", "actor_id", "occurred_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    persona_id: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    window_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    scene_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    origin: Mapped[str] = mapped_column(String(32))
    visibility: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


# ------------------------------------------------------------------ conversation


class Window(Base, TimestampMixin):
    """会话窗口：一个 subject 与角色的一段连续对话。"""

    __tablename__ = "windows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    persona_id: Mapped[str] = mapped_column(String(64))
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(128), default="新的对话")
    channel: Mapped[str] = mapped_column(String(32), default="web")
    visibility: Mapped[str] = mapped_column(String(32), default="subject_private")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Turn(Base):
    """一轮用户输入 → 角色回复。状态由服务端裁决，浏览器不参与。"""

    __tablename__ = "turns"
    __table_args__ = (UniqueConstraint("window_id", "client_turn_id", name="uq_turn_client"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_id: Mapped[str] = mapped_column(
        ForeignKey("windows.id", ondelete="CASCADE"), index=True
    )
    client_turn_id: Mapped[str] = mapped_column(String(128))
    state: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    model_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    config_revision: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Message(Base):
    """窗口内的可见消息。sequence 单调递增，用于稳定排序。"""

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_window_seq", "window_id", "sequence"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_id: Mapped[str] = mapped_column(
        ForeignKey("windows.id", ondelete="CASCADE"), index=True
    )
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | actor | system
    # 这条消息是谁说的（subject）。**群聊里必须记**：
    # 一个群窗口是多个人的，消息本身不带发言人，事后就再也分不清
    # 「刚才那句话是谁说的」——既没法回指（"你刚问的那个"），
    # 也没法按人用关系/熟悉度。actor 自己说的话留空。
    sender_subject_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    # 同一 turn 的第几次尝试：重试不删除旧文本，靠 attempt 区分"上一段未完成"
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(32), default="completed")
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    origin: Mapped[str] = mapped_column(String(32), default="user_statement")
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    model_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 她**看见之后**怎么处置这条消息。和"回了什么"是两件事：
    # `ignored` 表示"看到了，选择不回"——消息仍在库里，仍然算她见过。
    # 没有这个字段时，"她选择不回"和"她根本没收到"在外部完全一样。
    #
    # 取值目前只有 replied / ignored。noted / deferred / acted 需要意图层
    # 真的有对应的落点，不能先编出来。
    # 她**看没看见**这条消息。与 handled_as 是两件事：
    #   attention_state=unseen    还没看到（消息在库里，但没进过她的决策）
    #   attention_state=seen      看过了（进过决策上下文）
    #   attention_state=missed    **错过了**：一直没机会看，已过时效
    #
    # 第三个状态是必须的。只有 seen/unseen 时，"她忙了一下午所以没回"
    # 和"她看到了但选择不回"在数据上完全一样——而这正是用户反复强调要
    # 能分开的两件事。
    # 混用会让"没看见"和"看见但没回"再次无法区分——正是这个项目反复要防的。
    attention_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    handled_as: Mapped[str | None] = mapped_column(String(24), nullable=True)
    handled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WindowContext(Base):
    """窗口短记忆：话题、引用、未答问题。闲置过期，不进入长期记忆。"""

    __tablename__ = "window_contexts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_id: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_questions: Mapped[list] = mapped_column(JSON, default=list)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- 分层上下文（设计文档 §6.2：短时窗口上下文 + 滚动摘要）---
    # 已滚出原文窗口的早期对话摘要。由 Conversation 域维护，不是长期记忆。
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 摘要覆盖到哪条消息 sequence（含）。用于增量摘要与失效判断。
    summary_through_sequence: Mapped[int] = mapped_column(Integer, default=0)
    # 摘要生成时依据的消息条数，便于解释"摘要了什么"
    summary_message_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class HandoffCapsuleRow(Base):
    """同人跨端交接胶囊。默认 30 分钟，仅本人已绑定私聊端。"""

    __tablename__ = "handoff_capsules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    source_window_id: Mapped[str] = mapped_column(String(64))
    recent_topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    open_questions: Mapped[list] = mapped_column(JSON, default=list)
    short_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(String(32), default="subject_private")
    consent_revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# ------------------------------------------------------------------ settings / models


class ConfigRevision(Base):
    """唯一权威配置修订：draft → validated → published → rolled_back。"""

    __tablename__ = "config_revisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, index=True)
    state: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    validation_errors: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProviderCredential(Base):
    """供应商凭据：密文 + 后四位 + 指纹。原文永不回传浏览器/日志。"""

    __tablename__ = "provider_credentials"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    ciphertext: Mapped[str] = mapped_column(Text)
    last4: Mapped[str] = mapped_column(String(8), default="")
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ------------------------------------------------------------------ ops


class Operation(Base, TimestampMixin):
    """受控外部动作。计划不是结果，只有真实回执才能标完成。"""

    __tablename__ = "operations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="proposed", index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)


class DeliveryReceiptRow(Base):
    __tablename__ = "delivery_receipts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32))
    platform_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyKey(Base):
    """入站事件去重：connector_instance + external_event_id 唯一。"""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(256))
    result_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboxEvent(Base):
    """同库事务 outbox：worker 至少一次处理，消费者按 id 去重。"""

    __tablename__ = "outbox_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 哪个实例/进程处理的（多副本时诊断"谁做了这件事"）
    processed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ActorState(Base):
    """角色权威版本栅栏。提交前检查 revision，避免旧草稿覆盖新状态。"""

    __tablename__ = "actor_states"

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    persona_id: Mapped[str] = mapped_column(String(64))
    last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PresenceState(Base, TimestampMixin):
    """角色此刻的"生活状态"（精力、社交负担）。

    **不是人格**，也不是生理指标——它是模拟出来的状态，只用于决定
    "这次回复多长、要不要主动、要不要追问"，绝不作为拒绝用户的理由。

    单独成表而不是塞进 ``ActorState``：后者是版本栅栏（每次事件递增），
    这里是被时间与事件共同推进的投影，两者变化频率差一个数量级
    （栅栏每条事件都变，精力按小时衰减）。
    """

    __tablename__ = "presence_states"

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 0-100。低精力只影响表达长度与主动意愿，**不影响是否回答**
    energy: Mapped[int] = mapped_column(Integer, default=70)
    # 0-100。高社交负担时少追问，同样不影响回答
    social_load: Mapped[int] = mapped_column(Integer, default=20)
    # 上次结算时间：按经过时间衰减，避免"每次读都扣一次"
    settled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    revision: Mapped[int] = mapped_column(Integer, default=0)


class ActorRoutineRow(Base):
    """角色的软作息：几点醒、几点歇。

    **软**的意思是：它是活动可以发生的宽窗口，不是"到点必须执行"。
    用户来聊天可以打断，被打断的活动可以恢复。

    与 ``CommitmentRow`` 明确分开：那是**对用户的硬承诺**（"周五提醒我"），
    这里是角色自己的节奏。两者混在一起会导致"角色没按作息起床"
    和"答应了用户却没做到"被当成同一类失信。
    """

    __tablename__ = "actor_routines"

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # IANA 时区。角色自己的晨晚边界按它算，绝不从服务器时区推断
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    # 0-1439。允许 wake > rest 表达跨午夜（例如凌晨 2 点睡、10 点起）
    wake_minute: Mapped[int] = mapped_column(Integer, default=8 * 60)
    rest_minute: Mapped[int] = mapped_column(Integer, default=23 * 60)
    # configured（用户/人格设定） / default
    source: Mapped[str] = mapped_column(String(16), default="default")
    revision: Mapped[int] = mapped_column(Integer, default=0)


class AgendaPlanRow(Base, TimestampMixin):
    """角色自己的一件小事（"上午读会儿书"）。

    硬规则：
    - ``commitment_level`` 区分 tentative / confirmed。
      tentative 只能在相关话题出现，不能说成"约好了"
    - 计划**永远不能**被写成已完成 —— 完成必须来自 ``ActivityRow``
      且有完成证据
    """

    __tablename__ = "agenda_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(120))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    # tentative（可能）/ confirmed（已确认）
    commitment_level: Mapped[str] = mapped_column(String(16), default="tentative")
    # planned / active / done / cancelled / skipped
    state: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    place_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class SceneCommitRow(Base):
    """角色的"此刻在做什么"——短时、可中断、有有效期。

    设计要点（参照 `runtime_scene_resolver` 的 commit/valid_until 思路，
    但**不照抄它的进程内字典**）：
    - 带 ``valid_until``：过期就不能当现状说，避免"我还在读书"说了三天
    - 带 ``origin``：区分模拟生活与真实回执，模拟的不能冒充现实
    - ``source_event_ids`` 只存事件 id，不存原文，读时再经受众裁决
    """

    __tablename__ = "scene_commits"

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # chat / focus / rest / errand / unknown
    state: Mapped[str] = mapped_column(String(16), default="unknown")
    activity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    place_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # simulated（模拟生活） / tool_receipt（有真实回执） / user_stated
    origin: Mapped[str] = mapped_column(String(24), default="simulated")
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    committed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    # 过了这个时刻就不能再作为现状自述
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OpsAlertState(Base):
    """告警去重状态：同一告警在间隔内只推一次。

    不做去重的话，worker 挂一小时会推几百条，很快让人开始忽略通知——
    真正需要处理的那条就淹在噪声里。状态恢复时删行，下次能立刻再推。
    """

    __tablename__ = "ops_alert_state"

    fingerprint: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    count: Mapped[int] = mapped_column(Integer, default=0)


class EntityRow(Base, TimestampMixin):
    """实体：人、地点、项目、宠物等可被反复提到的对象。

    为什么需要：现在记忆只记录"用户有什么属性"，没有实体概念。
    用户问"我妈提过的那个人后来怎样"时，只能指望文本碰巧命中，
    而不是沿实体关系定位。

    ``aliases`` 存同一实体的不同叫法（妈/母亲/老妈）——
    没有别名归并，"换个叫法就串不起来"仍然成立。
    """

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint("subject_id", "name", name="uq_entities_subject_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24), default="other")
    # 同一实体的其他叫法（小写归一化后比对）
    aliases: Mapped[list] = mapped_column(JSON, default=list)


class MemoryEntityRow(Base):
    """记忆 ↔ 实体 的关联（多对多）。

    ``role`` 说明这条记忆与实体是什么关系（about/with/about_family…），
    让"沿实体找相关记忆"能带语义而不只是全量关联。
    """

    __tablename__ = "memory_entities"
    __table_args__ = (
        Index("ix_memory_entities_entity", "entity_id"),
    )

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(24), default="about")


class MemoryEmbeddingRow(Base):
    """记忆的稠密向量（可选能力，未配置 embedding 时不写）。

    单独成表而不是给 ``memories`` 加列：
    - 换 embedding 模型时要整体重建，独立表可以直接 truncate 重建
    - 不是每条记忆都需要向量（Episode 与 Assertion 择需）
    - 向量体积大，不该拖慢主表扫描

    没有 pgvector 扩展，检索时在候选集上算余弦（个人规模够用；
    上万条时应改用 pgvector 或外部向量库）。
    """

    __tablename__ = "memory_embeddings"
    __table_args__ = (
        Index("ix_memory_embeddings_model", "model_id"),
    )

    # 主键必须是 (memory_id, model_id) 复合：换 embedding 模型时要能并存
    # 多套向量。只用 memory_id 作主键时，新模型写向量会撞主键冲突，
    # 且异常被吞后事务失效，后续召回报 PendingRollbackError（实测）。
    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    model_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    dim: Mapped[int] = mapped_column(Integer)
    # 向量用 JSON 存：避免依赖 pgvector；缺点是占空间、无索引加速
    vector: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class CoreMemoryRow(Base, TimestampMixin):
    """核心记忆：每轮常驻，**不依赖检索命中**。

    参照 Letta 的 core memory blocks 与「我会牢牢记住你」的"核心记忆按 Label
    管理、每轮常驻"：有些内容不能靠 RAG——检索没命中就等于角色忘了自己是谁、
    忘了对方设过的边界。这类内容必须是"永远在上下文里"的少量条目。

    与 ``memories``（长尾、走检索、会衰减）分工不同：
    - 核心记忆：数量少（建议 ≤20 条）、人工/规则写入、不衰减、每轮注入
    - 长期记忆：自动提炼、走检索、有保留期与衰减

    ``label`` 是稳定标识（称呼/边界/禁忌/约定/重要日期…），同一 subject 下同名
    label 只一条，避免"关于边界记了十条互相矛盾"。
    """

    __tablename__ = "core_memories"
    __table_args__ = (
        UniqueConstraint("subject_id", "label", name="uq_core_memories_subject_label"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    # 优先级：预算不够时先保留优先级高的（数字越小越先保留）
    priority: Mapped[int] = mapped_column(Integer, default=100)
    # 来源：manual（用户显式要求记住）/ derived（从长期记忆提升）
    origin_kind: Mapped[str] = mapped_column(String(24), default="manual")
    # 归属作用域：private 只在该 subject 的私聊；group 在群里也生效
    scope: Mapped[str] = mapped_column(String(24), default="private")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


# ------------------------------------------------------------------ 跨实例协调（多进程部署）


class TurnControlRow(Base):
    """轮次停止信号（跨实例）。

    单进程时 stop 只需内存令牌；多实例时"在实例 A 点停止、轮次跑在实例 B"
    必须能传递。这里用一张极小的表 + 定期轮询实现，不引入 Redis。
    """

    __tablename__ = "turn_controls"

    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stop_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SpeakLeaseRow(Base):
    """发言仲裁租约（跨实例）。actor_id 唯一 → 同一角色同时只在一个窗口说话。

    用 ``INSERT ... ON CONFLICT DO UPDATE ... WHERE 过期或同窗口`` 实现原子获取，
    不需要分布式锁服务。租约到期自动可被抢占，避免实例崩溃后永久锁死。
    """

    __tablename__ = "speak_leases"

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_id: Mapped[str] = mapped_column(String(64))
    turn_id: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # 排队 key 列表（window#turn），让提升顺序在实例间一致
    queue: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# ------------------------------------------------------------------ 会话隔离与跨会话意识


class SessionPolicyRow(Base, TimestampMixin):
    """每个 subject 的会话隔离模式（ADR-0004）。

    为什么放在这里而不是 config revision：这是**用户隐私选择**，不是模型配置；
    也不能放窗口上——同一人在不同窗口必须看到一致的模式。
    """

    __tablename__ = "session_policies"

    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # sealed | per_window | continuous
    mode: Mapped[str] = mapped_column(String(24), default="per_window")
    # 切换模式时的修订号，便于审计与"切换后旧派生是否失效"的判断
    revision: Mapped[int] = mapped_column(Integer, default=1)


class AwarenessItemRow(Base):
    """跨会话意识条目：派生投影，不是原文（ADR-0004 §3）。

    只存"事实与事件"，绝不存对话状态（上一句、指代、未答问题）。
    """

    __tablename__ = "awareness_items"
    __table_args__ = (Index("ix_awareness_subject_kind", "subject_id", "kind"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    # actor_activity | subject_fact | group_event
    kind: Mapped[str] = mapped_column(String(32))
    # 第三人称中性摘要，不含任何原话
    text: Mapped[str] = mapped_column(Text)
    # actor_private | subject_scoped | group_scoped | public
    audience: Mapped[str] = mapped_column(String(32), index=True)
    source_window_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_visibility: Mapped[str] = mapped_column(String(32), default="window_private")
    origin: Mapped[str] = mapped_column(String(32), default="user_statement")
    # 溯源：原事实被纠正/撤回时，派生条目必须能级联失效（此前实现的问题缺这列）
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    consent_revision: Mapped[int] = mapped_column(Integer, default=0)
    policy_revision: Mapped[int] = mapped_column(Integer, default=1)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ------------------------------------------------------------------ W2 记忆域


class MemoryRow(Base, TimestampMixin):
    """长期记忆：情节/断言/技能。必须有证据、来源、受众与保留策略。

    ``(turn_id, text)`` 唯一：同一轮提取出的同一条断言只落一次。
    worker 至少一次处理，重试不能重复长记忆。
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_subject_kind", "subject_id", "kind"),
        # 幂等键用**稳定槽位**而不是文本：
        # 文本会随提取规则/模板变化，规则升级后同一轮会重复长出记忆。
        # slot 是提取器的语义槽位（如 name / likes / lives_in），与措辞无关。
        UniqueConstraint("turn_id", "slot", name="uq_memories_turn_slot"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # episode | assertion | procedure
    text: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(32))
    visibility: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 来源窗口与来源可见范围：跨受众过滤必须能判断"这是哪个群说的"
    # 没有这两列就只能按 visibility 粗过滤，"群 A 的内容进群 B" 拦不住。
    source_window_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 产生这条记忆的**渠道账号**（如 "napcat-2:10001"）。
    #
    # 为什么不能只存 subject_id：同一自然人可以挂多个账号（QQ 大号/小号/
    # 微信），合并成同一个 subject 之后，"这条记忆是哪个号说的"就无从得知。
    # 合并可无损拆分的前提就是这一列——拆分时按它把记忆还回原来的账号。
    # 群窗口是多人的，事后按窗口反推不出来，所以必须在写入时就记下。
    source_account: Mapped[str | None] = mapped_column(
        String(160), nullable=True, index=True
    )
    # 产生这条记忆的轮次（幂等键；手工创建的记忆为 NULL）
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 提取器的语义槽位（name / likes / lives_in / ...）。
    # 与文本无关，所以改措辞或换模板不会绕过去重。
    slot: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 槽位的**值**本身（不含模板文字）。压缩级联要把同槽位的多条并成一条，
    # 靠解析 text 反推值既脆弱又容易错，所以直接存。
    value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 产生时的可见范围（subject_private / window_private / group_scope / public）
    source_visibility: Mapped[str] = mapped_column(
        String(32), default="subject_private", index=True
    )
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_revision: Mapped[int] = mapped_column(Integer, default=0)
    retracted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    retract_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes: Mapped[str | None] = mapped_column(String(64), nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=365)
    # 保留期到期后归档：不再参与检索，但**不物理删除**。
    # 理由：superseded_by 链、Episode.source_event_ids 都引用 id，
    # 物理删除会断链；备份恢复也可能让已删内容复活（DESIGN §10 墓碑检查）。
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    archive_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 最近一次被召回的时间（用于"长期不用"的判断；只在不常写的路径更新）
    last_recalled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # **实际注入**统计（区别于"被检索到"）：
    # 参照实现强调只有真正进入提示词的记忆才更新强化分——
    # 否则"检索到但被预算截掉"的记忆会被反复强化，越挤越优先，形成马太效应。
    access_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_injected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MemoryEpisode(Base, TimestampMixin):
    """情节记忆：谁、何时、何地、发生何事及证据。

    ``turn_id`` 唯一：同一轮次只允许一条情节。
    这是 worker 幂等的**数据库级**保证——重试时插入冲突即代表已写过，
    不需要 handler 自己做"先查后写"（那样仍有竞态）。
    """

    __tablename__ = "memory_episodes"
    __table_args__ = (
        UniqueConstraint("turn_id", name="uq_memory_episodes_turn"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    # 产生这条情节的轮次（幂等键）
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 来源窗口的可见范围：群里的情节不能当成私聊事实被召回
    source_visibility: Mapped[str] = mapped_column(
        String(32), default="subject_private", index=True
    )
    summary: Mapped[str] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(String(32))
    visibility: Mapped[str] = mapped_column(String(32))
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    consent_revision: Mapped[int] = mapped_column(Integer, default=0)
    # 情节也会归档（不物理删除：source_event_ids 与后续断言引用它）
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class SelfExperienceRow(Base, TimestampMixin):
    """角色**自己**的经历（"今天做了什么"）。

    为什么不复用 ``MemoryEpisode``：后者永远是
    ``(subject_id, window_id, turn_id)`` 形状——"我跟某人在某个窗口的一轮对话"。
    自我经历是 ``(actor_id, 来源活动)`` 形状，没有对话方、没有窗口、没有轮次。
    硬塞进一张表意味着每个查询都要带类型过滤、并且把三个非空列改成可空；
    出错时很难发现（一条缺 subject 的 episode 会被当成"和某人的记忆"召回）。

    证据规则：**只能由已完成且有证据的活动派生**。
    计划、打算、想象都不能写成经历——那是编造人生。
    """

    __tablename__ = "self_experiences"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_self_experiences_dedupe"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    # activity / scene / milestone
    kind: Mapped[str] = mapped_column(String(24), default="activity")
    summary: Mapped[str] = mapped_column(Text)
    # 来源事件（activity:<id> 等）。用于追溯，也用于去重
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    dedupe_key: Mapped[str] = mapped_column(String(192))
    origin: Mapped[str] = mapped_column(String(24), default="simulated")
    visibility: Mapped[str] = mapped_column(String(32), default="actor_private")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MemoryRetrievalLog(Base):
    """检索审计：管理员可解释"为什么想起/没想起"。"""

    __tablename__ = "memory_retrieval_logs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    query: Mapped[str] = mapped_column(Text)
    hit_ids: Mapped[list] = mapped_column(JSON, default=list)
    filtered_out: Mapped[int] = mapped_column(Integer, default=0)
    strategy: Mapped[str] = mapped_column(String(48), default="fulltext+structured")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ------------------------------------------------------------------ W2 关系/情绪域


class RelationshipEvent(Base):
    """人际互动事件（有归因）。关系投影由事件驱动，不靠模型自述。

    ``(turn_id, kind)`` 唯一：同一轮同一类互动只记一次，
    保证 worker 重试不会重复放大关系数值。
    """

    __tablename__ = "relationship_events"
    __table_args__ = (
        UniqueConstraint("turn_id", "kind", name="uq_relationship_events_turn_kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    # 产生这条事件的轮次（幂等键）
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(48))  # warmth|conflict|repair|boundary|shared
    weight: Mapped[float] = mapped_column(Float, default=0.1)
    origin: Mapped[str] = mapped_column(String(32))
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RelationshipState(Base, TimestampMixin):
    """关系投影：熟悉度/信任度/边界。可重算。"""

    __tablename__ = "relationship_states"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True, unique=True)
    familiarity: Mapped[float] = mapped_column(Float, default=0.0)
    trust: Mapped[float] = mapped_column(Float, default=0.0)
    boundaries: Mapped[list] = mapped_column(JSON, default=list)
    last_interaction_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AffectState(Base):
    """短时情绪：强度 + 衰减。可以影响用词，不能放宽权限。

    ``(actor, emotion, scope, window_id)`` 唯一：并发 nudge 靠它做 upsert，
    否则两个事务会同时 INSERT 同一条情绪记录。
    """

    __tablename__ = "affect_states"
    __table_args__ = (
        UniqueConstraint(
            "actor_id", "emotion", "scope", "window_id", name="uq_affect_actor_emotion_scope"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    emotion: Mapped[str] = mapped_column(String(32))  # calm|warm|tired|curious|uneasy
    intensity: Mapped[float] = mapped_column(Float, default=0.0)
    decay_per_hour: Mapped[float] = mapped_column(Float, default=0.15)
    # 作用域（ADR-0004 修正）：global = 可跨窗口的粗粒度语气趋势；
    # window = 只属于某个窗口（带具体原因，绝不跨窗口）。
    scope: Mapped[str] = mapped_column(String(16), default="global", index=True)
    # global 作用域存空串而非 NULL：PostgreSQL 中 NULL 互不相等，
    # 会让 (actor, emotion, scope, window_id) 唯一约束对 global 行失效。
    window_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 最后产生/刷新这条情绪记录的轮次。worker 幂等的依据：
    # 同一 turn 对同一 (emotion, scope, window) 只调整一次，
    # 否则重复处理会不断叠加强度（此前实现的缺陷）。
    last_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    cause: Mapped[str | None] = mapped_column(Text, nullable=True)


# ------------------------------------------------------------------ W2 世界/活动/目标


class WorldPlace(Base, TimestampMixin):
    """模拟地点。虚拟世界变量，不是现实地理位置。"""

    __tablename__ = "world_places"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    items: Mapped[list] = mapped_column(JSON, default=list)


class ActivityRow(Base, TimestampMixin):
    """活动进程状态机：proposed → running → paused/completed/failed/cancelled。"""

    __tablename__ = "activities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    state: Mapped[str] = mapped_column(String(24), default="proposed", index=True)
    place_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 来源计划（可空：临时起意没有计划）
    plan_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # simulated（模拟生活）/ tool_receipt（真实回执）/ user_stated
    origin: Mapped[str] = mapped_column(String(24), default="simulated")
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 谁能知道。私聊促成的活动默认不进群聊
    visibility: Mapped[str] = mapped_column(String(32), default="subject_private")


class GoalRow(Base, TimestampMixin):
    """角色目标（自己的长期项目/短期打算）。"""

    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(200))
    horizon: Mapped[str] = mapped_column(String(24), default="short")  # short|long
    state: Mapped[str] = mapped_column(String(24), default="active")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class CommitmentRow(Base, TimestampMixin):
    """对人的承诺（提醒/约定）。到期必须有真实投递状态。"""

    __tablename__ = "commitments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    window_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    state: Mapped[str] = mapped_column(String(24), default="active", index=True)
    delivery_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 取消原因（用户撤回承诺时记录；撤回本身也是需要可解释的事实）
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ProactiveOpportunity(Base):
    """主动机会：有时效、需过勿扰/限额/可达性硬门。"""

    __tablename__ = "proactive_opportunities"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    reason: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(24), default="candidate", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # 实际送达时间。每日限额按它计日，不按 created_at——
    # 23:50 创建、00:10 发送的机会不该占掉前一天的额度。
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 生成好的开场白。**NULL 表示还没生成**，不是"生成了空字符串"。
    #
    # `reason` 不能直接当消息发：那是内部推理（"问对方手机上聊天指什么项目"），
    # 发出去很怪。必须过一次生成，并把结果留在这里让人能先看到。
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- 阶段 4：因果来源与结构化原因 ---
    # 没有来源的机会不是"关心"，是随机打扰。
    # 纯"到八点了"只能触发检查，不能生成问候内容。
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 同源同类型只允许一条：避免同一件事反复生成候选
    dedupe_key: Mapped[str | None] = mapped_column(
        String(192), nullable=True, unique=True
    )
    # 结构化原因码（不是给用户看的文案）
    reason_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # 最早可以说 / 最晚还值得说
    not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    best_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 谁能知道这件事（机会本身也可能带隐私）
    visibility: Mapped[str] = mapped_column(String(32), default="subject_private")
    # 是否期待对方回应。生活分享默认 False——不能因为没人回就追问
    reply_expected: Mapped[bool] = mapped_column(Boolean, default=False)
    channel_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_state: Mapped[str | None] = mapped_column(String(32), nullable=True)


# ------------------------------------------------------------------ W2 知识/设备/语音


class KnowledgeItem(Base, TimestampMixin):
    """外部知识与来源时效。来源必须可核验。"""

    __tablename__ = "knowledge_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(200), index=True)
    claim: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source_kind: Mapped[str] = mapped_column(String(32), default="verified_source")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    # 归属：NULL = 公共知识（任何人可读）；非 NULL = 该 subject 私有。
    # 必须有这一层：source_kind 可以是 user_statement（"用户说他住在杭州"），
    # 这种条目如果全局可见，就是把一个人的自述喂给另一个人。
    owner_subject_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    visibility: Mapped[str] = mapped_column(String(32), default="subject_private")


class DeviceRow(Base, TimestampMixin):
    """已配对设备（手机/VR/桌面）。有独立权限与撤销。"""

    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # phone|vr|desktop|robot
    name: Mapped[str] = mapped_column(String(64), default="设备")
    capabilities: Mapped[list] = mapped_column(JSON, default=list)
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SpeechSegment(Base):
    """语音分段结果。partial 只用于字幕，final 才进主链。"""

    __tablename__ = "speech_segments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="final")  # partial|final
    text: Mapped[str] = mapped_column(Text, default="")
    audio_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    model_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsentRule(Base, TimestampMixin):
    """授权范围与有效期。撤权后派生投影必须失效。"""

    __tablename__ = "consent_rules"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(64), index=True)
    scope: Mapped[str] = mapped_column(String(64))  # memory.cross_device|camera|location|publish
    granted: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class MediaEvidenceRow(Base, TimestampMixin):
    """媒体证据：一段图片/音频**被看到或被推断出**什么。

    为什么单独一张表而不是塞进 memory：媒体引用的生命周期比记忆短得多
    （``expires_at``），而且它的可信度决定它能不能进事实。混进 memory
    会丢掉"这是猜的还是看到的"这个区分。

    两条硬规则：

    1. **只在授权窗口被描述**。记录绑定 ``window_id``，别的窗口读不到；
       是否能描述由 ``media.inbound`` 授权决定，默认拒绝。
    2. **图像猜测不写事实**。``origin=model_inference`` 的描述永远
       ``fact_eligible=False``——模型看图说"这是一只猫"只是猜测，
       不能变成"用户养了一只猫"这样的记忆。
    """

    __tablename__ = "media_evidence"
    __table_args__ = (
        # 去重**按窗口**：同一张图在两个窗口里是两条独立证据
        # （各自的描述、各自的有效期）。只锁 dedupe_key 的话，后一个窗口
        # 拿到的是前一个窗口的证据，消息里引用的就不是用户刚发的那张。
        UniqueConstraint("dedupe_key", "window_id", name="uq_media_evidence_dedupe"),
        Index("ix_media_evidence_actor_window", "actor_id", "window_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    # 上传者的 subject；角色自己产生的素材为空
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    # image / audio / video / emoji
    kind: Mapped[str] = mapped_column(String(16), default="image")
    # 外部引用（对象存储 key、URL、本地相对路径），不存二进制
    media_ref: Mapped[str] = mapped_column(String(512))
    # user_upload / camera / model_generated
    origin: Mapped[str] = mapped_column(String(24), default="user_upload")
    visibility: Mapped[str] = mapped_column(String(32), default="window_private")
    # 记录这条证据时生效的授权修订。撤权后据此失效。
    consent_revision: Mapped[int] = mapped_column(Integer, default=0)
    # 看见了什么/推断出什么。**可能是猜的**，看 origin。
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 描述是谁产生的：human / model_inference
    description_origin: Mapped[str | None] = mapped_column(String(24), nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(192))
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class IntentRow(Base, TimestampMixin):
    """她自己产生的「想做什么」——一个跨轮的待办念头。

    为什么要独立于 agenda / activity：

    - ``agenda_plans`` 是**日程**：有明确起止时间的安排
    - ``ActivityRow`` 是**实际在做的事**
    - 这里是**念头**：「等她上线要说这个」「回头问问那件事」

    三者混在一起会让"没按日程做"和"没想起来说"变成同一类失信。
    念头不是承诺，到期没做只是自动失效，不需要道歉。

    ``rationale`` 必须记下**为什么会有这个念头**，且引用具体来源。
    没有来源的意图不允许产生——"凭空想打个招呼"正是路线图明确排除的假自主。
    """

    __tablename__ = "intents"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_intents_dedupe"),
        Index("ix_intents_state_due", "state", "not_before"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 产生时的窗口：决定可见性上限，不能用来跨窗口
    window_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # reply | note | act | reach_out
    kind: Mapped[str] = mapped_column(String(24), default="reach_out")
    # 想达成什么（不是最终文案）
    goal: Mapped[str] = mapped_column(Text)
    # 为什么有这个念头。**必须引用具体来源**
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # message / self / event
    source_kind: Mapped[str] = mapped_column(String(24), default="message")
    source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    origin: Mapped[str] = mapped_column(String(32), default="model_inference")
    visibility: Mapped[str] = mapped_column(String(32), default="subject_private")

    # open | consumed | dropped | expired
    state: Mapped[str] = mapped_column(String(24), default="open", index=True)
    # 不到点不试
    not_before: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 过期就放弃，**不补发**
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    dedupe_key: Mapped[str] = mapped_column(String(192))


class InteractionLoadRow(Base, TimestampMixin):
    """某个范围内的交互负担（"回烦了"的量化）。

    为什么不复用 ``ActorStateRow.social_load``：那是**整体**负担，
    跨窗口累计。而"被这个群烦到"和"被那个人烦到"是两件不同的事，
    混在一起会立刻变成跨受众泄露——群里的烦躁不该影响对私聊对象的态度。

    所以这里按 ``scope_key`` 隔离（用窗口 id），
    并且**按时间惰性衰减**：读的时候用时间戳推算，不需要定时任务。

    它只影响"要不要回、回多长"。**语气由人设决定，不由这个数值决定。**
    """

    __tablename__ = "interaction_load"
    __table_args__ = (
        UniqueConstraint("actor_id", "scope_key", name="uq_interaction_load_scope"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    # 作用域：窗口 id。跨受众的累计在这里是被禁止的
    scope_key: Mapped[str] = mapped_column(String(128))
    local_load: Mapped[float] = mapped_column(Float, default=0.0)
    # 上次结算时间。衰减从这里推算，不靠后台任务
    settled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AttentionCursorRow(Base, TimestampMixin):
    """注意力调度游标：下次什么时候看这个窗口。

    与 ``Message.attention_state`` 是两件事：

    - 消息状态是**事实**（她看没看见这一条），逐条可审计
    - 调度状态是**计划**（下次何时看），每窗口一行

    混在一起要么每次轮询扫全部未读推算时间，要么把计划写进消息行——
    两者都会把简单查询变成全表更新。

    ``lease_until`` 配合 ``SKIP LOCKED`` 防多 worker 重复处理同一窗口。
    ``focused_until`` 是"正在看这个群"的租约——**带 TTL 的状态，不是事件**。
    """

    __tablename__ = "attention_cursor"

    window_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # 已处理到的消息序（只用于调度，不替代逐条的 attention_state）
    last_sequence: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 上次**真的调了模型**的时间。NULL 表示从没调过。
    # 不能拿 updated_at 代替：游标刚建出来时它等于现在，
    # 第一次轮询会被自己的冷却挡住。
    last_call_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    focused_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # @ 之后等补正文的硬截止
    mention_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AttentionPollLogRow(Base):
    """一次轮询的结论与成本。给"她的判断像不像人"提供可查的历史。

    记的是**每次轮询**而不是每条消息。**不存消息正文**：
    统计不需要正文，存了等于把私聊内容多复制一份到审计表。
    """

    __tablename__ = "attention_poll_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    polled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    unseen_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    # 影子基线：如果每条消息各调一次会调几次
    baseline_calls: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class TogetherSessionRow(Base, TimestampMixin):
    """共处会话（共读/共看/一起听）。

    原先是纯内存字典，重启即全丢、多 worker 互相看不见。
    共处是"正在一起做一件事"的状态，丢了用户只看到"刚才那个共读没了"，
    还没有任何错误提示——那是数据丢失，不是性能问题。
    """

    __tablename__ = "together_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    host_subject_id: Mapped[str] = mapped_column(String(64), index=True)
    state: Mapped[str] = mapped_column(String(16), default="active")
    # 每个参与者各自的上报位置。**服务端不猜"看到哪了"**——
    # 进度只能来自客户端真实回报。
    progress: Mapped[dict] = mapped_column(JSON, default=dict)


class TogetherParticipantRow(Base):
    __tablename__ = "together_participants"
    __table_args__ = (
        UniqueConstraint("session_id", "subject_id", name="uq_together_participant"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("together_sessions.id", ondelete="CASCADE"),
        index=True,
    )
    subject_id: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# W2 需要新表时由对应批次提交迁移，主智能体协调 schema owner。


__all__ = [
    "ActivityRow",
    "ActorAccount",
    "AwarenessItemRow",
    "ActorEvent",
    "ActorState",
    "AuthSession",
    "AffectState",
    "CommitmentRow",
    "ConfigRevision",
    "ConsentRule",
    "AttentionCursorRow",
    "AttentionPollLogRow",
    "TogetherParticipantRow",
    "TogetherSessionRow",
    "IntentRow",
    "InteractionLoadRow",
    "MediaEvidenceRow",
    "DeviceRow",
    "GoalRow",
    "KnowledgeItem",
    "MemoryEpisode",
    "MemoryRetrievalLog",
    "MemoryRow",
    "ProactiveOpportunity",
    "RelationshipEvent",
    "RelationshipState",
    "CoreMemoryRow",
    "EntityRow",
    "OpsAlertState",
    "ActorRoutineRow",
    "AgendaPlanRow",
    "ActorRoutineRow",
    "AgendaPlanRow",
    "PresenceState",
    "SceneCommitRow",
    "SelfExperienceRow",
    "MemoryEmbeddingRow",
    "MemoryEntityRow",
    "SessionPolicyRow",
    "SpeakLeaseRow",
    "TurnControlRow",
    "SpeechSegment",
    "WorldPlace",
    "DeliveryReceiptRow",
    "HandoffCapsuleRow",
    "IdempotencyKey",
    "Message",
    "Operation",
    "OutboxEvent",
    "Persona",
    "ProviderCredential",
    "Subject",
    "Turn",
    "User",
    "Window",
    "WindowContext",
    "utcnow",
]

# 避免未使用告警（保留类型导入供后续批次）
_ = (Float,)
