"""
Tests for ``test_command_quality``.

The detector exists because a task's completion verdict cross-checks the
subagent's ``TEST_RESULT`` claim against the command's exit code — and
some commands cannot produce a meaningful exit code. A command built as a
long ``&&`` chain whose mid-chain ``grep`` / ``ls`` links exit non-zero
when they find nothing is structurally incapable of reporting success:
every ``TEST_RESULT: PASSED`` the subagent reports is scored as a lie,
and the task burns retry cycles plus a refiner pass before anyone reads
the command.

The corpus tests at the bottom are the important ones: an earlier draft
of this detector flagged 13 of the 14 real commands in that plan's
``plan_tasks`` table (it rejected absolute paths), which is noise rather
than a gate. False positives are a regression here in exactly the same
way false negatives are.
"""

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from test_command_quality import (  # noqa: E402
    check_falsifiable,
    commands_of,
    find_semicolon_exit_issue,
    find_unreachable_targets,
    has_test_runner,
    inspect_command,
    inspect_task,
    is_usable,
    probe_scope_is_usable,
    _split_top_level_chain,
    _strip_quoted_spans,
)


# The verbatim command shape.
PROBE_CHAIN = (
    "cd ~/work/dev-checkout && "
    "grep -n 'TODO' native_ext/src/types.rs && "
    "ls -la target/release/libnative_ext.dylib 2>/dev/null && "
    "ls -la venv1/lib/python3.11/site-packages/native_ext/*.so 2>/dev/null && "
    "source venv1/bin/activate && "
    "python3 -c \"import native_ext; print(dir(native_ext.SignalInfo))\""
)


# ---------------------------------------------------------------------------
# The shape that is caught
# ---------------------------------------------------------------------------


def test_the_real_probe_chain_is_flagged():
    issues = inspect_command(PROBE_CHAIN)
    assert [i.code for i in issues] == ["probe_chain"]
    assert "grep" in issues[0].detail and "ls" in issues[0].detail


def test_probe_chain_explains_the_mechanism():
    """The detail must say *why*, or an operator cannot act on it."""
    detail = inspect_command(PROBE_CHAIN)[0].detail
    assert "non-zero" in detail
    assert "2>/dev/null" in detail, (
        "the redirect-still-propagates-the-status trap is the whole "
        "reason this command is broken; it has to be named"
    )


def test_is_usable_wrapper_agrees():
    assert is_usable(PROBE_CHAIN) is False
    assert is_usable("pytest -q") is True


# ---------------------------------------------------------------------------
# What is deliberately NOT caught
# ---------------------------------------------------------------------------


def test_empty_command_is_not_this_modules_problem():
    """A missing command belongs to the dual-signal gate, not here."""
    assert inspect_command("") == []
    assert inspect_command("   ") == []


def test_single_grep_is_a_legitimate_contract_check():
    """A lone probe in final position *is* the assertion.

    Several repair tasks are exactly this.
    """
    assert inspect_command("grep -n 'TODO' native_ext/src/types.rs") == []


def test_two_segment_chain_is_ordinary():
    assert inspect_command("cd /tmp && grep -q foo bar.txt") == []


def test_absolute_paths_are_not_flagged():
    """The regression that made the first draft useless.

    In this deployment absolute paths are the norm and the instruction:
    the plan's ``project_dir`` is absolute, and ``repair_generator``
    explicitly asks the LLM for absolute paths.
    """
    for cmd in (
        "cargo test --manifest-path /Users/a/b/Cargo.toml --test x",
        "cd /Users/a/b && source venv1/bin/activate && pytest tests/ -q",
        "cd /tmp && DEVELOPER_DIR=/Library/Developer/CommandLineTools "
        "timeout 600 cargo test --manifest-path /Users/a/b/Cargo.toml",
        "cd /Users/a/b && python3 -c \"import json;print(1)\"",
    ):
        assert inspect_command(cmd) == [], f"false positive on: {cmd}"


