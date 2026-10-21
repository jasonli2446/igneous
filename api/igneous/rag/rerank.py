"""Optional local cross-encoder reranking for Q&A evidence."""
import logging
from functools import lru_cache

from igneous.config import settings

log = logging.getLogger(__name__)
_load_failure: Exception | None = None


@lru_cache(maxsize=1)
def _model():
    # Kept local so ordinary search and deployments without reranking support can start.
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=settings.reranker_model)


def rerank(question: str, items: list[dict], keep: int) -> list[dict]:
    """Score a bounded candidate pool, or preserve retrieval order when unavailable."""
    global _load_failure

    keep = max(keep, 0)
    pool = items[:max(settings.reranker_pool_size, 0)]
    if not pool or not settings.reranker_enabled:
        return pool[:keep]
    if _load_failure:
        log.warning("Cross-encoder reranker unavailable; using retrieval order: %s", _load_failure)
        return pool[:keep]

    try:
        # TextCrossEncoder.rerank is a generator, so materialize it while handling model/inference failures.
        scores = list(_model().rerank(question, [item["text"] for item in pool]))
    except Exception as exc:  # Model download, ONNX, or optional dependency failure must not break Q&A.
        _load_failure = exc
        log.warning("Cross-encoder reranker unavailable; using retrieval order: %s", exc)
        return pool[:keep]

    if len(scores) != len(pool):
        raise RuntimeError(f"reranker returned {len(scores)} scores for {len(pool)} candidates")

    ranked = []
    for item, score in zip(pool, scores, strict=True):
        # Preserve retrieval score semantics; cross-encoder logits are only comparable within this pool.
        ranked.append({**item, "rerank_score": round(float(score), 4)})
    return sorted(ranked, key=lambda item: item["rerank_score"], reverse=True)[:keep]
