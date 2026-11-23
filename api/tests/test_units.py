from datetime import datetime

from igneous.connectors.email_maildir import parse_message
from igneous.graph.spine import _name_from, _thread_key
from igneous.rag.chunking import split, strip_quoted
from igneous.rag.search import rrf

RAW = b"""Message-ID: <123.JavaMail.evans@thyme>
Date: Mon, 14 May 2001 16:39:00 -0700 (PDT)
From: phillip.allen@enron.com
To: tim.belden@enron.com, John.Arnold@enron.com
Subject: Re: Forecast
In-Reply-To: <100.JavaMail.evans@thyme>
X-From: Phillip K Allen
X-Folder: \\Phillip_Allen_Jan2002_1\\Allen, Phillip K.\\'Sent Mail

Here is our forecast.

 -----Original Message-----
From: Belden, Tim
Sent: Monday
Old quoted text.
"""


def test_synthetic_cross_source_corpus_is_deterministic_and_valid(tmp_path):
    from igneous.synthetic_corpus import NOTICE, generate, verify

    first = tmp_path / "first"
    second = tmp_path / "second"
    assert generate(first) == generate(second)
    assert verify(first) == {"documents": 5, "calendar_events": 2}
    assert NOTICE in (first / "README.md").read_text()
    assert NOTICE in (first / "calendar.ics").read_text()
    assert "phillip.allen@enron.com" in (first / "files/2001-05-14-forecast-memo.md").read_text()


def test_parse_message():
    item = parse_message(RAW, "allen-p/sent/1.")
    m = item.raw_meta
    assert m["from"] == [{"name": "", "email": "phillip.allen@enron.com"}]
    assert [a["email"] for a in m["to"]] == ["tim.belden@enron.com", "john.arnold@enron.com"]
    assert m["in_reply_to"] == "<100.JavaMail.evans@thyme>"
    assert item.title == "Re: Forecast"
    assert item.occurred_at.year == 2001


def test_content_hash_stable():
    assert parse_message(RAW, "a").content_hash == parse_message(RAW, "a").content_hash


def test_strip_quoted():
    assert strip_quoted(parse_message(RAW, "a").body_text) == "Here is our forecast."


def test_split_long_text_overlaps_and_covers():
    t = ("word " * 1000).strip()
    parts = split(t)
    assert len(parts) > 1 and all(len(p) <= 2000 for p in parts)
    assert split("short") == ["short"] and split("") == []


def test_names_and_threads():
    assert _name_from({"name": "", "email": "phillip.allen@enron.com"}, "Phillip K Allen") == "Phillip K Allen"
    assert _name_from({"name": "", "email": "tim.belden@enron.com"}, None) == "Tim Belden"
    assert _thread_key({"references": ["<a>", "<b>"]}, "x") == "<a>"
    assert _thread_key({"from": [{"email": "a@x"}], "to": [{"email": "b@x"}]}, "RE: FW: Forecast") == "subject:forecast|a@x,b@x"


def test_rrf_rewards_agreement():
    assert rrf([1, 2, 3], [3, 2, 9])[0][0] in (2, 3)
    assert {c for c, _ in rrf([1], [2])} == {1, 2}


def test_experts_uses_retrieved_items_and_returns_bounded_evidence(monkeypatch):
    from igneous.graph import traversal
    from igneous.rag import search

    monkeypatch.setattr(traversal, "names", lambda s: {})
    hits = [
        {"source_item_id": 10, "score": 0.03, "text": "a" * 500},
        {"source_item_id": 10, "score": 0.02, "text": "older duplicate"},
        {"source_item_id": 20, "score": 0.01, "text": "event discussion"},
    ]
    monkeypatch.setattr(search, "hybrid", lambda *args, **kwargs: hits)

    class Result:
        def mappings(self):
            return self

        def all(self):
            return [
                {"person_id": 7, "name": "Pat Smith", "score": 0.04, "evidence_count": 2,
                 "source_item_id": 10, "involvement": ["authored", "mentioned"], "source": "gmail",
                 "external_id": "m-10", "title": "Plan", "occurred_at": None},
                {"person_id": 7, "name": "Pat Smith", "score": 0.04, "evidence_count": 2,
                 "source_item_id": 20, "involvement": ["attended"], "source": "gcal",
                 "external_id": "e-20", "title": "Review", "occurred_at": None},
            ]

    class Session:
        def __init__(self):
            self.params = None

        def execute(self, _statement, params):
            self.params = params
            return Result()

    session = Session()
    assert search.experts(session, "topic", k=5, evidence_limit=2) == [{
        "id": 7, "name": "Pat Smith", "score": 0.04, "evidence_count": 2,
        "evidence": [
            {"source_item_id": 10, "source": "gmail", "external_id": "m-10", "title": "Plan",
             "occurred_at": None, "involvement": ["authored", "mentioned"], "excerpt": "a" * 400},
            {"source_item_id": 20, "source": "gcal", "external_id": "e-20", "title": "Review",
             "occurred_at": None, "involvement": ["attended"], "excerpt": "event discussion"},
        ],
    }]
    assert session.params["item_ids"] == [10, 20]
    assert session.params["relevance"] == [0.03, 0.01]
    assert session.params["k"] == 5 and session.params["evidence_limit"] == 2








