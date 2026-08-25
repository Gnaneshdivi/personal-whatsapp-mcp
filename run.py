#!/usr/bin/env python3
"""Start the server.

Exactly the same thing as `python -m wa_mcp`, and it takes the same options —
this file exists so that starting it does not require knowing that. Run it
with no arguments and it listens on http://127.0.0.1:8100.

    python run.py
    python run.py --port 9000
    python run.py --help
"""
from wa_mcp.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