def test_a_chain_that_runs_tests_is_cleared():
    """Inspection on the way to a real test run is fine."""
    cmd = (
        "cd /Users/a/b && source venv/bin/activate && "
        "grep -q marker src/x.py && pytest tests/ -q"
    )
    assert inspect_command(cmd) == []


def test_setup_verbs_are_not_probes():
    """``cd`` / ``source`` / ``timeout`` front ordinary chains.

    Treating them as probes flagged every legitimate multi-step command
    in the run corpus.
    """
    cmd = (
        "cd /Users/a/b && source venv1/bin/activate && "
        "timeout 5400 python scripts/ci_local.py --tiers lint,unit"
    )
    assert inspect_command(cmd) == []


# ---------------------------------------------------------------------------
# Quoted spans are data, not commands (2026-09-20)
# ---------------------------------------------------------------------------


def test_strip_quoted_spans_removes_both_quote_styles():
    assert _strip_quoted_spans('a "b" c') == "a  c"
    assert _strip_quoted_spans("a 'b' c") == "a  c"
    assert _strip_quoted_spans('a "b\'c" d') == "a  d"


def test_strip_quoted_spans_honours_backslash_escapes():
    """``\\"`` inside a double-quoted span does not close it."""
    assert _strip_quoted_spans('echo "a\\"b" tail') == "echo  tail"


def test_runner_inside_quotes_does_not_count_as_running_tests():
    """The quoted-runner hole: a quoted ``cargo test`` is an argument, not a run.

    A generated command can quote a real cargo
    invocation as ``sys.argv[3]``; a substring search over the whole
    command counts that as "this command runs tests".
    """
    cmd = (
        "python3 -c \"import sys; sys.exit(0)\" "
        "'plan.json' 'VP-015' 'bash -c \"cargo test --test x\"'"
    )
    assert has_test_runner(cmd) is False

    # The same runner unquoted still counts.
    assert has_test_runner("cd /tmp && cargo test --test x") is True


# ---------------------------------------------------------------------------
# ``no_execution``: a python -c probe that decides pass/fail by reading a file
# ---------------------------------------------------------------------------

# The verbatim shape a spec echo takes.
SPEC_ECHO = (
    "python3 -c \"import json,sys;"
    "d=json.load(open(sys.argv[1],encoding='utf-8'));"
    "cur=[v for v in d['verification_points'] if v.get('id')==sys.argv[2]];"
    "sys.exit(1 if not cur or "
    "cur[0].get('test_command','').strip()==sys.argv[3] else 0)\" "
    "'/Users/u/ac/plans/x/verification_plan.json' 'VP-013' "
    "'bash -c \"cargo test --test signal_classification\"'"
)


def test_spec_echo_is_flagged():
    issues = inspect_command(SPEC_ECHO)
    assert [i.code for i in issues] == ["no_execution"]
    assert is_usable(SPEC_ECHO) is False


def test_spec_echo_detail_names_the_mechanism():
    detail = inspect_command(SPEC_ECHO)[0].detail
    assert "sys.exit(" in detail
    assert "vacuous" in detail


def test_a_one_liner_that_actually_exercises_code_is_not_flagged():
    """The regression that would make this check useless.

    A ``python -c`` that imports the module under test and asserts on it
    is a real (if terse) test, even though it calls ``sys.exit`` and
    starts no process.
    """
    for cmd in (
        'python3 -c "import native_ext,sys; '
        'sys.exit(0 if native_ext.X==1 else 1)"',
        'python3 -c "import os; os.path.exists(\'a\') and print(1)"',
        "python3 -c \"import json,sys;P='/Users/u/ac/plans/x.json'\"",
        "python3 -c \"import json,sys;d=json.load(open(sys.argv[1]))\" x.json VP-001",
    ):
        assert inspect_command(cmd) == [], f"false positive on: {cmd}"


