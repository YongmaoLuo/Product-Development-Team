"""VP-024 / acceptance marker completeness meta-test.

Contract: every acceptance marker registered in ``backend/pytest.ini``
must have at least one collected test case under the ``backend/tests/``
tree.

If a marker is added to the registry but no real test in this repo
uses it, the marker is effectively dead — ``pytest --strict-markers``
will not catch that (it only checks that the *name* is registered,
not that any test references it).  This meta-test closes that gap.

The registry is read from ``pytest.ini`` rather than hardcoded.  This
file is shared by two repos whose registries legitimately differ: the
public one drops ``acceptance_5``, whose only consumers live in
``backend/tests/regression/`` and import the private ``tools/``
subsystem.  A hardcoded tuple would force the two registries to agree —
the one thing that is allowed to differ — and silently contradict the
paragraph above.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent  # backend/


def _registered_acceptance_markers(pytest_ini: Path) -> tuple[str, ...]:
    """Return the ``acceptance_*`` markers declared in *pytest_ini*."""
    text = pytest_ini.read_text(encoding="utf-8")
    return tuple(
        sorted(
            set(re.findall(r"^\s*(acceptance_\d+)\s*:", text, re.MULTILINE))
        )
    )


ACCEPTANCE_MARKERS = _registered_acceptance_markers(_BACKEND_DIR / "pytest.ini")


def _collect_marker_usages(tests_root: Path) -> dict:
    """Walk ``backend/tests/`` and collect decorator / pytest.mark usages.

    Returns ``{marker_name: [relative_file_path, ...]}``.
    """
    usages: dict = {m: [] for m in ACCEPTANCE_MARKERS}
    # Derived from the registry, not a hardcoded ``[1-6]`` range: a marker
    # registered outside that range would otherwise be counted as "has no
    # cases" no matter how many tests used it.
    pattern = re.compile(
        r"pytest\.mark\.("
        + "|".join(re.escape(m) for m in ACCEPTANCE_MARKERS)
        + r")\b"
    )
    for path in tests_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for marker in pattern.findall(text):
            usages.setdefault(marker, []).append(
                str(path.relative_to(tests_root.parent))
            )
    return usages


@pytest.fixture(scope="module")
def acceptance_marker_usages() -> dict:
    backend_tests = Path(__file__).resolve().parent.parent  # backend/tests/
    return _collect_marker_usages(backend_tests)


@pytest.mark.parametrize("marker_name", ACCEPTANCE_MARKERS)
def test_acceptance_marker_has_at_least_one_case(
    marker_name: str, acceptance_marker_usages: dict
) -> None:
    """Each acceptance_* marker must appear on >=1 collected test case."""
    files = acceptance_marker_usages.get(marker_name, [])
    assert files, (
        f"marker {marker_name!r} is registered in backend/pytest.ini "
        f"but no test under backend/tests/ references it; "
        f"add at least one @pytest.mark.{marker_name} test or "
        f"remove the marker from the registry"
    )


def test_the_registry_was_actually_read() -> None:
    """Guard the guard: a failed registry read must not pass vacuously.

    ``ACCEPTANCE_MARKERS`` drives both the parametrisation and the usage
    scan.  If the ``pytest.ini`` parse silently returned nothing, every
    parametrised case would disappear and this whole gate would report
    success while checking nothing — the one failure mode a completeness
    gate must not be able to have.
    """
    assert ACCEPTANCE_MARKERS, (
        f"parsed no acceptance_* markers from {_BACKEND_DIR / 'pytest.ini'}; "
        "the registry reader is broken and this gate is vacuous"
    )