def test_topic_normalization_is_conservative_and_similarity_requires_lexical_evidence():
    from igneous.graph.topics import Topic, normalize_key, similar_pairs

    assert normalize_key("California Price Caps") == "california-price-cap"
    assert normalize_key("Quarterly Analyses") == "quarterly-analysis"
    assert normalize_key("Business News") == "business-news"  # not a plural noun we can safely change

    topics = [Topic(1, "california-price-cap", "California price cap"),
              Topic(2, "price-cap-policy", "California price cap policy"),
              Topic(3, "unrelated", "Houston operations")]
    pairs = list(similar_pairs(topics, [[1.0, 0.0], [0.999, 0.001], [1.0, 0.0]], threshold=0.96))
    assert [(left.id, right.id) for left, right, _ in pairs] == [(1, 2)]


def test_topic_related_to_uses_distinct_message_cooccurrences_and_own_extractor(monkeypatch):
    from igneous.graph import derived
    from igneous.graph.topics import EXTRACTOR, _related

    monkeypatch.setattr(derived, "rejections", lambda _s: derived.Rejections())

    class Result:
        rowcount = 4

    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((str(statement), params))
            return Result()

    session = Session()
    assert _related(session, 3) == 4
    delete, insert = session.calls
    assert "DELETE FROM edges WHERE type = 'RELATED_TO'" in delete[0]
    assert delete[1] == {"ex": EXTRACTOR}
    assert "count(DISTINCT a.src)" in insert[0]
    assert "a.dst < b.dst" in insert[0]
    assert insert[1]["minimum"] == 3 and insert[1]["ex"] == EXTRACTOR and insert[1]["blocked"] == []


def test_locate_spans():
    from igneous.graph.extract import locate

    body = "Please ask Jeff\n  Skilling and FERC about the caps."
    assert locate(body, "FERC") == (body.index("FERC"), body.index("FERC") + 4)
    s0, s1 = locate(body, "Jeff Skilling")  # survives the line wrap
    assert body[s0:s1].split() == ["Jeff", "Skilling"]
    assert locate(body, "Ken Lay") is None and locate(body, "  ") is None


def test_automatic_extraction_requires_explicit_source_opt_in(monkeypatch):
    from igneous import llm
    from igneous.graph import extract

    assert llm.allowed_sources("") == frozenset()
    assert llm.allowed_sources(" gmail, ai_chats ,gmail ") == frozenset({"gmail", "ai_chats"})

    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((str(statement), params))
            return type("Result", (), {"all": lambda self: []})()

    session = Session()
    messages = []
    monkeypatch.setattr(llm.settings, "llm_sources", "")
    assert extract.run(session, log=messages.append) == 0
    assert session.calls == []
    assert "IGNEOUS_LLM_SOURCES" in messages[0]

    monkeypatch.setattr(llm.settings, "llm_sources", "ai_chats")
    assert extract.run(session) == 0
    query, params = session.calls[0]
    assert "si.source = ANY(CAST(:sources AS text[]))" in query
    assert params["sources"] == ["ai_chats"]


def test_ask_retrieves_only_explicitly_approved_sources(monkeypatch):
    from igneous.rag import qa
    from igneous.rag.search import Filters

    captured = []
    monkeypatch.setattr(qa, "apply", lambda _s, _q, f: f or Filters())
    monkeypatch.setattr(qa.llm, "allowed_sources", lambda: frozenset({"ai_chats"}))
    monkeypatch.setattr(qa, "hybrid", lambda _s, _q, f, **_kwargs: captured.append(f) or [])
    assert qa.answer(object(), "What did I decide?")["answerable"] is False
    assert captured[0].sources == ("ai_chats",)


def test_ask_with_nothing_opted_in_calls_no_model_and_searches_nothing(monkeypatch):
    from igneous.rag import qa
    from igneous.rag.search import Filters

    def forbidden(*_args, **_kwargs):
        raise AssertionError("must not run")

    monkeypatch.setattr(qa, "hybrid", forbidden)
    monkeypatch.setattr(qa.llm, "structured", forbidden)
    monkeypatch.setattr(qa, "apply", forbidden)  # constraint parsing may call the model too
    monkeypatch.setattr(qa.llm.settings, "llm_sources", "")
    out = qa.answer(object(), "What did I decide?")
    assert out["answerable"] is False and out["reason"] == "no_opted_in_sources"
    assert "IGNEOUS_LLM_SOURCES" in out["answer"] and out["evidence"] == []

    # A question limited to sources that aren't opted in is not widened to every source.
    monkeypatch.setattr(qa.llm.settings, "llm_sources", "gmail")
    monkeypatch.setattr(qa, "apply", lambda _s, _q, f: Filters(sources=("imessage",)))
    out = qa.answer(object(), "What did Sam text me?")
    assert out["reason"] == "no_opted_in_sources" and "imessage" in out["answer"]


