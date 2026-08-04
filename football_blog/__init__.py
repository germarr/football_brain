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
