"""Per-record change tracking for the spine and chunk stages.

`source_items.spined_hash` / `chunked_hash` hold the content hash the spine / chunk stage last processed. A record is
pending for a stage while its marker differs from `content_hash`; a content change clears both markers, and a stage
writes back only the hash it read, so a change made while it runs stays pending. Partial indexes cover pending rows.
They replace `pipeline_state` (timestamp watermarks): records at or before a stage's watermark count as processed.

Revision ID: 0010
Revises: 0008
"""
from alembic import op

revision = "0010"
down_revision = "0008"


def upgrade():
    op.execute("""
        ALTER TABLE source_items ADD COLUMN spined_hash text, ADD COLUMN chunked_hash text;
        UPDATE source_items si SET spined_hash = si.content_hash
        FROM pipeline_state ps WHERE ps.stage = 'spine' AND si.updated_at <= ps.watermark;
        UPDATE source_items si SET chunked_hash = si.content_hash
        FROM pipeline_state ps WHERE ps.stage = 'chunk' AND si.updated_at <= ps.watermark;
        DROP TABLE pipeline_state;
        CREATE INDEX source_items_spine_pending_idx ON source_items (id) WHERE spined_hash IS DISTINCT FROM content_hash;
        CREATE INDEX source_items_chunk_pending_idx ON source_items (id) WHERE chunked_hash IS DISTINCT FROM content_hash;

        CREATE FUNCTION source_items_content_changed() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.content_hash IS DISTINCT FROM OLD.content_hash THEN
                NEW.spined_hash := NULL;
                NEW.chunked_hash := NULL;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER source_items_content_changed BEFORE UPDATE OF content_hash ON source_items
            FOR EACH ROW EXECUTE FUNCTION source_items_content_changed();
    """)


def downgrade():
    op.execute("""
        DROP TRIGGER source_items_content_changed ON source_items;
        DROP FUNCTION source_items_content_changed();
        DROP INDEX source_items_chunk_pending_idx, source_items_spine_pending_idx;
        ALTER TABLE source_items DROP COLUMN chunked_hash, DROP COLUMN spined_hash;
        CREATE TABLE pipeline_state (
            stage       text PRIMARY KEY,
            watermark   timestamptz NOT NULL DEFAULT 'epoch',
            updated_at  timestamptz NOT NULL DEFAULT now());
    """)
