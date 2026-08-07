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
null until the nightly build mints it (ADR 0028).

**The stored pair is not the identity** (ADR 0042). The provider writes one ground several
ways — `Arena do Gremio` / `Arena do Grêmio`, `Beşiktaş Park` / `Besiktas Park` — and often
omits the city, so each spelling minted its own id and the registry's guarantee decayed from
*one id per ground* to *one id per spelling*. Identity is therefore matched on a **fold** of
the pair, and two entries that are one ground **merge**: the survivor is the *canonical*
Venue, and the merged id keeps its line here forever but is never assigned again.

A merge arrives one of two ways, and the split is deliberate: a **derived** merge (same
folded name *and* same folded city — a spelling difference) is decided here, silently, by
`load()`; a **decided** merge (the provider gave no city, or gave several) is a reviewed row
in the committed Venue merge list. *A derived merge must be conservative because nobody
reviews it; a proposed merge may be liberal because someone does* — which is why the fold
below requires the cities to match exactly while `--propose` compares them loosely.
"""
from __future__ import annotations

import fcntl
import json
import re
import unicodedata
from typing import Iterable

from .. import paths

# The identity of a ground, matching parse._venue_key: its name plus city (city may be
# null, which disambiguates same-named stadiums in different cities).
VenueKey = tuple[str, str | None]

REGISTRY_FILE = paths.VENUES_FILE
MERGES_FILE = paths.VENUE_MERGES_FILE
_LOCK_FILE = REGISTRY_FILE.with_name("venues.json.lock")

# Letters NFKD does **not** decompose, so they must be mapped by hand. This is not
# decoration: `NFKD -> encode("ascii", "ignore")` silently *deletes* æ, folding `Næsby`
# to `nsby` — a key that matches nothing and hides the duplicate the fold exists to find.
# Each entry below was confirmed against the registry to merge real duplicates: the Danish
# letters (`ALPI Arena Naesby` / `Næsby`), and Turkish dotless ı (`Osmanli Stadi` /
# `Osmanlı Stadı`).
_TRANSLIT = str.maketrans({
    "æ": "ae", "Æ": "ae", "ø": "o", "Ø": "o", "å": "aa", "Å": "aa", "ß": "ss",
    "œ": "oe", "Œ": "oe", "đ": "d", "Đ": "d", "ł": "l", "Ł": "l",
    "ı": "i", "ð": "d", "þ": "th",
})

# Dropped outright rather than turned into a space, so `St. Andrew's` and `St Andrews`
# fold together (a space would leave `st andrew s` beside `st andrews`).
_ELIDED = re.compile(r"[’'`.]")
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")

#: Whether `load()` resolves spelling duplicates onto one id. **Off until the merge window.**
#: The mechanism ships before the merge does (ADR 0042): flipping this changes the id 261
#: registry entries resolve to, which re-points fixtures in every store — so it belongs to
#: the commit that also runs the full rebuild and the wholesale `publish_pg`, never to a
#: night that would half-apply it through the delta. Decided merges in the list are honoured
#: regardless: a row there is an explicit act, and the list is empty until one is confirmed.
DERIVE_SPELLING_MERGES = False


def fold(s: str | None) -> str:
    """The match key for one half of a Venue identity. **Never displayed.**

    Transliterate the letters NFKD leaves alone, decompose and drop the remaining
    diacritics, casefold, elide apostrophes and periods, and reduce any other punctuation
    to a single space. `None` and `""` both fold to `""` — the absent city, which the fold
    deliberately does *not* treat as matching a present one (that is an inference about the
    world, not a fact about strings, and it is decided in the merge list instead).
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s.translate(_TRANSLIT))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    return _SPACES.sub(" ", _PUNCT.sub(" ", _ELIDED.sub("", s))).strip()


# The same order parse used when it enumerated (name, city) pairs, so a fresh registry
# seeds identically to the old 1..N surrogate. city None sorts as "" (can't compare to str).
def _sort_key(k: VenueKey) -> tuple[str, str]:
    return (k[0], k[1] or "")


