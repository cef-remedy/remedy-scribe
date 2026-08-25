"""append-only consent ledger

Backs P0-1 ("Immutable, append-only consent ledger") at the DB layer, not
just by application convention. A REVOKE on the table wouldn't be enough
here — the app's own DB role owns the table it created, and table owners
bypass GRANT/REVOKE in Postgres. A BEFORE UPDATE/DELETE trigger that
raises does apply to the owner too, so this holds even if the API's DB
credentials are fully compromised.

Revision ID: a1c2e3f4b5d6
Revises: 12b40b94de66
Create Date: 2026-08-18 19:41:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1c2e3f4b5d6"
down_revision = "12b40b94de66"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_consent_ledger_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'consent_ledger_entries is append-only (P0-1) — % is not permitted', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER consent_ledger_entries_no_mutation
        BEFORE UPDATE OR DELETE ON consent_ledger_entries
        FOR EACH ROW EXECUTE FUNCTION reject_consent_ledger_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS consent_ledger_entries_no_mutation ON consent_ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS reject_consent_ledger_mutation")
