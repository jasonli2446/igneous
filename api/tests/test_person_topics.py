"""What you talk about with someone (#154): a person's conversations with you, clustered from their stored embeddings,
weighted by recency as closeness is, split into lately and earlier, named, and linked to their records and threads.

A small graph: you (two addresses) and Dana (two addresses) talk about the garden lately, rent a while ago and a
Lisbon trip in between, part in a group chat with Bo and filed under a life thread. A record's vector is what every
record of its source shares (the source's own direction) plus its topic's direction and a little noise, and you and Bo
talk about everything else, so the topics are what the clustering must find once each source's centre is taken out.
The traps are a notification, a message Dana sent only to Bo, a record not embedded yet, and a hidden thread.
"""
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from igneous.config import settings
from igneous.graph import dirty
from igneous.graph import person_topics as pt
from igneous.main import app

NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)
DIM = 384
GARDEN, RENT, LISBON, STRAY = 0, 1, 2, 3  # each topic's direction
SOURCE_AXIS = {"imessage": 100, "gmail": 101}  # what every record of a source shares


class Talk:
    def __init__(self, s):
        self.s, self.rng, self.items = s, np.random.default_rng(154), {}

    def node(t, type_, key, name, item=None, **props) -> int:  # `t`, not `self`: a Person's props hold "self"
        return t.s.execute(text("""INSERT INTO nodes (type, key, name, props, source_item_id)
                                   VALUES (:t, :k, :n, CAST(:p AS jsonb), :i) RETURNING id"""),
                           {"t": type_, "k": key, "n": name, "p": json.dumps(props), "i": item}).scalar_one()

    def edge(self, a, b, type_, extractor="spine", **props) -> None:
        self.s.execute(text("""INSERT INTO edges (src, dst, type, extractor, props)
                               VALUES (:a, :b, :t, :x, CAST(:p AS jsonb))"""),
                       {"a": a, "b": b, "t": type_, "x": extractor, "p": json.dumps(props)})

    def say(self, ref, sender, to, days_ago, axis, body, title=None, source="imessage", group=None, thread=None,
            item_class="conversation", embedded=True) -> int:
        """A record `sender` sent to `to`, `days_ago` before NOW, whose vector points along `axis`."""
        when = NOW - timedelta(days=days_ago)
        title = title or f"Chat with Dana Kim — {when:%Y-%m-%d}"
        item = self.s.execute(text("""
            INSERT INTO source_items (source, external_id, content_hash, title, body_text, occurred_at, item_class)
            VALUES (:s, :e, :e, :t, :b, :w, :c) RETURNING id"""),
            {"s": source, "e": ref, "t": title, "b": body, "w": when, "c": item_class}).scalar_one()
        m = self.node("Message", f"{source}:{ref}", title, item=item, item_class=item_class,
                      hidden=item_class != "conversation")
        self.edge(sender, m, "SENT")
        for r in to:
            self.edge(m, r, "RECEIVED")
        if group:
            self.edge(m, group, "IN_THREAD")
        if thread:
            self.edge(m, thread, "PART_OF", "threads")
        vector = np.zeros(DIM)
        vector[SOURCE_AXIS[source]], vector[axis] = 0.6, 0.8
        vector += self.rng.normal(0, 0.01, DIM)
        self.s.execute(text("""INSERT INTO chunks (source_item_id, node_id, ord, text, embedding, embedding_model)
                               VALUES (:i, :n, 0, :t, CAST(:v AS vector), :m)"""),
                       {"i": item, "n": m, "t": f"{title}\n{body}", "v": str(vector.tolist()) if embedded else None,
                        "m": settings.embedding_model if embedded else None})
        self.items[ref] = item
        return m


