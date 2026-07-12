import marimo

__generated_with = "0.23.13"
app = marimo.App(width="medium")


@app.cell
def _():
    # 🔴 LIVE TOGGLE — flip to True to read the provisional Live Mirror
    # (live/live.db) for an in-progress match you're polling with
    # `python -m live.poll <fixture_id>`; False reads the authoritative
    # world-cup.db. Set `fixture_id` below to the match you're watching.
    # (Live mode shows headline + timeline; squad/stats sections are empty —
    #  the Mirror is events+header only. See ADR 0020.)
    live = False
    return (live,)


@app.cell
def _(live):
    import marimo as mo
    import duckdb
    import pandas as pd
    import altair as alt
    from pathlib import Path

    db_path = Path(
        "/home/azureuser/alt_data/live/live.db"
        if live
        else "/home/azureuser/alt_data/data/world-cup.db"
    )
    if not db_path.exists():
        raise FileNotFoundError(
            f"Could not locate database at {db_path}"
            + ("  (live=True but no live.db yet — start `python -m live.poll`)" if live else "")
        )

    conn = duckdb.connect(str(db_path), read_only=True)
    return alt, conn, db_path, mo, pd


@app.cell
def _(conn):
    conn.execute("""FROM fixture where season = 2026 and round = 'Quarter-finals' """).df()
    return


@app.cell
def _(pd):
    # ── Shared formatting helpers ────────────────────────────────────────────
    def fmt_time(minute, extra):
        """Render a match clock; stoppage as 90+5'."""
        m = int(minute)
        if extra is not None and not pd.isna(extra) and int(extra) > 0:
            return f"{m}+{int(extra)}'"
        return f"{m}'"

    def period_of(minute):
        """Three-bucket period label; WC knockouts can reach Extra Time."""
        m = int(minute)
        if m <= 45:
            return "1st half"
        if m <= 90:
            return "2nd half"
        return "Extra Time"

    return fmt_time, period_of


@app.cell
def _(db_path, live, mo):
    _mode = (
        "🔴 **LIVE** — reading the provisional Live Mirror (`live.db`), overwritten each poll"
        if live
        else f"🗄️ authoritative `{db_path.name}`"
    )
    mo.md(
        f"""
    # The story of a match

    {_mode}

    Edit `fixture_id` below to any World Cup fixture — every section
    re-runs reactively, general → specific: result, line-ups, goals,
    penalties, subs, cards, and a full timeline.
    """
    )
    return


@app.cell
def _():
    # ⬇️ Change this to any World Cup fixture_id
    fixture_id = 1582681
    return (fixture_id,)


@app.cell
def _(conn, fixture_id):
    # ── Load the one authoritative fixture row ───────────────────────────────
    fx = conn.execute(
        "SELECT * FROM fixture WHERE id = ?", [fixture_id]
    ).df()
    if fx.empty:
        raise ValueError(f"No fixture with id {fixture_id} in world-cup.db")

    row = fx.iloc[0]
    home_id, away_id = int(row.home_team_id), int(row.away_team_id)
    home_name, away_name = row.home_team_name, row.away_team_name
    team_name = {home_id: home_name, away_id: away_name}
    return away_id, away_name, home_id, home_name, row, team_name


@app.cell
def _(away_name, home_name, mo, row):
    # ── Headline: score, winner, penalties — straight from `fixture` ─────────
    hg, ag = int(row.home_goals), int(row.away_goals)
    pens = None
    import pandas as _pd
    if not _pd.isna(row.penalty_home):
        ph, pa = int(row.penalty_home), int(row.penalty_away)
        pens = (ph, pa)

    score = f"**{home_name} {hg} – {ag} {away_name}**"
    if pens is not None:
        score += f"  _({pens[0]}–{pens[1]} pens)_"

    if hg > ag:
        outcome = f"🏆 {home_name} win"
    elif ag > hg:
        outcome = f"🏆 {away_name} win"
    elif pens is not None:
        winner = home_name if pens[0] > pens[1] else away_name
        outcome = f"🏆 {winner} win on penalties"
    else:
        outcome = "🤝 Draw"

    context = f"{row.league_name} · {row['round']} · {row.date:%d %b %Y} · status {row.status}"

    mo.md(f"## {score}\n\n{outcome}\n\n_{context}_")
    return


