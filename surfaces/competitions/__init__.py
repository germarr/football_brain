"""The Competitions board — every Competition we collect, and how far it is from the
blog (ADR 0037).

Served at `/competitions` by `python -m surfaces`. A card per Registry Competition
showing the furthest **stage** it has reached — *Collected → In the Published Store →
Has a Publication → **Draftable** → Live* — and one button that advances it.

The button stops at **Draftable** and never crosses the last stage: a Publication's
`published` gate is flipped by a human, so a Competition can be drafted against for
weeks while nothing about it is public (CONTEXT.md).

Reads three stores and owns none: the Registry for the population, `serving/serve.db`
for what we hold, and the Published + Editorial Stores for the last two stages. The one
write is a subprocess of `python -m football_blog.onboard`, byte for byte what the
terminal would run (ADR 0021).

Named for the glossary's noun rather than the friendlier word: **Leagues** is on the
Publication entry's `_Avoid_` list, precisely because a Publication may cover a cup.
"""
