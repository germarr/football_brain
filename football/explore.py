"""Marimo notebook — explore La Liga & Liga MX (data/football.db).

Run with:  uv run marimo edit football/explore.py
      or:   uv run marimo run  football/explore.py   (read-only app)

Pick a Competition, Season, and Tournament (Liga MX splits into Apertura /
Clausura; La Liga has a single Regular Season). Reads Layer 2 (SQLite) only.
Age is derived at fixture date; Appearances are rows with minutes > 0; results
are relative to the row's own team; standings use regular-season matchdays only.
"""
import marimo

__generated_with = "0.23.13"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    import pandas as pd
    import altair as alt
    from pathlib import Path
    from sqlalchemy import create_engine

    try:
        base = Path(__file__).resolve().parent.parent
    except NameError:
        base = Path.cwd()
    engine = create_engine(f"sqlite:///{base / 'data' / 'football.db'}")

    players = pd.read_sql("SELECT * FROM player", engine)
    # Career Stints: full cross-competition team history per player (CONTEXT.md).
    player_teams = pd.read_sql(
        """
        SELECT pt.player_id, p.name AS player_name,
               pt.team_id, pt.team_name, pt.season
        FROM playerteam pt
        JOIN player p ON pt.player_id = p.id
        """,
        engine,
    )
    entries = pd.read_sql(
        """
        SELECT se.*, f.date AS fixture_date, f.season, f.matchday,
               f.league_name, f.tournament,
               f.home_team_id, f.home_team_name, f.away_team_name,
               f.home_goals, f.away_goals,
               p.name AS player_name, p.nationality, p.birth_date
        FROM squadentry se
        JOIN fixture f ON se.fixture_id = f.id
        JOIN player  p ON se.player_id  = p.id
        """,
        engine,
        parse_dates=["fixture_date", "birth_date"],
    )
    # Match-event timeline (ADR 0007): one row per goal/card/sub/VAR, scoped to the
    # same Competition/Season/Tournament via the fixture join. Only fixtures that
    # have been backfilled (football.collect_events) contribute rows, so a scope
    # can legitimately be empty — every events view below guards for that.
    events = pd.read_sql(
        """
        SELECT e.fixture_id, e.event_index, e.team_id, e.minute, e.extra,
               e.type, e.detail, e.player_id, e.assist_id, e.comments,
               f.season, f.league_name, f.tournament, f.date AS fixture_date,
               t.name AS team_name,
               p.name AS player_name, a.name AS assist_name
        FROM event e
        JOIN fixture f ON e.fixture_id = f.id
        JOIN team    t ON e.team_id    = t.id
        LEFT JOIN player p ON e.player_id = p.id
        LEFT JOIN player a ON e.assist_id = a.id
        """,
        engine,
        parse_dates=["fixture_date"],
    )
    return alt, entries, events, mo, pd, player_teams, players


@app.cell
def _(entries, pd):
    df = entries.copy()

    # Age at fixture date (CONTEXT.md: never a stored column).
    fx, bd = df["fixture_date"].dt, df["birth_date"].dt
    had_birthday = (fx.month > bd.month) | ((fx.month == bd.month) & (fx.day >= bd.day))
    df["age"] = (fx.year - bd.year) - (~had_birthday).astype("int64")

    # Team-relative view: everything is from the perspective of se.team_id.
    df["is_home"] = df["team_id"] == df["home_team_id"]
    df["team_name"] = df["home_team_name"].where(df["is_home"], df["away_team_name"])
    df["opponent"] = df["away_team_name"].where(df["is_home"], df["home_team_name"])
    df["team_goals"] = df["home_goals"].where(df["is_home"], df["away_goals"])
    df["opp_goals"] = df["away_goals"].where(df["is_home"], df["home_goals"])
    df["result"] = pd.cut(
        df["team_goals"] - df["opp_goals"],
        bins=[-99, -1, 0, 99], labels=["L", "D", "W"],
    ).astype(str)
    df["played"] = df["minutes"].fillna(0) > 0   # an Appearance
    df["scoreline"] = (
        df["team_goals"].astype("Int64").astype(str) + "–" +
        df["opp_goals"].astype("Int64").astype(str)
    )

    _cal_year_leagues = {"Brasileirão"}   # calendar-year seasons -> "2024", not "2024/25"

    def season_label(s, league=None):
        if league in _cal_year_leagues:
            return str(s)
        return f"{s}/{str(s + 1)[2:]}"

    return df, season_label


