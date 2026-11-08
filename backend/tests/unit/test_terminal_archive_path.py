"""The round archive's path must be knowable **before** the archive is written.

Background
----------
A terminal round writes its analysis to a pair of files under
``<plan_dir>/reports/``.  The name of the ``.md`` half is built from the
round number and the UTC second the write happened at, and it steps
aside (``-2``, ``-3``, …) when that name is already taken — an archive
is never overwritten.

The decision card quotes that ``.md`` path as its "完整分析" link.  But
the card is put to the human **before** the archive is written, so the
path cannot be read back from the write: it has to be computed on its
own.  :func:`verification.terminal_decision_session.round_archive_path`
computes the very path :func:`save_round_archive` will write — without
creating a directory or writing a file.

What these tests pin
--------------------
1. **The precomputed path is the written path.**  Same clock reading,
   same name: a caller that computed the path first hands out a link
   that resolves to the file the round actually wrote.
2. **An occupied name steps aside.**  The precomputed path takes the
   same ``-2`` suffix the writer would have taken, so the card's link
   still points at the file the round wrote and not at the older one.
3. **Computing a path writes nothing** — no directory, no file — and a
   path computed without a clock reading is stamped with the current
   UTC second.

2026-11-08: added for the decision card's "完整分析" path (arch
decision point 4: the path is determinable at question time).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from verification import terminal_decision_session as tds  # noqa: E402


#: The clock reading both tests below stamp their names with.
_MOMENT = datetime(2026, 11, 8, 10, 10, 10, tzinfo=timezone.utc)


def _analysis():
    """A well-formed analysis — the six fields the contract asks for."""
    return {
        "context": "第三轮验证停在登录接口",
        "problem": "登录接口返回 500，不是预期结果",
        "evidence": ["VP-3 失败"],
        "recommendation": "revise_criterion",
        "recommendation_reason": "缺陷已定性，重跑不会改变结果",
        "uncertainty": "没有看过服务端日志",
    }


def test_path_matches_the_file_save_round_archive_writes(tmp_path):
    """The path computed first is the path the write lands on."""
    expected = tds.round_archive_path(tmp_path, 4, now=_MOMENT)

    written = tds.save_round_archive(
        tmp_path,
        round_number=4,
        stop_reason="repair_execution_failed",
        question="a question",
        options=["accept_as_failed"],
        default_option="accept_as_failed",
        analysis=_analysis(),
        decision_id="dec-1",
        failed_vp_ids=["VP-3"],
        now=_MOMENT,
    )

    assert written is not None
    assert written == expected
    assert written.exists()
    assert written.parent == tmp_path / "reports"
    assert written.name == "terminal-decision-round-4-20261108T101010Z.md"


def test_path_steps_aside_for_an_existing_archive(tmp_path):
    """An occupied name steps aside — the same ``-2`` the writer takes."""
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "terminal-decision-round-4-20261108T101010Z.md").write_text(
        "an earlier archive", encoding="utf-8"
    )

    path = tds.round_archive_path(tmp_path, 4, now=_MOMENT)

    assert path.name == "terminal-decision-round-4-20261108T101010Z-2.md"


def test_path_steps_aside_for_an_existing_json_half(tmp_path):
    """The machine-readable half occupies the name too — it also steps aside."""
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "terminal-decision-round-4-20261108T101010Z.json").write_text(
        "{}", encoding="utf-8"
    )

    path = tds.round_archive_path(tmp_path, 4, now=_MOMENT)

    assert path.name == "terminal-decision-round-4-20261108T101010Z-2.md"


def test_path_writes_nothing(tmp_path):
    """Computing a path creates no directory and no file."""
    path = tds.round_archive_path(tmp_path, 4, now=_MOMENT)

    assert not (tmp_path / "reports").exists()
    assert not path.exists()


def test_path_without_a_clock_reading_uses_the_current_utc_second(tmp_path):
    """No clock reading given: the current UTC second names the file."""
    before = datetime.now(timezone.utc)

    path = tds.round_archive_path(tmp_path, 4)

    after = datetime.now(timezone.utc)
    assert path.name.startswith("terminal-decision-round-4-")
    assert path.name.endswith(".md")

    # The name carries a UTC stamp of the second the path was computed
    # on — the reading before the call, or the one after it.
    stamped = datetime.strptime(
        path.name[len("terminal-decision-round-4-") : -len(".md")],
        "%Y%m%dT%H%M%SZ",
    ).replace(tzinfo=timezone.utc)
    assert before.replace(microsecond=0) <= stamped <= after
