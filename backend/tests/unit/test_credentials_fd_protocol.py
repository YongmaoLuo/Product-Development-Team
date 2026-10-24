"""VP-019 (the credential fd protocol), clause by clause.

The criterion this file pins, quoted verbatim from the plan:

    仅当 fd 为 FIFO（fstat 判定）且非阻塞读在写端已关闭时读到 EOF，才返回 payload
    并在解析前 os.close(fd)；判定不成立时返回 None / 抛 ValueError，对 fd 既不 read
    也不 close，不改其 flags；注释写明 macOS 上 kqueue 描述符对 fstat 同样是 FIFO
    （必要而非充分），并指向 docs/development/service-spawn-root-cause.md；
    PDT_SECRET_FD_* 变量名未被删除；审查须给出 file:line 引用

Why a file of its own, when ``test_credentials_fd_payload.py`` already
covers the round trip. That file answers "does the transport work"; this
one answers "is the *protocol* still the one the criterion describes",
clause by clause, with the clause quoted in the docstring of the test
that pins it. The difference matters because the two failure modes look
alike from the outside: a test that exercises the behaviour passes
whenever the behaviour happens to hold, and says nothing about the
wording the criterion also asks for. A reviewer can therefore check one
clause against one test name against one symbol, without reading prose.

Three kinds of assertion live here, and the difference between them is
the point of the exercise:

* **Behaviour.** What the module does to a descriptor it does not own:
  not read, not closed, flags unchanged. Pinned for the three shapes
  the criterion names — a pipe with a live writer, a regular file, and
  a kqueue descriptor.
* **Structure.** The shape of the source, checked with ``ast``. A
  behaviour test cannot tell the difference between a drain that asks
  whether a read would block and one that puts the pipe into
  non-blocking mode to find out — for every descriptor whose ``fcntl``
  works, both answer the same way and leave the same flags behind.
  The difference is invisible from the outside, so it is pinned from
  the inside: the drain has to ask its readiness question, and it may
  not issue a call that touches the flags of the descriptor it was
  handed.
* **Wording.** A handful of load-bearing sentences — that a kqueue
  descriptor is a FIFO to ``fstat`` and that being a FIFO is
  necessary but not sufficient, that a failed ``F_SETFL`` still takes
  effect, and where the full account lives. Not a full-text
  comparison: prose that has nothing to do with the criterion is not
  the module's business, and a test that failed on a rewording would
  train people to stop reading the comment.

The negative control is in here too, and it is a real one: the shape
the module had *before* the flags fix is embedded below, and the tests
show that it really did change a descriptor it did not own. A clause
nobody has ever seen go red is a clause nobody can trust.

The acceptance command for the whole criterion is one pytest run, from
``backend/``::

    cd backend && .venv/bin/python -m pytest tests/unit/test_credentials_fd_protocol.py -v

The clause ↔ test ↔ symbol table lives in
``docs/development/service-spawn-root-cause.md``, and the last test
here checks that the table still matches this file — a table that has
drifted away from the tests is worse than no table, because it is
believed.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
import stat
import sys
import textwrap
import threading
from pathlib import Path
from typing import List, Optional

import pytest

import credentials

try:  # The helper this file reuses lives in the sibling module.
    from .test_credentials_fd_payload import _is_open
except ImportError:  # pragma: no cover - only when this file is run alone
    import importlib.util

    _payload_path = Path(__file__).with_name("test_credentials_fd_payload.py")
    _payload_spec = importlib.util.spec_from_file_location(
        "_fd_payload_module", _payload_path
    )
    _payload_module = importlib.util.module_from_spec(_payload_spec)
    _payload_spec.loader.exec_module(_payload_module)
    _is_open = _payload_module._is_open


#: The repository root, resolved from this file. The repository is
#: cloned at a different path on every machine, so no path is ever
#: written out literally.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The document the criterion points at, and the one that carries the
#: acceptance table.
ROOT_CAUSE_DOC = REPO_ROOT / "docs/development/service-spawn-root-cause.md"

#: The Chinese version of the same document.  The acceptance table is in
#: both, and the two have to agree: they are the same table, and a
#: translation that drifts from it is a table nobody can check.
ROOT_CAUSE_DOC_ZH = REPO_ROOT / "docs/development/service-spawn-root-cause.zh.md"

#: The one command that re-runs every clause of the criterion.
ACCEPTANCE_COMMAND = (
    "cd backend && .venv/bin/python -m pytest "
    "tests/unit/test_credentials_fd_protocol.py -v"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalized_source(func) -> str:
    """Return the source of ``func`` with its whitespace collapsed.

    Collapsing the whitespace is what lets a wording assertion say
    "this sentence is in the source" without also pinning where the
    line breaks are: a comment that is re-wrapped is not a comment
    that changed its meaning.
    """
    return " ".join(inspect.getsource(func).split())


def _is_select_call(call: ast.AST) -> bool:
    """Whether ``call`` is ``select.select(...)``.

    The readiness question the drain asks before every read. It is what
    takes the place of the non-blocking switch: a read that follows a
    "yes" cannot block, because there is something to take, and a read
    that follows a "no" is not made at all.
    """
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "select"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "select"
    )


def _is_flag_touching_call(call: ast.AST) -> bool:
    """Whether ``call`` is one of the calls that touch a descriptor's flags.

    Three shapes, and the rule is about what they *do* rather than how
    they are spelled: ``os.set_blocking(fd, ...)`` changes the flags,
    ``os.get_blocking(fd)`` reads them through the same ``F_GETFL`` that
    answers ENOTTY on a kqueue descriptor, and ``fcntl.fcntl`` is the
    raw syscall behind both. The ``from fcntl import fcntl`` spelling
    is caught as well, so the rule cannot be walked around by importing
    the function under a different name.
    """
    if not isinstance(call, ast.Call):
        return False
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in ("set_blocking", "get_blocking"):
            return isinstance(func.value, ast.Name) and func.value.id == "os"
        if func.attr == "fcntl":
            return isinstance(func.value, ast.Name) and func.value.id == "fcntl"
        return False
    if isinstance(func, ast.Name):
        return func.id in ("fcntl", "set_blocking", "get_blocking")
    return False


def _flag_guard_violations(source: str) -> List[str]:
    """Return the ways ``source`` breaks "the drain never touches the flags".

    The rule, in one sentence: the drain may not call anything that
    changes — or reads — the flags of the descriptor it was handed.

    Why the rule is what it is. The criterion says the descriptor's
    flags are not changed, and a shape that switches them off and back
    on cannot promise that: the kernel applies ``F_SETFL`` and only
    then reports ``ENOTTY``, so a descriptor whose ``fcntl`` fails is
    changed on the way in and only *maybe* put back. The way out is not
    a better ``finally`` — it is not making the call at all. So the
    guard is not "every switch has a restore": it is "there is no
    switch", plus the positive half — the drain has to ask a readiness
    question, because that is where the non-blocking guarantee comes
    from now.

    An empty list means the shape is right.
    """
    tree = ast.parse(textwrap.dedent(source))
    violations: List[str] = []

    for function in (
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    ):
        for call in ast.walk(function):
            if _is_flag_touching_call(call):
                violations.append(
                    f"line {call.lineno} of the function: "
                    f"{ast.unparse(call.func)}(...) touches the flags of a "
                    "descriptor this module does not own"
                )

    return violations


def _line_of_first_call(source: str, predicate) -> int:
    """Return the line of the first call in ``source`` that matches."""
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and predicate(node):
            return node.lineno
    raise AssertionError("no matching call in the source")


def _symbol_span(source: str, symbol: str):
    """Return the ``(first, last)`` line of a top-level def or class."""
    tree = ast.parse(textwrap.dedent(source))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return (node.lineno, node.end_lineno)
    return None


#: The ``_drain_published_pipe`` as it was before the flags fix, taken
#: from ``49f93be~1``. Embedded here so that the negative control does
#: not need git, a checkout, or the network: the shape that broke is
#: part of the test suite, not a thing the suite has to go and fetch.
_PRE_FIX_DRAIN_SOURCE = '''
def _drain_published_pipe(fd: int) -> Optional[bytes]:
    """Read the payload out of ``fd``, if ``fd`` is the pipe we published.

    Returns the raw bytes, or ``None`` when ``fd`` does not name a
    published pipe. In that case the descriptor is left exactly as it
    was found: not closed, its flags as they were, and whatever it
    names still owned by whoever owns it.

    Two things have to line up for a descriptor to be the pipe this
    module published:

    * it is a pipe at all (:func:`_is_pipe`), and
    * its writer is gone. The published pipe has its write end closed
      before the number is handed out, so a read of it ends at EOF. A
      pipe with a live writer belongs to somebody else, and reading it
      would wait for that somebody to write — in a server's startup,
      a hang rather than a failure.

    The read is therefore done out of blocking mode, and a read that
    finds the pipe empty with its writer still attached is the signal
    that this is somebody else's pipe.

    Why any of this matters. A descriptor *number* is only meaningful
    in the process that published it. The number travels through the
    environment; the pipe does not always travel with it. Where the
    pipe did not arrive, the number names whatever else the receiving
    process opened at that number — and in a child process that is
    usually the event loop's own selector, because the selector takes
    the lowest free descriptor. Closing that is what killed the service
    before it was ready, with ``OSError: [Errno 9] Bad file descriptor``
    out of ``asyncio/base_events.py``. The full account is in
    ``docs/development/service-spawn-root-cause.md``.
    """
    if not _is_pipe(fd):
        return None

    try:
        was_blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
    except OSError:
        # Not a descriptor this module can read. On macOS the event
        # loop's kqueue descriptor is a pipe as far as ``fstat`` is
        # concerned, and ``fcntl`` on it fails with ENOTTY — so this is
        # the path the production failure actually took.
        return None

    chunks: List[bytes] = []
    try:
        while True:
            try:
                chunk = os.read(fd, _FD_READ_CHUNK)
            except BlockingIOError:
                # The pipe is empty and its writer is still attached.
                # This is somebody else's pipe, not the published one.
                return None
            except OSError:
                # Not readable at all. Nothing was drained, so nothing
                # is spent and nothing is closed.
                return None
            if not chunk:
                # EOF: the writer is gone, which is the shape of a
                # published pipe.
                break
            chunks.append(chunk)
    finally:
        try:
            os.set_blocking(fd, was_blocking)
        except OSError:
            pass

    return b"".join(chunks)
'''


#: The ``_drain_published_pipe`` as it was until this change: the shape
#: that switched the pipe to non-blocking mode and restored the flags in
#: a ``finally`` that covered the switch as well as the read loop. It
#: kept its promise for every descriptor whose ``fcntl`` works, and it
#: still issued the calls the criterion forbids — which is why it is
#: here as a negative control and not as an acceptable shape.
_RESTORE_SHAPE_SOURCE = '''
def _drain_published_pipe(fd: int) -> Optional[bytes]:
    """A shape that switches the flags off and puts them back."""
    if not _is_pipe(fd):
        return None

    try:
        was_blocking = os.get_blocking(fd)
    except OSError:
        return None

    chunks: List[bytes] = []
    try:
        try:
            os.set_blocking(fd, False)
        except OSError:
            return None

        while True:
            try:
                chunk = os.read(fd, _FD_READ_CHUNK)
            except BlockingIOError:
                return None
            except OSError:
                return None
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        try:
            os.set_blocking(fd, was_blocking)
        except OSError:
            pass

    return b"".join(chunks)
'''


def _pre_fix_drain():
    """Build the pre-fix ``_drain_published_pipe`` as a callable.

    The source is compiled in a namespace that carries the module's own
    helpers, so the function behaves exactly as it did in the tree it
    came from — including the part where it corrupts a descriptor it
    does not own.
    """
    namespace = {
        "os": os,
        "stat": stat,
        "List": List,
        "Optional": Optional,
        "_is_pipe": credentials._is_pipe,
        "_FD_READ_CHUNK": credentials._FD_READ_CHUNK,
    }
    exec(  # nosec B102 - running the saved pre-fix snippet is the point
        compile(
            textwrap.dedent(_PRE_FIX_DRAIN_SOURCE),
            "<pre-fix backend/credentials.py>",
            "exec",
        ),
        namespace,
    )
    return namespace["_drain_published_pipe"]


def _kqueue_descriptor():
    """Return a kqueue descriptor, or skip the test.

    The caller has to keep the ``kqueue`` object alive: it owns the
    descriptor.
    """
    select = pytest.importorskip("select")
    if not hasattr(select, "kqueue"):
        pytest.skip("this platform has no kqueue descriptor")
    kqueue = select.kqueue()
    return kqueue, kqueue.fileno()


# ---------------------------------------------------------------------------
# The clauses
# ---------------------------------------------------------------------------


def test_clause_c1_fifo_is_decided_by_fstat():
    """VP-019：「仅当 fd 为 FIFO（fstat 判定）」。

    The kind of the descriptor is decided by ``os.fstat`` and
    ``stat.S_ISFIFO``, and by nothing else. There is no portable
    alternative: ``/proc`` does not exist on macOS and ``fcntl`` does
    not answer the question, so a guess based on the device would be a
    guess on the one platform where being wrong kills a service.

    A descriptor that is not open at all answers "no" rather than
    raising. The question this module asks is "can this be the pipe we
    published", and "no" is an answer.
    """
    read_fd, write_fd = os.pipe()
    try:
        assert credentials._is_pipe(read_fd) is True, "a pipe is not a FIFO to fstat"
    finally:
        os.close(read_fd)
        os.close(write_fd)

    path = REPO_ROOT / "backend" / "credentials.py"
    regular = os.open(str(path), os.O_RDONLY)
    try:
        assert credentials._is_pipe(regular) is False, "a regular file is a FIFO?"
    finally:
        os.close(regular)

    gone = os.open(os.devnull, os.O_RDONLY)
    os.close(gone)
    assert credentials._is_pipe(gone) is False, "a closed descriptor raised instead"

    source = _normalized_source(credentials._is_pipe)
    assert "os.fstat" in source, "the kind is not decided by fstat"
    assert "S_ISFIFO" in source, "the kind is not decided by S_ISFIFO"


def test_clause_c2_fifo_alone_is_not_sufficient():
    """VP-019：「仅当 fd 为 FIFO（fstat 判定）**且** 非阻塞读在写端已关闭时读到 EOF」。

    "Only if" — so a FIFO is a *necessary* condition and not a
    sufficient one. The pipe in this test is a genuine FIFO, and it is
    still not the pipe this module published, because its writer is
    still attached: the read finds the pipe empty, and an empty pipe
    with a live writer is somebody else's.

    The module says so in the same words, because the next reader of
    that code has to know that the FIFO test alone does not settle it.
    """
    read_fd, write_fd = os.pipe()
    try:
        assert credentials._is_pipe(read_fd) is True
        assert credentials._drain_published_pipe(read_fd) is None, (
            "a pipe with a live writer was taken for the published pipe"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    source = _normalized_source(credentials._is_pipe)
    assert "necessary condition" in source, "the comment does not say 'necessary'"
    assert "not a sufficient one" in source, "the comment does not say 'not sufficient'"


def test_clause_c3_payload_is_returned_only_after_eof():
    """VP-019：「非阻塞读在写端已关闭时读到 EOF，才返回 payload」。

    The payload comes back only when the read reaches EOF, which is
    the shape of a pipe whose writer is gone. And the read cannot
    block: a blocking read on a pipe whose writer is alive would wait
    for a writer that may never write, and in a server's startup that
    is a hang rather than a failure.

    The readiness question has to come *before* the read. That
    ordering is checked in the source, because the difference between
    "ask, then read" and "read, then ask" is not visible in the result
    — only in whether the reader can hang.
    """
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"the-payload")
    os.close(write_fd)
    try:
        assert credentials._drain_published_pipe(read_fd) == b"the-payload"
    finally:
        os.close(read_fd)

    source = inspect.getsource(credentials._drain_published_pipe)
    ask_line = _line_of_first_call(source, _is_select_call)
    read_line = _line_of_first_call(
        source,
        lambda call: isinstance(call.func, ast.Attribute)
        and call.func.attr == "read"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "os",
    )
    assert ask_line < read_line, (
        f"the read (line {read_line}) happens before the readiness question "
        f"(line {ask_line})"
    )


def test_clause_c4_fd_is_closed_before_the_payload_is_parsed(monkeypatch):
    """VP-019：「才返回 payload 并**在解析前** os.close(fd)」。

    The descriptor is spent by the reader, and it is spent *before* the
    payload is parsed — not after. A payload the reader could not
    understand still has to leave the descriptor spent: the caller
    handed over a descriptor and expects it spent, and a descriptor
    left behind is one the caller has no way to find again.

    The check is behavioural: the parse is instrumented, and the
    descriptor is looked at from inside the parse. If the close came
    after, the descriptor would still be open at that moment.
    """
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps({"feishu_app_secret": "v"}).encode("utf-8"))
    os.close(write_fd)

    seen = {}
    real_loads = json.loads

    def _recording_loads(text, *args, **kwargs):
        seen["open_at_parse"] = _is_open(read_fd)
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(credentials.json, "loads", _recording_loads)
    payload = credentials.read_secret_fd(read_fd)

    assert payload == {"feishu_app_secret": "v"}
    assert seen.get("open_at_parse") is False, (
        "the descriptor was still open while the payload was being parsed"
    )
    assert _is_open(read_fd) is False, "the descriptor was not spent"

    # And a payload that cannot be parsed still leaves it spent.
    broken_fd, broken_write = os.pipe()
    os.write(broken_write, b"this is not json")
    os.close(broken_write)
    try:
        with pytest.raises(ValueError):
            credentials.read_secret_fd(broken_fd)
        assert _is_open(broken_fd) is False, (
            "a payload that failed to parse left the descriptor open"
        )
    finally:
        try:
            os.close(broken_fd)
        except OSError:
            pass


def test_clause_c5_unpublished_descriptor_raises_value_error():
    """VP-019：「判定不成立时返回 None / 抛 ValueError」。

    Both halves, at both levels: the internal drain answers ``None``,
    and the public reader raises ``ValueError``. The exception is a
    contract violation between the two halves of this module rather
    than a deployment state, so it is reported rather than smoothed
    over — returning ``{}`` for a payload the reader could not
    understand would turn a bug at the handoff into an authentication
    error at the far end of a notification send.
    """
    path = REPO_ROOT / "backend" / "credentials.py"
    fd = os.open(str(path), os.O_RDONLY)
    try:
        assert credentials._drain_published_pipe(fd) is None, (
            "the drain returned a payload for a descriptor that is not a pipe"
        )
        with pytest.raises(ValueError):
            credentials.read_secret_fd(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def test_clause_c6_unpublished_descriptor_is_not_read():
    """VP-019：「对 fd 既不 read 也不 close」。

    Not read is the half that is easiest to get wrong quietly. A read
    that consumes a byte changes the state of a descriptor the reader
    does not own, and the damage is invisible afterwards: the next
    reader of that descriptor sees a pipe that has quietly lost its
    first bytes.

    Two shapes, two proofs. A regular file: the FIFO test rules it out
    before anything is read, so the file offset must not have moved. A
    pipe with a live writer and nothing in it: the reader has to find
    out that the writer is still attached, and it does that with a
    non-blocking read that comes back empty — so nothing is consumed,
    and the owner of the pipe can still write to it and still read
    back what it wrote.

    The boundary of this clause, written down rather than left for the
    next reader to trip over. A foreign pipe that *already holds
    bytes* cannot be inspected without consuming them: "is the writer
    gone" is only answerable by reading, and what a read returns
    cannot be put back into a pipe. So the "not read" clause holds
    for a descriptor the FIFO test rules out, and for a pipe that is
    empty — the shapes the production failure took — while a foreign
    pipe that already carries data is read and then refused. That is
    a property of pipes, not a choice this module made, and it is
    recorded in ``docs/development/service-spawn-root-cause.md`` so
    that nobody reads the criterion as covering it.
    """
    path = REPO_ROOT / "backend" / "credentials.py"
    fd = os.open(str(path), os.O_RDONLY)
    try:
        with pytest.raises(ValueError):
            credentials.read_secret_fd(fd)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0, (
            "the reader consumed a descriptor it does not own"
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(ValueError):
            credentials.read_secret_fd(read_fd)
        # Nothing was taken: the owner of the pipe still has a pipe.
        os.write(write_fd, b"still-here")
        os.set_blocking(read_fd, True)
        assert os.read(read_fd, 64) == b"still-here", (
            "the reader drained a pipe it does not own"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_clause_c7_unpublished_descriptor_is_not_closed():
    """VP-019：「对 fd 既不 read 也不 close」。

    Not closed. A descriptor number is only meaningful in the process
    that issued it; where the pipe did not arrive with the number, the
    number names something this process owns — very often the event
    loop's own selector. Closing that is what took the server down
    before it was ready, with ``OSError: [Errno 9] Bad file
    descriptor`` out of ``asyncio/base_events.py``.
    """
    path = REPO_ROOT / "backend" / "credentials.py"
    fd = os.open(str(path), os.O_RDONLY)
    try:
        with pytest.raises(ValueError):
            credentials.read_secret_fd(fd)
        assert _is_open(fd) is True, "the reader closed a descriptor it did not publish"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def test_clause_c8_flags_of_a_live_writer_pipe_are_unchanged():
    """VP-019：「不改其 flags」 — a pipe with a live writer.

    The reader has to tell an empty pipe from one whose writer is
    gone, and it does that by asking whether a read would find
    anything — a question about the pipe rather than a change to it.
    So the flags come back exactly as they were: not because they were
    changed and put back, but because nothing ever changed them. A
    foreign descriptor left in non-blocking mode would change how
    whoever owns it behaves, because a read on it comes back empty
    instead of waiting.
    """
    fcntl = pytest.importorskip("fcntl")

    read_fd, write_fd = os.pipe()
    try:
        before = fcntl.fcntl(read_fd, fcntl.F_GETFL)
        with pytest.raises(ValueError):
            credentials.read_secret_fd(read_fd)
        after = fcntl.fcntl(read_fd, fcntl.F_GETFL)
        assert after == before, (
            f"the reader changed the flags of a pipe it does not own: {before} -> {after}"
        )
        assert os.get_blocking(read_fd) is True
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_clause_c9_flags_of_a_regular_file_are_unchanged():
    """VP-019：「不改其 flags」 — a regular file.

    The ordinary case: a descriptor that is not a pipe is ruled out
    before any flag is touched. Asserted anyway, because "ruled out
    early" is a claim about the code's shape and the code's shape is
    what this criterion is about.
    """
    fcntl = pytest.importorskip("fcntl")

    path = REPO_ROOT / "backend" / "credentials.py"
    fd = os.open(str(path), os.O_RDONLY)
    try:
        before = fcntl.fcntl(fd, fcntl.F_GETFL)
        with pytest.raises(ValueError):
            credentials.read_secret_fd(fd)
        after = fcntl.fcntl(fd, fcntl.F_GETFL)
        assert after == before, (
            f"the reader changed the flags of a file it does not own: {before} -> {after}"
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def test_clause_c10_flags_of_a_kqueue_descriptor_are_unchanged():
    """VP-019：「不改其 flags」 — a kqueue descriptor.

    The shape the production failure landed on. On macOS the event
    loop's own selector is a kqueue descriptor: the kernel gives it the
    same kind of object as a pipe, so ``fstat`` reports a FIFO and the
    "is it a pipe" test cannot rule it out; and ``fcntl`` on it fails
    with ENOTTY, so it cannot be put into non-blocking mode either.

    What matters is what that failed attempt leaves behind: the kernel
    applies ``F_SETFL`` and only then reports the error, so the
    descriptor comes back with ``O_NONBLOCK`` set — a change to
    somebody else's descriptor, made by a reader that had not
    established the descriptor was anybody's business.
    """
    fcntl = pytest.importorskip("fcntl")
    _kqueue, fd = _kqueue_descriptor()

    before = fcntl.fcntl(fd, fcntl.F_GETFL)
    was_blocking = os.get_blocking(fd)

    with pytest.raises(ValueError):
        credentials.read_secret_fd(fd)

    after = fcntl.fcntl(fd, fcntl.F_GETFL)
    assert after == before, (
        f"the reader changed the flags of a descriptor it does not own: {before} -> {after}"
    )
    assert os.get_blocking(fd) == was_blocking, (
        "the reader changed the blocking mode of a descriptor it does not own"
    )
    assert _is_open(fd) is True, "the reader closed a descriptor it does not own"


def test_clause_c11_the_pre_fix_shape_changed_those_flags():
    """VP-019：「不改其 flags」 — the clause has teeth.

    A clause nobody has ever seen go red is a clause nobody can trust.
    This test runs the shape the module had *before* the flags fix —
    embedded above, from ``49f93be~1`` — against the same kqueue
    descriptor, and shows that it really did leave the descriptor
    changed. The shape in the tree now does not.

    The point is not that the old code was wrong. It is that the
    difference between the two shapes is invisible from the outside:
    both return ``None`` for a kqueue descriptor, and both restore the
    flags of a pipe whose writer is alive. Only the flags of the
    kqueue descriptor tell them apart — which is why this test looks
    at the flags.
    """
    fcntl = pytest.importorskip("fcntl")

    pre_fix = _pre_fix_drain()

    _kqueue, fd = _kqueue_descriptor()
    before = fcntl.fcntl(fd, fcntl.F_GETFL)
    try:
        assert pre_fix(fd) is None, "the pre-fix shape was supposed to refuse the payload"
        after = fcntl.fcntl(fd, fcntl.F_GETFL)
        assert after != before, (
            "the pre-fix shape did not change the flags, so this test is "
            "no longer a negative control"
        )
        assert os.get_blocking(fd) is False, (
            "the pre-fix shape left the descriptor in non-blocking mode"
        )
    finally:
        # Put the descriptor back the way it was found, so the failure
        # this test documents does not leak into the rest of the suite.
        try:
            os.set_blocking(fd, True)
        except OSError:
            pass

    _kqueue2, fd2 = _kqueue_descriptor()
    before2 = fcntl.fcntl(fd2, fcntl.F_GETFL)
    assert credentials._drain_published_pipe(fd2) is None
    assert fcntl.fcntl(fd2, fcntl.F_GETFL) == before2, (
        "the shape in the tree changes the flags of a descriptor it does not own"
    )


def test_clause_c12_kqueue_is_a_fifo_to_fstat_and_the_comment_says_so():
    """VP-019：「注释写明 macOS 上 kqueue 描述符对 fstat 同样是 FIFO（必要而非充分）」。

    Both halves. The behaviour: on macOS the kernel gives a kqueue
    descriptor the same kind of object as a pipe, so ``fstat`` reports
    a FIFO and the "is it a pipe" test cannot rule it out. The
    comment: the code says so, in the words "necessary condition" and
    "not a sufficient one", so that the next reader knows the FIFO
    test is where the reasoning stops and not where it concludes.
    """
    _kqueue, fd = _kqueue_descriptor()

    if sys.platform == "darwin":
        assert credentials._is_pipe(fd) is True, (
            "a kqueue descriptor is not a FIFO to fstat on this platform"
        )

    source = _normalized_source(credentials._is_pipe)
    assert "kqueue" in source, "the comment does not mention kqueue"
    assert "fstat" in source, "the comment does not mention fstat"
    assert "necessary condition" in source, "the comment does not say 'necessary'"
    assert "not a sufficient one" in source, "the comment does not say 'not sufficient'"


def test_clause_c13_the_source_points_at_the_root_cause_document():
    """VP-019：「并指向 docs/development/service-spawn-root-cause.md」。

    The code points at the document that carries the full account: the
    root cause, the isolation, the measurement of the kernel behaviour
    that makes the restore necessary even though it raises. A pointer
    to a document that is not there is worse than no pointer, so the
    document is checked to exist as well.
    """
    source = _normalized_source(credentials._drain_published_pipe)
    assert "docs/development/service-spawn-root-cause.md" in source, (
        "the source does not point at the root cause document"
    )
    assert ROOT_CAUSE_DOC.is_file(), f"{ROOT_CAUSE_DOC} does not exist"


def test_clause_c14_a_failed_f_setfl_still_takes_effect_and_the_source_says_so():
    """VP-019：「不改其 flags」 — and the reason, in the comment.

    **Raising is not proof that nothing changed.** The kernel applies
    ``F_SETFL`` and only then reports ``ENOTTY``, so a failed
    ``os.set_blocking(fd, False)`` leaves the descriptor in
    non-blocking mode anyway. That fact is why the drain does not
    issue the call at all: the shape that used to live here had to
    change the flags and change them back, and on a descriptor whose
    ``fcntl`` fails only the first half of that works.

    The wording assertion is narrow on purpose: one sentence, the one
    that carries the fact. A comment that is re-wrapped or reworded
    around it does not fail this test; a comment that drops the fact
    does.
    """
    source = _normalized_source(credentials._drain_published_pipe)
    assert "A failed F_SETFL still takes effect" in source, (
        "the source no longer says that a failed F_SETFL still takes effect"
    )


def test_clause_c15_pdt_secret_fd_variable_names_are_not_removed():
    """VP-019：「PDT_SECRET_FD_* 变量名未被删除」。

    The names are the contract between the launcher and the service:
    the launcher publishes a pipe and puts its number in
    ``PDT_SECRET_FD_<NAME>``, and the service reads that variable.
    Renaming or dropping the prefix would break the handoff in a way
    that shows up as an unconfigured notifier rather than as an error.

    The two halves of the module agree on the prefix, and the detached
    wrapper still strips these variables from the environment of a
    service it starts — the name survives because the *variable* is
    what the wrapper removes, not because the convention is gone.
    """
    import service_manager

    assert credentials._FD_ENV_PREFIX == "PDT_SECRET_FD_", (
        "the fd variable prefix was renamed"
    )
    assert (
        credentials.secret_fd_env_var("feishu_app_secret")
        == "PDT_SECRET_FD_FEISHU_APP_SECRET"
    )
    assert service_manager._SECRET_FD_ENV_PREFIX == credentials._FD_ENV_PREFIX, (
        "the launcher and the reader disagree about the fd variable prefix"
    )
    assert "PDT_SECRET_FD_" in service_manager._DETACHED_WRAPPER_SOURCE, (
        "the detached wrapper no longer strips the fd variables"
    )
    assert hasattr(credentials, "publish_secret_fd"), (
        "the publisher is gone; the variable names have no producer"
    )
    assert hasattr(credentials, "read_secret_fd"), (
        "the reader is gone; the variable names have no consumer"
    )


def test_clause_c16_drain_never_touches_descriptor_flags():
    """VP-019：「不改其 flags」 — the shape of the source.

    ``_drain_published_pipe`` may not call anything that changes — or
    reads — the flags of the descriptor it was handed: no
    ``os.set_blocking``, no ``os.get_blocking``, no ``fcntl.fcntl``.
    And it has to ask a readiness question instead
    (``select.select``), because that is where the non-blocking
    guarantee comes from now.

    This is the assertion a behaviour test cannot make. Both shapes
    behave identically for every descriptor whose ``fcntl`` works; they
    differ only for one whose ``fcntl`` fails, and only in what they
    leave behind — and in the calls they issue.
    """
    source = inspect.getsource(credentials._drain_published_pipe)
    violations = _flag_guard_violations(source)
    assert violations == [], (
        "the drain touches the flags of a descriptor it does not own:\n"
        + "\n".join(violations)
    )
    assert any(
        _is_select_call(call)
        for call in ast.walk(ast.parse(textwrap.dedent(source)))
    ), "the drain asks no readiness question, so a read of it could block"


def test_clause_c17_the_guard_rejects_the_shape_it_exists_to_reject():
    """VP-019：「不改其 flags」 — the guard is not vacuous.

    Two shapes the guard in the previous test exists to reject are
    embedded above: the one from ``49f93be~1``, whose switch to
    non-blocking mode sits in a ``try`` with no ``finally`` next to the
    read-only snapshot, and the one that was in the tree until this
    change, which did restore the flags in a ``finally``. The second
    one is the interesting one: it kept its promise for every
    descriptor whose ``fcntl`` works, and it still issued the calls the
    criterion forbids. The guard has to say no to both.

    A guard that accepts everything would pass the previous test for
    the wrong reason, and a clause defended by such a guard is not
    defended at all.
    """
    for label, shape in (
        ("the pre-fix shape", _PRE_FIX_DRAIN_SOURCE),
        ("the shape that restored the flags", _RESTORE_SHAPE_SOURCE),
    ):
        violations = _flag_guard_violations(shape)
        assert violations, f"the guard accepted {label}"
        assert any(
            "os.set_blocking" in violation for violation in violations
        ), f"the guard did not name the call it rejected in {label}"

    # And it accepts the shape that is right, so the previous test is
    # not passing because the guard says yes to everything.
    good_shape = '''
def _drain_published_pipe(fd: int) -> Optional[bytes]:
    """A shape that asks before it reads."""
    if not _is_pipe(fd):
        return None
    chunks: List[bytes] = []
    while True:
        ready, _, _ = select.select([fd], [], [], 0)
        if not ready:
            return None
        chunk = os.read(fd, _FD_READ_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)
'''
    assert _flag_guard_violations(good_shape) == []


def test_clause_c19_drain_never_attempts_to_change_the_flags(monkeypatch):
    """VP-019：「不改其 flags」 — a tripwire, not a shape.

    The AST guard pins the source. This one pins the behaviour: every
    call that could touch a descriptor's flags is wired to record
    itself, and the drain still does its job over the shapes the
    criterion names — the pipe this module published, a pipe with a
    live writer, a regular file and a kqueue descriptor — without
    making a single one of those calls.

    A tripwire rather than an assertion about the flags afterwards,
    because "the flags came back the same" cannot tell the difference
    between a drain that never touched them and one that changed them
    and changed them back.
    """
    fcntl = pytest.importorskip("fcntl")
    calls: List[tuple] = []

    real_set_blocking = os.set_blocking
    real_get_blocking = os.get_blocking
    real_fcntl = fcntl.fcntl

    def _record_set_blocking(fd, blocking=True):
        calls.append(("os.set_blocking", fd, blocking))
        return real_set_blocking(fd, blocking)

    def _record_get_blocking(fd):
        calls.append(("os.get_blocking", fd))
        return real_get_blocking(fd)

    def _record_fcntl(fd, cmd, *args):
        calls.append(("fcntl.fcntl", fd, cmd))
        return real_fcntl(fd, cmd, *args)

    monkeypatch.setattr(os, "set_blocking", _record_set_blocking)
    monkeypatch.setattr(os, "get_blocking", _record_get_blocking)
    monkeypatch.setattr(fcntl, "fcntl", _record_fcntl)

    # The pipe this module published.
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"the-payload")
    os.close(write_fd)
    try:
        assert credentials._drain_published_pipe(read_fd) == b"the-payload"
    finally:
        os.close(read_fd)

    # A pipe with a live writer.
    read_fd, write_fd = os.pipe()
    try:
        assert credentials._drain_published_pipe(read_fd) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)

    # A regular file.
    path = REPO_ROOT / "backend" / "credentials.py"
    fd = os.open(str(path), os.O_RDONLY)
    try:
        assert credentials._drain_published_pipe(fd) is None
    finally:
        os.close(fd)

    # A kqueue descriptor — the shape the production failure took.
    _kqueue, kqueue_fd = _kqueue_descriptor()
    assert credentials._drain_published_pipe(kqueue_fd) is None

    assert calls == [], (
        "the drain touched the flags of a descriptor it does not own: "
        f"{calls}"
    )


def _drain_within(fd: int, seconds: float = 5.0):
    """Drain ``fd`` on a thread; return ``(finished, result)``.

    ``finished`` is ``False`` when the drain was still running after
    ``seconds`` — that is the watchdog talking, and it is the only way
    a blocking read can be told apart from a fast one.
    """
    outcome = {}

    def _run():
        try:
            outcome["result"] = credentials._drain_published_pipe(fd)
        except BaseException as exc:  # pragma: no cover - reported below
            outcome["error"] = exc

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return False, None
    if "error" in outcome:
        raise outcome["error"]
    return True, outcome.get("result")


def test_clause_c20_no_read_can_block():
    """VP-019：「非阻塞读」 — the read cannot wedge a server's startup.

    The guarantee is behavioural: every read is preceded by a readiness
    question, so a drain of a descriptor that will never produce
    anything returns instead of waiting. This is the watchdog for it —
    each shape is drained on a thread that is given a moment to finish,
    and a drain that is still running when the moment is over has
    blocked.

    The shapes are the ones that could block: a pipe whose writer is
    attached and will never write, a kqueue descriptor, and a pipe
    whose writer is attached and already holds data.
    """
    # A pipe whose writer is attached and will never write.
    read_fd, write_fd = os.pipe()
    try:
        finished, result = _drain_within(read_fd)
        assert finished, "the drain blocked on a pipe whose writer is attached"
        assert result is None, (
            "a pipe with a live writer was taken for the published pipe"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    # A kqueue descriptor — the shape the production failure took.
    _kqueue, kqueue_fd = _kqueue_descriptor()
    finished, result = _drain_within(kqueue_fd)
    assert finished, "the drain blocked on a kqueue descriptor"
    assert result is None, "a kqueue descriptor was taken for the published pipe"

    # A pipe whose writer is attached and already holds data. The bytes
    # are consumed — that is a property of pipes and is written down in
    # the root cause document — but the drain still returns instead of
    # waiting for a writer that will not write again.
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"somebody else's bytes")
    try:
        finished, result = _drain_within(read_fd)
        assert finished, "the drain blocked on a pipe that holds data"
        assert result is None, (
            "a pipe with a live writer was taken for the published pipe"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


#: A row of the acceptance table in the root cause document:
#: ``| 条款 | 测试 | 符号 | 源码 |``
_ACCEPTANCE_ROW = re.compile(
    r"^\|\s*(?P<clause>C\d+)\s*\|\s*`(?P<test>[A-Za-z0-9_]+)`\s*"
    r"\|\s*`(?P<symbol>[A-Za-z0-9_]+)`\s*"
    r"\|\s*`(?P<path>[A-Za-z0-9_./-]+):(?P<line>\d+)`\s*\|"
)


def _acceptance_rows(document: str):
    """Return the rows of the acceptance table in ``document``.

    The acceptance table appears in both language versions of the
    document, and the versions live in two files, so the rows are
    collected from both and the caller checks that the two copies
    agree.
    """
    return [match.groupdict() for match in (
        _ACCEPTANCE_ROW.match(line) for line in document.splitlines()
    ) if match is not None]


def test_clause_c18_the_acceptance_table_matches_this_module():
    """VP-019：「审查须给出 file:line 引用」。

    The criterion asks the review to give ``file:line`` references.
    This test is what makes that request checkable: the acceptance
    table in the root-cause document -- in both language versions, the
    English one and the Chinese one -- names, for every clause, the
    test that pins it, the symbol it is about, and the line in the file
    where that symbol lives.

    Three things are checked, so that a table cannot rot unnoticed:

    * every test in this module appears in the table, and every test
      in the table exists here — the mapping is a bijection, not a
      list that happens to overlap;
    * every cited symbol exists in the cited file, and the cited line
      lies inside it — a citation that points into the wrong function
      is wrong, and this is the cheapest way to catch it;
    * the acceptance command and the negative control are written
      down, so that the next review can re-run the criterion instead
      of reading about it.
    """
    document = ROOT_CAUSE_DOC.read_text(encoding="utf-8")
    document += ROOT_CAUSE_DOC_ZH.read_text(encoding="utf-8")
    rows = _acceptance_rows(document)
    assert rows, f"the acceptance table is missing from {ROOT_CAUSE_DOC}"

    by_clause = {}
    for row in rows:
        clause = row["clause"]
        if clause in by_clause:
            assert by_clause[clause] == row, (
                f"the two copies of the acceptance table disagree about {clause}"
            )
            continue
        by_clause[clause] = row

    module_tests = {name for name in globals() if name.startswith("test_clause_")}
    table_tests = {row["test"] for row in by_clause.values()}
    assert table_tests == module_tests, (
        "the acceptance table and this module do not name the same tests: "
        f"only in the table {sorted(table_tests - module_tests)}, "
        f"only in the module {sorted(module_tests - table_tests)}"
    )
    assert len(by_clause) == len(module_tests), (
        "two clauses point at the same test, so the mapping is not one to one"
    )

    for clause, row in sorted(by_clause.items()):
        cited = REPO_ROOT / row["path"]
        assert cited.is_file(), f"{clause} cites a file that does not exist: {cited}"
        span = _symbol_span(cited.read_text(encoding="utf-8"), row["symbol"])
        assert span is not None, (
            f"{clause} cites the symbol {row['symbol']}, which is not in {cited}"
        )
        line = int(row["line"])
        assert span[0] <= line <= span[1], (
            f"{clause} cites {cited}:{line}, which is outside "
            f"{row['symbol']} (lines {span[0]}-{span[1]})"
        )

    assert ACCEPTANCE_COMMAND in document, (
        "the acceptance command is not written down in the root cause document"
    )
    assert "49f93be~1" in document, (
        "the negative control is not written down in the root cause document"
    )
