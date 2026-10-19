"""Google Calendar + Contacts connectors.

Calendar: events with attendees from every calendar the account can see, incremental via syncToken (410 Gone
means the token expired: full resync). Contacts: names and all email addresses from the People API, which
entity resolution can use as ground truth ("these three addresses are one person").
"""
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from igneous.connectors.base import Checkpoint, SourceItem
from igneous.connectors.gmail import _retry


def _when(t: dict) -> datetime | None:
    if "dateTime" in t:
        return datetime.fromisoformat(t["dateTime"].replace("Z", "+00:00"))
    if "date" in t:
        return datetime.fromisoformat(t["date"]).replace(tzinfo=UTC)
    return None


class CalendarConnector:
    name = "gcal"

    def __init__(self, creds, account: str, since_days: int = 365):
        self.svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.account = account
        self.since_days = since_days

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        tokens = dict(cursor.get("sync_tokens", {}))
        cals = _retry(self.svc.calendarList().list()).get("items", [])
        for cal in cals:
            cid = cal["id"]
            params = {"calendarId": cid, "singleEvents": True, "maxResults": 250, "showDeleted": False}
            if cid in tokens:
                params["syncToken"] = tokens[cid]
            else:
                params["timeMin"] = (datetime.now(UTC) - timedelta(days=self.since_days)).isoformat()
            page = None
            while True:
                try:
                    resp = _retry(self.svc.events().list(**params, pageToken=page))
                except HttpError as e:
                    if e.resp.status == 410:  # sync token expired: drop it and do a full sync of this calendar
                        tokens.pop(cid, None)
                        params.pop("syncToken", None)
                        params["timeMin"] = (datetime.now(UTC) - timedelta(days=self.since_days)).isoformat()
                        continue
                    raise
                for ev in resp.get("items", []):
                    if ev.get("status") == "cancelled":
                        continue
                    attendees = [{"name": a.get("displayName", ""), "email": a["email"].lower(),
                                  "response": a.get("responseStatus")}
                                 for a in ev.get("attendees", []) if a.get("email") and not a.get("resource")]
                    org = ev.get("organizer", {})
                    body = "\n".join(x for x in [ev.get("description", ""), ev.get("location", "")] if x)
                    yield SourceItem(
                        source="gcal", external_id=f"{cid}/{ev['id']}", title=ev.get("summary") or "(no title)",
                        body_text=body, occurred_at=_when(ev.get("start", {})), mime="text/calendar",
                        raw_meta={"calendar": cal.get("summary"), "attendees": attendees,
                                  "organizer": {"name": org.get("displayName", ""), "email": org.get("email", "").lower()},
                                  "start": ev.get("start"), "end": ev.get("end"), "account": self.account})
                page = resp.get("nextPageToken")
                if not page:
                    if resp.get("nextSyncToken"):
                        tokens[cid] = resp["nextSyncToken"]
                    break
            yield Checkpoint({"sync_tokens": tokens})


class ContactsConnector:
    name = "gcontacts"

    def __init__(self, creds, account: str):
        self.svc = build("people", "v1", credentials=creds, cache_discovery=False)
        self.account = account

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        # Contacts are small; a full pass each time is simpler than syncTokens and content-hash dedup makes
        # unchanged contacts free.
        page = None
        while True:
            resp = _retry(self.svc.people().connections().list(
                resourceName="people/me", pageSize=1000, pageToken=page,
                personFields="names,emailAddresses,organizations,phoneNumbers"))
            for p in resp.get("connections", []):
                emails = [e["value"].lower() for e in p.get("emailAddresses", []) if e.get("value")]
                name = (p.get("names") or [{}])[0].get("displayName", "")
                if not emails and not name:
                    continue
                orgs = [o.get("name", "") for o in p.get("organizations", []) if o.get("name")]
                yield SourceItem(source="gcontacts", external_id=p["resourceName"], title=name or emails[0],
                                 body_text="", mime="text/vcard",
                                 raw_meta={"emails": emails, "orgs": orgs, "account": self.account,
                                           "phones": [x.get("canonicalForm") or x.get("value")
                                                      for x in p.get("phoneNumbers", [])]})
            page = resp.get("nextPageToken")
            if not page:
                break
        yield Checkpoint({"synced_at": datetime.now(UTC).isoformat()})
