"""门禁判不了的时候，判决里不能带着上一轮的失败指纹。

Why this file exists
--------------------
2026-11-09 的 429 复盘：``VP-025`` 的门禁在 **0.04 秒**里以 ``exit 3``
结束（门禁自己没跑起来），判决却读出了 7 天前一轮留下的 **11517 项 /
23 个 failure**。机制有两段：

1. 计划任务 1 已经落地 —— 跑门禁之前先 :func:`clear_stale_junit_reports`
   把上一轮的 ``phase-*.xml`` 删掉。**但清理会失败**：目录不可写
   （VP-003 探针场景 D 用真实的 ``chmod 500`` 复现过）、文件被占着、
   只读挂载。清理失败时旧报告还在原地。
2. 计划任务 2（标注）当时记为 ``deferred: upstream_failed:1``，**从未
   执行**：``_read_junit_evidence`` 没有新鲜度参数，读者 ``glob`` 到
   什么就读什么，于是旧报告又被当成了这一轮的证据；evidence 里也没有
   任何一句说「本轮没有产出 junit」。
   **这两样现在都有了**：读者按 mtime 过滤（``not_before``），并且在
   本轮没有产出报告时写明「本轮未产出 junit」；本文件钉的就是这两样。

结果就是：``junit_phases`` / ``failed_cases`` 非空 →
``blocking_failures`` 非空 → 门禁说「机器答不了」的 exit 3 被改判成
``gate_failed``。**「门禁判不了」被写成了「交付物失败」**，违反架构的
两条硬约束（INCONCLUSIVE 时不得引用旧 junit；标注必须发生在生产路径上）。

What is pinned here
-------------------
1. **新鲜度**：比门禁开始得还早的报告不算这一轮的证据。
2. **stat 失败按旧文件处理**，记进 ``junit_error``，绝不抛异常。
3. **清理失败 + 门禁 exit 3** → 判决是 INCONCLUSIVE，不是 FAILED；
   evidence 不带任何旧的失败指纹。清理失败有两种：删不掉**并报错**，
   和删不掉**也不报错**（no-op）—— 后者只有 mtime 能挡。
4. **INCONCLUSIVE 轮次不带失败指纹**，并写明「本轮未产出 junit」。
5. **没有 artifact 目录**（也就没有 junit 目录）→ 同样写明这一点。
6. **新鲜的报告照旧说话**（负控）：门禁 exit 3 但报告里的失败不是墙钟
   形状 → 仍然 FAILED —— 证明上面几条不是把判据废掉。
7. **读不出来的报告等于本轮未产出**：畸形 / 零字节的报告不是这一轮的产出
   （``junit_fresh`` 为假、evidence 写明「本轮未产出 junit」），而判决仍
   由退出码决定 —— 读不出报告不把「判不了」写成交付物失败。报告**文件**
   存在这件事仍由 ``junit_available`` 记着（两个键可以同时为真）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Dict

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import verification_ci_runner  # noqa: E402
from verification_ci_runner import (  # noqa: E402
    STATUS_FAILED,
    STATUS_INCONCLUSIVE,
    STATUS_PASSED,
    run_ci_verification,
)

#: 门禁判不了时它自己说的话（真实门禁 summary 的形状）。
GATE_INCONCLUSIVE = """#!/bin/bash
echo '=== gate summary ==='
echo 'phase 1/3 (parallel):             INCONCLUSIVE (the gate budget was exhausted)'
echo 'host measurability:               OK (sleep(0.05)=0.051s, loadavg=3.10)'
echo 'gate budget: 7200s'
echo 'gate elapsed: 7200s'
echo 'gate budget exhausted in: 1/3 (parallel)'
echo 'gate: INCONCLUSIVE (exit 3)'
exit 3
"""

#: 门禁判不了，但它把自己写下的失败留在了报告里。
#:
#: ``{message}`` 是那条失败的消息 —— 墙钟形状的失败仍然是「机器的」，
#: 不是墙钟形状的失败仍然是「代码的」。
GATE_INCONCLUSIVE_WITH_REPORT = """#!/bin/bash
mkdir -p "$PDT_GATE_JUNIT_DIR"
cat > "$PDT_GATE_JUNIT_DIR/phase-1.xml" <<'XML'
<testsuites><testsuite name="pytest" tests="1" failures="1" errors="0" skipped="0">
<testcase classname="tests.unit.test_widget" name="test_widget">
<failure message="{message}">trace</failure>
</testcase></testsuite></testsuites>
XML
echo '=== gate summary ==='
echo 'phase 1/3 (parallel):             INCONCLUSIVE (the gate budget was exhausted)'
echo 'host measurability:               OK (sleep(0.05)=0.051s, loadavg=3.10)'
echo 'gate budget: 7200s'
echo 'gate elapsed: 7200s'
echo 'gate budget exhausted in: 1/3 (parallel)'
echo 'gate: INCONCLUSIVE (exit 3)'
exit 3
"""

#: 一个跑完了、退出码 0 的门禁，它写的报告却是坏的。
#:
#: 形状照 :data:`GATE_INCONCLUSIVE_WITH_REPORT`（往 ``$PDT_GATE_JUNIT_DIR``
#: 写一份 ``phase-1.xml``，再打印门禁自己的总结），只有两处不同：报告是
#: 坏的，而且门禁**判得了**（退出码 0）。
#:
#: 退出码 0 是这里的关键。如果门禁自己说「判不了」，那条 INCONCLUSIVE 分支
#: 本来就会写下「本轮未产出 junit」，用例就证明不了新鲜度判据在起作用。
#: 门禁说「过了」，却留下一份读不出来的报告 —— 这时候「本轮未产出」只能由
#: 新鲜度判据说。
#:
#: ``printf '%s'`` 而不是 heredoc：空报告要写成**零字节**的文件，heredoc
#: 至少会落一个换行。
GATE_WITH_BROKEN_REPORT = """#!/bin/bash
mkdir -p "$PDT_GATE_JUNIT_DIR"
printf '%s' '{report}' > "$PDT_GATE_JUNIT_DIR/phase-1.xml"
echo '=== gate summary ==='
echo 'phase 1/3 (parallel):             passed'
echo 'host measurability:               OK (sleep(0.05)=0.051s, loadavg=3.10)'
echo 'gate: PASS (exit 0)'
exit 0
"""

#: 一份坏掉的报告：开头就没写完，不是 XML。
MALFORMED_REPORT = "<testsuites><testsuite"

#: 门禁判不了，而它写下的那份报告是坏的。
#:
#: 形状照 :data:`GATE_INCONCLUSIVE_WITH_REPORT`（同样的总结、同样的退出码
#: 3），只有报告本身换了 —— 这一份读不出来。用来钉住两件事：读不出来的
#: 报告不是这一轮的产出（``junit_fresh`` 为假），而「判不了」仍然是
#: 「判不了」，不会被写成交付物失败。
GATE_INCONCLUSIVE_WITH_BROKEN_REPORT = """#!/bin/bash
mkdir -p "$PDT_GATE_JUNIT_DIR"
printf '%s' '{report}' > "$PDT_GATE_JUNIT_DIR/phase-1.xml"
echo '=== gate summary ==='
echo 'phase 1/3 (parallel):             INCONCLUSIVE (the gate budget was exhausted)'
echo 'host measurability:               OK (sleep(0.05)=0.051s, loadavg=3.10)'
echo 'gate budget: 7200s'
echo 'gate elapsed: 7200s'
echo 'gate budget exhausted in: 1/3 (parallel)'
echo 'gate: INCONCLUSIVE (exit 3)'
exit 3
"""

#: 一个不是墙钟形状的失败 —— 它是代码的，机器忙不忙都得算失败。
DETERMINISTIC_MESSAGE = "AssertionError: 1 + 1 == 3"

#: 一个墙钟形状的失败 —— 它是机器的，门禁说判不了就是判不了。
WALL_CLOCK_MESSAGE = "service did not become ready within 90s"

#: 一份 7 天前的报告 —— 2026-11-09 那一轮留下的东西。
STALE_AGE_SECONDS = 7 * 24 * 3600

#: 一份报告里的一条失败。
_STALE_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1" \
time="0.123" timestamp="2026-10-31T00:00:00" hostname="gate">
    <testcase classname="tests.unit.test_widget" name="test_widget" time="0.001">
      <failure message="AssertionError: 1 + 1 == 3">def test_widget():
&gt;       assert 1 + 1 == 3
E       assert 1 + 1 == 3</failure>
    </testcase>
  </testsuite>
</testsuites>
"""


