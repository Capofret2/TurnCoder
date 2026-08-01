"""Shared pytest setup for the TurnCoder suite.

The repository root has to be on sys.path before `import api.*` resolves.
pytest's default prepend import mode inserts the *test file's* directory — that
is tests/, one level too deep — and there is no packaging step that would put
the project on the path instead.

This lives here rather than in a root pytest.ini on purpose: export_snapshot in
app.py already ships the tests directory (see 'tests' in _update_dirs) while
_root_files does not list any ini file, so keeping the fix inside tests/ means a
deployed copy can still run its own suite.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope='session')
def repo_root():
    """Absolute path to the repository root."""
    return ROOT


@pytest.fixture(scope='session')
def read_text(repo_root):
    """Read a repo-relative text file. Used by the static-asset checks."""
    def _read(rel):
        return (repo_root / rel).read_text(encoding='utf-8')
    return _read