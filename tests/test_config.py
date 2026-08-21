"""Storage resolution is one env var deciding four things — so it gets tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from wa_mcp.config import Settings, resolve_storage


def test_unset_is_a_working_install(tmp_path: Path):
    """The headline promise: no configuration at all still works."""
    s = resolve_storage("", tmp_path)
    assert s.backend == "sqlite"
    assert s.app_url == f"sqlite+aiosqlite:///{tmp_path / 'app.db'}"
    assert s.session_dsn == str(tmp_path / "session.db")
    assert s.session_is_file is True


def test_postgres_takes_the_session_with_it(tmp_path: Path):
    """A Postgres user should need no volume — the login goes in Postgres too."""
    s = resolve_storage("postgresql://u:p@db:5432/wa", tmp_path)
    assert s.backend == "postgres"
    assert s.app_url.startswith("postgresql+asyncpg://")
    assert s.session_is_file is False
    # goneonize selects the driver by literal prefix; the SQLAlchemy form would
    # silently fall through to SQLite.
    assert s.session_dsn.startswith("postgres://")


def test_postgres_session_dsn_forces_sslmode(tmp_path: Path):
    """Unset sslmode makes lib/pq hang with no error and no tables."""
    s = resolve_storage("postgresql://u:p@db:5432/wa", tmp_path)
    assert "sslmode=disable" in s.session_dsn

    s2 = resolve_storage("postgresql://u:p@db:5432/wa?sslmode=require", tmp_path)
    assert s2.session_dsn.count("sslmode=") == 1


def test_mongo_leaves_the_login_on_disk(tmp_path: Path):
    """whatsmeow has no Mongo backend, so the session must stay a file."""
    s = resolve_storage("mongodb://u:p@db:27017/wa", tmp_path)
    assert s.backend == "mongo"
    assert s.app_url.startswith("mongodb://")
    assert s.session_is_file is True
    assert s.session_dsn == str(tmp_path / "session.db")


def test_explicit_sqlite_path_keeps_state_together(tmp_path: Path):
    s = resolve_storage(f"sqlite:///{tmp_path}/custom.db", tmp_path)
    assert s.backend == "sqlite"
    assert "custom.db" in s.app_url
    assert s.session_dsn == str(tmp_path / "session.db")


def test_unknown_scheme_fails_loudly(tmp_path: Path):
    with pytest.raises(ValueError, match="unsupported scheme"):
        resolve_storage("mysql://u:p@db/wa", tmp_path)


def test_raw_proto_is_off_by_default(monkeypatch):
    """Half the messages table for something almost nobody reads."""
    monkeypatch.delenv("WA_STORE_RAW_PROTO", raising=False)
    assert Settings.from_env().store_raw_proto is False
    monkeypatch.setenv("WA_STORE_RAW_PROTO", "1")
    assert Settings.from_env().store_raw_proto is True


def test_three_and_four_slash_sqlite_both_mean_absolute(tmp_path: Path):
    """SQLAlchemy's 3-vs-4 slash rule silently creates a relative db instead."""
    a = resolve_storage(f"sqlite:///{tmp_path}/x.db", tmp_path)
    b = resolve_storage(f"sqlite:////{str(tmp_path).lstrip('/')}/x.db", tmp_path)
    assert a.app_url == b.app_url
    assert f"{tmp_path}/x.db" in a.app_url


def test_relative_sqlite_stays_relative(tmp_path: Path):
    s = resolve_storage("sqlite://data/app.db", tmp_path)
    assert s.app_url.endswith("data/app.db")
