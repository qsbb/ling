import os

import pytest

# These fixtures run the real mounted application on a fresh PostgreSQL schema.
from tests.integration.conftest import logged_in, web_env  # noqa: F401


@pytest.fixture(autouse=True)
def clean_app_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    keep = {"LING_TEST_DATABASE_URL", "LING_DATABASE_URL", "LING_TEST_MODE", "LING_RECORD_WP00"}
    for name in tuple(os.environ):
        if name.startswith("LING_") and name not in keep:
            monkeypatch.delenv(name)
