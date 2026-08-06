"""Launch the Console alone on 127.0.0.1 — a debug entrypoint since ADR 0035.

    uv run python -m surfaces.console [--port 8000]

The normal way in is `python -m surfaces`, which serves this app at `/console`
alongside the Viewer and the Desk on :8001. This entrypoint stays for debugging;
booting alone still renders the nav, whose other two links will 404 — a known wart
of the debug mode (ADR 0036), not a bug.

Bound to localhost only: this app spawns subprocesses and spends paid API quota,
so it must never be network-exposed.
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m surfaces.console")
    ap.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — do not expose)")
    args = ap.parse_args(argv)
    print(f"Operator Console → http://{args.host}:{args.port}")
    uvicorn.run("surfaces.console.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
