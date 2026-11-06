"""TDD spec —— round payload 的指纹迁移（旧 payload 不炸、新 payload 往返、旧键不动）。

背景
----
旧键 ``VP-x|分类`` 只回答「同一失败又重复了一次」。它答不了「根因有没有演进」
—— 两次失败分类相同（都是 ``deliverable_defect``），一次断在 JSON 路径、一次断在
状态码，计数说「同一种失败」，根因其实换了。于是循环继续生成第 3、第 4 个修复
任务，每个都打在已经修好的地方。

失败指纹（任务 2）给出的答案是「根因有没有变」。这个文件钉住**指纹怎么进 round
payload、怎么出来、旧 payload 会不会被这个新键炸掉**。

四份契约
--------
1. ``test_legacy_payload_loads`` —— 旧 payload（没有 ``failure_fingerprints``）
   加载不抛错，指纹是「不知道」，决定 fail-open 为假，旧键读出来的值一字未变。
2. ``test_payload_roundtrip`` —— 新 payload 保存后原样加载回来，而且新旧两个键
   **同源**：同一次读、同一次写。
3. ``test_same_failure_counts_untouched`` —— 同样的输入，旧键仍然按「重复」计数，
   循环仍然在该停的那一轮停。

2026-11-06：原来还有第四份契约 ——「修复决策按指纹分叉」（指纹变了就把修复行
改成「先定位根因」）。那是**指纹 → 升级到根因链**那一步，而根因链已经整个
删除：指纹本身留着（它还在 payload 里往返、还带着规则），但它不再分叉出另一
条路。所以那份契约跟着它钉的行为一起没了。

边界条件
--------
* 旧 payload 绝不能因为缺新键而抛 ``KeyError`` —— 一个读不懂的记录必须降级，
  不能让一轮验证整个炸掉。
* 旧键的键名、语义、``max_rounds``、``loop_stopped`` 一律不动。
* payload 里没有指纹时，新键根本不写：空 map 不带信息，写出来只是噪声，而且会
  把旧 payload 的形状改掉。
* 指纹只有一半（``fp_prev`` 缺失）同样是「我们不知道」，不是「它没变」。

测试命令::

    pytest backend/tests/integration/verification_chain/test_round_payload_migration.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# ``backend/`` 进 sys.path，``import verification.*`` 才成立 —— 与仓库里
# 其它测试文件同一套做法，从哪个目录起 pytest 都一样。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification.orchestrator import VerificationOrchestrator  # noqa: E402
from verification.triage_agent import TriageConclusion  # noqa: E402


pytestmark = pytest.mark.integration


#: The round payload's file — the one that holds ``same_failure_counts``.
TRACKING_FILENAME = "verification_loop_tracking.json"

#: The frozen legacy payload (task 7's fixture): a round payload that
#: carries no ``failure_fingerprints`` at all.
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
LEGACY_ROUND_PAYLOAD = FIXTURES / "failure_history" / "legacy_payload.json"

#: The point the rounds below fail on.
VP_ID = "VP-3"

#: The failure class the rounds below carry. It counts (it is not
#: ``infra`` / ``out_of_scope``), so the old key's counter moves.
FAILURE_CLASS = "deliverable_defect"

#: The decision key the old key counts under.
DECISION_KEY = f"{VP_ID}|{FAILURE_CLASS}"

#: The first error line of round 1's failure.
FIRST_ERROR = "AssertionError: expected 200, got 401"



# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir(tmp_path):
    plan = tmp_path / "plans" / "round-payload-migration"
    plan.mkdir(parents=True, exist_ok=True)
    return plan


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "projects" / "round-payload-migration"
    project.mkdir(parents=True, exist_ok=True)
    return project


@pytest.fixture(autouse=True)
def state_db(tmp_path, monkeypatch):
    """Keep the orchestrator's state machine out of the real ``state.db``."""
    db = tmp_path / "state.db"
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(db))
    return db


