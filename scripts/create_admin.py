"""
Create the first super_admin account.

Usage:
    python scripts/create_admin.py

Prompts for email, full name, and password then:
  - hashes the password with bcrypt (cost 14)
  - generates a TOTP secret and encrypts it with ADMIN_TOTP_ENCRYPTION_KEY
  - inserts the admin_users document into MongoDB
  - prints the TOTP provisioning URI to scan with Google Authenticator
"""

import asyncio
import os
import sys
from pathlib import Path

# Load .env from backend root
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import bcrypt
import pyotp
from cryptography.fernet import Fernet
from motor.motor_asyncio import AsyncIOMotorClient
from uuid import uuid4
from datetime import datetime, timezone

MONGODB_URL = os.environ["MONGODB_URL"]
MONGODB_DB  = os.environ.get("MONGODB_DB", "opshero")
TOTP_KEY    = os.environ["ADMIN_TOTP_ENCRYPTION_KEY"]


async def main():
    print("\n── OpsHero super_admin seed ───────────────────────────────")
    email     = input("Email          : ").strip()
    full_name = input("Full name      : ").strip()
    password  = input("Password       : ").strip()

    if not email or not password:
        print("ERROR: email and password are required.")
        sys.exit(1)

    # Hash password (bcrypt cost 14, compatible with passlib verify at login)
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=14)).decode()

    # Generate + encrypt TOTP secret
    totp_secret = pyotp.random_base32()
    fernet      = Fernet(TOTP_KEY.encode())
    totp_enc    = fernet.encrypt(totp_secret.encode()).decode()

    admin_id = str(uuid4())
    doc = {
        "id":            admin_id,
        "email":         email,
        "password_hash": pw_hash,
        "totp_secret":   totp_enc,
        "totp_enabled":  True,
        "full_name":     full_name,
        "role":          "super_admin",
        "permissions": {
            "can_manage_patterns":      True,
            "can_review_contributions": True,
            "can_manage_users":         True,
            "can_view_billing":         True,
            "can_manage_config":        True,
            "can_delete_users":         True,
        },
        "failed_attempts": 0,
        "locked_until":    None,
        "created_at":      datetime.now(timezone.utc),
        "created_by":      None,
        "is_active":       True,
    }

    # Insert into MongoDB
    client = AsyncIOMotorClient(MONGODB_URL)
    db     = client[MONGODB_DB]

    existing = await db.admin_users.find_one({"email": email})
    if existing:
        print(f"\nERROR: An admin with email {email!r} already exists.")
        client.close()
        sys.exit(1)

    await db.admin_users.insert_one(doc)
    client.close()

    # TOTP provisioning URI for Google Authenticator
    totp_uri = pyotp.totp.TOTP(totp_secret).provisioning_uri(
        name=email,
        issuer_name="OpsHero Admin",
    )

    print("\n── Account created ────────────────────────────────────────")
    print(f"  Email    : {email}")
    print(f"  Role     : super_admin")
    print(f"  Admin ID : {admin_id}")
    print("\n── Google Authenticator setup ─────────────────────────────")
    print("  Scan this URI in Google Authenticator (or any TOTP app):")
    print(f"\n  {totp_uri}\n")
    print("  Or enter the secret key manually:")
    print(f"  Secret : {totp_secret}")
    print("\n  You will need the 6-digit code from the app to log in.")
    print("────────────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    asyncio.run(main())
