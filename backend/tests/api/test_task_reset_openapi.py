"""TDD tests pinning the task-reset endpoint into the public HTTP contract.

``POST /api/execution/{plan_id}/tasks/{task_id}/reset`` is not an internal
helper: it is a documented, machine-callable part of the service's HTTP
surface.  That makes its **shape** part of the contract — the path, the
method, and the request body's fields.  A caller (the PDT frontend, a
script, another agent) reads those three things off the OpenAPI document
and nothing else, so if any of them silently changes, the caller breaks
without any error being raised on either side.

These tests are the pins.  They are deliberately about the *document*,
not about the behaviour: the behaviour is pinned elsewhere
(``test_task_reset_happy.py`` and its siblings), and the document-wide
contract gate (``test_openapi_unchanged.py``) is the thing that keeps the
whole surface from drifting.  What lives here is the narrower, more
readable statement of *this one endpoint's* promise — a failure here
names the endpoint and the field, instead of pointing at a diff of the
whole schema.
"""

from __future__ import annotations

import server


RESET_PATH = "/api/execution/{plan_id}/tasks/{task_id}/reset"


def test_reset_path_is_in_the_openapi_document():
    assert RESET_PATH in server.app.openapi()["paths"]


def test_reset_operation_is_a_post():
    assert "post" in server.app.openapi()["paths"][RESET_PATH]


def test_request_schema_has_reason_and_force():
    schema = server.app.openapi()
    body = schema["paths"][RESET_PATH]["post"]["requestBody"]
    schema_obj = body["content"]["application/json"]["schema"]
    ref = schema_obj.get("$ref") or schema_obj["anyOf"][0]["$ref"]
    props = schema["components"]["schemas"][ref.split("/")[-1]]["properties"]
    assert {"reason", "force"} <= set(props)
