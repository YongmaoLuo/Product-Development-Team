# pdt-secret-reader

A tiny signed helper that reads one notification secret out of the
project's keychain — and the setup script that re-files those secrets so
**only this binary** is trusted to read them.

Written 2026-10-08, after asking why a keychain read never prompts.

---

## The diagnosis

The backend reads its secrets by shelling out to `/usr/bin/security`.
That works, and it is also why the read is *silent for everybody*.
`man security`, under `add-generic-password`:

> By default, the application which creates an item is trusted to access
> its data without warning. You can remove this default access by
> explicitly specifying an empty app pathname: `-T ""`.

So an item created by `security` has **`/usr/bin/security` in its access
control list** — an Apple-signed binary that every process on the machine
may execute. Measured on this machine:

```
/usr/bin/security
  Identifier  = com.apple.security
  Authority   = macOS Software Signing → Apple Code Signing CA → Apple Root CA
  designated  => identifier "com.apple.security" and anchor apple
```

A stable, Apple-anchored requirement, already granted. That is the whole
explanation for "no prompt on restart": **nothing new was being asked.**
Any process running as this user can read these secrets by running one
command, and the ACL will let it, forever, silently.

Related flags, for completeness:

| flag | meaning |
|---|---|
| *(default)* | the creating application — here `/usr/bin/security` — is trusted |
| `-T <appPath>` | trusted applications (repeatable). **Replaces** the default |
| `-T ""` | no application trusted: every read prompts |
| `-A` | *any* application, no warning. Insecure; not recommended by Apple |

---

## The paid-account claim, corrected

A previous investigation concluded that restricting the ACL to a specific
application "needs a paid Developer account and code signing of the
binary". That is **half right**, and the half that is wrong matters.

**Wrong:** a paid account is not required. Apple-signed system binaries
are not the only things a keychain ACL can name. A locally generated
**self-signed certificate** produces a perfectly good code identity, and
the ACL only needs the resulting requirement to be **stable**. Gatekeeper
is the component that demands a Developer ID, and Gatekeeper is not on
this path.

**Right, and this is the real blocker:** you do need a **stable code
signature**, and the binaries in play today do not have one.

```
/opt/homebrew/.../python3.11          ← what a Python-side reader would be
  Identifier  = python3-55554944510b3e1730163dc0bc990b869a31d2a8
  Signature   = adhoc
  designated  => (nothing — an ad-hoc binary has no DR at all)
```

`codesign -d -r-` prints no designated requirement for an ad-hoc binary,
and its identifier embeds a hash of the binary. There is nothing for an
ACL to pin that survives a rebuild. `brew upgrade python` would silently
break it.

### Which requirement goes in the ACL — and a correction

The first cut of this directory forced an **identifier-only** requirement:

```
designated => identifier "com.pdt.secret-reader"
```

That was wrong, and it is worth spelling out because the reasoning is
seductive. Identifier-only is maximally *stable* — it survives any
rebuild — but a bare `identifier "X"` requirement is satisfied by **any
binary that claims that identifier**, including one somebody else
compiles. It is close to `-A` in strength, and it is precisely the
weakness Apple's own note warns about: *"another app could gain access by
mimicking this app."* The OpenAgentd commit that suggested it was solving
the opposite problem — a *self-updating app* whose ACL kept breaking —
not restricting who may read.

The requirement that is both stable **and** narrow pins the leaf
certificate:

```
designated => identifier "com.pdt.secret-reader"
            and certificate leaf = H"<sha-1 of the DER certificate>"
```

* The certificate does not change when the *source* does, so this rides
  through every rebuild.
* Nobody can produce a signature from that certificate without its
  private key, so it is not spoofable.

`setup.sh build` does not pass `--requirements` on the first attempt: it
signs, reads back whatever DR `codesign` derived, and **judges it**. A
cdhash-pinned DR (changes on every rebuild) or an identifier-only one
(spoofable) is rejected and re-signed with the certificate pinned instead.
It then **asserts** the result: the DR it settled on has to name
`identifier "com.pdt.secret-reader"` *and* `certificate leaf`, or the build
stops without replacing anything. The script prints the DR so the value
that ends up in the ACL is never a mystery.

That assertion is not ceremony. `codesign` signs happily with an
untrusted certificate — measured — so "exit 0" and "the right requirement"
are two different claims. If codesign ever answered a self-signed
certificate with `anchor trusted` or `certificate root` instead, every ACL
written from that shape would quietly stop matching, and the place to find
out is here rather than in production.

### No trust settings are involved, and none are needed

