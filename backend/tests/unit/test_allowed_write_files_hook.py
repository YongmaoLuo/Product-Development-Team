"""The task's declaration is also its permission (2026-10-19).

A task's ``files_to_modify`` used to be only a CLAIM: it ordered the
schedule and it named the files the executor pre-locked at task start.
Anything the sub-agent decided to touch outside it was acquired by the
Edit/Write guard one edit at a time, in whatever order the model edited.
Two siblings that each declared a DISJOINT set and then both reached
outside it produced textbook AB-BA, and the run spun for 26 minutes
against a 300-second timeout.

Sorting cannot fix that — the executor only ever knows the declared set,
and the touched set was not a subset of it. This file pins the fix: the
guard refuses a write outside the declaration, so the touched set IS a
subset of the declared set, so the lock set is fixed before the task runs
and there is no incremental acquisition left to interleave.

Run as a black box on purpose. The contract under test is an exit code
and a stream, so the only honest way to check one is to make the process
produce it — same reason as ``test_readonly_task_guard_hook.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from agent import AutonomousAgent  # noqa: E402
from file_lock_broker import FileLockBroker  # noqa: E402
from file_lock_protocol import socket_path  # noqa: E402


@pytest.fixture
def workspace(tmp_path):
    """A project dir, a plans dir and the guard script, ready to invoke."""
    from coding_tool import _ensure_guard_hook_path

    project = tmp_path / "project"
    plans = tmp_path / "plans"
    (project / "backend").mkdir(parents=True)
    plans.mkdir()
    return {
        "project": project,
        "plans": plans,
        "script": _ensure_guard_hook_path(),
        "cli": _BACKEND_DIR / "file_lock_cli.py",
        "markers": tmp_path / "markers",
    }


@pytest.fixture
def broker(workspace):
    from file_lock_protocol import locks_dir_for_plan

    b = FileLockBroker(workspace["project"], locks_dir_for_plan(workspace["plans"]))
    b.start()
    try:
        yield b
    finally:
        b.stop()


def _run_hook(workspace, target, *, broker_on=True, task_id="t1",
              allowed=None, read_only=False, extra_env=None):
    """Run the guard hook for one Edit, returning ``(exit_code, stderr)``."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PDT_PROJECT_DIR": str(workspace["project"]),
        "PDT_PLANS_DIR": str(workspace["plans"]),
        "PDT_FORMAL_REPO_PATH": str(workspace["project"]),
        "PDT_LOCK_CLI": str(workspace["cli"]),
        "PDT_LOCK_TIMEOUT": "3",
    }
    if broker_on:
        env["PDT_LOCK_BROKER"] = str(socket_path(workspace["project"]))
        env["PDT_LOCK_TASK_ID"] = task_id
    if allowed is not None:
        env["PDT_ALLOWED_WRITE_FILES"] = json.dumps(allowed)
        env["PDT_UNDECLARED_WRITE_DIR"] = str(workspace["markers"])
    if read_only:
        env["PDT_READONLY_TASK"] = "1"
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [sys.executable, str(workspace["script"])],
        input=json.dumps({"tool_input": {"file_path": str(target)}}),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    return proc.returncode, proc.stderr


def _marker_lines(workspace, task_id="t1"):
    path = workspace["markers"] / f"{task_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# the permission
# ---------------------------------------------------------------------------
def test_a_declared_file_is_allowed(workspace, broker):
    """The happy path must be untouched — a rule that refuses everything
    would pass every refusal test below and break every task."""
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=["backend/a.py"],
    )
    assert code == 0, stderr
    assert broker.held("t1") == ["backend/a.py"], (
        "an allowed edit must still take its lock — the declaration "
        "replaces unordered acquisition, it does not replace locking"
    )


def test_an_undeclared_file_is_refused(workspace, broker):
    """The invariant itself."""
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "rogue.py",
        allowed=["backend/a.py"],
    )
    assert code == 2, stderr
    assert "did not declare" in stderr, (
        f"the refusal must say what is wrong, not merely that it failed: "
        f"{stderr!r}"
    )
    assert str(workspace["project"] / "backend" / "rogue.py") in stderr


