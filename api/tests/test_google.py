"""Google connectors (Drive, Calendar, Contacts, Gmail) against in-memory API fakes. No network."""
import time
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeCalendar, FakeDrive, FakeGmail, FakePeople, Request, http_error
from googleapiclient.errors import HttpError

from igneous.connectors import gmail, google_api
from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem, Tombstone
from igneous.connectors.drive import DriveConnector, _is_skippable_download_error
from igneous.connectors.gcal import CalendarConnector, ContactsConnector
from igneous.connectors.gmail import GmailConnector

DOC = "application/vnd.google-apps.document"


def items(events) -> list[SourceItem]:
    return [e for e in events if isinstance(e, SourceItem)]


@pytest.fixture(autouse=True)
def unpaced(monkeypatch):
    """The fakes answer instantly; pacing them to the real quotas would only slow the tests down."""
    for api in google_api.BUCKETS:
        monkeypatch.setitem(google_api.BUCKETS, api, google_api.TokenBucket(rate=1e9, burst=1e9))


# ---- Gmail ----

def gmail_sync(fake: FakeGmail, cursor: dict, **kwargs) -> list:
    return list(GmailConnector(None, "me@x.com", service=fake, **kwargs).sync(cursor))


def test_gmail_backfill_resumes_mid_page(monkeypatch):
    monkeypatch.setattr(gmail, "CHECKPOINT_EVERY", 2)
    fake = FakeGmail({f"m{i}": ["INBOX"] for i in range(6)}, pages=[[f"m{i}" for i in range(5)], ["m5"]],
                     history_id="h1")
    events = gmail_sync(fake, {}, since_days=None)
    assert [i.external_id for i in items(events)] == [f"m{i}" for i in range(6)]
    mid = [c.cursor for c in events if isinstance(c, Checkpoint) and "backfill_offset" in c.cursor]
    assert mid[1]["backfill_page"] is None and mid[1]["backfill_offset"] == 4 and mid[1]["history_id"] == "h1"
    fake.calls.clear()
    resumed = gmail_sync(fake, mid[1])
    assert [i.external_id for i in items(resumed)] == ["m4", "m5"]  # after m3, not from m0
    assert resumed[-1].cursor["history_id"] == "h1" and "backfill_q" not in resumed[-1].cursor


def test_gmail_lists_everything_since_the_last_sync_when_its_history_id_expired():
    last_sync = int(time.time()) - 30 * 86400  # a month away: well past Gmail's week of history
    fake = FakeGmail({"m1": ["INBOX"]}, pages=[["m1"]], history=[http_error(404, "notFound")], history_id="h9")
    events = gmail_sync(fake, {"history_id": "h1", "synced_at": last_sync, "all_mail": False, "after": None})
    (listing,) = [kwargs for call, kwargs in fake.calls if call == "list"]
    assert f"after:{last_sync - gmail.CLOCK_MARGIN}" in listing["q"]
    assert [i.external_id for i in items(events)] == ["m1"] and events[-1].cursor["history_id"] == "h9"


def test_gmail_history_skips_what_the_backfill_skips():
    fake = FakeGmail({"promo": ["CATEGORY_PROMOTIONS", "INBOX"], "spam": ["SPAM"], "draft": ["DRAFT"],
                      "kept": ["INBOX", "CATEGORY_PERSONAL"]}, history=[{"history": [{"id": "11", "messagesAdded": [
                          {"message": {"id": "promo", "labelIds": ["CATEGORY_PROMOTIONS", "INBOX"]}},
                          {"message": {"id": "spam"}},  # no labels in the history entry: format=raw has them
                          {"message": {"id": "draft", "labelIds": ["DRAFT"]}},
                          {"message": {"id": "kept", "labelIds": ["INBOX"]}}]}], "historyId": "12"}])
    events = gmail_sync(fake, {"history_id": "10", "synced_at": 1, "all_mail": False, "after": None})
    assert [i.external_id for i in items(events)] == ["kept"]
    assert ("get", {"id": "promo"}) not in fake.calls  # skipped before spending quota on it
    assert events[-1].cursor["history_id"] == "12" and events[-1].cursor["all_mail"] is False


