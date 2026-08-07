# Two Exchanges, one question, and a Market Track nobody stores

---
Status: accepted — generalises ADR 0041 (Winner Markets attach by Team) to a second
Exchange without superseding it, and **corrects a factual claim in ADR 0040** that the
freeze was built on. Adds **Exchange** and **Market Track** to CONTEXT.md and a fifth
Registry.
---

A **Match Preview** shows one prediction market's view of a Fixture. Polymarket lists the
same three contracts on the same 90-minute rule, so the card can show two views instead of
one — side by side, as two graphs of probability into kickoff. Deciding how turned up one
fact that invalidates a documented premise and several that make the obvious port wrong.

**Decisions:**

- **A Winner Market belongs to a Fixture *and* an Exchange; the two are never merged.**
  Polymarket's moneyline trio is not an analogue of a Winner Market, it *is* one: three
  mutually exclusive legs (`negRisk: true`) on one Fixture's result, resolving on *"the
  outcome within the first 90 minutes of regular play plus stoppage time"* — Kalshi's rule,
  word for word, including the same legitimate disagreement with our extra-time scoreline.
  So the term goes Exchange-neutral rather than acquiring a twin. What does **not**
  generalise is the number: a **Market Probability** is normalised within one Exchange's
  three legs and never across the two. There is no consensus probability, no blend, no
  primary Exchange. Two panels, never one.

  The word is **Exchange**, not *venue* — **Venue** is the ground a Fixture is played at
  (ADR 0028/0042) — and not *book*: neither takes the other side of a bet, which is why
  their legs sum near 1 instead of to a bookmaker's margin.

- **Each Exchange resolves to a Fixture by its own rule, and the rules do not swap. Neither
  rule resolves anything on its own.** Both Exchanges match on `(both clubs via the
  registry, plus a time)`; only the time differs. Kalshi's ticker is dated in the match's
  locale, so ADR 0041 compares the **local match date**. Polymarket's slug is dated too and
  is **not usable** — `mls-ner-hou-2026-03-07` is an `08-08 20:30Z` kickoff and
  `mex-mon1-jua-2026-08-15` an `08-16 01:00Z` one — so it compares `startTime`, the **exact
  kickoff instant**, which matched our fixtures to the minute. That is a consequence of
  Polymarket's own terms: *"if the game is postponed, this market will remain open until the
  game has been completed"*, so the event outlives a reschedule and moves `startTime` while
  the slug keeps its birth date. It makes the instant strictly **more** durable than
  Kalshi's date — a postponed Fixture keeps its Polymarket Winner Market and loses its
  Kalshi one.

  **The instant narrows; the team pair decides.** A finer timestamp reads like a stronger
  key and is not one: the current Leagues Cup week has **six pairs of Fixtures kicking off
  at the same instant** (`2026-08-07 23:30Z` is both Charlotte v Atlas and Columbus Crew v
  CF Pachuca). Matching on time alone would attach one of each pair to the wrong game, and
  do it invisibly. So the key is `(frozenset(team_ids), kickoff_instant)` — structurally
  what ADR 0041 already built for Kalshi, with the date swapped for the instant.

- **A second registry file, not one file with an Exchange column — and it is keyed by
  `(league, team id)`, not by team id.** ADR 0041's reasoning survives contact with
  Polymarket unchanged — three vocabularies disagree (`Tigres UANL` / `Tigres` / `Tigres de
  la UANL`; `Toluca` / `Toluca` / `Deportivo Toluca FC`) and no canonical comparison
  reconciles them, so a hand-reviewed registry is again the only bridge. Polymarket's
  `providerId` is a third party's id (`702` for New England, where ESPN says `18418`), so it
  buys nothing and is not the key; `teams[].id` is.

  But that id is **not global, and here Kalshi and Polymarket differ in kind.** ADR 0041
  verified a single Kalshi UUID for Club América across both the Leagues Cup and Liga MX
  series. Polymarket mints a fresh id per league: CF Monterrey is `115320` in `mex` and
  `3268366` in `lec`; Columbus Crew is `115057` in `mls` and `3268353` in `lec`. A club
  playing a domestic league and the Leagues Cup therefore occupies two rows, and the file
  is **98 rows** (32 `mls` + 24 `mex` + 42 `lec`), not one row per club. Keying on the bare
  id would silently merge two clubs' entries the first time the id spaces overlap.

  The two Exchanges' registries stay separate files because the bridge differs at both ends:
  different kinds of identifier, keyed differently, feeding different match rules. Merging
  them would imply a symmetry that isn't there and would falsify ADR 0041's text, which says
  "Kalshi UUID" throughout.

