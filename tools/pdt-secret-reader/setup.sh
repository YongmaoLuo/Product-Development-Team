#!/usr/bin/env bash
#
# setup.sh — build this project's keychain reader, and re-file this
# project's secrets so only that binary is trusted to read them.
#
#   ./setup.sh build             compile, create the signing identity if
#                                there is none, and sign. Idempotent;
#                                safe to re-run.
#   ./setup.sh build --new-identity
#                                throw away the signing identity and issue
#                                a different one — for a key pair that
#                                should stop existing: exposed, copied
#                                where it should not have been, rotating.
#                                Destructive by construction: a new
#                                certificate is a new leaf hash, so every
#                                access control list pinned to the old one
#                                stops matching until 'adopt --widen' (one
#                                dialog per item) then 'adopt --narrow'
#                                re-file it. The old cert.pem, key.pem,
#                                identity.pem and identity.p12 are moved
#                                aside as *.abandoned, never deleted; the
#                                copy inside the keychain is NOT touched,
#                                and this stops with the one command that
#                                removes it until you have run that.
#   ./setup.sh adopt --widen     have the system add this reader to every
#                                secret this deployment declares: read each
#                                item once, and when macOS asks about a
#                                binary that is not on the item's list, click
#                                "Always Allow" — the system files this
#                                reader there itself, beside whatever is
#                                already there. Nothing on a list is
#                                disturbed. Run this first.
#   ./setup.sh adopt --narrow    overwrite each of those lists with this
#                                reader alone, dropping every other
#                                application named on it. Run this only
#                                after --widen, a backend restart, and a
#                                notifier that actually came up.
#
# Two commands and two flags. Run it from a normal Terminal, not from
# inside an agent session: it touches the keychain and may need
# interactive confirmation.
#
# Where the arguments went
# ------------------------
# `adopt` needs a keychain and a list of accounts, and takes neither on
# the command line. Not because it has better defaults — because it asks
# the project. `build` asks too now, for the keychain alone: its signing
# identity is filed in the project's own keychain rather than the login
# one, which `.env.example` already says is not used here and which holds
# every credential this account has ever had. That makes `build` no longer
# self-contained, which is the right price: a binary you are about to
# compile having to agree with the project about which keychain it is
# compiling *for*.
#
# This tool ships inside the repository, so the repository is entitled to
# say which keychain it keeps its secrets in and which binary it
# currently reads them through. That is the only question it puts to
# `backend/credentials.py`, and asking beats carrying a copy: a second
# copy of the keychain path in a shell script is a second thing to
# forget to update, and it would be the wrong one the moment anybody
# moved the keychain.
#
# The accounts are the interesting case. The project is asked only for
# the *names* of the variables holding them — names that are already in
# this repository in plain sight, and that this script therefore has no
# business hard-coding. The values are read out of the environment by
# indirect expansion and handed straight to the reader. They do not pass
# through this script, they are not written down in it, and they never
# enter the repository, so this file is identical on every machine that
# runs it.
#
# `adopt` has two modes, and you pick one
# --------------------------------------
# Nothing here is inferred. The script does not read the project .env to
# work out which half of the migration you are in, because the same
# command doing two different things on two different runs is a command
# you cannot predict, and — the reason it was pulled — no such command
# can ever *widen on demand*: with the pointer already set, the only way
# back to a permissive ACL would be to delete a line first. So the mode
# is an argument, and refusing to widen is not something you can reach.
#
#   --widen    The system puts this reader on every declared item's access
#              control list, keeping everything already there. That is one
#              read per item plus one dialog per item, and the answer that
#              counts is "Always Allow". Nothing can break while you switch
#              the backend over. The project .env is pointed at this reader.
#   --narrow   Every one of those lists is overwritten with this reader
#              alone; every other application named on it is dropped. The
#              project .env is pointed at this reader.
#
# Widening is always safe to repeat. Narrowing is not: it removes the
# fallback, so do it only once a --widen has run and the backend has been
# restarted on the new reader with the notifier confirmed up. If you want
# the permissive state back, that is `--widen` again — no editing, no
# ordering to remember.
#
# Both modes record PDT_SECRET_READER_PATH in the project .env, and both
# overwrite it if it is already there. There is nothing to weigh about
# that: the variable can only name one binary, the only binary this script
# can have built is the one just compiled (an older one still in use would
# never have been rebuilt), and the operator has asked for this reader by
# running this script. So "should it be overwritten" is not a question,
# and refusing to answer it would just be a way of not doing the work.
#
# There is no separate command for the signing identity or for signing:
# both are steps of `build`, in an order that is not the caller's to
# remember and not the caller's to get wrong. Issuing another identity is
# a flag on that command rather than a command of its own: the same work
# with one step replaced, and every interface this script has is one more
# to learn, document and keep working.

set -euo pipefail

HERE=""
BUILD=""
IDENTITY_DIR=""
HELPER=""
ENV_FILE=""

#: `PATH` and `IFS` as they were before the project's `.env` ran in this
#: shell, and whether they were captured at all. `_load_project_env`
#: sources that file as shell, so whatever it says about `PATH` comes
#: back out as the `PATH` this script then does its work with — and
#: `dirname` is how this script works out where it lives.
_TRUSTED_PATH=""
_TRUSTED_IFS=""
_TRUSTED_CAPTURED=no

#: Taken once, before anything is sourced, because after the first source
#: there is nothing left to take it *from*.
_capture_trusted_env() {
    _TRUSTED_PATH="$PATH"
    _TRUSTED_IFS="$IFS"
    _TRUSTED_CAPTURED=yes
}

#: Every path this script acts on, in one place. Called once at startup
#: and again after the project's `.env` has been sourced into this shell
#: — that file is arbitrary shell, and a `.env` that happened to define
#: `HELPER` or `PY` must not be able to redirect what gets signed or
#: which interpreter is asked about the keychain.
#:
#: This is defence in depth, not a boundary: anyone who can write `.env`
#: can already run anything as this user. What it stops is a `.env` line
#: like `PATH=/tmp/elsewhere` silently moving this script's idea of where
#: it is — `dirname` and `cd` are PATH lookups, and a `.env` that breaks
#: them used to break it *quietly*.
_set_paths() {
    if [ "$_TRUSTED_CAPTURED" = yes ]; then
        PATH="$_TRUSTED_PATH"
        IFS="$_TRUSTED_IFS"
    fi

    local dir
    dir="$(dirname "${BASH_SOURCE[0]}")"

    #: `cd ""` returns 0 without moving, so `HERE` would silently become
    #: the caller's cwd and `REPO`, `ENV_FILE` and `HELPER` with it, with
    #: `set -e` saying nothing. And `cd` under a set `CDPATH` prints the
    #: directory it resolved to, which lands that line inside `HERE`. So:
    #: no `CDPATH`, and the answer is checked rather than assumed.
    HERE="$(CDPATH= cd -- "$dir" && pwd -P)" \
        || die "cannot resolve the directory holding this script: '$dir'"
    if [ -z "$HERE" ] || [ ! -d "$HERE" ]; then
        die "cannot resolve the directory holding this script: '$dir'"
    fi

    BUILD="$HERE/build"
    IDENTITY_DIR="$BUILD/identity"
    HELPER="$BUILD/pdt-secret-reader"

    #: The checkout this tool shipped in. `build` needs it as much as
    #: `adopt` does now: it is asked which keychain to file the signing
    #: identity in.
    REPO="$(cd "$HERE/../.." && pwd)"
    PY="$REPO/backend/.venv/bin/python3"
    [ -x "$PY" ] || PY="python3"

    #: The deployment's own configuration file: read for the keychain and
    #: the accounts, and appended to when `adopt` switches the backend
    #: over. Derived from `REPO` like everything else, so the same
    #: argument above covers it.
    ENV_FILE="$REPO/.env"
}

_capture_trusted_env

_set_paths

#: The name the signing identity is filed under, and the identifier the
#: binary is signed with. The identifier is what ends up inside the
#: keychain ACL, so changing it later means re-signing and re-adopting
#: every item — decide it before the first adopt, not after.
SIGN_CN="PDT Local Secret Reader"
BUNDLE_ID="com.pdt.secret-reader"
SIGN_DAYS=3650

#: The one assignment this script both *finds* — to know whether there is
#: anything to replace — and *writes*, to make the switch. A single
#: pattern, so the line it goes looking for, the line it rewrites and the
#: line it verifies afterwards are the same shape by construction rather
#: than by three people agreeing. It is handed to `grep -E` and to `sed
#: -E`, which are both POSIX extended regular expressions, so finding and
#: replacing cannot drift apart. Anchored, and with only whitespace
#: allowed before the name: the commented-out example in `.env.example`
#: is documentation, not configuration, and must not read as one.
READER_ASSIGN='^[[:space:]]*(export[[:space:]]+)?PDT_SECRET_READER_PATH[[:space:]]*='

say()  { printf '\n=== %s\n' "$*"; }
warn() { printf '\n!!  %s\n' "$*" >&2; }
die()  { printf '\n!!  %s\n' "$*" >&2; exit 1; }

require_macos() {
    [ "$(uname -s)" = "Darwin" ] || die "this is a macOS keychain; $(uname -s) has none"
}

# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

#: The freshly compiled reader, waiting to be signed. `build` only puts it
#: where `$HELPER` lives once codesign has accepted it — see `_compile`.
STAGED=""

