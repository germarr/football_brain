"""Classify commentary lines into standardized buckets with an LLM.

Reads the joined output of `commentary.join` and produces, per line:
`minute`, `team`, `category` — plus the line's `sequence` so results can be
reattached to the source.

Design decisions worth knowing before you trust the output:

- **ESPN's own types win.** Where a line carries a joined keyEvent, its category
  and team come from ESPN via `taxonomy.espn_category`, not the model. Only the
  ~88 untyped lines are actually sent for classification. That is both cheaper
  and strictly more accurate than asking a model to re-derive a label the feed
  already states.

- **The model can't invent a team.** `team` is a JSON-schema enum of the two
  teams in this match plus "none". The category is likewise an enum. Structured
  outputs enforce both at the decode layer, so a malformed answer is impossible
  rather than merely unlikely.

- **Lines are batched.** Commentary lines are one sentence each, so one request
  per line would be ~115 requests to answer ~115 sentences. Lines are sent
  CHUNK_SIZE at a time and matched back by `sequence`.

- **…so `sequence` is enumerated too, and every chunk is checked before it
  writes.** The batching makes `sequence` the join key back to the line, which
  makes a wrong one dangerous in a way a wrong category is not: it does not lose
  a label, it *moves* it onto a different line — and onto a line another chunk
  may already have labelled correctly. Match 760507 did exactly this: chunk 2
  returned 20 sequences belonging to chunk 1 and clobbered them, and the run only
  failed because some lines were then left over. A shifted-but-complete answer
  would have passed silently with every label wrong. So each chunk enumerates its
  own sequences in the schema, and the response is checked for foreign,
  duplicated and omitted sequences before a single row is written.

- **The model never sees the answer key.** `--audit` re-classifies the lines
  ESPN already typed and reports agreement, giving a real accuracy number on
  real labels. Run it before trusting a new match or a prompt change.

Model: claude-haiku-4-5 (cheapest current model; this is templated
one-sentence text, which is the workload Haiku is for).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import anthropic

from .espn import fetch_summary
from .join import build_match
from .taxonomy import (
    CATEGORY_NAMES,
    category_reference,
    espn_category,
    unknown_espn_types,
)

MODEL = "claude-haiku-4-5"
CHUNK_SIZE = 25
MAX_TOKENS = 4096

# A chunk that comes back malformed is usually just a bad roll, not a bad prompt:
# the same chunk of match 760507 returned 4 of 25 lines on one attempt and a
# clean 25 of 25 on the next, same input. Retrying is not papering over the
# failure — the response is fully validated first, and a retry only happens when
# it was going to be rejected anyway. It matters because chunks are paid for one
# at a time: without it, a single bad roll on the last chunk throws away the
# spend on every chunk before it and makes the operator re-run the lot.
MAX_ATTEMPTS = 3
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

SYSTEM = """You label football (soccer) match commentary lines with one category each.

The commentary is machine-generated from templates, so the wording is highly \
regular — classify on the leading phrase and do not overthink it.

Categories:
{categories}

Rules:
- Exactly one category per line. Every input sequence must appear in your output.
- `team` is the team the event is ATTRIBUTED to, which is not always the team \
named first. For "Corner, France. Conceded by Pau Cubarsi." the corner is \
France's, so team is France. For "Foul by Adrien Rabiot (France)." the foul is \
Rabiot's, so team is France.
- Use "none" for team only when the line names no team and implies none \
(e.g. "First Half begins.", "Fourth official has announced 6 minutes of added time.").
- Do not guess. If a line genuinely fits nothing, use "other"."""


def load_api_key() -> str:
    """ANTHROPIC_API_KEY from the environment, else from the repo .env."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_API_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError(f"ANTHROPIC_API_KEY not in the environment or {ENV_PATH}")


