"""The changelog gate: a judgment-surface change has to carry an entry.

Why this file exists
--------------------
The material (§7) makes the changelog the place where a change in
*judgment semantics* is written down, and it makes that place a
mechanical one: a change under a protected path — a path whose
contents decide what a verdict says — has to bring a ``[Unreleased]``
entry with it, that entry has to carry the ``### 判定语义变更``
section, and it has to name at least one of the files the change
actually touched.

The accident that started the work (§1) was a 137-line change to the
judgment semantics that landed inside an unrelated pull request: the
changelog said nothing about it, and the long-running process was
still holding the old module in memory, so four rounds judged by the
old rule.  A rule nobody can see is not a rule.

What the gate can and cannot decide
------------------------------------
The gate reads **text** and **the diff**.  It can say "you touched the
judgment surface and wrote nothing" and it can say "you wrote
something that names a file you did not touch".  It cannot say
whether the entry is *complete* — that is a review job, and §7 says so
too.  Every case below therefore pins a *shape*, never a judgement
about the prose.

The negative controls
---------------------
Five of the six cases are failures, and each of them is a way the
gate could be green while doing nothing: a change that touches
nothing protected, an entry that names an unrelated file, an entry
without the section, a pair of changelogs that disagree.  A gate that
reported nothing would pass all of them as happily as it passes the
compliant case — and a gate that cannot go red is not a gate.

The fixture
-----------
Each case builds a throwaway git repository: a base commit, a
``base`` branch pointing at it, and then the change under test.  The
gate is run against that repository through its own ``main()``, so
what is exercised is the diff-reading, not a hand-fed list of paths.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

#: The repository root — three parents up from ``backend/tests/unit/``.
_REPO_ROOT = Path(__file__).resolve().parents[3]
#: The gate script under test.
_SCRIPT = _REPO_ROOT / "scripts" / "ci_changelog_gate.py"

#: The script is loaded by path because ``scripts/`` is not a package on
#: ``sys.path``.  The registered name is unique to this file, so
#: nothing else can collide with it.
_MODULE_NAME = "ci_changelog_gate_under_test"

_spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPT)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
sys.modules[_MODULE_NAME] = gate
_spec.loader.exec_module(gate)


# ---------------------------------------------------------------------------
# The fixture: a throwaway repository with a base commit
# ---------------------------------------------------------------------------

#: The English changelog as it looks *before* the change under test.
#: It carries an ``[Unreleased]`` block and no judgment section, which
#: is what a repository that has never had one looks like.
_BASE_EN = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- Nothing yet.

## [0.1.0] - 2026-01-01

- The first release.
"""

#: The Chinese changelog, same shape.
_BASE_ZH = """# 变更日志

本项目所有值得注意的变更，都记录在这里。

## [Unreleased]

### 新增

- 暂无。

## [0.1.0] - 2026-01-01

- 首个版本。
"""

#: The judgment file each case touches: it is under the protected
#: surface (``backend/verification_*.py``), so a change to it is a
#: change to what a verdict says.
_JUDGMENT_FILE = "backend/verification_evidence.py"


def _git(repo: Path, *args: str) -> str:
    """Run git inside ``repo`` and return stdout; fail the test on error."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A git repository holding one base commit and a ``base`` branch."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    # The identity is set on the repository itself: the suite runs with
    # ``$HOME`` redirected, so the operator's global config is not there
    # to answer for it.
    _git(root, "config", "user.email", "gate@example.invalid")
    _git(root, "config", "user.name", "gate")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "CHANGELOG.md").write_text(_BASE_EN, encoding="utf-8")
    (root / "CHANGELOG.zh.md").write_text(_BASE_ZH, encoding="utf-8")
    (root / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "branch", "base")
    return root


def _touch_judgment_file(repo: Path, name: str = _JUDGMENT_FILE) -> None:
    """Write a file under the protected judgment surface."""
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# a judgment function\n", encoding="utf-8")


def _with_entry(base: str, addition: str) -> str:
    """The changelog ``base`` with ``addition`` put inside ``[Unreleased]``."""
    marker = "\n## [0.1.0]"
    index = base.index(marker)
    return base[:index] + "\n" + addition.rstrip("\n") + "\n" + base[index:]


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _run(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    """Run the gate against ``repo`` and return its exit code and output."""
    code = gate.main(["--base", "base", "--root", str(repo)])
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# The six cases
# ---------------------------------------------------------------------------


def test_no_protected_change_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A change that touches nothing protected needs no entry.

    This is the case that keeps the gate from being noise: the great
    majority of commits touch no judgment file, and a gate that
    demanded a changelog entry from all of them would be switched off
    within a week.
    """
    (repo / "README.md").write_text("# fixture, edited\n", encoding="utf-8")
    _commit(repo, "docs: edit the readme")

    code, out = _run(repo, capsys)

    assert code == 0, out
    assert "CHANGELOG GATE: PASS" in out


