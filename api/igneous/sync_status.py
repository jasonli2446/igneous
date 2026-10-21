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
        "gmail_backfill_in_progress": "backfill_page" in keys,
    }


def sync_status(
    s: Session,
    connector: str | None = None,
    account: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Return persisted sync checkpoints and connector-level item counts without changing state."""
    rows = (
        s.execute(
            text("""
        WITH states AS (
            SELECT connector, account, cursor, updated_at
            FROM sync_state
            WHERE (CAST(:connector AS text) IS NULL OR connector = :connector)
              AND (CAST(:account AS text) IS NULL OR account = :account)
        ), item_counts AS (
            SELECT source AS connector, count(*)::bigint AS item_count
            FROM source_items
            WHERE (CAST(:connector AS text) IS NULL OR source = :connector)
            GROUP BY source
        ), requested AS (
            SELECT CAST(:connector AS text) AS connector, CAST(:account AS text) AS account
            WHERE CAST(:connector AS text) IS NOT NULL AND CAST(:account AS text) IS NOT NULL
        ), subjects AS (
            SELECT connector, account FROM states
            UNION
            SELECT requested.connector, requested.account
            FROM requested
            WHERE NOT EXISTS (SELECT 1 FROM states)
        )
        SELECT subjects.connector, subjects.account, states.cursor, states.updated_at,
               COALESCE(item_counts.item_count, 0) AS item_count
        FROM subjects
        LEFT JOIN states
          ON states.connector = subjects.connector AND states.account = subjects.account
        LEFT JOIN item_counts ON item_counts.connector = subjects.connector
        ORDER BY subjects.connector, subjects.account
    """),
            {"connector": connector, "account": account},
        )
        .mappings()
        .all()
    )

    current_time = now or datetime.now(UTC)
    return [_status_from_row(row, current_time) for row in rows]


def _status_from_row(row: Mapping[str, Any], now: datetime) -> dict[str, object]:
    updated_at = row["updated_at"]
    if updated_at is None:
        return {
            "connector": row["connector"],
            "account": row["account"],
            "status": "never_synced",
            "last_synced_at": None,
            "cursor_age_seconds": None,
            "item_count": row["item_count"],
            "item_count_scope": "connector",
            "cursor": cursor_metadata(None),
        }

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
        "item_count_scope": "connector",
        "cursor": cursor_metadata(row["cursor"]),
    }