def test_the_refusal_names_the_declaration_that_has_to_change(workspace, broker):
    """An operator reading the log has to be able to act on it."""
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "rogue.py",
        allowed=["backend/a.py"],
    )
    assert code == 2
    assert "files_to_modify" in stderr, (
        f"the refusal must name the field that has to be edited: {stderr!r}"
    )
    assert "not a lock conflict" in stderr.lower(), (
        f"the refusal must say that waiting will not clear it, or the "
        f"sub-agent retries forever: {stderr!r}"
    )


def test_the_refusal_happens_before_the_broker_acquire(workspace, broker):
    """A refused write must leave no lock row behind.

    Refusing after the acquire would leave an entry only the executor's
    release can drop, and a task that can never complete a write is
    exactly the kind that gets killed mid-run.
    """
    code, _ = _run_hook(
        workspace, workspace["project"] / "backend" / "rogue.py",
        allowed=["backend/a.py"],
    )
    assert code == 2
    assert broker.held("t1") == [], (
        "a refused undeclared write still registered a lock"
    )


def test_a_declaration_matches_exactly_and_never_by_prefix(workspace, broker):
    """A sibling path is not a declared path.

    ``backend/a.py`` must not authorise ``backend/a.py.bak`` or
    ``backend/ab.py``; a prefix match would quietly widen the permission
    to every path that shares a first character.
    """
    for target in ("backend/a.py.bak", "backend/ab.py"):
        code, stderr = _run_hook(
            workspace, workspace["project"] / target,
            allowed=["backend/a.py"],
        )
        assert code == 2, f"{target} was allowed by a prefix match: {stderr!r}"


def test_a_directory_declaration_matches_nothing(workspace, broker):
    """A directory entry authorises no file, loudly.

    This is deliberate and it is the safe direction. A directory that
    matched everything under it would restore the exact hole this closes
    (looks allowed, holds no lock); failing instead tells the task its
    declaration is incomplete, which is true. ``agent`` refuses a
    directory declaration before it ever locks one, on the same grounds.
    """
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=["backend"],
    )
    assert code == 2, stderr
    assert "did not declare" in stderr


def test_an_absolute_declaration_matches_the_same_file(workspace, broker):
    """Both spellings of one path are one permission.

    A task that declares an absolute path is declaring the file it means;
    refusing it would be a rule that fails for a correct declaration.
    """
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=[str(workspace["project"] / "backend" / "a.py")],
    )
    assert code == 0, stderr


def test_a_leading_dot_slash_declaration_still_matches(workspace, broker):
    """``./backend/a.py`` and ``backend/a.py`` are one path."""
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=["./backend/a.py"],
    )
    assert code == 0, stderr


def test_a_task_with_no_slip_is_unchecked(workspace, broker):
    """No ``PDT_ALLOWED_WRITE_FILES`` means no permission check.

    This is the fail-open path and it is load-bearing: the refiner's own
    queries, the verification sub-agents and any standalone
    ``coding_tool`` call run with no task scope and must keep working. A
    guard that refused when it had no slip would brick all of them.
    """
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "anything.py",
    )
    assert code == 0, stderr


def test_an_empty_declaration_refuses_every_project_write(workspace, broker):
    """``[]`` is a slip for nothing, and says so.

    Distinct from no slip at all, which is what keeps this from being a
    trap: the executor only emits ``[]`` for a task whose declaration
    listed no files, and the read-only stamp is the right answer there.
    """
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=[],
    )
    assert code == 2, stderr
    assert "did not declare" in stderr


def test_unparsable_slip_fails_open(workspace, broker):
    """A corrupt slip must not brick every task.

    ``json.loads`` raising means the stamp is malformed — a bug in the
    executor, not a task's mistake. Failing open keeps the blast radius
    at "the new check is off"; failing closed would take down every
    running task in the plan.
    """
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        extra_env={"PDT_ALLOWED_WRITE_FILES": "{not json"},
    )
    assert code == 0, stderr


# ---------------------------------------------------------------------------
# the surfaces that were never brokered stay unbrokered
# ---------------------------------------------------------------------------
def test_a_plans_dir_write_is_not_checked(workspace, broker):
    """``plans/`` never went through the broker and still must not be
    declared for: those are shared state files with their own writers and
    their own contracts, owned by the executor, not by a sub-agent."""
    target = workspace["plans"] / "20260101-a-plan" / "tasks.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    code, stderr = _run_hook(workspace, target, allowed=["backend/a.py"])
    assert code == 0, stderr


