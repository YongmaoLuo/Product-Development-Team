"""The whole delivery, on a machine that has a real keychain: read it, hand it over, and prove no process can read it back.

Why this file exists
--------------------
Two other files already cover this migration, and each of them is silent about
the same thing.

``tests/integration/test_credentials_security_lookup.py`` runs the keychain
read against a stand-in for ``/usr/bin/security`` — a real executable, but one
this repository wrote. It can therefore pin the *command line* (the account, the
explicit keychain file, no service) and every failure classification, and it
cannot tell whether that command line works. A keychain is a macOS facility; on
every Linux runner in CI the real tool is absent, so the acceptance item "the
real read works" has no evidence anywhere in the suite.

``tests/e2e/test_secret_absent_from_child_environ.py`` closes the other half on
Linux: a real child process, sampled through ``/proc/<pid>/environ``, is shown
not to be carrying the secret. It runs precisely *because* Linux has no
keychain, so the value it publishes comes from the plaintext fallback — the
property is proven, and the provider it is proven for is the one Linux
deployments use.

Between them, one fact is untested: **on the platform where the secret is meant
to live in a keychain, does the real tool actually return it, and does the
handoff then keep it out of the child?** The threat model that put the secret in
the keychain says an environment variable is readable by every process of every
user on the machine. That is a claim about the keychain path, and it is a claim
about a running process rather than about a dict — so it is asserted here, end
to end, with nothing replaced:

* the keychain is a real keychain file, created, written to and destroyed by
  the real ``/usr/bin/security``;
* the lookup is the real ``credentials`` read, the real command line, the real
  keychain tool — ``credentials._SECURITY_BIN`` is *read* by this file, never
  pointed at something else, and there is no stand-in for it anywhere here;
* the handoff is a real pipe to a real child interpreter, read back through the
  real ``credentials.read_secret_fd``;
* the answer to "can another process read it" is asked of the kernel, about a
  live process, through two independent probes.

The only thing stubbed is the network, in the child, and only because the
question is where the value travels rather than where it is sent: the value
that arrived over the descriptor is what authenticates a request, and what the
transport was shown to have received is digested and compared, so "the payload
carried it" and "the payload was used" are two readings of one run.

Why this file is macOS-only
---------------------------
Everything above needs a keychain, and a keychain is a macOS facility. Off
macOS the read answers "no value" by design, and a case that ran there would be
testing the skip reason. So the platform is a ``skipif``, and the file declares
no marker of its own: ``e2e`` already exists, ``pytest.ini`` runs
``--strict-markers``, and the lane contracts pin one ``-m`` expression per job
— a new marker is a name three other places then have to be taught. This file
rides ``e2e`` and is selected by path.

Where this file runs
--------------------
Exactly one step in the workflow runs it, and that step is the last of the
three in this job that *run* pytest. The count is spelled out because a reader
who counts the word in the job's ``run:`` blocks finds four and not three: the
venv bootstrap names pytest in ``pip install pytest pytest-timeout`` and runs
no tests, so "three pytest steps" is only true of the steps that invoke it.
The step runs on a runner that has a keychain, and is quoted as the workflow
spells it rather than paraphrased, because a paraphrase of a step is how a
reader ends up trusting a wiring that has since changed::

    e2e-keychain  (macos-latest)
      - name: Run the macOS whole-delivery E2E (real keychain, real child, real probes)
        working-directory: backend
        env:
          PDT_REQUIRE_KEYCHAIN_E2E: "1"
        run: ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py --tb=short --timeout=300

The ``env:`` line is the step's whole weight. It puts the file in its strict
position there, which is the position in which a machine that will not start
the tools is a failure rather than a skip, and it is why a green step in this
job is a run rather than an absence. Off the lane the same file answers in its
default position, where the same machine is a skip — the difference between the
two is the switch and nothing else, and the section below says what each of
them produces.

The path rather than a ``-m`` expression is how the job selects this file, for
the reason the section above gives, and the job is a merge gate — so the step
is the only thing standing between this acceptance and a workflow in which
every check is green and none of these six cases has run. Neither half is
checked from inside this file, because a file cannot see the step that runs
it: both are pinned in
``tests/static_gates/test_keychain_macos_job_is_a_merge_gate.py``, which reads
the path and the switch name out of this file rather than out of a copy kept
in the workflow. That quote is therefore not a promise a reader has to take on
trust, and it is not the evidence either: item 4 below parses the very same
``run:`` line back out of ``ci.yml`` and dry-runs it locally, which says what
pytest would collect on this machine and says nothing whatever about whether
the six ran there. The ``env:`` line is what the static gate turns on, and it
is quoted above for the reason given there.

Wiring is not execution, and the gap between the two is worth stating here
rather than leaving a reader to close it. Being in a step's command line says
the workflow *intends* to run these cases; the cases run wherever the two
gated binaries actually start. Nothing here is replaced — no stand-in for the
keychain tool, no substitute for the process listing — so a machine that will
not ``execve`` them cannot run them at all, and the capability gate below
reports that as not-run rather than dressing it up. The lane is where this
repository *requires* the six to have executed; the strict switch is what makes
that requirement a verdict there instead of an intention.

The two positions, and how to ask for each
------------------------------------------
The switch is the only difference between them, so the two are one command
apart, run from ``backend/``::

    ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py

    PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py

Running both is how a reader tells which position a machine is in without
reading the source for it, and the first is the one a developer runs by
accident. Neither answer settles the acceptance, and the reason is the section
above rather than a caveat added to it: the requirement is scoped to the lane.
What a developer gets is a fact about that machine, and that fact is worth
having either way — green there means the six ran, which is an execution and
therefore a statement about the code rather than about the machine's policy,
and a refusal is named rather than passed over, which is the most any local run
can honestly report. What a local run cannot be is the *acceptance*, because
the acceptance is a requirement that the six have run and only the lane is a
place where that requirement is made and where its absence is somebody's job.
A file cannot manufacture the lane's result either. So the two commands are how
a reader finds out which account they are holding; the macOS lane's result is
the one that settles the acceptance, and a run of either command is a real
answer about the code on the machine that produced it.

What each position produces
---------------------------
The two commands above answer different questions, which is the only reason
both of them exist.

Each position produces **a fact about the machine that ran it**, and that fact
has two shapes because the probe has two answers. When both gated binaries
start, the six cases run: that is an execution, and its result is a statement
about the code rather than about the machine's policy. When the probe finds a
refusal, the default position reports six skips that each name the path
refused and the errno the kernel returned — a complete and honest account of
*that* machine, and not one item of evidence about the read.

Which of the two happened is read off the output rather than inferred from it,
because ``25 passed`` and ``19 passed, 6 skipped`` are different lines: a
reader holding a green run knows which account they are holding without having
to know this file's source. The six sit inside a total of twenty-five rather
than beside it — the gate's own cases carry no gate and run in both positions,
so ``6 skipped`` is a count of the six and not a line this file prints.
Describing "six skips" as what this file produces, without saying so of one
machine out of the two, would be the substitution this section exists to
refuse.

The strict position produces the same two answers, and only one of them
changes. Where nothing is refused the six run exactly as they did by default —
the switch adds nothing to a machine that needs nothing from it, and it does
not turn a real run into a weaker one — and where something *is* refused the
skip becomes a failure. Nothing else differs in either direction: same
binaries, same cases, same absence of anything standing in for either binary.
So the distance between a green lane that ran the six and a red lane that
could not start them is one environment variable, which is what makes "the
lane cannot be green by skipping" a property of the wiring rather than a hope.
``test_strict_mode_fails_instead_of_skipping`` below drives that property
through a real pytest run and reads the counts out of its output, so it is
observed rather than asserted over a mark — and it observes it on *every*
machine, because it supplies the refusal itself instead of waiting for an
endpoint-security product to be installed somewhere.

Neither is the evidence, and the thing most easily mistaken for the evidence is
worth naming outright. This file appearing in a step's command line says the
workflow *intends* to run these cases; that is an intention, and an intention
is not a result. The six run wherever the two binaries start and nowhere else —
a property of the machine, not of this file, which is why the file asks the
probe rather than answering the question on its own behalf. A reader who wants
to know whether they ran must read a run, not a wiring diagram.

What a local run can be archived as
------------------------------------
Four things about this file are reproducible on a developer machine, and they
are worth keeping separate from the one thing that is not reproducible there.
Only the first two are runs of the six; the last two are a reading of a
workflow and a reading of a file, and the section below labels which is which
because collapsing them is the substitution this file exists to refuse. The
first two report whatever the machine that produced them actually did — which
is why each of them has to be read off a summary line rather than quoted from
here:

* the default position's verdict — six skips naming the refused path and the
  errno the kernel returned, or six passes, whichever the probe found on the
  machine that ran it. Which account the reader is holding is on the summary
  line and nowhere else. The gate's own cases, which carry no gate and
  therefore run in this position too, are in the same count: they are what
  makes the verdict worth reading, because a probe stuck on skips this whole
  file is from outside indistinguishable from a suite that passed.
* the strict position's verdict, the same file with the switch set. On a
  machine that starts both binaries this changes nothing, because there is no
  refusal for it to convert; on a machine that refuses one, the cases that
  start it become failures naming that tool, and the cases that never start it
  are not dragged down with it.
* the workflow's own gates, which read the ``e2e-keychain`` job out of
  ``ci.yml`` and check it is a macOS lane, merge-gating, and strict on the
  step that runs this file. A green run there is a statement about the
  workflow's *text*. It is not a statement that the lane ran, and no local
  evidence turns it into one.
* collection under the lane's own run line, which says pytest can see the
  cases on the machine that collected them and names all of them. It is a
  reading of a file on disk and of nothing else: it says nothing whatever
  about whether any of the six ran, and a file named in a step's command line
  is in exactly the same position — reachable is not executed, and the word
  that gets read as "it ran" is the one this section refuses.

The four, as one machine's run actually produced them
-----------------------------------------------------
Written down because a section that describes what is obtainable without
recording what was obtained is the substitution it exists to prevent, and
because the recorded numbers are the part a reader cannot re-derive. They are
one machine's reading, taken 2026-10-05 from the repository root's ``backend/``,
and they are evidence about *that* run rather than a contract: a different
machine's probe reads differently and its own summary line says so.

All four were executed rather than transcribed, which is the distinction the
first sentence of this section is about, so it is worth saying where the
numbers came from: on that date the four commands below were each run to
completion and each returned the count and the exit status printed beside it.
The probe on that machine found no refusal for either gated binary, so these
are the readings of the *both-binaries-start* branch. Three of the four are
still re-derivable by re-running their command, and the third is not — the
section below says what it prints now and why, which is a different answer from
the one recorded beside it. A machine whose policy refuses the two binaries
returns the other branch's numbers, which are written down further down and are
not these.

Each of the four is labelled with *what kind of evidence it is*, because they
are not one thing and describing them as one thing is the confusion this
section exists to prevent. Two of them are runs of the six; the third is a
parse of the workflow's text; the fourth is a reading of a file on disk.

The exit status is written beside every one of them, because a count without
it is a claim about a run rather than the run: it is the number that says the
command was actually executed to completion, and it is the part a reader
re-running the command compares against first.

**1. The default position — an execution.**::

    $ ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py
    25 passed
    exit 0

**2. The strict position — an execution the switch had nothing to convert.**::

    $ PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py
    25 passed
    exit 0

Both gated binaries on that machine are executable — ``/usr/bin/security`` mode
0755, ``/bin/ps`` 04755, ``os.access(X_OK)`` true for each — and both start, so
the probe returned no refusals. The first line is therefore the branch of the
first two bullets that says what a machine which starts both binaries
produces: **the six cases executed.** Not skipped, and with nothing standing in
for either binary — the first of them created, wrote to and destroyed a real
keychain through the real tool, and the three probe cases sampled a live child
through two independent readings. A summary line reading ``passed`` is an
execution, which makes it a statement about the code rather than about the
machine's policy.

``25 passed`` is a total, and a total is the one reading this file refuses to
leave standing on its own, because a reader cannot get from it to the question
the acceptance asks. So the six are written down individually, from those same
two runs with ``-v`` added and nothing else — the only difference between the
two command lines is the flag that makes pytest name each case as it reports
it. The default position and the strict position are shown side by side
because they are the same six lines on this machine, and saying so is the
finding rather than a way of avoiding it::

    $ ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py -v
    test_full_delivery_reads_the_real_keychain_entry PASSED
    test_ps_eew_output_excludes_the_secret PASSED
    test_kern_procargs2_output_excludes_the_secret PASSED
    test_positive_control_ps_eew_does_see_the_secret PASSED
    test_delivery_payload_actually_carries_the_secret PASSED
    test_temporary_keychain_is_removed_even_on_failure PASSED

    $ PDT_REQUIRE_KEYCHAIN_E2E=1 ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py -v
    (the same six, PASSED, in the same order)

The other nineteen are the gate's own cases, which carry no gate and run in
this position too. Each of the six above is a ``PASSED`` rather than a ``SKIP``,
and that is the whole of what separates the branch this machine is on from the
one the rest of this section describes: on the other branch those same six names
are reported ``SKIPPED`` in the default position and ``FAILED`` in the strict
one. Nothing here claims the lane ran them — see the section below for why that
is a separate question and where it is read instead.

Those two lines are one branch, and the other branch is not a variation on them
but a different pair. Where an operating-system or endpoint-security policy
refuses ``execve`` of ``/usr/bin/security`` or ``/bin/ps`` — the refusal the
later sections describe, and one this file will not work around by putting
anything in the binary's place — the probe finds no runnability, the very same
two commands report ``19 passed, 6 skipped`` and ``6 failed, 19 passed`` instead
of two summary lines reading ``25 passed``, and **the six cases do not execute on
that machine at all**. The nineteen are the gate's own cases, which carry no
gate and therefore run in either position, so both refusal-branch lines still
count twenty-five — the total does not move, and only the six's own column does.
Nothing local runs there, so the four readings above collapse into a skip count
and a parse of a workflow file: not one item of execution evidence among them.
That is why they are recorded as *one machine's* reading and not as what this
file produces — the two branches are the same two commands and two entirely
different verdicts, and the numbers above belong to one of them and not to the
other. Which branch a given reader is holding is not written down in this file
and cannot be: it is on their own summary line, in the words the probe used.
What is written down here is the part that does not vary — the six execute
wherever the two binaries start, this repository *requires* that of the
``e2e-keychain`` step on macos-latest and of no other step, and no local run on
either branch is that requirement being met.

The two lines being identical is a result and not a gap. The switch converts a
*refusal*, that machine refused nothing, and so there was nothing to convert —
which is what the second bullet says happens on such a machine, and the reason
a green run of both is not a defect in the switch. What that machine cannot
supply is the divergence, six failures naming a tool, and this file does not
wait for a machine that would: the demonstration below drives a refusal
through a real child pytest, so "the lane cannot report green by skipping" is
observed everywhere rather than on the rare machine where it happens unforced.

That second ``exit 0`` is the one number in this section that reads like the
thing being claimed and is not it, so it is worth saying outright what it is not.
It is not a demonstration that a lane can report green without the six — the six
are inside the 25 and they ran. It says that this machine's probe returned no
refusal, so the switch was asked to convert a refusal and had none. The
demonstration that the lane cannot go green by skipping is the ``6 skipped`` /
``6 failed`` pair further down, and that pair is red on purpose; a green run of
the strict position on a machine that starts both binaries is a *pass* of the
six, and quoting it as anything else would be this file's own complaint turned
back on its own record.

**3. The workflow's own gates — a wiring check, and nothing more.**::

    $ ./.venv/bin/python3 -m pytest tests/static_gates/ -q
    258 passed
    exit 0

The number above is worth more for having been recorded honestly while it was
*not* what it is now. On the day these four readings were taken this line read
``1 failed, 257 passed`` / ``exit 1``: the failure was
``tests/static_gates/test_commit_messages_are_not_ai_attributed.py``, which
reads this repository's own commit *history* and objects to an AI attribution
trailer on it. That red was recorded as a red rather than quietly restated as
the all-green line the same command would have printed had the trailer not been
there, because a number a reader cannot reproduce is not evidence of anything
and a quoted green that the tree does not produce is worse than a quoted red.
The trailer is gone as of 2026-10-05 — the gate that objects to one passes on
this tree now — so the green above is that day's reading of the suite, not a
claim about the suite as it stands now. Re-running the command later the same
day collected 269 cases where the green above quotes 258, and a re-run after
that did not finish at all: one file in the suite was no longer valid Python —
a diff report had been committed in place of a gate's source — so pytest
stopped on it during collection, before a single case was reached, and
reported a collection error rather than a count. That was a defect in the
tree and not a reading about the code, and it is recorded here rather than
smoothed over because the number above and the number a reader got were two
different claims and only one of them was a measurement. Both readings are in
this paragraph precisely so that the green one cannot be mistaken for a gate
that has never been anything but green. The file has since been restored to
the source it held before the defect, and the command finishes again: at the
time of writing it collected 291 cases, every one of which passed. So the
three counts in this item are three readings of a suite that was being changed
underneath itself, and none of them is a contract — the current one is
whatever the command prints now, which is the only number a reader can
reproduce.

What reads the commit *history* is separate from what reads this file, which is
why a commit trailer could turn the line above red without touching the
acceptance. The five files under ``tests/static_gates/`` that name ``ci.yml``
pass on this machine today, each run on its own, and that is the part of the
third reading this file depends on — a statement about five files rather than
about a suite whose total is in motion.

This is the whole of what a developer machine can establish about the
``e2e-keychain`` job: it is declared on a macOS runner, registered as
merge-gating, runs this file by path, and sets the strict switch on the very
step that runs it. All four are properties of the workflow *text* — read out
of the file rather than out of a lane's log — and the gate that reads them says
as much in its own docstring. A green run here means the lane is wired as a
gate. It does not mean the lane ran, and no amount of local evidence turns it
into a statement that it did.

**4. Collection under the lane's own run line — reachability, not execution.**::

    $ ./.venv/bin/python3 -m pytest tests/e2e/test_keychain_full_delivery_macos.py --tb=short --timeout=300 --collect-only
    25 tests collected
    exit 0

Everything in that line but its last token is the step's own: the interpreter,
``-m pytest``, the path and ``--tb=short --timeout=300`` are parsed out of the
``e2e-keychain`` job in ``ci.yml``, which is the same parse the static gate
performs. Only ``--collect-only`` is added, and it is added on the end because
it is the one token that turns the lane's own command into a dry run rather
than a run — printed here rather than described, because an earlier version of
this item showed the lane's path with that flag in place of the two the step
actually spells, and then said the argument list had not been retyped. It had.
The difference is harmless to what pytest collects and fatal to the claim,
which is why the line is quoted whole.

So what was dry-run here is what the lane will hand to pytest. It settles one
thing — pytest can see twenty-five cases in this file on this machine — and it
is worth exactly that. Reachable is not executed, and a file named in a step's
command line is in exactly the same position.

What none of the four settles is the lane's own run, and recording them is what
makes that gap visible rather than theoretical. This repository requires the six
to have executed in exactly one place, the ``e2e-keychain`` step above. A
machine that starts the two binaries has produced a real execution and a real
answer about the code, and the requirement is still scoped to the lane, whose
absence is somebody's job. So the substitution this section refuses is not "a
local run ran the six" — on a machine that starts them it does — and it is not
"a step names this file" either. It is "either of those is the acceptance", and
neither one is read off a summary line written here.

The lane's own log is the fifth reading, and it is the one this file has not
taken. No run of that job was read while the four above were recorded, so the
acceptance is unrecorded here — not refused, not argued about, and not implied
by any of the four, but simply not read. The four are reproducible from a
checkout and the lane's result is a property of a run that happened somewhere
else, which is why no command in this repository reaches it and why a reader
who wants to close the question out has to go and read that job's log. It is
the only place the question has an answer, and a sentence here standing in for
it — that the six ran, or that the step is green — would put back exactly the
substitution these four readings were taken apart to refuse, and would put it
back with nothing on the other side of it.

A fifth thing is reproducible, and it is the one that turns the second bullet
above from a claim into something a reader can watch — it is a demonstration
rather than another item on the list, which is why the count below is still
four. Whether *this* machine's operating system refuses either binary is the
rare case, not the expected one: an endpoint-security product has to be
installed for the probe to find a refusal, and a machine without one reports
none, which is why the two commands above agree with each other on such a
machine and why a green run of both is not a defect in the switch. A reader
who wants to see the two positions *diverge* therefore drives a refusal rather
than waiting for one — and can, because the refusal is the probe's own answer
about a file that genuinely has no execute bit, not a string written into this
file. The two summary lines that come back are the whole demonstration, and
they are the two recorded on the same machine, on the same day, as the four
above:

    strict=False  ->  6 skipped
    strict=True   ->  6 failed

The counts are the reproducible part and the durations are not, so only the
counts are written down. The refusal that produced them is the probe's own
reading, quoted here in the form it quotes: ``…/refused-by-policy cannot be
started here: Permission denied``. The default is a green run carrying no
evidence at all; the strict run is the same machine, red, naming the tool and
the errno.

Unlike the four above, these two lines are not the output of a command a reader
can type: ``test_strict_mode_fails_instead_of_skipping`` at the bottom of this
file drives the refusal through a *child* pytest and reads both counts out of
that child's summary lines, so nothing in this process prints them. That case is
where they come from and it passes on every machine rather than on the rare one,
which is the whole reason this demonstration exists here instead of waiting for
a policy. Reading a number off a run is what makes it evidence; the run it was
read off is a child process the reader cannot see, and the case that watches it
is the evidence for that pair rather than the pair's own transcript.

That pair is what "the lane cannot report green by skipping" *is*, and it is
reproducible on any machine rather than only on the rare one. It is also why
this file drives both positions at the bottom of it instead of waiting for a
policy to turn up: a test that waited for the rare machine is a test that
never runs on most of them. What the pair still does not do is say anything
about *this* machine's policy — it says what the gate does with a refusal, and
the probe is what says whether there is one.

What none of the four settles is the acceptance, and the difference is not a
matter of degree. The third is a parse of the workflow's text and the fourth is
pytest's view of a file on disk, and neither of those two is an execution under
any reading. The first two are executions or they are not, depending on the
machine that produced them, and both answers are already recorded above rather
than assumed here: on a machine that starts the two binaries the six really do
run, which is an execution and a statement about the code, and the strict
position adds nothing to that run because there is no refusal for the switch to
convert; on a machine that refuses one they are six skips or six failures naming
the tool and the errno, which is a complete account of that machine and not one
item of evidence about the read. So the substitution this section exists to
refuse is not "a local run ran the six" — it does, wherever the machine's
policy permits — it is "a local run is the acceptance". This repository
requires the six to have run in exactly one place, the ``e2e-keychain`` step
above; whether that has happened is read from that step's log, and nothing in
this file can answer it. A sentence here claiming that it had would be the same
substitution in a docstring's clothes.

macOS is necessary and not sufficient
-------------------------------------
The platform gate above is about the *facility*. A macOS machine can still
refuse to start a particular system binary, and does so as a matter of policy
rather than by accident: an endpoint-security product on a developer laptop
will ``execve``-refuse ``/usr/bin/security`` and ``/bin/ps`` while running
every other tool in the same directory without complaint. The file is
written so that machine is a *skip* rather than a failure, because a case
that fails there is reporting a defect in code it never reached — which is
what the previous version of this gate did, and it made the file red on
exactly the laptops it was written for.

That position is the one for a developer's machine, and it is the wrong one for
a lane. A laptop is not the machine whose answer the acceptance is about, so
the default leaves a refusal there at "not-run". The ``e2e-keychain`` step runs
this file on a runner that is *supposed* to have a keychain and sets
``PDT_REQUIRE_KEYCHAIN_E2E``, so the skip channel is closed in the lane: a step
in the default position that met a refusal would be green with six items
absent, and no reader of a green check can tell that apart from six items that
passed. In the strict position the same machine is red, which is what makes
"the lane cannot report green by skipping" a property of the wiring rather
than a hope. So the six cases execute wherever the two binaries start, and
this repository requires that only of the macOS lane — a machine whose policy
refuses the binaries does not run them, and the file will not close the
difference by substituting anything for them.

Those are three claims and they are separable, so they are stated here once as
three rather than left to be reassembled: the default position is the one
written for a developer's machine, because a refusal there is a fact about the
laptop rather than a defect in code the case never reached; in the lane the
switch ``PDT_REQUIRE_KEYCHAIN_E2E`` closes the skip channel, so the lane cannot
report itself green by skipping and its green is a run rather than an absence;
and the six cases' execution is required of the ``e2e-keychain`` step on
macos-latest and of no other step in this repository.

Which of those two accounts a machine is holding is settled by its
operating-system policy, because the policy is what decides whether the two
gated binaries start at all, and this file does not consult it on the reader's
behalf or work around it. So a machine whose policy refuses ``execve`` of
``/usr/bin/security`` or ``/bin/ps`` executes none of the six, and the correct
report of that is the named refusal in the default position and the same six as
failures in the strict one; a machine whose policy permits them executes the
six, and the six named lines recorded above are that branch. Neither branch
substitutes for the lane: being required to execute in one place and observed to
execute wherever the two binaries start are two claims, and this file is not
allowed to convert the second into the first.

The local half of that third claim is a reading of a machine rather than a rule,
and it has two branches and not one. A machine whose policy refuses the two
binaries does not execute the six at all: the default position reports six
skips naming the refused path and the errno the kernel returned, the strict
position reports the same six as six failures, and neither of those two lines
carries one item of evidence about the read. A machine whose policy permits them
executes them, and the twenty-five passes recorded above are that branch — a
real keychain created, written and destroyed by the real tool, a real child
sampled while it was alive. Which of the two a given reader is holding is on
their own summary line, and the sentence that is deliberately not written down
here is the one that would decide it on their behalf. A developer machine
running these two commands is therefore never asking the acceptance question
and is never answering it either: the run is a fact about that machine, and the
requirement stays scoped to the lane.

The fourth thing a reader may expect to find in that list is not in it, and its
absence is the claim rather than an omission. Whether *this* machine's
operating-system policy will ``execve`` ``/usr/bin/security`` and ``/bin/ps``
is a reading of a run on that machine, not a property of this file and not a
sentence anyone may write down on its behalf: the machine that produced the
four runs recorded above started both binaries and reports twenty-five passes,
and a machine whose policy refuses them reports six skips in the default
position and six failures in the strict one, quoting the path the kernel
refused and the errno it returned. Both readings are produced by the same two
commands, neither is a defect in this file, and which of them a given reader is
holding is on their own summary line. So the six are *required* to execute in
one place and *observed* to execute wherever the two binaries start, and this
file is not allowed to convert the second into the first.

Stated once more, because this is the sentence the rest of this file is
arranged around and it is easy to lose inside the wiring: the default position
is the one written for a developer's machine; in the lane the switch
``PDT_REQUIRE_KEYCHAIN_E2E`` closes the skip channel, so that lane cannot
report itself green by skipping; and the six cases execute only where the two
binaries start, which this repository requires of the ``e2e-keychain`` step on
macos-latest and of nothing else. A machine whose operating-system or
endpoint-security policy refuses either binary does not execute the six — not
here, and not in the lane either, where the refusal is simply red. Which of
those two a given machine is cannot be read out of this file: it is a reading
of the probe, which is what the two commands above are for.

The execution this repository *requires* is therefore the macos-latest lane's
and no other step's, and a local run is a different thing whatever it says. On
the machine that produced the four runs recorded above, both gated binaries
start, so the six did execute there — a real answer about the code, on that
machine, on that day, and not a substitute for the lane. On a machine whose own
policy refuses them they execute nowhere, and the correct report of that is the
named refusal rather than a pass; this file will not close the gap with a
stand-in, and the spec it is written against forbids one.

The other half of that, and the half a reader on a locked-down laptop is
holding, is stated here so that the four runs above cannot be read as a claim
about that machine. Where a policy refuses ``execve`` — the machine answers
``EPERM`` for ``/usr/bin/security`` and ``/bin/ps`` while both files sit there
with their execute bits intact, which is the shape the endpoint-security
products above take — the six do not execute locally at all. The default
position reports them as six skips, each naming the path the kernel refused and
the errno it returned; the strict position reports the same six as six
failures. Neither is an execution, and this file will not convert one into the
other by putting a stand-in where the binary is, because a substituted tool
would turn a machine that cannot answer into a machine that appears to and the
spec this file is written against forbids exactly that. So the four sentences
above are about a machine that starts both binaries, and they are not a
description of a machine that does not: the default position is the one written
for a developer's machine either way, ``PDT_REQUIRE_KEYCHAIN_E2E`` closes the
skip channel in the lane so that lane cannot go all-green by skipping, and the
six cases' execution is required of the ``e2e-keychain`` step on macos-latest
and of nothing else on any machine. Which of the two accounts a given reader is
holding is on their own summary line, in the words the probe used, and nowhere
in this file.

The last clause of that sentence is the one that gets over-claimed in both
directions, so it is worth taking on its own. Where a machine *does* start
both binaries, the six cases execute there — a real keychain created, written
and destroyed by the real tool, a real child sampled while it was alive — and
that is an execution, so it is a statement about the code rather than about
the machine's policy. What it is not is the lane's execution, because the
requirement is scoped to the ``e2e-keychain`` step: "the six ran somewhere" and
"the six ran where they are required to have run" are two separate claims and
only the second is the acceptance. So a local green run is neither dismissed as
merely a fact about a machine nor promoted into the acceptance, and a local
refusal is a named fact about a machine rather than evidence about the read
either way. Both readings are on the summary line, and neither is this file's
to assert on the reader's behalf.

The two positions in one sentence, because a reader should not have to hold
three paragraphs to answer "where do these actually run": the default position
is the one written for a developer's machine, the lane is the only position
entitled to turn a skip into a failure, and the six cases execute wherever the
two binaries start. That last clause is a reading of a *run*, not of this file
and not of the step quoted above: a summary line reading ``passed`` is an
execution, one reading ``skipped`` names the path the kernel refused and the
errno it returned, and nothing written here produces either. So this repository
*requires* that execution of the ``e2e-keychain`` step on macos-latest and of no
other step, and a run from anywhere else is still a real answer about the code —
which is why the two commands above are worth running at all, and which is still
not the acceptance, because only the lane is required to have the six.

The asymmetry is deliberate and it is about *who is being answered*. The
default position exists for the developer running the suite on a laptop, and
it answers their question — "can this machine run the real thing?" — with the
most useful answer available, which is a skip that names what was refused and
why. The lane is not asking that question: it is asking whether the acceptance
holds, and for that the skip channel has to be closed, so ``PDT_REQUIRE_KEYCHAIN_E2E``
converts a missing capability back into the failure it always was underneath.
Being macOS and being able to run the two binaries are separate questions, and
only the lane is entitled to insist on the second.

Stated as the four things this section is asking a reader to carry: the default
position is the one written for a developer's machine; in the lane the same file
runs under ``PDT_REQUIRE_KEYCHAIN_E2E``, which closes the skip channel so that
the lane cannot report itself green by skipping; the six cases execute wherever
the two binaries start, which on a machine that will not start them is nowhere;
and this repository requires that execution of the ``e2e-keychain`` step on
macos-latest and of no other step. The paragraph above is the same four, and it
is the one to quote: what separates them is only which of them is stated as
something a reader has to go and run.

One further distinction belongs here, because it is the one a reader is most
likely to get wrong while checking the paragraph above by hand. An
endpoint-security product can kill a *copy* of a system binary while leaving the
original in place runnable, and the two are not the same observation. The probe
asks about the path the code actually spells — ``credentials._SECURITY_BIN`` and
``/bin/ps``, each at its own location, tested in place, which is the only
reading that answers the question these six cases ask. A machine that reports a
refusal for a copy it made itself has therefore said nothing about whether this
file can run here, in either direction, and a reader who concludes "so these six
cannot execute on this machine" from such a refusal has reached a verdict the
probe never supported. That is the same substitution this section refuses, with
a different object: the argument here is about evidence, and a refusal observed
on some other binary is not evidence about these two. So the verdict is read off
the two commands above and off nothing else — ``25 passed`` is an execution,
``19 passed, 6 skipped`` names the path the kernel refused and the errno it
returned on each of the six skip lines, and neither answer is legible off an
inspection of a copy.

Where the six therefore run is decided by the machine, not by this file and not
by the workflow: they run wherever the two binaries start, which is a property
of an operating-system policy rather than anything committed here. So "did they
run" is never answered from this file — it is answered from a run, in either
position, off a summary line that says ``passed``, ``skipped`` or ``failed``.
This repository *requires* that they have run in exactly one place: the
``e2e-keychain`` job on macos-latest, where the switch closes the skip channel
and a green step is therefore a run rather than an absence. Elsewhere a green
run is a real execution and a real answer about the code, and a machine that
refuses the binaries gets a named refusal — neither of which substitutes for
the lane's result, and neither of which is changed by this file being wired
into the step above. Being wired is not having run.

The distinction that keeps this honest is the one between *cannot start* and
*started and answered wrongly*. Only the former is a skip. A machine whose
keychain tool runs and then misbehaves is a failure, and ``_security`` below
keeps failing rather than skipping, so the gate cannot be used to talk the
suite out of a real defect.

Which of the two gated binaries is refused is a second question, and the two
positions answer it differently on purpose. A *skip line* is read by whoever
opened the lane and is a statement about the machine, so it names both. A
strict *failure* is a statement about one case, and a case is not evidence about
a binary it never starts: the six cases declare their own requirement, and a
refusal of the process listing does not withdraw the evidence from the three
that only read a keychain — or the other way round. The one unavailability
that is legitimate is one tool, not all of them.

The gate is itself under test, at the bottom of this file, for the reason a
file of negative assertions cannot check itself: a gate stuck in the "cannot
run" position silences every case in it and looks exactly like a passing
suite. Those cases confirm the probe agrees with what actually happens when a
binary is started, that it is keyed on the binaries production runs rather
than on a stand-in, and that every real case still carries the gate.

The two probes
--------------
They answer the same question through two different doors, because a probe that
is broken and a probe that reports an absence look identical from the outside.

``ps eew <pid>`` prints a running process's arguments *and* its environment.
The two ``w`` options are load-bearing: without them ``ps`` truncates the block
to the window width, and a truncated environment is a probe that reports an
absence because it could not see the end of a line.

``KERN_PROCARGS2`` is the kernel's own copy of the process image — the same
bytes ``execve`` was handed, which is what ``/proc/<pid>/environ`` is on Linux
and is the reason that file exists. It is reached through ``sysctl(2)`` rather
than through the ``sysctl(8)`` *command*, and the reason is worth writing down
because it is not a preference: ``KERN_PROCARGS2`` is a per-process MIB and the
command-line tool has no way to name one. On this platform it answers
``sysctl: unknown oid 'KERN_PROCARGS2'`` — for the name and for the numeric
form alike — so a case built on that spelling would fail on every machine,
including every one that would otherwise have run it. ``sysctl(2)`` is the same
facility the command does not expose: ``MIB = {CTL_KERN, KERN_PROCARGS2, pid}``.
That the block really is a process image is checked rather than assumed — it
begins with the argument count, and a wrong MIB would return a plausible
looking buffer of something else.

Neither probe can be taken on faith, so each negative assertion is paired with
a positive control: a child that really is handed the sentinel through its own
initial environment has to be *seen* by both probes. A probe that cannot see the
leak cannot be trusted to report its absence, and that is the one failure mode a
file made entirely of negative assertions cannot detect about itself.

Reading a probe's output honestly
--------------------------------
A reading is only usable if it is a reading *of that child*. Both probes answer
"empty" in the same three situations — the process has exited, this user may
not inspect it, or its block was not captured — and all three arrive at a
negative assertion looking exactly like a clean result. So every probe in this
file carries a marker this file put in the child's environment, and a sample
without it is a failure rather than a clean result.

Two of this file's own strings are excluded from the fragment sweep, and both
exclusions are load-bearing rather than cosmetic. The child's *arguments* are
this file's source, which names the module's own API — and a canary reads
``pdt-test-feishu-secret-<hex>``, whose first six characters of "secret" occur
inside ``read_secret_fd``. A sweep that ran over the arguments would therefore
report every child as leaking, which is the one thing a check like this must
never do: a check that fires on correct code gets switched off. The two
environment entries that conduct the handoff are excluded for the same reason,
and because both spell the secret's *name*. Neither exclusion touches the
whole-value assertion, which runs against the reading as it was taken.

Failure messages carry digests, never values. A failing E2E prints into a CI log
that anybody with read access to the run can see.

Resource discipline
-------------------
Every keychain is created inside ``tmp_path`` and destroyed in a ``finally``,
so an assertion that fires mid-case leaves nothing behind for the next one —
which is what ``test_temporary_keychain_is_removed_even_on_failure`` drives
directly. Every child is handed to ``register_child_process`` and released and
joined inside its own case; every descriptor to ``register_open_fd``. The
reclaim is the suite's, not this file's: this file starts the resources and the
central teardown is what collects them, so a case that fails before its own
cleanup still hands everything back.
"""

