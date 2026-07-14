"""FastAPI application for the operator dashboard (ADR 0021).

Three responsibilities:
  1. Render the trigger sections (from `football.commands`), the tracked
     leagues/cups panel, and this week's fixtures in NYC time.
  2. Run a trigger as a background subprocess of the exact `python -m ...` command,
     stream its log over SSE, and let the operator stop it.
  3. Guard: refuse to start a second football.db builder while one is running
     (reusing parse.py's exclusive flock on data/football.build.lock).

Read-only DB access degrades gracefully: if football.db is mid-rebuild or missing,
the panels render empty with a note rather than erroring.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import sqlite3
import subprocess
import threading
import time
import uuid
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from .. import commands, config

NY = zoneinfo.ZoneInfo("America/New_York")
UTC = dt.timezone.utc
WEEK_DAYS = 7

FINAL_STATUS = {"FT", "AET", "PEN"}
LIVE_STATUS = {"1H", "2H", "HT", "ET", "BT", "P", "LIVE", "INT", "SUSP"}

_HERE = Path(__file__).resolve().parent
LOG_DIR = _HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

# The Live Mirror (ADR 0020), read for the per-fixture Match Tracker (ADR 0022).
LIVE_DB_PATH = config.ROOT / "live" / "live.db"

app = FastAPI(title="Football 2.0 — Operator Dashboard")
templates = Jinja2Templates(directory=str(_HERE / "templates"))


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    id: str
    key: str
    title: str
    argv: list[str]
    builds_db: bool
    status: str = "running"          # running | done | failed | stopped
    returncode: int | None = None
    lines: list[str] = field(default_factory=list)
    started_at: str = ""
    proc: subprocess.Popen | None = None
    _logfile: object = None


JOBS: dict[str, Job] = {}


def _reader(job: Job) -> None:
    """Drain the child's merged stdout/stderr into memory + a log file."""
    assert job.proc is not None and job.proc.stdout is not None
    for line in job.proc.stdout:
        line = line.rstrip("\n")
        job.lines.append(line)
        if job._logfile:
            job._logfile.write(line + "\n")
            job._logfile.flush()
    job.proc.wait()
    job.returncode = job.proc.returncode
    if job.status == "running":
        job.status = "done" if job.returncode == 0 else "failed"
    if job._logfile:
        job._logfile.close()


def _spawn(cmd: commands.Command, argv: list[str]) -> Job:
    now = dt.datetime.now(NY)
    job_id = uuid.uuid4().hex[:12]
    log_path = LOG_DIR / f"{now:%Y%m%d-%H%M%S}-{cmd.key}-{job_id}.log"
    logfile = open(log_path, "w")
    logfile.write(f"$ {' '.join(argv)}\n\n")
    logfile.flush()

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        argv, cwd=str(config.ROOT), env=env, text=True, bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    job = Job(
        id=job_id, key=cmd.key, title=cmd.title, argv=argv,
        builds_db=cmd.builds_db, started_at=now.isoformat(timespec="seconds"),
        proc=proc, _logfile=logfile,
    )
    job.lines.append(f"$ {' '.join(argv)}")
    JOBS[job_id] = job
    threading.Thread(target=_reader, args=(job,), daemon=True).start()
    return job


def _db_build_busy() -> str | None:
    """Reason a football.db build cannot start now, or None if it can.

    Two checks: a UI-tracked builder still running, and the exclusive flock that
    parse.build() holds during a rebuild (catches builds started from the terminal).
    """
    for j in JOBS.values():
        if j.status == "running" and j.builds_db:
            return f"'{j.title}' is already running and rebuilds football.db."
    lock_path = config.DB_PATH.with_suffix(".build.lock")
    try:
        f = open(lock_path, "a")
    except OSError:
        return None
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return None
    except OSError:
        return "A football.db build is already in progress (build lock held)."
    finally:
        f.close()


# --------------------------------------------------------------------------- #
# Read-only data for the panels
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection | None:
    if not config.DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _competition_meta() -> dict[int, sqlite3.Row]:
    con = _connect()
    if con is None:
        return {}
    try:
        rows = con.execute(
            "select id, name, type, country, continent, flag, logo from competition"
        ).fetchall()
        return {r["id"]: r for r in rows}
    except sqlite3.Error:
        return {}
    finally:
        con.close()


def tracked_competitions() -> list[dict]:
    """Every Competition from competitions.json, enriched with DB metadata (flag…)."""
    config.reload_competitions()
    meta = _competition_meta()
    out = []
    for c in config.COMPETITIONS:
        m = meta.get(c["league_id"])
        seasons = c.get("seasons") or []
        out.append({
            "id": c["league_id"],
            "name": c["name"],
            "type": c["type"],
            "seasons": seasons,
            "season_from": min(seasons) if seasons else None,
            "season_to": max(seasons) if seasons else None,
            "country": (m["country"] if m else None),
            "continent": (m["continent"] if m and m["continent"] else "Not yet built"),
            "flag": (m["flag"] if m else None),
            "logo": (m["logo"] if m else None),
        })
    out.sort(key=lambda x: (x["continent"], x["type"], x["name"]))
    return out


