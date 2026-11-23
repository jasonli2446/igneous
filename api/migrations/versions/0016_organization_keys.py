"""Every key an organization is known by, and the organization it is now.

An organization's key follows the records (graph/organizations.py): a domain no company claims is keyed by the domain
(`acme.test`), and once a contact card, a LinkedIn connection, your resume or the model names its company, the domain
is that company's (`org:acme-robotics`). Your corrections and the verifier's verdicts are keyed by natural keys, so
the organizations stage records where each key it has known stands now: every email domain with the organization that
holds it, and every key an organization had that another organization has taken over. Readers resolve the keys they
store through it (graph/derived.py `rekeyed`), so a correction made under one key keeps applying under the next. The
rows are written by the next build: nothing to carry over.

Revision ID: 0016
Revises: 0015
"""
from alembic import op

revision = "0016"
down_revision = "0015"


def upgrade():
    op.execute("""
        CREATE TABLE organization_keys (
            key        text PRIMARY KEY,                   -- an email domain, or a key an organization had
            org_key    text NOT NULL,                      -- the key of the organization it stands for now
            updated_at timestamptz NOT NULL DEFAULT now());
    """)


def downgrade():
    # The code before this keys organizations its own way on its next build; corrections recorded under a company key
    # that build doesn't make no longer apply.
    op.execute("DROP TABLE organization_keys")
