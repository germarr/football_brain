# La Liga is the fourth Competition both Exchanges cover

---
Status: accepted and built (2026-08-25) — extends ADR 0046's **Market Store** and ADR
0043's coverage set by one Competition. Adds nothing to the model: no new term, no new
table, no new resolution rule. Obeys ADR 0041/0043 in full — both **Exchange team
registries** are extended by hand and reviewed as a diff, and a series slug is *verified*
rather than guessed. Amends `docs/market-api-contract.md`'s coverage sentence only.
---

La Liga (140) has been in the **Published Store** since ADR 0003 and has a **Publication**
(`la-liga-espanola`), but `markets.watch` never looked at it: `COVERED_COMPETITIONS` read
`(253, 262, 772)` and neither **Exchange team registry** named a La Liga club. Both
Exchanges have listed the league all along. Every La Liga Fixture since the store was
switched on is a **Winner Market** nobody enrolled, and ADR 0046's deadline applies to
each of them — a settled market leaves the sweep, so those are gone.

## The two strings, and how each was verified

**Kalshi: `KXLALIGAGAME` → 140.** `/markets?series_ticker=KXLALIGAGAME` returns 75 open
and 51 settled markets, priced, unpaginated — the same shape as `KXMLSGAME`. The draw is
the **same UUID** as everywhere else (`111193d4-…`), so the structural draw rule needed no
change and `propose()`'s one-draw-UUID assertion still holds across four series.

**Polymarket: `lal` → 140, series slug `la-liga-2025`.** The league key is `lal` — it keys
the teams (`/teams?league=lal`) and heads every event slug (`lal-cel-osa-2026-08-16`). The
slug that *enumerates* the league is neither of those. `laliga-2025`, `lal-2025`,
`soccer-lal` and `la-liga` each return **zero events without erroring** — precisely the
silent-empty failure ADR 0043 recorded for `sport=lec`, and the reason the registry stores
a verified slug rather than deriving one from the league key.

The series slug keeps its `-2025` while listing 2026/27 Fixtures, exactly as `mls-2025`
and `mex-2025` do. It is an identifier, not a season pin.

## Two things that could have been wrong and are not

**The slug date lies here too, and it does not matter.** `lal-cel-osa-2026-08-16` is a
`2026-08-27 18:30Z` kickoff. Resolution reads `startTime`, never the slug (ADR 0043), so
the Fixture attached anyway. The one live example is a useful reminder that the rule is
load-bearing rather than defensive.

**The Publication's timezone is `America/Bogota`, not `Europe/Madrid`.** Kalshi resolves on
the **local match date** taken from the Publication's `display_timezone` (ADR 0041/0029),
and La Liga's Publication is written for a Colombian audience. It resolves correctly, but
by arithmetic rather than by intent: every La Liga kickoff falls between 10:00 and 21:00
UTC, so the Madrid date, the UTC date and the Bogotá date are the same date, and the
ticker's `26AUG27` matches. Bogotá is behind UTC, so a kickoff before 05:00 UTC would flip
the date and silently attach the previous day's market. La Liga has none, and no scheduling
change can create one — but a Competition whose Publication timezone is on the other side
of its own locale, with a small-hours kickoff, would break here first.

25 of 25 listed Fixtures attached on each Exchange, with **zero unmapped**, against 54
scheduled Fixtures inside the 35-day horizon; the other 29 are `not_listed` — Kalshi opens
a game market two to five days out.

## The registries

Kalshi gained 20 clubs, Polymarket 26. Canonical name matching reached 15 and 10 of them;
the rest were added by hand and reviewed as a diff, which is the ratio ADR 0041 predicted
and the reason the registries exist:

| Exchange writes | we write |
| --- | --- |
| `Bilbao` | Athletic Club |
| `Santander` / `Real Racing Club` | Racing Santander |
| `Vallecano` / `Rayo Vallecano de Madrid` | Rayo Vallecano |
| `Atletico` / `Club Atlético de Madrid` | Atletico Madrid |
| `Deportivo De La Coruna` / `RC Deportivo A Coruña` | Deportivo La Coruna |

The Polymarket side maps all 26 clubs the league has ever fielded, three of them
(Leganés, Las Palmas, Valladolid) beyond the Seasons `propose()` scans. They cost nothing
now and close the silent refusal that would otherwise land on the day one is promoted —
a **refusal** is correct behaviour but reads as "not listed", and nobody would look.

## What this ADR does not do

It does not backfill. `markets.watch --settled` reaches Final Fixtures the Exchanges still
list — 51 settled `KXLALIGAGAME` markets and Polymarket's closed `la-liga-2025` events —
and `markets.backfill` harvests their **Market Candles**. That run is a decision to spend
time and requests, not a consequence of this mapping, and is left to a human.
