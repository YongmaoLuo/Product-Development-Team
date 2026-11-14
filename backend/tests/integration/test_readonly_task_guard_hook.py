"""A task with no declaration cannot write (2026-10-17).

``files_to_modify`` is the task's write contract. Nothing used to enforce
it at the write, and the runtime conflict guard made the absence worse: a
task that declared nothing had its claim set re-derived by sweeping its own
prose for file names. A guess is per-task, so two undeclared siblings
produced DISJOINT guessed claim sets — and the conflict graph orders
*declared* claims, so disjoint guesses read as "no conflict" and the pair
was scheduled together while their real edit surfaces overlapped. Ordering
cannot help there; the hook's broker acquire was left serialising the
pile-up one unordered acquire at a time.

So the enforcement moved to where the declaration is spent:

    declaration is not a concrete list of paths
        =>  every Edit/Write this task's body attempts is refused

Pinned here, as a black box — the contract under test is an exit code and
a stream, so the only honest way to check one is to make the process
produce it (same reason as ``test_edit_write_lock_hook.py``):

* read-only task + Edit  => exit 2, with a message that says WHY and what
  to do about it;
* the refusal happens BEFORE the broker acquire, so it leaves no lock row
  for the executor to release;
* a task that DID declare is unaffected — including while a read-only
  sibling is running against the same broker, which is what "per task"
  has to mean;
* the refusal is unconditional — no carve-out for scratch space or the
  plans dir — and per task, never per process or per project.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from file_lock_broker import FileLockBroker  # noqa: E402
from file_lock_protocol import socket_path  # noqa: E402


#: How long the caller waits for one guard-hook spawn.
#:
#: The guard is a separate process, and on a loaded host the interpreter
#: alone can take the better part of a minute to come up.  The broker path
#: starts a SECOND interpreter (the lock CLI) on top of that, so the whole
#: path costs roughly two spawns.  A ceiling below that makes the CALLER
#: die before the guard has rendered its verdict -- the case then reports
#: a broken contract for what is really a slow machine.
#:
#: This is the test's own waiting ceiling and nothing else: no exit code,
#: no stderr match, no assertion and no loop count moves with it.
_HOOK_SPAWN_TIMEOUT_S = 150


@pytest.fixture
def workspace(tmp_path):
    """A project dir, a plans dir, and the guard script, ready to invoke."""
    from coding_tool import _ensure_guard_hook_path

    project = tmp_path / "project"
    plans = tmp_path / "plans"
    project.mkdir()
    plans.mkdir()
    return {
        "project": project,
        "plans": plans,
        "script": _ensure_guard_hook_path(),
        "cli": _BACKEND_DIR / "file_lock_cli.py",
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


def _run_hook(workspace, target, *, broker_on, task_id="t1",
              read_only=False, extra_env=None):
    """Run the guard hook for one Edit, returning ``(exit_code, stderr)``."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PDT_PROJECT_DIR": str(workspace["project"]),
        "PDT_PLANS_DIR": str(workspace["plans"]),
        "PDT_FORMAL_REPO_PATH": str(workspace["project"]),
        "PDT_LOCK_CLI": str(workspace["cli"]),
    }
    if broker_on:
        env["PDT_LOCK_BROKER"] = str(socket_path(workspace["project"]))
        env["PDT_LOCK_TASK_ID"] = task_id
        env["PDT_LOCK_TIMEOUT"] = "3"
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
        timeout=_HOOK_SPAWN_TIMEOUT_S,
    )
    return proc.returncode, proc.stderr


# ---------------------------------------------------------------------------
# the refusal
# ---------------------------------------------------------------------------

def test_a_readonly_task_may_not_edit(workspace):
    """The invariant itself."""
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "app.py",
        broker_on=True, read_only=True,
    )

    assert code == 2
    assert "files_to_modify" in stderr, (
        f"the refusal must name the declaration it is about: {stderr!r}"
    )
    assert "__NO_FILE_CHANGES__" in stderr, (
        f"the refusal must offer the read-only spelling, or a read-only "
        f"task has no way to say so and gets stuck: {stderr!r}"
    )


