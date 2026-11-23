"""In-memory stand-ins for the Google and GitHub APIs the connectors call (each records its calls), and for macOS
refusing a database."""
import base64
import io
import json
import sqlite3
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

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
        self.fail_page: str | None = None  # a stale page token: its next listing answers 400

    def files(self):
        return self

    def changes(self):
        return SimpleNamespace(getStartPageToken=self._start, list=self._changes)

    def list(self, pageToken=None, **kwargs):
        self.calls.append(("list", {"pageToken": pageToken, **kwargs}))
        if pageToken is not None and pageToken == self.fail_page:
            self.fail_page = None  # a new listing hands out new tokens
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


class FakeGmail:
    """users().getProfile/messages().list/messages().get/history().list over `messages` (id -> labelIds).

    `pages` are the messages.list pages (lists of ids), and `queries` the pages for a query containing a given
    text; `history` holds the history.list responses in turn (a dict, or an exception to raise). Every message's raw
    form is a minimal RFC 822 mail."""

    def __init__(self, messages: dict[str, list[str]], pages: list[list[str]] | None = None,
                 history: list | None = None, history_id: str = "100",
                 queries: dict[str, list[list[str]]] | None = None):
        self.labels, self.pages, self.history_responses = messages, pages or [], list(history or [])
        self.history_id, self.queries = history_id, queries or {}
        self.calls: list[tuple[str, dict]] = []

    def users(self):
        return self

    def getProfile(self, userId):
        return Request({"historyId": self.history_id})

    def messages(self):
        return SimpleNamespace(list=self._list, get=self._get)

    def history(self):
        return SimpleNamespace(list=self._history)

    def _list(self, pageToken=None, **kwargs):
        self.calls.append(("list", {"pageToken": pageToken, **kwargs}))
        pages = next((p for text, p in self.queries.items() if text in (kwargs.get("q") or "")), self.pages)
        index = int(pageToken or 0)
        page = {"messages": [{"id": i} for i in pages[index]]} if pages else {}
        return Request({**page, **({"nextPageToken": str(index + 1)} if index + 1 < len(pages) else {})})

    def _get(self, id, **kwargs):
        self.calls.append(("get", {"id": id}))
        if id not in self.labels:
            return Request(http_error(404, "notFound"))
        raw = f"Message-ID: <{id}@x>\nFrom: a@x.com\nTo: b@x.com\nSubject: {id}\n\nbody {id}\n".encode()
        return Request({"id": id, "threadId": f"t-{id}", "labelIds": self.labels[id],
                        "raw": base64.urlsafe_b64encode(raw).decode(), "internalDate": "1700000000000"})

    def _history(self, **kwargs):
        self.calls.append(("history", kwargs))
        return Request(self.history_responses.pop(0))


class _Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict):
        super().__init__(body)
        self.headers = headers


class FakeGitHub:
    """An `opener` for GithubClient (in place of urlopen). `routes` maps a path to its JSON, or to a function of
    the query that returns it; a path ending in a status code answers that error. /user/repos carries ETag `etag`
    and answers 304 to it."""

    def __init__(self, routes: dict, etag: str = '"repos-1"'):
        self.routes, self.etag = routes, etag
        self.calls: list[tuple[str, dict, str | None]] = []

    def __call__(self, request, timeout=30):
        url = urlsplit(request.full_url)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        sent = request.get_header("If-none-match")
        self.calls.append((url.path, query, sent))
        if url.path == "/user/repos" and sent == self.etag:
            raise HTTPError(request.full_url, 304, "Not Modified", {}, io.BytesIO())
        route = self.routes.get(url.path, 404)
        if isinstance(route, int):
            raise HTTPError(request.full_url, route, "error", {}, io.BytesIO(b'{"message": "error"}'))
        body = route(query) if callable(route) else route
        headers = {"ETag": self.etag} if url.path == "/user/repos" else {}
        return _Response(json.dumps(body).encode(), headers)

    def paths(self) -> list[str]:
        return [path for path, _, _ in self.calls]


class FakePeople:
    """people().connections().list() over `connections`, one page."""

    def __init__(self, connections: list[dict]):
        self.connections = connections

    def people(self):
        return SimpleNamespace(connections=lambda: SimpleNamespace(
            list=lambda **kwargs: Request({"connections": self.connections})))


def refused_by_macos(*args, **kwargs):
    """`sqlite3.connect` as macOS answers an app without Full Disk Access: the file can't be opened."""
    error = sqlite3.OperationalError("unable to open database file")
    error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
    raise error
