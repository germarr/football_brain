# The Market API read contract

What `lacancha.gerardomarr.com` can rely on from the prediction-market API, written for
the side that cannot see this repo. Companion to `docs/pocketbase-read-contract.md`, which
covers everything else the blog reads.

Producer: `markets/api.py` here. Store: four Postgres tables in `football_prod`
(ADR 0046). Consumer: the **La Previa** dashboard.

This API is **additive**. It does not replace anything — the cards La Previa shows today
still come from PocketBase, and a Fixture with no market data is still a Fixture. If this
service is down, the section should render exactly as it does now, minus the chart.

---

## Base

```
GET https://<host>/api/…
```

Read-only. Every route is `GET`; there is no write path and no auth. CORS allows exactly
`https://lacancha.gerardomarr.com` (plus `localhost:4321` and `localhost:3000` for
development) and only the `GET` method.

Interactive schema: `/api/docs`.

---

## The four routes

| Route | Use it for |
|---|---|
| `GET /api/health` | `{"ok": true}` |
| `GET /api/fixtures?from=&to=&competition=` | which Fixtures have market data at all |
| `GET /api/fixtures/{id}/markets` | **the dashboard** — everything for one Fixture |
| `GET /api/fixtures/{id}/track?exchange=&resolution=` | just the probability lines |
| `GET /api/fixtures/{id}/ohlc?exchange=&side=&series=&resolution=` | **candlestick data** — one leg, un-normalised |
| `GET /api/fixtures/{id}/book?exchange=` | just the observed book |

`from`/`to` are dates (`YYYY-MM-DD`), defaulting to a −3d/+14d window — the same window
the `fixture_row` collection uses, so the two feeds cover the same games.

A Fixture that is in neither store returns **404** with `{"error": "…"}`. A Fixture that
exists but has no market returns **200** with typed absence (below). Those are different
answers and should render differently.

---

## Time

**Every timestamp is unix seconds, UTC**, named `t`, `kickoff`, `listed_from`, `t_from`,
`t_to`. The only ISO string is `kickoff_iso`, and it is there for display, not arithmetic.

```js
new Date(point.t * 1000)
```

This differs from the PocketBase contract, deliberately: that one carries space-separated
date strings because PocketBase compares dates as strings. Nothing here is a string
comparison, so nothing here is a string.

---

## `GET /api/fixtures/{id}/markets`

```jsonc
{
  "fixture_id": 1490136,
  "kickoff": 1786221000,
  "kickoff_iso": "2026-08-08T20:30:00+00:00",
  "competition": { "id": 253, "name": "Major League Soccer" },
  "home": { "team_id": 1602, "name": "New England Revolution" },
  "away": { "team_id": 1597, "name": "Houston Dynamo" },
  "status": "FT",
  "score": { "home": 0, "away": 2 },

  "axis": {
    "t_from": 1784073600,        // earliest point on EITHER exchange
    "t_to":   1786230000,        // latest point, or kickoff + 150m
    "kickoff": 1786221000,
    "in_play_from": 1786220100,  // kickoff − 15m
    "in_play_to":   1786230000   // kickoff + 150m
  },

  "exchanges": {
    "kalshi": {
      "state": "tracked",
      "backfill_state": "complete",
      "volume_unit": "contracts",
      "resolution": "auto",
      "listed_from": 1785783600,
      "enrolled_at": 1786405000,
      "last_seen_at": 1786405000,
      "gaps": 5,
      "candles": 513,
      "identifiers": { "series_ticker": "KXMLSGAME", "event_ticker": "…",
                       "event_slug": null, "league": null },

      "probability": [
        { "t": 1785783600, "home": 0.4712, "draw": 0.2611, "away": 0.2677 }
      ],

      "book": [
        { "t": 1786405000,
          "home": { "yes_bid": 0.18, "yes_ask": 0.68, "no_bid": 0.32, "no_ask": 0.82,
                    "last": null, "mid": 0.43, "spread": 0.50,
                    "yes_bid_size": 30, "yes_ask_size": 100,
                    "volume": 139, "volume_24h": 12, "open_interest": 162,
                    "liquidity": null },
          "draw": { … }, "away": { … } }
      ],

      "depth": [ { "t": …, "period_s": 3600, "volume": 139, "open_interest": 162 } ]
    },
    "polymarket": { … }
  },

  "events": [
    { "t": 1786224780, "t_estimated": true, "minute": 63, "extra": null,
      "type": "Goal", "detail": "Normal Goal", "team_id": 1597, "player": "A. Resch" }
  ],
  "events_vintage": "nightly"
}
```

