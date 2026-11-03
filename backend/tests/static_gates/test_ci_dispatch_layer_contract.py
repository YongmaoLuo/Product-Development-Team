"""The dispatch layer must select one lane, and the nightly must stay out of it.

Why this gate exists
--------------------

``ci.yml`` grew a ``layer`` input on ``workflow_dispatch`` so a maintainer
can run one lane by hand — ``unit``, ``integration``, ``e2e``,
``time-sensitive`` or ``nightly``. The contract that came with it is
narrow, and every part of it can be undone by an edit that reads as
tidying:

* **``layer=all`` must not start the nightly.** The nightly is the daily
  cron run: capped, scheduled, and pointed at a runner pool GitHub
  deprioritises. Folding it into the ordinary full-suite dispatch makes
  every hand-run of the suite pay for a job that is not the hand-run's
  subject.
* **``layer=nightly`` must start the nightly and nothing else.** That
  dispatch exists to re-check the runner pool after an infrastructure
  failure; dragging the four merge-gate lanes along with it turns a
  short check into a full-suite run.
* **The ``schedule`` event must still start the nightly, and a pull
  request must still start all four merge-gate lanes.** Narrowing the
  other way is the other failure: the repository loses its nightly
  signal, or its merge gate goes quiet. Neither shows up as a red test —
  the lanes simply stop running, and the merge gate sees nothing wrong
  with a tree it never looked at.

The contract was introduced by a change that touched the workflow file
and nothing else, so no test in the suite was watching it. Proving it by
running the workflow would prove it once, expensively, and would not
notice the next edit that widened the condition back.

What is worth pinning is the *condition*, and it can be read: a job's
``if:`` is an expression over the event context, and the dispatch layer
is one of the values it reads. So this file parses ``ci.yml``, evaluates
each job's ``if:`` against a synthetic event context, and asserts on the
result. It calls nothing, dispatches nothing, and needs no runner.

Scope, stated honestly
----------------------

The evaluator below implements the subset of the GitHub Actions
expression language this contract uses: ``&&``, ``||``, ``!``, ``==``,
``!=``, parentheses, the ``github.event_name`` /
``github.event.inputs.layer`` / ``needs.<job>.result`` contexts, and the
``cancelled()`` / ``always()`` / ``success()`` / ``failure()`` status
functions. It is not a reimplementation of GitHub's evaluator, and it
does not pretend to be: a green run here means "the workflow's conditions
still say what the contract says", not "GitHub agrees". The parts a
refactor can silently undo are the parts this file pins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

import pytest
import yaml

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The job the nightly half of the contract is about.
NIGHTLY_JOB = "nightly-regression"

#: The four lanes that feed the merge gate. A hand-run dispatch picks
#: exactly one of them, or none.
MERGE_GATE_JOBS = ("unit-tests", "integration-tests", "e2e-on-demand", "existing-test")

#: The layer value the repo-wide dispatch uses, and the one that asks
#: for the nightly.
LAYER_ALL = "all"
LAYER_NIGHTLY = "nightly"

#: Each named layer, and the merge-gate lane it is supposed to start.
#:
#: The layer input names a lane, so a dispatch with ``layer=unit`` is a
#: request to run the unit lane and not the other three.  This mapping
#: is that naming: it says which lane belongs to which layer, and the
#: gate below checks the workflow agrees with it in both directions --
#: the lane starts, and the other three do not.
LAYER_JOBS = {
    "unit": "unit-tests",
    "integration": "integration-tests",
    "e2e": "e2e-on-demand",
    "time-sensitive": "existing-test",
}


# ---------------------------------------------------------------------------
# The workflow under test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow() -> dict:
    if not WORKFLOW.is_file():
        pytest.fail(f"{WORKFLOW} does not exist; the dispatch contract has no host")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:
    return workflow.get("jobs") or {}


# ---------------------------------------------------------------------------
# A small evaluator for the subset of GitHub Actions expressions this
# contract uses.
#
# It is a tokenizer plus a recursive-descent parser rather than a pile of
# string substitutions, because the whole point of the gate is to read a
# *condition*. Substituting text and handing the result to ``eval`` would
# make the test assert on the shape of the string it happened to be
# given; parsing it makes the test assert on what the condition means.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
    | (?P<and>&&)
    | (?P<or>\|\|)
    | (?P<neq>!=)
    | (?P<eq>==)
    | (?P<not>!)
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<string>'[^']*'|"[^"]*")
    | (?P<number>\d+(?:\.\d+)?)
    | (?P<name>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*)
    """,
    re.VERBOSE,
)

