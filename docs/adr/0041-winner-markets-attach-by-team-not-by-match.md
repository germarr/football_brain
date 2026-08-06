# Winner Markets attach by Team, not by match

---
Status: accepted — supports ADR 0040 (Match Previews). Deliberately does **not** follow
the ESPN precedent of ADR 0026/0029/0030/0038/0039. Adds a third **Registry** to
CONTEXT.md alongside the Competition and Venue registries.
---

A **Match Preview** needs the prediction market's view of a Fixture, which means deciding
which Kalshi market is this Fixture's. The obvious move is to copy what we already do for
ESPN: a hand-supplied per-match link, verified on kickoff and team names before it is
stored. That would be wrong here, and the reason is worth writing down because the
similarity is so inviting.

**Decisions:**

- **The hand-supplied unit is the Team, not the match.** ESPN's link is per-match for
  exactly one reason ADR 0029 states outright: *"the ESPN game id has no lookup"*. That
  constraint does not hold for Kalshi. Every market carries
  `custom_strike.soccer_team` — a stable UUID, verified identical for Club América across
  both the Leagues Cup and Liga MX series — so a **Kalshi team registry** mapping
  `Kalshi UUID → our team id` (plus series → Competition) makes market resolution
  automatic and total. It is 49 rows for the three Competitions in scope, filled once.
  The per-match alternative is roughly 40 hand-supplied links *every week, forever*, to
  avoid 49 one-time ones. The draw carries its own constant UUID shared by every Winner
  Market, so it is recognised structurally rather than by matching the string `"Tie"`.

- **Names are not evidence, and no canonical comparison rescues them.** ADR 0039 taught
  us to compare team names canonically, and it is not enough here. It would resolve
  `Atlante FC`/`Atlante` and `FC Juarez`/`Juarez`, but not `Tigres UANL`/`Tigres`,
  `Guadalajara Chivas`/`Guadalajara`, `Atletico San Luis`/`San Luis`, or `Club
  Tijuana`/`Tijuana de Caliente` — about half the clubs in scope. These are alternate
  names, not respellings, which ADR 0039 already marks as out of canonical comparison's
  reach. Name matching is therefore not a fallback and not a proposal mechanism; the
  registry is the only bridge.

- **Which Winner Market belongs to which Fixture is settled by the local match date.**
  Kalshi's ticker is dated in the match's own locale (`KXLIGAMXGAME-26AUG15MONJUA`), so a
  01:00 UTC kickoff is the *previous* day's market — verified on Liga MX, where fixture
  1550922 is `Aug 16 01:00` UTC and `Aug 15` in `America/Mexico_City`, matching Kalshi
  6/6 across that round. The date compared is therefore the kickoff in the
  **Publication's `display_timezone`** — the same date that already fixes a **Match
  Post**'s slug (ADR 0029). Kalshi's `occurrence_datetime` is *not* the kickoff: it
  equals `expected_expiration_time`, some hours later, and is usable only as a sanity
  band.

- **A half-resolved market is refused, never half-attached.** The registry's correctness
  rests on Kalshi's UUIDs staying stable, which is evidenced rather than guaranteed. So a
  Winner Market whose two clubs do not *both* resolve through the registry is rejected
  outright — the resolver never infers the second team from the first, or from the ticker,
  or from the date alone. This is ADR 0030's refuse-rather-than-guess stance carried over
  intact; only the unit it applies to has changed.

- **The `/previews` surface proposes registry entries and writes them uncommitted; a human
  commits.** The two existing precedents point opposite ways: the Competition registry is
  hand-edited and ADR 0037 kept the Competitions board deliberately out of it, while the
  Venue registry is machine-appended *and* auto-committed by `nightly.sh`. This sits
  between them, and the deciding property is what a wrong entry costs. A venue id is
  arbitrary and cannot be wrong; mapping the wrong club silently attributes a market to the
  wrong team and produces a **Match Preview** that is confidently, invisibly incorrect.
  Asking a human to hand-transcribe a UUID they did not choose is toil, not review — the
  judgement is "is this Kalshi club our team 2287", and `git diff` puts exactly that
  question in front of them. So the surface writes the file and stops.

**Consequences:**

- **The manual gap the operator sees is a list of unmapped Teams, not a list of unmatched
  games** — a list that shrinks to zero and stays there, growing only when a franchise is
  added or a club is promoted into a covered Competition.

- **Kalshi's integer-cent price fields are dead and must not be used.** `yes_bid`,
  `yes_ask`, `last_price`, `volume` and `open_interest` now return null on every market,
  active and settled alike; the live values are `yes_bid_dollars`, `yes_ask_dollars`,
  `last_price_dollars`, `volume_fp`, `open_interest_fp`. `predicitons/fastapi_server/`
  reads the dead fields throughout — its `minutes_to_df`, `build_ranking` and candlestick
  path all key off `yes_bid`/`price.close` — so it would return empty results silently
  rather than failing. Its `KalshiClient` shape is worth keeping; its price handling is not.

- **`liquidity_dollars` is not a usable signal** — it reads `0.0000` on markets with tens
  of thousands of contracts traded. Depth is read from `volume_fp`, which is why a **Quote**
  carries volume (ADR 0040).

- **The whole market half costs three unauthenticated GETs**, one per series
  (`/markets?series_ticker=…&status=open`), each returning ~90 priced markets unpaginated.
  No API key, no signing, no quota to budget. `.env`'s `kalshi_api` is an API **Key ID**
  with no accompanying RSA private key, and nothing here needs one.
