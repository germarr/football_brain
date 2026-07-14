"""Operator dashboard (ADR 0021).

A local FastAPI + Jinja app, bound to 127.0.0.1, that triggers the pipeline
commands in `football.commands` as background subprocesses and shows the tracked
competitions and this week's fixtures (NYC time). The terminal workflow is
unchanged — this is a second front door to the same `python -m ...` commands.

Launch: `uv run python -m football.ui`  (then open http://127.0.0.1:8000)
"""