#: GitHub's status functions. At job level they answer for the job's own
#: dependencies; the only one the contract depends on is ``cancelled()``,
#: and the default context below is the uncancelled one.
_STATUS_FUNCTIONS = {
    "cancelled": lambda ctx: ctx.cancelled,
    "always": lambda ctx: True,
    "success": lambda ctx: True,
    "failure": lambda ctx: False,
}

_NEEDS_RESULT_RE = re.compile(r"needs\.([A-Za-z0-9_-]+)\.result")


@dataclass(frozen=True)
class _Context:
    """The event context a job's ``if:`` is evaluated against.

    ``layer`` is ``None`` for every event except ``workflow_dispatch``:
    GitHub exposes ``github.event.inputs`` only for a dispatch, and on
    every other event the expression reads ``null`` there. Modelling that
    is what keeps ``schedule`` and ``pull_request`` honest — a condition
    that tests ``layer == 'nightly'`` is false there, not undefined.
    """

    event_name: str
    layer: Optional[str]
    needs: Mapping[str, str]
    cancelled: bool = False


def _tokenize(expression: str) -> List[Tuple[str, str]]:
    tokens: List[Tuple[str, str]] = []
    position = 0
    while position < len(expression):
        match = _TOKEN_RE.match(expression, position)
        if match is None:
            raise ValueError(
                f"cannot read {expression[position:position + 20]!r} at "
                f"offset {position} of {expression!r}"
            )
        position = match.end()
        if match.lastgroup == "ws":
            continue
        tokens.append((match.lastgroup, match.group()))
    return tokens


class _Parser:
    """Recursive-descent parser for the expression subset.

    The grammar, in the order it is parsed::

        expression  := or_expr
        or_expr     := and_expr ( '||' and_expr )*
        and_expr    := unary ( '&&' unary )*
        unary       := '!' unary | comparison
        comparison  := operand ( ( '==' | '!=' ) operand )?
        operand     := '(' expression ')' | literal | function | context path
    """

    def __init__(self, tokens: List[Tuple[str, str]]) -> None:
        self._tokens = tokens
        self._position = 0

    def parse(self):
        node = self._parse_or()
        if self._position != len(self._tokens):
            raise ValueError(f"trailing tokens {self._tokens[self._position:]!r}")
        return node

    def _peek(self) -> Optional[Tuple[str, str]]:
        if self._position < len(self._tokens):
            return self._tokens[self._position]
        return None

    def _next(self) -> Tuple[str, str]:
        token = self._peek()
        if token is None:
            raise ValueError("the expression ends early")
        self._position += 1
        return token

    def _expect(self, kind: str) -> Tuple[str, str]:
        token = self._next()
        if token[0] != kind:
            raise ValueError(f"expected {kind}, found {token[1]!r}")
        return token

    def _parse_or(self):
        node = self._parse_and()
        while True:
            token = self._peek()
            if token is None or token[0] != "or":
                return node
            self._next()
            node = ("or", node, self._parse_and())

    def _parse_and(self):
        node = self._parse_unary()
        while True:
            token = self._peek()
            if token is None or token[0] != "and":
                return node
            self._next()
            node = ("and", node, self._parse_unary())

    def _parse_unary(self):
        token = self._peek()
        if token is not None and token[0] == "not":
            self._next()
            return ("not", self._parse_unary())
        return self._parse_comparison()

    def _parse_comparison(self):
        left = self._parse_operand()
        token = self._peek()
        if token is not None and token[0] in ("eq", "neq"):
            self._next()
            return ("cmp", token[0], left, self._parse_operand())
        return left

    def _parse_operand(self):
        kind, raw = self._next()
        if kind == "lparen":
            node = self._parse_or()
            self._expect("rparen")
            return node
        if kind == "string":
            return ("lit", raw[1:-1])
        if kind == "number":
            return ("lit", float(raw))
        if kind == "name":
            lowered = raw.lower()
            if lowered in ("true", "false"):
                return ("lit", lowered == "true")
            if lowered == "null":
                return ("lit", None)
            following = self._peek()
            if following is not None and following[0] == "lparen":
                self._next()
                self._expect("rparen")
                if lowered not in _STATUS_FUNCTIONS:
                    raise ValueError(f"unknown function {raw!r}")
                return ("func", lowered)
            return ("path", raw)
        raise ValueError(f"unexpected token {raw!r}")


