"""Your profile, from the documents that describe you (resumes, CVs, LinkedIn): the backbone of the graph.

Messages are scattered evidence; a resume says outright where you worked, what you built and when, in the names
you use. So the biggest parts of your life come from there first, and everything else attaches to them:

  1. Self-describing documents: files in Drive, local folders or Notes titled like a resume, CV or LinkedIn export
     (400-30,000 characters), newest first. Every version is read once (cached by content), and counts only when it
     names you.
  2. One structured extraction per version: experience {org, title, start, end}, education {school, degree,
     start, end}, projects {name, description}. Each fact keeps the document it came from.
  3. Experience and education -> you WORKED_AT an Organization named as you name it ("BigCo", "Startup Studio",
     your school) and keyed as a company is (`org:<slug>`, as LinkedIn and contact cards key it, so whichever stage
     meets it first, it is one node; graph/organizations.py company_for), with the domain the document gives it
     (props.domain on the link: the organizations stage gives that domain to this organization). One link per
     organization holds every role you had there (props.roles: an internship, then the full-time job; a degree),
     each its own title and dates, newest first; the link spans them all (valid_from, valid_to), and the backbone
     gives an organization with several roles a thread for each (graph/backbone.py). These are stated facts
     (extractor 'profile'): never judged by the verifier. Projects -> pinned rows in the thread registry (origin
     'profile'), named as you name them and found by their full name, or a word of it that stands for the project in
     your records ("Atlas" for "Atlas Mobile App", not "Mobile").
  4. A role is one title at one organization over one stretch of time: the same title in two documents, at dates
     that overlap, is one role, while a second stint is another. Every version adds to it: an older version adds what
     the newer ones left out, and a newer one wins on dates, except that an end it leaves open ("present") gives way
     to an end another document states for after the newer one was written. Each organization and project comes
     from the newest version that names it the same way.

A LinkedIn export (connectors/linkedin.py) is read too, without a model: its profile item carries the same structure
in `raw_meta.profile`. It has no date of its own, so a dated resume wins on dates and LinkedIn fills in what the
resumes leave out, an end included.
"""
import hashlib
import json
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph import identity
from igneous.graph.derived import norm
from igneous.graph.organizations import company_for, domain_of

EXTRACTOR = "profile"
VERSION = "1"
ALIAS_SHARE = 0.5  # a word of a project's name stands for it when at least this share of its records name the project
GENERIC_WORDS = {"project", "platform", "system", "the", "app", "application", "tool", "website", "site", "service",
                 "mobile", "web", "data", "team", "group", "new", "research", "study", "design", "development"}

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
    """Candidate self-describing documents: resume/CV-titled files, newest first, then the LinkedIn profile
    (`given`: its structure, already extracted)."""
    return s.execute(text(r"""
        SELECT id, source, title, occurred_at, body_text, NULL::jsonb AS given FROM source_items
        WHERE source IN ('fs', 'drive', 'notes') AND coalesce(body_text, '') <> ''
          AND (title ~* '(resume|résumé|\mcv\M|curriculum vitae|linkedin)')
          AND length(body_text) BETWEEN 400 AND 30000
        UNION ALL
        SELECT id, source, title, occurred_at, body_text, raw_meta->'profile' FROM source_items
        WHERE source = 'linkedin' AND external_id = 'profile' AND raw_meta ? 'profile'
        ORDER BY occurred_at DESC NULLS LAST, id DESC
    """)).all()


def cache_key(body: str) -> str:
    """Where the reading of a document with this text is cached (llm_cache)."""
    return "profile-extract:v3:" + hashlib.sha256(body.encode()).hexdigest()


def extract(s: Session, doc) -> dict | None:
    """One cached structured read of a document. Uses the stronger model: this is the backbone, once per version.
    A version already read keeps counting with the model off; a new one is read only if its source is opted in
    (#179)."""
    from igneous.llm import LLMDisabled, allowed_sources, cache_lookup, cache_store, structured

    if doc.given:  # the LinkedIn export states it outright: no model call
        return doc.given
    key = cache_key(doc.body_text)
    cached = cache_lookup(key)
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
    cache_store(key, out)
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


def _same_role(a: dict, b: dict) -> bool:
    """Two documents' entries for one role: the same title, at dates that overlap (or one of them gives none). Two
    stints with one title are two roles."""
    if norm(a["title"]) != norm(b["title"]):
        return False
    if not a["start"] or not b["start"]:
        return True
    return a["start"] <= (b["end"] or "9999") and b["start"] <= (a["end"] or "9999")


def _merge_role(kept: dict, older: dict) -> None:
    """An older document's entry for a role adds what the newer ones left out. The newer one wins on dates, except
    that an end it leaves open ("present") gives way to an end the other states for after the newer one was written
    (a LinkedIn export has no date of its own, so its end always counts)."""
    kept["start"] = kept["start"] or older["start"]
    if kept["end"] is None and older["end"] and (kept["at"] is None or older["end"] > kept["at"]):
        kept["end"] = older["end"]


