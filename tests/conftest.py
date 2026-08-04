"""Shared fixtures for the ADR 0033 guards.

The suite is a refactoring harness, not a quality programme: it covers the
invariants that a wrong path violates *without raising*, and nothing else.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: The committed slice of the raw cache (ADR 0033). The live cache is ~8 GB behind a
#: machine-specific symlink, so the suite must not read it — a test that skips when
#: its input is absent is the exact failure mode these guards exist to catch.
CARVED_CACHE = REPO_ROOT / "tests" / "fixtures" / "raw"

#: What the slice holds, and therefore what a build from it must produce. Carved by
#: `scripts/carve_test_fixtures.py --league 140 --season 2024 --fixtures 3`.
SLICE_LEAGUE_ID = 140
SLICE_LEAGUE_NAME = "La Liga"
SLICE_SEASON = 2024
SLICE_FIXTURES = 3
SLICE_TEAMS = 6
SLICE_VENUES = 3


@pytest.fixture
def carved_cache(monkeypatch) -> Path:
    """Point `config.RAW_DIR` at the committed slice.

    `RAW_DIR` is read at call time (`config.RAW_DIR / endpoint`), never captured at
    import, so monkeypatching the module attribute is enough and no production code
    needs a seam opened for the tests.
    """
    from football import config

    assert CARVED_CACHE.is_dir(), (
        f"carved cache missing at {CARVED_CACHE} — "
        f"re-create it with scripts/carve_test_fixtures.py"
    )
    monkeypatch.setattr(config, "RAW_DIR", CARVED_CACHE)
    return CARVED_CACHE


@pytest.fixture
def temp_venue_registry(tmp_path, monkeypatch) -> Path:
    """Redirect the Venue registry so a test build never mints into the committed one.

    `_LOCK_FILE` is derived from `REGISTRY_FILE` at import, so both must be patched —
    miss the second and the test takes a lock beside the real registry.
    """
    from football.build import venues

    registry = tmp_path / "venues.json"
    monkeypatch.setattr(venues, "REGISTRY_FILE", registry)
    monkeypatch.setattr(venues, "_LOCK_FILE", registry.with_name("venues.json.lock"))
    return registry