def _truthy(value) -> bool:
    """GitHub's truthiness: ``false``, ``0``, ``''`` and ``null`` are falsy."""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value != ""
    if isinstance(value, (int, float)):
        return value != 0
    return True


def _equal(left, right) -> bool:
    """GitHub's ``==``.

    Strings compare case-insensitively, ``null`` compares equal to the
    empty string, and a string that looks like a number compares as one.
    """
    if isinstance(left, str) and isinstance(right, str):
        return left.lower() == right.lower()
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if left is None and right is None:
        return True
    if left is None and isinstance(right, str):
        return right == ""
    if right is None and isinstance(left, str):
        return left == ""
    if left is None or right is None:
        return False
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def _resolve(path: str, ctx: _Context):
    if path == "github.event_name":
        return ctx.event_name
    if path in ("github.event.inputs.layer", "inputs.layer"):
        return ctx.layer
    match = _NEEDS_RESULT_RE.fullmatch(path)
    if match is not None:
        job = match.group(1)
        if job not in ctx.needs:
            raise ValueError(
                f"`needs.{job}.result` is read by this condition, but "
                f"`{job}` is not in the job's `needs:`"
            )
        return ctx.needs[job]
    raise ValueError(f"unsupported context path {path!r}")


def _evaluate(node, ctx: _Context):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "or":
        return _truthy(_evaluate(node[1], ctx)) or _truthy(_evaluate(node[2], ctx))
    if kind == "and":
        return _truthy(_evaluate(node[1], ctx)) and _truthy(_evaluate(node[2], ctx))
    if kind == "not":
        return not _truthy(_evaluate(node[1], ctx))
    if kind == "cmp":
        left = _evaluate(node[2], ctx)
        right = _evaluate(node[3], ctx)
        equal = _equal(left, right)
        return equal if node[1] == "eq" else not equal
    if kind == "func":
        return _STATUS_FUNCTIONS[node[1]](ctx)
    if kind == "path":
        return _resolve(node[1], ctx)
    raise ValueError(f"cannot evaluate node {node!r}")


def _strip_expression(raw: str) -> str:
    """``if:`` may be a bare expression or one wrapped in ``${{ }}``."""
    text = str(raw).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text


def _condition_text(jobs: dict, job_id: str) -> str:
    job = jobs.get(job_id)
    if not isinstance(job, dict):
        return "<no such job>"
    condition = job.get("if")
    if condition is None:
        return "<no if: — the job runs unconditionally>"
    return str(condition)


def _job_starts(
    jobs: dict,
    job_id: str,
    event_name: str,
    layer: Optional[str] = None,
) -> bool:
    """Would this job start under this event?

    GitHub starts a job whose ``if:`` evaluates truthy. A job with no
    ``if:`` at all starts unconditionally — that is GitHub's rule, and it
    is the rule this gate has to respect: a job that lost its condition
    does not stop running, it starts running always.

    The ``needs`` results are modelled as ``success``: the contract is
    about *which* job starts for an event, not about how a dependency
    that failed would change the answer.
    """
    job = jobs.get(job_id)
    if not isinstance(job, dict):
        pytest.fail(
            f"ci.yml has no `{job_id}` job. The dispatch contract is "
            f"written against it, so the contract cannot be checked."
        )
    condition = job.get("if")
    if condition is None:
        return True
    if isinstance(condition, bool):
        return condition

    declared = job.get("needs") or []
    if isinstance(declared, str):
        declared = [declared]
    ctx = _Context(
        event_name=event_name,
        layer=layer if event_name == "workflow_dispatch" else None,
        needs={name: "success" for name in declared},
    )

    expression = _strip_expression(condition)
    try:
        node = _Parser(_tokenize(expression)).parse()
    except ValueError as exc:
        pytest.fail(
            f"`{job_id}` has an `if:` this gate cannot read: {exc}. "
            f"The condition is {condition!r}."
        )
    try:
        value = _evaluate(node, ctx)
    except ValueError as exc:
        pytest.fail(f"`{job_id}`: {exc}. The condition is {condition!r}.")
    return _truthy(value)


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


