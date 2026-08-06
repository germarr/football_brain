"""Canonical team-name comparison across providers — the one definition (ADR 0039).

Three providers name the same club three ways, systematically rather than arbitrarily:

    'Charlotte FC'      vs 'Charlotte'            — a club-type suffix
    'Pumas UNAM'        vs 'U.N.A.M. - Pumas'     — acronym dots, and word order
    'Atletico San Luis' vs 'Atlético de San Luis' — an accent

This started in `commentary.fixture_link`, where it *gates* a link: an ESPN Narrated
Match whose kickoff agrees only within tolerance must be anchored by an exactly-agreeing
team name, and canonical agreement is what "exactly" means there (ADR 0030/0038/0039).

It is kernel rather than commentary-internal because a second provider now needs it for a
different purpose. `football_blog.kalshi` uses it only to **propose** Kalshi team-registry
entries for human review — never to accept one, since ADR 0041 makes the registry the
sole bridge. Two uses, two confidence levels, one algorithm: a private copy in the second
caller would drift silently and nothing would fail, which is the argument `football.status`
already won.

Like `football.status`, this imports nothing from the project.
"""
from __future__ import annotations

import re
import unicodedata

#: Dropped before comparing. Deliberately tiny. A longer list — `club`, `deportivo`,
#: `atletico`, `real` — merges Corinthians with Corinthians USA and Lyon with Club De
#: Lyon. `fc`/`cf`/`sc` carry no such freight: no pair of distinct clubs in the store is
#: told apart by them alone.
CLUB_TOKENS = frozenset({"fc", "cf", "sc"})


def canonical(name: str | None) -> str:
    """Canonical form of a team name, for comparing two providers' spellings.

    Accents folded away, dots *inside* an acronym deleted rather than spaced (or
    `U.N.A.M.` becomes four one-letter tokens), remaining punctuation turned into space,
    `CLUB_TOKENS` dropped, and the rest compared as an order-insensitive set — returned
    as a sorted string so callers can still print what agreed.

    Digits are kept, which is what keeps a reserve side distinct from its first team
    ('Toronto FC II' does not become 'Toronto FC'). Suffixes are dropped wherever they
    appear, not just at the end, because ESPN writes 'FC Cincinnati' where API-Football
    writes 'Cincinnati FC'.

    This reconciles a **respelling**, never an **alternate name**: 'Atlético de San Luis'
    against 'Atletico San Luis' agrees, but against 'San Luis' it does not, and that is
    the intended limit rather than a gap (ADR 0039).
    """
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    # `u.n.a.m.` -> `unam`: only dots bounded by single letters, so `St. Louis` keeps
    # its word break and does not become `stlouis`.
    folded = re.sub(r"(?<=\b\w)\.(?=\w\b)", "", folded).replace(".", " ")
    tokens = re.sub(r"[^a-z0-9]+", " ", folded).split()
    kept = [t for t in tokens if t not in CLUB_TOKENS]
    # A club named only for its type ('FC') would otherwise normalise to nothing and
    # compare equal to every other such name.
    return " ".join(sorted(kept or tokens))
