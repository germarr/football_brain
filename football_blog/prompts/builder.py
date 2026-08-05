"""Turn a FullFixture into a compact user prompt for the LLM.

The prompt structure is deliberately structured (headings, lists, numbers)
rather than prose, so the model can pattern-match on the facts and write
freely without paraphrasing our text back.
"""
from zoneinfo import ZoneInfo

from ..stage import stage_label_es
from ..types import CommentaryLine, FullFixture, MatchEventRow, SquadEntryRow


def _player_name(fx: FullFixture, player_id: int | None) -> str:
    if player_id is None:
        return "—"
    p = fx.players.get(player_id)
    return p.name if p else f"#{player_id}"


def _player_nationality(fx: FullFixture, player_id: int | None) -> str | None:
    if player_id is None:
        return None
    p = fx.players.get(player_id)
    return p.nationality if (p and p.nationality) else None


def _event_line(fx: FullFixture, e: MatchEventRow) -> str:
    minute = f"{e.minute}'{'+' + str(e.extra) if e.extra else ''}" if e.minute is not None else "?"
    team_name = fx.fixture.home_team_name if e.team_id == fx.fixture.home_team_id else fx.fixture.away_team_name
    who = _player_name(fx, e.player_id)
    assist = f" (assist: {_player_name(fx, e.assist_id)})" if e.assist_id else ""
    detail = f" [{e.detail}]" if e.detail else ""
    return f"  {minute:>5}  {team_name}  {e.type}{detail}  {who}{assist}"


def _team_stats_block(fx: FullFixture) -> str:
    home_id = fx.fixture.home_team_id
    home = next((s for s in fx.team_stats if s.team_id == home_id), None)
    away = next((s for s in fx.team_stats if s.team_id != home_id), None)
    if not (home and away):
        return "  (team match stats not available)"
    rows = [
        ("Possession %", home.possession, away.possession),
        ("Shots total", home.shots_total, away.shots_total),
        ("Shots on target", home.shots_on, away.shots_on),
        ("xG", home.expected_goals, away.expected_goals),
        ("Corners", home.corners, away.corners),
        ("Fouls", home.fouls, away.fouls),
        ("Yellow cards", home.yellow, away.yellow),
        ("Red cards", home.red, away.red),
    ]
    lines = [f"  {'Stat':<20} {fx.fixture.home_team_name:>25}  {fx.fixture.away_team_name:>25}"]
    for label, h, a in rows:
        lines.append(f"  {label:<20} {str(h if h is not None else '—'):>25}  {str(a if a is not None else '—'):>25}")
    return "\n".join(lines)


def _commentary_line(cl: CommentaryLine) -> str:
    minute = cl.minute or "?"
    team = cl.team or "—"
    return f"  {minute:>6}  [{cl.category}]  {team}  {cl.text}"


def _commentary_block(fx: FullFixture) -> str:
    if not fx.commentary:
        return ""
    lines = "\n".join(_commentary_line(cl) for cl in fx.commentary)
    return f"""
COMMENTARY (ESPN key moments — reference only for color and specific detail; DO NOT quote or paraphrase verbatim, write in your own voice):
{lines}
"""


def _lineup_tag(fx: FullFixture, s: SquadEntryRow) -> str:
    """Parenthesized identity tag: position, nationality, captain flag."""
    parts: list[str] = [s.position or "—"]
    nat = _player_nationality(fx, s.player_id)
    if nat:
        parts.append(nat)
    if s.captain:
        parts.append("C")
    return "(" + ", ".join(parts) + ")"


def _lineup_block(fx: FullFixture, team_id: int, team_name: str) -> str:
    starters = [s for s in fx.squad if s.team_id == team_id and s.status == "started"]
    subs = [s for s in fx.squad if s.team_id == team_id and s.status == "came_on"]
    lines = [f"  {team_name}:"]
    lines.append("    Starters: " + ", ".join(
        f"{_player_name(fx, s.player_id)} {_lineup_tag(fx, s)}"
        for s in starters
    ))
    if subs:
        lines.append("    Subs on:  " + ", ".join(
            f"{_player_name(fx, s.player_id)} {_lineup_tag(fx, s)} ({s.minutes}')"
            for s in subs
        ))
    return "\n".join(lines)


def _standout_ids(entries: list[SquadEntryRow], fill_to: int = 6) -> list[SquadEntryRow]:
    """Curate the shortlist per team: always include material contributors
    (scorers, assist-givers, card recipients, captain, penalty-takers), then
    fill with top-rated players up to `fill_to`. Output ordered by rating desc
    (nulls last)."""
    priority: list[SquadEntryRow] = []
    seen: set[int] = set()
    for s in entries:
        material = (
            s.goals > 0 or s.assists > 0
            or s.yellow > 0 or s.red > 0
            or s.captain
            or s.penalty_scored > 0 or s.penalty_missed > 0
        )
        if material and s.player_id not in seen:
            priority.append(s)
            seen.add(s.player_id)
    fillers = sorted(
        (s for s in entries if s.player_id not in seen and s.rating is not None),
        key=lambda s: s.rating,
        reverse=True,
    )
    for s in fillers:
        if len(priority) >= fill_to:
            break
        priority.append(s)
        seen.add(s.player_id)
    priority.sort(key=lambda s: (s.rating is None, -(s.rating or 0.0)))
    return priority


