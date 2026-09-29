"""Unit tests for plan-level service declarations (2026-09-18 C1).

Covers:
  * ``parse_declarations`` — the happy path, every rejection rule
    (missing/duplicate name, duplicate port, bad port, protected port,
    empty ``start_cmd``, escaping ``cwd``/``log_path``, non-http
    ``health_url``), and the ``block_present`` distinction between
    "no ``services`` key" and "an explicitly empty ``services`` block".
  * ``resolve_text`` / ``iter_placeholders`` — the four resolvable
    fields, and the deliberate refusal to substitute an unresolvable
    placeholder with an empty string.
  * ``find_reference_issues`` — unresolved placeholders flagged,
    hardcoded ports of *declared* services flagged, undeclared ports
    left alone.
  * ``apply_resolution`` — resolution is in-memory only; the on-disk
    plan keeps its placeholders.
  * ``service_freshness.extract_service_ports`` — declared-first,
    the empty-block-is-authoritative rule, the legacy regex fallback,
    and the newly-scanned ``target_url`` field.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from service_declaration import (  # noqa: E402
    DEFAULT_READY_TIMEOUT,
    MAX_READY_TIMEOUT,
    SERVICE_HOST,
    ServiceDeclaration,
    apply_resolution,
    find_reference_issues,
    iter_placeholders,
    parse_declarations,
    resolve_text,
)
from service_freshness import (  # noqa: E402
    PROTECTED_PORTS,
    extract_ports_from_text,
    extract_service_ports,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A project root with a couple of subdirectories so cwd/log_path
    containment has something real to resolve against."""
    (tmp_path / "tmp").mkdir()
    (tmp_path / "frontend-app").mkdir()
    return tmp_path


def _entry(**overrides) -> dict:
    base = {
        "name": "api",
        "port": 8080,
        "start_cmd": "source venv1/bin/activate && python -m api.server",
        "cwd": ".",
    }
    base.update(overrides)
    return base


def _plan(*entries, **extra) -> dict:
    plan = {"services": list(entries)} if entries else {"services": []}
    plan.update(extra)
    plan.setdefault("verification_points", [])
    return plan


# ---------------------------------------------------------------------------
# parse_declarations — presence + happy path
# ---------------------------------------------------------------------------


def test_no_services_key_is_not_a_declaration_block(project_dir: Path):
    """``block_present`` must be False when the key is absent — that is
    the "legacy plan, fall back to scraping" signal."""
    result = parse_declarations({"verification_points": []}, project_dir)

    assert result.block_present is False
    assert result.declarations == []
    assert result.issues == []


def test_empty_services_list_is_a_present_but_empty_block(project_dir: Path):
    """The distinction the preflight branches on: an explicitly empty
    block means "asked, declared nothing", not "legacy"."""
    result = parse_declarations({"services": []}, project_dir)

    assert result.block_present is True
    assert result.declarations == []
    assert result.issues == []


def test_valid_declaration_parses_with_defaults(project_dir: Path):
    result = parse_declarations(_plan(_entry()), project_dir)

    assert result.issues == []
    [decl] = result.declarations
    assert decl.name == "api"
    assert decl.port == 8080
    assert decl.start_cmd.startswith("source venv1")
    assert decl.cwd == "."
    assert decl.health_url == ""
    assert decl.ready_timeout_seconds == DEFAULT_READY_TIMEOUT
    assert decl.log_path == ""
    assert decl.reap_on_exit is True
    assert decl.url == f"http://{SERVICE_HOST}:8080"
    assert decl.resolved_health_url() == decl.url


def test_string_port_is_coerced(project_dir: Path):
    """The planner emits both ``8080`` and ``"8080"``."""
    result = parse_declarations(_plan(_entry(port="8080")), project_dir)

    assert result.issues == []
    assert result.declarations[0].port == 8080


def test_name_is_lowercased(project_dir: Path):
    """``{{svc.Api.port}}`` and ``{{svc.api.port}}`` must not mean
    different things."""
    result = parse_declarations(_plan(_entry(name="Api")), project_dir)

    assert result.issues == []
    assert result.declarations[0].name == "api"


