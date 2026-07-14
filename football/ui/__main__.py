"""Launch the operator dashboard on 127.0.0.1 (ADR 0021).

    uv run python -m football.ui [--port 8000]

Bound to localhost only: this app spawns subprocesses and spends paid API quota,
so it must never be network-exposed.
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m football.ui")
    ap.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — do not expose)")
    args = ap.parse_args(argv)
    print(f"Operator dashboard → http://{args.host}:{args.port}")
    uvicorn.run("football.ui.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
