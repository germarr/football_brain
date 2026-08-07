"""FastAPI application for the Previews board (ADR 0040/0041).

The split every surface here uses, for the same reasons (ADR 0021):

  **Reads run in-process and read-only.** The cards come from the Published Store and the
  Editorial Store; the unmapped-Team list comes from three unauthenticated Kalshi GETs on
  its own endpoint, so the page paints before the network call returns.

  **Writes run as a subprocess of the exact CLI argv**, streamed over SSE:

      python -m football_blog.preview --full
      python -m football_blog.preview --quotes
      python -m football_blog.kalshi --propose

  Byte for byte what the terminal would run. The job machinery is the Desk's and the
  Competitions board's, unchanged.

Two things this page will not do:

  - **It never commits the Kalshi team registry.** `--propose` writes the file and stops;
    the review is the `git diff`. A venue id is arbitrary and cannot be wrong, but mapping
    the wrong club silently attributes a market to the wrong Team and produces a **Match
    Preview** that is confidently, invisibly incorrect — so a human sits between the
    proposal and the commit (ADR 0041).
  - **It never edits a Match Preview.** A Preview is derived; the way to change one is to
    rebuild it. Anything that wants hand-authoring is a **Match Post** and belongs on the
    Desk.

Bound to 127.0.0.1: the runs it spawns write to the Editorial Store.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from football import config as football_config
from football_blog.kalshi import REGISTRY_FILE
from football_blog.pocketbase import PocketBaseClient

from .board import (list_cards, market_tracks, summary, unmapped_polymarket_teams,
                    unmapped_teams)

_HERE = Path(__file__).resolve().parent
LOG_DIR = _HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

app = FastAPI(title="La Cancha — Previews")
templates = Jinja2Templates(directory=str(_HERE / "templates"))
# The nav lives one level up; appended to this app's own loader at import so the include
# is unconditional and a rename fails loudly (ADR 0036).
templates.env.loader = ChoiceLoader([
    templates.env.loader,
    FileSystemLoader(str(_HERE.parent / "templates")),
])


def _base_url(request: Request) -> str:
    """Mount prefix: `""` standalone, `"/previews"` under `surfaces` (ADR 0035)."""
    return request.scope.get("root_path", "")


templates.env.globals["base_url"] = _base_url


# --------------------------------------------------------------------------- #
# Jobs — the Desk's machinery, same shape                                      #
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    id: str
    title: str
    argv: list[str]
    status: str = "running"          # running | done | failed | stopped
    returncode: Optional[int] = None
    lines: list[str] = field(default_factory=list)
    started_at: str = ""
    proc: Optional[subprocess.Popen] = None
    _logfile: object = None


JOBS: dict[str, Job] = {}


def _reader(job: Job) -> None:
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


def _spawn(title: str, argv: list[str]) -> Job:
    now = dt.datetime.now()
    job_id = uuid.uuid4().hex[:12]
    logfile = open(LOG_DIR / f"{now:%Y%m%d-%H%M%S}-{job_id}.log", "w")
    logfile.write(f"$ {' '.join(argv)}\n\n")
    logfile.flush()

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        argv, cwd=str(football_config.ROOT), env=env, text=True, bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    job = Job(id=job_id, title=title, argv=argv,
              started_at=now.isoformat(timespec="seconds"), proc=proc, _logfile=logfile)
    job.lines.append(f"$ {' '.join(argv)}")
    JOBS[job_id] = job
    threading.Thread(target=_reader, args=(job,), daemon=True).start()
    return job


#: The three runs this board can fire, and nothing else. Keyed so a request names an
#: action rather than supplying argv — the UI is a second front door, not a shell.
ACTIONS = {
    "full": ("Rebuild Match Previews",
             [sys.executable, "-m", "football_blog.preview", "--full"]),
    "quotes": ("Re-read Winner Markets",
               [sys.executable, "-m", "football_blog.preview", "--quotes"]),
    "propose": ("Propose Kalshi registry entries",
                [sys.executable, "-m", "football_blog.kalshi", "--propose"]),
    "propose_polymarket": ("Propose Polymarket registry entries",
                           [sys.executable, "-m", "football_blog.polymarket", "--propose"]),
}


# --------------------------------------------------------------------------- #
# Routes                                                                       #
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    pb = PocketBaseClient()
    try:
        cards = list_cards(pb)
    finally:
        pb.close()
    return templates.TemplateResponse(request, "index.html", {
        "cards": cards,
        "summary": summary(cards),
        "registry_path": str(REGISTRY_FILE),
    })


@app.get("/api/cards")
def api_cards():
    pb = PocketBaseClient()
    try:
        cards = list_cards(pb)
    finally:
        pb.close()
    return JSONResponse({"summary": summary(cards),
                         "cards": [{**c.__dict__, "kickoff": c.kickoff.isoformat(),
                                    "missing": c.missing, "complete": c.complete}
                                   for c in cards]})


@app.get("/api/unmapped")
def api_unmapped():
    """Kalshi Teams needing a registry entry. Separate endpoint because it hits Kalshi."""
    try:
        return JSONResponse(unmapped_teams())
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e), "teams": [], "markets_refused": 0},
                            status_code=200)


@app.get("/api/unmapped_polymarket")
def api_unmapped_polymarket():
    """Polymarket Teams needing a registry entry. Separate endpoint because it hits Gamma."""
    try:
        return JSONResponse(unmapped_polymarket_teams())
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e), "teams": [], "markets_refused": 0},
                            status_code=200)


@app.get("/api/track/{fixture_id}")
def api_track(fixture_id: int):
    """Both Exchanges' **Market Tracks** for one Fixture (ADR 0043).

    Per-Fixture on purpose: six GETs each, so forty cards would be 240 requests. Nothing
    is stored — the Exchanges keep the history and we re-ask, which is also why this
    still answers for a settled Match Preview.
    """
    pb = PocketBaseClient()
    try:
        return JSONResponse(market_tracks(pb, fixture_id))
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, status_code=200)
    finally:
        pb.close()


@app.get("/api/health")
def health():
    return {"ok": True}


@app.post("/run")
async def run(request: Request):
    body = await request.json()
    action = body.get("action")
    if action not in ACTIONS:
        return JSONResponse({"error": f"unknown action {action!r}"}, status_code=400)
    title, argv = ACTIONS[action]
    job = _spawn(title, argv)
    return {"job_id": job.id, "argv": argv}


@app.get("/jobs")
def jobs():
    return [{"id": j.id, "title": j.title, "status": j.status,
             "started_at": j.started_at, "returncode": j.returncode}
            for j in sorted(JOBS.values(), key=lambda j: j.started_at, reverse=True)]


@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    if job.proc and job.proc.poll() is None:
        job.proc.terminate()
        job.status = "stopped"
    return {"ok": True, "status": job.status}


@app.get("/jobs/{job_id}/stream")
def stream(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)

    def gen():
        sent = 0
        while True:
            while sent < len(job.lines):
                yield f"data: {json.dumps({'line': job.lines[sent]})}\n\n"
                sent += 1
            if job.status != "running" and sent >= len(job.lines):
                yield f"data: {json.dumps({'done': True, 'status': job.status, 'returncode': job.returncode})}\n\n"
                return
            import time
            time.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
