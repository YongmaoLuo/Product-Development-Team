"""Plan-level service declarations (2026-09-18, C1 of the service-ownership work).

Why this module exists
-----------------------
:mod:`service_freshness` and :mod:`service_restart_agent` (both 2026-09-16)
gave the verification round a "VP0" that can revive a dead or stale
service. But they discover *which* ports matter by regex-scraping the
raw text of every VP's ``test_command`` — i.e. the port list is an
emergent property of whatever the planner LLM happened to type. Three
concrete failures followed from that:


  * ``target_url`` was not scanned at all, so a ``ui_validation`` VP
    that only names its port there was invisible to the preflight.
  * Three VPs (VP-019/020/021) each carried ``nohup npx next dev -p
    3000 &`` and ran concurrently, so three servers raced for one
    literal port; the losers' ``curl && break`` wait loop then
    succeeded against the *winner's* server, and Playwright drove a
    process nobody had configured.
  * The freshness rule (listener older than newest source) blessed a
    ``next start`` production build on the port a VP wanted for
    ``next dev``, because "started after the last edit" says nothing
    about *which* server flavour is running.

The fix is a single source of truth: the plan declares its services
**once**, at plan level, and VPs reference them **by name** through
placeholders. This module owns that schema — parsing, validating, and
resolving ``{{svc.<name>.<field>}}`` references. Starting / stopping /
reaping the declared services is :mod:`service_manager`'s job (C2/C3);
this module is pure and side-effect free so it can be unit-tested
without a filesystem or a process table.

Contract
--------
  * :func:`parse_declarations` never raises. A malformed entry is
    dropped with a reason rather than aborting plan generation — the
    round still has 28 VPs to run and an unusable service block must
    not take it down.
  * Dropped declarations are *visible*: :class:`DeclarationParseResult`
    carries both the surviving declarations and the issues, and the
    caller logs/annotates them.
  * A VP that references a service which did not survive parsing is a
    hard error for that VP (it will be BLOCKED, not silently run
    against a port nobody started).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from service_freshness import PROTECTED_PORTS, extract_ports_from_text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Field names a ``{{svc.<name>.<field>}}`` placeholder may ask for.
#: ``health_url`` falls back to ``url`` when the declaration omits it,
#: so a VP can always ask for it without knowing whether the plan
#: bothered to specify a health endpoint.
FIELD_URL = "url"
FIELD_HOST = "host"
FIELD_PORT = "port"
FIELD_HEALTH_URL = "health_url"
RESOLVABLE_FIELDS = frozenset(
    {FIELD_URL, FIELD_HOST, FIELD_PORT, FIELD_HEALTH_URL}
)

#: The loopback host every declared service is reached on. The backend
#: backend and the services it manages are always co-located; a service
#: that must be reachable from elsewhere is out of scope for the
#: preflight (it cannot be started or probed locally anyway).
SERVICE_HOST = "127.0.0.1"

DEFAULT_READY_TIMEOUT = 60
MAX_READY_TIMEOUT = 600

#: Service names are lower-case so a placeholder never depends on the
#: planner's capitalisation, and so ``{{svc.Api.url}}`` and
#: ``{{svc.api.url}}`` cannot silently mean different things.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: ``{{svc.<name>.<field>}}`` — whitespace inside the braces is
#: tolerated because the planner is an LLM and will occasionally emit
#: ``{{ svc.api.port }}``. Whitespace *inside* the dotted path is
#: not: that would make the name ambiguous.
PLACEHOLDER_RE = re.compile(
    r"\{\{\s*svc\.([A-Za-z0-9_.-]+?)\.([A-Za-z0-9_]+)\s*\}\}"
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceDeclaration:
    """One plan-level service the verification round depends on.

    Frozen because the resolved set is shared by every VP in the round
    and the ledger (:mod:`service_manager`) keys on it — a declaration
    mutating mid-round would desynchronise the two.
    """

    name: str
    port: int
    start_cmd: str
    cwd: str = "."
    health_url: str = ""
    ready_timeout_seconds: int = DEFAULT_READY_TIMEOUT
    log_path: str = ""
    #: ``False`` opts a service out of exit-time reaping — for a
    #: long-lived server the operator wants to keep across rounds.
    #: Default is to reap: an backend-spawned process that outlives the
    #: workflow is an orphan, and orphans are what this work exists to
    #: prevent.
    reap_on_exit: bool = True

    @property
    def url(self) -> str:
        return f"http://{SERVICE_HOST}:{self.port}"

    def resolved_health_url(self) -> str:
        return self.health_url or self.url

    def resolve_field(self, field_name: str) -> Optional[str]:
        """The concrete string a placeholder for ``field_name`` expands
        to, or None when the field is not part of the vocabulary."""
        if field_name == FIELD_URL:
            return self.url
        if field_name == FIELD_HOST:
            return SERVICE_HOST
        if field_name == FIELD_PORT:
            return str(self.port)
        if field_name == FIELD_HEALTH_URL:
            return self.resolved_health_url()
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "port": self.port,
            "start_cmd": self.start_cmd,
            "cwd": self.cwd,
            "health_url": self.health_url,
            "ready_timeout_seconds": self.ready_timeout_seconds,
            "log_path": self.log_path,
            "reap_on_exit": self.reap_on_exit,
        }


@dataclass
class DeclarationIssue:
    """A declaration entry (or the block as a whole) that was rejected."""

    index: int
    name: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "name": self.name, "reason": self.reason}


@dataclass
class DeclarationParseResult:
    declarations: List[ServiceDeclaration] = field(default_factory=list)
    issues: List[DeclarationIssue] = field(default_factory=list)
    #: True when the plan carried a ``services`` key at all, even an
    #: empty/broken one. ``service_freshness.extract_service_ports``
    #: keys its "authoritative vs. legacy-regex" branch on this rather
    #: than on ``declarations`` — an explicitly empty block means the
    #: planner was asked and declared nothing, which must NOT silently
    #: fall back to scraping ports out of command text.
    block_present: bool = False

    def by_name(self) -> Dict[str, ServiceDeclaration]:
        return {d.name: d for d in self.declarations}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "services": [d.to_dict() for d in self.declarations],
            "issues": [i.to_dict() for i in self.issues],
            "block_present": self.block_present,
        }


# ---------------------------------------------------------------------------
# Parsing + validation
# ---------------------------------------------------------------------------


def _coerce_int(value: Any) -> Optional[int]:
    """Accept ``8080`` and ``"8080"`` (the LLM emits both)."""
    if isinstance(value, bool):  # bool is an int subclass — reject first
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _validate_cwd(raw: Any, project_dir: Path) -> Tuple[Optional[str], str]:
    """Return ``(relative_cwd, "")`` or ``(None, reason)``.

    The cwd must stay inside the project — a declaration pointing at
    ``/`` or ``../../..`` would let the service starter execute an
    arbitrary command outside the round's blast radius.
    """
    text = str(raw if raw is not None else ".").strip() or "."
    candidate = Path(text)
    if candidate.is_absolute():
        resolved = candidate
    else:
        resolved = Path(project_dir) / candidate
    try:
        resolved = resolved.resolve()
        root = Path(project_dir).resolve()
    except OSError as exc:  # pragma: no cover - defensive
        return None, f"cwd could not be resolved: {exc}"
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, f"cwd {text!r} escapes the project directory"
    rel = resolved.relative_to(root)
    return (str(rel) or "."), ""


def _validate_log_path(raw: Any, project_dir: Path) -> Tuple[Optional[str], str]:
    """Same containment rule as :func:`_validate_cwd`; empty is fine
    (the starter then picks its own path under the project's tmp/)."""
    if raw is None:
        return "", ""
    text = str(raw).strip()
    if not text:
        return "", ""
    return _validate_cwd(text, project_dir)


def _parse_one(
    entry: Any, index: int, project_dir: Path
) -> Tuple[Optional[ServiceDeclaration], Optional[DeclarationIssue]]:
    def _reject(reason: str, name: str = "") -> Tuple[None, DeclarationIssue]:
        return None, DeclarationIssue(index=index, name=name, reason=reason)

    if not isinstance(entry, dict):
        return _reject(f"entry is {type(entry).__name__}, expected an object")

    name = str(entry.get("name") or "").strip().lower()
    if not name:
        return _reject("missing 'name'")
    if not _NAME_RE.match(name):
        return _reject(
            "name must match [a-z0-9][a-z0-9_-]* (placeholders are "
            "case-sensitive and dotted, so the name cannot contain dots)",
            name,
        )

    port = _coerce_int(entry.get("port"))
    if port is None:
        return _reject("missing or non-numeric 'port'", name)
    if not (1 <= port <= 65535):
        return _reject(f"port {port} is out of range", name)
    if port in PROTECTED_PORTS:
        # Protected ports are the backend runtime's own control plane. The
        # 2026-09-07 incident killed the backend on :8000 this way —
        # never let a plan declare one as "its" service.
        return _reject(
            f"port {port} belongs to the backend runtime and must not be "
            f"declared as a plan service",
            name,
        )

    start_cmd = str(entry.get("start_cmd") or "").strip()
    if not start_cmd:
        return _reject("missing 'start_cmd'", name)

    cwd, cwd_reason = _validate_cwd(entry.get("cwd"), project_dir)
    if cwd is None:
        return _reject(cwd_reason, name)

    log_path, log_reason = _validate_log_path(entry.get("log_path"), project_dir)
    if log_path is None:
        return _reject(f"log_path: {log_reason}", name)

    health_url = str(entry.get("health_url") or "").strip()
    if health_url and not re.match(r"^https?://", health_url):
        return _reject(
            f"health_url {health_url!r} is not an http(s) URL", name
        )

    timeout = _coerce_int(entry.get("ready_timeout_seconds"))
    if timeout is None:
        timeout = DEFAULT_READY_TIMEOUT
    timeout = max(1, min(timeout, MAX_READY_TIMEOUT))

    reap = entry.get("reap_on_exit")
    reap_on_exit = True if reap is None else bool(reap)

    return (
        ServiceDeclaration(
            name=name,
            port=port,
            start_cmd=start_cmd,
            cwd=cwd,
            health_url=health_url,
            ready_timeout_seconds=timeout,
            log_path=log_path,
            reap_on_exit=reap_on_exit,
        ),
        None,
    )


def parse_declarations(
    plan_data: Any, project_dir: Any
) -> DeclarationParseResult:
    """Parse + validate ``plan_data["services"]``.

    Never raises. Duplicate names are rejected beyond the first
    occurrence (two services on one name would make the placeholder
    ambiguous and the ledger key collide). Two services on the *same
    port* are also rejected — that is the VP-019/020/021 race in
    declaration form.
    """
    result = DeclarationParseResult()
    if not isinstance(plan_data, dict):
        return result

    raw = plan_data.get("services")
    if raw is None:
        return result
    result.block_present = True

    if not isinstance(raw, list):
        result.issues.append(
            DeclarationIssue(
                index=-1,
                name="",
                reason=(
                    f"'services' is {type(raw).__name__}, expected a list"
                ),
            )
        )
        return result

    seen_names: set[str] = set()
    seen_ports: Dict[int, str] = {}
    for index, entry in enumerate(raw):
        decl, issue = _parse_one(entry, index, Path(project_dir))
        if issue is not None:
            result.issues.append(issue)
            continue
        assert decl is not None  # _parse_one returns exactly one of the two
        if decl.name in seen_names:
            result.issues.append(
                DeclarationIssue(
                    index=index,
                    name=decl.name,
                    reason="duplicate service name",
                )
            )
            continue
        if decl.port in seen_ports:
            result.issues.append(
                DeclarationIssue(
                    index=index,
                    name=decl.name,
                    reason=(
                        f"port {decl.port} is already declared by "
                        f"service {seen_ports[decl.port]!r}"
                    ),
                )
            )
            continue
        seen_names.add(decl.name)
        seen_ports[decl.port] = decl.name
        result.declarations.append(decl)

    return result


# ---------------------------------------------------------------------------
# Placeholder resolution
# ---------------------------------------------------------------------------


def iter_placeholders(text: str) -> Iterator[Tuple[str, str, str]]:
    """Yield ``(raw_match, service_name, field_name)`` for every
    placeholder in ``text``."""
    if not isinstance(text, str):
        return
    for match in PLACEHOLDER_RE.finditer(text):
        yield match.group(0), match.group(1).lower(), match.group(2).lower()


def resolve_text(
    text: Any, declarations: Dict[str, ServiceDeclaration]
) -> Tuple[str, List[str]]:
    """Substitute every resolvable placeholder in ``text``.

    Returns ``(resolved_text, unresolved_raw_placeholders)``.
    Unresolvable placeholders are left **verbatim** — replacing them
    with an empty string would turn a broken reference into a command
    that looks syntactically fine and quietly checks nothing, which is
    the exact failure class the acceptance-command guard exists to
    stop.
    """
    if not isinstance(text, str):
        return text, []

    unresolved: List[str] = []

    def _sub(match: re.Match) -> str:
        name = match.group(1).lower()
        field_name = match.group(2).lower()
        decl = declarations.get(name)
        if decl is None or field_name not in RESOLVABLE_FIELDS:
            unresolved.append(match.group(0))
            return match.group(0)
        value = decl.resolve_field(field_name)
        if value is None:  # pragma: no cover - RESOLVABLE_FIELDS guards this
            unresolved.append(match.group(0))
            return match.group(0)
        return value

    return PLACEHOLDER_RE.sub(_sub, text), unresolved


#: VP string fields a placeholder may appear in. ``target_url`` is a
#: first-class citizen here — it was the field the old regex extractor
#: never looked at, which is how VP-019/020/021's ports went unseen.
PLACEHOLDER_FIELDS = ("test_command", "target_url", "expected_result", "description")


def _iter_vp_texts(vp: Dict[str, Any]) -> Iterator[Tuple[str, str]]:
    """Yield ``(label, text)`` for every VP string that may carry a
    ``{{svc.*}}`` placeholder.

    Covers the flat fields plus the ``request`` block an ``api_test`` VP
    uses (2026-09-18). Missing the nested block would be a silent,
    severe bug: the deterministic runner would receive a literal
    ``{{svc.api.url}}`` as its URL and fail every api_test VP with a
    DNS error, which looks like a service problem rather than a
    substitution one.
    """
    for field_name in PLACEHOLDER_FIELDS:
        value = vp.get(field_name)
        if isinstance(value, str) and value:
            yield field_name, value

    request = vp.get("request")
    if not isinstance(request, dict):
        return
    url = request.get("url")
    if isinstance(url, str) and url:
        yield "request.url", url
    headers = request.get("headers")
    if isinstance(headers, dict):
        for name, value in headers.items():
            if isinstance(value, str) and value:
                yield f"request.headers.{name}", value
    body = request.get("body")
    if isinstance(body, str) and body:
        yield "request.body", body
    elif isinstance(body, (dict, list)):
        for label, value in _iter_nested_strings(body, "request.body"):
            yield label, value


def _iter_nested_strings(node: Any, prefix: str) -> Iterator[Tuple[str, str]]:
    """Recursively yield the string leaves of a JSON-ish structure."""
    if isinstance(node, str):
        if node:
            yield prefix, node
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_nested_strings(value, f"{prefix}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_nested_strings(value, f"{prefix}[{index}]")


def _resolve_nested_strings(
    node: Any, declarations: Dict[str, ServiceDeclaration], unresolved: List[str],
) -> Any:
    """Return a copy of ``node`` with every string leaf resolved."""
    if isinstance(node, str):
        return resolve_text(node, declarations)[0]
    if isinstance(node, dict):
        return {
            k: _resolve_nested_strings(v, declarations, unresolved)
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [
            _resolve_nested_strings(v, declarations, unresolved) for v in node
        ]
    return node


@dataclass
class ReferenceIssue:
    """A VP whose service reference does not resolve, or that bypasses
    the declaration by hardcoding a declared service's port."""

    vp_id: str
    kind: str  # "unresolved_placeholder" | "raw_declared_port"
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"vp_id": self.vp_id, "kind": self.kind, "detail": self.detail}


def find_reference_issues(
    plan_data: Any,
    declarations: Dict[str, ServiceDeclaration],
) -> List[ReferenceIssue]:
    """Per-VP problems with how it references the declared services.

    Two rules, deliberately narrow so neither produces noise:

      * **unresolved_placeholder** — the VP names a service (or a field)
        that does not exist. The VP cannot run correctly, so the caller
        turns it into a BLOCKED verdict rather than letting a literal
        ``{{svc.typo.port}}`` reach a shell.
      * **raw_declared_port** — the VP writes a port that a declared
        service owns, in any of the spellings
        :func:`service_freshness.extract_ports_from_text` recognises.
        Undeclared ports are NOT flagged: a VP curling a database or an
        external endpoint is legitimate and has no declaration to route
        through.

    VPs with no reference issues are simply absent from the result.
    """
    issues: List[ReferenceIssue] = []
    if not isinstance(plan_data, dict):
        return issues
    vps = plan_data.get("verification_points") or plan_data.get("vps") or []
    if not isinstance(vps, list):
        return issues

    declared_ports = {d.port: d.name for d in declarations.values()}
    for vp in vps:
        if not isinstance(vp, dict):
            continue
        vp_id = str(vp.get("id", "unknown"))
        for label, value in _iter_vp_texts(vp):
            _, unresolved = resolve_text(value, declarations)
            for raw in unresolved:
                issues.append(
                    ReferenceIssue(
                        vp_id=vp_id,
                        kind="unresolved_placeholder",
                        detail=f"{label}: {raw}",
                    )
                )
            if declared_ports:
                for port in sorted(extract_ports_from_text(value)):
                    owner = declared_ports.get(port)
                    if owner is not None:
                        issues.append(
                            ReferenceIssue(
                                vp_id=vp_id,
                                kind="raw_declared_port",
                                detail=(
                                    f"{label} hardcodes port {port}, "
                                    f"which service {owner!r} owns; use "
                                    f"{{{{svc.{owner}.port}}}} / "
                                    f"{{{{svc.{owner}.url}}}} instead"
                                ),
                            )
                        )
    return issues


def apply_resolution(plan_data: Any, declarations: Dict[str, ServiceDeclaration]) -> dict:
    """Return a shallow-copied plan whose VP placeholder fields are
    substituted with concrete values.

    The **on-disk** plan keeps the placeholders: ports are declared
    once and re-resolved every round, and a persisted resolved copy
    would go stale the moment a declaration's port changed. Only the
    in-memory plan handed to the executor is resolved.
    """
    if not isinstance(plan_data, dict):
        return plan_data
    resolved_plan = dict(plan_data)
    vps = plan_data.get("verification_points")
    if not isinstance(vps, list):
        return resolved_plan
    resolved_vps: List[Any] = []
    for vp in vps:
        if not isinstance(vp, dict):
            resolved_vps.append(vp)
            continue
        new_vp = dict(vp)
        for field_name in PLACEHOLDER_FIELDS:
            value = new_vp.get(field_name)
            if isinstance(value, str) and value:
                new_vp[field_name] = resolve_text(value, declarations)[0]
        # ``request`` is nested, so it needs a recursive pass — see
        # ``_iter_vp_texts`` for why leaving it out is not an option.
        request = new_vp.get("request")
        if isinstance(request, dict):
            new_request = dict(request)
            url = new_request.get("url")
            if isinstance(url, str) and url:
                new_request["url"] = resolve_text(url, declarations)[0]
            headers = new_request.get("headers")
            if isinstance(headers, dict):
                new_request["headers"] = {
                    k: (resolve_text(v, declarations)[0]
                        if isinstance(v, str) else v)
                    for k, v in headers.items()
                }
            body = new_request.get("body")
            if isinstance(body, (dict, list)):
                new_request["body"] = _resolve_nested_strings(
                    body, declarations, [],
                )
            elif isinstance(body, str) and body:
                new_request["body"] = resolve_text(body, declarations)[0]
            new_vp["request"] = new_request
        resolved_vps.append(new_vp)
    resolved_plan["verification_points"] = resolved_vps
    return resolved_plan


def service_name_of_port(
    declarations: Iterable[ServiceDeclaration], port: int
) -> Optional[str]:
    for decl in declarations:
        if decl.port == port:
            return decl.name
    return None
