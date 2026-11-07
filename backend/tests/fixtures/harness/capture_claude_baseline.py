"""Capture the claude argv baseline from the commit that built it.

Why a generator and not a hand-written fixture
-----------------------------------------------
The argv of a claude dispatch is not assembled by a function anybody can
call. It is built inline in
``coding_tool.ClaudeCodingTool._run_claude_interactive``, in the middle
of a long function that also resolves providers, writes a settings file
and runs the read loop. There is no seam to call, so there is no way to
ask the code "what argv would you build for this input?" -- except by
making it build one.

So this module drives the *real* function with a recording stand-in for
``subprocess.Popen``. Everything above the spawn -- the argv, the cwd,
the environment -- is the production code's; only the process is fake.
What comes out is therefore evidence about the code as it stands, not
about a re-implementation of it.

Where the evidence comes from
-----------------------------
The evidence has to come from the code that built the command line
*before* the argv moved into an adapter, or the "did the refactor
change anything?" question can only be answered by the new code -- which
is the code under suspicion. A capture that imports the working tree's
``coding_tool`` answers exactly that question with the wrong witness: the
adapter under test produces the baseline, the baseline matches the
adapter, and the gate is green on a change.

So the capture does not import the working tree's dispatch at all. It

1. materializes the baseline commit's tree (``git archive`` of
   :data:`BASELINE_COMMIT`) into a temporary directory,
2. verifies that the materialized ``backend/coding_tool.py`` is
   byte-for-byte the file that commit carries,
3. writes a small driver program into that tree and runs it *there*, so
   the ``coding_tool`` it drives is the baseline's own, and
4. checks, from the driver's own report, that the module it imported
   really came out of the materialized tree.

The driver is a program, not a library call: it runs in its own
process, so the baseline's modules cannot be confused with the working
tree's, and the capture's own process never imports either.

Writing the fixture
-------------------
The committed fixture may only be written from the commit it describes.
:func:`main` refuses to run at all when ``HEAD`` is not the baseline
commit, and the write path is only reachable in the explicit baseline
mode (``--write-baseline``), which re-checks the provenance of the
payload before anything is written. There is no path that lets the
current implementation vouch for itself: the capture has no way to
produce a payload from the working tree's dispatch, and the write path
refuses a payload that does not carry the baseline's own digest.

The procedure, therefore, is: check out :data:`BASELINE_COMMIT` (in a
worktree, if the working tree is busy), copy this file next to the
fixture, and run it there.

What is captured
----------------
Eight inputs: the session mode (a new session, ``--session-id``, or an
existing one, ``--resume``) x the MCP config (present or not) x the tool
whitelist (present or not). On top of that matrix the ``--model``,
``--settings`` and ``--append-system-prompt`` switches are turned on
and off across the eight, so the baseline pins the shape of those flags
too -- and pins them independently of the matrix, because a flag that
only ever appears in one corner of the matrix pins nothing about the
other seven.

Each case carries three things about the spawn:

``argv``
    The command line, in order.
``cwd``
    The directory the child runs in.
``env_delta``
    The environment the *command line* contributes -- the variables it
    adds to (or changes in) the inherited environment. This is the
    counterpart of ``HarnessInvocation.env_delta``, and the two are
    compared byte for byte.

What is *not* captured
----------------------
The value of ``--settings``. The dispatch writes that file into a
freshly created private directory whose name carries a random uuid, and
the file name carries another one, so the path is different on every
dispatch and its default root is a directory under the operator's home.
Pinning it would pin a random value and publish a home path. The
baseline pins what is actually stable about it: the flag is there, it is
last, and it carries exactly one value.

The run's own environment. The pre-refactor dispatch stamped two
per-run identifiers into the environment it handed the child --
``PDT_SUBAGENT_UUID`` and the log file that goes with it. They belong
to the *run*, not to the command line (the claude adapter says the same
thing about them), their values are random on every dispatch, and one of
them is an absolute path under the operator's temporary directory. They
are recorded separately, under ``run_env_delta``, with their values
replaced by placeholders -- so the shape of what the run adds is pinned
without publishing a random value or a path that is nobody else's
business.

The synthetic inputs
--------------------
The cwd and the MCP config the dispatch is given are fixed paths under
``/tmp/pdt-claude-baseline/`` -- a system temporary directory, not a
directory under anybody's home, and the same string on every machine.
They are real files, because a baseline taken from inputs that do not
exist would be a baseline of a fiction.

The tree is created, never removed: two captures may run at the same
time, and one that deleted the tree out from under the other would
break it mid-flight in a way that only shows up as a flaky failure
somewhere else. Everything written into it is identical on every run
and is written atomically, so a concurrent capture reads the same
bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, NoReturn, Optional, Sequence
from unittest.mock import patch

_HERE = Path(__file__).resolve()
#: ``backend/`` -- the directory ``coding_tool`` lives in.
_BACKEND_DIR = _HERE.parents[3]
#: The root of the checkout this capture belongs to. The git commands
#: below run against *this* repository, never against the temporary tree
#: the baseline is materialized into.
_REPO_ROOT = _HERE.parents[4]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

#: Where the synthetic inputs live. Fixed rather than derived from the
#: environment, because the fixture has to come out byte-identical on
#: every machine that regenerates it -- a per-run temporary directory
#: would put a different path in the baseline on every run.
SCRATCH_ROOT = Path("/tmp/pdt-claude-baseline")

#: The commit the baseline is anchored to. See :func:`_baseline_commit`.
BASELINE_COMMIT = "8046e97"

#: The session id handed to the dispatch. A fixed, obviously synthetic
#: UUID: the dispatch forwards it verbatim, and the baseline has to
#: name the same one every time.
SESSION_ID = "11111111-1111-1111-1111-111111111111"

#: What the ``--settings`` value is replaced with. See the module
#: docstring: the real path is random per dispatch and rooted under the
#: operator's home, so it cannot be published.
SETTINGS_PLACEHOLDER = "<private-dir>/subagent_settings_<uuid>.json"

#: The environment variables the *run* owns.
#:
#: The pre-refactor dispatch stamped a per-run uuid and the log file
#: that goes with it into the environment it handed the child. They
#: belong to the run rather than to the command line -- the claude
#: adapter's own contract says exactly that -- and their values are
#: random on every dispatch. They are recorded under ``run_env_delta``
#: with their values replaced, and they are not part of the command
#: line's ``env_delta``.
RUN_OWNED_ENV_KEYS = ("PDT_SUBAGENT_UUID", "PDT_SUBAGENT_LOG_FILE")

#: The shape each run-owned value has to have, and what it is recorded
#: as. A value that does not match its shape is a refusal, not a
#: placeholder: a placeholder that does not describe the value would be
#: a published claim about the baseline that nothing backs.
RUN_ENV_SHAPES: Dict[str, Any] = {
    "PDT_SUBAGENT_UUID": (
        re.compile(r"^[0-9a-f]{32}$"),
        "<per-run-uuid>",
    ),
    "PDT_SUBAGENT_LOG_FILE": (
        re.compile(r"^/tmp/subagent_[0-9a-f]{32}_activity\.log$"),
        "<per-run-tmp>/subagent_<per-run-uuid>_activity.log",
    ),
}

#: Shapes that must never reach the published fixture.
#:
#: The two home-path forms are the ones this repository's static gate
#: forbids outright; the rest are the credential and sandbox-profile
#: shapes that would carry an operator's machine into a file everybody
#: fetches.
PUBLISH_FORBIDDEN = (
    "/Users/",
    "/home/",
    "sk-",
    "sandbox-exec",
    "sandbox_profile",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
)

#: The eight inputs.
#:
#: ``mode`` is the session mode: ``create`` spawns a new session
#: (``--session-id <uuid>``), ``resume`` continues an existing one
#: (``--resume <uuid>``). ``model`` / ``settings`` / ``sys`` are the
#: orthogonal switches -- each is on for some inputs and off for others,
#: and each spans both session modes, both MCP states and both tool
#: states, so no switch is confounded with the matrix.
CASES: List[Dict[str, Any]] = [
    {
        "name": "create_mcp_tools_model",
        "mode": "create",
        "mcp": True,
        "tools": ["Bash"],
        "model": "m1",
        "settings": False,
        "sys": "S",
    },
    {
        "name": "create_mcp_settings",
        "mode": "create",
        "mcp": True,
        "tools": None,
        "model": None,
        "settings": True,
        "sys": None,
    },
    {
        "name": "create_tools_sys",
        "mode": "create",
        "mcp": False,
        "tools": ["Bash", "Read"],
        "model": None,
        "settings": False,
        "sys": "S",
    },
    {
        "name": "create_plain",
        "mode": "create",
        "mcp": False,
        "tools": None,
        "model": None,
        "settings": False,
        "sys": None,
    },
    {
        "name": "resume_mcp_tools_sys",
        "mode": "resume",
        "mcp": True,
        "tools": ["Bash"],
        "model": None,
        "settings": False,
        "sys": "S",
    },
    {
        "name": "resume_mcp_sys",
        "mode": "resume",
        "mcp": True,
        "tools": None,
        "model": None,
        "settings": False,
        "sys": "S",
    },
    {
        "name": "resume_tools_settings",
        "mode": "resume",
        "mcp": False,
        "tools": ["Bash", "Read"],
        "model": None,
        "settings": True,
        "sys": None,
    },
    {
        "name": "resume_model",
        "mode": "resume",
        "mcp": False,
        "tools": None,
        "model": "m1",
        "settings": False,
        "sys": None,
    },
]


class BaselineUnavailable(RuntimeError):
    """The baseline could not be taken.

    Raised when the baseline commit is not in this checkout, when the
    tree cannot be materialized, or when the materialized tree does not
    turn out to be the baseline's. Every one of those is a situation in
    which the capture cannot say anything true about the baseline, so it
    stops instead of guessing.
    """


# ---------------------------------------------------------------------------
# The driver: a program that runs inside the materialized tree
# ---------------------------------------------------------------------------

#: The program that drives the baseline dispatch.
#:
#: It is written into the materialized tree and executed there, so the
#: ``coding_tool`` it imports is the baseline commit's own -- the
#: working tree's is not on its path, and the capture checks the
#: module's origin against the materialized tree afterwards.
_DRIVER_SOURCE = '''\
"""Drive the pre-refactor claude dispatch and record what it spawned.

