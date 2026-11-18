"""
BIG G'S VOICE CLONER PRO - Next-Gen Commercial Voice AI & Audio Studio
Production Hardened & Enterprise Secured Architecture

Security & Privacy Guarantees:
- Server-side JWT session validation with HTTP-only cookies and Bearer auth fallback.
- True Role-Based Access Control (RBAC): verified user identities; tamper-proof token claims.
- Strict ownership enforcement for cloned voices, audio uploads, synthesized tracks, and support tickets.
- Zero credential disclosure: no hardcoded credentials, startup promotion only via explicit environment variables.
- Password hashing with Bcrypt (cost factor 12) + SHA-256 legacy verification auto-upgrade.
- Rate limiting / request throttling on auth, reset, cloning, and audio synthesis endpoints.
- Single-use, hashed, rate-limited, expiring password reset tokens (never leaked in API responses).
- Private audio streams authenticated through ownership checks; no public folder traversal.
- Automated cleanup and retention for stale uploads and temporary renders.
- Exact server-side credit balance charging and atomic transaction accounting.
- Whitelabel engine abstraction with clear indicators for zero-shot vs neural fallback.
"""

import os
import json
import uuid
import asyncio
import time
import shutil
import io
import re
import html
import sqlite3
import hashlib
import smtplib
import threading
import logging
from collections import defaultdict
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import bcrypt
import jwt
import numpy as np
import soundfile as sf
import edge_tts
import httpx
import msgpack
import speech_recognition as sr
from dotenv import load_dotenv

from fastapi import (
    FastAPI,
    UploadFile,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    Depends,
    BackgroundTasks,
    status
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Load environment configuration
load_dotenv(override=False)

# Structured, Privacy-Safe Logger Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("VoiceClonerPro")

# Base File Paths & Storage Persistence
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_ROOT = BASE_DIR

def _file_contents_differ(file_a: str, file_b: str) -> bool:
    """Returns True if two files have different size or content."""
    if not (os.path.exists(file_a) and os.path.exists(file_b)):
        return True
    if os.path.getsize(file_a) != os.path.getsize(file_b):
        return True
    # Compare in 64KB chunks
    with open(file_a, "rb") as fa, open(file_b, "rb") as fb:
        while True:
            ca = fa.read(65536)
            cb = fb.read(65536)
            if ca != cb:
                return True
            if not ca:
                break
    return False

def migrate_storage(source_root: str, target_root: str):
    """
    Safely migrates SQLite database and audio files when PERSISTENT_DATA_DIR is configured.
    - Idempotent: If migration was previously completed (marker present), cleanly skips.
    - Conflict check: Inspects database and media files (uploads, outputs, voices) before copying.
      If destination files already exist and differ in content from source, halts safely before changing anything.
    - Preserves file references and updates DB stored paths.
    - Writes a persistent '.migration_complete' marker on success.
    """
    source_root = os.path.abspath(source_root)
    target_root = os.path.abspath(target_root)
    if source_root == target_root:
        return

    marker_file = os.path.join(target_root, ".migration_complete")
    if os.path.exists(marker_file):
        logger.info(f"Storage migration marker found in '{target_root}'. Migration already completed; skipping.")
        return

    src_db = os.path.join(source_root, "voices", "studio.db")
    dst_db = os.path.join(target_root, "voices", "studio.db")

    # 1. Conflict detection phase: Database check
    if os.path.exists(src_db) and os.path.exists(dst_db):
        if os.path.getsize(src_db) > 0 and os.path.getsize(dst_db) > 0:
            if _file_contents_differ(src_db, dst_db):
                msg = (
                    f"Storage Migration Conflict: Persistent destination database '{dst_db}' already exists "
                    f"with differing content from source '{src_db}'. Cannot safely overwrite or merge. "
                    f"To preserve existing data, please inspect or backup the destination before starting."
                )
                logger.error(msg)
                raise RuntimeError(msg)

    # 2. Conflict detection phase: Media files check (before copying anything)
    for subfolder in ["voices", "uploads", "outputs"]:
        src_sub = os.path.join(source_root, subfolder)
        dst_sub = os.path.join(target_root, subfolder)
        if os.path.exists(src_sub):
            for fname in os.listdir(src_sub):
                s_file = os.path.join(src_sub, fname)
                d_file = os.path.join(dst_sub, fname)
                if os.path.isfile(s_file) and os.path.exists(d_file):
                    if _file_contents_differ(s_file, d_file):
                        msg = (
                            f"Storage Migration Media Conflict: Destination file '{d_file}' already exists "
                            f"with differing content from source '{s_file}'. Cannot proceed safely without risking data corruption."
                        )
                        logger.error(msg)
                        raise RuntimeError(msg)

    # 3. Copy / Migration phase
    # Migrate voices directory
    src_voices = os.path.join(source_root, "voices")
    dst_voices = os.path.join(target_root, "voices")
    os.makedirs(dst_voices, exist_ok=True)
    if os.path.exists(src_voices):
        for fname in os.listdir(src_voices):
            s_file = os.path.join(src_voices, fname)
            d_file = os.path.join(dst_voices, fname)
            if os.path.isfile(s_file):
                if not os.path.exists(d_file):
                    shutil.copy2(s_file, d_file)
                elif _file_contents_differ(s_file, d_file) and fname == "studio.db" and os.path.getsize(d_file) == 0:
                    shutil.copy2(s_file, d_file)

    # Migrate uploads directory
    src_uploads = os.path.join(source_root, "uploads")
    dst_uploads = os.path.join(target_root, "uploads")
    os.makedirs(dst_uploads, exist_ok=True)
    if os.path.exists(src_uploads):
        for fname in os.listdir(src_uploads):
            s_file = os.path.join(src_uploads, fname)
            d_file = os.path.join(dst_uploads, fname)
            if os.path.isfile(s_file) and not os.path.exists(d_file):
                shutil.copy2(s_file, d_file)

    # Migrate outputs directory
    src_outputs = os.path.join(source_root, "outputs")
    dst_outputs = os.path.join(target_root, "outputs")
    os.makedirs(dst_outputs, exist_ok=True)
    if os.path.exists(src_outputs):
        for fname in os.listdir(src_outputs):
            s_file = os.path.join(src_outputs, fname)
            d_file = os.path.join(dst_outputs, fname)
            if os.path.isfile(s_file) and not os.path.exists(d_file):
                shutil.copy2(s_file, d_file)

    # 4. Update database file paths to target_root if dst_db exists
    if os.path.exists(dst_db):
        conn = None
        try:
            conn = sqlite3.connect(dst_db)
            c = conn.cursor()
            # Update cloned_voices file_path if pointing to source_root
            c.execute("SELECT id, file_path FROM cloned_voices")
            for row in c.fetchall():
                v_id, old_fp = row[0], row[1]
                if old_fp and source_root in old_fp:
                    new_fp = old_fp.replace(source_root, target_root)
                    c.execute("UPDATE cloned_voices SET file_path = ? WHERE id = ?", (new_fp, v_id))
            # Update generated_audio filepath if pointing to source_root
            c.execute("SELECT id, filepath FROM generated_audio")
            for row in c.fetchall():
                a_id, old_ap = row[0], row[1]
                if old_ap and source_root in old_ap:
                    new_ap = old_ap.replace(source_root, target_root)
                    c.execute("UPDATE generated_audio SET filepath = ? WHERE id = ?", (new_ap, a_id))
            conn.commit()
            logger.info(f"Successfully migrated storage from '{source_root}' to persistent directory '{target_root}'.")
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            logger.error(f"Storage path update failed during migration: {e}")
            raise RuntimeError(f"Storage migration failed during database path updates: {e}")
        finally:
            if conn:
                conn.close()

    # 5. Write migration completion marker ONLY after all migration and path updates succeed
    try:
        with open(marker_file, "w", encoding="utf-8") as mf:
            mf.write(f"migrated_from={source_root}\ntimestamp={time.time()}\n")
    except Exception as e:
        logger.error(f"Could not write migration marker file: {e}")
        raise RuntimeError(f"Storage migration failed writing completion marker: {e}")

PERSISTENT_DATA_DIR = os.getenv("PERSISTENT_DATA_DIR", "").strip()
if PERSISTENT_DATA_DIR:
    DATA_ROOT = os.path.abspath(PERSISTENT_DATA_DIR)
    # Perform safe migration if source has existing data and target is distinct
    if os.path.exists(os.path.join(BASE_DIR, "voices", "studio.db")):
        migrate_storage(BASE_DIR, DATA_ROOT)
else:
    DATA_ROOT = BASE_DIR

STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOADS_DIR = os.path.join(DATA_ROOT, "uploads")
VOICES_DIR = os.path.join(DATA_ROOT, "voices")
OUTPUTS_DIR = os.path.join(DATA_ROOT, "outputs")
DB_FILE = os.path.join(VOICES_DIR, "studio.db")
VOICES_CACHE_FILE = os.path.join(VOICES_DIR, "us_voices_cache.json")

for directory in [STATIC_DIR, UPLOADS_DIR, VOICES_DIR, OUTPUTS_DIR]:
    os.makedirs(directory, exist_ok=True)

# JWT & Session Configurations
_raw_jwt_secret = os.getenv("JWT_SECRET", "").strip()
IS_PRODUCTION = os.getenv("RENDER", "").lower() == "true" or os.getenv("ENVIRONMENT", "").lower() == "production"

# In production, require an explicitly configured strong JWT_SECRET
KNOWN_DEV_SECRETS = {
    "super-secret-development-key-change-in-production-32chars",
    "generate a 32+ character random secret string",
    "your_long_random_jwt_secret_hex_string_here"
}
if IS_PRODUCTION:
    if not _raw_jwt_secret or len(_raw_jwt_secret) < 32 or _raw_jwt_secret.lower() in KNOWN_DEV_SECRETS:
        logger.critical("FATAL: JWT_SECRET environment variable is missing, too short (<32 chars), or using a placeholder in production!")
        raise RuntimeError("Production startup halted: Strong JWT_SECRET (min 32 chars) must be configured in environment variables.")

JWT_SECRET = _raw_jwt_secret if _raw_jwt_secret else "fallback-dev-secret-key-32chars-for-local-testing-only"
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256").strip()
JWT_EXPIRATION_HOURS = int(os.getenv("JWT_EXPIRATION_HOURS", "72"))
COOKIE_NAME = "bigg_session_token"

# Bounded, Cleanable In-Memory Sliding-Window Rate Limiter
RATE_LIMIT_STORE: Dict[str, List[float]] = defaultdict(list)
_LAST_RATE_LIMIT_PURGE = time.monotonic()

def check_rate_limit(key: str, max_requests: int, window_seconds: float):
    """
    Enforces a sliding window rate limit per client key (e.g. client IP or action prefix).
    Bounded memory store with periodic automatic pruning.
    Raises HTTPException(429) if exceeded.
    """
    if os.getenv("TESTING") == "1":
        return

    global _LAST_RATE_LIMIT_PURGE
    now = time.monotonic()

    # Periodic global purge of dead keys every 5 minutes to prevent memory leaks
    if now - _LAST_RATE_LIMIT_PURGE > 300:
        dead_keys = [k for k, ts in RATE_LIMIT_STORE.items() if not ts or (now - ts[-1] > 3600)]
        for k in dead_keys:
            del RATE_LIMIT_STORE[k]
        _LAST_RATE_LIMIT_PURGE = now

    timestamps = RATE_LIMIT_STORE[key]
    cutoff = now - window_seconds
    while timestamps and timestamps[0] < cutoff:
        timestamps.pop(0)

    if len(timestamps) >= max_requests:
        retry_after = int(timestamps[0] + window_seconds - now) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many requests. Please slow down and try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)}
        )
    timestamps.append(now)

