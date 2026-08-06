"""La Cancha — the content pipeline that fills the Editorial Store.

Three entry points:
    python -m football_blog.pipeline  — Fixture id + ESPN game id -> a Match Post draft
    python -m football_blog.draft     — draft Narratives for finished Fixtures (sweeps)
    python -m football_blog.onboard   — 8-step Publication onboarding checklist

See docs/adr/0029 for the design context, and CONTEXT.md for Editorial Store,
Publication, Match Post and Narrative.

Note: the "keep in sync with blog/la-cancha/src/lib/*.ts" comments throughout this
package refer to an Astro repo that is not on this machine, so the parity tests they
mention cannot run here. Those ADR numbers cited in older docstrings are that repo's,
not this one's.
"""
__version__ = "0.1.0"

#: The statuses that mean "this match is over and its data will not change again".
#:
#: Re-exported under this package's own name because `pipeline`, `draft`, `onboard` and
#: `candidates` all import it from here, and because it is one of the two conditions that
#: make a Fixture a **Drafting Candidate** (CONTEXT.md) — worth naming where that concept
#: lives. Until ADR 0034 these modules did not agree: `pipeline` held the constant while
#: `draft` and `onboard` inlined `IN ('FT','AET','PEN')` into SQL. A divergence makes the
#: Desk *hide* work the pipeline would happily draft, with no error.
#:
#: This note used to say the tuple was deliberately NOT shared with `refresh.core`,
#: `surfaces.viewer.app`, `serving.publish` or `live.poll`, on the grounds that "a context
#: does not import another's internals to save four strings" (ADR 0031). That is right
#: about contexts and wrong about this one: `football.status` is the **kernel**, which
#: ADR 0031 calls "what everything imports", and it imports nothing itself. Six copies of
#: one CONTEXT.md definition was the larger risk.
from football.status import FINAL as FINAL_STATUSES  # noqa: E402
