"""FastAPI application for the Operator Console (ADR 0021, ADR 0023).

The Console's *only* job is to run the scripts that build/refresh data/football.db
(Collect / Build / Refresh / Publish, from `football.commands`). Since ADR 0023 split
the reader surface out into the Viewer (`web.app`), this app no longer reads
football.db or renders any leagues/week/match panels — the Viewer owns those, over its
own serve.db. The Live group is gone too: launching `live.poll` now lives on the
Viewer's Match Tracker page.

Two responsibilities remain:
  1. Render the trigger sections (from `football.commands`); form options come from
     competitions.json (config), not football.db.
  2. Run a trigger as a background subprocess of the exact `python -m ...` command,
     stream its log over SSE, let the operator stop it, and refuse to start a second
     football.db builder while one holds data/football.build.lock.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
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

from football import commands, config

NY = zoneinfo.ZoneInfo("America/New_York")

_HERE = Path(__file__).resolve().parent
LOG_DIR = _HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Football 2.0 — Operator Console")
templates = Jinja2Templates(directory=str(_HERE / "templates"))


def _base_url(request: Request) -> str:
    """This app's mount prefix: `""` standalone, `"/console"` under `surfaces` (ADR 0035).

    Same device as the Desk's. Every endpoint the page calls is built from it, so one
    set of templates serves both modes and a hardcoded prefix cannot 404 in whichever
    mode nobody tested.
    """
    return request.scope.get("root_path", "")


templates.env.globals["base_url"] = _base_url


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
# Form options (from config — never football.db)
# --------------------------------------------------------------------------- #
def competition_options() -> list[dict]:
    """The competition picker for parameterised forms (scope/orchestrate/…), read
    straight from competitions.json (ADR 0019) — the Console reads no football.db."""
    config.reload_competitions()
    opts = [
        {"id": c["league_id"], "name": c["name"], "type": c["type"]}
        for c in config.COMPETITIONS
    ]
    opts.sort(key=lambda x: x["name"])
    return opts


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "groups": commands.by_group(),
            "comp_options": competition_options(),
            "fixture_options": [],   # no fixture-typed params remain on the Console
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
