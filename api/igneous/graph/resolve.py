"""Entity resolution for Person nodes: blocking -> pairwise scoring -> SAME_AS edges.

Tuned for precision: a false merge silently corrupts the graph, a missed merge only fragments it. Merges are
SAME_AS edges (non-destructive); `igneous resolve --undo` deletes them. A pair merges only when it has
name evidence AND either an address-structure match or shared correspondents, never on name alone (there are
seven different "David"s in the corpus).
"""
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph import dirty

EXTRACTOR = "er"
VERSION = "1"
THRESHOLD = 0.5  # chosen on AI-reviewed labels: P 0.938 R 0.944 F1 0.941 (eval/README.md)

# Display names and local parts that are mailbox roles, not people.
ROLE_WORDS = {"info", "news", "newsletter", "editor", "admin", "support", "help", "sales", "webmaster", "mail",
              "e-mail", "email", "noreply", "no-reply", "service", "team", "office", "contact", "list", "hr"}
ROLE_PATTERN = re.compile(r"^dl-|administration|announce|enron\.?(mail|messaging)|mailbox|distribution|"
                          r"updates?@|contacts?@|survey|alert|digest|bulletin|subscri", re.I)

# Common English nicknames, both directions. Eval misses were Rick/Richard, Joe/Joseph, Eddie/Eduardo, Mike/Michael.
_NICK = {
    "richard": ["rick", "dick", "rich", "ricky"], "robert": ["bob", "rob", "bobby", "robbie"],
    "william": ["bill", "will", "billy", "willie"], "michael": ["mike", "mick", "mickey"],
    "joseph": ["joe", "joey"], "james": ["jim", "jimmy", "jamie"], "john": ["jack", "johnny"],
    "thomas": ["tom", "tommy"], "christopher": ["chris"], "christine": ["chris", "christy"],
    "kenneth": ["ken", "kenny"], "jeffrey": ["jeff"], "jeffery": ["jeff"], "steven": ["steve"],
    "stephen": ["steve"], "edward": ["ed", "eddie", "ted"], "eduardo": ["eddie", "ed"], "daniel": ["dan", "danny"],
    "david": ["dave"], "gregory": ["greg"], "timothy": ["tim"], "anthony": ["tony"], "andrew": ["andy", "drew"],
    "matthew": ["matt"], "patrick": ["pat"], "patricia": ["pat", "patty", "trish"], "elizabeth": ["liz", "beth",
    "betsy", "lisa"], "katherine": ["kathy", "kate", "katie"], "kathleen": ["kathy", "kate"], "margaret":
    ["peggy", "maggie", "meg"], "susan": ["sue", "susie"], "deborah": ["debbie", "deb"], "jennifer": ["jen",
    "jenny"], "rebecca": ["becky"], "samuel": ["sam"], "benjamin": ["ben"], "charles": ["chuck", "charlie"],
    "lawrence": ["larry"], "ronald": ["ron"], "donald": ["don"], "gerald": ["jerry"], "frederick": ["fred"],
    "philip": ["phil"], "phillip": ["phil"], "nicholas": ["nick"], "alexander": ["alex"], "douglas": ["doug"],
    "raymond": ["ray"], "leonard": ["len"], "vincent": ["vince"], "sally": ["sal"], "victoria": ["vicky", "tori"],
}
NICKNAMES = {n: {n, *v} for n, v in _NICK.items()}
for _formal, _short in _NICK.items():
    for _n in _short:
        NICKNAMES.setdefault(_n, {_n}).update({_formal, *_short})


def same_first(a: str, b: str) -> bool:
    return a == b or a.startswith(b) or b.startswith(a) or b in NICKNAMES.get(a, ())


@dataclass
class P:
    id: int
    key: str           # email address, or "name:<slug>" for model-extracted mentions
    name: str
    first: str
    last: str
    peers: set[int]    # people this person exchanged mail with (via Message nodes)

    @property
    def is_email(self) -> bool:
        return "@" in self.key and not self.key.startswith("name:")

    @property
    def is_phone(self) -> bool:
        return bool(re.fullmatch(r"\+?\d{7,15}", self.key))

    @property
    def local(self) -> str:
        return self.key.split("@")[0] if self.is_email else ""

    @property
    def domain(self) -> str:
        return self.key.split("@")[-1] if self.is_email else ""


