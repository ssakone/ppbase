"""Integration tests for auth collection user authentication.

Covers:
- Registration (create record with password)
- Auth-with-password (login)
- Auth-refresh (token refresh)
- Auth-methods
- Email verification (request + confirm)
- Password reset (request + confirm)
- End-to-end flows
- Rule enforcement with auth records
"""

from __future__ import annotations

import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


# =========================================================================
# Test helpers
# =========================================================================

def _get_token_secret(coll, token_type: str, token_key: str) -> str:
    """Build the full signing secret for a purpose-specific token.

    Uses the per-collection secret from ``coll.options[token_type].secret``.
    """
    from ppbase.services.auth_service import get_collection_token_config
    secret, _ = get_collection_token_config(coll, token_type)
    return token_key + secret


def _get_token_duration(coll, token_type: str) -> int:
    from ppbase.services.auth_service import get_collection_token_config
    _, duration = get_collection_token_config(coll, token_type)
    return duration


# =========================================================================
# Registration Tests
# =========================================================================


class TestRegistration:
    """Tests for creating auth collection records (registration)."""

    async def test_register_success(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Register a user with valid email + password."""
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "alice@example.com",
                "password": "password123",
                "passwordConfirm": "password123",
                "name": "Alice",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert data["email"] == "alice@example.com"
        assert data["verified"] is False
        assert "emailVisibility" in data
        assert data["emailVisibility"] is False
        # password_hash and token_key must never appear in response
        assert "password_hash" not in data
        assert "token_key" not in data
        assert "collectionName" in data
        assert data["collectionName"] == "users"

    async def test_register_password_mismatch(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Registration fails when password and passwordConfirm don't match."""
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "mismatch@example.com",
                "password": "password123",
                "passwordConfirm": "different456",
            },
        )
        assert resp.status_code == 400, resp.text
        data = resp.json()
        assert "passwordConfirm" in data.get("data", {})

    async def test_register_missing_password(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Registration fails when password is missing."""
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "nopwd@example.com",
            },
        )
        assert resp.status_code == 400, resp.text
        data = resp.json()
        assert "password" in data.get("data", {})

    async def test_register_short_password(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Registration fails when password is shorter than 8 chars."""
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "short@example.com",
                "password": "123",
                "passwordConfirm": "123",
            },
        )
        assert resp.status_code == 400, resp.text
        data = resp.json()
        assert "password" in data.get("data", {})

    async def test_register_duplicate_email(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Registration fails for duplicate email (unique constraint)."""
        # First registration should succeed
        resp1 = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "dupe@example.com",
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp1.status_code == 200, resp1.text

        # Second registration with same email should fail with proper error
        resp2 = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "dupe@example.com",
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp2.status_code == 400
        data2 = resp2.json()
        assert "email" in data2.get("data", {})
        assert data2["data"]["email"]["code"] == "validation_not_unique"

    async def test_register_rejects_password_hash(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Client-supplied password_hash is ignored (not set in record)."""
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "noinject@example.com",
                "password": "password123",
                "passwordConfirm": "password123",
                "password_hash": "injected_hash",
                "token_key": "injected_key",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Should never expose password_hash or token_key
        assert "password_hash" not in data
        assert "token_key" not in data

    async def test_register_base_collection_no_password(
        self,
        app_client: AsyncClient,
        admin_token: str,
    ):
        """Creating a record on a base collection does not require password."""
        import uuid
        coll_name = f"posts_{uuid.uuid4().hex[:8]}"
        # Create a base collection first
        resp = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": coll_name,
                "type": "base",
                "schema": [
                    {"name": "title", "type": "text", "required": True},
                ],
                "createRule": "",
                "listRule": "",
                "viewRule": "",
                "updateRule": "",
                "deleteRule": "",
            },
        )
        assert resp.status_code == 200, resp.text

        # Create record without password
        resp2 = await app_client.post(
            f"/api/collections/{coll_name}/records",
            json={"title": "Hello World"},
        )
        assert resp2.status_code == 200, resp2.text