def test_optional_fields_are_carried(project_dir: Path):
    result = parse_declarations(
        _plan(
            _entry(
                health_url="http://127.0.0.1:8080/health",
                ready_timeout_seconds=90,
                log_path="tmp/api.log",
                reap_on_exit=False,
            )
        ),
        project_dir,
    )

    assert result.issues == []
    decl = result.declarations[0]
    assert decl.health_url == "http://127.0.0.1:8080/health"
    assert decl.resolved_health_url() == "http://127.0.0.1:8080/health"
    assert decl.ready_timeout_seconds == 90
    assert decl.log_path == "tmp/api.log"
    assert decl.reap_on_exit is False


# ---------------------------------------------------------------------------
# parse_declarations — rejection rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_entry, expected_fragment",
    [
        ("not-a-dict", "expected an object"),
        ({}, "missing 'name'"),
        (_entry(name=""), "missing 'name'"),
        (_entry(name="has.dot"), "name must match"),
        (_entry(name="has space"), "name must match"),
        (_entry(port=None), "missing or non-numeric 'port'"),
        (_entry(port="five"), "missing or non-numeric 'port'"),
        (_entry(port=True), "missing or non-numeric 'port'"),
        (_entry(port=0), "out of range"),
        (_entry(port=70000), "out of range"),
        (_entry(start_cmd=""), "missing 'start_cmd'"),
        (_entry(start_cmd="   "), "missing 'start_cmd'"),
        (_entry(health_url="ftp://x/y"), "not an http(s) URL"),
        (_entry(cwd="../outside"), "escapes the project directory"),
        (_entry(log_path="../../etc/passwd"), "escapes the project directory"),
    ],
)
def test_malformed_entries_are_rejected_with_a_reason(
    project_dir: Path, bad_entry, expected_fragment: str
):
    result = parse_declarations(_plan(bad_entry), project_dir)

    assert result.declarations == []
    assert len(result.issues) == 1
    assert expected_fragment in result.issues[0].reason


@pytest.mark.parametrize("port", sorted(PROTECTED_PORTS))
def test_protected_ports_cannot_be_declared(project_dir: Path, port: int):
    """Declaring an backend-runtime port is how the 2026-09-07 incident
    (a restart script killing the backend on :8000 mid-round) would
    come back."""
    result = parse_declarations(_plan(_entry(port=port)), project_dir)

    assert result.declarations == []
    assert "backend runtime" in result.issues[0].reason


def test_duplicate_name_rejects_only_the_second(project_dir: Path):
    result = parse_declarations(
        _plan(_entry(port=8080), _entry(port=5622)), project_dir
    )

    assert [d.port for d in result.declarations] == [8080]
    assert len(result.issues) == 1
    assert result.issues[0].reason == "duplicate service name"
    assert result.issues[0].index == 1


def test_duplicate_port_rejects_only_the_second(project_dir: Path):
    """Two services on one port is the VP-019/020/021 race in
    declaration form — the second must not survive."""
    result = parse_declarations(
        _plan(_entry(name="api"), _entry(name="chart", port=8080)),
        project_dir,
    )

    assert [d.name for d in result.declarations] == ["api"]
    assert len(result.issues) == 1
    assert "'api'" in result.issues[0].reason


def test_non_list_services_block_is_one_issue(project_dir: Path):
    result = parse_declarations({"services": {"name": "api"}}, project_dir)

    assert result.block_present is True
    assert result.declarations == []
    assert "expected a list" in result.issues[0].reason


def test_ready_timeout_is_clamped(project_dir: Path):
    high = parse_declarations(_plan(_entry(ready_timeout_seconds=99999)), project_dir)
    low = parse_declarations(_plan(_entry(ready_timeout_seconds=-5)), project_dir)

    assert high.declarations[0].ready_timeout_seconds == MAX_READY_TIMEOUT
    assert low.declarations[0].ready_timeout_seconds == 1


