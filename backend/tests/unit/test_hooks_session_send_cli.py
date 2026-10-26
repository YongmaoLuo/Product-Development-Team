"""Tests for ``hooks.session_send`` — the delivery CLI.

What is being pinned
--------------------
The formatter (:func:`hooks.session_send.format_message`) decides
*what a human is told*; this file pins *how the message gets there*.
The CLI is the delivery entry point: a decision event arrives on
stdin as JSON, is formatted, and is handed to ``cc-connect send`` on
the child's stdin.  Three properties carry the weight, and each of them
is a way a delivery can be lost without anybody noticing:

* **The child is called the way ``cc-connect`` expects.**  The
  arguments are the contract with the transport — ``send -p <project>
  -s <session> --stdin`` — and the message arrives on stdin rather
  than in an argument, so a message containing spaces, quotes or
  newlines cannot be mangled by the shell or by argument parsing.
* **A failure is a failure.**  If the transport exits non-zero, the
  exit code is passed through unchanged, and if the transport is not
  there at all the CLI exits non-zero with a line on stderr.  The
  dispatcher decides whether a destination was reached by this exit
  code, so a delivery that swallowed its own failure would be
  recorded as delivered when nothing was delivered.
* **A malformed call never reaches the transport.**  Missing
  arguments or an event that is not JSON are usage errors: they exit
  non-zero *without* spawning anything, so a broken caller cannot
  deliver a half-formed message to a real group.

The transport is never the real one in these tests.  A stub stands in
for it, records the arguments and the stdin it received, and exits
with whatever code the test needs.  Nothing here sends a message
anywhere.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import hooks.session_send as session_send  # noqa: E402
from hooks.session_send import format_message, main  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event():
    """A complete decision event, the way the outlet builds one."""
    return {
        "plan_id": "p1",
        "decision_id": "d1",
        "question": "是否合入？",
        "options": ["accept", "revise"],
        "default_option": "accept",
        "deadline_at": "2026-10-26T12:00:00Z",
    }


def _write_stub(tmp_path, exit_code=0):
    """A stand-in for ``cc-connect`` that records how it was called.

    The stub is a shell script rather than a Python one so that it does
    not depend on which interpreter the suite runs under.  It writes its
    arguments one per line, copies everything it reads on stdin into a
    file, and exits with ``exit_code``.

    Returns the stub path, the file its arguments land in and the file
    its stdin lands in.
    """
    argv_file = tmp_path / "argv.txt"
    stdin_file = tmp_path / "stdin.txt"
    stub = tmp_path / "cc-connect"

    stub.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > '{argv}'\n"
        "cat > '{stdin}'\n"
        "exit {code}\n".format(argv=argv_file, stdin=stdin_file, code=exit_code),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, argv_file, stdin_file


def _stdin(event=None):
    """The event JSON on a stream, the way the dispatcher pipes it."""
    if event is None:
        event = _event()
    return io.StringIO(json.dumps(event, ensure_ascii=False))


# ---------------------------------------------------------------------------
# The successful delivery
# ---------------------------------------------------------------------------


def test_stub_receives_argv_and_message(tmp_path, monkeypatch):
    """The child is called as ``send -p <project> -s <session> --stdin``.

    And the message arrives on its stdin — the formatted event, exactly
    as :func:`format_message` wrote it.  The message is not passed as
    an argument: a decision message is several lines long and contains
    characters a shell would be happy to reinterpret.
    """
    stub, argv_file, stdin_file = _write_stub(tmp_path)
    monkeypatch.setenv("PDT_CC_CONNECT_BIN", str(stub))

    code = main(["--project", "grp-1", "--session", "s-1"], _stdin())

    assert code == 0
    assert argv_file.read_text(encoding="utf-8").splitlines() == [
        "send",
        "-p",
        "grp-1",
        "-s",
        "s-1",
        "--stdin",
    ]
    assert stdin_file.read_text(encoding="utf-8") == format_message(_event())


# ---------------------------------------------------------------------------
# The failures are failures
# ---------------------------------------------------------------------------


def test_child_failure_propagates(tmp_path, monkeypatch):
    """The child's exit code is the CLI's exit code.

    The dispatcher reads this exit code to decide whether the
    destination was reached.  A transport that failed for a reason of
    its own — a rejected message, an unreachable session — must not be
    reported as a success, and it must not be rewritten into a generic
    failure either: the code is what the dispatcher records.
    """
    stub, _, _ = _write_stub(tmp_path, exit_code=7)
    monkeypatch.setenv("PDT_CC_CONNECT_BIN", str(stub))

    code = main(["--project", "grp-1", "--session", "s-1"], _stdin())

    assert code == 7


def test_missing_binary_exits_nonzero(tmp_path, monkeypatch, capsys):
    """A transport that is not there is a failure, not a quiet success.

    The exit code is non-zero and stderr says which program is
    missing.  Silently returning zero here would tell the dispatcher
    that a human was asked, when nobody was asked anything.
    """
    monkeypatch.setenv(
        "PDT_CC_CONNECT_BIN", str(tmp_path / "no-such-cc-connect")
    )

    code = main(["--project", "grp-1", "--session", "s-1"], _stdin())

    assert code != 0
    assert "cc-connect" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The malformed calls
# ---------------------------------------------------------------------------


def test_unparsable_stdin_exits_nonzero(tmp_path, monkeypatch):
    """An event that is not JSON fails before anything is spawned.

    Half a message is worse than no message: a caller that writes
    broken JSON would otherwise deliver a placeholder message to a real
    group, and the decision it was carrying would look answered.
    """
    stub, argv_file, stdin_file = _write_stub(tmp_path)
    monkeypatch.setenv("PDT_CC_CONNECT_BIN", str(stub))

    code = main(
        ["--project", "grp-1", "--session", "s-1"],
        io.StringIO("{bad"),
    )

    assert code != 0
    assert not argv_file.exists()
    assert not stdin_file.exists()


def test_missing_arguments_exit_nonzero(tmp_path, monkeypatch):
    """A call without a project and a session fails before spawning.

    Without them there is nowhere to send the message, so the CLI must
    not pick a destination on its own — and must not spawn the
    transport at all while finding that out.
    """
    stub, argv_file, stdin_file = _write_stub(tmp_path)
    monkeypatch.setenv("PDT_CC_CONNECT_BIN", str(stub))

    code = main(["--project", "grp-1"], _stdin())

    assert code != 0
    assert not argv_file.exists()
    assert not stdin_file.exists()


# ---------------------------------------------------------------------------
# Where the binary comes from
# ---------------------------------------------------------------------------


def test_empty_env_falls_back_to_path_lookup(tmp_path, monkeypatch):
    """With no pinned binary, the one on PATH is used.

    The environment variable is an override, not a requirement: the
    common case is a machine where ``cc-connect`` is simply installed.
    """
    stub, argv_file, _ = _write_stub(tmp_path)
    monkeypatch.delenv("PDT_CC_CONNECT_BIN", raising=False)
    monkeypatch.setattr(
        session_send.shutil,
        "which",
        lambda name: str(stub) if name == "cc-connect" else None,
    )

    code = main(["--project", "grp-1", "--session", "s-1"], _stdin())

    assert code == 0
    assert argv_file.exists()


def test_build_command_is_the_documented_argument_list():
    """The argument list is the contract with the transport."""
    assert session_send.build_command("/bin/cc-connect", "grp-1", "s-1") == [
        "/bin/cc-connect",
        "send",
        "-p",
        "grp-1",
        "-s",
        "s-1",
        "--stdin",
    ]
