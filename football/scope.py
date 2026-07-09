"""Build (or delete) a single-competition SQLite DB from the raw cache (no network).

`football.parse` builds one football.db holding every Competition. This extracts
ONE Competition into its own data/<slug>.db — e.g. just the Premier League — by
re-parsing only that competition's fixtures out of the shared raw cache. Like
parse, it uses a cache-only client, so it never spends API quota: the data must
already be collected (via football.orchestrate / football.collect); an uncollected
competition errors out with the command to collect it.

Career Stints are kept in full — a player's whole cross-competition career, faithful
to CONTEXT.md — so this DB's playerteam table carries team ids/names that are
deliberately not in its own (single-competition) team table.

Run with:
    uv run python -m football.scope 39            # build data/premier-league.db
    uv run python -m football.scope 39 --delete   # remove it (raw cache untouched)
"""
from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path

from . import collect, config, parse
from .client import CachedClient, QuotaExceeded


def _slugify(name: str) -> str:
    """A filesystem-safe ASCII slug for a competition name.

    'Premier League' -> 'premier-league'; 'Brasileirão' -> 'brasileirao';
    'Liga Profesional Argentina' -> 'liga-profesional-argentina'. Accents are
    stripped so the filename is plain ASCII across platforms.
    """
    ascii_name = (unicodedata.normalize("NFKD", name)
                  .encode("ascii", "ignore").decode("ascii"))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug or "competition"


def _competition(league_id: int) -> dict:
    """The registered/built-in Competition for a league id, or exit with guidance.

    A league we've never collected has no config entry (and no cache), so there is
    nothing to scope — point the user at orchestrate to collect it first.
    """
    for c in config.COMPETITIONS:
        if c["league_id"] == league_id:
            return c
    raise SystemExit(
        f"League {league_id} is not a known Competition. Collect it first with:\n"
        f"    uv run python -m football.orchestrate {league_id}"
    )


def _db_path(name: str) -> Path:
    """data/<slug>.db, alongside the full football.db."""
    return config.DB_PATH.parent / f"{_slugify(name)}.db"


def _cached_targets(comp: dict) -> list[tuple[int, str, int]]:
    """(league_id, name, season) for every season of this competition in the cache.

    Probes with a zero-budget client: a season whose fixtures were never collected
    raises QuotaExceeded and is skipped, so a partially collected competition still
    scopes cleanly — and the build (which also reads fixtures cache-only, but does
    not guard that read) never crashes on a missing season.
    """
    probe = CachedClient(max_live_requests=0)
    targets: list[tuple[int, str, int]] = []
    missing: list[int] = []
    for season in comp["seasons"]:
        try:
            collect.fetch_fixtures(probe, comp["league_id"], season)
        except QuotaExceeded:
            missing.append(season)
            continue
        targets.append((comp["league_id"], comp["name"], season))
    if missing:
        print(f"  note: {len(missing)} season(s) not in cache, skipped: "
              f"{', '.join(map(str, missing))}")
    return targets


def _build(league_id: int) -> None:
    comp = _competition(league_id)
    db_path = _db_path(comp["name"])
    targets = _cached_targets(comp)
    if not targets:
        raise SystemExit(
            f"No cached fixtures for {comp['name']} (id {league_id}). Collect it with:\n"
            f"    uv run python -m football.orchestrate {league_id}"
        )
    seasons = [s for _, _, s in targets]
    print(f"Scoping {comp['name']} (id {league_id}) -> {db_path.relative_to(config.ROOT)}")
    print(f"  {len(seasons)} season(s): {seasons[0]}–{seasons[-1]}")
    parse.build(db_path=db_path, targets=targets)


def _delete(league_id: int) -> None:
    comp = _competition(league_id)
    db_path = _db_path(comp["name"])
    # Only the scoped .db (and its build lock) — never the shared raw cache, which
    # is the expensive-to-rebuild asset, nor the full football.db.
    removed: list[str] = []
    for p in (db_path, db_path.with_suffix(".build.lock")):
        if p.exists():
            p.unlink()
            removed.append(p.name)
    if removed:
        print(f"Deleted {', '.join(removed)} (raw cache untouched).")
    else:
        print(f"Nothing to delete: {db_path.relative_to(config.ROOT)} does not exist.")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="python -m football.scope",
        description="Build (or delete) a single-competition SQLite DB from the raw cache.",
    )
    ap.add_argument("league_id", type=int,
                    help="provider league id, e.g. 39 (Premier League)")
    ap.add_argument("--delete", action="store_true",
                    help="delete the scoped DB instead of building it (raw cache untouched)")
    args = ap.parse_args(argv)
    if args.delete:
        _delete(args.league_id)
    else:
        _build(args.league_id)


if __name__ == "__main__":
    main()