def test_a_python_c_that_shells_out_is_not_flagged():
    """A program that spawns a process can exercise real code."""
    cmd = (
        'python3 -c "import subprocess,sys;'
        'r=subprocess.run([\'cargo\',\'test\']);sys.exit(r.returncode)"'
    )
    assert inspect_command(cmd) == []


# ---------------------------------------------------------------------------
# ``check_falsifiable``: the command must fail before the work is done
# ---------------------------------------------------------------------------


def test_falsifiable_rejects_a_command_that_already_passes(tmp_path):
    ok, detail = check_falsifiable("exit 0", cwd=str(tmp_path))
    assert ok is False
    assert "already exits 0" in detail


def test_falsifiable_accepts_a_command_that_fails(tmp_path):
    ok, detail = check_falsifiable("exit 3", cwd=str(tmp_path))
    assert ok is True
    assert "exit 3" in detail


def test_falsifiable_is_not_this_modules_problem_for_an_empty_command(tmp_path):
    ok, _ = check_falsifiable("", cwd=str(tmp_path))
    assert ok is False


def test_falsifiable_treats_a_timeout_as_inconclusive(tmp_path):
    ok, detail = check_falsifiable("sleep 5", cwd=str(tmp_path), timeout=1)
    assert ok is True
    assert "inconclusive" in detail


# ---------------------------------------------------------------------------
# ``probe_scope_is_usable``: the gate on EXECUTING a task's command
# ---------------------------------------------------------------------------
#
# 2026-09-23. The probe executes an LLM-authored shell command. Two
# properties make that acceptable: the command runs inside a declared
# project, and that project is not the backend's own checkout. The second one is
# not a stylistic preference — the backend's own fixtures carry
# ``test_command="pytest tests/ -v"``, so a probe pointed anywhere in
# the backend's tree runs a nested pytest over the whole suite, which re-enters
# the codepath that spawned it.
#
# The predicate is shared by both probe callers precisely because
# inlining it in one of them is what left the repair-side probe
# unguarded for a day.


def test_scope_refuses_when_no_workspace_is_declared():
    """``cwd=None`` means "inherit the caller's directory" — which is
    the backend's checkout, the one place the command must not run."""
    usable, reason = probe_scope_is_usable(None)
    assert usable is False
    assert "no project_dir" in reason


def test_scope_refuses_an_empty_string():
    usable, reason = probe_scope_is_usable("   ")
    assert usable is False
    assert "no project_dir" in reason


def test_scope_refuses_a_path_that_is_not_a_directory(tmp_path):
    usable, reason = probe_scope_is_usable(tmp_path / "does-not-exist")
    assert usable is False
    assert "not an existing directory" in reason


def test_scope_accepts_a_real_project_directory(tmp_path):
    usable, reason = probe_scope_is_usable(tmp_path)
    assert usable is True
    assert reason == ""


def test_scope_refuses_the_ac_checkout_itself():
    """The exact shape of the 2026-09-22 incident: the backend root."""
    usable, reason = probe_scope_is_usable(BACKEND_DIR.parent)
    assert usable is False
    assert "inside the backend's own checkout" in reason


def test_scope_refuses_anywhere_inside_the_ac_checkout():
    """``backend/`` and everything below it are equally off limits —
    that is the directory the unguarded probe actually ran in."""
    for sub in (BACKEND_DIR, BACKEND_DIR / "tests", BACKEND_DIR / "framework"):
        usable, reason = probe_scope_is_usable(sub)
        assert usable is False, f"{sub} must not be probeable"
        assert "inside the backend's own checkout" in reason


def test_a_command_with_no_workspace_is_never_executed():
    """End of the chain: the refusal is not advisory. A command that
    would have run is refused instead, and the reason is reported so an
    operator can see why a task was not probed."""
    ok, detail = check_falsifiable("exit 3")
    assert ok is False, (
        "a command with no declared workspace must not be probed; "
        "'exit 3' would otherwise return ok=True and look like a "
        "verified-falsifiable command"
    )
    assert "refused to run" in detail