@app.cell
def _(mo):
    mo.md(
        """
        # ⚽ Multi-league explorer

        La Liga · Premier League · Serie A · Bundesliga · Brasileirão · Liga MX.
        Full dataset built from the raw API cache. Choose a **competition**,
        **season**, and **tournament** below — Liga MX splits into *Apertura* and
        *Clausura*, each its own championship. Everything reacts to the selection.
        """
    )
    return


@app.cell
def _(df, mo):
    competition_select = mo.ui.dropdown(
        options=sorted(df["league_name"].unique()),
        value="La Liga" if "La Liga" in df["league_name"].values else None,
        label="Competition",
    )
    return (competition_select,)


@app.cell
def _(competition_select, df, mo, season_label):
    _comp = competition_select.value
    _seasons = sorted(df[df["league_name"] == _comp]["season"].unique())
    season_select = mo.ui.dropdown(
        options={season_label(s, _comp): s for s in _seasons},
        value=season_label(_seasons[-1], _comp),
        label="Season",
    )
    return (season_select,)


@app.cell
def _(competition_select, df, mo, season_select):
    _tourns = sorted(
        df[(df["league_name"] == competition_select.value)
           & (df["season"] == season_select.value)]["tournament"].unique()
    )
    tournament_select = mo.ui.dropdown(
        options=_tourns, value=_tourns[0], label="Tournament",
    )
    return (tournament_select,)


@app.cell
def _(competition_select, mo, season_select, tournament_select):
    mo.hstack([competition_select, season_select, tournament_select],
              justify="start", gap=1)
    return


@app.cell
def _(competition_select, df, season_select, tournament_select):
    scope = df[
        (df["league_name"] == competition_select.value)
        & (df["season"] == season_select.value)
        & (df["tournament"] == tournament_select.value)
    ]
    return (scope,)


@app.cell
def _(mo, tournament_select):
    mo.md(f"## League table — {tournament_select.value} "
          "*(regular-season matchdays only)*")
    return


@app.cell
def _(mo, scope):
    regular = scope[scope["matchday"].notna()]          # exclude playoff fixtures
    matches = regular.drop_duplicates(["fixture_id", "team_id"])
    table = (
        matches.assign(
            pts=matches["result"].map({"W": 3, "D": 1, "L": 0}),
            win=matches["result"].eq("W"),
            draw=matches["result"].eq("D"),
            loss=matches["result"].eq("L"),
        )
        .groupby("team_name")
        .agg(
            P=("fixture_id", "nunique"), W=("win", "sum"), D=("draw", "sum"),
            L=("loss", "sum"), GF=("team_goals", "sum"), GA=("opp_goals", "sum"),
            Pts=("pts", "sum"),
        )
        .reset_index()
    )
    table["GD"] = table["GF"] - table["GA"]
    table = (table.sort_values(["Pts", "GD", "GF"], ascending=False)
             .reset_index(drop=True))
    table.index = table.index + 1
    mo.ui.table(table[["team_name", "P", "W", "D", "L", "GF", "GA", "GD", "Pts"]],
                selection=None, pagination=True)
    return


@app.cell
def _(mo):
    mo.md("## Top scorers")
    return


@app.cell
def _(alt, scope):
    scorers = (
        scope.groupby(["player_name", "team_name"])["goals"].sum()
        .reset_index().sort_values("goals", ascending=False).head(15)
    )
    scorers["label"] = scorers["player_name"] + " (" + scorers["team_name"] + ")"
    chart = (
        alt.Chart(scorers)
        .mark_bar(color="#A50044")
        .encode(
            x=alt.X("goals:Q", title="Goals"),
            y=alt.Y("label:N", sort="-x", title=None),
            tooltip=["player_name", "team_name", "goals"],
        )
        .properties(height=420, title="Top 15 scorers")
    )
    chart
    return


@app.cell
def _(competition_select, events, season_select, tournament_select):
    events_scope = events[
        (events["league_name"] == competition_select.value)
        & (events["season"] == season_select.value)
        & (events["tournament"] == tournament_select.value)
    ]
    # Actual scored goals exclude "Missed Penalty" (the provider files a missed
    # spot-kick under type=Goal); count it as a shot, not a goal (ADR 0007).
    scored_goals = events_scope[
        (events_scope["type"] == "Goal")
        & (events_scope["detail"] != "Missed Penalty")
    ]
    return events_scope, scored_goals


