"""Embedding: every chunk gets a vector from the configured local model, whatever its source.

The model runs on this computer (FastEmbed) and sends nothing anywhere, so local files and Apple Notes are embedded
like everything else; IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED is about hosted models only. chunks.embedding is
vector(DIM) (migration 0001), and chunks.embedding_model names the model that produced each vector: a chunk with no
vector, or with one from another model, is embedded again, and dense search compares only vectors from the
configured model.
"""
from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.providers import embed

DIM = 384  # chunks.embedding is vector(384): a model with another dimension needs a migration
BATCH = settings.embedding_batch_size


def embed_texts(texts: list[str]) -> list[list[float]]:
    return embed(texts)


def embed_query(q: str) -> list[float]:
    return embed([q], query=True)[0]


def run(s: Session, log=print) -> int:
    """Embed every chunk without a vector from IGNEOUS_EMBEDDING_MODEL, in id order; returns how many."""
    model, total, after = settings.embedding_model, 0, 0
    while True:
        rows = s.execute(text("""
            SELECT id, text FROM chunks
            WHERE id > :after AND (embedding IS NULL OR embedding_model IS DISTINCT FROM :model)
            ORDER BY id LIMIT :n
        """), {"after": after, "model": model, "n": BATCH}).all()
        if not rows:
            break
        vecs = embed_texts([r.text for r in rows])
        if vecs and len(vecs[0]) != DIM:
            raise RuntimeError(f"{model} makes {len(vecs[0])}-dimensional vectors and chunks.embedding holds {DIM}: "
                               f"set IGNEOUS_EMBEDDING_MODEL to a {DIM}-dimensional model, or change the column in a "
                               "migration and embed again")
        # One statement per batch: an UPDATE per row would make the database, not the model, the bottleneck (~10x).
        s.execute(text("""
            UPDATE chunks c SET embedding = CAST(u.v AS vector), embedding_model = :model
            FROM unnest(CAST(:ids AS bigint[]), CAST(:vs AS text[])) AS u(id, v) WHERE c.id = u.id
        """), {"ids": [r.id for r in rows], "vs": [str(v) for v in vecs], "model": model})
        s.commit()
        total, after = total + len(rows), rows[-1].id
        if total % (BATCH * 10) == 0:
            log(f"  embedded {total}")
    return total
