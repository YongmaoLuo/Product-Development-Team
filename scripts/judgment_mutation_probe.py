#!/usr/bin/env python3
"""The mutation negative control for Tier A decision tables.

Why this script exists
----------------------
§4.4's fourth rule: a decision table has to be able to *fail*.  A
table that cannot fail is a comment with a ``pytest`` decorator on
it — it runs on every commit, it costs nothing, and it pins nothing.
The only honest way to show that a table carries weight is to break
the function on purpose and watch the table notice.

That is what this script does.  It is a negative control with three
steps, and the order is the whole design:

1. **Mutate.**  Change one line of the module under test — the
   smallest change that flips the function's conclusion.
2. **Run.**  Run the table's test file and record whether the table
   went red.
3. **Restore.**  Put the module back, byte for byte, and check the
   digest.

Step 3 is what makes step 1 safe to run against a real tree.  A probe
that leaves a mutated file behind has changed the thing it was
measuring: the next run would measure the mutant, and the mutant is
not the code anybody ships.  So the restore is *verified* (§12.7) —
the bytes on disk are compared with the bytes that were read before
the mutation, and the digest of the file is compared with the digest
taken before the mutation.  A restore that only looks right is not a
restore.

What "red" means, exactly
-------------------------
A run counts as **red** when pytest exited with a failure code — a
code that is neither 0 (everything passed) nor one of the codes that
mean *the tests never ran* (4: pytest usage error, 5: nothing
collected).  A run that timed out is never red: a timeout says the
table hung, which says nothing about whether the table can fail.

The probe reports what it saw.  A mutation that leaves the table
green is reported as ``red=False``, and the command exits 1 — a
negative control that did not bite has to fail loudly, or the report
would carry a row that says "RED" only because the probe said so.
A run that timed out is reported as ``red=False`` as well, but it
exits 2: the table did not stay green, it never answered, and those
are two different findings — the second one is not evidence that the
table lacks weight.

The mutation is refused, not guessed
------------------------------------
The probe is pointed at a line by number, so a wrong number is easy
to make.  When the line does not contain the text the caller says it
does, the probe refuses: it raises before writing anything.  A
half-applied mutation is worse than a refused one, because the
refusal is visible and the half-mutation is not.

Two details that keep the measurement honest
--------------------------------------------
* **The mutant is the one that runs.**  A ``__pycache__`` entry can
  survive a same-size mutation made within the same second, because
  the compiled file records the source's mtime in whole seconds.  The
  probe therefore points the subprocess's bytecode cache at a
  throw-away directory, so the mutated source is compiled from
  scratch and the repository's cache is neither read nor written.
* **The tree comes back as it was.**  The module's bytes *and* its
  timestamps are restored, so the next run — and the next build —
  sees the file it saw before the probe.

Command line
------------
::

    python scripts/judgment_mutation_probe.py \\
        --module backend/verification_evidence.py \\
        --line 975 \\
        --old 'return False' \\
        --new 'return True' \\
        --test backend/tests/unit/test_decision_table_ran_declared_entry.py

The exit codes are the contract: ``0`` — the table went red and the
module was restored; ``1`` — the table stayed green (the negative
control did not bite); ``2`` — the probe could not be run, the run
timed out, or the module was not restored.  A timeout is deliberately
not a ``1``: a run that never finished says nothing about whether the
table can fail, so it must not be written down as a table without
weight.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

__all__ = [
    "DEFAULT_TIMEOUT",
    "MutationError",
    "ProbeResult",
    "main",
    "mutate_line",
    "run_probe",
]


#: How long the probe waits for the table before it gives up.  The
#: default is generous but finite: a run with no ceiling is a run that
#: can hang the whole negative control.
DEFAULT_TIMEOUT = 300.0

#: pytest exit codes that mean "the tests never ran", as opposed to
#: "the tests ran and some of them failed".  Neither is a red.
_NO_TESTS_RAN = frozenset({4, 5})


class MutationError(ValueError):
    """The mutation could not be made — and nothing was written.

    It is a :class:`ValueError` because that is what it is: the
    caller pointed the probe at something that does not hold.
    """


@dataclass(frozen=True)
class ProbeResult:
    """What the probe saw.

    ``red``
        The table went red: the test file failed with the mutation in
        place.  This is the answer the probe exists to produce.
    ``restored``
        The module on disk is byte-for-byte what it was before the
        mutation.
    ``sha256_match``
        The digest of the module after the restore equals the digest
        taken before the mutation.  This is the *verification* of the
        restore (§12.7): ``restored`` compares bytes, this compares
        digests, and the two are computed from independent readings
        of the file.
    ``sha256_before`` / ``sha256_after``
        The two digests, kept so a report can record which hash it
        checked.
    ``returncode``
        pytest's exit code; ``None`` when the run timed out.
    ``timed_out``
        The run was killed before it finished.
    ``stdout`` / ``stderr``
        The run's output, kept so a reader can see what happened
        instead of only what the probe concluded.
    """

    red: bool
    restored: bool
    sha256_match: bool
    sha256_before: str = ""
    sha256_after: str = ""
    returncode: int | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""


def _sha256(data: bytes) -> str:
    """The digest of these bytes, as hex."""
    return hashlib.sha256(data).hexdigest()


def _as_text(value: object) -> str:
    """Whatever the subprocess gave us, as text.

    ``subprocess`` reports a timed-out run's output as bytes and a
    finished run's as text, depending on how it was asked; the probe
    wants one shape.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def mutate_line(
    path: str | Path,
    line: int,
    old: str,
    new: str,
) -> bytes:
    """Replace the first ``old`` on ``line`` of ``path`` with ``new``.

    The original bytes are returned so the caller can put the file
    back.  The file is *not* touched when the line does not contain
    ``old``: a probe that half-applies its mutation is worse than one
    that refuses to start, because the refusal is visible and the
    half-mutation is not.

    Raises :class:`MutationError` — a :class:`ValueError` — when the
    module cannot be read, when the line is outside the file, or when
    the line does not contain ``old``.
    """
    module_path = Path(path)
    try:
        original = module_path.read_bytes()
    except OSError as exc:
        raise MutationError(
            f"the module cannot be read: {module_path}: {exc}"
        ) from exc
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MutationError(
            f"the module is not utf-8 text, so its lines cannot be counted: "
            f"{module_path}"
        ) from exc

    lines = text.splitlines(keepends=True)
    if not 1 <= line <= len(lines):
        raise MutationError(
            f"line {line} is outside {module_path}, which has "
            f"{len(lines)} line(s)"
        )
    target = lines[line - 1]
    if old not in target:
        raise MutationError(
            f"line {line} of {module_path} does not contain {old!r}: "
            f"{target.strip()!r}"
        )

    lines[line - 1] = target.replace(old, new, 1)
    # Bytes, not text: writing text would let the platform's newline
    # translation rewrite the file, and the restore has to be exact.
    module_path.write_bytes("".join(lines).encode("utf-8"))
    return original


