"""Every path this package needs, resolved from one anchor (ADR 0031).

Before this module, each consumer resolved its own data relative to its *own*
``__file__``: ``config.py`` found ``competitions.json`` beside itself, ``venues.py``
found ``venues.json`` beside itself. That coupled data layout to code layout, and it
failed silently — ``venues._read()`` returns ``[]`` for an absent file and ``_write()``
then creates a fresh one, so moving the module without its data orphans the committed
registry, re-fetches ~8k venues against a paid API, and leaves the nightly
``git add`` with nothing to commit, without raising anywhere.

Anchoring every path here decouples the two permanently: modules may be reorganized
into subpackages without any registry moving, because none of them resolves its own
location any more.

Note that this module deliberately holds *paths only* and imports nothing from the
package, so it can be imported from anywhere — including a preflight that runs before
the rest of the package is known to be importable.
"""
from __future__ import annotations

from pathlib import Path

#: This package (``…/football``) and the repository that contains it.
PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent

ENV_FILE = REPO_ROOT / ".env"
DATA_DIR = REPO_ROOT / "data"

#: Layer 1: the raw response cache (ADR 0002). A symlink to another volume on this
#: machine — see the ADR 0002 addendum; broken, the cache-first fetcher silently
#: misses every key and re-fetches.
RAW_DIR = DATA_DIR / "raw"

#: Layer 2: the modeled SQLite store, disposable and rebuilt from the cache.
DB_PATH = DATA_DIR / "football.db"

#: The Registries — committed input to the pipeline, never its output (CONTEXT.md).
#: Both live here so that code can move without data following it.
REGISTRY_DIR = PACKAGE_ROOT / "registry"

#: Every Competition we collect, leagues and cups alike (ADR 0019).
COMPETITIONS_FILE = REGISTRY_DIR / "competitions.json"

#: The append-only ``(name, city) -> stable id`` map that gives the same stadium the
#: same id in every store (ADR 0028).
VENUES_FILE = REGISTRY_DIR / "venues.json"

#: The Venue merge list: entries judged to be one ground where no rule can decide it
#: (ADR 0042). Spelling duplicates need no row here — they are derived by the fold.
VENUE_MERGES_FILE = REGISTRY_DIR / "venue_merges.json"
