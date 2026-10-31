"""The CI gate container image is defined in the repository, not typed by hand.

The local gate used to run its lanes inside a container that was
created by a hand-written ``docker run`` command: the image had no
definition in the tree, so a dependency the lane needed was installed
with a one-off ``pip install`` inside the running container. The
result was a local run that could disagree with CI, which installs
from the locked manifest.

Two files close that gap:

* ``scripts/ci_container.Dockerfile`` -- the image definition: the
  interpreter CI runs, and the backend's dependencies inside a
  virtualenv, so a container starts and runs a lane without
  installing anything.
* ``scripts/build_ci_container.sh`` -- the build entry point: one
  command, one default tag, one overridable tag, and a loud failure
  when there is no docker to build with.

Every assertion here is static -- it reads the two files -- apart
from the subprocesses, which run the build script against stand-in
``docker`` / ``uv`` executables on ``PATH``. No docker daemon is
needed to check any of it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = PROJECT_ROOT / "scripts" / "ci_container.Dockerfile"
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_ci_container.sh"

#: The image tag a caller gets when it does not ask for one.
DEFAULT_IMAGE = "pdt-ci:local"

#: The path of the image definition, relative to the repository root.
#: The build script prints this path in its success line, so it is
#: spelled the same way in the script and here.
DOCKERFILE_REL = "scripts/ci_container.Dockerfile"

#: The dependency manifest the image copies in. The repository does not
#: carry this file -- its dependency source of truth is
#: ``backend/pyproject.toml`` plus ``backend/uv.lock`` -- so the build
#: script exports it from the lock file for the duration of the build.
REQUIREMENTS_REL = "backend/requirements.txt"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dockerfile_instructions() -> list[str]:
    """The Dockerfile's instruction lines, with comments and blanks removed."""
    instructions: list[str] = []
    for raw in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        instructions.append(stripped)
    return instructions


#: A stand-in for ``docker`` that records the arguments it was called
#: with, plus whether the dependency manifest was present in the build
#: context at that moment, and exits successfully. The build script's
#: own environment reaches the child, so the paths travel in
#: environment variables rather than in the fake's body.
_FAKE_DOCKER = """#!/bin/sh
printf '%s\\n' "$*" >> "$PDT_CI_TEST_DOCKER_LOG"
if [ -n "${PDT_CI_TEST_MANIFEST:-}" ]; then
    if [ -f "$PDT_CI_TEST_MANIFEST" ]; then
        printf 'present\\n' > "$PDT_CI_TEST_MANIFEST_STATE"
    else
        printf 'absent\\n' > "$PDT_CI_TEST_MANIFEST_STATE"
    fi
fi
exit 0
"""

#: A stand-in for ``uv export --output-file <path>``: it records the
#: arguments it was called with and writes the file the build script
#: asked for, and does nothing else.
_FAKE_UV = """#!/bin/sh
printf '%s\\n' "$*" >> "$PDT_CI_TEST_UV_LOG"
out=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --output-file|-o)
            out="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done
if [ -n "$out" ]; then
    printf 'pytest==0.0.0\\n' > "$out"
fi
exit 0
"""


