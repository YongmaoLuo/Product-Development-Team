"""Unit tests for the ``excluded_providers`` pass-through on ``query_json``.

``_query_json_internal`` has always known how to skip a provider
(``excluded = set(excluded_providers or [])`` is where the choice is
made), but the public entry point ``ClaudeCodingTool.query_json`` did
not expose the knob — so a caller that had just watched one provider
fail had no way to say "not that one again" and let the retry pick a
different one.

These tests pin the pass-through contract:

  * an explicit list reaches the internal call unchanged;
  * the default is ``None`` — the old behaviour, byte for byte;
  * an empty list is normalized to ``None`` (an empty exclusion set
    must not change which provider is picked);
  * a tuple or a set is forwarded as given — the internal call does its
    own ``set()``-ing, so the public entry point does not need to
    rewrite the caller's container;
  * ``allowed_tools`` / ``scene`` / ``retry_count`` keep flowing;
  * the scripted double still answers from its fixture and never
    reaches the internal call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


def _make_tool(monkeypatch):
    """A bare ``ClaudeCodingTool`` whose ``_query_json_internal`` is captured.

    ``object.__new__`` skips ``__init__`` — the constructor reads the
    config, the keychain and the model map, none of which this
    contract needs.  What the dispatch touches before it hands over to
    the internal call is ``_quota_readmitted_providers``, so that one
    attribute is provided by hand.

    Returns the tool and the dict the capture writes into.
    """
    import coding_tool as ct

    tool = object.__new__(ct.ClaudeCodingTool)
    tool._quota_readmitted_providers = set()

    captured = {}

    def _capture(prompt, system_instruction, timeout, **kwargs):
        captured["positional"] = (prompt, system_instruction, timeout)
        captured["kwargs"] = kwargs
        return {"captured": True}

    tool._query_json_internal = _capture
    # The scripted double answers before the dispatch does; these tests
    # exercise the live path, so the double is switched off.
    monkeypatch.setattr(ct, "scripted_mode_enabled", lambda: False)
    return tool, captured


def test_excluded_providers_forwarded(monkeypatch):
    """An explicit exclusion list reaches the internal call unchanged."""
    tool, captured = _make_tool(monkeypatch)

    tool.query_json(
        prompt="p",
        system_instruction="s",
        excluded_providers=["vendor-a-pro"],
    )

    assert captured["kwargs"]["excluded_providers"] == ["vendor-a-pro"]


def test_default_is_none(monkeypatch):
    """Not passing the argument is the old behaviour: ``None``."""
    tool, captured = _make_tool(monkeypatch)

    tool.query_json(prompt="p", system_instruction="s")

    assert captured["kwargs"]["excluded_providers"] is None


def test_empty_list_becomes_none(monkeypatch):
    """An empty list excludes nobody, so it is normalized to ``None``."""
    tool, captured = _make_tool(monkeypatch)

    tool.query_json(
        prompt="p",
        system_instruction="s",
        excluded_providers=[],
    )

    assert captured["kwargs"]["excluded_providers"] is None


@pytest.mark.parametrize(
    "value",
    [("vendor-a-pro", "vendor-b-pro"), {"vendor-a-pro", "vendor-b-pro"}],
    ids=["tuple", "set"],
)
def test_non_list_containers_pass_through_unchanged(monkeypatch, value):
    """A tuple or a set is forwarded as given.

    The internal call does its own ``set()``-ing; rewriting the
    caller's container here would only lose the shape it was given.
    """
    tool, captured = _make_tool(monkeypatch)

    tool.query_json(
        prompt="p",
        system_instruction="s",
        excluded_providers=value,
    )

    assert captured["kwargs"]["excluded_providers"] == value


def test_existing_kwargs_still_forwarded(monkeypatch):
    """``allowed_tools`` / ``scene`` / ``retry_count`` do not regress."""
    tool, captured = _make_tool(monkeypatch)

    tool.query_json(
        prompt="p",
        system_instruction="s",
        allowed_tools=["Read", "Grep"],
        scene="refiner",
    )

    kwargs = captured["kwargs"]
    assert kwargs["allowed_tools"] == ["Read", "Grep"]
    assert kwargs["scene"] == "refiner"
    assert kwargs["retry_count"] == 0
    assert kwargs["excluded_providers"] is None


def test_scripted_branch_answers_before_the_dispatch(tmp_path, monkeypatch):
    """The scripted double answers from its fixture, dispatch untouched.

    The scripted branch sits in front of the forwarding, so it must
    return the fixture answer and never reach ``_query_json_internal``.
    """
    import coding_tool as ct

    script_dir = tmp_path / "responses"
    script_dir.mkdir()
    (script_dir / "registry.json").write_text(
        json.dumps({"prompts": {"answer": "p"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (script_dir / "answer.txt").write_text('{"a": 1}', encoding="utf-8")
    monkeypatch.setenv("PDT_LLM_MODE", "scripted")
    monkeypatch.setenv("PDT_LLM_SCRIPT_DIR", str(script_dir))

    tool = object.__new__(ct.ClaudeCodingTool)
    tool._quota_readmitted_providers = set()
    reached = []
    tool._query_json_internal = lambda *a, **kw: reached.append((a, kw))

    result = tool.query_json(
        prompt="p",
        system_instruction="s",
        excluded_providers=["vendor-a-pro"],
    )

    assert result == {"a": 1}
    assert reached == []


def test_scripted_double_accepts_the_same_keyword_surface(tmp_path, monkeypatch):
    """The scripted double's keyword surface mirrors the live tool's.

    Its own docstring promises the two cannot be told apart by the
    call they make, so a caller that passes ``excluded_providers`` must
    not blow up on the double.
    """
    import coding_tool as ct

    script_dir = tmp_path / "responses"
    script_dir.mkdir()
    (script_dir / "registry.json").write_text(
        json.dumps({"prompts": {"answer": "p"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (script_dir / "answer.txt").write_text('{"a": 1}', encoding="utf-8")
    monkeypatch.setenv("PDT_LLM_MODE", "scripted")
    monkeypatch.setenv("PDT_LLM_SCRIPT_DIR", str(script_dir))

    tool = ct.ScriptedCodingTool(script_dir=str(script_dir))

    result = tool.query_json(
        prompt="p",
        system_instruction="s",
        excluded_providers=["vendor-a-pro"],
    )

    assert result == {"a": 1}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))