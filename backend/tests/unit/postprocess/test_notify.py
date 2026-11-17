"""The failure notification: three-part payload, one audit line, redaction.

What this file witnesses
------------------------
The post-processing stage's failure notification is the one message that
leaves the machine, so three things about it have to hold at once:

1. it says exactly three things — the plan, the failed items, the
   evidence path — and nothing else (no CPEA sections);
2. it is delivered **once**: a second attempt for the same plan neither
   reaches the channel again nor appends a second audit line;
3. it carries no credential.

Why the audit line exists
--------------------------
A real server process cannot be handed a Python sink, so "the
notification was sent exactly once" would be unobservable from the
outside — the E2E round would have nothing to assert on.  Therefore
every **actual** delivery leaves one JSONL line behind, in the plan's
own directory: one line per delivery, no more, no fewer.  The file is
the record of a delivery that happened, which is why a *repeated* call
finds the line already there and stays silent.

Why the payload is read back from two places
---------------------------------------------
The three-part claim is asserted against **both** the sink's payload
and the audit line, because they are two copies of one content: a
payload that grew a fourth key would be caught by the sink's copy, and
an audit line that drifted away from the payload would be caught by the
file's copy.  Asserting only one of them would let the other drift.

Why the channel is checked by parsing the source
-------------------------------------------------
"Deliver through the plan's already-bound channel" is a constraint on
the *module*, not on a value: a module that imported ``requests`` (or
spelled out a Feishu / Telegram call) would be a second channel, and a
second channel is a second set of credentials to leak.  So the check
parses the module's own AST and looks at what it imports — a test that
merely greps for a word would be satisfied by a comment.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import postprocess.notify as notify_module
from postprocess.notify import (
    NotifyError,
    notification_log_path,
    notify_failure,
    redact,
)
from tests.fixtures.postprocess.builders import fake_notifier


#: The three keys the failure payload carries — spelled out member by
#: member, because "the keys the code produces" compared against itself
#: witnesses nothing.
PAYLOAD_KEYS = {"plan_id", "failed_items", "evidence_path"}

#: The CPEA section names.  The architecture says the failure
#: notification is a plain three-part message and does not wear a CPEA
#: coat; a payload carrying one of these keys would be that coat.
CPEA_SECTIONS = {"context", "problem", "evidence", "action", "uncertainty"}

#: Modules that would be a second channel — or a second way to reach
#: the network — inside the notification adapter.
NETWORK_MODULES = {
    "requests",
    "httpx",
    "urllib",
    "aiohttp",
    "http",
    "socket",
    "smtplib",
}

#: Channel SDKs / transport modules this stage must not grow.
CHANNEL_MODULES = {
    "feishu",
    "lark",
    "telegram",
    "slack",
    "discord",
    "webhook",
    "bot",
}

#: The four credential shapes the security review named.  Each entry is
#: ``(the secret as it appears, a marker that must survive)``.
SECRET_EXAMPLES = [
    "ghp_AAAABBBBCCCCDDDDEEEE",
    "sk-AAAABBBBCCCCDDDDEEEE",
    "xoxb-AAAABBBBCCCCDDDDEEEE",
    "Bearer AAAABBBBCCCCDDDDEEEE",
]


def _read_lines(plan_id: str) -> List[str]:
    """The audit lines recorded for one plan (empty when there are none)."""
    path = notification_log_path(plan_id)
    if not path.exists():
        return []
    return [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_failure_payload_is_three_parts():
    """The notification says three things — and wears no CPEA coat."""
    capture = fake_notifier()

    notify_failure(
        "p1",
        ["open-pr"],
        "plans/p1/postprocess_progress.json",
        sink=capture,
    )

    assert capture.count == 1
    payload = capture.calls[0]["payload"]
    assert set(payload) == PAYLOAD_KEYS, (
        f"expected exactly {sorted(PAYLOAD_KEYS)}, got {sorted(payload)}"
    )
    assert len(payload) == 3, f"expected 3 keys, got {len(payload)}"
    assert not (set(payload) & CPEA_SECTIONS), (
        f"the failure notification must not carry CPEA sections, got "
        f"{sorted(set(payload) & CPEA_SECTIONS)}"
    )
    assert payload["plan_id"] == "p1"
    assert payload["failed_items"] == ["open-pr"]
    assert payload["evidence_path"] == "plans/p1/postprocess_progress.json"


def test_audit_line_carries_the_same_three_keys():
    """The audit line is the payload — same keys, same values."""
    capture = fake_notifier()

    notify_failure(
        "p1",
        ["open-pr"],
        "plans/p1/postprocess_progress.json",
        sink=capture,
    )

    lines = _read_lines("p1")
    assert len(lines) == 1, f"expected one audit line, got {lines}"
    recorded = json.loads(lines[0])
    assert set(recorded) == PAYLOAD_KEYS, (
        f"the audit line must carry exactly {sorted(PAYLOAD_KEYS)}, got "
        f"{sorted(recorded)}"
    )
    assert recorded == capture.calls[0]["payload"], (
        "the audit line and the delivered payload must be the same content"
    )


def test_failure_sent_once():
    """A second call for the same plan neither sends nor appends again."""
    capture = fake_notifier()
    evidence = "plans/p1/postprocess_progress.json"

    first = notify_failure("p1", ["open-pr"], evidence, sink=capture)
    second = notify_failure("p1", ["open-pr"], evidence, sink=capture)

    assert capture.count == 1, (
        f"the channel must be reached once, got {capture.count}"
    )
    assert first["already_sent"] is False
    assert second["already_sent"] is True, (
        "a repeated call must report that the notification was already sent"
    )
    lines = _read_lines("p1")
    assert len(lines) == 1, f"expected one audit line, got {lines}"


def test_notification_log_has_one_line_per_send():
    """The audit line count equals the number of actual deliveries."""
    capture = fake_notifier()

    for plan_id in ("p1", "p2", "p3"):
        notify_failure(
            plan_id,
            ["open-pr"],
            f"plans/{plan_id}/postprocess_progress.json",
            sink=capture,
        )

    assert capture.count == 3
    total_lines = sum(len(_read_lines(plan_id)) for plan_id in ("p1", "p2", "p3"))
    assert total_lines == capture.count, (
        f"expected {capture.count} audit lines, got {total_lines}"
    )
    for plan_id in ("p1", "p2", "p3"):
        assert len(_read_lines(plan_id)) == 1


def test_notification_log_path_points_into_the_plan_directory():
    """The audit file lives next to the plan's own artifacts."""
    path = notification_log_path("p1")

    assert path.name == "postprocess_notifications.jsonl"
    assert path.parent.name == "p1"