_compile() {
    require_macos
    command -v clang >/dev/null \
        || die "clang not found; install the Command Line Tools (xcode-select --install)"

    mkdir -p "$BUILD"
    STAGED="$BUILD/pdt-secret-reader.new"
    say "compiling $STAGED"

    # ``-Wno-deprecated-declarations``: the SecKeychain API is deprecated in
    # favour of the data-protection keychain, and that is the point. The
    # modern API's cross-application sharing is gated on the
    # ``keychain-access-groups`` entitlement, which needs a provisioning
    # profile and therefore a paid Apple Developer team. The legacy API is
    # the one whose access control list can name a locally-signed binary,
    # which is the whole objective. See README.md.
    #
    # Never straight to `$HELPER`. `clang -o` writes an ad-hoc linker-signed
    # stub, so a run that compiled and then died before signing had already
    # replaced the binary the ACL trusts with one that satisfies nothing —
    # for a deployment that had run `--narrow`, a compile error would take
    # the backend's own secrets with it.
    if ! clang -O2 -Wall -Wextra -Wno-deprecated-declarations \
            -o "$STAGED" "$HERE/pdt-secret-reader.c" \
            -framework Security -framework CoreFoundation; then
        rm -f "$STAGED"
        die "clang failed. Nothing was written: the reader at
    $HELPER
  is the one that was already there."
    fi
}

#: Whether this run was asked to throw the signing identity away. Default
#: `no`, so the flag only ever adds a decision and never takes one away.
NEW_IDENTITY=no

#: `build` with an argument it does not know: exit 2, and the whole build
#: surface, because "build takes something?" is the question being asked
#: and a one-line usage answers it by omission. $IDENTITY_DIR is named and
#: $KC is not — the project has not been asked for a keychain yet.
_build_usage_die() {
    printf '\n!!  %s\n' "$1" >&2
    cat >&2 <<USAGE

  build takes no arguments, or exactly one of:

  (none)               compile, create the signing identity if there is
                       none, and sign. Idempotent; safe to re-run.

  --new-identity       throw the signing identity away and issue a
                       different one. The key pair and its certificate in
                       $IDENTITY_DIR are moved aside as *.abandoned,
                       never deleted; the copy of the identity inside the
                       keychain is left alone, and this stops with the one
                       command that removes it until you have run it.

                       A new certificate is a new leaf hash, so every
                       access control list pinned to the old one stops
                       matching the moment it lands. The way back is:

                           $0 adopt --widen    (one dialog per item)
                           $0 adopt --narrow   (re-file against the new one)

  Full usage: '$0' with no arguments.
USAGE
    exit 2
}

cmd_build() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --new-identity) NEW_IDENTITY=yes ;;
            *) _build_usage_die "build does not take '$1'." ;;
        esac
        shift
    done

    _require_project
    _load_project_env
    _load_deployment
    _require_keychain_enabled

    KC="$DEPLOY_KEYCHAIN"
    _require_project_keychain "$KC"

    _compile
    # The flag skips the shortcut: reaching `cmd_identity` through the
    # reuse branch would quietly sign with the certificate the flag was
    # passed to get rid of.
    if [ "$NEW_IDENTITY" = no ] && have_identity; then
        echo
        echo "signing identity '$SIGN_CN' is already present in $KC — reusing it."
        echo
        echo "That is the safe default, and it is also why a key pair you"
        echo "meant to replace is still here: to replace it on purpose —"
        echo "because it was exposed, or is being rotated — say so:"
        echo
        echo "    $0 build --new-identity"
    else
        cmd_identity
    fi
    _sign
    _next_step
}

# ---------------------------------------------------------------------------
# identity — a step of `build`, not a command of its own
# ---------------------------------------------------------------------------

#: The keychain `build` files the signing identity in and signs against.
#: Set from the project's own answer (`_load_deployment`) before either
#: command acts. It is a global rather than an argument because `have_identity`
#: is asked from four places and threading it through all of them would be
#: the only way to be sure one of them forgot.
KC=""

#: Whether `$KC` already holds an identity under `SIGN_CN`. Deliberately
#: NOT `find-identity -v`: `-v` filters to *valid* identities, and a
#: self-signed certificate is not trusted, so it is always excluded. The
#: identity was there all along and this is what said it was not.
#:
#: Two facts, not one. "There is no such identity" is answered by a `false`,
#: and `cmd_identity` then goes on to mint a certificate. "I could not ask"
#: — a locked keychain, a path that is not there, a policy that blocks
#: `/usr/bin/security` — used to be the same `false`, because the exit
#: status the pipeline returned was grep's and nobody looked at what
#: `security` had said. That sends the operator to create an identity they
#: already have.
#:
#: The exit status cannot be the test, because the two conventions for it
#: disagree and the dangerous one is a *success* path: some `security`
#: builds exit non-zero when the query matched nothing, and that is the
#: ordinary state of a machine that has not run `build` yet. Reading a
#: non-zero exit as "could not ask" would stop a first build forever;
#: reading it as "no identity" is what this function used to do. So the
#: test is whether `security` *answered* instead: its own count line
#: (`     1 identities found`, `  0 valid identities found`) is what an
#: answered query looks like, and its absence means nothing was looked up.
#: stderr is captured rather than dropped because on that path it is the
#: entire diagnosis, and the fix is an unlock or a policy answer, neither of
#: which is this script's to make.
have_identity() {
    [ -n "$KC" ] || return 1

    local out rc=0
    out="$(security find-identity -p codesigning "$KC" 2>&1)" || rc=$?

    if ! printf '%s\n' "$out" \
        | grep -qE '^[[:space:]]*[0-9]+ (valid )?identities found'; then
        die "could not ask $KC whether it holds '$SIGN_CN': 'security
  find-identity' (exit $rc) printed no count of what it found, so this is
  it failing to answer rather than answering 'no'. Nothing is created,
  imported or signed:

$(printf '%s\n' "$out" | sed 's/^/    /')

  Answer that first — the usual causes are a locked keychain, a path that
  does not exist, and a policy that blocks /usr/bin/security (its own
  message says which)."
    fi

    printf '%s\n' "$out" | grep -qF "$SIGN_CN"
}

#: The p12's passphrase, and now only the fallback's: the PEM path hands
#: `security import` a bare key with nothing to unwrap. Not a secret either
#: way — key.pem sits beside it unencrypted (`-nodes`), so whoever has the
#: file has the key.
P12_PASSWORD="pdt-local-signing"

#: What `build --new-identity` sets aside. A fixed list rather than a glob
#: over the directory: `identity.pem` and `identity.p12` are the only
#: copies of the private key left once `_after_import` deletes key.pem —
#: whichever export path ran last — and a glob would move them silently.
_ABANDONED_FILES="cert.pem key.pem identity.pem identity.p12"

#: Get to a state where a new certificate may be issued, or stop. Both
#: refusals come before a byte moves: each is about something this script
#: is not entitled to decide on the operator's behalf.
_abandon_current_identity() {
    local f

    # `mv` over an existing destination replaces it, so a second run of
    # the flag would destroy the first run's evidence without a word —
    # cert.pem being the only record left of which certificate the access
    # control lists were pinned to, and key.pem a private key nobody has
    # asked to have destroyed. Stop instead, and say what is in the way.
    for f in $_ABANDONED_FILES; do
        if [ -e "$IDENTITY_DIR/$f.abandoned" ]; then
            die "$IDENTITY_DIR/$f.abandoned already exists.

  An earlier 'build --new-identity' set it aside and it is still there.
  It is not overwritten: that file may be the only copy of the
  certificate those keychain access control lists were pinned to, and
  of a private key nobody has asked to destroy. Decide what to do with
  it — move it somewhere else, or delete it yourself — and run this
  again.

  Nothing was changed."
        fi
    done

    # Not squeamishness about touching Apple's own store: two certificates
    # with one common name make every lookup by name ambiguous — and
    # `_sign` passes no `--keychain`, because that form reports 'no
    # identity found' even when the identity is right there, so it would
    # sign with the certificate being discarded, or fail for a reason that
    # names neither. Deleting is destructive and Apple's own tool asks
    # first, so the command is printed and the run waits.
    if have_identity; then
        die "$KC still holds an identity named '$SIGN_CN'.

  Issuing a second one alongside it would leave two certificates with
  the same common name in the keychain search list, and from here on
  nothing that goes by name can say which one it means: the identity
  this project looks up would match either, and so would the one
  codesign signs with. This script will not delete it for you.

  Remove the old identity yourself, then run this again:

    security delete-identity -c '$SIGN_CN' '$KC'

  which asks for confirmation and reports what it removed. Nothing on
  disk has been moved and no certificate has been generated."
    fi

    warn "issuing a DIFFERENT signing certificate than the one in use."
    warn "Every access control list pinned to the old one names its leaf"
    warn "hash, and a new certificate has a new one — so from the moment"
    warn "this lands, every secret this project files stops answering to"
    warn "this reader, and the backend will report that it cannot read"
    warn "its own credentials. That is a broken state with a way out:"
    warn
    warn "    $0 adopt --widen     one dialog per item; answer 'Always Allow'"
    warn "    $0 adopt --narrow    re-file every item against the new one"
    warn
    warn "Do the widen first and leave it there until the backend has been"
    warn "restarted on the new reader and the notifier is up, or there is"
    warn "a window in which nothing can read the secrets at all."

    for f in $_ABANDONED_FILES; do
        if [ -e "$IDENTITY_DIR/$f" ]; then
            mv "$IDENTITY_DIR/$f" "$IDENTITY_DIR/$f.abandoned" || die \
"could not move $IDENTITY_DIR/$f aside, so nothing was issued.

  Whatever had already been moved is under its *.abandoned name; the
  rest is untouched. Nothing was deleted and no certificate was made."
            say "moved $f aside as $f.abandoned — nothing was deleted"
        fi
    done
}

