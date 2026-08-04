"""Configuration and constants for the football fetcher.

Reads the API key from the project-root .env (`football_api=...`) without
depending on python-dotenv, and pins the target scope: FC Barcelona,
La Liga, season 2024 (the 2024/25 campaign) — see docs/adr/0001.
"""
from __future__ import annotations

import json
import os

# --- paths -----------------------------------------------------------------
# Sourced from `football.paths`, the single anchor for every path in this package
# (ADR 0031) — nothing here resolves its own location any more. Re-exported rather
# than referenced through `paths.` so that `config.RAW_DIR` stays the one name every
# caller (and every test monkeypatch) already uses.
from .paths import (  # noqa: E402
    COMPETITIONS_FILE,
    DB_PATH,
    ENV_FILE,
    RAW_DIR,
    REGISTRY_DIR,
    REPO_ROOT as ROOT,
)

# --- API -------------------------------------------------------------------
BASE_URL = "https://v3.football.api-sports.io"
KEY_HEADER = "x-apisports-key"

# Ultra plan: 150,000 requests/day, high per-minute cap.
DAILY_LIMIT = 150000
MIN_REQUEST_INTERVAL_S = 0.1  # ~600/min, well under the Ultra per-minute cap

# Fetch each player's cross-competition career history (players/teams)? Deferred
# for now — a separate ~18.5k-call run once the core match data is in (ADR 0006).
COLLECT_CAREERS = True

# --- target scope (docs/adr/0003–0010, 0019) -------------------------------
# Every Competition — league or cup — is defined by one entry in COMPETITIONS_FILE:
# `{league_id, name, seasons, calendar_year, type}`. `name` is our canonical name,
# overriding the provider's ambiguous one (the API calls BOTH Italy's and Brazil's
# league "Serie A"). `calendar_year` labels single-year seasons (Brasileirão 2024)
# vs straddling ones ("2024/25"); `type` is "league" or "cup" (ADR 0010) and both
# default sensibly when a hand-added entry omits them. Add a competition or a season
# by editing that file (or let `football.onboard.orchestrate`/`.cups` upsert into it).


def _load_competitions() -> list[dict]:
    """Read the sole competitions file (ADR 0019), raising if it is missing or bad.

    This is the only source of Competitions — there is no hardcoded fallback — so an
    absent or malformed file must stop the run loudly rather than yield an empty scope
    that `parse.build()` would silently rebuild `football.db` from.
    """
    if not COMPETITIONS_FILE.exists():
        raise FileNotFoundError(
            f"Missing competitions file {COMPETITIONS_FILE} — the sole source of "
            "every Competition (ADR 0019). It must exist and be committed."
        )
    try:
        data = json.loads(COMPETITIONS_FILE.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Malformed competitions file {COMPETITIONS_FILE}: {e}") from e
    if not isinstance(data, list):
        raise ValueError(
            f"Competitions file {COMPETITIONS_FILE} must be a JSON array of entries."
        )
    return data


def _build_competitions() -> tuple[list[dict], dict[int, str], set[int], dict[int, str]]:
    """Load every Competition and derive the module-level lookup tables."""
    raw = _load_competitions()
    comps = [{
        "league_id": r["league_id"], "name": r["name"],
        "seasons": list(r["seasons"]),
        "type": r.get("type", "league"),  # cups (ADR 0010) mark themselves "cup"
    } for r in raw]
    calendar = {r["league_id"] for r in raw if r.get("calendar_year")}
    names = {c["league_id"]: c["name"] for c in comps}
    types = {c["league_id"]: c["type"] for c in comps}
    return comps, names, calendar, types


# COMPETITIONS drives every collection/parse target; COMPETITION_NAMES is the
# league_id -> our canonical name map (disambiguates the two "Serie A"s);
# COMPETITION_TYPES maps league_id -> "league" | "cup" (ADR 0010).
COMPETITIONS, COMPETITION_NAMES, CALENDAR_YEAR_LEAGUES, COMPETITION_TYPES = _build_competitions()


def reload_competitions() -> None:
    """Re-read COMPETITIONS_FILE and refresh the module-level competition tables.

    Lets a process that just upserted a Competition (the orchestrator) act on it
    without re-importing (ADR 0019).
    """
    global COMPETITIONS, COMPETITION_NAMES, CALENDAR_YEAR_LEAGUES, COMPETITION_TYPES
    COMPETITIONS, COMPETITION_NAMES, CALENDAR_YEAR_LEAGUES, COMPETITION_TYPES = _build_competitions()

# FC Barcelona is only the default view for exploration, not a data boundary.
DEFAULT_TEAM_ID = 529
DEFAULT_TEAM_NAME = "FC Barcelona"

# --- continent (docs/adr/0016) ---------------------------------------------
# Country -> continent, extracted from docs/reference/leagues-by-continent.md (the
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
        'Philippines', 'Qatar', 'Saudi Arabia', 'Singapore', 'South Korea',
        'South-Korea',  # provider returns the hyphenated form for K League 1 (id 292)
        'Syria',
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


def _env_value(key: str) -> str | None:
    """First `key=value` in the .env, or None. First-wins, matching load_api_key()."""
    if not ENV_FILE.exists():
        return None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


def load_api_key() -> str:
    """Return the API key from the .env file (`football_api=...`)."""
    if not ENV_FILE.exists():
        raise FileNotFoundError(f"No .env at {ENV_FILE} (expected `football_api=...`)")
    key = _env_value("football_api")
    if key is None:
        raise KeyError("`football_api` not found in .env")
    return key


def load_pg_url() -> str:
    """The Published Store's Postgres URL (ADR 0027), from the environment or .env.

    Reads FOOTBALL_DATABASE_URL and never the bare PG* vars: those are defined twice
    in this project's .env (PGDATABASE=football, then PGDATABASE=content_analysis
    further down), so resolving them would silently target the wrong database.
    """
    url = os.environ.get("FOOTBALL_DATABASE_URL") or _env_value("FOOTBALL_DATABASE_URL")
    if not url:
        raise KeyError(
            "`FOOTBALL_DATABASE_URL` not found in the environment or .env.\n"
            "    Expected: postgresql://user:pass@host:5432/football?sslmode=require\n"
            "    (percent-encode the password: % -> %25, ! -> %21)"
        )
    return url
