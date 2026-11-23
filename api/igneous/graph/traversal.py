"""What the explorer shows: the opening view around a centre, a node's connections, and why two things are linked.

Each reader takes a session and returns plain data, so the HTTP API (igneous/main.py) and the MCP server call the same
functions. Every person is their identity head (graph/identity.py), only what graph/visibility.py shows appears as a
node, member or connection, and no link joins a pair you rejected. Queries start from the records of the node they are
about (its identities' edges, found by index), never from a scan of every message. The opening view and the ranking it
rests on are kept per graph version (graph/dirty.py), for the API and the MCP server alike.

Every edge says where it came from: `origin` is 'stated' (read from your data: who sent what, your contacts, your
resume), 'user' (you added it), 'agent:<name>' (an agent added it) or 'derived' (inferred by a rule or a model, or
drawn by the explorer itself, like the spokes from a centre), and `stated` is whether it is a fact rather than an
inference.
"""
import hashlib
import math
import re
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph import dirty, identity, visibility
from igneous.graph.organizations import PERSONAL_DOMAINS, YOUR_ORGS
from igneous.graph.visibility import group_conversation, not_owner, shown, visible

HALF_LIFE_DAYS = 90  # an interaction's weight halves every 90 days: last week ~1, six months ~0.25, 2 years ~0
CONTAINED = 0.8  # share of two people's common records inside one project or group chat that makes it "that's how they know each other"
MIN_SHOWN = 3   # every node you open shows at least this many connections, if it has them
GROUP_MAX = 25  # members revealed at once when expanding a group or event
PATH_MAX_HOPS = 3
PATH_FANOUT = 200  # the path search never passes through a node with more links than this (a mass mail, a big org)
EDGE_EVIDENCE_LIMIT = 5
# A file that is code, not a document: detail inside a project, never shown beside it.
CODE_FILE = r"\.(ts|tsx|js|jsx|mjs|py|go|rs|java|c|h|cpp|json|ya?ml|toml|lock|css|scss|sh|sql|env)$"


def _decay(column: str) -> str:
    """SQL weight of a record dated `column`: 1 now, halving every HALF_LIFE_DAYS (bind :hl)."""
    return f"power(0.5, GREATEST(0, EXTRACT(EPOCH FROM now() - {column}) / 86400) / :hl)"


# ---------------------------------------------------------------------------------------------------------------
# Names: a phone number reads as the contact it belongs to.
# ---------------------------------------------------------------------------------------------------------------

PHONE = re.compile(r"\+?\d{10,15}")


def handle_names(s: Session) -> dict[str, str]:
    """Phone number -> the name of the person it belongs to: its own contact card, or, once identity resolution has
    folded it into someone (an address with the same name, a contact card listing both), that person's best real
    name. Only phone handles: they are what text and labels carry in place of a name."""
    rows = s.execute(text("""
        WITH named AS (
            SELECT COALESCE(c.canonical_id, n.id) AS head, n.name, (n.props->>'named_by' = 'contacts') AS card
            FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
            WHERE n.type = 'Person' AND n.name ~ ' ' AND n.name !~ '@' AND n.name !~ '[0-9]{5,}'
        ), best AS (
            SELECT DISTINCT ON (head) head, name FROM named ORDER BY head, card DESC, length(name) DESC
        )
        SELECT n.key, COALESCE(b.name, CASE WHEN n.props->>'named_by' = 'contacts' THEN n.name END)
        FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
        LEFT JOIN best b ON b.head = COALESCE(c.canonical_id, n.id)
        WHERE n.type = 'Person' AND n.key ~ '^\\+?[0-9]{10,15}$'
    """)).all()
    out = {}
    for key, name in rows:
        if not name or PHONE.fullmatch(name.strip()):
            continue  # a card with only a number on it names nothing
        out[key] = name
        out[re.sub(r"\D", "", key)[-10:]] = name  # message text may carry the number without +1
    return out


def names(s: Session) -> dict[str, str]:
    """handle_names, kept per graph version."""
    return dirty.cached(s, ("names",), lambda: handle_names(s))


def name_handles(t: str | None, named: dict[str, str], mask: bool = False) -> str | None:
    """'[14:39] +16174075277: Yes' -> '[14:39] Sam Lee: Yes' where the number is a saved contact. With `mask`,
    a number with no contact becomes '···5277' (for labels, where a wall of digits is unreadable)."""
    if not t:
        return t

    def sub(m):
        hit = named.get(m.group(), named.get(re.sub(r"\D", "", m.group())[-10:]))
        return hit or (f"···{m.group()[-4:]}" if mask else m.group())
    return PHONE.sub(sub, t)


def pretty_thread(name: str | None, named: dict[str, str]) -> str | None:
    """An unnamed group chat is titled by its members' handles; show names, and cap long lists."""
    if not name or not PHONE.search(name):
        return name
    parts = [name_handles(p.strip(), named) for p in name.split(",")]
    shown_ = [p.split()[0] if p and not PHONE.fullmatch(p) else p for p in parts]
    extra = len(shown_) - 3
    return ", ".join(shown_[:3]) + (f" and {extra} other{'s' if extra != 1 else ''}" if extra > 0 else "")


def label(name: str | None, node_type: str, named: dict[str, str]) -> str | None:
    """A node's name as the explorer shows it: a group chat by its members' names, and a phone number by its contact's
    name, or its last four digits when it has none."""
    return name_handles(pretty_thread(name, named) if node_type == "Thread" else name, named, mask=True)


# ---------------------------------------------------------------------------------------------------------------
# Sections: what kind of thing a node is, for its color and the order of the opening view.
# ---------------------------------------------------------------------------------------------------------------

CATEGORY = {"Person": "People", "Thread": "Groups", "Event": "Events", "Organization": "Organizations",
            "Project": "Projects", "File": "Files", "Folder": "Files", "Topic": "Topics", "Place": "Places"}

# A judged thread's kind ("trip", "TA job", "book club") names its section, by whole words, first match wins, and says
# what it is for placing it under one of your organizations (graph/backbone.py): a class, work, something of your own
# (a trip, a hobby, a party, a side project), or nothing either way (a project, an event: whose it is shows elsewhere).
_KIND_SECTIONS = [
    (("trip", "trips", "travel", "travels", "vacation", "holiday", "visit"), "Trips", "personal"),
    (("course", "class", "lecture", "seminar", "lab section", "coursework", "capstone"), "Courses", "class"),
    (("job", "internship", "intern", "work", "employment", "ta", "teaching assistant", "role", "position", "hiring",
      "interview", "onboarding", "field trial", "field trials"), "Work", "work"),
    (("hobby", "hobbies", "game", "games", "sport", "sports", "club", "climbing", "music", "band", "league",
      "campaign"), "Hobbies", "personal"),
    (("side project", "personal project"), "Projects", "personal"),
    (("project", "client", "startup", "research", "hackathon", "repo", "app", "platform", "thesis"), "Projects", None),
    (("party", "wedding", "concert", "festival", "hangout"), "Events", "personal"),
    (("event",), "Events", None),
]
# Sections of things that are not projects: a kind never takes one of these names for itself.
_RESERVED = frozenset({"People", "Groups", "Files", "Topics", "Places"})


def _kind_entry(kind: str | None) -> tuple[str, str | None] | None:
    """(section, what it is for placing) of the first entry of _KIND_SECTIONS the kind names, or None."""
    phrase = f" {' '.join(re.findall(r'[a-z0-9]+', (kind or '').lower()))} "
    return next(((section, placing) for terms, section, placing in _KIND_SECTIONS
                 if any(f" {t} " in phrase for t in terms)), None)


def placing_kind(kind: str | None) -> str | None:
    """What a judged kind says a thread is, for placing it under an organization: 'class', 'work', 'personal', or None
    (a kind that says neither, or no kind)."""
    entry = _kind_entry(kind)
    return entry[1] if entry else None


def section_for_kind(kind: str | None) -> str:
    """The section a project of this kind belongs to. A kind none of the sections names becomes its own section,
    named after its last word ("fundraiser" -> Fundraisers), or after the whole kind when that word names a section of
    other things ("reading group" -> Reading groups, not the Groups of group chats)."""
    words = re.findall(r"[a-z0-9]+", (kind or "").lower())
    if not words or words == ["thread"]:
        return "Projects"
    if entry := _kind_entry(kind):
        return entry[0]

    def plural(w: str) -> str:
        return w if w.endswith("s") else w + "s"
    section = plural(words[-1]).capitalize()
    if section in _RESERVED:
        section = plural(" ".join(words)).capitalize()
    return section if section not in _RESERVED else f"Other {section.lower()}"


def category(node_type: str, kind: str | None = None) -> str | None:
    """A node's section: its type's, or for a project the one its kind names."""
    return section_for_kind(kind) if node_type == "Project" else CATEGORY.get(node_type)


# ---------------------------------------------------------------------------------------------------------------
# Edges: where each came from, and the links the explorer draws itself.
# ---------------------------------------------------------------------------------------------------------------

