# Orphan reaper

PDT spawns processes — a backend, a fixture, an agent, a helper — and
most of them are supposed to die with the thing that started them.
Some do not.  When the parent is killed hard (a swap storm, a jetsam,
a `kill -9`), the child is re-parented to pid 1 and keeps holding its
memory, and nothing left on the machine knows that it belongs to PDT.

The **orphan reaper** closes that gap.  It scans the machine, decides
which processes are genuinely orphaned, and reclaims the ones that
are: `SIGTERM`, a grace period, then `SIGKILL`.

It is deliberately conservative.  A process is only ever a candidate if
PDT itself marked it, and every unknown resolves towards leaving it
alone.  This page is the operator's contract: what the reaper
recognises, how it is configured, where its audit trail lands, and the
four conditions under which it refuses to kill anything.

## How a process is recognised as ours

The reaper does not guess from the working directory or the shape of
the command line: PDT is open source and runs on other people's
machines, where neither says anything reliable.  What every
PDT-spawned process carries is a marker **PDT put there itself**,
inherited by every descendant:

| Marker | Meaning |
|---|---|
| `PDT_SPAWN_OWNER_PID` | the pid of the process that spawned it |
| `PDT_SPAWN_OWNER_START` | that owner's start time — it defeats pid reuse |
| `PDT_SPAWN_KIND` | what it is: `service`, `fixture`, `subprocess`, `agent` or `reaper` |

Two properties make the scheme work:

* **The owner is stamped at every spawn level**, not once at the top.
  The owner of a process is the process that actually started it, so a
  grandchild whose parent died is an orphan even when the top-level
  backend is still running.
* **The owner start time travels with the pid.**  A pid alone is not
  an identity — the kernel recycles pids — so the marker records
  *when* the owner started.  A live pid whose start time differs from
  the recorded one is a different process that happens to have the
  same number, and must not be touched.

## Configuration

Everything is environmental, and every default is the safe value — a
deployment that configures nothing can only ever reclaim processes PDT
itself marked.

| Variable | Default | Meaning |
|---|---|---|
| `PDT_ORPHAN_REAPER` | `1` | `0` turns it off entirely |
| `PDT_ORPHAN_REAPER_INTERVAL` | `30` | seconds between scans |
| `PDT_ORPHAN_REAPER_PROTECT_PORTS` | `(none)` | extra protected ports, comma separated; **added** to the defaults, never replacing them |
| `PDT_ORPHAN_REAPER_GRACE` | `5` | seconds between SIGTERM and SIGKILL |
| `PDT_ORPHAN_REAPER_IDLE_EXIT` | `1800` | the daemon exits after this many idle seconds; `0` disables the self-exit |

The default protected ports are `8000`, `8002`, `3000`, `3001` and
`8651` — PDT's own long-lived services (the backend, the supervisor,
the frontends and one more service).  A process holding one of them is
very likely to be a service somebody is using, so it is protected by
default.

A malformed value never raises: it falls back to the default, because
a reaper that refuses to start over a typo would leave the memory it
exists to reclaim unreclaimed.  With `PDT_ORPHAN_REAPER=0` a pass
prints `disabled` and does nothing at all.

## Running it

The reaper depends on the standard library only, so it runs on its own:

```bash
python -m backend.orphan_reaper --once
```

| Flag | What it does |
|---|---|
| `--once` | one pass: scan, reap, write the events down, print one line per verdict, and exit |
| `--daemon` | the same pass every `interval` seconds, until it is stopped (`SIGTERM`/`SIGINT`) or the machine has been quiet for `idle_exit` seconds |
| `--dry-run` | decide but never signal: every verdict is reached and written down, and not one signal is sent |
| `--interval` | override the scan interval, in seconds |
| `--plans-root` | override the plan-artifact root (default: `$PDT_PLANS_DIR`, otherwise `<repo>/plans`) |

Without `--once` or `--daemon` the reaper prints its resolved
configuration and exits without touching anything.

The daemon is a **singleton**: it holds the reaper's lock while it
runs, and a second one stands down immediately — two reapers would
race over the same candidates and apply the same grace period twice.