from __future__ import annotations

import ctypes
import errno as errno_module
import functools
import hashlib
import inspect
import os
import re
import select
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Iterator, Mapping, NamedTuple, Optional

import pytest

import cli
import credentials

#: ``backend/``, resolved from this file rather than written out: the
#: repository is checked out at a different path on every machine, and this
#: path is handed to a child as its working directory and import path.
_BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The secret this file follows from the keychain to a child process.
LOGICAL_NAME = "feishu_app_secret"

#: Its spec, read through the provider's own table so a renamed row is a
#: rename this file follows rather than a stale literal it outgrows.
SPEC = credentials.SECRET_SPECS[LOGICAL_NAME]

#: The variable that carries a descriptor *number* to the child, derived
#: through the module's entry point rather than written out, for the same
#: reason. Every probe asserts this name is present, because a child that was
#: told nothing has an environment with no secret in it for reasons that have
#: nothing to do with the handoff.
FD_ENV_VAR = credentials.secret_fd_env_var(LOGICAL_NAME)

#: The keychain tool, as the provider defines it — read, never replaced.
#: Every ``security`` invocation in this file goes through this constant, so
#: the command line a reviewer checks is the command line that runs and a
#: case cannot accidentally pass against a stand-in somebody installed.
_SECURITY_BIN = credentials._SECURITY_BIN

