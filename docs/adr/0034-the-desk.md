# The Desk: a surface for drafting, and the four things it is not allowed to do

---
Status: accepted — **amends ADR 0029**, which recorded the ESPN game-id lookup as
considered and deliberately unbuilt. It is now built, and the reasoning below is what
changed. Builds on ADR 0021 (command registry, subprocess triggers), ADR 0023
(Console / Viewer split), ADR 0031 (context is the tree, role is declared in the
registry), ADR 0014 (Coverage-light Competitions) and ADR 0028 (delta publish).
---

Drafting a match report meant typing this:

    uv run python -m football_blog.pipeline --fixture-id 1550918 --espn-id 401877021 --force-link

Every argument in that line is a small research task. The Fixture id comes from a SQL
query against the Published Store. The ESPN id comes from finding the match on ESPN by
hand. `--force-link` is there because ESPN's club names disagree with ours, which the
operator knows from experience rather than from anything on screen. And the prompt the
model is about to be given is invisible unless you run `--dry-run` — which, per its own
docstring, still executes stages 1 through 3 first.

So this ADR adds a third local surface, the **Desk**, whose job is to answer "what
could I write about tonight, and what will the model be told?" What follows is mostly
about the things it refuses to do, because a UI in front of the **Editorial Store** —
the one store here that is authored, has no rebuild path, and (at time of writing) is
not backed up — is a place where convenience is the risk.

**Decisions:**

- **A third surface, and it lives inside `football_blog/` rather than beside
  `console/`.** Neither existing app could host it. ADR 0023 stripped the **Operator
  Console** of *all* reading — "form options come from competitions.json (config), not
  football.db" — and this page's whole substance is a table read live from two networked
  stores. The **Viewer** reads `serve.db`, a window over local data, and knows nothing of
  **Publications** or **Match Posts**. Teaching either about the Editorial Store would
  spread a dependency on the one store with no rebuild path across three packages.
  The tree placement is the part that will look wrong at a glance, since `console/` and
  `web/` are both at the root. That is ADR 0031 applied, not ignored: role cross-cuts
  context, role is declared in `commands.py`, and a package does not move to express it.
  The Console sits at the root because it belongs to no context — it reads no store and
  only renders the registry and spawns argv. The Desk is the opposite: it imports
  `loader`, `prompts.builder`, `pocketbase`, `slugs` and `pipeline`, all from
  `football_blog`. Rooting it would create a package with no store of its own reaching
  deep into another context's internals — the exact coupling ADR 0031's tree exists to
  make visible. It is Control-role code inside a Publish-role package, and under ADR
  0031 that is not a contradiction; it is the point.

- **A Drafting Candidate is gated on exactly the two things the pipeline refuses, and on
  nothing else.** Final, and its Competition has a Publication. That is `assert_final`
  and `preflight`, and no third condition was added.
  The temptation was to also require "has all the data" — events, Squad Entries, Team
  Match Stats. It was rejected because `prompts/builder.py` does not fail on any of
  them; it emits `(team match stats not available)`, `(no per-player stats)`,
  `(no timed events recorded)` and writes a thinner report. Filtering on completeness
  would therefore hide Fixtures the pipeline would happily draft, with no way to ask
  why — and for a **Coverage**-light Competition (ADR 0014) it would hide *every* match,
  where a timeline-only report is precisely what we said we wanted. So completeness is
  rendered as a **signal** on each row and never as a filter. A Match Post that is
  already `published` stays listed too, visually retired: "have I written this one?" is
  a question the page should answer, and answering it by omission answers it badly.
  This split is load-bearing and it is the thing most likely to drift. If a gate is ever
  added to the pipeline and not to the Desk, the Desk silently stops listing work that
  is genuinely available — a failure with no error, which is what ADR 0033 exists for.

- **Refreshing the Candidates is *additive*, and deliberately re-does work the per-Fixture
  run will do again minutes later.** The Published Store only learns of a new Final at
  04:00 (`nightly.sh`) or when the pipeline's own steps 1 and 3 run. So the match that
  finished two hours ago — the one most worth writing about — is not listed, because the
  pipeline has not run, which you cannot trigger from a list it is not on. `football.db`
  does not rescue this: the same cron rebuilds it, so it is stale in the identical window.
  The Desk therefore gets a **Refresh Candidates** command: the frontier refresh plus a
  delta publish, run once across every Publication's Competition instead of once per
  Fixture.
  The obvious follow-on — have the per-Fixture run then skip its own steps 1 and 3,
  since the Desk just did them — is **rejected**, and this is the subtle part. ADR 0029
  fixed the ESPN stage *between* the refresh and the publish because `_apply_delta`
  full-copies the commentary tables on every apply, so one publish carries both the new
  Final and its commentary. If the Desk publishes first and the run then ingests ESPN,
  the commentary lands after the publish and needs a second one — the arrangement 0029
  examined and rejected. Keeping the pipeline's stages untouched costs a redundant
  refresh of one Competition, which is seconds and mostly cache hits. A future reader
  will find that redundancy and be tempted to remove it; this paragraph is why not.

