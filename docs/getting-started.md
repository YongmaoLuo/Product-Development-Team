# Getting started

## 1. Create the virtualenv

```bash
uv sync --project backend
source backend/.venv/bin/activate
```

Everything below assumes this venv. Not as a convention — as a
requirement:

!!! warning "Always use `backend/.venv`, never the system Python"
    Run `pytest` as `backend/.venv/bin/python3 -m pytest ...`. The system
    Python is missing the pinned wheels, and it carries a stale `urllib3`
    whose `NotOpenSSLWarning` shim no longer matches the runtime LibreSSL
    build. The failure lands at **collection**: the suite dies with an
    `ImportError` before the first test runs, which reads like a broken
    repository rather than a wrong interpreter.

    Activating the venv is the only step that makes `pytest` import the
    modules the suite actually loads. There is no fallback configuration
    that would make the system interpreter work.

## 2. Provide a `.env` — optional

The server starts with no `.env` at all. Create one only if you want
notifications, a non-default timezone, or a non-default keychain:

```bash
cp .env.example .env
```

`.env` is gitignored; `.env.example` is the committed, non-secret
template. It holds no credential — a provider API key is supplied at
runtime by the provider layer, and the two notification secrets are read
from a macOS keychain ([Credentials](operations/configuration.md#credentials)).
Every line in it is commented out, so copying it changes nothing until
you fill one in.

## 3. Start the server

```bash
# From the repository root. The app is a package (`backend/`), so `-m` is
# what puts both the root and `backend/` on `sys.path`. Running
# `python server.py` from inside `backend/` fails on `from backend.framework...`.
python -m backend.server
```

The server binds **loopback only** — `http://127.0.0.1:8000` — and serves
the static frontend from the same process. If 8000 is taken it walks up to
the next free port and prints the one it chose; set `PDT_PORT` to pin it.

Open <http://localhost:8000> for the UI.

## 4. Run your first plan

The UI walks the whole workflow. If you would rather drive it from a
script, the same phases are HTTP endpoints — and every `/api/*` request
needs one extra header:

```bash
curl -H 'X-PDT-Request: 1' http://localhost:8000/api/plans
```

That header is not a password. It exists so a web page you happen to have
open cannot drive your instance; the mechanics are in
[Running it](operations/running.md).

A minimal round trip:

| Step | Call |
|---|---|
| Create a plan from a one-line requirement | `POST /api/interview/{plan_id}/start` |
| Answer the clarifying questions | `POST /api/interview/{plan_id}/answer` |
| Generate the PRD | `POST /api/prd/{plan_id}/generate` |
| Review each decision point | `GET /api/review/{plan_id}/review/items` |
| …then accept / revise / skip each one | `POST /api/review/{plan_id}/review/item/{index}` |
| Generate tasks | `POST /api/tasks/{plan_id}/generate` |
| Start execution | `POST /api/execution/{plan_id}/start` |
| Watch it | `GET /api/execution/{plan_id}/progress` |
| Start verification | `POST /api/verification/{plan_id}/start` |

The reviewed documents are at `GET /api/prd/{id}`, `GET /api/arch/{id}` and
`GET /api/test/{id}`; `GET /api/plan/{id}/summary` is the single call that
tells you where a plan currently stands.

## Next

- [Workflow](workflow.md) — what each phase produces and why the review
  loops exist.
- [Operations / Running it](operations/running.md) — the local-only scope
  and the request guard, before you expose anything.
- [Configuration](operations/configuration.md) — provider routing and
  concurrency caps.
