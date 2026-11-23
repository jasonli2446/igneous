"""Mail threads follow reply chains.

A mail record's thread follows the messages its reply headers name (graph/spine.py, "threads"): a reply is in the
thread of the message it answers, else of the one that began its conversation, and a message without reply headers
that another message names is keyed by its own Message-ID, so a conversation's first message is in one thread with
its replies. Before, that first message was keyed by its subject and participants while its replies were keyed by
its Message-ID: two threads, with the replies' REPLIES_TO edges crossing between them.

- `source_items_root_idx` indexes the first entry of mail's References (the message that began the conversation), so
  the spine finds the messages naming a Message-ID as their root; In-Reply-To has its index since 0012.
- Statistics on both headers. Their indexes are partial (only mail with reply headers), and a partial index gives the
  planner no statistics when no record has the header (Enron): it then takes a batch's Message-IDs to match most of
  the table and reads all of it, where the index answers at once.
- Every non-Gmail mail record that has reply headers, or that such a record names, is pending for the spine once more,
  and the next pipeline run builds it in the thread it is in now. Gmail threads keep Gmail's conversations, and mail
  without reply headers (Enron) its subject threads, so neither is built again.

Revision ID: 0015
Revises: 0014
"""
from alembic import op

revision = "0015"
down_revision = "0014"


def upgrade():
    op.execute("""
        CREATE INDEX source_items_root_idx ON source_items ((raw_meta->'references'->>0))
            WHERE source IN ('email', 'gmail') AND raw_meta->'references'->>0 IS NOT NULL;
        CREATE STATISTICS source_items_in_reply_to_stats ON (raw_meta->>'in_reply_to') FROM source_items;
        CREATE STATISTICS source_items_root_stats ON (raw_meta->'references'->>0) FROM source_items;
        ANALYZE source_items;
        UPDATE source_items si SET spined_hash = NULL
        WHERE si.spined_hash IS NOT NULL AND si.source IN ('email', 'gmail')
          AND si.raw_meta->>'gmail_thread_id' IS NULL
          AND (si.raw_meta->>'in_reply_to' IS NOT NULL OR si.raw_meta->'references'->>0 IS NOT NULL
               OR EXISTS (SELECT 1 FROM source_items r
                          WHERE r.source IN ('email', 'gmail') AND r.raw_meta->>'gmail_thread_id' IS NULL
                            AND (r.raw_meta->>'in_reply_to' = si.raw_meta->>'message_id'
                                 OR r.raw_meta->'references'->>0 = si.raw_meta->>'message_id')));
    """)


def downgrade():
    # The threads the records are built in stay; code of either revision builds a changed record its own way.
    op.execute("""
        DROP STATISTICS source_items_root_stats, source_items_in_reply_to_stats;
        DROP INDEX source_items_root_idx;
    """)