def test_a_temp_scratch_write_is_not_checked(workspace, broker):
    """Scratch space is not the task's edit surface either."""
    target = Path(tempfile.gettempdir()) / "pdt-allowed-write-probe.txt"
    code, stderr = _run_hook(workspace, target, allowed=["backend/a.py"])
    assert code == 0, stderr


# ---------------------------------------------------------------------------
# the two refusals must not be confused with one another
# ---------------------------------------------------------------------------
def test_the_readonly_refusal_is_still_the_readonly_one(workspace, broker):
    """A read-only task's refusal must not change wording.

    The two rules answer different questions and are diagnosed
    differently (``undeclared_write`` vs. a read-only task). Sharing a
    message would leave the executor classifying a read-only task from
    the undeclared-write rule, and its refiner instruction would then add
    a targeted file task to a task that is supposed to change nothing.
    """
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=None, read_only=True,
    )
    assert code == 2
    assert "declared no files_to_modify" in stderr, stderr
    assert "did not declare" not in stderr, (
        f"the read-only refusal must not reuse the undeclared-write "
        f"wording: {stderr!r}"
    )


def test_a_readonly_task_is_refused_before_the_declaration_check(workspace, broker):
    """Read-only is the prior question: a task that may write nothing has
    no declaration to be checked against, and the ``[]`` slip would
    otherwise produce the wrong message."""
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=[], read_only=True,
    )
    assert code == 2
    assert "declared no files_to_modify" in stderr, stderr


def test_a_declaring_task_is_unaffected_by_a_readonly_sibling(workspace, broker):
    """The stamp is per task, never per process or per project."""
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=["backend/a.py"], read_only=False,
    )
    assert code == 0, stderr


# ---------------------------------------------------------------------------
# the marker channel
# ---------------------------------------------------------------------------
def test_a_refused_write_records_the_path_in_the_task_marker(workspace, broker):
    """The classification cannot depend on the sub-agent's prose.

    The executor classifies from ``test_reason`` / ``coder_response``,
    and the hook's stderr only ever reaches the sub-agent — which is
    free to paraphrase it, quote half of it, or drop it. The marker is
    the machine-readable second channel that makes the classification
    describe what happened rather than what the model reported.
    """
    rogue = workspace["project"] / "backend" / "rogue.py"
    code, _ = _run_hook(
        workspace, rogue, allowed=["backend/a.py"], task_id="7-2",
        extra_env={"PDT_DECLARED_MANIFEST_HASH": "deadbeefdeadbeef"},
    )
    assert code == 2

    lines = _marker_lines(workspace, "7-2")
    assert len(lines) == 1, lines
    assert lines[0]["path"] == str(rogue), lines
    assert lines[0]["task"] == "7-2", lines
    # ``path`` is the field every historical reader of this marker
    # knows; it must keep carrying the same value under its own name.
    assert lines[0]["attempted_path"] == str(rogue), lines
    assert lines[0]["attempted_path"] == lines[0]["path"], lines
    # The declaration that was in force when the task reached outside
    # it, and the lineage the attempt belongs to. A split child writing
    # "7-2" must not be counted as a sibling of "7"'s own attempts.
    assert lines[0]["declared_manifest_hash"] == "deadbeefdeadbeef", lines
    assert lines[0]["lineage"] == "7", lines


def test_a_task_with_no_declaration_hash_still_gets_the_marker_fields(workspace, broker):
    """The new fields are present even when the stamp is absent.

    A missing env var means ``""``, not a missing key: a reader that
    distinguishes "the declaration was empty" from "we were never told"
    has to be able to, and a marker whose shape changes with the
    environment is a marker every reader has to guard for.
    """
    code, _ = _run_hook(
        workspace, workspace["project"] / "backend" / "rogue.py",
        allowed=["backend/a.py"],
    )
    assert code == 2
    entry = _marker_lines(workspace)[0]
    assert entry["declared_manifest_hash"] == "", entry
    assert entry["lineage"] == "t1", entry


def test_an_allowed_write_records_nothing(workspace, broker):
    """The marker is a refusal log, not an access log.

    An entry written on success would make the executor claim a task
    failed for an undeclared write it never attempted.
    """
    code, _ = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=["backend/a.py"],
    )
    assert code == 0
    assert _marker_lines(workspace) == []


