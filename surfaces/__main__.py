"""Launch the composed surfaces on 127.0.0.1 (ADR 0035).

    uv run python -m surfaces [--port 8001]

`:8001` is the Viewer's old port, kept deliberately: the Viewer still answers at `/`,
so every bookmark you already have survives in path *and* port. The Desk is at `/desk`;
its old `:8002` is retired.

Bound to localhost only, and this process carries **both** surfaces' reasons at once:
`live.poll` and the pipeline spend API-Football quota, the Desk queries ESPN and spends
Anthropic tokens, and it writes to the **Editorial Store**, the only store here with no
rebuild path. It must never be network-exposed.

The Operator Console stays a separate process on :8000 — it writes `football.db`, which
is the coupling ADR 0023 split apart and this does not undo.
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
    print(f"Viewer → http://{args.host}:{args.port}/")
    print(f"the Desk → http://{args.host}:{args.port}/desk")
    uvicorn.run("surfaces.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
