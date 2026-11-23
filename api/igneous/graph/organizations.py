"""Where people work: WORKED_AT edges from people to Organization nodes, from evidence only, precision first.

  - Your own address at an organization's domain (you@employer.com) says you worked there, provided you wrote with
    someone else there too: a domain only you use is not an organization.
  - A sustained two-way exchange with three or more people at one domain, two weeks or more from first to last
    message, says you worked there (or closely with them); one-off contact never does.
  - At those domains, everyone you wrote to and heard back from, at their address there, works there too.
  - A contact card's company: the person on the card works there, matched by every address and phone number on it.

Each person and organization gets one fact, carrying every reason and evidence item behind it; its dates
(valid_from, valid_to) are the first and last activity behind it (a contact card has none).

One organization is one node, whichever stage or record meets it first. A company is keyed by its name, `org:<slug>`,
as LinkedIn, contact cards, model mentions and your resume key it, and its email domains are its `props.domains`. A
domain belongs to a company: the one your resume gives it (graph/profile.py records the claim), else the company its
own people name on their contact cards and LinkedIn connections, else the one named by a cached cheap-model call per
domain reading how its people sign their mail in opted-in sources, when your records name that company too (LinkedIn,
a card, a mention, your resume). A domain no company claims is an organization of its own, keyed by the domain and
named by the model or by the domain itself. Which key a domain takes depends on the records alone, never on what an
earlier build left, so two builds of the same data give the same organizations. A name your resume gave an
organization always stays, and so does the name a company's own records give it.

The stage owns its own rows, written through graph/derived.py's Writer (a person and an organization you said aren't
connected never are again), and `props.domains` on every organization.
"""
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.graph import identity
from igneous.graph.derived import Writer

EXTRACTOR = "organizations"
VERSION = "2"
MIN_PEOPLE = 3                         # people at a domain you exchange mail with, both ways...
SUSTAINED = timedelta(days=14)         # ...from first to last message at least this long
EVIDENCE = 6                           # evidence items kept per fact (the most recent)
NAME_CONFIDENCE = 0.6                  # a model's name for a domain below this is not used
NEVER = datetime.min.replace(tzinfo=UTC)

# Consumer mailbox providers are identities, not employers.  Keep this list
# small and explicit: an unknown domain is preferable to a false organization.
PERSONAL_DOMAINS = frozenset({
    "aol.com", "gmail.com", "hotmail.com", "icloud.com", "live.com", "me.com",
    "msn.com", "outlook.com", "proton.me", "protonmail.com", "yahoo.com",
})
# The WORKED_AT edges from you that make an organization yours: your resume or LinkedIn profile (graph/profile.py),
# what you said, and what this stage finds from your own mail.
YOUR_ORGS = ("profile", "user", EXTRACTOR)


def domain_of(value: str | None) -> str | None:
    """Return a usable work-domain candidate, never a personal-mail domain."""
    if not isinstance(value, str) or "@" not in value:
        return None
    domain = value.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if not domain or domain in PERSONAL_DOMAINS or "." not in domain:
        return None
    return domain


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def company_key(name: str | None) -> tuple[str, str] | None:
    """A company name's key, `org:<slug>` (how LinkedIn companies and resume organizations are keyed too), and its
    display spelling; None for text that can't be a company name."""
    if not isinstance(name, str):
        return None
    display = re.sub(r"\s+", " ", name).strip()
    key = slug(display)
    if len(key) < 2 or len(key) > 100:
        return None
    return f"org:{key}", display


def _kind(domains: list[str], name: str) -> str:
    """'school' for an organization at an academic domain or named like one, else 'employer'."""
    school = any(re.search(r"(^|\.)(edu|ac)(\.[a-z]{2})?$", d) for d in domains) or re.search(
        r"\b(university|college|school|institute|academy)\b", name, re.I)
    return "school" if school else "employer"


@dataclass(frozen=True)
class Fact:
    person: int              # an identity head
    key: str                 # the organization: a domain or org:<slug>
    why: str
    sid: int | None          # the evidence item
    at: datetime | None      # when it happened (None for a contact card)
    confidence: float
    spelling: str | None = None  # how the evidence spells the company (org:<slug> keys)


