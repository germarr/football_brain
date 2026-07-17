"""ESPN match commentary: cache-first fetch + typed keyEvents join.

Exploratory, standalone source (see commentary/README.md). Writes nothing to
football.db; the only on-disk side effect is the raw cache under data/raw/.
"""
from __future__ import annotations

from .espn import fetch_summary
from .join import build_match, join_commentary

__all__ = ["fetch_summary", "build_match", "join_commentary"]
