"""
Pulling the post-fix task list out of the tasks self-review reply.

Background (2026-09-21)
-----------------------
The self-review prompt tells the LLM to answer with::

    {"findings": [...], "fixed_content": <完整 tasks.json>}

``_extract_rewritten_tasks`` looked for ``tasks`` and ``fixed_tasks``
but **not** ``fixed_content``. A reply that followed the documented
schema therefore fell through to the ``"findings" in candidate`` branch
and returned the input *unchanged*.

The failure was invisible from every angle:

* ``rewrote`` was ``False`` — which reads as "the LLM found nothing to
  change", not as "the rewrite was dropped";
* ``findings`` was ``[]`` — they live on the wrapper, and the code read
  them off the tasks dict it had just discarded;
* the falsifiability iteration reported ``5 -> 5`` and stopped as
  "no progress", so the loop looked bounded and well-behaved.

Net effect: whenever the model complied with the prompt, the self-review
was a no-op. These tests pin the schema so a future refactor of the
prompt cannot silently desynchronise the extractor again.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from tasks_generator import TasksGenerator, _extract_first_json_object  # noqa: E402


ORIGINAL = {"requirement": "r", "tasks": [{"id": "1", "title": "ORIGINAL"}]}


def _extract(reply_text: str):
    return TasksGenerator._extract_rewritten_tasks(reply_text, fallback=ORIGINAL)


def _title(result) -> str:
    return result["tasks"][0]["title"]


# ---------------------------------------------------------------------------
# The shape the prompt asks for
# ---------------------------------------------------------------------------


def test_fixed_content_is_honoured():
    """The regression: this used to come back as ORIGINAL."""
    reply = json.dumps({
        "findings": [{"severity": "high", "type": "violation"}],
        "fixed_content": {
            "requirement": "r",
            "tasks": [{"id": "1", "title": "FIXED"}],
        },
    })
    assert _title(_extract(reply)) == "FIXED"


def test_fixed_content_wins_over_a_stale_tasks_key():
    """The fix is authoritative even if the wrapper echoes the input."""
    reply = json.dumps({
        "findings": [],
        "tasks": [{"id": "1", "title": "ECHO"}],
        "fixed_content": {
            "requirement": "r",
            "tasks": [{"id": "1", "title": "FIXED"}],
        },
    })
    assert _title(_extract(reply)) == "FIXED"


# ---------------------------------------------------------------------------
# The shapes that already worked
# ---------------------------------------------------------------------------


def test_a_bare_tasks_dict_still_works():
    reply = json.dumps({"tasks": [{"id": "1", "title": "BARE"}]})
    assert _title(_extract(reply)) == "BARE"


def test_the_legacy_fixed_tasks_key_still_works():
    reply = json.dumps({"fixed_tasks": {"tasks": [{"id": "1", "title": "LEGACY"}]}})
    assert _title(_extract(reply)) == "LEGACY"


# ---------------------------------------------------------------------------
# When there is genuinely nothing to apply
# ---------------------------------------------------------------------------


def test_a_findings_only_reply_falls_back():
    """No fix in the reply → keep the input. (The findings are salvaged
    separately; see `_run_tasks_self_review`.)"""
    reply = json.dumps({"findings": [{"severity": "high"}]})
    assert _title(_extract(reply)) == "ORIGINAL"


def test_an_unparseable_reply_falls_back():
    assert _title(_extract("not json at all")) == "ORIGINAL"
    assert _title(_extract("")) == "ORIGINAL"


def test_a_fixed_content_without_a_task_list_is_not_treated_as_a_fix():
    reply = json.dumps({"findings": [], "fixed_content": {"requirement": "r"}})
    assert _title(_extract(reply)) == "ORIGINAL"


# ---------------------------------------------------------------------------
# The findings live on the wrapper
# ---------------------------------------------------------------------------


def test_findings_are_readable_off_the_wrapper():
    """`_run_tasks_self_review` reads them here because the tasks dict
    has no ``findings`` key — reading them off the extracted tasks is
    what made a discarded rewrite leave no trace."""
    reply = json.dumps({
        "findings": [{"severity": "high", "type": "violation"}],
        "fixed_content": {"tasks": [{"id": "1"}]},
    })
    obj = _extract_first_json_object(reply)
    assert isinstance(obj.get("findings"), list)
    assert obj["findings"][0]["type"] == "violation"
