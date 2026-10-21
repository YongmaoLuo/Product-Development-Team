#!/usr/bin/env bash
# scripts/install_git_hooks.sh — install this repo's git hooks into .git/hooks/.
#
# Why a native installer, and not only `pre-commit install`
# ---------------------------------------------------------
# ``.pre-commit-config.yaml`` is the canonical declaration of the hooks, but
# the framework is an extra dependency. On a machine that does not have
# ``pre-commit`` the config is inert and ``git commit`` runs nothing at all
# — which is easy to be wrong about, because the file *looks* like it is
# doing something.
#
# This script writes the same hooks directly, so the default clone enforces
# the repo's rules with no toolchain to install first. It is idempotent,
# refuses to overwrite a hook it did not write, and resolves the hooks
# directory through git so it also works inside a linked worktree — where
# ``.git`` is a file, not the directory the hooks actually live in.
#
# Run this once after merging a pull request that touches these hooks
# --------------------------------------------------------------------
# ``.git/hooks/`` is not tracked by git. Merging a change to
# ``scripts/pre_commit_gate.sh`` updates the script every *new* checkout
# would run and the script no *existing* checkout runs, because the hook
# body that decides which gates to run is a file in that untracked
# directory. Nothing in the merge tells anybody, and nothing in CI can
# check it: a GitHub runner has no hooks to check. A checkout whose
# ``pre-commit`` still points at the one-gate version runs one gate,
# reports success, and enforces none of the additions.
#
# So: after merging, run ``bash scripts/install_git_hooks.sh`` on the
# machine. ``--check`` answers "is what is installed what this script
# would write?" and is safe to run at any time — it changes nothing.
#
# Note also that hooks are shared by every linked worktree of a checkout
# (``git rev-parse --git-common-dir``), so installing here changes the
# hooks the other worktrees run too. That is a reason to run it
# deliberately, not to run it from a scratch worktree by accident.
#
# Usage
# -----
#     bash scripts/install_git_hooks.sh          # install / refresh
#     bash scripts/install_git_hooks.sh --check  # report, change nothing
#
# Exit contract
# -------------
#   * 0 — hooks installed (or, with --check, all present and current)
#   * 1 — with --check: at least one hook is missing, stale, or not executable
#   * 2 — setup error (not a git work tree, hooks dir unwritable)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CHECK_ONLY=0
if [[ "${1:-}" == "--check" ]]; then
    CHECK_ONLY=1
