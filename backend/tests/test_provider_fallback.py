"""VP-015: Provider Order Config Loading & Fallback.

This module pins the contract that:

  1. ``provider_order.load_fallback_order`` reads a valid
     ``provider-order.json`` and returns its ``order`` array, filtered
     through the CC Switch consumer layer against the provider names
     actually present in the database.

  2. ``ClaudeCodingTool._run_claude_interactive`` walks
     ``self.provider_priority`` and takes the first provider whose row is
     usable — an entry that cannot serve the call is skipped and the
     search continues, rather than ending it.

  3. When every entry in the chain is unusable the dispatch does NOT
     raise: it falls back to the parent process configuration and logs
     ``provider_parent_fallback`` naming the provider that should have
     served the call.

The ``-k 'load or fallback_chain or exhausted'`` filter on the
verification command selects the three tests below plus the re-exported
load tests from :mod:`backend.tests.test_provider_order`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coding_tool import ClaudeCodingTool
from provider_order import (
    SCHEMA_VERSION,
    load_fallback_order,
)

# Re-export the load tests from the canonical test module so the
# ``-k load`` half of the verification filter has coverage in BOTH files
# (test_command includes both paths).
from backend.tests.test_provider_order import (  # noqa: F401
    test_load_order_returns_display_names,
    test_load_fallback_order_returns_json_order,
)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _write_payload(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_fake_cc_switch_db(tmp_path: Path, provider_names):
    """Create a fake CC Switch DB declaring *provider_names*.

    Production ``providers`` schema — the name is what the backend
    resolves providers by, and what a ``provider-order.json`` chain
    carries.
    """
    db_path = tmp_path / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE providers ("
            "id TEXT PRIMARY KEY, name TEXT, settings_config TEXT"
            ")"
        )
        for i, name in enumerate(provider_names):
            conn.execute(
                "INSERT INTO providers (id, name, settings_config) "
                "VALUES (?, ?, ?)",
                (
                    f"row-{i}",
                    name,
                    json.dumps({
                        "env": {
                            "ANTHROPIC_BASE_URL": f"https://{i}.example.com/v1",
                            "ANTHROPIC_AUTH_TOKEN": f"sk-{i}",
                        }
                    }),
                ),
            )
        conn.commit()
    finally:
        conn.close()

    cc_dir = tmp_path / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path.replace(cc_dir / "cc-switch.db")
    return cc_dir / "cc-switch.db"


def _make_ok_proc() -> MagicMock:
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdout = iter([
        json.dumps({"type": "system", "subtype": "init", "session_id": "x"}),
        json.dumps({"type": "result", "result": "ok", "is_error": False}),
    ])
    proc.stderr.read.return_value = ""
    proc.wait.return_value = 0
    return proc


def _run_dispatch(tool, availability: dict):
    """Run one dispatch against a synthetic availability table.

    ``availability`` maps a provider name to the ``(available, config)``
    the checker answers for it; a name that is absent from the table is
    unavailable. CC Switch's "current provider" shortcut is pinned
    closed so the machine a test runs on cannot decide the outcome.
    """
    captured = []

    def _check(name):
        return availability.get(name, (False, {}))

    with patch.object(
        ClaudeCodingTool, "_check_provider_availability", staticmethod(_check)
    ), patch.object(
        ClaudeCodingTool, "_load_cc_switch_current_provider",
        staticmethod(lambda: None),
    ), patch(
        "coding_tool.subprocess.Popen",
        side_effect=lambda cmd, **kw: captured.append(
            {"cmd": cmd, "env": kw.get("env", {})}
        ) or _make_ok_proc(),
    ):
        tool._run_claude_interactive("hi")
    return captured


# ---------------------------------------------------------------------------
# Tests (filter: load OR fallback_chain OR exhausted)
# ---------------------------------------------------------------------------


def test_load_chain_supports_fallback_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``load_fallback_order`` returns the multi-provider chain the
    runtime walk will follow.

    The chain carries CC Switch provider names — the key
    ``get_provider`` reads by — so the JSON and the DB
    have to agree on the spelling for anything to survive the filter.
    """
    chain = ["Vendor A Pro", "Vendor B Pro", "Vendor C App"]
    _make_fake_cc_switch_db(tmp_path, chain)
    monkeypatch.setenv("HOME", str(tmp_path))

    target = tmp_path / "provider-order.json"
    _write_payload(target, {
        "version": SCHEMA_VERSION,
        "updated_at": _now_iso(),
        "source": "producer",
        "order": chain,
        "providers": {},
    })

    result = load_fallback_order(target)
    assert result == chain
    assert len(result) >= 2, "fallback_chain requires >=2 providers"


def test_fallback_chain_skips_unusable_primary_and_takes_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable head of the chain falls through to the next entry.

    The walk's whole purpose: the first preference may have no usable
    row (removed in CC Switch, missing credentials), and that must not
    end the search.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    tool = ClaudeCodingTool(
        provider_priority=["Vendor A Pro", "Vendor B Pro"],
    )

    calls = _run_dispatch(tool, {
        "Vendor B Pro": (
            True,
            {"base_url": "https://vendor-b.test/v1", "api_key": "sk-vendor-b",
             "models": {}},
        ),
    })

    assert tool.current_call_provider == "Vendor B Pro", (
        f"the unusable primary must fall through to the next chain entry; "
        f"selected={tool.current_call_provider!r}"
    )
    assert calls[0]["env"]["ANTHROPIC_BASE_URL"] == "https://vendor-b.test/v1"


def test_exhausted_chain_falls_back_to_parent_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully unusable chain degrades to the parent env, and says so.

    There is no chain-exhausted exception: the parent process
    configuration (without CC Switch, the user's own Claude Code
    settings) is a valid answer, and the run must not die because a
    provider list went stale. What must NOT be silent is the
    degradation — the warning names the provider that should have
    served the call, which is what makes missing parent quota
    attributable.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    logger = MagicMock()
    tool = ClaudeCodingTool(
        logger=logger,
        provider_priority=["Vendor A Pro", "Vendor B Pro"],
    )

    calls = _run_dispatch(tool, {})  # nothing available

    assert tool.current_call_provider == "parent"
    assert calls, "the dispatch must still spawn a subprocess"

    warnings = [
        c for c in logger.warning.call_args_list
        if c.args and c.args[0] == "provider_parent_fallback"
    ]
    assert warnings, (
        "an exhausted chain must log provider_parent_fallback; got "
        f"{[c.args[0] for c in logger.warning.call_args_list if c.args]!r}"
    )
    data = warnings[0].kwargs.get("data", {})
    assert data.get("intended_provider") == "Vendor A Pro", (
        "the warning must name who should have served the call; got "
        f"{data!r}"
    )
