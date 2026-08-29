"""Phase 5.3 follow-up: close the four model/schema divergences the new
migration gate found on its first contact with the real tree.

None of these was a correctness bug, which is exactly why all four survived
four phases unnoticed: nothing compared the models against the deployed
schema until `scripts/check_migrations.py` existed. They are fixed here so
that gate starts from **zero** recorded exceptions rather than four.

That matters more than the individual fixes. `KNOWN_DIVERGENCES` is a
snapshot assertion, not an ignore list — it fails when an entry disappears
as well as when one appears — so leaving four entries in it means four
places where a genuine future drift could land on a pre-approved key and
pass. An empty baseline is a real gate; a populated one is a gate with
four holes in it.

**Two redundant unique constraints.** `f6a7b8c9d0e1_transcripts.py` and
`c3d4e5f6a7b8_auth_hardening.py` each wrote `sa.UniqueConstraint(col)`
*and* a unique index on the same column, while the models
(`mapped_column(unique=True, index=True)`) declare only the index. Verified
against the live database before dropping — `\\d transcripts` showed both
`ix_transcripts_encounter_id UNIQUE, btree` and
`transcripts_encounter_id_key UNIQUE CONSTRAINT, btree` on `encounter_id`.
Two B-trees over one column, both maintained on every insert, for one
guarantee. The index is kept and the constraint dropped rather than the
reverse: the models ask for an index, and a unique index enforces
uniqueness just as completely.

**Two enum columns wider than their models declare.** Both were created as
plain `String` in the initial schema and became `Enum(native_enum=False)`
in Phase 0.4 (decision 0010) with no accompanying `ALTER`. A non-native
Enum *is* a VARCHAR whose width SQLAlchemy derives from the longest member,
so the deployed columns were simply wider than asked — every legal value
fit, and the CHECK constraints 0.4 added are what actually enforce
membership. Narrowing them costs a table scan on two small tables and buys
a schema that matches its own models.

⚠️ Narrowing means a future enum member longer than the new width needs its
own migration. That is the correct outcome and the gate now enforces it:
`KNOWN_DIVERGENCES` keys contain every enum member, so adding a state
fails the check on purpose — which is the moment to confirm the column is
still wide enough.
"""

from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e4f5a6"
down_revision = "a0b1c2d3e4f5"
branch_labels = None
depends_on = None

# Longest member of each enum, which is what SQLAlchemy renders a
# non-native Enum's VARCHAR width from. Spelled out rather than computed
# so the arithmetic is auditable in review: 'withdrawn' is 9,
# 'transcription_failed' is 20.
_CONSENT_EVENT_WIDTH = 9
_PIPELINE_STATUS_WIDTH = 20

# The widths the columns actually had before this revision, needed for a
# truthful downgrade.
_CONSENT_EVENT_PRIOR_WIDTH = 16
_PIPELINE_STATUS_PRIOR_WIDTH = 32


def upgrade() -> None:
    # --- the redundant unique constraints -------------------------------
    # The matching unique *indexes* (ix_transcripts_encounter_id,
    # ix_refresh_tokens_token_hash) are deliberately left in place; they are
    # what the models declare and they enforce the same uniqueness.
    op.drop_constraint("transcripts_encounter_id_key", "transcripts", type_="unique")
    op.drop_constraint("refresh_tokens_token_hash_key", "refresh_tokens", type_="unique")

    # --- the over-wide enum columns -------------------------------------
    # Narrowing only. Every stored value is already constrained to the enum
    # members by the CHECK constraints added in 0.4, and the longest member
    # is shorter than the new width, so no row can fail the implicit
    # length check this performs.
    op.alter_column(
        "consent_ledger_entries",
        "event",
        existing_type=sa.String(length=_CONSENT_EVENT_PRIOR_WIDTH),
        type_=sa.String(length=_CONSENT_EVENT_WIDTH),
        existing_nullable=False,
    )
    op.alter_column(
        "encounters",
        "pipeline_status",
        existing_type=sa.String(length=_PIPELINE_STATUS_PRIOR_WIDTH),
        type_=sa.String(length=_PIPELINE_STATUS_WIDTH),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Widening first: if the constraint re-creation were to fail, the
    # columns are still at their original widths rather than half-reverted.
    op.alter_column(
        "encounters",
        "pipeline_status",
        existing_type=sa.String(length=_PIPELINE_STATUS_WIDTH),
        type_=sa.String(length=_PIPELINE_STATUS_PRIOR_WIDTH),
        existing_nullable=False,
    )
    op.alter_column(
        "consent_ledger_entries",
        "event",
        existing_type=sa.String(length=_CONSENT_EVENT_WIDTH),
        type_=sa.String(length=_CONSENT_EVENT_PRIOR_WIDTH),
        existing_nullable=False,
    )
    op.create_unique_constraint("refresh_tokens_token_hash_key", "refresh_tokens", ["token_hash"])
    op.create_unique_constraint("transcripts_encounter_id_key", "transcripts", ["encounter_id"])
