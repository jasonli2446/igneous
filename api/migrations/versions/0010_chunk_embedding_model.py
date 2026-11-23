"""Record which embedding model produced each chunk's vector.

The embed stage re-embeds a chunk whose vector came from a model other than IGNEOUS_EMBEDDING_MODEL, and dense search
compares only vectors from that model, so changing the model never mixes two vector spaces. Every vector stored before
this column existed came from BAAI/bge-small-en-v1.5, the model Igneous ships with; a database embedded with another
model is re-embedded once.

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade():
    op.execute("""
        ALTER TABLE chunks ADD COLUMN embedding_model text;
        UPDATE chunks SET embedding_model = 'BAAI/bge-small-en-v1.5' WHERE embedding IS NOT NULL;
        -- A vector and the name of the model that made it are written together.
        ALTER TABLE chunks ADD CONSTRAINT chunks_embedding_has_model
            CHECK ((embedding IS NULL) = (embedding_model IS NULL));
    """)


def downgrade():
    op.execute("""
        ALTER TABLE chunks DROP CONSTRAINT chunks_embedding_has_model;
        ALTER TABLE chunks DROP COLUMN embedding_model;
    """)