def week_fixtures(days: int = WEEK_DAYS) -> tuple[list[dict], dict]:
    """This week's fixtures grouped by NYC calendar day, plus window metadata."""
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
        "db_missing": not config.DB_PATH.exists(),
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

    today = now_ny.date()
    by_day: dict[dt.date, dict] = {}
    for r in rows:
        try:
            naive = dt.datetime.fromisoformat(r["date"])
        except (ValueError, TypeError):
            continue
        ko = naive.replace(tzinfo=UTC).astimezone(NY)
        status = (r["status"] or "").upper()
        if status in FINAL_STATUS:
            state, score = "final", _score(r)
        elif status in LIVE_STATUS:
            state, score = "live", _score(r)
        else:
            state, score = "scheduled", None
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
        })

    groups = []
    for day in sorted(by_day):
        delta = (day - today).days
        if delta == 0:
            label = "Today"
        elif delta == 1:
            label = "Tomorrow"
        else:
            label = day.strftime("%A")
        groups.append({
            "label": label,
            "date": day.strftime("%b %-d"),
            "fixtures": by_day[day]["fixtures"],
        })
    meta["count"] = sum(len(g["fixtures"]) for g in groups)
    return groups, meta


def _score(r: sqlite3.Row) -> str | None:
    if r["home_goals"] is None or r["away_goals"] is None:
        return None
    return f"{r['home_goals']}–{r['away_goals']}"


# --------------------------------------------------------------------------- #
# Per-fixture Match Tracker (ADR 0022)
# --------------------------------------------------------------------------- #
def _live_connect() -> sqlite3.Connection | None:
    """Read-only connection to the Live Mirror, or None if it doesn't exist."""
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


def _event_text(r: sqlite3.Row, team_name: dict, home_id: int, away_id: int) -> str:
    """Human description of one Event — mirrors match_story.py's _describe,
    including the subst gotcha (player_id = OFF, assist_id = ON; CONTEXT.md)."""
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


def _read_fixture(con: sqlite3.Connection, fixture_id: int):
    """(fixture row, [event rows]) for one fixture from the given DB, or None.
    Works against football.db or the schema-identical Live Mirror (ADR 0020)."""
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
    the Live Mirror wins while a livepoll row exists, else football.db."""
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


def _running_poll_job(fixture_id: int) -> str | None:
    """The id of a running live_poll job watching this fixture, if any."""
    fid = str(fixture_id)
    for j in JOBS.values():
        if j.status == "running" and j.key == "live_poll" and fid in j.argv:
            return j.id
    return None


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    comps = tracked_competitions()
    fixtures, week_meta = week_fixtures()
    # Options for parameterised forms.
    comp_options = sorted(
        ({"id": c["id"], "name": c["name"], "type": c["type"]} for c in comps),
        key=lambda x: x["name"],
    )
    fixture_options = [
        {
            "id": fx["id"],
            "label": f"{fx['comp']} · {fx['home']} v {fx['away']} · {g['label']} {fx['time']}",
        }
        for g in fixtures
        for fx in g["fixtures"]
    ]
    # continents for the leagues panel (preserve sorted order)
    continents: list[str] = []
    for c in comps:
        if c["continent"] not in continents:
            continents.append(c["continent"])

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "groups": commands.by_group(),
            "comps": comps,
            "continents": continents,
            "comp_options": comp_options,
            "fixture_options": fixture_options,
            "fixtures": fixtures,
            "week": week_meta,
            "n_leagues": sum(1 for c in comps if c["type"] == "league"),
            "n_cups": sum(1 for c in comps if c["type"] == "cup"),
        },
    )


@app.get("/api/health")
def health():
    return {"ok": True, "jobs": len(JOBS)}


@app.post("/run")
async def run(request: Request):
    body = await request.json()
    key = body.get("key")
    values = body.get("values") or {}
    cmd = commands.BY_KEY.get(key)
    if cmd is None:
        return JSONResponse({"error": f"Unknown command '{key}'."}, status_code=404)

    if cmd.builds_db:
        busy = _db_build_busy()
        if busy:
            return JSONResponse({"error": busy}, status_code=409)

    try:
        argv = commands.build_argv(cmd, values)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    job = _spawn(cmd, argv)
    return {"job_id": job.id, "argv": argv, "title": cmd.title}


@app.get("/jobs")
def jobs():
    return [
        {
            "id": j.id, "key": j.key, "title": j.title, "status": j.status,
            "returncode": j.returncode, "started_at": j.started_at,
            "builds_db": j.builds_db, "lines": len(j.lines),
        }
        for j in sorted(JOBS.values(), key=lambda x: x.started_at, reverse=True)
    ]


@app.get("/jobs/{job_id}/log")
def job_log(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return {"status": job.status, "returncode": job.returncode, "lines": job.lines}


@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)
    if job.proc and job.status == "running":
        job.status = "stopped"
        job.proc.terminate()
    return {"status": job.status}


@app.get("/jobs/{job_id}/stream")
def stream(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        return JSONResponse({"error": "no such job"}, status_code=404)

    def gen():
        idx = 0
        while True:
            while idx < len(job.lines):
                yield f"data: {json.dumps({'line': job.lines[idx]})}\n\n"
                idx += 1
            if job.status != "running":
                yield ("event: end\n"
                       f"data: {json.dumps({'status': job.status, 'code': job.returncode})}\n\n")
                return
            time.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream")


# --------------------------------------------------------------------------- #
# Match Tracker routes (ADR 0022)
# --------------------------------------------------------------------------- #
@app.get("/fixture/{fixture_id}", response_class=HTMLResponse)
def fixture_page(request: Request, fixture_id: int):
    st = _fixture_state(fixture_id)
    if st is None:
        return HTMLResponse(
            "<p style='font-family:system-ui;padding:40px'>"
            f"Unknown fixture <b>{fixture_id}</b> — not in football.db or the Live "
            "Mirror. <a href='/'>← back to the dashboard</a></p>",
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


@app.post("/fixture/{fixture_id}/clear")
def fixture_clear(fixture_id: int):
    """Delete this fixture's rows from the Live Mirror, reverting the page to
    authoritative (ADR 0022)."""
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
