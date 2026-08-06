"""The serving store and the step that builds it (ADR 0023; moved here by ADR 0036).

  - `serving/serve.db` — a small, schema-identical, daily-published slice of
    `football.db`: every competition, every player + career, and a rolling −3..+10 day
    window of fixtures/events. Owned by this package, built by `serving.publish`.

    uv run python -m serving.publish

`football.db` has exactly two consumers: the pipeline that writes it, and
`serving.publish`, which reads it to derive this store. No web UI reads it live — that
separation is the whole point of ADR 0023. The Viewer that consumes `serve.db` is
`surfaces/viewer/`, and it never opens `football.db`.

This package was `web/` until ADR 0036 moved the three local apps into `surfaces/`. The
name went with the app rather than with the store: what remains builds and owns a
database, and `web` described neither. It keeps owning `serve.db` instead of folding
into `football/publish/` beside `pg.py` and `delta.py`, because that would leave the
store written by one package and owned by none.
"""
