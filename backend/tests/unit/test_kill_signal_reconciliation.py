"""The kill-signal reconciliation, tested on its own.

``scripts/kill_signal_reconciliation.py`` is the tool that turns a
round's raw evidence into a **decidable** statement about who sent a
signal.  It exists because of a failure that is easy to repeat and hard
to see: a conclusion was drawn from a query that could never match, so
the conclusion was unfounded while looking perfectly rigorous.

The rules it has to keep
------------------------

* **A query that cannot match must not answer anything.**  When the
  ledger does not carry the field the search needs, the answer names
  the gap -- 「无法归因：账本不含 <字段>」 -- and never degrades into
  a statement about the world.
* **Every attribution carries its evidence.**  A matched send record
  names the ledger file and the line, so a reader can go and look.
* **The three places a sender can be** are told apart: inside the
  repository (with the path), inside the receiver's own process group
  but outside the repository's audit, or outside both.
* **The control artifacts must be told apart too.**  The two negative
  controls describe opposite situations; a tool that answers them the
  same way answers neither.
* **It reads.**  It sends no signal, and it changes no file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import verification_ci_runner as ci_runner  # noqa: E402

_SCRIPT_PATH = REPO_ROOT / "scripts" / "kill_signal_reconciliation.py"


def _load(path: Path, name: str) -> ModuleType:
    """Import a module from its file."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


recon = _load(_SCRIPT_PATH, "kill_signal_reconciliation_under_test")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: The second the fatal SIGTERM arrived, in the local time the evidence
#: was written in.
ARRIVED_AT = 1793256721.0

#: The process group the signal arrived at.
RECEIVER_PGID = 68639


def _sent_record(
    *,
    target_pid=None,
    target_pgid=None,
    is_group=False,
    signal=15,
    sender_pid=68728,
    sender_pgid=68639,
    ts_epoch=ARRIVED_AT,
    stack="backend/service_manager.py:1381 kill_service",
) -> dict:
    """One record in the shape ``backend/kill_audit.py`` writes."""
    return {
        "event": "signal.sent",
        "call": "os.killpg" if is_group else "os.kill",
        "signal": signal,
        "signal_name": "SIGTERM",
        "target_pid": target_pid,
        "target_pgid": target_pgid,
        "target_is_group": is_group,
        "sender_pid": sender_pid,
        "sender_pgid": sender_pgid,
        "sender_is_self": False,
        "ts": "2026-10-29T03:32:01.100000",
        "ts_epoch": ts_epoch,
        "stack": stack,
        # the old field names, kept for the readers that still use them
        "kind": "killpg" if is_group else "kill",
        "target": target_pgid if is_group else target_pid,
        "outcome": "attempted",
        "pid": sender_pid,
    }


def _received_record(
    *,
    receiver_pid=68728,
    receiver_pgid=RECEIVER_PGID,
    ts_epoch=ARRIVED_AT,
) -> dict:
    """One record in the shape the received side of the audit writes."""
    return {
        "event": "signal.received",
        "signal": 15,
        "signal_name": "SIGTERM",
        "receiver_pid": receiver_pid,
        "receiver_pgid": receiver_pgid,
        "received_at": "2026-10-29T03:32:01",
        "received_at_second": "2026-10-29T03:32:01",
        "received_at_epoch": ts_epoch,
        "group_members": [
            {"pid": receiver_pid, "pgid": receiver_pgid, "command": "pytest"},
        ],
        "sender_pid": None,
        "sender_pgid": None,
        "sender_is_self": False,
        "ts": "2026-10-29T03:32:01.100000",
        "ts_epoch": ts_epoch,
        "stack": "signal handler",
    }


def _legacy_record() -> dict:
    """One record in the shape the round-7 ledger actually has.

    The fields are ``{ts, event, args, pid, ppid, pgid, stack}`` -- and
    there is **no** ``target`` key, which is why the query that was run
    against it could never match anything.
    """
    return {
        "ts": 1793510626.359373,
        "event": "os.kill",
        "args": [2873, 9],
        "pid": 99993,
        "ppid": 99465,
        "pgid": 99295,
        "stack": ["/tmp/pdt-kill-forensics/sitecustomize.py:41:_hook"],
    }


