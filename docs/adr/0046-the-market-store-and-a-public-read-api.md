# The Market Store, and the first process we let the internet reach

---
Status: accepted — supersedes the **"a Market Track is fetched and never stored"** decision
of ADR 0043 and rewrites the **Market Track** entry in CONTEXT.md. Adds **Market
Observation**, **Market Candle**, **In-Play Window** and **Market Store** to the glossary.
Leaves ADR 0040's kickoff freeze standing and says why it does not reach here. Bounded by
ADR 0027/0028 (the Published Store and its wholesale swap), ADR 0041 (Winner Markets attach
by Team) and ADR 0033 (test what fails silently).
---

The blog's **La Previa** section shows a **Match Preview** per upcoming Fixture with each
**Exchange**'s three percentages on it. The section is to become a dashboard: the story of
a game as the prediction markets told it, before kickoff *and through the match*.

Nothing of that story is kept today. The hourly `preview --quotes` pass overwrites
`match_preview`'s market half in place, so every previous hour is destroyed and the only
survivor is the last pre-kickoff snapshot. The graph on the `/previews` board is drawn by
re-fetching both Exchanges at read time, which ADR 0043 settled deliberately: *"no store,
no cadence, no record, no backfill and nothing to freeze."*

That decision was right for what it served and is wrong for what is now being asked. This
ADR reverses it, and the reversal has to be argued on the correct ground, because the
obvious reason is false.

**The Exchanges have not stopped serving history, and a stored Market Probability would
still be redundant.** Verified again while writing this, on an open MLS market:
`period_interval=1` returns 54 one-minute Kalshi candlesticks over three days, and
`fidelity=1` returns 4,451 one-minute Polymarket points where `fidelity=60` returns 743.
Both keep serving after settlement, exactly as ADR 0043 recorded. If the dashboard were
only a probability line, ADR 0043 would still be right and this ADR would not exist.

The dashboard is not only a probability line. What it needs, and what neither Exchange
serves retrospectively, is **the book**.

Four decisions were not obvious.

**Decisions:**

- **Store what expires, derive what does not — and the line between them runs through the
  middle of each Exchange, not between them.** The split is a measured fact, not a policy:

  | | Kalshi | Polymarket |
  |---|---|---|
  | Retrospective, at 1-minute | `yes_bid` OHLC, `yes_ask` OHLC, `price` OHLC, `volume_fp`, `open_interest_fp` | the **mid**, and nothing else |
  | Live-only, gone if not read | top-of-book **sizes** (`yes_bid_size_fp`/`yes_ask_size_fp`) | `bestBid`, `bestAsk`, `spread`, `liquidity`, and the **path** `volume` took |

  So the Market Store holds two kinds of row with two different guarantees. A **Market
  Candle** is the Exchange's own published history and is re-fetchable forever, which makes
  the backfill idempotent and every gap repairable. A **Market Observation** is what our
  poller read at an instant it stamped itself, and is the only copy there will ever be.
  Conflating them would put a repairable fact and an unrepairable one behind the same
  guarantee, and the weaker one would win silently.

  A **Market Probability** is still not stored. It is the mid normalised across one
  Exchange's three legs, it is cheap to compute from rows we hold, and a stored copy is one
  more thing that can quietly disagree with the numbers it was derived from. The API
  derives it per request. This is the half of ADR 0043 that survives intact.

- **ADR 0040's kickoff freeze stays, and does not extend to this store.** The freeze exists
  because a **Quote** does not stop at kickoff — it converges on the result and settles at
  1 and 0 — so re-running a settled Fixture would overwrite a *forecast* with an *outcome*,
  both spelled as three percentages, with nothing on the card to say which was stored. That
  reasoning is about a card showing **one** number. A series has a time axis: the
  convergence is *drawn*, at the timestamps it happened, beside the kickoff marker. The
  distinction the freeze protects is the thing the dashboard renders rather than the thing
  it loses. `match_preview` keeps freezing; the Market Store keeps collecting through
  settlement, and the **In-Play Window** runs to kickoff + 150 minutes precisely so the
  convergence is inside the picture.

- **Enrolment is the deadline, not collection.** ADR 0043's sharpest observation is that
  the history outlives the listing but *the pointer to it does not*: a few hours after
  kickoff Kalshi marks the market `settled` and Polymarket marks the event `closed`, and
  neither sweep returns it again. So the store is built around one table whose only job is
  to capture `series_ticker` + per-leg `market_ticker`, and `event_slug` + per-leg
  `token_id`, **while the market is still listed**. Everything else can be late. A Fixture
  that is never enrolled cannot be backfilled at any price, which is why `markets.watch`
  runs hourly against every covered Fixture rather than only the ones with a Publication,
  and why it is the first job to be switched on.

  This also makes the collector cheap in the way that matters. Both Exchanges enumerate by
  league — `/markets?series_ticker=` and `/events?series_slug=` each return a whole
  Competition in one sweep — so **cost is per Exchange-series, not per Fixture**. Ten
  matches kicking off at once cost the same six unauthenticated GETs as one, which is what
  makes a once-a-minute poll defensible.