# Plain-language provenance of an edge, by the extractor that made it.
_HOW = {
    "spine": "Straight from your data (who sent and received it, or the conversation it's in).",
    "contacts": "From your contacts.",
    "profile": "From your resume or LinkedIn profile.",
    "linkedin": "From your LinkedIn connections.",
    "github": "From GitHub.",
    "self": "These are your own addresses and numbers.",
    "user": "You added this.",
    "threads": "Suggested automatically from shared words and timing.",
    "hierarchy": "Suggested automatically from the names.",
    "thread-resolution": "Suggested automatically from the two threads' names and records.",
    "er": "Matched automatically by name and contacts.",
    "organizations": "Suggested from email domains and contact cards.",
    "backbone": "Placed under your school or workplace: what it is (a class there, work there), whose email it "
                "involves, your dates there, or what you wrote about a role there.",
    "entities": "Found from what these records share (a folder, a series, a group chat).",
    "topic-normalization": "Merged automatically from similar topic names.",
    "relationship-history": "Inferred from who wrote to whom, and when.",
    "spine-quoted": "Inferred from the text the reply quotes.",
    "spine-inferred": "Inferred from the subject and timing.",
    "llm-extract": "Read from the text by a model.",
}


def origin(extractor: str) -> tuple[str, bool]:
    """(origin, stated) of an edge, from the extractor that made it."""
    from igneous.graph.verify import STATED

    if extractor == "user":
        return "user", True
    if extractor and extractor.startswith("agent:"):
        return extractor, False
    if extractor in STATED:
        return "stated", True
    return "derived", False


def _how(extractor: str, verdict: str | None) -> str:
    o, stated = origin(extractor)
    base = (f"Added by {extractor.split(':', 1)[1]}." if o.startswith("agent:")
            else _HOW.get(extractor, "Inferred automatically."))
    return base if stated or verdict else f"{base} Not checked yet."


DRAWN = ("derived", False)  # the provenance of a link the explorer draws from a ranking (a spoke from a centre)
READ = ("stated", True)     # ...and of one it reads straight off who sent and received what (a tie)


def drawn(src: int, dst: int, edge_type: str, weight: float = 1, provenance: tuple[str, bool] = DRAWN) -> dict:
    """A link the explorer draws on screen, with a stable negative id of its own and its (origin, stated)."""
    digest = hashlib.blake2b(f"{src}>{dst}>{edge_type}".encode(), digest_size=6).digest()
    return {"id": -int.from_bytes(digest, "big"), "src": src, "dst": dst, "type": edge_type, "weight": weight,
            "origin": provenance[0], "stated": provenance[1]}


def edge_view(row: dict, evidence: list[dict]) -> dict:
    """One edge as the Why view shows it: where it came from (`origin`, `stated`, and `how` in plain words), the
    verifier's verdict, reason and confidence when it has judged it, and the items it was derived from."""
    p = row.get("props") or {}
    o, stated = origin(row["extractor"])
    return {"id": row["id"], "type": row["type"], "extractor": row["extractor"], "origin": o, "stated": stated,
            "how": _how(row["extractor"], p.get("verdict")),
            "src": row["src"], "dst": row["dst"], "src_name": row.get("src_name"), "dst_name": row.get("dst_name"),
            "verdict": p.get("verdict"), "reason": p.get("reason"),
            # What the rule that made it saw ("62% of its conversations involve university.edu"), for unjudged links.
            "why": p["why"] if isinstance(p.get("why"), str) else "; ".join(p.get("why") or []) or None,
            "confidence": p.get("confidence", row.get("confidence")), "evidence": evidence}


def visible_edge(row: dict) -> bool:
    """A verifier 'drop' hides the edge from traversal and from the Why view."""
    return (row.get("props") or {}).get("verdict") != "drop"


def rejected(s: Session) -> set[frozenset[int]]:
    """visibility.rejected, kept per graph version."""
    return dirty.cached(s, ("rejected",), lambda: visibility.rejected(s))


# ---------------------------------------------------------------------------------------------------------------
# Relationships: who a person really talks to, and what two people share.
# ---------------------------------------------------------------------------------------------------------------

def relationships(s: Session, head: int, limit: int) -> list[tuple[int, float, dict]]:
    """Rank a person's correspondents by how real the relationship is, not raw volume: (person, strength, stats),
    strongest first, each person an identity head, never a service or the owner.

    A relationship needs both directions: someone who emailed once and got no reply (a newsletter, a recruiter blast)
    isn't one, unless the two share calendar events. Strength is mostly about now (recency-weighted messages, weeks in
    touch in the last six months, shared events), plus a little lifetime history, how many channels you use, and how
    small the conversations are (a message in a 12-person group chat is split across the others in it)."""
    rows = s.execute(text(f"""
        WITH me AS (SELECT unnest(CAST(:me AS bigint[])) AS id),
        mine AS MATERIALIZED (  -- the person's visible messages, and whether one of their identities sent each
            SELECT m.id AS m, bool_or(x.sent) AS i_sent, si.occurred_at AS at, si.source
            FROM (SELECT e.dst AS m, true AS sent FROM edges e WHERE e.src IN (SELECT id FROM me) AND e.type = 'SENT'
                  UNION ALL
                  SELECT e.src, false FROM edges e WHERE e.dst IN (SELECT id FROM me) AND e.type = 'RECEIVED') x
            JOIN nodes m ON m.id = x.m AND {visible('m')} JOIN source_items si ON si.id = m.source_item_id
            GROUP BY m.id, si.occurred_at, si.source),
        parts AS MATERIALIZED (  -- everyone on those messages, as identity heads
            SELECT mine.m, COALESCE(c.canonical_id, e.src) AS p, true AS sent
            FROM mine JOIN edges e ON e.dst = mine.m AND e.type = 'SENT' LEFT JOIN canonical c ON c.node_id = e.src
            UNION ALL
            SELECT mine.m, COALESCE(c.canonical_id, e.dst), false
            FROM mine JOIN edges e ON e.src = mine.m AND e.type = 'RECEIVED' LEFT JOIN canonical c ON c.node_id = e.dst),
        size AS (SELECT m, count(DISTINCT p) AS n FROM parts GROUP BY m),
        theirs AS (SELECT p, m, bool_or(sent) AS they_sent FROM parts WHERE p <> :head GROUP BY p, m),
        -- Shared events counted by occurrence, not by series node: a weekly meeting with someone is ~15 shared
        -- occurrences. The ATTENDED edge is one per series, so read each occurrence's attendees from its item.
        occ AS (
            SELECT si.id AS occurrence, lower(a->>'email') AS email, si.occurred_at AS at
            FROM source_items si, jsonb_array_elements(si.raw_meta->'attendees') a
            WHERE si.source = 'gcal' AND COALESCE(a->>'response', '') <> 'declined'
              AND si.occurred_at < now() + interval '60 days'),
        events AS (
            SELECT p, count(*) AS n, sum({_decay('at')}) AS recent
            FROM (SELECT DISTINCT COALESCE(c.canonical_id, p.id) AS p, o.occurrence, o.at
                  FROM occ mine JOIN occ o ON o.occurrence = mine.occurrence AND o.email <> mine.email
                  JOIN nodes p ON p.type = 'Person' AND p.key = o.email LEFT JOIN canonical c ON c.node_id = p.id
                  WHERE mine.email IN (SELECT key FROM nodes WHERE id IN (SELECT id FROM me))) shared
            GROUP BY 1)
        SELECT t.p, count(*) AS msgs,
               count(*) FILTER (WHERE t.they_sent) AS from_them, count(*) FILTER (WHERE mine.i_sent) AS from_me,
               count(DISTINCT date_trunc('week', mine.at)) AS weeks,
               count(DISTINCT mine.source) FILTER (WHERE sz.n <= 4) AS channels, COALESCE(max(ev.n), 0) AS events,
               sum({_decay('mine.at')} / GREATEST(1, sz.n - 1)) AS recent_msgs,
               sum(1.0 / GREATEST(1, sz.n - 1)) AS direct_msgs,
               -- Weeks in touch one-on-one or in a small group (4 or fewer people): a busy group chat would
               -- otherwise give every member the same "talked every week" credit.
               count(DISTINCT date_trunc('week', mine.at))
                   FILTER (WHERE mine.at > now() - interval '180 days' AND sz.n <= 4) AS recent_weeks,
               COALESCE(max(ev.recent), 0) AS recent_events, max(mine.at) AS last_at
        FROM theirs t JOIN mine USING (m) JOIN size sz USING (m) LEFT JOIN events ev ON ev.p = t.p
        JOIN nodes tp ON tp.id = t.p AND tp.type = 'Person' AND {shown('tp')} AND {not_owner('tp')}
        GROUP BY t.p
    """), {"me": identity.members(s, head), "head": head, "hl": HALF_LIFE_DAYS}).mappings().all()
    scored = []
    for r in rows:
        if not (r["from_them"] and r["from_me"]) and not r["events"]:
            continue  # one-directional: mail never answered, or a list you're on
        strength = (math.log1p(float(r["recent_msgs"])) * 2.0 + math.log1p(r["recent_weeks"]) * 1.5
                    + math.log1p(float(r["recent_events"])) * 1.2
                    + (r["channels"] + (1 if r["events"] else 0) - 1) * 0.6
                    + (math.log1p(float(r["direct_msgs"])) + math.log1p(r["weeks"])) * 0.25)
        scored.append((r["p"], round(strength, 3), dict(r)))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:limit]