The certificate is self-signed, so it is **not trusted** by anything on
this machine. `security find-identity -v -p codesigning` reports
`0 valid identities found` for it, and that stays true forever. Two
things follow, and both are load-bearing:

* **Never judge "did the import work?" with `-v`.** The identity is there;
  it is just not trusted, and `-v` filters trusted-ness out. `setup.sh`
  asks `find-identity` *without* `-v`. Asking with it was the reason
  `build` had never once succeeded: the import worked, the check said no,
  the script fell through to the p12 fallback, and everything failed.
* **Nothing is added to the trust store.** There is no
  `security add-trusted-cert` anywhere in this directory, and there should
  not be. It would not fix a single problem — the DR compares the *leaf
  certificate* by hash, not trust — and it would leave a global trust
  record that Keychain Access cannot show you: trust is stored against a
  certificate hash in a "domain", independent of whether the certificate
  is still filed anywhere. Delete the certificate and the record stays.

Measured on this machine, in a keychain that holds only this project. The
certificate's own SHA-1 is elided: it is a property of the key pair one
`setup.sh build` happened to generate rather than of the procedure, so a
reader running the same commands would find a different value in the three
places it appears below. `setup.sh build` prints the one your build
produced, and `openssl x509 -in build/identity/cert.pem -outform der |
openssl sha1` reproduces it.

```
$ security find-identity -p codesigning kc        # no -v
  1) <your certificate's SHA-1, uppercase> "PDT Local Secret Reader" (CSSMERR_TP_NOT_TRUSTED)
     1 identities found

$ security find-identity -v -p codesigning kc     # -v
     0 valid identities found

$ codesign --force --sign "PDT Local Secret Reader" …  →  exit 0
$ codesign -d -r- probe
  designated => identifier "com.pdt.secret-reader"
            and certificate leaf = H"<the same hash, lowercase>"
$ openssl x509 -in cert.pem -outform der | openssl sha1
  <the same hash>                                   # the same hash
```

`certificate leaf`, not `anchor trusted`, not `certificate root`. So the
ACL side is a plain hash comparison and no trust configuration enters it.

**One honest unknown.** All of the above shows codesign *will use* an
untrusted certificate and *does* produce the right DR shape. It does
**not** show that securityd will honour that certificate when it evaluates
a keychain item's ACL. That can only be answered end to end — a throwaway
keychain, one item, one `adopt --narrow`, then a read with the signed
reader — which an agent session cannot do. `setup.sh` does not paper over
it and neither does this file.

### How unstable is it, in practice

| event | effect on the ACL |
|---|---|
| edit the C, rebuild, re-sign with the same certificate | **none** — same DR |
| upgrade clang / the SDK / macOS | **none** |
| upgrade Python, Homebrew, anything else | **none** — Python is not in the ACL any more |
| regenerate the signing certificate | DR changes, items must be re-filed (`./setup.sh adopt --widen`, then `--narrow`) |

Only the last row costs anything, and it is a deliberate act: `build`
refuses to overwrite an existing signing identity, so it cannot happen by
accident. When you *do* mean it — the key pair was exposed, or is being
rotated — say so with `./setup.sh build --new-identity`. That moves the
old `cert.pem`, `key.pem`, `identity.pem` and `identity.p12` aside as
`*.abandoned` rather than deleting them, refuses to go on if the identity
is still in the keychain (it prints the one `security delete-identity`
command that removes it), and otherwise issues a different certificate.
From that point every list pinned to the old one stops matching until you
re-file it. A certificate that is lost entirely is recoverable the same
way: run `./setup.sh adopt --narrow` again, and the items are re-filed
against whatever reader is built now.

### The two routes, which are easy to conflate


| | Route A — legacy ACL *(this helper)* | Route B — data-protection keychain |
|---|---|---|
| API | `SecKeychain*` (deprecated 10.10) | `SecItem*` + `kSecAttrAccessControl` |
| Boundary | which applications may read | Secure Enclave, per-access rules |
| Sharing across apps | ACL's trusted-application list | `keychain-access-groups` entitlement |
| Signing needed | self-signed cert — **free** | provisioning profile / team id |
| Cost | none | **paid Apple Developer account** |

Route B is where "you need to pay Apple" is true, and it is very likely
what the earlier investigation ran into. It is also the stronger design —
access groups are enforced by the OS rather than by an ACL that a
same-user process can rewrite. But it is not reachable without the
entitlement, so this directory implements **Route A**.

---

## What is measured, and what is not