def test_the_refusal_says_it_is_not_a_lock_conflict(workspace):
    """The refusal is permanent; saying so stops a sub-agent retrying.

    The neighbouring exit 2 (another task holds the file) is resolved by
    waiting. This one is not, and a sub-agent told to "wait for it to
    finish" will sit there until the harness kills it.
    """
    _, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "app.py",
        broker_on=True, read_only=True,
    )

    assert "not a lock conflict" in stderr
    assert "wait" in stderr.lower()


def test_the_refusal_names_the_task(workspace):
    """Two read-only siblings must be tellable apart in the logs."""
    _, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "app.py",
        broker_on=True, task_id="12-3", read_only=True,
    )

    assert "12-3" in stderr


def test_a_readonly_task_may_not_write_either(workspace):
    """Write is the same matcher; both are refused."""
    code, stderr = _run_hook(
        workspace, workspace["project"] / "new_file.py",
        broker_on=True, read_only=True,
    )

    assert code == 2
    assert "files_to_modify" in stderr


# ---------------------------------------------------------------------------
# it refuses BEFORE the broker acquire
# ---------------------------------------------------------------------------

def test_the_refusal_leaves_no_lock_behind(workspace, broker):
    """A refused edit must not create a row only the executor can drop.

    The refusal is placed ahead of the acquire precisely so a read-only
    task — which by definition will never finish a write, and so is the
    kind of task that gets killed mid-run — cannot strand a lock.
    """
    code, _ = _run_hook(
        workspace, workspace["project"] / "backend" / "app.py",
        broker_on=True, task_id="ro-1", read_only=True,
    )

    assert code == 2
    assert broker.held("ro-1") == [], (
        "the refused edit still took a broker lock; the executor would "
        "have to release it on a path that never completes"
    )


def test_the_refusal_does_not_wait_for_the_broker(workspace):
    """No acquire means no wait: the refusal cannot be blocked by a peer."""
    import time

    started = time.monotonic()
    code, _ = _run_hook(
        workspace, workspace["project"] / "backend" / "app.py",
        broker_on=True, read_only=True, extra_env={"PDT_LOCK_TIMEOUT": "30"},
    )
    elapsed = time.monotonic() - started

    assert code == 2
    # PDT_LOCK_TIMEOUT was 30s. Generously bounded so a slow machine does
    # not flake this, but far below 30 — the point is "did not wait".
    assert elapsed < 20, (
        f"the read-only refusal took {elapsed:.1f}s with a 30s lock "
        f"timeout — it must refuse before the acquire, not after"
    )


# ---------------------------------------------------------------------------
# per task, not per process
# ---------------------------------------------------------------------------

def test_a_declared_task_is_unaffected(workspace, broker):
    """Negative control for every refusal above.

    Same hook, same project, same broker, same file — only the
    read-only stamp differs. Without this, a bug that set the stamp
    unconditionally would satisfy every assertion in the previous section.
    """
    code, stderr = _run_hook(
        workspace, workspace["project"] / "backend" / "app.py",
        broker_on=True, task_id="rw-1", read_only=False,
    )

    assert code == 0, stderr
    assert broker.held("rw-1") == ["backend/app.py"]


def test_a_declared_sibling_writes_while_a_readonly_one_is_refused(workspace, broker):
    """"Per task" means one task's read-only-ness is not another's."""
    target = workspace["project"] / "backend" / "app.py"

    ro_code, _ = _run_hook(workspace, target, broker_on=True,
                           task_id="ro", read_only=True)
    rw_code, rw_err = _run_hook(workspace, target, broker_on=True,
                                task_id="rw", read_only=False)

    assert ro_code == 2
    assert rw_code == 0, rw_err
    assert broker.held("ro") == []
    assert broker.held("rw") == ["backend/app.py"]


def test_the_refusal_is_per_task_not_per_project(workspace):
    """A second task in the same project still writes.

    Guards against the stamp leaking through a shared, mutable settings
    dict: the env rides the ``--settings`` payload, which is rebuilt per
    dispatch, and a leaked value would make the whole project read-only
    after one undeclared task.
    """
    target = workspace["project"] / "a.py"

    _run_hook(workspace, target, broker_on=False, task_id="ro", read_only=True)
    code, stderr = _run_hook(workspace, target, broker_on=False,
                             task_id="rw", read_only=False)

    assert code == 0, stderr


# ---------------------------------------------------------------------------
# it is unconditional
# ---------------------------------------------------------------------------