def test_the_marker_is_per_task(workspace, broker):
    """One task's refusals must not be attributed to its sibling."""
    _run_hook(workspace, workspace["project"] / "backend" / "r1.py",
              allowed=["backend/a.py"], task_id="t1")
    _run_hook(workspace, workspace["project"] / "backend" / "r2.py",
              allowed=["backend/a.py"], task_id="t2")
    assert len(_marker_lines(workspace, "t1")) == 1
    assert len(_marker_lines(workspace, "t2")) == 1


def test_a_refusal_still_happens_when_the_marker_cannot_be_written(workspace, broker):
    """The marker is best-effort; the refusal is not.

    An unwritable marker directory is the exact condition under which a
    silent pass would be invisible — so the exit code has to be produced
    regardless of whether the side-channel worked.
    """
    blocker = workspace["project"] / "not-a-dir"
    blocker.write_text("")
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "rogue.py",
        allowed=["backend/a.py"],
        extra_env={"PDT_UNDECLARED_WRITE_DIR": str(blocker)},
    )
    assert code == 2, stderr
    assert "did not declare" in stderr


# ---------------------------------------------------------------------------
# the deadlock verdict reaching the hook
# ---------------------------------------------------------------------------
def test_the_deadlock_exit_code_is_refused_and_called_a_deadlock(workspace, broker):
    """CLI exit 5 means "waiting can never help", and must say so.

    Exit 3 (busy) tells the sub-agent to retry; exit 5 must not, because
    the arrangement provably cannot make progress. Silently treating 5 as
    "broker unavailable, allow the edit" would be the worst possible
    response — it would let a task write the exact file that is wedging
    the run.

    The CLI is stubbed rather than driven through a real cycle: what is
    under test here is the hook's handling of the code, and the
    broker-to-code mapping is pinned in ``test_file_lock_broker_deadlock``.
    """
    stub = workspace["project"] / "stub_cli.py"
    stub.write_text("import sys\nsys.exit(5)\n")
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "a.py",
        allowed=["backend/a.py"],
        extra_env={"PDT_LOCK_CLI": str(stub)},
    )
    assert code == 2, (
        f"a deadlock must refuse the edit, not allow it: {stderr!r}"
    )
    assert "deadlock" in stderr.lower(), stderr
    assert "not retryable" in stderr.lower(), (
        f"the message must tell the sub-agent not to wait it out: {stderr!r}"
    )
    assert broker.held("t1") == [], (
        "a deadlock-refused edit left a lock row behind"
    )


# ---------------------------------------------------------------------------
# the seam: declaration -> PDT_ALLOWED_WRITE_FILES in the --settings payload
# ---------------------------------------------------------------------------
#
# The env var has to ride the ``--settings`` payload (flagSettings), not
# the child process env: the CLI folds its settings scopes into
# ``process.env`` last-wins, so an ``env=`` value would be clobbered by
# whatever the machine's user settings say.
def test_the_seam_stamps_the_declaration_of_a_bound_task():
    """Driven through the real executor binding, not by handing the
    answer in.

    The thing that can be wrong is the PAIR — what the executor binds and
    what the guard is told — so a test that called
    ``current_task_allowed_files()`` and passed its result to the stamper
    would pass even if the executor bound the wrong field.
    """
    from coding_tool import _stamp_allowed_write_files
    from usage_registry import plan_usage_context

    with plan_usage_context(task_id="7", task_allowed_files=["backend/a.py"]):
        s = {"env": {}}
        _stamp_allowed_write_files(s, "7")
    assert json.loads(s["env"]["PDT_ALLOWED_WRITE_FILES"]) == ["backend/a.py"]


