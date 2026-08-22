"""Configuration, and the storage decisions that fall out of one env var.

The whole point of this module is that a bare `pip install` with nothing
configured produces a working install. Everything below has a default that works
on a laptop, and the only variable most people will ever set is the one that
points at a real database.

    WA_DATABASE_URL   unset -> SQLite in the data dir
                      postgresql://…  -> Postgres
                      mongodb://…     -> Mongo
                      sqlite:///path  -> that file

    WA_DATA_DIR       where SQLite files and cached media live
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def data_dir() -> Path:
    """Where anything file-shaped lives. Created on first use.

    NOT inside the installed package. `pip install --upgrade` rewrites
    site-packages, and site-packages is read-only in plenty of container and
    system-python setups — either would silently destroy the WhatsApp login.
    A user data directory is the only location that survives both.
    """
    override = os.getenv("WA_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        path = Path(base) / "suprai-whatsapp"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "suprai-whatsapp"
    else:
        base = os.getenv("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        path = Path(base) / "suprai-whatsapp"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Storage:
    """Resolved storage decisions.

    `backend` selects the app-data adapter. `session_dsn` is handed to neonize
    and is a SEPARATE decision, because whatsmeow only speaks SQLite and
    Postgres — it cannot follow a Mongo URL.
    """

    backend: str          # "sqlite" | "postgres" | "mongo"
    app_url: str          # DSN/URI for the app-data adapter
    session_dsn: str      # what NewAClient() gets
    session_is_file: bool # True when the login lives on disk and must persist


def resolve_storage(url: str | None = None, dir_: Path | None = None) -> Storage:
    """Turn one optional URL into every storage decision.

    Scheme detection rather than a separate WA_STORE=… selector, because the
    URL already carries the answer and two variables that must agree is one
    variable too many. It also mirrors how whatsmeow picks its own dialect —
    by prefix — so the behaviour is consistent top to bottom.
    """
    url = (url if url is not None else os.getenv("WA_DATABASE_URL", "")).strip()
    dir_ = dir_ or data_dir()
    session_file = f"{dir_ / 'session.db'}"

    if not url:
        return Storage(
            backend="sqlite",
            app_url=f"sqlite+aiosqlite:///{dir_ / 'app.db'}",
            session_dsn=session_file,
            session_is_file=True,
        )

    scheme = urlparse(url).scheme.split("+")[0].lower()

    if scheme in ("postgresql", "postgres"):
        # The session rides along. whatsmeow supports Postgres, so a user who
        # gave us one gets a fully stateless container — no volume to mount and
        # nothing to lose on a rolling restart. That is worth the extra branch:
        # a container that loses session.db re-pairs on every deploy and burns
        # a linked-device slot each time, and WhatsApp allows about four.
        return Storage(
            backend="postgres",
            app_url=_asyncpg(url),
            session_dsn=_neonize_pg(url),
            session_is_file=False,
        )

    if scheme in ("mongodb", "mongodb+srv"):
        # No Mongo backend exists for whatsmeow, so the login stays a file and
        # the deployment MUST persist WA_DATA_DIR. Callers surface this loudly.
        return Storage(
            backend="mongo",
            app_url=url,
            session_dsn=session_file,
            session_is_file=True,
        )

    if scheme == "sqlite":
        # Deliberately NOT SQLAlchemy's three-slash-relative / four-slash-absolute
        # rule. Everyone writes sqlite:///Users/me/app.db meaning an absolute
        # path, and under the strict reading that silently creates
        # "Users/me/app.db" relative to the working directory — a second, empty
        # database that looks like data loss. Any leading slash means absolute.
        rest = url.split("://", 1)[1]
        path = Path("/" + rest.lstrip("/")) if rest.startswith("/") else Path(rest)
        return Storage(
            backend="sqlite",
            app_url=f"sqlite+aiosqlite:///{path}",
            # Beside the app db, not in the data dir — someone who pointed at an
            # explicit path meant "keep my state here".
            session_dsn=f"{path.parent / 'session.db'}",
            session_is_file=True,
        )

    raise ValueError(
        f"WA_DATABASE_URL has an unsupported scheme {scheme!r}. "
        "Use postgresql://, mongodb://, sqlite:/// or leave it unset."
    )


def _asyncpg(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def _neonize_pg(url: str) -> str:
    """The form whatsmeow's Go layer expects.

    Two non-obvious requirements, both learned the hard way:

    1. The DSN must start with the literal "postgres" — goneonize selects the
       driver with `strings.HasPrefix(db, "postgres")`, so SQLAlchemy's
       `postgresql+asyncpg://` form silently selects the SQLite branch and
       writes a local file named after the whole connection string.
    2. sslmode must be explicit. Go's lib/pq defaults to requiring SSL while a
       default local Postgres does not offer it, and the connection then hangs
       with no error, no log line and no tables created — which looks exactly
       like "Postgres is unsupported".
    """
    plain = url
    if plain.startswith("postgresql+asyncpg://"):
        plain = "postgresql://" + plain[len("postgresql+asyncpg://"):]
    if plain.startswith("postgresql://"):
        plain = "postgres://" + plain[len("postgresql://"):]
    if "sslmode=" not in plain:
        plain += ("&" if "?" in plain else "?") + "sslmode=" + os.getenv(
            "WA_SESSION_SSLMODE", "disable"
        )
    return plain


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8100
    auth_token: str = ""
    public_base_url: str = ""
    log_level: str = "INFO"
    # Raw protobuf is ~18x the size of the text it contains and about half of
    # the messages table. Useful for debugging an extraction bug, dead weight
    # for everyone else, so it is opt-in.
    store_raw_proto: bool = False
    device_os: str = "Chrome"
    device_platform: str = "CHROME"
    # OAuth turns the connector flow into "click Connect, scan, done". The
    # static token still works alongside it, which is what keeps localhost and
    # curl simple.
    oauth: bool = True
    # How much history WhatsApp sends at pair time. It is delivered ONCE, on
    # the pairing connection — a reconnect never replays it — so asking for too
    # little here cannot be corrected later without unlinking and scanning
    # again. 365 days is generous without being absurd; the size limit is what
    # actually bounds it.
    history_days: int = 365
    history_size_mb: int = 500

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.getenv("WA_HOST", "127.0.0.1"),
            port=int(os.getenv("WA_PORT", "8100")),
            auth_token=os.getenv("WA_AUTH_TOKEN", os.getenv("MCP_AUTH_TOKEN", "")),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "").rstrip("/"),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            store_raw_proto=_flag("WA_STORE_RAW_PROTO", False),
            device_os=os.getenv("WA_DEVICE_OS", "Chrome"),
            device_platform=os.getenv("WA_DEVICE_PLATFORM", "CHROME"),
            oauth=_flag("WA_OAUTH", True),
            history_days=int(os.getenv("WA_HISTORY_DAYS", "365")),
            history_size_mb=int(os.getenv("WA_HISTORY_SIZE_MB", "500")),
        )


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
