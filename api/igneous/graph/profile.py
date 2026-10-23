"""Your profile, from the documents that describe you (resumes, CVs, LinkedIn): the backbone of the graph.

Messages are scattered evidence; a resume says outright where you worked, what you built and when, in the names
you use. So the biggest parts of your life come from there first, and everything else attaches to them:

  1. Self-describing documents are found by content, not filename alone: a document about the owner that lists
     roles and dates. Every version is read once (cached by content), newest first.
  2. One structured extraction per version: experience {org, title, start, end}, education {school, degree,
     start, end}, projects {name, description}. Each fact keeps the document it came from.
  3. Experience -> you WORKED_AT an Organization named as you name it ("BigCo", "Startup Studio"), with dates and the
     domain it matches. Projects -> pinned threads named as you name them. Education -> your school.
     All are pinned (origin 'profile'): never dropped by the verifier, and the gold-set equivalent of what you'd say.
  4. Older versions keep roles that later versions drop (history), newer versions win on titles and dates.
"""
import hashlib
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings

EXTRACTOR = "profile"
VERSION = "1"

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["is_about_owner", "person_name", "experience", "education", "projects"],
    "properties": {
        "is_about_owner": {"type": "boolean"},
        "person_name": {"type": "string"},
        "experience": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["org", "title", "start", "end", "domain_hint", "parent", "project"],
            "properties": {"org": {"type": "string"}, "title": {"type": "string"}, "start": {"type": "string"},
                           "end": {"type": "string"}, "domain_hint": {"type": "string"},
                           "parent": {"type": "string"}, "project": {"type": "string"}}}},
        "education": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["school", "degree", "start", "end", "domain_hint"],
            "properties": {"school": {"type": "string"}, "degree": {"type": "string"}, "start": {"type": "string"},
                           "end": {"type": "string"}, "domain_hint": {"type": "string"}}}},
        "projects": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "description", "org"],
            "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                           "org": {"type": "string"}}}},
    },
}


def documents(s: Session) -> list:
    """Candidate self-describing documents: resume/CV-titled files, newest first."""
    return s.execute(text(r"""
        SELECT id, source, title, occurred_at, body_text FROM source_items
        WHERE source IN ('fs', 'drive', 'notes') AND coalesce(body_text, '') <> ''
          AND (title ~* '(resume|résumé|\mcv\M|curriculum vitae|linkedin)')
          AND length(body_text) BETWEEN 400 AND 30000
        ORDER BY occurred_at DESC NULLS LAST
    """)).all()


def extract(s: Session, doc) -> dict | None:
    """One cached structured read of a document. Uses the stronger model: this is the backbone, once per version.
    A version already read keeps counting with the model off; a new one is read only if its source is opted in
    (#179)."""
    from igneous.llm import LLMDisabled, allowed_sources, structured

    key = "profile-extract:v3:" + hashlib.sha256(doc.body_text.encode()).hexdigest()
    cached = s.execute(text("SELECT response FROM llm_cache WHERE cache_key = :k"), {"k": key}).scalar()
    if cached is not None:
        return cached
    if not settings.llm_enabled or doc.source not in allowed_sources():
        return None
    try:
        out, _ = structured(
            "Read a document and, if it describes its owner (a resume, CV or profile), extract their history as "
            "they state it. Use the organization and project names exactly as written ('BigCo', 'Startup Studio', "
            "'Robotics Lab'). An entry's org is the organization alone: when a heading combines an institution, an "
            "organization and a project or client ('State U Robotics Lab - Acme Client'), org is the organization ('Robotics Lab'), "
            "parent is the larger institution it belongs to ('State University', spelled out), and "
            "project is the project or client ('Acme Client'); otherwise parent and project are ''. Dates as YYYY-MM "
            "(or YYYY, or 'present'). domain_hint: the organization's or school's own email/web domain if you can "
            "tell (employer.com, university.edu), else ''; a lab or program inside a university has no domain of its own (''). "
            "A project's org: the employer or lab it was done for, else ''. If the "
            "document isn't about its owner, is_about_owner=false and leave the lists empty. person_name: the full "
            "name of the person the document is about, exactly as written at the top.",
            doc.body_text[:12000], SCHEMA, max_tokens=3000)
    except LLMDisabled:
        return None
    except Exception:
        return None
    s.execute(text("INSERT INTO llm_cache (cache_key, response) VALUES (:k, CAST(:r AS jsonb)) "
                   "ON CONFLICT (cache_key) DO NOTHING"), {"k": key, "r": json.dumps(out)})
    return out


