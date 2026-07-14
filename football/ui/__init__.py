"""Operator Console (ADR 0021, ADR 0023).

A local FastAPI + Jinja app, bound to 127.0.0.1, whose only job is to trigger the
pipeline commands in `football.commands` (Collect / Build / Refresh / Publish) as
background subprocesses and stream their logs. The terminal workflow is unchanged —
this is a second front door to the same `python -m ...` commands.

ADR 0023 split the reader surface out: the tracked-competitions panel, this week's
fixtures, and the per-match Match Tracker now live in the Viewer (`web.app`), which
reads its own serve.db — this Console no longer reads football.db at all.

Launch: `uv run python -m football.ui`  (then open http://127.0.0.1:8000)
"""
