"""LinkedIn data export as a profile item plus one item per connection (#157).

Input: the zip LinkedIn emails you (Settings & Privacy > Data privacy > Get a copy of your data; either the
"larger archive" or the fast "Connections" only), one or several such zips, an extracted folder, or a loose
.csv. Files are matched by base name, case-insensitively, anywhere in the tree. LinkedIn does not document
its CSV headers and they drift, so every column is looked up by normalized name with aliases, a missing file
or column is simply skipped, and the "Notes:" preamble LinkedIn puts above the Connections header is ignored
(the header row is the first row containing a known column name).

Files read: Profile.csv, Positions.csv, Education.csv, Projects.csv, Skills.csv, Certifications.csv,
Honors.csv, Languages.csv, Connections.csv. Never read: messages, ad targeting, login/security history,
Profile.csv's address, birth date and zip code.

Items (source 'linkedin'; stable external ids, so re-importing is a no-op):
  profile              one item: readable body and structured raw_meta (positions, education, projects, skills, ...)
  connection/<id>      one per connection: name, company, position, connected-on, email if present, profile URL;
                       <id> is the /in/<slug> of the profile URL, else a hash of name + connected-on.

Contract with the profile stage (graph/profile.py): raw_meta["profile"] has the same shape that stage's own
extraction produces, so it can consume this item without a model call:
  {"is_about_owner": true, "person_name": str | None,
   "experience": [{"org", "title", "start", "end", "domain_hint"}],
   "education":  [{"school", "degree", "start", "end"}],
   "projects":   [{"name", "description", "org"}]}
Dates are "YYYY-MM" (None when unknown or still ongoing). A year-only date becomes January for a start and
December for an end. domain_hint and a project's org are None: LinkedIn exports carry neither.

Everything stored passes through redact(). The export is read-only input; no credentials are involved or stored.
LinkedIn data stays out of every model call unless named in IGNEOUS_LLM_SOURCES.
"""
import csv
import hashlib
import io
import re
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from igneous.connectors.base import SourceItem
from igneous.connectors.redact import redact

MAX_MEMBER_BYTES = 256 * 1024 * 1024

# file -> column names that identify its header row (normalized)
MARKERS: dict[str, set[str]] = {
    "profile.csv": {"first name", "last name", "headline"},
    "positions.csv": {"company name", "title"},
    "education.csv": {"school name", "school"},
    "projects.csv": {"title", "name"},
    "skills.csv": {"name", "skill"},
    "certifications.csv": {"name", "authority"},
    "honors.csv": {"title", "name"},
    "languages.csv": {"name", "language"},
    "connections.csv": {"first name", "last name", "url"},
}


class LinkedInError(ValueError):
    pass


def norm(h: str) -> str:
    return re.sub(r"[\s_]+", " ", h.replace("﻿", "")).strip().lower()


def pick(row: dict[str, str], *names: str) -> str:
    for n in names:
        v = row.get(n)
        if v and v.strip():
            return v.strip()
    return ""


# ---- reading the export ----

