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
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return None
    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)
    return path or None


def _mint_routine_token(storage) -> int:
    import asyncio

    from .delivery import mint_routine
    from .runtime import build_store

    async def run() -> str:
        store = build_store(storage)
        await store.connect()
        try:
            return await mint_routine(store)
        finally:
            await store.close()

    token = asyncio.run(run())
    print(token)
    print()
    print("Configure the routine's WhatsApp connector with this, not your own", file=sys.stderr)
    print("token. It can call wa_send, wa_send_media and wa_typing, only in the", file=sys.stderr)
    print("chat a delivery names, and only when the call carries that", file=sys.stderr)
    print("delivery's reply_token. It does not expire; delete its row to revoke.", file=sys.stderr)
    return 0


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
    p.add_argument("--mint-routine-token", action="store_true",
                   help="print a restricted token for a hand-off webhook's "
                        "connector, then exit. It may only send, only in the "
                        "chat a delivery names, and only with that delivery's "
                        "reply_token.")
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
    if env_file:
        logging.getLogger("wa_mcp").info("configuration read from %s", env_file)

    try:
        storage = resolve_storage()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.mint_routine_token:
        return _mint_routine_token(storage)

    if args.print_config:
        print(f"host          {settings.host}:{settings.port}")
        print(f"backend       {storage.backend}")
        print(f"app_url       {storage.app_url}")
        print(f"session_dsn   {storage.session_dsn}")
        print(f"session file  {storage.session_is_file}")
        print(f"data dir      {data_dir()}")
        print(f"auth token    {'set' if settings.auth_token else 'NOT SET'}")
        return 0

    print(_banner(settings, storage, settings.auth_token), flush=True)

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
