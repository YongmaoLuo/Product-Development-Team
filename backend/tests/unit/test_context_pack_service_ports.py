"""Unit tests for the port-probe parsing paths of ``backend.context_pack``.

Why this file exists
--------------------
``context_pack`` reports the TCP ports this round cares about: the ports
the focused test command names, plus the ports something is *already*
listening on. The second source is read out of ``lsof -F cn`` output, and
every line of that parsing used to be executed only when the machine
running the tests happened to have a listening TCP socket.

That made the coverage gate machine-dependent: on a runner with a
listening port the parsing lines were covered, on a runner without one
they were not, and the same source tree could sit above or below the
gate floor depending on where it ran.

These tests pin the parsing itself -- the probe subprocess, the program
label, the socket-name-to-port conversion, the ``lsof`` output loop and
the merge into the round's purposes -- by feeding them fixed inputs. No
test here spawns ``lsof``, and no test here depends on what the host
happens to be listening on.

What this pins
--------------
* A probe that succeeds yields its stdout; a probe that fails or blows
  up yields ``None`` and never propagates -- a probe is best-effort.
* A program name is reduced to a label that cannot carry a value: only
  safe characters survive, and it is truncated.
* A socket name yields a port only when the port is a real TCP port.
* ``lsof`` output is parsed into ``{port: label}``, and a socket with no
  known program still yields a label.
* A port named by both sources appears once, and the focused test
  command's label wins.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import context_pack  # noqa: E402
from context_pack import (  # noqa: E402
    _listening_ports,
    _port_from_socket_name,
    _run_probe,
    _safe_label,
    _service_ports,
)


class _FakeCompleted:
    """The shape ``subprocess.run`` returns, as far as the probe reads it."""

    def __init__(self, returncode: int, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_probe_returns_stdout_when_the_command_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful probe hands back exactly what the command printed."""
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["kwargs"] = kwargs
        return _FakeCompleted(0, "cPython\nn127.0.0.1:8000\n")

    monkeypatch.setattr(context_pack.subprocess, "run", fake_run)

    assert _run_probe(["lsof", "-F", "cn"]) == "cPython\nn127.0.0.1:8000\n"
    assert seen["argv"] == ["lsof", "-F", "cn"]
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["text"] is True


def test_probe_returns_none_when_the_command_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit carries no fact; the probe reports none."""
    monkeypatch.setattr(
        context_pack.subprocess,
        "run",
        lambda argv, **kwargs: _FakeCompleted(1, "should be ignored\n"),
    )

    assert _run_probe(["lsof", "-F", "cn"]) is None


def test_probe_returns_none_when_the_command_blows_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing binary is a missing fact, not a failed round."""

    def exploding_run(argv, **kwargs):
        raise FileNotFoundError("no lsof here")

    monkeypatch.setattr(context_pack.subprocess, "run", exploding_run)

    assert _run_probe(["lsof", "-F", "cn"]) is None


def test_safe_label_keeps_only_safe_characters() -> None:
    """Anything that could carry a value is stripped out of the label."""
    assert _safe_label("node") == "node"
    assert _safe_label("my prog") == "myprog"
    assert _safe_label("a/b:c=d") == "abcd"
    assert _safe_label("with.dots-and_dashes") == "with.dots-and_dashes"
    assert "=" not in _safe_label("a=b")
    assert "/" not in _safe_label("a/b")
    assert ":" not in _safe_label("a:b")


def test_safe_label_truncates_to_32_characters() -> None:
    """A long program name cannot grow the pack without bound."""
    label = _safe_label("A" * 100)
    assert len(label) == 32
    assert label == "A" * 32


def test_port_from_socket_name_reads_a_real_port() -> None:
    """The port is the text after the last colon."""
    assert _port_from_socket_name("127.0.0.1:8080") == 8080
    assert _port_from_socket_name("*:5432") == 5432
    assert _port_from_socket_name("  127.0.0.1:3000  ") == 3000


def test_port_from_socket_name_rejects_a_missing_port() -> None:
    """No socket name means no port."""
    assert _port_from_socket_name("") is None
    assert _port_from_socket_name("   ") is None


def test_port_from_socket_name_rejects_a_non_numeric_port() -> None:
    """A name that does not end in a number is not a port."""
    assert _port_from_socket_name("host:notaport") is None


def test_port_from_socket_name_rejects_out_of_range_ports() -> None:
    """A port outside the TCP range is not a port."""
    assert _port_from_socket_name("host:0") is None
    assert _port_from_socket_name("host:70000") is None


def test_listening_ports_parses_lsof_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``lsof -F cn`` loop yields the port of every listener."""
    monkeypatch.setattr(
        context_pack,
        "_run_probe",
        lambda argv: "cPython\nn127.0.0.1:8000\nn*:5432\n",
    )

    assert _listening_ports() == {8000: "listening:Python", 5432: "listening:Python"}


def test_listening_ports_labels_a_socket_with_no_known_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A socket seen before any program line still gets a label."""
    monkeypatch.setattr(
        context_pack,
        "_run_probe",
        lambda argv: "n127.0.0.1:9999\n",
    )

    assert _listening_ports() == {9999: "listening"}


def test_listening_ports_skips_a_socket_name_without_a_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name with no usable port is not a service port."""
    monkeypatch.setattr(
        context_pack,
        "_run_probe",
        lambda argv: "cPython\nnsomething-without-a-port\n",
    )

    assert _listening_ports() == {}


def test_listening_ports_is_empty_when_the_probe_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No probe output means no listening ports -- not an error."""
    monkeypatch.setattr(context_pack, "_run_probe", lambda argv: "")

    assert _listening_ports() == {}


def test_listening_ports_is_empty_when_the_probe_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed probe reports nothing rather than raising."""
    monkeypatch.setattr(context_pack, "_run_probe", lambda argv: None)

    assert _listening_ports() == {}


def test_service_ports_keeps_the_focused_label_for_a_shared_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port named by both sources appears once, focused first."""
    monkeypatch.setattr(
        context_pack,
        "_listening_ports",
        lambda: {8000: "listening:Python", 5432: "listening:Python"},
    )

    ports = _service_ports("pytest tests/unit --base-url http://127.0.0.1:8000")

    assert ports == [
        {"port": 5432, "purpose": "listening:Python"},
        {"port": 8000, "purpose": "focused_test_command"},
    ]


def test_service_ports_reports_a_listening_port_the_command_does_not_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A port that is only listening is still a port this round cares about."""
    monkeypatch.setattr(
        context_pack,
        "_listening_ports",
        lambda: {9000: "listening:Redis"},
    )

    ports = _service_ports("pytest tests/unit")

    assert ports == [{"port": 9000, "purpose": "listening:Redis"}]


def test_service_ports_is_empty_when_nothing_is_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no listener and no named port, the round has no ports."""
    monkeypatch.setattr(context_pack, "_listening_ports", lambda: {})

    assert _service_ports(None) == []
