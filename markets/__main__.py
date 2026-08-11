"""Serve the Market Store read API.

    uv run python -m markets                 # 127.0.0.1:8010

Binds to loopback like every other app here, and unlike every other app here it is *meant*
to be reached from outside — through a reverse proxy or a `cloudflared` tunnel that
terminates TLS and forwards to this port. Binding to `0.0.0.0` would put it on the LAN
without any of that, which is a different decision and not this one's to make.

Port 8010 rather than the 8000–8005 block the `surfaces/` apps use, and rather than 8005,
which an unrelated project on this box already holds.

What makes this safe to expose is in `api.py`: no write route, no subprocess, no
credential a reader can reach. Nothing here should ever mount a surface alongside it —
that would put a public listener in the same process as the Console (ADR 0046).
"""
import argparse

import uvicorn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m markets")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--reload", action="store_true", help="development autoreload")
    args = ap.parse_args(argv)
    uvicorn.run("markets.api:app", host=args.host, port=args.port,
                reload=args.reload, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
