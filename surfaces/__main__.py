"""Launch all three local surfaces on 127.0.0.1 (ADR 0035).

    uv run python -m surfaces [--port 8001]

`:8001` is the Viewer's old port, kept deliberately: the Viewer still answers at `/`,
so every bookmark you already have survives in path *and* port. The Desk is at `/desk`
and the Console at `/console`; their old `:8002` and `:8000` are retired.

Bound to localhost only, and this process carries every surface's reason at once:
`live.poll`, the pipeline and the Console's collectors spend API-Football quota, the
Desk queries ESPN and spends Anthropic tokens and writes to the **Editorial Store**
(the only store here with no rebuild path), and the Console can drop and rebuild
`football.db` or replace the Postgres Published Store wholesale. It must never be
network-exposed.

One process now holds all three job registries, so restarting it drops every streaming
log at once. The subprocesses themselves survive — orphaned but running — you just lose
the log and the Stop button. That is the cost this arrangement pays.
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m surfaces")
    ap.add_argument("--port", type=int, default=8001, help="port (default 8001)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — do not expose)")
    args = ap.parse_args(argv)
    print(f"Viewer  → http://{args.host}:{args.port}/")
    print(f"Desk    → http://{args.host}:{args.port}/desk")
    print(f"Console → http://{args.host}:{args.port}/console")
    uvicorn.run("surfaces.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