def test_a_readonly_task_is_refused_in_every_allowed_zone(workspace):
    """No carve-out for scratch space or the plans dir.

    The rule is stated without exception ("Edit/Write of a task that
    declared nothing is refused"), so this pins the unconditional form
    rather than a narrower one. Scratch is deliberately NOT exempt even
    though it is not brokered: an exemption would be a hole in the very
    invariant, and a read-only investigation task can always report its
    findings in the task description it already has to write.
    """
    target = Path("/tmp/pdt-readonly-scratch-notes.md")
    code, stderr = _run_hook(workspace, target, broker_on=True, read_only=True)
    assert code == 2, stderr
    assert "files_to_modify" in stderr

    in_plans = workspace["plans"] / "notes.md"
    code, stderr = _run_hook(workspace, in_plans, broker_on=True, read_only=True)
    assert code == 2, stderr
    assert "files_to_modify" in stderr


# ---------------------------------------------------------------------------
# the seam: task -> env var
# ---------------------------------------------------------------------------

def test_the_context_reports_read_only_only_for_a_bound_task():
    """``current_task_is_readonly`` is the single source of the stamp.

    ``False`` with no task bound is load-bearing, not a default to
    tidy up: the refiner's own queries and the verification sub-agents
    run outside a task scope, and a refusal there would break them for a
    declaration they never had a chance to make.
    """
    from usage_registry import (
        current_task_id,
        current_task_is_readonly,
        plan_usage_context,
    )

    assert current_task_is_readonly() is False
    assert current_task_id() is None

    with plan_usage_context(task_id="7", task_read_only=True):
        assert current_task_is_readonly() is True
        assert current_task_id() == "7"

    with plan_usage_context(task_id="7", task_read_only=False):
        assert current_task_is_readonly() is False

    # Old callers that pass nothing at all are unaffected.
    with plan_usage_context(task_id="7"):
        assert current_task_is_readonly() is False

    # ...and the binding does not outlive the scope.
    assert current_task_is_readonly() is False


def test_the_executor_binds_the_verdict_from_the_declaration(monkeypatch):
    """The production path, end to end, decides read-only-ness.

    ``AutonomousAgent._run_task_with_provider_slot`` is where a task is
    turned into an execution. This drives that real method — with only the
    LLM call stubbed — and records what it bound, so the decision cannot
    be right in isolation and wrong where it is actually made. The seam it
    cannot cover (the ``--settings`` stamp inside the spawned CLI) is
    covered above at the hook's own boundary.
    """
    import asyncio
    import contextlib
    import subprocess
    import tempfile
    from pathlib import Path
    from unittest.mock import Mock

    import agent as agent_module
    from task import (
        NO_FILE_CHANGES_SENTINEL,
        UNKNOWN_MODIFICATIONS_SENTINEL,
        SubTask,
    )
    from usage_registry import plan_usage_context

    recorded: list = []

    @contextlib.contextmanager
    def _spy(**kwargs):
        recorded.append(kwargs)
        with plan_usage_context(**kwargs):
            yield

    monkeypatch.setattr(agent_module, "plan_usage_context", _spy)

    class _Controller:
        async def acquire(self, provider):
            return None

        def release(self, provider):
            return None

    tmp = Path(tempfile.mkdtemp())
    project = tmp / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project), capture_output=True, check=True,
    )

    a = agent_module.AutonomousAgent(
        requirement=None, project_dir=project, coding_tool=Mock(),
    )
    a._execute_task_with_retry = Mock(return_value=True)

    shapes = [
        (["backend/agent.py"], False, "concrete list"),
        (list(UNKNOWN_MODIFICATIONS_SENTINEL), True, "unknown sentinel"),
        (list(NO_FILE_CHANGES_SENTINEL), True, "no-file-changes sentinel"),
        ([], True, "explicit empty list"),
    ]
    loop = asyncio.new_event_loop()
    try:
        for files_to_modify, expect_read_only, label in shapes:
            recorded.clear()
            task = SubTask(
                id="1", title="t", description="d",
                files_to_modify=files_to_modify, test_command="echo ok",
            )
            loop.run_until_complete(
                a._run_task_with_provider_slot(task, _Controller(), None)
            )
            assert recorded, f"{label}: the executor bound no task context"
            assert recorded[0]["task_read_only"] is expect_read_only, (
                f"{label}: bound task_read_only="
                f"{recorded[0].get('task_read_only')!r}, expected "
                f"{expect_read_only!r}"
            )
            assert recorded[0]["task_id"] == task.id
    finally:
        loop.close()


