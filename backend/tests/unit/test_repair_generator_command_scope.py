"""The generation side keeps ``test_command`` and ``files_to_modify`` in step.

A repair task carries two things that have to agree: the command that
has to exit 0 for the repair to count, and the list of files the task is
allowed to change.  They come from two different places — the command
from the verification point or from the model, the file list from the
model's own hint — and until now nothing compared them.

The incident this rule answers is a repair task whose command ran a test
under ``tests/`` while its declaration named a single docs file.  The
completion gate compares the files git reports as changed against the
declaration (``agent.excess_declared_paths``), so a repair that touched
the file the command names was judged an *undeclared write* and the task
failed for the wrong reason.

The two sides are put in step by **merging** the paths the command names
into the declaration (material R2, option 1).  The command is the
objective signal — it is what proves the repair worked — so it is left
exactly as it is, and the declaration grows to cover what the command
needs.

The rules that read the paths out of a command live in
``backend/test_command_rules.py``: ``paths_named_by_command()`` (task
4-1) and its read-only allow-list (task 4-2).  The entry script a
command merely *runs* is read-only and is therefore never declared, and a
command that names no source file names no repo file.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
for _path in (str(REPO_ROOT), str(BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from backend.repair_generator import RepairTaskAssembler  # noqa: E402


def _repo(tmp_path: Path) -> Path:
    """A repository holding the files the commands below name.

    The rule under test reads a command, it does not run one, so the
    files do not have to exist — but a real tree keeps the test honest
    about what it claims: the command names a file that is really there.
    """
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "a.zh.md").write_text("# 文档\n", encoding="utf-8")
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "unit" / "test_b.py").write_text(
        "def test_b():\n    assert True\n", encoding="utf-8"
    )
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "run_tests.sh").write_text(
        "#!/bin/sh\nexit 0\n", encoding="utf-8"
    )
    return repo


def _assembler(project_dir=None) -> RepairTaskAssembler:
    """An assembler with one failed VP and no command of its own.

    ``project_dir`` is the assembler's fallback workspace — the same
    field the production chain fills from the plan's execution row.
    """
    return RepairTaskAssembler(
        round_number=1,
        failed_vps=[
            {"id": "VP-1", "title": "VP-1", "priority": "medium"},
        ],
        vp_test_commands={},
        project_dir=project_dir,
    )


def _content(repo: Path, **overrides) -> dict:
    """One repair-content row, as the generator hands it over."""
    content = {
        "failed_vp_id": "VP-1",
        "title": "Fix the gate",
        "description": "d",
        "files_to_modify": ["docs/a.zh.md"],
        "test_command": "cd backend && pytest tests/unit/test_b.py",
        "project_dir": str(repo),
    }
    content.update(overrides)
    return content


# ---------------------------------------------------------------------------
# The command's paths are declared
# ---------------------------------------------------------------------------


def test_a_command_path_outside_the_declaration_is_declared(tmp_path, capsys):
    """The file the acceptance command names is part of the declaration.

    This is the incident: the command runs a test under ``tests/`` and
    the declaration named a docs file only.  A repair that touched the
    test file was an undeclared write, so the task failed for the wrong
    reason.  The declaration now covers what the command needs.
    """
    repo = _repo(tmp_path)
    assembler = _assembler(project_dir=str(repo))

    tasks = assembler.assemble([_content(repo)])

    assert len(tasks) == 1
    assert tasks[0]["files_to_modify"] == [
        "docs/a.zh.md",
        "tests/unit/test_b.py",
    ]
    # The command is the objective signal and is left exactly as it was.
    assert tasks[0]["test_command"] == "cd backend && pytest tests/unit/test_b.py"
    # The widening is recorded, not silent.
    out = capsys.readouterr().out
    assert "Declaring the paths the acceptance command needs" in out
    assert "tests/unit/test_b.py" in out


def test_an_already_declared_path_is_not_duplicated(tmp_path):
    """A path the declaration already carries is not added twice."""
    repo = _repo(tmp_path)
    assembler = _assembler(project_dir=str(repo))

    tasks = assembler.assemble([
        _content(repo, files_to_modify=[
            "docs/a.zh.md",
            "tests/unit/test_b.py",
        ]),
    ])

    assert len(tasks) == 1
    assert tasks[0]["files_to_modify"] == [
        "docs/a.zh.md",
        "tests/unit/test_b.py",
    ]


def test_a_declared_path_written_with_a_dot_slash_is_not_duplicated(tmp_path):
    """``./x.py`` and ``x.py`` are the same path.

    The comparison normalises the leading ``./``; the declaration itself
    is left as it was written, because rewriting it is not this rule's
    business.
    """
    repo = _repo(tmp_path)
    assembler = _assembler(project_dir=str(repo))

    tasks = assembler.assemble([
        _content(repo, files_to_modify=["./tests/unit/test_b.py"]),
    ])

    assert len(tasks) == 1
    assert tasks[0]["files_to_modify"] == ["./tests/unit/test_b.py"]


# ---------------------------------------------------------------------------
# The rule does not fire where it should not
# ---------------------------------------------------------------------------


def test_whitelisted_read_only_command_leaves_both_untouched(tmp_path):
    """A command that only runs a read-only entry script declares nothing.

    ``scripts/run_tests.sh`` is the script the command *runs*, not a file
    the task has to change, so the read-only allow-list drops it and the
    declaration is left exactly as it was.
    """
    repo = _repo(tmp_path)
    assembler = _assembler(project_dir=str(repo))

    tasks = assembler.assemble([
        _content(repo, test_command="bash scripts/run_tests.sh"),
    ])

    assert len(tasks) == 1
    assert tasks[0]["files_to_modify"] == ["docs/a.zh.md"]
    assert tasks[0]["test_command"] == "bash scripts/run_tests.sh"


def test_a_task_without_a_command_keeps_its_declaration(tmp_path):
    """No command names no path, so the declaration is untouched."""
    repo = _repo(tmp_path)
    assembler = _assembler(project_dir=str(repo))

    tasks = assembler.assemble([_content(repo, test_command="")])

    assert len(tasks) == 1
    assert tasks[0]["files_to_modify"] == ["docs/a.zh.md"]
    assert tasks[0]["test_command"] == ""


def test_no_workspace_means_no_reconciliation(tmp_path):
    """Without a workspace there is no repository to reconcile against.

    The plan's execution row is what says where the repair runs; a row
    that knows nothing about it leaves the declaration alone rather than
    guessing a scope.
    """
    repo = _repo(tmp_path)
    assembler = _assembler()
    content = _content(repo)
    content.pop("project_dir")

    tasks = assembler.assemble([content])

    assert len(tasks) == 1
    assert tasks[0]["files_to_modify"] == ["docs/a.zh.md"]
    assert tasks[0]["test_command"] == "cd backend && pytest tests/unit/test_b.py"


# ---------------------------------------------------------------------------
# The declaration's own shape
# ---------------------------------------------------------------------------


def test_a_bare_string_declaration_is_normalised_before_the_merge(tmp_path):
    """A declaration that is one string is one entry, not a sequence.

    The merge compares the declaration against the command's paths, so
    the declaration has to be in list shape before the comparison — a
    bare string would otherwise be compared character by character.
    """
    repo = _repo(tmp_path)
    assembler = _assembler(project_dir=str(repo))

    tasks = assembler.assemble([
        _content(repo, files_to_modify="docs/a.zh.md"),
    ])

    assert len(tasks) == 1
    assert tasks[0]["files_to_modify"] == [
        "docs/a.zh.md",
        "tests/unit/test_b.py",
    ]
