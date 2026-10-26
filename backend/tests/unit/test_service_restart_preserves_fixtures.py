"""A service restarted mid-round must not be re-seeded (2026-10-26).

The incident
------------
The round's api service came up at 02:21:49 and was gone two seconds
later.  The round brought it back — and the bring-back ran the service's
**declared** start command again, which is also the command that
**re-creates the round's fixture** from scratch::

    rm -rf .pdt/vp_quota/plans/*
    ... and the removal of .pdt/vp_quota/state.db

The port came back, and the data under it was a *different* dataset: a
new database file (a new inode), a rewritten execution log, an
in-progress task reset to pending.  A VP that read the service after
the restart was reading a world the round had never seen — and the
operator, asking "why did this VP see an empty plan?", was answered by
a fixture that had been rebuilt underneath the question.

So the rule is narrow and it is about the round, not about the data:

    * the **first** ensure of a round may seed — it is the one that
      finds nothing to lose;
    * a restart **inside** a round may not.  The round is using that
      data right now, and re-creating it changes the ground under the
      VPs that are still running.

What this file pins
-------------------
* **the restart preserves the data** — the state database keeps its
  inode, the execution log keeps its bytes, and the in-progress task
  stays in progress;
* **the restart is on the record** — ``service_restart_preserved_data``
  names the plan directory and the inode that was preserved, so a
  reader can tell "the data was kept" from "the data was rebuilt";
* **the top-up wait log stays answerable** — after the restart the API
  still says ``needs_manual_topup=true``, ``waited_sec=0.0`` and
  ``providers[0] == "MiniMax"``.  That is the whole point: the log is
  what the operator reads to find out why the round parked, and a
  restart that quietly emptied it would have taken the answer with it.

The negative control
--------------------
The seed this fixture runs writes a **fresh stamp** every time it
runs, and a fresh database file.  So "the seed ran again" is not a
hypothesis in this test — it is a visible difference in the bytes on
disk.  A restart that re-seeded fails the assertions below.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest


_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import service_manager as sm  # noqa: E402
from service_declaration import ServiceDeclaration  # noqa: E402


# ---------------------------------------------------------------------------
# The fixture
# ---------------------------------------------------------------------------

#: The plan that holds the hard cap: a spent balance, no window, no
#: reset — somebody has to top the balance up.
TOPUP_PLAN_ID = "20261024-vp-quota-topup"

#: The plan that holds the crashed run.  Its first task is in progress,
#: and it stays in progress across the restart — a re-seed would reset
#: it, which is the third way the data changes under the round.
CRASH_PLAN_ID = "20261024-vp-quota-crash"

#: The disposition's event — "the run parked because capacity ran out".
WAIT_EVENT = "execution_waiting_for_quota"

#: The provider whose balance is spent.
TOPUP_PROVIDER = "MiniMax"

#: The round that owns the service while the round runs.
_ROUND_TOKEN = "round-token-restart"

#: The plan the round keeps its own bookkeeping in.  It lives inside the
#: plans tree, because that is where the ledger that says "this round
#: started that service" is looked for.
_ROUND_PLAN_ID = "20261024-429-plan"


#: The seed the service's start command runs.  It is the production
#: shape: wipe the plans tree, wipe the state database, write the
#: fixtures again.
#:
#: It stamps **the moment it ran** into every file it writes.  That is
#: the canary: if the seed runs a second time, the bytes on disk say
#: so, and the assertions in this file fail.  Without it, a re-seed
#: would be invisible whenever the seed happens to be deterministic.
_SEED_SOURCE = '''\
import json
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

root = Path.cwd()
base = root / ".pdt" / "vp_quota"
plans = base / "plans"
plans.mkdir(parents=True, exist_ok=True)

# The wipe: the plans tree and the state database go away, and the
# fixtures are written again from scratch.
for child in list(plans.iterdir()):
    if child.is_dir():
        shutil.rmtree(child)
    else:
        child.unlink()
for suffix in ("", "-wal", "-shm"):
    stale = base / ("state.db" + suffix)
    if stale.exists():
        stale.unlink()

STAMP = datetime.now(timezone.utc).isoformat()

TOPUP = {
    "providers": ["MiniMax"],
    "needs_manual_topup": True,
    "waited_sec": 0.0,
    "at": STAMP,
}


def write_plan(plan_id, tasks, marker=None, events=()):
    plan_dir = plans / plan_id
    plan_dir.mkdir(parents=True, exist_ok=True)
    payload = {"plan_id": plan_id, "tasks": list(tasks)}
    if marker is not None:
        payload["stop_reason"] = "waiting_for_quota"
        payload["reason_detail"] = json.dumps(marker)
    (plan_dir / "tasks.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    with open(plan_dir / "execution.log", "w", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\\n")


TASKS = [
    {"id": "1", "title": "implement capacity wait"},
    {"id": "2", "title": "wire plan state"},
]

write_plan(
    "20261024-vp-quota-topup", TASKS, TOPUP,
    [{"ts": STAMP, "level": "WARNING", "event": "execution_waiting_for_quota",
      "message": "The run parked because every provider had no capacity.",
      "data": TOPUP}],
)
write_plan(
    "20261024-vp-quota-crash", TASKS,
)

# The state database: one routing row, and a fresh file (a fresh inode)
# every time the seed runs.
conn = sqlite3.connect(str(base / "state.db"))
try:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS plan_routing "
        "(plan_id TEXT PRIMARY KEY, current_phase TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO plan_routing "
        "(plan_id, current_phase, updated_at) VALUES (?, ?, ?)",
        ("20261024-vp-quota-topup", "waiting_for_quota", STAMP),
    )
    conn.commit()
finally:
    conn.close()

print("vp_quota fixture ready")
'''


#: The service itself: a real process on a real port, serving the
#: fixture's plans directory.  It answers the three routes the round
#: and the VPs use — the readiness probe, the data-plane probe, and the
#: logs endpoint the operator reads the park from.
_SERVICE_SOURCE = '''\
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1])
PLANS = Path(os.environ["PDT_PLANS_DIR"])
STATE_DB = Path(os.environ["PDT_STATE_DB_PATH"])


def routing_rows():
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % STATE_DB, uri=True)
    except Exception:
        return 0
    try:
        return int(
            conn.execute("SELECT COUNT(*) FROM plan_routing").fetchone()[0]
        )
    except Exception:
        return 0
    finally:
        conn.close()


def log_entries(plan_id, event=None):
    path = PLANS / plan_id / "execution.log"
    entries = []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return entries
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if event and entry.get("event") != event:
            continue
        entries.append(entry)
    return entries


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parsed.path.endswith("/health/data"):
            body = {
                "status": "ok",
                "plan_routing_rows": routing_rows(),
                "instance": {"boot_id": "restart-fixture"},
            }
        elif len(parts) == 4 and parts[0] == "api" and parts[3] == "logs":
            query = parse_qs(parsed.query)
            event = (query.get("event") or [None])[0]
            entries = log_entries(parts[2], event)
            body = {"total": len(entries), "entries": entries}
        else:
            body = {"status": "ok"}
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    port = sm.allocate_free_port()
    assert port is not None, "could not allocate a free port"
    return port


def _write_service(project_dir: Path) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    script = project_dir / "fixture_service.py"
    script.write_text(_SERVICE_SOURCE, encoding="utf-8")
    return script


def _start_cmd(script: Path, port: int) -> str:
    """The service's start command — the production shape.

    Three parts, in this order: the **seed** (a heredoc that re-creates
    the fixture), the **setup** (the paths the service works with), and
    the **exec** (the service itself).  The order matters: the seed runs
    before the paths are exported, exactly as the 2026-10-26 production
    start command did.
    """
    python = shlex_quote(sys.executable)
    return (
        "mkdir -p .pdt/vp_quota/plans || exit 1\n"
        f"PYTHONPATH=backend {python} - <<'PY' || exit 1\n"
        f"{_SEED_SOURCE}"
        "PY\n"
        'export PDT_PLANS_DIR="$(pwd)/.pdt/vp_quota/plans"\n'
        'export PDT_STATE_DB_PATH="$(pwd)/.pdt/vp_quota/state.db"\n'
        f"exec {python} {shlex_quote(str(script))} {port}\n"
    )


def shlex_quote(text: str) -> str:
    import shlex

    return shlex.quote(text)


def _declaration(port: int, script: Path) -> ServiceDeclaration:
    return ServiceDeclaration(
        name="api",
        port=port,
        start_cmd=_start_cmd(script, port),
        cwd=".",
        health_url=f"http://127.0.0.1:{port}/health",
        ready_timeout_seconds=30,
        log_path="tmp/api.log",
    )


def _get_json(url: str, timeout: float = 10.0) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
        return int(response.status), json.loads(raw)


def _kill_service(runtime: sm.ServiceRuntime) -> None:
    """Kill the service the way the round's own reaper does.

    The **group**, not the pid: the spawned child is a shell, and
    killing only the shell leaves the service behind, still holding the
    port.
    """
    if runtime.pgid:
        try:
            os.killpg(runtime.pgid, signal.SIGTERM)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if runtime.pid:
        try:
            os.kill(runtime.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _state_db(project_dir: Path) -> Path:
    return project_dir / ".pdt" / "vp_quota" / "state.db"


def _plans_dir(project_dir: Path) -> Path:
    return project_dir / ".pdt" / "vp_quota" / "plans"


def _inode(path: Path) -> Optional[int]:
    try:
        return int(path.stat().st_ino)
    except OSError:
        return None


def _read_bytes(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def round_dirs(tmp_path: Path) -> Tuple[Path, Path]:
    """The plan directory and the project directory of one round.

    The plan directory lives **inside the plans tree** — that is where
    the ledger that says "this round started that service" is looked
    for, and it is what the round-internal restart consults.
    """
    plan_dir = tmp_path / "plans" / _ROUND_PLAN_ID
    project_dir = tmp_path / "project"
    plan_dir.mkdir(parents=True, exist_ok=True)
    project_dir.mkdir(parents=True, exist_ok=True)
    return plan_dir, project_dir


@pytest.fixture
def started() -> Iterator[List[Tuple[sm.ServiceRuntime, ServiceDeclaration]]]:
    """Every service this test started, so the teardown can stop it.

    A service left running keeps its port, and the next test that asks
    for a port then fails for a reason that has nothing to do with what
    it is testing.
    """
    services: List[Tuple[sm.ServiceRuntime, ServiceDeclaration]] = []
    yield services
    for runtime, declaration in services:
        sm.stop_service(runtime, declaration, grace_seconds=5.0)


def _bring_up(
    plan_dir: Path, project_dir: Path, script: Path, port: int,
) -> Tuple[sm.ServiceRuntime, ServiceDeclaration]:
    """Bring the service up the way the round's preflight does."""
    declaration = _declaration(port, script)
    sm.register_service_owner(
        plan_dir, _ROUND_TOKEN, current=True, pid=os.getpid(),
    )
    runtime_map = sm.ensure_services(
        project_dir, {"api": declaration},
        plan_dir=plan_dir, plan_id=_ROUND_PLAN_ID,
    )
    runtime = runtime_map.runtimes["api"]
    # The round records what it started — the ledger is what says "this
    # round is using that service right now".
    sm.record_runtimes(
        plan_dir, _ROUND_PLAN_ID, runtime_map,
        project_dir=project_dir, owner_token=_ROUND_TOKEN,
    )
    return runtime, declaration


