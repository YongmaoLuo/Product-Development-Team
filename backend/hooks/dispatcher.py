"""Fan-out dispatcher for a plan's event hooks.

What lives here
---------------
:mod:`hooks.config` decides *who* is asked; this module decides *how*
the question is delivered.  A plan's ``hooks`` array is a set of
destinations, and every one of them is a first-class target: the array
is a fan-out, not a fallback chain.  "A failed, so try B" is not a
thing this module does — "A failed, and B still gets it" is.

Three properties carry the weight, and each of them is a way the
"one bad destination degrades the whole plan" rule can be broken by
accident:

* **One destination's failure is that destination's failure.**  A
  webhook that 404s, a command that exits non-zero, a URL that does not
  resolve, a token that expired — each of those writes one audit line
  and one log line, and changes nothing about the other destinations.
  A plan whose Telegram token was mistyped must not have its human
  review quietly dropped on the floor: that is the failure mode this
  module exists to prevent.
* **Every destination has its own clock.**  A destination that hangs
  gets its own ``timeout_sec`` and is abandoned when it expires.  The
  destinations are dispatched in parallel, so a hung one delays none of
  the others; the dispatch as a whole is bounded by the slowest
  destination's timeout rather than by the sum of all of them.
* **Nothing is retried.**  A retry is a second delivery of the same
  event, and a second delivery of "a human must look at this" is a
  second question asked of a human.  Every destination is attempted
  exactly once.

The payload never becomes a command
-----------------------------------
The ``cmd`` transport runs a command from the plan's own configuration
and hands it the event payload **on stdin**, as JSON.  No field of the
payload is ever interpolated into the command string.  This is not a
style preference: the payload carries decision text, and decision text
carries whatever an operator or a model wrote into it — including a
string like ``; rm -rf /``.  Interpolating it into a shell command is
remote code execution with extra steps, and the only defence that
actually holds is structural: the payload never reaches the command
line at all.

What is not implemented yet
---------------------------
The plan format names five transports: ``cc_connect``, ``http``,
``cmd``, ``feishu`` and ``telegram``.  This module implements the two
that can be verified locally — ``http`` (a POST of the JSON payload)
and ``cmd`` (a process fed the JSON payload on stdin).  The other
three are recognised, recorded as *not implemented* and skipped: they
are announced in the audit line so an operator can see that a
destination existed and was not served, rather than inferring from
silence that nobody was asked.

The report and the audit line
-----------------------------
:func:`dispatch` answers with a :class:`DispatchReport` — three
numbers, because a caller has three questions: did anybody get it
(``delivered``), did somebody hang up on us (``timed_out``), and did
somebody not even get asked (``skipped``).  ``skipped`` counts every
destination that produced no delivery, whether it was never attempted
(unknown transport, missing field, transport not implemented) or the
attempt failed; the ``failed`` attribute is the subset of ``skipped``
that *was* attempted and did not land, for callers that need to tell
"the configuration is broken" from "the network is down".

Alongside the report, every dispatch appends one line to
``<plan_dir>/hooks_audit.jsonl``: the event, the counts, and one
record per destination with its outcome.  The line records *outcomes*,
never the payload: a decision's text can carry source excerpts, and a
hook's URL can carry a credential in its query string.  Targets are
recorded in redacted form (see :func:`_redact`), so a token in a
webhook URL does not become a token in the plan directory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from hooks.config import load_for_plan

logger = logging.getLogger(__name__)

PathLike = Union[str, Path]

#: Name of the per-plan dispatch audit mirror.  It sits next to the
#: plan's other artifacts so it travels with the plan directory.
AUDIT_FILENAME = "hooks_audit.jsonl"

#: The per-destination timeout, in seconds, used when a hook does not
#: declare one of its own.
DEFAULT_TIMEOUT_SEC = 10

#: The transport that POSTs the JSON payload to a URL.
KIND_HTTP = "http"

#: The transport that runs a command with the JSON payload on stdin.
KIND_CMD = "cmd"

#: The transports this module can deliver on today.
SUPPORTED_KINDS: Tuple[str, ...] = (KIND_HTTP, KIND_CMD)

#: The transports the plan format allows but this module does not
#: deliver on yet.  They are recognised rather than treated as
#: mistakes: a destination that exists and was not served is a
#: different fact from a destination that was never configured.
UNIMPLEMENTED_KINDS: Tuple[str, ...] = ("cc_connect", "feishu", "telegram")

#: The outcomes a single destination can have.
OUTCOME_DELIVERED = "delivered"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_SKIPPED = "skipped"
OUTCOME_FAILED = "failed"

#: Upper bound on the destinations dispatched at the same time.  A
#: plan with more hooks than this still fans out; it just does not
#: open an unbounded number of sockets and processes at once.
_MAX_CONCURRENT_DELIVERIES = 8

#: Serialises the append of whole audit lines within this process.
_AUDIT_LOCK = threading.Lock()

#: Patterns whose *value* is a credential.  The value is replaced with
#: ``<redacted>`` before a target is written to the audit line or to
#: the log.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|apikey|access[_-]?key"
    r"|access[_-]?token|auth|authorization|credential|credentials|signature|sig)"
    r"\b\s*[=:]\s*([^\s&;'\"]+)"
)

#: ``Bearer <token>`` — the value follows the keyword by a space.
_BEARER = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]+)")

#: ``scheme://user:password@host`` — the userinfo part of a URL.
_URL_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect handler that declines to redirect.

    ``HTTPRedirectHandler.redirect_request`` returning ``None`` makes
    the opener hand the 3xx response back as the response — which is
    what "a non-2xx response is a failure" means when the destination
    answers with a redirect.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


#: The opener used for every delivery.  It ignores the ambient
#: ``http_proxy`` environment: a hook's destination is chosen by the
#: plan's configuration, and an operator's shell proxy must not
#: silently redirect it somewhere else.  Redirects are *not* followed,
#: so the status this module records is the status of the response the
#: destination actually sent — "not 2xx is a failure" then means what
#: it says.
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirect(),
)


@dataclass(frozen=True)
class DispatchReport:
    """What one dispatch did.

    The three fields are the report the contract names, and they
    partition the destinations: ``delivered`` (the destination got it),
    ``timed_out`` (the destination hung and was abandoned) and
    ``skipped`` (the destination produced no delivery — either it was
    never attempted, or the attempt failed).

    ``failed`` is the subset of ``skipped`` that was attempted and did
    not land.  It is excluded from the repr and from equality so the
    report's public shape stays the three numbers the contract names;
    it is there for a caller that needs to tell "the configuration is
    broken" from "the network is down".
    """

    delivered: int = 0
    timed_out: int = 0
    skipped: int = 0
    failed: int = field(default=0, repr=False, compare=False)


@dataclass
class _Outcome:
    """What happened to one destination."""

    kind: str
    target: str
    outcome: str
    reason: str = ""
    timeout_sec: Optional[int] = None
    status: Optional[int] = None
    exit_code: Optional[int] = None

    def as_audit(self) -> Dict[str, Any]:
        """The destination's record in the audit line."""
        record: Dict[str, Any] = {
            "kind": self.kind,
            "target": self.target,
            "outcome": self.outcome,
        }
        if self.reason:
            record["reason"] = self.reason
        if self.timeout_sec is not None:
            record["timeout_sec"] = self.timeout_sec
        if self.status is not None:
            record["status"] = self.status
        if self.exit_code is not None:
            record["exit_code"] = self.exit_code
        return record


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def dispatch(
    event: str,
    payload: Mapping[str, Any],
    plan_dir: PathLike,
) -> DispatchReport:
    """Deliver ``event`` to every destination the plan configures.

    ``event`` is the outlet's event name (``hitl_decision_created``
    and friends); ``payload`` carries the event's fields.  The two are
    merged into the body the destinations receive, with the event at
    the top level — the outlet's only contract is ``{"event": "...",
    ...}``, and a destination that learned the event's name from
    anywhere else would be reading a different contract.

    The destinations come from ``hooks.config.load_for_plan()``; this
    module never parses ``hitl_config.json`` itself.  They are
    dispatched in parallel, each with its own timeout, and each is
    attempted exactly once.  A destination that fails, hangs, or is not
    deliverable changes nothing about the others.

    The call blocks until every destination has been attempted, so the
    call is bounded by the slowest destination's timeout — not by the
    sum of them, because they run at the same time.  A caller that
    must answer a request quickly (a route handler, a decision
    creation) should therefore run this on a worker thread rather than
    on the request's own thread.

    One line is appended to ``<plan_dir>/hooks_audit.jsonl`` for the
    event, whatever the outcomes were — including the case where the
    plan configures no destinations at all, because "this event was
    dispatched to nobody" is exactly the fact an operator needs.
    """
    body = _body(event, payload)
    hooks = load_for_plan(plan_dir)["hooks"]
    outcomes = _fan_out(hooks, body)
    report = _report(outcomes)
    _log_outcomes(event, outcomes)
    _append_audit(plan_dir, event, report, outcomes)
    return report


