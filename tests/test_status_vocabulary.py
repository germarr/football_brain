"""The Fixture status vocabulary has exactly one definition (ADR 0033).

The silent failure: six packages each held their own copy of `("FT","AET","PEN")` and
they all agreed, so nothing ever surfaced the duplication. A seventh copy, or an edit to
one of six, produces no error and no failing test — it produces a Refresh that collects a
fixture the Desk will not list, or a Live Mirror row that publish declines to clear.
`football_blog`'s own note recorded that `draft` and `onboard` had already drifted from
`pipeline` once, before the Desk existed.

So this file guards two things a reader cannot check by looking: that every consumer
still resolves to the *same object*, and that the literal has not quietly reappeared
somewhere new.
"""
from __future__ import annotations

import ast

import pytest

from football import status
from tests.conftest import REPO_ROOT

#: Every module that names the vocabulary, and the attribute it exposes. Adding a
#: consumer means adding it here — cheaper than discovering a seventh copy later.
CONSUMERS = [
    ("football_blog", "FINAL_STATUSES", "FINAL"),
    ("refresh.core", "FINAL_STATUSES", "FINAL"),
    ("serving.publish", "FINAL", "FINAL"),
    ("serving.publish", "TERMINAL", "TERMINAL"),
    ("live.poll", "_FINAL", "FINAL"),
    ("live.poll", "_TERMINAL", "TERMINAL"),
    ("surfaces.viewer.app", "FINAL_STATUS", "FINAL"),
    ("surfaces.viewer.app", "TERMINAL_STATUS", "TERMINAL"),
    ("surfaces.viewer.app", "LIVE_STATUS", "LIVE"),
]


@pytest.mark.parametrize("module,attr,canonical", CONSUMERS)
def test_consumer_is_the_kernel_object(module, attr, canonical):
    """Not merely equal — the same object, so no copy can drift from the original."""
    import importlib
    got = getattr(importlib.import_module(module), attr)
    assert got is getattr(status, canonical), (
        f"{module}.{attr} is a copy, not {canonical} from football.status"
    )


#: Production code only. `notebooks/` is exploratory marimo and holds the literal inline
#: in three SQL strings — left alone deliberately rather than edited blind, and recorded
#: here so the exclusion is a decision rather than an oversight. `tests/` spells values
#: out on purpose: a test that imports the thing it checks proves nothing.
SEARCH_DIRS = ["football", "football_blog", "refresh", "serving", "live", "surfaces",
               "commentary", "scripts"]


def _redefines_the_vocabulary(source: str) -> bool:
    """True if this module builds a status collection *in code*.

    Parsed rather than grepped: the first version of this test regexed the raw text and
    tripped over `football_blog/__init__.py`, whose comment *quotes* the old inlined
    literal while explaining why it is gone. Prose about a constant is not a copy of it,
    and a guard that cannot tell the difference gets deleted the first time it lies.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            vals = {e.value for e in node.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            if {"FT", "AET"} <= vals:
                return True
    return False


#: `football_blog/types.py` is exempt, and the exemption is narrow. Its `FixtureStatus`
#: is a `typing.Literal` — a *static* type, which cannot be built from a runtime tuple in
#: a form a type checker accepts. It also is not this vocabulary: it enumerates the
#: statuses that appear in Published Store rows, including `NS` and the mixed-case `Canc`
#: the provider sometimes returns, and its docstring pins it to the blog's `types.ts`
#: rather than to CONTEXT.md.
ALLOWED = ["football/status.py", "football_blog/types.py"]


def test_the_vocabulary_is_defined_in_exactly_one_place():
    hits = [
        str(py.relative_to(REPO_ROOT))
        for d in SEARCH_DIRS
        for py in (REPO_ROOT / d).rglob("*.py")
        if "__pycache__" not in py.parts and _redefines_the_vocabulary(py.read_text())
    ]
    assert sorted(hits) == sorted(ALLOWED), (
        f"the status vocabulary should be defined once, in football/status.py; "
        f"also built in {sorted(set(hits) - set(ALLOWED))}"
    )


def test_terminal_is_final_plus_the_dataless_ends():
    """The relationship CONTEXT.md states, asserted rather than assumed."""
    assert status.TERMINAL == status.FINAL + status.ENDED_WITHOUT_RESULT
    assert set(status.FINAL).isdisjoint(status.ENDED_WITHOUT_RESULT)


def test_suspended_is_live_not_terminal():
    """Pinned because CONTEXT.md and the code disagree, and the code won.

    The glossary groups `SUSP` with the terminal-but-dataless states; the Viewer treats a
    suspended match as live, since it may resume, and the Refresh button therefore keeps
    polling it. Consolidating the definitions preserved that behaviour rather than
    quietly changing which fixtures get polled. If the disagreement is ever settled the
    other way, this test is the thing that should fail first.
    """
    assert "SUSP" in status.LIVE
    assert "SUSP" not in status.TERMINAL
