#!/usr/bin/env python3
"""Private PostgreSQL moderation. Uses the authorized DATABASE_URL Secret.

Output contains report contents. Keep it in an operator-only terminal; do not
publish it in application logs or source bundles. This tool never impersonates
a browser session and never creates or automatically migrates a database.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.security import DomainError, Settings
from app.store import create_store
from sqlalchemy.exc import SQLAlchemyError


def positive_limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="Read reports, including reports about a host")
    listing.add_argument("--status", choices=["open", "resolved", "dismissed", "all"], default="open")
    listing.add_argument("--after", type=int, default=0)
    listing.add_argument("--limit", type=positive_limit, default=50)
    resolution = commands.add_parser("resolve", help="Close one report with a durable operator note")
    resolution.add_argument("report_id")
    resolution.add_argument("--status", choices=["resolved", "dismissed"], required=True)
    resolution.add_argument("--note", required=True)
    resolution.add_argument("--operator", required=True, help="Private audit label; not a browser identity")
    ending = commands.add_parser("end-room", help="End a room whose host cannot act; writes an operator audit record")
    ending.add_argument("room_id")
    ending.add_argument("--note", required=True)
    ending.add_argument("--operator", required=True, help="Private audit label; not a browser identity")
    args = parser.parse_args()
    if args.command == "list" and args.after < 0:
        parser.error("--after must not be negative")
    settings = Settings.from_values(public_origin="http://localhost")
    if not settings.database_url:
        parser.error("DATABASE_URL is required; only PostgreSQL is supported. Existing SQLite files are preserved.")
    store = create_store(settings)
    try:
        store.ready()  # Requires the current schema; no initialize()/migration in this CLI.
        if args.command == "list":
            result = store.operator_list_reports(args.status, args.after, args.limit)
        elif args.command == "resolve":
            result = store.operator_resolve_report(args.report_id, args.status, args.note, args.operator)
        else:
            result = store.operator_end_room(args.room_id, args.note, args.operator)
    except DomainError as error:
        print(json.dumps({"error": error.detail, "status": error.status_code}, ensure_ascii=False), file=sys.stderr)
        return 1
    except SQLAlchemyError as error:
        print(json.dumps({"error": "Database operation failed", "exception": type(error).__name__}), file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