# ---------------------------------------------------------------------------
# The body the destinations receive
# ---------------------------------------------------------------------------


def _body(event: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The JSON body every destination receives.

    The event comes first and always wins: a payload that happens to
    carry an ``event`` key of its own does not get to rename the
    outlet's event out from under the destinations.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(
            "the payload of a hook dispatch must be a mapping; got "
            f"{type(payload).__name__}"
        )
    body: Dict[str, Any] = {"event": event}
    body.update(payload)
    body["event"] = event
    return body


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


def _fan_out(
    hooks: List[Dict[str, Any]],
    body: Mapping[str, Any],
) -> List[_Outcome]:
    """Attempt every destination, in parallel, and report each outcome.

    The outcomes come back in the order the plan listed the
    destinations, whatever order they actually finished in — the audit
    line has to be comparable across runs.
    """
    if not hooks:
        return []
    if len(hooks) == 1:
        return [_deliver(hooks[0], body)]

    workers = min(len(hooks), _MAX_CONCURRENT_DELIVERIES)
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="hook-dispatch"
    ) as pool:
        # ``map`` yields the results in the order the destinations
        # were submitted, and ``_deliver`` never raises, so this is
        # the whole fan-out: one entry per destination, no exceptions
        # escaping into the pool.
        return list(pool.map(lambda hook: _deliver(hook, body), hooks))