| claim | status |
|---|---|
| `/usr/bin/security`'s DR is `identifier "com.apple.security" and anchor apple` | **measured**, `codesign -dvvv` on this machine |
| Homebrew's `python3.11` is ad-hoc with **no** DR | **measured**, same |
| `-T`/`-A`/`-T ""` semantics | **measured**, `man security` |
| The items' ACLs currently name `/usr/bin/security` | **inferred** from the man page's documented default plus the fact that the reads have never prompted. Not read out of the ACL — `dump-keychain` needs the keychain password |
| A self-signed cert is *not* valid per `find-identity -v`, but is listed without it | **measured**, real `security` on this machine |
| `codesign` signs with that untrusted cert, exit 0, DR `… and certificate leaf = H"…"` | **measured**, real `codesign`; the DR hash equals `openssl … \| openssl sha1` of `cert.pem` |
| `codesign --keychain <file>` cannot find an identity that `find-identity` lists; putting the file in the search list makes the identical command succeed | **measured**, real `codesign` + real `security`. Note this is `man codesign`'s own disambiguation flag, and it does not work here — see the runbook, "The contradiction in that, stated plainly" |
| `codesign --requirements` without a leading `=` treats the whole string as a *file name* (`invalid requirement specification`, exit 1); with `=`, exit 0 and the requirement is stored | **measured**, real `codesign` |
| `codesign -d -r-` prefixes a *derived* DR with `# ` on an ad-hoc signature (`# designated => cdhash H"…"`); a certificate-signed derived DR has no `#`, and neither does an explicit `--requirements` | **measured**, real `codesign` |
| Removing `-T` from `security import` makes codesign *ask*, not refuse | **measured**: dialog → Allow → exit 0 |
| `/usr/bin/openssl` (LibreSSL) leaves a 0-byte `-out` file when an export fails; Homebrew OpenSSL 3 leaves none | **measured**, both binaries on this machine |
| That securityd honours an **untrusted** leaf certificate when evaluating an item's ACL | **UNKNOWN.** Not testable from an agent session; needs a real end-to-end adopt + read. `setup.sh` does not assume it either way |
| "Always Allow" **appends** to the list rather than replacing it | **not verified**. No Apple document says either; strongly inferred from behaviour. See "How `--widen` widens" for the one-command check |
| The helper's own code path works | **measured**: builds, runs, returns distinct exit codes for usage / missing item / refused |

The one thing nobody could test from an agent session: the agent's tool
sandbox blocks **`/usr/bin/security` the binary** (exit 127,
`operation not permitted`) but *not* the Security framework. Querying a
non-existent account from inside the session returned `errSecItemNotFound`
(-25300) rather than a permission error — so the framework is reachable
and the ACL is decided **per item**. No real account was queried on
purpose; see "What this does not protect against".

---

## Runbook

From a **normal Terminal**, not from inside an agent session. It touches
the keychain and needs to put a permission dialog in front of a human.

Two commands, `adopt` takes one mode, and `build` takes one flag:

```bash
cd tools/pdt-secret-reader

./setup.sh build            # compile, create the signing identity if
                            # there is none, and sign. Then adopt --
                            # see "Order"; this one is not the last.
./setup.sh build --new-identity
                            # DESTRUCTIVE: throw the signing identity
                            # away and issue a different one. Old files
                            # are kept as *.abandoned; the copy inside
                            # the keychain you remove yourself, with the
                            # command it prints. Every ACL pinned to the
                            # old certificate stops matching until you
                            # re-file it below.
./setup.sh adopt --widen    # have the system add this reader to every
                            # secret this deployment declares. Start here.
./setup.sh adopt --narrow   # overwrite those lists with this reader
                            # alone. Only after --widen, a restart, and
                            # a notifier up.
```

Both commands ask the project for its keychain first, and both refuse if
the project reports the keychain **disabled** — that is, unless
`PDT_DISABLE_KEYCHAIN_SECRETS` is `0` or `false`, which out of the box it
is not. Filing secrets into a keychain the backend never opens would
succeed and change nothing, which is the worst possible outcome: it looks
done. Turn the switch on and restart the backend, or leave this alone.

**`build` is not the last step, and it is not supposed to write
`PDT_SECRET_READER_PATH`.** It compiles and signs; pointing the backend at
the result is `adopt`'s job, and on no branch of `build` does the reader
get into the keychain items' access control lists first — so a `.env`
naming it would aim the backend at a binary nothing will answer to, and
the backend has no window in which to ask. Looking for that line after a
successful `build` and not finding it is the expected state, not a bug.
`build` now ends by telling you which of the three situations you are in
and what to run next; the sequence itself is under **Order**.

### Two things `build` does to your machine, both visible