# ---------------------------------------------------------------------------
# ``commands_of`` / ``inspect_task``: both schema forms
# ---------------------------------------------------------------------------
#
# A task carries its commands as ``test_commands`` (list, canonical — what
# the generator is told to write) or ``test_command`` (single string,
# legacy — what most of the corpus and every repair task uses).
# A check that reads only one of them passes every task that uses the
# other. That is exactly what happened on 2026-09-20: a regeneration
# produced 17 tasks, all list-form, and the checks reported zero problems.


def test_commands_of_reads_both_forms():
    assert commands_of({"test_commands": ["a", "b"]}) == ["a", "b"]
    assert commands_of({"test_command": "a"}) == ["a"]
    assert commands_of({"test_commands": ["a"], "test_command": "b"}) == ["a", "b"]


def test_commands_of_dedupes_and_drops_empties():
    assert commands_of({"test_commands": ["a"], "test_command": "a"}) == ["a"]
    assert commands_of({"test_commands": ["", "  ", "a"]}) == ["a"]
    assert commands_of({}) == []
    assert commands_of({"test_commands": None, "test_command": None}) == []


def test_commands_of_accepts_objects_not_just_dicts():
    """Both sides of the ``SubTask`` model boundary share this helper."""

    class _T:
        test_commands = ["a"]
        test_command = "b"

    assert commands_of(_T()) == ["a", "b"]


def test_inspect_task_sees_the_list_form():
    """The form the generator writes, which the old check was blind to."""
    task = {"id": "13", "test_commands": [SPEC_ECHO]}
    pairs = inspect_task(task)
    assert [issue.code for _, issue in pairs] == ["no_execution"]
    assert pairs[0][0] == SPEC_ECHO


def test_inspect_task_reports_every_bad_command():
    task = {
        "test_commands": [SPEC_ECHO, "pytest -q"],
        "test_command": PROBE_CHAIN,
    }
    codes = sorted({issue.code for _, issue in inspect_task(task)})
    assert codes == ["no_execution", "probe_chain"]


# ---------------------------------------------------------------------------
# ``find_unreachable_targets``: the complement of RED
# ---------------------------------------------------------------------------
#
# RED asks "does this fail before the work?". A command that fails
# *forever* answers yes just as well — the check passes while the task is
# permanently broken.


def test_a_doubled_cd_prefix_is_flagged():
    """Task 19/20's shape: the generator duplicated the dir to make the
    path miss, which it does — permanently."""
    task = {
        "test_commands": [
            "cd frontend-app && npx vitest run "
            "frontend-app/tests/unit/x.spec.ts"
        ],
    }
    issues = find_unreachable_targets(task)
    assert [i.code for _, i in issues] == ["doubled_cd_prefix"]


def test_a_correct_cd_relative_path_is_not_flagged():
    task = {
        "test_commands": [
            "cd frontend-app && npx vitest run tests/unit/x.spec.ts"
        ],
    }
    assert find_unreachable_targets(task) == []


def test_an_absolute_cd_is_not_checked():
    """``cd /abs/path`` cannot be doubled by a relative argument."""
    task = {
        "test_commands": [
            "cd /Users/u/proj/frontend && npx vitest run tests/x.spec.ts"
        ],
    }
    assert find_unreachable_targets(task) == []


def test_a_truncated_declared_filename_is_flagged():
    """Task 21's shape."""
    task = {
        "files_to_modify": [
            "frontend-app/tests/e2e/x.spec.ts",
            "tests/e2e/signal-type-.py",
        ],
        "test_commands": ["pytest tests/e2e/x.spec.ts"],
    }
    codes = [i.code for _, i in find_unreachable_targets(task)]
    assert codes == ["malformed_declared_path"]


