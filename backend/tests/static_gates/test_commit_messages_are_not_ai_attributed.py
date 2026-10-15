"""Commit messages carry no AI attribution trailer (2026-09-28).

Why this gate exists
--------------------
Commits in this repository ended with a trailer naming a specific model::

    Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

The committer cannot know that. The *tool* is Claude Code; the model behind
any given commit is a routing decision made elsewhere and is not recorded in
the commit. Naming one is a guess written in the grammar of a fact, and it
lands in permanent public history where it cannot be corrected.

The rule is deliberately narrow, and these tests pin the narrowness as
firmly as the catch: the checker inspects the **attribution trailer**, not
the prose. A commit message may discuss Claude Code, models, providers or
AI at length — see ``test_leaves_prose_about_the_tool_alone``. Only a line
that claims authorship is rejected.

What is pinned, and where each piece lives
------------------------------------------
``scripts/check_commit_msg.py`` is the single implementation. It is reached
three ways, and a rule with three entry points needs a test on each or one
of them silently rots:

* ``.git/hooks/commit-msg`` — installed by ``scripts/install_git_hooks.sh``
  (the native path, for a clone without the pre-commit framework);
* ``.pre-commit-config.yaml`` — the ``commit-msg`` stage hook;
* ``.github/workflows/ci.yml`` — a job over the pushed range, which is what
  catches ``git commit --no-verify`` and a machine with no hooks at all.

The last test walks **this repository's own history**, so the gate is not
only about future commits: it is also the assertion that the history we
publish is clean.

Refs other than HEAD (2026-10-05)
--------------------------------
**The figures in this section were taken on 2026-10-05 and every one of
them has since moved, so they are labelled as readings rather than stated
as current.** At the time it reported 0 violations over HEAD-reachable
history, and that 0 was the module's own per-commit iteration speaking —
not a text search over the log. The walk behind that figure covered 144
commits. That count moves with every commit, exactly as the figures below
do, and it is quoted so that "0 of 144" is distinguishable from "0 of
nothing" — a walk that reached no commits would satisfy the same rule.
But that walk reaches what HEAD reaches and nothing else, so its clean
result is a statement about one ref. Listing every ref and running
the same checker over each ref's own history found three still carrying
the trailer this rule forbids, none of them reachable from HEAD
(``git merge-base --is-ancestor`` returns 1 for every offender):
``refs/heads/backup/pre-ai-trailer-strip-20261005`` (3 commits),
``refs/heads/backup/pre-fd-secrets-sync-20261004`` (15), and
``refs/original/refs/heads/main`` (1, tip ``f5cad96``) — 19 offending
commits, and no two refs share one. No ``refs/remotes/**`` ref carries a
violation, so all three are local-only: no remote-tracking counterpart,
never pushed. **That inventory is historical on two counts now.** The
residue ref has been deleted (see *The dispositions*), and a third
landing has put ``refs/heads/main`` back in the sweep, so the figure is
again three refs — but not the three above::

    $ git rev-list HEAD | wc -l
    151

    refs/heads/backup/pre-ai-trailer-strip-20261005   3
    refs/heads/backup/pre-fd-secrets-sync-20261004   15
    refs/heads/main                                  1   (f24ecc3)

The two backup counts are unchanged, and the total is back to 19 by
coincidence of arithmetic — one commit left the tree with the ref that
held it and one arrived on ``main``, so a reader who checked only the
total would conclude nothing had changed and would be wrong about which
ref is red. ``refs/heads/main`` is reachable from HEAD
(``git merge-base --is-ancestor f24ecc3 main`` returns 0), which is the
one clause above that no longer holds, and the reason a "0 violations"
figure cannot be quoted in the present tense at all. The nine-file and
nineteen-commit figures are left as they were written, because they
record what the sweep found at the time and the count belongs to that
reading, not to this one.

Two consequences follow. The first is the push rule: publishing any ref
that carries the trailer would publish the history it retains, so none of
them may be pushed, and a clean walk of HEAD says nothing about them. The
two declared backup refs still exist and the push rule binds them; it is
restated where their disposition is recorded. The ``refs/heads/main``
offender is *not* covered by that rule, because ``main`` is the ref that
gets pushed by design — the distinction is that it is unpushed so far,
not that it is exempt, and ``git for-each-ref --format='%(refname)'
--contains f24ecc3`` naming only ``refs/heads/main`` is the evidence that
it has not been. The second consequence is that a ref sweep is not a
complete inventory — a further commit carrying the trailer, ``e9c3cba``,
resolves from no ref at all, so it is reachable from nothing and no walk
over the ref list would ever find it. That is the reason the HEAD walk is
a separate test rather than a special case of the sweep.

These counts come from iterating history and calling
``find_violations`` per commit — the path ``_history_messages()`` takes
below. Searching the log text is not the same measurement: a commit
whose body merely *discusses* the trailer is legitimate prose under
``test_leaves_prose_about_the_tool_alone``, and a text search counts
those as offenders.

The gate over those refs (2026-10-05)
-------------------------------------
That inventory was prose, and prose is what the three tests below were
written against. ``_ALLOWED_TRAILER_REFS`` declares the two backup refs —
and only those two — as permitted to retain the trailer, each with its
one-line reason, and ``test_only_the_declared_refs_retain_the_trailer``
walks every name from ``git for-each-ref`` and requires the refs carrying
violations to be a subset of that set. The realistic way a third one
appears is another ``backup/pre-*`` branch taken before a later rewrite:
the shape that turns this red is the same mistake, one task on.

**The sweep covers two rules, and each ref is judged against the rules it
is actually exempt for.** ``find_violations`` rejects an AI attribution
trailer *and* a date-prefixed plan id, and a declaration in the table
above exempts a ref from named rules only — so a ref that is red for a
plan id is still red after being added to a table whose name says
*trailer*. ``test_a_plan_id_finding_survives_the_trailer_allowlist`` is
the control for that boundary, and it pins a property that used to be
unstated anywhere: the sweep put both rules' findings in one list, the
message called all of them trailers and gave all of them the trailer's
remedy, and a reader who followed that advice granted a plan-id finding
a permanent exemption under a heading that never described it.

``test_no_remote_tracking_ref_retains_the_trailer`` is the push rule in
the only form that survives after the event. A ``refs/remotes/**`` ref
carrying a violation means the history is already on the remote, and
rewriting locally does not recall it.

Both are pinned against passing vacuously. If the ref enumeration returns
nothing, or the HEAD walk returns no commits, the tests fail with "the
scan is broken" rather than reporting a clean bill.
``test_the_ref_sweep_reports_a_planted_trailer`` is the negative control
and exists because of what the other two have in common: "only the
declared refs retain the trailer" is a sentence a helper that finds
nothing also satisfies. So a real violation is planted in a throwaway
repository under ``tmp_path`` and the same helper is required to report
it. Both mutations were run and both are caught — breaking the helper
turns the control red, and making the enumeration return nothing turns
both ref tests red.

A red that was real, and is now historical
------------------------------------------
**The red below was seen, not predicted. It is historical now, and the
thing that made it historical is named: the residue ref was deleted, and the
trailer was stripped from ``main``.** Both dispositions are recorded in
their own sections below; they are cross-referenced here rather than
repeated, because a red that was actually seen stays in the file even after
it is fixed — deleting it would leave a reader unable to tell whether the
gate ever fired. Do not read this section as a claim that the file is
green: **this file is red again today**, for a third landing of the
trailer on ``main`` that is neither of the two dispositions below. The
historical red and the current one are different findings and are kept
apart on purpose.

The red was first run at 15:39 and again at 16:52 on 2026-10-05, with the
same single test failing both times on the same undeclared ref. Re-run from
``backend/``::

    ./.venv/bin/python3 -m pytest tests/static_gates/test_commit_messages_are_not_ai_attributed.py -q
    1 failed, 17 passed      # exit code 1

    ./.venv/bin/python3 -m pytest tests/static_gates/ -q
    1 failed, 274 passed, 5 warnings      # exit code 1

Seventeen of this file's eighteen tests passed then, and the one that did
not was ``test_only_the_declared_refs_retain_the_trailer``. That was the
correct result rather than a defect in the test:
``refs/original/refs/heads/main`` existed and carried one trailer-bearing
commit (``f5cad96a``). The cleanup that was to remove that residue had not
been applied — there was no commit for it anywhere in the log, and the ref
was written at 13:31, before the 15:39 measurement above saw it, so the
residue predated these tests rather than being something they created.

``refs/original/**`` is never a legitimate entry in
``_ALLOWED_TRAILER_REFS`` — the residue of a rewrite is not an audit
trail — so widening the declared set to make this green would re-open
exactly the hole the gate closes. That remains true after the ref is
deleted, and it is why the fix below was a deletion and not a
declaration.

"Delete that ref" looked like the obvious remedy and read as *not*
available, which is why this was reported rather than fixed at the time.
``f5cad96`` was reachable from no other ref: ``git merge-base
--is-ancestor f5cad96 <ref>`` succeeded for that one ref and for none of
the other seventeen, and the rewrite that created the residue left no
twin on ``main`` — no commit there carried its subject. On that reading
deleting the ref would have destroyed the only copy of that commit in
this repository, which read as a history decision and not a test edit.

**That reading argued from the wrong noun, and the correction is the
point.** Object uniqueness is not the question. Every residue commit
retained by a rewrite is reachable from exactly one ref — that is what
makes it residue — so the fact carries no information and cannot support
a decision to keep one. The question is whether its *content* exists
anywhere else, and re-read on 2026-10-05 it does::

    $ git log -1 --format='%H %P' f5cad96
    f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87 48c65a6736e96e14e9fa373bc57bc2986bfc6b44

    $ git merge-base --is-ancestor 48c65a6 main
    rc=0

    $ git diff --stat 48c65a6 f5cad96
     .../tests/e2e/test_keychain_full_delivery_macos.py | 10 ++++++++++
     1 file changed, 10 insertions(+)

    $ git grep -n "All four were executed rather than transcribed" main -- \
        backend/tests/e2e/test_keychain_full_delivery_macos.py
    main:.../test_keychain_full_delivery_macos.py:220:All four were executed ...

The commit's *subject* still has no twin on ``main``; its *content* does.
The ten lines it added are the ones at line 220 of the same file on
``main`` today. So the residue was unique in message and superseded in
content, and deleting it destroyed nothing — which is the reverse of what
"reachable from no other ref" implied on its own.

**What actually resolved that reading.** The premise was right about the
object and wrong about the remedy twice over: ``git update-ref -d``
removes the *name*, not the commit, and the content question above
removes the reason for keeping the name. The two dispositions, in the
order they were applied, were:

* the residue ref deleted under task 20-5-12-3-2, after which ``f5cad96``
  became an unreachable object rather than a lost one; and
* the trailer stripped from ``main`` under the task that stripped it, by
  a tree-pinned replay that rewrote only the messages of the offending
  commits and left every file byte identical (see *A trailer that landed
  on main* below).

Each is named by what it changed as well as by the number that scheduled
it: the number is this repository's bookkeeping and says nothing about
whether the change happened, and the change is the part a reader has to
be able to check.

**The reading now, re-verified rather than expected.** The residue is
gone, and re-read on 2026-10-05 rather than inferred from the absence
of a red::

    $ git for-each-ref --format='%(refname) %(objectname)' refs/original
    (no rows)

    $ git for-each-ref --format='%(refname)' | grep -i original
    (no rows; exit 1)

    $ git for-each-ref --format='%(refname)' --contains f5cad96
    (no rows)

    $ git cat-file -t f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87
    commit

The third command is the one that says the deletion landed: before it,
``--contains f5cad96`` named ``refs/original/refs/heads/main`` and that
was the whole finding. It now names nothing, and the object still
resolves. ``git update-ref -d refs/original/refs/heads/main`` is the
command that did it (see *The dispositions* below), and
**``f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87`` is the SHA that restores
the ref** — nothing points at it, so it will be pruned at the next
``git gc``. ``git reflog show refs/original/refs/heads/main`` fails
with "unknown revision", which is the same absence seen from the
reflog side.

**What a green run of this file now means, stated before anyone looks.**
It means the residue is gone. It does not mean this file passes: the
gate is unchanged, and it is red for a different reason, named in *A
trailer that landed on main* below.

The counts, and which is which
---------------------------
Four figures appear in the history of this section and only the last is
current; they are not interchangeable, so each is labelled with where it
came from. The pre-gate baseline quoted for this suite was 258 passed /
exit 0 — carried over from an earlier record, not re-measured here.
Running the suite directly before this file gained its three ref tests
printed 269 passed, exit 0. Adding those three gives the 272 collected at
the 15:39 reading, which is why 271 passed and 1 failed is that baseline
with the new tests included and one of them failing by design. The 275
collected / 274 passed / 1 failed quoted in the red above was current for
the window in which ``test_keychain_macos_job_is_a_merge_gate`` had gained
three tests of its own in the same lane. This file contributes the same
eighteen tests throughout — no count below moved because anything was
added or removed here.

**The current reading for this file**, re-run from ``backend/`` on
2026-10-05, is **red**, and the red is a third landing rather than either
disposition above failing::

    ./.venv/bin/python3 -m pytest tests/static_gates/test_commit_messages_are_not_ai_attributed.py -q
    2 failed, 16 passed      # exit code 1

    FAILED test_no_commit_in_this_history_carries_an_ai_trailer
    FAILED test_only_the_declared_refs_retain_the_trailer

An earlier revision of this section quoted ``18 passed / exit code 0``
here as the current reading. That was true when it was taken and is
false now, and the drift is a commit added since — which is the same
lesson as the map above, and the reason the figure is dated rather than
merely written. Neither residue ref nor a ``refs/original/**`` name
appears in the failure any more: the undeclared ref named by
``test_only_the_declared_refs_retain_the_trailer`` is ``refs/heads/main``.
The residue disposition above is complete and is not what is red.

**Re-read on 2026-10-05, the residue disposition is still in
force, since a green run of this file must not be read as covering it.**
The deletion is a property of the ref list and is checked directly rather
than inferred from the absence of a failure::

    $ git for-each-ref --format='%(refname) %(objectname)' refs/original
    (no rows)

    $ git for-each-ref --format='%(refname)' | grep -i original
    (no rows; exit 1)

    $ git cat-file -t f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87
    commit

The two tests failing above fail for one reason between them, and it is
not this one. Both name the same commit, ``f24ecc3``, reachable from
``refs/heads/main`` and from no other ref::

    $ git for-each-ref --format='%(refname)' --contains f24ecc3
    refs/heads/main

    $ git merge-base --is-ancestor f24ecc3 backup/pre-ai-trailer-strip-20261005
    rc=1
    $ git merge-base --is-ancestor f24ecc3 backup/pre-fd-secrets-sync-20261004
    rc=1

Those two ``rc=1`` lines are what separate this offender from the two
declared refs: no audit copy of ``f24ecc3`` exists, so declaring
``refs/heads/main`` would not be declaring a backup — it would be
declaring the branch that is published, which is the hole the gate closes.
The two tests fail together for that reason and not by coincidence: the
HEAD walk and the ref sweep are independent measurements that happen to
have the same single offender, and their agreeing is the sweep working
rather than one test shadowing the other.

**The whole-lane figure cannot be quoted, and the reason is a defect
outside this subtree.** One file in the lane,
``test_e2e_docstring_labels_its_evidence.py``, is not importable: line 1 is
a bare parenthesis rather than a module docstring, so the collection of the
whole lane is interrupted and no total is printed at all::

    $ ./.venv/bin/python3 -m pytest tests/static_gates/ -q
    collected 268 items / 1 error
    !!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
    =============================== 1 error in 0.15s ===============================

    # exit code 2

A green total printed from a lane that silently skipped the broken file
would be worse than no number — it would read as coverage of a file that
was never run. The lane excluding that one file is 268 passed, exit 0, and
that number belongs to the file that owns the break rather than to a count
this file asserts about itself.

The dispositions, and the invariant the rewrite had to hold (2026-10-05)
------------------------------------------------------------------------
The map above is a map. This section is the decision taken on each ref it
names, applied, and the invariant checked while applying it. It is
appended here rather than folded into the sections it corrects so that a
later reader can see which claim was made when, and which measurement
replaced it.

**The residue is deleted.** ``refs/original/refs/heads/main`` is what a
rewrite leaves behind, not an audit copy anyone took on purpose, so it is
gone::

    $ git for-each-ref --format='%(refname) %(objectname)' refs/original
    refs/original/refs/heads/main f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87

    $ git update-ref -d refs/original/refs/heads/main
    (no output, exit 0)

    $ git for-each-ref --format='%(refname) %(objectname)' refs/original
    (no rows)

The section above predicted that deleting this ref "would destroy the
only copy of that commit in this repository" and called the remedy
unavailable. That reasoning was right about the object and wrong about
the remedy: ``git update-ref -d`` removes the *name*, not the commit. The
commit is still there and still resolves::

    $ git cat-file -t f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87
    commit

**The restoring SHA is ``f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87``, and
it has a shelf life.** Nothing references it now, so it is unreachable
and will be pruned at the next ``git gc``; after that ``git cat-file``
fails and the commit is gone for good. Anyone who needs it must either
run the command below before a ``gc``, or hold a ref of their own::

    git update-ref refs/heads/audit/pre-strip-residue f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87

No such ref was created here. One was not taken, because the decision
was that this is residue and not an audit trail — but the SHA is written
down so the choice stays reversible for as long as the object survives,
which is the difference between a decision and an erasure.

Running that command is not a silent act, and the record should say so
before someone reaches for it. ``refs/heads/audit/pre-strip-residue``
would carry the one commit carrying the trailer and is not an entry in
``_ALLOWED_TRAILER_REFS``, so restoring it turns
``test_only_the_declared_refs_retain_the_trailer`` red again — by
design, on the same rule that produced the finding. Whoever restores it
is choosing to audit the residue after all, and owes the declaration
that says so; a ref held privately to survive a ``gc`` is the opposite
case, and the shorter life makes that the cheaper option here.

**Both backup refs are kept.** ``refs/heads/backup/pre-ai-trailer-strip-20261005``
(tip ``4529d1d``, 3 offending commits) and
``refs/heads/backup/pre-fd-secrets-sync-20261004`` (tip ``c9e2194``, 15)
are pre-rewrite audit copies taken deliberately, one per rewrite, and are
declared in ``_ALLOWED_TRAILER_REFS``. They are local-only and **must
never be pushed**: this repository is public, and a push publishes every
commit reachable from the ref, so pushing either would publish the
trailer-bearing history it exists to remember. That is the whole reason
``test_no_remote_tracking_ref_retains_the_trailer`` exists — a declared
ref says what is permitted locally, and only a ``refs/remotes/**`` hit
says what was actually published.

**The strip changed messages, not content.** This is the invariant that
makes the two audit refs worth keeping: if the rewrite had also rewritten
file content, they would be the only record of the bytes that shipped, and
the "audit copy" framing would be a cover for having lost them.

The proof is a *same-position* pair — the same logical commit before and
after the rewrite — and not a diff between the backup tip and HEAD::

    $ git rev-parse 4529d1d^{tree}
    c16224dc0207a683c5b9c200f1ca642619dfdba9
    $ git rev-parse b07b55a^{tree}
    c16224dc0207a683c5b9c200f1ca642619dfdba9
    $ git diff --stat 4529d1d b07b55a
    (no output)

``4529d1d`` is the tip of ``backup/pre-ai-trailer-strip-20261005``;
``b07b55a`` is its post-strip counterpart on ``main``, found by subject.
Identical tree hashes and an empty diff are the invariant in its
strongest form: the two commits are the same tree, so the rewrite altered
the message and nothing else.

``git rev-parse 34c1480^{tree}`` prints
``609446ae5729c29aa46b00e8bfce36cf93f28df1``, as quoted in the task that
asked for it. It is recorded here for completeness but it is **not** the
proof above: ``34c1480`` is a later commit on ``main`` (14:47, after the
strip), so its tree belongs to work authored after the rewrite rather
than to the commit the rewrite rewrote. The two figures are not
interchangeable and neither refutes the other.

**A predicted command that does not show what it was predicted to show.**
The obvious way to state the invariant is to diff the backup tip against
HEAD. Run on 2026-10-05 it does not isolate the strip at all, because
HEAD has moved on since the backup was taken::

    $ git diff --stat refs/heads/backup/pre-ai-trailer-strip-20261005 HEAD
     .../tests/e2e/test_credential_chain_acceptance.py  | 2110 ++++++++++++++++++++
     .../tests/e2e/test_keychain_full_delivery_macos.py |  456 ++++-
     .../test_commit_messages_are_not_ai_attributed.py  |  573 +++++-
     .../test_e2e_docstring_labels_its_evidence.py      |   42 +
     .../test_keychain_macos_job_is_a_merge_gate.py     |  480 +++-
     docs/development/design-notes.md                   |   63 +
     docs/development/design-notes.zh.md                |   46 +
     scripts/check_e2e_module_intact.sh                 |  333 +++
     scripts/scanners/scan_e2e_module_intact.py         |  399 ++++
     9 files changed, 4453 insertions(+), 49 deletions(-)

Nine files, not one, and every one of them is work committed *after* the
strip. Read as evidence about the rewrite this diff says nothing at all;
read as evidence about the current tree it is simply a changelog of the
lane since. The same-position pair above is the measurement that
actually isolates the rewrite. Recorded because a future reader will
reach for the HEAD diff first, and "9 files changed" is exactly the kind
of output that looks like a finding.

**Which half of that block is stable, and which half moves.** This file
is one of the nine, so its own line count and the grand total change
with every edit to this docstring — including the edit that recorded
them. Earlier versions of this section quoted ``344 +++-`` with ``4884
insertions``, then ``485 +++-`` with ``5025``; neither reading was wrong
and each was superseded by the paragraph written after it. That is a
measurement which invalidates itself, and it is worth naming rather than
silently refreshing, because the failure mode is specific: a reader who
re-runs the command finds different numbers, and the tempting conclusion
is that the section is stale rather than that it was always an artefact.

The nine *filenames* are the finding and they are stable. The per-file
counts and the total are not, and the deletions are steadier than either:
``49 deletions(-)`` has survived every re-reading in this section, because
the lane's history in this window is almost entirely added tests. Anyone
who re-runs the command and sees a different total has found the second
thing, not a contradiction of the first.

**This diffstat is a reading, and a task that predicted a single file from
it was wrong.** The plausible expectation before running the command is
that a rewrite confined to commit messages touches no file at all, and so
the diff should name exactly the one file the rewrite rewrote. It does
not: it names nine. The per-file numbers above are therefore a record of
one run against a moving ``HEAD``, and the "one file" expectation is not
recoverable from this command by anyone — it has to come from the
same-position pair above.

A trailer that landed on main, what removed it, and what landed after
------------------------------------------------------------------
The red above had a second cause after the residue ref was deleted, and it
was on ``main`` itself::

    $ ./.venv/bin/python3 -m pytest tests/static_gates/test_commit_messages_are_not_ai_attributed.py -q
    2 failed, 16 passed      # exit code 1

    FAILED test_no_commit_in_this_history_carries_an_ai_trailer
    FAILED test_only_the_declared_refs_retain_the_trailer

The offender was not the residue. ``refs/heads/main`` carried one
violating commit, ``994827a``, which was HEAD-reachable and had been
committed inside this lane after the figures in the map above were
taken::

    $ git log -1 --format=%B 994827a | grep -i co-authored
    Co-Authored-By: Claude <noreply@anthropic.com>

So the "0 violations over HEAD-reachable history" quoted in the first
section had gone stale: it was true when taken and was false afterwards,
and the drift was a commit added since, not a change in the checker. That
made this file a second instance of its own lesson — a measured number
goes stale the moment the lane moves on, which is why each figure above
carries the date it was read.

**A third landing has now appeared, and the recurrence is the finding.**
An earlier revision of this section read "No third landing has appeared
since" and supported it with a clean walk — "0 offenders over 144
commits" — which was true when taken. Re-read on 2026-10-05 with the
same method, over 148 commits, it is no longer true::

    $ git rev-list HEAD | wc -l
    148

    $ git merge-base --is-ancestor 994827a main
    rc=1

    $ git merge-base --is-ancestor f24ecc3 main
    rc=0

``994827a`` is gone from ``main``, so the replay described above did land
and did rewrite what it named. The offender now is a *different* commit
one step further along::

    $ git log -1 --format=%B f24ecc3 | grep -i co-authored
    Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

Walking all 148 with ``find_violations`` reports that one commit and no
other, so the red in *The counts* above is a single offending commit on
``main`` and not a return of the residue. Both figures moved — 144 to 148
commits, 0 to 1 offender — and both moved the same way, by commits added
after the reading was taken.

This is the second recurrence of the same landing, and it is the whole
argument of this section. The strip had been applied to this history
twice now and a commit in the same lane brought the trailer back both
times. A gate that fires on the first landing and is then satisfied by one
cleanup has not been shown to hold, which is why the honest reading of a
green run here is "the offending commits were rewritten", never "the gate
stopped checking".

**Removing this one is a message rewrite and is not a test edit.** The
remedy is the replay above applied to a new commit, and nothing in this
file can substitute for it: ``refs/heads/main`` is not declarable, so
adding it to ``_ALLOWED_TRAILER_REFS`` would be exactly the hole the gate
closes, and the alternative — a new ref holding the pre-rewrite commit —
only relocates the red under a name. It is recorded here as an open
disposition rather than fixed, because the fix belongs to the work that
owns history rewriting and this file owns a rule, not the history.

**The method, and the invariant it had to hold.** The offending commits
were rewritten by replaying the range with ``git commit-tree``, taking
each commit's own tree as the tree of its replacement and removing exactly
the lines this repository's own ``find_violations`` flagged. The trailer
was not searched for with a text pattern and cut wherever it appeared, so
prose that discusses the trailer is untouched. The invariant is that the
rewrite changed messages and no file content: the tree of the old tip and
the tree of the new tip are the same object, and a diff between them is
empty. That is the check that makes the rewrite safe to do at all, and it
is the same shape as the same-position pair above — a claim about the
rewrite measured inside the rewrite, never as a diff against a moving
``HEAD``.

Two details of the replay are worth keeping, because each is a way to
lose work. ``refs/heads/main`` was advanced with an expected-old-value
argument rather than a bare ``update-ref``, so a commit landing between
reading the old tip and writing the new one is *refused* instead of
overwritten; the first attempt was refused exactly that way, and the
replay was re-run against the newer tip. And no commit was rewritten
until the whole replacement chain existed, so an interrupted replay
leaves ``main`` pointing at the old history with the new objects
unreferenced — recoverable, not destructive.

A second post-strip landing, and the range the rewrite must replay
------------------------------------------------------------------
The figures below were read on 2026-10-05 by iterating history and calling
``find_violations`` once per commit message — the path ``_history_messages()``
takes — and never by searching the log text. The distinction decides the
result rather than the style of it: a commit whose body merely *discusses* the
trailer is legitimate prose under ``test_leaves_prose_about_the_tool_alone``,
and a text search counts those as offenders. An earlier pass of this check
used one and reported five violations on a history the iterator found clean.

**The residue ref is still absent, and nothing was deleted here.** The credit
belongs to the task that removed ``refs/original/refs/heads/main``; what
follows is a confirmation that the deletion held, not a second application of
it::

    $ git for-each-ref --format='%(refname) %(objectname)' | grep -i original
    (no rows; exit 1)

    $ git for-each-ref --format='%(refname)' --contains f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87
    (no rows)

    $ git cat-file -t f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87
    commit

The second command is the one that says the deletion landed — it used to name
``refs/original/refs/heads/main``, and it names nothing now, while the object
still resolves. So ``f5cad96a7a0cb09b5b698d9a169c7cfaf4502a87`` remains the
SHA that restores the ref, for as long as the next ``git gc`` does not prune
it. The full ref list is seventeen names and not one of them is a
``refs/original/**`` path::

    $ git for-each-ref --format='%(refname)' | wc -l
    17

**The offender, quoted from the checker rather than from a grep.** Over the
153 commits ``git rev-list 7e213e4`` reaches — the tip named at the end of
this section — exactly one carries a violation::

    f24ecc3ee3efcec30a06ad40a9ce6448dbcec875 L104: co-author trailer names an AI system ('claude')
        Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

One commit, one violation, on line 104 of its own message. Its subject is
``[task-20-5-2-strict] Strict-lane forensics: skip count is ZERO, so the
switch is live``, and ``git for-each-ref --format='%(refname)' --contains
f24ecc3`` names a single ref::

    refs/heads/main

Neither residue nor backup, then — it is the published branch, and declaring
it in ``_ALLOWED_TRAILER_REFS`` would be declaring the one ref the rule
protects. That set stays closed. The sweep over the seventeen refs agrees and
says where every violation in this repository lives::

    refs/heads/backup/pre-ai-trailer-strip-20261005    3
    refs/heads/backup/pre-fd-secrets-sync-20261004   15
    refs/heads/main                                  1
    (13 refs/remotes/** plus 2 refs/tags/**)          0

The two backup counts are unchanged from the map at the top, and the total is
back to 19 only by coincidence of arithmetic — one commit left the tree along
with the ref that held it, one arrived on ``main`` — which is why the total is
never quoted here without the ref it belongs to.

**This is a second, distinct post-strip landing, not the commits the earlier
rewrite touched.** That rewrite replaced ``994827a`` with ``7e10c13``, and
neither is an ancestor of ``main`` today — ``git merge-base --is-ancestor``
returns 1 for each — so the replay landed and did rewrite what it named.
``f24ecc3`` is a different commit, added by a concurrent lane afterwards, and
it is a post-strip landing of its own rather than a return of the first. Both
red tests name it, and their agreeing is the sweep working rather than one
test shadowing the other: the HEAD walk and the ref sweep share no code path
and found the same single commit.

**The remedy is a message rewrite, and it belongs to the work that owns
history rewriting.** Nothing in this file can substitute for it, and
``refs/heads/main`` is not declarable, so this stays red until the following
range is replayed. A message rewrite moves every commit after the offender,
because each descendant records its parent by SHA::

    $ git rev-list --count f24ecc3ee3efcec30a06ad40a9ce6448dbcec875..main
    6

    $ git log --oneline --reverse f24ecc3ee3efcec30a06ad40a9ce6448dbcec875..main
    6bdd69c [task-20-5-3] Strict lane: skip count is ZERO, so the switch is live; the red is constructed
    67f0896 [task-20-5-12-3-3-2] Record the residue deletion and name the red that is not it
    0885ff6 [task-20-5-4] Measure the strict lane, and record that the suite's own banner is not a measurement
    e8f543e [task-20-5-3] Strict-position forensics: the invariant is ZERO SKIPS, not a non-zero exit
    aa4beb8 [task-20-5-12-3-3-2] Re-verify the residue deletion and re-date the figures it invalidated
    7e213e4 [task-20-5-12-3-3-2] Re-measure the landing with the checker, and record the range the rewrite owes

    $ git rev-parse main
    7e213e4b897a2e8af00095a1e31b0c504f72c3f1

Six descendants plus the offending commit is **seven** to replay, and that
count is what decides the rewrite's shape: each is rebuilt from its own tree
with the lines the checker flagged removed from the message, which is the
tree-pinned replay described above and carries the same invariant —
identical trees, so messages change and no file byte does. The tip to
re-point is ``7e213e4b897a2e8af00095a1e31b0c504f72c3f1``, and it is a reading
rather than a constant: it moves with the next commit to land on ``main``, so
whoever replays this re-reads it instead of copying it from here. Every count
above is stated against ``main`` at that tip for the same reason — a count
quoted against a moving ``HEAD`` is a claim about a commit that has not
happened yet.

**Both sets of figures above were read on 2026-10-05, hours apart, and only
two of the numbers moved.** The commit that recorded this section is itself
on ``main``, so its readings were one commit behind the moment it landed —
the drift this file keeps describing, arriving from inside. Walking the 153
commits ``git rev-list 7e213e4`` reaches and calling ``find_violations`` once
per message returns the same single violation, on the same line of the same
commit, and the sweep over the seventeen refs returns the same three counts.
So the residue ref's continued absence, the offender, its reason, and the
closed ``_ALLOWED_TRAILER_REFS`` boundary are all unmoved: this is the same
second, distinct post-strip landing the section above describes, not a third
one, and the remedy is unchanged — a message rewrite owned by the work that
owns history, with this file recording the range rather than performing it.
Only the range moved. ``f24ecc3..main`` grew from five descendants to six,
the tip from ``aa4beb8`` to ``7e213e4``, and the count to replay from six to
seven.

The rewrite, and the range it actually owed
-------------------------------------------
The section above left the range recorded and the work undone. This is the
other half: the rewrite was applied, and it was applied to a range **larger
than the one recorded there**, because the same drift the section keeps
describing arrived from inside it again. The figures in the section above
were its readings; these are the ones this rewrite acted on, and where the
two differ the section above is the stale one.

**The offender was the same commit, and the range was nine commits longer.**
Walking ``git rev-list HEAD`` and calling ``find_violations`` once per
message — the ``_history_messages()`` path, not a text search — over the 155
commits then reachable::

    f24ecc3ee3efcec30a06ad40a9ce6448dbcec875 L104: co-author trailer names an AI system ('claude')
        Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

One offender, the same commit, the same line 104, the same reason. What
moved is everything downstream of it, and the section above is a reading
from before::

    $ git rev-list HEAD | wc -l
    155

    $ git rev-list --count f24ecc3ee3efcec30a06ad40a9ce6448dbcec875..main
    8

    $ git rev-parse main
    3f42395fc48c59a17b5ba5bb3b054532467fb93e

Eight descendants, not the six recorded above, and a tip of ``3f42395``
rather than the ``7e213e4`` that section named — so nine commits to replay
against the seven it recorded. A range quoted as a task number rather than
as a reading is a claim about a commit that has not happened yet, which is
the same lesson as every other count in this file arriving one more time.

**Both tips are written down, and the earlier one is a recovery handle.**
The rewrite moved ``refs/heads/main`` from ``3f42395`` to
``dfe09828b6b45f2a19796d31bd220b991eb37188``. Every pre-rewrite SHA survives
in the reflog and as an object until the next ``git gc``; the full old
chain, in topological order, was::

    f24ecc3ee3efcec30a06ad40a9ce6448dbcec875   -> 2fdec1b
    6bdd69cd8f93903040937810331d35df256bf390   -> 3d33256
    67f0896bebbd5861ca0a2144ddbddc519105597c   -> 5fe3503
    0885ff6eee1804bc9b41c953607f754588659a6f   -> 10e9d6b
    e8f543e846d9584d9efabb0644cd99db710939aa   -> c8f5c58
    aa4beb813ed882447d7ca539d7e896a7f43f0641   -> 5dcf52a
    7e213e4b897a2e8af00095a1e31b0c504f72c3f1   -> 7dd407c
    c3172c61577b719703287006f11985419bd70968   -> 67b19a1
    08581135b0c361e230ed1de7bb852e6e6320a041   -> cdda8ea
    3f42395fc48c59a17b5ba5bb3b054532467fb93e   -> dfe09828b6b45f2a19796d31bd220b991eb37188

(the right-hand column abbreviated to seven characters here; each is a full
SHA in the working record, and the tip is given in full). ``3f42395`` is the
tip the rewrite was built from, and ``git reflog show refs/heads/main``
names it — but a reflog entry is not a ref, so nothing holds these commits
against ``gc`` and no ref was created to hold them. That is a decision, not
an oversight: a ref carrying these commits would be an undeclared
``refs/heads/**`` name carrying the trailer, which is the finding rather
than an audit copy of it, so ``test_only_the_declared_refs_retain_the_trailer``
would go red on the fix. Anyone who needs the pre-rewrite tip to outlive a
``gc`` must hold it privately and accept that the same gate then names it.

**The invariant held: identical trees, one line removed, identities intact.**
Each replacement was built with ``git commit-tree <old>^{tree} -p <new-parent>
-F <message>``, taking the old commit's own tree verbatim, so the tree hash
is not merely equal but the same object::

    f24ecc3ee3efcec30a06ad40a9ce6448dbcec875 -> 2fdec1b  b06a12abacff511de3a9a9465a3bf588bb9f2cef
    6bdd69cd8f93903040937810331d35df256bf390 -> 3d33256  c05fc4c189ed7ba8eb35ac534bc40aef12760cfc
    67f0896bebbd5861ca0a2144ddbddc519105597c -> 5fe3503  deb794a03c21cad8021c6711417ecdf9f59d35b6
    0885ff6eee1804bc9b41c953607f754588659a6f -> 10e9d6b  54bd08e65a851e7e88c1c01b8bf87961b629e529
    e8f543e846d9584d9efabb0644cd99db710939aa -> c8f5c58  0e931e782609807e88509f53e1d9d14f41ab557c
    aa4beb813ed882447d7ca539d7e896a7f43f0641 -> 5dcf52a  339dd49cfa2a48c8d5cfa65a2048cc93a8f7f09f
    7e213e4b897a2e8af00095a1e31b0c504f72c3f1   -> 7dd407c  a03dcaaff0d7f9e1d9fce093529a59a7202cd088
    c3172c61577b719703287006f11985419bd70968   -> 67b19a1  06c4c26cb818899ca8c26ff25c04b7ffc8ea4d05
    08581135b0c361e230ed1de7bb852e6e6320a041   -> cdda8ea  904283942b1ad80eea63451378e07672159ec1be
    3f42395fc48c59a17b5ba5bb3b054532467fb93e   -> dfe0982  904283942b1ad80eea63451378e07672159ec1be

Ten rows, ten equalities, and the last two share a tree because
``0858113`` and ``3f42395`` were two commits of the same work and the second
changed no file. The message diff is narrower still: nine of the ten
messages are byte-identical to their originals, and the tenth differs by one
deleted line and nothing else::

    $ git cat-file commit f24ecc3ee3efcec30a06ad40a9ce6448dbcec875 | sed '1,/^$/d' > /tmp/mo
    $ git cat-file commit 2fdec1b | sed '1,/^$/d' > /tmp/mn
    $ diff /tmp/mo /tmp/mn
    104d103
    < Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>

Author and committer name, email and date were read from each original and
passed through ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*``, and a comparison of
``%an <%ae> %aI`` and ``%cn <%ce> %cI`` between each old and new SHA is equal
in all ten pairs. **The identity comparison is not a formality and the first
attempt failed it.** An earlier build of this same chain mis-parsed the
author name and wrote ``Yongmao Luo <yongmao.luo@columbia.edu>`` as the
*name*, leaving a name field carrying its own angle brackets. Every tree
still matched and every message was still correct, so a tree-equality check
alone would have passed it — the rewrite would have changed authorship on ten
public commits and reported success. It was caught by comparing identity
rather than by inspecting the rewritten chain, which is the reason that
comparison is part of this record and not a formality to skip. The bad tip
was ``3ae0d1b`` and it is named here because a reader who finds it in the
reflog should know it was superseded rather than discarded silently.

**The commands, and the guard on the one that moves a ref.** The chain was
built before any ref was written, so an interrupted replay leaves ``main``
on the old history with the new objects unreferenced. The ref was moved
with an expected-old-value argument rather than a bare ``update-ref``::

    $ git update-ref -m "rewrite: strip AI-attribution trailer from f24ecc3 and replay 9 descendants" \
        refs/heads/main dfe09828b6b45f2a19796d31bd220b991eb37188 3f42395fc48c59a17b5ba5bb3b054532467fb93e
    (no output, exit 0)

**The expected-old value is the guard, and it is load-bearing here rather
than defensive.** ``main`` moved once while the chain was being built: the
tip this rewrite acted on, ``3f42395``, did not exist when the walk above
started, and a concurrent lane committed it in between. A bare
``update-ref`` would have written the chain built from the *previous* tip
and discarded that commit; the expected-old argument refuses the write
instead, and the chain was rebuilt against the newer tip. This is the second
time that argument has caught a live move in this repository, which is why
the record says what it did rather than describing the flag as a precaution.

**What did not move.** No ``filter-branch`` was run and nothing was written
under ``refs/original/**`` — that namespace is the residue the task above
disposed of, and recreating it would re-open the red rather than close it::

    $ git for-each-ref --format='%(refname) %(objectname)' refs/original
    (no rows)

No backup ref, audit ref, tag, or remote-tracking ref was created; the ref
list is the same seventeen names it was before the rewrite, and the only one
that changed is the one the task named::

    $ git for-each-ref --format='%(refname)' | wc -l
    17

    $ git rev-parse refs/heads/backup/pre-ai-trailer-strip-20261005 \
                     refs/heads/backup/pre-fd-secrets-sync-20261004
    4529d1d9891e8184a1e52bb5affa99befc485f14
    c9e219403be46963a6271273c0ccc7bf1c3fa75e

Both declared backup refs are byte-for-byte where they were, which is the
point of declaring them: an audit copy that moved during a rewrite would
record nothing. ``refs/remotes/**`` was not touched by this work and
``test_no_remote_tracking_ref_retains_the_trailer`` is what says so, rather
than this sentence — a clean ref list is not evidence about the remote and
the two questions are kept apart throughout this file.

**The result, and what it is not.** The offending count over HEAD-reachable
history is now zero, established by this file's own test and not by a text
search over the log::

    $ ./.venv/bin/python3 -m pytest tests/static_gates/test_commit_messages_are_not_ai_attributed.py -q
    18 passed      # exit code 0

That is a statement about *this* landing, and the section on the second
post-strip landing says why that is the strongest claim available: the strip
had already been applied to this history once and a commit in the same lane
brought the trailer back. A green run here means "the offending commits were
rewritten", never "the gate stopped checking". The two backup refs still
carry the trailer by declaration and must still never be pushed.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHECKER = _REPO_ROOT / "scripts" / "check_commit_msg.py"
_INSTALLER = _REPO_ROOT / "scripts" / "install_git_hooks.sh"
_PRECOMMIT = _REPO_ROOT / ".pre-commit-config.yaml"
_CI = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_checker():
    """Import ``scripts/check_commit_msg.py`` by path.

    ``scripts/`` is not a package and must not become one (the backend
    launches scripts with a plain interpreter), so the module is loaded
    from its file rather than imported by name.
    """
    assert _CHECKER.is_file(), (
        f"{_CHECKER} is missing — it is the single implementation of this "
        f"rule; the pre-commit hook and the CI job both call into it"
    )
    spec = importlib.util.spec_from_file_location("check_commit_msg", _CHECKER)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_commit_msg", module)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

#: Which of the two rules a finding broke. Read off the checker rather than
#: spelled out here so the two cannot drift: these names are what the
#: failure output prints to say which rule sent a ref red, and a copy that
#: drifts is a report that sorts under a heading nothing was ever checked
#: against.
_RULE_TRAILER = checker.RULE_TRAILER
_RULE_PLAN_ID = checker.RULE_PLAN_ID
#: Both rules, in the order the report groups them.
_RULES = (_RULE_TRAILER, _RULE_PLAN_ID)


class _Declared(NamedTuple):
    """One allowlist entry: why a ref is exempt, and from which rules.

    *rules* is the load-bearing half. ``find_violations`` enforces two
    independent rules with two different remedies, so "this ref is allowed
    to keep the trailer" is a statement about the trailer rule only —
    reading it as a blanket exemption is how a plan-id violation acquires a
    permanent waiver by being filed under the wrong heading. The negative
    control ``test_a_plan_id_finding_survives_the_trailer_allowlist`` pins
    that reading shut.
    """

    reason: str
    rules: frozenset[str]


#: The fix for a ``[trailer]`` finding, and only for one. Kept apart from
#: :data:`_PLAN_ID_REMEDY` so neither can be handed to the other rule's
#: findings: the two were one string until it produced a report that told
#: a reader to amend an allowlist over a private id.
_TRAILER_REMEDY = (
    "For a [trailer] finding: delete the line from the commit message. If "
    "one of these is a ref/original/** path, a history rewrite left it "
    "behind and it should be deleted — residue is not an audit trail. If "
    "it is a backup taken before a rewrite, say so by adding it to "
    "_ALLOWED_TRAILER_REFS with its reason and with the rules it is "
    "exempt from; do not delete it without checking what it is the only "
    "remaining copy of. It is not exempt from the plan-id rule by being "
    "listed here."
)

#: The fix for a ``[plan id]`` finding. It is a message rewrite and it is
#: never an allowlist entry — the allowlist is the one thing in this file
#: that turns a finding into a permanent exemption, so it is the wrong
#: instrument for a private identifier that should not have been written
#: down at all.
_PLAN_ID_REMEDY = (
    "For a [plan id] finding: do NOT add the ref to "
    "_ALLOWED_TRAILER_REFS. That table exempts a ref from the trailer "
    "rule; a name entered there for a plan-id finding does not excuse it "
    "and, read as a blanket exemption, would leave a private date-prefixed "
    "id sitting in permanent history under a waiver nobody chose "
    "deliberately. Rewrite the id out of the commit message and describe "
    "the behaviour change instead — deleting the ref does not delete the "
    "commit, and a merge commit on the remote cannot be force-pushed away."
)


#: The refs permitted to retain commits carrying the trailer this rule
#: forbids, each with the one-line reason it is permitted to.
#:
#: These are audit references taken *before* a history rewrite, and a
#: rewrite cannot rewrite a reference someone is holding on to for
#: comparison — that is the point of taking one. They are local-only; the
#: push rule is enforced by ``test_no_remote_tracking_ref_retains_the_
#: trailer``, the only check that can catch a push after the fact.
#:
#: **Both entries name both rules, and that is deliberate.** When this
#: table was first written the sweep below bucketed the two rules together,
#: so "declared here" meant "exempt from everything the checker finds",
#: and these two entries have meant that from the start. Narrowing them to
#: the trailer rule now would silently shrink a standing audit exemption
#: that was taken on purpose — the refs are kept precisely so their history
#: is never edited, and an entry that quietly stops covering what it
#: covered is the failure mode worth spending this paragraph on. A *new*
#: entry gets no such carry-over: it exempts the trailer rule alone unless
#: it says otherwise in its own ``rules``, and a plan-id finding has no
#: exemption to be had here at all (see ``_PLAN_ID_REMEDY``).
#:
#: The set is deliberately closed. A new ref carrying the trailer is a
#: finding, not a name to add here: adding one is how this list stops
#: being a bound and becomes a running inventory of whatever the history
#: happens to contain. ``refs/original/**`` is residue left behind by a
#: rewrite and is never a legitimate entry — if one appears, the rewrite
#: was not cleaned up, and the answer is to delete that ref, not to
#: declare it.
_ALLOWED_TRAILER_REFS: dict[str, _Declared] = {
    "refs/heads/backup/pre-ai-trailer-strip-20261005": _Declared(
        "audit ref taken before the AI trailer was stripped from history",
        frozenset(_RULES),
    ),
    "refs/heads/backup/pre-fd-secrets-sync-20261004": _Declared(
        "audit ref taken before the secrets sync rewrite",
        frozenset(_RULES),
    ),
}


def _violations(message: str) -> list:
    return checker.find_violations(message)


def _sweep(
    refs: list[str],
    allowed: dict[str, _Declared] | None = None,
    repo: Path | None = None,
) -> dict[str, dict[str, list[str]]]:
    """Findings no declaration exempts, as ``{ref: {rule: [finding, ...]}}``.

    The one place the ref tests read history, so the negative controls
    below exercise the same walk the tests they guard depend on rather
    than a parallel implementation that could agree with them while both
    were broken.

    A *finding* is ``"<sha10> L<lineno>"``. The offending line itself is
    deliberately not carried through: every line the plan-id rule matches
    is by definition a line carrying a private date-prefixed id, and the
    trailer rule's lines name a vendor. Neither belongs in a failure
    message, and a message that quotes them has to be scrubbed before it
    can be pasted anywhere.
    """
    table = _ALLOWED_TRAILER_REFS if allowed is None else allowed
    offenders: dict[str, dict[str, list[str]]] = {}
    for ref in refs:
        # A ref absent from the table is exempt from nothing: the set is
        # closed, and a ref it names is exempt only from the rules that
        # entry lists, never from the rule it does not.
        exempted = set(table[ref].rules) if ref in table else set()
        per_rule: dict[str, list[str]] = {}
        for sha, body in _history_messages(ref=ref, repo=repo):
            for v in _violations(body):
                if v.rule in exempted:
                    continue
                per_rule.setdefault(v.rule, []).append(f"{sha[:10]} L{v.lineno}")
        if per_rule:
            offenders[ref] = per_rule
    return offenders


def _census(offenders: dict[str, dict[str, list[str]]]) -> str:
    """One line per rule saying how many findings of it are on the report.

    A reader who only reads the first line of a 40-finding failure should
    still learn that two rules are implicated, not one — otherwise the
    natural next question is "which of these is the trailer I should be
    deleting", which is a different question per rule.
    """
    counts = {
        rule: sum(len(found[rule]) for found in offenders.values() if rule in found)
        for rule in _RULES
    }
    counts = {rule: n for rule, n in counts.items() if n}
    if not counts:
        return "no findings\n"
    return (
        ", ".join(f"{counts[rule]} {rule}" for rule in _RULES if rule in counts)
        + f" ({sum(counts.values())} findings total)\n\n"
    )


def _report(offenders: dict[str, dict[str, list[str]]]) -> str:
    """The per-ref body of a failure message, grouped by rule.

    Shape::

        refs/heads/<name>  (2 findings: 1 trailer, 1 plan id)
            [trailer] 8a66858489 L7
            [plan id] 8a66858489 L7
    """
    blocks = []
    for ref, found in sorted(offenders.items()):
        total = sum(len(hits) for hits in found.values())
        breakdown = ", ".join(
            f"{len(found[rule])} {rule}" for rule in _RULES if rule in found
        )
        blocks.append(
            f"  {ref}  ({total} findings: {breakdown})\n      "
            + "\n      ".join(
                f"[{rule}] {finding}"
                for rule in _RULES if rule in found
                for finding in found[rule][:3]
            )
        )
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# The catch — the shapes that make an authorship claim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    # The exact trailer this rule was written for. Built from fragments so
    # this test file does not itself carry the literal it rejects — the
    # same convention the other gates in this directory follow.
    "Add the phase router\n\nBody text.\n\nCo-Authored-By: " + "Claude" + " Opus 5 <noreply@anthropic.com>\n",
    "Fix the race\n\nCo-authored-by: " + "Claude" + " <noreply@anthropic.com>\n",
    "Refactor\n\nCo-Authored-By: " + "GitHub" + " Copilot <x@example.com>\n",
    "Wire the tool\n\n\U0001f916 Generated with [" + "Claude" + " Code](https://example.invalid)\n",
    "Generated with " + "Open" + "AI Codex\n",
])
def test_flags_an_ai_attribution(message: str) -> None:
    assert _violations(message), (
        f"the checker missed an AI attribution in:\n{message!r}\n"
        f"It is the single implementation of the rule and all three entry "
        f"points call into it, so a miss here is a miss everywhere."
    )


# ---------------------------------------------------------------------------
# Tokens that are ordinary English
# ---------------------------------------------------------------------------
#
# The row that started this section is the reason it exists. It reads
# ``Co-Authored-By: Autonomous Agent <agent@…>`` — no vendor, no model
# family, nothing the original thirteen tokens match — and the checker
# passed it, exit 0. That is the defect this rule exists to prevent,
# spelled a way the rule could not see: a gate that only catches leaks
# phrased like the last leak is not a gate.
#
# So the product *names* are not the rule. "Names an AI system" is the
# rule, and the words vendors actually put in those trailers are the words
# everybody recognises as ordinary English. Each row below is a spelling a
# vendor ships today.


@pytest.mark.parametrize("message", [
    # The trailer that got through: no vendor name at all.
    "Tighten the gate\n\nCo-Authored-By: " + "Autonomous" + " " + "Agent" + " <agent@example.invalid>\n",
    # Vendor display names built on those words.
    "Refactor\n\nCo-Authored-By: " + "Copilot" + " " + "Agent" + " <bot@example.invalid>\n",
    "Refactor\n\nCo-Authored-By: " + "Open" + "AI " + "Assistant" + " <bot@example.invalid>\n",
    "Refactor\n\nCo-Authored-By: " + "Chat" + "GPT " + "Bot" + " <bot@example.invalid>\n",
    # Coding agents that are neither a vendor nor a model family.
    "Refactor\n\nCo-Authored-By: " + "Wind" + "surf <agent@example.invalid>\n",
    "Refactor\n\nCo-Authored-By: " + "Code" + "ium <agent@example.invalid>\n",
    "Refactor\n\nCo-Authored-By: " + "Tab" + "nine <agent@example.invalid>\n",
    "Refactor\n\nCo-Authored-By: " + "open" + "code <agent@example.invalid>\n",
    # Model families the original list did not carry.
    "Refactor\n\nCo-Authored-By: " + "Lla" + "ma 3 <agent@example.invalid>\n",
    "Refactor\n\nCo-Authored-By: " + "Mis" + "tral <agent@example.invalid>\n",
    "Refactor\n\nCo-Authored-By: " + "Deep" + "Seek <agent@example.invalid>\n",
    "Refactor\n\nCo-Authored-By: " + "Q" + "wen <agent@example.invalid>\n",
    "Refactor\n\nCo-Authored-By: " + "Gr" + "ok <agent@example.invalid>\n",
    # The same words on the other line shape the rule covers.
    "Refactor\n\nGenerated with " + "Cod" + "ex " + "Agent" + "\n",
])
def test_flags_an_ai_system_named_in_ordinary_english(message: str) -> None:
    assert _violations(message), (
        f"the checker missed an AI attribution in:\n{message!r}\n"
        f"Tokens that are also ordinary words are what a leak wears when "
        f"it is not dressed as one of the listed products."
    )


def test_the_accepted_false_positive_is_pinned_rather_than_assumed() -> None:
    """``bot`` and ``agent`` cost a false positive. Name what it is.

    A human co-author whose name contains one of those substrings trips the
    gate. That is a deliberate trade — the two ways this rule can be wrong
    are not symmetric: a false positive costs a rewritten co-author line,
    and a false negative is permanent public history that cannot be
    corrected. Writing the cost down as a test means the next person to
    widen a token sees what widening already costs, instead of
    discovering it from a colleague's blocked commit.
    """
    human = "Ship the parser\n\nCo-Authored-By: Abbot Example <abbot@example.com>\n"
    assert _violations(human), (
        "if this now passes, `bot` was narrowed — which is fine and is a "
        "change to make deliberately, with the false-positive cost written "
        "down rather than rediscovered"
    )


def test_no_token_is_generic_enough_to_match_ordinary_prose() -> None:
    """``ai`` is not a token, and this is why that has to stay true.

    Matching is substring, so a token that occurs inside a common word
    turns the gate into a coin flip on the commit message's vocabulary
    rather than on its trailer. ``ai`` is the canonical example — it is
    inside ``email``, ``maintain``, ``chain``, ``detail`` — and any token
    with the same property would be a rule that cannot be used.
    """
    ordinary = (
        "maintain the email chain\n\n"
        "Co-Authored-By: Dana Example <dana@example.com>\n\n"
        "This message mentions detail, maintainer and email on purpose.\n"
    )
    assert not _violations(ordinary), (
        "a token is matching ordinary English in a human's trailer; the "
        "rule has stopped being about attribution"
    )


# ---------------------------------------------------------------------------
# The narrowness — a rule that fires on prose is a rule that gets deleted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message", [
    # A human co-author. The trailer shape is fine; only an AI *value* is not.
    "Pair on the parser\n\nCo-Authored-By: Dana Example <dana@example.com>\n",
    # Prose about the tool. The message is allowed to say what it was
    # produced with — what it may not do is claim a co-author.
    "Rewrite the dispatcher\n\n"
    "This was produced with Claude Code running the repo's own gates.\n"
    "The model behind the session was not recorded, so the message does\n"
    "not name one.\n",
    "Document the routing\n\nProviders and models are deployment config.\n",
    # Comments are stripped by git before the message is stored, so a
    # commented-out example in a template must not trip the hook.
    "Real subject\n\n# Co-Authored-By: " + "Claude" + " Opus 5 <noreply@anthropic.com>\n",
    # A plain message.
    "fix(secrets): read credentials from the environment\n",
    "",
])
def test_leaves_legitimate_messages_alone(message: str) -> None:
    assert not _violations(message), (
        f"false positive on:\n{message!r}\nA checker that fires on prose "
        f"about the tool, or on a human co-author, is a checker that gets "
        f"disabled rather than fixed."
    )


# ---------------------------------------------------------------------------
# The three entry points
# ---------------------------------------------------------------------------


def test_the_pre_commit_config_wires_the_hook() -> None:
    text = _PRECOMMIT.read_text(encoding="utf-8")
    assert "commit-msg" in text, (
        ".pre-commit-config.yaml no longer declares a commit-msg stage, so "
        "the hook does not run through the pre-commit framework"
    )
    assert "scripts/check_commit_msg.py" in text, (
        ".pre-commit-config.yaml no longer calls the checker"
    )


def test_ci_wires_the_checker_over_the_pushed_range() -> None:
    text = _CI.read_text(encoding="utf-8")
    assert "commit-hygiene" in text, (
        ".github/workflows/ci.yml no longer has the commit-hygiene job; "
        "without it a --no-verify commit reaches main unchecked"
    )
    assert "scripts/check_commit_msg.py" in text, (
        "the CI job no longer calls the checker"
    )
    assert "fetch-depth: 0" in text, (
        "the commit-hygiene job must not use a shallow clone — the range it "
        "walks would not be present, and git would find nothing to check"
    )


def test_the_native_installer_covers_the_commit_msg_hook() -> None:
    """The pre-commit framework is optional; on a clone without it the
    config is inert and ``git commit`` runs nothing. The native installer is
    what makes the rule hold by default."""
    assert _INSTALLER.is_file(), (
        f"{_INSTALLER} is missing — a clone without the pre-commit "
        f"framework would then have no commit-msg hook at all"
    )
    text = _INSTALLER.read_text(encoding="utf-8")
    assert "commit-msg" in text, "the installer no longer installs a commit-msg hook"
    assert "check_commit_msg.py" in text, "the installer no longer points at the checker"


# ---------------------------------------------------------------------------
# This repository's own history
# ---------------------------------------------------------------------------


def _history_messages(
    ref: str | None = None, repo: Path | None = None,
) -> list[tuple[str, str]]:
    """Return ``(sha, message)`` for every commit reachable from *ref*.

    *ref* defaults to HEAD, which is what the published-history test wants.
    The ref sweep below passes a ref name; the negative control passes a
    repository of its own. One ``git log`` per range rather than one call
    per commit, so a ref with a hundred commits does not fork a hundred
    processes.
    """
    cwd = _REPO_ROOT if repo is None else repo
    if repo is None and not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree — no history to check")
    cmd = ["git", "log", "--format=%H%x1f%B%x1e"]
    if ref is not None:
        cmd.append(ref)
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True).stdout
    pairs = []
    for record in out.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, body = record.partition("\x1f")
        pairs.append((sha.strip(), body))
    return pairs


def _run_git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stdout.

    Synchronous by construction, so nothing it starts outlives the call.
    """
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout


def test_no_commit_in_this_history_carries_an_ai_trailer() -> None:
    """The rule is about the published history, not only future commits.

    One ``git log`` for the whole range rather than one call per commit:
    a clone with thousands of commits would otherwise fork a process each.
    """
    history = _history_messages()
    assert history, (
        "git log returned no commits — the walk is broken, and this test "
        "would pass without checking anything"
    )

    offenders: dict[str, list[str]] = {}
    for sha, body in history:
        for v in _violations(body):
            offenders.setdefault(v.rule, []).append(
                f"{sha[:10]} L{v.lineno}: {v.reason}"
            )

    assert not offenders, (
        "commits in this repository carry something the commit-message "
        "gate rejects:\n"
        + "".join(
            f"  {_RULE_TRAILER}: " if rule == _RULE_TRAILER else f"  {rule}: "
            for rule in _RULES if rule in offenders
        )
        + "\n"
        + "\n".join(
            "    " + line
            for rule in _RULES if rule in offenders
            for line in offenders[rule]
        )
        + "\n\n"
        + _TRAILER_REMEDY
        + "\n\n"
        + _PLAN_ID_REMEDY
    )


# ---------------------------------------------------------------------------
# The ref sweep — and proof that the sweep can fail
# ---------------------------------------------------------------------------