@app.cell
def _(conn, fixture_id, row):
    # ── Resolve each player's club at tournament time: the latest
    #    non-national, non-youth Career Stint at/before the tournament Season.
    #    (The Season-2026 stint is the national team; the club sits at 2025.)
    #    Same-season clubs (a transfer) are joined with ' / '. ────────────────
    _clubs = conn.execute(
        """
        WITH pids AS (
          SELECT DISTINCT player_id FROM squadentry WHERE fixture_id = ?
        ),
        nat AS (SELECT id FROM teamprofile WHERE is_national = 1),
        c AS (
          SELECT pt.player_id, pt.season, pt.team_name
          FROM playerteam pt
          JOIN pids ON pids.player_id = pt.player_id
          WHERE pt.season <= ?
            AND pt.team_id NOT IN (SELECT id FROM nat)
            AND NOT regexp_matches(pt.team_name, ' U[0-9]')
        ),
        latest AS (SELECT player_id, max(season) AS ms FROM c GROUP BY player_id)
        SELECT c.player_id, string_agg(DISTINCT c.team_name, ' / ') AS club
        FROM c
        JOIN latest l ON l.player_id = c.player_id AND c.season = l.ms
        GROUP BY c.player_id
        """,
        [fixture_id, int(row.season)],
    ).df()
    club_of = dict(zip(_clubs["player_id"], _clubs["club"]))
    return (club_of,)


@app.cell
def _(mo):
    mo.md("""
    ### Starting XIs
    """)
    return


@app.cell
def _(away_id, away_name, club_of, conn, fixture_id, home_id, home_name, mo):
    # ── Two lean side-by-side teamsheets, G → D → M → F ──────────────────────
    xi = conn.execute(
        """
        SELECT s.team_id, s.player_id, s.position, p.name, s.captain, s.minutes
        FROM squadentry s
        LEFT JOIN player p ON p.id = s.player_id
        WHERE s.fixture_id = ? AND s.status = 'started'
        """,
        [fixture_id],
    ).df()

    _pos_order = {"G": 0, "D": 1, "M": 2, "F": 3}
    xi["_ord"] = xi["position"].map(_pos_order).fillna(9)
    xi["©"] = xi["captain"].map(lambda c: "©" if c else "")
    xi["club"] = xi["player_id"].map(club_of).fillna("—")
    xi = xi.sort_values(["_ord", "name"])

    def _sheet(team_id):
        t = xi[xi["team_id"] == team_id]
        return t[["position", "name", "club", "©", "minutes"]].reset_index(drop=True)

    home_xi = _sheet(home_id)
    away_xi = _sheet(away_id)

    mo.hstack(
        [
            mo.vstack([mo.md(f"**{home_name}**"), home_xi]),
            mo.vstack([mo.md(f"**{away_name}**"), away_xi]),
        ],
        justify="start",
        gap=2,
    )
    return


@app.cell
def _(mo):
    mo.md("""
    ### Squad profiles

    Pre-match context on the same 22 starters — age (at kick-off), height,
    birthplace, and who was **born abroad** (`birth_country` ≠ the team's country).
    """)
    return


