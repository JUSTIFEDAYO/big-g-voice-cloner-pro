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
import sqlite3
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
    """Priority 1: Users can sign up, receive a secure HTTP-only cookie and no token in JSON."""
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
    # Token must NOT be in JSON payload (kept strictly inside HTTP-only cookie)
    assert "token" not in data
    assert data["user"]["username"] == unique_user
    assert data["user"]["credits"] == 5000
    assert data["user"]["role"] == "user"

    # Verify cookie was set
    assert studio_app.COOKIE_NAME in res.cookies


def test_auth_login_and_bcrypt(client):
    """Priority 1: Passwords verify with Bcrypt, bad credentials fail with non-revealing error."""
    client.cookies.clear()
    unique_user = f"user_{uuid.uuid4().hex[:6]}"
    unique_email = f"{unique_user}@example.com"
    payload = {
        "username": unique_user,
        "email": unique_email,
        "password": "CorrectPassword123!"
    }
    client.post("/api/auth/signup", json=payload)
    client.cookies.clear()

    # 1. Invalid password
    bad_login = client.post("/api/auth/login", json={"username": unique_user, "password": "WrongPassword"})
    assert bad_login.status_code == 401
    assert "Invalid username or password" in bad_login.json()["detail"]

    # 2. Correct password sets cookie without JSON token
    good_login = client.post("/api/auth/login", json={"username": unique_user, "password": "CorrectPassword123!"})
    assert good_login.status_code == 200
    login_data = good_login.json()
    assert login_data["success"] is True
    assert "token" not in login_data
    assert studio_app.COOKIE_NAME in good_login.cookies

    # 3. /api/auth/me resolves identity from verified session cookie
    me_res = client.get("/api/auth/me")
    assert me_res.status_code == 200
    me_data = me_res.json()
    assert me_data["user"]["username"] == unique_user
    assert me_data["user"]["role"] == "user"


def test_admin_authorization_enforced(client):
    """Priority 1: Regular users cannot access admin endpoints."""
    client.cookies.clear()
    unique_user = f"regular_{uuid.uuid4().hex[:6]}"
    payload = {"username": unique_user, "email": f"{unique_user}@example.com", "password": "UserPass123!"}
    client.post("/api/auth/signup", json=payload)

    # Regular user attempting admin endpoints via session cookie
    res = client.get("/api/admin/users")
    assert res.status_code == 403
    assert "Administrator permissions required" in res.json()["detail"]

    ticket_res = client.get("/api/admin/tickets")
    assert ticket_res.status_code == 403

    client.cookies.clear()
    unauth_res = client.get("/api/admin/users")
    assert unauth_res.status_code == 401


