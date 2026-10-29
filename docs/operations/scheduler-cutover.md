# Scheduler cutover

The native scheduler lives inside the PDT backend and does the scheduling
itself. It replaces the scheduler that used to run beside it — a tick woken
by an external timer, and, on the deployments that ran the resident form, a
small service of its own with a read-only page and a control API on port
8004.

**The invariant this whole procedure exists for: at any moment there is
exactly one scheduler.** Two schedulers are two writers on the same queue,
and the operator finds out about the second one when the two have already
disagreed. Every step below is ordered so that the old scheduler stops
*before* the new one starts.

Three steps, in this order:

1. a 24 hour dry run, judged against the behaviour the old scheduler had;
2. the cutover, inside one comparison window;
3. the decommission of what is left over.

Nothing here is written for one machine. The identifiers that differ per
deployment — the old scheduler's job id, the state file's location — are
looked up at the moment they are needed and never written down.

---

## 1. Before you start

### The two switches, and when they are read

`scheduler.enabled` and `scheduler.dry_run` live in the `scheduler:` section
of `backend/config.yaml` — in the configuration file and nowhere else.
There is no endpoint and no panel control that writes them: the panel acts
on one plan at a time, never on the scheduler itself.

Both are read **when the backend starts**. A worker that is already running
does not stop itself because the file changed, so editing the file and
restarting the backend are two halves of one operation, and the second half
is not optional.

| setting | what it does |
| --- | --- |
| `enabled: false` | the worker thread is never started; nothing is scheduled and nothing is written |
| `dry_run: true` | the pipeline runs and writes down what it decided; no plan is started, stopped or moved |
| `enabled: true`, `dry_run: false` | the scheduler is live |

### The pre-flight

Run these from the repository root. Every one of them is a read.

```bash
# 1. The backend is up and can read its state database.
curl -fsS http://127.0.0.1:8000/health

# 2. The scheduler's settings, exactly as they are right now.
grep -A 8 '^scheduler:' backend/config.yaml

# 3. The old scheduler's tick, if it still has one: find the entry that
#    wakes the retired scheduler and write its job id down. The id is
#    different on every deployment — read it, do not assume it.
cc-connect cron list

# 4. The old scheduler's read-only page. It answers while it is alive.
#    Probe the page, not one of its /api/ paths: every path under /api/
#    sits behind a guard header, so an unguarded probe is answered 403
#    even while the service is running — `curl -f` would report a live
#    service as dead, and the operator would go looking for a process
#    that was running all along.
curl -fsS --max-time 2 http://127.0.0.1:8004/

# 5. The old scheduler's state directory, while it is still there. The
#    state file sits in it; the directory can be pointed elsewhere with
#    AC_SCHEDULER_STATE_DIR.
ls -l "${AC_SCHEDULER_STATE_DIR:-$HOME/.ac-scheduler}"

# 6. What the supervisor is managing right now.
curl -fsS http://127.0.0.1:8002/api/v1/subsidiaries
```

Do not go on until:

- the backend answers on `/health`;
- you know which entry of the cron list is the old scheduler's, and which
  service — if any — is serving port 8004;
- the supervisor's list of managed services is written down. It is the list
  you compare against after the decommission.

---

## 2. The dry run and the judgement of its artifacts

### What a dry run is

A dry run is the pipeline running on its own rhythm while reading the plans
and changing none of them. It is the evidence for the switch: 24 hours of
it is 48 half-hour windows, and a half-hour window is the unit the old
scheduler worked in.

Set it up like this and restart the backend — the switches are read at
startup:

```yaml
# backend/config.yaml — for the dry run only
scheduler:
  enabled: true
  dry_run: true
```

### The three artifacts

One day of a dry run leaves three files under the dry-run root —
`/tmp/pdt-native-scheduler/dryrun` by default, which is the constant
`DEFAULT_DRYRUN_ROOT` in `backend/plan_scheduler/dryrun.py` — in a
directory named after the day:

