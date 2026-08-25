# Adding a covered Competition is a procedure, not a decision

---
Status: accepted and built (2026-08-25) — adds the Premier League (39) to ADR 0046's
**Market Store**, and generalises what ADR 0050 did once for La Liga. Changes no rule in
ADR 0041 or ADR 0043; adds no term to CONTEXT.md. **Supersedes nothing.** Its lasting half
is the checklist: a sixth Competition needs no ADR unless it teaches something new.
---

ADR 0050 added La Liga and read as a one-off. Doing it a second time, for the Premier
League, produced no new finding — the same five edits, the same registry ratio, the same
slug trap, the same clean attach. Two runs is enough to call it a procedure.

## The Premier League, in the same two strings

**Kalshi: `KXEPLGAME` → 39.** 60 open and 30 settled markets, and the draw is once again
`111193d4-…`, the UUID every series shares.

**Polymarket: `epl` → 39, series slug `premier-league-2025`.** The trap held for a third
league: `epl-2025`, `epl-2026`, `soccer-epl` and `english-premier-league-2025` each return
zero events without erroring. If the slug were derivable from the league key, this would
be the third time it was derivable differently.

20 of 20 listed Fixtures attached on each Exchange, zero unmapped.

## The checklist

Five edits, in this order. Nothing here is a judgement call except step 2.

1. **Verify both strings against the live APIs.** A Kalshi series ticker that returns
   markets; a Polymarket series slug that returns *events with moneyline legs*. A slug
   returning `[]` is the failure mode, not an error — check the length, never the status
   code.
2. **`--propose` both registries, then map the rest by hand.** `football.teamnames`
   resolves a respelling and never an alternate name (ADR 0039), so expect roughly half
   to fall to a human — 15/20 and 10/26 for La Liga, 18/20 and 12/25 here. Review as a
   `git diff`, checking that each pair names the same club. Map **every** club the
   Exchange lists, including ones outside the Seasons `propose()` scans: they cost nothing
   and close the silent refusal that lands the day one is promoted.
3. **Add the id to `markets.store.COVERED_COMPETITIONS`.**
4. **Update the coverage sentence in `docs/market-api-contract.md`** and the competition
   set in `tests/test_market_tracks.py`.
5. **`markets.watch --dry-run`, and confirm `*_unmapped` is zero on both.** A non-zero
   count is a half-mapped registry, and every Fixture naming those clubs is refused.

Then `--settled` and a backfill, if the Competition has already played (below).

## What the Publication supplies, and the one way it can bite

Kalshi resolves on the **local match date** from the Publication's `display_timezone`
(ADR 0041/0029), so a Competition with no Publication gets no Kalshi half at all — the
Polymarket half still runs, because it matches on the kickoff instant.

Both Publications added so far are written for a Colombian audience and carry
`America/Bogota`, which is not either league's own locale. Both resolve correctly, by
arithmetic rather than by intent: every La Liga and Premier League kickoff falls between
10:00 and 21:00 UTC, so the local date, the UTC date and the Bogotá date coincide. Bogotá
is behind UTC, so a kickoff before 05:00 UTC would flip the date and attach the previous
day's market **silently**. Neither league has one. A Competition in Asia or Oceania would
break here first, and step 5's dry run is where it would show.

## Enrolment is the deadline, and it is retroactive only briefly

A market leaves both sweeps once it settles (ADR 0043), so a Competition mapped mid-season
has already lost Fixtures. `markets.watch --settled` recovers what the Exchanges still
list, and the two retentions are not alike: for La Liga, Polymarket's closed events reached
back four months and Kalshi's settled markets ten days. Run `--settled` **immediately**
after step 5, not at leisure — the Kalshi half of an already-played Fixture is the part
with a clock on it.