def test_the_agent_wires_project_dir_into_its_refiner(tmp_path):
    """The refiner's validator is dead unless the agent hands it the dir.

    ``TaskRefiner`` builds its output validator only when it knows the
    project dir; ``refine()`` returns the model's candidate unvalidated
    when it does not. Asserted on the constructed agent so the wiring
    cannot be dropped while the refiner's own unit tests keep passing.
    """
    import subprocess
    from unittest.mock import Mock

    from agent import AutonomousAgent

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main"],
        cwd=str(project), capture_output=True, check=True,
    )

    a = AutonomousAgent(
        requirement=None, project_dir=project, coding_tool=Mock(),
    )

    assert a.refiner.project_dir == str(project), (
        "the refiner has no project_dir, so its output validator is None "
        "and step 3 (files_to_modify) never runs"
    )


# ---------------------------------------------------------------------------
# the seam: verdict -> PDT_READONLY_TASK in the --settings payload
# ---------------------------------------------------------------------------
#
# The env var has to ride the ``--settings`` payload (flagSettings), not
# the child process env: the CLI folds its settings scopes into
# ``process.env`` last-wins, so an ``env=`` value would be clobbered by
# whatever the machine's user settings say.

def test_the_seam_stamps_only_a_bound_readonly_task():
    """``_stamp_readonly_task`` is the only writer of the env var.

    Driven through the real executor binding
    (``plan_usage_context(task_read_only=...)``), not by calling
    ``current_task_is_readonly`` and handing the answer in — the thing
    that can be wrong is the pair of them together.
    """
    from coding_tool import _stamp_readonly_task
    from usage_registry import plan_usage_context

    def _settings() -> dict:
        return {"env": {}}

    # read-only task, bound
    with plan_usage_context(task_id="7", task_read_only=True):
        s = _settings()
        _stamp_readonly_task(s, "7")
    assert s["env"].get("PDT_READONLY_TASK") == "1"

    # task that declared its files, bound
    with plan_usage_context(task_id="7", task_read_only=False):
        s = _settings()
        _stamp_readonly_task(s, "7")
    assert "PDT_READONLY_TASK" not in s["env"]

    # no task bound at all, even if a verdict somehow leaked in
    s = _settings()
    _stamp_readonly_task(s, None)
    assert "PDT_READONLY_TASK" not in s["env"], (
        "outside a task context the stamp must not be written — that is "
        "what keeps the refiner's own queries working"
    )


def test_the_stamp_does_not_survive_into_the_next_dispatch():
    """The settings payload is rebuilt per dispatch; the key must not stick.

    ``settings_data`` is freshly loaded from the template each time (and
    the pristine cache is taken BEFORE this module writes into it), so a
    read-only dispatch cannot leave the key behind for the next task that
    shares the process. Asserted here by simulating the sequence.
    """
    import copy

    from coding_tool import _stamp_readonly_task
    from usage_registry import plan_usage_context

    template = {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "1"}}
    pristine = copy.deepcopy(template)

    with plan_usage_context(task_id="1", task_read_only=True):
        first = copy.deepcopy(template)
        _stamp_readonly_task(first, "1")
        assert first["env"]["PDT_READONLY_TASK"] == "1"
        assert pristine == template, "the cached template was mutated"

    with plan_usage_context(task_id="2", task_read_only=False):
        second = copy.deepcopy(template)
        _stamp_readonly_task(second, "2")
        assert "PDT_READONLY_TASK" not in second["env"]


def test_the_env_var_is_the_one_the_guard_reads():
    """The stamp's name and the guard's name are the same name.

    A rename on either side would leave both sides green: the stamp test
    asserts a dict key, the hook test asserts a behaviour driven by an env
    var this test sets by hand. Only this ties them together.
    """
    from coding_tool import _EDIT_WRITE_FORMAL_REPO_GUARD_SRC

    assert '"PDT_READONLY_TASK"' in _EDIT_WRITE_FORMAL_REPO_GUARD_SRC
    assert "PDT_READONLY_TASK" in (
        __import__("inspect").getsource(_stamp_readonly_task_src())
    )


