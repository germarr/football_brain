import marimo

__generated_with = "0.23.13"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    import re
    import pandas as pd
    import altair as alt
    import plotly.graph_objects as go
    from pathlib import Path
    import duckdb

    # The notebook's depth below the project root is not fixed, so we can't
    # number of .parent hops to the project root. Walk up from the file (or the
    # CWD when __file__ is undefined) until we find data/football.db. We use the
    # full modeled store — not liga-mx.db — because a League of Origin is derived
    # by resolving a prior club's team id against the fixtures of every collected
    # Competition, and only football.db holds them all (CONTEXT.md: League of
    # Origin). football.db is a superset of liga-mx.db, so the Liga MX subject
    # data is identical.
    try:
        start = Path(__file__).resolve().parent
    except NameError:
        start = Path.cwd()

    db_name = "football.db"
    db_path = None
    for p in (start, *start.parents):
        candidate = p / "data" / db_name
        if candidate.exists():
            db_path = candidate
            break
    if db_path is None:
        raise FileNotFoundError(f"Could not locate data/{db_name} walking up from {start}")

    conn = duckdb.connect(str(db_path), read_only=True)

    # The target Competition / Season / Tournament, and the two derived buckets
    # that are NOT leagues (CONTEXT.md: League of Origin).
    LEAGUE_ID, SEASON, TOURNAMENT = 262, 2025, "Clausura"
    HOMEGROWN = "Liga MX — homegrown/debut"
    UNKNOWN = "Unknown"
    return (
        HOMEGROWN,
        LEAGUE_ID,
        SEASON,
        TOURNAMENT,
        UNKNOWN,
        alt,
        conn,
        go,
        mo,
        pd,
        re,
    )


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    # Liga MX — Clausura 2025: where the players came from

    Two questions about the players who **made an Appearance** in **Liga MX,
    Clausura 2025** — i.e. actually took the field (`minutes > 0`) in a fixture
    of that tournament; players who only ever sat as unused substitutes are
    excluded:

    1. **League of Origin** — the domestic league of the club each player
       joined their current Liga MX club *from* — shown as a Sankey.
    2. **Nationality** — which footballing nations they represent.

    ### How "League of Origin" is derived

    A Career Stint carries no league, only a team id. So for each player we
    take their **current Liga MX club** (the club they logged the most
    minutes for this tournament), find the earliest Career-Stint season at
    that club (their *join season*), and pick the **club held at the latest
    season at or before that join** as the source. That club's team id is
    resolved to the **domestic league** it appears in most across the whole
    store; continental cups never count, and **national teams are excluded**
    (clubs only).

    Two origins are not leagues and are shown as their own nodes:
    **homegrown/debut** (a player whose only club is their current one) and
    **Unknown** (a prior club in a league we don't collect, or seen only in a
    cup).
    """)
    return


@app.cell
def _(LEAGUE_ID, SEASON, TOURNAMENT, conn):
    # Population + destination: one current Liga MX club per player, taken as the
    # club they logged the most minutes for in this tournament (settles the ~6
    # players who appeared for two clubs after a mid-season move).
    # Population is restricted to Appearances (minutes > 0, CONTEXT.md) — a player
    # who was only ever an unused sub is excluded, dropping the fringe/debutant
    # squad members the provider has no biography for.
    current_club = conn.execute(
        """
        select se.player_id,
               se.team_id            as current_team_id,
               t.name                as current_team_name,
               sum(se.minutes)       as minutes
        from squadentry se
        join fixture f on se.fixture_id = f.id
        join team   t on se.team_id = t.id
        where f.league_id = ? and f.season = ? and f.tournament = ?
          and se.minutes > 0
        group by 1, 2, 3
        """,
        [LEAGUE_ID, SEASON, TOURNAMENT],
    ).df()
    current_club = (
        current_club.sort_values(["player_id", "minutes"], ascending=[True, False])
        .drop_duplicates("player_id")[
            ["player_id", "current_team_id", "current_team_name"]
        ]
    )
    return (current_club,)


@app.cell
def _(conn):
    # team id -> its domestic league: the league-type Competition it appears in
    # most across every fixture in the store. Cups are excluded, so a club seen
    # only in (say) the Libertadores has no row here and resolves to Unknown.
    team_league_df = conn.execute(
        """
        with app as (
            select home_team_id as team_id, league_id from fixture
            union all
            select away_team_id as team_id, league_id from fixture
        ), counts as (
            select a.team_id, c.name as league_name, count(*) as n
            from app a
            join competition c on a.league_id = c.id
            where c.type = 'league'
            group by 1, 2
        ), ranked as (
            select team_id, league_name,
                   row_number() over (
                       partition by team_id order by n desc, league_name
                   ) as rk
            from counts
        )
        select team_id, league_name from ranked where rk = 1
        """
    ).df()
    team_league = dict(zip(team_league_df.team_id, team_league_df.league_name))
    return (team_league,)


@app.cell
def _(conn, re):
    # National teams to exclude from career history: any team that appears in an
    # international Competition (World Cup = 1, Copa America = 9), plus a name
    # regex for the youth / women's sides those senior-only lists miss
    # (e.g. "Mexico U20", "Colombia U17").
    national_ids = set(
        conn.execute(
            """
            select home_team_id as team_id from fixture where league_id in (1, 9)
            union
            select away_team_id as team_id from fixture where league_id in (1, 9)
            """
        ).df().team_id.tolist()
    )
    youth_re = re.compile(r"\bU\d{2}\b| W$|Women", re.I)
    return national_ids, youth_re


@app.cell
def _(
    HOMEGROWN,
    SEASON,
    UNKNOWN,
    conn,
    current_club,
    national_ids,
    pd,
    team_league,
    youth_re,
):
    # Career stints for our players, national teams stripped out (clubs only).
    conn.register("cc", current_club)
    stints = conn.execute(
        """
        select p.player_id, p.team_id, p.season, p.team_name
        from playerteam p
        where p.player_id in (select player_id from cc)
        """
    ).df()
    _club = stints[
        ~(
            stints.team_id.isin(national_ids)
            | stints.team_name.astype(str).str.contains(youth_re)
        )
    ].merge(current_club[["player_id", "current_team_id"]], on="player_id")

    def _origin_for(g):
        cur = g.current_team_id.iloc[0]
        at_current = g.loc[g.team_id == cur, "season"]
        join_season = at_current.min() if len(at_current) else SEASON
        prior = g[(g.team_id != cur) & (g.season <= join_season)]
        if prior.empty:
            return HOMEGROWN  # only club history is their current club
        src = prior.loc[prior.season.idxmax()]  # club held just before the join
        return team_league.get(src.team_id, UNKNOWN)

    origin = (
        _club.groupby("player_id")
        .apply(_origin_for, include_groups=False)
        .rename("origin_league")
        .reset_index()
    )
    # A player with no surviving club stint (all national-team rows) is homegrown.
    _missing = set(current_club.player_id) - set(origin.player_id)
    if _missing:
        origin = pd.concat(
            [origin, pd.DataFrame({"player_id": list(_missing),
                                   "origin_league": HOMEGROWN})],
            ignore_index=True,
        )
    return (origin,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 1 · League of Origin → Liga MX
    """)
    return


