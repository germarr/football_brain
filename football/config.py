"""Configuration and constants for the football fetcher.

Reads the API key from the project-root .env (`football_api=...`) without
depending on python-dotenv, and pins the target scope: FC Barcelona,
La Liga, season 2024 (the 2024/25 campaign) — see docs/adr/0001.
"""
from __future__ import annotations

from pathlib import Path

# --- paths -----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw"          # Layer 1: raw response cache (ADR 0002)
DB_PATH = ROOT / "data" / "football.db"  # Layer 2: modeled SQLite store

# --- API -------------------------------------------------------------------
BASE_URL = "https://v3.football.api-sports.io"
KEY_HEADER = "x-apisports-key"

# Ultra plan: 75,000 requests/day, high per-minute cap.
DAILY_LIMIT = 75000
MIN_REQUEST_INTERVAL_S = 0.1  # ~600/min, well under the Ultra per-minute cap

# Fetch each player's cross-competition career history (players/teams)? Deferred
# for now — a separate ~18.5k-call run once the core match data is in (ADR 0006).
COLLECT_CAREERS = True

# --- target scope (docs/adr/0003, 0004, 0005, 0006) ------------------------
# Each competition and the seasons to collect. Add a competition or a season by
# editing this list; collection and parsing iterate the (league, season) targets.
# `name` is our canonical competition name — it overrides the provider's, which
# is ambiguous (the API calls BOTH Italy's and Brazil's league "Serie A").
# Per-player stats begin at 2015/16 for the European leagues and Brazil.
COMPETITIONS = [
    {"league_id": 140, "name": "La Liga", "seasons": list(range(2015, 2026))},
    {"league_id": 39,  "name": "Premier League", "seasons": list(range(2015, 2026))},
    {"league_id": 135, "name": "Serie A", "seasons": list(range(2015, 2026))},
    {"league_id": 78,  "name": "Bundesliga", "seasons": list(range(2015, 2026))},
    {"league_id": 71,  "name": "Brasileirão", "seasons": list(range(2015, 2026))},
    {"league_id": 262, "name": "Liga MX", "seasons": list(range(2016, 2026))},
    # Argentina (128) deferred: no 2015 player stats + inconsistent format (ADR 0006).
]

# league_id -> our canonical name (disambiguates the two "Serie A"s).
COMPETITION_NAMES = {c["league_id"]: c["name"] for c in COMPETITIONS}

# South American leagues run on a single calendar year, so their season number is
# that year (2024), not a straddling "2024/25". Used for correct season labels.
CALENDAR_YEAR_LEAGUES = {71}  # Brasileirão

# FC Barcelona is only the default view for exploration, not a data boundary.
DEFAULT_TEAM_ID = 529
DEFAULT_TEAM_NAME = "FC Barcelona"


def targets():
    """Yield (league_id, competition_name, season) for every collection target."""
    for c in COMPETITIONS:
        for season in c["seasons"]:
            yield c["league_id"], c["name"], season


def load_api_key() -> str:
    """Return the API key from the .env file (`football_api=...`)."""
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"No .env at {ENV_FILE} (expected `football_api=...`)")
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == "football_api":
            return value.strip().strip('"').strip("'")
    raise KeyError("`football_api` not found in .env")
