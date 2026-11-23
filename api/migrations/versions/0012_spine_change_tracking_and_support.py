"""Spine change tracking and provenance.

- `source_items.spined_hash` / `chunked_hash`: the content hash the spine / chunk stage last processed. A record is
  pending for a stage while its marker differs from `content_hash`; a content change clears both markers, and a stage
  writes back only the hash it read, so a change made while it runs stays pending. Partial indexes cover pending rows.
- `spine_node_support` / `spine_edge_support`: which records each spine-built node and edge was built from, what each
  record contributes to a node (the name it gives, `rank` saying how strongly, and its props), and the interval each
  record observed an edge. Processing a record replaces its rows; deleting the record deletes them. The spine
  recomputes a node's name and props from the rows that remain (graph/spine.py SPINE_PROPS).
- `spine_gc_nodes` / `spine_gc_edges`: nodes and edges that lost a supporting record, queued by triggers whatever
  removed it (a reprocessed record, a sync's prune or tombstone, any DELETE on source_items). The spine deletes those no
  record supports any more and re-derives what depended on them.
- `spine_quotes`: the quoted "Original Message" header of each message that quotes one, so a newly built message is
  matched to the replies that quote it without reading every body again.
- `nodes.source_item_id` / `edges.source_item_id` are ON DELETE SET NULL, so a node or edge several records share (an
  event series, an email filed in two folders) stays while any of them remains; the spine points it at another of its
  records, or garbage-collects it. What other stages hang off one record alone goes with it
  (`source_items_take_derived`, before the record is deleted): their nodes that point at it, their edges whose records
  (source_item_id and edge_evidence) are only it, and the evidence it gave. Your corrections stay.
- `pipeline_state` (timestamp watermarks) is dropped, and with it the bounded temporal backfill's index.

An existing database keeps its chunk progress (records at or before the chunk watermark count as chunked) but every
record is pending for the spine, so the next run records which records support each node and edge. Every spine-built
node and edge is queued, so whatever no record supports after that run (reply parents that were never ingested,
attendees a later update removed, a LinkedIn company keyed by an email domain) is deleted at its end.

Revision ID: 0012
Revises: 0010
"""
from alembic import op

revision = "0012"
down_revision = "0010"

# Node types and edge extractors the spine builds from records (graph/spine.py: SPINE_TYPES, RECORD_EXTRACTORS).
SPINE_NODES = """
    type IN ('Message', 'Thread', 'Event', 'File', 'Folder', 'Place')
    OR (type = 'Person' AND key <> 'me' AND key NOT LIKE 'name:%')
    OR (type = 'Organization' AND props->>'source' = 'linkedin')
"""
# Edges a deleted record never takes by itself: the spine's (graph/spine.py SPINE_EXTRACTORS) and your corrections.
KEPT = "'spine', 'contacts', 'github', 'linkedin', 'spine-quoted', 'spine-inferred', 'user'"


