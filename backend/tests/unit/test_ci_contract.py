"""Unit tests for the per-plan CI contract reader (``backend/ci_contract.py``).

The contract lets a plan say what its gate's exit codes mean, where its
budget comes from and how its summary looks.  Everything here pins the
same promise: **a broken contract never changes the verdict** — it falls
back to the framework defaults and says so in the log.

Covers:
  * the no-contract path — ``plan_dir=None`` and a plan directory without
    a contract file both land on the framework defaults;
  * a fully declared contract — every field is read as declared;
  * a contract that exists but declares no summary format — no summary is
    parsed, because a contract that says nothing about the format must
    not be read through the framework's own format;
  * broken JSON, a non-object payload, an unreadable file, and fields of
    the wrong type — each one falls back and logs a warning instead of
    raising.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import ci_contract  # noqa: E402
from ci_contract import (  # noqa: E402
    CONTRACT_FILENAME,
    FRAMEWORK_INCONCLUSIVE_EXIT_CODES,
    CiContract,
    load_ci_contract,
)


def _write_contract(plan_dir: Path, payload: str) -> Path:
    """Write ``payload`` verbatim as the plan's contract file."""
    path = plan_dir / CONTRACT_FILENAME
    path.write_text(payload, encoding="utf-8")
    return path


def _warnings(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


# --------------------------------------------------------------------------
# No contract at all -> the framework defaults
# --------------------------------------------------------------------------


def test_no_plan_dir_uses_framework_defaults():
    """No plan directory at all -> the framework defaults, untouched.

    This is the zero-regression path: a runner that is not given a plan
    directory must behave exactly like it did before contracts existed.
    """
    contract = load_ci_contract(None)

    assert contract.applied is False
    assert contract.inconclusive_exit_codes == FRAMEWORK_INCONCLUSIVE_EXIT_CODES
    assert contract.inconclusive_exit_codes == (3,)
    assert contract.budget_source == "gate_script"
    assert contract.budget_seconds is None
    assert contract.summary_format == "gate_v1"
    assert contract.path is None
    assert contract.error is None


def test_missing_contract_file_uses_framework_defaults(tmp_path):
    """A plan directory without a contract file -> the framework defaults.

    The default summary format stays ``gate_v1`` here: the framework
    parses its own gate's summary exactly as it did before.
    """
    contract = load_ci_contract(tmp_path)

    assert contract.applied is False
    assert contract.inconclusive_exit_codes == (3,)
    assert contract.budget_source == "gate_script"
    assert contract.budget_seconds is None
    assert contract.summary_format == "gate_v1"
    assert contract.path is None
    assert contract.error is None


# --------------------------------------------------------------------------
# A declared contract
# --------------------------------------------------------------------------


def test_a_declared_contract_reads_every_field(tmp_path):
    """A fully declared contract is read field by field, as declared."""
    _write_contract(
        tmp_path,
        json.dumps(
            {
                "inconclusive_exit_codes": [7],
                "budget_source": "contract",
                "budget_seconds": 900,
                "summary_format": "gate_v1",
            }
        ),
    )

    contract = load_ci_contract(tmp_path)

    assert contract.inconclusive_exit_codes == (7,)
    assert contract.budget_source == "contract"
    assert contract.budget_seconds == 900
    assert contract.summary_format == "gate_v1"
    assert contract.applied is True
    assert contract.path == tmp_path / CONTRACT_FILENAME
    assert contract.error is None


def test_an_absent_summary_format_means_no_parsing(tmp_path):
    """A contract that says nothing about the format -> no summary parsing.

    The contract is present, so its silence is a decision: the contract
    does not describe the gate's output, and reading the output through
    the framework's own format would be a guess.
    """
    _write_contract(tmp_path, json.dumps({"inconclusive_exit_codes": [7]}))

    contract = load_ci_contract(tmp_path)

    assert contract.summary_format == "none"
    assert contract.parses_summary() is False
    assert contract.inconclusive_exit_codes == (7,)
    assert contract.budget_source == "gate_script"
    assert contract.budget_seconds is None
    assert contract.applied is True


def test_a_contract_without_any_field_uses_the_contract_defaults(tmp_path):
    """An empty contract object still *is* a contract: no format, default codes."""
    _write_contract(tmp_path, "{}")

    contract = load_ci_contract(tmp_path)

    assert contract.applied is True
    assert contract.inconclusive_exit_codes == (3,)
    assert contract.budget_source == "gate_script"
    assert contract.budget_seconds is None
    assert contract.summary_format == "none"


def test_only_gate_v1_parses_the_summary(tmp_path):
    """``parses_summary()`` is true for exactly one format."""
    _write_contract(tmp_path, json.dumps({"summary_format": "gate_v1"}))
    assert load_ci_contract(tmp_path).parses_summary() is True

    _write_contract(tmp_path, json.dumps({"summary_format": "none"}))
    assert load_ci_contract(tmp_path).parses_summary() is False


# --------------------------------------------------------------------------
# A broken contract falls back and says so
# --------------------------------------------------------------------------


def test_broken_json_falls_back_and_logs(tmp_path, caplog):
    """Unparseable JSON -> framework defaults, an error and a warning."""
    _write_contract(tmp_path, "{oops")

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.applied is False
    assert contract.error
    assert contract.inconclusive_exit_codes == (3,)
    assert contract.summary_format == "gate_v1"
    assert contract.path == tmp_path / CONTRACT_FILENAME
    assert _warnings(caplog), "a broken contract must be said out loud"


def test_a_contract_that_is_not_an_object_falls_back_and_logs(tmp_path, caplog):
    """A JSON payload that is not an object -> framework defaults + warning."""
    _write_contract(tmp_path, "[7]")

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.applied is False
    assert contract.error
    assert contract.inconclusive_exit_codes == (3,)
    assert _warnings(caplog)


def test_an_unreadable_contract_falls_back_and_logs(tmp_path, caplog):
    """A contract file that cannot be read -> framework defaults + warning."""
    # A directory where the file should be: it exists, and reading it fails.
    (tmp_path / CONTRACT_FILENAME).mkdir()

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.applied is False
    assert contract.error
    assert contract.inconclusive_exit_codes == (3,)
    assert _warnings(caplog)


def test_a_contract_that_cannot_be_stat_ed_falls_back_and_logs(tmp_path, caplog):
    """A plan directory that cannot even be listed -> defaults + warning.

    ``Path.exists()`` swallows a handful of ``os.stat`` errno values
    (ENOENT, ENOTDIR, EBADF, ENAMETOOLONG) and re-raises everything
    else — an EACCES on the plan directory, for instance.  Without a
    handler of its own that error escapes the reader, and a contract
    nobody is allowed to read takes the gate down with it.

    The plan directory is locked with mode 000, which is what makes the
    ``stat`` fail with EACCES.  Root may stat it anyway, so there the
    case cannot be produced.
    """
    plan_dir = tmp_path / "locked"
    plan_dir.mkdir()
    (plan_dir / CONTRACT_FILENAME).write_text("{}", encoding="utf-8")

    if os.geteuid() == 0:
        pytest.skip("root may stat a mode-000 directory")

    os.chmod(plan_dir, 0)
    try:
        with caplog.at_level(logging.WARNING):
            contract = load_ci_contract(plan_dir)
    finally:
        # The mode is restored to what the owner needs and nothing more.
        # "World readable" would say the plan directory is for every
        # account on the machine, which it is not — and the static
        # security scan says so (B103).  The owner keeps every bit it
        # had, so the temporary directory is still cleaned up.
        os.chmod(plan_dir, 0o700)

    # A contract that cannot be read is **not** the same thing as a plan
    # without a contract: the file may well be there, the reader simply
    # cannot tell.  So the path is named, the reason is recorded and the
    # failure is said out loud.
    assert contract.applied is False
    assert contract.path == plan_dir / CONTRACT_FILENAME
    assert contract.error
    assert contract.inconclusive_exit_codes == (3,)
    assert contract.summary_format == "gate_v1"
    assert _warnings(caplog)


def test_a_contract_the_decoder_cannot_parse_falls_back_and_logs(
    tmp_path, caplog, monkeypatch
):
    """A decoder failure that is not a ValueError -> defaults + warning.

    ``json.loads`` reports a payload nested past the recursion limit with
    ``RecursionError``, which is not a ``ValueError``: without a handler of
    its own it escapes the reader, and a broken contract would take the
    gate down with it.
    """
    _write_contract(tmp_path, "{}")

    def _too_deep(_raw):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ci_contract.json, "loads", _too_deep)

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.applied is False
    assert contract.error
    assert contract.inconclusive_exit_codes == (3,)
    assert _warnings(caplog)