def _shared(s: Session, focus: Iterable[int], people: Iterable[int]) -> dict[tuple[int, int], tuple[int, int | None]]:
    """For each person in `focus` and each other person in `people` (identity heads) who share visible messages:
    {(a, b) with a < b: (messages they share, the context that holds them)}. The context is the group chat or project
    that holds CONTAINED of their shared messages or more (Taylor and Sam only ever overlap on the CS 401 project):
    that is how they know each other. Starts from the focus people's own messages."""
    focus, people = sorted(set(focus)), sorted(set(people) | set(focus))
    if not focus or len(people) < 2:
        return {}
    rows = s.execute(text(f"""
        WITH ids AS (SELECT COALESCE(c.node_id, f) AS id, f FROM unnest(CAST(:focus AS bigint[])) f
                     LEFT JOIN canonical c ON c.canonical_id = f),
        msgs AS MATERIALIZED (
            SELECT DISTINCT x.f, x.m FROM (
                SELECT ids.f, e.dst AS m FROM ids JOIN edges e ON e.src = ids.id AND e.type = 'SENT'
                UNION ALL SELECT ids.f, e.src FROM ids JOIN edges e ON e.dst = ids.id AND e.type = 'RECEIVED') x
            JOIN nodes m ON m.id = x.m AND {visible('m')}),
        parts AS MATERIALIZED (
            SELECT msgs.f, msgs.m, COALESCE(c.canonical_id, e.src) AS p
            FROM msgs JOIN edges e ON e.dst = msgs.m AND e.type = 'SENT' LEFT JOIN canonical c ON c.node_id = e.src
            UNION
            SELECT msgs.f, msgs.m, COALESCE(c.canonical_id, e.dst)
            FROM msgs JOIN edges e ON e.src = msgs.m AND e.type = 'RECEIVED' LEFT JOIN canonical c ON c.node_id = e.dst),
        shared AS MATERIALIZED (
            SELECT DISTINCT LEAST(f, p) AS a, GREATEST(f, p) AS b, m FROM parts
            WHERE p <> f AND p = ANY(CAST(:people AS bigint[]))),
        tot AS (SELECT a, b, count(*) AS n FROM shared GROUP BY a, b),
        ctx AS (
            SELECT sh.a, sh.b, it.dst AS c, sh.m FROM shared sh JOIN edges it ON it.src = sh.m AND it.type = 'IN_THREAD'
            JOIN nodes g ON g.id = it.dst AND {group_conversation('g')}
            UNION
            SELECT sh.a, sh.b, pe.dst, sh.m FROM shared sh JOIN edges pe ON pe.src = sh.m AND pe.type = 'PART_OF'
            JOIN nodes pj ON pj.id = pe.dst AND pj.type = 'Project' AND {visible('pj')}
             AND (pj.props->>'umbrella') IS DISTINCT FROM 'true'),
        best AS (SELECT DISTINCT ON (a, b) a, b, c, count(*) AS n FROM ctx GROUP BY a, b, c
                 ORDER BY a, b, count(*) DESC, c)
        SELECT tot.a, tot.b, tot.n, CASE WHEN best.n >= :share * tot.n THEN best.c END
        FROM tot LEFT JOIN best USING (a, b)
    """), {"focus": focus, "people": people, "share": CONTAINED}).all()
    return {(a, b): (int(n), c) for a, b, n, c in rows}


# ---------------------------------------------------------------------------------------------------------------
# Why two things are linked.
# ---------------------------------------------------------------------------------------------------------------

def explain(s: Session, a: int, b: int, limit: int = 8, named: dict[str, str] | None = None) -> dict:
    """Why two nodes are connected: the conversations they share (message counts, dates, the latest excerpt), the
    events they both attended, their latest shared messages, the links between them (each with where it came from, its
    reason and evidence), or the shortest path when there are none, and the corrections you made to the pair. A person
    is all of their identities; a phone number reads as its contact (`named`, `names` by default). Raises
    identity.NotFound, and ValueError when a and b are one identity."""
    na, nb = identity.node(s, a), identity.node(s, b)
    if na["id"] == nb["id"]:
        raise ValueError("Those are the same node: pick two different things.")
    ids_a, ids_b = identity.members(s, na["id"]), identity.members(s, nb["id"])
    named = names(s) if named is None else named
    convo = s.execute(text(f"""
        WITH a AS (SELECT e.dst AS m FROM edges e WHERE e.src = ANY(:a) AND e.type = 'SENT'
                   UNION SELECT e.src FROM edges e WHERE e.dst = ANY(:a) AND e.type = 'RECEIVED'),
        b AS (SELECT e.dst AS m FROM edges e WHERE e.src = ANY(:b) AND e.type = 'SENT'
              UNION SELECT e.src FROM edges e WHERE e.dst = ANY(:b) AND e.type = 'RECEIVED'),
        shared AS (SELECT a.m FROM a JOIN b USING (m) JOIN nodes msg ON msg.id = a.m AND {visible('msg')})
        SELECT t.id AS thread_id, t.name AS thread, {visible('t')} AS shown, count(*) AS messages,
               min(si.occurred_at) AS first, max(si.occurred_at) AS last,
               (array_agg(si.source ORDER BY si.occurred_at DESC))[1] AS source,
               (array_agg(left(si.body_text, 220) ORDER BY si.occurred_at DESC))[1] AS excerpt
        FROM shared JOIN nodes msg ON msg.id = shared.m JOIN source_items si ON si.id = msg.source_item_id
        JOIN edges it ON it.src = shared.m AND it.type = 'IN_THREAD' JOIN nodes t ON t.id = it.dst
        GROUP BY t.id, t.name, t.props ORDER BY count(*) DESC, t.id LIMIT :lim
    """), {"a": ids_a, "b": ids_b, "lim": limit}).mappings().all()
    people = dict(s.execute(text("""
        SELECT t, count(DISTINCT p) FROM (
            SELECT it.dst AS t, COALESCE(c.canonical_id, e.src) AS p FROM edges it
            JOIN edges e ON e.dst = it.src AND e.type = 'SENT' LEFT JOIN canonical c ON c.node_id = e.src
            WHERE it.dst = ANY(:t) AND it.type = 'IN_THREAD'
            UNION ALL
            SELECT it.dst, COALESCE(c.canonical_id, e.dst) FROM edges it
            JOIN edges e ON e.src = it.src AND e.type = 'RECEIVED' LEFT JOIN canonical c ON c.node_id = e.dst
            WHERE it.dst = ANY(:t) AND it.type = 'IN_THREAD') x
        GROUP BY t
    """), {"t": [c["thread_id"] for c in convo]}).all()) if convo else {}
    events = s.execute(text(f"""
        SELECT e.id, e.name, si.occurred_at AS at
        FROM edges x JOIN edges y ON x.dst = y.dst AND x.type = 'ATTENDED' AND y.type = 'ATTENDED'
        JOIN nodes e ON e.id = x.dst AND {visible('e')} LEFT JOIN source_items si ON si.id = e.source_item_id
        WHERE x.src = ANY(:a) AND y.src = ANY(:b) GROUP BY e.id, e.name, si.occurred_at
        ORDER BY si.occurred_at DESC NULLS LAST, e.id LIMIT :lim
    """), {"a": ids_a, "b": ids_b, "lim": limit}).mappings().all()
    total = sum(c["messages"] for c in convo)
    conversations = [{
        # A conversation that isn't a group (a one-on-one thread) is the relationship itself, not a node to open.
        "thread_id": c["thread_id"] if c["shown"] else None, "thread": label(c["thread"], "Thread", named),
        "messages": c["messages"], "people": people.get(c["thread_id"], 0), "first": c["first"], "last": c["last"],
        "source": c["source"], "excerpt": name_handles(c["excerpt"], named)} for c in convo]
    links = connection(s, ids_a, ids_b, named, allow_path=total == 0 and not events)
    from igneous.graph.corrections import for_pair
    return {"a": {"id": na["id"], "name": label(na["name"], na["type"], named)},
            "b": {"id": nb["id"], "name": label(nb["name"], nb["type"], named)},
            "messages": total, "conversations": conversations, "events": [dict(e) for e in events],
            "shared": shared_items(s, ids_a, ids_b, named) if total else [],
            "corrections": for_pair(s, ids_a, ids_b), **links}


def shortest_path(adj: dict[int, list[tuple[int, int]]], start: set[int], goal: set[int],
                  max_hops: int = PATH_MAX_HOPS) -> list[tuple[int, int, int]] | None:
    """BFS over {node: [(neighbour, edge_id)]}: the hops [(from, to, edge_id)] of a shortest path of at most
    `max_hops`, or None. Pure so it can be tested without a database."""
    prev: dict[int, tuple[int, int] | None] = {n: None for n in start}
    frontier = list(start)
    for _ in range(max_hops):
        nxt = []
        for n in frontier:
            for m, eid in adj.get(n, ()):
                if m in prev:
                    continue
                prev[m] = (n, eid)
                if m in goal:
                    hops, cur = [], m
                    while prev[cur] is not None:
                        f, e = prev[cur]
                        hops.append((f, cur, e))
                        cur = f
                    return hops[::-1]
                nxt.append(m)
        frontier = nxt
    return None


def _evidence(s: Session, edge_ids: list[int], named: dict[str, str]) -> dict[int, list[dict]]:
    """edge id -> its evidence items (edge_evidence rows plus the edge's own source item), newest first."""
    from igneous.items import item_card
    rows = s.execute(text("""
        SELECT x.edge_id, x.kind, si.id, si.source, si.external_id, si.title, si.occurred_at, si.raw_meta,
               left(si.body_text, 600) AS body_text
        FROM (SELECT edge_id, source_item_id, kind FROM edge_evidence WHERE edge_id = ANY(:e)
              UNION SELECT id, source_item_id, 'source' FROM edges WHERE id = ANY(:e) AND source_item_id IS NOT NULL) x
        JOIN source_items si ON si.id = x.source_item_id
        ORDER BY si.occurred_at DESC NULLS LAST, si.id
    """), {"e": edge_ids}).mappings().all()
    out: dict[int, list[dict]] = {}
    for r in rows:
        bucket = out.setdefault(r["edge_id"], [])
        if len(bucket) >= EDGE_EVIDENCE_LIMIT or any(c["id"] == r["id"] for c in bucket):
            continue
        card = item_card(dict(r), named)
        card["kind"] = r["kind"]
        bucket.append(card)
    return out