@app.cell
def _(go, origin):
    # Fan-in Sankey: every source league flows into the single "Liga MX" node,
    # width = number of players. Nodes ordered by volume, count shown in-label.
    _dist = origin.origin_league.value_counts().sort_values(ascending=False)
    _sources = list(_dist.index)
    _labels = [f"{lg} ({_dist[lg]})" for lg in _sources] + [f"Liga MX ({int(_dist.sum())})"]
    _target = len(_sources)
    sankey = go.Figure(
        go.Sankey(
            node=dict(label=_labels, pad=14, thickness=16,
                      line=dict(color="rgba(0,0,0,0.25)", width=0.5)),
            link=dict(
                source=list(range(len(_sources))),
                target=[_target] * len(_sources),
                value=[int(_dist[lg]) for lg in _sources],
            ),
        )
    )
    sankey.update_layout(
        title_text="Liga MX Clausura 2025 — League of Origin (players)",
        font_size=12, height=650,
    )
    sankey
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## 2 · Nationalities of the players
    """)
    return


@app.cell
def _(alt, conn):
    nationalities = conn.execute(
        """
        select coalesce(nationality, '(unknown)') as nationality,
               count(*) as players
        from player
        where id in (select player_id from cc)
        group by 1
        order by 2 desc
        """
    ).df()
    chart = (
        alt.Chart(nationalities)
        .mark_bar()
        .encode(
            x=alt.X("players:Q", title="Players"),
            y=alt.Y("nationality:N", sort="-x", title=None),
            tooltip=["nationality", "players"],
        )
        .properties(
            title="Liga MX Clausura 2025 — players by nationality",
            width=520,
            height=alt.Step(18),
        )
    )
    chart
    return


if __name__ == "__main__":
    app.run()
