"""Entity resolution for Person nodes: blocking -> pairwise scoring -> SAME_AS edges -> identity clusters.

Tuned for precision: a false merge silently corrupts the graph, a missed merge only fragments it. A merge is a
SAME_AS edge with extractor 'er' (non-destructive); `igneous resolve --undo` deletes them.

Two addresses merge on name evidence plus corroboration: correspondents they share, counted on merged identities and
weighted by how rare each one is (never you, your own addresses or a service: everyone in your mail shares you), the
same organizational mail domain, or the same local part. A phone handle or a name extracted from text has no mail of
its own: it joins on its exact full name, and only when no other bearer of that name could be the one it means.
Nothing merges on a name alone otherwise (there are seven different "David"s in the corpus), nothing joins your own
cluster (that is the self stage's, from identity signals), and no pair said not to be one person merges, directly or
through others.

Identity clusters (`canonical`) are rebuilt from every SAME_AS link on every run; pairs are scored again only when
source records changed since the last scoring or the scoring itself changed (VERSION).
"""
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph import dirty, identity
from igneous.graph.names import GENERATIONAL
from igneous.graph.organizations import PERSONAL_DOMAINS
from igneous.graph.self_identity import NAME_PARTICLES

EXTRACTOR = "er"
VERSION = "2"      # links in place from another version are scored again
THRESHOLD = 0.5    # chosen on AI-reviewed labels (eval/README.md)
NAME_SIM = 0.8     # how alike two full names must read before anything else about a pair counts
SHARED_FULL = 3.0  # rarity-weighted shared correspondents that count as full corroboration
SAME_BEARER = 0.5  # rarity-weighted shared correspondents that make two same-name identities one bearer
DROP_CONFIDENCE = 0.8  # a verifier 'drop' on a SAME_AS link this sure keeps the pair apart (as verify.apply_verdicts)

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
    """Whether two first names can be one person's, either way round: equal, one a prefix of the other (an initial),
    or nicknames of one name."""
    return a == b or a.startswith(b) or b.startswith(a) or b in NICKNAMES.get(a, ()) or a in NICKNAMES.get(b, ())


def name_evidence(a: tuple[str, str], b: tuple[str, str]) -> tuple[bool, float]:
    """What two (first, last) names say about being one person's, read in this order: whether the first names can
    be (`same_first`), and how alike the full names read in [0, 1], on the surnames alone when the first names are
    nicknames of one name (Rick ~ Richard). The similarity depends on the order, so each caller fixes one."""
    (a_first, a_last), (b_first, b_last) = a, b
    compatible = same_first(a_first, b_first)
    if b_first in NICKNAMES.get(a_first, ()) or a_first in NICKNAMES.get(b_first, ()):
        a_first = b_first
    return compatible, SequenceMatcher(None, f"{a_first} {a_last}", f"{b_first} {b_last}").ratio()


def block(first: str, last: str) -> str:
    """The block a (first, last) name is compared in: its surname and first initial."""
    return f"{last}|{first[:1]}"