def split_name(name: str, key: str) -> tuple[str, str]:
    """Best-effort (first, last), lowercase. Handles 'Last, First', 'First M Last', and first.last@ keys."""
    n = re.sub(r"\(.*?\)|\".*?\"|<.*?>", "", name).strip().lower()
    if "," in n:
        last, first = (x.strip() for x in n.split(",", 1))
    else:
        parts = [p for p in re.split(r"\s+", n) if p]
        first, last = (parts[0], parts[-1]) if len(parts) >= 2 else (parts[0] if parts else "", "")
    if not last and "@" in key:  # fall back to the address: phillip.allen@ -> phillip, allen
        bits = [b for b in re.split(r"[._]", key.split("@")[0]) if b]
        if len(bits) >= 2:
            first, last = bits[0], bits[-1]
    clean = lambda s: re.sub(r"[^a-z]", "", s)  # noqa: E731
    return clean(first.split()[0] if first else ""), clean(last)


def unresolvable(p: P) -> bool:
    """Exchange-mangled stubs ("e-mail <.john@enron.com>") and role mailboxes can't be one person."""
    return (p.local.startswith(".") or p.name.lower() in ROLE_WORDS or p.local in ROLE_WORDS
            or bool(ROLE_PATTERN.search(p.key)) or not p.last)


def load(s: Session) -> list[P]:
    rows = s.execute(text("SELECT id, key, name FROM nodes WHERE type = 'Person'")).all()
    peers: dict[int, set[int]] = defaultdict(set)
    for a, b in s.execute(text("""
        SELECT DISTINCT snd.src, rcv.dst FROM edges snd JOIN edges rcv ON rcv.src = snd.dst
        WHERE snd.type = 'SENT' AND rcv.type = 'RECEIVED' AND snd.src <> rcv.dst
    """)):
        peers[a].add(b)
        peers[b].add(a)
    out = []
    for r in rows:
        first, last = split_name(r.name, r.key)
        out.append(P(r.id, r.key, r.name, first, last, peers.get(r.id, set())))
    return out


def blocks(people: list[P]) -> dict[str, list[P]]:
    """Candidate pairs only within a block: surname + first initial. Avoids all-pairs (14k^2)."""
    b: dict[str, list[P]] = defaultdict(list)
    for p in people:
        if not unresolvable(p) and p.first:
            b[f"{p.last}|{p.first[0]}"].append(p)
    return {k: v for k, v in b.items() if 1 < len(v) <= 50}


def score(a: P, b: P) -> tuple[float, dict]:
    """Returns (score in [0,1], features). Name evidence is required; it can't merge a pair on its own."""
    first_compatible = same_first(a.first, b.first)
    # Score name similarity on surnames when first names are nickname-equivalent (Rick ~ Richard).
    a_first = b.first if first_compatible and b.first in NICKNAMES.get(a.first, ()) else a.first
    name_sim = SequenceMatcher(None, f"{a_first} {a.last}", f"{b.first} {b.last}").ratio()
    shared = len(a.peers & b.peers)
    jaccard = shared / max(1, len(a.peers | b.peers))
    same_domain = a.is_email and b.is_email and a.domain == b.domain
    # Mentions (name:...) and phone handles have no mail correspondents; they lean on exact full-name agreement
    # instead. A phone's name comes from a contact card, so an exact match to an address's name is strong.
    mention_pair = not a.is_email or not b.is_email
    exact_name = (a.first, a.last) == (b.first, b.last)

    f = {"name_sim": round(name_sim, 3), "first_compatible": first_compatible, "shared_peers": shared,
         "jaccard": round(jaccard, 3), "same_domain": same_domain, "exact_name": exact_name}
    if not first_compatible or name_sim < 0.8:
        return 0.0, f
    if mention_pair:
        return (0.8 if exact_name else 0.0), f
    # Same local part on different domains (ken.lay@enron.com ~ ken_lay@enron.net) is strong on its own.
    norm_local = lambda p: re.sub(r"[._'-]", "", p.local)  # noqa: E731
    same_local = norm_local(a) == norm_local(b) and len(norm_local(a)) >= 6
    corroboration = (min(1.0, shared / 3) * 0.6 + (0.25 if same_domain else 0.0) + min(0.15, jaccard)
                     + (0.6 if same_local else 0.0))
    f["same_local"] = same_local
    return round(min(1.0, 0.4 * name_sim + 0.6 * corroboration), 3), f