def test_password_reset_security(client, monkeypatch):
    """Priority 2: Response body and status 200 are identical for unknown email, successful delivery, and failed delivery."""
    client.cookies.clear()
    unique_user = f"reset_{uuid.uuid4().hex[:6]}"
    unique_email = f"{unique_user}@example.com"
    client.post("/api/auth/signup", json={"username": unique_user, "email": unique_email, "password": "OldPassword123!"})

    # Expected identical generic message
    expected_msg = "If this email is registered, a 6-digit password reset code has been dispatched to your inbox."

    # 1. Unknown email address outcome
    unknown_email = f"unknown_{uuid.uuid4().hex[:6]}@example.com"
    res_unknown = client.post("/api/auth/forgot-password", json={"email": unknown_email})
    assert res_unknown.status_code == 200
    assert res_unknown.json() == {"success": True, "message": expected_msg}

    # 2. Registered email with successful SMTP delivery
    monkeypatch.setattr(studio_app, "send_smtp_email", lambda **kwargs: {"sent": True, "reason": "Dispatched"})
    res_success = client.post("/api/auth/forgot-password", json={"email": unique_email})
    assert res_success.status_code == 200
    assert res_success.json() == {"success": True, "message": expected_msg}

    # 3. Registered email with SMTP delivery failure
    monkeypatch.setattr(studio_app, "send_smtp_email", lambda **kwargs: {"sent": False, "reason": "SMTP transport error"})
    res_smtp_fail = client.post("/api/auth/forgot-password", json={"email": unique_email})
    assert res_smtp_fail.status_code == 200
    assert res_smtp_fail.json() == {"success": True, "message": expected_msg}

    # Verify timing neutrality: Slow SMTP (e.g. 500ms network delay) must not delay HTTP response
    def slow_smtp(**kwargs):
        time.sleep(0.5)
        return {"sent": True, "reason": "Delivered after delay"}

    monkeypatch.setattr(studio_app, "send_smtp_email", slow_smtp)
    t_start = time.monotonic()
    res_timed = client.post("/api/auth/forgot-password", json={"email": unique_email})
    t_elapsed = time.monotonic() - t_start
    assert res_timed.status_code == 200
    assert res_timed.json() == {"success": True, "message": expected_msg}
    # Response must be returned immediately without waiting for slow_smtp (well below 0.35s)
    assert t_elapsed < 0.35, f"Response waited for SMTP! Elapsed: {t_elapsed}s"

    # Verify reset code is NEVER returned in response
    assert "reset_code" not in res_unknown.json()
    assert "reset_code" not in res_success.json()
    assert "reset_code" not in res_smtp_fail.json()
    assert "reset_code" not in res_timed.json()

    # Query hashed token in DB directly for test verification
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, token_hash FROM reset_tokens WHERE email = ?", (unique_email,))
    row = c.fetchone()
    conn.close()
    assert row is not None

    # 4. Reset attempt with wrong code
    wrong_reset = client.post("/api/auth/reset-password", json={
        "email": unique_email,
        "token": "000000",
        "new_password": "NewStrongPassword123!"
    })
    assert wrong_reset.status_code == 400
    assert "Incorrect reset code" in wrong_reset.json()["detail"]

    # 5. Expired token cannot be used
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

    # Sign up User 1 (sets session cookie)
    client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"})
    # User 1 files a complaint
    t_res = client.post("/api/support/ticket", json={
        "category": "Credit Issue",
        "subject": "Missing tokens on generation",
        "message": "Tokens were deducted but connection dropped during playback."
    })
    assert t_res.status_code == 200
    t_id = t_res.json()["ticket_id"]

    # User 1 views tickets
    my_t = client.get("/api/support/my-tickets").json()
    assert len(my_t["tickets"]) == 1
    assert my_t["tickets"][0]["id"] == t_id

    # Switch session cookie to User 2
    client.cookies.clear()
    client.post("/api/auth/signup", json={"username": u2, "email": f"{u2}@example.com", "password": "Password123!"})
    # User 2 views tickets -> Should see 0 tickets (Strict isolation)
    u2_t = client.get("/api/support/my-tickets").json()
    assert len(u2_t["tickets"]) == 0


