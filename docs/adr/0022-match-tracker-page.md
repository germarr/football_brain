# Per-fixture Match Tracker page (provisional-preferring, SSE display refresh)

*Relates to ADR 0020 (Live Poll / Live Mirror), ADR 0021 (operator dashboard +
command registry), ADR 0018 (nightly Refresh), and ADR 0007 (match-event timeline).*

The operator dashboard (ADR 0021) lists **this week's** fixtures read-only and offers
a single bulk *Live-poll a match* card that launches `live.poll` for a multi-select of
fixtures. What it does **not** offer is a way to *click one game and watch it* — see the
timeline we already have, start or stop polling it, and have the page update itself as new
in-play events land. That is what a **Match Tracker** page adds: a focused per-fixture view
that is a **reader + launcher** over the existing Live Poll (ADR 0020), introducing **no new
collection path**.

The design has to hold two things apart that both want to be called "polling":

- the **Live Poll** — the API-side `live.poll` loop that hits the provider every 60s and
  writes `live/live.db` (spends quota; ADR 0020);
- the **display refresh** — the browser re-reading *our own server* to re-render the page as
  `live.db` changes (zero quota).

They are independent cadences. Conflating them is the main thing this ADR prevents.

**Decisions:**

- **Live-focused content: headline + timeline only.** The page renders the score / status /
  elapsed headline and the goal/card/sub/VAR **Event** timeline — exactly what the Live Mirror
  can supply and exactly what updates live. The richer `match_story.py` sections (starting XIs,
  team stats, Man of the Match) stay in the marimo notebook; they are `squadentry`/`teammatchstat`
  data the Mirror never holds (ADR 0020), so on the live matches you'd actually track they would
  be empty and static. Rejected a full match page with graceful-degrade sections as mostly-empty
  clutter for the live case.

- **Data precedence — the Mirror wins while it exists, else authoritative.** Per fixture: if a
  `livepoll` row exists in `live.db`, the page reads its **provisional** headline + timeline from
  `live.db` (badged *provisional · as of {polled_at}*); otherwise it reads the authoritative
  `football.db`. One rule, easy to reason about: the page goes live the moment you poll and reverts
  to authoritative once the Mirror row is cleared. We rejected *freshest-wins / auto-heal* (prefer
  the Mirror only until `football.db` has the collected events, then switch back) as more logic than
  the payoff, and *side-by-side* (two columns) as visually heavy and duplicative. The accepted risk
  of the simple rule — a **stale Mirror row shadowing the authoritative record** after the nightly
  Refresh writes the Final match — is mitigated by an explicit **Clear** control (below), matching
  CONTEXT.md's _Avoid_: "keeping a poll's row once the Refresh has written the Final record."

- **Display refresh = server-sent events, push-on-change.** A `GET /fixture/{id}/live` SSE stream
  watches `livepoll.polled_at` for the fixture and pushes the current headline + timeline **only when
  it changes**, sending one initial state on connect so the page shows "what we have" immediately —
  even before any poll. This reuses the exact `StreamingResponse` pattern already serving job logs in
  `app.py`. For a **Poll once** run `polled_at` changes once → one push; for **Poll live** it pushes
  each cycle. Rejected client-side interval polling of a JSON endpoint: with the poll cadence already
  known and the update event observable in one cheap column, push-on-change wastes no redraws and
  matches the codebase's existing SSE idiom. (A plain `GET /fixture/{id}/state` JSON endpoint is still
  provided for the initial server-render and for testing.)

- **Controls reuse existing plumbing; only Clear is new.** The page's control bar is contextual:
  **Poll live (60s)** and **Poll once** both `POST /run` with the existing `live_poll` command —
  `commands.build_argv` already turns `{fixture id, --interval / --once}` into the right argv, and the
  build-lock exemption for `live.poll` (ADR 0021) already applies. **Stop** reuses the existing job
  stop for that fixture's running poll. **Clear live data** is the one new endpoint —
  `POST /fixture/{id}/clear` deletes this fixture's rows from `live.db` (`fixture`, `event`, and the
  `livepoll` marker), reverting the precedence rule to authoritative. No second collection path exists:
  the page starts, stops, and forgets ADR 0020's poll, nothing more.

**Consequences:**

- New routes on the FastAPI app (`football.ui.app`): `GET /fixture/{id}` (the page),
  `GET /fixture/{id}/state` (headline + timeline JSON under the precedence rule),
  `GET /fixture/{id}/live` (the SSE display-refresh stream), and `POST /fixture/{id}/clear`. The
  existing *this week* table rows become links to `/fixture/{id}`.

- The timeline-rendering logic (icons and human descriptions for Goal / Card / subst / Var, including
  the `subst` gotcha where `player_id` = the player **off** and `assist_id` = the player **on**, per
  CONTEXT.md) is reimplemented server-side in `app.py` for the state JSON, mirroring `match_story.py`'s
  `_describe`/`_icon` so the two views agree.

- The page can legitimately show **provisional** data that later disagrees with the authoritative
  record (a VAR reversal between the last poll and Final) — the accepted nature of the Mirror (ADR
  0020). The provisional badge, the `polled_at` freshness line, and the reversible **Clear** make that
  divergence legible rather than silent.

- No change to cron, auto-discovery, or the collectors. The Match Tracker is still operator-driven:
  you open a game and decide whether/how to poll it.

- `CONTEXT.md` is untouched. **Live Poll**, **Live Mirror** and **provisional** are already defined;
  "Match Tracker page" and "display refresh" are implementation, not domain vocabulary — consistent
  with ADR 0020/0021 keeping UI terms out of the glossary.

- Deferred, each its own step: the richer match_story sections on the page once a match is
  Final + Refreshed; auto-clearing the Mirror row when the Refresh writes the authoritative Final
  (retiring the manual Clear); and a live score for a fixture you are *not* polling (would require
  refreshing `football.db`'s fixture list on demand, not just reading it).
