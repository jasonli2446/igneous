"""Source records for pipeline tests, written the way connectors write them, and local models that need no download."""
import hashlib
import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from igneous import providers
from igneous.config import settings

T0 = datetime(2025, 3, 3, 12, tzinfo=UTC)


def day(n: float) -> datetime:
    return T0 + timedelta(days=n)


def put(s, source: str, external_id: str, title: str, meta: dict, when: datetime | None = None,
        body: str = "body") -> None:
    """Store one source item exactly as connectors/base.run_sync does."""
    from igneous.connectors.base import SourceItem, store

    store(s, SourceItem(source=source, external_id=external_id, title=title, body_text=body, occurred_at=when,
                        raw_meta=meta))


def mail(s, ref: str, sender: str, to: list[str], subject: str, when: datetime, body: str = "Hello there.",
         sent: bool = False, names: dict[str, str] | None = None, thread: str | None = None) -> None:
    """A Gmail message; `sent` marks it as one the owner sent (label SENT)."""
    names = names or {}
    put(s, "gmail", ref, subject, {
        "from": [{"email": sender, "name": names.get(sender, "")}],
        "to": [{"email": t, "name": names.get(t, "")} for t in to],
        "fingerprint": f"fp-{ref}", "gmail_thread_id": thread or f"t-{ref}",
        "labels": ["SENT"] if sent else ["INBOX"]}, when, body)


def card(s, ref: str, name: str, emails=(), phones=(), orgs=(), source: str = "gcontacts") -> None:
    put(s, source, ref, name, {"emails": list(emails), "phones": list(phones), "orgs": list(orgs), "account": "acct"})


def event(s, ref: str, title: str, when: datetime, attendees: list[str], organizer: str, series: str,
          names: dict[str, str] | None = None) -> None:
    names = names or {}
    put(s, "gcal", ref, title, {
        "attendees": [{"email": a, "name": names.get(a, ""), "response": "accepted"} for a in attendees],
        "organizer": {"email": organizer, "name": names.get(organizer, "")}, "recurring": True,
        "series_key": series, "event_key": f"gcal:{series}"}, when, f"{title} with friends")


def linkedin_profile(s, name: str, positions: list[dict]) -> None:
    """The profile item of a LinkedIn export (connectors/linkedin.py), which the profile stage reads without a model."""
    put(s, "linkedin", "profile", f"LinkedIn profile: {name}", {
        "kind": "profile", "person_name": name,
        "profile": {"is_about_owner": True, "person_name": name, "education": [], "projects": [],
                    "experience": [{"org": p["org"], "title": p["title"], "start": p.get("start", ""),
                                    "end": p.get("end", ""), "domain_hint": ""} for p in positions]}},
        body=f"LinkedIn profile of {name}")


def vector(value: str) -> list[float]:
    rnd = random.Random(hashlib.sha256(value.encode()).digest())
    v = [rnd.gauss(0, 1) for _ in range(settings.embedding_dim)]
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v]


class LocalModels:
    """Embeddings from a hash (no model download); hosted chat is off unless a test stubs llm.structured."""

    def embed(self, texts, *, query=False):
        return [vector(t) for t in texts]

    def chat(self, messages, schema=None, **options):
        raise providers.LLMDisabled("no hosted model in tests")

    def rerank(self, query, docs):
        return [0.0 for _ in docs]


@pytest.fixture
def local_models(monkeypatch):
    monkeypatch.setattr(providers, "get_provider", LocalModels)


def rows(s, sql: str, **params) -> list[tuple]:
    return [tuple(r) for r in s.execute(text(sql), params).all()]
