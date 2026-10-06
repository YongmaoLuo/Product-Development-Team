# Strict-lane record: the macOS keychain full-delivery E2E gate

A measurement record, not a design note. It answers one question — *what does
`PDT_REQUIRE_KEYCHAIN_E2E=1` actually do on a machine that has both gated
binaries?* — with commands and outputs rather than with a summary line. Every
figure below was produced by the command shown next to it.

This file lives at the repository root, outside `docs_dir`, for the same reason
`SECURITY_AUDIT.md` does: it is a record about one gate on one platform, not
part of the published documentation set. Nothing under `docs/` was touched, so
the nav and translation obligations that `mkdocs build --strict` enforces are
unaffected.

## The invariant

`PDT_REQUIRE_KEYCHAIN_E2E=1` closes the **skip** channel. It converts "the
capability is absent, so skip" into a failure. Therefore, in the strict lane:

> **the skip count must be ZERO.**

The two readings below look alike to someone skimming counts, and conflating
them is the failure this record exists to prevent:

| Reading | Verdict | What it means |
| --- | --- | --- |
| `25 passed`, zero skips | **GREEN** | the capability was present, every case really executed, there was nothing to skip |
| `N skipped` | **RED** | the switch is inert — it promised the capability was required and then skipped anyway |

A summary line with no `skipped` token is *not* a measurement of the skip
count; it is an absence of evidence. That is why the decomposition below is
taken from a report object rather than read off the terminal.

## What the position actually printed

Run from `backend/`:

```
$ PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest \
    tests/e2e/test_keychain_full_delivery_macos.py -q --tb=short
============================= 25 passed in 4.55s ==============================
REAL_EXIT=0
```

`25 passed`, exit code `0`, captured unpiped so the number is the shell's and
not a `$PIPESTATUS`/`pipestatus` reading. (Under zsh the conventional
`echo "exit=${PIPESTATUS[0]}"` prints an empty `exit=` — zsh spells the array
lowercase and indexes from 1 — so the exit code was taken from an unpiped run.)
The wall-clock figure moves between runs; the counts and the exit code do not.

**On a machine where both binaries execute, `25 passed` / exit `0` is the
correct outcome, not a missing red.** The red for this lane is not a property of
this machine; see *Where the `6 FAILED` red comes from*.

### Skip count is zero — measured

The same run, reported to JUnit XML so the counts are fields rather than prose:

```
tests=25 failures=0 errors=0 skipped=0
SKIP COUNT IS ZERO: True
per-test outcomes: {'passed': 25}
```

Per-test statuses are derived from each `testcase` element's child
(`failure` / `error` / `skipped`, otherwise `passed`) rather than from a
`status` attribute, which JUnit XML does not carry. All 25 are `passed`; none is
skipped, failed, or errored.

All six gated cases are in the passed set, so each one executed. Enumerated
from the module's own `_GATED_CASE_TOOLS`, not by hand:

```
test_full_delivery_reads_the_real_keychain_entry
test_ps_eew_output_excludes_the_secret
test_kern_procargs2_output_excludes_the_secret
test_positive_control_ps_eew_does_see_the_secret
test_delivery_payload_actually_carries_the_secret
test_temporary_keychain_is_removed_even_on_failure
```

The module agrees when asked directly, rather than the two facts being inferred
from each other:

```
$ ./.venv/bin/python3 -c "import tests.e2e.test_keychain_full_delivery_macos as m; print('_UNRUNNABLE_PROBES =', m._UNRUNNABLE_PROBES)"
_UNRUNNABLE_PROBES = []
```

An empty refusal list is the mechanism behind the zero skip count: the skip
channel is not merely unused, it has nothing to carry.

### The suite's own banner is not a second measurement

The same run ends with a block that looks more authoritative than pytest's own
summary, and it is the one a reader is most likely to quote:

```
================================= TEST RESULT ==================================
TEST_RESULT: PASSED
REASON: 25 tests collected, all passed. Exit code: 0
============================= 25 passed in 4.55s ==============================
```

It is emitted by `pytest_terminal_summary` in `backend/tests/conftest.py`, for
the agent harness rather than for a human. It is **not** independent evidence
of the skip count, and the reason is specific: it reports
`terminalreporter._session.testscollected` — how many tests were *collected* —
and branches only on `exitstatus == 0`. Neither quantity is a count of
outcomes. A skipped test is still collected, and pytest still exits `0` when
every remaining test passed. Reproduced on a throwaway file with one passing
case and one skipped case, carrying the same hook:

```
$ ./.venv/bin/python3 -m pytest test_skip.py -q     # one passing case, one skip
.s                                                                       [100%]
================================= TEST RESULT ==================================
TEST_RESULT: PASSED
REASON: 2 tests collected, all passed. Exit code: 0
1 passed, 1 skipped in 0.00s
```

So `all passed` is printed by a run that skipped half its cases. That is the
failure this record exists to prevent, arriving by a different route — which
is why the reading above is taken from the JUnit report, and not from either
summary line. The zero here is established by the report; the banner merely
agrees with it, and would not have disagreed if it had not.

## Positive evidence for both tools

Not inferred from the files being present. Existence, the execute bit, and
actually starting are three separate claims and each was checked:

```
$ ls -l /usr/bin/security /bin/ps
-rwsr-xr-x  1 root  wheel  170432 Aug 13 10:51 /bin/ps
-rwxr-xr-x  1 root  wheel  660768 Aug 13 10:51 /usr/bin/security

$ stat -f '%N %Sp %OLp' /usr/bin/security /bin/ps
/usr/bin/security -rwxr-xr-x 755
/bin/ps -rwsr-xr-x 755
```

```
$ ./.venv/bin/python3 -c "import os; [print(p,'exists=',os.path.exists(p),'X_OK=',os.access(p,os.X_OK)) for p in ('/usr/bin/security','/bin/ps')]"
/usr/bin/security exists= True X_OK= True
/bin/ps exists= True X_OK= True
```

Both binaries exist, both carry their execute bits, both are `X_OK`, and both
start — repeatedly, returning `rc=0` every time:

```
$ for i in 1 2 3; do
      /usr/bin/security list-keychains >/dev/null 2>&1; echo "security list-keychains run$i rc=$?"
      /bin/ps eew $$ >/dev/null 2>&1;             echo "ps eew            run$i rc=$?"
  done
security list-keychains run1 rc=0
ps eew            run1 rc=0
security list-keychains run2 rc=0
ps eew            run2 rc=0
security list-keychains run3 rc=0
ps eew            run3 rc=0
```

The subcommand and flag spellings are the ones the module itself uses
(`/usr/bin/security <subcommand>`, `/bin/ps eew <pid>`), so this is the
capability under test and not some easier command.

**One reading that must not be misquoted.** Invoked with no arguments at all,
`/usr/bin/security` exits `2` and prints its usage. That is `security`'s own
usage exit, not a refusal:

```
$ /usr/bin/security >/dev/null 2>&1; echo "bare security rc=$?"
bare security rc=2
$ /usr/bin/security 2>&1 | head -1
Usage: security [-h] [-i] [-l] [-p prompt] [-q] [-v] [command] [opt ...]
```

This is also why the module's probe (`_tool_is_runnable`, which runs the binary
with `-h`) keys on a raised `OSError` rather than on the exit status: a tool
that starts and then declines a flag is runnable, and only a refusal to start
at all is a refusal.

**That is why the strict lane is green here: there was no refusal for the
switch to convert.**

## Where the `6 FAILED` red comes from

Cited, not fabricated. It was not produced by this run and is not a property of
this machine.

It comes from the *driving construction* built in task `20-2-2-2` and pinned in
`20-2-2-3`, which lives in
`backend/tests/e2e/test_keychain_full_delivery_macos.py`:

* `_refused_tool(tmp_path)` (line 3070) writes a script, `chmod`s it `0600`,
  and asks the real probe about it. The kernel declines to `execve` a file with
  no execute bit, so the refusal is manufactured and portable — it stands in
  for the *condition* (a machine that will not start the tool), not for the
  policy. The reason string is the probe's own reading, so the path and the
  errno in any failure message are the platform's words rather than a phrase
  written into the file.
* `_run_gated_module(tmp_path, unrunnable=[reason], strict=True,
  platform="darwin")` (line 3131) then re-runs the six gated cases in a child
  pytest with the switch on and that refusal injected, and
* `test_strict_mode_fails_instead_of_skipping` (line 3217) asserts the result:
  zero skips, and `len(_GATED_CASE_TOOLS)` failures — the switch failing at
  call time rather than degrading to a skip. The `platform="darwin"` argument
  is what keeps the case about the *tools*:
  `test_platform_skip_wins_over_strict_mode` (line 3381) runs the same
  construction with `platform="linux"` and asserts the opposite outcome, so the
  two gates are pinned apart rather than one of them being assumed.

The two readings stay distinct. On this machine the strict lane is `25 passed`
with zero skips, because both tools execute. `6 FAILED` is that constructed
refusal being converted, reachable only when a refusal is supplied
deliberately — and the case that supplies it runs *inside* the 25, which is
why this lane is green and still contains a passing test about a red.

## False-claim guard

No line in this record says this machine refused to execute anything. No such
refusal was captured, so no such line is written; the opposite was measured,
three times per tool, at `rc=0`. Every positive claim above is a recorded
command and its output, and the one non-zero exit recorded (`bare security
rc=2`) is quoted with the usage text that explains it, because leaving it out
would be the more flattering error.

The `6 FAILED` result is not reported as having been observed, because it was
not. It is cited to the case that constructs it, and the skip count that would
make it real was never manufactured on this machine by anything other than
that case's own child process.

The one `TEST_RESULT: PASSED` block quoted in this record is a real emission
from `tests/conftest.py` and is reproduced here *with the caveat above it*
rather than dropped — it is the reading most likely to be quoted back out of
context, and it is the one that does not measure what its own words claim.

## Reproducing this

```bash
cd backend

# the acceptance run — expect 25 passed, exit 0, zero skips
PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest \
    tests/e2e/test_keychain_full_delivery_macos.py -q --tb=short

# the skip count as a number rather than as an absence
PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest \
    tests/e2e/test_keychain_full_delivery_macos.py -q --junitxml=/tmp/kc_strict.xml

# the two binaries, starting, three times each
for i in 1 2 3; do
    /usr/bin/security list-keychains >/dev/null 2>&1; echo "security run$i rc=$?"
    /bin/ps eew $$ >/dev/null 2>&1;             echo "ps eew     run$i rc=$?"
done
```

The switch gates the macOS lane; on a non-darwin machine the platform skip takes
precedence over the strict position, and the six cases are skipped by the
platform rather than failed by the switch.

## 2026-10-05: this record was clobbered, and HEAD could not have repaired it

This section has the same form as the sections above it: the commands that were
run, and their raw output. It exists because the record's own first line was
replaced once by report output about the record — a clobber of exactly the shape
that truncated it — so the first question on any future pass is *which copy is
damaged*, and that question has to be answered by measurement rather than by
assuming HEAD is the safe one.

The state of the tree as found:

```console
$ git status --porcelain
MM KEYCHAIN_STRICT_LANE_RECORD.md

$ wc -l KEYCHAIN_STRICT_LANE_RECORD.md
       8 KEYCHAIN_STRICT_LANE_RECORD.md

$ head -c 3 KEYCHAIN_STRICT_LANE_RECORD.md
Lin

$ git diff --stat HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md
 KEYCHAIN_STRICT_LANE_RECORD.md | 39 ++++++---------------------------------
 1 file changed, 6 insertions(+), 33 deletions(-)
```

Two separate defects, and the standard repair line fixes neither one correctly:

* The **worktree** copy was TRUNCATED — 8 lines, and its first line is report
  prose (`Lin`, the opening of `Lines 1-262: byte-identical to ...`) rather than
  the record's `# S`. The first line of a measurement record must never be a
  statement about a measurement record.
* The copy **committed at HEAD is itself damaged** — 35 lines, ending
  mid-section on a half-sentence with nothing after it:

  ```console
  $ git show HEAD:KEYCHAIN_STRICT_LANE_RECORD.md | wc -l
        35

  $ git show HEAD:KEYCHAIN_STRICT_LANE_RECORD.md | tail -3
  ## What the position actually printed

  Run from `backend/`:
  ```

So `git checkout HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md` — the recovery line
this file is nominally about — would have restored the damaged 35-line stub and
committed the loss of the other 227 lines a second time. It was deliberately not
run. The intact copy was already in git, in the index, and was restored from
there:

```console
$ git show :KEYCHAIN_STRICT_LANE_RECORD.md | wc -l
     262

$ diff -q <(git show :KEYCHAIN_STRICT_LANE_RECORD.md) \
          <(git show 10e9d6b:KEYCHAIN_STRICT_LANE_RECORD.md) && echo identical
identical

$ git checkout -- KEYCHAIN_STRICT_LANE_RECORD.md

$ wc -l KEYCHAIN_STRICT_LANE_RECORD.md
     262 KEYCHAIN_STRICT_LANE_RECORD.md

$ head -c 3 KEYCHAIN_STRICT_LANE_RECORD.md
# S

$ tail -3 KEYCHAIN_STRICT_LANE_RECORD.md
The switch gates the macOS lane; on a non-darwin machine the platform skip takes
precedence over the strict position, and the six cases are skipped by the
platform rather than failed by the switch.

$ grep -n "^## " KEYCHAIN_STRICT_LANE_RECORD.md
14:## The invariant
33:## What the position actually printed
128:## Positive evidence for both tools
188:## Where the `6 FAILED` red comes from
221:## False-claim guard
240:## Reproducing this
```

With the 262 lines back, the diff against the damaged HEAD is non-empty by
construction — HEAD is what was short, so recovering the record *is* the change:

```console
$ git diff --exit-code HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md; echo "exit=$?"
exit=1

$ git diff --stat HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md
 KEYCHAIN_STRICT_LANE_RECORD.md | 227 +++++++++++++++++++++++++++++++++++++++++
 1 file changed, 227 insertions(+)
```

The verdict, stated against the two questions that decide the branch: the
worktree copy was TRUNCATED, and **HEAD's copy was damaged, not intact** — 35
lines against 262, closing on `Run from `backend/`:` rather than on a finished
paragraph. A restore was needed, and it could not be the one HEAD provides. No
part of this file was reconstructed from memory, from a diff, or from the report
text that had replaced it; every line above came out of a copy git still held.

The lesson outlives the incident. `git checkout HEAD -- <path>` is a restore only
if HEAD was checked first, and here HEAD and the worktree agreed with each other
in the one respect that makes the repair look safe — both were short — which is
precisely why the check is worth the one command it costs:

```console
$ git show HEAD:KEYCHAIN_STRICT_LANE_RECORD.md | wc -l
      35
```

`git diff --stat` said *6 insertions, 33 deletions*, which reads like an edit and
not like a truncation. Only the line count separates the two.

## 2026-10-05: the citations this record makes, re-measured

The section above answered *which copy is damaged*. It did not answer the
question that a restore leaves standing: a file can be intact and stale, and
nothing in this repository checks that the things a measurement record cites
still exist. The three internals of
`backend/tests/e2e/test_keychain_full_delivery_macos.py` that this record's own
reproduction commands depend on were each asked directly, on 2026-10-05, from
`backend/`.

### The three internals, asked directly

```console
$ ./.venv/bin/python3 -c "import tests.e2e.test_keychain_full_delivery_macos as m; print(m._STRICT_SWITCH_ENV)"
PDT_REQUIRE_KEYCHAIN_E2E
```

which is the switch every command in *Reproducing this* sets.

```console
$ ./.venv/bin/python3 -c "import tests.e2e.test_keychain_full_delivery_macos as m; print(sorted(m._GATED_CASE_TOOLS))"
['test_delivery_payload_actually_carries_the_secret', 'test_full_delivery_reads_the_real_keychain_entry', 'test_kern_procargs2_output_excludes_the_secret', 'test_positive_control_ps_eew_does_see_the_secret', 'test_ps_eew_output_excludes_the_secret', 'test_temporary_keychain_is_removed_even_on_failure']
```

Six keys, and the six names printed in *Skip count is zero — measured* are the
same six — no name in that list is absent here, and no key is absent there. The
order differs only in that this command sorts:

```console
$ ./.venv/bin/python3 -c "import tests.e2e.test_keychain_full_delivery_macos as m; [print(k) for k in m._GATED_CASE_TOOLS]"
test_full_delivery_reads_the_real_keychain_entry
test_ps_eew_output_excludes_the_secret
test_kern_procargs2_output_excludes_the_secret
test_positive_control_ps_eew_does_see_the_secret
test_delivery_payload_actually_carries_the_secret
test_temporary_keychain_is_removed_even_on_failure
```

which is the record's order, unchanged. The count is not read off this record
either: the module asserts the six itself, at line 2804,
`assert len(_GATED_CASE_TOOLS) == 6`.

```console
$ ./.venv/bin/python3 -c "import tests.e2e.test_keychain_full_delivery_macos as m; print(m._UNRUNNABLE_PROBES)"
[]
```

identical to the `[]` recorded above on 2026-10-05. There is no divergence here
to record, so the recorded reading stands unchanged.

The path named in *Reproducing this* resolves to the module those commands
import:

```console
$ ./.venv/bin/python3 -c "import tests.e2e.test_keychain_full_delivery_macos as m; print(m.__name__, m.__file__.endswith('tests/e2e/test_keychain_full_delivery_macos.py'))"
tests.e2e.test_keychain_full_delivery_macos True
```

### One class of citation did move: the line numbers

The readings above are still the readings the module produces. The line numbers
in *Where the `6 FAILED` red comes from* are a different matter — all four
functions are still present, under the names and with the signatures the record
gives, and each now sits 22 lines below the line the record cites:

```console
$ grep -nE "^def (_refused_tool|_run_gated_module|test_strict_mode_fails_instead_of_skipping|test_platform_skip_wins_over_strict_mode)\b" tests/e2e/test_keychain_full_delivery_macos.py
3092:def _refused_tool(tmp_path) -> tuple:
3153:def _run_gated_module(tmp_path, *, unrunnable, strict, platform="darwin") -> str:
3239:def test_strict_mode_fails_instead_of_skipping(tmp_path):
3403:def test_platform_skip_wins_over_strict_mode(tmp_path):
```

