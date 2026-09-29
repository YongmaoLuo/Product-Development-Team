"""
API tests for session management endpoints.

VP-015: 会话管理API功能
- test_session_lifecycle: Session creation returns session_id,
  query returns session details, delete returns 204
"""

import pytest

try:
    from fastapi.testclient import TestClient
    from server import app, _sessions, _sessions_lock
except ImportError:
    pytest.skip(
        "Session API not implemented in server.py",
        allow_module_level=True,
    )

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_sessions():
    """Clean session storage before and after each test."""
    with _sessions_lock:
        _sessions.clear()
    yield
    with _sessions_lock:
        _sessions.clear()


def test_session_lifecycle():
    """
    VP-015: test_session_lifecycle

    Expected: Session creation returns session_id, query returns session details,
    delete returns 204 status code.
    """
    # 1. Create session
    create_response = client.post(
        "/api/sessions",
        json={"user_id": "testuser", "expires_in_seconds": 3600}
    )
    assert create_response.status_code == 200, f"Expected 200, got {create_response.status_code}"
    session_data = create_response.json()
    assert "session_id" in session_data, "Session missing 'session_id' field"
    assert "user_id" in session_data, "Session missing 'user_id' field"
    assert "created_at" in session_data, "Session missing 'created_at' field"
    assert "expires_at" in session_data, "Session missing 'expires_at' field"
    assert "is_active" in session_data, "Session missing 'is_active' field"
    assert session_data["is_active"] is True, "New session should be active"
    assert len(session_data["session_id"]) > 0, "session_id should not be empty"

    session_id = session_data["session_id"]

    # 2. Query session
    query_response = client.get(f"/api/sessions/{session_id}")
    assert query_response.status_code == 200, f"Expected 200, got {query_response.status_code}"
    query_data = query_response.json()
    assert query_data["session_id"] == session_id, "Query should return correct session_id"
    assert query_data["user_id"] == "testuser", "Query should return correct user_id"
    assert query_data["is_valid"] is True, "Active session should be valid"

    # 3. Delete session (logout)
    delete_response = client.delete(f"/api/sessions/{session_id}")
    assert delete_response.status_code == 204, f"Expected 204, got {delete_response.status_code}"

    # 4. Verify session is deleted
    verify_response = client.get(f"/api/sessions/{session_id}")
    assert verify_response.status_code == 404, f"Deleted session should return 404, got {verify_response.status_code}"


def test_session_not_found():
    """Test that querying a non-existent session returns 404."""
    response = client.get("/api/sessions/nonexistent_session_id")
    assert response.status_code == 404, f"Expected 404, got {response.status_code}"


def test_session_delete_not_found():
    """Test that deleting a non-existent session returns 404."""
    response = client.delete("/api/sessions/nonexistent_session_id")
    assert response.status_code == 404, f"Expected 404, got {response.status_code}"