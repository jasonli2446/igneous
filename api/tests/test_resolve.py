"""Entity resolution: precise merges, pairs you rejected kept apart, clusters always current, names that follow them."""
import json

import pytest
from pipeline_data import card, day, local_models, mail, put  # noqa: F401
from sqlalchemy import text
from typer.testing import CliRunner

from igneous import cli, pipeline
from igneous.graph import corrections, dirty, verify

pytestmark = pytest.mark.usefixtures("local_models")
ME = "me@x.test"


def build(s, stages=("spine", "self", "resolve")) -> dict:
    s.commit()
    return pipeline.run(s, list(stages))


def cluster(s, key: str) -> set[str]:
    """The keys of the identities in `key`'s cluster (just `key` when it isn't merged)."""
    return set(s.execute(text("""
        WITH h AS (SELECT COALESCE(c.canonical_id, p.id) AS id FROM nodes p
                   LEFT JOIN canonical c ON c.node_id = p.id WHERE p.type = 'Person' AND p.key = :k)
        SELECT n.key FROM nodes n LEFT JOIN canonical c ON c.node_id = n.id
        WHERE n.type = 'Person' AND COALESCE(c.canonical_id, n.id) = (SELECT id FROM h)
    """), {"k": key}).scalars())


def scored_links(s) -> int:
    return s.execute(text("SELECT count(*) FROM edges WHERE type = 'SAME_AS' AND extractor = 'er'")).scalar()


def test_namesakes_who_only_wrote_to_you_stay_two_people(db):
    """Each John Smith writes to you and you answer; you are no shared correspondent."""
    names = {"john.smith@alpha-plumbing.test": "John Smith", "jsmith@beta-university.test": "John Smith"}
    for i, other in enumerate(names):
        mail(db, f"in{i}", other, [ME], f"Question {i}", day(2 * i), names=names)
        mail(db, f"out{i}", ME, [other], f"Re: Question {i}", day(2 * i + 1), sent=True, names=names)
    build(db)
    assert cluster(db, "john.smith@alpha-plumbing.test") == {"john.smith@alpha-plumbing.test"}
    assert scored_links(db) == 0


def test_four_alex_chens_and_a_phone_named_alex_chen_stay_five_people(db):
    addresses = ["alex.chen@north.test", "achen@south.test", "alex@east-lab.test", "chen.alex@west.test"]
    for i, a in enumerate(addresses):
        mail(db, f"in{i}", a, [ME], "Hi", day(i), names={a: "Alex Chen"})
        mail(db, f"out{i}", ME, [a], "Re: Hi", day(i + 0.5), sent=True)
    card(db, "card1", "Alex Chen", phones=["+16145550000"], source="maccontacts")
    put(db, "imessage", "chat1/2025-03-12", "Alex Chen", {"chat": "chat1", "participants": ["+16145550000"],
                                                          "senders": ["+16145550000", "me"]}, day(9))
    build(db)
    assert scored_links(db) == 0
    assert all(cluster(db, key) == {key} for key in [*addresses, "+16145550000"])


def test_one_person_with_two_addresses_and_a_phone_is_one_cluster(db):
    """Jordan writes from school and from home to the same two friends; his phone is on a contact card."""
    jordan = {"jordan.lee@school.test": "Jordan Lee", "jlee@home.test": "Jordan Lee"}
    for i, address in enumerate(jordan):
        for k in range(2):
            mail(db, f"j{i}{k}", address, ["ana@q.test", "bo@r.test"], f"Weekend {k}", day(3 * i + k), names=jordan)
        mail(db, f"me{i}", ME, [address], "Plans", day(10 + i), sent=True)
    card(db, "card1", "Jordan Lee", phones=["+16145551234"], source="maccontacts")
    build(db)
    assert cluster(db, "jordan.lee@school.test") == {"jordan.lee@school.test", "jlee@home.test", "+16145551234"}


def test_scoring_never_adds_anyone_to_your_own_cluster(db):
    """Another Mo Self at your domain writes to your colleagues; only identity signals say an address is yours."""
    names = {ME: "Mo Self", "mo@x.test": "Mo Self"}
    for i, sender in enumerate(names):
        for k in range(2):
            mail(db, f"m{i}{k}", sender, ["kai@x.test", "lu@x.test"], f"Status {k}", day(2 * k + i), names=names,
                 sent=sender == ME)
    build(db)
    assert "mo@x.test" not in cluster(db, ME) and cluster(db, "mo@x.test") == {"mo@x.test"}
    assert ME in cluster(db, "me")


