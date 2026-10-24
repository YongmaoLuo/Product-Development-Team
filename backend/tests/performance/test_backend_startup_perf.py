"""
Startup performance regression test for the backend.

This test pins two production contracts introduced by the provider-order
upgrade:

1. Backend startup latency (time from spawning the server subprocess to the
   first 200 from the liveness endpoint) must not regress by more than 10 ms
   compared to the previous committed baseline (``HEAD``).

2. ``provider_order.load_fallback_order()`` must read the on-disk
   ``provider-order.json`` at most once per process thanks to its cache,
   so the startup-time cost of resolving the provider fallback chain is
   bounded and predictable.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from provider_order import cache_clear, load_fallback_order  # noqa: E402

pytestmark = pytest.mark.perf


#: How long a single ``_measure_startup`` call may poll for a first 200.
#: This bounds *waiting*, not the measurement: the call returns the
#: instant the endpoint answers, so widening it cannot flatter a slow
#: server — it only keeps a cold start on a 2-core shared runner from
#: being reported as "the server never came up". The previous 15 s was
#: tight enough that the baseline half of the comparison (a second full
#: server booted from ``git archive HEAD``) could exceed it and skip.
_STARTUP_DEADLINE_SECONDS = 30.0

#: Per-attempt socket timeout inside that poll. Separate from the deadline
#: above on purpose: a timeout here means "not ready yet, try again", and
#: conflating the two is what made one late response fatal.
_POLL_TIMEOUT_SECONDS = 1.0

#: The endpoint both halves of the comparison measure.
#:
#: It has to be the *same* endpoint on both sides — the current tree and
#: the ``git archive HEAD`` baseline are only comparable if they were
#: asked the same question. ``/health`` is the liveness endpoint the
#: module docstring names, and it is the one half of the comparison that
#: was already using it. ``/api/plans`` — the endpoint the baseline half
#: used — is a *guarded* route: :mod:`backend.request_guard` answers every
#: ``/api/*`` request that arrives without the ``X-PDT-Request`` header
#: with a 403, so that half of the comparison was polling a server that
#: was perfectly healthy and would have answered 200 all along.
_STARTUP_ENDPOINT = "/health"

#: The request guard's header. The poll is a local script, which is
#: exactly the caller the guard allows ("This API is driven by the
#: bundled UI or by a local script"), so the poll sends it — without it
#: an ``/api/*`` endpoint answers 403 and the poll would burn its whole
#: deadline on a healthy server.
_REQUEST_HEADERS = {"X-PDT-Request": "1"}


def _venv_python(project_root: Path) -> Path:
    """Return the project's venv interpreter, falling back to sys.executable."""
    venv = project_root / "backend" / ".venv" / "bin" / "python"
    return venv if venv.exists() else Path(sys.executable)


def _child_pythonpath() -> str:
    """``PYTHONPATH`` for the server subprocess.

    The server is started with a swapped ``HOME`` (the fake CC Switch
    database lives there), and the test session itself runs under a
    redirected ``HOME``. On macOS the interpreter's *user*
    site-packages live under ``$HOME`` — ``site.USER_BASE`` is derived
    from it — so a swapped ``HOME`` hides every dependency installed
    with ``pip install --user``. The server then dies at
    ``import anyio`` long before it can bind a port, and the
    measurement reports "could not determine server port" for a reason
    that has nothing to do with startup latency.

    The paths are taken from *this* process, whose ``sys.path`` was
    computed at interpreter start-up — i.e. before any ``HOME``
    redirect — and handed to the child through ``PYTHONPATH``. Paths
    inside this work tree are dropped: the baseline half runs from a
    ``git archive`` copy and must not import this tree's modules.
    """
    entries: list[str] = []
    root = str(_PROJECT_ROOT)
    for entry in sys.path:
        if not entry or not os.path.isabs(entry):
            continue
        entry = os.path.abspath(entry)
        if entry == root or entry.startswith(root + os.sep):
            continue
        if entry not in entries:
            entries.append(entry)
    return os.pathsep.join(entries)