def _exchanges(s: Session, mine: list[int]) -> list:
    """Every message between you and someone at a work address: (direction, address id, key, head, item, date)."""
    return s.execute(text("""
        WITH mine AS (SELECT unnest(CAST(:mine AS bigint[])) AS id),
        msgs AS (
            SELECT 'out' AS dir, r.dst AS addr, m.source_item_id AS sid
            FROM edges snd JOIN edges r ON r.src = snd.dst AND r.type = 'RECEIVED' JOIN nodes m ON m.id = snd.dst
            WHERE snd.type = 'SENT' AND snd.src IN (SELECT id FROM mine) AND r.dst NOT IN (SELECT id FROM mine)
            UNION
            SELECT 'in', snd.src, m.source_item_id
            FROM edges snd JOIN edges r ON r.src = snd.dst AND r.type = 'RECEIVED' JOIN nodes m ON m.id = snd.dst
            WHERE snd.type = 'SENT' AND r.dst IN (SELECT id FROM mine) AND snd.src NOT IN (SELECT id FROM mine))
        SELECT msgs.dir, p.id, p.key, COALESCE(c.canonical_id, p.id) AS head, msgs.sid, si.occurred_at
        FROM msgs JOIN nodes p ON p.id = msgs.addr AND p.type = 'Person' AND p.key LIKE '%@%'
             AND COALESCE(p.props->>'kind', '') <> 'service'
        LEFT JOIN canonical c ON c.node_id = p.id
        JOIN source_items si ON si.id = msgs.sid
        ORDER BY si.occurred_at, si.id
    """), {"mine": mine}).all()


def _own_activity(s: Session, mine: list[int]) -> dict[str, list[tuple[int, datetime | None]]]:
    """Your own addresses -> the items they sent or received, oldest first."""
    out: dict[str, list] = defaultdict(list)
    for key, sid, at in s.execute(text("""
        SELECT p.key, m.source_item_id, si.occurred_at FROM nodes p
        JOIN edges e ON e.src = p.id AND e.type = 'SENT' JOIN nodes m ON m.id = e.dst
        JOIN source_items si ON si.id = m.source_item_id
        WHERE p.id = ANY(:mine) AND p.key LIKE '%@%'
        UNION
        SELECT p.key, m.source_item_id, si.occurred_at FROM nodes p
        JOIN edges e ON e.dst = p.id AND e.type = 'RECEIVED' JOIN nodes m ON m.id = e.src
        JOIN source_items si ON si.id = m.source_item_id
        WHERE p.id = ANY(:mine) AND p.key LIKE '%@%'
        ORDER BY 3, 2
    """), {"mine": mine}):
        out[key].append((sid, at))
    for key in s.execute(text("SELECT key FROM nodes WHERE id = ANY(:mine) AND key LIKE '%@%'"),
                         {"mine": mine}).scalars():
        out.setdefault(key, [])
    return out


def _mail_facts(s: Session) -> list[Fact]:
    """Your employers, from your own addresses and sustained exchanges, and the people you wrote with there."""
    me = identity.self_id(s)
    if me is None:
        return []
    mine = identity.members(s, me)
    by_address: dict[int, dict] = {}
    for direction, addr, key, head, sid, at in _exchanges(s, mine):
        a = by_address.setdefault(addr, {"key": key, "head": head, "in": False, "out": False, "items": []})
        a[direction] = True
        a["items"].append((sid, at))
    correspondents = {domain_of(a["key"]) for a in by_address.values()} - {None}
    # Two-way exchanges, per domain and per person there (an identity head).
    two_way: dict[str, dict[int, list[tuple[str, int, datetime]]]] = defaultdict(lambda: defaultdict(list))
    for a in by_address.values():
        if a["in"] and a["out"] and (domain := domain_of(a["key"])):
            two_way[domain][a["head"]] += [(a["key"], sid, at) for sid, at in a["items"]]

    yours: dict[str, list[tuple[str, int | None, datetime | None]]] = defaultdict(list)  # domain -> evidence
    for domain, people in two_way.items():
        dates = [at for items in people.values() for _k, _sid, at in items if at is not None]
        if len(people) >= MIN_PEOPLE and dates and max(dates) - min(dates) >= SUSTAINED:
            why = f"two-way mail with {len(people)} people at {domain}"
            yours[domain] += [(why, sid, at) for items in people.values() for _k, sid, at in items]
    for key, items in _own_activity(s, mine).items():
        domain = domain_of(key)
        if domain and domain in correspondents:
            yours[domain] += [(f"your address {key}", sid, at) for sid, at in items] or [(f"your address {key}",
                                                                                           None, None)]
    facts = [Fact(me, domain, why, sid, at, 0.8) for domain, evidence in yours.items() for why, sid, at in evidence]
    for domain in yours:
        for head, items in two_way.get(domain, {}).items():
            facts += [Fact(head, domain, f"wrote with you from {key}", sid, at, 0.7) for key, sid, at in items]
    return facts


