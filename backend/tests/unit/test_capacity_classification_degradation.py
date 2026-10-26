"""A classification says *what* it decided; the degradation event says *how*.

``_classify_provider_failure`` folds a live usage reading into the
classification, and the reading is folded in *ahead* of the prose the
provider sent. When the reading cannot be had, the prose is all that is
left — and until now nothing said so: the answer came back as
``"exhausted"`` with no trace that no meter had been asked. Two
different situations produced the same line: "the meter said the budget
is gone" and "there was no meter, and the wording said so". Only the
second one is a degradation, and only the second one is worth an
operator's attention.

So the classification keeps its answer and grows a report:

  * the event ``capacity_classified_without_live_reading`` is written
    when the classification had no live reading to stand on;
  * its data names the provider, the reason the reading was missing and
    the bucket that was decided (``provider`` / ``reason`` / ``bucket``);
  * the reason comes from the readers' own accessors, so it says
    *which* reading was missing: ``file_missing`` for the file-backed
    table, ``provider_absent`` for a table that was read and does not
    hold this provider, and the quota leg's own reasons for CC Switch.

Two things the event must never do:

  * change the answer — the bucket is the same with and without the
    line, and the folding of the live reading is untouched;
  * name a provider it cannot attribute the failure to — an empty
    provider means the failure belongs to nobody, and an event about
    "somebody" is worse than no event.

Nothing here contacts CC Switch or reads the payload file: the reason
is what the last read already said.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, List, Tuple

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import provider_order  # noqa: E402
from coding_tool import ApiError, _classify_provider_failure  # noqa: E402
from provider_order import (  # noqa: E402
    REASON_FILE_MISSING,
    REASON_PROVIDER_ABSENT,
    REASON_QUOTA_UNREACHABLE,
    load_provider_5h_usage,
)

#: The vendor's spent-plan refusal — the text that reads as an
#: exhaustion, so a classification with no reading behind it has to
#: fall back to it.
EXHAUSTION_TEXT = (
    "已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。(2056)"
)

#: The event an operator greps for.
EVENT = "capacity_classified_without_live_reading"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Recorder:
    """A structured sink: the shape the execution logger has.

    ``warning(event, message, **kwargs)`` — the same shape
    ``coding_tool`` already writes its structured events to.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, dict]] = []

    def warning(self, event: str, message: str, **kwargs: Any) -> None:
        self.calls.append((event, message, kwargs))

    @property
    def events(self) -> List[str]:
        return [call[0] for call in self.calls]

    @property
    def data(self) -> List[dict]:
        return [call[2].get("data") or {} for call in self.calls]