def _fake_repo_root(tmp_path: Path) -> Path:
    """A throwaway copy of the repository's build entry points.

    The build script derives the repository root from its own
    location, so running a copy of it inside a temporary directory is
    a faithful run -- and it keeps the *real* tree out of it.

    That isolation is not tidiness. The script has to materialise
    ``backend/requirements.txt`` for the build, and the repository
    carries a static gate that fails when that file is present
    (``backend/tests/static_gates/
    test_dependency_manifest_and_lock_agree.py``). A test that wrote
    the manifest into the real tree would race that gate in any
    pytest run that collects both directories -- the nightly lane
    runs ``pytest tests/ -n 2``, which collects both.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "backend").mkdir(parents=True)
    shutil.copy2(BUILD_SCRIPT, root / "scripts" / BUILD_SCRIPT.name)
    shutil.copy2(DOCKERFILE, root / "scripts" / DOCKERFILE.name)
    return root


def _run_build_script(
    root: Path, tmp_path: Path, env_overrides: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    """Run the build script in *root* with stand-in docker and uv on PATH.

    Returns the completed process and the logs the stand-ins wrote:
    ``docker_args``, ``uv_args``, ``manifest_state`` and ``manifest``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name, body in (("docker", _FAKE_DOCKER), ("uv", _FAKE_UV)):
        fake = bin_dir / name
        fake.write_text(body, encoding="utf-8")
        fake.chmod(0o755)

    logs = {
        "docker_args": str(tmp_path / "docker.log"),
        "uv_args": str(tmp_path / "uv.log"),
        "manifest_state": str(tmp_path / "manifest-state.log"),
        "manifest": str(root / REQUIREMENTS_REL),
    }

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PDT_CI_TEST_DOCKER_LOG"] = logs["docker_args"]
    env["PDT_CI_TEST_UV_LOG"] = logs["uv_args"]
    env["PDT_CI_TEST_MANIFEST"] = logs["manifest"]
    env["PDT_CI_TEST_MANIFEST_STATE"] = logs["manifest_state"]
    env.update(env_overrides or {})

    result = subprocess.run(
        ["bash", str(root / "scripts" / BUILD_SCRIPT.name)],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )
    return result, logs


def _read_log(path: str) -> str:
    """The contents of a log the stand-ins wrote, or ``""`` if none."""
    log = Path(path)
    if not log.exists():
        return ""
    return log.read_text(encoding="utf-8").strip()


def _build_flags(arguments: str) -> dict[str, str]:
    """The value of each ``docker build`` flag in *arguments*.

    Read as flags rather than as substrings: ``--file`` and ``-f``
    name the same thing, and a test that pins one spelling pins the
    implementation instead of the contract.
    """
    tokens = arguments.split()
    flags: dict[str, str] = {}
    for index, token in enumerate(tokens):
        if token in {"-f", "--file", "-t", "--tag"} and index + 1 < len(tokens):
            flags[token] = tokens[index + 1]
    return flags


# ---------------------------------------------------------------------------
# The image definition
# ---------------------------------------------------------------------------


def test_the_image_pins_python_311() -> None:
    """The image is built on the interpreter the CI jobs run.

    A gate that ran the suite on a different interpreter than CI
    compared two different things and called the difference a result.
    The pin is asserted on the first instruction, not on a comment:
    a comment can drift away from the base image without anything
    noticing.
    """
    instructions = _dockerfile_instructions()

    assert instructions, (
        "scripts/ci_container.Dockerfile has no instructions; the image "
        "would build from an empty definition"
    )

    first = instructions[0]
    assert first.startswith("FROM "), (
        f"the first instruction of the image definition is {first!r}, not "
        "a FROM; the base image is what pins the interpreter"
    )
    assert "python:3.11" in first, (
        f"the base image is {first!r}; the CI jobs run Python 3.11, so a "
        "local container on another interpreter would not reproduce them"
    )