#: 一份最小的报告：两个用例，其中一个失败。
#:
#: 用例名 ``test_a`` 是有意挑的：整份证据里只有它能带来 ``test_a`` 这个
#: 字符串，所以「证据里没有 ``test_a``」就是一句可判定的话 —— 那一条失败
#: 没有被读进来。
MINIMAL_JUNIT_XML = (
    '<testsuites><testsuite tests="2" failures="1" errors="0" skipped="0">'
    '<testcase classname="t" name="test_a">'
    '<failure message="boom">assert 1</failure></testcase>'
    '<testcase classname="t" name="test_b"/></testsuite></testsuites>'
)

#: 门禁判得了（退出码 1），并且在跑的过程中写下了自己的报告。
#:
#: 这份报告是在门禁**运行中**写的，所以它的 mtime 落在门禁开始之后 ——
#: 它是这一轮的证据，读者必须照读。
GATE_FAILING_WITH_REPORT = """#!/bin/bash
mkdir -p "$PDT_GATE_JUNIT_DIR"
cat > "$PDT_GATE_JUNIT_DIR/phase-1.xml" <<'XML'
{report}
XML
exit 1
"""


def _vp(vp_id: str = "VP-903", entry: str = "exit 0", **extra) -> Dict:
    vp = {
        "id": vp_id,
        "verification_method": "full_ci",
        "verification_phase": 2,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


def _gate(tmp_path: Path, body: str, name: str = "gate.sh") -> Path:
    """A gate script that runs *body*."""
    gate = tmp_path / name
    gate.write_text(body, encoding="utf-8")
    gate.chmod(0o755)
    return gate


def _junit_dir(tmp_path: Path, vp_id: str = "VP-903") -> Path:
    """The directory the runner hands to the gate as its junit directory."""
    junit_dir = tmp_path / "vp_artifacts" / vp_id
    junit_dir.mkdir(parents=True, exist_ok=True)
    return junit_dir


def _write_report(path: Path, body: str = _STALE_REPORT, *, age: float = 0.0) -> Path:
    """Write a report and push its mtime *age* seconds into the past."""
    path.write_text(body, encoding="utf-8")
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


# --------------------------------------------------------------------------
# 新鲜度判据
# --------------------------------------------------------------------------


def test_a_report_older_than_the_gate_start_is_not_this_rounds_evidence(tmp_path):
    """The report of a round that is over is not evidence for this one.

    The reader is given the moment the gate started; anything older than
    that belongs to a round that already ended — and a round that already
    ended cannot say anything about this one.
    """
    junit_dir = tmp_path / "VP-903"
    junit_dir.mkdir()
    _write_report(junit_dir / "phase-1.xml", age=STALE_AGE_SECONDS)

    evidence = verification_ci_runner._read_junit_evidence(
        junit_dir, not_before=time.time(),
    )

    assert evidence["junit_phases"] == [], (
        f"a report from seven days ago was read as this round's: "
        f"{evidence['junit_phases']}"
    )
    assert evidence["junit_failures"] == []
    assert evidence["failed_cases"] == []


def test_a_report_written_after_the_gate_start_is_read(tmp_path):
    """The report the gate wrote during the run is this round's evidence."""
    junit_dir = tmp_path / "VP-903"
    junit_dir.mkdir()
    _write_report(junit_dir / "phase-1.xml")

    evidence = verification_ci_runner._read_junit_evidence(
        junit_dir, not_before=time.time() - 5.0,
    )

    assert len(evidence["junit_phases"]) == 1
    assert evidence["junit_phases"][0]["phase"] == 1
    assert evidence["junit_failures"]
    assert evidence["failed_cases"] == ["tests/unit/test_widget.py::test_widget"]


def test_a_report_a_moment_older_than_the_start_is_still_read(tmp_path):
    """One second of slack — the clock is not exact, the gate is not slow.

    A report written a fraction of a second before the runner took its
    timestamp is this round's report; the filesystem's timestamp
    granularity must not decide a verdict.
    """
    junit_dir = tmp_path / "VP-903"
    junit_dir.mkdir()
    _write_report(junit_dir / "phase-1.xml", age=0.5)

    evidence = verification_ci_runner._read_junit_evidence(
        junit_dir, not_before=time.time(),
    )

    assert len(evidence["junit_phases"]) == 1, (
        "a report written half a second earlier was thrown away"
    )


def test_without_a_freshness_bound_every_report_is_read(tmp_path):
    """The bound is optional — a reader without one reads what it finds.

    The freshness rule is a parameter, not a change of the reader's
    meaning: the callers that do not know when the gate started keep the
    old behaviour.
    """
    junit_dir = tmp_path / "VP-903"
    junit_dir.mkdir()
    _write_report(junit_dir / "phase-1.xml", age=STALE_AGE_SECONDS)

    evidence = verification_ci_runner._read_junit_evidence(junit_dir)

    assert len(evidence["junit_phases"]) == 1


def test_a_report_that_cannot_be_measured_is_treated_as_an_old_one(tmp_path,
                                                                   monkeypatch):
    """A report whose mtime cannot be read is an old one — and it says so.

    ``stat`` failing (a vanished file, a permission problem) must not
    raise: evidence must never break a verdict.  Such a report is
    skipped — it cannot be shown to be this round's — and the reason
    goes into ``junit_error``.
    """
    junit_dir = tmp_path / "VP-903"
    junit_dir.mkdir()
    _write_report(junit_dir / "phase-1.xml")

    original_stat = Path.stat

    def stat(self, *args, **kwargs):
        if self.name == "phase-1.xml":
            raise PermissionError("boom")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)

    evidence = verification_ci_runner._read_junit_evidence(
        junit_dir, not_before=time.time(),
    )

    assert evidence["junit_phases"] == []
    assert evidence["junit_failures"] == []
    assert "phase-1.xml" in evidence.get("junit_error", ""), (
        f"the unreadable report was not named: {evidence.get('junit_error')}"
    )


