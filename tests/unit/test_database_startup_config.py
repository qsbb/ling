"""启动必须明确数据库来源，缺省时保护历史本地文件。"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ling.config import Settings
from ling.infra.db import build_engine

ROOT = Path(__file__).resolve().parents[2]


def _environment_without_database() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LING_DATABASE_URL", None)
    env.pop("LING_PROJECT_ROOT", None)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT)))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _sqlite_sentinel(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE user_data (content TEXT NOT NULL)")
        connection.execute("INSERT INTO user_data VALUES ('keep this data')")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("value", [None, "", "  \t"])
def test_database_url_is_required_when_settings_are_created(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("LING_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("LING_DATABASE_URL", value)

    with pytest.raises(RuntimeError, match="LING_DATABASE_URL") as error:
        Settings()
    assert "PostgreSQL" in str(error.value)
    assert "SQLite" in str(error.value)


def test_explicit_empty_settings_are_rejected_before_engine_creation(tmp_path):
    settings = Settings(database_url="", data_dir=tmp_path / "data")
    with pytest.raises(RuntimeError, match="LING_DATABASE_URL"):
        build_engine(settings)
    assert not settings.data_dir.exists()


def test_missing_configuration_preserves_legacy_database(tmp_path, monkeypatch):
    legacy = tmp_path / ".data" / "ling.db"
    before = _sqlite_sentinel(legacy)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LING_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="LING_DATABASE_URL"):
        Settings()

    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("SELECT content FROM user_data").fetchone() == ("keep this data",)


def test_invalid_database_url_reports_no_credentials(monkeypatch):
    sensitive = "invalid-fake-password-example"
    monkeypatch.setenv("LING_DATABASE_URL", f"{sensitive} without a scheme")

    with pytest.raises(RuntimeError, match="LING_DATABASE_URL") as error:
        Settings()
    assert sensitive not in str(error.value)


def test_explicit_postgres_configuration_is_preserved(monkeypatch):
    monkeypatch.delenv("LING_DATABASE_URL", raising=False)
    postgres = "postgresql+asyncpg://deploy@192.0.2.10:5432/ling"
    assert Settings(database_url=postgres).database_url == postgres


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///old-explicit.db", "mysql+asyncmy://host/db"])
def test_explicit_non_postgres_database_is_rejected_before_engine_creation(tmp_path, monkeypatch, url):
    import ling.infra.db as db

    monkeypatch.setattr(db, "create_async_engine", lambda *_args, **_kwargs: pytest.fail("连接不应创建"))
    settings = Settings(database_url=url, data_dir=tmp_path / "data")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        build_engine(settings)
    assert not settings.data_dir.exists()


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-c", "import ling.app"],
        [sys.executable, "-m", "ling.worker"],
        [sys.executable, "-m", "ling.migrate", "upgrade"],
        [sys.executable, "-m", "ling.migrate", "stamp"],
        [sys.executable, "-m", "ling.migrate", "preflight"],
    ],
    ids=["api", "worker", "migration-upgrade", "migration-stamp", "migration-preflight"],
)
def test_startup_entrypoints_require_configuration_without_touching_old_database(tmp_path, command):
    legacy = tmp_path / ".data" / "ling.db"
    before = _sqlite_sentinel(legacy)
    env = _environment_without_database()
    env["LING_DATA_DIR"] = str(tmp_path / ".data")

    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)

    assert result.returncode != 0
    assert "LING_DATABASE_URL" in result.stderr
    assert "PostgreSQL" in result.stderr
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


def test_alembic_offline_requires_a_database_source():
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=ROOT,
        env=_environment_without_database(),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert "LING_DATABASE_URL" in result.stderr
    assert "PostgreSQL" in result.stderr
    assert "CREATE TABLE" not in result.stdout


def test_development_script_requires_explicit_database(tmp_path):
    env = _environment_without_database()
    result = subprocess.run(
        ["bash", "-c", 'source "$1"', "bash", str(ROOT / "scripts/dev_env.sh")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "LING_DATABASE_URL" in result.stderr
    assert "PostgreSQL" in result.stderr


def test_development_script_loads_local_database_configuration_without_printing_password(tmp_path):
    secret = "fake-config-password"
    url = f"postgresql+asyncpg://deploy:{secret}@192.0.2.10:5432/ling"
    local = tmp_path / ".data" / "local_secrets.env"
    local.parent.mkdir()
    local.write_text(f"LING_DATABASE_URL='{url}'\n", encoding="utf-8")
    env = _environment_without_database()
    env["EXPECTED_DATABASE_URL"] = url
    result = subprocess.run(
        [
            "bash", "-c", 'source "$1" && test "$LING_DATABASE_URL" = "$EXPECTED_DATABASE_URL"',
            "bash", str(ROOT / "scripts/dev_env.sh"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert secret not in result.stdout
    assert secret not in result.stderr


def test_alembic_postgres_guard_preserves_old_database_before_first_revision(tmp_path):
    legacy = tmp_path / "old.db"
    _sqlite_sentinel(legacy)
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE windows (id VARCHAR(64) PRIMARY KEY)")
        connection.execute("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)")
        connection.execute("INSERT INTO alembic_version VALUES ('0072_proactive_gate_snapshot')")
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()
    env = _environment_without_database()
    env["LING_DATABASE_URL"] = f"sqlite+aiosqlite:///{legacy}"

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode != 0
    assert "LING_DATABASE_URL" in result.stderr
    assert "PostgreSQL" in result.stderr
    assert "Running upgrade" not in result.stderr
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before
    with sqlite3.connect(legacy) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0072_proactive_gate_snapshot",
        )
        assert connection.execute("SELECT content FROM user_data").fetchone() == ("keep this data",)


def test_alembic_accepts_an_explicit_configured_database_without_environment_url():
    code = "\n".join(
        [
            "from alembic import command",
            "from alembic.config import Config",
            f"config = Config({str(ROOT / 'alembic.ini')!r})",
            f"config.set_main_option('script_location', {str(ROOT / 'migrations')!r})",
            "config.set_main_option('sqlalchemy.url', 'postgresql+asyncpg://deploy@192.0.2.10/db')",
            "command.upgrade(config, 'base', sql=True)",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=_environment_without_database(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "BEGIN" in result.stdout


def test_development_launcher_rejects_missing_database_before_starting_processes(tmp_path):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/dev_up.sh"), "start"],
        cwd=tmp_path,
        env=_environment_without_database(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "LING_DATABASE_URL" in result.stderr
    assert "已启动" not in result.stdout


@pytest.mark.parametrize("configured", [False, True], ids=["missing", "non-postgresql"])
def test_container_entrypoint_rejects_invalid_configuration_before_migration(tmp_path, configured):
    env = _environment_without_database()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    legacy = tmp_path / "old.db"
    before = _sqlite_sentinel(legacy)
    if configured:
        env["LING_DATABASE_URL"] = f"sqlite+aiosqlite:///{legacy}"
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/entrypoint.sh"), "api"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "LING_DATABASE_URL" in result.stderr
    assert "PostgreSQL" in result.stderr
    assert "执行迁移" not in result.stdout
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


def test_alembic_rejects_empty_environment_even_with_configured_fallback():
    code = "\n".join(
        [
            "from alembic import command",
            "from alembic.config import Config",
            f"config = Config({str(ROOT / 'alembic.ini')!r})",
            f"config.set_main_option('script_location', {str(ROOT / 'migrations')!r})",
            "config.set_main_option('sqlalchemy.url', 'postgresql+asyncpg://deploy@192.0.2.10/db')",
            "command.upgrade(config, 'base', sql=True)",
        ]
    )
    env = _environment_without_database()
    env["LING_DATABASE_URL"] = "  "
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert "LING_DATABASE_URL" in result.stderr
    assert "BEGIN" not in result.stdout


@pytest.mark.parametrize("scheme", ["postgresql", "postgresql+asyncpg"])
def test_development_script_accepts_shell_database_without_printing_password(tmp_path, scheme):
    secret = "fake-shell-password"
    env = _environment_without_database()
    env["LING_DATABASE_URL"] = f"{scheme}://deploy:{secret}@192.0.2.10:5432/ling"
    result = subprocess.run(
        ["bash", "-c", 'source "$1"', "bash", str(ROOT / "scripts/dev_env.sh")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert secret not in result.stdout
    assert secret not in result.stderr


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-c", "import ling.app"],
        [sys.executable, "-m", "ling.worker"],
        [sys.executable, "-m", "ling.migrate", "upgrade"],
        [sys.executable, "-m", "ling.migrate", "stamp"],
        [sys.executable, "-m", "ling.migrate", "preflight"],
    ],
    ids=["api", "worker", "migration-upgrade", "migration-stamp", "migration-preflight"],
)
def test_explicit_old_database_is_rejected_without_touching_it(tmp_path, command):
    legacy = tmp_path / ".data" / "ling.db"
    before = _sqlite_sentinel(legacy)
    env = _environment_without_database()
    env["LING_DATA_DIR"] = str(tmp_path / ".data")
    env["LING_DATABASE_URL"] = f"sqlite+aiosqlite:///{legacy}"

    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)

    assert result.returncode != 0
    assert "LING_DATABASE_URL" in result.stderr
    assert "PostgreSQL" in result.stderr
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


def test_alembic_rejects_non_postgres_configured_without_environment_url(tmp_path):
    legacy = tmp_path / "old.db"
    before = _sqlite_sentinel(legacy)
    url = f"sqlite+aiosqlite:///{legacy}"
    code = "\n".join(
        [
            "from alembic import command",
            "from alembic.config import Config",
            f"config = Config({str(ROOT / 'alembic.ini')!r})",
            f"config.set_main_option('script_location', {str(ROOT / 'migrations')!r})",
            f"config.set_main_option('sqlalchemy.url', {url!r})",
            "command.upgrade(config, 'head')",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=_environment_without_database(),
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert "PostgreSQL" in result.stderr
    assert "Running upgrade" not in result.stderr
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


def test_development_scripts_reject_explicit_non_postgres_before_start(tmp_path):
    legacy = tmp_path / "old.db"
    before = _sqlite_sentinel(legacy)
    env = _environment_without_database()
    env["LING_DATABASE_URL"] = f"sqlite+aiosqlite:///{legacy}"
    commands = [
        ["bash", "-c", 'source "$1"', "bash", str(ROOT / "scripts/dev_env.sh")],
        ["bash", str(ROOT / "scripts/dev_up.sh"), "start"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "LING_DATABASE_URL" in result.stderr
        assert "PostgreSQL" in result.stderr
        assert "已启动" not in result.stdout
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


def test_alembic_environment_url_takes_precedence_over_config(tmp_path):
    legacy = tmp_path / "old.db"
    before = _sqlite_sentinel(legacy)
    legacy_url = f"sqlite+aiosqlite:///{legacy}"
    postgres = "postgresql+asyncpg://deploy@192.0.2.10/db"
    code = "\n".join(
        [
            "import os",
            "from alembic import command",
            "from alembic.config import Config",
            f"config = Config({str(ROOT / 'alembic.ini')!r})",
            f"config.set_main_option('script_location', {str(ROOT / 'migrations')!r})",
            "config.set_main_option('sqlalchemy.url', os.environ['CONFIG_DATABASE_URL'])",
            "command.upgrade(config, 'base', sql=True)",
        ]
    )
    env = _environment_without_database()
    env["LING_DATABASE_URL"] = legacy_url
    env["CONFIG_DATABASE_URL"] = postgres
    rejected = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=20,
    )
    assert rejected.returncode != 0
    assert "PostgreSQL" in rejected.stderr
    assert "BEGIN" not in rejected.stdout

    env["LING_DATABASE_URL"] = postgres
    env["CONFIG_DATABASE_URL"] = legacy_url
    accepted = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=20,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "BEGIN" in accepted.stdout
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("configured", [False, True], ids=["missing", "postgres"])
def test_development_script_can_be_sourced_from_zsh(tmp_path, configured):
    env = _environment_without_database()
    if configured:
        env["LING_DATABASE_URL"] = "postgresql+asyncpg://deploy@192.0.2.10/db"
    result = subprocess.run(
        ["/bin/zsh", "-c", 'source "$1"', "zsh", str(ROOT / "scripts/dev_env.sh")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )
    if configured:
        assert result.returncode == 0
    else:
        assert result.returncode != 0
        assert "LING_DATABASE_URL" in result.stderr


def test_plain_postgres_url_is_normalized_for_the_async_driver():
    from ling.config import require_database_url

    plain = "postgresql://deploy:fake-password@192.0.2.10:5432/ling"
    expected = "postgresql+asyncpg://deploy:fake-password@192.0.2.10:5432/ling"
    assert require_database_url(plain) == expected
    assert require_database_url(expected) == expected


@pytest.mark.parametrize("driver", ["psycopg", "unknown_driver"])
@pytest.mark.parametrize("entrypoint", ["settings", "engine", "alembic", "container", "dev_env", "dev_up"])
def test_unsupported_postgres_driver_is_rejected_before_startup(tmp_path, monkeypatch, driver, entrypoint):
    secret = "fake-unsupported-driver-password"
    url = f"postgresql+{driver}://deploy:{secret}@192.0.2.10:5432/ling"
    if entrypoint == "settings":
        monkeypatch.setenv("LING_DATABASE_URL", url)
        with pytest.raises(RuntimeError) as error:
            Settings()
        message = str(error.value)
    elif entrypoint == "engine":
        import ling.infra.db as db

        monkeypatch.setattr(db, "create_async_engine", lambda *_args, **_kwargs: pytest.fail("不应创建引擎"))
        with pytest.raises(RuntimeError) as error:
            build_engine(Settings(database_url=url))
        message = str(error.value)
    else:
        commands = {
            "alembic": [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
            "container": ["bash", str(ROOT / "deploy/entrypoint.sh"), "api"],
            "dev_env": ["bash", "-c", 'source "$1"', "bash", str(ROOT / "scripts/dev_env.sh")],
            "dev_up": ["bash", str(ROOT / "scripts/dev_up.sh"), "start"],
        }
        env = _environment_without_database()
        env["LING_DATABASE_URL"] = url
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        result = subprocess.run(
            commands[entrypoint], cwd=ROOT if entrypoint == "alembic" else tmp_path,
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode != 0
        assert "BEGIN" not in result.stdout
        assert "执行迁移" not in result.stdout
        assert "已启动" not in result.stdout
        message = result.stdout + result.stderr
    assert "LING_DATABASE_URL" in message
    assert "postgresql+asyncpg://" in message
    assert secret not in message
