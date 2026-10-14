# Tests

One rule beats every other piece of advice: **run the suite through
`backend/.venv`, with the working directory at the repository root.** The
reason is in [Getting started](../getting-started.md) — the system Python
fails at *collection*, not at some later, more explicable point.

## The lanes

```bash
# The unit lane. The directory, not the marker — see the note below.
backend/.venv/bin/python3 -m pytest backend/tests/unit -q

# Integration: spawns the server and talks to it over HTTP. Needs no live
# model, but does need a free loopback port.
backend/.venv/bin/python3 -m pytest backend/tests -m integration -q

# End-to-end: a full plan lifecycle through dry-run fake backends.
# Main branch only, per the test-design decision.
backend/.venv/bin/python3 -m pytest backend/tests -m e2e -q

# One module, loudly
backend/.venv/bin/python3 -m pytest backend/tests/<file>.py -xvs
```

!!! note "The marker and the directory are not the same set"
    `tests/unit/conftest.py` auto-marks everything under that directory, and a
    handful of files *outside* it mark themselves
    (`tests/security/test_request_guard_rejections.py`,
    `tests/test_server_json_cleanup.py`, …) because they are unit-level tests
    living elsewhere. The marker therefore selects a *superset* of the
    directory: the same tree plus those few files. No exact count is
    recorded here on purpose — it is a property of one checkout's
    collection, not something a reader can derive.

    Prefer the directory. It is what the gate runs, and it cannot drift.

    **Until the hook was scoped the marker selected the entire suite.** A
    subdirectory conftest's `pytest_collection_modifyitems` is a
    **session-level** hook, so the loop in `tests/unit/conftest.py` marked
    every test pytest had collected rather than the ones under its own
    directory, contradicting its own docstring. That pulled in `tests/e2e/`,
    which prompts for the keychain. `README.md` and this page both advertised
    `-m unit` as the unit lane; the hook is now scoped, so the marker means
    what it always claimed to.

!!! warning "A marker and a directory are not the same set"
    They overlap without being equal, for the same reason. The e2e marker
    set is the *larger* one because
    `tests/e2e/test_provider_quota_wake_end_to_end.py` carries no marker at
    all — it is inside the directory and inside no marker expression, which
    is why CI's `-m "not e2e and …"` cannot exclude it. That file sleeps
    through a quota window, so it costs a 300 s timeout every run that
    reaches it.

## Running in parallel

`pytest-xdist` is installed in `backend/.venv`.

**`-n auto` is a trap here**, and it gets worse the more workers you give
it: it aborts, with

```
ERROR gw14 - Different tests were collected between gw2 and gw14.
```

The cause is `tests/unit/test_provider_quota_dispatch.py:_stamp()`, which
computes `datetime.now() + 30min` **at import time** and hands the result to
`parametrize` as an argument — so the wall-clock string *is* the test id.
Workers that start in different seconds disagree about what they collected,
and the wider the worker spread the more likely the disagreement. That is
also why a small `-n` may be green by luck rather than by design: nothing
bounds how far apart the workers may start. Use an explicit count (`-n 8`),
and treat larger values as unsupported until `_stamp` stops putting the
clock into an id.

(The timing tables that used to sit here were one machine's measurements —
core count, host load, wall-clock per worker count — so they could not be
reproduced or checked by a reader and were removed rather than left to rot.
The shape above is the part that holds on any machine.)

Two further hazards, recorded so the next person does not rediscover them:

- **All workers share one scratch root.** `backend/tests/conftest.py` keys
  `PDT_TEST_SESSION_ROOT` on the environment, so the xdist controller mints
  one directory and every worker inherits it — N workers against a single
  SQLite `state.db`. Per-test autouse fixtures mask this today; it is the
  next thing to break once the collection race is fixed.
- **`time_sensitive` and `perf` must stay serial.** They assert on wall-clock
  (see the rationale in `backend/pytest.ini`), which is why `-n` belongs on
  the command line and not in `addopts`.

!!! tip "Prefer the wrapper"
    `scripts/run_tests.sh` is the canonical entry point. It resolves the
    venv, defaults the target to `backend/tests`, and keeps flags from
    changing *which* tests run — a flag-only invocation previously switched
    suites because pytest resolves its inifile from the current directory,
    and this repository has two `pytest.ini` files with different addopts.

    A script that ends in a pipe (`run_tests.sh | tail`) reports the
    **pipe's** exit code, not pytest's. Check `$?` without a pipe when it
    matters.

## Two collection rules worth pinning

- **`pytest.ini` collection is directory-sensitive.** From `backend/`,
  pytest reads `backend/pytest.ini` (with `--strict-markers`, so a typo in
  a marker name fails loudly instead of silently matching nothing); from
  the repository root it reads the root `pytest.ini`. Pick one and stay
  there for the run — the two enumerate the same markers but their addopts
  differ.
- **`backend/.venv` is the only interpreter the suite imports cleanly
  under.**

## How the suite is organised

| Directory | What it holds |
|---|---|
| `unit/` | one module's behaviour, in isolation |
| `integration/` | the server spawned and driven over HTTP |
| `e2e/` | a full plan lifecycle against fake backends |
| `security/` | attack-path assertions |
| `contract/` | the HTTP surface a caller can depend on |
| `meta_tests/` | gates over the audit document itself |
| `static_gates/` | gates over the **source tree** |

The last two are the ones that surprise people, and they are the ones worth
understanding before contributing:

- **`meta_tests/`** parse the security audit document and assert its shape —
  every finding has the required elements, the coverage grid has no empty
  cell, the reference tables resolve. The audit document is a build input,
  not a write-up that happens to live in the repository.
- **`static_gates/`** audit the *repository itself*: that the frontend goes
  through its API wrapper, that no operator-private path appears in source,
  that a credential-bearing file is written private, that the state-database
  path has exactly one resolver. Most of them exist because the thing they
  check has already broken once — the gate's docstring says which incident
  it came from.

A gate that scans nothing passes vacuously, so `static_gates` tests usually
include a "the scan is non-empty" assertion and a sensitivity test proving
the scanner still matches the shape it was written for. Keep those when you
touch a gate.

## Writing a gate that survives

Two failure modes kill gates, and the existing ones are written to avoid
both:

- **Noise.** A scanner that fires on legitimate code gets deleted by the
  next person it annoys. When you write one, include the negative cases as
  tests — the shapes that must **not** match — not just the positive ones.
- **Self-reference.** A gate must not contain the literal it forbids, or it
  matches its own definition. The existing gates build their patterns from
  concatenated fragments for exactly this reason; several of them were
  caught by their own assertions before that convention was adopted.