# --------------------------------------------------------------------------
# 生产路径：清理失败 + 门禁判不了
# --------------------------------------------------------------------------


def test_a_stale_report_cannot_decide_an_inconclusive_gate(tmp_path, monkeypatch):
    """The 429 case, in miniature: the cleanup failed, the gate could not
    decide, and the old report must not say the deliverable is broken.

    The report is seven days old and its failure is a plain assertion —
    the shape that is the code's.  If the reader picks it up, the verdict
    becomes ``gate_failed``: "the machine could not measure" is turned
    into "the delivery is defective".  It must stay INCONCLUSIVE.
    """
    junit_dir = _junit_dir(tmp_path)
    _write_report(junit_dir / "phase-1.xml", age=STALE_AGE_SECONDS)
    # The cleanup fails — a directory that cannot be written to, a file
    # that cannot be unlinked.  The old report stays where it is.
    monkeypatch.setattr(
        verification_ci_runner,
        "clear_stale_junit_reports",
        lambda junit_dir: "phase-1.xml: PermissionError: boom",
    )
    gate = _gate(tmp_path, GATE_INCONCLUSIVE)

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"a gate that could not decide was graded as a code failure:\n"
        f"{result.reasons}"
    )
    assert result.evidence["junit_phases"] == [], (
        f"the report of a round seven days old is still in the evidence: "
        f"{result.evidence['junit_phases']}"
    )
    assert result.evidence["junit_failures"] == []
    assert result.evidence["failed_cases"] == [], (
        f"the evidence still carries a failure fingerprint: "
        f"{result.evidence['failed_cases']}"
    )
    assert result.evidence["junit_note"] == "本轮未产出 junit"
    assert result.evidence["junit_fresh"] is False


