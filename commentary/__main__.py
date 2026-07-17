"""CLI: python -m commentary <gameId> [--refresh] [-o out.json]

    python -m commentary 760514
    python -m commentary 760514 -o data/commentary-760514.json
"""
from __future__ import annotations

import argparse
import json
import sys

from .espn import fetch_summary
from .join import build_match


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="commentary", description=__doc__)
    parser.add_argument("game_id", help="ESPN gameId, e.g. 760514")
    parser.add_argument(
        "--refresh", action="store_true", help="bypass the raw cache and refetch"
    )
    parser.add_argument(
        "-o", "--out", help="write JSON here (default: stdout)"
    )
    args = parser.parse_args(argv)

    payload = fetch_summary(args.game_id, refresh=args.refresh)
    match = build_match(payload, args.game_id)
    text = json.dumps(match, indent=2, ensure_ascii=False)

    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        j = match["join"]
        print(
            f"{match['home']['team']} {match['home']['score']}–"
            f"{match['away']['score']} {match['away']['team']}  "
            f"({match['league']}, {match['date']})",
            file=sys.stderr,
        )
        print(
            f"wrote {args.out}: {j['commentary_lines']} lines, "
            f"{j['matched']}/{j['key_events_with_text']} key events joined "
            f"({j['by_strategy']}), {len(j['unmatched'])} unmatched",
            file=sys.stderr,
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
