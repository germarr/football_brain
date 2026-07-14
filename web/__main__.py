"""Launch the Viewer on 127.0.0.1 (ADR 0023).

    uv run python -m web [--port 8001]

Bound to localhost only: the per-match page can spawn a `live.poll` subprocess that
spends paid API quota, so it must never be network-exposed. Its read panels come from
web/serve.db and live/live.db — never data/football.db.

(To publish serve.db, run `python -m web.publish`, not this.)
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m web")
    ap.add_argument("--port", type=int, default=8001, help="port (default 8001)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — do not expose)")
    args = ap.parse_args(argv)
    print(f"Viewer → http://{args.host}:{args.port}")
    uvicorn.run("web.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
