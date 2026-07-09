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

    db_name = "football.db"

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
    conn = duckdb.connect(db_path)
    return conn, engine, pd


@app.cell
def _(conn):
    conn.execute(""" 
    SELECT*
        FROM fixture limit 5
    """)\
    .df()
    return


@app.cell
def _(engine, pd):
    # Query using SQLAlchemy engine
    df = pd.read_sql("""
        SELECT id,name,type,country FROM competition
    """, engine)

    df
    return


@app.cell
def _(engine, pd):
    # Query using SQLAlchemy engine
    df1 = pd.read_sql("""
        SELECT * FROM competition
    """, engine)

    df1
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