def test_gmail_remembers_all_mail_and_relists_when_the_choice_changes():
    history = {"history": [{"id": "11", "messagesAdded": [
        {"message": {"id": "promo", "labelIds": ["CATEGORY_PROMOTIONS"]}}]}], "historyId": "12"}
    fake = FakeGmail({"promo": ["CATEGORY_PROMOTIONS"]}, history=[history])
    remembered = {"history_id": "10", "synced_at": 1, "all_mail": True, "after": 5}
    assert [i.external_id for i in items(gmail_sync(fake, remembered))] == ["promo"]  # no flag: the account's choice

    fake = FakeGmail({"promo": ["CATEGORY_PROMOTIONS"]}, pages=[["promo"]])
    events = gmail_sync(fake, {**remembered, "all_mail": False}, all_mail=True)
    (listing,) = [kwargs for call, kwargs in fake.calls if call == "list"]
    assert "category:promotions" not in listing["q"] and "after:5" in listing["q"]
    assert [i.external_id for i in items(events)] == ["promo"] and events[-1].cursor["all_mail"] is True


def test_gmail_fetches_a_message_that_leaves_a_skipped_label():
    fake = FakeGmail({"moved": ["INBOX", "CATEGORY_PERSONAL"], "read": ["INBOX"]}, history=[{"history": [
        {"id": "11", "labelsRemoved": [
            {"message": {"id": "moved", "labelIds": ["INBOX", "CATEGORY_PERSONAL"]}, "labelIds": ["CATEGORY_PROMOTIONS"]},
            {"message": {"id": "read", "labelIds": ["INBOX"]}, "labelIds": ["UNREAD"]}]}], "historyId": "11"}])
    events = gmail_sync(fake, {"history_id": "10", "synced_at": 1})
    assert [i.external_id for i in items(events)] == ["moved"]
    assert fake.calls[0][1]["historyTypes"] == ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"]


def tombstones(events) -> list[str]:
    return [e.external_id for e in events if isinstance(e, Tombstone)]


def test_gmail_removes_mail_that_is_deleted_or_enters_a_skipped_label():
    record = {"id": "11", "messagesDeleted": [{"message": {"id": "deleted"}}], "labelsAdded": [
        {"message": {"id": "spammed", "labelIds": ["SPAM"]}, "labelIds": ["SPAM"]},
        {"message": {"id": "binned", "labelIds": ["TRASH"]}, "labelIds": ["TRASH"]},
        {"message": {"id": "promo", "labelIds": ["CATEGORY_PROMOTIONS"]}, "labelIds": ["CATEGORY_PROMOTIONS"]},
        {"message": {"id": "starred", "labelIds": ["INBOX", "STARRED"]}, "labelIds": ["STARRED"]}]}
    narrow = gmail_sync(FakeGmail({}, history=[{"history": [record], "historyId": "11"}]),
                        {"history_id": "10", "synced_at": 1})
    assert tombstones(narrow) == ["deleted", "spammed", "binned", "promo"]
    everything = gmail_sync(FakeGmail({}, history=[{"history": [record], "historyId": "11"}]),
                            {"history_id": "10", "synced_at": 1, "all_mail": True})
    assert tombstones(everything) == ["deleted", "spammed", "binned"]  # all mail keeps Promotions


def test_gmail_removes_a_fetched_message_that_now_carries_a_skipped_label():
    fake = FakeGmail({"now-spam": ["SPAM"]}, history=[{"history": [{"id": "11", "labelsRemoved": [
        {"message": {"id": "now-spam", "labelIds": ["INBOX"]}, "labelIds": ["CATEGORY_PROMOTIONS"]}]}],
        "historyId": "11"}])
    assert tombstones(gmail_sync(fake, {"history_id": "10", "synced_at": 1})) == ["now-spam"]


