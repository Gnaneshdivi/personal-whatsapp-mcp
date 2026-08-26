from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def data_dir() -> Path:
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

    backend: str
    app_url: str
    session_dsn: str
    session_is_file: bool


def resolve_storage(url: str | None = None, dir_: Path | None = None) -> Storage:
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
        return Storage(
            backend="postgres",
            app_url=_asyncpg(url),
            session_dsn=_neonize_pg(url),
            session_is_file=False,
        )

    if scheme in ("mongodb", "mongodb+srv"):
        return Storage(
            backend="mongo",
            app_url=url,
            session_dsn=session_file,
            session_is_file=True,
        )

    if scheme == "sqlite":
        rest = url.split("://", 1)[1]
        path = Path("/" + rest.lstrip("/")) if rest.startswith("/") else Path(rest)
        return Storage(
            backend="sqlite",
            app_url=f"sqlite+aiosqlite:///{path}",
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
    store_raw_proto: bool = False
    device_os: str = "Chrome"
    device_platform: str = "CHROME"
    allow_open: bool = False
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
            allow_open=_flag("WA_ALLOW_OPEN", False),
            history_days=int(os.getenv("WA_HISTORY_DAYS", "365")),
            history_size_mb=int(os.getenv("WA_HISTORY_SIZE_MB", "500")),
        )


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
