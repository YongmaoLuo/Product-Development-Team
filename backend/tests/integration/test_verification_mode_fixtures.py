"""The fixtures an end-to-end verification run is handed — and the rule that
every one of them is handed back.

What this pins
--------------
A verification run is only reproducible if the things it depends on are
pinned, and the things it starts are given back.  Four properties, each a
way to lose a run quietly rather than a way to fail loudly:

  1. **The upstream repository is reproducible.**  Two constructions of the
     fixture repository produce the *same* commit SHA, because the author
     identity and both commit dates are fixed.  A verdict compared with
     another verdict is worth nothing if the two runs verified different
     commits.
  2. **The workspace never touches the real home.**  The workspace root is
     forced into the test's own tmp tree, and ``$HOME`` is redirected with
     it, so ``~/.pdt/verification-workspaces`` — the operator's own tree —
     is not a place a test can reach, not even by accident when the
     override is missing.
  3. **A leftover is a failure.**  A worktree a test started and never gave
     back is detected by name, so the suite's teardown turns it into a
     failed test rather than a directory tree nobody cleans up.
  4. **The server comes up and goes away.**  A real FastAPI instance on a
     kernel-assigned port, and the port is free again once the fixture is
     done with it.

Everything here is local: the upstream repository is a directory, the LLM
answers from files, the server binds to loopback.  No test in this module
reaches the network.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from tests.fixtures.verification_mode import (  # noqa: E402
    PLAN_ID,
    PR_REF,
    UpstreamRepo,
    build_upstream_repo,
    detect_leftover_resources,
    reclaim_and_verify,
    remove_worktree,
)
from verification_workspace import WORKSPACE_ROOT_ENV  # noqa: E402
from verification_workspace import workspace_root as resolve_workspace_root  # noqa: E402


def _materialise_leftover(upstream_repo: UpstreamRepo, workspace_root: Path) -> Path:
    """Materialise a worktree the fixture set does not know about.

    It is created the way production creates one — through
    :func:`verification_workspace.ensure_workspace` — but it is never
    registered, so the reclamation has no reason to remove it.  That is
    exactly the shape of a test that starts something and forgets about it.
    """
    from verification_workspace import Baseline, ensure_workspace

    baseline = Baseline(
        repo_dir=upstream_repo.repo,
        ref=upstream_repo.pr_ref,
        commit_sha=upstream_repo.head_sha(),
        pr_number=upstream_repo.pr_number,
    )
    return ensure_workspace("leftover-plan", baseline).path


def test_upstream_repo_sha_reproducible(tmp_path: Path, upstream_repo: UpstreamRepo):
    """Two constructions of the upstream repository yield the same commit SHA.

    The dates and the identity are pinned, so the SHA is a function of the
    *content* and not of the moment the fixture ran.  Without that, every
    run would verify a different commit and no two verdicts could be
    compared.
    """
    second = build_upstream_repo(tmp_path / "second")

    assert second.repo != upstream_repo.repo, "the two repositories must be separate trees"
    assert second.head_sha() == upstream_repo.head_sha()
    assert second.base_sha() == upstream_repo.base_sha()
    assert re.fullmatch(r"[0-9a-f]{40}", upstream_repo.head_sha())
    # The pull request head is the second commit, and the baseline is its
    # ancestor — the two are not the same commit.
    assert upstream_repo.head_sha() != upstream_repo.base_sha()
    assert upstream_repo.pr_ref == PR_REF


def test_workspace_root_never_real_home(
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Without the override the root is still a tmp path — never the real ``~/.pdt``.

    The override is the first line of defence; the redirected ``$HOME`` is
    the second, and it is the one that holds when the override is missing.
    """
    monkeypatch.delenv(WORKSPACE_ROOT_ENV, raising=False)

    root = resolve_workspace_root()

    assert root.is_absolute()
    assert tmp_path in root.parents, (
        f"the workspace root {root} is not inside the test's own tmp tree"
    )
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    if real_home:
        assert not str(root).startswith(str(Path(real_home) / ".pdt")), (
            f"the workspace root {root} lands in the operator's real home"
        )


def test_cleanup_detects_leftover_worktree(
    upstream_repo: UpstreamRepo,
    workspace_root: Path,
    verification_plan,
):
    """A worktree left behind is detected, by name, and fails the cleanup check.

    The check the suite's teardown runs is the one exercised here: the
    reclamation removes what was registered, and whatever is still on disk
    afterwards is a leftover — the thing that must fail the test.
    """
    leftover = _materialise_leftover(upstream_repo, workspace_root)

    problems = detect_leftover_resources()
    assert any(str(leftover) in problem for problem in problems), (
        f"the leftover {leftover} was not detected; problems={problems}"
    )

    with pytest.raises(AssertionError) as excinfo:
        reclaim_and_verify()
    assert str(leftover) in str(excinfo.value)

    # Hand it back, so this test itself ends with nothing left behind.
    remove_worktree(upstream_repo.repo, leftover)
    assert not detect_leftover_resources()


def test_running_server_on_ephemeral_port(running_server):
    """The server comes up on a kernel-assigned port, and the port is freed again.

    The port is not one the suite chose: it is the one the kernel handed
    out, which is what keeps two concurrent runs from fighting over 8001.
    """
    assert running_server.port > 0
    assert running_server.alive()
    assert running_server.base_url == f"http://127.0.0.1:{running_server.port}"

    status = running_server.get("/health")
    assert 200 <= status < 300, f"/health answered {status}"

    # The reclamation: the server is stopped and the port is free again.
    running_server.stop()
    assert running_server.port_released()
    assert not running_server.alive()