This program runs inside the materialized baseline tree. It imports the
baseline commit's own ``coding_tool`` and records the argv, the working
directory and the environment the dispatch gave the child. Only the
process is fake: everything above the spawn is the production code's.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch


class _Captured(Exception):
    """Raised by the fake stdout to break out of the dispatch read loop.

    The real read loop is ``for line in process.stdout:``. The fake
    raises out of ``__iter__``, so the loop never starts.
    """


class _FakeStdout:
    """The stdout pipe of the fake process: it yields nothing, loudly."""

    def __iter__(self):
        raise _Captured()

    def read(self, *_args, **_kwargs):
        return ""

    def close(self):
        return None


class _FakeStdin:
    """The stdin pipe of the fake process: it swallows the prompt."""

    def write(self, data):
        return len(data or "")

    def flush(self):
        return None

    def close(self):
        return None


class _FakeProcess:
    """A stand-in for the spawned ``claude`` process.

    ``pid`` is ``0`` on purpose: ``utils.process.kill_process_group``
    refuses any pid that is not a positive int, so this fake can never
    be mistaken for a real process group.
    """

    def __init__(self):
        self.pid = 0
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout()
        self.stderr = _FakeStdout()
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        return None

    def kill(self):
        return None


def _environment_delta(env, inherited):
    """The difference between the spawn environment and the inherited one.

    The runner merges a delta on top of its own copy of the inherited
    environment, so the delta is exactly this difference: the keys the
    dispatch added, and the keys whose value it changed.
    """
    delta = {}
    for key, value in env.items():
        if inherited.get(key) != value:
            delta[key] = value
    for key in inherited:
        if key not in env:
            delta[key] = ""
    return delta


