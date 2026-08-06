"""FastAPI application for the Desk (ADR 0034).

The split that governs every route here:

  **Reads run in-process, read-only, and instantly.** The board, the assembled prompt,
  the ESPN proposals. No subprocess, no writes, no API quota. In particular the prompt
  preview calls `prompts.assemble_prompt` directly and never `pipeline --dry-run`,
  which executes stages 1-3 before it prints anything — a preview that refreshes the
  frontier and delta-publishes is not a preview.

  **Writes run as a subprocess of the exact CLI argv, streamed over SSE.** The pipeline
  run and Refresh Candidates. Identical to what the terminal would run, byte for byte,
  which is ADR 0021's rule that the UI is a *second* front door and never a
  reimplementation. The job machinery below is `console/app.py`'s, adapted.

Two things this app will not do, both deliberate:

  - **It never emits `--redraft`.** That flag overwrites a *published* Match Post,
    destroying hand-edited prose nothing can regenerate, in the only store with no
    rebuild path. A published Match Post renders retired with the command to paste.
    Friction is the only protection it has (ADR 0034).
  - **It never edits or publishes a Narrative.** It shows the log and links to the
    PocketBase admin.

Bound to 127.0.0.1: it spends API-Football quota, ESPN requests and Anthropic tokens,
and it writes to the Editorial Store.
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

from football_blog import espn_lookup
from football_blog.candidates import DEFAULT_DAYS, BoardRow, list_board, list_competitions
from football_blog.loader import load_post_bundles
from football_blog.pocketbase import PocketBaseClient
from football_blog.prompts import assemble_prompt, load_system_prompt, system_prompt_path

_HERE = Path(__file__).resolve().parent
LOG_DIR = _HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)

POCKETBASE_ADMIN = "http://127.0.0.1:8090/_/#/collections?collection=match_post"

app = FastAPI(title="La Cancha — the Desk")
templates = Jinja2Templates(directory=str(_HERE / "templates"))
# The nav lives in `surfaces/templates/`, one level up. Appended here rather than
# injected by the composition root (ADR 0035's arrangement) because a sibling directory
# always exists — so `_nav.html` is included unconditionally and a rename fails loudly
# (ADR 0036).
templates.env.loader = ChoiceLoader([
    templates.env.loader,
    FileSystemLoader(str(_HERE.parent / "templates")),
])



def _base_url(request: Request) -> str:
    """This app's mount prefix: `""` standalone, `"/desk"` under `surfaces` (ADR 0035).

    Every absolute path in the Desk's templates is written against this rather than
    hardcoded, so the same templates serve both `python -m surfaces.desk` and the
    composed surfaces. Hardcoding `/desk` would have worked when mounted and 404'd
    standalone — leaving the debug entrypoint as the one place the bug hides.
    """
    return request.scope.get("root_path", "")


templates.env.globals["base_url"] = _base_url


# --------------------------------------------------------------------------- #
# Jobs — console/app.py's machinery, same shape                                #
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
    fixture_id: Optional[int] = None
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


def _spawn(title: str, argv: list[str], *, fixture_id: Optional[int] = None) -> Job:
    now = dt.datetime.now()
    job_id = uuid.uuid4().hex[:12]
    log_path = LOG_DIR / f"{now:%Y%m%d-%H%M%S}-{job_id}.log"
    logfile = open(log_path, "w")
    logfile.write(f"$ {' '.join(argv)}\n\n")
    logfile.flush()

    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        argv, cwd=str(football_config.ROOT), env=env, text=True, bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    job = Job(id=job_id, title=title, argv=argv, fixture_id=fixture_id,
              started_at=now.isoformat(timespec="seconds"), proc=proc, _logfile=logfile)
    job.lines.append(f"$ {' '.join(argv)}")
    JOBS[job_id] = job
    threading.Thread(target=_reader, args=(job,), daemon=True).start()
    return job


# --------------------------------------------------------------------------- #
# argv builders — the only place a command line is composed                    #
# --------------------------------------------------------------------------- #
def build_pipeline_argv(
    fixture_id: int,
    espn_id: Optional[str] = None,
    *,
    force_link: bool = False,
    reclassify: bool = False,
    instruction: Optional[str] = None,
    dry_run: bool = False,
) -> list[str]:
    """The exact `python -m football_blog.pipeline …` the terminal would run.

    `--redraft` is absent by construction and there is no parameter that could
    introduce it. A published Match Post is refused by `preflight`, which is what we
    want: the Desk does not offer the waiver, so the refusal stands (ADR 0034).
    """
    argv = [sys.executable, "-m", "football_blog.pipeline",
            "--fixture-id", str(fixture_id)]
    if espn_id:
        argv += ["--espn-id", str(espn_id).strip()]
    if force_link:
        argv.append("--force-link")
    if reclassify:
        argv.append("--reclassify")
    if instruction and instruction.strip():
        argv += ["--instruction", instruction.strip()]
    if dry_run:
        argv.append("--dry-run")
    return argv


def build_refresh_argv(days: int = DEFAULT_DAYS) -> list[str]:
    return [sys.executable, "-m", "football_blog.candidates", "--days", str(days)]


# --------------------------------------------------------------------------- #
# Read helpers                                                                 #
# --------------------------------------------------------------------------- #
def _board(days: int, league_id: Optional[int] = None) -> list[BoardRow]:
    pb = PocketBaseClient()
    try:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        return list_board(pb, since=since, league_id=league_id)
    finally:
        pb.close()


def _row(fixture_id: int) -> Optional[BoardRow]:
    """One board row, resolved directly rather than by scanning a window.

    A Fixture reached by URL must resolve even when it has fallen off the default
    board, so this ignores the date window entirely — but it applies the same two
    gates, so a non-Final or unpublished-Competition id still comes back as None.
    """
    pb = PocketBaseClient()
    try:
        rows = list_board(pb, fixture_ids=[fixture_id])
    finally:
        pb.close()
    return rows[0] if rows else None


# --------------------------------------------------------------------------- #
# Routes — reads                                                               #
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request, days: int = DEFAULT_DAYS, league_id: Optional[int] = None):
    # The window's whole board is fetched even when a Competition is selected, and the
    # narrowing happens here rather than in SQL. The chips carry counts, and a count
    # per Competition needs the unfiltered set anyway — so this is one pass, not a
    # query per chip. `list_board(league_id=…)` stays for the CLI, which prints no
    # counts and can afford the narrower query.
    all_rows = _board(days)
    rows = [r for r in all_rows if league_id is None or r.league_id == league_id]

    pb = PocketBaseClient()
    try:
        competitions = list_competitions(pb)
    finally:
        pb.close()
    counts = {c.league_id: sum(1 for r in all_rows if r.league_id == c.league_id)
              for c in competitions}

    return templates.TemplateResponse(request, "index.html", {
        "rows": rows,
        "days": days,
        "competitions": competitions,
        "counts": counts,
        "league_id": league_id,
        "n_all": len(all_rows),
        "selected": next((c for c in competitions if c.league_id == league_id), None),
        # The glossary names the default rather than hardcoding 14 in prose — the
        # window is a display choice, and the one place it is decided is candidates.py.
        "default_days": DEFAULT_DAYS,
        "n_candidates": sum(1 for r in rows if r.is_candidate),
        "pocketbase_admin": POCKETBASE_ADMIN,
    })


@app.get("/candidate/{fixture_id}", response_class=HTMLResponse)
def candidate(request: Request, fixture_id: int):
    row = _row(fixture_id)
    if row is None:
        return templates.TemplateResponse(request, "missing.html", {
            "fixture_id": fixture_id,
            "pocketbase_admin": POCKETBASE_ADMIN,
        }, status_code=404)

    pb = PocketBaseClient()
    try:
        publication = pb.get_publication_by_competition_id(row.league_id)
    finally:
        pb.close()

    return templates.TemplateResponse(request, "candidate.html", {
        "row": row,
        "publication": publication,
        "pocketbase_admin": POCKETBASE_ADMIN,
        "redraft_command": (
            f"uv run python -m football_blog.pipeline --fixture-id {row.fixture_id} "
            f"--espn-id <ESPN> --redraft"
        ),
    })


@app.get("/api/candidate/{fixture_id}/prompt")
def api_prompt(fixture_id: int, instruction: str = ""):
    """The three layers, exactly as the model will receive them.

    Assembled by `prompts.assemble_prompt` — the same function `draft_narrative` calls.
    If these two ever diverge, the Desk shows a prompt it does not send, and nothing
    reports it (ADR 0034).
    """
    bundle = load_post_bundles([fixture_id]).get(fixture_id)
    if bundle is None:
        return JSONResponse(
            {"error": f"Fixture {fixture_id} is not in the Published Store. "
                      f"Refresh the board first."}, status_code=404)

    pb = PocketBaseClient()
    try:
        publication = pb.get_publication_by_competition_id(bundle.fixture.league_id)
    finally:
        pb.close()
    if not publication:
        return JSONResponse(
            {"error": f"No Publication covers competition {bundle.fixture.league_id}."},
            status_code=404)

    lang = publication["default_language"]
    tz = publication["display_timezone"]
    overrides = publication.get("llm_prompt_overrides") or ""
    system_prompt, user_prompt = assemble_prompt(
        bundle, lang, tz, overrides or None, instruction or None)

    return {
        "language": lang,
        "display_timezone": tz,
        # Layer 1 — the git file, editable.
        "system_base": load_system_prompt(lang),
        "system_path": str(system_prompt_path(lang)),
        # Layer 2 — the Publication's standing style, editable.
        "publication_overrides": overrides,
        "publication_id": publication["id"],
        # Layer 3 — derived from the Published Store. Read-only, and the reason is in
        # ADR 0034: a hand-edited facts block makes the input to an unregenerable
        # Narrative unrecorded too.
        "user_prompt": user_prompt,
        # What is actually sent, for the character counts on screen.
        "system_assembled": system_prompt,
        "chars_system": len(system_prompt),
        "chars_user": len(user_prompt),
    }


@app.get("/api/candidate/{fixture_id}/espn")
def api_espn(fixture_id: int, refresh: bool = False):
    """ESPN game ids whose kickoff is within tolerance of ours.

    Loaded from the page rather than rendered with it, so a slow or unreachable ESPN
    never blocks the Desk. Filtered on kickoff alone — never on team names, which are
    exactly what disagree, and filtering on them would hide the true match.
    """
    row = _row(fixture_id)
    if row is None:
        return JSONResponse({"error": f"Fixture {fixture_id} is not on the board."},
                            status_code=404)
    try:
        proposals = espn_lookup.propose(row.kickoff, refresh=refresh)
    except espn_lookup.LookupUnavailable as e:
        # Never an empty list for this case: "no match found" and "could not ask" are
        # different answers, and rendering them alike sends an operator hunting for a
        # game id that was there all along (ADR 0034).
        return JSONResponse({"unavailable": True, "error": str(e)}, status_code=503)

    return {
        "ours": {
            "home": row.home_team_name,
            "away": row.away_team_name,
            "kickoff": row.kickoff.isoformat(),
        },
        "proposals": [
            {
                "game_id": p.game_id,
                "home": p.home_team,
                "away": p.away_team,
                "kickoff": p.kickoff.isoformat(),
                "drift_minutes": round(p.drift_minutes, 1),
                "exact_kickoff": p.exact_kickoff,
                "league": p.league,
                "completed": p.completed,
                "detail": p.detail,
                "url": p.commentary_url,
                # Whether ingest will need --force-link. Compared here only to *show*
                # the operator; `commentary/fixture_link.py` makes the real decision.
                "names_agree": (
                    p.home_team.strip().casefold() == row.home_team_name.strip().casefold()
                    and p.away_team.strip().casefold() == row.away_team_name.strip().casefold()
                ),
            }
            for p in proposals
        ],
    }


# --------------------------------------------------------------------------- #
# Routes — writes to the authored prompt layers                                #
# --------------------------------------------------------------------------- #
@app.post("/api/prompt/system")
async def write_system_prompt(request: Request):
    """Layer 1 — write `system_{lang}.md` back to the working tree.

    Never commits. The edit lands as an ordinary working-tree change so `git diff`
    shows it like any other, which is the review this deserves: one file shapes every
    Narrative written in that language.
    """
    body = await request.json()
    lang = (body.get("language") or "").strip()
    text = body.get("text")
    if not lang or text is None:
        return JSONResponse({"error": "language and text are required."}, status_code=400)

    path = system_prompt_path(lang)
    if not path.exists():
        return JSONResponse({"error": f"No system prompt for language '{lang}'."},
                            status_code=404)
    if not text.strip():
        return JSONResponse(
            {"error": "Refusing to write an empty system prompt — it carries every "
                      "rule the model follows, including 'never invent data'."},
            status_code=400)

    path.write_text(text)
    return {"ok": True, "path": str(path), "chars": len(text)}


@app.post("/api/prompt/publication")
async def write_publication_overrides(request: Request):
    """Layer 2 — the Publication's standing style guidance, in the Editorial Store."""
    body = await request.json()
    publication_id = (body.get("publication_id") or "").strip()
    text = body.get("text")
    if not publication_id or text is None:
        return JSONResponse({"error": "publication_id and text are required."},
                            status_code=400)

    pb = PocketBaseClient()
    try:
        r = pb._client.patch(
            f"{pb.base_url}/api/collections/publication/records/{publication_id}",
            headers=pb._headers(),
            json={"llm_prompt_overrides": text},
        )
        r.raise_for_status()
        record = r.json()
    finally:
        pb.close()
    return {"ok": True, "chars": len(record.get("llm_prompt_overrides") or "")}


