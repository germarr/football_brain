"""The Venue identity registry — a committed, append-only ``(name, city) -> id`` map.

`Venue.id` must be the **same** surrogate in every store built from the raw cache
(`data/football.db`, each scoped `data/<slug>.db`, the delta temp store, the Postgres
Published Store), so the additive delta publish (ADR 0028) can reference a stadium already
in Postgres without renumbering. Enumerating the unique `(name, city)` pairs `1..N` *per
build* — as `parse._build_venues` used to — cannot give that: one new alphabetically-early
ground shifts every id. So ids are assigned **once**, here, and never reshuffled.

The registry is a committed JSON array, held with `competitions.json` in one registry
directory and located through `football.paths` rather than relative to this module —
so this module can be reorganized without the data following it (ADR 0031, which also
records the silent orphaning that motivated the change). It is deliberately **not**
regenerable from the raw cache — rebuilt in sorted
order, a later-added early-alphabet ground would take a *different* id than it did when
appended incrementally — so it joins `data/commentary.db` as a durable artifact. But a
*milder* one: losing it costs a one-time global re-baseline (every store renumbers its
venues on its next rebuild; the ids are surrogates nobody pins externally), never data.

Writes are **append-only under a file lock**. The nightly full `football.db` build is the
sole minter (it parses every Competition, so it sees every venue); the unattended intraday
delta is read-only against the registry and leaves an unknown venue's `Fixture.venue_id`
null until the nightly build mints it (ADR 0028). See ADR 0028.
"""
from __future__ import annotations

import fcntl
import json
from typing import Iterable

from . import paths

# The identity of a ground, matching parse._venue_key: its name plus city (city may be
# null, which disambiguates same-named stadiums in different cities).
VenueKey = tuple[str, str | None]

REGISTRY_FILE = paths.VENUES_FILE
_LOCK_FILE = REGISTRY_FILE.with_name("venues.json.lock")

# The same order parse used when it enumerated (name, city) pairs, so a fresh registry
# seeds identically to the old 1..N surrogate. city None sorts as "" (can't compare to str).
def _sort_key(k: VenueKey) -> tuple[str, str]:
    return (k[0], k[1] or "")


def _read() -> list[dict]:
    if not REGISTRY_FILE.exists():
        return []
    return json.loads(REGISTRY_FILE.read_text())


def load() -> dict[VenueKey, int]:
    """The committed ``(name, city) -> stable id`` map (empty if the registry is absent)."""
    return {(e["name"], e["city"]): e["id"] for e in _read()}


def _write(mapping: dict[VenueKey, int]) -> None:
    """Persist the map as a JSON array, one venue per line ordered by id — so an append is
    a single trailing line in the git diff, never a reshuffle of the ~8k existing rows."""
    entries = [{"id": i, "name": n, "city": c}
               for (n, c), i in sorted(mapping.items(), key=lambda kv: kv[1])]
    rows = [json.dumps(e, ensure_ascii=False, separators=(",", ":")) for e in entries]
    REGISTRY_FILE.write_text("[\n" + ",\n".join(rows) + "\n]\n")


def mint(keys: Iterable[VenueKey]) -> dict[VenueKey, int]:
    """Register any unknown keys append-only under an exclusive lock; return the full map.

    New keys take ``max(id) + 1`` in sorted ``(name, city)`` order, so seeding a fresh
    registry with every key reproduces the old 1..N enumeration, and a later append never
    moves an existing id. The lock serialises a concurrent minter (only the nightly build
    mints today, but a second writer must never clobber the read-modify-write). ``None``
    keys (a fixture the provider gave no venue) are ignored.
    """
    wanted = {k for k in keys if k is not None}
    with open(_LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            mapping = load()  # re-read inside the lock — another minter may have appended
            new = sorted((k for k in wanted if k not in mapping), key=_sort_key)
            if new:
                nxt = max(mapping.values(), default=0) + 1
                for k in new:
                    mapping[k] = nxt
                    nxt += 1
                _write(mapping)
            return mapping
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def seed_from_cache() -> dict[VenueKey, int]:
    """One-time migration seed (ADR 0028): mint every venue already in the raw cache.

    Enumerates the same `(name, city)` identities `parse` does, across every cached Season
    of every Competition, and mints them in one pass — so the registry starts as the full
    sorted `1..N` map over the union, matching what a full `football.db` build would assign.
    Idempotent: re-running only appends genuinely new grounds. Zero API (cache-only reads).
    """
    from . import collect, config, scope
    from .client import CachedClient
    from .parse import _venue_key

    probe = CachedClient(max_live_requests=0)
    keys: set[VenueKey] = set()
    for comp in config.COMPETITIONS:
        for league_id, _name, season in scope._cached_targets(comp):
            for fx in collect.fetch_fixtures(probe, league_id, season):
                key = _venue_key(fx)
                if key is not None:
                    keys.add(key)
    return mint(keys)