| file | what it holds |
| --- | --- |
| `ticks.jsonl` | one line per decision, four fields: `ts`, `action`, `plan_id`, `reason` |
| `windows.json` | one record per half-hour boundary: the running set and the queue snapshot |
| `diff.md` | the report over those windows, one line per difference |

### The criterion

The switch may happen when all of this holds:

- the run covered **48** windows — a day, at the old scheduler's own
  cadence;
- at least **45** of them are identical;
- the windows that are not identical carry **allowed** differences only;
- **no** forbidden difference appears anywhere.

| verdict | kinds |
| --- | --- |
| allowed | `transient_set_diff`, `workspace_order_diff` |
| forbidden | `new_start`, `eviction_flip`, `queue_order_flip` |

A kind nobody recognises counts as forbidden. A difference nobody has
looked at is not a difference that does not matter.

### Judging it

The judgement is executable, and it is the same judgement the release gate
uses — the replay script calls the criterion in `plan_scheduler.dryrun`
rather than re-implementing it:

```bash
DRYRUN_ROOT=/tmp/pdt-native-scheduler/dryrun
DAY=$(date -u +%Y%m%d)

ls -l "$DRYRUN_ROOT/$DAY"

python3 scripts/scheduler_dryrun_diff.py \
  --ticks   "$DRYRUN_ROOT/$DAY/ticks.jsonl" \
  --windows "$DRYRUN_ROOT/$DAY/windows.json" \
  --out     "$DRYRUN_ROOT/$DAY/diff.md"
echo "exit=$?"
```

| exit code | meaning | what to do |
| --- | --- | --- |
| 0 | the criterion holds — the switch may happen | go to section 3 |
| 1 | a forbidden difference, fewer than 48 windows, or fewer than 45 identical ones | do not switch. Read `diff.md`, find the window that differs, fix it, run the dry run again |
| 2 | an input file is missing, unreadable or malformed — the missing path is printed | the dry run did not produce what it should have. Check that it ran with `dry_run: true` and that the day is complete |

---

## 3. The cutover, inside one window

The comparison was made in half-hour windows, so the cutover happens in one
too: start just after a window boundary — at `:00` or `:30` — and finish
inside the next half hour. A window covered twice is a window in which two
schedulers were live; a window covered by neither is a window in which
nothing ran at all.

**The old scheduler stops first.** There is no moment in which both are
running.

```bash
# 1. Stop the old scheduler's tick. Take the job id from the cron list
#    you wrote down in the pre-flight — it is not written down anywhere
#    else, and it is different on every deployment.
cc-connect cron edit <old-scheduler-job-id> enabled false

# 2. Confirm it is off before anything else starts.
cc-connect cron list
```

Only now does the native scheduler go on. Edit `backend/config.yaml`:

```yaml
# backend/config.yaml
scheduler:
  enabled: true
  dry_run: false
```

```bash
# 3. Restart the backend. The two switches are read at startup, so the
#    restart is part of the operation, not a follow-up to it. From the
#    repository root:  python3 -m backend.server
#    (use whatever actually runs the backend in this deployment)

# 4. Verify: the read model answers, and the queue is where the dry run
#    left it.
curl -fsS -H 'X-PDT-Request: 1' http://127.0.0.1:8000/api/scheduler/state
```

The answer is the read model: `capacity` (total and used), `queue_length`,
`over_capacity`, `queue`, `running` and `generated_at`. The startup log
says which of the two happened:

```
[scheduler] started: capacity=4 dry_run=False tick_seconds=5.0
```

If it says `disabled by the configuration; the worker thread is not
started`, the switch did not take — check the file, and restart again.

---

## 4. Rolling back

### The rollback is one setting and one restart

```yaml
# backend/config.yaml
scheduler:
  enabled: false
```

```bash
# Restart the backend: the switch is read at startup.
# From the repository root:  python3 -m backend.server
```

