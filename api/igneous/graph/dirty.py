"""Persistent dirty markers for derived graph work.

A marker is intentionally keyed by node rather than a source: a source item can
change several nodes, and later stages can consume exactly the affected graph
clusters.  Marking is idempotent and safe to call from every stage.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session


def ensure_table(s: Session) -> None:
    s.execute(text("""
        CREATE TABLE IF NOT EXISTS dirty_nodes (
            node_id bigint PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
            reason text NOT NULL,
            marked_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS dirty_rebuild (id bool PRIMARY KEY DEFAULT true CHECK (id));
        CREATE TABLE IF NOT EXISTS graph_version (id bool PRIMARY KEY DEFAULT true CHECK (id), version bigint NOT NULL DEFAULT 0);
        INSERT INTO graph_version (id) VALUES (true) ON CONFLICT DO NOTHING
    """))


def mark(s: Session, node_ids: set[int], reason: str) -> None:
    if not node_ids:
        return
    ensure_table(s)
    s.execute(text("""
        INSERT INTO dirty_nodes (node_id, reason) SELECT unnest(CAST(:ids AS bigint[])), :reason
        ON CONFLICT (node_id) DO UPDATE SET reason = EXCLUDED.reason, marked_at = now()
    """), {"ids": sorted(node_ids), "reason": reason[:200]})


def mark_rebuild(s: Session) -> None:
    ensure_table(s)
    s.execute(text("INSERT INTO dirty_rebuild (id) VALUES (true) ON CONFLICT DO NOTHING"))


def pending(s: Session) -> set[int]:
    ensure_table(s)
    ids = set(s.execute(text("SELECT node_id FROM dirty_nodes")).scalars())
    return ids | ({-1} if s.execute(text("SELECT 1 FROM dirty_rebuild LIMIT 1")).first() else set())


def touch(s: Session) -> None:
    ensure_table(s)
    s.execute(text("UPDATE graph_version SET version = version + 1 WHERE id = true"))


def version(s: Session) -> int:
    ensure_table(s)
    return s.execute(text("SELECT version FROM graph_version WHERE id = true")).scalar_one()


def clear(s: Session, node_ids: set[int] | None = None) -> None:
    ensure_table(s)
    if node_ids is None:
        s.execute(text("DELETE FROM dirty_nodes; DELETE FROM dirty_rebuild"))
    elif node_ids:
        s.execute(text("DELETE FROM dirty_nodes WHERE node_id = ANY(:ids)"), {"ids": sorted(node_ids)})