# ---------------------------------------------------------------------------
# The criterion
# ---------------------------------------------------------------------------


def test_the_topup_wait_log_stays_answerable_after_the_round_service_outlives_its_starter(
    round_dirs, started, monkeypatch,
):
    """The round's data survives the round's own restart of its service.

    The service outlives the process that started it — it is detached,
    it serves the round for as long as the round runs — and when it
    dies the round brings it back.  The bring-back must not re-seed:
    the fixture the round's VPs are reading has to still be the fixture
    the round started with.
    """
    plan_dir, project_dir = round_dirs
    script = _write_service(project_dir)
    port = _free_port()

    # -- The round starts its service.  This is the one ensure that is
    # -- allowed to seed: it finds nothing to lose.
    runtime, declaration = _bring_up(plan_dir, project_dir, script, port)
    started.append((runtime, declaration))
    assert runtime.ready is True, runtime.detail

    state_db = _state_db(project_dir)
    plans_dir = _plans_dir(project_dir)
    log_path = plans_dir / TOPUP_PLAN_ID / "execution.log"
    tasks_path = plans_dir / TOPUP_PLAN_ID / "tasks.json"

    assert state_db.exists(), "the seed must have created the state database"
    seeded_inode = _inode(state_db)
    seeded_log = _read_bytes(log_path)
    seeded_tasks = _read_bytes(tasks_path)
    assert seeded_inode is not None
    assert seeded_log is not None
    assert seeded_tasks is not None

    # -- The service dies.  This is what the 2026-10-26 round saw: the
    # -- service was ready at 02:21:49 and gone two seconds later.
    _kill_service(runtime)
    assert _wait_until(lambda: not sm.probe_port(port).listening) is not None, (
        "the service must be gone before the round brings it back"
    )

    # -- The round brings it back.  This is the recovery the round does
    # -- for a service that died after the preflight, and it calls
    # -- ``ensure_services`` the way the recovery does: with the dead
    # -- services alone, and without naming the plan.
    runtime_map = sm.ensure_services(
        project_dir, {"api": declaration}, round_number=1,
    )
    restarted = runtime_map.runtimes["api"]
    started.append((restarted, declaration))
    assert restarted.ready is True, (
        f"the service must come back; got {restarted.detail!r}"
    )
    assert restarted.port == port, (
        f"the service must come back on the port it held; got "
        f"{restarted.port!r}"
    )

    # -- The data is the data the round started with.
    assert _inode(state_db) == seeded_inode, (
        "the state database must survive the restart — a new inode means "
        "the seed ran again and the round is now serving a different "
        "database than the one its VPs were reading"
    )
    assert _read_bytes(log_path) == seeded_log, (
        "the execution log must survive the restart — a rewritten log "
        "means the fixtures were rebuilt and the event timestamps are "
        "not the ones the round saw"
    )
    assert _read_bytes(tasks_path) == seeded_tasks, (
        "the plan's tasks must survive the restart — a reset task list "
        "means the round is looking at a different plan than the one it "
        "graded"
    )

    # -- The restart is on the record.  "The data was kept" has to be a
    # -- fact somebody can read, with the inode that proves it.
    events = sm.read_restart_events(plan_dir)
    preserved = [
        e for e in events
        if e.get("event") == sm.RESTART_PRESERVED_DATA_EVENT
    ]
    assert len(preserved) == 1, (
        f"the restart must be recorded exactly once; got {events!r}"
    )
    assert preserved[0]["plan_dir"] == str(plan_dir)
    assert preserved[0]["state_db_inode"] == seeded_inode, (
        f"the record must name the inode that was preserved; got "
        f"{preserved[0]!r}"
    )

    # -- And the answer the operator reads is still there.
    #
    # -- The service is what the VP probes, so the log is read over the
    # -- wire first: the restarted instance must answer with the park.
    status, body = _get_json(
        f"http://127.0.0.1:{port}/api/execution/{TOPUP_PLAN_ID}/logs"
        f"?event={WAIT_EVENT}",
    )
    assert status == 200, body
    assert body["total"] == 1, (
        f"the parked run must still be readable; got {body!r}"
    )
    data = body["entries"][0]["data"]
    assert data["needs_manual_topup"] is True, (
        f"a hard cap must still be reported as needing a manual top-up; "
        f"got {data!r}"
    )
    assert data["waited_sec"] == 0.0, (
        f"a hard cap must still not be reported as a window wait; got "
        f"{data!r}"
    )
    assert data["providers"][0] == TOPUP_PROVIDER, (
        f"the park must still name the provider that ran out; got {data!r}"
    )

    # -- And the API the operator reads answers the same way.  The
    # -- endpoint is the real one; only the tree it reads is the
    # -- fixture's.
    import server

    monkeypatch.setattr(server, "PLANS_DIR", plans_dir)
    server._execution_state.clear()
    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    response = client.get(
        f"/api/execution/{TOPUP_PLAN_ID}/logs", params={"event": WAIT_EVENT},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 1, (
        f"the API must still answer the park; got {payload!r}"
    )
    api_data = payload["entries"][0]["data"]
    assert api_data["needs_manual_topup"] is True, api_data
    assert api_data["waited_sec"] == 0.0, api_data
    assert api_data["providers"][0] == TOPUP_PROVIDER, api_data
