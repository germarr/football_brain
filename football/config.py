"""Configuration and constants for the football fetcher.

Reads the API key from the project-root .env (`football_api=...`) without
depending on python-dotenv, and pins the target scope: FC Barcelona,
La Liga, season 2024 (the 2024/25 campaign) — see docs/adr/0001.
"""
from __future__ import annotations

import json
from pathlib import Path

# --- paths -----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
RAW_DIR = ROOT / "data" / "raw"          # Layer 1: raw response cache (ADR 0002)
DB_PATH = ROOT / "data" / "football.db"  # Layer 2: modeled SQLite store
# Leagues registered ad-hoc by `football.orchestrate` (ADR 0009). Merged into the
# built-in list below so parse.py and re-runs keep collecting them.
REGISTRY_FILE = ROOT / "data" / "competitions.json"

# --- API -------------------------------------------------------------------
BASE_URL = "https://v3.football.api-sports.io"
KEY_HEADER = "x-apisports-key"

# Ultra plan: 150,000 requests/day, high per-minute cap.
DAILY_LIMIT = 150000
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
# Every built-in is a domestic league (type "league"); cups (Champions League,
# World Cup) arrive only via the registry, collected by `football.cups` (ADR 0010).
_BUILTIN_COMPETITIONS = [
    {"league_id": 140, "name": "La Liga", "seasons": list(range(2015, 2026))},
    {"league_id": 39,  "name": "Premier League", "seasons": list(range(2015, 2026))},
    {"league_id": 135, "name": "Serie A", "seasons": list(range(2015, 2026))},
    {"league_id": 78,  "name": "Bundesliga", "seasons": list(range(2015, 2026))},
    {"league_id": 71,  "name": "Brasileirão", "seasons": list(range(2015, 2026))},
    {"league_id": 262, "name": "Liga MX", "seasons": list(range(2016, 2026))},
    # Argentina: per-player stats from 2016 (2015 empty); format varies by era —
    # Regular Season / Round / phases + playoffs — so its standings are per-phase
    # and playoff-excluded, not a clean single table (ADR 0008 supersedes 0006).
    {"league_id": 128, "name": "Liga Profesional Argentina", "seasons": list(range(2016, 2026))},
]
for _c in _BUILTIN_COMPETITIONS:
    _c.setdefault("type", "league")

# Built-in leagues whose season number is a single calendar year (2024), not a
# straddling "2024/25". Used for correct season labels. Argentina is a mixed case
# — it straddled pre-2020 but runs calendar-year since — and is labelled
# calendar-year to match its majority/most-recent seasons (ADR 0008); the stored
# season integer is always the provider's own and stays correct for API calls.
_BUILTIN_CALENDAR_YEAR = {71, 128}  # Brasileirão, Liga Profesional Argentina