def test_an_inconclusive_round_carries_no_failure_fingerprint(tmp_path):
    """Even a fresh report does not put a failure into an INCONCLUSIVE round.

    The round could not be decided, so there is no failure to point at.
    A fingerprint left in the evidence would make the repair loop treat
    an undecidable round as a defect it has seen before.
    """
    gate = _gate(
        tmp_path, GATE_INCONCLUSIVE_WITH_REPORT.format(
            message=WALL_CLOCK_MESSAGE,
        ),
    )

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.status == STATUS_INCONCLUSIVE
    assert result.evidence["failed_cases"] == [], (
        f"an undecidable round carries a failure fingerprint: "
        f"{result.evidence['failed_cases']}"
    )
    assert result.evidence["junit_failures"] == []
    assert result.evidence["junit_phases"] == []
    assert result.evidence["junit_note"] == "本轮未产出 junit"


def test_a_round_without_a_junit_directory_says_so(tmp_path):
    """No artifact directory → no junit directory → the evidence says so.

    The note is what tells a reader that "no failures" means "nothing was
    measured", not "nothing failed".
    """
    result = run_ci_verification(
        _vp(entry="exit 0", ci_timeout_seconds=60), project_dir=tmp_path,
    )

    assert result.status == STATUS_PASSED
    assert result.evidence["junit_note"] == "本轮未产出 junit"
    assert result.evidence["junit_fresh"] is False