def test_the_ref_sweep_reports_a_planted_trailer(tmp_path) -> None:
    """Negative control: the sweep below must be able to go red.

    The two tests that follow assert that only *declared* refs retain the
    trailer, and that no remote-tracking ref does. Both of those can be
    satisfied by a sweep that finds nothing at all — a helper pointed at
    the wrong repository, a ref name git cannot resolve, a format string
    that silently returns zero rows. Such a helper is green forever and
    means nothing, which is the failure mode this gate was written to
    remove from the HEAD walk.

    So plant a violation where it must be found: a throwaway repository
    under ``tmp_path``, one commit whose message carries the trailer,
    walked by the same helper the sweep uses. The literal is spliced from
    fragments, as elsewhere in this file, so the control does not put the
    thing it rejects into the repository that rejects it.
    """
    repo = tmp_path / "planted-trailer"
    repo.mkdir()
    # An empty hooks directory, so a globally configured core.hooksPath
    # cannot reject the planted commit before the test gets to walk it.
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    trailer = (
        "Co-Authored-By: " + "Claude" + " Opus 5 <noreply@anthropic.com>"
    )
    try:
        _run_git(repo, "init", "-q")
        (repo / "planted.txt").write_text("x\n", encoding="utf-8")
        _run_git(repo, "add", "planted.txt")
        _run_git(
            repo, "-c", "user.name=Plant", "-c", "user.email=plant@example.invalid",
            "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={hooks}",
            "commit", "-q", "-m", "plant a violation\n\n" + trailer + "\n",
        )

        history = _history_messages(repo=repo)
        offenders = [
            v for _, body in history for v in _violations(body)
        ]

        assert offenders, (
            "the sweep walked a repository containing a planted "
            "attribution trailer and reported nothing. The ref tests that "
            "follow cannot fail, so their green means only that the scan is "
            "broken."
        )
    finally:
        # Nothing started here may outlive the test: tmp_path is pytest's
        # to clean, but the repository is this test's own.
        shutil.rmtree(repo, ignore_errors=True)
    assert not repo.exists(), "the planted repository outlived the test"


