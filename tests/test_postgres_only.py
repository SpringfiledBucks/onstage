"""Current entry points never create or import a retired SQLite database."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from app.security import Settings
from app.store import create_store

ROOT = Path(__file__).resolve().parents[1]


def without_database(tmp_path):
    env = dict(os.environ)
    env.pop('DATABASE_URL', None)
    env.pop('ONSTAGE_DATABASE_URL', None)
    env.update(ONSTAGE_DATA_DIR=str(tmp_path), ONSTAGE_PUBLIC_ORIGIN='http://localhost')
    return env


def test_factory_requires_postgres_and_preserves_old_file(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('ONSTAGE_DATABASE_URL', raising=False)
    old = tmp_path / 'onstage.sqlite3'
    old.write_bytes(b'retired-database-must-remain-untouched')
    with pytest.raises(RuntimeError, match='DATABASE_URL is required'):
        create_store(Settings.from_values(data_dir=tmp_path, public_origin='http://localhost'))
    assert old.read_bytes() == b'retired-database-must-remain-untouched'
    assert list(tmp_path.iterdir()) == [old]
    with pytest.raises(RuntimeError, match='must use PostgreSQL'):
        create_store(Settings.from_values(database_url='sqlite:///ignored.sqlite3', public_origin='http://localhost'))
    assert not (ROOT / 'ignored.sqlite3').exists()


@pytest.mark.parametrize('arguments', [
    ['-c', 'import app.main'],
    ['tools/database.py', 'migrate'],
    ['tools/accounts.py', '--operator', 'test', 'grant-admin', 'someone'],
    ['tools/moderate.py', 'list'],
    ['tools/backup.py', 'old.sqlite3', 'new.sqlite3'],
    ['tools/run_local.py'],
    ['tools/database.py', 'import-sqlite', 'old.sqlite3'],
])
def test_entrypoints_fail_closed_without_database(arguments, tmp_path):
    result = subprocess.run([sys.executable, *arguments], cwd=ROOT,
                            env=without_database(tmp_path), capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert 'DATABASE_URL' in result.stderr or 'invalid choice' in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_factory_uses_explicit_postgres_without_connecting(tmp_path):
    from app.postgres import PostgresStore
    store = create_store(Settings.from_values(data_dir=tmp_path,
        database_url='postgresql+psycopg://onstage@127.0.0.1:1/import_only', public_origin='http://localhost'))
    try:
        assert isinstance(store, PostgresStore)
        assert not tmp_path.exists() or not list(tmp_path.iterdir())
    finally:
        store.close()
