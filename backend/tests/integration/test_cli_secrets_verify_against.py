"""``secrets verify --verify-against``: three verdicts, and no value printed.

Why this suite exists
---------------------
The acceptance item "matches the source side, value for value" had no
component to point at. ``secrets verify`` says *where* a secret comes
from and ``secrets show`` says *under which index* it is filed; neither
says whether this machine holds the same credential the source side
holds, so the comparison was done by a person reading two terminals.
``--verify-against <file>`` is that comparison, and this suite is where
its contract is pinned.

Three constraints, and each one has a failure it prevents
---------------------------------------------------------
**Only a boolean leaves this command.** The line for a secret grows one
column and the column is one of three words. Both values are in this
process to be compared and neither is written: the answer an operator
wants is "does it match", and a command that answers it by printing the
credential has turned a check into a disclosure — into a terminal
scrollback, a CI log, a screen share. The instrument is a sentinel on
*each* side carrying a marker in its middle, so a value that leaked
whole is caught and a value that leaked truncated is caught too.

**A missing baseline key is not a mismatch.** The two answers mean
opposite things to whoever reads them. "Not equal" is a finding: this
deployment is configured with a different credential than the source
side, and someone has to go and fix it. "Absent from the baseline" is
an unfinished check: the file does not carry this key, and the next
step is to find out why. Collapsing the second into the first
manufactures a finding that does not exist, and an operator who has seen
that alarm once has learned not to trust this command. So the
distinction is a whole verdict, and the test that pins it asserts the
*absence* of the wrong one — a comparison that printed both would pass
an assertion that only checked for the right one.

**The provider does not learn that a baseline exists.** The comparison
is a property of this command, not of where secrets come from: a
provider that took a baseline path would have to answer "what is this
file for", and every one of its callers would be carrying that question
for a command only one of them asked for. So the counter here is
placed on the two transports — the places a value is actually fetched
— and a run with a baseline must start exactly as many of them as a run
without one. Reading the value is a memo hit, not a second fetch, and a
baseline file that doubled the cost would be doubling a cost the whole
project is trying to bound.

The baseline file itself
------------------------
A ``KEY=value`` file, the spelling ``.env`` already uses, keyed
by the spec table's own ``fallback_env_key``. Three deliberate limits.
No interpolation, so a value is compared as written rather than as
whatever the shell it was written in would have produced. A line with
no ``=`` is skipped rather than read as an empty value, because in this
grammar a bare key means "inherit from the environment" — and a
baseline that inherited the environment would compare the value against
itself. Bytes that are not valid UTF-8 survive: a secret is bytes, and a
baseline that cannot hold the credential it is meant to check is a
baseline that reports a mismatch for a reason that is not one.

Why integration
---------------
One case reads the baseline through a real child process, because the
argument arrives through argparse and ``load_env()`` and the shape of
the answer can depend on both. The rest call the command in this
process so the streams can be read whole; the child is waited on before
the test ends. Nothing here starts a real keychain, reaches a network,
or writes outside ``tmp_path``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import cli
import credentials

pytestmark = pytest.mark.integration

#: ``backend/`` and the repository root, both resolved from this file:
#: the checkout lives at a different path on every machine.
BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

#: The three words :func:`cli.cmd_secrets_verify` may put in the
#: comparison column, and nothing else.
EQUAL = "相等"
NOT_EQUAL = "不相等"
BASELINE_ABSENT = "基准缺失"

#: A marker buried in the middle of every sentinel. The full-value check
#: is the contract; this is what makes the check bite on a value that
#: left the process whole-but-shorter, which a ``in`` test on the whole
#: string would call a pass.
MARKER = "NEVER-PRINT-THIS"

#: Every environment variable a ``secrets`` answer can depend on, taken
#: from the spec table rather than spelled out here, so a row added to
#: the table is covered the day it is added.
_CREDENTIAL_ENV_KEYS = tuple(
    key
    for spec in credentials.SECRET_SPECS.values()
    for key in (spec.fallback_env_key, spec.account_env_key)
) + ("PDT_DISABLE_KEYCHAIN_SECRETS",)

# ``env_config.load_env()`` used to demand three variables before the CLI
# parsed anything, and these tests filled them with placeholders so the
# subprocess would get that far. It demands nothing now (2026-10-08), so
# there is nothing to fill in.


def _sentinel(canary, kind, tmp_path, tag) -> str:
    """Return a synthetic credential carrying :data:`MARKER` in its middle.

    The marker is not at either end, so a command that printed a prefix
    or a suffix of the value is caught as surely as one that printed all
    of it.
    """
    return "{}-{}-{}".format(canary(kind, tmp_path), MARKER, tag)


@pytest.fixture(autouse=True)
def scrub_credential_environment(monkeypatch, tmp_path):
    """Start every test with no provider credential in the environment.

    A developer's shell may carry a real secret, and every assertion
    below is about output — so a value the test did not mint would make
    "the value is not printed" pass for the wrong reason on one machine
    and fail on another. The keychain tool is pointed at a present,
    executable stand-in for the same reason the unit suite does it: a
    suite whose answers change with the runner cannot tell a regression
    from a platform, and this one is read on macOS and in a Linux
    container.
    """
    for key in _CREDENTIAL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PDT_DISABLE_KEYCHAIN_SECRETS", "1")
    monkeypatch.setattr(credentials, "_SECURITY_BIN", str(_stub_tool(tmp_path)))
    credentials.reset_cache()
    yield
    credentials.reset_cache()


def _stub_tool(tmp_path, name: str = "security") -> Path:
    """Return an executable stand-in for the keychain tool.

    Never run by this suite — the availability check asks two things of
    a path and this answers both.
    """
    tool = tmp_path / name
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    return tool


def _export_every_secret(monkeypatch, values) -> None:
    """Put ``values`` (keyed by logical name) into the fallback variables."""
    for name, value in values.items():
        monkeypatch.setenv(
            credentials.SECRET_SPECS[name].fallback_env_key, value
        )


def _write_baseline(path, values) -> Path:
    """Write a ``KEY=value`` baseline file for ``values`` (by logical name)."""
    path.write_text(
        "".join(
            "{}={}\n".format(credentials.SECRET_SPECS[name].fallback_env_key, value)
            for name, value in values.items()
        ),
        encoding="utf-8",
    )
    return path


def _row_for(output: str, name: str) -> str:
    """Return the single line of ``output`` reporting on ``name``."""
    rows = [line for line in output.splitlines() if line.startswith(name)]
    assert len(rows) == 1, "expected one line for {}, got {}".format(name, rows)
    return rows[0]


def _assert_no_value(output: str, value: str) -> None:
    """Assert ``value`` — and its marker — is absent from ``output``."""
    assert value not in output, "a value was written to the output"
    assert MARKER not in output, "a fragment of a value was written to the output"


# ---------------------------------------------------------------------------
# The three verdicts
# ---------------------------------------------------------------------------


def test_equal_baseline_reports_equal(monkeypatch, capsys, canary, tmp_path):
    """Every secret equal to the baseline: every line says so, and exits 0.

    The baseline carries a value for every key, so the only verdict this
    can produce is the first one. Asserting on the count of the other two
    as well as on the first keeps a run that quietly skipped a secret
    from passing as a clean one.
    """
    values = {
        name: _sentinel(canary, "feishu_secret", tmp_path, "equal")
        for name in credentials.SECRET_SPECS
    }
    _export_every_secret(monkeypatch, values)
    baseline = _write_baseline(tmp_path / "base.env", values)

    exit_code = cli.cmd_secrets_verify(verify_against=str(baseline))
    out = capsys.readouterr().out

    for name in credentials.SECRET_SPECS:
        assert _row_for(out, name).endswith(EQUAL), out
    assert NOT_EQUAL not in out
    assert BASELINE_ABSENT not in out
    assert exit_code == 0


def test_mismatched_value_reports_not_equal(monkeypatch, capsys, canary, tmp_path):
    """One value differing: that line says so, its neighbour does not, exit 1.

    Both shapes are asserted because "one line changed" is the failure
    this contract exists to catch, and a command that reported every row
    as equal — or every row as not equal — would satisfy either single
    assertion on its own.
    """
    names = list(credentials.SECRET_SPECS)
    matching, differing = names[0], names[1]

    live = {name: _sentinel(canary, "feishu_secret", tmp_path, "live") for name in names}
    _export_every_secret(monkeypatch, live)
    baseline = _write_baseline(
        tmp_path / "base.env",
        {
            matching: live[matching],
            differing: _sentinel(canary, "feishu_secret", tmp_path, "other"),
        },
    )

    exit_code = cli.cmd_secrets_verify(verify_against=str(baseline))
    out = capsys.readouterr().out

    assert _row_for(out, differing).endswith(NOT_EQUAL), out
    assert _row_for(out, matching).endswith(EQUAL), out
    assert exit_code != 0


def test_absent_key_reports_missing_not_mismatch(
    monkeypatch, capsys, canary, tmp_path
):
    """A key the baseline does not carry is 基准缺失, and never 不相等.

    The negative assertion is the test. ``基准缺失`` and ``不相等`` both
    say "this is not equal to the baseline" to a reader who has not been
    told the vocabulary, and the whole point of the third verdict is
    that they are not the same answer: one is a finding about a
    deployment, the other is an unfinished check. A line carrying the
    wrong one here would send an operator to fix a deployment that was
    never wrong.
    """
    names = list(credentials.SECRET_SPECS)
    present, absent = names[0], names[1]

    live = {
        name: _sentinel(canary, "feishu_secret", tmp_path, "live") for name in names
    }
    _export_every_secret(monkeypatch, live)
    baseline = _write_baseline(tmp_path / "base.env", {present: live[present]})

    exit_code = cli.cmd_secrets_verify(verify_against=str(baseline))
    out = capsys.readouterr().out

    row = _row_for(out, absent)
    assert row.endswith(BASELINE_ABSENT), out
    assert NOT_EQUAL not in row
    assert _row_for(out, present).endswith(EQUAL), out
    assert exit_code != 0


def test_neither_side_value_appears_in_output(monkeypatch, capsys, canary, tmp_path):
    """Neither the live value nor the baseline value is written anywhere.

    Both sides are deliberately different, so the command has both in
    hand and both are checked: a comparison that printed one of them and
    not the other would still have disclosed a credential. The
    deployment is reported as not matching, which is the only thing an
    operator learns from a nonzero run.
    """
    live = {
        name: _sentinel(canary, "feishu_secret", tmp_path, "live") for name in credentials.SECRET_SPECS
    }
    _export_every_secret(monkeypatch, live)
    baseline = _write_baseline(
        tmp_path / "base.env",
        {
            name: _sentinel(canary, "feishu_secret", tmp_path, "base")
            for name in credentials.SECRET_SPECS
        },
    )

    cli.cmd_secrets_verify(verify_against=str(baseline))
    captured = capsys.readouterr()

    for value in live.values():
        _assert_no_value(captured.out, value)
    for line in baseline.read_text(encoding="utf-8").splitlines():
        _assert_no_value(captured.out, line.split("=", 1)[1])
    _assert_no_value(captured.err, MARKER)


# ---------------------------------------------------------------------------
# The baseline is not the provider's business
# ---------------------------------------------------------------------------


def test_baseline_does_not_change_lookup_count(monkeypatch, capsys, canary, tmp_path):
    """A run with a baseline fetches exactly as many values as one without.

    The counter is on the two transports — the only places in the
    provider where a value is actually fetched — so it counts lookups
    rather than calls. Reading a value is a memo hit on the second call
    and that is the point: the comparison needs the value, and needing
    it must not cost a second fetch, or the command would double a cost
    the provider memoises precisely so it is not paid twice.

    The memo is checked as well as the count. A comparison that emptied
    the cache would pass a counter (every secret is still fetched once,
    in a fresh process) and would leave the deployment paying a lookup
    per call afterwards, which is the cost the memo exists to remove.
    """
    values = {
        name: _sentinel(canary, "feishu_secret", tmp_path, "live")
        for name in credentials.SECRET_SPECS
    }
    _export_every_secret(monkeypatch, values)

    # The originals are captured once. Wrapping whatever the attribute
    # happens to hold at wrap time would stack one counter on the
    # previous one, and the second run would count every fetch twice —
    # a number that still looks like a count, and is not this one.
    original_env = credentials._resolve_from_env
    original_keychain = credentials._resolve_from_keychain

    def _count_fetches():
        counter = {}

        def _wrap(resolve):
            def _counted(spec):
                counter[spec.logical_name] = counter.get(spec.logical_name, 0) + 1
                return resolve(spec)

            return _counted

        monkeypatch.setattr(
            credentials, "_resolve_from_env", _wrap(original_env)
        )
        monkeypatch.setattr(
            credentials, "_resolve_from_keychain", _wrap(original_keychain)
        )
        return counter

    # Run one: no baseline at all. Run two: a baseline carrying a
    # *different* value for every key, so the comparison is a real
    # comparison and not a lookup that could have short-circuited.
    without_baseline = _count_fetches()
    cli.cmd_secrets_verify()
    capsys.readouterr()

    credentials.reset_cache()
    with_baseline = _count_fetches()
    baseline = _write_baseline(
        tmp_path / "base.env",
        {
            name: _sentinel(canary, "feishu_secret", tmp_path, "base")
            for name in credentials.SECRET_SPECS
        },
    )
    cli.cmd_secrets_verify(verify_against=str(baseline))
    capsys.readouterr()

    assert with_baseline == without_baseline, (
        "the baseline changed how often a value is fetched: {} vs {}".format(
            with_baseline, without_baseline
        )
    )
    assert set(without_baseline.values()) == {1}, without_baseline
    # Still memoised afterwards: every secret the command reported on is
    # in the cache, and a comparison has not thrown it away.
    assert set(credentials._CACHE) == set(credentials.SECRET_SPECS)


# ---------------------------------------------------------------------------
# A baseline that is not there
# ---------------------------------------------------------------------------


def test_missing_baseline_file_exits_nonzero_without_echoing_the_path(
    monkeypatch, capsys, canary, tmp_path
):
    """No baseline file: a nonzero exit, a sentence, and no path.

    A path is not a secret, but it is the one string on this path an
    operator did not type into this command and cannot predict: it
    arrives in a CI job's argv, in a shell history, in a process
    listing, and in whatever directory the job happened to be given. The
    failure is reported without it, and reported as a failure — a
    comparison that did not run must not be able to exit 0.

    The ordinary listing is still printed. The baseline is an addition
    to this command, not a precondition for it, and an operator whose
    file was in the wrong place still gets the answer they came for
    before they learn that the comparison did not happen.
    """
    _export_every_secret(
        monkeypatch,
        {
            name: _sentinel(canary, "feishu_secret", tmp_path, "live")
            for name in credentials.SECRET_SPECS
        },
    )
    absent = tmp_path / "no-such-baseline.env"

    exit_code = cli.cmd_secrets_verify(verify_against=str(absent))
    captured = capsys.readouterr()

    assert exit_code != 0
    assert str(absent) not in captured.out
    assert str(absent) not in captured.err
    assert "Traceback" not in captured.err
    assert out_has_sources(captured.out), captured.out
    _assert_no_value(captured.out, MARKER)


def out_has_sources(output: str) -> bool:
    """Return whether every secret still has its ``source=`` line."""
    return all(
        "source=" in _row_for(output, name) for name in credentials.SECRET_SPECS
    )


# ---------------------------------------------------------------------------
# The real entry point
# ---------------------------------------------------------------------------


def test_real_subprocess_reads_the_baseline_and_prints_no_value(
    canary, tmp_path
):
    """A child process, real argv, real file: the same three verdicts.

    The argument has to survive argparse and ``load_env()`` before the
    comparison runs, and neither is exercised by calling the function
    directly — a flag the parser never registered is a flag that does
    not exist, and no in-process test can see it.

    Every credential key is *set* rather than removed, for the same
    reason the unit suite sets them: so that a key cannot arrive from the
    developer's own shell to stand in for one this test meant to
    supply. The keychain is switched off so the deployment being
    described is the same one on every platform.

    The exit code is compared as "0 or 1" rather than pinned: whether
    ``/usr/bin/security`` can be executed is a property of the runner
    and not of the spelling of this command, and the verdicts — which
    are the contract — are on stdout either way.
    """
    values = {
        name: _sentinel(canary, "feishu_secret", tmp_path, "live")
        for name in credentials.SECRET_SPECS
    }
    child_env = dict(os.environ)
    for key in _CREDENTIAL_ENV_KEYS:
        child_env.pop(key, None)
    child_env["PDT_DISABLE_KEYCHAIN_SECRETS"] = "1"
    for name, value in values.items():
        child_env[credentials.SECRET_SPECS[name].fallback_env_key] = value

    names = list(credentials.SECRET_SPECS)
    matching, differing = names[0], names[1]
    baseline = _write_baseline(
        tmp_path / "base.env",
        {
            matching: values[matching],
            differing: _sentinel(canary, "feishu_secret", tmp_path, "other"),
        },
    )

    child = subprocess.run(
        [sys.executable, "-m", "backend.cli", "secrets", "verify",
         "--verify-against", str(baseline)],
        cwd=str(REPO_ROOT), env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
    )
    out = child.stdout.decode("utf-8", "replace")
    err = child.stderr.decode("utf-8", "replace")

    assert _row_for(out, matching).endswith(EQUAL), out
    assert _row_for(out, differing).endswith(NOT_EQUAL), out
    assert child.returncode != 0, "a mismatch has to be able to fail the run"
    for value in values.values():
        _assert_no_value(out, value)
        _assert_no_value(err, value)
    assert MARKER not in err
    assert "Traceback" not in err
