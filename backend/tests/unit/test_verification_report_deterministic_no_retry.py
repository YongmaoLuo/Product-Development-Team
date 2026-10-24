"""Deterministic local errors must not be retried by Phase 3 (2026-10-23).

Incident A, second retry chain: ``Orchestrator.generate_verification_report``
judged the report with ``retry_llm=3`` and its generic
``except Exception`` handler only gave up on the LAST attempt. Every
earlier attempt fell straight into the next loop iteration — with no
backoff — so a *deterministic* local failure (the dispatch's working
directory is gone: ``cwd`` does not exist) was paid for three full
dispatches before the fallback report was produced. No retry can heal
a missing directory; the three attempts only multiplied the cost and
buried the real cause under "retrying" log lines.

``coding_tool.is_deterministic_local_error`` (added by the sibling task)
is the single place that knows which errors are answers about the LOCAL
environment (``FileNotFoundError`` / ``NotADirectoryError`` /
``PermissionError``). Phase 3 consumes it: a deterministic local error
short-circuits the retry loop and returns the fallback report right
away.

Everything else keeps its retry semantics. ``ApiError`` has its own
branch (provider-side, with its own rules), and an unexpected error
(``RuntimeError`` and friends) still runs the full ``retry_llm`` rounds
before the fallback report — the fix must not widen into "no exception
is ever retried".
"""

from pathlib import Path

from verification_agent import Orchestrator


class FakeTool:
    """A coding tool whose ``query_json`` always raises ``exc``.

    Counts its calls so a test can assert HOW MANY dispatches the
    judgment phase actually paid for.
    """

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    def query_json(self, *args, **kwargs):
        self.calls += 1
        raise self.exc


def _make_agent(tmp_path: Path, tool) -> Orchestrator:
    """Build an orchestrator over throwaway plan/project directories."""
    plan_dir = tmp_path / "plan"
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return Orchestrator(
        plan_dir=plan_dir,
        project_dir=project_dir,
        coding_tool=tool,
    )


def _execution_results() -> dict:
    """One FAILED VP — the fallback report's status is derived from it."""
    return {
        "verification_points": [],
        "execution_results": [
            {
                "id": "VP-001",
                "status": "FAILED",
                "actual_result": "cwd does not exist",
                "evidence": "FileNotFoundError",
            },
        ],
    }


def test_deterministic_error_returns_fallback_after_one_dispatch(tmp_path):
    """A missing working directory is not retryable — dispatch once, fall back.

    The dispatch raises ``FileNotFoundError`` (the incident's exact
    error). The judgment phase must spend exactly ONE dispatch on it and
    return the fallback report, whose ``overall_status`` is derived
    from the round's execution results (one FAILED VP → ``FAILED``).
    """
    tool = FakeTool(FileNotFoundError("[Errno 2] No such file or directory"))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report(_execution_results())

    assert tool.calls == 1, (
        "a deterministic local error must not be retried: the missing "
        "directory is still missing on the second attempt"
    )
    assert report["overall_status"] == "FAILED"
    assert report["verification_results"][0]["id"] == "VP-001"


def test_unexpected_error_still_retries(tmp_path):
    """An unexpected error still runs the full retry budget.

    The fix narrows ONE class of failures (deterministic local ones).
    A ``RuntimeError`` says nothing about the local environment, so the
    judgment phase keeps its ``retry_llm`` attempts and only then falls
    back — the fix must not become "no exception is ever retried".
    """
    tool = FakeTool(RuntimeError("LLM down"))
    agent = _make_agent(tmp_path, tool)

    report = agent.generate_verification_report(_execution_results())

    assert tool.calls == 3, (
        "an unexpected error must still be retried the full retry_llm "
        "budget before the fallback report"
    )
    assert report["overall_status"] == "FAILED"
    assert report["verification_results"][0]["id"] == "VP-001"
