"""The negative control's switch: pin the plan source to ``NONE``.

What this plugin is for
-----------------------
``backend/tests/integration/test_verification_prelude_boundaries.py`` asserts
that a reviewed plan document is **translated** into the machine plan the
verification round consumes — on a development branch too, not only on the
verification branch.  A suite like that measures nothing if the assertions it
makes would also hold when the translation never happened — "the machine plan
landed" has to be a statement about the product, not about a counter that was
asleep.

So the control needs a way to turn the translation **off** and watch the
suite notice.  That is this plugin: loaded with ``-p p0_plan_source_none``,
it replaces :func:`plan_artifacts.resolve_plan_source` with a function that
always answers ``PlanSource.NONE`` — the source that says "there is no
reviewed plan here" — and the suite turns red.

Two places are pinned, and both have to be
------------------------------------------
The decision is made in the **backend process**, not in the test process:
``backend/verification_agent.py`` asks ``resolve_plan_source`` whether the
plan directory holds a reviewed document, and the answer decides whether the
round translates the document or asks a model for a fresh plan.  The
test process cannot reach into that process, so the plugin also sets
:data:`FORCED_SOURCE_ENV`; the spawned backend's own bootstrap reads that
variable and pins the same function there.  Without the environment
variable the pinning would be in-process only, and the suite would stay
green — which is exactly the vacuous control this file exists to prevent.

By value, not by module
-----------------------
Several modules import the function **by value**
(``from plan_artifacts import resolve_plan_source``), so replacing the
attribute on the module is not enough on its own: a module that already
holds a reference to the original keeps calling the original.  The plugin
therefore does two things:

* it replaces the attribute on :mod:`plan_artifacts` — which is what the
  code paths that look the function up at call time (``from plan_artifacts
  import ...`` **inside** a function body) see; and
* it re-points every already-imported module that still holds the original
  function object at the replacement.

The replacement happens in :func:`pytest_configure`, which pytest runs
**before** it collects — so before the test module is imported, and before
the product's own modules bind the name.  A patch applied later would miss
the modules that bound the name at import time, and the switch would look
installed while changing nothing.

The switch has to be verifiable
-------------------------------
A control that cannot tell "the switch worked" from "the switch never ran"
is not a control.  So the plugin

* refuses to pretend: if it cannot find the function to replace, or the
  replacement does not answer ``PlanSource.NONE``, it raises — the pytest
  session then dies with an INTERNALERROR instead of a green run that means
  nothing; and
* leaves a marker file behind (named by :data:`MARKER_ENV`), saying which
  function was replaced and what it now answers.  The control script reads
  that marker and fails when it is missing or does not name the target.

The file is named without a ``test_`` prefix on purpose: it is a pytest
**plugin**, not a test, and a pytest plugin that is also collected as a
test module runs twice for no reason.

Usage
-----
    PYTHONPATH=scripts/negctl_fixtures \\
        python -m pytest tests/integration/test_verification_prelude_boundaries.py \\
        -p p0_plan_source_none
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

#: The environment variable the spawned backend reads.  It is the same
#: variable the coexistence end-to-end test uses for its own control, so
#: there is one switch, not two.
FORCED_SOURCE_ENV: str = "PDT_E2E_FORCED_PLAN_SOURCE"

#: The file the plugin writes when it has taken effect.  The control script
#: checks that it exists: a plugin that failed to load would leave the
#: suite green, and a control that cannot tell "the switch worked" from
#: "the switch never ran" is not a control.
MARKER_ENV: str = "PDT_NEGCTL_MARKER_FILE"

#: What the marker file says.
MARKER_TEXT: str = "plan source pinned to NONE"

#: The function this plugin replaces — named in the marker so the control
#: script can check that *this* function was replaced, and not something
#: else.
TARGET: str = "plan_artifacts.resolve_plan_source"


def _backend_dir() -> Path:
    """The backend directory, derived from this file's own location."""
    return Path(__file__).resolve().parents[2] / "backend"