@app.cell
def _(away_id, away_name, club_of, conn, fixture_id, home_id, home_name, mo):
    # ── Age/height/birthplace + club of each starting XI, with the birth
    #    country surfaced for anyone born abroad. ──────────────────────────────
    def _bios(team_id):
        b = conn.execute(
            """
            SELECT p.id AS player_id, p.name,
                   date_diff('year', p.birth_date, f.date) AS age,
                   p.height_cm AS height,
                   p.birth_place AS birthplace,
                   p.birth_country AS birth_country,
                   tp.country AS team_country
            FROM squadentry s
            JOIN fixture f ON f.id = s.fixture_id
            LEFT JOIN player p ON p.id = s.player_id
            LEFT JOIN teamprofile tp ON tp.id = s.team_id
            WHERE s.fixture_id = ? AND s.team_id = ? AND s.status = 'started'
            ORDER BY age
            """,
            [fixture_id, team_id],
        ).df()
        b["_abroad_flag"] = b["birth_country"] != b["team_country"]
        # Country when born abroad, blank otherwise
        b["born abroad"] = b["birth_country"].where(b["_abroad_flag"], "")
        b["club"] = b["player_id"].map(club_of).fillna("—")
        return b

    def _panel(team_id, team_label):
        b = _bios(team_id)
        if b.empty:
            return mo.vstack([mo.md(f"**{team_label}**"), mo.md("_No squad data._")])
        _abroad = int(b["_abroad_flag"].sum())
        _summary = (
            f"Avg age **{b['age'].mean():.1f}** "
            f"(youngest {b.iloc[0]['name']} {int(b.iloc[0]['age'])}, "
            f"oldest {b.iloc[-1]['name']} {int(b.iloc[-1]['age'])}) · "
            f"avg height **{b['height'].mean():.0f} cm** · "
            f"**{_abroad}** born abroad"
        )
        _tbl = b[
            ["name", "age", "height", "birthplace", "born abroad", "club"]
        ].reset_index(drop=True)
        return mo.vstack([mo.md(f"**{team_label}**  \n{_summary}"), _tbl])

    mo.hstack(
        [_panel(home_id, home_name), _panel(away_id, away_name)],
        justify="start",
        gap=2,
    )
    return


@app.cell
def _(mo):
    mo.md("""
    ### Goals & assists
    """)
    return


@app.cell
def _(away_name, conn, fixture_id, fmt_time, home_id, home_name, team_name):
    # ── Goals: Normal / Penalty / Own Goal. Own Goals credited to opponent. ──
    goals = conn.execute(
        """
        SELECT e.minute, e.extra, e.team_id, e.detail,
               sc.name  AS scorer,
               ast.name AS assist
        FROM event e
        LEFT JOIN player sc  ON sc.id  = e.player_id
        LEFT JOIN player ast ON ast.id = e.assist_id
        WHERE e.fixture_id = ?
          AND e.type = 'Goal'
          AND e.detail <> 'Missed Penalty'
        ORDER BY e.minute, COALESCE(e.extra, 0)
        """,
        [fixture_id],
    ).df()

    def _credited(r):
        # Own Goal: player_id scored into their own net → counts for the other side
        if r["detail"] == "Own Goal":
            return away_name if r["team_id"] == home_id else home_name
        return team_name[r["team_id"]]

    _tag = {"Normal Goal": "", "Penalty": "(pen)", "Own Goal": "(OG)"}
    if not goals.empty:
        goals["time"] = goals.apply(lambda r: fmt_time(r["minute"], r["extra"]), axis=1)
        goals["team"] = goals.apply(_credited, axis=1)
        goals["kind"] = goals["detail"].map(_tag)
    goals_view = (
        goals[["time", "team", "scorer", "assist", "kind"]]
        if not goals.empty
        else goals
    )
    goals_view
    return


@app.cell
def _(mo):
    mo.md("""
    ### Penalties (scored & missed)
    """)
    return


@app.cell
def _(conn, fixture_id, fmt_time, mo, team_name):
    # ── In-game penalties only (the shootout lives in the headline). ─────────
    pens_tbl = conn.execute(
        """
        SELECT e.minute, e.extra, e.team_id, e.detail, p.name AS taker
        FROM event e
        LEFT JOIN player p ON p.id = e.player_id
        WHERE e.fixture_id = ?
          AND e.detail IN ('Penalty', 'Missed Penalty')
        ORDER BY e.minute, COALESCE(e.extra, 0)
        """,
        [fixture_id],
    ).df()

    if pens_tbl.empty:
        pens_out = mo.md("_No penalties taken during play._")
    else:
        pens_tbl["time"] = pens_tbl.apply(
            lambda r: fmt_time(r["minute"], r["extra"]), axis=1
        )
        pens_tbl["team"] = pens_tbl["team_id"].map(team_name)
        pens_tbl["outcome"] = pens_tbl["detail"].map(
            {"Penalty": "Scored", "Missed Penalty": "Missed"}
        )
        pens_out = pens_tbl[["time", "team", "taker", "outcome"]].reset_index(drop=True)
    pens_out
    return


