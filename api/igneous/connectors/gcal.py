"""Google Calendar + Contacts connectors.

Calendar events are bootstrapped for a bounded history and then kept current with
per-calendar sync tokens. Contacts are small enough for a complete People API scan
on every run, allowing safe deletion reconciliation after that scan finishes.
"""
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem, Tombstone
from igneous.connectors.gmail import _retry


def _when(t: dict) -> datetime | None:
    if "dateTime" in t:
        return datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00"))
    if "date" in t:
        return datetime.fromisoformat(t["date"]).replace(tzinfo=UTC)
    return None


def _external_id(account: str, resource_id: str) -> str:
    """Avoid collisions and cross-account deletion reconciliation."""
    return f"{account}/{resource_id}"


class CalendarConnector:
    name = "gcal"

    def __init__(self, creds, account: str, since_days: int = 365):
        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.account = account
        self.since_days = since_days

    def _calendars(self) -> Iterator[dict]:
        page = None
        while True:
            response = _retry(self.svc.calendarList().list(pageToken=page))
            yield from response.get("items", [])
            page = response.get("nextPageToken")
            if not page:
                return

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | Tombstone]:
        tokens = dict(cursor.get("sync_tokens", {}))
        for cal in self._calendars():
            cid = cal["id"]
            token = tokens.get(cid)
            # Google rejects timeMin combined with syncToken. The bounded initial list
            # deliberately has no CompletedScan: it is not a complete source snapshot.
            params = {"calendarId": cid, "singleEvents": True, "maxResults": 250,
                      "showDeleted": bool(token)}
            if token:
                params["syncToken"] = token
            else:
                params["timeMin"] = (datetime.now(UTC) - timedelta(days=self.since_days)).isoformat()
            page = None
            while True:
                try:
                    response = _retry(self.svc.events().list(**params, pageToken=page))
                except HttpError as exc:
                    if exc.resp.status != 410 or not token:
                        raise
                    # An expired token requires the same bounded bootstrap as a new calendar.
                    tokens.pop(cid, None)
                    token, page = None, None
                    params.pop("syncToken", None)
                    params["showDeleted"] = False
                    params["timeMin"] = (datetime.now(UTC) - timedelta(days=self.since_days)).isoformat()
                    continue
                for event in response.get("items", []):
                    external_id = _external_id(self.account, f"{cid}/{event['id']}")
                    if event.get("status") == "cancelled":
                        yield Tombstone(self.name, external_id)
                        continue
                    attendees = [{"name": attendee.get("displayName", ""),
                                  "email": attendee["email"].lower(),
                                  "response": attendee.get("responseStatus")}
                                 for attendee in event.get("attendees", [])
                                 if attendee.get("email") and not attendee.get("resource")]
                    organizer = event.get("organizer", {})
                    body = "\n".join(value for value in (event.get("description", ""),
                                                            event.get("location", "")) if value)
                    yield SourceItem(
                        source=self.name, external_id=external_id,
                        title=event.get("summary") or "(no title)", body_text=body,
                        occurred_at=_when(event.get("start", {})), mime="text/calendar",
                        raw_meta={"calendar": cal.get("summary"), "calendar_id": cid,
                                  "attendees": attendees,
                                  "organizer": {"name": organizer.get("displayName", ""),
                                                "email": organizer.get("email", "").lower()},
                                  "start": event.get("start"), "end": event.get("end"),
                                  "account": self.account,
                                  # One node per recurring series (90% of real events recur): Google's series
                                  # id, else the shared iCalUID, else this event (a true one-off). Scoped like
                                  # external_id (account/calendar/...) so two accounts' series can't collide.
                                  "series_key": _external_id(self.account, f"{cid}/" + (
                                      event.get("recurringEventId") or event.get("iCalUID") or event["id"])),
                                  "recurring": bool(event.get("recurringEventId")),
                                  # The graph's Event identity, shared across accounts: every invitee's copy of a
                                  # meeting carries the same iCalUID (recurring instances share the series' UID),
                                  # so two connected accounts invited to it make one node, not two.
                                  "event_key": "gcal:" + (event.get("iCalUID") or _external_id(
                                      self.account, f"{cid}/" + (event.get("recurringEventId") or event["id"])))})
                page = response.get("nextPageToken")
                if not page:
                    if response.get("nextSyncToken"):
                        tokens[cid] = response["nextSyncToken"]
                    break
            yield Checkpoint({"sync_tokens": tokens})


class ContactsConnector:
    name = "gcontacts"

    def __init__(self, creds, account: str):
        self.svc = build("people", "v1", credentials=creds, cache_discovery=False)
        self.account = account

    def sync(self, cursor: dict) -> Iterator[SourceItem | CompletedScan]:
        page, observed = None, set()
        while True:
            response = _retry(self.svc.people().connections().list(
                resourceName="people/me", pageSize=1000, pageToken=page,
                personFields="names,emailAddresses,organizations,phoneNumbers"))
            for person in response.get("connections", []):
                resource_name = person["resourceName"]
                external_id = _external_id(self.account, resource_name)
                observed.add(external_id)
                emails = [email["value"].lower() for email in person.get("emailAddresses", [])
                          if email.get("value")]
                name = (person.get("names") or [{}])[0].get("displayName", "")
                if not emails and not name:
                    continue
                orgs = [org["name"] for org in person.get("organizations", []) if org.get("name")]
                yield SourceItem(source=self.name, external_id=external_id, title=name or emails[0],
                                 body_text="", mime="text/vcard",
                                 raw_meta={"emails": emails, "orgs": orgs, "account": self.account,
                                           "phones": [phone.get("canonicalForm") or phone.get("value")
                                                      for phone in person.get("phoneNumbers", [])]})
            page = response.get("nextPageToken")
            if not page:
                break
        yield CompletedScan({"synced_at": datetime.now(UTC).isoformat()}, self.name, frozenset(observed),
                            external_id_prefix=f"{self.account}/")
