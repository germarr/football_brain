"""Launch the Previews board alone on 127.0.0.1 — a debug entrypoint (ADR 0040).

    uv run python -m surfaces.previews [--port 8005]

The normal way in is `python -m surfaces`, which serves this at `/previews` alongside the
Viewer, the Desk, the Console and Competitions on :8001. Booting alone still renders the
nav, whose other links will 404 — a known wart of the debug mode (ADR 0036), not a bug.

Bound to localhost only: the runs it spawns write to the Editorial Store.
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m surfaces.previews")
    ap.add_argument("--port", type=int, default=8005, help="port (default 8005)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — do not expose)")
    args = ap.parse_args(argv)
    print(f"Previews → http://{args.host}:{args.port}")
    uvicorn.run("surfaces.previews.app:app", host=args.host, port=args.port,
                log_level="info")


if __name__ == "__main__":
    main()