def shared_items(s: Session, ids_a: list[int], ids_b: list[int], named: dict[str, str], limit: int = 5) -> list[dict]:
    """The latest visible messages both sides took part in, as openable item cards."""
    from igneous.items import CARD_COLS, item_card
    rows = s.execute(text(f"""
        WITH a AS (SELECT e.dst AS m FROM edges e WHERE e.src = ANY(:a) AND e.type = 'SENT'
                   UNION SELECT e.src FROM edges e WHERE e.dst = ANY(:a) AND e.type = 'RECEIVED'),
        b AS (SELECT e.dst AS m FROM edges e WHERE e.src = ANY(:b) AND e.type = 'SENT'
              UNION SELECT e.src FROM edges e WHERE e.dst = ANY(:b) AND e.type = 'RECEIVED')
        SELECT {CARD_COLS} FROM a JOIN b USING (m) JOIN nodes msg ON msg.id = a.m AND {visible('msg')}
        JOIN source_items si ON si.id = msg.source_item_id
        ORDER BY si.occurred_at DESC NULLS LAST, si.id DESC LIMIT :lim
    """), {"a": ids_a, "b": ids_b, "lim": limit}).mappings().all()
    return [item_card(dict(r), named) for r in rows]


def connection(s: Session, ids_a: list[int], ids_b: list[int], named: dict[str, str], allow_path: bool = True) -> dict:
    """Works for any pair: `direct` = edges between the two (all their identities), each with its reason and
    evidence items; when there are none, `path` = the shortest route through shown nodes (at most 3 hops) with each
    hop's evidence. Each end of an edge or a hop is shown as its identity head."""
    edge_cols = "e.id, e.src, e.dst, e.type, e.extractor, e.props, e.confidence"

    def views(rows: list[dict], hops: list[tuple[int, int]] | None = None) -> list[dict]:
        ends = identity.heads(s, [x for r in rows for x in (r["src"], r["dst"])])
        about = {i: (n, t) for i, n, t in s.execute(text("SELECT id, name, type FROM nodes WHERE id = ANY(:i)"),
                                                     {"i": sorted(set(ends.values()))})}
        ev = _evidence(s, [r["id"] for r in rows], named)
        out = []
        for i, r in enumerate(rows):
            a, b = ends[r["src"]], ends[r["dst"]]
            v = edge_view({**r, "src": a, "dst": b, "src_name": label(*about[a], named),
                           "dst_name": label(*about[b], named)}, ev.get(r["id"], []))
            if hops is not None:
                f, t = hops[i]
                v.update({"from": ends[f], "to": ends[t]})
            out.append(v)
        return out

    rows = [dict(r) for r in s.execute(text(f"""
        SELECT {edge_cols} FROM edges e
        WHERE (e.src = ANY(:a) AND e.dst = ANY(:b)) OR (e.src = ANY(:b) AND e.dst = ANY(:a))
        ORDER BY e.confidence DESC NULLS LAST, e.id LIMIT 40
    """), {"a": ids_a, "b": ids_b}).mappings()]
    rows = [r for r in rows if visible_edge(r)]
    if rows:
        return {"direct": views(rows), "path": None}
    if not allow_path:
        return {"direct": [], "path": None}

    adj: dict[int, list[tuple[int, int]]] = {}
    seen, frontier, edges = set(ids_a), list(ids_a), {}
    for _ in range(PATH_MAX_HOPS):
        if not frontier:
            break
        # One step out from the frontier, through nodes that may be shown and aren't hubs (a mass mail, a big org).
        layer = s.execute(text(f"""
            WITH step AS (
                SELECT e.*, CASE WHEN e.src = ANY(:f) THEN e.dst ELSE e.src END AS other FROM edges e
                WHERE (e.src = ANY(:f) OR e.dst = ANY(:f)) AND e.type NOT IN ('HAS_CHUNK', 'SAME_AS'))
            SELECT {edge_cols} FROM step e JOIN nodes o ON o.id = e.other
            WHERE e.other = ANY(:goal)
               OR ({shown('o')} AND (SELECT count(*) FROM (SELECT 1 FROM edges x WHERE x.src = e.other OR x.dst = e.other
                                                          LIMIT :fan + 1) d) <= :fan)
            ORDER BY e.id LIMIT 4000
        """), {"f": frontier, "goal": ids_b, "fan": PATH_FANOUT}).mappings().all()
        nxt = []
        for r in map(dict, layer):
            if not visible_edge(r) or r["id"] in edges:
                continue
            edges[r["id"]] = r
            adj.setdefault(r["src"], []).append((r["dst"], r["id"]))
            adj.setdefault(r["dst"], []).append((r["src"], r["id"]))
            nxt += [n for n in (r["src"], r["dst"]) if n not in seen]
        seen.update(nxt)
        frontier = list(dict.fromkeys(nxt))
    hops = shortest_path(adj, set(ids_a), set(ids_b))
    if not hops:
        return {"direct": [], "path": None}
    return {"direct": [], "path": views([edges[e] for _f, _t, e in hops], [(f, t) for f, t, _e in hops])}


# ---------------------------------------------------------------------------------------------------------------
# The opening view: a centre, the most meaningful things around it, what is part of what, and the ties among them.
# ---------------------------------------------------------------------------------------------------------------

def scored_members(s: Session, head: int) -> dict[str, list[dict]]:
    """Everything related to a centre, scored by how strongly, by section (People, Groups, Events, Organizations, the
    sections projects' kinds name, Places), strongest first. One scale for all: 0..1. Kept per graph version."""
    return dirty.cached(s, ("members", head), lambda: _scored_members(s, head))


