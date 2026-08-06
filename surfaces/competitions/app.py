"""FastAPI application for the Competitions board (ADR 0037).

The same split the Desk uses, for the same reasons:

  **Reads run in-process and read-only.** The cards, their stages and the derived form
  defaults. No subprocess, no writes, no quota.

  **The one write runs as a subprocess of the exact CLI argv**, streamed over SSE:

      python -m football_blog.onboard --league-id N --slug … --language … --timezone … --yes

  Identical to what the terminal would run, which is ADR 0021's rule that the UI is a
  *second* front door and never a reimplementation. The ordering constraint that makes
  this work — publish to the store before the checks that read it — lives in that
  command, not in this app's JavaScript.

Two things this page will not do:

  - **It never sets `published = true`.** The button advances a Competition to
    **Draftable** and stops. That gate is the Publication's and flipping it is a human
    act, the same separation ADR 0034 keeps between drafting a Match Post and publishing
    one (CONTEXT.md).
  - **It never edits an existing Publication.** The run is create-if-absent; a second
    click reports the record unchanged. `llm_prompt_overrides` is authored from the Desk
    and lives in the one store with no rebuild path.

Bound to 127.0.0.1: the run spends API-Football quota and rewrites the Published Store.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, FileSystemLoader

from football import config as football_config
from football_blog.onboard import COMMON_TZS, VALID_LANGUAGES, derive_defaults

from .board import STAGE_LABEL, STAGES, list_cards, summary

_HERE = Path(__file__).resolve().parent
LOG_DIR = _HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

app = FastAPI(title="La Cancha — Competitions")
templates = Jinja2Templates(directory=str(_HERE / "templates"))
templates.env.loader = ChoiceLoader([
    templates.env.loader,
    FileSystemLoader(str(_HERE.parent / "templates")),
])


def _base_url(request: Request) -> str:
    """Mount prefix: `""` standalone, `"/competitions"` under `surfaces` (ADR 0035)."""
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
    league_id: Optional[int] = None
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


def _spawn(title: str, argv: list[str], *, league_id: Optional[int] = None) -> Job:
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
    job = Job(id=job_id, title=title, argv=argv, league_id=league_id,
              started_at=now.isoformat(timespec="seconds"), proc=proc, _logfile=logfile)
    job.lines.append(f"$ {' '.join(argv)}")
    JOBS[job_id] = job
    threading.Thread(target=_reader, args=(job,), daemon=True).start()
    return job


def build_onboard_argv(
    league_id: int,
    *,
    slug: str,
    language: str,
    timezone_name: str,
    display_name: Optional[str] = None,
    brand_color: Optional[str] = None,
    skip_publish: bool = False,
) -> list[str]:
    """The exact `python -m football_blog.onboard …` the terminal would run.

    There is no parameter that could set `published`, by construction — the command's
    `--yes` path never flips the gate either, so the refusal holds from both directions
    (ADR 0037).
    """
    argv = [sys.executable, "-m", "football_blog.onboard",
            "--league-id", str(league_id),
            "--slug", slug.strip(),
            "--language", language.strip(),
            "--timezone", timezone_name.strip(),
            "--yes"]
    if display_name and display_name.strip():
        argv += ["--display-name", display_name.strip()]
    if brand_color and brand_color.strip():
        argv += ["--brand-color", brand_color.strip()]
    if skip_publish:
        argv.append("--skip-publish")
    return argv


# --------------------------------------------------------------------------- #
# Routes — reads                                                               #
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cards = list_cards()
    return templates.TemplateResponse(request, "index.html", {
        "cards": cards,
        "summary": summary(cards),
        "stages": STAGES,
        "stage_label": STAGE_LABEL,
        "languages": VALID_LANGUAGES,
        "timezones": sorted(set(COMMON_TZS.values())),
    })


@app.get("/api/defaults/{league_id}")
def api_defaults(league_id: int):
    """What the form pre-fills — and what it is careful to call a suggestion.

    Empty for a Competition not yet in the Published Store, which is most of them: the
    derivation reads the Postgres `competition` row. The form falls back to the Registry
    name in that case, which is what the card already shows.
    """
    d = derive_defaults(league_id)
    return {"defaults": d or {}, "derived": bool(d)}


@app.get("/api/cards")
def api_cards():
    cards = list_cards()
    return {
        "summary": summary(cards),
        "cards": [{"league_id": c.league_id, "name": c.name, "stage": c.stage,
                   "draftable": c.is_draftable, "blocked": c.blocked_reason}
                  for c in cards],
    }


@app.get("/api/health")
def health():
    return {"ok": True, "jobs": len(JOBS)}


# --------------------------------------------------------------------------- #
# Routes — the one write                                                       #
# --------------------------------------------------------------------------- #
@app.post("/run")
async def run(request: Request):
    body = await request.json()
    try:
        league_id = int(body.get("league_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "league_id is required."}, status_code=400)

    missing = [f for f in ("slug", "language", "timezone") if not (body.get(f) or "").strip()]
    if missing:
        # Refused here as well as in the command. Not redundant: this one produces a
        # usable message in the form, while the command's refusal is what guarantees the
        # terminal cannot be talked into inventing a permanent slug either (ADR 0037).
        return JSONResponse(
            {"error": f"Missing {', '.join(missing)} — these are editorial decisions and "
                      f"the run will not guess them."}, status_code=400)
    if body.get("language") not in VALID_LANGUAGES:
        return JSONResponse({"error": f"language must be one of {VALID_LANGUAGES}."},
                            status_code=400)

    argv = build_onboard_argv(
        league_id,
        slug=body["slug"], language=body["language"], timezone_name=body["timezone"],
        display_name=body.get("display_name"), brand_color=body.get("brand_color"),
        skip_publish=bool(body.get("skip_publish")),
    )
    job = _spawn(f"Onboard competition {league_id}", argv, league_id=league_id)
    return {"job_id": job.id, "argv": argv, "title": job.title}


# --------------------------------------------------------------------------- #
# Routes — job status                                                          #
# --------------------------------------------------------------------------- #
@app.get("/jobs")
def jobs():
    return [{"id": j.id, "title": j.title, "status": j.status, "returncode": j.returncode,
             "started_at": j.started_at, "league_id": j.league_id, "lines": len(j.lines)}
            for j in sorted(JOBS.values(), key=lambda x: x.started_at, reverse=True)]


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
                       f"data: {json.dumps({'status': job.status, 'code': job.returncode, 'league_id': job.league_id})}\n\n")
                return
            time.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream")