def _deliver(hook: Mapping[str, Any], body: Mapping[str, Any]) -> _Outcome:
    """Attempt one destination, and never raise.

    A destination that cannot be delivered to — an unknown transport, a
    transport this module does not implement, a missing field — is
    skipped with a reason.  A destination that is attempted and fails
    comes back as a failure.  Neither of them stops the next
    destination.
    """
    kind = _text(hook.get("kind"))
    timeout = _timeout_of(hook.get("timeout_sec"))
    reason = _skip_reason(hook)

    if reason is not None:
        target = ""
        if reason == "unsupported_kind":
            # The destination the plan named is worth recording even
            # though this build cannot deliver on it: "a destination
            # existed and was not served" is not the same fact as "a
            # destination was never configured".
            target = _redact(_text(hook.get("destination")))
        return _Outcome(
            kind=kind, target=target, outcome=OUTCOME_SKIPPED, reason=reason,
        )

    if kind == KIND_HTTP:
        return _deliver_http(_text(hook.get("url")), body, timeout)

    return _deliver_cmd(_text(hook.get("command")), body, timeout)


def _skip_reason(hook: Mapping[str, Any]) -> Optional[str]:
    """Why this destination cannot be attempted, or ``None``.

    The three reasons are the ones the audit line records:

    ``unknown_kind``
        The transport is not one the plan format names.
    ``unsupported_kind``
        The transport is one the plan format names, but this build does
        not deliver on it yet.
    ``missing_field:<field>``
        The transport is one this build delivers on, and the entry does
        not carry the field that transport needs.

    ``None`` means the destination is deliverable: it will be attempted,
    and whether it lands is a question of the network, not of the
    configuration.
    """
    kind = _text(hook.get("kind"))
    if kind == KIND_HTTP:
        return None if _text(hook.get("url")) else "missing_field:url"
    if kind == KIND_CMD:
        return None if _text(hook.get("command")) else "missing_field:command"
    if kind in UNIMPLEMENTED_KINDS:
        return "unsupported_kind"
    return "unknown_kind"