def aliases(s: Session, name: str) -> list[str]:
    """Words of a project's name that stand for it on their own: at least ALIAS_SHARE of the life-source records
    naming the word (as a whole word) name the whole project too. A word used for other things ("Mobile" in your
    phone bills) isn't one, nor is a generic word."""
    from igneous.source_catalog import LIFE

    out = []
    for word in dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", name)):
        if word.lower() in GENERIC_WORDS or word.lower() == name.lower():
            continue
        named, both = s.execute(text("""
            SELECT count(*), count(*) FILTER (WHERE hay ~* :full) FROM (
                SELECT coalesce(title, '') || ' ' || left(coalesce(body_text, ''), 4000) AS hay FROM source_items
                WHERE source = ANY(:src)) q WHERE hay ~* :word
        """), {"word": r"\m" + re.escape(word) + r"\M", "full": r"\m" + re.escape(name) + r"\M",
               "src": sorted(LIFE)}).one()
        if named and both / named >= ALIAS_SHARE:
            out.append(word)
    return out


def run(s: Session, log=print) -> dict:
    """Build the backbone from every self-describing document, newest first (newer wins, older adds what it lacks).
    Owns WORKED_AT and Organization PART_OF edges with extractor 'profile', and the registry rows of origin
    'profile'."""
    self_id = identity.self_id(s)
    if self_id is None:
        return {"documents": 0}
    s.execute(text("""DELETE FROM edges e USING nodes a WHERE a.id = e.src AND e.extractor = :x
                      AND a.type <> 'Project'"""), {"x": EXTRACTOR})
    my_names = {n.lower() for n in s.execute(text("""
        SELECT n.name FROM nodes n WHERE n.id = :me OR n.id IN (SELECT node_id FROM canonical WHERE canonical_id = :me)
    """), {"me": self_id}).scalars() if n and "@" not in n}
    # Every role, per organization: a position (or a degree, at a school) is (organization, title, dates), and each
    # document's entry for it adds to the one role (`_same_role`, `_merge_role`). The organization's name, domain and
    # parent come from the newest entry that gives them.
    orgs: dict[str, dict] = {}     # org slug -> {name, domain_hint, parent, project, roles}
    projects: dict[str, dict] = {}
    used = 0
    for doc in documents(s):
        out = extract(s, doc)
        if not out or not out.get("is_about_owner"):
            continue
        # A resume in your Drive may be a friend's you reviewed: only one that names YOU counts. A LinkedIn export
        # is always of the account that exported it.
        if doc.source != "linkedin" and not _is_me(out.get("person_name", ""), my_names):
            continue
        used += 1
        via = "your LinkedIn" if doc.source == "linkedin" else "your resume"
        at = doc.occurred_at.date().isoformat() if doc.occurred_at else None
        entries = [(ed.get("school") or "", ed, True) for ed in out.get("education", [])] + \
                  [(e.get("org") or "", e, False) for e in out.get("experience", [])]
        for name, e, school in entries:
            k = _slug(name)
            if not k:
                continue
            org = orgs.setdefault(k, {"name": name, "domain_hint": "", "parent": "", "project": "", "roles": []})
            for field in ("domain_hint", "parent", "project"):
                org[field] = org[field] or (e.get(field) or "").strip()
            role = {"title": ((e.get("degree") if school else e.get("title")) or "").strip(), "school": school,
                    "start": _date(e.get("start")), "end": _date(e.get("end")), "via": via, "doc": doc.id, "at": at}
            same = next((r for r in org["roles"] if r["school"] == school and _same_role(r, role)), None)
            if same:
                _merge_role(same, role)
            else:
                org["roles"].append(role)
        for p in out.get("projects", []):
            k = _slug(p["name"])
            if k and k not in projects:
                projects[k] = {**p, "doc": doc.id}

    # An organization is keyed as a company is (`org:<slug>`, the key LinkedIn and contact cards give it), whatever
    # stage or record meets it first: by the name its domain's own people give it, else by yours, and named as you
    # name it (graph/organizations.py company_for). The domain your resume gives it is its claim, which the
    # organizations stage reads off the WORKED_AT link (props.domain). A domain names one organization: schools claim
    # theirs first, and a later entry with the same domain (a lab at the university) is part of the one that claimed
    # it, never a rename of it.
    claimed: dict[str, int] = {}   # domain -> the org that claimed it
    by_name: dict[str, int] = {}   # the names your documents give -> org

    def org_node(name: str, domain: str) -> tuple[int | None, int | None]:
        """(org id, the org it's part of when another one claimed its domain first); no id for a name that can't be
        an organization's."""
        d = (domain or "").lower().strip()
        d = d if domain_of(f"x@{d}") == d else ""
        nid = by_name.get(_slug(name))
        if nid is None:
            company = company_for(s, name, "" if d in claimed else d)
            if company is None:
                return None, None
            nid = s.execute(text("""
                INSERT INTO nodes (type, key, name, props) VALUES ('Organization', :k, :n, '{"named_by": "profile"}')
                ON CONFLICT (type, key) DO UPDATE SET name = EXCLUDED.name,
                    props = nodes.props || '{"named_by": "profile"}'
                RETURNING id
            """), {"k": company[0], "n": company[1]}).scalar_one()
            by_name[_slug(name)] = nid
        if d:
            claimed.setdefault(d, nid)
        return nid, (claimed[d] if d and claimed[d] != nid else None)

    def claim(oid: int) -> dict:
        """The domain `oid` claims, for its WORKED_AT link (graph/organizations.py reads it)."""
        return next(({"domain": d} for d, n in sorted(claimed.items()) if n == oid), {})

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
    # Schools first: they claim their domains before a lab at the same domain does.
    for k, e in sorted(orgs.items(), key=lambda kv: not any(r["school"] for r in kv[1]["roles"])):
        oid, part_of = org_node(e["name"], e["domain_hint"])
        if oid is None:
            continue
        org_ids[k] = oid
        # One link per organization (edges are one per pair, type and stage), with every role you had there, newest
        # first, each with its own dates; the link spans them all.
        roles = sorted(e["roles"], key=lambda r: (r["start"] or "", r["end"] or "9999", r["title"]), reverse=True)
        vias = {via: [r["title"] for r in roles if r["via"] == via] for via in dict.fromkeys(r["via"] for r in roles)}
        props = {"why": "; ".join(f"{via}: {', '.join(titles)}" for via, titles in vias.items()),
                 "title": roles[0]["title"],
                 "roles": [{"title": r["title"], "start": r["start"], "end": r["end"], "via": r["via"],
                            "source_item_id": r["doc"], **({"school": True} if r["school"] else {})} for r in roles],
                 **({"school": True} if any(r["school"] for r in roles) else {}), **claim(oid)}
        starts = [r["start"] for r in roles if r["start"]]
        edge(self_id, oid, "WORKED_AT", props, min(starts, default=None),
             None if any(r["end"] is None for r in roles) else max(r["end"] for r in roles), roles[0]["doc"])
        parent = part_of
        if e["parent"]:
            parent = by_name.get(_slug(e["parent"])) or org_node(e["parent"], "")[0]
        if parent and parent != oid:
            edge(oid, parent, "PART_OF", {"why": f"{roles[0]['via']}: {e['name']} is part of {e['parent'] or 'it'}"},
                 sid=roles[0]["doc"])
        if e["project"]:
            named_projects.setdefault(oid, set()).add(e["project"])
    # Projects become pinned rows of the thread registry, named as you name them. A row keeps its state (a project
    # folded into a thread stays its facet); a project your documents no longer list goes.
    keys = []
    for k, p in projects.items():
        key = "profile:" + hashlib.sha256(k.encode()).hexdigest()[:16]
        keys.append(key)
        terms = list(dict.fromkeys([p["name"], *aliases(s, p["name"])]))
        s.execute(text("""
            INSERT INTO thread_registry (key, name, kind, terms, status, pinned, origin, confidence)
            VALUES (:k, :n, 'project', :t, 'active', true, 'profile', 1.0)
            ON CONFLICT (key) DO UPDATE SET name = EXCLUDED.name, terms = EXCLUDED.terms, pinned = true,
                updated_at = now()
            WHERE (thread_registry.name, thread_registry.terms) IS DISTINCT FROM (EXCLUDED.name, EXCLUDED.terms)
        """), {"k": key, "n": p["name"][:80], "t": terms})
        oid = org_ids.get(_slug(p.get("org") or "")) or by_name.get(_slug(p.get("org") or ""))
        if oid:
            named_projects.setdefault(oid, set()).add(p["name"])
    # Which projects sit under which org: read by the backbone stage once threads exist (graph/backbone.py).
    s.execute(text("UPDATE nodes SET props = props - 'resume_projects' WHERE type = 'Organization' AND props ? 'resume_projects'"))
    for oid, names in named_projects.items():
        s.execute(text("UPDATE nodes SET props = props || jsonb_build_object('resume_projects', CAST(:n AS jsonb)) WHERE id = :i"),
                  {"n": json.dumps(sorted(names)), "i": oid})
    s.execute(text("DELETE FROM thread_registry WHERE origin = 'profile' AND NOT (key = ANY(:k))"), {"k": keys})
    # Organizations an older reading of your resume made that nothing points at any more.
    s.execute(text("""DELETE FROM nodes n WHERE n.type = 'Organization' AND n.props->>'named_by' = 'profile'
                      AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = n.id OR e.dst = n.id)
                      AND NOT EXISTS (SELECT 1 FROM spine_node_support x WHERE x.node_id = n.id)"""))
    stats = {"documents": used, "orgs": sum(1 for o in orgs.values() if not any(r["school"] for r in o["roles"])),
             "schools": sum(1 for o in orgs.values() if any(r["school"] for r in o["roles"])),
             "roles": sum(len(o["roles"]) for o in orgs.values()), "projects": len(projects)}
    if log:
        log(json.dumps(stats))
    return stats