@pytest.fixture
def talk(db):
    t = Talk(db)
    t.me = t.node("Person", "me", "Me Myself", self=True)
    t.me_work = t.node("Person", "me@work.test", "Me Myself")
    t.dana = t.node("Person", "dana@home.test", "Dana Kim")
    t.dana2 = t.node("Person", "dana.kim@work.test", "Dana Kim")
    t.bo = t.node("Person", "bo@x.test", "Bo Brown")
    db.execute(text("INSERT INTO canonical VALUES (:a, :a), (:b, :a), (:c, :c), (:d, :c)"),
               {"a": t.me, "b": t.me_work, "c": t.dana, "d": t.dana2})
    t.crew = t.node("Thread", "imessage:crew", "Trip crew", conversational=True, hidden=False)
    t.lisbon = t.node("Project", "thread:lisbon", "Lisbon · Mar 2026", derived="threads", kind="trip")
    t.secret = t.node("Project", "thread:secret", "Secret plan", derived="threads", kind="project", hidden=True)
    # Lately: the garden, one-on-one, from both of each one's addresses.
    garden = ["[18:01] Dana Kim: the tomatoes are ripe\n[18:05] Me Myself: the garden looks great",
              "[09:12] Me Myself: watered the garden and the tomatoes\n[09:30] Dana Kim: thanks!",
              "[20:44] Dana Kim: garden party on Saturday?\n[20:50] Me Myself: yes, I'll bring tomatoes",
              "[11:02] Me Myself: the garden needs a fence\n[11:10] Dana Kim: tomatoes are safe for now"]
    for i, (days, body) in enumerate(zip((5, 12, 20, 40), garden, strict=True)):
        a, b = (t.dana, t.me) if i % 2 else (t.me_work, t.dana2)
        t.say(f"garden{i}", a, [b], days, GARDEN, body, thread=t.secret if i == 0 else None)
    # Between: the Lisbon trip, filed under its thread; two of it in the group chat with Bo.
    t.say("lisbon0", t.me, [t.dana], 120, LISBON, "Flights to Lisbon are set for March 8.", "Lisbon flights",
          source="gmail", thread=t.lisbon)
    t.say("lisbon1", t.dana2, [t.me], 130, LISBON, "Lisbon hostel by the river, two nights.", "Re: Lisbon flights",
          source="gmail", thread=t.lisbon)
    t.say("lisbon2", t.bo, [t.me, t.dana], 140, LISBON, "[10:00] Bo Brown: Lisbon tram tickets\n[10:05] Dana Kim: yay",
          title="Trip crew — chat", group=t.crew, thread=t.lisbon)
    t.say("lisbon3", t.dana, [t.me, t.bo], 150, LISBON, "[12:00] Dana Kim: Lisbon pastries list\n[12:01] Bo Brown: ok",
          title="Trip crew — chat", group=t.crew, thread=t.lisbon)
    # A while ago: rent and the landlord, by mail.
    for i, days in enumerate((200, 250, 300)):
        t.say(f"rent{i}", t.dana if i % 2 else t.me, [t.me if i % 2 else t.dana], days, RENT,
              "Hi Dana,\n\nThe landlord wants the rent by Friday.\n\nMe", "Rent and the landlord" if i else "Rent",
              source="gmail")
    # Everything else you talk about, with Bo: each source's centre is mostly what its records share.
    for i in range(40):
        for source in ("imessage", "gmail"):
            t.say(f"bo-{source}-{i}", t.bo, [t.me], 10 + i, 200 + i + (40 if source == "gmail" else 0),
                  f"Bo's news {i}", f"News {i}", source=source)
    # One stray record (no topic of one), and what never counts.
    t.say("stray", t.dana, [t.me], 60, STRAY, "Did you see the eclipse?")
    t.say("notice", t.dana, [t.me], 3, GARDEN, "Dana Kim shared a garden album with you", item_class="notification")
    t.say("to-bo", t.dana, [t.bo], 4, GARDEN, "[08:00] Dana Kim: garden tomatoes for Bo")
    t.say("unembedded", t.dana, [t.me], 6, GARDEN, "[09:00] Dana Kim: garden again", embedded=False)
    db.execute(text("UPDATE graph_version SET version = version + 1"))
    return t


