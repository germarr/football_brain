"""The Viewer — the reader-facing FastAPI app (ADR 0023).

Reads ONLY its own stores, never `data/football.db`:
  - `web/serve.db`  — the daily-published serving slice (authoritative-for-the-Viewer),
  - `live/live.db`  — the provisional Live Mirror (ADR 0020), the per-match live overlay.

Three surfaces:
  1. the tracked leagues/cups panel and this week's games (from serve.db), and
  2. the per-match Match Tracker (ADR 0022) — headline + event timeline under the
     precedence rule "Live Mirror wins while a livepoll row exists, else serve.db" —
  3. which can launch / stop `live.poll` for that fixture (the ONE subprocess the Viewer
     spawns; it spends API quota, which is why this app stays bound to 127.0.0.1).

If serve.db is missing (never published), the panels render empty with a note.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from football import config

NY = zoneinfo.ZoneInfo("America/New_York")
UTC = dt.timezone.utc
WEEK_DAYS = 7

FINAL_STATUS = {"FT", "AET", "PEN"}
LIVE_STATUS = {"1H", "2H", "HT", "ET", "BT", "P", "LIVE", "INT", "SUSP"}
# A fixture is "settled" (no live refresh will change it) once terminal: Final plus the
# non-Final ends (postponed/cancelled/abandoned/…). The Refresh button targets the
# complement — kicked-off but NOT terminal (ADR 0024).
TERMINAL_STATUS = FINAL_STATUS | {"PST", "CANC", "ABD", "AWD", "WO"}

_HERE = Path(__file__).resolve().parent
LOG_DIR = _HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

SERVE_DB_PATH = config.ROOT / "web" / "serve.db"       # authoritative-for-the-Viewer
LIVE_DB_PATH = config.ROOT / "live" / "live.db"        # provisional Live Mirror (ADR 0020)

app = FastAPI(title="Football 2.0 — Viewer")
templates = Jinja2Templates(directory=str(_HERE / "templates"))


# --------------------------------------------------------------------------- #
# Live-poll jobs — the sole subprocess the Viewer spawns (ADR 0022/0023)
# --------------------------------------------------------------------------- #
@dataclass
class PollJob:
    id: str
    fixture_ids: list[int]
    argv: list[str]
    status: str = "running"          # running | done | failed | stopped
    returncode: int | None = None
    started_at: str = ""
    proc: subprocess.Popen | None = None
    _logfile: object = None


JOBS: dict[str, PollJob] = {}


def _reader(job: PollJob) -> None:
    assert job.proc is not None and job.proc.stdout is not None
    for line in job.proc.stdout:
        if job._logfile:
            job._logfile.write(line)
            job._logfile.flush()
    job.proc.wait()
    job.returncode = job.proc.returncode
    if job.status == "running":
        job.status = "done" if job.returncode == 0 else "failed"
    if job._logfile:
        job._logfile.close()


def _spawn_poll(fixture_ids: list[int], interval: int = 60, once: bool = False) -> PollJob:
    """Launch `python -m live.poll <ids> [--interval N | --once]` as a background child.
    Mirrors the Console's spawn pattern, but scoped to the single live.poll command."""
    argv = [sys.executable, "-m", "live.poll", *[str(i) for i in fixture_ids]]
    if once:
        argv.append("--once")
    else:
        argv += ["--interval", str(interval)]

    now = dt.datetime.now(NY)
    job_id = uuid.uuid4().hex[:12]
    log_path = LOG_DIR / f"{now:%Y%m%d-%H%M%S}-live_poll-{job_id}.log"
    logfile = open(log_path, "w")
    logfile.write(f"$ {' '.join(argv)}\n\n")
    logfile.flush()

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        argv, cwd=str(config.ROOT), env=env, text=True, bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    job = PollJob(
        id=job_id, fixture_ids=list(fixture_ids), argv=argv,
        started_at=now.isoformat(timespec="seconds"), proc=proc, _logfile=logfile,
    )
    JOBS[job_id] = job
    threading.Thread(target=_reader, args=(job,), daemon=True).start()
    return job


def _running_poll_job(fixture_id: int) -> str | None:
    """The id of a running live.poll job watching this fixture, if any."""
    for j in JOBS.values():
        if j.status == "running" and fixture_id in j.fixture_ids:
            return j.id
    return None


