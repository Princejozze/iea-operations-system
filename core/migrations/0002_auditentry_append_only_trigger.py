"""
R7, database layer: reject UPDATE and DELETE on core_auditentry even from raw
SQL that never touches Django. INSERT stays allowed.

PostgreSQL only (the production database, CLAUDE.md §1). On any other backend
this migration is a no-op; the model, queryset and admin layers still apply.

Limits worth knowing:
- Row-level triggers do not fire on TRUNCATE. Django's test runner relies on
  that to reset tables between TransactionTestCase runs. In production, the
  application's database role should not be granted TRUNCATE on this table.
- A PostgreSQL superuser can disable triggers. The application must connect
  as an ordinary role, never as a database superuser.
"""

from django.db import migrations

FORWARD_SQL = """
CREATE OR REPLACE FUNCTION core_auditentry_reject_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'core_auditentry is append-only (R7): % refused', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER core_auditentry_append_only
    BEFORE UPDATE OR DELETE ON core_auditentry
    FOR EACH ROW EXECUTE FUNCTION core_auditentry_reject_change();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS core_auditentry_append_only ON core_auditentry;
DROP FUNCTION IF EXISTS core_auditentry_reject_change();
"""


def forwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD_SQL, params=None)  # verbatim: RAISE uses a literal %


def backwards(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE_SQL, params=None)


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunPython(forwards, backwards)]