def _load_raw() -> list[dict]:
    """The registry file's records, exactly as stored. The minter appends to *these* —
    `load()` returns a resolved map, which is a different thing and must not be written back.
    """
    if not REGISTRY_FILE.exists():
        return []
    return json.loads(REGISTRY_FILE.read_text())


def load_merges() -> list[dict]:
    """The committed Venue merge list (empty if absent — a repo may have no merges yet)."""
    if not MERGES_FILE.exists():
        return []
    return json.loads(MERGES_FILE.read_text())


def _canonical(entries: list[dict], merges: list[dict]) -> dict[int, int]:
    """``id -> canonical id`` for every registry entry.

    Two sources, unioned: entries whose folded name *and* folded city agree (derived), and
    the merge list's explicit pairs (decided). The survivor of a component is the lowest id
    that is not itself merged away — "merged away" meaning it appears as a `from`, which is
    what lets a decided merge overrule lowest-id-wins. That matters: `Stade Pierre Pibarot`
    holds the null city at id 5912 and `Alès` at 5913, so plain lowest-id-wins would elect
    the cityless entry and display a ground with no city. Chains (a → b, b → c) fall out for
    free, since b is a `from` and so cannot be elected either.
    """
    parent: dict[int, int] = {e["id"]: e["id"] for e in entries}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    if DERIVE_SPELLING_MERGES:
        by_fold: dict[tuple[str, str], list[int]] = {}
        for e in entries:
            city = fold(e["city"])
            if not city:                  # a cityless entry never merges by derivation
                continue
            by_fold.setdefault((fold(e["name"]), city), []).append(e["id"])
        for ids in by_fold.values():
            for other in ids[1:]:
                union(ids[0], other)

    known = parent.keys()
    for m in merges:
        if m["from"] in known and m["into"] in known:
            union(m["from"], m["into"])

    merged_away = {m["from"] for m in merges}
    members: dict[int, list[int]] = {}
    for i in parent:
        members.setdefault(find(i), []).append(i)

    canonical: dict[int, int] = {}
    for group in members.values():
        eligible = [i for i in group if i not in merged_away] or group
        winner = min(eligible)
        for i in group:
            canonical[i] = winner
    return canonical


def load() -> dict[VenueKey, int]:
    """The committed ``(name, city) -> canonical id`` map (empty if the registry is absent).

    Every stored pair resolves, including the spellings that merged — so a fixture written
    with any known spelling of a ground lands on the one id that ground has.
    """
    entries = _load_raw()
    canonical = _canonical(entries, load_merges())
    return {(e["name"], e["city"]): canonical[e["id"]] for e in entries}


def displays() -> dict[int, VenueKey]:
    """``canonical id -> (name, city)`` — the strings a Venue row should carry.

    Identity and display are chosen separately *on purpose* (ADR 0042). The id must be the
    lowest, because ids only grow and lowest-id-wins is the one rule an append cannot flip;
    but in 89 of 129 spelling clusters the lowest id holds the *mangled* spelling, so taking
    the strings from it too would fix the merge and display `Arena do Gremio`. So the strings
    come from the richest spelling in the group — most non-ASCII characters, ties to the
    lowest id — preferring an entry that actually has a city, and overridden outright by a
    merge row's optional `display` when the heuristic is known to be wrong (`Doosan Aréna`).
    """
    entries = _load_raw()
    merges = load_merges()
    canonical = _canonical(entries, merges)
    groups: dict[int, list[dict]] = {}
    for e in entries:
        groups.setdefault(canonical[e["id"]], []).append(e)

    def richness(e: dict) -> tuple[int, bool, int]:
        # Diacritics first, then case: the provider shouts some names, and a fold that
        # only counts non-ASCII cannot see it — `SIGNAL IDUNA PARK` and `Signal Iduna
        # Park` are equally "rich", so the lowest id would elect the shout (5 groups do,
        # all Bundesliga grounds). Lowest id still breaks a genuine tie.
        text = e["name"] + (e["city"] or "")
        return (sum(1 for ch in text if ord(ch) > 127), not e["name"].isupper(), -e["id"])

    out: dict[int, VenueKey] = {}
    for cid, group in groups.items():
        sited = [e for e in group if e["city"]] or group
        best = max(sited, key=richness)
        out[cid] = (best["name"], best["city"])
    for m in merges:
        display = m.get("display")
        if display and m["into"] in out:
            out[canonical[m["into"]]] = (display["name"], display.get("city"))
    return out


