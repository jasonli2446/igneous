"""Who the owner is: identity signals only, no owner without one, and service marks recomputed every run."""
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from igneous.graph import identity, resolve, self_identity, spine
from igneous.graph.find import find
from igneous.graph.self_identity import looks_like_person_name

T0 = datetime(2001, 5, 14, 16, tzinfo=UTC)


def filed(s, path, frm, to, title="Forecast", body="see attached", cc=(), x_from=None, names=None,
          n=[0]):  # noqa: B006 - a counter across calls
    """An Enron-style record: `path` is <mailbox>/<folder>/<file>, as the maildir connector names it. Enron's address
    headers carry no names: X-From (`x_from`) names the sender, and `names` gives an address a display name."""
    n[0] += 1
    names = names or {}
    meta = {"from": [{"email": frm, "name": names.get(frm, "")}], "fingerprint": f"fp-{path}",
            "to": [{"email": t, "name": names.get(t, "")} for t in to],
            "cc": [{"email": c, "name": names.get(c, "")} for c in cc], "x_from": x_from}
    s.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, title, body_text, raw_meta, occurred_at)
        VALUES ('email', :e, :e, :t, :b, CAST(:m AS jsonb), :w)
    """), {"e": path, "t": title, "b": body, "w": T0 + timedelta(hours=n[0]), "m": json.dumps(meta)})


def owner(s):
    me = s.execute(text("SELECT id FROM nodes WHERE type = 'Person' AND key = 'me'")).scalar()
    linked = s.execute(text("""
        SELECT n.key, e.props->'signals' FROM edges e JOIN nodes n ON n.id IN (e.src, e.dst) AND n.key <> 'me'
        WHERE e.type = 'SAME_AS' AND e.extractor = 'self'""")).all()
    return me, {k: v for k, v in linked}


def build(s):
    spine.run(s)
    return self_identity.run(s)


def phil_mailbox(s):
    for i in range(3):
        filed(s, f"allen-p/sent/{i}", "phillip.allen@x.test", ["tim@x.test"])
    filed(s, "allen-p/_sent_mail/9", "ina.rangel@x.test", ["tim@x.test"])   # his assistant, sending for him
    filed(s, "allen-p/inbox/1", "tim@x.test", ["phillip.allen@x.test"])


def test_a_mail_archive_with_one_owner_names_them(db):
    phil_mailbox(db)
    assert build(db)["self_links"] == 1
    me, linked = owner(db)
    assert me is not None and linked == {"phillip.allen@x.test": ["archive_sent"]}


def test_several_peoples_mailboxes_have_no_owner(db):
    phil_mailbox(db)
    build(db)
    filed(db, "arnold-j/sent_items/1", "john.arnold@x.test", ["tim@x.test"])
    assert build(db) ["owner"] is None
    assert owner(db) == (None, {})   # the owner the first mailbox gave is gone with the second one


def test_a_mailbox_with_no_sent_folder_leaves_the_archive_ownerless(db):
    phil_mailbox(db)
    filed(db, "kay-k/inbox/1", "tim@x.test", ["kay@x.test"])   # whose mailbox this is, nothing says
    assert build(db)["owner"] is None


def allen_mailbox(s):
    """Phillip Allen's mailbox, named as Enron's headers name it: he sends from two addresses under his name, and his
    assistant, his sister and an address named only "Phillip" send from his sent folders too."""
    for i in range(6):
        filed(s, f"allen-p/sent/{i}", "phillip.allen@x.test", ["tim@x.test"], x_from="Phillip K Allen")
    for i in range(2):
        filed(s, f"allen-p/sent_items/{i}", "k..allen@x.test", ["tim@x.test"],
              x_from="Allen, Phillip K. </O=ENRON/OU=NA/CN=RECIPIENTS/CN=PALLEN>")
    filed(s, "allen-p/_sent_mail/1", "ina.rangel@x.test", ["tim@x.test"], x_from="Rangel, Ina")
    filed(s, "allen-p/_sent_mail/2", "diana.allen@x.test", ["tim@x.test"], x_from="Allen, Diana")
    filed(s, "allen-p/_sent_mail/3", "pa@x.test", ["tim@x.test"], x_from="Phillip")


def test_the_owners_other_address_in_their_sent_folders_is_them_and_no_one_else_there(db):
    """k..allen@ sends under his name; an assistant and a sister have names of their own, and a first name alone says
    nothing."""
    allen_mailbox(db)
    assert build(db)["self_links"] == 2
    assert owner(db)[1] == {"phillip.allen@x.test": ["archive_sent"], "k..allen@x.test": ["archive_alias"]}


def test_the_addresses_the_owners_mail_came_to_are_them_and_no_list_colleague_or_relative(db):
    allen_mailbox(db)
    names = {"phillip.sr@allen.test": "Phillip Allen", "p.allen@x.test": "Pam Allen"}
    for path, sender, to, cc in [
        ("inbox/1", "tim@x.test", ["pallen@x.test"], ()),             # his: nothing names it but his initial and name
        # his name, on mail that came to none of his known addresses:
        ("inbox/2", "tim@x.test", ["phillip.k.allen@x.test", "mike@x.test"], ()),
        ("inbox/3", "desk@x.test", ["traders@x.test"], ()),           # a list he is on
        ("inbox/4", "tim@x.test", ["k..allen@x.test"], ["mike.grigsby@x.test", "ina.rangel@x.test"]),  # colleagues
        ("inbox/5", "tim@x.test", ["ina.rangel@x.test"], ()),         # his assistant's mail, filed with his
        ("inbox/6", "tim@x.test", ["diana.allen@x.test"], ()),        # his sister
        ("inbox/7", "tim@x.test", ["p.allen@x.test"], ()),            # P. Allen's address, a header names Pam
        ("inbox/8", "tim@x.test", ["dallen@x.test"], ()),             # D. Allen's
        ("inbox/9", "mom@allen.test", ["phillip.allen@x.test"], ["phillip.sr@allen.test"]),  # his father and namesake
        ("all_documents/1", "phillip.allen@x.test", ["phillip.sr@allen.test"], ()),  # a copy of his own mail to him
        ("deleted_items/1", "shop@store.test", ["pallen@other.test"], ()),  # pallen@ at another domain
        ("deleted_items/2", "shop@store.test", ["pallen@ees.x.test"], ()),  # ...or another mail system of his
    ]:
        filed(db, f"allen-p/{path}", sender, to, cc=cc, names=names)
    build(db)
    assert owner(db)[1] == {"phillip.allen@x.test": ["archive_sent"], "k..allen@x.test": ["archive_alias"],
                            "pallen@x.test": ["archive_received"], "phillip.k.allen@x.test": ["archive_received"]}
    resolve.run(db)
    me = identity.self_id(db)
    yours = set(db.execute(text("SELECT key FROM nodes WHERE id = ANY(:m)"), {"m": identity.members(db, me)}).scalars())
    assert yours == {"me", "phillip.allen@x.test", "k..allen@x.test", "pallen@x.test", "phillip.k.allen@x.test"}
    found = find(db, "allen", types=["Person"])   # Jump to lists you once, and your father as someone else
    assert [r["context"] for r in found if r["id"] == me] == ["You"]
    assert sorted(r["name"] for r in found if r["name"].startswith("Phillip")) == ["Phillip Allen", "Phillip K Allen"]


def test_an_owner_without_a_surname_has_no_other_addresses(db):
    """Nothing but a first name to go on: no other address is theirs by name."""
    for i in range(3):
        filed(db, f"pk/sent/{i}", "pk@x.test", ["tim@x.test"], x_from="Phillip")
    filed(db, "pk/sent/9", "phillip@x.test", ["tim@x.test"], x_from="Phillip")
    filed(db, "pk/inbox/1", "tim@x.test", ["phillip.k@x.test"])
    build(db)
    assert owner(db)[1] == {"pk@x.test": ["archive_sent"]}


def test_no_signal_means_no_owner_node(db):
    filed(db, "allen-p/inbox/1", "tim@x.test", ["phillip.allen@x.test"])
    assert build(db) == {"owner": None, "self_links": 0, "signals": 0, "services": 0, "services_cleared": 0}
    assert owner(db) == (None, {})


def test_an_owner_that_a_source_or_a_fact_you_stated_names_stays_without_signals(db):
    db.execute(text("""
        INSERT INTO source_items (source, external_id, content_hash, title, raw_meta, occurred_at)
        VALUES ('imessage', 'c1/2001-05-14', 'h', 'Pat', CAST(:m AS jsonb), :w)
    """), {"m": json.dumps({"chat": "c1", "participants": ["+16145550100"], "senders": ["me"]}), "w": T0})
    me = build(db)["owner"]
    assert me is not None and owner(db)[0] == me   # Messages are yours: its records name you
    db.execute(text("DELETE FROM source_items"))
    spine.run(db)
    org = db.execute(text("INSERT INTO nodes (type, key, name) VALUES ('Organization', 'org:acme', 'Acme') "
                          "RETURNING id")).scalar_one()
    db.execute(text("INSERT INTO edges (src, dst, type, extractor) VALUES (:me, :o, 'WORKED_AT', 'user')"),
               {"me": me, "o": org})
    assert self_identity.run(db)["owner"] == me      # you said where you work
    db.execute(text("DELETE FROM edges WHERE extractor = 'user'"))
    assert self_identity.run(db)["owner"] is None    # nothing names you any more


def services(s):
    return dict(s.execute(text("SELECT key, props->>'service_reason' FROM nodes WHERE props->>'kind' = 'service'")).all())


def test_service_marks_are_set_and_cleared_every_run(db):
    db.execute(text("INSERT INTO source_items (source, external_id, content_hash, title, raw_meta) "
                    "VALUES ('gcontacts', 'a/people/1', 'card', 'Pat Lee', '{\"emails\": [\"pat@x.test\"]}')"))
    filed(db, "me-m/sent/1", "me@x.test", ["pat@x.test"])                  # this archive is yours
    for i in range(2):
        filed(db, f"me-m/inbox/deal{i}", "deals@shop.test", ["me@x.test"], "Sale", "unsubscribe")
    for i, body in enumerate(["unsubscribe", "unsubscribe", "lunch?"]):    # mostly bulk, but a person
        filed(db, f"me-m/inbox/jose{i}", "jose@corp.test", ["me@x.test"], "Hi", body)
    filed(db, "me-m/inbox/n1", "noreply@corp.test", ["me@x.test"], "Receipt")
    db.execute(text("""UPDATE source_items SET raw_meta = raw_meta || '{"from": [{"email": "jose@corp.test",
                       "name": "José García"}]}' WHERE external_id LIKE 'me-m/inbox/jose%'"""))
    build(db)
    assert services(db) == {"deals@shop.test": "one-way bulk sender", "noreply@corp.test": "automated address"}
    filed(db, "me-m/sent/2", "me@x.test", ["deals@shop.test"], "Re: Sale")  # you wrote back: not one-way any more
    build(db)
    assert services(db) == {"noreply@corp.test": "automated address"}


def test_a_human_name_is_any_capitalized_name():
    for name in ("José García", "Mary Ann Smith", "Jean-Luc Picard", "Siobhan O'Brien", "Siobhan O\u2019Brien",
                 "Mary A. Smith", "Vincent van Gogh", "Maria de la Cruz"):
        assert looks_like_person_name(name), name
    for name in ("Adam from Bolt", "ALERTS TEAM", "Netflix", "Team 42", "Sam Lee (Google Docs)", "john smith", "",
                 None, "van Gogh"):
        assert not looks_like_person_name(name), name