def test_turning_all_mail_off_removes_the_accounts_bulk_mail(db):
    from sqlalchemy import text

    from igneous.connectors.base import SourceItem as Item
    from igneous.connectors.base import run_sync

    class Stored:
        name = "gmail"

        def sync(self, cursor):
            for i, account in (("promo", "me@x.com"), ("chat", "me@x.com"), ("other-promo", "you@y.com")):
                yield Item("gmail", i, i, "x", raw_meta={"account": account})

    run_sync(db, Stored(), account="seed")
    db.execute(text("INSERT INTO sync_state (connector, account, cursor) VALUES ('gmail', 'me@x.com', "
                    "CAST(:c AS jsonb))"), {"c": '{"history_id": "10", "synced_at": 1, "all_mail": true, "after": 5}'})
    fake = FakeGmail({"chat": ["INBOX"]}, pages=[["chat"]], queries={"{category:promotions": [["promo"]]})
    run_sync(db, GmailConnector(None, "me@x.com", service=fake, all_mail=False), account="me@x.com")
    listings = [kwargs["q"] for call, kwargs in fake.calls if call == "list"]
    assert listings == [gmail.query(False, 5), "{category:promotions category:social category:forums} after:5"]
    # The other account's mail stays: only ids this mailbox lists as bulk are removed.
    assert set(db.execute(text("SELECT external_id FROM source_items WHERE source = 'gmail'")).scalars()) == {
        "chat", "other-promo"}


def test_gmail_checkpoints_fall_between_history_records(monkeypatch):
    monkeypatch.setattr(gmail, "CHECKPOINT_EVERY", 2)
    added = lambda *ids: [{"message": {"id": i, "labelIds": ["INBOX"]}} for i in ids]  # noqa: E731
    fake = FakeGmail({i: ["INBOX"] for i in "abcd"}, history=[{"history": [
        {"id": "11", "messagesAdded": added("a", "b", "c")}, {"id": "12", "messagesAdded": added("d")}],
        "historyId": "12"}])
    events = gmail_sync(fake, {"history_id": "10", "synced_at": 1})
    order = [e.external_id if isinstance(e, SourceItem) else e.cursor["history_id"] for e in events]
    # Resuming from "11" continues after the whole record, so its checkpoint comes after all three messages.
    assert order == ["a", "b", "c", "11", "d", "12"]


# ---- Drive ----

def test_drive_backfill_exports_workspace_files_and_preserves_all_parent_ids():
    drive = FakeDrive([
        {"id": "doc-1", "name": "Plan", "mimeType": DOC, "parents": ["folder-a", "folder-b"],
         "modifiedTime": "2026-10-21T12:00:00Z"},
        {"id": "image-1", "name": "photo.jpg", "mimeType": "image/jpeg"},
        {"id": "folder-a", "name": "Gloomhaven Night", "mimeType": "application/vnd.google-apps.folder"},
    ], {"doc-1": b"ship it"}, start_token="before-backfill")
    events = list(DriveConnector(None, "me@example.com", drive).sync({}))
    (item,) = items(events)
    assert item.external_id == "me@example.com/doc-1" and item.body_text == "ship it"
    assert item.raw_meta["parents"] == ["folder-a", "folder-b"] and item.raw_meta["path"] == "drive/doc-1"
    assert item.raw_meta["parent_names"] == {"folder-a": "Gloomhaven Night"}  # folder-b isn't visible: no name
    assert ("export", {"fileId": "doc-1", "mimeType": "text/plain"}) in drive.calls
    completed = events[-1]  # observes what it stored: a stored file that no longer qualifies is removed
    assert completed == CompletedScan({"start_page_token": "before-backfill"}, "drive",
                                      frozenset({"me@example.com/doc-1"}), scopes=("me@example.com",))


def test_drive_changes_emit_tombstones_and_advance_only_at_final_page():
    drive = FakeDrive([], changes=[{"changes": [
        {"fileId": "deleted", "removed": True},
        {"fileId": "trashed", "file": {"id": "trashed", "trashed": True}},
    ], "newStartPageToken": "cursor-2"}])
    events = list(DriveConnector(None, "me@example.com", drive).sync({"start_page_token": "cursor-1"}))
    assert drive.calls[0][1]["pageToken"] == "cursor-1"
    assert events == [Tombstone("drive", "me@example.com/deleted"), Tombstone("drive", "me@example.com/trashed"),
                      Checkpoint({"start_page_token": "cursor-2"})]


