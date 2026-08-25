"""Entrypoint: one process serving MCP and the web UI.

    suprai-whatsapp-mcp                 # or: python -m wa_mcp

Nothing is required to be configured. With no environment set it stores in the
user data directory, listens on 127.0.0.1:8100, and tells you where to open a
browser to link a number.
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys

from .config import Settings, data_dir, resolve_storage


def _banner(settings: Settings, storage, token: str) -> str:
    base = f"http://{settings.host}:{settings.port}"
    key = f"?k={token}" if token else ""
    where = "in memory of the process" if not storage.session_is_file else str(
        data_dir()
    )
    lines = [
        "",
        "  suprai-whatsapp-mcp",
        "",
        f"  open        {base}/{key}",
        f"  MCP URL     {base}/mcp{key}",
        f"  storage     {storage.backend}",
        f"  data dir    {where}",
        "",
    ]
    if not token:
        lines += [
            "  NO AUTH TOKEN SET — fine on localhost, never behind a tunnel.",
            "  Set WA_AUTH_TOKEN before exposing this publicly: it can send",
            "  WhatsApp messages from your real number.",
            "",
        ]
    if storage.session_is_file:
        lines += [
            "  The data directory holds your WhatsApp login, not a cache.",
            "  Delete it and you must scan a new QR — and WhatsApp allows",
            "  only about four linked devices.",
            "",
        ]
    return "\n".join(lines)


def _load_dotenv() -> str | None:
    """Read .env from the working directory, if there is one.

    Configuration is a dozen environment variables, and on a server they have
    to live somewhere. A file next to the process is the least surprising
    place, and it means `docker run --env-file` and a bare `python -m wa_mcp`
    are configured the same way. Real environment variables always win, so a
    stale .env cannot override what the platform set.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:          # optional at runtime; only the file is lost
        return None
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)
    return path or None


def main(argv: list[str] | None = None) -> int:
    env_file = _load_dotenv()
    p = argparse.ArgumentParser(
        prog="suprai-whatsapp-mcp",
        description="WhatsApp for any LLM — MCP server, web UI and auto-reply.",
    )
    p.add_argument("--host", default=None, help="default 127.0.0.1")
    p.add_argument("--port", type=int, default=None, help="default 8100")
    p.add_argument("--database-url", default=None,
                   help="postgresql://… , mongodb://… or sqlite:///path. "
                        "Unset uses SQLite in the data directory.")
    p.add_argument("--data-dir", default=None, help="where SQLite files and media live")
    p.add_argument("--token", default=None,
                   help="bearer token. Use --token=generate for a fresh one.")
    p.add_argument("--log-level", default=None)
    p.add_argument("--print-config", action="store_true",
                   help="show resolved configuration and exit")
    args = p.parse_args(argv)

    if args.data_dir:
        os.environ["WA_DATA_DIR"] = args.data_dir
    if args.database_url:
        os.environ["WA_DATABASE_URL"] = args.database_url
    if args.token == "generate":
        os.environ["WA_AUTH_TOKEN"] = secrets.token_urlsafe(32)
    elif args.token:
        os.environ["WA_AUTH_TOKEN"] = args.token

    settings = Settings.from_env()
    if args.host:
        settings = settings.__class__(**{**settings.__dict__, "host": args.host})
    if args.port:
        settings = settings.__class__(**{**settings.__dict__, "port": args.port})
    if args.log_level:
        settings = settings.__class__(**{**settings.__dict__, "log_level": args.log_level})

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # After basicConfig, or the line goes nowhere.
    if env_file:
        logging.getLogger("wa_mcp").info("configuration read from %s", env_file)

    try:
        storage = resolve_storage()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.print_config:
        print(f"host          {settings.host}:{settings.port}")
        print(f"backend       {storage.backend}")
        print(f"app_url       {storage.app_url}")
        print(f"session_dsn   {storage.session_dsn}")
        print(f"session file  {storage.session_is_file}")
        print(f"data dir      {data_dir()}")
        print(f"auth token    {'set' if settings.auth_token else 'NOT SET'}")
        return 0

    print(_banner(settings, storage, settings.auth_token))

    import uvicorn

    from .app import create_app

    uvicorn.run(
        create_app(settings, storage),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
