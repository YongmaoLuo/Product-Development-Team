# Mutation negative controls for the Tier A decision tables

§12.7 of the judgment-surface handoff asks for one negative control per
decision table: flip a branch on purpose, watch the table go red, put
the module back byte for byte, and check the hash.  This page is the
record of those runs — the command that was executed and what it saw.

## Why the record exists

A decision table is only worth running if it can *fail*.  A table that
cannot fail is a comment with a `pytest` decorator on it: it runs on
every commit, it costs nothing, and it pins nothing.  The only honest
way to show that a table carries weight is to break the function on
purpose and watch the table notice.

The probe does that in three steps, and the order is the design:

1. **Mutate** — change one line of the module under test: the smallest
   change that flips the function's conclusion.
2. **Run** — run the table's test file and record whether it went red.
3. **Restore** — put the module back byte for byte and check the digest.

Step 3 is what makes step 1 safe to run against a real tree.  A probe
that leaves a mutated file behind has changed the thing it was
measuring: the next run would measure the mutant, and the mutant is
not the code anybody ships.  So the restore is *verified* — the bytes on
disk are compared with the bytes read before the mutation, and the
digest is compared with the digest taken before the mutation.  A
restore that only looks right is not a restore.

## The nine runs

Every row below was executed from the repository root with the probe in
`scripts/judgment_mutation_probe.py`.  Each run exited `0`, which is
the probe's own contract: the table went red **and** the module was
restored.  Each run left the working tree exactly as it found it.