def test_a_deeply_nested_contract_falls_back_and_logs(tmp_path, caplog):
    """A contract nested past the decoder's depth limit -> defaults + warning."""
    _write_contract(tmp_path, "[" * 20000 + "]" * 20000)

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.applied is False
    assert contract.error
    assert contract.inconclusive_exit_codes == (3,)
    assert _warnings(caplog)


def test_invalid_inconclusive_exit_codes_fall_back_and_log(tmp_path, caplog):
    """A codes field of the wrong type -> the default codes + a warning."""
    _write_contract(tmp_path, json.dumps({"inconclusive_exit_codes": "3"}))

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.inconclusive_exit_codes == (3,)
    assert _warnings(caplog)


def test_a_negative_exit_code_falls_back_and_log(tmp_path, caplog):
    """A negative exit code is not an exit code -> the default codes."""
    _write_contract(tmp_path, json.dumps({"inconclusive_exit_codes": [-1]}))

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.inconclusive_exit_codes == (3,)
    assert _warnings(caplog)


def test_a_boolean_exit_code_falls_back_and_log(tmp_path, caplog):
    """``True`` is an ``int`` in Python, but it is not an exit code."""
    _write_contract(tmp_path, json.dumps({"inconclusive_exit_codes": [True]}))

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.inconclusive_exit_codes == (3,)
    assert _warnings(caplog)


