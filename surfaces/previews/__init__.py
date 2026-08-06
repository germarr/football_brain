"""The Previews board — the next seven days of Fixtures, and what each card carries
(ADR 0040/0041).

Served at `/previews` by `python -m surfaces`. One card per scheduled Fixture in a
published Publication's Competition, showing whether its **Match Preview** has a table,
**Team Leaders** and a **Winner Market** — and, separately, the Kalshi Teams that need a
registry entry before their markets can attach.

It is a *board*, not an editor. A Match Preview is derived, so the way to change one is
to rebuild it; the buttons here fire `football_blog.preview` and `football_blog.kalshi`
as subprocesses and nothing else. Editing belongs to a **Match Post**, on the Desk.

The unmapped list names **Teams, not games**, which is the whole shape of ADR 0041: a
Winner Market attaches by Kalshi's stable team UUID, so the manual gap is a club we have
never mapped — a list that shrinks to zero and stays there — rather than the ~40 games a
week a per-match link would have demanded forever.

Fifth surface rather than a Desk tab, because CONTEXT.md defines the **Desk** as the
local surface for *writing* — it lists Drafting Candidates, shows the prompt, and fires
the drafter. A Preview board does none of those, and widening that definition would blur
the one term that draws a clean line between authored and derived.
"""