def test_the_marker_hash_matches_the_sorted_declaration():
    """The declaration is a SET; its hash must not read its order.

    Two attempts at the same task would otherwise carry two different
    ``declared_manifest_hash`` values for one declaration — the planner
    emitted the files in a different order, or the context rebuilt the
    list — and the executor would conclude the declaration changed under
    the task when nothing about the permission did. Conversely a
    declaration that really did change must hash differently, or "the
    same declaration reached for another file" and "a new declaration
    also cannot stop it" are indistinguishable.
    """
    import hashlib

    from coding_tool import _stamp_allowed_write_files
    from usage_registry import plan_usage_context

    def _hash_for(declaration):
        with plan_usage_context(task_id="7", task_allowed_files=declaration):
            s = {"env": {}}
            _stamp_allowed_write_files(s, "7")
        return s["env"]["PDT_DECLARED_MANIFEST_HASH"]

    reordered = _hash_for(["b.py", "a.py"])
    assert reordered == _hash_for(["a.py", "b.py"]), (
        "the same declaration hashed two ways purely because of the order "
        "the planner emitted it in"
    )
    assert reordered != _hash_for(["a.py", "c.py"]), (
        "a declaration naming a different file must not hash the same"
    )
    # And it is the documented function of the declaration, not an
    # opaque tag: 16 hex chars of sha256 over the sorted, newline-joined
    # strings, taken verbatim (no resolve — the guard matches lexically,
    # so resolving here would hash something never compared).
    expected = hashlib.sha256("a.py\nb.py".encode()).hexdigest()[:16]
    assert reordered == expected
    assert len(reordered) == 16 and all(
        c in "0123456789abcdef" for c in reordered
    )


def test_a_read_only_task_gets_no_slip():
    """No declaration, no slip — the read-only stamp is the right answer.

    An empty slip would read as "may touch nothing" and refuse every
    write with the wrong message, so the key must be absent, not ``[]``.
    """
    from coding_tool import _stamp_allowed_write_files
    from usage_registry import plan_usage_context

    with plan_usage_context(task_id="7", task_read_only=True):
        s = {"env": {}}
        _stamp_allowed_write_files(s, "7")
    assert "PDT_ALLOWED_WRITE_FILES" not in s["env"]


def test_outside_a_task_context_no_slip_is_written():
    """What keeps the refiner's own queries and the verification
    sub-agents working: none of them run under a task scope, and a slip
    with no task behind it would refuse everything they edit."""
    from coding_tool import _stamp_allowed_write_files
    from usage_registry import plan_usage_context

    with plan_usage_context(plan_id="p1"):
        s = {"env": {}}
        _stamp_allowed_write_files(s, None)
    assert "PDT_ALLOWED_WRITE_FILES" not in s["env"]