def test_local_sources_need_their_own_switch_as_well(monkeypatch):
    from igneous import llm

    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", False)
    assert llm.allowed_sources("fs,notes,gmail") == frozenset({"gmail"})
    monkeypatch.setattr(llm.settings, "local_sources_model_enabled", True)
    assert llm.allowed_sources("fs,notes,gmail") == frozenset({"fs", "notes", "gmail"})
    assert llm.allowed_sources("") == frozenset()  # the switch alone opts nothing in


def test_extraction_spans_index_the_stored_body():
    """Regression: offsets were computed on a .strip()-ed body and stored against the original (off by one)."""
    from igneous.graph.extract import locate
    from igneous.rag.chunking import QUOTE_START

    stored = "\n  Ask David Oxley.\n -----Original Message-----\nold"
    own = stored[: QUOTE_START.search(stored).start()]
    s0, s1 = locate(own, "David Oxley")
    assert stored[s0:s1] == "David Oxley"


def test_er_names_and_scoring():
    from igneous.graph.resolve import P, same_first, score, split_name, unresolvable

    assert split_name("Allen, Phillip K.", "pallen@enron.com") == ("phillip", "allen")
    assert split_name("", "jeff.skilling@enron.com") == ("jeff", "skilling")
    assert same_first("rick", "richard") and same_first("joe", "joseph") and not same_first("ann", "mark")

    def p(i, key, name, peers=()):
        f, last = split_name(name, key)
        return P(i, key, name, f, last, set(peers))

    shared = {10, 11, 12, 13}
    assert score(p(1, "james.steffes@enron.com", "James Steffes", shared), p(2, "steffes@enron.com", "James Steffes", shared))[0] >= 0.75
    assert score(p(1, "ken.lay@enron.com", "Ken Lay"), p(2, "ken_lay@enron.net", "Ken Lay"))[0] >= 0.75
    # Same surname, different first name: never merges no matter how much mail they share.
    assert score(p(1, "ann.schmidt@enron.com", "Ann Schmidt", shared), p(2, "mark.schmidt@enron.com", "Mark Schmidt", shared))[0] == 0
    assert unresolvable(p(3, ".john@enron.com", "e-mail")) and unresolvable(p(4, "dl-ga-all@enron.com", "DL GA All"))


def test_same_name_is_the_name_evidence_scoring_requires_either_way_round():
    from igneous.graph.resolve import same_name, split_name

    allen = split_name("Phillip K Allen", "phillip.allen@enron.com")
    for name, key in [("Allen, Phillip K.", "k..allen@enron.com"), ("Phil Allen", "phil@a.test"),
                      ("", "phillip.allen@b.test")]:
        theirs = split_name(name, key)
        assert same_name(allen, theirs) and same_name(theirs, allen), name or key
    for name, key in [("Rangel, Ina", "ina.rangel@enron.com"), ("Allen, Diana", "diana.allen@enron.com"),
                      ("Pam Allen", "pam@a.test"), ("P Allen", "p.allen@a.test"), ("Phillip", "pallen2@a.test"),
                      ("Pallen", "pallen@enron.com"), ("Phillip Allenby", "pa@a.test")]:
        theirs = split_name(name, key)
        assert not same_name(allen, theirs) and not same_name(theirs, allen), name


def test_er_rejects_mailing_lists():
    from igneous.graph.resolve import P, split_name, unresolvable

    for key, name in [("aei-bi_updates@www.brookings.org", "Aei-bi Updates"),
                      ("western.price.survey.contacts@proxy4.ba.best.com", "Western Price Survey Contacts")]:
        f, last = split_name(name, key)
        assert unresolvable(P(1, key, name, f, last, set()))


def test_quoted_header_parsing():
    from igneous.graph.quoted import _surname, parse_quotes

    body = ("Sounds good.\n\n -----Original Message-----\nFrom: Rice, Ken \nSent: Mon 8/20/2001 10:20 AM \n"
            "To: Skilling, Jeff \nSubject: RE: Astros tickets\n\nThanks for the tickets.")
    (q,) = parse_quotes(body)
    assert (q["sent"].month, q["sent"].day, q["sent"].hour, q["sent"].minute) == (8, 20, 10, 20)
    assert q["subject"] == "Astros tickets" and _surname(q["from"]) == "rice"
    from igneous.graph.quoted import _first
    assert _first("Rice, Ken") == "ken" and _first("Brad Schaeffer <b@x.com>") == "brad" and _first("Rice") == ""
    long = "-----Original Message-----\nFrom: Brad Schaeffer <BSchaeffer@prebon.com>@ENRON \nSent: Thursday, November 29, 2001 9:49 AM\nSubject: x\n"
    assert parse_quotes(long)[0]["sent"].year == 2001 and _surname(parse_quotes(long)[0]["from"]) == "schaeffer"
    assert parse_quotes("no quote here") == []


