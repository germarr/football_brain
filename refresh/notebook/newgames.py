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
    db_path = Path("/home/azureuser/alt_data/refresh/refresh.db")

    if not db_path.exists():
        raise FileNotFoundError(
            f"Could not locate database at {db_path}"
        )

    engine = create_engine(f"sqlite:///{db_path}")
    conn = duckdb.connect(db_path)
    return (conn,)


@app.cell
def _(conn):
    conn.execute("""
    SELECT * FROM sqlite_sequence
    """).df()
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
