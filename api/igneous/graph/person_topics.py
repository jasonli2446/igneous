"""What you talk about with someone: the topics of your conversations with one person, recent first
(`GET /graph/node/{id}/topics`, the MCP tool `topics`).

The records are the person's conversations with you: messages whose class is conversation (graph/item_class.py) that
one of their identities (graph/identity.py) and one of yours both sent or received, the newest RECORDS of them (every
conversation of theirs while no source says who you are). Each record is the mean of its chunks' stored embeddings
(rag/embed.py: nothing is embedded again) less its source's centre, the mean of SAMPLE of the source's records, which
takes out what every record of a source shares (an email's "Subject:" line, a chat day's timestamps and speakers).

Records are clustered by average-linkage agglomeration on cosine similarity, until no two clusters are SIMILAR alike:
- the life threads (graph/threads.py) say what is one thing: the records in one thread (the message is part of it, or
  its group chat is) start as one cluster, and records of two different threads never join;
- two records of one group conversation (a group chat, a thread of three or more people) are GROUP_BOND more alike.
A cluster of MIN_RECORDS or more is a topic. It is linked to its thread when at least THREAD_SHARE of its records are
the thread's and you haven't rejected the pair (graph/visibility.py).

A record weighs what it weighs in closeness (graph/traversal.py `relationships`): 1 now, halving every
HALF_LIFE_DAYS, split among the others on it. A topic with a record in the last LATELY_DAYS is "lately", ranked by
those records' weight; the rest are "earlier", ranked by all of theirs.

Names: with hosted models on and some of a topic's records in opted-in sources (llm.allowed_sources), one cheap-model
call names it from short excerpts of those records alone, under the rule every prompt follows (igneous/llm.py): the
prompt carries no other text and names no node. Names are cached by their exact prompt in llm_cache, and a request
makes at most NAME_BUDGET calls. Otherwise, and whenever a call fails, a topic is named by its distinctive terms: the
words and phrases its records use far more than the person's other records do.

The clusters are kept per (person, graph version) (graph/dirty.py); weights, the lately split and names are worked out
per request.
"""
import hashlib
import logging
import math
import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import source_catalog
from igneous.config import settings
from igneous.graph import dirty, identity
from igneous.graph.traversal import HALF_LIFE_DAYS, label, names, rejected
from igneous.graph.visibility import group_conversation, visible

log = logging.getLogger(__name__)

RECORDS = 2000          # the newest conversations clustered per person
SAMPLE = 2000           # records per source that make its centre
SIMILAR = 0.3           # clusters at least this alike (mean cosine of centred records) merge
GROUP_BOND = 0.3        # added to the likeness of two records of one group conversation
MIN_RECORDS = 2         # records that make a topic
THREAD_SHARE = 0.5      # of a topic's records in a thread, for the topic to be linked to it
LATELY_DAYS = HALF_LIFE_DAYS
LIMIT = 6               # topics per section, by default
ITEMS = 5               # record cards per topic
TERMS = 3               # terms in a name made from them
NAME_BUDGET = 12        # model calls per request
NAME_WORKERS = 4
EXCERPTS = 8            # records a naming prompt quotes
EXCERPT_CHARS = 220
PROMPT_VERSION = "v1"

SYSTEM = (
    "You name one topic of someone's conversations with a person they know. You get short excerpts of the messages "
    "about it, newest first. Reply with the topic's name, 2 to 6 words, the way they would say it: 'the Lisbon trip', "
    "'rent and the landlord', 'CS 401 homework'. Use only what the excerpts say. No dates, no quotation marks.")
SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"],
          "additionalProperties": False}