# Database Helpers
def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    """Initializes schema and upgrades legacy structures if necessary."""
    conn = get_db_connection()
    c = conn.cursor()
    
    # 1. Users Table
    c.execute('''
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
    ''')

    # Ensure case-insensitive unique indexes on username and email
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_nocase ON users (username COLLATE NOCASE)")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_nocase ON users (email COLLATE NOCASE)")

    # 1b. Email Verification Codes Table
    c.execute('''
    CREATE TABLE IF NOT EXISTS email_verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL COLLATE NOCASE,
        username TEXT NOT NULL COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        purpose TEXT NOT NULL DEFAULT 'signup',
        user_id INTEGER,
        expires_at REAL NOT NULL,
        attempts INTEGER DEFAULT 0,
        used INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # 2. Cloned Voices Table (ownership linked)
    c.execute('''
    CREATE TABLE IF NOT EXISTS cloned_voices (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        reference_text TEXT,
        tags TEXT,
        file_path TEXT NOT NULL,
        base_model TEXT,
        engine TEXT,
        profile_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    ''')

    # 3. Generated Audio Tracks Table (ownership linked)
    c.execute('''
    CREATE TABLE IF NOT EXISTS generated_audio (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        filename TEXT NOT NULL,
        filepath TEXT NOT NULL,
        voice_name TEXT,
        engine_used TEXT,
        characters_count INTEGER,
        tokens_charged INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    ''')

    # 4. Password Reset Tokens Table (Hashed tokens, single-use, rate-limited attempts)
    c.execute('''
    CREATE TABLE IF NOT EXISTS reset_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        token_hash TEXT NOT NULL,
        expires_at REAL NOT NULL,
        attempts INTEGER DEFAULT 0,
        used INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # 5. Support Tickets Table
    c.execute('''
    CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT NOT NULL,
        email TEXT NOT NULL,
        category TEXT NOT NULL,
        subject TEXT NOT NULL,
        message TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        admin_response TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()

    # Environment-based Admin Bootstrap / Promotion
    admin_env_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    admin_env_pass = os.getenv("ADMIN_INITIAL_PASSWORD", "").strip()

    if admin_env_email:
        c.execute("SELECT id, role FROM users WHERE email = ?", (admin_env_email,))
        existing_admin = c.fetchone()
        if existing_admin:
            if existing_admin["role"] != "admin":
                c.execute("UPDATE users SET role = 'admin', credits = 999999 WHERE id = ?", (existing_admin["id"],))
                logger.info(f"Promoted existing user {admin_env_email} to admin role via environment variable.")
        elif admin_env_pass and len(admin_env_pass) >= 8:
            hashed_admin_pass = hash_password(admin_env_pass)
            c.execute(
                "INSERT INTO users (username, email, password_hash, role, credits, status) VALUES (?, ?, ?, 'admin', 999999, 'active')",
                ("admin", admin_env_email, hashed_admin_pass)
            )
            logger.info(f"Bootstrapped new administrator account ({admin_env_email}) via environment variables.")
        conn.commit()

    conn.close()

# Password Hashing & Upgrading Utilities
def hash_password(password: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.strip().encode("utf-8"), salt).decode("utf-8")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        # Check standard bcrypt hash format
        if hashed_password.startswith("$2a$") or hashed_password.startswith("$2b$") or hashed_password.startswith("$2y$"):
            return bcrypt.checkpw(plain_password.strip().encode("utf-8"), hashed_password.encode("utf-8"))
        # Graceful migration for legacy SHA-256 hashes
        legacy_hash = hashlib.sha256(plain_password.strip().encode("utf-8")).hexdigest()
        return legacy_hash == hashed_password
    except Exception:
        return False

# Token Generation & Verification
def create_session_jwt(user_id: int, username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=JWT_EXPIRATION_HOURS)).timestamp())
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def decode_session_jwt(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired. Please sign in again.")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session token.")

# Fast Dependency: Current Verified User
def get_current_user(request: Request) -> Dict[str, Any]:
    # 1. Check HTTP-only cookie
    token = request.cookies.get(COOKIE_NAME)
    
    # 2. Check Authorization Bearer header
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")

    claims = decode_session_jwt(token)
    user_id = int(claims.get("sub", 0))

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, username, email, role, credits, status FROM users WHERE id = ?", (user_id,))
    user = c.fetchone()
    conn.close()

    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found.")
    if user["status"] == "banned":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended. Please contact support.")

    return dict(user)

def require_admin(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Administrator permissions required.")
    return current_user

# Automated Periodic File Cleanup
async def cleanup_stale_files():
    """Prunes temporary rendered audio and orphaned samples older than 24 hours."""
    while True:
        try:
            now = time.time()
            cutoff = now - (24 * 3600)  # 24 hours retention for generated temp files
            for folder in [OUTPUTS_DIR]:
                for fname in os.listdir(folder):
                    fpath = os.path.join(folder, fname)
                    if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                        try:
                            os.remove(fpath)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Error in automated cleanup task: {e}")
        await asyncio.sleep(3600)

# Email Dispatch & Verification
def check_smtp_health() -> Dict[str, Any]:
    """Diagnostic check for SMTP configuration without exposing secrets."""
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip())
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").strip()

    if not smtp_user or not smtp_pass:
        return {
            "configured": False,
            "host": smtp_host,
            "port": smtp_port,
            "user_configured": bool(smtp_user),
            "pass_configured": bool(smtp_pass),
            "status": "Missing SMTP_USER or SMTP_PASSWORD environment variables."
        }

    try:
        if smtp_port == 465:
            server_cls = smtplib.SMTP_SSL
            server = server_cls(smtp_host, smtp_port, timeout=8)
            server.ehlo()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=8)
            server.ehlo()
            server.starttls()
            server.ehlo()

        with server:
            server.login(smtp_user, smtp_pass)
        return {
            "configured": True,
            "host": smtp_host,
            "port": smtp_port,
            "user_configured": True,
            "pass_configured": True,
            "status": "SMTP credentials authenticated successfully with mail server."
        }
    except Exception as e:
        logger.warning(f"SMTP Health verification failed: {type(e).__name__}")
        return {
            "configured": False,
            "host": smtp_host,
            "port": smtp_port,
            "user_configured": True,
            "pass_configured": True,
            "status": f"SMTP authentication failed: {type(e).__name__}"
        }

def send_smtp_email(to_email: str, subject: str, html_content: str, text_content: str) -> dict:
    # Under test mode or if recipient domain is an example/test domain, skip sending real outbound email
    clean_to = to_email.strip().lower()
    if os.getenv("TESTING") == "1" or clean_to.endswith("@example.com") or clean_to.endswith("@test.com"):
        logger.info(f"Skipping actual SMTP dispatch to synthetic test address: {to_email}")
        return {"sent": True, "reason": "Simulated dispatch in test environment."}

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587").strip())
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").strip()

    if not smtp_user or not smtp_pass:
        logger.warning(f"SMTP delivery skipped for {to_email}: SMTP_USER or SMTP_PASSWORD not configured in environment.")
        return {"sent": False, "reason": "SMTP credentials not configured."}

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"BIG G Voice Cloner Pro <{smtp_user}>"
        msg["To"] = to_email

        part1 = MIMEText(text_content, "plain")
        part2 = MIMEText(html_content, "html")
        msg.attach(part1)
        msg.attach(part2)

        if smtp_port == 465:
            server_cls = smtplib.SMTP_SSL
            server = server_cls(smtp_host, smtp_port, timeout=12)
            server.ehlo()
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=12)
            server.ehlo()
            server.starttls()
            server.ehlo()

        with server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [to_email], msg.as_string())

        logger.info(f"Email successfully dispatched to {to_email}")
        return {"sent": True, "reason": "Email dispatched successfully."}
    except Exception as e:
        logger.error(f"Failed sending email to {to_email}: {type(e).__name__} - {e}")
        return {"sent": False, "reason": f"SMTP transport error: {type(e).__name__} - {e}"}

# Curated High-Fidelity Human Pro Voices
CURATED_HUMAN_VOICES = [
    {
        "id": "fish_7c0a85bc08f3454c9e29dba716eef0ab",
        "fish_id": "7c0a85bc08f3454c9e29dba716eef0ab",
        "name": "Marcus - Deep American Documentary",
        "gender": "Male",
        "locale": "en-US",
        "category": "Cinematic & Narration",
        "description": "Ultra-rich, deep American narrator voice (Morgan Freeman style).",
        "avatar": "🎙️",
        "is_fish_pro": True
    },
    {
        "id": "fish_933563129e564b19a115bedd57b7406a",
        "fish_id": "933563129e564b19a115bedd57b7406a",
        "name": "Sarah - Natural American Conversational",
        "gender": "Female",
        "locale": "en-US",
        "category": "Podcast & Everyday",
        "description": "Warm, relatable, authentic American young adult conversational voice.",
        "avatar": "🎧",
        "is_fish_pro": True
    },
    {
        "id": "fish_bf322df2096a46f18c579d0baa36f41d",
        "fish_id": "bf322df2096a46f18c579d0baa36f41d",
        "name": "Arthur - British BBC Scholar",
        "gender": "Male",
        "locale": "en-GB",
        "category": "UK Narration & Audiobook",
        "description": "Aristocratic, distinguished British English scholar with BBC cadence.",
        "avatar": "🎩",
        "is_fish_pro": True
    },
    {
        "id": "fish_802e3bc2b27e49c2995d23ef70e6ac89",
        "fish_id": "802e3bc2b27e49c2995d23ef70e6ac89",
        "name": "Jordan - American Commercial & Tech",
        "gender": "Male",
        "locale": "en-US",
        "category": "Commercial & Tech",
        "description": "Crisp, dynamic corporate voice ideal for product overviews and advertisements.",
        "avatar": "💼",
        "is_fish_pro": True
    },
    {
        "id": "fish_d8b3ca0dbfc0460c9103d32fa8df05fc",
        "fish_id": "d8b3ca0dbfc0460c9103d32fa8df05fc",
        "name": "Evelyn - Gentle British Storyteller",
        "gender": "Female",
        "locale": "en-GB",
        "category": "Audiobook & UK Drama",
        "description": "Poetic, elegant British female voice with soothing, gentle cadence.",
        "avatar": "📖",
        "is_fish_pro": True
    },
    {
        "id": "fish_6fa730641de144038d185bb35b31787b",
        "fish_id": "6fa730641de144038d185bb35b31787b",
        "name": "Seraphina - Emotional Cinema",
        "gender": "Female",
        "locale": "en-US",
        "category": "Drama & Cinema",
        "description": "Soft, emotive narrator with subtle natural whisper pauses.",
        "avatar": "✨",
        "is_fish_pro": True
    },
    {
        "id": "fish_c5f56a6cc2ec4fa8920cb4c5889a3fb7",
        "fish_id": "c5f56a6cc2ec4fa8920cb4c5889a3fb7",
        "name": "Slax - Clear American Radio Host",
        "gender": "Male",
        "locale": "en-US",
        "category": "Podcast & Broadcast",
        "description": "Smooth, articulate, balanced broadcast voice for news and journalism.",
        "avatar": "📻",
        "is_fish_pro": True
    }
]

# Audio Constants & Helpers
ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}
MAX_AUDIO_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

async def read_bounded_upload(upload_file: UploadFile, max_bytes: int = MAX_AUDIO_UPLOAD_BYTES, chunk_size: int = 64 * 1024) -> bytes:
    """
    Reads an uploaded file in bounded chunks, immediately aborting if size exceeds max_bytes.
    Prevents loading unbounded or oversized files into server memory.
    """
    buffer = io.BytesIO()
    total_read = 0
    while True:
        chunk = await upload_file.read(chunk_size)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"Audio file exceeds maximum allowed limit of {max_bytes // (1024 * 1024)} MB."
            )
        buffer.write(chunk)
    return buffer.getvalue()

def safe_remove_managed_file(file_path: str, allowed_dirs: Optional[List[str]] = None) -> bool:
    """
    Safely deletes a file only if its canonical path is located inside allowed managed directories.
    Prevents path traversal or accidental deletion outside uploads/outputs.
    """
    if not file_path:
        return False
    if allowed_dirs is None:
        allowed_dirs = [UPLOADS_DIR, OUTPUTS_DIR]

    canonical_target = os.path.realpath(file_path)
    # Check if target is inside any of allowed_dirs
    is_safe = False
    for ad in allowed_dirs:
        canonical_dir = os.path.realpath(ad)
        try:
            common = os.path.commonpath([canonical_target, canonical_dir])
            if common == canonical_dir and canonical_target != canonical_dir:
                is_safe = True
                break
        except ValueError:
            continue

    if not is_safe:
        logger.warning(f"File deletion rejected: '{file_path}' is outside managed directories: {allowed_dirs}")
        return False

    if os.path.isfile(canonical_target):
        try:
            os.remove(canonical_target)
            return True
        except Exception as e:
            logger.warning(f"Could not remove file '{canonical_target}': {e}")
    return False

def preprocess_reference_audio(input_path: str, output_path: str) -> str:
    """
    Strict audio validation and normalization.
    Raises ValueError if input is not valid non-empty audio.
    """
    try:
        data, sr_val = sf.read(input_path)
    except Exception as e:
        raise ValueError(f"Could not read audio file: {e}")

    if data is None or len(data) == 0:
        raise ValueError("Audio data is empty.")

    if len(data.shape) > 1:
        data = data.mean(axis=1)

    # Check for non-zero audio content
    peak = np.max(np.abs(data))
    if peak <= 1e-6:
        raise ValueError("Audio file contains only silence or near-zero samples.")

    data = data - np.mean(data)
    data = (data / peak) * 0.95
    sf.write(output_path, data, sr_val)
    return output_path

def analyze_audio_profile(file_path: str) -> dict:
    """
    Analyzes valid acoustic profile.
    Raises ValueError if file cannot be decoded as valid non-empty audio.
    """
    try:
        data, samplerate = sf.read(file_path)
    except Exception as e:
        raise ValueError(f"Audio profile decoding failed: {e}")

    if data is None or len(data) == 0:
        raise ValueError("Audio file for profile analysis is empty.")

    if len(data.shape) > 1:
        data = data.mean(axis=1)

    duration = float(len(data) / samplerate)
    if duration < 0.1:
        raise ValueError(f"Audio duration {duration:.2f}s is too short to clone.")

    energy = float(np.sqrt(np.mean(data**2)))
    if energy <= 1e-6:
        raise ValueError("Audio file contains no audible acoustic energy.")

    corr = np.correlate(data[:min(len(data), 16000)], data[:min(len(data), 16000)], mode='full')
    corr = corr[len(corr)//2:]
    d = np.diff(corr)
    start = np.where(d > 0)[0]
    if len(start) > 0:
        peak = np.argmax(corr[start[0]:]) + start[0]
        estimated_pitch = float(samplerate / peak) if peak > 0 else 180.0
    else:
        estimated_pitch = 180.0

    gender = "Female" if estimated_pitch > 165 else "Male"
    pitch_shift_pct = int(np.clip(((estimated_pitch - 160) / 160) * 40, -40, 40))

    return {
        "duration": round(duration, 2),
        "samplerate": samplerate,
        "estimated_pitch_hz": round(estimated_pitch, 1),
        "gender_affinity": gender,
        "recommended_pitch_mod": f"{'+' if pitch_shift_pct >= 0 else ''}{pitch_shift_pct}%",
        "energy_db": round(20 * np.log10(energy + 1e-6), 1)
    }

ALLOWED_EFFECTS = {"none", "reverb", "telephone", "radio", "bass_boost", "cyberpunk"}

def apply_dsp_effects(file_path: str, effect: str) -> str:
    if effect not in ALLOWED_EFFECTS or effect == "none":
        return os.path.basename(file_path)

    try:
        data, sr = sf.read(file_path)
        if len(data.shape) > 1:
            data = data.mean(axis=1)

        out_name = f"fx_{effect}_{uuid.uuid4().hex[:8]}.wav"
        out_path = os.path.join(OUTPUTS_DIR, out_name)

        if effect == "reverb":
            decay = np.exp(-np.linspace(0, 5, int(sr * 0.45)))
            ir = np.random.randn(len(decay)) * decay
            convolved = np.convolve(data, ir, mode='full')[:len(data)]
            processed = 0.7 * data + 0.3 * convolved
        elif effect == "telephone":
            fft = np.fft.rfft(data)
            freqs = np.fft.rfftfreq(len(data), 1.0 / sr)
            fft[(freqs < 400) | (freqs > 3400)] *= 0.05
            processed = np.fft.irfft(fft, n=len(data))
            processed = np.tanh(processed * 2.2) * 0.8
        elif effect == "radio":
            fft = np.fft.rfft(data)
            freqs = np.fft.rfftfreq(len(data), 1.0 / sr)
            fft[(freqs < 300) | (freqs > 4500)] *= 0.02
            processed = np.fft.irfft(fft, n=len(data))
            processed = np.clip(processed * 1.5, -0.7, 0.7)
        elif effect == "bass_boost":
            fft = np.fft.rfft(data)
            freqs = np.fft.rfftfreq(len(data), 1.0 / sr)
            fft[freqs < 220] *= 2.2
            processed = np.fft.irfft(fft, n=len(data))
        elif effect == "cyberpunk":
            delay_samples = int(sr * 0.025)
            echo = np.zeros_like(data)
            if len(data) > delay_samples:
                echo[delay_samples:] = data[:-delay_samples] * 0.65
            processed = data + echo
        else:
            processed = data

        max_val = np.max(np.abs(processed))
        if max_val > 0:
            processed = (processed / max_val) * 0.95

        sf.write(out_path, processed, sr)
        return out_name
    except Exception as e:
        logger.warning(f"DSP Effect Error: {e}")
        return os.path.basename(file_path)

# Initialize Database on load
init_db()

# Fast API Application Definition
app = FastAPI(
    title="BIG G'S VOICE CLONER PRO",
    version="6.0.0",
    docs_url=None,       # Disable Swagger in prod
    redoc_url=None
)

# CORS Configuration (Restricted to Trusted Origins, no wildcard with credentials)
allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "http://localhost:7860,http://127.0.0.1:7860")
allowed_origins = [o.strip() for o in allowed_origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["http://localhost:7860"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)

# Global Startup Event
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(cleanup_stale_files())

# Pydantic Request Models
class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=30)
    email: str = Field(..., min_length=5, max_length=100)
    password: str = Field(..., min_length=6, max_length=128)

class VerifyEmailRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=100)
    code: str = Field(..., min_length=6, max_length=6)

class ResendVerificationRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=100)

class CheckUsernameRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)

class UpdateProfileRequest(BaseModel):
    username: Optional[str] = Field(None, min_length=3, max_length=30)
    email: Optional[str] = Field(None, min_length=5, max_length=100)
    verification_code: Optional[str] = Field(None, min_length=6, max_length=6)

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=100)
    password: str = Field(..., min_length=1, max_length=128)

class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=100)

class ResetPasswordRequest(BaseModel):
    email: str = Field(..., min_length=5, max_length=100)
    token: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=6, max_length=128)

class UpdateUserCreditsRequest(BaseModel):
    user_id: int
    credits: int = Field(..., ge=0, le=10000000)

class CreateTicketRequest(BaseModel):
    category: str = Field(..., min_length=2, max_length=50)
    subject: str = Field(..., min_length=3, max_length=150)
    message: str = Field(..., min_length=10, max_length=3000)

class ReplyTicketRequest(BaseModel):
    ticket_id: int
    response: str = Field(..., min_length=1, max_length=3000)
    status: Optional[str] = "open"

class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    voice: str = Field(..., min_length=2, max_length=100)
    engine: Optional[str] = "auto"
    rate: Optional[str] = "+0%"
    pitch: Optional[str] = "+0Hz"
    volume: Optional[str] = "+0%"
    temperature: Optional[float] = Field(0.7, ge=0.1, le=1.0)
    top_p: Optional[float] = Field(0.7, ge=0.1, le=1.0)
    chunk_length: Optional[int] = Field(200, ge=100, le=300)
    enhance_audio: Optional[bool] = False
    dsp_effect: Optional[str] = "none"

class DialogueLine(BaseModel):
    speaker: str = Field(..., min_length=2, max_length=100)
    speaker_name: Optional[str] = "Actor"
    text: str = Field(..., min_length=1, max_length=1000)

class DialogueRequest(BaseModel):
    lines: List[DialogueLine] = Field(..., min_length=1, max_length=25)


# -------------------------------------------------------------
# PRIORITY 1: Authentication & Authorization Endpoints
# -------------------------------------------------------------

def _send_verification_email_worker(email: str, username: str, code: str):
    """Background worker to send email verification codes."""
    escaped_user = html.escape(username)
    html_email = f"""
    <div style="font-family: Arial, sans-serif; background-color: #0b0f19; color: #f8fafc; padding: 30px; border-radius: 10px; max-width: 520px; margin: 0 auto; border: 1px solid #1e293b;">
        <h2 style="color: #6366f1; margin-top: 0;">BIG G'S VOICE CLONER PRO</h2>
        <p style="font-size: 15px; color: #cbd5e1;">Welcome, <strong>{escaped_user}</strong>!</p>
        <p style="font-size: 14px; color: #94a3b8; line-height: 1.5;">Please use the 6-digit verification code below to confirm your email and activate your account:</p>
        <div style="text-align: center; margin: 25px 0;">
            <span style="display: inline-block; font-size: 32px; font-weight: 800; letter-spacing: 8px; color: #38bdf8; background: #1e293b; padding: 12px 24px; border-radius: 8px; border: 1px solid #3b82f6;">{code}</span>
        </div>
        <p style="font-size: 13px; color: #64748b;">This verification code expires in 30 minutes. If you did not create this account, please ignore this email.</p>
        <hr style="border: 0; border-top: 1px solid #1e293b; margin: 20px 0;">
        <p style="font-size: 11px; color: #475569; text-align: center;">BIG G Voice Cloner Pro • Studio AI Audio Suite</p>
    </div>
    """
    text_email = f"Your BIG G Voice Cloner Pro email verification code is: {code}\nThis code expires in 30 minutes."

    delivery = send_smtp_email(
        to_email=email,
        subject=f"{code} is your Voice Cloner Pro verification code",
        html_content=html_email,
        text_content=text_email
    )
    if not delivery.get("sent", False):
        logger.warning(f"Background email verification delivery failure: {delivery.get('reason')}")

def _generate_username_suggestions(base_u: str, existing_usernames: set) -> List[str]:
    """Generates 3 available username suggestions based on base_u."""
    clean_base = re.sub(r"[^a-zA-Z0-9_\-]", "", base_u)[:20]
    if not clean_base:
        clean_base = "voiceartist"
    suggestions = []
    import random
    candidates = [
        f"{clean_base}{random.randint(10, 99)}",
        f"{clean_base}_{random.randint(100, 999)}",
        f"pro_{clean_base}",
        f"{clean_base}_ai",
        f"{clean_base}{random.randint(1000, 9999)}",
        f"studio_{clean_base}"
    ]
    for c in candidates:
        c_lower = c.lower()
        if c_lower not in existing_usernames and c_lower != base_u.lower():
            if c not in suggestions:
                suggestions.append(c)
        if len(suggestions) >= 3:
            break
    return suggestions

@app.post("/api/auth/check-username")
def check_username_availability(req: CheckUsernameRequest, request: Request):
    """Checks if a chosen username is available and returns suggestions if taken."""
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"check_user:{client_ip}", max_requests=30, window_seconds=60)

    u = req.username.strip().lower()
    if not re.match(r"^[a-zA-Z0-9_\-\.]{3,30}$", u):
        return JSONResponse({
            "available": False,
            "message": "Username must be 3-30 characters with letters, numbers, hyphens or underscores.",
            "suggestions": []
        })

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT username FROM users")
    all_users = {row["username"].lower() for row in c.fetchall()}
    conn.close()

    if u in all_users:
        suggestions = _generate_username_suggestions(u, all_users)
        return JSONResponse({
            "available": False,
            "message": f"Username '{req.username.strip()}' is already taken.",
            "suggestions": suggestions
        })

    return JSONResponse({
        "available": True,
        "message": "Username is available!",
        "suggestions": []
    })

@app.post("/api/auth/signup")
def auth_signup(req: SignupRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"signup:{client_ip}", max_requests=6, window_seconds=60)

    u = req.username.strip().lower()
    e = req.email.strip().lower()
    p = req.password.strip()

    if not re.match(r"^[a-zA-Z0-9_\-\.]{3,30}$", u):
        raise HTTPException(status_code=400, detail="Username must be 3-30 characters with letters, numbers, hyphens or underscores.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", e):
        raise HTTPException(status_code=400, detail="Please provide a valid email address.")
    if len(p) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")

    conn = get_db_connection()
    c = conn.cursor()

    # Check username uniqueness (case-insensitive)
    c.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (u,))
    if c.fetchone():
        c.execute("SELECT username FROM users")
        all_users = {row["username"].lower() for row in c.fetchall()}
        conn.close()
        suggestions = _generate_username_suggestions(u, all_users)
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"Username '{u}' is already taken.",
                "field": "username",
                "suggestions": suggestions
            }
        )

    # Check email uniqueness (case-insensitive)
    # Generic non-revealing response to protect against email enumeration
    generic_success_msg = "A 6-digit verification code has been dispatched to your email address. Please enter it to complete signup."
    c.execute("SELECT id FROM users WHERE email = ? COLLATE NOCASE", (e,))
    existing_email_user = c.fetchone()
    if existing_email_user:
        conn.close()
        # Return generic success without creating duplicate verification code
        return JSONResponse({
            "success": True,
            "requires_verification": True,
            "email": e,
            "message": generic_success_msg
        })

    # Generate 6-digit email verification code
    import secrets
    verify_code = f"{secrets.randbelow(900000) + 100000}"
    code_hash = hashlib.sha256(verify_code.encode("utf-8")).hexdigest()
    expires_at = time.time() + 1800  # 30 minutes
    pw_hash = hash_password(p)

    # Invalidate previous unverified signup attempts for this email
    c.execute("DELETE FROM email_verifications WHERE email = ? COLLATE NOCASE AND purpose = 'signup'", (e,))
    c.execute(
        """
        INSERT INTO email_verifications (email, username, password_hash, token_hash, purpose, expires_at, attempts, used)
        VALUES (?, ?, ?, ?, 'signup', ?, 0, 0)
        """,
        (e, u, pw_hash, code_hash, expires_at)
    )
    conn.commit()
    conn.close()

    # Dispatch email in background thread
    worker = threading.Thread(
        target=_send_verification_email_worker,
        args=(e, u, verify_code),
        daemon=True
    )
    worker.start()

    return JSONResponse({
        "success": True,
        "requires_verification": True,
        "email": e,
        "message": generic_success_msg
    })

@app.post("/api/auth/verify-email")
def auth_verify_email(req: VerifyEmailRequest, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"verify:{client_ip}", max_requests=8, window_seconds=60)

    e = req.email.strip().lower()
    submitted_code = req.code.strip()
    submitted_hash = hashlib.sha256(submitted_code.encode("utf-8")).hexdigest()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, username, password_hash, token_hash, expires_at, attempts, used FROM email_verifications WHERE email = ? COLLATE NOCASE AND purpose = 'signup' ORDER BY id DESC LIMIT 1",
        (e,)
    )
    row = c.fetchone()

    if not row or row["used"] == 1:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid or expired verification code.")

    if time.time() > row["expires_at"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Verification code has expired. Please request a new one.")

    if row["attempts"] >= 5:
        c.execute("UPDATE email_verifications SET used = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=400, detail="Too many incorrect verification attempts. Code invalidated.")

    if row["token_hash"] != submitted_hash:
        c.execute("UPDATE email_verifications SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=400, detail="Incorrect verification code.")

    # Mark code as used
    c.execute("UPDATE email_verifications SET used = 1 WHERE id = ?", (row["id"],))

    # Create user account in users table (2,000 initial credits)
    user_id = None
    u = row["username"]
    pw_hash = row["password_hash"]
    try:
        c.execute(
            "INSERT INTO users (username, email, password_hash, role, credits, status) VALUES (?, ?, ?, 'user', 2000, 'active')",
            (u, e, pw_hash)
        )
        conn.commit()
        user_id = c.lastrowid
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=400, detail="Account with this username or email already exists.")
    finally:
        conn.close()

    session_token = create_session_jwt(user_id=user_id, username=u, role="user")
    is_https = request.url.scheme == "https"
    json_resp = JSONResponse({
        "success": True,
        "user": {
            "id": user_id,
            "username": u,
            "email": e,
            "role": "user",
            "credits": 2000
        },
        "message": "Email verified! Welcome to BIG G'S VOICE CLONER PRO."
    })
    json_resp.set_cookie(
        key=COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=JWT_EXPIRATION_HOURS * 3600
    )
    return json_resp

@app.post("/api/auth/resend-verification")
def auth_resend_verification(req: ResendVerificationRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"resend:{client_ip}", max_requests=4, window_seconds=60)

    e = req.email.strip().lower()
    conn = get_db_connection()
    c = conn.cursor()

    # Check if already registered
    c.execute("SELECT id FROM users WHERE email = ? COLLATE NOCASE", (e,))
    if c.fetchone():
        conn.close()
        return JSONResponse({"success": True, "message": "If this email requires verification, a code has been dispatched."})

    # Check unverified signup
    c.execute(
        "SELECT id, username, password_hash FROM email_verifications WHERE email = ? COLLATE NOCASE AND purpose = 'signup' AND used = 0 ORDER BY id DESC LIMIT 1",
        (e,)
    )
    row = c.fetchone()
    if not row:
        conn.close()
        return JSONResponse({"success": True, "message": "If this email requires verification, a code has been dispatched."})

    import secrets
    verify_code = f"{secrets.randbelow(900000) + 100000}"
    code_hash = hashlib.sha256(verify_code.encode("utf-8")).hexdigest()
    expires_at = time.time() + 1800

    c.execute("DELETE FROM email_verifications WHERE email = ? COLLATE NOCASE AND purpose = 'signup'", (e,))
    c.execute(
        """
        INSERT INTO email_verifications (email, username, password_hash, token_hash, purpose, expires_at, attempts, used)
        VALUES (?, ?, ?, ?, 'signup', ?, 0, 0)
        """,
        (e, row["username"], row["password_hash"], code_hash, expires_at)
    )
    conn.commit()
    conn.close()

    worker = threading.Thread(
        target=_send_verification_email_worker,
        args=(e, row["username"], verify_code),
        daemon=True
    )
    worker.start()

    return JSONResponse({"success": True, "message": "If this email requires verification, a code has been dispatched."})

@app.put("/api/auth/profile")
def auth_update_profile(
    req: UpdateProfileRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
    request: Request = None,
    response: Response = None
):
    """
    Profile editing:
    - Edit username: validates uniqueness (case-insensitive) across all users.
    - Edit email: initiates verification flow. Sends code to new email and requires code to apply change.
    """
    client_ip = request.client.host if request and request.client else "unknown"
    check_rate_limit(f"profile:{current_user['id']}", max_requests=10, window_seconds=60)

    conn = get_db_connection()
    c = conn.cursor()

    target_user_id = current_user["id"]
    new_username = req.username.strip().lower() if req.username else None
    new_email = req.email.strip().lower() if req.email else None
    v_code = req.verification_code.strip() if req.verification_code else None

    # Handle username update
    if new_username and new_username != current_user["username"].lower():
        if not re.match(r"^[a-zA-Z0-9_\-\.]{3,30}$", new_username):
            conn.close()
            raise HTTPException(status_code=400, detail="Username must be 3-30 characters with letters, numbers, hyphens or underscores.")
        c.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE AND id != ?", (new_username, target_user_id))
        if c.fetchone():
            c.execute("SELECT username FROM users")
            all_users = {row["username"].lower() for row in c.fetchall()}
            conn.close()
            suggestions = _generate_username_suggestions(new_username, all_users)
            raise HTTPException(
                status_code=400,
                detail={
                    "message": f"Username '{new_username}' is already in use.",
                    "field": "username",
                    "suggestions": suggestions
                }
            )
        c.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, target_user_id))

    # Handle email update
    if new_email and new_email != current_user["email"].lower():
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", new_email):
            conn.close()
            raise HTTPException(status_code=400, detail="Please provide a valid email address.")

        c.execute("SELECT id FROM users WHERE email = ? COLLATE NOCASE AND id != ?", (new_email, target_user_id))
        if c.fetchone():
            conn.close()
            raise HTTPException(status_code=400, detail="This email address is already in use by another account.")

        if not v_code:
            # Generate and dispatch verification code for new email
            import secrets
            email_code = f"{secrets.randbelow(900000) + 100000}"
            code_hash = hashlib.sha256(email_code.encode("utf-8")).hexdigest()
            expires_at = time.time() + 1800

            c.execute("DELETE FROM email_verifications WHERE user_id = ? AND purpose = 'email_change'", (target_user_id,))
            c.execute(
                """
                INSERT INTO email_verifications (email, username, password_hash, token_hash, purpose, user_id, expires_at, attempts, used)
                VALUES (?, ?, '', ?, 'email_change', ?, ?, 0, 0)
                """,
                (new_email, current_user["username"], code_hash, target_user_id, expires_at)
            )
            conn.commit()
            conn.close()

            worker = threading.Thread(
                target=_send_verification_email_worker,
                args=(new_email, current_user["username"], email_code),
                daemon=True
            )
            worker.start()

            return JSONResponse({
                "success": True,
                "requires_verification": True,
                "pending_email": new_email,
                "message": f"Verification code sent to {new_email}. Please submit the code to complete email change."
            })
        else:
            # Verify code and apply change
            c.execute(
                "SELECT id, email, token_hash, expires_at, attempts, used FROM email_verifications WHERE user_id = ? AND purpose = 'email_change' ORDER BY id DESC LIMIT 1",
                (target_user_id,)
            )
            row = c.fetchone()
            if not row or row["used"] == 1 or row["email"].lower() != new_email:
                conn.close()
                raise HTTPException(status_code=400, detail="Invalid verification code for this email change.")
            if time.time() > row["expires_at"]:
                conn.close()
                raise HTTPException(status_code=400, detail="Verification code expired. Please request a new one.")
            if row["attempts"] >= 5:
                c.execute("UPDATE email_verifications SET used = 1 WHERE id = ?", (row["id"],))
                conn.commit()
                conn.close()
                raise HTTPException(status_code=400, detail="Too many failed attempts. Code invalidated.")

            submitted_hash = hashlib.sha256(v_code.encode("utf-8")).hexdigest()
            if row["token_hash"] != submitted_hash:
                c.execute("UPDATE email_verifications SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
                conn.commit()
                conn.close()
                raise HTTPException(status_code=400, detail="Incorrect verification code.")

            c.execute("UPDATE email_verifications SET used = 1 WHERE id = ?", (row["id"],))
            c.execute("UPDATE users SET email = ? WHERE id = ?", (new_email, target_user_id))

    conn.commit()

    # Re-fetch updated user record
    c.execute("SELECT id, username, email, role, credits, status FROM users WHERE id = ?", (target_user_id,))
    updated_user = c.fetchone()
    conn.close()

    # Update session token with new username if changed
    updated_username = updated_user["username"]
    new_session_token = create_session_jwt(user_id=updated_user["id"], username=updated_username, role=updated_user["role"])
    is_https = request.url.scheme == "https" if request else False

    json_resp = JSONResponse({
        "success": True,
        "user": {
            "id": updated_user["id"],
            "username": updated_user["username"],
            "email": updated_user["email"],
            "role": updated_user["role"],
            "credits": updated_user["credits"],
            "status": updated_user["status"]
        },
        "message": "Profile updated successfully!"
    })
    json_resp.set_cookie(
        key=COOKIE_NAME,
        value=new_session_token,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=JWT_EXPIRATION_HOURS * 3600
    )
    return json_resp

@app.post("/api/auth/login")
def auth_login(req: LoginRequest, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"login:{client_ip}", max_requests=10, window_seconds=60)

    u_input = req.username.strip().lower()
    plain_password = req.password.strip()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, username, email, password_hash, role, credits, status FROM users WHERE username = ? OR email = ?",
        (u_input, u_input)
    )
    user = c.fetchone()

    if not user or not verify_password(plain_password, user["password_hash"]):
        conn.close()
        # Non-revealing error to prevent user enumeration
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password.")

    if user["status"] == "banned":
        conn.close()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account suspended. Please contact support.")

    # Auto-upgrade legacy hash to Bcrypt if necessary
    if not (user["password_hash"].startswith("$2a$") or user["password_hash"].startswith("$2b$")):
        new_hash = hash_password(plain_password)
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, user["id"]))
        conn.commit()

    conn.close()

    session_token = create_session_jwt(user_id=user["id"], username=user["username"], role=user["role"])

    is_https = request.url.scheme == "https"
    json_resp = JSONResponse({
        "success": True,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "email": user["email"],
            "role": user["role"],
            "credits": user["credits"]
        },
        "message": f"Welcome back, {user['username']}!"
    })
    json_resp.set_cookie(
        key=COOKIE_NAME,
        value=session_token,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=JWT_EXPIRATION_HOURS * 3600
    )
    return json_resp

@app.post("/api/auth/logout")
def auth_logout(response: Response):
    json_resp = JSONResponse({"success": True, "message": "Signed out successfully."})
    json_resp.delete_cookie(key=COOKIE_NAME)
    return json_resp

@app.get("/api/auth/me")
def auth_get_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    return JSONResponse({
        "success": True,
        "user": {
            "id": current_user["id"],
            "username": current_user["username"],
            "email": current_user["email"],
            "role": current_user["role"],
            "credits": current_user["credits"],
            "status": current_user["status"]
        }
    })


# -------------------------------------------------------------
# PRIORITY 2: Password Reset & Support Endpoints
# -------------------------------------------------------------

def _send_reset_email_worker(email: str, username: str, reset_code: str):
    """Background worker to send password reset emails outside the request response path."""
    escaped_user = html.escape(username)
    html_email = f"""
    <div style="font-family: Arial, sans-serif; background-color: #0b0f19; color: #f8fafc; padding: 30px; border-radius: 10px; max-width: 520px; margin: 0 auto; border: 1px solid #1e293b;">
        <h2 style="color: #6366f1; margin-top: 0;">BIG G'S VOICE CLONER PRO</h2>
        <p style="font-size: 15px; color: #cbd5e1;">Hello <strong>{escaped_user}</strong>,</p>
        <p style="font-size: 14px; color: #94a3b8; line-height: 1.5;">You requested a password reset for your Voice Cloner Pro account. Use the 6-digit verification code below to set a new password:</p>
        <div style="text-align: center; margin: 25px 0;">
            <span style="display: inline-block; font-size: 32px; font-weight: 800; letter-spacing: 8px; color: #38bdf8; background: #1e293b; padding: 12px 24px; border-radius: 8px; border: 1px solid #3b82f6;">{reset_code}</span>
        </div>
        <p style="font-size: 13px; color: #64748b;">This code expires in 30 minutes. If you did not request this, you can safely ignore this email.</p>
        <hr style="border: 0; border-top: 1px solid #1e293b; margin: 20px 0;">
        <p style="font-size: 11px; color: #475569; text-align: center;">BIG G Voice Cloner Pro • Studio AI Audio Suite</p>
    </div>
    """
    text_email = f"Your BIG G Voice Cloner Pro verification code is: {reset_code}\nThis code expires in 30 minutes."

    delivery = send_smtp_email(
        to_email=email,
        subject=f"{reset_code} is your Voice Cloner Pro reset code",
        html_content=html_email,
        text_content=text_email
    )
    if not delivery.get("sent", False):
        logger.warning(f"Background password reset delivery failure for account: {delivery.get('reason')}")

@app.post("/api/auth/forgot-password")
def auth_forgot_password(req: ForgotPasswordRequest, request: Request, background_tasks: BackgroundTasks):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"forgot:{client_ip}", max_requests=4, window_seconds=60)

    e = req.email.strip().lower()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, username FROM users WHERE email = ?", (e,))
    user = c.fetchone()

    # Generic non-revealing response to protect user privacy
    generic_resp = JSONResponse({
        "success": True,
        "message": "If this email is registered, a 6-digit password reset code has been dispatched to your inbox."
    })

    if not user:
        conn.close()
        return generic_resp

    # Generate cryptographically secure 6-digit numeric reset token
    import secrets
    reset_code = f"{secrets.randbelow(900000) + 100000}"
    # Token stored as SHA-256 hash (never plain text)
    token_hash = hashlib.sha256(reset_code.encode("utf-8")).hexdigest()
    expires_at = time.time() + 1800  # 30 minutes validity

    # Invalidate previous unused tokens for this email
    c.execute("DELETE FROM reset_tokens WHERE email = ?", (e,))
    c.execute(
        "INSERT INTO reset_tokens (email, token_hash, expires_at, attempts, used) VALUES (?, ?, ?, 0, 0)",
        (e, token_hash, expires_at)
    )
    conn.commit()
    conn.close()

    # Dispatch email in a background worker thread outside the HTTP response path so registered
    # and unregistered email addresses return with identical timing without leaking SMTP latency
    worker_thread = threading.Thread(
        target=_send_reset_email_worker,
        args=(e, user["username"], reset_code),
        daemon=True
    )
    worker_thread.start()

    return generic_resp

@app.post("/api/auth/reset-password")
def auth_reset_password(req: ResetPasswordRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(f"reset:{client_ip}", max_requests=5, window_seconds=60)

    e = req.email.strip().lower()
    submitted_code = req.token.strip()
    new_p = req.new_password.strip()

    if len(new_p) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters.")

    submitted_hash = hashlib.sha256(submitted_code.encode("utf-8")).hexdigest()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, token_hash, expires_at, attempts, used FROM reset_tokens WHERE email = ? ORDER BY id DESC LIMIT 1",
        (e,)
    )
    row = c.fetchone()

    if not row or row["used"] == 1:
        conn.close()
        raise HTTPException(status_code=400, detail="Invalid or expired reset code.")

    if time.time() > row["expires_at"]:
        conn.close()
        raise HTTPException(status_code=400, detail="Reset code has expired. Please request a new one.")

    if row["attempts"] >= 5:
        # Invalidate after too many failed verification attempts
        c.execute("UPDATE reset_tokens SET used = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=400, detail="Too many incorrect attempts. Code invalidated.")

    if row["token_hash"] != submitted_hash:
        c.execute("UPDATE reset_tokens SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
        raise HTTPException(status_code=400, detail="Incorrect reset code.")

    # Mark token as used and update user's password with Bcrypt
    c.execute("UPDATE reset_tokens SET used = 1 WHERE id = ?", (row["id"],))
    new_hash = hash_password(new_p)
    c.execute("UPDATE users SET password_hash = ? WHERE email = ?", (new_hash, e))
    conn.commit()
    conn.close()

    return JSONResponse({"success": True, "message": "Password reset successfully! You can now sign in."})

@app.post("/api/support/ticket")
def create_support_ticket(
    req: CreateTicketRequest,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    # Enforce reasonable per-user rate limit (e.g., max 5 tickets per 10 minutes)
    check_rate_limit(f"ticket:{current_user['id']}", max_requests=5, window_seconds=600)

    clean_cat = html.escape(req.category.strip())
    clean_subj = html.escape(req.subject.strip())
    clean_msg = html.escape(req.message.strip())

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "INSERT INTO support_tickets (user_id, username, email, category, subject, message, status) VALUES (?, ?, ?, ?, ?, ?, 'open')",
        (current_user["id"], current_user["username"], current_user["email"], clean_cat, clean_subj, clean_msg)
    )
    conn.commit()
    ticket_id = c.lastrowid
    conn.close()

    # Notify admin via email if configured
    admin_notify_email = os.getenv("SMTP_USER", "")
    if admin_notify_email:
        send_smtp_email(
            to_email=admin_notify_email,
            subject=f"New Support Ticket #{ticket_id}: [{clean_cat}] {clean_subj}",
            html_content=f"<p>User <b>{html.escape(current_user['username'])}</b> ({html.escape(current_user['email'])}) filed a complaint:</p><p><b>Category:</b> {clean_cat}<br><b>Subject:</b> {clean_subj}</p><p>{clean_msg}</p>",
            text_content=f"Ticket #{ticket_id} from {current_user['username']} ({current_user['email']}):\n{req.message}"
        )

    return JSONResponse({
        "success": True,
        "ticket_id": ticket_id,
        "message": f"Your ticket #{ticket_id} has been submitted! Our support team is reviewing it."
    })

@app.get("/api/support/my-tickets")
def get_user_tickets(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, category, subject, message, status, admin_response, created_at FROM support_tickets WHERE user_id = ? ORDER BY id DESC",
        (current_user["id"],)
    )
    rows = c.fetchall()
    conn.close()
    return JSONResponse({"success": True, "tickets": [dict(r) for r in rows]})


# -------------------------------------------------------------
# PRIORITY 3: Audio Privacy, Storage & Ownership Endpoints
# -------------------------------------------------------------

ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".flac"}
MAX_AUDIO_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB

@app.post("/api/clone")
async def clone_voice(
    name: str = Form(...),
    description: Optional[str] = Form(""),
    reference_text: Optional[str] = Form(""),
    tags: Optional[str] = Form("Custom Cloned Voice"),
    audio_file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    check_rate_limit(f"clone:{current_user['id']}", max_requests=10, window_seconds=120)

    ext = os.path.splitext(audio_file.filename or "")[1].lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format '{ext}'. Allowed extensions: {', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}"
        )

    # Read and validate file size in bounded chunks (aborts early if > 15MB)
    file_bytes = await read_bounded_upload(audio_file, max_bytes=MAX_AUDIO_UPLOAD_BYTES)
    if len(file_bytes) < 1000:
        raise HTTPException(status_code=400, detail="Audio sample is too short or empty.")

    voice_id = uuid.uuid4().hex[:12]
    raw_filename = f"{voice_id}_raw{ext}"
    clean_filename = f"{voice_id}_clean.wav"
    raw_path = os.path.join(UPLOADS_DIR, raw_filename)
    clean_path = os.path.join(UPLOADS_DIR, clean_filename)

    with open(raw_path, "wb") as f:
        f.write(file_bytes)

    # Verify it is valid audio and not malformed
    try:
        final_ref_path = preprocess_reference_audio(raw_path, clean_path)
        profile = analyze_audio_profile(final_ref_path)
        # Successfully preprocessed: remove raw sample since clean_path is used as reference
        if raw_path != clean_path:
            safe_remove_managed_file(raw_path, allowed_dirs=[UPLOADS_DIR])
    except Exception as e:
        safe_remove_managed_file(raw_path, allowed_dirs=[UPLOADS_DIR])
        safe_remove_managed_file(clean_path, allowed_dirs=[UPLOADS_DIR])
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid or readable audio track.")

    base_voice = "en-US-AvaMultilingualNeural" if profile.get("gender_affinity") == "Female" else "en-US-AndrewMultilingualNeural"
    clean_name = html.escape(name.strip()[:60])
    clean_desc = html.escape(description.strip()[:200]) if description else f"Cloned voice ({profile.get('gender_affinity')}, duration: {profile.get('duration')}s)"
    clean_ref = reference_text.strip()[:500] if reference_text else ""
    clean_tags = html.escape(tags.strip()[:100]) if tags else "Custom Voice"

    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO cloned_voices (id, user_id, name, description, reference_text, tags, file_path, base_model, engine, profile_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fish_audio', ?)
        """,
        (voice_id, current_user["id"], clean_name, clean_desc, clean_ref, clean_tags, final_ref_path, base_voice, json.dumps(profile))
    )
    conn.commit()
    conn.close()

    return JSONResponse({
        "success": True,
        "voice": {
            "id": voice_id,
            "name": clean_name,
            "description": clean_desc,
            "reference_text": clean_ref,
            "tags": [t.strip() for t in clean_tags.split(",") if t.strip()],
            "sample_url": f"/api/audio/stream/{clean_filename}",
            "base_model": base_voice,
            "profile": profile
        }
    })

@app.get("/api/cloned-voices")
def list_cloned_voices(current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute(
        "SELECT id, name, description, reference_text, tags, file_path, base_model, engine, profile_json, created_at FROM cloned_voices WHERE user_id = ? ORDER BY created_at DESC",
        (current_user["id"],)
    )
    rows = c.fetchall()
    conn.close()

    result = []
    for r in rows:
        prof = {}
        try:
            prof = json.loads(r["profile_json"]) if r["profile_json"] else {}
        except Exception:
            pass
        filename = os.path.basename(r["file_path"])
        result.append({
            "id": r["id"],
            "name": r["name"],
            "description": r["description"],
            "reference_text": r["reference_text"],
            "tags": [t.strip() for t in (r["tags"] or "").split(",") if t.strip()],
            "sample_url": f"/api/audio/stream/{filename}",
            "base_model": r["base_model"],
            "engine": r["engine"],
            "profile": prof,
            "created_at": r["created_at"]
        })
    return JSONResponse(result)

@app.delete("/api/cloned-voices/{voice_id}")
def delete_cloned_voice(voice_id: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, user_id, file_path, name FROM cloned_voices WHERE id = ?", (voice_id,))
    voice = c.fetchone()

    if not voice:
        conn.close()
        raise HTTPException(status_code=404, detail="Cloned voice not found.")

    # Enforce strict ownership: only owner or administrator can delete
    if voice["user_id"] != current_user["id"] and current_user["role"] != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Permission denied. You do not own this voice model.")

    file_path = voice["file_path"]
    if file_path:
        safe_remove_managed_file(file_path, allowed_dirs=[UPLOADS_DIR, OUTPUTS_DIR])
        # Also clean any matching raw file if it exists
        base_root, _ = os.path.splitext(file_path)
        raw_guess = base_root.replace("_clean", "_raw")
        for ext in ALLOWED_AUDIO_EXTENSIONS:
            safe_remove_managed_file(f"{raw_guess}{ext}", allowed_dirs=[UPLOADS_DIR])

    c.execute("DELETE FROM cloned_voices WHERE id = ?", (voice_id,))
    conn.commit()
    conn.close()

    return JSONResponse({"success": True, "message": f"Voice '{voice['name']}' deleted successfully."})

@app.get("/api/audio/stream/{filename}")
def stream_private_audio(filename: str, current_user: Dict[str, Any] = Depends(get_current_user)):
    """
    Authenticated Audio Streamer:
    Never exposes public directories directly.
    Enforces that the requested audio file belongs to the authenticated user.
    """
    # Prevent directory traversal attacks
    safe_filename = os.path.basename(filename)
    if ".." in filename or safe_filename != filename:
        raise HTTPException(status_code=400, detail="Invalid audio filename.")

    # Search in outputs or uploads
    out_path = os.path.join(OUTPUTS_DIR, safe_filename)
    up_path = os.path.join(UPLOADS_DIR, safe_filename)
    found_path = out_path if os.path.exists(out_path) else (up_path if os.path.exists(up_path) else None)

    if not found_path:
        raise HTTPException(status_code=404, detail="Audio file not found or expired.")

    # Verify ownership via database
    conn = get_db_connection()
    c = conn.cursor()
    is_owner = False

    # Check generated_audio table
    c.execute("SELECT user_id FROM generated_audio WHERE filename = ?", (safe_filename,))
    gen = c.fetchone()
    if gen and gen["user_id"] == current_user["id"]:
        is_owner = True

    # Check cloned_voices table
    if not is_owner:
        c.execute("SELECT user_id FROM cloned_voices WHERE file_path LIKE ?", (f"%{safe_filename}%",))
        cv = c.fetchone()
        if cv and cv["user_id"] == current_user["id"]:
            is_owner = True

    # Admin override
    if current_user["role"] == "admin":
        is_owner = True

    conn.close()

    if not is_owner:
        raise HTTPException(status_code=403, detail="Access denied. You do not have permission to access this audio track.")

    ext = os.path.splitext(safe_filename)[1].lower()
    media_types = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac"
    }
    return FileResponse(found_path, media_type=media_types.get(ext, "application/octet-stream"))


# -------------------------------------------------------------
# PRIORITY 4: Speech Synthesis, Credit Accounting & Safety
# -------------------------------------------------------------

@app.post("/api/tts")
async def generate_speech(req: TTSRequest, current_user: Dict[str, Any] = Depends(get_current_user)):
    clean_text = req.text.strip()
    if not clean_text:
        raise HTTPException(status_code=400, detail="Text cannot be empty.")

    check_rate_limit(f"tts:{current_user['id']}", max_requests=25, window_seconds=60)

    # Calculate token cost (1 token per character, minimum 10)
    tokens_required = max(len(clean_text), 10)
    user_id = current_user["id"]
    is_admin = current_user["role"] == "admin"

    conn = get_db_connection()
    c = conn.cursor()

    # Atomic credit check & deduction before generation with row locking / atomic balance update
    if not is_admin:
        c.execute("UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?", (tokens_required, user_id, tokens_required))
        conn.commit()
        if c.rowcount == 0:
            c.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
            user_row = c.fetchone()
            curr_credits = user_row["credits"] if user_row else 0
            conn.close()
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Insufficient tokens. You have {curr_credits:,} tokens, but this generation requires {tokens_required:,} tokens."
            )

    conn.close()

    # Voice Resolution
    api_key = os.getenv("FISH_AUDIO_API_KEY", "").strip()
    pro_match = next((v for v in CURATED_HUMAN_VOICES if v["id"] == req.voice), None)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, user_id, name, base_model, file_path, reference_text FROM cloned_voices WHERE id = ?", (req.voice,))
    cloned_match = c.fetchone()
    conn.close()

    # Enforce clone ownership check upfront regardless of engine or whether Fish Audio is configured
    if cloned_match:
        if cloned_match["user_id"] != user_id and not is_admin:
            raise HTTPException(status_code=403, detail="Permission denied. You do not own this voice clone model.")

    output_filename = f"gen_{uuid.uuid4().hex[:12]}.mp3"
    output_path = os.path.join(OUTPUTS_DIR, output_filename)
    engine_used = "Edge Neural Fallback"
    generation_succeeded = False
    is_fallback = False

    try:
        # 1. Curated Ultra-Human Pro Voice via Fish Audio
        if pro_match and api_key:
            try:
                payload = {
                    "text": clean_text,
                    "reference_id": pro_match["fish_id"],
                    "format": "mp3",
                    "temperature": float(np.clip(req.temperature or 0.7, 0.1, 1.0)),
                    "top_p": float(np.clip(req.top_p or 0.7, 0.1, 1.0)),
                    "chunk_length": int(np.clip(req.chunk_length or 200, 100, 300))
                }
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "model": os.getenv("FISH_AUDIO_MODEL", "s2.1-pro-free")
                }
                async with httpx.AsyncClient(timeout=40.0) as client:
                    resp = await client.post("https://api.fish.audio/v1/tts", headers=headers, json=payload)
                if resp.status_code == 200:
                    with open(output_path, "wb") as f:
                        f.write(resp.content)
                    engine_used = "Studio Neural Pro HD"
                    generation_succeeded = True
            except Exception as e:
                logger.warning(f"Pro Voice synthesis failed, falling back to neural: {e}")

        # 2. Cloned User Voice via Zero-Shot Audio Conditioning
        elif cloned_match and api_key and (req.engine in ["auto", "fish_audio"]):
            ref_path = cloned_match["file_path"]
            if os.path.exists(ref_path):
                try:
                    with open(ref_path, "rb") as rf:
                        ref_bytes = rf.read()
                    ref_obj = {"audio": ref_bytes}
                    if cloned_match["reference_text"]:
                        ref_obj["text"] = cloned_match["reference_text"]

                    payload = {
                        "text": clean_text,
                        "references": [ref_obj],
                        "format": "mp3",
                        "temperature": float(np.clip(req.temperature or 0.7, 0.1, 1.0)),
                        "top_p": float(np.clip(req.top_p or 0.7, 0.1, 1.0)),
                        "chunk_length": int(np.clip(req.chunk_length or 200, 100, 300))
                    }
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/msgpack",
                        "model": os.getenv("FISH_AUDIO_MODEL", "s2.1-pro-free")
                    }
                    async with httpx.AsyncClient(timeout=45.0) as client:
                        resp = await client.post("https://api.fish.audio/v1/tts", headers=headers, content=msgpack.packb(payload))
                    if resp.status_code == 200:
                        with open(output_path, "wb") as f:
                            f.write(resp.content)
                        engine_used = "Acoustic Zero-Shot Clone"
                        generation_succeeded = True
                except Exception as e:
                    logger.warning(f"Zero-shot acoustic cloning failed, falling back to standard neural: {e}")

        # 3. Standard / Fallback Neural Synthesis
        if not generation_succeeded:
            is_fallback = bool(cloned_match or pro_match)
            if cloned_match:
                target_voice = cloned_match["base_model"]
                pitch_val = "+0Hz"
            elif pro_match:
                target_voice = "en-GB-RyanNeural" if pro_match["locale"] == "en-GB" else ("en-US-AndrewMultilingualNeural" if pro_match["gender"] == "Male" else "en-US-AvaMultilingualNeural")
                pitch_val = "+0Hz"
            else:
                target_voice = req.voice
                pitch_val = req.pitch or "+0Hz"

            # Pitch & Rate Sanitization
            clean_pitch = str(pitch_val).strip()
            if clean_pitch.endswith("%"):
                try:
                    pct = int(clean_pitch.replace("%", "").replace("+", ""))
                    clean_pitch = f"{'+' if pct >= 0 else ''}{int(pct * 1.5)}Hz"
                except Exception:
                    clean_pitch = "+0Hz"
            elif not clean_pitch.endswith("Hz"):
                clean_pitch = f"{clean_pitch}Hz"
            if not clean_pitch.startswith("+") and not clean_pitch.startswith("-"):
                clean_pitch = f"+{clean_pitch}"

            clean_rate = req.rate or "+0%"
            if not clean_rate.endswith("%"):
                clean_rate = f"{clean_rate}%"
            if not clean_rate.startswith("+") and not clean_rate.startswith("-"):
                clean_rate = f"+{clean_rate}"

            communicate = edge_tts.Communicate(
                text=clean_text,
                voice=target_voice,
                rate=clean_rate,
                pitch=clean_pitch,
                volume=req.volume or "+0%"
            )
            await communicate.save(output_path)
            engine_used = "Standard Neural (Fallback)" if is_fallback else "Edge Neural HD"
            generation_succeeded = True

        # Optional DSP Audio FX
        if req.dsp_effect and req.dsp_effect in ALLOWED_EFFECTS and req.dsp_effect != "none":
            output_filename = apply_dsp_effects(output_path, req.dsp_effect)

        # Record generated audio in database with ownership
        audio_id = uuid.uuid4().hex[:12]
        conn = get_db_connection()
        c = conn.cursor()
        voice_label = pro_match["name"] if pro_match else (cloned_match["name"] if cloned_match else req.voice)
        c.execute(
            """
            INSERT INTO generated_audio (id, user_id, filename, filepath, voice_name, engine_used, characters_count, tokens_charged)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (audio_id, user_id, output_filename, os.path.join(OUTPUTS_DIR, output_filename), voice_label, engine_used, len(clean_text), tokens_required)
        )
        c.execute("SELECT credits FROM users WHERE id = ?", (user_id,))
        remaining_credits = c.fetchone()["credits"]
        conn.commit()
        conn.close()

        return JSONResponse({
            "success": True,
            "audio_url": f"/api/audio/stream/{output_filename}",
            "filename": output_filename,
            "engine_used": engine_used,
            "is_fallback": is_fallback,
            "voice_name": voice_label,
            "tokens_spent": tokens_required,
            "credits_remaining": remaining_credits
        })

    except Exception as e:
        # Atomic Refund on failure
        if not is_admin:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (tokens_required, user_id))
            conn.commit()
            conn.close()
            logger.info(f"Refunded {tokens_required} tokens to user #{user_id} due to generation failure.")

        logger.error(f"TTS Synthesis error: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail="Voice synthesis encountered a temporary error. Your tokens have been refunded.")

@app.post("/api/dialogue")
async def generate_dialogue(req: DialogueRequest, current_user: Dict[str, Any] = Depends(get_current_user)):
    check_rate_limit(f"dialogue:{current_user['id']}", max_requests=8, window_seconds=60)

    combined_segments = []
    sample_rate = 24000
    total_tokens_spent = 0
    generated_line_files = []
    completed_lines_tokens = 0
    dialogue_filename = None

    try:
        for line in req.lines:
            line_tts = TTSRequest(text=line.text, voice=line.speaker, engine="auto")
            tts_res = await generate_speech(line_tts, current_user=current_user)
            line_data = json.loads(tts_res.body.decode("utf-8"))

            line_cost = line_data.get("tokens_spent", 0)
            total_tokens_spent += line_cost
            completed_lines_tokens += line_cost

            fname = line_data.get("filename")
            if fname:
                generated_line_files.append(fname)
            file_path = os.path.join(OUTPUTS_DIR, fname)
            if os.path.exists(file_path):
                audio_data, sr_val = sf.read(file_path)
                if len(audio_data.shape) > 1:
                    audio_data = audio_data.mean(axis=1)
                combined_segments.append(audio_data)
                # Add subtle natural conversational pause (0.35s)
                pause = np.zeros(int(sr_val * 0.35))
                combined_segments.append(pause)
                sample_rate = sr_val

        if not combined_segments:
            raise HTTPException(status_code=500, detail="Multi-speaker synthesis produced no audio.")

        merged_data = np.concatenate(combined_segments)
        dialogue_filename = f"dialogue_{uuid.uuid4().hex[:12]}.wav"
        dialogue_path = os.path.join(OUTPUTS_DIR, dialogue_filename)
        sf.write(dialogue_path, merged_data, sample_rate)

        # Record ownership
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO generated_audio (id, user_id, filename, filepath, voice_name, engine_used, characters_count, tokens_charged)
            VALUES (?, ?, ?, ?, 'Multi-Speaker Ensemble', 'Multi-Track Stitcher', 0, ?)
            """,
            (uuid.uuid4().hex[:12], current_user["id"], dialogue_filename, dialogue_path, total_tokens_spent)
        )
        c.execute("SELECT credits FROM users WHERE id = ?", (current_user["id"],))
        remaining_credits = c.fetchone()["credits"]
        conn.commit()
        conn.close()

        # Clean up intermediate line files and their database records so outputs and DB only hold the master track
        if generated_line_files:
            conn = get_db_connection()
            c = conn.cursor()
            placeholders = ",".join("?" * len(generated_line_files))
            c.execute(f"DELETE FROM generated_audio WHERE filename IN ({placeholders})", generated_line_files)
            conn.commit()
            conn.close()

            for f in generated_line_files:
                try:
                    safe_remove_managed_file(os.path.join(OUTPUTS_DIR, f), allowed_dirs=[OUTPUTS_DIR])
                except Exception:
                    pass

        return JSONResponse({
            "success": True,
            "audio_url": f"/api/audio/stream/{dialogue_filename}",
            "filename": dialogue_filename,
            "lines_rendered": len(req.lines),
            "credits_remaining": remaining_credits
        })

    except Exception as e:
        logger.error(f"Dialogue generation failed: {e}")
        # Clean up dialogue master track if created
        if dialogue_filename:
            safe_remove_managed_file(os.path.join(OUTPUTS_DIR, dialogue_filename), allowed_dirs=[OUTPUTS_DIR])

        # Clean up any generated line temp files
        for f in generated_line_files:
            safe_remove_managed_file(os.path.join(OUTPUTS_DIR, f), allowed_dirs=[OUTPUTS_DIR])

        # Remove DB records and refund credits for lines that succeeded before failure
        conn = get_db_connection()
        c = conn.cursor()
        if generated_line_files:
            placeholders = ",".join("?" * len(generated_line_files))
            c.execute(f"DELETE FROM generated_audio WHERE filename IN ({placeholders})", generated_line_files)

        if completed_lines_tokens > 0 and current_user["role"] != "admin":
            c.execute("UPDATE users SET credits = credits + ? WHERE id = ?", (completed_lines_tokens, current_user["id"]))
            logger.info(f"Refunded {completed_lines_tokens} tokens for completed lines to user #{current_user['id']} due to dialogue failure.")
        conn.commit()
        conn.close()

        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail="Multi-speaker dialogue rendering encountered a failure.")

@app.post("/api/transcribe")
async def transcribe_audio_file(
    audio_file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    check_rate_limit(f"transcribe:{current_user['id']}", max_requests=10, window_seconds=60)

    # Read in bounded chunks (aborts early if > 15MB)
    content = await read_bounded_upload(audio_file, max_bytes=MAX_AUDIO_UPLOAD_BYTES)

    try:
        audio_io = io.BytesIO(content)
        data, sample_rate = sf.read(audio_io)
        if len(data.shape) > 1:
            data = data.mean(axis=1)

        wav_io = io.BytesIO()
        sf.write(wav_io, data, sample_rate, format='WAV')
        wav_io.seek(0)

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_io) as source:
            recorded = recognizer.record(source)

        transcript = recognizer.recognize_google(recorded)
        return JSONResponse({"success": True, "transcript": transcript})
    except sr.UnknownValueError:
        return JSONResponse({"success": False, "transcript": "", "message": "Speech could not be deciphered clearly."})
    except Exception as e:
        logger.warning(f"Transcription error: {e}")
        return JSONResponse({"success": False, "transcript": "", "message": "Failed to transcribe audio clip."})


# -------------------------------------------------------------
# PRIORITY 1 & 2: Administrator Control Panel Endpoints
# -------------------------------------------------------------

@app.get("/api/admin/users")
def admin_get_users(admin_user: Dict[str, Any] = Depends(require_admin)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, username, email, role, credits, status, created_at FROM users ORDER BY id DESC")
    rows = c.fetchall()
    c.execute("SELECT COUNT(*) FROM cloned_voices")
    total_clones = c.fetchone()[0]
    conn.close()

    users = [dict(r) for r in rows]
    return JSONResponse({
        "success": True,
        "users": users,
        "stats": {
            "total_users": len(users),
            "cloned_voices_count": total_clones,
            "engine": "Neural Audio Pro Studio"
        }
    })

@app.post("/api/admin/users/{target_user_id}/status")
def admin_toggle_user_status(
    target_user_id: int,
    status: str = Form(...),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    clean_status = status.strip().lower()
    if clean_status not in ["active", "banned"]:
        raise HTTPException(status_code=400, detail="Status must be either 'active' or 'banned'.")

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET status = ? WHERE id = ? AND role != 'admin'", (clean_status, target_user_id))
    conn.commit()
    conn.close()
    return JSONResponse({"success": True, "message": f"User status set to {clean_status}."})

@app.post("/api/admin/users/credits")
def admin_set_user_credits(
    req: UpdateUserCreditsRequest,
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET credits = ? WHERE id = ?", (req.credits, req.user_id))
    conn.commit()
    conn.close()
    return JSONResponse({"success": True, "message": "Tokens updated successfully!"})

@app.delete("/api/admin/users/{target_user_id}")
def admin_delete_user(
    target_user_id: int,
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    conn = get_db_connection()
    c = conn.cursor()

    # Find and delete all cloned voices and associated files for this user
    c.execute("SELECT id, file_path FROM cloned_voices WHERE user_id = ?", (target_user_id,))
    for row in c.fetchall():
        fp = row["file_path"]
        if fp:
            safe_remove_managed_file(fp, allowed_dirs=[UPLOADS_DIR, OUTPUTS_DIR])
            base_root, _ = os.path.splitext(fp)
            raw_guess = base_root.replace("_clean", "_raw")
            for ext in ALLOWED_AUDIO_EXTENSIONS:
                safe_remove_managed_file(f"{raw_guess}{ext}", allowed_dirs=[UPLOADS_DIR])

    # Find and delete all generated audio files for this user
    c.execute("SELECT filepath FROM generated_audio WHERE user_id = ?", (target_user_id,))
    for row in c.fetchall():
        afp = row["filepath"]
        if afp:
            safe_remove_managed_file(afp, allowed_dirs=[OUTPUTS_DIR])

    c.execute("DELETE FROM users WHERE id = ? AND role != 'admin'", (target_user_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"success": True, "message": "User account and associated audio deleted."})

@app.get("/api/admin/smtp-health")
def admin_smtp_health_check(admin_user: Dict[str, Any] = Depends(require_admin)):
    """Safe SMTP diagnostics for platform admin."""
    health = check_smtp_health()
    return JSONResponse({"success": True, "smtp": health})

@app.get("/api/admin/tickets")
def admin_get_all_tickets(admin_user: Dict[str, Any] = Depends(require_admin)):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, user_id, username, email, category, subject, message, status, admin_response, created_at FROM support_tickets ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return JSONResponse({"success": True, "tickets": [dict(r) for r in rows]})

@app.post("/api/admin/tickets/reply")
def admin_reply_ticket(
    req: ReplyTicketRequest,
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    clean_reply = html.escape(req.response.strip())
    clean_status = "resolved" if req.status == "resolved" else "open"

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT email, username, subject FROM support_tickets WHERE id = ?", (req.ticket_id,))
    ticket = c.fetchone()
    if not ticket:
        conn.close()
        raise HTTPException(status_code=404, detail="Support ticket not found.")

    c.execute(
        "UPDATE support_tickets SET admin_response = ?, status = ? WHERE id = ?",
        (clean_reply, clean_status, req.ticket_id)
    )
    conn.commit()
    conn.close()

    status_label = clean_status.upper()
    send_smtp_email(
        to_email=ticket["email"],
        subject=f"Update on Support Ticket #{req.ticket_id}: {ticket['subject']}",
        html_content=f"""
        <div style="font-family: Arial, sans-serif; background: #0b0f19; color: #f8fafc; padding: 24px; border-radius: 8px;">
            <h3 style="color: #6366f1;">BIG G'S Support Desk</h3>
            <p>Hello <b>{html.escape(ticket['username'])}</b>,</p>
            <p>Our administrator has replied to your ticket: <i>{html.escape(ticket['subject'])}</i></p>
            <div style="background: #1e293b; padding: 14px; border-left: 4px solid #3b82f6; margin: 16px 0; border-radius: 4px;">
                {clean_reply}
            </div>
            <p style="font-size: 13px; color: #94a3b8;">Ticket Status: <b style="color: {'#10b981' if clean_status == 'resolved' else '#f59e0b'};">{status_label}</b></p>
        </div>
        """,
        text_content=f"Hello {ticket['username']},\n\nSupport response for #{req.ticket_id}:\n{req.response}\n\nStatus: {status_label}"
    )

    return JSONResponse({"success": True, "message": f"Reply saved ({status_label}) and emailed to user."})

@app.post("/api/admin/tickets/{ticket_id}/resolve")
def admin_resolve_ticket_direct(
    ticket_id: int,
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE support_tickets SET status = 'resolved' WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"success": True, "message": f"Ticket #{ticket_id} marked as RESOLVED."})

@app.get("/api/config")
def get_config():
    api_key = os.getenv("FISH_AUDIO_API_KEY", "").strip()
    return JSONResponse({
        "engine_label": "Studio Neural HD Engine Ready",
        "has_pro_engine": bool(api_key and len(api_key) > 10)
    })

@app.get("/api/models")
async def list_neural_voices():
    """Return catalog of built-in US & UK neural voices + Pro Human voices."""
    try:
        if os.path.exists(VOICES_CACHE_FILE):
            with open(VOICES_CACHE_FILE, "r", encoding="utf-8") as f:
                curated = json.load(f)
        else:
            voices = await edge_tts.list_voices()
            curated = []
            for v in voices:
                loc = v.get("Locale", "")
                if loc in ["en-US", "en-GB"]:
                    friendly = v.get("FriendlyName", v.get("ShortName"))
                    name_clean = friendly.replace("Microsoft Server Speech Text to Speech Voice (", "").replace(")", "").replace("Online (Natural)", "").strip()
                    curated.append({
                        "id": v.get("ShortName"),
                        "name": name_clean,
                        "gender": v.get("Gender"),
                        "locale": loc,
                        "suggested": True,
                        "is_fish_pro": False
                    })
            curated.sort(key=lambda x: (x["locale"] != "en-US", x["locale"] != "en-GB", x["name"]))
            with open(VOICES_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(curated, f, indent=2)

        return JSONResponse({
            "count": len(curated),
            "voices": curated,
            "human_pro_voices": CURATED_HUMAN_VOICES
        })
    except Exception as e:
        logger.warning(f"Could not load voice models: {e}")
        return JSONResponse({"count": 0, "voices": [], "human_pro_voices": CURATED_HUMAN_VOICES})

# Mount Frontend static assets (Only public UI HTML/CSS/JS, NO audio folders)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=7860, reload=True)

