"""Adversarial input samples for the 5 grep gates.

Background
----------
The 5 grep gate tests in ``backend/tests/test_server_json_cleanup.py``
target 5 different ways of constructing a forbidden JSON state filename
in production code:

    1. direct_literal      — a quoted string of the form ``"<name>.json"``
    2. string_concat       — ``"<a>" + "<b>.json"`` (AST-level check)
    3. variable_reference  — a reference to ``_RT_FN_*`` or one of the
                              legacy filenames
    4. fstring_format      — f-string interpolation into ``.json`` OR a
                              ``.format(...)`` call whose args contain
                              ``.json``
    5. pathlib_join        — ``Path(...) / "<name>.json"`` or
                              ``os.path.join(..., "<name>.json")``

This fixture plants one synthetic sample per class so the
``test_adversarial_grep_inputs`` self-test can run each gate against
its own shaped input and assert the gate fires.  Without this, the
gates could silently deselect every line of production code (e.g.
because of a regex typo) and still report PASSED.

Format
------
Each constant below is a single line of Python source that, if it
appeared in production code, would trip the corresponding gate.  The
constants are read by exec() in ``_load_adversarial_samples`` —
they are NOT imports of any project module and they are NOT
executed at runtime.
"""

# Pattern 1 — direct literal: a quoted forbidden filename.
SAMPLE_DIRECT_LITERAL = 'PLAN_STATE_FILE = "plan_state.json"'

# Pattern 2 — string concat: ``"a" + "b.json"`` shape.  Must contain
# one of the five forbidden basenames when the operands are joined.
SAMPLE_STRING_CONCAT = '_RT_FN_EXEC = "executi" + "on.json"'

# Pattern 3 — variable reference: the ``_RT_FN_*`` identifier prefix.
SAMPLE_VARIABLE_REFERENCE = "_RT_FN_EXEC = _RT_FN_PLAN_STATE + _RT_FN_VERIFICATION"

# Pattern 4 — fstring / .format: an f-string that interpolates into
# ``.json``.  This sample uses the f-string arm of the regex.
SAMPLE_FSTRING_FORMAT = 'PREFIX = "plan"; plan_state_file = f"{PREFIX}_state.json"'

# Pattern 5 — pathlib / os.path.join: a ``Path(...) / "<name>.json"``
# shape.  This sample uses pathlib; the os.path.join arm is also
# exercised by the production regex but a single sample is enough
# to drive the gate's positive case.
SAMPLE_PATHLIB_JOIN = 'PLAN_STATE_FILE = Path(base_dir) / "plan_state.json"'
