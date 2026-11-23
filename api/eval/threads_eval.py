"""Score derived threads/projects against a gold set: found, duplicated, hierarchy, must-include/exclude, who took part.

    IGNEOUS_DATABASE_URL=...igneous_personal uv run python eval/threads_eval.py [data/eval/threads_gold.json]
    IGNEOUS_DATABASE_URL=...igneous_persona uv run python eval/threads_eval.py igneous/persona/gold/threads.json

A personal gold file names a person's projects and people, so it stays local, in git-ignored data/; the synthetic
persona's (igneous/persona/gold) is in the repo. A file holds {"threads": [...]}, each entry {name, terms: [words its
name may use], parent: the name of the gold thread it sits under, must_include: [records], must_exclude: [...],
participants: [names], not_participants: [names]}; every field but name and terms is optional, and a parent of null
says it sits under nothing (a trip with school friends is not part of their school), neither as found nor as any
duplicate of it. A record is a stable key (gold.py: {source, external_id} or {source, path}), which holds on any
build, or a source_items id of the database the file was written on. A gold thread is "found" by the derived Project
named exactly like it, or else by a Project whose name contains one of its terms (three letters or more), the one
covering most of must_include. Its records are its own and those of every Project under it (a school holds its
classes'). Reported per thread and in total, so a change that fixes one case and breaks another shows.
"""
import json
import re
import sys
from pathlib import Path

import gold as keys
from sqlalchemy import text

from igneous.config import settings
from igneous.db import SessionLocal


def _projects(s) -> dict[int, dict]:
    rows = s.execute(text("""
        SELECT p.id, p.name, p.props->>'kind' AS kind, COALESCE((p.props->>'umbrella')::bool, false) AS umbrella,
               array_remove(array_agg(DISTINCT n.source_item_id), NULL) AS items
        FROM nodes p LEFT JOIN edges e ON e.dst = p.id AND e.type = 'PART_OF'
        LEFT JOIN nodes n ON n.id = e.src
        WHERE p.type = 'Project' AND NOT COALESCE((p.props->>'hidden')::bool, false)
        GROUP BY p.id
    """)).mappings().all()
    return {r["id"]: {**r, "items": set(r["items"] or [])} for r in rows}


def _records(s, given: list) -> set[int]:
    """The source_items ids a gold list names: ids as they are, keys resolved on this database."""
    ids = {r for r in given if isinstance(r, int)}
    found = keys.resolve(s, [r for r in given if isinstance(r, dict)])
    return ids.union(*found)


def _held(projects: dict[int, dict], parents: set[tuple[int, int]]) -> dict[int, set[int]]:
    """Each project's records with those of every project under it, however deep."""
    children: dict[int, set[int]] = {}
    for child, parent in parents:
        children.setdefault(parent, set()).add(child)
    out = {}
    for top in projects:
        held, seen, todo = set(), set(), [top]
        while todo:
            pid = todo.pop()
            if pid not in seen:
                seen.add(pid)
                held |= projects[pid]["items"] if pid in projects else set()
                todo.extend(children.get(pid, ()))
        out[top] = held
    return out