def _scored_members(s: Session, head: int) -> dict[str, list[dict]]:
    me = identity.members(s, head)
    out: dict[str, list[dict]] = {}

    # People: real (two-way) relationships, already scored.
    people = [{"id": p, "strength": st} for p, st, _ in relationships(s, head, 200)]
    if people:
        out["People"] = people

    # Groups: the group conversations the centre writes in or hears in (2+ of its messages), recent activity first.
    groups = s.execute(text(f"""
        WITH mine AS (SELECT DISTINCT m.id AS m, si.occurred_at AS at FROM (
                          SELECT dst AS m FROM edges WHERE src = ANY(:me) AND type = 'SENT'
                          UNION ALL SELECT src FROM edges WHERE dst = ANY(:me) AND type = 'RECEIVED') x
                      JOIN nodes m ON m.id = x.m AND {visible('m')} LEFT JOIN source_items si ON si.id = m.source_item_id),
        g AS (SELECT it.dst AS t, count(*) AS msgs, sum({_decay('mine.at')}) AS recent
              FROM mine JOIN edges it ON it.src = mine.m AND it.type = 'IN_THREAD'
              JOIN nodes t ON t.id = it.dst AND {group_conversation('t')}
              GROUP BY it.dst HAVING count(*) >= 2)
        SELECT g.t, g.msgs, COALESCE(g.recent, 0),
               (SELECT count(DISTINCT COALESCE(c.canonical_id, x.src)) FROM edges it2
                JOIN edges x ON x.dst = it2.src AND x.type = 'SENT' LEFT JOIN canonical c ON c.node_id = x.src
                WHERE it2.dst = g.t AND it2.type = 'IN_THREAD') AS people
        FROM g
    """), {"me": me, "hl": HALF_LIFE_DAYS}).all()
    # A group you're active in now outranks a big chat that went quiet; lifetime volume only adds a little.
    gl = [{"id": t, "strength": math.log1p(float(recent)) * 2 + math.log1p(n) * 0.5 + math.log1p(msgs) * 0.25}
          for t, msgs, recent, n in groups]
    if gl:
        out["Groups"] = gl

    # Events: calendar series the centre attends, by occurrences (a weekly meeting outranks a one-off).
    events = s.execute(text(f"""
        SELECT id, sum({_decay('at')}) AS occ FROM (
        SELECT DISTINCT n.id, si.id AS occurrence, si.occurred_at AS at
        FROM source_items si
        CROSS JOIN LATERAL jsonb_array_elements(si.raw_meta->'attendees') a
        JOIN nodes n ON n.type = 'Event' AND n.key = COALESCE(si.raw_meta->>'event_key',
                                                               'gcal:' || COALESCE(si.raw_meta->>'series_key', si.external_id))
             AND {visible('n')}
        WHERE si.source = 'gcal' AND lower(a->>'email') IN (SELECT key FROM nodes WHERE id = ANY(:me))
          AND COALESCE(a->>'response','') <> 'declined'
          AND si.occurred_at < now() + interval '60 days'  -- far-future instances of a series aren't "now"
        ) mine GROUP BY id
    """), {"me": me, "hl": HALF_LIFE_DAYS}).all()
    el = [{"id": e, "strength": math.log1p(float(occ))} for e, occ in events]
    if el:
        out["Events"] = el

    # Organizations: where the centre's people work, weighted by how close each person is.
    if people:
        orgs = s.execute(text(f"""
            WITH p AS (SELECT unnest(CAST(:ids AS bigint[])) AS head, unnest(CAST(:ws AS float8[])) AS w),
            ids AS (SELECT p.head, COALESCE(c.node_id, p.head) AS id, p.w FROM p
                    LEFT JOIN canonical c ON c.canonical_id = p.head),
            worked AS (SELECT DISTINCT ids.head, ids.w, e.dst AS org FROM ids
                       JOIN edges e ON e.src = ids.id AND e.type = 'WORKED_AT')
            SELECT o.id, sum(worked.w) AS w, count(*) AS n
            FROM worked JOIN nodes o ON o.id = worked.org AND o.type = 'Organization' AND {visible('o')}
            WHERE o.key <> ALL(:personal)
            GROUP BY o.id
        """), {"ids": [p["id"] for p in people], "ws": [p["strength"] for p in people],
               "personal": sorted(PERSONAL_DOMAINS)}).all()
        ol = [{"id": o, "strength": float(w) + math.log1p(n)} for o, w, n in orgs]
        if ol:
            out["Organizations"] = ol

    # Projects (derived by graph/entities.py and graph/threads.py): the ones the centre's records are part of, by how
    # many and how recent. A record counts directly or through the conversation it is in.
    projects = s.execute(text(f"""
        WITH mine AS (SELECT dst AS i FROM edges WHERE src = ANY(:me) AND type IN ('SENT', 'ATTENDED')
                      UNION SELECT src FROM edges WHERE dst = ANY(:me) AND type = 'RECEIVED'),
        via AS (SELECT pe.dst AS p, mine.i FROM mine JOIN edges pe ON pe.src = mine.i AND pe.type = 'PART_OF'
                UNION SELECT pe.dst, mine.i FROM mine JOIN edges it ON it.src = mine.i AND it.type = 'IN_THREAD'
                      JOIN edges pe ON pe.src = it.dst AND pe.type = 'PART_OF')
        SELECT via.p, n.props->>'kind' AS kind, count(*) AS records,
               (SELECT count(*) FROM edges x WHERE x.dst = via.p AND x.type = 'INVOLVED_IN') AS people,
               COALESCE(sum({_decay('si.occurred_at')}), 0) AS recent
        FROM via JOIN nodes n ON n.id = via.p AND n.type = 'Project' AND {visible('n')}
        JOIN nodes item ON item.id = via.i LEFT JOIN source_items si ON si.id = item.source_item_id
        GROUP BY via.p, n.props->>'kind'
    """), {"me": me, "hl": HALF_LIFE_DAYS}).all()
    for p, kind, n, members, recent in projects:
        # Each project lands in the section its kind names: a judged thread says what it is ("trip", "course",
        # "TA job"), so sections come from the data (Trips, Courses, Work, ...) rather than a fixed list. Mostly about
        # now: a one-time hangout years ago fades like an old friendship does.
        out.setdefault(section_for_kind(kind), []).append(
            {"id": p, "strength": math.log1p(float(recent)) * 2
                + (math.log1p(n) * 0.25 + 0.5 * math.log1p(members)) * min(1.0, float(recent) / max(1.0, n * 0.05))})

    # Places (Google Takeout Maps): where you actually go, by visit count and distinct days; saved places count too.
    if s.execute(text("SELECT EXISTS (SELECT 1 FROM nodes WHERE id = ANY(:me) AND props->>'self' = 'true')"),
                 {"me": me}).scalar():
        places = s.execute(text(f"""
            WITH v AS (
                SELECT x->>'key' AS key, CAST(x->>'start' AS timestamptz) AS at
                FROM source_items si CROSS JOIN LATERAL jsonb_array_elements(si.raw_meta->'visits') x
                WHERE si.source = 'takeout_maps' AND si.raw_meta->>'kind' = 'visits'),
            seen AS (SELECT n.id, count(*) AS visits, count(DISTINCT CAST(v.at AS date)) AS days
                     FROM v JOIN nodes n ON n.type = 'Place' AND n.key = v.key AND {visible('n')} GROUP BY n.id),
            saved AS (SELECT DISTINCT e.dst AS id FROM edges e JOIN nodes n ON n.id = e.dst AND {visible('n')}
                      WHERE e.src = ANY(:me) AND e.type = 'SAVED')
            SELECT COALESCE(seen.id, saved.id), COALESCE(seen.visits, 0), COALESCE(seen.days, 0),
                   (saved.id IS NOT NULL) AS is_saved
            FROM seen FULL JOIN saved ON saved.id = seen.id
            WHERE saved.id IS NOT NULL OR seen.days >= 2
        """), {"me": me}).all()
        pl = [{"id": p, "strength": math.log1p(v) + math.log1p(d) + (1.0 if sv else 0.0)} for p, v, d, sv in places]
        if pl:
            out["Places"] = pl

    # One scale for everything: each item's raw score divided by the strongest raw score in its section gives 0..1
    # within the section; multiplying by how strong that section's best item is relative to the overall best keeps a
    # section of stale things (a class from two years ago) from claiming the top spots.
    tops = {c: max((it["strength"] for it in items), default=0) for c, items in out.items()}
    overall = max(tops.values(), default=0) or 1
    for c, items in out.items():
        items.sort(key=lambda x: (-x["strength"], x["id"]))
        for it in items:
            it["strength"] = round(it["strength"] / (tops[c] or 1) * (0.4 + 0.6 * tops[c] / overall), 3)
    return out


def your_orgs(s: Session, me: list[int]) -> list[tuple[int, bool, object]]:
    """Where you work and study, each organization once: (org, school, until), current ones first. `me` is your
    identities."""
    return [tuple(r) for r in s.execute(text(f"""
        SELECT org, school, valid_to FROM (
            SELECT DISTINCT ON (e.dst) e.dst AS org, (e.props->>'school') IS NOT NULL AS school, e.valid_to
            FROM edges e JOIN nodes o ON o.id = e.dst AND o.type = 'Organization' AND {visible('o')}
            WHERE e.src = ANY(:me) AND e.type = 'WORKED_AT' AND e.extractor = ANY(:yours)
            ORDER BY e.dst, e.valid_to IS NULL DESC, e.valid_to DESC, e.id) o
        ORDER BY valid_to IS NULL DESC, valid_to DESC, org
    """), {"me": me, "yours": list(YOUR_ORGS)})]


def empty_view(s: Session) -> dict:
    """The answer when there is no one to open on. `reason`: 'nothing_built' while the graph holds nothing yet (no
    source connected, or Rebuild not run), 'no_owner' when it does but no source says which person is you, or nothing
    links you to anyone yet."""
    built = s.execute(text("SELECT EXISTS (SELECT 1 FROM nodes WHERE NOT (type = 'Person' AND key = :owner))"),
                      {"owner": identity.OWNER_KEY}).scalar()
    return {"seed": None, "nodes": [], "edges": [], "empty": True, "reason": "no_owner" if built else "nothing_built"}


def view(s: Session, seed: int | None = None, per: int = 6) -> dict:
    """The opening view for a centre (default: you): your orgs, then the strongest people, groups and sustained
    projects, each linked to the centre, with what is part of what and the ties among them. With no seed and no one
    to open on (no owner, or nothing linked to you), `empty_view`. Raises identity.NotFound for an unknown seed."""
    if seed is None:
        owner = identity.self_id(s)
        if owner is None:
            return empty_view(s)
        g = view(s, owner, per)
        return g if len(g["nodes"]) > 1 else empty_view(s)
    head = identity.node(s, seed)["id"]
    return dirty.cached(s, ("view", head, per), lambda: _view(s, head, per))