#: A plan id that matches :data:`PLAN_ID_SHAPE`, assembled from fragments so
#: this file does not carry the literal it is planting. Same convention as
#: ``_SYNTHETIC_PLAN_ID`` in ``test_no_plan_ids_in_metadata``.
_PLANTED_PLAN_ID = "2026" + "1012-example" + "-topic"

#: A ref name whose only finding is a plan id, and one whose only finding is
#: a trailer. Two separate branches rather than two commits on one branch, so
#: neither ref's history can pick up the other rule's finding and the
#: "only" in the test below is a fact about the history, not an assumption.
_TRAILER_ONLY_REF = "refs/heads/backup/planted-trailer-only"
_PLAN_ID_ONLY_REF = "refs/heads/backup/planted-plan-id-only"


def test_a_plan_id_finding_survives_the_trailer_allowlist(tmp_path) -> None:
    """Negative control for the boundary between the two rules.

    The defect this pins shut: ``find_violations`` enforces two rules, the
    sweep reported their findings in one list, and the failure message
    named only the trailer rule and handed out only the trailer rule's
    fix. So a ref that was red for a plan id was described as "retaining
    an AI attribution trailer", and the remedy printed underneath it was
    to add the ref to ``_ALLOWED_TRAILER_REFS``. Following that advice
    grants a permanent exemption for a finding it never described — and
    since one entry then covers every future finding on that ref, the
    private id stays in history under a waiver.

    The judgement it encodes: **a ref that is red only for a plan id must
    still be red after being added to the trailer allowlist.** Both refs
    below are declared, in the exact shape a new entry takes, exempting
    the trailer rule alone. The trailer-only ref must go quiet; the
    plan-id-only ref must not.
    """
    repo = tmp_path / "planted-two-rules"
    repo.mkdir()
    trailer = (
        "Co-Authored-By: " + "Claude" + " Opus 5 <noreply@anthropic.com>"
    )
    plan_id_line = f"see {_PLANTED_PLAN_ID} for the round-4 rewrite\n"
    try:
        _run_git(repo, "init", "-q")
        (repo / "trailer.txt").write_text("x\n", encoding="utf-8")
        (repo / "planid.txt").write_text("x\n", encoding="utf-8")

        def commit(ref: str, filename: str, message: str) -> None:
            """Point *ref* at a fresh commit with **no parent**.

            ``git commit`` would chain the second commit onto the first,
            so the plan-id ref would also reach the trailer-bearing commit
            and stop being a plan-id-*only* ref. Each ref is built as its
            own root, which is the whole point of having two of them.
            ``commit-tree`` rather than ``commit`` also means no hook can
            reject the fixture before the sweep gets to walk it.
            """
            _run_git(repo, "add", filename)
            tree = _run_git(repo, "write-tree").strip()
            _run_git(repo, "read-tree", "--empty")
            sha = _run_git(
                repo,
                "-c", "user.name=Plant", "-c", "user.email=plant@example.invalid",
                "commit-tree", tree, "-m", message,
            ).strip()
            _run_git(repo, "update-ref", ref, sha)

        commit(_TRAILER_ONLY_REF, "trailer.txt", f"a commit\n\n{trailer}\n")
        commit(_PLAN_ID_ONLY_REF, "planid.txt", f"a commit\n\n{plan_id_line}")

        # The fixtures are only meaningful if each really breaks exactly one
        # rule. Asserted against the checker directly, so a change to the
        # planted text that quietly broke both would fail here rather than
        # make the boundary below look correct for the wrong reason.
        trailer_rules = {v.rule for v in _violations(f"a commit\n\n{trailer}\n")}
        plan_id_rules = {v.rule for v in _violations(f"a commit\n\n{plan_id_line}")}
        assert trailer_rules == {_RULE_TRAILER}, (
            f"the trailer fixture no longer breaks exactly the trailer rule: "
            f"{sorted(trailer_rules)} — the boundary check below would be "
            f"measuring the wrong thing"
        )
        assert plan_id_rules == {_RULE_PLAN_ID}, (
            f"the plan-id fixture no longer breaks exactly the plan-id rule: "
            f"{sorted(plan_id_rules)} — the boundary check below would be "
            f"measuring the wrong thing"
        )

        # What "adding it to _ALLOWED_TRAILER_REFS" actually produces for a
        # new entry: a reason, and the trailer rule as the exemption.
        trailer_only_allowlist = {
            ref: _Declared("planted by the boundary control", frozenset({_RULE_TRAILER}))
            for ref in (_TRAILER_ONLY_REF, _PLAN_ID_ONLY_REF)
        }
        offenders = _sweep(
            [_TRAILER_ONLY_REF, _PLAN_ID_ONLY_REF],
            allowed=trailer_only_allowlist,
            repo=repo,
        )

        assert _TRAILER_ONLY_REF not in offenders, (
            "a ref declared exempt from the trailer rule was still reported "
            "for it — the declaration does nothing, so the exemption is "
            "either a no-op or a name that suppresses every rule at once: "
            f"{offenders}"
        )
        assert _PLAN_ID_ONLY_REF in offenders, (
            "the plan-id-only ref was declared in _ALLOWED_TRAILER_REFS and "
            "the sweep stopped reporting it. That is the hole this control "
            "exists to close: the trailer allowlist now grants a standing "
            "exemption to any ref named in it, whatever rule the ref is "
            "actually red for. Declare it exempt from the plan-id rule in "
            "its own `rules`, or narrow the entry to the trailer rule, and "
            "the finding comes back."
        )
        assert set(offenders[_PLAN_ID_ONLY_REF]) == {_RULE_PLAN_ID}, (
            "the plan-id-only ref is reported under rules "
            f"{sorted(offenders[_PLAN_ID_ONLY_REF])}, but its planted commit "
            f"breaks exactly one — {_RULE_PLAN_ID}. The bucket a finding is "
            "filed under has to be the rule it broke, or the remedy printed "
            "next to it belongs to a different rule."
        )

        # The other half: the exemption is real and rule-scoped, so naming
        # the plan-id rule too does quiet it. Without this, "still red"
        # above could be satisfied by a sweep that reports everything and
        # ignores every declaration.
        both_rules_allowlist = {
            ref: _Declared("planted by the boundary control", frozenset(_RULES))
            for ref in (_TRAILER_ONLY_REF, _PLAN_ID_ONLY_REF)
        }
        wide = _sweep(
            [_TRAILER_ONLY_REF, _PLAN_ID_ONLY_REF],
            allowed=both_rules_allowlist,
            repo=repo,
        )
        assert wide == {}, (
            "declaring the refs exempt from both rules did not quiet them, so "
            "the control above is not measuring the declaration at all — it "
            f"would go green on a sweep that reports everything: {wide}"
        )
    finally:
        # Nothing started here may outlive the test: tmp_path is pytest's
        # to clean, but the repository is this test's own.
        shutil.rmtree(repo, ignore_errors=True)
    assert not repo.exists(), "the planted repository outlived the test"


