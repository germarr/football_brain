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
    import duckdb

    # Create a DuckDB connection

    # The notebook lives at football/notebooks/, so we can't hard-code a fixed
    # number of .parent hops to the project root. Walk up from the file (or the
    # CWD when __file__ is undefined) until we find data/football.db.
    try:
        start = Path(__file__).resolve().parent
    except NameError:
        start = Path.cwd()

    db_name = "liga-mx.db"

    db_path = None
    for p in (start, *start.parents):
        candidate = p / "data" / db_name
        if candidate.exists():
            db_path = candidate
            break

    if db_path is None:
        raise FileNotFoundError(
            f"Could not locate data/football.db walking up from {start}"
        )

    engine = create_engine(f"sqlite:///{db_path}")
    return db_path, duckdb, mo


@app.cell
def _(db_path, duckdb):
    conn = duckdb.connect(db_path)
    return (conn,)


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## Finals Result - Flattened Query Explanation

    The query above flattens the 2-row finals result into a single row with the following logic:

    ### How it works:

    1. **Normalize Teams**: Since the finals consist of two legs (one in each team's stadium), we normalize the team names so that `team_1` is always lexicographically smaller than `team_2`. This ensures consistent pairing.

    2. **Track Orientation**: For each match, we track whether `team_1` is playing at home or away (`team_1_is_away`). This is crucial for the away goals rule.

    3. **Aggregate Goals**:
       - `team_1_total_goals` and `team_2_total_goals`: Sum of goals across both legs
       - `team_1_away_goals` and `team_2_away_goals`: Goals scored while playing away

    4. **Winner Determination** (applied in this order):
       - ✅ **Most Goals**: Team with more total goals wins
       - 🥅 **Penalties**: If goals are tied, the team that won the penalty shootout wins
       - 🚗 **Away Goals Rule**: If still tied and no penalties, the team with more away goals wins
       - 🤝 **Draw**: If all conditions are equal

    ### Result Columns:
    - `season` & `tournament`: Season and tournament details
    - `team_1` & `team_2`: The two finalist teams (team_1 is alphabetically first)
    - `overall_score`: Score formatted as "X-Y"
    - `team_1_total_goals` & `team_2_total_goals`: Total goals in the final
    - `team_1_away_goals` & `team_2_away_goals`: Away goals scored
    - `team_1_penalties` & `team_2_penalties`: Penalty shootout results (if applicable)
    - `winner`: Winner determination with rule applied (e.g., "Cruz Azul (Goals)", "Team A (Penalties)", "Team B (Away Goals)")
    - `final_date`: Date of the final match
    """)
    return


@app.cell
def _(conn):
    # Flattened Finals with Winner Determination
    # This takes the last 2 matches (finals) and combines them into a single row
    df_finals_flattened = conn.execute("""
    WITH finals_raw AS (
        -- Get the last 2 matches for each season/tournament
        WITH ranked_matches AS (
            SELECT *,
            ROW_NUMBER() OVER (PARTITION BY season, tournament ORDER BY date DESC) as match_rank
            FROM fixture 
        )
        SELECT date, season, tournament, home_team_name, away_team_name, home_goals, away_goals,
            penalty_home, penalty_away, match_rank
        FROM ranked_matches
        WHERE match_rank <= 2
    ),
    -- Normalize teams so team_1 is always lexicographically smaller
    finals_normalized AS (
        SELECT 
            season,
            tournament,
            CASE WHEN home_team_name < away_team_name 
                 THEN home_team_name ELSE away_team_name END as team_1,
            CASE WHEN home_team_name < away_team_name 
                 THEN away_team_name ELSE home_team_name END as team_2,
            -- Goals scored by team_1 in this match
            CASE WHEN home_team_name < away_team_name 
                 THEN home_goals ELSE away_goals END as team_1_goals,
            -- Goals scored by team_2 in this match
            CASE WHEN home_team_name < away_team_name 
                 THEN away_goals ELSE home_goals END as team_2_goals,
            -- Penalties for team_1
            CASE WHEN home_team_name < away_team_name 
                 THEN penalty_home ELSE penalty_away END as team_1_penalties,
            -- Penalties for team_2
            CASE WHEN home_team_name < away_team_name 
                 THEN penalty_away ELSE penalty_home END as team_2_penalties,
            -- Whether team_1 is playing away in this match (0 = home, 1 = away)
            CASE WHEN home_team_name < away_team_name 
                 THEN 0 ELSE 1 END as team_1_is_away,
            date
        FROM finals_raw
    ),
    -- Aggregate both legs of the final
    finals_aggregated AS (
        SELECT 
            season,
            tournament,
            team_1,
            team_2,
            SUM(team_1_goals) as team_1_total_goals,
            SUM(team_2_goals) as team_2_total_goals,
            -- Away goals for team_1 (when team_1_is_away = 1)
            SUM(CASE WHEN team_1_is_away = 1 THEN team_1_goals ELSE 0 END) as team_1_away_goals,
            -- Away goals for team_2 (when team_1_is_away = 0, team_2 is away)
            SUM(CASE WHEN team_1_is_away = 0 THEN team_2_goals ELSE 0 END) as team_2_away_goals,
            -- Penalties (taking max in case there are multiple rows)
            MAX(team_1_penalties) as team_1_penalties,
            MAX(team_2_penalties) as team_2_penalties,
            MAX(date) as final_date
        FROM finals_normalized
        GROUP BY season, tournament, team_1, team_2
    ),
    final_step as (SELECT 
        season,
        tournament,
        team_1,
        team_2,
        team_1_total_goals || '-' || team_2_total_goals as overall_score,
        team_1_total_goals,
        team_2_total_goals,
        team_1_away_goals,
        team_2_away_goals,
        team_1_penalties,
        team_2_penalties,
        CASE 
            -- Team 1 wins on goals
            WHEN team_1_total_goals > team_2_total_goals THEN team_1 || ' (Goals)'
            -- Team 2 wins on goals
            WHEN team_2_total_goals > team_1_total_goals THEN team_2 || ' (Goals)'
            -- Goals are tied, check penalties
            WHEN team_1_penalties IS NOT NULL AND team_1_penalties > team_2_penalties 
                THEN team_1 || ' (Penalties)'
            WHEN team_2_penalties IS NOT NULL AND team_2_penalties > team_1_penalties 
                THEN team_2 || ' (Penalties)'
            -- Still tied, check away goals rule
            WHEN team_1_away_goals > team_2_away_goals 
                THEN team_1 || ' (Away Goals)'
            WHEN team_2_away_goals > team_1_away_goals 
                THEN team_2 || ' (Away Goals)'
            -- Complete draw
            ELSE 'Draw'
        END as winner,
        final_date
    FROM finals_aggregated
    ORDER BY season DESC, tournament DESC)

    SELECT season, tournament,team_1,team_2, overall_score, winner FROM final_step

    """).df()

    df_finals_flattened
    return


@app.cell
def _(conn):
    conn.execute("""
        WITH games_selected as (
        SELECT DISTINCT id,tournament,season FROM fixture 
        WHERE matchday IS NOT NULL
        ),
        full_df as (SELECT squadentry.player_id, squadentry.team_id,squadentry.position,
        team.name as team_name,player.name as player_name,player.nationality as player_nationality,
        games_selected.tournament,games_selected.season,
        SUM(goals) goals,
        RANK() OVER (PARTITION BY season, tournament ORDER BY SUM(goals) DESC) as player_rank
        FROM squadentry
        INNER JOIN games_selected ON games_selected.id = squadentry.fixture_id
        LEFT JOIN (SELECT * FROM team) as team ON team.id = squadentry.team_id
        LEFT JOIN (SELECT id,name,nationality FROM player) as player ON player.id = squadentry.player_id
        GROUP BY 1,2,3,4,5,6,7,8
        ORDER BY goals DESC)

    SELECT * FROM full_df
    WHERE player_rank = 1
    ORDER BY season DESC, tournament DESC
    """)\
    .df()
    return


@app.cell
def _(conn):
    df_nationality_goals = conn.execute("""
        WITH games_selected as (
        SELECT DISTINCT id,tournament,season FROM fixture 
        WHERE matchday IS NOT NULL
        ),
        full_df as (SELECT squadentry.player_id, squadentry.team_id,squadentry.position,
        team.name as team_name,player.name as player_name,player.nationality as player_nationality,
        games_selected.tournament,games_selected.season,
        SUM(goals) goals
        FROM squadentry
        INNER JOIN games_selected ON games_selected.id = squadentry.fixture_id
        LEFT JOIN (SELECT * FROM team) as team ON team.id = squadentry.team_id
        LEFT JOIN (SELECT id,name,nationality FROM player) as player ON player.id = squadentry.player_id
        GROUP BY 1,2,3,4,5,6,7,8
        ORDER BY goals DESC)

    SELECT * FROM (SELECT season,tournament, player_nationality,SUM(goals) as goals, COUNT(*) count_players,ROUND(SUM(goals)/COUNT(*),2) as avg_2,
    DENSE_RANK() OVER(PARTITION BY season, tournament ORDER BY SUM(goals) DESC) as rank
    FROM full_df
    GROUP BY 1,2,3
    ORDER BY season DESC, tournament)
    WHERE tournament = 'Clausura' and season =2025
    ORDER BY season DESC, tournament, rank
    """).df()
    return (df_nationality_goals,)


@app.cell
def _(df_nationality_goals):
    import plotly.graph_objects as go

    fig = go.Figure(data=[go.Pie(
        labels=df_nationality_goals['player_nationality'],
        values=df_nationality_goals['goals'],
        hole=0.4,  # This creates the donut shape
        hovertemplate='<b>%{label}</b><br>Goals: %{value}<br><extra></extra>',
        textposition='inside',
        textinfo='label+percent'
    )])

    fig.update_layout(
        title_text="Goals by Player Nationality - Clausura 2025",
        title_font_size=18,
        height=600,
        showlegend=True,
        legend=dict(
            orientation="v",
            yanchor="middle",
            y=0.5,
            xanchor="left",
            x=1.05
        )
    )

    fig
    return


@app.cell
def _(conn):
    df_nationality_position_index = conn.execute("""
        WITH games_selected as (
            SELECT DISTINCT id, tournament, season FROM fixture 
            WHERE matchday IS NOT NULL
        ),
        full_df as (
            SELECT 
                squadentry.player_id, 
                squadentry.team_id,
                squadentry.position,
                team.name as team_name,
                player.name as player_name,
                player.nationality as player_nationality,
                games_selected.tournament,
                games_selected.season,
                SUM(goals) goals
            FROM squadentry
            INNER JOIN games_selected ON games_selected.id = squadentry.fixture_id
            LEFT JOIN (SELECT * FROM team) as team ON team.id = squadentry.team_id
            LEFT JOIN (SELECT id, name, nationality FROM player) as player ON player.id = squadentry.player_id
            GROUP BY 1,2,3,4,5,6,7,8
        ),
        nationality_position_goals as (
            SELECT 
                player_nationality,
                position,
                SUM(goals) as goals_by_nat_pos,
                tournament,
                season
            FROM full_df
            GROUP BY player_nationality, position, tournament, season
        ),
        nationality_totals as (
            SELECT 
                player_nationality,
                SUM(goals_by_nat_pos) as total_goals_by_nat,
                tournament,
                season
            FROM nationality_position_goals
            GROUP BY player_nationality, tournament, season
        ),
        overall_position_distribution as (
            SELECT 
                position,
                SUM(goals_by_nat_pos) as total_goals_by_pos,
                tournament,
                season
            FROM nationality_position_goals
            GROUP BY position, tournament, season
        ),
        overall_totals as (
            SELECT 
                SUM(total_goals_by_nat) as total_goals,
                tournament,
                season
            FROM nationality_totals
            GROUP BY tournament, season
        )
        SELECT 
            n.player_nationality,
            n.position,
            n.goals_by_nat_pos,
            nt.total_goals_by_nat,
            ROUND(100.0 * n.goals_by_nat_pos / nt.total_goals_by_nat, 2) as pct_of_nat_goals,
            opd.total_goals_by_pos,
            ot.total_goals,
            ROUND(100.0 * opd.total_goals_by_pos / ot.total_goals, 2) as pct_of_overall_goals,
            ROUND((100.0 * n.goals_by_nat_pos / nt.total_goals_by_nat) / NULLIF(100.0 * opd.total_goals_by_pos / ot.total_goals, 0), 2) as overindex_ratio,
            n.tournament,
            n.season
        FROM nationality_position_goals n
        LEFT JOIN nationality_totals nt ON n.player_nationality = nt.player_nationality 
            AND n.tournament = nt.tournament 
            AND n.season = nt.season
        LEFT JOIN overall_position_distribution opd ON n.position = opd.position 
            AND n.tournament = opd.tournament 
            AND n.season = opd.season
        LEFT JOIN overall_totals ot ON n.tournament = ot.tournament 
            AND n.season = ot.season
        WHERE n.season = 2025 AND n.tournament = 'Clausura'
        ORDER BY n.player_nationality, overindex_ratio DESC
    """).df()

    df_nationality_position_index
    return (df_nationality_position_index,)


@app.cell
def _(df_nationality_position_index):
    import plotly.express as px

    # Create a pivot table for the heatmap
    heatmap_data = df_nationality_position_index.pivot_table(
        index='player_nationality',
        columns='position',
        values='overindex_ratio',
        aggfunc='first'
    )

    fig1 = px.imshow(
        heatmap_data,
        labels=dict(x="Position", y="Nationality", color="Overindex Ratio"),
        color_continuous_scale="RdYlGn",
        title="Nationality Overindexing by Position - Clausura 2025",
        height=600,
        width=900,
        text_auto='.2f',
        zmin=0.5,
        zmax=2
    )

    fig1.update_layout(
        xaxis_title="Position",
        yaxis_title="Player Nationality",
        coloraxis_colorbar=dict(title="Overindex<br>Ratio")
    )

    fig1
    return


@app.cell
def _(conn):
    conn.execute("""
        WITH games_selected as (
        SELECT DISTINCT id,tournament,season FROM fixture
        WHERE matchday IS NOT NULL
        ),
        full_df as (SELECT squadentry.player_id, squadentry.team_id,squadentry.position,
        team.name as team_name,player.name as player_name,player.nationality as player_nationality,
        games_selected.tournament,games_selected.season,
        SUM(assists) assists,
        RANK() OVER (PARTITION BY season, tournament ORDER BY SUM(assists) DESC) as player_rank
        FROM squadentry
        INNER JOIN games_selected ON games_selected.id = squadentry.fixture_id
        LEFT JOIN (SELECT * FROM team) as team ON team.id = squadentry.team_id
        LEFT JOIN (SELECT id,name,nationality FROM player) as player ON player.id = squadentry.player_id
        GROUP BY 1,2,3,4,5,6,7,8
        ORDER BY assists DESC)

    SELECT * FROM full_df
    WHERE player_rank = 1
    ORDER BY season DESC, tournament DESC
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute("""
        WITH games_selected as (
            SELECT DISTINCT id,tournament,season FROM fixture WHERE matchday IS NOT NULL
        ),
        full_df as (
            SELECT squadentry.player_id, squadentry.team_id,
                   team.name as team_name, player.name as player_name, player.nationality as player_nationality,
                   games_selected.tournament, games_selected.season,
                   SUM(goals) goals, SUM(assists) assists,
                   SUM(goals) + SUM(assists) as goals_assists,
                   SUM(squadentry.minutes) total_minutes,
                   ROUND((SUM(goals) + SUM(assists)) * 90.0 / NULLIF(SUM(squadentry.minutes),0), 2) as ga_per90,
                   RANK() OVER (PARTITION BY season, tournament ORDER BY SUM(goals) + SUM(assists) DESC) as player_rank
            FROM squadentry
            INNER JOIN games_selected ON games_selected.id = squadentry.fixture_id
            LEFT JOIN team   ON team.id = squadentry.team_id
            LEFT JOIN player ON player.id = squadentry.player_id
            GROUP BY 1,2,3,4,5,6,7
        )
        SELECT * FROM full_df WHERE player_rank = 1 ORDER BY season DESC, tournament DESC
    """).df()
    return


@app.cell
def _(conn):
    conn.execute(""" SELECT DISTINCT team_name  FROM playerteam WHERE player_id = 30562 """).df()
    return


@app.cell
def _(conn):
    conn.execute("""
        WITH games_selected as (
            SELECT DISTINCT id,tournament,season FROM fixture WHERE matchday IS NOT NULL
        ),
        full_df as (
            SELECT squadentry.player_id, squadentry.team_id,
                   team.name as team_name, player.name as player_name, player.nationality as player_nationality,
                   games_selected.tournament, games_selected.season,
                   SUM(goals) goals, SUM(assists) assists,
                   SUM(goals) + SUM(assists) as goals_assists,
                   SUM(squadentry.minutes) total_minutes,
                   ROUND((SUM(goals) + SUM(assists)) * 90.0 / NULLIF(SUM(squadentry.minutes),0), 2) as ga_per90,
                   RANK() OVER (PARTITION BY season, tournament ORDER BY SUM(goals) DESC)                as goals_rank,
                   RANK() OVER (PARTITION BY season, tournament ORDER BY SUM(assists) DESC)              as assists_rank,
                   RANK() OVER (PARTITION BY season, tournament ORDER BY SUM(goals) + SUM(assists) DESC) as ga_rank
            FROM squadentry
            INNER JOIN games_selected ON games_selected.id = squadentry.fixture_id
            LEFT JOIN team   ON team.id = squadentry.team_id
            LEFT JOIN player ON player.id = squadentry.player_id
            GROUP BY 1,2,3,4,5,6,7
        )
        SELECT * FROM full_df
        WHERE ga_rank <= 10
        ORDER BY season DESC, tournament DESC, ga_rank
    """).df()
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ## Higest Scoring Teams Regular Season
    """)
    return


@app.cell
def _(conn):
    conn.execute(""" 

    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    WITH df as (SELECT * EXCLUDE(fixture.id), 
    ROW_NUMBER() OVER (PARTITION BY season, tournament ORDER BY date DESC) as match_rank
        FROM event
        LEFT JOIN (SELECT id,date,season,tournament FROM fixture) as fixture ON fixture.id = event.fixture_id)

        SELECT     fixture_id, season, tournament,
        team_id, 
        team.name as team_name, 
        COUNT(*) as goals FROM df
        LEFT JOIN team ON team.id = df.team_id
        WHERE match_rank <= 2 and type = 'Goal'
        GROUP BY fixture_id, team_id, team.name, season, tournament
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    SELECT 
        fixture_id, 
        team_id, 
        team.name as team_name, 
        COUNT(*) as goals
    FROM event
    LEFT JOIN team ON team.id = event.team_id
    WHERE (fixture_id = 292003 OR fixture_id = 292002) 
      AND type = 'Goal'
    GROUP BY fixture_id, team_id, team.name
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" WITH finals_dates AS (
        -- Find the last 2 matches (by date) for each season/tournament combination
        SELECT 
            season,
            tournament,
            id,
            home_team_name,
            away_team_name,
            home_goals,
            away_goals,
            date,
            ROW_NUMBER() OVER (PARTITION BY season, tournament ORDER BY date DESC) as match_rank
        FROM fixture
    ),
    finals_raw AS (
        -- Filter to get only the last 2 matches (the finals)
        SELECT 
            season,
            tournament,
            id,
            home_team_name,
            away_team_name,
            home_goals,
            away_goals,
            date
        FROM finals_dates
        WHERE match_rank <= 2
    )

    from finals_raw
    """)
    return


@app.cell
def _(conn):
    conn.execute(""" 
    SELECT id, season,tournament,home_team_name, away_team_name, home_goals,away_goals 
        FROM fixture 
        WHERE round LIKE '%Final%'

    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    WITH main_db as (SELECT * 
        FROM fixture 
        WHERE matchday IS NOT NULL
        ORDER BY date desc), 
    away_team as (
            SELECT 
            away_team_name as team, season, tournament, SUM(away_goals) as goals
        FROM main_db
        GROUP BY away_team_name, season, tournament
        )

        SELECT * FROM (SELECT *, RANK() OVER(PARTITION BY season,tournament ORDER BY goals DESC) as rank  FROM(
        SELECT team,season,tournament, SUM(goals) as goals 
        FROM (SELECT 
            home_team_name as team,season,tournament, SUM(home_goals) as goals
            FROM main_db
            GROUP BY 1,2,3
            UNION SELECT * FROM away_team)
        GROUP BY 1,2,3)) WHERE rank = 1 ORDER BY season DESC,tournament

    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" WITH main_db as (SELECT * 
        FROM fixture 
        WHERE season = 2025 and tournament = 'Clausura' and matchday IS NOT NULL
        ORDER BY date desc), away_team as (
            SELECT 
            away_team_name as team, SUM(away_goals) as goals
        FROM main_db
        GROUP BY team
        )

        SELECT team, SUM(goals) as goals 
        FROM (SELECT 
            home_team_name as team, SUM(home_goals) as goals
            FROM main_db
            GROUP BY team
            UNION SELECT * FROM away_team)
        GROUP BY team
        ORDER BY goals DESC

    """)\
    .df()
    return


@app.cell
def _():
    return


@app.cell
def _(conn):
    conn.execute(""" WITH games_selected as (
    SELECT DISTINCT id FROM fixture 
    WHERE season = 2019 and tournament = 'Clausura' 
    )

    SELECT * EXCLUDE(fixture.id) FROM fixture 
    INNER JOIN games_selected ON games_selected.id = fixture.id
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    WITH games_selected as (
    SELECT DISTINCT id FROM fixture 
    WHERE season = 2025 and tournament = 'Clausura' and matchday IS NOT NULL
    )

    SELECT * FROM teammatchstat 
    INNER JOIN games_selected ON games_selected.id = teammatchstat.fixture_id
    limit 5
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    WITH games_selected as (
    SELECT DISTINCT id FROM fixture 
    WHERE season = 2025 and tournament = 'Clausura' and matchday IS NOT NULL
    )

    SELECT player.id as player_id,name,nationality,SUM(goals) as goals
    FROM squadentry
    LEFT JOIN (SELECT id,name,nationality FROM player) AS player ON squadentry.player_id = player.id
    INNER JOIN games_selected ON games_selected.id = squadentry.fixture_id
    WHERE nationality IS NULL
    GROUP BY name,nationality,player.id
    ORDER BY goals DESC
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    WITH games_selected as (
    SELECT DISTINCT id FROM fixture 
    WHERE season = 2025 and tournament = 'Clausura' and matchday IS NOT NULL
    )

    SELECT nationality,  SUM(1) as goals
    FROM event 
    INNER JOIN games_selected ON games_selected.id = event.fixture_id
    LEFT JOIN (SELECT id,name,nationality FROM player) AS player ON event.player_id = player.id
    LEFT JOIN (SELECT id as team_id, name as team_name FROM team) as teams on teams.team_id = event.team_id
    WHERE type = 'Goal'
    GROUP BY nationality
    ORDER BY goals DESC
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    WITH games_selected as (
    SELECT DISTINCT id FROM fixture 
    WHERE season = 2025 and tournament = 'Clausura' and matchday IS NOT NULL
    )

    SELECT event.player_id,name, nationality, event.team_id, team_name, SUM(1) as goals
    FROM event 
    INNER JOIN games_selected ON games_selected.id = event.fixture_id
    LEFT JOIN (SELECT id as player_id,name,nationality FROM player) AS player ON event.player_id = player.player_id
    LEFT JOIN (SELECT id as team_id, name as team_name FROM team) as teams on teams.team_id = event.team_id
    WHERE type = 'Goal' 
    GROUP BY event.player_id,player.name, nationality, event.team_id, team_name
    ORDER BY goals DESC
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    SELECT * FROM team limit 5
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    SELECT * FROM venue limit 5
    """)\
    .df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    WITH games_selected_liguilla as (
    SELECT DISTINCT id, 'ligulla' as tournament  FROM fixture 
    WHERE season = 2025 and tournament = 'Clausura' and matchday IS NULL
    ),  games_selected_tournament as (
    SELECT DISTINCT id, 'regular' as tournament  FROM fixture 
    WHERE season = 2025 and tournament = 'Clausura' and matchday IS NOT NULL
    ), reg_tournament as (
    SELECT team_id, ROUND(AVG(possession),2)  as avg_possession_reg
    FROM teammatchstat 
    LEFT JOIN (SELECT * FROM team) as team ON team.id = teammatchstat.team_id
    INNER JOIN games_selected_tournament ON games_selected_tournament.id = teammatchstat.fixture_id
    GROUP BY 1
    )

    SELECT teammatchstat.team_id,team.name,tournament, ROUND(AVG(possession),2)  as avg_possession_liguilla, MAX(avg_possession_reg) as avg_possession_reg
    FROM teammatchstat 
    LEFT JOIN (SELECT * FROM team) as team ON team.id = teammatchstat.team_id
    INNER JOIN games_selected_liguilla ON games_selected_liguilla.id = teammatchstat.fixture_id
    LEFT JOIN reg_tournament ON teammatchstat.team_id = reg_tournament.team_id
    GROUP BY 1,2,3
    ORDER BY avg_possession_liguilla DESC
    """).df()
    return


