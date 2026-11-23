"""Local cross-encoder reranking for Q&A evidence (FastEmbed, no hosted call).

Optional by design: when it is turned off (IGNEOUS_RERANKER_ENABLED=false) or its model can't run, `rerank` returns
None and Q&A chooses the evidence without it (`qa.choose`). A model that failed once is not retried in the same
process, so a missing model file costs one attempt, not one per question.
"""
import logging

from igneous.config import settings
from igneous.providers import rerank as score_documents

log = logging.getLogger(__name__)
_load_failure: Exception | None = None


def rerank(question: str, items: list[dict], keep: int) -> list[dict] | None:
    """The `keep` items the cross-encoder scores highest for `question`, best first, each with its `rerank_score`;
    None when reranking is off or its model can't run."""
    global _load_failure

    if not settings.reranker_enabled:
        return None
    if _load_failure:
        log.warning("Cross-encoder reranker unavailable; Q&A chooses evidence without it: %s", _load_failure)
        return None
    if not items:
        return []
    try:
        scores = score_documents(question, [item["text"] for item in items])
    except Exception as exc:  # A model download, ONNX or dependency failure must not break Q&A.
        _load_failure = exc
        log.warning("Cross-encoder reranker unavailable; Q&A chooses evidence without it: %s", exc)
        return None
    if len(scores) != len(items):
        raise RuntimeError(f"reranker returned {len(scores)} scores for {len(items)} candidates")
    # Cross-encoder logits are comparable only within one pool; each item keeps its retrieval score as well.
    ranked = [{**item, "rerank_score": round(float(score), 4)} for item, score in zip(items, scores, strict=True)]
    return sorted(ranked, key=lambda item: item["rerank_score"], reverse=True)[:max(keep, 0)]