def test_absolute_cwd_inside_the_project_is_accepted(project_dir: Path):
    result = parse_declarations(
        _plan(_entry(cwd=str(project_dir / "frontend-app"))), project_dir
    )

    assert result.issues == []
    assert result.declarations[0].cwd == "frontend-app"


def test_a_bad_entry_does_not_discard_its_siblings(project_dir: Path):
    """An unusable declaration must not take the whole block down —
    the round still has a real service to run against."""
    result = parse_declarations(
        _plan(_entry(name="api", port=8080), _entry(name="", port=5622)),
        project_dir,
    )

    assert [d.name for d in result.declarations] == ["api"]
    assert len(result.issues) == 1


def test_parse_never_raises_on_garbage(project_dir: Path):
    for garbage in (None, [], "services", 42):
        result = parse_declarations(garbage, project_dir)
        assert result.declarations == []
        assert result.block_present is False


# ---------------------------------------------------------------------------
# Placeholder resolution
# ---------------------------------------------------------------------------


def _declarations(**kwargs) -> dict:
    decl = ServiceDeclaration(
        name="api", port=8080, start_cmd="run", **kwargs
    )
    return {"api": decl}


def test_iter_placeholders_yields_name_and_field():
    text = "curl {{svc.api.url}}/api && use port {{ svc.api.port }}"

    assert list(iter_placeholders(text)) == [
        ("{{svc.api.url}}", "api", "url"),
        ("{{ svc.api.port }}", "api", "port"),
    ]


@pytest.mark.parametrize(
    "template, expected",
    [
        ("curl {{svc.api.url}}/api", "curl http://127.0.0.1:8080/api"),
        ("--port {{svc.api.port}}", "--port 8080"),
        ("host {{svc.api.host}}", "host 127.0.0.1"),
        (
            "curl {{svc.api.health_url}}",
            "curl http://127.0.0.1:8080",
        ),
        ("{{ svc.Api.PORT }}", "8080"),
    ],
)
def test_resolve_text_substitutes_known_fields(template: str, expected: str):
    resolved, unresolved = resolve_text(template, _declarations())

    assert resolved == expected
    assert unresolved == []


def test_resolve_text_uses_the_declared_health_url():
    decls = _declarations(health_url="http://127.0.0.1:8080/healthz")

    resolved, unresolved = resolve_text("curl {{svc.api.health_url}}", decls)

    assert resolved == "curl http://127.0.0.1:8080/healthz"
    assert unresolved == []


@pytest.mark.parametrize(
    "template",
    [
        "curl {{svc.typo.port}}",
        "curl {{svc.api.nonsense}}",
    ],
)
def test_unresolvable_placeholders_are_left_verbatim(template: str):
    """Substituting an empty string would turn a broken reference into
    a command that looks fine and quietly checks nothing."""
    resolved, unresolved = resolve_text(template, _declarations())

    assert resolved == template
    assert unresolved == [template.split()[-1]]


def test_resolve_text_passes_non_strings_through():
    assert resolve_text(None, {}) == (None, [])
    assert resolve_text(42, {}) == (42, [])


# ---------------------------------------------------------------------------
# Reference validation
# ---------------------------------------------------------------------------


def test_unresolved_placeholder_is_a_reference_issue():
    plan = {
        "verification_points": [
            {"id": "VP-001", "test_command": "curl {{svc.typo.port}}"},
            {"id": "VP-002", "test_command": "curl {{svc.api.port}}"},
        ]
    }

    issues = find_reference_issues(plan, _declarations())

    assert [i.vp_id for i in issues] == ["VP-001"]
    assert issues[0].kind == "unresolved_placeholder"
    assert "typo" in issues[0].detail


def test_hardcoded_declared_port_is_flagged_in_every_field():
    """The VP that writes ``-p 8080`` instead of ``{{svc.api.port}}``
    is the VP that starts a second server."""
    plan = {
        "verification_points": [
            {"id": "VP-001", "test_command": "nohup npx next dev -p 8080 &"},
            {"id": "VP-002", "target_url": "http://127.0.0.1:8080/data-viewer"},
        ]
    }

    issues = find_reference_issues(plan, _declarations())

    assert {i.vp_id for i in issues} == {"VP-001", "VP-002"}
    assert {i.kind for i in issues} == {"raw_declared_port"}
    assert all("'api'" in i.detail for i in issues)


