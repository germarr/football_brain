"""THROWAWAY spike (live/): does API-Football return in-play events?

Hits /fixtures and /fixtures/events for one fixture *live*, bypassing the
cache-first client entirely (no cache read, no cache write), and prints the
current status + event timeline. This is a scratch probe to see the live API's
shape before we design live.db — NOT part of the durable pipeline.

Run:  uv run python -m live.spike_events [fixture_id]   (default 1582681)
"""
from __future__ import annotations

import sys

import requests

from football import config


def _fetch(endpoint: str, params: dict) -> dict:
    url = f"{config.BASE_URL}/{endpoint}"
    headers = {config.KEY_HEADER: config.load_api_key()}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def main(fixture_id: int) -> None:
    fx = _fetch("fixtures", {"id": fixture_id}).get("response", [])
    if not fx:
        print(f"No fixture {fixture_id}")
        return
    f = fx[0]
    st = f["fixture"]["status"]
    g = f["goals"]
    print(
        f'{f["teams"]["home"]["name"]} {g["home"]}-{g["away"]} '
        f'{f["teams"]["away"]["name"]}   '
        f'[{st["short"]} · elapsed {st.get("elapsed")}\']'
    )

    ev = _fetch("fixtures/events", {"fixture": fixture_id}).get("response", [])
    print(f"\n{len(ev)} events from fixtures/events:")
    for e in ev:
        t = e["time"]
        clk = f'{t["elapsed"]}' + (f'+{t["extra"]}' if t.get("extra") else "")
        who = (e.get("player") or {}).get("name") or ""
        ast = (e.get("assist") or {}).get("name")
        print(
            f'  {clk:>5}\'  {e["team"]["name"]:<16} '
            f'{e["type"]:<6} {e["detail"]:<18} {who}'
            + (f" (assist {ast})" if ast else "")
        )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1582681)
