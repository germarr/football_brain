import marimo

__generated_with = "0.23.13"
app = marimo.App(width="medium")


@app.cell
def _():
    import sys
    import marimo as mo
    import duckdb
    import pandas as pd
    import altair as alt
    from pathlib import Path

    # The repo root, pinned like db_path below. Buys the canonical Category
    # order from the taxonomy itself rather than re-typing 22 names here, which
    # would drift the moment a Category is added.
    if "/home/azureuser/alt_data" not in sys.path:
        sys.path.insert(0, "/home/azureuser/alt_data")
    from commentary.taxonomy import CATEGORY_NAMES

    db_path = Path("/home/azureuser/alt_data/data/commentary.db")
    if not db_path.exists():
        raise FileNotFoundError(
            f"No commentary store at {db_path} — ingest a match first:\n"
            f"  python -m commentary.ingest https://www.espn.com/soccer/commentary/_/gameId/760514"
        )

    # duckdb reads the SQLite file directly, same as notebooks/match_story.py
    conn = duckdb.connect(str(db_path), read_only=True)
    return CATEGORY_NAMES, alt, conn, mo, pd


@app.cell
def _(mo):
    # ── Palette ──────────────────────────────────────────────────────────────
    # Two series only — home and away. That is deliberate: a match has 22
    # Categories and colouring by Category would need 22 hues, which no palette
    # can keep distinguishable (an 8-hue attempt failed the normal-vision floor
    # at ΔE 7.1, red vs orange — and 8 is already short of 22). So Category is
    # carried by other channels — a text token on the key-moments strip, a row
    # of its own on the everything chart — and colour is left to answer the
    # question a timeline is actually asked: *which team*.
    #
    # Both steps validated against their own surface (ΔE 29.0 normal / 26.5 CVD
    # light; 29.9 / 27.3 dark). Dark is selected for the dark surface, not flipped.
    try:
        _dark = mo.app_meta().theme == "dark"
    except Exception:
        _dark = False

    PALETTE = (
        {
            "home": "#3987e5",
            "away": "#008300",
            "neutral": "#898781",
            "seq": "#3987e5",
            "ink": "#ffffff",
            "muted": "#898781",
            "grid": "#2c2c2a",
        }
        if _dark
        else {
            "home": "#2a78d6",
            "away": "#008300",
            "neutral": "#898781",
            "seq": "#2a78d6",
            "ink": "#0b0b0b",
            "muted": "#898781",
            "grid": "#e1e0d9",
        }
    )

    # The token carries the Category. Only *key moments* get one — the ~88 flow
    # lines per narrative match (foul, corner, offside, attempt_*) would bury them.
    #
    # Text, not emoji. ⚽🟨🟥🔄 read beautifully in a browser but depend on the
    # client having an emoji font, degrade silently to empty boxes when it
    # doesn't, and cannot be rendered headlessly at all — so the chart could
    # never be exported or verified. These tokens render in any sans font,
    # everywhere, and say what they mean without knowing the icon language.
    # GLYPH = {
    #     "goal": "GOAL",
    #     "penalty_scored": "PEN",
    #     "own_goal": "OG",
    #     "penalty_missed": "MISS",
    #     "penalty_awarded": "PEN?",
    #     "yellow_card": "YEL",
    #     "red_card": "RED",
    #     "substitution": "SUB",
    #     "var_decision": "VAR",
    # }

    GLYPH = {
        "goal": "> ⚽",
        "penalty_scored": "> ⚽",
        "own_goal": "> ⚽",
        "penalty_missed": "> 😩",
        "penalty_awarded": "> 🫸",
        "yellow_card": "> 🟨",
        "red_card": "> 🟥",
        "substitution": "> 🔄",
        "var_decision": "> 📺",
    }
    return GLYPH, PALETTE