@app.cell
def _(mo):
    mo.md("""
    ### Substitutions
    """)
    return


@app.cell
def _(conn, fixture_id, fmt_time, mo, team_name):
    # ── subst gotcha: player_id = OFF, assist_id = ON (see CONTEXT.md). ──────
    subs = conn.execute(
        """
        SELECT e.minute, e.extra, e.team_id,
               off.name AS off_player,
               onp.name AS on_player
        FROM event e
        LEFT JOIN player off ON off.id = e.player_id
        LEFT JOIN player onp ON onp.id = e.assist_id
        WHERE e.fixture_id = ? AND e.type = 'subst'
        ORDER BY e.minute, COALESCE(e.extra, 0)
        """,
        [fixture_id],
    ).df()

    if subs.empty:
        subs_out = mo.md("_No substitutions recorded._")
    else:
        subs["time"] = subs.apply(lambda r: fmt_time(r["minute"], r["extra"]), axis=1)
        subs["team"] = subs["team_id"].map(team_name)
        subs_out = subs[["time", "team", "off_player", "on_player"]].reset_index(
            drop=True
        )
    subs_out
    return


@app.cell
def _(mo):
    mo.md("""
    ### Cards
    """)
    return


@app.cell
def _(conn, fixture_id, fmt_time, mo, team_name):
    # ── Cards + a sent-off flag (a Red Card OR two yellows for one player). ──
    cards = conn.execute(
        """
        SELECT e.minute, e.extra, e.team_id,
               e.detail AS card, p.name AS player, e.comments AS reason
        FROM event e
        LEFT JOIN player p ON p.id = e.player_id
        WHERE e.fixture_id = ? AND e.type = 'Card'
        ORDER BY e.minute, COALESCE(e.extra, 0)
        """,
        [fixture_id],
    ).df()

    if cards.empty:
        cards_out = mo.md("_No cards shown._")
    else:
        cards["time"] = cards.apply(lambda r: fmt_time(r["minute"], r["extra"]), axis=1)
        cards["team"] = cards["team_id"].map(team_name)

        _reds = list(cards.loc[cards["card"] == "Red Card", "player"])
        _yc = cards.loc[cards["card"] == "Yellow Card", "player"].value_counts()
        _two_yellow = list(_yc[_yc >= 2].index)
        _sent_off = _reds + [p for p in _two_yellow if p not in _reds]

        _flag = (
            mo.md("🟥 **Sent off:** " + ", ".join(_sent_off))
            if _sent_off
            else mo.md("_No one sent off._")
        )
        _tbl = cards[["time", "team", "player", "card", "reason"]].reset_index(
            drop=True
        )
        cards_out = mo.vstack([_flag, _tbl])
    cards_out
    return


@app.cell
def _(mo):
    mo.md("""
    ### Timeline
    """)
    return


