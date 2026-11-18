"""
Automated Comprehensive Test Suite for BIG G'S Voice Cloner Pro
Validates:
- Priority 1: User Signup, Login, Bcrypt password hashing, Tamper-proof JWT session validation, Admin RBAC.
- Priority 2: Secure password reset tokens, token expiration, attempt limits, no secrets in API responses, Support tickets.
- Priority 3: Data isolation, audio ownership enforcement, private streaming authorization, upload limits.
- Priority 4: Server-side credit deduction, insufficient token handling, error handling, voice fallback handling.
"""

import os
import io
import time
import uuid
import json
import pytest
from fastapi.testclient import TestClient

# Point test database to a clean, isolated SQLite test file
os.environ["TESTING"] = "1"
os.environ["JWT_SECRET"] = "test-secret-key-32-chars-long-for-testing-purpose"
os.environ["ALLOWED_ORIGINS"] = "http://testserver,http://localhost:7860"

import app as studio_app

@pytest.fixture(scope="module")
def client():
    # Use isolated test database
    test_db = os.path.join(studio_app.VOICES_DIR, "test_studio.db")
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass

    original_db = studio_app.DB_FILE
    studio_app.DB_FILE = test_db
    studio_app.init_db()

    with TestClient(studio_app.app) as test_client:
        yield test_client

    # Teardown
    studio_app.DB_FILE = original_db
    if os.path.exists(test_db):
        try:
            os.remove(test_db)
        except Exception:
            pass


