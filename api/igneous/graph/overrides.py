"""Your corrections to derived Projects and the connections you added, stored as intents and applied last.

Every derived stage rebuilds its own rows on each run, so a correction can't live on the rows it corrects. It lives
in `overrides`, keyed by natural keys (`Type:key`, e.g. `Project:thread:4f2a...`), never by node or edge ids, which
change on every rebuild:

  rename  node_keys=[project]          value=the new name
  hide    node_keys=[project]          the Project stays in the database, flagged props.hidden; views skip it
  merge   node_keys=[keep, fold]       every link of fold moves to keep (marked props.merged_from) and fold goes; a
                                        thread folded into a thread is the kept thread's facet in the registry, so
                                        the threads stage builds its records into the kept one. data.node: fold as
                                        it was, for undo
  unlink  node_keys=[b, a]             a pair rejection (graph/corrections.py): your 'drop' verdicts on the pair, and
                                        exact copies of the links it removed, for undo; or, with data.user_link, a
                                        link you added and took out again (value=its type), holding the intents it
                                        took out, for undo
  link    node_keys=[src, dst]         value=the edge type: a connection you added; data.props are its props

A Project you put under another Project is in `thread_parents` (origin 'user') instead: the threads stage reads it as
it builds. The edges with extractor 'user' are exactly what these intents say: one for each, and no other.

`apply` is a pure function of these rows. It completes undos first (an undo marks its row `undone_at`; apply reverses
the correction and forgets the row), puts every Project back to its derived state (its props.original_name, neither
renamed nor hidden), and applies every correction again. A Project can be folded into another by your merge, or by a
rebuild that finds the two are one thing (graph/derived.py `merges`): a name you gave it then names the one it went
into (the latest name you gave any of them), unless you named that one yourself, while a hide follows only your
merges, so a rebuild never hides a Project because something you hid turned out to be part of it. It runs as the
pipeline's last step and, when no rebuild is running, right after each correction (graph/corrections.py). It bumps
nothing: whoever publishes bumps the version.
"""
import json
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.derived import follow


@dataclass
class Plan:
    renames: dict[str, str] = field(default_factory=dict)         # project key -> name
    hidden: set[str] = field(default_factory=set)
    merges: dict[str, str] = field(default_factory=dict)          # folded key -> surviving key
    links: list[tuple[str, str, str, dict]] = field(default_factory=list)  # (src key, dst key, edge type, props)


