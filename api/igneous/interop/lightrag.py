"""Evidence-backed LightRAG custom KG, checked against lightrag-hku 1.5.7.

Export existing facts, without extraction or model calls. Text and node names follow the same opt-in policy
as hosted prompts. LightRAG accepts one chunk alias per entity/relationship and stores one undirected edge
per entity pair. A cited evidence bundle preserves different facts about a pair instead of overwriting them.
"""
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.graph.resolve import P, split_name, unresolvable
from igneous.llm import allowed_sources, visible_nodes
from igneous.rag.chunking import chunks_for

CANON = "COALESCE((SELECT canonical_id FROM canonical WHERE node_id = {x}), {x})"
DERIVED = ("INVOLVED_IN", "PART_OF", "WORKED_AT", "INTRODUCED", "HAS_COMMITMENT", "HAS_DECISION")

# Preserve sender->mention/topic facts, and add the derived graph. Evidence must be an edge's own source
# or an explicit edge_evidence record, never an unrelated record that happens to name either endpoint.
FACTS = text("""
    WITH facts AS (
        SELECT e.id, e.src, e.dst, e.type, e.source_item_id, e.span_start, e.span_end,
               CASE WHEN e.type = 'INTRODUCED' THEN e.props->>'introducer' END AS introducer, false AS legacy
        FROM edges e WHERE e.type = ANY(:derived) AND COALESCE(e.props->>'verdict', '') <> 'drop'
        UNION ALL
        SELECT e.id, sender.src, e.dst, e.type, COALESCE(e.source_item_id, m.source_item_id),
               e.span_start, e.span_end, NULL, true
        FROM edges e JOIN nodes m ON m.id = e.src AND m.type = 'Message'
        JOIN edges sender ON sender.dst = m.id AND sender.type = 'SENT'
        WHERE e.type IN ('MENTIONS', 'ABOUT') AND COALESCE(e.props->>'verdict', '') <> 'drop'
          AND COALESCE(sender.props->>'verdict', '') <> 'drop'
          AND NOT COALESCE((m.props->>'hidden')::bool, false)
    ), evidence AS (
        SELECT f.*, COALESCE(ca.canonical_id, a.id) AS a, COALESCE(cb.canonical_id, b.id) AS b,
               ah.name AS a_name, bh.name AS b_name,
               si.id AS item, si.occurred_at, c.id AS chunk, c.ord, si.source, si.external_id,
               CASE WHEN f.source_item_id = si.id AND f.span_start IS NOT NULL THEN
                   COALESCE(substring(si.body_text FROM f.span_start + 1 FOR GREATEST(f.span_end - f.span_start, 0)), '')
               END AS quote
        FROM facts f JOIN nodes a ON a.id = f.src JOIN nodes b ON b.id = f.dst
        LEFT JOIN canonical ca ON a.type = 'Person' AND ca.node_id = a.id
        LEFT JOIN canonical cb ON b.type = 'Person' AND cb.node_id = b.id
        JOIN nodes ah ON ah.id = COALESCE(ca.canonical_id, a.id)
        JOIN nodes bh ON bh.id = COALESCE(cb.canonical_id, b.id)
        JOIN LATERAL (
            SELECT f.source_item_id AS sid
            UNION SELECT source_item_id FROM edge_evidence WHERE edge_id = f.id
        ) refs ON true
        JOIN source_items si ON si.id = refs.sid AND si.source = ANY(CAST(:sources AS text[]))
        LEFT JOIN LATERAL (
            SELECT c.id, c.ord FROM chunks c WHERE c.source_item_id = si.id AND btrim(c.text) <> ''
              AND (f.span_start IS NULL OR f.source_item_id IS DISTINCT FROM si.id OR (
                  f.span_end > f.span_start AND strpos(c.text, substring(si.body_text FROM f.span_start + 1
                      FOR GREATEST(f.span_end - f.span_start, 0))) > 0))
            ORDER BY (CASE WHEN strpos(lower(c.text), lower(bh.name)) > 0 THEN 2 ELSE 0 END
                    + CASE WHEN strpos(lower(c.text), lower(ah.name)) > 0 THEN 1 ELSE 0 END) DESC,
                     c.ord, c.id LIMIT 1
        ) c ON true
        WHERE NOT COALESCE((a.props->>'hidden')::bool, false)
          AND NOT COALESCE((b.props->>'hidden')::bool, false)
          AND NOT COALESCE((ah.props->>'hidden')::bool, false)
          AND NOT COALESCE((bh.props->>'hidden')::bool, false)
          AND COALESCE(a.props->>'kind', '') <> 'service' AND COALESCE(b.props->>'kind', '') <> 'service'
          AND COALESCE(ah.props->>'kind', '') <> 'service' AND COALESCE(bh.props->>'kind', '') <> 'service'
          AND COALESCE(ca.canonical_id, a.id) <> COALESCE(cb.canonical_id, b.id)
          AND (c.id IS NOT NULL OR btrim(si.body_text) <> '')
    ), items AS (
        SELECT DISTINCT item, occurred_at FROM evidence
        ORDER BY occurred_at DESC NULLS LAST, item DESC LIMIT :lim
    )
    SELECT e.* FROM evidence e JOIN items i ON i.item = e.item
    ORDER BY e.occurred_at DESC NULLS LAST, e.item DESC, e.id, e.a, e.b
""")


