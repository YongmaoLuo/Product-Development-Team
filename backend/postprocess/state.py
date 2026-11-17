"""Post-processing state — the phase names of the optional finishing stage.

Why this module exists
----------------------
The optional post-processing stage (收尾阶段) adds two phases to the plan
vocabulary: the stage itself and its failure state.  They are declared
here, in the package that owns the stage, so the rest of the subsystem
imports the names instead of re-spelling the same two strings in three
places — a spelling that has to be changed in three places when it
changes.

The names are also **registered** in ``plan_state.VALID_PHASES``: the
vocabulary is the state machine's list of legal phase values, and a
phase that is not in it cannot be entered at all (the transition raises
``ValueError`` before it looks at any edge).  ``PHASE_ADDITIONS`` names
the two entries this package contributes; the phase table and this list
are kept in step by a test — see
``backend/tests/unit/postprocess/test_package_and_phases.py``.

The package is a **namespace package**: there is deliberately no
``__init__.py``.  ``backend/`` is on ``sys.path`` (see the
``pythonpath`` setting in ``backend/pytest.ini``), so ``import
postprocess.state`` resolves to this directory as a namespace portion.
"""

#: The finishing stage itself.  The plan sits in this phase while the
#: post-processing items are executed and judged.
POST_PROCESSING: str = "post_processing"

#: The failure state of the finishing stage.  A plan that gets here has
#: run its finishing items and at least one of them did not pass; it is
#: resumable, so it is not a terminal phase.
POSTPROCESS_FAILED: str = "postprocess_failed"

#: The phase names this package contributes to ``plan_state.VALID_PHASES``.
PHASE_ADDITIONS: tuple[str, ...] = (POST_PROCESSING, POSTPROCESS_FAILED)
