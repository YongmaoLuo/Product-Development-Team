"""Whose runner executes the gate? (2026-11-04, VP-012 round 4)

The round-4 failure of ``VP-012`` was **not** a new defect: the machine
fact was byte-for-byte the same as in round 3 (the schema rejected
``ci_entry`` with the very same detail), and only the prose differed —
which is all the ``legacy-v2`` fingerprint hashed.  So the round read
one defect as two.

Why the same defect came back at all: the round ran a runner that was
**not the project's**.  ``backend/__init__.py`` puts the *server's* own
``backend/`` at ``sys.path[0]``, so the flat import
``from verification_ci_runner import run_ci_verification`` resolved to
the checkout the long-running server was started in — not to the tree
under test.  The fix that had landed in the tree under test was never
executed.

These tests pin the four properties that make the tree under test the
one that runs:

* **A1** the project's own runner is loaded **from its path**, and the
  harness' ``verification_ci_runner`` entry in ``sys.modules`` is left
  alone (swapping it would swap the harness' own machinery);
* **A2** a project tree that ships no runner — or one that cannot be
  loaded — falls back to the harness' copy **without raising**;
* **A3** the digest-named module does not stay in ``sys.modules``;
* **A6** a failure always leaves a **machine-written** identity, so the
  fingerprint stops hashing prose (``identity-v1`` instead of
  ``legacy-v2``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _project_tree(tmp_path: Path, runner_source: str) -> Path:
    """A project tree that ships its own ``backend/verification_ci_runner.py``."""
    backend = tmp_path / "backend"
    backend.mkdir(parents=True, exist_ok=True)
    (backend / "verification_ci_runner.py").write_text(
        runner_source, encoding="utf-8",
    )
    return tmp_path


#: A project runner that offers the entry points the harness looks for.
_STUB_RUNNER = '''
"""A project-tree copy of the runner (stub)."""


class SchemaIssue:
    def __init__(self, vp_id, detail):
        self.vp_id = vp_id
        self.detail = detail

    def to_dict(self):
        return {"vp_id": self.vp_id, "detail": self.detail}


def validate_vp(vp, *, project_dir=None):
    issues = []
    if project_dir is not None:
        issues.append(
            SchemaIssue(str(vp.get("id")), "project runner saw project_dir")
        )
    return issues


def run_ci_verification(vp, *, project_dir=None, artifact_dir=None):
    return {"status": "PASSED", "reasons": [], "evidence": {"from": "project"}}
'''


# ---------------------------------------------------------------------------
# A1 / A2 / A3 — the loader
# ---------------------------------------------------------------------------


class TestProjectRunnerLoader:
    def test_project_runner_is_loaded_from_its_path(self, tmp_path):
        """A1: the module comes from the tree under test."""
        from verification_agent import _load_ci_runner

        project = _project_tree(tmp_path, _STUB_RUNNER)

        module = _load_ci_runner(project)

        assert Path(module.__file__).resolve() == (
            project / "backend" / "verification_ci_runner.py"
        ).resolve()

    def test_harness_module_is_not_shadowed(self, tmp_path):
        """A1: ``sys.modules['verification_ci_runner']`` stays the harness' one."""
        import verification_ci_runner

        from verification_agent import _load_ci_runner

        project = _project_tree(tmp_path, _STUB_RUNNER)
        harness_file = Path(verification_ci_runner.__file__).resolve()

        _load_ci_runner(project)

        import verification_ci_runner as after

        assert Path(after.__file__).resolve() == harness_file

    def test_missing_project_runner_falls_back(self, tmp_path):
        """A2: a project that ships no runner falls back — and does not raise."""
        import verification_ci_runner

        from verification_agent import _load_ci_runner

        module = _load_ci_runner(tmp_path)

        assert Path(module.__file__).resolve() == Path(
            verification_ci_runner.__file__,
        ).resolve()

    def test_broken_project_runner_falls_back(self, tmp_path):
        """A2: a project runner that cannot be loaded falls back — no raise."""
        import verification_ci_runner

        from verification_agent import _load_ci_runner

        project = _project_tree(tmp_path, "this is not python (((\n")

        module = _load_ci_runner(project)

        assert Path(module.__file__).resolve() == Path(
            verification_ci_runner.__file__,
        ).resolve()

    def test_digest_name_does_not_stay_in_sys_modules(self, tmp_path):
        """A3: the digest-named module is popped again after loading."""
        from verification_agent import _load_ci_runner

        project = _project_tree(tmp_path, _STUB_RUNNER)

        _load_ci_runner(project)

        leftovers = [
            name for name in sys.modules
            if name.startswith("pdt_project_ci_runner_")
        ]
        assert leftovers == []


