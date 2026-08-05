"""Mount the Viewer at `/`, the Desk at `/desk`, the Console at `/console` (ADR 0035).

Three mechanics, all of them load-bearing:

  **Mount order.** The Viewer is mounted at `/`, which matches everything, so it must
  be registered *last* — Starlette tries routes in order. Anything mounted after it is
  unreachable.

  **The Viewer keeps `/` and this process keeps `:8001`.** So every Viewer URL survives
  in path *and* port — `/fixture/1550918`, `/league/262`, `/week` — and its templates
  need no edits. Only the Desk's and the Console's paths take a prefix, which each
  derives per-request from `root_path` so both still work standalone.

  **The header is lent, not imported.** No app may know this package exists, so
  `surfaces` reaches into the loaders of what it just composed and adds its own template
  directory. All three include `_nav.html` with `ignore missing`, so booting any of them
  alone renders its header with no nav and no error.

It owns exactly two routes — redirects from the bare `/desk` and `/console` — and
nothing else. The three surfaces answer everything.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from jinja2 import ChoiceLoader, FileSystemLoader

from console.app import app as console_app
from console.app import templates as console_templates
from football_blog.desk.app import app as desk_app
from football_blog.desk.app import templates as desk_templates
from web.app import app as viewer_app
from web.app import templates as viewer_templates

_HERE = Path(__file__).resolve().parent
TEMPLATES = _HERE / "templates"

app = FastAPI(title="La Cancha — local surfaces")


def _lend_templates(templates) -> None:
    """Let a mounted app resolve `surfaces/templates/*` after its own.

    Its own loader stays first, so an app can always override anything lent to it.
    This is the only contact between the three packages, and it runs at import time
    here rather than anywhere inside `web` or `football_blog`.
    """
    templates.env.loader = ChoiceLoader([
        templates.env.loader,
        FileSystemLoader(str(TEMPLATES)),
    ])


_lend_templates(viewer_templates)
_lend_templates(desk_templates)
_lend_templates(console_templates)


@app.get("/desk", include_in_schema=False)
def _desk_root() -> RedirectResponse:
    """A mount at `/desk` matches `/desk/…` and *not* the bare `/desk`, which would
    otherwise fall through to the catch-all Viewer and 404 — a URL anyone would type by
    hand and the first one anyone links to."""
    return RedirectResponse("/desk/")


@app.get("/console", include_in_schema=False)
def _console_root() -> RedirectResponse:
    """Same as `/desk` — see above."""
    return RedirectResponse("/console/")


# Order matters: the prefixes first, then the catch-all — Starlette tries routes in
# order, and nothing registered after a mount at `/` is ever reached.
app.mount("/desk", desk_app)
app.mount("/console", console_app)
app.mount("/", viewer_app)