def test_the_ref_report_names_the_rule_beside_every_finding() -> None:
    """The grouping in ``_report``/``_census`` is the deliverable, so pin it.

    A report that lists both rules' findings in one flat list is the
    original defect: the reader cannot tell which remedy belongs to which
    finding, and the one remedy printed underneath applies to the wrong
    one. So this asserts the *structure* — the rule is a prefix on the
    line the finding is printed on, and the per-ref header breaks the
    total down by rule.

    Deliberately not ``"[plan-id]" in report``: a legend, a HOWTO or a
    trailing sentence would satisfy that while every finding stayed
    unlabelled, which is the shape of assertion that let this file's own
    guidance go stale in the first place. Here a line must *equal* the
    tagged finding, so a flat list fails even if the tag is spelled
    somewhere in the blob.
    """
    assert _RULE_TRAILER != _RULE_PLAN_ID, (
        f"the two rules report under the same name ({_RULE_TRAILER!r}); "
        f"anything that buckets by rule name now merges them again"
    )
    offenders = {
        "refs/heads/example": {
            _RULE_TRAILER: ["aaaaaaaaaa L3"],
            _RULE_PLAN_ID: ["bbbbbbbbbb L7"],
        }
    }

    lines = [line.strip() for line in _report(offenders).splitlines()]
    assert lines == [
        "refs/heads/example  (2 findings: 1 trailer, 1 plan-id)",
        f"[{_RULE_TRAILER}] aaaaaaaaaa L3",
        f"[{_RULE_PLAN_ID}] bbbbbbbbbb L7",
    ], (
        "the report no longer puts one rule per finding line with the rule "
        "as the line's prefix, and no longer breaks the per-ref total down "
        "by rule. That grouping is the whole reason this file can tell a "
        f"reader which remedy applies:\n{_report(offenders)}"
    )

    assert _census(offenders).splitlines()[0] == (
        "1 trailer, 1 plan-id (2 findings total)"
    ), (
        "the census no longer names both rules with their counts, so a "
        f"reader who stops after the first line learns that one rule is "
        f"implicated: {_census(offenders)!r}"
    )


