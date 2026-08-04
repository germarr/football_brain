"""Turn a fixture's raw round/matchday into a Spanish-language display label.

Data comes from api-football and uses English round names with inconsistent
capitalization ("Group Stage - 1" vs "Group stage - 1"). This module maps
those raw strings to canonical Spanish labels used by both the LLM prompt
builder (drafter) and the Astro post-page hero.

Mirrors blog/la-cancha/src/lib/stage.ts — keep the two in sync.
"""
from __future__ import annotations

import re

# Exact-match canonical knockout labels (case-insensitive lookup).
_KNOCKOUT_MAP = {
    "round of 32": "Dieciseisavos de Final",
    "round of 16": "Octavos de Final",
    "8th finals": "Octavos de Final",
    "quarter-finals": "Cuartos de Final",
    "quarter finals": "Cuartos de Final",
    "semi-finals": "Semifinales",
    "semi finals": "Semifinales",
    "final": "Final",
    "finals": "Final",
    "3rd place final": "Tercer Lugar",
    "third place final": "Tercer Lugar",
    "play-in round": "Play-In",
    "play-in round - finals": "Play-In Final",
}

# Prefix maps: "Apertura - Semi-finals" → "Semifinal (Apertura)".
_SEASON_PREFIXES = ("Apertura", "Clausura")
_CUP_PREFIXES = ("MLS Cup",)

_MATCHDAY_PATTERNS = [
    re.compile(r"^regular season\s*-\s*(\d+)$", re.IGNORECASE),
    re.compile(r"^group stage\s*-\s*(\d+)$", re.IGNORECASE),
]


def stage_label_es(round_str: str | None, matchday: int | None = None) -> str | None:
    """Return a Spanish display label for a fixture's stage, or None if
    nothing meaningful can be derived."""
    if not round_str:
        return None
    raw = round_str.strip()
    lower = raw.lower()

    # 1. Regular-season / group-stage matchday patterns.
    for pat in _MATCHDAY_PATTERNS:
        m = pat.match(raw)
        if m:
            n = m.group(1)
            if "group" in lower:
                return f"Fase de Grupos — Jornada {n}"
            return f"Jornada {n}"

    # 2. Season-prefixed rounds (Liga MX): "Apertura - N", "Apertura - Semi-finals".
    for pref in _SEASON_PREFIXES:
        p = f"{pref} - "
        if raw.startswith(p):
            tail = raw[len(p):].strip()
            # Matchday form: "Apertura - 15"
            if tail.isdigit():
                return f"{pref}, Jornada {tail}"
            # Knockout form: reuse the knockout map.
            canon = _knockout_lookup(tail)
            if canon:
                return f"{canon} ({pref})"
            # Play-In / Reclasificación / Play-offs: pass through in Spanish where possible.
            if tail.lower().startswith("play-in "):
                sub = tail[len("play-in "):].strip().lower()
                if sub == "semi-finals":
                    return f"Play-In Semifinal ({pref})"
                if sub == "final":
                    return f"Play-In Final ({pref})"
            if tail.lower() == "play-offs":
                return f"Play-offs ({pref})"
            if tail.lower().startswith("reclasific"):
                return f"Reclasificación ({pref})"
            # Unknown tail — pass through with prefix.
            return f"{tail} ({pref})"

    # 3. Cup-prefixed rounds (MLS Cup): "MLS Cup - Conference Semi-finals".
    for pref in _CUP_PREFIXES:
        p = f"{pref} - "
        if raw.startswith(p):
            tail = raw[len(p):].strip()
            canon = _knockout_lookup(tail)
            if canon:
                return f"{canon} ({pref})"
            if tail.lower().startswith("conference "):
                sub = tail[len("conference "):].strip()
                sub_canon = _knockout_lookup(sub)
                if sub_canon:
                    # Swap "Cuartos de Final" → "Cuartos de Conferencia" etc.
                    conf_map = {
                        "Cuartos de Final": "Cuartos de Conferencia",
                        "Semifinales": "Semifinal de Conferencia",
                        "Final": "Final de Conferencia",
                    }
                    label = conf_map.get(sub_canon, f"Conferencia — {sub_canon}")
                    return f"{label} ({pref})"
            if tail.lower() == "play-in round":
                return f"Play-In ({pref})"
            if tail.lower().startswith("round "):
                return f"{tail.title()} ({pref})"
            return f"{tail} ({pref})"

    # 4. Bare knockout label.
    canon = _knockout_lookup(raw)
    if canon:
        return canon

    # 5. Fallback: return raw string — reader still sees something.
    return raw


def _knockout_lookup(s: str) -> str | None:
    return _KNOCKOUT_MAP.get(s.strip().lower())