def test_a_dot_variant_duplicate_is_flagged():
    """Task 22's shape: ``github/…`` next to ``.github/…``."""
    task = {
        "files_to_modify": [
            ".github/workflows/pr-gate.yml",
            "tests/ci/test_pr_gate_lint_gate.py",
            "github/workflows/pr-gate.yml",
        ],
    }
    codes = [i.code for _, i in find_unreachable_targets(task)]
    assert codes == ["duplicate_declared_path"]


def test_a_clean_task_is_not_flagged():
    task = {
        "files_to_modify": [
            "native_ext/src/types.rs",
            "native_ext/tests/signal_kind_enum_test.rs",
        ],
        "test_commands": [
            "cargo test -p native_ext --no-default-features "
            "--test signal_kind_enum_test"
        ],
    }
    assert find_unreachable_targets(task) == []


# ---------------------------------------------------------------------------
# ``subproject_outside_cwd`` (2026-09-21)
# ---------------------------------------------------------------------------
#
# Tasks 17-19 of the regeneration ran `npx vitest run
# frontend-app/lib/signals/x.spec.ts` with no `cd`. Measured: from the
# repo root vitest uses a different config (`include: tests/**`) and
# reports `No test files found`, exit 1 — the same before and after the
# task, so the task could never complete.
#
# The marker set has to be *per runner family*. `tests/e2e/` in the same
# repo carries a `package.json` for its Playwright setup, but a pytest
# command there is unaffected by it — a single marker list flagged those
# as unreachable, which would have had the rewriting agent "fix" working
# commands.


def _project_with_node_subproject(tmp_path):
    (tmp_path / "frontend-app" / "lib" / "signals").mkdir(parents=True)
    (tmp_path / "frontend-app" / "package.json").write_text("{}")
    (tmp_path / "frontend-app" / "vitest.config.ts").write_text("//")
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    # Node setup that must NOT constrain a pytest command.
    (tmp_path / "tests" / "e2e" / "package.json").write_text("{}")
    return tmp_path


def test_a_node_runner_outside_the_subproject_is_flagged(tmp_path):
    project = _project_with_node_subproject(tmp_path)
    task = {
        "project_dir": str(project),
        "test_commands": [
            "npx vitest run frontend-app/lib/signals/x.spec.ts"
        ],
    }
    codes = [i.code for _, i in find_unreachable_targets(task)]
    assert codes == ["subproject_outside_cwd"]


def test_cding_into_the_subproject_clears_it(tmp_path):
    project = _project_with_node_subproject(tmp_path)
    task = {
        "project_dir": str(project),
        "test_commands": [
            "cd frontend-app && npx vitest run lib/signals/x.spec.ts"
        ],
    }
    assert find_unreachable_targets(task) == []


def test_pytest_is_not_constrained_by_a_package_json(tmp_path):
    """The false positive that made the marker set runner-specific."""
    project = _project_with_node_subproject(tmp_path)
    task = {
        "project_dir": str(project),
        "test_commands": [
            "venv1/bin/pytest tests/e2e/test_frontend_pure_render_scan.py -v"
        ],
    }
    assert find_unreachable_targets(task) == []


def test_no_project_dir_means_no_guess(tmp_path):
    """Without a filesystem root this check stays silent."""
    task = {
        "test_commands": [
            "npx vitest run frontend-app/lib/signals/x.spec.ts"
        ],
    }
    assert find_unreachable_targets(task) == []


# ---------------------------------------------------------------------------
# ``exit_code_swallowed_by_semicolon`` (2026-09-21)
# ---------------------------------------------------------------------------
#
# The tasks self-review caught this one by *reading* the command while the
# shape checks saw nothing:
#
#   cd native_ext && cargo test --test x ; venv1/bin/pytest tests/y.py -v
#
# exits 0 whenever pytest passes, however cargo did — the same family as a
# pipeline ending in ``tee``, with a different operator.


def test_a_semicolon_chain_is_flagged():
    cmd = (
        "cd native_ext && cargo test --test x ; "
        "venv1/bin/pytest tests/y.py -v"
    )
    assert find_semicolon_exit_issue(cmd) is not None
    assert [i.code for i in inspect_command(cmd)] == [
        "exit_code_swallowed_by_semicolon"
    ]


