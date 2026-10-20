"""Read-only sync status queries for the API."""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def cursor_metadata(cursor: Mapping[str, Any] | None) -> dict[str, object]:
    """Describe a cursor without exposing its opaque values or tokens."""
    keys = sorted(str(key) for key in (cursor or {}))
    return {
        "present": bool(cursor),
        "keys": keys,
        "backfill_in_progress": "backfill_page" in keys,
    }


def sync_status(
    s: Session,
    connector: str | None = None,
    account: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Return persisted sync checkpoints and source-item counts without changing state."""
    rows = (
        s.execute(
            text("""
        WITH states AS (
            SELECT connector, account, cursor, updated_at
            FROM sync_state
            WHERE (:connector IS NULL OR connector = :connector)
              AND (:account IS NULL OR account = :account)
        ), item_counts AS (
            SELECT source AS connector,
                   COALESCE(raw_meta ->> 'account', 'default') AS account,
                   count(*)::bigint AS item_count
            FROM source_items
            GROUP BY source, COALESCE(raw_meta ->> 'account', 'default')
        )
        SELECT states.connector, states.account, states.cursor, states.updated_at,
               COALESCE(item_counts.item_count, 0) AS item_count
        FROM states
        LEFT JOIN item_counts
          ON item_counts.connector = states.connector
         AND item_counts.account = states.account
        ORDER BY states.connector, states.account
    """),
            {"connector": connector, "account": account},
        )
        .mappings()
        .all()
    )

    current_time = now or datetime.now(UTC)
    statuses = [_status_from_row(row, current_time) for row in rows]
    if statuses or connector is None or account is None:
        return statuses

    # A requested connector/account pair with no checkpoint is explicitly never synced.
    return [
        {
            "connector": connector,
            "account": account,
            "status": "never_synced",
            "last_synced_at": None,
            "cursor_age_seconds": None,
            "item_count": 0,
            "cursor": cursor_metadata(None),
        }
    ]


def _status_from_row(row: Mapping[str, Any], now: datetime) -> dict[str, object]:
    updated_at = row["updated_at"]
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    age = max(0, int((now - updated_at).total_seconds()))
    return {
        "connector": row["connector"],
        "account": row["account"],
        "status": "known",
        "last_synced_at": updated_at,
        "cursor_age_seconds": age,
        "item_count": row["item_count"],
        "cursor": cursor_metadata(row["cursor"]),
    }
