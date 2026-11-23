"""Google Drive connector: a resumable backfill, then incremental Changes API sync."""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from igneous.config import settings
from igneous.connectors import google_api
from igneous.connectors.base import Checkpoint, CompletedScan, Every, SourceItem, Tombstone
from igneous.parsers import MAX_DOCUMENT_BYTES, parse_document, supported

FOLDER_MIME = "application/vnd.google-apps.folder"
WORKSPACE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    # .xlsx, not CSV: a CSV export holds only the first sheet.
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                                ".xlsx"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}
FILE_FIELDS = "id,name,mimeType,size,modifiedTime,createdTime,parents,webViewLink,trashed"


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) if value else None


def _external_id(account: str, file_id: str) -> str:
    """Scope opaque Drive IDs to the connected account for source-wide reconciliation."""
    return f"{account}/{file_id}"


def _is_skippable_download_error(exc: HttpError) -> bool:
    if exc.resp.status == 404:
        return True
    content = exc.content or b""
    return exc.resp.status == 403 and any(reason in content for reason in (
        b"fileNotDownloadable", b"exportSizeLimitExceeded", b"cannotDownloadAbusiveFile",
        b"cannotDownloadFile", b"cannotExportFile"))  # owner disabled download for viewers


def _is_expired_change_token(exc: HttpError) -> bool:
    if exc.resp.status == 410:
        return True
    content = exc.content or b""
    return exc.resp.status in (400, 404) and any(reason in content for reason in (
        b"pageNotFound", b"invalidPageToken", b"pageToken"))