#: Export a p12 from whatever key pair is on disk, and get it imported.
cmd_identity() {
    require_macos
    mkdir -p "$IDENTITY_DIR"

    # The flag belongs to `build` and is read once, there; this is the only
    # place in `build` that can act on it.
    if [ "$NEW_IDENTITY" = yes ]; then
        _abandon_current_identity
    elif have_identity; then
        say "a signing identity named '$SIGN_CN' already exists — reusing it"
        warn "Do NOT recreate it. A keychain ACL stores the designated"
        warn "requirement derived from this certificate; regenerating the"
        warn "certificate changes that requirement, and every ACL written"
        warn "with the old one stops matching."
        return 0
    fi

    # Not in the keychain, but the pair is on disk: an earlier run got as
    # far as generating it and never landed the import. Regenerating here
    # is the one act the warning above forbids, and it would happen
    # silently every time `have_identity` answers false — a locked
    # keychain, an emptied one, a transient query failure. Reuse, and
    # redo only the export.
    if [ -f "$IDENTITY_DIR/key.pem" ] && [ -f "$IDENTITY_DIR/cert.pem" ]; then
        say "reusing the key pair already in $IDENTITY_DIR"
    elif [ -f "$IDENTITY_DIR/cert.pem" ]; then
        # Not an edge case: this is the ordinary state of every machine
        # that has run `build` twice, because `_after_import` deletes
        # key.pem. Minting a replacement pair here would be quiet, and it
        # is the one thing that must not be — a new key means a new
        # certificate, and every ACL pinned to this one pins
        # `certificate leaf = H"…"`.
        die "the certificate is still on disk at
    $IDENTITY_DIR/cert.pem
  but its private key is not, and $KC holds no identity named
  '$SIGN_CN'. There is no second copy of that key anywhere: 'build'
  removes key.pem as soon as the keychain has it. Importing this
  certificate again needs the key that went with it, so this cannot be
  retried, and generating a replacement pair here would silently issue a
  DIFFERENT certificate — every access control list this one is pinned
  into would stop matching, and this project's secrets would become
  unreadable rather than merely unbuilt.

  If you are sure the old identity is gone, replace it deliberately:

    mv $IDENTITY_DIR/cert.pem $IDENTITY_DIR/cert.pem.abandoned
    $0 build            # mints a new certificate and signs
    $0 adopt --widen    # answer 'Always Allow' on each dialog
    $0 adopt --narrow   # re-files every item against the new one

  Nothing was changed, and no key was generated."
    else
        say "creating a self-signed code-signing identity: $SIGN_CN"
        echo "No Apple Developer account is needed for this, and none is used."

        # `extendedKeyUsage = critical,codeSigning` is load-bearing:
        # without it the certificate is invisible to
        # `find-identity -p codesigning` and codesign will not accept it.
        openssl req -x509 -newkey rsa:2048 -nodes -days "$SIGN_DAYS" \
            -keyout "$IDENTITY_DIR/key.pem" \
            -out    "$IDENTITY_DIR/cert.pem" \
            -subj   "/CN=$SIGN_CN" \
            -addext "extendedKeyUsage = critical,codeSigning" \
            -addext "basicConstraints = critical,CA:false" \
            -addext "keyUsage = critical,digitalSignature" \
            2>/dev/null
    fi

    _import_identity
}

#: The main path, and the reason the p12 is now only a fallback.
#:
#: `security import -t agg` takes a PEM sequence as it stands — man
#: security: "agg is one of the aggregate types (pkcs12 and PEM
#: sequence)", with `security import /tmp/certs.pem -k` as its own worked
#: example. So the two files already on disk are concatenated and handed
#: over: no openssl, no container, no passphrase, and so no MAC for the
#: keychain to fail a check on. "MAC verification failed during PKCS12
#: import (wrong password?)" — the thing this replaces — is a complaint
#: about a wrapper that no longer exists.
_import_pem_sequence() {
    printf 'importing %s as a PEM sequence\n' "$IDENTITY_DIR/identity.pem"
    cat "$IDENTITY_DIR/cert.pem" "$IDENTITY_DIR/key.pem" \
        > "$IDENTITY_DIR/identity.pem" \
        || die "could not join the certificate and the key into
    $IDENTITY_DIR/identity.pem
  Nothing was imported, and both files are exactly where they were."

    # NOT `-A`, and NOT `-T`. `-A` is "allow any application to access the
    # imported key without warning"; `-T` is "specify an application which
    # may access the imported key", and the only two this tool could name
    # are `/usr/bin/codesign` and `/usr/bin/security` — both `-rwxr-xr-x`,
    # both runnable by anything on this machine. Naming either puts the
    # *signing* key in reach of every process here: one command, no
    # dialog, and a binary satisfying `certificate leaf = H"…"` in
    # whatever ACL stands behind it. That is the skeleton key taken off
    # `/usr/bin/security` and put on `/usr/bin/codesign`.
    #
    # With neither, codesign asks for the keychain password — which is the
    # whole argument for an ACL: the key must not be reachable silently.
    if security import "$IDENTITY_DIR/identity.pem" -t agg -k "$KC"; then
        return 0
    fi
    printf '  the keychain refused the PEM sequence (security says why, above)\n'
    return 1
}

#: The fallback: the same two files in a pkcs12 container. What goes wrong
#: with one is the MAC covering the container — a value the *keychain*
#: checks on import, sealed differently by LibreSSL and by OpenSSL 3 — so
#: two exports, in the order most likely to be accepted first.
#: $1 = openssl binary, $2... = extra `pkcs12 -export` flags.
_try_import_p12() {
    local bin="$1"; shift
    printf 'trying %s pkcs12 -export%s\n' "$bin" "${*:+ $*}"
    local p12="$IDENTITY_DIR/identity.p12" tmp

    # No `2>/dev/null`: a failed export used to come out as a failed
    # *import*, which sends you at the keychain instead of at openssl.
    #
    # Exported to a name of its own and moved into place only once it is a
    # whole p12. Two things go wrong writing straight to `identity.p12`.
    # /usr/bin/openssl (LibreSSL) creates the output before it parses, so a
    # refusal leaves a 0-byte file that Keychain Access will cheerfully
    # accept a password for and then never open; and the previous attempt's
    # p12 is still good — it is exactly the file the "by hand" advice below
    # points an operator at — so this attempt must not be able to truncate
    # it by failing.
    tmp="$(mktemp "$IDENTITY_DIR/.identity.p12.XXXXXX")" \
        || { printf '  could not create a temporary file in %s\n' "$IDENTITY_DIR"
             return 1; }

    if ! "$bin" pkcs12 -export "$@" \
            -inkey "$IDENTITY_DIR/key.pem" \
            -in    "$IDENTITY_DIR/cert.pem" \
            -out   "$tmp" \
            -passout "pass:$P12_PASSWORD"; then
        printf '  %s could not export the p12 (openssl says why, above)\n' "$bin"
        rm -f "$tmp"
        return 1
    fi

    if [ ! -s "$tmp" ]; then
        printf '  %s exited 0 but wrote no p12; nothing was installed\n' "$bin"
        rm -f "$tmp"
        return 1
    fi

    mv -f "$tmp" "$p12"

    if security import "$p12" -k "$KC" -P "$P12_PASSWORD"; then
        return 0
    fi
    printf '  the keychain refused the p12 %s wrote (above); it is still on disk\n' "$bin"
    return 1
}

#: What has to be true before the disk copy of the private key goes away,
#: and what it costs when it does.
_after_import() {
    # Exit 0 from `security import` is necessary and not sufficient: an
    # item can land and still not be a codesigning identity, and the only
    # thing that settles that is the same query `_sign` will make.
    have_identity || die \
"the keychain took the import but '$KC' still does not list
  '$SIGN_CN' as a codesigning identity, so nothing was signed with and
  there is nothing to keep. Nothing was deleted: cert.pem and key.pem are
  both still in $IDENTITY_DIR, and re-running
  '$0 build' retries the import against them."

    # key.pem goes, cert.pem stays. cert.pem is public — it is what the
    # ACL's designated requirement was derived from, and keeping it is how
    # you can still answer "which certificate is that list pinned to?"
    # later. key.pem is `-nodes`, in the clear, readable by anything that
    # can read this directory: strictly more exposed than the copy now
    # behind the keychain's ACL.
    #
    # The cost, plainly: the key now exists in exactly one place. Lose the
    # keychain — moved, deleted, rebuilt from its creation code, restored
    # onto another machine — and this certificate can never be signed with
    # again. The only way back is the deliberate one: a new identity, then
    # `adopt --widen` (asks, per item) and `adopt --narrow` (re-files
    # them).
    rm -f "$IDENTITY_DIR/key.pem" \
          "$IDENTITY_DIR/identity.pem" \
          "$IDENTITY_DIR/identity.p12"

    say "done"
    # A display, not a check: `have_identity` above is the check, and it has
    # already stopped this run if the query could not be answered. Left able
    # to fail, this last line of `_after_import` would take the whole script
    # down with `set -e` — silently, after the reader was already in place.
    security find-identity -p codesigning "$KC" | grep -F "$SIGN_CN" || true
}