def _view(s: Session, head: int, per: int) -> dict:
    me = identity.members(s, head)
    cats = dict(scored_members(s, head))
    # Where you work and study: always part of your graph, at the top.
    life = your_orgs(s, me)
    orgs = {o for o, _sch, _to in life}
    if life:
        cats["Organizations"] = [{"id": o, "strength": 1.0 if to is None else 0.9} for o, _sch, to in life] + \
            [m for m in cats.get("Organizations", []) if m["id"] not in orgs]
    # Sections by how much they matter: the summed strength of their top members.
    order = sorted(cats, key=lambda c: (-sum(m["strength"] for m in cats[c][:10]), c))
    ranked = sorted(((m, c) for c in order for m in cats[c]), key=lambda mc: -mc[0]["strength"])
    pinned = set(s.execute(text("""
        SELECT n.id FROM nodes n JOIN thread_registry r ON r.key = n.key
        WHERE n.type = 'Project' AND r.pinned AND r.status = 'active'""")).scalars())
    sustained = set(s.execute(text("""
        SELECT id FROM nodes WHERE type = 'Project' AND COALESCE((props->>'records')::int, 0) >= 10
          AND (props->>'end')::timestamptz - (props->>'start')::timestamptz >= interval '21 days'""")).scalars())
    # The first ring is the most meaningful things in your life, not the top item of every kind: your orgs; then the
    # strongest people and things overall, where a project must be sustained (weeks of activity, many of your own
    # records) or pinned: a single dinner or a one-off flight is reachable by expanding, never up front.
    size = per * 3
    meaningful = [mc for mc in ranked
                  if mc[0]["id"] in orgs | pinned or mc[1] in ("People", "Groups")
                  or (mc[0]["id"] in sustained and mc[1] != "Events")]
    backbone = [mc for mc in meaningful if mc[0]["id"] in orgs]
    rest = [mc for mc in meaningful if mc[0]["id"] not in orgs]
    people = [mc for mc in rest if mc[1] == "People"][:size // 2]
    things = [mc for mc in rest if mc[1] != "People"][:max(size // 2, size - len(people))]
    first = backbone + sorted(people + things, key=lambda mc: -mc[0]["strength"])
    # An org from your resume stands for its own thread (graph/backbone.py): show it once, as the org.
    stands = set(s.execute(text("""SELECT (props->>'thread')::bigint FROM nodes
                                   WHERE type = 'Organization' AND props ? 'thread'""")).scalars())
    blocked = rejected(s)
    first = [(m, c) for m, c in first if m["id"] not in stands and frozenset((head, m["id"])) not in blocked]
    named = names(s)
    nodes = _nodes(s, [head, *(m["id"] for m, _c in first)], named, keep={head})
    on = {n["id"] for n in nodes}
    strength = {m["id"]: (m["strength"], c) for m, c in first}
    for n in nodes:
        st, cat = strength.get(n["id"], (1.0, None))  # the centre is drawn as the centre, in no section
        n.update(strength=st, category=cat, kind="seed" if n["id"] == head else "entity", expandable=n["id"] != head)
    ring = [m["id"] for m, _c in first if m["id"] in on]
    edges = [drawn(head, m["id"], "RELATED", 1 + 4 * m["strength"]) for m, _c in first if m["id"] in on]
    edges += _structure(s, ring, blocked) + _ties(s, ring, ring, blocked, per_node=2)
    return {"seed": head, "nodes": nodes, "edges": edges, "empty": False}


def _nodes(s: Session, ids: Iterable[int], named: dict[str, str], keep: set[int] = frozenset()) -> list[dict]:
    """The nodes the explorer draws for `ids` (identity heads), in order, each once: only what may be shown (no hidden
    node, no service) unless it is in `keep` (the centre you asked for)."""
    ids = list(dict.fromkeys(ids))
    rows = {r["id"]: r for r in s.execute(text(f"""
        SELECT n.id, n.type, n.name, n.key, n.props->>'kind' AS kind, {shown('n')} AS shown FROM nodes n
        WHERE n.id = ANY(:ids)"""), {"ids": ids}).mappings()}
    return [{"id": i, "type": r["type"], "name": label(r["name"], r["type"], named), "key": r["key"],
             "category": category(r["type"], r["kind"])}
            for i in ids if (r := rows.get(i)) is not None and (r["shown"] or i in keep)]


def _structure(s: Session, ids: list[int], blocked: set[frozenset[int]]) -> list[dict]:
    """What is part of what among the nodes on screen (Project Atlas in the Lab, the Lab in State University), as
    stored. An org that stands for a thread carries that thread's links."""
    alias = dict(s.execute(text("""SELECT (props->>'thread')::bigint, id FROM nodes
                                   WHERE id = ANY(:i) AND type = 'Organization' AND props ? 'thread'"""), {"i": ids}).all())
    under = list(set(ids) | set(alias))
    rows = s.execute(text("""
        SELECT e.src, e.dst, e.extractor FROM edges e JOIN nodes c ON c.id = e.src AND c.type IN ('Project', 'Organization')
        WHERE e.type = 'PART_OF' AND e.src = ANY(:i) AND e.dst = ANY(:i) ORDER BY e.id
    """), {"i": under}).all()
    out, seen = [], set()
    for a, b, extractor in rows:
        a, b = alias.get(a, a), alias.get(b, b)
        if a != b and (a, b) not in seen and frozenset((a, b)) not in blocked:
            seen.add((a, b))
            out.append(drawn(a, b, "PART_OF", 3, origin(extractor)))
    return out


def _ties(s: Session, ids: list[int], focus: list[int], blocked: set[frozenset[int]], per_node: int = 2) -> list[dict]:
    """The strongest relationships among nodes on screen that involve one in `focus`: people who share messages
    (CORRESPONDS, weighted by how many), people in a group chat or event on screen (PART_OF). Two people who only
    overlap inside one project or group chat know each other through it, so their line goes through that context, not
    straight between them. Only each node's strongest `per_node`, so the picture stays readable."""
    types = dict(s.execute(text("SELECT id, type FROM nodes WHERE id = ANY(:i)"), {"i": ids}).all())
    people = [i for i in ids if types.get(i) == "Person"]
    hubs = [i for i in ids if types.get(i) in ("Thread", "Event")]
    focus_people = [i for i in focus if types.get(i) == "Person"]
    focus_hubs = [i for i in focus if types.get(i) in ("Thread", "Event")]
    edges = [drawn(a, b, "CORRESPONDS", n, READ) for (a, b), (n, ctx) in _shared(s, focus_people, people).items()
             if ctx is None and frozenset((a, b)) not in blocked]
    if people and hubs and (focus_people or focus_hubs):
        # Who is in each group chat or event on screen: from the hub's own records, one indexed step each way.
        member_of = s.execute(text(f"""
            WITH hub_msgs AS (SELECT it.dst AS hub, it.src AS m FROM edges it JOIN nodes m ON m.id = it.src AND {visible('m')}
                              WHERE it.dst = ANY(:hubs) AND it.type = 'IN_THREAD'),
            parts AS (SELECT h.hub, e.src AS id FROM hub_msgs h JOIN edges e ON e.dst = h.m AND e.type = 'SENT'
                      UNION SELECT h.hub, e.dst FROM hub_msgs h JOIN edges e ON e.src = h.m AND e.type = 'RECEIVED'
                      UNION SELECT e.dst, e.src FROM edges e WHERE e.dst = ANY(:hubs) AND e.type = 'ATTENDED')
            SELECT DISTINCT COALESCE(c.canonical_id, parts.id) AS p, parts.hub FROM parts
            LEFT JOIN canonical c ON c.node_id = parts.id
            WHERE COALESCE(c.canonical_id, parts.id) = ANY(:people)
        """), {"people": people, "hubs": hubs}).all()
        edges += [drawn(p, h, "PART_OF", 1, READ) for p, h in member_of
                  if (p in focus_people or h in focus_hubs) and frozenset((p, h)) not in blocked]
    by: dict[int, list[dict]] = {}
    for e in edges:
        by.setdefault(e["src"], []).append(e)
        by.setdefault(e["dst"], []).append(e)
    keep = {id(e) for es in by.values() for e in sorted(es, key=lambda e: -e["weight"])[:per_node]}
    return [e for e in edges if id(e) in keep]


# ---------------------------------------------------------------------------------------------------------------
# Opening a node: what it is made of.
# ---------------------------------------------------------------------------------------------------------------

def person_world(s: Session, pid: int) -> list[tuple[int, str, float]]:
    """What a person is part of, strongest and most recent first: (node, type, weight). The group chats they speak
    in, the projects they're checked to be in, the events they attend and the files they wrote."""
    return [(r[0], r[1], float(r[2])) for r in s.execute(text(f"""
        WITH theirs AS (
            SELECT DISTINCT m.id AS m, {_decay('si.occurred_at')} AS w FROM (
                SELECT dst AS m FROM edges WHERE src = ANY(:ids) AND type = 'SENT'
                UNION ALL SELECT src FROM edges WHERE dst = ANY(:ids) AND type = 'RECEIVED') x
            JOIN nodes m ON m.id = x.m AND {visible('m')} LEFT JOIN source_items si ON si.id = m.source_item_id)
        -- Only group chats: one-on-one messages and email threads with them ARE the relationship (they're in the
        -- person's sources and closeness), not separate things beside them.
        SELECT it.dst, 'Thread', sum(t.w)
        FROM theirs t JOIN edges it ON it.src = t.m AND it.type = 'IN_THREAD'
        JOIN nodes g ON g.id = it.dst AND {group_conversation('g')}
        GROUP BY it.dst
        UNION ALL  -- projects and life threads, once checked (or stated by you): no unverified guess as a fact
        SELECT e.dst, 'Project', 1 + COALESCE((e.props->>'items')::float, 1)
        FROM edges e JOIN nodes p ON p.id = e.dst AND p.type = 'Project' AND {visible('p')}
        WHERE e.src = ANY(:ids) AND e.type = 'INVOLVED_IN'
          AND (e.props->>'verdict' = 'keep' OR e.extractor IN ('user', 'profile', 'contacts'))
        UNION ALL
        SELECT e.dst, 'Event', 1 FROM edges e JOIN nodes ev ON ev.id = e.dst AND ev.type = 'Event' AND {visible('ev')}
        WHERE e.src = ANY(:ids) AND e.type = 'ATTENDED'
        UNION ALL
        SELECT e.dst, 'File', 1 FROM edges e JOIN nodes f ON f.id = e.dst AND f.type = 'File' AND {visible('f')}
        WHERE e.src = ANY(:ids) AND e.type = 'AUTHORED'
        ORDER BY 3 DESC, 1 LIMIT 60
    """), {"ids": identity.members(s, pid), "hl": HALF_LIFE_DAYS})]


def earned_cut(scored: list[tuple[int, float]], lo: int = 1, hi: int = 12) -> list[int]:
    """Keep the strong ones: sort by score and cut at the largest relative drop (a project with two real children
    shows two, one with ten shows ten), between lo and hi."""
    scored = sorted(scored, key=lambda x: -x[1])
    # Many children of similar size (a university's classes): no natural cut, so show the top `hi` rather than
    # stop at the first small gap among the biggest few.
    lo = max(lo, min(hi, len(scored) // 4))
    if len(scored) <= lo:
        return [i for i, _ in scored]
    cut, best = len(scored[:hi]), 0.0
    for i in range(lo, min(len(scored), hi)):
        prev, cur = scored[i - 1][1], scored[i][1]
        drop = (prev - cur) / prev if prev > 0 else 0
        if drop > best and drop >= 0.4:
            cut, best = i, drop
    return [i for i, _ in scored[:cut]]


def _core_people(people: list[tuple[int, float]], hi: int = 10, keep: set[int] = frozenset()) -> list[int]:
    """A project's people, without the ones who only passed by: measured against its most active few. `keep`
    (members of its own group chats) is direct evidence and always stays."""
    from igneous.graph.threads import relative_members
    real = relative_members([(p, int(w)) for p, w in people], keep)
    # Who's in it was decided above; the strongest `hi` of them show (no second cut at the biggest gap, which
    # dropped a trip's travellers because one of them texted about it four times as much).
    return [p for p, _w in sorted(people, key=lambda x: -x[1]) if p in real][:hi]


def children(s: Session, node: int, ntype: str) -> list[int]:
    """A project's or org's children, the bigger picture first: the projects under it, then (for a project) its
    checked people, its main group conversations and its key documents; (for an org) the people there. Code files and
    one-off notices are detail that lives inside a project, not next to it."""
    hidden, own = s.execute(text(f"""SELECT NOT {visible('n')}, (n.props->>'thread')::bigint
                                     FROM nodes n WHERE n.id = :n"""), {"n": node}).one()
    if hidden:
        return []
    if ntype == "Organization" and own:
        # An org from your resume stands for its thread (graph/backbone.py): it opens to what's under it.
        # A child that is itself an org's thread (the Lab under State University) is shown as that org.
        stands = dict(s.execute(text("""SELECT (props->>'thread')::bigint, id FROM nodes
                                        WHERE type = 'Organization' AND props ? 'thread'""")).all())
        out = [stands.get(c, c) for c in children(s, own, "Project")]
        return [c for c in dict.fromkeys(out) if c != node]
    # Projects under it: the ones you stated (or your resume did) always show; others earn their place by size.
    stated = s.execute(text(f"""
        SELECT DISTINCT e.src FROM edges e JOIN nodes c ON c.id = e.src AND c.type = 'Project' AND {visible('c')}
        WHERE e.dst = :n AND e.type = 'PART_OF' AND e.extractor IN ('user', 'profile') ORDER BY e.src
    """), {"n": node}).scalars().all()
    subs = s.execute(text(f"""
        SELECT c.id, COALESCE((c.props->>'records')::float, 1) FROM edges e
        JOIN nodes c ON c.id = e.src AND c.type = 'Project' AND {visible('c')}
        WHERE e.dst = :n AND e.type = 'PART_OF' AND e.extractor NOT IN ('user', 'profile')
        UNION
        SELECT c.id, COALESCE((c.props->>'records')::float, 1) FROM edges e
        JOIN nodes c ON c.id = e.dst AND c.type = 'Project' AND {visible('c')}
        WHERE e.src = :n AND e.type = 'RELATED_TO' AND e.extractor = 'thread-resolution'
    """), {"n": node}).all()
    if ntype == "Organization":
        # An employer or school: its projects first (yours, under it), then the people there, the best attested first
        # (the most evidence behind their link to it).
        people = s.execute(text(f"""
            SELECT h.id, sum(1 + (SELECT count(*) FROM edge_evidence ev WHERE ev.edge_id = e.id)) AS w
            FROM edges e LEFT JOIN canonical c ON c.node_id = e.src JOIN nodes h ON h.id = COALESCE(c.canonical_id, e.src)
            WHERE e.dst = :n AND e.type IN ('WORKED_AT', 'MEMBER_OF') AND h.type = 'Person'
              AND {shown('h')} AND {not_owner('h')}
            GROUP BY h.id
        """), {"n": node}).all()
        return list(stated) + earned_cut([(i, w * 2) for i, w in subs]) + \
            earned_cut([(i, float(w)) for i, w in people], hi=8)
    if subs or stated:
        # An umbrella shows its projects first; its own people, chats and repo still follow, so a project that
        # also has a sub-project (a class with its capstone) never opens to just that one.
        top = list(stated) + [i for i in earned_cut([(i, w) for i, w in subs]) if i not in stated]
        return top + [i for i in _own_content(s, node) if i not in top]
    return _own_content(s, node)


def _own_content(s: Session, node: int) -> list[int]:
    """A project's own content: where its work lives (repo), its people, its group chats and key documents."""
    # Where the work lives: the repository or folder that holds most of its records (the GitHub repo for a
    # coding project), one link instead of its issues one by one.
    homes = s.execute(text(f"""
        SELECT f.id, count(*) FROM edges pe JOIN edges ct ON ct.dst = pe.src AND ct.type = 'CONTAINS'
        JOIN nodes f ON f.id = ct.src AND f.type = 'Folder' AND {visible('f')}
        WHERE pe.dst = :n AND pe.type = 'PART_OF'
        GROUP BY f.id HAVING count(*) >= 3
    """), {"n": node}).all()
    # Its people as identity heads: checked (or stated) ones, plus anyone the verifier was unsure about who is in one
    # of its own group chats; being in the trip's chat is the direct evidence the verifier didn't have.
    involved = s.execute(text(f"""
        SELECT h.id, sum(COALESCE((e.props->>'items')::float, 1)) AS items,
               bool_or(e.props->>'verdict' = 'keep' OR e.extractor IN ('user', 'profile', 'contacts')) AS checked,
               bool_or(e.props->>'verdict' = 'unsure') AS unsure
        FROM edges e LEFT JOIN canonical c ON c.node_id = e.src JOIN nodes h ON h.id = COALESCE(c.canonical_id, e.src)
        WHERE e.dst = :n AND e.type = 'INVOLVED_IN' AND h.type = 'Person' AND {shown('h')}
          AND COALESCE(e.props->>'verdict', '') <> 'drop'
        GROUP BY h.id
    """), {"n": node}).all()
    team = {p for p, _w, _c, _u in involved}
    # Its group chats, ranked by how many of its people they hold (the trip's chat is the one its travellers are in),
    # plus how many of its records happened there; a chat attached to it as a whole counts most.
    chats = s.execute(text(f"""
        WITH ch AS (SELECT it.dst AS g, count(DISTINCT m.id) AS msgs FROM edges pe
                    JOIN nodes m ON m.id = pe.src AND m.type = 'Message'
                    JOIN edges it ON it.src = m.id AND it.type = 'IN_THREAD'
                    JOIN nodes t ON t.id = it.dst AND {group_conversation('t')}
                    WHERE pe.dst = :n AND pe.type = 'PART_OF' GROUP BY it.dst),
        mem AS (SELECT ch.g, count(DISTINCT COALESCE(c.canonical_id, x.dst)) AS shared FROM ch
                JOIN edges it ON it.dst = ch.g AND it.type = 'IN_THREAD'
                JOIN edges x ON x.src = it.src AND x.type = 'RECEIVED' LEFT JOIN canonical c ON c.node_id = x.dst
                WHERE COALESCE(c.canonical_id, x.dst) = ANY(:team) GROUP BY ch.g)
        SELECT ch.g, ch.msgs + 20 * COALESCE(mem.shared, 0) FROM ch LEFT JOIN mem USING (g)
        UNION ALL
        SELECT g.id, 50 FROM edges pe JOIN nodes g ON g.id = pe.src AND {group_conversation('g')}
        WHERE pe.dst = :n AND pe.type = 'PART_OF' AND COALESCE(pe.props->>'verdict', '') <> 'drop'
    """), {"n": node, "team": sorted(team)}).all()
    convs: dict[int, float] = {}
    for g, w in chats:
        convs[g] = max(convs.get(g, 0.0), float(w))
    in_chats = set(s.execute(text("""
        SELECT COALESCE(c.canonical_id, e.src) FROM edges it JOIN edges e ON e.dst = it.src AND e.type = 'SENT'
        LEFT JOIN canonical c ON c.node_id = e.src WHERE it.dst = ANY(:chats) AND it.type = 'IN_THREAD'
        UNION
        SELECT COALESCE(c.canonical_id, e.dst) FROM edges it JOIN edges e ON e.src = it.src AND e.type = 'RECEIVED'
        LEFT JOIN canonical c ON c.node_id = e.dst WHERE it.dst = ANY(:chats) AND it.type = 'IN_THREAD'"""),
        {"chats": [g for g, _w in sorted(convs.items(), key=lambda x: (-x[1], x[0]))][:5]}).scalars())
    people = [(p, float(w)) for p, w, checked, unsure in involved if checked or (unsure and p in in_chats)]
    docs = s.execute(text(f"""
        SELECT f.id, 1 + length(coalesce(si.body_text, '')) / 4000.0 FROM edges pe
        JOIN nodes f ON f.id = pe.src AND f.type = 'File' AND {visible('f')}
        JOIN source_items si ON si.id = f.source_item_id
        WHERE pe.dst = :n AND pe.type = 'PART_OF' AND f.name !~* :code
    """), {"n": node, "code": CODE_FILE}).all()
    return (earned_cut([(i, float(w)) for i, w in homes], hi=2)
            + _core_people(people, keep={p for p, _w in people} & in_chats)
            + earned_cut(list(convs.items()), hi=5)
            + earned_cut([(i, float(w)) for i, w in docs], hi=4))


def fallback_neighbors(s: Session, node: int, exclude: set[int], k: int) -> list[int]:
    """The node's strongest connections when its own rules found too few: direct links first (a verifier keep or a
    stated link before an unchecked one, then by confidence), then what its records link to (an org's people, a file's
    conversation). People are identity heads; nothing hidden, no service, no code file, never what it is part of."""
    if k <= 0:
        return []
    ntype = s.execute(text("SELECT type FROM nodes WHERE id = :n"), {"n": node}).scalar()
    rows = s.execute(text(f"""
        WITH links AS (
            -- Never the node's own parent (what it's part of): opening the Lab must not list State University in it.
            SELECT e.dst AS o, e.props, e.extractor, e.confidence FROM edges e
            WHERE e.src = ANY(:ids) AND e.type NOT IN ('SAME_AS', 'HAS_CHUNK', 'PART_OF')
            UNION ALL
            SELECT e.src, e.props, e.extractor, e.confidence FROM edges e
            WHERE e.dst = ANY(:ids) AND e.type NOT IN ('SAME_AS', 'HAS_CHUNK')
              AND NOT (e.type = 'PART_OF' AND e.src = (SELECT (props->>'thread')::bigint FROM nodes WHERE id = :n))),
        direct AS MATERIALIZED (
            SELECT o, (CASE WHEN props->>'verdict' = 'keep' OR extractor IN ('user', 'profile', 'contacts', 'spine')
                            THEN 2 ELSE 1 END) + COALESCE(confidence, 0.5) AS w
            FROM links WHERE COALESCE(props->>'verdict', '') <> 'drop'),
        records AS (SELECT DISTINCT d.o FROM direct d JOIN nodes dn ON dn.id = d.o AND dn.type IN ('Message', 'Event', 'File')),
        via AS (  -- one step through the node's records: the message's thread and people, the event's attendees
            SELECT e2.dst AS o, 0.5 AS w FROM records r
            JOIN edges e2 ON e2.src = r.o AND e2.type IN ('IN_THREAD', 'RECEIVED', 'PART_OF')
            UNION ALL
            SELECT e2.src, 0.5 FROM records r JOIN edges e2 ON e2.dst = r.o AND e2.type IN ('SENT', 'ATTENDED')),
        allc AS (SELECT o, w FROM direct UNION ALL SELECT o, w FROM via)
        SELECT x.id, x.type, max(a.w) + 0.1 * ln(1 + count(*)) AS w
        FROM allc a LEFT JOIN canonical c ON c.node_id = a.o JOIN nodes x ON x.id = COALESCE(c.canonical_id, a.o)
        WHERE {shown('x')} AND x.id <> ALL(CAST(:ids AS bigint[]))
          -- A person's messages are the person; only bigger things or other people go beside them.
          AND NOT (:person AND x.type = 'Message')
          AND NOT (x.type = 'File' AND x.name ~* :code)
        GROUP BY x.id, x.type ORDER BY 3 DESC, 1 LIMIT 60
    """), {"n": node, "ids": identity.members(s, node), "person": ntype == "Person", "code": CODE_FILE}).all()
    # A single message stands for its conversation: the group chat it's in, else the person it's with. Only a
    # message with neither (a note to yourself) stands for itself, and only after everything else.
    messages = [o for o, t, _w in rows if t == "Message"]
    rep = dict(s.execute(text(f"""
        SELECT m.id, COALESCE(
            (SELECT it.dst FROM edges it JOIN nodes g ON g.id = it.dst AND {group_conversation('g')}
             WHERE it.src = m.id AND it.type = 'IN_THREAD' ORDER BY it.dst LIMIT 1),
            (SELECT h.id FROM (SELECT dst AS p FROM edges WHERE src = m.id AND type = 'RECEIVED'
                               UNION SELECT src FROM edges WHERE dst = m.id AND type = 'SENT') x
             LEFT JOIN canonical c ON c.node_id = x.p JOIN nodes h ON h.id = COALESCE(c.canonical_id, x.p)
             WHERE {shown('h')} AND {not_owner('h')} ORDER BY h.id LIMIT 1))
        FROM nodes m WHERE m.id = ANY(:i)"""), {"i": messages}).all()) if messages else {}
    # A folder is where a file sits, not what it's connected to: last, like a lone message.
    last = [o for o, t, _w in rows if (t == "Message" and rep.get(o) is None) or t == "Folder"]
    first = [rep.get(o, o) for o, t, _w in rows if o not in last]
    out: list[int] = []
    for o in first + last:
        if o is not None and o not in exclude and o != node and o not in out:
            out.append(o)
            if len(out) == k:
                break
    return out


def expand(s: Session, seed: int | None, node: int, visible_ids: list[int], k: int = 6) -> dict:
    """Reveal `node`'s top connections that aren't on screen (`visible_ids`) yet. A person reveals their strongest
    people, group chats, projects, events and files; a project or org what it is made of (see `children`); a group or
    event who takes part in it. Each new node hangs off `node`, with what is part of what and the ties among what is on
    screen. Raises identity.NotFound for an unknown node or seed."""
    n = identity.node(s, node)
    nid, ntype = n["id"], n["type"]
    head = identity.node(s, seed)["id"] if seed is not None else None
    seen = set(identity.heads(s, visible_ids).values())
    blocked = rejected(s)
    new: list[int] = []
    edges: list[dict] = []
    if ntype == "Person":
        # Someone this person only knows through one project or group chat goes under it, not beside them: the
        # context takes their place (once), and they appear when it's opened.
        people = [p for p, _st, _ in relationships(s, nid, 40) if p not in seen and p != head]
        ctx = {(a if b == nid else b): c for (a, b), (_n, c) in _shared(s, [nid], people).items() if c is not None}
        people = list(dict.fromkeys(ctx.get(p, p) for p in people))
        quota = {"Person": max(3, k // 2), "Thread": 3, "Project": 3, "Event": 2, "File": 2}
        used: dict[str, int] = {}
        for x, t in [*((p, "Person") for p in people), *((x, t) for x, t, _w in person_world(s, nid))]:
            if x not in seen and x not in new and used.get(t, 0) < quota.get(t, 1):
                new.append(x)
                used[t] = used.get(t, 0) + 1
        new = new[:k * 2]
    elif ntype in ("Project", "Organization"):
        # What it's made of, one level down: an umbrella (a lab, an employer) shows its projects; a project shows
        # its people, its main conversations and its key documents. How many is earned (see earned_cut).
        new = [p for p in children(s, nid, ntype) if p not in seen and p != head]
    else:
        # A group or event: every member who takes part (2+ messages or 2% of its messages; everyone when it is
        # small), so a huge list's silent recipients stay out; at most GROUP_MAX of them.
        rows = s.execute(text(f"""
            WITH q AS (
                SELECT x.src AS p FROM edges it JOIN nodes m ON m.id = it.src AND {visible('m')}
                JOIN edges x ON x.dst = it.src AND x.type = 'SENT'
                WHERE it.dst = :n AND it.type = 'IN_THREAD'
                UNION ALL SELECT src FROM edges WHERE dst = :n AND type IN ('ATTENDED', 'MEMBER_OF', 'INVOLVED_IN')),
            c AS (SELECT COALESCE(cn.canonical_id, q.p) AS p, count(*) AS n FROM q
                  LEFT JOIN canonical cn ON cn.node_id = q.p GROUP BY 1)
            SELECT c.p FROM c JOIN nodes pn ON pn.id = c.p AND pn.type = 'Person' AND {shown('pn')}
            WHERE c.n >= 2 OR c.n >= 0.02 * (SELECT sum(n) FROM c) OR (SELECT count(*) FROM c) <= 12
            ORDER BY c.n DESC, c.p
        """), {"n": nid}).scalars().all()
        members = [p for p in rows if p != head and frozenset((nid, p)) not in blocked]
        # Members already on screen: still show they're in this group.
        edges += [drawn(nid, p, "RELATED") for p in members if p in seen]
        new = [p for p in members if p not in seen][:GROUP_MAX]
    unlinked = {x for pair in blocked if nid in pair for x in pair}  # what you said isn't linked to it
    new = [p for p in dict.fromkeys(new) if p not in (nid, head) and p not in unlinked]
    # However strict the rules above are for this kind of node, opening it never shows (nearly) nothing.
    if len(new) < MIN_SHOWN:
        new += fallback_neighbors(s, nid, seen | set(new) | unlinked | {head}, MIN_SHOWN - len(new))
    named = names(s)
    nodes = _nodes(s, new, named)
    for x in nodes:
        x.update(kind="entity", expandable=True, strength=0.5)
    new = [x["id"] for x in nodes]
    edges += [drawn(nid, p, "RELATED") for p in new]
    vis = list(dict.fromkeys([*new, *seen, nid]))
    struct = [e for e in _structure(s, vis, blocked) if e["src"] in new or e["dst"] in new]
    linked = {(e["src"], e["dst"]) for e in struct} | {(e["dst"], e["src"]) for e in struct}
    edges = [e for e in edges if (e["src"], e["dst"]) not in linked]  # the real "part of" replaces the generic tie
    # Among a group's members, show how they relate to each other too (who talks to whom), not one tie each.
    ties = _ties(s, vis, new, blocked, per_node=1 if ntype in ("Person", "Project") else 3)
    return {"nodes": nodes, "edges": edges + struct + ties}