def _stamp_readonly_task_src():
    import coding_tool

    return coding_tool._stamp_readonly_task


@pytest.mark.timeout(600)
def test_a_concurrent_rematerialisation_never_disarms_the_hook(workspace, monkeypatch):
    """The guard script is never visible half-written.

    Every ``import coding_tool`` re-materialises the shared tmpfile, and
    the reader is the hook subprocess. An in-place write truncates first,
    so a hook starting inside that window runs a 0-byte script and exits
    0 -- the read-only refusal silently disappears. CI hit exactly that
    (root-4, run 39014769298): exit 0 with empty stderr.

    The writer is bounded on purpose. A timed tight loop is a CPU and
    filesystem stressor, and this shard's sibling tests drive a real
    lock broker over a socket: under that load the broker CLI returns
    an unexpected code, the guard fails open by design, and their
    ``held()`` assertions see an empty table. Measured: 2/26 shard runs
    failed with the tight loop, 0/15 with this test deselected. 500
    rewrites at 1 ms still expose a non-atomic write without starving a
    neighbour.

    The falsifier is DETERMINISTIC, not a race that is usually empty.
    The writers below are threads in THIS process, so the pid cannot
    tell them apart: a scratch name derived from the pid alone is shared
    by every writer in the process. Sharing one scratch path is not
    untidy, it is the defect -- writer B truncates the file writer A is
    about to publish (A then publishes a half-written guard, so a reader
    sees a 0-byte script), and B afterwards finds its scratch file
    already renamed away (``FileNotFoundError``). Both symptoms land on
    a timing window, which is exactly why this test used to be an empty
    falsifier four times out of five: three reads against one writer
    almost never met the window, so it shipped a regression detector
    that did not detect.

    So the judgement does not wait for the race: the scratch path each
    write actually publishes from is captured (the source handed to
    ``os.replace`` IS the scratch path that write used) and asserted to
    be pairwise distinct. With one scratch path for the whole process
    the paths are equal and the assertion fails EVERY time -- no luck
    involved. The captured names are also checked for the shape the
    contract asks for: this process's pid (which separates writers of
    different processes) followed by a per-write unique id (which
    separates two writers inside this one). The reader stays as the
    black-box confirmation: it samples the published path while the
    writers rewrite it and must never see anything but a complete
    script -- and never an EMPTY one, which is the exact shape of the
    CI failure (a hook running a 0-byte script prints nothing and exits
    0, so the refusal silently disappears).

    The reader is dense on purpose, for the opposite reason: the window
    is a small slice of each 1 ms sleep, so how many times the reader
    looks decides whether it can catch anything at all. Measured
    against the in-place write, the first short body showed up at read
    1 twice, 256, 475, 1073 and worst 1431 of 20 trials (a read costs
    ~11 us). 20000 samples is ~14x the worst case observed and still
    bounded: it ends in ~0.2 s and stops the moment the writers' 500
    rewrites are done.
    """
    import os
    import threading
    import time

    import coding_tool

    path = coding_tool._ensure_guard_hook_path()
    src = coding_tool._EDIT_WRITE_FORMAL_REPO_GUARD_SRC
    stop = threading.Event()

    WRITERS = 3
    WRITES_PER_WRITER = 500

    # The evidence, collected from the publish step itself: ``os.replace``
    # is how a write becomes visible, so the source it is handed is the
    # scratch path THAT write used.
    real_os = coding_tool.os
    scratch_paths = []
    scratch_lock = threading.Lock()

    class _RecordingOs:
        """Delegates to the real ``os`` and records each publish source."""

        def __getattr__(self, name):
            return getattr(real_os, name)

        def replace(self, src_path, dst_path):
            with scratch_lock:
                scratch_paths.append(Path(src_path))
            return real_os.replace(src_path, dst_path)

    monkeypatch.setattr(coding_tool, "os", _RecordingOs())

    writer_errors = []

    def _rewrite():
        try:
            for _ in range(WRITES_PER_WRITER):
                if stop.is_set():
                    return
                coding_tool._materialise_guard_script(path)
                time.sleep(0.001)
        except Exception as exc:  # noqa: BLE001 -- recorded, asserted below
            writer_errors.append(repr(exc))

    writers = [threading.Thread(target=_rewrite, daemon=True)
               for _ in range(WRITERS)]
    for writer in writers:
        writer.start()
    try:
        # The black-box half of the falsifier, inside the rewrite window:
        # every read here races the writers, and an atomic replace makes
        # a short body unreachable. Bounded so a healthy writer cannot
        # spin forever -- 500 rewrites at 1 ms finish in ~0.66 s, long
        # before 20000 reads are spent, so this loop cannot outlive the
        # race it is sampling.
        short = None
        for _ in range(20000):
            body = path.read_text(encoding="utf-8")
            if body != src:
                short = len(body)
                break
        assert short is None, (
            "a reader saw the guard script truncated to %d byte(s)%s; a hook "
            "starting then would exit 0 and allow the edit"
            % (short, " (it was EMPTY)" if short == 0 else "")
        )

        # The black-box symptom, still inside the same window: the guard
        # refuses while the file is being re-materialised. This is the CI
        # failure itself -- the hook ran against a 0-byte script, printed
        # nothing, and exited 0.
        for _ in range(3):
            code, stderr = _run_hook(
                workspace, workspace["project"] / "backend" / "app.py",
                broker_on=True, read_only=True,
            )
            assert code == 2, stderr

        # The deterministic half: every writer must have finished its
        # quota before the scratch paths are judged, otherwise the sample
        # is partial and the comparison below proves nothing.
        for writer in writers:
            writer.join(timeout=30)
        assert not writer_errors, (
            "a writer failed while re-materialising the guard script: %r"
            % (writer_errors[:3],)
        )
        assert len(scratch_paths) == WRITERS * WRITES_PER_WRITER, (
            "expected %d scratch paths (one per write), recorded %d"
            % (WRITERS * WRITES_PER_WRITER, len(scratch_paths))
        )
        distinct = len(set(scratch_paths))
        assert distinct == len(scratch_paths), (
            "two writers shared one scratch path: %d writes produced only "
            "%d distinct scratch path(s), so one writer can publish "
            "another writer's half-written script"
            % (len(scratch_paths), distinct)
        )
        # The other half of the contract: the scratch name carries this
        # process's pid (that is what separates writers of DIFFERENT
        # processes) plus a per-write unique component (that is what
        # separates two writers inside THIS process). Pairwise distinct
        # names that all carry the pid can only come from a per-write
        # unique id -- a pid-only name would be one name for all of them.
        pid = str(os.getpid())
        prefix = path.name + "." + pid + "."
        for scratch in scratch_paths:
            assert scratch.name.startswith(prefix), (
                "the scratch path %r does not carry this process's pid (%s) "
                "followed by a per-write unique id" % (scratch.name, pid)
            )
    finally:
        stop.set()
        for writer in writers:
            writer.join(timeout=5)