@app.cell
def _(conn):
    matches = conn.execute(
        """
        SELECT m.game_id, m.league, m.date, m.venue, m.status, m.narration_coverage,
               m.home_team, m.home_score, m.away_team, m.away_score,
               m.fixture_id, m.model,
               (SELECT COUNT(*) FROM commentary_line l WHERE l.game_id = m.game_id) AS lines
        FROM narrated_match m
        ORDER BY m.date DESC
        """
    ).df()
    if matches.empty:
        raise ValueError(
            "commentary.db has no matches yet — run `python -m commentary.ingest <espn-url>`"
        )
    return (matches,)


@app.cell
def _(matches, mo):
    def _label(r):
        return (
            f"{r.home_team} {r.home_score}–{r.away_score} {r.away_team}"
            f"  ·  {r.league}  ·  {str(r.date)[:10]}"
            f"  ({r.narration_coverage}, {r.lines} lines)"
        )

    _options = {_label(r): r.game_id for r in matches.itertuples()}
    match_select = mo.ui.dropdown(
        options=_options,
        value=next(iter(_options)),
        label="Match",
    )
    return (match_select,)


@app.cell
def _(match_select, mo):
    mo.vstack(
        [
            mo.md("# Match timeline"),
            mo.md(
                "Every **Commentary Line** ESPN published for one match, as stored in "
                "`data/commentary.db` (ADR 0026). Pick a match — everything below "
                "re-runs reactively."
            ),
            match_select,
        ]
    )
    return


@app.cell
def _(match_select, matches):
    m = matches[matches["game_id"] == match_select.value].iloc[0]
    return (m,)


@app.cell
def _(m, mo, pd):
    # pd.notna, not `is not None`: a NULL fixture_id arrives as pd.NA, whose
    # str() is "<NA>" — the normal case, since most narrated matches have no
    # Fixture at all.
    _linked = (
        f"linked to Fixture `{int(m.fixture_id)}`"
        if pd.notna(m.fixture_id)
        else "no Fixture link (ESPN narrates competitions we don't collect)"
    )
    _labels = (
        f"Categories **asserted by ESPN** on every line — no model involved."
        if m.narration_coverage == "events_only"
        else f"Categories inferred by `{m.model}` where ESPN published no type."
    )

    # Narration Coverage is the one thing a reader MUST know before reading this
    # chart: an events_only match reports zero fouls because fouls are never
    # narrated there, not because none were committed. See CONTEXT.md.
    _warn = (
        mo.callout(
            mo.md(
                "**Narration Coverage: `events_only`.** ESPN narrated only goals, "
                "cards and substitutions for this match — so there are **no** fouls, "
                "corners, offsides or attempts below. That is *not narrated*, not "
                "*did not happen*. Do not compare its flow against a `narrative` match."
            ),
            kind="warn",
        )
        if m.narration_coverage == "events_only"
        else mo.callout(
            mo.md(
                "**Narration Coverage: `narrative`.** Full play-by-play — fouls, "
                "corners, offsides and attempts alongside the goals and cards."
            ),
            kind="info",
        )
    )

    mo.vstack(
        [
            mo.md(
                f"## {m.home_team} {m.home_score}–{m.away_score} {m.away_team}\n\n"
                f"**{m.league}** · {str(m.date)[:10]} · {m.venue or 'venue unknown'} · "
                f"{m.status} · ESPN game `{m.game_id}` · {_linked}\n\n"
                f"{m.lines} Commentary Lines. {_labels}"
            ),
            _warn,
        ]
    )
    return


@app.cell
def _(conn, m, pd):
    lines = conn.execute(
        """
        SELECT sequence, minute, clock_seconds, team, category, source, text,
               field_position
        FROM commentary_line
        WHERE game_id = ?
        ORDER BY sequence
        """,
        [m.game_id],
    ).df()

    # clock_seconds is the honest x-axis: `minute` is a display string that
    # renders stoppage as "45'+7'", while clock_seconds keeps counting.
    lines["clock_min"] = lines["clock_seconds"] / 60.0
    lines["lane"] = lines["team"].where(pd.notna(lines["team"]), "(neutral)")
    return (lines,)