@pytest.fixture
def model_off(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", False)


def topics(t, node=None, **kw) -> dict:
    return pt.person_topics(t.s, node or t.dana, now=NOW, **kw)


def ids(t, *refs) -> list[int]:
    return [t.items[r] for r in refs]


def test_conversations_cluster_into_topics_split_lately_and_earlier(talk, model_off):
    t = talk
    out = topics(t)
    assert out["person"] == {"id": t.dana, "name": "Dana Kim"}
    assert out["since"] == (NOW - timedelta(days=90)).isoformat()
    assert out["records"] == 12  # 4 garden, 4 Lisbon, 3 rent, the stray: nothing else is a conversation with you
    (garden,) = out["lately"]
    assert garden["item_ids"] == ids(t, "garden0", "garden1", "garden2", "garden3")  # newest first
    assert (garden["records"], garden["recent"]) == (4, 4)
    assert [x["id"] for x in garden["items"]] == garden["item_ids"]
    lisbon, rent = out["earlier"]  # the trip is more recent than the rent: it weighs more
    assert lisbon["item_ids"] == ids(t, "lisbon0", "lisbon1", "lisbon2", "lisbon3")
    assert rent["item_ids"] == ids(t, "rent0", "rent1", "rent2")
    assert rent["first"] == (NOW - timedelta(days=300)).isoformat() and rent["recent"] == 0
    assert out["more"] == {"lately": 0, "earlier": 0}


def test_a_topic_links_its_thread_and_group_chat_but_never_a_hidden_one(talk, model_off):
    t = talk
    out = topics(t)
    (garden,), (lisbon, rent) = out["lately"], out["earlier"]
    assert lisbon["thread"] == {"id": t.lisbon, "name": "Lisbon · Mar 2026"}
    assert lisbon["conversations"] == [{"id": t.crew, "name": "Trip crew", "records": 2}]
    assert lisbon["sources"] == ["gmail", "imessage"]
    assert garden["thread"] is None and garden["conversations"] == []  # the secret thread is hidden
    assert rent["thread"] is None
    linked = [x["thread"]["id"] for x in out["lately"] + out["earlier"] if x["thread"]]
    assert linked == [t.lisbon]


def test_a_thread_you_rejected_for_them_is_not_linked(talk, model_off):
    t = talk
    t.s.execute(text("""INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, reason, source)
                        VALUES ('Person:dana@home.test', 'Project:thread:lisbon', 'INVOLVED_IN', 'threads', 'user',
                                'drop', 'not her trip', 'user')"""))
    t.s.execute(text("UPDATE graph_version SET version = version + 1"))
    (lisbon, _rent) = topics(t)["earlier"]
    assert lisbon["item_ids"] == ids(t, "lisbon0", "lisbon1", "lisbon2", "lisbon3") and lisbon["thread"] is None


def test_names_are_the_distinctive_terms_with_the_model_off(talk, model_off):
    t = talk
    out = topics(t)
    (garden,), (lisbon, rent) = out["lately"], out["earlier"]
    assert garden["terms"][:2] == ["garden", "tomatoes"] and garden["named_by"] == "terms"
    assert garden["name"] == " · ".join(garden["terms"])
    assert lisbon["terms"][0] == "Lisbon"  # always written capitalized: a name
    assert set(rent["terms"][:2]) == {"rent", "landlord"}
    for topic in (garden, lisbon, rent):  # a speaker or a greeting is never a topic
        assert not {"dana", "kim", "me", "myself", "bo", "brown"} & {w.lower() for w in topic["terms"]}


def test_records_split_like_closeness_and_a_person_is_every_identity(talk, model_off):
    t = talk
    assert topics(t, t.dana2) == topics(t)  # another address of hers is her
    c = pt.clustered(t.s, t.dana)
    split = {r.id: r.split for r in c.records}
    assert split[t.items["lisbon2"]] == 0.5 and split[t.items["lisbon0"]] == 1.0  # three on it: halved
    fresh = pt.Record(1, "gmail", NOW, 1.0, None, None, (), (), "")
    old = pt.Record(2, "gmail", NOW - timedelta(days=90), 1.0, None, None, (), (), "")
    assert pt.weight(fresh, NOW) == 1.0 and pt.weight(old, NOW) == pytest.approx(0.5)


def test_limit_and_more(talk, model_off):
    t = talk
    out = topics(t, limit=1)
    assert [len(out["lately"]), len(out["earlier"])] == [1, 1]
    assert out["more"] == {"lately": 0, "earlier": 1}


def test_without_an_owner_their_conversations_are_theirs(talk, model_off):
    t = talk
    t.s.execute(text("UPDATE nodes SET key = 'not-me' WHERE id = :i"), {"i": t.me})
    t.s.execute(text("UPDATE graph_version SET version = version + 1"))
    garden = topics(t)["lately"][0]
    assert t.items["to-bo"] in garden["item_ids"]  # nobody is "you": what she wrote Bo counts too


def test_the_model_names_each_topic_once_within_its_budget(talk, monkeypatch):
    t = talk
    calls = []

    def model(system, user):
        calls.append(user)
        return {"tomatoes": "the garden", "Lisbon": "the Lisbon trip"}.get(
            next((k for k in ("tomatoes", "Lisbon") if k in user), ""), "rent and the landlord")

    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_sources", "gmail,imessage")
    out = topics(t, model=model)
    assert [x["name"] for x in out["lately"] + out["earlier"]] == ["the garden", "the Lisbon trip",
                                                                   "rent and the landlord"]
    assert all(x["named_by"] == "model" for x in out["lately"] + out["earlier"]) and len(calls) == 3
    assert topics(t, model=model)["lately"][0]["name"] == "the garden" and len(calls) == 3  # cached
    # Only opted-in records are quoted: with the texts out, the garden (all texts) is named by its terms.
    monkeypatch.setattr(settings, "llm_sources", "gmail")
    calls.clear()
    out = topics(t, model=model)
    assert out["lately"][0]["named_by"] == "terms" and all("tomatoes" not in c for c in calls)
    assert all("Lisbon tram" not in c for c in calls)  # the group chat's texts aren't quoted either


def test_a_request_spends_at_most_its_budget_and_a_failure_keeps_the_terms(talk, monkeypatch):
    t = talk
    calls = []

    def model(system, user):
        calls.append(user)
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "llm_sources", "gmail,imessage")
    monkeypatch.setattr(pt, "NAME_BUDGET", 2)
    out = topics(t, model=model)
    assert len(calls) == 2 and all(x["named_by"] == "terms" for x in out["lately"] + out["earlier"])