#: The process-listing tool, named once for the same reason. A system path,
#: not a fact about any one machine.
_PS_BIN = "/bin/ps"

#: The home directory this process was started with, captured at import —
#: before any case redirects ``$HOME``. Every delete in this file is checked
#: against *this* value rather than against ``Path.home()`` read at teardown:
#: teardown is exactly when the redirect has been undone, so a check made
#: there would compare the keychain against the home it was derived from and
#: pass unconditionally.
_REAL_HOME = Path.home()

#: The two binaries the capability gate is keyed on, read from the constants
#: above rather than restated. The gate decides whether this file can run at
#: all, so a gate pointed at anything other than the binaries production runs
#: would open on exactly the machine it exists to detect.
_GATED_KEYCHAIN_TOOL = _SECURITY_BIN
_GATED_PROCESS_LISTING = _PS_BIN

#: The variable that says the six real-tool cases below are not allowed to skip.
#:
#: The capability gate further down has one position, and a lane that lands in
#: it is green while publishing no evidence at all: the cases are not passing,
#: they are absent, and nothing in a job can tell those two apart. Setting this
#: variable is how a lane asks for the other answer — a machine that cannot run
#: the tools stops being an answer and becomes the failure it always was
#: underneath.
#:
#: Named in exactly one place because two readers have to agree on the spelling:
#: the gates built on :func:`require_real_tools_requested`, and the CI job that
#: sets it. Nothing else reads it, and there is no default elsewhere to fall
#: back on — a lane that never sets it gets the file exactly as it is today.
_STRICT_SWITCH_ENV = "PDT_REQUIRE_KEYCHAIN_E2E"

#: The two spellings that mean "the real run was asked for", and the whole of
#: them. Two rather than a family, because a spelling that is not on this list
#: is a value the function below has to refuse. See its docstring for why.
_STRICT_SWITCH_ON_VALUES = frozenset({"1", "true"})

#: The attribute a strict-position gate tags the case it wrapped.
#:
#: The default position is a ``skipif`` mark and is identifiable as one. The
#: strict position is a call-time wrapper, and a wrapper leaves no
#: ``pytestmark`` behind — so without this tag a strictly gated case is
#: indistinguishable from one that was never gated, and the structural check
#: at the bottom of this file would report the gate as missing on precisely
#: the machines the gate was written for.
_CAPABILITY_GATE_ATTR = "_pdt_capability_gate"


def require_real_tools_requested() -> bool:
    """Whether ``_STRICT_SWITCH_ENV`` says the real cases have to actually run.

    Answers a question about one string and reads nothing else: no other
    environment variable takes part, and the platform is not consulted here.
    Whether this machine is macOS at all is what the ``pytestmark`` below asks,
    and a switch that could overrule that would let a runner demand a keychain
    it has never had.

    Read from ``os.environ`` on every call rather than captured at import. The
    cases and the checks on them share one process, so a value snapshotted when
    this module was imported would answer for the entire session — and the two
    positions could then never both be exercised in one run, leaving a switch
    that can only be tested by re-importing the file.

    Surrounding whitespace is removed and nothing else is: the accepted
    spellings are exactly ``1`` and ``true``. ``TRUE``, ``True`` and ``Yes`` are
    not on the list, and that is the direction rather than an oversight — a
    value nobody wrote down as "on" leaves the file in its default position,
    while a spelling that quietly meant "on" would turn somebody's guess into a
    demand that six real cases run. The cost is stated rather than hidden: a
    spelling left off this list is a lane that skips where its author believed
    it was speaking. That is why the list is exactly the two spellings that
    are written down, why ``1`` is what a job sets, and why the parser is
    pinned cell by cell in the checks at the bottom of this file.

    Non-ASCII needs no rule of its own: every accepted spelling is ASCII, so a
    value carrying anything else cannot match.
    """
    raw = os.environ.get(_STRICT_SWITCH_ENV)
    if raw is None:
        return False
    return raw.strip() in _STRICT_SWITCH_ON_VALUES

#: ``CTL_KERN`` and ``KERN_PROCARGS2`` from ``<sys/sysctl.h>``.
_CTL_KERN = 1
_KERN_PROCARGS2 = 49

#: How long a ``security`` invocation may take. Longer than the provider's own
#: read bound, because creating a keychain and writing an item are heavier than
#: reading one. Bounded anyway: a tool that blocks forever must fail the case
#: that started it rather than hold the lane.
_SECURITY_TIMEOUT_SECONDS = 60

#: How long the process listing may take. It is a local read of one process;
#: anything slower is a problem with the machine, and the case should say so
#: rather than hang until the lane's own ceiling.
_PS_TIMEOUT_SECONDS = 30

#: How long a child may take to announce that it holds the payload, and how
#: long it may take to exit once its stdin is closed. Generous, because these
#: cover a loaded runner rather than a child that refuses to finish.
_HANDSHAKE_TIMEOUT_SECONDS = 30
_CHILD_TIMEOUT_SECONDS = 60

#: The string the child writes on stdout once it has read the payload and used
#: it. A sentinel, so a truncated or interleaved line cannot be mistaken for
#: one.
_READY = "READY"

#: The child's exit code for "I never got the payload" — distinct from any
#: error the interpreter itself would use, so the parent's failure message can
#: tell the two apart.
_CHILD_BAD = 3

#: The service this file files its keychain items under.
#:
#: Required by ``add-generic-password`` and used by nothing else: the provider
#: reads by account alone, so the lookup this file's cases make is unaffected
#: by which name is written here. A constant rather than a per-case string
#: because nothing in the file asks it a question.
_ITEM_SERVICE = "pdt-e2e-item"

#: The authorization scheme the child puts the secret in. A token of this
#: file's own making, not a real provider's scheme: what is under test is where
#: the value came from, and the real client's header is pinned by the
#: notifier's own suite. A fixed prefix rather than interpolation, so the
#: expected digest in the parent is derived from the same constant.
_AUTHORIZATION_PREFIX = "Bearer "

#: Where the child's stubbed request goes. A reserved, non-resolvable TLD and
#: an in-process transport, so no packet can leave the machine even if the
#: transport were ever replaced: the assertion is about the value, not the
#: network.
_ENDPOINT = "https://notify.invalid/hook"

#: This file's two environment entries, in the form a reading carries them.
#: Removed before a fragment sweep because both spell the secret's *name* and
#: a canary is built out of exactly those words. See the module docstring.
_HARNESS_ENTRY_RE = re.compile(r"PDT_TEST_(?:FD_VAR|SECRET_NAME)=\S*")

