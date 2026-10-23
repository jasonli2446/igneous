"""Google Takeout (My Activity incl. Gemini, Chrome, Maps) as day-items, places, and visits (#109).

Input: one or several Takeout .zip files (a split export), an extracted folder, or a loose JSON file. Big JSON
members are streamed with ijson, so a multi-GB zip is never read into memory; only the compact per-day
aggregates are. Pass every zip of an export in one run: a day's items are rebuilt from everything seen, so
importing the parts one at a time would overwrite a day with its partial view.

What is read (names are matched case-insensitively and tolerate a missing "Takeout/" prefix):
  My Activity/<Product>/MyActivity.json   one item per product per day. Gemini Apps entries go to the existing
                                          ai_chats gemini path (same external ids as /import/ai-export).
                                          The HTML variant is NOT read; the importer says so.
  Chrome/History.json | BrowserHistory.json   one item per day, repeat visits to a URL collapsed
  Chrome/Bookmarks.html                       one item, grouped by folder
  Maps (your places)/*.json                   saved/starred places and Reviews.json (GeoJSON features)
  Saved/*.csv                                 saved lists in the CSV form newer exports use
  Location History*/Semantic Location History/YYYY/YYYY_MONTH.json   place visits (placeVisit)
  Timeline.json (on-device export)            semanticSegments[].visit; names are usually absent
  Records.json (raw location points) is skipped: huge and low value.
Formats are not guaranteed by Google and change without notice, so every field is read defensively and a
file that fails to parse is reported in `notes` rather than aborting the import.

Everything stored passes through redact(), and URLs lose tracking parameters. The sources below stay out of
every model call unless named in IGNEOUS_LLM_SOURCES (`takeout` names them all; see igneous/llm.py).
"""
import csv
import io
import json
import os
import re
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import ijson

from igneous.connectors.ai_chats import _ts
from igneous.connectors.ai_exports import export_items, gemini_conversations
from igneous.connectors.base import Checkpoint, SourceItem
from igneous.connectors.redact import redact

TAKEOUT_SOURCES = ("takeout_activity", "takeout_chrome", "takeout_maps", "takeout_tasks", "takeout_meet",
                   "takeout_chat")
# My Activity products that are advertising/analytics telemetry, not things you did.
SKIP_PRODUCTS = {"ads", "google analytics", "takeout"}
MAX_LINES = 1000  # per item; a day with more than this is almost certainly a script, not you
GEMINI_PRODUCTS = {"gemini apps", "gemini"}
TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|gclsrc|dclid|msclkid|yclid|mc_cid|mc_eid|_ga|_gl|igshid|si|"
                      r"usg|ved|ei|sa|sxsrf|gs_lcp|gs_lp|sclient|oq|aqs|rlz|ie|cid|ref_src|spm|"
                      r"vero_id|trk|trkid|hsa_.*|_hsenc|_hsmi|mkt_tok)$", re.I)
URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')]+")


class TakeoutError(ValueError):
    """The input is not a readable Takeout export; the message is safe to show to the user."""


# ---- text helpers ----

def clean_url(url: str) -> str:
    """Unwrap Google redirects, drop tracking parameters and fragments, lowercase the host."""
    try:
        parts = urlsplit(url.strip())
        if parts.netloc.lower().endswith("google.com") and parts.path == "/url":
            target = dict(parse_qsl(parts.query)).get("q") or dict(parse_qsl(parts.query)).get("url")
            if target:
                return clean_url(unquote(target))
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)]
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))
    except ValueError:
        return url


def domain(url: str) -> str:
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return host.removeprefix("www.")


def _clean_text(text: str) -> str:
    return URL_IN_TEXT.sub(lambda m: clean_url(m.group(0)), text).strip()


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def _any_ts(raw) -> datetime | None:
    """ISO string, or epoch milliseconds as int/str (older Semantic Location History)."""
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(raw / 1000, UTC) if raw else None
    if isinstance(raw, str):
        if raw.isdigit():
            return datetime.fromtimestamp(int(raw) / 1000, UTC)
        return _utc(_ts(raw))
    return None


_CHAT_FORMATS = ("%A, %B %d, %Y at %I:%M:%S %p", "%A, %b %d, %Y at %I:%M:%S %p", "%B %d, %Y at %I:%M:%S %p")


def _chat_ts(raw) -> datetime | None:
    """'Tuesday, May 6, 2025 at 3:04:05 PM UTC' (the space before PM is a narrow no-break space) or ISO."""
    if not isinstance(raw, str):
        return None
    t = re.sub(r"[\u202f\u00a0\s]+", " ", raw).strip()
    t = re.sub(r" (UTC|GMT)$", "", t)
    for fmt in _CHAT_FORMATS:
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return _utc(_ts(raw))


