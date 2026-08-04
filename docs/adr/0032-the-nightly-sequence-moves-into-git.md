# The nightly sequence moves into git; the crontab keeps one path

---
Status: accepted — amends ADR 0018 (Refresh) and ADR 0023 (the cron that publishes
`serve.db`), and unblocks ADR 0031 (the package reorganization).
---

The whole nightly pipeline lives in a single installed crontab line:

    0 4 * * * cd /home/azureuser/alt_data && flock -n /tmp/football-refresh.lock /bin/sh -c '
        .venv/bin/python -m refresh …
        .venv/bin/python -m football.publish_pg --heal-venues …
        { git add football/venues.json; git diff --cached --quiet || git commit -m "venues: …"; }
        .venv/bin/python -m web.publish …'

Four module paths, one data path, and a git commit — none of it in version control. This
was tolerable while the paths were stable. ADR 0031 moves every one of them, which exposes
three problems at once.

**One checkout, one crontab, two branches.** `/home/azureuser/alt_data` is a single working
tree, so the 04:00 job runs against whatever branch is checked out. During a
reorganization the crontab cannot be right for both: pre-reorg paths crash against the
reorg branch, post-reorg paths crash the moment you switch back to compare. There is no
edit to an inline crontab that is correct on both nights.

**The venue commit fails silently.** Three of the four commands crash loudly if their
module moves — `MAILTO` delivers the traceback. But `git add football/venues.json` on a
path that no longer exists adds nothing, `git diff --cached --quiet` is then true, no
commit happens, and the **Venue registry** drifts uncommitted for weeks with no error
emitted on any night.

**The crontab is the only place project paths live outside git.** Every other reference —
`commands.py`, `web/app.py`, the ADRs — is versioned, reviewable, and moves with the branch
that changes it. The crontab is the sole exception, and it is precisely the exception that
cannot be tested before it runs.

**Decisions:**

- **The four commands move into a committed `scripts/nightly.sh`; the crontab keeps one
  path.** The cron line becomes `cd /home/azureuser/alt_data && flock -n
  /tmp/football-refresh.lock ./scripts/nightly.sh`. The only path left in the crontab is
  the repo root, which no reorganization will ever change. The sequence itself — which
  modules, in what order, with which redirections — becomes reviewable in a diff.

- **The interim problem disappears rather than being managed.** Because the script is
  versioned, whichever branch is checked out at 04:00 runs *its own* correct version of the
  sequence: `footballV3` runs pre-reorg paths, the reorg branch runs post-reorg paths, and
  neither needs a crontab edit at any point during the work. Cutting over is a merge, not an
  operational step. This is the decisive argument for the change; it is not a refactoring
  for neatness.

- **The script asserts before it acts, so silence becomes noise.** `nightly.sh` runs the
  preflight of ADR 0033 as its first statement and exits non-zero on failure, before any
  work or any API spend. Specifically it refuses to proceed when the **Venue registry** file
  is missing, when `config.RAW_DIR` does not resolve, or when any `commands.py` module string
  fails to import. The `git add` that used to drift quietly is now guarded by an existence
  check that emails on the first bad night rather than the fiftieth.

- **`flock` and the redirections stay exactly as they are.** The lock still guards the whole
  sequence against overlap, `refresh` still leaves stderr unredirected so a failed night
  emails, and publish still runs unconditionally (`;` rather than `&&`) so `serve.db`
  refreshes even after a partial refresh that still rebuilt `football.db` — the behaviour ADR
  0018/0023 chose deliberately. Moving the text into a file changes where the sequence is
  written, not what it does.

## Consequences

- **The crontab must be reinstalled exactly once**, and that installation is the last time
  it needs to change for a path reason. It is a manual step on this machine, outside git,
  and it is the single point where this ADR can be got wrong; it is worth verifying by
  running `./scripts/nightly.sh` by hand once before installing.

- **A broken branch at 04:00 now fails loudly instead of running the wrong thing.** Mid-reorg
  the checked-out tree may genuinely be inconsistent. The preflight catches that and exits
  before spending quota — a strictly better failure than a half-completed publish discovered
  the next morning.

- **The nightly sequence becomes part of code review.** Changing what runs overnight is now a
  commit on a branch like any other change, rather than an untracked edit to machine state
  that no diff records and no rollback recovers.

- **A second checkout would still not be safe to run**, because `data/raw` is a
  machine-specific symlink (ADR 0002 addendum) and `.env`/`.venv` are untracked. The script
  being versioned does not make the *environment* portable, and nothing here claims it does.

## Considered Options

- **Pause the cron for the duration of the reorganization.** Simple and safe, but the Viewer
  and Published Store go stale for as many nights as the work takes, the catch-up refresh
  afterwards is one large quota-bound run, and the underlying problem — paths outside git —
  is still there for the next reorganization.

- **Do the reorganization in a second git worktree, leaving this checkout and its cron
  untouched.** Attractive until the environment is examined: `.venv`, `.env` and `data/` are
  all untracked, and `data/raw` is a symlink to another volume, so the moved modules could not
  actually be *run* in the worktree without symlinking back the very state the cron writes to.
  A reorganization that cannot be executed is not verified.

- **Keep the crontab inline and edit its four paths at cutover.** The status quo. Rejected
  because it leaves the job broken on every night between branching and merging, and because
  it re-runs this exact conversation the next time anything moves.

- **A Python entrypoint (`python -m nightly`) rather than a shell script.** Marginally nicer
  to test, but it would have to import the project to run the preflight that decides whether
  the project is importable. The shell script can check first and import second.