def sam_lees(s) -> list[str]:
    """Three Sam Lees with one local part at three domains: each pair scores as one person."""
    keys = ["sam.lee@a.test", "sam.lee@b.test", "sam.lee@c.test"]
    for i, key in enumerate(keys):
        mail(s, f"s{i}", key, ["boss@a.test"], f"Update {i}", day(i), names={key: "Sam Lee"})
    return keys


def test_a_pair_you_rejected_stays_apart_on_every_rebuild_even_through_others(committed_db):
    s = committed_db
    a, b, c = sam_lees(s)
    build(s)
    assert cluster(s, a) == {a, b, c}
    verify.record_correction(s, f"Person:{a}", f"Person:{b}", "SAME_AS", "er", "drop", "two different Sams")
    mail(s, "s9", c, ["boss@a.test"], "Update 9", day(9))  # a new record: pairs are scored again
    result = build(s)  # self and resolve only: no later stage is needed to keep them apart
    assert result["resolve"]["result"]["rescored"]
    assert b not in cluster(s, a)
    assert not s.execute(text("""SELECT 1 FROM edges e JOIN nodes x ON x.id = e.src JOIN nodes y ON y.id = e.dst
                                 WHERE e.type = 'SAME_AS' AND ARRAY[x.key, y.key] @> ARRAY[:a, :b]"""),
                         {"a": a, "b": b}).first()


def test_clusters_follow_trusted_links_when_no_record_changed(db):
    """You say two addresses are one person; nothing new arrived, so nothing is scored again, yet they merge."""
    mail(db, "a", "ana@a.test", [ME], "Hi", day(0), names={"ana@a.test": "Ana Ames"})
    mail(db, "b", "aa@b.test", [ME], "Hey", day(1), names={"aa@b.test": "A. Ames"})
    build(db)
    db.execute(text("""INSERT INTO edges (src, dst, type, extractor, confidence)
                       SELECT a.id, b.id, 'SAME_AS', 'user', 1.0 FROM nodes a, nodes b
                       WHERE a.key = 'ana@a.test' AND b.key = 'aa@b.test'"""))
    result = build(db, ("resolve",))
    assert result["resolve"]["result"]["rescored"] is False
    assert cluster(db, "ana@a.test") == {"ana@a.test", "aa@b.test"}


def test_undo_removes_only_scored_merges_and_publishes(db):
    card(db, "acct/people/1", "Dana Fox", emails=["dana@acme.test", "dana.fox@home.test"])
    a = sam_lees(db)[0]
    build(db)
    version = dirty.version(db)
    out = CliRunner().invoke(cli.app, ["resolve", "--undo"])
    assert out.exit_code == 0 and '"removed": 3' in out.output, out.output
    assert scored_links(db) == 0 and cluster(db, a) == {a}
    assert cluster(db, "dana@acme.test") == {"dana@acme.test", "dana.fox@home.test"}  # a contact card's link
    assert dirty.version(db) == version + 1
    help_text = CliRunner().invoke(cli.app, ["resolve", "--help"], env={"COLUMNS": "200"}).output
    assert "extractor 'er'" in help_text


def test_a_clusters_name_leaves_with_the_merge(db):
    """Chris's address heads the cluster (most mail) and shows the contact card's name; rejected, it is Chris again."""
    names = {"chris.lee@a.test": "Chris Lee", "christine.lee@a.test": "Christine Lee"}
    for i in range(4):
        mail(db, f"c{i}", "chris.lee@a.test", ["boss@a.test"], f"S{i}", day(i), names=names)
    mail(db, "c9", "christine.lee@a.test", ["boss@a.test"], "S9", day(9), names=names)
    card(db, "card", "Christine Lee", emails=["christine.lee@a.test"], source="maccontacts")
    build(db)
    head = db.execute(text("SELECT id, name, props FROM nodes WHERE key = 'chris.lee@a.test'")).one()
    assert cluster(db, "chris.lee@a.test") == set(names)
    assert head.name == "Christine Lee" and head.props["own_name"] == "Chris Lee"
    link = db.execute(text("SELECT id FROM edges WHERE type = 'SAME_AS' AND extractor = 'er'")).scalar_one()
    corrections.reject(db, link)
    head = db.execute(text("SELECT name, props FROM nodes WHERE key = 'chris.lee@a.test'")).one()
    assert head.name == "Chris Lee" and "own_name" not in head.props and "shown_name" not in head.props


