# Running it

## It is a local tool

Three properties decide how this may be run, and they are not configuration
defaults you can talk your way out of:

1. **It writes files, spawns subprocesses and runs commands as *you*, on
   your machine.** It is not built for shared, multi-user or public
   hosting, and nothing in it separates one user's work from another's.
2. **The REST API is the management surface for this process** — start /
   stop / inspect a run, read the artifacts each phase produced, drive the
   workflow. That is what it is for: driving *your* instance, from a local
   script or the bundled UI. It is not a service meant to be handed to
   other people's clients.
3. **The API is unauthenticated.** No accounts, no login, no per-user
   separation. The request guard below decides whether a caller is *this
   instance's own client*; it is not authentication and does not make the
   port safe to expose.

## The request guard

Loopback is not a security boundary against a **browser**. Any page you
visit can issue requests to `127.0.0.1:<port>` from inside your browser, and
the server cannot distinguish those from its own UI — both arrive from
127.0.0.1.

So every `/api/*` request must carry:

```
X-PDT-Request: 1
```

The value is not a secret and is not meant to be. Read the header as a
proof of *not being a foreign web page*, not as a password:

- A **cross-origin** request carrying a custom header is no longer a CORS
  "simple request", so the browser has to preflight it. This server answers
  no preflight (it installs no CORS middleware at all), so a foreign page
  **cannot make the call**. Knowing the value does not help — the browser
  refuses to send the request, not the server to accept it.
- `Origin` is rejected when it is not this machine, which answers a
  cross-origin page that got past the above.
- `Host` is rejected when it is not this machine, which is what answers
  **DNS rebinding** — where the attacker's hostname resolves to 127.0.0.1,
  the browser considers the request same-origin, and neither of the previous
  two checks fires.

Two things are deliberately left open, and both are stated here rather than
discovered later:

- **Static assets.** Browser *navigation* cannot attach a header, so
  requiring one would make the UI unloadable. They are read-only copies of
  what already ships in this repository.
- **`/health` and `/health/data`** live outside the `/api/` prefix and are
  therefore not guarded either. They answer liveness and "can the server
  read its state database", and `/health/data` reports the exception type on
  a failure.

### The UI's fetch contract

The bundled UI sends the header for you — but only through its two wrapper
functions. **A bare `fetch(...)` from any other JS module gets `403
Forbidden`**, because the browser will not attach `X-PDT-Request: 1` on its
own.

| Wrapper | Used by |
|---|---|
| `apiRequest(path, options)` in `frontend/api.js` | the verification-related views |
| `api(path, options)` inside `frontend/app.js` | the rest of the bundled UI |

Both attach the header, parse JSON, and throw an `Error` whose `.detail`
carries the server's `detail` field on a non-2xx response.

Anything else — a `fetch(...)` typed into a new view, a one-off
`XMLHttpRequest`, a script tag injected by a third-party page — fails the
preflight and never reaches the server. The rule is also pinned in source:
`backend/tests/static_gates/test_frontend_uses_api_wrapper.py` audits every
`.js` file under `frontend/` and refuses a bare `fetch(` it cannot trace
back to one of those two wrappers.

Scripts that drive the API directly need the header explicitly:

```bash
curl -H 'X-PDT-Request: 1' http://localhost:8000/api/plans
```

## Off-loopback

If you genuinely need it (a container, a VM you control), set `PDT_HOST`
explicitly, add any non-loopback hostname you reach it by to
`PDT_ALLOWED_HOSTS`, and put your own authentication in front of it:

```bash
PDT_HOST=0.0.0.0 PDT_ALLOWED_HOSTS=pdt.internal python -m backend.server
# only behind your own auth proxy, on a network you trust
```

Unset `PDT_HOST` unless you have a specific reason and know who else is on
that network.

## Watching a run

| Call | Answers |
|---|---|
| `GET /api/plan/{id}/summary` | the one call that says where a plan stands |
| `GET /api/execution/{id}/progress` | task counts, the current task, the next one |
| `GET /api/execution/{id}/status` | process status plus recent log lines |
| `GET /api/execution/{id}/logs?level=WARNING` | filtered execution log |
| `GET /api/execution/{id}/diagnose` | a diagnosis and suggestions for a stuck run |
| `GET /api/execution/{id}/files` | what has actually landed on disk |
| `GET /api/verification/{id}/status` | round, status, stop reason, results |

Execution logs are persisted to `plans/{plan_id}/execution.log` as
JSON-lines, so they survive a server restart — the in-memory log view does
not. `progress` survives one for the same reason, but it reads two on-disk
sources rather than one: the **static task graph** from `tasks.json`, and
the **per-task runtime state** — status, attempts, commit sha, end time —
from the state database (`.pdt/state.db`). `tasks.json` is static-only, so
a restart cannot lose a task's progress or leave it half-written.

!!! tip "`running` is a claim, not evidence"
    `verification_status: "running"` reflects an in-memory field. A
    verification round that has written no log output for a long stretch is
    far more likely to be stuck than slow. Check the modification time of
    the newest file under `plans/{plan_id}/logs/` before believing the
    status field — that is what the watchdog ticks do on your behalf, and
    they mark a thread that stopped reporting rather than waiting forever.
