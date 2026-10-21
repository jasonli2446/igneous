"""Persist evidence and cached verification for derived entities.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade():
    op.execute("""
        CREATE TABLE llm_cache (
            cache_key text PRIMARY KEY,
            response jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE edge_evidence (
            id bigserial PRIMARY KEY,
            edge_id bigint NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
            source_item_id bigint REFERENCES source_items(id) ON DELETE SET NULL,
            kind text NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'
        );
        CREATE INDEX edge_evidence_edge_id_idx ON edge_evidence (edge_id);
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS edge_evidence, llm_cache;")
