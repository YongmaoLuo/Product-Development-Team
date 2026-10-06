# Security

## The trust model, stated plainly

This is a **single-user local tool**. It runs commands as you, on your
machine, with the privileges you already have. It is not designed to be a
boundary between people, and no part of it should be read as one.

What that means in practice:

| Property | Consequence |
|---|---|
| No authentication | Anyone who can reach the port can drive the instance. |
| No user separation | Every plan, artifact and credential is shared state. |
| Runs as you | A sub-agent's commands have your access to your filesystem and network. |
| Loopback by default | It is not listening to the network unless you set `PDT_HOST`. |

See [Running it](operations/running.md) for what the request guard does and
does not protect, and for what to do if you genuinely need it off-loopback.

## What is protected, and what is not

**Protected by design:**

- **Browser-origin access.** A web page you happen to have open cannot drive
  the instance. The mechanism is described in
  [Running it](operations/running.md); the short version is that the guard
  makes an attacker's request one the *browser* refuses to send, rather than
  one the server refuses to answer, so knowing the header value does not
  help.
- **DNS rebinding.** A hostname that resolves to loopback is rejected on the
  `Host` header.
- **Credential-bearing files.** Temp files that carry provider credentials
  are written into a private directory with a private mode, and the
  credential is redacted once the process that needed it has exited.
- **Notification secrets in a process listing — when the keychain path is
  enabled.** The two notification secrets can be read from a dedicated
  keychain and delivered over a file descriptor, so `ps eew` and
  `KERN_PROCARGS2` show an fd number rather than the secret. This is
  opt-in and off by default: an installation that has not run the
  migration is still reading them from the environment, and
  `secrets verify` is what tells the two apart. See
  [Keychain migration](operations/keychain-migration.md).

**Not protected, and not intended to be:**

- **A local process running as your user.** It can read anything you can
  read. This is true of every tool in this class; treat the machine as the
  boundary, not the application.
- **A sub-agent acting on content it read.** Sub-agents ingest plan files,
  repository files and command output, and they have shell access to do
  their job. Treat plan content with the same suspicion you would treat any
  other input that ends up in an execution context.
- **Anything reachable from a shell.** The edit/write guard is a discipline
  guard against accidents, not a containment boundary — a shell command is
  not constrained by a tool-level hook.

## Reporting a vulnerability

Open a private security advisory on the repository
(<https://github.com/YongmaoLuo/Product-Development-Team/security/advisories/new>)
rather than a public issue. Please include the version or commit, what an
attacker gains, and the smallest reproduction you have.

This is a personal project rather than a funded one, so there is no bug
bounty and no guaranteed response window — but reports are read, and a real
issue will be fixed rather than argued with.

## How this project audits itself

The repository carries a security audit document at `SECURITY_AUDIT.md`. It
is a build input, not a write-up: several of the test gates parse it and
assert on its structure, and the diff-attribution gate requires that a
change to the source be traceable to an entry in it.

It records **defects and their remediation** — the shape of the problem,
what the fix was, and how to verify the fix. It deliberately does not record
what happened on any particular machine: counts, durations and exposure
windows are not re-derivable by a reader of the source, so publishing them
would cost something and buy nothing.

The same line splits the audit in two. Anything describing **the sweep
rather than the software** — a dated wall-clock baseline, the grid of which
(problem class × surface) cells were examined and came back empty, the
table of greps and their dispositions — is bookkeeping over the process
that produced the findings. None of it is derivable from the source, and
the negative half of it is a map of where nobody found anything, which is
where nobody will look next. So it lives operator-local under the
gitignored `.config/`: a fresh clone has none of it, and the meta-tests
that parse it skip there rather than fail. `SECURITY_AUDIT.md` keeps the
tier definitions, the entry template, the findings, and the two appendices
that *are* statements about the repository — which tests were modified, and
which source files the audit authorised changing.

That line is enforced, not just stated. A static gate fails the build if a
public document carries a collection date, a suite baseline figure, a
negative-result cell, or one of the sweep sections' headings.

If you are adding to it, the working rule is in
[Contributing](development/contributing.md): write what is true of the
software, not what is true of your machine.
