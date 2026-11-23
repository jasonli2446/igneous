"""Research runs: each briefing the research agent wrote (#92), with the trace of how it got there.

A run's trace grows step by step while it runs, so a client polling it sees the research live, and a reload or a
restart still shows what happened. A finished briefing answers the same question again while the graph stays at the
version it read (`version`) and the same sources, model and agent would write it (`key`); one run at a time is under
way for each.

Revision ID: 0015
Revises: 0014
"""
from alembic import op

revision = "0015"
down_revision = "0014"


def upgrade():
    op.execute("""
        CREATE TABLE research (
            id          text PRIMARY KEY,
            question    text NOT NULL,
            key         text NOT NULL,       -- the question, the sources the agent may read, the model and the agent
            version     bigint NOT NULL,     -- the graph version it read (graph_version)
            status      text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'succeeded', 'failed')),
            trace       jsonb NOT NULL DEFAULT '[]',
            report      jsonb,
            usage       jsonb,
            error       text,
            owner_host  text,
            owner_pid   integer,
            started_at  timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz);
        CREATE INDEX research_key_version ON research (key, version);
        CREATE UNIQUE INDEX research_one_running ON research (key, version) WHERE status = 'running';
    """)


def downgrade():
    op.execute("DROP TABLE research")