**What a pass prints.**  One line per verdict — `pid decision reason`,
plus the marker kind when the verdict carries one:

```
4711 kill orphan service
4712 skip unmarked
```

**Where the audit trail lands.**  Every pass appends one JSON line per
verdict — kills *and* skips — to:

```
<plans_root>/.orphan_reaper/kill_events.jsonl
```

A record looks like this:

```json
{"ts": "2026-10-27T10:00:00", "pid": 4711, "ppid": 1, "pgid": 4711, "argv": "python -m backend.server", "rss": 213000, "owner_pid": 999, "owner_start": "17", "reason": "orphan", "signal": "TERM"}
```

`signal` says what was sent — `TERM`, `KILL`, or `none` for a dry run
— and is empty for every verdict that was not a kill.  The log is
appended to, never rewritten: a reaper that runs every thirty seconds
must not lose the record of what it did the last thirty seconds ago.
A write that fails is a warning and nothing else; the reaper exists to
give memory back, and a full disk must not stop it from doing that.

## The verdict ladder

Every candidate is walked down the verdict ladder in a fixed order.  The
six rungs below are the ones that separate "ours, and orphaned" from
everything else; the ones not set out here — `other_uid` (the process
belongs to another user), `env_unreadable`, `owner_unknown` and
`ports_unknown` — can only ever skip, and are covered in the last
section of this page.

No rung can turn a skip into a kill: the only way to reach the last one
is to have passed all the others, and the last one is the only one that
may send a signal.

### 1. No PDT marker

A process that carries no `PDT_SPAWN_*` marker at all is not ours, so
it is never a candidate.  The reason recorded in the log is
`unmarked`.  Anything a user started by hand — their own pytest run,
their editor, an unrelated service — is invisible to the reaper,
always.

### 2. Owner still alive

A process whose owner is still alive is never a candidate, even when
every other marker looks right.  The reason is `owner_alive`; when the
owner is alive but the two start times could not be compared, the
reason is `owner_alive_unverified` — which is still a skip.

### 3. PID reuse

A pid is not an identity: the kernel recycles them.  When the owner
pid is alive but its start time differs from the recorded one, it is a
different process that happens to have the same number, and the
candidate is left alone.  The reason is `pid_reused`.

### 4. Protected port

A process that holds a protected port is never a candidate.  The
reason is `protected_port`.  The protected set is the default ports
plus whatever `PDT_ORPHAN_REAPER_PROTECT_PORTS` adds — never minus.

### 5. The reaper itself

The reaper never signals another reaper, and never signals pid 1 or
itself.  The reason is `self_reaper` (or `self`).

### 6. Owner is dead — reclaim

Only a process that carries the markers, whose owner is provably gone,
that holds no protected port and that is not the reaper reaches this
rung.  The reason is `orphan`, and it is the only reason that may lead
to a signal: `SIGTERM`, then the grace period, then `SIGKILL`.

## Uncertainty resolves towards not killing

Two of the rungs above are *degradations* rather than judgements: an
environment that could not be read is **unknown**, not empty, and a
port map that could not be read is **unknown**, not empty.  "We could
not tell" is not evidence of anything, and mistaking it for evidence is
exactly how an unrelated process ends up on the kill list.  The same
holds for an owner that could not be checked at all
(`owner_unknown`) and for a port map that could not be read
(`ports_unknown`).

This is the **fail-closed** rule: when the reaper cannot tell, it
leaves the process alone.  A marker that cannot be read, a start time
that cannot be determined, a pid that cannot be checked — each of them
means skip, never kill.

**Reading an unreadable environment on macOS.**  macOS has no
`/proc`, and `sysctl kern.procargs2` does not expose the environment
on this platform, so the environment is read through `ps -E`.  Apple's
own binaries are protected by SIP: `ps -E` shows their command line
and no environment at all.

A read that produced output but not one `k=v` segment therefore means
*unreadable* — not empty.  Such a process is recorded with the reason
`env_unreadable` and is skipped, never killed.  **This is by design,
not a fault**: the reaper cannot tell whether an Apple platform binary
carries a marker, and "cannot tell" resolves towards leaving it alone.
Seeing `env_unreadable` in the log means the boundary held, not that
something broke.
