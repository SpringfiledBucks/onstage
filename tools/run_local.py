#!/usr/bin/env python3
"""Loopback-only PostgreSQL development launcher."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.version_info < (3, 12):
        raise SystemExit("Python 3.12 or later is required.")
    if not (os.environ.get('DATABASE_URL') or os.environ.get('ONSTAGE_DATABASE_URL')):
        raise SystemExit("DATABASE_URL is required. Use an isolated PostgreSQL database and run tools/database.py migrate first; SQLite is retired.")
    data = ROOT / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    os.environ["ONSTAGE_DATA_DIR"] = str(data)
    os.environ["ONSTAGE_PUBLIC_ORIGIN"] = "http://127.0.0.1:8765"
    os.environ["ONSTAGE_SECURE_COOKIE"] = "0"
    os.environ["ONSTAGE_VERSION"] = (ROOT / "VERSION").read_text().strip()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("Install requirements.txt in the project virtual environment first.")
    print("Local demo: http://127.0.0.1:8765")
    print("Guests can browse public content; register/login to participate.")
    print("Use separate browser profiles/devices for separate people. Tabs share identity.")
    uvicorn.run("app.main:app", host="127.0.0.1", port=8765,
                workers=1, access_log=False, ws_per_message_deflate=False)


if __name__ == "__main__":
    main()
