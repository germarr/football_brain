"""The **Market Store** and the public read API over it (ADR 0046).

Four modules, in the order a Fixture passes through them:

    watch.py     enrol every covered Winner Market and observe it hourly
    poll.py      collect Market Observations through the In-Play Window
    backfill.py  harvest Market Candles from the Exchanges' own history
    api.py       serve the dashboard, read-only, to lacancha.gerardomarr.com

The **Exchange adapters are not here.** `football_blog.kalshi` and
`football_blog.polymarket` already resolve a Winner Market to a Fixture — the team
registries, the two match rules, the refusal of a half-mapped market (ADR 0041/0043) —
and that logic has exactly one correct implementation. This package imports them and
adds no resolution of its own. The dependency runs one way only: nothing in
`football_blog` imports anything here, so the blog keeps working if this package is
removed entirely.
"""