class DriveConnector:
    """Synchronize Drive files by immutable Drive ID, never by a synthesized path.

    A file is stored when it is in Drive, not trashed, and readable: a format the parsers read, no larger than
    MAX_DOCUMENT_BYTES, and downloadable. Whether a file qualifies is decided from its listing before anything is
    downloaded. The first sync obtains a Changes API token before a complete `files.list` snapshot, whose
    CompletedScan observes only the files it stored, so a stored file that no longer qualifies is removed; the
    snapshot checkpoints its page and what it has observed, so an interrupted one continues where it stopped. Later
    syncs consume the Changes API: a file removed, trashed or no longer readable becomes a Tombstone.
    """

    name = "drive"

    def __init__(self, creds, account: str, service=None):
        self.svc = service or build("drive", "v3", credentials=creds, cache_discovery=False)
        self.account = account
        self._folder_names: dict[str, str | None] = {}

    def _folder_name(self, folder_id: str) -> str | None:
        """A parent folder's human name (one read-only lookup per folder per sync), so the graph shows
        "Gloomhaven Night" rather than a Drive id. None when the folder isn't visible to this account."""
        if folder_id not in self._folder_names:
            try:
                found = google_api.call("drive", self.svc.files().get(fileId=folder_id, fields="name",
                                                                      supportsAllDrives=True))
                self._folder_names[folder_id] = found.get("name")
            except HttpError as exc:
                if exc.resp.status not in (403, 404):
                    raise
                self._folder_names[folder_id] = None
        return self._folder_names[folder_id]

    def _source_item(self, file: dict) -> SourceItem | None:
        mime_type = file.get("mimeType")
        if mime_type == FOLDER_MIME or file.get("trashed"):
            return None
        export = WORKSPACE_EXPORTS.get(mime_type)
        if export:
            export_mime, suffix = export
            filename, parser_mime = f"{file.get('name', file['id'])}{suffix}", export_mime
            request = self.svc.files().export(fileId=file["id"], mimeType=export_mime)
        elif mime_type and mime_type.startswith("application/vnd.google-apps."):
            return None  # Forms, Sites, shortcuts and the like have no export to read
        else:
            filename, parser_mime = file.get("name", file["id"]), mime_type
            if not supported(filename, parser_mime) or int(file.get("size") or 0) > MAX_DOCUMENT_BYTES:
                return None  # decided from the listing: nothing is downloaded
            request = self.svc.files().get_media(fileId=file["id"], supportsAllDrives=True)
        try:
            data = google_api.call("drive", request)
        except HttpError as exc:
            if _is_skippable_download_error(exc):
                return None
            raise
        parsed = parse_document(data, filename, parser_mime)
        if parsed.status != "ok":
            return None
        parents = list(file.get("parents") or [])
        raw_meta = {
            "account": self.account,
            "drive_id": file["id"],
            "path": f"drive/{file['id']}",
            "folders": [],
            # Drive permits more than one parent. Keep every immutable parent ID, rather
            # than flattening the graph into a misleading filesystem-style path.
            "parents": parents,
            "parent_names": {p: n for p in parents if (n := self._folder_name(p))},
            "original_mime": file.get("mimeType"),
            "web_view_link": file.get("webViewLink"),
            "modified_time": file.get("modifiedTime"),
            "parser": parsed.parser,
            "boundaries": parsed.boundaries,
        }
        return SourceItem(
            source=self.name,
            external_id=_external_id(self.account, file["id"]),
            title=file.get("name") or file["id"],
            body_text=parsed.text,
            occurred_at=_when(file.get("modifiedTime") or file.get("createdTime")),
            mime=parsed.mime,
            raw_meta=raw_meta,
        )

    def _backfill(self, resume: dict | None = None) -> Iterator[SourceItem | Checkpoint | CompletedScan]:
        """A complete listing. `resume` (the cursor's "backfill") continues one: its change token, the next page
        and the ids observed so far."""
        resume = resume or {}
        # Capture the change position before the snapshot so mutations during it are not missed.
        start_token = resume.get("start_page_token") or google_api.call(
            "drive", self.svc.changes().getStartPageToken(supportsAllDrives=True))["startPageToken"]
        page, observed, due = resume.get("page_token"), set(resume.get("observed", [])), Every()
        while True:
            try:
                response = google_api.call("drive", self.svc.files().list(
                    q="trashed = false", spaces="drive", pageSize=settings.drive_page_size, pageToken=page,
                    fields=f"nextPageToken,files({FILE_FIELDS})", orderBy="name", supportsAllDrives=True,
                    includeItemsFromAllDrives=True))
            except HttpError as exc:
                if resume and exc.resp.status == 400:  # a page token that outlived its listing: list again
                    yield from self._backfill()
                    return
                raise
            for file in response.get("files", []):
                item = self._source_item(file)
                if item:
                    observed.add(item.external_id)
                    yield item
            page = response.get("nextPageToken")
            if not page:
                break
            if due():
                yield Checkpoint({"backfill": {"start_page_token": start_token, "page_token": page,
                                               "observed": sorted(observed)}})
        # The listing covers this account only: another account's files are not its to delete.
        yield CompletedScan({"start_page_token": start_token}, self.name, frozenset(observed), scopes=(self.account,))

    def _changes(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | Tombstone]:
        start = cursor["start_page_token"]
        page = cursor.get("changes_page_token")
        while True:
            try:
                response = google_api.call("drive", self.svc.changes().list(
                    pageToken=page or start, spaces="drive", pageSize=settings.drive_page_size,
                    fields=f"nextPageToken,newStartPageToken,changes(changeType,fileId,removed,file({FILE_FIELDS}))",
                    includeRemoved=True, supportsAllDrives=True, includeItemsFromAllDrives=True))
            except HttpError as exc:
                if _is_expired_change_token(exc):
                    yield from self._backfill()
                    return
                raise
            for change in response.get("changes", []):
                # Shared-drive metadata changes have no file ID and do not affect files.
                if change.get("changeType", "file") != "file":
                    continue
                file_id = change.get("fileId")
                if not file_id:
                    continue
                external_id = _external_id(self.account, file_id)
                file = change.get("file")
                item = None if change.get("removed") or not file else self._source_item(file)
                yield item or Tombstone(self.name, external_id)  # removed, trashed or no longer readable
            page = response.get("nextPageToken")
            if not page:
                new_token = response.get("newStartPageToken", start)
                yield Checkpoint({"start_page_token": new_token})
                break
            yield Checkpoint({"start_page_token": start, "changes_page_token": page})

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan | Tombstone]:
        if "backfill" in cursor:
            yield from self._backfill(cursor["backfill"])
        elif "start_page_token" not in cursor:
            yield from self._backfill()
        else:
            yield from self._changes(cursor)