def test_protected_change_without_entry_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A judgment-surface change with no changelog entry is refused.

    The failure has to *say* that the entry is missing — a gate that
    exits 1 without naming the reason sends the reader to the diff to
    find out what the gate wanted.
    """
    _touch_judgment_file(repo)
    _commit(repo, "feat: change a judgment")

    code, out = _run(repo, capsys)

    assert code == 1, out
    assert "CHANGELOG GATE: FAIL" in out
    assert "判定面改动缺少 [Unreleased] 条目" in out


def test_entry_naming_unrelated_file_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An entry that names a file the change did not touch is refused.

    This is the copy-paste case (§7 row 2): the entry is there, the
    section is there, and it says nothing about what was changed.  A
    gate that only counted sections would pass it.
    """
    _touch_judgment_file(repo)
    (repo / "CHANGELOG.md").write_text(
        _with_entry(
            _BASE_EN,
            "### 判定语义变更\n\n"
            f"- `backend/verification_unrelated.py`：判定面改动。",
        ),
        encoding="utf-8",
    )
    (repo / "CHANGELOG.zh.md").write_text(
        _with_entry(
            _BASE_ZH,
            "### 判定语义变更\n\n"
            f"- `backend/verification_unrelated.py`：判定面改动。",
        ),
        encoding="utf-8",
    )
    _commit(repo, "feat: change a judgment")

    code, out = _run(repo, capsys)

    assert code == 1, out
    assert "条目未点名实际改动的判定文件" in out
    assert _JUDGMENT_FILE in out


def test_missing_judgment_section_fails(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An entry without the ``### 判定语义变更`` section is refused.

    New lines are there — the check that fails is the section itself.
    That is the third row of §7: the section has to exist whether or
    not there is anything to put in it, so that "somebody else's
    judgment changed" is a thing the reader can find.
    """
    _touch_judgment_file(repo)
    (repo / "CHANGELOG.md").write_text(
        _with_entry(_BASE_EN, "### Changed\n\n- Something changed."),
        encoding="utf-8",
    )
    (repo / "CHANGELOG.zh.md").write_text(
        _with_entry(_BASE_ZH, "### 变更\n\n- 改了点东西。"),
        encoding="utf-8",
    )
    _commit(repo, "feat: change a judgment")

    code, out = _run(repo, capsys)

    assert code == 1, out
    assert "### 判定语义变更" in out
    assert "缺少" in out


def test_compliant_change_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The compliant change: a protected file touched, an entry that says so.

    The entry carries the section and names the file that was changed,
    in both languages.  This is the case the gate exists to allow.
    """
    _touch_judgment_file(repo)
    (repo / "CHANGELOG.md").write_text(
        _with_entry(
            _BASE_EN,
            "### 判定语义变更\n\n"
            f"- `{_JUDGMENT_FILE}`：判定面改动。",
        ),
        encoding="utf-8",
    )
    (repo / "CHANGELOG.zh.md").write_text(
        _with_entry(
            _BASE_ZH,
            "### 判定语义变更\n\n"
            f"- `{_JUDGMENT_FILE}`：判定面改动。",
        ),
        encoding="utf-8",
    )
    _commit(repo, "feat: change a judgment")

    code, out = _run(repo, capsys)

    assert code == 0, out
    assert "CHANGELOG GATE: PASS" in out


def test_bilingual_mismatch_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two changelogs that do not say the same thing are refused.

    The English changelog names the file that changed; the Chinese one
    carries the section and names nothing.  A reader of either language
    has to learn about a judgment change the other language does not
    mention (§12.3) — and the two files disagreeing is a stronger
    signal than either of them being wrong on its own.
    """
    _touch_judgment_file(repo)
    (repo / "CHANGELOG.md").write_text(
        _with_entry(
            _BASE_EN,
            "### 判定语义变更\n\n"
            f"- `{_JUDGMENT_FILE}`：判定面改动。",
        ),
        encoding="utf-8",
    )
    (repo / "CHANGELOG.zh.md").write_text(
        _with_entry(
            _BASE_ZH,
            "### 判定语义变更\n\n"
            "- 本次未改动判定面。",
        ),
        encoding="utf-8",
    )
    _commit(repo, "feat: change a judgment")

    code, out = _run(repo, capsys)

    assert code == 1, out
    assert "中英不同步" in out
