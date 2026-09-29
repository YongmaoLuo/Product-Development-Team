"""Cross-process concurrent ``append_verdict`` atomicity (bug 5 anchor).

This is the bug-5 regression test that lives at the path the verification
harness expects (``tests/concurrency/test_concurrent_verdict_append_no_lost_update.py``).
It is the **cross-process** companion to the in-process thread test in
``state_machine/tests/unit/test_verification_repository.py::test_concurrent_append_verdict_no_lost_update``.

The in-process test is cheap to write but it shares a single Python
process — meaning a regression that depends on Python's GIL or on
SQLite's per-process connection cache would not be caught.  This file
spawns **eight independent subprocesses** (the kind of layout that the
real verification executor uses when 8 worker VPs run in parallel),
each writing 25 verdicts with a tagged ``(worker_id, seq)`` pair, and
then asserts on the post-run on-disk database:

  1. Exactly 200 verdicts are present in ``plan_verification.verdicts``.
  2. The set of ``(worker_id, seq)`` tags equals the full Cartesian
     product ``{(w, s) for w in range(8) for s in range(25)}`` —
     no tag is duplicated, none is missing (no lost update).
  3. Every verdict JSON-decodes and contains the expected keys.
  4. A concurrent reader (running alongside the writers in a separate
     subprocess) sees only legal JSON whose length is monotonically
     non-decreasing across samples.
  5. The ``plan_execution`` row for the same ``plan_id`` is untouched
     (cross-table isolation — bug 1 anchor contracts).
  6. **Sanity guard** — the legacy JSON-file append path (the pre-
     refactor ``verdicts.json`` write) on this same workload MUST
     drop updates.  If the legacy path "passes" this assertion we
     know the workload is too easy (it would have been a flaky test),
     not that the new path is wrong.
"""

from __future__ import annotations

import json
import os
import string
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