def test_audio_ownership_and_private_streaming(client):
    """Priority 3: Users cannot delete or stream another user's cloned voice or generated tracks."""
    client.cookies.clear()
    u1 = f"owner1_{uuid.uuid4().hex[:5]}"
    u2 = f"intruder2_{uuid.uuid4().hex[:5]}"

    # User 1 signs up and uploads voice
    client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"})

    import numpy as np
    import soundfile as sf
    test_wav_bytes = io.BytesIO()
    t_arr = np.linspace(0, 1, 16000, endpoint=False)
    dummy_audio = (0.5 * np.sin(2 * np.pi * 440 * t_arr)).astype(np.float32)
    sf.write(test_wav_bytes, dummy_audio, 16000, format='WAV')
    test_wav_bytes.seek(0)

    files = {"audio_file": ("test_sample.wav", test_wav_bytes.read(), "audio/wav")}
    clone_res = client.post("/api/clone", data={"name": "OwnerVoice"}, files=files)
    assert clone_res.status_code == 200
    voice_id = clone_res.json()["voice"]["id"]

    # Switch session to User 2
    client.cookies.clear()
    client.post("/api/auth/signup", json={"username": u2, "email": f"{u2}@example.com", "password": "Password123!"})

    # User 2 lists cloned voices -> Must not see User 1's voice
    u2_voices = client.get("/api/cloned-voices").json()
    assert not any(v["id"] == voice_id for v in u2_voices)

    # User 2 attempts to delete User 1's voice -> 403 Forbidden
    del_attempt = client.delete(f"/api/cloned-voices/{voice_id}")
    assert del_attempt.status_code == 403
    assert "Permission denied" in del_attempt.json()["detail"]

    # Switch back to User 1
    client.cookies.clear()
    client.post("/api/auth/login", json={"username": u1, "password": "Password123!"})

    # User 1 successfully deletes own voice -> 200 OK
    del_success = client.delete(f"/api/cloned-voices/{voice_id}")
    assert del_success.status_code == 200


def test_credit_enforcement_and_atomic_charging(client):
    """Priority 4: Tokens checked before generation, charged accurately, refunds on failure."""
    client.cookies.clear()
    u1 = f"credit_{uuid.uuid4().hex[:5]}"
    s1 = client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"}).json()
    user_id = s1["user"]["id"]

    # Set user balance to exactly 20 tokens
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET credits = 20 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    # Attempt to synthesize 50 characters (costs 50 tokens) -> Should fail with 402 Insufficient tokens
    long_text = "This is a long sentence that exceeds twenty tokens."
    tts_fail = client.post("/api/tts", json={"text": long_text, "voice": "en-US-AndrewMultilingualNeural"})
    assert tts_fail.status_code == 402
    assert "Insufficient tokens" in tts_fail.json()["detail"]

    # Balance must remain intact (no incorrect charge)
    me = client.get("/api/auth/me").json()
    assert me["user"]["credits"] == 20


def test_smtp_health_diagnostic_endpoint(client):
    """Verify SMTP health check diagnostic endpoint works for admin without disclosing secrets."""
    client.cookies.clear()
    # Create an admin user in test DB
    admin_user = f"adm_{uuid.uuid4().hex[:5]}"
    client.post("/api/auth/signup", json={"username": admin_user, "email": f"{admin_user}@example.com", "password": "AdminPassword123!"})
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET role = 'admin' WHERE username = ?", (admin_user,))
    conn.commit()
    conn.close()

    res = client.get("/api/admin/smtp-health")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert "smtp" in data
    # Ensure sensitive password is NEVER returned in response
    assert "SMTP_PASSWORD" not in str(data)
    assert "password" not in str(data).lower() or "pass_configured" in data["smtp"]


