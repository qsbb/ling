"""Check registry completeness without incidental test or application imports."""

import os
import subprocess
import sys
from pathlib import Path


def test_registry_loads_tables_in_a_fresh_interpreter() -> None:
    src = Path(__file__).resolve().parents[2] / "src"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from ling.infra.db import Base
from ling.infra.orm_registry import import_all_models

assert not Base.metadata.tables
import_all_models()
expected = {
    "windows",
    "asr_jobs",
    "asr_call_reservations",
    "conversation_policy_audit",
    "media_assets",
    "media_asset_grants",
    "media_asset_leases",
    "media_play_events",
    "proactive_quota_reservations",
}
registered = set(Base.metadata.tables)
assert expected <= registered, sorted(expected - registered)
import_all_models()
assert set(Base.metadata.tables) == registered
""",
        ],
        env={**os.environ, "PYTHONPATH": str(src)},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