**1. A key-access dialog, while signing.** Not the login keychain: `build`
signs against the project's own keychain, and macOS asks you for that
keychain's password itself, to set the partition list on the private key —
**on every `build`**, not only the run that imports one, including the runs
that reuse an identity and import nothing. That is not an oversight and
there is no way around it: the partition list is a property of the key in
the keychain, so it has to be established before signing whatever route
`build` took, and establishing it needs that password. `setup.sh` never
reads that password and never passes it to anything — a password this
script prompted for and then handed to `security` with `-k` would sit in
the process table where every other process on the machine can read it with
`ps`, which is the one thing a script built around a keychain ACL must not
do. You are typing it into a macOS dialog, not into this script. It may
also put up one key-access dialog when `codesign` reaches for the private
key.

Without that partition list `codesign` cannot use the key at all, and it
says so as `PDT Local Secret Reader: no identity found` — no dialog, and
nothing in that sentence to say the key is sitting right there in the
keychain. `build` therefore stops with its own message if
`set-key-partition-list` is refused, instead of letting signing fail later
on a line that reads like a missing certificate.

* **Answer "Allow", and type the password.** That grant lasts this run,
  which is the entire point.
* **Do not answer "Always Allow".** That writes `/usr/bin/codesign` into
  this private key's access list, permanently — putting back exactly what
  leaving `-T` off `security import` was for. The skeleton key comes off
  `/usr/bin/security` only to be handed to `/usr/bin/codesign`, and
  *that* binary is `-rwxr-xr-x` and runnable by anything here.

**2. It may append the keychain to your search list.** `codesign` only
finds identities in the user's keychain search list. `--keychain <file>`
does not help — measured, it answers `no identity found` even when the
identity is valid, the keychain is unlocked and the path is right. So if
`$KC` is not already in `security list-keychains -d user`, `build` adds
it, and prints:

```
!!  added '/path/to/runtime-secrets.keychain-db' to your keychain search list; your existing entries are untouched — the list was read back after the write and compared, entry for entry and in order.
  it was:
    "<every entry that was already on the list, one per line, quoted>"
  undo: security list-keychains -d user -s
    "<the same entries, one per line, quoted>"
```

**The contradiction in that, stated plainly.** `man codesign` documents
`--keychain` as the way to *remove* identity ambiguity, so a flag that
exists to be used is here being left off on the strength of a measurement.
What that costs: with no `--keychain`, `codesign` picks the identity out
of the user's search list by name, and if that list holds a *second*
certificate with the same common name it may pick that one instead. The
name this project signs with is `PDT Local Secret Reader`, chosen to be
unmistakable, and the identity that gets picked is then printed as the
designated requirement and pinned into every ACL — so a wrong pick is
visible in the build output rather than silent. If you ever have two
identificates by that name on one machine, that is the situation to fix
by renaming one, not by re-adding `--keychain`.

The write is guarded rather than hopeful. `security list-keychains -s`
*sets* the list to the arguments it is given (`man security`), there is
no "add one" verb, and the entries have to come back out of a dump
printed for a human — one per line, indented, quoted. So `build` parses
that dump one line at a time, puts every entry back and compares it with
the line it came from before writing a byte, and **refuses** — printing
the list verbatim and a command to run by hand — rather than write a list
it cannot reproduce exactly. The claim "your existing entries are
untouched" is made only after the new list has been read back and
compared entry for entry; if that read-back does not match, the script
says so instead.

It only ever appends, and it never undoes it, because the two directions
cost different amounts: one extra entry is visible in
`security list-keychains` and harmless, whereas "restoring" means
overwriting the whole list with a *parsed* copy of it — and
`security list-keychains` output spans lines and carries quotes, so it
does not parse back losslessly. Parse it once wrong and your production
keychain is off the list and the backend cannot read its own secrets. Not
worth the bet for the sake of one fewer entry. On the reference layout
(`~/Library/Keychains/runtime-secrets.keychain-db`) this step normally
does nothing at all.

Two things about that step are **not** verified here, because they cannot
be from an agent session: the exact byte layout of a real
`security list-keychains -d user` on a machine whose home directory
contains a space (this is the one the parse is written for, and the one
nobody here has watched the real tool print), and whether the real
`security list-keychains -s` takes an argument carrying literal double
quotes. The parse strips one matching pair of quotes and refuses any entry
containing a quote or a backslash at all, which is the shape that fails
loudly instead of quietly.

Expect a system dialog **once per item** while `--widen` runs, and that
is the mechanism rather than a side effect: macOS files an application
onto an item's access control list when it catches that application
reading the item and you answer. Click **Always Allow**.

