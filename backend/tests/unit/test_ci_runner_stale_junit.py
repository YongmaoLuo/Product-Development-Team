"""门禁跑之前，旧一轮的 junit 报告必须先被清掉。

Why this file exists
--------------------
2026-11-09 的 429 复盘：``VP-025`` 的门禁在 **0.04 秒**里以 ``exit 3``
结束（``gh`` 不在门禁的 PATH 里，脚本自己死了），判决却读出了
**11517 项 / 23 个 failure** —— 那些数字来自 7 天前一轮留下的
``vp_artifacts/VP-025/phase-*.xml``。

机制是这样的：``run_ci_verification()`` 只做 ``mkdir(exist_ok=True)``，
然后 :func:`_read_junit_evidence` 直接 ``glob("phase-*.xml")``。目录是
**跨轮复用**的，所以上一轮的报告还躺在那里；这一轮的门禁如果一个报告
都没写出来（因为它自己没跑起来），读到的就全是旧的。判决于是拿一份
过期报告当成了这一轮的证据。

修法只有一句话：**跑门禁之前把上一轮的 ``phase-*.xml`` 删掉。** 门禁
跑完之后目录里剩下的报告，就是这一轮写的；一份都没有，证据就是空的。

What is pinned here
-------------------
1. 清理只删 ``phase-*.xml``：``ci_output.json`` 和别的文件不动。
2. 目录不存在 / 没有目录 → 没有错误（不是失败，是没东西可清）。
3. 某一个文件删不掉 → 其余文件照删，返回第一个错误串。
4. **清理失败不改变判决**：它只进 evidence，status 仍是门禁说的那个。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import verification_ci_runner  # noqa: E402
from verification_ci_runner import (  # noqa: E402
    STATUS_PASSED,
    run_ci_verification,
)


def _vp(vp_id: str, entry: str = "exit 0", **extra) -> Dict:
    vp = {
        "id": vp_id,
        "verification_method": "full_ci",
        "verification_phase": 2,
        "ci_entry": entry,
    }
    vp.update(extra)
    return vp


def _junit_dir(tmp_path: Path, vp_id: str) -> Path:
    """The directory the runner hands to the gate as its junit directory."""
    junit_dir = tmp_path / "vp_artifacts" / vp_id
    junit_dir.mkdir(parents=True, exist_ok=True)
    return junit_dir


# --------------------------------------------------------------------------
# The cleanup itself
# --------------------------------------------------------------------------


def test_clear_stale_junit_reports_deletes_old_phase_files(tmp_path):
    """The old reports go; everything else stays.

    ``keep.txt`` is there to prove the cleanup is *scoped*: only the
    ``phase-*.xml`` files are ours to delete, and the audit copy
    (``ci_output.json``) and anything else in the directory are not.
    """
    junit_dir = tmp_path / "VP-025"
    junit_dir.mkdir()
    (junit_dir / "phase-1.xml").write_text("<testsuite/>", encoding="utf-8")
    (junit_dir / "phase-2.xml").write_text("<testsuite/>", encoding="utf-8")
    (junit_dir / "keep.txt").write_text("not a report", encoding="utf-8")

    error = verification_ci_runner.clear_stale_junit_reports(junit_dir)

    assert error is None, f"the cleanup failed: {error}"
    assert sorted(p.name for p in junit_dir.glob("phase-*.xml")) == []
    assert (junit_dir / "keep.txt").exists() is True


def test_clear_stale_junit_reports_leaves_the_audit_copy_alone(tmp_path):
    """``ci_output.json`` is the verdict's own copy — it is not ours."""
    junit_dir = tmp_path / "VP-025"
    junit_dir.mkdir()
    (junit_dir / "phase-1.xml").write_text("<testsuite/>", encoding="utf-8")
    (junit_dir / "ci_output.json").write_text("{}", encoding="utf-8")

    verification_ci_runner.clear_stale_junit_reports(junit_dir)

    assert (junit_dir / "ci_output.json").exists() is True
    assert (junit_dir / "phase-1.xml").exists() is False


def test_clear_stale_junit_reports_on_no_directory_is_not_an_error(tmp_path):
    """Nothing to clear is not a failure.

    ``None`` means "this run has no junit directory at all", and a
    directory that does not exist means "there is nothing in it".  Both
    are ordinary states, and neither may raise.
    """
    assert verification_ci_runner.clear_stale_junit_reports(None) is None
    assert verification_ci_runner.clear_stale_junit_reports(tmp_path / "does-not-exist") is None


def test_a_file_that_cannot_be_deleted_is_named_and_the_rest_go(tmp_path,
                                                                monkeypatch):
    """One stuck file does not stop the cleanup, and it is named.

    The error string says which file it was and what went wrong, so the
    reader of the evidence does not have to guess.
    """
    junit_dir = tmp_path / "VP-025"
    junit_dir.mkdir()
    for name in ("phase-1.xml", "phase-2.xml", "phase-3.xml"):
        (junit_dir / name).write_text("<testsuite/>", encoding="utf-8")

    original_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == "phase-2.xml":
            raise OSError("boom")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)

    error = verification_ci_runner.clear_stale_junit_reports(junit_dir)

    assert error == "phase-2.xml: OSError: boom", (
        f"the error does not name the file and the failure: {error}"
    )
    assert (junit_dir / "phase-1.xml").exists() is False, (
        "the file before the stuck one was not deleted"
    )
    assert (junit_dir / "phase-3.xml").exists() is False, (
        "the file after the stuck one was not deleted"
    )


# --------------------------------------------------------------------------
# The runner purges before the gate runs
# --------------------------------------------------------------------------


def test_run_ci_verification_purges_before_running(tmp_path):
    """A report left over from a previous round is gone after the run.

    This is the 429 case in miniature: the artifact directory already
    holds a report, the gate runs and exits 0 without writing a report of
    its own, and the evidence must not be able to read the old one.
    """
    junit_dir = _junit_dir(tmp_path, "VP-901")
    stale = junit_dir / "phase-1.xml"
    stale.write_text("<testsuite tests='11517'/>", encoding="utf-8")

    result = run_ci_verification(
        _vp("VP-901"), project_dir=tmp_path, artifact_dir=tmp_path / "vp_artifacts",
    )

    assert stale.exists() is False, (
        "the stale report from the previous round is still in the junit "
        "directory — the verdict can read it again"
    )
    assert result.status == STATUS_PASSED


def test_cleanup_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    """A cleanup that blows up is recorded; the verdict is untouched.

    The verdict is the gate's exit code and nothing else — a broken
    cleanup is a fact about the *bookkeeping*, and it goes into the
    evidence, not into the status.
    """
    _junit_dir(tmp_path, "VP-902")
    monkeypatch.setattr(
        verification_ci_runner,
        "clear_stale_junit_reports",
        lambda junit_dir: "x: OSError: boom",
    )

    result = run_ci_verification(
        _vp("VP-902"), project_dir=tmp_path, artifact_dir=tmp_path / "vp_artifacts",
    )

    assert result.evidence["junit_stale_cleanup_error"] == "x: OSError: boom"
    assert result.status == STATUS_PASSED
