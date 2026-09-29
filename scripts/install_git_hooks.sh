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
# Usage
# -----
#     bash scripts/install_git_hooks.sh          # install / refresh
#     bash scripts/install_git_hooks.sh --check  # report, change nothing
#
# Exit contract
# -------------
#   * 0 — hooks installed (or, with --check, all present and current)
#   * 1 — with --check: at least one hook is missing or stale
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
HOOKS=(
    "commit-msg	scripts/check_commit_msg.py	rejects AI attribution trailers"
    "pre-commit	scripts/grep_guard.sh	forbidden-filename sweep"
)

missing=0
for entry in "${HOOKS[@]}"; do
    IFS=$'\t' read -r name script desc <<<"$entry"
    target="$HOOKS_DIR/$name"

    if [[ -f "$target" ]] && ! grep -qF "$MARKER" "$target" 2>/dev/null; then
        echo "skip  $name — exists and was not written by this script" >&2
        continue
    fi

    # The body differs by hook kind: a commit-msg hook must pass the
    # message file through ("$1"), everything else takes no arguments.
    case "$script" in
        *.py) invoke="python3 \"\$REPO_ROOT/$script\"" ;;
        *)    invoke="bash \"\$REPO_ROOT/$script\"" ;;
    esac
    case "$name" in
        commit-msg) passthrough='"$1"' ;;
        *)          passthrough="" ;;
    esac

    if [[ $CHECK_ONLY -eq 1 ]]; then
        # Compare against the *relative* script path and the marker: the
        # hook body stores ``$REPO_ROOT`` unexpanded, so grepping for the
        # absolute path would report every correctly-installed hook as
        # stale. (It did, on the first run of this script.)
        if [[ ! -f "$target" ]] \
            || ! grep -qF "$MARKER" "$target" 2>/dev/null \
            || ! grep -qF "$script" "$target" 2>/dev/null; then
            echo "stale $name — run: bash scripts/install_git_hooks.sh" >&2
            missing=1
        fi
        continue
    fi

    cat >"$target" <<EOF
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
    chmod +x "$target"
    echo "ok    $name — $desc"
done

if [[ $CHECK_ONLY -eq 1 ]]; then
    exit $missing
fi