def test_cross_encoder_reranks_full_pool_then_limits(monkeypatch):
    from igneous.rag import rerank

    def scores(question, documents):
        assert question == "Which item is relevant?"
        assert documents == ["first", "second", "third"]
        return [0.1, 0.2, 0.9]

    monkeypatch.setattr(rerank, "score_documents", scores)
    monkeypatch.setattr(rerank, "_load_failure", None)
    items = [{"text": "first", "score": 1.0}, {"text": "second", "score": 0.5}, {"text": "third"}]
    assert rerank.rerank("Which item is relevant?", items, keep=2) == [
        {"text": "third", "rerank_score": 0.9},
        {"text": "second", "score": 0.5, "rerank_score": 0.2},
    ]


def test_cross_encoder_turned_off_leaves_the_order_to_its_caller(monkeypatch):
    from igneous.rag import rerank

    monkeypatch.setattr(rerank.settings, "reranker_enabled", False)
    monkeypatch.setattr(rerank, "score_documents", lambda *_: (_ for _ in ()).throw(AssertionError("must not load")))
    assert rerank.rerank("question", [{"text": "first"}, {"text": "second"}], keep=1) is None


def test_cross_encoder_failure_is_tried_once_and_leaves_the_order_to_its_caller(monkeypatch, caplog):
    from igneous.rag import rerank

    attempts = 0

    def unavailable(_question, _documents):
        nonlocal attempts
        attempts += 1
        raise OSError("model cache is offline")

    monkeypatch.setattr(rerank, "score_documents", unavailable)
    monkeypatch.setattr(rerank, "_load_failure", None)
    items = [{"text": "first"}, {"text": "second"}]
    assert rerank.rerank("question", items, keep=1) is None
    assert rerank.rerank("question", items, keep=1) is None
    assert attempts == 1
    assert "chooses evidence without it" in caplog.text


def test_contact_names_in_messages():
    from igneous.graph.traversal import name_handles, pretty_thread

    named = {"+16174075277": "Sam Lee", "6174075277": "Sam Lee", "+16149733915": "Ana Ruiz", "6149733915": "Ana Ruiz"}
    assert name_handles("[14:39] +16174075277: home rn", named) == "[14:39] Sam Lee: home rn"
    assert name_handles("[1:00] +19995550000: hi", named) == "[1:00] +19995550000: hi"  # unknown stays
    assert pretty_thread("+16149733915, +16174075277", named) == "Ana, Sam"
    assert pretty_thread("+16149733915, +16174075277, +19995550000, +18885550000", named).endswith("and 1 other")
    assert pretty_thread("Biggers in Paris", named) == "Biggers in Paris"


def test_service_addresses_are_not_people():
    from igneous.graph.self_identity import is_service_address

    assert is_service_address("cfa0123@import.calendar.google.com")
    assert is_service_address("notifications@github.com")
    assert is_service_address("no-reply@accounts.google.com")
    assert not is_service_address("12345+jli@users.noreply.github.com")  # a real person's commit address
    assert not is_service_address("jyl123@university.edu") and not is_service_address("+16145551234")




def test_entity_names_normalize_and_drop_generic():
    from igneous.graph.entities import _norm, _usable

    assert _norm("Gloomhaven Night (session 4)") == "gloomhaven night"
    assert _norm("Igneous - weekly sync") == "igneous"
    assert _norm("Gloomhaven Night #12") == "gloomhaven night"
    assert not _usable(_norm("Lunch"))
    assert not _usable(_norm("1:1"))
    assert not _usable("10/21")
    assert _usable("acme robotics")


def test_entity_screening_caches_model_verdicts(db, monkeypatch):
    import hashlib

    from igneous.graph import entities
    from igneous.llm import cache_lookup, cache_store

    calls = []
    monkeypatch.setattr(entities.settings, "llm_enabled", True)
    monkeypatch.setattr(entities, "_screen", lambda names: calls.append(names) or set(names))
    assert entities._screened(["igneous"], {"igneous"}) == {"igneous"}
    assert entities._screened(["igneous"], {"igneous"}) == {"igneous"}
    assert calls == [["igneous"]]
    key = "entities-screen:v1:" + hashlib.sha256(b"igneous").hexdigest()
    assert cache_lookup(key) == {"keep": True, "name": "igneous", "by": "model"}

    # A name whose anchors appear only in sources the model may not read is kept without asking, and that
    # fallback is not cached as a verdict: once the name may be shown, the model is asked.
    calls.clear()
    assert entities._screened(["lunch club"], set()) == {"lunch club"}
    assert calls == [] and cache_lookup("entities-screen:v1:" + hashlib.sha256(b"lunch club").hexdigest()) is None
    monkeypatch.setattr(entities, "_screen", lambda names: calls.append(names) or set())
    assert entities._screened(["lunch club"], {"lunch club"}) == set()
    assert calls == [["lunch club"]]

    # A cached keep that no model made is asked again once the name may be shown, and the verdict replaces it.
    key = "entities-screen:v1:" + hashlib.sha256(b"plain name").hexdigest()
    cache_store(key, {"keep": True, "name": "plain name"})
    assert entities._screened(["plain name"], {"plain name"}) == set()
    assert cache_lookup(key)["by"] == "model"