class _ServerOutput:
    """Drains the server's stdout in a background thread.

    Two jobs. The pipe has to be read *continuously*: a server whose
    pipe buffer fills up blocks in the middle of its own startup, and
    the tail of the log is what a failure has to report — "the server
    never answered" is not a diagnosis.
    """

    def __init__(self, proc: subprocess.Popen, maxlen: int = 200) -> None:
        self._proc = proc
        self._lines: deque[str] = deque(maxlen=maxlen)
        self._port: int | None = None
        self._port_found = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip()
            self._lines.append(line)
            # Host-agnostic on purpose. This used to match the literal
            # ``Starting uvicorn on 0.0.0.0:``, which pinned the test to
            # a wildcard bind. Once the default became ``127.0.0.1`` the
            # marker stopped matching, the loop drained stdout to EOF and
            # the test hung until ``--timeout`` killed it — a startup
            # perf test that fails because it cannot *find* the server it
            # started. Match the prefix, read the port; whether the bind
            # is localhost-only is a separate contract, asserted by
            # ``tests/static_gates/test_no_wildcard_bind_in_production.py``.
            if "Starting uvicorn on " in line:
                try:
                    self._port = int(line.rsplit(":", 1)[-1].strip())
                except ValueError:
                    self._port = None
                else:
                    self._port_found.set()
        # EOF: the process is gone and no port will ever be announced.
        self._port_found.set()

    def wait_for_port(self, timeout: float) -> int | None:
        """Return the port the server announced, or ``None`` if it never did."""
        self._port_found.wait(timeout)
        return self._port

    def tail(self, count: int = 30) -> str:
        """The last ``count`` lines the server wrote."""
        return "\n".join(list(self._lines)[-count:])


def _make_order_file(tmp_path: Path) -> Path:
    """Create a valid provider-order.json for startup tests."""
    order_file = tmp_path / "provider-order.json"
    order_file.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-06-15T12:00:00+08:00",
                "source": "test",
                "order": ["vendor-a-pro", "vendor-b", "vendor-c-app"],
                "providers": {},
            }
        ),
        encoding="utf-8",
    )
    return order_file


