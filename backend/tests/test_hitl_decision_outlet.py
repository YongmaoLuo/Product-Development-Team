"""VP-010's and VP-011's outlets: the fixture plans can actually be asked.

What is pinned here
-------------------

VP-010 and VP-011 are acceptance criteria, not unit tests.  They ask a
plan for a decision over HTTP and expect the plan's own configuration
to win — ``verify-hitl-api`` says ``mode`` ``auto`` / ``timeout_sec``
1800, ``verify-hitl-strict`` says ``strict`` / 7200 — and the decision
to be left ``pending``.

They can only hold when the fixture plan the acceptance run copies has
somebody to ask.  A question nobody can be asked is ``unroutable`` from
the start (PRD decision point 6): it is never ``pending``, it can never
be answered, and it holds nothing up.  So a plan whose outlet is empty
makes ``$.status == "pending"`` unreachable — not because the service
under test is wrong, but because the fixture it runs against never gave
the question a destination.

This module therefore pins the *fixture*, not the service:

1. **The fixture plan has a deliverable outlet.**  The very
   configuration file the acceptance run copies into its plans tree is
   installed here, and the criterion's own request is asked against it
   with the criterion's own assertions — evaluated by the runner's own
   comparator, so "the criterion holds" means what it means in the
   acceptance report.
2. **An empty outlet still means ``unroutable``.**  The second half of
   the same test empties the hooks of the same plan and asks again: the
   question is ``unroutable``.  The outlet is not allowed to weaken that
   rule, and a fixture that gave every plan a destination would leave
   the ``unroutable`` path untestable — so both halves live in one test.

The service's ``unroutable`` rule, and the rule that a plan's
configuration is read from that plan's own file and nowhere else, are
left exactly as they are: this file changes neither.

Isolation is the autouse ``isolated_plans_dir`` fixture: the plans
tree and the state database are both redirected at ``tmp_path``, so this
file never reads a live plan and never writes into a live database.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from hooks import config_path  # noqa: E402
from utils.atomic_io import atomic_write_json  # noqa: E402
from verification_api_runner import (  # noqa: E402
    _comparator_and_expected,
    _evaluate_comparator,
    _subject_value,
)


client = TestClient(server.app)

#: The plan the acceptance run copies out of ``backend/tests/fixtures``.
PLAN_ID = "verify-hitl-strict"

#: The configuration the acceptance run installs for that plan.  It is
#: read from the fixture rather than restated here: a fixture that is
#: copied into the acceptance run and a fixture that is copied into
#: this test are the same file, and a change to one has to be a change
#: to both.
FIXTURE_CONFIG = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "plans"
    / PLAN_ID
    / "hitl_config.json"
)

#: VP-011's request body, verbatim from the plan's verification point:
#: a question that declares ``mode=auto`` and carries no
#: ``default_option``, asked of a plan whose configuration says
#: ``strict``/7200.
VP011_BODY: Dict[str, Any] = {
    "question": "是否作废该判据？",
    "options": ["作废并替换", "保留原判据"],
    "mode": "auto",
    "source": "verification",
}

#: VP-011's assertions, verbatim from the same verification point.  The
#: names are the plan's own, so a failure here names the same thing the
#: acceptance report names.
VP011_ASSERTIONS: List[Dict[str, Any]] = [
    {"name": "状态码 200", "status": 200},
    {"name": "配置的 strict 覆盖请求体", "json_path": "$.mode", "equals": "strict"},
    {"name": "timeout 取自计划配置 7200", "json_path": "$.timeout_sec", "equals": 7200},
    {"name": "决策状态为 pending", "json_path": "$.status", "equals": "pending"},
]

#: The plan VP-010 is asked of.  It is the plan whose configuration
#: declares ``auto``/``1800`` — the mode and the deadline VP-010 checks
#: are frozen from (PRD decision point 7: the plan's configuration owns
#: them, the request does not).
API_PLAN_ID = "verify-hitl-api"

#: The configuration the acceptance run installs for that plan.  It is
#: read from the fixture rather than restated here, for the same reason
#: as ``FIXTURE_CONFIG`` above: the file the acceptance run copies and
#: the file this test reads have to be one file.
API_FIXTURE_CONFIG = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "plans"
    / API_PLAN_ID
    / "hitl_config.json"
)

#: VP-010's request body, verbatim from the plan's verification point: a
#: question that names a default answer and says nothing about the mode
#: or the deadline, asked of a plan whose configuration says
#: ``auto``/1800.  The mode and the deadline in the answer therefore
#: come from the plan and not from the request.
VP010_BODY: Dict[str, Any] = {
    "question": "是否按默认方案继续？",
    "options": ["approve", "reject"],
    "default_option": "approve",
    "source": "verification",
}

#: VP-010's assertions, verbatim from the same verification point.  The
#: names are the plan's own, so a failure here names the same thing the
#: acceptance report names.
VP010_ASSERTIONS: List[Dict[str, Any]] = [
    {"name": "状态码 200", "status": 200},
    {"name": "决策状态为 pending", "json_path": "$.status", "equals": "pending"},
    {"name": "decision_id 已生成", "json_path": "$.decision_id", "exists": True},
    {"name": "mode 冻结为 auto", "json_path": "$.mode", "equals": "auto"},
    {"name": "timeout 取自计划配置 1800", "json_path": "$.timeout_sec", "equals": 1800},
    {"name": "default_option 原样保存", "json_path": "$.default_option", "equals": "approve"},
]

#: How long to wait for the outlet to be told about the question.  The
#: delivery runs on its own thread, so the wait is a real wait and not
#: a formality; it is generous because a slow machine is not a broken
#: outlet.
DELIVERY_TIMEOUT_SEC = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_fixture_config(
    plans_dir: Path,
    plan_id: str = PLAN_ID,
    fixture: Path = FIXTURE_CONFIG,
) -> Dict[str, Any]:
    """Install a fixture plan's own configuration, and return it.

    The document is read from the fixture file and written where the
    plan reads it from, so what the criterion is measured against here
    is the file the acceptance run copies — not a local paraphrase of
    it.
    """
    document = json.loads(fixture.read_text(encoding="utf-8"))
    plan_dir = Path(plans_dir) / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(config_path(plan_dir), document, reraise=True)
    return document


def _write_config(plan_dir: Path, document: Dict[str, Any]) -> None:
    """Write ``document`` as ``plan_dir``'s HITL configuration."""
    atomic_write_json(config_path(plan_dir), document, reraise=True)