# =========================================================================
# Auth-with-Password Tests
# =========================================================================


class TestAuthWithPassword:
    """Tests for login via auth-with-password endpoint."""

    @pytest_asyncio.fixture
    async def login_user(self, app_client: AsyncClient, auth_collection: dict):
        """Create a user for login tests (unique email per test)."""
        import uuid
        email = f"login_{uuid.uuid4().hex[:8]}@example.com"
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
                "name": "Login User",
            },
        )
        assert resp.status_code == 200, resp.text
        user = resp.json()
        user["_email"] = email  # for tests to use
        return user

    async def test_auth_with_password_success(
        self,
        app_client: AsyncClient,
        login_user: dict,
    ):
        """Valid identity + password returns token and record."""
        email = login_user.get("_email", login_user.get("email", ""))
        resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": email,
                "password": "securepass123",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert "token" in data
        assert "record" in data
        assert data["record"]["email"] == email
        # Token should be a valid JWT
        decoded = pyjwt.decode(
            data["token"], options={"verify_signature": False}
        )
        assert decoded["type"] == "authRecord"
        assert decoded["id"] == login_user["id"]

    async def test_auth_with_password_invalid_credentials(
        self,
        app_client: AsyncClient,
        login_user: dict,
    ):
        """Wrong password returns 400."""
        email = login_user.get("_email", login_user.get("email", ""))
        resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": email,
                "password": "wrongpassword",
            },
        )
        assert resp.status_code == 400, resp.text

    async def test_auth_with_password_unknown_identity(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Non-existent email returns 400 (same as wrong password — no enumeration)."""
        resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": "doesnotexist@example.com",
                "password": "somepassword",
            },
        )
        assert resp.status_code == 400, resp.text

    async def test_auth_with_password_wrong_collection(
        self,
        app_client: AsyncClient,
    ):
        """Using a base collection for auth returns 404."""
        resp = await app_client.post(
            "/api/collections/posts/auth-with-password",
            json={
                "identity": "user@example.com",
                "password": "password123",
            },
        )
        assert resp.status_code == 404, resp.text

    async def test_auth_with_password_superusers_blocked(
        self,
        app_client: AsyncClient,
    ):
        """_superusers collection is blocked for user auth."""
        resp = await app_client.post(
            "/api/collections/_superusers/auth-with-password",
            json={
                "identity": "admin@test.com",
                "password": "adminpass123",
            },
        )
        assert resp.status_code == 404, resp.text

    async def test_auth_with_password_empty_fields(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Empty identity and password return 400."""
        resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": "", "password": ""},
        )
        assert resp.status_code == 400, resp.text


# =========================================================================
# Auth-Refresh Tests
# =========================================================================