@app.cell
def _(mo):
    mo.md(
        """
        ## Match events — timeline

        Goals, cards, substitutions and VAR calls with the **minute** they
        happened, the **goal type** (normal / penalty / own goal), the **assist**
        behind each goal, and **added time**. Requires the events backfill
        (`uv run python -m football.collect_events`, then rebuild with
        `python -m football.parse`); scopes without cached events show a note.
        """
    )
    return


@app.cell
def _(events_scope, mo, scored_goals):
    mo.stop(
        events_scope.empty,
        mo.callout(
            mo.md(
                "**No events cached for this competition / season / tournament "
                "yet.** Run `uv run python -m football.collect_events` to backfill "
                "the timeline, then rebuild with `uv run python -m football.parse`."
            ),
            kind="info",
        ),
    )
    _pens = scored_goals[scored_goals["detail"] == "Penalty"]
    _own = scored_goals[scored_goals["detail"] == "Own Goal"]
    _stoppage = scored_goals[scored_goals["extra"].notna()]
    _cards = events_scope[events_scope["type"] == "Card"]
    _yellow = _cards[_cards["detail"].str.contains("Yellow", na=False)]
    _red = _cards[_cards["detail"].str.contains("Red", na=False)]
    mo.hstack(
        [
            mo.stat(len(scored_goals), label="Goals",
                    caption=f"in {events_scope['fixture_id'].nunique()} fixtures"),
            mo.stat(len(_pens), label="Penalties"),
            mo.stat(len(_own), label="Own goals"),
            mo.stat(len(_stoppage), label="Stoppage-time goals"),
            mo.stat(len(_yellow), label="Yellow cards"),
            mo.stat(len(_red), label="Red cards"),
        ],
        justify="start", gap=1,
    )
    return


@app.cell
def _(alt, mo, scored_goals):
    mo.stop(scored_goals.empty, mo.md("_No goals in this scope yet._"))
    _hist = (
        alt.Chart(scored_goals)
        .mark_bar()
        .encode(
            x=alt.X("minute:Q", bin=alt.Bin(maxbins=18), title="Match minute"),
            y=alt.Y("count():Q", title="Goals"),
            color=alt.Color("detail:N", title="Goal type"),
            tooltip=["detail:N", "count():Q"],
        )
        .properties(height=300, title="When goals are scored (by minute and type)")
    )
    _hist
    return


@app.cell
def _(alt, events_scope, mo):
    _cards = events_scope[events_scope["type"] == "Card"].copy()
    mo.stop(_cards.empty, mo.md("_No cards in this scope yet._"))
    # Fold anything that isn't a clean Red into the Yellow bucket (second yellows,
    # provider quirks) so the colour split stays binary.
    _cards["card"] = _cards["detail"].where(
        _cards["detail"].str.contains("Red", na=False), "Yellow Card"
    ).where(~_cards["detail"].str.contains("Red", na=False), "Red Card")
    _order = (_cards.groupby("player_name").size()
              .sort_values(ascending=False).head(15).index.tolist())
    _top = (_cards[_cards["player_name"].isin(_order)]
            .groupby(["player_name", "card"]).size().reset_index(name="n"))
    _chart = (
        alt.Chart(_top)
        .mark_bar()
        .encode(
            x=alt.X("n:Q", title="Cards"),
            y=alt.Y("player_name:N", sort=_order, title=None),
            color=alt.Color("card:N", title=None,
                            scale=alt.Scale(domain=["Yellow Card", "Red Card"],
                                            range=["#E1C340", "#C0202A"])),
            tooltip=["player_name", "card", "n"],
        )
        .properties(height=420, title="Most-booked players")
    )
    _chart
    return


@app.cell
def _(mo, scored_goals):
    _late = scored_goals[scored_goals["extra"].notna()].copy()
    mo.stop(_late.empty, mo.md("_No stoppage-time goals in this scope yet._"))
    _late["when"] = (_late["minute"].astype("Int64").astype(str) + "+"
                     + _late["extra"].astype("Int64").astype(str) + "'")
    _late["date"] = _late["fixture_date"].dt.date
    _view = (_late[["date", "team_name", "player_name", "assist_name", "detail", "when"]]
             .rename(columns={"team_name": "team", "player_name": "scorer",
                              "assist_name": "assist", "detail": "type"})
             .sort_values(["date", "when"]))
    mo.vstack([mo.md("### Stoppage-time goals *(added-time drama)*"),
               mo.ui.table(_view, selection=None, pagination=True)])
    return