def test_invalid_budget_seconds_fall_back_and_log(tmp_path, caplog):
    """A budget that is not a number -> no budget + a warning.

    The type is checked whether or not the budget is the one in force: a
    wrong-typed field is a mistake worth saying out loud either way.
    """
    _write_contract(tmp_path, json.dumps({"budget_seconds": "soon"}))

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.budget_seconds is None
    assert _warnings(caplog)


def test_an_invalid_contract_budget_falls_back_and_logs(tmp_path, caplog):
    """The same, with the budget actually in force."""
    _write_contract(
        tmp_path,
        json.dumps({"budget_source": "contract", "budget_seconds": "soon"}),
    )

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.budget_seconds is None
    assert _warnings(caplog)


def test_a_contract_budget_must_be_a_positive_integer(tmp_path, caplog):
    """Zero and negative budgets are not budgets -> no budget + a warning."""
    _write_contract(
        tmp_path,
        json.dumps({"budget_source": "contract", "budget_seconds": 0}),
    )

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.budget_seconds is None
    assert _warnings(caplog)


def test_an_unknown_summary_format_falls_back_to_none_and_logs(tmp_path, caplog):
    """An unknown summary format -> no parsing, and a warning."""
    _write_contract(tmp_path, json.dumps({"summary_format": "gate_v9"}))

    with caplog.at_level(logging.WARNING):
        contract = load_ci_contract(tmp_path)

    assert contract.summary_format == "none"
    assert contract.parses_summary() is False
    assert _warnings(caplog)


def test_budget_seconds_only_takes_effect_under_the_contract_source(tmp_path):
    """A budget that is not sourced from the contract does not take effect."""
    _write_contract(
        tmp_path,
        json.dumps({"budget_source": "gate_script", "budget_seconds": 900}),
    )

    contract = load_ci_contract(tmp_path)

    assert contract.budget_source == "gate_script"
    assert contract.budget_seconds is None


# --------------------------------------------------------------------------
# The evidence
# --------------------------------------------------------------------------


def test_the_evidence_is_json_serialisable_and_carries_every_field(tmp_path):
    """``to_evidence()`` is what lands in the audit artifact: it must be JSON."""
    _write_contract(
        tmp_path,
        json.dumps(
            {
                "inconclusive_exit_codes": [7],
                "budget_source": "contract",
                "budget_seconds": 900,
                "summary_format": "gate_v1",
            }
        ),
    )

    evidence = load_ci_contract(tmp_path).to_evidence()

    assert set(evidence) >= {
        "path",
        "applied",
        "inconclusive_exit_codes",
        "budget_source",
        "budget_seconds",
        "summary_format",
        "error",
    }
    assert evidence["applied"] is True
    assert evidence["inconclusive_exit_codes"] == [7]
    assert evidence["budget_source"] == "contract"
    assert evidence["budget_seconds"] == 900
    assert evidence["summary_format"] == "gate_v1"
    assert evidence["path"] == str(tmp_path / CONTRACT_FILENAME)
    assert json.dumps(evidence)


def test_the_default_contract_can_be_built_without_arguments():
    """``CiContract()`` is the framework default, and it is frozen."""
    contract = CiContract()

    assert contract.applied is False
    assert contract.inconclusive_exit_codes == (3,)
    assert contract.budget_source == "gate_script"
    assert contract.budget_seconds is None
    assert contract.summary_format == "gate_v1"
    assert contract.path is None
    assert contract.error is None

    with pytest.raises(Exception):
        contract.budget_source = "contract"  # type: ignore[misc]


def test_the_module_exposes_the_documented_constants():
    """The contract of the module itself: filename + framework exit codes."""
    assert CONTRACT_FILENAME == "ci_contract.json"
    assert FRAMEWORK_INCONCLUSIVE_EXIT_CODES == (3,)
    assert ci_contract.CONTRACT_FILENAME == "ci_contract.json"