elif [[ $# -gt 0 ]]; then
    echo "usage: $0 [--check]" >&2
    exit 2
fi

# Ask git where the hooks live rather than assuming ``$REPO_ROOT/.git/hooks``.
# In a linked worktree ``.git`` is a *file* pointing at the main checkout, so
# the literal path does not exist there and the installer would refuse to run
# in exactly the layout the hooks are most useful — one checkout, several
# worktrees. ``--git-common-dir`` answers with the directory hooks are read
# from in both layouts.
if ! git_dir="$(git -C "$REPO_ROOT" rev-parse --git-common-dir 2>/dev/null)"; then
    echo "error: $REPO_ROOT is not inside a git work tree" >&2
    exit 2
fi
# Older git answers with a path relative to the repository root.
case "$git_dir" in
    /*) ;;
    *)  git_dir="$REPO_ROOT/$git_dir" ;;
esac
HOOKS_DIR="$git_dir/hooks"

if [[ ! -d "$HOOKS_DIR" ]]; then
    echo "error: $HOOKS_DIR not found — is this a git work tree?" >&2
    exit 2
fi

# The marker makes the script's ownership of a hook unambiguous, so a
# hand-written hook is never silently replaced by this one.
MARKER="# installed by scripts/install_git_hooks.sh"

# hook-name<TAB>relative-script<TAB>one-line description
#
# ``pre-commit`` points at ``scripts/pre_commit_gate.sh`` rather than
# directly at ``scripts/grep_guard.sh`` because a git hook is one
# executable and the stage now runs two gates: the forbidden-filename
# sweep, and the staged-content privacy scan added after the 2026-09-29
# publication. The dispatcher keeps each gate in its own wrapper with its
# own exit contract, so neither is reimplemented and both stay runnable
# from a shell.
#
# ``commit-msg`` keeps pointing at the same script it always did. That
# script enforces the plan-id rule and, since 2026-10-21, a narrowed
# AI-attribution rule: a trailer may name the tool that produced the work
# but not the model behind it, because the model is a routing decision made
# elsewhere and is not knowable from the commit. The description names
# both rules it is left with.
HOOKS=(
    "commit-msg	scripts/check_commit_msg.py	rejects AI model names and plan ids in the message"
    "pre-commit	scripts/pre_commit_gate.sh	forbidden-filename sweep, then the staged-content privacy scan"
    "pre-push	scripts/scan_commit_range.sh	scans the commits being pushed for private info"
)

# Render the exact body a hook should hold. Kept as one function so the
# writer and the checker cannot disagree: they call the same renderer and
# compare, rather than each restating what "correct" looks like.
#
# The body stores ``$REPO_ROOT`` unexpanded and resolves it when the hook
# runs, so a checkout that moves does not leave the hook pointing at
# whatever now sits at the old path -- and git runs hooks with the working
# tree as cwd, so this is always the tree being committed to.
render_hook() {
    local script="$1" desc="$2" name="$3"
    local invoke passthrough
    case "$script" in
        *.py) invoke="python3 \"\$REPO_ROOT/$script\"" ;;
        *)    invoke="bash \"\$REPO_ROOT/$script\"" ;;
    esac
    # The body differs by hook kind: a commit-msg hook must pass the
    # message file through ("$1"); pre-push must be told to read the ref
    # lines from stdin, because git gives those on stdin and not in argv;
    # everything else takes no arguments.
    case "$name" in
        commit-msg) passthrough='"$1"' ;;
        pre-push)   passthrough='--stdin-pre-push' ;;
        *)          passthrough="" ;;
    esac
    cat <<EOF
#!/usr/bin/env bash
$MARKER — $desc.
# Edit scripts/install_git_hooks.sh (and the script it points at), not this
# file: it is regenerated on every install.
set -euo pipefail
# Resolved when the hook runs, not baked in when it was installed. A
# checkout that moves would otherwise leave the hook pointing at whatever
# now sits at the old path — and git runs hooks with the working tree as
# cwd, so this is always the tree being committed to.
REPO_ROOT="\$(git rev-parse --show-toplevel)"
$invoke $passthrough
EOF
}

missing=0
for entry in "${HOOKS[@]}"; do
    IFS=$'\t' read -r name script desc <<<"$entry"
    target="$HOOKS_DIR/$name"

    if [[ -f "$target" ]] && ! grep -qF "$MARKER" "$target" 2>/dev/null; then
        echo "skip  $name — exists and was not written by this script" >&2
        continue
    fi

    # Byte-for-byte, against the same renderer that writes the file.
    #
    # The earlier check compared *tokens* — the marker, the script path,
    # the passthrough argument. That caught the two drifts it was written
    # for and nothing else: a hook that had picked up every token it was
    # supposed to have while any other line had been edited, moved to a
    # baked-in absolute path, or quietly stopped being executable, was
    # reported current. A hook is not a bag of tokens; it is a file, and
    # the only question worth asking of a file is whether it equals the
    # one the generator would produce.
    #
    # ``-x`` is part of the same question: git skips a non-executable hook
    # silently, so a lost mode bit disables the entire gate and leaves a
    # token-compatible, therefore "current", file behind.
    expected="$(mktemp)"
    render_hook "$script" "$desc" "$name" >"$expected"
    if [[ $CHECK_ONLY -eq 1 ]]; then
        if [[ ! -f "$target" ]]; then
            echo "stale $name — not installed at all" >&2
            missing=1
        elif ! cmp -s "$expected" "$target"; then
            echo "stale $name — content differs from what this script writes:" >&2
            diff -u "$target" "$expected" | sed 's/^/    /' >&2 || true
            echo "stale $name — run: bash scripts/install_git_hooks.sh" >&2
            missing=1
        elif [[ ! -x "$target" ]]; then
            echo "stale $name — installed but not executable (git skips it)" >&2
            missing=1
        fi
        rm -f "$expected"
        continue
    fi

    cat "$expected" >"$target"
    rm -f "$expected"
    chmod +x "$target"
    echo "ok    $name — $desc"
done

if [[ $CHECK_ONLY -eq 1 ]]; then
    if [[ $missing -ne 0 ]]; then
        echo >&2
        echo "The hooks live in $HOOKS_DIR, which git does not track: merging" >&2
        echo "a pull request that changes them installs nothing. Run" >&2
        echo "    bash scripts/install_git_hooks.sh" >&2
        echo "once per checkout after the merge." >&2
    fi
    exit $missing
fi
