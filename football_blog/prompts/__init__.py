"""LLM prompt templates, and the one function that assembles them.

The templates are kept as .md files so they're diff-friendly and version-controlled
independently of the Python that assembles them.

`assemble_prompt` is that Python, and it is deliberately the *only* way a prompt is
built. Two callers need one: `draft.draft_narrative`, which sends it to the model, and
the Desk, which shows it to a human to edit (ADR 0034). If those two assembled
independently — even from the same pieces, even correctly today — the Desk would
eventually display something other than what it sends, and nothing would report it.
An edited prompt that is not the sent prompt is the failure ADR 0034 names.

The three layers, in the order the model receives them:

  1. `system_{lang}.md`   the voice — a git file, shared by every Publication in that
                          language. Editing it changes every Narrative written in it.
  2. the Publication's    `llm_prompt_overrides`, appended to layer 1. Per-Competition
                          and durable, held in the Editorial Store.
                          (`cup_override_es.md` is a copy-paste *template* for this
                          field — an onboarding aid. Nothing here loads it.)
  3. the user prompt      match facts, timeline, stats, lineups, standouts, commentary
                          — **derived** from the Published Store by `builder.py`, and
                          for that reason the one layer the Desk will not let anyone
                          edit. See ADR 0034.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..types import FullFixture
from .builder import build_user_prompt

_PROMPTS_DIR = Path(__file__).resolve().parent


def load_system_prompt(lang: str) -> str:
    """Layer 1 — the voice, by Publication language."""
    path = _PROMPTS_DIR / f"system_{lang}.md"
    if not path.exists():
        raise ValueError(f"No system prompt for language: {lang}")
    return path.read_text()


def system_prompt_path(lang: str) -> Path:
    """Where layer 1 lives, so the Desk can edit the file rather than a copy of it."""
    return _PROMPTS_DIR / f"system_{lang}.md"


def assemble_prompt(
    bundle: FullFixture,
    lang: str,
    display_timezone: str,
    extra_style: Optional[str] = None,
    instruction: Optional[str] = None,
) -> tuple[str, str]:
    """Build exactly what the model will be given: `(system_prompt, user_prompt)`.

    Pure: reads the two prompt files and the bundle, and nothing else. That is what
    lets the Desk preview a prompt without a network call, a subprocess, or a write —
    and without going anywhere near `pipeline --dry-run`, which runs three stages
    before it prints anything.

    `extra_style` and `instruction` are both operator text and land in different
    halves on purpose: the Publication's standing style joins layer 1 (the voice,
    every Narrative it writes), while a one-off steer joins layer 3 (this match,
    this run). Swapping them would make a passing remark permanent.
    """
    system_prompt = load_system_prompt(lang)
    if extra_style:
        system_prompt = (
            f"{system_prompt}\n\n"
            f"Additional style guidance for this publication:\n{extra_style}"
        )
    return system_prompt, build_user_prompt(bundle, display_timezone, instruction)
