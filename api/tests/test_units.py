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


def test_locate_spans():
    from igneous.graph.extract import locate

    body = "Please ask Jeff\n  Skilling and FERC about the caps."
    assert locate(body, "FERC") == (body.index("FERC"), body.index("FERC") + 4)
    s0, s1 = locate(body, "Jeff Skilling")  # survives the line wrap
    assert body[s0:s1].split() == ["Jeff", "Skilling"]
    assert locate(body, "Ken Lay") is None and locate(body, "  ") is None


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


def test_er_rejects_mailing_lists():
    from igneous.graph.resolve import P, split_name, unresolvable

    for key, name in [("aei-bi_updates@www.brookings.org", "Aei-bi Updates"),
                      ("western.price.survey.contacts@proxy4.ba.best.com", "Western Price Survey Contacts")]:
        f, last = split_name(name, key)
        assert unresolvable(P(1, key, name, f, last, set()))


def test_imessage_decoding_and_dates():
    from igneous.connectors.imessage import apple_time, decode_attributed

    short = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject" \
            b"\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+\x05hello\x86"
    assert decode_attributed(short) == "hello"
    long_text = "x" * 300
    long_blob = b"NSString\x01\x94\x84\x01+\x81" + (300).to_bytes(2, "little") + long_text.encode()
    assert decode_attributed(long_blob) == long_text
    assert decode_attributed(None) is None and decode_attributed(b"garbage") is None
    assert apple_time(0) is None
    assert apple_time(700_000_000 * 10**9).year == 2023   # nanoseconds (current format)
    assert apple_time(700_000_000).year == 2023           # seconds (pre-10.13 rows)


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


def test_gmail_backfill_resumes_mid_page(monkeypatch):
    """Regression: a mid-page checkpoint saved only the page token, so resuming re-fetched the page."""
    from igneous.connectors import gmail
    from igneous.connectors.base import Checkpoint

    monkeypatch.setattr(gmail, "CHECKPOINT_EVERY", 2)
    monkeypatch.setattr(gmail, "_retry", lambda call, units=5, tries=6: call)
    fetched = []

    class Conn(gmail.GmailConnector):
        def __init__(self):
            self.query, self.since_days, self.account = "", None, "x"
            pages = {None: {"messages": [{"id": f"m{i}"} for i in range(5)], "nextPageToken": "p2"},
                     "p2": {"messages": [{"id": "m5"}]}}

            class Svc:
                def users(_):
                    class U:
                        def getProfile(_, userId):
                            return {"historyId": "h1"}

                        def messages(_):
                            class M:
                                def list(_, pageToken=None, **k):
                                    return pages[pageToken]
                            return M()
                    return U()
            self.svc = Svc()

        def _fetch(self, msg_id):
            fetched.append(msg_id)
            return None

    out = list(Conn()._backfill())
    mid = [c.cursor for c in out if isinstance(c, Checkpoint) and "backfill_offset" in c.cursor]
    assert mid[1] == {"history_id": "h1", "backfill_page": None, "backfill_offset": 4, "backfill_q": ""}
    fetched.clear()
    list(Conn()._backfill(mid[1]))
    assert fetched == ["m4", "m5"]  # resumes after m3, not from m0


def test_phone_normalization():
    from igneous.connectors.mac_contacts import norm_phone

    assert norm_phone("(614) 555-1234") == norm_phone("+1 614 555 1234") == norm_phone("6145551234") == "+16145551234"
    assert norm_phone("+44 20 7946 0958") == "+442079460958"
    assert norm_phone("555-1234") is None and norm_phone(None) is None


def test_contact_names_in_messages():
    from igneous.graph.traversal import name_handles, pretty_thread

    named = {"+16174075277": "Sam Lee", "6174075277": "Sam Lee", "+16149733915": "Ana Ruiz", "6149733915": "Ana Ruiz"}
    assert name_handles("[14:39] +16174075277: home rn", named) == "[14:39] Sam Lee: home rn"
    assert name_handles("[1:00] +19995550000: hi", named) == "[1:00] +19995550000: hi"  # unknown stays
    assert pretty_thread("+16149733915, +16174075277", named) == "Ana, Sam"
    assert pretty_thread("+16149733915, +16174075277, +19995550000, +18885550000", named).endswith("and 1 other")
    assert pretty_thread("Biggers in Paris", named) == "Biggers in Paris"