@app.cell
def _(GLYPH, PALETTE, alt, lines, m, mo):
    _key = lines[lines["category"].isin(GLYPH)].copy()

    if _key.empty:
        _timeline = mo.md("*No key moments (goals, cards, subs) in this match.*")
    else:
        _key["glyph"] = _key["category"].map(GLYPH)

        # Dodge collisions — tokens are ~3 minutes wide at this scale, so
        # neighbours overlap, not just exact ties.
        #
        # Bucketing on a fixed grid does NOT work, and failed visibly: SUB at
        # 85.45' and GOAL at 87.37' are under two minutes apart but landed in
        # different 3' buckets, so both took slot 0 and rendered as "SUBGOAL".
        # A grid measures which cell you're in, never how close you are to your
        # neighbour. So place greedily on actual proximity: lowest free slot
        # whose last token is at least MIN_GAP away.
        _MIN_GAP = 4.0  # minutes — "GOAL" at 10px bold, plus headroom
        _key = _key.sort_values("clock_min")
        _slots: list[int] = []
        _last: dict = {}
        for _lane_i, _x in zip(_key["lane"], _key["clock_min"]):
            _s = 0
            while _last.get((_lane_i, _s), -1e9) > _x - _MIN_GAP:
                _s += 1
            _slots.append(_s)
            _last[(_lane_i, _s)] = _x
        _key["slot"] = _slots

        _lanes = [m.home_team, m.away_team]
        _range = [PALETTE["home"], PALETTE["away"]]
        if "(neutral)" in set(_key["lane"]):
            _lanes.append("(neutral)")
            _range.append(PALETTE["neutral"])

        _scale = alt.Scale(domain=_lanes, range=_range)
        _xmax = max(95.0, float(lines["clock_min"].max()) + 2)

        # Half-time / full-time reference lines: recessive, behind the marks.
        _rules = (
            alt.Chart(alt.Data(values=[{"at": 45}, {"at": 90}]))
            .mark_rule(color=PALETTE["grid"], strokeDash=[4, 4], size=1)
            .encode(x=alt.X("at:Q"))
        )

        _marks = (
            alt.Chart(_key)
            .mark_text(size=10, fontWeight="bold", baseline="middle")
            .encode(
                x=alt.X(
                    "clock_min:Q",
                    title="minute",
                    scale=alt.Scale(domain=[0, _xmax], nice=False),
                    axis=alt.Axis(values=[0, 15, 30, 45, 60, 75, 90], grid=False),
                ),
                y=alt.Y("lane:N", title=None, sort=_lanes, scale=alt.Scale(domain=_lanes)),
                yOffset=alt.YOffset("slot:N", title=None),
                text=alt.Text("glyph:N"),
                color=alt.Color(
                    "lane:N", scale=_scale, legend=alt.Legend(title="Team", orient="top")
                ),
                tooltip=[
                    alt.Tooltip("minute:N", title="minute"),
                    alt.Tooltip("category:N", title="category"),
                    alt.Tooltip("team:N", title="team"),
                    alt.Tooltip("source:N", title="label from"),
                    alt.Tooltip("text:N", title="commentary"),
                ],
            )
        )

        _timeline = mo.ui.altair_chart(
            (_rules + _marks)
            .properties(height=210, width="container", title="Key moments")
            .configure_view(stroke=None)
            .configure_axis(
                labelColor=PALETTE["muted"],
                titleColor=PALETTE["muted"],
                domainColor=PALETTE["grid"],
                tickColor=PALETTE["grid"],
            )
            .configure_legend(labelColor=PALETTE["ink"], titleColor=PALETTE["muted"])
            .configure_title(color=PALETTE["ink"], anchor="start")
        )

    # The token key IS the legend for Category — colour can't carry 22 classes,
    # so this is the direct-label channel, not decoration.
    _legend = mo.md(
        "`GOAL` goal  ·  `PEN` penalty scored  ·  `OG` own goal  ·  "
        "`MISS` penalty missed  ·  `PEN?` penalty awarded  ·  `YEL` yellow  ·  "
        "`RED` red  ·  `SUB` substitution  ·  `VAR` VAR decision"
        "  \n*Dashed lines mark 45' and 90'. Hover any token for the full line.*"
    )
    mo.vstack([_timeline, _legend])
    return