- **A registry names only the Competitions its Exchange actually covers — and Polymarket
  covers all three.** `polymarket_teams.json` maps `mls → 253`, `mex → 262` and
  `soccer-lec → 772`. Leagues Cup resolved **38/38** against the current seven-day window.
  The rule still earns its place, because coverage is a per-Exchange fact that must be
  stated rather than inferred: a Fixture in a Competition an Exchange does not list is
  **not covered**, a distinct state from **not listed yet** and from **unmapped**, so the
  board never reports gaps no human can close.

- **Polymarket's league is enumerated by `series_slug`, never by the league's name.** This
  is a trap that costs a whole Competition if you miss it. Leagues Cup's *sport* slug is
  `lec` but its *tag* slug is `lcs` — which collides with the esports LCS — and passing
  `sport=lec` to `/events` returns 100 events and **zero** moneylines, silently. Neither
  `tag_slug=lec` nor `tag_slug=leagues-cup` nor `public-search?q=Leagues Cup` finds the
  structured games; the last returns only hand-made 2025 events with no `teams[]`, no
  `startTime`, and in several cases no draw leg. Only `series_slug=soccer-lec` (equivalently
  `tag_id=102449`) enumerates them. The registry therefore stores the **series slug**, and
  a league whose series slug is unverified is not added.

- **A Match Preview stores one Winner Market per Exchange, and the field names say so.**
  The point-in-time half is stored (only the **Market Track** is not), so the record now
  carries `market_kalshi` / `market_polymarket`, each with its own `market_state_*` and
  `quote_read_at_*`. The pre-existing `market`, `market_state` and `quote_read_at` were
  **renamed** rather than kept with Polymarket bolted alongside: leaving one Exchange
  holding the unqualified name would encode a seniority the rest of this decision denies,
  and a card whose percentages come from an unnamed source is the thing the two-panel
  design exists to stop. A card is **complete on one Exchange**, not both — requiring both
  would mark a card incomplete for a coverage gap nobody can close.

- **A Market Track is fetched and never stored.** Both Exchanges serve their own history on
  demand and keep serving it after settlement — 142 candlesticks on a settled Kalshi market,
  323 points on a closed Polymarket one. So the graph costs six unauthenticated GETs at read
  time and introduces **no store, no cadence, no record, no backfill and nothing to freeze**.
  A settled Match Preview's graph stays drawable forever because the Exchange keeps the data,
  not because we copied it.

  **But finding the market is resolution, and resolution only sees open markets.** A few
  hours after kickoff Kalshi moves the market to `settled` and Polymarket marks the event
  `closed`, at which point neither sweep returns it (`KXMLSGAME`: 31 open against 53
  settled, disjoint; `mls-2025`: 234 open against 1,325 closed, disjoint). The history is
  still served — it is the *pointer to it* that expires. So a Track for a played Fixture is
  built from the identifiers the **frozen record already holds** (`series_ticker` plus each
  leg's `market_ticker`; each leg's `token_id`), never by re-resolving. This is why those
  identifiers are stored on the outcomes at all, and it makes the graph cheaper before
  settlement too: reading the record skips both sweeps (~2.1s to ~0.4s).