def test_drive_decides_from_the_listing_what_to_download(monkeypatch):
    from igneous.connectors import drive as drive_module

    monkeypatch.setattr(drive_module, "MAX_DOCUMENT_BYTES", 100)
    drive = FakeDrive([
        {"id": "notes", "name": "notes.md", "mimeType": "text/markdown", "size": "20"},
        {"id": "video", "name": "talk.mp4", "mimeType": "video/mp4", "size": "50"},
        {"id": "huge", "name": "dump.txt", "mimeType": "text/plain", "size": "5000"},
        {"id": "sheet", "name": "Budget", "mimeType": "application/vnd.google-apps.spreadsheet"},
    ], {"notes": b"# notes", "sheet": _xlsx()})
    events = list(DriveConnector(None, "me@x.com", drive).sync({}))
    assert [call for call, _ in drive.calls if call in ("get_media", "export")] == ["get_media", "export"]
    assert {"get_media": "notes", "export": "sheet"} == {c: k["fileId"] for c, k in drive.calls if c in ("get_media", "export")}
    assert "size" in next(k for c, k in drive.calls if c == "list")["fields"]
    sheet = next(i for i in items(events) if i.title == "Budget")
    assert "Q1" in sheet.body_text and "Q2" in sheet.body_text  # every sheet, not the first one CSV holds
    assert ("export", {"fileId": "sheet", "mimeType": drive_module.WORKSPACE_EXPORTS[
        "application/vnd.google-apps.spreadsheet"][0]}) in drive.calls


def _xlsx() -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.title = "Q1"
    workbook.active.append(["Q1", 10])
    workbook.create_sheet("Q2").append(["Q2", 20])
    data = BytesIO()
    workbook.save(data)
    return data.getvalue()


def test_an_interrupted_drive_backfill_continues_where_it_stopped(monkeypatch):
    from igneous.connectors import base

    monkeypatch.setattr(base, "CHECKPOINT_SECONDS", 0)
    files = [{"id": f"d{i}", "name": f"d{i}.txt", "mimeType": "text/plain", "size": "3"} for i in range(4)]
    drive = FakeDrive(files, {f"d{i}": b"txt" for i in range(4)}, start_token="T", page_size=2)
    events = list(DriveConnector(None, "me@x.com", drive).sync({}))
    (mid,) = [e.cursor for e in events if isinstance(e, Checkpoint)]
    assert mid == {"backfill": {"start_page_token": "T", "page_token": "2", "observed": ["me@x.com/d0", "me@x.com/d1"]}}
    drive.calls.clear()
    resumed = list(DriveConnector(None, "me@x.com", drive).sync(mid))
    assert [i.external_id for i in items(resumed)] == ["me@x.com/d2", "me@x.com/d3"]  # page one isn't read again
    assert resumed[-1] == CompletedScan({"start_page_token": "T"}, "drive", frozenset(
        f"me@x.com/d{i}" for i in range(4)), scopes=("me@x.com",))
    drive.fail_page = "2"  # a page token that outlived its listing: list again from the start
    again = list(DriveConnector(None, "me@x.com", drive).sync(mid))
    assert len(items(again)) == 4 and isinstance(again[-1], CompletedScan)


def test_drive_folder_names_fail_loudly_unless_the_folder_is_hidden():
    class Broken(FakeDrive):
        def get(self, fileId, **kwargs):
            return Request(http_error(500, "backendError") if fileId == "boom" else http_error(404, "notFound"))

    connector = DriveConnector(None, "me@x.com", Broken([]))
    assert connector._folder_name("hidden") is None  # not shared with this account: the file keeps no folder name
    with pytest.raises(HttpError):  # a failure is not a missing folder: the sync stops and retries later
        connector._folder_name("boom")


def test_each_google_api_has_its_own_pace(monkeypatch):
    taken = []

    class Recorder:
        def __init__(self, api):
            self.api = api

        def take(self, n=1.0):
            taken.append((self.api, n))

    for api in google_api.BUCKETS:
        monkeypatch.setitem(google_api.BUCKETS, api, Recorder(api))
    gmail_sync(FakeGmail({"m": ["INBOX"]}, pages=[["m"]]), {}, since_days=None)
    list(CalendarConnector(None, "me@x.com", service=FakeCalendar([{"id": "c"}], [{"items": []}])).sync({}))
    list(ContactsConnector(None, "me@x.com", service=FakePeople([])).sync({}))
    assert {api for api, _ in taken} == {"gmail", "calendar", "people"}
    assert ("gmail", 5) in taken and ("calendar", 1) in taken  # Gmail counts quota units, the others calls


