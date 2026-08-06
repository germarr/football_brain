"""Launch the Competitions board alone on 127.0.0.1 — a debug entrypoint (ADR 0037).

    uv run python -m surfaces.competitions [--port 8004]

The normal way in is `python -m surfaces`, which serves this at `/competitions`
alongside the Viewer, the Desk and the Console on :8001. Booting alone still renders the
nav, whose other links will 404 — a known wart of the debug mode (ADR 0036), not a bug.

Bound to localhost only: the run it spawns spends API-Football quota and replaces the
Postgres Published Store wholesale.
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m surfaces.competitions")
    ap.add_argument("--port", type=int, default=8004, help="port (default 8004)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — do not expose)")
    args = ap.parse_args(argv)
    print(f"Competitions → http://{args.host}:{args.port}")
    uvicorn.run("surfaces.competitions.app:app", host=args.host, port=args.port,
                log_level="info")


if __name__ == "__main__":
    main()