class TestTheNightlyIsNotPartOfAnOrdinaryDispatch:
    """``layer=all`` is the repo-wide gate's own dispatch.

    It asks for the whole suite. The nightly is not part of the whole
    suite: it is the cron run, capped and scheduled, and pointing it at
    the runner pool from a hand-run is the failure the layer input was
    introduced to stop.
    """

    def test_layer_all_does_not_start_the_nightly(self, jobs: dict) -> None:
        assert not _job_starts(jobs, NIGHTLY_JOB, "workflow_dispatch", LAYER_ALL), (
            "a dispatch with layer=all still starts `nightly-regression` "
            f"(if={_condition_text(jobs, NIGHTLY_JOB)!r}) — the ordinary "
            "full-suite dispatch would start the nightly job as well, so "
            "the nightly's runner-pool problem comes back with every "
            "hand-run"
        )

    def test_layer_nightly_starts_the_nightly(self, jobs: dict) -> None:
        assert _job_starts(jobs, NIGHTLY_JOB, "workflow_dispatch", LAYER_NIGHTLY), (
            "a dispatch with layer=nightly does not start `nightly-regression` "
            f"(if={_condition_text(jobs, NIGHTLY_JOB)!r}) — the manual "
            "nightly has no way to run, so the cron trigger is the only "
            "thing left that can check the runner pool"
        )

    def test_the_schedule_still_starts_the_nightly(self, jobs: dict) -> None:
        assert _job_starts(jobs, NIGHTLY_JOB, "schedule"), (
            "the `schedule` event no longer starts `nightly-regression` "
            f"(if={_condition_text(jobs, NIGHTLY_JOB)!r}) — the repository "
            "has no nightly signal left, and the loss is invisible: a "
            "scheduled run that skips its job is a green run"
        )


class TestTheNightlyDispatchRunsNothingElse:
    """``layer=nightly`` asks for the nightly, and only the nightly.

    The four lanes below are the merge gate's inputs. Running them
    alongside a nightly check turns a check of the runner pool into a
    full-suite run, which is the cost the manual nightly exists to avoid.
    """

    @pytest.mark.parametrize("job_id", MERGE_GATE_JOBS)
    def test_layer_nightly_does_not_start_it(self, jobs: dict, job_id: str) -> None:
        assert not _job_starts(jobs, job_id, "workflow_dispatch", LAYER_NIGHTLY), (
            f"a dispatch with layer=nightly still starts `{job_id}` "
            f"(if={_condition_text(jobs, job_id)!r}) — the nightly check "
            "drags a merge-gate lane along with it"
        )


class TestANamedLayerStartsExactlyItsOwnLane:
    """A named layer starts its own lane, and none of the other three.

    The layer input exists so a maintainer can run *one* lane by hand.
    That promise is only worth something if the lane the layer names is
    the lane that starts -- so the contract is checked by name, in both
    directions: the layer's own lane starts, and the other three do
    not.

    Checking only the first half would be satisfied by a condition that
    ignores the layer and starts everything; checking only the second
    half would be satisfied by one that starts nothing.  Both halves
    together are what "exactly its own lane" means.
    """

    @pytest.mark.parametrize("layer,job_id", sorted(LAYER_JOBS.items()))
    def test_the_named_layer_starts_its_own_lane(
        self, jobs: dict, layer: str, job_id: str
    ) -> None:
        assert _job_starts(jobs, job_id, "workflow_dispatch", layer), (
            f"a dispatch with layer={layer} does not start `{job_id}` "
            f"(if={_condition_text(jobs, job_id)!r}) — the lane the layer "
            "names is the lane that is supposed to run, so asking for it "
            "by name runs nothing"
        )

    @pytest.mark.parametrize("layer,job_id", sorted(LAYER_JOBS.items()))
    def test_the_named_layer_starts_no_other_lane(
        self, jobs: dict, layer: str, job_id: str
    ) -> None:
        for other in MERGE_GATE_JOBS:
            if other == job_id:
                continue
            assert not _job_starts(jobs, other, "workflow_dispatch", layer), (
                f"a dispatch with layer={layer} starts `{other}` as well as "
                f"`{job_id}` (if={_condition_text(jobs, other)!r}) — a "
                "named layer is meant to select one lane, so the other "
                "three start as well and the hand-run costs the whole "
                "merge gate"
            )

    def test_layer_all_starts_every_merge_gate_lane(self, jobs: dict) -> None:
        for job_id in MERGE_GATE_JOBS:
            assert _job_starts(jobs, job_id, "workflow_dispatch", LAYER_ALL), (
                f"a dispatch with layer={LAYER_ALL} does not start "
                f"`{job_id}` (if={_condition_text(jobs, job_id)!r}) — the "
                "repo-wide dispatch is the whole merge gate, so a lane it "
                "does not start is a lane the gate never sees"
            )


