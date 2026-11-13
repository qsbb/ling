"""Execute the real pre-projection migration chain in a local isolated schema.

The test uses the shared ling_test server and a disposable schema. It executes
migration upgrade bodies through Alembic Operations, including 0070, and checks
the stored old message contents before and after downgrade/upgrade.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import TEST_DB_URL

pytestmark = pytest.mark.w0_web

REVISION = "0070_voice_reply_projection"
PARENT = "0069_asr_jobs"


def _scripts() -> ScriptDirectory:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    return ScriptDirectory.from_config(config)


def _migrate(connection, revision: str, *, downgrade: bool = False) -> None:
    scripts = _scripts()
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        if downgrade:
            scripts.get_revision(revision).module.downgrade()
        elif revision == REVISION:
            scripts.get_revision(revision).module.upgrade()
        else:
            for item in reversed(list(scripts.walk_revisions(base="base", head=revision))):
                item.module.upgrade()


def _table(connection, name: str) -> sa.Table:
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


@dataclass
class _LegacySnapshot:
    messages: dict[str, tuple[str, str | None]]
    message_rows: dict[str, dict[str, object]]
    opportunity: dict[str, object]


def _seed_legacy(connection) -> _LegacySnapshot:
    now = datetime.now(UTC)
    windows = _table(connection, "windows")
    connection.execute(windows.insert(), {
        "id": "window_voice_projection_old", "actor_id": "actor_fixture",
        "persona_id": "persona_fixture", "subject_id": "subject_fixture",
        "title": "旧语音消息验收", "channel": "web", "visibility": "subject_private",
        "archived": False, "message_count": 0, "created_at": now, "updated_at": now,
    })
    opportunities = _table(connection, "proactive_opportunities")
    opportunity = {
        "id": "opp_voice_projection_old", "actor_id": "actor_fixture",
        "subject_id": "subject_fixture", "kind": "share",
        "reason": "旧机会记录", "state": "candidate",
        "expires_at": now + timedelta(days=1), "created_at": now,
        "sent_at": None, "rejected_reason": None, "draft_text": "迁移前草稿",
        "source_event_id": "event_voice_projection_old",
        "dedupe_key": "dedupe_voice_projection_old",
        "reason_code": "share_update", "not_before": now + timedelta(minutes=5),
        "best_until": now + timedelta(hours=12), "visibility": "subject_private",
        "reply_expected": True, "channel_hint": "web",
        "delivery_operation_id": None, "delivery_state": None,
    }
    connection.execute(opportunities.insert(), opportunity)
    messages = _table(connection, "messages")
    blobs = _table(connection, "media_assets")
    grants = _table(connection, "media_asset_grants")
    cases = {
        "plain": ("actor", None, True, False),
        "logical": ("actor", "tts_output", True, False),
        "legacy": ("actor", None, True, True),
        "user_input": ("user", "asr_input", True, True),
        "unfinished": ("actor", "tts_output", False, True),
        "reference": ("actor", "tts_reference", True, False),
    }
    expected: dict[str, tuple[str, str | None]] = {}
    message_rows: dict[str, dict[str, object]] = {}
    for sequence, (case, (role, purpose, ready, legacy_link)) in enumerate(cases.items(), 1):
        message_id = f"msg_old_voice_{case}"
        canonical = f"迁移原文保持-{case}"
        message_row = {
            "id": message_id, "window_id": "window_voice_projection_old", "role": role,
            "text": canonical, "state": "completed", "sequence": sequence,
            "origin": "model_inference" if role == "actor" else "user_statement",
            "attempt": 1, "attention_state": "seen", "created_at": now,
        }
        connection.execute(messages.insert(), message_row)
        message_rows[message_id] = message_row
        if case == "plain":
            expected[message_id] = (canonical, None)
            continue
        digest = hashlib.sha256(case.encode()).hexdigest()
        connection.execute(blobs.insert(), {
            "digest": digest, "actor_id": "actor_fixture", "window_id": "window_voice_projection_old",
            "subject_id": "subject_fixture", "kind": "audio", "format": "wav",
            "mime_type": "audio/wav", "relative_path": f"generated_media/{case}.wav",
            "size_bytes": 3200, "sample_rate": 16000, "duration_ms": 100,
            "message_id": message_id if legacy_link else None,
            "producer": "fixture", "ready_at": now if ready else None,
            "created_at": now, "updated_at": now,
        })
        asset_id = None
        if purpose is not None:
            asset_id = f"asset_old_voice_{case}"
            connection.execute(grants.insert(), {
                "asset_id": asset_id, "blob_digest": digest, "actor_id": "actor_fixture",
                "window_id": "window_voice_projection_old", "subject_id": "subject_fixture",
                "purpose": purpose, "message_id": message_id,
                "created_at": now, "updated_at": now,
            })
        if case == "logical":
            # A newer ready output must win even when it uses a different blob.
            newer = hashlib.sha256(b"newer logical output").hexdigest()
            connection.execute(blobs.insert(), {
                "digest": newer, "actor_id": "actor_fixture", "window_id": "window_voice_projection_old",
                "subject_id": "subject_fixture", "kind": "audio", "format": "wav",
                "mime_type": "audio/wav", "relative_path": "generated_media/newer.wav",
                "size_bytes": 3200, "sample_rate": 16000, "duration_ms": 100,
                "producer": "fixture", "ready_at": now,
                "created_at": now + timedelta(seconds=1), "updated_at": now,
            })
            asset_id = "asset_old_voice_latest"
            connection.execute(grants.insert(), {
                "asset_id": asset_id, "blob_digest": newer, "actor_id": "actor_fixture",
                "window_id": "window_voice_projection_old", "subject_id": "subject_fixture",
                "purpose": "tts_output", "message_id": message_id,
                "created_at": now + timedelta(seconds=1), "updated_at": now,
            })
        expected_asset = asset_id if case == "logical" else digest if case == "legacy" else None
        expected[message_id] = (canonical, expected_asset)
    return _LegacySnapshot(
        messages=expected,
        message_rows=message_rows,
        opportunity=opportunity,
    )


def _assert_legacy_rows(connection, expected: _LegacySnapshot) -> None:
    message_columns = tuple(next(iter(expected.message_rows.values())))
    rows = connection.execute(sa.text(
        "SELECT " + ", ".join(message_columns) + " FROM messages ORDER BY id"
    )).mappings().all()
    actual_messages = {
        row["id"]: {column: row[column] for column in message_columns}
        for row in rows
    }
    assert actual_messages == expected.message_rows

    opportunity_columns = tuple(expected.opportunity)
    opportunity = connection.execute(sa.text(
        "SELECT " + ", ".join(opportunity_columns)
        + " FROM proactive_opportunities WHERE id='opp_voice_projection_old'"
    )).mappings().one()
    assert {column: opportunity[column] for column in opportunity_columns} == expected.opportunity


def _assert_upgraded(connection, expected: _LegacySnapshot) -> None:
    _assert_legacy_rows(connection, expected)
    rows = connection.execute(sa.text(
        "SELECT id, text, output_modality, audio_asset_id, speech_state, speech_delivery_state "
        "FROM messages ORDER BY id"
    )).mappings().all()
    assert len(rows) == len(expected.messages)
    for row in rows:
        text, asset_id = expected.messages[row["id"]]
        assert row["text"] == text
        assert row["output_modality"] == ("voice" if asset_id else "text"), dict(row)
        assert row["audio_asset_id"] == asset_id, dict(row)
        assert row["speech_state"] == ("ready" if asset_id else "none"), dict(row)
        assert row["speech_delivery_state"] is None
    connection.execute(sa.text(
        "INSERT INTO messages (id, window_id, role, text, sequence, state, origin, attempt, "
        "attention_state, created_at) VALUES "
        "('msg_new_text_default', 'window_voice_projection_old', 'actor', '普通文字默认保持', "
        "100, 'completed', 'model_inference', 1, 'seen', now())"
    ))
    default = connection.execute(sa.text(
        "SELECT output_modality, audio_asset_id, speech_state, speech_delivery_state "
        "FROM messages WHERE id='msg_new_text_default'"
    )).one()
    assert tuple(default) == ("text", None, "none", None)
    connection.execute(sa.text("DELETE FROM messages WHERE id='msg_new_text_default'"))

    tables = set(sa.inspect(connection).get_table_names())
    assert "speech_synthesis_jobs" in tables
    job_columns = {
        column["name"]
        for column in sa.inspect(connection).get_columns("speech_synthesis_jobs")
    }
    assert job_columns == {
        "id", "message_id", "provider_id", "model_id", "config_revision",
        "voice_config_hash", "text_hash", "input_characters", "state",
        "usage_source", "created_at",
    }
    job_indexes = {
        index["name"] for index in sa.inspect(connection).get_indexes("speech_synthesis_jobs")
    }
    assert "ix_speech_synthesis_jobs_provider_id" in job_indexes

    opportunity = connection.execute(sa.text(
        "SELECT output_modality FROM proactive_opportunities "
        "WHERE id='opp_voice_projection_old'"
    )).scalar_one()
    assert opportunity == "text"
    connection.execute(sa.text(
        "UPDATE proactive_opportunities SET output_modality='voice' "
        "WHERE id='opp_voice_projection_old'"
    ))
    assert connection.execute(sa.text(
        "SELECT output_modality FROM proactive_opportunities "
        "WHERE id='opp_voice_projection_old'"
    )).scalar_one() == "voice"
    connection.execute(sa.text(
        "UPDATE proactive_opportunities SET output_modality='text' "
        "WHERE id='opp_voice_projection_old'"
    ))

    connection.execute(sa.text(
        "INSERT INTO speech_synthesis_jobs "
        "(id, message_id, provider_id, model_id, config_revision, voice_config_hash, "
        "text_hash, input_characters, state, usage_source, created_at) VALUES "
        "('job_voice_projection', 'msg_old_voice_logical', 'tts_fixture', 'speech_fixture', "
        "3, NULL, 'text-hash', 12, 'succeeded', 'local', now())"
    ))
    stored_job = connection.execute(sa.text(
        "SELECT message_id, provider_id, model_id, input_characters, state, usage_source "
        "FROM speech_synthesis_jobs WHERE id='job_voice_projection'"
    )).one()
    assert tuple(stored_job) == (
        "msg_old_voice_logical", "tts_fixture", "speech_fixture", 12, "succeeded", "local",
    )


async def _exercise_migration() -> None:
    schema = f"voice_mig_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DB_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as connection:
        await connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(TEST_DB_URL, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda conn: _migrate(conn, PARENT))
            expected = await connection.run_sync(_seed_legacy)
            await connection.run_sync(lambda conn: _migrate(conn, REVISION))
            await connection.run_sync(lambda conn: _assert_upgraded(conn, expected))
            await connection.run_sync(lambda conn: _migrate(conn, REVISION, downgrade=True))
            columns = (await connection.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=:schema AND table_name='messages'"
            ), {"schema": schema})).scalars().all()
            assert not {"output_modality", "audio_asset_id", "speech_state", "speech_delivery_state"}.intersection(columns)
            opportunity_columns = (await connection.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=:schema AND table_name='proactive_opportunities'"
            ), {"schema": schema})).scalars().all()
            assert "output_modality" not in opportunity_columns
            tables = set((await connection.execute(sa.text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=:schema"
            ), {"schema": schema})).scalars().all())
            assert "speech_synthesis_jobs" not in tables
            await connection.run_sync(lambda conn: _assert_legacy_rows(conn, expected))
            await connection.run_sync(lambda conn: _migrate(conn, REVISION))
            await connection.run_sync(lambda conn: _assert_upgraded(conn, expected))
    finally:
        await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


def test_projection_upgrade_preserves_old_user_and_plain_text_with_audio_backfill() -> None:
    asyncio.run(_exercise_migration())