def test_undeclared_ports_are_not_flagged():
    """A VP curling a database or an external endpoint is legitimate —
    it just has no declaration to route through."""
    plan = {
        "verification_points": [
            {"id": "VP-001", "test_command": "psql -h 127.0.0.1 -p 5432 -c 'select 1'"},
            {"id": "VP-002", "test_command": "curl http://example.com:9099/x"},
        ]
    }

    assert find_reference_issues(plan, _declarations()) == []


def test_find_reference_issues_handles_malformed_plans():
    assert find_reference_issues(None, {}) == []
    assert find_reference_issues({"verification_points": "nope"}, {}) == []
    assert find_reference_issues({"verification_points": [None, 3]}, {}) == []


# ---------------------------------------------------------------------------
# apply_resolution
# ---------------------------------------------------------------------------


def test_apply_resolution_is_in_memory_only():
    """The disk plan keeps its placeholders so the round re-resolves
    them against the current declaration every time."""
    plan = {
        "services": [{"name": "api", "port": 8080, "start_cmd": "run"}],
        "verification_points": [
            {
                "id": "VP-001",
                "test_command": "curl {{svc.api.port}}",
                "target_url": "http://127.0.0.1:{{svc.api.port}}/x",
                "expected_result": "port {{svc.api.port}} responds",
                "title": "untouched {{svc.api.port}}",
            }
        ],
    }

    resolved = apply_resolution(plan, _declarations())

    vp = resolved["verification_points"][0]
    assert vp["test_command"] == "curl 8080"
    assert vp["target_url"] == "http://127.0.0.1:8080/x"
    assert vp["expected_result"] == "port 8080 responds"
    # ``title`` is not a placeholder field — left alone on purpose.
    assert vp["title"] == "untouched {{svc.api.port}}"
    # Original untouched.
    assert plan["verification_points"][0]["test_command"] == "curl {{svc.api.port}}"


def test_apply_resolution_passes_non_dict_plans_through():
    assert apply_resolution(None, {}) is None
    assert apply_resolution([1, 2], {}) == [1, 2]


# ---------------------------------------------------------------------------
# extract_service_ports — declared-first vs. legacy scrape
# ---------------------------------------------------------------------------


def test_declared_services_are_the_port_list(project_dir: Path):
    plan = _plan(_entry(port=8080))
    plan["verification_points"] = [
        {"id": "VP-001", "test_command": "curl http://127.0.0.1:8080/x"},
    ]

    assert extract_service_ports(plan, project_dir) == [8080]


def test_declared_services_win_over_command_text(project_dir: Path):
    """An undeclared port mentioned in a VP command is NOT adopted —
    adopting it would mean managing (and eventually reaping) a service
    the plan never declared."""
    plan = _plan(_entry(port=8080))
    plan["verification_points"] = [
        {"id": "VP-001", "test_command": "curl http://127.0.0.1:9099/other"},
    ]

    assert extract_service_ports(plan, project_dir) == [8080]


def test_explicitly_empty_block_beats_scraping(project_dir: Path):
    """The whole point of the declaration: an empty block means the
    plan needs no services, not "go find some ports in the prose"."""
    plan = {
        "services": [],
        "verification_points": [
            {"id": "VP-001", "test_command": "curl http://127.0.0.1:9099/other"},
        ],
    }

    assert extract_service_ports(plan, project_dir) == []


def test_legacy_plan_without_a_services_key_still_scrapes(project_dir: Path):
    plan = {
        "verification_points": [
            {"id": "VP-001", "test_command": "curl http://127.0.0.1:8080/x"},
        ]
    }

    assert extract_service_ports(plan, project_dir) == [8080]


def test_legacy_plan_target_url_is_now_scanned(project_dir: Path):
    """The field that was invisible before 2026-09-18."""
    plan = {
        "verification_points": [
            {
                "id": "VP-019",
                "target_url": "http://127.0.0.1:3000/data-viewer",
                "test_command": "echo 'Basic functionality check'",
            }
        ]
    }

    assert extract_service_ports(plan, project_dir) == [3000]