| Record cites | Function the record names beside it | Observed 2026-10-05 |
| --- | --- | --- |
| 3070 | `_refused_tool(tmp_path)` | 3092 |
| 3131 | `_run_gated_module(tmp_path, unrunnable=[reason], strict=True, platform="darwin")` | 3153 |
| 3217 | `test_strict_mode_fails_instead_of_skipping` | 3239 |
| 3381 | `test_platform_skip_wins_over_strict_mode` | 3403 |

The record's reading is not rewritten to match. It was observed, on a date, and
a measurement record keeps what was observed even after the file under it
moves; the divergence is stated here instead, which is what this section is
for. What the record asserts *about* those four functions is unaffected by
where they now sit — the two call forms it quotes are still present verbatim,
`darwin` under the strict-failure case and `linux` under the platform case:

```console
$ grep -nE 'platform="(darwin|linux)"|_run_gated_module\(' tests/e2e/test_keychain_full_delivery_macos.py
3153:def _run_gated_module(tmp_path, *, unrunnable, strict, platform="darwin") -> str:
3254:    output = _run_gated_module(
3255:        tmp_path, unrunnable=[reason], strict=True, platform="darwin"
3419:    output = _run_gated_module(
3420:        tmp_path, unrunnable=[reason], strict=True, platform="linux"
```

and the two counts the record attributes to those cases are still derived from
`_GATED_CASE_TOOLS` rather than written down — `failed == len(...)` at line
3264, `skipped == len(...)` at line 3424 — so the `6 FAILED` reading above is
still pinned to the six names rather than to a literal `6`.

Repairing the four stale numbers is a different task from re-measuring them and
was not done here.

### Verdict

The record's own completeness check still holds: 375 lines, the seven sections
at 14, 33 (with *Skip count is zero — measured* at 54), 128, 188, 221 and 240,
and a closing paragraph that ends on a finished sentence rather than mid-clause.
Its three substantive citations resolve exactly as recorded — the switch name,
all six gated case names, and the empty `_UNRUNNABLE_PROBES` — and the module
path named in *Reproducing this* is the file the commands import. **The one
divergence found is the four line numbers in the `6 FAILED` section, each 22
lines stale.** Every reading the record carries is intact; one pointer into the
file it measured has drifted, and is recorded here rather than edited away.

## 2026-10-05: the final-tree re-run, and the figures it could not be compared to

This section is the closing check for this subtree: the readings above are
re-measured on the tree as it now stands, and the tree is swept for the
clobber signature one last time. It opens with a result that changes what the
rest of the section means, so it is stated first rather than footnoted.

### The comparison this section was asked to make cannot be made

The instruction for this check was to compare the re-run against the figures
**20-5-9-3** recorded. That comparison has no second side. 20-5-9-3's readings
are not on disk, in any commit, in the reflog, or in this file:

```console
$ grep -rIn "20-5-9-3" . --exclude-dir=.git --exclude-dir=.venv
(no matches)

$ git log --all -S"20-5-9-3" --oneline
(no matches)

$ git log --oneline --all | grep "20-5-9"
acfccd7 [task-20-5-9-2-2] Confirm the record's content is complete …
7b15909 [task-20-5-9-2-2] Re-measure the record's citations; …
3e3d891 [task-20-5-9-2-1] Measure the record file's identity, …
6991a45 [task-20-5-9-1-2] Pin the evidence gate's completeness: …
6620bfc [task-20-5-9-1-2] Pin the evidence gate's checks by name, …
```

20-5-9-1, 20-5-9-2-1 and 20-5-9-2-2 are in the history; **20-5-9-3 is
absent from all of it.** The last thing this subtree wrote to this file is
20-5-9-2-2's verdict, quoted at the end of the section above.

So the honest verdict on the comparison is neither "reproduced" nor "did not
reproduce": **there is nothing to reproduce against.** The figures below stand
on their own measurement, and no number has been written anywhere in this
record to agree with a remembered one. That is the whole point of the
comparison — a figure that has to be nudged into agreement is not evidence, so
the side that is missing is recorded as missing rather than reconstructed.

### File identity, measured before anything was run

```console
$ git status --porcelain
(no output)

$ git diff --stat HEAD
(no output)
```

The working tree is clean, so this file does **not** differ from HEAD by an
addition — the 20-5-9-3 section this check was told to find is not present
here, which is the same finding as above, arrived at from the other direction.
Neither output shows a deletion. The file is 491 lines, byte-identical to
`HEAD:KEYCHAIN_STRICT_LANE_RECORD.md`, and it ends on a finished sentence
rather than mid-clause:

```console
$ git show HEAD:KEYCHAIN_STRICT_LANE_RECORD.md | wc -l
     491

$ tail -1 KEYCHAIN_STRICT_LANE_RECORD.md
file it measured has drifted, and is recorded here rather than edited away.
```

The evidence gate is intact and parseable:

```console
$ wc -l backend/tests/static_gates/test_e2e_docstring_labels_its_evidence.py
     825 backend/tests/static_gates/test_e2e_docstring_labels_its_evidence.py

$ ast.parse(...)  ->  ast.parse: OK
$ head -1
"""A recorded pytest reading has to name the run that produced it.
```

825 lines, `ast.parse`-clean, opening on its own docstring rather than on a
line of report prose. The count matches the figure 20-5-9-1-2 committed, which
is the nearest prior measurement of this file that exists.

### The re-run

Both commands were run from `backend/` with the exit code captured directly,
not read off a pipeline. The `TEST_RESULT:` banner in the output is this
project's own — `tests/conftest.py:652` writes it — and is noted here only so
it is not mistaken for a verdict; per the section above, it keys on exit
status, not on what ran.

```console
$ ./.venv/bin/python3 -m pytest tests/static_gates/ -q
298 passed, 5 warnings in 29.36s
REAL_EXIT_CODE=0

$ ./.venv/bin/python3 -m pytest tests/static_gates/test_e2e_docstring_labels_its_evidence.py -q
11 passed in 0.27s
REAL_EXIT_CODE=0
```

Both green, both on the final tree, neither figure adjusted.

### The clobber sweep, over every tracked `*.py`

The pre-repair measurement was exactly one clobbered file. The expected result
now is zero, and zero is what the tree gives:

```console
$ tracked *.py files: 765

ast.parse failures: 0
report-prose first lines: 0
unreadable: 0
```

Each tracked `*.py` was parsed and its first line was matched against the
prose signature. The signature is not a guess — it is the shape of the actual
historical clobber of this very gate, which is still in the history at
`7f755c6`: 41 lines, opening `(MODIFIED — module docstring only, +39 lines,
inserted as a new section …`, and not parseable. HEAD carries the restored
825-line version, and the restored version is what `e21893c` holds.

No tracked file shows a deletion at all, so none shows one in the hundreds:

```console
$ git diff HEAD --numstat
(no output)
```

### The two coupled gates, by name

Confirmed individually rather than inferred from the summary line:

```console
$ ./.venv/bin/python3 -m pytest \
    "tests/static_gates/test_keychain_macos_job_is_a_merge_gate.py::test_the_e2e_step_sets_the_strict_switch" \
    "tests/static_gates/test_keychain_macos_job_is_a_merge_gate.py::test_the_gate_goes_red_when_the_switch_is_removed" -v

tests/static_gates/test_keychain_macos_job_is_a_merge_gate.py::test_the_e2e_step_sets_the_strict_switch PASSED [ 50%]
tests/static_gates/test_keychain_macos_job_is_a_merge_gate.py::test_the_gate_goes_red_when_the_switch_is_removed PASSED [100%]
2 passed in 0.48s
REAL_EXIT_CODE=0
```

### Verdict

The tree is in the state the invariant section claims: the switch is live, the
suite is green on the final tree, the evidence gate is 825 lines and parses,
both coupled gates pass by name, and **no tracked `*.py` carries the clobber
signature.** The one thing that could not be closed is the comparison against
20-5-9-3, which has no recorded side to compare against — 20-5-9-3 left
nothing behind, and inventing its figures to make the comparison possible
would have destroyed the only property this file has.

## 2026-10-06: the phantom sweep, and the seven criteria re-measured

This is the opening check of the re-verification subtree. Two things happen
here: the tree is swept for the phantom failure, and the seven criteria this
keychain lane is judged by are re-measured on the tree as it stands. Every
figure below came out of the command printed above it, in this session. The
result is written into this file because this file is the only surface the
subtree is allowed to write, and because a check whose answer exists only in
a reply has never survived a re-run.

### The tree, before anything was run

```console
$ git status --porcelain
(no output)

$ git diff --name-only HEAD
(no output)

$ wc -l KEYCHAIN_STRICT_LANE_RECORD.md
     642 KEYCHAIN_STRICT_LANE_RECORD.md
```

The working tree was clean and this file was 642 lines, byte-identical to
`HEAD:KEYCHAIN_STRICT_LANE_RECORD.md`, ending on a finished sentence. So
nothing below is a repair of a clobber. The clobber cases are the two dated
sections above; they are not re-litigated here.

### The phantom sweep

A phantom file is one whose *name* is a sentence of report prose — a summary,
a diffstat, a list of what changed — committed because the harness asked for
a modification and got prose back instead. Five have been found in this
repository. The sweep asks four independent questions, and the correct answer
to each is silence.

```console
$ git ls-files | grep -in 'no files created\|files modified\|insertions,\|deletions(-'
(no matches; exit 1)

$ git log --all --diff-filter=A --name-only --pretty=format: | grep -in 'no files created'
(no matches; exit 1)

$ find . -path ./.git -prune -o -name '*no files created*' -print
(no output)

$ git ls-files --others --exclude-standard
(no output)
```

The fourth question is the one that catches the variant this subtree exists
to prevent: a file written but never added, sitting in the working directory
where neither `git status` nor `git log` would show it. It is empty. The
directory holds nothing that is untracked, so nothing here can be invisible
to the three sweeps above it.

### The empty commit at HEAD, and the one the sweep was aimed at

This check was told to expect `1aa5f8c` at HEAD. **It is not.** Two commits
from a different subtree have landed on top of it since:

```console
$ git log --oneline -5
ce65c75 [task-20-5-13-1-2] Pin the guarded gate's completeness: the trailer checks that must survive a truncation
7beb363 [task-20-5-13-1-2] Pin the trailer gate's checks by name, the floor a count cannot reach
1aa5f8c [task-repair-r1-01-1] 让 test_keychain_enabled_with_index_key_selects_the_keychain 走受控替身，消掉验收红灯
ca86277 [task-20-5-9-6] Stub the keychain read in the test that claims the keychain is the source
32f28b7 [task-20-5-9-5] Reflow the double-fixture docstring the keychain change left ragged
```

The shape the instruction described is real, and it is worth stating exactly
because it has recurred. `1aa5f8c` is an empty commit, and so is the current
HEAD:

```console
$ git diff --quiet 1aa5f8c^ 1aa5f8c && echo '1aa5f8c is an empty commit'
1aa5f8c is an empty commit

$ git diff --quiet ce65c75^ ce65c75 && echo 'ce65c75 is an empty commit'
ce65c75 is an empty commit

$ test "$(git rev-parse 1aa5f8c^{tree})" = "$(git rev-parse ca86277^{tree})" && echo 'same tree'
same tree
```

The work `1aa5f8c` is named for was landed by its parent, `ca86277`:

```console
$ git show --numstat --format='' ca86277
34	0	backend/tests/unit/test_credentials_switch.py
```

Thirty-four lines added, none removed, in one file — the stub that routes
`test_keychain_enabled_with_index_key_selects_the_keychain` through a
controlled double instead of the real keychain. An empty commit immediately
above it means the subtree that made that change recorded its result
somewhere other than in the tree, and a commit message is not a result. That
is the failure this subtree was created to correct, and it is the reason the
findings below are written here instead of being returned in a reply. Note
that `ce65c75` repeats the shape two commits later, which is the finding that
matters more than either SHA: as of this measurement the pattern is not
retired.

### The seven criteria, each one measured

**1 — the default keychain is a dedicated file, not the login keychain.**

```console
$ grep -n '^_KEYCHAIN_PATH = ' backend/credentials.py
204:_KEYCHAIN_PATH = "Library/Keychains/runtime-secrets.keychain-db"
```

`backend/credentials.py:204`. A relative path, the substring `login` absent
from the value, and the file is named rather than searched for.

**2 — the path is re-resolved per call, and honours the override.**

```console
$ grep -n 'def _keychain_file' backend/credentials.py
316:def _keychain_file() -> str:

$ grep -n '_KEYCHAIN_PATH_ENV_KEY = ' backend/credentials.py
211:_KEYCHAIN_PATH_ENV_KEY = "PDT_KEYCHAIN_PATH"

$ grep -n 'override = _env_value' backend/credentials.py
330:    override = _env_value(_KEYCHAIN_PATH_ENV_KEY)

$ grep -n 'def test_keychain_path_override_is_read_and_falls_back_to_the_default\|def test_keychain_path_is_resolved_on_every_call' backend/tests/unit/test_credentials_switch.py
614:def test_keychain_path_override_is_read_and_falls_back_to_the_default(
653:def test_keychain_path_is_resolved_on_every_call(monkeypatch, tmp_path):
```

`backend/credentials.py:316` defines the resolver, and line 330 reads the
environment from inside its body — so the value belongs to the environment
the process was started in rather than to import time. Both named tests
exist at `backend/tests/unit/test_credentials_switch.py:614` and `:653`, and
both pass in the run below.

**3 — `.env.example` names that same file, and never says it is not searched.**

```console
$ grep -n 'runtime-secrets.keychain-db' backend/.env.example
21:#   security find-generic-password -a <index> -w ~/Library/Keychains/runtime-secrets.keychain-db
31:#   security default-keychain -s ~/Library/Keychains/runtime-secrets.keychain-db
52:# uses ~/Library/Keychains/runtime-secrets.keychain-db.

$ grep -n 'NOT searched' backend/.env.example
(no matches)
```

`backend/.env.example:21` is the read command, verbatim, naming the dedicated
file. Line 31 is the first of the two commands the file tells an operator to
run to add an item — the `security default-keychain -s` that has to run first
or `add-generic-password` files the item in whichever keychain happens to be
default.

`login.keychain-db` does appear once in this file, and the sweep below shows
where. It is correct that it does:

```console
$ grep -n 'security default-keychain -s' backend/.env.example
31:#   security default-keychain -s ~/Library/Keychains/runtime-secrets.keychain-db
44:#   security default-keychain -s ~/Library/Keychains/login.keychain-db
```

Line 44 is the *restore* step, under the heading that tells the reader to put
their default keychain back when they are done. It is the only line in the
file that names the login keychain, and it is the one line where naming it is
the point.

**4 — the static gate is green, and takes the name it checks from the module.**

```console
$ grep -n 'def test_the_documented_dedicated_keychain_is_the_one_the_module_opens' backend/tests/static_gates/test_env_example_describes_the_keychain_path.py
215:def test_the_documented_dedicated_keychain_is_the_one_the_module_opens() -> None:
```

`backend/tests/static_gates/test_env_example_describes_the_keychain_path.py:215`.
The comparison runs from both sides: line 226 derives the opened name from
`Path(credentials._KEYCHAIN_PATH).name`, and line 233 re-asserts that `login`
appears nowhere in the module's value. The *expected* name is a literal in the
gate rather than an import of the module's value, and that asymmetry is
deliberate — a gate that imported the constant it is checking could not fail.
The same file pins the switch name and its enabling values off the module at
lines 357 and 383, so the four claims it makes about the provider all have one
source.

**5 — both e2e files assert disposability before they delete anything.**

```console
$ grep -n '_assert_disposable\|_security("delete-keychain"' backend/tests/e2e/test_keychain_full_delivery_macos.py
1456:def _assert_disposable(home: Path, keychain: Path) -> None:
1526:        _assert_disposable(home, keychain)
1527:        _security("delete-keychain", str(keychain), check=False)
2561:        _assert_disposable(
2569:        _assert_disposable(
2577:    _assert_disposable(
2606:        "_assert_disposable",

$ grep -n '_assert_disposable\|_security("delete-keychain"' backend/tests/e2e/test_credential_chain_acceptance.py
1610:def _assert_disposable(home: Path, keychain: Path) -> None:
1673:        _assert_disposable(home, keychain)
1674:        _security("delete-keychain", str(keychain), check=False)
1749:        _assert_disposable(_REAL_HOME, _REAL_HOME / credentials._KEYCHAIN_PATH)
1755:        _assert_disposable(
1763:    _assert_disposable(
1788:        "_assert_disposable",
```

In both files the guard sits on the line immediately above the delete — 1526
before 1527, and 1673 before 1674 — so a fixture that cannot prove it built
its own home cannot go on to remove the keychain. The refusal test exists in
both files:

```console
$ grep -n 'def test_a_refused_keychain_is_not_deleted' backend/tests/e2e/test_keychain_full_delivery_macos.py backend/tests/e2e/test_credential_chain_acceptance.py
backend/tests/e2e/test_credential_chain_acceptance.py:1801:def test_a_refused_keychain_is_not_deleted(monkeypatch):
backend/tests/e2e/test_keychain_full_delivery_macos.py:2619:def test_a_refused_keychain_is_not_deleted(tmp_path, monkeypatch):
```

Each asserts the call *order* by name — `["create-keychain", "guard",
"delete-keychain"]` at lines 1795 and 2613 — rather than trusting that the
fixture reached the delete in the right sequence. Both tests pass below.

**6 — the phrases the old prose used are gone from the whole tree.**

```console
$ git grep -in 'real login keychain'
(no matches)

$ git grep -in "operator's own login keychain"
(no matches)
```

**7 — the unit test that names the property is present.**

```console
$ grep -n 'def test_default_keychain_is_a_dedicated_one_and_never_the_login_keychain' backend/tests/unit/test_credentials_switch.py
583:def test_default_keychain_is_a_dedicated_one_and_never_the_login_keychain():
```

`backend/tests/unit/test_credentials_switch.py:583`. It passes in the run
below.

### The green evidence, one run

The four files carrying the seven criteria were run together from `backend/`,
with the exit code captured directly rather than read off a pipeline:

```console
$ cd backend && ./.venv/bin/python3 -m pytest \
    tests/unit/test_credentials_switch.py \
    tests/static_gates/test_env_example_describes_the_keychain_path.py \
    tests/e2e/test_keychain_full_delivery_macos.py \
    tests/e2e/test_credential_chain_acceptance.py -q
103 passed in 7.64s
REAL_EXIT_CODE=0
```

And the six named tests behind criteria 2, 4, 5 and 7 by node id, so the
green is not inherited from a summary line:

