"""Classify source items (conversation / event / notification / marketing).

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade():
    # Populated by the spine stage (graph/item_class.py), not here: classification reads message bodies.
    op.execute("""
        ALTER TABLE source_items ADD COLUMN item_class text;
        CREATE INDEX source_items_item_class_idx ON source_items (source, item_class);
        CREATE INDEX nodes_hidden_idx ON nodes (id)
        WHERE props->>'hidden' = 'true';
    """)


def downgrade():
    op.execute("""
        DROP INDEX IF EXISTS nodes_hidden_idx;
        DROP INDEX IF EXISTS source_items_item_class_idx;
        ALTER TABLE source_items DROP COLUMN IF EXISTS item_class;
    """)
