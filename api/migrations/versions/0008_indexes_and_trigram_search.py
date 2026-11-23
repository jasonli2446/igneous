"""Indexes behind every foreign key and the hot filters, trigram name search, no unused parent link.

Revision ID: 0008
Revises: 0007
"""
from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade():
    op.execute("""
        -- Each foreign key gets an index, so deleting a source item or a node finds its referencing rows directly
        -- instead of scanning nodes, edges, evidence and chunks once per deleted row.
        CREATE INDEX nodes_source_item_idx ON nodes (source_item_id) WHERE source_item_id IS NOT NULL;
        CREATE INDEX edges_source_item_idx ON edges (source_item_id) WHERE source_item_id IS NOT NULL;
        CREATE INDEX edge_evidence_source_item_idx ON edge_evidence (source_item_id)
            WHERE source_item_id IS NOT NULL;
        CREATE INDEX chunks_node_idx ON chunks (node_id) WHERE node_id IS NOT NULL;
        CREATE INDEX source_items_occurred_at_idx ON source_items (occurred_at);

        -- No connector stores a record as part of another one.
        ALTER TABLE source_items DROP COLUMN parent_id;

        -- Name search matches substrings (ILIKE) and near spellings (pg_trgm similarity) through trigram indexes.
        DROP INDEX nodes_name_trgm;
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX nodes_name_trgm ON nodes USING gin (name gin_trgm_ops);
        CREATE INDEX nodes_person_key_trgm ON nodes USING gin (key gin_trgm_ops) WHERE type = 'Person';
    """)


def downgrade():
    op.execute("""
        DROP INDEX nodes_person_key_trgm;
        DROP INDEX nodes_name_trgm;
        CREATE INDEX nodes_name_trgm ON nodes USING gin (to_tsvector('simple', name));
        ALTER TABLE source_items ADD COLUMN parent_id bigint REFERENCES source_items(id) ON DELETE CASCADE;
        DROP INDEX source_items_occurred_at_idx;
        DROP INDEX chunks_node_idx;
        DROP INDEX edge_evidence_source_item_idx;
        DROP INDEX edges_source_item_idx;
        DROP INDEX nodes_source_item_idx;
    """)
