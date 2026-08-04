"""URL-safe slug helpers. Behavior MUST match blog/la-cancha/src/lib/slugs.ts —
change both together, verify with the parity tests."""
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo


def team_name_to_slug(name: str) -> str:
    """Lowercase, ASCII, hyphenated. Idempotent."""
    decomposed = unicodedata.normalize("NFD", name)
    without_marks = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    lowered = without_marks.lower()
    ascii_only = re.sub(r"[^a-z0-9\s-]", "", lowered)
    hyphenated = re.sub(r"[\s-]+", "-", ascii_only)
    return hyphenated.strip("-")


def fixture_slug(
    home_slug: str,
    away_slug: str,
    kickoff_utc: datetime,
    display_timezone: str,
) -> str:
    """Compose home-vs-away-yyyy-mm-dd, using league TZ for the date suffix.

    kickoff_utc must be timezone-aware (UTC).
    """
    if kickoff_utc.tzinfo is None:
        raise ValueError("kickoff_utc must be a timezone-aware UTC datetime")
    local = kickoff_utc.astimezone(ZoneInfo(display_timezone))
    date_str = local.strftime("%Y-%m-%d")
    return f"{home_slug}-vs-{away_slug}-{date_str}"