def test_unknown_numbers_are_masked_in_labels_only():
    from igneous.graph.traversal import name_handles

    named = {"+16145551234": "Sam Lee"}
    assert name_handles("+16145551234, +19995550000", named, mask=True) == "Sam Lee, ···0000"
    assert name_handles("call +19995550000", named) == "call +19995550000"


def test_no_sqlalchemy_param_casts_with_double_colon():
    """`:name::bigint` breaks SQLAlchemy text(): it reads `name:` as the parameter and Postgres sees a stray
    `:`. This crashed /timeline in production (tests with a fake session never sent the SQL to Postgres).
    Use CAST(:name AS bigint) instead."""
    import pathlib
    import re

    bad = []
    for path in pathlib.Path(__file__).resolve().parents[1].joinpath("igneous").rglob("*.py"):
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"(?<![:\w]):[A-Za-z_]\w*::[A-Za-z]", line):
                bad.append(f"{path.name}:{i}: {line.strip()}")
    assert not bad, "use CAST(:param AS type), not :param::type:\n" + "\n".join(bad)


def test_shared_event_and_drive_file_from_two_accounts_make_one_node():
    from igneous.graph import spine

    class B:
        def __init__(self):
            self.keys = []

        def node(self, type_, key, name=None, props=None, **_):
            self.keys.append((type_, key))
            return (type_, key)

        def edge(self, *a, **k):
            pass

    class Item:
        def __init__(self, source, external_id, meta):
            self.source, self.external_id, self.raw_meta = source, external_id, meta
            self.title, self.occurred_at, self.id = "x", None, 1

    b = B()
    for acct in ("a@x.com", "b@y.com"):
        spine.build_event(b, Item("gcal", f"{acct}/primary/evt", {"event_key": "gcal:uid-123@google.com",
                                                                   "series_key": f"{acct}/primary/evt"}))
        spine.build_file(b, Item("drive", f"{acct}/f1", {"drive_id": "f1", "path": "drive/f1", "parents": []}))
    assert {k for t, k in b.keys if t == "Event"} == {"gcal:uid-123@google.com"}
    assert {k for t, k in b.keys if t == "File"} == {"drive:f1"}


def test_relationship_claims_require_verifiable_quotes_and_stay_derived():
    from igneous.graph.history import store_claims

    class Result:
        def scalar_one(self):
            return 9

    class Session:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params):
            self.calls.append((str(statement), params))
            return Result()

    body = "I will send the draft by Friday. We decided to use Postgres."
    item = type("Item", (), {"id": 3, "node_id": 4, "occurred_at": datetime(2026, 1, 1)})()
    session = Session()
    stats = store_claims(session, item, body, {
        "commitments": [{"who": "sender", "what": "send the draft", "by_when": "Friday",
                           "quote": "I will send the draft by Friday."}],
        "decisions": [{"what": "use Postgres", "quote": "We decided to use Postgres."},
                      {"what": "unsupported", "quote": "not in body"}],
    }, "test-model")
    assert stats == {"commitments": 1, "decisions": 1, "dropped_claims": 1}
    node_sql, node_params = session.calls[0]
    assert "'derived'" not in node_sql and node_params["props"].find('"derived": true') >= 0
    edge_sql, edge_params = session.calls[1]
    assert "HAS_COMMITMENT" not in edge_sql and edge_params["type"] == "HAS_COMMITMENT"
    assert body[edge_params["start"]:edge_params["end"]] == "I will send the draft by Friday."




def test_spine_repo_evidence_uses_shared_repository_anchor():
    from igneous.graph.spine import build_file

    class B:
        def __init__(self):
            self.nodes, self.edges = [], []

        def node(self, *args, **kwargs):
            self.nodes.append(args)
            return len(self.nodes)

        def edge(self, *args, **kwargs):
            self.edges.append(args)

    item = type("Item", (), {"source": "fs", "external_id": "root/.repos/igneous", "title": "igneous",
        "raw_meta": {"path": "root/igneous", "folders": ["root", "root/igneous"], "kind": "repo", "repo": "igneous",
                     "remote": "git@github.com:a/igneous.git"}, "occurred_at": None, "id": 7})()
    builder = B()
    build_file(builder, item)
    assert ("Folder", "repo:igneous", "igneous", {"path": "root/igneous", "kind": "repo",
            "remote": "git@github.com:a/igneous.git"}) in builder.nodes


