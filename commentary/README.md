# ESPN commentary → typed JSON

Exploratory, standalone source. Fetches a match's commentary from ESPN and
attaches ESPN's own typed events (goal / card / sub / VAR, with players, teams
and shot coordinates) to the right commentary lines.

It writes **nothing** to `football.db` and is not wired into `orchestrate`,
`refresh`, or cron. The only on-disk side effect is the raw cache under
`data/raw/espn-summary/<gameId>.json`. It is not a competitor to the
API-Football path (ADR 0002); it is a look at whether ESPN's per-line narration
is worth having at all.

## Use

```bash
python -m commentary 760514                 # JSON to stdout
python -m commentary 760514 -o out.json     # to a file, join report to stderr
python -m commentary 760514 --refresh       # bypass the raw cache
```

The gameId is the number in the ESPN URL:
`espn.com/soccer/commentary/_/gameId/760514`.

## The process we followed

The point of this file is the *method*, so the next source can be onboarded the
same way. Each step below is the one that changed the design.

**1. The page is not the source.** `espn.com/soccer/commentary/...` answers
non-browser clients with HTTP 202 and an empty body — it is a client-side render
behind a bot challenge. Rather than fight that, we used the API the page itself
calls:

```
https://site.api.espn.com/apis/site/v2/sports/soccer/all/summary?event=<gameId>
```

It returns 200 with plain JSON and needs no key. `all` stands in for the league
slug, so a gameId is the only identifier required. The payload also carries
`boxscore`, `rosters`, `odds`, `standings` and `leaders` — the same fetch covers
more than commentary if we ever want it.

**2. Cache first, in the style of `football/client.py`.** The raw payload lands
in `data/raw/espn-summary/` and is never refetched without `--refresh`, so
iterating on the join costs zero requests. This mattered: the join was rewritten
several times against one cached fixture.

**3. The two arrays don't share an id.** ESPN describes each match twice:

| array | rows (in 760514) | has |
|---|---|---|
| `commentary` | 115 | `sequence`, `time`, `text` — no types, no players |
| `keyEvents` | 41 | full typed event — but no `sequence` |

So `keyEvents` never says *which* line it belongs to. The join has to be on
content, and choosing that key was the whole problem.

**4. The first join key was wrong, and quietly so.** The obvious key is the
displayed minute (`"24'"`). It is wrong: several key events routinely share a
minute — this match has two different substitutions at each of 72', 78' and 84'
— so a minute join assigns *every* event in a minute to *every* line in that
minute. It doesn't error. It just silently mislabels rows. A first pass using it
"matched" 43 of 115 lines and looked fine.

**5. What we verified instead.** Two properties of the feed, checked against the
real payload before relying on either:

- every text-bearing keyEvent's `text` appears **verbatim** in some commentary
  line (27/27) — ESPN generates both from one string;
- `(text, clock)` is **unique** across commentary (0 collisions), while `text`
  alone is not — generic lines ("Offside, France.") repeat.

That makes `(text, clock)` an exact, collision-free primary key.

**6. Where the exact key breaks: period boundaries.** 25 of 27 matched on
`(text, clock)`. The 2 failures were Halftime and End Regular Time, because
`keyEvents` **clamps** `clock.value` to regulation (2700.0 = 45:00, 5400.0 =
90:00) while `commentary` counts real elapsed seconds through stoppage (3075.0,
5820.0). Both render the same `displayValue` (`45'+7'`), so the disagreement is
invisible unless you compare the numbers. A clock-equality join drops exactly
the halftime and fulltime markers and reports success.

Hence one fallback: **unique text match**, taken only when the text hits exactly
one line — which is what makes it safe. Result: 27/27, 0 unmatched.

**7. Refuse to guess.** The join raises on an ambiguous match (>1 candidate) and
on a commentary line being claimed by two events, rather than picking one. A
wrong label is worse than a crash here, because nothing downstream could detect
it.

**8. Read the output, not the counters.** "27/27 joined" was true while the docs
were still wrong. Printing the joined rows showed `Start Delay` in both the
joined and the structural buckets, which looked like double-counting. It wasn't:
ESPN records each delay **twice, once per team**, with distinct ids — one copy
has the text and the team responsible, the twin names the opposing team and has
no text. The text-bearing copy joins; the twin is skipped. All 41 ids are
distinct, so there are no true duplicates. Only inspecting rows caught this.

## Step two: standardized categories (`commentary.classify`)

```bash
python -m commentary.classify 760514 --audit          # accuracy check, classifies nothing else
python -m commentary.classify 760514 -o synthesis.json
```

Produces one row per line: `minute`, `team`, `category` (+ `sequence`, `source`,
`text`). Taxonomy and the ESPN-type mapping live in `taxonomy.py`; all 21
categories were **observed in the feed**, not invented.

