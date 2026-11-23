"""Embedding backfill through the configured model provider."""
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous import source_catalog
from igneous.config import settings
from igneous.providers import embed

BATCH = settings.embedding_batch_size


def embed_texts(texts: list[str]) -> list[list[float]]:
    return embed(texts)


def embed_query(q: str) -> list[float]:
    return embed([q], query=True)[0]


PENDING = """
    FROM chunks c JOIN source_items si ON si.id = c.source_item_id
    WHERE c.embedding IS NULL AND (:local_models OR si.source <> ALL(CAST(:local AS text[])))
"""


def run(s: Session, progress: Callable[[int, int], None] | None = None) -> int:
    """Embed every chunk that has no vector yet, a batch per transaction; `progress(done, total)` after each batch.
    Returns how many chunks it embedded."""
    params = {"local_models": settings.local_sources_model_enabled, "local": list(source_catalog.LOCAL_TEXT)}
    total = s.execute(text(f"SELECT count(*) {PENDING}"), params).scalar_one()
    done = 0
    while True:
        rows = s.execute(text(f"SELECT c.id, c.text {PENDING} ORDER BY c.id LIMIT :n"), {**params, "n": BATCH}).all()
        if not rows:
            break
        vecs = embed_texts([r.text for r in rows])
        # One statement per batch; per-row UPDATEs made the DB, not the model, the bottleneck (~10x).
        s.execute(text("""
            UPDATE chunks c SET embedding = CAST(u.v AS vector)
            FROM unnest(CAST(:ids AS bigint[]), CAST(:vs AS text[])) AS u(id, v) WHERE c.id = u.id
        """), {"ids": [r.id for r in rows], "vs": [str(v) for v in vecs]})
        s.commit()
        done += len(rows)
        if progress:
            progress(done, max(total, done))
    return done
