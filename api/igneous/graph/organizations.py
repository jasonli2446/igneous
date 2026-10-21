"""Conservative, evidence-backed employment organizations.

This stage deliberately derives only ``WORKED_AT`` edges it can explain from a
contact card, correspondence sent by the owner, or a calendar organizer.  It
owns only its rows, so manual edges and entity corrections survive rebuilds.
"""
import json
import re
from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.orm import Session

EXTRACTOR = "organizations"
VERSION = "1"

# Consumer mailbox providers are identities, not employers.  Keep this list
# small and explicit: an unknown domain is preferable to a false organization.
PERSONAL_DOMAINS = frozenset({
    "aol.com", "gmail.com", "hotmail.com", "icloud.com", "live.com", "me.com",
    "msn.com", "outlook.com", "proton.me", "protonmail.com", "yahoo.com",
})


def domain_of(value: str | None) -> str | None:
    """Return a usable work-domain candidate, never a personal-mail domain."""
    if not isinstance(value, str) or "@" not in value:
        return None
    domain = value.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if not domain or domain in PERSONAL_DOMAINS or "." not in domain:
        return None
    return domain


def company_key(name: str | None) -> tuple[str, str] | None:
    """Normalize a contact-card company without treating arbitrary text as one."""
    if not isinstance(name, str):
        return None
    display = re.sub(r"\s+", " ", name).strip()
    key = re.sub(r"[^a-z0-9]+", "-", display.lower()).strip("-")
    if len(key) < 2 or len(key) > 100:
        return None
    return f"company:{key}", display


def _kind(key: str) -> str:
    return "school" if key.endswith(".edu") else "employer"


def _upsert_org(s: Session, key: str, name: str) -> int:
    return s.execute(text("""
        INSERT INTO nodes (type, key, name, props)
        VALUES ('Organization', :key, :name, CAST(:props AS jsonb))
        ON CONFLICT (type, key) DO UPDATE SET props = nodes.props || EXCLUDED.props
        RETURNING id
    """), {"key": key, "name": name,
           "props": json.dumps({"derived": True, "kind": _kind(key), "inferred_by": EXTRACTOR})}).scalar_one()


def _observations(s: Session) -> list[tuple[int, str, str, int, object, str]]:
    """Return (canonical person, org key/name, evidence item/date/reason) facts."""
    facts = []
    # A recipient's domain is organization evidence only when the owner sent
    # the message. Receiving a newsletter or unsolicited mail must not employ
    # its sender in the graph.
    for person, email, sid, occurred_at in s.execute(text("""
        SELECT COALESCE(pc.canonical_id, recipient.id), recipient.key, si.id, si.occurred_at
        FROM nodes mine
        LEFT JOIN canonical mc ON mc.node_id = mine.id
        JOIN edges sent ON sent.type = 'SENT' AND (
            sent.src = COALESCE(mc.canonical_id, mine.id)
            OR sent.src IN (SELECT node_id FROM canonical WHERE canonical_id = COALESCE(mc.canonical_id, mine.id))
        )
        JOIN edges received ON received.src = sent.dst AND received.type = 'RECEIVED'
        JOIN nodes recipient ON recipient.id = received.dst AND recipient.type = 'Person' AND recipient.key LIKE '%@%'
        LEFT JOIN canonical pc ON pc.node_id = recipient.id
        JOIN source_items si ON si.id = sent.source_item_id
        WHERE mine.type = 'Person' AND COALESCE((mine.props->>'self')::bool, false)
          AND COALESCE(pc.canonical_id, recipient.id) <> COALESCE(mc.canonical_id, mine.id)
    """)):
        if (domain := domain_of(email)):
            facts.append((person, domain, domain, sid, occurred_at, "sent correspondence"))

    # Organizer domains corroborate an attendee's affiliation without relying
    # on event text. Do not turn a personal organizer address into an org.
    for person, email, sid, occurred_at in s.execute(text("""
        SELECT COALESCE(pc.canonical_id, attendee.id), si.raw_meta->'organizer'->>'email', si.id, si.occurred_at
        FROM source_items si
        JOIN nodes event ON event.type = 'Event' AND event.key = COALESCE(si.raw_meta->>'event_key',
            'gcal:' || COALESCE(si.raw_meta->>'series_key', si.external_id))
        JOIN edges attended ON attended.dst = event.id AND attended.type = 'ATTENDED'
        JOIN nodes attendee ON attendee.id = attended.src
        LEFT JOIN canonical pc ON pc.node_id = attendee.id
        WHERE si.source = 'gcal'
          AND COALESCE(pc.canonical_id, attendee.id) NOT IN (
              SELECT COALESCE(cself.canonical_id, self.id)
              FROM nodes self LEFT JOIN canonical cself ON cself.node_id = self.id
              WHERE self.type = 'Person' AND COALESCE((self.props->>'self')::bool, false)
          )
    """)):
        if (domain := domain_of(email)):
            facts.append((person, domain, domain, sid, occurred_at, "calendar organizer"))

    # Contacts are authoritative for a company label, including phone-only
    # cards.  The source item is retained even though contact cards have no date.
    for person, orgs, sid in s.execute(text("""
        SELECT COALESCE(c.canonical_id, person.id), si.raw_meta->'orgs', si.id
        FROM source_items si
        CROSS JOIN LATERAL jsonb_array_elements_text(
            COALESCE(si.raw_meta->'emails', '[]'::jsonb) || COALESCE(si.raw_meta->'phones', '[]'::jsonb)
        ) identity(key)
        JOIN nodes person ON person.type = 'Person' AND person.key = identity.key
        LEFT JOIN canonical c ON c.node_id = person.id
        WHERE si.source IN ('gcontacts', 'maccontacts')
    """)):
        for org in orgs or []:
            if candidate := company_key(org):
                key, display = candidate
                facts.append((person, key, display, sid, None, "contact-card company"))
    return facts