def _real_people(s: Session) -> set[int]:
    """Legacy mention filtering: resolved people with reciprocal correspondence, not announcement lists."""
    return set(s.execute(text(f"""
        WITH sent AS (
            SELECT {CANON.format(x='snd.src')} AS p, {CANON.format(x='rcv.dst')} AS q
            FROM edges snd JOIN edges rcv ON rcv.src = snd.dst
            WHERE snd.type = 'SENT' AND rcv.type = 'RECEIVED')
        SELECT DISTINCT a.p FROM sent a JOIN sent b ON b.p = a.q AND b.q = a.p WHERE a.p <> a.q
    """)).scalars())


def _introducer(value: str | None) -> int | None:
    if value and value.isascii() and value.isdigit() and len(value) <= 19:
        n = int(value)
        if 0 < n < 2**63:
            return n
    return None


def _evidence(s: Session, rows: list) -> tuple[list[dict], dict[str, str]]:
    """Export-only chunks for unindexed evidence (notably LinkedIn profile records). Never write to the DB."""
    missing = sorted({r["item"] for r in rows if r["chunk"] is None})
    parts = {r["id"]: chunks_for(SimpleNamespace(**dict(r))) for r in s.execute(text("""
        SELECT id, source, title, body_text, raw_meta, occurred_at FROM source_items WHERE id = ANY(:ids)
    """), {"ids": missing}).mappings()} if missing else {}
    ready, texts = [], {}
    for row in rows:
        r = dict(row)
        if r["chunk"] is not None:
            r["source_id"] = f"igneous-c{r['chunk']}"
        else:
            candidates = [(i, p) for i, p in enumerate(parts.get(r["item"], []))
                          if r["quote"] is None or (r["quote"] and r["quote"] in p)]
            match = max(candidates, default=None, key=lambda part: (
                2 * (r["b_name"].casefold() in part[1].casefold())
                + (r["a_name"].casefold() in part[1].casefold()), -part[0]))
            if match is None:
                continue
            r["ord"], content = match
            r["source_id"] = f"igneous-item-{r['item']}-{r['ord']}"
            texts[r["source_id"]] = content
        ready.append(r)
    return ready, texts


def _description(kind: str, a: str, b: str, introducer: str | None = None) -> str:
    if kind == "INTRODUCED":
        return f"{a} and {b} were introduced" + (f" by {introducer}." if introducer else ".")
    verb = {"MENTIONS": "mentioned", "ABOUT": "wrote about", "INVOLVED_IN": "took part in",
            "PART_OF": "is part of", "WORKED_AT": "worked or studied at",
            "HAS_COMMITMENT": "records the commitment", "HAS_DECISION": "records the decision"}[kind]
    return f"{a} {verb} {b}."