def _card_facts(s: Session) -> list[Fact]:
    """The company on each contact card, for the person on it (every address and phone number on the card)."""
    cards = s.execute(text("""
        SELECT id, raw_meta FROM source_items WHERE source IN ('gcontacts', 'maccontacts')
          AND jsonb_array_length(COALESCE(raw_meta->'orgs', '[]'::jsonb)) > 0 ORDER BY id
    """)).all()
    keys = {sid: identity.card_keys(meta) for sid, meta in cards}
    heads = dict(s.execute(text("""
        SELECT p.key, COALESCE(c.canonical_id, p.id) FROM nodes p LEFT JOIN canonical c ON c.node_id = p.id
        WHERE p.type = 'Person' AND p.key = ANY(:keys) AND COALESCE(p.props->>'kind', '') <> 'service'
    """), {"keys": sorted({k for ks in keys.values() for k in ks})}).all())
    facts = []
    for sid, meta in cards:
        for person in {heads[k] for k in keys[sid] if k in heads}:
            for org in meta.get("orgs") or []:
                if company := company_key(org):
                    facts.append(Fact(person, company[0], "contact card", sid, None, 0.9, company[1]))
    return facts


def _own_words(s: Session, domains: set[str]) -> dict[str, Counter]:
    """For each domain: the company names its people give, on their contact cards and LinkedIn connections."""
    words: dict[str, Counter] = defaultdict(Counter)
    for source, meta in s.execute(text("""
        SELECT source, raw_meta FROM source_items
        WHERE source IN ('gcontacts', 'maccontacts')
           OR (source = 'linkedin' AND raw_meta->>'kind' = 'connection')
    """)):
        if source == "linkedin":
            addresses, companies = [meta.get("email") or ""], [meta.get("company") or ""]
        else:
            addresses, companies = meta.get("emails") or [], meta.get("orgs") or []
        for domain in {domain_of(a) for a in addresses} & domains:
            for company in companies:
                if found := company_key(company):
                    words[domain][found[1]] += 1
    return words


NAME_SYSTEM = ("You name the organization that owns an email domain the way its own people write it ('Acme Robotics', "
               "'State University'), from the domain and how people there sign their messages. If the messages "
               "don't show it, answer with an empty name.")
NAME_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name", "confidence"],
               "properties": {"name": {"type": "string"}, "confidence": {"type": "number"}}}


def model_name(s: Session, domain: str) -> str | None:
    """One cached cheap-model call per domain: its name, from the ends of messages its people sent in opted-in
    sources (where signatures are). None when the model is off, nothing from the domain may be read, or it isn't
    sure."""
    from igneous.llm import LLMDisabled, allowed_sources, cache_lookup, cache_store, structured
    from igneous.rag.chunking import QUOTE_START

    key = f"org-name:v1:{domain}"
    answer = cache_lookup(key)
    if answer is None:
        sources = sorted(allowed_sources())
        if not settings.llm_enabled or not sources:
            return None
        bodies = s.execute(text("""
            SELECT si.body_text FROM nodes p JOIN edges e ON e.src = p.id AND e.type = 'SENT'
            JOIN nodes m ON m.id = e.dst JOIN source_items si ON si.id = m.source_item_id
            WHERE p.type = 'Person' AND p.key LIKE '%@' || :d AND si.source = ANY(CAST(:src AS text[]))
              AND COALESCE(si.body_text, '') <> ''
            ORDER BY si.occurred_at DESC NULLS LAST, si.id LIMIT 5
        """), {"d": domain, "src": sources}).scalars().all()
        own = [body[:m.start()] if (m := QUOTE_START.search(body)) else body for body in bodies]
        endings = [text_.strip()[-300:] for text_ in own if text_.strip()]
        if not endings:
            return None
        try:
            answer, _ = structured(NAME_SYSTEM, f"Domain: {domain}\nHow messages from people there end:\n---\n"
                                   + "\n---\n".join(endings), NAME_SCHEMA, max_tokens=100,
                                   model=settings.llm_cheap_model)
        except LLMDisabled:
            return None
        except Exception:  # named by its domain this run; asked again on the next
            return None
        cache_store(key, answer)
    name = re.sub(r"\s+", " ", answer.get("name") or "").strip()
    return name if name and len(name) <= 80 and (answer.get("confidence") or 0) >= NAME_CONFIDENCE else None


