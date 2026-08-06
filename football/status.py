"""The provider's Fixture status vocabulary — the one definition (CONTEXT.md).

Until this module there were six, in five packages: `refresh.core`, `serving.publish`,
`live.poll`, `surfaces.viewer.app` and `football_blog`, plus inline `IN ('FT','AET','PEN')`
in SQL. They agreed, which is exactly why the duplication was survivable and exactly why
a divergence would not have been noticed — `football_blog`'s own note recorded that
`draft` and `onboard` had already drifted from `pipeline` once, before the Desk existed,
and that such a divergence makes the Desk *hide* work the pipeline would happily draft,
with no error anywhere (ADR 0033/0034).

That note also argued *against* sharing: "a context does not import another's internals
to save four strings" (ADR 0031). It is right about contexts and does not apply here.
This is the **kernel** — the part ADR 0031 describes as "what everything imports" — not
a context's internals, and this module imports nothing at all, so a package takes on no
dependency beyond four tuples by reading it. `football_blog` was the one package
importing nothing from `football`; that edge is new and costs a bare docstring.

Deliberately *not* resolved here: **CONTEXT.md and the Viewer disagree about `SUSP`.**
The glossary groups it with the terminal-but-dataless states, while the Viewer treats a
suspended match as live — it may yet resume — and leaves it out of `TERMINAL`. The code's
behaviour is preserved exactly as it was, because consolidating definitions is not the
moment to change which fixtures the Refresh button polls. It is a real question and it
needs answering somewhere other than a mechanical merge.

Tuples rather than sets, throughout: they are interpolated into SQL and psycopg
`= ANY(%s)` parameters, and printed in operator-facing refusals, all of which want a
stable order. Membership tests read the same either way at this size.
"""
from __future__ import annotations

#: **Final** — played to completion, and the only state with per-fixture data to collect
#: (squad, events, team match stats). A Refresh collects a Fixture's per-match data
#: exactly once it is Final and never before (CONTEXT.md).
FINAL: tuple[str, ...] = ("FT", "AET", "PEN")

#: Ended, but with no played result to collect: postponed, cancelled, abandoned, awarded,
#: walkover. Terminal in the sense that no further polling will change them, and *not*
#: Final — an `AWD` carries a scoreline without a match having been played.
ENDED_WITHOUT_RESULT: tuple[str, ...] = ("PST", "CANC", "ABD", "AWD", "WO")

#: Settled: nothing a later poll or refresh can change. The complement is what the
#: Viewer's Refresh button targets and what the Live Mirror's auto-clear drops
#: (ADR 0024). Note the absence of `SUSP` — see the module docstring.
TERMINAL: tuple[str, ...] = FINAL + ENDED_WITHOUT_RESULT

#: In progress, including the breaks within a match and `SUSP`, which may resume.
LIVE: tuple[str, ...] = ("1H", "2H", "HT", "ET", "BT", "P", "LIVE", "INT", "SUSP")