def test_declared_protected_ports_are_dropped_by_the_parser(project_dir: Path):
    """A plan declaring :8000 loses the declaration, so the preflight
    has nothing to restart."""
    plan = _plan(_entry(port=8000))

    assert extract_service_ports(plan, project_dir) == []


def test_legacy_protected_ports_still_filtered(project_dir: Path):
    plan = {
        "verification_points": [
            {"id": "VP-001", "test_command": "curl http://127.0.0.1:8000/api"},
        ]
    }

    assert extract_service_ports(plan, project_dir) == []


def test_ports_are_deduped_and_sorted(project_dir: Path):
    plan = _plan(_entry(name="b", port=9001), _entry(name="a", port=9000))

    assert extract_service_ports(plan, project_dir) == [9000, 9001]


# ---------------------------------------------------------------------------
# extract_ports_from_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("curl http://127.0.0.1:8080/x", {8080}),
        ("curl http://localhost:8080/", {8080}),
        ("nohup npx next dev -p 3000 &", {3000}),
        ("uvicorn app --port 8001", {8001}),
        ("API_PORT=8080 python -m server", {8080}),
        ("no ports here", set()),
        ("", set()),
    ],
)
def test_extract_ports_from_text(text: str, expected: set):
    assert extract_ports_from_text(text) == expected


def test_extract_ports_from_text_ignores_non_strings():
    assert extract_ports_from_text(None) == set()
    assert extract_ports_from_text(3000) == set()


# ---------------------------------------------------------------------------
# VerificationAgent wiring — ``_normalize_service_declarations``
# ---------------------------------------------------------------------------


def _make_agent(tmp_path: Path):
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    return VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )


def test_agent_parses_declarations_off_the_plan(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _plan(_entry())
    plan["verification_points"] = [
        {"id": "VP-001", "test_command": "curl {{svc.api.url}}/api"},
    ]

    returned = agent._normalize_service_declarations(plan)

    assert returned is plan
    assert set(agent._service_declarations) == {"api"}
    assert agent._service_declarations["api"].port == 8080
    assert agent._service_declaration_report.block_present is True


def test_agent_legacy_plan_yields_no_declarations(tmp_path: Path):
    agent = _make_agent(tmp_path)

    agent._normalize_service_declarations({"verification_points": []})

    assert agent._service_declarations == {}
    assert agent._service_declaration_report.block_present is False


def test_agent_annotates_vps_with_reference_issues(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _plan(_entry())
    plan["verification_points"] = [
        {"id": "VP-001", "test_command": "curl {{svc.typo.port}}"},
        {"id": "VP-002", "test_command": "curl {{svc.api.port}}"},
    ]

    agent._normalize_service_declarations(plan)

    assert "service_reference_issues" in plan["verification_points"][0]
    assert "service_reference_issues" not in plan["verification_points"][1]


def test_agent_keeps_rejected_declarations_visible(tmp_path: Path):
    agent = _make_agent(tmp_path)
    plan = _plan(_entry(port=8000))

    agent._normalize_service_declarations(plan)

    assert agent._service_declarations == {}
    assert len(agent._service_declaration_report.issues) == 1


def test_agent_survives_a_malformed_reference_field(tmp_path: Path):
    """``uses_services`` as a bare string must not be iterated
    character-by-character."""
    agent = _make_agent(tmp_path)
    plan = _plan(_entry())
    plan["verification_points"] = [
        {"id": "VP-001", "test_command": "x", "uses_services": "api"},
    ]

    agent._normalize_service_declarations(plan)

    assert set(agent._service_declarations) == {"api"}


# ---------------------------------------------------------------------------
# Nested `request.*` fields (2026-09-18 api_test rework)
#
# An api_test VP states its endpoint in `request.url`. Missing the nested
# block would hand the deterministic runner a literal `{{svc.api.url}}`
# as its URL — it would fail every api_test VP with a DNS error, which
# looks like a service problem rather than a substitution one.
# ---------------------------------------------------------------------------


def _api_vp(**request_over) -> dict:
    request = {
        "method": "GET",
        "url": "{{svc.api.url}}/api/signal",
        "headers": {"X-Svc": "{{svc.api.host}}"},
        "body": {"service": "{{svc.api.url}}", "nested": ["{{svc.api.port}}"]},
    }
    request.update(request_over)
    return {"id": "VP-001", "verification_method": "api_test", "request": request}


def test_apply_resolution_expands_request_url():
    plan = {"verification_points": [_api_vp()]}

    resolved = apply_resolution(plan, _declarations())

    assert resolved["verification_points"][0]["request"]["url"] == (
        "http://127.0.0.1:8080/api/signal"
    )


def test_apply_resolution_expands_request_headers():
    plan = {"verification_points": [_api_vp()]}

    resolved = apply_resolution(plan, _declarations())

    assert resolved["verification_points"][0]["request"]["headers"] == {
        "X-Svc": "127.0.0.1",
    }


def test_apply_resolution_expands_string_leaves_of_request_body():
    plan = {"verification_points": [_api_vp()]}

    resolved = apply_resolution(plan, _declarations())

    body = resolved["verification_points"][0]["request"]["body"]
    assert body["service"] == "http://127.0.0.1:8080"
    assert body["nested"] == ["8080"]


def test_apply_resolution_does_not_mutate_the_original_request():
    plan = {"verification_points": [_api_vp()]}

    apply_resolution(plan, _declarations())

    assert plan["verification_points"][0]["request"]["url"] == (
        "{{svc.api.url}}/api/signal"
    )
    assert plan["verification_points"][0]["request"]["body"]["nested"] == [
        "{{svc.api.port}}"
    ]


def test_apply_resolution_handles_a_string_request_body():
    vp = _api_vp(body="{{svc.api.url}}")
    plan = {"verification_points": [vp]}

    resolved = apply_resolution(plan, _declarations())

    assert resolved["verification_points"][0]["request"]["body"] == (
        "http://127.0.0.1:8080"
    )


def test_find_reference_issues_sees_request_url():
    vp = _api_vp(url="http://127.0.0.1:8080/api/signal")
    plan = {"verification_points": [vp]}

    issues = find_reference_issues(plan, _declarations())

    assert [i.kind for i in issues] == ["raw_declared_port"]
    assert "request.url" in issues[0].detail


def test_find_reference_issues_sees_an_unresolved_request_url_placeholder():
    vp = _api_vp(url="{{svc.typo.url}}/api")
    plan = {"verification_points": [vp]}

    issues = find_reference_issues(plan, _declarations())

    assert [i.kind for i in issues] == ["unresolved_placeholder"]
    assert "request.url" in issues[0].detail


def test_request_placeholder_survives_a_vp_without_a_request():
    plan = {"verification_points": [{"id": "VP-001", "request": None}]}

    resolved = apply_resolution(plan, _declarations())

    assert resolved["verification_points"][0]["request"] is None


def test_request_url_ports_are_scanned_for_the_blocked_gate():
    """2026-09-18: an ``api_test`` VP names its endpoint in ``request.url``.
    Without scanning it, the blocked-port gate cannot connect a failed
    service to the VP that depends on it, and the VP reports a
    connection error instead of a BLOCKED verdict."""
    plan = {
        "verification_points": [
            {
                "id": "VP-001",
                "verification_method": "api_test",
                "request": {"method": "GET", "url": "http://127.0.0.1:8080/api/x"},
            }
        ]
    }

    assert extract_service_ports(plan, None) == [8080]


def test_request_body_ports_are_scanned_too():
    plan = {
        "verification_points": [
            {
                "id": "VP-001",
                "verification_method": "api_test",
                "request": {
                    "method": "POST", "url": "http://127.0.0.1:9000/x",
                    "body": "{\"upstream\": \"http://127.0.0.1:8080\"}",
                },
            }
        ]
    }

    assert extract_service_ports(plan, None) == [8080, 9000]