def _standout_line(fx: FullFixture, s: SquadEntryRow) -> str:
    """One line per standout — skip stat components that are null / zero-and-boring."""
    parts: list[str] = [f"{_player_name(fx, s.player_id)} {_lineup_tag(fx, s)}"]
    if s.rating is not None:
        parts.append(f"rated {s.rating:.1f}")
    if s.minutes is not None and s.status == "came_on":
        parts.append(f"{s.minutes}' played")
    if s.goals or s.assists:
        parts.append(f"{s.goals}G/{s.assists}A")
    if s.shots_total:
        on = s.shots_on if s.shots_on is not None else 0
        parts.append(f"{s.shots_total}sh({on}on)")
    if s.passes_total:
        kp = s.passes_key or 0
        parts.append(f"{s.passes_total}pa" + (f"({kp}kp)" if kp else ""))
    if s.duels_total:
        won = s.duels_won if s.duels_won is not None else 0
        parts.append(f"{won}/{s.duels_total} duels")
    if s.tackles_total or s.interceptions:
        tkl = s.tackles_total or 0
        intc = s.interceptions or 0
        parts.append(f"{tkl}tkl+{intc}int")
    if s.dribbles_attempts:
        succ = s.dribbles_success if s.dribbles_success is not None else 0
        parts.append(f"{succ}/{s.dribbles_attempts}drb")
    if s.fouls_committed or s.fouls_drawn:
        parts.append(f"fouls {s.fouls_committed or 0}c/{s.fouls_drawn or 0}d")
    if s.yellow or s.red:
        parts.append(f"Y{s.yellow} R{s.red}")
    if s.penalty_scored or s.penalty_missed:
        parts.append(f"pen {s.penalty_scored}/{s.penalty_missed}m")
    return "  " + " · ".join(parts)


def _standouts_block(fx: FullFixture, team_id: int, team_name: str) -> str:
    entries = [s for s in fx.squad if s.team_id == team_id]
    picked = _standout_ids(entries)
    if not picked:
        return f"  {team_name}: (no per-player stats)"
    lines = [f"  {team_name}:"]
    for s in picked:
        lines.append(_standout_line(fx, s))
    return "\n".join(lines)


def _instruction_block(instruction: str | None) -> str:
    """The operator's one-off steer for this report (ADR 0034).

    It sits in the *user* prompt, not the system prompt, and the distinction is not
    cosmetic: the system prompt carries the Publication's standing voice, while this
    is about one match and must not outlive it.

    The framing matters more than the placement. Rule 1 of `system_es.md` is "Nunca
    inventes datos", and an instruction is the obvious way to erode it — "say they
    were unlucky" invites a claim no Event supports. So the block says in the prompt
    itself that direction governs emphasis and shape only, and never licenses a fact
    absent from the material above. It is placed after the facts for the same reason:
    the model reads the evidence first and the steer second.
    """
    if not instruction or not instruction.strip():
        return ""
    return f"""
EDITORIAL DIRECTION FOR THIS REPORT (from the editor):
  {instruction.strip()}

  This direction governs emphasis, angle and structure only. It does not license any
  claim that is not supported by the facts above — if it asks for something the data
  does not show, follow the facts and ignore the direction on that point.
"""


def build_user_prompt(
    fx: FullFixture,
    display_timezone: str,
    instruction: str | None = None,
) -> str:
    f = fx.fixture
    local_kickoff = f.date.astimezone(ZoneInfo(display_timezone))
    venue_str = fx.venue.name if fx.venue else "unknown venue"
    if fx.venue and fx.venue.city:
        venue_str += f", {fx.venue.city}"

    stage = stage_label_es(f.round, f.matchday)
    header = (
        f"MATCH FACTS — write a narrative in the language of this prompt's system message.\n\n"
        f"Fixture: {f.home_team_name} {f.home_goals if f.home_goals is not None else '?'}"
        f"–{f.away_goals if f.away_goals is not None else '?'} {f.away_team_name}\n"
        f"League: {f.league_name} · {f.tournament}"
        + (f" · {stage}" if stage else "")
        + f" · Season {f.season}\n"
        f"Kickoff: {local_kickoff.strftime('%A %d %B %Y, %H:%M')} (local)\n"
        f"Venue: {venue_str}\n"
        f"Status: {f.status}"
    )

    events_block = "\n".join(_event_line(fx, e) for e in fx.events) or "  (no timed events recorded)"

    return f"""{header}

TIMELINE OF EVENTS:
{events_block}

TEAM MATCH STATS:
{_team_stats_block(fx)}

STARTING LINEUPS + SUBS:
{_lineup_block(fx, f.home_team_id, f.home_team_name)}
{_lineup_block(fx, f.away_team_id, f.away_team_name)}

STANDOUT PERFORMERS (name these players by name in the narrative — do not write abstractly when you have individual stats):
{_standouts_block(fx, f.home_team_id, f.home_team_name)}
{_standouts_block(fx, f.away_team_id, f.away_team_name)}
{_commentary_block(fx)}{_instruction_block(instruction)}
Write the match report now. Do not include a title or a headline — start straight into the first paragraph.
"""
