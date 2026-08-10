"""List users from the database pointed at by DATABASE_URL (diagnostic only)."""

import os

import psycopg2

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise SystemExit("Set DATABASE_URL before running this script.")

conn = psycopg2.connect(DATABASE_URL)
try:
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, email, full_name FROM users ORDER BY username")
    users = cur.fetchall()
    print("Users:")
    for user in users:
        print(user)
finally:
    conn.close()