def main():
    spec_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])
    scratch = Path(sys.argv[3])
    private_root = Path(sys.argv[4])

    import coding_tool
    from coding_tool import ClaudeCodingTool

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    specs = spec["cases"]
    session_id = spec["session_id"]

    spawned = []

    def _popen(args, **kwargs):
        env = dict(kwargs.get("env") or {})
        spawned.append(
            {
                "argv": [str(item) for item in args],
                "cwd": kwargs.get("cwd"),
                "env_delta": _environment_delta(env, dict(os.environ)),
            }
        )
        return _FakeProcess()

    env = {
        # Inherit mode: no provider walk, so the argv cannot pick up a
        # second ``--model`` from the provider's own model resolution.
        "PDT_PROVIDER_MODE": "inherit",
        # The private root the settings file is written into. Without
        # it that is a directory under the operator's home; with it the
        # capture leaves nothing behind outside the scratch tree.
        "PDT_SECRET_TEMP_ROOT": str(private_root),
    }

    captured = []
    with patch("subprocess.Popen", _popen), patch.dict(os.environ, env), patch(
        "claude_sandbox.resolve_sandbox_profile_file", lambda: None
    ):
        # An operator-configured provider chain is not part of the argv
        # being baselined, and it would make the capture depend on
        # whatever this machine happens to have configured.
        os.environ.pop("PDT_PROVIDER_PRIORITY", None)
        for case in specs:
            workdir = scratch / "workdir"
            tool = ClaudeCodingTool(
                model=case["model"],
                cwd=str(workdir),
                mcp_config=str(workdir / "mcp.json") if case["mcp"] else None,
                settings=(scratch / "settings.json") if case["settings"] else None,
            )
            try:
                tool._run_claude_interactive(
                    prompt="baseline probe",
                    system_instruction=case["sys"],
                    session_id=session_id,
                    resume=case["mode"] == "resume",
                    allowed_tools=case["tools"],
                )
            except _Captured:
                pass
            if not spawned:
                raise RuntimeError(
                    "the case %r never reached the spawn; the baseline "
                    "would be empty" % case["name"]
                )
            record = spawned.pop(0)
            captured.append(
                {
                    "name": case["name"],
                    "argv": record["argv"],
                    "cwd": record["cwd"],
                    "env_delta": record["env_delta"],
                }
            )

    out_path.write_text(
        json.dumps(
            {
                "coding_tool_path": os.path.abspath(coding_tool.__file__),
                "cases": captured,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# Git: the baseline commit and the tree it carries
# ---------------------------------------------------------------------------


def _git(*args: str, binary: bool = False) -> Any:
    """Run a git command against this checkout and return its output.

    Args:
        args: The git arguments, without the leading ``git``.
        binary: Whether to return stdout as bytes instead of text.

    Returns:
        The command's stdout, stripped of its trailing newline.

    Raises:
        BaselineUnavailable: The command could not be run, or it failed.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), *args],
            capture_output=True,
            text=not binary,
        )
    except OSError as exc:
        raise BaselineUnavailable(f"git could not be run: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise BaselineUnavailable(
            f"git {' '.join(args)} failed: {str(stderr).strip()}"
        )
    if binary:
        return result.stdout
    return result.stdout.strip()


def head_commit() -> str:
    """The commit this checkout is at.

    Returns:
        The full SHA of ``HEAD``.
    """
    return str(_git("rev-parse", "HEAD"))


def baseline_sha() -> str:
    """The full SHA of the baseline commit.

    Returns:
        The full SHA of :data:`BASELINE_COMMIT`.

    Raises:
        BaselineUnavailable: The commit is not in this checkout. A
            shallow clone does not have it, and a baseline cannot be
            taken from a tree that is not there.
    """
    return str(_git("rev-parse", f"{BASELINE_COMMIT}^{{commit}}"))


def materialize_baseline_tree(destination: Path) -> Path:
    """Materialize the baseline commit's tree at *destination*.

    The tree comes out of ``git archive``: the commit's own files, with
    nothing of the working tree mixed in. The archive is written next to
    the destination and removed again -- it is a means, not an artifact.

    Args:
        destination: The directory the tree is unpacked into. It is
            created if it does not exist.

    Returns:
        The directory the tree was unpacked into.

    Raises:
        BaselineUnavailable: The commit is not in this checkout, or the
            archive could not be written or unpacked.
    """
    archive = destination.parent / f"{destination.name}.tar"
    try:
        _git("archive", "--format=tar", f"--output={archive}", BASELINE_COMMIT)
    except BaselineUnavailable:
        archive.unlink(missing_ok=True)
        raise
    try:
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:*") as tar:
            if hasattr(tarfile, "data_filter"):
                tar.extractall(destination, filter="data")
            else:  # pragma: no cover - Python without the extraction filter
                tar.extractall(destination)
    except (OSError, tarfile.TarError) as exc:
        raise BaselineUnavailable(
            f"the baseline tree could not be unpacked: {exc}"
        ) from exc
    finally:
        archive.unlink(missing_ok=True)
    return destination


def verify_materialized_tree(tree: Path) -> str:
    """Check that the materialized tree really is the baseline's.

    The tree is unpacked from ``git archive``, but an archive that came
    out of the wrong commit would be a baseline of the wrong code, and
    the whole point of the exercise is that the code is the baseline's.
    So the file the evidence is about is compared against the commit's
    own copy, byte for byte.

    Args:
        tree: The materialized tree.

    Returns:
        The sha256 of the baseline's ``backend/coding_tool.py``.

    Raises:
        BaselineUnavailable: The materialized file is not the baseline's.
    """
    expected = _git("show", f"{BASELINE_COMMIT}:backend/coding_tool.py", binary=True)
    try:
        actual = (tree / "backend" / "coding_tool.py").read_bytes()
    except OSError as exc:
        raise BaselineUnavailable(
            f"the materialized tree carries no coding_tool.py: {exc}"
        ) from exc
    if actual != expected:
        raise BaselineUnavailable(
            "the materialized tree does not carry the baseline's "
            "backend/coding_tool.py; the evidence would be about some "
            "other code"
        )
    return hashlib.sha256(expected).hexdigest()


# ---------------------------------------------------------------------------
# The synthetic inputs
# ---------------------------------------------------------------------------


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON without ever exposing a half-written file.

    The temporary name carries the pid, so two captures writing the
    same path at the same time cannot scribble over each other's
    partial file.
    """
    partial = path.with_name(f"{path.name}.{os.getpid()}.partial")
    partial.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(partial, path)


def _prepare_inputs(root: Path) -> None:
    """Create the synthetic input tree the recorded paths point at.

    Nothing here removes anything, and every write is atomic. Two
    captures may run at the same time -- the unit suite runs one, and a
    regeneration may run beside it -- and a capture that deleted the
    tree out from under the other would break it mid-flight, in a way
    that only shows up as a flaky failure somewhere else.

    The content is identical on every run, so a concurrent capture
    reads the same bytes whichever writer got there first.
    """
    (root / "workdir").mkdir(parents=True, exist_ok=True)
    _write_json_atomic(root / "workdir" / "mcp.json", {"mcpServers": {}})
    _write_json_atomic(root / "settings.json", {"env": {}, "hooks": {}})


def _private_root(root: Path) -> Path:
    """A private root for this capture's settings file.

    Per process, so two captures never share a directory. The settings
    file lands in a fresh leaf under it, and that leaf's name is part
    of the path the baseline redacts.
    """
    return Path(tempfile.mkdtemp(prefix="private-", dir=str(root)))


def _normalize(argv: List[str]) -> List[str]:
    """Replace the one volatile argv element with a stable placeholder."""
    normalized: List[str] = []
    skip_next = False
    for item in argv:
        if skip_next:
            skip_next = False
            continue
        if item == "--settings":
            normalized.append(item)
            normalized.append(SETTINGS_PLACEHOLDER)
            skip_next = True
            continue
        normalized.append(item)
    return normalized


def _run_env_placeholder(key: str, value: str) -> str:
    """The publishable form of one run-owned environment value.

    Args:
        key: The variable's name.
        value: The value the baseline dispatch gave it.

    Returns:
        The placeholder that stands for the value.

    Raises:
        BaselineUnavailable: The value does not have the shape the
            placeholder claims. A placeholder that does not describe
            the value would be a published claim nothing backs.
    """
    shape = RUN_ENV_SHAPES.get(key)
    if shape is None:
        raise BaselineUnavailable(
            f"the run owns {key!r}, which this capture does not know how "
            "to record"
        )
    pattern, placeholder = shape
    if not pattern.match(value):
        raise BaselineUnavailable(
            f"the value of {key!r} does not have the shape the baseline "
            f"is known to give it ({value!r}); recording a placeholder "
            "for it would publish a claim nothing backs"
        )
    return placeholder


def _split_env_delta(measured: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """Split the measured spawn environment into the two halves.

    The measured delta is what the dispatch gave the child on top of
    the inherited environment. The variables the *run* owns (the
    per-run uuid and its log file) are recorded separately, with their
    random values replaced; everything else is the command line's own
    contribution and is recorded as it was measured.

    Args:
        measured: The difference between the spawn environment and the
            inherited one.

    Returns:
        ``{"env_delta": ..., "run_env_delta": ...}``.
    """
    env_delta: Dict[str, str] = {}
    run_env_delta: Dict[str, str] = {}
    for key, value in measured.items():
        if key in RUN_OWNED_ENV_KEYS:
            run_env_delta[key] = _run_env_placeholder(key, value)
        else:
            env_delta[key] = value
    return {"env_delta": env_delta, "run_env_delta": run_env_delta}


# ---------------------------------------------------------------------------
# The capture
# ---------------------------------------------------------------------------


def _run_driver(tree: Path, scratch: Path, private_root: Path) -> Dict[str, Any]:
    """Run the driver inside the materialized tree and read its report.

    The driver runs in its own process, with the materialized tree's
    ``backend`` directory as its working directory and its only entry on
    the module path, so the ``coding_tool`` it imports is the
    baseline's own and not the working tree's.

    Args:
        tree: The materialized baseline tree.
        scratch: The scratch root holding the synthetic inputs.
        private_root: The private directory the settings file goes into.

    Returns:
        The driver's report: the module it imported and the cases it
        recorded.

    Raises:
        BaselineUnavailable: The driver failed, or it imported a module
            that did not come out of the materialized tree.
    """
    backend = tree / "backend"
    driver_path = backend / "_pdt_baseline_capture_driver.py"
    spec_path = backend / "_pdt_baseline_capture_spec.json"
    out_path = backend / "_pdt_baseline_capture_result.json"

    driver_path.write_text(_DRIVER_SOURCE, encoding="utf-8")
    spec_path.write_text(
        json.dumps({"session_id": SESSION_ID, "cases": CASES}, indent=2),
        encoding="utf-8",
    )

    env = dict(os.environ)
    # The module path is replaced, not appended to: the working tree's
    # ``backend`` must not be reachable from the driver at all.
    env["PYTHONPATH"] = str(backend)
    env["PYTHONNOUSERSITE"] = "1"

    try:
        result = subprocess.run(
            [
                sys.executable,
                str(driver_path),
                str(spec_path),
                str(out_path),
                str(scratch),
                str(private_root),
            ],
            cwd=str(backend),
            env=env,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise BaselineUnavailable(f"the capture could not be started: {exc}") from exc

    if result.returncode != 0:
        raise BaselineUnavailable(
            "the baseline capture failed inside the materialized tree:\n"
            f"{result.stdout}\n{result.stderr}"
        )

    try:
        report = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BaselineUnavailable(
            f"the baseline capture wrote no usable report: {exc}"
        ) from exc

    origin = Path(str(report.get("coding_tool_path", ""))).resolve()
    if not origin.is_relative_to(backend.resolve()):
        raise BaselineUnavailable(
            f"the capture ran against {origin}, which is not the "
            "materialized baseline tree; the evidence would be about "
            "some other code"
        )
    return report


def _guard_cli_spawns(real_popen: Any) -> Any:
    """A ``Popen`` that refuses to start the claude CLI in this process.

    The evidence is produced by the driver, in the materialized tree.
    Nothing in *this* process may start a claude CLI: a spawn here
    would be the working tree's own dispatch running, and the recording
    would be about the code under suspicion.

    Args:
        real_popen: The real ``subprocess.Popen``.

    Returns:
        A replacement that refuses a claude CLI and delegates everything
        else to the real one.
    """

    def _popen(args: Any, **kwargs: Any) -> Any:
        argv = [str(item) for item in args]
        if argv and Path(argv[0]).name == "claude":
            raise RuntimeError(
                "the capture must not start a claude CLI in this process: "
                "the evidence comes from the baseline tree, in the driver"
            )
        return real_popen(args, **kwargs)

    return _popen


def _provenance(digest: str) -> Dict[str, Any]:
    """How the evidence was taken, recorded next to it.

    Args:
        digest: The sha256 of the baseline's ``coding_tool.py``.

    Returns:
        The provenance block of the payload.
    """
    return {
        "commit_sha": baseline_sha(),
        "source": "backend/coding_tool.py at the baseline commit",
        "method": (
            "the baseline commit's tree is materialized with `git archive` "
            "into a temporary directory and driven there; the capture "
            "never imports the working tree's coding_tool"
        ),
        "coding_tool_sha256": digest,
        "env_delta_meaning": (
            "env_delta is the environment the command line itself "
            "contributes; run_env_delta is the run's own per-run "
            "identifiers, recorded with their values replaced"
        ),
    }


def capture_cases() -> Dict[str, Any]:
    """Capture the baseline from the baseline commit's own code.

    Returns:
        ``{"commit": ..., "provenance": ..., "cases": [...]}`` -- the
        shape ``claude_argv_golden.json`` is written in. Every case is a
        name, the argv, the working directory, the command line's
        environment delta and the run's own environment delta.
    """
    root = SCRATCH_ROOT
    _prepare_inputs(root)
    private_root = _private_root(root)
    try:
        with tempfile.TemporaryDirectory(prefix="pdt-claude-baseline-tree-") as tmp:
            tree = materialize_baseline_tree(Path(tmp) / "tree")
            digest = verify_materialized_tree(tree)
            with patch("subprocess.Popen", _guard_cli_spawns(subprocess.Popen)):
                report = _run_driver(tree, root, private_root)
    finally:
        # Only this capture's own private root goes away. The synthetic
        # input tree stays: its paths are what the baseline records, so
        # a second capture must find them exactly as this one left them.
        shutil.rmtree(private_root, ignore_errors=True)

    cases: List[Dict[str, Any]] = []
    for record in report["cases"]:
        split = _split_env_delta(dict(record.get("env_delta") or {}))
        cases.append(
            {
                "name": record["name"],
                "argv": _normalize([str(item) for item in record["argv"]]),
                "cwd": record["cwd"],
                "env_delta": split["env_delta"],
                "run_env_delta": split["run_env_delta"],
            }
        )
    # Checked here, not at the gate: a leaky capture must stop at the
    # capture, before anything has been written for it to be picked up.
    return assert_publishable(
        {
            "commit": _baseline_commit(),
            "provenance": _provenance(digest),
            "cases": cases,
        }
    )


# ---------------------------------------------------------------------------
# What may be published
# ---------------------------------------------------------------------------


def _iter_strings(value: Any) -> Iterator[str]:
    """Every string in a nested structure, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def assert_publishable(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Refuse a payload that carries anything machine-specific.

    The baseline is committed and published, so a home directory, a
    credential or a sandbox profile name in it is a leak rather than a
    detail. Two rules:

    * none of the forbidden shapes appears anywhere in the payload;
    * every absolute path in it is under :data:`SCRATCH_ROOT`.

    The second rule is the stronger one, and it is what catches a path
    that is *not* a home directory but is still nobody else's business
    -- a per-user temp directory, a worktree that only exists on one
    machine. The only absolute paths the capture is allowed to record
    are the ones under the scratch root it made itself.

    Returns the payload, so it can be used inline.
    """
    for text in _iter_strings(payload):
        for needle in PUBLISH_FORBIDDEN:
            if needle in text:
                raise ValueError(
                    f"the baseline carries {needle!r} ({text!r}); it is "
                    f"published, and that identifies a machine or an "
                    f"operator"
                )
        if text.startswith("/") and not Path(text).is_relative_to(SCRATCH_ROOT):
            raise ValueError(
                f"the baseline carries the absolute path {text!r}, which "
                f"is outside the capture's own scratch root "
                f"({SCRATCH_ROOT}); it is published, and that identifies "
                f"a machine"
            )
    return payload


def _baseline_commit() -> str:
    """The commit the baseline is anchored to.

    A constant, not ``git rev-parse HEAD``: a baseline is a snapshot of
    *one* commit, and a field that follows the branch tip would change
    every time the file is regenerated -- which is exactly what a
    committed artifact must not do. The value names the commit whose
    ``backend/coding_tool.py`` produced this argv; that file is
    byte-identical at that commit and at the tree this was captured
    from.

    Bump it when the baseline is recaptured.
    """
    return BASELINE_COMMIT


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def _refuse(reason: str) -> NoReturn:
    """Stop, loudly, with a non-zero exit code.

    Args:
        reason: Why the capture is not going on.

    Raises:
        SystemExit: Always, with the exit code 2.
    """
    print(f"capture_claude_baseline: {reason}", file=sys.stderr)
    raise SystemExit(2)


def _verify_payload_provenance(payload: Dict[str, Any]) -> None:
    """Re-check that a payload really came from the baseline commit.

    This is the last thing before the fixture is written. A payload
    whose provenance does not name the baseline commit, or whose
    recorded digest is not the digest of the baseline's own
    ``coding_tool.py``, is refused: the fixture is a statement about
    that commit and nothing else.

    Args:
        payload: The payload about to be written.

    Raises:
        BaselineUnavailable: The payload does not carry the baseline's
            provenance.
    """
    provenance = payload.get("provenance") or {}
    if provenance.get("commit_sha") != baseline_sha():
        raise BaselineUnavailable(
            "the payload does not carry the baseline commit's SHA; it "
            "was not taken from the baseline"
        )
    expected = hashlib.sha256(
        _git("show", f"{BASELINE_COMMIT}:backend/coding_tool.py", binary=True)
    ).hexdigest()
    if provenance.get("coding_tool_sha256") != expected:
        raise BaselineUnavailable(
            "the payload does not carry the baseline's own "
            "backend/coding_tool.py; it was not taken from the baseline"
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Take the baseline, or check the committed one.

    Two modes, and only two:

    * the default mode *checks*. The capture is taken from the baseline
      commit and compared with the committed fixture. Nothing is
      written, ever -- there is no path from here to a fixture that the
      current implementation produced.
    * ``--write-baseline`` *writes* the fixture, and only from the
      baseline commit: the working tree has to be at that commit, and
      the payload's provenance is re-checked against the commit's own
      file before anything is written.

    Args:
        argv: The arguments. Defaults to the process's own.

    Returns:
        ``0`` when the check passed.

    Raises:
        SystemExit: With the exit code 2, when the capture refuses to
            go on.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    write_baseline = "--write-baseline" in args
    for arg in args:
        if arg != "--write-baseline":
            _refuse(
                f"unknown argument {arg!r}; the capture takes "
                "--write-baseline and nothing else"
            )

    try:
        head = head_commit()
        baseline = baseline_sha()
    except BaselineUnavailable as exc:
        _refuse(str(exc))

    if head != baseline:
        _refuse(
            "the claude argv baseline may only be taken from the commit "
            f"it describes: HEAD is {head[:12]} and the baseline is "
            f"anchored to {BASELINE_COMMIT}. A capture taken anywhere "
            "else would be the current implementation vouching for "
            "itself. Check out the baseline commit (in a worktree, if "
            "this one is busy) and run the capture there."
        )

    try:
        payload = capture_cases()
    except BaselineUnavailable as exc:
        _refuse(str(exc))

    golden_path = _HERE.parent / "claude_argv_golden.json"

    if not write_baseline:
        if not golden_path.is_file():
            _refuse(
                f"there is no committed baseline at {golden_path} to "
                "check the capture against"
            )
        committed = json.loads(golden_path.read_text(encoding="utf-8"))
        if committed != payload:
            _refuse(
                "the committed baseline no longer matches the capture "
                f"taken from {BASELINE_COMMIT}"
            )
        print(
            f"the committed baseline still matches the capture taken from "
            f"{BASELINE_COMMIT}"
        )
        return 0

    try:
        _verify_payload_provenance(payload)
    except BaselineUnavailable as exc:
        _refuse(str(exc))

    golden_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {golden_path} from the capture taken at {BASELINE_COMMIT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
