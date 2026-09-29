"""Regression tests for :meth:`FeishuNotifier._write_card_snapshot`.

Background (2026-09-08):

Before this snapshot writer existed, the notifier pushed the
rendered card to Feishu / Telegram and discarded the body. When a card
body failed to change in Feishu, the only available diagnostics were
the server log pushes_ok counter and a hash fingerprint. There was no
on-disk artefact an operator (or
this assistant) could read to see what was actually rendered.

After the fix, every successful push writes:

* ``plans/<plan_id>/notifications/last_pushed_card.json`` — latest
  snapshot, atomic JSON write (tmp + rename), full summary blob.
* ``plans/<plan_id>/notifications/pushed_card_history.jsonl`` —
  append-only JSONL history, one line per push.

These tests pin the contract: a snapshot is written on success,
contains the rendered card + metadata, and a second push appends
to the history without clobbering the previous line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _make_notifier(monkeypatch: pytest.MonkeyPatch, repo_root: Path):
    """Build a FeishuNotifier whose plans root is redirected to ``repo_root``.

    The notifier resolves its plans root through
    ``config_paths.resolve_plans_dir()`` on every call (2026-09-14 fix),
    which gives ``PDT_PLANS_DIR`` precedence over the compiled-in
    ``<repo>/plans`` default. Pointing the env var at ``tmp_path`` is
    therefore the supported redirect — patching the module-level
    ``plan_dir_resolver.PLANS_DIR`` no longer has any effect, because
    that constant is not read any more.

    This also keeps the test hermetic: before the env-var hop, a bare
    ``PLANS_DIR`` patch was the only thing standing between these tests
    and the operator's live ``plans/`` tree.
    """
    monkeypatch.setenv("PDT_PLANS_DIR", str(repo_root))

    from notifications.feishu_notifier import FeishuNotifier

    notifier = FeishuNotifier(
        coalesce_seconds=0,
        min_interval_seconds=0,
        backend_base_url="http://127.0.0.1:1",
    )
    return notifier


def test_write_card_snapshot_writes_latest_snapshot(tmp_path, monkeypatch):
    notifier = _make_notifier(monkeypatch, tmp_path)

    plan_id = "plan-test-snap-latest"
    fp = notifier._write_card_snapshot(
        plan_id=plan_id,
        card={"header": {"title": {"content": "OK"}}},
        fingerprint="sha256:abc",
        message_id="om_123",
        transports=["feishu", "telegram"],
        phase="executing",
        verification_status="failed",
        summary={"state": {"current_phase": "executing"}},
    )

    snap_path = (
        tmp_path / plan_id / "notifications" / "last_pushed_card.json"
    )
    assert snap_path.exists(), (
        "Latest snapshot must be written to "
        "plans/<plan_id>/notifications/last_pushed_card.json"
    )
    payload = json.loads(snap_path.read_text(encoding="utf-8"))
    assert payload["plan_id"] == plan_id
    assert payload["fingerprint"] == "sha256:abc"
    assert payload["message_id"] == "om_123"
    assert payload["transports"] == ["feishu", "telegram"]
    assert payload["phase"] == "executing"
    assert payload["verification_status"] == "failed"
    assert payload["card"] == {"header": {"title": {"content": "OK"}}}
    assert payload["summary"] == {"state": {"current_phase": "executing"}}
    assert "timestamp" in payload and payload["timestamp"].endswith("Z")


def test_write_card_snapshot_appends_to_history(tmp_path, monkeypatch):
    notifier = _make_notifier(monkeypatch, tmp_path)

    plan_id = "plan-test-history"
    for i in range(3):
        notifier._write_card_snapshot(
            plan_id=plan_id,
            card={"i": i},
            fingerprint=f"sha256:line{i}",
            message_id=f"om_{i}",
            transports=["feishu"],
            phase="executing",
            verification_status=None,
            summary=None,
        )

    hist_path = (
        tmp_path / plan_id / "notifications" / "pushed_card_history.jsonl"
    )
    assert hist_path.exists()
    lines = hist_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3, (
        "Each push should append one JSONL line — no truncation, "
        "no overwrite of previous lines."
    )
    parsed = [json.loads(line) for line in lines]
    assert [p["fingerprint"] for p in parsed] == [
        "sha256:line0", "sha256:line1", "sha256:line2",
    ]


def test_write_card_snapshot_latest_overwrites_atomically(
    tmp_path, monkeypatch,
):
    """The latest snapshot file must be overwritten atomically — a
    partial write must never leave a half-written file behind that
    an operator could read.
    """
    notifier = _make_notifier(monkeypatch, tmp_path)
    plan_id = "plan-test-atomic"

    notifier._write_card_snapshot(
        plan_id=plan_id,
        card={"v": 1},
        fingerprint="sha256:v1",
        message_id="om_1",
        transports=["feishu"],
        phase="executing",
        verification_status=None,
        summary=None,
    )
    snap_path = (
        tmp_path / plan_id / "notifications" / "last_pushed_card.json"
    )
    assert snap_path.exists()
    # No leftover tmp file from the atomic rename.
    leftover_tmp = list(snap_path.parent.glob("last_pushed_card.json.tmp"))
    assert not leftover_tmp, (
        "Atomic rename must remove the .tmp file after success; "
        f"found: {leftover_tmp}"
    )

    # Second write replaces the file (not appended).
    notifier._write_card_snapshot(
        plan_id=plan_id,
        card={"v": 2},
        fingerprint="sha256:v2",
        message_id="om_2",
        transports=["feishu"],
        phase="verification_running",
        verification_status="running",
        summary={"state": {"current_phase": "verification_running"}},
    )
    payload = json.loads(snap_path.read_text(encoding="utf-8"))
    assert payload["fingerprint"] == "sha256:v2", (
        "Second snapshot must replace the first; the file must not "
        "contain the older v1 payload."
    )


def test_write_card_snapshot_survives_disk_error(
    tmp_path, monkeypatch, caplog,
):
    """A disk error must NOT raise — push is best-effort and the
    notifier worker must continue.
    """
    notifier = _make_notifier(monkeypatch, tmp_path)
    plan_id = "plan-test-disk-error"

    # Force the JSON write to fail by passing a snapshot whose
    # ``card`` field is not JSON-serializable. The history append
    # would also fail, so neither file should appear.
    """
    NOTE: json.dump raises TypeError on non-serializable objects.
    The wrapper catches the exception and logs WARNING without
    propagating. We assert no exception escapes.
    """
    notifier._write_card_snapshot(
        plan_id=plan_id,
        card={"__bad__": set()},  # set() is not JSON-serializable
        fingerprint="sha256:bad",
        message_id=None,
        transports=["feishu"],
        phase="executing",
        verification_status=None,
        summary=None,
    )
    # No file should have been written — but more importantly no
    # exception should have escaped.
    snap_path = (
        tmp_path / plan_id / "notifications" / "last_pushed_card.json"
    )
    # Even a partial write shouldn't leave a half-written file.
    assert not snap_path.exists() or json.loads(
        snap_path.read_text(encoding="utf-8")
    )["fingerprint"] != "sha256:bad", (
        "A failing write must not leave a partial / corrupt "
        "snapshot file behind."
    )