def test_the_capture_and_reraise_idiom_is_not_flagged():
    """``cmd > log 2>&1; rc=$?; …; exit $rc`` propagates the status on
    purpose — repair commands legitimately take this shape, and
    flagging them would have the rewriting agent break working commands."""
    cmd = (
        "cd /x && cargo test --test y > /tmp/l.log 2>&1; rc=$?; "
        "tail -20 /tmp/l.log; echo \"EXIT_CODE=$rc\"; exit $rc"
    )
    assert find_semicolon_exit_issue(cmd) is None
    assert inspect_command(cmd) == []


def test_a_trailing_semicolon_is_not_a_chain():
    assert find_semicolon_exit_issue("cd x && cargo test --test y ;") is None


def test_a_semicolon_inside_quotes_is_not_a_chain():
    assert find_semicolon_exit_issue(
        'python3 -c "import x; print(1)"'
    ) is None


# ---------------------------------------------------------------------------
# Chain splitting
# ---------------------------------------------------------------------------


def test_split_ignores_separators_inside_quotes():
    """A naive ``split("&&")`` would invent extra segments."""
    segments = _split_top_level_chain('python -c "a && b" && pytest')
    assert segments == ['python -c "a && b"', "pytest"]


def test_split_handles_empty_segments():
    assert _split_top_level_chain("pytest && && ls") == ["pytest", "ls"]


def test_quoted_chain_does_not_trigger_a_false_positive():
    """The embedded ``&&`` must not push the count over the threshold."""
    cmd = 'python3 -c "import os; os.path.exists(\'a\') and print(1)" && echo done'
    assert inspect_command(cmd) == []


# ---------------------------------------------------------------------------
# Corpus regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # Non-empty test_commands from an earlier plan's plan_tasks that
        # run something real. Flagging any of these is a false positive.
        #
        # 2026-09-20: this list previously carried a `python -c` case
        # that read a JSON file, under the assumption that the whole
        # family was legitimately runnable. Measurement against the live
        # corpus showed the opposite — such commands exit
        # 0 before any repair runs, so they certify nothing. The shape
        # is now caught by `no_execution` (see SPEC_ECHO above); what
        # remains here is the part of the corpus that does real work.
        "cd /Users/u/p && source venv1/bin/activate && timeout 600 "
        "pytest tests/serialization/test_signal_type_passthrough.py",
        "cd /Users/u/p/native_ext && timeout 900 cargo test --test "
        "signal_classification -- --test-threads=1 --nocapture",
        "cd /Users/u/p/native_ext && timeout 600 cargo test --test "
        "signal_classification test_consolidation_entering",
        "cd /tmp && DEVELOPER_DIR=/Library/Developer/CommandLineTools "
        "timeout 600 cargo test --manifest-path /Users/u/p/native_ext/Cargo.toml",
        'cd "$HOME/work/dev-checkout" && '
        "python3 tools/verify_vp_rust_case.py --vp VP-006 --apply",
        'cd "$HOME/work/dev-checkout" && '
        "python3 tools/verify_vp016_marker.py --report-only",
    ],
)
def test_real_corpus_commands_are_not_flagged(command):
    assert inspect_command(command) == [], (
        "false positive on a real, runnable command from an earlier plan"
    )


def test_the_spec_echo_family_is_flagged():
    """Pin the shape that produces vacuous completion verdicts.

    The family is recognisable from the command alone: a ``python -c``
    program that compares a string in ``verification_plan.json`` against
    ``sys.argv[3]`` and exits on the comparison. Its exit code is a
    function of the plan file's contents and of nothing else, so it can
    never distinguish "the fix worked" from "nothing was changed".
    """
    assert [i.code for i in inspect_command(SPEC_ECHO)] == ["no_execution"]