def test_organization_candidates_exclude_personal_domains_and_normalize_contact_companies():
    from igneous.graph.organizations import company_key, domain_of

    assert domain_of("pat@studio.vc") == "studio.vc"
    assert domain_of("pat@gmail.com") is None
    assert domain_of("not-an-email") is None
    assert company_key("  Studio Ventures, LLC ") == ("org:studio-ventures-llc", "Studio Ventures, LLC")
    assert company_key(" ") is None


def test_phone_contact_merges_with_email_on_exact_name_unless_ambiguous():
    from igneous.graph.resolve import P, score, split_name

    def p(i, key, name):
        first, last = split_name(name, key)
        return P(i, key, name, first, last, set())

    phone, email = p(1, "+16145551234", "Sam Lee"), p(2, "sam.lee@x.com", "Sam Lee")
    assert phone.is_phone and not phone.is_email and email.is_email
    assert score(phone, email)[0] >= 0.5
    assert score(phone, p(3, "s.lee@y.com", "Sara Lee"))[0] == 0


def test_self_is_named_with_a_real_name_not_a_handle():
    from igneous.graph.resolve import _human_name

    assert not _human_name("Jdoe2446", {"jdoe2446"})
    assert not _human_name("Jane", set())
    assert _human_name("Jane Doe", {"jdoe2446"})


def test_recency_half_life_and_cross_category_scale():
    from igneous.graph.traversal import HALF_LIFE_DAYS

    weight = lambda days: 0.5 ** (days / HALF_LIFE_DAYS)  # noqa: E731
    assert weight(7) > 0.9 and weight(180) < 0.3 and weight(730) < 0.01

    # A category whose best item is weak can't outrank a strong one at the top of the first ring.
    tops = {"People": 10.0, "Events": 1.0}
    overall = max(tops.values())
    scaled = lambda c, raw: raw / tops[c] * (0.4 + 0.6 * tops[c] / overall)  # noqa: E731
    assert scaled("People", 10.0) == 1.0 and scaled("Events", 1.0) < 0.5


def test_product_relay_senders_are_services_not_people():
    from igneous.graph.resolve import P, _human_name, unresolvable
    from igneous.graph.self_identity import is_relay_name, is_service_address

    assert is_service_address("comments-noreply@docs.google.com")
    assert is_service_address("drive-shares-dm-noreply@google.com")
    assert is_relay_name("Sam Lee (Google Docs)") and is_relay_name("Alex Ng (via Google Chat)")
    assert not is_relay_name("Sam Lee") and not is_service_address("jane.doe@gmail.com")
    assert unresolvable(P(1, "comments-noreply@docs.google.com", "Sam Lee (Google Docs)", "sam", "lee", set()))
    assert not _human_name("Sam Lee (Google Docs)", set())


def test_group_expand_reveals_every_participant_not_top_k():
    from igneous.graph import traversal

    assert traversal.GROUP_MAX >= 20  # a group shows all who take part (up to a readable cap), not 6


def test_life_thread_bursts_and_sections_from_kinds():
    from datetime import datetime, timedelta

    from igneous.graph.threads import _bursts, _terms
    from igneous.graph.traversal import section_for_kind

    assert "Portland" in _terms("Flight to Portland on Friday") and "Friday" not in _terms("see you Friday")
    assert "CS 401" in _terms("CS 401 final report")

    class R:
        def __init__(self, day, src):
            self.occurred_at, self.source = datetime(2026, 1, 1) + timedelta(days=day), src

    # a term mentioned once a month for two years, then a 3-week spike across sources
    background = [R(d, "gmail") for d in range(0, 720, 30)]
    spike = [R(700 + i, s) for i, s in enumerate(["gmail", "imessage", "gcal", "fs", "gmail", "imessage", "drive"])]
    its = sorted(background + spike, key=lambda r: r.occurred_at)
    windows = _bursts(its)
    assert windows
    assert its[windows[-1][0]].occurred_at >= datetime(2026, 1, 1) + timedelta(days=660)  # the spike, not the background

    assert section_for_kind("trip") == "Trips" and section_for_kind("course") == "Courses"
    assert section_for_kind("TA job") == "Work" and section_for_kind("client project") == "Projects"
    assert section_for_kind("book club") == "Hobbies" and section_for_kind("research lab") == "Projects"
    assert section_for_kind("fundraiser") == "Fundraisers"  # a new kind becomes its own section
    # Whole words only: a word that merely starts like a section's ("tabletop", "tax", "classical") is not it.
    assert section_for_kind("tabletop game") == "Hobbies" and section_for_kind("tax filing") == "Filings"
    assert section_for_kind("classical music") == "Hobbies" and section_for_kind("talk series") == "Series"
    # A kind never takes the name of a section of other things: a reading group is not a group chat.
    assert section_for_kind("reading group") == "Reading groups" and section_for_kind("organization") == "Organizations"


