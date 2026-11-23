"""Read-only GitHub connector for repositories, issues/PRs, READMEs, and daily commit activity.

The first sync reads every repository the token can see; later syncs read what changed since the last complete
one: issues and pull requests through `/issues?since` (one listing covers both), their comments when they have
any, READMEs of repositories pushed since, and the commit days touched since. The cursor: `synced_at` (when the
last complete scan began), `repos` (the names it listed) and `repos_etag`, all saved only when a scan completes.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import chain
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem, Tombstone

API = "https://api.github.com"
CHECKPOINT_EVERY = 50
PER_PAGE = 100
OVERLAP = timedelta(minutes=5)  # GitHub's `since` compares its own clock with ours


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) if value else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class GithubError(RuntimeError):
    """A GitHub failure; the message says what to do about it."""


def _error(e: HTTPError, path: str) -> GithubError:
    headers = e.headers or {}
    try:
        message = str(json.loads(e.read() or b"{}").get("message", ""))
    except (ValueError, AttributeError, OSError):
        message = ""
    if e.code == 401:
        return GithubError("GitHub rejected the token: it is wrong, expired or revoked. Save a new read-only token.")
    if e.code in (403, 429) and headers.get("X-RateLimit-Remaining") == "0":
        reset = headers.get("X-RateLimit-Reset")
        until = f" until {datetime.fromtimestamp(int(reset), UTC):%H:%M} UTC" if reset and reset.isdigit() else ""
        return GithubError(f"GitHub's rate limit for this token is used up{until}. Sync again after that.")
    if e.code == 429 or headers.get("Retry-After") or "rate limit" in message.lower():
        return GithubError("GitHub asked Igneous to slow down (secondary rate limit). Sync again in a few minutes.")
    if e.code == 403:
        return GithubError(f"GitHub refused {path}: the token can't read it. Give it read access to Contents, "
                           "Issues and Pull requests.")
    return GithubError(f"GitHub GET {path} failed ({e.code}).")


class GithubClient:
    """Small GET-only REST client. Keeping this local makes write requests impossible by construction."""

    def __init__(self, token: str, opener=urlopen):
        self.token, self.opener = token, opener

    def _open(self, url: str, path: str, etag: str | None = None, missing: tuple[int, ...] = ()):
        """(data, response headers); data is None for 304 Not Modified and for a status in `missing`."""
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "igneous-readonly"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            with self.opener(Request(url, headers=headers, method="GET"), timeout=30) as response:
                return json.load(response), response.headers
        except HTTPError as e:
            if e.code == 304 or e.code in missing:
                return None, e.headers or {}
            raise _error(e, path) from e

    def get(self, path: str, params: dict | None = None, etag: str | None = None,
            missing: tuple[int, ...] = ()) -> tuple[object | None, str | None, str | None]:
        """(data, ETag, Link). Data is None when the resource is unchanged since `etag` or its status is in
        `missing` (a repository without a README, say)."""
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        data, headers = self._open(f"{API}{path}{query}", path, etag, missing)
        if data is None and etag:
            return None, etag, None
        return data, headers.get("ETag"), headers.get("Link")

    def pages(self, path: str, params: dict | None = None, etag: str | None = None,
              missing: tuple[int, ...] = ()) -> tuple[list[dict] | None, str | None]:
        """Every page of a collection, and the first page's ETag. None when the first page is unchanged since
        `etag` or its status is in `missing`."""
        data, response_etag, link = self.get(path, params, etag, missing)
        if data is None:
            return None, response_etag
        out = list(data) if isinstance(data, list) else []
        while link and 'rel="next"' in link:
            next_url = next(part[part.find("<") + 1:part.find(">")] for part in link.split(",") if 'rel="next"' in part)
            # GitHub's Link URL is authoritative pagination state; it remains a GET request.
            page, headers = self._open(next_url, path)
            out.extend(page if isinstance(page, list) else [])
            link = headers.get("Link")
        return out, response_etag


def account_for(token: str, client: GithubClient | None = None) -> str:
    """The login a token belongs to: checks that it looks like a token and that GitHub accepts it."""
    token = token.strip()
    if len(token) < 20 or any(c.isspace() for c in token):
        raise GithubError("That does not look like a GitHub token.")
    profile, _, _ = (client or GithubClient(token)).get("/user")
    login = profile.get("login") if isinstance(profile, dict) else None
    if not login:
        raise GithubError("GitHub did not return an account for that token.")
    return login


def _commit_date(commit: dict) -> str:
    return ((commit.get("commit") or {}).get("author") or {}).get("date") or ""


class GithubConnector:
    name = "github"

    def __init__(self, token: str, account: str, client: GithubClient | None = None):
        self.account, self.client = account, client or GithubClient(token)

    def _item(self, kind: str, repo: str, record: dict, comments: list[dict] | None = None) -> SourceItem:
        number = record.get("number")
        title = f"{repo} #{number}: {record.get('title', '')}" if number else f"{repo}: {record.get('name', repo)}"
        author = record.get("user") or record.get("author") or {}
        parts = [record.get("body") or ""]
        for comment in comments or []:
            who = (comment.get("user") or {}).get("login", "unknown")
            parts.append(f"\n\n{who}:\n{comment.get('body') or ''}")
        return SourceItem(source=self.name, external_id=f"{self.account}/{repo}/{kind}/{number or record.get('sha') or kind}",
                          title=title, body_text="".join(parts).strip(), occurred_at=_when(record.get("updated_at") or record.get("date")),
                          mime="text/markdown", raw_meta={"kind": kind, "account": self.account, "repo": repo,
                              "repo_url": record.get("html_url"), "number": number, "url": record.get("html_url"),
                              "author": {"login": author.get("login"), "email": (record.get("commit") or {}).get("author", {}).get("email")},
                              "comments": [{"login": (c.get("user") or {}).get("login"), "created_at": c.get("created_at")} for c in comments or []]})

    def _readme(self, repo: dict) -> SourceItem | None:
        name = repo["full_name"]
        readme, _, _ = self.client.get(f"/repos/{name}/readme", {"ref": repo.get("default_branch") or "HEAD"},
                                       missing=(404,))
        if not isinstance(readme, dict):
            return None
        body = base64.b64decode(readme.get("content", "")).decode(errors="replace") \
            if readme.get("encoding") == "base64" else ""
        return SourceItem(self.name, f"{self.account}/{name}/readme", f"{name}: README", body, _when(repo.get("updated_at")),
                          "text/markdown", {"kind": "readme", "account": self.account, "repo": name, "url": readme.get("html_url")})

    def _discussions(self, repo: dict, since: str | None) -> Iterator[SourceItem]:
        """Issues and pull requests updated since `since` (all of them without it), each with its comments."""
        name = repo["full_name"]
        records = None
        if repo.get("has_issues") is not False:
            params = {"state": "all", "per_page": PER_PAGE, "sort": "updated", "direction": "asc",
                      **({"since": since} if since else {})}
            records, _ = self.client.pages(f"/repos/{name}/issues", params, missing=(410,))
        if records is None:
            # Issues are turned off for the repository (410), and /issues lists no pull requests then. /pulls
            # has no `since`: list them all and keep the recent ones.
            cutoff = _when(since)
            pulls, _ = self.client.pages(f"/repos/{name}/pulls", {"state": "all", "per_page": PER_PAGE})
            records = [{**p, "pull_request": {}} for p in pulls or []
                       if cutoff is None or (_when(p.get("updated_at")) or cutoff) >= cutoff]
        for record in records:
            comments = []
            if record.get("comments", 1):  # a pull request listed by /pulls has no count: look
                comments, _ = self.client.pages(f"/repos/{name}/issues/{record['number']}/comments",
                                                {"per_page": PER_PAGE})
            yield self._item("pr" if "pull_request" in record else "issue", name, record, comments)

    def _commit_days(self, name: str, since: str | None) -> Iterator[SourceItem]:
        """One item per UTC day with commits since `since`, each holding that whole day's commits."""
        params = {"per_page": PER_PAGE, **({"since": since} if since else {})}
        commits, _ = self.client.pages(f"/repos/{name}/commits", params, missing=(409,))  # 409: an empty repository
        days = sorted({_commit_date(c)[:10] for c in commits or []} - {""})
        for day in days:
            if since:
                # An incremental listing may hold only part of a day: re-read the whole day, so its item never
                # replaces earlier commits with a partial set.
                start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
                group, _ = self.client.pages(f"/repos/{name}/commits", {
                    "since": _iso(start), "until": _iso(start + timedelta(days=1)), "per_page": PER_PAGE})
            else:
                group = [c for c in commits or [] if _commit_date(c).startswith(day)]
            if not group:
                continue
            first = group[0]
            lines = [f"{c['sha'][:7]} {next(iter(((c.get('commit') or {}).get('message') or '').splitlines()), '')}"
                     for c in group]
            author = (first.get("author") or {}).get("login")
            email = ((first.get("commit") or {}).get("author") or {}).get("email")
            yield SourceItem(self.name, f"{self.account}/{name}/commits/{day}", f"{name}: {len(group)} commits on {day}",
                             "\n".join(lines), _when(_commit_date(first)), "text/plain",
                             {"kind": "commits", "account": self.account, "repo": name, "day": day,
                              "author": {"login": author, "email": email},
                              "commits": [{"sha": c["sha"], "author": (c.get("author") or {}).get("login"),
                                           "email": ((c.get("commit") or {}).get("author") or {}).get("email")}
                                          for c in group]})

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan | Tombstone]:
        # Advance only to the instant this scan began. An endpoint listed later cannot miss a mutation that
        # happened during a long scan on the next invocation.
        watermark = _iso(datetime.now(UTC))
        full = not cursor.get("synced_at")
        since = None if full else _iso(_when(cursor["synced_at"]) - OVERLAP)
        # The ETag covers the first page only: use it when the whole list fit on one.
        etag = cursor.get("repos_etag") if not full and len(cursor.get("repos", [])) < PER_PAGE else None
        repos, new_etag = self.client.pages("/user/repos", {"affiliation": "owner,collaborator", "sort": "updated",
                                                            "per_page": PER_PAGE}, etag)
        listed = repos is not None
        if not listed:  # 304: the same repositories as last time, and none pushed since
            repos, new_etag = [{"full_name": n} for n in cursor.get("repos", [])], etag
        names = {repo["full_name"] for repo in repos}
        if listed:
            for missing in sorted(set(cursor.get("repos", [])) - names):
                yield Tombstone(self.name, f"{self.account}/{missing}", prefix=True)
        observed, emitted = set(), 0
        for repo in repos:
            name = repo["full_name"]
            found = [self._item("repo", name, repo)] if listed else []
            pushed = _when(repo.get("pushed_at"))
            if listed and (full or pushed is None or pushed >= _when(since)):  # a README changes with a push
                found += [r for r in [self._readme(repo)] if r]
            for item in chain(found, self._discussions(repo, since), self._commit_days(name, since)):
                observed.add(item.external_id)
                emitted += 1
                yield item
                if emitted % CHECKPOINT_EVERY == 0:
                    yield Checkpoint(cursor)  # commits the items; the cursor moves only when the scan completes
        next_cursor = {"repos_etag": new_etag, "repos": sorted(names), "synced_at": watermark}
        if full:
            yield CompletedScan(next_cursor, self.name, frozenset(observed), scopes=(self.account,))
        else:
            yield Checkpoint(next_cursor)