- **The ESPN game id is now *proposed* — reversing ADR 0029 — and the reversal is really
  about `--force-link`.** 0029 conceded the argument and declined to build it: the
  scoreboard would propose, `fixture_link.py` would still check exact kickoff and exact
  team names, and "proposal by code is not assertion by hand." What changed is not that
  argument but the evidence. The command at the top of this file carries `--force-link`,
  and it is habitual — ESPN's club names disagree with ours for Liga MX and MLS often
  enough that the flag is now passed preemptively. **A check that is always waived checks
  nothing**, and it is the check standing between us and linking commentary to the wrong
  match, an error `fixture_link.py` itself says nothing downstream could detect.
  So the Desk fetches `scoreboard?dates=YYYYMMDD` for the Fixture's kickoff date and
  filters candidates by **kickoff within the ADR 0030 tolerance — never by team name**,
  which would be circular given that names are exactly what disagree. It shows ESPN's
  names against ours and the operator picks. `fixture_link.py` runs unchanged at ingest;
  the proposal waives nothing and cannot. The gain is not the saved copy-paste: it is
  that `--force-link` stops being a reflex and becomes a judgment made against two names
  displayed side by side, for one match at a time.
  Because this puts an outbound call in a page render, the payload cache in
  `commentary/espn.py` is reused, and an ESPN failure must surface as *"ESPN
  unreachable — enter the id manually"* and never as an empty candidate list. "No match
  found" and "could not ask" are different answers and must not render alike.

- **The prompt is shown whole and edited only where it is authored. The derived facts
  block is read-only, and a per-run instruction is recorded on the Match Post.** What
  reaches the model has three layers: `prompts/system_{lang}.md` (a git file), the
  Publication's `llm_prompt_overrides` (PocketBase), and the user prompt that
  `builder.py` derives from the Fixture. The Desk renders all three exactly as they will
  be sent; the first two are editable, each written back where it lives.
  The third is refused, and the reason is narrower than "derived data should not be
  edited". CONTEXT.md already grants that a **Narrative** cannot be regenerated. That is
  survivable only because its *input* can be: re-run the builder against the store and
  the identical facts block comes back. Allow the facts to be hand-edited and the input
  becomes unrecorded too — a Narrative that cannot be reproduced, built from a prompt
  that cannot be reconstructed, asserting facts that may be in no store. Rule 1 of
  `system_es.md` is *"Nunca inventes datos."* Editing that block is the operator
  inventing them on the model's behalf.
  What survives of the impulse is a per-run **draft instruction** — *"lead with the
  comeback"* — appended for one draft. It is honest only if it is durable, so it becomes
  a `draft_instruction` field on `match_post` via `pb_migrations`, written whenever it is
  used. Unrecorded, it is the same provenance hole wearing a nicer hat; and retrofitting
  the field later leaves it null on early Match Posts for reasons nobody will remember.

- **`--redraft` is not on the Desk.** It overwrites a *published* Match Post, destroying
  hand-edited prose that nothing regenerates — the single most destructive action in this
  repo, in the only store with no rebuild path and no backup. The pipeline currently
  guards it with a refusal and four lines of explanation; a checkbox is not that. A
  published Match Post renders retired with the exact command to paste. A confirmation
  dialog was rejected as friction theatre: a step you can learn to perform is not a
  guard. The argument is not that it would be clicked carelessly, it is the asymmetry —
  every other action here is re-runnable, this one alone is not, and friction is the only
  protection it has. `--reclassify` is exposed normally; its cost is money and
  non-identical Categories, both bounded.

**Consequences:**

- **The Desk is a launcher, and stops at the log.** It streams the run and deep-links to
  the Match Post in the PocketBase admin. It does not render, edit or publish the
  Narrative. Rendering it read-only was genuinely tempting and adds no write path, but it
  invites exactly one question — *why can't I fix that typo here?* — whose only answer is
  "we decided not to," and that is a weak wall. A deep link answers it structurally: the
  place you read it is the place you edit it. The harder line matters more: moving a Match
  Post to `published` is, per CONTEXT.md, "a manual, human act, and the only one," and the
  safety of that rests on drafting and publishing being separate acts in separate places.
  On one page they would be adjacent buttons.

- **Reads run in-process; writes run as a subprocess of the exact CLI argv.** The Candidate
  list, the prompt preview and the ESPN proposals are read-only, spend no quota, and are
  answered directly. The pipeline run and Refresh Candidates spawn `python -m …` and stream
  over SSE, exactly as the Console does. `pipeline --dry-run` is *not* what backs the
  preview: it runs stages 1–3 before printing anything, so the Desk calls
  `build_user_prompt` directly. Both paths must assemble the prompt through one function
  or the Desk shows you something other than what it sends.

- **`--instruction` becomes a real flag, so nothing is Desk-only.** ADR 0021's rule — "the
  UI is a *second* front door to the same commands, never a reimplementation" — would
  otherwise break the moment the Desk grew a capability the terminal lacked. Refresh
  Candidates likewise enters `commands.py` under **Refresh**, and the Desk itself enters
  under **Control** (unlike the Console, which cannot invoke itself, the Desk is a
  separate process the Console can launch). The README role table gains it on the Control
  row.

- **The Editorial Store's missing backup is now urgent, and is tracked separately.** ADR
  0029 wrote that this store "is the only one here that genuinely needs backing up." It
  is not: nothing in cron or any backup script touches `personal_site/pocketbase/pb_data`.
  The Desk does not make this worse in kind — the CLI could always overwrite a draft — but
  it lowers the cost of every write to the store to two clicks, and that changes how often
  the gap is exposed. It belongs in `nightly.sh` (ADR 0032), not in this work.

- **Four things here fail silently, and are what the tests target (ADR 0033).** The
  Candidate predicate drifting from the pipeline's gates, which makes the Desk *hide*
  available work; `--instruction` parsed but never threaded into `draft_narrative`, which
  looks like success and produces a Narrative that reads subtly wrong; the preview
  assembled differently from the real call, so an edited prompt is not the sent prompt;
  and an ESPN failure rendered as an empty proposal list. The first three produce no
  error at all.