def _meet_ts(raw) -> datetime | None:
    """'2026-05-04 14:00:00 UTC'."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip().removesuffix(" UTC"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return _utc(_ts(raw.strip()))


def _red(text: str | None, counter: list[int]) -> str:
    clean, found = redact(text or "")
    counter[0] += sum(found.values())
    return clean or ""


def _duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end or end <= start:
        return ""
    minutes = int((end - start).total_seconds() // 60)
    if minutes < 1:
        return ""
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


# ---- places ----

def place_key(place_id: str | None, name: str | None, lat: float | None, lng: float | None) -> str | None:
    """`place:<placeId>` when Google gives one, else normalized name plus coordinates rounded to ~100 m."""
    if place_id:
        return f"place:{place_id}"
    n = _norm_name(name or "")
    has_coords = lat is not None and lng is not None and (lat, lng) != (0, 0)
    if n and has_coords:
        return f"place:{n}@{lat:.3f},{lng:.3f}"
    return f"place:{n}" if n else None


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _place(key: str, name: str | None, address: str | None, lat, lng, place_id: str | None, red: list[int]) -> dict:
    return {"key": key, "name": _red(name, red) or None, "address": _red(address, red) or None,
            "lat": lat, "lng": lng, "place_id": place_id}


# ---- files ----

@dataclass
class Member:
    path: str  # logical path below Takeout/, '/'-joined
    open: Callable[[], BinaryIO]


def _logical(parts: list[str]) -> str:
    low = [p.lower() for p in parts]
    if "takeout" in low:
        parts = parts[low.index("takeout") + 1:]
    return "/".join(parts)


class Tree:
    """Members of zips, folders and loose files, with zips held open until the `with` block ends."""

    def __init__(self, paths: list[Path]):
        self.paths, self._zips, self.members = paths, [], []

    def __enter__(self) -> list[Member]:
        try:
            for p in self.paths:
                self._add(Path(p).expanduser())
        except BaseException:
            self.__exit__()
            raise
        return self.members

    def __exit__(self, *exc):
        for z in self._zips:
            z.close()
        self._zips.clear()

    def _add(self, p: Path):
        if not p.exists():
            raise TakeoutError(f"{p} does not exist.")
        if p.is_dir():
            for root, _, files in os.walk(p):
                for f in sorted(files):
                    full = Path(root) / f
                    rel = full.relative_to(p).parts
                    if f.lower().endswith(".zip") and zipfile.is_zipfile(full):
                        self._add_zip(full)
                    elif "__MACOSX" not in rel:
                        # An extracted folder may be the Takeout dir, its parent, or a copy of one product.
                        self.members.append(Member(_logical([p.name, *rel]), lambda full=full: open(full, "rb")))
        elif zipfile.is_zipfile(p):
            self._add_zip(p)
        elif p.suffix.lower() in (".json", ".html", ".csv"):
            self.members.append(Member(p.name, lambda p=p: open(p, "rb")))
        else:
            raise TakeoutError(f"{p.name} is not a Takeout .zip, an extracted folder, or a JSON file.")

    def _add_zip(self, p: Path):
        zf = zipfile.ZipFile(p)
        self._zips.append(zf)
        for name in zf.namelist():
            if name.endswith("/") or name.startswith("__MACOSX"):
                continue
            self.members.append(Member(_logical(name.split("/")), lambda zf=zf, name=name: zf.open(name)))


def classify(path: str) -> tuple[str, str | None] | None:
    """(kind, hint) for a member path, or None if it is not something we read."""
    parts = path.split("/")
    low = [p.lower() for p in parts]
    name, dirs = low[-1], low[:-1]
    if name == "records.json":
        return "records", None
    if name.startswith("myactivity") and name.endswith((".json", ".html")):
        i = dirs.index("my activity") if "my activity" in dirs else -1
        product = parts[i + 1] if i >= 0 and len(parts) > i + 2 else None
        return ("activity" if name.endswith(".json") else "activity_html"), product
    if "chrome" in dirs and name in ("history.json", "browserhistory.json"):
        return "chrome_history", None
    if name == "browserhistory.json":
        return "chrome_history", None
    if "chrome" in dirs and name == "bookmarks.html":
        return "chrome_bookmarks", None
    if "chrome" in dirs and name == "reading list.html":
        return "chrome_reading_list", None
    if "google chat" in dirs:
        if name == "messages.json":
            return "chat_messages", parts[-2]
        if name == "group_info.json":
            return "chat_group", parts[-2]
        if name == "user_info.json":
            return "chat_user", None
    if "tasks" in dirs and name == "tasks.json":
        return "tasks", None
    if name == "conference_history_records.csv":
        return "meet", None
    stem = Path(parts[-1]).stem
    if any(d.startswith("maps") and "your places" in d for d in dirs) and name.endswith(".json"):
        return ("maps_reviews" if "review" in name else "maps_places"), stem
    if "saved" in dirs and name.endswith(".csv"):
        return "maps_saved_csv", stem
    if re.fullmatch(r"\d{4}_[a-z]+\.json", name) and "semantic location history" in dirs:
        return "semantic", None
    if name == "timeline.json":
        return "timeline", None
    return None


# ---- parsers (each yields/accumulates compactly; bad input is reported, never fatal) ----

def _stream(m: Member, prefix: str):
    with m.open() as fh:
        yield from ijson.items(fh, prefix, use_float=True)


def activity_line(e: dict, red: list[int]) -> tuple[datetime, str] | None:
    """'[time] Searched for ...' text for one My Activity entry, or None for noise (bare 'Used Google Search',
    ad interactions, empty titles)."""
    title, at = e.get("title"), _utc(_ts(e.get("time")))
    if not isinstance(title, str) or not at:
        return None
    if any(isinstance(d, dict) and "google ads" in str(d.get("name", "")).lower() for d in e.get("details") or []):
        return None
    t, url = title.strip(), e.get("titleUrl") if isinstance(e.get("titleUrl"), str) else None
    if not t or t in ("Searched for", "Visited", "Watched", "Viewed"):
        return None
    if re.fullmatch(r"Used [^:]{1,60}", t) and not url and not e.get("subtitles"):
        return None  # "Used Google Search": the app opened, nothing you did
    text = _clean_text(t)
    if url and t.startswith(("Visited", "Viewed")):
        dom = domain(clean_url(url))
        if dom and dom not in text:
            text += f" ({dom})"
    elif t.startswith("Watched"):
        subs = [s.get("name") for s in e.get("subtitles") or [] if isinstance(s, dict) and s.get("name")]
        if subs:
            text += f" — {subs[0]}"
    return at, _red(text, red)


class _Bookmarks(HTMLParser):
    """Netscape bookmark file: <DT><H3>Folder</H3><DL> ... <DT><A HREF=...>Title</A> ... </DL>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.pending: str | None = None
        self.marks: list[tuple[str, str, str]] = []  # (folder path, title, url)
        self._tag, self._buf, self._href = None, [], None

    def handle_starttag(self, tag, attrs):
        if tag in ("h3", "a"):
            self._tag, self._buf = tag, []
            self._href = dict(attrs).get("href") if tag == "a" else None
        elif tag == "dl":
            self.stack.append(self.pending or "")
            self.pending = None

    def handle_endtag(self, tag):
        if tag == "h3" and self._tag == "h3":
            self.pending = "".join(self._buf).strip()
            self._tag = None
        elif tag == "a" and self._tag == "a":
            if self._href:
                self.marks.append(("/".join(f for f in self.stack if f), "".join(self._buf).strip(), self._href))
            self._tag = None
        elif tag == "dl" and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self._tag:
            self._buf.append(data)


