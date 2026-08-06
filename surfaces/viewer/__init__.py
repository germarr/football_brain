"""The Viewer — the reader-facing surface (ADR 0023; moved here by ADR 0036).

Served at `/` by `python -m surfaces`. Shows the tracked leagues and cups, this week's
games in NYC time, and the per-match Match Tracker, reading **only** stores it is
allowed to and never `data/football.db`:

  - `serving/serve.db` — the daily-published serving slice, authoritative-for-the-Viewer;
  - `live/live.db`     — the provisional Live Mirror (ADR 0020), the per-match overlay.

It owns neither. That is not a violation of one-package-one-store but the reason this
package could move at all: the three surfaces own no store between them (ADR 0036).

`python -m surfaces.viewer` still boots it alone on :8001. That entrypoint is kept
deliberately — ADR 0023 deferred a "truly public, read-only Viewer", and that deferral
needs a Viewer process with no Editorial Store in it, which the composed app is not.
"""
