"""Every fact you or an agent add is one override 'link' row.

- The projects you put under other projects (`thread_parents`) become override 'link' rows (PART_OF from one Project
  to the other, stated by you), like every other connection you added, so each is undone by its override id; and
  `thread_parents` goes. A connection you took out and can still bring back (an override 'unlink' with
  data.user_link) holds the projects it took out as such rows too.
- `node_held`: a fact an agent added (extractor 'agent:<name>') holds a node as one of yours does: a person or
  organization it names stays while the fact does; a node that is its records (a message, thread, event, file, folder
  or place) still goes with them.

Revision ID: 0015
Revises: 0014
"""
from alembic import op

revision = "0015"
down_revision = "0014"

RECORDS = "'Message', 'Thread', 'Event', 'File', 'Folder', 'Place'"  # node types that are their records
PARENT = """jsonb_build_object('props', jsonb_build_object('why', 'you said so'), 'by', 'user')"""


def _node_held(agents: bool) -> str:
    """node_held (migrations 0012 and 0013): the edge of a fact holds a node that isn't its records; with `agents`,
    an agent's fact as well as yours (as 0013 made it without)."""
    by = "(u.extractor = 'user' OR u.extractor LIKE 'agent:%')" if agents else "u.extractor = 'user'"
    return f"""
        CREATE OR REPLACE FUNCTION node_held(node bigint, leaving bigint) RETURNS boolean LANGUAGE sql STABLE AS $$
            SELECT (n.type = 'Person' AND n.key = 'me')
                OR EXISTS (SELECT 1 FROM spine_node_support x WHERE x.node_id = n.id
                           AND x.source_item_id IS DISTINCT FROM leaving)
                OR (n.type <> ALL (ARRAY[{RECORDS}]) AND (
                    EXISTS (SELECT 1 FROM edges u WHERE u.src = n.id AND {by})
                    OR EXISTS (SELECT 1 FROM edges u WHERE u.dst = n.id AND {by})
                    OR EXISTS (SELECT 1 FROM edges e WHERE e.src = n.id
                               AND edge_backed(e.id, e.source_item_id, leaving))
                    OR EXISTS (SELECT 1 FROM edges e WHERE e.dst = n.id
                               AND edge_backed(e.id, e.source_item_id, leaving))))
            FROM nodes n WHERE n.id = node
        $$;
    """


def upgrade():
    op.execute(f"""
        INSERT INTO overrides (op, node_keys, value, data)
            SELECT 'link', ARRAY['Project:' || child_key, 'Project:' || parent_key], 'PART_OF', {PARENT}
            FROM thread_parents ORDER BY child_key, parent_key;
        UPDATE overrides o SET data = (o.data - 'parents') || jsonb_build_object('links',
                COALESCE(o.data->'links', '[]'::jsonb) || COALESCE((
                    SELECT jsonb_agg(jsonb_build_object('op', 'link', 'value', 'PART_OF', 'data', {PARENT},
                                                        'node_keys', jsonb_build_array('Project:' || (p->>0),
                                                                                       'Project:' || (p->>1)))
                                     ORDER BY p->>0, p->>1)
                    FROM jsonb_array_elements(o.data->'parents') p), '[]'::jsonb))
            WHERE o.op = 'unlink' AND o.data ? 'parents';
        DROP TABLE thread_parents;
    """)
    op.execute(_node_held(agents=True))


def downgrade():
    # The code before this has no agents' facts: they go, and so do the new things no fact of yours still names. The
    # projects you put under projects go back to thread_parents.
    op.execute(_node_held(agents=False))
    op.execute("""
        CREATE TABLE thread_parents (
            child_key text NOT NULL, parent_key text NOT NULL, origin text NOT NULL DEFAULT 'gold',
            PRIMARY KEY (child_key, parent_key));
        INSERT INTO thread_parents (child_key, parent_key, origin)
            SELECT substr(node_keys[1], 9), substr(node_keys[2], 9), 'user' FROM overrides
            WHERE op = 'link' AND value = 'PART_OF' AND undone_at IS NULL AND COALESCE(data->>'by', 'user') = 'user'
              AND node_keys[1] LIKE 'Project:%' AND node_keys[2] LIKE 'Project:%'
        ON CONFLICT DO NOTHING;
        DELETE FROM overrides WHERE op = 'link' AND value = 'PART_OF' AND COALESCE(data->>'by', 'user') = 'user'
            AND node_keys[1] LIKE 'Project:%' AND node_keys[2] LIKE 'Project:%';
        DELETE FROM overrides WHERE op = 'link' AND data->>'by' LIKE 'agent:%';
        DELETE FROM edges WHERE extractor LIKE 'agent:%';
        DELETE FROM nodes n WHERE n.key LIKE 'added:%' AND NOT EXISTS (
            SELECT 1 FROM overrides o WHERE o.op = 'link' AND n.type || ':' || n.key = ANY(o.node_keys));
    """)
