# Production Deployment & Persistence Guide: BIG G'S Voice Cloner Pro

This guide outlines how to deploy **BIG G's Voice Cloner Pro** to [Render](https://render.com) or similar platforms securely, explaining persistent storage solutions, environment variables, and zero-downtime deployment steps.

---

## 1. Why User Accounts Were Reset on Past Deploys

Render free web services operate on an **ephemeral disk**. Every time you trigger a new build or deploy:
1. Render spins up a fresh Docker container from Git.
2. Any local files modified or created during runtime (such as `voices/studio.db` or files in `voices/` and `outputs/`) are discarded.
3. This caused the local SQLite database to be reinitialized, losing user accounts and uploaded samples.

### Solutions for Persistent Data on Render:
- **Option A (Recommended for SQLite / Quickest Setup): Render Persistent Disk**
  - In your Render Web Service dashboard, go to **Disks** -> **Add Disk**.
  - Mount Path: `/var/data` (e.g. Size: 10GB - 50GB).
  - Update `render.yaml` or set environment variable `PERSISTENT_DATA_DIR=/var/data`. The app will store `studio.db` and uploaded reference voices inside this mount point, surviving all rebuilds and redeployments.
- **Option B (Recommended for Enterprise Scaling): Render Managed PostgreSQL + Cloudflare R2 / AWS S3**
  - Spin up a free or starter **Render PostgreSQL database**.
  - Set `DATABASE_URL` in environment variables.
  - Store reference and generated audio in an S3-compatible bucket (e.g. Cloudflare R2 has zero egress fees).

---

## 2. Environment Variables Configuration

In Render Dashboard -> **Environment**:

| Environment Variable | Required | Description | Example / Recommended Value |
| :--- | :--- | :--- | :--- |
| `PYTHON_VERSION` | Yes | Python runtime version | `3.11.9` |
| `JWT_SECRET` | **Critical** | 32+ character random string for signing session JWTs | *Generate via `python -c "import secrets; print(secrets.token_hex(32))"`* |
| `JWT_ALGORITHM` | Optional | JWT signing algorithm (Default: `HS256`) | `HS256` |
| `JWT_EXPIRATION_HOURS` | Optional | Session lifetime in hours (Default: `72`) | `72` |
| `ALLOWED_ORIGINS` | **Critical** | Comma-separated list of trusted frontend origins for CORS | `https://big-g-voice-cloner-pro.onrender.com` |
| `ADMIN_EMAIL` | **Critical** | The email address to promote or bootstrap as Administrator | `ifelexdan@gmail.com` |
| `ADMIN_INITIAL_PASSWORD` | **Critical** | Initial admin password (min 8 chars) if account doesn't exist | *(Your secure admin password)* |
| `FISH_AUDIO_API_KEY` | Optional | Commercial Fish Audio neural API key | *(Fish Audio Key)* |
| `FISH_AUDIO_MODEL` | Optional | Model identifier for Fish Audio | `s2.1-pro-free` |
| `SMTP_HOST` | Optional | SMTP Host for password reset & ticket emails | `smtp.gmail.com` |
| `SMTP_PORT` | Optional | SMTP Port (587 for TLS, 465 for SSL) | `587` |
| `SMTP_USER` | Optional | SMTP Sender / Admin email address | `ifelexdan@gmail.com` |
| `SMTP_PASSWORD` | Optional | 16-character Google App Password (2FA) | *(16-character App Password)* |

> [!CAUTION]
> Never commit actual passwords, API keys, or `.env` files to GitHub. Set them exclusively in the **Render Dashboard Environment** tab.

---

## 3. Render Blueprint (`render.yaml`)

```yaml
services:
  - type: web
    name: big-g-voice-cloner-pro
    env: python
    plan: starter # Starter plan recommended for persistent disk support
    region: frankfurt # Low latency to West Africa / Nigeria
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn app:app --host 0.0.0.0 --port $PORT
    disk:
      name: studio-storage
      mountPath: /var/data
      sizeGB: 10
    envVars:
      - key: PYTHON_VERSION
        value: 3.11.9
      - key: PERSISTENT_DATA_DIR
        value: /var/data
      - key: JWT_SECRET
        generateValue: true
      - key: ALLOWED_ORIGINS
        value: https://big-g-voice-cloner-pro.onrender.com
      - key: FISH_AUDIO_MODEL
        value: s2.1-pro-free
      - key: FISH_AUDIO_API_KEY
        sync: false
      - key: ADMIN_EMAIL
        sync: false
      - key: ADMIN_INITIAL_PASSWORD
        sync: false
      - key: SMTP_HOST
        value: smtp.gmail.com
      - key: SMTP_PORT
        value: 587
      - key: SMTP_USER
        sync: false
      - key: SMTP_PASSWORD
        sync: false
```

---

## 4. Admin Bootstrap & Promotion Workflow

1. Configure `ADMIN_EMAIL` and `ADMIN_INITIAL_PASSWORD` in Render's environment variables.
2. When the application starts:
   - If the user already exists in the database, their role is automatically promoted to `admin` with `999,999` tokens.
   - If the user does not exist, a new administrator account is securely bootstrapped with a 12-round Bcrypt password hash.
   - Credentials are never logged, printed, or exposed to the frontend.
3. You can log into the studio at `/` using the login modal and access the dedicated **Admin Control Panel** with user management, live token balances, search filters, and ticket response controls.

---

## 5. Security & Isolation Architecture

- **Session Authentication:** HTTP-only, secure, `SameSite=Lax` cookies backed by signed JWTs.
- **Resource Ownership:** Cloned voices, uploaded reference audio, generated speech, and support complaints are strictly tied to `user_id`. Direct URL guessing or unauthorized API requests return `401 Unauthorized` or `403 Forbidden`.
- **Private Audio Streaming:** Audio files are never exposed in public directories; streamed exclusively via `/api/audio/stream/{filename}` after verifying ownership.
- **Password Resets:** Reset codes are 6 digits, cryptographically hashed with SHA-256 in the database, valid for 30 minutes, limited to 5 attempts, and invalidated immediately after first use. Codes are never echoed in API payloads.
- **Credit Enforcement:** Server-side atomic balance checks (1 token per character, min 10 tokens); automatic token refunds if speech synthesis fails.