def test_a_drive_change_that_makes_a_file_unreadable_removes_it():
    drive = FakeDrive([], {"doc": b"\x00\x01binary now"}, changes=[{"changes": [
        {"fileId": "doc", "file": {"id": "doc", "name": "notes.txt", "mimeType": "text/plain"}},
        {"fileId": "form", "file": {"id": "form", "name": "Survey", "mimeType": "application/vnd.google-apps.form"}},
    ], "newStartPageToken": "cursor-2"}])
    events = list(DriveConnector(None, "me@example.com", drive).sync({"start_page_token": "cursor-1"}))
    assert events[:2] == [Tombstone("drive", "me@example.com/doc"), Tombstone("drive", "me@example.com/form")]


def test_drive_skips_non_downloadable_workspace_files_and_export_limits():
    assert _is_skippable_download_error(http_error(403, "fileNotDownloadable"))
    assert _is_skippable_download_error(http_error(403, "exportSizeLimitExceeded"))
    assert _is_skippable_download_error(http_error(403, "cannotDownloadAbusiveFile"))
    assert _is_skippable_download_error(http_error(404, "notFound"))
    assert not _is_skippable_download_error(http_error(403, "insufficientPermissions"))


def test_drive_backfills_again_when_its_change_token_expired():
    drive = FakeDrive([], start_token="fresh-token", changes=[http_error(410, "pageNotFound")])
    events = list(DriveConnector(None, "me@example.com", drive).sync({"start_page_token": "expired-token"}))
    assert events == [CompletedScan({"start_page_token": "fresh-token"}, "drive", frozenset(),
                                    scopes=("me@example.com",))]


def test_drive_ignores_changes_that_are_not_about_a_file():
    drive = FakeDrive([], changes=[{"changes": [{"changeType": "drive", "driveId": "shared-drive"}, {"removed": True}],
                                    "newStartPageToken": "next"}])
    assert list(DriveConnector(None, "me@example.com", drive).sync({"start_page_token": "old"})) == [
        Checkpoint({"start_page_token": "next"})]
    assert "changeType" in drive.calls[0][1]["fields"]


# ---- Calendar and Contacts ----

def test_calendar_uses_clean_incremental_params_and_tombstones_cancelled_events():
    calendar = FakeCalendar([{"id": "team", "summary": "Team"}], [
        {"items": [{"id": "gone", "status": "cancelled"}], "nextSyncToken": "new-token"}])
    since = {"team": "2025-01-01T00:00:00+00:00"}
    events = list(CalendarConnector(None, "me@example.com", service=calendar).sync(
        {"sync_tokens": {"team": "old-token"}, "since": since}))
    (params,) = calendar.calls
    assert params["syncToken"] == "old-token" and "timeMin" not in params and params["showDeleted"] is True
    done = Checkpoint({"sync_tokens": {"team": "new-token"}, "calendars": ["team"], "since": since})
    assert events == [Tombstone("gcal", "me@example.com/team/gone"), done, done]


def test_a_calendars_first_listing_removes_what_it_no_longer_lists_in_its_window(db):
    from sqlalchemy import text

    from igneous.connectors.base import run_sync

    now = datetime.now(UTC)
    for external_id, at in (("me@x.com/team/old", now - timedelta(days=400)),  # before the window: not listed
                            ("me@x.com/team/gone", now - timedelta(days=10)),   # deleted while the token had expired
                            ("me@x.com/team/kept", now - timedelta(days=5))):
        db.execute(text("INSERT INTO source_items (source, external_id, content_hash, occurred_at) "
                        "VALUES ('gcal', :e, 'h', :at)"), {"e": external_id, "at": at})
    # A cursor from before windows were recorded: the calendar is listed again from the start of its window.
    db.execute(text("INSERT INTO sync_state (connector, account, cursor) VALUES ('gcal', 'me@x.com', CAST(:c AS jsonb))"),
               {"c": '{"sync_tokens": {"team": "t1"}, "calendars": ["team"]}'})
    kept = {"id": "kept", "summary": "Kept", "start": {"dateTime": (now - timedelta(days=5)).isoformat()}}
    calendar = FakeCalendar([{"id": "team", "summary": "Team"}], [{"items": [kept], "nextSyncToken": "t2"}])
    stats = run_sync(db, CalendarConnector(None, "me@x.com", service=calendar), account="me@x.com")
    (params,) = calendar.calls
    assert "syncToken" not in params and params["timeMin"]
    assert stats["deleted"] == 1
    assert set(db.execute(text("SELECT external_id FROM source_items")).scalars()) == {
        "me@x.com/team/old", "me@x.com/team/kept"}
    cursor = db.execute(text("SELECT cursor FROM sync_state WHERE account = 'me@x.com'")).scalar_one()
    assert cursor["sync_tokens"] == {"team": "t2"} and cursor["since"] == {"team": params["timeMin"]}


