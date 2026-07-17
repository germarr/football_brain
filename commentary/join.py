"""Join ESPN's typed `keyEvents` onto its untyped `commentary` lines.

ESPN ships two parallel arrays that describe the same match:

- `commentary` — every line, but only `sequence` / `time` / `text`. No types,
  no players, no teams.
- `keyEvents` — the notable subset, fully typed: goal / card / sub / VAR, with
  `participants`, `team`, `period`, and shot coordinates — but no `sequence`,
  so it does not say *which* commentary line it belongs to.

There is no shared id, so the join is on content. Two properties of the feed,
both verified against a real match (see README.md), decide the strategy:

1. Every text-bearing keyEvent's `text` appears **verbatim** in some commentary
   line — ESPN generates both from the same string.
2. `(text, clock)` is unique across commentary, while `text` alone is not:
   generic lines ("Offside, France.") repeat across the match.

So the primary key is `(text, clock)`. It is exact and collision-free.

The one place it fails is **stoppage time**: keyEvents clamps `clock.value` to
the period boundary (2700.0 = 45:00, 5400.0 = 90:00) while commentary counts
real elapsed seconds through stoppage. An End Delay at 90'+8' carries clock
5400.0 in keyEvents and 5850.0 in commentary. Both render the same
`displayValue` ("90'+8'"), so the disagreement is invisible unless you compare
the numbers, and a clock-equality join silently drops every stoppage-time event.

So the fallback is `(text, displayValue)` — the minute *string*, which both
sides agree on even when the numeric clocks don't.

Deliberately *not* fallbacks:

- **Minute alone.** Several key events routinely share a minute (this feed has
  two different substitutions at each of 72', 78', 84'), so a minute join
  assigns every one of them to every line in that minute — wrong rows, silently.
- **Text alone.** Tried and rejected: generic lines repeat. "Delay over. They
  are ready to continue." appears at 52', 80' and 90'+8' in match 401873984, so
  a text-only fallback is ambiguous exactly when it is needed.

keyEvents with no `text` at all are never joined — there is nothing to match on
— and are reported as `structural_unjoined`. They are of two kinds:

- true structural markers (Kickoff, Start 2nd Half);
- the **mirror twin of a delay**. ESPN records each delay twice, once per team,
  with distinct ids: one copy carries the text and the team responsible, the
  twin names the opposing team and carries no text. The text-bearing copy joins
  normally, so the delay is not lost — only its redundant twin is skipped.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

# Feed quirk: keyEvents clamps these to the end of regulation while commentary
# keeps counting through stoppage. Used only to explain a fallback, not to match.
_PERIOD_BOUNDARY_CLOCKS = {2700.0, 5400.0}


def _norm(text: str) -> str:
    """Collapse whitespace for matching. Deliberately conservative: no case or
    punctuation folding, which could merge distinct lines into a false match."""
    return re.sub(r"\s+", " ", (text or "").strip())


def _event_row(event: dict) -> dict:
    """Flatten a keyEvent into the typed fields worth keeping."""
    return {
        "id": event.get("id"),
        "type": (event.get("type") or {}).get("text"),
        "type_slug": (event.get("type") or {}).get("type"),
        "period": (event.get("period") or {}).get("number"),
        "minute": (event.get("clock") or {}).get("displayValue") or None,
        "clock_seconds": (event.get("clock") or {}).get("value"),
        "team": (event.get("team") or {}).get("displayName"),
        "team_id": (event.get("team") or {}).get("id"),
        "scoring_play": event.get("scoringPlay"),
        "shootout": event.get("shootout"),
        "wallclock": event.get("wallclock"),
        "players": [
            {
                "id": (p.get("athlete") or {}).get("id"),
                "name": (p.get("athlete") or {}).get("displayName"),
            }
            for p in event.get("participants") or []
        ],
        "field_position": (
            {
                "x": event.get("fieldPositionX"),
                "y": event.get("fieldPositionY"),
                "x2": event.get("fieldPosition2X"),
                "y2": event.get("fieldPosition2Y"),
                "goal_y": event.get("goalPositionY"),
            }
            if event.get("fieldPositionX") is not None
            else None
        ),
        "text": event.get("text"),
    }


def narration_coverage(payload: dict) -> str:
    """Which feed shape ESPN served for this match — `narrative` or `events_only`.

    ESPN publishes two schemas and does not label which one you got. The tell is
    `sequence`: the narrative shape numbers every line, the events-only shape has
    no `sequence` at all and instead embeds a typed `play` object on each row.
    Note that a *narrative* row may also carry `play`, so `play`'s presence proves
    nothing — only the absence of `sequence` does.
    """
    commentary = payload.get("commentary") or []
    if not commentary:
        raise ValueError("payload has no commentary lines — cannot infer feed shape.")
    return "events_only" if any("sequence" not in c for c in commentary) else "narrative"


def join_commentary(payload: dict) -> dict:
    """Attach typed keyEvents to commentary lines.

    Returns `{commentary, key_events, join}` where each commentary row carries
    `key_event` (the typed event, or None) and `join` reports how every
    text-bearing keyEvent was resolved. Raises on an ambiguous match rather
    than guessing.
    """
    commentary = payload.get("commentary") or []
    key_events = payload.get("keyEvents") or []

    missing_seq = [c for c in commentary if "sequence" not in c]
    if missing_seq:
        raise ValueError(
            f"{len(missing_seq)}/{len(commentary)} commentary rows have no 'sequence'. "
            "This is ESPN's coverage-light feed shape, which carries a typed `play` "
            "object on each row instead — a different schema this module does not "
            "handle. See README 'Known limits'."
        )

    rows = [
        {
            "sequence": c["sequence"],
            "minute": (c.get("time") or {}).get("displayValue") or None,
            "clock_seconds": (c.get("time") or {}).get("value"),
            "text": c.get("text"),
            "key_event": None,
        }
        for c in commentary
    ]
    rows.sort(key=lambda r: r["sequence"])  # served oldest-first; sort anyway
    by_seq = {r["sequence"]: r for r in rows}

    exact: dict[tuple[str, Any], list[int]] = defaultdict(list)
    by_text_minute: dict[tuple[str, Any], list[int]] = defaultdict(list)
    for r in rows:
        exact[(_norm(r["text"]), r["clock_seconds"])].append(r["sequence"])
        by_text_minute[(_norm(r["text"]), r["minute"])].append(r["sequence"])

    structural, matched, unmatched = [], [], []
    strategies: dict[str, int] = defaultdict(int)

    for event in key_events:
        row = _event_row(event)
        if not _norm(row["text"]):
            structural.append(row)  # Kickoff / delays / half markers
            continue

        text = _norm(row["text"])
        hits = exact.get((text, row["clock_seconds"]), [])
        strategy = "exact(text,clock)"

        if not hits:
            # Stoppage-time clamp: numeric clocks disagree, minute strings agree.
            hits = by_text_minute.get((text, row["minute"]), [])
            strategy = "fallback(text,minute)"

        if len(hits) > 1:
            raise ValueError(
                f"ambiguous keyEvent {row['id']} ({row['type']} @ {row['minute']}): "
                f"matches commentary sequences {hits}. Refusing to guess."
            )
        if not hits:
            unmatched.append(row)
            continue

        seq = hits[0]
        if by_seq[seq]["key_event"] is not None:
            raise ValueError(
                f"commentary sequence {seq} claimed twice — "
                f"by {by_seq[seq]['key_event']['id']} and {row['id']}."
            )
        by_seq[seq]["key_event"] = row
        strategies[strategy] += 1
        matched.append(row)

    return {
        "commentary": rows,
        "key_events": [_event_row(e) for e in key_events],
        "join": {
            "commentary_lines": len(rows),
            "key_events_total": len(key_events),
            "key_events_with_text": len(matched) + len(unmatched),
            "matched": len(matched),
            "by_strategy": dict(strategies),
            "structural_unjoined": [
                {"type": r["type"], "minute": r["minute"]} for r in structural
            ],
            "unmatched": [
                {"id": r["id"], "type": r["type"], "minute": r["minute"]}
                for r in unmatched
            ],
        },
    }


def match_meta(payload: dict, game_id: str | int) -> dict:
    """Match identity and metadata, with no commentary and no cost.

    Split out from `build_match` because ingest needs identity *before* it decides
    to spend anything: a `--fixture-id` is verified against this, so a typo is
    refused before the classifier runs rather than after it has been paid for.
    Both feed shapes share it — the header is identical either way.
    """
    header = payload.get("header") or {}
    competition = (header.get("competitions") or [{}])[0]
    competitors = {c.get("homeAway"): c for c in competition.get("competitors") or []}

    def side(which: str) -> dict:
        c = competitors.get(which) or {}
        return {
            "team": (c.get("team") or {}).get("displayName"),
            "team_id": (c.get("team") or {}).get("id"),
            "score": c.get("score"),
        }

    return {
        "source": f"{'site.api.espn.com/apis/site/v2/sports/soccer/all/summary'}?event={game_id}",
        "game_id": str(game_id),
        "league": (header.get("league") or {}).get("name"),
        "date": competition.get("date"),
        "venue": ((payload.get("gameInfo") or {}).get("venue") or {}).get("fullName"),
        **match_status(payload),
        "home": side("home"),
        "away": side("away"),
    }


def build_match(payload: dict, game_id: str | int) -> dict:
    """Full output document: match metadata + joined commentary + key events."""
    return {**match_meta(payload, game_id), **join_commentary(payload)}


def match_status(payload: dict) -> dict:
    """ESPN's view of whether this match is over.

    Three fields, and the distinction matters. `status` is a display string
    ("Full Time"); `status_name` is the stable enum (`STATUS_FULL_TIME`);
    `completed` is ESPN's own computed boolean.

    Ingest gates on `completed`, never on `status`. Every match observed so far is
    STATUS_FULL_TIME, so the vocabulary of finished states is unknown — an
    extra-time or shootout match certainly reports something else, and matching on
    the display string would silently refuse exactly the most interesting games.
    `completed` covers them without guessing at names we have never seen.
    """
    competition = ((payload.get("header") or {}).get("competitions") or [{}])[0]
    status_type = (competition.get("status") or {}).get("type") or {}
    return {
        "status": status_type.get("description"),
        "status_name": status_type.get("name"),
        "completed": bool(status_type.get("completed")),
    }
