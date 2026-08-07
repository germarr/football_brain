"""The committed migrations still describe the live collections (ADR 0033/0044).

`football_blog/migrations/` holds copies of the two migrations that define
`match_bundle` and `fixture_row`. PocketBase does not read them — it reads
`personal_site/pocketbase/pb_migrations/` — so on their own they are a claim about a
schema nobody checks, and a claim like that rots without ever raising.

This compares the fields those files declare against the fields the **live instance**
actually has. Checking against the running schema rather than against the other repo's
copy is deliberate and catches strictly more: a field renamed in the PocketBase admin UI
touches no migration file anywhere, so a file-to-file comparison would pass while the
builder had quietly started writing a key nothing stores.

Which is the failure this is really about. PocketBase **ignores unknown keys on write**.
A field that vanishes does not 400 the builder — the write succeeds, the count says
`written`, and the data is simply absent from the page. Nothing in the pipeline can tell
that from success.

Skips rather than fails when PocketBase is unreachable: the rest of the suite is offline
and must stay runnable on a machine that is not hosting the Editorial Store.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import football_blog
from football_blog.pocketbase import BUNDLE_COLLECTION, ROW_COLLECTION

#: Resolved off the package rather than imported as one — the directory holds `.js` and a
#: README, no Python, so an `__init__.py` there would exist only to satisfy an import.
MIGRATIONS_DIR = Path(football_blog.__file__).parent / "migrations"

#: Which committed file is the authority for which live collection.
COLLECTIONS = {
    BUNDLE_COLLECTION: "1786150000_created_match_bundle.js",
    ROW_COLLECTION: "1786150001_created_fixture_row.js",
}

#: PocketBase maintains these itself; a migration declares them but they are not part of
#: the contract with the builder or with `blog/la-cancha`.
SYSTEM_FIELDS = {"id", "created", "updated"}


def _declared_fields(path: Path) -> dict[str, str]:
    """`{field name: type}` as the migration file declares them.

    The files are JS, not JSON — they carry `//` comments and a `migrate((app) => …)`
    wrapper — so this reads the `"fields"` array textually rather than parsing them.
    Comments are stripped first, because several of them contain quoted field names in
    prose and would otherwise be read as declarations.
    """
    src = re.sub(r"^\s*//.*$", "", path.read_text(), flags=re.MULTILINE)
    start = src.index('"fields":')
    depth, i = 0, src.index("[", start)
    for i in range(i, len(src)):
        if src[i] == "[":
            depth += 1
        elif src[i] == "]":
            depth -= 1
            if depth == 0:
                break
    block = src[src.index("[", start):i + 1]
    names = re.findall(r'"name":\s*"(\w+)"', block)
    types = re.findall(r'"type":\s*"(\w+)"', block)
    assert len(names) == len(types), f"{path.name}: {len(names)} names vs {len(types)} types"
    return {n: t for n, t in zip(names, types) if n not in SYSTEM_FIELDS}


def _live_fields(collection: str) -> dict[str, str]:
    import httpx

    from football_blog.pocketbase import PocketBaseClient

    pb = PocketBaseClient()
    try:
        r = pb._client.get(f"{pb.base_url}/api/collections",
                           headers=pb._headers(), params={"perPage": 200})
        r.raise_for_status()
        for c in r.json()["items"]:
            if c["name"] == collection:
                return {f["name"]: f["type"] for f in c["fields"]
                        if f["name"] not in SYSTEM_FIELDS}
        pytest.fail(f"collection {collection!r} does not exist in the live instance")
    finally:
        pb.close()


@pytest.fixture(scope="module")
def live():
    """The live instance, or a skip when it is not reachable."""
    import httpx

    from football_blog.config import optional_env
    base = optional_env("POCKETBASE_URL", "http://localhost:8090").rstrip("/")
    try:
        httpx.get(f"{base}/api/health", timeout=2.0).raise_for_status()
    except Exception as exc:                       # noqa: BLE001 — any failure is a skip
        pytest.skip(f"PocketBase not reachable at {base} ({type(exc).__name__})")
    return base


@pytest.mark.parametrize("collection,filename", sorted(COLLECTIONS.items()))
def test_the_committed_migration_matches_the_live_collection(collection, filename, live):
    """Every declared field exists live, with the declared type, and no extras."""
    declared = _declared_fields(MIGRATIONS_DIR / filename)
    actual = _live_fields(collection)

    missing = {k: v for k, v in declared.items() if k not in actual}
    extra = {k: v for k, v in actual.items() if k not in declared}
    retyped = {k: (v, actual[k]) for k, v in declared.items()
               if k in actual and actual[k] != v}

    assert not missing, (
        f"{collection}: declared in {filename} but ABSENT live: {missing}. "
        f"The builder writes these keys and PocketBase discards unknown keys silently.")
    assert not extra, (
        f"{collection}: present live but not in {filename}: {extra}. "
        f"Someone changed the schema without a migration, or the copy is stale.")
    assert not retyped, f"{collection}: type changed (declared, live): {retyped}"


@pytest.mark.parametrize("collection,filename", sorted(COLLECTIONS.items()))
def test_the_copy_matches_the_migration_pocketbase_actually_reads(collection, filename):
    """The copy here is byte-identical to the file in the repo PocketBase reads.

    Weaker than the live check above and worth having anyway: it names the *other* file
    when they diverge, which is the one an editor has to go and change. Skips when that
    repo is not on this machine, since neither repo may assume the other's path.
    """
    other = Path("/home/azureuser/personal_site/pocketbase/pb_migrations") / filename
    if not other.exists():
        pytest.skip(f"the PocketBase repo is not at {other.parent}")
    assert (MIGRATIONS_DIR / filename).read_text() == other.read_text(), (
        f"{filename} has drifted from {other}. PocketBase reads THAT one; this is the "
        f"copy. Edit there, then copy back here.")


def test_every_field_the_row_builder_writes_is_declared():
    """Close the loop from the other end: the builder's payload against the schema.

    A field the builder writes but no migration declares is the silent case — the PATCH
    returns 200 and the value is dropped.
    """
    declared = _declared_fields(MIGRATIONS_DIR / COLLECTIONS[ROW_COLLECTION])
    written = {
        "postgres_fixture_id", "publication", "kickoff_utc", "league_id", "league_name",
        "home_team_id", "home_team_name", "home_logo",
        "away_team_id", "away_team_name", "away_logo",
        "status", "home_goals", "away_goals", "penalty_home", "penalty_away",
        "computed_at",
    }
    assert written <= set(declared), f"written but undeclared: {written - set(declared)}"


def test_every_field_the_bundle_builder_writes_is_declared():
    declared = _declared_fields(MIGRATIONS_DIR / COLLECTIONS[BUNDLE_COLLECTION])
    written = {
        "postgres_fixture_id", "publication", "kickoff_utc", "status",
        "home_team_id", "away_team_id",
        "fixture", "events", "squad", "team_stats",
        "home_profile", "away_profile", "venue", "players",
        "home_form", "away_form", "football_computed_at",
    }
    assert written <= set(declared), f"written but undeclared: {written - set(declared)}"
