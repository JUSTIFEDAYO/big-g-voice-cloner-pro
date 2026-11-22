"""
Hardening verification tests for:
1. Support ticket idempotency and dedicated ADMIN_NOTIFY_EMAIL
2. Google OAuth strict issuer and token expiration validation
3. Atomic profile changes (username changes staged until email verification)
4. SQLite WAL mode, busy timeout, and point-in-time snapshot backup & rotation
"""

import os
import time
import uuid
import pytest
from fastapi.testclient import TestClient

os.environ["TESTING"] = "1"
os.environ["JWT_SECRET"] = "test-secret-key-32-chars-long-for-testing-purpose"

import app as studio_app
from tests.test_production_suite import helper_signup_and_verify, client

def test_support_ticket_idempotency_and_admin_notify_email(client, monkeypatch):
    """
    1. Dedicated ADMIN_NOTIFY_EMAIL takes precedence over SMTP_USER.
    2. Repeated submission with the same idempotency_key returns the original ticket without duplicate email.
    """
    client.cookies.clear()
    u = f"idemp_{uuid.uuid4().hex[:6]}"
    helper_signup_and_verify(client, u, f"{u}@example.com", "Password123!")

    dispatched = []
    def mock_send(to_email, subject, **kwargs):
        dispatched.append({"to": to_email, "subject": subject})
        return {"sent": True, "reason": "OK"}

    monkeypatch.setattr(studio_app, "send_smtp_email", mock_send)
    monkeypatch.setenv("ADMIN_NOTIFY_EMAIL", "security-admin@studio.com")
    monkeypatch.setenv("SMTP_USER", "fallback@studio.com")

    idemp_key = f"key_{uuid.uuid4().hex}"
    ticket_payload = {
        "category": "Billing Inquiry",
        "subject": "Double charged credits",
        "message": "Please review my credit transaction logs.",
        "idempotency_key": idemp_key
    }

    # First submission
    res1 = client.post("/api/support/ticket", json=ticket_payload)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["success"] is True
    assert data1["idempotent"] is False
    ticket_id = data1["ticket_id"]

    # Verify notification went to ADMIN_NOTIFY_EMAIL
    assert len(dispatched) == 1
    assert dispatched[0]["to"] == "security-admin@studio.com"

    # Second submission (identical idempotency_key)
    res2 = client.post("/api/support/ticket", json=ticket_payload)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["success"] is True
    assert data2["ticket_id"] == ticket_id
    assert data2["idempotent"] is True

    # No additional email should have been dispatched
    assert len(dispatched) == 1

def test_google_auth_strict_issuer_and_expiration(client, monkeypatch):
    """
    Google OAuth validation explicitly rejects invalid issuers and expired tokens.
    """
    import httpx

    fake_email = f"googtest_{uuid.uuid4().hex[:6]}@gmail.com"

    class MockGoogleResp:
        def __init__(self, payload, status_code=200):
            self._payload = payload
            self.status_code = status_code
        def json(self):
            return self._payload

    current_payload = {}
    async def mock_get(self, url, *args, **kwargs):
        return MockGoogleResp(current_payload)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    # Case A: Invalid issuer
    current_payload = {
        "aud": studio_app.GOOGLE_CLIENT_ID,
        "iss": "https://malicious-issuer.com",
        "exp": time.time() + 3600,
        "email": fake_email,
        "email_verified": "true"
    }
    res_bad_iss = client.post("/api/auth/google", json={"credential": "mock_cred_1"})
    assert res_bad_iss.status_code == 400
    assert "Invalid Google token issuer" in res_bad_iss.json()["detail"]

    # Case B: Expired token
    current_payload = {
        "aud": studio_app.GOOGLE_CLIENT_ID,
        "iss": "accounts.google.com",
        "exp": time.time() - 100,  # Expired in past
        "email": fake_email,
        "email_verified": "true"
    }
    res_expired = client.post("/api/auth/google", json={"credential": "mock_cred_2"})
    assert res_expired.status_code == 400
    assert "Google authentication token has expired" in res_expired.json()["detail"]

    # Case C: Valid issuer and valid expiration
    current_payload = {
        "aud": studio_app.GOOGLE_CLIENT_ID,
        "iss": "https://accounts.google.com",
        "exp": time.time() + 3600,
        "email": fake_email,
        "email_verified": "true"
    }
    res_valid = client.post("/api/auth/google", json={"credential": "mock_cred_3"})
    assert res_valid.status_code == 200
    assert res_valid.json()["success"] is True

def test_atomic_profile_update_username_not_prematurely_changed(client):
    """
    When a user updates both username and email:
    - If email verification is pending (or abandoned), the username in the database remains unchanged.
    - Only after valid email verification is submitted are both username and email updated atomically.
    """
    client.cookies.clear()
    original_user = f"atom_{uuid.uuid4().hex[:6]}"
    orig_email = f"{original_user}@example.com"
    s = helper_signup_and_verify(client, original_user, orig_email, "Password123!")
    user_id = s["user"]["id"]

    new_user = f"new_{uuid.uuid4().hex[:6]}"
    new_email = f"pending_{uuid.uuid4().hex[:6]}@example.com"

    # Request change for both username and email
    res = client.put("/api/auth/profile", json={"username": new_user, "email": new_email})
    assert res.status_code == 200
    assert res.json()["requires_verification"] is True

    # Inspect users table: username must STILL be original_user!
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT username, email FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    assert row["username"].lower() == original_user.lower()
    assert row["email"].lower() == orig_email.lower()

    # Now simulate verification code submission
    c.execute("SELECT token_hash FROM email_verifications WHERE user_id = ? AND purpose = 'email_change'", (user_id,))
    v_hash = c.fetchone()[0]
    conn.close()

    import hashlib
    found_code = None
    for n in range(100000, 1000000):
        if hashlib.sha256(str(n).encode("utf-8")).hexdigest() == v_hash:
            found_code = str(n)
            break
    assert found_code is not None

    apply_res = client.put("/api/auth/profile", json={"email": new_email, "verification_code": found_code})
    assert apply_res.status_code == 200
    data = apply_res.json()
    assert data["user"]["username"] == new_user
    assert data["user"]["email"] == new_email

    # Verify both changed in database atomically
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT username, email FROM users WHERE id = ?", (user_id,))
    final_row = c.fetchone()
    conn.close()
    assert final_row["username"].lower() == new_user.lower()
    assert final_row["email"].lower() == new_email.lower()

def test_sqlite_wal_mode_and_snapshot_backup(tmp_path):
    """
    Verify:
    1. get_db_connection enables WAL mode and busy_timeout.
    2. backup_database produces a valid snapshot and rotates older backups.
    """
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("PRAGMA journal_mode")
    journal_mode = c.fetchone()[0].lower()
    c.execute("PRAGMA busy_timeout")
    busy_timeout = c.fetchone()[0]
    conn.close()

    assert journal_mode == "wal"
    assert busy_timeout == 5000

    backup_dir = str(tmp_path / "backups_test")
    
    # Generate 7 backups to verify rotation keeping top 5
    created_backups = []
    for i in range(7):
        bfile = studio_app.backup_database(backup_dir=backup_dir)
        assert bfile is not None
        assert os.path.exists(bfile)
        created_backups.append(bfile)
        time.sleep(0.01)

    remaining = [f for f in os.listdir(backup_dir) if f.startswith("studio_backup_") and f.endswith(".db")]
    assert len(remaining) == 5
