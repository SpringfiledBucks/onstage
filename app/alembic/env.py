from alembic import context

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use the controlled tools/database.py migration command.")
context.configure(connection=connection, target_metadata=None, transactional_ddl=True)
with context.begin_transaction():
    context.run_migrations()
