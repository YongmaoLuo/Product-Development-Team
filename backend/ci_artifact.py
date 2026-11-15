"""The one contract for the CI artifact (``ci_output.json``).

2026-11-09 — the contract for the CI artifact was written down **three
times**: once by the producer (the evidence dictionaries
``run_ci_verification`` builds), once by the consumer (the orchestrator
that reads the artifact back), and once by the tests that pinned both.
Three copies of one contract drift, and the drift only shows up in the
whole-repository gate — long after the thing that drifted was written.

This module is the single copy.  It states the field set once and
offers one pure function that says whether a payload obeys it.  It
never reads a file, never touches the runner and never mutates what it
is given: a contract that has side effects is not a contract.

What the field set is
--------------------

:data:`CI_ARTIFACT_FIELDS` is taken **verbatim** from the two evidence
dictionaries ``run_ci_verification`` builds
(``backend/verification_ci_runner.py``) — the schema-failure path and
the normal path.  Nothing is added and nothing is guessed: a field the
runner does not write is not in the contract, and a field the runner
writes under another name is not in it either.

The keys are spelled out **literally** on both sides — here and in the
runner's evidence dictionaries — because the field set is not copied
between them but **derived**: the pinning test reads the allowed keys
out of the runner's own source.  A key assembled from a constant
(``evidence[JUNIT_AVAILABLE_KEY] = ...``) is invisible to that
derivation, so it can never enter the contract — and a contract that
does not allow a key the producer writes makes the runner reject its
own artifact as carrying unexpected keys.  Spelled out, the two sets
stay equal in both directions.

Two names, one slot
-------------------

``ci_entry`` and ``entry_command`` are the **same slot at runtime**.
The runner writes the entry under ``entry_key``, which is
``ENTRY_COMMAND_FIELD`` for an ``e2e`` command point and
``CI_ENTRY_FIELD`` for a ``full_ci`` gate.  Only one of the two
appears in any single artifact, and which one depends on the kind of
the entry — so **both** stay in the allowed set and neither is
required.  A contract that allowed only one of them would reject half
of the artifacts the runner writes.

What is required
----------------

:data:`CI_ARTIFACT_REQUIRED_FIELDS` is the three keys every artifact
carries on every path: which VP it is (``vp_id``), which file judged
it (``verifier_source``) and which cases failed (``failed_cases``).
The last two are there so that "the machine did not say" can never be
mistaken for "the machine said nothing failed" — the distinction the
repair loop depends on when it decides whether it is reading a defect
or a broken environment.

The round-copy extras
---------------------

``round`` and ``round_source`` are **not** part of the artifact the
runner writes: they are bookkeeping a round copies onto the payload
while it moves it around.  They are therefore rejected by default, and
:func:`validate_ci_artifact` lets them through only when the caller
says it is validating a round copy (``allow_round_fields=True``).

The messages
------------

Every problem comes back as one line of English that names what is
wrong: a contract that says "something is wrong" without saying what
is wrong sends the reader back to the source to find out.  The missing
keys come back in **one** message and the unexpected keys come back in
**one** message — a payload with three unknown keys is one problem,
not three.
"""

from __future__ import annotations

from typing import Any, FrozenSet, List

#: Every key a CI artifact may carry.
#:
#: Taken verbatim from the two evidence dictionaries
#: ``run_ci_verification`` builds.  ``ci_entry`` and ``entry_command``
#: are both here because they are one slot under two names — see the
#: module docstring.
CI_ARTIFACT_FIELDS: FrozenSet[str] = frozenset({
    "vp_id",
    "ci_entry",
    "entry_command",
    "exit_code",
    "timed_out",
    "duration_seconds",
    "timeout_seconds",
    "budget_exceeded",
    "stdout_head",
    "stdout_tail",
    "stderr_head",
    "stderr_tail",
    "lane_summaries",
    "ci_timeout_seconds",
    "gate_budget_seconds",
    "gate_summary",
    "spawned_processes_reaped",
    # 2026-11-10: the proof that this run left nothing behind — the
    # entries still registered are the processes this run could not give
    # back.  Both names are spelled out literally (see the module
    # docstring above).
    "spawned_processes_outstanding",
    "spawned_processes_all_given_back",
    "budget_exhausted_in",
    "gate_elapsed_seconds",
    "verifier_source",
    "error",
    "host_measurable",
    "signal",
    "junit_phases",
    "junit_failures",
    "junit_error",
    "junit_failures_truncated",
    # 2026-11-10: whether this run could produce a junit report at all,
    # why it could not, and which reports of an earlier round were
    # removed to start from a clean directory.
    "junit_available",
    "junit_unavailable_reason",
    "junit_stale_reports_removed",
    "failed_cases",
    "schema_issues",
    # The declaration this verdict was based on: which exit codes mean
    # "the gate could not decide", where its budget came from and
    # whether its output was parsed as the framework's summary format.
    # A plan that writes no CI contract gets the framework's default,
    # and this key says so rather than being absent.
    "ci_contract",
    # 2026-11-14: the **machine-written** identity of the failure — the
    # ids of what broke, spelled out by the validator rather than by a
    # model.  The runner writes it on both paths (the schema-failure
    # path and the normal path), so the contract has to allow it; the
    # key is spelled out literally on both sides so the derivation
    # below can see it.
    "failure_identity",
})