def _latlng(s) -> tuple[float | None, float | None]:
    nums = re.findall(r"-?\d+(?:\.\d+)?", s) if isinstance(s, str) else []
    return (float(nums[0]), float(nums[1])) if len(nums) >= 2 else (None, None)


def _geo_feature(f: dict, red: list[int]) -> dict | None:
    """One Maps (your places) GeoJSON feature -> place dict (+ review fields), or None."""
    if not isinstance(f, dict):
        return None
    props = f.get("properties") if isinstance(f.get("properties"), dict) else {}
    loc = props.get("Location") if isinstance(props.get("Location"), dict) else {}
    name = props.get("Title") or props.get("name") or loc.get("Business Name")
    coords = (f.get("geometry") or {}).get("coordinates") if isinstance(f.get("geometry"), dict) else None
    lat = lng = None
    if isinstance(coords, list) and len(coords) >= 2:
        lng, lat = _num(coords[0]), _num(coords[1])
    geo = loc.get("Geo Coordinates") if isinstance(loc.get("Geo Coordinates"), dict) else {}
    if lat is None or (lat, lng) == (0, 0):
        lat, lng = _num(geo.get("Latitude")), _num(geo.get("Longitude"))
    key = place_key(None, name, lat, lng)
    if not key:
        return None
    place = _place(key, name, loc.get("Address") or props.get("Address"), lat, lng, None, red)
    place["date"] = props.get("date") or props.get("Published") or props.get("Updated")
    place["comment"] = _red(props.get("Comment") or props.get("Review Comment") or props.get("Review"), red)
    place["rating"] = props.get("Star Rating") or props.get("Rating")
    return place