def score(gold: dict) -> dict:
    with SessionLocal() as s:
        projects = _projects(s)
        parents = {(a, b) for a, b in s.execute(text(
            "SELECT src, dst FROM edges WHERE type = 'PART_OF' AND src IN (SELECT id FROM nodes WHERE type='Project') "
            "AND dst IN (SELECT id FROM nodes WHERE type='Project')"))}
        records = [(_records(s, g.get("must_include") or []), _records(s, g.get("must_exclude") or []))
                   for g in gold["threads"]]
    held = _held(projects, parents)
    report, found_ids, same_ids = [], {}, {}
    for g, (inc, exc) in zip(gold["threads"], records, strict=True):
        terms = [t.lower() for t in g["terms"]]
        # A gold thread is found by name first (the thing exists under the name the person uses); records only
        # break ties, since matching by overlapping records would let one big thread match several gold threads.
        gname = g["name"].lower()
        by_name = [p for p in projects.values() if (p["name"] or "").lower() == gname]
        usable = [re.escape(t.strip()) for t in terms if len(t.strip()) >= 3]
        word = re.compile(r"\b(" + "|".join(usable) + r")\b") if usable else None  # no terms: no name matches
        named = [p for p in projects.values() if word.search((p["name"] or "").lower())] if word else []
        matches = by_name or named
        best = max(matches, key=lambda p: (p in by_name, len(held[p["id"]] & inc), len(held[p["id"]])), default=None)
        dups = [p for p in by_name + named if p is not best and not p["umbrella"]]
        dups = list({p["id"]: p for p in dups}.values())  # a Project named exactly like it and by its terms is one
        found_ids[g["name"]] = best["id"] if best else None
        same_ids[g["name"]] = {best["id"], *(p["id"] for p in dups)} if best else set()
        mine = held[best["id"]] if best else set()
        report.append({
            "name": g["name"], "found": best["name"] if best else None, "duplicates": len(dups),
            "dup_names": [p["name"] for p in dups][:4], "records": len(mine),
            "include_recall": round(len(mine & inc) / len(inc), 2) if best and inc else None,
            "exclude_leaks": len(mine & exc),
            "kind": best["kind"] if best else None})
    # Who took part: names the person gave (match by first name, so "Tara" finds her contact under any name).
    with SessionLocal() as s:
        for g, r in zip(gold["threads"], report, strict=True):
            if not (g.get("participants") or g.get("not_participants")) or not found_ids.get(g["name"]):
                continue
            went = [n.lower() for n in s.execute(text(
                "SELECT n.name FROM edges e JOIN nodes n ON n.id = e.src WHERE e.dst = :t AND e.type = 'INVOLVED_IN'"),
                {"t": found_ids[g["name"]]}).scalars()]
            first = lambda n: n.lower().split()[0]  # noqa: E731
            r["participants_missing"] = [p for p in g.get("participants", []) if not any(first(p) in w for w in went)]
            r["wrongly_went"] = [p for p in g.get("not_participants", []) if any(first(p) in w for w in went)]
    for g, r in zip(gold["threads"], report, strict=True):
        child = found_ids.get(g["name"])
        if g.get("parent"):
            parent = found_ids.get(g["parent"])
            r["parent_ok"] = bool(child and parent and (child, parent) in parents)
        elif "parent" in g:  # null: it sits under nothing (a trip is no part of the school its friends attend)
            r["parent_ok"] = bool(child) and not any(c in same_ids[g["name"]] for c, _p in parents)
    total = len(report)
    summary = {
        "found": sum(1 for r in report if r["found"]), "of": total,
        "duplicated": sum(1 for r in report if r["duplicates"]),
        "leaks": sum(r["exclude_leaks"] for r in report),
        "hierarchy_ok": sum(1 for r in report if r.get("parent_ok")),
        "hierarchy_of": sum(1 for g in gold["threads"] if "parent" in g),
        "participant_errors": sum(len(r.get("participants_missing", [])) + len(r.get("wrongly_went", [])) for r in report)}
    return {"summary": summary, "threads": report}


if __name__ == "__main__":
    path = Path(sys.argv[1] if len(sys.argv) > 1 else Path(settings.data_dir) / "eval" / "threads_gold.json")
    out = score(json.loads(path.read_text()))
    for r in out["threads"]:
        flag = "OK " if r["found"] and not r["duplicates"] and not r["exclude_leaks"] else "-- "
        print(f"{flag}{r['name']:18s} found={r['found']!s:22.22s} records={r['records']:5d} dup={r['duplicates']} "
              f"parent={r.get('parent_ok', '-')!s:5s} {('dups: ' + '; '.join(r['dup_names'])) if r['dup_names'] else ''}"
              + (f" MISSING={r['participants_missing']}" if r.get("participants_missing") else "")
              + (f" WRONGLY_WENT={r['wrongly_went']}" if r.get("wrongly_went") else ""))
    print(json.dumps(out["summary"]))
