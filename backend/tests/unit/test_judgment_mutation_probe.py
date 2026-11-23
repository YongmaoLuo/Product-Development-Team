"""The mutation negative control: a table that cannot go red proves nothing.

Why this file exists
--------------------
§4.4's fourth rule and §12.7: a decision table has to carry weight,
and the only honest way to show that it does is to break the function
on purpose and watch the table notice.

The probe does three things, and the order is the point:

1. change one line of the module under test — the smallest change
   that flips the function's conclusion;
2. run the table's test file and record whether the table went red;
3. put the module back, byte for byte, and check the digest.

Step 3 is what makes step 1 safe to run against a real tree.  A probe
that leaves a mutated file behind has changed the thing it was
measuring, and the next run would measure the mutated file instead of
the real one.  So the restore is verified by hash (§12.7) rather than
by eyeballing a diff.

The three contracts this file names:

* ``test_mutation_makes_table_red`` — a mutation that flips the
  function's conclusion makes the table red.
* ``test_source_restored_with_same_sha256`` — after the probe the
  module is byte-identical to what it was, and its digest is the
  digest it had before.
* ``test_unaffected_mutation_is_not_red`` — a mutation that does not
  change the conclusion leaves the table green.  The probe reports
  what it saw; it does not announce a red that did not happen.

The script is loaded by path because ``scripts/`` is not a package on
``sys.path`` — the same reason ``test_census_determinism.py`` loads
its script that way.  It is loaded lazily, from inside the test that
needs it, so that a missing script fails the tests that asked for it
instead of the whole collection.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

#: The repository root, four parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
#: The mutation probe script under test.
_SCRIPT = _REPO_ROOT / "scripts" / "judgment_mutation_probe.py"

#: The registered name is unique to this file, so nothing else can
#: collide with it.  ``dataclasses`` resolves a class's module through
#: ``sys.modules`` while the class body runs, so the name has to be in
#: ``sys.modules`` before the script is executed.
_MODULE_NAME = "judgment_mutation_probe_under_test"

#: The module the probe mutates.  ``is_even`` is the function under
#: test; ``unused_helper`` is there to be mutated *without* changing
#: what the table sees.
_MODULE_SOURCE = """\
def is_even(value: int) -> bool:
    if value < 0:
        return False
    return value % 2 == 0


def unused_helper() -> int:
    return 42
"""

#: The table that pins ``is_even``.  It puts its own directory on
#: ``sys.path`` so it can import the module next to it, which keeps the
#: fixture independent of how pytest happens to arrange the path.
_TEST_SOURCE = """\
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from t import is_even  # noqa: E402


def test_even_number_is_even():
    assert is_even(2) is True


def test_odd_number_is_not_even():
    assert is_even(3) is False
"""

#: A table that never finishes, for the timeout rule.
_SLOW_TEST_SOURCE = """\
import time


def test_never_finishes():
    time.sleep(30)