# --------------------------------------------------------------------------- #
# Read-only data for the panels (serve.db)
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection | None:
    if not SERVE_DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{SERVE_DB_PATH}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def tracked_competitions() -> list[dict]:
    """Every Competition in the serving store, grouped-ready by continent."""
    con = _connect()
    if con is None:
        return []
    try:
        rows = con.execute(
            "select id, name, type, country, continent, flag, logo from competition"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    out = [{
        "id": r["id"], "name": r["name"], "type": r["type"], "country": r["country"],
        "continent": r["continent"] or "Unknown", "flag": r["flag"], "logo": r["logo"],
    } for r in rows]
    out.sort(key=lambda x: (x["continent"], x["type"], x["name"]))
    return out


def _live_overlay() -> dict[int, dict]:
    """fixture_id -> {status, home_goals, away_goals, polled_at} for every fixture the
    Live Mirror is tracking (has a `livepoll` marker). The table and the active-set both
    overlay this over the serve.db snapshot — the ADR 0022 precedence rule, batched."""
    con = _live_connect()
    if con is None:
        return {}
    try:
        rows = con.execute(
            "select lp.fixture_id, lp.polled_at, f.status, f.home_goals, f.away_goals "
            "from livepoll lp join fixture f on f.id = lp.fixture_id"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return {
        r["fixture_id"]: {
            "status": r["status"], "home_goals": r["home_goals"],
            "away_goals": r["away_goals"], "polled_at": r["polled_at"],
        }
        for r in rows
    }


def _state_and_score(status: str, hg, ag) -> tuple[str, str | None]:
    if status in FINAL_STATUS:
        state = "final"
    elif status in LIVE_STATUS:
        state = "live"
    else:
        state = "scheduled"
    score = None if state == "scheduled" else (f"{hg}–{ag}" if hg is not None and ag is not None else None)
    return state, score


def week_fixtures(days: int = WEEK_DAYS) -> tuple[list[dict], dict]:
    """This week's fixtures grouped by NYC calendar day, plus window metadata.
    Reads the serve.db −3..+10 superset, filters to today..+`days` in NYC time, and
    overlays the provisional Live Mirror where it has fresher rows (ADR 0024)."""
    now_ny = dt.datetime.now(NY)
    start_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
    end_ny = start_ny + dt.timedelta(days=days)
    start_utc = start_ny.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    end_utc = end_ny.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")

    meta = {
        "start": start_ny.strftime("%a %b %-d"),
        "end": (end_ny - dt.timedelta(days=1)).strftime("%a %b %-d"),
        "days": days,
        "count": 0,
        "db_missing": not SERVE_DB_PATH.exists(),
    }

    con = _connect()
    if con is None:
        return [], meta
    try:
        rows = con.execute(
            """
            select f.id, f.date, f.status,
                   f.home_team_name, f.away_team_name, f.home_goals, f.away_goals,
                   coalesce(c.name, f.league_name) as comp, c.type as ctype, c.flag
            from fixture f
            left join competition c on c.id = f.league_id
            where f.date >= ? and f.date < ?
            order by f.date
            """,
            (start_utc, end_utc),
        ).fetchall()
    except sqlite3.Error:
        return [], meta
    finally:
        con.close()

    overlay = _live_overlay()
    today = now_ny.date()
    by_day: dict[dt.date, dict] = {}
    for r in rows:
        try:
            naive = dt.datetime.fromisoformat(r["date"])
        except (ValueError, TypeError):
            continue
        ko = naive.replace(tzinfo=UTC).astimezone(NY)
        ov = overlay.get(r["id"])
        status = ((ov["status"] if ov else r["status"]) or "").upper()
        hg = ov["home_goals"] if ov else r["home_goals"]
        ag = ov["away_goals"] if ov else r["away_goals"]
        state, score = _state_and_score(status, hg, ag)
        day = ko.date()
        bucket = by_day.setdefault(day, {"date": day, "fixtures": []})
        bucket["fixtures"].append({
            "id": r["id"],
            "time": ko.strftime("%-I:%M %p"),
            "comp": r["comp"],
            "ctype": r["ctype"] or "",
            "flag": r["flag"],
            "home": r["home_team_name"],
            "away": r["away_team_name"],
            "state": state,
            "status": status,
            "score": score,
            "provisional": ov is not None,
            "polled_at": ov["polled_at"] if ov else None,
        })

    groups = []
    for day in sorted(by_day):
        delta = (day - today).days
        label = "Today" if delta == 0 else "Tomorrow" if delta == 1 else day.strftime("%A")
        groups.append({
            "label": label,
            "date": day.strftime("%b %-d"),
            "fixtures": by_day[day]["fixtures"],
        })
    meta["count"] = sum(len(g["fixtures"]) for g in groups)
    return groups, meta


def _active_fixture_ids(days: int = WEEK_DAYS) -> list[int]:
    """Week fixtures that have kicked off but aren't settled — the set the Refresh
    button polls (ADR 0024). Effective status overlays the Live Mirror, so a game
    already polled to Final is excluded and never re-polled."""
    now_ny = dt.datetime.now(NY)
    start_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_ny.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    now_utc = now_ny.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")

    con = _connect()
    if con is None:
        return []
    try:
        rows = con.execute(
            "select id, status from fixture where date >= ? and date <= ?",
            (start_utc, now_utc),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()

    overlay = _live_overlay()
    ids = []
    for r in rows:
        ov = overlay.get(r["id"])
        status = ((ov["status"] if ov else r["status"]) or "").upper()
        if status not in TERMINAL_STATUS:
            ids.append(r["id"])
    return ids


def _score(r) -> str | None:
    if r["home_goals"] is None or r["away_goals"] is None:
        return None
    return f"{r['home_goals']}–{r['away_goals']}"


def league_detail(league_id: int) -> dict | None:
    """The precomputed league section (ADR 0025): standings + top-5 scorers/assists +
    team-most-goals for the current season, read straight from serve.db. Returns a shape
    the `_league.html` fragment renders — including the cup / no-data / stats-light cases."""
    con = _connect()
    if con is None:
        return None
    try:
        comp = con.execute(
            "select name, type, flag, country, continent from competition where id=?",
            (league_id,),
        ).fetchone()
        if comp is None:
            return None
        base = {"league_id": league_id, "name": comp["name"], "flag": comp["flag"],
                "country": comp["country"]}
        if (comp["type"] or "").lower() != "league":
            return {**base, "is_cup": True}
        meta = con.execute(
            "select * from league_meta where league_id=?", (league_id,)
        ).fetchone()
        if meta is None:
            return {**base, "no_data": True}   # a league with no completed games yet
        standings = con.execute(
            "select pos, team_name, P, W, D, L, GF, GA, GD, Pts from league_standing "
            "where league_id=? order by pos", (league_id,),
        ).fetchall()
        scorers = con.execute(
            "select rank, player_name, team_name, value from league_scorer "
            "where league_id=? and kind='goals' order by rank", (league_id,),
        ).fetchall()
        assists = con.execute(
            "select rank, player_name, team_name, value from league_scorer "
            "where league_id=? and kind='assists' order by rank", (league_id,),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return {
        **base,
        "season_label": meta["season_label"],
        "team_count": meta["team_count"],
        "played": meta["played"],
        "stats_light": bool(meta["stats_light"]),
        "top_team_name": meta["top_team_name"],
        "top_team_goals": meta["top_team_goals"],
        "standings": [dict(r) for r in standings],
        "scorers": [dict(r) for r in scorers],
        "assists": [dict(r) for r in assists],
    }


# --------------------------------------------------------------------------- #
# Per-fixture Match Tracker (ADR 0022) — precedence: live.db, else serve.db
# --------------------------------------------------------------------------- #
def _live_connect() -> sqlite3.Connection | None:
    if not LIVE_DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{LIVE_DB_PATH}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _fmt_clock(minute, extra) -> str:
    if minute is None:
        return ""
    m = int(minute)
    return f"{m}+{int(extra)}'" if extra else f"{m}'"


def _event_icon(t: str, detail: str | None) -> str:
    if t == "Goal":
        return "❌" if detail == "Missed Penalty" else "⚽"
    if t == "Card":
        return "🟥" if detail == "Red Card" else "🟨"
    if t == "subst":
        return "🔁"
    if t == "Var":
        return "📺"
    return "•"


def _event_text(r, team_name: dict, home_id: int, away_id: int) -> str:
    """Human description of one Event — mirrors match_story.py's _describe, including
    the subst gotcha (player_id = OFF, assist_id = ON; CONTEXT.md)."""
    t, detail = r["type"], r["detail"]
    team = team_name.get(r["team_id"], "?")
    player = r["player"] or "?"
    assist, reason = r["assist"], r["comments"]
    if t == "Goal":
        if detail == "Own Goal":
            opp = team_name.get(away_id if r["team_id"] == home_id else home_id, "?")
            return f"Own goal — {player} ({team}), counts for {opp}"
        if detail == "Missed Penalty":
            return f"Missed penalty — {player} ({team})"
        kind = " (pen)" if detail == "Penalty" else ""
        asst = f", assist {assist}" if assist else ""
        return f"Goal{kind} — {player} ({team}){asst}"
    if t == "Card":
        label = "Red" if detail == "Red Card" else "Yellow"
        because = f", {reason}" if reason else ""
        return f"{label} — {player} ({team}){because}"
    if t == "subst":
        return f"Sub — {player} off, {assist or '?'} on ({team})"
    if t == "Var":
        return f"VAR — {detail} ({team})"
    return f"{t} — {detail} ({team})"


def _read_fixture(con, fixture_id: int):
    """(fixture row, [event rows]) for one fixture from the given DB, or None.
    Works against serve.db or the schema-identical Live Mirror (ADR 0020)."""
    try:
        fx = con.execute("select * from fixture where id = ?", (fixture_id,)).fetchone()
        if fx is None:
            return None
        events = con.execute(
            """
            select e.event_index, e.minute, e.extra, e.team_id, e.type, e.detail,
                   e.comments, p.name as player, a.name as assist
            from event e
            left join player p on p.id = e.player_id
            left join player a on a.id = e.assist_id
            where e.fixture_id = ?
            order by e.minute, coalesce(e.extra, 0), e.event_index
            """,
            (fixture_id,),
        ).fetchall()
        return fx, events
    except sqlite3.Error:
        return None


def _fixture_state(fixture_id: int) -> dict | None:
    """Headline + timeline for one fixture under the ADR 0022 precedence rule:
    the Live Mirror wins while a livepoll row exists, else serve.db."""
    source: str | None = None
    polled_at = None
    data = None

    live = _live_connect()
    if live is not None:
        try:
            lp = live.execute(
                "select polled_at from livepoll where fixture_id = ?", (fixture_id,)
            ).fetchone()
            if lp is not None:
                data = _read_fixture(live, fixture_id)
                if data is not None:
                    source, polled_at = "live", lp["polled_at"]
        except sqlite3.Error:
            pass
        finally:
            live.close()

    if data is None:
        con = _connect()
        if con is not None:
            try:
                data = _read_fixture(con, fixture_id)
                if data is not None:
                    source = "authoritative"
            finally:
                con.close()

    if data is None:
        return None

    fx, events = data
    home_id, away_id = fx["home_team_id"], fx["away_team_id"]
    team_name = {home_id: fx["home_team_name"], away_id: fx["away_team_name"]}
    status = (fx["status"] or "").upper()
    if status in FINAL_STATUS:
        state = "final"
    elif status in LIVE_STATUS:
        state = "live"
    else:
        state = "scheduled"

    return {
        "id": fixture_id,
        "source": source,
        "provisional": source == "live",
        "polled_at": polled_at,
        "home": fx["home_team_name"],
        "away": fx["away_team_name"],
        "home_goals": fx["home_goals"],
        "away_goals": fx["away_goals"],
        "score": _score(fx),
        "status": status,
        "state": state,
        "league": fx["league_name"],
        "round": fx["round"],
        "date": fx["date"],
        "events": [
            {
                "time": _fmt_clock(e["minute"], e["extra"]),
                "icon": _event_icon(e["type"], e["detail"]),
                "text": _event_text(e, team_name, home_id, away_id),
            }
            for e in events
        ],
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    comps = tracked_competitions()
    fixtures, week_meta = week_fixtures()
    continents: list[str] = []
    for c in comps:
        if c["continent"] not in continents:
            continents.append(c["continent"])
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "comps": comps,
            "continents": continents,
            "fixtures": fixtures,
            "week": week_meta,
            "active_count": len(_active_fixture_ids()),
            "n_leagues": sum(1 for c in comps if c["type"] == "league"),
            "n_cups": sum(1 for c in comps if c["type"] == "cup"),
        },
    )


@app.get("/week", response_class=HTMLResponse)
def week(request: Request):
    """The `this week` block as an HTML fragment (button + day tables), overlaid with the
    Live Mirror — re-rendered in place after a Refresh (ADR 0024). Same partial the index
    includes, so there is one source of truth for the markup."""
    fixtures, week_meta = week_fixtures()
    return templates.TemplateResponse(
        request, "_week.html",
        {"fixtures": fixtures, "week": week_meta, "active_count": len(_active_fixture_ids())},
    )


@app.get("/league/{league_id}", response_class=HTMLResponse)
def league_section(request: Request, league_id: int):
    """The inline league section as an HTML fragment (ADR 0025) — standings + leaders,
    injected in place when a league chip is clicked."""
    d = league_detail(league_id)
    if d is None:
        return HTMLResponse(
            "<div class='ld'><p class='ld-empty'>League not found in the serving store.</p></div>",
            status_code=404,
        )
    return templates.TemplateResponse(request, "_league.html", {"d": d})


@app.post("/refresh-live")
def refresh_live():
    """Poll the active slate once into the Live Mirror (ADR 0024): the week's fixtures
    that kicked off but aren't settled. Reuses the Viewer's live.poll subprocess; the
    table then overlays the fresh live.db rows. Returns the job id + how many were polled."""
    ids = _active_fixture_ids()
    if not ids:
        return {"job_id": None, "count": 0, "ids": []}
    job = _spawn_poll(ids, once=True)
    return {"job_id": job.id, "count": len(ids), "ids": ids}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return {"status": job.status, "returncode": job.returncode}


@app.get("/api/health")
def health():
    return {"ok": True, "serve_db": SERVE_DB_PATH.exists(), "jobs": len(JOBS)}


@app.get("/fixture/{fixture_id}", response_class=HTMLResponse)
def fixture_page(request: Request, fixture_id: int):
    st = _fixture_state(fixture_id)
    if st is None:
        return HTMLResponse(
            "<p style='font-family:system-ui;padding:40px'>"
            f"Unknown fixture <b>{fixture_id}</b> — not in this week's serving store or "
            "the Live Mirror. <a href='/'>← back</a></p>",
            status_code=404,
        )
    return templates.TemplateResponse(
        request, "fixture.html",
        {"st": st, "running_job": _running_poll_job(fixture_id)},
    )


@app.get("/fixture/{fixture_id}/state")
def fixture_state(fixture_id: int):
    st = _fixture_state(fixture_id)
    if st is None:
        return JSONResponse({"error": "unknown fixture"}, status_code=404)
    return st


@app.post("/run")
async def run(request: Request):
    """Launch a live.poll for a fixture — the Viewer's only trigger (ADR 0023).
    Kept POST /run with key='live_poll' so the Match Tracker page's fetch is unchanged."""
    body = await request.json()
    if body.get("key") != "live_poll":
        return JSONResponse({"error": "The Viewer only runs live_poll."}, status_code=400)
    values = body.get("values") or {}
    raw_ids = values.get("fixture_ids") or []
    if not isinstance(raw_ids, list):
        raw_ids = [raw_ids]
    try:
        fixture_ids = [int(x) for x in raw_ids if str(x).strip()]
    except (TypeError, ValueError):
        return JSONResponse({"error": "Bad fixture ids."}, status_code=400)
    if not fixture_ids:
        return JSONResponse({"error": "No fixtures to watch."}, status_code=400)
    once = bool(values.get("once"))
    try:
        interval = int(values.get("interval") or 60)
    except (TypeError, ValueError):
        interval = 60
    job = _spawn_poll(fixture_ids, interval=interval, once=once)
    return {"job_id": job.id, "argv": job.argv, "title": "Live poll"}


@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    if job.proc and job.status == "running":
        job.status = "stopped"
        job.proc.terminate()
    return {"status": job.status}


@app.post("/fixture/{fixture_id}/clear")
def fixture_clear(fixture_id: int):
    """Delete this fixture's rows from the Live Mirror, reverting the page to
    authoritative (serve.db) (ADR 0022)."""
    if not LIVE_DB_PATH.exists():
        return {"cleared": False}
    try:
        con = sqlite3.connect(LIVE_DB_PATH, timeout=5)
        con.execute("delete from event where fixture_id = ?", (fixture_id,))
        con.execute("delete from fixture where id = ?", (fixture_id,))
        con.execute("delete from livepoll where fixture_id = ?", (fixture_id,))
        con.commit()
        con.close()
    except sqlite3.Error as e:
        return JSONResponse({"cleared": False, "error": str(e)}, status_code=500)
    return {"cleared": True}


@app.get("/fixture/{fixture_id}/live")
def fixture_live(fixture_id: int):
    """SSE display-refresh: push headline+timeline on connect and whenever
    livepoll.polled_at changes for this fixture (ADR 0022). Distinct from the
    API-side Live Poll — this is the browser re-reading our own store."""
    def gen():
        last = "\x00"  # sentinel distinct from any polled_at string or None
        while True:
            st = _fixture_state(fixture_id)
            marker = (st or {}).get("polled_at")
            if marker != last:
                last = marker
                yield f"event: state\ndata: {json.dumps(st)}\n\n"
            time.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream")