# ---------------------------------------------------------------------------
# A6 — the machine-written failure identity
# ---------------------------------------------------------------------------


class TestFailureIdentity:
    def test_schema_rejection_writes_a_machine_identity(self, tmp_path):
        """A failed gate always leaves a machine-written identity behind."""
        from verification_ci_runner import run_ci_verification

        artifact = tmp_path / "vp_artifacts"
        vp = {
            "id": "VP-012",
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/does_not_exist.sh",
        }

        result = run_ci_verification(
            vp, project_dir=tmp_path, artifact_dir=artifact,
        )

        assert result.status == "FAILED"
        identity = result.evidence.get("failure_identity")
        assert identity, "a failed gate must leave a machine-written identity"
        # The identity is the schema's own words — not prose.
        assert identity == [
            issue["detail"] for issue in result.evidence["schema_issues"]
        ]

        payload = json.loads(
            (artifact / "VP-012" / "ci_output.json").read_text(encoding="utf-8"),
        )
        assert payload["failure_identity"] == identity

    def test_artifact_is_written_even_when_the_schema_rejects(self, tmp_path):
        """The schema short-circuit used to return before the artifact."""
        from verification_ci_runner import run_ci_verification

        artifact = tmp_path / "vp_artifacts"
        vp = {"id": "VP-013", "verification_method": "full_ci"}

        run_ci_verification(vp, project_dir=tmp_path, artifact_dir=artifact)

        assert (artifact / "VP-013" / "ci_output.json").is_file()

    def test_artifact_identity_reaches_the_fingerprint(self, tmp_path):
        """A6: the orchestrator reads the identity out of the artifact, and
        the fingerprint rule switches to ``identity-v1``."""
        from verification.failure_fingerprint import fingerprint_rule
        from verification.orchestrator import VerificationOrchestrator
        from verification_ci_runner import run_ci_verification

        artifact = tmp_path / "vp_artifacts"
        vp = {
            "id": "VP-014",
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/does_not_exist.sh",
        }
        run_ci_verification(vp, project_dir=tmp_path, artifact_dir=artifact)

        class _Stub:
            plan_dir = str(tmp_path)

        ids = VerificationOrchestrator._artifact_failed_case_ids(
            _Stub(), "VP-014",
        )
        assert ids, "the artifact's identity must reach the orchestrator"
        assert fingerprint_rule(failed_cases=ids) == "identity-v1"

    def test_prose_is_not_hashed_anymore(self, tmp_path):
        """The same machine fact fingerprints the same, however the prose
        differs — the run's wording is **not** part of the failure's
        identity.

        The identity rule (``identity-v1``) hashes ``vp_id`` /
        ``failure_class`` / the failing case set and leaves the prose out
        on purpose: the prose measures the *run* (how long it took, how
        busy the machine was), not the defect.  Two rounds that hit the
        same defect therefore carry the same fingerprint, so a repeated
        defect is not read as a new one.

        The downstream decision that used to consume this — the
        root-cause chain — was removed on purpose (``refactor/
        remove-root-cause-chain``); the contract it relied on still
        stands, so it is asserted here directly.
        """
        from verification.failure_fingerprint import (
            FINGERPRINT_RULE_IDENTITY,
            failure_fingerprint,
            fingerprint_rule,
        )

        detail = "ci_entry 引用的路径不存在：backend/.venv)"
        fingerprints = []
        for prose in ("第一轮的说法", "第二轮的说法，第三轮又换了"):
            fingerprints.append(failure_fingerprint(
                vp_id="VP-012",
                failure_class="deliverable_defect",
                failed_assertions=None,
                command=None,
                first_error_line=prose,
                failed_cases=[detail],
            ))

        assert fingerprints[0] == fingerprints[1]
        assert fingerprint_rule(
            failed_cases=[detail],
        ) == FINGERPRINT_RULE_IDENTITY

    def test_a_missing_path_still_goes_down_the_deterministic_path(
        self, tmp_path,
    ):
        """A ``ci_entry`` naming a path that is not in the tree is a
        **judgeable** failure: the gate runs and reports a machine-written
        verdict.  Sending it to the LLM path instead would replace that
        verdict with a sentence — and the next round could no longer
        recognise it as the same defect."""
        from verification_subagent import (
            DETERMINISTIC_MODE,
            ESCALATE_PATH_MODE,
            LLM_ROUTE,
            resolve,
        )

        vp = {
            "id": "VP-015",
            "verification_method": "full_ci",
            "ci_entry": "bash scripts/nowhere.sh",
        }

        route, mode = resolve(vp, project_dir=tmp_path)

        assert (route, mode) != (LLM_ROUTE, ESCALATE_PATH_MODE)
        assert mode == DETERMINISTIC_MODE