def _failed_assertions(response, assertions) -> List[str]:
    """The assertions that did not hold, in the runner's own words.

    The comparator is the acceptance runner's, not a second
    implementation of it: a criterion that holds here has to hold for the
    same reason it holds in the report.
    """
    failures: List[str] = []
    for index, assertion in enumerate(assertions):
        name = str(assertion.get("name") or f"assertions[{index}]")
        found, actual, label = _subject_value(
            assertion,
            response.status_code,
            {key.lower(): value for key, value in response.headers.items()},
            response.text,
        )
        comparator, expected = _comparator_and_expected(assertion)
        ok, explanation = _evaluate_comparator(
            comparator, expected, found, actual
        )
        if not ok:
            failures.append(f"[{name}] {label}: {explanation}")
    return failures


def _wait_for_a_delivery(plan_dir: Path) -> None:
    """Wait until the plan's outlet has been told about a question.

    The delivery runs on its own thread — a hung destination must not
    be able to hold the request path hostage — so the question is
    ``pending`` before the delivery has happened.  Waiting for the
    dispatcher's audit line is what makes the second half of the test
    deterministic: without it the plan's configuration is rewritten
    underneath a delivery that has not read it yet.

    The audit line records outcomes and never the payload, so what is
    waited for is the count of deliveries that landed.
    """
    audit = plan_dir / "hooks_audit.jsonl"
    deadline = time.monotonic() + DELIVERY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if audit.exists():
            for line in audit.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if int(record.get("delivered") or 0) >= 1:
                    return
        time.sleep(0.05)
    raise AssertionError(
        f"the plan's outlet was never told about the question: {audit} "
        f"holds no delivery after {DELIVERY_TIMEOUT_SEC:.0f}s"
    )


