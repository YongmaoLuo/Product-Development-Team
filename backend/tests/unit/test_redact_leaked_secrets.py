"""The temp-root credential sweeper (2026-09-28).

``test_secret_files.py`` covers the redaction *primitive* and
``test_credential_files_are_written_private.py`` pins what production code
writes. This file covers ``scripts/redact_leaked_secrets.py`` — the
operator tool that cleans up after a run that died before the primitive
could fire.

The interesting assertions are all about what the script must **refuse**
to do. A cleaner that walks a world-writable temp root and rewrites files
by name is a weapon if it is careless, so each refusal is pinned:

* it does not follow a symlink (a planted link would aim the rewrite);
* it does not touch a file a live process is using (that subagent would
  read ``<redacted>`` as its API key);
* it does not rewrite a file it cannot positively identify.
  ``utils.secret_files.is_managed`` stays load-bearing here rather than
  being loosened to make the cleanup easier; the one extra shape it
  accepts — a payload the nightly runner writes straight into the temp
  root — is admitted by an exact name *and* an exact location, never by a
  prefix, and is pinned by its own tests below;
* it never runs against the machine's real temp root from a test: every
  case passes ``--root``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "redact_leaked_secrets.py"
_BACKEND_DIR = _REPO_ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from utils.secret_files import REDACTED  # noqa: E402
from utils import secret_sweep as sweeper  # noqa: E402


#: The CLI is a thin wrapper over ``sweeper``; it is loaded by path
#: because ``scripts/`` is not a package on ``sys.path``. Only ``main``
#: is exercised through it — every rule under test lives in the module.
_spec = importlib.util.spec_from_file_location("redact_leaked_secrets", _SCRIPT)
assert _spec and _spec.loader
_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cli)


def _settings_payload(api_key: str = "sk-live-key",
                      auth_token: str = "sk-live-token") -> dict:
    return {
        "env": {
            "ANTHROPIC_BASE_URL": "https://api.vendor-a.example/anthropic",
            "ANTHROPIC_API_KEY": api_key,
            "ANTHROPIC_AUTH_TOKEN": auth_token,
            "ANTHROPIC_MODEL": "Vendor A-M3",
        },
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": []}]},
    }


def _managed_settings(root: Path, stem: str = "subagent_settings",
                      payload: dict | None = None,
                      dir_name: str = "pdt-subagent-abc123") -> Path:
    """Write a settings file the way production does (0700 dir / 0600 file)."""
    directory = root / dir_name
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{stem}_deadbeefdeadbeef.json"
    path.write_text(json.dumps(payload if payload is not None else _settings_payload()),
                    encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _env_of(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["env"]


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "subagent_settings_478cd839b02e421ba8bf6fe9450b44cd.json",
    "verif_settings_VP-1-rebuild_p.json",
])
def test_generated_names_are_recognised(name: str) -> None:
    assert sweeper.is_generated_settings_name(name)


@pytest.mark.parametrize("name", [
    "settings.json",                       # the operator's own config
    "subagent_settings_abc.txt",           # wrong suffix
    "xsubagent_settings_abc.json",         # prefix must be at the start
    "passwd",
    "",
])
def test_foreign_names_are_not_recognised(name: str) -> None:
    assert not sweeper.is_generated_settings_name(name)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_finds_files_one_directory_below_the_root(tmp_path: Path) -> None:
    written = _managed_settings(tmp_path)
    assert sweeper.find_candidates([tmp_path]) == [written]


def test_does_not_walk_deeper_than_the_writer_does(tmp_path: Path) -> None:
    """Depth 3 is not a shape this project produces; scanning it is
    needless blast radius in a world-writable directory."""
    deep = tmp_path / "a" / "pdt-subagent-deep"
    deep.mkdir(parents=True)
    (deep / "subagent_settings_deadbeef.json").write_text("{}", encoding="utf-8")
    assert sweeper.find_candidates([tmp_path]) == []


def test_a_directory_named_like_a_settings_file_is_not_a_candidate(
    tmp_path: Path,
) -> None:
    (tmp_path / "pdt-subagent-abc" / "subagent_settings_deadbeef.json").mkdir(
        parents=True
    )
    assert sweeper.find_candidates([tmp_path]) == []


def test_unreadable_subdirectory_does_not_abort_the_walk(tmp_path: Path) -> None:
    """macOS ``TemporaryItems`` is mode 0700-root and raises on scandir."""
    good = _managed_settings(tmp_path)
    locked = tmp_path / "locked"
    locked.mkdir()
    os.chmod(locked, 0o000)
    try:
        assert sweeper.find_candidates([tmp_path]) == [good]
    finally:
        os.chmod(locked, 0o700)


# ---------------------------------------------------------------------------
# Refusals — the part that makes this safe to point at /tmp
# ---------------------------------------------------------------------------


def test_a_symlink_is_reported_and_never_followed(tmp_path: Path) -> None:
    """A link planted in a world-writable root must not aim the rewrite.

    The victim here is a stand-in for ``~/.claude/settings.json``: it has
    a credential, it is a valid settings payload, and it is deliberately
    *not* ours. Following the link would redact the operator's own
    configuration.
    """
    victim = tmp_path / "victim_settings.json"
    victim.write_text(json.dumps(_settings_payload(api_key="sk-operator-key")),
                      encoding="utf-8")

    link_dir = tmp_path / "pdt-subagent-abc123"
    link_dir.mkdir()
    link = link_dir / "subagent_settings_deadbeef.json"
    link.symlink_to(victim)

    findings, failures = sweeper.sweep([tmp_path], apply=True)
    assert failures == 0
    by_path = {f.path: f for f in findings}
    assert by_path[link].status == "symlink"
    assert _env_of(victim)["ANTHROPIC_API_KEY"] == "sk-operator-key", (
        "the sweeper followed a symlink and redacted the file it pointed at"
    )


def test_a_file_a_live_process_is_using_is_left_alone(tmp_path: Path) -> None:
    """Redacting out from under a running subagent hands it ``<redacted>``."""
    path = _managed_settings(tmp_path)
    monkeypatched = os.path.realpath(path)
    original = sweeper.settings_paths_in_use
    sweeper.settings_paths_in_use = lambda: {monkeypatched}
    try:
        findings, failures = sweeper.sweep([tmp_path], apply=True)
    finally:
        sweeper.settings_paths_in_use = original

    assert failures == 0
    assert findings[0].status == "in_use"
    assert "after --settings" in findings[0].detail
    assert _env_of(path)["ANTHROPIC_API_KEY"] == "sk-live-key"


def test_an_unmanaged_path_is_reported_not_rewritten(tmp_path: Path) -> None:
    """``is_managed`` is not loosened just so the cleaner has more reach.

    Same *name*, same *content*, different directory — and the answer has
    to be no. This is the boundary between "clean up our residue" and
    "rewrite any file that looks like ours".
    """
    path = tmp_path / "not-ours" / "subagent_settings_deadbeef.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_settings_payload()), encoding="utf-8")

    findings, failures = sweeper.sweep([tmp_path], apply=True)
    assert failures == 0
    assert findings[0].status == "unmanaged"
    assert _env_of(path)["ANTHROPIC_API_KEY"] == "sk-live-key"


# ---------------------------------------------------------------------------
# Flat-temp payloads — the one shape admitted outside a private directory
# ---------------------------------------------------------------------------

#: 32 hex characters, the width of ``uuid.uuid4().hex``. That is the name
#: the nightly runner generates, and the sweeper matches it exactly.
_FLAT_TEMP_NAME = "nightly_ci_settings_" + "a1b2c3d4" * 4 + ".json"


def _flat_temp_settings(root: Path, name: str = _FLAT_TEMP_NAME) -> Path:
    """A payload written straight into ``root``, as the nightly runner does."""
    path = root / name
    path.write_text(json.dumps(_settings_payload()), encoding="utf-8")
    return path


def test_a_flat_temp_payload_is_discovered_and_redacted(tmp_path: Path) -> None:
    """A writer in another repository puts its payload in the root itself.

    That writer adopted neither ``private_dir`` nor this module, so its
    residue was invisible to a scan that only knew the ``pdt-subagent-*``
    layout — the file sat in a world-writable root holding a live key.
    """
    path = _flat_temp_settings(tmp_path)

    assert path in sweeper.find_candidates([tmp_path])

    findings, failures = sweeper.sweep([tmp_path], apply=True)
    assert failures == 0
    assert findings[0].status == "redacted"
    env = _env_of(path)
    assert env["ANTHROPIC_API_KEY"] == REDACTED
    assert env["ANTHROPIC_AUTH_TOKEN"] == REDACTED
    # The routing config is what an operator reads; it survives.
    assert env["ANTHROPIC_BASE_URL"].startswith("https://")


def test_a_flat_temp_file_ends_up_private(tmp_path: Path) -> None:
    """The rewrite goes through ``write_private_json``.

    So a 0644 payload in a world-writable root comes back 0600 — part of
    the remedy for this shape is the file mode itself, not only the
    credential replacement.
    """
    path = _flat_temp_settings(tmp_path)
    os.chmod(path, 0o644)

    sweeper.sweep([tmp_path], apply=True)

    assert (path.stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("name", [
    "nightly_ci_settings_abc.json",              # not the uuid width
    "nightly_ci_settings_" + "z" * 32 + ".json",  # not hex
    "nightly_ci_settings_a1b2c3d4.json",         # too short
    "x" + _FLAT_TEMP_NAME,                       # anchored, not a substring
])
def test_a_near_miss_flat_temp_name_is_never_touched(tmp_path: Path,
                                                     name: str) -> None:
    """A root-level name is matched exactly.

    The tempting version of this feature is a prefix match, which in a
    world-writable temp root means every ``<that-prefix>*.json`` any
    program on the machine happens to leave there. A near miss is not
    reported either — it is not ours to report.
    """
    path = _flat_temp_settings(tmp_path, name)

    findings, failures = sweeper.sweep([tmp_path], apply=True)

    assert failures == 0
    assert findings == []
    assert _env_of(path)["ANTHROPIC_API_KEY"] == "sk-live-key"


def test_a_flat_temp_name_outside_the_scanned_roots_is_left_alone(
    tmp_path: Path,
) -> None:
    """Both halves of the eligibility test are required.

    The vulnerable version is "any file whose name matches is fair game":
    a payload planted elsewhere in the temp root would then be rewritten.
    Requiring the file to sit directly in a root the caller *named* means
    the scan scope is always something an operator chose.
    """
    scanned = tmp_path / "scanned"
    elsewhere = tmp_path / "elsewhere"
    scanned.mkdir()
    elsewhere.mkdir()
    path = _flat_temp_settings(elsewhere)

    findings, failures = sweeper.sweep([scanned], apply=True)

    assert failures == 0
    assert findings == []
    assert _env_of(path)["ANTHROPIC_API_KEY"] == "sk-live-key"


def test_an_operators_own_settings_file_is_still_refused(tmp_path: Path) -> None:
    """The widening must not reach ``~/.claude/settings.json``.

    Same directory as an eligible flat-temp payload, same credential
    inside, and the answer still has to be no — that is the file whose
    rewrite would break the operator's own Claude Code install, and it is
    the reason ``is_managed`` exists at all.
    """
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(_settings_payload(api_key="sk-operator-key")),
                    encoding="utf-8")

    findings, failures = sweeper.sweep([tmp_path], apply=True)

    assert failures == 0
    assert findings == []
    assert _env_of(path)["ANTHROPIC_API_KEY"] == "sk-operator-key"


def test_is_flat_temp_settings_requires_both_name_and_location(
    tmp_path: Path,
) -> None:
    from utils.secret_files import is_flat_temp_settings as eligible

    path = _flat_temp_settings(tmp_path)
    assert eligible(path, [tmp_path])
    assert eligible(path, [tmp_path.resolve()]), (
        "a symlinked temp root must still match — /tmp is one on macOS"
    )
    assert not eligible(path, [tmp_path / "other"])
    assert not eligible(path, [])
    assert not eligible(tmp_path / "settings.json", [tmp_path])
    assert not eligible(None, [tmp_path])


# ---------------------------------------------------------------------------
# The redaction itself
# ---------------------------------------------------------------------------


def test_apply_redacts_credentials_and_keeps_the_routing_config(
    tmp_path: Path,
) -> None:
    path = _managed_settings(tmp_path)
    findings, failures = sweeper.sweep([tmp_path], apply=True)

    assert failures == 0
    assert findings[0].status == "redacted"
    env = _env_of(path)
    assert env["ANTHROPIC_API_KEY"] == REDACTED
    assert env["ANTHROPIC_AUTH_TOKEN"] == REDACTED
    # What an operator reads when debugging a routing mistake survives.
    assert env["ANTHROPIC_BASE_URL"].startswith("https://")
    assert env["ANTHROPIC_MODEL"] == "Vendor A-M3"
    assert json.loads(path.read_text(encoding="utf-8"))["hooks"]


def test_apply_leaves_the_file_mode_at_0600(tmp_path: Path) -> None:
    """The rewrite must not re-create the file with the default mode."""
    path = _managed_settings(tmp_path)
    sweeper.sweep([tmp_path], apply=True)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    path = _managed_settings(tmp_path)
    findings, failures = sweeper.sweep([tmp_path], apply=False)
    assert failures == 0
    assert findings[0].status == "live" and findings[0].needs_action
    assert _env_of(path)["ANTHROPIC_API_KEY"] == "sk-live-key"


def test_second_apply_is_a_no_op(tmp_path: Path) -> None:
    """Idempotence: ``<redacted>`` is a non-empty string, so a naive
    implementation counts it as "there is something to replace" forever."""
    _managed_settings(tmp_path)
    sweeper.sweep([tmp_path], apply=True)
    findings, failures = sweeper.sweep([tmp_path], apply=True)
    assert failures == 0
    assert findings[0].status == "clean"


def test_a_reportedly_failed_redaction_is_surfaced_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``redact()`` returns False on write failure rather than raising.

    Reported as "clean" that would mean telling the operator a leak is
    fixed while it is still on disk, so the sweeper re-reads the file.
    """
    path = _managed_settings(tmp_path)
    monkeypatch.setattr(sweeper, "redact", lambda _p, **k: False)

    findings, failures = sweeper.sweep([tmp_path], apply=True)
    assert failures == 1
    assert findings[0].status == "failed"
    assert "still present" in findings[0].detail


