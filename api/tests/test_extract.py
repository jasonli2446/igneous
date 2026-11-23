"""Which messages extraction reads: every opted-in message long enough to be worth a call, whether or not its source
labels mail, except Gmail's automated categories."""
import pytest
from pipeline_data import day, local_models, mail  # noqa: F401
from sqlalchemy import text

from igneous import llm
from igneous.graph import extract, spine

pytestmark = pytest.mark.usefixtures("local_models")
BODY = "Kai, the quarterly budget review moved to Thursday at noon in the large conference room. " * 4
NOTHING = {"people": [], "organizations": [], "topics": [], "commitments": [], "decisions": []}


def test_a_message_without_labels_is_read_and_automated_gmail_is_not(db, monkeypatch):
    mail(db, "plain", "me@x.test", ["kai@y.test"], "Budget", day(0), sent=True, body=BODY)
    mail(db, "promo", "deals@shop.test", ["me@x.test"], "Sale", day(1), body="A big sale, this week only. " * 12)
    # An Enron email, a text or an AI chat carries no labels at all; Gmail files promotions under a category.
    db.execute(text("UPDATE source_items SET raw_meta = raw_meta - 'labels' WHERE external_id = 'plain'"))
    db.execute(text("""UPDATE source_items SET raw_meta = jsonb_set(raw_meta, '{labels}',
                                                                  '["INBOX", "CATEGORY_PROMOTIONS"]')
                       WHERE external_id = 'promo'"""))
    db.commit()
    spine.run(db)
    read = []

    def model(system, user, schema, **_options):
        read.append(user)
        return NOTHING, {"input_tokens": 0, "output_tokens": 0}

    monkeypatch.setattr(llm, "structured", model)
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    assert extract.run(db, log=lambda *_: None) == 1
    assert len(read) == 1 and "quarterly budget review" in read[0]
