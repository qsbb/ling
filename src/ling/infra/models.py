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

from datetime import UTC, date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
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
    # 场景覆盖：只存**写过的那些场景**，没写的不落库，读时回落到
    # `domains/persona/scenarios.py` 的内置默认。存"生效文本"会把默认值
    # 复制进每一行——以后想改默认，已有的人设永远收不到。
    scenario_prompts: Mapped[dict] = mapped_column(JSON, default=dict)
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
    # `ix_messages_unseen` 是 0034 建的**偏索引**，必须在这里显式声明：
    # 缺了它就等于告诉 autogenerate「这个索引该删」，下一个迁移会真的把它删掉，
    # 注意力轮询会从「索引上顺序取 N 条」退化成「扫窗口后回表过滤」。
    # 定义必须与 0034_unseen_partial_index.py 逐字一致。
    __table_args__ = (
        Index("ix_messages_window_seq", "window_id", "sequence"),
        Index(
            "ix_messages_unseen",
            "window_id",
            "sequence",
            postgresql_where=sa.text("attention_state = 'unseen' AND role = 'user'"),
        ),
    )

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
    # 对方在平台上**撤回了**这条消息（迁移 0055）。
    #
    # 非空之后：原文不再进任何模型上下文（历史 / 摘要素材 / 提示词），
    # 但**行不删**——她当时为什么没接那句话、记忆为什么缺一块，都要能追溯。
    # 幂等靠条件更新 `WHERE retracted_at IS NULL`，不靠进程内去重。
    #
    # 为什么不用 `state='retracted'`：`state` 描述的是**这一轮的处理状态**
    # （completed/failed/stopped…），撤回是**消息在平台上被收回**，两件事
    # 可以同时成立（她已回复完，对方随后撤回）。挤进同一列会让"这轮失败了"
    # 和"对方撤回了"互相覆盖。
    retracted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 撤回**发生时**她是否已经看过这条（迁移 0056）。
    #
    # 只有 `retracted_at` 不够：用户要求分两种情况——
    #
    # - 已经看过（`attention_state='seen'` / `handled_as` 已定）：撤回不能把
    #   她见过的事实抹掉，上下文里保留原文并附注「（这条消息后来被撤回了）」，
    #   已经发出的回复也不回滚；
    # - 还没看过：`retracted_at` 打上，且**不进任何模型上下文**（等同没看见）。
    #
    # 为什么要**落一列**而不是读取时再推：`attention_state` 会变（`missed`
    # 就是"一直没机会看"），读取时推等于用**今天**的状态去解释**撤回那一刻**
    # 的事实。判定必须在撤回那一刻做完并存下来——这也是排障时唯一能回答
    # "当时到底看没看见"的东西。
    #
    # NULL 只出现在"没有撤回"的行上；撤回过的行一定是 true/false。
    retracted_after_seen: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # --- 出站撤回：**她自己**把自己说过的话收回来（迁移 0057）---
    #
    # 与上面的 `retracted_at` 是**两件事**：那个是"对方收回了"，这个是
    # "她收回自己的话"。撤回方不同，窗口/频次/回执也都不同，所以列必须分开
    # （见 `migrations/versions/0057_outbound_recall.py` 的说明）。
    #
    # `recalled_at` 只有平台回执 == recalled 时才写；`uncertain` 只写
    # `recall_attempted_at`——把"试过"当成"成了"会让一次超时被永久记成功。
    recalled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recall_state: Mapped[str | None] = mapped_column(String(24), nullable=True)
    recall_receipt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recall_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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


