"""A tiny HTTP service for the spawn reproduction.

Why this file exists
--------------------
The spawn reproduction needs *something* to start: a process that binds
a port and answers on it, so that "the port answers and our child is
still running" can be judged.  The real backend would do, but it is a
large surface -- a state database, a plans directory, a lifespan, a
watchdog -- and every one of those is a way for a start-up to fail for
a reason that has nothing to do with the spawn.

This is that "something else": a service with one route and no state.

Two rules this file keeps
-------------------------
**Importing starts nothing.**  The application is built inside
:func:`build_app` and the server is started inside :func:`main`, so
``import tiny_service`` leaves no listener behind and no port taken.
A test that imports this file to look at it must not be the reason a
port is busy.

**The health route answers 200.**  ``/health`` is what a caller probes
to learn that the service came up; it exists so the readiness check
has something meaningful to say.

Run it directly::

    python3 tiny_service.py --port 8123
"""

from __future__ import annotations

import argparse
from typing import Any, Optional, Sequence


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


def build_app() -> Any:
    """The ASGI application.

    Built on demand rather than at import time: importing this module
    must not start anything, and the less an import does, the easier
    that promise is to keep.
    """
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        """Answer 200 -- the service is up and serving."""
        return {"status": "ok"}

    return app


# ---------------------------------------------------------------------------
# The entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Serve ``/health`` on ``--port`` until the process is stopped."""
    parser = argparse.ArgumentParser(
        prog="tiny_service.py",
        description="A minimal HTTP service for the spawn reproduction.",
    )
    parser.add_argument(
        "--port", type=int, required=True,
        help="the port to bind on loopback",
    )
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        build_app(),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