_import_identity() {
    say "importing it into $KC"

    if _import_pem_sequence; then
        _after_import
        return 0
    fi
    rm -f "$IDENTITY_DIR/identity.pem"

    # 1. The system LibreSSL, no `-legacy`: it has no such flag, and its
    #    default output already is the format Apple's own tools read.
    if [ -x /usr/bin/openssl ] && _try_import_p12 /usr/bin/openssl; then
        _after_import
        return 0
    fi

    # 2. Whatever is on PATH — usually OpenSSL 3, which needs `-legacy`
    #    to emit the older algorithms.
    if _try_import_p12 openssl -legacy; then
        _after_import
        return 0
    fi

    # Only worth offering the GUI route when there is something to drag.
    # Pointing an operator at a file openssl failed to write is how they
    # spend an afternoon on a dead end.
    local by_hand=""
    if [ -s "$IDENTITY_DIR/identity.p12" ]; then
        by_hand="
  Last resort, by hand: a p12 is still on disk at
    $IDENTITY_DIR/identity.p12
  Drag it into Keychain Access and give it the password '$P12_PASSWORD'.
  A file the GUI takes and 'security import' will not is a known macOS
  roadblock, not something this script can code around."
    else
        by_hand="
  No usable p12 was left behind — neither openssl wrote one, and an empty
  file is not something to hand to Keychain Access."
    fi

    die \
"neither $KC nor either export would take this identity — see what openssl
  and security said above, which is the only thing that says which. If the
  PEM import was refused for a reason of its own, that message is the one to
  read.
$by_hand

  Then re-run '$0 build' — the key pair is still on disk, so it retries the
  import and does not mint a new certificate."
}

# ---------------------------------------------------------------------------
# signing — likewise a step of `build`
# ---------------------------------------------------------------------------

cert_sha1() {
    openssl x509 -in "$IDENTITY_DIR/cert.pem" -outform der \
        | shasum -a 1 | awk '{print $1}'
}

#: Sign the staged reader and print the designated requirement codesign
#: produced. Signs `$STAGED`, not `$HELPER` — see `_compile`. There is
#: deliberately no `--keychain`: with one, codesign reports `no identity
#: found` even when the identity is there, the keychain is unlocked and
#: the path is right. It only ever looks in the user's search list — see
#: `_ensure_searchable`.
sign_with() {   # $1 = explicit requirements, or "" to let codesign derive it
    if [ -n "$1" ]; then
        codesign --force --sign "$SIGN_CN" --identifier "$BUNDLE_ID" \
            --requirements "$1" --timestamp=none "$STAGED" || return 1
    else
        codesign --force --sign "$SIGN_CN" --identifier "$BUNDLE_ID" \
            --timestamp=none "$STAGED" || return 1
    fi
    codesign -d -r- "$STAGED" 2>&1 | grep -i designated || true
}

#: Sign with the certificate named outright. The only shape an ACL can live
#: with: stable across rebuilds, and unforgeable without the private key.
#:
#: The leading `=` is part of the requirement-set syntax codesign parses, and
#: it is not optional: `--requirements` takes either a *path* to a
#: requirement file or a requirement set, and a set has to say so. Measured
#: with the real codesign — the same string without the `=` comes back
#: `designated => ...: No such file or directory` /
#: `invalid requirement specification`, exit 1; with it, the signature is
#: written and exit 0. `_sign` only comes here when the default DR did not
#: pin this certificate, so on a machine whose default DR already says
#: `certificate leaf` this call is never made at all.
_repin() {
    sign_with "=designated => identifier \"$BUNDLE_ID\" and certificate leaf = H\"$(cert_sha1)\""
}

#: The user's keychain search list, recovered from `security`'s own dump —
#: one entry per element, never a single string that gets word-split later.
#: Written by `_parse_keychain_list` and read by `_ensure_searchable`.
_KC_ENTRIES=()

#: Why `_parse_keychain_list` answers "no" so eagerly. `man security`:
#: "-s  Set the search list to the specified keychains" — replacement, not
#: addition, and there is no "add one" verb to use instead. So the only way
#: to put one more entry on that list is to read it, recover the entries
#: from a dump printed for a human (one per line, indented, each carrying
#: a pair of quotes), and hand them all back.
#:
#: A wrong recovery is the worst kind of failure here, because nothing
#: reports it. `-s` accepts whatever argv it is given, so a path holding a
#: space arrives as two entries, the command exits 0, and the keychain that
#: was on the list is quietly no longer on it — a backend that cannot read
#: its own secrets, with no message anywhere to connect the two. Hence the
#: recovery is checked against itself before a byte is written, and a list
#: that does not survive that check is refused with a command the operator
#: can run and read, because a refusal is loud and a wrong write is not.
_KC_LIST_UNPARSED=""

