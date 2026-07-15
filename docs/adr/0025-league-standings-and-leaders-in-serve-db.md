# Precomputed league standings + leaders in serve.db; an inline league section in the Viewer

*Relates to ADR 0023 (Console/Viewer split + serve.db), ADR 0024 (live-score overlay),
ADR 0011 (per-competition scoping), and ADR 0012 (on-pitch vs shootout goals). Extends
the "Standings" definition already in CONTEXT.md. Fourth entry of the "Football 2.0" line
of work.*

The Viewer lists the tracked leagues and cups as chips (ADR 0023). We want clicking a
**league** to open a section showing, for the season in progress: the standings table
computed from that season's games, the top scorers and top assisters, and the team with
the most goals. Cups are out of scope for now — their group + knockout structure means a
single round-robin table does not apply, and they will get their own treatment.

Two facts from the data shape the whole design. First, **`serve.db` cannot answer this as
built**: it carries only the −3..+10 day fixture window (ADR 0023) and *no* `squadentry`,
so it has neither a full season of fixtures for a table nor the per-player goal/assist
data for leaders. Second, **"current season" is not `max(config seasons)`**: through the
off-season that maximum is the next campaign, already listed in config but not yet kicked
off (La Liga 2026/27: 380 scheduled fixtures, 0 played, 0 squad data). A standings table
for it would be empty. Standings are also **computed, never stored** — there is no
`Standings` table in `football.models`; the only existing implementation is the
`explore.py` notebook.

**Decisions:**

- **Target season = the latest season with at least one Final fixture.** Not
  `max(config seasons)`. Today that resolves to 2025 (2025/26) for La Liga; the moment
  2026/27 has a game go Final it auto-advances to 2026 and the table becomes the new
  season's partial standings. This always yields a view that has data and reflects the
  season actually in progress, with no empty off-season tables and no manual rollover.

- **Precompute the aggregates into new `serve.db` tables, in `web.publish`.** For each
  tracked **league**'s target season, publish computes the standings rows, the top-N
  scorer and assister leaders, and the team-goals, and writes them to small purpose-built
  tables (`league_standing`, `league_scorer`; a `league_meta` carries the resolved season
  and stats-light flag). The Viewer only `SELECT`s and renders — no computation. This is a
  deliberate departure from ADR 0023's *schema-identity* rule: that rule existed to reuse
  the Match Tracker reader against `serve.db`/`live.db`, which is irrelevant here (new
  data, new rendering). The aggregates are tiny (~20 standings + ~15+15 leaders per league
  ≈ ~1,000 rows total) and stable within a day, so precomputing keeps `serve.db` small and
  the Viewer dumb. We rejected copying the raw season **fixtures + squadentry** into
  `serve.db` and computing in the Viewer: `squadentry` is ~17k rows per league-season
  (~340k across the tracked leagues), roughly doubling `serve.db` to carry data used only
  to derive a top-15. The cost of precomputing: a new league view means a publish change,
  not just a Viewer change — acceptable for a store rebuilt daily.

- **Standings rules — reuse `explore.py`, leagues only.** A league is a single
  round-robin (every fixture has a matchday; no phases/groups). Only **Final** fixtures
  (`FT`/`AET`/`PEN`) count, so mid-season `P` is games actually played. Points 3/1/0;
  result per team from on-pitch `home_goals`/`away_goals` (shootout scores excluded, ADR
  0012 — a drawn league game is a draw for both). Columns Pos, Team, P, W, D, L, GF, GA,
  GD, Pts. **Sort Pts → GD → GF.** Real leagues use competition-specific tiebreakers (La
  Liga head-to-head, Premier League goal difference); computing those per league is a
  rules engine we declined. Instead a universal GD/GF sort with a small **"unofficial
  ordering"** footnote — it matches the official table except when teams are level on both
  points and GD, and the footnote sets that expectation without pretending to be official.

- **Leaders: top-5, per-player season totals.** Top scorers and top assisters are the
  top 5 each by `sum(squadentry.goals)` / `sum(squadentry.assists)` over the season's
  league fixtures, **grouped by player** (a mid-season transfer within the league sums to
  the player's season total, primary team shown) — not by player+team as `explore.py`
  does, because a "top scorer" means the season total. Team-most-goals is a single
  highlight, the top of the standings' GF column.

- **Interaction: an inline section from an HTML fragment.** Clicking a league chip fetches
  `GET /league/{id}` — an HTML fragment, the same pattern as ADR 0024's `/week` — and
  injects it into a detail area that opens in place, one league at a time, with a close
  control. Reuses the Viewer's fragment machinery; no new page. We rejected a dedicated
  `/league/{id}` *page* (bookmarkable, but a navigation away from the panel rather than a
  section opening beside it) for v1 — a stable per-league URL is an easy later change if
  wanted.

- **Cups are inert; stats-light leagues degrade gracefully.** In the panel, league chips
  get a click affordance; cup chips stay static (no cursor, no action) until cups get
  their own treatment. A **stats-light** league (fixtures+events only, no `squadentry`;
  CONTEXT.md) still gets a standings table from its fixtures, but the leader blocks show a
  "player stats not collected for this competition" note rather than empty boxes — the
  `league_meta.stats_light` flag drives that.

**Consequences:**

- `web.publish` gains a computation stage after the copy: it reads the freshly-built
  `serve.db` (or `football.db`) for each tracked league, computes the aggregates, and
  writes the three new tables into the same `serve.db` before the atomic swap. Publish is
  no longer a pure copy — it now also *derives*. Still zero-API, still seconds.

- `serve.db` is no longer schema-identical to `football.db` — it carries `league_standing`,
  `league_scorer`, `league_meta` that `football.db` does not. The Match Tracker's
  schema-identical precedence (ADR 0022/0023) is unaffected: those tables are additive and
  the fixture/event/player tables are unchanged.

- New Viewer route `GET /league/{id}` (HTML fragment) and the panel's league chips become
  clickable. The standings/leaders reflect the daily publish, like the rest of `serve.db`;
  incorporating today's just-finished games (via the ADR 0024 live overlay) into the
  standings before the next publish is deferred.

- The standings ordering can differ from the official table in the rare level-on-points-
  and-GD case; the footnote makes that explicit. Per-league official tiebreakers are
  deferred, as is a season picker for prior seasons (the data is all present) and any cup
  treatment.
