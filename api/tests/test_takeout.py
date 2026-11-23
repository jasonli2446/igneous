"""Google Takeout importer. Synthetic data only; DB tests run in the throwaway test database."""
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from igneous.connectors import takeout
from igneous.connectors.ai_exports import export_items, parse_export
from igneous.db import SessionLocal

TOKEN = "ghp_" + "Z" * 36
ACTIVITY = {
    "Search": [
        {"header": "Search", "title": "Searched for gloomhaven night rules", "time": "2025-06-01T14:02:00.000Z",
         "titleUrl": "https://www.google.com/search?q=draw+steel+rules", "products": ["Search"]},
        {"header": "Search", "title": f"Searched for token {TOKEN}", "time": "2025-06-01T15:00:00.000Z"},
        {"header": "Search", "title": "Used Google Search", "time": "2025-06-01T16:00:00.000Z", "products": ["Search"]},
        {"header": "Search", "title": "Visited Example Page", "time": "2025-06-01T17:00:00.000Z",
         "titleUrl": "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fa%3Futm_source%3Dnews%26id%3D5%26fbclid%3Dxyz"},
        {"header": "Search", "title": "Viewed ad", "time": "2025-06-01T18:00:00.000Z",
         "details": [{"name": "From Google Ads"}]},
    ],
    "YouTube": [
        {"header": "YouTube", "title": "Watched Gloomhaven Night actual play", "time": "2025-06-02T20:00:00.000Z",
         "subtitles": [{"name": "Some Channel"}]},
    ],
    "Gemini Apps": [
        {"header": "Gemini Apps", "title": "Prompted second question", "time": "2025-05-01T15:00:00.000Z",
         "safeHtmlItem": [{"html": "<p>Answer <b>two</b></p>"}]},
        {"header": "Gemini Apps", "title": "Prompted first question", "time": "2025-05-01T14:00:00.000Z",
         "safeHtmlItem": [{"html": "<p>Answer one</p>"}]},
    ],
}
HISTORY = {"Browser History": [
    {"title": "Gloomhaven Night wiki", "url": "https://wiki.example.com/gloomhaven-night?utm_campaign=a&page=2#top",
     "time_usec": 1748800000000000, "page_transition": "LINK"},
    {"title": "Gloomhaven Night wiki", "url": "https://wiki.example.com/gloomhaven-night?page=2&utm_medium=b",
     "time_usec": 1748800600000000, "page_transition": "LINK"},
    {"title": "Other", "url": "https://other.example.org/", "time_usec": 1748801200000000},
    {"title": "no", "url": "chrome://settings", "time_usec": 1748801200000000},
]}
BOOKMARKS = (b'<!DOCTYPE NETSCAPE-Bookmark-file-1><DL><p><DT><H3>Games</H3><DL><p>'
             b'<DT><A HREF="https://example.com/ds?utm_source=x">Gloomhaven Night</A></DL><p>'
             b'<DT><A HREF="https://example.com/top">Top</A></DL>')
SAVED = {"type": "FeatureCollection", "features": [
    {"geometry": {"coordinates": [-81.6, 41.5], "type": "Point"},
     "properties": {"Title": "Board Game Cafe", "Location": {"Address": "1 Main St", "Business Name": "Board Game Cafe"},
                    "Google Maps URL": "http://maps.google.com/?cid=1", "Published": "2025-01-02T00:00:00Z"}}]}
REVIEWS = {"type": "FeatureCollection", "features": [
    {"geometry": {"coordinates": [-81.7, 41.4], "type": "Point"},
     "properties": {"Title": "Pizza Spot", "Comment": f"Great, key {TOKEN}", "Star Rating": 5,
                    "Published": "2025-02-03T00:00:00Z"}}]}
SEMANTIC = {"timelineObjects": [
    {"placeVisit": {"location": {"placeId": "ChIJabc", "name": "Board Game Cafe", "address": "1 Main St",
                                 "latitudeE7": 415000000, "longitudeE7": -816000000},
                    "duration": {"startTimestamp": "2025-06-03T18:00:00.000Z", "endTimestamp": "2025-06-03T20:30:00.000Z"}}},
    {"activitySegment": {"distance": 5}},
    {"placeVisit": {"location": {"placeId": "ChIJabc", "name": "Board Game Cafe", "latitudeE7": 415000000,
                                 "longitudeE7": -816000000},
                    "duration": {"startTimestamp": "2025-06-10T18:00:00.000Z", "endTimestamp": "2025-06-10T19:00:00.000Z"}}},
]}
TIMELINE = {"semanticSegments": [
    {"startTime": "2025-07-01T10:00:00Z", "endTime": "2025-07-01T11:00:00Z",
     "visit": {"topCandidate": {"placeId": "ChIJnoname", "semanticType": "HOME"}}}]}