def run(s: Session) -> int:
    """Rebuild inferred organizations, retaining provenance and observed date ranges."""
    s.execute(text("DELETE FROM edges WHERE extractor = :extractor"), {"extractor": EXTRACTOR})
    # Before this stage existed, spine modeled every address domain as MEMBER_OF.
    # Remove only those deterministic legacy rows; manual/user rows are untouched.
    s.execute(text("DELETE FROM edges WHERE type = 'MEMBER_OF' AND extractor = 'spine'"))
    s.execute(text("""
        DELETE FROM nodes n WHERE type = 'Organization' AND key = ANY(:personal)
          AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = n.id OR e.dst = n.id)
    """), {"personal": sorted(PERSONAL_DOMAINS)})
    grouped: dict[tuple[int, str, str], list[tuple[int, object, str]]] = defaultdict(list)
    for person, key, name, sid, at, why in _observations(s):
        grouped[(person, key, name)].append((sid, at, why))

    for (person, key, name), evidence in grouped.items():
        org = _upsert_org(s, key, name)
        dates = [at for _sid, at, _why in evidence if at is not None]
        edge = s.execute(text("""
            INSERT INTO edges (src, dst, type, props, valid_from, valid_to, extractor, extractor_version, confidence,
                               source_item_id)
            VALUES (:person, :org, 'WORKED_AT', CAST(:props AS jsonb), :start, :end, :extractor, :version, :confidence,
                    :sid)
            RETURNING id
        """), {"person": person, "org": org, "start": min(dates) if dates else None,
               "end": max(dates) if dates else None, "extractor": EXTRACTOR, "version": VERSION,
               "confidence": 0.9 if any(why == "contact-card company" for _sid, _at, why in evidence) else 0.7,
               "sid": next((sid for sid, _at, _why in evidence if sid is not None), None),
               "props": json.dumps({"why": sorted({why for _sid, _at, why in evidence}), "inferred": True})}).scalar_one()
        for sid, _at, why in evidence:
            s.execute(text("""
                INSERT INTO edge_evidence (edge_id, source_item_id, kind, detail)
                VALUES (:edge, :sid, :kind, CAST(:detail AS jsonb))
            """), {"edge": edge, "sid": sid, "kind": why, "detail": json.dumps({"inferred_by": EXTRACTOR})})
    s.commit()
    return len(grouped)