MAX_SAME_NAME = 3  # identities sharing one exact full name that are still assumed to be one person
threshold_exact = 0.6  # score given to a rare exact-name pair (above THRESHOLD, below corroborated merges)


def candidates(s: Session) -> list[tuple[P, P, float, dict]]:
    out = []
    people = load(s)
    # Two different addresses with the same full name and nothing else in common might be two people; then a
    # phone or mention with that name can't be safely assigned to either.
    by_name: dict[tuple[str, str], int] = defaultdict(int)
    for p in people:
        if p.first and p.last and not unresolvable(p):
            by_name[(p.first, p.last)] += 1
    for group in blocks(people).values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                sc, f = score(a, b)
                same = by_name[(a.first, a.last)]
                # A full name carried by only 2-3 identities in one person's data is almost always one human with
                # a school address, a personal one and a phone. Merge on the exact name alone; a name on 4+
                # identities is common enough to be different people, so it still needs corroboration.
                if f.get("exact_name") and sc < threshold_exact and 1 < same <= MAX_SAME_NAME:
                    sc, f = threshold_exact, {**f, "rare_exact_name": same}
                elif sc and (not a.is_email or not b.is_email) and same > MAX_SAME_NAME:
                    sc, f = 0.0, {**f, "ambiguous_name": True}
                out.append((a, b, sc, f))
    return out


def run(s: Session, threshold: float = THRESHOLD) -> dict:
    if not dirty.pending(s):
        return {"same_as": 0, "clusters": 0}
    s.execute(text("DELETE FROM edges WHERE type = 'SAME_AS' AND extractor = :ex"), {"ex": EXTRACTOR})  # not 'contacts'
    merged = 0
    for a, b, sc, f in candidates(s):
        if sc >= threshold:
            src, dst = sorted((a.id, b.id))  # one undirected edge per pair
            s.execute(text("""
                INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
                VALUES (:a, :b, 'SAME_AS', CAST(:p AS jsonb), :ex, :v, :c)
                ON CONFLICT (src, dst, type, extractor) DO NOTHING
            """), {"a": src, "b": dst, "p": json.dumps(f), "ex": EXTRACTOR, "v": VERSION, "c": sc})
            merged += 1
    s.commit()
    clusters = refresh_canonical(s)
    dirty.clear(s)
    dirty.touch(s)
    s.commit()
    return {"same_as": merged, "clusters": clusters}


# SAME_AS from these sources is ground truth (a contact card, the signed-in account, a user correction, a
# commit's verified email): never second-guess it with name heuristics. Without this, "Me" (first name "me")
# could never join the user's named cluster.
TRUSTED = ("self", "contacts", "user", "github")


def _human_name(name: str, handles: set[str]) -> bool:
    """'Jane Doe', not 'Jdoe2446': two or more words, no digits, not one of the person's address handles."""
    return len(name.split()) >= 2 and name.replace(" ", "").lower() not in handles and not re.search(r"\d", name)