def build_zip(path: Path, files: dict[str, bytes | dict]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr("Takeout/" + name, content if isinstance(content, bytes) else json.dumps(content))
    return path


CHAT_MSGS = {"messages": [
    {"creator": {"name": "Sam Lee", "email": "sam@example.com", "user_type": "Human"},
     "created_date": "Tuesday, May 6, 2025 at 3:04:05\u202fPM UTC", "text": f"see you at gloomhaven night {TOKEN}",
     "topic_id": "t", "message_id": "m1"},
    {"creator": {"name": "Me", "email": "me@example.com", "user_type": "Human"},
     "created_date": "Tuesday, May 6, 2025 at 3:09:00\u202fPM UTC", "text": "ok", "topic_id": "t", "message_id": "m2"}]}
CHAT_GROUP = {"members": [{"name": "Sam Lee", "email": "sam@example.com", "user_type": "Human"},
                          {"name": "Me", "email": "me@example.com", "user_type": "Human"}]}
TASKS = {"kind": "tasks#taskLists", "items": [{"id": "L1", "title": "Todo", "updated": "2025-05-01T00:00:00.000000Z",
                                                "items": [{"title": "Buy dice", "status": "completed",
                                                           "completed": "2025-05-02T00:00:00Z", "notes": "d10s"}]}]}
MEET_CSV = (b"Owner Gaia Id,Item Id,Conference Id,Item Type,Meeting Code,Event Id,Thread Id,Inbox Id,Call Direction,"
            b"Start Time,End Time,Duration,Direct Call Result,Participation State,Call Counterparts,Meeting Media Type\n"
            b"1,i1,conf1,meet:meeting,abc-defg-hij,EVT123,,,,2025-06-04 18:00:00 UTC,2025-06-04 18:30:00 UTC,0:30:00,,,,\n")


def full_files(html_activity=False):
    files = {f"My Activity/{p}/MyActivity.json": rows for p, rows in ACTIVITY.items()}
    files.update({"My Activity/Ads/MyActivity.json": [{"header": "Ads", "title": "Viewed an ad", "time": "2025-06-01T10:00:00Z"}],
                  "My Activity/Chrome/MyActivity.json": [{"header": "Chrome", "title": "Visited Dup", "time": "2025-06-01T10:00:00Z",
                                                          "titleUrl": "https://dup.example.com"}],
                  "Google Chat/Groups/DM abc/messages.json": CHAT_MSGS,
                  "Google Chat/Groups/DM abc/group_info.json": CHAT_GROUP,
                  "Tasks/Tasks.json": TASKS, "Chrome/Addresses and more.json": {"secret": "do not import"},
                  "Chrome/Reading List.html": b'<DL><DT><A HREF="https://read.example.com/x?utm_source=q">Read me</A></DL>',
                  "Google Meet/ConferenceHistory/conference_history_records.csv": MEET_CSV})
    files.update({"Chrome/History.json": HISTORY, "Chrome/Bookmarks.html": BOOKMARKS,
                  "Maps (your places)/Saved Places.json": SAVED, "Maps (your places)/Reviews.json": REVIEWS,
                  "Location History (Timeline)/Semantic Location History/2025/2025_JUNE.json": SEMANTIC,
                  "Location History (Timeline)/Records.json": b"[]", "Timeline.json": TIMELINE})
    if html_activity:
        files["My Activity/Maps/MyActivity.html"] = b"<html></html>"
    return files


@pytest.fixture
def zpath(tmp_path):
    return build_zip(tmp_path / "takeout-20250801T000000Z-001.zip", full_files())


def by_id(items):
    return {i.external_id: i for i in items}


def test_parse_items_cover_all_parts(zpath):
    items, report = takeout.parse([zpath])
    ids = by_id(items)
    assert {"activity/search/2025-06-01", "activity/youtube/2025-06-02", "chrome/history/2025-06-01",
            "chrome/bookmarks", "maps/visits/2025-06-03", "maps/visits/2025-06-10", "maps/visits/2025-07-01",
            "maps/saved/saved-places", "maps/reviews/reviews", "chrome/reading-list", "tasks/l1",
            "meet/conf1", "chat/dm-abc/2025-05-06"} <= set(ids)
    assert report["parts"]["gemini"] == 1 and report["places"] == 4
    assert not any("ads" in i for i in ids) and not any("activity/chrome" in i for i in ids)
    assert "do not import" not in "".join(i.body_text for i in items)
    assert "Buy dice [completed, done 2025-05-02]" in ids["tasks/l1"].body_text and "d10s" in ids["tasks/l1"].body_text
    chat = ids["chat/dm-abc/2025-05-06"]
    assert chat.occurred_at.hour == 15 and "Sam Lee: see you at gloomhaven night [REDACTED" in chat.body_text
    assert chat.raw_meta["participants"] == ["me@example.com", "sam@example.com"]
    assert ids["meet/conf1"].raw_meta["event_id"] == "EVT123" and "Meeting abc-defg-hij (0:30:00)" in ids["meet/conf1"].body_text
    assert "read.example.com/x)" in ids["chrome/reading-list"].body_text
    assert any("Records.json" in n for n in report["notes"])
    search = ids["activity/search/2025-06-01"]
    assert "[14:02] Searched for gloomhaven night rules" in search.body_text
    assert "Used Google Search" not in search.body_text and "Viewed ad" not in search.body_text
    assert "Watched Gloomhaven Night actual play" in ids["activity/youtube/2025-06-02"].body_text
    assert search.source == "takeout_activity" and ids["maps/visits/2025-06-03"].source == "takeout_maps"


def test_redaction_and_url_cleaning(zpath):
    items, report = takeout.parse([zpath])
    blob = "\n".join(i.body_text for i in items) + json.dumps([i.raw_meta for i in items], default=str)
    assert TOKEN not in blob and "[REDACTED:github_token]" in blob and report["redactions"] >= 2
    assert "utm_" not in blob and "fbclid" not in blob and "#top" not in blob
    ids = by_id(items)
    assert "Visited Example Page (example.com)" in ids["activity/search/2025-06-01"].body_text
    chrome = ids["chrome/history/2025-06-01"].body_text
    assert "Gloomhaven Night wiki (wiki.example.com) x2" in chrome  # repeats collapsed
    assert "chrome://" not in chrome and "settings" not in chrome
    assert "https://example.com/ds)" in ids["chrome/bookmarks"].body_text
    assert takeout.clean_url("https://a.com/p?utm_source=x&q=1&gclid=2#f") == "https://a.com/p?q=1"


def test_gemini_ids_match_the_ai_export_path(zpath):
    items, _ = takeout.parse([zpath])
    gem = [i for i in items if i.source == "ai_chats"]
    raw = json.dumps(ACTIVITY["Gemini Apps"]).encode()
    kind, convs = parse_export(io.BytesIO(raw), "gemini")
    direct = export_items(kind, convs)
    assert [(i.source, i.external_id, i.content_hash) for i in gem] == [(i.source, i.external_id, i.content_hash)
                                                                       for i in direct]
    assert gem[0].external_id == "gemini/gemini-2025-05-01/2025-05-01"


def test_places_keys_and_timeline_without_names(zpath):
    items, _ = takeout.parse([zpath])
    v = by_id(items)["maps/visits/2025-06-03"]
    assert v.raw_meta["visits"][0]["key"] == "place:ChIJabc"
    assert "Board Game Cafe, 1 Main St (2h30m)" in v.body_text
    home = by_id(items)["maps/visits/2025-07-01"].raw_meta["visits"][0]
    assert home["key"] == "place:ChIJnoname" and home["name"] == "Home"
    saved = by_id(items)["maps/saved/saved-places"].raw_meta["places"][0]
    assert saved["key"] == "place:board game cafe@41.500,-81.600"
    assert takeout.place_key(None, "  Board  Game Cafe", 41.5004, -81.6004) == saved["key"]


def test_a_days_visits_count_the_secrets_redacted_from_them(tmp_path):
    visit = {"placeVisit": {"location": {"placeId": "ChIJkey", "name": f"Locker {TOKEN}", "address": "2 Side St"},
                            "duration": {"startTimestamp": "2025-06-04T09:00:00.000Z",
                                         "endTimestamp": "2025-06-04T10:00:00.000Z"}}}
    # The same visit twice (a history exported in both formats is one visit): counted once.
    z = build_zip(tmp_path / "t.zip", {
        "Location History (Timeline)/Semantic Location History/2025/2025_JUNE.json": {"timelineObjects": [visit, visit]}})
    items, report = takeout.parse([z])
    day = by_id(items)["maps/visits/2025-06-04"]
    assert TOKEN not in day.body_text and "[REDACTED:github_token]" in day.body_text
    assert day.raw_meta["redactions"] == 1 and report["redactions"] >= 1


def test_html_activity_is_reported_not_parsed(tmp_path):
    z = build_zip(tmp_path / "t.zip", full_files(html_activity=True))
    _, report = takeout.parse([z])
    assert any("HTML" in n for n in report["notes"])
    only_html = build_zip(tmp_path / "h.zip", {"My Activity/Search/MyActivity.html": b"<html></html>"})
    with pytest.raises(takeout.TakeoutError, match="JSON"):
        takeout.preflight([only_html])
    with pytest.raises(takeout.TakeoutError):
        takeout.preflight([build_zip(tmp_path / "x.zip", {"Drive/a.txt": b"hi"})])


def test_split_zips_and_extracted_folder_agree(zpath, tmp_path):
    full = full_files()
    a = build_zip(tmp_path / "a.zip", {k: v for k, v in full.items() if k.startswith("My Activity")})
    b = build_zip(tmp_path / "b.zip", {k: v for k, v in full.items() if not k.startswith("My Activity")})
    folder = tmp_path / "extracted"
    with zipfile.ZipFile(zpath) as zf:
        zf.extractall(folder)

    def snap(paths):
        return sorted((i.external_id, i.content_hash) for i in takeout.parse(paths)[0])

    assert snap([a, b]) == snap([zpath]) == snap([folder / "Takeout"]) == snap([folder])


def test_folder_of_uploaded_zips(zpath, tmp_path):
    up = tmp_path / "uploads"
    up.mkdir()
    (up / "0-takeout-x.zip").write_bytes(zpath.read_bytes())
    assert sorted(i.external_id for i in takeout.parse([up])[0]) == sorted(i.external_id for i in takeout.parse([zpath])[0])


def test_corrupt_member_is_reported_and_rest_imported(tmp_path):
    files = full_files()
    files["Chrome/History.json"] = b'{"Browser History": [{"url": "https://a.com", "time_usec": 17488'
    items, report = takeout.parse([build_zip(tmp_path / "t.zip", files)])
    assert any("History.json" in n for n in report["notes"]) and any(i.source == "takeout_activity" for i in items)


def test_scan_downloads(tmp_path):
    (tmp_path / "takeout-1-001.zip").write_bytes(b"x")
    (tmp_path / "other.zip").write_bytes(b"x")
    assert [p.name for p in takeout.downloads_zips(tmp_path)] == ["takeout-1-001.zip"]


@pytest.fixture
def db(committed_db):
    """These tests commit through their own sessions (as an import does): start from an empty graph."""
    yield committed_db


def test_import_is_idempotent_and_builds_graph(db, zpath):
    with SessionLocal() as s:
        first = takeout.run_import(s, [zpath])
        assert first["inserted"] >= 9 and first["unchanged"] == 0
    with SessionLocal() as s:
        second = takeout.run_import(s, [zpath])
        assert second["inserted"] == 0 and second["updated"] == 0 and second["unchanged"] == first["inserted"]
        n = s.execute(text("SELECT count(*) FROM source_items WHERE source LIKE 'takeout\\_%'")).scalar()
        assert n == first["inserted"] - 1  # minus the gemini ai_chats day

    from igneous.graph import spine
    from igneous.rag import chunking

    with SessionLocal() as s:
        spine.run(s)
        chunking.run(s)
        place = s.execute(text("SELECT id FROM nodes WHERE type = 'Place' AND key = 'place:ChIJabc'")).scalar_one()
        e = s.execute(text("""SELECT count(*), min(valid_from)::date, max(valid_to)::date FROM edges
                              WHERE dst = :p AND type = 'VISITED'"""), {"p": place}).one()
        assert e[0] == 1 and str(e[1]) == "2025-06-03" and str(e[2]) == "2025-06-10"
        assert s.execute(text("SELECT count(*) FROM edges WHERE type = 'SAVED'")).scalar() >= 2
        # activity is chunked with a node, so entities.py phrase matching can see "gloomhaven night"
        hit = s.execute(text("""SELECT count(*) FROM chunks c JOIN source_items si ON si.id = c.source_item_id
                                WHERE si.source LIKE 'takeout\\_%' AND c.node_id IS NOT NULL
                                  AND c.tsv @@ phraseto_tsquery('english', 'gloomhaven night')""")).scalar()
        assert hit >= 3
        from igneous.graph.invariants import check_graph
        assert [v for v in check_graph(s) if v.details and v.details.get("edge_type") in ("VISITED", "SAVED")] == []


def test_chat_people_and_meet_event_link(db, zpath):
    from igneous.connectors.base import SourceItem, run_sync
    from igneous.graph import spine

    class Cal:
        name = "takeouttest"

        def sync(self, cursor):
            yield SourceItem(source="gcal", external_id="acct/primary/EVT123", title="Standup", body_text="",
                             raw_meta={"series_key": "acct/primary/EVT123", "attendees": [],
                                       "event_key": "gcal:takeouttest-uid"},
                             occurred_at=datetime(2025, 6, 4, tzinfo=UTC))

    try:
        with SessionLocal() as s:
            run_sync(s, Cal(), account="t")
            takeout.run_import(s, [zpath])
            spine.run(s)
            people = s.execute(text("SELECT key FROM nodes WHERE type = 'Person' AND key LIKE '%@example.com' "
                                    "ORDER BY key")).scalars().all()
            assert people == ["me@example.com", "sam@example.com"]
            link = s.execute(text("""
                SELECT count(*) FROM edges e JOIN nodes ev ON ev.id = e.dst AND ev.key = 'gcal:takeouttest-uid'
                JOIN nodes f ON f.id = e.src AND f.key = 'takeout_meet:meet/conf1' WHERE e.type = 'RELATED_TO'
            """)).scalar()
            assert link == 1
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM source_items WHERE source = 'gcal' AND external_id = 'acct/primary/EVT123'"))
            s.execute(text("DELETE FROM nodes WHERE key IN ('gcal:takeouttest-uid', 'me@example.com', 'sam@example.com')"
                           " OR key LIKE 'takeout-chat:%'"))
            s.execute(text("DELETE FROM sync_state WHERE connector = 'takeouttest'"))
            s.commit()


def test_search_joins_existing_project_after_rebuild(db, zpath):
    """Done-when: a search mentioning an existing Project links to it. The Project comes from a synthetic
    recurring calendar series named 'Gloomhaven Night' (3 occurrences)."""
    from igneous.connectors.base import SourceItem, run_sync
    from igneous.graph import entities, spine
    from igneous.rag import chunking

    class Cal:
        name = "takeouttest"

        def sync(self, cursor):
            for i in range(3):
                yield SourceItem(source="gcal", external_id=f"takeouttest/{i}", title="Gloomhaven Night",
                                 body_text="game night", raw_meta={"series_key": "takeouttest-ds", "attendees": []},
                                 occurred_at=datetime(2025, 5, 1 + i, tzinfo=UTC))

    try:
        with SessionLocal() as s:
            run_sync(s, Cal(), account="t")
            takeout.run_import(s, [zpath])
            spine.run(s)
            chunking.run(s)
            entities.run(s, log=None)
            linked = s.execute(text("""
                SELECT DISTINCT si.source FROM edges e
                JOIN nodes p ON p.id = e.dst AND p.type = 'Project' AND p.name ILIKE 'gloomhaven night'
                JOIN source_items si ON si.id = e.source_item_id
                WHERE e.type = 'PART_OF' AND e.props->>'why' = 'mentions' AND si.source LIKE 'takeout\\_%'
            """)).scalars().all()
        assert {"takeout_activity", "takeout_chrome"} <= set(linked)
    finally:
        with SessionLocal() as s:
            s.execute(text("DELETE FROM source_items WHERE source = 'gcal' AND external_id LIKE 'takeouttest/%'"))
            s.execute(text("DELETE FROM nodes WHERE type = 'Event' AND key LIKE 'gcal:takeouttest%'"))
            s.execute(text("DELETE FROM nodes WHERE type = 'Project' AND props->>'derived' = 'true'"
                           " AND key LIKE 'project:gloomhaven night%'"))
            s.execute(text("DELETE FROM sync_state WHERE connector = 'takeouttest'"))
            s.commit()


LEE = {"name": "Lee Park", "email": "lee@example.com", "user_type": "Human"}
SPACE_MSGS = {"messages": [
    {"creator": LEE, "created_date": "Tuesday, May 6, 2025 at 7:00:00\u202fPM UTC", "text": "rules recap tonight?",
     "topic_id": "t", "message_id": "s1"}]}
ELLA = {"name": "Ella Novak", "email": "ella@example.com", "user_type": "Human"}  # a member who never writes
SPACE_INFO = {"name": "Gloomhaven Night", "members": [CHAT_GROUP["members"][0], CHAT_GROUP["members"][1], LEE, ELLA]}
CALENDAR = "\r\n".join([
    "BEGIN:VCALENDAR", "PRODID:-//Google Inc//Google Calendar 70.9054//EN", "VERSION:2.0", "X-WR-CALNAME:me@example.com",
    "X-WR-TIMEZONE:America/New_York",
    "BEGIN:VEVENT", "DTSTART:20250604T180000Z", "DTEND:20250604T183000Z", "DTSTAMP:20250801T000000Z",
    "ORGANIZER;CN=Sam Lee:mailto:sam@example.com", "UID:EVT123@google.com",
    "ATTENDEE;PARTSTAT=ACCEPTED;CN=Me:mailto:me@example.com", "SUMMARY:Standup", "END:VEVENT",
    "BEGIN:VEVENT", "DTSTART;TZID=America/New_York:20250505T190000", "DTEND;TZID=America/New_York:20250505T220000",
    "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4", "DTSTAMP:20250801T000000Z", "UID:gloom@google.com",
    "ATTENDEE;PARTSTAT=ACCEPTED;CN=Lee Park:mailto:lee@example.com", "SUMMARY:Gloomhaven Night", "END:VEVENT",
    "END:VCALENDAR", ""]).encode()


@pytest.fixture
def chat_and_calendar(tmp_path):
    """A minimal export: a named space, a direct message, a single event and a weekly series."""
    return build_zip(tmp_path / "takeout-20250801T000000Z-001.zip", {
        "Google Chat/Groups/Space AAAAgloom/group_info.json": SPACE_INFO,
        "Google Chat/Groups/Space AAAAgloom/messages.json": SPACE_MSGS,
        "Google Chat/Groups/DM abc/group_info.json": CHAT_GROUP,
        "Google Chat/Groups/DM abc/messages.json": CHAT_MSGS,
        "Google Chat/Users/User 1234/user_info.json": {"user": {"name": "Me", "email": "me@example.com"}},
        "Calendar/me@example.com.ics": CALENDAR})


def test_spaces_are_called_by_their_names_and_direct_messages_by_who_is_in_them(chat_and_calendar):
    items, report = takeout.parse([chat_and_calendar])
    ids = by_id(items)
    space, dm = ids["chat/space-aaaagloom/2025-05-06"], ids["chat/dm-abc/2025-05-06"]
    assert space.title == "Gloomhaven Night, 2025-05-06" and space.raw_meta["space"] is True
    assert dm.title == "Sam Lee, 2025-05-06" and dm.raw_meta["conversation"] == "Sam Lee"  # you aren't named
    assert dm.raw_meta["space"] is False and "Lee Park: rules recap tonight?" in space.body_text
    # Every member by address and the name Chat gives them, whether or not they wrote that day.
    assert space.raw_meta["names"] == {"ella@example.com": "Ella Novak", "lee@example.com": "Lee Park",
                                       "me@example.com": "Me", "sam@example.com": "Sam Lee"}
    events = sorted(i.external_id for i in items if i.source == "gcal")
    assert events == ["me@example.com/me@example.com/EVT123"] + [
        f"me@example.com/me@example.com/gloom_202505{d}T230000Z" for d in ("05", "12", "19", "26")]
    assert report["parts"] == {"chat": 2, "calendar": 5} and report["counts"]["calendar_events"] == 2
    assert all(i.raw_meta["product"] == "Calendar" for i in items if i.source == "gcal")


def test_takeout_calendar_and_chat_build_one_event_per_series_and_named_threads(db, chat_and_calendar):
    from igneous import sources
    from igneous.graph import spine

    with SessionLocal() as s:
        first = takeout.run_import(s, [chat_and_calendar])
        assert first["inserted"] == 7 and first["parts"]["calendar"] == 5
    with SessionLocal() as s:
        again = takeout.run_import(s, [chat_and_calendar])
        assert (again["inserted"], again["updated"], again["unchanged"]) == (0, 0, 7)  # the same zip changes nothing
        spine.run(s)
        threads = dict(s.execute(text("""SELECT name, (props->>'group')::bool FROM nodes
                                         WHERE type = 'Thread' AND key LIKE 'takeout-chat:%'""")).all())
        assert threads == {"Gloomhaven Night": True, "Sam Lee": False}
        events = dict(s.execute(text("SELECT name, props->>'series' FROM nodes WHERE type = 'Event'")).all())
        assert events == {"Standup": "false", "Gloomhaven Night": "true"}
        (attended,) = s.execute(text("""SELECT count(*) FROM edges e JOIN nodes p ON p.id = e.src
                                        JOIN nodes ev ON ev.id = e.dst AND ev.name = 'Gloomhaven Night'
                                        WHERE e.type = 'ATTENDED' AND p.key = 'lee@example.com'""")).one()
        assert attended == 1  # one edge for the series, spanning its occurrences
        counts = {row["id"]: row["items"] for row in sources.describe(s)}
        assert counts["takeout"] == 7 and counts["gcal"] == 0  # the export's events count for the export
        # People known from the chat are named as Chat names them: the member who never wrote too.
        named = dict(s.execute(text("SELECT key, name FROM nodes WHERE type = 'Person' AND key LIKE '%@example.com'")).all())
        assert (named["ella@example.com"], named["lee@example.com"], named["sam@example.com"]) == (
            "Ella Novak", "Lee Park", "Sam Lee")


def test_a_chat_name_gives_way_to_a_mail_headers(db, chat_and_calendar):
    from igneous.connectors.base import SourceItem, run_sync
    from igneous.graph import spine

    class Mail:
        name = "takeouttest"

        def sync(self, cursor):
            yield SourceItem(source="email", external_id="inbox/1", title="Game night", body_text="See you there.",
                             occurred_at=datetime(2025, 5, 7, tzinfo=UTC),
                             raw_meta={"from": [{"email": "sam@example.com", "name": "Samuel Lee"}],
                                       "to": [{"email": "me@example.com", "name": ""}], "fingerprint": "fp-1"})

    with SessionLocal() as s:
        takeout.run_import(s, [chat_and_calendar])
        run_sync(s, Mail())
        spine.run(s)
        name = s.execute(text("SELECT name FROM nodes WHERE type = 'Person' AND key = 'sam@example.com'")).scalar_one()
        assert name == "Samuel Lee"


def ics(stamp: str, *events: str, name: str = "me@example.com") -> bytes:
    """A calendar as Google exports one: every event stamped with the export's time."""
    body = [line for e in events for line in ["BEGIN:VEVENT", *e.strip().splitlines(), f"DTSTAMP:{stamp}", "END:VEVENT"]]
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", f"X-WR-CALNAME:{name}", "X-WR-TIMEZONE:UTC", *body,
                         "END:VCALENDAR", ""]).encode()


