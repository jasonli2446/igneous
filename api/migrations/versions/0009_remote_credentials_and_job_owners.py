"""Git remotes without credentials; jobs that record their owner.

A remote such as https://user:token@github.com/me/repo.git was stored with its user name and token in the Local
files repo item (`source_items.raw_meta.remote`) and copied to the repository's node (`nodes.props.remote`). The
user info is removed from both, as the connector removes it before storing a remote. The scrub can't be undone: a
downgrade leaves the remotes as they are.

A job (kind sync, import or pipeline; source a Sources page id or 'pipeline') records the host and process that run
it, so a process starting up fails only the running jobs whose process died, never one another process still runs.

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"

# scheme://anything-without-a-slash@ at the start of the URL; scp-style remotes (git@host:path) have no scheme.
USERINFO = r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]*@"
SCRUB = rf"""
    UPDATE source_items
    SET raw_meta = jsonb_set(raw_meta, '{{remote}}', to_jsonb(regexp_replace(raw_meta->>'remote', '{USERINFO}', '\1')))
    WHERE raw_meta->>'remote' ~ '{USERINFO}';
    UPDATE nodes
    SET props = jsonb_set(props, '{{remote}}', to_jsonb(regexp_replace(props->>'remote', '{USERINFO}', '\1')))
    WHERE props->>'remote' ~ '{USERINFO}';
"""


def upgrade():
    op.execute(SCRUB)
    op.execute("ALTER TABLE jobs ADD COLUMN owner_host text, ADD COLUMN owner_pid integer")


def downgrade():
    op.execute("ALTER TABLE jobs DROP COLUMN owner_pid, DROP COLUMN owner_host")
