# Tests

One rule beats every other piece of advice: **run the suite through
`backend/.venv`, with the working directory at the repository root.** The
reason is in [Getting started](../getting-started.md) — the system Python
fails at *collection*, not at some later, more explicable point.

## The lanes

```bash
# The default CI lane. -m unit skips the time_sensitive / integration / e2e /
# real_model markers; the missing models are satisfied by the stubs
# backend/tests/conftest.py installs.
backend/.venv/bin/python3 -m pytest backend/tests -m unit -q

# Integration: spawns the server and talks to it over HTTP. Needs no live
# model, but does need a free loopback port.
backend/.venv/bin/python3 -m pytest backend/tests -m "not time_sensitive" -q

# End-to-end: a full plan lifecycle through dry-run fake backends.
# Main branch only, per the test-design decision.
backend/.venv/bin/python3 -m pytest backend/tests -m e2e -q

# One module, loudly
backend/.venv/bin/python3 -m pytest backend/tests/<file>.py -xvs
```

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