STANDUP = "DTSTART:20250604T180000Z\nDTEND:20250604T183000Z\nUID:EVT123@google.com\nSUMMARY:Standup"
RETRO = "DTSTART:20250610T180000Z\nUID:retro@google.com\nSUMMARY:Retro"
GLOOM = "DTSTART:20250505T230000Z\nRRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=4\nUID:gloom@google.com\nSUMMARY:Gloomhaven Night"


def calendar_records(s) -> dict[str, str | None]:
    return dict(s.execute(text("SELECT external_id, raw_meta->>'product' FROM source_items WHERE source = 'gcal'")).all())


def test_a_newer_export_replaces_a_calendars_records_and_an_older_one_leaves_them(db, tmp_path):
    august = build_zip(tmp_path / "takeout-20250801T000000Z-001.zip",
                       {"Calendar/me@example.com.ics": ics("20250801T000000Z", STANDUP, GLOOM)})
    # A month later the standup is gone, Gloomhaven skipped its third night, and there is a retro.
    september = build_zip(tmp_path / "takeout-20250901T000000Z-001.zip", {"Calendar/me@example.com.ics": ics(
        "20250901T000000Z", RETRO, GLOOM + "\nEXDATE:20250519T230000Z")})
    stem = "me@example.com/me@example.com"
    with SessionLocal() as s:
        assert takeout.run_import(s, [august])["inserted"] == 5
        newer = takeout.run_import(s, [september])
        assert (newer["inserted"], newer["deleted"]) == (1, 2)
        assert set(calendar_records(s)) == {f"{stem}/retro", *(f"{stem}/gloom_202505{d}T230000Z" for d in ("05", "12", "26"))}
        older = takeout.run_import(s, [august])  # the older export brings nothing back and moves nothing
        assert (older["inserted"], older["updated"], older["deleted"]) == (0, 0, 0)
        assert any("is older than one imported before (2025-09-01)" in n for n in older["notes"])
        assert len(calendar_records(s)) == 4