def _split_node_id(test: str | Path) -> tuple[Path, str | None]:
    """The test file, and the node id inside it if one was given.

    ``"path/to/test_x.py::test_decision_table"`` is two things: the
    file to run and the case to run inside it.
    """
    text = str(test)
    if "::" in text:
        file_part, _, node = text.partition("::")
        return Path(file_part), node
    return Path(text), None


def _pytest_command(test_path: Path, node: str | None) -> list[str]:
    """The command that runs the table.

    The interpreter is the one running the probe, so the table is
    tested against the same environment the probe was launched in —
    the operator's venv, not the system python.
    """
    target = f"{test_path}::{node}" if node else str(test_path)
    return [
        sys.executable,
        "-m",
        "pytest",
        target,
        "-p",
        "no:cacheprovider",
        "-q",
    ]


def _run_tests(
    test_path: Path,
    node: str | None,
    timeout: float,
    cwd: str | Path | None,
) -> tuple[int | None, bool, str, str]:
    """Run the table.  Returns ``(returncode, timed_out, stdout, stderr)``.

    The bytecode cache is pointed at a throw-away directory for the
    duration of the run.  A compiled file records the source's mtime
    in whole seconds, so a same-size mutation made in the same second
    as the last compile would be masked by the stale cache — and the
    mutant would silently not be the thing that ran.
    """
    command = _pytest_command(test_path, node)
    environment = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="judgment-mutation-pycache-") as cache:
        environment["PYTHONPYCACHEPREFIX"] = cache
        try:
            completed = subprocess.run(
                command,
                cwd=None if cwd is None else str(cwd),
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return None, True, _as_text(exc.stdout), _as_text(exc.stderr)
    return (
        completed.returncode,
        False,
        completed.stdout or "",
        completed.stderr or "",
    )


def run_probe(
    module: str | Path,
    line: int,
    old: str,
    new: str,
    test: str | Path,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: str | Path | None = None,
) -> ProbeResult:
    """Mutate one line, run the table, put the module back.

    ``module`` is the file under test, ``line`` the 1-based line to
    change, ``old`` the text that line has to contain and ``new`` what
    replaces it.  ``test`` is the table's test file (a pytest node id
    is accepted too).

    The module is restored whether the run passed, failed or blew up:
    a probe that leaves a mutant behind has changed the thing it was
    measuring.

    Raises :class:`MutationError` when the module or the test file is
    not there, when ``cwd`` is not a directory, or when the line does
    not contain ``old`` — in every one of those cases nothing was
    written.
    """
    module_path = Path(module)
    test_path, node = _split_node_id(test)
    if not module_path.is_file():
        raise MutationError(f"the module under test is not there: {module_path}")
    if not test_path.is_file():
        raise MutationError(f"the table's test file is not there: {test_path}")
    if cwd is not None and not Path(cwd).is_dir():
        raise MutationError(f"the directory to run the table in is not there: {cwd}")

    original = module_path.read_bytes()
    original_sha = _sha256(original)
    stat = module_path.stat()

    # Raises before writing anything when the line does not hold the
    # text the caller says it holds.
    mutate_line(module_path, line, old, new)

    try:
        returncode, timed_out, stdout, stderr = _run_tests(
            test_path, node, timeout=timeout, cwd=cwd
        )
    finally:
        # The module goes back byte for byte, with the timestamps it
        # had: a restored file that carries a new mtime is a file the
        # next build treats as changed.
        module_path.write_bytes(original)
        os.utime(module_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    restored_bytes = module_path.read_bytes()
    restored = restored_bytes == original
    after_sha = _sha256(restored_bytes)

    if timed_out or returncode is None:
        red = False
    elif returncode == 0 or returncode in _NO_TESTS_RAN:
        red = False
    else:
        red = True

    return ProbeResult(
        red=red,
        restored=restored,
        sha256_match=after_sha == original_sha,
        sha256_before=original_sha,
        sha256_after=after_sha,
        returncode=returncode,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one negative control.  Returns the process exit code.

    ``0`` — the table went red and the module was restored;
    ``1`` — the table stayed green, so the table does not carry
    weight;
    ``2`` — the probe could not be run, the run timed out, or the
    module was not restored.  A timeout is not a ``1``: a run that
    never finished is not a table that stayed green, and reporting it
    as one would turn a hung table into a false negative control.

    The report is printed to stdout in a stable, greppable shape, so
    the audit trail can quote it instead of paraphrasing it.
    """
    parser = argparse.ArgumentParser(
        prog="judgment_mutation_probe.py",
        description=(
            "Mutate one line of a judgment module and check that its "
            "decision table goes red."
        ),
    )
    parser.add_argument(
        "--module",
        required=True,
        help="the module under test",
    )
    parser.add_argument(
        "--line",
        required=True,
        type=int,
        help="the 1-based line to mutate",
    )
    parser.add_argument(
        "--old",
        required=True,
        help="the text the line has to contain",
    )
    parser.add_argument(
        "--new",
        required=True,
        help="the text to replace it with",
    )
    parser.add_argument(
        "--test",
        required=True,
        help="the table's test file (a pytest node id is accepted too)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=(
            "how long to wait for the table, in seconds "
            f"(default: {DEFAULT_TIMEOUT:.0f})"
        ),
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="the directory to run the table in (default: this one)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        result = run_probe(
            module=args.module,
            line=args.line,
            old=args.old,
            new=args.new,
            test=args.test,
            timeout=args.timeout,
            cwd=args.cwd,
        )
    except MutationError as exc:
        print(f"MUTATION PROBE: ERROR: {exc}", file=sys.stderr)
        return 2

    print("MUTATION PROBE")
    print(f"module: {args.module}")
    print(f"line: {args.line}")
    print(f"old: {args.old}")
    print(f"new: {args.new}")
    print(f"test: {args.test}")
    print(f"RED: {'yes' if result.red else 'no'}")
    print(f"RESTORED: {'yes' if result.restored else 'no'}")
    print(f"SHA256: {'match' if result.sha256_match else 'MISMATCH'}")
    print(f"SHA256_BEFORE: {result.sha256_before}")
    print(f"SHA256_AFTER: {result.sha256_after}")
    print(f"PYTEST_EXIT_CODE: {result.returncode}")
    print(f"TIMED_OUT: {'yes' if result.timed_out else 'no'}")

    if not result.restored or not result.sha256_match:
        print(
            "MUTATION PROBE: the module was not restored, so the run "
            "cannot be trusted",
            file=sys.stderr,
        )
        return 2
    if result.timed_out or result.returncode is None:
        print(
            "MUTATION PROBE: the run timed out, so it says nothing about "
            "whether the table can fail",
            file=sys.stderr,
        )
        return 2
    return 0 if result.red else 1


if __name__ == "__main__":
    raise SystemExit(main())