class Export:
    """The CSV files of one or more zips / folders, by lowercase base name."""

    def __init__(self, paths: list[Path]):
        self.files: dict[str, Callable[[], bytes]] = {}
        for p in paths:
            if p.is_dir():
                for f in sorted(p.rglob("*.csv")):
                    self._add(f.name, lambda f=f: f.read_bytes(), f.stat().st_size)
            elif zipfile.is_zipfile(p):
                zf = zipfile.ZipFile(p)
                for info in zf.infolist():
                    if info.filename.lower().endswith(".csv") and not info.is_dir():
                        self._add(Path(info.filename).name, lambda zf=zf, i=info: zf.read(i), info.file_size)
            elif p.suffix.lower() == ".csv" and p.is_file():
                self._add(p.name, p.read_bytes, p.stat().st_size)
            else:
                raise LinkedInError(f"{p.name} is not a zip, folder or .csv file.")

    def _add(self, name: str, opener: Callable[[], bytes], size: int) -> None:
        name = name.lower()
        if name in MARKERS and name not in self.files and size <= MAX_MEMBER_BYTES:
            self.files[name] = opener

    def rows(self, name: str) -> list[dict[str, str]]:
        """Data rows of one file as {normalized header: value}; [] if the file or its header is missing."""
        opener = self.files.get(name)
        if opener is None:
            return []
        raw = opener()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        table = list(csv.reader(io.StringIO(text)))
        markers = MARKERS[name]
        head = next((i for i, r in enumerate(table) if markers & {norm(c) for c in r}), None)
        if head is None:
            return []
        cols = [norm(c) for c in table[head]]
        out = []
        for r in table[head + 1:]:
            if not any(c.strip() for c in r):
                continue
            out.append({c: r[i] for i, c in enumerate(cols) if c and i < len(r)})
        return out


# ---- dates ----

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], 1)}


def parse_date(s: str) -> tuple[int, int | None, int | None] | None:
    """(year, month, day); month/day None when the text has none. Accepts 'Jun 2025', '2025', '2025-06',
    '06/2025', '09 Mar 2024', 'Mar 9, 2024', '2024-03-09'."""
    s = (s or "").strip().replace(",", " ")
    if not s:
        return None
    if m := re.fullmatch(r"(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?(?:[T ].*)?", s):
        y, mo, d = int(m[1]), m[2], m[3]
        return (y, int(mo) if mo else None, int(d) if d else None) if not mo or 1 <= int(mo) <= 12 else None
    if m := re.fullmatch(r"(\d{1,2})/(\d{4})", s):
        return (int(m[2]), int(m[1]), None) if 1 <= int(m[1]) <= 12 else None
    toks = s.split()
    mon = next((MONTHS[t[:3].lower()] for t in toks if t[:3].lower() in MONTHS and t.isalpha()), None)
    nums = [int(t) for t in toks if t.isdigit()]
    year = next((n for n in nums if n >= 1900), None)
    if mon is None or year is None:
        return (year, None, None) if year and len(toks) == 1 else None
    day = next((n for n in nums if 1 <= n <= 31 and n != year), None)
    return year, mon, day


def month_str(s: str, end: bool = False) -> str | None:
    d = parse_date(s)
    if not d:
        return None
    y, mo, _ = d
    return f"{y:04d}-{mo or (12 if end else 1):02d}"


def day_dt(s: str) -> datetime | None:
    d = parse_date(s)
    if not d:
        return None
    y, mo, dd = d
    try:
        return datetime(y, mo or 1, dd or 1, tzinfo=UTC)
    except ValueError:
        return None


# ---- items ----

def _clean(text: str, counts: dict[str, int]) -> str:
    out, c = redact(text)
    for k, v in c.items():
        counts[k] = counts.get(k, 0) + v
    return (out or "").strip()