def test_two_calendars_keyed_alike_in_one_export_keep_each_others_records(db, tmp_path):
    """Two calendars called "Work", and nothing says whose they are: both are keyed under takeout/Work, and importing
    the export again changes nothing (neither removes the other's events)."""
    export = build_zip(tmp_path / "takeout-20250801T000000Z-001.zip", {
        "Calendar/Work.ics": ics("20250801T000000Z", RETRO, name="Work"),
        "Calendar/Work (1).ics": ics("20250801T000000Z", STANDUP, name="Work")})
    with SessionLocal() as s:
        assert takeout.run_import(s, [export])["inserted"] == 2
        again = takeout.run_import(s, [export])
        assert (again["inserted"], again["updated"], again["deleted"], again["unchanged"]) == (0, 0, 0, 2)


def test_a_calendar_sync_keeps_its_window_and_the_export_adds_only_what_came_before(db, tmp_path):
    from fakes import FakeCalendar

    from igneous import sources
    from igneous.connectors.base import run_sync
    from igneous.connectors.gcal import CalendarConnector

    export = build_zip(tmp_path / "takeout-20250801T000000Z-001.zip",
                       {"Calendar/me@example.com.ics": ics("20250801T000000Z", STANDUP, RETRO, GLOOM)})
    stem = "me@example.com/me@example.com"
    # The sync's first listing reaches back to June 1, 2025: Gloomhaven (May) is before it, the standup and the retro
    # in it. The standup moved a day; the retro was deleted after the export, so the listing doesn't have it.
    since_days = (datetime.now(UTC) - datetime(2025, 6, 1, tzinfo=UTC)).days
    moved = {"id": "EVT123", "iCalUID": "EVT123@google.com", "summary": "Standup (moved)",
             "start": {"dateTime": "2025-06-05T18:00:00Z"}, "end": {"dateTime": "2025-06-05T18:30:00Z"}}
    with SessionLocal() as s:
        takeout.run_import(s, [export])
        service = FakeCalendar([{"id": "me@example.com", "summary": "me@example.com"}],
                               [{"items": [moved], "nextSyncToken": "t1"}])
        stats = run_sync(s, CalendarConnector(None, "me@example.com", since_days=since_days, service=service),
                         account="me@example.com")
        assert (stats["inserted"], stats["updated"], stats["deleted"]) == (0, 1, 1)
        assert calendar_records(s) == {f"{stem}/EVT123": None, **{f"{stem}/gloom_202505{d}T230000Z": "Calendar"
                                                                  for d in ("05", "12", "19", "26")}}
        # The same export again: what the sync keeps stays its own (the standup as it moved, the retro gone), and what
        # came before its window stays the export's.
        again = takeout.run_import(s, [export])
        assert (again["inserted"], again["updated"], again["deleted"]) == (0, 0, 0)
        assert any(n.startswith("Google Calendar syncs the calendar me@example.com: 2 of its events were left as the "
                                "sync keeps them") for n in again["notes"])
        title = s.execute(text(f"SELECT title FROM source_items WHERE external_id = '{stem}/EVT123'")).scalar_one()
        assert title == "Standup (moved)"
        counts = {row["id"]: row["items"] for row in sources.describe(s)}
        assert (counts["takeout"], counts["gcal"]) == (4, 1)  # the export's records count for the export