# --- the same graph resolves the same way ----------------------------------------------------------------------------

def outcome(s) -> dict:
    """What resolution decided, by natural keys: each scored link with its score and features, each cluster's head,
    and the name each person shows."""
    links = {(*sorted((a, b)), conf, json.dumps(props, sort_keys=True)) for a, b, conf, props in s.execute(text("""
        SELECT x.key, y.key, e.confidence, e.props FROM edges e JOIN nodes x ON x.id = e.src
        JOIN nodes y ON y.id = e.dst WHERE e.type = 'SAME_AS' AND e.extractor = 'er'"""))}
    heads = dict(s.execute(text("""SELECT n.key, h.key FROM canonical c JOIN nodes n ON n.id = c.node_id
                                   JOIN nodes h ON h.id = c.canonical_id""")).all())
    names = set(s.execute(text("SELECT key, name, props->>'own_name' FROM nodes WHERE type = 'Person'")).all())
    return {"links": links, "heads": heads, "names": names}


def jordan_and_sam(s, reverse: bool) -> None:
    """Jordan Lee writes from two addresses to the same friends, has a phone on his card and is mentioned by name;
    Sam Lee's two addresses (one card) send as much mail each. Stored in the order given, or reversed."""
    jordan = {"jordan.lee@school.test": "Jordan Lee", "jlee@home.test": "Jordan Lee"}
    writes = [lambda i=i, a=a, k=k: mail(s, f"j{i}{k}", a, ["ana@q.test", "bo@r.test"], f"Weekend {k}",
                                         day(3 * i + k), names=jordan)
              for i, a in enumerate(jordan) for k in range(2)]
    writes += [lambda: card(s, "card1", "Jordan Lee", emails=["jordan.lee@school.test"], phones=["+16145551234"]),
               lambda: card(s, "card2", "Sam Lee", emails=["sam@x.test", "samlee@y.test"]),
               lambda: mail(s, "s1", "sam@x.test", ["bo@r.test"], "Lunch", day(7)),
               lambda: mail(s, "s2", "samlee@y.test", ["bo@r.test"], "Lunch?", day(8))]

    def mention():  # a name read from text, keyed as graph/extract.py keys it
        s.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Person', 'name:jordan-lee', 'Jordan Lee')"))

    for write in [mention, *writes] if not reverse else reversed(writes):
        write()
    s.commit()
    pipeline.run(s, ["spine"])
    if reverse:
        mention()
    build(s, ("self", "resolve"))


def test_resolution_reads_the_graph_not_the_order_its_rows_were_stored_in(db):
    from igneous.graph import resolve

    jordan_and_sam(db, reverse=False)
    first = outcome(db)
    pairs = [(a, b) for a, b, _score, _features in resolve.candidates(db)]
    assert pairs and all(resolve.score(a, b) == resolve.score(b, a) for a, b in pairs)
    db.execute(text("TRUNCATE source_items, nodes, edges, canonical, pipeline_changes CASCADE"))
    jordan_and_sam(db, reverse=True)
    assert outcome(db) == first
    assert cluster(db, "name:jordan-lee") == {"name:jordan-lee", "+16145551234", "jordan.lee@school.test",
                                              "jlee@home.test"}
    assert first["heads"]["samlee@y.test"] == "sam@x.test"  # as much mail each: the first by key heads it