def _write_payload(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _table(*provider_ids: str) -> dict:
    """A payload whose ``providers`` table holds the given ids."""
    return {
        "version": 1,
        "providers": {
            pid: {"five_hour_remaining_pct": 50.0} for pid in provider_ids
        },
    }


@pytest.fixture(autouse=True)
def _no_reading_state(monkeypatch):
    """Each test starts with no reading at all, and stays hermetic.

    The reason of the last read lives in module state that
    :func:`provider_order.cache_clear` does not touch, so it is reset
    here — otherwise a test that ran earlier in the session could hand
    this one a reason of its own. The quota map is stubbed empty, so no
    CC Switch is contacted and no reading exists for any provider.
    """
    provider_order._record_usage_read(None, frozenset())
    provider_order._record_quota_read(None, frozenset())
    monkeypatch.setattr(
        provider_order, "load_provider_quota_map", lambda *a, **k: {}
    )
    yield
    provider_order._record_usage_read(None, frozenset())
    provider_order._record_quota_read(None, frozenset())


def _missing_reading(tmp_path: Path) -> None:
    """Stage "the reading file is not there"."""
    load_provider_5h_usage(tmp_path / "not-there.json")


def _absent_provider(tmp_path: Path) -> None:
    """Stage "the table was read and does not hold this provider"."""
    target = tmp_path / "provider-order.json"
    _write_payload(target, _table("vendor-a-pro"))
    load_provider_5h_usage(target)


# ---------------------------------------------------------------------------
# 1. The classification still works, and says that it was degraded
# ---------------------------------------------------------------------------


def test_missing_reading_still_classifies_by_text(caplog, tmp_path):
    """No reading: the prose decides, and the line says so."""
    _missing_reading(tmp_path)

    with caplog.at_level(logging.WARNING):
        kind = _classify_provider_failure(
            ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax"
        )

    assert kind == "exhausted"
    assert EVENT in caplog.text
    assert "provider=MiniMax" in caplog.text
    assert f"reason={REASON_FILE_MISSING}" in caplog.text


def test_the_event_carries_provider_reason_and_bucket(tmp_path):
    """The structured record names the provider, the reason and the bucket."""
    _missing_reading(tmp_path)
    recorder = _Recorder()

    _classify_provider_failure(
        ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax", logger=recorder
    )

    assert recorder.events == [EVENT]
    data = recorder.data[0]
    assert data["provider"] == "MiniMax"
    assert data["reason"] == REASON_FILE_MISSING
    assert data["bucket"] == "exhausted"


# ---------------------------------------------------------------------------
# 2. The report does not change the answer
# ---------------------------------------------------------------------------


def test_log_does_not_change_bucket(tmp_path):
    """The bucket is the same with a sink and without one."""
    _missing_reading(tmp_path)
    recorder = _Recorder()

    with_sink = _classify_provider_failure(
        ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax", logger=recorder
    )
    without_sink = _classify_provider_failure(
        ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax"
    )

    assert with_sink == without_sink == "exhausted"
    assert recorder.events == [EVENT]


def test_a_live_reading_still_wins(monkeypatch):
    """The folding of the live reading is untouched by the reporting.

    A meter that says the budget is gone still outranks the prose, and
    the reporting does not get a vote on that.
    """

    class _Quota:
        exhausted = True

    monkeypatch.setattr(
        provider_order,
        "load_provider_quota_map",
        lambda *a, **k: {"MiniMax": _Quota()},
    )

    kind = _classify_provider_failure(
        ApiError("slow down (2062)", status="429"), "MiniMax"
    )

    assert kind == "exhausted"


# ---------------------------------------------------------------------------
# 3. Nothing is reported that cannot be attributed
# ---------------------------------------------------------------------------


def test_no_provider_no_event(caplog, tmp_path):
    """A failure that belongs to no provider names no provider."""
    _missing_reading(tmp_path)
    recorder = _Recorder()

    with caplog.at_level(logging.WARNING):
        kind = _classify_provider_failure(
            ApiError(EXHAUSTION_TEXT, status="429"), logger=recorder
        )

    assert kind == "exhausted"
    assert recorder.calls == []
    assert EVENT not in caplog.text


# ---------------------------------------------------------------------------
# 4. The module logger is the fallback
# ---------------------------------------------------------------------------


def test_logger_none_falls_back(caplog, tmp_path):
    """``logger=None`` means the module logger, not silence."""
    _missing_reading(tmp_path)

    with caplog.at_level(logging.WARNING):
        kind = _classify_provider_failure(
            ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax"
        )

    assert kind == "exhausted"
    assert EVENT in caplog.text
    assert "provider=MiniMax" in caplog.text


# ---------------------------------------------------------------------------
# 5. "Absent" and "unreadable" are not one reason
# ---------------------------------------------------------------------------


def test_provider_absent_is_distinct_in_the_log(caplog, tmp_path):
    """A table that was read and lacks the provider is not a missing file.

    The two are different operator problems — a producer that has not
    written the file, and a producer that has written one without this
    provider in it — so the reason in the log must tell them apart.
    """
    _missing_reading(tmp_path)
    with caplog.at_level(logging.WARNING):
        _classify_provider_failure(
            ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax"
        )
    missing_line = caplog.text
    caplog.clear()

    _absent_provider(tmp_path)
    with caplog.at_level(logging.WARNING):
        _classify_provider_failure(
            ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax"
        )
    absent_line = caplog.text

    assert f"reason={REASON_FILE_MISSING}" in missing_line
    assert f"reason={REASON_PROVIDER_ABSENT}" in absent_line
    assert REASON_FILE_MISSING != REASON_PROVIDER_ABSENT
    assert f"reason={REASON_PROVIDER_ABSENT}" not in missing_line


# ---------------------------------------------------------------------------
# 6. Which reader's reason is reported
# ---------------------------------------------------------------------------


def test_the_quota_reason_is_the_fallback(monkeypatch, tmp_path):
    """The table is there and holds the provider: the quota leg answers.

    Nothing is missing from the file-backed table, so its reason is
    ``None`` — and the quota leg's own reason is what is left to say.
    """
    target = tmp_path / "provider-order.json"
    _write_payload(target, _table("MiniMax"))
    load_provider_5h_usage(target)
    monkeypatch.setattr(
        provider_order,
        "last_quota_read_reason",
        lambda p=None: REASON_QUOTA_UNREACHABLE,
    )
    recorder = _Recorder()

    _classify_provider_failure(
        ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax", logger=recorder
    )

    assert recorder.data[0]["reason"] == REASON_QUOTA_UNREACHABLE


def test_the_table_reason_is_asked_first(monkeypatch, tmp_path):
    """Both readings are degraded: the table's reason is reported.

    The file-backed table is the reading whose absence most often
    leaves the classification resting on the prose alone, so its reason
    is the one that goes into the event.
    """
    _missing_reading(tmp_path)
    monkeypatch.setattr(
        provider_order,
        "last_quota_read_reason",
        lambda p=None: REASON_QUOTA_UNREACHABLE,
    )
    recorder = _Recorder()

    _classify_provider_failure(
        ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax", logger=recorder
    )

    assert recorder.data[0]["reason"] == REASON_FILE_MISSING


# ---------------------------------------------------------------------------
# 7. The reason comes from the readers, not from a new read
# ---------------------------------------------------------------------------


def test_no_new_read_is_made(monkeypatch):
    """The report asks the accessors, not the disk and not the network.

    A second read would be a second answer to a question the readers
    have already answered, and it would cost a file read or a request
    to CC Switch on a path that runs on every refused dispatch. The one
    quota read the classifier always made is still the only one.
    """
    quota_calls: List[int] = []
    usage_calls: List[int] = []

    def _quota_map(*_args: Any, **_kwargs: Any) -> dict:
        quota_calls.append(1)
        return {}

    def _usage(*_args: Any, **_kwargs: Any) -> dict:
        usage_calls.append(1)
        return {}

    monkeypatch.setattr(provider_order, "load_provider_quota_map", _quota_map)
    monkeypatch.setattr(provider_order, "load_provider_5h_usage", _usage)

    kind = _classify_provider_failure(
        ApiError(EXHAUSTION_TEXT, status="429"), "MiniMax"
    )

    assert kind == "exhausted"
    assert usage_calls == []
    assert len(quota_calls) == 1
