"""
Backend API Client
==================

Async HTTP client for the spec-driven development backend API.

Features:
- 30 second timeout per request
- 3 retries with exponential backoff (1s/2s/4s) on timeout and 5xx
- No retry on 4xx (raises BadRequestError immediately)
"""

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx


logger = logging.getLogger(__name__)


# ============================================================
# Exceptions
# ============================================================

class BackendApiException(Exception):
    """Base for backend API errors."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class BadRequestError(BackendApiException):
    """Raised on 4xx responses. Not retried."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"Bad request {status_code}: {message}", status_code)


class ServerError(BackendApiException):
    """Raised after retries exhausted on 5xx responses."""

    def __init__(self, status_code: int, message: str):
        super().__init__(f"Server error {status_code}: {message}", status_code)


class TimeoutException(BackendApiException):
    """Raised after retries exhausted on request timeout."""

    def __init__(self, message: str):
        super().__init__(message)


# ============================================================
# Client
# ============================================================

class BackendApiClient:
    """Async HTTP client for the spec-driven development backend API.

    Usage:
        async with BackendApiClient(base_url="http://localhost:8000") as client:
            result = await client.create_plan("plan-id")

        # Or manage lifecycle manually
        client = BackendApiClient()
        try:
            result = await client.create_plan("plan-id")
        finally:
            await client.close()
    """

    DEFAULT_TIMEOUT = 30.0
    MAX_RETRIES = 3
    BACKOFF_SECONDS = [1, 2, 4]

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        timeout: float = DEFAULT_TIMEOUT,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "BackendApiClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def _ensure_client(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make an HTTP request with retry/backoff logic.

        Retries on timeout and 5xx (3 retries with backoff 1s/2s/4s).
        Raises immediately on 4xx (BadRequestError).
        """
        await self._ensure_client()

        last_exception: Optional[BackendApiException] = None

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                response = await self._client.request(
                    method=method,
                    url=path,
                    json=json,
                )

                if 400 <= response.status_code < 500:
                    raise BadRequestError(response.status_code, response.text)

                if response.status_code >= 500:
                    last_exception = ServerError(response.status_code, response.text)
                    if attempt < self.MAX_RETRIES:
                        delay = self.BACKOFF_SECONDS[attempt]
                        logger.warning(
                            "Request %s %s status %d. Retrying in %ds (retry %d/%d)",
                            method, path, response.status_code,
                            delay, attempt + 1, self.MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise last_exception

                if response.content:
                    return response.json()
                return {}

            except httpx.TimeoutException as e:
                last_exception = TimeoutException(
                    f"Request {method} {path} timed out: {e}"
                )
                if attempt < self.MAX_RETRIES:
                    delay = self.BACKOFF_SECONDS[attempt]
                    logger.warning(
                        "Request %s %s timed out. Retrying in %ds (retry %d/%d)",
                        method, path, delay, attempt + 1, self.MAX_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise last_exception

        if last_exception is not None:
            raise last_exception
        raise BackendApiException("Request retry path exited unexpectedly")

    # ============================================================
    # Plans
    # ============================================================

    async def list_plans(self) -> Dict[str, Any]:
        return await self._request("GET", "/api/plans")

    async def get_plan_state(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/plan/{plan_id}/state")

    async def get_plan_summary(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/plan/{plan_id}/summary")

    async def update_plan_state(
        self,
        plan_id: str,
        arch_enabled: Optional[bool] = None,
        test_enabled: Optional[bool] = None,
        current_phase: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if arch_enabled is not None:
            body["arch_enabled"] = arch_enabled
        if test_enabled is not None:
            body["test_enabled"] = test_enabled
        if current_phase is not None:
            body["current_phase"] = current_phase
        return await self._request(
            "POST",
            f"/api/plan/{plan_id}/state",
            json=body,
        )

    # ============================================================
    # Interview
    # ============================================================

    async def start_interview(
        self,
        requirement: str,
        plan_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"requirement": requirement}
        if plan_id:
            body["plan_id"] = plan_id
        return await self._request("POST", "/api/interview/start", json=body)

    async def continue_interview(
        self,
        plan_id: str,
        reply: str,
    ) -> Dict[str, Any]:
        """Continue an interview with the user's reply."""
        return await self._request(
            "POST",
            f"/api/interview/{plan_id}/continue",
            json={"reply": reply},
        )

    async def get_interview(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/interview/{plan_id}")

    # ============================================================
    # PRD
    # ============================================================

    async def create_plan(self, plan_id: str) -> Dict[str, Any]:
        """Generate PRD for a plan via POST /api/prd/{plan_id}/generate."""
        return await self._request("POST", f"/api/prd/{plan_id}/generate")

    async def get_prd(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/prd/{plan_id}")

    async def regenerate_prd(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/prd/{plan_id}/regenerate")

    async def refine_prd(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/prd/{plan_id}/refine")

    # ============================================================
    # PRD Review
    # ============================================================

    async def get_review_items(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/review/{plan_id}/review/items")

    async def submit_review(
        self,
        plan_id: str,
        index: int,
        action: str,
        note: str = "",
        question: str = "",
    ) -> Dict[str, Any]:
        """Submit review action for a single PRD decision point.

        Calls POST /api/review/{plan_id}/review/item/{index}.
        """
        return await self._request(
            "POST",
            f"/api/review/{plan_id}/review/item/{index}",
            json={"action": action, "note": note, "question": question},
        )

    # ============================================================
    # Architecture
    # ============================================================

    async def generate_arch(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/arch/{plan_id}/generate")

    async def get_arch(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/arch/{plan_id}")

    async def regenerate_arch(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/arch/{plan_id}/regenerate")

    async def refine_arch(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/arch/{plan_id}/refine")

    async def get_arch_review_items(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/arch/{plan_id}/review/items")

    async def submit_arch_review(
        self,
        plan_id: str,
        index: int,
        action: str,
        note: str = "",
        question: str = "",
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/arch/{plan_id}/review/item/{index}",
            json={"action": action, "note": note, "question": question},
        )

    # ============================================================
    # Test Design
    # ============================================================

    async def generate_test_design(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/test/{plan_id}/generate")

    async def get_test_design(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/test/{plan_id}")

    async def regenerate_test_design(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/test/{plan_id}/regenerate")

    async def refine_test_design(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/test/{plan_id}/refine")

    async def get_test_review_items(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/test/{plan_id}/review/items")

    async def submit_test_review(
        self,
        plan_id: str,
        index: int,
        action: str,
        note: str = "",
        question: str = "",
    ) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"/api/test/{plan_id}/review/item/{index}",
            json={"action": action, "note": note, "question": question},
        )

    # ============================================================
    # Tasks
    # ============================================================

    async def generate_tasks(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/tasks/{plan_id}/generate")

    async def get_tasks(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/tasks/{plan_id}")

    # ============================================================
    # Execution
    # ============================================================

    async def start_execution(
        self,
        plan_id: str,
        project_dir: str,
        tool: Optional[str] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {"project_dir": project_dir}
        if tool:
            body["tool"] = tool
        return await self._request(
            "POST",
            f"/api/execution/{plan_id}/start",
            json=body,
        )

    async def get_execution_status(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/execution/{plan_id}/status")

    async def stop_execution(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("POST", f"/api/execution/{plan_id}/stop")

    async def get_execution_progress(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/execution/{plan_id}/progress")

    async def get_execution_files(self, plan_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/api/execution/{plan_id}/files")