**The feed is machine-generated, and that changes the design.** The commentary is
templated (`source: SA.ENVOY`) — "Foul by X (Team)." appears 22 times, attempts
are always "Attempt [missed|blocked|saved]", free kicks are always "X (Team) wins
a free kick in the {attacking,defensive} half". Three consequences:

1. **Haiku is the right tier.** One-sentence templated text is exactly the
   workload the cheapest model is for. Full match: **4 requests, ~7.8k in /
   1.7k out tokens** — fractions of a cent.
2. **A regex would also score well.** The LLM buys robustness against template
   drift and unseen phrasings, not raw accuracy on this match. If cost or
   determinism ever matters more, a rules pass with LLM fallback for
   non-matching lines is the obvious next step — the taxonomy wouldn't change.
3. **ESPN's types are ground truth, so the model never sees those 27 lines.**
   Only the ~88 untyped lines are sent. Cheaper *and* strictly more accurate
   than asking a model to re-derive a label the feed already states.

**The audit is the important part.** ESPN's 27 typed keyEvents are real labels.
`--audit` re-classifies exactly those lines **blind** (the model is not told the
answer) and reports agreement — a real accuracy number instead of a vibe. On
760514: **27/27 (100%)**. Run it on any new match or after any prompt change;
it costs 2 requests.

**Correctness guards, all of which refuse rather than guess:**

- `category`, `team` **and `sequence`** are JSON-schema **enums** (`team` is
  enumerated to the two teams in *this* match plus `"none"`; `sequence` to the
  line numbers of *this* chunk), so a hallucinated team, category or line number
  is undecodable, not merely unlikely.
- **A chunk is validated before it writes anything**, and rejected if it returns
  a foreign, duplicated or missing `sequence`. This one is not theoretical:
  match 760507 returned 20 sequences belonging to an *earlier* chunk and
  overwrote its labels. Because `sequence` is the join key, a wrong one doesn't
  lose a label — it moves it onto another line. The old code only checked for
  leftovers, so a shifted-but-complete answer would have passed with every label
  silently wrong.
- **A rejected chunk is retried** (3 attempts) rather than aborting the run.
  Malformed answers are usually a bad roll — the same chunk gave 4/25 lines on
  one attempt and 25/25 on the next — and chunks are paid for one at a time, so
  failing outright throws away the spend on every chunk before it.
- If a chunk hits `max_tokens`, the answer is **rejected** — no silently
  truncated batch.
- Every row carries `source`: `espn_keyevent` or `llm`. You can always tell which
  labels are asserted by the feed and which were inferred.

**Attribution rule worth knowing:** `team` is who the event is *attributed to*,
not who is named first. "Corner, France. Conceded by Pau Cubarsí." is France's
corner; "Foul by Adrien Rabiot (France)." is France's foul. This is stated in
the prompt and verified in the output (0 corners or offsides attributed to the
conceding team).

Signals that the output is coherent, beyond the audit: 22 fouls ↔ 22
free-kicks-won (each foul yields one), and 1 `goal` + 1 `penalty_scored` = the
0–2 final score.

## Output shape

```jsonc
{
  "game_id": "760514", "league": "FIFA World Cup", "date": "...", "venue": "...",
  "home": {"team": "France", "score": "0"}, "away": {"team": "Spain", "score": "2"},

  "commentary": [                       // chronological; ESPN serves newest-first
    {"sequence": 13, "minute": "22'", "clock_seconds": 1312.0,
     "text": "Goal! France 0, Spain 1. ...",
     "key_event": {                     // null on the ~76% of lines with no typed event
       "type": "Penalty - Scored", "team": "Spain", "scoring_play": true,
       "players": [{"id": "229018", "name": "Mikel Oyarzabal"}],
       "field_position": {"x": 88.5, "y": 50.0, "goal_y": 45.8}}}
  ],

  "key_events": [ /* all 41, standalone — usable as its own table */ ],

  "join": {                             // always check this before trusting a run
    "matched": 27, "key_events_with_text": 27,
    "by_strategy": {"exact(text,clock)": 25, "fallback(unique-text)": 2},
    "structural_unjoined": [...], "unmatched": []
  }
}
```

`join.unmatched` being non-empty on a new match means the feed did something
this join hasn't seen — read those rows before trusting the output.

## Step three: the sweep (`commentary.sweep`)

```bash
python -m commentary.sweep 760514 401873984 401873998 -o sweep.json
```

Audits several matches at once and reports, per match: join rate, blind accuracy
vs ESPN ground truth, and **any ESPN types the taxonomy can't map**. Find
candidates via the scoreboard (`.../soccer/all/scoreboard?dates=YYYYMMDD`).