@pytest.fixture
def repair_mocks():
    """The repair path, mocked so the round's repair rows are observable.

    The generator **echoes** the candidates it was handed, and the
    assembler turns each content row into a task — so what the round
    decided about a row (its title, its description) is what the caller
    can read back.
    """
    with patch("verification.orchestrator.VerificationAgent"), \
         patch("verification.orchestrator.RepairTaskGenerator") as mock_generator, \
         patch("repair_generator.RepairTaskAssembler") as mock_assembler:
        mock_generator.return_value.generate_repair_contents.side_effect = (
            lambda candidates, *args, **kwargs: [
                {
                    "failed_vp_id": str(candidate.get("id")),
                    "title": f"fix {candidate.get('id')}",
                    "description": f"fix {candidate.get('id')}",
                    "files_to_modify": ["src/app.py"],
                    "test_command": "pytest -q",
                }
                for candidate in candidates
            ]
        )
        mock_assembler.return_value.assemble.side_effect = (
            lambda contents: [
                {
                    "id": f"RP-{index}",
                    "failed_vp_id": str(content.get("failed_vp_id")),
                    "title": str(content.get("title") or ""),
                    "description": str(content.get("description") or ""),
                    "files_to_modify": ["src/app.py"],
                    "test_command": "pytest -q",
                }
                for index, content in enumerate(contents, start=1)
            ]
        )
        yield mock_generator


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class _InconclusiveJudge:
    """A triage that ran and could not characterise the failure.

    2026-10-23 (VP-019): reaching the same-failure threshold buys one
    triage call, and this verdict stops the chain. It is the judge the
    old behaviour is pinned with — the chain must stop exactly where it
    stopped before the fingerprint branch existed.
    """

    def __init__(self):
        self.calls = []

    def judge(self, vp, failure_history, round_number=0):
        point = vp if isinstance(vp, dict) else {}
        self.calls.append({
            "vp": dict(point),
            "failure_history": list(failure_history or []),
            "round_number": round_number,
        })
        return TriageConclusion(
            vp_id=str(point.get("id") or ""),
            failure_class=FAILURE_CLASS,
            conclusive=False,
            evidence_quote="",
            rationale="the triage could not characterise the failure",
            available=True,
        )


# ---------------------------------------------------------------------------
# The scenario on disk
# ---------------------------------------------------------------------------


def _write_plan_state(plan_dir, phase="executing", verification_round=0,
                      max_rounds=3):
    """Bootstrap the plan state the orchestrator reads on construction."""
    state = {
        "plan_id": plan_dir.name,
        "current_phase": phase,
        "completed_phases": ["execution"],
        "review_rounds": {"prd": 0, "arch": 0, "test": 0},
        "flags": {},
        "verification": {
            "status": "pending",
            "round": verification_round,
            "max_rounds": max_rounds,
            "stop_reason": None,
        },
    }
    (plan_dir / "plan_state.json").write_text(
        json.dumps(state), encoding="utf-8",
    )


def _write_plan(plan_dir):
    """The verification plan — one point, the one the rounds fail on."""
    payload = {
        "plan_id": plan_dir.name,
        "verification_points": [
            {
                "id": VP_ID,
                "title": "the endpoint returns 200",
                "verification_method": "api_test",
                "related_prd_criteria": "AC-1",
                "expected_result": "the endpoint returns 200",
                "verification_phase": 1,
            },
        ],
    }
    (plan_dir / "verification_plan.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _failed_report(error_line=FIRST_ERROR):
    """A round report: the point failed, with the given first error line."""
    return {
        "overall_status": "FAILED",
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "failure_class": FAILURE_CLASS,
                "actual_result": error_line,
                "evidence": error_line,
            },
        ],
        "requirement_deviations": [],
    }


