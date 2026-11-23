"""Corrections as intents keyed by natural keys, and the thread registry's lifecycle.

- `nodes` gets an index on its natural key (`Type:key`), which corrections, verdicts and the registry join on.
- `edges_dropped` holds what the verifier dropped: one row per natural key (src_key, dst_key, type, extractor),
  deleted with the verdict that dropped it. The newest row of each key is kept. Rows whose nodes are gone, and rows
  that no model 'drop' verdict backs (a rejection of yours keeps its exact copies in its override, for undo), go.
- `thread_registry` gets `judged` (false: named without the model, and judged again once its records may be read)
  and `merged_by` ('user' when a merge you made folded it). Rows named without the model are marked unjudged, and
  rejected rows shaped like a failed model call (unjudged) are removed, so their candidates are judged again.
- `overrides` gets op 'link' (a connection you added) and `undone_at` (an undo that a rebuild's final step
  completes). Unlinks in the format older than pair rejections become pair rejections: a 'drop' verdict of yours
  on the pair, which their undo removes.
- Connections you added (edges with extractor 'user') become intents, so a rebuild that deletes and recreates a
  node never loses them: a Project under a Project goes to `thread_parents` (origin 'user'), anything else to an
  override 'link'.

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade():
    op.execute("""
        CREATE INDEX nodes_natural_key_idx ON nodes ((type || ':' || key));

        ALTER TABLE edges_dropped ADD COLUMN src_key text, ADD COLUMN dst_key text;
        UPDATE edges_dropped d SET src_key = a.type || ':' || a.key, dst_key = b.type || ':' || b.key
            FROM nodes a, nodes b WHERE a.id = d.src AND b.id = d.dst;
        DELETE FROM edges_dropped d WHERE d.src_key IS NULL OR d.dst_key IS NULL OR NOT EXISTS (
            SELECT 1 FROM edge_verdicts v
            WHERE v.src_key = d.src_key AND v.dst_key = d.dst_key AND v.type = d.type AND v.extractor = d.extractor
              AND v.verdict = 'drop' AND v.source = 'model');
        DELETE FROM edges_dropped d USING edges_dropped n
            WHERE (n.src_key, n.dst_key, n.type, n.extractor) = (d.src_key, d.dst_key, d.type, d.extractor)
              AND (n.id, n.ctid) > (d.id, d.ctid);
        ALTER TABLE edges_dropped ALTER COLUMN src_key SET NOT NULL, ALTER COLUMN dst_key SET NOT NULL;
        ALTER TABLE edges_dropped ADD CONSTRAINT edges_dropped_pkey PRIMARY KEY (src_key, dst_key, type, extractor);
        ALTER TABLE edges_dropped ADD CONSTRAINT edges_dropped_verdict_fkey FOREIGN KEY (src_key, dst_key, type, extractor)
            REFERENCES edge_verdicts (src_key, dst_key, type, extractor) ON DELETE CASCADE;

        ALTER TABLE thread_registry ADD COLUMN judged boolean NOT NULL DEFAULT true, ADD COLUMN merged_by text;
        UPDATE thread_registry SET judged = false
            WHERE origin IN ('burst', 'name') AND kind = 'thread' AND name LIKE '% · %';
        DELETE FROM thread_registry WHERE status = 'rejected' AND NOT judged;

        ALTER TABLE overrides DROP CONSTRAINT overrides_op_check;
        ALTER TABLE overrides ADD CONSTRAINT overrides_op_check
            CHECK (op IN ('rename', 'hide', 'merge', 'unlink', 'link'));
        ALTER TABLE overrides ADD COLUMN undone_at timestamptz;

        -- Older unlinks: (project, member) with snapshots by node id. Each becomes your 'drop' on the pair.
        CREATE TEMP TABLE old_unlink ON COMMIT DROP AS
            SELECT o.id, o.node_keys[2] AS a, o.node_keys[1] AS b,
                   CASE WHEN split_part(o.node_keys[2], ':', 1) = 'Person' THEN 'INVOLVED_IN' ELSE 'PART_OF' END AS type,
                   (SELECT to_jsonb(v) FROM edge_verdicts v WHERE v.src_key = o.node_keys[2]
                      AND v.dst_key = o.node_keys[1] AND v.extractor = 'entities'
                      AND v.type = CASE WHEN split_part(o.node_keys[2], ':', 1) = 'Person' THEN 'INVOLVED_IN'
                                        ELSE 'PART_OF' END) AS previous
            FROM overrides o
            WHERE o.op = 'unlink' AND cardinality(o.node_keys) = 2 AND o.undone_at IS NULL
              AND COALESCE((o.data->>'pair_correction')::bool, false) = false;
        INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence, reason, source)
            SELECT a, b, type, 'entities', 'override:' || id, 'drop', 1.0, 'you said this connection is wrong', 'user'
            FROM old_unlink
        ON CONFLICT (src_key, dst_key, type, extractor) DO UPDATE SET evidence = EXCLUDED.evidence,
            verdict = 'drop', confidence = 1.0, reason = EXCLUDED.reason, source = 'user', updated_at = now();
        UPDATE overrides o SET data = jsonb_build_object('pair_correction', true, 'edges', '[]'::jsonb,
                'verdicts', jsonb_build_array(jsonb_build_object('a', u.a, 'b', u.b, 'type', u.type,
                                                                 'extractor', 'entities', 'previous', u.previous)))
            FROM old_unlink u WHERE u.id = o.id;

        -- Connections you added become intents.
        INSERT INTO thread_parents (child_key, parent_key, origin)
            SELECT a.key, b.key, 'user' FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
            WHERE e.extractor = 'user' AND e.type = 'PART_OF' AND a.type = 'Project' AND b.type = 'Project'
        ON CONFLICT DO NOTHING;
        INSERT INTO overrides (op, node_keys, value, data)
            SELECT 'link', ARRAY[a.type || ':' || a.key, b.type || ':' || b.key], e.type,
                   jsonb_build_object('props', e.props)
            FROM edges e JOIN nodes a ON a.id = e.src JOIN nodes b ON b.id = e.dst
            WHERE e.extractor = 'user' AND NOT (e.type = 'PART_OF' AND a.type = 'Project' AND b.type = 'Project')
            ORDER BY e.id;
    """)


def downgrade():
    # Connections you added stay as edges (the code before this reads those). Undos still waiting for a rebuild
    # are dropped as they are; converted unlinks stay pair rejections, which that code also applies.
    op.execute("""
        DELETE FROM overrides WHERE op = 'link' OR undone_at IS NOT NULL;
        ALTER TABLE overrides DROP COLUMN undone_at;
        ALTER TABLE overrides DROP CONSTRAINT overrides_op_check;
        ALTER TABLE overrides ADD CONSTRAINT overrides_op_check CHECK (op IN ('rename', 'hide', 'merge', 'unlink'));

        ALTER TABLE thread_registry DROP COLUMN merged_by, DROP COLUMN judged;

        ALTER TABLE edges_dropped DROP CONSTRAINT edges_dropped_verdict_fkey;
        ALTER TABLE edges_dropped DROP CONSTRAINT edges_dropped_pkey;
        ALTER TABLE edges_dropped DROP COLUMN dst_key, DROP COLUMN src_key;

        DROP INDEX nodes_natural_key_idx;
    """)