def _load_registered() -> list[dict]:
    """Ad-hoc leagues from data/competitions.json (ADR 0009), or [] if none."""
    if not REGISTRY_FILE.exists():
        return []
    try:
        data = json.loads(REGISTRY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def _merge_competitions() -> tuple[list[dict], dict[int, str], set[int], dict[int, str]]:
    """Built-in leagues plus any registered one not already built in."""
    comps = [dict(c) for c in _BUILTIN_COMPETITIONS]
    known = {c["league_id"] for c in comps}
    calendar = set(_BUILTIN_CALENDAR_YEAR)
    for r in _load_registered():
        lid = r.get("league_id")
        if lid is None or lid in known:
            continue  # a built-in always wins over a registry duplicate
        known.add(lid)
        comps.append({
            "league_id": lid, "name": r["name"], "seasons": list(r["seasons"]),
            "type": r.get("type", "league"),  # cups (ADR 0010) mark themselves "cup"
        })
        if r.get("calendar_year"):
            calendar.add(lid)
    names = {c["league_id"]: c["name"] for c in comps}
    types = {c["league_id"]: c.get("type", "league") for c in comps}
    return comps, names, calendar, types


# COMPETITIONS drives every collection/parse target; COMPETITION_NAMES is the
# league_id -> our canonical name map (disambiguates the two "Serie A"s);
# COMPETITION_TYPES maps league_id -> "league" | "cup" (ADR 0010).
COMPETITIONS, COMPETITION_NAMES, CALENDAR_YEAR_LEAGUES, COMPETITION_TYPES = _merge_competitions()


def reload_competitions() -> None:
    """Re-read the registry and refresh the module-level competition tables.

    Lets a process that just wrote data/competitions.json (the orchestrator) act
    on the new league without re-importing (ADR 0009).
    """
    global COMPETITIONS, COMPETITION_NAMES, CALENDAR_YEAR_LEAGUES, COMPETITION_TYPES
    COMPETITIONS, COMPETITION_NAMES, CALENDAR_YEAR_LEAGUES, COMPETITION_TYPES = _merge_competitions()

# FC Barcelona is only the default view for exploration, not a data boundary.
DEFAULT_TEAM_ID = 529
DEFAULT_TEAM_NAME = "FC Barcelona"

# --- continent (docs/adr/0016) ---------------------------------------------
# Country -> continent, extracted from context/leagues-by-continent.md (the
# API /leagues catalogue grouped by continent). Grouped by continent for
# readability, inverted into COUNTRY_CONTINENT below. The provider gives no
# continent field, so this static table derives it from a Competition's
# country name (ADR 0016). "World" (every continental/international cup) maps
# to "International / Intercontinental", matching the source doc.
_CONTINENT_COUNTRIES: dict[str, list[str]] = {
    'Europe': [
        'Albania', 'Andorra', 'Armenia', 'Austria', 'Azerbaijan', 'Belarus', 'Belgium',
        'Bosnia', 'Bulgaria', 'Crimea', 'Croatia', 'Cyprus', 'Czech Republic', 'Denmark',
        'England', 'Estonia', 'Faroe Islands', 'Finland', 'France', 'Georgia', 'Germany',
        'Gibraltar', 'Greece', 'Hungary', 'Iceland', 'Ireland', 'Italy', 'Kosovo',
        'Latvia', 'Liechtenstein', 'Lithuania', 'Luxembourg', 'Macedonia', 'Malta',
        'Moldova', 'Montenegro', 'Netherlands', 'Northern Ireland', 'Norway', 'Poland',
        'Portugal', 'Romania', 'Russia', 'San Marino', 'Scotland', 'Serbia', 'Slovakia',
        'Slovenia', 'Spain', 'Sweden', 'Switzerland', 'Turkey', 'Ukraine', 'Wales',
    ],
    'South America': [
        'Argentina', 'Bolivia', 'Brazil', 'Chile', 'Colombia', 'Ecuador', 'Paraguay',
        'Peru', 'Suriname', 'Uruguay', 'Venezuela',
    ],
    'North & Central America': [
        'Antigua And Barbuda', 'Aruba', 'Barbados', 'Belize', 'Bermuda', 'Canada',
        'Costa Rica', 'Cuba', 'Curacao', 'Dominican Republic', 'El Salvador', 'Grenada',
        'Guadeloupe', 'Guatemala', 'Haiti', 'Honduras', 'Jamaica', 'Mexico', 'Nicaragua',
        'Panama', 'Trinidad And Tobago', 'USA',
    ],
    'Africa': [
        'Algeria', 'Angola', 'Benin', 'Botswana', 'Burkina Faso', 'Burundi', 'Cameroon',
        'Congo', 'Congo DR', 'Egypt', 'Eswatini', 'Ethiopia', 'Gabon', 'Gambia', 'Ghana',
        'Guinea', 'Ivory Coast', 'Kenya', 'Lesotho', 'Liberia', 'Libya', 'Malawi', 'Mali',
        'Mauritania', 'Mauritius', 'Morocco', 'Namibia', 'Nigeria', 'Rwanda', 'Senegal',
        'Somalia', 'South Africa', 'Sudan', 'Tanzania', 'Togo', 'Tunisia', 'Uganda',
        'Zambia', 'Zimbabwe',
    ],
    'Asia': [
        'Bahrain', 'Bangladesh', 'Bhutan', 'Cambodia', 'China', 'Chinese Taipei',
        'Hong Kong', 'India', 'Indonesia', 'Iran', 'Iraq', 'Israel', 'Japan', 'Jordan',
        'Kazakhstan', 'Kuwait', 'Kyrgyzstan', 'Laos', 'Lebanon', 'Macao', 'Malaysia',
        'Maldives', 'Mongolia', 'Myanmar', 'Nepal', 'Oman', 'Pakistan', 'Palestine',
        'Philippines', 'Qatar', 'Saudi Arabia', 'Singapore', 'South Korea', 'Syria',
        'Tajikistan', 'Thailand', 'Turkmenistan', 'United Arab Emirates', 'Uzbekistan',
        'Vietnam', 'Yemen',
    ],
    'Oceania': [
        'Australia', 'Fiji', 'New Zealand',
    ],
    'International / Intercontinental': [
        'World',
    ],
}
COUNTRY_CONTINENT: dict[str, str] = {
    country: continent
    for continent, countries in _CONTINENT_COUNTRIES.items()
    for country in countries
}


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
