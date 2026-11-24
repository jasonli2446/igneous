"""Read-only checks that corrections and the thread registry hold (`igneous check-graph`, graph/invariants.py).

  rejected_pair_linked    a derived link joins a pair you rejected, under any of their identities (graph/derived.py)
  thread_without_row      a thread Project no active registry row builds
  merged_row_dangling     a merged registry row whose merged_into chain doesn't end at an active row
  duplicate_aliases       a Project lists one alias twice
  dropped_edge_live       a link the verifier dropped is in the graph as well as in edges_dropped
  orphan_org_thread       an org thread the backbone made for an organization that no longer stands for it
  empty_umbrella          an umbrella no thread sits under
  joined_project_stands   the Project of a folder, series or chat that joined a thread still stands beside it
  joined_row_unmerged     the registry row the entities stage keeps for an anchor that joined a thread is not merged
                          into a thread (no term of its own builds anything)
  joined_file_outside     a file the entities stage put in a thread it joined, from outside the thread's window
  evidence_is_member      a record linked to a Project as its evidence (a notice, a search, a place) and as a derived
                          member at once
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.derived import STATED, blocked_sql, follow, rejections
from igneous.graph.invariants import Violation


def rejected_pair_violations(s: Session) -> list[Violation]:
    blocked = rejections(s).id_pairs()
    if not blocked:
        return []
    return [Violation(code="rejected_pair_linked", edge_id=r.id, details={"src": r.src, "dst": r.dst,
                      "type": r.type, "extractor": r.extractor},
                      message=f"{r.type} ({r.extractor}) joins a pair you said is wrong")
            for r in s.execute(text(f"""
                SELECT e.id, e.src, e.dst, e.type, e.extractor FROM edges e
                WHERE e.extractor <> ALL(:stated) AND {blocked_sql('e.src', 'e.dst')} ORDER BY e.id
            """), {"stated": list(STATED), "blocked": blocked})]


def registry_violations(s: Session) -> list[Violation]:
    rows = {r.key: r for r in s.execute(text("SELECT key, status, merged_into FROM thread_registry"))}
    out = [Violation(code="thread_without_row", node_id=r.id, details={"key": r.key},
                     message="a thread Project that no active registry row builds")
           for r in s.execute(text("""
               SELECT n.id, n.key FROM nodes n WHERE n.type = 'Project' AND n.props->>'derived' = 'threads'
                 AND NOT COALESCE((n.props->>'umbrella')::bool, false)
                 AND NOT EXISTS (SELECT 1 FROM thread_registry r WHERE r.key = n.key AND r.status = 'active')
               ORDER BY n.id"""))]
    into = {k: r.merged_into for k, r in rows.items() if r.status == "merged" and r.merged_into}
    for key, r in sorted(rows.items()):
        if r.status == "merged":
            end = rows.get(follow(into, key))
            if end is None or end.status != "active":
                out.append(Violation(code="merged_row_dangling", details={"key": key, "merged_into": r.merged_into},
                                     message="a merged registry row whose thread is not active"))
    return out


def alias_violations(s: Session) -> list[Violation]:
    return [Violation(code="duplicate_aliases", node_id=r.id, message="a Project lists an alias twice")
            for r in s.execute(text("""
                SELECT n.id FROM nodes n WHERE n.type = 'Project' AND jsonb_typeof(n.props->'aliases') = 'array'
                  AND jsonb_array_length(n.props->'aliases') >
                      (SELECT count(DISTINCT a) FROM jsonb_array_elements_text(n.props->'aliases') a)
                ORDER BY n.id"""))]


def dropped_violations(s: Session) -> list[Violation]:
    return [Violation(code="dropped_edge_live", edge_id=r.id, details={"src_key": r.src_key, "dst_key": r.dst_key,
                      "type": r.type, "extractor": r.extractor},
                      message="a link the verifier dropped is also live")
            for r in s.execute(text("""
                SELECT e.id, d.src_key, d.dst_key, d.type, d.extractor FROM edges_dropped d
                JOIN nodes a ON a.type || ':' || a.key = d.src_key JOIN nodes b ON b.type || ':' || b.key = d.dst_key
                JOIN edges e ON e.src = a.id AND e.dst = b.id AND e.type = d.type AND e.extractor = d.extractor
                ORDER BY e.id"""))]


def orphan_project_violations(s: Session) -> list[Violation]:
    orphans = [Violation(code="orphan_org_thread", node_id=r.id, details={"key": r.key},
                         message="an org thread no organization stands for")
               for r in s.execute(text("""
                   SELECT p.id, p.key FROM nodes p WHERE p.type = 'Project' AND p.props->>'derived' = 'backbone'
                     AND NOT EXISTS (SELECT 1 FROM nodes o WHERE o.type = 'Organization'
                                     AND (o.props->>'thread')::bigint = p.id)
                   ORDER BY p.id"""))]
    return orphans + [Violation(code="empty_umbrella", node_id=r.id, details={"key": r.key},
                                message="an umbrella no thread sits under")
                      for r in s.execute(text("""
                          SELECT p.id, p.key FROM nodes p WHERE p.type = 'Project'
                            AND COALESCE((p.props->>'umbrella')::bool, false)
                            AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst = p.id AND e.type = 'PART_OF')
                          ORDER BY p.id"""))]


def one_thing_violations(s: Session) -> list[Violation]:
    """One real thing, one Project (graph/entities.py): an anchor's Project that joined a thread (its key a registry
    row of origin 'entities', always merged) never stands beside it, a joined folder brings only its files from while
    the thread happened (graph/threads.py `window`), and a Project's evidence is never one of its derived members (a
    link you or your data state is no derived member)."""
    from igneous.graph.threads import window

    joined = [Violation(code="joined_project_stands", node_id=r.id, details={"key": r.key, "thread": r.merged_into},
                        message="the Project of an anchor that joined a thread still stands beside it")
              for r in s.execute(text("""
                  SELECT n.id, n.key, r.merged_into FROM nodes n
                  JOIN thread_registry r ON r.key = n.key AND r.origin = 'entities'
                  WHERE n.type = 'Project' ORDER BY n.id"""))]
    unmerged = [Violation(code="joined_row_unmerged", details={"key": r.key, "status": r.status},
                          message="the registry row of an anchor that joined a thread is not merged into one")
                for r in s.execute(text("""SELECT key, status FROM thread_registry
                                           WHERE origin = 'entities' AND status <> 'merged' ORDER BY key"""))]
    rows = {r.key: r for r in s.execute(text("SELECT * FROM thread_registry WHERE status = 'active'"))}
    outside = []
    for r in s.execute(text("""
            SELECT e.id, p.key, si.occurred_at FROM edges e JOIN nodes p ON p.id = e.dst AND p.type = 'Project'
            JOIN nodes f ON f.id = e.src JOIN source_items si ON si.id = f.source_item_id
            WHERE e.type = 'PART_OF' AND e.extractor = 'entities' AND e.props ? 'folder'
              AND p.props->>'derived' = 'threads' ORDER BY e.id""")):
        win = window(rows[r.key]) if r.key in rows else None
        if win and not (r.occurred_at and win[0] <= r.occurred_at <= win[1]):
            outside.append(Violation(code="joined_file_outside", edge_id=r.id, details={"thread": r.key},
                                     message="a file of a folder that joined a thread is from outside the thread's time"))
    return joined + unmerged + outside + [
        Violation(code="evidence_is_member", edge_id=r.id, details={"src": r.src, "dst": r.dst},
                  message="a record is a Project's evidence and one of its members at once")
        for r in s.execute(text("""
            SELECT e.id, e.src, e.dst FROM edges e
            JOIN edges m ON m.src = e.src AND m.dst = e.dst AND m.type = 'PART_OF' AND m.extractor <> ALL(:stated)
            WHERE e.type = 'RELATED_TO' AND e.props->>'role' = 'evidence' ORDER BY e.id"""), {"stated": list(STATED)})]


def check(s: Session) -> list[Violation]:
    """Every check above, in order."""
    return [*rejected_pair_violations(s), *registry_violations(s), *alias_violations(s), *dropped_violations(s),
            *orphan_project_violations(s), *one_thing_violations(s)]