# --------------------------------------------------------------------------- #
# Routes — writes that spawn                                                   #
# --------------------------------------------------------------------------- #
@app.post("/run")
async def run(request: Request):
    body = await request.json()
    fixture_id = body.get("fixture_id")
    if fixture_id is None:
        return JSONResponse({"error": "fixture_id is required."}, status_code=400)

    argv = build_pipeline_argv(
        int(fixture_id),
        body.get("espn_id"),
        force_link=bool(body.get("force_link")),
        reclassify=bool(body.get("reclassify")),
        instruction=body.get("instruction"),
        dry_run=bool(body.get("dry_run")),
    )
    job = _spawn(f"Draft fixture {fixture_id}", argv, fixture_id=int(fixture_id))
    return {"job_id": job.id, "argv": argv, "title": job.title}


@app.post("/refresh-candidates")
async def refresh_candidates(request: Request):
    body = await request.json() if await request.body() else {}
    days = int(body.get("days") or DEFAULT_DAYS)
    argv = build_refresh_argv(days)
    job = _spawn("Refresh Candidates", argv)
    return {"job_id": job.id, "argv": argv, "title": job.title}


# --------------------------------------------------------------------------- #
# Routes — job status                                                          #
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {"ok": True, "jobs": len(JOBS)}


@app.get("/jobs")
def jobs():
    return [
        {"id": j.id, "title": j.title, "status": j.status, "returncode": j.returncode,
         "started_at": j.started_at, "fixture_id": j.fixture_id, "lines": len(j.lines)}
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
                       f"data: {json.dumps({'status': job.status, 'code': job.returncode, 'fixture_id': job.fixture_id})}\n\n")
                return
            time.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream")