def test_the_image_installs_the_backend_requirements_into_a_venv() -> None:
    """The backend's dependencies are baked into a virtualenv in the image.

    Two properties are pinned here, and both matter for the same
    reason: a container that installs at start-up is a container whose
    contents depend on when it was started, which is exactly the
    drift the image is meant to remove.

    * the dependency manifest is copied in, and
    * it is installed with the virtualenv's own ``pip``, so the
      packages land in the virtualenv rather than in whatever
      interpreter happens to be first on ``PATH``.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY backend/requirements.txt" in text, (
        "the image does not copy the dependency manifest in; without it "
        "the container would install its dependencies at start-up"
    )
    assert "python -m venv /opt/venv" in text, (
        "the image does not create /opt/venv; the dependencies have to "
        "land in a virtualenv rather than in the system interpreter"
    )
    assert "/opt/venv/bin/pip install -r" in text, (
        "the dependencies are not installed with the virtualenv's pip; "
        "a bare `pip install` resolves against whichever interpreter is "
        "first on PATH"
    )

    venv_at = text.index("python -m venv /opt/venv")
    install_at = text.index("/opt/venv/bin/pip install -r")
    assert venv_at < install_at, (
        "the image installs into /opt/venv before creating it"
    )


def test_the_image_does_not_bake_the_repository() -> None:
    """The image carries dependencies, not the repository.

    The repository is mounted at run time. Copying it into the image
    would make every source change require a rebuild, and would put a
    second copy of the tree inside the container -- the same leak the
    per-lane containers exist to prevent.

    The one file from the backend tree that belongs in the image is
    the dependency manifest: it is what the virtualenv is built from,
    and the file is a build-time export, not the repository.
    """
    for instruction in _dockerfile_instructions():
        verb, _, argument = instruction.partition(" ")
        if verb.upper() not in {"COPY", "ADD"}:
            continue
        sources = argument.split()
        assert sources, f"a {verb} instruction with no source: {instruction!r}"
        for source in sources:
            assert source not in {".", "./", "/", "*"}, (
                f"{instruction!r} copies the whole build context into the "
                "image; the repository is mounted at run time instead"
            )
            assert not source.endswith("/"), (
                f"{instruction!r} copies a directory into the image; the "
                "repository is mounted at run time instead"
            )
            if source.startswith("backend/"):
                assert source == REQUIREMENTS_REL, (
                    f"{instruction!r} copies {source!r} into the image; the "
                    "only file from the backend tree that belongs there is "
                    "the dependency manifest"
                )


# ---------------------------------------------------------------------------
# The build script
# ---------------------------------------------------------------------------


def test_the_build_script_names_the_dockerfile_and_a_default_tag() -> None:
    """The build script names the image definition and carries a default tag.

    Both spellings live in the script so the two cannot drift apart:
    the path it builds and the path it prints in its success line, and
    the tag a caller lands on when it does not ask for one.
    """
    text = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert DOCKERFILE_REL in text, (
        "the build script does not name the image definition it builds"
    )
    assert DEFAULT_IMAGE in text, (
        "the build script has no default image tag; a caller that does "
        "not set PDT_CI_IMAGE has nothing to land on"
    )
    assert "PDT_CI_IMAGE" in text, (
        "the image tag is not overridable; PDT_CI_IMAGE is how a caller "
        "builds a variant without editing the script"
    )
    assert "set -euo pipefail" in text, (
        "the build script does not fail fast on the first error"
    )


def test_the_build_script_exits_127_without_docker() -> None:
    """Without a docker to build with, the script says so and exits 127.

    127 is the exit code a shell already uses for "command not found",
    so a caller can tell "there is no docker here" apart from "the
    build failed". The message is on stderr, which is where this
    repository's scripts report failures -- and the exit code is what
    a script can act on.

    The check runs before anything else, so the run that finds no
    docker also changes nothing: no manifest is exported, no image is
    built.
    """
    bash = shutil.which("bash")
    assert bash is not None, "bash is required to run the build script"

    env = dict(os.environ)
    env["PATH"] = ""

    result = subprocess.run(
        [bash, str(BUILD_SCRIPT)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 127, (
        f"the build script exited {result.returncode} without docker, "
        "expected 127"
    )
    assert "docker is required" in result.stdout + result.stderr, (
        "the build script did not say that docker is required; output was: "
        f"{result.stdout!r} / {result.stderr!r}"
    )
    assert not (PROJECT_ROOT / REQUIREMENTS_REL).exists(), (
        "the build script exported the dependency manifest before "
        "checking for docker; the run that cannot build should not "
        "change the tree"
    )


def test_the_build_script_builds_the_repository_root_as_the_context(
    tmp_path: Path,
) -> None:
    """The build context is the repository root, and the tag is the default.

    The context decides what the image definition's ``COPY``
    instructions resolve against, so it is pinned rather than left to
    wherever the script happened to be invoked from.
    """
    root = _fake_repo_root(tmp_path)
    result, logs = _run_build_script(root, tmp_path)

    assert result.returncode == 0, (
        f"the build script failed: {result.stdout!r} / {result.stderr!r}"
    )

    arguments = _read_log(logs["docker_args"])
    flags = _build_flags(arguments)
    dockerfile = flags.get("-f", flags.get("--file", ""))
    tag = flags.get("-t", flags.get("--tag", ""))

    assert Path(dockerfile).resolve() == (root / DOCKERFILE_REL).resolve(), (
        f"the build did not use the image definition in the repository: "
        f"{arguments!r}"
    )
    assert tag == DEFAULT_IMAGE, (
        f"the build did not use the default image tag: {arguments!r}"
    )
    assert Path(arguments.split()[-1]).resolve() == root.resolve(), (
        f"the build context is not the repository root: {arguments!r}"
    )
    assert result.stdout.splitlines() == [
        f"built {DEFAULT_IMAGE} from {DOCKERFILE_REL}"
    ], (
        "the build script's stdout is not exactly the line it promises; "
        "a caller that captures stdout to learn what was built should "
        f"get one line and nothing else: {result.stdout!r}"
    )


def test_the_build_script_honours_the_image_override(tmp_path: Path) -> None:
    """``PDT_CI_IMAGE`` replaces the default tag."""
    root = _fake_repo_root(tmp_path)
    result, logs = _run_build_script(
        root, tmp_path, env_overrides={"PDT_CI_IMAGE": "pdt-ci:test"}
    )

    assert result.returncode == 0, (
        f"the build script failed: {result.stdout!r} / {result.stderr!r}"
    )
    arguments = _read_log(logs["docker_args"])
    flags = _build_flags(arguments)
    assert flags.get("-t", flags.get("--tag", "")) == "pdt-ci:test", (
        f"the image tag was not overridden: {arguments!r}"
    )
    assert result.stdout.splitlines() == [
        f"built pdt-ci:test from {DOCKERFILE_REL}"
    ], (
        "the build script's stdout is not exactly the line it promises; "
        f"a caller that captures stdout should get one line: {result.stdout!r}"
    )


def test_the_build_script_exports_the_manifest_and_takes_it_away(
    tmp_path: Path,
) -> None:
    """The manifest is exported from the lock file for the build, then removed.

    The repository carries one dependency source of truth --
    ``backend/pyproject.toml`` with ``backend/uv.lock`` beside it --
    and a gate (``backend/tests/static_gates/
    test_dependency_manifest_and_lock_agree.py``) fails the build if a
    requirements file appears beside them. The image needs a manifest
    to install from, so the script exports one from the lock file,
    builds with it, and removes it again.

    Both halves are checked: the manifest has to be there *during* the
    build, or the image would not build at all, and it has to be gone
    *after* it, or the tree would carry a second manifest.
    """
    root = _fake_repo_root(tmp_path)
    manifest = root / REQUIREMENTS_REL
    assert not manifest.exists(), "the manifest should not exist before the build"

    result, logs = _run_build_script(root, tmp_path)

    assert result.returncode == 0, (
        f"the build script failed: {result.stdout!r} / {result.stderr!r}"
    )

    uv_arguments = _read_log(logs["uv_args"])
    assert "export" in uv_arguments, (
        f"the manifest was not exported from the lock file: {uv_arguments!r}"
    )
    assert "backend" in uv_arguments, (
        f"the export did not name the backend project: {uv_arguments!r}"
    )
    assert _read_log(logs["manifest_state"]) == "present", (
        "the dependency manifest was not in the build context while the "
        "image was built; the image's COPY instruction could not resolve"
    )
    assert not manifest.exists(), (
        "backend/requirements.txt was left behind by the build; the "
        "repository's dependency manifest is backend/pyproject.toml plus "
        "backend/uv.lock, and a second manifest is what the static gate "
        "exists to refuse"
    )