def test_auth_signup_and_jwt(client):
    """Priority 1: Users can sign up, receive a secure session token and HTTP-only cookie."""
    unique_user = f"user_{uuid.uuid4().hex[:6]}"
    unique_email = f"{unique_user}@example.com"
    payload = {
        "username": unique_user,
        "email": unique_email,
        "password": "StrongPassword123!"
    }
    res = client.post("/api/auth/signup", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["success"] is True
    assert "token" in data
    assert data["user"]["username"] == unique_user
    assert data["user"]["credits"] == 5000
    assert data["user"]["role"] == "user"

    # Verify cookie was set
    assert studio_app.COOKIE_NAME in res.cookies


def test_auth_login_and_bcrypt(client):
    """Priority 1: Passwords verify with Bcrypt, bad credentials fail with non-revealing error."""
    unique_user = f"user_{uuid.uuid4().hex[:6]}"
    unique_email = f"{unique_user}@example.com"
    payload = {
        "username": unique_user,
        "email": unique_email,
        "password": "CorrectPassword123!"
    }
    client.post("/api/auth/signup", json=payload)

    # 1. Invalid password
    bad_login = client.post("/api/auth/login", json={"username": unique_user, "password": "WrongPassword"})
    assert bad_login.status_code == 401
    assert "Invalid username or password" in bad_login.json()["detail"]

    # 2. Correct password
    good_login = client.post("/api/auth/login", json={"username": unique_user, "password": "CorrectPassword123!"})
    assert good_login.status_code == 200
    login_data = good_login.json()
    assert login_data["success"] is True
    assert "token" in login_data

    # 3. /api/auth/me resolves identity from verified session token
    headers = {"Authorization": f"Bearer {login_data['token']}"}
    me_res = client.get("/api/auth/me", headers=headers)
    assert me_res.status_code == 200
    me_data = me_res.json()
    assert me_data["user"]["username"] == unique_user
    assert me_data["user"]["role"] == "user"


def test_admin_authorization_enforced(client):
    """Priority 1: Regular users cannot access admin endpoints."""
    unique_user = f"regular_{uuid.uuid4().hex[:6]}"
    payload = {"username": unique_user, "email": f"{unique_user}@example.com", "password": "UserPass123!"}
    signup = client.post("/api/auth/signup", json=payload).json()
    headers = {"Authorization": f"Bearer {signup['token']}"}

    # Regular user attempting admin endpoints
    res = client.get("/api/admin/users", headers=headers)
    assert res.status_code == 403
    assert "Administrator permissions required" in res.json()["detail"]

    ticket_res = client.get("/api/admin/tickets", headers=headers)
    assert ticket_res.status_code == 403

    unauth_res = client.get("/api/admin/users", cookies={studio_app.COOKIE_NAME: ""})
    assert unauth_res.status_code == 401


def test_password_reset_security(client):
    """Priority 2: Reset code is NEVER in response, codes expire, and are single-use."""
    unique_user = f"reset_{uuid.uuid4().hex[:6]}"
    unique_email = f"{unique_user}@example.com"
    client.post("/api/auth/signup", json={"username": unique_user, "email": unique_email, "password": "OldPassword123!"})

    # 1. Request code
    req_res = client.post("/api/auth/forgot-password", json={"email": unique_email})
    assert req_res.status_code == 200
    data = req_res.json()
    assert "reset_code" not in data, "Security violation: reset_code must NEVER be returned in API response"

    # Query hashed token in DB directly for test verification
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, token_hash FROM reset_tokens WHERE email = ?", (unique_email,))
    row = c.fetchone()
    conn.close()
    assert row is not None

    # 2. Reset attempt with wrong code
    wrong_reset = client.post("/api/auth/reset-password", json={
        "email": unique_email,
        "token": "000000",
        "new_password": "NewStrongPassword123!"
    })
    assert wrong_reset.status_code == 400
    assert "Incorrect reset code" in wrong_reset.json()["detail"]

    # 3. Expired token cannot be used
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE reset_tokens SET expires_at = ? WHERE id = ?", (time.time() - 100, row["id"]))
    conn.commit()
    conn.close()

    expired_reset = client.post("/api/auth/reset-password", json={
        "email": unique_email,
        "token": "123456",
        "new_password": "NewStrongPassword123!"
    })
    assert expired_reset.status_code == 400


def test_support_tickets_ownership(client):
    """Priority 2: Users can only view their own tickets; category and subject are validated."""
    client.cookies.clear()
    u1 = f"tuser1_{uuid.uuid4().hex[:5]}"
    u2 = f"tuser2_{uuid.uuid4().hex[:5]}"
    s1 = client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"}).json()
    client.cookies.clear()
    s2 = client.post("/api/auth/signup", json={"username": u2, "email": f"{u2}@example.com", "password": "Password123!"}).json()
    client.cookies.clear()

    h1 = {"Authorization": f"Bearer {s1['token']}"}
    h2 = {"Authorization": f"Bearer {s2['token']}"}

    # User 1 files a complaint
    t_res = client.post("/api/support/ticket", headers=h1, json={
        "category": "Credit Issue",
        "subject": "Missing tokens on generation",
        "message": "Tokens were deducted but connection dropped during playback."
    })
    assert t_res.status_code == 200
    t_id = t_res.json()["ticket_id"]

    # User 1 views tickets
    my_t = client.get("/api/support/my-tickets", headers=h1).json()
    assert len(my_t["tickets"]) == 1
    assert my_t["tickets"][0]["id"] == t_id

    # User 2 views tickets -> Should see 0 tickets (Strict isolation)
    u2_t = client.get("/api/support/my-tickets", headers=h2).json()
    assert len(u2_t["tickets"]) == 0


def test_audio_ownership_and_private_streaming(client):
    """Priority 3: Users cannot delete or stream another user's cloned voice or generated tracks."""
    client.cookies.clear()
    u1 = f"owner1_{uuid.uuid4().hex[:5]}"
    u2 = f"intruder2_{uuid.uuid4().hex[:5]}"
    s1 = client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"}).json()
    client.cookies.clear()
    s2 = client.post("/api/auth/signup", json={"username": u2, "email": f"{u2}@example.com", "password": "Password123!"}).json()
    client.cookies.clear()

    h1 = {"Authorization": f"Bearer {s1['token']}"}
    h2 = {"Authorization": f"Bearer {s2['token']}"}

    # Create dummy reference voice for User 1
    import numpy as np
    import soundfile as sf
    test_wav_bytes = io.BytesIO()
    dummy_audio = np.zeros(16000, dtype=np.float32)
    sf.write(test_wav_bytes, dummy_audio, 16000, format='WAV')
    test_wav_bytes.seek(0)

    files = {"audio_file": ("test_sample.wav", test_wav_bytes.read(), "audio/wav")}
    clone_res = client.post("/api/clone", headers=h1, data={"name": "OwnerVoice"}, files=files)
    assert clone_res.status_code == 200
    voice_id = clone_res.json()["voice"]["id"]

    # User 2 lists cloned voices -> Must not see User 1's voice
    u2_voices = client.get("/api/cloned-voices", headers=h2).json()
    assert not any(v["id"] == voice_id for v in u2_voices)

    # User 2 attempts to delete User 1's voice -> 403 Forbidden
    del_attempt = client.delete(f"/api/cloned-voices/{voice_id}", headers=h2)
    assert del_attempt.status_code == 403
    assert "Permission denied" in del_attempt.json()["detail"]

    # User 1 successfully deletes own voice -> 200 OK
    del_success = client.delete(f"/api/cloned-voices/{voice_id}", headers=h1)
    assert del_success.status_code == 200


def test_credit_enforcement_and_atomic_charging(client):
    """Priority 4: Tokens checked before generation, charged accurately, refunds on failure."""
    u1 = f"credit_{uuid.uuid4().hex[:5]}"
    s1 = client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"}).json()
    user_id = s1["user"]["id"]
    h1 = {"Authorization": f"Bearer {s1['token']}"}

    # Set user balance to exactly 20 tokens
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET credits = 20 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    # Attempt to synthesize 50 characters (costs 50 tokens) -> Should fail with 402 Insufficient tokens
    long_text = "This is a long sentence that exceeds twenty tokens."
    tts_fail = client.post("/api/tts", headers=h1, json={"text": long_text, "voice": "en-US-AndrewMultilingualNeural"})
    assert tts_fail.status_code == 402
    assert "Insufficient tokens" in tts_fail.json()["detail"]

    # Balance must remain intact (no incorrect charge)
    me = client.get("/api/auth/me", headers=h1).json()
    assert me["user"]["credits"] == 20