@app.cell
def _(conn):
    #id = 1545451 and 1545452
    conn.execute("""
    WITH games_selected as (
    SELECT DISTINCT id FROM fixture 
    WHERE season = 2025 and tournament = 'Clausura'
    )

    FROM fixture

    WHERE id = 1545451 OR id = 1545452
    limit 5 
    """)\
    .df()
    return


@app.cell
def _():
    return


@app.cell
def _(conn):
    conn.execute(""" WITH main_db as (SELECT * 
        FROM fixture 
        WHERE season = 2025 and tournament = 'Clausura' and matchday IS NOT NULL
        ORDER BY date desc), away_team as (
            SELECT 
            away_team_name as team, SUM(away_goals) as goals
        FROM main_db
        GROUP BY team
        )

        SELECT team, SUM(goals) as goals FROM (SELECT 
            home_team_name as team, SUM(home_goals) as goals
        FROM main_db
        GROUP BY team
        UNION SELECT * FROM away_team)
        GROUP BY team
        ORDER BY goals DESC

    """)\
    .df()
    return


@app.cell
def _():
    return


@app.cell
def _(conn):
    # Fixed query - Team with most goals per season and tournament
    df_goals_ranking = conn.execute("""
    WITH main_db as (
        SELECT * 
        FROM fixture 
        WHERE matchday IS NOT NULL
        ORDER BY date desc
    ), 
    away_team as (
        SELECT 
            away_team_name as team, season, tournament, SUM(away_goals) as goals
        FROM main_db
        GROUP BY away_team_name, season, tournament
    )

    SELECT 
        team,
        season,
        tournament,
        goals,
        RANK() OVER(PARTITION BY season, tournament ORDER BY goals DESC) as rank
    FROM(
        SELECT team, season, tournament, SUM(goals) as goals 
        FROM (
            SELECT 
                home_team_name as team, season, tournament, SUM(home_goals) as goals
            FROM main_db
            GROUP BY 1,2,3
            UNION ALL 
            SELECT * FROM away_team
        )
        GROUP BY 1,2,3
        ORDER BY season DESC, goals DESC
    )
    """).df()

    df_goals_ranking
    return


