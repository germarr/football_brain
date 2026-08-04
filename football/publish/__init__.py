"""Publish — derive the remote Postgres Published Store from what we hold locally.

`pg` is the wholesale rebuild-and-swap, now the reset / re-baseline tool; `delta` is
the fast default intraday path that applies only the Finals whose data actually
changed (ADR 0028). Both are derived and re-runnable — a re-run makes the remote match
the local store.
"""