class PromptSnapshot(Base):
    """一次回复**实际发给模型**的上下文快照（对话页「查看本次提示词」）。

    为什么要有：她说的每句话都由一大段注入内容决定（人设、记忆、摘要、
    历史、媒体描述…）。没有快照时，"她为什么这么说"只能靠猜——而所有
    注入来源都写着"尽力而为"，所以出问题时连"当时到底注入了什么"都拿不到。

    ## 三条硬约束

    1. **一轮一份**：``turn_id`` 唯一。重试（attempt+1）**覆盖**同一行，
       不按 attempt 追加——否则这张表会随重试次数膨胀，而且同一轮的旧
       提示词既没人看、又多一份泄漏面。
    2. **写快照绝不阻塞出话**：捕获点用**独立事务**写入，任何异常只记日志。
    3. **单条 ≤ ``PROMPT_SNAPSHOT_MAX_BYTES``**（见 ``kernel/turn.py``）：
       超了就丢中间段、保留 system 与最后 N 条，并置 ``truncated=True``。

    ## 保留期

    ``expires_at`` = 写入时刻 + 7 天。到期后 ``payload`` 由维护任务清空
    （``size_bytes=0``，行本身作为"这一轮**曾经**有过快照"的墓碑短暂保留），
    再放一段时间才真删。这样读接口才能把「过期」与「从没存下来」分开——
    把两者混成一种，等于把"内容按保留期清掉了"报成"从来没有过"。

    ``message_id`` 可空：快照在**回复落库之前**就写了（比消息更早），
    回复落库后由 ``TurnCoordinator._finalize`` 回填。不加外键：快照的写入
    与消息生命周期无关，重试删旧消息时也不该因为外键把快照挡住。
    """

    __tablename__ = "prompt_snapshots"
    __table_args__ = (
        UniqueConstraint("turn_id", name="uq_prompt_snapshots_turn"),
        Index("ix_prompt_snapshots_actor_created", "actor_id", "created_at"),
        # 读接口按消息反查快照；清理任务扫过期时间。名字必须与
        # `0053_prompt_snapshots.py` 逐字一致（否则 autogenerate 会把它当成
        # "该删的索引"，下一个迁移就真删了）。
        Index("ix_prompt_snapshots_message", "message_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    turn_id: Mapped[str] = mapped_column(String(64))
    window_id: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # payload 的**结构**版本（不是契约 v1）。结构变了就升它，老行照原样读，
    # 读侧按版本决定怎么渲染——不要把老快照读崩。
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    # 真正要发出去的那份 messages（+ 每条的语义块标签与 token 估算）。
    # 到期后由维护任务清空正文，只留 model / token_total 这类非内容元数据；
    # 这里**不存**原始用户输入之外的任何东西（输入本来就在 messages 里）。
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


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


class ConnectorConfigRevision(Base):
    """接入器配置的**候选/已生效修订**（OneBot / QQBot / 白名单）。

    为什么不复用 ``config_revisions``：那张表的 ``payload`` 是可读 JSON，装的是
    "模型怎么跑"这类与发布/回滚绑定的配置；接入器修订里含**渠道密钥**，
    必须逐字段密文存储，而且它的生效方式完全不同（重建长连接实例，不是发布
    新配置给下一次请求）。混进同一张表会让"回滚模型配置"顺手改掉入站凭据。

    状态机：``pending``（已校验、等激活）→ ``activating``（正在切换实例）
    → ``active``（全部副本确认）；失败落 ``rolled_back``（已回到上一版）；
    回退本身也失败才是 ``degraded``（**必须可观测**，不能报成功）。
    """

    __tablename__ = "connector_config_revisions"
    __table_args__ = (
        Index("ix_connector_config_revisions_scope_state", "scope", "state"),
        Index("ix_connector_config_revisions_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # onebot | qqbot | access
    scope: Mapped[str] = mapped_column(String(16), index=True)
    # 上一版（用于回退时读"旧的有效覆盖集合"）。为空表示这是该 scope 的第一版。
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 稳定错误码（不携带原始异常字符串：它可能带 URL / Authorization）。
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # 可以安全展示给 owner 的一句话（不含值、不含异常原文）。
    error_safe: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConnectorOverrideValue(Base):
    """某个修订里的一项覆盖值（每个 scope 的完整覆盖集合快照里的一个成员）。

    **不出现的 key = 回退到 env**，不写空串哨兵：空串在密钥上是"没设过"、
    在文本上是"显式空值"，用哨兵表达"没有覆盖"会把这两件事混成一件。

    CHECK 约束保证"密钥只存密文、非密钥只存规范化值"：
    ``kind='secret'`` 时 ``ciphertext`` 非空且 ``plain_value`` 为空；
    其余 ``plain_value`` 非空且 ``ciphertext`` 为空。这条约束的价值在于
    它防的是**未来某次改动的疏忽**（比如有人图省事把密钥写进可读列），
    而且是被数据库拒绝，不依赖代码评审。
    """

    __tablename__ = "connector_override_values"
    __table_args__ = (
        UniqueConstraint("revision_id", "field_key", name="uq_connector_override_key"),
        CheckConstraint(
            "value_kind IN ('secret', 'text', 'switch', 'list')",
            name="ck_connector_override_kind",
        ),
        CheckConstraint(
            "(value_kind = 'secret' AND ciphertext IS NOT NULL AND plain_value IS NULL)"
            " OR (value_kind <> 'secret' AND plain_value IS NOT NULL AND ciphertext IS NULL)",
            name="ck_connector_override_secret_shape",
        ),
        Index("ix_connector_override_revision", "revision_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_id: Mapped[str] = mapped_column(String(64), index=True)
    # 注册表里的固定 key（迁移不从请求动态生成 schema，见 settings/connector_fields.py）
    field_key: Mapped[str] = mapped_column(String(64))
    value_kind: Mapped[str] = mapped_column(String(16))
    # 非密钥的规范化值：JSON 标量或数组
    # （文本 → str，开关 → "1"/"0"，名单 → list[str] 且**保留前导零**）。
    # 不做成 `dict`：这里的值从来不是对象，标注成 dict 会逼调用方到处
    # `type: ignore`，也会让人以为里面能放结构化内容。
    plain_value: Mapped[Any] = mapped_column(JSON, nullable=True)
    # 密钥：SecretBox（AES-GCM）密文，逐字段加密
    ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    # UI 元数据（前 N 字符 + 固定遮罩）。不是解密依据，也不参与认证。
    preview: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 审计指纹（带域分离的 HMAC 截断），不参与认证与连接
    audit_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConnectorConfigHead(Base):
    """每个 scope 一行的**指针**：现在生效的是哪一版、正在激活的是哪一版。

    独立成行（而不是"查 state=active 的最新一条"）的原因：回退时需要在
    同一事务里原子地把指针从候选拨回旧版；用查询表达"最新 active"会让
    并发下的指针漂移变成一次静默的重排。
    """

    __tablename__ = "connector_config_heads"

    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    active_revision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_revision_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 单调递增的代际号：客户端的 If-Match 与副本 ACK 都以它为准
    generation: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ConnectorRuntimeAck(Base):
    """副本对某代际的确认（``(generation, replica_id)`` 唯一）。

    单副本部署里它就是"本地切换完成"的证据；多副本时只有**全部**存活副本
    ACK 才把 head 指向新修订，失联副本不能无限阻塞（有界超时后回退）。
    """

    __tablename__ = "connector_runtime_acks"
    __table_args__ = (
        # 唯一键必须带 scope：onebot 与 qqbot 可以同时停在"第 1 代"，
        # 少了 scope 这一列，第二个 scope 生效时会被唯一约束挡住。
        UniqueConstraint("scope", "generation", "replica_id", name="uq_connector_ack_generation"),
        Index("ix_connector_ack_generation", "generation"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    replica_id: Mapped[str] = mapped_column(String(64))
    # prepared | activated | failed
    phase: Mapped[str] = mapped_column(String(16), default="prepared")
    # ok | degraded | unknown —— 与 phase 分开：切过去了但远端没连上是 degraded
    health: Mapped[str] = mapped_column(String(16), default="unknown")
    error_safe: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProviderTokenUsage(Base):
    """供应商当日已用 token。一行 = 一家供应商 + 一个本地日。

    ## 为什么单独一张表，而不是塞进 config_revisions 的 payload

    用量是**运行事实**，不是配置。塞进修订里会让每次调用都写一条新修订，
    修订历史会被用量刷成噪音，而回滚一份旧修订会连带把用量"回滚"掉。

    ## 为什么主键是 (provider_id, day)

    额度是"这家今天能用多少"。按职责分行会让同一家出现五行，加起来才是真实
    消耗——而任何一行单独看都不完整，超限判断必须先求和，那是每轮调用都要
    多一次聚合。用复合主键让"读一次就是全部"。

    ## 为什么 day 是 DATE 而不是时间戳

    额度按**本地日历日**清零（`ling.models.quota.local_day`）。存日期而不是
    时间戳，是为了让"跨天"这件事只是一次主键失配，不需要任何清理任务。

    ``estimated=True`` 表示这一格里有调用没拿到供应商的 usage 回执，数值来自
    本地估算。显示时必须区分，否则用户会把估算值当成账单。
    """

    __tablename__ = "provider_token_usage"
    __table_args__ = (
        UniqueConstraint("provider_id", "day", name="uq_provider_token_day"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(String(64), index=True)
    day: Mapped[date] = mapped_column(sa.Date, index=True)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
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
    __table_args__ = (
        CheckConstraint(
            "satiety BETWEEN 0 AND 100", name="ck_presence_states_satiety"
        ),
    )

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 0-100。低精力只影响表达长度与主动意愿，**不影响是否回答**
    energy: Mapped[int] = mapped_column(Integer, default=70)
    # 0-100。高社交负担时少追问，同样不影响回答
    social_load: Mapped[int] = mapped_column(Integer, default=20)
    # 0-100 饱腹（模拟值，不是生理指标）。与 energy 共用同一个 `settled_at`
    # 惰性结算：不另起"饥饿 tick"。默认 70 是**模拟初值**，不伪造过去的餐食。
    satiety: Mapped[int] = mapped_column(
        Integer, default=70, server_default=sa.text("70")
    )
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

    **主键是 (turn_id, attempt)，不是 turn_id**：停止信号属于"这一轮"，
    而"这一轮"的标识是 ``turn_id + attempt``。retry 复用同一个 turn_id
    （只把 attempt 加一），若信号只按 turn_id 绑定，上一轮留下的停止标记
    会让**每一次重试出来的新一轮立刻被停掉**——实测在 0.5s 轮询间隔下，
    重试后的新一轮首次检查就命中旧标记，直接收敛成 stopped，
    重试多少次都一样（用户看到"重试永远没回复"）。

    按 attempt 隔离后：旧标记只对旧 attempt 生效（旧轮次照样停得下来），
    新一轮查的是自己那一行，天然不命中。
    """

    __tablename__ = "turn_controls"

    turn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 与 Turn.attempt 对齐。存量行默认 1：既兼容旧数据，
    # 又让"上一轮留下的幽灵标记"在 retry（attempt≥2）后自然失效。
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
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


class UserPreferenceRow(Base, TimestampMixin):
    """每个 subject 的**用户自己的**偏好（当前只有时区）。

    为什么单独一张表、而不是塞进 `config_revisions.payload`：

    - 它是**用户的事实**，不是模型/行为配置。修订的语义是"我们怎么跑"，
      回滚到旧修订不该把"我在东八区"一起回滚掉。
    - 它决定"今天"的边界（总览日程、主动联系日限额）与勿扰时段落在哪几小时。
      落进修订意味着改它要走一次发布，而发布失败会让勿扰时段整段算错。

    参照 `SessionPolicyRow` 的做法：按 subject 主键、独立成表、不进修订。

    ``timezone`` 为空 = **没设过**（不是"设成了空字符串"）。解析优先级见
    ``ling/settings/user_preferences.py``：偏好 → 环境变量 → 默认
    ``Asia/Shanghai``。这个区分必须保留，否则"清掉偏好、回退默认"会变成
    一次隐式写入。
    """

    __tablename__ = "user_preferences"

    subject_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # IANA 名（如 Asia/Shanghai）。写入前已校验；读取时仍再校验一次，
    # 因为库里可能有手工改过的值。
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 改动次数。便于回答"这人的时区什么时候被改过"，与 SessionPolicyRow 对齐。
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # 视频读取模式：``frames``（抽帧，默认）/ ``native``（原生视频输入）。
    # 为空 = 没设过，读侧按 ``frames`` 处理（见 settings/user_preferences.py）。
    # **用户偏好**，不是模型配置：换个模型不该把"我怎么看视频"重置掉。
    video_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)


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
        CheckConstraint(
            "valence IS NULL OR valence BETWEEN -1 AND 1",
            name="ck_affect_states_valence",
        ),
        CheckConstraint(
            "arousal IS NULL OR arousal BETWEEN -1 AND 1",
            name="ck_affect_states_arousal",
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
    # Russell 二维连续状态（-1..1）。**可空**：旧行保留 NULL，由读取层按
    # `emotion` 标签映射；下一次写入才填。不在这里换衰减公式——
    # 调研建议指数衰减，但源码实际是线性，换公式会让旧情绪突然变长/变短。
    valence: Mapped[float | None] = mapped_column(Float, nullable=True)
    arousal: Mapped[float | None] = mapped_column(Float, nullable=True)


# ------------------------------------------------------------------ W2 世界/活动/目标


class WorldPlace(Base, TimestampMixin):
    """模拟地点。虚拟世界变量，不是现实地理位置。"""

    __tablename__ = "world_places"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    items: Mapped[list] = mapped_column(JSON, default=list)


class PhoneStateRow(Base, TimestampMixin):
    """她跟手机的关系：上次什么时候看、下次什么时候看、此刻在不在看。

    ## 为什么单独一张表

    不用 `activities.checkpoint` 塞：那是**某个活动自己的进度快照**，
    生命周期跟着活动走（活动结束就冻结）。看手机是**跨活动**的持续状态——
    写日记时也在看手机，出去吃饭时也在看手机，它不属于任何一个活动。

    ## 为什么"下次"要单独存，而不是算出来

    因为它**不是**"上次 + 固定间隔"。规则是：这次看完了才开始计时，
    而且在忙、在睡、刚聊完都会改间隔。存下来才能回答界面上那句
    「下次看手机 17:05」——现算的话每次刷新数字都不一样，人会看出来假。

    ## 状态机

    ``idle``（没在看）→ 到点了 / 被 @ 叫醒 → ``watching``（在看）
    → 收起 → 回到 ``idle``，**并把下次时间从现在起算**。
    """

    __tablename__ = "phone_states"

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # idle | watching
    state: Mapped[str] = mapped_column(String(16), default="idle")
    # 在看的时候在看哪个应用/哪个窗口（"QQ · 摸鱼群"）；不看的时候为空
    watching_app: Mapped[str | None] = mapped_column(String(48), nullable=True)
    watching_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    watching_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_looked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_look_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # 上一轮看的时候有没有接话（给界面显示「没接话」/「回了谁」）
    last_result: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 那段时间本来在做什么（"在写日记"），界面上用来解释为什么是这个间隔
    last_context: Mapped[str | None] = mapped_column(String(128), nullable=True)


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
    # 这条活动由哪个日程段（叶段）启动。可空 = 临时起意/历史活动。
    # **不复用 `plan_id`**：那一个字符串已经指 `agenda_plans`，
    # 让同一个字段指两张表会同时失去外键与审计能力。
    schedule_segment_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("schedule_segments.id", name="fk_activities_schedule_segment"),
        nullable=True,
        index=True,
    )


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
    # 0027 里是「部分唯一索引」：只对非空 dedupe_key 去重。
    # 这里必须写成 Index(unique=True) + where，不能写成 UniqueConstraint：
    # 后者是整列唯一约束（含 NULL 语义差异），且 autogenerate 会认成两回事。
    __table_args__ = (
        Index(
            "ux_proactive_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=sa.text("dedupe_key IS NOT NULL"),
        ),
    )

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
    # 唯一性由上面的 `ux_proactive_dedupe` 偏索引保证（NULL 不去重）
    dedupe_key: Mapped[str | None] = mapped_column(String(192), nullable=True)
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
    # **描述失败终态**：引用本身不可用（越界/超限/形式不支持）时写下时间与原因。
    #
    # 为什么需要一列而不是只靠内存里的一个"可重试"标志：`pending()` 只按
    # `description IS NULL` 过滤，没有任何地方消费"可重试"。所以只要一条
    # 引用永久不可用，它每一轮（每 60 秒）都会被重新捞出来重试，永远重试
    # 不完，日志里一条"下载失败"重复到把真问题淹掉。要真的停下来，终态
    # 必须**落库**。
    #
    # 不写进 `description` 的原因：那会把"没看到"伪装成"看到了但没内容"，
    # 既污染证据，又让 `fact_eligible` / 上下文装配误判。
    describe_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 失败原因（截断到 200 字）。给运维看，不参与判定。
    describe_failed_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(192))
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class MediaReadJob(Base):
    """一次**视频后台取证**作业（`media.video.read`，迁移 0058）。

    裁决见 `docs/ASYNC-TOOL-DECISION.md` §3：后台读取必须是一个**有主键、能被
    查询、能取消、能在崩溃后对账**的领域作业，而不是进程内的一个 task。

    ## 为什么不是 `operations` 表加个 type

    `operations` 记的是**投递**（一个 part 发到平台、拿到回执），它的状态词表
    围绕"发没发出去"；而这个作业的状态围绕"读没读到、能不能再花钱"。硬塞进
    去会让两边共享一套 `state` 字符串，而它们的合法转移完全不同——合成一张表
    之后，两边的状态机只能有一个活下来。

    ## 字段分四组

    1. **身份**：`call_id` 主键 + `actor/subject/window/evidence`。窗口用于
       "同窗口鉴权"，evidence 决定读谁。
    2. **因果**：`source_message_id/source_turn_id/source_attempt` 记"是谁在
       哪一轮要求的"。裁决明确**不从自然语言猜**用户想不想等，所以这个来源只
       用于审计与事后解释，**不作为**准入判据。
    3. **协议**：`request_key`（幂等键）+ `tool_version` + `mode` +
       `feedback_mode` + `authorization_ref`（登记时生效的那条授权修订）。
    4. **状态**：`state` + `error_code` + `result_ref` + 时间戳 + 租约。

    ## 约束

    - `UNIQUE(actor_id, window_id, request_key)`：同一个人在同一窗口里重发
      同一个 `request_key` 必须命中**同一个**作业，而不是排第二个。跨窗口不
      合并——两个窗口的授权与可见性不同，合并等于让 A 窗口的读取结果进 B 窗口。
    - **部分唯一索引** `(evidence_id, tool_version) WHERE state IN
      ('pending','running')`：同一证据同一版本最多一个在途作业。为什么是部分
      索引而不是普通唯一约束：终态之后**允许**再排一次（用户可能想再读一遍，
      或上次失败想重试），只是不允许"同一个证据同时有两个在途读取"——那会
      两次视觉调用、两次收费，而结果写的是同一行证据。
    """

    __tablename__ = "media_read_jobs"
    __table_args__ = (
        UniqueConstraint(
            "actor_id", "window_id", "request_key", name="uq_media_read_job_request"
        ),
        # 在途互斥（部分唯一索引）。`postgresql_where` 让 SQLite 之类的方言
        # 仍能建表（本项目生产用 PG，测试库也是 PG，但迁移可回滚性依赖方言
        # 不报错）。
        Index(
            "uq_media_read_job_active_evidence",
            "evidence_id",
            "tool_version",
            unique=True,
            postgresql_where=text("state IN ('pending', 'running')"),
            sqlite_where=text("state IN ('pending', 'running')"),
        ),
        Index("ix_media_read_jobs_actor_window", "actor_id", "window_id"),
        # 清理任务（第 3 步）按 state + deadline 扫过期/失联作业。
        Index("ix_media_read_jobs_state_deadline", "state", "deadline"),
        Index("ix_media_read_jobs_lease", "lease_owner", "lease_until"),
    )

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    window_id: Mapped[str] = mapped_column(String(64), index=True)
    evidence_id: Mapped[str] = mapped_column(String(64), index=True)

    # --- 因果：谁在什么时候要求的（**只用于审计，不作准入判据**）---
    source_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 同一轮的第几次尝试。重试不删除已交付文本（与 messages.attempt 同语义）。
    source_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- 协议 ---
    request_key: Mapped[str] = mapped_column(String(128))
    # 工具版本：改行为就要改它。部分唯一索引按它分组。
    tool_version: Mapped[str] = mapped_column(String(64))
    # 首期只有 `video`（这个作业只读视频）。留字段是为了将来扩展时不用迁移。
    mode: Mapped[str] = mapped_column(String(24), default="video")
    # **首期固定 `evidence_only`**：只把结果写成证据，不主动补话。
    feedback_mode: Mapped[str] = mapped_column(String(32), default="evidence_only")
    # 登记时生效的授权修订（`consent_revisions`）。撤权后据此判"这条还该不该读"。
    # 存的是**哪一条授权**，不是布尔：撤权审计时要指回具体的人。
    authorization_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # --- 状态机 ---
    # 取值见 `domains/media/jobs.py::JobState`。迁移里有 CHECK 约束兜底。
    state: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # 稳定错误码（`model_unavailable` / `download_failed` / ...），人话在接口里映射。
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 结果引用：成功时指向 **evidence_id**（描述写进那条证据，不另存一份副本）。
    # 为什么不是复制描述正文：证据是唯一的事实载体，复制一份就有两个"最新描述"。
    result_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 排队截止：登记 + 15 分钟。超过还没开始 → 清理任务终结为 expired。
    deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 模型**可能已经调用**的时间。`unknown` 的判据：有它、但没有终态。
    model_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- 租约（第 3 步的执行栅栏）---
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 每次抢占 +1。结果提交时带 `WHERE lease_generation = :gen`，旧代次的提交
    # 更新行数为 0——这是"被接管的作业不会写出两份结果"的唯一保证。
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)


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
    # 索引名以 0035 迁移为准；`index=True` 会自动派生成
    # `ix_attention_cursor_next_due_at`，与既有索引对不上
    __table_args__ = (Index("ix_attention_cursor_due", "next_due_at"),)

    window_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
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
    # 窗口索引名以 0036 迁移为准（`ix_attention_poll_log_window`）；
    # polled_at 的 index=True 生成的 `ix_attention_poll_log_polled_at` 与迁移一致
    __table_args__ = (Index("ix_attention_poll_log_window", "window_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_id: Mapped[str] = mapped_column(String(64))
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
    # 索引名以 0037 迁移为准（`ix_together_sessions_host`）
    __table_args__ = (Index("ix_together_sessions_host", "host_subject_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    host_subject_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16), default="active")
    # 每个参与者各自的上报位置。**服务端不猜"看到哪了"**——
    # 进度只能来自客户端真实回报。
    progress: Mapped[dict] = mapped_column(JSON, default=dict)


class TogetherParticipantRow(Base):
    __tablename__ = "together_participants"
    # 索引名以 0037 迁移为准（`ix_together_participants_session`）
    __table_args__ = (
        UniqueConstraint("session_id", "subject_id", name="uq_together_participant"),
        Index("ix_together_participants_session", "session_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("together_sessions.id", ondelete="CASCADE"),
    )
    subject_id: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(128))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# W2 需要新表时由对应批次提交迁移，主智能体协调 schema owner。


# ------------------------------------------------------------------ 她的一天（日程时间轴）
#
# 一条硬边界写在最前面：**计划是意向，不是事实**。这里的段只表达"想做什么/
# 若无新事可能怎样"，完成必须来自 `activities` 的完成证据，梦也不能回灌成经历。
#
# 三层（block/hour/minute）都是意向，父子段有意重叠；只有叶段可用于启动活动。
# 内部时间一律左闭右开 `[start_at, end_at)`，UTC after 入库，展示时由 IANA
# 时区投影。两个"日"的定义见 `domains/agenda/timeline.py`：
#   - 展示日：[D 00:00, D+1 00:00)（用户有效时区）
#   - 计划日：[D 04:00, D+1 04:00)（角色规范时区，以起点日期 D 命名）

DAY_PLAN_STATES: tuple[str, ...] = ("pending", "ready", "degraded")
SEGMENT_LAYERS: tuple[str, ...] = ("block", "hour", "minute")
SEGMENT_STATUSES: tuple[str, ...] = ("current", "superseded", "cancelled")
SEGMENT_ANCHOR_KINDS: tuple[str, ...] = ("", "sleep", "commitment")
SEGMENT_SOURCES: tuple[str, ...] = ("model", "rule", "baseline", "user")
BEHAVIOR_CALL_STATES: tuple[str, ...] = ("reserved", "committed", "failed", "expired")
DREAM_STATUSES: tuple[str, ...] = ("ready", "failed", "missed", "no_dream")

# ------------------------------------------------------------ 长程意向（§3A/§6）
#
# 意向是"有目标日期区间、**没有**执行时段"的目标，和 `day_plans` 的段是两种东西：
# `confirmed` 只代表"用户对这个目标/日期作过明确确认"，**不是**某时段的承诺。
# 硬锚点只能来自 commitments / confirmed agenda_plans / 周期规则 occurrence。
INTENTION_STATES: tuple[str, ...] = (
    "proposed",
    "planned",
    "confirmed",
    "completed",
    "missed",
    "abandoned",
    "expired_unknown",
)
INTENTION_TERMINAL_STATES: tuple[str, ...] = (
    "completed",
    "missed",
    "abandoned",
    "expired_unknown",
)
# 模型只能创 `proposed`；`user`/`actor`/`recurrence` 才允许带来源的 `planned`。
INTENTION_SOURCE_KINDS: tuple[str, ...] = ("model", "user", "actor", "recurrence")
# 意向类别：与段的活动类别同一套标签，另加 `travel`/`other`。**不含 sleep**：
# 睡眠只由作息锚点产生，不能靠一条"想睡觉"的意向变成时间轴上的段。
INTENTION_KINDS: tuple[str, ...] = (
    "meal",
    "work",
    "study",
    "reading",
    "exercise",
    "walk",
    "chore",
    "errand",
    "leisure",
    "social",
    "hobby",
    "rest",
    "commute",
    "travel",
    "free",
    "other",
)
# 周期规则**本批只建表、不做展开**（设计 §6：等夜间输入装配那一批再接）。
RECURRENCE_RULE_STATES: tuple[str, ...] = ("active", "paused", "cancelled")


def _in_check(column: str, values: tuple[str, ...], *, name: str) -> CheckConstraint:
    """生成 ``column IN ('a','b')`` 的 CHECK。

    枚举取值在 Python 侧同时导出（上面那几个常量），**服务和数据库各判一次**：
    只靠 DB 会让"未知取值"在写入时才炸；只靠服务层则手工改库/旧代码能绕过。
    """
    joined = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({joined})", name=name)


class DayPlanRow(Base, TimestampMixin):
    """一个**计划日**的版本头（D 04:00 → D+1 04:00）。

    ``UNIQUE(actor_id, plan_day)`` 是日界幂等的唯一凭据：同一天只能有一行，
    所有并发 worker 都在这行上用 CAS ``revision`` 提交，重复调度只增版本、
    不增日期行。
    """

    __tablename__ = "day_plans"
    __table_args__ = (
        UniqueConstraint("actor_id", "plan_day", name="uq_day_plans_actor_day"),
        # 复合 FK 的目标：`schedule_segments` 用它保证"段和计划属于同一个 actor"。
        UniqueConstraint("id", "actor_id", name="uq_day_plans_id_actor"),
        Index("ix_day_plans_actor_window", "actor_id", "window_start_at"),
        CheckConstraint("revision >= 0", name="ck_day_plans_revision"),
        CheckConstraint(
            "window_end_at > window_start_at", name="ck_day_plans_window"
        ),
        _in_check("state", DAY_PLAN_STATES, name="ck_day_plans_state"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    # 计划日的起点日期（本地 D 04:00 那天），不是 UTC 日期
    plan_day: Mapped[date] = mapped_column(Date)
    # 生成这一版时用的角色规范时区与来源（preference/env/default）
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    timezone_source: Mapped[str] = mapped_column(String(16), default="default")
    # 排程快照版本：时区**或**作息变化时 +1。幂等键用它，
    # 让"换了时区要重排"与"同一天重复调度不重排"能区分开。
    timezone_revision: Mapped[int] = mapped_column(Integer, default=0)
    routine_revision: Mapped[int] = mapped_column(Integer, default=0)
    window_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    # pending（还没生成）/ ready（模型或规则大纲已提交）/ degraded（兜底生成）
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # model / rule / baseline：这一版大纲的可信来源。规则兜底必须能一眼看出来。
    generation_source: Mapped[str] = mapped_column(String(16), default="rule")
    # 最近一次失败/降级的确定性原因码（不含正文、不含路径）
    last_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)


class ScheduleIntentionRow(Base, TimestampMixin):
    """一条**有目标日期区间、没有执行时段**的长程意向（设计 §3A）。

    ## 三个对象必须分清

    - ``day_plans(D)``：某一天可修订的候选时间轴；
    - ``schedule_intentions``：指向未来日期区间的**有来源目标**，尚无时段；
    - ``commitments`` / confirmed ``agenda_plans`` / 周期 occurrence：才可能提供
      D 的**不可移动时间锚点**。

    所以 ``state='confirmed'`` **不是**"某个时段已经约好"：它只表示用户对这个
    目标/日期作过明确确认，仍然是高优先**软偏好**。把 `confirmed` 当硬占用会
    让"周五想旅行"凭空堵掉整周的时间轴。

    本表也**不承载**完成事实：``completed`` 只保存证据引用，事实入库仍归
    ``activities`` + 完成证据链，绝不写 ``self_experiences``。

    ## 唯一性与幂等

    ``UNIQUE(actor_id, source_kind, source_ref, target_start_day, target_end_day)``
    是"同一来源重放返回原 ID"的凭据。**不用**标题或 JSON 做唯一性：标题会改，
    JSON 在 PostgreSQL 里没有稳定的相等语义。

    ## 状态与证据

    - ``proposed`` 模型/歧义候选，``planned`` 有明确来源的愿望，``confirmed``
      有**独立确认来源**（CHECK 要求 ``confirmation_source_ref`` 非空）；
    - 终态 ``completed/missed/abandoned/expired_unknown`` 不可复活；
    - 终态必须带证据引用，**只有** ``expired_unknown`` 例外——"过期 = 不知道"，
      没有证据就不能写成 ``missed``。

    ## 两个版本号

    ``revision`` 是写入方的乐观锁（每次变更 +1）；``horizon_rev`` 只跟**展望
    投影**相关的变更走（状态/目标区间/priority/准备依赖/可见范围），夜间
    D+1…D+7 输入快照用它做 CAS。反复进入 prompt/展望**不构成升级证据**，也不会
    改动这两个值。

    ## 保留期（本批只写约定，清理任务不实现）

    终态正文 90 天后归档为"ID/状态/来源哈希/时间"的审计摘要，审计摘要至少留
    1 年；被计划段或决定引用的行不提前删。
    """

    __tablename__ = "schedule_intentions"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "source_kind",
            "source_ref",
            "target_start_day",
            "target_end_day",
            name="uq_schedule_intentions_source",
        ),
        Index(
            "ix_schedule_intentions_actor_horizon",
            "actor_id",
            "target_start_day",
            "state",
        ),
        CheckConstraint(
            "target_end_day >= target_start_day",
            name="ck_schedule_intentions_window",
        ),
        # 目标跨度 ≤ 90 天（设计 §6）。写成减法而不是 `date + 90`：Date 加整数
        # 在 PostgreSQL 是"加天数"，但在 sqlite/其它方言里没有同义语义。
        CheckConstraint(
            "target_end_day - target_start_day <= 90",
            name="ck_schedule_intentions_span",
        ),
        CheckConstraint(
            "(prep_due_day IS NULL) = (prep_source_ref IS NULL)",
            name="ck_schedule_intentions_prep_pair",
        ),
        CheckConstraint(
            "prep_due_day IS NULL OR prep_due_day < target_start_day",
            name="ck_schedule_intentions_prep_due",
        ),
        CheckConstraint(
            "state != 'confirmed' OR confirmation_source_ref IS NOT NULL",
            name="ck_schedule_intentions_confirmed_source",
        ),
        CheckConstraint("priority BETWEEN 0 AND 100", name="ck_schedule_intentions_priority"),
        CheckConstraint("revision >= 1", name="ck_schedule_intentions_revision"),
        CheckConstraint("horizon_rev >= 1", name="ck_schedule_intentions_horizon_rev"),
        _in_check("state", INTENTION_STATES, name="ck_schedule_intentions_state"),
        _in_check(
            "source_kind",
            INTENTION_SOURCE_KINDS,
            name="ck_schedule_intentions_source_kind",
        ),
        _in_check("kind", INTENTION_KINDS, name="ck_schedule_intentions_kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    # 目标区间用 `target_timezone` 的**本地日期**表达（与 plan_day 同为午夜日），
    # 不是 UTC 日期：解析/展示都按 IANA 时区投影，不能拿服务器时区推断。
    target_start_day: Mapped[date] = mapped_column(Date)
    target_end_day: Mapped[date] = mapped_column(Date)
    target_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    # 类别（受 INTENTION_KINDS 白名单约束）；标题只是短主题，不参与唯一性。
    kind: Mapped[str] = mapped_column(String(32), default="other")
    title: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(24), default="proposed")
    # model / user / actor / recurrence
    source_kind: Mapped[str] = mapped_column(String(16))
    # 来源 ID：turn_id / commitment_id / recurrence_rule_id / 上游 intention_id。
    # 撤回时按它递归传播（下游意向用 `source_ref=<上游 intention_id>` 表示派生）。
    source_ref: Mapped[str] = mapped_column(String(160))
    # 授权/证据引用：`confirm()` 写确认来源；终态写完成/失败/撤回的依据。
    # 终态会覆盖确认时的值，所以**确认来源另存** `confirmation_source_ref`。
    resolution_evidence_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    confirmation_source_ref: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 前置依赖（§3A）：只有**另有来源**的准备依据才允许落准备段；二者同空或同非空。
    # 用户确认"目标"不顺带确认模型猜的准备事项。
    prep_source_ref: Mapped[str | None] = mapped_column(String(160), nullable=True)
    prep_due_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 0-100。展望排序在同状态内用它打破平手，不改变状态优先级。
    priority: Mapped[int] = mapped_column(Integer, default=50)
    horizon_rev: Mapped[int] = mapped_column(Integer, default=1)
    # actor_private / subject_private / window_private / group_scope / public
    visibility: Mapped[str] = mapped_column(String(32), default="actor_private")
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ScheduleRecurrenceRuleRow(Base, TimestampMixin):
    """用户确认的周期规则：**精确本地时段 + 有效期**（设计 §6）。

    ## 本批只建表，展开**未启用**

    这一批只落结构与约束：没有任何代码会读它生成 occurrence、也不会把它变成
    时间轴上的硬锚点。展开要在夜间输入装配/周期接线那一批做，并且必须：

    - 只在**用户确认了精确时段和有效期**后建 ``active``（`confirmation_source_ref`
      必填且每 actor 唯一）；
    - 按 ``weekday`` 在角色本地墙钟展开到 D 窗口，跨午夜拆成各计划日内的片段；
    - 从不让"通常周四上课"这种软习惯自动变成硬锚点。

    ``end_local_minute`` 允许到 1440（本地 24:00），表示"到当天结束"；跨午夜
    请建两条规则，而不是写 ``end < start``——那会让 DST/日界换算失去唯一定义。
    """

    __tablename__ = "schedule_recurrence_rules"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "confirmation_source_ref",
            name="uq_schedule_recurrence_rule_source",
        ),
        CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_schedule_recurrence_weekday"),
        CheckConstraint(
            "start_local_minute BETWEEN 0 AND 1439 AND "
            "end_local_minute BETWEEN 1 AND 1440",
            name="ck_schedule_recurrence_minutes",
        ),
        CheckConstraint(
            "end_local_minute > start_local_minute",
            name="ck_schedule_recurrence_range",
        ),
        CheckConstraint(
            "effective_end_day >= effective_start_day",
            name="ck_schedule_recurrence_effective",
        ),
        _in_check("state", RECURRENCE_RULE_STATES, name="ck_schedule_recurrence_state"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    # 规则属于谁（可空：单角色部署里是角色自己的固定安排）
    subject_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 0=周一 … 6=周日（与设计 §6 一致；Python 的 weekday() 同一口径）
    weekday: Mapped[int] = mapped_column(Integer)
    start_local_minute: Mapped[int] = mapped_column(Integer)
    end_local_minute: Mapped[int] = mapped_column(Integer)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    effective_start_day: Mapped[date] = mapped_column(Date)
    effective_end_day: Mapped[date] = mapped_column(Date)
    # active / paused / cancelled
    state: Mapped[str] = mapped_column(String(16), default="active")
    # 用户确认的凭据（消息/确认记录 ID）。**不是**模型输出，模型无权建 active 规则。
    confirmation_source_ref: Mapped[str] = mapped_column(String(160))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    visibility: Mapped[str] = mapped_column(String(32), default="subject_private")


class ScheduleSegmentRow(Base, TimestampMixin):
    """日程时间轴上的一个段（大块 / 小时 / 分钟）。

    ## 为什么父子段允许重叠

    大块是"想做什么"，子段是"若无新事可能怎样做"。它们不是并列任务，
    拿"段与段不许重叠"一刀切会把父子关系判成冲突。

    ## 为什么 `parent_key` 是非空字符串

    PostgreSQL 的 UNIQUE 对 NULL 互不相等：根段若把 `parent_id` 写成 NULL，
    `UNIQUE(plan_id, revision_created, layer, start_at, parent_key)` 对根段就失效。
    所以根段用空串（与 `affect_states.window_id` 同一个理由）。

    ## 重叠防护分两层

    - 服务层（`agenda/timeline.py`）在事务里校验：叶段互不重叠、子段落在父段内、
      不压不可移动锚点；
    - 数据库层（迁移里，若能装 `btree_gist`）：同 actor 的 `current` 叶段加
      `EXCLUDE USING gist (actor_id WITH =, tstzrange(start_at,end_at,'[)') WITH &&)`。
      exclusion **只防叶段**，不限制父子、也不限制历史 superseded 行。
    """

    __tablename__ = "schedule_segments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "actor_id"],
            ["day_plans.id", "day_plans.actor_id"],
            name="fk_schedule_segments_plan_actor",
        ),
        # 复合自引用：子段的 parent 必须属于**同一个 plan**。
        # 只写 parent_id 单列 FK 时，模型给一个别的 plan 的段 ID 也能通过。
        UniqueConstraint("id", "plan_id", name="uq_schedule_segments_id_plan"),
        ForeignKeyConstraint(
            ["parent_id", "plan_id"],
            ["schedule_segments.id", "schedule_segments.plan_id"],
            name="fk_schedule_segments_parent_plan",
        ),
        ForeignKeyConstraint(
            ["place_id"],
            ["world_places.id"],
            name="fk_schedule_segments_place",
        ),
        # 长程扩展（§6）：为某条意向准备的段。单列 FK 足够——意向属于 actor，
        # 服务层再校验"段与意向同 actor"，没有复合键可借。
        ForeignKeyConstraint(
            ["intention_id"],
            ["schedule_intentions.id"],
            name="fk_schedule_segments_intention",
        ),
        UniqueConstraint(
            "plan_id",
            "revision_created",
            "layer",
            "start_at",
            "parent_key",
            name="uq_schedule_segments_rev",
        ),
        Index("ix_schedule_segments_actor_window", "actor_id", "start_at", "end_at"),
        Index("ix_schedule_segments_plan_rev", "plan_id", "revision_created"),
        Index("ix_schedule_segments_intention_status", "intention_id", "status"),
        CheckConstraint("end_at > start_at", name="ck_schedule_segments_order"),
        # 根段 ``parent_id IS NULL`` 且 ``parent_key = ''``；子段两者都非空。
        # **不能**写成 ``length(parent_key) > 0``：那会把根段（空串是唯一键的
        # 防 NULL 手段）全部拒收，而空串正是设计 §6 规定的根段取值。
        CheckConstraint(
            "(parent_id IS NULL AND parent_key = '')"
            " OR (parent_id IS NOT NULL AND length(parent_key) > 0)",
            name="ck_schedule_segments_parent_key",
        ),
        _in_check("layer", SEGMENT_LAYERS, name="ck_schedule_segments_layer"),
        _in_check("status", SEGMENT_STATUSES, name="ck_schedule_segments_status"),
        _in_check(
            "anchor_kind", SEGMENT_ANCHOR_KINDS, name="ck_schedule_segments_anchor"
        ),
        _in_check("source", SEGMENT_SOURCES, name="ck_schedule_segments_source"),
        CheckConstraint(
            "commitment_level IN ('tentative', 'confirmed')",
            name="ck_schedule_segments_commitment",
        ),
        # `intention_id` 与 `preparation_for_day` 同空或同非空：只有"为某目标日
        # 准备的段"才引用意向，普通段不能只挂一半的关联。
        CheckConstraint(
            "(intention_id IS NULL) = (preparation_for_day IS NULL)",
            name="ck_schedule_segments_preparation_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(String(64), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 根段为 ''（见类注释：NULL 会绕过唯一约束）
    parent_key: Mapped[str] = mapped_column(String(64), default="")
    # block / hour / minute
    layer: Mapped[str] = mapped_column(String(8), default="block")
    # 活动类别（meal/work/leisure/...）。受服务层白名单约束。
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(120))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    place_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commitment_level: Mapped[str] = mapped_column(String(16), default="tentative")
    # current / superseded / cancelled。**不物理删除**：旧段要能被审计/解释。
    status: Mapped[str] = mapped_column(String(16), default="current", index=True)
    # 只有叶段可以启动活动
    is_leaf: Mapped[bool] = mapped_column(Boolean, default=True)
    # '' 表示可变段；sleep/commitment 是**不可移动锚点**
    anchor_kind: Mapped[str] = mapped_column(String(16), default="")
    # model / rule / baseline / user
    source: Mapped[str] = mapped_column(String(16), default="model")
    # 这个段属于哪一版（与 day_plans.revision 对齐）
    revision_created: Mapped[int] = mapped_column(Integer, default=0)
    # 在哪一版被结束（superseded 时填）；NULL = 仍是当前版本
    revision_ended: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_event_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 长程扩展（§6）：这条段是为哪条意向/哪个目标日做准备。旧段保持 NULL，
    # **不猜来源**；两列由 CHECK 保证同空或同非空。
    intention_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    preparation_for_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 受众范围。新建准备段时由服务层取来源里**最严**的那个（见
    # `domains/agenda/intents.strictest_visibility`）；旧段为 NULL（未知，不猜）。
    visibility: Mapped[str | None] = mapped_column(String(32), nullable=True)


class ScheduleChangeRequestRow(Base, TimestampMixin):
    """一次"临时提议 → 局部重排"的决定记录（阶段 3 才会写）。

    阶段 1 建表是为了让重排链路有地方落审计，**不提前实现决定逻辑**。
    不存聊天原文：只存 turn/window/计划 id 与决定和原因码。
    """

    __tablename__ = "schedule_change_requests"
    __table_args__ = (
        UniqueConstraint(
            "actor_id", "source_turn_id", name="uq_schedule_change_actor_turn"
        ),
        Index(
            "ix_schedule_change_actor_created", "actor_id", "created_at"
        ),
        _in_check(
            "decision", ("accept", "decline", "defer"), name="ck_schedule_change_decision"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    source_turn_id: Mapped[str] = mapped_column(String(64))
    window_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("day_plans.id", name="fk_schedule_change_plan"),
        nullable=True,
    )
    base_revision: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[str] = mapped_column(String(16))
    reason_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    range_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    range_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    committed_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)


class BehaviorModelCallRow(Base):
    """行为模型调用（大纲/细化/重排/梦）的预算预留与租约。

    ## 为什么预算必须落库

    多 worker 下进程内计数器各算各的，同一计划日会被各副本各烧一份预算。
    所以"调用前预留"与"job lease"同事务写入这一行：
    `UNIQUE(actor_id, kind, idempotency_key, attempt)` 让同一 attempt 只有一个
    赢家；`lease_until` 过期后只能开**新 attempt**（旧 attempt 的预算不退款，
    与设计稿一致）。明细保留 90 天，provider 的 token 账单不能替代这张表。
    """

    __tablename__ = "behavior_model_calls"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "kind",
            "idempotency_key",
            "attempt",
            name="uq_behavior_model_calls_attempt",
        ),
        Index(
            "ix_behavior_model_calls_day",
            "actor_id",
            "plan_day",
            "kind",
            "state",
        ),
        Index(
            "ix_behavior_model_calls_window",
            "actor_id",
            "window_id",
            "created_at",
        ),
        _in_check("state", BEHAVIOR_CALL_STATES, name="ck_behavior_model_calls_state"),
        CheckConstraint(
            "reserved_input_tokens >= 0 AND reserved_output_tokens >= 0 "
            "AND (actual_tokens IS NULL OR actual_tokens >= 0)",
            name="ck_behavior_model_calls_tokens",
        ),
        CheckConstraint("attempt >= 1", name="ck_behavior_model_calls_attempt"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64))
    plan_day: Mapped[date] = mapped_column(Date)
    # outline / refine / replan / dream
    kind: Mapped[str] = mapped_column(String(16))
    # 聊天窗口（replan 用）。空串代替 NULL：唯一键/索引里 NULL 互不相等。
    window_id: Mapped[str] = mapped_column(String(64), default="")
    idempotency_key: Mapped[str] = mapped_column(String(160))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(16), default="reserved")
    reserved_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    actual_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DreamReportRow(Base, TimestampMixin):
    """一晚的梦（模拟叙事）。**结构上与事实隔离**。

    不写 `activities` / `self_experiences` / 事实抽取 outbox：梦是虚构叙事，
    检索者漏一次过滤就会把"梦到"说成"做过"。固定 origin/visibility 由 CHECK
    兜底，应用代码写错也进不了事实链。
    """

    __tablename__ = "dream_reports"
    __table_args__ = (
        UniqueConstraint("actor_id", "sleep_date", name="uq_dream_reports_actor_date"),
        _in_check("status", DREAM_STATUSES, name="ck_dream_reports_status"),
        CheckConstraint("origin = 'simulated'", name="ck_dream_reports_origin"),
        CheckConstraint(
            "visibility = 'actor_private'", name="ck_dream_reports_visibility"
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    # 醒来所在本地日期（跨日作息以实际预计醒点定义）
    sleep_date: Mapped[date] = mapped_column(Date)
    # ready / failed / missed / no_dream
    status: Mapped[str] = mapped_column(String(16), default="no_dream")
    title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    mood: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cue_ids: Mapped[list] = mapped_column(JSON, default=list)
    transform_ids: Mapped[list] = mapped_column(JSON, default=list)
    origin: Mapped[str] = mapped_column(String(16), default="simulated")
    visibility: Mapped[str] = mapped_column(String(24), default="actor_private")
    plan_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("day_plans.id", name="fk_dream_reports_plan"),
        nullable=True,
    )


class SleepSettlementRow(Base, TimestampMixin):
    """跨夜情绪/状态结算（阶段 4 才会写）。

    唯一键 `(actor_id, sleep_date)` 让"醒来结算"天然幂等；与梦是否成功**无关**
    ——梦失败也要结算（否则一次模型故障会连带把情绪冻住）。
    """

    __tablename__ = "sleep_settlements"

    actor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sleep_date: Mapped[date] = mapped_column(Date, primary_key=True)
    bed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    wake_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 睡前/醒后的 global 情绪快照（只存粗粒度标签与强度，不存原文）
    global_affect_before: Mapped[dict] = mapped_column(JSON, default=dict)
    global_affect_after: Mapped[dict] = mapped_column(JSON, default=dict)
    source_routine_revision: Mapped[int] = mapped_column(Integer, default=0)


class AffectSignalRow(Base):
    """日内情绪峰值信号（带 scope 与来源事件），供梦与结算取峰值。

    现有 `affect_states` 只存**当前**衰减值，回看不到"日内峰值"；
    所以单独留一张只追加的信号表。`window` 作用域的信号**不得跨受众注入**：
    表里有 window_id，读侧必须按受众过滤。
    """

    __tablename__ = "affect_signals"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "source_event_id",
            "scope",
            "window_id",
            "emotion",
            name="uq_affect_signals_event",
        ),
        Index("ix_affect_signals_actor_time", "actor_id", "occurred_at"),
        CheckConstraint("intensity >= 0", name="ck_affect_signals_intensity"),
        CheckConstraint(
            "valence IS NULL OR valence BETWEEN -1 AND 1",
            name="ck_affect_signals_valence",
        ),
        CheckConstraint(
            "arousal IS NULL OR arousal BETWEEN -1 AND 1",
            name="ck_affect_signals_arousal",
        ),
        CheckConstraint("scope IN ('global', 'window')", name="ck_affect_signals_scope"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(64), index=True)
    source_event_id: Mapped[str] = mapped_column(String(64))
    # global（可跨窗口的粗粒度趋势）/ window（只属于某窗口）
    scope: Mapped[str] = mapped_column(String(16), default="global")
    # global 存空串而不是 NULL（同 affect_states）
    window_id: Mapped[str] = mapped_column(String(64), default="")
    emotion: Mapped[str] = mapped_column(String(32))
    intensity: Mapped[float] = mapped_column(Float, default=0.0)
    valence: Mapped[float | None] = mapped_column(Float, nullable=True)
    arousal: Mapped[float | None] = mapped_column(Float, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


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
    "PromptSnapshot",
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
    "UserPreferenceRow",
    "AffectSignalRow",
    "BehaviorModelCallRow",
    "DayPlanRow",
    "DreamReportRow",
    "ScheduleChangeRequestRow",
    "ScheduleIntentionRow",
    "ScheduleRecurrenceRuleRow",
    "ScheduleSegmentRow",
    "SleepSettlementRow",
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
    "ProviderTokenUsage",
    "Subject",
    "Turn",
    "User",
    "MediaReadJob",
    "Window",
    "WindowContext",
    "utcnow",
]

# 避免未使用告警（保留类型导入供后续批次）
_ = (Float,)