class _Import:
    """Accumulates everything seen across files, then renders stable items."""

    def __init__(self, progress: Callable[[dict], None] | None):
        self.progress = progress
        self.activity: dict[tuple[str, str], list[tuple[datetime, str]]] = defaultdict(list)
        self.activity_secrets: dict[tuple[str, str], int] = defaultdict(int)
        self.gemini: list[dict] = []
        self.chrome: dict[str, dict[str, list]] = defaultdict(dict)  # day -> clean url -> [first, n, title]
        self.chrome_secrets: dict[str, int] = defaultdict(int)
        self.bookmarks: list[tuple[str, str, str]] = []
        self.visits: dict[str, dict[tuple[str, str], dict]] = defaultdict(dict)  # day -> (key, start) -> visit
        self.lists: dict[tuple[str, str], list[dict]] = defaultdict(list)  # (kind, list name) -> places
        self.list_secrets: dict[tuple[str, str], int] = defaultdict(int)
        self.reading: list[tuple[str, str, str]] = []
        self.chat: dict[str, dict] = {}  # group dir -> {members, days: {day: [(at, email, text)]}}
        self.tasks: list[dict] = []
        self.meet: list[dict] = []
        self.notes: list[str] = []
        self.counts: dict[str, int] = defaultdict(int)

    def note(self, msg: str):
        if msg not in self.notes:
            self.notes.append(msg)

    def _safe(self, m: Member, fn: Callable[[], None]):
        try:
            fn()
        except (ijson.JSONError, ijson.IncompleteJSONError, UnicodeDecodeError, ValueError) as e:
            self.note(f"{m.path}: could not be read completely ({type(e).__name__}); what was readable was kept.")

    # -- activity
    def read_activity(self, m: Member, hint: str | None):
        def go():
            for e in _stream(m, "item"):
                if not isinstance(e, dict):
                    continue
                product = hint or e.get("header") or "Google"
                if _norm_name(product) in SKIP_PRODUCTS:
                    self.counts["skipped_noise"] += 1
                    continue
                if _norm_name(str(e.get("header") or product)) in GEMINI_PRODUCTS or _norm_name(product) in GEMINI_PRODUCTS:
                    self.gemini.append(e)
                    continue
                red = [0]
                row = activity_line(e, red)
                if row:
                    key = (product, row[0].date().isoformat())
                    self.activity[key].append(row)
                    self.activity_secrets[key] += red[0]
                    self.counts["activity_entries"] += 1
        self._safe(m, go)

    # -- chat / tasks / meet / reading list
    def read_chat(self, m: Member, group: str | None, kind: str):
        g = self.chat.setdefault(group or "?", {"members": {}, "days": defaultdict(list)})

        def go():
            with m.open() as fh:
                data = json.load(fh)
            if kind == "chat_group":
                for u in data.get("members") or []:
                    if isinstance(u, dict) and u.get("email"):
                        g["members"][u["email"].lower()] = u.get("name") or u["email"]
                return
            for msg in data.get("messages") or []:
                at, text_ = _chat_ts(msg.get("created_date")), msg.get("text")
                who = msg.get("creator") if isinstance(msg.get("creator"), dict) else {}
                if not at or not isinstance(text_, str) or not text_.strip() or not who.get("email"):
                    continue
                g["members"].setdefault(who["email"].lower(), who.get("name") or who["email"])
                g["days"][at.date().isoformat()].append((at, who["email"].lower(), text_))
                self.counts["chat_messages"] += 1
        self._safe(m, go)

    def read_tasks(self, m: Member):
        def go():
            with m.open() as fh:
                data = json.load(fh)
            for lst in data.get("items") or []:
                if not isinstance(lst, dict):
                    continue
                self.tasks.append({"id": str(lst.get("id") or lst.get("title")), "title": lst.get("title") or "Tasks",
                                   "updated": lst.get("updated") or lst.get("create_time"),
                                   "tasks": [t for t in lst.get("items") or [] if isinstance(t, dict)]})
                self.counts["tasks"] += len(self.tasks[-1]["tasks"])
        self._safe(m, go)

    def read_meet(self, m: Member):
        def go():
            with m.open() as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")):
                    start = _meet_ts(r.get("Start Time"))
                    cid = r.get("Conference Id") or r.get("Item Id")
                    if not start or not cid:
                        continue
                    self.meet.append({"id": cid, "start": start, "end": _meet_ts(r.get("End Time")),
                                      "duration": r.get("Duration"), "kind": r.get("Item Type") or "",
                                      "code": r.get("Meeting Code") or "", "event_id": r.get("Event Id") or "",
                                      "counterparts": r.get("Call Counterparts") or ""})
                    self.counts["meet_conferences"] += 1
        self._safe(m, go)

    def read_reading_list(self, m: Member):
        p = _Bookmarks()
        with m.open() as fh:
            p.feed(fh.read().decode("utf-8", errors="replace"))
        self.reading += [(f or "Reading list", t, u) for f, t, u in p.marks if u.startswith(("http://", "https://"))]

    # -- chrome
    def read_chrome(self, m: Member):
        def entries():
            got = False
            for e in _stream(m, "Browser History.item"):
                got = True
                yield e
            if not got:
                yield from _stream(m, "item")

        def go():
            for e in entries():
                url = e.get("url") if isinstance(e, dict) else None
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    continue
                us = e.get("time_usec")
                at = datetime.fromtimestamp(us / 1e6, UTC) if isinstance(us, int | float) and us else _utc(_ts(e.get("time")))
                if not at:
                    continue
                red = [0]
                clean = clean_url(url)
                title = _red(_clean_text(e.get("title") or ""), red)
                rec = self.chrome[at.date().isoformat()].setdefault(clean.split("://", 1)[-1].rstrip("/"), [at, 0, title, clean])
                rec[0], rec[1] = min(rec[0], at), rec[1] + 1
                rec[2] = rec[2] or title
                self.chrome_secrets[at.date().isoformat()] += red[0]
                self.counts["chrome_visits"] += 1
        self._safe(m, go)

    def read_bookmarks(self, m: Member):
        p = _Bookmarks()
        with m.open() as fh:
            p.feed(fh.read().decode("utf-8", errors="replace"))
        self.bookmarks += [m_ for m_ in p.marks if m_[2].startswith(("http://", "https://"))]

    # -- maps
    def add_visit(self, start, end, name, address, lat, lng, place_id, red):
        key = place_key(place_id, name, lat, lng)
        if not key or not start:
            return
        v = _place(key, name, address, lat, lng, place_id, red)
        v.update(start=start.isoformat(), end=end.isoformat() if end else None)
        self.visits[start.date().isoformat()].setdefault((key, v["start"]), v)

    def read_semantic(self, m: Member):
        def go():
            for o in _stream(m, "timelineObjects.item"):
                pv = o.get("placeVisit") if isinstance(o, dict) else None
                if not isinstance(pv, dict):
                    continue
                loc, dur = pv.get("location") or {}, pv.get("duration") or {}
                lat = loc.get("latitudeE7") / 1e7 if isinstance(loc.get("latitudeE7"), int | float) else None
                lng = loc.get("longitudeE7") / 1e7 if isinstance(loc.get("longitudeE7"), int | float) else None
                start = _any_ts(dur.get("startTimestamp") or dur.get("startTimestampMs"))
                end = _any_ts(dur.get("endTimestamp") or dur.get("endTimestampMs"))
                red = [0]
                self.add_visit(start, end, loc.get("name") or loc.get("address"), loc.get("address"), lat, lng,
                               loc.get("placeId"), red)
                self.counts["place_visits"] += 1
        self._safe(m, go)

    def read_timeline(self, m: Member):
        """On-device export. Visits carry a placeId and a semantic type (Home, Work) but usually no name."""
        labels = {"INFERRED_HOME": "Home", "HOME": "Home", "INFERRED_WORK": "Work", "WORK": "Work",
                  "SEARCHED_ADDRESS": None, "ALIASED_LOCATION": None}

        def go():
            for seg in _stream(m, "semanticSegments.item"):
                visit = seg.get("visit") if isinstance(seg, dict) else None
                cand = visit.get("topCandidate") if isinstance(visit, dict) else None
                if not isinstance(cand, dict):
                    continue
                lat, lng = _latlng((cand.get("placeLocation") or {}).get("latLng"))
                name = cand.get("name") or labels.get(str(cand.get("semanticType") or "").removeprefix("TYPE_"))
                self.add_visit(_any_ts(seg.get("startTime")), _any_ts(seg.get("endTime")), name, None, lat, lng,
                               cand.get("placeId"), [0])
                self.counts["place_visits"] += 1
        self._safe(m, go)

    def read_places(self, m: Member, stem: str, reviews: bool):
        def go():
            for f in _stream(m, "features.item"):
                red = [0]
                p = _geo_feature(f, red)
                if p:
                    self.lists[("reviews" if reviews else "saved", stem)].append(p)
                    self.list_secrets[("reviews" if reviews else "saved", stem)] += red[0]
                    self.counts["reviews" if reviews else "saved_places"] += 1
        self._safe(m, go)

    def read_saved_csv(self, m: Member, stem: str):
        with m.open() as fh:
            for row in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")):
                red = [0]
                name = row.get("Title") or row.get("Name")
                key = place_key(None, name, None, None)
                if key:
                    p = _place(key, name, None, None, None, None, red)
                    p.update(date=None, comment=_red(row.get("Note") or row.get("Comment"), red), rating=None)
                    self.lists[("saved", stem)].append(p)
                    self.list_secrets[("saved", stem)] += red[0]
                    self.counts["saved_places"] += 1

    # -- rendering
    def items(self) -> Iterator[SourceItem]:
        for (product, day), rows in sorted(self.activity.items()):
            rows.sort(key=lambda r: r[0])
            lines = list(dict.fromkeys(f"[{dt:%H:%M}] {text}" for dt, text in rows))
            yield self._day_item("takeout_activity", f"activity/{_slug(product)}/{day}",
                                 f"{product} activity, {day}", day, lines, rows[0][0],
                                 {"product": product, "entries": len(rows),
                                  "redactions": self.activity_secrets[(product, day)]})
        for day, urls in sorted(self.chrome.items()):
            recs = sorted(urls.values(), key=lambda r: r[0])
            lines = [f"[{r[0]:%H:%M}] Visited {r[2] or domain(r[3])} ({domain(r[3])})" + (f" x{r[1]}" if r[1] > 1 else "")
                     for r in recs]
            yield self._day_item("takeout_chrome", f"chrome/history/{day}", f"Chrome history, {day}", day, lines,
                                 recs[0][0], {"product": "Chrome", "urls": len(recs),
                                              "visits": sum(r[1] for r in recs), "redactions": self.chrome_secrets[day]})
        yield from self._bookmark_item()
        yield from self._extra_items()
        for day, vs in sorted(self.visits.items()):
            vl = sorted(vs.values(), key=lambda v: v["start"])
            lines = []
            for v in vl:
                st, en = _utc(_ts(v["start"])), _utc(_ts(v["end"])) if v["end"] else None
                label = v["name"] or "Unnamed place"
                tail = f", {v['address']}" if v["address"] and v["address"] != v["name"] else ""
                dur = _duration(st, en)
                lines.append(f"[{st:%H:%M}] Visited {label}{tail}" + (f" ({dur})" if dur else ""))
            yield SourceItem(source="takeout_maps", external_id=f"maps/visits/{day}", title=f"Places visited, {day}",
                             body_text="\n".join(lines[:MAX_LINES]), occurred_at=_utc(_ts(vl[0]["start"])),
                             mime="text/plain", raw_meta={"kind": "visits", "product": "Maps", "day": day,
                                                          "visits": vl, "redactions": 0})
        for (kind, stem), places in sorted(self.lists.items()):
            uniq = list({p["key"]: p for p in places}.values())
            lines = []
            for p in uniq:
                line = f"- {p['name'] or p['key']}" + (f", {p['address']}" if p["address"] else "")
                if p.get("rating"):
                    line += f" ({p['rating']} stars)"
                if p.get("comment"):
                    line += f": {p['comment']}"
                lines.append(line)
            dates = sorted(d for d in (_utc(_ts(p["date"])) if isinstance(p.get("date"), str) else None
                                       for p in uniq) if d)
            label = "Reviews" if kind == "reviews" else f"Saved places: {stem}"
            yield SourceItem(source="takeout_maps", external_id=f"maps/{kind}/{_slug(stem)}", title=label,
                             body_text="\n".join(lines[:MAX_LINES]), occurred_at=dates[-1] if dates else None,
                             mime="text/plain", raw_meta={"kind": kind, "product": "Maps", "list": stem,
                                                          "places": [{k: v for k, v in p.items() if k not in ("date",)}
                                                                     for p in uniq],
                                                          "redactions": self.list_secrets[(kind, stem)]})
        if self.gemini:
            yield from export_items("gemini", gemini_conversations(self.gemini))

    def _day_item(self, source, external_id, title, day, lines, first: datetime, meta) -> SourceItem:
        body = lines[:MAX_LINES]
        if len(lines) > MAX_LINES:
            body.append(f"... {len(lines) - MAX_LINES} more")
        return SourceItem(source=source, external_id=external_id, title=title, body_text="\n".join(body),
                          occurred_at=first, mime="text/plain", raw_meta={"day": day, **meta})

    def _extra_items(self) -> Iterator[SourceItem]:
        if self.reading:
            red = [0]
            body = "\n".join(f"- {_red(t, red) or domain(clean_url(u))} ({_red(clean_url(u), red)})"
                             for _, t, u in self.reading[:MAX_LINES])
            yield SourceItem(source="takeout_chrome", external_id="chrome/reading-list", title="Chrome reading list",
                             body_text=body, mime="text/plain",
                             raw_meta={"product": "Chrome", "entries": len(self.reading), "redactions": red[0]})
        for lst in self.tasks:
            red = [0]
            lines = []
            for t in lst["tasks"][:MAX_LINES]:
                title = _red(t.get("title") or "(untitled)", red)
                bits = [t.get("status") or "", f"due {t['due'][:10]}" if t.get("due") else "",
                        f"done {t['completed'][:10]}" if t.get("completed") else ""]
                line = f"- {title}" + (f" [{', '.join(b for b in bits if b)}]" if any(bits) else "")
                if t.get("notes"):
                    line += f"\n  {_red(_clean_text(str(t['notes'])), red)}"
                lines.append(line)
            yield SourceItem(source="takeout_tasks", external_id=f"tasks/{_slug(lst['id'])}",
                             title=f"Tasks: {_red(lst['title'], red)}", body_text="\n".join(lines),
                             occurred_at=_any_ts(lst.get("updated")), mime="text/plain",
                             raw_meta={"product": "Tasks", "tasks": len(lst["tasks"]), "redactions": red[0]})
        for c in sorted(self.meet, key=lambda c: c["start"]):
            red = [0]
            is_call = "call" in c["kind"]
            label = "Direct call" if is_call else f"Meeting {c['code']}".strip()
            who = _red(c["counterparts"].strip("[]"), red)
            body = f"[{c['start']:%H:%M}] {label}" + (f" with {who}" if who else "")
            body += f" ({c['duration']})" if c["duration"] and c["duration"] != "0:00:00" else ""
            yield SourceItem(source="takeout_meet", external_id=f"meet/{_slug(c['id'])}",
                             title=f"Google Meet {label.lower()}, {c['start']:%Y-%m-%d}", body_text=body,
                             occurred_at=c["start"], mime="text/plain",
                             raw_meta={"product": "Google Meet", "event_id": c["event_id"] or None,
                                       "code": c["code"], "start": c["start"].isoformat(),
                                       "end": c["end"].isoformat() if c["end"] else None, "redactions": red[0]})
        for group, g in sorted(self.chat.items()):
            members = g["members"]
            for day, msgs in sorted(g["days"].items()):
                msgs.sort(key=lambda m_: m_[0])
                red = [0]
                lines = [f"[{at:%H:%M}] {members.get(em, em)}: {_red(_clean_text(text_), red)}"
                         for at, em, text_ in msgs[:MAX_LINES]]
                yield SourceItem(source="takeout_chat", external_id=f"chat/{_slug(group)}/{day}",
                                 title=f"Google Chat, {day}", body_text="\n".join(lines), occurred_at=msgs[0][0],
                                 mime="text/plain",
                                 raw_meta={"product": "Google Chat", "group": group, "day": day,
                                           "participants": sorted(members), "senders": sorted({m_[1] for m_ in msgs}),
                                           "turns": len(msgs), "redactions": red[0]})

    def _bookmark_item(self) -> Iterator[SourceItem]:
        if not self.bookmarks:
            return
        by_folder: dict[str, list[str]] = defaultdict(list)
        red = [0]
        for folder, title, url in self.bookmarks:
            u = clean_url(url)
            by_folder[folder or "(top level)"].append(f"- {_red(title, red) or domain(u)} ({_red(u, red)})")
        body = "\n".join(f"{folder}\n" + "\n".join(lines) for folder, lines in sorted(by_folder.items()))
        yield SourceItem(source="takeout_chrome", external_id="chrome/bookmarks", title="Chrome bookmarks",
                         body_text=body, mime="text/plain",
                         raw_meta={"product": "Chrome", "bookmarks": len(self.bookmarks),
                                   "folders": len(by_folder), "redactions": red[0]})


