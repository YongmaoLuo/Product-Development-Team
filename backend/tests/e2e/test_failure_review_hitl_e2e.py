"""The failure-review chain, end to end, on a real page.

What this covers
----------------
A plan that really ended in failure is **announced**: the terminal
write the verification loop makes when the chain ends
(``_persist_verification_terminal(..., chain_ending=True)``) leaves a
``failure_review.json`` sidecar behind and a pending decision in the
HITL store.  The announcement is only worth something if a human can
find it, read it and answer it, so this case drives the whole chain:

* the waiting row shows the problem and the suggestion, and offers the
  answer — and nothing else (a failure review has no deadline to push
  out and no wait to terminate, so 延长 / 终止 are not offered);
* the answer settles the decision, and the row is gone;
* the plan's own card shows the same paragraph, and the read-only
  endpoint answers with the four recorded fields.

The seams this file drives
--------------------------
* the **terminal write** is the product's own hook
  (``server._persist_verification_terminal``), not
  ``failure_review.announce_failure`` called by hand: what is under
  test is the chain, and the announcement is one link of it;
* the **decision** is answered through the panel, which posts to the
  HITL answer endpoint with the decision id the waiting row carries;
* the **read model** is the scheduler's own (``GET /api/scheduler/state``)
  and the plan's own detail (``GET /api/scheduler/plan/{id}/detail``);
* the **executor** is a stand-in (``PDT_EXECUTOR_CMD``) and the
  scheduler's capacity is **zero**, so nothing is ever run: the case is
  about the failure review, not about a plan executing.

Two places where the tree's own naming differs from the plan's wording
----------------------------------------------------------------------------
Both are noted here because a reader of the plan's wording would look
for something the tree does not have:

1. The plan's wording asks for ``details.scheduler-failure-review``.
   The tree draws the block as ``<details data-section="failure_review">``
   (``frontend/app.js`` → ``schedulerSectionHtml``), so the case asks
   for **either** spelling and asserts on what it finds.
2. The plan's wording asks for a ``.scheduler-plan-card`` of a plan
   that is in the **failed** phase.  A failed plan is drawn in the
   terminal list — a *row*, not a card: the cards belong to the queue
   and to the running list (``renderSchedulerTerminalPlans`` says so in
   its own comment).  So the case brings the failed plan back the way
   an operator does — the terminal list's 「重新排队执行」 button, which
   is the legal ``failed → queued`` edge — and opens the card that
   appears.  The scheduler's capacity is zero, so the plan waits in the
   queue and is never run.

Reading the data
----------------
Every read the case makes goes through the **page's own** ``api()``
(``page.evaluate``), so the requests carry the guard header the
backend demands (``X-PDT-Request: 1``).  Nothing here asks the
backend for anything the page could not have asked for itself.

Test command::

    pytest backend/tests/e2e/test_failure_review_hitl_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

for _import_path in (str(REPO_ROOT), str(BACKEND_DIR)):
    if _import_path not in sys.path:
        sys.path.insert(0, _import_path)

from hitl.store import HitlStore  # noqa: E402
from state_machine.db.connection import open as open_db  # noqa: E402
from state_machine.db.schema import migrate  # noqa: E402
from state_machine.repositories.routing_repository import (  # noqa: E402
    RoutingRepository,
)


# ---------------------------------------------------------------------------
# The markers
# ---------------------------------------------------------------------------

#: ``backend/pytest.ini`` — the config a run from ``backend/`` resolves —
#: runs pytest with ``--strict-markers``, and pytest checks a marker at the
#: moment it is *used*, so a module that uses a marker the governing
#: config does not declare aborts collection for its whole module.
#: ``needs_real_process`` is declared in the repo-root ``pytest.ini`` and
#: not in ``backend/pytest.ini``, whose marker list is frozen; the module
#: registers the marker with the live config before it uses it — the
#: pattern ``backend/tests/unit/test_orphan_reaper_procs.py`` established.
_NEEDS_REAL_PROCESS_DESCRIPTION = (
    "needs_real_process: cases that drive a real server / real subprocess / "
    "real repository rather than a fixture double, so they need a quiet "
    "machine and a port to themselves."
)


def _register_needs_real_process_marker() -> None:
    """Declare the ``needs_real_process`` marker with the live pytest config."""
    config = getattr(pytest.mark, "_config", None)
    if config is None:  # pragma: no cover - a collection always has a config
        return
    known = {
        line.split(":")[0].split("(")[0].strip()
        for line in config.getini("markers")
    }
    if "needs_real_process" not in known:
        config.addinivalue_line("markers", _NEEDS_REAL_PROCESS_DESCRIPTION)


_register_needs_real_process_marker()

pytestmark = [pytest.mark.e2e, pytest.mark.needs_real_process]


# ---------------------------------------------------------------------------
# The scene
# ---------------------------------------------------------------------------

#: The plan this file fails on the purpose.  A leaf name, nothing
#: exotic: every route that takes a plan id validates it before it
#: resolves a path.
PLAN_ID = "e2e-failure-review"

#: What went wrong — the report's own summary.
PROBLEM = "验证失败：登录接口 500"

#: What should happen about it — the repair task's own title.
SUGGESTION = "建议修复：修复登录接口"

#: What the operator types into the waiting row.
ANSWER = "已确认：修复登录接口"

#: The backend subprocess has to run on the interpreter this suite was
#: installed into.  A checkout with no ``backend/.venv`` (fresh
#: worktree, container) falls back to the interpreter already running
#: pytest — never to a system python, which would not have the
#: dependencies installed.
_VENV_PYTHON = BACKEND_DIR / ".venv" / "bin" / "python3"
VENV_PYTHON = _VENV_PYTHON if _VENV_PYTHON.exists() else Path(sys.executable)

#: The stand-in executor, installed through the named production seam
#: (``PDT_EXECUTOR_CMD``).  Nothing runs in this case — the scheduler's
#: capacity is zero — but the seam is set anyway, so a case that went
#: wrong could not call a real model.
EXECUTOR_FIXTURE: Path = (
    BACKEND_DIR / "tests" / "e2e" / "fixtures" / "deterministic_executor.py"
)

#: How long the backend may take to answer its readiness probe.  A boot
#: that has not answered within this budget is a failure, and the
#: failure carries the tail of the server's own output: a boot that
#: died says why in there, and a boot that hung says where it stopped.
READY_TIMEOUT_SECONDS = 15.0

#: How long the browser may take to show what it is asked to show.  A
#: page that cannot show it in this budget is a failure, and the
#: failure says why — there is no fixed sleep anywhere in the driver.
BROWSER_TIMEOUT_MS = 20_000

#: How long the whole browser run may take.  The driver is a process of
#: its own, and a driver that hangs is killed rather than waited for.
DRIVER_TIMEOUT_SECONDS = 180.0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind an ephemeral loopback port and hand the number back.

    The kernel picks the port, so the number is free at the moment it
    is returned.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _server_output(proc: subprocess.Popen, limit: int = 4000) -> str:
    """The tail of the backend's own output (stdout and stderr alike)."""
    path = getattr(proc, "_pdt_log_path", None)
    if path is None:
        return "(no server output was captured)"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - the file is ours
        return f"(could not read {path}: {exc})"
    return text[-limit:]


