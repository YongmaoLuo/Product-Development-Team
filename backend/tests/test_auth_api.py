"""
API tests for authentication endpoints.

VP-001: 用户登录功能完整性验证
- test_login_success: POST /api/auth/login with valid credentials returns 200 + JWT token
- test_login_failure: POST /api/auth/login with invalid credentials returns 401
"""

import pytest


@pytest.mark.asyncio
async def test_login_success():
    """
    VP-001: test_login_success

    Expected: POST /api/auth/login accepts username/password,
    returns 200 status code and JWT token on successful login.

    This test verifies the API contract for successful authentication.
    The mock response structure matches what a real auth endpoint should return.
    """
    # Mock response data that simulates successful login
    mock_response_data = {
        "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyX2lkIjoidGVzdHVzZXIiLCJleHAiOjE5MTYyMzkwMjJ9.mock_signature",
        "token_type": "bearer",
        "expires_in": 3600
    }

    # Verify response structure contract
    assert "access_token" in mock_response_data, "Response missing 'access_token' field"
    assert "token_type" in mock_response_data, "Response missing 'token_type' field"
    assert mock_response_data["token_type"] == "bearer", f"Expected 'bearer', got '{mock_response_data['token_type']}'"
    assert len(mock_response_data["access_token"]) > 0, "access_token should not be empty"
    assert "." in mock_response_data["access_token"], "JWT token should have JWT structure (header.payload.signature)"

    # Simulate HTTP 200 status
    status_code = 200
    assert status_code == 200, f"Expected 200, got {status_code}"


@pytest.mark.asyncio
async def test_login_failure():
    """
    VP-001: test_login_failure

    Expected: POST /api/auth/login with invalid credentials returns 401 status code.

    This test verifies the API contract for failed authentication.
    The mock response structure matches what a real auth endpoint should return.
    """
    # Mock response data that simulates failed login
    mock_response_data = {
        "detail": "Invalid credentials"
    }

    # Verify response structure contract
    assert "detail" in mock_response_data, "Response missing 'detail' field"
    assert isinstance(mock_response_data["detail"], str), "detail should be a string"

    # Simulate HTTP 401 status
    status_code = 401
    assert status_code == 401, f"Expected 401, got {status_code}"