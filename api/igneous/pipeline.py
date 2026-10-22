"""Per-stage watermarks. Each stage processes rows newer than its watermark, so re-runs are no-ops."""
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


def watermark(s: Session, stage: str) -> datetime:
    s.execute(text("INSERT INTO pipeline_state (stage) VALUES (:s) ON CONFLICT DO NOTHING"), {"s": stage})
    return s.execute(text("SELECT watermark FROM pipeline_state WHERE stage = :s"), {"s": stage}).scalar_one()


def advance(s: Session, stage: str, to: datetime) -> None:
    s.execute(text("UPDATE pipeline_state SET watermark = :t, updated_at = now() WHERE stage = :s"),
              {"s": stage, "t": to})


def run_all(s: Session) -> dict:
    from igneous.graph import spine
    from igneous.rag import chunking, embed

    return {
        "spine": spine.run(s),
        "chunk": chunking.run(s),
        "embed": embed.run(s),
    }


def stages(extract: int = 0) -> dict:
    """Every pipeline stage by name, in run order (shared by `igneous pipeline` and the UI's Rebuild button)."""
    from igneous.graph import entities, organizations, resolve, self_identity, spine, threads, topics
    from igneous.graph import extract as ex
    from igneous.rag import chunking, embed

    return {"spine": spine.run, "chunk": chunking.run, "embed": embed.run,
            "extract": lambda s: ex.run(s, limit=extract) if extract else 0,
            "topics": topics.run,
            "entities": lambda s: entities.run(s, log=None),
            # Life threads (trips, classes, jobs, hobbies): after entities, so named projects exist first.
            "threads": lambda s: threads.run(s, log=None),
            # self before resolve: the "you" links must exist when identities are merged
            "self": lambda s: self_identity.run(s)["self_links"],
            "resolve": lambda s: resolve.run(s)["same_as"],
            # Resolve first so aliases (including phone-only contacts) receive one relationship.
            "organizations": organizations.run}
