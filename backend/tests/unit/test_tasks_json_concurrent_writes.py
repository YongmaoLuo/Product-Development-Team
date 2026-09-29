"""Concurrent writes to ``tasks.json`` must not lose the race.

The bug
-------
``tasks.json`` is the dispatcher's startup input and the authoritative
record of task status. Every running sub-agent persists it on its own
status change, so several writers touch the same path at once.

Three call sites each built the *same fixed* temp name and renamed it
into place::

    tmp = tasks_file.parent / (tasks_file.name + ".tmp")   # execution.py
    tmp_path = self.tasks_file.with_suffix(".json.tmp")    # task_manager.py
    tmp_path = tasks_file.with_suffix(".tmp")              # agent.py

Two writers therefore truncate one temp file, and whichever renames
second finds nothing to rename::

    [Errno 2] No such file or directory:
        '.../tasks.json.tmp' -> '.../tasks.json'

With several sub-agents in flight this bites: a task dies on the rename
while its actual work was fine.

``utils.atomic_io.atomic_write_json`` already builds its temp file with
:func:`tempfile.mkstemp`, which is unique per writer — so the fix is for
every writer to go through it rather than keep a private copy of the
write-then-rename dance.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from utils.atomic_io import atomic_write_json  # noqa: E402


def test_concurrent_writes_all_succeed(tmp_path):
    """N threads writing the same path must not raise.

    With a fixed temp name this fails with ``FileNotFoundError`` /
    ``ENOENT`` on the rename; with ``mkstemp`` every writer owns its own
    temp file and the last rename wins.
    """
    target = tmp_path / "tasks.json"
    errors: list = []
    barrier = threading.Barrier(8)

    def writer(n: int) -> None:
        try:
            barrier.wait(timeout=5)
            for _ in range(20):
                atomic_write_json(
                    target,
                    {"requirement": "r", "tasks": [{"id": str(n)}]},
                    indent=2,
                    reraise=True,
                )
        except BaseException as exc:  # noqa: BLE001 — recorded, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, (
        f"concurrent atomic writes raised: {[type(e).__name__ + ': ' + str(e) for e in errors][:3]}"
    )


def test_no_temp_file_is_left_behind(tmp_path):
    """Every writer cleans up its own temp file."""
    target = tmp_path / "tasks.json"
    for n in range(10):
        atomic_write_json(target, {"tasks": [{"id": str(n)}]}, reraise=True)

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "tasks.json"]
    assert leftovers == [], f"temp files survived: {leftovers}"


def test_write_is_atomic_reader_sees_a_complete_document(tmp_path):
    """A reader never observes a partially written file."""
    import json

    target = tmp_path / "tasks.json"
    atomic_write_json(target, {"tasks": [{"id": "1"}]}, reraise=True)

    seen: list = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                seen.append(json.loads(target.read_text(encoding="utf-8")))
            except FileNotFoundError:
                pass
            except json.JSONDecodeError as exc:
                seen.append(exc)

    r = threading.Thread(target=reader, daemon=True)
    r.start()
    try:
        for n in range(60):
            atomic_write_json(
                target, {"tasks": [{"id": str(n)}]}, reraise=True
            )
    finally:
        stop.set()
        r.join(timeout=5)

    torn = [s for s in seen if not isinstance(s, dict)]
    assert not torn, f"reader observed torn writes: {torn[:2]}"


@pytest.mark.parametrize(
    "module, forbidden",
    [
        ("task_manager.py", 'with_suffix(".json.tmp")'),
        ("routes/execution.py", 'tasks_file.name + ".tmp"'),
        ("agent.py", 'tasks_file.with_suffix(".tmp")'),
    ],
)
def test_writers_do_not_hand_roll_a_fixed_temp_name(module, forbidden):
    """Source gate: no writer may reintroduce a shared temp path.

    Behaviour tests cannot reliably reproduce a race in CI, so this pins
    the shape instead — the defect was three hand-rolled copies of
    write-then-rename, and the way it returns is someone pasting the old
    block into a fourth writer.
    """
    source = (_BACKEND_DIR / module).read_text(encoding="utf-8")
    assert forbidden not in source, (
        f"{module} builds a fixed temp filename again ({forbidden}); go "
        f"through utils.atomic_io.atomic_write_json so each writer owns "
        f"its temp file"
    )
