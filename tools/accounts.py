"""Explicit local operator grant/revoke; credentials are read only via settings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy.exc import SQLAlchemyError
from app.security import DomainError, Settings
from app.store import create_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator', required=True, help='Private operator audit label')
    parser.add_argument('action', choices=['grant-admin', 'revoke-admin'])
    parser.add_argument('username')
    args = parser.parse_args()
    settings = Settings.from_values(public_origin='http://localhost')
    if not settings.database_url:
        parser.error('DATABASE_URL is required; only PostgreSQL is supported. No database is created.')
    store = create_store(settings)
    try:
        store.ready()
        result = store.operator_grant_admin(args.username, enabled=args.action == 'grant-admin', operator=args.operator)
    except DomainError as error:
        print(json.dumps({'error': error.detail, 'status': error.status_code}, ensure_ascii=False), file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print('Database operation failed; run the controlled migration if needed.', file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