@app.cell
def _(
    away_name,
    conn,
    fixture_id,
    fmt_time,
    home_id,
    home_name,
    period_of,
    team_name,
):
    # ── Unified chronological timeline: goals, cards, subs, missed pens, VAR ─
    ev = conn.execute(
        """
        SELECT e.event_index, e.minute, e.extra, e.team_id, e.type, e.detail,
               e.comments,
               p.name AS player,
               a.name AS assist
        FROM event e
        LEFT JOIN player p ON p.id = e.player_id
        LEFT JOIN player a ON a.id = e.assist_id
        WHERE e.fixture_id = ?
        ORDER BY e.minute, COALESCE(e.extra, 0), e.event_index
        """,
        [fixture_id],
    ).df()

    def _icon(t, detail):
        if t == "Goal":
            if detail == "Missed Penalty":
                return "❌"
            if detail == "Own Goal":
                return "⚽"
            return "⚽"
        if t == "Card":
            return "🟥" if detail == "Red Card" else "🟨"
        if t == "subst":
            return "🔁"
        if t == "Var":
            return "📺"
        return "•"

    def _describe(r):
        t, detail, team = r["type"], r["detail"], team_name.get(r["team_id"], "?")
        player, assist = r["player"], r["assist"]
        reason = r["comments"]
        if t == "Goal":
            if detail == "Own Goal":
                opp = away_name if r["team_id"] == home_id else home_name
                return f"Own goal — {player} ({team}), counts for {opp}"
            if detail == "Missed Penalty":
                return f"Missed penalty — {player} ({team})"
            kind = " (pen)" if detail == "Penalty" else ""
            asst = f", assist {assist}" if isinstance(assist, str) and assist else ""
            return f"Goal{kind} — {player} ({team}){asst}"
        if t == "Card":
            label = "Red" if detail == "Red Card" else "Yellow"
            because = f", {reason}" if isinstance(reason, str) and reason else ""
            return f"{label} — {player} ({team}){because}"
        if t == "subst":
            return f"Sub — {player} off, {assist} on ({team})"
        if t == "Var":
            return f"VAR — {detail} ({team})"
        return f"{t} — {detail} ({team})"

    if ev.empty:
        timeline = ev
    else:
        timeline = ev.copy()
        timeline["time"] = timeline.apply(
            lambda r: fmt_time(r["minute"], r["extra"]), axis=1
        )
        timeline["period"] = timeline["minute"].map(period_of)
        timeline["icon"] = timeline.apply(
            lambda r: _icon(r["type"], r["detail"]), axis=1
        )
        timeline["description"] = timeline.apply(_describe, axis=1)
        timeline = timeline[["time", "period", "icon", "description"]].reset_index(
            drop=True
        )
    timeline
    return


@app.cell
def _(mo):
    mo.md("""
    ### Match stats

    The numbers behind the game — possession, shots, **xG**, passing — head-to-head,
    with an xG-vs-scoreline read of who earned the result.
    """)
    return


@app.cell
def _(
    alt,
    away_id,
    away_name,
    conn,
    fixture_id,
    home_id,
    home_name,
    mo,
    pd,
    row,
):
    # ── Head-to-head team match stats + possession/xG bars + xG insight. ─────
    _stats = conn.execute(
        """
        SELECT team_id, possession, shots_total, shots_on, expected_goals,
               corners, offsides, fouls, saves, passes_total, passes_pct
        FROM teammatchstat
        WHERE fixture_id = ?
        """,
        [fixture_id],
    ).df()

    if _stats.empty:
        match_stats = mo.md("_No team match stats for this fixture._")
    else:
        _by = {int(r.team_id): r for _, r in _stats.iterrows()}
        _h, _a = _by[home_id], _by[away_id]
        _rows = [
            ("Possession %", "possession"),
            ("Shots", "shots_total"),
            ("On target", "shots_on"),
            ("xG", "expected_goals"),
            ("Corners", "corners"),
            ("Offsides", "offsides"),
            ("Fouls", "fouls"),
            ("Saves", "saves"),
            ("Passes", "passes_total"),
            ("Pass %", "passes_pct"),
        ]

        def _fmt(v, key):
            return f"{v:.2f}" if key == "expected_goals" else f"{int(v)}"

        _table = pd.DataFrame(
            {
                home_name: [_fmt(_h[k], k) for _, k in _rows],
                "": [lbl for lbl, _ in _rows],
                away_name: [_fmt(_a[k], k) for _, k in _rows],
            }
        )

        def _bar(label, key):
            d = pd.DataFrame(
                {"team": [home_name, away_name], "value": [_h[key], _a[key]]}
            )
            return (
                alt.Chart(d)
                .mark_bar()
                .encode(
                    x=alt.X("value:Q", title=None),
                    y=alt.Y("team:N", title=None, sort=[home_name, away_name]),
                    color=alt.Color("team:N", legend=None),
                )
                .properties(height=80, width=240, title=label)
            )

        _hg, _ag = int(row.home_goals), int(row.away_goals)
        _hx, _ax = float(_h["expected_goals"]), float(_a["expected_goals"])

        def _read(goals, xg):
            if goals > xg + 0.75:
                return "clinical, outscored xG"
            if goals < xg - 0.75:
                return "wasteful, under xG"
            return "about as expected"

        _insight = (
            f"**xG read** — {home_name}: {_hg} from {_hx:.2f} xG "
            f"({_read(_hg, _hx)}); "
            f"{away_name}: {_ag} from {_ax:.2f} xG ({_read(_ag, _ax)})."
        )
        match_stats = mo.vstack(
            [
                mo.md(_insight),
                mo.hstack(
                    [_bar("Possession %", "possession"), _bar("xG", "expected_goals")],
                    justify="start",
                    gap=2,
                ),
                _table,
            ]
        )
    match_stats
    return


