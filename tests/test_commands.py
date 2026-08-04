"""Every `module=` string in the command registry resolves (ADR 0033).

The silent failure: `commands.py` records a module as a *string*, which the Console
turns into `python -m <module>` argv (ADR 0021). Nothing resolves it at import, so a
module renamed by ADR 0031 stays inert until an operator clicks the button and gets
`No module named`. No test, no import and no lint catches it.
"""
from __future__ import annotations

import importlib.util

from football.commands import COMMANDS, GROUPS
from scripts import preflight


def test_no_command_module_problems():
    assert preflight.check_command_modules() == []


def test_every_command_module_resolves():
    unresolved = [
        (c.key, c.module) for c in COMMANDS
        if importlib.util.find_spec(c.module) is None
    ]
    assert unresolved == []


def test_every_command_is_in_a_declared_group():
    """Role is declared here and nowhere else (ADR 0031), so a typo'd group would
    render a command into a section the Console never displays."""
    stray = [(c.key, c.group) for c in COMMANDS if c.group not in GROUPS]
    assert stray == []


def test_command_keys_are_unique():
    keys = [c.key for c in COMMANDS]
    assert len(set(keys)) == len(keys), "duplicate command key"


def test_stale_module_string_is_reported(monkeypatch):
    """The guard must bite — this is the exact shape of an ADR 0031 breakage."""
    from football import commands

    stale = commands.Command(
        key="stale-probe", group=GROUPS[0], title="probe",
        module="football.parse_MOVED", summary="probe",
    )
    monkeypatch.setattr(commands, "COMMANDS", [*COMMANDS, stale])
    problems = preflight.check_command_modules()
    assert any("stale-probe" in p for p in problems), problems
