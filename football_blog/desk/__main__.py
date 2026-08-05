"""Launch the Desk alone on 127.0.0.1 (ADR 0034) — a debug entrypoint since ADR 0035.

    uv run python -m football_blog.desk [--port 8002]

The normal way in is `python -m surfaces`, which serves the Desk at `/desk` alongside
the Viewer on :8001. This entrypoint stays for debugging; note that a stale process left
here is a genuine confusion, since the Desk then answers on two addresses. Every path in
the templates is built from `base_url(request)`, so both modes work — and this is the
one mode where a hardcoded `/desk` would have looked fine and 404'd under the mount.

Bound to localhost only, and this one has three reasons rather than the usual one: it
spawns the pipeline (API-Football quota), it queries ESPN, and it spends Anthropic
tokens — and it writes to the **Editorial Store**, the only store here with no rebuild
path. It must never be network-exposed.

The other two surfaces: the Operator Console is `python -m console` on :8000, the
Viewer `python -m web` on :8001 (ADR 0021/0023).
"""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m football_blog.desk")
    ap.add_argument("--port", type=int, default=8002, help="port (default 8002)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind host (default 127.0.0.1 — do not expose)")
    args = ap.parse_args(argv)
    print(f"the Desk → http://{args.host}:{args.port}")
    uvicorn.run("football_blog.desk.app:app", host=args.host, port=args.port,
                log_level="info")


if __name__ == "__main__":
    main()
