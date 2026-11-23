"""The filters a question states: explicit phrasing only, correspondents vs. mentions, titles, and the cheap,
cached model fallback. People live in the test database."""
import json
from datetime import datetime

import pytest
from sqlalchemy import text

from igneous import llm, source_catalog
from igneous.providers import ChatResult, ProviderError
from igneous.rag import constraints
from igneous.rag.constraints import PLACES, _rule_constraints, apply, describe
from igneous.rag.search import Filters

MAIL, CALENDAR = PLACES["mail"], PLACES["calendar"]


def _person(s, key, name) -> int:
    return s.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', :k, :n) RETURNING id"),
                     {"k": key, "n": name}).scalar_one()


@pytest.mark.parametrize(("question", "sources"), [
    ("Show my emails from Jeff Skilling in May 2001", MAIL),
    ("What did Sam say in his email to me?", MAIL),
    ("What's in my calendar about the gas prices?", CALENDAR),
    ("calendar invites about the offsite", CALENDAR),
    ("my texts about dinner", ("imessage",)),
    ("Find it in my files or my texts", tuple(sorted({*PLACES["documents"], "imessage"}))),
    # A source word that says what the question is about narrows nothing.
    ("events about gas prices", ()),
    ("Which events led to the California crisis?", ()),
    ("What did the text of the contract say about penalties?", ()),
    ("Summarize the documents Jeff sent about the merger", ()),
    ("Who arranged the SUN demonstration for the YPO breakfast event, according to the voicemail?", ()),
    ("Who should I send it to via interoffice mail?", ()),
])
def test_sources_come_only_from_explicit_phrasing(question, sources):
    assert _rule_constraints(question).sources == sources


def test_people_are_correspondents_or_mentions_and_titles_are_skipped():
    found = _rule_constraints("Did the email from Dr. Sarah Lee arrive?")
    assert found.correspondent == "Sarah Lee" and found.mentioned is None
    found = _rule_constraints("mail to Ms Jane Doe regarding Kenneth Lay")
    assert found.correspondent == "Jane Doe" and found.mentioned == "Kenneth Lay"
    assert _rule_constraints("Anything involving Jeffery McMahon?").mentioned == "Jeffery McMahon"
    assert _rule_constraints("what did he say on May 32, 2001").since is None


def test_a_question_fills_unset_filters_with_canonical_people_and_dates(db):
    head = _person(db, "jeff.skilling@enron.com", "Jeff Skilling")
    alias = _person(db, "jskilling@enron.com", "Jeff Skilling")
    db.execute(text("INSERT INTO canonical (node_id, canonical_id) VALUES (:a, :h), (:h, :h)"), {"a": alias, "h": head})
    f = apply(db, "Show my emails from Jeff Skilling in May 2001")
    assert f.person_id == head and f.sources == MAIL
    assert (f.since, f.until) == (datetime(2001, 5, 1), datetime(2001, 6, 1))
    assert f.from_question == {"sources", "person", "since", "until"}
    shown = describe(db, f)
    assert shown["person"] == {"id": head, "name": "Jeff Skilling"} and shown["mentions"] is None
    assert shown["from_question"] == ["person", "since", "sources", "until"]
    # A three-word capture ("Jeff Skilling In") still finds the two-word name.
    assert apply(db, "emails from Jeff Skilling In May").person_id == head


def test_filters_the_caller_set_win_and_question_sources_only_narrow_them(db):
    explicit = Filters(sources=("fs",), since=datetime(2020, 1, 1))
    assert apply(db, "What was discussed in my calendar in 2001?", explicit) == explicit  # dates win as a range
    # The MCP server's default scope (everything but browsing) narrows to what the question names, even several.
    content = Filters(sources=source_catalog.CONTENT)
    assert apply(db, "my emails about the budget", content).sources == MAIL
    assert apply(db, "my emails about the budget", content).from_question == {"sources"}
    assert apply(db, "my calendar invites", content).sources == tuple(sorted(set(CALENDAR) & set(source_catalog.CONTENT)))


def test_regarding_someone_filters_to_records_that_mention_them(db):
    jane = _person(db, "name:jane-mentioned", "Jane Mentioned")
    f = apply(db, "What did the email regarding Jane Mentioned say about storage?")
    assert f.mentions_id == jane and f.person_id is None and f.from_question == {"mentions"}
    # A name that isn't a person's adds nothing; neither does one the records can't be searched for.
    assert apply(db, "Any mail regarding Enron Online?").mentions_id is None
    _person(db, "x:r2", "R2 D2")
    assert not constraints._servable_mention(db, constraints.resolve_person(db, "R2 D2"))


class Recorder:
    def __init__(self, reply: dict):
        self.reply, self.calls = reply, []

    def __call__(self, messages, schema=None, **options):
        self.calls.append({"messages": messages, "schema": schema, **options})
        return ChatResult(json.dumps(self.reply), {"input_tokens": 1, "output_tokens": 1})


def test_the_fallback_reads_only_the_question_once_with_the_cheap_model(db, monkeypatch):
    sam = _person(db, "sam@example.test", "Sam Lee")
    rec = Recorder({"correspondent": "Sam Lee", "mentioned": None, "source": "calendar",
                    "since": "2001-05-01", "until": None})
    monkeypatch.setattr(llm, "chat", rec)
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    question = "What did the planning discussion cover?"

    monkeypatch.setattr(llm.settings, "llm_sources", "")  # nothing opted in: not even the question is sent
    assert apply(db, question) == Filters()
    assert rec.calls == []

    monkeypatch.setattr(llm.settings, "llm_sources", "gcal")
    f = apply(db, question)
    assert (f.person_id, f.sources, f.since, f.until) == (sam, CALENDAR, datetime(2001, 5, 1), None)
    (call,) = rec.calls
    assert call["model"] == llm.settings.llm_cheap_model
    assert [m["content"] for m in call["messages"] if m["role"] == "user"] == [question]
    assert apply(db, question) == f and len(rec.calls) == 1  # cached under the question's hash
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    assert apply(db, question) == f  # a cached answer keeps counting


def test_the_fallback_ignores_what_it_cannot_validate_and_any_failure(db, monkeypatch):
    monkeypatch.setattr(llm.settings, "llm_sources", "gmail")
    monkeypatch.setattr(llm.settings, "llm_enabled", True)
    monkeypatch.setattr(llm, "chat", Recorder({"correspondent": "<script>", "mentioned": 7, "source": "faxes",
                                               "since": "2001-09-01", "until": "2001-01-01"}))
    assert apply(db, "What was decided at the offsite?") == Filters()

    def fail(*_args, **_kwargs):
        raise ProviderError("model stopped: refusal")

    monkeypatch.setattr(llm, "chat", fail)
    assert apply(db, "What was decided at the retreat?") == Filters()