def deliverable_hooks(
    hooks: Sequence[Mapping[str, Any]],
) -> List[Mapping[str, Any]]:
    """The destinations that would actually be attempted.

    A caller that has to know *before* the fan-out whether anybody can
    be asked asks this: it is the same validation
    :func:`_skip_reason` applies, on the same entries, so the two
    cannot drift.

    An empty list means one of two things, and the difference is the
    caller's business: the plan configures no destinations at all, or
    every destination it configures is one this build cannot deliver
    to.  Either way nobody is asked, and a question nobody is asked
    must not be left waiting.
    """
    return [hook for hook in hooks if _skip_reason(hook) is None]


# ---------------------------------------------------------------------------
# The http transport
# ---------------------------------------------------------------------------


def _deliver_http(
    url: str, body: Mapping[str, Any], timeout: int
) -> _Outcome:
    """POST the JSON body to ``url``.

    A response that is not 2xx is a failure.  It is not retried: the
    destination was asked once, and asking twice is how a webhook
    endpoint ends up with the same event twice.
    """
    target = _redact(url)
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = int(response.status)
            response.read()
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is an answer, not an execution failure: the
        # destination was reached and said no.
        return _Outcome(
            kind=KIND_HTTP, target=target, outcome=OUTCOME_FAILED,
            reason="http_status", status=int(exc.code),
        )
    except TimeoutError:
        return _Outcome(
            kind=KIND_HTTP, target=target, outcome=OUTCOME_TIMED_OUT,
            timeout_sec=timeout,
        )
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return _Outcome(
                kind=KIND_HTTP, target=target, outcome=OUTCOME_TIMED_OUT,
                timeout_sec=timeout,
            )
        return _Outcome(
            kind=KIND_HTTP, target=target, outcome=OUTCOME_FAILED,
            reason="unreachable",
        )
    except ValueError:
        return _Outcome(
            kind=KIND_HTTP, target=target, outcome=OUTCOME_FAILED,
            reason="invalid_url",
        )
    except Exception as exc:  # noqa: BLE001 - a delivery must not raise
        return _Outcome(
            kind=KIND_HTTP, target=target, outcome=OUTCOME_FAILED,
            reason=type(exc).__name__,
        )

    if 200 <= status < 300:
        return _Outcome(
            kind=KIND_HTTP, target=target, outcome=OUTCOME_DELIVERED,
            status=status,
        )
    return _Outcome(
        kind=KIND_HTTP, target=target, outcome=OUTCOME_FAILED,
        reason="http_status", status=status,
    )


# ---------------------------------------------------------------------------
# The cmd transport
# ---------------------------------------------------------------------------