**Not "Allow".** Here the opposite holds. "Allow" covers that one run.
The reader process exits, the grant exits with it, and the backend's next
start finds exactly what it found this morning. That asymmetry is
deliberate: `--widen` grants are meant to be reusable, `build`'s
key-access grant is meant to be one-shot.

There is no password to type during `--widen`. It does read the item —
that is what provokes the dialog — but the secret goes straight to
`/dev/null`, and the reader refuses to write one to a terminal in any
case. `--narrow` reads nothing at all: it rewrites each access object in
place.

`build` is idempotent and safe to re-run: the identity is created once
and reused, and re-signing after a rebuild keeps the same designated
requirement, so no ACL is disturbed. Run it whenever the C changes.

`build`'s main path is a plain `security import -t agg` of the
certificate and key concatenated into one PEM sequence — no container, no
passphrase, nothing for the keychain to fail a MAC check on. If the
keychain turns that down, it retries with a p12 (the system LibreSSL's
export first, then OpenSSL 3's `-legacy` one). If the keychain turns down
**all three**, it stops and says which one said what, and then:

* if a real p12 was exported and refused, it is at
  `tools/pdt-secret-reader/build/identity/identity.p12` and you can
  **drag it into Keychain Access** — a file the GUI takes and
  `security import` will not is a known macOS roadblock, not a bad file.
  Password `pdt-local-signing`.
* if no p12 could be exported, it says exactly that and does not point you
  at one. `/usr/bin/openssl` creates its `-out` file *before* it parses,
  so a failed export used to leave a 0-byte `identity.p12` on disk that
  Keychain Access will ask you for a password to and then never open.

Either way, re-run `./setup.sh build`: the key pair is still on disk, so
it retries the import rather than minting a new certificate.

Re-running `build` never regenerates the certificate: if the pair is on
disk but not in the keychain, that is a failed import to be retried, not
a licence to mint a new identity.

### Both commands ask the project the rest

Neither one carries a copy of anything. `setup.sh` runs
`backend/credentials.py` — the same module the backend itself reads its
configuration from — and takes its answer from there. This tool ships
inside the repository, so the repository is entitled to say which
keychain it keeps its secrets in, and asking is strictly better than
carrying a copy: a second copy of the keychain path inside `setup.sh`
would be a second thing to forget to update, and it would be the wrong
one the moment anyone moved the keychain.

What is asked:

| question | asked of | by |
|---|---|---|
| which keychain | `credentials._keychain_file()` | both |
| are keychain reads switched on | `credentials.keychain_disabled()` | both |
| the **names** of the account variables | `SECRET_SPECS[*].account_env_key` | `adopt` |

**`build` is not self-contained any more, and that is deliberate.** It
asks which keychain to file its signing identity into, because that
identity belongs in the same dedicated keychain as the secrets and *not*
in the login keychain, which holds every credential this account has ever
had. The price is that the binary you are about to compile has to agree
with the project about which keychain it is compiling *for*. That is a
good trade.

Neither asks which binary the backend reads its secrets through today.
`--widen` used to need that, so it could write that reader's name into each
item's list beside its own; it does not any more, because the system does
that appending and the script has no business knowing what else a list may
carry.

The interpreter is run with an **empty environment** plus the two
variables those two answers are computed from. `.env` is arbitrary shell
and has already been sourced into the script's own process, so handing the
whole environment on would hand the interpreter whatever `PYTHON*`,
`LD_*` and `DYLD_*` lines the file happens to carry.

The accounts are the part worth being precise about. The project is asked
for the **name** of each variable that holds an account — those names are
already in this repository in plain sight. The **values** are read out of
the environment by indirect expansion and handed straight to the reader.
They never pass through this script, are never written down in it, and
never enter the repository, which is why `setup.sh` is the same file on
every machine that runs it. An account id that never gets a copy of
itself is an account id that cannot leak from here.

So the old advice — name your own items yourself and pass them on the
command line — no longer applies, and in fact runs backwards. A
hand-typed `<account>` or `<keychain>` is a value the script cannot
check, going stale silently, and ending up on a shell history line.

### The mode is a flag, because a flag is what you can undo

`adopt` has exactly two modes and no default:

| mode | what the keychain items' ACLs end up naming |
|---|---|
| `--widen` | this reader **added** to whatever each list already names — one dialog per item |
| `--narrow` | this reader alone; everything else on each list is gone |

An earlier version worked the mode out by reading the project `.env` —
unset meant widen, pointing at this reader meant narrow — and that was
wrong for two reasons. The same command doing two different things on two
different runs is a command whose effect you cannot predict. And, worse,
there was no way to ask for the permissive state on demand: once the
pointer was set, the only route back to widening was to delete a line
first, so the one operation you might reach for in a hurry was the one
that required editing a file first.

Why widen first and narrow second, and why it costs nothing: with both
readers trusted there is no interval in which the backend's next restart
comes up without a notifier. Narrowing straight to your reader leaves
exactly that interval, because the backend still reads through `security`
until you point it at your own. `--widen` is safe to repeat, so a
deployment that is unsure where it stands can run it again and be back in
the state where nothing can break.

**How `--widen` widens: it asks, and macOS answers.** The previous
version wrote the list itself — `--adopt --also <the reader
`credentials.py` reports>`, i.e. this reader *plus* `/usr/bin/security`.
That is still a replacement wearing the name of a merge: any third party
on an item's list that the project does not read through — a keyring tool
the items were archived from, say — was not in the composed list and
would have been quietly evicted. A `--widen` that narrows is worse than
no `--widen`. So the script no longer composes a list. It reads the item
once; the system sees a binary that is not on the list, puts its dialog
up, and adding the binary is the system's own append.

**Whether "Always Allow" appends or replaces is not documented.** No
Apple document states which of the two it does, and the developer forum
threads about ACLs do not settle it. The strong working assumption is
append — if it replaced, every newly authorised application would evict
the last one, and no item's trusted list could ever grow past a single
entry — but that is an inference from experience, not something measured
here. Hand-writing the append yourself does not work either: on
[thread 691160](https://developer.apple.com/forums/thread/691160) the
read-modify-write (`SecKeychainItemCopyAccess` →
`SecACLCreateWithSimpleContents` → `SecItemUpdate`) returns
`errSecSuccess` and leaves the list alone, and the thread ends with the
report being escalated to DTS.

So check it once, rather than take it on trust. After the first
`--widen`, open one item in Keychain Access and look at its **Access
Control**: `/usr/bin/security` should still be named there. If it is, the
append assumption holds. If it is not, `--widen` did the one thing it
could still do — the read succeeded, so this reader can read, which is
what the backend's next start needs — and anything else that was lost
has to be added by hand, once.

### The line in `.env` is written for you

Both modes record `PDT_SECRET_READER_PATH` in the project `.env`, and
then read the line back to confirm it landed. Neither leaves that to you,
because a mistyped path is not a cosmetic mistake: the backend would read
its secrets through a binary that is not trusted, and the error it
produces blames the keychain.

It **overwrites**. If the `.env` already assigns that variable to
something else, `setup.sh` rewrites that line in place and leaves every
other line byte for byte as it was. This is not carelessness about
somebody's deliberate configuration — it is that there is no competing
answer to protect. The variable can only name one binary, and the only
binary `setup.sh` could have built is the one it just compiled: if the
old path were still in use, nobody would have rebuilt. So overwriting is
always the right answer, and refusing would be a way of not doing the
job. The write goes through a temporary file in the same directory and a
rename, so a backend starting up mid-write reads either the old file or
the new one — never a truncated one — and the file's permission bits are
carried across, because a `.env` may hold plaintext fallback secrets and
quietly widening `600` to `644` would undo that. That temporary file comes
from `mktemp` under `umask 077`, not from a fixed name: a predictable
path in the one directory on this machine that holds plaintext secrets is
a place another run can collide with. Any failure takes it with it.

**The two modes put the write on opposite sides of the account loop**, and
that is deliberate. Any step that fails must leave a backend that can
still read its own secrets.

* `--widen` writes **after** the accounts have been read. The reader is
  only trustworthy once the system has put it on the items, so a `.env`
  pointing at it earlier would aim the backend at a binary nothing
  answers to. Written after, the worst a failure can do is leave the old
  reader in place — and a widen read only ever adds to a list, so that
  reader is still trusted. Failing to write costs nothing at all in that
  state.
* `--narrow` writes **before** them, because narrowing is the step that
  *removes* the old reader. Written after, a failure would leave `.env`
  naming the old reader that the loop had just taken out of the ACLs: a
  backend that cannot read its own secrets.

### Stuck? Widen it back

If the backend is aimed at this reader and cannot read, run
`./setup.sh adopt --widen` and answer **Always Allow** again. That is the
whole procedure — no editing, and nothing to remember.

If it is aimed at `/usr/bin/security` and a `--narrow` dropped that from
the items, `--widen` will *not* bring it back: it only ever asks for
**this** reader, which is what makes it safe to repeat. Point
`PDT_SECRET_READER_PATH` at `/usr/bin/security` **explicitly** for the
one read — the dialog comes up, **Always Allow** files it, and the line
goes back to this reader afterwards. Set it to the path rather than
deleting the line: an unset reader now issues no read at all, so
removing it would leave you with a keychain you still cannot read and no
way to re-file it.

The reader stays trusted either way, on purpose: it is the one program
that can rewrite a list, so removing it would remove the recovery path
too.

### The fallback, and what it costs

`--narrow` never reads a secret. `--widen` reads each item once, to
provoke the dialog, and throws the value away. The documented fallback,
`security add-generic-password -T`, has to read the value out and write
it back. It is one line, and the cost is stated in "What this does not
protect against":

```bash
security add-generic-password -U -a <account> -T <abs path to reader> <keychain> -w
```

**Never regenerate the signing certificate by accident.** Starting from
a new certificate changes the designated requirement, and every ACL
written with the old one stops matching. `build` reuses an existing
identity for exactly this reason — regenerating is a deliberate act, and
one you would then pay for with `adopt --widen` and `adopt --narrow`
again. Reuse, not just "an identity in the keychain": `build` also reuses
a key pair that is sitting on disk but has not made it into the keychain,
so a re-run after a failed import retries the import instead of quietly
minting a new certificate.

When regenerating *is* what you want — an exposed key pair, or a
rotation — the deliberate act has a name:

```bash
./setup.sh build --new-identity
```

It moves `cert.pem`, `key.pem`, `identity.pem` and `identity.p12` to
`*.abandoned` (nothing is deleted), stops if any `*.abandoned` file is
already there, and stops if the keychain still holds the identity —
printing `security delete-identity -c '<name>' '<keychain>'` for you to
run, because two certificates sharing a common name make every lookup by
name ambiguous, including the one `codesign` signs with. Delete the
identity, re-run, and every ACL pinned to the old certificate needs
`adopt --widen` (one dialog per item) and then `adopt --narrow`.

### Exit codes the reader returns

`setup.sh adopt` branches on these, so they are part of its contract:

| code | meaning |
|---|---|
| 0 | success |
| 2 | usage error — unknown option, an empty argument, missing `-a`, no keychain, or **two** keychains on one line (the read forms and `--adopt` alike: taking the last of two would read a different secret than the one asked for) |
| 3 | stdout is a terminal: refusing to put the secret on a screen |
| 4 | no keychain at that path, or it would not open |
| 5 | no item for that account — the ordinary "not filed yet" case |
| 6 | the item yielded no data, or the secret could not be written in full |
| 7 | `errSecAuthFailed` (-25293) — the item is there and **refused this binary** |
| 8 | `errSecInteractionNotAllowed` (-25308) — it would have had to ask, and there is nobody here to answer |

The last two used to be folded into 5, which is the one message that must
be precise: "no item for that account" sends an operator looking for a
credential that is on disk and perfectly readable by hand. 7 says the ACL
is the thing to look at; 8 says the process has no session to put a dialog
in front of — a launchd job, a daemon — and that retrying will not help.
`setup.sh` treats 5 as "skip this one, that provider is not configured"
and stops on anything else, with the reader's own message attached.

Usage errors are errors. An unknown flag is rejected rather than ignored,
and a `--adopt` line naming two keychains is refused rather than silently
answering the second one: both are a caller building a command line for a
different program, and dropping the mistake turns it into a quiet
operation on something other than what was asked for.

---

## Switching the backend over

**Implemented.** `backend/credentials.py` reads `PDT_SECRET_READER_PATH`
and **unset means no read is issued at all** — there is no fallback to
`/usr/bin/security`. A deployment that has the keychain switched on and
no reader configured gets `"missing"` for every secret, and
`backend/cli.py secrets verify` names that case specifically rather than
reporting an empty keychain.

That is deliberate, and it is the other half of what this directory is
for: a fallback would mean the ACL a deployment narrowed still has
`security` answering its reads, invisibly, and `setup.sh adopt` would
have nothing to prove. If you turn the keychain on, run `adopt`.

The helper is **argv-compatible** with the command line the backend
already builds:

```
pdt-secret-reader find-generic-password -a <account> -w <keychain>
                  ^^^^^^^^^^^^^^^^^^^^^ same argv as /usr/bin/security
```

so the switch is one line in the project `.env`:

```bash
PDT_SECRET_READER_PATH=/abs/path/to/tools/pdt-secret-reader/build/pdt-secret-reader
```

which the launcher already loads (see `secret_launcher._load_deployment_env`).
You do not have to paste that in: `adopt` writes it for you, and points
at it whichever mode you ran.

Going back is `./setup.sh adopt --widen`, with no rebuild and no editing
— **provided a `--narrow` has not already dropped the old reader from the
items**. `--widen` only ever asks about *this* reader, which is what makes
it safe to repeat; it does not restore anything else. The one case it
does not cover is written up under "Stuck? Widen it back", and it takes
one hand-edited line plus one read through `/usr/bin/security`.

### The one call it deliberately does *not* redirect

`credentials` runs the keychain tool for two different reasons, and only
one of them reads a secret:

| call | argv | redirectable |
|---|---|---|
| `_resolve_from_keychain` — get the password | `find-generic-password -a … -w …` | **yes** — this is the call an ACL governs |
| `_keychain_state` — is the keychain locked? | `show-keychain-info` | **no** — see below |

The lock probe asks about the *container*, never opening an item, so no
ACL entry is consulted and the system tool answers it on every
deployment — including one whose ACL has stopped trusting `security` for
a read. Redirecting it would require the reader to reimplement
`show-keychain-info` to be usable at all, and a probe that could not run
reports "the tool could not be run" for what is really a locked keychain.
That is the single message an operator most needs to get right.

Both halves are pinned in `tests/unit/test_credentials_switch.py`
(`test_the_lock_probe_keeps_the_system_tool` and its neighbours), which
fail if the probe is ever pointed at the reader.

### Order

1. `./setup.sh build`. Read the DR it prints. It asks which keychain the
   project keeps its secrets in, may append that keychain to your search
   list, and asks you for *its* password once — the project's, never the
   login keychain. See "Two things `build` does to your machine".
2. `./setup.sh adopt --widen`. Answer **Always Allow** on each dialog.
   This reader is added to every item and nothing already trusted is
   touched, so nothing can break. Once the accounts are read it records
   `PDT_SECRET_READER_PATH` in the project `.env` and reads the line back
   to confirm it landed.
3. Restart the backend, confirm the notifier comes up
   (`FeishuClient initialized`).
4. `./setup.sh adopt --narrow`. This reader alone; every other
   application on each list is dropped. Restart, confirm again.
5. Stuck? `./setup.sh adopt --widen` puts this reader back — see
   "Stuck? Widen it back" for the one case it does not cover.

Widen first, narrow second, because with both readers trusted there is no
interval in which the backend's next restart comes up without a notifier,
and narrowing first would create exactly that interval. That argument has
not changed — what has changed is that the order is now something you
type rather than something the script infers, so a deployment that
skipped a step is one it can also repeat deliberately.

---

## What this does not protect against

Said plainly, because the honest scope is small and the failure mode of
overselling it is real:

* **A process running as this user can still exec the helper.** The ACL
  names a binary, and any process that can run that binary gets the
  secret. What changes is that reading it is no longer *silent and
  universal* — it takes an explicit, attributable act.
* **Social engineering still works.** A prompt can be clicked through.
* **The ACL can be rewritten** by anything that can access the item.
* The `isatty` refusal inside the helper is a **speed bump**, not a
  boundary: `pdt-secret-reader … | cat` defeats it.

What it does buy: `/usr/bin/security` stops being a skeleton key for
these two items. Before, one command read them with no trace. After,
that command asks — and the asking is the point.

---

## Sources

* `man security` — the `-T` / `-A` / default-trust semantics quoted above,
  from the machine.
* [make-signing-identity.sh](https://github.com/inflightsec/keys-on-the-wire/blob/main/helper/make-signing-identity.sh)
  — self-signed codesigning identity, no Developer account; states that
  "a perfectly stable designated requirement… is the only property the
  keychain ACL cares about".
* [OpenAgentd `302ef6e`](https://github.com/lthoangg/OpenAgentd/commit/302ef6ebe44381905e49229ff4bdd479298d5db9)
  — the trap: `codesign` pins the certificate hash into the DR by
  default, so a *self-updating* app has to escape it or its ACL breaks on
  every release. Read it as "here is how to make a DR survive rebuilds",
  **not** as "identifier-only is the right requirement here" — for an ACL
  whose job is to restrict *who* may read, identifier-only is spoofable.
  See "Which requirement goes in the ACL".
* [agentcookie codesign runbook](https://github.com/mvanhorn/agentcookie/blob/v0.12.0-beta.1/docs/runbook-v0.12-codesign.md)
  — the other side: Developer ID *is* required for distribution and
  notarization, and ad-hoc builds "will not pass the Keychain ACL trust
  step".
* [Apple: if an unsigned app asks to use a keychain](https://support.apple.com/guide/keychain-access/kyca17140/mac)
  — unsigned apps can still be granted access, with the warning that
  "another app could gain access by mimicking this app".
