# Contributing

## The contribution surface

The split you can see in the repository layout *is* the contribution
surface:

**Backend phases** live under `backend/` as a FastAPI package. Each phase —
`prd`, `arch`, `test_design`, `tasks`, `execution`, `verification` — has its
own generator / executor module and its own HTTP router under
`backend/routes/`. Touch the route and the generator **together** if you
change the surface: a route with no generator behind it returns `501` on the
next request, and the contract tests already assert on that.

**The UI** is a static SPA under `frontend/` — `index.html`, `app.js`,
`style.css`, plus the small wrapper file `frontend/api.js`. There is no build
step: edit a file and reload the browser. Every fetch goes through the
wrapper rule described in [Running it](../operations/running.md); new view
code that bypasses the wrapper fails both the runtime preflight and
`backend/tests/static_gates/test_frontend_uses_api_wrapper.py`.

**The test suite** lives entirely under `backend/tests/`. See
[Tests](testing.md) for how it is split and which lane your change belongs
to.

## Workflow for a change

1. **Read the artifact that owns the area.** The PRD section for the phase
   you are touching, or the design notes if the PRD predates it.
2. **Decide which phase the change belongs to.** Phases are load-bearing on
   each other: a change to PRD generation without a matching acceptance test
   will pass `pytest` and still be wrong, because verification compares the
   running code against the documents the earlier phases produced.
3. **Add the test first.** Rules about the repository itself go in
   `backend/tests/static_gates/`; generator behaviour goes in the relevant
   `backend/tests/unit/` module; a new endpoint goes in the contract suite.
4. **Implement it**, keep the new test green, then run the rest of the unit
   lane to make sure nothing else moved.
5. **Commit on a topic branch.** The CI unit lane is the merge gate; the
   integration and e2e lanes run nightly on `main`.

## Commit messages

A commit message may say **what changed and why**. It may not claim an AI
system as a co-author:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>    ← rejected
```

The committer cannot know that. The tool may have been Claude Code, but the
*model* behind any given commit is a routing decision made elsewhere and is
not recorded in the commit — so naming one writes a guess into permanent,
public history, in the grammar of a fact, where it cannot be corrected.

Writing *about* the tool is fine. The checker inspects the attribution
trailer, not the prose, so a body may discuss Claude Code, providers or
models freely — and a human co-author's trailer passes.

`scripts/check_commit_msg.py` is the single implementation, and it is
reached three ways: `.git/hooks/commit-msg` (installed by
`scripts/install_git_hooks.sh`, or by `pre-commit install --hook-type
commit-msg`), the `commit-msg` stage in `.pre-commit-config.yaml`, and a CI
job over the pushed range — so `git commit --no-verify` does not get it
past review.

## The rule about names {#the-rule-about-names}

If your change adds an external provider, **do not** name it on a template.
A row of concrete names belongs in `PDT_PROVIDER_*` config on the deploying
machine. Putting it into `example/` would compile one install's provider
list into every install's onboarding — see
[Configuration](../operations/configuration.md).

The same reasoning is why the repository ships no operator-specific paths,
no local checkout names, and no attribution to a person. Gates enforce the
mechanical parts of that
(`test_no_local_home_path_in_first_party.py`,
`test_no_operator_attribution_in_source.py`), but the rule is broader than
the gates: **write what is true of the software, not what is true of your
machine.**

## Where a file belongs

`scripts/` holds scripts that are **true of the software** — the ones CI
(`.github/workflows/`), the pre-commit hooks, and a developer's shell run.
Anything true of *one run* or *one machine* does not go there: a one-off
migration, a helper that writes this installation's nightly artifact, a
scratch script you will run twice. Those belong in `.pdt/`, which is
gitignored and already holds the rest of the local runtime state.

This is [the rule about names](#the-rule-about-names) applied to a
directory instead of a name, and it is what keeps the tree legible: a
reader who opens `scripts/` should be able to run any of it. The directory
is also load-bearing in a way the rest of the tree is not —
`backend/tests/static_gates/test_scripts_have_no_dangerous_defaults.py`
treats every script there as a shell CI will execute, which is only a
meaningful statement while everything in the directory is actually CI's.

If a script *is* general — another install would want to run it — it
belongs in `scripts/`, and the Layout table in the README should name it.
The README carries that map: what each directory is for, and which
directories are local runtime state rather than product. Detail that does
not fit a one-line table entry belongs on the page for the area it
describes, like this one.

## Documentation

The site you are reading is built from `docs/` with MkDocs:

```bash
backend/.venv/bin/python3 -m pip install -r docs/requirements.txt
backend/.venv/bin/python3 -m mkdocs serve     # http://127.0.0.1:8000
backend/.venv/bin/python3 -m mkdocs build --strict
```

`--strict` is the gate. A nav entry pointing at a missing page, or a link to
a page that does not exist, exits non-zero — and CI runs exactly that on
every pull request that touches `docs/` or `mkdocs.yml`.

The Markdown here is plain and renders on github.com too, so nothing about
reading the docs depends on the site being built.

## Comments

A comment may say **what the code does and why**. It may not quote a person,
name a sibling checkout, or narrate what happened on one machine.

That last distinction is worth stating plainly, because the two read almost
identically while writing them:

- *"This path used `.parent.parent` and reached the wrong directory, because
  each caller re-derived it from its own `__file__`"* — a **defect**. Anyone
  can re-derive it by reading the code. Write it down.
- *any sentence reporting what had piled up on the box this was run on —
  how many, how old, over what window* — an **incident**. Nobody can
  re-derive it from the source. Do not put it in a public document.

That second bullet is deliberately abstract, and the reason is worth
reading twice: an earlier draft illustrated it with a realistic sentence,
which republished exactly what the rule forbids. The page about not
leaking incidents leaked one. The gate under
`backend/tests/static_gates/` catches the shape, so it will catch yours
too — describe the incident, never quote it.

The dividing question is one line: **could a stranger reach this conclusion
by reading the code?** If yes it belongs in the repository; if no it does
not.
