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
#: This package's single definition, and the reason it is here rather than in a
#: module: it is one of the two conditions that make a Fixture a **Drafting
#: Candidate** (CONTEXT.md), so `pipeline`, `draft`, `onboard` and the Desk must all
#: read the same tuple. Until ADR 0034 they did not — `pipeline` held the constant
#: while `draft` and `onboard` inlined `IN ('FT','AET','PEN')` into SQL, which is the
#: drift ADR 0034 warns about, already present before the Desk existed. A divergence
#: here makes the Desk *hide* work the pipeline would happily draft, with no error.
#:
#: Deliberately NOT shared with `refresh.core`, `web.app`, `web.publish` or
#: `live.poll`, which each hold their own. Those are separate contexts, and under
#: ADR 0031 a context does not import another's internals to save four strings.
#:
#: A tuple, not a set: it is passed to psycopg as `= ANY(%s)` (via `list(...)`) and
#: printed in operator-facing refusals, both of which want a stable order.
FINAL_STATUSES = ("FT", "AET", "PEN")