#: The shortest fragment of the sentinel that counts as a leak. Any shorter and
#: the search starts matching the key names themselves, which is the one thing
#: the diagnostics are supposed to contain.
_LEAK_MIN_FRAGMENT = 6


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        sys.platform != "darwin",
        reason=(
            "this file reads a real keychain through /usr/bin/security and "
            "answers a process-visibility question with macOS facilities (ps, "
            "KERN_PROCARGS2). Off macOS the read answers 'no value' by design, "
            "so a case here would be testing the skip reason"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Can this machine run the tools this file needs
# ---------------------------------------------------------------------------
#
# A keychain is a macOS facility, and so is the process listing: off macOS the
# read answers "no value" by design, which is a ``skipif``. macOS is not
# sufficient on its own, though, and the gap is a real one rather than a
# theoretical one — a machine can be running macOS and still refuse to
# ``execve`` a particular system binary, because a security policy says so.
# Endpoint-security products do exactly this, and a developer laptop carrying
# one refuses ``/usr/bin/security`` and ``/bin/ps`` while running every other
# tool in the same directory without complaint.
#
# A case that fails there is reporting a defect in code it never reached. The
# gate below makes that machine a skip instead, with the refusal quoted, so the
# line says what it could not do and why rather than naming a symptom.
#
# What the gate does *not* do is soften anything past it. A machine that can
# start the keychain tool and then answers wrongly is still a failure, and
# ``_security`` below keeps failing rather than skipping — the difference
# between "this machine cannot answer the question" and "the answer is wrong"
# is the whole reason the two are separate.


def _tool_is_runnable(path: str) -> tuple:
    """``(runnable, reason)`` for ``path``, by starting it.

    Starting the binary is the probe rather than ``os.access`` because the
    thing being asked is whether the kernel will ``execve`` it, and an
    ``access(2)`` answer is not that: a policy that refuses the execution
    leaves the file's mode intact, so a mode-based check reports a capability
    the machine does not have. The probe runs the real binary with ``-h``, which
    starts it and does nothing else, and reads the refusal off the exception.

    The three verdicts are kept apart because a skip message has to tell them
    apart too: absent (this machine never had the tool), present but refused
    (a policy is in the way), and runnable. A probe that collapsed the first
    two would send a reader looking for a missing file on a machine that has it.
    """
    try:
        completed = subprocess.run(
            [path, "-h"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_SECURITY_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return False, "{} does not exist on this machine".format(path)
    except OSError as exc:
        # ``EPERM`` from a policy, ``EACCES`` from a mode: both mean the same
        # thing to this file, and the errno is quoted because which one it was
        # is the difference between "fix the permissions" and "the machine is
        # not going to let us".
        return False, "{} cannot be started here: {}".format(
            path, exc.strerror or exc
        )
    except subprocess.TimeoutExpired:
        # It started and then failed to answer. That is not an inability to
        # run it, and treating it as one would skip a machine whose tool is
        # merely slow — which the case that needs it would then report as a
        # pass over a timeout.
        return True, "{} started but did not answer -h within {}s".format(
            path, _SECURITY_TIMEOUT_SECONDS
        )
    # A nonzero exit is a tool answering, not a tool refusing to be started:
    # `-h` is not supported by every one of these, and refusing to run would be
    # a verdict about the help text rather than about the capability.
    return True, "{} started (exit {} for -h)".format(path, completed.returncode)


def _unrunnable_tool_probes() -> list:
    """``(path, reason)`` for every gated tool this machine will not start.

    Probed once each: the probe starts a process, and calling it twice per
    binary would start four to learn two things. The pairs are kept rather than
    the reasons alone because the per-case judgement further down has to know
    *which* tool a refusal is about, and the path a probe quoted is the only
    part of its reason that can be matched against a case's declared tools.
    """
    refused = []
    for path in (_GATED_KEYCHAIN_TOOL, _GATED_PROCESS_LISTING):
        runnable, reason = _tool_is_runnable(path)
        if not runnable:
            refused.append((path, reason))
    return refused


#: The capability skip, evaluated once at import. Cached because a skip's
#: reason is rendered per test and the probe starts two processes each time;
#: the verdict cannot change within a run, since a policy that lifts mid-suite
#: is not a thing this file can act on.
_UNRUNNABLE_PROBES = _unrunnable_tool_probes()

#: The same verdict as reason text, which is what the default position's skip
#: renders and what every refusal is quoted from. Unchanged in wording: a skip
#: line is read by whoever opened the lane, and they need to know what the
#: machine could not do rather than which case happened to be reported.
_UNRUNNABLE_TOOLS = [reason for _path, reason in _UNRUNNABLE_PROBES]

#: The same verdict as paths, which is what the per-case judgement below is
#: decided on. Both views come out of the one probe run on purpose: a machine
#: that would start one binary and refuse the other is exactly the machine this
#: split exists for, and reconstructing that from the reason text would be
#: reading a capability back out of a sentence.
_UNSTARTABLE_TOOLS = frozenset(path for path, _reason in _UNRUNNABLE_PROBES)

#: The six cases that need the real tools, each with the gated binaries that
#: case is about. This mapping is the whole list of them — the structural check
#: below walks it, the strict gate reads each case's entry out of it, and a case
#: added without an entry is a case no gate can reason about.
#:
#: Per case rather than one pair for all six, because the six do not share a
#: subject: three of them are statements about the process listing and three
#: about a keychain read. A single verdict over both binaries lets a policy
#: that refuses one of them withdraw the evidence for questions the other was
#: able to answer, which is the one thing one unavailable tool must not do.
#:
#: What a case is "about" is the gated tool whose refusal is what makes that
#: case's own assertion unanswerable; the scaffolding around it is not part of
#: the subject. The three process-visibility cases build their keychain through
#: the shared ``deployment`` fixture, so a machine that refuses the keychain
#: tool does not skip them — it reaches :func:`_security`, which fails with
#: the refusal quoted rather than reporting the case as absent. That is a
#: question about the fixture, and it is deliberately not answered by widening
#: these entries: widening them is what would restore the collateral.
#:
#: Read-only rather than a plain dict, because this is the source of truth for
#: whether six tests are able to run at all, and an entry edited from inside a
#: function body is a gate nobody re-reads.
#:
#: ``KERN_PROCARGS2`` is a kernel MIB reached through ``sysctl(2)`` rather than
#: a binary — see the module docstring — so a case built on it is gated on the
#: process listing for the family it belongs to rather than on nothing, and a
#: third path would be a gate keyed on something this file never runs.
_GATED_CASE_TOOLS: Mapping[str, tuple] = MappingProxyType(
    {
        "test_full_delivery_reads_the_real_keychain_entry": (_GATED_KEYCHAIN_TOOL,),
        "test_ps_eew_output_excludes_the_secret": (_GATED_PROCESS_LISTING,),
        "test_kern_procargs2_output_excludes_the_secret": (_GATED_PROCESS_LISTING,),
        "test_positive_control_ps_eew_does_see_the_secret": (_GATED_PROCESS_LISTING,),
        "test_delivery_payload_actually_carries_the_secret": (_GATED_KEYCHAIN_TOOL,),
        "test_temporary_keychain_is_removed_even_on_failure": (_GATED_KEYCHAIN_TOOL,),
    }
)


def _capability_reason(unrunnable=None) -> str:
    """Why this machine cannot run the cases, or ``""`` when it can.

    Whole-machine wording on purpose, and unchanged: a skip line here is read by
    whoever opened the lane, and "this installation will not start these two
    binaries" is what tells them whether to fix a permission, remove an
    endpoint-security product, or move the lane. A per-case line would answer a
    question they did not ask, and the case the line is attached to is decided
    by which of the six pytest happened to report first.

    ``unrunnable`` defaults to the probed list so the module-level gate and the
    checks below can both call this with no argument and get the same text; it
    is a parameter so a check can ask what the reason *would* be for a refusal
    this machine does not currently have, which is the only way to test the
    skip text without waiting for a policy to be installed.
    """
    if unrunnable is None:
        unrunnable = _UNRUNNABLE_TOOLS
    if not unrunnable:
        return ""
    return (
        "this machine will not start {} so the real keychain read and the "
        "process probes cannot happen here: {}. Nothing in this file is "
        "replaced, so a machine that refuses the tool cannot answer the "
        "question either way; the run is reported as not-run rather than as a "
        "failure in the code, which was never reached.".format(
            " or ".join((_GATED_KEYCHAIN_TOOL, _GATED_PROCESS_LISTING)),
            "; ".join(unrunnable),
        )
    )


def _refused_paths(unrunnable) -> tuple:
    """Which gated tools the refusals in ``unrunnable`` are about.

    Attributed by the path each probe quoted into its reason, which is the only
    machine-specific part of that text and the only part a case's declared
    tools can be compared with. A refusal is therefore never filed against a
    tool the probe did not name.

    A reason naming neither is attributed to **both**, and the direction is
    chosen: a refusal this file cannot place is a stand-in — a check driving the
    gate with a file in its own ``tmp_path``, which is the only refusal a
    machine can be made to produce on demand — and letting an unplaceable
    refusal narrow a case's requirement would answer "this case's tools are
    fine" about a refusal the file knows about and cannot file. Narrowing is for
    real evidence; an unplaceable refusal widens.
    """
    gated = (_GATED_KEYCHAIN_TOOL, _GATED_PROCESS_LISTING)
    paths = set()
    for reason in unrunnable:
        named = {path for path in gated if path in reason}
        paths.update(named or set(gated))
    return tuple(path for path in gated if path in paths)


def _case_missing_tools(case_name: str, refuses_to_start) -> tuple:
    """The tools ``case_name`` starts that this machine will not start.

    The whole per-case rule, and deliberately the smallest thing that says it: a
    declaration (:data:`_GATED_CASE_TOOLS`) and a decision about which paths
    would not start, with nothing else consulted. It is a function of two
    values rather than a probe, so it can be driven from both ends —
    production hands it the probe's own paths, and a check hands it a refusal
    for a tool the case never starts, which is the only way to watch the rule
    work on a machine that starts everything. No assertion in this file starts
    a binary to make one refuse.

    ``refuses_to_start`` is normally a callable — the decision stated the way
    the question is asked, "would this path be unstartable here?" — and a
    collection of paths is read as a set instead, so the caller does not have to
    wrap the probe's own paths in a lambda to ask about them.
    """
    tools = _GATED_CASE_TOOLS[case_name]
    if callable(refuses_to_start):
        return tuple(path for path in tools if refuses_to_start(path))
    refused = frozenset(refuses_to_start)
    return tuple(path for path in tools if path in refused)


def _case_strict_reason(case_name: str, unrunnable, refused=None) -> str:
    """The strict failure for one case: the tools *that case* starts.

    :func:`_strict_reason` is the whole-machine wording and stays the answer for
    a case this file has no per-case declaration for — there is nothing
    narrower to say about one. For a declared case the message names only the
    binaries that case starts, because a reader sent to a policy has to be sent
    to the one the case in front of them would have used: a case that reads a
    keychain is no evidence at all about a refusal of the process listing, and
    naming it anyway points the reader at a product to uninstall that was never
    in the way.

    The evidence is the refusal that concerns the missing tool, and falls back
    to the whole list when none of them does — which is what a driven gate
    looks like, where the refusal is a stand-in naming no gated path at all.
    """
    tools = _GATED_CASE_TOOLS.get(case_name)
    if tools is None:
        return _strict_reason(unrunnable)
    if refused is None:
        refused = _refused_paths(unrunnable)
    missing = _case_missing_tools(case_name, refused)
    if not missing:
        # Nothing this case starts is refused, so there is nothing to say about
        # this case — the whole-machine wording is the honest answer rather
        # than a narrower one about a tool that was never in the way.
        return _strict_reason(unrunnable)
    evidence = [r for r in unrunnable if any(t in r for t in missing)] or list(
        unrunnable
    )
    return (
        "{} is set, so {} had to actually run rather than be reported as "
        "not-run, and this machine will not start {}: {}. A case is failed for "
        "the tools it starts and not for the ones it does not — {} starts {} "
        "and nothing else this file gates on — and nothing in this file is "
        "replaced, so a machine that refuses it cannot answer the question "
        "either way; the skip that would otherwise have covered this is the "
        "failure it was always underneath.".format(
            _STRICT_SWITCH_ENV,
            case_name,
            " or ".join(missing),
            "; ".join(evidence),
            case_name,
            " or ".join(tools),
        )
    )


def _strict_reason(unrunnable) -> str:
    """The message a strict-position failure carries for the file as a whole.

    Same evidence as the skip reason and the same consequence for the reader —
    which binary, and what the kernel said about it — with one thing changed:
    it is a failure rather than a skip, because the switch said the real run
    was required and a machine that cannot perform it has not satisfied that.

    The switch is named in the message. A lane that turns red on this has to be
    able to connect the failure to its own configuration, and a message that
    only said "the tools would not start" would leave the reader wondering
    which of the two positions they were in.

    Per case there is a narrower wording — :func:`_case_strict_reason` — and
    this stays the answer for a case with no declared tools of its own.
    """
    return (
        "{} is set, so the real keychain read and the process probes were "
        "required to run rather than be reported as not-run, and this machine "
        "cannot run them: {}. Nothing in this file is replaced — {} is the "
        "binary production reads through and {} is the one the probe runs — so "
        "a machine that refuses them cannot answer the question either way, "
        "and the skip that would otherwise have covered this is the failure it "
        "was always underneath.".format(
            _STRICT_SWITCH_ENV,
            "; ".join(unrunnable),
            _GATED_KEYCHAIN_TOOL,
            _GATED_PROCESS_LISTING,
        )
    )


def _strict_capability_gate(unrunnable, refused=None):
    """A decorator that fails at call time, naming the tools it could not start.

    A *call-time* failure rather than an import-time one, and the distinction
    is the whole implementation. Raising during import would abort collection
    of the entire file, so the six gated cases would be reported as one
    collection error and — worse — the cases that check the gate itself, which
    are precisely the ones that would have told a reader the gate was stuck,
    would be gone too. Failing when the item is called reports the failures and
    leaves every ungated case in the file collected and runnable, so a reader
    can still ask the gate's own questions of it.

    Which cases fail is decided per case, at decoration time, from
    :data:`_GATED_CASE_TOOLS`. A case whose own tools all start is handed back
    untouched — the same object, signature intact — because a case this machine
    can run must not be turned into one it cannot: the wrapper below drops the
    signature, and a case permitted to run has to keep its fixtures. The gate
    has still judged it, which is what the tag records.

    The wrapper drops the signature rather than passing the call through. The
    gated cases take fixtures — a real keychain, a real child process — and on
    a machine that cannot start the tools those fixtures would each fail
    during setup, which pytest reports as an *error* on the case. An error reads
    as a defect in the harness, and this file has no harness defect to report:
    it has a machine that would not ``execve`` a binary. Requesting nothing
    makes the refusal the only thing that happens, and it happens in the call
    phase where a failure belongs.

    The wrapper also tags what it produced, and the tag is what lets the
    structural check recognise this gate at all. The default position is a
    mark, so a case carrying it is identifiable by looking for ``skipif``; a
    wrapper has no ``pytestmark`` to look for, and without the tag a gated
    case here is indistinguishable from an ungated one — which would make
    that check go red on the machines this position exists for and nowhere
    else.
    """
    if refused is None:
        refused = _refused_paths(unrunnable)

    def decorate(func):
        case_name = getattr(func, "__name__", "")
        if case_name in _GATED_CASE_TOOLS and not _case_missing_tools(
            case_name, refused
        ):
            setattr(func, _CAPABILITY_GATE_ATTR, "strict")
            return func

        @functools.wraps(func)
        def gated(*args, **kwargs):
            pytest.fail(
                _case_strict_reason(case_name, unrunnable, refused), pytrace=False
            )

        # An empty signature is what stops pytest from setting the case's
        # fixtures up. ``functools.wraps`` would otherwise advertise them, and
        # the setup would be where this file failed instead of the call.
        gated.__signature__ = inspect.Signature()
        setattr(gated, _CAPABILITY_GATE_ATTR, "strict")
        return gated

    return decorate


def _capability_gate(unrunnable, strict: bool, refused=None):
    """The gate for one position: a skip by default, a failure when strict.

    Split out from the module-level ``requires_real_tools`` so both positions
    can be constructed and inspected on any machine. A gate built only where
    the tools happen to be runnable has one reachable position, and the other
    one is untested on every machine that is not the one the strict position
    was written for — which is nearly all of them.

    The two positions judge differently, and the asymmetry is the point rather
    than an inconsistency. The default is a statement about the *machine*: any
    refusal makes the whole file unanswerable as far as a reader of the skip
    line is concerned, so it is worded for the machine and covers all six. The
    strict position is a statement about a *case*: it exists because a lane
    that demanded the real run and got a skip published no evidence, and
    failing a case for a tool it never starts would be answering that demand
    with a refusal it had nothing to do with. So there the requirement is read
    per case, from ``refused``.
    """
    if strict and unrunnable:
        return _strict_capability_gate(unrunnable, refused)
    return pytest.mark.skipif(bool(unrunnable), reason=_capability_reason(unrunnable))


#: The gate, as a decorator. Separate from the platform ``pytestmark`` above
#: so the cases that check the gate itself are not themselves gated by it —
#: a gate that cannot be examined because it is what examines everything else
#: is a gate nobody can tell is stuck.
#:
#: Two positions rather than one, and the switch decides which. The default is
#: unchanged: a machine that will not start the tools is a skip. The other is
#: reached by :data:`_STRICT_SWITCH_ENV`, and there the same machine is a
#: failure, because a lane that asked for the real run and got a skip has
#: published no evidence while reporting green.
#:
#: The probe's own paths are handed in beside its reasons, so the strict
#: position judges each case against what the machine actually refused rather
#: than against text it has to take apart.
requires_real_tools = _capability_gate(
    _UNRUNNABLE_TOOLS, require_real_tools_requested(), _UNSTARTABLE_TOOLS
)


# ---------------------------------------------------------------------------
# Running the real keychain tool
# ---------------------------------------------------------------------------


def _security(
    *args: str, check: bool = True, timeout: int = _SECURITY_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess:
    """Run ``/usr/bin/security`` and return what it did.

    Failures raise rather than return, because in this file a nonzero exit
    from the keychain tool has exactly one meaning: the case cannot say what
    it came to say. Silence there would turn a broken read into a negative
    assertion — a probe that found nothing because it never asked.

    ``check=False`` is for the one call whose failure must not mask the
    exception already on its way out: deleting the keychain on the way out of
    a ``finally``.
    """
    argv = [_SECURITY_BIN, *args]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except OSError as exc:
        pytest.fail(
            "{} could not be run: {}\n"
            "This file replaces nothing — the read under test is the one "
            "production runs. A machine where that binary cannot be executed "
            "cannot answer the question, and skipping quietly would be the "
            "one way this file could report success without evidence.".format(
                _SECURITY_BIN, exc
            )
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "{} {} did not finish within {}s. A keychain that blocks here is "
            "usually waiting on an unlock prompt this machine has no way to "
            "show.".format(_SECURITY_BIN, args[0] if args else "", timeout)
        )

    if check and completed.returncode != 0:
        pytest.fail(
            "{} {} exited {}\nstderr: {}\nstdout: {}".format(
                _SECURITY_BIN,
                args[0] if args else "",
                completed.returncode,
                completed.stderr.decode("utf-8", "replace").strip(),
                completed.stdout.decode("utf-8", "replace").strip(),
            )
        )
    return completed


def _assert_disposable(home: Path, keychain: Path) -> None:
    """Fail rather than destroy a keychain that is not this file's own.

    Every keychain created here is created to be destroyed, and the only
    reason that is true is that ``home`` is a directory under ``tmp_path``.
    The check is made here, against the home captured at import, because the
    cost of being wrong is now unrecoverable: ``credentials._KEYCHAIN_PATH``
    names this project's dedicated keychain, the container that holds the
    real credentials, and no code in this project can put an item back into
    it once the file is gone.

    Two ways to be wrong, both refused. A home that *is* the real one, and a
    home that is disposable while the keychain resolves somewhere under the
    real home — the second being what a half-applied redirect looks like. A
    path outside both is allowed: this file does not presume to know every
    way a deployment lays out its temporary directory, and the check exists
    to catch the specific catastrophe rather than to police a policy.
    """
    if home == _REAL_HOME:
        pytest.fail(
            "refusing to delete {}: its home directory is this machine's own, "
            "so the keychain is not one this file created. {}".format(
                keychain,
                "The login keychain used to be rebuilt on demand; this "
                "project's dedicated keychain is not, and the credentials it "
                "holds cannot be restored from here.",
            ),
        )
    if _REAL_HOME in keychain.parents:
        pytest.fail(
            "refusing to delete {}: it resolves inside this machine's home "
            "directory ({}), so it is not a keychain this file created. A "
            "disposable home of {} does not make a real keychain disposable.".format(
                keychain, _REAL_HOME, home
            ),
        )


@contextmanager
def temporary_keychain(home: Path) -> Iterator[Path]:
    """Yield a real, empty keychain at the path a redirected ``$HOME`` implies.

    Named by :func:`credentials._keychain_file` rather than invented here: the
    point of redirecting the home directory is that the provider resolves the
    same path this file created, with its own resolution and its own constant,
    so the read cannot quietly point somewhere else.

    Created with no password, which is a real keychain in its unlocked state —
    a headless runner has no way to answer an unlock prompt, and a prompt here
    would be a hang rather than a failure.

    Destroyed in a ``finally``, and the delete is ``check=False``: cleanup runs
    while an exception is propagating, and a cleanup that raises would replace
    the assertion that fired with a message about the cleanup — the one
    failure this file must never misreport.
    """
    keychain = home / credentials._KEYCHAIN_PATH
    keychain.parent.mkdir(parents=True, exist_ok=True)
    _security("create-keychain", str(keychain))
    if not keychain.exists():
        pytest.fail(
            "{} reported success but {} does not exist, so the provider's "
            "read would resolve a path this file never created and the case "
            "would be sampling something other than what it set up.".format(
                _SECURITY_BIN, keychain
            )
        )
    try:
        yield keychain
    finally:
        _assert_disposable(home, keychain)
        _security("delete-keychain", str(keychain), check=False)


# ---------------------------------------------------------------------------
# The deployment under test
# ---------------------------------------------------------------------------


class Deployment(NamedTuple):
    """One deployment configured the way this project asks an operator to.

    ``home`` is redirected, the switch is on, every row of the spec table has
    an account exported and an item filed under it, and no plaintext fallback
    is exported at all. That last part is what makes the whole file mean
    something: with a fallback set, a secret resolved from the environment
    would be indistinguishable from one resolved from the keychain, and the
    label on the CLI's row would be the only thing telling them apart.
    """

    home: Path
    keychain: Path
    #: logical name -> the account its keychain item is filed under.
    accounts: Dict[str, str]
    #: logical name -> the synthetic value filed under that account.
    sentinels: Dict[str, str]

    def sentinel(self, name: str = LOGICAL_NAME) -> str:
        """The value filed for ``name``. A canary: synthetic, per test, never
        written to disk outside this test's own ``tmp_path``."""
        return self.sentinels[name]

    def digest(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()

    def env(self, extra: Optional[dict] = None) -> dict:
        """The environment the CLI is started with.

        This is the deployment's own environment: the redirected home, the
        switch, and one account per row of the spec table — the state an
        operator's machine is in, and what the ``secrets verify`` subprocess
        has to resolve against a real keychain.

        Built from nothing rather than copied from ``os.environ``. A copy
        would carry whatever the machine running the suite has exported —
        including, on a developer's machine, a real ``FEISHU_APP_SECRET`` —
        and the row that reports a source would then be answering a question
        about that shell rather than about this code.
        """
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(_BACKEND_DIR),
            "HOME": str(self.home),
            credentials._SWITCH_ENV_KEY: "0",
        }
        for name, account in self.accounts.items():
            env[credentials.SECRET_SPECS[name].account_env_key] = account
        if extra:
            env.update(extra)
        return env


def child_env(fd: int, extra: Optional[dict] = None) -> dict:
    """The environment a child receives the handoff through.

    The child's whole address book, and it is three entries: the descriptor
    variable's *name*, the logical name the payload is keyed by, and the
    descriptor *number*. There is no ``$HOME``, no keychain switch and no
    account index in it, and that is the point rather than an omission: the
    child resolves nothing — it reads a descriptor it was handed — so a
    home directory and an account name would be a keychain address and an
    item index added to the very environment this file asserts is clean. It
    would also mean the fragment sweep was matching this file's own
    scaffolding, since the accounts are minted from the same canary marker
    as the secret.

    ``extra`` is how the positive control puts the sentinel where a real leak
    would have put it — the child's own initial environment, under the very
    variable this project is removing — because a control that put it
    anywhere else would not be a control on the probe.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(_BACKEND_DIR),
        "PDT_TEST_FD_VAR": FD_ENV_VAR,
        "PDT_TEST_SECRET_NAME": LOGICAL_NAME,
        FD_ENV_VAR: str(fd),
    }
    if extra:
        env.update(extra)
    return env


def _account_for(name: str, canary, path) -> str:
    """Return the keychain account this file files ``name`` under.

    Minted through the suite's ``canary`` fixture, from the index kinds that
    fixture already declares: an account is an index rather than a secret, but
    it is still a per-test string, and one literal reused across the suite
    could not be told apart from another test's in a failure. ``path`` is the
    test's own ``tmp_path``, which is what makes the value per-test rather
    than per-session.
    """
    kind = "feishu_index" if name == LOGICAL_NAME else "telegram_index"
    return canary(kind, path)


def _file_item(account: str, value: str, keychain: Path) -> None:
    """Write one generic-password item into ``keychain``, for real.

    The keychain is always named explicitly, so the item lands in the file
    this file created and not in whichever keychain the process happens to
    have open. Leaving that to a search list is how a test ends up filing
    its canary alongside — and then asserting over — the credentials this
    project keeps in its own keychain.

    The value reaches the tool as an argument, so for the second of a case's
    runs it is briefly visible in this machine's process listing. That is a
    property of the fixture's own bookkeeping, it holds a synthetic canary,
    and the alternative — the tool's interactive prompt — is a hang waiting
    for a keypress on a headless runner.

    The service is required and is not what the item is looked up by.
    ``add-generic-password`` on this platform names ``-s`` as required, and
    answering a usage message with a nonzero exit is not adding an item — the
    command would fail with no keychain written and every case that files an
    item would go on to assert against an empty keychain. Which name is
    written is therefore not a decision this file gets to have: the provider
    reads by account alone (:mod:`credentials` passes no ``-s``, for the
    reason its own docstring gives), and an item filed under a service is
    found by an account-only lookup all the same. So the service here is a
    constant this file mints, and nothing downstream depends on its value.
    """
    _security(
        "add-generic-password",
        "-a",
        account,
        "-s",
        _ITEM_SERVICE,
        "-w",
        value,
        str(keychain),
    )


# ---------------------------------------------------------------------------
# The child
# ---------------------------------------------------------------------------
#
# A real interpreter — ``sys.executable -c <source>`` — with nothing on the
# path patched. It takes the descriptor number from its environment, reads the
# payload through the module's own reader, authenticates a stubbed request
# with what it read, reports the digest of both, and then parks on stdin.
#
# It is told nothing about the value it is expected to find. A child that knew
# the answer could not fail the way this file needs it to fail — the parent
# would be handing over the answer it is asking for.
#
# The parking is the reason the file is shaped the way it is. The parent has to
# sample a live process: once the child exits, there is nothing to sample, and
# a child that read and exited on its own would turn the sample into a race
# against its own lifetime. Instead the child announces readiness and holds
# still until the parent closes the pipe — at which point ``sys.stdin.read()``
# returns at EOF and it exits — and the parent holds the closing end. "The
# process exists" and "the process has exited" are therefore two explicit
# moments, and every sample is taken in the first.
#
# The harness names reach the child through its environment rather than
# through interpolated source, so the script is one constant with nothing
# spliced into it: an f-string would have to double every brace in the child's
# own literals, and the first one somebody forgot to double would be a syntax
# error reported as an exit code.

_CHILD_SRC = """
import hashlib
import os
import sys

READY = {ready!r}
BAD = {bad}
SCHEME = {scheme!r}
ENDPOINT = {endpoint!r}

fd_var = os.environ["PDT_TEST_FD_VAR"]
logical = os.environ["PDT_TEST_SECRET_NAME"]

raw = os.environ.get(fd_var)
if raw is None:
    sys.exit(BAD)
try:
    fd = int(raw)
except ValueError:
    sys.exit(BAD)

import credentials

try:
    payload = credentials.read_secret_fd(fd)
except (OSError, ValueError):
    sys.exit(BAD)
value = payload.get(logical)
if value is None:
    sys.exit(BAD)

sent = hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()

# The one double in this file, and it is on the wire rather than anywhere
# near the secret: the transport is in-process, and what it is shown is
# digested and reported back, so "the payload carried the value" and "the
# payload was used" are two readings of one run.
import httpx

seen = []

def _handler(request):
    seen.append(request.headers.get("authorization", ""))
    return httpx.Response(200, json={{"ok": True}})

with httpx.Client(transport=httpx.MockTransport(_handler)) as client:
    client.post(ENDPOINT, headers={{"Authorization": SCHEME + value}},
                json={{"text": "ping"}})

if not seen:
    sys.exit(BAD)
used = hashlib.sha256(seen[0].encode("utf-8", "surrogateescape")).hexdigest()

sys.stdout.write(READY + " " + sent + " " + used + "\\n")
sys.stdout.flush()

# Park until the parent closes the pipe: read() returns at EOF, so the
# closing of stdin is the signal to finish, and nothing is read from it.
sys.stdin.read()
sys.exit(0)
""".format(
    ready=_READY,
    bad=_CHILD_BAD,
    scheme=_AUTHORIZATION_PREFIX,
    endpoint=_ENDPOINT,
)


class Child(NamedTuple):
    """A started child, and the three ways a case interacts with it."""

    proc: subprocess.Popen

    @property
    def pid(self) -> int:
        return self.proc.pid

    def read_ready_line(self, timeout: int = _HANDSHAKE_TIMEOUT_SECONDS) -> str:
        """Return the line the child wrote once it holds and has used the payload.

        Bounded by waiting on the pipe rather than by a timer around a
        blocking read: a child that never writes would otherwise hold the case
        until the suite's own per-case ceiling, which is five minutes to learn
        that a fork never happened. The child writes its one line and flushes,
        so readability means the line is there; a child that dies instead makes
        the pipe readable at EOF, and an empty line is reported as the failure
        it is.
        """
        readable, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not readable:
            self.proc.kill()
            _, stderr = self.proc.communicate()
            pytest.fail(
                "the child wrote nothing to stdout within {}s and had to be "
                "killed, so it never reached the point where it holds the "
                "payload.\nstderr: {}".format(
                    timeout, stderr.decode("utf-8", "replace")
                )
            )
        line = self.proc.stdout.readline()
        text = line.decode("utf-8", "replace").strip()
        if not text:
            self.proc.kill()
            _, stderr = self.proc.communicate()
            pytest.fail(
                "the child's stdout reached EOF without the {} line, so it "
                "exited instead of parking with the payload in hand.\n"
                "exit: {}\nstderr: {}".format(
                    _READY,
                    self.proc.returncode,
                    stderr.decode("utf-8", "replace"),
                )
            )
        return text

    def finish(self, timeout: int = _CHILD_TIMEOUT_SECONDS) -> int:
        """Close stdin, join the child, and return its exit code.

        Closing stdin is the release: the child is parked in
        ``sys.stdin.read()``, which returns at EOF and lets it exit. Then
        ``communicate`` drains what is left and waits, so the join is an
        ordering guarantee rather than a hope — and a child that ignores the
        release is killed and reported, rather than outliving the case.

        ``proc.stdin`` is cleared afterwards, which is what ``communicate``
        does to the stream once it has dealt with it. Left in place, the next
        ``communicate`` flushes the file this method closed and raises
        ``ValueError: flush of closed file`` — the release would be reported
        as a defect in the harness rather than as the join it is.
        """
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except BrokenPipeError:
                # The child is already gone; the join below reports how.
                pass
            self.proc.stdin = None
        try:
            self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()
            pytest.fail(
                "the child was still running {}s after its stdin was closed "
                "and had to be killed; it never reached its own exit "
                "code".format(timeout)
            )
        assert self.proc.poll() is not None, (
            "the child was handed back before it exited"
        )
        return self.proc.returncode


def _spawn_child(
    register_child_process, source: str, *, env: dict, pass_fds: tuple = ()
) -> Child:
    """Start one real interpreter and hand it to the suite's teardown.

    Every child goes through ``register_child_process``, so it rides the
    teardown that already reclaims a test's workers rather than needing a
    second, file-only mechanism. That registration is the backstop for a case
    that fails before it reaches :meth:`Child.finish`; the finish is what the
    case itself relies on.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", source],
        env=env,
        cwd=str(_BACKEND_DIR),
        pass_fds=tuple(pass_fds),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    register_child_process(proc)
    return Child(proc)


class Report(NamedTuple):
    """What the child reported about the payload it read and used."""

    sent: str
    used: str


def _marker(fd: int) -> bytes:
    """The entry that proves a reading is a reading of *this* child.

    The descriptor variable, name and number together. A sample carrying it is
    provably the process image of a process this file set up, and it says
    nothing about the secret — so it is a marker a leak cannot fake its way
    past by being present in the wrong process.
    """
    return "{}={}".format(FD_ENV_VAR, fd).encode("utf-8")


# ---------------------------------------------------------------------------
# The two probes
# ---------------------------------------------------------------------------


def _as_text(sample: bytes) -> str:
    """The probe's bytes decoded, never raising on a value that is not text.

    ``errors="replace"`` rather than a strict decode: the probe is looking for
    a substring, and a byte sequence that is not valid UTF-8 still has to be
    searchable rather than raising past the assertion that was supposed to
    report it.
    """
    return sample.decode("utf-8", "replace")


def _refuse_unprovable(sample: bytes, marker: bytes, probe: str, child: Child) -> None:
    """Fail unless ``sample`` carries a marker this file put in the child.

    Both probes answer "empty" in the same three situations: the process has
    exited, this user may not inspect it, or its block was not captured. All
    three arrive at a negative assertion looking exactly like a clean result,
    and a file of negative assertions would go green forever against a
    machine that leaks. So a reading has to prove what it is a reading *of*
    before it is allowed to say anything about the secret.
    """
    if marker in sample:
        return
    alive = child.proc.poll() is None
    pytest.fail(
        "the {} probe returned {} bytes for pid {}, and none of them carry "
        "{!r} — the marker this file put in that child's environment. So the "
        "sample is not a reading of that child's process image and cannot be "
        "used to say the secret is absent from it.\n"
        "The child is {} its exit-code-poll.".format(
            probe,
            len(sample),
            child.pid,
            marker.decode("utf-8", "replace"),
            "still running at" if alive else "already gone at",
        )
    )


def ps_environ(child: Child, marker: bytes) -> str:
    """What ``ps eew`` says the running process was started with.

    ``e`` prints the environment and the two ``w`` options remove the width
    limit. Without them the block is truncated to the window, and a truncated
    environment is a probe that reports an absence because it could not see
    the end of a line.
    """
    try:
        completed = subprocess.run(
            [_PS_BIN, "eew", str(child.pid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_PS_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        pytest.fail(
            "{} could not be run: {}\n"
            "One of the two probes is what makes the negative assertions in "
            "this file mean anything; a machine where it cannot run has not "
            "answered the question, and skipping quietly would be the one way "
            "this file could report success without evidence.".format(_PS_BIN, exc)
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "{} did not answer for pid {} within {}s.".format(
                _PS_BIN, child.pid, _PS_TIMEOUT_SECONDS
            )
        )

    if completed.returncode != 0:
        pytest.fail(
            "{} exited {} for pid {}.\nstderr: {}".format(
                _PS_BIN,
                completed.returncode,
                child.pid,
                completed.stderr.decode("utf-8", "replace").strip(),
            )
        )

    _refuse_unprovable(completed.stdout, marker, "ps eew", child)
    return _as_text(completed.stdout)


def _procargs_failure(child: Child, code: int) -> str:
    """Turn a failed ``sysctl`` into a failure that says which it was.

    The errnos this can return are different findings, and only one of them is
    about the process: ``ESRCH``/``ENOENT`` means it has gone, ``EPERM`` means
    this user may not look, and anything else is a problem with the call
    itself. All three are failures here, because all three arrive at a
    negative assertion as an empty reading.
    """
    alive = child.proc.poll() is None
    pytest.fail(
        "sysctl(KERN_PROCARGS2, pid={}) failed with {} ({}). No sample means "
        "no evidence, and an absent sample cannot be told apart from an "
        "absent secret.\nThe child is {} its exit-code-poll.".format(
            child.pid,
            code,
            errno_module.errorcode.get(code, code),
            "still running at" if alive else "already gone at",
        )
    )


def procargs2(child: Child, marker: bytes) -> str:
    """What ``KERN_PROCARGS2`` says the kernel has for the running process.

    The same bytes ``execve`` was handed — what ``/proc/<pid>/environ`` is on
    Linux — read through ``sysctl(2)`` rather than the ``sysctl(8)`` command,
    which cannot name a per-process MIB. See the module docstring for why.

    The block begins with the argument count, and that is checked rather than
    assumed: a wrong MIB number returns a plausible-looking buffer of
    something else entirely, and a fragment search over it would report an
    absence just as confidently.
    """
    libc = ctypes.CDLL("libc.dylib", use_errno=True)
    libc.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, child.pid)
    size = ctypes.c_size_t(0)

    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        return _procargs_failure(child, ctypes.get_errno())

    if size.value == 0:
        pytest.fail(
            "KERN_PROCARGS2 reported a zero-length block for pid {}, which no "
            "process has.".format(child.pid)
        )

    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return _procargs_failure(child, ctypes.get_errno())

    sample = buf.raw[:size.value]
    if int.from_bytes(sample[:4], "little") < 1:
        pytest.fail(
            "the KERN_PROCARGS2 block for pid {} does not begin with a "
            "nonzero argument count (first four bytes: {!r}), so it is not a "
            "process image and nothing can be concluded from it. The MIB is "
            "the one this file declares; a different kernel layout would show "
            "up here rather than as a silently clean result.".format(
                child.pid, sample[:4]
            )
        )

    _refuse_unprovable(sample, marker, "KERN_PROCARGS2", child)
    return _as_text(sample)


#: The child's source, as it appears in a reading, matched tolerantly.
#:
#: Every line of :data:`_CHILD_SRC` in order, with whatever the probe put
#: between two of them matched rather than assumed. The tolerance is not
#: caution — it is the only thing that makes the exclusion work at all. The
#: two probes disagree about how an argument reaches the log, and the
#: difference is not cosmetic: ``KERN_PROCARGS2`` hands back the bytes
#: ``execve`` was given, newlines included, while ``ps eew`` prints a
#: *line*, so it escapes the newline out of the argument as the four
#: characters ``\012`` (and escapes ``"`` and ``\`` on the way). A removal
#: written as an exact string therefore matches one probe and silently
#: misses the other, and the miss is invisible: the reading still carries
#: this file's source, the sweep still finds ``secret`` inside
#: ``read_secret_fd``, and the case reports a leak that is an artefact of
#: how the reading was printed. A check that fires on correct code gets
#: switched off, so it has to not fire.
#:
#: The class between two lines is the character set a probe can emit as an
#: escape — a backslash, the digits of an octal escape, a quote, whitespace
#: — and nothing else. The match is still anchored at both ends on lines
#: this file holds, so it can only ever remove the source and nothing that
#: merely resembles it.
_HARNESS_SOURCE_RE = re.compile(
    r'[\\\s0-9"\']*'.join(re.escape(line) for line in _CHILD_SRC.split("\n"))
)


def sweepable_text(sample: str) -> str:
    """The reading with this file's own two strings removed.

    Two exclusions, both load-bearing, and the second one is specific to this
    platform: ``ps eew`` prints the child's *arguments* as well as its
    environment, and those arguments are this file's source, which names
    ``read_secret_fd``. A canary reads ``pdt-test-feishu-secret-<hex>``, and
    the six characters ``secret`` inside it occur in that name — so a sweep
    that ran over the arguments would report every child as leaking, and a
    check that fires on correct code is a check that gets switched off.

    The source is removed through :data:`_HARNESS_SOURCE_RE` rather than as
    a literal, because the two probes do not print an argument the same way
    and a literal only ever reaches one of them. See that constant for the
    escaping it has to tolerate.

    Neither exclusion touches the whole-value assertion, which runs against
    the reading as it was taken: what must never appear is the *value*, and a
    value smuggled inside this file's own source or its two entries would
    still be caught there.
    """
    return _HARNESS_ENTRY_RE.sub("", _HARNESS_SOURCE_RE.sub("", sample))


def leaked_fragments(text: str, value: str) -> list:
    """Every fragment of ``value`` that ``text`` carries, six characters up.

    A substring search rather than an equality test, and deliberately: the
    failure this rules out is a secret that travelled *inside* a larger value
    — prefixed, suffixed, or a whole environment serialised into one variable
    — and equality would call that clean.
    """
    return [
        value[start:start + length]
        for start in range(len(value))
        for length in range(_LEAK_MIN_FRAGMENT, len(value) - start + 1)
        if value[start:start + length] in text
    ]


def assert_no_sentinel_in(sample: str, sentinel: str, probe: str) -> None:
    """The negative assertion, in the form both probes need.

    Reported as a count and a digest rather than as the fragment itself,
    because this text is destined for a CI log and a diagnostic that printed
    the secret it found would publish it at the moment somebody is reading
    about it. The full reading is included: it is the evidence for the count,
    and the sweep has already removed the two strings that are legitimately
    there.
    """
    assert sentinel not in sample, (
        "the running child's {} carries the secret itself.\n"
        "sentinel digest: {}\nfull reading:\n{}".format(
            probe,
            hashlib.sha256(sentinel.encode("utf-8", "surrogateescape")).hexdigest(),
            sample,
        )
    )
    swept = sweepable_text(sample)
    leaked = leaked_fragments(swept, sentinel)
    assert not leaked, (
        "the running child's {} carries part of the secret: {} fragment(s), "
        "the longest {} characters.\nsentinel digest: {}\nfull reading:\n{}".format(
            probe,
            len(leaked),
            max((len(fragment) for fragment in leaked), default=0),
            hashlib.sha256(sentinel.encode("utf-8", "surrogateescape")).hexdigest(),
            swept,
        )
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def deployment(tmp_path, monkeypatch, canary):
    """A deployment whose secrets really live in a real, temporary keychain.

    Redirects what the provider reads from the environment, and touches
    nothing else: the switch is on, ``$HOME`` points into ``tmp_path`` so the
    keychain the provider resolves is the one this file created, every row of
    the spec table has an account exported and an item filed under it, and no
    plaintext fallback is exported at all.

    The platform is *not* patched. This file is macOS-gated, so
    ``sys.platform`` already says what the read needs it to say, and patching
    it here would make the file's one claim — that nothing is replaced — false
    in the very place the claim is made.
    """
    home = tmp_path / "home"
    (home / "Library" / "Keychains").mkdir(parents=True)

    accounts = {name: _account_for(name, canary, tmp_path) for name in credentials.SECRET_SPECS}
    sentinels = {
        name: canary(
            "feishu_secret" if name == LOGICAL_NAME else "telegram_token", tmp_path
        )
        for name in credentials.SECRET_SPECS
    }

    with temporary_keychain(home) as keychain:
        for name, account in accounts.items():
            _file_item(account, sentinels[name], keychain)

        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv(credentials._SWITCH_ENV_KEY, "0")
        for name, account in accounts.items():
            monkeypatch.setenv(credentials.SECRET_SPECS[name].account_env_key, account)
            # The whole point of the file in one line: with a fallback
            # exported, "it resolved" and "it resolved from the keychain"
            # are the same observation, and only the label would tell them
            # apart.
            monkeypatch.delenv(
                credentials.SECRET_SPECS[name].fallback_env_key, raising=False
            )
        credentials.reset_cache()

        assert credentials.keychain_disabled() is False, (
            "the switch is on and the platform is macOS, so the keychain must "
            "not be reported as unavailable — if it is, nothing below is "
            "testing what this file claims to test"
        )
        assert cli._keychain_tool_available() is True, (
            "{} is present but not runnable here, so the real read cannot "
            "happen and every case below would be reporting a green answer to "
            "a question this machine did not answer".format(_SECURITY_BIN)
        )
        yield Deployment(home, keychain, accounts, sentinels)

    credentials.reset_cache()


@pytest.fixture
def run_child(register_child_process):
    """Return a function that starts a parked reader of the handoff."""

    def _run(source: str, *, env: dict, pass_fds: tuple = ()) -> Child:
        return _spawn_child(register_child_process, source, env=env, pass_fds=pass_fds)

    return _run


@pytest.fixture
def published_secret(deployment, register_open_fd):
    """Return ``(secret, fd)`` for a handoff out of the real keychain.

    The value is a canary, filed in a keychain this file created and read
    back through the real tool: the assertion in this file is about where a
    value travels, and a real credential would put a real credential into
    every failure message it can produce.

    The descriptor is registered, because the module deliberately hands the
    read end to the caller and closes only the write end — a descriptor this
    file opened is a descriptor this file owns until the child spends it.
    """
    secret = deployment.sentinel()
    fd = credentials.publish_secret_fd(LOGICAL_NAME)
    assert fd is not None, (
        "nothing was published, so there is no handoff to inspect: the "
        "keychain read returned no value for {}".format(LOGICAL_NAME)
    )
    register_open_fd(fd)
    return secret, fd


def _handshake(child: Child, deployment: Deployment, secret: str) -> Report:
    """Read the child's one line and check it against the sentinel.

    The comparison happens here, before any probe is asked, so a negative
    assertion downstream is always a statement about a child that really holds
    the secret. Without it, a child that never received the payload would
    produce two clean "the secret is not in the environment" results, and the
    file would be green about a handoff that never happened.
    """
    parts = child.read_ready_line().split()
    if not parts or parts[0] != _READY or len(parts) != 3:
        child.finish()
        pytest.fail(
            "the child announced {!r} where '{} <digest> <digest>' was "
            "expected, so nothing downstream can be read as a report about "
            "the payload.".format(" ".join(parts) or "<nothing>", _READY)
        )
    report = Report(sent=parts[1], used=parts[2])
    assert report.sent == deployment.digest(secret), (
        "the child did not read the published payload — the digest it "
        "reported is not the sentinel's — so the probes below would be "
        "sampling a process that never held the secret, and their answer "
        "would mean nothing.\nreported digest: {}".format(report.sent)
    )
    return report


# ---------------------------------------------------------------------------
# The cases
# ---------------------------------------------------------------------------


@requires_real_tools
def test_full_delivery_reads_the_real_keychain_entry(deployment, published_secret):
    """``secrets verify`` names the keychain as the source — from a real read.

    The command is run as a process rather than called as a function, for
    three reasons at once: it is the interface an operator actually uses; its
    resolution memo starts empty, so the source it reports is one real lookup
    and not a value some earlier call in this test session left behind; and it
    resolves ``$HOME`` in a process that inherited nothing from this one, which
    is the resolution the acceptance is about.

    The exit code is asserted too. It is the command's own verdict — every
    secret resolved, and the platform can serve a keychain — and a case that
    checked the label while ignoring the code would pass on a machine whose
    keychain tool cannot run at all, which is the one machine whose answer
    means nothing.

    And the value itself is checked for absence: the command reports *where* a
    secret comes from, never the secret, and a run that printed it would be
    publishing a credential into the very log this file is read from.
    """
    secret, _fd = published_secret

    completed = subprocess.run(
        [sys.executable, str(_BACKEND_DIR / "cli.py"), "secrets", "verify"],
        cwd=str(_BACKEND_DIR),
        env=deployment.env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_SECURITY_TIMEOUT_SECONDS,
    )
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")

    line = _verify_row(stdout, LOGICAL_NAME)
    assert "source=keychain" in line, (
        "`secrets verify` did not report the keychain as the source of "
        "{}:\n{!r}\nstdout:\n{}stderr:\n{}".format(
            LOGICAL_NAME, line, stdout, stderr
        )
    )
    assert "source=os.environ" not in stdout, (
        "a secret was reported as coming from the environment, and this "
        "deployment exports no plaintext fallback — so either a residue "
        "leaked in from the ambient environment or the switch is being read as "
        "off.\nstdout:\n{}".format(stdout)
    )
    assert completed.returncode == 0, (
        "`secrets verify` exited {} where every secret resolved from a real "
        "keychain. Its own verdict is part of what this case is checking.\n"
        "stdout:\n{}stderr:\n{}".format(completed.returncode, stdout, stderr)
    )
    assert secret not in stdout, (
        "`secrets verify` printed the secret itself. The command reports where "
        "a secret is read from; a run that printed the value would be "
        "publishing a credential into the log this failure is read from."
    )


def _verify_row(stdout: str, name: str) -> str:
    """The ``secrets verify`` row for ``name``, or a failure naming the output."""
    for row in stdout.splitlines():
        fields = row.split()
        if fields and fields[0] == name:
            return row
    pytest.fail(
        "`secrets verify` printed no row for {} at all.\nThe rows it did "
        "print:\n{}".format(name, stdout)
    )


@requires_real_tools
def test_ps_eew_output_excludes_the_secret(deployment, published_secret, run_child):
    """The process listing of a child that holds the secret shows no secret.

    Non-vacuous by construction: :func:`_handshake` has already compared the
    digest of what the child read off the descriptor, so this is a statement
    about a process carrying the secret rather than about one that never
    received it. The sample is itself checked — :func:`_refuse_unprovable`
    refuses a reading that does not carry the descriptor entry — so the
    assertion is never reached with an empty or foreign reading in hand.
    """
    secret, fd = published_secret

    child = run_child(
        _CHILD_SRC, env=child_env(fd), pass_fds=(fd,)
    )
    _handshake(child, deployment, secret)

    sample = ps_environ(child, _marker(fd))
    assert child.finish() == 0, "the child did not exit cleanly once released"

    assert_no_sentinel_in(sample, secret, "ps eew listing")


@requires_real_tools
def test_kern_procargs2_output_excludes_the_secret(deployment, published_secret, run_child):
    """The kernel's own copy of that process image shows no secret either.

    The same question asked of the same child through a different door. Two
    probes agree only if both work, and a single probe on a platform this file
    does not control is one implementation's opinion about what a process
    image contains.
    """
    secret, fd = published_secret

    child = run_child(
        _CHILD_SRC, env=child_env(fd), pass_fds=(fd,)
    )
    _handshake(child, deployment, secret)

    sample = procargs2(child, _marker(fd))
    assert child.finish() == 0, "the child did not exit cleanly once released"

    assert_no_sentinel_in(sample, secret, "KERN_PROCARGS2 block")


@requires_real_tools
def test_positive_control_ps_eew_does_see_the_secret(deployment, published_secret, run_child):
    """A child that *is* handed the secret through the environment is caught.

    The control for both cases above, and the reason they can be believed.
    The sentinel is put in the child's own initial environment under the very
    variable this project is removing — the exact shape of the leak being
    ruled out, produced deliberately — and **both** probes have to see it.

    The child is otherwise identical: it still reads the keychain's value over
    a real descriptor, so a probe that had seen the control's sentinel by way
    of the descriptor rather than the environment would still be a broken
    probe, and this is what rules that reading out.

    If this case ever fails, neither probe is reading a process image, and
    every "the secret is absent" assertion in this file is answering a
    question about nothing. That is the failure mode a purely negative file
    cannot see about itself.
    """
    secret, fd = published_secret

    child = run_child(
        _CHILD_SRC,
        env=child_env(fd, extra={SPEC.fallback_env_key: secret}),
        pass_fds=(fd,),
    )
    _handshake(child, deployment, secret)

    listing = ps_environ(child, _marker(fd))
    image = procargs2(child, _marker(fd))
    assert child.finish() == 0, "the control child did not exit cleanly once released"

    assert secret in listing, (
        "the ps probe did not see a sentinel that was in the control child's "
        "own environment, so it cannot be trusted to report that a secret is "
        "absent from one.\nfull listing:\n{}".format(listing)
    )
    assert secret in image, (
        "the KERN_PROCARGS2 probe did not see a sentinel that was in the "
        "control child's own environment, so it cannot be trusted to report "
        "that a secret is absent from one.\nfull process image:\n{}".format(image)
    )


@requires_real_tools
def test_delivery_payload_actually_carries_the_secret(deployment, published_secret, run_child):
    """The child received the keychain's value, and used it.

    The negative cases above are all satisfied by a child that never got the
    value at all, so they need this one: the value was read out of a real
    keychain by the real tool, handed over a real descriptor, and read back by
    a real interpreter — and the same value is what authenticated the request
    the child's transport was shown.

    Two digests, not one, because they answer different questions. The first
    is what the child read off the descriptor; the second is what the
    transport received. A payload that carried the value and a child that used
    it are separate claims, and reporting only the first would leave "used"
    resting on the reader's good intentions.
    """
    secret, fd = published_secret
    child = run_child(
        _CHILD_SRC, env=child_env(fd), pass_fds=(fd,)
    )

    report = _handshake(child, deployment, secret)
    assert child.finish() == 0, "the child did not exit cleanly once released"

    assert report.sent == deployment.digest(secret), (
        "the payload the child read was not the value in the keychain.\n"
        "expected digest: {}\nreported digest: {}".format(
            deployment.digest(secret), report.sent
        )
    )
    assert report.used == deployment.digest(_AUTHORIZATION_PREFIX + secret), (
        "the transport saw something other than the value the child read off "
        "the descriptor, so the payload was delivered and not used.\n"
        "expected digest: {}\nreported digest: {}".format(
            deployment.digest(_AUTHORIZATION_PREFIX + secret), report.used
        )
    )


class _DeliberateFailure(Exception):
    """Raised by a case to drive a cleanup path with a failure on it."""


@requires_real_tools
def test_temporary_keychain_is_removed_even_on_failure(tmp_path):
    """A keychain created by a case is destroyed even when the case fails.

    Both answers are driven from here, in order, because a cleanup that can
    only fire on the happy path is not a cleanup: the context manager is
    entered, an item is written and read back — so the keychain is provably
    alive and provably holding something — and then the body raises.

    The manager is the same one every other case in this file gets its
    keychain from, entered directly rather than through a fixture, because a
    fixture's teardown cannot be observed from inside the test it is tearing
    down: by the time it has run, the case is over.

    The post-condition is checked twice over. The file being gone is the
    filesystem's answer; asking the keychain tool about the item afterwards is
    the tool's, and a delete that half-succeeded would satisfy the first and
    fail the second.
    """
    home = tmp_path / "home"
    (home / "Library" / "Keychains").mkdir(parents=True)
    account = "cli_sentinel_cleanup"
    value = "cleanup-canary-0000"
    created = []

    with pytest.raises(_DeliberateFailure):
        with temporary_keychain(home) as keychain:
            created.append(keychain)
            _file_item(account, value, keychain)
            found = _security(
                "find-generic-password", "-a", account, "-w", str(keychain)
            )
            assert found.stdout.decode("utf-8", "replace").strip() == value, (
                "the item was not readable while the keychain was open, so the "
                "case never proved it had anything to clean up"
            )
            raise _DeliberateFailure("the assertion a case would have failed on")

    (keychain,) = created
    assert not keychain.exists(), (
        "{} survived a case that raised inside it. Every other case in this "
        "file runs against a keychain it created, and a leftover one is a "
        "keychain the next case can resolve to instead of its own.".format(
            keychain
        )
    )
    after = _security(
        "find-generic-password", "-a", account, "-w", str(keychain), check=False
    )
    assert after.returncode != 0, (
        "the keychain file is gone but {} still answered for the item, so the "
        "delete did not do what the file's absence says it did".format(_SECURITY_BIN)
    )


# ---------------------------------------------------------------------------
# The guard on the delete
# ---------------------------------------------------------------------------
#
# Everything above assumes the keychain being destroyed belongs to the case
# that is destroying it. That assumption is carried by ``$HOME`` having been
# redirected to ``tmp_path``, and it is the only thing standing between a lost
# redirect and the destruction of a real one. It used to be a safe thing to
# get wrong: the path resolved to the login keychain, which macOS rebuilds on
# demand, so the worst a stale ``$HOME`` could do was empty a keychain the
# user had already been prompted to recreate. ``credentials._KEYCHAIN_PATH``
# now names this project's dedicated keychain instead — the container that
# holds the real credentials — and nothing in this project can put an item
# back into it once it is gone.
#
# So the delete checks before it destroys, against a home directory captured
# at import rather than read at teardown, and a mismatch is a failure that
# leaves the file alone.


def test_the_delete_guard_refuses_the_real_home_directory(tmp_path):
    """A keychain under the real home is refused, whatever else is true."""
    with pytest.raises(pytest.fail.Exception, match="delete"):
        _assert_disposable(
            _REAL_HOME, _REAL_HOME / credentials._KEYCHAIN_PATH
        )


def test_the_delete_guard_refuses_a_real_home_with_a_redirected_file(tmp_path):
    """A disposable home does not make a real-home keychain disposable."""
    with pytest.raises(pytest.fail.Exception, match="delete"):
        _assert_disposable(
            tmp_path / "home",
            _REAL_HOME / "Library" / "Keychains" / "runtime-secrets.keychain-db",
        )


def test_the_delete_guard_allows_a_keychain_under_the_temporary_directory(tmp_path):
    """The case this file actually runs is allowed through."""
    _assert_disposable(
        tmp_path / "home",
        tmp_path / "home" / credentials._KEYCHAIN_PATH,
    )


def test_the_cleanup_checks_disposability_before_it_deletes(tmp_path, monkeypatch):
    """The guard is on the path out, not merely defined somewhere nearby.

    Driven with the keychain tool replaced by a recorder, so this asserts the
    ordering and the wiring without creating a keychain: the guard has to run
    before the delete, and a delete that ran first could not be taken back.
    """
    order = []

    def _fake_security(*args, **kwargs):
        order.append(args[0])
        if args[0] == "create-keychain":
            # The manager checks the file exists after creating it, and a
            # stand-in that writes nothing fails the case for the wrong reason.
            Path(args[1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[1]).touch()
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(
        sys.modules[__name__], "_security", _fake_security
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_assert_disposable",
        lambda home, keychain: order.append("guard"),
    )

    with temporary_keychain(tmp_path / "home"):
        pass

    assert order == ["create-keychain", "guard", "delete-keychain"], (
        "the cleanup must check that the keychain is disposable and then "
        "delete it, in that order. Got: {}".format(order)
    )


def test_a_refused_keychain_is_not_deleted(tmp_path, monkeypatch):
    """When the guard refuses, the delete does not happen at all.

    This case is the one place in the file that *must* point a home at
    the operator's own directory tree — the guard's second branch fires
    on a keychain resolving under the real home, and no other path
    reaches it. So the fake ``create-keychain`` really does create a
    directory and a zero-byte file there, because
    ``temporary_keychain`` checks the file exists before yielding.

    Which means this case owes the machine a cleanup. It did not have
    one: every run left ``<real home>/not-a-redirect/Library/
    Keychains/runtime-secrets.keychain-db`` behind, in the one
    directory this file's whole guard exists to keep its hands out of.
    The litter is harmless — the tool never ran, so it is a 0-byte file
    and no keychain was ever added to any search list — but a test that
    writes into the operator's home and calls that acceptable is a test
    whose neighbours will do worse.

    The cleanup is conditional on the directory not having existed
    beforehand: ``not-a-redirect`` is a name a person could plausibly
    have given a real directory, and ``rmtree`` does not ask.
    """
    litter = _REAL_HOME / "not-a-redirect"
    pre_existed = litter.exists()
    deleted = []

    def _fake_security(*args, **kwargs):
        if args[0] == "delete-keychain":
            deleted.append(args[1])
        if args[0] == "create-keychain":
            Path(args[1]).parent.mkdir(parents=True, exist_ok=True)
            Path(args[1]).touch()
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(sys.modules[__name__], "_security", _fake_security)

    try:
        with pytest.raises(pytest.fail.Exception, match="delete"):
            with temporary_keychain(litter):
                pass

        assert deleted == [], (
            "the guard refused, so nothing may have been deleted. Deleted: "
            "{}".format(deleted)
        )
    finally:
        if not pre_existed:
            shutil.rmtree(litter, ignore_errors=True)


# ---------------------------------------------------------------------------
# The capability gate, and the checks on the gate itself
# ---------------------------------------------------------------------------
#
# A skip is only honest if it says what it skipped *for*, and only safe if the
# thing it keys on is the real thing. Both halves are asserted below, because
# this gate is the one piece of machinery in the file that can make every case
# in it disappear — and a gate that has quietly stopped gating is
# indistinguishable, from the outside, from a suite that passed.
#
# The gate probes for the ability to *start* a process, which is the only
# question it exists to answer: a machine whose security policy refuses
# ``execve`` of the keychain tool cannot run the cases, and reporting a defect
# in code that was never reached says nothing about that code. Everything past
# the gate stays a hard failure, so a machine that *can* start the tool and
# then misbehaves is still red.


def test_capability_probe_accepts_a_binary_this_process_already_ran():
    """The probe must not be stuck on — a false negative skips the whole file.

    The failure this rules out is the quiet one: a probe that always answers
    "unrunnable" turns every case below into a skip, and a suite that skips
    everything is green in CI and says nothing, which is the state this file
    exists to prevent.
    """
    runnable, reason = _tool_is_runnable(sys.executable)
    assert runnable, (
        "the capability probe reported {} — an interpreter this very process "
        "is running out of — as unrunnable, so it would skip every case in "
        "this file. {}".format(sys.executable, reason)
    )


def test_capability_probe_rejects_a_path_that_does_not_exist(tmp_path):
    """Absent is not runnable, and the reason has to name it.

    The two are checked separately because they are two different verdicts a
    skip message has to tell apart: a machine that never had the tool is not
    the machine whose tool was refused.
    """
    missing = str(tmp_path / "no-such-binary")
    runnable, reason = _tool_is_runnable(missing)
    assert not runnable, "{} does not exist but the probe called it runnable".format(
        missing
    )
    assert missing in reason, (
        "the reason a skip prints has to name what it could not run, or the "
        "line is not actionable. reason: {!r}".format(reason)
    )


def test_capability_probe_rejects_a_file_without_execute_permission(tmp_path):
    """Present and not runnable is a third verdict, and the common one.

    This is what a machine with a restrictive policy on the keychain tool
    actually looks like: the file is there, its mode is intact, and starting
    it is refused. A probe that only checked for existence would call that
    machine capable and the cases would run into the refusal.
    """
    script = tmp_path / "not-executable"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o600)
    runnable, reason = _tool_is_runnable(str(script))
    assert not runnable, (
        "{} has no execute bit but the probe called it runnable, so a machine "
        "that cannot start it would be treated as one that can".format(script)
    )


def test_capability_probe_agrees_with_actually_starting_the_binary(tmp_path):
    """The control: the probe's verdict is the same as the fact.

    Every other case here is a negative assertion, and "the probe said no" is
    satisfied by a probe that says no about everything — the exact shape of a
    file that skips itself into silence. So this case actually starts a binary
    it knows is startable and a file it knows is not, and requires the probe
    to agree both times. A probe stuck in either direction fails here.
    """
    missing = tmp_path / "absent"
    refused = tmp_path / "refused"
    refused.write_text("#!/bin/sh\nexit 0\n")
    refused.chmod(0o600)

    for path in (sys.executable, str(missing), str(refused)):
        try:
            subprocess.run(
                [path, "-h"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=_SECURITY_TIMEOUT_SECONDS,
            )
            started = True
        except OSError:
            started = False
        runnable, reason = _tool_is_runnable(path)
        assert runnable is started, (
            "the probe disagrees with what actually happened for {}: the "
            "attempt to start it {} but the probe answered runnable={} "
            "({!r}). A gate that does not track reality gates nothing.".format(
                path, "succeeded" if started else "was refused", runnable, reason
            )
        )


def test_capability_gates_are_keyed_on_the_production_binaries():
    """The gate reads the provider's own constants, so no stand-in can pass it.

    A gate pointed at anything other than the binaries production runs would
    open on exactly the machine it exists to detect: this file's whole claim is
    that nothing is replaced, and a skip that fired for the wrong binary would
    report the claim as untested rather than as false.
    """
    assert _GATED_KEYCHAIN_TOOL == credentials._SECURITY_BIN, (
        "the keychain gate is keyed on {!r} but the provider reads {}".format(
            _GATED_KEYCHAIN_TOOL, credentials._SECURITY_BIN
        )
    )
    assert _GATED_PROCESS_LISTING == _PS_BIN, (
        "the process-listing gate is keyed on {!r} rather than the binary the "
        "probe runs".format(_GATED_PROCESS_LISTING)
    )
    for path in (_GATED_KEYCHAIN_TOOL, _GATED_PROCESS_LISTING):
        assert os.path.isabs(path), (
            "{!r} is not absolute, so it names something relative to wherever "
            "the suite happened to be run from".format(path)
        )


def _capability_gate_shape(case) -> Optional[str]:
    """Which position gated ``case``, or ``None`` for a case that is ungated.

    The default position is found by comparing the mark against the
    module-level gate's own — by value, not by name. A ``skipif`` is a shape
    rather than a verdict, and on a machine with no refusals this file's gate
    is ``skipif(False)`` with no reason, indistinguishable by name from any
    other ``skipif`` the file might grow. Comparing against the gate the module
    actually built is the only comparison that means "this case was gated by
    *this* gate".

    The strict position is found by its tag, and on a machine that entered it
    the module-level decorator is a function rather than a mark, so there is
    nothing to compare against and the tag is all there is.
    """
    if getattr(case, _CAPABILITY_GATE_ATTR, None):
        return "strict"
    module_mark = getattr(requires_real_tools, "mark", None)
    if module_mark is None:
        return None
    wanted = (module_mark.name, module_mark.args, module_mark.kwargs)
    for mark in getattr(case, "pytestmark", []):
        if (mark.name, mark.args, mark.kwargs) == wanted:
            return "skipif"
    return None


def test_every_real_case_carries_a_capability_gate():
    """A case added without the gate would run on a machine that cannot serve it.

    The structural half of the same guarantee: a new case in this file that
    forgot the decorator would not be caught by any other check here, and would
    report a defect about a machine the case was never able to run on. Driven
    from the declaration rather than from a list of names, so there is one list
    and the per-case requirement and the gate cannot drift apart: a case
    declared here and not decorated is caught, and — the other direction — a
    case that *is* decorated and not declared is caught too, since the strict
    position would be judging a case whose tools nothing has ever said.

    Both gate shapes count, and that is not a relaxation. The gate has two
    positions, and only one of them is a mark: the strict position is a
    call-time wrapper, and a wrapper has no ``pytestmark`` at all. A check
    that looked for ``skipif`` alone would therefore pass on every machine
    whose tools start and go red on exactly the machines the strict position
    exists for — reporting the gate as missing where the gate is present, in
    the file whose subject is not confusing a gate that is working with one
    that has stopped.

    The second half asserts the strict shape is *recognised*, by asking the
    same question of a case the strict position builds. Which position this
    module is in is a fact about the machine, so a check that only ever
    inspected the live cases would test the strict shape on a machine that
    never entered it — which is every machine, usually.
    """
    for name in _GATED_CASE_TOOLS:
        case = globals().get(name)
        assert case is not None, (
            "{} is declared as a gated case but this file defines no such "
            "function, so the declaration has drifted from the file".format(name)
        )
        assert _capability_gate_shape(case) is not None, (
            "{} carries neither the default skip nor a strict capability gate, "
            "so on a machine that cannot start {} or {} it would fail for a "
            "reason that is not a defect in the code under test.".format(
                name, _GATED_KEYCHAIN_TOOL, _GATED_PROCESS_LISTING
            )
        )

    for name, case in sorted(globals().items()):
        if not name.startswith("test_") or not callable(case):
            continue
        if _capability_gate_shape(case) is None:
            continue
        assert name in _GATED_CASE_TOOLS, (
            "{} carries this file's capability gate but declares no tools in "
            "_GATED_CASE_TOOLS. A case the gate judges without a declaration is "
            "a case whose requirement the strict position cannot narrow.".format(
                name
            )
        )

    # The strict shape, driven rather than waited for: the same question asked
    # of a case the strict position builds on any machine.
    def _ungated(deployment):
        pass

    strictly_gated = _capability_gate(
        ["a refusal this machine does not currently have"], strict=True
    )(_ungated)
    assert getattr(strictly_gated, _CAPABILITY_GATE_ATTR, None), (
        "a case gated by the strict position does not announce itself, so the "
        "check above cannot tell it from a case that was never gated at all — "
        "and the strict position is the one that runs on a machine where the "
        "default gate would have skipped it"
    )


# ---------------------------------------------------------------------------
# The gate per case: which tool each case is actually about
# ---------------------------------------------------------------------------
#
# The gate above asks one question of one machine: "can this installation start
# the binaries this file gates on". That is the right question for a *skip line*
# — whoever opened the lane needs to know what the machine could not do, and
# naming both binaries tells them that.
#
# It is the wrong question for a *case*, because the six are not six samples of
# one experiment. Three read a keychain and never run the process listing; three
# answer a process-visibility question. A machine whose policy refuses
# ``/bin/ps`` cannot run three of them and can run the other three perfectly
# well — so a single verdict over both binaries lets an unavailability that
# touches one tool silently withdraw the evidence for a question the machine
# *could* have answered. That is the whole of what the strict position must not
# do: one tool being unavailable may not veto a case that never starts it.
#
# So the requirement is declared per case (:data:`_GATED_CASE_TOOLS`) and the
# judgement is a pure function of that declaration plus a decision about which
# paths the machine will not start. It is a function rather than a probe so it
# can be driven from both ends: production hands it the real probe's verdict, and
# the checks below hand it a refusal for a tool the case never starts — which is
# the only way to watch the rule work on a machine that starts everything, and
# the reason nothing here starts a binary to make one refuse.


def test_a_case_is_gated_only_on_the_tools_it_uses():
    """The rule itself, driven from a refusal the machine is assumed to have.

    Injected as a *decision* — a callable answering "would this path be
    unstartable" — rather than as a probe, because the machine this has to be
    demonstrated on is one that starts both binaries, and manufacturing a
    refusal for a real system path is not something a test may do to the
    machine it runs on.
    """
    ps_only = "test_ps_eew_output_excludes_the_secret"
    keychain_only = "test_temporary_keychain_is_removed_even_on_failure"

    # A ps-only case that is not one would pass this check vacuously, so the
    # declaration it is being judged against is read first.
    assert _GATED_PROCESS_LISTING in _GATED_CASE_TOOLS[ps_only], (
        "{} declares {!r}, so it does not use the process listing this case "
        "checks and the assertion below would be about nothing.".format(
            ps_only, _GATED_CASE_TOOLS[ps_only]
        )
    )

    def refuses_the_keychain_tool(path: str) -> bool:
        return path == _GATED_KEYCHAIN_TOOL

    assert _case_missing_tools(ps_only, refuses_the_keychain_tool) == (), (
        "a case that only runs the process listing was gated on the keychain "
        "tool being refused. The two are independent facilities: a policy that "
        "refuses {} removes no evidence from a case that never starts it, and "
        "gating it anyway is an unavailability silently deleting a question "
        "this machine could still have answered.".format(_GATED_KEYCHAIN_TOOL)
    )
    assert _case_missing_tools(keychain_only, refuses_the_keychain_tool) == (
        _GATED_KEYCHAIN_TOOL,
    ), (
        "the case that does read a keychain was not gated on the tool it "
        "starts, so a refusal of {} would let it run into the very refusal the "
        "gate exists to answer for.".format(_GATED_KEYCHAIN_TOOL)
    )


def test_every_gated_case_declares_its_tools():
    """The declaration is complete, and every entry is one of the two tools.

    Three properties, and none of them is optional. A name here that the file
    does not define is a list that has drifted from the code. An empty entry is
    a case gated by omission — nothing refuses nothing, so it runs on a machine
    the gate was written to catch. An entry naming a third path is a gate keyed
    on a binary this file never runs, which is the exact failure the gate's
    constants exist to prevent.
    """
    assert (_GATED_KEYCHAIN_TOOL, _GATED_PROCESS_LISTING) == (
        "/usr/bin/security",
        "/bin/ps",
    ), (
        "the two gated paths moved (now {!r} and {!r}); this file's gate is "
        "keyed on the binaries production runs, so the names below are quoted "
        "rather than interpolated and need re-reading".format(
            _GATED_KEYCHAIN_TOOL, _GATED_PROCESS_LISTING
        )
    )
    assert len(_GATED_CASE_TOOLS) == 6, (
        "the file has six cases behind the capability gate and the declaration "
        "names {}. A case added without declaring the tools it starts is a case "
        "no gate can reason about.".format(len(_GATED_CASE_TOOLS))
    )

    for name, tools in sorted(_GATED_CASE_TOOLS.items()):
        assert globals().get(name) is not None, (
            "{} is declared as a gated case but this file defines no such "
            "function, so the declaration has drifted from the code".format(name)
        )
        assert tools, (
            "{} declares no tools, so nothing can ever be unstartable for it and "
            "the gate covers no case at all".format(name)
        )
        assert isinstance(tools, tuple), (
            "{} declares {!r} rather than a tuple, so membership and ordering "
            "are not part of its type and a reader cannot tell a single path "
            "from a list of them".format(name, tools)
        )
        for path in tools:
            assert path in (_GATED_KEYCHAIN_TOOL, _GATED_PROCESS_LISTING), (
                "{} declares {!r}, which is not one of the two binaries this "
                "file gates on. A gate keyed on anything else opens on exactly "
                "the machine it exists to detect.".format(name, path)
            )


def test_unused_tools_do_not_block_a_case():
    """The other direction, and the one this machine is actually in.

    A refusal of the process listing must not reach the cases that read a
    keychain — the delivery cases, the payload case and the cleanup case — and
    the positive control, which is about both probes, is checked from the other
    side so the rule is not merely a pass for whichever direction the reader
    happened to imagine.
    """
    keychain_only = "test_temporary_keychain_is_removed_even_on_failure"
    ps_only = "test_ps_eew_output_excludes_the_secret"
    control = "test_positive_control_ps_eew_does_see_the_secret"

    def refuses_the_process_listing(path: str) -> bool:
        return path == _GATED_PROCESS_LISTING

    for name in (
        keychain_only,
        "test_full_delivery_reads_the_real_keychain_entry",
        "test_delivery_payload_actually_carries_the_secret",
    ):
        assert _case_missing_tools(name, refuses_the_process_listing) == (), (
            "{} never starts {}, so a refusal of it cannot be why this case "
            "does not run — and reporting otherwise tells a reader to go and "
            "look at a policy that has nothing to do with the case they are "
            "reading about.".format(name, _GATED_PROCESS_LISTING)
        )
    assert _case_missing_tools(ps_only, refuses_the_process_listing) == (
        _GATED_PROCESS_LISTING,
    ), "a case that runs the process listing was not gated on it"
    assert _case_missing_tools(control, refuses_the_process_listing) == (
        _GATED_PROCESS_LISTING,
    ), "the positive control runs the process listing and was not gated on it"


def test_strict_gate_does_not_block_a_case_whose_own_tools_start():
    """Per-case judging reaches the gate, not just the helper above it.

    The two checks above pin a rule; this pins that the rule is what the strict
    position actually decides by. A gate that went on consulting the whole
    machine would pass them and go on failing every case for a tool it never
    starts, which is the collateral the declaration exists to remove.

    The case that is left alone has to be handed back *unchanged* — the strict
    wrapper drops a case's signature so that a refusal is the only thing that
    happens (see :func:`_strict_capability_gate`), and a case that is permitted
    to run cannot afford that. So the identity of the returned object is the
    assertion, and a wrapper that decided per case but still wrapped would fail
    it.
    """
    reason = "{} cannot be started here: Permission denied".format(
        _GATED_PROCESS_LISTING
    )
    gate = _capability_gate([reason], strict=True, refused=(_GATED_PROCESS_LISTING,))

    permitted = gate(test_temporary_keychain_is_removed_even_on_failure)
    assert permitted is test_temporary_keychain_is_removed_even_on_failure, (
        "the strict position wrapped a case whose own tools this machine "
        "starts. The wrapper drops the signature, so the case would fail in "
        "setup for a tool it never runs — a machine refusing {} would report a "
        "defect in a case it could have executed.".format(_GATED_PROCESS_LISTING)
    )
    assert getattr(permitted, _CAPABILITY_GATE_ATTR, None) == "strict", (
        "the case the strict position let through does not announce that it was "
        "judged, so the structural check cannot tell a gated case from an "
        "ungated one"
    )

    refused = gate(test_ps_eew_output_excludes_the_secret)
    assert refused is not test_ps_eew_output_excludes_the_secret, (
        "the case that does start {} was not gated, so the strict position "
        "reported green for a machine that cannot run it".format(
            _GATED_PROCESS_LISTING
        )
    )
    assert not refused.__signature__.parameters, (
        "the strict wrapper is supposed to advertise no fixtures, so that the "
        "refusal is the only thing that happens and the case is reported as a "
        "failure rather than as an error in the harness"
    )

    with pytest.raises(BaseException) as failure:
        refused()
    message = str(failure.value)
    assert _GATED_PROCESS_LISTING in message, (
        "the strict failure does not name the tool the case could not start"
    )
    assert _GATED_KEYCHAIN_TOOL not in message, (
        "the strict failure names {}, which this case never starts. A reader "
        "sent to that policy would be chasing a refusal that has nothing to do "
        "with the case in front of them.\nmessage: {}".format(
            _GATED_KEYCHAIN_TOOL, message
        )
    )


def test_default_skip_reason_still_renders_the_whole_machine(tmp_path):
    """The default position's wording, byte for byte, because it is a contract.

    Per-case judging was added for the strict position, and the default position
    is where its absence has to be pinned: a skip line here is read by whoever
    opened the lane, and they need "this installation will not start these two
    binaries" rather than a verdict about whichever case was reported. The text
    is quoted whole rather than rebuilt from the constants, because a check that
    assembled the expected value out of the same pieces would pass on any
    rewording of the sentence around them.
    """
    path, reason = _refused_tool(tmp_path)
    assert (_GATED_KEYCHAIN_TOOL, _GATED_PROCESS_LISTING) == (
        "/usr/bin/security",
        "/bin/ps",
    ), (
        "the two gated paths moved, so the quoted text below no longer describes "
        "this file's gate and would pass or fail for the wrong reason"
    )

    gate = _capability_gate([reason], False)

    assert gate.mark.kwargs["reason"] == (
        "this machine will not start /usr/bin/security or /bin/ps so the real "
        "keychain read and the process probes cannot happen here: "
        "{}. Nothing in this file is replaced, so a machine that refuses the "
        "tool cannot answer the question either way; the run is reported as "
        "not-run rather than as a failure in the code, which was never "
        "reached.".format(reason)
    ), (
        "the default position's skip reason no longer reads as a statement "
        "about the machine. It is a per-case change that must not have reached "
        "this position.\nrendered: {}".format(gate.mark.kwargs["reason"])
    )


# ---------------------------------------------------------------------------
# The strict switch: what the environment variable means
# ---------------------------------------------------------------------------
#
# A skip is only useful if something can say "this one was not allowed to
# skip". The gate above has one position, and a lane that lands in it reports
# green while publishing no evidence at all — the six cases above are then not
# passing, they are absent, and nothing in the job can tell the two apart.
#
# The switch is the other position: when it is set, "this machine cannot run
# the tools" stops being an answer and becomes the failure it has always been
# underneath. What is pinned here is not what the switch *does* — that is the
# gate's business, built on top of this — but what the switch *says*, because
# a parser whose spelling rules are wider than its documentation is a lane that
# goes quiet in a way nobody chose.


def test_strict_switch_truth_table(monkeypatch):
    """Every class of value the switch can carry, one cell at a time.

    The cells are asserted individually rather than through one representative
    per class: a parser that gets ``1`` right and ``true`` wrong still has a
    hole, and a hole here is a job that skips in a place its author believed
    it was speaking.
    """
    monkeypatch.delenv(_STRICT_SWITCH_ENV, raising=False)
    table = (
        # (the kind of value, the value, what it has to decide)
        ("explicit on", "1", True),
        ("explicit on", "true", True),
        ("explicit on", " 1 ", True),
        ("explicit on", "\ttrue\n", True),
        ("explicit off", "0", False),
        ("explicit off", "false", False),
        ("explicit off", "yes", False),
        ("explicit off", "on", False),
        ("unset", None, False),
        ("blank", "", False),
        ("blank", "   ", False),
        ("blank", "\t\n", False),
        ("illegal", "maybe", False),
        ("illegal", "2", False),
        ("illegal", "truthy", False),
        ("illegal", "tr ue", False),
        ("non-ascii", "truë", False),
        ("non-ascii", "１", False),
    )
    for kind, raw, expected in table:
        if raw is None:
            monkeypatch.delenv(_STRICT_SWITCH_ENV, raising=False)
        else:
            monkeypatch.setenv(_STRICT_SWITCH_ENV, raw)
        decided = require_real_tools_requested()
        assert decided is expected, (
            "{} value {!r} was read as {!r} where {!r} was required. The "
            "switch decides whether this file's cases are allowed to skip, so "
            "every cell of this table is a lane that is either silent or "
            "loud.".format(kind, raw, decided, expected)
        )


def test_strict_switch_reread_per_call(monkeypatch):
    """The verdict is read fresh on every call, never snapshotted at import.

    The switch is a fact about the process that runs the cases, and this
    module is imported once per session — a snapshot taken at import would be
    evaluated before anything had a chance to set the variable, and every
    later change would be ignored. That would make the switch untestable in a
    single session, and an untestable switch is one whose two positions cannot
    be told apart in the one place both of them have to work.
    """
    monkeypatch.delenv(_STRICT_SWITCH_ENV, raising=False)
    assert require_real_tools_requested() is False, (
        "the switch read {!r} as a request for the real run with nothing set"
    ).format(_STRICT_SWITCH_ENV)

    monkeypatch.setenv(_STRICT_SWITCH_ENV, "1")
    assert require_real_tools_requested() is True, (
        "setting {} after this module was imported did not change the "
        "verdict, so the value was read once and kept".format(_STRICT_SWITCH_ENV)
    )

    monkeypatch.setenv(_STRICT_SWITCH_ENV, "true")
    assert require_real_tools_requested() is True, (
        "the second accepted spelling was read as off in the same session "
        "that read the first one as on, which is a snapshot with two values"
    )

    monkeypatch.setenv(_STRICT_SWITCH_ENV, "0")
    assert require_real_tools_requested() is False, (
        "the switch stayed on after it was set to an explicit off value, so "
        "the verdict is not a function of the variable"
    )

    monkeypatch.delenv(_STRICT_SWITCH_ENV)
    assert require_real_tools_requested() is False, (
        "unsetting {} did not return the file to its default position"
    ).format(_STRICT_SWITCH_ENV)


# ---------------------------------------------------------------------------
# The gate in both positions
# ---------------------------------------------------------------------------
#
# The switch above says what a value *means*. What the two positions do with it
# is the gate's business, and it is the part a lane actually depends on: in one
# position a machine that cannot start the tools is a skip, in the other it is a
# failure. Both claims are about how pytest *reports* an item, so both are
# checked the way pytest produces the report — a real run of a real module,
# read back out of the summary — rather than by inspecting a mark and trusting
# that pytest does with it what a mark is supposed to do.
#
# The positions are *driven* here rather than waited for. A machine whose
# endpoint-security policy refuses ``execve`` of the keychain tool is the
# machine the strict position exists for, and it is a rare machine: on a
# developer laptop that has never installed such a product, ``/usr/bin/security``
# and ``/bin/ps`` both start, the probe reports no refusals, and the strict
# position is never entered by a run at all. A test that waited for it would be
# a test that never runs on most machines — the same absence this file exists to
# object to, one level up. So each case below constructs the refusals it needs
# from the real probe and then asserts what pytest makes of them.
#
# What is *not* constructed is the answer. The refusal text is whatever
# ``_tool_is_runnable`` reports for a binary the machine really does refuse, so
# the path and the errno in the failure message are this machine's own reading
# and not a string written into this file.


def _refused_tool(tmp_path) -> tuple:
    """``(path, reason)`` for a binary this machine really will not start.

    A file with no execute bit: the probe refuses it for the same reason it
    refuses a policy-blocked system tool — the kernel declines to ``execve`` it
    — and it does so on every machine, which is what makes it usable as a
    stand-in for the *condition* rather than for the policy. The policy itself
    is not simulated and is not what is under test here; what is under test is
    what pytest reports once the probe has produced a refusal.

    The reason is the probe's own, so the errno quoted in a failure message
    below is this platform's text for the errno the kernel actually returned.
    """
    refused = tmp_path / "refused-by-policy"
    refused.write_text("#!/bin/sh\nexit 0\n")
    refused.chmod(0o600)
    runnable, reason = _tool_is_runnable(str(refused))
    assert not runnable, (
        "{} has no execute bit but the probe called it runnable, so there is "
        "no refusal to drive the gate with".format(refused)
    )
    return str(refused), reason


def _errno_text_of(path: str) -> str:
    """The errno text the kernel returns for ``path``, or ``""`` if it starts.

    Read by starting the binary and catching the refusal, which is the same
    question :func:`_tool_is_runnable` asks. Used instead of matching a
    sentence in the probe's own reason so the assertion is about the kernel's
    words rather than about this file's phrasing of them — a check that
    restated the format string it was checking would pass even if the errno had
    been dropped from the message.
    """
    try:
        subprocess.run(
            [path, "-h"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_SECURITY_TIMEOUT_SECONDS,
        )
    except OSError as exc:
        return exc.strerror or ""
    return ""


def _report_counts(output: str) -> dict:
    """The per-outcome counts pytest printed in its summary line.

    Read from the summary rather than from a parsed report object, because the
    claim under test is the one a CI job reads: what a human sees when a lane
    goes red, and whether a line says "skipped" or "failed".
    """
    counts = {}
    for kind in ("failed", "passed", "skipped", "error", "errors"):
        for match in re.findall(r"(\d+) {}".format(kind), output):
            counts[kind] = int(match)
    return counts


def _run_gated_module(tmp_path, *, unrunnable, strict, platform="darwin") -> str:
    """Run this file's six gated cases in one gate position, in a child pytest.

    A real child process rather than an in-process ``pytest.main``: re-entering
    pytest from inside a test leaves plugin state, collection caches and the
    ``-p no:cacheprovider`` this suite runs under installed for the rest of the
    session, and the thing being asked is a question about a *run*. The child is
    given its own rootdir and a file of its own, so the only thing it shares
    with this process is the module under test.

    ``platform`` is written into the child before the module is imported,
    because ``pytestmark`` reads ``sys.platform`` at import time. Naming it
    rather than inheriting it is what makes the platform case testable off
    macOS, and what keeps the strict case from depending on the host this suite
    happens to be running on.

    A refusal passed in here has to be one the gate cannot place, or every case
    stays gated — which is what the module's own fixtures require, since the
    child module has none of them. That is not a limitation of the strict
    position: a stand-in refusal is attributed to both gated tools precisely so
    it cannot narrow a case's requirement (see :func:`_refused_paths`), and a
    real refusal that named one tool would leave the other three cases ungated
    in a module with no ``deployment`` fixture to set them up with. To watch
    that narrowing happen, assert on the judgement itself — no run involved.

    The child is reaped by ``subprocess.run`` before this returns, so nothing
    it started outlives the case.
    """
    e2e_dir = Path(__file__).resolve().parent
    source = (
        "import sys\n"
        "import sysconfig, zoneinfo\n"
        "\n"
        "# Both of these initialise lazily, from the *build* platform's config\n"
        "# module, and a flipped ``sys.platform`` makes them look for a config\n"
        "# module that was never built for this interpreter — so the child would\n"
        "# die importing the module under test and report a collection error\n"
        # instead of the run being asked about. Forced here, while the platform\n"
        "# is still the real one, so the flip below is the only thing that is\n"
        # untrue about this process.\n"
        "sysconfig.get_config_vars()\n"
        "zoneinfo.ZoneInfo('UTC')\n"
        "\n"
        "sys.platform = {platform!r}\n"
        "sys.path[:0] = [{backend!r}, {e2e!r}]\n"
        "import pytest\n"
        "import test_keychain_full_delivery_macos as m\n"
        "\n"
        "# The platform mark the whole file carries, so the run below is gated\n"
        "# exactly as this file's own run is.\n"
        "pytestmark = m.pytestmark\n"
        "\n"
        "_gate = m._capability_gate({unrunnable!r}, {strict!r})\n"
        "for _name in m._GATED_CASE_TOOLS:\n"
        "    globals()[_name] = _gate(getattr(m, _name))\n"
    ).format(
        platform=platform,
        backend=str(_BACKEND_DIR),
        e2e=str(e2e_dir),
        unrunnable=list(unrunnable),
        strict=strict,
    )
    module = tmp_path / "test_gate_position.py"
    module.write_text(source)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(module),
            "-q",
            "--tb=line",
            "-rs",
            "-p",
            "no:cacheprovider",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=_CHILD_TIMEOUT_SECONDS,
        cwd=str(tmp_path),
    )
    return completed.stdout.decode("utf-8", "replace")


def test_strict_mode_fails_instead_of_skipping(tmp_path):
    """Strict + a machine that cannot start the tools: FAILED, six times over.

    The whole point of the second position. A lane that set the switch and
    landed on a machine the probe refused would otherwise report green with six
    items absent, and nothing in the job could tell that from a lane where the
    six had genuinely run.

    The counts are read out of a real run rather than out of a mark, because
    "reported as a skip" and "carries a mark whose condition is true" are the
    same statement until pytest has acted on it — and this file's entire
    complaint is about gates whose meaning is asserted rather than observed.
    """
    _path, reason = _refused_tool(tmp_path)

    output = _run_gated_module(
        tmp_path, unrunnable=[reason], strict=True, platform="darwin"
    )
    counts = _report_counts(output)

    assert counts.get("skipped", 0) == 0, (
        "the strict position skipped something. A switch that says 'the real "
        "run was required' and then skips is the failure this position exists "
        "to prevent, and it is the one a job cannot see.\n{}".format(output)
    )
    assert counts.get("failed", 0) == len(_GATED_CASE_TOOLS), (
        "expected all {} gated cases to be reported as failures under the "
        "strict position, got {}.\n{}".format(
            len(_GATED_CASE_TOOLS), counts, output
        )
    )
    assert counts.get("error", 0) == 0 and counts.get("errors", 0) == 0, (
        "the strict position has to fail at call time. A failure raised while "
        "the item is being collected or set up is reported as an error, which "
        "reads as a defect in the harness rather than as a machine that cannot "
        "answer the question — and this file has no harness defect to report, "
        "it has a machine that refused a binary.\n{}".format(output)
    )


def test_strict_failure_names_the_offending_tool(tmp_path):
    """The failure has to say which binary, and what the kernel said about it.

    A strict failure that reads "the real run was required and did not happen"
    is unactionable: the reader's next question is always *which tool*, and on
    the machines this position is for the answer is a policy rather than a bug
    — so the path and the errno are the only things that tell them whether to
    change a permission, remove an endpoint-security product, or move the lane
    to a machine that can serve it.

    Both are checked against what the probe reported rather than against a
    literal, so the errno text in the message is the text this platform gives
    for the errno the kernel actually returned, and the assertion would still
    hold on a machine whose refusal comes back as ``EACCES`` instead.
    """
    path, reason = _refused_tool(tmp_path)

    message = _strict_reason([reason])

    assert path in message, (
        "the strict failure does not name the tool that could not be started, "
        "so a reader cannot tell which policy to go and look at.\n"
        "message: {}".format(message)
    )
    assert os.path.basename(path) in message, (
        "the failure names neither the path nor anything recognisable from "
        "it.\nmessage: {}".format(message)
    )
    # The errno the probe actually read, quoted back verbatim. The text is
    # read off a real refusal rather than off the probe's sentence, so this
    # asserts that the kernel's own words survive into the message — and would
    # still hold on a machine whose refusal comes back as EPERM where this
    # one is EACCES.
    errno_text = _errno_text_of(path)
    assert errno_text, (
        "{} started on this machine, so there is no errno to quote. The probe "
        "and this check disagree about whether it can be started.".format(path)
    )
    assert errno_text in message, (
        "the failure does not quote the errno the kernel returned ({}), and on "
        "a machine whose refusal is a security policy the difference between "
        "'fix the permissions' and 'this machine will not let us' is exactly "
        "that word.\nmessage: {}".format(errno_text, message)
    )
    assert _STRICT_SWITCH_ENV in message, (
        "the failure does not say which switch asked for the real run, so a "
        "lane that set it cannot connect this failure to its own "
        "configuration.\nmessage: {}".format(message)
    )


def test_default_mode_still_skips_with_the_probe_verdict(tmp_path):
    """Switch off + a machine that cannot start the tools: a skip that says why.

    The default position is unchanged by any of this, and it is checked as
    carefully as the new one because a gate that fails in the strict position
    and quietly stops skipping in the default one would move the problem rather
    than fix it — a developer running the suite locally would get six red items
    about a policy on their own machine, and would learn to pass ``-k`` past the
    cases they most need to run.

    The reason is checked for the probe's own verdict rather than for a
    paraphrase of it, for the reason the module docstring gives: a skip that
    does not say what it skipped *for* is not actionable, and a skip that says
    something other than what the probe found is worse than no reason at all.
    """
    path, reason = _refused_tool(tmp_path)

    gate = _capability_gate([reason], False)

    mark = getattr(gate, "mark", None)
    assert mark is not None and mark.name == "skipif", (
        "the default position is no longer a skipif: {!r}. A machine that "
        "cannot start the tools has to stay a skip in this position — it is "
        "reporting a defect in code the case never reached.".format(gate)
    )
    assert mark.args == (True,), (
        "the default position's skipif condition is {!r} where True was "
        "required, so a machine that cannot start {} would not skip.".format(
            mark.args, path
        )
    )
    assert path in mark.kwargs["reason"], (
        "the skip reason does not name the tool that could not be started.\n"
        "reason: {}".format(mark.kwargs["reason"])
    )
    assert reason in mark.kwargs["reason"], (
        "the skip reason does not carry the probe's own verdict, so a reader "
        "cannot tell a policy refusal from a missing file.\n"
        "probe said: {!r}\nreason: {}".format(
            reason, mark.kwargs["reason"]
        )
    )

    # And the position this file is actually in. The gate built above is
    # deliberately built *with* a refusal, so what has to be checked separately
    # is the module-level one: on a machine whose probe found nothing there is
    # no refusal to report, and the six cases must therefore run rather than
    # skip. A gate stuck on silences this whole file, which is the quiet
    # failure this file was written to make impossible to miss.
    if not _UNRUNNABLE_TOOLS:
        live = getattr(requires_real_tools, "mark", None)
        assert live is not None and live.name == "skipif", (
            "the module-level gate is {!r} rather than a skipif. With no "
            "refusals probed it must gate nothing, and a gate of some other "
            "shape is a gate this file can no longer reason about.".format(
                requires_real_tools
            )
        )
        assert live.args == (False,), (
            "this machine's probe reported no refusals, yet the module-level "
            "gate's condition is {!r} where (False,) was required. It has to "
            "be False so the six cases run; a gate stuck on would skip this "
            "whole file and report green while publishing no evidence.".format(
                live.args
            )
        )
        assert not live.kwargs.get("reason"), (
            "this machine's probe reported no refusals, yet the module-level "
            "gate carries a reason ({!r}) to explain a skip it will never "
            "cause.".format(live.kwargs.get("reason"))
        )


def test_platform_skip_wins_over_strict_mode(tmp_path):
    """Off macOS the file is skipped, strict switch or not.

    The strict switch is about *tools*, and off macOS the tools are not the
    problem — the facility is. A runner with no keychain cannot be asked for a
    real keychain read, and a switch that overrode the platform gate would turn
    every Linux runner into a lane reporting six failures about a machine that
    was never supposed to answer the question. The two gates are kept apart so
    the switch stays a statement about the tools and nothing else.

    Asserted by reading a real run, because "the platform mark is on the module"
    and "the platform mark fired" are different claims, and only the second one
    is the one a CI job depends on.
    """
    _path, reason = _refused_tool(tmp_path)

    output = _run_gated_module(
        tmp_path, unrunnable=[reason], strict=True, platform="linux"
    )
    counts = _report_counts(output)

    assert counts.get("skipped", 0) == len(_GATED_CASE_TOOLS), (
        "off macOS the six gated cases must be skipped whatever the strict "
        "switch says — a runner with no keychain cannot be asked for a real "
        "keychain read, and turning its lane red would be reporting a defect "
        "in a machine that was never meant to answer. Got {}.\n{}".format(
            counts, output
        )
    )
    assert counts.get("failed", 0) == 0, (
        "the strict position overrode the platform gate. The switch is a "
        "statement about the tools, and the platform gate is about the "
        "facility; a switch that could overrule the second would let a Linux "
        "runner fail six cases about a keychain it has never had.\n{}".format(
            output
        )
    )


#: Kept as an alias for the case above, which is the older spelling of the same
#: question and is the name the surrounding cases use. Two names for one
#: assertion is the file's habit elsewhere (the two probes), and a reader
#: arriving at the strict section expects to find the platform case under the
#: name they saw in the boundary conditions.
test_platform_skip_beats_strict_mode = test_platform_skip_wins_over_strict_mode


def test_strict_switch_fails_closed_on_unknown_values(monkeypatch):
    """A value this function does not recognise leaves the lane where it was.

    The direction is chosen rather than incidental. Reading a value wrongly is
    only ever safe in one direction: an unrecognised one has to leave the file
    in its default position, because the alternative — a spelling nobody wrote
    down quietly meaning "on" — is a guess about a lane that is not this one's.
    So the parser accepts two spellings and refuses everything else, and the
    refusals below are the near misses a reader is most likely to try.

    ``True`` and ``TRUE`` are in that list on purpose. They read as true to a
    person, and they are refused here for the same reason ``Yes`` is: a switch
    that fires on a guess is a switch whose firing cannot be predicted from its
    documentation. The two accepted spellings are the whole contract.
    """
    unknown = (
        "",
        "True",
        "TRUE",
        "Yes",
        "ON",
        "tr ue",
        "maybe",
        "2",
        "trü",
        "１",
    )
    for raw in unknown:
        monkeypatch.setenv(_STRICT_SWITCH_ENV, raw)
        decided = require_real_tools_requested()
        assert decided is False, (
            "{!r} was read as a request for the real run. Only the two "
            "spellings in {} are accepted, and anything else has to leave the "
            "lane in the position it is in by default.".format(raw, _STRICT_SWITCH_ENV)
        )