def _all_refs() -> list[str]:
    """Every ref name git knows about, or fail: a sweep of nothing is not
    evidence of nothing."""
    if not (_REPO_ROOT / ".git").exists():
        pytest.skip("not a git work tree — no refs to sweep")
    out = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname)"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def test_only_the_declared_refs_retain_the_trailer() -> None:
    """Every ref, not just the one HEAD reaches.

    ``test_no_commit_in_this_history_carries_an_ai_trailer`` is green and
    can be misread as *this repository has no trailer-bearing history*.
    It is a statement about one ref. The realistic way a third one appears
    is a ``backup/pre-*`` branch taken before some later rewrite — the
    same mistake as 2026-10-05, one task after it.

    The report says which rule sent each ref red, because the two rules
    have opposite remedies and the old single-list message handed both of
    them the trailer's fix. Telling a reader to "add it to
    _ALLOWED_TRAILER_REFS" about a plan-id finding is how a private id
    acquires a standing exemption: the entry is then consulted for that
    ref on every future run and the id stays.
    """
    refs = _all_refs()
    assert refs, (
        "git for-each-ref returned no refs — the scan is broken, and this "
        "test would pass without checking anything"
    )

    offenders = _sweep(refs)
    assert not offenders, (
        "undeclared refs retain commits the commit-message gate rejects:\n"
        + _census(offenders)
        + _report(offenders)
        + "\n\n"
        + _TRAILER_REMEDY
        + "\n\n"
        + _PLAN_ID_REMEDY
    )