def company_for(s: Session, name: str, domain: str = "") -> tuple[str, str] | None:
    """(key, name) of the organization your resume calls `name`, at `domain` when it gives one: the company that
    domain's own people name on their contact cards and LinkedIn connections, so your resume, their records and the
    domain are one node, else the company `name` is. Named as you name it; None for text that can't be a company's
    name."""
    company = company_key(name)
    if company is None:
        return None
    words = _own_words(s, {domain}).get(domain) if domain else None
    if words and (theirs := company_key(max(words.items(), key=lambda kv: (kv[1], len(kv[0]), kv[0]))[0])):
        return theirs[0], company[1]
    return company


def _resume_claims(s: Session) -> dict[str, tuple[str, str]]:
    """domain -> (organization key, name) for each domain your resume gives an organization: the profile stage, which
    runs first, marks its WORKED_AT link to that organization with the domain (props.domain)."""
    claims: dict[str, tuple[str, str]] = {}
    for domain, key, name in s.execute(text("""
        SELECT lower(e.props->>'domain'), o.key, o.name FROM edges e JOIN nodes o ON o.id = e.dst
        WHERE e.type = 'WORKED_AT' AND e.extractor = 'profile' AND COALESCE(e.props->>'domain', '') <> ''
        ORDER BY e.id
    """)):
        if domain_of(f"x@{domain}") == domain:
            claims.setdefault(domain, (key, name))
    return claims


def _companies(s: Session) -> set[str]:
    """The companies your records name (`org:<slug>` keys) apart from this stage's own findings: a LinkedIn company or
    model mention the spine or extraction made, your resume's organizations, a connection you added. This stage's
    links are gone when it reads them, so an organization it alone made in an earlier build doesn't count."""
    return set(s.execute(text("""
        SELECT n.key FROM nodes n WHERE n.type = 'Organization' AND n.key LIKE 'org:%'
          AND (EXISTS (SELECT 1 FROM edges e WHERE e.src = n.id) OR EXISTS (SELECT 1 FROM edges e WHERE e.dst = n.id)
               OR EXISTS (SELECT 1 FROM spine_node_support x WHERE x.node_id = n.id))
    """)).scalars())