### `resolution`

`auto` (default), `hour`, or `minute`.

`auto` is hourly across the run-up and **per-minute across the In-Play Window**, stitched
at `axis.in_play_from`. One continuous line that simply gets denser at kickoff — plot `t`
and it works. Use `hour` for a sparkline; `minute` returns only the in-play span.

---

## Six rules the API guarantees, and one thing it will never do

**1 — The two Exchanges are never merged.** There is no consensus number, no blend, no
primary Exchange. Two panels, side by side. They are expected to disagree and the
disagreement is the point.

**2 — Volume is not comparable across Exchanges.** Kalshi counts **contracts** and
Polymarket counts **dollars traded**; `139` and `41,814` on one Fixture are two different
measurements with no conversion between them. `volume_unit` is on every Exchange block —
label it, and **never put the two on a shared axis**.

**3 — A zero is a claim; a null is silence.** `volume: 0` means nobody traded — render
*no depth*, not a blank. `null` means the Exchange does not publish that field at all:
Polymarket publishes no `open_interest` and no `no_bid`/`no_ask`, Kalshi publishes no
`liquidity` worth having. A blank cell reads as "no information" when the fact is often
the opposite claim.

**4 — A gap is a break in the line, never an interpolation.** A bucket missing any of the
three legs is omitted from `probability` and counted in `gaps`. Do not connect across it
and do not carry the previous value forward — normalising over two of three legs would
draw a distribution nobody quoted. Use a line renderer that breaks on missing `t`.

**5 — Both panels share one axis, and the server computed it.** The two Tracks are
routinely 190× different in span: Polymarket lists a Fixture around four weeks out, Kalshi
two to five days. Auto-scaling each panel independently renders a month-long drift and a
four-hour wobble at the same width, which is a lie. Draw both across `axis.t_from` →
`axis.t_to`, and mark each Exchange's span before its own `listed_from` as **not listed**
rather than leaving it blank — when an Exchange began having an opinion is part of what the
pair shows.

**6 — Absence is typed.** `state` is one of:

| `state` | Means | Will it resolve itself? |
|---|---|---|
| `tracked` | there is data | — |
| `not_listed` | this Exchange does not list this Fixture yet | yes, when it opens the market |
| `not_covered` | this Exchange does not cover this Competition at all | no, and nobody can change it |
| `unmapped` | a club is missing from the Exchange's team registry | only a human here can fix it |
| `no_data` | enrolled, nothing harvested yet | yes, on the next backfill |

Render `not_covered` as "not offered here", `not_listed` as "not open yet", and never as
each other.

**And the thing it will never do:** return a probability the Exchange published. A
**Market Probability** is *ours* — the mid of bid and ask, normalised across one
Exchange's three legs to sum to 1. Neither Exchange publishes it. The raw mids sum to
roughly 1.005–1.025 on Kalshi and 0.995–1.065 on Polymarket, which is why they are
normalised; the raw `book` is beside them so the sum stays auditable.

---

## `GET /api/fixtures/{id}/ohlc` — candlestick data

**A different chart from `/track`, not a richer one.** `/track` gives the three-way
**Market Probability**: ours, normalised across the legs so they sum to 1. This gives
**one contract's raw price**, exactly as Kalshi published it, un-normalised. The response
says `"normalised": false` so the two can never be confused. Do not put them on one axis.

