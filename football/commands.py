"""The command registry — the single documented source of truth for every
triggerable pipeline command (ADR 0021).

Each `Command` records a script's role (`group`), a human `summary`/`detail` of
what it accomplishes, the module invoked as `python -m <module>`, and its typed
`params`. The terminal keeps calling `python -m <module>` directly and needs
nothing here; the Operator Console (`console`) reads this registry to render
its trigger sections and to build the exact same argv as a background subprocess.

Adding a command to the UI = adding an entry here. That's the deliberate cost of
keeping role + description + params documented in one place (ADR 0021).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

# The operator-facing groups (ADR 0021; ADR 0023 dropped Live, added Publish; ADR 0031
# split Collect and added Control). Order is display order.
#
# Onboard admits an entity to a Registry so every later recurring job covers it;
# Backfill bulk-fetches Seasons into the raw cache and admits nothing. They were one
# group until ADR 0031, but they fail differently: a backfill cut short resumes for
# free, while an entity that was never onboarded is covered by nothing (CONTEXT.md).
#
# Publish covers both derived read stores: the Viewer's serving copy (ADR 0023) and the
# remote Postgres Published Store (ADR 0027) — the latter is the one command here that
# never touches football.db, deriving from the raw cache instead.
#
# Control is the surfaces you drive the pipeline from. Since ADR 0035 all three are one
# process — `python -m surfaces`, Viewer at /, Desk at /desk, Console at /console — and
# its single entry below is the only Control command. The old "the Console cannot invoke
# itself" no longer holds literally: the Console is *inside* what that entry launches, so
# running it from the Console starts a second copy of all three on another port. It is
# marked long_running (a server, not a job) and defaults to :8001, so the usual outcome
# is an "address already in use" in the log rather than a surprise. The Live Poll
# launcher moved to the Viewer's Match Tracker page (ADR 0023).
GROUPS = ["Onboard", "Backfill", "Build", "Refresh", "Publish", "Control"]


@dataclass
class Param:
    """One argument of a command, and how the UI should render/collect it.

    `kind` drives both the form widget and how the value becomes argv:
      - "bool"        -> checkbox; emits `flag` when checked, nothing otherwise
      - "int"/"str"   -> number/text input; emits `flag value` (or bare value if
                         positional, i.e. flag is None)
      - "competition" -> <select> the app fills with tracked competitions; the
                         chosen league_id is emitted as a positional/flag value
      - "competitions"-> multi <select> of tracked competitions; emits each chosen
                         league_id as a positional (nargs="*"). Distinct from
                         "competition": for a command whose argument is a *set*
                         (publish_pg's Published Store IS exactly the ids given),
                         a single-pick widget cannot express the intent.
      - "fixture"     -> multi <select> the app fills with this week's fixtures;
                         emits each chosen fixture id as a positional (nargs="+")
    `flag` None means a positional argument.
    """
    name: str
    label: str
    kind: str = "str"           # bool | int | str | competition | competitions | fixture
    flag: str | None = None     # e.g. "--from", "--no-events"; None = positional
    required: bool = False
    default: object = None
    help: str = ""
    placeholder: str = ""
    advanced: bool = False      # tuck behind a "more options" toggle in the UI


@dataclass
class Command:
    key: str                    # url-safe id, unique
    group: str                  # one of GROUPS
    title: str                  # short human title
    module: str                 # invoked as: python -m <module> [args]
    summary: str                # one line: what it accomplishes
    detail: str = ""            # a sentence or two of context / caveats
    example: str = ""           # a concrete "run it now" scenario, in glossary terms
    scope: str = ""             # what it touches at a glance: "one league" vs "all competitions"
    params: list[Param] = field(default_factory=list)
    builds_db: bool = False     # rebuilds data/football.db (serialized by build lock)
    long_running: bool = False  # hours, or (live poll) an unbounded loop
    network: bool = False       # spends API quota
    danger: bool = False        # destructive (e.g. --delete)


# Shared collect-time flags for orchestrate/cups (identical argparse on both).
def _collect_flags() -> list[Param]:
    return [
        Param("name", "Canonical name override", "str", "--name",
              placeholder="(default: provider name)", advanced=True,
              help="Renames the Competition in your data — use it to disambiguate the "
                   "two 'Serie A's; leave blank to keep the provider's name."),
        Param("from_season", "From season", "int", "--from", placeholder="e.g. 2015",
              advanced=True,
              help="Earliest season to collect — raise it to skip old history and spend "
                   "less quota (default: the earliest the provider offers)."),
        Param("to_season", "To season", "int", "--to", placeholder="e.g. 2026",
              advanced=True,
              help="Latest season to collect — lower it to stop before the current season."),
        Param("calendar_year", "Calendar-year seasons", "bool", "--calendar-year",
              advanced=True,
              help="Labels each season as a single year (e.g. 2024), not a 2024/25 split. "
                   "Needed for calendar-year leagues like the Brasileirão; wrong for European ones."),
        Param("no_events", "Skip event timeline", "bool", "--no-events", advanced=True,
              help="Skips the Event timeline (goals/cards/subs) — faster and cheaper, but "
                   "match_events stays empty for this Competition."),
        Param("no_stats", "Skip team match stats", "bool", "--no-stats", advanced=True,
              help="Skips Team Match Stats (possession/shots/xG) — leaves those columns null; "
                   "fixtures and results are still collected."),
        Param("no_careers", "Skip career histories", "bool", "--no-careers", advanced=True,
              help="Skips Career Stint histories — player Bios still load, but you lose each "
                   "player's cross-competition career."),
        Param("no_teams", "Skip team-profile enrichment", "bool", "--no-teams", advanced=True,
              help="Skips Team Profile enrichment — the DB still builds, but out-of-league "
                   "career teams stay bare ids until you run Enrich team profiles."),
        Param("no_rebuild", "Collect only (don't rebuild DB)", "bool", "--no-rebuild",
              advanced=True,
              help="Fetches into the raw cache but skips rebuilding football.db — useful when "
                   "you'll run several collections back-to-back and rebuild once at the end."),
        Param("no_publish", "Don't refresh the Viewer (skip publish)", "bool", "--no-publish",
              advanced=True,
              help="After the rebuild, this normally publishes web/serve.db so the new "
                   "Competition shows in the Viewer immediately. Tick to skip that and "
                   "publish later with the Publish command."),
    ]


COMMANDS: list[Command] = [
    # --- Onboard / Backfill (network — spends API quota) ------------------------------
    Command(
        key="orchestrate", group="Onboard", title="Add / re-collect a league",
        module="football.onboard.orchestrate",
        summary="Collect one league end-to-end and register it as a Competition.",
        detail="Fixtures, squads, bios, careers, team profiles, events and team "
               "stats for the given provider league id, then rebuild football.db and "
               "publish serve.db so the new league appears in the Viewer right away. "
               "Cache-first and resumable — re-run to continue after a quota stop.",
        example="Enter 61 (Ligue 1) → collects every Ligue 1 season end-to-end — fixtures, "
                "Squad Entries, player Bios, Career Stints, the Event timeline and Team Match "
                "Stats — then rebuilds football.db and registers Ligue 1 as a tracked "
                "Competition. Re-run the same id after a quota stop to resume where it left off.",
        scope="one league · every layer",
        network=True, builds_db=True, long_running=True,
        params=[
            Param("league_id", "Provider league id", "int", None, required=True,
                  placeholder="e.g. 61 (Ligue 1)",
                  help="The API-Football league id. Use a NEW id to add a league, "
                       "or an existing one to re-collect it."),
            *_collect_flags(),
        ],
    ),
    Command(
        key="cups", group="Onboard", title="Add / re-collect a cup",
        module="football.onboard.cups",
        summary="Collect one cup-type Competition (tags group/knockout phases).",
        detail="Like the league orchestrator but for cups (Champions League id 2, "
               "World Cup id 1). Refuses a non-cup id. Rebuilds football.db at the end, "
               "then publishes serve.db so the new cup appears in the Viewer right away.",
        example="Enter 2 (Champions League) → the same end-to-end collection as a league, but "
                "each fixture is tagged with its group vs knockout phase. Passing a league id "
                "(e.g. 61 Ligue 1) is refused — this entrypoint is cups only.",
        scope="one cup · every layer",
        network=True, builds_db=True, long_running=True,
        params=[
            Param("league_id", "Provider league id", "int", None, required=True,
                  placeholder="e.g. 2 (Champions League)",
                  help="The API-Football league id of a cup competition."),
            *_collect_flags(),
        ],
    ),
    # The "Full backfill" command was retired by ADR 0031. It drove the full-sweep
    # CLI in the old football/collect.py, which the per-league orchestrator superseded
    # (ADR 0009) — it was neither Coverage-gated (ADR 0014) nor a collector of events
    # (ADR 0007) or team stats (ADR 0010), and none of the registered competitions was
    # collected through it. Its shared fetch helpers survive as football/fetch.py.
    Command(
        key="collect_events", group="Backfill", title="Backfill event timeline",
        module="football.collect.events",
        summary="Fetch the goal/card/sub/VAR timeline (fixtures/events) for every fixture.",
        detail="Separate, day-long run (~20.8k calls, ADR 0007). Cache-first and "
               "resumable; the daily quota stops it cleanly. Writes the raw cache only.",
        example="Arsenal 3–1 Chelsea is already cached with its final score. This adds the "
                "Events: the 71' Saka goal, the 84' red card, the subs — WHO did WHAT at which "
                "MINUTE. It says nothing about how many shots each side had (that's Team match "
                "stats). ~20.8k calls; writes the raw cache — run Rebuild afterwards.",
        scope="all competitions · events only",
        network=True, long_running=True,
    ),
    Command(
        key="collect_stats", group="Backfill", title="Backfill team match stats",
        module="football.collect.stats",
        summary="Fetch per-team match aggregates (possession, shots, xG).",
        detail="Separate resumable backfill of fixtures/statistics into the raw "
               "cache. Run a Build afterwards to surface it in football.db.",
        example="For that same Arsenal 3–1 Chelsea, this adds the two Team Match Stats rows — "
                "Arsenal 62% possession / 17 shots / 2.4 xG vs Chelsea's — the per-team "
                "aggregate COUNTS, not a minute-by-minute timeline (that's Event timeline). "
                "One call per fixture; writes the raw cache — run Rebuild afterwards.",
        scope="all competitions · stats only",
        network=True, long_running=True,
    ),
    Command(
        key="teams", group="Backfill", title="Enrich team profiles",
        module="football.collect.teams",
        summary="Build the Team Profile directory for every team a career touches.",
        detail="Resolves each career team's identity and representative league "
               "(ADR 0017), then rebuilds football.db unless told not to.",
        example="Walks every team any Career Stint touches — including clubs outside your "
                "tracked leagues — and builds each one's Team Profile (identity + "
                "representative league, ADR 0017). Example: a player who once played for Boca "
                "Juniors gets Boca added to the Team Profile directory even though you don't "
                "track the Argentine league. Rebuilds football.db at the end.",
        scope="every career-touched team",
        network=True, builds_db=True, long_running=True,
        params=[
            Param("no_rebuild", "Enrich only (don't rebuild DB)", "bool", "--no-rebuild"),
        ],
    ),

    # --- Build (offline — no network) --------------------------------------
    Command(
        key="parse", group="Build", title="Rebuild football.db",
        module="football.build.parse",
        summary="Re-model the raw cache into data/football.db (drops & rebuilds all tables).",
        detail="Offline, no network. A cache miss fails loudly rather than spending "
               "quota, so it is safe to run repeatedly.",
        example="Drops and rebuilds every table in football.db purely from the raw cache — no "
                "network. Example: right after a Backfill event timeline run, this is what "
                "surfaces those new Events into match_events so they show up in queries. A "
                "cache miss fails loudly instead of spending quota, so it's safe to re-run.",
        scope="whole database · offline",
        builds_db=True,
    ),
    Command(
        key="scope", group="Build", title="Extract / delete a single-competition DB",
        module="football.build.scope",
        summary="Build data/<slug>.db for one tracked Competition from the shared cache.",
        detail="Zero-API re-parse of one already-collected Competition into its own "
               "SQLite file (ADR 0011). Use Delete to remove that scoped DB.",
        example="Pick Champions League → writes data/champions-league.db containing just that "
                "Competition, re-parsed from the shared cache with zero API calls (ADR 0011). "
                "The shared football.db is untouched. Tick Delete to remove that scoped file "
                "instead of building it.",
        scope="one competition → its own DB",
        params=[
            Param("league_id", "Competition", "competition", None, required=True,
                  help="Pick one of the competitions you already track."),
            Param("delete", "Delete the scoped DB instead of building it", "bool",
                  "--delete", advanced=True,
                  help="Deletes data/<slug>.db for this Competition instead of building it — "
                       "the shared football.db is unaffected."),
        ],
    ),

    # --- Refresh (nightly frontier) ----------------------------------------
    Command(
        key="refresh", group="Refresh", title="Nightly frontier refresh",
        module="refresh",
        summary="Re-collect only the mutable frontier: each competition's current season.",
        detail="Force-refreshes the current-season fixture list and collects any "
               "newly-Final fixtures (ADR 0018), then rebuilds football.db and "
               "re-scopes existing per-competition DBs. This is the cron job.",
        example="Re-collects only the mutable frontier — each Competition's current season: "
                "force-refreshes today's fixture list, pulls any fixtures that just went Final, "
                "rebuilds football.db and re-scopes your per-competition DBs (ADR 0018). Run at "
                "03:00 it picks up last night's results without re-touching finished seasons.",
        scope="all competitions · current season only",
        network=True, builds_db=True, long_running=True,
        params=[
            Param("scope_only", "Scope-only (fast, no network)", "bool", "--scope-only",
                  advanced=True,
                  help="Skips all collection and network — just rebuilds football.db and "
                       "re-scopes per-competition DBs from the cache you already have. Fast."),
            Param("no_rebuild", "Collect only (don't rebuild DB)", "bool", "--no-rebuild",
                  advanced=True,
                  help="Collects the frontier into the cache but skips the rebuild + re-scope "
                       "— you'll need to run Rebuild football.db afterwards."),
        ],
    ),

    Command(
        key="candidates", group="Refresh", title="Refresh Candidates",
        module="football_blog.candidates",
        summary="Pull every Publication's Competition frontier, then list what can be drafted.",
        detail="The Desk's Refresh Candidates, as a command (ADR 0034). Refreshes the "
               "frontier for the Competitions a Publication covers and delta-publishes "
               "them to the Postgres Published Store, then prints the board: every "
               "Fixture that is Final, covered by a Publication, and not already a "
               "published Match Post. Runs `refresh --no-rebuild`, so football.db and "
               "serve.db are untouched. Exists because a match that finished two hours "
               "ago — the one most worth writing about — is in no store until 04:00, "
               "and you cannot trigger a per-Fixture run from a list it is not on. A "
               "partial run (hit quota) thins the board rather than emptying it.",
        example="Run it after tonight's games: it force-refreshes the current-season "
                "frontier for Liga MX, MLS and the World Cup, collects whatever went "
                "Final, delta-publishes those rows, and prints the Drafting Candidates "
                "with their EV/SQ/TS/CM signals. Then draft one from the Desk at "
                "http://127.0.0.1:8001/desk. Tick Skip the refresh to just re-read the "
                "store — no network, no quota.",
        scope="every Publication's competition · current season only",
        network=True, long_running=True,
        params=[
            # No `default` on purpose: leaving the field blank emits no --days, so the
            # window's default stays decided in candidates.py (DEFAULT_DAYS) rather than
            # being copied here where the two could drift apart silently.
            Param("days", "Window (days)", "int", "--days", placeholder="14 (default)",
                  help="How far back to look for Finals. Affects the printed board only "
                       "— the refresh always covers every Publication's Competition, "
                       "because a half-refreshed frontier is not worth having."),
            Param("league_id", "Restrict to one Competition", "competition", "--league-id",
                  advanced=True,
                  help="Narrows the printed board to one Competition. Does NOT narrow the "
                       "refresh, which still covers them all."),
            Param("no_refresh", "Skip the refresh (list only)", "bool", "--no-refresh",
                  advanced=True,
                  help="Prints the board from the Published Store as it stands — fast, no "
                       "network, no quota, and no delta publish. Use it to re-read the "
                       "board without paying for it; anything that went Final since the "
                       "last refresh will be missing."),
        ],
    ),

    # --- Publish (rebuild a derived read store; offline, no API quota) ------
    Command(
        key="publish", group="Publish", title="Publish serve.db (for the Viewer)",
        module="web.publish",
        summary="Clone a small, current slice of football.db into web/serve.db.",
        detail="Zero-API DB→DB copy (ADR 0023): all competitions + all players/careers "
               "+ a rolling window of fixtures/events, written to serve.db.tmp then "
               "atomically swapped in. This is what the Viewer app reads; football.db "
               "is never read by a web UI. The nightly Refresh runs this as its last step.",
        example="After a Refresh (or Rebuild) updates football.db, run this to refresh the "
                "Viewer: it copies every tracked Competition, every player Bio + Career Stint, "
                "and the −3..+10 day fixture/Event window into web/serve.db — seconds, no API "
                "calls. The Viewer at http://127.0.0.1:8001 then serves the new data.",
        scope="football.db → serve.db · offline",
        params=[
            Param("before", "Days of past fixtures", "int", "--before", default=3,
                  placeholder="3", advanced=True,
                  help="How many days of already-played fixtures to include (recent results). "
                       "Default 3."),
            Param("after", "Days of upcoming fixtures", "int", "--after", default=10,
                  placeholder="10", advanced=True,
                  help="How many days of upcoming fixtures to include (the slate). Default 10."),
        ],
    ),
    Command(
        key="publish_pg", group="Publish", title="Publish the Postgres Published Store",
        module="football.publish.pg",
        summary="Rebuild the remote Postgres replica: selected Competitions + all commentary.",
        detail="Zero-API re-parse of the chosen Competitions from the raw cache, plus a clone "
               "of the whole ESPN commentary store, bulk-loaded into Postgres and swapped "
               "over `public` in one transaction (ADR 0027). REPLACES the Published Store "
               "wholesale — the football tables end up holding exactly the Competitions you "
               "pick and nothing else, so leaving one out REMOVES it. The commentary tables "
               "are always copied in FULL: a Narrated Match is ESPN's unit and is not "
               "Competition-scoped (most narrate leagues we do not collect at all). Never "
               "merges — Venue ids and Event indexes are re-derived every build, so an upsert "
               "would corrupt them. Touches no local store: football.db, serve.db, "
               "commentary.db and the raw cache are all read-only or untouched.",
        example="Leave the picker empty → publishes the default pair, Liga MX + Major League "
                "Soccer (~498k rows), plus every Narrated Match and Commentary Line, into the "
                "remote Postgres `football` database (~2 min, no API quota). Readers keep "
                "querying the previous copy until the final swap, then see the new one — never "
                "a half-loaded table. Pick Liga MX ALONE and the football half becomes Liga MX "
                "only: MLS is dropped. The commentary half is unaffected either way.",
        scope="selected competitions + all commentary → remote Postgres · offline",
        long_running=True,   # ~2 min for the default pair; minutes more per added competition
        params=[
            Param("league_ids", "Competitions to publish", "competitions", None,
                  help="The Published Store's FOOTBALL tables will hold EXACTLY these "
                       "Competitions — any you leave unselected are removed from Postgres. "
                       "Select none for the default pair (Liga MX + Major League Soccer). "
                       "Does not scope the commentary tables: those are always published in "
                       "full, so a narration may name a Fixture no longer in the store."),
        ],
    ),

    # --- Control (open a surface; populates no store) ------------------------
    Command(
        key="surfaces", group="Control", title="Open the local surfaces",
        module="surfaces",
        summary="Serve all three surfaces on one port: Viewer at /, Desk at /desk, "
                "Console at /console.",
        detail="Starts the composed app (ADR 0035) — a long-running server, not a job "
               "that finishes, so it stays 'running' until you Stop it. Populates no "
               "store itself: the Viewer reads serve.db + live.db, the Desk reads the "
               "Editorial and Published Stores, and the Console reads nothing at all. "
               "Note that this Console is *inside* what this launches: running it from "
               "here starts a second copy of all three, which on the default port will "
               "just fail with 'address already in use'.",
        example="Start it, then open http://127.0.0.1:8001 for this week's fixtures and "
                "the tracked Competitions, /desk to see tonight's Drafting Candidates and "
                "fire the match-report pipeline, /console for these triggers. One process, "
                "one port; the header switches between them. Bound to 127.0.0.1 — between "
                "them these surfaces spend API-Football quota and Anthropic tokens, write "
                "to the Editorial Store, and can rebuild football.db.",
        scope="local web surfaces · no store written",
        long_running=True,   # a server: runs until stopped
        params=[
            Param("port", "Port", "int", "--port", default=8001, placeholder="8001",
                  advanced=True,
                  help="Port to bind on 127.0.0.1. Default 8001 — the Viewer's old port, "
                       "kept so existing bookmarks survive. The Desk's former :8002 is "
                       "retired."),
        ],
    ),
]

# Lookup by key.
BY_KEY: dict[str, Command] = {c.key: c for c in COMMANDS}


def by_group() -> dict[str, list[Command]]:
    """Commands grouped for display, in GROUPS order."""
    return {g: [c for c in COMMANDS if c.group == g] for g in GROUPS}


def build_argv(cmd: Command, values: dict) -> list[str]:
    """Turn submitted form `values` into the exact argv a terminal user would type.

    Positionals are emitted after all optional flags so an `nargs="+"` positional
    (live poll's fixture ids) is not split by a trailing option. Runs in the same
    interpreter/venv as the app via `sys.executable`, so no dependency on `uv`
    being on PATH.
    """
    argv: list[str] = [sys.executable, "-m", cmd.module]
    positionals: list[str] = []
    for p in cmd.params:
        v = values.get(p.name)
        if p.kind == "bool":
            if _truthy(v):
                argv.append(p.flag)
            continue
        if p.kind in ("fixture", "competitions"):
            # Multi-select -> repeated positionals. Selecting none emits nothing, so the
            # command's own argparse default applies (publish_pg: the Liga MX + MLS pair).
            ids = v if isinstance(v, list) else ([v] if v not in (None, "") else [])
            for fid in ids:
                fid = str(fid).strip()
                if fid:
                    positionals.append(fid)
            continue
        # scalar int/str/competition
        if v is None or str(v).strip() == "":
            if p.required and p.flag is None:
                raise ValueError(f"{p.label} is required.")
            continue
        val = str(v).strip()
        if p.flag is None:
            positionals.append(val)
        else:
            argv.extend([p.flag, val])
    argv.extend(positionals)
    return argv


def _truthy(v) -> bool:
    return v in (True, "true", "True", "on", "1", 1)