# --------------------------------------------------------------------------
# 读不出来的报告 = 本轮没有产出
# --------------------------------------------------------------------------


def test_a_malformed_report_means_this_round_produced_no_junit(tmp_path):
    """A report that cannot be parsed is not a report this round produced.

    The gate passed (exit 0) and left a ``phase-1.xml`` that is not XML.
    The reader catches the parse error, records it in ``junit_error`` and
    moves on — so the round has no phase at all.  The evidence must
    therefore say "本轮未产出 junit" instead of reading the *presence of
    a file* as "the round produced a report".

    Two facts have to stay apart here: ``junit_available`` says a report
    file was there, ``junit_fresh`` says the round produced a readable
    one.  Only the second one decides whether the round measured
    anything.
    """
    gate = _gate(
        tmp_path, GATE_WITH_BROKEN_REPORT.format(report=MALFORMED_REPORT),
    )

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.status == STATUS_PASSED, (
        f"a broken report changed the verdict:\n{result.reasons}"
    )
    assert result.evidence["junit_fresh"] is False, (
        f"a report that cannot be parsed was read as this round's: "
        f"junit_fresh={result.evidence['junit_fresh']!r}"
    )
    # The two keys are not in conflict: the file WAS there, and it said
    # nothing.  ``junit_available`` keeps its upstream meaning here.
    assert result.evidence["junit_available"] is True, (
        f"the report file was not seen at all: "
        f"junit_available={result.evidence['junit_available']!r}"
    )
    assert result.evidence["junit_note"] == "本轮未产出 junit"
    assert result.evidence["junit_phases"] == []
    assert result.evidence["junit_failures"] == []
    assert result.evidence["failed_cases"] == []
    assert "phase-1.xml" in result.evidence.get("junit_error", ""), (
        f"the broken report was not named: "
        f"{result.evidence.get('junit_error')}"
    )


def test_an_empty_report_means_this_round_produced_no_junit(tmp_path):
    """A report of zero bytes is the same case: a file, but no report.

    The reader must not raise on it, must name it in ``junit_error``, and
    must not let its presence pass for a report the round produced.
    """
    gate = _gate(tmp_path, GATE_WITH_BROKEN_REPORT.format(report=""))

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.status == STATUS_PASSED, (
        f"an empty report changed the verdict:\n{result.reasons}"
    )
    assert result.evidence["junit_fresh"] is False, (
        f"a zero-byte report was read as this round's: "
        f"junit_fresh={result.evidence['junit_fresh']!r}"
    )
    assert result.evidence["junit_available"] is True, (
        f"the report file was not seen at all: "
        f"junit_available={result.evidence['junit_available']!r}"
    )
    assert result.evidence["junit_note"] == "本轮未产出 junit"
    assert result.evidence["junit_phases"] == []
    assert result.evidence["junit_failures"] == []
    assert result.evidence["failed_cases"] == []
    assert "phase-1.xml" in result.evidence.get("junit_error", ""), (
        f"the empty report was not named: "
        f"{result.evidence.get('junit_error')}"
    )


def test_a_broken_report_does_not_turn_an_undecidable_gate_into_a_failure(
    tmp_path,
):
    """「判不了」仍然是「判不了」—— 读不出来的报告不把它变成失败。

    门禁说它判不了（退出码 3），它留下的报告又是坏的。这样的报告既
    不是这一轮的产出（``junit_fresh`` 为假），也**不是一条失败** —— 它
    什么也没说。于是判决必须停在 INCONCLUSIVE：把它写成交付物失败，等于
    拿一个机器答不了的事实去指控代码。
    """
    gate = _gate(
        tmp_path, GATE_INCONCLUSIVE_WITH_BROKEN_REPORT.format(
            report=MALFORMED_REPORT,
        ),
    )

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"an undecidable gate was graded as a code failure:\n{result.reasons}"
    )
    assert result.evidence["junit_fresh"] is False, (
        f"a report that cannot be parsed was read as this round's: "
        f"junit_fresh={result.evidence['junit_fresh']!r}"
    )
    assert result.evidence["junit_available"] is True
    assert result.evidence["junit_note"] == "本轮未产出 junit"
    assert result.evidence["junit_phases"] == []
    assert result.evidence["junit_failures"] == []
    assert result.evidence["failed_cases"] == []