def test_resolution_is_the_same_whatever_order_it_reads_people_in(db, monkeypatch):
    """Resolution reads people in key order, but nothing it decides depends on that order: handed the same people
    shuffled, it scores the same pairs the same way and forms the same clusters. Rick and Richard Lee write to the same
    friends (nicknames, either way round); Jordan Lee's phone and a mention of his name could each be the one that
    joins him."""
    import random

    from igneous.graph import resolve

    jordan_and_sam(db, reverse=False)
    rick = {"richard.lee@a.test": "Richard Lee", "rick.lee@b.test": "Rick Lee"}
    for i, address in enumerate(rick):
        for k, friend in enumerate(("pat@p.test", "quinn@q.test", "ana@q.test")):
            mail(db, f"r{i}{k}", address, [friend], f"Notes {k}", day(20 + 4 * i + k), names=rick)
    build(db)
    people = resolve.load(db)

    def resolved(order) -> tuple:
        monkeypatch.setattr(resolve, "load", lambda s: list(order))
        scores = {(frozenset((a.key, b.key)), score, json.dumps(features, sort_keys=True))
                  for a, b, score, features in resolve.candidates(db)}
        resolve.run(db, rescore=True)
        return scores, outcome(db)

    first = resolved(people)
    assert cluster(db, "rick.lee@b.test") == set(rick)
    assert cluster(db, "+16145551234") >= {"name:jordan-lee", "jordan.lee@school.test"}
    shuffle = random.Random(203).shuffle
    for _ in range(10):
        order = list(people)
        shuffle(order)
        assert resolved(order) == first


def test_scoring_again_reads_no_earlier_scorings_links(db):
    """Cara writes from two addresses to the same two friends, so scoring merges them. Xavi writes to one of Cara's
    addresses from each of his: only Cara's merge makes him share a correspondent with himself, and scoring must not
    read it, or each rescoring would build on the last."""
    from igneous.graph import resolve

    cara = {"cara.cole@a.test": "Cara Cole", "ccole@b.test": "Cara Cole"}
    xavi = {"xavi.diaz@c.test": "Xavi Diaz", "xdiaz@d.test": "Xavi Diaz"}
    for i, (c, x) in enumerate(zip(cara, xavi, strict=True)):
        for k, friend in enumerate(("pat@p.test", "quinn@q.test")):
            mail(db, f"c{i}{k}", c, [friend], f"Notes {k}", day(4 * i + k), names=cara)
            mail(db, f"f{i}{k}", friend, [c], f"Re: Notes {k}", day(4 * i + k + 0.5), names=cara)
        mail(db, f"x{i}", x, [c], "Hello", day(4 * i + 2), names={**cara, **xavi})
    build(db)
    assert cluster(db, "cara.cole@a.test") == set(cara)
    first = outcome(db)
    resolve.run(db, rescore=True)
    assert outcome(db) == first
    assert cluster(db, "xavi.diaz@c.test") == {"xavi.diaz@c.test"}


def services(s) -> set[str]:
    return set(s.execute(text("SELECT key FROM nodes WHERE type = 'Person' AND props->>'kind' = 'service'")).scalars())


def test_a_bulk_sender_is_one_whatever_an_earlier_scoring_merged(db):
    """Alice writes from two addresses to the same friends, so scoring merges them, and you write to one of them.
    When her other address turns out to send mostly newsletters, it is a one-way bulk sender, as it is in a graph
    built at once: the self stage folds identities by trusted links, never by an earlier scoring."""
    alice = {"alice@a.test": "Alice Ames", "alice@b.test": "Alice Ames"}

    def first(s):
        for i, a in enumerate(alice):
            for k, friend in enumerate(("pat@p.test", "quinn@q.test")):
                mail(s, f"a{i}{k}", a, [friend], f"Notes {k}", day(4 * i + k), names=alice)
                mail(s, f"f{i}{k}", friend, [a], f"Re: Notes {k}", day(4 * i + k + 0.5), names=alice)
        mail(s, "me1", ME, ["alice@a.test"], "Hi Alice", day(9), sent=True)

    def later(s):
        for k in range(8):
            mail(s, f"news{k}", "alice@b.test", [ME], f"Digest {k}", day(10 + k), names=alice,
                 body="This week's digest. Unsubscribe at any time.")

    first(db)
    build(db)
    assert cluster(db, "alice@a.test") == set(alice)
    later(db)
    build(db)
    stepwise = outcome(db), services(db)
    db.execute(text("TRUNCATE source_items, nodes, edges, canonical, pipeline_changes CASCADE"))
    first(db)
    later(db)
    build(db)
    assert (outcome(db), services(db)) == stepwise
    assert "alice@b.test" in services(db) and cluster(db, "alice@a.test") == {"alice@a.test"}