def parse(paths: list[Path], progress: Callable[[dict], None] | None = None) -> tuple[list[SourceItem], dict]:
    """(items, report). report: parts (counts), notes (things the user should know), redactions, places."""
    imp = _Import(progress)
    with Tree(paths) as members:
        todo = [(m, c) for m in members if (c := classify(m.path)) is not None]
        html = [m for m, (kind, _) in todo if kind == "activity_html"]
        if not todo:
            raise TakeoutError("No Takeout data found. Expected My Activity, Chrome, Maps or Location History "
                               "folders inside the zip.")
        for i, (m, (kind, hint)) in enumerate(todo, 1):
            if progress:
                progress({"file": m.path.rsplit("/", 1)[-1], "files_done": i - 1, "files_total": len(todo)})
            if kind == "activity":
                if _norm_name(hint or "") == "chrome" and any(c and c[0] == "chrome_history" for _, c in todo):
                    imp.note("My Activity > Chrome was skipped because Chrome/History.json covers the same visits.")
                    continue
                imp.read_activity(m, hint)
            elif kind == "chrome_history":
                imp.read_chrome(m)
            elif kind == "chrome_bookmarks":
                imp.read_bookmarks(m)
            elif kind == "chrome_reading_list":
                imp.read_reading_list(m)
            elif kind in ("chat_messages", "chat_group"):
                imp.read_chat(m, hint, kind)
            elif kind == "tasks":
                imp.read_tasks(m)
            elif kind == "meet":
                imp.read_meet(m)
            elif kind == "semantic":
                imp.read_semantic(m)
            elif kind == "timeline":
                imp.read_timeline(m)
            elif kind in ("maps_places", "maps_reviews"):
                imp.read_places(m, hint or "places", kind == "maps_reviews")
            elif kind == "maps_saved_csv":
                imp.read_saved_csv(m, hint or "saved")
            elif kind == "records":
                imp.note("Records.json (raw location points) was skipped: it is huge and adds little over place visits.")
        if imp.counts.get("skipped_noise"):
            imp.note(f"{imp.counts['skipped_noise']} Ads / Google Analytics / Takeout activity entries were skipped.")
        if not any(c and c[0] in ("timeline", "semantic") for _, c in todo):
            imp.note("No Timeline / Location History data in this export (Timeline now lives on your phone). "
                     "Place visits come from Maps (your places) if present.")
        if html:
            imp.note(f"{len(html)} My Activity file(s) are in HTML format and were skipped. Re-export My Activity "
                     "with Multiple formats set to JSON.")
        items = list(imp.items())
    if not items:
        raise TakeoutError("Found Takeout files but nothing readable in them." + (
            " My Activity is in HTML format; re-export it as JSON (Multiple formats, Activity records: JSON)."
            if html else ""))
    places = {p["key"] for vs in imp.visits.values() for p in vs.values()}
    places |= {p["key"] for ps in imp.lists.values() for p in ps}
    by_source: dict[str, int] = defaultdict(int)
    for it in items:
        by_source["gemini" if it.source == "ai_chats" else it.source.removeprefix("takeout_")] += 1
    report = {"parts": dict(by_source), "counts": dict(imp.counts), "notes": imp.notes, "places": len(places),
              "redactions": sum(i.raw_meta.get("redactions", 0) for i in items)}
    return items, report


