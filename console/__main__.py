"""Launch the Operator Console on 127.0.0.1 (ADR 0021, ADR 0023).

    uv run python -m console [--port 8000]

Bound to localhost only: this app spawns subprocesses and spends paid API quota,
so it must never be network-exposed. It builds football.db; the reader-facing
Viewer is a separate app — `python -m web` on :8001 (ADR 0023).
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m console")
    ap.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — do not expose)")
    args = ap.parse_args(argv)
    print(f"Operator Console → http://{args.host}:{args.port}")
    uvicorn.run("console.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
