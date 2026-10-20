"""Versioned, additive community migrations. Never invoked by PostgreSQL startup."""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

REVISION = "20261020_02"


def migrate_extensions(connection):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).with_name("alembic")))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


def migrate_sqlite_extensions(path):
    engine = create_engine("sqlite:///" + str(path))
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            migrate_extensions(connection)
    finally:
        engine.dispose()