def test_the_slip_does_not_survive_into_the_next_dispatch():
    """``settings_data`` is rebuilt per dispatch from the pristine
    template, so a slip cannot leak onto the next task in the process."""
    import copy

    from coding_tool import _stamp_allowed_write_files
    from usage_registry import plan_usage_context

    template = {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1"}}
    pristine = copy.deepcopy(template)

    with plan_usage_context(task_id="1", task_allowed_files=["a.py"]):
        first = copy.deepcopy(template)
        _stamp_allowed_write_files(first, "1")
        assert json.loads(first["env"]["PDT_ALLOWED_WRITE_FILES"]) == ["a.py"]
        assert pristine == template, "the cached template was mutated"

    with plan_usage_context(task_id="2", task_read_only=True):
        second = copy.deepcopy(template)
        _stamp_allowed_write_files(second, "2")
        assert "PDT_ALLOWED_WRITE_FILES" not in second["env"]


def test_the_env_var_is_the_one_the_guard_reads():
    """The stamper's name and the guard's name are the same name.

    A rename on either side leaves both sides green: the stamp test
    asserts a dict key, the hook test asserts behaviour driven by an env
    var the test sets by hand. Only this ties them together.
    """
    import inspect

    import coding_tool

    assert '"PDT_ALLOWED_WRITE_FILES"' in coding_tool._EDIT_WRITE_FORMAL_REPO_GUARD_SRC
    assert "PDT_ALLOWED_WRITE_FILES" in inspect.getsource(
        coding_tool._stamp_allowed_write_files
    )


# ---------------------------------------------------------------------------
# symlinks: the declaration is lexical, the guard is resolved
# ---------------------------------------------------------------------------
#
# ``R(p)`` in the guard resolves the path BEFORE any rule looks at it.
# That is what makes the containment rule and the declaration check
# agree on one file — and it is also what a symlink can attack. These
# two cases are the whole surface: a link that leaves the project, and a
# link that stays inside it while naming something else.
def _real_home() -> Path:
    """The home this run was launched with — not the redirected one.

    Same helper, same reason and same env var as
    ``tests/integration/test_edit_write_lock_hook.py``. The guard allows
    all temp scratch space unconditionally, and WHERE pytest puts
    ``tmp_path`` is a property of the machine, not of this code: on Linux
    ``TMPDIR`` is unset, so the basetemp IS ``/tmp`` and a ``tmp_path``
    target is *legitimately* allowed there. A "symlink to a file outside
    the project" built under ``tmp_path`` would therefore assert nothing
    about the containment rule and pass on macOS while failing on Linux.
    ``$HOME`` is outside every allowed root on both platforms; the
    *launch* home is used because ``install_hermetic_env()`` redirects
    ``$HOME`` at a tempdir path, which the scratch clause allows.
    """
    value = os.environ.get("PDT_TEST_REAL_HOME")
    if not value:
        raise RuntimeError(
            "PDT_TEST_REAL_HOME is unset; install_hermetic_env() must run "
            "before this module derives a path. Falling back to Path.home() "
            "would return the redirected tempdir home, which the guard "
            "allows unconditionally."
        )
    return Path(value)


def test_a_symlink_to_outside_the_project_is_refused(workspace, broker):
    """A link inside the workspace cannot launder a write out of it.

    The declaration names a path INSIDE the project, so a purely lexical
    check reads this as an authorised edit — and the bytes land wherever
    the link points. The refusal therefore has to come from the
    containment rule, resolved, before the declaration is consulted at
    all; the lock must not be taken on the way out.
    """
    outside = _real_home() / "pdt-symlink-probe" / "secret.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    try:
        outside.write_text("not yours")
        link = workspace["project"] / "backend" / "sneaky.py"
        link.symlink_to(outside)

        code, stderr = _run_hook(workspace, link, allowed=["backend/sneaky.py"])
        assert code == 2, (
            f"a symlink pointing out of the workspace was allowed: {stderr!r}"
        )
        assert "outside the project dir" in stderr, stderr
        assert broker.held("t1") == [], (
            "an out-of-project write left a lock row behind"
        )
    finally:
        outside.unlink(missing_ok=True)
        outside.parent.rmdir()


def test_a_symlink_inside_the_project_does_not_authorize_its_target_spelling(
    workspace, broker
):
    """The declaration is checked against the RESOLVED path.

    The check is exact-match on a path the guard has already resolved,
    so what the declaration has to name is the file the write will
    actually land in. Both directions are asserted, because only
    asserting the refusing one would leave a rule that refuses every
    symlink write — including the legitimate one, which is what a
    fail-closed slip looks like when it is wrong.
    """
    real = workspace["project"] / "real.txt"
    real.write_text("payload")
    link = workspace["project"] / "link.txt"
    link.symlink_to(real)

    # Declared as the link, written as the link: resolves to real.txt,
    # which the declaration never named. Fail closed, loudly.
    code, stderr = _run_hook(
        workspace, link, allowed=["link.txt"], task_id="t1"
    )
    assert code == 2, (
        f"a declaration of 'link.txt' authorised the write it resolves to: "
        f"{stderr!r}"
    )
    assert "did not declare" in stderr, stderr
    assert broker.held("t1") == [], (
        "a refused symlink write left a lock row behind"
    )

    # Declared as the file it actually is, written as the link: the
    # resolved path matches, so this is the ordinary authorised edit and
    # must not be refused.
    code, stderr = _run_hook(
        workspace, link, allowed=["real.txt"], task_id="t2"
    )
    assert code == 0, (
        f"declaring the resolved path must authorise the write: {stderr!r}"
    )
    assert broker.held("t2") == ["real.txt"], broker.held("t2")


# ---------------------------------------------------------------------------
# the executor's end of the marker channel
# ---------------------------------------------------------------------------
@pytest.fixture
def drain(tmp_path, monkeypatch):
    """``(agent, markers_dir)`` with the marker directory thrown away.

    ``_take_undeclared_writes`` resolves the directory through
    ``tempfile.gettempdir()``, which production shares with every other
    process on the machine. Redirecting it keeps the test from reading —
    or worse, truncating — another run's markers.
    """
    import tempfile as tempfile_mod
    from unittest.mock import Mock

    import subprocess

    markers = tmp_path / "pdt-undeclared-writes"
    monkeypatch.setattr(
        tempfile_mod, "gettempdir", lambda: str(tmp_path), raising=True
    )
    project = tmp_path / "project"
    project.mkdir()
    # ``AutonomousAgent`` builds a ``GitManager`` eagerly, which wants a
    # repository at the project dir.
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project), capture_output=True, text=True, check=True,
    )
    agent = AutonomousAgent(
        requirement=None, project_dir=project, coding_tool=Mock(),
    )
    return agent, markers


