"""Every package directory is a real, tracked package (ADR 0033).

The silent failure this exists for, found the day ADR 0031 landed: `.gitignore` held
an unanchored `build/` rule for Python packaging artifacts, which also matched the new
`football/build/` package. `git mv` stages explicitly and so slipped the moved modules
past it, but a freshly created `football/build/__init__.py` was skipped by `git add -A`
without a word. Nothing failed: `git add` is silent about ignored paths, the tests
passed because Python 3 imports a directory without `__init__.py` as a namespace
package, and the loss would have surfaced only in a fresh clone — or as every
*subsequent* file added to that directory vanishing too.
"""
from __future__ import annotations

import subprocess

import pytest

from tests.conftest import REPO_ROOT

#: Directories that are Python packages and must be committed as such.
PACKAGE_DIRS = [
    "football", "football/onboard", "football/collect", "football/build",
    "football/publish", "console", "commentary", "live", "refresh", "web",
    "football_blog", "football_blog/prompts",
]


def _is_ignored(path: str) -> bool:
    """True if git would ignore `path`. `git check-ignore` exits 0 on a match."""
    return subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=REPO_ROOT,
    ).returncode == 0


@pytest.mark.parametrize("pkg", PACKAGE_DIRS)
def test_package_has_an_init(pkg):
    assert (REPO_ROOT / pkg / "__init__.py").is_file(), f"{pkg} has no __init__.py"


@pytest.mark.parametrize("pkg", PACKAGE_DIRS)
def test_package_is_not_gitignored(pkg):
    """A package directory matching an ignore rule loses every file added to it,
    silently — which is how football/build/__init__.py went missing."""
    assert not _is_ignored(f"{pkg}/"), f"{pkg}/ is gitignored"
    assert not _is_ignored(f"{pkg}/__init__.py"), f"{pkg}/__init__.py is gitignored"


@pytest.mark.parametrize("pkg", PACKAGE_DIRS)
def test_package_init_is_tracked(pkg):
    """On disk and un-ignored is not enough — it has to actually be in the index."""
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", f"{pkg}/__init__.py"],
        cwd=REPO_ROOT, capture_output=True,
    ).returncode == 0
    assert tracked, f"{pkg}/__init__.py exists but is not tracked by git"
