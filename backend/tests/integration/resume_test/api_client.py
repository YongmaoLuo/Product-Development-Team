"""HTTP API client for the autonomous-coding backend.

Wraps ``urllib.request`` to provide a small, dependency-free facade for
the backend's JSON HTTP API. Used by integration tests and the
resume / inventory helpers that need to query plan state without
spinning up the FastAPI server in-process.

This module deliberately avoids ``requests`` so it can be imported in
the project's base Python environment without an extra dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional


class ApiError(Exception):
    """Base class for ApiClient failures.

    Subclasses map directly to the boundary conditions documented in
    the resume test spec:

      - ``ConnectionError`` — server is unreachable / DNS / refused
      - ``HTTPError`` — server returned a 4xx / 5xx status code
    """


class ConnectionError(ApiError):
    """Raised when the server cannot be reached.

    Mirrors the built-in ``ConnectionError`` to keep ``except`` clauses
    symmetric with the standard library.
    """


class HTTPError(ApiError):
    """Raised when the server returns a non-2xx HTTP status code.

    The original response body is preserved verbatim on ``body`` so the
    caller can inspect the JSON error payload or plain-text diagnostic.

    Attributes
    ----------
    status_code:
        HTTP status code (e.g. 404, 500).
    body:
        Raw response body string (utf-8 decoded with surrogateescape
        so invalid byte sequences are preserved instead of raising).
    """

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"HTTP {status_code}: {body[:200] if body else ''}"
        )


class ApiClient:
    """Tiny urllib-based JSON HTTP client.

    Parameters
    ----------
    base_url:
        Root URL of the backend, e.g. ``"http://127.0.0.1:8000"``.
        Trailing slashes are tolerated.
    timeout:
        Default per-request timeout in seconds. Defaults to 10s.
        Individual ``_request`` calls can override this.
    """

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_url(self, path: str, query: Optional[Dict[str, Any]] = None) -> str:
        """Combine ``base_url`` + ``path`` + optional query string.

        ``path`` may begin with ``/`` (preferred) or be empty; the
        helper normalises the join so the result is always
        ``<base_url>/<path>`` without double slashes.
        """
        if path.startswith("/"):
            tail = path
        elif path:
            tail = "/" + path
        else:
            tail = ""
        url = self.base_url + tail
        if query:
            # ``doseq`` allows list-valued params; not used here, but
            # kept for future flexibility.
            encoded = urllib.parse.urlencode(query, doseq=True)
            url = url + ("&" if "?" in url else "?") + encoded
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        query: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Perform a single HTTP request and return the parsed JSON body.

        Boundary handling:

          * ``urllib.error.HTTPError`` is translated to our own
            ``HTTPError`` carrying ``status_code`` and ``body``.
          * ``urllib.error.URLError`` (which subsumes ``http.client``
            connection-refused, DNS failures, and timeouts) is
            translated to ``ConnectionError`` so callers can write a
            single ``except ConnectionError`` clause.
          * A non-dict JSON body (e.g. ``null`` or ``[1,2,3]``) is
            returned as-is rather than coerced — the resume spec calls
            for "JSON 解析" and most of our endpoints return dicts,
            but we don't want to surprise the caller.
        """
        url = self._build_url(path, query=query)
        data: Optional[bytes] = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            url,
            data=data,
            method=method.upper(),
            headers=headers,
        )
        effective_timeout = float(timeout) if timeout is not None else self.timeout

        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # ``HTTPError`` is a subclass of ``URLError``; it carries
            # ``.code`` (status code) and is itself a file-like object
            # exposing ``.read()`` for the response body. We translate
            # it into our own ``HTTPError`` BEFORE the URLError catch
            # below runs.
            try:
                error_body_bytes = exc.read()
            except Exception:  # pragma: no cover - defensive
                error_body_bytes = b""
            try:
                error_body = error_body_bytes.decode("utf-8")
            except UnicodeDecodeError:
                error_body = error_body_bytes.decode(
                    "utf-8", errors="surrogateescape"
                )
            raise HTTPError(status_code=int(exc.code), body=error_body) from exc
        except urllib.error.URLError as exc:
            # Anything under URLError (ConnectionRefused, timeout,
            # NameResolutionError, ...) that is NOT an HTTPError is
            # reported as ConnectionError so callers can write a
            # single ``except ConnectionError`` clause.
            raise ConnectionError(
                f"failed to reach {url}: {exc.reason!r}"
            ) from exc

        # Decode without strict UTF-8 to preserve the original bytes
        # even if the server returned a malformed body.
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="surrogateescape")

        if not text:
            return {}

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # Body is not JSON — surface it as an HTTP error so the
            # caller can decide what to do. Use 0 as a sentinel status
            # because urllib did not provide one.
            raise HTTPError(status_code=0, body=text)

        return parsed

    def get(
        self,
        path: str,
        *,
        query: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Issue an HTTP GET and return the parsed JSON body."""
        return self._request("GET", path, query=query, timeout=timeout)

    def post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        query: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Issue an HTTP POST and return the parsed JSON body."""
        return self._request("POST", path, body=body, query=query, timeout=timeout)

    # ------------------------------------------------------------------
    # Convenience wrappers around the backend endpoints
    # ------------------------------------------------------------------

    def get_plan_summary(self, plan_id: str) -> Dict[str, Any]:
        """GET /api/plan/{plan_id}/summary."""
        encoded = urllib.parse.quote(plan_id, safe="")
        return self.get(f"/api/plan/{encoded}/summary")

    def get_execution_progress(self, plan_id: str) -> Dict[str, Any]:
        """GET /api/execution/{plan_id}/progress."""
        encoded = urllib.parse.quote(plan_id, safe="")
        return self.get(f"/api/execution/{encoded}/progress")

    def get_verification_status(self, plan_id: str) -> Dict[str, Any]:
        """GET /api/verification/{plan_id}/status."""
        encoded = urllib.parse.quote(plan_id, safe="")
        return self.get(f"/api/verification/{encoded}/status")

    def post_execution_start(
        self,
        plan_id: str,
        *,
        project_dir: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST /api/execution/{plan_id}/start.

        ``project_dir`` is forwarded verbatim in the JSON body so the
        caller can keep the exact path string the backend expects.
        """
        encoded = urllib.parse.quote(plan_id, safe="")
        body: Optional[Dict[str, Any]] = None
        if project_dir is not None:
            body = {"project_dir": project_dir}
        return self.post(
            f"/api/execution/{encoded}/start",
            body=body,
            timeout=timeout,
        )

    def post_verification_start(
        self,
        plan_id: str,
        *,
        max_rounds: Optional[int] = None,
        auto_fix: Optional[bool] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """POST /api/verification/{plan_id}/start.

        ``max_rounds`` and ``auto_fix`` are only included in the body
        when explicitly provided, matching the backend's tolerance
        for missing optional fields.
        """
        encoded = urllib.parse.quote(plan_id, safe="")
        body: Optional[Dict[str, Any]] = None
        if max_rounds is not None or auto_fix is not None:
            body = {}
            if max_rounds is not None:
                body["max_rounds"] = int(max_rounds)
            if auto_fix is not None:
                body["auto_fix"] = bool(auto_fix)
        return self.post(
            f"/api/verification/{encoded}/start",
            body=body,
            timeout=timeout,
        )