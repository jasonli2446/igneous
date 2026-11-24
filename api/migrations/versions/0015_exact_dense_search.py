"""Dense search compares every vector, so the approximate vector index goes.

Dense search (rag/search.py) finds the nearest passages exactly: the query against every vector the filters let
through, equal distances in chunk id order. The HNSW index answered approximately. Its graph is random, so two builds
of the same records found different passages, and under a filter it returned only the candidates it had found before
filtering. No query reads it now. Kept, it would slow every embedding write, and a planner that can sort on it
(Postgres 17 and later) could answer approximately again. eval/README.md has the measurements.

Revision ID: 0015
Revises: 0014
"""
from alembic import op

revision = "0015"
down_revision = "0014"


def upgrade():
    op.execute("DROP INDEX chunks_embedding_idx")


def downgrade():
    op.execute("CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops)")