def refresh_canonical(s: Session) -> int:
    """Union-find over Person SAME_AS edges; each cluster's canonical node is its best-connected member, so
    the explorer shows "Kenneth Lay" with all his addresses folded in. Scoped to Person nodes: rows for other
    node types (e.g. project merges) are left alone. The user's own cluster (props.self) is headed by the
    self node, renamed to the best human name in the cluster."""
    edges = s.execute(text("""
        SELECT e.src, e.dst, e.confidence, e.extractor FROM edges e
        JOIN nodes a ON a.id = e.src AND a.type = 'Person' JOIN nodes b ON b.id = e.dst AND b.type = 'Person'
        WHERE e.type = 'SAME_AS'
    """)).all()
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    meta = {r.id: r for r in s.execute(text("SELECT id, key, name, props FROM nodes WHERE type = 'Person'"))}
    firsts: dict[int, set[str]] = {}  # per cluster root: the distinct full first names it contains

    def first_of(n: int) -> str:
        m = meta.get(n)
        if m is None or (m.props or {}).get("self"):
            return ""  # the self node's display name ("Me") isn't a first name
        return split_name(m.name, "")[0]

    # Trusted links first, then strongest; refuse a heuristic union that would put two incompatible first
    # names in one cluster (else a surname-only address like taylor@ bridges Liz and Larry Taylor).
    for a, b, _conf, ex in sorted(edges, key=lambda e: (e.extractor not in TRUSTED, -(e.confidence or 0))):
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        fa = firsts.get(ra, {first_of(a)} - {""} if len(first_of(a)) > 1 else set())
        fb = firsts.get(rb, {first_of(b)} - {""} if len(first_of(b)) > 1 else set())
        if ex not in TRUSTED and any(not same_first(x, y) for x in fa for y in fb):
            continue
        parent[ra] = rb
        firsts[rb] = fa | fb
    clusters: dict[int, list[int]] = defaultdict(list)
    for n in list(parent):
        clusters[find(n)].append(n)
    members = [n for c in clusters.values() for n in c]
    degree = dict(s.execute(text("""
        SELECT n, (SELECT count(*) FROM edges e WHERE (e.src = n OR e.dst = n) AND e.type IN ('SENT', 'RECEIVED'))
        FROM unnest(CAST(:ids AS bigint[])) AS n
    """), {"ids": members}).all()) if members else {}
    s.execute(text("DELETE FROM canonical WHERE node_id IN (SELECT id FROM nodes WHERE type = 'Person')"))
    rows = []
    for c in clusters.values():
        is_self = {n for n in c if (meta[n].props or {}).get("self")} if all(n in meta for n in c) else set()
        head = max(c, key=lambda n: (n in is_self, degree.get(n, 0), -n))
        rows += [{"n": n, "c": head} for n in c]
        # Every person is labeled by their best human name in the cluster, never an address or number when a
        # name is known (the user's own cluster is headed by the self node, which takes the same rule).
        if c:
            names = [meta[n] for n in c if n not in is_self and meta[n].name and "@" not in meta[n].name
                     and not re.fullmatch(r"\+?\d[\d\s()-]+", meta[n].name)]
            # A real name has a first and last name and isn't an address handle ("jasonli2446"); prefer it over
            # a single-token card name, then prefer contact cards, then the best-connected identity.
            handles = {k.split("@")[0].lower() for k in (meta[n].key for n in c) if "@" in k}
            named = sorted(names, key=lambda m, h=handles: (not _human_name(m.name, h),
                                                            (m.props or {}).get("named_by") != "contacts",
                                                            -degree.get(m.id, 0)))
            if named and (is_self or meta[head].name != named[0].name):
                s.execute(text("UPDATE nodes SET name = :n WHERE id = :id"), {"n": named[0].name, "id": head})
    if rows:
        s.execute(text("INSERT INTO canonical (node_id, canonical_id) VALUES (:n, :c)"), rows)
    s.commit()
    return len(clusters)


def undo(s: Session) -> int:
    n = s.execute(text("DELETE FROM edges WHERE type = 'SAME_AS' AND extractor = :ex"), {"ex": EXTRACTOR}).rowcount
    s.execute(text("DELETE FROM canonical WHERE node_id IN (SELECT id FROM nodes WHERE type = 'Person')"))
    s.commit()
    return n