# ---------------------------------------------------------------------------
# The criterion
# ---------------------------------------------------------------------------


def test_vp011_strict_fixture_plan_has_deliverable_outlet(isolated_plans_dir):
    """The fixture plan has somebody to ask — and an empty outlet still
    means ``unroutable``.

    The first half is VP-011 itself: the fixture configuration, the
    plan's own request, the plan's own assertions.  The second half
    takes the outlet away and asks again, because the rule the outlet
    must not weaken is that a question nobody can be asked is
    ``unroutable`` from the start.
    """
    plan_dir = Path(isolated_plans_dir) / PLAN_ID
    document = _install_fixture_config(isolated_plans_dir)

    # (a) The fixture as the acceptance run installs it: a plan whose
    #     configuration is strict/7200 and that has a destination, so
    #     the question is asked of somebody and stays pending.
    response = client.post(f"/api/hitl/{PLAN_ID}/decisions", json=VP011_BODY)
    assert response.status_code == 200, response.text
    failures = _failed_assertions(response, VP011_ASSERTIONS)
    assert not failures, "; ".join(failures)

    # The question really reached the outlet — a ``pending`` decision
    # nobody was told about would be a question waiting for an answer
    # that was never asked for.
    _wait_for_a_delivery(plan_dir)

    # (b) The same plan, with its outlet taken away: nobody can be
    #     asked, so the decision is settled as ``unroutable`` right
    #     away.  The mode and the deadline still come from the plan's
    #     configuration — the outlet is what changed, not the rules.
    without_outlet = dict(document)
    without_outlet["hooks"] = []
    _write_config(plan_dir, without_outlet)

    again = client.post(f"/api/hitl/{PLAN_ID}/decisions", json=VP011_BODY)
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["mode"] == "strict", body
    assert body["timeout_sec"] == 7200, body
    assert body["status"] == "unroutable", body


def test_vp010_fixture_plan_has_deliverable_outlet(isolated_plans_dir):
    """The fixture plan has somebody to ask — and an empty outlet still
    means ``unroutable``.

    The first half is VP-010 itself: the fixture configuration, the
    plan's own request, the plan's own assertions.  The second half
    takes the outlet away and asks again, because the rule the outlet
    must not weaken is that a question nobody can be asked is
    ``unroutable`` from the start.
    """
    plan_dir = Path(isolated_plans_dir) / API_PLAN_ID
    document = _install_fixture_config(
        isolated_plans_dir, API_PLAN_ID, API_FIXTURE_CONFIG,
    )

    # (a) The fixture as the acceptance run installs it: a plan whose
    #     configuration is auto/1800 and that has a destination, so the
    #     question is asked of somebody and stays pending.
    response = client.post(
        f"/api/hitl/{API_PLAN_ID}/decisions", json=VP010_BODY,
    )
    assert response.status_code == 200, response.text
    failures = _failed_assertions(response, VP010_ASSERTIONS)
    assert not failures, "; ".join(failures)

    # The question really reached the outlet — a ``pending`` decision
    # nobody was told about would be a question waiting for an answer
    # that was never asked for.
    _wait_for_a_delivery(plan_dir)

    # (b) The same plan, with its outlet taken away: nobody can be
    #     asked, so the decision is settled as ``unroutable`` right
    #     away.  The mode and the deadline still come from the plan's
    #     configuration — the outlet is what changed, not the rules.
    without_outlet = dict(document)
    without_outlet["hooks"] = []
    _write_config(plan_dir, without_outlet)

    again = client.post(
        f"/api/hitl/{API_PLAN_ID}/decisions", json=VP010_BODY,
    )
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["mode"] == "auto", body
    assert body["timeout_sec"] == 1800, body
    assert body["status"] == "unroutable", body