def profile_item(x: Export, counts: dict[str, int]) -> SourceItem | None:
    prof = (x.rows("profile.csv") or [{}])[0]
    name = " ".join(p for p in (pick(prof, "first name"), pick(prof, "last name")) if p) or None
    positions, education, projects = [], [], []
    for r in x.rows("positions.csv"):
        org, title = pick(r, "company name", "company", "organization"), pick(r, "title", "position")
        if org or title:
            positions.append({
                "org": _clean(org, counts), "title": _clean(title, counts),
                "description": _clean(pick(r, "description"), counts), "location": _clean(pick(r, "location"), counts),
                "start": month_str(pick(r, "started on", "start date", "start")),
                "end": month_str(pick(r, "finished on", "end date", "end"), end=True)})
    for r in x.rows("education.csv"):
        school = pick(r, "school name", "school")
        if school:
            education.append({"school": _clean(school, counts), "degree": _clean(pick(r, "degree name", "degree"), counts),
                              "start": month_str(pick(r, "start date", "started on", "start")),
                              "end": month_str(pick(r, "end date", "finished on", "end"), end=True)})
    for r in x.rows("projects.csv"):
        pname = pick(r, "title", "name")
        if pname:
            projects.append({"name": _clean(pname, counts), "description": _clean(pick(r, "description"), counts),
                             "url": pick(r, "url"), "start": month_str(pick(r, "started on", "start date")),
                             "end": month_str(pick(r, "finished on", "end date"), end=True)})
    skills = [_clean(pick(r, "name", "skill"), counts) for r in x.rows("skills.csv")]
    certs = [{"name": _clean(pick(r, "name"), counts), "authority": _clean(pick(r, "authority"), counts),
              "start": month_str(pick(r, "started on", "start date")), "end": month_str(pick(r, "finished on", "end date"), end=True)}
             for r in x.rows("certifications.csv") if pick(r, "name")]
    honors = [{"title": _clean(pick(r, "title", "name"), counts), "description": _clean(pick(r, "description"), counts),
               "issued": month_str(pick(r, "issued on", "date"))} for r in x.rows("honors.csv") if pick(r, "title", "name")]
    languages = [{"name": _clean(pick(r, "name", "language"), counts), "proficiency": pick(r, "proficiency")}
                 for r in x.rows("languages.csv") if pick(r, "name", "language")]
    skills = [s for s in skills if s]
    headline, summary, industry = (_clean(pick(prof, k), counts) for k in ("headline", "summary", "industry"))
    if not (name or positions or education or projects or skills or headline or summary):
        return None

    lines = [f"LinkedIn profile of {name}" if name else "LinkedIn profile"]
    if headline:
        lines.append(headline)
    if industry:
        lines.append(f"Industry: {industry}")
    if summary:
        lines += ["", "Summary", summary]
    if positions:
        lines += ["", "Experience"]
        for p in positions:
            span = f" ({p['start'] or '?'} to {p['end'] or 'present'})"
            lines.append(f"- {p['title'] or 'Role'} at {p['org'] or '?'}{span}" + (f", {p['location']}" if p["location"] else ""))
            if p["description"]:
                lines.append(f"  {p['description']}")
    if education:
        lines += ["", "Education"]
        lines += [f"- {e['school']}" + (f", {e['degree']}" if e["degree"] else "")
                  + f" ({e['start'] or '?'} to {e['end'] or '?'})" for e in education]
    if projects:
        lines += ["", "Projects"]
        for p in projects:
            lines.append(f"- {p['name']}" + (f": {p['description']}" if p["description"] else ""))
    if skills:
        lines += ["", "Skills: " + ", ".join(skills)]
    if certs:
        lines += ["", "Certifications: " + ", ".join(c["name"] for c in certs)]
    if honors:
        lines += ["", "Honors: " + ", ".join(h["title"] for h in honors)]
    if languages:
        lines += ["", "Languages: " + ", ".join(lang["name"] for lang in languages)]

    meta = {
        "kind": "profile", "person_name": name, "headline": headline, "industry": industry,
        "positions": positions, "education": education, "projects": projects, "skills": skills,
        "certifications": certs, "honors": honors, "languages": languages,
        "profile": {
            "is_about_owner": True, "person_name": name,
            "experience": [{"org": p["org"], "title": p["title"], "start": p["start"], "end": p["end"], "domain_hint": None}
                           for p in positions],
            "education": [{"school": e["school"], "degree": e["degree"], "start": e["start"], "end": e["end"]}
                          for e in education],
            "projects": [{"name": p["name"], "description": p["description"], "org": None} for p in projects],
        },
    }
    return SourceItem(source="linkedin", external_id="profile", title=f"LinkedIn profile: {name}" if name else "LinkedIn profile",
                      body_text="\n".join(lines), mime="text/plain", raw_meta=meta)


