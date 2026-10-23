"""Export the graph as a LightRAG custom KG (`LightRAG.insert_custom_kg`), checked against lightrag-hku 1.5.7.

LightRAG then queries OUR entities and relationships (resolved people, extracted mentions, correspondence ties)
instead of re-extracting its own from raw text. Every entity and relationship points at the chunk it came
from via `source_id`, which is how LightRAG keeps facts tied to evidence. LightRAG sends these chunks to its
model, so only records from opted-in sources are exported (#179, igneous/llm.py).

Schema (from LightRAG.ainsert_custom_kg):
  chunks:        content, source_id, chunk_order_index, file_path
  entities:      entity_name, entity_type, description, source_id, file_path
  relationships: src_id, tgt_id, description, keywords, weight, source_id, file_path
"""
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.resolve import P, split_name, unresolvable
from igneous.llm import allowed_sources

CANON = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"


def export(s: Session, limit_messages: int | None = None) -> dict:
    """Messages are scoped to those with extraction or correspondence among resolved people, newest first."""
    lim = "LIMIT :lim" if limit_messages else ""
    msgs = s.execute(text(f"""
        SELECT m.id AS msg, c.id AS chunk, c.text, si.source, si.external_id
        FROM nodes m JOIN chunks c ON c.node_id = m.id AND c.ord = 0
        JOIN source_items si ON si.id = c.source_item_id
        WHERE m.type = 'Message' AND EXISTS (SELECT 1 FROM edges e WHERE e.src = m.id AND e.type = 'MENTIONS')
          AND si.source = ANY(CAST(:sources AS text[]))
        ORDER BY si.occurred_at DESC NULLS LAST {lim}
    """), {"lim": limit_messages, "sources": sorted(allowed_sources())}).all()
    msg_ids = [m.msg for m in msgs]
    chunk_of = {m.msg: f"igneous-c{m.chunk}" for m in msgs}

    chunks = [{"content": m.text, "source_id": chunk_of[m.msg], "chunk_order_index": 0,
               "file_path": f"{m.source}/{m.external_id}"} for m in msgs]

    # People who sent or were mentioned in these messages, folded to their canonical identity.
    rows = s.execute(text(f"""
        WITH links AS (
            SELECT e.dst AS msg, {CANON.format(x='e.src')} AS person, 'SENT' AS rel, NULL::text AS role
            FROM edges e WHERE e.type = 'SENT' AND e.dst = ANY(:m)
            UNION ALL
            SELECT e.src, {CANON.format(x='e.dst')}, 'MENTIONS', e.props->>'role'
            FROM edges e WHERE e.type = 'MENTIONS' AND e.src = ANY(:m)
            UNION ALL
            SELECT e.src, e.dst, 'ABOUT', NULL FROM edges e WHERE e.type = 'ABOUT' AND e.src = ANY(:m))
        SELECT l.msg, l.rel, l.role, n.id AS node_id, n.name, n.type, n.key
        FROM links l JOIN nodes n ON n.id = l.person
    """), {"m": msg_ids}).all()

    # A Person is a real person if their entity-resolved identity has two-way mail with someone: bots and
    # announcement lists (ipayit@, "UBSW Energy Global Infrastructure") only send. Name-only mentions from
    # extraction can't have mail, so they pass the role-name filter alone.
    real = set(s.execute(text(f"""
        WITH sent AS (
            SELECT {CANON.format(x='snd.src')} AS p, {CANON.format(x='rcv.dst')} AS q
            FROM edges snd JOIN edges rcv ON rcv.src = snd.dst
            WHERE snd.type = 'SENT' AND rcv.type = 'RECEIVED')
        SELECT DISTINCT a.p FROM sent a JOIN sent b ON b.p = a.q AND b.q = a.p WHERE a.p <> a.q
    """)).scalars())

    def is_person(node_id: int, name: str, key: str) -> bool:
        f, last = split_name(name, key)
        if unresolvable(P(0, key, name, f, last, set())):
            return False
        return key.startswith("name:") or node_id in real

    rows = [r for r in rows if r.type != "Person" or is_person(r.node_id, r.name, r.key)]
    entities: dict[str, dict] = {}
    by_msg: dict[int, list] = {}
    for r in rows:
        etype = {"Person": "person", "Organization": "organization", "Topic": "topic"}.get(r.type, r.type.lower())
        ent = entities.setdefault(r.name, {"entity_name": r.name, "entity_type": etype, "description": "",
                                           "source_id": chunk_of[r.msg], "file_path": "igneous", "_roles": set()})
        if r.role:
            ent["_roles"].add(r.role)
        by_msg.setdefault(r.msg, []).append(r)
    for e in entities.values():
        roles = sorted(e.pop("_roles"))[:5]
        e["description"] = (f"{e['entity_type'].title()} in the email archive"
                            + (f"; seen {'; '.join(roles)}" if roles else ""))

    # Relationships: sender -> mentioned entity, and sender -> topic, per message.
    rels: dict[tuple[str, str], dict] = {}
    for msg, links in by_msg.items():
        senders = [r.name for r in links if r.rel == "SENT"]
        for snd in senders:
            for r in links:
                if r.rel == "SENT" or r.name == snd:
                    continue
                key = (snd, r.name)
                rel = rels.setdefault(key, {"src_id": snd, "tgt_id": r.name, "weight": 0.0, "source_id": chunk_of[msg],
                                            "keywords": "about" if r.rel == "ABOUT" else "mentions",
                                            "description": "", "file_path": "igneous", "_roles": set()})
                rel["weight"] += 1.0
                if r.role:
                    rel["_roles"].add(r.role)
    for r in rels.values():
        roles = sorted(r.pop("_roles"))[:3]
        verb = "wrote about" if r["keywords"] == "about" else "mentioned"
        r["description"] = f"{r['src_id']} {verb} {r['tgt_id']} in {int(r['weight'])} email(s)" + (
            f" ({'; '.join(roles)})" if roles else "")

    return {"chunks": chunks, "entities": list(entities.values()), "relationships": list(rels.values())}


def write(s: Session, out: Path, limit_messages: int | None = None) -> dict:
    kg = export(s, limit_messages)
    out.write_text(json.dumps(kg))
    return {k: len(v) for k, v in kg.items()}