def _boot(
    plans_dir: Path,
    state_db: Path,
    scheduler_settings: Path,
    log_path: Path,
    port: int,
) -> subprocess.Popen:
    """Start a real ``backend.server`` process against the seeded tree.

    The environment variables are the whole contract between the seed
    and the server: the plans tree, the state database, the scheduler's
    settings and the port.  The ones that are set here are the ones the
    child would otherwise inherit from the test process and misbehave
    with:

    * ``PDT_PROVIDER_MODE=inherit`` — the boot asks CC Switch for the
      provider chain and **aborts the startup** when it cannot be
      asked.  This server exists to answer one status question; it must
      not need a provider fleet to come up.
    * ``PYTHONPATH`` — ``backend/`` has to be importable, because the
      server's own modules import each other by their flat names
      (``from config_paths import ...``).
    * ``PDT_ORPHAN_REAPER=0`` — the boot otherwise walks **every
      process on the machine** to find orphans left behind by a
      previous life.  A throwaway server has no orphans to reap.
    * ``PYTEST_CURRENT_TEST`` — the product's own answer to "may this
      process touch the machine?": a *test* process must not reap the
      developer's processes and must never open the operator's live
      ``state.db``.

    The child is started in a **session of its own**
    (``start_new_session=True``), so the teardown can take the whole
    process group with it and leave no orphan behind.
    """
    env = dict(os.environ)
    env.update(
        {
            "PDT_PLANS_DIR": str(plans_dir),
            "PDT_STATE_DB_PATH": str(state_db),
            "PDT_SCHEDULER_FILE": str(scheduler_settings),
            "PDT_PORT": str(port),
            "PDT_PROVIDER_MODE": "inherit",
            "PDT_ORPHAN_REAPER": "0",
            "PDT_EXECUTOR_CMD": "{0} -u {1}".format(
                str(VENV_PYTHON), str(EXECUTOR_FIXTURE)
            ),
            "PYTEST_CURRENT_TEST": (
                "backend/tests/e2e/test_failure_review_hitl_e2e.py (setup)"
            ),
            "PYTHONPATH": "{0}{1}{2}".format(
                BACKEND_DIR, os.pathsep, env.get("PYTHONPATH", "")
            ),
        }
    )
    # A stale secret descriptor must never be closed by a process that
    # never owned it.
    for key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(key, None)
    env.pop("ANTHROPIC_BASE_URL", None)

    handle = log_path.open("wb")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "backend.server"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._pdt_log_path = log_path  # type: ignore[attr-defined]
    proc._pdt_log_handle = handle  # type: ignore[attr-defined]
    # The group id is read now, while the child is alive: it is the
    # only handle on the group that survives the child's own exit.
    proc._pdt_pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined]
    return proc