def plan(rows: list[dict]) -> Plan:
    """Resolve active override rows into what to do. Pure, so it is testable without a database.
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
        elif r["op"] == "link" and len(keys) == 2 and r.get("value"):
            p.links.append((keys[0], keys[1], r["value"], (r.get("data") or {}).get("props") or {}))

    p.renames = {survivor(k): v for k, v in p.renames.items()}
    p.hidden = {survivor(k) for k in p.hidden}
    return p


def load(s: Session, undone: bool = False) -> list[dict]:
    """The active corrections, oldest first (with `undone`, also the undone ones waiting to be completed)."""
    return [dict(r) for r in s.execute(text(f"""
        SELECT id, op, node_keys, value, data, created_at, undone_at FROM overrides
        {'' if undone else 'WHERE undone_at IS NULL'} ORDER BY id""")).mappings()]


def add(s: Session, op: str, node_keys: list[str], value: str | None = None, data: dict | None = None) -> int:
    return s.execute(text("""
        INSERT INTO overrides (op, node_keys, value, data) VALUES (:op, :k, :v, CAST(:d AS jsonb)) RETURNING id
    """), {"op": op, "k": node_keys, "v": value, "d": json.dumps(data) if data is not None else None}).scalar_one()


def _ids(s: Session, keys) -> dict[str, int]:
    rows = s.execute(text("SELECT type || ':' || key, id FROM nodes WHERE type || ':' || key = ANY(:k)"),
                     {"k": sorted(set(keys))}).all()
    return {k: i for k, i in rows}


def apply(s: Session) -> dict:
    """Complete every undo, then replay every correction. Idempotent."""
    from igneous.graph.derived import merges

    undone = [r for r in load(s, undone=True) if r["undone_at"] is not None]
    for r in undone:
        _reverse(s, r)
    if undone:
        s.execute(text("DELETE FROM overrides WHERE id = ANY(:ids)"), {"ids": [r["id"] for r in undone]})
    rows = load(s)  # with what the undos gave back (a link you added, then took out)
    p = plan(rows)
    _registry(s, p)
    folded = merges(s)
    return {"undone": len(undone), "merged": _merge(s, p, folded),
            "projects": _projects(s, p, given_names(rows, folded)), "links": _links(s, p, folded)}


def _registry(s: Session, p: Plan) -> None:
    """A thread you folded into a thread is the kept one's facet: the threads stage builds its records into the kept
    thread. A merge you undid gives the folded thread back its own life."""
    known = set(s.execute(text("SELECT key FROM thread_registry")).scalars())
    folds = {f.removeprefix("Project:"): k.removeprefix("Project:") for f, k in p.merges.items()
             if f.startswith("Project:") and k.startswith("Project:")}
    folds = {f: k for f, k in folds.items() if f in known and k in known}
    for fold, keep in sorted(folds.items()):
        s.execute(text("""
            UPDATE thread_registry SET status = 'merged', merged_into = :keep, merged_by = 'user', updated_at = now()
            WHERE key = :fold AND (status, merged_into, merged_by) IS DISTINCT FROM ('merged', :keep, 'user')
        """), {"fold": fold, "keep": keep})
    s.execute(text("""
        UPDATE thread_registry SET status = 'active', merged_into = NULL, merged_by = NULL, updated_at = now()
        WHERE merged_by = 'user' AND NOT (key = ANY(:folds))
    """), {"folds": sorted(folds)})


def _merge(s: Session, p: Plan, folded: dict[str, str]) -> int:
    """Fold every merged Project into the one it ends up in: all its links move there (both directions, any extractor,
    one copy per link), marked with where they came from, and its name joins the survivor's aliases."""
    ids = _ids(s, [*p.merges, *(follow(folded, k) for k in p.merges.values())])
    n = 0
    for fold, keep in sorted(p.merges.items()):
        a, b = ids.get(follow(folded, keep)), ids.get(fold)
        if a is None or b is None or a == b:
            continue
        fold_into(s, b, a, fold)
        n += 1
    return n


def fold_into(s: Session, fold: int, keep: int, fold_key: str, mark: bool = True) -> None:
    """Move every link of node `fold` to `keep` (a link `keep` already has stays as it is), add fold's names to
    keep's aliases, and delete fold. With `mark` (a merge you can undo), each moved link is marked
    props.merged_from {key: fold_key, end} unless an earlier merge already marked it."""
    params = {"f": fold, "k": keep, "fk": fold_key, "mark": mark}
    s.execute(text("DELETE FROM edges WHERE (src = :f AND dst = :k) OR (src = :k AND dst = :f)"), params)
    s.execute(text("""
        DELETE FROM edges e WHERE e.dst = :f AND EXISTS (
            SELECT 1 FROM edges x WHERE x.dst = :k AND x.src = e.src AND x.type = e.type AND x.extractor = e.extractor)
    """), params)
    s.execute(text("""
        DELETE FROM edges e WHERE e.src = :f AND EXISTS (
            SELECT 1 FROM edges x WHERE x.src = :k AND x.dst = e.dst AND x.type = e.type AND x.extractor = e.extractor)
    """), params)
    for end in ("dst", "src"):
        s.execute(text(f"""
            UPDATE edges SET {end} = :k, props = CASE WHEN NOT :mark OR props ? 'merged_from' THEN props
                ELSE props || jsonb_build_object('merged_from', jsonb_build_object('key', CAST(:fk AS text),
                                                                                    'end', '{end}')) END
            WHERE {end} = :f
        """), params)
    s.execute(text("""
        UPDATE nodes k SET props = k.props || jsonb_build_object('aliases', (
            SELECT COALESCE(jsonb_agg(DISTINCT a ORDER BY a), '[]'::jsonb) FROM (
                SELECT jsonb_array_elements_text(COALESCE(k.props->'aliases', '[]'::jsonb)) AS a
                UNION SELECT jsonb_array_elements_text(COALESCE(f.props->'aliases', '[]'::jsonb))
                UNION SELECT COALESCE(f.props->>'original_name', f.name)) names WHERE a <> k.name))
        FROM nodes f WHERE k.id = :k AND f.id = :f
    """), params)
    s.execute(text("DELETE FROM nodes WHERE id = :f"), params)