@pytest.mark.parametrize("payload,expected", [
    ({"env": {"ANTHROPIC_API_KEY": ""}}, []),
    ({"env": {"ANTHROPIC_API_KEY": REDACTED}}, []),
    ({"env": {}}, []),
    ({"env": {"ANTHROPIC_API_KEY": "sk-x"}}, ["ANTHROPIC_API_KEY"]),
])
def test_live_credential_detection(payload: dict, expected: list,
                                   tmp_path: Path) -> None:
    """A blank value is legitimate — ``SubagentConfig`` emits
    ``ANTHROPIC_AUTH_TOKEN`` unconditionally and it is empty for a
    provider that only uses an API key."""
    path = tmp_path / "x.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert sweeper.live_credential_keys(path) == expected


def test_unparseable_file_is_reported_not_crashed(tmp_path: Path) -> None:
    path = _managed_settings(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    findings, failures = sweeper.sweep([tmp_path], apply=True)
    assert failures == 0
    assert findings[0].status == "unreadable"


# ---------------------------------------------------------------------------
# Empty-directory pruning
# ---------------------------------------------------------------------------


def _age(path: Path, seconds: float) -> None:
    stamp = os.stat(path).st_mtime - seconds
    os.utime(path, (stamp, stamp))


def test_prunes_an_old_empty_managed_directory(tmp_path: Path) -> None:
    stale = tmp_path / "pdt-subagent-old"
    stale.mkdir()
    _age(stale, 3600)
    removed = sweeper.prune_empty_dirs([tmp_path], min_age_sec=600, apply=True)
    assert removed == [stale]
    assert not stale.exists()


def test_does_not_prune_a_directory_that_has_a_file_in_it(
    tmp_path: Path,
) -> None:
    path = _managed_settings(tmp_path)
    _age(path.parent, 3600)
    assert sweeper.prune_empty_dirs([tmp_path], min_age_sec=600, apply=True) == []
    assert path.exists()


def test_does_not_prune_a_fresh_directory(tmp_path: Path) -> None:
    """The age gate is what stops this racing a dispatch that has just
    created its directory and not yet written the payload into it."""
    fresh = tmp_path / "pdt-subagent-fresh"
    fresh.mkdir()
    assert sweeper.prune_empty_dirs([tmp_path], min_age_sec=600, apply=True) == []
    assert fresh.exists()


def test_does_not_prune_a_directory_that_is_not_ours(tmp_path: Path) -> None:
    other = tmp_path / "some-other-tool-state"
    other.mkdir()
    _age(other, 3600)
    assert sweeper.prune_empty_dirs([tmp_path], min_age_sec=600, apply=True) == []
    assert other.exists()


def test_prunes_an_old_empty_fallback_lock_directory(tmp_path: Path) -> None:
    """``pdt-ws-locks-*`` is the other per-workspace directory writer.

    It is keyed by workspace digest, so a suite that gives every case its
    own ``tmp_path`` mints one per case. Its writer creates the directory
    with ``exist_ok=True``, which is what makes removing an empty one
    safe: the next writer recreates it instead of hitting ENOENT.
    """
    stale = tmp_path / "pdt-ws-locks-0123456789abcdef"
    stale.mkdir()
    _age(stale, 3600)
    assert sweeper.prune_empty_dirs([tmp_path], min_age_sec=600, apply=True) == [stale]
    assert not stale.exists()


def test_a_lock_directory_holding_a_lock_file_is_left_alone(
    tmp_path: Path,
) -> None:
    """Empty is the only eligible state, and that is the safety argument.

    Removing a directory a running process holds a lock in would break
    exclusion *silently*: the next process would create a fresh directory,
    find no lock in it, and proceed alongside the one already holding it.
    """
    held = tmp_path / "pdt-ws-locks-0123456789abcdef"
    held.mkdir()
    (held / "deadbeef.lock").write_text("", encoding="utf-8")
    _age(held, 3600)

    assert sweeper.prune_empty_dirs([tmp_path], min_age_sec=600, apply=True) == []
    assert (held / "deadbeef.lock").exists()


def test_the_retention_gate_is_thirty_days_and_the_boundary_keeps(
    tmp_path: Path,
) -> None:
    """The number is pinned, and both sides of it are exercised.

    Two failure modes with the same invisible symptom — the directories
    just never go away. A gate that quietly widened, and a gate that
    nobody re-checks because no test mentions it, are indistinguishable
    from the outside. The other tests in this section all pass the
    constant in symbolically, so none of them would notice a change to
    it; this one states the value.

    The sides are probed a minute either side rather than exactly on the
    boundary: the rule compares against a ``time.time()`` it reads
    itself, so a directory whose mtime is exactly one gate old has
    already crossed by the time the comparison runs, and a test that
    claimed otherwise would be asserting a coincidence rather than the
    rule.
    """
    gate = sweeper.DEFAULT_RESIDUE_MAX_AGE_SEC
    assert gate == 30 * 24 * 3600, (
        f"the retention gate is {gate / (24 * 3600):.1f} days; this test "
        f"pins 30, so a change to the number is a deliberate one"
    )

    inside = _managed_settings(tmp_path, dir_name="pdt-subagent-in")
    past_it = _managed_settings(tmp_path, dir_name="pdt-subagent-out")
    _age(inside.parent, gate - 60)
    _age(past_it.parent, gate + 60)

    removed = sweeper.prune_aged_residue([tmp_path], max_age_sec=gate, apply=True)

    assert removed == [past_it.parent]
    assert inside.exists(), "a directory a minute inside the gate must be kept"
    assert not past_it.parent.exists()


# ---------------------------------------------------------------------------
# Aged-residue pruning — the rule the empty-directory prune cannot express
# ---------------------------------------------------------------------------


def test_prunes_an_aged_directory_that_still_holds_a_payload(
    tmp_path: Path,
) -> None:
    """The population that grows by one per dispatch is never empty.

    A dispatch's directory holds the redacted payload kept for post-mortem
    reading, so it is not empty and never becomes empty — which is exactly
    why the empty-directory prune can never reach it. Age is the gate that
    does: the directory's mtime is when the dispatch stopped touching it,
    and the child that wrote into it lives for minutes.
    """
    path = _managed_settings(tmp_path)
    _age(path.parent, 200 * 24 * 3600)

    removed = sweeper.prune_aged_residue(
        [tmp_path],
        max_age_sec=sweeper.DEFAULT_RESIDUE_MAX_AGE_SEC,
        apply=True,
    )

    assert removed == [path.parent]
    assert not path.parent.exists()


def test_keeps_an_aged_payload_directory_inside_the_gate(
    tmp_path: Path,
) -> None:
    """Inside the gate the copy is still the post-mortem record, and stays."""
    path = _managed_settings(tmp_path)
    _age(path.parent, 24 * 3600)

    assert sweeper.prune_aged_residue(
        [tmp_path],
        max_age_sec=sweeper.DEFAULT_RESIDUE_MAX_AGE_SEC,
        apply=True,
    ) == []
    assert path.exists()


def test_the_age_rule_leaves_lock_directories_alone(tmp_path: Path) -> None:
    """``pdt-ws-locks-*`` is excluded from the age rule on purpose.

    Its mtime records when the directory was *made*, not when it was last
    used: the writer creates it once with ``exist_ok=True``, and only the
    first lock file inside it touches the directory again. A workspace in
    constant use can therefore sit in a directory whose mtime is months
    old — and removing it out from under a process holding a lock inside
    would break exclusion silently.
    """
    held = tmp_path / "pdt-ws-locks-0123456789abcdef"
    held.mkdir()
    (held / "deadbeef.lock").write_text("", encoding="utf-8")
    _age(held, 200 * 24 * 3600)

    assert sweeper.prune_aged_residue(
        [tmp_path],
        max_age_sec=sweeper.DEFAULT_RESIDUE_MAX_AGE_SEC,
        apply=True,
    ) == []
    assert (held / "deadbeef.lock").exists()


def test_the_age_rule_removes_directories_only(tmp_path: Path) -> None:
    """No file is ever removed by this rule — only whole directory trees."""
    stray = tmp_path / "pdt-subagent-not-a-directory"
    stray.write_text("", encoding="utf-8")
    _age(stray, 200 * 24 * 3600)

    assert sweeper.prune_aged_residue(
        [tmp_path],
        max_age_sec=sweeper.DEFAULT_RESIDUE_MAX_AGE_SEC,
        apply=True,
    ) == []
    assert stray.exists()


def test_the_age_rule_does_not_follow_a_symlink(tmp_path: Path) -> None:
    """A symlink named like our residue is neither resolved nor removed.

    The type check is what excludes it (``is_dir(follow_symlinks=False)``),
    which is the property that matters: a symlink planted in a
    world-writable temp root must not be able to point the recursive
    removal at a tree of its choosing.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "pdt-subagent-symlinked"
    link.symlink_to(outside)

    assert sweeper.prune_aged_residue(
        [tmp_path],
        max_age_sec=sweeper.DEFAULT_RESIDUE_MAX_AGE_SEC,
        apply=True,
    ) == []
    assert (outside / "keep.txt").exists()


def test_aged_residue_pruning_is_a_dry_run_without_apply(tmp_path: Path) -> None:
    """``apply`` is the switch, as everywhere else in this module."""
    path = _managed_settings(tmp_path)
    _age(path.parent, 200 * 24 * 3600)

    removed = sweeper.prune_aged_residue(
        [tmp_path],
        max_age_sec=sweeper.DEFAULT_RESIDUE_MAX_AGE_SEC,
        apply=False,
    )

    assert removed == [path.parent]
    assert path.exists(), "a dry run reported the candidate but removed it"


# ---------------------------------------------------------------------------
# The composed boot-time pass
# ---------------------------------------------------------------------------


def test_the_boot_sweep_does_both_halves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redaction alone leaves the population unbounded.

    Every dispatch mints a directory, the redaction keeps the *file* for
    post-mortem, and a dispatch that died before writing leaves an empty
    directory from the start. Nothing else collects either kind — so a
    boot pass that only redacts still grows the temp root by at least one
    directory per dispatch.

    Age is what bounds the first kind (``prune_aged_residue``); this case
    pins the half that must *not* change. A freshly redacted payload is
    the post-mortem copy this project deliberately keeps, and the wide age
    gate is wide precisely so that an ordinary boot pass never shortens
    its life.
    """
    from utils import secret_sweep

    monkeypatch.delenv(secret_sweep.DISABLE_ENV, raising=False)
    monkeypatch.setattr(secret_sweep, "default_roots", lambda: [tmp_path])

    live = _managed_settings(tmp_path)
    never_written = tmp_path / "pdt-subagent-never-written-into"
    never_written.mkdir()
    _age(never_written, 3600)

    findings, failures, pruned = secret_sweep.sweep_default_roots(apply=True)

    assert failures == 0
    assert _env_of(live)["ANTHROPIC_API_KEY"] == REDACTED
    assert live.parent.exists(), (
        "the redacted payload is the post-mortem copy this project keeps; "
        "the boot pass must not age out one that is still fresh"
    )
    assert pruned == [never_written]
    assert not never_written.exists()


def test_the_boot_sweep_runs_both_removal_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each rule covers a different half of the population.

    The empty-directory rule collects dispatches that died before writing.
    The age rule collects the ones that wrote a payload — the half that
    grows by one per dispatch and is otherwise unreachable. A boot pass
    that ran only the first would look correct while the count kept
    climbing, so both are pinned together here.
    """
    from utils import secret_sweep

    monkeypatch.delenv(secret_sweep.DISABLE_ENV, raising=False)
    monkeypatch.setattr(secret_sweep, "default_roots", lambda: [tmp_path])

    payload = _managed_settings(tmp_path)
    # Model the steady state rather than the first boot: a dispatch redacts
    # its own payload when the child is reaped, long before the directory
    # is old. Ageing a still-live payload and *then* booting would measure
    # from the boot, because redaction rewrites the file atomically and
    # that updates the directory's mtime.
    assert sweeper.sweep([tmp_path], apply=True)[1] == 0
    _age(payload.parent, 200 * 24 * 3600)
    never_written = tmp_path / "pdt-subagent-never-written-into"
    never_written.mkdir()
    _age(never_written, 3600)

    findings, failures, pruned = secret_sweep.sweep_default_roots(apply=True)

    assert failures == 0
    assert sorted(str(p) for p in pruned) == sorted(
        [str(payload.parent), str(never_written)]
    )
    assert not payload.parent.exists()
    assert not never_written.exists()


def test_the_switch_gates_the_prune_half_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling the boot pass must not leave the suite deleting things.

    The switch exists because a suite that boots the app would otherwise
    rewrite the developer's credentials. The prune half is a *deletion*,
    so it has to be gated by the same switch rather than run alongside it.
    """
    from utils import secret_sweep

    monkeypatch.setenv(secret_sweep.DISABLE_ENV, "1")
    monkeypatch.setattr(secret_sweep, "default_roots", lambda: [tmp_path])
    stale = tmp_path / "pdt-subagent-empty"
    stale.mkdir()
    _age(stale, 3600)

    assert secret_sweep.sweep_default_roots(apply=True) == ([], 0, [])
    assert stale.exists()


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_main_dry_run_then_apply(tmp_path: Path, capsys) -> None:
    path = _managed_settings(tmp_path)

    assert _cli.main(["--root", str(tmp_path)]) == 0
    assert "Dry run — nothing was written" in capsys.readouterr().out
    assert _env_of(path)["ANTHROPIC_API_KEY"] == "sk-live-key"

    assert _cli.main(["--root", str(tmp_path), "--apply"]) == 0
    assert "redacted 1 file(s)" in capsys.readouterr().out
    assert _env_of(path)["ANTHROPIC_API_KEY"] == REDACTED


def test_fail_if_dirty_gives_ci_a_non_zero(tmp_path: Path) -> None:
    _managed_settings(tmp_path)
    assert _cli.main(["--root", str(tmp_path), "--fail-if-dirty"]) == 1
    assert _cli.main(["--root", str(tmp_path), "--apply"]) == 0
    assert _cli.main(["--root", str(tmp_path), "--fail-if-dirty"]) == 0


def test_json_report_is_machine_readable(tmp_path: Path, capsys) -> None:
    path = _managed_settings(tmp_path)
    assert _cli.main(["--root", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False
    assert [f["status"] for f in report["findings"]] == ["live"]
    assert report["findings"][0]["path"] == str(path)
    assert report["findings"][0]["keys"] == [
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    ]


def test_root_replaces_the_default_scan_scope(tmp_path: Path, capsys) -> None:
    """Without this the tests would scan the machine's real temp root.

    Also the property an operator relies on when they want to point the
    tool at one directory rather than everywhere.
    """
    _cli.main(["--root", str(tmp_path)])
    assert str(tmp_path) in capsys.readouterr().out


def test_two_roots_are_taken_and_deduplicated(tmp_path: Path) -> None:
    """macOS ``gettempdir()`` is ``/var/folders/…`` while ``/tmp`` is a
    separate path that a symlink usually bridges; the same file must not
    be reported twice."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _managed_settings(a)
    assert len(sweeper.find_candidates([a])) == 1
    assert len(sweeper.find_candidates([a, a])) == 1
    assert len(sweeper.find_candidates([a, b])) == 1


# ---------------------------------------------------------------------------
# Where the sweep looks
# ---------------------------------------------------------------------------


def test_default_roots_covers_the_private_root_the_writers_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep must look where the writers actually write.

    A sweep pointed at a root nothing writes to returns an empty finding
    list, which reads exactly like "this machine is clean" — the one
    outcome a cleanup tool must never be able to produce by accident.
    So the root is read through the same resolver the writer uses rather
    than named here, and this test pins that both the override and the
    per-user default land in the list.
    """
    from utils import secret_files, secret_sweep

    private = tmp_path / "private"
    private.mkdir()
    monkeypatch.setenv(secret_files.PRIVATE_ROOT_ENV_VAR, str(private))
    roots = {p.resolve() for p in secret_sweep.default_roots()}
    assert private.resolve() in roots, (
        "the sweep would not scan the root the writers use; a payload "
        "left by a crash would be reported as no residue at all"
    )

    monkeypatch.delenv(secret_files.PRIVATE_ROOT_ENV_VAR, raising=False)
    default = tmp_path / "default-root"
    default.mkdir()
    # Patch the resolver rather than creating ~/.pdt-scratch: a test must
    # not leave a directory in the developer's home to assert where the
    # code would look.
    monkeypatch.setattr(secret_files, "default_private_root", lambda: default)
    roots = {p.resolve() for p in secret_sweep.default_roots()}
    assert default.resolve() in roots, (
        "with no override the sweep must still cover the per-user default, "
        "or every dispatch's residue is unreachable on a fresh machine"
    )


# ---------------------------------------------------------------------------
# The test-isolation switch
# ---------------------------------------------------------------------------


def test_the_suite_disables_the_machine_wide_sweep() -> None:
    """Asserted from a test rather than trusted to conftest.

    ``sweep_default_roots`` walks the *real* temp roots and overwrites
    credentials it finds, which is correct at boot and wrong when a test
    boots the app — the lifespan tests here do that directly and
    ``test_state_from_db.py`` does it through ``TestClient``'s context
    manager. Deleting the switch would silently re-arm a machine-wide
    rewrite for every one of them, so its presence is pinned.
    """
    from utils import secret_sweep

    assert os.environ.get(secret_sweep.DISABLE_ENV) == "1", (
        "the suite can reach the machine's real temp roots through the "
        "startup sweep; tests must not rewrite the developer's own residue"
    )


def test_the_switch_stops_the_default_roots_entry_point_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One function is disabled; the pipeline behind it is not.

    The switch has to be narrow enough that the sweep stays testable —
    ``sweep`` with explicit roots is how every other case in this file
    runs, and it must keep working while the switch is set.
    """
    from utils import secret_sweep

    monkeypatch.setenv(secret_sweep.DISABLE_ENV, "1")
    assert sweeper.sweep_default_roots(apply=True) == ([], 0, []), (
        "the startup sweep ran against the machine's real temp roots — "
        "it rewrites credentials AND removes directories there"
    )

    path = _managed_settings(tmp_path)
    findings, failures = sweeper.sweep([tmp_path], apply=True)
    assert failures == 0
    assert findings[0].status == "redacted"
    assert _env_of(path)["ANTHROPIC_API_KEY"] == REDACTED


# ---------------------------------------------------------------------------
# The CLI's aged-residue flag
# ---------------------------------------------------------------------------


def test_cli_aged_residue_needs_its_flag(tmp_path: Path, capsys) -> None:
    """``--prune-empty-dirs`` must not quietly grow into the age rule.

    The two rules have very different blast radii, so the operator asks for
    each one by name; a flag that silently picked up the wider rule would
    make a dry run of the narrow one a lie.
    """
    path = _managed_settings(tmp_path)
    # Already redacted, as it would be long before the gate: the age is
    # measured from the last change to the directory, and the CLI redacts
    # on every call.
    assert sweeper.sweep([tmp_path], apply=True)[1] == 0
    _age(path.parent, 200 * 24 * 3600)

    assert _cli.main(
        ["--root", str(tmp_path), "--apply", "--prune-empty-dirs"]
    ) == 0
    capsys.readouterr()
    assert path.parent.exists(), (
        "the empty-directory rule reached a directory that holds a payload"
    )

    assert _cli.main(
        ["--root", str(tmp_path), "--apply", "--prune-aged-residue"]
    ) == 0
    capsys.readouterr()
    assert not path.parent.exists()


def test_cli_aged_residue_is_a_dry_run_by_default(tmp_path: Path, capsys) -> None:
    """Without ``--apply`` the flag reports the candidate and removes nothing."""
    path = _managed_settings(tmp_path)
    assert sweeper.sweep([tmp_path], apply=True)[1] == 0
    _age(path.parent, 200 * 24 * 3600)

    assert _cli.main(["--root", str(tmp_path), "--prune-aged-residue"]) == 0
    capsys.readouterr()
    assert path.exists()


def test_cli_residue_max_age_moves_the_gate(tmp_path: Path, capsys) -> None:
    """The default gate is three months; ``--residue-max-age`` overrides it."""
    path = _managed_settings(tmp_path)
    assert sweeper.sweep([tmp_path], apply=True)[1] == 0
    _age(path.parent, 24 * 3600)

    assert _cli.main(
        ["--root", str(tmp_path), "--apply", "--prune-aged-residue"]
    ) == 0
    capsys.readouterr()
    assert path.exists(), "a day-old directory is well inside the gate"

    assert _cli.main(
        ["--root", str(tmp_path), "--apply", "--prune-aged-residue",
         "--residue-max-age", "60"]
    ) == 0
    capsys.readouterr()
    assert not path.parent.exists()