@app.cell
def _(mo):
    mo.md("""
    ### Man of the Match
    """)
    return


@app.cell
def _(conn, fixture_id, mo, team_name):
    # ── Highest Match Rating (either side, anyone who played) + top 5. ───────
    _rated = conn.execute(
        """
        SELECT p.name AS player, s.team_id, s.rating, s.goals, s.assists
        FROM squadentry s
        LEFT JOIN player p ON p.id = s.player_id
        WHERE s.fixture_id = ? AND s.minutes > 0 AND s.rating IS NOT NULL
        ORDER BY s.rating DESC
        """,
        [fixture_id],
    ).df()

    if _rated.empty:
        motm = mo.md("_Player ratings unavailable for this fixture._")
    else:
        _rated["team"] = _rated["team_id"].map(team_name)
        _star = _rated.iloc[0]
        _callout = (
            f"⭐ **Man of the Match: {_star.player} ({_star.team}) — {_star.rating}**"
        )
        _top = _rated.head(5)[["player", "team", "rating", "goals", "assists"]].reset_index(
            drop=True
        )
        motm = mo.vstack([mo.md(_callout), _top])
    motm
    return


@app.cell
def _(mo):
    mo.md("""
    ## The cup context

    Zooming out from this one match to **the winner's tournament run** —
    where this XI came from, how they've performed so far, and where they go next.
    """)
    return


@app.cell
def _(away_id, away_name, home_id, home_name, pd, row):
    # ── The team the story follows forward. Winner; on a drawn group game,
    #    fall back to the home team so the section still resolves. ────────────
    _hg, _ag = int(row.home_goals), int(row.away_goals)
    if _hg > _ag:
        win_id, win_name = home_id, home_name
    elif _ag > _hg:
        win_id, win_name = away_id, away_name
    elif not pd.isna(row.penalty_home):
        if int(row.penalty_home) > int(row.penalty_away):
            win_id, win_name = home_id, home_name
        else:
            win_id, win_name = away_id, away_name
    else:
        win_id, win_name = home_id, home_name  # drawn: profile the home team
    return win_id, win_name


@app.cell
def _(mo):
    mo.md("""
    ### World Cup pedigree
    """)
    return


