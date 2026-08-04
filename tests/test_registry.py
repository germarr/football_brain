"""Both Registries resolve, exist, and are non-empty (ADR 0033).

The silent failure: `venues.py` resolves its data relative to its own `__file__`, and
`_read()` returns `[]` for an absent file while `_write()` then creates a fresh one at
whatever path the module now sits at. Move the module without its data and the
committed registry is orphaned, ~8k venues are re-fetched against a paid API, and the
nightly `git add` finds nothing to commit — with no error raised anywhere.

That is precisely what ADR 0031 does to `venues.py`, which is why these guards are
written before the move rather than after it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import preflight


def test_no_registry_problems():
    assert preflight.check_registries() == []


def test_competition_registry_is_a_non_empty_list():
    from football import config

    entries = json.loads(Path(config.COMPETITIONS_FILE).read_text())
    assert isinstance(entries, list) and entries
    assert all("league_id" in e for e in entries)


def test_venue_registry_is_a_non_empty_list():
    from football.build import venues

    entries = json.loads(Path(venues.REGISTRY_FILE).read_text())
    assert isinstance(entries, list) and entries
    assert all({"id", "name", "city"} <= e.keys() for e in entries)


def test_venue_ids_are_unique_and_stable():
    """Append-only, `max(id)+1`: a duplicate id would silently collapse two grounds
    into one across every store built from the registry (ADR 0028)."""
    from football.build import venues

    entries = json.loads(Path(venues.REGISTRY_FILE).read_text())
    ids = [e["id"] for e in entries]
    keys = [(e["name"], e["city"]) for e in entries]
    assert len(set(ids)) == len(ids), "duplicate venue id in the registry"
    assert len(set(keys)) == len(keys), "duplicate (name, city) in the registry"


def test_missing_registry_is_reported_not_silently_empty(monkeypatch, tmp_path):
    """The guard must bite. `venues.load()` returning `{}` for an absent file is the
    library's documented behaviour; the point is that the *check* refuses to shrug."""
    from football.build import venues

    monkeypatch.setattr(venues, "REGISTRY_FILE", tmp_path / "gone.json")
    problems = preflight.check_registries()
    assert any("Venue registry" in p and "no file" in p for p in problems), problems
    assert venues.load() == {}, "load() is silent by design — the preflight is not"


def test_empty_registry_file_is_reported(monkeypatch, tmp_path):
    from football.build import venues

    empty = tmp_path / "venues.json"
    empty.write_bytes(b"")
    monkeypatch.setattr(venues, "REGISTRY_FILE", empty)
    problems = preflight.check_registries()
    assert any("Venue registry" in p and "empty" in p for p in problems), problems


@pytest.mark.parametrize("attr", ["RAW_DIR"])
def test_raw_cache_symlink_resolves(attr):
    """`data/raw` is a symlink to another volume (ADR 0002 addendum). Broken, the
    cache-first fetcher misses every key and re-fetches ~8 GB against a paid plan."""
    assert preflight.check_raw_cache() == []


def test_broken_raw_cache_is_reported(monkeypatch, tmp_path):
    from football import config

    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "not-there")
    assert any("does not resolve" in p for p in preflight.check_raw_cache())


def test_empty_raw_cache_is_reported(monkeypatch, tmp_path):
    from football import config

    monkeypatch.setattr(config, "RAW_DIR", tmp_path)
    assert any("empty" in p for p in preflight.check_raw_cache())