"""

#: The line of ``_MODULE_SOURCE`` that decides whether a number is
#: even.  Flipping it makes the table red.
_EVEN_LINE = 4
#: The line of ``_MODULE_SOURCE`` that nothing reads.  Flipping it
#: leaves the table green.
_UNUSED_LINE = 8

#: The text the probe replaces on ``_EVEN_LINE``, and what it puts
#: there instead.
_EVEN_OLD = "value % 2 == 0"
_EVEN_NEW = "value % 2 != 0"

#: The text the probe replaces on ``_UNUSED_LINE``, and what it puts
#: there instead.
_UNUSED_OLD = "return 42"
_UNUSED_NEW = "return 43"

#: The loaded script, kept between tests.
_PROBE = None


def _probe():
    """Load the probe script by path, once, and hand it back.

    The script is missing in the RED state of this file, so the load
    fails the test that asked for it — with a message that names the
    file — rather than the whole collection.
    """
    global _PROBE
    if _PROBE is None:
        if not _SCRIPT.exists():
            raise AssertionError(
                f"the mutation probe script is not there: {_SCRIPT}"
            )
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)
        _PROBE = module
    return _PROBE


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """A module under test and the table that pins it."""
    module = tmp_path / "t.py"
    module.write_text(_MODULE_SOURCE, encoding="utf-8")
    test = tmp_path / "test_t.py"
    test.write_text(_TEST_SOURCE, encoding="utf-8")
    return module, test


# ---------------------------------------------------------------------------
# The three cases the material names
# ---------------------------------------------------------------------------


def test_mutation_makes_table_red(tree: tuple[Path, Path]):
    """A mutation that flips the conclusion makes the table red.

    This is the whole point of the probe.  A table that stays green
    under a mutation that reverses the function's answer is a table
    that cannot fail, and §4.4's fourth rule says such a table does
    not count as a pin.
    """
    module, test = tree
    probe = _probe()

    result = probe.run_probe(
        module=module,
        line=_EVEN_LINE,
        old=_EVEN_OLD,
        new=_EVEN_NEW,
        test=test,
    )

    assert result.red is True
    assert result.returncode not in (0, None), (
        "the table was supposed to fail, but pytest exited "
        f"{result.returncode}:\n{result.stdout}\n{result.stderr}"
    )


def test_source_restored_with_same_sha256(tree: tuple[Path, Path]):
    """After the probe the module is exactly what it was.

    Both halves are checked: the bytes on disk are the bytes the
    probe read, and the digest of the file is the digest taken before
    the mutation.  The digest is the half that matters for the report
    (§12.7) — a byte comparison that passes because both sides were
    read from the same buffer would prove nothing.
    """
    module, test = tree
    probe = _probe()
    before = module.read_bytes()
    before_sha = hashlib.sha256(before).hexdigest()

    result = probe.run_probe(
        module=module,
        line=_EVEN_LINE,
        old=_EVEN_OLD,
        new=_EVEN_NEW,
        test=test,
    )

    after = module.read_bytes()
    assert after == before, "the probe left the module changed"
    assert hashlib.sha256(after).hexdigest() == before_sha
    assert result.restored is True
    assert result.sha256_match is True
    assert result.sha256_before == before_sha
    assert result.sha256_after == before_sha


def test_unaffected_mutation_is_not_red(tree: tuple[Path, Path]):
    """A mutation that changes nothing leaves the table green.

    The other half of the contract: a probe that always says "red"
    would pass the first test without ever having run a table.  So a
    mutation outside the table's reach has to be reported as *not*
    red, and the run has to have finished normally.
    """
    module, test = tree
    probe = _probe()

    result = probe.run_probe(
        module=module,
        line=_UNUSED_LINE,
        old=_UNUSED_OLD,
        new=_UNUSED_NEW,
        test=test,
    )

    assert result.red is False
    assert result.timed_out is False
    assert result.returncode == 0, (
        "the table was supposed to stay green, but pytest exited "
        f"{result.returncode}:\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# The rules around the mutation itself
# ---------------------------------------------------------------------------


def test_line_without_the_old_text_is_refused_and_nothing_is_written(
    tree: tuple[Path, Path],
):
    """A line that does not contain the text is refused before any write.

    The probe is pointed at a line by number, so a wrong number is
    easy to make.  Writing the mutation anyway would change a line
    the caller never meant to change; refusing leaves the tree as it
    was, which is the only safe answer.
    """
    module, test = tree
    probe = _probe()
    before = module.read_bytes()

    with pytest.raises(ValueError):
        probe.run_probe(
            module=module,
            line=_EVEN_LINE,
            old="this text is not on the line",
            new="replacement",
            test=test,
        )

    assert module.read_bytes() == before


def test_line_outside_the_module_is_refused(tree: tuple[Path, Path]):
    """A line number past the end of the module is refused."""
    module, test = tree
    probe = _probe()
    before = module.read_bytes()

    with pytest.raises(ValueError):
        probe.run_probe(
            module=module,
            line=9999,
            old=_EVEN_OLD,
            new=_EVEN_NEW,
            test=test,
        )

    assert module.read_bytes() == before


def test_timeout_is_not_red(tmp_path: Path):
    """A run that never finishes is not evidence of a red.

    A timeout says the table hung; it says nothing about whether the
    table can fail.  Counting it as a red would let a table that never
    finishes pass the negative control.
    """
    module = tmp_path / "t.py"
    module.write_text(_MODULE_SOURCE, encoding="utf-8")
    test = tmp_path / "test_slow.py"
    test.write_text(_SLOW_TEST_SOURCE, encoding="utf-8")
    probe = _probe()

    result = probe.run_probe(
        module=module,
        line=_EVEN_LINE,
        old=_EVEN_OLD,
        new=_EVEN_NEW,
        test=test,
        timeout=2.0,
    )

    assert result.timed_out is True
    assert result.red is False


def test_missing_module_is_refused(tmp_path: Path):
    """A module that is not there is refused, not reported as a red."""
    probe = _probe()
    test = tmp_path / "test_t.py"
    test.write_text(_TEST_SOURCE, encoding="utf-8")

    with pytest.raises(ValueError):
        probe.run_probe(
            module=tmp_path / "not_here.py",
            line=_EVEN_LINE,
            old=_EVEN_OLD,
            new=_EVEN_NEW,
            test=test,
        )


def test_a_directory_that_is_not_there_is_refused(tree: tuple[Path, Path]):
    """A ``cwd`` that does not exist is refused before anything runs."""
    module, test = tree
    probe = _probe()

    with pytest.raises(ValueError):
        probe.run_probe(
            module=module,
            line=_EVEN_LINE,
            old=_EVEN_OLD,
            new=_EVEN_NEW,
            test=test,
            cwd=module.parent / "nowhere",
        )


def test_a_node_id_selects_the_case_it_names(tree: tuple[Path, Path]):
    """``file.py::case`` runs that case and not the others.

    The mutation makes every number even, so the odd-number case
    fails and the even-number case still passes.  The same mutation is
    therefore red over the whole file and green over one case — which
    is only true if the node id was honoured.
    """
    module, test = tree
    probe = _probe()

    whole_file = probe.run_probe(
        module=module,
        line=_EVEN_LINE,
        old=_EVEN_OLD,
        new="True",
        test=test,
    )
    one_case = probe.run_probe(
        module=module,
        line=_EVEN_LINE,
        old=_EVEN_OLD,
        new="True",
        test=f"{test}::test_even_number_is_even",
    )

    assert whole_file.red is True
    assert one_case.red is False
    assert one_case.returncode == 0


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_the_table_goes_red(
    tree: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
):
    """The CLI says the table went red and exits 0."""
    module, test = tree
    probe = _probe()

    code = probe.main(
        [
            "--module",
            str(module),
            "--line",
            str(_EVEN_LINE),
            "--old",
            _EVEN_OLD,
            "--new",
            _EVEN_NEW,
            "--test",
            str(test),
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "RED: yes" in out
    assert "SHA256: match" in out


def test_main_returns_one_when_the_table_stays_green(
    tree: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
):
    """The CLI says the table stayed green and exits 1.

    The exit code is the gate: a negative control that did not bite
    has to fail the command, or the report would carry a row that
    says "RED" only because the probe said so.
    """
    module, test = tree
    probe = _probe()

    code = probe.main(
        [
            "--module",
            str(module),
            "--line",
            str(_UNUSED_LINE),
            "--old",
            _UNUSED_OLD,
            "--new",
            _UNUSED_NEW,
            "--test",
            str(test),
        ]
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "RED: no" in out


def test_main_returns_two_when_the_mutation_cannot_be_made(
    tree: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
):
    """A refused mutation exits 2 and says why on stderr."""
    module, test = tree
    probe = _probe()

    code = probe.main(
        [
            "--module",
            str(module),
            "--line",
            str(_EVEN_LINE),
            "--old",
            "this text is not on the line",
            "--new",
            "replacement",
            "--test",
            str(test),
        ]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "ERROR" in captured.err


def test_main_returns_two_when_the_run_times_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """A run that never finishes exits 2 — not 1.

    A timeout says the table hung; it says nothing about whether the
    table can fail.  Reporting it as "the table stayed green" would
    turn a hanging table into a false negative control: the report
    would carry a row saying the table does not carry weight, when
    what actually happened is that nobody found out.  So the CLI has
    to keep the two apart — 2 for "this run cannot be read as an
    answer", 1 for "the table stayed green".
    """
    module = tmp_path / "t.py"
    module.write_text(_MODULE_SOURCE, encoding="utf-8")
    test = tmp_path / "test_slow.py"
    test.write_text(_SLOW_TEST_SOURCE, encoding="utf-8")
    probe = _probe()

    code = probe.main(
        [
            "--module",
            str(module),
            "--line",
            str(_EVEN_LINE),
            "--old",
            _EVEN_OLD,
            "--new",
            _EVEN_NEW,
            "--test",
            str(test),
            "--timeout",
            "0.5",
        ]
    )

    captured = capsys.readouterr()
    assert code == 2, (
        "a timed-out run cannot be read as 'the table stayed green', so "
        f"it must exit 2, not 1:\n{captured.out}\n{captured.err}"
    )
    assert "TIMED_OUT: yes" in captured.out
    assert "SHA256: match" in captured.out
    assert captured.err.strip(), "the reason has to be on stderr"


# ---------------------------------------------------------------------------
# The fixture the tests mutate
# ---------------------------------------------------------------------------


def test_the_fixture_lines_are_the_lines_the_probe_is_pointed_at():
    """The line numbers above name the lines they claim to name.

    The fixture is the whole negative control, so a fixture that
    drifted — one blank line added, say — would turn every mutation
    into a no-op and the probe would report "not red" for the wrong
    reason.
    """
    lines = _MODULE_SOURCE.splitlines()

    assert _EVEN_OLD in lines[_EVEN_LINE - 1]
    assert _UNUSED_OLD in lines[_UNUSED_LINE - 1]
    assert "is_even" in lines[0]