@app.cell
def _(mo):
    mo.md("## Team focus")
    return


@app.cell
def _(mo, scope):
    _teams = sorted(scope["team_name"].unique())
    _default = "Barcelona" if "Barcelona" in _teams else _teams[0]
    team_select = mo.ui.dropdown(options=_teams, value=_default, label="Team")
    team_select
    return (team_select,)


@app.cell
def _(mo, scope, team_select):
    sel = scope[scope["team_name"] == team_select.value]
    tm = sel.drop_duplicates("fixture_id")
    rec = tm["result"].value_counts()
    apps = sel[sel["played"]]
    mo.hstack(
        [
            mo.stat(len(tm), label="Matches"),
            mo.stat(f"{rec.get('W', 0)}-{rec.get('D', 0)}-{rec.get('L', 0)}", label="W–D–L"),
            mo.stat(int(tm["team_goals"].sum()), label="Goals for",
                    caption=f"{int(tm['opp_goals'].sum())} against"),
            mo.stat(apps["player_id"].nunique(), label="Players used"),
            mo.stat(f"{apps['age'].mean():.1f}" if len(apps) else "—",
                    label="Avg age", caption="by appearance"),
        ],
        justify="start", gap=1,
    )
    return


@app.cell
def _(mo, scope, team_select):
    roster = (
        scope[(scope["team_name"] == team_select.value) & scope["played"]]
        ["player_name"].sort_values().unique().tolist()
    )
    player_select = mo.ui.dropdown(options=roster, value=roster[0] if roster else None,
                                   label="Player")
    player_select
    return (player_select,)


@app.cell
def _(mo, player_select, scope, team_select):
    log = (
        scope[(scope["team_name"] == team_select.value)
              & (scope["player_name"] == player_select.value)]
        .sort_values("fixture_date")
    )
    played = log[log["played"]]
    header = mo.hstack(
        [
            mo.stat(int(log["played"].sum()), label="Apps",
                    caption=f"{int((log['status'] == 'started').sum())} starts"),
            mo.stat(int(played["minutes"].sum()), label="Minutes"),
            mo.stat(int(log["goals"].sum()), label="Goals"),
            mo.stat(int(log["assists"].sum()), label="Assists"),
            mo.stat(f"{played['rating'].mean():.2f}" if len(played) else "—",
                    label="Avg rating"),
        ],
        justify="start", gap=1,
    )
    view = (
        log[["fixture_date", "matchday", "opponent", "is_home", "scoreline",
             "result", "status", "minutes", "goals", "assists", "rating", "age"]]
        .assign(fixture_date=lambda d: d["fixture_date"].dt.date,
                venue=lambda d: d["is_home"].map({True: "H", False: "A"}))
        .drop(columns="is_home")
    )
    mo.vstack([header, mo.md(f"### {player_select.value} — match log"),
               mo.ui.table(view, selection=None)])
    return


@app.cell
def _(mo):
    mo.md("### Squad usage — minutes shared")
    return


@app.cell
def _(alt, scope, team_select):
    usage = (
        scope[(scope["team_name"] == team_select.value) & scope["played"]]
        .groupby("player_name")
        .agg(minutes=("minutes", "sum"), apps=("fixture_id", "nunique"),
             goals=("goals", "sum"), assists=("assists", "sum"))
        .reset_index().sort_values("minutes", ascending=False)
    )
    chart_min = (
        alt.Chart(usage)
        .mark_bar(color="#004D98")
        .encode(
            x=alt.X("minutes:Q", title="Minutes played"),
            y=alt.Y("player_name:N", sort="-x", title=None),
            tooltip=["player_name", "apps", "goals", "assists", "minutes"],
        )
        .properties(height=560, title="Total minutes by player")
    )
    chart_min
    return


@app.cell
def _(mo):
    mo.md(
        """
        ## Cross-competition comparison

        Compare any players across **La Liga and Liga MX**. Raw totals are unfair
        across a 38-game league and a 17-game tournament, so output is shown
        **per 90 minutes** — the honest cross-competition metric.
        """
    )
    return


