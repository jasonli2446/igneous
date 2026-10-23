"""Google Takeout importer (#109). Synthetic data only; DB tests run in the throwaway test database."""
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


def clean():
    with SessionLocal() as s:
        s.execute(text("DELETE FROM nodes WHERE type = 'Place' OR (type IN ('File', 'Message') AND key LIKE 'takeout\\_%')"))
        s.execute(text("DELETE FROM source_items WHERE source LIKE 'takeout\\_%' OR "
                       "(source = 'ai_chats' AND external_id LIKE 'gemini/gemini-2025-05-01/%')"))
        s.execute(text("DELETE FROM sync_state WHERE connector = 'takeout'"))
        s.commit()


@pytest.fixture
def db(test_db):
    clean()
    yield
    clean()


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


def test_takeout_sources_are_hidden_from_models_by_default():
    from igneous.graph.extract import allowed_sources

    assert not set(takeout.TAKEOUT_SOURCES) & allowed_sources("")
    assert set(takeout.TAKEOUT_SOURCES) - allowed_sources("takeout_maps") == set(takeout.TAKEOUT_SOURCES) - {"takeout_maps"}
