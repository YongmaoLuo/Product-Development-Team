"""The host-load gate is asked in one place, and the wall-clock cases use it.

Why this file exists
--------------------
The performance lanes assert on wall-clock numbers, and on a host that is
already carrying someone else's work those numbers measure the machine rather
than the code under test.  So the question "is this host too loaded to measure
on?" is asked *before* the bound is asserted — and it has to be asked the same
way everywhere, because a decision that is made twice is a decision that
drifts.

There is therefore one gate (``backend/tests/_host_load.py``), one place that
asks it (``backend/tests/conftest.py``), and the wall-clock cases ask it too
instead of reading the load average themselves.

What this pins
--------------
1. A case marked ``perf`` or ``time_sensitive`` is gated by the shared helper;
   a case with neither marker is not — it is not measuring the clock, so a
   busy host is not its problem.
2. CI is never gated.  A runner's load is not the runner's own doing, and a
   lane that quietly skipped would report green while asserting nothing.
3. The gate is reached from an autouse fixture, so a marked case cannot forget
   to ask.
4. The wall-clock cases no longer read the load average themselves: one
   module reads it, so one limit and one message format apply.
5. At least six wall-clock modules ask the shared gate, and none of them
   keeps a gate of its own.  One gate means one limit and one message; a
   second gate answering the same question is a second answer that drifts.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tests import _host_load  # noqa: E402

#: The conftest module, imported the way a test may import it: the gate is
#: asked from there, so that is where it is read from.
_conftest = importlib.import_module("tests.conftest")

#: The cases that used to decide for themselves whether the host was too busy.
_WALL_CLOCK_FILES = (
    Path("tests") / "perf" / "plan_scheduler" / "test_scheduler_perf_budget.py",
    Path("tests") / "performance" / "test_backend_startup_perf.py",
    Path("tests") / "perf" / "test_hitl_concurrency.py",
    Path("tests") / "perf" / "test_kernel_scale.py",
    Path("tests") / "perf" / "test_perf_verification.py",
    Path("tests") / "perf" / "test_repo_scheduler_refiner_watchdog.py",
    Path("tests") / "perf" / "test_verification_mode_scaling.py",
)

#: How many of the wall-clock modules have to ask the shared gate.
#:
#: The PRD asks for six.  The perf directory holds seven, and the number is
#: a floor rather than an exact count: a module that stops measuring the
#: clock may drop out of the list, but a module that starts measuring it
#: has to join it.
_MIN_ADOPTERS = 6

#: The import every adopting module has to use to reach the shared gate.
_ADOPTION_IMPORT = "from tests import _host_load"

#: The call every adopting module has to make.
_ADOPTION_CALL = "_host_load.skip_reason("

#: The name of the constant that the local, parallel gate used to define.
#:
#: It is spelled in two pieces on purpose: this file is itself scanned for
#: the token, and a file that carried the whole of it would look like the
#: second gate it is here to forbid.
_PARALLEL_GATE_CONSTANT = "HOST_PROBE_MAX_SLEEP" + "_SEC"

#: A module-level constant that names a load limit of the module's own.
_LOCAL_LOAD_LIMIT = re.compile(r"^\s*[A-Z][A-Z0-9_]*LOAD[A-Z0-9_]*\s*=", re.MULTILINE)


class _FakeMarker:
    """What ``iter_markers`` yields: pytest's ``Mark``, reduced to its name."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeItem:
    """A test item, reduced to the one thing the gate reads: its markers."""

    def __init__(self, markers: Iterable[str]) -> None:
        self._markers: List[_FakeMarker] = [_FakeMarker(name) for name in markers]

    def iter_markers(self) -> Iterable[_FakeMarker]:
        return iter(self._markers)


def _a_loaded_host(monkeypatch: pytest.MonkeyPatch, load: float = 55.0) -> None:
    """Make the host look busier than the gate's limit allows.

    The load is injected rather than produced: a test that can only fail on a
    busy machine is not a test.  The limit and the CI flag are cleared for the
    same reason — the answer being asserted must not depend on the machine
    the suite happens to run on.
    """
    monkeypatch.setattr(os, "getloadavg", lambda: (load, load, load))
    monkeypatch.delenv(_host_load.HOST_LOAD_LIMIT_ENV, raising=False)
    monkeypatch.delenv(_host_load.CI_ENV, raising=False)


def test_a_perf_item_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A case that measures wall-clock time is skipped on a loaded host."""
    _a_loaded_host(monkeypatch)

    reason = _conftest._host_load_skip_reason(_FakeItem(["perf"]))

    assert reason == "skipped: host load 55.0 >= 20", (
        "a perf case on a host at load 55 over a limit of 20 must be skipped, "
        "and the reason must carry both numbers: %r" % (reason,)
    )


def test_a_time_sensitive_item_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other marker that says "this case measures the clock"."""
    _a_loaded_host(monkeypatch)

    reason = _conftest._host_load_skip_reason(_FakeItem(["time_sensitive"]))

    assert reason == "skipped: host load 55.0 >= 20", (
        "a time_sensitive case on a host at load 55 must be skipped: %r"
        % (reason,)
    )