def _write_report(plan_dir, report):
    """The round's report on disk — what the repair path reads."""
    (plan_dir / "verification_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )


def _read_payload(plan_dir):
    """The round payload on disk, parsed."""
    return json.loads(
        (plan_dir / TRACKING_FILENAME).read_text(encoding="utf-8")
    )


def _orchestrator(plan_dir, project_dir, judge=None):
    """An orchestrator with the agent wiring stubbed out.

    Only the round payload and the repair rows matter here; the
    verification agent and the repair generator would otherwise reach
    for a real toolchain.
    """
    return VerificationOrchestrator(
        plan_dir, project_dir, Mock(), triage_judge=judge,
    )


def _run_round(plan_dir, project_dir, report, round_number, judge=None):
    """Run one verification round through a *fresh* orchestrator.

    A fresh instance per round is how production runs it: the auto-loop
    rebuilds the orchestrator after every repair, so anything the round
    remembers has to come back from the payload on disk.
    """
    _write_report(plan_dir, report)
    orch = _orchestrator(plan_dir, project_dir, judge=judge)
    orch.start_verification_cycle(round_number=round_number)
    result = orch.check_cycle_conditions(report, round_number=round_number)
    return orch, result


# ---------------------------------------------------------------------------
# 1. 旧 payload 加载
# ---------------------------------------------------------------------------


def test_legacy_payload_loads(plan_dir, project_dir):
    """A payload without the new key loads — and knows no fingerprint.

    The legacy shape carries the old key and nothing else. It must read
    exactly as it read before the fingerprints existed, and the decision
    it feeds must be "no": the count says the failure repeated, the
    fingerprints say we do not know why, so the old path takes it.
    """
    legacy = {
        "version": 2,
        "previous_failed_keys": [DECISION_KEY],
        "same_failure_counts": {DECISION_KEY: 2},
        "last_round": 1,
    }
    (plan_dir / TRACKING_FILENAME).write_text(
        json.dumps(legacy), encoding="utf-8",
    )

    orch = _orchestrator(plan_dir, project_dir)

    assert orch._failure_fingerprints == {}, (
        "a payload without fingerprints knows none — that is not an error"
    )

    keys, counts, last_round = orch._load_loop_tracking()
    assert keys == {(VP_ID, FAILURE_CLASS)}, "the old key reads as before"
    assert counts == {DECISION_KEY: 2}, "the old count reads as before"
    assert last_round == 1

    # The history this payload describes: the count is read, the
    # fingerprints are not there to be read.
    history = {
        "vp_id": VP_ID,
        "consecutive_failures": counts[DECISION_KEY],
        "fp_prev": None,
        "fp_curr": None,
    }
    assert history["fp_prev"] is None and history["fp_curr"] is None, (
        "a legacy payload knows no fingerprint — that is not an error"
    )


def test_legacy_fixture_payload_loads(plan_dir, project_dir):
    """The frozen legacy fixture (task 7) loads without a ``KeyError``.

    The fixture is a round payload of a shape this reader has never
    seen: it names no decision key at all. A reader that cannot make
    sense of a payload must degrade to "nothing known" — it must not
    raise, because one unreadable record must not be able to abort a
    round.
    """
    payload = json.loads(LEGACY_ROUND_PAYLOAD.read_text(encoding="utf-8"))
    assert "failure_fingerprints" not in payload, (
        "the fixture's whole point is that the key is absent"
    )
    (plan_dir / TRACKING_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8",
    )

    orch = _orchestrator(plan_dir, project_dir)

    assert orch._failure_fingerprints == {}
    keys, counts, last_round = orch._load_loop_tracking()
    assert keys == set(), "a payload with no decision key names no failure"
    assert counts == {}
    assert last_round == 0

    # The history degrades too — no count, no fingerprint, no answer.
    assert counts.get(DECISION_KEY, 0) == 0


def test_malformed_fingerprints_are_dropped(plan_dir, project_dir):
    """A half-written fingerprint is dropped, not repaired, not fatal."""
    (plan_dir / TRACKING_FILENAME).write_text(
        json.dumps({
            "version": 2,
            "previous_failed_keys": [DECISION_KEY],
            "same_failure_counts": {DECISION_KEY: 2},
            "last_round": 1,
            "failure_fingerprints": {
                VP_ID: ["aaa"],              # half a pair
                "VP-4": ["aaa", "bbb"],      # fine
                "VP-5": ["aaa", 7],          # not a fingerprint
                "VP-6": "aaa",               # not a pair
                "": ["aaa", "bbb"],          # no point
            },
        }),
        encoding="utf-8",
    )

    orch = _orchestrator(plan_dir, project_dir)

    assert orch._failure_fingerprints == {"VP-4": ["aaa", "bbb"]}, (
        "only a well-formed pair is evidence; the rest is dropped"
    )
    # The old key is untouched by a broken new key.
    _, counts, _ = orch._load_loop_tracking()
    assert counts == {DECISION_KEY: 2}


# ---------------------------------------------------------------------------
# 2. 新 payload 往返
# ---------------------------------------------------------------------------


def test_payload_roundtrip(plan_dir, project_dir):
    """What the round writes is what the next round's orchestrator reads.

    The payload has to outlive the orchestrator instance — the
    post-repair round builds a fresh one — so the round trip through the
    file is the whole contract. And the two keys come from the **same**
    read and the **same** write: a round can never see the count of one
    write and the fingerprint of another.
    """
    orch = _orchestrator(plan_dir, project_dir)
    orch._previous_failed_keys = {(VP_ID, FAILURE_CLASS)}
    orch._same_failure_counts = {DECISION_KEY: 2}
    orch._failure_fingerprints = {VP_ID: ["aaa", "bbb"]}
    orch._last_round = 4
    orch._save_loop_tracking()

    payload = _read_payload(plan_dir)
    assert payload["failure_fingerprints"] == {VP_ID: ["aaa", "bbb"]}
    assert payload["same_failure_counts"] == {DECISION_KEY: 2}, (
        "the old key rides along — the two come from the same write"
    )
    assert payload["previous_failed_keys"] == [DECISION_KEY]
    assert payload["last_round"] == 4

    fresh = _orchestrator(plan_dir, project_dir)
    assert fresh._failure_fingerprints == {VP_ID: ["aaa", "bbb"]}
    keys, counts, last_round = fresh._load_loop_tracking()
    assert keys == {(VP_ID, FAILURE_CLASS)}
    assert counts == {DECISION_KEY: 2}
    assert last_round == 4


def test_empty_fingerprints_are_not_written(plan_dir, project_dir):
    """An empty fingerprint map is not written at all.

    An empty map carries no information, and a payload without the key
    is exactly the legacy shape — which the reader already tolerates. So
    a run that has not seen a failure yet writes the file it has always
    written, and the payload's key set does not change under the
    consumer's feet.
    """
    orch = _orchestrator(plan_dir, project_dir)
    orch._same_failure_counts = {DECISION_KEY: 1}
    orch._save_loop_tracking()

    payload = _read_payload(plan_dir)
    assert "failure_fingerprints" not in payload
    assert set(payload) == {
        "version", "previous_failed_keys", "same_failure_counts", "last_round",
    }


# ---------------------------------------------------------------------------
# 3. 旧键不动
# ---------------------------------------------------------------------------


def test_same_failure_counts_untouched(plan_dir, project_dir, repair_mocks):
    """The old key still counts repeats, and still stops the loop.

    The fingerprint branch is additive: it must not change what the old
    key counts, nor where the loop stops. The same failure twice is
    the old key's business.
    """
    _write_plan_state(plan_dir)
    _write_plan(plan_dir)
    report = _failed_report()

    _, r1 = _run_round(plan_dir, project_dir, report, 1, judge=_InconclusiveJudge())
    assert r1["status"] != "loop_stopped", "the first failure is not a repeat"

    _, r2 = _run_round(plan_dir, project_dir, report, 2, judge=_InconclusiveJudge())
    assert r2["status"] != "loop_stopped", (
        "the second failure is still inside the repair budget (2026-11-06: "
        "the budget is two rounds)"
    )

    orch, r3 = _run_round(plan_dir, project_dir, report, 3, judge=_InconclusiveJudge())
    assert r3["status"] == "loop_stopped", (
        "the same failure for a third round stops the loop"
    )
    assert orch._same_failure_counts == {DECISION_KEY: 3}, (
        "the old counter still counts the repeat"
    )



# ---------------------------------------------------------------------------
# 4. 规则跟着指纹一起过一轮
# ---------------------------------------------------------------------------

def test_the_rule_pair_round_trips_with_the_fingerprints(plan_dir, project_dir):
    """The rule each fingerprint was made under rides along with it.

    The fingerprint is the only input to "did the root cause evolve?".
    Two fingerprints made under different rules are not comparable — the
    instrument changed, not the defect — so the rule has to survive the
    round trip through the payload. Without it, the next round compares a
    legacy fingerprint with an identity one and reads that as an evolved
    root cause, which re-opens a root-cause task for a failure nobody
    touched.
    """
    orch = _orchestrator(plan_dir, project_dir)

    # The gate's artifact: the machine-written failing set of this round.
    artifact_dir = plan_dir / "vp_artifacts" / VP_ID
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "ci_output.json").write_text(
        json.dumps({
            "junit_failures": [
                {"node_id": "tests/unit/test_x.py::test_one", "first_line": "boom"},
                {"node_id": "tests/unit/test_x.py::test_two", "first_line": "bang"},
            ],
        }),
        encoding="utf-8",
    )

    report = {
        "verification_results": [
            {
                "id": VP_ID,
                "status": "FAILED",
                "actual_result": "boom",
                "evidence": "boom",
            },
        ],
    }

    orch._record_round_fingerprints(report, set())

    rules = getattr(orch, "_failure_fingerprint_rules", None)
    assert rules is not None, (
        "the orchestrator must remember which rule each fingerprint was "
        "computed under"
    )
    assert rules[VP_ID][1] == "identity-v1", (
        "a fingerprint computed from the gate's failing set is an identity "
        "fingerprint — the rule says so"
    )
    assert orch._failure_fingerprints[VP_ID][0] is None, (
        "the point failed for the first time — there is no previous "
        "fingerprint to compare against"
    )

    orch._save_loop_tracking()
    payload = _read_payload(plan_dir)
    assert payload["failure_fingerprint_rules"] == {VP_ID: [None, "identity-v1"]}, (
        "the rule pair rides in the payload next to the fingerprint pair"
    )

    fresh = _orchestrator(plan_dir, project_dir)
    assert fresh._failure_fingerprint_rules == {VP_ID: [None, "identity-v1"]}, (
        "what the round wrote is what the next round's orchestrator reads"
    )


def test_a_payload_without_rules_is_read_as_unknown(plan_dir, project_dir):
    """A payload written before the rules existed knows no rule.

    "We do not know" must not be spelled as "it was the same" and must not
    raise — a record the reader cannot make sense of degrades.
    """
    (plan_dir / TRACKING_FILENAME).write_text(
        json.dumps({
            "version": 2,
            "previous_failed_keys": [DECISION_KEY],
            "same_failure_counts": {DECISION_KEY: 2},
            "last_round": 1,
            "failure_fingerprints": {VP_ID: ["aaa", "bbb"]},
        }),
        encoding="utf-8",
    )

    orch = _orchestrator(plan_dir, project_dir)
    assert orch._failure_fingerprint_rules == {}, (
        "a payload without rules knows none — that is not an error"
    )
    assert orch._failure_fingerprints == {VP_ID: ["aaa", "bbb"]}
