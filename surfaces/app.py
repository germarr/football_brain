"""Mount the Viewer at `/`, the Desk, the Console, Competitions and Previews
(ADR 0035/0037/0040).

Three mechanics, all of them load-bearing:

  **Mount order.** The Viewer is mounted at `/`, which matches everything, so it must
  be registered *last* — Starlette tries routes in order. Anything mounted after it is
  unreachable.

  **The Viewer keeps `/` and this process keeps `:8001`.** So every Viewer URL survives
  in path *and* port — `/fixture/1550918`, `/league/262`, `/week` — and its templates
  need no edits. Only the Desk's and the Console's paths take a prefix, which each
  derives per-request from `root_path` so both still work standalone.

  **The header is a shared template, not lent machinery.** ADR 0035 had this package
  reach into each app's Jinja loader at mount time, because the three lived in packages
  that could not name each other. ADR 0036 made them subpackages, so each now appends
  `surfaces/templates/` to its own loader at import and includes `_nav.html`
  unconditionally. A renamed nav raises `TemplateNotFound` instead of silently vanishing
  from every page — the `ignore missing` that used to hide that is gone.

It owns only redirects from the bare `/desk`, `/console`, `/competitions` and
`/previews` — a Mount matches below its prefix, never the prefix itself. The five
surfaces answer everything else.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from .competitions.app import app as competitions_app
from .console.app import app as console_app
from .desk.app import app as desk_app
from .previews.app import app as previews_app
from .viewer.app import app as viewer_app

app = FastAPI(title="La Cancha — local surfaces")


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


@app.get("/competitions", include_in_schema=False)
def _competitions_root() -> RedirectResponse:
    """Same as `/desk` — see above."""
    return RedirectResponse("/competitions/")


@app.get("/previews", include_in_schema=False)
def _previews_root() -> RedirectResponse:
    """Same as `/desk` — see above."""
    return RedirectResponse("/previews/")


# Order matters: the prefixes first, then the catch-all — Starlette tries routes in
# order, and nothing registered after a mount at `/` is ever reached.
app.mount("/desk", desk_app)
app.mount("/console", console_app)
app.mount("/competitions", competitions_app)
app.mount("/previews", previews_app)
app.mount("/", viewer_app)