def _write_raw(entries: list[dict]) -> None:
    """Persist the registry as a JSON array, one venue per line ordered by id — so an append
    is a single trailing line in the git diff, never a reshuffle of the ~8k existing rows."""
    rows = [json.dumps({"id": e["id"], "name": e["name"], "city": e["city"]},
                       ensure_ascii=False, separators=(",", ":"))
            for e in sorted(entries, key=lambda e: e["id"])]
    REGISTRY_FILE.write_text("[\n" + ",\n".join(rows) + "\n]\n")


def mint(keys: Iterable[VenueKey]) -> dict[VenueKey, int]:
    """Register any unknown keys append-only under an exclusive lock; return the resolved map.

    New keys take ``max(id) + 1`` in sorted ``(name, city)`` order, so seeding a fresh
    registry with every key reproduces the old 1..N enumeration, and a later append never
    moves an existing id. The lock serialises a concurrent minter (only the nightly build
    mints today, but a second writer must never clobber the read-modify-write). ``None``
    keys (a fixture the provider gave no venue) are ignored.

    A new spelling of a ground already registered still gets its own *line* — the registry
    stays a faithful record of every pair the provider has sent — but not its own *identity*:
    the returned map resolves it onto the ground's canonical id, and `_write_raw` persists the
    stored records rather than the resolved map, so a merge is never written back as data.
    """
    wanted = {k for k in keys if k is not None}
    with open(_LOCK_FILE, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            entries = _load_raw()  # re-read inside the lock — another minter may have appended
            known = {(e["name"], e["city"]) for e in entries}
            new = sorted((k for k in wanted if k not in known), key=_sort_key)
            if new:
                nxt = max((e["id"] for e in entries), default=0) + 1
                for name, city in new:
                    entries.append({"id": nxt, "name": name, "city": city})
                    nxt += 1
                _write_raw(entries)
            canonical = _canonical(entries, load_merges())
            return {(e["name"], e["city"]): canonical[e["id"]] for e in entries}
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def seed_from_cache() -> dict[VenueKey, int]:
    """One-time migration seed (ADR 0028): mint every venue already in the raw cache.

    Enumerates the same `(name, city)` identities `parse` does, across every cached Season
    of every Competition, and mints them in one pass — so the registry starts as the full
    sorted `1..N` map over the union, matching what a full `football.db` build would assign.
    Idempotent: re-running only appends genuinely new grounds. Zero API (cache-only reads).
    """
    from .. import config, fetch
    from . import scope
    from ..client import CachedClient
    from .parse import _venue_key

    probe = CachedClient(max_live_requests=0)
    keys: set[VenueKey] = set()
    for comp in config.COMPETITIONS:
        for league_id, _name, season in scope._cached_targets(comp):
            for fx in fetch.fetch_fixtures(probe, league_id, season):
                key = _venue_key(fx)
                if key is not None:
                    keys.add(key)
    return mint(keys)


# --- proposing the merges no rule can decide -------------------------------

def _loose_city(city: str | None) -> str:
    """The city fold `--propose` compares on: the fold, truncated at its first comma.

    `Washington, District of Columbia` and `Washington` are one town; so are `Cleethorpes`
    and `Cleethorpes, North East Lincolnshire`. Across the whole registry this truncation
    fuses two distinct city strings exactly once — `Farsley, Leeds, West Yorkshire` with
    `Farsley, West Yorkshire`, correctly. It stays out of `fold()` regardless: a wrong fusion
    (`Newport, Wales` and `Newport, Isle of Wight`) must arrive as a proposal to reject, never
    as a derived merge nobody sees.
    """
    return fold((city or "").split(",")[0])


def propose() -> list[dict]:
    """Missing-city merges to review: a cityless entry beside exactly one town.

    Returns rows in merge-list form. Only *unproposed* clusters are returned — anything
    already carrying a decision is left alone, because a confirmed merge is frozen and never
    recomputed (`Stade Pierre Pibarot` gained a second town on one onboarding run; had the
    rule been re-derived at load time its fixtures would have moved silently).
    """
    entries = _load_raw()
    decided = {m["from"] for m in load_merges()} | {m["into"] for m in load_merges()}
    by_name: dict[str, list[dict]] = {}
    for e in entries:
        by_name.setdefault(fold(e["name"]), []).append(e)

    rows: list[dict] = []
    for group in by_name.values():
        cityless = [e for e in group if not e["city"]]
        sited = [e for e in group if e["city"]]
        if not cityless or not sited:
            continue
        if len({_loose_city(e["city"]) for e in sited}) != 1:
            continue                      # several towns — no rule decides this one
        into = min(sited, key=lambda e: e["id"])["id"]
        for e in cityless:
            if e["id"] not in decided:
                rows.append({"from": e["id"], "into": into, "class": "missing-city"})
    return sorted(rows, key=lambda r: r["from"])


def write_proposals(rows: list[dict]) -> int:
    """Append proposals to the merge list, leaving every existing row untouched."""
    merges = load_merges() + rows
    body = ",\n".join(json.dumps(m, ensure_ascii=False, separators=(",", ":")) for m in merges)
    MERGES_FILE.write_text(f"[\n{body}\n]\n" if merges else "[]\n")
    return len(rows)


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Inspect and propose Venue merges (ADR 0042).")
    ap.add_argument("--propose", action="store_true",
                    help="append missing-city merge proposals to the merge list for review")
    ap.add_argument("--report", action="store_true",
                    help="print the duplicate census without writing anything")
    args = ap.parse_args(argv)

    entries = _load_raw()
    canonical = _canonical(entries, load_merges())
    derived = sum(1 for i, c in canonical.items() if i != c)
    print(f"{len(entries)} registry entries; {derived} resolve onto another id today.")

    rows = propose()
    if args.report or not args.propose:
        print(f"{len(rows)} missing-city merge(s) proposable.")
        counts = _fixture_counts({r["from"] for r in rows} | {r["into"] for r in rows})
        for r in rows[:20]:
            e = {x["id"]: x for x in entries}
            frm, into = e[r["from"]], e[r["into"]]
            print(f"  {r['from']:>5} ({counts.get(r['from'], 0):>4} fx) {frm['name']!r}"
                  f"  ->  {r['into']:>5} ({counts.get(r['into'], 0):>4} fx) {into['city']!r}")
        if len(rows) > 20:
            print(f"  … and {len(rows) - 20} more")
    if args.propose:
        n = write_proposals(rows)
        print(f"wrote {n} proposal(s) to {MERGES_FILE}. Review the diff before committing.")


def _fixture_counts(ids: set[int]) -> dict[int, int]:
    """Fixtures per venue id from the built store — review context, never a decision input."""
    import sqlite3
    if not ids or not paths.DB_PATH.exists():
        return {}
    with sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True) as db:
        return dict(db.execute(
            "SELECT venue_id, count(*) FROM fixture WHERE venue_id IN (%s) GROUP BY 1"
            % ",".join("?" * len(ids)), tuple(ids)).fetchall())


if __name__ == "__main__":
    main()
