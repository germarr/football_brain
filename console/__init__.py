"""Operator Console (ADR 0021, ADR 0023).

A local FastAPI + Jinja app, bound to 127.0.0.1, whose only job is to trigger the
pipeline commands in `football.commands` (Onboard / Backfill / Build / Refresh /
Publish) as
background subprocesses and stream their logs. The terminal workflow is unchanged —
this is a second front door to the same `python -m ...` commands.

ADR 0023 split the reader surface out: the tracked-competitions panel, this week's
fixtures, and the per-match Match Tracker now live in the Viewer (`web.app`), which
reads its own serve.db — this Console no longer reads football.db at all.

Launch: `uv run python -m surfaces`  (then open http://127.0.0.1:8001/console)

Since ADR 0035 this is mounted at `/console` alongside the Viewer and the Desk on one
port. It could be, precisely because ADR 0023 had already stripped its reads: it holds
no database handle, its options come from `competitions.json`, it *probes* the build
lock and releases it at once, and `football.db` is written by the subprocesses it
spawns — separate OS processes, however this app is hosted. `python -m console`
still boots it alone as a debug entrypoint; every path in its template is built from
`base_url(request)` so both modes serve correctly.

ADR 0031 promoted this out of `football/ui/`: it is an application that *fires* the
pipeline, not a part of it, and it is the sole member of the `Control` role.
"""
