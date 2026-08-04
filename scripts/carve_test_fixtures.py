"""Carve a small, real slice of the raw cache into tests/fixtures/raw/ (ADR 0033).

The live cache is ~8 GB behind a machine-specific symlink (ADR 0002 addendum), so
`test_parse.py` cannot read it: a suite that skips when its input is missing is the
very failure mode ADR 0033 exists to eliminate. This carves a committed slice instead
— real provider responses, trimmed to a handful of fixtures.

Only the `fixtures` list is modified, and only by truncating its `response` array to
the chosen fixtures. Every other file is copied byte-for-byte, so the shapes the
parser sees are the shapes the provider actually returns.

Re-run it to refresh the slice:

    python scripts/carve_test_fixtures.py --league 140 --season 2024 --fixtures 6
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from football import config  # noqa: E402

DEST = REPO_ROOT / "tests" / "fixtures" / "raw"


def _src(endpoint: str, key: str) -> Path:
    return Path(config.RAW_DIR) / endpoint / f"{key}.json"


def _copy(endpoint: str, key: str, *, required: bool = False) -> int:
    """Copy one cache entry into the slice. Returns bytes copied (0 if absent)."""
    src = _src(endpoint, key)
    if not src.exists():
        if required:
            raise SystemExit(f"required cache entry missing: {src}")
        return 0
    dst = DEST / endpoint / f"{key}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league", type=int, default=140)
    ap.add_argument("--season", type=int, default=2024)
    ap.add_argument("--fixtures", type=int, default=6)
    args = ap.parse_args()

    if DEST.exists():
        shutil.rmtree(DEST)

    total = 0

    # 1. The competition's catalogue record (country, code, crest, flag — ADR 0015).
    total += _copy("leagues", f"id={args.league}", required=True)

    # 2. The season's fixture list, truncated to the chosen fixtures. Prefer finished
    #    ones: an unplayed fixture carries no events, players or statistics, so it
    #    would exercise none of the per-fixture parsing.
    fx_key = f"league={args.league}&season={args.season}"
    payload = json.loads(_src("fixtures", fx_key).read_text())
    played = [f for f in payload["response"]
              if (f.get("fixture", {}).get("status", {}).get("short") == "FT")]
    if len(played) < args.fixtures:
        raise SystemExit(f"only {len(played)} finished fixtures cached for {fx_key}")
    chosen = played[:args.fixtures]
    payload["response"] = chosen
    payload["results"] = len(chosen)
    out = DEST / "fixtures" / f"{fx_key}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    total += out.stat().st_size

    fixture_ids = [f["fixture"]["id"] for f in chosen]
    team_ids, player_ids = set(), set()
    for f in chosen:
        for side in ("home", "away"):
            team_ids.add(f["teams"][side]["id"])

    # 3. Per-fixture data.
    for fid in fixture_ids:
        for endpoint in ("fixtures_players", "fixtures_events", "fixtures_statistics"):
            total += _copy(endpoint, f"fixture={fid}")
        fp = _src("fixtures_players", f"fixture={fid}")
        if fp.exists():
            for block in json.loads(fp.read_text()).get("response") or []:
                team_ids.add(block["team"]["id"])
                for p in block.get("players") or []:
                    player_ids.add(p["player"]["id"])

    # 4. Player bios and career directories for everyone who appeared.
    for pid in sorted(player_ids):
        total += _copy("players", f"id={pid}&season={args.season}")
        total += _copy("players_teams", f"player={pid}")

    # 5. Team dossiers (ADR 0017) for both sides of every fixture.
    for tid in sorted(team_ids):
        total += _copy("teams", f"id={tid}")
        total += _copy("leagues", f"team={tid}")

    files = sum(1 for _ in DEST.rglob("*.json"))
    print(f"league={args.league} season={args.season} "
          f"fixtures={len(fixture_ids)} teams={len(team_ids)} players={len(player_ids)}")
    print(f"{files} files, {total / 1024:.0f} KiB in {DEST.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