#: The keys every CI artifact carries, on every path.
CI_ARTIFACT_REQUIRED_FIELDS: FrozenSet[str] = frozenset({
    "vp_id",
    "verifier_source",
    "failed_cases",
})

#: The keys a round copy adds on top of the artifact it carries.
#:
#: They are bookkeeping, not evidence: the runner never writes them,
#: so an artifact that carries them is a round copy and has to be
#: validated as one.
ROUND_COPY_EXTRA_FIELDS: FrozenSet[str] = frozenset({
    "round",
    "round_source",
})


def validate_ci_artifact(
    payload: Any,
    *,
    allow_round_fields: bool = False,
) -> List[str]:
    """Say whether a CI artifact payload obeys the contract.

    Returns the problems as a list of one-line messages — an empty list
    means the payload obeys the contract.  The function is a pure
    function of its arguments: it does not read the artifact from disk,
    does not call the runner and does not change the payload.

    The checks, in the order they are reported:

    * **not a dict** — one issue naming the type that arrived.  A
      payload that is not a dict has no keys to check, so there is
      nothing else to say about it.
    * **missing keys** — one issue naming every required key the
      payload does not carry.
    * **unexpected keys** — one issue naming every key the payload
      carries that the contract does not allow.

    ``allow_round_fields`` widens the allowed set with
    :data:`ROUND_COPY_EXTRA_FIELDS` — pass it when the payload being
    checked is a round copy rather than the artifact the runner wrote.

    A validator does not get to crash on the payload it is handed.
    A key that is not a string is a key the contract does not allow,
    so it is reported as an unexpected key — with its ``repr``, so the
    message says what the key actually was — rather than crashing the
    caller on ``", ".join`` (which raises ``TypeError: sequence item
    0: expected str instance, int found``).
    """
    if not isinstance(payload, dict):
        return [
            f"ci artifact payload is not a dict: {type(payload).__name__}",
        ]

    allowed: FrozenSet[str] = CI_ARTIFACT_FIELDS
    if allow_round_fields:
        allowed = allowed | ROUND_COPY_EXTRA_FIELDS

    keys = set(payload.keys())
    issues: List[str] = []

    missing = sorted(CI_ARTIFACT_REQUIRED_FIELDS - keys)
    if missing:
        issues.append("missing keys: " + ", ".join(missing))

    # Keys the contract does not allow, split by type: the string ones
    # are joined as they are, the others come with their ``repr`` —
    # a validator that crashes on a hostile payload is no validator at
    # all.  The two groups are sorted separately because sorting a
    # mixed set raises ``TypeError: '<' not supported between
    # instances of 'int' and 'str'``.
    unexpected = keys - allowed
    unexpected_strings = sorted(k for k in unexpected if isinstance(k, str))
    unexpected_others = sorted(
        (k for k in unexpected if not isinstance(k, str)), key=repr
    )
    if unexpected_strings or unexpected_others:
        names = unexpected_strings + [repr(k) for k in unexpected_others]
        issues.append("unexpected keys: " + ", ".join(names))

    return issues


__all__ = [
    "CI_ARTIFACT_FIELDS",
    "CI_ARTIFACT_REQUIRED_FIELDS",
    "ROUND_COPY_EXTRA_FIELDS",
    "validate_ci_artifact",
]
