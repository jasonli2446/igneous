"""Core schema: source items, graph, chunks, sync state.

Revision ID: 0001
"""
from alembic import op

revision = "0001"
down_revision = None

UP = """
CREATE EXTENSION IF NOT EXISTS vector;

-- Everything a connector produces, normalized. (source, external_id) makes ingestion idempotent.
CREATE TABLE source_items (
    id            bigserial PRIMARY KEY,
    source        text NOT NULL,                 -- email | gmail | drive | calendar | contacts | fs
    external_id   text NOT NULL,
    parent_id     bigint REFERENCES source_items(id) ON DELETE CASCADE,  -- attachment -> message
    content_hash  text NOT NULL,
    mime          text,
    title         text,
    body_text     text,
    raw_meta      jsonb NOT NULL DEFAULT '{}',
    occurred_at   timestamptz,
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, external_id)
);
CREATE INDEX ON source_items (updated_at);

-- Node types: Person, Organization, Thread, Message, File, Folder, Event, Topic.
CREATE TABLE nodes (
    id              bigserial PRIMARY KEY,
    type            text NOT NULL,
    key             text NOT NULL,               -- natural key, e.g. email address or source_item id
    name            text NOT NULL,
    props           jsonb NOT NULL DEFAULT '{}',
    source_item_id  bigint REFERENCES source_items(id) ON DELETE CASCADE,
    UNIQUE (type, key)
);
CREATE INDEX nodes_name_trgm ON nodes USING gin (to_tsvector('simple', name));

-- Edge types: SENT, RECEIVED, MEMBER_OF, IN_THREAD, REPLIES_TO, ATTACHED, CONTAINS, ATTENDED,
-- AUTHORED, HAS_CHUNK, MENTIONS, ABOUT, RELATED_TO, SAME_AS.
-- Provenance columns are NULL for deterministic (stage one) edges and required for model-derived ones.
CREATE TABLE edges (
    id                 bigserial PRIMARY KEY,
    src                bigint NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst                bigint NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    type               text NOT NULL,
    props              jsonb NOT NULL DEFAULT '{}',
    valid_from         timestamptz,
    valid_to           timestamptz,
    extractor          text NOT NULL DEFAULT 'spine',
    extractor_version  text NOT NULL DEFAULT '1',
    model              text,
    confidence         real,
    source_item_id     bigint REFERENCES source_items(id) ON DELETE CASCADE,
    span_start         int,
    span_end           int,
    UNIQUE (src, dst, type, extractor)
);
CREATE INDEX ON edges (src, type);
CREATE INDEX ON edges (dst, type);

CREATE TABLE chunks (
    id              bigserial PRIMARY KEY,
    source_item_id  bigint NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    node_id         bigint REFERENCES nodes(id) ON DELETE SET NULL,
    ord             int NOT NULL,
    text            text NOT NULL,
    meta            jsonb NOT NULL DEFAULT '{}',
    tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding       vector({dim}),
    UNIQUE (source_item_id, ord)
);
CREATE INDEX ON chunks USING gin (tsv);
CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE sync_state (
    connector   text NOT NULL,
    account     text NOT NULL DEFAULT 'default',
    cursor      jsonb NOT NULL DEFAULT '{}',
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (connector, account)
);

-- Per-stage watermark so every pipeline stage is incremental and resumable.
CREATE TABLE pipeline_state (
    stage       text PRIMARY KEY,
    watermark   timestamptz NOT NULL DEFAULT 'epoch',
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""

DOWN = """
DROP TABLE IF EXISTS pipeline_state, sync_state, chunks, edges, nodes, source_items CASCADE;
"""


def upgrade():
    from igneous.config import settings

    op.execute(UP.replace("{dim}", str(settings.embedding_dim)))


def downgrade():
    op.execute(DOWN)
