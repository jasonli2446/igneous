"""Score derived threads/projects against a gold set: found, duplicated, hierarchy, must-include/exclude.

    IGNEOUS_DATABASE_URL=...igneous_personal uv run python eval/threads_eval.py [data/eval/threads_gold.json]

The gold file is local (it names a person's projects and people) and lives in git-ignored data/. Each entry:
{name, kind, terms, parent, children, must_include: [source_item ids], must_exclude: [...], members: [names]}.
A gold thread is "found" by the derived Project whose records best cover must_include (and whose name matches
any of its terms). Reported per thread and in total, so a change that fixes one case and breaks another shows.
"""
import json
import re
import sys
from pathlib import Path

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


def score(gold: dict) -> dict:
    with SessionLocal() as s:
        projects = _projects(s)
        parents = {(a, b) for a, b in s.execute(text(
            "SELECT src, dst FROM edges WHERE type = 'PART_OF' AND src IN (SELECT id FROM nodes WHERE type='Project') "
            "AND dst IN (SELECT id FROM nodes WHERE type='Project')"))}
    report, found_ids = [], {}
    for g in gold["threads"]:
        terms = [t.lower() for t in g["terms"]]
        inc, exc = set(g.get("must_include") or []), set(g.get("must_exclude") or [])
        # A gold thread is found by name first (the thing exists under the name the person uses); records only
        # break ties. Matching by overlapping records first let one big thread "match" several gold threads.
        gname = g["name"].lower()
        by_name = [p for p in projects.values() if (p["name"] or "").lower() == gname]
        word = re.compile(r"\b(" + "|".join(re.escape(t.strip()) for t in terms if len(t.strip()) >= 3) + r")\b")
        named = [p for p in projects.values() if word.search((p["name"] or "").lower())] if terms else []
        matches = by_name or named
        best = max(matches, key=lambda p: (p in by_name, len(p["items"] & inc), len(p["items"])), default=None)
        dups = [p for p in named if p is not best and not p["umbrella"]]
        found_ids[g["name"]] = best["id"] if best else None
        report.append({
            "name": g["name"], "found": best["name"] if best else None, "duplicates": len(dups),
            "dup_names": [p["name"] for p in dups][:4], "records": len(best["items"]) if best else 0,
            "include_recall": round(len(best["items"] & inc) / len(inc), 2) if best and inc else None,
            "exclude_leaks": len(best["items"] & exc) if best else 0,
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
        if g.get("parent"):
            child, parent = found_ids.get(g["name"]), found_ids.get(g["parent"])
            r["parent_ok"] = bool(child and parent and (child, parent) in parents)
    total = len(report)
    summary = {
        "found": sum(1 for r in report if r["found"]), "of": total,
        "duplicated": sum(1 for r in report if r["duplicates"]),
        "leaks": sum(r["exclude_leaks"] for r in report),
        "hierarchy_ok": sum(1 for r in report if r.get("parent_ok")),
        "hierarchy_of": sum(1 for g in gold["threads"] if g.get("parent")),
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
