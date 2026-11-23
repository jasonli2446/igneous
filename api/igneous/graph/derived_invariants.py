"""Read-only checks that corrections and the thread registry hold (`igneous check-graph`, graph/invariants.py).

  rejected_pair_linked    a derived link joins a pair you rejected, under any of their identities (graph/derived.py)
  thread_without_row      a thread Project no active registry row builds
  merged_row_dangling     a merged registry row whose merged_into chain doesn't end at an active row
  duplicate_aliases       a Project lists one alias twice
  dropped_edge_live       a link the verifier dropped is in the graph as well as in edges_dropped
  orphan_org_thread       an org thread the backbone made for an organization that no longer stands for it
  empty_umbrella          an umbrella no thread sits under
  domain_held_twice       an email domain two organizations hold (graph/organizations.py: one organization, one node)
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


def domain_violations(s: Session) -> list[Violation]:
    """An organization holds its domains (props.domains; one keyed by its domain holds that one): no domain is held by
    two."""
    return [Violation(code="domain_held_twice", details={"domain": r.domain, "organizations": r.keys},
                      message=f"two organizations hold the domain {r.domain}")
            for r in s.execute(text("""
                SELECT d AS domain, array_agg(DISTINCT n.key ORDER BY n.key) AS keys FROM nodes n
                CROSS JOIN LATERAL (SELECT jsonb_array_elements_text(COALESCE(n.props->'domains', '[]')) AS d
                                    UNION SELECT n.key WHERE n.key !~ ':' AND n.key ~ '\\.') ds
                WHERE n.type = 'Organization' GROUP BY d HAVING count(DISTINCT n.id) > 1 ORDER BY d"""))]


def check(s: Session) -> list[Violation]:
    """Every check above, in order."""
    return [*rejected_pair_violations(s), *registry_violations(s), *alias_violations(s), *dropped_violations(s),
            *orphan_project_violations(s), *domain_violations(s)]
