"""The coding agent must not be handed the plans root.

What went wrong
---------------
The coding agent's ``--settings`` payload used to carry the **whole**
plan context: the plan's id *and* the absolute root its directory hangs
under.  Writing the root there was redundant — the executor already
puts it into ``os.environ`` (``cli.py``: ``os.environ.update(plan_env(
plan_id, plan_dir.parent))``, with an absolute root), so every process
below the executor inherits it — and it was not harmless.

A settings payload's ``env`` is applied to the child's environment, so
it **overrides** what the child inherited.  The value written was the
one *this module* resolved at import time, i.e. the **backend's** view
of the root.  So any process that ran this code got the backend's root
stamped onto it — including a test run in a worktree, which then wrote
into the production plans tree.

So the payload carries the plan's id and nothing else.  The root is a
fact about *where the plans live*, not about *which plan this is*; the
id is the fact, and the id is what a child needs to derive the plan's
own directory.

The seam under test
-------------------
The payload is built inside a very large method, so the plan context it
writes comes from one named helper — ``coding_tool._plan_context_env``
— and the test calls that helper.  It is the same function the
production code calls; the test does not re-implement it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import coding_tool  # noqa: E402
from plan_env import PLAN_ID_ENV, PLANS_DIR_ENV  # noqa: E402


# ---------------------------------------------------------------------------
# the cases
# ---------------------------------------------------------------------------

def test_the_coding_agent_env_carries_the_plan_id_and_no_plans_root(
    monkeypatch,
):
    """The plan's id is there; the plans root is not.

    ``PDT_PLANS_DIR`` is *set* in the environment on purpose — that is
    the state the executor leaves every child in, and it is exactly the
    state in which the payload used to reach for the root.
    """
    monkeypatch.setenv(PLAN_ID_ENV, "plan-xyz")
    monkeypatch.setenv(PLANS_DIR_ENV, "/somewhere/else/plans")

    env = coding_tool._plan_context_env()

    assert PLANS_DIR_ENV not in env, (
        f"the coding agent's settings payload must not carry the plans "
        f"root; it carried {PLANS_DIR_ENV}={env[PLANS_DIR_ENV]!r}"
    )
    assert env.get(PLAN_ID_ENV) == "plan-xyz", (
        "the plan's id must still be in the payload — it is the fact a "
        f"child needs to derive the plan's own directory; the payload "
        f"was {env!r}"
    )


def test_without_a_plan_id_the_coding_agent_env_is_empty(monkeypatch):
    """No plan, no context — and still no plans root.

    The other half of the rule: a process that knows no plan must not
    be given one, and must not be given a root either.
    """
    monkeypatch.delenv(PLAN_ID_ENV, raising=False)
    monkeypatch.setenv(PLANS_DIR_ENV, "/somewhere/else/plans")

    env = coding_tool._plan_context_env()

    assert PLANS_DIR_ENV not in env, (
        f"the coding agent's settings payload must not carry the plans "
        f"root; it carried {PLANS_DIR_ENV}={env[PLANS_DIR_ENV]!r}"
    )
    assert PLAN_ID_ENV not in env, (
        "a process that knows no plan must not be given a plan id; the "
        f"payload was {env!r}"
    )


def test_the_settings_payload_has_no_other_way_to_the_plans_root():
    """No other line in the module may write the root into the payload.

    The helper is the one seam the payload takes its plan context from,
    but a module this size can grow a second line.  This reads the
    source and looks at every assignment into the payload's ``env``:
    none of them may name the plans root, and the one ``update()`` of
    the plan context must be the helper's.
    """
    source = (_BACKEND_DIR / "coding_tool.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    written_keys = []
    plan_context_updates = []

    for node in ast.walk(tree):
        # settings_data["env"][<key>] = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Subscript):
                    continue
                inner = target.value
                if not isinstance(inner, ast.Subscript):
                    continue
                if not (
                    isinstance(inner.value, ast.Name)
                    and inner.value.id == "settings_data"
                    and isinstance(inner.slice, ast.Constant)
                    and inner.slice.value == "env"
                ):
                    continue
                if isinstance(target.slice, ast.Constant):
                    written_keys.append(target.slice.value)

        # settings_data["env"].update(<argument>)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr != "update" or not node.args:
                continue
            target = node.func.value
            if not (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Name)
                and target.value.id == "settings_data"
                and isinstance(target.slice, ast.Constant)
                and target.slice.value == "env"
            ):
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Call) and isinstance(
                argument.func, ast.Name
            ):
                plan_context_updates.append(argument.func.id)

    assert PLANS_DIR_ENV not in written_keys, (
        f"coding_tool.py writes {PLANS_DIR_ENV} into the settings payload "
        f"at one of {written_keys!r}; the payload must not carry the "
        "plans root"
    )
    assert plan_context_updates == ["_plan_context_env"], (
        "the settings payload must take its plan context from "
        f"_plan_context_env(); it takes it from {plan_context_updates!r}"
    )