# Words that say nothing about a topic: function words, greetings and sign-offs, chat filler, mail plumbing, days and
# months. A phrase never starts or ends with one, nor spans one.
STOPWORDS = frozenset("""
a about above after again against ago all almost also although always am among an and another any anybody anyone
anything anyway are aren't around as at away back be because been before being below between both but by can can't
cannot could couldn't did didn't do does doesn't doing don't done down during each either else enough even ever every
everyone everything few for from further get gets getting go goes going gone got gotten had hadn't has hasn't have
haven't having he he'd he'll he's her here here's hers herself him himself his how how's however i i'd i'll i'm i've
if in into is isn't it it'd it'll it's its itself just know knew let let's like likely made make makes many may maybe
me might mine more most much must mustn't my myself need needs never next no nor not nothing now of off often oh ok
okay on once one ones only onto or other others otherwise our ours ourselves out over own per perhaps please put quite
rather re really right said same say says see seen seem seems she she'd she'll she's should shouldn't since so some
somebody someone something sometimes soon still such sure take takes than that that's the their theirs them
themselves then there there's these they they'd they'll they're they've thing things think this those though thought
through thus till to too toward towards try under unless until up upon us use used using very via want wants was
wasn't way ways we we'd we'll we're we've well were weren't what what's whatever when when's where where's whether
which while who who's whoever whom whose why why's will with within without won't would wouldn't yes yet you you'd
you'll you're you've your yours yourself yourselves
hi hey hello dear thanks thank thx regards best cheers sincerely love xoxo lol haha hahaha yeah yep yup nope omg wow
cool great good nice fine sorry sounds awesome amazing totally definitely probably actually also already literally
wait anyway btw fyi asap pls plz
sent wrote original message forwarded subject fw fwd cc bcc attached attachment attachments email emails mail
pm am today tomorrow yesterday tonight finally min mins minute minutes hour hours sec secs second seconds stuff whole
came come comes coming went gone gives give gave given look looks looked looking call called calls tell told ask
asked asks feel felt leave left keep kept start started find found show showed mean meant hear heard talk talked
check checked guess bring brought hope hoping glad happy sent sending send lot lots bit little big
monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thur thurs fri sat sun
january february march april may june july august september october november december jan feb mar apr jun jul aug
sep sept oct nov dec
""".split())
WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|['\u2019-](?=[^\W\d_]))*")  # letters, an apostrophe or hyphen inside
TOKEN = re.compile(r"\w(?:\w|['\u2019-](?=\w))*")  # a word or a number
# A phrase never spans punctuation: , . ; : ! ? brackets, quotes, slashes, bullets, dashes, an ellipsis.
PUNCTUATION = re.compile(r"[,.;:!?()\[\]{}\"\u201c\u201d/|\u2022\u00b7\u2013\u2014\u2026]+|\s-\s")
CHAT_LINE = re.compile(r"^\[\d{1,2}:\d{2}\]\s+[^:\n]{1,80}:\s*", re.M)  # "[14:39] Sam Lee: " opens a chat line
SUBJECT_PREFIX = re.compile(r"^\s*((re|fwd?|aw|sv)\s*:\s*)+", re.I)


# ---------------------------------------------------------------------------------------------------------------
# The records, and their clusters (kept per person and graph version).
# ---------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Record:
    id: int                   # source_items.id
    source: str
    at: datetime
    split: float              # 1 / the others on it: a message in a group of five counts a quarter
    group: int | None         # the group conversation it is in
    thread: int | None        # the life thread it belongs to
    grams: tuple[tuple[str, ...], ...]  # its distinct terms (1 to 3 words), lowercase
    forms: tuple[tuple[tuple[str, ...], str], ...]  # how it writes those it shows how to write (`grams`)
    excerpt: str              # its own text, short, for a naming prompt


@dataclass(frozen=True)
class Cluster:
    members: tuple[int, ...]  # indexes into Clustered.records, nearest the cluster's centre first
    thread: int | None        # the thread it is linked to


@dataclass(frozen=True)
class Clustered:
    person: int
    records: tuple[Record, ...]
    clusters: tuple[Cluster, ...]


RECORDS_SQL = f"""
WITH theirs AS (
    SELECT e.dst AS m FROM edges e WHERE e.src = ANY(CAST(:ids AS bigint[])) AND e.type = 'SENT'
    UNION SELECT e.src FROM edges e WHERE e.dst = ANY(CAST(:ids AS bigint[])) AND e.type = 'RECEIVED'),
shared AS MATERIALIZED (
    SELECT n.id AS node, si.id, si.source, si.occurred_at, si.title, left(si.body_text, 4000) AS body
    FROM theirs JOIN nodes n ON n.id = theirs.m AND n.type = 'Message'
    JOIN source_items si ON si.id = n.source_item_id
    WHERE si.occurred_at IS NOT NULL
      AND COALESCE(n.props->>'item_class', si.item_class, 'conversation') = 'conversation'
      AND (cardinality(CAST(:mine AS bigint[])) = 0
           OR EXISTS (SELECT 1 FROM edges y WHERE y.dst = n.id AND y.type = 'SENT'
                                              AND y.src = ANY(CAST(:mine AS bigint[])))
           OR EXISTS (SELECT 1 FROM edges y WHERE y.src = n.id AND y.type = 'RECEIVED'
                                              AND y.dst = ANY(CAST(:mine AS bigint[]))))
      AND EXISTS (SELECT 1 FROM chunks c WHERE c.source_item_id = si.id AND c.embedding_model = :model)
    ORDER BY si.occurred_at DESC, si.id DESC LIMIT :cap)
SELECT sh.id, sh.source, sh.occurred_at, sh.title, sh.body,
       (SELECT CAST(avg(c.embedding) AS real[]) FROM chunks c
        WHERE c.source_item_id = sh.id AND c.embedding_model = :model) AS vector,
       (SELECT count(DISTINCT COALESCE(k.canonical_id, p.id)) FROM (
            SELECT e.src AS id FROM edges e WHERE e.dst = sh.node AND e.type = 'SENT'
            UNION SELECT e.dst FROM edges e WHERE e.src = sh.node AND e.type = 'RECEIVED') p
        LEFT JOIN canonical k ON k.node_id = p.id) AS people,
       (SELECT min(g.id) FROM edges it JOIN nodes g ON g.id = it.dst AND {group_conversation('g')}
        WHERE it.src = sh.node AND it.type = 'IN_THREAD') AS grp,
       (SELECT array_agg(DISTINCT t.id ORDER BY t.id) FROM (
            SELECT pe.dst AS id, pe.props FROM edges pe WHERE pe.src = sh.node AND pe.type = 'PART_OF'
            UNION ALL
            SELECT pe.dst, pe.props FROM edges it JOIN edges pe ON pe.src = it.dst AND pe.type = 'PART_OF'
            WHERE it.src = sh.node AND it.type = 'IN_THREAD') part
        JOIN nodes t ON t.id = part.id AND t.type = 'Project' AND t.props->>'derived' = 'threads' AND {visible('t')}
         AND (t.props->>'umbrella') IS DISTINCT FROM 'true'
        WHERE (part.props->>'verdict') IS DISTINCT FROM 'drop') AS threads
FROM shared sh
"""

CENTRE_SQL = """
SELECT CAST(avg(c.embedding) AS real[]) FROM (
    SELECT id FROM source_items WHERE source = :source ORDER BY external_id LIMIT :n) si
JOIN chunks c ON c.source_item_id = si.id AND c.embedding_model = :model
"""


def _kind(source: str) -> str:
    stored = source_catalog.BY_VALUE.get(source)
    return stored.kind if stored else ""


def own_text(source: str, title: str | None, body: str | None) -> tuple[str, str]:
    """What a record itself says, as (heading, text): a mail's subject and its text above any quoted reply, a chat
    day's lines without their times and speakers (no heading: its title is the chat and the day), anything else's
    title and text."""
    from igneous.rag.chunking import strip_quoted

    body = body or ""
    kind = _kind(source)
    if kind == "chat":
        return "", CHAT_LINE.sub("", body).strip()
    if kind == "mail":
        return SUBJECT_PREFIX.sub("", title or "").strip(), strip_quoted(body)
    return (title or "").strip(), body.strip()


def excerpt(source: str, heading: str, body: str) -> str:
    """A record's own text on one short line: a chat day's lines joined by ' / ', anything else's heading, then its
    text."""
    lines = [" ".join(line.split()) for line in body.splitlines()]
    lines = [line for line in lines if line]
    joined = (" / " if _kind(source) == "chat" else " ").join(lines)
    if heading:
        joined = f"{heading}: {joined}" if joined else heading
    return joined if len(joined) <= EXCERPT_CHARS else joined[:EXCERPT_CHARS - 1].rstrip() + "…"


def grams(own: str, skip: frozenset[str]) -> tuple[tuple[tuple[str, ...], ...], dict[tuple[str, ...], str | None]]:
    """The distinct terms of a text, lowercase: 1 to 3 adjacent words (of three letters or more, or a two-letter
    acronym) with no stopword, number or name in `skip` among them, and course codes ("CS 401"). With each, how the
    text writes it: in lowercase when it ever does, as written when it capitalizes it where a phrase doesn't start
    (a name: "Boulder Barn", "Lisbon"), else None (only ever at the start of a phrase, which says nothing)."""
    from igneous.graph.threads import course_codes

    found: dict[tuple[str, ...], str | None] = {}
    for line in own.splitlines():
        for code in course_codes(line):
            found.setdefault((code.lower(),), code)
        for part in PUNCTUATION.split(line):
            _phrases(part, skip, found)
    return tuple(sorted(found)), found


def _phrases(part: str, skip: frozenset[str], found: dict[tuple[str, ...], str | None]) -> None:
    """Add to `found` the terms of a stretch of text with no punctuation in it (see `grams`)."""
    run: list[tuple[int, str]] = []  # (position in the stretch, word)
    for position, token in enumerate([*TOKEN.findall(part), ""]):
        word = token.replace("\u2019", "'")
        if word.lower().endswith("'s"):
            word = word[:-2]
        low = word.lower()
        short = len(low) < 3 and not (len(word) == 2 and word.isupper())
        if short or not WORD.fullmatch(word) or low in STOPWORDS or low in skip:
            run = []
            continue
        run.append((position, word))
        for n in (1, 2, 3):
            if len(run) < n:
                continue
            words = run[-n:]
            gram = tuple(w.lower() for _, w in words)
            written = " ".join(w for _, w in words)
            if written == written.lower():
                found[gram] = written
            elif found.get(gram) is None and any(
                    (at > 0 and w != w.lower()) or (len(w) > 1 and w.isupper()) for at, w in words):
                found[gram] = written
            else:
                found.setdefault(gram, None)


def _centre(s: Session, source: str) -> np.ndarray | None:
    """The mean embedding of SAMPLE of a source's records (by external id: the same sample every time), kept per
    graph version."""
    def compute():
        v = s.execute(text(CENTRE_SQL), {"source": source, "n": SAMPLE, "model": settings.embedding_model}).scalar()
        return np.asarray(v, dtype=np.float64) if v else None
    return dirty.cached(s, ("person-topics-centre", source, settings.embedding_model), compute)


def agglomerate(sim: np.ndarray, labels: list[int | None], threshold: float = SIMILAR) -> list[list[int]]:
    """Average-linkage clusters of the items `sim` compares (a symmetric matrix of likeness), merged while two clusters
    are at least `threshold` alike on average. Items with one label start as one cluster, and items with different
    labels never join. Ties go to the lowest index: the same input gives the same clusters."""
    n = len(sim)
    sim = np.array(sim, dtype=np.float64)
    lab = np.array([-1 if x is None else x for x in labels])
    sim[(lab[:, None] >= 0) & (lab[None, :] >= 0) & (lab[:, None] != lab[None, :])] = -np.inf
    np.fill_diagonal(sim, -np.inf)
    size = np.ones(n)
    alive = np.ones(n, dtype=bool)
    members = {i: [i] for i in range(n)}

    def merge(a: int, b: int) -> None:
        # Lance-Williams for the average: a cluster's likeness to another is the size-weighted mean of its parts'.
        # -inf (two threads) stays -inf, so a cluster holding a thread's records never takes another thread's.
        row = (size[a] * sim[a] + size[b] * sim[b]) / (size[a] + size[b])
        sim[a], sim[:, a] = row, row
        sim[a, a] = -np.inf
        sim[b], sim[:, b] = -np.inf, -np.inf
        size[a] += size[b]
        alive[b] = False
        members[a] += members.pop(b)
        if lab[a] < 0:
            lab[a] = lab[b]

    first: dict[int, int] = {}
    for i in range(n):
        if lab[i] >= 0:
            if lab[i] in first:
                merge(first[lab[i]], i)
            else:
                first[lab[i]] = i
    if n > 1:
        best = np.argmax(sim, axis=1)
        value = sim[np.arange(n), best]
        value[~alive] = -np.inf
        while True:
            i = int(np.argmax(value))
            if not value[i] >= threshold:
                break
            a, b = sorted((i, int(best[i])))
            merge(a, b)
            value[b] = -np.inf
            # The merged row is new. Any other row's likeness to it is an average of its likeness to the two parts,
            # never above the larger: only rows whose best was one of them need a look.
            for k in [a, *np.nonzero(alive & ((best == a) | (best == b)))[0]]:
                best[k] = int(np.argmax(sim[k]))
                value[k] = sim[k, best[k]]
    return sorted((sorted(m) for m in members.values()), key=lambda m: (-len(m), m[0]))


def _skip_words(s: Session, ids: list[int]) -> frozenset[str]:
    """The words of the names of `ids` (a person's and your identities): a chat's speakers and a mail's greeting are
    no topic."""
    rows = s.execute(text("SELECT name FROM nodes WHERE id = ANY(CAST(:ids AS bigint[]))"), {"ids": ids}).scalars()
    return frozenset(w.lower() for name in rows for w in WORD.findall(name or ""))


def cluster(s: Session, head: int) -> Clustered:
    """The person's conversations with you, clustered (see the module docstring). `head` is a Person's identity
    head."""
    ids = identity.members(s, head)
    owner = identity.self_id(s)
    mine = identity.members(s, owner) if owner is not None and owner != head else []
    rows = s.execute(text(RECORDS_SQL), {"ids": ids, "mine": mine, "cap": RECORDS,
                                         "model": settings.embedding_model}).mappings().all()
    rows = sorted((r for r in rows if r["vector"]), key=lambda r: (r["occurred_at"], r["id"]))
    if not rows:
        return Clustered(head, (), ())
    vectors = np.asarray([r["vector"] for r in rows], dtype=np.float64)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    for source in sorted({r["source"] for r in rows}):
        centre = _centre(s, source)
        if centre is not None and np.linalg.norm(centre):
            at = [i for i, r in enumerate(rows) if r["source"] == source]
            vectors[at] -= centre / np.linalg.norm(centre)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = np.divide(vectors, norms, out=np.zeros_like(vectors), where=norms > 0)
    sim = vectors @ vectors.T
    groups = np.asarray([r["grp"] if r["grp"] is not None else -1 for r in rows])
    sim += GROUP_BOND * ((groups[:, None] == groups[None, :]) & (groups[:, None] >= 0))
    # A record in two threads counts for the one more of this person's records are in.
    count = Counter(t for r in rows for t in r["threads"] or [])
    threads = [min(r["threads"], key=lambda t: (-count[t], t)) if r["threads"] else None for r in rows]

    skip = _skip_words(s, ids + mine)
    records = []
    for r, thread in zip(rows, threads, strict=True):
        heading, body = own_text(r["source"], r["title"], r["body"])
        found, forms = grams(f"{heading}\n{body}", skip)
        records.append(Record(r["id"], r["source"], r["occurred_at"], 1.0 / max(1, (r["people"] or 2) - 1),
                              r["grp"], thread, found, tuple(sorted((g, w) for g, w in forms.items() if w)),
                              excerpt(r["source"], heading, body)))
    clusters = []
    for members in agglomerate(sim, threads):
        if len(members) < MIN_RECORDS:
            continue
        centre = vectors[members].mean(axis=0)
        nearest = sorted(members, key=lambda i: (-float(vectors[i] @ centre), -rows[i]["occurred_at"].timestamp(),
                                                 rows[i]["id"]))
        thread = next((threads[i] for i in members if threads[i] is not None), None)
        if thread is not None and sum(threads[i] == thread for i in members) < THREAD_SHARE * len(members):
            thread = None
        clusters.append(Cluster(tuple(nearest), thread))
    return Clustered(head, tuple(records), tuple(clusters))


def clustered(s: Session, head: int) -> Clustered:
    """`cluster`, kept per graph version."""
    return dirty.cached(s, ("person-topics", head), lambda: cluster(s, head))


# ---------------------------------------------------------------------------------------------------------------
# Names.
# ---------------------------------------------------------------------------------------------------------------

def terms(records: tuple[Record, ...], members: list[int], pool: list[int], k: int = TERMS) -> list[str]:
    """The terms that set `members` apart from the rest of `pool` (indexes into `records`), most distinctive first:
    each scored by the share of the members that use it times how rare it is in the pool. A longer phrase stands for
    its words when nearly as many members use it, and no two terms share a word."""
    if not members:
        return []
    df_pool = Counter(g for i in pool for g in records[i].grams)
    df = Counter(g for i in members for g in records[i].grams)
    floor = 2 if len(members) >= 2 and any(n >= 2 for n in df.values()) else 1
    candidates = {g: n for g, n in df.items() if n >= floor}
    for g, n in list(candidates.items()):
        for size in range(1, len(g)):
            for start in range(len(g) - size + 1):
                part = g[start:start + size]
                if part in candidates and n >= 0.8 * candidates[part]:
                    candidates[part] = 0  # the phrase says it better
    candidates = {g: n for g, n in candidates.items() if n}
    ranked = sorted(candidates, key=lambda g: (-(candidates[g] / len(members)) * math.log(1 + len(pool) / df_pool[g]),
                                               -len(g), g))
    picked: list[tuple[str, ...]] = []
    for g in ranked:
        if not any(set(g) & set(p) for p in picked):
            picked.append(g)
        if len(picked) == k:
            break
    return [_spelling(records, members, g) for g in picked]


def _spelling(records: tuple[Record, ...], members: list[int], gram: tuple[str, ...]) -> str:
    """How the members write a term: as a name when they capitalize it and never write it in lowercase ("Boulder
    Barn"), else in lowercase."""
    written = Counter(form for i in members for g, form in records[i].forms if g == gram)
    if not written or any(form == form.lower() for form in written):
        return " ".join(gram)
    return max(written, key=lambda f: (written[f], f))


def name_from_terms(found: list[str]) -> str | None:
    return " · ".join(found) if found else None


def prompt(records: tuple[Record, ...], members: tuple[int, ...], sources: frozenset[str]) -> tuple[str, list[int]]:
    """A naming prompt's user message, from the EXCERPTS members nearest the cluster's centre whose source is in
    `sources`, newest first, and the records it quotes. ('', []) when none may be quoted."""
    quoted = [i for i in members if records[i].source in sources and records[i].excerpt][:EXCERPTS]
    quoted.sort(key=lambda i: (records[i].at, records[i].id), reverse=True)
    if not quoted:
        return "", []
    lines = [f"- {records[i].at:%Y-%m-%d}: {records[i].excerpt}" for i in quoted]
    return "Excerpts:\n" + "\n".join(lines), [records[i].id for i in quoted]


def cache_key(user: str) -> str:
    return f"person-topic:{PROMPT_VERSION}:" + hashlib.sha256(f"{SYSTEM}\n{user}".encode()).hexdigest()


def _model_name(system: str, user: str) -> str:
    from igneous.llm import structured

    out, _ = structured(system, user, SCHEMA, max_tokens=60, model=settings.llm_cheap_model)
    return str(out.get("name") or "")


def clean_name(name: str) -> str:
    name = " ".join(str(name).split()).strip(" \"'\u201c\u201d\u2018\u2019.")
    return name[:80]


def model_names(prompts: dict[int, str], model: Callable[[str, str], str] = _model_name, may_call: bool = True,
                budget: int | None = None) -> dict[int, str]:
    """{topic: name} for the topics whose prompt (`prompts`, topic -> user message) has a cached name, or gets one
    from `model` within `budget` calls (NAME_BUDGET by default; in parallel, NAME_WORKERS at a time) when `may_call`.
    A name the model gives is cached as it arrives; a call that fails leaves its topic out. Cache reads and writes
    happen on this thread."""
    from igneous.llm import LLMDisabled, cache_lookup, cache_store

    budget = NAME_BUDGET if budget is None else budget
    named: dict[int, str] = {}
    todo: list[tuple[int, str]] = []
    for topic, user in prompts.items():
        try:
            hit = cache_lookup(cache_key(user))
        except Exception as e:  # a cache we can't read is a name we don't have: never break the inspector
            log.info("topic name cache unreadable: %s", type(e).__name__)
            hit = None
        if hit and hit.get("name"):
            named[topic] = hit["name"]
        elif may_call and len(todo) < budget:
            todo.append((topic, user))
    if not todo:
        return named
    with ThreadPoolExecutor(min(NAME_WORKERS, len(todo))) as pool:
        futures = [(topic, user, pool.submit(model, SYSTEM, user)) for topic, user in todo]
        for topic, user, future in futures:
            try:
                name = clean_name(future.result())
            except LLMDisabled:
                break  # off or not configured: every other call would fail the same way
            except Exception as e:  # unavailable, or no usable answer: this topic keeps its terms
                log.info("topic %s not named: %s: %s", topic, type(e).__name__, str(e)[:120])
                continue
            if name:
                named[topic] = name
                try:
                    cache_store(cache_key(user), {"name": name})
                except Exception as e:  # a name we can't cache is still its name
                    log.info("topic name not cached: %s", type(e).__name__)
    return named


# ---------------------------------------------------------------------------------------------------------------
# The reader.
# ---------------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Topic:
    cluster: Cluster
    newest: tuple[int, ...]   # its records, newest first (indexes into Clustered.records)


@dataclass(frozen=True)
class Topics:
    person: dict              # the Person (identity.node)
    clustered: Clustered
    lately: tuple[Topic, ...]   # ranked by the weight of their records since `since`
    earlier: tuple[Topic, ...]  # ranked by the weight of all their records
    since: datetime


def _person(s: Session, node_id: int) -> dict:
    """The Person `node_id` is (its identity head). Raises identity.NotFound, and ValueError for anything but a person
    other than you."""
    n = identity.node(s, node_id)
    if n["type"] != "Person":
        raise ValueError(f"Node {n['id']} is a {n['type']}: topics are what you talk about with a person.")
    if n["id"] == identity.self_id(s):
        raise ValueError("That's you: open someone else to see what the two of you talk about.")
    return n


def weight(r: Record, now: datetime) -> float:
    """What a record weighs in closeness: 1 now, halving every HALF_LIFE_DAYS, split among the others on it."""
    age = max(0.0, (now - r.at).total_seconds() / 86400)
    return r.split * 0.5 ** (age / HALF_LIFE_DAYS)


def rank(c: Clustered, now: datetime) -> tuple[tuple[Topic, ...], tuple[Topic, ...], datetime]:
    """The topics, lately and earlier, each ranked, and when lately starts (LATELY_DAYS before `now`)."""
    since = now - timedelta(days=LATELY_DAYS)
    lately, earlier = [], []
    for cl in c.clusters:
        newest = tuple(sorted(cl.members, key=lambda i: (c.records[i].at, c.records[i].id), reverse=True))
        last = c.records[newest[0]]
        tie = (-last.at.timestamp(), last.id)
        if last.at >= since:
            lately.append(((-sum(weight(c.records[i], now) for i in newest if c.records[i].at >= since), *tie),
                           Topic(cl, newest)))
        else:
            earlier.append(((-sum(weight(c.records[i], now) for i in newest), *tie), Topic(cl, newest)))
    return (tuple(t for _key, t in sorted(lately, key=lambda x: x[0])),
            tuple(t for _key, t in sorted(earlier, key=lambda x: x[0])), since)


def topics(s: Session, node_id: int, now: datetime | None = None) -> Topics:
    """A person's topics, ranked (see the module docstring). Raises identity.NotFound, and ValueError when the node is
    not a person or is you."""
    person = _person(s, node_id)
    c = clustered(s, person["id"])
    lately, earlier, since = rank(c, now or datetime.now(UTC))
    return Topics(person, c, lately, earlier, since)


def named_by_model(c: Clustered, shown: list[Topic], sources: frozenset[str], may_call: bool,
                   model: Callable[[str, str], str] = _model_name) -> dict[int, str]:
    """{index in `shown`: name} for the topics a model named (cached) or names now (when `may_call`), each from
    excerpts of its records in `sources` only."""
    prompts = {}
    for t, topic in enumerate(shown):
        user, _quoted = prompt(c.records, topic.cluster.members, sources)
        if user:
            prompts[t] = user
    return model_names(prompts, model, may_call=may_call) if prompts else {}


def topic_key(c: Clustered, topic: Topic) -> str:
    """A topic's key in an answer: the same records, the same key."""
    ids = ",".join(str(i) for i in sorted(c.records[i].id for i in topic.newest))
    return hashlib.sha1(ids.encode()).hexdigest()[:12]


def person_topics(s: Session, node_id: int, limit: int = LIMIT, named: dict[str, str] | None = None,
                  model: Callable[[str, str], str] = _model_name, now: datetime | None = None) -> dict:
    """What you talk about with a person: {person, since, lately, earlier, more, records}. `lately` and `earlier` hold
    up to `limit` topics each: {key, name, named_by ('model' or 'terms'), terms, records, recent, first, last,
    sources, thread ({id, name}, or None), conversations (the group conversations it happened in), items (the newest
    ITEMS record cards), item_ids (all its records, newest first)}; `more` counts the topics left out of each, and
    `records` the conversations clustered. A phone number reads as its contact (`named`, traversal.names by default).
    `model(system, user) -> str` is injectable for tests. Raises identity.NotFound, and ValueError when the node is
    not a person or is you."""
    from igneous.items import CARD_COLS, item_card
    from igneous.llm import allowed_sources

    found = topics(s, node_id, now)
    c, person = found.clustered, found.person
    named = names(s) if named is None else named
    shown = [*found.lately[:limit], *found.earlier[:limit]]
    by_model = named_by_model(c, shown, allowed_sources(), settings.llm_enabled, model)

    blocked = rejected(s)
    threads = sorted({t.cluster.thread for t in shown if t.cluster.thread is not None})
    thread_names = dict(s.execute(text("SELECT id, name FROM nodes WHERE id = ANY(CAST(:ids AS bigint[]))"),
                                  {"ids": threads}).all()) if threads else {}
    card_ids = sorted({c.records[i].id for t in shown for i in t.newest[:ITEMS]})
    cards = {r["id"]: item_card(dict(r), named) for r in s.execute(text(
        f"SELECT {CARD_COLS} FROM source_items si WHERE si.id = ANY(CAST(:ids AS bigint[]))"),
        {"ids": card_ids}).mappings()} if card_ids else {}
    group_ids = sorted({c.records[i].group for t in shown for i in t.newest if c.records[i].group is not None})
    groups = {r.id: label(r.name, "Thread", named) for r in s.execute(text(
        "SELECT id, name FROM nodes WHERE id = ANY(CAST(:ids AS bigint[]))"), {"ids": group_ids})} if group_ids else {}
    pool = list(range(len(c.records)))

    def view(t: int, topic: Topic) -> dict:
        recs = [c.records[i] for i in topic.newest]
        words = terms(c.records, list(topic.newest), pool)
        name = by_model.get(t) or name_from_terms(words) or c.records[topic.cluster.members[0]].excerpt[:60]
        thread = topic.cluster.thread
        held = Counter(r.group for r in recs if r.group in groups)
        return {"key": topic_key(c, topic), "name": name or "Untitled",
                "named_by": "model" if t in by_model else "terms", "terms": words,
                "records": len(recs), "recent": sum(r.at >= found.since for r in recs),
                "first": recs[-1].at.isoformat(), "last": recs[0].at.isoformat(),
                "sources": sorted({r.source for r in recs}),
                "thread": {"id": thread, "name": label(thread_names.get(thread), "Project", named)}
                if thread is not None and frozenset({person["id"], thread}) not in blocked else None,
                "conversations": [{"id": g, "name": groups[g], "records": n}
                                  for g, n in sorted(held.items(), key=lambda x: (-x[1], x[0]))[:3]],
                "items": [cards[r.id] for r in recs[:ITEMS] if r.id in cards],
                "item_ids": [r.id for r in recs]}

    views = [view(t, topic) for t, topic in enumerate(shown)]
    n = min(limit, len(found.lately))
    return {"person": {"id": person["id"], "name": label(person["name"], "Person", named)},
            "since": found.since.isoformat(), "lately": views[:n], "earlier": views[n:],
            "more": {"lately": max(0, len(found.lately) - limit), "earlier": max(0, len(found.earlier) - limit)},
            "records": len(c.records)}
