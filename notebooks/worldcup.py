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
    db_path = Path("/home/azureuser/alt_data/data/world-cup.db")
    db_path_all = Path("/home/azureuser/alt_data/data/football.db")

    if not db_path.exists():
        raise FileNotFoundError(
            f"Could not locate database at {db_path}"
        )

    engine = create_engine(f"sqlite:///{db_path}")
    conn = duckdb.connect(db_path)
    conn1 = duckdb.connect(db_path_all)
    return (conn,)


@app.cell
def _(conn):
    conn.execute(""" 
    FROM fixture
    where id = 1581821
    """).df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    FROM event
    where fixture_id = 1581821
    """).df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    FROM teamprofile
    limit 5
    """).df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    FROM teammatchstat
    where fixture_id = 1581821
    """).df()
    return


@app.cell
def _(conn):
    # conn.execute(""" 
    # FROM fixture
    # where id = 1581821
    # """).df()

    conn.execute(""" 
    FROM squadentry
    LEFT JOIN player ON player.id = squadentry.player_id
    where fixture_id = 1581821
    """).df()
    return


@app.cell
def _(conn):
    all_games_26 = conn.execute("""
    SELECT DISTINCT id
    FROM fixture
    WHERE season = 2026
    """).df()

    event_df = conn.execute("""
    SELECT event.*
    FROM event
    INNER JOIN all_games_26 ON event.fixture_id = all_games_26.id
    """).df()

    individual_stats = conn.execute("""
        FROM squadentry
        INNER JOIN all_games_26 ON squadentry.fixture_id = all_games_26.id
        """).df()
    return (individual_stats,)


@app.cell
def _(individual_stats):
    individual_stats.head(2)
    return


@app.cell
def _(conn):
    pool = conn.execute("""
    SELECT individual_stats.*, 

        --PLAYER DATA
        player.name as player_name, 
        player.nationality as player_nationality, 
        player.birth_country as player_birt_country,

        -- TEAM DATA
        team.name as team_name,

    FROM individual_stats
    LEFT JOIN player ON player.id = individual_stats.player_id
    LEFT JOIN team ON team.id = individual_stats.team_id
    """).df()

    pool
    return


@app.cell
def _(conn):
    #1,581,821
    conn.execute("""
    SELECT individual_stats.*, 

        --PLAYER DATA
        player.name as player_name, 
        player.nationality as player_nationality, 
        player.birth_country as player_birt_country,

        -- TEAM DATA
        team.name as team_name,

    FROM individual_stats
    LEFT JOIN player ON player.id = individual_stats.player_id
    LEFT JOIN team ON team.id = individual_stats.team_id
    where fixture_id = 1581821
    """).df()
    return


@app.cell
def _(conn):
    conn.execute("""
    WITH total_score as (
    SELECT team_name, player_name, 

        --STATS
        SUM(goals) as goals, SUM(assists) as assists,
        SUM(goals) + SUM(assists) as total_perf

    FROM pool
    GROUP BY 1,2
    ORDER BY goals DESC, assists DESC
    )

    SELECT player_name,team_name, goals FROM(
    SELECT *, DENSE_RANK() OVER(ORDER BY goals DESC, total_perf DESC) rank
    FROM total_score)
    WHERE rank <= 5

    """).df()
    return


@app.cell
def _(conn):
    conn.execute("""
    WITH total_score as (
    SELECT team_name, player_name, 

        --STATS
        SUM(goals) as goals, SUM(assists) as assists,
        SUM(goals) + SUM(assists) as total_perf

    FROM pool
    GROUP BY 1,2
    ORDER BY goals DESC, assists DESC
    ),

    team_totals as (
    SELECT team_name, SUM(goals) as team_total_goals
    FROM total_score
    GROUP BY team_name
    ),

    ranked_players as (
    SELECT *, DENSE_RANK() OVER(ORDER BY goals DESC, total_perf DESC) rank
    FROM total_score
    )

    SELECT 
        rp.player_name,
        rp.team_name, 
        rp.goals,
        tt.team_total_goals,
        ROUND(100.0 * rp.goals / tt.team_total_goals, 2) as goals_percentage
    FROM ranked_players rp
    LEFT JOIN team_totals tt ON rp.team_name = tt.team_name
    WHERE rp.rank <= 5
    ORDER BY rp.goals DESC

    """).df()
    return


@app.cell
def _(conn):
    conn.execute("""
    SELECT 
        team_name,
        SUM(goals) as total_goals,
        SUM(assists) as total_assists,
        SUM(goals) + SUM(assists) as total_contribution
    FROM pool
    GROUP BY team_name
    ORDER BY total_goals DESC, total_assists DESC
    """).df()
    return


@app.cell
def _(conn):
    partial_df = conn.execute("""
    SELECT event_df.*, 

        --PLAYER DATA
        player.name as player_name, 
        player.nationality as player_nationality, 
        player.birth_country as player_birt_country,

        -- TEAM DATA
        team.name as team_name,

    FROM event_df
    LEFT JOIN player ON player.id = event_df.player_id
    LEFT JOIN team ON team.id = event_df.team_id
    """).df()

    partial_df.head()
    return


@app.cell
def _(conn):
    ## TOP SCORERS
    top_scorer = conn.execute("""
    SELECT team_name, player_name, detail, COUNT(*) as goals
    FROM partial_df
    WHERE type = 'Goal' and detail NOT IN ('Missed Penalty', 'Own Goal')
    GROUP BY 1,2,3
    ORDER BY goals DESC
    """).df().pivot_table(index='player_name', columns='detail', values='goals').reset_index()\
        .sort_values(by='Normal Goal', ascending=False)

    top_scorer['Penalty'] = top_scorer['Penalty'].fillna(0)
    top_scorer['total_goals'] = top_scorer['Normal Goal'] + top_scorer['Penalty']

    top_scorer.sort_values(by='total_goals', ascending=False).head()
    return


@app.cell
def _(conn):
    ## TOP SCORERS
    conn.execute("""
    SELECT team_name, player_name, detail, COUNT(*) as goals
    FROM partial_df
    WHERE type = 'Goal' and detail != ('Missed Penalty' or 'Own Goal')
    GROUP BY 1,2,3
    ORDER BY goals DESC
    """).df()\
        .pivot_table(index='player_name', columns='detail', values='goals').reset_index()\
        .sort_values(by='Normal Goal', ascending=False)
    return


@app.cell
def _(conn):
    # all_games_26 = conn.execute("""
    # SELECT DISTINCT id
    # FROM fixture
    # WHERE season = 2026
    # """).df()

    conn.execute(""" 
    SELECT *
    FROM fixture
    INNER JOIN all_games_26 ON all_games_26.id = fixture.id
    WHERE round = 'Semi-finals'
    """).df()
    return


@app.cell
def _(conn):
    conn.execute(""" 
    FROM fixture
    INNER JOIN all_games_26 ON all_games_26.id = fixture.id
    where round = 'Quarter-finals'
    """).df()
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