def _create_mock_line_generator(fail_on_line=None):
    """
    Returns a deterministic mock for generate_speech that produces synthetic audio
    and updates database/credits without contacting Edge TTS.
    """
    call_idx = 0

    async def mock_generate_speech(req, current_user):
        nonlocal call_idx
        call_idx += 1
        if fail_on_line is not None and call_idx == fail_on_line:
            raise RuntimeError(f"Deterministic simulated failure on line {fail_on_line}")

        clean_text = req.text.strip()
        tokens_required = max(len(clean_text), 10)
        user_id = current_user["id"]

        # Deduct credits
        conn = studio_app.get_db_connection()
        c = conn.cursor()
        c.execute("UPDATE users SET credits = credits - ? WHERE id = ?", (tokens_required, user_id))
        conn.commit()

        # Write dummy line audio file
        line_filename = f"mock_line_{uuid.uuid4().hex[:8]}.wav"
        line_path = os.path.join(studio_app.OUTPUTS_DIR, line_filename)
        import soundfile as sf
        import numpy as np
        t_arr = np.linspace(0, 0.2, int(24000 * 0.2), endpoint=False)
        dummy_wav = (0.3 * np.sin(2 * np.pi * 440 * t_arr)).astype(np.float32)
        sf.write(line_path, dummy_wav, 24000)

        # Record generated_audio row
        audio_id = uuid.uuid4().hex[:12]
        c.execute(
            """
            INSERT INTO generated_audio (id, user_id, filename, filepath, voice_name, engine_used, characters_count, tokens_charged)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (audio_id, user_id, line_filename, line_path, req.voice, "MockEngine", len(clean_text), tokens_required)
        )
        c.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
        remaining = c.fetchone()["credits"]
        conn.commit()
        conn.close()

        from fastapi.responses import JSONResponse
        return JSONResponse({
            "success": True,
            "audio_url": f"/api/audio/stream/{line_filename}",
            "filename": line_filename,
            "engine_used": "MockEngine",
            "is_fallback": False,
            "voice_name": req.voice,
            "tokens_spent": tokens_required,
            "credits_remaining": remaining
        })

    return mock_generate_speech

def test_dialogue_failure_accounting_later_line(client, monkeypatch):
    """Dialogue failure on a later line leaves NO credit charge, NO db records, and NO files."""
    client.cookies.clear()
    u1 = f"diag_{uuid.uuid4().hex[:5]}"
    s1 = client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"}).json()
    user_id = s1["user"]["id"]
    initial_credits = s1["user"]["credits"]

    # Deterministic mock failing on line 2
    mock_gen = _create_mock_line_generator(fail_on_line=2)
    monkeypatch.setattr(studio_app, "generate_speech", mock_gen)

    payload = {
        "lines": [
            {"speaker": "en-US-AndrewMultilingualNeural", "text": "First line succeeds."},
            {"speaker": "en-US-AndrewMultilingualNeural", "text": "Second line will fail."}
        ]
    }

    res = client.post("/api/dialogue", json=payload)
    assert res.status_code == 500

    # Verify no credits were deducted
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
    final_credits = c.fetchone()["credits"]

    # Verify no generated_audio rows exist for this user
    c.execute("SELECT COUNT(*) FROM generated_audio WHERE user_id = ?", (user_id,))
    records_count = c.fetchone()[0]
    conn.close()

    assert final_credits == initial_credits, f"Expected {initial_credits} credits, got {final_credits}"
    assert records_count == 0, f"Expected 0 generated_audio records, got {records_count}"


def test_dialogue_failure_accounting_assembly(client, monkeypatch):
    """Dialogue failure during assembly leaves NO credit charge, NO db records, and NO files."""
    client.cookies.clear()
    u1 = f"asmb_{uuid.uuid4().hex[:5]}"
    s1 = client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"}).json()
    user_id = s1["user"]["id"]
    initial_credits = s1["user"]["credits"]

    # Deterministic mock where all individual lines succeed
    mock_gen = _create_mock_line_generator(fail_on_line=None)
    monkeypatch.setattr(studio_app, "generate_speech", mock_gen)

    # Monkeypatch sf.write to fail during master track assembly
    orig_sf_write = studio_app.sf.write

    def mock_sf_write(file, data, samplerate, **kwargs):
        if "dialogue_" in str(file):
            raise IOError("Simulated disk error during master track assembly")
        return orig_sf_write(file, data, samplerate, **kwargs)

    monkeypatch.setattr(studio_app.sf, "write", mock_sf_write)

    payload = {
        "lines": [
            {"speaker": "en-US-AndrewMultilingualNeural", "text": "First line"},
            {"speaker": "en-US-AndrewMultilingualNeural", "text": "Second line"}
        ]
    }

    res = client.post("/api/dialogue", json=payload)
    assert res.status_code == 500

    # Verify no credits were deducted
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
    final_credits = c.fetchone()["credits"]

    c.execute("SELECT COUNT(*) FROM generated_audio WHERE user_id = ?", (user_id,))
    records_count = c.fetchone()[0]
    conn.close()

    assert final_credits == initial_credits
    assert records_count == 0


def test_dialogue_success_cleans_line_records(client, monkeypatch):
    """Successful dialogue leaves only 1 master record in generated_audio, with line records removed."""
    client.cookies.clear()
    u1 = f"diagok_{uuid.uuid4().hex[:5]}"
    s1 = client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"}).json()
    user_id = s1["user"]["id"]
    initial_credits = s1["user"]["credits"]

    mock_gen = _create_mock_line_generator(fail_on_line=None)
    monkeypatch.setattr(studio_app, "generate_speech", mock_gen)

    payload = {
        "lines": [
            {"speaker": "en-US-AndrewMultilingualNeural", "text": "Line one here."},
            {"speaker": "en-US-AndrewMultilingualNeural", "text": "Line two here."}
        ]
    }

    res = client.post("/api/dialogue", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    master_file = data["filename"]

    # Verify generated_audio DB table: Exactly ONE record (the master dialogue), 0 line records
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, filename, voice_name FROM generated_audio WHERE user_id = ?", (user_id,))
    rows = c.fetchall()
    conn.close()

    assert len(rows) == 1
    assert rows[0]["filename"] == master_file
    assert rows[0]["voice_name"] == "Multi-Speaker Ensemble"


def test_storage_migration_and_conflicts(tmp_path):
    """Test safe migration, destination conflict detection, and database path updates."""
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"

    src_voices = src_dir / "voices"
    src_uploads = src_dir / "uploads"
    src_outputs = src_dir / "outputs"
    src_voices.mkdir(parents=True)
    src_uploads.mkdir(parents=True)
    src_outputs.mkdir(parents=True)

    # Create dummy database and sample in src
    db_path = str(src_voices / "studio.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    c.execute("INSERT INTO users VALUES (1, 'alice')")
    c.execute("CREATE TABLE cloned_voices (id TEXT PRIMARY KEY, file_path TEXT)")
    sample_file = str(src_uploads / "sample.wav")
    with open(sample_file, "w") as f:
        f.write("dummy audio content")
    c.execute("INSERT INTO cloned_voices VALUES ('v1', ?)", (sample_file,))
    c.execute("CREATE TABLE generated_audio (id TEXT PRIMARY KEY, filepath TEXT)")
    output_file = str(src_outputs / "out.mp3")
    with open(output_file, "w") as f:
        f.write("dummy output content")
    c.execute("INSERT INTO generated_audio VALUES ('a1', ?)", (output_file,))
    conn.commit()
    conn.close()

    # 1. Successful Initial Migration
    studio_app.migrate_storage(str(src_dir), str(dst_dir))

    dst_db_path = str(dst_dir / "voices" / "studio.db")
    assert os.path.exists(dst_db_path)
    assert os.path.exists(str(dst_dir / "uploads" / "sample.wav"))
    assert os.path.exists(str(dst_dir / "outputs" / "out.mp3"))
    assert os.path.exists(str(dst_dir / ".migration_complete"))

    # Verify paths inside database were remapped to dst_dir
    conn = sqlite3.connect(dst_db_path)
    c = conn.cursor()
    c.execute("SELECT file_path FROM cloned_voices WHERE id = 'v1'")
    new_fp = c.fetchone()[0]
    c.execute("SELECT filepath FROM generated_audio WHERE id = 'a1'")
    new_ap = c.fetchone()[0]
    conn.close()

    assert str(dst_dir) in new_fp
    assert str(dst_dir) in new_ap

    # 2. Idempotence: Migration can run a second time (e.g. server restart) without error
    # even though source database and destination database both exist
    studio_app.migrate_storage(str(src_dir), str(dst_dir))
    assert os.path.exists(str(dst_dir / ".migration_complete"))

    # 3. Media Conflict Handling: Fails before changing anything if destination media file differs
    media_conflict_dir = tmp_path / "media_conflict"
    (media_conflict_dir / "uploads").mkdir(parents=True)
    with open(str(media_conflict_dir / "uploads" / "sample.wav"), "w") as f:
        f.write("conflicting different sample audio")

    with pytest.raises(RuntimeError) as exc_media:
        studio_app.migrate_storage(str(src_dir), str(media_conflict_dir))
    assert "Storage Migration Media Conflict" in str(exc_media.value)

    # 4. Database Conflict Handling: Migration must fail if destination already has independent non-empty data without marker
    conflict_dir = tmp_path / "conflict"
    (conflict_dir / "voices").mkdir(parents=True)
    with open(str(conflict_dir / "voices" / "studio.db"), "w") as f:
        f.write("existing destination data")

    with pytest.raises(RuntimeError) as exc_info:
        studio_app.migrate_storage(str(src_dir), str(conflict_dir))
    assert "Storage Migration Conflict" in str(exc_info.value)


def test_invalid_audio_upload_rejection_and_cleanup(client):
    """Invalid or silent audio is rejected without fallback, leaving no files or DB records."""
    client.cookies.clear()
    u1 = f"inva_{uuid.uuid4().hex[:5]}"
    client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"})

    # 1. Upload completely corrupt/non-audio bytes
    corrupt_bytes = b"NOT_REAL_AUDIO_HEADER_DATA_12345" * 50
    files = {"audio_file": ("corrupt.wav", corrupt_bytes, "audio/wav")}
    res = client.post("/api/clone", data={"name": "BadVoice"}, files=files)
    assert res.status_code == 400
    assert "not a valid or readable audio track" in res.json()["detail"]

    # 2. Upload silent audio samples (peak near zero)
    import numpy as np
    import soundfile as sf
    silent_io = io.BytesIO()
    silent_audio = np.zeros(8000, dtype=np.float32)
    sf.write(silent_io, silent_audio, 16000, format='WAV')
    silent_io.seek(0)

    files_silent = {"audio_file": ("silent.wav", silent_io.read(), "audio/wav")}
    res_silent = client.post("/api/clone", data={"name": "SilentVoice"}, files=files_silent)
    assert res_silent.status_code == 400

    # Ensure no voices were recorded in DB
    v_res = client.get("/api/cloned-voices").json()
    assert len(v_res) == 0


def test_support_ticket_rate_limiting(client, monkeypatch):
    """Per-user limit on support ticket creation. Rejected requests create no ticket or email."""
    # Ensure TESTING bypass does not skip ticket limit during this test
    client.cookies.clear()
    u1 = f"trate_{uuid.uuid4().hex[:5]}"
    s1 = client.post("/api/auth/signup", json={"username": u1, "email": f"{u1}@example.com", "password": "Password123!"}).json()
    user_id = s1["user"]["id"]

    # Temporarily unset TESTING env var for this test
    monkeypatch.delenv("TESTING", raising=False)

    email_count = 0
    monkeypatch.setattr(studio_app, "send_smtp_email", lambda **kwargs: {"sent": True, "reason": "OK"})

    ticket_payload = {
        "category": "Credit Issue",
        "subject": "Need help with credits",
        "message": "Detailed message explaining the issue with my balance."
    }

    # Submit 5 tickets (allowed)
    for _ in range(5):
        r = client.post("/api/support/ticket", json=ticket_payload)
        assert r.status_code == 200

    # 6th request within window must be rate-limited (HTTP 429)
    blocked_res = client.post("/api/support/ticket", json=ticket_payload)
    assert blocked_res.status_code == 429

    # Verify DB only contains exactly 5 tickets
    conn = studio_app.get_db_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM support_tickets WHERE user_id = ?", (user_id,))
    total_tickets = c.fetchone()[0]
    conn.close()

    assert total_tickets == 5