def _unmerge(s: Session, r: dict) -> None:
    """Undo a merge: the folded Project comes back as it was when you merged it, with every link that moved from it
    (those marked merged_from); the next rebuild recomputes the rest."""
    fold_key = r["node_keys"][1]
    snapshot = (r.get("data") or {}).get("node") or {}
    type_, key = fold_key.split(":", 1)
    s.execute(text("""
        INSERT INTO nodes (type, key, name, props) VALUES (:t, :k, :n, CAST(:p AS jsonb))
        ON CONFLICT (type, key) DO NOTHING
    """), {"t": type_, "k": key, "n": snapshot.get("name") or key,
           "p": json.dumps({k: v for k, v in (snapshot.get("props") or {}).items()
                            if k not in ("renamed", "hidden")})})
    fold = s.execute(text("SELECT id FROM nodes WHERE type = :t AND key = :k"), {"t": type_, "k": key}).scalar_one()
    for end in ("dst", "src"):
        s.execute(text(f"""
            UPDATE edges SET {end} = :f, props = props - 'merged_from'
            WHERE props->'merged_from'->>'key' = :fk AND props->'merged_from'->>'end' = :end
        """), {"f": fold, "fk": fold_key, "end": end})
    name = snapshot.get("props", {}).get("original_name") or snapshot.get("name")
    if name:
        s.execute(text("""
            UPDATE nodes SET props = props || jsonb_build_object('aliases', (
                SELECT COALESCE(jsonb_agg(a ORDER BY a), '[]'::jsonb)
                FROM jsonb_array_elements_text(props->'aliases') a WHERE a <> :n))
            WHERE type || ':' || key = ANY(:keys) AND props ? 'aliases'
        """), {"n": name, "keys": [r["node_keys"][0]]})


def _reverse(s: Session, r: dict) -> None:
    """Undo one correction on the graph as it is now. (A link you added needs nothing here: with its row gone, the
    link goes too, `_links`.)"""
    from igneous.graph import corrections

    data = r.get("data") or {}
    if r["op"] == "unlink" and data.get("user_link"):
        for link in data.get("links", []):  # the intents it took out come back
            add(s, link["op"], link["node_keys"], link["value"], link["data"])
        for child, parent, origin in data.get("parents", []):
            s.execute(text("""INSERT INTO thread_parents (child_key, parent_key, origin) VALUES (:c, :p, :o)
                              ON CONFLICT DO NOTHING"""), {"c": child, "p": parent, "o": origin})
    elif r["op"] == "unlink":
        corrections.restore(s, r)
    elif r["op"] == "merge" and len(r["node_keys"]) == 2:
        _unmerge(s, r)


def given_names(rows: list[dict], folded: dict[str, str]) -> dict[str, dict]:
    """Project key -> the rename of yours (its override row) that names it: the latest you gave the Project itself, else
    the latest you gave a Project folded into it (a merge of yours, or a rebuild that found the two are one thing:
    `derived.merges`). A hide follows only your own merges (`plan`): a Project a rebuild folds into another never hides
    it."""
    own: dict[str, dict] = {}
    inherited: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["id"]):
        if r["op"] == "rename" and r.get("value"):
            key = r["node_keys"][0]
            end = follow(folded, key)
            (own if end == key else inherited)[end] = r
    return {**inherited, **own}


