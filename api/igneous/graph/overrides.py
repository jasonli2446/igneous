"""Corrections to derived entities: rename, hide, merge, unlink. Stored, never baked into the graph.

`graph/entities.py` rebuilds its Projects and edges from scratch on every run, so a correction can't live on
the rows it corrects. It lives here, keyed by natural keys (`Type:key`, e.g. `Project:project:gloomhaven night`),
and `apply` replays every override as the last step of each rebuild. `apply` is a pure function of the
`overrides` table: delete a row and apply again, and the correction is gone (that is how undo works).

  rename  node_keys=[project]          value=new name
  hide    node_keys=[project]          the project stays in the database, flagged props.hidden; views skip it
  merge   node_keys=[keep, fold]       fold's edges move to keep; fold is deleted and so never reappears alone
  unlink  node_keys=[project, member]  that one PART_OF / INVOLVED_IN edge is dropped on every rebuild
"""
import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

OPS = ("rename", "hide", "merge", "unlink")
EXTRACTOR = "entities"  # edges owned by graph/entities.py


@dataclass
class Plan:
    renames: dict[str, str] = field(default_factory=dict)         # project key -> name
    hidden: set[str] = field(default_factory=set)
    merges: dict[str, str] = field(default_factory=dict)          # folded key -> surviving key
    unlinks: set[tuple[str, str]] = field(default_factory=set)    # (project key, member key)


def plan(rows: list[dict]) -> Plan:
    """Resolve override rows into what to do. Pure, so it is testable without a database.
    Later renames win; merge chains collapse (C into B into A -> C into A); an override aimed at a project
    that was folded away follows it to the survivor."""
    p = Plan()

    def survivor(k: str) -> str:
        while k in p.merges:
            k = p.merges[k]
        return k

    for r in sorted(rows, key=lambda r: r["id"]):
        keys = r["node_keys"]
        if r["op"] == "rename" and r.get("value"):
            p.renames[keys[0]] = r["value"]
        elif r["op"] == "hide":
            p.hidden.add(keys[0])
        elif r["op"] == "merge" and len(keys) == 2:
            keep, fold = survivor(keys[0]), survivor(keys[1])
            if keep != fold:  # a merge that would close a cycle (or merge a project into itself) is a no-op
                p.merges[fold] = keep
                p.merges = {f: (keep if k == fold else k) for f, k in p.merges.items()}  # keep chains one deep
        elif r["op"] == "unlink" and len(keys) == 2:
            p.unlinks.add((keys[0], keys[1]))

    p.renames = {survivor(k): v for k, v in p.renames.items()}
    p.hidden = {survivor(k) for k in p.hidden}
    p.unlinks = {(survivor(k), m) for k, m in p.unlinks}
    return p


def nkey(type_: str, key: str) -> str:
    return f"{type_}:{key}"


def _ids(s: Session, keys: set[str]) -> dict[str, int]:
    rows = s.execute(text("SELECT type || ':' || key, id FROM nodes WHERE type || ':' || key = ANY(:k)"),
                     {"k": sorted(keys)}).all()
    return {k: i for k, i in rows}


def load(s: Session) -> list[dict]:
    return [dict(r) for r in s.execute(text(
        "SELECT id, op, node_keys, value, data, created_at FROM overrides ORDER BY id")).mappings()]


def add(s: Session, op: str, node_keys: list[str], value: str | None = None, data: dict | None = None) -> int:
    return s.execute(text("""
        INSERT INTO overrides (op, node_keys, value, data) VALUES (:op, :k, :v, CAST(:d AS jsonb)) RETURNING id
    """), {"op": op, "k": node_keys, "v": value, "d": json.dumps(data) if data is not None else None}).scalar_one()


def remove(s: Session, override_id: int) -> dict | None:
    """Delete one override and return it (None if it doesn't exist). The caller then re-applies, or, for a
    merge, rebuilds, since the folded project's rows are gone."""
    row = s.execute(text("DELETE FROM overrides WHERE id = :i RETURNING id, op, node_keys, value, data"),
                    {"i": override_id}).mappings().first()
    return dict(row) if row else None


def restore_unlink(s: Session, data: dict | None) -> None:
    """Undo an unlink: put the dropped edges back (if both ends still exist)."""
    for e in (data or {}).get("edges", []):
        s.execute(text("""
            INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence, source_item_id,
                               span_start, span_end)
            SELECT :src, :dst, :type, CAST(:props AS jsonb), :x, :v, :conf, :sid, :a, :b
            WHERE EXISTS (SELECT 1 FROM nodes WHERE id = :src) AND EXISTS (SELECT 1 FROM nodes WHERE id = :dst)
            ON CONFLICT (src, dst, type, extractor) DO NOTHING
        """), {"src": e["src"], "dst": e["dst"], "type": e["type"], "props": json.dumps(e.get("props") or {}),
               "x": EXTRACTOR, "v": e.get("extractor_version") or "1", "conf": e.get("confidence"),
               "sid": e.get("source_item_id"), "a": e.get("span_start"), "b": e.get("span_end")})


def apply(s: Session) -> int:
    """Replay every override. Idempotent; runs last in every entities rebuild and after each correction."""
    p = plan(load(s))

    # Back to the derived state first, so a removed rename or hide really disappears.
    s.execute(text("""
        UPDATE nodes SET name = COALESCE(props->>'original_name', name), props = props - 'hidden' - 'renamed'
        WHERE type = 'Project' AND props->>'derived' = 'true'
    """))

    ids = _ids(s, {*p.merges, *p.merges.values(), *p.renames, *p.hidden,
                   *(k for k, _ in p.unlinks), *(m for _, m in p.unlinks)})

    for fold, keep in p.merges.items():
        if fold not in ids or keep not in ids:
            continue
        a, b = ids[keep], ids[fold]
        s.execute(text("""
            INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence, source_item_id,
                               span_start, span_end)
            SELECT src, :a, type, props, extractor, extractor_version, confidence, source_item_id, span_start, span_end
            FROM edges WHERE dst = :b AND extractor = :x AND src <> :a
            ON CONFLICT (src, dst, type, extractor) DO NOTHING
        """), {"a": a, "b": b, "x": EXTRACTOR})
        s.execute(text("""
            UPDATE nodes SET props = props || jsonb_build_object('merged_names',
                (SELECT COALESCE(jsonb_agg(DISTINCT v), '[]'::jsonb) FROM jsonb_array_elements_text(
                    COALESCE(props->'merged_names', '[]'::jsonb) || to_jsonb((SELECT name FROM nodes WHERE id = :b))) v))
            WHERE id = :a
        """), {"a": a, "b": b})
        s.execute(text("DELETE FROM nodes WHERE id = :b"), {"b": b})  # cascades its edges

    for k, name in p.renames.items():
        if k in ids:
            s.execute(text("""
                UPDATE nodes SET name = :n, props = props || jsonb_build_object('renamed', true) WHERE id = :i
            """), {"n": name, "i": ids[k]})
    for k in p.hidden:
        if k in ids:
            s.execute(text("""UPDATE nodes SET props = props || '{"hidden": true}' WHERE id = :i"""), {"i": ids[k]})
    for k, m in p.unlinks:
        if k in ids and m in ids:
            s.execute(text("""
                DELETE FROM edges WHERE src = :m AND dst = :p AND extractor = :x AND type IN ('PART_OF', 'INVOLVED_IN')
            """), {"m": ids[m], "p": ids[k], "x": EXTRACTOR})
    from igneous.graph import dirty
    dirty.touch(s)
    return len(p.merges) + len(p.renames) + len(p.hidden) + len(p.unlinks)
