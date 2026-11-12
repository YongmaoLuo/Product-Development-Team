#!/usr/bin/env python3
"""Reconcile one round's kill evidence into a decidable statement.

What went wrong before this tool existed
----------------------------------------
A conclusion about a fatal signal was drawn from a query that could
never match:

    hits = [r for r in recs if r.get('event') == 'os.killpg'
            and r.get('target') == 68639]

The ledger it was run against has no ``target`` key at all -- its
fields are ``{ts, event, args, pid, ppid, pgid, stack}`` -- so the
expression was always empty, and "the fatal signal left no sender
record" was an artefact of the query rather than a fact about the
ledger.  A conclusion drawn from an empty result is a conclusion drawn
from an absence, and it looks exactly like a conclusion drawn from the
data.

What this tool does instead
---------------------------
It reads a round's evidence and answers, per received signal, one of:

``attributed``
    a send record in the ledger matches the signal -- the receiver's
    process group, the signal number and the second it arrived in --
    and the finding names the ledger file and the line, so a reader can
    go and look at the record.

``sender_inside_repo``
    the sender is a process the repository's audit saw, and the code it
    was running is named.

``sender_in_receiver_group_not_audited``
    the sender was in the receiver's own process group, but the
    repository's audit never covered it (a shell that runs a repository
    script is the usual case: the audit hook lives in python).

``sender_outside_repo_and_group``
    the sender was neither in the repository nor in the receiver's
    process group.

``unable_to_attribute``
    the evidence cannot answer the question -- and the finding says
    **which field is missing**, so the gap is a thing to fix rather
    than a thing to argue about.  This is the answer for a ledger that
    does not carry the fields the search needs, and its wording is
    「无法归因：账本不含 <字段>」.  It is deliberately **not** the
    answer "there is no sender record": that wording turns a missing
    field into a fact about the world.

The controls are told apart too
-------------------------------
The two negative controls describe opposite situations -- a signal to
the whole group (the sentinel dies) and a signal to one pid (the
sentinel lives).  A tool that answered them the same way would answer
neither, so each control gets its own verdict and the two must differ.

Read-only
---------
This tool opens files for reading and writes nothing.  It sends no
signal, to any process, for any reason: a tool that could signal could
create the very evidence it is asked to explain.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: The signal that arrived and could not be attributed, because the
#: evidence does not carry the fields the search needs.
VERDICT_UNABLE = "unable_to_attribute"

#: The sender is a process the repository's audit saw.
VERDICT_INSIDE_REPO = "sender_inside_repo"

#: The sender was in the receiver's own process group, but the
#: repository's audit never covered it.
VERDICT_IN_GROUP = "sender_in_receiver_group_not_audited"

#: The sender was neither in the repository nor in the receiver's
#: process group.
VERDICT_OUTSIDE = "sender_outside_repo_and_group"

#: The control's signal reached the whole process group.
VERDICT_GROUP_SIGNAL = "group_signal"

#: The control's signal reached one pid only.
VERDICT_SINGLE_PID = "single_pid_signal"

#: The control contradicts itself and cannot be read as evidence.
VERDICT_CONTROL_INCONSISTENT = "control_inconsistent"

#: The fields a send record must carry for the search to be possible
#: at all.  The order is the order the reason names them in.
#:
#: They are the explicit fields: what was signalled (``target_pid`` /
#: ``target_pgid`` / ``target_is_group``), with which signal
#: (``signal``), and by whom (``sender_pid`` / ``sender_is_self``).
#: A ledger that carries none of them -- the shape of the earlier
#: rounds, ``{ts, event, args, pid, ppid, pgid, stack}`` -- cannot
#: answer the question, and the target is never guessed from ``args``.
REQUIRED_SEND_FIELDS = (
    "target_pgid",
    "target_pid",
    "signal",
    "sender_pid",
    "sender_is_self",
    "target_is_group",
)

#: How a gap in the ledger is worded.  The field is named, because a
#: missing field is a thing to fix rather than a thing to argue about.
UNABLE_REASON = "无法归因：账本不含 {fields}"

#: The event names a send record may carry -- the ones this module
#: writes, and the ones the older ledgers wrote.
SENT_EVENTS = ("signal.sent", "os.kill", "os.killpg")

#: The event name a received record carries.
RECEIVED_EVENT = "signal.received"

#: Directories a repository walk does not descend into.
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pdt",
    ".pytest_cache", ".mypy_cache", "dist", "build",
})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _signal_name(number: Optional[int]) -> str:
    """The name of a signal number ("SIGTERM"), or a best effort."""
    if number is None:
        return ""
    try:
        return signal_name_of(int(number))
    except Exception:  # noqa: BLE001 - a name is a convenience
        return f"SIG{number}"


def signal_name_of(number: int) -> str:
    """The name of a signal number, from the standard library."""
    import signal as _signal

    try:
        return _signal.Signals(int(number)).name
    except (ValueError, TypeError):
        return f"SIG{number}"


def signal_number_of(name: str) -> int:
    """The number of a signal name ("SIGTERM", "TERM" or "15")."""
    text = str(name).strip()
    if not text:
        raise ValueError("no signal name given")
    try:
        return int(text)
    except ValueError:
        pass
    import signal as _signal

    upper = text.upper()
    if not upper.startswith("SIG"):
        upper = "SIG" + upper
    number = getattr(_signal, upper, None)
    if not isinstance(number, int):
        raise ValueError(f"unknown signal: {name}")
    return int(number)


def _epoch_of(record: Dict[str, Any]) -> Optional[float]:
    """The moment a record was written, as an epoch float."""
    for key in ("ts_epoch", "received_at_epoch"):
        value = record.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    value = record.get("ts")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def _iso(epoch: Optional[float]) -> str:
    """An epoch float as an ISO stamp, or an empty string."""
    if epoch is None:
        return ""
    try:
        return datetime.fromtimestamp(float(epoch)).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _as_int(value: Any) -> Optional[int]:
    """An int, or ``None`` for anything that is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and float(value).is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _stack_text(record: Dict[str, Any]) -> str:
    """The stack of a record, as text -- the ledgers disagree on the
    shape (a string, or a list of lines)."""
    stack = record.get("stack")
    if isinstance(stack, str):
        return stack
    if isinstance(stack, (list, tuple)):
        return " <- ".join(str(part) for part in stack)
    return ""


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def load_ledger(path: Path) -> Dict[str, Any]:
    """Read one ledger file: its records, with the line each is on."""
    records: List[Dict[str, Any]] = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append({
                "ledger": str(path),
                "line": number,
                "record": record,
            })
    return {"path": str(path), "records": records}


