"""Cover the retired setting at both service entrypoints and in Compose."""

import logging
from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from ling import app as api_module
from ling import worker
from ling.config import Settings, reset_settings_cache

ROOT = Path(__file__).resolve().parents[2]
OLD_KEY = "LING_OBSERVE_WINDOW_MESSAGES"


def test_retired_window_setting_is_no_longer_a_field() -> None:
    assert "observe_window_messages" not in {field.name for field in fields(Settings)}


def test_unused_app_fingerprint_is_removed() -> None:
    assert "_fingerprint" not in vars(api_module)


@pytest.mark.parametrize("role", ["api", "worker"])
@pytest.mark.parametrize(
    "value", [None, "", "synthetic-not-an-integer"], ids=["absent", "blank", "invalid"]
)
async def test_deprecated_env_warns_at_startup_only_if_present(
    role: str, value: str | None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    if value is None:
        monkeypatch.delenv(OLD_KEY, raising=False)
    else:
        monkeypatch.setenv(OLD_KEY, value)
    monkeypatch.setenv("LING_PORT", "6543")

    class DatabaseInitializationReached(Exception):
        pass

    def stop_at_database(settings: Settings) -> None:
        assert settings.port == 6543
        raise DatabaseInitializationReached

    reset_settings_cache()
    try:
        # Neither test opens a database connection; the real startup must reach it.
        with caplog.at_level(logging.INFO), pytest.raises(DatabaseInitializationReached):
            if role == "api":
                monkeypatch.setattr(api_module, "init_engine", stop_at_database)
                app = api_module.create_app()
                async with app.router.lifespan_context(app):
                    pytest.fail("API did not initialize its database")
            else:
                monkeypatch.setattr(worker, "init_engine", stop_at_database)
                await worker.run_forever()
    finally:
        reset_settings_cache()

    records = [record for record in caplog.records if OLD_KEY in record.getMessage()]
    if value is None:
        assert not records
    else:
        assert len(records) == 1
        (record,) = records
        assert record.levelno == logging.WARNING
        assert "不再生效" in record.getMessage()
        if value:
            assert value not in caplog.text


@pytest.mark.parametrize("compose", ["compose.yaml", "compose.image.yaml"])
def test_compose_can_forward_the_old_key_without_a_default(compose: str) -> None:
    model = yaml.safe_load((ROOT / "deploy" / compose).read_text(encoding="utf-8"))
    for service in ("api", "worker"):
        # Compose null entries resolve from the environment, but do not invent an empty default.
        assert model["services"][service]["environment"][OLD_KEY] is None