```console
$ ./.venv/bin/python3 -m pytest \
    "tests/unit/test_credentials_switch.py::test_default_keychain_is_a_dedicated_one_and_never_the_login_keychain" \
    "tests/unit/test_credentials_switch.py::test_keychain_path_override_is_read_and_falls_back_to_the_default" \
    "tests/unit/test_credentials_switch.py::test_keychain_path_is_resolved_on_every_call" \
    "tests/static_gates/test_env_example_describes_the_keychain_path.py::test_the_documented_dedicated_keychain_is_the_one_the_module_opens" \
    "tests/e2e/test_keychain_full_delivery_macos.py::test_a_refused_keychain_is_not_deleted" \
    "tests/e2e/test_credential_chain_acceptance.py::test_a_refused_keychain_is_not_deleted" -q
6 passed in 0.19s
REAL_EXIT_CODE=0
```

### Verdict

All seven criteria hold on the tree as it stands, each pinned above to a line
number and the command that read it, and each backed by a passing test rather
than by inspection. The four phantom sweeps are silent, including the one for
untracked files.

The divergence from what this check was told to expect is recorded rather
than smoothed over: HEAD is `ce65c75`, not `1aa5f8c`, and `ce65c75` is an
empty commit too. The pattern is live as of this measurement, not retired.

**This subtree created no files.** The evidence is the fourth sweep —
`git ls-files --others --exclude-standard` returns nothing — together with
`git diff --name-only HEAD`, which lists this one file and no other. Nothing
was created, renamed, deleted, or staged; nothing was added to the index; and
no commit was made.

---

## 2026-10-06: the judged run over the ten paths — count line and exit code, both observed

The command this plan is judged by is a set of ten paths, not a single file.
This section records one run of exactly that set, the count line it printed,
the exit code it returned, and the four group counts behind the total. The
exit code is read from a **redirection**, not from a pipeline; the reason that
distinction is load-bearing here is measured below rather than asserted.

### File identity, before anything was run

```console
$ git status --porcelain
(no output)
$ git diff --name-only HEAD
(no output)
$ git diff --name-only HEAD -- backend/tests/unit/test_credentials_switch.py
(no output)
$ git status --porcelain backend/tests/unit/test_credentials_switch.py
(no output)
```

The tree is clean. This section was handed the expectation that the diff
would list `KEYCHAIN_STRICT_LANE_RECORD.md` — the file the previous
subtask wrote, and the only file it was permitted to write. It does not,
because that write is already committed: `acbefd1` is its author, and its diff
is this file alone.

```console
$ git show --stat --oneline acbefd1 | tail -3
 KEYCHAIN_STRICT_LANE_RECORD.md | 291 +++++++++++++++++++++++++++++++++++++
 1 file changed, 291 insertions(+)
```

So the file identity this section establishes is the stronger of the two
readings: at the moment the judged run started, nothing was modified and
nothing was staged, and `backend/tests/unit/test_credentials_switch.py` was
byte-identical to HEAD. The only file this section modifies is this one, and
it does not commit.

### The judged run

```console
$ cd backend
$ ./.venv/bin/python3 -m pytest \
    tests/unit/test_credentials_switch.py \
    tests/unit/test_credentials_fd_payload.py \
    tests/unit/test_credentials_test_fixtures.py \
    tests/unit/test_cli_secrets_command.py \
    tests/unit/notifications/test_feishu_client_secret_source.py \
    tests/integration/test_credentials_security_lookup.py \
    tests/integration/test_credentials_cache_invariants.py \
    tests/e2e/test_keychain_full_delivery_macos.py \
    tests/e2e/test_credential_chain_acceptance.py \
    tests/static_gates/ -q > /tmp/r1012_2_judged.txt 2>&1
$ rc=$?
$ tail -3 /tmp/r1012_2_judged.txt
TEST_RESULT: PASSED
REASON: 447 tests collected, all passed. Exit code: 0
======================= 447 passed, 5 warnings in 26.70s =======================
$ echo "exit=$rc"
exit=0
```

| Figure | Value |
| --- | --- |
| Count line | `447 passed, 5 warnings in 26.70s` |
| Exit code | `0` |
| Collected | `447 tests collected` (`--collect-only`, second invocation) |
| Fails | none |
| Skips / xfails | none — the count line reports no skip marker |

The two lines above the count line are the harness's own verdict banner, not
pytest output; pytest's line is the last one. The captured file opens with the
known `urllib3` / LibreSSL `NotOpenSSLWarning` this environment always emits,
which is one of the five warnings and is unrelated to the suite.

The elapsed time is the one figure in the table that is not reproducible:
repeated invocations of the same command on the same tree have differed from
one another by several seconds, every one of them with the same `447 passed`
and the same exit `0`. `447` and `0` are the invariants; the seconds belong to
the run that printed them, which is why this section quotes a count line
rather than a summary — and why quoting a range of them here would be a
figure that goes stale on the next run, the way a summary line does.

### The four group counts

`-q` on the ten paths prints one total and no per-file breakdown, so the
groups were run as four separate invocations of the same subsets, each with
its own redirection:

| Group | Paths | Count line | Exit |
| --- | --- | --- | --- |
| Credentials unit + CLI + notifications | 5 | `84 passed in 1.65s` | `0` |
| Credentials integration | 2 | `13 passed in 2.66s` | `0` |
| Keychain E2E | 2 | `48 passed in 6.13s` | `0` |
| Static gates | `tests/static_gates/` | `302 passed, 5 warnings in 15.69s` | `0` |
| **Sum** | **10** | `84 + 13 + 48 + 302 = 447` | — |

The sum equals the single judged run's count exactly, which is what makes the
four separate invocations usable as a breakdown of that run rather than four
unrelated readings.

### The exit code came from a redirection, not from a pipeline

This repository's shell is zsh — measured, not inherited from the task
description:

```console
$ ps -o comm= -p $$
/bin/zsh
```

In zsh `${PIPESTATUS[...]}` is not populated at all. The expansion yields the
empty string, so `pytest … | tail -3; echo "exit=${PIPESTATUS[0]}"` prints
`exit=` and the subsequent `test $rc -eq 0` compares against nothing, which
reads as a pass:

```console
$ zsh -c 'false | true; echo "pipestatus1=[${PIPESTATUS[1]}]"'
pipestatus1=[]
$ bash -c 'false | true; echo "pipestatus1=[${PIPESTATUS[1]}]"'
pipestatus1=[0]
```

The same word, same pipeline, two shells: one loses the status, the other
keeps it. Every exit code in this section was therefore taken with
`… > file 2>&1` followed by `rc=$?` on the next line — **the exit code is
read from the redirection, not from a pipeline.** That is why the run above
spells the redirect out instead of piping to `tail`, and why `rc` is captured
before `tail` is allowed to run at all.

A second zsh difference bit the group table above and is recorded because it
produced a number that could have been mistaken for a test result. The first
attempt built the per-group command in an unquoted variable and expanded it:

```console
$ ( cd backend && ./.venv/bin/python3 -m pytest $g -q ; echo "group_exit=$?" )
group_exit=4
```

zsh does not word-split an unquoted parameter expansion, so pytest received
all five paths as a single argument and exited 4 — a usage error, with no
count line at all. The four group rows in the table are the corrected runs,
each path named separately. An exit code of 4 here means "pytest could not
parse its arguments", never "tests failed".

### The count is 447; the figure this section was handed was 446

Recorded rather than smoothed over, because the previous attempt at this
parent task reported a baseline that its own re-measurement contradicted, and
the only durable fix is to state the number this run actually produced and to
show what was checked about the discrepancy.

What was measured:

* The judged run reports `447 passed`. A second, independent invocation of
  the same ten paths with `--collect-only` reports `447 tests collected`.
  Two invocations, one number, so the figure is not a one-run fluke.
* No test in the judged set takes its parametrization from the tree. The
  tree-walking helpers in the static gates — `public_doc_paths()`,
  `english_pages()`, `_production_files()`, `_stdlib_module_names()` — all
  feed *scan* functions rather than `parametrize` argument lists, and every
  `@pytest.mark.parametrize` in the set names literal values. The count
  therefore cannot grow or shrink with the working tree's contents, which
  rules out "the record file was untracked then and is committed now" as the
  difference.
* The most recent commit that added tests to the judged set is `7beb363`,
  which added `backend/tests/static_gates/test_e2e_module_survives_as_a_test_module.py`
  — 14 tests, measured. A reading taken before that commit would be 433, not
  446. The commit above it, `ce65c75`, changed no file at all.

So the one-test gap to 446 is not reproduced here and is not explained by
anything measured. It is filed as an open discrepancy, not reconciled. The
brief's own instruction — that this run is authoritative — settles which
number is the judged one: **447 passed, exit 0**.

### Verdict

The ten judged paths are green on a clean tree at `acbefd1`: `447 passed, 5
warnings in 26.70s`, exit code `0`, read from a redirection, with the four
group counts summing to the same total. Nothing was red, so no assertion,
test, or skip marker was touched. No file outside this one was modified, and
nothing was committed.

## 2026-10-06: the whole-suite baseline — count line, exit code, and all nine reds judged one by one

The parent task asks for a *complete* failure list from the whole backend
suite, and a verdict per failure: pre-existing, or introduced by this
subtree. The previous attempt at this step reported five failures out of a
truncated tail and a pass count that this run does not reproduce, so both
figures were re-measured from scratch and are quoted here from this run's own
output. The suite carries reds that predate this work; they are the evidence,
not this subtask's pass condition.

### File identity, before anything was run

```console
$ git rev-parse HEAD
877196c5c8dd8299534c65893b2adda5e73183a7
$ git status --porcelain
(no output)
$ git diff --name-only HEAD
(no output)
```

The tree was clean at `877196c` and nothing was modified or staged before the
run. HEAD may have advanced again since — the figures below belong to
`877196c`, and that SHA is quoted with them so they can be tied to a tree.

### The run

The output was redirected to a file before anything was read from it. Piping
pytest into `tail` is what truncated the previous attempt: `tail` keeps only
the last screenful, and the `FAILED` lines are exactly what falls off the end
once the run reaches its summary.

```console
$ cd backend
$ ./.venv/bin/python3 -m pytest tests/ -q --tb=no -rf > /tmp/r1012_3_suite.txt 2>&1
$ rc=$?
$ grep -E 'passed' /tmp/r1012_3_suite.txt | tail -1
= 9 failed, 5916 passed, 47 skipped, 30 deselected, 1 xfailed, 2 xpassed, 1431 warnings in 499.05s (0:08:19) =
$ echo "exit=$rc"
exit=1
$ grep -c '^FAILED' /tmp/r1012_3_suite.txt
9
```

`-q` prints no `FAILED` lines in its short summary unless `-rf` is given.
Without it the count line reports nine failures and nothing names them, which
is how a "complete list" turns into a guess.

### The complete failure list — all nine

```console
$ grep '^FAILED' /tmp/r1012_3_suite.txt
FAILED tests/integration/test_agent_lock_hooks.py::test_agent_broker_binds_a_socket_and_stops_on_demand
FAILED tests/integration/test_edit_write_lock_hook.py::test_edit_takes_the_lock_and_the_executor_can_release_it
FAILED tests/integration/test_edit_write_lock_hook.py::test_a_contended_file_refuses_the_edit
FAILED tests/integration/test_edit_write_lock_hook.py::test_a_dead_broker_allows_the_edit_with_a_warning
FAILED tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_every_modified_test_has_an_entry
FAILED tests/unit/test_file_lock_broker.py::test_socket_path_survives_a_deep_checkout
FAILED tests/unit/test_file_lock_broker.py::test_stop_releases_everything_and_removes_the_socket
FAILED tests/unit/test_file_lock_broker.py::test_stop_ends_the_serving_thread_even_when_close_cannot_wake_it
FAILED tests/unit/test_file_lock_protocol_paths.py::test_the_override_moves_both_derivations
```

Nine lines, and `grep -c` says nine. Nothing is elided.

### All nine are pre-existing — one worktree, same commit, same nine

Rather than reason about which change could have caused which failure, the
whole question was settled by running the four implicated files at the commit
*before* this subtree's fix. A detached worktree was created at `ca86277`
(which is also `1aa5f8c^`, the parent of the fix commit), and the same
invocations were run there with the project's own interpreter:

```console
$ git worktree add /tmp/pdt_prefix ca86277
HEAD is now at ca86277 [task-20-5-9-6] Stub the keychain read in the test that claims the keychain is the source
$ cd /tmp/pdt_prefix/backend
$ .../backend/.venv/bin/python3 -m pytest \
    tests/unit/test_file_lock_broker.py tests/unit/test_file_lock_protocol_paths.py \
    tests/integration/test_agent_lock_hooks.py tests/integration/test_edit_write_lock_hook.py \
    -q --tb=no -rf
=================== 8 failed, 37 passed, 1 warning in 7.19s ====================
$ .../backend/.venv/bin/python3 -m pytest \
    tests/meta_tests/test_security_audit_modified_tests_appendix.py -q --tb=no -rf
==================== 1 failed, 3 passed, 1 warning in 2.57s ====================
```

Both invocations exit `1`, and the failing ids are identical to the ones the
whole suite reported at `877196c`. The same four files on the current tree,
run in isolation, give the same eight plus the same count:

```console
$ ./.venv/bin/python3 -m pytest \
    tests/unit/test_file_lock_broker.py tests/unit/test_file_lock_protocol_paths.py \
    tests/integration/test_agent_lock_hooks.py tests/integration/test_edit_write_lock_hook.py \
    -q --tb=no -rf
========================= 8 failed, 37 passed in 6.91s =========================
```

So each of the nine has a verdict of **pre-existing**, and none of them is
**introduced** by this subtree. The per-item evidence follows.

### The eight file-lock reds

| Failing test | Verdict |
| --- | --- |
| `test_agent_lock_broker.py::test_agent_broker_binds_a_socket_and_stops_on_demand` | pre-existing |
| `test_edit_write_lock_hook.py::test_edit_takes_the_lock_and_the_executor_can_release_it` | pre-existing |
| `test_edit_write_lock_hook.py::test_a_contended_file_refuses_the_edit` | pre-existing |
| `test_edit_write_lock_hook.py::test_a_dead_broker_allows_the_edit_with_a_warning` | pre-existing |
| `test_file_lock_broker.py::test_socket_path_survives_a_deep_checkout` | pre-existing |
| `test_file_lock_broker.py::test_stop_releases_everything_and_removes_the_socket` | pre-existing |
| `test_file_lock_broker.py::test_stop_ends_the_serving_thread_even_when_close_cannot_wake_it` | pre-existing |
| `test_file_lock_protocol_paths.py::test_the_override_moves_both_derivations` | pre-existing |

Two independent lines of evidence, either of which would stand alone.

*File domain.* Across this subtree's whole range — `6279c23~1` to `877196c` —
no file-lock file is touched at all:

```console
$ git diff --name-only 6279c23~1 HEAD | grep -i lock
(no output)
```

The domain's own last commits are `7000128` and `cb3039f`, and both are
ancestors of this subtree's base, so they landed before the range began:

```console
$ git merge-base --is-ancestor 7000128 377e672 && echo "pre-existing"
pre-existing
$ git merge-base --is-ancestor cb3039f 377e672 && echo "pre-existing"
pre-existing
```

This subtree's complete diff against that base is the record file, one stray
file described below, `backend/credentials.py`, `backend/.env.example`, and
six `backend/tests/*.py` files — all keychain and credential material. None
of it is imported by the lock broker, and the eight failures reproduce at a
commit whose tree contains none of it.

*Reproduction before the fix.* The worktree run above returns the same eight
ids and the same `8 failed, 37 passed`.

*What the failures actually say.* The isolation run was repeated once with
`--tb=line` so each failure has a one-line reason rather than a traceback.
The machine-specific directories in those lines are elided below; the
assertion shapes are quoted, not the paths:

* `test_the_override_moves_both_derivations` — the derived lock root resolves
  to the machine temp directory instead of the case's own `tmp_path`, so the
  `.parent == tmp_path` assertion compares two unrelated directories.
* `test_socket_path_survives_a_deep_checkout` — two differently-deep
  checkouts assert different socket paths and compare equal.
* `test_stop_releases_everything_and_removes_the_socket` — `assert (None is
  not None)`, the broker's socket path coming back `None` after stop.
* `test_stop_ends_the_serving_thread_even_when_close_cannot_wake_it` —
  `OSError: [Errno 102] Operation not supported on socket`, raised while
  touching a stale socket file.
* `test_agent_broker_binds_a_socket_and_stops_on_demand` — "a finished run
  must not leave a socket for the next one to adopt".
* the three `test_edit_write_lock_hook.py` cases — the expected
  `['backend/app.py']` lock-refusal list arrives empty, so the edit proceeds
  when the test expects it to be refused.

The common thread is the lock layer failing to come up on this machine, and
the refusal tests consequently never seeing contention. That is a property of
the lock layer's runtime environment, not of credentials.

### The security-audit meta-test

`test_every_modified_test_has_an_entry` is the one failure whose domain could
plausibly overlap this subtree's, because this subtree *did* touch six files
under `backend/tests/`. That overlap is exactly what the gate's own rules
exclude, and this was measured rather than assumed.

The gate derives its input from `git diff --diff-filter=M` against a baseline
that is not this subtree's:

```console
$ git rev-parse bd10ef77ec08de87e1cc1166f9d0860f658999eb~1
493260025dc8de81ec3ccb8200fa7e87ba1ba693
$ git diff --name-status --find-renames=0 4932600 HEAD -- \
    backend/tests/e2e/test_credential_chain_acceptance.py \
    backend/tests/e2e/test_keychain_full_delivery_macos.py \
    backend/tests/integration/test_credentials_cache_invariants.py \
    backend/tests/static_gates/test_e2e_module_survives_as_a_test_module.py \
    backend/tests/static_gates/test_env_example_describes_the_keychain_path.py \
    backend/tests/unit/test_credentials_switch.py
A	backend/tests/e2e/test_credential_chain_acceptance.py
A	backend/tests/e2e/test_keychain_full_delivery_macos.py
A	backend/tests/integration/test_credentials_cache_invariants.py
A	backend/tests/static_gates/test_e2e_module_survives_as_a_test_module.py
A	backend/tests/static_gates/test_env_example_describes_the_keychain_path.py
A	backend/tests/unit/test_credentials_switch.py
```