After the restart the worker thread is not started at all, and nothing is
written: not to the queue, not to the slots, not to the commands, and not
to a plan's phase.

### A rollback stops the writing; it does not undo the history

The rows that are already there stay exactly as they were. The plans that
were started keep running — the rollback is not a stop and not a pause.
Rolling back gives the plans back to the operator; it does not re-queue
them.

### The switch is not in the panel

The panel does not expose this switch. There is no button, and there is no
endpoint that flips `scheduler.enabled`: the four panel actions act on one
plan each, and a control that stops every plan in the system is one
mis-click away from an outage. The configuration file is the only place the
switch lives, and editing it is the only way back.

### Verifying the rollback

```bash
# The read model answers with the idle document: no capacity in use,
# nothing queued, nothing running.
curl -fsS -H 'X-PDT-Request: 1' http://127.0.0.1:8000/api/scheduler/state

# The startup log says the worker was not started:
#   [scheduler] disabled by the configuration; the worker thread is not started
```

---

## 5. The decommission checklist

One item per line. Each one carries the command that proves it and what
"done" looks like. Work down the list: a checklist that is only read is
not a checklist.

### The old read-only page is gone

```bash
curl -sS --max-time 2 http://127.0.0.1:8004/
echo "exit=$?"
```

Done when the connection is refused. The old read-only page answers
nothing.

### The old control API is gone

```bash
curl -sS --max-time 2 -X POST http://127.0.0.1:8004/api/control/promote
echo "exit=$?"
```

Done when the connection is refused. The control API is the port's other
half, and a page that is gone while its control API still answers is only
half decommissioned.

### Nothing listens on the old port

```bash
python3 -c 'import socket; s = socket.socket(); s.settimeout(2); print("open" if s.connect_ex(("127.0.0.1", 8004)) == 0 else "closed")'
```

Done when it prints `closed`. The probe has a timeout: a probe without a
bound is a probe that hangs the check instead of answering it.

### The old state file is gone

```bash
test ! -e "${AC_SCHEDULER_STATE_DIR:-$HOME/.ac-scheduler}/state.json"
echo "exit=$?"
```

Done when the file is not there. The retired scheduler's state belongs to
the retired scheduler, and a file that looks live and is not is how the
next operator is misled.

### The supervisor no longer manages the retired scheduler

```bash
curl -fsS http://127.0.0.1:8002/api/v1/subsidiaries \
  | python3 -c 'import json, sys; print([row["name"] for row in json.load(sys.stdin)])'
```

Done when `ac-scheduler` — the name the supervisor registers the retired
scheduler under — is not in that list. A supervisor that still manages
it starts it again on the next tick, and that tick is the moment the
invariant breaks.

If the entry is kept at all, it is kept as the read-only Feishu
forwarder and nothing else: no tick, no port 8004, no control API, no
read-only page, no old state file — a request that carries
`X-PDT-Request: 1` and reads PDT's read-only endpoints, and publishes
nothing but the progress card.

The rest of that list is none of this item's business.
`provider-optimizer`, `provider-usage-forwarder`, the `perpetual-*`
services and `pdt-backend` are managed for their own reasons, and this
check does not ask for any of them to be removed. "The retired
scheduler's entry" and "the supervisor's managed set" are two different
things, and a checklist that reads as one sends the operator to delete
the PDT backend along with the old scheduler.

### The old cron entry is retired

```bash
cc-connect cron list
```

Done when the list no longer holds an entry for the retired scheduler.
Retire the entry once the cutover has been verified:

```bash
cc-connect cron del <old-scheduler-job-id>
```

The job lives in the scheduler tool's own store, not in this repository,
so deleting the entry is the whole of the retirement.

### The native scheduler is the only one left

```bash
curl -fsS -H 'X-PDT-Request: 1' http://127.0.0.1:8000/api/scheduler/state
```

Done when the read model answers and the backend's log shows exactly one
started scheduler.
