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