# Backend root is two levels up from this file's directory.
BACKEND_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = BACKEND_ROOT / ".venv" / "bin" / "python3"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Yield a fresh tmp_path SQLite database with the four-table schema."""
    path = tmp_path / "state.db"
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate

    conn = open_db(path)
    migrate(conn)
    conn.close()
    return path


@pytest.fixture
def seeded_db(db_path: Path) -> Path:
    """Yield ``db_path`` after seeding plan_* rows for ``plan_id='p1'``."""
    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(db_path)
    repo = VerificationRepository(conn)
    repo.insert("p1", "running", verdicts=[])
    conn.execute(
        "INSERT INTO plan_execution "
        "(plan_id, current_phase, attempt_count, project_dir, "
        " task_progress, next_run_at, updated_at) "
        "VALUES ('p1', 'executing', 0, NULL, NULL, NULL, '2026-08-05T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO plan_routing "
        "(plan_id, current_phase, substage, version, updated_at) "
        "VALUES ('p1', 'executing', NULL, 0, '2026-08-05T00:00:00Z')"
    )
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Subprocess script templates
# ---------------------------------------------------------------------------


_WRITER_TEMPLATE = string.Template(textwrap.dedent(
    '''
    """Writer subprocess -- append $per_worker verdicts tagged (worker_id, seq)."""
    from __future__ import annotations

    import json
    import os
    import time
    from pathlib import Path

    DB_PATH = Path($db_path_literal)
    BARRIER_DIR = Path($barrier_dir_literal)
    WORKER_ID = $worker_id
    PER_WORKER = $per_worker
    N_WORKERS = $n_workers
    MARKER_PATH = Path($marker_path_literal)

    from state_machine.db.connection import open as open_db
    from state_machine.repositories.verification_repository import (
        VerificationRepository,
    )

    conn = open_db(DB_PATH)
    repo = VerificationRepository(conn)

    BARRIER_DIR.mkdir(parents=True, exist_ok=True)
    (BARRIER_DIR / ("ready_%d" % os.getpid())).write_text("ready", encoding="utf-8")

    go_file = BARRIER_DIR / "go"
    deadline = time.time() + 30.0
    while not go_file.exists():
        if time.time() > deadline:
            raise SystemExit("writer %d: barrier timeout" % WORKER_ID)
        time.sleep(0.005)

    for seq in range(PER_WORKER):
        repo.append_verdict("p1", {"worker_id": WORKER_ID, "seq": seq})

    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text(
        json.dumps({"worker_id": WORKER_ID, "pid": os.getpid()}),
        encoding="utf-8",
    )
    conn.close()
    '''
))


_READER_TEMPLATE = string.Template(textwrap.dedent(
    '''
    """Reader subprocess -- sample verdicts every sample_interval_ms ms."""
    from __future__ import annotations

    import json
    import time
    from pathlib import Path

    import sqlite3

    DB_PATH = Path($db_path_literal)
    STOP_FILE = Path($stop_file_literal)
    LOG_PATH = Path($log_path_literal)
    SAMPLE_INTERVAL = $sample_interval_ms / 1000.0

    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_PATH, "w", encoding="utf-8", buffering=1)
    try:
        while not STOP_FILE.exists():
            row = conn.execute(
                "SELECT verdicts FROM plan_verification WHERE plan_id = 'p1'"
            ).fetchone()
            raw = row[0] if row else None
            sample = None
            ok = False
            try:
                sample = json.loads(raw) if raw else []
                ok = True
            except json.JSONDecodeError:
                sample = []
                ok = False
            log_fh.write(json.dumps({
                "ts": time.time(),
                "ok": ok,
                "length": len(sample) if isinstance(sample, list) else 0,
            }) + "\\n")
            time.sleep(SAMPLE_INTERVAL)
    finally:
        log_fh.close()
        conn.close()
    '''
))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spawn_writer(
    *,
    db_path: Path,
    barrier_dir: Path,
    worker_id: int,
    per_worker: int,
    n_workers: int,
    marker_path: Path,
    python_exe: Path,
    backend_root: Path,
) -> subprocess.Popen:
    script = _WRITER_TEMPLATE.substitute(
        db_path_literal=repr(str(db_path)),
        barrier_dir_literal=repr(str(barrier_dir)),
        worker_id=worker_id,
        per_worker=per_worker,
        n_workers=n_workers,
        marker_path_literal=repr(str(marker_path)),
    )
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(backend_root) + (os.pathsep + existing_pp if existing_pp else "")
    )
    return subprocess.Popen(
        [str(python_exe), "-c", script],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _spawn_reader(
    *,
    db_path: Path,
    stop_file: Path,
    log_path: Path,
    sample_interval_ms: int,
    python_exe: Path,
    backend_root: Path,
) -> subprocess.Popen:
    script = _READER_TEMPLATE.substitute(
        db_path_literal=repr(str(db_path)),
        stop_file_literal=repr(str(stop_file)),
        log_path_literal=repr(str(log_path)),
        sample_interval_ms=sample_interval_ms,
    )
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(backend_root) + (os.pathsep + existing_pp if existing_pp else "")
    )
    return subprocess.Popen(
        [str(python_exe), "-c", script],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _wait_for_ready_files(barrier_dir: Path, n: int, timeout: float = 30.0) -> list[Path]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ready = sorted(barrier_dir.glob("ready_*"))
        if len(ready) >= n:
            return ready
        time.sleep(0.01)
    raise RuntimeError(
        f"only {len(list(barrier_dir.glob('ready_*')))} of {n} writers ready "
        f"after {timeout}s"
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.bug_5
def test_concurrent_verdict_append_no_lost_update(seeded_db: Path, tmp_path: Path) -> None:
    """8 independent subprocesses each append 25 verdicts -> exactly 200 verdicts."""
    db_path = seeded_db
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    n_workers = 8
    per_worker = 25

    writers: list[subprocess.Popen] = []
    for worker_id in range(n_workers):
        marker = tmp_path / f"marker_w{worker_id}.json"
        proc = _spawn_writer(
            db_path=db_path,
            barrier_dir=barrier_dir,
            worker_id=worker_id,
            per_worker=per_worker,
            n_workers=n_workers,
            marker_path=marker,
            python_exe=VENV_PYTHON,
            backend_root=BACKEND_ROOT,
        )
        writers.append(proc)

    stop_file = tmp_path / "reader_stop"
    reader_log = tmp_path / "reader.log"
    reader = _spawn_reader(
        db_path=db_path,
        stop_file=stop_file,
        log_path=reader_log,
        sample_interval_ms=10,
        python_exe=VENV_PYTHON,
        backend_root=BACKEND_ROOT,
    )

    try:
        _wait_for_ready_files(barrier_dir, n_workers)
        (barrier_dir / "go").write_text("go", encoding="utf-8")

        for proc in writers:
            rc = proc.wait(timeout=120)
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            assert rc == 0, (
                f"writer subprocess exited with rc={rc}; stderr={stderr!r}"
            )

        # 2026-09-14 settle grace: the reader exits the moment STOP_FILE
        # appears, so under load it can stop BEFORE sampling the final
        # committed state (observed in the full lane: final sample 179
        # != 200 while the cold DB already held all 200 verdicts).  The
        # "final sample == post-commit total" assertion below is only
        # meaningful if the reader actually got a chance to observe the
        # post-commit total, so wait for that observation first.
        expected_total = n_workers * per_worker
        settle_deadline = time.time() + 10.0
        while time.time() < settle_deadline:
            last_len = None
            try:
                if reader_log.exists():
                    log_lines = reader_log.read_text(
                        encoding="utf-8"
                    ).strip().splitlines()
                    if log_lines:
                        last_len = json.loads(log_lines[-1]).get("length")
            except (OSError, json.JSONDecodeError):
                # Torn trailing line while the reader appends -- retry.
                last_len = None
            if last_len == expected_total:
                break
            time.sleep(0.02)

        stop_file.write_text("stop", encoding="utf-8")
        rc = reader.wait(timeout=30)
        stderr = reader.stderr.read().decode("utf-8", errors="replace") if reader.stderr else ""
        assert rc == 0, (
            f"reader subprocess exited with rc={rc}; stderr={stderr!r}"
        )
    finally:
        for proc in writers:
            if proc.poll() is None:
                proc.kill()
        if reader.poll() is None:
            stop_file.write_text("stop", encoding="utf-8")
            try:
                reader.wait(timeout=5)
            except subprocess.TimeoutExpired:
                reader.kill()

    from state_machine.db.connection import open as open_db

    cold = open_db(db_path)
    try:
        row = cold.execute(
            "SELECT verdicts FROM plan_verification WHERE plan_id = 'p1'"
        ).fetchone()
        assert row is not None, "plan_verification row missing after writers"
        verdicts = json.loads(row[0])
        assert len(verdicts) == n_workers * per_worker, (
            f"expected {n_workers * per_worker} verdicts, got {len(verdicts)} -- "
            f"bug 5 lost-update regression"
        )
        expected_tags = {(w, s) for w in range(n_workers) for s in range(per_worker)}
        actual_tags = {(v["worker_id"], v["seq"]) for v in verdicts}
        missing = expected_tags - actual_tags
        extra = actual_tags - expected_tags
        assert not missing and not extra, (
            f"verdict tag set mismatch -- missing={len(missing)} extra={len(extra)}; "
            f"missing[:5]={sorted(missing)[:5]} extra[:5]={sorted(extra)[:5]}"
        )
        for v in verdicts:
            assert isinstance(v, dict)
            assert "worker_id" in v
            assert "seq" in v
            assert isinstance(v["worker_id"], int)
            assert isinstance(v["seq"], int)

        exec_before = cold.execute(
            "SELECT plan_id, current_phase, attempt_count, project_dir, "
            "task_progress, next_run_at, updated_at "
            "FROM plan_execution WHERE plan_id = 'p1'"
        ).fetchone()
        assert exec_before is not None
        # Schema column order:
        #   0 plan_id, 1 current_phase, 2 attempt_count, 3 project_dir,
        #   4 task_progress, 5 next_run_at, 6 updated_at
        assert exec_before[1] == "executing"
        assert exec_before[2] == 0
        assert exec_before[3] is None
        assert exec_before[4] is None  # task_progress untouched
        assert exec_before[5] is None  # next_run_at untouched
        assert exec_before[6] == "2026-08-05T00:00:00Z"
    finally:
        cold.close()

    assert reader_log.exists(), "reader subprocess never wrote any samples"
    samples: list[dict[str, Any]] = []
    with reader_log.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            samples.append(json.loads(line))
    assert samples, "reader log was empty"
    assert all(s["ok"] is True for s in samples), (
        f"reader observed invalid JSON at some point -- first bad sample: "
        f"{next(s for s in samples if not s['ok'])!r}"
    )
    lengths = [s["length"] for s in samples]
    for i in range(1, len(lengths)):
        assert lengths[i] >= lengths[i - 1], (
            f"reader observed length regression at sample {i}: "
            f"{lengths[i - 1]} -> {lengths[i]} -- atomicity violation"
        )
    assert lengths[-1] == n_workers * per_worker, (
        f"final reader sample length {lengths[-1]} != {n_workers * per_worker} "
        f"-- readers must converge on the post-commit total"
    )


# ---------------------------------------------------------------------------
# Sanity guard -- legacy JSON-file append path drops updates on this workload
# ---------------------------------------------------------------------------


_LEGACY_APPEND_TEMPLATE = string.Template(textwrap.dedent(
    '''
    """Legacy JSON-file append -- read whole, append, write whole."""
    from __future__ import annotations

    import json
    import os
    import time
    from pathlib import Path

    JSON_PATH = Path($json_path_literal)
    BARRIER_DIR = Path($barrier_dir_literal)
    WORKER_ID = $worker_id
    PER_WORKER = $per_worker
    N_WORKERS = $n_workers
    MARKER_PATH = Path($marker_path_literal)

    BARRIER_DIR.mkdir(parents=True, exist_ok=True)
    (BARRIER_DIR / ("ready_%d" % os.getpid())).write_text("ready", encoding="utf-8")
    go_file = BARRIER_DIR / "go"
    deadline = time.time() + 30.0
    while not go_file.exists():
        if time.time() > deadline:
            raise SystemExit("legacy writer %d: barrier timeout" % WORKER_ID)
        time.sleep(0.005)

    for seq in range(PER_WORKER):
        data = []
        if JSON_PATH.exists():
            try:
                data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                # Race-prone legacy read can observe a partial file from
                # a concurrent writer; treat as "no prior data" and let
                # this writer overwrite with its own append.  The whole
                # point of this test is that such overwrites cause lost
                # updates, not that the subprocess exits cleanly.
                data = []
        data.append({"worker_id": WORKER_ID, "seq": seq})
        JSON_PATH.write_text(json.dumps(data), encoding="utf-8")

    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text(
        json.dumps({"worker_id": WORKER_ID, "pid": os.getpid()}),
        encoding="utf-8",
    )
    '''
))


def _spawn_legacy_writer(
    *,
    json_path: Path,
    barrier_dir: Path,
    worker_id: int,
    per_worker: int,
    n_workers: int,
    marker_path: Path,
    python_exe: Path,
) -> subprocess.Popen:
    script = _LEGACY_APPEND_TEMPLATE.substitute(
        json_path_literal=repr(str(json_path)),
        barrier_dir_literal=repr(str(barrier_dir)),
        worker_id=worker_id,
        per_worker=per_worker,
        n_workers=n_workers,
        marker_path_literal=repr(str(marker_path)),
    )
    return subprocess.Popen(
        [str(python_exe), "-c", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


@pytest.mark.bug_5
def test_legacy_json_append_loses_updates_under_contention(tmp_path: Path) -> None:
    """Sanity guard -- the legacy JSON-file append path loses updates."""
    json_path = tmp_path / "verdicts.json"
    json_path.write_text("[]", encoding="utf-8")
    barrier_dir = tmp_path / "barrier"
    barrier_dir.mkdir()
    n_workers = 8
    per_worker = 25

    procs: list[subprocess.Popen] = []
    for worker_id in range(n_workers):
        marker = tmp_path / f"legacy_marker_w{worker_id}.json"
        procs.append(_spawn_legacy_writer(
            json_path=json_path,
            barrier_dir=barrier_dir,
            worker_id=worker_id,
            per_worker=per_worker,
            n_workers=n_workers,
            marker_path=marker,
            python_exe=VENV_PYTHON,
        ))

    try:
        _wait_for_ready_files(barrier_dir, n_workers)
        (barrier_dir / "go").write_text("go", encoding="utf-8")
        for proc in procs:
            rc = proc.wait(timeout=60)
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            assert rc == 0, f"legacy writer subprocess exited with rc={rc}; stderr={stderr!r}"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()

    expected_total = n_workers * per_worker
    raw = json_path.read_text(encoding="utf-8")
    try:
        final = json.loads(raw)
    except json.JSONDecodeError:
        # 8 个子进程并发追加同一个文件，损坏的**形状**是不确定的：有时
        # 得到一个短列表，有时是交错的字节、整个文件根本 parse 不了。
        # 两种都是"legacy 路径有损"的证据，而本测试是 sanity guard——
        # 它要证明的是"这个路径靠不住"，不是某个特定的损坏格式。
        # 之前只认前者，于是在不同的 interleaving 下随机挂（2026-09-15
        # 的全量套件里就挂过一次，隔离复跑 6 次全过）。
        return
    assert len(final) < expected_total, (
        f"sanity-guard regression: legacy JSON append kept all "
        f"{len(final)}/{expected_total} updates -- workload is too easy, "
        f"the atomic-path test above is vacuous"
    )
    assert len(final) <= expected_total - 1
