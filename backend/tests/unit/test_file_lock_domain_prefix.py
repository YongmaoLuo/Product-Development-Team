"""Does a declared directory domain cover a file written under it? (P1-3)

A task may declare its write domain as a directory (``src/a``) while
another declares a single file inside it (``src/a/b.py``). Those two are
the *same bytes*, so the plan gate has to reject them as a conflict — but
the literal strings differ, so anything comparing domains by equality
lets the pair through.

The comparison is lexical on purpose: no ``Path.resolve()``, no
``os.stat``. The domain strings come from a plan, not from a checkout, and
a task routinely names a directory that does not exist yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from file_lock_manager import domains_overlap  # noqa: E402


def test_prefix_overlap():
    """A directory domain covers a file written under it, in both orders."""
    assert domains_overlap('src/a', 'src/a/b.py') is True
    assert domains_overlap('src/a/b.py', 'src/a') is True
    # Nested directories overlap too, not just file-under-directory.
    assert domains_overlap('src/a', 'src/a/b/c.py') is True
    assert domains_overlap('src/a/b', 'src/a') is True


def test_literal_similar_not_overlap():
    """Prefix must land on a separator, not merely on a shared name.

    ``src/ab/c.py`` starts with the characters ``src/a`` but is a
    different directory: a string ``startswith`` without the separator
    check calls this a conflict and refuses a perfectly parallel pair of
    tasks.
    """
    assert domains_overlap('src/a', 'src/ab/c.py') is False
    assert domains_overlap('src/ab', 'src/a/b.py') is False
    assert domains_overlap('src/ab', 'src/abc') is False
    # Unrelated siblings stay unrelated.
    assert domains_overlap('src/a', 'src/b/c.py') is False


def test_normalization():
    """``./``, ``..`` and absolute prefixes are resolved before comparing."""
    # '.' segments and trailing separators carry no domain information.
    assert domains_overlap('./src/a', 'src/a/b.py') is True
    assert domains_overlap('src/a/', 'src/a') is True
    assert domains_overlap('src//a', 'src/a/b.py') is True
    # '..' climbs: back into the same directory, and out to a shared parent.
    assert domains_overlap('src/a/../a', 'src/a/b.py') is True
    assert domains_overlap('src/b/../c', 'src/a/x.py') is False
    # Absolute paths normalise the same way, and stay anchored at their root.
    assert domains_overlap('/repo/src/a', '/repo/src/a/b.py') is True
    assert domains_overlap('/repo/src/a', '/other/src/a/b.py') is False
    # Identical domains overlap trivially.
    assert domains_overlap('src/a', 'src/a') is True
    # An empty domain constrains nothing, so it conflicts with nothing.
    assert domains_overlap('', 'src/a') is False
    assert domains_overlap('src/a', '') is False
    assert domains_overlap('', '') is False