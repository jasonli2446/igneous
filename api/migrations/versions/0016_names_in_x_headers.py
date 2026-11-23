"""People named by the display names in Enron's X-headers, quoted ones included.

Enron's mail keeps the display names its From, To, Cc and Bcc headers lack in X-From, X-To, X-cc and X-bcc. The spine
reads each entry's name with the standard address parser, so a quoted name names its person ("Mark Skilling" for
"Mark Skilling" <markskilling@hotmail.com>, not "Markskilling" from the address), and a name that reads as a person's
ranks as a header's display name does (graph/spine.py, `x_names`). What each record says a person is called is stored
with its support row (`spine_node_support`, migration 0012), so a database built before this revision keeps the names
its records gave then until they are built again: every mail record with an X-From is pending for the spine once
more, and the next pipeline run builds it with the names its X-headers give. Earlier syncs stored only X-From, so the
names X-To, X-cc and X-bcc give recipients come with mail synced after this revision.

Revision ID: 0016
Revises: 0015
"""
from alembic import op

revision = "0016"
down_revision = "0015"


def upgrade():
    op.execute("""
        UPDATE source_items SET spined_hash = NULL
        WHERE spined_hash IS NOT NULL AND source IN ('email', 'gmail') AND raw_meta->>'x_from' IS NOT NULL
    """)


def downgrade():
    pass  # the names the records gave stay valid; code of either revision builds a changed record its own way