#: $1 = raw output of `security list-keychains -d user`. Sets `_KC_ENTRIES`,
#: returns non-zero with `_KC_LIST_UNPARSED` set rather than exiting: the
#: caller has to decide whether stopping is still true, and before a write it
#: is while after a write it is not.
_parse_keychain_list() {
    local raw="$1" line indent body entry quoted again
    local lines=0 entries=0 shape=""
    _KC_ENTRIES=()
    _KC_LIST_UNPARSED=""

    [ -n "$raw" ] || return 0

    while IFS= read -r line; do
        lines=$((lines + 1))
        indent="${line%%[![:space:]]*}"
        body="${line#"$indent"}"

        case "$body" in
            \"*\")
                quoted=yes
                entry="${body:1:${#body}-2}"
                ;;
            \"*)
                # An opening quote this line never closes is an entry
                # carrying a newline, which is the one dump shape that is
                # its own diagnosis: an operator reading "two keychains were
                # written into one entry" knows to put them on as two.
                # Named here rather than left to the rule below, which
                # reports it as a stray quote and not as the fault it is.
                _KC_LIST_UNPARSED="entry $lines contains a newline, so two keychains were written into one entry and codesign will not find either"
                return 1
                ;;
            *)
                quoted=no
                entry="$body"
                ;;
        esac

        # A quote or a backslash inside an entry is where the dump's own
        # escaping would be, and an escaped byte cannot be handed back to
        # `-s` as itself: this is the case a parse cannot see and a naive
        # one mangles, so it is refused rather than repaired.
        case "$entry" in
            '')     _KC_LIST_UNPARSED="line $lines is blank or empty"; return 1 ;;
            *'"'*|*'\'*)
                _KC_LIST_UNPARSED="entry $lines carries a quote or a backslash, which cannot be handed back to '-s' as the same bytes"
                return 1
                ;;
        esac

        # One shape for the whole dump. The comparison below only proves
        # something if every line was laid out the same way, and a line
        # that was not is a line this script has no rule for.
        if [ -z "$shape" ]; then
            shape="$indent:$quoted"
        elif [ "$shape" != "$indent:$quoted" ]; then
            _KC_LIST_UNPARSED="line $lines is laid out differently from the lines above it"
            return 1
        fi

        if [ "$quoted" = yes ]; then
            printf -v again '%s"%s"' "$indent" "$entry"
        else
            printf -v again '%s%s' "$indent" "$entry"
        fi
        if [ "$again" != "$line" ]; then
            _KC_LIST_UNPARSED="line $lines does not survive being parsed and put back"
            return 1
        fi

        _KC_ENTRIES[${#_KC_ENTRIES[@]}]="$entry"
        entries=$((entries + 1))
    done <<< "$raw"

    if [ "$lines" -ne "$entries" ]; then
        _KC_LIST_UNPARSED="$lines lines came back as $entries entries"
        return 1
    fi
    return 0
}

#: $1 = the raw output, printed back verbatim: an operator comparing it with
#: what they see from their own Terminal is the check there is, and a refusal
#: they can read beats a rewrite they cannot check.
_kc_list_refuse() {
    die "will not rewrite your keychain search list: $_KC_LIST_UNPARSED, and
  'security list-keychains -s' sets the list to the arguments it is given
  rather than adding to it — a list recovered wrongly is still written, and
  the keychain that fell out of it fails much later and somewhere else.

  'security list-keychains -d user' printed this, verbatim:

$1

  Nothing was written, so the list is exactly as it was. To add '$KC' by
  hand, and to read the result back yourself:

    security list-keychains -d user -s \\
        'each entry above, one argument each, exactly as printed' '$KC'
    security list-keychains -d user"
}

#: codesign only finds an identity that is in the user's keychain search
#: list. Measured: with `--keychain` naming the file and the identity
#: valid, unlocked and correctly spelled, it still answers `no identity
#: found`; adding that same file to the search list makes the identical
#: command succeed.
#:
#: Everything below asks one question — is this keychain already on that
#: list — and answers it out of parsed entries, never out of the dump as
#: text. The dump is a rendering for a person, so a substring of it can be
#: part of an entry rather than the whole of one, and "already there" said
#: on that evidence is a build that goes on to sign with nothing there.
#:
#: Appended and never undone, because the two directions cost different
#: amounts. One extra entry is visible (`security list-keychains` shows it)
#: and harmless; taking it back off means writing the whole list a second
#: time out of a *parsed* copy of it, where one bad parse costs a keychain
#: the backend still needs. Also: every entry here has been through
#: `_parse_keychain_list`, which refuses quotes and backslashes, so quoting
#: each one for the printed undo command below reproduces it exactly.
_ensure_searchable() {
    local raw after entry same undo kc_canon i=0
    local was=() want=()

    raw="$(security list-keychains -d user)" \
        || die "could not read your keychain search list; security's own
  message is above, and nothing was written. codesign can only find an
  identity that is on that list, so this has to be answered before
  signing."

    # 'is it already on the list' is a question about whole entries. It is
    # never asked of the dump as text, because "the string appears somewhere
    # in there" and "this keychain is on the list" are different answers: an
    # entry holding a newline prints as
    #     "/…/login.keychain-db
    # /…/runtime-secrets.keychain-db"
    # — one entry, two keychains — and it *contains* '$KC' without being it,
    # which is how this call used to report the list already carried the
    # keychain and let codesign fail later with `no identity found`.
    # `_resolve` first, so two spellings of one file answer one way.
    kc_canon="$(_resolve "$KC")"

    if ! _parse_keychain_list "$raw"; then
        # A dump that cannot be reproduced is refused, full stop. Whether
        # '$KC' is somewhere in it is not evidence that it is *on* the list,
        # and answering "it is already there" on that basis is what hid the
        # failure this refusal now exists to stop.
        _kc_list_refuse "$raw"
    fi

    for entry in ${_KC_ENTRIES[@]+"${_KC_ENTRIES[@]}"}; do
        was[${#was[@]}]="$entry"
        if [ "$(_resolve "$entry")" = "$kc_canon" ]; then
            return 0
        fi
    done

    # Every entry is one argument, quoted by being an element of the array.
    security list-keychains -d user -s \
        ${_KC_ENTRIES[@]+"${_KC_ENTRIES[@]}"} "$KC" \
        || die "could not add '$KC' to your keychain search list; codesign
  can only see identities that are on it. security's own message is above."

    # What is claimed below is claimed only after reading the list back, so
    # "your existing entries are untouched" is something this checked rather
    # than something it hopes.
    for entry in ${_KC_ENTRIES[@]+"${_KC_ENTRIES[@]}"}; do
        want[${#want[@]}]="$entry"
    done
    want[${#want[@]}]="$KC"

    same=no
    if after="$(security list-keychains -d user)" \
        && _parse_keychain_list "$after" \
        && [ "${#_KC_ENTRIES[@]}" -eq "${#want[@]}" ]; then
        same=yes
        for entry in ${_KC_ENTRIES[@]+"${_KC_ENTRIES[@]}"}; do
            if [ "$entry" != "${want[$i]}" ]; then same=no; fi
            i=$((i + 1))
        done
    fi

    if [ "$same" = yes ]; then
        warn "added '$KC' to your keychain search list; your existing entries are untouched — the list was read back after the write and compared, entry for entry and in order."
    else
        warn "added '$KC' to the keychain search list, but it does not read back the way it was written, so codesign may still not find the identity. Run 'security list-keychains -d user' and look at it yourself; this script cannot say what is on it."
    fi
    warn "  it was:"
    for entry in ${was[@]+"${was[@]}"}; do
        warn "    \"$entry\""
    done
    undo="  undo: security list-keychains -d user -s \\"
    for entry in ${was[@]+"${was[@]}"}; do
        printf -v undo '%s\n    "%s" \\' "$undo" "$entry"
    done
    printf -v undo '%s' "${undo% \\}"
    warn "$undo"
}

#: Let codesign use the key at all. A property of the key once it is *in*
#: the keychain, not a step of importing it — and that is why it cannot
#: live on the import path. `have_identity` answers a different question
#: (is a certificate and key of this name in there) and answers yes for an
#: identity an earlier run imported, so a build that reuses one skips the
#: import entirely; codesign then refuses a key with no partition list by
#: printing "no identity found", with no dialog, which reads like a
#: missing certificate rather than an unusable key. Hence unconditional,
#: before signing, every time.
#:
#: A setter rather than an addition: on a key that already carries this
#: list it changes nothing, which is what makes running it every build
#: safe.
#:
#: `security` collects the keychain password itself and this script never
#: sees it. The obvious alternative — prompt here, hand it over with `-k`
#: — puts that password into this process's argv, and `ps` prints argv to
#: every other process on the machine. A script whose whole argument is
#: that only one binary may read a secret cannot publish the keychain's
#: own password to get there. The price is a dialog on every build rather
#: than only the run that imported the key: paid, not avoided.
#:
#: A refusal costs a re-run, not a key. This is called from `_sign`, so it
#: runs after `_after_import` removed key.pem — by which point the key is
#: in the keychain, checked by the `have_identity` that guards the call.
#:
#: `security`'s own words stay above the stop rather than being dropped,
#: and whether omitting `-k` prompts and succeeds is left to be seen on
#: the operator's machine — it is not a question this script can answer.
#: Not in tension with the missing `-T` on `security import`: man security
#: says "if you'd like to run /usr/bin/codesign with the key, \"apple:\"
#: must be an element of the partition list", which is about which tool
#: may use the key, not who may take it unnoticed.
_ensure_partition_list() {
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s "$KC" \
        || die "could not set the partition list on the key in $KC — this is
  the step that makes codesign able to use that key at all, and the one
  whose absence shows up later as a bare

    $SIGN_CN: no identity found

  with no dialog and no hint of what is wrong with it. 'security' said
  why, above.

  Nothing was signed and nothing was replaced: $HELPER is the one that was
  already there. The key itself is in the keychain, so re-running
  '$0 build' retries this and mints no second certificate."
}

#: codesign said no. Its own words are above and are deliberately not
#: swallowed: a signing failure with the reason discarded sends the
#: operator off to inspect certificates while the refusal goes unread.
#: What is *not* done here is carry on quietly — `$HELPER` stays exactly
#: as it was, so an already-narrowed deployment keeps reading.
_sign_failed() {
    rm -f "$STAGED"
    die "codesign would not sign the reader — its own message is above, and
  that is the one to read.

  Nothing was replaced: $HELPER is untouched and the half-built file
  beside it is gone, so a backend already pointed at it is still fine.

  If a key-access dialog came up, that is the refusal explained: answer
  ALLOW and type the keychain password. That grant lasts this run, which is
  the point — 'Always Allow' would add /usr/bin/codesign to this private
  key's access list permanently, and leaving -T off 'security import' is
  the only reason the key is not already reachable from every process here.

  A refusal with no dialog at all means the identity was never looked for:
  run 'security list-keychains -d user' and check $KC is in it."
}

_sign() {
    if [ -z "$STAGED" ] || [ ! -f "$STAGED" ]; then
        die "no freshly compiled reader to sign — run '$0 build'"
    fi
    if ! have_identity; then
        die "no signing identity in $KC — run '$0 build'"
    fi
    _ensure_searchable
    _ensure_partition_list

    say "signing $STAGED"
    local dr
    if ! dr="$(sign_with "")"; then
        _sign_failed
    fi

    # What the ACL will store is this requirement, and what it must be is
    # narrow *and* stable. Two ways to get it wrong, pulling opposite ways:
    #
    #   identifier-only -> survives every rebuild, but is satisfied by ANY
    #                      binary claiming that identifier, including one
    #                      somebody else compiles. Close to `-A` in
    #                      strength, and the exact weakness Apple's own
    #                      note warns about ("another app could gain access
    #                      by mimicking this app").
    #   cdhash-pinned   -> narrow, but dies on the next rebuild.
    #
    # Pinning the *leaf certificate* is both: the certificate does not
    # change when the source does, and nobody can sign with it without the
    # private key.
    case "$dr" in
        *cdhash*)
            warn "the default DR pins the binary's cdhash, which changes on"
            warn "every rebuild. Re-signing with the certificate pinned."
            if ! dr="$(_repin)"; then _sign_failed; fi
            ;;
        *"certificate leaf"*) ;;
        *)
            warn "the default DR does not pin this certificate — any binary"
            warn "claiming '$BUNDLE_ID' would satisfy it. Re-signing with"
            warn "the certificate pinned."
            if ! dr="$(_repin)"; then _sign_failed; fi
            ;;
    esac

    say "the designated requirement that will be stored in the ACL"
    echo "  $dr"
    echo "  certificate sha1: $(cert_sha1)"

    #: `codesign` exiting 0 is not the claim; this text is. Signing works
    #: with an untrusted certificate — measured — but only `certificate
    #: leaf` is what the ACL compares. `anchor trusted` or `certificate
    #: root` would mean the scheme silently changed underneath us, and has
    #: to be found here rather than in production.
    case "$dr" in
        *"identifier \"$BUNDLE_ID\""*"certificate leaf"*) ;;
        *)
            rm -f "$STAGED"
            die "the designated requirement is not the certificate-pinned one:
    ${dr:-<codesign reported none>}
  codesign signed this happily, which is not the same as producing the
  requirement the access control lists will store. Nothing was replaced:
  $HELPER is as it was, and the half-built file beside it is gone."
            ;;
    esac

    #: The one moment `$HELPER` changes. Until here it is whatever the
    #: last successful build left, and every `die` above leaves it alone.
    mv -f "$STAGED" "$HELPER"
    STAGED=""
    echo "built and signed: $HELPER"

    say "stable, and not spoofable"
    cat <<'STABLE'
The DR pins the certificate, not the binary. So:

  rebuild the reader (edit the C, new clang, new SDK)  -> DR unchanged
  re-sign the same source with the same certificate    -> DR unchanged
  regenerate the signing certificate                   -> DR CHANGES

Only the last one costs you, it is a deliberate act, and `build` refuses
to recreate an existing signing identity, so it cannot happen by
accident. If it ever does, run `./setup.sh adopt` again.
STABLE
}

#: What to do about the reader now there is one. `build` writes
#: PDT_SECRET_READER_PATH on no branch — see `cmd_adopt`, which spells out
#: why — so "signed" and "in use" are separate states, and closing that
#: gap is `adopt`'s. Which one this is is read, not assumed:
#: `_env_reader_path` says what the file has, `_same_reader` whether that
#: is this binary (through `_resolve`, so a symlinked spelling still counts).
_next_step() {
    local r
    r="$(_env_reader_path)"
    say "next step"
    if [ -z "$r" ]; then
        cat <<D
  $ENV_FILE names no reader and 'build' writes none, so the backend
  reads the keychain through what it read before. To trust this one:
    $0 adopt --widen — one dialog per secret: answer Always Allow
    restart the backend, confirm 'FeishuClient initialized'
    $0 adopt --narrow — only after that, never before
D
    elif _same_reader "$r"; then
        cat <<D
  $ENV_FILE already names this reader. To leave it the only application
  those secrets answer to:  $0 adopt --narrow — nothing must run first.
  Restart the backend afterwards, and confirm 'FeishuClient initialized'.
D
    else
        cat <<D
  $ENV_FILE names a different reader, which 'build' does not touch:
    recorded:  $r
    this build: $HELPER
  To switch over: $0 adopt --widen, restart, confirm the notifier, then
  $0 adopt --narrow — widen first; narrowing first is what breaks it.
D
    fi
}

# ---------------------------------------------------------------------------
# asking the project for its configuration
# ---------------------------------------------------------------------------

#: The keychain both commands act on. `adopt` files this reader onto the
#: secrets; `build` files the *signing identity* into it, which is not the
#: smaller act — a private key that can mint a signature satisfying any
#: ACL this project writes is at least as much to protect as the secrets
#: behind those ACLs.
_require_project_keychain() {
    if [ "${1##*/}" = "login.keychain-db" ]; then
        die \
"refusing to use $1 — that is the login keychain, which holds every
  credential on this account rather than this project's. Point
  PDT_KEYCHAIN_PATH in $REPO/.env at a dedicated keychain, then run this
  again. Nothing was read, written or imported."
    fi
    if [ ! -f "$1" ]; then
        die \
"no keychain at $1.
  Create it, or set PDT_KEYCHAIN_PATH in $REPO/.env to the keychain this
  project keeps its secrets in. A relative path there is taken against your
  home directory. Nothing was read, written or imported."
    fi
}

#: What `_deployment` reported, and the accounts it declared. Globals
#: rather than locals because `while read` is the natural way to consume
#: key/value lines and a pipeline would run it in a subshell, taking the
#: accumulated names with it.
DEPLOY_KEYCHAIN=""
DEPLOY_KEYCHAIN_DISABLED=""
ACCOUNT_ENVS=()

_require_project() {
    [ -f "$REPO/backend/credentials.py" ] || die \
"cannot find $REPO/backend/credentials.py.
  setup.sh asks this project's own Python where it keeps its secrets, so
  it has to be run from a checkout, next to that backend/."
}

# Put the project's .env in this process's environment, so the accounts
# can be looked up by name below. Deliberately the *project's* file and
# not a list of variables chosen here: which variables matter is the
# project's business, and a list in this script would be a copy of that
# answer, free to go stale.
_load_project_env() {
    if [ -f "$REPO/.env" ]; then
        # `set +u` around the load: it is somebody else's file, entitled
        # to reference variables this shell never defined, and an unset
        # one would otherwise abort this script before it got to work.
        set +u
        set -a; . "$REPO/.env" >/dev/null 2>&1; set +a
        set -u
    fi
    # That file is arbitrary shell, and it is now running in a shell that
    # has real work to do. Put back what this script is before trusting
    # anything derived below — see _set_paths.
    _set_paths
}

# Print this deployment's configuration as TAB-separated key/value lines.
#
# This is the project's answer, not this script's. Every value here comes
# out of `backend/credentials.py`, which is the same module the backend
# itself reads its configuration from — so it cannot answer "which
# keychain" differently from the backend that is going to open it. Note
# what is *not* asked for: no account values. Only `account_env`, the
# name of the variable each account lives in.
#
# Also not asked: which binary the backend reads its secrets through today.
# `--widen` used to need that, to write a second name into each list beside
# its own. It does not any more — macOS appends this reader itself, so the
# script has no business knowing which other readers a list may carry.
_deployment() {
    # `.env` is already in this process's environment — `_load_project_env`
    # sourced it with `set -a` — so nothing needs sourcing again here, and
    # nothing should: this used to run it a second time in this subshell
    # without the `_set_paths` that undoes a `PATH=` line, which left a
    # `.env` able to break the one query it was answering.
    #
    # The child gets an empty environment plus what these two answers are
    # computed from, rather than every variable in `.env`. Handing an
    # interpreter whatever `PYTHON*`, `LD_*` and `DYLD_*` lines a file
    # happens to carry is not the same as asking it a question.
    local passthrough=()
    if [ -n "${PDT_KEYCHAIN_PATH-}" ]; then
        passthrough[${#passthrough[@]}]="PDT_KEYCHAIN_PATH=$PDT_KEYCHAIN_PATH"
    fi
    if [ -n "${PDT_DISABLE_KEYCHAIN_SECRETS-}" ]; then
        passthrough[${#passthrough[@]}]="PDT_DISABLE_KEYCHAIN_SECRETS=$PDT_DISABLE_KEYCHAIN_SECRETS"
    fi

    (
        env -i "PATH=$_TRUSTED_PATH" "HOME=${HOME-}" "PYTHONPATH=$REPO" \
            PYTHONNOUSERSITE=1 \
            ${passthrough[@]+"${passthrough[@]}"} \
            "$PY" - <<'PY'
from backend import credentials as c
print("keychain\t" + c._keychain_file())
print("keychain_disabled\t" + ("yes" if c.keychain_disabled() else "no"))
for spec in c.SECRET_SPECS.values():
    print("account_env\t" + spec.account_env_key)
PY
    )
}

_load_deployment() {
    local raw k v

    # Not a pipeline: the exit status has to survive, so that a broken
    # Python is a clear error rather than an empty configuration that
    # reads as "no secrets configured".
    raw="$(_deployment)" || die \
"could not read this project's keychain configuration.
  Expected 'PYTHONPATH=$REPO $PY -c import backend.credentials' to work.
  If the project's virtualenv is missing, create it, or point PATH at a
  python3 that can import the project."

    DEPLOY_KEYCHAIN=""
    DEPLOY_KEYCHAIN_DISABLED=""
    ACCOUNT_ENVS=()

    while IFS=$'\t' read -r k v; do
        case "$k" in
            keychain)           DEPLOY_KEYCHAIN="$v" ;;
            keychain_disabled)  DEPLOY_KEYCHAIN_DISABLED="$v" ;;
            account_env)        ACCOUNT_ENVS[${#ACCOUNT_ENVS[@]}]="$v" ;;
        esac
    done <<< "$raw"

    [ -n "$DEPLOY_KEYCHAIN" ] || die \
        "the project did not report a keychain to adopt into"
}

#: Refuse to file anything into a keychain the backend has been told not to
#: read. Both commands would otherwise succeed, and an operator would walk
#: away believing they had done something.
_require_keychain_enabled() {
    [ "$DEPLOY_KEYCHAIN_DISABLED" = yes ] || return 0
    die "this project's backend is not reading the keychain — nothing built
  or filed in it would ever be used. backend/credentials.py reports it as
  disabled, which is what an unset PDT_DISABLE_KEYCHAIN_SECRETS means: only
  0 or false enable it.

  So either turn the keychain on (PDT_DISABLE_KEYCHAIN_SECRETS=0 in
    $ENV_FILE
  and restart the backend) or leave this script alone. Nothing was created,
  imported, or written."
}

# Canonical form of a path, so that two spellings of the same file
# compare equal. realpath resolves symlinks in the last component too,
# which is the case that matters: `PDT_SECRET_READER_PATH` reaches a
# .env as free text — typed by somebody, or written by `_record_reader_path`
# — and nothing stops either from spelling it with a symlink or a
# trailing slash.
_resolve() {
    realpath "$1" 2>/dev/null || printf '%s' "$1"
}

#: What one `PDT_SECRET_READER_PATH` line from `.env` assigns. The value
#: is read the way the shell that sources that file will read it, so that
#: a line written by hand and the same line written here are recognised as
#: the same answer: whitespace around the `=` goes, one pair of matching
#: quotes goes, and an unquoted trailing `# …` goes. A `#` that is *part
#: of the word* — `/opt/a#b` — is not a comment and is kept, because that
#: is exactly where the shell would keep it too.
_env_line_value() {
    local line="$1" value rest prefix

    value="${line#*=}"
    # A `#` at the head of the value, or one preceded by whitespace, is
    # where the shell would start a comment.
    rest="${value#*#}"
    if [ "$rest" != "$value" ]; then
        prefix="${value%"$rest"}"
        case "$prefix" in
            *[![:space:]]) value="${prefix%#}" ;;
            *)               value="" ;;
        esac
    fi
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in
        \"*\") value="${value#\"}"; value="${value%\"}" ;;
        \'*\') value="${value#\'}"; value="${value%\'}" ;;
    esac
    printf '%s\n' "$value"
}

#: What the project's `.env` currently says the reader is, on one line,
#: and nothing at all when there is no such assignment. One line, because
#: one line is what the backend gets — the last assignment wins when the
#: file is sourced, but which of several is last is the operator's
#: business, not this function's.
_env_reader_path() {
    local line
    [ -f "$ENV_FILE" ] || return 0

    line="$(grep -E "$READER_ASSIGN" "$ENV_FILE" | head -n 1 || true)"
    [ -n "$line" ] || return 0

    _env_line_value "$line"
}

#: Does every assignment in that file already name this reader? "Every",
#: not "the first one": a file carrying two of them and only the first
#: being right is a file whose answer depends on which line the reader
#: happens to read, and rewriting all of them is the only way to end up
#: with a single answer. A soft link or a trailing slash spelling of the
#: same file counts as the same answer — see `_resolve`.
_all_readers_are_this_one() {
    local line found=no

    [ -f "$ENV_FILE" ] || return 1
    while IFS= read -r line; do
        if ! _same_reader "$(_env_line_value "$line")"; then
            return 1
        fi
        found=yes
    done <<< "$(grep -E "$READER_ASSIGN" "$ENV_FILE" || true)"
    [ "$found" = yes ]
}

_same_reader() {
    if [ -z "$1" ]; then return 1; fi
    if [ "$1" = "$HELPER" ]; then return 0; fi
    [ "$(_resolve "$1")" = "$(_resolve "$HELPER")" ]
}

#: Whether the last `_record_reader_path` actually changed the file. "It
#: already said this" counts as no. `--narrow` reads it: if narrowing had
#: to write the line, then the deployment cannot have come through
#: `--widen` first, which is worth saying out loud.
RECORD_WROTE=no

#: The temp file `_rewrite_reader_path` is in the middle of building, so
#: that the EXIT trap it arms can take it away again on any failure.
RECORD_TMP=""

_record_drop_tmp() {
    if [ -n "$RECORD_TMP" ] && [ -e "$RECORD_TMP" ]; then
        rm -f "$RECORD_TMP"
    fi
    RECORD_TMP=""
}

#: Write the reader path into the project's own `.env`, so the operator
#: never has to copy a path out of a terminal by hand.
#:
#: It *overwrites*. An assignment naming something else is not a conflict
#: to be argued about, because it is not one: this variable can only ever
#: name one binary, and the only binary this script could have built is
#: the one it just compiled — if the old path were still in use, nobody
#: would have rebuilt. So there is no question of whose value should win,
#: and stopping to ask would be a way of declining to do the job. An
#: existing assignment is rewritten in place and every other byte of the
#: file is left exactly where it was.
#:
#: Both modes end up with `.env` naming `$HELPER`, so the modes differ
#: only in *when* that happens relative to the accounts being adopted —
#: see `cmd_adopt`, where the asymmetry is the whole point.
_record_reader_path() {
    local existing count readback assign

    RECORD_WROTE=no
    # `grep -c` prints nothing at all for a file it cannot open, and an
    # empty string is not a number to `[`. Default it first.
    count=0
    if [ -f "$ENV_FILE" ]; then
        count="$(grep -cE "$READER_ASSIGN" "$ENV_FILE" 2>/dev/null || true)"
    fi
    existing="$(_env_reader_path)"

    # Already this reader — a soft link or a trailing slash spelling of the
    # same file counts as the same answer, see `_resolve`. Nothing to
    # write, and saying so beats a rewrite that would change nothing.
    if [ "$count" -gt 0 ] && _all_readers_are_this_one; then
        echo
        echo "$ENV_FILE already records PDT_SECRET_READER_PATH=$existing"
        echo "    (leaving it exactly as it is)"
        return 0
    fi

    # `$HELPER` is `$BUILD/pdt-secret-reader`, and `$BUILD` came out of a
    # `cd && pwd`, so today none of these can be in it. But `.env` is
    # arbitrary shell that gets *sourced*, and a value carrying a quote,
    # a backslash, a backtick or a `$` would come back out of it as
    # something other than the path written here — a silently wrong
    # answer in the one line this step exists to get right. Refuse to
    # write a line we cannot promise is read back unchanged.
    case "$HELPER" in
        *[[:space:]]*|*'#'*|*'\'*|*'"'*|*"'"*|*'`'*|*'$'*)
            die "the reader's path cannot be recorded in $ENV_FILE as written:
    $HELPER
  setup.sh writes the value unquoted, and this one contains a character
  (whitespace, #, a quote, a backslash, a backtick or $) that the shell
  sourcing that file would interpret. Nothing was written." ;;
    esac

    assign="PDT_SECRET_READER_PATH=$HELPER"

    # --- an assignment is already there: rewrite it in place -----------
    if [ "$count" -gt 0 ]; then
        _rewrite_reader_path "$assign" "$existing"
        RECORD_WROTE=yes
    else
        # --- nothing there yet: append a comment and the assignment -----
        say "recording the reader path in $ENV_FILE"

        if [ ! -f "$ENV_FILE" ]; then
            : > "$ENV_FILE"
            warn "$ENV_FILE did not exist — created, holding just that line."
        fi

        # A file whose last byte is not a newline would have the comment
        # glued onto whatever the operator typed last. Command substitution
        # strips trailing newlines, so `[ -n ]` here is exactly the question
        # "is the last byte something other than a newline?".
        if [ -n "$(tail -c 1 "$ENV_FILE")" ]; then
            printf '\n' >> "$ENV_FILE"
        fi

        {
            cat <<'NOTE'
# Written by tools/pdt-secret-reader/setup.sh — the backend reads the
# keychain through this binary. Delete this line to go back to
# /usr/bin/security; if a --narrow had dropped that from the items, the
# next read asks, and 'Always Allow' is what puts it back.
NOTE
            printf '%s\n' "$assign"
        } >> "$ENV_FILE"

        RECORD_WROTE=yes
    fi

    # Read it back instead of trusting the write. Both `>>` and a rename
    # over a path can come back successful against a file that was not the
    # one we meant — behind a symlink, on a full disk, under a permissions
    # surprise — and not noticing leaves the backend reading through
    # /usr/bin/security while the operator walks away believing the switch
    # happened.
    readback="$(_env_reader_path)"
    if [ "$readback" != "$HELPER" ]; then
        die "wrote PDT_SECRET_READER_PATH to $ENV_FILE, but reading it
    back gives:
    ${readback:-<no PDT_SECRET_READER_PATH assignment in that file>}
  The keychain is unaffected; the switch simply did not land. Read that
  file before running '$0 adopt' again — with whichever mode you were
  asked for."
    fi

    echo "recorded PDT_SECRET_READER_PATH=$HELPER"
}

#: Replace every `PDT_SECRET_READER_PATH` assignment in `.env` with
#: `$1`, leaving every other line of the file byte for byte as it was.
#:
#: Atomic, and atomic the only way that is honest here: a write that
#: truncates the file and appends to it leaves a window in which a backend
#: starting up reads a `.env` with no reader in it — no assignment at all,
#: which the backend answers to by silently going back to
#: `/usr/bin/security`. So the new file is built beside the old one and
#: renamed over it, which is a single step the reader either sees the old
#: file or the new one. Same directory on purpose: `mv` is only atomic
#: within a filesystem, and `/tmp` is very much not one of them.
#:
#: Permission bits are carried across explicitly. `.env` may hold
#: plaintext fallback secrets, so 600 on it is a real boundary, and
#: creating the temp file under the umask and then renaming it over the
#: original would quietly widen 600 to 644 — turning a file that was
#: deliberately private into one every local account can read. macOS
#: `chmod` has no `--reference`, hence the `stat`.
#:
#: Every failure path takes the temp file with it: the trap is armed
#: before the file is created and disarmed only once the rename is done.
_rewrite_reader_path() {
    local assign="$1" previous="$2" mode repl

    [ -f "$ENV_FILE" ] || die "no $ENV_FILE to rewrite"
    if [ ! -w "$ENV_FILE" ]; then
        die "$ENV_FILE is not writable — refusing to touch it.
  Nothing was changed, and the keychain is unaffected."
    fi

    # sed's replacement side is not free text: `&` stands for the whole
    # match and the delimiter ends the command. Neither can occur in a
    # path this script builds — but escaping them costs nothing and
    # turns "cannot happen" into "does not matter".
    repl="$assign"
    repl="${repl//&/\\&}"
    repl="${repl//|/\\|}"

    mode="$(stat -f '%Lp' "$ENV_FILE")"
    if [ -z "$mode" ]; then
        die "cannot read the permission bits of $ENV_FILE.
  Nothing was changed. Without them the replacement file could only be
  created fresh, which is exactly the quiet 600 -> 644 this avoids."
    fi

    # `mktemp`, not a fixed name beside `.env`. A predictable path in the
    # one directory that holds plaintext fallback secrets is a place a
    # second run — or another process — can collide with, and `sed … >
    # "$RECORD_TMP"` over a name somebody else created follows a symlink and
    # writes the whole of `.env` wherever that symlink points.
    #
    # The mode needs no help from the umask: `mktemp` creates `O_EXCL` at
    # 0600 itself, and a umask can only take permissions away. Measured all
    # four ways — bare `mktemp` under umask 022 and under 000, and
    # `umask 077 && mktemp` under each — and the file is `-rw-------` every
    # time.
    RECORD_TMP="$(mktemp "$ENV_FILE.setup-tmp.XXXXXX")" \
        || die "could not create a temporary file beside $ENV_FILE. Nothing was changed."
    trap '_record_drop_tmp' EXIT

    # The same pattern `grep -E` finds them with, plus the rest of the
    # line, so what is replaced is exactly what was counted. Every
    # match becomes the same line: a `.env` carrying two assignments
    # does not have an answer, it has an argument between two answers,
    # and the backend's answer would be whichever it reads last.
    sed -E "s|$READER_ASSIGN.*$|$repl|" "$ENV_FILE" > "$RECORD_TMP"

    chmod "$mode" "$RECORD_TMP"
    mv -f "$RECORD_TMP" "$ENV_FILE"

    RECORD_TMP=""
    trap - EXIT

    say "rewrote PDT_SECRET_READER_PATH in $ENV_FILE"
    if [ -n "$previous" ]; then
        echo "  was: $previous"
    fi
    echo "  now: $assign"
}

# ---------------------------------------------------------------------------
# adopt
# ---------------------------------------------------------------------------

# Presence is not the property that matters; signature is.
#
# What ends up in the ACL is the designated requirement `codesign`
# derived from this binary, so a binary that is *there* but not signed
# with this project's identity has nothing stable to pin. A stray
# `clang` invocation, or a linker-signed adhoc stub, is executable and
# would be adopted happily — and the ACL it produced would stop matching
# the moment anything re-signed the file. That failure lands weeks later,
# as a read that suddenly prompts, with nothing on screen connecting it
# to this command.
_require_signed_reader() {
    local dr
    dr="$(codesign -d -r- "$HELPER" 2>&1 | grep -i designated || true)"
    case "$dr" in
        *"identifier \"$BUNDLE_ID\""*certificate*) return 0 ;;
    esac
    die \
"nothing at
  $HELPER
  is signed with this project's identity, so there is no stable
  requirement to record in the access control list:
    ${dr:-<codesign reported no designated requirement>}
Run '$0 build' — it compiles, and signs what it compiled. Then run
  '$0 adopt --widen'."
}

#: A wrong or missing mode is answered with both modes and when each one
#: belongs — not just "usage: --widen|--narrow". The flag is one word; the
#: order behind it is the part an operator has to get right, and an error
#: message is the one place they are certainly still reading.
_adopt_usage_die() {
    die "$1
  There are two modes, and nothing was changed:

  --widen    Have the system add this reader to every secret this
             deployment declares, keeping whatever is already trusted on
             each item — you answer one dialog per item with 'Always
             Allow'. Start here. Safe to repeat: it only ever adds this
             reader, so running it can never drop anything.

  --narrow   Overwrite each item's access control list with this reader
             alone, dropping every other application on it. Only after
             --widen has run, the backend has been restarted on the new
             reader, and the notifier has actually come up
             ('FeishuClient initialized').

  Both modes point this file at the reader, overwriting what is there:
    $ENV_FILE

  Widening and then narrowing leaves no window in which the backend
  cannot read its own secrets. Narrowing first leaves exactly one.

  Full usage: '$0' with no arguments."
}

cmd_adopt() {
    # --- the mode, before anything else -------------------------------
    # Checked first and on its own, because a missing or misspelled mode
    # is a usage mistake and there is nothing to be learned from it by
    # probing the machine. There is no default: the two modes differ in
    # whether the old reader keeps working, and a script that picked one
    # on the caller's behalf would be making a security decision that
    # reads like a convenience.
    local mode="${1:-}"
    if [ "$#" -ne 1 ]; then
        _adopt_usage_die "adopt takes exactly one mode — got $#: ${*:-<none>}"
    fi
    case "$mode" in
        --widen|--narrow) ;;
        *) _adopt_usage_die "adopt does not take '$mode'." ;;
    esac

    require_macos
    [ -x "$HELPER" ] || die \
"no reader to adopt yet — there is nothing built at
  $HELPER
Run '$0 build' first (it is idempotent), then run '$0 adopt --widen'."

    _require_signed_reader
    _require_project
    _load_project_env
    _load_deployment
    _require_keychain_enabled

    local kc="$DEPLOY_KEYCHAIN"
    local widening=no

    # --- refuse the wrong keychain -------------------------------------
    _require_project_keychain "$kc"

    # --- which of the two are we doing? --------------------------------
    if [ "$mode" = "--widen" ]; then
        widening=yes
        say "widening — letting the system add this reader to each item"
        cat <<'WIDEN'

  macOS is about to ask about this reader, once per item. That is the
  whole mechanism: the system puts an application on an item's access
  control list when that application reads the item and you answer the
  dialog. Nothing here composes a list of its own, so nothing already on
  those lists can be evicted by a wrong guess about who else is on them.

  Click "Always Allow". Not "Allow".

  "Allow" covers this run only. This process exits, the grant exits with
  it, and the backend's next start finds what it found this morning.
WIDEN
    else
        say "narrowing — the items' lists become this reader alone"
        echo "Every other application named on them is dropped."
    fi

    # --- one adopt per declared account --------------------------------
    if [ "${#ACCOUNT_ENVS[@]}" -eq 0 ]; then
        die "this project declares no keychain accounts — nothing to adopt"
    fi

    # --- where the `.env` write goes ------------------------------------
    # The two modes put it on opposite sides of the account loop, and the
    # asymmetry is the whole point of it: it decides what state a failure
    # leaves the deployment in. The rule both orders obey is that any
    # step that fails must leave a backend that can still read its own
    # secrets.
    #
    #   --widen writes AFTER the loop. The reader is only trustworthy once
    #   the system has put it on the items, so a `.env` pointing at it
    #   before that is a backend aimed at a binary nothing will answer
    #   to. Written after, the worst a failure can do is leave the old
    #   reader pointed at — and that reader is untouched, because a widen
    #   read only ever adds to a list. A failure to write costs nothing
    #   at all here, for that reason.
    #
    #   --narrow writes BEFORE the loop, because narrowing is the step
    #   that removes the old reader. If it ran first and the write then
    #   failed, `.env` would still name the old reader, which the loop
    #   has just taken out of the ACLs: a backend that cannot read its
    #   own secrets, reporting an error that blames the keychain. So the
    #   write goes first, when the old reader is still trusted — and in
    #   the ordinary sequence it points at a reader `--widen` already put
    #   in the ACL, so there is no window at all.
    if [ "$widening" = no ]; then
        _record_reader_path

        if [ "$RECORD_WROTE" = yes ]; then
            warn "$ENV_FILE did not already name this reader — so this deployment"
            warn "has not been through '$0 adopt --widen'. The ordinary sequence is"
            warn "--widen, restart, confirm the notifier, and only then --narrow."
            warn "Carrying on, because a deliberate operator may well want this:"
            warn "but until a --widen has run and been restarted, any restart of"
            warn "the backend in the meantime reads secrets nothing will answer to."
        fi
    fi

    say "adopting into $kc"
    local name acct rc adopted=0 missing=0
    local verb=adopted
    if [ "$widening" = yes ]; then verb=trusted; fi

    for name in ${ACCOUNT_ENVS[@]+"${ACCOUNT_ENVS[@]}"}; do
        # Indirect expansion, on purpose. The value goes from the
        # environment to this process to the reader's argv and stops
        # there; it is not logged, not printed, and not stored. Reading
        # it through a variable whose *name* the project chose is the
        # whole reason no account id appears anywhere in this file.
        acct="${!name-}"
        if [ -z "$acct" ]; then
            warn "$name is not set — skipping it"
            echo "    (set it in $REPO/.env; a deployment without it is not"
            echo "     using that provider)" >&2
            continue
        fi

        # Two different tools, on purpose. Widening is a *read*: macOS
        # files this reader on the item's list itself, at the moment it
        # catches this binary reading the item, so all this has to do is
        # ask — and answering with "Always Allow" is what puts it there.
        # The code this replaced passed `--adopt --also <the project's
        # current reader>`, which composed a list of its own and so
        # evicted anything it did not know about: a keyring tool the
        # project never reads through would have been dropped, and
        # `--widen` would have narrowed instead.
        #
        # stdout goes to /dev/null on the widening read: the reader
        # refuses to write a secret to a terminal, and what comes back is
        # not this script's to print. stderr is left alone — the dialog
        # and any complaint from the reader go there.
        rc=0
        if [ "$widening" = yes ]; then
            "$HELPER" find-generic-password -a "$acct" -w "$kc" >/dev/null || rc=$?
        else
            "$HELPER" --adopt -a "$acct" "$kc" || rc=$?
        fi

        if [ "$rc" -eq 0 ]; then
            adopted=$((adopted + 1))
            continue
        fi

        # 5 is "no such item", from either tool: the expected outcome for
        # a secret nobody has put in this keychain yet, which must not
        # take the other accounts down with it.
        if [ "$rc" -eq 5 ]; then
            if [ "$widening" = yes ]; then
                warn "no item to read under $name in this keychain — skipping"
            else
                warn "no item filed under $name in this keychain yet — skipping"
            fi
            echo "    (archive that secret into the keychain and run this" >&2
            echo "     again; nothing about it is broken)" >&2
            missing=$((missing + 1))
            continue
        fi

        if [ "$widening" = yes ]; then
            die "reading $name failed (reader exited $rc).
  Nothing after it was tried, and every item already read is as it was.
  macOS can be set to refuse an application without asking it first, and
  this looks like that. Add the reader by hand instead — Keychain
  Access, select the item, 'Access Control', '+', and this binary:
    $HELPER
  then run this again."
        fi
        die "adopting $name failed (reader exited $rc).
  Nothing after it was tried, and the items already visited are
  unchanged. Fix the above and re-run — adopting twice is harmless."
    done

    say "$adopted $verb, $missing skipped"

    # --- what to do next ------------------------------------------------
    if [ "$widening" = yes ]; then
        # The other half of the asymmetry above: on this branch the write
        # comes after the loop.
        _record_reader_path

        cat <<DONE

PDT_SECRET_READER_PATH in
    $ENV_FILE

now names this reader, so there is nothing left to copy in by hand.

Restart the backend, and wait for the notifier to come up —
'FeishuClient initialized' in the log. Once it has, run

    $0 adopt --narrow

which leaves this reader as the only application the items answer to.

Until that second run, everything trusted before this run is still
trusted too, and nothing is broken: the backend reads through whichever
reader it read this morning.

To widen it back afterwards: $0 adopt --widen, which asks the system to
add this reader beside whatever is there. This reader stays trusted
either way, on purpose — it is the one program that can rewrite a list,
so removing it would remove the recovery path too.
DONE
    else
        cat <<DONE

Every one of those access control lists now names this reader alone —
whatever else was on them is gone. $ENV_FILE names this reader too.

Restart the backend and confirm the notifier comes up —
'FeishuClient initialized' in the log.

To widen it back: $0 adopt --widen. This reader stays trusted on
purpose — it is the one program that can rewrite the list, so removing
it would remove the recovery path too.
DONE
    fi
}

# ---------------------------------------------------------------------------

case "${1:-}" in
    build) shift; cmd_build "$@" ;;
    adopt) shift; cmd_adopt "$@" ;;
    *)
        awk 'NR > 2 && /^#/ { sub(/^# ?/, "", $0); print; next }
             NR > 2 { exit }' "$0"
        exit 1
        ;;
esac