def _write_ledger(path: Path, records) -> Path:
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _write_census(path: Path, rows, epoch=ARRIVED_AT) -> Path:
    """A ps census in the shape the round-7 sampling wrote."""
    lines = [f"--- {int(epoch)} ---", "  PID  PPID  PGID COMMAND"]
    for pid, ppid, pgid, command in rows:
        lines.append(f"{pid} {ppid} {pgid} {command}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_trap(path: Path, *, mode: str, sentinel_alive: str) -> Path:
    """A control artifact in the shape ``negctl_group_signal_trap.sh`` writes."""
    shell_pid = 90117
    sentinel_pid = 90134
    body = [
        f"=== 2026-11-01T08:42:43 the control shell was signalled ===",
        f"mode={mode}",
        "signal_target=-90117",
        f"shell_pid={shell_pid}",
        f"shell_pgid={shell_pid}",
        f"sentinel_pid={sentinel_pid}",
        "sentinel_alive_before_the_signal=yes",
        f"sentinel_alive_after_the_signal={sentinel_alive}",
        f"received_by={mode}",
        "--- ps -axo pid,ppid,pgid,command | awk '$3 == 90117' "
        "(the group at the moment of the signal) ---",
        "  PID  PPID  PGID COMMAND",
        f"{shell_pid} 90115 {shell_pid} bash scripts/negctl_group_signal_trap.sh",
        "--- end of the census ---",
    ]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


def _reconcile(**kwargs) -> dict:
    """The reconciliation with the defaults of this investigation."""
    options = {
        "ledgers": [],
        "ps_samples": None,
        "traps": [],
        "receiver_pgid": RECEIVER_PGID,
        "receiver_pid": 68728,
        "signal_number": 15,
        "at_epoch": ARRIVED_AT,
        "sender_pids": [],
        "repo_root": REPO_ROOT,
        "round_label": "7",
        "window": 2.0,
    }
    options.update(kwargs)
    return recon.reconcile(**options)


def _signal_findings(report: dict) -> list:
    return [
        finding for finding in report["findings"]
        if finding["kind"] == "received_signal"
    ]


# ---------------------------------------------------------------------------
# The query that cannot match
# ---------------------------------------------------------------------------


def test_a_ledger_without_the_field_the_search_needs_is_unable_to_attribute(
    tmp_path,
):
    """The round-7 ledger has no ``target_pid``/``target_pgid``.

    The search cannot be run against it, so the answer cannot be "there
    is no sender record" -- it is "the ledger does not carry the field
    the search needs".  This is the regression pin for the query that
    could never match.
    """
    ledger = _write_ledger(tmp_path / "kill-audit-round7.jsonl",
                            [_legacy_record()])

    report = _reconcile(ledgers=[ledger])

    findings = _signal_findings(report)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["verdict"] == recon.VERDICT_UNABLE
    assert "target_pgid" in finding["missing_fields"]
    assert "无法归因：账本不含 target_pgid" in finding["reason"]
    assert "no sender record" not in finding["reason"]
    assert "没有发送者记录" not in finding["reason"]
    assert finding["attribution"] is None
    assert report["attribution_complete"] is False


def test_the_unable_to_attribute_reason_never_degrades_into_absence(tmp_path):
    """The old wording ("no sender record") is what made the old
    conclusion unfounded, so it must not come back."""
    ledger = _write_ledger(tmp_path / "kill-audit-round7.jsonl",
                            [_legacy_record()])

    report = _reconcile(ledgers=[ledger])

    blob = json.dumps(report, ensure_ascii=False)
    assert "no sender record" not in blob
    assert "没有发送者记录" not in blob
    assert "unable_to_attribute" in blob
    assert "无法归因：账本不含" in blob


def test_a_ledger_missing_one_explicit_field_names_that_field(tmp_path):
    """The explicit fields the search reads are the fields the
    conclusion needs, so a ledger that lacks one of them names *that*
    field -- not a general complaint about the ledger."""
    record = _sent_record(target_pgid=RECEIVER_PGID, is_group=True)
    del record["target_is_group"]
    ledger = _write_ledger(tmp_path / "kill-audit-round7.jsonl", [record])

    report = _reconcile(ledgers=[ledger])

    finding = _signal_findings(report)[0]
    assert finding["verdict"] == recon.VERDICT_UNABLE
    assert finding["missing_fields"] == ["target_is_group"]
    assert "无法归因：账本不含 target_is_group" in finding["reason"]
    assert finding["attribution"] is None


def test_a_target_is_never_guessed_from_args(tmp_path):
    """The old ledger carries the target in ``args``.

    Reading it would be guessing -- the conclusion may only come from an
    explicit field, so a record whose target lives in ``args`` matches
    nothing and the answer names the missing field instead.
    """
    ledger = _write_ledger(
        tmp_path / "kill-audit-round7.jsonl",
        [{
            "ts": 1793510626.359373,
            "event": "os.kill",
            "args": [68728, 15],
            "pid": 99993,
            "ppid": 99465,
            "pgid": 99295,
            "stack": ["/tmp/pdt-kill-forensics/sitecustomize.py:41:_hook"],
        }],
    )

    report = _reconcile(ledgers=[ledger])

    finding = _signal_findings(report)[0]
    assert finding["attribution"] is None
    assert finding["verdict"] == recon.VERDICT_UNABLE
    assert "无法归因：账本不含" in finding["reason"]


def test_an_empty_ledger_cannot_answer_the_question_either(tmp_path):
    """A ledger with no send record at all carries no field either."""
    ledger = _write_ledger(tmp_path / "kill-audit-round7.jsonl", [])

    report = _reconcile(ledgers=[ledger])

    finding = _signal_findings(report)[0]
    assert finding["verdict"] == recon.VERDICT_UNABLE
    assert finding["missing_fields"]


# ---------------------------------------------------------------------------
# A match, with its evidence
# ---------------------------------------------------------------------------


def test_a_matching_send_record_is_attributed_with_its_line_number(tmp_path):
    """The attribution names the ledger file and the line, so a reader
    can go and look at the record."""
    ledger = _write_ledger(
        tmp_path / "kill-audit-round7.jsonl",
        [
            _sent_record(target_pid=18618, signal=15,
                         ts_epoch=ARRIVED_AT - 30),
            _sent_record(target_pgid=RECEIVER_PGID, is_group=True,
                         signal=15, ts_epoch=ARRIVED_AT),
        ],
    )

    report = _reconcile(ledgers=[ledger])

    finding = _signal_findings(report)[0]
    assert finding["verdict"] == recon.VERDICT_INSIDE_REPO
    attribution = finding["attribution"]
    assert attribution is not None
    assert attribution["ledger"] == str(ledger)
    assert attribution["line"] == 2
    assert attribution["record"]["sender_pid"] == 68728
    assert finding["sender"]["repo_path"] == "backend/service_manager.py"
    assert report["attribution_complete"] is True


def test_a_send_to_another_group_is_not_the_one_that_arrived(tmp_path):
    """The search is scoped to the receiver's group and the second the
    signal arrived."""
    ledger = _write_ledger(
        tmp_path / "kill-audit-round7.jsonl",
        [
            _sent_record(target_pgid=99999, is_group=True, signal=15,
                         ts_epoch=ARRIVED_AT),
            _sent_record(target_pgid=RECEIVER_PGID, is_group=True, signal=15,
                         ts_epoch=ARRIVED_AT - 600),
        ],
    )

    report = _reconcile(ledgers=[ledger])

    finding = _signal_findings(report)[0]
    assert finding["attribution"] is None


def test_a_single_pid_send_to_a_member_of_the_group_is_the_signal(tmp_path):
    """A group signal and a single-pid signal both reach the receiver;
    both are found."""
    census = _write_census(
        tmp_path / "ps.txt",
        [(68728, 68686, RECEIVER_PGID, "pytest"), (68653, 68686, 68639, "x")],
    )
    ledger = _write_ledger(
        tmp_path / "kill-audit-round7.jsonl",
        [_sent_record(target_pid=68653, signal=15, ts_epoch=ARRIVED_AT)],
    )

    report = _reconcile(ledgers=[ledger], ps_samples=census)

    finding = _signal_findings(report)[0]
    assert finding["attribution"] is not None
    assert finding["attribution"]["line"] == 1


def test_a_group_send_to_another_group_is_not_matched_through_a_member_pid(
    tmp_path,
):
    """A send to another group stays a send to another group.

    ``target_is_group`` says which it was, so the search is made by the
    group a group send went to -- never by a pid that happens to sit in
    the receiver's group.
    """
    census = _write_census(
        tmp_path / "ps.txt", [(68728, 68686, RECEIVER_PGID, "pytest")],
    )
    ledger = _write_ledger(
        tmp_path / "kill-audit-round7.jsonl",
        [_sent_record(target_pgid=99999, target_pid=68728, is_group=True,
                      ts_epoch=ARRIVED_AT)],
    )

    report = _reconcile(ledgers=[ledger], ps_samples=census)

    finding = _signal_findings(report)[0]
    assert finding["attribution"] is None


def test_the_matched_record_states_how_the_signal_arrived(tmp_path):
    """``target_is_group`` says whether the signal went to the whole
    group or to one pid.  The two are different situations, and the
    finding has to tell them apart."""
    census = _write_census(
        tmp_path / "ps.txt", [(68728, 68686, RECEIVER_PGID, "pytest")],
    )
    group = _write_ledger(
        tmp_path / "group.jsonl",
        [_sent_record(target_pgid=RECEIVER_PGID, is_group=True,
                      ts_epoch=ARRIVED_AT)],
    )
    single = _write_ledger(
        tmp_path / "single.jsonl",
        [_sent_record(target_pid=68728, ts_epoch=ARRIVED_AT)],
    )

    group_finding = _signal_findings(_reconcile(
        ledgers=[group], ps_samples=census))[0]
    single_finding = _signal_findings(_reconcile(
        ledgers=[single], ps_samples=census))[0]

    assert group_finding["delivered_to"] == "group"
    assert single_finding["delivered_to"] == "single-pid"
    assert group_finding["delivered_to"] != single_finding["delivered_to"]


def test_a_send_to_the_receiver_itself_is_named_as_such(tmp_path):
    """``sender_is_self`` is an explicit field: when the ledger says the
    process that received the signal sent it, the finding says so."""
    census = _write_census(
        tmp_path / "ps.txt", [(68728, 68686, RECEIVER_PGID, "pytest")],
    )
    record = _sent_record(target_pid=68728, sender_pid=68728,
                          ts_epoch=ARRIVED_AT)
    record["sender_is_self"] = True
    ledger = _write_ledger(tmp_path / "kill-audit-round7.jsonl", [record])

    report = _reconcile(ledgers=[ledger], ps_samples=census)

    finding = _signal_findings(report)[0]
    assert finding["attribution"] is not None
    assert finding["sender"]["is_self"] is True
    assert "itself" in finding["reason"]


# ---------------------------------------------------------------------------
# The three places a sender can be
# ---------------------------------------------------------------------------


def test_the_sender_inside_the_repository_is_named_with_its_path(tmp_path):
    ledger = _write_ledger(
        tmp_path / "kill-audit-round7.jsonl",
        [_sent_record(target_pgid=RECEIVER_PGID, is_group=True,
                      ts_epoch=ARRIVED_AT,
                      stack="backend/service_manager.py:1381 kill_service")],
    )

    report = _reconcile(ledgers=[ledger])

    finding = _signal_findings(report)[0]
    assert finding["verdict"] == recon.VERDICT_INSIDE_REPO
    assert finding["sender"]["repo_path"] == "backend/service_manager.py"


def test_the_sender_in_the_receivers_own_group_is_told_apart(tmp_path):
    """A member of the receiver's group that the repository's audit
    never saw is a different answer from "somewhere else"."""
    census = _write_census(
        tmp_path / "ps.txt",
        [
            (68728, 68686, RECEIVER_PGID, "pytest"),
            (68601, 68600, RECEIVER_PGID, "bash scripts/run_tests.sh"),
        ],
    )
    ledger = _write_ledger(
        tmp_path / "kill-audit-round7.jsonl",
        [_sent_record(target_pid=18618, signal=15, ts_epoch=ARRIVED_AT - 30)],
    )

    report = _reconcile(ledgers=[ledger], ps_samples=census)

    finding = _signal_findings(report)[0]
    assert finding["verdict"] == recon.VERDICT_IN_GROUP
    assert finding["sender"]["pid"] == 68601
    assert "run_tests.sh" in finding["sender"]["command"]


def test_the_sender_outside_both_is_told_apart(tmp_path):
    """A process that is neither in the repository nor in the receiver's
    group is the third answer, and it is not the second one."""
    census = _write_census(
        tmp_path / "ps.txt",
        [
            (68728, 68686, RECEIVER_PGID, "pytest"),
            (55555, 1, 55555, "python -m pytest /other/checkout/tests"),
        ],
    )
    ledger = _write_ledger(
        tmp_path / "kill-audit-round7.jsonl",
        [_sent_record(target_pid=18618, signal=15, ts_epoch=ARRIVED_AT - 30)],
    )

    report = _reconcile(ledgers=[ledger], ps_samples=census,
                        sender_pids=[55555])

    finding = _signal_findings(report)[0]
    assert finding["verdict"] == recon.VERDICT_OUTSIDE
    assert finding["sender"]["pid"] == 55555


# ---------------------------------------------------------------------------
# The control artifacts
# ---------------------------------------------------------------------------


def test_the_two_control_artifacts_get_opposite_verdicts(tmp_path):
    """The group round and the single-pid round are different
    situations; the tool has to be able to tell them apart."""
    group = _write_trap(tmp_path / "group-trap.txt", mode="group",
                        sentinel_alive="no")
    single = _write_trap(tmp_path / "group-trap-single-pid.txt",
                         mode="single-pid", sentinel_alive="yes")

    report = _reconcile(traps=[group, single])

    control = [f for f in report["findings"] if f["kind"] == "control_trap"]
    assert len(control) == 2
    verdicts = {Path(f["artifact"]).name: f["verdict"] for f in control}
    assert verdicts["group-trap.txt"] == recon.VERDICT_GROUP_SIGNAL
    assert verdicts["group-trap-single-pid.txt"] == recon.VERDICT_SINGLE_PID
    assert verdicts["group-trap.txt"] != verdicts["group-trap-single-pid.txt"]


def test_a_control_that_contradicts_itself_is_reported_as_such(tmp_path):
    """The sentinel is the witness: alive before, gone after means the
    group got it.  A file that says otherwise is not evidence."""
    trap = _write_trap(tmp_path / "group-trap.txt", mode="group",
                        sentinel_alive="yes")

    report = _reconcile(traps=[trap])

    control = [f for f in report["findings"] if f["kind"] == "control_trap"][0]
    assert control["verdict"] == recon.VERDICT_CONTROL_INCONSISTENT


# ---------------------------------------------------------------------------
# The round, and the read-only promise
# ---------------------------------------------------------------------------


def test_the_report_names_the_round_it_reconciles(tmp_path):
    """A conclusion about "the run" is worthless without the round."""
    ledger = _write_ledger(tmp_path / "kill-audit-round7.jsonl",
                            [_legacy_record()])

    report = _reconcile(ledgers=[ledger], round_label="round-7")

    assert report["round"] == "round-7"
    assert report["inputs"]["ledgers"] == [str(ledger)]


def test_the_script_refuses_to_reconcile_without_a_round(tmp_path, capsys):
    """The round is not optional: the whole point is that a conclusion
    says which round it belongs to."""
    ledger = _write_ledger(tmp_path / "kill-audit-round7.jsonl",
                            [_legacy_record()])

    with pytest.raises(SystemExit) as excinfo:
        recon.main(["--ledger", str(ledger)])

    assert excinfo.value.code == 2


def test_the_script_sends_no_signal():
    """A reconciliation tool that could signal would be able to create
    the very evidence it is asked to explain."""
    source = _SCRIPT_PATH.read_text(encoding="utf-8")
    for forbidden in ("os.kill(", "os.killpg(", "signal.raise_signal(",
                      "import subprocess", "subprocess."):
        assert forbidden not in source, forbidden


def test_a_received_record_of_the_older_shape_is_still_read(tmp_path):
    """The ledgers of the earlier rounds wrote the receiving side with
    the fields they had: ``pid`` and ``pgid`` of the receiver.

    Those records are evidence -- the excerpt of the instrumented run
    holds the fatal signal's own receipt -- so the tool has to read
    them, not skip them because they do not look like the current
    shape.
    """
    ledger = _write_ledger(
        tmp_path / "audit_ledger_excerpt.jsonl",
        [{
            "ts": "2026-10-29T03:32:01",
            "event": "signal.received",
            "pid": 68728,
            "pgid": 68639,
            "target": 15,
            "signal": 15,
        }],
    )

    report = recon.reconcile(
        ledgers=[ledger], repo_root=REPO_ROOT, round_label="1",
    )

    findings = _signal_findings(report)
    assert len(findings) == 1
    assert findings[0]["receiver_pid"] == 68728
    assert findings[0]["receiver_pgid"] == 68639
    assert findings[0]["signal_number"] == 15
    # the ledger has no field the search needs, so the answer is the
    # honest one -- not "there is no sender record"
    assert findings[0]["verdict"] == recon.VERDICT_UNABLE
    assert "无法归因：账本不含 target_pgid" in findings[0]["reason"]


def test_the_script_leaves_its_inputs_untouched(tmp_path):
    ledger = _write_ledger(tmp_path / "kill-audit-round7.jsonl",
                            [_legacy_record()])
    census = _write_census(tmp_path / "ps.txt",
                            [(68728, 68686, RECEIVER_PGID, "pytest")])
    trap = _write_trap(tmp_path / "group-trap.txt", mode="group",
                       sentinel_alive="no")
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ledger, census, trap)
    }

    recon.main([
        "--ledger", str(ledger),
        "--ps-samples", str(census),
        "--trap", str(trap),
        "--receiver-pgid", str(RECEIVER_PGID),
        "--round", "7",
    ])

    after = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (ledger, census, trap)
    }
    assert before == after
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "group-trap.txt", "kill-audit-round7.jsonl", "ps.txt",
    ]


