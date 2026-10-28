"""A file that signals verdicts has to pin a pid scope.

Why this gate exists
--------------------
The reaper's blast-radius suite proves that a *scoped* scan cannot hand a
process outside the scope to an action layer that sends signals.  That
suite is about the reaper's own behaviour; it says nothing about the files
that call it.

The accident this gate exists for was a file rather than a defect in the
reaper.  A negative-control pytest plugin replaced the reaper's action
layer with one that sent ``SIGTERM`` to every verdict it was handed, and
the plugin was run against a full-machine scan with no ``pid_scope``
configured.  The judgement layer kept skipping every process it saw -- it
never once decided to kill anything -- and the signals went out anyway,
because the signals did not come from the judgement layer.  They came from
the replacement.  A ``SIGTERM`` aimed at one process' group took the
launchd jobs and the supervisor that shared it down with it: the machine
did not restart, and the whole user session started over.

The tree's answer to that was documentation -- "an unscoped scan is a dry
run" -- and documentation is not a control.  Nothing in the tree stopped the
next plugin from doing the same thing again.  This gate is what stops it:
**a file that replaces the action layer, or that signals verdicts
directly, has to name a pid scope.**  A file that says ``pid_scope`` or
``PDT_ORPHAN_REAPER_PID_SCOPE`` is saying, in the same breath, which
processes it is allowed to touch.

What the rule looks at
----------------------
Two shapes, and either one is enough:

* the action layer is **replaced** -- a whole-line assignment to the
  reaper's action layer, or to the module's own name for it;
* a verdict is **signalled directly** -- the file calls ``os.kill(`` and
  speaks the reaper's verdict vocabulary.

The second shape needs a qualifier, and the qualifier is the whole point
of the gate.  The word "verdict" is used across this repository for
execution verdicts, plan verdicts and evidence verdicts; several of those
files call ``os.kill(`` and say "verdict" while having nothing to do with
the reaper.  A rule that fired on them would be a rule that fires
everywhere, and a rule that fires everywhere is a rule nobody reads --
and one that gets allowlisted within a week, which protects nothing.  So
"a verdict" here means a **reaper** verdict: the file has to reach the
reaper's own vocabulary before the signal counts as a signal on a verdict.

The gate exempts itself, by ``__file__``: it quotes the shapes it forbids
in the self-check below, and a gate that flags its own fixtures protects
nobody.

Scope, stated honestly: this reads the test tree's source text.  It does
not run the reaper, and it cannot tell whether a pid scope that *is* pinned
is narrow enough -- a file that pins a scope of every pid on the machine
passes this gate.  What it proves is the narrower, mechanical claim: a
file that signals verdicts cannot do it without naming the scope gate, so
the "unscoped signals" accident has to be written down loudly, in the
same file, before it can be merged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

#: The tree this gate reads: every Python file under ``backend/tests``.
SCAN_ROOT = Path(__file__).resolve().parents[1]

#: The repository root, so that a violation can be reported as the path a
#: reviewer would type.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: This file.  It quotes the shapes it forbids, so it is exempt from them.
THIS_FILE = Path(__file__).resolve()

#: The reaper's action layer, split into fragments so the literal the
#: rule forbids never appears whole in this file's own source -- the gate
#: would otherwise flag its own definition.
_ACTION_LAYER = "reap_" + "verdicts"

#: The whole-line assignments that replace the action layer.  Anchored to
#: the start of a line, so that a mention of the name in prose or inside
#: a longer expression is not an assignment.
_REPLACES_ACTION_LAYER = re.compile(
    r"^\s*(?:reaper\.)?" + _ACTION_LAYER + r"\s*=", re.MULTILINE
)

#: The reaper's own vocabulary for a verdict.  A file that calls
#: ``os.kill(`` and speaks one of these is signalling a reaper verdict.
_REAPER_VERDICT_MARKERS: Tuple[str, ...] = (
    _ACTION_LAYER,
    "verdict.proc",
    "orphan_reaper",
)

#: The call that sends a signal.
_SIGNAL_CALL = "os.kill("

#: What a file has to name to say "only these pids".
_SCOPE_NAMES: Tuple[str, ...] = ("pid_scope", "PDT_ORPHAN_REAPER_PID_SCOPE")


def _pin_violation(source: str) -> Optional[str]:
    """Return the reason *source* breaks the pin, or ``None`` if it does not.

    A pure function on purpose: the self-check below feeds it the shape of
    the accident and the shape of the compliant version, so the rule can
    be exercised without a file on disk and without a process anywhere
    near it.

    The reason is one line and names the shape, because a gate that says
    "violation" without saying which shape is a gate nobody can act on.
    """
    replaces = _REPLACES_ACTION_LAYER.search(source) is not None
    signals = _SIGNAL_CALL in source and any(
        marker in source for marker in _REAPER_VERDICT_MARKERS
    )
    if not (replaces or signals):
        return None
    if any(name in source for name in _SCOPE_NAMES):
        return None
    if replaces:
        return (
            "it replaces the reaper's action layer (a whole-line assignment "
            "to it) but never names a pid scope"
        )
    return (
        "it signals reaper verdicts directly (a kill call plus the "
        "reaper's verdict vocabulary) but never names a pid scope"
    )


def _python_files(root: Path) -> Iterator[Path]:
    """Every Python file under *root*, compiled output left out."""
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _violations_in(path: Path) -> Optional[str]:
    """The reason *path* breaks the pin, or ``None`` if it does not."""
    source = path.read_text(encoding="utf-8", errors="replace")
    return _pin_violation(source)


#: The accident, as a file: a replacement action layer that signals every
#: verdict it is handed, and no scope anywhere in sight.
ACCIDENT_SOURCE = (
    "import os\n"
    "import signal\n"
    "\n"
    "\n"
    "def broken_reap(verdicts, config, **kwargs):\n"
    "    for verdict in verdicts:\n"
    "        os.kill(verdict.proc.pid, signal.SIGTERM)\n"
)

#: The same thing in a file that pins its scope: the shape the gate
#: accepts, and the shape the blast-radius suite uses.
COMPLIANT_SOURCE = (
    "import os\n"
    "import signal\n"
    "\n"
    "\n"
    "def broken_reap(verdicts, config, **kwargs):\n"
    "    for verdict in verdicts:\n"
    "        os.kill(verdict.proc.pid, signal.SIGTERM)\n"
    "\n"
    "\n"
    "config = ReaperConfig(pid_scope=(1234, 5678))\n"
)

#: A file that kills a pid and mentions a verdict that has nothing to do
#: with the reaper -- an execution verdict, say.  The gate must leave it
#: alone, or it fires on the whole tree.
UNRELATED_SOURCE = (
    "import os\n"
    "\n"
    "\n"
    "def stop_the_worker(pid):\n"
    "    os.kill(pid, 9)\n"
    "\n"
    "\n"
    "# the execution verdict of the round, which has its own lifecycle\n"
)


def test_the_scan_is_not_empty() -> None:
    """The scan has to reach real files.

    A gate whose walk has drifted away from the tree passes vacuously.
    The same rationale as the sibling gates' "the scan is not empty"
    assertion.
    """
    files = list(_python_files(SCAN_ROOT))
    assert files, (
        "%s yielded no Python files, so the gate would now pass on an "
        "empty result" % SCAN_ROOT
    )
    assert len(files) > 1, (
        "%s yielded a single file, which is not a tree" % SCAN_ROOT
    )


def test_no_file_signals_verdicts_without_a_pid_scope() -> None:
    """No file in the test tree signals a verdict without naming a scope.

    The whole point of the gate, over the whole tree: the accident is
    stopped here, not by whoever reads the diff carefully enough to notice
    that a scope is missing.
    """
    violations: List[str] = []
    for path in _python_files(SCAN_ROOT):
        if path.resolve() == THIS_FILE:
            continue
        reason = _violations_in(path)
        if reason is None:
            continue
        try:
            shown = path.relative_to(REPO_ROOT)
        except ValueError:  # pragma: no cover - the tree is inside the repo
            shown = path
        violations.append("%s: %s" % (shown, reason))

    assert not violations, (
        "these files signal verdicts without pinning a pid scope:\n%s\n"
        "A file that replaces the reaper's action layer, or that signals a "
        "verdict directly, has to say which pids it is allowed to touch -- "
        "name `pid_scope` or `PDT_ORPHAN_REAPER_PID_SCOPE` in the file."
        % "\n".join(violations)
    )


def test_the_accident_shape_is_flagged() -> None:
    """The shape of the accident is a violation, and it says why.

    The gate's own control: a rule that cannot fail is not a gate.  The
    source fed in here is the accident -- a replacement action layer that
    signals every verdict it is handed, with no scope in the file.
    """
    reason = _pin_violation(ACCIDENT_SOURCE)
    assert reason is not None, (
        "the accident's shape was not flagged: a replacement action layer "
        "that signals every verdict it is handed, with no pid scope "
        "anywhere in the file"
    )
    assert "pid scope" in reason, (
        "the reason does not say what the file has to do about it: %r" % reason
    )


def test_the_compliant_shape_passes() -> None:
    """The same signals, in a file that pins its scope, are accepted.

    This is the counterweight to the test above.  A gate that flagged
    this file too would be a gate that says "never signal a verdict",
    which is not the rule -- the rule is "say which pids you may touch".
    """
    assert _pin_violation(COMPLIANT_SOURCE) is None, (
        "a file that pins `pid_scope` was flagged; the rule is about the "
        "missing scope, not about the signal"
    )


def test_replacing_the_action_layer_is_flagged() -> None:
    """A file that replaces the action layer has to pin a scope as well.

    The other shape the rule looks at.  Replacing the action layer is
    how the accident happened, and it is a replacement whether or not the
    replacement itself sends a signal.
    """
    source = "reaper." + _ACTION_LAYER + " = broken_reap\n"
    reason = _pin_violation(source)
    assert reason is not None, (
        "a whole-line assignment that replaces the action layer was not "
        "flagged"
    )


def test_an_unrelated_verdict_is_not_a_reaper_verdict() -> None:
    """A kill call next to an unrelated "verdict" is left alone.

    The counterweight to the qualifier.  The word "verdict" belongs to
    several other subsystems in this repository, and a gate that flagged
    every file that says it would be a gate that fires everywhere -- and
    one that gets allowlisted within a week, which protects nothing.
    """
    assert _pin_violation(UNRELATED_SOURCE) is None, (
        "a file that kills a pid and mentions an execution verdict was "
        "flagged; the rule is about the reaper's verdicts"
    )
