"""People's names in the order they are read.

The spine takes a name written surname first ("Rangel, Ina", the way Outlook and Exchange write people in mail
headers, calendars and address books) in the order it is read: "Ina Rangel" (graph/names.py). What each record says
a person is called is stored with its support row (`spine_node_support`, migration 0012), so a database built before
keeps its surname-first names until those records are built again. Every record that gave a person a name with a
comma is pending for the spine once more, and the next pipeline run builds it with the name it gives now; identity
resolution and everything after it then read the new names. A name the spine still leaves as it is ("Smith, Jr.") is
built again unchanged.

Revision ID: 0014
Revises: 0013
"""
from alembic import op

revision = "0014"
down_revision = "0013"


def upgrade():
    op.execute("""
        UPDATE source_items SET spined_hash = NULL
        WHERE spined_hash IS NOT NULL AND id IN (
            SELECT x.source_item_id FROM spine_node_support x JOIN nodes n ON n.id = x.node_id
            WHERE n.type = 'Person' AND x.name LIKE '%,%')
    """)


def downgrade():
    pass  # the names the records gave stay valid; code of either revision builds a changed record its own way
