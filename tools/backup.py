#!/usr/bin/env python3
"""Retired SQLite backup helper, retained only for historical regression tests.

PostgreSQL uses DATABASE_URL and tools/database.py backup DESTINATION; do not
point this SQLite-specific helper at a disconnected legacy file. Its CLI fails
explicitly and does not open or modify any database.
"""
import argparse
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile


def make_backup(source: Path, destination: Path):
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination or destination.exists():
        raise ValueError("Destination must be a new file, distinct from source.")
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".onstage-backup-", suffix=".sqlite3",
                                          dir=destination.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=15)) as src:
            with closing(sqlite3.connect(temporary, timeout=15)) as dst:
                src.backup(dst)
                integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
                violations = dst.execute("PRAGMA foreign_key_check").fetchall()
                if integrity != "ok" or violations:
                    raise RuntimeError("Backup consistency check failed.")
                dst.commit()
        # Both connections are closed before publishing (also required on Windows).
        # A same-directory hard link creates the final name atomically and fails
        # if another backup already owns it; no check/replace overwrite race.
        os.link(temporary, destination)
        os.chmod(destination, 0o600)
        return {"backup": str(destination), "integrity": "ok",
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "size_bytes": destination.stat().st_size}
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Retired SQLite backup CLI. Use PostgreSQL tools/database.py backup DESTINATION.")
    parser.error("SQLite operations are retired; use tools/database.py backup DESTINATION with DATABASE_URL. Existing files are preserved.")


if __name__ == "__main__":
    main()
