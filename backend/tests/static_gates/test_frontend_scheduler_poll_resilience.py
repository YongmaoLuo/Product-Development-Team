"""A failed poll must keep the panel it had, and say so once.

Why this gate exists
--------------------
The scheduler panel re-reads the read model every five seconds.  A read
that fails has two ways to go wrong, and both of them are visible to
the operator on a screen somebody is looking at:

1. **The panel is thrown away.**  The state read paints an error box
   over the panel.  Once the panel polls, a backend that is briefly
   unreachable does not show "temporarily unavailable" — it shows the
   error, and then five seconds later the error again, forever.  The
   operator is left watching a panel that flickers between a working
   panel and a broken one while the one state they needed — the last
   good one — is thrown away on every tick.
2. **The console is flooded.**  The same failure every five seconds is
   the same failure, and it deserves one line, not one line per tick.
   A log that repeats itself every five seconds is a log nobody reads,
   and it hides the one line that was new.

The rule
--------
* A **quiet** read — the poll's read — keeps the last good frame.  It
  paints no error over the panel; the panel simply stays as it was.
* The **same** failure is written to the console once.  The next
  failure of a different kind is written as well, because it is
  different.
* What makes two failures the same is the **read** *and* the message.
  Every read goes through the same ``api()`` wrapper, so a backend
  that is down makes them all fail with the very same message — and
  they are still separate failures, because separate reads are broken.
  A marker shared by every read cannot tell them apart: the second
  read's failure is swallowed by the first one's marker, and the
  console shows one line for two broken reads.
* A read that succeeds again clears **its own** marker, so the next
  failure of the same kind is written again — the failure is news once
  more.  It clears nobody else's marker: another read may still be
  failing with the same message, and that failure must not be written
  a second time on the next tick.
* A read that is **not** quiet behaves exactly as it always did.  The
  first load, the manual refresh and the read that follows an action
  all still show the error: those are the moments where a question was
  put to the panel, and the answer has to say "the request failed".

The panel reads the read model alone
-------------------------------------
The 「可操作的计划」 block was a superset of the two blocks that
remain, and it is gone from the markup; the loader that filled it went
with it.  So the poll reads one model now — ``/scheduler/state`` — and
the rules above are pinned against that one read.  The *mechanism* is
still pinned the way it was: the memory is keyed by the read, so a
second read that fails with the same message is a second failure and
is written on its own line.  A memory that went back to being shared
by every read would swallow it again, and the gate says so.

Scan target
-----------
``frontend/app.js``, read as source.  These are shape gates like the
rest of ``static_gates/``: they run on a plain Python environment with
no JS toolchain, so they pin the contract in the source rather than
executing the page.

The failure path is looked at on its own
----------------------------------------
The loader asks the quiet question twice: once on the success path,
where it clears the failure marker, and once on the failure path,
where it decides whether the panel is allowed to throw the last good
frame away.  Only the second one says anything about a failed poll, so
the gate slices the ``catch`` block out of the loader body and looks
for the quiet check there.  A gate that searches the whole body finds
the success path's check first and compares it with a line that lives
in another block — the comparison then comes out true whatever the
failure path does, and the gate goes green on a panel that flickers.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# ``static_gates/`` is on sys.path for this directory's modules, so the
# body-slicing helpers of the sibling scheduler gates are imported
# instead of re-implemented: two slicers would be two judgements about
# the same file, and they would drift.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_scheduler_view_present import _function_body, _source  # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_JS = _REPO_ROOT / "frontend" / "app.js"


#: A ``catch`` clause, with or without a binding (``catch (err) {`` and
#: the bare ES2019 ``catch {``).  The lookahead keeps a word that merely
#: ends in "catch" out of the match.
_CATCH_CLAUSE = re.compile(r"\bcatch\b(?=\s*[({])")


def _catch_block(body: str, function_name: str) -> str:
    """Return the text of the single ``catch`` block inside a function body.

    The loaders have one failure path, and the quiet decision has to be
    made *there*.  A quiet check on the success path says nothing at all
    about what a failure does — which is why the whole failure path is
    sliced out of the body and looked at on its own.  Searching the
    body as a whole finds the success path's check first and then
    compares it with a line that lives in a different block, so the
    comparison comes out true whatever the failure path does.

    Raises :class:`AssertionError` when the body has no catch clause or
    more than one: a gate that cannot find the failure path cannot tell
    whether the failure path is quiet, and it has to say so rather than
    pass on a file it did not understand.
    """
    clauses = list(_CATCH_CLAUSE.finditer(body))
    assert len(clauses) == 1, (
        f"{function_name}() must have exactly one catch block; found "
        f"{len(clauses)}. The gate cannot tell the failure path from the "
        "success path, so it cannot tell whether a failed quiet read "
        "holds the last frame."
    )

    # Skip the clause's binding — ``catch (err)`` — to reach the block.
    index = clauses[0].end()
    while index < len(body) and body[index].isspace():
        index += 1
    if index < len(body) and body[index] == "(":
        close = body.find(")", index)
        assert close != -1, f"{function_name}() has an unterminated catch clause"
        index = close + 1

    start = body.find("{", index)
    assert start != -1, f"{function_name}() has a catch clause with no block"

    # Brace counting is naive — it does not know about strings or
    # comments — which is adequate for the short loader bodies this
    # file inspects, and matches the slicer next door.
    depth = 0
    for position in range(start, len(body)):
        char = body[position]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return body[start + 1 : position]
    raise AssertionError(f"unbalanced braces in the catch block of {function_name}()")


# ---------------------------------------------------------------------------
# The failure memory, and the key it is filed under
# ---------------------------------------------------------------------------

#: The failure the console was last told about, with **no read attached
#: to it**.  A shared marker cannot tell two reads apart: every read
#: goes through the same ``api()`` wrapper, so a backend that is down
#: makes them all fail with the very same message — and they are still
#: separate failures, because separate reads are broken.  The shared
#: marker swallowed the second read's failure, so one of the two broken
#: reads never reached the console at all.
#:
#: The pattern is anchored on word boundaries on purpose: the per-read
#: memory is called ``_schedulerPollErrors``, and that name ends in an
#: ``s``.  The negative lookahead stops the match there, so the gate
#: looks for the shared marker and not for the map that replaced it.
_SHARED_POLL_ERROR_MARKER = re.compile(r"(?<![\w$])_schedulerPollError(?![\w$])")

#: The per-read memory lookup — ``_schedulerPollErrors[<key>]`` against
#: the message.  The key is what makes the memory per read, so the gate
#: captures it and then asks where it comes from.
_PER_SOURCE_LOOKUP = re.compile(
    r"_schedulerPollErrors\[\s*([A-Za-z_$][\w$]*)\s*\]\s*===?\s*message\b"
)

#: The write into the per-read memory — ``_schedulerPollErrors[<key>] =
#: message``.
_PER_SOURCE_WRITE = re.compile(
    r"_schedulerPollErrors\[\s*([A-Za-z_$][\w$]*)\s*\]\s*=\s*message\b"
)

#: The forgetting of a per-read failure — ``delete
#: _schedulerPollErrors[<key>]``.
_PER_SOURCE_DELETE = re.compile(
    r"delete\s+_schedulerPollErrors\[\s*([A-Za-z_$][\w$]*)\s*\]"
)


def _braced_block(text: str, start: int) -> str:
    """Return the text of the ``{ ... }`` block that opens after *start*.

    The quiet branch of the catch block is the block that opens right
    after ``options.quiet``: it is the half of the failure path that
    decides the panel keeps its last frame.  The rest of the catch
    block is the ordinary path, which paints the error — so the two
    have to be looked at separately, or the ordinary path's error is
    found in the quiet branch and the gate cries wolf.

    Raises :class:`AssertionError` when no block opens after *start*,
    so a failure path that has been rewritten cannot be read as a
    quiet branch that happens to be empty.
    """
    brace = text.find("{", start)
    assert brace != -1, "no block opens after the marker"

    depth = 0
    for index in range(brace, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1 : index]
    raise AssertionError("unbalanced braces")


def _bound_to_source(body: str, name: str) -> bool:
    """Is ``name`` assigned from the ``source`` argument inside ``body``?

    The memory has to be filed under the read the failure belongs to.
    A key that does not come from ``source`` is a key that cannot tell
    two reads apart, and the gate says so rather than letting the
    lookup shape pass for the semantics.
    """
    pattern = re.compile(
        r"\b(?:const|let|var)\s+" + re.escape(name) + r"\s*=[^;]*\bsource\b"
    )
    return pattern.search(body) is not None


def _code_only(source: str) -> str:
    """Return the source with its comments taken out.

    The question the shared-marker gate asks is whether the *code*
    still keeps a marker that every read shares.  A name that only
    survives in a comment is a name somebody wrote down while
    explaining what the code used to do, and failing the gate on that
    would make the pin cry wolf.
    """
    without_blocks = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"(?m)^[ \t]*//[^\n]*", " ", without_blocks)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_poll_reads_quietly() -> None:
    """The poll must read quietly; the other callers must not.

    The poll is the only reader that runs on a timer, so it is the only
    one whose failure the operator did not ask about.  Every other
    caller — the first load, the manual refresh button, the read that
    follows an action — is the operator asking a question, and a
    failure there must still be shown.

    So the quiet option belongs to the poll alone: if the manual
    refresh went quiet, the operator who pressed the button would be
    told nothing at all.
    """
    source = _source(_APP_JS)
    body = _function_body(source, "refreshScheduler")

    assert "loadSchedulerState({ quiet: true })" in body, (
        "refreshScheduler() does not read the read model quietly "
        "(loadSchedulerState({ quiet: true })). The poll runs every "
        "five seconds, so a failure it paints is painted again and "
        "again — the panel flickers instead of holding its last frame."
    )
    assert source.count("loadSchedulerState({ quiet: true })") == 1, (
        "the quiet option is used by more than one caller of "
        "loadSchedulerState(). A quiet read keeps the last frame, so a "
        "caller that was meant to answer the operator would answer "
        "them with silence."
    )

    # The poll reads the read model and nothing else.  A second read
    # would be a second thing that can go wrong on every tick, and the
    # 「可操作的计划」 loader is gone from the panel: a poll that still
    # called it would find a table that is not there and throw on the
    # next line, every five seconds.
    assert "loadSchedulerPlans" not in body, (
        "refreshScheduler() reads the deleted plans block. The block is "
        "gone from the markup, so the read finds nothing and the panel "
        "goes blank around it — every five seconds."
    )


def test_a_failed_poll_keeps_the_last_frame() -> None:
    """A quiet failure must not paint the error over the panel.

    The loader must ask the quiet question *before* it reaches for the
    destructive rendering: ``loadSchedulerState()`` paints the error
    box.  A poll that paints it shows the operator an error every five
    seconds — and an error over a panel that is otherwise fine.

    The quiet check has to come first, so the failure path of a quiet
    read is taken before the failure path of an ordinary one.

    And it has to be asked *in the catch block*.  The loader also asks
    the quiet question on the success path — that is the line that
    clears the failure marker once a read succeeds again — and that
    question says nothing about what a failure does.  Searching the
    whole body finds the success path's check first, compares it with a
    line in a different block, and comes out true whatever the failure
    path does.  So the failure path is sliced out on its own, and the
    quiet check is looked for inside it.

    The other half of the rule is kept as it was: the ordinary read
    still has to paint the error.  The quiet branch is an addition to
    the failure path, not a replacement of it.
    """
    source = _source(_APP_JS)

    state_body = _function_body(source, "loadSchedulerState")

    state_catch = _catch_block(state_body, "loadSchedulerState")

    state_quiet = state_catch.find("options.quiet")
    assert state_quiet != -1, (
        "loadSchedulerState() never asks whether the read is quiet on the "
        "failure path. A failed poll would paint the error box over a "
        "panel that is otherwise fine, once every five seconds."
    )
    state_error = state_body.find("renderSchedulerError")
    assert state_error != -1, (
        "loadSchedulerState() never calls renderSchedulerError(). The "
        "read has to keep showing the failure to the caller who asked "
        "for it — the quiet branch is an addition, not a replacement."
    )
    state_error_in_catch = state_catch.find("renderSchedulerError")
    assert state_error_in_catch != -1, (
        "loadSchedulerState() does not paint the error inside its catch "
        "block. The failure path is the one that has to decide between "
        "holding the last frame and painting over it."
    )
    assert state_quiet < state_error_in_catch, (
        "loadSchedulerState() reaches renderSchedulerError() before it "
        "asks whether the read is quiet. A quiet failure would paint "
        "the error box anyway."
    )

    # The quiet branch itself must not paint over anything: the whole
    # point of the quiet read is that the panel keeps the frame it last
    # drew.  The ordinary path paints the error, and it is *after* the
    # quiet branch — so the branch is sliced out on its own.
    quiet_branch = _braced_block(state_catch, state_quiet)
    assert "renderSchedulerError" not in quiet_branch, (
        "loadSchedulerState() paints the error inside the quiet branch "
        "of its catch block. A quiet failure must leave the panel the "
        "way it was."
    )


def test_one_line_per_failure_class() -> None:
    """The same failure is written to the console once, not once per tick.

    Two failures are the same failure when they come from the **same
    read** and carry the **same message**.  The marker is what
    remembers the last failure of that read, so the check against it is
    what keeps a backend that is down from writing one line every five
    seconds.

    The read is part of the key, and that is the whole point of the
    lookup the gate looks for: the memory is filed under the read the
    failure belongs to (``_schedulerPollErrors[source]``), so two
    reads that fail with the same message each get their own line and
    each get their own memory.

    The marker is written **before** the console line.  Written after,
    the tick that follows would not find the marker and would write
    the very same failure again.

    A different message is a different kind of failure and is written
    as well — the marker is a memory of the last failure of this read,
    not a switch that silences the console forever.
    """
    body = _function_body(_source(_APP_JS), "schedulerLogPollError")

    lookup = _PER_SOURCE_LOOKUP.search(body)
    assert lookup is not None, (
        "schedulerLogPollError() does not compare the message with the "
        "failure remembered for a read (_schedulerPollErrors[...]). "
        "Without that comparison every tick writes the same line again, "
        "and a log that repeats itself every five seconds is a log "
        "nobody reads."
    )
    key = lookup.group(1)
    assert _bound_to_source(body, key), (
        f"schedulerLogPollError() looks the failure up under {key!r}, but "
        f"{key!r} does not come from the source. The key has to be the "
        "read the failure belongs to, or two reads that fail with the "
        "same message share one memory and one of them is never written."
    )

    write = _PER_SOURCE_WRITE.search(body)
    assert write is not None, (
        "schedulerLogPollError() does not remember the failure for the "
        "read it came from. The next tick of the same failure would be "
        "written again, once every five seconds, for as long as the "
        "backend stays down."
    )
    assert write.group(1) == key, (
        f"schedulerLogPollError() compares the message under {key!r} but "
        f"remembers it under {write.group(1)!r}. The memory has to be "
        "read and written under one and the same key."
    )

    warned = body.find("console.warn")
    assert warned != -1, (
        "schedulerLogPollError() never writes to the console. A quiet "
        "failure that is not written down anywhere leaves the operator "
        "with a panel that stops updating and no way to tell why."
    )
    assert write.start() < warned, (
        "schedulerLogPollError() writes to the console before it "
        "remembers the failure. The marker has to be written first, or "
        "the very next tick of the same failure is written again."
    )


def test_a_shared_marker_must_not_swallow_another_reads_failure() -> None:
    """Two reads that fail with the same message are two failures.

    Every read goes through the same ``api()`` wrapper, so a backend
    that is down makes them all fail with the very same message.  They
    are still separate failures: one broken read and another broken
    read are two broken reads.

    A marker shared by every read cannot tell them apart — the second
    read that fails with the same message is swallowed by the first
    one's marker, and the console shows one line for two broken reads.
    The operator then reads a console that names a failure the panel
    is not showing, and no line at all about the read that broke.

    So the shared marker has to be gone, and the memory has to be
    keyed by the read: ``_schedulerPollErrors[source]``.
    """
    source = _source(_APP_JS)

    assert _SHARED_POLL_ERROR_MARKER.search(_code_only(source)) is None, (
        "frontend/app.js still keeps a failure marker that is shared by "
        "every read (_schedulerPollError). Every read fails with the "
        "very same message when the backend is down, so the shared "
        "marker swallows the second failure and the "
        "console shows one line for two broken reads. The memory has to "
        "be keyed by the read — _schedulerPollErrors[source] — so that "
        "each read is written once, on its own."
    )


def test_a_successful_read_clears_only_its_own_marker() -> None:
    """A read that succeeds again forgets its own failure and no other.

    The marker is a memory of a failure, not a permanent mute.  Once
    the read succeeds, the failure is over: the next failure of the
    same kind has to be written again, or a backend that recovers and
    breaks a second time would fail silently for ever.

    And only that read's failure is over.  Another read may still be
    failing with the same message — the backend is down, and one of the
    two reads happens to answer anyway — and forgetting *its* marker as
    well would write that failure a second time on the very next tick.
    The same failure, written twice.
    """
    body = _function_body(_source(_APP_JS), "schedulerClearPollError")

    deletes = _PER_SOURCE_DELETE.findall(body)
    assert deletes, (
        "schedulerClearPollError() does not forget the failure of the "
        "read that succeeded. Once a read had failed, every later "
        "failure of the same kind would stay silent."
    )
    assert len(deletes) == 1, (
        "schedulerClearPollError() forgets "
        f"{len(deletes)} entries of the failure memory. A successful read "
        "must forget its own failure and no other: another read may "
        "still be failing with the same message, and forgetting its "
        "marker as well writes that failure a second time on the next "
        "tick."
    )
    key = deletes[0]
    assert _bound_to_source(body, key), (
        f"schedulerClearPollError() forgets {key!r}, but {key!r} does not "
        "come from the source. A successful read must forget the "
        "failure of the read that succeeded — not the failure of some "
        "other read."
    )


def test_a_successful_read_clears_the_marker() -> None:
    """A read that succeeds again makes the next failure news again.

    The marker is a memory of a failure, not a permanent mute.  Once the
    read succeeds, the failure is over: the next failure of the same
    kind has to be written again, or a backend that recovers and breaks
    a second time would fail silently for ever.
    """
    source = _source(_APP_JS)

    state_body = _function_body(source, "loadSchedulerState")

    assert "schedulerClearPollError(" in state_body, (
        "loadSchedulerState() never clears the failure marker. Once a "
        "read had failed, every later failure of the same kind would "
        "stay silent."
    )

    assert "function schedulerClearPollError" in source, (
        "frontend/app.js has no schedulerClearPollError(). The marker "
        "can then only be set, never cleared."
    )


def test_the_ordinary_reads_keep_their_old_behaviour() -> None:
    """The quiet option is optional, and the ordinary reads are untouched.

    The loaders are called without arguments from the places that
    existed before the poll: the first load, the manual refresh button
    and the read that follows an action.  Those callers must keep the
    behaviour they always had, so the option has to default to
    "not quiet" rather than to "quiet".

    The default is what makes the old call sites work at all: an
    option that had no default would throw a TypeError on every one of
    them.
    """
    source = _source(_APP_JS)

    state_body = _function_body(source, "loadSchedulerState")

    assert "loadSchedulerState(options = {})" in source, (
        "loadSchedulerState() does not default its options. The call "
        "sites that existed before the poll call it without arguments, "
        "and an option without a default would throw for every one of "
        "them."
    )

    assert "options.quiet" in state_body, (
        "the loader never reads options.quiet, so the quiet branch "
        "could not be taken."
    )