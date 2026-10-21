"""Conservative normalization and relationship discovery for open-vocabulary Topic nodes.

Topic merges are deliberately destructive only at the node level: every ABOUT edge is re-pointed to the
surviving node, retaining its extractor, source item, span, model, and confidence provenance unchanged.
"""
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.rag.embed import embed_texts

EXTRACTOR = "topic-normalization"
VERSION = "1"
SIMILARITY_THRESHOLD = 0.96
MIN_COOCCURRENCES = 3


@dataclass(frozen=True)
class Topic:
    id: int
    key: str
    name: str


def _singular(word: str) -> str:
    """Singularize only unambiguous English regular plurals; never guess at irregular words."""
    lower = word.lower()
    if len(lower) <= 3 or lower.endswith(("ss", "us", "is", "news")):
        return lower
    if lower.endswith("yses"):
        return lower[:-2] + "is"  # analyses -> analysis; diagnoses -> diagnosis
    if lower.endswith("ies") and len(lower) > 4:
        return lower[:-3] + "y"
    if lower.endswith(("ches", "shes", "xes", "zes", "ses")):
        return lower[:-2]
    if lower.endswith("s"):
        return lower[:-1]
    return lower


def normalize_key(name: str) -> str:
    """Create a stable lowercase topic key, singularizing only the phrase's final word when safe."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    if not words:
        return ""
    words[-1] = _singular(words[-1])
    return "-".join(words)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    a_norm = sum(x * x for x in a) ** 0.5
    b_norm = sum(x * x for x in b) ** 0.5
    return dot / (a_norm * b_norm) if a_norm and b_norm else 0.0


def _tokens(key: str) -> set[str]:
    return set(key.split("-"))


def similar_pairs(topics: list[Topic], vectors: Iterable[list[float]], threshold: float = SIMILARITY_THRESHOLD):
    """Yield high-confidence semantic duplicates with lexical overlap as a precision guardrail."""
    vectors = list(vectors)
    for i, topic in enumerate(topics):
        for other, vector in zip(topics[i + 1:], vectors[i + 1:], strict=True):
            shared = _tokens(normalize_key(topic.name)) & _tokens(normalize_key(other.name))
            if not shared:
                continue
            similarity = cosine(vectors[i], vector)
            if similarity >= threshold:
                yield topic, other, similarity


def _merge(s: Session, survivor: int, duplicate: int, reason: dict) -> bool:
    """Move evidence only when it cannot collide with an existing provenance-bearing edge."""
    collision = s.execute(text("""
        SELECT 1
        FROM edges old JOIN edges current
          ON current.src = old.src AND current.type = old.type AND current.extractor = old.extractor
         AND current.dst = :survivor
        WHERE old.dst = :duplicate
        LIMIT 1
    """), {"survivor": survivor, "duplicate": duplicate}).first()
    if collision:
        return False
    s.execute(text("UPDATE edges SET dst = :survivor WHERE dst = :duplicate"),
              {"survivor": survivor, "duplicate": duplicate})
    s.execute(text("UPDATE nodes SET props = props || CAST(:props AS jsonb) WHERE id = :id"),
              {"id": survivor, "props": json.dumps({"topic_normalized": True, "merge_reason": reason})})
    s.execute(text("DELETE FROM nodes WHERE id = :id"), {"id": duplicate})
    return True


def _related(s: Session, minimum: int) -> int:
    s.execute(text("DELETE FROM edges WHERE type = 'RELATED_TO' AND extractor = :ex"), {"ex": EXTRACTOR})
    result = s.execute(text("""
        WITH pairs AS (
            SELECT a.dst AS src, b.dst AS dst, count(DISTINCT a.src) AS cooccurrences
            FROM edges a JOIN edges b ON b.src = a.src AND a.dst < b.dst
            WHERE a.type = 'ABOUT' AND b.type = 'ABOUT'
            GROUP BY a.dst, b.dst
            HAVING count(DISTINCT a.src) >= :minimum
        )
        INSERT INTO edges (src, dst, type, props, extractor, extractor_version, confidence)
        SELECT src, dst, 'RELATED_TO', jsonb_build_object('cooccurrences', cooccurrences), :ex, :version, 1.0
        FROM pairs
        ON CONFLICT (src, dst, type, extractor) DO UPDATE
          SET props = EXCLUDED.props, confidence = EXCLUDED.confidence
    """), {"minimum": minimum, "ex": EXTRACTOR, "version": VERSION})
    return result.rowcount


def run(s: Session, threshold: float = SIMILARITY_THRESHOLD, minimum: int = MIN_COOCCURRENCES) -> dict:
    """Normalize Topic keys, merge obvious duplicates, and regenerate co-occurrence RELATED_TO edges."""
    topics = [Topic(*row) for row in s.execute(text(
        "SELECT id, key, name FROM nodes WHERE type = 'Topic' ORDER BY id")).all()]
    merged = skipped = 0

    # Exact normalized keys need no embedding call and are safe even on an offline first run.
    groups: dict[str, list[Topic]] = {}
    for topic in topics:
        key = normalize_key(topic.name)
        if key:
            groups.setdefault(key, []).append(topic)
    for key, group in groups.items():
        survivor = group[0]
        for duplicate in group[1:]:
            if _merge(s, survivor.id, duplicate.id, {"normalized_key": key}):
                merged += 1
            else:
                skipped += 1
        s.execute(text("UPDATE nodes SET key = :key WHERE id = :id"), {"key": key, "id": survivor.id})

    remaining = [Topic(*row) for row in s.execute(text(
        "SELECT id, key, name FROM nodes WHERE type = 'Topic' ORDER BY id")).all()]
    if len(remaining) > 1:
        vectors = embed_texts([topic.name for topic in remaining])
        removed: set[int] = set()
        for first, second, similarity in similar_pairs(remaining, vectors, threshold):
            # Candidate pairs were built from the pre-merge set; do not use a node deleted by an earlier merge.
            if first.id in removed or second.id in removed:
                continue
            survivor, duplicate = (first, second) if first.id < second.id else (second, first)
            if _merge(s, survivor.id, duplicate.id, {"similarity": round(similarity, 4)}):
                removed.add(duplicate.id)
                merged += 1
            else:
                skipped += 1
    related = _related(s, minimum)
    s.commit()
    return {"topics_merged": merged, "merge_collisions_skipped": skipped, "related_to": related}
