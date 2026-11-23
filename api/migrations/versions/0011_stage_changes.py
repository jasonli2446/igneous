"""Per-stage change tracking, and organization keys shared with LinkedIn and your resume.

`pipeline_changes` records that source records changed, once per writing transaction and per stage that skips work
when its inputs are unchanged (pipeline.GATED). Each such stage removes exactly the rows it covered, so a change
that arrives while it runs waits for the next run, and no stage consumes another's changes. The table replaces
dirty_nodes and dirty_rebuild (one flag that every stage shared); changes pending there carry over.

Contact-card companies are keyed `org:<slug>`, like LinkedIn companies and resume organizations, so one company is
one node: a `company:<slug>` node takes that key unless an `org:` node already has it. An address domain is not
evidence of membership and a personal-mail domain is not an organization, so MEMBER_OF rows the spine derived from
domains and unlinked personal-domain organizations are deleted.

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

        UPDATE nodes c SET key = 'org:' || substr(c.key, 9)
        WHERE c.type = 'Organization' AND c.key LIKE 'company:%'
          AND NOT EXISTS (SELECT 1 FROM nodes o WHERE o.type = 'Organization' AND o.key = 'org:' || substr(c.key, 9));

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
