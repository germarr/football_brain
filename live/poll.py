"""Live Poll — fill the Live Mirror (live/live.db) for in-play fixtures.

Repeatedly fetches, for each watched fixture, its `fixtures/events` timeline and
`fixtures` header (live score, status, elapsed) every INTERVAL seconds, and
**overwrites** that fixture's rows in the Live Mirror. Polling a fixture stops
once it reaches a Final/terminal status; the loop exits when all watched
fixtures are done. See ADR 0020 and CONTEXT.md (Live Poll, Live Mirror).

The Mirror reuses `football.models` and `parse.py` helpers, so its `event` /
`fixture` columns match `world-cup.db` exactly — point match_story.py at
live.db and the headline + timeline light up for an in-progress match. Data is
**provisional**: overwritten each poll, revisable until Final (a yellow can
become a red, a goal can be VAR-cancelled). The authoritative record is still
the nightly Refresh into world-cup.db.

Run:  uv run python -m live.poll 1582681 [more_ids...] [--interval 60]
"""
from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

import requests
from sqlmodel import Field, Session, SQLModel, create_engine, delete, select

from football import config
from football.models import Event, Fixture, Player
from football.parse import _is_shootout_kick, _parse_event, _parse_fixture

DB_PATH = Path(__file__).resolve().parent / "live.db"

# Final states (the authoritative record; ADR 0012) plus other terminal statuses
# that also end a watch (a match that is abandoned/postponed will never go Final).
_FINAL = {"FT", "AET", "PEN"}
_TERMINAL = _FINAL | {"PST", "CANC", "ABD", "AWD", "WO"}


class LivePoll(SQLModel, table=True):
    """Freshness marker for a mirrored fixture: when it was last polled, and the
    live status at that moment. `status` duplicates Fixture.status for a cheap
    'what's live right now' read; `polled_at` says how stale the snapshot is."""

    fixture_id: int = Field(primary_key=True)
    polled_at: dt.datetime
    status: str | None = None


def _fetch(endpoint: str, params: dict) -> dict:
    """One live GET, bypassing the cache-first client entirely (no cache I/O)."""
    url = f"{config.BASE_URL}/{endpoint}"
    headers = {config.KEY_HEADER: config.load_api_key()}
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _poll_once(session: Session, fixture_id: int) -> str | None:
    """Fetch + overwrite one fixture's rows in the Mirror. Returns its status."""
    fx_resp = _fetch("fixtures", {"id": fixture_id}).get("response", [])
    if not fx_resp:
        return None
    fx = fx_resp[0]

    # Fixture header (score/status/elapsed) — parsed exactly as world-cup.db.
    fixture = _parse_fixture(fx, {})  # empty venue map -> venue_id null
    session.merge(fixture)

    # Events timeline — full array re-fetched each poll, so overwrite wholesale.
    raw = _fetch("fixtures/events", {"fixture": fixture_id}).get("response", [])
    kept = [e for e in raw if not _is_shootout_kick(e)]
    session.exec(delete(Event).where(Event.fixture_id == fixture_id))
    for i, ev in enumerate(kept):
        session.add(_parse_event(fixture_id, i, ev))

    # Minimal player(id, name) from the same payload so the timeline reads with
    # names — zero extra API calls (Q2 scope: events + header only).
    names: dict[int, str] = {}
    for ev in kept:
        for who in (ev.get("player"), ev.get("assist")):
            if who and who.get("id") and who.get("name"):
                names[who["id"]] = who["name"]
    for pid, name in names.items():
        session.merge(Player(id=pid, name=name))

    status = fx["fixture"]["status"]["short"]
    session.merge(
        LivePoll(
            fixture_id=fixture_id,
            polled_at=dt.datetime.now(dt.timezone.utc),
            status=status,
        )
    )
    session.commit()
    return status


def poll(fixture_ids: list[int], interval: int = 60) -> None:
    engine = create_engine(f"sqlite:///{DB_PATH}")
    SQLModel.metadata.create_all(engine)

    watching = set(fixture_ids)
    print(f"Live Poll → {DB_PATH.name} · watching {sorted(watching)} · every {interval}s")
    while watching:
        with Session(engine) as session:
            for fid in sorted(watching):
                status = _poll_once(session, fid)
                n_ev = session.exec(
                    select(Event).where(Event.fixture_id == fid)
                ).all()
                stamp = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
                print(f"  [{stamp}] {fid}: status={status} events={len(n_ev)}")
                if status is None or status in _TERMINAL:
                    tag = "final" if status in _FINAL else (status or "gone")
                    print(f"    → {fid} done ({tag}); dropping from watch")
                    watching.discard(fid)
        if watching:
            time.sleep(interval)
    print("All watched fixtures finished. Live Poll exiting.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Poll in-play fixtures into live/live.db")
    ap.add_argument("fixture_ids", nargs="+", type=int, help="fixture ids to watch")
    ap.add_argument("--interval", type=int, default=60, help="seconds between polls")
    ap.add_argument("--once", action="store_true", help="one cycle then exit (testing)")
    args = ap.parse_args()
    if args.once:
        engine = create_engine(f"sqlite:///{DB_PATH}")
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            for fid in args.fixture_ids:
                st = _poll_once(session, fid)
                print(f"{fid}: status={st}")
    else:
        poll(args.fixture_ids, args.interval)


if __name__ == "__main__":
    main()