def _schema(teams: list[str], sequences: list[int]) -> dict:
    """Response schema. Enums make a malformed answer undecodable, not merely
    unlikely — and that applies to `sequence` as much as to category and team.

    `sequence` is the join key back to the commentary line, so a wrong one does
    not just lose a label, it *moves* it onto another line. Enumerating this
    chunk's sequences means the model cannot return a line number it was not
    given: renumbering 0..N-1, or echoing another chunk's ids, is rejected at
    the decode layer.
    """
    return {
        "type": "json_schema",
        "schema": {
            "type": "object",
            "properties": {
                "classifications": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sequence": {"type": "integer", "enum": sequences},
                            "category": {"type": "string", "enum": CATEGORY_NAMES},
                            "team": {"type": "string", "enum": [*teams, "none"]},
                        },
                        "required": ["sequence", "category", "team"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["classifications"],
            "additionalProperties": False,
        },
    }


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


class _ChunkError(RuntimeError):
    """One attempt at one chunk came back unusable. Retryable; never written."""


def _classify_chunk(
    client: anthropic.Anthropic,
    chunk: list[dict],
    teams: list[str],
    system: str,
    usage: dict,
) -> list[dict]:
    """One request for one chunk, fully validated. Raises `_ChunkError` if the
    answer is unusable — the caller retries rather than writing it.

    The validation is the point. `sequence` is the join key back to the line, so
    a wrong one does not lose a label, it *moves* it onto a different line —
    possibly one another chunk already labelled correctly. Match 760507 did
    exactly that: a chunk returned 20 sequences belonging to an earlier chunk and
    clobbered them, and the run only failed because some lines were left over. A
    shifted-but-complete answer would have passed with every label silently wrong.
    """
    expected = [r["sequence"] for r in chunk]
    payload = [{"sequence": r["sequence"], "text": r["text"]} for r in chunk]

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        output_config={"format": _schema(teams, expected)},
        messages=[
            {
                "role": "user",
                "content": "Classify every line:\n"
                + json.dumps(payload, ensure_ascii=False, indent=1),
            }
        ],
    )
    # Count every attempt, including rejected ones: they were paid for.
    usage["input_tokens"] += response.usage.input_tokens
    usage["output_tokens"] += response.usage.output_tokens
    usage["requests"] += 1

    if response.stop_reason == "max_tokens":
        raise _ChunkError(f"hit max_tokens ({MAX_TOKENS}) — answer was truncated.")

    text = next(b.text for b in response.content if b.type == "text")
    items = json.loads(text)["classifications"]
    got = [item["sequence"] for item in items]

    foreign = sorted(set(got) - set(expected))
    if foreign:
        raise _ChunkError(
            f"returned sequence(s) {foreign[:10]} not in this chunk "
            f"({expected[0]}..{expected[-1]}) — they would overwrite other lines."
        )
    duplicated = sorted({s for s in got if got.count(s) > 1})
    if duplicated:
        raise _ChunkError(f"returned duplicate sequence(s) {duplicated[:10]}.")
    absent = sorted(set(expected) - set(got))
    if absent:
        raise _ChunkError(
            f"omitted {len(absent)} of {len(expected)} line(s): {absent[:10]}."
        )
    return items


def classify_lines(
    client: anthropic.Anthropic, lines: list[dict], teams: list[str], *, verbose: bool = True
) -> tuple[dict[int, dict], dict]:
    """Classify `lines` (each needing `sequence` + `text`). Returns
    ({sequence: {category, team}}, usage_totals)."""
    out: dict[int, dict] = {}
    usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    system = SYSTEM.format(categories=category_reference())

    for n, chunk in enumerate(_chunks(lines, CHUNK_SIZE), 1):
        expected = [r["sequence"] for r in chunk]
        items = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                items = _classify_chunk(client, chunk, teams, system, usage)
                break
            except _ChunkError as error:
                if attempt == MAX_ATTEMPTS:
                    raise RuntimeError(
                        f"chunk {n} failed {MAX_ATTEMPTS} attempts. Last: {error} "
                        f"Nothing was written. Lower CHUNK_SIZE ({CHUNK_SIZE}) if it "
                        f"persists — a smaller chunk is an easier answer to get right."
                    ) from None
                print(
                    f"  chunk {n} attempt {attempt}/{MAX_ATTEMPTS} rejected "
                    f"({error}) — retrying",
                    file=sys.stderr,
                )

        # Only a fully validated chunk reaches this point, so no chunk can ever
        # write onto another chunk's lines.
        for item in items:
            out[item["sequence"]] = {"category": item["category"], "team": item["team"]}
        if verbose:
            print(
                f"  chunk {n}: {len(chunk)} lines -> {len(out)} classified",
                file=sys.stderr,
            )

    missing = [r["sequence"] for r in lines if r["sequence"] not in out]
    if missing:
        raise RuntimeError(
            f"model omitted {len(missing)} line(s): {missing[:10]}. "
            f"Not filling them in silently — re-run or lower CHUNK_SIZE."
        )
    return out, usage