**Result across 10 matches (5 competitions): 147/147 (100%) blind accuracy.**
Exercised against real labels: `substitution` 91, `delay` 46, `yellow_card` 44,
`period_marker` 24, `goal` 23, `penalty_scored` 4, **`red_card` 4**,
**`own_goal` 2**.

The sweep found four things one match never could. Each is why the sweep exists:

1. **Unmapped goal variants — silent loss of ground truth.** ESPN names goals by
   method: `Goal - Header`, `Goal - Free-kick`, `Goal - Volley`. The map had only
   `"Goal"`, so those fell through to the model *and* dropped out of the audit's
   denominator — accuracy would have kept reading 100% while silently covering
   fewer goals. Fixed with a `Goal - *` prefix rule in `espn_category()`, plus
   `unknown_espn_types()` reported per match so the next gap is loud.

2. **`Own Goal` needed its own category.** It isn't a goal variant: attribution is
   *inverted* (an own goal by France counts for Spain). Folding it into `goal`
   would have credited the wrong team.

3. **The clock clamp is routine, and text-only fallback was unsafe.** The clamp
   is not limited to halftime/fulltime — **every** stoppage-time event is clamped
   to the period boundary. Across these matches `fallback(text,minute)` fires
   **30 times** against 208 exact matches. The old unique-text fallback broke on
   the first match that needed it: "Delay over. They are ready to continue."
   repeats at 52', 80' and 90'+8' in 401873984, so the guard refused to guess and
   the match failed hard. The fallback is now `(text, displayValue)` — the minute
   *string*, which both sides agree on even when the numeric clocks don't.

4. **A second feed shape exists.** See Known limits.

**Unmatched keyEvents are often correct, not a bug.** In lower-coverage leagues
the commentary doesn't narrate every event: Larne 401877822 has 30 lines but 22
text-bearing keyEvents, and its substitutions arrive as `"Kevin O'Hara (Larne)
Substitution at 58'"` — a shortText style that never appears in that match's
narrative commentary. Those 10 are genuinely absent from `commentary` and remain
available in `key_events`. Judge a run by `join.unmatched` *plus* whether the
missing rows exist in the commentary array at all.

## Known limits

- **A second feed shape is unsupported.** Coverage-light leagues serve a
  different schema: no `sequence` field, already chronological, and a typed
  `play` object embedded on each commentary row (so no join is needed at all) —
  with shortText-style text like `"Bendik Berntsen (Sandefjord) Goal at 4'"`
  rather than narrative. `join_commentary` detects it and raises a specific
  error instead of a `KeyError`.

  **This limit is now measured, and both halves of the old claim were wrong.**
  It is not 3-in-10 but **25 of 49** cached matches — half the feed. And it is
  *not* "a real piece of work": that judgement was about the join, which this
  shape does not need at all. Every row carries an embedded typed `play` object,
  and all **362/362** of them map through the existing `espn_category()` with zero
  unmapped — so it needs no join, no model and no taxonomy change. ADR 0026 decides
  to support it; every Category from this shape is *asserted*, never inferred.

- **`penalty_missed` is unreachable, not merely untested.** All three matches
  found carrying `Penalty - Missed` / `Penalty - Saved` (401874228, 401843376,
  401859614) use the coverage-light shape above. The category and its mapping
  exist but have never executed — supporting that shape (ADR 0026) is what makes
  them reachable, via 5 asserted rows. `attempt_woodwork` remains unreached.

- **Extra time and shootouts remain untested.** None appeared in 49 scanned
  matches across a 5-day window. `keyEvents` carries a `shootout` flag we pass
  through but have never seen set, and no period > 2 has been observed.

- **Accuracy is measured only where ESPN provides labels.** The audit covers the
  typed subset (goal / card / sub / delay / period). The ~88 untyped lines per
  match — `foul`, `corner`, `offside`, `attempt_*`, `free_kick_won` — have **no
  ground truth** and are unverified by the audit. Their support comes from the
  templated feed and internal consistency (22 fouls ↔ 22 free-kicks-won; goals
  reconciling to the final score), not measurement.
- **In-play behaviour untested.** Everything here is a finished match. Whether
  `sequence` is stable while a match is live is unknown, and it matters if this
  is ever pointed at the Live Poll path (ADR 0020).
- **No id mapping.** ESPN team/athlete ids are ESPN's own; nothing links them to
  API-Football ids. Any join to `football.db` needs that bridge first.
- **It has now graduated.** This page describes the *spike* — a stdout/JSON tool
  that writes nothing but its raw cache. The decision to make it a store lives in
  **ADR 0026**, and its vocabulary (**Narrated Match**, **Commentary Line**,
  **Category**, **Narration Coverage**) is in `CONTEXT.md`. Read those first: the
  store keys on ESPN's game id rather than a Fixture, ingests only matches ESPN
  reports Final, supports both feed shapes, and — unlike every other store here —
  is **not** rebuildable from the raw cache.