def test_only_the_calendar_folder_or_a_calendar_file_given_on_its_own_is_your_calendar(tmp_path):
    invite = ics("20250801T000000Z", RETRO, name="Team")
    export = build_zip(tmp_path / "takeout-20250801T000000Z-001.zip", {
        "Calendar/me@example.com.ics": ics("20250801T000000Z", STANDUP),
        "Google Chat/Groups/Space AAAA/File-invite.ics": invite,  # an invitation someone shared in a chat
        "Drive/My Drive/old team calendar.ics": invite})
    items, _ = takeout.parse([export])
    assert [i.external_id for i in items if i.source == "gcal"] == ["me@example.com/me@example.com/EVT123"]
    loose = tmp_path / "work.ics"
    loose.write_bytes(invite)
    items, _ = takeout.parse([loose])
    assert [i.external_id for i in items] == ["takeout/Team/retro"]  # no account in a file alone: keyed by the export


def test_a_calendar_that_cannot_be_read_in_part_never_stops_the_import(tmp_path):
    """An event out of Python's range, and a property in a CHARSET nobody knows: the rest of the export is read."""
    odd = ics("20250801T000000Z", "DTSTART:99991231T230000Z\nDURATION:P2D\nRRULE:FREQ=DAILY\nUID:far@google.com",
              "DTSTART:20250605T180000Z\nUID:caf@google.com\nSUMMARY;ENCODING=QUOTED-PRINTABLE;CHARSET=X-NOPE:caf=C3=A9",
              name="Odd")
    export = build_zip(tmp_path / "takeout-20250801T000000Z-001.zip", {
        "Calendar/me@example.com.ics": ics("20250801T000000Z", STANDUP), "Calendar/Odd.ics": odd,
        "Tasks/Tasks.json": TASKS})
    items, report = takeout.parse([export])
    titles = sorted(i.title for i in items if i.source == "gcal")
    assert titles == ["Standup", "café"] and any(i.source == "takeout_tasks" for i in items)
    assert any("couldn't be read were left out (1)" in n for n in report["notes"])


