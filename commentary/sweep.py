"""Audit the classifier across several matches at once.

    python -m commentary.sweep 760514 401873984 401873998

For each match: blind-classify the lines ESPN already typed and report
agreement, plus any ESPN types this taxonomy cannot map. An unmapped type is a
silent loss of ground truth — those lines fall through to the model and vanish
from the audit's denominator, which is exactly how a passing accuracy number
can hide a shrinking sample. So it is reported per match, loudly.

Discovering candidate gameIds (any finished match, all competitions):

    https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard?dates=YYYYMMDD
"""
from __future__ import annotations

import argparse
import json
import sys

import anthropic

from .classify import audit, load_api_key
from .espn import fetch_summary
from .join import build_match
from .taxonomy import unknown_espn_types


def sweep(game_ids: list[str]) -> dict:
    client = anthropic.Anthropic(api_key=load_api_key())
    results, totals = [], {"checked": 0, "agreed": 0, "requests": 0}

    for gid in game_ids:
        try:
            payload = fetch_summary(gid)
            match = build_match(payload, gid)
            report = audit(client, match)
        except Exception as exc:  # keep sweeping; a bad match shouldn't end the run
            results.append({"game_id": gid, "error": f"{type(exc).__name__}: {exc}"})
            print(f"  {gid}: ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        row = {
            "game_id": gid,
            "match": f"{match['home']['team']} {match['home']['score']}-"
            f"{match['away']['score']} {match['away']['team']}",
            "league": match["league"],
            "lines": len(match["commentary"]),
            "join": {
                "matched": match["join"]["matched"],
                "of": match["join"]["key_events_with_text"],
                "unmatched": len(match["join"]["unmatched"]),
                "by_strategy": match["join"]["by_strategy"],
            },
            "audit": {
                "checked": report.get("checked", 0),
                "agreed": report.get("agreed", 0),
                "accuracy": report.get("accuracy"),
                "disagreements": report.get("disagreements", []),
            },
            "unmapped_espn_types": unknown_espn_types(payload),
        }
        results.append(row)
        totals["checked"] += row["audit"]["checked"]
        totals["agreed"] += row["audit"]["agreed"]
        totals["requests"] += report.get("usage", {}).get("requests", 0)

        acc = row["audit"]["accuracy"]
        print(
            f"  {gid}: {row['match'][:38]:<38} "
            f"join {row['join']['matched']}/{row['join']['of']}  "
            f"audit {row['audit']['agreed']}/{row['audit']['checked']}"
            + (f" ({acc:.0%})" if acc is not None else " (no labels)")
            + (
                f"  UNMAPPED: {row['unmapped_espn_types']}"
                if row["unmapped_espn_types"]
                else ""
            ),
            file=sys.stderr,
        )

    totals["accuracy"] = (
        round(totals["agreed"] / totals["checked"], 4) if totals["checked"] else None
    )
    return {"matches": results, "totals": totals}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="commentary.sweep", description=__doc__)
    parser.add_argument("game_ids", nargs="+", help="ESPN gameIds")
    parser.add_argument("-o", "--out", help="write the full report here")
    args = parser.parse_args(argv)

    out = sweep(args.game_ids)
    t = out["totals"]
    print(
        f"\nTOTAL: {t['agreed']}/{t['checked']} "
        + (f"({t['accuracy']:.1%})" if t["accuracy"] is not None else "(no labels)")
        + f" across {len(args.game_ids)} matches, {t['requests']} requests",
        file=sys.stderr,
    )
    unmapped = sorted({u for m in out["matches"] for u in m.get("unmapped_espn_types", [])})
    if unmapped:
        print(f"UNMAPPED ESPN TYPES (ground truth lost): {unmapped}", file=sys.stderr)

    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