@app.cell
def _(CATEGORY_NAMES, PALETTE, alt, lines, m, mo):
    # ── Everything narrated ──────────────────────────────────────────────────
    # The key-moments chart above shows ~16 of 125 lines. This one shows all of
    # them: fouls, free kicks won, corners, offsides, attempts — every Category.
    #
    # Category has 22 classes, and colour cannot carry that (the 8-hue attempt
    # already failed the normal-vision floor at ΔE 7.1, red vs orange). So
    # Category becomes POSITION — one row each — which has unlimited capacity,
    # and colour stays on the two teams, where it validates. Shape carries the
    # one other thing worth seeing at a glance: whether ESPN asserted the
    # Category or a model inferred it.
    _all = lines.copy()
    _all["label_src"] = _all["source"].map(
        {"espn_keyevent": "ESPN (asserted)", "llm": "model (inferred)"}
    )

    # Canonical taxonomy order, NOT per-match frequency — the rows must not
    # reshuffle when you switch match, or two matches can't be compared.
    _present = set(_all["category"])
    _order = [c for c in CATEGORY_NAMES if c in _present]

    _lanes = [m.home_team, m.away_team]
    _range = [PALETTE["home"], PALETTE["away"]]
    if "(neutral)" in set(_all["lane"]):
        _lanes.append("(neutral)")
        _range.append(PALETTE["neutral"])

    _xmax = max(95.0, float(_all["clock_min"].max()) + 2)

    _rules = (
        alt.Chart(alt.Data(values=[{"at": 45}, {"at": 90}]))
        .mark_rule(color=PALETTE["grid"], strokeDash=[4, 4], size=1)
        .encode(x=alt.X("at:Q"))
    )

    _dots = (
        alt.Chart(_all)
        .mark_point(filled=True, size=48, opacity=0.9)
        .encode(
            x=alt.X(
                "clock_min:Q",
                title="minute",
                scale=alt.Scale(domain=[0, _xmax], nice=False),
                axis=alt.Axis(values=[0, 15, 30, 45, 60, 75, 90], grid=False),
            ),
            y=alt.Y(
                "category:N",
                title=None,
                sort=_order,
                scale=alt.Scale(domain=_order),
                axis=alt.Axis(grid=True),
            ),
            # No yOffset. Sub-laning by team sounds appealing but "(neutral)"
            # is in the domain, so EVERY Category got a third, usually empty
            # sub-lane: the chart ballooned to ~1850px and, worse, pushed marks
            # so far from their axis label that period_marker's dots sat nearer
            # added_time's label. One row per Category keeps the label on its
            # own marks; team stays legible by colour.
            color=alt.Color(
                "lane:N",
                scale=alt.Scale(domain=_lanes, range=_range),
                legend=alt.Legend(title="Team", orient="top"),
            ),
            shape=alt.Shape(
                "label_src:N",
                scale=alt.Scale(
                    domain=["ESPN (asserted)", "model (inferred)"],
                    range=["circle", "triangle-up"],
                ),
                legend=alt.Legend(title="Category from", orient="top"),
            ),
            tooltip=[
                alt.Tooltip("minute:N", title="minute"),
                alt.Tooltip("category:N", title="category"),
                alt.Tooltip("team:N", title="team"),
                alt.Tooltip("label_src:N", title="label from"),
                alt.Tooltip("text:N", title="commentary"),
            ],
        )
    )

    mo.vstack(
        [
            mo.ui.altair_chart(
                (_rules + _dots)
                .properties(
                    height=alt.Step(26),
                    width="container",
                    title=f"Everything narrated — all {len(_order)} Categories, {len(_all)} lines",
                )
                .configure_view(stroke=None)
                .configure_axis(
                    labelColor=PALETTE["muted"],
                    titleColor=PALETTE["muted"],
                    gridColor=PALETTE["grid"],
                    domainColor=PALETTE["grid"],
                    tickColor=PALETTE["grid"],
                )
                .configure_legend(
                    labelColor=PALETTE["ink"], titleColor=PALETTE["muted"]
                )
                .configure_title(color=PALETTE["ink"], anchor="start")
            ),
            mo.md(
                "*One dot per Commentary Line, on its own Category row and its "
                "team's sub-lane. Rows follow the taxonomy's canonical order, so "
                "they stay put when you switch match — a Category with no row "
                "simply never appeared in this one.*"
            ),
        ]
    )
    return


