"""Regression — verification artifact writers must not leave ``.tmp`` residue.

Why this file exists
--------------------
Both ``verification_ci_runner._write_artifact`` and
``verification_api_runner._write_artifact`` persist the raw verdict basis
with a write-replace pattern::

    fd, tmp_name = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)
    os.replace(tmp_name, target)

The ``.tmp`` exists from the moment ``mkstemp`` returns.  When
``json.dump`` raises — a non-serialisable payload, a closed fd, an OOM
halfway through — the exception propagates to a top-level
``except Exception`` that **only logs**.  The ``os.replace`` that would
have turned ``.tmp`` into the final file never runs, the inner
``os.fdopen`` context manager closed the fd but the filesystem entry
is still there, and ``.tmp`` stays behind as a foreverorphan.

The orphan is invisible to ``find_orphans()`` (which only sweeps for
legacy JSON or analysis files), so the regression accumulates silently.
A plan re-running a Phase-2 gate several times and failing on every
``json.dump`` leaves a stack of stale ``.tmp`` files in the artifact
directory that no current test will catch — pinning it here.

Two contracts this file pins
----------------------------

1. **No residue on failure.** A non-serialisable payload (a ``set``,
   which ``json`` cannot encode) makes ``json.dump`` raise inside the
   inner ``with`` block.  The exception is caught upstream and the
   verdict is reported; the *file* left on disk must be empty.

2. **No residue on success.** A successful write replaces the
   ``.tmp`` with the named target, so ``artifact_dir.rglob("*.tmp")``
   sees no entry and the target file's content matches what was
   passed in.

Both writers are exercised: the CI runner's helper refuses to hand
its caller a half-written disk; the API runner's helper behaves the
same way.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# Backend root is two levels up from this file's directory.
BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _import_writer(module_name: str):
    """Import ``_write_artifact`` from one of the verification runners.

    ``importlib.import_module`` so the same test exercises both helpers
    without each test having to know the import path by heart.  Re-import
    on every call: a previous test may have monkey-patched a dependency
    (the inner ``json.dump`` target) and we want the live module to
    reflect it.
    """
    module = importlib.import_module(module_name)
    return module._write_artifact, module


# ---------------------------------------------------------------------------
# The two contracts both writers must pin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name, target_name",
    [
        ("verification_ci_runner", "ci_output.json"),
        ("verification_api_runner", "api_response.json"),
    ],
)
def test_write_artifact_leaves_no_tmp_on_error(
    tmp_path: Path, module_name: str, target_name: str,
) -> None:
    """A non-serialisable payload must not leave a ``.tmp`` behind.

    The path under test: ``mkstemp`` materialised the temp file, then
    ``json.dump(payload, handle)`` raised inside the ``os.fdopen``
    context manager.  The writer's outer ``except Exception`` logs and
    returns — it must unlink the partial file before returning,
    otherwise the artifact directory accumulates orphans on every
    Phase-2 failure.
    """
    write_artifact, _module = _import_writer(module_name)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    vp_id = "vp-tmp-leak"
    bad_payload: dict[str, Any] = {
        "vp_id": vp_id,
        # ``set`` is the canonical non-JSON value: ``json`` raises
        # ``TypeError: Object of type set is not JSON serializable``
        # the moment it sees it.
        "tags": {"a", "b"},
        "evidence": {"items": [1, 2, {"deep": {"oops": {1, 2}}}]},
    }

    write_artifact(artifact_dir, vp_id, bad_payload)

    residue = sorted(p.relative_to(artifact_dir).as_posix()
                     for p in artifact_dir.rglob("*.tmp"))
    assert residue == [], (
        f"{module_name}._write_artifact left .tmp residue after a "
        f"serialisation error: {residue}; the writer must unlink the "
        "partial temp file before the outer except returns"
    )

    target = artifact_dir / vp_id / target_name
    assert not target.exists(), (
        f"target file {target} should not exist — the payload failed to "
        "serialise, so neither the .tmp nor the target should remain"
    )


@pytest.mark.parametrize(
    "module_name, target_name",
    [
        ("verification_ci_runner", "ci_output.json"),
        ("verification_api_runner", "api_response.json"),
    ],
)
def test_write_artifact_replaces_atomically(
    tmp_path: Path, module_name: str, target_name: str,
) -> None:
    """The success path leaves no ``.tmp`` and writes a complete target.

    Pins the reason the wrapper exists in the first place: a successful
    write must atomic-replace the temp file with the named target, so
    the directory has ``[vp_id/<target_name>]`` and nothing else.  If
    either side regresses (no replace, partial dump) this test fails
    with a clear message.
    """
    write_artifact, _module = _import_writer(module_name)
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    vp_id = "vp-atomic"
    payload = {
        "vp_id": vp_id,
        "evidence": {
            "stdout_tail": "ok",
            "stderr_tail": "",
            "exit_code": 0,
        },
        "reasons": ["[full_ci] `pytest` 退出码 0（用时 1s）"],
    }

    write_artifact(artifact_dir, vp_id, payload)

    residue = list(artifact_dir.rglob("*.tmp"))
    assert not residue, (
        f"{module_name}._write_artifact left .tmp residue after a "
        f"successful write: {[p.relative_to(artifact_dir).as_posix() for p in residue]}; "
        "os.replace must move the .tmp into the target file"
    )

    target = artifact_dir / vp_id / target_name
    assert target.is_file(), (
        f"target file {target} is missing after a successful write; "
        "the writer must replace the .tmp into a named artifact"
    )

    on_disk = json.loads(target.read_text(encoding="utf-8"))
    assert on_disk == payload, (
        f"{module_name}._write_artifact wrote a partial or corrupted "
        f"payload: got {on_disk!r}, expected {payload!r}"
    )