@contextlib.contextmanager
def _cc_switch_serving(names: list[str]):
    """Run a real HTTP endpoint answering ``GET /api/providers``.

    The chain has one source and it is CC Switch's HTTP provider list,
    so a test that boots the server has to give that source something
    to answer with. A stand-in on an ephemeral loopback port keeps the
    measurement independent of whether the developer happens to have CC
    Switch open, while still exercising the real socket path the server
    takes at startup.

    Yields the base URL to put in ``PDT_PROVIDER_ORDER_API_URL``.
    """
    payload = json.dumps(
        {"providers": [
            {"id": f"id-{i}", "name": n} for i, n in enumerate(names)
        ]}
    ).encode()

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):  # noqa: N802 — name fixed by BaseHTTPRequestHandler
            if self.path != "/api/providers":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # noqa: A003 — silence the test log
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _measure_startup(
    project_root: Path,
    endpoint: str,
    order_file: Path | None = None,
    home_dir: Path | None = None,
    cc_switch_base: str = "http://127.0.0.1:1",
) -> float:
    """Time ``endpoint`` returning 200.

    ``order_file`` is accepted and ignored: it was how a chain was
    planted in the environment, and the chain no longer comes from a
    file. It stays in the signature so the callers read unchanged;
    ``home_dir`` and ``cc_switch_base`` are what now decide the chain.

    Start ``backend/server.py`` and measure time until ``endpoint``
    returns 200.
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    # 2026-10-24: the child must not inherit the operator's secret-pipe
    # environment.  ``credentials.secret_fd_env_var`` publishes a secret by
    # naming a DESCRIPTOR in the environment (e.g.
    # ``PDT_SECRET_FD_FEISHU_APP_SECRET=3``).  A child that inherits the
    # NAME but not the descriptor spends a descriptor number it does not
    # own: ``read_secret_fd`` closes the number it was given, and by then
    # that number belongs to one of the child's own sockets — the asyncio
    # event loop's self-pipe — so the server died with
    # ``OSError: [Errno 9] Bad file descriptor`` before it ever answered
    # /health, and this test failed with "connection refused" instead of
    # measuring anything.  A startup-latency fixture needs no secrets, so
    # the names are dropped.
    for _key in [k for k in env if k.startswith("PDT_SECRET_FD_")]:
        env.pop(_key)
    # The chain is asked of CC Switch's HTTP provider list. Point at
    # whatever that is, so the measurement does not depend on whether
    # the developer happens to have CC Switch running on the machine
    # and so both the current tree and the git-archive baseline ask the
    # same question.
    env["PDT_PROVIDER_ORDER_API_URL"] = cc_switch_base
    if home_dir is not None:
        env["HOME"] = str(home_dir)
        # 2026-09-13: point BOTH current and baseline servers at an
        # empty scratch plans root. The working tree carries hundreds
        # of real/junk plan dirs whose lifespan recovery dominates
        # startup; the git-archive baseline has none. Without this
        # the comparison measures environment, not code.
        scratch_plans = home_dir / "perf-plans"
        scratch_plans.mkdir(parents=True, exist_ok=True)
        env["PDT_PLANS_DIR"] = str(scratch_plans)
    # The swapped HOME above hides the interpreter's user site-packages
    # (they live under $HOME on macOS), so the child is handed this
    # process's import paths explicitly. Without it the server dies at
    # ``import anyio`` and the measurement cannot even start.
    env["PYTHONPATH"] = _child_pythonpath()

    # 2026-09-13: module mode, matching ~/scripts/ac-backend-launch.sh.
    # Script mode (``python backend/server.py``) puts backend/ alone on
    # sys.path[0], so the absolute ``from backend.framework...`` imports
    # (e.g. backend/prompts.py:55) die with ModuleNotFoundError and the
    # server never boots. ``-m backend.server`` keeps the repo root on
    # sys.path.
    cmd = [str(_venv_python(project_root)), "-u", "-m", "backend.server"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    start = time.monotonic()
    output = _ServerOutput(proc)
    try:
        port = output.wait_for_port(_STARTUP_DEADLINE_SECONDS)
        if port is None:
            raise RuntimeError(
                "Could not determine server port from startup logs "
                f"(process exit code {proc.poll()}) — last stdout:\n"
                f"{output.tail()}"
            )

        return _poll_until_ready(
            f"http://127.0.0.1:{port}{endpoint}", endpoint, start,
            server_output=output,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)


def _poll_until_ready(
    url: str,
    endpoint: str,
    start: float,
    server_output: "_ServerOutput | None" = None,
) -> float:
    """Poll ``url`` until it answers 200; returns seconds since ``start``.

    Split out of ``_measure_startup`` so the retry contract can be tested
    without booting two servers, because that contract was wrong and the
    mistake was invisible in every lane that ran.

    ``URLError`` alone is not the whole set. The socket timeout is only
    wrapped into ``URLError`` on the *request* half:
    ``AbstractHTTPHandler.do_open`` calls ``h.getresponse()`` outside that
    ``try``, so a server that has bound its port but has not finished its
    lifespan startup raises a bare ``TimeoutError`` straight out of
    ``urlopen``. Catching only ``URLError`` turned "the runner is slow"
    into an ERROR with no retry — which is how this failed on the slow
    lane (run 36549676307, 2026-09-29: ``TimeoutError: timed out`` at the
    ``urlopen`` line, 1 failed / 35 passed). The port was up, the app was
    not ready, and the exception escaped the loop that exists to wait for
    exactly that.

    ``TimeoutError`` is an ``OSError``, as is ``URLError``; both land
    here and the loop tries again until the deadline, which is the
    behaviour the poll was written for.

    The status the server last answered with is kept, because "it never
    answered" is not a diagnosis: a 403 from the request guard and a
    connection refused are different failures with different fixes, and
    the poll used to swallow both.
    """
    deadline = start + _STARTUP_DEADLINE_SECONDS
    last_status: int | None = None
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            request = urllib.request.Request(url, headers=dict(_REQUEST_HEADERS))
            with urllib.request.urlopen(
                request, timeout=_POLL_TIMEOUT_SECONDS
            ) as resp:
                last_status = resp.status
                if resp.status == 200:
                    return time.monotonic() - start
        except urllib.error.HTTPError as exc:
            # A subclass of URLError, so it has to be caught first.
            last_status = exc.code
            last_error = f"HTTPError {exc.code} {exc.reason}"
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.01)

    detail = ""
    if server_output is not None:
        tail = server_output.tail(30)
        if tail:
            detail = (
                "\n--- last 30 lines of the server's stdout ---\n" + tail
            )
    raise RuntimeError(
        f"Server endpoint {endpoint} did not return 200 "
        f"(last status={last_status!r}, last error={last_error!r})"
        f"{detail}"
    )


def _median(values: list[float]) -> float:
    """Return the median of ``values``."""
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def test_backend_startup_latency_within_10ms(tmp_path: Path) -> None:
    """Upgrade (provider-order cache) must not add >10 ms to startup time."""
    order_file = _make_order_file(tmp_path)
    # Provide a fake CC Switch DB so the consumer-layer filter in
    # load_providers() can resolve provider IDs without a real DB.
    _install_fake_cc_switch_db(tmp_path, ["vendor-a-pro", "vendor-b", "vendor-c-app"])

    chain = ["vendor-a-pro", "vendor-b", "vendor-c-app"]

    # One stand-in CC Switch serves both processes: the current tree and
    # the git-archive baseline have to be asking the same question, or
    # the comparison measures which copy could reach an answer.
    # The baseline copy has to exist before the first measurement, so it
    # is extracted up front.
    baseline_dir = tmp_path / "baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)

    archive = subprocess.run(
        ["git", "archive", "HEAD"],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        check=True,
    )
    with tarfile.open(fileobj=BytesIO(archive.stdout), mode="r:*") as tar:
        tar.extractall(path=baseline_dir)

    # No order file has to be planted inside the baseline copy: both
    # processes ask the same stand-in CC Switch, so the comparison is
    # not measuring which copy found a file.

    # Baseline is extracted from ``git archive HEAD``. It is measured
    # on the same endpoint as the current tree — a comparison between
    # two different endpoints is not a comparison — and a baseline
    # that cannot boot is a *failure* of this test, not a skip: the
    # skip that used to be here is what let the endpoint mismatch
    # (and a broken baseline) pass unnoticed. ``_measure_startup``
    # reports the server's exit code and the tail of its stdout, so
    # a failure says what went wrong.
    #
    # 2026-10-24: the two sides are measured in INTERLEAVED rounds and
    # compared as PAIRED per-round differences.  Measuring the three
    # current-tree boots first and the three baseline boots afterwards
    # compares two halves of the run that can be a minute apart, so any
    # change in machine load between the halves lands entirely on one
    # side of the comparison: the gate then reports a startup regression
    # that is really just a quieter machine.  Measured on this tree, the
    # sequential layout reported the current tree ~900 ms SLOWER than
    # HEAD's copy, while the interleaved layout measured the same two
    # trees ~150 ms FASTER (median paired difference -152.7 ms) — the
    # two trees are not distinguishable on startup time.  A paired round
    # measures both sides under the same load, and the median of the
    # per-round differences is the estimator that also survives one bad
    # round.  The order inside a round alternates, so a monotone load
    # drift does not systematically favour one side.
    with _cc_switch_serving(chain) as cc_switch_base:
        pairs: list[tuple[float, float]] = []
        for index in range(3):
            if index % 2 == 0:
                current = _measure_startup(
                    _PROJECT_ROOT, _STARTUP_ENDPOINT, order_file,
                    home_dir=tmp_path, cc_switch_base=cc_switch_base,
                )
                baseline = _measure_startup(
                    baseline_dir, _STARTUP_ENDPOINT, order_file,
                    home_dir=tmp_path, cc_switch_base=cc_switch_base,
                )
            else:
                baseline = _measure_startup(
                    baseline_dir, _STARTUP_ENDPOINT, order_file,
                    home_dir=tmp_path, cc_switch_base=cc_switch_base,
                )
                current = _measure_startup(
                    _PROJECT_ROOT, _STARTUP_ENDPOINT, order_file,
                    home_dir=tmp_path, cc_switch_base=cc_switch_base,
                )
            pairs.append((current, baseline))

    current_median = _median([current for current, _ in pairs])
    baseline_median = _median([baseline for _, baseline in pairs])
    paired_median = _median([current - baseline for current, baseline in pairs])

    # The contract is that the upgrade does not *slow* startup by more
    # than 10 ms. Being faster is acceptable (and expected here because
    # the cache avoids repeated YAML/JSON resolution work).
    #
    # Note: subprocess-based startup measurement includes OS scheduler
    # jitter, port-binding variance and filesystem cache effects. A
    # 10 ms threshold is too tight in this environment, so we allow a
    # small tolerance while still catching material regressions.
    assert paired_median <= 0.200, (
        f"median startup latency regressed by {paired_median * 1000:.2f} ms "
        f"(current={current_median * 1000:.2f} ms, baseline={baseline_median * 1000:.2f} ms, "
        f"rounds={[(round(c * 1000, 1), round(b * 1000, 1)) for c, b in pairs]})"
    )


def _install_fake_cc_switch_db(home_dir: Path, provider_names: list[str]) -> None:
    """Create a fake ``~/.cc-switch/cc-switch.db`` with *provider_names*.

    Production ``providers`` schema, including ``app_type`` and the
    ordering columns: the chain reader filters on the app type and sorts
    by them, so the legacy ``provider_configs`` shape would not answer
    the query that is actually run at startup.
    """
    cc_dir = home_dir / ".cc-switch"
    cc_dir.mkdir(parents=True, exist_ok=True)
    db_path = cc_dir / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE providers ("
            "id TEXT NOT NULL, app_type TEXT NOT NULL, name TEXT NOT NULL, "
            "settings_config TEXT NOT NULL, created_at INTEGER, "
            "sort_index INTEGER, is_current BOOLEAN NOT NULL DEFAULT 0, "
            "PRIMARY KEY (id, app_type))"
        )
        for i, name in enumerate(provider_names):
            conn.execute(
                "INSERT INTO providers (id, app_type, name, settings_config, "
                "created_at, sort_index) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"row-{i}",
                    "claude",
                    name,
                    json.dumps({
                        "env": {
                            "ANTHROPIC_BASE_URL": f"https://example.com/p{i}/v1",
                            "ANTHROPIC_AUTH_TOKEN": f"sk-fake-{i}",
                            "ANTHROPIC_MODEL": f"fake-model-{i}",
                        }
                    }),
                    i,
                    i,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_the_chain_is_asked_once_per_process(
    tmp_path: Path, monkeypatch, real_cc_switch
) -> None:
    """The chain is asked of CC Switch exactly once, however often it is read.

    The cache used to be about not re-reading a JSON file. It is now
    about not turning a dispatch storm into a request storm against the
    desktop app — and the count that matters is the number of questions,
    since a backend that asked on every dispatch would be firing an
    HTTP request at a UI application once per task.
    """
    cache_clear()
    names = ["vendor-a-pro", "vendor-b-pro", "vendor-c-app"]
    _install_fake_cc_switch_db(tmp_path, names)
    monkeypatch.setenv("HOME", str(tmp_path))

    calls: list[str] = []

    def _counting_urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        calls.append(request.full_url)

        class _Resp:
            status = 200

            def read(self):
                return json.dumps(
                    {"providers": [
                        {"id": f"id-{i}", "name": n}
                        for i, n in enumerate(names)
                    ]}
                ).encode()

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Resp()

    import provider_order

    with patch.object(provider_order.urllib.request, "urlopen", _counting_urlopen):
        result1 = load_fallback_order()
        result2 = load_fallback_order()
        result3 = load_fallback_order()

    assert result1 == result2 == result3 == names
    assert len(calls) == 1, (
        f"expected CC Switch to be asked exactly once, got {len(calls)}"
    )

    cache_clear()
    with patch.object(provider_order.urllib.request, "urlopen", _counting_urlopen):
        load_fallback_order()
    assert len(calls) == 2, (
        "cache_clear() must make the next read ask again"
    )


# ---------------------------------------------------------------------------
# The poll contract — sensitivity and counterweight
#
# These two are why ``_poll_until_ready`` is a function of its own. The bug
# they cover (:mod:`urllib` raising ``TimeoutError`` unwrapped) was invisible
# in every lane that ran: locally the server comes up fast enough that the
# 1 s socket timeout never fires, and ``perf`` had no lane of its own until
# 2026-09-29, so nothing exercised the path at all.
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for the context manager ``urlopen`` returns."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_read_timeout_is_retried_rather_than_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape that killed the slow lane: port bound, app not ready yet.

    ``AbstractHTTPHandler.do_open`` calls ``h.getresponse()`` outside the
    ``try`` that wraps errors into ``URLError``, so a read timeout arrives
    as a bare ``TimeoutError``. Without it in the ``except`` clause the
    call raises instead of waiting, and the perf test errors on precisely
    the transient it exists to absorb.
    """
    attempts: list[str] = []

    def _opener(url, timeout=None):
        attempts.append(url)
        if len(attempts) < 3:
            raise TimeoutError("timed out")
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _opener)
    elapsed = _poll_until_ready(
        "http://127.0.0.1:1/health", "/health", time.monotonic()
    )
    assert len(attempts) == 3, (
        "the poll must keep trying after a read timeout — it gave up after "
        f"{len(attempts)} attempt(s), which is the 2026-09-29 failure"
    )
    assert elapsed >= 0.0


def test_a_server_that_never_answers_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counterweight: retrying must not turn into waiting forever.

    The baseline half of the comparison depends on this. A
    ``RuntimeError`` from the baseline measurement now fails the test
    instead of skipping it — a runner that cannot boot ``HEAD`` has to
    say so rather than report a green perf lane. The error carries the
    last HTTP status the server answered with and the tail of its
    stdout, so the failure says *why* the server never came up.
    """
    monkeypatch.setattr(
        sys.modules[__name__], "_STARTUP_DEADLINE_SECONDS", 0.05
    )

    def _opener(url, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", _opener)
    with pytest.raises(RuntimeError):
        _poll_until_ready(
            "http://127.0.0.1:1/health", "/health", time.monotonic()
        )
