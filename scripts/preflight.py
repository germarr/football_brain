"""Assert the invariants whose violation is otherwise invisible (ADR 0033).

The rule this file exists to serve: *test what fails silently, let loud failures
stay loud.* Everything checked here fails without raising — a wrong path that
imports cleanly, a registry that reads as empty, a cache directory that misses
every key and re-fetches against a paid API, a `module=` string nothing resolves
until an operator clicks it.

Run standalone (`python scripts/preflight.py`) as the first statement of
`scripts/nightly.sh` (ADR 0032), so a broken tree costs an email rather than a
night's quota. The same checks are imported by `tests/` — they are written as
functions returning problems, not as pytest bodies, precisely so both callers
can share them.

Exit status: 0 if every invariant holds, 1 otherwise, with each problem named on
stderr.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Runnable as a bare script from anywhere (cron does `cd` first, but a hand-run from
# scripts/ must not fail for a reason unrelated to what is being checked).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def check_registries() -> list[str]:
    """Both Registries resolve, exist, and are non-empty.

    `venues.py:_read()` returns `[]` for an absent file and `_write` then creates a
    fresh one, so a registry at the wrong path orphans the committed one, re-fetches
    every venue, and leaves the nightly `git add` with nothing to commit — in total
    silence. This is the check that turns that into a failed night.
    """
    problems: list[str] = []
    for label, getter in (
        ("Competition registry", lambda: __import__(
            "football.config", fromlist=["config"]).COMPETITIONS_FILE),
        ("Venue registry", lambda: __import__(
            "football.build.venues", fromlist=["venues"]).REGISTRY_FILE),
        # The third Registry (ADR 0041). Its own failure is loud — `load_registry` raises
        # rather than returning empty — but it is checked here for the same reason as the
        # other two: a Registry at the wrong path is the class of fault that should stop a
        # night before it spends anything, not surface later as cards quietly missing
        # their Winner Market.
        ("Kalshi team registry", lambda: __import__(
            "football_blog.kalshi", fromlist=["kalshi"]).REGISTRY_FILE),
    ):
        try:
            path = Path(getter())
        except Exception as exc:                       # noqa: BLE001 — report, never raise
            problems.append(f"{label}: could not resolve its path ({exc!r})")
            continue
        if not path.exists():
            problems.append(f"{label}: no file at {path}")
        elif path.stat().st_size == 0:
            problems.append(f"{label}: empty file at {path}")
    return problems


def check_raw_cache() -> list[str]:
    """`config.RAW_DIR` resolves to a populated directory.

    It is a symlink to another volume (ADR 0002 addendum). Broken or absent, the
    cache-first fetcher simply misses every key and re-fetches ~8 GB against a paid
    plan without erroring.
    """
    try:
        from football import config
    except Exception as exc:                           # noqa: BLE001
        return [f"raw cache: could not import football.config ({exc!r})"]

    raw = Path(config.RAW_DIR)
    if not raw.exists():
        target = f" -> {raw.readlink()}" if raw.is_symlink() else ""
        return [f"raw cache: {raw}{target} does not resolve"]
    if not raw.is_dir():
        return [f"raw cache: {raw} is not a directory"]
    if not any(raw.iterdir()):
        return [f"raw cache: {raw} resolves but is empty"]
    return []


def check_command_modules() -> list[str]:
    """Every `module=` string in the command registry actually resolves.

    They are strings the Console turns into `python -m` argv (ADR 0021), so a stale
    one is inert until someone clicks the button and gets `No module named`.
    """
    try:
        from football.commands import COMMANDS
    except Exception as exc:                           # noqa: BLE001
        return [f"command registry: could not import football.commands ({exc!r})"]

    problems = []
    for cmd in COMMANDS:
        try:
            if importlib.util.find_spec(cmd.module) is None:
                problems.append(f"command {cmd.key!r}: module {cmd.module!r} does not resolve")
        except Exception as exc:                       # noqa: BLE001
            problems.append(f"command {cmd.key!r}: module {cmd.module!r} failed to resolve ({exc!r})")
    return problems


CHECKS = (check_registries, check_raw_cache, check_command_modules)


def run() -> list[str]:
    """Every problem found, across every check. Never raises."""
    problems: list[str] = []
    for check in CHECKS:
        problems.extend(check())
    return problems


def main() -> int:
    problems = run()
    if problems:
        print("preflight FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print(f"preflight OK ({len(CHECKS)} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