def _projects(s: Session, p: Plan, names: dict[str, dict]) -> int:
    """Every Project back to its derived name, neither renamed nor hidden; then the names you gave (`given_names`) and
    your hides. A Project the backbone stage found to be the same as an organization's thread (props.same_as_org) is
    hidden too. Only rows that change are written."""
    renames = {k: r["value"] for k, r in names.items()}
    changed = []
    for nid, natural, name, props in s.execute(text(
            "SELECT id, type || ':' || key, name, props FROM nodes WHERE type = 'Project' ORDER BY id")):
        props = props or {}
        target = {k: v for k, v in props.items() if k not in ("renamed", "hidden")}
        new_name = props.get("original_name") or name
        if natural in renames:
            new_name, target["renamed"] = renames[natural], True
        if natural in p.hidden or props.get("same_as_org"):
            target["hidden"] = True
        if (new_name, target) != (name, props):
            changed.append({"i": nid, "n": new_name, "p": json.dumps(target)})
    if changed:
        s.execute(text("UPDATE nodes SET name = :n, props = CAST(:p AS jsonb) WHERE id = :i"), changed)
    return len(changed)


def stated_parents(s: Session) -> list[tuple[str, str]]:
    """Projects under Projects as someone stated them (thread_parents: you, or a pinned gold set), as natural keys."""
    return [(f"Project:{c}", f"Project:{p}") for c, p in s.execute(text(
        "SELECT child_key, parent_key FROM thread_parents ORDER BY child_key, parent_key"))]


def _links(s: Session, p: Plan, folded: dict[str, str]) -> int:
    """The connections you added, exactly: an edge with extractor 'user' for each intent (an override 'link', or a
    Project under a Project in thread_parents) on whatever each end is now (a Project you folded away: the one it went
    into), and none that no intent backs (one you took out, a 'same person' you took back). Identity clusters are
    rebuilt when a 'same person' link comes or goes."""
    links = [*p.links, *((c, q, "PART_OF", {"why": "you said so"}) for c, q in stated_parents(s))]
    ids = _ids(s, [follow(folded, k) for a, b, _t, _p in links for k in (a, b)])
    wanted: dict[tuple[int, int, str], dict] = {}
    for a_key, b_key, type_, props in links:
        a, b = ids.get(follow(folded, a_key)), ids.get(follow(folded, b_key))
        if a is not None and b is not None and a != b:
            wanted[(*sorted((a, b)), type_) if type_ == "SAME_AS" else (a, b, type_)] = props
    stale = [r for r in s.execute(text("SELECT id, src, dst, type FROM edges WHERE extractor = 'user' ORDER BY id"))
             if (r.src, r.dst, r.type) not in wanted]
    if stale:
        s.execute(text("DELETE FROM edges WHERE id = ANY(:i)"), {"i": [r.id for r in stale]})
    written, same_as = len(stale), any(r.type == "SAME_AS" for r in stale)
    for (a, b, type_), props in sorted(wanted.items()):
        new = s.execute(text("""
            INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
            VALUES (:a, :b, :t, CAST(:p AS jsonb), 'user', '1', 1.0)
            ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props
                WHERE edges.props IS DISTINCT FROM EXCLUDED.props
            RETURNING (xmax = 0) AS inserted
        """), {"a": a, "b": b, "t": type_, "p": json.dumps(props or {"why": "you added this"})}).scalar()
        if new is not None:
            written += 1
            same_as |= bool(new) and type_ == "SAME_AS"
    if same_as:
        from igneous.graph.resolve import refresh_canonical
        refresh_canonical(s)
    return written
