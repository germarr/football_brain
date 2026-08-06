"""Launch the Viewer alone on 127.0.0.1 (ADR 0023) — a debug entrypoint since ADR 0035.

    uv run python -m surfaces.viewer [--port 8001]

The normal way in is `python -m surfaces`, which serves this app at `/` and the Desk at
`/desk` on one port. This entrypoint stays for two reasons: debugging the Viewer without
the Editorial Store in the process, and because ADR 0023's deferred "truly public,
read-only Viewer" needs a Viewer that boots alone — `surfaces` is not one. Booting alone
renders the header with the nav, whose Desk and Console links will 404 — a known
wart of the debug mode (ADR 0036), not a bug.

Bound to localhost only: the per-match page can spawn a `live.poll` subprocess that
spends paid API quota, so it must never be network-exposed. Its read panels come from
serving/serve.db and live/live.db — never data/football.db.

(To publish serve.db, run `python -m serving.publish`, not this.)
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m surfaces.viewer")
    ap.add_argument("--port", type=int, default=8001, help="port (default 8001)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — do not expose)")
    args = ap.parse_args(argv)
    print(f"Viewer → http://{args.host}:{args.port}")
    uvicorn.run("surfaces.viewer.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