# ---------------------------------------------------------------------------
# ③ The round attribution of the artifacts
# ---------------------------------------------------------------------------
#
# The artifact directory of a VP is reused across rounds, so the audit
# copy of the verdict (``ci_output.json``) is overwritten by the next
# round -- and a conclusion that quotes it is quoting whatever the last
# round left there.  The per-round copies are what makes a conclusion
# about "this round" checkable.


def _plan_dir(tmp_path: Path, *rounds: int) -> Path:
    """A plan directory with a context pack for each of the rounds."""
    plan_dir = tmp_path / "plans" / "plan-1"
    context = plan_dir / "context"
    context.mkdir(parents=True)
    for number in rounds:
        (context / f"round-{number}.json").write_text(
            json.dumps({"round_no": number}), encoding="utf-8",
        )
    return plan_dir


def _artifact_dir(plan_dir: Path) -> Path:
    """Where the runner writes the audit copy of the verdict."""
    return plan_dir / "vp_artifacts"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_audit_copy_keeps_one_file_per_round(tmp_path):
    """The live verdict is the newest round's; the round's own copy is
    what a later round cannot take away."""
    plan_dir = _plan_dir(tmp_path, 1, 2, 3)

    ci_runner._write_artifact(
        _artifact_dir(plan_dir), "VP-014",
        {"vp_id": "VP-014", "exit_code": 143, "timed_out": False,
         "duration_seconds": 255.92},
    )

    target = _artifact_dir(plan_dir) / "VP-014"
    assert (target / "ci_output.json").is_file()
    copy = target / "ci_output.round-3.json"
    assert copy.is_file()
    record = _load(copy)
    assert record["exit_code"] == 143
    assert record["timed_out"] is False
    assert record["duration_seconds"] == 255.92


