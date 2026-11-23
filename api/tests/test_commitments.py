"""A derived commitment resolves only when a later message in its thread confirms it, from one cached check."""
import pytest
from pipeline_data import day, local_models, mail  # noqa: F401
from sqlalchemy import text

from igneous import llm
from igneous.graph import history, spine

pytestmark = pytest.mark.usefixtures("local_models")
PROMISE = "I will send the budget report by Friday."


def promised(s) -> int:
    """Mo promises Kai a report in one email; Kai's reply in the same thread says it arrived."""
    body = f"Hi Kai. {PROMISE} Thanks, Mo"
    mail(s, "a", "me@x.test", ["kai@y.test"], "Budget", day(0), sent=True, body=body, thread="budget")
    mail(s, "b", "kai@y.test", ["me@x.test"], "Re: Budget", day(2), body="Got the budget report, all done.",
         thread="budget")
    s.commit()
    spine.run(s)
    item = s.execute(text("""SELECT si.id, n.id AS node_id, si.occurred_at FROM source_items si
                             JOIN nodes n ON n.source_item_id = si.id AND n.type = 'Message'
                             WHERE si.external_id = 'a'""")).one()
    history.store_claims(s, item, body, {"commitments": [{"who": "sender", "what": "send the budget report",
                                                          "by_when": "Friday", "quote": PROMISE}],
                                         "decisions": []}, "test-model")
    return s.execute(text("SELECT id FROM nodes WHERE type = 'Commitment'")).scalar_one()


def status(s, commitment: int) -> str:
    return s.execute(text("SELECT props->>'status' FROM nodes WHERE id = :i"), {"i": commitment}).scalar()


def test_a_later_message_resolves_a_commitment_from_one_cached_check(db, monkeypatch):
    commitment = promised(db)
    asked = []

    def model(system, user, schema, **_options):
        asked.append(user)
        return {"resolved": True}, {}

    monkeypatch.setattr(llm, "structured", model)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    assert history.refresh_commitment_status(db) == 1
    assert status(db, commitment) == "resolved"
    assert len(asked) == 1 and "send the budget report" in asked[0] and "Got the budget report" in asked[0]
    # The answer is stored: it counts again with the model off, without another call.
    db.execute(text("""UPDATE nodes SET props = props || '{"status": "open"}' WHERE id = :i"""), {"i": commitment})
    monkeypatch.setattr(llm.settings, "llm_enabled", False)
    assert history.refresh_commitment_status(db) == 1 and len(asked) == 1


def test_a_failed_check_leaves_the_commitment_open_for_the_next_run(db, monkeypatch):
    commitment = promised(db)

    def throttled(*_args, **_options):
        raise RuntimeError("ThrottlingException")

    monkeypatch.setattr(llm, "structured", throttled)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    assert history.refresh_commitment_status(db) == 0
    assert status(db, commitment) == "open"
    assert db.execute(text("SELECT count(*) FROM llm_cache")).scalar() == 0


def test_nothing_opted_in_means_no_check(db, monkeypatch):
    promised(db)

    def forbidden(*_args, **_options):
        raise AssertionError("no model call without an opted-in source")

    monkeypatch.setattr(llm, "structured", forbidden)
    assert history.refresh_commitment_status(db) == 0