def test_without_user_info_you_are_the_one_person_in_every_conversation(tmp_path):
    space = {"name": "Gloomhaven Night", "members": [CHAT_GROUP["members"][1], LEE, ELLA]}  # Sam isn't in it
    files = {"Google Chat/Groups/Space AAAAgloom/group_info.json": space,
             "Google Chat/Groups/Space AAAAgloom/messages.json": SPACE_MSGS,
             "Google Chat/Groups/DM abc/group_info.json": CHAT_GROUP,
             "Google Chat/Groups/DM abc/messages.json": CHAT_MSGS}
    items, _ = takeout.parse([build_zip(tmp_path / "takeout-20250801T000000Z-001.zip", files)])
    dm = by_id(items)["chat/dm-abc/2025-05-06"]
    assert dm.title == "Sam Lee, 2025-05-06"  # me@example.com is in both conversations: you


def test_a_meet_call_finds_its_repeating_event_by_the_occurrences_id_or_the_series(db, tmp_path):
    """Meet names the calendar event a call was joined from; for a repeating one, by the occurrence's id or by the
    series' id, and either way the call is about the one Event, whichever was read first."""
    from igneous.graph import spine

    header = MEET_CSV.split(b"\n")[0]
    calls = header + b"\n" + b"\n".join(
        f"1,i{n},conf{n},meet:meeting,abc-defg-hij,{eid},,,,2025-05-{day} 23:00:00 UTC,2025-05-{day} 23:30:00 UTC,0:30:00,,,,"
        .encode() for n, (eid, day) in enumerate([("gloom_20250512T230000Z", "12"), ("gloom", "19")])) + b"\n"
    meet = build_zip(tmp_path / "takeout-20250801T000000Z-001.zip",
                     {"Google Meet/ConferenceHistory/conference_history_records.csv": calls})
    calendar = build_zip(tmp_path / "takeout-20250801T000000Z-002.zip",
                         {"Calendar/me@example.com.ics": ics("20250801T000000Z", GLOOM)})
    with SessionLocal() as s:
        takeout.run_import(s, [meet])
        spine.run(s)  # the calls first: each finds its event once the calendar is read
        takeout.run_import(s, [meet, calendar])
        spine.run(s)
        linked = s.execute(text("""
            SELECT f.key FROM edges e JOIN nodes ev ON ev.id = e.dst AND ev.type = 'Event' AND ev.key = 'gcal:gloom@google.com'
            JOIN nodes f ON f.id = e.src WHERE e.type = 'RELATED_TO' ORDER BY 1""")).scalars().all()
        assert linked == ["takeout_meet:meet/conf0", "takeout_meet:meet/conf1"]


def test_takeout_sources_are_hidden_from_models_by_default():
    from igneous.llm import allowed_sources

    assert not set(takeout.TAKEOUT_SOURCES) & allowed_sources("")
    assert set(takeout.TAKEOUT_SOURCES) - allowed_sources("takeout_maps") == set(takeout.TAKEOUT_SOURCES) - {"takeout_maps"}
    # "takeout" is the id the Sources page shows; it covers every product stored under its own source value.
    assert allowed_sources("takeout") == frozenset(takeout.TAKEOUT_SOURCES)