def audit(client: anthropic.Anthropic, match: dict) -> dict:
    """Blind-classify the lines ESPN already typed; report agreement.

    This is the only honest accuracy number available — it compares the model
    against real labels rather than against our expectations.
    """
    teams = [match["home"]["team"], match["away"]["team"]]
    labeled = [
        r
        for r in match["commentary"]
        if r["key_event"] and espn_category(r["key_event"]["type"])
    ]
    if not labeled:
        return {"checked": 0}

    predictions, usage = classify_lines(client, labeled, teams, verbose=False)
    disagreements = []
    for row in labeled:
        truth = espn_category(row["key_event"]["type"])
        got = predictions[row["sequence"]]["category"]
        if got != truth:
            disagreements.append(
                {
                    "sequence": row["sequence"],
                    "minute": row["minute"],
                    "espn": truth,
                    "model": got,
                    "text": row["text"],
                }
            )
    checked = len(labeled)
    return {
        "checked": checked,
        "agreed": checked - len(disagreements),
        "accuracy": round((checked - len(disagreements)) / checked, 4),
        "disagreements": disagreements,
        "usage": usage,
    }


def build_synthesis(client: anthropic.Anthropic, match: dict, *, verbose: bool = True) -> dict:
    """The output document: every line as {minute, team, category}."""
    teams = [match["home"]["team"], match["away"]["team"]]
    rows = match["commentary"]

    truth: dict[int, dict] = {}
    for r in rows:
        ke = r["key_event"]
        cat = espn_category(ke["type"]) if ke else None
        if cat:
            truth[r["sequence"]] = {
                "category": cat,
                "team": ke["team"] or "none",
            }

    todo = [r for r in rows if r["sequence"] not in truth]
    if verbose:
        print(
            f"{len(truth)} lines from ESPN keyEvents (ground truth), "
            f"{len(todo)} to classify with {MODEL}",
            file=sys.stderr,
        )
    predicted, usage = ({}, {"input_tokens": 0, "output_tokens": 0, "requests": 0})
    if todo:
        predicted, usage = classify_lines(client, todo, teams, verbose=verbose)

    events = []
    for r in rows:
        seq = r["sequence"]
        src = "espn_keyevent" if seq in truth else "llm"
        label = truth.get(seq) or predicted[seq]
        events.append(
            {
                "sequence": seq,
                "minute": r["minute"],
                "clock_seconds": r["clock_seconds"],
                "team": None if label["team"] == "none" else label["team"],
                "category": label["category"],
                "source": src,
                "text": r["text"],
            }
        )

    counts: dict[str, int] = {}
    for e in events:
        counts[e["category"]] = counts.get(e["category"], 0) + 1

    return {
        "game_id": match["game_id"],
        "league": match["league"],
        "date": match["date"],
        "venue": match["venue"],
        "home": match["home"],
        "away": match["away"],
        "model": MODEL,
        "events": events,
        "summary": {
            "total": len(events),
            "from_espn": len(truth),
            "from_llm": len(predicted),
            "by_category": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "usage": usage,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="commentary.classify", description=__doc__)
    parser.add_argument("game_id", help="ESPN gameId, e.g. 760514")
    parser.add_argument("-o", "--out", help="write JSON here (default: stdout)")
    parser.add_argument("--refresh", action="store_true", help="bypass the raw cache")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="blind-classify ESPN's already-typed lines and report accuracy; classify nothing else",
    )
    args = parser.parse_args(argv)

    client = anthropic.Anthropic(api_key=load_api_key())
    match = build_match(fetch_summary(args.game_id, refresh=args.refresh), args.game_id)

    if args.audit:
        report = audit(client, match)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if report.get("checked"):
            print(
                f"\naccuracy vs ESPN ground truth: {report['agreed']}/{report['checked']} "
                f"({report['accuracy']:.1%})",
                file=sys.stderr,
            )
        return 0

    doc = build_synthesis(client, match)
    text = json.dumps(doc, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text)
        s = doc["summary"]
        print(
            f"wrote {args.out}: {s['total']} events "
            f"({s['from_espn']} ESPN + {s['from_llm']} {MODEL}), "
            f"{s['usage']['requests']} requests, "
            f"{s['usage']['input_tokens']}in/{s['usage']['output_tokens']}out tokens",
            file=sys.stderr,
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
