# `tests/fixtures/` — data fixtures, not a test tree

There is no test suite in this directory. The suite lives in
[`backend/tests/`](../backend/tests/), and that is the only tree CI runs.

This directory exists because a small number of tests resolve fixture
paths *relative to the repository root* rather than to `backend/` —
`backend/tests/integration/test_backward_compat.py` does
`Path(__file__).parent.parent.parent.parent / "tests" / "fixtures" / ...`.
Keeping the fixtures where those tests look for them is cheaper and
less surprising than rewriting the path arithmetic in each one.

So: **add tests to `backend/tests/`**; add data files here only when an
existing test already expects them at this path.
