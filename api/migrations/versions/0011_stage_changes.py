"""Per-stage change tracking, and organization keys shared by every writer.

`pipeline_changes` records that source records changed, once per writing transaction and per stage that skips work
when its inputs are unchanged (pipeline.GATED). Each such stage removes exactly the rows it covered, so a change
that arrives while it runs waits for the next run, and no stage consumes another's changes. The table replaces
dirty_nodes and dirty_rebuild (one flag that every stage shared); changes pending there carry over.

A company name is keyed `org:<slug>` by every writer (contact cards, LinkedIn, your resume, model mentions), so one
company is one node: `company:<slug>` and `name:<slug>` organizations take that key, or, where an `org:` node already
has it, move their links to that node and go. An address domain is not evidence of membership and a personal-mail
domain is not an organization, so MEMBER_OF rows the spine derived from domains and unlinked personal-domain
organizations are deleted.

Revision ID: 0011
Revises: 0008
"""
from alembic import op

revision = "0011"
down_revision = "0008"


def upgrade():
    op.execute("""
        CREATE TABLE pipeline_changes (
            id        bigserial PRIMARY KEY,
            stage     text NOT NULL,          -- the stage that has not covered this change yet
            xact      xid8 NOT NULL,          -- the transaction that recorded it: one row per stage and transaction
            reason    text NOT NULL,
            marked_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (stage, xact));
        INSERT INTO pipeline_changes (stage, xact, reason)
            SELECT 'resolve', pg_current_xact_id(), 'source records changed'
            WHERE EXISTS (SELECT 1 FROM dirty_nodes) OR EXISTS (SELECT 1 FROM dirty_rebuild);
        DROP TABLE dirty_nodes, dirty_rebuild;

        CREATE TEMP TABLE org_rekey ON COMMIT DROP AS
            SELECT n.id AS old, 'org:' || regexp_replace(n.key, '^(company|name):', '') AS key, NULL::bigint AS survivor
            FROM nodes n WHERE n.type = 'Organization' AND n.key ~ '^(company|name):';
        UPDATE org_rekey r SET survivor = COALESCE(
            (SELECT x.id FROM nodes x WHERE x.type = 'Organization' AND x.key = r.key),
            (SELECT min(o.old) FROM org_rekey o WHERE o.key = r.key));
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT old, survivor FROM org_rekey WHERE old <> survivor ORDER BY old LOOP
                UPDATE edges e SET dst = r.survivor WHERE e.dst = r.old AND e.src <> r.survivor
                  AND NOT EXISTS (SELECT 1 FROM edges x WHERE x.src = e.src AND x.dst = r.survivor
                                    AND x.type = e.type AND x.extractor = e.extractor);
                UPDATE edges e SET src = r.survivor WHERE e.src = r.old AND e.dst <> r.survivor
                  AND NOT EXISTS (SELECT 1 FROM edges x WHERE x.src = r.survivor AND x.dst = e.dst
                                    AND x.type = e.type AND x.extractor = e.extractor);
                DELETE FROM nodes WHERE id = r.old;
            END LOOP;
        END $$;
        UPDATE nodes n SET key = r.key FROM org_rekey r WHERE n.id = r.old AND r.old = r.survivor;

        DELETE FROM edges WHERE type = 'MEMBER_OF' AND extractor = 'spine';
        DELETE FROM nodes n WHERE n.type = 'Organization'
          AND n.key IN ('aol.com', 'gmail.com', 'hotmail.com', 'icloud.com', 'live.com', 'me.com', 'msn.com',
                        'outlook.com', 'proton.me', 'protonmail.com', 'yahoo.com')
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = n.id OR e.dst = n.id);
    """)


def downgrade():
    # Re-keyed organizations and deleted rows stay as they are: both are valid at 0008 too.
    op.execute("""
        CREATE TABLE dirty_nodes (
            node_id bigint PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
            reason text NOT NULL,
            marked_at timestamptz NOT NULL DEFAULT now());
        CREATE TABLE dirty_rebuild (id bool PRIMARY KEY DEFAULT true CHECK (id));
        INSERT INTO dirty_rebuild SELECT true WHERE EXISTS (SELECT 1 FROM pipeline_changes);
        DROP TABLE pipeline_changes;
    """)
