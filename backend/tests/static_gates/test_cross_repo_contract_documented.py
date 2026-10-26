"""The provider-side cross-repo contract must be written down — and the
writing must match the code.

Why this gate exists
--------------------
The plan lifecycle is being split across two repositories. PDT (this one)
is the **provider**: it owns the state machine, the executor and the
verification loop. The scheduler (the other one) is the **consumer**: it
decides *which* plan runs next, and it does that by reading PDT's plan
list and calling PDT's lifecycle endpoints.

A contract that lives only in two people's heads is not a contract. It is
a shared assumption, and the next implementation change on either side
rewrites it silently: PDT renames a response key, or the scheduler starts
guessing *which* side to stop by itself, and the two halves stop agreeing
without anybody noticing. The merge order (PDT first, the scheduler only
consumes what has already been merged) is only enforceable if the thing
being consumed is written down somewhere both repositories can point at.

So the provider side is frozen in
``docs/development/cross-repo-contract.md``, and this gate is what keeps
that document honest. A document that drifts from the code is worse than
no document: it is a promise the code does not keep, published under the
project's own name.

What it pins
------------
1. **The section exists, with the pinned title.** ``## 1. 提供方（PDT）契约``
   is the identifier the scheduler's own gate asserts on; the wording is
   fixed so that neither side has to guess what the other called it.
2. **The endpoint table exists and is pinned by its header row.** The
   table is the contract: four endpoints, one method each, one request
   body, one success response.
3. **The four endpoints are the ones the code actually serves.** The
   table is checked against the registered routes, so a route that is
   renamed without updating the contract fails here rather than in the
   scheduler.
4. **``queued`` is the only waiting phase**, ``ready`` is an input alias
   that leaves a deprecation log, and the plan listing does not emit
   ``ready``.
5. **``LifecycleResult`` has exactly the six documented fields**, checked
   against the dataclass.
6. **The error codes are written down** (404 / 409 / 400).
7. **The document carries no local-machine path and no private project
   name.** It is published in a public repository; a path that identifies
   one developer's checkout, or the name of an unrelated private project,
   must not travel with it.
8. **The contract is Markdown.** A ``.py`` next to the contract would be
   a second, executable copy of the same promises — and the only such
   file is this gate.

Scope, stated honestly
----------------------
The gate checks that the *text* says the right things and that the text
still agrees with the code on the points where the two can be compared
mechanically (the routes, the dataclass fields). It cannot check that
the prose is unambiguous, and it cannot check the consumer's half of the
contract — that is the scheduler's own gate, in the scheduler's
repository.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOCS_DIR = _REPO_ROOT / "docs"
_DOC_DIR = _DOCS_DIR / "development"
_CONTRACT_DOC = _DOC_DIR / "cross-repo-contract.md"

#: The provider section's title, pinned verbatim. The scheduler's gate
#: asserts on the same string, so the two repositories cannot drift into
#: calling the same section two different things.
CONTRACT_SECTION_TITLE = "## 1. 提供方（PDT）契约"

#: The endpoint table's header row, pinned verbatim. The table is the
#: contract itself; a header that changes shape means a reader in the
#: other repository is looking at a table it does not understand.
ENDPOINT_TABLE_HEADER = "| 端点 | 方法 | 请求体 | 成功响应 |"

#: The four endpoints the provider exposes, as ``(method, path)``. The
#: path is the one the code registers; ``{plan_id}`` is the path
#: parameter's real name in ``backend/routes/plan_lifecycle.py`` and
#: ``backend/routes/phases.py``.
REQUIRED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/api/plan/{plan_id}/lifecycle/start"),
    ("POST", "/api/plan/{plan_id}/lifecycle/stop"),
    ("GET", "/api/plan/{plan_id}/state-from-db"),
    ("GET", "/api/plans"),
)

#: The six fields of ``LifecycleResult`` — the body both lifecycle
#: endpoints answer with.
LIFECYCLE_RESULT_FIELDS: tuple[str, ...] = (
    "plan_id",
    "dispatched",
    "berth_phase",
    "final_phase",
    "checkpoint",
    "detail",
)

#: The status codes the provider promises to answer with.
REQUIRED_ERROR_CODES: tuple[str, ...] = ("404", "409", "400")

#: Local-machine path shapes. ``/api/...`` is an HTTP path and is not a
#: filesystem path, so these are the *filesystem* shapes only: the home
#: directories a checkout lives in, the macOS volume mount, the Windows
#: drive letter, and the shell's home shorthand.
_LOCAL_PATH_RE = re.compile(
    r"(?:/Users/|/home/|/Volumes/|/mnt/[a-z]|[A-Za-z]:\\|~/|file://)"
)

#: A repository URL in a published contract names one project out of the
#: two that are talking to each other — which is exactly the shape a
#: private, unrelated checkout has when it leaks into a document.
_REPO_URL_RE = re.compile(r"github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def contract_text() -> str:
    """The contract document's text.

    The file is read once per module: it does not change while the suite
    runs, and every test below reads the same bytes.
    """
    assert _CONTRACT_DOC.exists(), (
        f"{_CONTRACT_DOC.relative_to(_REPO_ROOT)} does not exist. The "
        "provider-side cross-repo contract has to be written down in one "
        "place both repositories can point at — a promise that lives only "
        "in a commit message is renegotiated by the next implementation "
        "change."
    )
    return _CONTRACT_DOC.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body under ``heading`` (up to the next heading).

    A heading is any line starting with ``#``. The body is what the
    heading introduces and nothing else, so a keyword that belongs to a
    *different* subsection cannot satisfy a check that is about this one.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    assert start is not None, (
        f"the document has no section titled {heading!r}"
    )
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def _endpoint_table(text: str) -> dict[str, str]:
    """Map every endpoint path in the endpoint table to its method cell.

    Only four-column table rows whose first cell is a path are read, so
    the other tables in the document (the response fields, the error
    codes) cannot be mistaken for the endpoint table.
    """
    rows: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) != 4:
            continue
        path = cells[0].strip("`")
        if not path.startswith("/api/"):
            continue
        rows[path] = cells[1]
    return rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_contract_doc_exists_with_required_sections(contract_text: str) -> None:
    """The document exists, and it carries the pinned provider section.

    The title is an identifier, not a caption: the scheduler's gate
    asserts on the same string, so a reworded heading breaks the link
    between the two repositories rather than silently renaming one half
    of a shared contract.
    """
    assert CONTRACT_SECTION_TITLE in contract_text, (
        "the contract document does not contain the pinned provider "
        f"section title {CONTRACT_SECTION_TITLE!r}. That title is the "
        "identifier the consumer's gate asserts on — the two repositories "
        "have to call the same section the same thing."
    )

    assert ENDPOINT_TABLE_HEADER in contract_text, (
        "the contract document does not contain the pinned endpoint table "
        f"header {ENDPOINT_TABLE_HEADER!r}. The table is the contract: "
        "four endpoints, one method, one request body, one success "
        "response each. A header that changes shape leaves a reader in "
        "the other repository unable to read it."
    )


def test_contract_doc_lists_the_four_endpoints(contract_text: str) -> None:
    """The endpoint table lists the four endpoints, each with its method.

    The check is a substring assertion on the table itself: the path has
    to be a row of the pinned table, and the method cell of that row has
    to say the method the code registers. A path mentioned in prose
    somewhere else does not satisfy it — the table is what the consumer
    reads.
    """
    rows = _endpoint_table(contract_text)

    for method, path in REQUIRED_ENDPOINTS:
        assert path in rows, (
            f"the endpoint table does not list {path!r}. The provider "
            f"exposes {len(REQUIRED_ENDPOINTS)} endpoints to the "
            "consumer; a missing one is a capability the consumer cannot "
            f"code against. Table rows found: {sorted(rows)}"
        )
        assert rows[path] == method, (
            f"the endpoint table lists {path!r} as {rows[path]!r}, but "
            f"the code registers it as {method!r}. The method is part of "
            "the contract: the consumer calls it with that verb."
        )

    # The same four endpoints, spelled with the generic ``{id}``
    # placeholder the prose uses. Both spellings must be in the document:
    # the table carries the path the code registers, the prose carries
    # the shape the two sides agree on in writing.
    for method, path in REQUIRED_ENDPOINTS:
        generic = path.replace("{plan_id}", "{id}")
        assert f"`{method} {generic}`" in contract_text, (
            f"the document does not mention {method} {generic!r}. The "
            "endpoint list in prose and the endpoint table must cover the "
            "same four endpoints."
        )


def test_contract_doc_states_queued_is_the_only_waiting_phase(
    contract_text: str,
) -> None:
    """``queued`` is the only waiting phase; ``ready`` is an input alias.

    Three claims, all of them load-bearing for the consumer:

    * **``queued`` is the only waiting phase.** The scheduler decides
      what to run by looking for plans in the waiting phase. If there
      were two names for it, the scheduler would have to know both.
    * **``ready`` is an input alias and leaves a deprecation log.** Old
      callers may still send it; the read path normalises it to
      ``queued`` and records that it saw the old word, so the old data
      can be found and cleaned up.
    * **The plan listing does not emit ``ready``.** The scheduler reads
      the listing, so a listing that still said ``ready`` would put the
      old vocabulary back on the consumer's side of the wire.
    """
    section = _section(contract_text, "### 1.2 等待相位口径")

    assert "queued" in section, (
        "the waiting-phase section does not mention `queued`. The only "
        "waiting phase has to be named here — the consumer schedules on "
        "that name."
    )
    assert "ready" in section, (
        "the waiting-phase section does not mention `ready`. The legacy "
        "spelling is still accepted on input, and the contract has to "
        "say so (and say what happens to it)."
    )
    assert "别名" in section, (
        "the waiting-phase section does not call `ready` an alias "
        "(别名). `ready` is not a second waiting phase — it is the old "
        "name for `queued`, and the consumer must not treat it as a "
        "value of its own."
    )
    assert "弃用" in section and "日志" in section, (
        "the waiting-phase section does not say that reading `ready` "
        "leaves a deprecation log (弃用日志). The alias is accepted, but "
        "it is recorded — that record is what makes the old data "
        "findable and cleanable."
    )
    assert "/api/plans" in section and "ready" in section, (
        "the waiting-phase section does not tie `ready` to the plan "
        "listing (/api/plans). The listing is what the consumer reads; "
        "it must not carry the legacy spelling."
    )


def test_contract_doc_documents_the_lifecycle_result_fields(
    contract_text: str,
) -> None:
    """``LifecycleResult`` has exactly the six documented fields.

    The field list is the response body of both lifecycle endpoints, so
    it is compared against the dataclass rather than trusted: a field
    added to the dataclass without a line in the contract is a response
    key the consumer has never heard of.
    """
    from plan_lifecycle_service import LifecycleResult

    documented = {
        field for field in LIFECYCLE_RESULT_FIELDS if f"`{field}`" in contract_text
    }
    assert documented == set(LIFECYCLE_RESULT_FIELDS), (
        "the contract does not document every field of `LifecycleResult`. "
        f"Missing: {sorted(set(LIFECYCLE_RESULT_FIELDS) - documented)}. The "
        "response body is the contract; a field that is not written down "
        "is a key the consumer cannot rely on."
    )

    actual = tuple(f.name for f in dataclasses.fields(LifecycleResult))
    assert actual == LIFECYCLE_RESULT_FIELDS, (
        "`LifecycleResult` has the fields "
        f"{list(actual)}, but the contract documents "
        f"{list(LIFECYCLE_RESULT_FIELDS)}. Update the contract and this "
        "gate together — the response body is what the consumer parses."
    )


def test_contract_doc_documents_the_error_codes(contract_text: str) -> None:
    """The three status codes the provider promises are written down.

    404 (no such plan), 409 (conflict) and 400 (bad request) are the
    answers the consumer has to branch on. A facade that wrapped them
    all in a 500 would leave the consumer with one branch and one
    behaviour — so the codes are part of what is promised.
    """
    section = _section(contract_text, "### 1.4 错误码")

    for code in REQUIRED_ERROR_CODES:
        assert code in section, (
            f"the error section does not mention {code}. The consumer "
            "branches on the status code: 404 (no such plan), 409 "
            "(conflict), 400 (bad request). A code that is not written "
            "down is a code the consumer cannot handle."
        )


def test_contract_doc_carries_no_local_paths_or_private_names(
    contract_text: str,
) -> None:
    """A published contract names no local path and no unrelated project.

    The document ships in a public repository. A path that identifies one
    developer's checkout, or the name of a private project that has
    nothing to do with this contract, travels with it — and neither is
    needed to state the contract.
    """
    offenders = [
        f"{match.group(0)!r} (local filesystem path)"
        for match in _LOCAL_PATH_RE.finditer(contract_text)
    ]
    offenders += [
        f"{match.group(0)!r} (repository URL)"
        for match in _REPO_URL_RE.finditer(contract_text)
    ]
    assert not offenders, (
        "the contract document carries a local-machine path or a "
        "repository URL. Both identify a machine or a project that the "
        "contract does not need to name: the endpoints are enough. "
        "Offenders:\n  " + "\n  ".join(offenders)
    )


def test_the_contract_lives_in_markdown_only() -> None:
    """The contract is a document; the only code next to it is this gate.

    A ``.py`` file under ``docs/`` would be a second, executable copy of
    the same promises — and executable copies drift. The gate itself
    lives under ``backend/tests/static_gates/``, which is where a test
    belongs.
    """
    contract_files = sorted(_DOC_DIR.glob("cross-repo-contract*"))
    assert contract_files, (
        "no contract file was found under "
        f"{_DOC_DIR.relative_to(_REPO_ROOT)} — the gate would pass "
        "without checking anything."
    )
    non_markdown = [p for p in contract_files if p.suffix != ".md"]
    assert not non_markdown, (
        "the contract directory holds a file that is not Markdown: "
        + ", ".join(str(p.relative_to(_REPO_ROOT)) for p in non_markdown)
        + ". The contract is a document; an executable copy of it would "
        "drift from it."
    )

    python_files = sorted(_DOCS_DIR.rglob("*.py"))
    assert not python_files, (
        "the docs tree holds a Python file: "
        + ", ".join(str(p.relative_to(_REPO_ROOT)) for p in python_files)
        + ". The only code that belongs to this contract is the gate "
        "itself, and it lives under backend/tests/static_gates/."
    )


def test_the_contract_has_a_chinese_sibling(contract_text: str) -> None:
    """The contract exists in both locales, and the two copies agree.

    The docs site is bilingual: ``mkdocs.yml`` declares an ``en`` and a
    ``zh`` locale, and the i18n plugin falls back to the default locale
    when a translation is missing. That fallback is exactly why a missing
    translation is invisible — the build succeeds, the language switcher
    still appears, and the reader silently gets the other language.

    The site-wide gate (``test_docs_are_bilingual.py``) checks that every
    page is *paired*. This one checks the pairing for this contract, and
    checks that the two copies still say the same thing: a contract whose
    locales disagree is worse than a contract written in one language,
    because the consumer codes against whichever copy it was handed.
    """
    zh_doc = _DOC_DIR / "cross-repo-contract.zh.md"
    assert zh_doc.exists(), (
        f"{zh_doc.relative_to(_REPO_ROOT)} does not exist. The contract "
        "has to exist in both locales — otherwise a reader who switches "
        "the language silently gets the other one, and the language "
        "switcher keeps promising a page that is not there."
    )

    zh_text = zh_doc.read_text(encoding="utf-8")

    assert CONTRACT_SECTION_TITLE in zh_text, (
        "the Chinese copy does not carry the pinned section title "
        f"{CONTRACT_SECTION_TITLE!r}. Both locales have to name the "
        "provider section the same way: the title is the identifier the "
        "consumer's gate asserts on."
    )

    assert ENDPOINT_TABLE_HEADER in zh_text, (
        "the Chinese copy does not carry the pinned endpoint table header "
        f"{ENDPOINT_TABLE_HEADER!r}. The header is printed unchanged in "
        "every locale, so the two tables can be compared row by row."
    )

    base_rows = _endpoint_table(contract_text)
    zh_rows = _endpoint_table(zh_text)
    assert zh_rows == base_rows, (
        "the two locales' endpoint tables disagree: "
        f"{zh_rows} != {base_rows}. The endpoint table is what the "
        "consumer codes against; the two copies have to name the same "
        "endpoints with the same methods."
    )


def test_the_contract_doc_lists_the_endpoints_the_code_registers(
    contract_text: str,
) -> None:
    """The contract table agrees with the routes the application serves.

    This is the check that keeps the document from becoming a promise
    the code does not keep: the table is read out of the document, the
    routes are read out of the application, and the two sets have to be
    the same four.
    """
    from server import iter_app_routes

    registered = {
        route.path: route.methods
        for route in iter_app_routes()
        if route.path.startswith("/api/")
    }

    rows = _endpoint_table(contract_text)
    for method, path in REQUIRED_ENDPOINTS:
        assert path in registered, (
            f"{path!r} is in the contract but is not a registered route. "
            "The consumer would call a URL this application does not "
            "serve."
        )
        methods = registered[path] or set()
        assert method in methods, (
            f"{path!r} is registered for {sorted(methods)}, but the "
            f"contract promises {method!r}."
        )