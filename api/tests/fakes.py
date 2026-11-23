"""In-memory stand-ins for the Google API clients the connectors call. Each records its calls."""
from types import SimpleNamespace

from googleapiclient.errors import HttpError


def http_error(status: int, reason: str = "") -> HttpError:
    return HttpError(SimpleNamespace(status=status, reason=reason), reason.encode())


class Request:
    """A googleapiclient request: `execute()` returns the value, or raises it when it is an exception."""

    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeDrive:
    """files() and changes() over `items` (Drive file resources); `content` maps a file id to its bytes.

    `changes` are the responses changes().list returns in turn (a dict, or an exception to raise)."""

    def __init__(self, items: list[dict], content: dict[str, bytes] | None = None, start_token: str = "start",
                 page_size: int = 100, changes: list | None = None):
        self.items, self.content, self.start_token = items, content or {}, start_token
        self.page_size, self.change_responses = page_size, list(changes or [])
        self.calls: list[tuple[str, dict]] = []
        self.fail_page: str | None = None  # a page token whose listing answers 400

    def files(self):
        return self

    def changes(self):
        return SimpleNamespace(getStartPageToken=self._start, list=self._changes)

    def list(self, pageToken=None, **kwargs):
        self.calls.append(("list", {"pageToken": pageToken, **kwargs}))
        if pageToken is not None and pageToken == self.fail_page:
            return Request(http_error(400, "invalidPageToken"))
        start = int(pageToken or 0)
        page = self.items[start:start + self.page_size]
        more = start + self.page_size < len(self.items)
        return Request({"files": page, **({"nextPageToken": str(start + self.page_size)} if more else {})})

    def export(self, fileId, mimeType):
        self.calls.append(("export", {"fileId": fileId, "mimeType": mimeType}))
        return Request(self.content.get(fileId, b""))

    def get_media(self, fileId, **kwargs):
        self.calls.append(("get_media", {"fileId": fileId}))
        return Request(self.content.get(fileId, b""))

    def get(self, fileId, **kwargs):
        self.calls.append(("get", {"fileId": fileId}))
        found = next((f for f in self.items if f["id"] == fileId), None)
        return Request({"name": found["name"]} if found else http_error(404, "notFound"))

    def _start(self, **kwargs):
        self.calls.append(("start", kwargs))
        return Request({"startPageToken": self.start_token})

    def _changes(self, **kwargs):
        self.calls.append(("changes", kwargs))
        return Request(self.change_responses.pop(0))


class FakeCalendar:
    """calendarList() and events() for `calendars`; `events` are the responses events().list returns in turn."""

    def __init__(self, calendars: list[dict], events: list):
        self.calendars, self.responses = calendars, list(events)
        self.calls: list[dict] = []

    def calendarList(self):
        return SimpleNamespace(list=lambda **kwargs: Request({"items": self.calendars}))

    def events(self):
        return SimpleNamespace(list=self._events)

    def _events(self, **kwargs):
        self.calls.append(kwargs)
        return Request(self.responses.pop(0))


class FakePeople:
    """people().connections().list() over `connections`, one page."""

    def __init__(self, connections: list[dict]):
        self.connections = connections

    def people(self):
        return SimpleNamespace(connections=lambda: SimpleNamespace(
            list=lambda **kwargs: Request({"connections": self.connections})))