@app.cell
def _(away_id, away_name, conn, home_id, home_name, mo, row):
    # ── Each team's furthest stage per edition (as-of this match, no leak),
    #    champions vs runners-up resolved from the Final result. ─────────────
    def _pedigree(tid):
        df = conn.execute(
            """
            WITH g AS (
              SELECT season,
                     CASE
                       WHEN lower(round) LIKE 'group%' THEN 1
                       WHEN round = 'Round of 32' THEN 2
                       WHEN round IN ('Round of 16', '8th Finals') THEN 3
                       WHEN round = 'Quarter-finals' THEN 4
                       WHEN round IN ('Semi-finals', '3rd Place Final') THEN 5
                       WHEN round = 'Final' THEN 6
                       ELSE 1 END AS rk
              FROM fixture
              WHERE (home_team_id = ? OR away_team_id = ?) AND date <= ?
            )
            SELECT season, max(rk) AS rk FROM g GROUP BY season ORDER BY season
            """,
            [tid, tid, row.date],
        ).df()
        _lab = {1: "Group", 2: "R32", 3: "R16", 4: "QF", 5: "SF", 6: "Final"}
        parts = []
        for _, r in df.iterrows():
            lbl = _lab[int(r.rk)]
            if int(r.rk) == 6:
                fin = conn.execute(
                    """
                    SELECT home_team_id, away_team_id, home_goals, away_goals,
                           penalty_home, penalty_away
                    FROM fixture WHERE season = ? AND round = 'Final'
                    """,
                    [int(r.season)],
                ).df()
                if not fin.empty:
                    f = fin.iloc[0]
                    _hw = (f.home_goals > f.away_goals) or (
                        f.home_goals == f.away_goals
                        and (f.penalty_home or 0) > (f.penalty_away or 0)
                    )
                    _w = int(f.home_team_id) if _hw else int(f.away_team_id)
                    lbl = "🏆 Champions" if _w == tid else "Runners-up"
            parts.append(f"{int(r.season)} {lbl}")
        return " · ".join(parts) if parts else "—"

    _h2h = (
        conn.execute(
            """
            SELECT count(*) AS n FROM fixture
            WHERE ((home_team_id = ? AND away_team_id = ?)
                OR (home_team_id = ? AND away_team_id = ?))
              AND date < ?
            """,
            [home_id, away_id, away_id, home_id, row.date],
        )
        .df()
        .iloc[0]["n"]
    )
    # Our data only reaches back so far — say so rather than overclaim "first ever".
    _first, _last = conn.execute(
        "SELECT min(season), max(season) FROM fixture"
    ).fetchone()
    _note = (
        f"These teams have not met at a World Cup since our data begins ({_first})."
        if int(_h2h) == 0
        else f"Prior World Cup meetings in our data ({_first}–{_last}): {int(_h2h)}."
    )

    pedigree = mo.md(
        f"**{home_name}** — {_pedigree(home_id)}  \n"
        f"**{away_name}** — {_pedigree(away_id)}  \n\n"
        f"_{_note}_  \n"
        f"_Pedigree covers editions in our data ({_first}–{_last}) only; a team's "
        f"World Cup history may run deeper._"
    )
    pedigree
    return


@app.cell
def _(mo):
    mo.md("""
    ### Lineup continuity
    """)
    return


@app.cell
def _(conn, fixture_id, mo, row, win_id):
    # ── What changed from the winner's immediately previous game (by date). ──
    _prev = conn.execute(
        """
        SELECT id, date, round, home_team_name, away_team_name
        FROM fixture
        WHERE season = 2026 AND date < ?
          AND (home_team_id = ? OR away_team_id = ?)
          AND status IN ('FT', 'AET', 'PEN')
        ORDER BY date DESC
        LIMIT 1
        """,
        [row.date, win_id, win_id],
    ).df()

    def _xi(fx_id):
        return set(
            conn.execute(
                """
                SELECT p.name
                FROM squadentry s
                LEFT JOIN player p ON p.id = s.player_id
                WHERE s.fixture_id = ? AND s.team_id = ? AND s.status = 'started'
                """,
                [fx_id, win_id],
            ).df()["name"]
        )

    if _prev.empty:
        continuity = mo.md("_First match of the tournament — no prior XI to compare._")
    else:
        _pr = _prev.iloc[0]
        _cur, _pre = _xi(fixture_id), _xi(int(_pr.id))
        _out = sorted(_pre - _cur)
        _in = sorted(_cur - _pre)
        _ref = (
            f"{_pr['round']} · {_pr.date:%d %b} "
            f"({_pr.home_team_name} v {_pr.away_team_name})"
        )
        if not _out and not _in:
            continuity = mo.md(f"**Unchanged XI** from {_ref}.")
        else:
            continuity = mo.md(
                f"**{len(_out)} change(s)** from {_ref}\n\n"
                f"**Out:** {', '.join(_out) or '—'}  \n"
                f"**In:** {', '.join(_in) or '—'}"
            )
    continuity
    return


@app.cell
def _(mo):
    mo.md("""
    ### Tournament run
    """)
    return


