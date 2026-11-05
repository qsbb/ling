"""baseline: 冻结 W0/W2 全部表结构

Revision ID: 0001_baseline
Revises: None
Create Date: 2026-10-27

这是**可在空库建立完整基线**的迁移（33 张表 + 索引）。
已有库上线前必须：
1. 只读预检：核对实际 schema 与本基线一致
2. 备份并做恢复演练
3. `python -m ling.migrate stamp` 只登记版本，不重跑建表

之后的结构变化从 0002 起逐次迁移（例如 messages.attempt）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    existing = set(inspect.get_table_names())
    for name in TABLES:
        if name in existing:
            continue
        for stmt in DDL[name]:
            op.execute(stmt)


def downgrade() -> None:
    inspect = sa.inspect(op.get_bind())
    existing = set(inspect.get_table_names())
    for name in reversed(TABLES):
        if name in existing:
            op.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')


TABLES = [
    "activities",
    "actor_accounts",
    "actor_events",
    "actor_states",
    "affect_states",
    "commitments",
    "config_revisions",
    "consent_rules",
    "delivery_receipts",
    "devices",
    "goals",
    "handoff_capsules",
    "idempotency_keys",
    "knowledge_items",
    "memories",
    "memory_episodes",
    "memory_retrieval_logs",
    "operations",
    "outbox_events",
    "personas",
    "proactive_opportunities",
    "provider_credentials",
    "relationship_events",
    "relationship_states",
    "speech_segments",
    "subjects",
    "users",
    "window_contexts",
    "windows",
    "world_places",
    "auth_sessions",
    "messages",
    "turns",
]

# 每张表的建表语句与索引（逐条执行：op.execute 不接受多语句）
DDL: dict[str, list[str]] = {
    "activities": [
        """CREATE TABLE activities (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	kind VARCHAR(48) NOT NULL,
	state VARCHAR(24) NOT NULL,
	place_id VARCHAR(64),
	checkpoint JSON NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	ended_at TIMESTAMP WITH TIME ZONE,
	completion_evidence TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_activities_state ON activities (state)""",
        """CREATE INDEX ix_activities_actor_id ON activities (actor_id)""",
    ],
    "actor_accounts": [
        """CREATE TABLE actor_accounts (
	id VARCHAR(64) NOT NULL,
	connector_instance VARCHAR(64) NOT NULL,
	external_account_id VARCHAR(128) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	proof VARCHAR(32) NOT NULL,
	consent_revision INTEGER NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_account_external UNIQUE (connector_instance, external_account_id)
)""",
        """CREATE INDEX ix_actor_accounts_subject_id ON actor_accounts (subject_id)""",
        """CREATE INDEX ix_actor_accounts_connector_instance ON actor_accounts (connector_instance)""",
    ],
    "actor_events": [
        """CREATE TABLE actor_events (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	persona_id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64),
	window_id VARCHAR(64),
	scene_id VARCHAR(64),
	type VARCHAR(64) NOT NULL,
	origin VARCHAR(32) NOT NULL,
	visibility VARCHAR(32) NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	valid_until TIMESTAMP WITH TIME ZONE,
	causation_id VARCHAR(64),
	correlation_id VARCHAR(64),
	revision INTEGER NOT NULL,
	payload JSON NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_actor_events_window_id ON actor_events (window_id)""",
        """CREATE INDEX ix_actor_events_actor_id ON actor_events (actor_id)""",
        """CREATE INDEX ix_actor_events_type ON actor_events (type)""",
        """CREATE INDEX ix_actor_events_actor_time ON actor_events (actor_id, occurred_at)""",
        """CREATE INDEX ix_actor_events_subject_id ON actor_events (subject_id)""",
    ],
    "actor_states": [
        """CREATE TABLE actor_states (
	actor_id VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	persona_id VARCHAR(64) NOT NULL,
	last_event_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (actor_id)
)""",
    ],
    "affect_states": [
        """CREATE TABLE affect_states (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	emotion VARCHAR(32) NOT NULL,
	intensity FLOAT NOT NULL,
	decay_per_hour FLOAT NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	cause TEXT,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_affect_states_actor_id ON affect_states (actor_id)""",
    ],
    "commitments": [
        """CREATE TABLE commitments (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	window_id VARCHAR(64),
	text TEXT NOT NULL,
	due_at TIMESTAMP WITH TIME ZONE NOT NULL,
	timezone VARCHAR(64) NOT NULL,
	state VARCHAR(24) NOT NULL,
	delivery_operation_id VARCHAR(64),
	delivery_state VARCHAR(24),
	source_event_ids JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_commitments_actor_id ON commitments (actor_id)""",
        """CREATE INDEX ix_commitments_due_at ON commitments (due_at)""",
        """CREATE INDEX ix_commitments_subject_id ON commitments (subject_id)""",
        """CREATE INDEX ix_commitments_state ON commitments (state)""",
    ],
    "config_revisions": [
        """CREATE TABLE config_revisions (
	id VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	state VARCHAR(32) NOT NULL,
	payload JSON NOT NULL,
	validation_errors JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE,
	created_by VARCHAR(64),
	note TEXT,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_config_revisions_revision ON config_revisions (revision)""",
        """CREATE INDEX ix_config_revisions_state ON config_revisions (state)""",
    ],
    "consent_rules": [
        """CREATE TABLE consent_rules (
	id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	scope VARCHAR(64) NOT NULL,
	granted BOOLEAN NOT NULL,
	revision INTEGER NOT NULL,
	valid_until TIMESTAMP WITH TIME ZONE,
	note TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_consent_rules_subject_id ON consent_rules (subject_id)""",
    ],
    "delivery_receipts": [
        """CREATE TABLE delivery_receipts (
	id VARCHAR(64) NOT NULL,
	operation_id VARCHAR(64) NOT NULL,
	channel VARCHAR(32) NOT NULL,
	state VARCHAR(32) NOT NULL,
	platform_message_id VARCHAR(128),
	detail TEXT,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_delivery_receipts_operation_id ON delivery_receipts (operation_id)""",
    ],
    "devices": [
        """CREATE TABLE devices (
	id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	name VARCHAR(64) NOT NULL,
	capabilities JSON NOT NULL,
	permissions JSON NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_devices_subject_id ON devices (subject_id)""",
    ],
    "goals": [
        """CREATE TABLE goals (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	title VARCHAR(200) NOT NULL,
	horizon VARCHAR(24) NOT NULL,
	state VARCHAR(24) NOT NULL,
	progress FLOAT NOT NULL,
	note TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_goals_actor_id ON goals (actor_id)""",
    ],
    "handoff_capsules": [
        """CREATE TABLE handoff_capsules (
	id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	source_window_id VARCHAR(64) NOT NULL,
	recent_topic TEXT,
	open_questions JSON NOT NULL,
	short_quote TEXT,
	visibility VARCHAR(32) NOT NULL,
	consent_revision INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_handoff_capsules_subject_id ON handoff_capsules (subject_id)""",
    ],
    "idempotency_keys": [
        """CREATE TABLE idempotency_keys (
	id VARCHAR(64) NOT NULL,
	scope VARCHAR(64) NOT NULL,
	key VARCHAR(256) NOT NULL,
	result_ref VARCHAR(128),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_idempotency_scope_key UNIQUE (scope, key)
)""",
    ],
    "knowledge_items": [
        """CREATE TABLE knowledge_items (
	id VARCHAR(64) NOT NULL,
	topic VARCHAR(200) NOT NULL,
	claim TEXT NOT NULL,
	source_url VARCHAR(1000),
	source_kind VARCHAR(32) NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	valid_until TIMESTAMP WITH TIME ZONE,
	confidence FLOAT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_knowledge_items_topic ON knowledge_items (topic)""",
    ],
    "memories": [
        """CREATE TABLE memories (
	id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	kind VARCHAR(24) NOT NULL,
	text TEXT NOT NULL,
	origin VARCHAR(32) NOT NULL,
	visibility VARCHAR(32) NOT NULL,
	confidence FLOAT NOT NULL,
	source_event_ids JSON NOT NULL,
	valid_from TIMESTAMP WITH TIME ZONE,
	valid_until TIMESTAMP WITH TIME ZONE,
	consent_revision INTEGER NOT NULL,
	retracted BOOLEAN NOT NULL,
	retract_reason TEXT,
	retracted_at TIMESTAMP WITH TIME ZONE,
	supersedes VARCHAR(64),
	superseded_by VARCHAR(64),
	embedding_version VARCHAR(32),
	retention_days INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_memories_subject_kind ON memories (subject_id, kind)""",
        """CREATE INDEX ix_memories_subject_id ON memories (subject_id)""",
        """CREATE INDEX ix_memories_retracted ON memories (retracted)""",
    ],
    "memory_episodes": [
        """CREATE TABLE memory_episodes (
	id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	window_id VARCHAR(64) NOT NULL,
	summary TEXT NOT NULL,
	origin VARCHAR(32) NOT NULL,
	visibility VARCHAR(32) NOT NULL,
	source_event_ids JSON NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	consent_revision INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_memory_episodes_window_id ON memory_episodes (window_id)""",
        """CREATE INDEX ix_memory_episodes_subject_id ON memory_episodes (subject_id)""",
    ],
    "memory_retrieval_logs": [
        """CREATE TABLE memory_retrieval_logs (
	id VARCHAR(64) NOT NULL,
	window_id VARCHAR(64) NOT NULL,
	query TEXT NOT NULL,
	hit_ids JSON NOT NULL,
	filtered_out INTEGER NOT NULL,
	strategy VARCHAR(48) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_memory_retrieval_logs_window_id ON memory_retrieval_logs (window_id)""",
    ],
    "operations": [
        """CREATE TABLE operations (
	id VARCHAR(64) NOT NULL,
	kind VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64),
	state VARCHAR(32) NOT NULL,
	payload JSON NOT NULL,
	detail TEXT,
	error_code VARCHAR(48),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_operations_kind ON operations (kind)""",
        """CREATE INDEX ix_operations_state ON operations (state)""",
        """CREATE INDEX ix_operations_actor_id ON operations (actor_id)""",
    ],
    "outbox_events": [
        """CREATE TABLE outbox_events (
	id VARCHAR(64) NOT NULL,
	topic VARCHAR(64) NOT NULL,
	payload JSON NOT NULL,
	state VARCHAR(24) NOT NULL,
	attempts INTEGER NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	processed_at TIMESTAMP WITH TIME ZONE,
	last_error TEXT,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_outbox_events_state ON outbox_events (state)""",
        """CREATE INDEX ix_outbox_events_topic ON outbox_events (topic)""",
    ],
    "personas": [
        """CREATE TABLE personas (
	id VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	name VARCHAR(64) NOT NULL,
	display_name VARCHAR(64) NOT NULL,
	system_prompt TEXT NOT NULL,
	traits JSON NOT NULL,
	boundaries JSON NOT NULL,
	published BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
    ],
    "proactive_opportunities": [
        """CREATE TABLE proactive_opportunities (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	kind VARCHAR(48) NOT NULL,
	reason TEXT NOT NULL,
	state VARCHAR(24) NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	rejected_reason TEXT,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_proactive_opportunities_subject_id ON proactive_opportunities (subject_id)""",
        """CREATE INDEX ix_proactive_opportunities_state ON proactive_opportunities (state)""",
        """CREATE INDEX ix_proactive_opportunities_actor_id ON proactive_opportunities (actor_id)""",
    ],
    "provider_credentials": [
        """CREATE TABLE provider_credentials (
	id VARCHAR(64) NOT NULL,
	provider_id VARCHAR(64) NOT NULL,
	ciphertext TEXT NOT NULL,
	last4 VARCHAR(8) NOT NULL,
	fingerprint VARCHAR(64) NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE UNIQUE INDEX ix_provider_credentials_provider_id ON provider_credentials (provider_id)""",
    ],
    "relationship_events": [
        """CREATE TABLE relationship_events (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	kind VARCHAR(48) NOT NULL,
	weight FLOAT NOT NULL,
	origin VARCHAR(32) NOT NULL,
	source_event_ids JSON NOT NULL,
	note TEXT,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_relationship_events_actor_id ON relationship_events (actor_id)""",
        """CREATE INDEX ix_relationship_events_subject_id ON relationship_events (subject_id)""",
    ],
    "relationship_states": [
        """CREATE TABLE relationship_states (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	familiarity FLOAT NOT NULL,
	trust FLOAT NOT NULL,
	boundaries JSON NOT NULL,
	last_interaction_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_relationship_states_actor_id ON relationship_states (actor_id)""",
        """CREATE UNIQUE INDEX ix_relationship_states_subject_id ON relationship_states (subject_id)""",
    ],
    "speech_segments": [
        """CREATE TABLE speech_segments (
	id VARCHAR(64) NOT NULL,
	window_id VARCHAR(64) NOT NULL,
	turn_id VARCHAR(64),
	kind VARCHAR(16) NOT NULL,
	text TEXT NOT NULL,
	audio_ref VARCHAR(256),
	model_label VARCHAR(128),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_speech_segments_window_id ON speech_segments (window_id)""",
    ],
    "subjects": [
        """CREATE TABLE subjects (
	id VARCHAR(64) NOT NULL,
	display_name VARCHAR(128) NOT NULL,
	proof_level VARCHAR(32) NOT NULL,
	merged_into VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
    ],
    "users": [
        """CREATE TABLE users (
	id VARCHAR(64) NOT NULL,
	username VARCHAR(128) NOT NULL,
	password_hash VARCHAR(256) NOT NULL,
	role VARCHAR(32) NOT NULL,
	disabled BOOLEAN NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_users_subject_id ON users (subject_id)""",
        """CREATE UNIQUE INDEX ix_users_username ON users (username)""",
    ],
    "window_contexts": [
        """CREATE TABLE window_contexts (
	id VARCHAR(64) NOT NULL,
	window_id VARCHAR(64) NOT NULL,
	topic TEXT,
	open_questions JSON NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE UNIQUE INDEX ix_window_contexts_window_id ON window_contexts (window_id)""",
    ],
    "windows": [
        """CREATE TABLE windows (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	persona_id VARCHAR(64) NOT NULL,
	subject_id VARCHAR(64) NOT NULL,
	title VARCHAR(128) NOT NULL,
	channel VARCHAR(32) NOT NULL,
	visibility VARCHAR(32) NOT NULL,
	archived BOOLEAN NOT NULL,
	message_count INTEGER NOT NULL,
	last_message_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_windows_subject_id ON windows (subject_id)""",
        """CREATE INDEX ix_windows_actor_id ON windows (actor_id)""",
    ],
    "world_places": [
        """CREATE TABLE world_places (
	id VARCHAR(64) NOT NULL,
	actor_id VARCHAR(64) NOT NULL,
	name VARCHAR(64) NOT NULL,
	description TEXT,
	items JSON NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
)""",
        """CREATE INDEX ix_world_places_actor_id ON world_places (actor_id)""",
    ],
    "auth_sessions": [
        """CREATE TABLE auth_sessions (
	id VARCHAR(64) NOT NULL,
	user_id VARCHAR(64) NOT NULL,
	token_hash VARCHAR(128) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	user_agent VARCHAR(256),
	PRIMARY KEY (id),
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)""",
        """CREATE UNIQUE INDEX ix_auth_sessions_token_hash ON auth_sessions (token_hash)""",
        """CREATE INDEX ix_auth_sessions_user_id ON auth_sessions (user_id)""",
    ],
    "messages": [
        """CREATE TABLE messages (
	id VARCHAR(64) NOT NULL,
	window_id VARCHAR(64) NOT NULL,
	turn_id VARCHAR(64),
	role VARCHAR(16) NOT NULL,
	text TEXT NOT NULL,
	attempt INTEGER NOT NULL,
	state VARCHAR(32) NOT NULL,
	sequence INTEGER NOT NULL,
	origin VARCHAR(32) NOT NULL,
	error_code VARCHAR(48),
	model_label VARCHAR(128),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(window_id) REFERENCES windows (id) ON DELETE CASCADE
)""",
        """CREATE INDEX ix_messages_window_seq ON messages (window_id, sequence)""",
        """CREATE INDEX ix_messages_turn_id ON messages (turn_id)""",
        """CREATE INDEX ix_messages_window_id ON messages (window_id)""",
    ],
    "turns": [
        """CREATE TABLE turns (
	id VARCHAR(64) NOT NULL,
	window_id VARCHAR(64) NOT NULL,
	client_turn_id VARCHAR(128) NOT NULL,
	state VARCHAR(32) NOT NULL,
	model_label VARCHAR(128),
	config_revision INTEGER NOT NULL,
	error_code VARCHAR(48),
	error_detail TEXT,
	attempt INTEGER NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_turn_client UNIQUE (window_id, client_turn_id),
	FOREIGN KEY(window_id) REFERENCES windows (id) ON DELETE CASCADE
)""",
        """CREATE INDEX ix_turns_state ON turns (state)""",
        """CREATE INDEX ix_turns_window_id ON turns (window_id)""",
    ],
}
