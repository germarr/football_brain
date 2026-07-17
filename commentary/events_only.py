"""Read ESPN's `events_only` feed shape — the half of the feed `join.py` refuses.

ESPN serves two schemas. `join.py` handles the **narrative** one: ~110 lines per
match narrating fouls, corners, offsides and attempts, of which only the notable
subset is typed, so most Categories must be inferred by a model.

This module handles the other: ~15 lines per match covering goals, cards and
substitutions only, with a typed `play` object embedded on **every** row. That
difference is the whole point of a separate module, and it makes this the easier
of the two, not the harder:

- **No join.** The narrative shape ships `commentary` and `keyEvents` as two
  arrays with no shared id, and reconciling them is the hard problem `join.py`
  exists to solve. Here the type is already on the row. There is nothing to join.
- **No model.** Every row's `play.type.text` maps through `taxonomy.espn_category`
  — verified 362/362 across 25 cached matches, zero unmapped. So every Category
  from this shape is **asserted**, never inferred, and ingesting it costs nothing.
- **No `sequence`.** Rows are served oldest-first (verified: `time.value` is
  ascending in all 25 cached matches), so position *is* the ordering and sequence
  is assigned by index.

Two limits are structural, not bugs, and both are recorded in CONTEXT.md:

- **No player or team ids.** Athletes and teams arrive as `displayName` only —
  0 of 590 athlete references carry an id. The narrative shape has them for all
  673. Nothing here can be joined to a Player.
- **No field position.** 0 of 362 rows carry coordinates, so `field_position` is
  always null for this shape. Only scoring lines in the narrative shape have them.

What this shape emphatically does *not* mean is "less happened". It means less was
narrated: an events_only match reports zero fouls because fouls are never narrated
there. See CONTEXT.md's **Narration Coverage** before aggregating across both.
"""
from __future__ import annotations

from typing import Any

from .join import match_meta
from .taxonomy import espn_category


def _row(line: dict, sequence: int) -> dict:
    """One commentary row, already typed by the provider."""
    play = line.get("play") or {}
    time = line.get("time") or {}
    type_text = (play.get("type") or {}).get("text")

    category = espn_category(type_text)
    if category is None:
        # Every observed type maps (362/362). An unmapped one is a real gap: this
        # shape has no model to fall back on, so guessing would invent a label
        # nothing downstream could check. Refuse, and name the type.
        raise ValueError(
            f"events_only line {sequence} has ESPN type {type_text!r}, which "
            f"taxonomy.espn_category() cannot map. This shape has no classifier "
            f"fallback — add the type to ESPN_TYPE_TO_CATEGORY rather than guess."
        )

    return {
        "sequence": sequence,
        "minute": time.get("displayValue") or None,
        "clock_seconds": time.get("value"),
        "team": (play.get("team") or {}).get("displayName"),
        "category": category,
        "source": "espn_keyevent",
        "text": line.get("text"),
        "field_position": None,  # never present in this shape (0/362 observed)
    }


def build_events_only(payload: dict, game_id: str | int) -> dict:
    """Full document for an events_only match: metadata + typed lines.

    Mirrors `join.build_match` + `classify.build_synthesis` combined, minus the
    join report and the model — neither of which this shape needs.
    """
    lines: list[dict[str, Any]] = payload.get("commentary") or []
    return {
        **match_meta(payload, game_id),
        "model": None,  # nothing inferred: every Category is asserted by ESPN
        "events": [_row(line, i) for i, line in enumerate(lines)],
    }