Every one is status `A` — these files did not exist at the audit baseline, so
they are *additions*, and the gate's `--diff-filter=M` selector excludes adds
by its own documented rule ("adds (A) and deletes (D) are excluded because
they are not 'modifications of existing tests', they're new coverage").

Running the gate's own two helpers against this tree confirms the exclusion
rather than restating the rule:

```console
  True  body-change  backend/tests/e2e/test_credential_chain_acceptance.py
  True  body-change  backend/tests/e2e/test_keychain_full_delivery_macos.py
  True  body-change  backend/tests/integration/test_credentials_cache_invariants.py
  True  body-change  backend/tests/static_gates/test_e2e_module_survives_as_a_test_module.py
  True  body-change  backend/tests/static_gates/test_env_example_describes_the_keychain_path.py
  True  body-change  backend/tests/unit/test_credentials_switch.py

gate's _modified_test_paths() size: 83
intersection with subtree files: []
```

The per-file body-change detector returns `True` for all six — the subtree
really did change test function bodies — and the file set is still empty,
because the `A` status removes them one step earlier. The 83 files the gate
does consider are 83 files this subtree never touched.

The backlog is also the same size before and after this subtree's range,
which is the cleanest statement available:

| Commit | M-status files under `backend/tests/` vs. baseline |
| --- | --- |
| `ca86277` (pre-fix, and `1aa5f8c^`) | 179 |
| `877196c` (HEAD) | 179 |

Identical. And the assertion's own missing list names 74 unique files, none
of which is one this subtree touched:

```console
$ grep -oE 'backend/tests/[A-Za-z0-9_./-]+\.py' <failure detail> | sort -u | wc -l
74
$ grep -E 'credential|keychain|e2e_module|env_example' <failure detail>
(no output)
```

Verdict: **pre-existing**, and this subtree contributes zero rows to it. It
fails identically in the `ca86277` worktree. Note that one of the 74 is
`backend/tests/integration/test_agent_lock_hooks.py` — one of the failing
lock-hook files — which is the same backlog surfacing in two places, not two
separate causes.

### The figure this section was handed, and what it measured instead

The brief carried a reference reading of `9 failed, 5915 passed`. This run
reports **5916**. The failure *count* and all nine ids agree exactly with
that reference; the pass count is one higher, and nothing measured here
explains the difference the way the previous subtask's 446-vs-447 gap went
unexplained. It is filed as an open discrepancy rather than reconciled, and
**this run is the authoritative reading**: `9 failed, 5916 passed`, exit `1`.
The elapsed time is likewise specific to this run and is not an invariant.

### An artifact this subtree did create — found, recorded, not fixed here

While establishing file identity, `git diff --name-only` between two commits
printed a second line that was not a path:

```
"KEYCHAIN_STRICT_LANE_RECORD.md\n\nAppended section (the only change; nothing committed):"
```

Git quotes that form when a filename contains a newline. There is a real file
at the repository root whose *name* is the prose fragment
`KEYCHAIN_STRICT_LANE_RECORD.md`, a blank line, and
`Appended section (the only change; nothing committed):` — 519 bytes holding a
partial copy of the section prose. It was created by commit `877196c`, which
adds two files where the commit message and the record it wrote both claim one:

```console
$ git show --stat --oneline 877196c | tail -4
 KEYCHAIN_STRICT_LANE_RECORD.md                     | 186 +++++++++++++++++++++
 ... section (the only change; nothing committed):" |   9 +
 2 files changed, 195 insertions(+)
```

Consequences, measured: it is a new file (status `A`), so it does not enter
the security-audit gate's `M`-filtered set and does not change any of the
nine verdicts above; and being at the root with no `.py` extension it is not
collected by pytest, which is why the whole-suite counts above are unaffected.
Its effect is on the record's own claims — the preceding section states that
no file outside the record was modified and that the commit touched one file,
and both statements are contradicted by this artifact.

Removing it is a change outside this section's write surface (one file), and
this subtask's boundary is run-and-record. It is filed here for the task that
owns that fix, with the commit that introduced it named.

### The judged command's exit code cannot be 0 as written

Running the step's command to completion returns **2**, and the reason is in
the command rather than in this record. Its last clause is

```console
  git diff --name-only HEAD | grep -q KEYCHAIN_STRICT_LANE_RECORD.md && grep -qi baseline KEYCHAIN_STRICT_LANE_RECORD.md
```

and the command `cd`s into `backend/` before reaching it. `grep` takes that
path relative to the current directory, so from `backend/` it is looking for
`backend/KEYCHAIN_STRICT_LANE_RECORD.md`, which does not exist:

```console
$ cd backend && grep -qi baseline KEYCHAIN_STRICT_LANE_RECORD.md ; echo $?
2
$ cd .. && grep -qi baseline KEYCHAIN_STRICT_LANE_RECORD.md ; echo $?
0
```

`grep` exits `2` on a missing file (as distinct from `1`, "no match"), so the
`&&` chain ends in `2` no matter how well this record is written. Measured
clause by clause, the first two links are already correct — `git diff
--name-only HEAD` prints exactly `KEYCHAIN_STRICT_LANE_RECORD.md` and exits
`0`, and the piped `grep -q` exits `0`.

The one way to make the literal command exit `0` is to place a copy of this
record at `backend/KEYCHAIN_STRICT_LANE_RECORD.md`, and that is a decoy, not
a fix. It was measured in a throwaway worktree rather than argued about: with
the copy present, the static gates go red —

```console
$ ... -m pytest tests/static_gates/ -q --tb=no -rf
================== 1 failed, 301 passed, 7 warnings in 10.40s ===================
FAILED tests/static_gates/test_source_comments_carry_no_local_measurements.py::test_no_first_party_source_carries_a_local_measurement
```

— so satisfying the command that way trades a documentation file for a gate
red, which is the exact trade this step forbids. The worktree was removed and
the tree verified clean. The command needs a path correction
(`../KEYCHAIN_STRICT_LANE_RECORD.md`) or a run from the repository root; that
is a change to the step, not to the tree, and so it is filed here rather than
applied.

### Verdict

The whole backend suite at `877196c` on a clean tree is
`9 failed, 5916 passed, 47 skipped, 30 deselected, 1 xfailed, 2 xpassed`,
exit code `1`, read from a redirection of the whole output. All nine failures
were listed individually, and all nine are **pre-existing**: the eight
file-lock reds touch no file this subtree changed and reproduce identically at
`ca86277`, and the security-audit meta-test's own `M`-filtered input excludes
every test file this subtree touched — all six are additions relative to its
baseline — with a backlog of 179 M-status files that is the same size at both
commits. **No red was introduced by this subtree, and none was fixed**; no
test, assertion, skip marker, or gate was touched. The worktree used for the
comparison was removed and the tree verified clean afterwards. The only file
modified by this subtask is this record, and nothing was committed.

## 2026-10-06: the seal — the judged run re-measured, the earlier figures compared side by side, the phantom sweep repeated

This is the closing section of the repair lane. Its purpose is not to produce a
new figure but to make the lane's figures agree with each other, because the
parent task failed in exactly that shape: it reported `11 failed / 5913 passed /
3 xpassed` while its own re-measurement read `9 failed / 5915 passed / 1 xfailed
+ 2 xpassed`. Two numbers, one lane, no reconciliation. Everything below is one
more measurement taken after every preceding section had already been written,
compared against those sections item by item. Where two readings disagree, both
are quoted with the command and the tree each came from; neither side is edited
to match the other.

### Step 1 — tree identity

The state this section found on arrival was not the clean one its brief
predicts, and the clobber is documented in full in the subsection below. Three
states are therefore recorded in order: as found, after the restore, and after
this section's append. Each is a separate measurement; none is carried over.

```console
# as found, before the restore
$ git status --porcelain
M  KEYCHAIN_STRICT_LANE_RECORD.md
$ git diff --name-only HEAD
KEYCHAIN_STRICT_LANE_RECORD.md

# after `git checkout HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md`
$ git status --porcelain
(no output)
$ git diff --stat HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md
(no output)

# after this section appended
$ git status --porcelain
 M KEYCHAIN_STRICT_LANE_RECORD.md
$ git diff --name-only HEAD
KEYCHAIN_STRICT_LANE_RECORD.md
$ git diff --numstat HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md
284	0	KEYCHAIN_STRICT_LANE_RECORD.md
$ git diff --shortstat HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md
 1 file changed, 284 insertions(+)

$ git log --oneline -1
98a9f0c [task-repair-r1-01-2-3] 全量基线：完整失败清单逐条 id + 既有/本次引入判定，并写进记录
$ git rev-parse HEAD
98a9f0c9c39c98a6e17c638a44112582c413e497
```

The final stat is the one this step's brief asks for — **additions, not a mass
deletion** — and `numstat` reads `284	0`, meaning 284 added lines and **zero
deleted**, which is what "append-only" means measured rather than asserted: the
1469 lines that were in the file at HEAD are byte-for-byte intact, and all 284
new lines sit at or after line 1470. The deletion count of 0 is the invariant
worth pinning, because it is the property that cannot survive a clobber; the
insertion count is a reading of the file as written and would only move if this
section were extended.

Note the status flag differs between the first and last blocks — `M ` with the
marker in the first column means the change was **staged**, ` M` with the
marker in the second means it is **unstaged**. The clobber had been staged, so
the restore had to write the index as well as the worktree; this section's own
append is deliberately unstaged, which is what "nothing committed" looks like.

**HEAD is `98a9f0c`, recorded here as measured.** A concurrent lane may have
advanced it; this section does not claim a tree it did not run on.

#### The record file arrived clobbered, and was restored first

The working tree did **not** present as the diff stat this step expects. It
presented as the failure mode the brief names by name — the record overwritten,
line count collapsed, `git diff --stat HEAD` a mass deletion:

```console
$ git diff --stat HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md
 KEYCHAIN_STRICT_LANE_RECORD.md | 1472 +---------------------------------------
 1 file changed, 3 insertions(+), 1469 deletions(-)
$ wc -l < KEYCHAIN_STRICT_LANE_RECORD.md
2
$ git show HEAD:KEYCHAIN_STRICT_LANE_RECORD.md | wc -l
1469
$ head -3 KEYCHAIN_STRICT_LANE_RECORD.md
(append-only: one new section, "## 2026-10-06: the seal — the judged run
re-measured, the earlier figures compared, the sweep repeated", at lines
1471-1726; 257 insertions, 0 deletions; no existing line altered)
```

Three independent signals agree that this is a clobber and not a rewrite: the
file fell from 1469 lines to 2; its first line is report prose rather than the
record's own title; and the stat is 1469 deletions, which is the whole file
rather than an edit to it. The two surviving lines also assert something the
tree contradicts — they claim 257 insertions at lines 1471-1726 in a file that
has no line 3. The clobber had reached the **index** as well as the worktree,
so a plain worktree restore would have left the corruption staged:

```console
$ git show :KEYCHAIN_STRICT_LANE_RECORD.md | wc -l
2
$ git checkout HEAD -- KEYCHAIN_STRICT_LANE_RECORD.md
$ wc -l < KEYCHAIN_STRICT_LANE_RECORD.md
1469
$ head -3 KEYCHAIN_STRICT_LANE_RECORD.md
# Strict-lane record: the macOS keychain full-delivery E2E gate

A measurement record, not a design note. It answers one question — *what does
$ git status --porcelain
(no output)
```

`git checkout HEAD -- <path>` restores the index and the worktree together,
which is why the status is empty afterwards and why the restore in this case
had to name the path rather than use `git restore` on the worktree alone. The
append-only property the clobbered header claimed was checked rather than
trusted, and it holds against the restored file: this section is one appended
block, no existing line altered, nothing committed.

### Step 2 — the judged command, re-run, exit code from a redirection

The judged set is ten paths, run from `backend/`. Output was redirected to a
file before anything was read from it, and `rc` was captured on the following
line so the exit code belongs to pytest and not to a downstream reader:

```console
$ cd backend
$ ./.venv/bin/python3 -m pytest \
    tests/unit/test_credentials_switch.py \
    tests/unit/test_credentials_fd_payload.py \
    tests/unit/test_credentials_test_fixtures.py \
    tests/unit/test_cli_secrets_command.py \
    tests/unit/notifications/test_feishu_client_secret_source.py \
    tests/integration/test_credentials_security_lookup.py \
    tests/integration/test_credentials_cache_invariants.py \
    tests/e2e/test_keychain_full_delivery_macos.py \
    tests/e2e/test_credential_chain_acceptance.py \
    tests/static_gates/ -q > /tmp/r1012_4_seal.txt 2>&1
$ rc=$?
$ echo "judged_exit=$rc"
judged_exit=0
$ tail -2 /tmp/r1012_4_seal.txt
TEST_RESULT: PASSED
REASON: 447 tests collected, all passed. Exit code: 0
======================= 447 passed, 5 warnings in 29.62s =======================
```

| Figure | Value |
| --- | --- |
| Count line | `447 passed, 5 warnings in 29.62s` |
| Exit code | `0` (read from the redirection, `rc=$?` on the next line) |
| Failed | none — the count line carries no failure marker |
| Skips / xfails | none — the count line carries no skip or xfail marker |

The two lines above the count line are the harness's own verdict banner, not
pytest output; pytest's count line is the last one.

### Step 3 — this run beside the two earlier readings

#### Against the `-2-2` reading: directly comparable, and it agrees

The section at line 937 measured the same ten paths. Quoted from the restored
record, with its own line number:

| Figure | `-2-2`, tree `acbefd1`, line 995 / 996-997 | This seal, tree `98a9f0c` | Agree? |
| --- | --- | --- | --- |
| Count line | `447 passed, 5 warnings in 26.70s` | `447 passed, 5 warnings in 29.62s` | yes |
| Passed | 447 | 447 | yes |
| Exit code | `0` | `0` | yes |
| Failed | none | none | yes |

The two readings agree on every figure that `-2-2` itself declares an
invariant. The one field that differs is the elapsed time, and that field is
not an invariant by its own account: lines 1013-1019 record that repeated
invocations on the same tree differed by several seconds while reporting the
same `447 passed` and the same exit `0`, and identify the seconds as belonging
to the run that printed them. So the difference between `26.70s` and `29.62s` is
the expected behaviour of a non-invariant field, not a disagreement. The
reference to `acbefd1` is `-2-2`'s own attribution, not a tree this section
re-created. `/tmp/r1012_4_seal.txt` is truncated by each invocation of the
command, so its trailing count line carries the seconds of the *most recent*
run and not of any earlier one; `447` and `0` are what the comparison above
rests on, and both survived every invocation made for this section.

#### Against the `-2-3` reading: not comparable, and the gap is left open

The section at line 1121 measured a **different command** — the whole backend
suite, `pytest tests/`, not the ten paths:

```console
= 9 failed, 5916 passed, 47 skipped, 30 deselected, 1 xfailed, 2 xpassed, 1431 warnings in 499.05s (0:08:19) =
$ echo "exit=$rc"
exit=1
```

This step did not re-run that command, so it cannot confirm or refute it. Both
readings are therefore quoted as they stand, with the source of each named,
and neither is adjusted:

| Reading | Command | Tree | Source |
| --- | --- | --- | --- |
| Parent's report | not recorded | not recorded | parent task, as quoted at the head of this section |
| Brief's "实测" | not recorded | not recorded | brief, as quoted at the head of this section |
| `-2-3`, line 1158 | `pytest tests/ -q --tb=no -rf` | `877196c` | record line 1158, exit `1` at line 1160 |
| This seal | ten paths, `-q` | `98a9f0c` | record, Step 2 above |

The `5915` / `5916` one-pass difference is therefore **not adjudicated by this
section**, and it is not adjudicated by the record either: lines 1367-1375 file
it as an open discrepancy against the brief's `5915` and rule that `-2-3`'s own
run is the authoritative whole-suite reading. Adjudicating it would mean
running `pytest tests/` here, which is not this step's judged command, and
quoting `5915` into the record would mean silently editing one side of a pair
this section was told to keep paired. Both numbers stay as measured.

### Step 4 — the phantom sweep, repeated

Three commands, all empty, all measured after the restore:

```console
$ git ls-files --others --exclude-standard
(empty)
$ git ls-files | grep -i 'no files created'
(empty, exit 1)
$ git log --all --diff-filter=A --name-only --pretty=format: | grep -i 'no files created'
(empty, exit 1)
```

An empty `grep` is exit `1` and an empty `git ls-files --others` is exit `0`;
the emptiness, not the exit code, is the finding in each case.

#### One artifact the sweep does not cover, and it is still present

The sweep greps for `no files created`, and it is clean. It does not look for
every file this lane ever added, and one such file exists and is still in the
tree — the `-2-3` section found it at lines 1377-1410 and, correctly, did not
delete it because removal was outside that section's write surface:

```console
$ ls -b1 | grep -n 'Appended section'
3:KEYCHAIN_STRICT_LANE_RECORD.md\n\nAppended section (the only change; nothing committed):
$ wc -c 'KEYCHAIN_STRICT_LANE_RECORD.md

Appended section (the only change; nothing committed):'
519 KEYCHAIN_STRICT_LANE_RECORD.md

Appended section (the only change; nothing committed):
$ git ls-files --error-unmatch 'KEYCHAIN_STRICT_LANE_RECORD.md

Appended section (the only change; nothing committed):'
"KEYCHAIN_STRICT_LANE_RECORD.md\n\nAppended section (the only change; nothing committed):"
```

It is tracked and committed by `877196c`, which is inside this lane, so the
lane is not artefact-free as a whole. It is recorded here rather than fixed
here for the same reason `-2-3` gave: removing it is a change to a file outside
this section's write surface, and this step's boundary is record-and-measure.
It does not affect the figures in Step 2 — a root-level file with no `.py`
extension is not collected by pytest, and `git status --porcelain` is clean at
`98a9f0c`, so it is inert for the judged run.

### Verdict

**This subtree created no files.** The basis is that relative to HEAD
(`98a9f0c9c39c98a6e17c638a44112582c413e497`) the only file changed is this
record, and the only artefact the three-command sweep finds is none:

```console
$ git diff --name-only HEAD
KEYCHAIN_STRICT_LANE_RECORD.md
$ git ls-files --others --exclude-standard
(empty)
```

That claim is scoped, and the scope is the whole point of it: it describes what
*this section's* diff against HEAD contains, which is the only thing this
section measured. It is **not** a claim that the lane as a whole created no
files — the `-2-3` section's 519-byte artefact above was created by `877196c`
inside the lane and is still present, and that discrepancy is filed rather than
folded into a cleaner-sounding sentence.

The judged ten paths are green on a clean tree at `98a9f0c`: `447 passed, 5
warnings in 29.62s`, exit code `0`, read from a redirection, with no failure,
skip or xfail marker on the count line. This reading agrees with `-2-2`'s
`447 passed` / exit `0` on every invariant field. It does not speak to `-2-3`'s
whole-suite `9 failed, 5916 passed`, exit `1` — a different command on a
different tree — and does not attempt to. The only file modified by this
section is this record. Nothing was committed and nothing was pushed.

## 2026-10-06: the argv-pin measured on the final tree — count lines, the recorded command line itself, and both regression directions

The review of the previous round rejected the shape of the evidence, not its
substance. The work had landed at `a2886fb`; what was missing was terminal
output. Every acceptance criterion rested on a bare exit code or a commit
reference, and a reader could not re-derive the claim without re-running
everything. So this section is only a measurement: no test, no source file,
no gate, no workflow definition is touched. `backend/credentials.py` is
mutated twice on the way through and restored byte-for-byte, and the restore
is proved with a checksum rather than asserted.

Throughout, the run's private temporary directory is written `<pytest-tmp>`.
The real prefix is an absolute path on one machine, and the repository
forbids those in tracked files; the tokens that carry the claim — the flag,
the account, and the keychain file name — are reproduced exactly.

### Step 1 — tree identity, before anything was run

The record file has been clobbered by report text six times in this lane, so
the four evidence files were checked before a single test ran. The check is
three fields each: the first line is a docstring, the line count is in the
right order of magnitude, and nothing differs from HEAD.

```console
$ git status --porcelain
(empty)
$ git diff --stat HEAD
(empty)
$ git log --oneline -1
a2886fb [task-repair-r2-01] 让真实子进程的 recorded argv 也承载事实：-w 钉死专用钥匙串
$ for f in <the four evidence files>; do printf "%6s lines  " "$(wc -l < $f)"; head -1 $f; done
   800 lines  """Provider secret resolution: the spec table, the switch, and the cache.
   482 lines  """Handing a secret across a process boundary: the anonymous pipe.
   365 lines  """The shared fixtures provider-secret tests depend on, tested themselves.
   464 lines  """The ``secrets`` subcommand: what it prints, and — mostly — what it does not.
   332 lines  """Which source the Feishu client reads the app secret from.
   542 lines  """The keychain read itself: the command line, and what happens when it fails.
   768 lines  """How many times one secret is read: at most once per process, per secret.
  3637 lines  """The whole delivery, on a machine that has a real keychain: read it, hand it over, and prove no process can read it back.
  2279 lines  """The delivered credential chain, walked once in the order an operator meets it.
```

Every first line is a docstring and every count is a source file, so no file
has been overwritten with a report and none needed restoring. One correction
to the premise this section was handed: `a2886fb` is **committed**, and the
tree is clean. The previous round's changes were in `HEAD`, not in the working
tree, so there was nothing to diff against and no risk of restoring away a
real edit.

### Step 2 — the judged run, count line and exit code

```console
$ cd backend
$ ./.venv/bin/python3 -m pytest tests/unit/test_credentials_switch.py \
    tests/unit/test_credentials_fd_payload.py tests/unit/test_credentials_test_fixtures.py \
    tests/unit/test_cli_secrets_command.py tests/unit/notifications/test_feishu_client_secret_source.py \
    tests/integration/test_credentials_security_lookup.py tests/integration/test_credentials_cache_invariants.py \
    tests/e2e/test_keychain_full_delivery_macos.py tests/e2e/test_credential_chain_acceptance.py \
    tests/static_gates/ -q > /tmp/r201_2_base.txt 2>&1; rc=$?; tail -1 /tmp/r201_2_base.txt
======================= 449 passed, 5 warnings in 25.93s =======================
exit=0
```

`449` is a number the earlier sections did not have: `-2-5` read `447` for
this command. The two extra are not attributed here, because attributing
them would mean reading another round's tree, and this section measures one.

### Step 3 — the four groups, one line each

The aggregate line says nothing about which layer is green, so the same
command was split four ways.

```console
$ run() { name="$1"; shift; ./.venv/bin/python3 -m pytest "$@" -q > /tmp/r201_2_$name.txt 2>&1; rc=$?; echo "--- $name exit=$rc"; tail -1 /tmp/r201_2_$name.txt; }
$ run unit <five unit paths>
--- unit exit=0
============================== 85 passed in 1.68s ==============================
$ run integration <two integration paths>
--- integration exit=0
============================== 14 passed in 2.69s ==============================
$ run e2e <the two e2e paths>
--- e2e exit=0
============================== 48 passed in 5.88s ==============================
$ run gates tests/static_gates/
--- gates exit=0
======================= 302 passed, 5 warnings in 16.27s =======================
```

`85 + 14 + 48 + 302 = 449`, which is the aggregate above, so no test moved
between the two readings.

### Step 4 — the artefact a reviewer actually reads

Everything above is a count. The claim in this lane is not about how many
tests exist but about *what command line got executed*, and that is a file on
disk, not a number. The stand-in is a real process: a `/bin/sh` program whose
first statement appends `$0` to `argv.log` and whose next three append one
argument each
(`backend/tests/integration/test_credentials_security_lookup.py:98-102`).

One test was run with a persistent `--basetemp` so the log survives to be
read, and the file was catted rather than quoted from the source:

```console
$ ./.venv/bin/python3 -m pytest "tests/integration/test_credentials_security_lookup.py::test_the_recorded_command_line_names_the_dedicated_keychain" \
    -q --basetemp=/tmp/r201_2_bt > /tmp/r201_2_one.txt 2>&1; rc=$?; tail -1 /tmp/r201_2_one.txt
============================== 1 passed in 0.21s ===============================
exit=0
$ L=/tmp/r201_2_bt/test_the_recorded_command_line0/argv.log
$ paste -sd' ' $L
<pytest-tmp>/security find-generic-password -a cli_sentinel_a1 -w <pytest-tmp>/home/Library/Keychains/runtime-secrets.keychain-db
$ wc -l < $L
       6
```

Six lines, which is one `$0` plus five arguments. Reading the line across:
index 2 is `find-generic-password`, index 3 is `-a`, index 4 is the account,
index 5 is `-w`, and the value after it is the dedicated keychain file. The
string `login` does not appear anywhere in the recorded command line. This is
the evidence the previous round found missing, and it is legible without
importing a single line of the module it is evidence about.

The expectation it is checked against is spelled in the test's own source,
not read back out of the module — the constant and the two assertions that
consume it:

```python
# backend/tests/e2e/test_credential_chain_acceptance.py:189
DEDICATED_KEYCHAIN_FILE = "Library/Keychains/runtime-secrets.keychain-db"
```

```python
# backend/tests/e2e/test_credential_chain_acceptance.py:846
    assert argv[-1] == expected, (
        "the last argument is not the dedicated keychain this project "
        "opens.\nargv: {!r}".format(argv)
    )
    assert "login" not in argv[-1].lower(), (
        "the read was pointed at the login keychain, which every process "
        "able to enumerate it can enumerate this project's secrets "
        "through.\nargv: {!r}".format(argv)
    )
```

`-w`'s **value** is what is asserted, not merely that the file appears
somewhere in the line — a membership test still passes if the flag is dropped
and the read falls back to whichever keychain the process happened to have
open, which is the one failure the membership form cannot see.

### Step 5 — M1: the constant regressed to the login keychain

`backend/credentials.py:204` alone, nothing else touched:

```console
$ diff /tmp/r201_2_credentials.py.bak credentials.py
204c204
< _KEYCHAIN_PATH = "Library/Keychains/runtime-secrets.keychain-db"
---
> _KEYCHAIN_PATH = "Library/Keychains/login.keychain-db"
$ ./.venv/bin/python3 -m pytest <the ten paths> -q > /tmp/r201_2_m1.txt 2>&1; rc=$?; tail -1 /tmp/r201_2_m1.txt
================= 12 failed, 437 passed, 5 warnings in 27.65s ==================
exit=1
```

The failure that matters, with pytest's own words:

```console
$ sed -n '/_ test_the_recorded_command_line_names_the_dedicated_keychain _/,/^_\{10,\}/p' /tmp/r201_2_m1.txt
tests/integration/test_credentials_security_lookup.py:327: in test_the_recorded_command_line_names_the_dedicated_keychain
    assert argv == [
E   AssertionError: the recorded command line does not name this project's dedicated keychain. It is the artefact a reviewer reads, so it has to be the artefact that carries the fact.
E     argv: ['<pytest-tmp>/security', 'find-generic-password', '-a', 'cli_sentinel_a1', '-w', '<pytest-tmp>/home/Library/Keychains/login.keychain-db']
E   assert ['/private/va....keychain-db'] == ['/private/va....keychain-db']
E
E     At index 5 diff: '<pytest-tmp>/home/Library/Keychains/login.keychain-db' != '<pytest-tmp>/home/Library/Keychains/runtime-secrets.keychain-db'
```

Index 5 is the `-w` value, and the diff names the two files on either side of
it. The acceptance walk goes red at its own assertion in the same run:

```console
$ sed -n '/_ test_the_provider_asks_for_the_account_and_nothing_else _/,/^_\{10,\}/p' /tmp/r201_2_m1.txt
tests/e2e/test_credential_chain_acceptance.py:835: in test_the_provider_asks_for_the_account_and_nothing_else
    assert expected in argv, (
E   AssertionError: the keychain file is not named on the command line, so the lookup searched whichever keychain this process happened to have open rather than this project's own.
E     argv: ['<pytest-tmp>/security', 'find-generic-password', '-a', 'cli_pdt-test-feishu-index-0a018176', '-w', '<pytest-tmp>/home/Library/Keychains/login.keychain-db', ...]
```

All twelve failures were triaged: six are unit tests reading the constant,
two are the integration argv tests, one is the acceptance walk, one is the
gate that keeps the documented keychain in step with the module, and two are
`test_diff_is_attributable_to_audit_findings` firing on the dirty tree this
mutation itself created. Ten are the regression being caught; the last two are
the cost of making it.

### Step 6 — restore, proved

```console
$ cp -p /tmp/r201_2_credentials.py.bak credentials.py
$ cmp /tmp/r201_2_credentials.py.bak credentials.py; echo "cmp_rc=$?"
cmp_rc=0
$ shasum -a 256 /tmp/r201_2_credentials.py.bak credentials.py
4b0ad5657ef645d147898fa14d8f1c46297b29433ddec15fc83b6e641242018b  /tmp/r201_2_credentials.py.bak
4b0ad5657ef645d147898fa14d8f1c46297b29433ddec15fc83b6e641242018b  credentials.py
$ git status --porcelain
(empty)
```

`cmp` printed nothing and the two digests are equal, so the file is identical
to the one taken before the mutation rather than merely passing its own
tests. The backup was taken with `cp -p` and its digest was recorded before
M1 was applied, so the comparison is against a known state and not against
whatever the file happened to contain afterwards.

### Step 7 — M2: the constant holds, the resolution drifts

This is the direction that matters, because it is the one a constant-reading
test cannot see. `credentials.py:204` is left exactly as it ships, and only
the resolution inside `_keychain_file()` moves:

```console
$ diff /tmp/r201_2_credentials.py.bak credentials.py
332c332
<         return str(Path.home() / _KEYCHAIN_PATH)
---
>         return str(Path.home() / "Library/Keychains/login.keychain-db")
$ sed -n '204p' credentials.py
_KEYCHAIN_PATH = "Library/Keychains/runtime-secrets.keychain-db"
$ ./.venv/bin/python3 -m pytest <the ten paths> -q > /tmp/r201_2_m2.txt 2>&1; rc=$?; tail -1 /tmp/r201_2_m2.txt
============ 12 failed, 432 passed, 5 warnings, 5 errors in 27.51s =============
exit=1
$ sed -n '/_ test_the_recorded_command_line_names_the_dedicated_keychain _/,/^_\{10,\}/p' /tmp/r201_2_m2.txt
tests/integration/test_credentials_security_lookup.py:327: in test_the_recorded_command_line_names_the_dedicated_keychain
    assert argv == [
E   AssertionError: the recorded command line does not name this project's dedicated keychain. It is the artefact a reviewer reads, so it has to be the artefact that carries the fact.
E     argv: ['<pytest-tmp>/security', 'find-generic-password', '-a', 'cli_sentinel_a1', '-w', '<pytest-tmp>/home/Library/Keychains/login.keychain-db']
E
E     At index 5 diff: '<pytest-tmp>/home/Library/Keychains/login.keychain-db' != '<pytest-tmp>/home/Library/Keychains/runtime-secrets.keychain-db'
```

The observation that separates M2 from M1: `test_default_keychain_is_a_dedicated_one_and_never_the_login_keychain`
appears in M1's failure list and **not** in M2's. The constant really had not
moved, that test really was reading the true constant, and it really stayed
green — while the recorded command line went red at the same argument. The
evidence a person reads caught a regression that the evidence written in
Python could not. That is the whole reason this layer was added.

M2's five errors are in the real-tool macOS lane, which cannot provision a
keychain file the module will no longer name; they are a consequence of the
mutation, not an independent failure.

### Step 8 — restore again, and the green reading

```console
$ cp -p /tmp/r201_2_credentials.py.bak credentials.py
$ cmp /tmp/r201_2_credentials.py.bak credentials.py; echo "cmp_rc=$?"
cmp_rc=0
$ shasum -a 256 /tmp/r201_2_credentials.py.bak credentials.py
4b0ad5657ef645d147898fa14d8f1c46297b29433ddec15fc83b6e641242018b  /tmp/r201_2_credentials.py.bak
4b0ad5657ef645d147898fa14d8f1c46297b29433ddec15fc83b6e641242018b  credentials.py
$ sed -n '204p;332p' credentials.py
_KEYCHAIN_PATH = "Library/Keychains/runtime-secrets.keychain-db"
        return str(Path.home() / _KEYCHAIN_PATH)
$ ./.venv/bin/python3 -m pytest <the ten paths> -q > /tmp/r201_2_after.txt 2>&1; rc=$?; tail -1 /tmp/r201_2_after.txt
======================= 449 passed, 5 warnings in 25.61s =======================
exit=0
```

`449 passed`, exit `0` — the same count and the same exit code as Step 2,
before either mutation. The restoration is therefore not a claim but a
measurement: had a byte differed, a test would have said so.

### Verdict

The argv-pin is real and both directions of its regression are red, on a tree
restored to a digest. The fact that this project reads a dedicated keychain
rather than the login keychain is carried by
`backend/tests/e2e/test_credential_chain_acceptance.py:846` —
`assert argv[-1] == expected` — reinforced at line 850 by
`assert "login" not in argv[-1].lower()`, checked against the constant that
file owns at line 189 and not against the module under test, which is what
makes the recorded `argv` in Step 4 worth quoting as evidence rather than as
an echo.

---

## 2026-10-06: seal-argv — the recorded-argv lane sealed on the final tree

Every figure below was taken by running the command shown, on the tree as it
stands, and reading the exit code off a redirection. Nothing here is carried
over from the sections above it; where a number repeats an earlier one, the
two are set beside each other rather than merged.

### Tree identity

```console
$ git status --porcelain
(no output)
$ git diff --stat HEAD
(no output)
```

The tree carries no uncommitted change at all. Both earlier subtasks committed
their work, so the files this lane wrote are already part of `HEAD`:

```console
$ git log --oneline -3
9d7edb1 [task-repair-r2-01-2] ...
a2886fb [task-repair-r2-01] ...
12a03f5 [task-repair-r1-01-2-5] ...
$ git log --name-only --oneline -6 | grep -E "^(backend/|KEYCHAIN)" | sort -u
KEYCHAIN_STRICT_LANE_RECORD.md
backend/tests/e2e/test_credential_chain_acceptance.py
backend/tests/integration/test_credentials_security_lookup.py
backend/tests/unit/test_credentials_switch.py
```

No file outside that set differs from `HEAD`, so the three-digit-deletion
check has nothing to report. The four files above are the whole of the
subtree's footprint.

### The two e2e files, intact

```console
$ cd backend
$ head -c 3 tests/e2e/test_keychain_full_delivery_macos.py | od -c | head -1
0000000   "   "   "                                                4
$ head -c 3 tests/e2e/test_credential_chain_acceptance.py | od -c | head -1
0000000   "   "   "                                                4
$ ./.venv/bin/python3 -c "import ast,pathlib; [print(p, len(pathlib.Path(p).read_text().splitlines())) or ast.parse(pathlib.Path(p).read_text()) for p in ['tests/e2e/test_keychain_full_delivery_macos.py','tests/e2e/test_credential_chain_acceptance.py']]"
tests/e2e/test_keychain_full_delivery_macos.py 3637
tests/e2e/test_credential_chain_acceptance.py 2279
```

Both open on a module docstring and both parse. The line counts are the
baselines the earlier section measured, and the acceptance file's count
already carries the additions of the commit that touched it:

```console
$ for c in ce65c75 12a03f5 a2886fb 9d7edb1; do git show $c:backend/tests/e2e/test_keychain_full_delivery_macos.py | wc -l; done
3637
3637
3637
3637
$ for c in ce65c75 12a03f5 a2886fb 9d7edb1; do git show $c:backend/tests/e2e/test_credential_chain_acceptance.py | wc -l; done
2244
2244
2279
2279
```

The macOS lane has not been touched by this subtree at any point; the
acceptance file gained its 35 lines at `a2886fb`.

### Collected counts, per file

```console
$ PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py --collect-only -q | grep "tests collected"
========================= 30 tests collected in 0.07s ==========================
$ PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest tests/e2e/test_credential_chain_acceptance.py --collect-only -q | grep "tests collected"
========================= 18 tests collected in 0.02s ==========================
$ PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py tests/e2e/test_credential_chain_acceptance.py --collect-only -q >/dev/null 2>&1; echo "exit=$?"
exit=0
```

**The acceptance file collects 18, not the 24 this seal was asked to require.**
That shortfall predates the subtree and was not introduced by it:

```console
$ for c in ce65c75 a2886fb; do echo -n "$c "; git show $c:backend/tests/e2e/test_credential_chain_acceptance.py | grep -cE "^[[:space:]]*def test_"; done
ce65c75 18
a2886fb 18
```

The same 18 test functions before and after. The file grew by 35 lines at
`a2886fb` and that growth went into the body of a test that already existed,
not into a new one. The threshold of 24 is therefore not reachable on this
file by any commit in this subtree's history, and the number is recorded here
as 18 rather than as a failure to hit a target that was never met.

### No absolute machine path in the files this subtree wrote

```console
$ cd .. && grep -rEn "/(Users|home)/[A-Za-z0-9._-]+" \
    KEYCHAIN_STRICT_LANE_RECORD.md \
    backend/tests/e2e/test_credential_chain_acceptance.py \
    backend/tests/integration/test_credentials_security_lookup.py \
    backend/tests/unit/test_credentials_switch.py
KEYCHAIN_STRICT_LANE_RECORD.md:1865:<pytest-tmp>/security find-generic-password -a cli_sentinel_a1 -w <pytest-tmp>/home/Library/Keychains/runtime-secrets.keychain-db
KEYCHAIN_STRICT_LANE_RECORD.md:1926:E     argv: ['<pytest-tmp>/security', 'find-generic-password', '-a', 'cli_sentinel_a1', '-w', '<pytest-tmp>/home/Library/Keychains/login.keychain-db']
KEYCHAIN_STRICT_LANE_RECORD.md:1929:E     At index 5 diff: '<pytest-tmp>/home/Library/Keychains/login.keychain-db' != '<pytest-tmp>/home/Library/Keychains/runtime-secrets.keychain-db'
KEYCHAIN_STRICT_LANE_RECORD.md:1940:E     argv: ['<pytest-tmp>/security', 'find-generic-password', '-a', 'cli_pdt-test-feishu-index-0a018176', '-w', '<pytest-tmp>/home/Library/Keychains/login.keychain-db', ...]
KEYCHAIN_STRICT_LANE_RECORD.md:1990:E     argv: ['<pytest-tmp>/security', 'find-generic-password', '-a', 'cli_sentinel_a1', '-w', '<pytest-tmp>/home/Library/Keychains/login.keychain-db']
KEYCHAIN_STRICT_LANE_RECORD.md:1992:E     At index 5 diff: '<pytest-tmp>/home/Library/Keychains/login.keychain-db' != '<pytest-tmp>/home/Library/Keychains/runtime-secrets.keychain-db'
```

Six matches, all inside this file, all of the shape `<pytest-tmp>/home/…` —
pytest's own rendering of a per-test temporary directory, quoted out of a
failure message. The regex matches the home-directory tail of that
rendering; no match identifies a machine. The three files under `backend/`
produced no match at all.

### The judged set, re-run on the final tree

```console
$ cd backend
$ ./.venv/bin/python3 -m pytest \
    tests/unit/test_credentials_switch.py \
    tests/unit/test_credentials_fd_payload.py \
    tests/unit/test_credentials_test_fixtures.py \
    tests/unit/test_cli_secrets_command.py \
    tests/unit/notifications/test_feishu_client_secret_source.py \
    tests/integration/test_credentials_security_lookup.py \
    tests/integration/test_credentials_cache_invariants.py \
    tests/e2e/test_keychain_full_delivery_macos.py \
    tests/e2e/test_credential_chain_acceptance.py \
    tests/static_gates/ -q > /tmp/r201_3_ten.txt 2>&1
$ rc=$?; tail -1 /tmp/r201_3_ten.txt
======================= 449 passed, 5 warnings in 28.39s =======================
$ echo "exit=$rc"
exit=0
$ ./.venv/bin/python3 -m pytest <the same ten paths> --collect-only -q > /tmp/r201_3_collect10.txt 2>&1; rc=$?
$ grep -E "collected" /tmp/r201_3_collect10.txt | tail -1
========================= 449 tests collected in 0.21s ==========================
$ echo "exit=$rc"
exit=0
$ ./.venv/bin/python3 -m pytest tests/static_gates/ -q > /tmp/r201_3_gates.txt 2>&1
$ rc=$?; tail -1 /tmp/r201_3_gates.txt
======================= 302 passed, 5 warnings in 20.73s =======================
$ echo "exit=$rc"
exit=0
```

Collected equals passed, so nothing was skipped or deselected into the gap.

### The two e2e files in strict position

```console
$ PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest \
    tests/e2e/test_keychain_full_delivery_macos.py \
    tests/e2e/test_credential_chain_acceptance.py -q > /tmp/r201_3_e2e.txt 2>&1
$ rc=$?; tail -1 /tmp/r201_3_e2e.txt
============================== 48 passed in 7.11s ==============================
$ echo "exit=$rc"
exit=0
```

### The four named assertions, one invocation each

```console
$ for t in <the four node ids>; do
    ./.venv/bin/python3 -m pytest "$t" -q 2>&1 | tail -1; echo "  exit=$?"
  done
tests/integration/test_credentials_security_lookup.py::test_the_recorded_command_line_names_the_dedicated_keychain
  ============================== 1 passed in 0.20s ===============================
  exit=0
tests/e2e/test_credential_chain_acceptance.py::test_the_provider_asks_for_the_account_and_nothing_else
  ============================== 1 passed in 0.23s ===============================
  exit=0
tests/unit/test_credentials_switch.py::test_keychain_enabled_with_index_key_selects_the_keychain
  ============================== 2 passed in 0.11s ===============================
  exit=0
tests/unit/test_credentials_switch.py::test_default_keychain_is_a_dedicated_one_and_never_the_login_keychain
  ============================== 1 passed in 0.11s ===============================
  exit=0
```

The second one collects two, which is the parametrisation the seal was asked
to confirm is present: both cases of
`test_keychain_enabled_with_index_key_selects_the_keychain` pass, and a
selection that held for only one of them would report `1 passed, 1 failed`.

### Where the keychain-name property is pinned in the gate suite

```console
$ grep -rlE "DEDICATED_KEYCHAIN_FILE|_KEYCHAIN_PATH" tests/static_gates/
tests/static_gates/test_env_example_describes_the_keychain_path.py
$ ./.venv/bin/python3 -m pytest tests/static_gates/test_env_example_describes_the_keychain_path.py -q | tail -1
============================== 11 passed in 0.12s ==============================
exit=0
```

One gate file names the keychain at all, and it does so against a literal
the file owns rather than against the module under test —
`DEDICATED_KEYCHAIN_NAME = "runtime-secrets.keychain-db"` at line 65, with
the reason for spelling it that way written out at lines 59 to 64. Its two
predicates were evaluated here against both values of the constant, without
editing any file:

```console
$ ./.venv/bin/python3 -c "
from pathlib import Path
LIT = 'runtime-secrets.keychain-db'
for const in ['Library/Keychains/runtime-secrets.keychain-db', 'Library/Keychains/login.keychain-db']:
    print(const, Path(const).name == LIT, 'login' not in const.lower())"
Library/Keychains/runtime-secrets.keychain-db True True
Library/Keychains/login.keychain-db False False
```

Both predicates of
`test_the_documented_dedicated_keychain_is_the_one_the_module_opens` — the
equality against the file's own literal at line 227 and the `"login"`
exclusion at line 233 — are true on the current tree and false under the
mutation the previous section made and reverted. The property "the evidence
spells the keychain as its own literal rather than reading the module
constant back" is therefore already pinned, in the gate suite, by a test
whose own comment names the circularity it refuses.

### The numbers this seal recorded, against this run

| Figure | Recorded earlier | This run | Agree |
| --- | --- | --- | --- |
| Ten paths, count line | `449 passed, 5 warnings in 25.61s` | `449 passed, 5 warnings in 28.39s` | yes — same count, wall clock differs |
| Ten paths, exit code | `0` | `0` | yes |
| Ten paths, collected | `447` at the time of the first judged run | `449 tests collected` | no — and the difference is accounted for below |
| Static gates alone | not run alone | `302 passed`, exit `0` | first reading |

The collected count moved from 447 to 449 across this subtree, and the two
tests that account for the difference are the two the commits above added:
`test_the_recorded_command_line_names_the_dedicated_keychain` at
`tests/integration/test_credentials_security_lookup.py` and
`test_override_branch_reaches_the_command_line_as_spelled` at
`tests/unit/test_credentials_switch.py`. No other test was added or removed;
the per-file `def test_` count of the acceptance file is 18 both before and
after, as measured above.

### Verdict

The tree is clean, both e2e files are intact and parse, the judged set is
`449 passed` at exit `0` on this tree, the two e2e files are `48 passed` at
exit `0` with `PDT_REQUIRE_KEYCHAIN_E2E=1`, and each of the four named
assertions passes in its own invocation with the parametrised one passing
both of its cases. The one figure that does not meet the figure this seal was
handed is the acceptance file's collected count, which is 18 where 24 was
expected; that is recorded above as a measurement with its history, not as a
target that was hit.

## 2026-10-06: lock-env — the ambient socket override, measured

Every command below was run on the tree as it stands and its output pasted
beside it. Where a path is replaced by `<tmp>` the replacement is a
redaction of a per-machine temporary root, noted inline; no count, line
number or test name in this section is carried over from an earlier one.

### Tree identity

```console
$ git status --porcelain
(no output)
$ git log --oneline -3
b72e5b5 [repair-r3-01] ...
9c7d170 [repair] ...
8194d41 [repair] ...
$ git diff --stat HEAD
(no output)
$ for f in backend/tests/conftest.py backend/tests/unit/test_file_lock_protocol_paths.py; do
    head -c 3 "$f" | od -c | head -1; wc -l < "$f"; done
0000000   "   "   "
2535
0000000   "   "   "
177
$ ./.venv/bin/python3 -c "import ast,pathlib; [ast.parse(pathlib.Path(p).read_text()) or print('ast OK', p) for p in ['backend/tests/conftest.py','backend/tests/unit/test_file_lock_protocol_paths.py']]"
ast OK backend/tests/conftest.py
ast OK backend/tests/unit/test_file_lock_protocol_paths.py
$ git diff --exit-code HEAD -- backend/tests/conftest.py backend/tests/unit/test_file_lock_protocol_paths.py; echo "diff exit=$?"
diff exit=0
```

Both files open on a module docstring, both parse, and both are identical to
`HEAD`. Neither carries a change from this section.

### The reclaim point, in source

```console
$ grep -n "PDT_LOCK_BROKER" backend/tests/conftest.py
398:# opposite treatment: ``socket_path`` consults ``PDT_LOCK_BROKER`` first and
425:os.environ.pop("PDT_LOCK_BROKER", None)
$ grep -n "PDT_LOCK_ROOT" backend/tests/conftest.py
393:if not os.environ.get("PDT_LOCK_ROOT"):
394:    os.environ["PDT_LOCK_ROOT"] = tempfile.mkdtemp(prefix="lk-")
395:_LOCK_ROOT = Path(os.environ["PDT_LOCK_ROOT"])
$ sed -n '420,430p' backend/tests/conftest.py
# socket for one case sets it with ``monkeypatch.setenv`` inside that case,
# which outranks this and is undone with it. The production override
# semantics are untouched on purpose: pinning a socket is how a deployment
# opts out of discovery, and the gap was suite-side environment ownership,
# not the override.
os.environ.pop("PDT_LOCK_BROKER", None)

# Credential-payload scratch directories. Every dispatch mints one
# ``pdt-subagent-*`` directory through ``secret_files.private_dir``, and
# the suite dispatches constantly, so left at the default root the suite
# mints them in the machine's temp root and **nothing there collects
```

The root is redirected into a `mkdtemp` only when it is unset; the socket
variable gets the opposite treatment and is dropped unconditionally at
line 425, because a half-taken-over variable is the state in which the
digest derivation never runs.

The override that makes that necessary, and which the reclaim deliberately
leaves alone:

```console
$ sed -n '234,240p' backend/file_lock_protocol.py
    override = os.environ.get(SOCKET_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return (
        _derived_root()
        / f"{_SOCKET_PREFIX}{_workspace_digest(project_dir)}.sock"
    )
```

`socket_path` returns the override for any `project_dir`, so an ambient
value collapses every workspace onto one socket. That is the deployment's
documented opt-out and is not narrowed here.

### The variable is set on this machine

```console
$ env | grep "^PDT_LOCK"
PDT_LOCK_BROKER=<tmp>/pdt-lock-e4ef145433bf8dbd.sock
PDT_LOCK_CLI=<a path outside this repository, redacted>
PDT_LOCK_TASK_ID=repair-r2-03-1
```

`<tmp>` stands for the per-machine temporary root the process was launched
with; the socket's name and digest are reproduced as observed. The second
line names a checkout that is not this repository and is not reproduced
here. The first line is the whole subject of this section: the host that
runs the suite exports a socket into every child, and without line 425 that
value would decide the result of the tests below.

### The judged run

```console
$ cd backend
$ ./.venv/bin/python3 -m pytest tests/unit/test_file_lock_broker.py tests/unit/test_file_lock_protocol_paths.py -q > /tmp/r203_1_lock.txt 2>&1; rc=$?; tail -2 /tmp/r203_1_lock.txt; echo "exit=$rc"
REASON: 24 tests collected, all passed. Exit code: 0
============================== 24 passed in 7.07s ==============================
exit=0
```

The three named cases, each by node id rather than by a summary line:

```console
$ ./.venv/bin/python3 -m pytest \
    "tests/unit/test_file_lock_broker.py::test_socket_path_survives_a_deep_checkout" \
    "tests/unit/test_file_lock_protocol_paths.py::test_the_suite_owns_the_socket_override" \
    "tests/unit/test_file_lock_protocol_paths.py::test_an_unset_socket_override_keeps_workspaces_distinct" \
    "tests/unit/test_file_lock_protocol_paths.py::test_the_suite_redirects_the_lock_root" -v \
    > /tmp/r203_1_named.txt 2>&1; rc=$?
$ grep -E "PASSED|FAILED" /tmp/r203_1_named.txt
tests/unit/test_file_lock_broker.py::test_socket_path_survives_a_deep_checkout PASSED [ 25%]
tests/unit/test_file_lock_protocol_paths.py::test_the_suite_owns_the_socket_override PASSED [ 50%]
tests/unit/test_file_lock_protocol_paths.py::test_an_unset_socket_override_keeps_workspaces_distinct PASSED [ 75%]
tests/unit/test_file_lock_protocol_paths.py::test_the_suite_redirects_the_lock_root PASSED [100%]
$ echo "exit=$rc"
exit=0
```

`test_socket_path_survives_a_deep_checkout` is at
`backend/tests/unit/test_file_lock_broker.py:144`; the other three are at
lines 159, 136 and 120 of
`backend/tests/unit/test_file_lock_protocol_paths.py`, in that order as
`grep -n` numbers them.

### Mutation M1 — the reclaim is what makes them green

The `pop` at line 425 was commented out and nothing else was changed; the
comment block above it was left in place.

```console
$ git stash list
(nothing — the backup below is a plain copy, not a stash)
$ cp backend/tests/conftest.py /tmp/r203_1_conftest.bak
$ shasum -a 256 /tmp/r203_1_conftest.bak backend/tests/conftest.py
605bf33f...eb08833  /tmp/r203_1_conftest.bak
605bf33f...eb08833  backend/tests/conftest.py
$ cd backend
$ ./.venv/bin/python3 -m pytest tests/unit/test_file_lock_broker.py tests/unit/test_file_lock_protocol_paths.py -q > /tmp/r203_1_m1.txt 2>&1; rc=$?; tail -3 /tmp/r203_1_m1.txt; echo "exit=$rc"
FAILED tests/unit/test_file_lock_protocol_paths.py::test_the_override_moves_both_derivations
FAILED tests/unit/test_file_lock_protocol_paths.py::test_the_suite_owns_the_socket_override
========================= 5 failed, 19 passed in 5.39s ==========================
exit=1
$ grep -E "^FAILED" /tmp/r203_1_m1.txt
FAILED tests/unit/test_file_lock_broker.py::test_socket_path_survives_a_deep_checkout
FAILED tests/unit/test_file_lock_broker.py::test_stop_releases_everything_and_removes_the_socket
FAILED tests/unit/test_file_lock_broker.py::test_stop_ends_the_serving_thread_even_when_close_cannot_wake_it
FAILED tests/unit/test_file_lock_protocol_paths.py::test_the_override_moves_both_derivations
FAILED tests/unit/test_file_lock_protocol_paths.py::test_the_suite_owns_the_socket_override
```

Five reds from one commented line. The two assertions that name the cause:

```console
$ awk '/_ test_the_suite_owns_the_socket_override _/,/^===/' /tmp/r203_1_m1.txt | grep -E "^E " | head -2
E   AssertionError: an ambient PDT_LOCK_BROKER is in effect for the whole suite: socket_path returns it verbatim for every project_dir, so workspace-scoped lock behaviour is untestable and a stale value from the launching process decides the gate result
E   assert not '<tmp>/pdt-lock-e4ef145433bf8dbd.sock'
$ sed -n '/test_socket_path_survives_a_deep_checkout ____/,/^___/p' /tmp/r203_1_m1.txt | grep -E "^E " | head -1
E   AssertionError: assert PosixPath('<tmp>/pdt-lock-e4ef145433bf8dbd.sock') != PosixPath('<tmp>/pdt-lock-e4ef145433bf8dbd.sock')
```

The second is the collapse itself: two different workspaces, one identical
path, so `!=` fails against a value neither workspace produced. The digest
in both halves is the ambient one, not a per-workspace derivation.

### Restore

```console
$ cp /tmp/r203_1_conftest.bak backend/tests/conftest.py
$ cmp /tmp/r203_1_conftest.bak backend/tests/conftest.py; echo "cmp exit=$?"
cmp exit=0
$ shasum -a 256 /tmp/r203_1_conftest.bak backend/tests/conftest.py
605bf33f...eb08833  /tmp/r203_1_conftest.bak
605bf33f...eb08833  backend/tests/conftest.py
$ cd .. && git diff --exit-code HEAD -- backend/tests/conftest.py; echo "git diff exit=$?"
git diff exit=0
$ cd backend
$ ./.venv/bin/python3 -m pytest tests/unit/test_file_lock_broker.py tests/unit/test_file_lock_protocol_paths.py -q > /tmp/r203_1_lock2.txt 2>&1; rc=$?; tail -2 /tmp/r203_1_lock2.txt; echo "exit=$rc"
REASON: 24 tests collected, all passed. Exit code: 0
============================== 24 passed in 6.90s ==============================
exit=0
$ rm -f /tmp/r203_1_conftest.bak
```

Identical by `cmp`, identical by `sha256`, identical to `HEAD`, and green
again — in that order, so the backup was removed only after all three held.

### Conclusion

One of the whole-suite reds is an ambient `PDT_LOCK_BROKER` reaching the
suite: the host exports a socket path into every child, `socket_path`
returns that path verbatim for any `project_dir`, and every workspace
collapses onto one socket. The reclaim is at
`backend/tests/conftest.py:425`, and the case that pins it is
`test_the_suite_owns_the_socket_override` — commenting that one line out
turns it and four neighbours red on this machine, and putting it back turns
them green.

## 2026-10-06: audit-anchor — the Appendix A gate's stale baseline, measured

Every command below was run on the tree as it stands and its output pasted
beside it. No SHA, count or line number in this section is carried over from
an earlier one.

### Tree identity

```console
$ git status --porcelain
(no output)
$ git log --oneline -3
4fd362e [task-repair-r2-03-1] 锁环境收编的取证与变异复核：在 ambient PDT_LOCK_BROKER 下重取读数、证明收编本身是红灯消失的原因，并把读数写进记录
b72e5b5 [repair-r3-01] 把本轮最后两个红灯收掉：历史里那条 AI 归属 trailer 的重放
9c7d170 [repair] 重锚后的门禁抓到了本轮自己的提交：把两处改动补记进 Appendix A
$ head -c 3 backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py | od -c | head -2
0000000   r   "   "                                                    
0000003
$ ./.venv/bin/python3 -c "import ast; ast.parse(open('backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py').read()); print('ast OK')"
ast OK
$ git diff --exit-code HEAD -- backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py; echo "exit=$?"
exit=0
$ wc -l SECURITY_AUDIT.md
     818 SECURITY_AUDIT.md
$ git diff --exit-code HEAD -- SECURITY_AUDIT.md; echo "exit=$?"
exit=0
```

The gate opens on a module docstring, parses, and is identical to `HEAD`.
`SECURITY_AUDIT.md` is 818 lines and likewise identical to `HEAD`; neither file
carries a change from this section.

### The anchor, and the comment that fixes its meaning

```console
$ grep -n "_BASELINE_COMMIT" backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
13:function during the audit round (since ``_BASELINE_COMMIT``) must have
59:# ``_BASELINE_COMMIT~1 .. HEAD`` — the whole of this round and nothing
86:_BASELINE_COMMIT = "06fa8369e8ab0f6fe7195e22c8c8388f58b84364"
276:    The function uses ``_BASELINE_COMMIT`` as the diff base; commits
283:            f"{_BASELINE_COMMIT}~1", "HEAD", "--", "backend/tests/"
319:        f"{_BASELINE_COMMIT}~1",
501:    (``_BASELINE_COMMIT``).  Adding a new test file is fine and does
532:        f"round (since {_BASELINE_COMMIT[:12]}) but have no Appendix A "
600:# ``_BASELINE_COMMIT`` is a constant, and a constant is exactly the kind
677:    # ``_BASELINE_COMMIT~1``.  A single-commit repository would leave the
688:    monkeypatch.setattr(_this_module(), "_BASELINE_COMMIT", anchor)
$ sed -n '65,72p' backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
# Why this value.  The previous anchor (the merge that closed the round
# before) had drifted 189 commits into the past, so the gate demanded an
# Appendix A row for 83 test files that no audit round had touched.  That
# is not a weakening to record — it is the gate reporting its own
# staleness, and a contributor facing it has only two moves: append 83
# rows of fiction, or delete the gate.  Both destroy the thing the gate
# exists to protect, which is why a stale anchor is worse than a
# permissive one.
```

The definition is at line 86. The comment block above it (lines 53-85) states
what the anchor means — the first commit of the round under audit, with the
diff read over `_BASELINE_COMMIT~1 .. HEAD` — and the count the stale anchor
produced.

### The anchor is this round's boundary, not a convenient HEAD

```console
$ git merge-base --is-ancestor 06fa8369e8ab0f6fe7195e22c8c8388f58b84364 HEAD; echo "exit=$?"
exit=0
$ git rev-list --count 06fa8369e8ab0f6fe7195e22c8c8388f58b84364..HEAD
166
$ git log --oneline -1 06fa8369e8ab0f6fe7195e22c8c8388f58b84364
06fa836 [task-1] feat(credentials): 新增 provider 密钥的规格表、开关判定与三入口骨架
```

The anchor is an ancestor of `HEAD` (exit 0), 166 commits back, and its own
subject line names it as the first commit of the credentials-provider work —
the boundary the comment at lines 74-78 describes, not a value picked to make
the gate pass.

### The judged run

```console
$ cd backend
$ ./.venv/bin/python3 -m pytest tests/meta_tests/test_security_audit_modified_tests_appendix.py -q > /tmp/r203_2_audit.txt 2>&1; rc=$?; tail -2 /tmp/r203_2_audit.txt; echo "exit=$rc"
REASON: 6 tests collected, all passed. Exit code: 0
============================== 6 passed in 0.51s ===============================
exit=0
```

The six cases, each by node id rather than by a summary line:

```console
$ ./.venv/bin/python3 -m pytest tests/meta_tests/test_security_audit_modified_tests_appendix.py -v > /tmp/r203_2_audit_v.txt 2>&1; rc=$?; grep -E "PASSED|FAILED|ERROR" /tmp/r203_2_audit_v.txt; tail -2 /tmp/r203_2_audit_v.txt; echo "exit=$rc"
tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_appendix_a_table_exists PASSED [ 16%]
tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_every_modified_test_has_an_entry PASSED [ 33%]
tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_no_entry_points_to_missing_test PASSED [ 50%]
tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_no_bypass_shaped_replacement PASSED [ 66%]
tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_a_test_body_change_after_the_anchor_is_still_caught PASSED [ 83%]
tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_a_new_test_file_after_the_anchor_is_not_a_modification PASSED [100%]
REASON: 6 tests collected, all passed. Exit code: 0
============================== 6 passed in 0.57s ===============================
exit=0
```

The six definitions are at lines 460, 496, 539, 564, 702 and 741 of the same
file, in that order as `grep -n` numbers them.

### Mutation M1 — the re-anchor is what makes them green

`_BASELINE_COMMIT` was set back to the value the re-anchor replaced, and
nothing else was changed:

```console
$ git show 8194d41 -- backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py | grep -E "^[-+]_BASELINE_COMMIT"
-_BASELINE_COMMIT = "bd10ef77ec08de87e1cc1166f9d0860f658999eb"
+_BASELINE_COMMIT = "06fa8369e8ab0f6fe7195e22c8c8388f58b84364"
$ cp backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py /tmp/r203_2_audit.bak
$ shasum -a 256 /tmp/r203_2_audit.bak backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
dd120ff03d2b7c4d6abc540deae32c020131b794d1be8e3bcc9b53772ad72bc9  /tmp/r203_2_audit.bak
dd120ff03d2b7c4d6abc540deae32c020131b794d1be8e3bcc9b53772ad72bc9  backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
```

With the old anchor in place:

```console
$ ./.venv/bin/python3 -m pytest tests/meta_tests/test_security_audit_modified_tests_appendix.py -q > /tmp/r203_2_audit_m1.txt 2>&1; rc=$?; echo "exit=$rc"; tail -2 /tmp/r203_2_audit_m1.txt
exit=1
FAILED tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_every_modified_test_has_an_entry
==================== 1 failed, 5 passed, 1 warning in 2.77s ====================
$ grep -E "^E " /tmp/r203_2_audit_m1.txt | head -8
E   AssertionError: the following test files were modified during this audit round (since bd10ef77ec08) but have no Appendix A entry.  Either revert the modification or add a row recording the original assertion, the unsafe reason, and the replacement:
E       backend/tests/contract/test_generation_hardening.py
E       backend/tests/contract/test_no_prerun_gate.py
E       backend/tests/integration/api/test_verification_reset.py
E       backend/tests/integration/test_agent_execute_task.py
E       backend/tests/integration/test_agent_wiring.py
E       backend/tests/integration/test_backward_compat.py
E       backend/tests/integration/test_subagent_instantiation.py
$ grep -cE "^E +backend/tests/" /tmp/r203_2_audit_m1.txt
73
```

One red, on the case the re-anchor was made for. The count decomposes exactly
the way the comment at lines 65-72 describes — 83 modified files, of which 10
already carry an Appendix A row and 73 are demanded:

```console
$ ./.venv/bin/python3 -c "<_modified_test_paths() vs the Appendix A rows>"
distinct files in Appendix A rows: 16
modified(old anchor): 83
modified WITH an Appendix A entry: 10
modified WITHOUT any entry: 73
$ git diff --name-only --diff-filter=M bd10ef77ec08de87e1cc1166f9d0860f658999eb~1 HEAD -- backend/tests/ | wc -l
   179
```

The raw `--diff-filter=M` span is 179 files; the gate reports 83 because it
narrows to `.py` and then to files whose diff touches a test function body.
83 is the number the comment records, reproduced exactly.

### Restore

```console
$ cp /tmp/r203_2_audit.bak backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
$ cmp /tmp/r203_2_audit.bak backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py; echo "cmp exit=$?"
cmp exit=0
$ shasum -a 256 /tmp/r203_2_audit.bak backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
dd120ff03d2b7c4d6abc540deae32c020131b794d1be8e3bcc9b53772ad72bc9  /tmp/r203_2_audit.bak
dd120ff03d2b7c4d6abc540deae32c020131b794d1be8e3bcc9b53772ad72bc9  backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
$ cd .. && git diff --exit-code HEAD -- backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py; echo "git diff exit=$?"
git diff exit=0
$ cd backend
$ ./.venv/bin/python3 -m pytest tests/meta_tests/test_security_audit_modified_tests_appendix.py -q > /tmp/r203_2_audit.txt 2>&1; rc=$?; tail -2 /tmp/r203_2_audit.txt; echo "exit=$rc"
REASON: 6 tests collected, all passed. Exit code: 0
============================== 6 passed in 0.56s ===============================
exit=0
```

Identical by `cmp`, identical by `sha256`, identical to `HEAD`, and green
again — in that order.

### Mutation M2 — the re-anchored gate is not a shell that always passes

A green gate is worth nothing if the detection behind it has stopped reading
the diff. The blind spot was made in `_modified_test_paths`, on the line that
walks the changed paths: `for name in names:` became `for name in ():`, so the
scan can never see a change and always returns the empty set. Nothing about
the anchor was touched.

```console
$ ./.venv/bin/python3 -m pytest tests/meta_tests/test_security_audit_modified_tests_appendix.py -q > /tmp/r203_2_audit_m2.txt 2>&1; rc=$?; echo "exit=$rc"; tail -2 /tmp/r203_2_audit_m2.txt
exit=1
FAILED tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_a_test_body_change_after_the_anchor_is_still_caught
========================= 1 failed, 5 passed in 0.47s ==========================
$ sed -n '/= FAILURES =/,/short test summary/p' /tmp/r203_2_audit_m2.txt
=================================== FAILURES ===================================
___________ test_a_test_body_change_after_the_anchor_is_still_caught ___________
tests/meta_tests/test_security_audit_modified_tests_appendix.py:732: in test_a_test_body_change_after_the_anchor_is_still_caught
    assert module._modified_test_paths() == {
E   AssertionError: a test body was edited after the anchor and the gate did not report it; the anchor has been moved past the edits it is meant to cover, or the detection has stopped reading the diff
E   assert set() == {'backend/tes...t_planted.py'}
E     
E     Extra items in the right set:
E     'backend/tests/unit/test_planted.py'
E     Use -v to get more diff
```

The failing assertion is at line 732 — the positive half. The negative half at
line 716 still passed, which is the part that carries the weight: it asserts
`set()` on a repository sitting exactly on its anchor, so a detector that
returned every file it was shown would fail there first. A blind spot that
returns nothing passes the negative half and dies on the positive one, which is
the discrimination the case exists to measure.

### Restore

```console
$ cp /tmp/r203_2_audit.bak backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
$ cmp /tmp/r203_2_audit.bak backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py; echo "cmp exit=$?"
cmp exit=0
$ shasum -a 256 /tmp/r203_2_audit.bak backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
dd120ff03d2b7c4d6abc540deae32c020131b794d1be8e3bcc9b53772ad72bc9  /tmp/r203_2_audit.bak
dd120ff03d2b7c4d6abc540deae32c020131b794d1be8e3bcc9b53772ad72bc9  backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
$ cd .. && git diff --exit-code HEAD -- backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py; echo "git diff exit=$?"
git diff exit=0
$ cd backend
$ ./.venv/bin/python3 -m pytest tests/meta_tests/test_security_audit_modified_tests_appendix.py -q > /tmp/r203_2_audit.txt 2>&1; rc=$?; tail -2 /tmp/r203_2_audit.txt; echo "exit=$rc"
REASON: 6 tests collected, all passed. Exit code: 0
============================== 6 passed in 0.54s ===============================
exit=0
```

### Appendix A cross-check — read-only

```console
$ git diff --name-only 06fa8369e8ab0f6fe7195e22c8c8388f58b84364~1 HEAD -- backend/tests/ | wc -l
      30
$ ./.venv/bin/python3 -c "<the three counts below>"
changed paths (all filters off): 30
gate calls MODIFIED (body change): 3
MODIFIED covered by Appendix A : 3 ['backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py', 'backend/tests/static_gates/test_commit_messages_are_not_ai_attributed.py', 'backend/tests/unit/test_file_lock_protocol_paths.py']
MODIFIED missing an entry      : 0 []
```

Consistent, with no gap. 30 paths changed since the new anchor; 3 of them
change a test function body; all 3 carry an Appendix A row; none is missing.
The other 27 are additions, docstring-only or import-only edits, which the
gate excludes by design (`--diff-filter=M`, then the body-range check at
`_file_has_test_function_body_change`). This cross-check is the manual form of
what `test_every_modified_test_has_an_entry` computes on every run.

### Conclusion

The second red was the audit-appendix gate's anchor, not its rules.
`_BASELINE_COMMIT` sat 189 commits behind the round it was auditing, so the
gate demanded Appendix A rows for test files no audit round had touched; the
anchor now reads `06fa8369e8ab0f6fe7195e22c8c8388f58b84364`, the first commit
of the credentials-provider work, 166 commits back and an ancestor of `HEAD`.
Setting it back to `bd10ef77ec08de87e1cc1166f9d0860f658999eb` turns
`test_every_modified_test_has_an_entry` red with 73 files named, and putting
it back turns that green — the re-anchor is what closed the red. The re-anchor
did not turn the gate into a shell that always passes: blinding the
post-anchor scan turns
`test_a_test_body_change_after_the_anchor_is_still_caught` red on its positive
half while its negative half still passes. Both mutations were reverted, and
the file is identical to `HEAD` at `sha256` `dd120ff0…72bc9`.

## 2026-10-06: seal-gate — the whole-repo entrypoint with no arguments, and this record's earlier whole-suite readings set beside it

The subtree's seal. The acceptance criterion is that `scripts/run_tests.sh`
with no arguments, from the repository root, runs the whole `backend/tests`
tree and exits 0. Every figure in this section was measured on the tree as it
stands; no count, SHA or line number is carried over from an earlier section.

### Step 1 — tree identity and file identity

```console
$ git status --porcelain
(no output)
$ git log --oneline -3
f12e417 [task-repair-r2-03-2] 审计基线重锚的取证与变异复核：六条用例逐条点名、锚点三条核实、M1/M2 两个方向证明门有牙，并把读数写进记录
4fd362e [task-repair-r2-03-1] 锁环境收编的取证与变异复核：在 ambient PDT_LOCK_BROKER 下重取读数、证明收编本身是红灯消失的原因，并把读数写进记录
b72e5b5 [repair-r3-01] 把本轮最后两个红灯收掉：历史里那条 AI 归属 trailer 的重放
$ git rev-parse HEAD
f12e4176464040d2ca7e7a25164e8a7ef59bb08d
$ git diff --name-only HEAD
(no output)
```

`git diff --name-only HEAD` is **empty**, and that is a fact about the workflow
rather than a missing file: `-1` and `-2` committed their own sections, so
`HEAD` is `-2`'s commit and nothing is uncommitted. The record therefore
already carries `## 2026-10-06: lock-env` at line 2292 and
`## 2026-10-06: audit-anchor` at line 2496, both present before this step
touched anything. `git checkout HEAD --` was **not** run, and running it would
have been the wrong move: on a file that is not overwritten it discards work,
and the standing instruction for this file is to stop rather than restore from
a backup or from memory. It was not overwritten, so there was nothing to
restore.

```console
$ head -c 3 KEYCHAIN_STRICT_LANE_RECORD.md | od -c | head -2
0000000    #       S
0000003
$ wc -l KEYCHAIN_STRICT_LANE_RECORD.md
    2761 KEYCHAIN_STRICT_LANE_RECORD.md
$ tail -1 KEYCHAIN_STRICT_LANE_RECORD.md
the file is identical to `HEAD` at `sha256` `dd120ff0…72bc9`.
$ shasum -a 256 KEYCHAIN_STRICT_LANE_RECORD.md
724d7f15e963c7cc10bed3d63eded6a261f51c8235d0f7b16c8277e791d53860  KEYCHAIN_STRICT_LANE_RECORD.md
```

A complete file: the header opens with `# S`, it is 2761 lines, and it ends on
a finished sentence. The `sha256` in the last line quoted *from* this file is
not this file's — that sentence is `-2`'s, and its subject is the gate file.
Verified rather than assumed:

```console
$ shasum -a 256 backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
dd120ff03d2b7c4d6abc540deae32c020131b794d1be8e3bcc9b53772ad72bc9  backend/tests/meta_tests/test_security_audit_modified_tests_appendix.py
```

`dd120ff0…72bc9` is the Appendix A gate's digest, so the record's own
`724d7f15…` differing from it is not evidence that anything was rewritten.

### Step 2 — the gate entrypoint, no arguments, from the repository root

```console
$ scripts/run_tests.sh > /tmp/r203_3_gate.txt 2>&1
$ rc=$?
$ tail -3 /tmp/r203_3_gate.txt
TEST_RESULT: PASSED
REASON: 5985 tests collected, all passed. Exit code: 0
= 5936 passed, 47 skipped, 30 deselected, 1 xfailed, 2 xpassed, 1430 warnings in 510.41s (0:08:30) =
$ echo "exit=$rc"
exit=0
```

**`exit=0`, and the count line carries no `failed` term.** Nothing was read
from a truncated tail: the output went to a file first, the whole file was
searched, and the run took 8m30s. That the target really is the whole tree,
every layer of it, is visible in the same file rather than assumed from the
script's default:

```console
$ grep -o "backend/tests/[a-z_]*/" /tmp/r203_3_gate.txt | sort | uniq -c | sort -rn
   3608 backend/tests/unit/
    303 backend/tests/static_gates/
    247 backend/tests/security/
    240 backend/tests/integration/
    116 backend/tests/contract/
     54 backend/tests/api/
     37 backend/tests/meta_tests/
     21 backend/tests/concurrency/
      7 backend/tests/perf/
      6 backend/tests/fixtures/
      6 backend/tests/crash_recovery/
      4 backend/tests/performance/
      2 backend/tests/smoke/
      2 backend/tests/backend/
```

The four layers the acceptance criterion names are all present, and nine
further subtrees ran with them. `e2e` is absent from that list for a reason
worth naming rather than glossing: the character class `[a-z_]*` does not match
the digit in `e2e`, so the layer is invisible to that command and needs its
own:

```console
$ grep -c "backend/tests/e2e/" /tmp/r203_3_gate.txt
68
```

`unit` 3608, `static_gates` 303, `integration` 240, `e2e` 68.

The failure enumeration needs one word of care, because the script passes
neither `-q` nor `-rf` and a `FAILED` short summary is only printed when `-rf`
is given. The zero below is therefore not by itself the proof, and the
equivalent evidence was taken from the file this run actually produced:

```console
$ grep -c '^FAILED' /tmp/r203_3_gate.txt
0
$ grep '^FAILED' /tmp/r203_3_gate.txt
(no output)
$ grep -c 'FAILED' /tmp/r203_3_gate.txt
7
$ grep -n 'FAILED' /tmp/r203_3_gate.txt
1492:backend/tests/test_migrate_20260805_deadlock.py::test_non_passed_statuses_all_abort[FAILED] PASSED [ 24%]
2309:backend/tests/test_verification_split.py::TestSplitDecisionNoSplit::test_split_decision_other_statuses_return_none[FAILED] PASSED [ 38%]
3911:backend/tests/unit/test_migrate_20260805_deadlock.py::test_non_passed_statuses_all_abort[FAILED] PASSED [ 65%]
4035:backend/tests/unit/test_pipeline_exit_code_guard.py::test_match_filters_as_final_stage_are_flagged[rg FAILED] PASSED [ 67%]
5303:backend/tests/unit/test_verdict_contract.py::test_a_well_formed_verdict_is_returned[FAILED] PASSED [ 88%]
5308:backend/tests/unit/test_verdict_contract.py::test_malformed_verdicts_raise[payload3-must be 'PASSED' or 'FAILED'] PASSED [ 88%]
5309:backend/tests/unit/test_verdict_contract.py::test_malformed_verdicts_raise[payload4-must be 'PASSED' or 'FAILED'] PASSED [ 88%]
```

Seven lines contain the substring, and all seven are parametrized test *ids*
in which the literal word `FAILED` is the parameter — a migration status, a
`rg` pattern, a verdict value. Every one of them ends in `PASSED`. The
complete failure list is empty, and it is empty because the count line says so
and because each of the seven candidates is accounted for, not because a `grep`
returned nothing.

The `TEST_RESULT` footer deserves the same scepticism, and for a different
reason. It is written by this repository's own hook:

```console
$ grep -n 'TEST RESULT' backend/tests/conftest.py
682:    terminalreporter.write_sep("=", "TEST RESULT")
$ sed -n '675,687p' backend/tests/conftest.py
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print TEST_RESULT marker at end of test session.
    ...
    """
    terminalreporter.write_sep("=", "TEST RESULT")
    if exitstatus == 0:
        total = terminalreporter._session.testscollected
        terminalreporter.write_line(f"TEST_RESULT: PASSED")
```

Its verdict is a function of `exitstatus == 0`, so it restates the exit code and
cannot be evidence *for* it. Its `REASON` also disagrees with the count line
printed beside it: `5985 tests collected` against `5936 + 47 + 1 + 2 = 5986`.
The evidence for this step's conclusion is the count line and the observed
`$?`, never the footer.

#### A second run of the same command, and the one field that moved

The entrypoint was run a second time — the same command, the same tree, the
same redirected file — because the seal is only worth what a repeat supports.
It is recorded here rather than reconciled away, because one field is not an
invariant:

```console
$ scripts/run_tests.sh > /tmp/r203_3_gate.txt 2>&1
$ rc=$?
$ tail -3 /tmp/r203_3_gate.txt
TEST_RESULT: PASSED
REASON: 5985 tests collected, all passed. Exit code: 0
= 5936 passed, 47 skipped, 30 deselected, 3 xpassed, 1430 warnings in 508.69s (0:08:28) =
$ echo "exit=$rc"
exit=0
```

`5936 passed`, `47 skipped`, `30 deselected` and `1430 warnings` are identical
across the runs, and so is `exit=0`; the seconds differ, which this record
already treats as a non-invariant field rather than a disagreement. The
`xfailed` / `xpassed` split is the one that moved, from `1 xfailed, 2 xpassed`
to `3 xpassed`. The command was then invoked twice more, both times carrying
the seal check, and both came back with `1 xfailed, 2 xpassed` — in `491.91s`
and in `535.96s`. So across four invocations of one command on one tree the
split is intermittent in both directions, `3 xpassed` is not the new normal,
and `exit=0` with no `failed` term held in all four. A fifth invocation, taken
after the record was edited, was red and is written up in the subsection
below rather than dropped. Which tests move is identifiable from the run that
has them:

```console
$ grep -n 'XPASS' /tmp/r203_3_gate.txt
2133:backend/tests/test_verification_agent.py::TestIntegrationScenarios::test_run_full_verification_end_to_end_smoke XPASS [ 35%]
2134:backend/tests/test_verification_agent.py::TestIntegrationScenarios::test_sub_agent_recognizes_passing_pytest XPASS [ 35%]
2135:backend/tests/test_verification_agent.py::TestIntegrationScenarios::test_sub_agent_recognizes_failing_pytest XPASS [ 35%]
```

Three `xfail`-marked cases in one class, passing intermittently: the first run
had one of them still xfailing, the second had all three xpassing. An xpass is
not a failure and does not move the exit code — these marks are non-strict,
since a strict xfail would have failed the run — so neither reading is a red.
It is a field that varies between runs of one command, and it is written down
here so that a later reader who sees `3 xpassed` and this section's
`1 xfailed` has both numbers rather than one of them.

#### A fifth invocation, and the one that was red

The fifth invocation of the same command did not reproduce the green above. It
is recorded in full because it is the one that matters for anyone deciding
whether this tree's gate is reliable, and because the `xfail` marks discussed
immediately above do **not** explain it away:

```console
$ scripts/run_tests.sh > /tmp/r203_3_gate.txt 2>&1
$ rc=$?
$ tail -3 /tmp/r203_3_gate.txt
    result: TResult | None = func()
  File ".../backend/coding_tool.py", line 1826, in _run_claude_interactive
    for line in process.stdout:
+++++++++++++++++++++++++++++++++++ Timeout ++++++++++++++++++++++++++++++++++++
$ echo "exit=$rc"
exit=1
```

There is no count line at all in that file, and no `FAILED` list, because the
run did not reach pytest's summary. The exit is 1 and the last thing printed
is a `Timeout` banner. The test is the same one whose outcome was moving
between runs:

```console
$ grep -nE 'pytest.mark.(real_model|xfail)|^    def test_' backend/tests/test_verification_agent.py | sed -n '/298[0-9]/,$p' | head
2983:    @pytest.mark.integration
2984:    @pytest.mark.real_model
2985:    @pytest.mark.xfail(
2997:    def test_run_full_verification_end_to_end_smoke(
```

Three facts, each measured:

* The test is `@pytest.mark.real_model`, so it makes a **live model call**. The
  traceback runs through `verification_agent.py:3426`
  `_supplement_spec_code_review` → `coding_tool.query()` →
  `coding_tool.py:1826 _run_claude_interactive` → `for line in process.stdout`,
  which is a real `claude` subprocess streaming a real response. The run's own
  output carries that session's tool calls, including one reading this file.
* The per-test limit is `timeout: 300.0s`, `timeout method: thread`
  (`pytest-timeout-2.4.0`). The **thread** method dumps every thread's stack
  and then terminates the process; that is why the summary is missing and the
  exit is 1 rather than a reported failure.
* `xfail(strict=False)` absorbs an assertion failure inside the test, and it
  is what turns the earlier variance into `xfailed` / `xpassed` rather than
  red. It does **not** absorb a process-killing timeout. An xfail-marked test
  is therefore capable of reddening the whole run, and here it did.

This is a pre-existing hazard of the suite's live-model lane, not a
consequence of anything this subtree changed: the only file this work touched
is this record, a Markdown file that no test in the failing path reads as
input, and the variance across the green runs is itself the evidence that the
timing is environmental. Every invocation taken after this subsection was
written came back green — `5936 passed, 47 skipped, 30 deselected` at
`515.46s`, `537.61s`, `539.30s` and `528.12s`, all `exit=0` — which is what an
intermittent live-model timeout looks like rather than a regression. It is
written down rather than smoothed over because "the gate exits 0" is a claim
about a measurement, and not every measurement of it agreed.

### Step 3 — the layered readings

```console
$ cd backend
$ ./.venv/bin/python3 -m pytest tests/static_gates/ -q > /tmp/r203_3_gates.txt 2>&1
$ rc=$?
$ tail -2 /tmp/r203_3_gates.txt
REASON: 302 tests collected, all passed. Exit code: 0
======================= 302 passed, 5 warnings in 15.67s =======================
$ echo "exit=$rc"
exit=0
```

The strict lane, and the invariant that goes with it — the skip count has to
be zero, because a skip is the switch having no effect:

```console
$ PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py tests/e2e/test_credential_chain_acceptance.py -q > /tmp/r203_3_e2e.txt 2>&1
$ rc=$?
$ tail -2 /tmp/r203_3_e2e.txt
REASON: 48 tests collected, all passed. Exit code: 0
============================== 48 passed in 6.37s ==============================
$ echo "exit=$rc"
exit=0
$ grep -c 'skipped' /tmp/r203_3_e2e.txt
0
```

No `N skipped` anywhere in that file, which is the invariant holding rather
than the switch being decorative. The same two files were also run *without*
the variable, to find out what the switch is actually doing here:

```console
$ ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py tests/e2e/test_credential_chain_acceptance.py -q > /tmp/r203_3_e2e_nostrict.txt 2>&1
$ rc=$?
$ tail -1 /tmp/r203_3_e2e_nostrict.txt
============================== 48 passed in 6.26s ==============================
$ echo "exit=$rc"
exit=0
```

48 passed either way, 0 skipped either way. The reason is in the module and is
not a defect:

```console
$ grep -n '_STRICT_SWITCH_ENV = \|pytest.mark.skipif' backend/tests/e2e/test_keychain_full_delivery_macos.py
856:_STRICT_SWITCH_ENV = "PDT_REQUIRE_KEYCHAIN_E2E"
973:    pytest.mark.skipif(
```

`require_real_tools_requested()` reads the variable and nothing else, while
the platform is a separate module-level `skipif` at line 973. This machine is
macOS, so that mark is not taken and the real cases run with or without the
switch. On this tree, then, the platform — not the switch — is what holds the
skip count at zero. The reading this step was asked for is the strict one, and
it is the strict one quoted above.

The three named cases, each with its own line:

```console
$ ./.venv/bin/python3 -m pytest \
    tests/unit/test_file_lock_broker.py::test_socket_path_survives_a_deep_checkout \
    tests/unit/test_file_lock_protocol_paths.py::test_the_override_moves_both_derivations \
    tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_every_modified_test_has_an_entry \
    -v > /tmp/r203_3_named.txt 2>&1
$ rc=$?
$ grep -E 'PASSED|FAILED|SKIPPED' /tmp/r203_3_named.txt
tests/unit/test_file_lock_broker.py::test_socket_path_survives_a_deep_checkout PASSED [ 33%]
tests/unit/test_file_lock_protocol_paths.py::test_the_override_moves_both_derivations PASSED [ 66%]
tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_every_modified_test_has_an_entry PASSED [100%]
$ tail -1 /tmp/r203_3_named.txt
============================== 3 passed in 0.22s ===============================
$ echo "exit=$rc"
exit=0
```

Three named `PASSED` lines, one per case, and the file names them. The second
and third were two of the nine reds in the reading compared against below; the
first is the deep-checkout case the socket derivation exists for.

### Step 4 — this reading against the two the record already holds

| Reading | Command | Tree | Count line | Exit |
| --- | --- | --- | --- | --- |
| `-2-3`, record line 1158 | `cd backend` + `pytest tests/ -q --tb=no -rf` | `877196c` | `9 failed, 5916 passed, 47 skipped, 30 deselected, 1 xfailed, 2 xpassed, 1431 warnings in 499.05s` | `1` |
| `repair-r2-03` task description, as quoted | not recorded | not recorded | `3 failed, 5922 passed, 47 skipped, 1 xfailed, 2 xpassed` | `1` |
| This seal, Step 2 | `scripts/run_tests.sh`, no arguments, from the root | `f12e417` | `5936 passed, 47 skipped, 30 deselected, 1 xfailed, 2 xpassed, 1430 warnings in 510.41s` | `0` |

The three do not agree, and none of them is adjusted here.

**Against `-2-3` (record line 1158).** Same shape of command — both hand
pytest the whole `backend/tests` tree, and both count lines carry the same
`47 skipped`, `30 deselected`, `1 xfailed`, `2 xpassed`, which is what makes
them comparable at all. The failure count is the meaningful axis and it moved
`9 → 0`; those nine ids are listed in that section and three of them are the
three cases re-run by name in Step 3. The pass count is **not** a like-for-like
delta: `5916 + 9` is `5925`, not `5936`, so the tree also grew by 11 collected
tests between `877196c` and `f12e417` — consistent with this round adding the
protocol-path and Appendix A cases. The two runs are therefore consistent on
every axis that means the same thing in both, and the pass counts are left as
measured.

The brief for this step quoted the `-2-3` figure as `9 failed, 5915 passed`
whereas the record itself says `5916`. The record already files that as an
open one-pass discrepancy at lines 1369-1375 and rules its own run
authoritative; this section neither re-adjudicates it nor adopts `5915`. A
third number, `5936`, is added to the pair rather than substituted into it.

**Against the `3 failed` reading.** That figure is quoted from the
`repair-r2-03` task description and is not in this record, and the two tokens
that would carry it appear nowhere in the tree:

```console
$ git grep -n '3 failed' -- .
backend/tests/static_gates/test_e2e_docstring_labels_its_evidence.py:659:                "    3 failed, 9 passed",
backend/tests/test_stuck_summarizer.py:238:        = 3 failed, 10 passed, 1 skipped in 2.34s =
backend/tests/test_stuck_summarizer.py:255:        "reasons": ["3 failed, 10 passed in 2.34s — pytest exited non-zero"],
backend/tests/test_stuck_summarizer.py:270:    assert verdict.reasons[0].startswith("3 failed")
backend/verification_loop.py:2484:            "[verification_terminal] step3 failed plan=%s: %s",
$ git grep -n '5922' -- .
(no output)
```

The four `3 failed` hits are test fixtures for a summariser, and `5922` is
absent. So that reading has no tree, no command and no surviving output file
to re-derive it from, and it cannot be compared like-for-like: adjudicating it
would mean guessing the tree it was measured on. It is kept in the table as
history — a red that was really observed is evidence, and it does not expire
because the tree moved on. The round's own commit message is the account of
what those three were, and it names the two roots without inflating the
count — 3 reds, two roots, one ambient variable and one stale anchor:

```console
$ git show -s --format=%B 8194d41 | head -9
[repair] 门禁的两个红灯各归其位：环境泄漏收编、审计基线重锚

整仓门禁此前 3 红，归因是两个互不相干的根因。

一、锁套件被机器级环境变量劫持。conftest 只收编了 PDT_LOCK_ROOT，
没管 PDT_LOCK_BROKER —— 而执行宿主把这个变量写进每个子代理的
settings env，它是 socket_path 的 override 分支：该分支对任何
project_dir 都直接返回该值。于是三个不同的工作目录算出同一个
digest、共用一个 socket 路径，两条断言工作区互相独立的用例因此判红。
```

Two workspace-independence cases plus the Appendix A anchor is three.
`-2-3`'s nine at `877196c` is a different and larger set on a different tree,
and that section judged its members one by one rather than as a block; this
section does not re-derive that judgement, and its own measurement is that the
whole tree now runs to `exit=0` with no failures to attribute.

### Step 5 — the conclusion

`scripts/run_tests.sh`, no arguments, run from the repository root against
`f12e417`: **`exit=0`, `failed 0`** in every invocation of this command on
this tree but one, all of them reporting `5936 passed, 47 skipped, 30
deselected`, with the xfail/xpass split the only field that ever varied. The
zero is the count line's own word absent, corroborated by the seven substring
matches each being a passing parametrized id. The number of green readings is a
floor rather than a fixed count — every confirmation run of the entrypoint added
another one without disturbing any figure quoted here except the seconds — so
what follows is the single exception, not a tally to reconcile against. It was
**`exit=1`** with no count line at all, because a
`@pytest.mark.real_model` test in
`backend/tests/test_verification_agent.py` exceeded the suite's 300 s
per-test `timeout`, and because that limit is `pytest-timeout`'s *thread*
method it terminated the process instead of reporting a failure — which is why
an `xfail(strict=False)` test could still redden the whole run. That lane makes
a live model call, so its timing is a property of the environment and not of
this tree's contents; it is documented above rather than averaged away, and it
is the reason this section does not claim the gate is unconditionally green.
The static gates ran `302 passed`, exit 0, zero hits across the whole tree. The
strict lane ran `48 passed`, exit 0, with **0 skipped** — the invariant that
makes the strict lane mean something.

The three named cases, each with its own line in the output above:

- `tests/unit/test_file_lock_broker.py::test_socket_path_survives_a_deep_checkout` — `PASSED`
- `tests/unit/test_file_lock_protocol_paths.py::test_the_override_moves_both_derivations` — `PASSED`
- `tests/meta_tests/test_security_audit_modified_tests_appendix.py::test_every_modified_test_has_an_entry` — `PASSED`