def test_an_unmarked_item_is_not_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A case that does not measure the clock is measured as usual.

    The gate is about the clock, not about the host: skipping every case on a
    busy machine would turn a busy afternoon into an empty report.
    """
    _a_loaded_host(monkeypatch)

    assert _conftest._host_load_skip_reason(_FakeItem([])) is None


def test_ci_keeps_the_marked_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI is never gated, however loaded the runner is.

    A hosted runner's load is not the runner's own doing, and a lane that
    quietly skipped would report green while asserting nothing at all.
    """
    _a_loaded_host(monkeypatch)
    monkeypatch.setenv(_host_load.CI_ENV, "true")

    assert _conftest._host_load_skip_reason(_FakeItem(["perf"])) is None


def test_a_case_with_both_markers_is_skipped_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carrying both markers is still one skip, with the same message.

    The gate is asked once per case, so a case that carries both markers
    cannot be skipped twice over — and the reason does not change with the
    number of markers.
    """
    _a_loaded_host(monkeypatch)

    reason = _conftest._host_load_skip_reason(_FakeItem(["perf", "time_sensitive"]))

    assert reason == "skipped: host load 55.0 >= 20", (
        "both markers, one skip, one message: %r" % (reason,)
    )


def test_the_fixture_is_registered_as_autouse() -> None:
    """The gate is reached without a case having to remember to ask.

    A fixture that has to be requested by name is a fixture that some case
    will forget, and a case that forgets measures the machine.
    """
    source = (BACKEND_DIR / "tests" / "conftest.py").read_text(encoding="utf-8")
    lines = source.splitlines()

    definitions = [
        index
        for index, line in enumerate(lines)
        if line.startswith("def _host_load_gate")
    ]

    assert len(definitions) == 1, (
        "there must be exactly one _host_load_gate fixture: %r" % (definitions,)
    )

    index = definitions[0]
    assert lines[index - 1].strip() == "@pytest.fixture(autouse=True)", (
        "the fixture must be registered with @pytest.fixture(autouse=True); "
        "the line before it is %r" % (lines[index - 1],)
    )


def test_the_inline_host_load_check_is_gone() -> None:
    """The wall-clock cases ask the shared gate instead of reading the load.

    One module reads the load average, so one limit and one message format
    apply to every case that measures the clock.
    """
    for relative in _WALL_CLOCK_FILES:
        source = (BACKEND_DIR / relative).read_text(encoding="utf-8")

        assert "os.getloadavg" not in source, (
            "%s still reads the load average itself" % (relative,)
        )
        assert "_host_load" in source, (
            "%s does not ask the shared gate" % (relative,)
        )


def test_at_least_six_wall_clock_modules_ask_the_shared_gate() -> None:
    """The perf directory asks the one gate, in the one way.

    A module that measures the clock asks the shared gate before it
    asserts a bound: the import reaches the helper, and the call asks it.
    Six modules have to do this — the PRD's floor — so that a module
    cannot quietly grow a gate of its own without the count falling below
    what was agreed.
    """
    adopters = []
    for relative in _WALL_CLOCK_FILES:
        source = (BACKEND_DIR / relative).read_text(encoding="utf-8")
        if _ADOPTION_IMPORT in source and _ADOPTION_CALL in source:
            adopters.append(relative)

    assert len(adopters) >= _MIN_ADOPTERS, (
        "at least %d wall-clock modules must ask the shared gate; only %d of "
        "%d do: %r"
        % (
            _MIN_ADOPTERS,
            len(adopters),
            len(_WALL_CLOCK_FILES),
            [str(relative) for relative in _WALL_CLOCK_FILES],
        )
    )


def test_no_wall_clock_module_keeps_a_gate_of_its_own() -> None:
    """A second gate answering the same question is a second answer.

    The local gate this replaced had its own threshold, its own message and
    its own idea of when a host was busy — and it had no CI exception, so a
    busy CI runner skipped a case that the shared gate would have asserted.
    None of that may come back.
    """
    for relative in _WALL_CLOCK_FILES:
        source = (BACKEND_DIR / relative).read_text(encoding="utf-8")

        assert _PARALLEL_GATE_CONSTANT not in source, (
            "%s still defines the local, parallel gate's threshold" % (relative,)
        )
        assert not _LOCAL_LOAD_LIMIT.search(source), (
            "%s still defines a load limit of its own; the limit belongs to "
            "the shared gate" % (relative,)
        )