def test_verifier_snippets_claims_and_merge_safety():
    from datetime import datetime

    from igneous.graph import verify

    item = {"body": "lots of unrelated words " * 30 + " see you at Gloomhaven Night on Friday " + "more text " * 30,
            "title": "plans", "source": "gmail", "occurred_at": datetime(2026, 8, 1), "recipients": 2}
    assert "Gloomhaven Night" in verify._snippet(item, ["Gloomhaven Night"])  # the part that mentions the claim, not line 1
    edge = {"type": "INVOLVED_IN", "src_id": 1, "src_type": "Person", "src_name": "A", "dst_id": 2,
            "dst_type": "Project", "dst_name": "Gloomhaven Night", "dst_kind": "tabletop game"}
    desc = verify._describe(edge, [item], names={1: "Ana"})
    assert "2 recipients" in desc and "A = Person 'Ana'" in desc and len(verify.digest(("INVOLVED_IN", "threads"), desc)) == 64
    # every claim the verifier judges is phrased (merges are split only by a confident drop: tests/test_verifier.py)
    assert ("SAME_AS", "er") in verify.CLAIMS and ("PART_OF", "hierarchy") in verify.CLAIMS


def test_ask_suggestions_templates_skip_missing_slots():
    from igneous.suggestions import build

    assert build([], []) == []
    assert build(["Ana"], []) == ["What have Ana and I talked about lately?"]
    full = build(["Ana", "Bo"], [{"name": "Lisbon", "kind": "trip"}, {"name": "Atlas", "kind": "project"},
                                 {"name": "CS 401", "kind": "class"}, {"name": "Extra", "kind": "thread"}])
    assert full == ["Who's working on Atlas with me?", "When is the Lisbon?",
                    "What have Ana and I talked about lately?", "What did we decide about CS 401?",
                    "What have Bo and I talked about lately?"]
    assert build([], [{"name": "Atlas", "kind": "project"}]) == [
        "Who's working on Atlas with me?", "What did we decide about Atlas?"]
    assert build([], [{"name": "Lisbon", "kind": "trip"}]) == ["When is the Lisbon?"]


def test_thread_clusters_merge_only_on_direct_evidence():
    from igneous.graph.resolve_threads import _clusters_agree

    same = {"relation": "same", "confidence": 0.9}
    names = {1: "Atlas platform", 2: "atlas-platform (client app)", 3: "Atlas Platform MVP", 4: "Portland trip"}
    assert _clusters_agree({1, 2}, {3}, {(1, 3): same, (2, 3): same}, names)
    assert _clusters_agree({1, 2}, {3}, {(1, 3): same}, names)  # half the pairs judged is enough
    # an unrelated thread can't ride in on its neighbours' judgments, nor merge without sharing a name word
    assert not _clusters_agree({1}, {4}, {(1, 4): same}, names)
    assert not _clusters_agree({1, 2}, {3}, {(1, 3): same, (2, 3): {"relation": "related", "confidence": 0.9}}, names)
    assert not _clusters_agree({1}, {3}, {}, names)


def test_thread_terms_match_whole_words_and_course_codes_are_whole():
    import re

    from igneous.graph.resolve_threads import _tokens

    # Same number, different department: different courses, nothing shared.
    assert not _tokens("CS 401") & _tokens("EE 401 Senior Design Seminar")
    assert _tokens("CS 401") & _tokens("cs401 final presentation")
    assert "course:ee401" in _tokens("EE 401") & _tokens("EE 401 Senior Design Seminar")
    pat = re.compile(r"(?<![a-z0-9])(atlas)(?![a-z0-9])")
    assert pat.search("the atlas platform demo") and not pat.search("fatlas testing") and not pat.search("atlases")


def test_links_you_stated_are_never_judged_by_the_model():
    from igneous.graph import verify
    from igneous.graph.derived import STATED

    # the verifier only judges model-derived links; stated ones (yours: extractor 'user') are never in its claims
    assert all(claim[1] not in STATED for claim in verify.CLAIMS)


def test_person_merge_is_a_trusted_user_link(db):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from igneous.graph import resolve
    from igneous.main import app

    a, b = (db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', :k, 'Sam Lee') RETURNING id"),
                       {"k": k}).scalar_one() for k in ("sam@a.test", "sam@b.test"))
    answer = TestClient(app).post(f"/graph/person/{b}/merge", json={"other": a}).json()
    assert answer["applied"] == "now" and answer["person"] in (a, b)
    # stored as your identity link, an intent by natural keys, which resolution always honors on every rebuild
    assert db.execute(text("SELECT op, value FROM overrides")).one() == ("link", "SAME_AS")
    assert db.execute(text("SELECT src, dst FROM edges WHERE type = 'SAME_AS' AND extractor = 'user'")).one() == (a, b)
    assert "user" in resolve.TRUSTED


