"""Seed isolated W0 PostgreSQL data for real relationship-page browser checks."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import asyncpg
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ling.infra.models import MemoryRow
from ling.infra.security import hash_password


async def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: seed_relationship_acceptance.py HOST PORT DATABASE")

    host, port, database = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    now = datetime.now(UTC)
    subjects = [
        (
            f"e2e-page-{index:04d}",
            f"Acceptance page {index:04d}",
            now + timedelta(microseconds=index),
        )
        for index in range(120)
    ]
    subjects.extend(
        [
            ("e2e-merge-source", "Acceptance merge source", now + timedelta(seconds=1)),
            ("e2e-merge-target", "Acceptance merge target", now + timedelta(seconds=2)),
            ("e2e-member", "Acceptance member", now + timedelta(seconds=3)),
        ]
    )

    connection = await asyncpg.connect(
        host=host,
        port=port,
        user="ling",
        database=database,
    )
    try:
        async with connection.transaction():
            await connection.executemany(
                """
                INSERT INTO subjects (
                    id, display_name, proof_level, trust_level, merged_into,
                    forget_generation, created_at, updated_at
                ) VALUES ($1, $2, 'password', 'known', NULL, 0, $3, $3)
                """,
                subjects,
            )
            await connection.execute(
                """
                INSERT INTO users (
                    id, username, password_hash, role, disabled,
                    must_change_password, subject_id, created_at, updated_at
                ) VALUES ($1, $2, $3, 'member', FALSE, FALSE, $4, $5, $5)
                """,
                "user-e2e-member",
                "acceptance-member",
                hash_password("e2e-member-test-password"),
                "e2e-member",
                now + timedelta(seconds=4),
            )
    finally:
        await connection.close()

    engine = create_async_engine(
        f"postgresql+asyncpg://ling@{host}:{port}/{database}"
    )
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            session.add(
                MemoryRow(
                    id="memory-e2e-private-sentinel",
                    subject_id="e2e-merge-target",
                    kind="assertion",
                    text="Private acceptance sentinel",
                    origin="user_statement",
                    visibility="subject_private",
                    source_visibility="subject_private",
                    created_at=now,
                )
            )
            await session.commit()
    finally:
        await engine.dispose()

    print("seeded relationship subjects=123 member=acceptance-member private_memory=1")


if __name__ == "__main__":
    asyncio.run(main())