@app.cell
def _(PALETTE, alt, lines, m, mo):
    # Match flow: how much was narrated per 5 minutes, per team. Only meaningful
    # for `narrative` coverage — an events_only match has ~15 lines total, so its
    # "flow" would be noise shaped like a signal.
    if m.narration_coverage == "events_only":
        _flow = mo.md(
            "*Match flow is not shown: this match is `events_only`, so line volume "
            "reflects how much ESPN narrated, not how the match was played.*"
        )
    else:
        _f = lines[lines["lane"] != "(neutral)"].copy()
        _f["bucket"] = (_f["clock_min"] // 5) * 5
        _agg = _f.groupby(["bucket", "lane"], as_index=False).size()

        _flow = mo.ui.altair_chart(
            alt.Chart(_agg)
            .mark_line(size=2, point=alt.OverlayMarkDef(size=28))
            .encode(
                x=alt.X("bucket:Q", title="minute", axis=alt.Axis(values=[0, 15, 30, 45, 60, 75, 90])),
                y=alt.Y("size:Q", title="lines narrated"),
                color=alt.Color(
                    "lane:N",
                    scale=alt.Scale(domain=[m.home_team, m.away_team],
                                    range=[PALETTE["home"], PALETTE["away"]]),
                    legend=alt.Legend(title="Team", orient="top"),
                ),
                tooltip=["bucket:Q", "lane:N", "size:Q"],
            )
            .properties(
                height=190, width="container", title="Match flow — lines per 5 minutes"
            )
            .configure_view(stroke=None)
            .configure_axis(
                labelColor=PALETTE["muted"],
                titleColor=PALETTE["muted"],
                gridColor=PALETTE["grid"],
                domainColor=PALETTE["grid"],
                tickColor=PALETTE["grid"],
            )
            .configure_legend(labelColor=PALETTE["ink"], titleColor=PALETTE["muted"])
            .configure_title(color=PALETTE["ink"], anchor="start")
        )
    _flow
    return


@app.cell
def _(PALETTE, alt, lines):
    # Category counts: a magnitude question, so one hue and a sorted bar —
    # not 21 colours.
    _counts = lines.groupby("category", as_index=False).size().sort_values("size", ascending=False)

    (alt.Chart(_counts)
        .mark_bar(cornerRadiusEnd=4, color=PALETTE["seq"])
        .encode(
            x=alt.X("size:Q", title="lines"),
            y=alt.Y("category:N", title=None, sort="-x"),
            tooltip=["category:N", "size:Q"],
        )
        .properties(
            height=alt.Step(20), width="container", title="Categories in this match"
        )
        .configure_view(stroke=None)
        .configure_axis(
            labelColor=PALETTE["muted"],
            titleColor=PALETTE["muted"],
            gridColor=PALETTE["grid"],
            domainColor=PALETTE["grid"],
            tickColor=PALETTE["grid"],
        )
        .configure_title(color=PALETTE["ink"], anchor="start"))
    return


@app.cell
def _(lines, mo):
    _view = lines[["sequence", "minute", "team", "category", "source", "text"]]
    mo.vstack(
        [
            mo.md(
                "### Every line\n`source` says where the Category came from: "
                "`espn_keyevent` is asserted by the provider, `llm` is inferred."
            ),
            mo.ui.table(_view, selection=None, pagination=True, page_size=20),
        ]
    )
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