class TakeoutConnector:
    """Adapter for run_sync. Parses everything first (day-items need the whole export), then emits."""
    name = "takeout"

    def __init__(self, paths: list[Path], progress: Callable[[dict], None] | None = None):
        self.paths, self.progress, self.report = paths, progress, {}

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint]:
        items, self.report = parse(self.paths, self.progress)
        for n, item in enumerate(items, 1):
            if self.progress:
                self.progress({"items": n, "items_total": len(items)})
            yield item
        yield Checkpoint({"items": len(items)})


def run_import(s, paths: list[Path], progress: Callable[..., None] | None = None) -> dict:
    """Import into source_items (the `igneous import takeout` / POST /import/takeout body). Re-running on the
    same export changes nothing: external ids are stable and unchanged items keep their content hash."""
    from igneous.connectors.base import run_sync

    conn = TakeoutConnector(paths, progress)
    stats = run_sync(s, conn, account="default")
    return {**stats, **conn.report}


# ---- locating input ----

def downloads_zips(root: Path | None = None) -> list[Path]:
    root = root or Path("~/Downloads").expanduser()
    return sorted(p for p in root.glob("*") if p.is_file() and re.fullmatch(r"takeout-.*\.zip", p.name, re.I))


def resolve_paths(path: str | list[str] | None, scan_downloads: bool = False) -> list[Path]:
    raw = [path] if isinstance(path, str) else list(path or [])
    paths = [Path(p).expanduser() for p in raw if p]
    if scan_downloads:
        found = downloads_zips()
        if not found and not paths:
            raise TakeoutError("No takeout-*.zip files found in ~/Downloads.")
        paths += found
    if not paths:
        raise TakeoutError("Give a path to a Takeout .zip or folder, upload the zip, or scan Downloads.")
    return paths


def preflight(paths: list[Path]) -> dict:
    """Cheap synchronous check (lists names, reads no JSON): fail fast with a readable reason."""
    with Tree(paths) as members:
        kinds = [c[0] for m in members if (c := classify(m.path))]
    readable = [k for k in kinds if k not in ("activity_html", "records")]
    if not readable:
        raise TakeoutError("No readable Takeout data found." + (
            " My Activity is in HTML format; re-export it as JSON (Multiple formats, Activity records: JSON)."
            if "activity_html" in kinds else " Expected My Activity, Chrome, Maps or Location History folders."))
    return {"files": len(readable)}
