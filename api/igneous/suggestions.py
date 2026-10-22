"""Starter questions for the Ask page, built from the user's own graph with plain templates (no model call)."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.traversal import relationships

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
    seed = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND props->>'self' = 'true' LIMIT 1")).scalar()
    if seed is None:
        return []
    ids = [p for p, _, _ in relationships(s, seed, PEOPLE)]
    names = {r.id: r.name for r in s.execute(text("SELECT id, name FROM nodes WHERE id = ANY(:ids)"), {"ids": ids})}
    people = [n for n in (names.get(i) for i in ids) if n and "@" not in n]
    projects = [dict(r) for r in s.execute(text("""
        SELECT name, props->>'kind' AS kind FROM nodes
        WHERE type = 'Project' AND props->>'derived' = 'threads' AND name <> ''
        ORDER BY props->>'end' DESC NULLS LAST, id DESC LIMIT :n"""), {"n": PROJECTS * 3}).mappings()]
    return build(people, projects)
