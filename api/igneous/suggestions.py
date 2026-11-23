"""Starter questions for the Ask page, built from the user's own graph with plain templates (no model call)."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.identity import self_id
from igneous.graph.traversal import label, names, relationships
from igneous.graph.visibility import visible

MAX_SUGGESTIONS = 5
PEOPLE = 2
PROJECTS = 3


def build(people: list[str], projects: list[dict]) -> list[str]:
    """Fill question templates from names only. `people` is strongest first, `projects` most recent first
    (dicts with `name` and `kind`). A template whose slot is missing is skipped; an empty graph gives []."""
    trips = [p["name"] for p in projects if p.get("kind") == "trip"]
    others = [p["name"] for p in projects if p.get("kind") != "trip"]
    out: list[str] = []
    if others:
        out.append(f"Who's working on {others[0]} with me?")
    if trips:
        out.append(f"When is the {trips[0]}?")
    if people:
        out.append(f"What have {people[0]} and I talked about lately?")
    if len(others) > 1:
        out.append(f"What did we decide about {others[1]}?")
    elif others:
        out.append(f"What did we decide about {others[0]}?")
    if len(people) > 1:
        out.append(f"What have {people[1]} and I talked about lately?")
    return list(dict.fromkeys(out))[:MAX_SUGGESTIONS]


def suggestions(s: Session) -> list[str]:
    """Questions about your closest people and your most recent life threads; [] with no owner."""
    seed = self_id(s)
    if seed is None:
        return []
    named = names(s)
    ids = [p for p, _, _ in relationships(s, seed, PEOPLE)]
    by_id = dict(s.execute(text("SELECT id, name FROM nodes WHERE id = ANY(:ids)"), {"ids": ids}).all())
    people = [n for n in (label(by_id.get(i), "Person", named) for i in ids) if n and "@" not in n]
    projects = [dict(r) for r in s.execute(text(f"""
        SELECT n.name, n.props->>'kind' AS kind FROM nodes n
        WHERE n.type = 'Project' AND n.props->>'derived' = 'threads' AND n.name <> '' AND {visible('n')}
        ORDER BY n.props->>'end' DESC NULLS LAST, n.id DESC LIMIT :n"""), {"n": PROJECTS * 3}).mappings()]
    return build(people, projects)