def test_secret_patterns_redacted():
    """The four credential shapes never survive redaction."""
    for secret in SECRET_EXAMPLES:
        stdout = f"command output: {secret}\nsecond line\n"

        cleaned = redact(stdout)

        assert secret not in cleaned, (
            f"the credential {secret!r} survived redaction"
        )
        assert "<redacted>" in cleaned, (
            f"the credential {secret!r} must be replaced by <redacted>"
        )


def test_redaction_keeps_the_text_around_the_credential():
    """Redaction removes the credential, not the line it sits on."""
    cleaned = redact("before ghp_AAAABBBBCCCCDDDDEEEE after")

    assert cleaned == "before <redacted> after"


def test_delivered_payload_is_redacted():
    """What leaves the process carries no credential either."""
    capture = fake_notifier()

    notify_failure(
        "p1",
        ["push ghp_AAAABBBBCCCCDDDDEEEE"],
        "plans/p1/postprocess_progress.json",
        sink=capture,
    )

    payload = capture.calls[0]["payload"]
    assert "ghp_AAAABBBBCCCCDDDDEEEE" not in json.dumps(payload, ensure_ascii=False)


def test_missing_sink_raises_notify_error():
    """Without a channel the failure is loud — and nothing is written."""
    with pytest.raises(NotifyError):
        notify_failure(
            "p1",
            ["open-pr"],
            "plans/p1/postprocess_progress.json",
        )

    assert _read_lines("p1") == [], (
        "a notification that was never delivered must leave no audit line"
    )
    assert not notification_log_path("p1").exists(), (
        "a notification that was never delivered must leave no audit file"
    )


def test_a_failing_sink_leaves_no_audit_line():
    """A delivery that blew up is not a delivery — nothing is recorded."""

    class ExplodingSink:
        def notify(self, plan_id, kind, payload):
            raise RuntimeError("channel down")

    with pytest.raises(NotifyError):
        notify_failure(
            "p1",
            ["open-pr"],
            "plans/p1/postprocess_progress.json",
            sink=ExplodingSink(),
        )

    assert _read_lines("p1") == [], (
        "a failed delivery must not leave an audit line behind"
    )


def test_no_channel_implementation_created():
    """The adapter grows no channel of its own."""
    source = Path(notify_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: Dict[str, List[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.setdefault(alias.name.split(".")[0], []).append(
                    alias.name
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import cannot reach a channel SDK.
                continue
            imported.setdefault((node.module or "").split(".")[0], []).append(
                node.module or ""
            )

    offenders = sorted(set(imported) & (NETWORK_MODULES | CHANNEL_MODULES))
    assert not offenders, (
        f"the notification adapter must not implement or import a channel: "
        f"{offenders}"
    )


def test_audit_file_is_not_world_readable():
    """The audit file is written private, like the other plan artifacts."""
    capture = fake_notifier()

    notify_failure(
        "p1",
        ["open-pr"],
        "plans/p1/postprocess_progress.json",
        sink=capture,
    )

    mode = notification_log_path("p1").stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_return_value_carries_the_payload_and_the_flag():
    """The caller learns what was sent and whether it was a repeat."""
    capture = fake_notifier()
    evidence = "plans/p1/postprocess_progress.json"

    result: Any = notify_failure("p1", ["open-pr"], evidence, sink=capture)

    assert set(result) == PAYLOAD_KEYS | {"already_sent"}, (
        f"expected the payload plus the delivery flag, got {sorted(result)}"
    )
    assert result["plan_id"] == "p1"
    assert result["failed_items"] == ["open-pr"]
    assert result["evidence_path"] == evidence
    assert result["already_sent"] is False


def test_failed_items_given_as_a_string_are_refused():
    """A bare string is a caller's mistake — it must not be split up."""
    capture = fake_notifier()

    with pytest.raises(NotifyError):
        notify_failure(
            "p1",
            "open-pr",
            "plans/p1/postprocess_progress.json",
            sink=capture,
        )

    assert capture.count == 0
    assert _read_lines("p1") == [], (
        "a refused notification must leave no audit line behind"
    )


def test_plan_id_must_not_name_a_path():
    """The plan id becomes a path segment — it may not point elsewhere."""
    with pytest.raises(NotifyError):
        notification_log_path("../../etc")

    with pytest.raises(NotifyError):
        notify_failure(
            "../escape",
            ["open-pr"],
            "plans/p1/postprocess_progress.json",
            sink=fake_notifier(),
        )