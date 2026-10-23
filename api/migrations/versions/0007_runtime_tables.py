"""Tables the app used to create on first use (#181): jobs, tokens, dirty markers, overrides, the thread registry,
edge verdicts.

Every statement is idempotent: on a database where the app already created these tables, they are kept with their
data and only brought up to date (the registry's later columns and status values).

Revision ID: 0007
Revises: 0006
"""
from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade():
    op.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id          text PRIMARY KEY,
            kind        text NOT NULL,            -- sync | pipeline | connect
            source      text NOT NULL,            -- connector id, 'pipeline', or 'google_connect'
            account     text,
            status      text NOT NULL DEFAULT 'running',   -- running | succeeded | failed
            started_at  timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            progress    jsonb NOT NULL DEFAULT '{}',
            result      jsonb,
            error       text);
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_running ON jobs (source) WHERE status = 'running';

        CREATE TABLE IF NOT EXISTS oauth_tokens (
            provider text NOT NULL, account text NOT NULL, token_enc bytea NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (provider, account));

        CREATE TABLE IF NOT EXISTS dirty_nodes (
            node_id bigint PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
            reason text NOT NULL,
            marked_at timestamptz NOT NULL DEFAULT now());
        CREATE TABLE IF NOT EXISTS dirty_rebuild (id bool PRIMARY KEY DEFAULT true CHECK (id));
        CREATE TABLE IF NOT EXISTS graph_version (id bool PRIMARY KEY DEFAULT true CHECK (id),
                                                  version bigint NOT NULL DEFAULT 0);
        INSERT INTO graph_version (id) VALUES (true) ON CONFLICT DO NOTHING;

        CREATE TABLE IF NOT EXISTS overrides (
            id         bigserial PRIMARY KEY,
            op         text NOT NULL CHECK (op IN ('rename', 'hide', 'merge', 'unlink')),
            node_keys  text[] NOT NULL,      -- natural keys 'Type:key', so they survive node-id churn on rebuild
            value      text,                 -- rename: the new name
            data       jsonb,                -- unlink: snapshot of the dropped edges, for undo
            created_at timestamptz NOT NULL DEFAULT now());

        CREATE TABLE IF NOT EXISTS thread_registry (
            key        text PRIMARY KEY,
            name       text NOT NULL,
            kind       text,
            terms      text[] NOT NULL,          -- the words that define it (match = membership)
            span_start timestamptz,
            span_end   timestamptz,
            status     text NOT NULL DEFAULT 'active',
            merged_into text,                         -- for status 'merged': the surviving thread's key
            pinned     boolean NOT NULL DEFAULT false,   -- gold set or a user correction: never dropped
            origin     text NOT NULL DEFAULT 'burst',    -- burst | name | gold | user | profile
            confidence real,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now());
        -- Registries the app created before these existed.
        ALTER TABLE thread_registry ADD COLUMN IF NOT EXISTS merged_into text;
        ALTER TABLE thread_registry ADD COLUMN IF NOT EXISTS anchor text;       -- series | repo | chat | course
        ALTER TABLE thread_registry ADD COLUMN IF NOT EXISTS anchor_name text;  -- the seed's own name
        ALTER TABLE thread_registry DROP CONSTRAINT IF EXISTS thread_registry_status_check;
        ALTER TABLE thread_registry ADD CONSTRAINT thread_registry_status_check
            CHECK (status IN ('active', 'rejected', 'merged'));
        CREATE TABLE IF NOT EXISTS thread_parents (
            child_key text NOT NULL, parent_key text NOT NULL, origin text NOT NULL DEFAULT 'gold',
            PRIMARY KEY (child_key, parent_key));

        CREATE TABLE IF NOT EXISTS edge_verdicts (
            src_key    text NOT NULL,
            dst_key    text NOT NULL,
            type       text NOT NULL,
            extractor  text NOT NULL,
            evidence   text NOT NULL,          -- hash of the evidence the verdict was made on
            verdict    text NOT NULL CHECK (verdict IN ('keep', 'drop', 'unsure')),
            confidence real,
            reason     text,
            source     text NOT NULL DEFAULT 'model',   -- 'model' or 'user' (a correction; never overwritten)
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (src_key, dst_key, type, extractor));
        -- Edges the verifier dropped, kept for the why-view and undo; same columns as edges.
        CREATE TABLE IF NOT EXISTS edges_dropped (LIKE edges INCLUDING DEFAULTS);
    """)


def downgrade():
    # These tables predate this migration (the app created them at runtime) and hold user data: tokens,
    # corrections, verdicts. Downgrading leaves them in place.
    pass
