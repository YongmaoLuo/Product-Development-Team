"""Feishu IM client — minimal subset of the forwarder's `feishu_client.FeishuClient`.

Why this exists
---------------
The full client at ``the forwarder's feishu_client.py``
implements both the IM-message surface (send / patch interactive
cards) AND a Bitable surface (records, tables, fields, batch
ops). The notifier only needs the IM surface — sending a fresh
interactive card and patching an existing one.

Splitting this out has three benefits:

1. **No symlink.** The original file lived under `tools/task_sync/`
   (that package was deleted 2026-09-13 together with the polling
   bridge; the client moved into the forwarder as `feishu_client.py`).
   The new file lives under
   `backend/notifications/` and depends on nothing in `tools/`.
2. **No Bitable surface in the backend.** Bitable records are the
   provider-usage forwarder's concern (its usage table); the backend
   should not import a thousand lines of bitable plumbing to push a
   card.
3. **Lazy `lark_oapi` import.** If the operator hasn't installed
   `lark-oapi`, the server still boots — `FeishuClient.__init__`
   imports the SDK lazily and the notifier detects the failure at
   startup and disables itself with an explicit ERROR log instead
   of crashing the request path.

Contract
--------
`send_message(receive_id, content, msg_type="text") -> Optional[str]`
    Returns the new ``message_id`` on success or ``None`` on
    failure (the notifier records the failure under
    ``pushes_failed`` and falls back to a fresh card next event).

`update_message(message_id, content) -> bool`
    PATCHes the existing card in place via
    ``/open-apis/im/v1/messages/{message_id}``. Returns True on
    success, False otherwise. A False here is the notifier's
    signal that the card has expired (Feishu caps card edits at
    ~14 days, or the message was deleted) and a new card must be
    sent.

`get_tenant_access_token(force_refresh=False) -> str`
    Caches the tenant access token and refreshes it 5 minutes
    before ``expire_in``. Exposed for the notifier's startup
    health check.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)


class FeishuApiException(Exception):
    """Raised for Feishu API errors that the notifier should treat as fatal."""

    def __init__(
        self,
        message: str,
        code: Optional[int] = None,
        error_data: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.error_data = error_data or {}


class FeishuUnavailable(Exception):
    """Raised at FeishuClient construction when lark-oapi is not installed.

    The notifier catches this and disables itself with a single
    ERROR log, so the server stays up.
    """


class FeishuClient:
    """Minimal Feishu IM client (send + patch interactive cards).

    Construction reads ``FEISHU_APP_ID`` and ``FEISHU_APP_SECRET``
    from the environment. If ``lark-oapi`` is not importable, the
    constructor raises ``FeishuUnavailable`` — callers (the
    notifier) must handle that explicitly rather than catching
    ImportError, so the failure mode is named.
    """

    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
    ) -> None:
        import os

        try:
            import lark_oapi as lark  # noqa: F401 — used at .build() below
        except ImportError as exc:
            raise FeishuUnavailable(
                "lark-oapi SDK is required for Feishu push. "
                "Install with: pip install lark-oapi"
            ) from exc

        self.app_id = app_id or os.environ.get("FEISHU_APP_ID", "")
        self.app_secret = app_secret or os.environ.get("FEISHU_APP_SECRET", "")

        if not self.app_id or not self.app_secret:
            raise FeishuUnavailable(
                "FEISHU_APP_ID / FEISHU_APP_SECRET not set in environment"
            )

        # Lazy import — lark is heavy and we want the constructor to
        # fail fast at startup if it's missing, but not at module
        # import time (which would block every test).
        import lark_oapi as lark

        self._lark_client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .build()
        )
        self._tenant_access_token: Optional[str] = None
        self._token_expire_time: Optional[datetime] = None

        logger.info("FeishuClient initialized for app_id=%s", self.app_id)

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def get_tenant_access_token(self, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self._tenant_access_token is not None
            and self._token_expire_time is not None
            and datetime.utcnow()
            < self._token_expire_time - timedelta(minutes=5)
        ):
            return self._tenant_access_token

        try:
            payload = json.dumps(
                {"app_id": self.app_id, "app_secret": self.app_secret}
            ).encode("utf-8")
            req = urllib.request.Request(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())

            if body.get("code", -1) != 0:
                raise FeishuApiException(
                    f"Token request failed: {body.get('msg')}",
                    code=body.get("code"),
                )

            self._tenant_access_token = body["tenant_access_token"]
            expire_in = body.get("expire", 7200)
            self._token_expire_time = datetime.utcnow() + timedelta(seconds=expire_in)
            logger.info("Obtained tenant access token, expires in %ds", expire_in)
            return self._tenant_access_token

        except FeishuApiException:
            raise
        except Exception as exc:
            raise FeishuApiException(
                f"Error getting tenant access token: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # IM message surface
    # ------------------------------------------------------------------

    def send_message(
        self,
        receive_id: str,
        content: str,
        msg_type: str = "text",
        receive_id_type: str = "chat_id",
    ) -> Optional[str]:
        """Send an IM message and the new message id (or ``None`` on failure)."""
        self.get_tenant_access_token()

        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )
            response = self._lark_client.im.v1.message.create(request)

            if response.code != 0:
                logger.error(
                    "Failed to send message: %s (code=%s)",
                    response.msg,
                    response.code,
                )
                return None

            msg_id = response.data.message_id if response.data else None
            logger.info("Sent %s message to %s", msg_type, receive_id)
            return msg_id

        except Exception as exc:
            logger.error("Error sending message: %s", exc)
            return None

    def update_message(self, message_id: str, content: str) -> bool:
        """PATCH an existing IM message in place.

        Uses ``/open-apis/im/v1/messages/{message_id}`` (NOT the
        legacy ``/open-apis/interactive/v1/cards/{message_id}`` —
        that endpoint returns 404 for IM-API-sent messages).

        Returns True on success, False otherwise. A False here
        signals the notifier to fall back to a fresh card.
        """
        token = self.get_tenant_access_token()
        try:
            payload = json.dumps({"content": content}).encode("utf-8")
            req = urllib.request.Request(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                data=payload,
                method="PATCH",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())

            if body.get("code", -1) != 0:
                logger.error(
                    "Failed to update message %s: %s (code=%s)",
                    message_id,
                    body.get("msg"),
                    body.get("code"),
                )
                return False

            logger.info("Updated message %s", message_id)
            return True

        except urllib.error.HTTPError as exc:
            logger.error(
                "HTTP %s updating message %s", exc.code, message_id
            )
            return False
        except Exception as exc:
            logger.error("Error updating message %s: %s", message_id, exc)
            return False