@app.cell
def _(df, mo):
    idx = (
        df[df["played"]]
        .groupby("player_id")
        .agg(
            name=("player_name", "first"),
            team=("team_name", lambda s: s.mode().iat[0] if not s.mode().empty else s.iat[0]),
            comps=("league_name", lambda s: "/".join(sorted(s.unique()))),
            goals=("goals", "sum"),
        )
        .reset_index()
    )
    idx["label"] = idx["name"] + " · " + idx["team"] + " (" + idx["comps"] + ")"
    player_options = dict(zip(idx["label"], idx["player_id"]))
    player_meta = idx.set_index("player_id")[["name", "comps"]].to_dict("index")

    # Default to a genuinely cross-competition pair: each league's top scorer.
    laliga = idx[idx["comps"].str.contains("La Liga")].sort_values("goals", ascending=False)
    ligamx = idx[idx["comps"].str.contains("Liga MX")].sort_values("goals", ascending=False)
    default = [lbl.iloc[0] for lbl in (laliga["label"], ligamx["label"]) if len(lbl)]

    comparison_select = mo.ui.multiselect(
        options=player_options, value=default, label="Players to compare",
    )
    comparison_select
    return comparison_select, player_meta


@app.cell
def _(alt, comparison_select, df, mo, player_meta):
    _ids = comparison_select.value
    mo.stop(not _ids, mo.md("_Pick one or more players above to compare._"))

    _cmp = df[df["player_id"].isin(_ids) & df["played"]]
    _agg = (
        _cmp.groupby("player_id")
        .agg(apps=("fixture_id", "nunique"), minutes=("minutes", "sum"),
             goals=("goals", "sum"), assists=("assists", "sum"),
             rating=("rating", "mean"))
        .reset_index()
    )
    _agg["player"] = _agg["player_id"].map(lambda p: player_meta[p]["name"])
    _agg["comps"] = _agg["player_id"].map(lambda p: player_meta[p]["comps"])
    _agg["G+A"] = _agg["goals"] + _agg["assists"]
    for _col, _src in [("G/90", "goals"), ("A/90", "assists"), ("G+A/90", "G+A")]:
        _agg[_col] = (_agg[_src] / _agg["minutes"] * 90).round(2)
    _agg["rating"] = _agg["rating"].round(2)

    _show = _agg[["player", "comps", "apps", "minutes", "goals", "assists",
                  "G+A", "G/90", "A/90", "G+A/90", "rating"]]
    _long = _agg.melt(id_vars="player", value_vars=["G/90", "A/90", "G+A/90"],
                      var_name="metric", value_name="per90")
    _chart = (
        alt.Chart(_long)
        .mark_bar()
        .encode(
            x=alt.X("player:N", title=None, axis=alt.Axis(labelAngle=0)),
            xOffset="metric:N",
            y=alt.Y("per90:Q", title="Per 90 minutes"),
            color=alt.Color("metric:N", title=None),
            tooltip=["player", "metric", "per90"],
        )
        .properties(height=320,
                    title="Output per 90 — normalized for fair cross-competition comparison")
    )
    mo.vstack([mo.ui.table(_show, selection=None), _chart])
    return


@app.cell
def _(comparison_select, df, mo, player_meta, season_label):
    _ids = comparison_select.value
    mo.stop(not _ids, None)

    _cmp = df[df["player_id"].isin(_ids) & df["played"]]
    _split = (
        _cmp.groupby(["player_id", "league_name", "season", "tournament"])
        .agg(apps=("fixture_id", "nunique"), minutes=("minutes", "sum"),
             goals=("goals", "sum"), assists=("assists", "sum"))
        .reset_index()
    )
    _split["player"] = _split["player_id"].map(lambda p: player_meta[p]["name"])
    _split["scope"] = (
        _split["league_name"] + " "
        + _split.apply(lambda r: season_label(r["season"], r["league_name"]), axis=1)
        + " · " + _split["tournament"]
    )
    _split["G+A/90"] = ((_split["goals"] + _split["assists"]) / _split["minutes"] * 90).round(2)
    _view = (_split[["player", "scope", "apps", "minutes", "goals", "assists", "G+A/90"]]
             .sort_values(["player", "scope"]))
    mo.vstack([
        mo.md("### Per-tournament splits *(Apertura vs Clausura, league vs league)*"),
        mo.ui.table(_view, selection=None, pagination=True),
    ])
    return


if __name__ == "__main__":
    app.run()