# --------------------------------------------------------------------------
# 负控：新鲜的报告照旧说话
# --------------------------------------------------------------------------


def test_a_fresh_report_still_decides_a_gate_that_could_not(tmp_path):
    """The negative control: the rule is about *old* reports, not about all.

    The gate wrote its report during this round, and the failure in it is
    a plain assertion — the machine being busy does not explain
    ``1 + 1 == 3``.  The verdict stays FAILED and the reason names the
    case.
    """
    gate = _gate(
        tmp_path, GATE_INCONCLUSIVE_WITH_REPORT.format(
            message=DETERMINISTIC_MESSAGE,
        ),
    )

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.status == STATUS_FAILED, (
        f"a deterministic failure was excused by the host:\n{result.reasons}"
    )
    assert "test_widget" in " ".join(result.reasons), (
        f"the reason does not name the failing case:\n{result.reasons}"
    )
    assert result.evidence["junit_fresh"] is True
    assert "junit_note" not in result.evidence, (
        "the round produced a report — it does not need the note"
    )


def test_a_passing_gate_with_a_fresh_report_keeps_its_evidence(tmp_path):
    """A passing gate's report says how much it ran — that is kept."""
    gate = _gate(
        tmp_path, GATE_INCONCLUSIVE_WITH_REPORT.format(
            message=WALL_CLOCK_MESSAGE,
        ).replace("exit 3", "exit 0"),
    )

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.status == STATUS_PASSED
    assert result.evidence["junit_fresh"] is True
    assert len(result.evidence["junit_phases"]) == 1


# --------------------------------------------------------------------------
# 证据的形状
# --------------------------------------------------------------------------


def test_the_audit_copy_carries_the_note(tmp_path, monkeypatch):
    """The note is written to disk too — the artifact is what is read later."""
    import json

    junit_dir = _junit_dir(tmp_path)
    _write_report(junit_dir / "phase-1.xml", age=STALE_AGE_SECONDS)
    monkeypatch.setattr(
        verification_ci_runner,
        "clear_stale_junit_reports",
        lambda junit_dir: "phase-1.xml: PermissionError: boom",
    )
    gate = _gate(tmp_path, GATE_INCONCLUSIVE)
    artifact = tmp_path / "vp_artifacts"

    run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=artifact,
    )

    payload = json.loads(
        (artifact / "VP-903" / "ci_output.json").read_text(encoding="utf-8")
    )
    assert payload["junit_note"] == "本轮未产出 junit"
    assert payload["junit_fresh"] is False
    assert payload["failed_cases"] == []


def test_the_evidence_names_the_cleanup_failure_separately(tmp_path, monkeypatch):
    """The cleanup failure is a fact about the bookkeeping, and it is kept.

    It does not change the verdict — but a reader has to be able to see
    why the old report was still there.
    """
    junit_dir = _junit_dir(tmp_path)
    _write_report(junit_dir / "phase-1.xml", age=STALE_AGE_SECONDS)
    monkeypatch.setattr(
        verification_ci_runner,
        "clear_stale_junit_reports",
        lambda junit_dir: "phase-1.xml: PermissionError: boom",
    )
    gate = _gate(tmp_path, GATE_INCONCLUSIVE)

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.evidence["junit_stale_cleanup_error"] == (
        "phase-1.xml: PermissionError: boom"
    )
    assert result.status == STATUS_INCONCLUSIVE


# --------------------------------------------------------------------------
# 本轮的新鲜度：判据的两头
# --------------------------------------------------------------------------
#
# 前面那些用例钉住的是「旧报告不算数」。这里钉住的是这条判据的**两头**：
# 清理彻底失效的时候旧报告照样不算数（负控），而本轮真的写了报告的时候
# 汇总照样留着（正控）。少了任何一头，这条判据都可能长成「把所有报告都
# 扔掉」或者「什么报告都算数」。