class TestAPullRequestStillRunsEveryMergeGateLane:
    """The other half of the same narrowing: it must not overreach.

    Without this half, the assertions above would also be satisfied by a
    condition that simply stopped everything — the four lanes would never
    run for a pull request, and the merge gate would see a tree it never
    looked at and call it green.
    """

    @pytest.mark.parametrize("job_id", MERGE_GATE_JOBS)
    def test_pull_request_still_starts_it(self, jobs: dict, job_id: str) -> None:
        assert _job_starts(jobs, job_id, "pull_request"), (
            f"a pull request no longer starts `{job_id}` "
            f"(if={_condition_text(jobs, job_id)!r}) — the merge gate's "
            "input stops arriving, and a missing lane is a green gate "
            "rather than a red one"
        )


class TestTheLayerInputIsActuallyOffered:
    """The contract is about a dispatch input, so the input has to exist.

    A condition that reads ``layer == 'nightly'`` is vacuous if the
    dispatch does not offer ``nightly`` — the branch would be true for
    a value nobody can select. That is a silent failure: the manual
    nightly would have no way to run, and the workflow would still look
    correct.
    """

    def _layer_input(self, workflow: dict) -> dict:
        triggers = workflow.get("on", workflow.get(True))
        assert isinstance(triggers, dict), (
            "ci.yml does not declare its triggers, so the dispatch input "
            "cannot be checked"
        )
        dispatch = triggers.get("workflow_dispatch")
        assert isinstance(dispatch, dict), (
            "ci.yml no longer declares a `workflow_dispatch` trigger; the "
            "layer input has no other way to reach any job"
        )
        inputs = dispatch.get("inputs") or {}
        layer = inputs.get("layer")
        assert isinstance(layer, dict), (
            "the `workflow_dispatch` trigger declares no `layer` input; "
            "the dispatch contract is written against one"
        )
        return layer

    def test_the_layer_input_offers_all_and_nightly(self, workflow: dict) -> None:
        layer = self._layer_input(workflow)
        options = layer.get("options") or []
        assert LAYER_ALL in options, (
            f"the `layer` input no longer offers {LAYER_ALL!r} "
            f"(options={options!r}); the repo-wide dispatch has no way to "
            "ask for the whole suite"
        )
        assert LAYER_NIGHTLY in options, (
            f"the `layer` input no longer offers {LAYER_NIGHTLY!r} "
            f"(options={options!r}); the manual nightly has no way to run"
        )

    def test_the_layer_input_offers_every_named_layer(self, workflow: dict) -> None:
        layer = self._layer_input(workflow)
        options = layer.get("options") or []
        for name in LAYER_JOBS:
            assert name in options, (
                f"the `layer` input no longer offers {name!r} "
                f"(options={options!r}); the lane {LAYER_JOBS[name]!r} has "
                "no way to be asked for by name"
            )

    def test_the_layer_input_defaults_to_all(self, workflow: dict) -> None:
        layer = self._layer_input(workflow)
        assert layer.get("default") == LAYER_ALL, (
            "the `layer` input no longer defaults to "
            f"{LAYER_ALL!r} (default={layer.get('default')!r}); a dispatch "
            "that names no layer would start something other than the "
            "repo-wide suite"
        )