def test_no_remote_tracking_ref_retains_the_trailer() -> None:
    """The push rule, in the one form that can be checked after the fact.

    Declaring a ref allowed to retain the trailer says nothing about
    whether it was published. A remote-tracking ref is the receipt: if
    ``refs/remotes/**`` carries a violation, the trailer is already on
    the remote, and stripping it locally has not undone that.
    """
    refs = _all_refs()
    assert refs, (
        "git for-each-ref returned no refs — the scan is broken, and this "
        "test would pass without checking anything"
    )

    remote_refs = [r for r in refs if r.startswith("refs/remotes/")]
    if not remote_refs:
        # Vacuous unless a remote is configured and simply not fetched —
        # that is a broken scan, not an absence of pushes.
        configured = subprocess.run(
            ["git", "remote"], cwd=_REPO_ROOT, capture_output=True, text=True,
        ).stdout.split()
        assert not configured, (
            "remotes are configured but git enumerated no refs/remotes/** "
            "ref — the scan is broken, so this test cannot detect a push"
        )

    offenders = _sweep(remote_refs)
    assert not offenders, (
        "remote-tracking refs carry commits the commit-message gate "
        "rejects, so that history has been published:\n"
        + _census(offenders)
        + _report(offenders)
        + "\n\n"
        + _TRAILER_REMEDY
        + "\n\n"
        + _PLAN_ID_REMEDY
        + "\n\nNone of this can be undone by rewriting local history; the "
        "remote has to be dealt with, and nothing already fetched can be "
        "recalled."
    )