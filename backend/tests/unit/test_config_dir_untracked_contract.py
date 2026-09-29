"""Pin the untracked / gitignored contract of ``.config/`` and the template
shape of ``example/``.

Why this gate exists
--------------------
``example/`` ships placeholders; ``.config/`` ships operator-owned
values. The two surfaces are easy to mix up — a plain ``git add -A``,
a stray copy-paste, or a future ``.gitignore`` edit that drops the
rule would all land real provider / routing data into the public
repository. The leaked content is not a credential, so no secret
scanner would catch it; the audit sweep needs an explicit gate.

This file pins the *other* half of the property that
``test_config_placeholders_are_neutral.py`` pins for the template
side:

1. **``.config/`` carries nothing tracked.** ``git ls-files .config/``
   must be empty on every checkout, present directory or not. The
   check holds even on a fresh clone where ``.config/`` has never
   been initialised — the assertion is purely about what *git*
   tracks, not about what exists on disk.

2. **``.config/`` is matched by ``.gitignore``.**
   ``git check-ignore -q`` exits 0 against both the live routing
   config and the provider-order file, so a future ``.gitignore``
   edit that drops the rule trips the gate at PR time. The check is
   pinned against *two* files so the gate does not pass on a rule
   that ignores only one of them.

3. **``example/`` holds nothing but templates.** Every file under
   ``example/`` ends with ``.example``. A file without that suffix
   would be a live config that has slipped into the tracked tree —
   the exact shape the gate exists to prevent.

4. **Templates are reachable through the real loader.**
   ``resolve_provider_capacity_file()`` honours
   ``PDT_PROVIDER_CAPACITY_FILE`` and returns a path the operator
   can actually open. The "copy template → fill placeholder → point
   env var at it" workflow documented for first-time setup must
   succeed mechanically, not just on paper.

The gate deliberately does **not** read the contents of any
``example/*.example`` file — that's
``test_config_placeholders_are_neutral.py``'s job. The two files
cover complementary halves of the same contract and share no
imports so a future refactor of one cannot silently weaken the
other.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

#: The repository root. ``backend/tests/unit/<file>`` lives three
#: directories below it; the same parents[N] walk used by the sibling
#: placeholder gate keeps the two files structurally identical.
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two surfaces the gate inspects. Both are pinned by absolute
#: path so the assertions are independent of the runner's cwd.
_CONFIG_DIR = _REPO_ROOT / ".config"
_EXAMPLE_DIR = _REPO_ROOT / "example"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a read-only ``git`` command rooted at the repository top.

    ``capture_output=True`` keeps stdout / stderr inspectable from the
    test; ``text=True`` means the strings round-trip cleanly into the
    assertion messages.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_config_file_is_tracked() -> None:
    """No file under ``.config/`` may appear in ``git ls-files``.

    The assertion holds whether ``.config/`` exists on disk or not:
    ``git ls-files .config/`` returns an empty string in both cases,
    so a clean clone (no ``.config/``) and a populated checkout both
    pass without special-casing. A non-empty output means one
    operator's live routing / capacity file has slipped into source
    control — the very leak this gate exists to catch.
    """
    result = _run_git("ls-files", ".config/")
    assert result.returncode == 0, (
        f"`git ls-files .config/` exited with {result.returncode}; "
        f"stderr={result.stderr!r}"
    )
    assert result.stdout.strip() == "", (
        ".config/ has tracked files; the live operator config has "
        "leaked into the public repository. Remove the tracked "
        "entries (and the `.config/` ignore rule if missing) so a "
        "plain `git add -A` cannot reintroduce them.\n"
        f"  tracked entries: {result.stdout!r}"
    )


def test_config_paths_are_git_ignored() -> None:
    """``.config/`` must be matched by ``.gitignore``.

    The check is pinned against *two* concrete paths so the gate does
    not pass on a rule that ignores only one. ``git check-ignore -q``
    exits 0 when the path is ignored and 1 when it is not — silent
    success is the whole point of the ``-q`` flag here. The companion
    assertion in ``test_no_config_file_is_tracked`` covers the
    "tracked but ignored" edge, so the two tests are complementary,
    not redundant.
    """
    targets = [
        _CONFIG_DIR / "provider_routing.yaml",
        _CONFIG_DIR / "provider-order.json",
    ]
    for target in targets:
        result = _run_git("check-ignore", "-q", str(target))
        assert result.returncode == 0, (
            f"{target} is not matched by .gitignore; a plain "
            "`git add -A` would commit one deployment's live "
            "config into the public repository. exit="
            f"{result.returncode} stdout={result.stdout!r} "
            f"stderr={result.stderr!r}"
        )


def test_example_dir_holds_only_templates() -> None:
    """Every file under ``example/`` must end with ``.example``.

    A file in ``example/`` *without* the suffix is the exact shape of
    "real config slipped into the tracked tree": the directory name
    advertises templates, but a concrete deployment file would
    compile one install's providers into every checkout that copies
    it. The check is purely a name check — content is the sibling
    gate's job — so this assertion can run without parsing YAML.
    """
    assert _EXAMPLE_DIR.is_dir(), (
        f"{_EXAMPLE_DIR} is missing; the template surface has no "
        "home. Restore the directory and its tracked templates."
    )

    offenders: list[str] = []
    for entry in sorted(_EXAMPLE_DIR.iterdir()):
        # Subdirectories are out of scope for this gate: they would be
        # a different category of leak and a different rule. The
        # gate is about files.
        if entry.is_dir():
            continue
        if not entry.name.endswith(".example"):
            offenders.append(entry.name)

    assert not offenders, (
        "example/ holds a file without the .example suffix; a real "
        "config has slipped into the tracked tree and would be "
        "copied into every checkout. Move it to .config/ (which is "
        "gitignored) and rename the original to .example so only "
        "the template shape ships.\n"
        f"  offenders: {offenders}"
    )


def test_capacity_resolver_reports_a_readable_path_or_path_bearing_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``PDT_PROVIDER_CAPACITY_FILE`` must drive the loader to a real path.

    The first-run workflow documented for the configuration surface is
    "copy ``example/provider_capacity.yaml.example`` into
    ``.config/provider_capacity.yaml`` and edit it". Setting
    ``PDT_PROVIDER_CAPACITY_FILE`` to the template simulates a caller
    pointing the loader at the template directly; the resolver must
    return a path the operator can read, or raise an error that names
    the path it tried to read.

    The assertion is deliberately tolerant of the success path (the
    template is readable) and the failure path (a future refactor
    that adds validation may raise, but only with a path-bearing
    message). What it forbids is a silent return — a path that
    points at nothing, or an exception with no path in the message
    would make the documented workflow fail on the operator's
    machine without leaving a trace.
    """
    template_path = _REPO_ROOT / "example" / "provider_capacity.yaml.example"
    monkeypatch.setenv("PDT_PROVIDER_CAPACITY_FILE", str(template_path))

    # Import lazily so the env-var override is in force before the
    # module reads it. ``resolve_provider_capacity_file`` re-reads
    # the env on every call, so import order does not actually matter
    # here, but delaying the import keeps the test's intent visible.
    from config_paths import resolve_provider_capacity_file

    try:
        resolved = resolve_provider_capacity_file()
    except (FileNotFoundError, OSError) as exc:
        # The error must name the path the loader tried to read;
        # a bare "not found" without a path is the silent failure
        # the assertion forbids.
        assert str(template_path) in str(exc), (
            f"resolve_provider_capacity_file() raised without naming "
            f"the path: {exc!r}. The operator needs to know which "
            f"path failed; update the error to include it."
        )
        return

    # Success path: the resolver must return the template path we
    # asked for (or its resolved form) and that path must be openable
    # — i.e. the template is reachable through the real loader, not
    # just on paper.
    assert resolved == template_path or str(resolved) == str(template_path), (
        f"resolve_provider_capacity_file() returned {resolved!r}; "
        f"expected the template path {template_path!r}. The env-var "
        f"override is not being honoured."
    )
    assert resolved.is_file(), (
        f"{resolved} does not exist on disk; the template is "
        f"reachable in name only. Restore the file at {template_path}."
    )
    # A readable path must actually open — this is the line that
    # catches "the file exists but has the wrong permissions / is a
    # broken symlink" edge cases without needing a separate gate.
    with resolved.open("r", encoding="utf-8") as handle:
        handle.read(1)