def _write_marker(markers_dir, task_id, paths):
    markers_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"task": task_id, "path": p}) for p in paths]
    (markers_dir / f"{task_id}.jsonl").write_text("\n".join(lines) + "\n")


def test_draining_the_marker_returns_the_refused_paths(drain):
    draining_agent, markers = drain
    _write_marker(markers, "7", ["/w/a.py", "/w/b.py"])
    assert draining_agent._take_undeclared_writes("7") == ["/w/a.py", "/w/b.py"]


def test_draining_is_a_take_not_a_peek(drain):
    """The second read must be empty.

    Without the truncation, a stale marker from an earlier attempt
    re-appears in every later failure's classification and in the
    refiner's prompt forever — and the task is told to declare a file it
    stopped reaching for two retries ago.
    """
    draining_agent, markers = drain
    _write_marker(markers, "7", ["/w/a.py"])
    assert draining_agent._take_undeclared_writes("7") == ["/w/a.py"]
    assert draining_agent._take_undeclared_writes("7") == []
    assert (markers / "7.jsonl").read_text() == ""


def test_draining_deduplicates_repeated_attempts_at_one_file(drain):
    """Three refusals of one path is one diagnosis, not three.

    A sub-agent that retries an edit it was already refused writes the
    same marker three times; the classification would then read as a
    three-path problem.
    """
    draining_agent, markers = drain
    _write_marker(markers, "7", ["/w/a.py"] * 3)
    assert draining_agent._take_undeclared_writes("7") == ["/w/a.py"]


def test_draining_a_missing_marker_is_empty_not_an_error(drain):
    """Most failures have no marker at all; that must be the quiet case."""
    draining_agent, _ = drain
    assert draining_agent._take_undeclared_writes("nothing-here") == []


def test_draining_survives_a_torn_line(drain):
    """Best effort means best effort.

    Two processes appending to one file can interleave a partial line. A
    crash there must cost the classification its specificity, not take
    down the task's failure path.
    """
    draining_agent, markers = drain
    markers.mkdir(parents=True, exist_ok=True)
    (markers / "7.jsonl").write_text(
        json.dumps({"task": "7", "path": "/w/a.py"})
        + "\n{not json\n"
        + json.dumps({"task": "7", "path": "/w/b.py"}) + "\n"
    )
    assert draining_agent._take_undeclared_writes("7") == ["/w/a.py", "/w/b.py"]


def test_the_drain_is_scoped_to_one_task(drain):
    """One task's refusals must never be blamed on its sibling."""
    draining_agent, markers = drain
    _write_marker(markers, "7", ["/w/a.py"])
    _write_marker(markers, "8", ["/w/b.py"])
    assert draining_agent._take_undeclared_writes("7") == ["/w/a.py"]
    assert draining_agent._take_undeclared_writes("8") == ["/w/b.py"]


def test_a_task_id_with_path_characters_does_not_escape_the_marker_dir(drain):
    """The guard sanitises the task id into a filename; so must the reader.

    Both ends use the same ``[alnum] or - or _`` rule, so a task id
    containing ``/`` cannot make the reader look at some other file.
    """
    draining_agent, markers = drain
    _write_marker(markers, "7_1", ["/w/a.py"])
    assert draining_agent._take_undeclared_writes("7-1") == [], (
        "a different task id must not read another task's marker"
    )
    assert draining_agent._take_undeclared_writes("7_1") == ["/w/a.py"]


def test_the_refusal_text_the_executor_writes_classifies_with_its_paths(drain):
    """The two ends meet: drained paths in, ``undeclared_write`` out.

    The marker channel exists so the classification does not depend on the
    sub-agent relaying the hook's stderr. This asserts the chain closes —
    the drained paths produce text that carries the SAME paths — rather
    than that each half works in isolation. Two correct halves wired
    together wrongly would still hand the refiner a task to declare.
    """
    from failure_classification import UNDECLARED_WRITE, classify_failure

    draining_agent, markers = drain
    _write_marker(markers, "7", ["/w/a.py", "/w/b.py"])
    refused = draining_agent._take_undeclared_writes("7")
    assert refused, "the marker was not drained"

    failure = classify_failure("undeclared_write: " + ", ".join(refused[:5]))
    assert failure.kind == UNDECLARED_WRITE
    assert failure.detail == "/w/a.py, /w/b.py"