def test_profile_only_counts_documents_about_you():
    from igneous.graph.profile import _date, _is_me

    assert _is_me("Jane Q. Doe", {"jane doe", "jdoe"})
    assert not _is_me("John Smith", {"jane doe"})           # a friend's resume in your Drive isn't yours
    assert not _is_me("Jane", {"jane doe"})                 # a first name alone isn't proof
    assert _date("2026-06") == "2026-06-01" and _date("present") is None


def test_earned_cut_keeps_the_strong_ones():
    from igneous.graph.traversal import earned_cut

    assert earned_cut([(1, 100), (2, 90), (3, 10), (4, 9)]) == [1, 2]     # a clear drop after the strong two
    assert len(earned_cut([(i, 50 - i) for i in range(10)])) == 10        # ten similar ones all show
    assert earned_cut([(1, 5)]) == [1]




def test_threads_naming_each_other_are_judged_even_far_apart():
    from datetime import datetime

    from igneous.graph.resolve_threads import candidate_pairs

    a = {"id": 1, "name": "CS 401", "items": {1}, "people": set(), "a": datetime(2025, 1, 1), "b": datetime(2025, 5, 1)}
    b = {"id": 2, "name": "Igneous", "items": {9}, "people": set(), "a": datetime(2026, 4, 1), "b": datetime(2026, 10, 1)}
    assert candidate_pairs([a, b]) == []                     # nothing shared and far apart: not judged
    assert len(candidate_pairs([a, b], {(2, 1)})) == 1       # Igneous's records name CS 401: judged


def test_people_are_judged_against_the_threads_core():
    from igneous.graph.threads import relative_members

    busy = [(1, 22), (2, 20), (3, 18), (4, 2), (5, 1), (6, 3)]
    assert relative_members(busy) == {1, 2, 3, 6}            # bar = max(2, 0.15*20) = 3
    assert relative_members(busy, keep={5}) == {1, 2, 3, 5, 6}  # in the group chat: always stays
    small = [(1, 3), (2, 2), (3, 2)]
    assert relative_members(small) == {1, 2, 3}             # small thread: everyone counts


def test_a_persons_messages_and_one_on_one_chats_fold_into_them():
    import inspect

    from igneous.graph import traversal

    world = inspect.getsource(traversal.person_world)
    assert "'Message'" not in world and "it.src IN (SELECT m FROM mine)" not in world
    assert "x.type = 'Message'" in inspect.getsource(traversal.fallback_neighbors)




def test_bot_accounts_are_services():
    from igneous.graph.self_identity import is_service_address

    assert is_service_address("github:vercel[bot]") and is_service_address("github:dependabot[bot]")
    assert not is_service_address("github:samlee")
    assert not is_service_address("1+someone@users.noreply.github.com")


def test_a_projects_people_get_one_cut_not_two():
    from igneous.graph.traversal import _core_people

    # One traveller texted about the trip far more than the others: all three still show.
    assert _core_people([(1, 28.0), (2, 8.0), (3, 7.0)]) == [1, 2, 3]
    assert _core_people([(1, 30.0), (2, 25.0), (3, 22.0), (4, 1.0)]) == [1, 2, 3]  # a passing mention goes


def test_mcp_server_starts_light_and_exposes_graph_tools():
    import subprocess
    import sys

    # The model/search stacks must not load at import: a slow start races the client's first tool call.
    code = "import sys, igneous.mcp_server as m; print('igneous.rag.search' in sys.modules, 'anthropic' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.split()
    assert out == ["False", "False"]
    from igneous import mcp_server as m
    for tool in ("me", "find", "open_node", "why_connected", "sources", "read_item", "search", "ask"):
        assert callable(getattr(m, tool))


def test_an_umbrella_still_shows_its_own_content():
    import inspect

    from igneous.graph import traversal

    # An umbrella still shows its own repo, people and chats after its sub-projects.
    assert "_own_content" in inspect.getsource(traversal.children)


def test_ongoing_threads_have_no_window_and_nested_names_are_judged():
    from datetime import datetime

    from igneous.graph.resolve_threads import candidate_pairs
    from igneous.graph.threads import ONGOING

    assert ONGOING.search("hobby") and ONGOING.search("club") and not ONGOING.search("trip")
    assert not ONGOING.search("club project")  # a bounded thing done by a club
    a = {"id": 1, "name": "Climbing", "items": {1}, "people": set(), "a": datetime(2024, 1, 1), "b": datetime(2026, 1, 1)}
    b = {"id": 2, "name": "Climbing Club", "items": {9}, "people": set(), "a": datetime(2024, 8, 1), "b": datetime(2025, 4, 1)}
    assert len(candidate_pairs([a, b])) == 1  # nothing shared, but one name is inside the other
