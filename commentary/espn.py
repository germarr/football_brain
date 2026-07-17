"""Cache-first ESPN summary client.

The public commentary page (espn.com/soccer/commentary/_/gameId/<id>) is a
client-side render and answers non-browser clients with HTTP 202 and an empty
body. The page's own data source is the site API below, which serves the same
payload as plain JSON, so that is what we call.

`all` in the path stands in for the league slug, so a gameId is the only
identifier needed. The response also carries boxscore / rosters / odds /
standings alongside commentary and keyEvents.

Cache-first in the style of football/client.py: every gameId is fetched once,
ever, and re-runs of the join cost zero requests.
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary"
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "espn-summary"
TIMEOUT = 30


def cache_path(game_id: str | int) -> Path:
    return CACHE_DIR / f"{game_id}.json"


def fetch_summary(game_id: str | int, *, refresh: bool = False) -> dict:
    """Return ESPN's summary payload for `game_id`, from cache unless `refresh`."""
    path = cache_path(game_id)
    if path.exists() and not refresh:
        return json.loads(path.read_text())

    response = requests.get(
        SUMMARY_URL, params={"event": str(game_id)}, timeout=TIMEOUT
    )
    response.raise_for_status()
    payload = response.json()

    if "commentary" not in payload:
        raise ValueError(
            f"gameId {game_id}: no commentary in payload "
            f"(keys: {sorted(payload)!r}) — wrong id, or match too old/not started?"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return payload