- **`fixture_id` is a bridge, never a foreign key — and here that is a load-bearing
  structural fact, not a modelling preference.** `narrated_match.fixture_id` already carries
  no `REFERENCES` because it may dangle. These tables have a second, harder reason: the
  wholesale publish runs `DROP TABLE IF EXISTS public.fixture CASCADE` inside its blue-green
  swap (`football/publish/pg.py`), and `CASCADE` silently drops every foreign-key constraint
  pointing at the table being dropped. A constraint added here would disappear on the next
  reset with no error, on a store that is not rebuildable — leaving the schema quietly
  weaker than it reads. So the Market Store references the Published Store by integer and
  by nothing else, and a test asserts the DDL contains no `REFERENCES` and that none of its
  table names appear in `FOOTBALL_TABLES` or `COMMENTARY_TABLES`.

**Consequences:**

- **This is the second store in the project that cannot be rebuilt, and the first inside
  Postgres.** `commentary.db` broke ADR 0002's rebuild-from-cache invariant because a
  language model's labels cost money and do not reproduce; this breaks it because an order
  book at 20:47 on a Saturday is not served by anyone afterwards. Postgres has been
  "derived, never authored" since ADR 0027, and remains so for its ten published tables —
  but `football_prod` now holds four tables that a `publish` cannot restore. They are
  outside every publish path by name, and the backup story for the database changes from
  "re-run the pipeline" to "the observations are irreplaceable".

- **Indexes are declared, which ADR 0027 refused to do.** That refusal was explicitly
  "deliberately not guessed at here… once real query patterns exist". A time series has
  exactly one access pattern and it is known before the first row: everything is read as
  `(fixture_id, exchange)` ordered by time. The guess is no longer a guess.

- **The API is a separate process from `surfaces/`, and that separation is the security
  model.** Every existing surface binds to `127.0.0.1` with a docstring explaining that it
  must never be exposed — they spawn subprocesses, spend API-Football quota and Anthropic
  tokens, and write the Editorial Store. None of that can be fixed with a route guard,
  because exposability is a property of what a process *can* do rather than of what its
  handlers happen to call. So the public API is its own app with no write path, no
  subprocess, and no credential a reader could reach, and it carries the first
  `CORSMiddleware` in the repo — `GET` only, one origin. Mounting it into the surfaces app
  was rejected outright: the mount would put a public listener in the same process as the
  Console.

- **Kalshi's payload was being read at about half its width.** `quote_of()` takes
  `yes_bid_dollars`, `yes_ask_dollars`, `last_price_dollars`, `volume_fp` and
  `open_interest_fp`, and drops `no_bid_dollars`, `no_ask_dollars`, `yes_bid_size_fp`,
  `yes_ask_size_fp` and `volume_24h_fp`. The sizes matter most: they are the only depth
  figure either Exchange gives at the top of book, and they appear in **no** candlestick, so
  they are live-only. A **Market Observation** takes all of it. `liquidity_dollars` stays
  unread — it is still `0.0000`, as ADR 0041 found. Polymarket's `liquidity` is *not* the
  same field and is real (`1485.01` on the market checked), so it is stored, per Exchange,
  never compared across them.

- **Volume remains incomparable across Exchanges, now in the schema rather than only in the
  rendering.** `volume_unit` sits on every observation row instead of being inferred from
  `exchange`, so a query that forgets to group by Exchange still cannot add contracts to
  dollars. `open_interest` is null on every Polymarket row because Gamma publishes none —
  null meaning *not published*, which is a different claim from zero.

- **The full Polymarket order book is reachable and is deliberately not taken.** CLOB
  `/book?token_id=` returns the whole ladder, but it is one request per leg per Fixture,
  which would trade the per-series cost property for depth nobody has asked to chart. Gamma's
  sweep already carries `bestBid`, `bestAsk` and `spread`. If a depth ladder is ever wanted,
  it is a new decision with a new cost, not an extension of this one.

- **A **Market Probability** cannot be candlesticked, so the API serves OHLC per leg and
  un-normalised.** The store keeps Kalshi's full `yes_bid` / `yes_ask` / `price` OHLC per
  period, and `/api/fixtures/{id}/ohlc` serves it verbatim — but only ever for **one
  contract at a time**. Normalising open and close across the three legs is sound, since
  each is a single instant; normalising **high and low is not**, because the three legs'
  highs need not occur at the same moment inside the bucket, so the result is a high
  nobody quoted and four values that no longer agree with one another. The same argument
  rules out a *mid* candle even within one leg: the mid's open and close are exact, its
  high and low are not, and `(yes_bid_high + yes_ask_high) / 2` is an upper bound rather
  than a price. It is the gap rule again — a series that cannot be built honestly is not
  built. The endpoint is also Kalshi-only by construction, and says so with
  `ohlc_state: "not_published"` rather than an empty array, because Polymarket publishes
  one mid per point and that will not change.

- **Event markers on the chart carry the Published Store's nightly vintage.** The overlay
  joins `public.event` at read time and adds no storage, but the Published Store's scores
  and events only advance at 04:00 and only for **Final** Fixtures. So a chart watched live
  shows the line move with no markers, and the markers appear the next morning. The response
  states its own vintage rather than leaving the reader to infer it, for the same reason
  ADR 0044 refused a permanently-null `live_minute`: an absent marker must not be readable
  as "nothing happened".