def test_a_calendar_that_leaves_the_list_takes_its_events(db):
    from sqlalchemy import text

    from igneous.connectors.base import run_sync

    for external_id in ("me@x.com/old/e1", "me@x.com/team/e2", "you@y.com/old/e3"):
        db.execute(text("INSERT INTO source_items (source, external_id, content_hash) VALUES ('gcal', :e, 'h')"),
                   {"e": external_id})
    since = '"since": {"team": "2025-01-01T00:00:00+00:00", "old": "2025-01-01T00:00:00+00:00"}'
    db.execute(text("INSERT INTO sync_state (connector, account, cursor) VALUES ('gcal', 'me@x.com', CAST(:c AS jsonb))"),
               {"c": '{"sync_tokens": {"team": "t1", "old": "t2"}, "calendars": ["old", "team"], ' + since + "}"})
    calendar = FakeCalendar([{"id": "team", "summary": "Team"}], [{"items": [], "nextSyncToken": "t3"}])
    run_sync(db, CalendarConnector(None, "me@x.com", service=calendar), account="me@x.com")
    assert set(db.execute(text("SELECT external_id FROM source_items")).scalars()) == {
        "me@x.com/team/e2", "you@y.com/old/e3"}  # another account's calendar of that name stays
    cursor = db.execute(text("SELECT cursor FROM sync_state WHERE account = 'me@x.com'")).scalar_one()
    assert cursor == {"sync_tokens": {"team": "t3"}, "calendars": ["team"],
                      "since": {"team": "2025-01-01T00:00:00+00:00"}}
    # An empty list is a failure to list, not every calendar gone.
    events = list(CalendarConnector(None, "me@x.com", service=FakeCalendar([], [])).sync(cursor))
    assert not tombstones(events)


def test_a_card_without_a_name_ties_its_addresses_together_but_names_no_one(db):
    from sqlalchemy import text

    from igneous.connectors.base import run_sync
    from igneous.graph import spine

    people = FakePeople([
        {"resourceName": "people/anon", "emailAddresses": [{"value": "Anon@Example.com"}, {"value": "a2@example.com"}]},
        {"resourceName": "people/sam", "names": [{"displayName": "Sam Lee"}],
         "emailAddresses": [{"value": "sam@example.com"}]},
    ])
    assert [i.title for i in items(ContactsConnector(None, "me@x.com", service=people).sync({}))] == [None, "Sam Lee"]
    run_sync(db, ContactsConnector(None, "me@x.com", service=people))
    spine.run(db)
    person = {row.key: row for row in db.execute(text("SELECT id, key, name, props FROM nodes WHERE type = 'Person'"))}
    anon, other, sam = person["anon@example.com"], person["a2@example.com"], person["sam@example.com"]
    assert (anon.name, other.name) == ("anon@example.com", "a2@example.com")
    assert "named_by" not in anon.props and "named_by" not in other.props
    assert sam.name == "Sam Lee" and sam.props["named_by"] == "contacts"
    same = db.execute(text("SELECT src, dst FROM edges WHERE type = 'SAME_AS' AND extractor = 'contacts'")).all()
    assert same == [tuple(sorted((anon.id, other.id)))]


def test_contacts_complete_scan_is_account_scoped():
    people = FakePeople([{"resourceName": "people/one", "names": [{"displayName": "One"}]}])
    events = list(ContactsConnector(None, "me@example.com", service=people).sync({}))
    assert events[0].external_id == "me@example.com/people/one" and events[0].title == "One"
    assert isinstance(events[-1], CompletedScan)
    assert events[-1].external_ids == frozenset({"me@example.com/people/one"})
    assert events[-1].scopes == ("me@example.com",)