def test_the_round_copy_says_which_round_it_belongs_to(tmp_path):
    """A file called ``round-3`` that does not say it is round 3 is a
    file a reader has to guess about."""
    plan_dir = _plan_dir(tmp_path, 1, 2, 3)

    ci_runner._write_artifact(
        _artifact_dir(plan_dir), "VP-014", {"exit_code": 0},
    )

    record = _load(_artifact_dir(plan_dir) / "VP-014" / "ci_output.round-3.json")
    assert record["round"] == 3
    assert record["round_source"] == "context_pack"


def test_an_earlier_round_s_copy_is_not_overwritten(tmp_path):
    """The failure this exists to prevent: a later round replaces the
    evidence of an earlier one."""
    plan_dir = _plan_dir(tmp_path, 1, 2)
    target = _artifact_dir(plan_dir) / "VP-014"
    target.mkdir(parents=True)
    earlier = target / "ci_output.round-1.json"
    earlier.write_text(
        json.dumps({"round": 1, "exit_code": 143, "marker": "the earlier round"}),
        encoding="utf-8",
    )
    before = earlier.read_bytes()

    ci_runner._write_artifact(
        _artifact_dir(plan_dir), "VP-014", {"exit_code": 0},
    )

    assert earlier.read_bytes() == before
    assert (target / "ci_output.round-2.json").is_file()