@app.cell
def _(conn, mo, row, win_id, win_name):
    # ── Per-player tallies over the winner's run, as-of this match. ──────────
    _run_ids = conn.execute(
        """
        SELECT id FROM fixture
        WHERE season = 2026 AND date <= ?
          AND (home_team_id = ? OR away_team_id = ?)
          AND status IN ('FT', 'AET', 'PEN')
        ORDER BY date
        """,
        [row.date, win_id, win_id],
    ).df()["id"].tolist()
    n_games = len(_run_ids)

    if not _run_ids:
        # No played fixtures for this team in this DB (e.g. a live-only Mirror).
        run_view = mo.md(f"_No played fixtures for {win_name} in this database yet._")
    else:
        _in_list = ", ".join(str(i) for i in _run_ids)  # ints from our own DB
        squad = conn.execute(
            f"""
            SELECT p.name AS player,
                   count(*) FILTER (WHERE s.status IN ('started', 'came_on')) AS apps,
                   count(*) FILTER (WHERE s.status = 'started') AS starts,
                   COALESCE(sum(s.minutes), 0) AS mins,
                   sum(s.goals)   AS goals,
                   sum(s.assists) AS assists,
                   sum(s.yellow)  AS yellow,
                   sum(s.red)     AS red
            FROM squadentry s
            LEFT JOIN player p ON p.id = s.player_id
            WHERE s.team_id = {win_id} AND s.fixture_id IN ({_in_list})
            GROUP BY p.name
            HAVING count(*) FILTER (WHERE s.status IN ('started', 'came_on')) >= 1
            ORDER BY goals DESC, assists DESC, mins DESC
            """
        ).df()

        if squad.empty:
            # Fixtures exist but no per-player entries — a squad-less DB (Live
            # Mirror is events+header only; see ADR 0020).
            run_view = mo.md(
                f"_No per-player squad data for {win_name}'s run in this database._"
            )
        else:
            for _c in ["apps", "starts", "mins", "goals", "assists", "yellow", "red"]:
                squad[_c] = squad[_c].fillna(0).astype(int)
            # ★ = ever-present: started every game in the run
            squad["XI"] = squad["starts"].map(lambda s: "★" if s == n_games else "")

            _scorer = squad.sort_values("goals", ascending=False).iloc[0]
            _assist = squad.sort_values("assists", ascending=False).iloc[0]
            _callout = (
                f"**{win_name} — {n_games} game(s) so far** · "
                f"Top scorer: {_scorer.player} ({_scorer.goals}) · "
                f"Top assists: {_assist.player} ({_assist.assists})  \n"
                f"_★ = ever-present in the starting XI_"
            )
            _view = squad.rename(columns={"yellow": "🟨", "red": "🟥"})[
                ["player", "apps", "starts", "mins", "goals", "assists", "🟨", "🟥", "XI"]
            ]
            run_view = mo.vstack([mo.md(_callout), _view])
    run_view
    return


@app.cell
def _(mo):
    mo.md("""
    ### Next game
    """)
    return


@app.cell
def _(conn, mo, row, win_id, win_name):
    # ── Derive the next opponent by looking forward: the earliest later
    #    Fixture that already names the winner. No bracket edge is stored,
    #    so if none exists yet, the round is known but the rival is not. ──────
    _LADDER = {
        "Round of 32": "Round of 16",
        "Round of 16": "Quarter-finals",
        "Quarter-finals": "Semi-finals",
        "Semi-finals": "Final",
    }

    if row.status not in ("FT", "AET", "PEN"):
        next_game = mo.md("_Match not yet played — no projection._")
    else:
        _fut = conn.execute(
            """
            SELECT date, round, home_team_id, home_team_name,
                   away_team_id, away_team_name
            FROM fixture
            WHERE season = 2026 AND date > ?
              AND (home_team_id = ? OR away_team_id = ?)
            ORDER BY date
            LIMIT 1
            """,
            [row.date, win_id, win_id],
        ).df()

        if not _fut.empty:
            _f = _fut.iloc[0]
            _opp = (
                _f.away_team_name
                if int(_f.home_team_id) == win_id
                else _f.home_team_name
            )
            next_game = mo.md(
                f"🎟️ **{win_name} advance to {_f['round']}** — "
                f"next: **{win_name} vs {_opp}**, {_f.date:%d %b %Y}"
            )
        elif row["round"] == "Final":
            next_game = mo.md("🏆 **Final** — no next round.")
        elif _LADDER.get(row["round"]):
            next_game = mo.md(
                f"🎟️ **{win_name} advance to {_LADDER[row['round']]}** — "
                f"opponent not yet determined."
            )
        else:
            next_game = mo.md(f"**{win_name} advance** — next match not yet scheduled.")
    next_game
    return


if __name__ == "__main__":
    app.run()