def test_a_failed_rewrite_leaves_no_scratch_file_behind(tmp_path, monkeypatch):
    """A failed write must not strand its scratch file.

    Every write now has a name of its own (the pid plus a per-write
    unique id), so a write that FAILS would leave a new orphan in the
    guard's directory every time -- the old pid-only name could strand
    at most one, and nothing would ever collect the rest. So a failed
    write removes its own scratch file and re-raises; the published path
    keeps the previous complete script, so a reader still never sees a
    half-written guard.
    """
    import pathlib

    import coding_tool

    target = tmp_path / "guard.py"
    coding_tool._materialise_guard_script(target)
    published = target.read_bytes()

    real_write_text = pathlib.Path.write_text

    def _explode(self, *args, **kwargs):
        if self.name.endswith(".tmp"):
            # A truncated write that then fails, the worst case: the
            # scratch file exists and holds half a script.
            real_write_text(self, "half written", encoding="utf-8")
            raise RuntimeError("disk full")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "write_text", _explode)
    try:
        with pytest.raises(RuntimeError):
            coding_tool._materialise_guard_script(target)
    finally:
        monkeypatch.undo()

    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == [target.name], (
        "a failed rewrite stranded %r next to the guard script" % (left,)
    )
    assert target.read_bytes() == published, (
        "a failed rewrite damaged the published guard script"
    )
