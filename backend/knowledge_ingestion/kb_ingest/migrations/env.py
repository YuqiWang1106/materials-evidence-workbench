from alembic import context
from kb_ingest.storage.models import Base
from kb_ingest.storage.sqlite import migration_transaction

connection = context.config.attributes["connection"]
with migration_transaction(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, transactional_ddl=True)
    context.run_migrations()
