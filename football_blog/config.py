"""Env loading + typed client factories.

Loads the project-root .env into os.environ on module import so any script
that imports football_blog gets the vars for free — mirrors the TypeScript
side's dotenv-on-import in blog/la-cancha/src/lib/env.ts.

conftest.py already handles this for pytest, but production CLIs
(`python -m football_blog.draft`, `... .onboard`) don't run through pytest,
so we need the same behavior here.
"""
import os
from pathlib import Path
from typing import Optional


def _load_project_env() -> None:
    """Read the project-root .env into os.environ. Idempotent — existing values win."""
    root = Path(__file__).resolve().parent.parent
    env_file = root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'\"")
        if k and k not in os.environ:
            os.environ[k] = v


_load_project_env()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(
            f"Missing required env var: {name}. "
            f"Set it in the project-root .env file."
        )
    return value


def optional_env(name: str, fallback: str) -> str:
    value = os.environ.get(name)
    return value if (value is not None and value != "") else fallback
