"""The Viewer + serving store (ADR 0023).

A separate world from the pipeline: a reader-facing FastAPI app (`web.app`) bound to
127.0.0.1 that shows the tracked leagues/cups, this week's games, and the per-match
Match Tracker — reading **only** its own stores, never `data/football.db`:

  - `web/serve.db`  — the serving store: a small, schema-identical, daily-published
    slice of `football.db` (all competitions + all players/careers + a rolling window
    of fixtures/events). Built by `web.publish`.
  - `live/live.db`  — the provisional Live Mirror (ADR 0020), the live overlay on the
    per-match page.

`football.db` is written by the pipeline (the `console` Operator Console) and read by exactly
one thing, `web.publish`. No web UI reads it live.

Launch the Viewer: `uv run python -m web`  (then open http://127.0.0.1:8001)
Publish the store: `uv run python -m web.publish`
"""