| table | function | mutation | command | RED | sha256 |
| --- | --- | --- | --- | --- | --- |
| `backend/tests/unit/test_decision_table_ran_declared_entry.py` | `_ran_the_declared_entry` | `return False` -> `return True` (backend/verification_evidence.py:976) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_evidence.py --line 976 --old 'return False' --new 'return True' --test backend/tests/unit/test_decision_table_ran_declared_entry.py` | RED | `sha256 ok 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82 == 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82` |
| `backend/tests/unit/test_decision_table_projected_ran_declared_entry.py` | `_projected_ran_the_declared_entry` | `return False` -> `return True` (backend/verification_evidence.py:932) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_evidence.py --line 932 --old 'return False' --new 'return True' --test backend/tests/unit/test_decision_table_projected_ran_declared_entry.py` | RED | `sha256 ok 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82 == 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82` |
| `backend/tests/unit/test_decision_table_apply_subject_check.py` | `apply_subject_check_to_verdict` | `if not reason:` -> `if reason:` (backend/verification_evidence.py:1087) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_evidence.py --line 1087 --old 'if not reason:' --new 'if reason:' --test backend/tests/unit/test_decision_table_apply_subject_check.py` | RED | `sha256 ok 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82 == 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82` |
| `backend/tests/unit/test_decision_table_verdict_subject_mismatch.py` | `verdict_subject_mismatch` | `in bare:` -> `not in bare:` (backend/verification_evidence.py:1047) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_evidence.py --line 1047 --old 'in bare:' --new 'not in bare:' --test backend/tests/unit/test_decision_table_verdict_subject_mismatch.py` | RED | `sha256 ok 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82 == 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82` |
| `backend/tests/unit/test_decision_table_excess_declared_paths.py` | `excess_declared_paths` | `not norm or norm in declared_paths` -> `not norm` (backend/agent.py:1815) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/agent.py --line 1815 --old 'not norm or norm in declared_paths' --new 'not norm' --test backend/tests/unit/test_decision_table_excess_declared_paths.py` | RED | `sha256 ok 436f98b33418559b4909996179912fad32deb952d24fdeff880dbbe8c9861f65 == 436f98b33418559b4909996179912fad32deb952d24fdeff880dbbe8c9861f65` |
| `backend/tests/unit/test_decision_table_task_declared_files_exist.py` | `_task_declared_files_exist` | `return all(` -> `return any(` (backend/agent.py:10137) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/agent.py --line 10137 --old 'return all(' --new 'return any(' --test backend/tests/unit/test_decision_table_task_declared_files_exist.py` | RED | `sha256 ok 436f98b33418559b4909996179912fad32deb952d24fdeff880dbbe8c9861f65 == 436f98b33418559b4909996179912fad32deb952d24fdeff880dbbe8c9861f65` |
| `backend/tests/unit/test_decision_table_derive_index_buckets.py` | `derive_index_buckets` | `status, vp_id=vp_id, retired=retired` -> `status, vp_id=vp_id` (backend/verification_buckets.py:210) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_buckets.py --line 210 --old 'status, vp_id=vp_id, retired=retired' --new 'status, vp_id=vp_id' --test backend/tests/unit/test_decision_table_derive_index_buckets.py` | RED | `sha256 ok 2ad23565558dad6f0a0e1d5d4396a18aa7fe07c2d8204af04880804f8ed662ca == 2ad23565558dad6f0a0e1d5d4396a18aa7fe07c2d8204af04880804f8ed662ca` |
| `backend/tests/unit/test_decision_table_gate_block_reason.py` | `_gate_block_reason_for` | `or self._gate_predecessor_reason(gate_order)` -> `and self._gate_predecessor_reason(gate_order)` (backend/verification_executor.py:4421) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_executor.py --line 4421 --old 'or self._gate_predecessor_reason(gate_order)' --new 'and self._gate_predecessor_reason(gate_order)' --test backend/tests/unit/test_decision_table_gate_block_reason.py` | RED | `sha256 ok bb77f6f221f26dea7adbd1302194c2789f7c18732a3ce41f98090d9a98d1ff85 == bb77f6f221f26dea7adbd1302194c2789f7c18732a3ce41f98090d9a98d1ff85` |
| `backend/tests/unit/test_decision_table_rerun_deferred_gate_layers.py` | `rerun_deferred_gate_layers` | `if reason:` -> `if not reason:` (backend/verification_executor.py:4483) | `backend/.venv/bin/python3 scripts/judgment_mutation_probe.py --module backend/verification_executor.py --line 4483 --old 'if reason:' --new 'if not reason:' --test backend/tests/unit/test_decision_table_rerun_deferred_gate_layers.py` | RED | `sha256 ok bb77f6f221f26dea7adbd1302194c2789f7c18732a3ce41f98090d9a98d1ff85 == bb77f6f221f26dea7adbd1302194c2789f7c18732a3ce41f98090d9a98d1ff85` |

## What each mutation breaks

Every mutation is the smallest change that turns one of the function's
answers into its opposite — a flipped condition, an inverted branch, a
clause dropped.  None of them touches a test file: the table is what
notices, and a table that only notices because the test was edited is
not a table.

- `_ran_the_declared_entry` — a point that declares no entry now claims
  it ran one, so it would be exempted from the subject check.
- `_projected_ran_the_declared_entry` — a browser point now counts as a
  runner record, so prose would buy an exemption it never earns.
- `apply_subject_check_to_verdict` — a verdict that *is* on subject now
  gets downgraded to FAILED, because the "no reason" guard is gone.
- `verdict_subject_mismatch` — a bare file name now counts as a match
  for a file it is not, so the rescue branch fires in the wrong place.
- `excess_declared_paths` — a declared path now counts as an undeclared
  write, because the exemption clause is gone.
- `_task_declared_files_exist` — one existing file now passes for all of
  them, because `all` became `any`.
- `derive_index_buckets` — a point the plan retired now lands in a
  bucket instead of being dropped, because the retirement map is no
  longer consulted.
- `_gate_block_reason_for` — the predecessor reason now wins over the
  global blocker, because the two are combined with `and` instead of
  `or`.
- `rerun_deferred_gate_layers` — a blocked layer now runs and a free
  layer now waits, because the "still blocked" test is inverted.

## The columns

- **table** — the decision table that was measured.  It is the table the
  manifest (`docs/judgment_points.json`) names for the function, so the
  report and the manifest cannot drift apart silently.
- **function** — the judgment function the mutation was made in.
- **mutation** — the old text, the new text, and the file and line they
  live on.
- **command** — the probe invocation, exactly as it was run, from the
  repository root.  Copy it and it runs.
- **RED** — the table went red under the mutation.  A row that says
  anything else is a negative control that did not bite.
- **sha256** — the probe's verdict, then the digest it took before the
  mutation and the digest it took after the restore.  The two are the
  same on every row: that is what "the module was put back" means.

## Reproducing a row

Run the command from the table's `command` column from the repository
root.  The probe prints its own verdict:

```
MUTATION PROBE
module: backend/verification_evidence.py
line: 976
old: return False
new: return True
test: backend/tests/unit/test_decision_table_ran_declared_entry.py
RED: yes
RESTORED: yes
SHA256: match
SHA256_BEFORE: 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82
SHA256_AFTER: 4eb2448a814e0fe6e10f5b8b39cca6b6e34ae0b0cdfc49e01a5eaa3463100b82
PYTEST_EXIT_CODE: 1
TIMED_OUT: no
```

The exit codes are the contract: `0` — the table went red and the
module was restored; `1` — the table stayed green, so the table does
not carry weight; `2` — the probe could not be run, or the module was
not restored.

## The gate that keeps this page honest

`backend/tests/static_gates/test_judgment_mutation_controls_report.py`
reads this page and the manifest, and refuses to let it drift:

- every Tier A table in the manifest has exactly one row here, and this
  page has no other rows — a report that quietly skips a table reads
  like a complete audit;
- each row's command names the probe, and its `--module` and `--test`
  are that row's own module and table, so a row cannot point at
  somebody else's run;
- each row records `RED` and two equal digests, so a run that did not
  bite, or a module that was not put back, cannot be written down as a
  passing negative control.

The gate never runs the probe: a gate that mutated the modules it
audits would be changing the thing it measures.  It also does not
re-hash the modules against the digests recorded here — `backend/agent.py`
changes every few weeks, and a report that went red on an unrelated
edit would be a report nobody keeps.  Re-running the probe is how a row
is refreshed.