def _deliver_cmd(
    command: str, body: Mapping[str, Any], timeout: int
) -> _Outcome:
    """Run ``command`` with the JSON body on stdin.

    The command string comes from the plan's own configuration and is
    run as written.  The body is *never* interpolated into it: it is
    written to the process's stdin, so a payload that happens to
    contain shell metacharacters is data to the command and nothing
    else.

    The command runs in its own session, so a timeout kills the whole
    process group rather than leaving the command's children running
    after the dispatcher has given up on them.
    """
    target = _redact(command)
    data = json.dumps(body).encode("utf-8")

    try:
        process = subprocess.Popen(
            command,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return _Outcome(
            kind=KIND_CMD, target=target, outcome=OUTCOME_FAILED,
            reason="spawn_failed",
        )

    try:
        process.communicate(input=data, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(process)
        try:
            process.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - the child is already gone
            pass
        return _Outcome(
            kind=KIND_CMD, target=target, outcome=OUTCOME_TIMED_OUT,
            timeout_sec=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - a delivery must not raise
        _kill_group(process)
        return _Outcome(
            kind=KIND_CMD, target=target, outcome=OUTCOME_FAILED,
            reason=type(exc).__name__,
        )

    if process.returncode == 0:
        return _Outcome(
            kind=KIND_CMD, target=target, outcome=OUTCOME_DELIVERED,
            exit_code=0,
        )
    return _Outcome(
        kind=KIND_CMD, target=target, outcome=OUTCOME_FAILED,
        reason="exit_code", exit_code=process.returncode,
    )


def _kill_group(process: "subprocess.Popen") -> None:
    """Kill a timed-out command and everything it started.

    The command was started in its own session, so its children share
    its process group and one kill reaches all of them.  A command
    that was left running would keep doing whatever it does — writing
    files, holding locks — long after the dispatcher stopped waiting.
    """
    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        process.kill()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _report(outcomes: List[_Outcome]) -> DispatchReport:
    """Count the outcomes into the report the caller gets."""
    delivered = sum(1 for o in outcomes if o.outcome == OUTCOME_DELIVERED)
    timed_out = sum(1 for o in outcomes if o.outcome == OUTCOME_TIMED_OUT)
    failed = sum(1 for o in outcomes if o.outcome == OUTCOME_FAILED)
    skipped = len(outcomes) - delivered - timed_out
    return DispatchReport(
        delivered=delivered, timed_out=timed_out, skipped=skipped, failed=failed
    )


# ---------------------------------------------------------------------------
# The audit mirror
# ---------------------------------------------------------------------------


def _append_audit(
    plan_dir: PathLike,
    event: str,
    report: DispatchReport,
    outcomes: List[_Outcome],
) -> None:
    """Append one line to the plan's dispatch audit mirror.

    The mirror is a record, not a channel: a plan whose audit line
    cannot be written has still been dispatched, and turning a full
    disk into a failed delivery would be the wrong way round.  The
    failure is therefore logged and swallowed.
    """
    record = {
        "ts": _now_iso(),
        "event": event,
        "delivered": report.delivered,
        "timed_out": report.timed_out,
        "skipped": report.skipped,
        "entries": [outcome.as_audit() for outcome in outcomes],
    }
    path = Path(plan_dir) / AUDIT_FILENAME
    try:
        with _AUDIT_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            # One write of a whole line under "a": two concurrent
            # dispatches cannot tear each other's line in half.
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
    except OSError as exc:
        logger.warning(
            "hooks_audit_unwritable: cannot append the dispatch of %s to %s (%s)",
            event,
            path,
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_outcomes(event: str, outcomes: List[_Outcome]) -> None:
    """Write one log line per destination.

    Exactly one line per destination, so "one destination failed" is
    one line in the log and not a paragraph.  The targets are the
    redacted ones: the service log is read by people who should not
    be handed a webhook's credential.
    """
    for outcome in outcomes:
        if outcome.outcome == OUTCOME_DELIVERED:
            logger.debug(
                "hooks_dispatch_delivered: event=%s kind=%s target=%s",
                event,
                outcome.kind,
                outcome.target,
            )
        elif outcome.outcome == OUTCOME_TIMED_OUT:
            logger.warning(
                "hooks_dispatch_timed_out: event=%s kind=%s target=%s "
                "timeout_sec=%s",
                event,
                outcome.kind,
                outcome.target,
                outcome.timeout_sec,
            )
        elif outcome.outcome == OUTCOME_FAILED:
            logger.warning(
                "hooks_dispatch_failed: event=%s kind=%s target=%s reason=%s",
                event,
                outcome.kind,
                outcome.target,
                outcome.reason,
            )
        else:
            logger.warning(
                "hooks_dispatch_skipped: event=%s kind=%s target=%s reason=%s",
                event,
                outcome.kind,
                outcome.target,
                outcome.reason,
            )


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """A UTC ISO-8601 stamp at second precision, ``Z``-suffixed."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any) -> str:
    """A scalar field as text; anything missing becomes an empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _timeout_of(value: Any) -> int:
    """A per-destination timeout in seconds, or the default.

    ``bool`` is excluded explicitly: ``True`` is an ``int`` in Python,
    and a timeout of one second is not what anybody meant.
    """
    if isinstance(value, bool):
        return DEFAULT_TIMEOUT_SEC
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        seconds = int(value.strip())
        if seconds > 0:
            return seconds
    return DEFAULT_TIMEOUT_SEC


def _redact(text: str) -> str:
    """A target with its credentials removed.

    A webhook URL is routinely ``https://hooks.example.com/t/<token>``
    and the plan directory is not the place for that token.  The
    redaction is deliberately narrow — it removes values that are
    named like credentials — because a target that is redacted into
    uselessness is no better than one that was never recorded.
    """
    if not text:
        return text
    redacted = _URL_USERINFO.sub(r"\1<redacted>@", text)
    redacted = _BEARER.sub("Bearer <redacted>", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(r"\1=<redacted>", redacted)
    return redacted