- **A Market Track is built to the same rule as the number on the card.** Legs are bucketed
  to the hour — Kalshi's are hour-aligned already (`ts % 3600 == 0`), Polymarket's drift by
  3–372 seconds and share an exact timestamp **0% of the time**, so bucketing is required
  rather than tidy. An hour missing any of the three legs is a **gap in the line**. That is
  ADR 0041's refusal applied to a series: normalising over two of three legs invents a
  distribution nobody quoted. The alternative — plotting raw published mids — was rejected
  because the lines would sum to 99.5–106.5% and the last point would not equal the
  percentage printed on the card directly above it.

- **One shared time axis, with "not listed" drawn rather than left blank.** The two Tracks
  are not the same length and never will be. On New England v Houston: Polymarket 743 hourly
  points spanning 30d 23h (drifting `0.505 → 0.395`), Kalshi 5 points spanning 4h, its
  `open_time` being `2026-08-06 19:36Z` for an `08-08 20:30Z` kickoff. Independent axes would
  render a month-long drift and a four-hour wobble at the same width — the same lie by
  omission the glossary already forbids for volume. So both panels share the earlier
  first-point to kickoff, and the interval before an Exchange listed the Fixture is labelled.
  When an Exchange began having an opinion is part of what the pair shows.

**Consequences:**

- **ADR 0040 and `preview.py` state something false and must be corrected.** Both justify
  the freeze with *"Kalshi's candlesticks come back empty for these series"*. They do not:
  `/series/{series}/markets/{ticker}/candlesticks?period_interval=60` returns hourly bid/ask
  OHLC, on open and settled markets alike. The freeze is kept, but its reason is replaced. A
  Quote does not stop at kickoff — it converges on the result and settles at 1 and 0 (the
  closed Polymarket legs read `0.9995` and `0.0005`). Re-running a settled Fixture would
  overwrite a **forecast** with an **outcome**, both spelled as three percentages, with
  nothing on the card to say which was stored. The freeze preserves that distinction, not
  the data.

- **Volume is not comparable across Exchanges and must never share an axis.** Kalshi counts
  contracts (`139`), Polymarket counts dollars traded (`41,814.17`). No conversion exists.
  A quoted market with **no** volume must say *no depth* rather than render blank: on the
  first run carrying both Exchanges, every disagreement wider than 10 percentage points
  sat on an untraded Polymarket book (Seattle v Querétaro: Kalshi `47/17/37` against
  Polymarket `26/43/31`, zero volume), and a blank cell reads as "no information" when the
  fact is the opposite claim.

- **Two settled Match Previews are Kalshi-only forever.** The schema change landed with 38
  records, all `upcoming` — the cheapest possible moment, since the next run rewrote every
  one. But two Fixtures kicked off *during* that run and froze before a Polymarket block
  had ever been written for them. The freeze is absolute, so fixtures 1530121 and 1530122
  keep one Exchange permanently. This is the freeze behaving correctly, not damage, and it
  is recorded here so nobody later reads those two as a migration failure.

- **"Overround" keeps its name but loses its sign.** Kalshi's raw mids sum to ~1.005–1.025;
  Polymarket's observed range is 0.995–1.065, so a sum below 1 is ordinary there.

- **Depth varies by Competition far more than coverage does.** All three Competitions
  resolve on both Exchanges, but Polymarket's Liga MX moneylines trade at **$7–$360** while
  its Leagues Cup ones trade at **$7k–$84k** on the same weekend. A Polymarket panel on a
  Liga MX Fixture is a nearly-flat line drawn on almost no depth, and volume is the only
  thing on the card that says so.

- **The whole Polymarket half is unauthenticated**, like Kalshi's: Gamma for discovery,
  CLOB `/prices-history` for a Track. One implementation note that costs an afternoon to
  rediscover — CLOB is behind Cloudflare and 403s (`error code: 1010`) on a
  `Python-urllib` user agent. `httpx`, which this project already uses, passes untouched.

- **Polymarket ships prose we must not take.** Every game event carries
  `eventMetadata.context_description`, a model-written paragraph about form, absences and
  head-to-head. A **Match Preview** carries no prose and the writing on this project remains
  the **Narrative**'s alone (ADR 0029/0040). It is not read, not stored, not shown.