def test_prompts_quote_only_short_excerpts_of_their_own_records():
    records = tuple(pt.Record(i, source, NOW - timedelta(days=i), 1.0, None, None, (), (), text_)
                    for i, (source, text_) in enumerate([("gmail", "Lisbon flights: set for March 8"),
                                                         ("imessage", "PRIVATE text"), ("gmail", "")]))
    user, quoted = pt.prompt(records, (0, 1, 2), frozenset({"gmail"}))
    assert user == f"Excerpts:\n- {(NOW):%Y-%m-%d}: Lisbon flights: set for March 8" and quoted == [0]
    assert pt.prompt(records, (1,), frozenset({"gmail"})) == ("", [])
    heading, body = pt.own_text("imessage", "Chat with Dana — 2026-05-01", "[18:01] Dana Kim: hi\n[18:02] Me: yo")
    assert (heading, body) == ("", "hi\nyo")
    heading, body = pt.own_text("gmail", "Re: Fwd: Rent", "The landlord wants it.\n\nOn Mon, Dana wrote:\n> old")
    assert heading == "Rent" and body == "The landlord wants it."
    assert pt.excerpt("imessage", "", "hi\nyo") == "hi / yo"
    long = pt.excerpt("gmail", "Rent", "word " * 200)
    assert long.startswith("Rent: word word") and long.endswith("…") and len(long) <= pt.EXCERPT_CHARS


def test_terms_are_phrases_without_stopwords_and_course_codes():
    found, forms = pt.grams("Our CS 401 group: the water samples came back clean, Boulder Barn at 7", frozenset())
    assert ("cs 401",) in found and forms[("cs 401",)] == "CS 401"
    assert ("water", "samples") in found and ("samples", "came") not in found  # 'came' says nothing
    assert ("boulder", "barn") in found and ("clean", "boulder") not in found  # a comma ends a phrase
    assert not any("the" in g or "our" in g for g in found)


def test_agglomerate_follows_the_threads_and_the_threshold():
    a, b, c = np.eye(3)
    near_a = a * 0.9 + b * 0.1
    vectors = np.array([a, near_a / np.linalg.norm(near_a), b, c])
    sim = vectors @ vectors.T
    assert pt.agglomerate(sim, [None] * 4, 0.5) == [[0, 1], [2], [3]]
    # One thread is one cluster however far apart; two threads never join however close.
    assert pt.agglomerate(sim, [None, None, 7, 7], 0.5) == [[0, 1], [2, 3]]
    assert pt.agglomerate(sim, [5, 6, None, None], 0.5) == [[0], [1], [2], [3]]
    # A record with no thread joins a thread's cluster it is close to, and the cluster keeps the thread.
    assert pt.agglomerate(sim, [5, None, 6, None], 0.5) == [[0, 1], [2], [3]]
    assert pt.agglomerate(np.zeros((0, 0)), [], 0.5) == []


def test_the_route(talk, model_off):
    t = talk
    client = TestClient(app)
    r = client.get(f"/graph/node/{t.dana2}/topics", params={"limit": 2})
    assert r.status_code == 200, r.text
    body = r.json()  # today, months after NOW: everything here is earlier
    assert body["person"] == {"id": t.dana, "name": "Dana Kim"} and body["records"] == 12
    assert [x["records"] for x in body["lately"] + body["earlier"]] == [4, 4] and body["more"]["earlier"] == 1
    assert client.get("/graph/node/99999999/topics").status_code == 404
    for node in (t.me, t.me_work, t.lisbon, t.crew):
        assert client.get(f"/graph/node/{node}/topics").status_code == 422
    for limit in (0, 21):
        assert client.get(f"/graph/node/{t.dana}/topics", params={"limit": limit}).status_code == 422


def test_clusters_are_kept_per_graph_version(talk, model_off, monkeypatch):
    t = talk
    calls = []
    real = pt.cluster
    monkeypatch.setattr(pt, "cluster", lambda s, head: calls.append(head) or real(s, head))
    dirty.forget()
    topics(t)
    topics(t)
    assert calls == [t.dana]
    t.s.execute(text("UPDATE graph_version SET version = version + 1"))
    topics(t)
    assert calls == [t.dana, t.dana]