def is_sent(record: Dict[str, Any]) -> bool:
    """Is this record about a signal this repository **sent**?"""
    event = str(record.get("event") or "")
    if event in SENT_EVENTS:
        return True
    kind = str(record.get("kind") or "")
    return kind in ("kill", "killpg")


def is_received(record: Dict[str, Any]) -> bool:
    """Is this record about a signal this process **received**?"""
    return str(record.get("event") or "") == RECEIVED_EVENT


def missing_send_fields(ledgers: Sequence[Dict[str, Any]]) -> List[str]:
    """The fields the search needs that no send record carries.

    This is the check that keeps a query that cannot match from
    answering anything: when the field is missing, the answer is
    "unable to attribute", not "there is no sender record".
    """
    seen = set()
    for ledger in ledgers:
        for entry in ledger["records"]:
            record = entry["record"]
            if is_sent(record):
                seen.update(record.keys())
    return [field for field in REQUIRED_SEND_FIELDS if field not in seen]


def sent_records(ledgers: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every send record of every ledger, with where it came from."""
    entries: List[Dict[str, Any]] = []
    for ledger in ledgers:
        for entry in ledger["records"]:
            if is_sent(entry["record"]):
                entries.append(entry)
    return entries


def received_records(ledgers: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every received record of every ledger, with where it came from."""
    entries: List[Dict[str, Any]] = []
    for ledger in ledgers:
        for entry in ledger["records"]:
            if is_received(entry["record"]):
                entries.append(entry)
    return entries


def sender_pids(ledgers: Sequence[Dict[str, Any]]) -> List[int]:
    """The pids the repository's audit saw send something."""
    pids = set()
    for entry in sent_records(ledgers):
        pid = _as_int(entry["record"].get("sender_pid"))
        if pid is None:
            pid = _as_int(entry["record"].get("pid"))
        if pid is not None:
            pids.add(pid)
    return sorted(pids)


def stack_of_pid(ledgers: Sequence[Dict[str, Any]], pid: int) -> str:
    """The stack of the last record a pid wrote."""
    stack = ""
    for entry in sent_records(ledgers):
        record = entry["record"]
        sender = _as_int(record.get("sender_pid"))
        if sender is None:
            sender = _as_int(record.get("pid"))
        if sender == pid:
            stack = _stack_text(record)
    return stack


# ---------------------------------------------------------------------------
# The ps census
# ---------------------------------------------------------------------------


def _parse_row(line: str) -> Optional[Dict[str, Any]]:
    """One row of a ps census: pid, ppid, pgid and the command."""
    parts = line.split(None, 3)
    if len(parts) < 3:
        return None
    pid = _as_int(parts[0])
    ppid = _as_int(parts[1])
    pgid = _as_int(parts[2])
    if pid is None or ppid is None or pgid is None:
        return None
    return {
        "pid": pid,
        "ppid": ppid,
        "pgid": pgid,
        "command": parts[3].strip() if len(parts) > 3 else "",
    }


def parse_census(text: str) -> List[Dict[str, Any]]:
    """The samples of a ps census.

    A sampled census has one section per second, marked by a
    ``--- <epoch> ---`` line; the control artifacts carry one section
    with no epoch.  A row is ``pid ppid pgid command``.
    """
    samples: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("---"):
            if "end of" in stripped:
                current = None
                continue
            match = re.search(r"(\d{6,})", stripped)
            current = {
                "epoch": int(match.group(1)) if match else None,
                "rows": [],
            }
            samples.append(current)
            continue
        row = _parse_row(stripped)
        if row is None:
            continue
        if current is None:
            current = {"epoch": None, "rows": []}
            samples.append(current)
        current["rows"].append(row)
    return samples


def load_census(path: Optional[Path]) -> List[Dict[str, Any]]:
    """The census of a sampled file, or nothing."""
    if path is None:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return parse_census(text)


def census_members(
    samples: Sequence[Dict[str, Any]],
    pgid: int,
    at_epoch: Optional[float] = None,
    window: float = 2.0,
) -> List[Dict[str, Any]]:
    """The members of a process group, as of a moment in time.

    A sample that carries no epoch is used whatever the moment is: it
    is a census of the group at the moment it was taken, and the caller
    has no better information.
    """
    members: List[Dict[str, Any]] = []
    seen = set()
    for sample in samples:
        if at_epoch is not None and sample.get("epoch") is not None:
            if abs(float(sample["epoch"]) - at_epoch) > window:
                continue
        for row in sample.get("rows", []):
            if row["pgid"] != pgid or row["pid"] in seen:
                continue
            seen.add(row["pid"])
            members.append(row)
    return members


def census_command(
    samples: Sequence[Dict[str, Any]], pid: int,
) -> str:
    """The command line of a pid, as the census recorded it."""
    for sample in samples:
        for row in sample.get("rows", []):
            if row["pid"] == pid:
                return row["command"]
    return ""


# ---------------------------------------------------------------------------
# The control artifacts
# ---------------------------------------------------------------------------


def parse_trap(path: Path) -> Dict[str, Any]:
    """Read one control artifact: its facts and its census.

    The artifact is a list of ``key=value`` lines, followed by the
    census of the group at the moment of the signal.
    """
    facts: Dict[str, str] = {}
    census_text: List[str] = []
    in_census = False
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("---"):
            in_census = "end of" not in stripped
            continue
        if in_census:
            census_text.append(line)
            continue
        match = re.match(r"^([a-z_]+)=([A-Za-z0-9_.:\-]*)$", stripped)
        if match:
            facts[match.group(1)] = match.group(2)
    return {
        "artifact": str(path),
        "facts": facts,
        "census": parse_census("\n".join(census_text)),
    }


def control_verdict(facts: Dict[str, str]) -> Tuple[str, str]:
    """The verdict of a control artifact, and why.

    The sentinel is the witness: it carries no trap, so it cannot
    report anything -- but it can die.  Alive before the signal and
    gone after it means the **group** got the signal; alive after it
    means one pid did.  An artifact that says one thing and shows
    another is not evidence, and it is reported as such.
    """
    received_by = str(facts.get("received_by") or "").strip()
    alive_before = str(facts.get("sentinel_alive_before_the_signal") or "").strip()
    alive_after = str(facts.get("sentinel_alive_after_the_signal") or "").strip()
    if received_by == "group":
        expected = "no"
        verdict = VERDICT_GROUP_SIGNAL
        reason = "the whole group received the signal: the sentinel died with it"
    elif received_by in ("single-pid", "single_pid"):
        expected = "yes"
        verdict = VERDICT_SINGLE_PID
        reason = "one pid received the signal: the sentinel in the same group lived"
    else:
        return (
            VERDICT_CONTROL_INCONSISTENT,
            f"the artifact says received_by={received_by or 'nothing'}, "
            "so it cannot be read as a control",
        )
    if alive_before != "yes":
        return (
            VERDICT_CONTROL_INCONSISTENT,
            "the sentinel was not alive before the signal, so its death "
            "after it proves nothing",
        )
    if alive_after != expected:
        return (
            VERDICT_CONTROL_INCONSISTENT,
            f"the artifact says received_by={received_by} but the sentinel "
            f"was {'alive' if alive_after == 'yes' else 'gone'} after the "
            "signal",
        )
    return verdict, reason


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------


def _repo_index(repo_root: Path) -> Dict[str, str]:
    """Every file of the repository, by file name.

    The audit's stack names the file, not its path -- a repository
    path is a fact about the host, and the evidence does not carry
    one.  This index is how a file name becomes a repository path.
    """
    index: Dict[str, str] = {}
    root = Path(repo_root)
    if not root.is_dir():
        return index
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        index.setdefault(path.name, str(relative))
    return index


def _candidate_paths(text: str) -> List[str]:
    """The path-like tokens of a stack or a command line."""
    candidates: List[str] = []
    for token in re.split(r"\s+|<-|->|\|", str(text or "")):
        token = token.strip().strip("'\"")
        if not token or "/" not in token:
            continue
        head = token.split(":", 1)[0]
        if not head or head.startswith("-"):
            continue
        candidates.append(head)
    return candidates


def repo_path_for(text: str, repo_root: Path, index: Optional[Dict[str, str]] = None) -> Optional[str]:
    """The repository-relative path of the code a stack or command names.

    ``None`` when the text names nothing of the repository -- which is
    an answer, and not a weak one: it says the code that ran is not the
    repository's code.
    """
    root = Path(repo_root)
    for candidate in _candidate_paths(text):
        path = Path(candidate)
        if not path.is_absolute():
            if (root / path).is_file():
                return str(path)
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if (root / relative).is_file():
            return str(relative)
    if index is None:
        index = _repo_index(root)
    for candidate in _candidate_paths(text):
        name = Path(candidate).name
        found = index.get(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# The reconciliation
# ---------------------------------------------------------------------------


def _match_sends(
    entries: Sequence[Dict[str, Any]],
    signal_number: int,
    receiver_pgid: int,
    at_epoch: Optional[float],
    window: float,
    member_pids: Iterable[int],
) -> List[Dict[str, Any]]:
    """The send records that match the signal that arrived.

    The match is made by the explicit fields: a send to a group is
    matched by the group it went to, a send to one pid by that pid.
    A record that says neither is read both ways.
    """
    members = set(member_pids)
    matches: List[Dict[str, Any]] = []
    for entry in entries:
        record = entry["record"]
        number = _as_int(record.get("signal"))
        if number is None or number != int(signal_number):
            continue
        epoch = _epoch_of(record)
        if at_epoch is not None and epoch is not None:
            if abs(epoch - at_epoch) > window:
                continue
        target_pgid = _as_int(record.get("target_pgid"))
        target_pid = _as_int(record.get("target_pid"))
        is_group = record.get("target_is_group")
        if is_group is True:
            # the signal went to the whole group, and the receiver is in
            # it -- so it arrived here.
            if target_pgid is not None and target_pgid == int(receiver_pgid):
                matches.append(entry)
            continue
        if is_group is False:
            # the signal went to one pid.
            if target_pid is not None and members and target_pid in members:
                matches.append(entry)
            continue
        if target_pgid is not None and target_pgid == int(receiver_pgid):
            matches.append(entry)
            continue
        if target_pid is not None and members and target_pid in members:
            matches.append(entry)
    return matches


def _delivery_of(record: Dict[str, Any]) -> str:
    """How the send record says the signal was addressed.

    The vocabulary is the one the control artifacts use, so a reader
    can hold the two against each other without translating.
    """
    is_group = record.get("target_is_group")
    if is_group is True:
        return "group"
    if is_group is False:
        return "single-pid"
    return ""


def _delivery_details(delivery: str, is_self: Optional[bool]) -> List[str]:
    """How the signal arrived, said in words."""
    details: List[str] = []
    if delivery == "group":
        details.append("the signal went to the whole group")
    elif delivery == "single-pid":
        details.append("the signal went to one pid")
    if is_self:
        details.append("the sender signalled itself")
    return details


def _sender(
    pid: int,
    ledgers: Sequence[Dict[str, Any]],
    census: Sequence[Dict[str, Any]],
    repo_root: Path,
    index: Optional[Dict[str, str]],
    is_self: Optional[bool] = None,
) -> Dict[str, Any]:
    """What is known about a pid that may have sent a signal."""
    stack = stack_of_pid(ledgers, pid)
    command = census_command(census, pid)
    repo_path = repo_path_for(stack, repo_root, index)
    if repo_path is None:
        repo_path = repo_path_for(command, repo_root, index)
    return {
        "pid": int(pid),
        "command": command,
        "stack": stack,
        "repo_path": repo_path,
        "covered_by_audit": pid in set(sender_pids(ledgers)),
        "is_self": is_self,
    }


def _classify(
    sender: Dict[str, Any],
    receiver_pgid: int,
    census: Sequence[Dict[str, Any]],
) -> Tuple[str, str]:
    """Where the sender was, in the three places a sender can be.

    The order is the order of what the evidence can support: a sender
    the repository's audit saw is a repository sender (and the code it
    ran is named); a sender that was in the receiver's own process group
    but that the audit never covered is the second case -- a shell that
    runs a repository script is the usual one; anything else is
    outside both.
    """
    if sender.get("covered_by_audit"):
        return (
            VERDICT_INSIDE_REPO,
            "the repository's audit recorded this pid sending a signal"
            + (f" from {sender['repo_path']}" if sender.get("repo_path") else ""),
        )
    members = {
        row["pid"]
        for sample in census
        for row in sample.get("rows", [])
        if row["pgid"] == int(receiver_pgid)
    }
    if sender["pid"] in members:
        return (
            VERDICT_IN_GROUP,
            "the sender was in the receiver's own process group, and the "
            "repository's audit never covered it",
        )
    return (
        VERDICT_OUTSIDE,
        "the sender was neither in the repository nor in the receiver's "
        "process group",
    )


def _reconcile_signal(
    *,
    signal_number: int,
    receiver_pgid: int,
    receiver_pid: Optional[int],
    at_epoch: Optional[float],
    ledgers: Sequence[Dict[str, Any]],
    census: Sequence[Dict[str, Any]],
    sender_candidates: Sequence[int],
    repo_root: Path,
    window: float,
    missing: Sequence[str],
    source: str,
) -> Dict[str, Any]:
    """The finding for one received signal."""
    members = census_members(census, receiver_pgid, at_epoch, window)
    member_pids = [row["pid"] for row in members]
    finding: Dict[str, Any] = {
        "kind": "received_signal",
        "source": source,
        "signal": _signal_name(signal_number),
        "signal_number": int(signal_number),
        "received_at": _iso(at_epoch),
        "received_at_epoch": at_epoch,
        "receiver_pgid": int(receiver_pgid),
        "receiver_pid": receiver_pid,
        "receiver_group_members": member_pids,
        "verdict": VERDICT_UNABLE,
        "reason": "",
        "attribution": None,
        "sender": None,
        "delivered_to": "",
        "missing_fields": list(missing),
    }

    if missing:
        finding["reason"] = UNABLE_REASON.format(fields="、".join(missing))
        return finding

    matches = _match_sends(
        sent_records(ledgers), signal_number, receiver_pgid, at_epoch,
        window, member_pids,
    )
    if matches:
        entry = matches[-1]
        record = entry["record"]
        pid = _as_int(record.get("sender_pid"))
        if pid is None:
            pid = _as_int(record.get("pid"))
        is_self = record.get("sender_is_self")
        if not isinstance(is_self, bool):
            is_self = None
        sender = (
            _sender(pid, ledgers, census, repo_root, None, is_self)
            if pid else None
        )
        verdict, reason = _classify(sender, receiver_pgid, census) if sender else (
            VERDICT_UNABLE,
            "the matching send record does not name the pid that sent it",
        )
        finding["verdict"] = verdict
        finding["delivered_to"] = _delivery_of(record)
        finding["reason"] = "; ".join(
            [reason] + _delivery_details(finding["delivered_to"], is_self)
        )
        finding["attribution"] = {
            "ledger": entry["ledger"],
            "line": entry["line"],
            "record": record,
        }
        finding["sender"] = sender
        return finding

    candidates = list(sender_candidates)
    if not candidates:
        covered = set(sender_pids(ledgers))
        candidates = [
            row["pid"] for row in members
            if row["pid"] not in covered
        ]
    if not candidates:
        finding["reason"] = (
            "unable to attribute: no candidate sender was named and the "
            "searched ledger holds no matching send record"
        )
        return finding

    index = _repo_index(repo_root)
    described = []
    for pid in candidates:
        sender = _sender(pid, ledgers, census, repo_root, index)
        verdict, reason = _classify(sender, receiver_pgid, census)
        described.append({"sender": sender, "verdict": verdict, "reason": reason})
    first = described[0]
    finding["verdict"] = first["verdict"]
    finding["reason"] = first["reason"]
    finding["sender"] = first["sender"]
    finding["candidates"] = described
    return finding


def _received_fields(record: Dict[str, Any]) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """The receiver, its group and the signal of a received record.

    The current shape names them ``receiver_pid`` / ``receiver_pgid``;
    the ledgers of the earlier rounds wrote the same facts as ``pid``
    and ``pgid``, and put the signal number in ``target``.  Those
    records are evidence, so they are read.
    """
    pid = _as_int(record.get("receiver_pid"))
    if pid is None:
        pid = _as_int(record.get("pid"))
    pgid = _as_int(record.get("receiver_pgid"))
    if pgid is None:
        pgid = _as_int(record.get("pgid"))
    number = _as_int(record.get("signal"))
    if number is None:
        number = _as_int(record.get("target"))
    return pid, pgid, number


def reconcile(
    *,
    ledgers: Sequence[Path] = (),
    ps_samples: Optional[Path] = None,
    traps: Sequence[Path] = (),
    receiver_pgid: Optional[int] = None,
    receiver_pid: Optional[int] = None,
    signal_number: int = 15,
    at_epoch: Optional[float] = None,
    sender_pids: Sequence[int] = (),
    repo_root: Optional[Path] = None,
    round_label: str = "",
    window: float = 2.0,
) -> Dict[str, Any]:
    """Reconcile one round's evidence into a report.

    The report names the round it reconciles: a conclusion about "the
    run" is worthless without it, because the artifact directory is
    reused across rounds.
    """
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[1]
    loaded = [load_ledger(Path(path)) for path in ledgers]
    census = load_census(ps_samples)
    controls = [parse_trap(Path(path)) for path in traps]
    for control in controls:
        census.extend(control["census"])

    missing = missing_send_fields(loaded)
    findings: List[Dict[str, Any]] = []

    seen_signals = set()
    for entry in received_records(loaded):
        record = entry["record"]
        pid, pgid, number = _received_fields(record)
        if pgid is None:
            continue
        epoch = _epoch_of(record)
        seen_signals.add((pgid, number, epoch))
        findings.append(_reconcile_signal(
            signal_number=number if number is not None else signal_number,
            receiver_pgid=pgid,
            receiver_pid=pid,
            at_epoch=epoch,
            ledgers=loaded,
            census=census,
            sender_candidates=sender_pids,
            repo_root=root,
            window=window,
            missing=missing,
            source=f"{entry['ledger']}:{entry['line']}",
        ))

    if receiver_pgid is not None:
        already = any(
            finding["receiver_pgid"] == int(receiver_pgid)
            and (at_epoch is None
                 or finding["received_at_epoch"] is None
                 or abs(finding["received_at_epoch"] - at_epoch) <= window)
            for finding in findings
        )
        if not already:
            findings.append(_reconcile_signal(
                signal_number=signal_number,
                receiver_pgid=int(receiver_pgid),
                receiver_pid=receiver_pid,
                at_epoch=at_epoch,
                ledgers=loaded,
                census=census,
                sender_candidates=sender_pids,
                repo_root=root,
                window=window,
                missing=missing,
                source="the caller",
            ))

    for control in controls:
        verdict, reason = control_verdict(control["facts"])
        findings.append({
            "kind": "control_trap",
            "artifact": control["artifact"],
            "facts": control["facts"],
            "verdict": verdict,
            "reason": reason,
        })

    signal_findings = [f for f in findings if f["kind"] == "received_signal"]
    control_findings = [f for f in findings if f["kind"] == "control_trap"]
    complete = all(
        finding["verdict"] != VERDICT_UNABLE for finding in signal_findings
    )
    return {
        "tool": "kill_signal_reconciliation",
        "version": 1,
        "round": str(round_label),
        "inputs": {
            "ledgers": [str(path) for path in ledgers],
            "ps_samples": str(ps_samples) if ps_samples else "",
            "traps": [str(path) for path in traps],
            "receiver_pgid": receiver_pgid,
            "receiver_pid": receiver_pid,
            "signal": _signal_name(signal_number),
            "signal_number": int(signal_number),
            "at_epoch": at_epoch,
            "window_seconds": window,
            "repo_root": str(root),
        },
        "ledger_missing_fields": list(missing),
        "findings": findings,
        "verdicts": {
            "received_signal": [f["verdict"] for f in signal_findings],
            "control_trap": [f["verdict"] for f in control_findings],
        },
        "attribution_complete": complete,
    }


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def _text_report(report: Dict[str, Any]) -> str:
    """The report, for a reader rather than for a machine."""
    lines = [
        f"round: {report['round']}",
        f"attribution complete: {report['attribution_complete']}",
    ]
    for finding in report["findings"]:
        if finding["kind"] == "received_signal":
            lines.append(
                "received {signal} at {at} (group {pgid}): {verdict}".format(
                    signal=finding["signal"],
                    at=finding["received_at"] or "an unknown second",
                    pgid=finding["receiver_pgid"],
                    verdict=finding["verdict"],
                )
            )
            lines.append(f"  {finding['reason']}")
            attribution = finding.get("attribution")
            if attribution:
                lines.append(
                    f"  record: {attribution['ledger']}:{attribution['line']}"
                )
        else:
            lines.append(
                f"control {finding['artifact']}: {finding['verdict']}"
            )
            lines.append(f"  {finding['reason']}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """The command line of the tool."""
    parser = argparse.ArgumentParser(
        prog="kill_signal_reconciliation.py",
        description=(
            "Reconcile one round's kill ledger, ps census and control "
            "traps into a decidable statement about who sent a signal."
        ),
    )
    parser.add_argument(
        "--ledger", action="append", default=[], metavar="PATH",
        help="a kill ledger of the round (repeatable)",
    )
    parser.add_argument(
        "--ps-samples", default="", metavar="PATH",
        help="the ps census sampled during the round",
    )
    parser.add_argument(
        "--trap", action="append", default=[], metavar="PATH",
        help="a control artifact of the round (repeatable)",
    )
    parser.add_argument(
        "--receiver-pgid", type=int, default=None,
        help="the process group the signal arrived at",
    )
    parser.add_argument(
        "--receiver-pid", type=int, default=None,
        help="the process that received the signal",
    )
    parser.add_argument(
        "--signal", default="SIGTERM",
        help="the signal that arrived (default: SIGTERM)",
    )
    parser.add_argument(
        "--at", type=float, default=None, metavar="EPOCH",
        help="the second the signal arrived, as an epoch",
    )
    parser.add_argument(
        "--sender-pid", action="append", type=int, default=[], metavar="PID",
        help="a pid suspected of having sent the signal (repeatable)",
    )
    parser.add_argument(
        "--repo-root", default="", metavar="PATH",
        help="the root of the repository the run belongs to",
    )
    parser.add_argument(
        "--round", required=True, metavar="LABEL",
        help="the round the evidence belongs to (it must be written down)",
    )
    parser.add_argument(
        "--window", type=float, default=2.0, metavar="SECONDS",
        help="how far apart a send and a receipt may be (default: 2s)",
    )
    parser.add_argument(
        "--format", choices=("json", "text"), default="json",
        help="the shape of the report (default: json)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="exit 1 when the attribution is incomplete",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the tool.  Never raises."""
    parser = build_parser()
    options = parser.parse_args(list(argv) if argv is not None else None)
    try:
        signal_number = signal_number_of(options.signal)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    repo_root = options.repo_root or str(
        Path(__file__).resolve().parents[1]
    )
    report = reconcile(
        ledgers=[Path(path) for path in options.ledger],
        ps_samples=Path(options.ps_samples) if options.ps_samples else None,
        traps=[Path(path) for path in options.trap],
        receiver_pgid=options.receiver_pgid,
        receiver_pid=options.receiver_pid,
        signal_number=signal_number,
        at_epoch=options.at,
        sender_pids=list(options.sender_pid),
        repo_root=Path(repo_root),
        round_label=options.round,
        window=options.window,
    )
    if options.format == "text":
        print(_text_report(report))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if options.strict and not report["attribution_complete"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