def _domain_names(s: Session, domains: set[str], claims: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """domain -> (name, where it came from): your resume, people's own words, the model, or the domain itself."""
    words = _own_words(s, domains - set(claims))
    names = {}
    for domain in sorted(domains):
        if domain in claims:
            names[domain] = (claims[domain][1], "your resume")
        elif words.get(domain):
            names[domain] = (max(words[domain].items(), key=lambda kv: (kv[1], len(kv[0]), kv[0]))[0],
                             "their own words")
        elif named := model_name(s, domain):
            names[domain] = (named, "how its people sign")
        else:
            names[domain] = (domain, "its domain")
    return names


def _owners(names: dict[str, tuple[str, str]], claims: dict[str, tuple[str, str]],
            companies: set[str]) -> dict[str, str]:
    """domain -> the key of the organization it belongs to: the organization your resume gives it; the company its
    people name; the company the model names, when your records name it too; else the domain itself."""
    owners = {}
    for domain, (name, how) in sorted(names.items()):
        company = company_key(name) if how != "its domain" else None
        if domain in claims:
            owners[domain] = claims[domain][0]
        elif company and (how == "their own words" or company[0] in companies):
            owners[domain] = company[0]
        else:
            owners[domain] = domain
    return owners


def _organization(s: Session, key: str, name: str, named: str | None, kind: str) -> int:
    """The Organization node for `key`, of `kind`: created when missing, its name refreshed when this stage names it
    (`named`: a domain of its own) and your resume didn't, and named when nothing else names it (it shows its key: the
    record that named it is gone)."""
    props = {"kind": kind, "inferred_by": EXTRACTOR, **({"named_from": named} if named else {})}
    return s.execute(text("""
        INSERT INTO nodes (type, key, name, props) VALUES ('Organization', :k, :n, CAST(:p AS jsonb))
        ON CONFLICT (type, key) DO UPDATE SET
            name = CASE WHEN nodes.props->>'named_by' = 'profile' THEN nodes.name
                        WHEN CAST(:named AS text) IS NOT NULL OR nodes.name = nodes.key THEN EXCLUDED.name
                        ELSE nodes.name END,
            props = nodes.props || jsonb_build_object('kind', CAST(:kind AS text))
                    || CASE WHEN CAST(:named AS text) IS NULL THEN '{}'::jsonb
                            ELSE jsonb_build_object('named_from', CAST(:named AS text)) END
        RETURNING id
    """), {"k": key, "n": name, "p": json.dumps(props), "named": named, "kind": kind}).scalar_one()


def run(s: Session) -> dict:
    """Rebuild this stage's WORKED_AT edges from evidence, one per person and organization, key each domain to the
    organization it belongs to (props.domains), and name the domains that are organizations of their own. Returns
    {worked_at, organizations}."""
    s.execute(text("DELETE FROM edges WHERE extractor = :x"), {"x": EXTRACTOR})
    w = Writer(s, EXTRACTOR, VERSION)
    mail = _mail_facts(s)
    cards = _card_facts(s)
    claims = _resume_claims(s)
    names = _domain_names(s, {f.key for f in mail} | set(claims), claims)
    owners = _owners(names, claims, _companies(s) | {f.key for f in cards})
    domains: dict[str, list[str]] = defaultdict(list)  # organization key -> its domains
    for domain, key in sorted(owners.items()):
        domains[key].append(domain)
    grouped: dict[tuple[int, str], list[Fact]] = defaultdict(list)
    for fact in mail:
        grouped[(fact.person, owners[fact.key])].append(fact)
    for fact in cards:
        grouped[(fact.person, fact.key)].append(fact)

    orgs: dict[str, tuple[int, str]] = {}  # key -> (node, kind)
    for (person, key), facts in sorted(grouped.items()):
        if key not in orgs:
            # A domain of its own by the name found for it; a company by the name a domain of its was found by (its
            # own records' name or your resume's stays), else by its most common spelling on the cards.
            named_by = [names[d] for d in domains.get(key, []) if names[d][1] != "its domain"]
            if key in names:
                name, named = names[key]
            elif named_by:
                name, named = named_by[0][0], None
            else:
                name, named = Counter(f.spelling for f in facts if f.spelling).most_common(1)[0][0], None
            kind = _kind(domains.get(key, []), name)
            orgs[key] = (_organization(s, key, name, named, kind), kind)
        dates = [f.at for f in facts if f.at is not None]
        latest: dict[int, datetime | None] = {}
        for f in facts:
            if f.sid is not None and (f.sid not in latest or (f.at and (latest[f.sid] is None or f.at > latest[f.sid]))):
                latest[f.sid] = f.at
        kept = sorted(latest, key=lambda sid: (latest[sid] is not None, latest[sid] or NEVER, sid),
                      reverse=True)[:EVIDENCE]
        props = {"why": sorted({f.why for f in facts}), "inferred": True,
                 **({"school": True} if orgs[key][1] == "school" else {})}
        w.edge(person, orgs[key][0], "WORKED_AT", props, confidence=max(f.confidence for f in facts),
               source_item_id=kept[0] if kept else None, valid_from=min(dates, default=None),
               valid_to=max(dates, default=None),
               evidence=[(sid, why, {"inferred_by": EXTRACTOR})
                         for sid, why in sorted({(f.sid, f.why) for f in facts if f.sid in kept})])
    # Each organization's domains: the ones it has now, and none on an organization that has none any more.
    s.execute(text("""UPDATE nodes SET props = props - 'domains' WHERE type = 'Organization' AND props ? 'domains'
                      AND NOT (key = ANY(:k))"""), {"k": sorted(domains)})
    for key, ds in sorted(domains.items()):
        s.execute(text("""UPDATE nodes SET props = props || jsonb_build_object('domains', CAST(:d AS jsonb))
                          WHERE type = 'Organization' AND key = :k AND props->'domains' IS DISTINCT FROM CAST(:d AS jsonb)"""),
                  {"k": key, "d": json.dumps(ds)})
    # Organizations a derived stage made (this one, or your resume's reading) that nothing points at any more: their
    # evidence is gone, or their domain now belongs to a company.
    s.execute(text("""
        DELETE FROM nodes n WHERE n.type = 'Organization'
          AND (n.props->>'inferred_by' = :x OR n.props->>'named_by' = 'profile')
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = n.id) AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst = n.id)
          AND NOT EXISTS (SELECT 1 FROM spine_node_support x WHERE x.node_id = n.id)
    """), {"x": EXTRACTOR})
    return {"worked_at": len(grouped), "organizations": len(orgs)}
