"""Google Drive connector: full backfill plus incremental Changes API sync."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from igneous.config import settings
from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem, Tombstone
from igneous.connectors.gmail import _retry
from igneous.parsers import parse_document

CHECKPOINT_EVERY = 100
FOLDER_MIME = "application/vnd.google-apps.folder"
WORKSPACE_EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}
FILE_FIELDS = "id,name,mimeType,modifiedTime,createdTime,parents,webViewLink,trashed"


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
        b"fileNotDownloadable", b"exportSizeLimitExceeded", b"cannotDownloadAbusiveFile"))


def _is_expired_change_token(exc: HttpError) -> bool:
    if exc.resp.status == 410:
        return True
    content = exc.content or b""
    return exc.resp.status in (400, 404) and any(reason in content for reason in (
        b"pageNotFound", b"invalidPageToken", b"pageToken"))


class DriveConnector:
    """Synchronize Drive files by immutable Drive ID, never by a synthesized path.

    The first sync obtains a Changes API token before a complete ``files.list`` snapshot.
    Subsequent syncs consume the Changes API. Explicit removed/trashed changes become
    ``Tombstone`` events; they never misuse ``CompletedScan``, which is only for a full
    listing whose observed IDs are known complete.
    """

    name = "drive"

    def __init__(self, creds, account: str):
        self.svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.account = account

    def _source_item(self, file: dict) -> SourceItem | None:
        mime_type = file.get("mimeType")
        if mime_type == FOLDER_MIME or file.get("trashed"):
            return None
        export = WORKSPACE_EXPORTS.get(mime_type)
        # Workspace types without an export format (Forms, Sites, shortcuts, etc.) cannot
        # be downloaded. They remain observed during a full scan but have no SourceItem.
        if mime_type and mime_type.startswith("application/vnd.google-apps.") and not export:
            return None
        try:
            if export:
                export_mime, suffix = export
                data = _retry(self.svc.files().export(fileId=file["id"], mimeType=export_mime))
                filename, parser_mime = f"{file.get('name', file['id'])}{suffix}", export_mime
            else:
                data = _retry(self.svc.files().get_media(fileId=file["id"], supportsAllDrives=True))
                filename, parser_mime = file.get("name", file["id"]), file.get("mimeType")
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
            "original_mime": file.get("mimeType"),
            "web_view_link": file.get("webViewLink"),
            "modified_time": file.get("modifiedTime"),
            "parser": parsed.parser,
            "boundaries": parsed.boundaries,
        }
        digest = hashlib.sha256(data + b"\0" + json.dumps(raw_meta, sort_keys=True).encode()).hexdigest()
        return SourceItem(
            source=self.name,
            external_id=_external_id(self.account, file["id"]),
            title=file.get("name") or file["id"],
            body_text=parsed.text,
            occurred_at=_when(file.get("modifiedTime") or file.get("createdTime")),
            mime=parsed.mime,
            raw_meta=raw_meta,
            content_hash=digest,
        )

    def _backfill(self) -> Iterator[SourceItem | Checkpoint | CompletedScan]:
        # Capture the change position before the snapshot so mutations during it are not missed.
        start_token = _retry(self.svc.changes().getStartPageToken(supportsAllDrives=True))["startPageToken"]
        page, observed = None, set()
        while True:
            response = _retry(self.svc.files().list(
                q="trashed = false", spaces="drive", pageSize=settings.drive_page_size, pageToken=page,
                fields=f"nextPageToken,files({FILE_FIELDS})", orderBy="name", supportsAllDrives=True,
                includeItemsFromAllDrives=True))
            for file in response.get("files", []):
                observed.add(_external_id(self.account, file["id"]))
                item = self._source_item(file)
                if item:
                    yield item
            page = response.get("nextPageToken")
            if not page:
                break
            # A limited run deliberately restarts a fresh snapshot later, preventing a partial
            # listing from ever authorizing deletion reconciliation.
            yield Checkpoint({})
        yield CompletedScan({"start_page_token": start_token}, self.name, frozenset(observed))

    def _changes(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | Tombstone]:
        start = cursor["start_page_token"]
        page = cursor.get("changes_page_token")
        while True:
            try:
                response = _retry(self.svc.changes().list(
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
                if change.get("removed") or not file or file.get("trashed"):
                    yield Tombstone(self.name, external_id)
                    continue
                item = self._source_item(file)
                if item:
                    yield item
            page = response.get("nextPageToken")
            if not page:
                new_token = response.get("newStartPageToken", start)
                yield Checkpoint({"start_page_token": new_token})
                break
            yield Checkpoint({"start_page_token": start, "changes_page_token": page})

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan | Tombstone]:
        if "start_page_token" not in cursor:
            yield from self._backfill()
        else:
            yield from self._changes(cursor)