class TestAuthRefresh:
    """Tests for refreshing auth tokens."""

    @pytest_asyncio.fixture
    async def user_token(self, app_client: AsyncClient, auth_collection: dict):
        """Register + login a user and return the token."""
        import uuid
        email = f"refresh_{uuid.uuid4().hex[:8]}@example.com"
        await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": email,
                "password": "securepass123",
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    async def test_auth_refresh_success(
        self,
        app_client: AsyncClient,
        user_token: str,
    ):
        """Valid record token returns a new token + record."""
        resp = await app_client.post(
            "/api/collections/users/auth-refresh",
            headers={"Authorization": user_token},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "token" in data
        assert "record" in data

    async def test_auth_refresh_invalid_token(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Invalid token returns 401."""
        resp = await app_client.post(
            "/api/collections/users/auth-refresh",
            headers={"Authorization": "invalid.jwt.token"},
        )
        assert resp.status_code == 401, resp.text

    async def test_auth_refresh_missing_token(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Missing Authorization header returns 401."""
        resp = await app_client.post(
            "/api/collections/users/auth-refresh",
        )
        assert resp.status_code == 401, resp.text

    async def test_auth_refresh_wrong_collection(
        self,
        app_client: AsyncClient,
        admin_token: str,
        auth_collection: dict,
    ):
        """Token from collection A, refresh on collection B → 401."""
        import uuid
        members_name = f"members_{uuid.uuid4().hex[:8]}"
        # Create a second auth collection
        resp = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": members_name,
                "type": "auth",
                "schema": [],
                "createRule": "",
                "listRule": "",
                "viewRule": "",
                "updateRule": "",
                "deleteRule": "",
            },
        )
        assert resp.status_code == 200, resp.text
        # Register a user in members
        await app_client.post(
            f"/api/collections/{members_name}/records",
            json={
                "email": "member@example.com",
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        # Login to get a members token
        resp = await app_client.post(
            f"/api/collections/{members_name}/auth-with-password",
            json={
                "identity": "member@example.com",
                "password": "securepass123",
            },
        )
        assert resp.status_code == 200, resp.text
        members_token = resp.json()["token"]

        # Try to refresh members token on users collection
        resp2 = await app_client.post(
            "/api/collections/users/auth-refresh",
            headers={"Authorization": members_token},
        )
        assert resp2.status_code == 401, resp2.text


# =========================================================================
# Auth-Methods Tests
# =========================================================================


class TestAuthMethods:
    """Tests for GET auth-methods endpoint."""

    async def test_auth_methods_returns_password_config(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Returns password auth config for auth collection."""
        resp = await app_client.get(
            "/api/collections/users/auth-methods",
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "password" in data
        assert data["password"]["enabled"] is True
        assert "email" in data["password"]["identityFields"]

    async def test_auth_methods_invalid_collection(
        self,
        app_client: AsyncClient,
    ):
        """Base collection returns 404."""
        resp = await app_client.get(
            "/api/collections/posts/auth-methods",
        )
        assert resp.status_code == 404, resp.text


# =========================================================================
# Email Verification Tests
# =========================================================================


class TestEmailVerification:
    """Tests for verification request and confirmation."""

    @pytest_asyncio.fixture
    async def unverified_user(self, app_client: AsyncClient, auth_collection: dict):
        """Create an unverified user."""
        import uuid
        email = f"verify_{uuid.uuid4().hex[:8]}@example.com"
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        assert resp.status_code == 200, resp.text
        user = resp.json()
        user["_email"] = email
        return user

    async def test_request_verification_success(
        self,
        app_client: AsyncClient,
        unverified_user: dict,
    ):
        """Requesting verification for an existing user returns 204."""
        email = unverified_user.get("_email", unverified_user.get("email", ""))
        resp = await app_client.post(
            "/api/collections/users/request-verification",
            json={"email": email},
        )
        assert resp.status_code == 204, resp.text

    async def test_request_verification_unknown_email(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Requesting verification for unknown email still returns 204 (no enumeration)."""
        resp = await app_client.post(
            "/api/collections/users/request-verification",
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 204, resp.text

    async def test_confirm_verification_success(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Confirm verification with a valid token sets verified=true."""
        # Register a fresh user
        reg_resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "confirm_verify@example.com",
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        assert reg_resp.status_code == 200
        user = reg_resp.json()

        # Generate token directly using the service
        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_id
        from ppbase.services.auth_service import create_verification_token

        engine = get_engine()
        from ppbase.services.record_service import resolve_collection

        coll = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_id(engine, coll, user["id"])
        assert raw is not None

        token = create_verification_token(
            raw["id"],
            coll.id,
            raw["email"],
            _get_token_secret(coll, "verificationToken", raw["token_key"]),
            _get_token_duration(coll, "verificationToken"),
        )

        resp = await app_client.post(
            "/api/collections/users/confirm-verification",
            json={"token": token},
        )
        assert resp.status_code == 204, resp.text

        # Verify the record is now verified
        view_resp = await app_client.get(
            f"/api/collections/users/records/{user['id']}",
        )
        assert view_resp.status_code == 200
        assert view_resp.json()["verified"] is True

    async def test_confirm_verification_invalid_token(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Invalid verification token returns 400."""
        resp = await app_client.post(
            "/api/collections/users/confirm-verification",
            json={"token": "invalid.token.here"},
        )
        assert resp.status_code == 400, resp.text

    async def test_confirm_verification_already_verified(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Re-confirming an already verified user is idempotent or accepted."""
        # Register and verify a user
        reg_resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "already_verified@example.com",
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        assert reg_resp.status_code == 200
        user = reg_resp.json()

        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_id
        from ppbase.services.auth_service import create_verification_token
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        coll = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_id(engine, coll, user["id"])

        token = create_verification_token(
            raw["id"],
            coll.id,
            raw["email"],
            _get_token_secret(coll, "verificationToken", raw["token_key"]),
            _get_token_duration(coll, "verificationToken"),
        )

        # First confirm
        resp1 = await app_client.post(
            "/api/collections/users/confirm-verification",
            json={"token": token},
        )
        assert resp1.status_code == 204

        # Second confirm — should succeed or be idempotent
        # Note: after first confirm, verified=True, so request-verification
        # won't generate a new token. But the old token still works because
        # the confirm endpoint just checks the token and sets verified=True.
        resp2 = await app_client.post(
            "/api/collections/users/confirm-verification",
            json={"token": token},
        )
        # Could be 204 (idempotent) or 400 if re-verify is rejected
        assert resp2.status_code in (204, 400)


# =========================================================================
# Password Reset Tests
# =========================================================================


class TestPasswordReset:
    """Tests for password reset request and confirmation."""

    @pytest_asyncio.fixture
    async def reset_user(self, app_client: AsyncClient, auth_collection: dict):
        """Create a user for password reset tests."""
        import uuid
        email = f"reset_{uuid.uuid4().hex[:8]}@example.com"
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "oldpassword123",
                "passwordConfirm": "oldpassword123",
            },
        )
        assert resp.status_code == 200, resp.text
        user = resp.json()
        user["_email"] = email
        return user

    async def test_request_password_reset_success(
        self,
        app_client: AsyncClient,
        reset_user: dict,
    ):
        """Request password reset for existing user returns 204."""
        email = reset_user.get("_email", reset_user.get("email", ""))
        resp = await app_client.post(
            "/api/collections/users/request-password-reset",
            json={"email": email},
        )
        assert resp.status_code == 204, resp.text

    async def test_request_password_reset_unknown_email(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Request password reset for unknown email returns 204 (no enumeration)."""
        resp = await app_client.post(
            "/api/collections/users/request-password-reset",
            json={"email": "unknown@example.com"},
        )
        assert resp.status_code == 204, resp.text

    async def test_confirm_password_reset_success(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Valid reset token + new password succeeds, and login works with new password."""
        # Register user
        reg_resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "reset_confirm@example.com",
                "password": "oldpassword123",
                "passwordConfirm": "oldpassword123",
            },
        )
        assert reg_resp.status_code == 200
        user = reg_resp.json()

        # Generate token directly
        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_id
        from ppbase.services.auth_service import create_password_reset_token
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        coll = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_id(engine, coll, user["id"])

        token = create_password_reset_token(
            raw["id"],
            coll.id,
            raw["email"],
            _get_token_secret(coll, "passwordResetToken", raw["token_key"]),
            _get_token_duration(coll, "passwordResetToken"),
        )

        resp = await app_client.post(
            "/api/collections/users/confirm-password-reset",
            json={
                "token": token,
                "password": "newpassword456",
                "passwordConfirm": "newpassword456",
            },
        )
        assert resp.status_code == 204, resp.text

        # Login with new password should work
        login_resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": "reset_confirm@example.com",
                "password": "newpassword456",
            },
        )
        assert login_resp.status_code == 200, login_resp.text

        # Login with old password should fail
        old_login = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": "reset_confirm@example.com",
                "password": "oldpassword123",
            },
        )
        assert old_login.status_code == 400

    async def test_confirm_password_reset_password_mismatch(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Password mismatch in confirm returns 400."""
        # Register user
        reg_resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "reset_mismatch@example.com",
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert reg_resp.status_code == 200
        user = reg_resp.json()

        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_id
        from ppbase.services.auth_service import create_password_reset_token
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        coll = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_id(engine, coll, user["id"])

        token = create_password_reset_token(
            raw["id"],
            coll.id,
            raw["email"],
            _get_token_secret(coll, "passwordResetToken", raw["token_key"]),
            _get_token_duration(coll, "passwordResetToken"),
        )

        resp = await app_client.post(
            "/api/collections/users/confirm-password-reset",
            json={
                "token": token,
                "password": "newpass123",
                "passwordConfirm": "different456",
            },
        )
        assert resp.status_code == 400, resp.text

    async def test_confirm_password_reset_invalid_token(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Invalid reset token returns 400."""
        resp = await app_client.post(
            "/api/collections/users/confirm-password-reset",
            json={
                "token": "invalid.token.here",
                "password": "newpass123",
                "passwordConfirm": "newpass123",
            },
        )
        assert resp.status_code == 400, resp.text

    async def test_confirm_password_reset_invalidates_old_tokens(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """After password reset, old auth tokens no longer work."""
        # Register + login
        await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "invalidate_tokens@example.com",
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        login_resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": "invalidate_tokens@example.com",
                "password": "password123",
            },
        )
        assert login_resp.status_code == 200
        old_token = login_resp.json()["token"]

        # Generate reset token
        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_field
        from ppbase.services.auth_service import create_password_reset_token
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        coll = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_field(
            engine, coll, "email", "invalidate_tokens@example.com"
        )

        token = create_password_reset_token(
            raw["id"],
            coll.id,
            raw["email"],
            _get_token_secret(coll, "passwordResetToken", raw["token_key"]),
            _get_token_duration(coll, "passwordResetToken"),
        )

        # Confirm reset (changes token_key)
        resp = await app_client.post(
            "/api/collections/users/confirm-password-reset",
            json={
                "token": token,
                "password": "newpassword789",
                "passwordConfirm": "newpassword789",
            },
        )
        assert resp.status_code == 204

        # Old auth token should no longer work for refresh
        refresh_resp = await app_client.post(
            "/api/collections/users/auth-refresh",
            headers={"Authorization": old_token},
        )
        assert refresh_resp.status_code == 401


# =========================================================================
# End-to-End Flow Tests
# =========================================================================


class TestEndToEndFlows:
    """Comprehensive end-to-end tests."""

    async def test_full_registration_login_flow(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Register → Login → Use token for protected endpoint."""
        import uuid
        email = f"e2e_{uuid.uuid4().hex[:8]}@example.com"
        # Register
        reg_resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
                "name": "E2E User",
            },
        )
        assert reg_resp.status_code == 200
        user = reg_resp.json()
        assert user["email"] == email

        # Login
        login_resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": email,
                "password": "securepass123",
            },
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        # Use token to access records
        list_resp = await app_client.get(
            "/api/collections/users/records",
            headers={"Authorization": token},
        )
        assert list_resp.status_code == 200
        assert list_resp.json()["totalItems"] >= 1

    async def test_full_verification_flow(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Register → Request verification → Confirm → verified=true."""
        import uuid
        email = f"e2e_verify_{uuid.uuid4().hex[:8]}@example.com"
        # Register
        reg_resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        assert reg_resp.status_code == 200
        user = reg_resp.json()
        assert user["verified"] is False

        # Request verification (returns 204)
        req_resp = await app_client.post(
            "/api/collections/users/request-verification",
            json={"email": email},
        )
        assert req_resp.status_code == 204

        # Generate token directly for confirmation
        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_id
        from ppbase.services.auth_service import create_verification_token
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        coll = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_id(engine, coll, user["id"])

        token = create_verification_token(
            raw["id"],
            coll.id,
            raw["email"],
            _get_token_secret(coll, "verificationToken", raw["token_key"]),
            _get_token_duration(coll, "verificationToken"),
        )

        # Confirm verification
        confirm_resp = await app_client.post(
            "/api/collections/users/confirm-verification",
            json={"token": token},
        )
        assert confirm_resp.status_code == 204

        # Check record is verified
        view_resp = await app_client.get(
            f"/api/collections/users/records/{user['id']}",
        )
        assert view_resp.status_code == 200
        assert view_resp.json()["verified"] is True

    async def test_full_password_reset_flow(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Register → Request reset → Confirm reset → Login with new password."""
        import uuid
        email = f"e2e_reset_{uuid.uuid4().hex[:8]}@example.com"
        # Register
        await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "oldpassword123",
                "passwordConfirm": "oldpassword123",
            },
        )

        # Request reset
        req_resp = await app_client.post(
            "/api/collections/users/request-password-reset",
            json={"email": email},
        )
        assert req_resp.status_code == 204

        # Generate reset token directly
        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_field
        from ppbase.services.auth_service import create_password_reset_token
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        coll = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_field(engine, coll, "email", email)
        assert raw is not None, f"Record not found for {email}"

        token = create_password_reset_token(
            raw["id"],
            coll.id,
            raw["email"],
            _get_token_secret(coll, "passwordResetToken", raw["token_key"]),
            _get_token_duration(coll, "passwordResetToken"),
        )

        # Confirm reset
        confirm_resp = await app_client.post(
            "/api/collections/users/confirm-password-reset",
            json={
                "token": token,
                "password": "brandnewpass456",
                "passwordConfirm": "brandnewpass456",
            },
        )
        assert confirm_resp.status_code == 204

        # Login with new password
        login_resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": email,
                "password": "brandnewpass456",
            },
        )
        assert login_resp.status_code == 200

        # Old password doesn't work
        old_resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": email,
                "password": "oldpassword123",
            },
        )
        assert old_resp.status_code == 400

    async def test_rule_with_auth_record(
        self,
        app_client: AsyncClient,
        admin_token: str,
    ):
        """Create collection with rule → Login → Only own records visible."""
        import uuid
        coll_name = f"notes_{uuid.uuid4().hex[:8]}"
        # Create a collection with auth rules
        coll_resp = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": coll_name,
                "type": "auth",
                "schema": [
                    {"name": "content", "type": "text"},
                ],
                "createRule": "",
                "listRule": 'id = @request.auth.id',
                "viewRule": 'id = @request.auth.id',
                "updateRule": 'id = @request.auth.id',
                "deleteRule": 'id = @request.auth.id',
            },
        )
        assert coll_resp.status_code == 200, coll_resp.text

        # Register two users
        for email, name in [
            ("note_user1@example.com", "User1"),
            ("note_user2@example.com", "User2"),
        ]:
            await app_client.post(
                f"/api/collections/{coll_name}/records",
                json={
                    "email": email,
                    "password": "securepass123",
                    "passwordConfirm": "securepass123",
                    "content": f"Note by {name}",
                },
            )

        # Login as user1
        login_resp = await app_client.post(
            f"/api/collections/{coll_name}/auth-with-password",
            json={
                "identity": "note_user1@example.com",
                "password": "securepass123",
            },
        )
        assert login_resp.status_code == 200
        token1 = login_resp.json()["token"]

        # List records with user1's token — should only see own record
        list_resp = await app_client.get(
            f"/api/collections/{coll_name}/records",
            headers={"Authorization": token1},
        )
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        assert len(items) == 1
        assert items[0]["email"] == "note_user1@example.com"

    async def test_token_refresh_returns_valid_token(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Register → Login → Refresh → Use new token."""
        import uuid
        email = f"refresh_e2e_{uuid.uuid4().hex[:8]}@example.com"
        # Register + Login
        await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        login_resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": email,
                "password": "securepass123",
            },
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        # Refresh
        refresh_resp = await app_client.post(
            "/api/collections/users/auth-refresh",
            headers={"Authorization": token},
        )
        assert refresh_resp.status_code == 200
        new_token = refresh_resp.json()["token"]
        assert new_token != ""

        # Use new token for a request
        list_resp = await app_client.get(
            "/api/collections/users/records",
            headers={"Authorization": new_token},
        )
        assert list_resp.status_code == 200

    async def test_password_hash_never_leaks(
        self,
        app_client: AsyncClient,
        admin_token: str,
        auth_collection: dict,
    ):
        """Verify password_hash and token_key never appear in any response."""
        import uuid
        email = f"noleak_{uuid.uuid4().hex[:8]}@example.com"
        # Register
        reg_resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        assert reg_resp.status_code == 200
        user = reg_resp.json()
        user_id = user["id"]

        # Check create response
        assert "password_hash" not in reg_resp.text
        assert "token_key" not in reg_resp.text

        # Check get single record
        get_resp = await app_client.get(
            f"/api/collections/users/records/{user_id}",
        )
        assert "password_hash" not in get_resp.text
        assert "token_key" not in get_resp.text

        # Check list records
        list_resp = await app_client.get(
            "/api/collections/users/records",
        )
        assert "password_hash" not in list_resp.text
        assert "token_key" not in list_resp.text

        # Check login response
        login_resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": email,
                "password": "securepass123",
            },
        )
        assert "password_hash" not in login_resp.text
        assert "token_key" not in login_resp.text

        # Check admin view
        admin_get = await app_client.get(
            f"/api/collections/users/records/{user_id}",
            headers={"Authorization": admin_token},
        )
        assert "password_hash" not in admin_get.text
        assert "token_key" not in admin_get.text


# =========================================================================
# Email Validation Tests
# =========================================================================


class TestEmailValidation:
    """Tests for email format validation on auth collection records."""

    async def test_register_invalid_email_format(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Registration fails for an invalid email format."""
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "not-an-email",
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp.status_code == 400, resp.text
        data = resp.json()
        assert "email" in data.get("data", {})
        assert data["data"]["email"]["code"] == "validation_invalid_email"

    async def test_register_email_no_domain(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Registration fails for email without a proper domain."""
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "user@",
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp.status_code == 400, resp.text
        data = resp.json()
        assert "email" in data.get("data", {})
        assert data["data"]["email"]["code"] == "validation_invalid_email"

    async def test_register_email_no_at_sign(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Registration fails for email without @ sign."""
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": "justtext",
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp.status_code == 400, resp.text
        data = resp.json()
        assert "email" in data.get("data", {})
        assert data["data"]["email"]["code"] == "validation_invalid_email"

    async def test_register_valid_email_succeeds(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
    ):
        """Registration succeeds for a properly formatted email."""
        import uuid
        email = f"valid_{uuid.uuid4().hex[:8]}@example.com"
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["email"] == email


# =========================================================================
# Email Uniqueness on Update Tests
# =========================================================================


class TestEmailUniquenessOnUpdate:
    """Tests for email uniqueness when updating auth records."""

    async def test_update_email_to_existing_fails(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
        admin_token: str,
    ):
        """Updating email to one already in use returns proper error."""
        import uuid
        email1 = f"unique1_{uuid.uuid4().hex[:8]}@example.com"
        email2 = f"unique2_{uuid.uuid4().hex[:8]}@example.com"

        # Create two users
        resp1 = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email1,
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp1.status_code == 200
        user1 = resp1.json()

        resp2 = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email2,
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp2.status_code == 200

        # Try to update user1's email to user2's email
        patch_resp = await app_client.patch(
            f"/api/collections/users/records/{user1['id']}",
            headers={"Authorization": admin_token},
            json={"email": email2},
        )
        assert patch_resp.status_code == 400, patch_resp.text
        data = patch_resp.json()
        assert "email" in data.get("data", {})
        assert data["data"]["email"]["code"] == "validation_not_unique"

    async def test_update_email_to_same_value_succeeds(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
        admin_token: str,
    ):
        """Updating a record with the same email (no change) succeeds."""
        import uuid
        email = f"same_{uuid.uuid4().hex[:8]}@example.com"
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp.status_code == 200
        user = resp.json()

        # Update with same email
        patch_resp = await app_client.patch(
            f"/api/collections/users/records/{user['id']}",
            headers={"Authorization": admin_token},
            json={"email": email},
        )
        assert patch_resp.status_code == 200, patch_resp.text

    async def test_update_email_invalid_format_fails(
        self,
        app_client: AsyncClient,
        auth_collection: dict,
        admin_token: str,
    ):
        """Updating email to invalid format fails."""
        import uuid
        email = f"fmt_{uuid.uuid4().hex[:8]}@example.com"
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "password123",
                "passwordConfirm": "password123",
            },
        )
        assert resp.status_code == 200
        user = resp.json()

        patch_resp = await app_client.patch(
            f"/api/collections/users/records/{user['id']}",
            headers={"Authorization": admin_token},
            json={"email": "not-valid"},
        )
        assert patch_resp.status_code == 400, patch_resp.text
        data = patch_resp.json()
        assert "email" in data.get("data", {})
        assert data["data"]["email"]["code"] == "validation_invalid_email"