```jsonc
{
  "fixture_id": 1490136,
  "exchange": "kalshi",
  "state": "tracked",
  "ohlc_state": "published",
  "normalised": false,
  "series": ["yes_bid", "yes_ask", "price"],
  "resolution": "minute",
  "volume_unit": "contracts",
  "kickoff": 1786221000, "in_play_from": 1786220100, "in_play_to": 1786230000,
  "sides": {
    "away": {
      "team_id": 1597,
      "market_ticker": "KXMLSGAME-26AUG08NEHOU-HOU",
      "candles": [
        { "t": 1786224720, "period_s": 60, "volume": 4413, "open_interest": 41226,
          "yes_bid": { "open": 0.58, "high": 0.60, "low": 0.58, "close": 0.59 },
          "yes_ask": { "open": 0.60, "high": 0.61, "low": 0.59, "close": 0.60 },
          "price":   { "open": 0.59, "high": 0.60, "low": 0.59, "close": 0.60 } }
      ]
    }
  }
}
```

| param | values | default |
|---|---|---|
| `exchange` | `kalshi`, `polymarket` | `kalshi` |
| `side` | `home`, `draw`, `away` | all three |
| `series` | `book`, `trades`, `both` | `book` |
| `resolution` | `auto`, `hour`, `minute` | `auto` |

- **`book`** → `yes_bid` and `yes_ask` OHLC. Quoted throughout, so **every** period has
  them. The pair is also the spread over time.
- **`trades`** → `price` OHLC, the traded price. **`null` on any period with no trade** —
  about two in five across a whole market's life, though in-play it is closer to one in
  ten. Skip those periods; do not draw them at zero.
- **`both`** → all three blocks on each candle.

### Three things this endpoint will not do

**It is Kalshi-only, structurally.** Polymarket's `/prices-history` publishes one mid per
point — no open, no high, no low, no volume. A Polymarket request returns
`ohlc_state: "not_published"` with a note and empty series. That is the Exchange having
nothing to give, not a hole in our collection, and it will never fill in. Render the
candlestick panel for Kalshi and Polymarket's line from `/track` beside it.

**There is no mid candle, and there never will be.** The mid's *open* and *close* are
exact — bid and ask are both quoted at the period's edges, so their mean is the real mid
there. Its **high and low are not recoverable**: the bid's high and the ask's high need
not happen at the same moment inside the period, so `(yes_bid_high + yes_ask_high) / 2` is
an upper bound on the mid rather than a price anyone saw. A mid *line* is exact and is
what `/track` serves; a mid *candle* would be invented.

**You cannot candlestick a probability.** Same reason, one level up. Open and close are
single instants and normalise correctly; high and low do not, because the three legs'
highs do not co-occur. Normalising them yields a high nobody quoted and four values that
no longer agree with each other. If you want a candlestick, it is of one contract.

## Traps

**`events` are nightly, and during a match there are none.** They come from the Published
Store, which advances at 04:00 and only for Fixtures that are **Final**. A chart watched
live shows the line move with no markers; they appear the next morning. `events_vintage`
says so. Do not render "no events" as "nothing happened" — for a match in progress, render
nothing at all.

**`t_estimated: true` on every event.** A match minute is not a wall clock — 45+3 and 48
are different moments — and the store records only the minute. The marker is placed at
`kickoff + minute × 60`, close enough to sit beside the step it explains and not close
enough to align to the second. Do not draw a hairline from marker to curve.

**A settled market converges to 1 and 0, and that is the data, not a bug.** The last
points of a played Fixture's line read `0.005 / 0.005 / 0.99`. That is the market settling
on the result. It is why the pre-kickoff **Match Preview** card freezes and this chart does
not: on a card one number would silently become an outcome, whereas here the convergence is
drawn at the timestamps it happened.

**A Kalshi market often opens at exactly 1/3 each.** The first points of a Kalshi line are
frequently `0.3333 / 0.3333 / 0.3333` — an opened book with no trading. It is real, and it
is not a placeholder.

**Only four Competitions exist here.** La Liga (140), MLS (253), Liga MX (262) and
Leagues Cup (772) are what either Exchange lists. Anything else is `not_covered`,
permanently.

**Payload size.** A played Fixture's dashboard is a few hundred KB — up to ~840 probability
points per Exchange plus the book. Use `/track` when only the line is needed, and cache on
`fixture_id`: a Fixture whose `status` is Final and whose `backfill_state` is `complete`
will never change again.