def export(s: Session, limit_messages: int | None = None) -> dict:
    """Export cited facts from the newest N eligible evidence records (legacy argument name retained).

    Facts without opted-in text evidence are omitted. Unindexed evidence is chunked in memory only;
    span-bearing facts require a chunk containing the actual span. Neither graph state nor caches change.
    """
    if limit_messages is not None and limit_messages < 1:
        raise ValueError("the export limit must be at least 1")
    empty = {"chunks": [], "entities": [], "relationships": []}
    sources = allowed_sources()
    if not sources:
        return empty
    rows = list(s.execute(FACTS, {"derived": list(DERIVED), "sources": sorted(sources),
                                  "lim": limit_messages}).mappings())
    if not rows:
        return empty
    rows, extra_texts = _evidence(s, rows)
    # Introducers are third-party names in a relationship description: gate them as well as its two ends.
    introducers = {i for r in rows if (i := _introducer(r["introducer"])) is not None}
    canonical = dict(s.execute(text("SELECT node_id, canonical_id FROM canonical WHERE node_id = ANY(:ids)"),
                               {"ids": sorted(introducers)}).all()) if introducers else {}
    ids = {r[k] for r in rows for k in ("a", "b")} | {canonical.get(i, i) for i in introducers}
    visible = visible_nodes(s, ids, sources)
    nodes = {r["id"]: dict(r) for r in s.execute(text("""
        SELECT id, type, name, key FROM nodes WHERE id = ANY(:ids) AND btrim(name) <> ''
          AND NOT COALESCE((props->>'hidden')::bool, false) AND COALESCE(props->>'kind', '') <> 'service'
    """), {"ids": sorted(visible)}).mappings()}
    real = _real_people(s) if any(r["legacy"] for r in rows) else set()

    def legacy_person(i):
        n = nodes[i]
        if n["type"] != "Person":
            return True
        first, last = split_name(n["name"], n["key"])
        return not unresolvable(P(i, n["key"], n["name"], first, last, set())) and (
            n["key"].startswith("name:") or i in real)

    # One representative evidence record per directed fact. LightRAG cannot distinguish repeated edges.
    facts = {}
    for r in rows:
        if r["a"] not in nodes or r["b"] not in nodes:
            continue
        if r["legacy"] and not all(legacy_person(i) for i in (r["a"], r["b"])):
            continue
        facts.setdefault((r["a"], r["b"], r["type"]), r)
    if not facts:
        return empty
    used = {r[k] for r in facts.values() for k in ("a", "b")}
    for r in facts.values():
        intro = _introducer(r["introducer"])
        if intro in introducers and canonical.get(intro, intro) in nodes:
            used.add(canonical.get(intro, intro))
    names = defaultdict(list)
    for i in sorted(used):
        names[nodes[i]["name"]].append(i)
    # Projects and their organizations often share a name. Preserve their distinct identities.
    labels = {i: name if len(group) == 1 else f"{name} ({nodes[i]['type'].lower()}, igneous:{i})"
              for name, group in names.items() for i in group}
    chunk_ids = sorted({r["chunk"] for r in facts.values() if r["chunk"] is not None})
    texts = {f"igneous-c{i}": content for i, content in s.execute(text(
        "SELECT id, text FROM chunks WHERE id = ANY(:ids)"), {"ids": chunk_ids})}
    texts.update(extra_texts)
    chunks, entities, pairs = {}, {}, defaultdict(dict)
    for (a, b, kind), r in facts.items():
        source_id = r["source_id"]
        path = f"{r['source']}/{r['external_id']}"
        chunks[source_id] = {"content": texts[source_id], "source_id": source_id,
                             "chunk_order_index": r["ord"], "file_path": path}
        intro = _introducer(r["introducer"])
        intro = canonical.get(intro, intro) if intro in introducers else None
        intro = intro if intro in used else None
        for i in (a, b, *([intro] if intro is not None else [])):
            entities.setdefault(i, {"entity_name": labels[i], "entity_type": nodes[i]["type"].lower(),
                                    "description": f"{nodes[i]['type']} in the supplied records.",
                                    "source_id": source_id, "file_path": path})
        desc = _description(kind, labels[a], labels[b], labels.get(intro))
        pairs[tuple(sorted((a, b)))][(a, b, kind)] = (desc, source_id)
    relationships = []
    for (a, b), claims in sorted(pairs.items()):
        refs = sorted({ref for _desc, ref in claims.values()})
        if len(refs) == 1:
            source_id = refs[0]
        else:
            source_id = "igneous-evidence-" + hashlib.sha256("|".join(refs).encode()).hexdigest()[:20]
            chunks[source_id] = {"source_id": source_id, "chunk_order_index": 0, "file_path": "igneous/evidence",
                                 "content": "\n\n".join(f"Source: {chunks[ref]['file_path']}\n{chunks[ref]['content']}"
                                                          for ref in refs)}
        relationships.append({"src_id": labels[a], "tgt_id": labels[b], "weight": float(len(claims)),
                              "source_id": source_id, "file_path": chunks[source_id]["file_path"],
                              "description": " ".join(desc for desc, _ref in claims.values()),
                              "keywords": ", ".join(sorted({k[2].lower() for k in claims}))})
    return {"chunks": list(chunks.values()), "entities": list(entities.values()), "relationships": relationships}


def write(s: Session, out: Path, limit_messages: int | None = None) -> dict:
    kg = export(s, limit_messages)
    out.write_text(json.dumps(kg))
    return {k: len(v) for k, v in kg.items()}