def same_name(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """Whether two (first, last) names pass the name evidence scoring requires of a pair before anything else about
    it counts: both whole (a first name and a surname), in one block, with first names that can be one person's and
    full names at least NAME_SIM alike. The same either way round."""
    if not all((*a, *b)) or block(*a) != block(*b):
        return False
    compatible, similarity = name_evidence(*sorted((a, b)))
    return compatible and similarity >= NAME_SIM


@dataclass
class P:
    id: int
    key: str           # email address, phone number, or "name:<slug>" for a name extracted from text
    name: str
    first: str
    last: str
    peers: set[int]    # the identity heads this address exchanged mail with, never you or a service
    weight: dict[int, float] = field(default_factory=dict)  # each correspondent's rarity in [0, 1] (shared by all)
    head: int = 0      # its identity cluster's head (itself when unmerged)
    known: int = 0     # its cluster of trusted links (TRUSTED): identities known to be one person, whatever scoring says
    owner: bool = False    # one of your own identities
    service: bool = False  # an automated sender (props.kind = 'service')

    def __post_init__(self):
        self.head = self.head or self.id
        self.known = self.known or self.id

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
    """Best-effort (first, last), lowercase. Handles 'Last, First', 'First M Last' (the surname keeps its particles,
    'Eric Le Dain' -> ledain, and is never a generational suffix, 'Errol McLaughlin Jr.' -> mclaughlin), and
    first.last@ keys."""
    n = re.sub(r"\(.*?\)|\".*?\"|<.*?>", "", name).strip().lower()
    if "," in n:
        last, first = (x.strip() for x in n.split(",", 1))
    else:
        parts = [p for p in re.split(r"\s+", n) if p]
        while len(parts) > 2 and parts[-1].rstrip(".") in GENERATIONAL:
            parts.pop()
        at = len(parts) - 1  # where the surname starts
        while at > 1 and parts[at - 1] in NAME_PARTICLES:
            at -= 1
        first, last = (parts[0], " ".join(parts[at:])) if len(parts) >= 2 else (parts[0] if parts else "", "")
    if not last and "@" in key:  # fall back to the address: phillip.allen@ -> phillip, allen
        bits = [b for b in re.split(r"[._]", key.split("@")[0]) if b]
        if len(bits) >= 2:
            first, last = bits[0], bits[-1]
    clean = lambda s: re.sub(r"[^a-z]", "", s)  # noqa: E731
    return clean(first.split()[0] if first else ""), clean(last)


def unresolvable(p: P) -> bool:
    """Exchange-mangled stubs ("e-mail <.john@enron.com>"), role mailboxes, product relays ("Sam Lee (Google Docs)",
    which carries someone else's name) and automated senders can't be one person."""
    from igneous.graph.self_identity import is_relay_name, is_service_address
    return (p.service or p.local.startswith(".") or p.name.lower() in ROLE_WORDS or p.local in ROLE_WORDS
            or bool(ROLE_PATTERN.search(p.key)) or not p.last or is_relay_name(p.name)
            or ("@" in p.key and is_service_address(p.key)))


# SAME_AS from these sources is ground truth (a contact card, the signed-in account, a user correction, a
# commit's verified email): never second-guess it with name heuristics. Without this, "Me" (first name "me")
# could never join the user's named cluster.
TRUSTED = identity.TRUSTED


def _clusters(links) -> dict[int, int]:
    """node -> root of its connected component over `links` ((a, b) pairs)."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in links:
        parent[find(a)] = find(b)
    return {n: find(n) for n in list(parent)}


def load(s: Session) -> list[P]:
    """Every Person, with the identity heads it exchanged mail with (never you, your own addresses or a service:
    everyone in your mail shares you) and each correspondent's rarity: log(N / n) / log(N) for a correspondent n of
    the N identities with any mail reaches, 1 for one only one reaches, 0 for one everyone does."""
    head = dict(s.execute(text("""
        SELECT c.node_id, c.canonical_id FROM canonical c JOIN nodes n ON n.id = c.node_id AND n.type = 'Person'
    """)).all())
    known = identity.known(s)
    me = identity.self_id(s)
    mine = {n for n, root in known.items() if me is not None and root == known.get(me)} | ({me} - {None})
    # In key order, so blocks and candidate pairs come out the same however the rows are stored.
    rows = s.execute(text("""
        SELECT id, key, name, COALESCE(props->>'kind', '') = 'service' AS service FROM nodes WHERE type = 'Person'
        ORDER BY key
    """)).all()
    never = mine | {r.id for r in rows if r.service}
    peers: dict[int, set[int]] = defaultdict(set)
    for a, b in s.execute(text("""
        SELECT DISTINCT snd.src, rcv.dst FROM edges snd JOIN edges rcv ON rcv.src = snd.dst
        WHERE snd.type = 'SENT' AND rcv.type = 'RECEIVED' AND snd.src <> rcv.dst
    """)):
        if b not in never:
            peers[a].add(head.get(b, b))
        if a not in never:
            peers[b].add(head.get(a, a))
    reach: dict[int, set[int]] = defaultdict(set)  # correspondent -> the identity heads whose mail reaches it
    for p, ps in peers.items():
        ps.discard(head.get(p, p))
        for c in ps:
            reach[c].add(head.get(p, p))
    n = len({head.get(p, p) for p, ps in peers.items() if ps})
    weight = {c: (math.log(n / len(r)) / math.log(n) if n > 2 else 1.0) for c, r in reach.items()}
    out = []
    for r in rows:
        first, last = split_name(r.name, r.key)
        out.append(P(r.id, r.key, r.name, first, last, peers.get(r.id, set()), weight, head.get(r.id, r.id),
                     known.get(r.id, r.id), r.id in mine, bool(r.service)))
    return out


def blocks(people: list[P]) -> dict[str, list[P]]:
    """Candidate pairs only within a block: surname + first initial. Avoids all-pairs (14k^2). Your own identities
    are the self stage's (from identity signals) and never a candidate."""
    b: dict[str, list[P]] = defaultdict(list)
    for p in people:
        if not p.owner and not unresolvable(p) and p.first:
            b[block(p.first, p.last)].append(p)
    return {k: v for k, v in b.items() if 1 < len(v) <= 50}


def shared_weight(a: P, b: P) -> float:
    """The correspondents two identities share, each counted by its rarity."""
    return math.fsum(a.weight.get(c, 1.0) for c in a.peers & b.peers)  # exact: the same in any order


def score(a: P, b: P) -> tuple[float, dict]:
    """Returns (score in [0,1], features), the same for (a, b) and (b, a). Name evidence is required; it can't merge
    a pair on its own."""
    a, b = sorted((a, b), key=lambda p: p.key)  # one order per pair: the comparisons below are read in it
    first_compatible, name_sim = name_evidence((a.first, a.last), (b.first, b.last))
    shared = shared_weight(a, b)
    union = math.fsum(a.weight.get(c, 1.0) for c in a.peers | b.peers)
    jaccard = shared / union if union else 0.0
    same_domain = a.is_email and b.is_email and a.domain == b.domain
    # Mentions (name:...) and phone handles have no mail correspondents; they lean on exact full-name agreement
    # instead, and candidates() allows that only when the name is unambiguous.
    mention_pair = not a.is_email or not b.is_email
    exact_name = (a.first, a.last) == (b.first, b.last)

    f = {"name_sim": round(name_sim, 3), "first_compatible": first_compatible, "shared_peers": len(a.peers & b.peers),
         "shared_weight": round(shared, 3), "jaccard": round(jaccard, 3), "same_domain": same_domain,
         "exact_name": exact_name}
    if not first_compatible or name_sim < NAME_SIM:
        return 0.0, f
    if mention_pair:
        return (0.8 if exact_name else 0.0), f
    # Same local part on different domains (ken.lay@enron.com ~ ken_lay@enron.net) is strong on its own.
    norm_local = lambda p: re.sub(r"[._'-]", "", p.local)  # noqa: E731
    same_local = norm_local(a) == norm_local(b) and len(norm_local(a)) >= 6
    corroboration = (min(1.0, shared / SHARED_FULL) * 0.6 + (0.25 if same_domain else 0.0) + min(0.15, jaccard)
                     + (0.6 if same_local else 0.0))
    f["same_local"] = same_local
    return round(min(1.0, 0.4 * name_sim + 0.6 * corroboration), 3), f


def _same_bearer(a: P, b: P) -> bool:
    """Evidence that two identities with one name are one human: trusted links (a contact card, your merge),
    correspondents they share (weighted by rarity), or the same organizational mail domain (two addresses at one
    school). A phone isn't evidence on its own (two people named Jordan can each have one)."""
    if a.known == b.known or shared_weight(a, b) >= SAME_BEARER:
        return True
    return bool(a.is_email and b.is_email and a.domain == b.domain and a.domain not in PERSONAL_DOMAINS)


def bearers(people: list[P]) -> dict[tuple[str, str], dict[int, set[int]]]:
    """For each exact full name, the distinct humans who carry it: {bearer: identity ids}. Identities tied by
    _same_bearer count once, so one person with a phone and four school addresses is one bearer and ten unrelated
    John Smiths are ten. Your own identities and automated senders don't count."""
    groups: dict[tuple[str, str], list[P]] = defaultdict(list)
    for p in people:
        if p.first and p.last and not p.owner and not unresolvable(p):
            groups[(p.first, p.last)].append(p)
    out = {}
    for name, ps in groups.items():
        root = _clusters((a.id, b.id) for i, a in enumerate(ps) for b in ps[i + 1:] if _same_bearer(a, b))
        holders: dict[int, set[int]] = defaultdict(set)
        for p in ps:
            holders[root.get(p.id, p.id)].add(p.id)
        out[name] = dict(holders)
    return out


def _only_bearer(x: P, y: P, holders: dict[int, set[int]]) -> bool:
    """x (a phone or an extracted name, with no mail of its own) may join y on the name alone: apart from x by
    itself, y's bearer is the only one carrying that name."""
    others = {b for b, ids in holders.items() if ids != {x.id}}
    return len(others) == 1 and y.id in holders[next(iter(others))]


def rejected(s: Session, yours: bool = False) -> set[frozenset[int]]:
    """Identity pairs said not to be one person: by you (a 'drop' on their SAME_AS link, whatever proposed it, or
    "not the same person") or, unless `yours`, confidently by the verifier; matched by natural key so they hold when
    nodes are rebuilt."""
    rows = s.execute(text("""
        SELECT a.id, b.id FROM edge_verdicts v
        JOIN nodes a ON a.type || ':' || a.key = v.src_key JOIN nodes b ON b.type || ':' || b.key = v.dst_key
        WHERE v.type = 'SAME_AS' AND v.verdict = 'drop'
          AND (v.source = 'user' OR (NOT :yours AND v.confidence >= :sure))
          AND a.type = 'Person' AND b.type = 'Person' AND a.id <> b.id
    """), {"sure": DROP_CONFIDENCE, "yours": yours}).all()
    return {frozenset(r) for r in rows}


def candidates(s: Session) -> list[tuple[P, P, float, dict]]:
    """Every pair in a block with its score, after the precision rules: a pair said not to be one person scores 0,
    and a phone or an extracted name joins on its exact name only when no other bearer could be the one it means."""
    people = load(s)
    holders = bearers(people)
    never = rejected(s)
    out = []
    for group in blocks(people).values():
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if frozenset((a.id, b.id)) in never:
                    out.append((a, b, 0.0, {"rejected": True}))
                    continue
                sc, f = score(a, b)
                # A side with no mail of its own joins on the name alone. With two such sides (a phone and a
                # mention), either may be the one that joins, so the pair holds when either could.
                lone = [(x, y) for x, y in ((a, b), (b, a)) if not x.is_email]
                named = holders.get((a.first, a.last), {})
                if sc and lone and not any(_only_bearer(x, y, named) for x, y in lone):
                    sc, f = 0.0, {**f, "ambiguous_name": True}
                out.append((a, b, sc, f))
    return out


def run(s: Session, threshold: float = THRESHOLD, rescore: bool = False) -> dict:
    """Rebuild identity clusters from every SAME_AS link. Before that, score candidate pairs again (replacing every
    'er' link) when source records changed since the last scoring, when the links in place come from another
    scoring VERSION, or when `rescore` asks. Scoring reads the clusters only trusted links form (TRUSTED), never an
    earlier scoring's, so its links depend on the graph as it is and not on how often it was scored. Returns
    {rescored, same_as: scored links in place, clusters}."""
    changes = dirty.pending(s, "resolve")
    stale = s.execute(text("""
        SELECT EXISTS (SELECT 1 FROM edges WHERE type = 'SAME_AS' AND extractor = :ex AND extractor_version <> :v)
    """), {"ex": EXTRACTOR, "v": VERSION}).scalar()
    rescored = bool(changes or stale or rescore)
    if rescored:
        from igneous.graph.derived import rejections

        s.execute(text("DELETE FROM edges WHERE type = 'SAME_AS' AND extractor = :ex"), {"ex": EXTRACTOR})
        refresh_canonical(s)  # who you are and who is known to be one person, which scoring reads
        blocked = rejections(s)  # a pair you said is wrong, on any link, is never one person by score
        links = [{"a": min(a.id, b.id), "b": max(a.id, b.id), "p": json.dumps(f), "c": sc}  # one edge per pair
                 for a, b, sc, f in candidates(s) if sc >= threshold and not blocked.blocks(a.id, b.id)]
        if links:
            s.execute(text("""
                INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
                VALUES (:a, :b, 'SAME_AS', CAST(:p AS jsonb), :ex, :v, :c)
                ON CONFLICT (src, dst, type, extractor) DO NOTHING
            """), [{**link, "ex": EXTRACTOR, "v": VERSION} for link in links])
        dirty.consume(s, "resolve", changes)
    clusters = refresh_canonical(s)
    same_as = s.execute(text("SELECT count(*) FROM edges WHERE type = 'SAME_AS' AND extractor = :ex"),
                        {"ex": EXTRACTOR}).scalar()
    return {"rescored": rescored, "same_as": same_as, "clusters": clusters}


def _human_name(name: str, handles: set[str]) -> bool:
    """'Jane Doe', not 'Jdoe2446' or 'Jane Doe (Google Docs)': two or more words, no digits, no parenthetical,
    not one of the person's address handles."""
    return (len(name.split()) >= 2 and name.replace(" ", "").lower() not in handles and not re.search(r"\d", name)
            and "(" not in name)


def own_name(name: str, props: dict | None) -> str:
    """A Person's own name, as its sources set it: `name`, unless refresh_canonical shows the cluster's name there
    (props.shown_name), in which case the own name waits in props.own_name."""
    props = props or {}
    if props.get("shown_name") is not None and name == props["shown_name"]:
        return props.get("own_name") or name
    return name


def refresh_canonical(s: Session) -> int:
    """Rebuild identity clusters (`canonical`: every member of a 2+ cluster -> its head) from every Person SAME_AS
    link, and return how many there are.

    Trusted links (TRUSTED) are unioned first and hold, except that no link of any kind joins two identities you said
    are not one person, directly or through others. A scored link is also refused when it would put two different
    first names in one cluster (else a surname-only address like taylor@ bridges Liz and Larry Taylor), add anyone to
    your own cluster, or join two identities the verifier confidently said are not one person (`rejected`). Each
    cluster's head is you for your own, else its best-connected member (so the explorer shows "Kenneth Lay" with all
    his addresses folded in), and the head shows the cluster's best human name. The head's own name is kept in
    props.own_name meanwhile and comes back once it no longer heads a cluster named otherwise. Links are taken, and
    ties broken, by natural keys, never by row ids: the same graph forms the same clusters however it was built."""
    edges = s.execute(text("""
        SELECT e.src, e.dst, e.confidence, e.extractor, e.id FROM edges e
        JOIN nodes a ON a.id = e.src AND a.type = 'Person' JOIN nodes b ON b.id = e.dst AND b.type = 'Person'
        WHERE e.type = 'SAME_AS'
    """)).all()
    meta = {r.id: r for r in s.execute(text("SELECT id, key, name, props FROM nodes WHERE type = 'Person'"))}
    own = {n: own_name(m.name, m.props) for n, m in meta.items()}
    me = identity.self_id(s)
    forbid: dict[int, set[int]] = defaultdict(set)   # said not to be one person
    yours: dict[int, set[int]] = defaultdict(set)    # ...by you: no link joins them, a trusted one included
    for pairs, apart in ((rejected(s), forbid), (rejected(s, yours=True), yours)):
        for a, b in map(tuple, pairs):
            apart[a].add(b)
            apart[b].add(a)

    parent: dict[int, int] = {}
    members: dict[int, set[int]] = {}
    firsts: dict[int, set[str]] = {}  # per cluster root: the distinct full first names it contains

    def find(x: int) -> int:
        if x not in parent:
            parent[x], members[x] = x, {x}
            first = "" if x == me or x not in own else split_name(own[x], "")[0]  # "Me" is not a first name
            firsts[x] = {first} if len(first) > 1 else set()
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # Trusted links first, then the surest; ties by the endpoints' keys, so clusters don't depend on row ids.
    order = lambda e: (e.extractor not in TRUSTED, -(e.confidence or 0),  # noqa: E731
                       sorted((meta[e.src].key, meta[e.dst].key)), e.extractor)
    for a, b, _conf, ex, _id in sorted(edges, key=order):
        ra, rb = find(a), find(b)
        if ra == rb or any(yours[x] & members[rb] for x in members[ra] if x in yours):
            continue
        if ex not in TRUSTED and (any(not same_first(x, y) for x in firsts[ra] for y in firsts[rb])
                                  or me in members[ra] or me in members[rb]
                                  or any(forbid[x] & members[rb] for x in members[ra] if x in forbid)):
            continue
        parent[ra] = rb
        members[rb] |= members.pop(ra)
        firsts[rb] |= firsts.pop(ra)
    clusters = [sorted(c) for c in members.values() if len(c) > 1]
    ids = [n for c in clusters for n in c]
    degree: Counter = Counter()
    for n, k in s.execute(text("""
        SELECT src, count(*) FROM edges WHERE type = 'SENT' AND src = ANY(:ids) GROUP BY src
        UNION ALL SELECT dst, count(*) FROM edges WHERE type = 'RECEIVED' AND dst = ANY(:ids) GROUP BY dst
    """), {"ids": ids}) if ids else ():
        degree[n] += k
    heads, shown = {}, {}
    for c in clusters:
        head = min(c, key=lambda n: (n != me, -degree[n], meta[n].key))
        heads.update({n: head for n in c})
        # Every person is labeled by their best human name in the cluster, never an address or number when a name is
        # known: a real first-and-last name that isn't an address handle ("jlee2026") first, then contact cards, then
        # the best-connected identity. Your own cluster shows your name in place of "Me".
        handles = {meta[n].key.split("@")[0].lower() for n in c if "@" in meta[n].key}
        names = sorted((n for n in c if n != me and own[n] and "@" not in own[n]
                        and not re.fullmatch(r"\+?\d[\d\s()-]+", own[n])),
                       key=lambda n: (not _human_name(own[n], handles),
                                      (meta[n].props or {}).get("named_by") != "contacts", -degree[n], meta[n].key))
        if names and own[names[0]] != own[head]:
            shown[head] = own[names[0]]

    existing = dict(s.execute(text("""
        SELECT c.node_id, c.canonical_id FROM canonical c JOIN nodes n ON n.id = c.node_id AND n.type = 'Person'
    """)).all())
    if gone := [n for n in existing if n not in heads]:
        s.execute(text("DELETE FROM canonical WHERE node_id = ANY(:ids)"), {"ids": gone})
    if moved := [{"n": n, "c": h} for n, h in heads.items() if existing.get(n) != h]:
        s.execute(text("""
            INSERT INTO canonical (node_id, canonical_id) VALUES (:n, :c)
            ON CONFLICT (node_id) DO UPDATE SET canonical_id = EXCLUDED.canonical_id
        """), moved)

    renamed, restored = [], []
    for n, m in meta.items():
        props = m.props or {}
        if n in shown:
            if (m.name, props.get("shown_name"), props.get("own_name")) != (shown[n], shown[n], own[n]):
                renamed.append({"id": n, "name": shown[n], "own": own[n]})
        elif "shown_name" in props or "own_name" in props:
            restored.append({"id": n, "name": own[n]})
    if renamed:
        s.execute(text("""
            UPDATE nodes SET name = :name,
                props = props || jsonb_build_object('own_name', CAST(:own AS text), 'shown_name', CAST(:name AS text))
            WHERE id = :id
        """), renamed)
    if restored:
        s.execute(text("UPDATE nodes SET name = :name, props = props - 'own_name' - 'shown_name' WHERE id = :id"),
                  restored)
    return len(clusters)


def undo(s: Session) -> int:
    """Delete the links scoring made (SAME_AS, extractor 'er') and rebuild the clusters from the links that remain
    (contact cards, your accounts, merges you made); returns how many it deleted. The caller publishes."""
    n = s.execute(text("DELETE FROM edges WHERE type = 'SAME_AS' AND extractor = :ex"), {"ex": EXTRACTOR}).rowcount
    refresh_canonical(s)
    return n