# =========================================================================
# Expand/Fields on Auth Endpoints Tests
# =========================================================================


class TestAuthExpandFields:
    """Tests for expand and fields support on auth-with-password and auth-refresh."""

    @pytest_asyncio.fixture
    async def user_with_relation(
        self,
        app_client: AsyncClient,
        admin_token: str,
        auth_collection: dict,
    ):
        """Create a user and a related collection for expand testing."""
        import uuid
        suffix = uuid.uuid4().hex[:8]
        email = f"expand_{suffix}@example.com"

        # Register user
        resp = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
                "name": "Expand User",
            },
        )
        assert resp.status_code == 200, resp.text
        user = resp.json()
        user["_email"] = email
        return user

    async def test_auth_with_password_fields_filter(
        self,
        app_client: AsyncClient,
        user_with_relation: dict,
    ):
        """auth-with-password with ?fields=id,email returns only those fields on record."""
        email = user_with_relation["_email"]
        resp = await app_client.post(
            "/api/collections/users/auth-with-password?fields=id,email",
            json={
                "identity": email,
                "password": "securepass123",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "token" in data
        record = data["record"]
        assert "id" in record
        assert "email" in record
        # Other fields should be filtered out
        assert "collectionId" not in record
        assert "collectionName" not in record

    async def test_auth_refresh_fields_filter(
        self,
        app_client: AsyncClient,
        user_with_relation: dict,
    ):
        """auth-refresh with ?fields=id,email returns filtered record."""
        email = user_with_relation["_email"]
        # Login first
        login_resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={
                "identity": email,
                "password": "securepass123",
            },
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]

        # Refresh with fields filter
        resp = await app_client.post(
            "/api/collections/users/auth-refresh?fields=id,email",
            headers={"Authorization": token},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "token" in data
        record = data["record"]
        assert "id" in record
        assert "email" in record
        assert "collectionId" not in record

    async def test_auth_with_password_no_expand_no_crash(
        self,
        app_client: AsyncClient,
        user_with_relation: dict,
    ):
        """auth-with-password with ?expand=nonexistent doesn't crash."""
        email = user_with_relation["_email"]
        resp = await app_client.post(
            "/api/collections/users/auth-with-password?expand=nonexistent",
            json={
                "identity": email,
                "password": "securepass123",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "token" in data
        assert "record" in data