def _is_me(name: str, my_names: set[str]) -> bool:
    """The document's subject is you: first and last name match one of your own names."""
    words = [w for w in re.findall(r"[a-z]+", (name or "").lower()) if len(w) > 1]
    if len(words) < 2:
        return False
    return any(words[0] in mine.split() and words[-1] in mine.split() for mine in my_names)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _date(v: str):
    v = (v or "").strip().lower()
    if not v or v in ("present", "current", "now"):
        return None
    m = re.match(r"(\d{4})(?:-(\d{1,2}))?", v)
    return f"{m.group(1)}-{int(m.group(2) or 1):02d}-01" if m else None


def run(s: Session, log=print) -> dict:
    """Build the backbone from every self-describing document, newest first (newer wins, older adds history)."""
    from igneous.graph import threads

    self_id = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND (props->>'self')::bool LIMIT 1")).scalar()
    if self_id is None:
        return {"documents": 0}
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    s.execute(text("DELETE FROM thread_registry WHERE origin = 'profile'"))
    my_names = {n.lower() for n in s.execute(text("""
        SELECT n.name FROM nodes n WHERE n.id = :me OR n.id IN (SELECT node_id FROM canonical WHERE canonical_id = :me)
    """), {"me": self_id}).scalars() if n and "@" not in n}
    threads.ensure_registry(s)
    threads._migrate_registry(s)
    orgs: dict[str, dict] = {}
    projects: dict[str, dict] = {}
    schools: dict[str, dict] = {}
    used = 0
    for doc in documents(s):
        out = extract(s, doc)
        if not out or not out.get("is_about_owner"):
            continue
        # A resume in your Drive may be a friend's you reviewed: only one that names YOU counts.
        if not _is_me(out.get("person_name", ""), my_names):
            continue
        used += 1
        for e in out.get("experience", []):
            k = _slug(e["org"])
            if k and k not in orgs:  # newest version wins
                orgs[k] = {**e, "doc": doc.id}
        for p in out.get("projects", []):
            k = _slug(p["name"])
            if k and k not in projects:
                projects[k] = {**p, "doc": doc.id}
        for ed in out.get("education", []):
            k = _slug(ed["school"])
            if k and k not in schools:
                schools[k] = {**ed, "doc": doc.id}

    # A domain names one organization. Schools claim theirs first; a later entry with the same domain (a lab at
    # the university) is part of the one that claimed it, never a rename of it.
    claimed: dict[str, int] = {}
    by_name: dict[str, int] = {}

    def org_node(name: str, domain: str) -> tuple[int, int | None]:
        """(org id, the org it's part of when its domain was already claimed)."""
        d = (domain or "").lower().strip()
        if d in claimed:
            nid = s.execute(text("""
                INSERT INTO nodes (type, key, name, props) VALUES ('Organization', :k, :n, '{"named_by": "profile"}')
                ON CONFLICT (type, key) DO UPDATE SET name = EXCLUDED.name RETURNING id
            """), {"k": "org:" + _slug(name), "n": name}).scalar_one()
            by_name.setdefault(_slug(name), nid)
            return nid, claimed[d]
        nid = None
        if d:  # reuse the domain's node (people already MEMBER_OF it count), named as you name it
            nid = s.execute(text("SELECT id FROM nodes WHERE type = 'Organization' AND key = :d"), {"d": d}).scalar()
            if nid:
                s.execute(text("UPDATE nodes SET name = :n, props = props || '{\"named_by\": \"profile\"}' WHERE id = :i"),
                          {"n": name, "i": nid})
        if nid is None:  # no domain given: the org already in your graph under this name, if one is
            nid = by_name.get(_slug(name)) or s.execute(text("""
                SELECT id FROM nodes WHERE type = 'Organization' AND lower(name) = lower(:n)
                ORDER BY (SELECT count(*) FROM edges e WHERE e.dst = nodes.id) DESC LIMIT 1"""), {"n": name}).scalar()
        if nid is None:
            nid = s.execute(text("""
                INSERT INTO nodes (type, key, name, props) VALUES ('Organization', :k, :n, '{"named_by": "profile"}')
                ON CONFLICT (type, key) DO UPDATE SET name = EXCLUDED.name RETURNING id
            """), {"k": d or "org:" + _slug(name), "n": name}).scalar_one()
        if d:
            claimed[d] = nid
        by_name.setdefault(_slug(name), nid)
        return nid, None

    def edge(src: int, dst: int, type_: str, props: dict, frm=None, to=None, sid=None):
        s.execute(text("""
            INSERT INTO edges (src, dst, type, props, valid_from, valid_to, extractor, extractor_version, confidence,
                               source_item_id)
            VALUES (:a, :b, :t, CAST(:p AS jsonb), :f, :u, :x, :v, 1.0, :sid)
            ON CONFLICT (src, dst, type, extractor) DO UPDATE SET props = EXCLUDED.props,
                valid_from = EXCLUDED.valid_from, valid_to = EXCLUDED.valid_to
        """), {"a": src, "b": dst, "t": type_, "p": json.dumps(props), "f": frm, "u": to, "x": EXTRACTOR, "v": VERSION,
               "sid": sid})

    org_ids: dict[str, int] = {}
    named_projects: dict[int, set[str]] = {}  # org -> project names your resume puts under it
    for k, ed in schools.items():
        oid, _ = org_node(ed["school"], ed.get("domain_hint") or "")
        org_ids[k] = oid
        edge(self_id, oid, "WORKED_AT", {"why": f"your resume: {ed['degree']}", "title": ed["degree"], "school": True},
             _date(ed["start"]), _date(ed["end"]), ed["doc"])
    for k, e in orgs.items():
        oid, part_of = org_node(e["org"], e.get("domain_hint") or "")
        org_ids[k] = oid
        edge(self_id, oid, "WORKED_AT", {"why": f"your resume: {e['title']}", "title": e["title"]},
             _date(e["start"]), _date(e["end"]), e["doc"])
        parent = part_of
        if e.get("parent"):
            parent = by_name.get(_slug(e["parent"])) or org_node(e["parent"], "")[0]
        if parent and parent != oid:
            edge(oid, parent, "PART_OF", {"why": f"your resume: {e['org']} is part of "
                                                  f"{e.get('parent') or 'it'}"}, sid=e["doc"])
        if e.get("project"):
            named_projects.setdefault(oid, set()).add(e["project"])
    # Projects become pinned threads named as you name them (the registry keeps them across runs).
    for k, p in projects.items():
        key = "profile:" + hashlib.sha256(k.encode()).hexdigest()[:16]
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", p["name"])
                 if w.lower() not in ("project", "platform", "system", "the")][:2]
        terms = [p["name"], *words]
        s.execute(text("""
            INSERT INTO thread_registry (key, name, kind, terms, status, pinned, origin, confidence)
            VALUES (:k, :n, 'project', :t, 'active', true, 'profile', 1.0)
            ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, terms = EXCLUDED.terms, status = 'active',
                pinned = true, updated_at = now()
        """), {"k": key, "n": p["name"][:80], "t": list(dict.fromkeys(terms))})
        oid = org_ids.get(_slug(p.get("org") or "")) or by_name.get(_slug(p.get("org") or ""))
        if oid:
            named_projects.setdefault(oid, set()).add(p["name"])
    # Which projects sit under which org: read by the backbone stage once threads exist (graph/backbone.py).
    s.execute(text("UPDATE nodes SET props = props - 'resume_projects' WHERE type = 'Organization' AND props ? 'resume_projects'"))
    for oid, names in named_projects.items():
        s.execute(text("UPDATE nodes SET props = props || jsonb_build_object('resume_projects', CAST(:n AS jsonb)) WHERE id = :i"),
                  {"n": json.dumps(sorted(names)), "i": oid})
    s.execute(text("DELETE FROM thread_parents WHERE origin = 'profile'"))
    # Organizations an older reading of your resume made that nothing points at any more.
    s.execute(text("""DELETE FROM nodes n WHERE n.type = 'Organization' AND n.props->>'named_by' = 'profile'
                      AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = n.id OR e.dst = n.id)"""))
    s.commit()
    stats = {"documents": used, "orgs": len(orgs), "schools": len(schools), "projects": len(projects)}
    if log:
        log(json.dumps(stats))
    return stats
