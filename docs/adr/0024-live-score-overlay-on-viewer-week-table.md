# Live-score overlay + a refresh button on the Viewer's week table

*Relates to ADR 0023 (Console/Viewer split + serve.db), ADR 0022 (Match Tracker
precedence), ADR 0020 (Live Poll / Live Mirror), and ADR 0018 (nightly Refresh).
Third entry of the "Football 2.0" line of work.*

The Viewer's *this week* table reads only `web/serve.db` — the once-a-day snapshot
(ADR 0023). So a match that was in play when `serve.db` was last published freezes at
that moment: France v Spain shows `0–1 2H` on the table long after it actually finished
`0–2 FT`. The per-match **Match Tracker** already solves this for a *single* opened
fixture — it overlays the provisional **Live Mirror** (`live/live.db`) over the snapshot
under a precedence rule (ADR 0022) and can launch `live.poll` to refresh it — but the
**table** ignores `live.db` entirely and has no way to refresh a score in place. We want
the table to reflect the true current score of the games that are live or just finished,
on demand, without turning `serve.db` back into a live read-dependency (the whole point
of ADR 0023).

The fix has two independent halves, and neither works alone: a **display overlay** (the
table reads `live.db` where it has fresher rows) has nothing fresh to show for a fixture
nobody polled; a **fetch** (put current scores into `live.db`) is invisible while the
table still ignores `live.db`. France v Spain needs both — it is frozen in the snapshot
*and* absent from `live.db`.

**Decisions:**

- **The table overlays `live.db`, same precedence as the Match Tracker.** `week_fixtures`
  now reads `serve.db` and, per fixture, prefers the Live Mirror's score/status when a
  `livepoll` row exists for it — otherwise the snapshot. One rule, already proven on the
  per-match page (ADR 0022), extended to the table. Implemented as a single batched read
  of `live.db` (all `livepoll`-marked fixtures at once) overlaid onto the window, not a
  per-row query.

- **A "Refresh live games" button fetches the active slate via `live.poll --once`.** The
  button POSTs `/refresh-live`; the Viewer computes the **active set** from `serve.db` —
  `date <= now (UTC)` AND `status NOT IN {FT, AET, PEN, PST, CANC, ABD, AWD, WO}` (kicked
  off but not recorded terminal) — and spawns the existing `live.poll --once <ids>`, the
  same subprocess the Match Tracker already launches (ADR 0023's sanctioned Viewer
  trigger). We reused `live.poll` rather than write a scores-only fetch: it correctly
  handles *both* in-play and just-finished fixtures (fetched by id, unlike
  `fixtures?live=all` which omits the just-finished — the case that actually hurts), it
  writes header + events + a `livepoll` marker so the same refresh lights up the Match
  Tracker timeline for free, and it adds no new collection code. The accepted cost is 2
  API calls per fixture (header + events) where a header-only batch (`fixtures?ids=`)
  would be one; for the handful of games live at once this is negligible against the
  150k/day cap. If a giant simultaneous slate ever makes it bite, a scores-only batch is
  the optimization.

- **Cost is made visible, not capped.** The button's label carries the live count —
  "↻ Refresh 4 live games" — so the API cost (count × 2 calls) is legible before the
  click. The `−3..+10` day window already bounds the universe; we did not add a hard cap
  (rejected as premature) — the count is the guardrail.

- **The table re-renders in place, no reload.** A new `GET /week` returns the week groups
  already overlaid with `live.db`. The button shows an "updating…" state, watches the
  poll job to completion over the existing `/jobs/{id}/stream` end signal, then re-fetches
  `/week` and swaps the table body — matching the Match Tracker's in-place re-render. We
  rejected a full `location.reload()` (flashes the whole page) and a table-level SSE
  auto-refresh (overkill for a one-shot `--once` button; noted as the natural path *if* a
  continuously self-updating live table is ever wanted).

- **Provisional rows are marked; the overlay self-heals on publish.** A row sourced from
  `live.db` is provisional — revisable, even at `FT`, until the authoritative publish
  writes it (ADR 0020) — so it carries a subtle marker (a dot with a `polled Xs ago`
  hint), honouring ADR 0022's "provisional must be legible, not silent." And because the
  table makes a stale Mirror row *more* visible than the per-match page did (a finished
  game would keep a provisional dot until manually cleared), **`web.publish` now deletes
  any `live.db` rows for fixtures it just wrote as Final into `serve.db`** — retiring the
  manual Clear for finished games. This is the auto-clear ADR 0020/0022 deferred; the
  table overlay is what makes it worth doing now.

- **No change to the ADR 0023 exposure decision, and `CONTEXT.md` untouched.** The button
  spawns `live.poll` — the one quota-spending subprocess the Viewer was already allowed
  (ADR 0023) — so the Viewer stays bound to `127.0.0.1`; nothing new is exposed. "Active
  slate", "refresh button", "overlay" are implementation, not domain language; the domain
  nouns (Live Poll, Live Mirror, provisional, Final) already exist, so the glossary is
  unchanged — consistent with ADR 0020–0023 keeping UI terms out.

**Consequences:**

- New coupling: `web.publish` now reads *and writes* `live.db` (deletes settled rows),
  where before it only read `football.db` and wrote `serve.db`. A publish still never
  touches `football.db`'s content; the `live.db` delete is scoped to fixtures the fresh
  snapshot records as Final.

- New Viewer routes: `POST /refresh-live` (compute active set → spawn `live.poll --once`)
  and `GET /week` (overlaid week JSON). `week_fixtures` gains the `live.db` overlay used
  by both the initial render and `/week`.

- The table can now legitimately show a score that later disagrees with the eventual
  authoritative record (a VAR reversal between the poll and the Final), the accepted
  nature of the Mirror (ADR 0020). The provisional marker and the publish auto-clear keep
  that divergence legible and short-lived.

- Refreshing costs quota from the table, a surface that was read-only under ADR 0023.
  That is deliberate and bounded (manual button, visible count, `live.poll`-only), and it
  is why the Viewer remains localhost.

- Deferred: a scores-only batch fetch (`fixtures?ids=`) if big slates make the 2-calls-
  per-fixture cost bite; a continuously self-updating live table via table-level SSE; and
  auto-discovery of the live slate via `fixtures?live=all` (still deferred from ADR 0020).