def _wait_ready(base_url: str, proc: subprocess.Popen) -> None:
    """Wait until the backend answers its readiness probe.

    The backend is up when the API answers, not when the process exists:
    the process exists for a moment before uvicorn is listening.  A
    backend that dies on the way up says so in its own output, and that
    output is attached to the failure.
    """
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"the backend exited with {proc.returncode} before it was "
                f"ready:\n{_server_output(proc)}"
            )
        request = urllib.request.Request(f"{base_url}/api/health")
        request.add_header("X-PDT-Request", "1")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status == 200:
                    return
                last = response.status
        except urllib.error.HTTPError as exc:
            last = exc.code
        except (urllib.error.URLError, ConnectionResetError, OSError) as exc:
            last = exc
        time.sleep(0.2)
    raise AssertionError(
        f"{base_url}/api/health was not ready within "
        f"{READY_TIMEOUT_SECONDS:.0f}s; last={last!r}\n"
        f"{_server_output(proc)}"
    )


def _kill_group(proc: subprocess.Popen) -> None:
    """Kill everything in the child's process group, then reap it.

    The group is the unit: a server that started a worker of its own
    leaves that worker behind when only the leader is killed.  The
    group id is read **once**, when the child is started, and kept —
    it cannot be read off the child any more once the child is gone,
    and a child that is gone is exactly the case where the group may
    still have members.
    """
    pgid = getattr(proc, "_pdt_pgid", None)
    if pgid is not None:
        try:
            os.killpg(int(pgid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            # The group is empty — every member is already gone.
            pass
    elif proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):  # pragma: no cover
            proc.kill()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive
        pass


def _teardown(proc: Optional[subprocess.Popen]) -> None:
    """Stop the backend and let go of its port.

    ``SIGTERM`` to the **group** first — uvicorn shuts down gracefully
    on SIGTERM, and the group is what carries the children it started
    — and ``SIGKILL`` to the group after that, whether or not the
    leader is still alive.  The group kill is unconditional on purpose:
    a leader that exits on its own while a member of its group is still
    running is exactly the case where "kill the leader" leaves an
    orphan behind.  The port has to be free again before the next boot
    binds it.
    """
    if proc is None:
        return
    handle = getattr(proc, "_pdt_log_handle", None)
    pgid = getattr(proc, "_pdt_pgid", None)
    try:
        if proc.poll() is None:
            if pgid is not None:
                try:
                    os.killpg(int(pgid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.terminate()
            else:  # pragma: no cover - the group is always recorded
                proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
    finally:
        # The group goes last and goes always: nothing in it may
        # outlive this case.
        _kill_group(proc)
        if handle is not None:
            try:
                handle.close()
            except OSError:  # pragma: no cover - the handle is ours
                pass


def _driver_env() -> Dict[str, str]:
    """The environment the browser driver runs under.

    ``$HOME`` is pointed back at the home the run was launched with (see
    ``PDT_TEST_REAL_HOME``) so that Playwright can find the browser
    binaries it installed there.
    """
    env = dict(os.environ)
    real_home = os.environ.get("PDT_TEST_REAL_HOME")
    if real_home:
        env["HOME"] = real_home
    return env


def _playwright_interpreter() -> Optional[str]:
    """An interpreter that can import ``playwright``, or ``None``.

    The candidates, in order: the interpreter running this suite (the
    virtualenv the test command uses), this checkout's own
    ``backend/.venv``, and the interpreter behind a ``playwright``
    command on ``PATH`` — the command is a Python entry point, so its
    shebang names an interpreter that has the package installed.
    Nothing is hard-coded: every candidate is read out of the
    environment or out of this file's own location.
    """
    candidates = [sys.executable]

    if _VENV_PYTHON.exists():
        candidates.append(str(_VENV_PYTHON))

    command = shutil.which("playwright")
    if command:
        try:
            first_line = Path(command).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[0]
        except (OSError, IndexError):
            first_line = ""
        if first_line.startswith("#!"):
            # ``#!/usr/bin/env python3`` names the interpreter in the
            # argument after ``env``; ``#!/path/to/python3`` names it in
            # the shebang itself.
            parts = shlex.split(first_line[2:].strip())
            interpreter = ""
            if parts:
                if os.path.basename(parts[0]) == "env" and len(parts) > 1:
                    interpreter = parts[1]
                else:
                    interpreter = parts[0]
            if "python" in interpreter:
                candidates.append(interpreter)

    for candidate in candidates:
        try:
            probe = subprocess.run(
                [candidate, "-c", "import playwright.sync_api"],
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return candidate
    return None


# ---------------------------------------------------------------------------
# The seed
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    """Write one JSON document, the way the product writes its own."""
    path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _seed_failure(plans_dir: Path) -> Path:
    """Write everything a plan that really failed leaves behind.

    Three artefacts, and the announcement reads all of them: the
    postprocess progress (empty — the finishing stage got nowhere), the
    verification report (its ``summary`` is what went wrong, and its
    ``overall_status`` says the round failed) and the repair tasks (the
    title of the task that is still owed is what should happen about
    it).
    """
    plan_dir = plans_dir / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    _write_json(plan_dir / "postprocess_progress.json", {})
    _write_json(
        plan_dir / "verification_report.json",
        {"summary": PROBLEM, "overall_status": "failed"},
    )
    _write_json(
        plan_dir / "verification_repair_tasks.json",
        {"tasks": [{"id": "R-1", "title": "修复登录接口"}]},
    )
    return plan_dir


def _seed_database(state_db: Path) -> None:
    """Migrate the state database and put the plan in the failed phase.

    The decision table is migrated by the store itself (the table is
    created on first use), and the routing row is the plan's own
    answer to "where is this plan" — ``failed`` is where a plan that
    ended in failure is.
    """
    with HitlStore(state_db):
        pass

    conn = open_db(state_db)
    try:
        migrate(conn)
        RoutingRepository(conn).insert(PLAN_ID, "failed")
    finally:
        conn.close()


def _write_scheduler_settings(path: Path) -> None:
    """Write the scheduler's own settings.

    The scheduler is **enabled** — the panel's cards come from its read
    model — and its capacity is **zero**: "run nothing".  A plan that
    waits in the queue is a plan the operator can open a card for, and
    a plan that never runs is a plan that cannot call a model.
    """
    path.write_text(
        "scheduler:\n  enabled: true\n  dry_run: false\n  capacity: 0\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# The browser driver
# ---------------------------------------------------------------------------

#: The driver runs in its own process: Playwright's synchronous API
#: wants the event loop it was started with, and a test process that
#: has been running for a while is not a good place to start one.
_BROWSER_DRIVER = r'''
"""Open the panel, answer the failure review, and open the plan's card."""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BROWSER_TIMEOUT_MS = __BROWSER_TIMEOUT_MS__


def browser_executable():
    """A Chromium already installed under the Playwright cache.

    Playwright resolves the browser it launches from its own cache, and
    a driver whose expected revision is not the one installed cannot
    launch anything.  The binaries that *are* installed are found here
    instead -- nothing is downloaded, and nothing is hard-coded: the
    cache lives under the home the run was launched with.
    """
    home = Path.home()
    roots = [
        home / "Library" / "Caches" / "ms-playwright",
        home / ".cache" / "ms-playwright",
    ]
    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chromium-*/chrome-linux*/chrome",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern)):
                if candidate.exists():
                    return str(candidate)
    return None


def run(url, plan_id, answer, waiting_shot, detail_shot):
    waiting_row = "tr[data-plan-id='{0}']".format(plan_id)
    answer_input = ".scheduler-waiting-answer[data-plan='{0}']".format(plan_id)
    answer_button = "button[data-action='answer'][data-plan='{0}']".format(plan_id)
    card = ".scheduler-plan-card[data-plan-id='{0}']".format(plan_id)
    card_header = card + " .scheduler-plan-header"
    detail = "details.scheduler-failure-review, details[data-section='failure_review']"
    start_button = ("#scheduler-terminal-list "
                    "button[data-action='start'][data-plan='{0}']".format(plan_id))

    payload = {"executable": browser_executable()}

    launch_kwargs = {}
    if payload["executable"]:
        launch_kwargs["executable_path"] = payload["executable"]

    try:
        with sync_playwright() as driver:
            browser = driver.chromium.launch(**launch_kwargs)
            try:
                page = browser.new_page()
                try:
                    page.goto(url, wait_until="load")

                    # The panel is behind a button; the operator opens it.
                    page.wait_for_selector(
                        "#btn-open-scheduler", timeout=BROWSER_TIMEOUT_MS
                    )
                    page.click("#btn-open-scheduler")
                    page.wait_for_selector(
                        "#scheduler-view", state="visible", timeout=BROWSER_TIMEOUT_MS
                    )

                    # The read model, straight from the panel's own api().
                    # It is read here and not later because a page that
                    # shows nothing must say what the server answered.
                    payload["state"] = page.evaluate(
                        """async () => await api("/scheduler/state")"""
                    )

                    # 1. The waiting row: the question the operator is asked.
                    page.wait_for_selector(waiting_row, timeout=BROWSER_TIMEOUT_MS)
                    payload["waiting_row_text"] = page.eval_on_selector(
                        waiting_row, "el => el.textContent"
                    )
                    payload["waiting_row_buttons"] = page.eval_on_selector_all(
                        waiting_row + " button",
                        "els => els.map(el => el.textContent.trim())",
                    )
                    payload["waiting_row_inputs"] = page.eval_on_selector_all(
                        waiting_row + " .scheduler-waiting-answer", "els => els.length"
                    )
                    page.screenshot(path=waiting_shot, full_page=True)

                    # 2. The answer: typed into the row, then sent.
                    page.fill(answer_input, answer)
                    payload["answer_input_value"] = page.eval_on_selector(
                        answer_input, "el => el.value"
                    )
                    page.click(answer_button)
                    page.wait_for_function(
                        "(sel) => !document.querySelector(sel)",
                        arg=waiting_row,
                        timeout=BROWSER_TIMEOUT_MS,
                    )
                    payload["waiting_row_after_answer"] = (
                        lambda row: row.text_content() if row else ""
                    )(page.query_selector(waiting_row))

                    # 3. The decision, read through the page's own api().
                    payload["decisions"] = page.evaluate(
                        """async (planId) => {
                            const list = await api(
                                `/hitl/${encodeURIComponent(planId)}/decisions`
                            );
                            return list.map((d) => ({
                                decision_id: d.decision_id,
                                source: d.source,
                                status: d.status,
                                answer: d.answer,
                            }));
                        }""",
                        plan_id,
                    )

                    # 4. A failed plan is drawn in the terminal list, not
                    #    in a card: bring it back the way the operator
                    #    does, so that the plan's own card is there to
                    #    be opened.
                    page.wait_for_selector(start_button, timeout=BROWSER_TIMEOUT_MS)
                    page.click(start_button)
                    page.wait_for_selector(card, timeout=BROWSER_TIMEOUT_MS)
                    page.click(card_header)
                    page.wait_for_selector(detail, timeout=BROWSER_TIMEOUT_MS)
                    payload["detail_text"] = page.eval_on_selector(
                        detail, "el => el.textContent"
                    )
                    payload["detail_visible"] = page.is_visible(detail)
                    payload["detail_paragraphs"] = page.eval_on_selector_all(
                        detail + " .scheduler-detail-text",
                        "els => els.map(el => el.textContent.trim())",
                    )
                    payload["detail_marker"] = page.eval_on_selector(
                        detail,
                        """el => JSON.stringify({
                            tag: el.tagName,
                            section: el.getAttribute("data-section"),
                            cls: el.className || "",
                        })""",
                    )
                    payload["card_text"] = page.eval_on_selector(
                        card, "el => el.textContent"
                    )
                    page.screenshot(path=detail_shot, full_page=True)

                    # 5. The read-only endpoint, asked the way the page
                    #    asks: with the guard header the backend demands.
                    payload["review"] = page.evaluate(
                        """async (planId) => {
                            const headers = (typeof REQUEST_GUARD_HEADERS !== "undefined")
                                ? REQUEST_GUARD_HEADERS
                                : {"X-PDT-Request": "1"};
                            const res = await fetch(
                                `/api/plan/${encodeURIComponent(planId)}/failure-review`,
                                {headers: {...headers, "Content-Type": "application/json"}},
                            );
                            let body = null;
                            try { body = await res.json(); } catch (err) { body = null; }
                            return {status: res.status, body: body};
                        }""",
                        plan_id,
                    )
                except Exception:
                    # The page as it stood when the step failed: the
                    # evidence a reader needs to say which wait ran out.
                    try:
                        payload["page_html"] = page.content()[:30000]
                    except Exception:
                        pass
                    raise
            finally:
                browser.close()
    except Exception as exc:
        # The reason travels out on stderr: a page that never showed the
        # plan has to say which wait ran out.
        payload["error"] = type(exc).__name__
        payload["detail"] = str(exc)
        print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    return payload


payload = run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
print(json.dumps(payload, ensure_ascii=False))
'''


def _run_driver(
    argv: List[str], timeout: float
) -> Tuple[int, str, str]:
    """Run the browser driver in a process of its own.

    The driver is given a session of its own, so a driver that has to
    be killed takes the browser it started with it: a browser left
    running is an orphan, and an orphan is a process nobody reaps.
    """
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_driver_env(),
        start_new_session=True,
    )
    proc._pdt_pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined]
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        return 124, stdout, stderr + f"\n(the driver timed out after {timeout}s)"
    finally:
        # Nothing the driver started may outlive it: a browser that
        # survived its driver would be an orphan nobody reaps.
        _kill_group(proc)
    return proc.returncode, stdout, stderr


# ---------------------------------------------------------------------------
# The case
# ---------------------------------------------------------------------------


def test_failure_review_hitl_end_to_end(tmp_path, monkeypatch) -> None:
    """A failed plan is announced, and a human answers it on the panel.

    The chain, in one case: the terminal write announces the failure,
    the sidecar is written, the panel shows the question, the answer
    settles the decision, and the plan's own card carries the same
    paragraph the announcement wrote.
    """
    # The paths live **inside** ``tmp_path`` and are the very ones the
    # suite's own ``isolated_plans_dir`` fixture redirects the backend
    # to (``tmp_path/plans`` and ``tmp_path/state.db``).  That is not
    # tidiness: the fixture points ``server._state_db_path`` and
    # ``server.PLANS_DIR`` at those two, and the environment variables
    # below point the *other* resolvers (``plan_state``,
    # ``failure_review``, the scheduler's routes) at the same two.  A
    # seed that wrote anywhere else would leave the server reading a
    # different database than the one the seed wrote — the plan would
    # be announced into a store the panel never looks at.
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir(parents=True)
    state_db = tmp_path / "state.db"

    shots_dir = tmp_path / "failure-review-shots"
    shots_dir.mkdir()
    waiting_shot = shots_dir / "waiting.png"
    detail_shot = shots_dir / "detail.png"

    scheduler_settings = tmp_path / "scheduler.yaml"
    _write_scheduler_settings(scheduler_settings)

    # The environment the seed and the server have to agree on.  It is
    # set **before** the seed: the seed writes into the same tree the
    # server will read.
    monkeypatch.setenv("PDT_PLANS_DIR", str(plans_dir))
    monkeypatch.setenv("PDT_STATE_DB_PATH", str(state_db))
    monkeypatch.setenv("PDT_SCHEDULER_FILE", str(scheduler_settings))

    plan_dir = _seed_failure(plans_dir)
    _seed_database(state_db)

    import server as server_mod

    # The server's own plans root is resolved when the module is
    # imported; the test points it at the tree the seed wrote.
    monkeypatch.setattr(server_mod, "PLANS_DIR", plans_dir)

    # The trigger: the terminal write the verification loop makes when
    # the chain really ends.  This is the product's own hook — the
    # announcement is one link of this chain, and the chain is what is
    # under test.
    server_mod._persist_verification_terminal(
        PLAN_ID, "failed", "e2e", chain_ending=True
    )

    sidecar = plan_dir / "failure_review.json"
    assert sidecar.exists(), (
        f"the terminal write announced nothing: no sidecar at {sidecar}"
    )
    record = json.loads(sidecar.read_text(encoding="utf-8"))
    assert record.get("problem") == PROBLEM, (
        f"the sidecar's problem is {record.get('problem')!r}, "
        f"not {PROBLEM!r}: {record}"
    )
    assert record.get("suggestion") == SUGGESTION, (
        f"the sidecar's suggestion is {record.get('suggestion')!r}, "
        f"not {SUGGESTION!r}: {record}"
    )
    for field in ("decision_id", "problem", "suggestion", "created_at"):
        assert record.get(field), (
            f"the sidecar carries no {field}: {record}"
        )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / f"server-{port}.log"
    proc = _boot(plans_dir, state_db, scheduler_settings, log_path, port)
    try:
        _wait_ready(base_url, proc)

        interpreter = _playwright_interpreter()
        if interpreter is None:
            pytest.fail(
                "no Playwright driver is installed in this environment, so "
                "the page cannot be driven and the failure review cannot "
                "be seen; install one with `python -m playwright install "
                "chromium` and re-run this case"
            )

        driver_script = tmp_path / "drive_failure_review.py"
        driver_script.write_text(
            _BROWSER_DRIVER.replace(
                "__BROWSER_TIMEOUT_MS__", str(BROWSER_TIMEOUT_MS)
            ),
            encoding="utf-8",
        )

        returncode, stdout, stderr = _run_driver(
            [
                interpreter,
                str(driver_script),
                base_url,
                PLAN_ID,
                ANSWER,
                str(waiting_shot),
                str(detail_shot),
            ],
            DRIVER_TIMEOUT_SECONDS,
        )

        context = (
            f"\nscreenshots: {waiting_shot}, {detail_shot}"
            f"\nserver log: {log_path}"
            f"\ndriver stdout:\n{stdout}"
            f"\ndriver stderr:\n{stderr}"
        )
        assert returncode == 0, (
            f"the browser driver failed (exit {returncode}){context}"
        )

        payload = json.loads(stdout.strip().splitlines()[-1])

        # 1. The waiting row shows the question, and offers the answer
        #    and nothing else.
        waiting_text = str(payload.get("waiting_row_text") or "")
        assert PROBLEM in waiting_text, (
            f"the waiting row does not carry the problem {PROBLEM!r}: "
            f"{waiting_text!r}{context}"
        )
        assert SUGGESTION in waiting_text, (
            f"the waiting row does not carry the suggestion {SUGGESTION!r}: "
            f"{waiting_text!r}{context}"
        )
        buttons = [str(label) for label in (payload.get("waiting_row_buttons") or [])]
        assert not any("延长" in label for label in buttons), (
            f"the waiting row offers 延长: {buttons!r}{context}"
        )
        assert not any("终止" in label for label in buttons), (
            f"the waiting row offers 终止: {buttons!r}{context}"
        )
        assert int(payload.get("waiting_row_inputs") or 0) == 1, (
            "the waiting row does not offer an answer box: "
            f"{buttons!r}{context}"
        )
        assert payload.get("answer_input_value") == ANSWER, (
            "the answer box did not take the answer: "
            f"{payload.get('answer_input_value')!r}{context}"
        )

        # 2. The answer settled the decision, and the row is gone.
        assert not payload.get("waiting_row_after_answer"), (
            "the waiting row is still on the page after the answer: "
            f"{payload.get('waiting_row_after_answer')!r}{context}"
        )
        decisions = payload.get("decisions") or []
        reviews = [
            decision
            for decision in decisions
            if isinstance(decision, dict)
            and decision.get("source") == "failure_review"
        ]
        assert len(reviews) == 1, (
            f"the plan carries {len(reviews)} failure-review decisions: "
            f"{decisions!r}{context}"
        )
        assert reviews[0].get("status") == "answered", (
            f"the failure-review decision is "
            f"{reviews[0].get('status')!r}, not 'answered': "
            f"{reviews[0]!r}{context}"
        )
        assert reviews[0].get("decision_id") == record.get("decision_id"), (
            "the decision the panel answered is not the one the "
            f"announcement made: {reviews[0]!r} vs {record!r}{context}"
        )

        # 3. The plan's own card carries the same paragraph.
        detail_text = str(payload.get("detail_text") or "")
        assert PROBLEM in detail_text, (
            f"the plan's card does not carry the problem {PROBLEM!r}: "
            f"{detail_text!r}{context}"
        )
        assert SUGGESTION in detail_text, (
            f"the plan's card does not carry the suggestion {SUGGESTION!r}: "
            f"{detail_text!r}{context}"
        )
        # The block that carries the paragraph has to be the failure
        # review's own block, not some other collapsible section of the
        # card that happens to say the same words.
        marker = json.loads(str(payload.get("detail_marker") or "{}"))
        assert marker.get("section") == "failure_review" or (
            "scheduler-failure-review" in str(marker.get("cls") or "")
        ), (
            "the card's block is not the failure review's block: "
            f"{marker!r}{context}"
        )
        # And it has to be **on the card**: a block that is in the
        # document but hidden is not a block the operator ever sees.
        assert payload.get("detail_visible") is True, (
            "the failure review's block is not visible on the card: "
            f"{marker!r}{context}"
        )
        # The block says what went wrong and what to do about it, in
        # two paragraphs of its own.
        paragraphs = [
            str(text) for text in (payload.get("detail_paragraphs") or [])
        ]
        assert any(PROBLEM in text for text in paragraphs), (
            f"no paragraph of the block carries the problem {PROBLEM!r}: "
            f"{paragraphs!r}{context}"
        )
        assert any(SUGGESTION in text for text in paragraphs), (
            f"no paragraph of the block carries the suggestion "
            f"{SUGGESTION!r}: {paragraphs!r}{context}"
        )

        # 4. The read-only endpoint answers with the four fields.
        review = payload.get("review") or {}
        assert int(review.get("status") or 0) == 200, (
            f"the failure-review endpoint answered "
            f"{review.get('status')!r}, not 200: {review!r}{context}"
        )
        body = review.get("body") or {}
        for field in ("decision_id", "problem", "suggestion", "created_at"):
            assert body.get(field), (
                f"the failure-review endpoint carries no {field}: "
                f"{body!r}{context}"
            )
        assert body.get("problem") == PROBLEM, (
            f"the endpoint's problem is {body.get('problem')!r}, not "
            f"{PROBLEM!r}: {body!r}{context}"
        )
        assert body.get("suggestion") == SUGGESTION, (
            f"the endpoint's suggestion is {body.get('suggestion')!r}, not "
            f"{SUGGESTION!r}: {body!r}{context}"
        )

        # 5. The two screenshots are on disk, and they are not empty.
        for shot in (waiting_shot, detail_shot):
            assert shot.exists(), (
                f"the screenshot was not written: {shot}{context}"
            )
            assert shot.stat().st_size > 0, (
                f"the screenshot is empty: {shot}{context}"
            )
    finally:
        _teardown(proc)
        assert proc.poll() is not None, (
            f"the backend was left running (pid {proc.pid})"
        )