def _import_plan_artifacts():
    """Import ``plan_artifacts``, adding the backend to the path if needed."""
    try:
        import plan_artifacts

        return plan_artifacts
    except ImportError:
        backend = _backend_dir()
        if not backend.is_dir():
            raise
        sys.path.insert(0, str(backend))
        import plan_artifacts

        return plan_artifacts


def _rebind_by_value_references(original: Any, replacement: Any) -> List[str]:
    """Re-point modules that imported the function **by value**.

    ``from plan_artifacts import resolve_plan_source`` binds the function
    object into the importing module's namespace.  Replacing the attribute
    on :mod:`plan_artifacts` does not change that binding, so a module
    that was imported before the switch was flipped would keep calling the
    original — the switch would look installed and change nothing.

    Only modules whose attribute **is** the original function object are
    touched, so nothing else in the process is affected.

    Args:
        original: the function that was replaced.
        replacement: the function that replaced it.

    Returns:
        The names of the modules whose reference was re-pointed.
    """
    rebound: List[str] = []
    for name, module in list(sys.modules.items()):
        if module is None or name == "plan_artifacts":
            continue
        if getattr(module, "resolve_plan_source", None) is not original:
            continue
        try:
            setattr(module, "resolve_plan_source", replacement)
        except Exception:  # pragma: no cover - a read-only module namespace
            continue
        rebound.append(name)
    return rebound


def _pin_the_plan_source() -> Dict[str, Any]:
    """Make ``resolve_plan_source`` answer ``PlanSource.NONE``, always.

    Returns:
        A record of what was replaced: the original function and the
        modules whose by-value reference was re-pointed.

    Raises:
        RuntimeError: the function could not be found, or the replacement
            does not answer ``PlanSource.NONE``.  The switch must never
            fail silently — a switch that did not take would leave the
            suite green, and the control would report a difference it did
            not cause.
    """
    module = _import_plan_artifacts()

    original = getattr(module, "resolve_plan_source", None)
    if original is None:
        raise RuntimeError(
            "the negative control's switch found no "
            "plan_artifacts.resolve_plan_source to replace"
        )

    def resolve_plan_source(plan_dir):  # noqa: ANN001, ANN202 - the patched shape
        """Answer "there is no reviewed plan here", whatever the directory."""
        return module.PlanSource.NONE

    module.resolve_plan_source = resolve_plan_source

    rebound = _rebind_by_value_references(original, resolve_plan_source)

    # Verify the switch rather than trusting the assignment: the control
    # script reads the marker file, and the marker must not be written for
    # a switch that did not take.
    if getattr(module, "resolve_plan_source", None) is not resolve_plan_source:
        raise RuntimeError(
            "the negative control's switch could not install itself on "
            "plan_artifacts"
        )
    if module.resolve_plan_source(Path(".")) is not module.PlanSource.NONE:
        raise RuntimeError(
            "the negative control's switch does not answer PlanSource.NONE"
        )

    return {"original": original, "rebound": rebound}


def _describe(function: Any) -> str:
    """Name a function the way a reader would recognise it."""
    module = getattr(function, "__module__", "?")
    qualname = getattr(function, "__qualname__", getattr(function, "__name__", "?"))
    return f"{module}.{qualname}"


def _write_the_marker(details: Dict[str, Any]) -> None:
    """Leave a note that the switch took effect, where the caller looks."""
    target: Optional[str] = os.environ.get(MARKER_ENV)
    if not target:
        return
    rebound = details.get("rebound") or []
    lines = [
        MARKER_TEXT,
        f"target={TARGET}",
        f"original={_describe(details['original'])}",
        f"rebound={','.join(rebound) if rebound else '-'}",
        "returns=PlanSource.NONE",
    ]
    try:
        Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        # The marker is evidence for the control script; if it cannot be
        # written the control says so by the missing file rather than by
        # an exception here.
        pass


def pytest_configure(config) -> None:  # noqa: ANN001 - pytest's hook shape
    """Pin the plan source, in this process and in the one it spawns."""
    os.environ[FORCED_SOURCE_ENV] = "none"
    details = _pin_the_plan_source()
    _write_the_marker(details)
