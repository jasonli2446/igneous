"""Optional local cross-encoder reranking for Q&A evidence.

The model is deliberately initialized on first use. If its optional model files cannot be
loaded (for example, while offline), Q&A retains the retrieval/graph ordering instead of
failing a request.
"""
import logging
from functools import lru_cache

from igneous.config import settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _model():
    # Kept local so ordinary search and deployments without reranking support can start.
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=settings.reranker_model)


def rerank(question: str, items: list[dict], keep: int) -> list[dict]:
    """Cross-encode candidate chunks, retaining their input ordering if unavailable."""
    candidates = items[:keep]
    if not candidates or not settings.reranker_enabled:
        return candidates

    try:
        scores = list(_model().rerank(question, [item["text"] for item in candidates]))
        if len(scores) != len(candidates):
            raise ValueError(f"reranker returned {len(scores)} scores for {len(candidates)} candidates")
    except Exception as exc:  # Model download, ONNX, or optional dependency failure must not break Q&A.
        log.warning("Cross-encoder reranker unavailable; using retrieval order: %s", exc)
        return candidates

    for item, score in zip(candidates, scores, strict=True):
        item["score"] = round(float(score), 4)
    return sorted(candidates, key=lambda item: item["score"], reverse=True)