def test_stale_junit_is_ignored_even_when_cleanup_is_a_noop(tmp_path,
                                                            monkeypatch):
    """清理成了 no-op 也挡不住旧报告 —— 新鲜度判据自己就够用。

    清理会失败，而且失败的时候**不一定**留下痕迹：目录不可写时，删除
    动作可以被悄无声息地跳过，连 ``junit_stale_cleanup_error`` 都没有。
    这时候唯一还能分辨「这份报告是谁的」的，就是它自己的 mtime。

    于是：上一轮的报告（7 天前）留在原地，门禁这一轮判不了（exit 3），
    判决必须停在 INCONCLUSIVE，而证据里不许出现那份报告的失败
    （``test_a``）。否则「机器答不了」就被写成了「交付物失败」。
    """
    artifact = tmp_path / "arts"
    (artifact / "VP-025").mkdir(parents=True)
    old = artifact / "VP-025" / "phase-1.xml"
    old.write_text(MINIMAL_JUNIT_XML, encoding="utf-8")
    stamp = time.time() - STALE_AGE_SECONDS
    os.utime(old, (stamp, stamp))

    # The cleanup does nothing at all — and says nothing at all.
    monkeypatch.setattr(
        verification_ci_runner,
        "clear_stale_junit_reports",
        lambda junit_dir: None,
    )
    gate = _gate(tmp_path, GATE_INCONCLUSIVE)

    result = run_ci_verification(
        _vp(vp_id="VP-025", entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=artifact,
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"a gate that could not decide was graded as a code failure:\n"
        f"{result.reasons}"
    )
    assert result.evidence["junit_fresh"] is False, (
        f"the report of a round seven days old was read as this round's: "
        f"junit_fresh={result.evidence['junit_fresh']!r}"
    )
    assert result.evidence["junit_phases"] == [], (
        f"the report of a round seven days old is still in the evidence: "
        f"{result.evidence['junit_phases']}"
    )
    assert result.evidence["junit_failures"] == []
    assert result.evidence["failed_cases"] == [], (
        f"the evidence still carries a failure fingerprint: "
        f"{result.evidence['failed_cases']}"
    )
    assert result.evidence["junit_note"] == "本轮未产出 junit"
    assert "test_a" not in json.dumps(result.evidence, ensure_ascii=False), (
        "the stale report's failure reached the evidence"
    )


def test_fresh_junit_is_still_reported(tmp_path):
    """正控：本轮真的写了报告，汇总就留着。

    门禁在运行中写下的 ``phase-1.xml`` 是这一轮的证据（2 个用例，1 个
    失败）。读者必须照读它 —— 判据是关于**旧**报告的，不是关于所有报告
    的。判据要是长成「把报告都扔掉」，这一条就会红。
    """
    gate = _gate(
        tmp_path, GATE_FAILING_WITH_REPORT.format(report=MINIMAL_JUNIT_XML),
    )

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.evidence["junit_fresh"] is True, (
        f"the report this round wrote was not read as this round's: "
        f"junit_fresh={result.evidence['junit_fresh']!r}"
    )
    assert len(result.evidence["junit_phases"]) == 1, (
        f"the report this round wrote was dropped: "
        f"{result.evidence['junit_phases']}"
    )
    assert result.evidence["junit_phases"][0]["tests"] == 2
    assert result.evidence["junit_phases"][0]["failures"] == 1
    assert "junit_note" not in result.evidence, (
        "the round produced a report — it does not need the note"
    )


def test_inconclusive_round_does_not_report_a_deliverable_failure(tmp_path):
    """判不了的轮次不报告交付物失败 —— 而且说清楚它没测什么。

    门禁说它判不了（exit 3），它什么也没留下。这样的轮次没有失败可指：
    ``failed_cases`` 必须是空的，而证据要写明「本轮未产出 junit」——
    「没有失败」和「什么都没测」不能读成同一句话。
    """
    gate = _gate(tmp_path, GATE_INCONCLUSIVE)

    result = run_ci_verification(
        _vp(entry=f"bash {gate}", ci_timeout_seconds=60),
        project_dir=tmp_path,
        artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.status == STATUS_INCONCLUSIVE, (
        f"a gate that could not decide was graded as a code failure:\n"
        f"{result.reasons}"
    )
    assert result.evidence["failed_cases"] == [], (
        f"an undecidable round reports a deliverable failure: "
        f"{result.evidence['failed_cases']}"
    )
    assert result.evidence["junit_note"] == "本轮未产出 junit"
    assert result.evidence["junit_fresh"] is False