def profile_slug(url: str) -> str | None:
    if not url:
        return None
    parts = [p for p in unquote(urlparse(url if "//" in url else "//" + url).path).split("/") if p]
    if "in" in parts and parts.index("in") + 1 < len(parts):
        return parts[parts.index("in") + 1].lower()
    return parts[-1].lower() if parts else None


def connection_items(x: Export, counts: dict[str, int]) -> Iterator[SourceItem]:
    seen: set[str] = set()
    for r in x.rows("connections.csv"):
        first, last = pick(r, "first name"), pick(r, "last name")
        name = _clean(" ".join(p for p in (first, last) if p), counts)
        url = pick(r, "url", "profile url")
        connected = pick(r, "connected on", "connected")
        iso = day_dt(connected)
        slug = profile_slug(url)
        if not name and not slug:
            continue
        cid = slug or "h-" + hashlib.sha1(f"{name.lower()}|{iso.date() if iso else ''}".encode()).hexdigest()[:16]
        if cid in seen:
            continue
        seen.add(cid)
        email = pick(r, "email address", "email").lower()
        email = email if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) else ""
        company, position = _clean(pick(r, "company"), counts), _clean(pick(r, "position", "title"), counts)
        body = f"{name}" + (f", {position}" if position else "") + (f" at {company}" if company else "")
        body += f". Connected on {iso.date()}." if iso else "."
        yield SourceItem(
            source="linkedin", external_id=f"connection/{cid}", title=name or cid, body_text=body, mime="text/plain",
            occurred_at=iso,
            raw_meta={"kind": "connection", "name": name, "company": company, "position": position,
                      "connected_on": iso.date().isoformat() if iso else None, "email": email or None,
                      "url": url or None})


def find_items(paths: list[Path]) -> tuple[list[SourceItem], dict]:
    x = Export(paths)
    counts: dict[str, int] = {}
    items: list[SourceItem] = []
    if p := profile_item(x, counts):
        items.append(p)
    conns = list(connection_items(x, counts))
    items += conns
    report = {"profile": int(p is not None), "connections": len(conns),
              "positions": len(p.raw_meta["positions"]) if p else 0,
              "education": len(p.raw_meta["education"]) if p else 0,
              "files": sorted(x.files), "redactions": sum(counts.values())}
    return items, report


class LinkedInConnector:
    name = "linkedin"

    def __init__(self, paths: list[Path], progress: Callable[..., None] | None = None):
        self.paths, self.progress, self.report = paths, progress, {}

    def sync(self, cursor: dict):
        items, self.report = find_items(self.paths)
        for i, item in enumerate(items, 1):
            if self.progress:
                self.progress({"items": i, "items_total": len(items)})
            yield item


def preflight(paths: list[Path]) -> dict:
    """Cheap synchronous check: fail fast with a readable reason when the input has nothing to read."""
    x = Export(paths)
    if not x.files:
        raise LinkedInError("No LinkedIn data found. Expected Connections.csv, Positions.csv or Profile.csv in the "
                            "zip or folder.")
    return {"files": sorted(x.files)}


def run_import(s, paths: list[Path], progress: Callable[..., None] | None = None) -> dict:
    """Body of `igneous import linkedin` and POST /import/linkedin. Re-running on the same export changes
    nothing: external ids are stable and unchanged items keep their content hash."""
    from igneous.connectors.base import run_sync

    conn = LinkedInConnector(paths, progress)
    stats = run_sync(s, conn, account="default")
    return {**stats, **conn.report}


def resolve_paths(path: str | list[str] | None) -> list[Path]:
    raw = [path] if isinstance(path, str) else list(path or [])
    paths = [Path(p).expanduser() for p in raw if p]
    if not paths:
        raise LinkedInError("Give a path to the LinkedIn export (.zip or folder), or upload the zip.")
    if missing := next((p for p in paths if not p.exists()), None):
        raise LinkedInError(f"Not found: {missing}")
    return paths