def upgrade():
    op.execute(f"""
        ALTER TABLE source_items ADD COLUMN spined_hash text, ADD COLUMN chunked_hash text;
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

        -- Replies: In-Reply-To names its parent by Message-ID, in either direction.
        CREATE INDEX source_items_message_id_idx ON source_items ((raw_meta->>'message_id'))
            WHERE source IN ('email', 'gmail');
        CREATE INDEX source_items_in_reply_to_idx ON source_items ((raw_meta->>'in_reply_to'))
            WHERE source IN ('email', 'gmail') AND raw_meta->>'in_reply_to' IS NOT NULL;

        CREATE TABLE spine_node_support (
            node_id         bigint NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
            source_item_id  bigint NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
            represents      boolean NOT NULL DEFAULT false,   -- the node is this record (a message, file, occurrence)
            name            text,                             -- the name this record gives the node
            rank            smallint NOT NULL DEFAULT 0,      -- how strongly (graph/spine.py RANK_*)
            props           jsonb NOT NULL DEFAULT '{{}}',     -- the props it gives the node (SPINE_PROPS)
            PRIMARY KEY (node_id, source_item_id));
        CREATE INDEX spine_node_support_item_idx ON spine_node_support (source_item_id);
        CREATE TABLE spine_edge_support (
            edge_id         bigint NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
            source_item_id  bigint NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
            represents      boolean NOT NULL DEFAULT false,   -- the edge's source_item_id may point at this record
            valid_from      timestamptz,                      -- when this record observed the edge
            valid_to        timestamptz,
            PRIMARY KEY (edge_id, source_item_id));
        CREATE INDEX spine_edge_support_item_idx ON spine_edge_support (source_item_id);

        CREATE TABLE spine_gc_nodes (node_id bigint PRIMARY KEY);
        CREATE TABLE spine_gc_edges (edge_id bigint PRIMARY KEY);
        CREATE FUNCTION spine_queue_nodes() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO spine_gc_nodes (node_id) SELECT DISTINCT node_id FROM removed ON CONFLICT DO NOTHING;
            RETURN NULL;
        END $$;
        CREATE FUNCTION spine_queue_edges() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO spine_gc_edges (edge_id) SELECT DISTINCT edge_id FROM removed ON CONFLICT DO NOTHING;
            RETURN NULL;
        END $$;
        CREATE TRIGGER spine_node_support_removed AFTER DELETE ON spine_node_support
            REFERENCING OLD TABLE AS removed FOR EACH STATEMENT EXECUTE FUNCTION spine_queue_nodes();
        CREATE TRIGGER spine_edge_support_removed AFTER DELETE ON spine_edge_support
            REFERENCING OLD TABLE AS removed FOR EACH STATEMENT EXECUTE FUNCTION spine_queue_edges();

        CREATE TABLE spine_quotes (
            node_id  bigint PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
            sent     timestamp NOT NULL,   -- the quoted Sent time: local wall-clock time, no zone
            sender   text NOT NULL,        -- the quoted From field
            subject  text NOT NULL);       -- the quoted Subject without reply prefixes
        CREATE INDEX spine_quotes_sent_idx ON spine_quotes (sent);

        ALTER TABLE nodes DROP CONSTRAINT nodes_source_item_id_fkey,
            ADD CONSTRAINT nodes_source_item_id_fkey FOREIGN KEY (source_item_id)
                REFERENCES source_items(id) ON DELETE SET NULL;
        ALTER TABLE edges DROP CONSTRAINT edges_source_item_id_fkey,
            ADD CONSTRAINT edges_source_item_id_fkey FOREIGN KEY (source_item_id)
                REFERENCES source_items(id) ON DELETE SET NULL;
        DROP INDEX IF EXISTS edges_temporal_missing_idx;

        -- What other stages hang off one record alone goes with it: their edges whose records (source_item_id and
        -- evidence) are only this one, and their nodes that point at it. The evidence it gave goes too, and an edge
        -- with evidence from elsewhere points at another of its records. The spine's nodes and edges are left to
        -- its provenance (`spine_node_support`, `spine_edge_support`), and your corrections stay.
        CREATE FUNCTION source_items_take_derived() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            hit bigint[];
        BEGIN
            SELECT array_agg(e.id) INTO hit FROM edges e
            WHERE e.id IN (SELECT id FROM edges WHERE source_item_id = OLD.id
                           UNION SELECT edge_id FROM edge_evidence WHERE source_item_id = OLD.id)
              AND e.extractor <> ALL (ARRAY[{KEPT}]);
            DELETE FROM edge_evidence WHERE source_item_id = OLD.id;
            IF hit IS NOT NULL THEN
                DELETE FROM edges e WHERE e.id = ANY (hit) AND COALESCE(e.source_item_id, OLD.id) = OLD.id
                  AND NOT EXISTS (SELECT 1 FROM edge_evidence ev WHERE ev.edge_id = e.id);
                UPDATE edges e SET source_item_id = (SELECT min(ev.source_item_id) FROM edge_evidence ev
                                                     WHERE ev.edge_id = e.id)
                WHERE e.id = ANY (hit) AND e.source_item_id = OLD.id;
            END IF;
            DELETE FROM nodes n WHERE n.source_item_id = OLD.id
              AND NOT EXISTS (SELECT 1 FROM spine_node_support x WHERE x.node_id = n.id);
            RETURN OLD;
        END $$;
        CREATE TRIGGER source_items_take_derived BEFORE DELETE ON source_items
            FOR EACH ROW EXECUTE FUNCTION source_items_take_derived();

        -- Reply parents that were never ingested: placeholders, not messages.
        DELETE FROM nodes WHERE type = 'Message' AND source_item_id IS NULL AND name = '(not ingested)';
        INSERT INTO spine_gc_nodes (node_id) SELECT id FROM nodes WHERE {SPINE_NODES};
        INSERT INTO spine_gc_edges (edge_id) SELECT id FROM edges
            WHERE extractor IN ('spine', 'contacts', 'github', 'linkedin');
    """)


def downgrade():
    op.execute("""
        DROP TRIGGER source_items_take_derived ON source_items;
        DROP FUNCTION source_items_take_derived();
        CREATE INDEX edges_temporal_missing_idx ON edges (id) WHERE valid_from IS NULL OR valid_to IS NULL;
        ALTER TABLE edges DROP CONSTRAINT edges_source_item_id_fkey,
            ADD CONSTRAINT edges_source_item_id_fkey FOREIGN KEY (source_item_id)
                REFERENCES source_items(id) ON DELETE CASCADE;
        ALTER TABLE nodes DROP CONSTRAINT nodes_source_item_id_fkey,
            ADD CONSTRAINT nodes_source_item_id_fkey FOREIGN KEY (source_item_id)
                REFERENCES source_items(id) ON DELETE CASCADE;
        DROP TABLE spine_quotes, spine_gc_edges, spine_gc_nodes, spine_edge_support, spine_node_support;
        DROP FUNCTION spine_queue_edges(), spine_queue_nodes();
        DROP INDEX source_items_in_reply_to_idx, source_items_message_id_idx;
        DROP TRIGGER source_items_content_changed ON source_items;
        DROP FUNCTION source_items_content_changed();
        DROP INDEX source_items_chunk_pending_idx, source_items_spine_pending_idx;
        ALTER TABLE source_items DROP COLUMN chunked_hash, DROP COLUMN spined_hash;
        CREATE TABLE pipeline_state (
            stage       text PRIMARY KEY,
            watermark   timestamptz NOT NULL DEFAULT 'epoch',
            updated_at  timestamptz NOT NULL DEFAULT now());
    """)
