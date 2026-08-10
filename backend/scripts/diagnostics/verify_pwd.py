"""Verify a user's password hash against DATABASE_URL (diagnostic only)."""

import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from modules.auth.services import AuthService

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("Set DATABASE_URL before running this script.")

username = sys.argv[1] if len(sys.argv) > 1 else "admin"
password = sys.argv[2] if len(sys.argv) > 2 else "admin123"

conn = psycopg2.connect(DATABASE_URL)
try:
    cur = conn.cursor()
    cur.execute("SELECT password_hash FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if not row:
        print(f"User {username!r} not found")
    else:
        is_valid = AuthService.verify_password(password, row[0])
        print(f"Password check for {username!r}: {'valid' if is_valid else 'invalid'}")
finally:
    conn.close()