def test_a_second_run_of_the_same_round_does_not_destroy_the_first(tmp_path):
    """Two runs in one round are two runs; neither erases the other."""
    plan_dir = _plan_dir(tmp_path, 1)
    target = _artifact_dir(plan_dir) / "VP-014"

    ci_runner._write_artifact(
        _artifact_dir(plan_dir), "VP-014", {"exit_code": 143, "run": 1},
    )
    ci_runner._write_artifact(
        _artifact_dir(plan_dir), "VP-014", {"exit_code": 0, "run": 2},
    )

    first = _load(target / "ci_output.round-1.json")
    assert first["run"] == 1
    second = _load(target / "ci_output.round-1.1.json")
    assert second["run"] == 2
    # the live verdict is the newest run -- that is what it is for
    assert _load(target / "ci_output.json")["run"] == 2


def test_the_live_verdict_stays_the_one_full_ci_verdict(tmp_path):
    """``ci_output.json`` keeps its path and its meaning; the copies
    are copies.

    The copy is the live verdict plus the two bookkeeping keys and by
    nothing else: the writer adds no key of its own (2026-11-09), so a
    reader of the copy is reading the very same run the live verdict
    describes — and can still say which round it belongs to.
    """
    plan_dir = _plan_dir(tmp_path, 1)

    ci_runner._write_artifact(
        _artifact_dir(plan_dir), "VP-014", {"exit_code": 0},
    )

    target = _artifact_dir(plan_dir) / "VP-014"
    live = _load(target / "ci_output.json")
    assert live["exit_code"] == 0
    assert "artifact_role" not in live
    assert "round" not in live
    assert "round_source" not in live
    copy = _load(target / "ci_output.round-1.json")
    # The bookkeeping and the bookkeeping only.
    assert set(copy) - set(live) == {"round", "round_source"}
    assert copy["round"] == 1
    assert copy["round_source"] == "context_pack"
    stripped = {
        key: value for key, value in copy.items()
        if key not in ("round", "round_source")
    }
    assert stripped == live


def test_no_round_copy_is_written_when_the_round_is_unknown(tmp_path):
    """A copy whose name claims a round nobody can name is worse than
    no copy at all."""
    artifact_dir = tmp_path / "vp_artifacts"

    ci_runner._write_artifact(artifact_dir, "VP-014", {"exit_code": 0})

    target = artifact_dir / "VP-014"
    assert (target / "ci_output.json").is_file()
    assert not list(target.glob("ci_output.round-*.json"))
    # The live verdict is the payload the caller gave: the writer
    # invents no round label for a round nobody can name (2026-11-09).
    assert "round" not in _load(target / "ci_output.json")
