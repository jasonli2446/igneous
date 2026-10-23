"""Structured chat calls, and the one rule for what a hosted model may read (#179).

Every hosted call follows the same policy: a prompt carries text only from records in opted-in sources
(`allowed_sources`), names only of people and things that appear in such records (`visible_nodes`), and counts
for everything else. A cached model result may be reused whatever the policy says now: it discloses nothing new.
"""
import json
import os

from sqlalchemy import text
from sqlalchemy.orm import Session

from igneous.config import settings
from igneous.providers import LLMDisabled, chat, get_provider

__all__ = ["LLMDisabled", "allowed_sources", "client", "model_status", "require_whole_corpus", "structured",
           "visible_nodes"]

LOCAL_SOURCES = ("fs", "notes")  # also need IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED


def client():
    """Return the existing Bedrock client for legacy integrations.

    New application code should call ``igneous.providers.chat`` instead.
    """
    provider = get_provider()
    return provider.chat_provider._client()  # type: ignore[attr-defined]


def structured(system: str, user: str, schema: dict, max_tokens: int = 4000, effort: str = "low",
               model: str | None = None) -> tuple[dict, dict]:
    """One schema-constrained chat call returning parsed JSON and usage."""
    result = chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], schema, max_tokens=max_tokens, effort=effort, model=model)
    return json.loads(result.text), result.usage


def allowed_sources(value: str | None = None) -> frozenset[str]:
    """Stored source values whose records may reach a hosted model: the ids in IGNEOUS_LLM_SOURCES.

    `takeout` (the Sources page id) stands for every Takeout product. Local files and Apple Notes also need
    IGNEOUS_LOCAL_SOURCES_MODEL_ENABLED=true. Empty, the default, allows nothing."""
    from igneous.connectors.takeout import TAKEOUT_SOURCES

    out: set[str] = set()
    for source in (value if value is not None else settings.llm_sources).split(","):
        source = source.strip()
        if source == "takeout":
            out.update(TAKEOUT_SOURCES)
        elif source:
            out.add(source)
    if not settings.local_sources_model_enabled:
        out.difference_update(LOCAL_SOURCES)
    return frozenset(out)


def visible_nodes(s: Session, ids, sources: frozenset[str] | None = None) -> set[int]:
    """The nodes among `ids` a prompt may name: a record from an allowed source is the node itself, the node at
    the other end of one of its edges, or that edge's own evidence."""
    ids = sorted({int(i) for i in ids if i is not None})
    allowed = sorted(allowed_sources() if sources is None else sources)
    if not ids or not allowed:
        return set()
    return set(s.execute(text("""
        SELECT n.id FROM nodes n WHERE n.id = ANY(CAST(:ids AS bigint[])) AND (
            EXISTS (SELECT 1 FROM source_items si
                    WHERE si.id = n.source_item_id AND si.source = ANY(CAST(:src AS text[])))
            OR EXISTS (SELECT 1 FROM edges e JOIN nodes m ON m.id = e.dst
                       JOIN source_items si ON si.id IN (e.source_item_id, m.source_item_id)
                       WHERE e.src = n.id AND si.source = ANY(CAST(:src AS text[])))
            OR EXISTS (SELECT 1 FROM edges e JOIN nodes m ON m.id = e.src
                       JOIN source_items si ON si.id IN (e.source_item_id, m.source_item_id)
                       WHERE e.dst = n.id AND si.source = ANY(CAST(:src AS text[])))
            OR EXISTS (SELECT 1 FROM edges e JOIN edge_evidence ev ON ev.edge_id = e.id
                       JOIN source_items si ON si.id = ev.source_item_id
                       WHERE (e.src = n.id OR e.dst = n.id) AND si.source = ANY(CAST(:src AS text[]))))
    """), {"ids": ids, "src": allowed}).scalars())


def require_whole_corpus(s: Session) -> None:
    """For eval and demo tools that send records from the whole database: stop unless every source in it is
    opted in (IGNEOUS_LLM_SOURCES=email for the Enron demo database)."""
    present = set(s.execute(text("SELECT DISTINCT source FROM source_items")).scalars())
    missing = sorted(present - allowed_sources())
    if missing:
        raise SystemExit(f"This sends records from the whole database to the model. Opt in every source it holds "
                         f"(IGNEOUS_LLM_SOURCES); not opted in: {', '.join(missing)}")


def model_status() -> dict:
    """What the Sources page says about hosted model use."""
    return {"enabled": settings.llm_enabled, "configured": bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK")),
            "sources": sorted(allowed_sources()), "local_sources": settings.local_sources_model_enabled}
