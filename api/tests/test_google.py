"""Google connectors (Drive, Calendar, Contacts, Gmail) against in-memory API fakes. No network."""
from fakes import FakeCalendar, FakeDrive, FakePeople, http_error

from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem, Tombstone
from igneous.connectors.drive import DriveConnector, _is_skippable_download_error
from igneous.connectors.gcal import CalendarConnector, ContactsConnector

DOC = "application/vnd.google-apps.document"


def items(events) -> list[SourceItem]:
    return [e for e in events if isinstance(e, SourceItem)]


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
    completed = events[-1]
    assert completed == CompletedScan({"start_page_token": "before-backfill"}, "drive", frozenset(
        {"me@example.com/doc-1", "me@example.com/image-1", "me@example.com/folder-a"}), scopes=("me@example.com",))


def test_drive_changes_emit_tombstones_and_advance_only_at_final_page():
    drive = FakeDrive([], changes=[{"changes": [
        {"fileId": "deleted", "removed": True},
        {"fileId": "trashed", "file": {"id": "trashed", "trashed": True}},
    ], "newStartPageToken": "cursor-2"}])
    events = list(DriveConnector(None, "me@example.com", drive).sync({"start_page_token": "cursor-1"}))
    assert drive.calls[0][1]["pageToken"] == "cursor-1"
    assert events == [Tombstone("drive", "me@example.com/deleted"), Tombstone("drive", "me@example.com/trashed"),
                      Checkpoint({"start_page_token": "cursor-2"})]


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
    events = list(CalendarConnector(None, "me@example.com", service=calendar).sync({"sync_tokens": {"team": "old-token"}}))
    (params,) = calendar.calls
    assert params["syncToken"] == "old-token" and "timeMin" not in params and params["showDeleted"] is True
    assert events == [Tombstone("gcal", "me@example.com/team/gone"), Checkpoint({"sync_tokens": {"team": "new-token"}})]


def test_contacts_complete_scan_is_account_scoped():
    people = FakePeople([{"resourceName": "people/one", "names": [{"displayName": "One"}]}])
    events = list(ContactsConnector(None, "me@example.com", service=people).sync({}))
    assert events[0].external_id == "me@example.com/people/one" and events[0].title == "One"
    assert isinstance(events[-1], CompletedScan)
    assert events[-1].external_ids == frozenset({"me@example.com/people/one"})
    assert events[-1].scopes == ("me@example.com",)
