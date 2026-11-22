import os
import pytest
from fastapi.testclient import TestClient

def test_turso_cloud_connection_and_table_init(monkeypatch):
    url = "https://voice-cloner-db-justifedayo.aws-eu-central-1.turso.io"
    token = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTUzMTE1MzgsImlkIjoiMDFhMjAwZTQtN2UwMS03ODIzLTg2MzEtZGYwODNhMDBmNDlmIiwia2lkIjoiRVhjcGNmODNKdFJkb0Q5NWdGaWxONEpMUWswQXcwamV6d3F5QUlVX1hDOCIsInJpZCI6Ijc0OGQzZTAxLThlOGYtNGI4Ni04MmQ0LTgxNTEyN2MwNjdjOSJ9.3ink6D2S0sM5lykSaQNeHroxDoE1aw2nPkk5H9Ds7PA-8IpdBAsxWZIkYFYp-TBNOsh4g1mpaBFGxRL-SQNlBg"

    monkeypatch.setenv("TURSO_DATABASE_URL", url)
    monkeypatch.setenv("TURSO_AUTH_TOKEN", token)
    monkeypatch.setenv("JWT_SECRET", "super-secret-development-key-change-in-production-32chars")

    import app as studio_app
    import libsql_client

    turso_client = libsql_client.create_client_sync(url=url, auth_token=token)
    turso_conn = studio_app.TursoConnection(turso_client)

    # Test executing init_db schema queries against Turso
    c = turso_conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL COLLATE NOCASE,
        email TEXT UNIQUE NOT NULL COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        credits INTEGER DEFAULT 2000,
        status TEXT DEFAULT 'active',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase ON users (username COLLATE NOCASE)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_nocase ON users (email COLLATE NOCASE)")

    # Test insertion & lastrowid
    test_email = "test_persistence_turso@teststudio.local"
    c.execute("DELETE FROM users WHERE email = ?", (test_email,))
    c.execute("INSERT INTO users (username, email, password_hash, role, credits, status) VALUES (?, ?, ?, 'user', 2000, 'active')",
              ("test_turso_user", test_email, "dummyhash"))
    assert c.lastrowid is not None
    uid = c.lastrowid

    # Test select & dict conversion
    c.execute("SELECT id, username, email, role, credits, status FROM users WHERE id = ?", (uid,))
    row = c.fetchone()
    assert row is not None
    assert row["username"] == "test_turso_user"
    assert row["email"] == test_email
    d = dict(row)
    assert d["credits"] == 2000

    # Test update & rowcount
    c.execute("UPDATE users SET credits = credits + 500 WHERE id = ?", (uid,))
    assert c.rowcount >= 1

    # Test soft-delete behavior
    del_ts = 12345
    c.execute("UPDATE users SET status = 'deleted', username = 'deleted_' || id || '_' || ?, email = 'deleted_' || id || '_' || ? WHERE id = ?",
              (del_ts, del_ts, uid))
    c.execute("SELECT status, username, email FROM users WHERE id = ?", (uid,))
    del_row = c.fetchone()
    assert del_row["status"] == "deleted"
    assert "deleted_" in del_row["username"]

    # Cleanup test row
    c.execute("DELETE FROM users WHERE id = ?", (uid,))
    turso_client.close()

def test_config_endpoint_reflects_persistence():
    import app as studio_app
    client = TestClient(studio_app.app)
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "database_backend" in data
    assert "is_cloud_database" in data
    assert "is_persistent_storage" in data