@app.cell
def _(conn):
    # Finals - Flattened with Winner Determination
    # Using date-based logic to identify the true finals (last 2 games of each tournament)
    df_finals = conn.execute("""
    WITH finals_dates AS (
        -- Find the last 2 matches (by date) for each season/tournament combination
        SELECT 
            season,
            tournament,
            id,
            home_team_name,
            away_team_name,
            home_goals,
            away_goals,
            date,
            ROW_NUMBER() OVER (PARTITION BY season, tournament ORDER BY date DESC) as match_rank
        FROM fixture
    ),
    finals_raw AS (
        -- Filter to get only the last 2 matches (the finals)
        SELECT 
            season,
            tournament,
            id,
            home_team_name,
            away_team_name,
            home_goals,
            away_goals,
            date
        FROM finals_dates
        WHERE match_rank <= 2
    ),
    finals_base AS (
        -- Normalize the data so team_1 is always lexicographically smaller
        SELECT 
            season,
            tournament,
            CASE WHEN home_team_name < away_team_name 
                 THEN home_team_name ELSE away_team_name END as team_1,
            CASE WHEN home_team_name < away_team_name 
                 THEN away_team_name ELSE home_team_name END as team_2,
            -- Goals scored by team_1 in this match
            CASE WHEN home_team_name < away_team_name 
                 THEN home_goals ELSE away_goals END as team_1_goals,
            -- Goals scored by team_2 in this match
            CASE WHEN home_team_name < away_team_name 
                 THEN away_goals ELSE home_goals END as team_2_goals,
            -- Whether team_1 is playing away in this match (0 = home, 1 = away)
            CASE WHEN home_team_name < away_team_name 
                 THEN 0 ELSE 1 END as team_1_is_away,
            date
        FROM finals_raw
    ),
    final_stats AS (
        -- Aggregate both legs of each final
        SELECT 
            season,
            tournament,
            team_1,
            team_2,
            SUM(team_1_goals) as team_1_total_goals,
            SUM(team_2_goals) as team_2_total_goals,
            -- Count away goals for team_1 (when team_1_is_away = 1)
            SUM(CASE WHEN team_1_is_away = 1 THEN team_1_goals ELSE 0 END) as team_1_away_goals,
            -- Count away goals for team_2 (when team_1_is_away = 0, team_2 is away)
            SUM(CASE WHEN team_1_is_away = 0 THEN team_2_goals ELSE 0 END) as team_2_away_goals,
            MAX(date) as final_date
        FROM finals_base
        GROUP BY season, tournament, team_1, team_2
    )
    SELECT 
        season,
        tournament,
        team_1,
        team_2,
        team_1_total_goals,
        team_2_total_goals,
        team_1_away_goals,
        team_2_away_goals,
        CASE 
            WHEN team_1_total_goals > team_2_total_goals THEN team_1
            WHEN team_2_total_goals > team_1_total_goals THEN team_2
            WHEN team_1_away_goals > team_2_away_goals THEN team_1 || ' (Away Goals)'
            WHEN team_2_away_goals > team_1_away_goals THEN team_2 || ' (Away Goals)'
            ELSE 'Draw'
        END as winner,
        final_date
    FROM final_stats
    ORDER BY season DESC, tournament DESC
    """).df()

    df_finals
    return


@app.cell
def _(mo):
    mo.md(f"""
 
    """)
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
