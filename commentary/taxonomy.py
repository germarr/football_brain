"""The category vocabulary, and ESPN's own types mapped onto it.

Derived from the 115 commentary lines of match 760514, not invented: every
category below was observed in that feed. ESPN's `keyEvents` types cover only
the notable subset (goal / card / sub / delay / period markers) — there is no
ESPN type for corner, foul, offside, or attempt, which is precisely the gap the
classifier fills.

`ESPN_TYPE_TO_CATEGORY` does double duty:

1. **Ground truth.** Where a line carries a joined keyEvent, its category comes
   from this map, never from the model. ESPN said it; we don't second-guess it.
2. **A free accuracy check.** Those same lines can be classified blind and the
   answers compared, which measures the classifier against real labels instead
   of vibes. See `commentary/classify.py --audit`.
"""
from __future__ import annotations

# Ordered roughly by how often they appear, for readable prompts.
CATEGORIES: list[tuple[str, str]] = [
    ("foul", "A player commits a foul. 'Foul by X (Team).'"),
    ("free_kick_won", "A player wins a free kick. 'X (Team) wins a free kick in the defensive half.'"),
    ("corner", "A corner is awarded. 'Corner, Team. Conceded by X.'"),
    ("offside", "A player is caught offside. 'Offside, Team. X is caught offside.'"),
    ("attempt_saved", "A shot on target saved by the keeper. 'Attempt saved.'"),
    ("attempt_missed", "A shot that goes wide or over. 'Attempt missed.'"),
    ("attempt_blocked", "A shot blocked by an outfield player. 'Attempt blocked.'"),
    ("attempt_woodwork", "A shot that hits the post or crossbar."),
    ("goal", "A goal from open play, by any method (header, volley, free-kick)."),
    (
        "own_goal",
        "A player scores against their own team. Attribute `team` to the team that "
        "BENEFITS (the scoring side), not the player's own team.",
    ),
    ("penalty_awarded", "A penalty is given, before it is taken. Includes 'Penalty conceded by X'."),
    ("penalty_scored", "A penalty kick that is scored."),
    ("penalty_missed", "A penalty kick that is missed or saved."),
    ("yellow_card", "A player is shown a yellow card."),
    ("red_card", "A player is sent off (straight red or second yellow)."),
    ("substitution", "A player is replaced. 'Substitution, Team. X replaces Y.'"),
    ("var_decision", "A VAR review or its outcome. 'VAR Decision: ...'"),
    ("delay", "Play is stopped or resumes — injury, drinks break, or other stoppage."),
    ("period_marker", "Start or end of a period: kickoff, halftime, second half, full time."),
    ("added_time", "Announcement of stoppage time. 'Fourth official has announced N minutes...'"),
    ("lineups", "Pre-match lineup or warm-up note."),
    ("other", "Anything that fits none of the above. Use sparingly."),
]

CATEGORY_NAMES: list[str] = [name for name, _ in CATEGORIES]

# ESPN keyEvent `type.text` -> our category. Authoritative where present.
#
# ESPN names goals by method: a sweep across 49 matches turned up "Goal",
# "Goal - Header", "Goal - Free-kick", and "Goal - Volley". Enumerating those is
# a losing game (there are surely more), so `espn_category()` also applies a
# "Goal - *" prefix rule. "Own Goal" is deliberately NOT a goal variant — its
# team attribution is inverted, so it gets its own category.
ESPN_TYPE_TO_CATEGORY: dict[str, str] = {
    "Goal": "goal",
    "Goal - Header": "goal",
    "Goal - Free-kick": "goal",
    "Goal - Volley": "goal",
    "Own Goal": "own_goal",
    "Penalty - Scored": "penalty_scored",
    "Penalty - Missed": "penalty_missed",
    "Penalty - Saved": "penalty_missed",
    "Yellow Card": "yellow_card",
    "Red Card": "red_card",
    "Second Yellow Card": "red_card",
    "Substitution": "substitution",
    "Start Delay": "delay",
    "End Delay": "delay",
    "Kickoff": "period_marker",
    "Halftime": "period_marker",
    "Start 2nd Half": "period_marker",
    "End Regular Time": "period_marker",
    "Match Ends": "period_marker",
}


def espn_category(type_text: str | None) -> str | None:
    """ESPN keyEvent type -> our category, or None if we don't recognise it.

    Falls back to a "Goal - <method>" prefix rule so an unseen goal variant
    resolves instead of silently defaulting to the model. Returning None is
    meaningful: the caller treats it as "no ground truth" and should surface it
    (see `unknown_espn_types`) rather than pretend the feed said nothing.
    """
    if not type_text:
        return None
    if type_text in ESPN_TYPE_TO_CATEGORY:
        return ESPN_TYPE_TO_CATEGORY[type_text]
    if type_text.startswith("Goal - "):  # Goal - Header / Free-kick / Volley / ...
        return "goal"
    return None


def unknown_espn_types(payload: dict) -> list[str]:
    """ESPN types in this payload that `espn_category` can't map.

    Not an error — those lines just fall through to the classifier. But an
    unknown type is a silent loss of ground truth, so callers report it.
    """
    seen = {
        (e.get("type") or {}).get("text")
        for e in payload.get("keyEvents") or []
        if e.get("text")
    }
    return sorted(t for t in seen if t and espn_category(t) is None)


def category_reference() -> str:
    """The category list as prompt text."""
    return "\n".join(f"- {name}: {desc}" for name, desc in CATEGORIES)
