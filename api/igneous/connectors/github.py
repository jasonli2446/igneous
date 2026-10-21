"""Read-only GitHub connector for repositories, issues/PRs, READMEs, and daily commit activity."""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from igneous.connectors.base import Checkpoint, CompletedScan, SourceItem, Tombstone

API = "https://api.github.com"
CHECKPOINT_EVERY = 50


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC) if value else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class GithubError(RuntimeError):
    pass


class GithubClient:
    """Small GET-only REST client. Keeping this local makes write requests impossible by construction."""

    def __init__(self, token: str, opener=urlopen):
        self.token, self.opener = token, opener

    def get(self, path: str, params: dict | None = None, etag: str | None = None,
            allow_not_found: bool = False) -> tuple[object | None, str | None, str | None]:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "igneous-readonly"}
        if etag:
            headers["If-None-Match"] = etag
        request = Request(f"{API}{path}{query}", headers=headers, method="GET")
        try:
            with self.opener(request, timeout=30) as response:
                return json.load(response), response.headers.get("ETag"), response.headers.get("Link")
        except HTTPError as e:
            if e.code == 304:
                return None, etag, None
            if e.code == 404 and allow_not_found:
                return None, None, None
            if e.code in (401, 403):
                raise GithubError("GitHub rejected the token or its read permissions. Save a read-only token and try again.") from e
            raise GithubError(f"GitHub GET {path} failed ({e.code}).") from e

    def pages(self, path: str, params: dict | None = None, etag: str | None = None) -> tuple[list[dict] | None, str | None]:
        """GET paginated data. Conditional requests are used only for one-page collection requests."""
        data, response_etag, link = self.get(path, params, etag)
        if data is None:
            return None, response_etag
        out = list(data) if isinstance(data, list) else []
        while link and 'rel="next"' in link:
            next_url = next(part[part.find("<") + 1:part.find(">")]
                            for part in link.split(",") if 'rel="next"' in part)
            # GitHub's Link URL is authoritative pagination state; it remains a GET request.
            request = Request(next_url, headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self.token}",
                                                  "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "igneous-readonly"}, method="GET")
            with self.opener(request, timeout=30) as response:
                page = json.load(response)
                out.extend(page if isinstance(page, list) else [])
                link = response.headers.get("Link")
        return out, response_etag


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

    def _repo_items(self, repo: dict, since: str | None) -> Iterator[SourceItem]:
        name = repo["full_name"]
        yield self._item("repo", name, repo)
        readme, _, _ = self.client.get(f"/repos/{name}/readme", {"ref": repo.get("default_branch", "HEAD")},
                                           allow_not_found=True)
        if isinstance(readme, dict):
            # The contents API returns base64, but download_url is a public/private raw GET endpoint; use its decoded content only when provided.
            import base64
            body = base64.b64decode(readme.get("content", "")).decode(errors="replace") if readme.get("encoding") == "base64" else ""
            yield SourceItem(self.name, f"{self.account}/{name}/readme", f"{name}: README", body, _when(repo.get("updated_at")),
                             "text/markdown", {"kind": "readme", "account": self.account, "repo": name, "url": readme.get("html_url")})
        for kind, endpoint in (("issue", "issues"), ("pr", "pulls")):
            params = {"state": "all", "per_page": 100, "sort": "updated", "direction": "asc"}
            if since:
                params["since"] = since
            records, _ = self.client.pages(f"/repos/{name}/{endpoint}", params)
            for record in records or []:
                if kind == "issue" and record.get("pull_request"):
                    continue
                comments, _ = self.client.pages(f"/repos/{name}/issues/{record['number']}/comments", {"per_page": 100})
                yield self._item(kind, name, record, comments)
        commits, _ = self.client.pages(f"/repos/{name}/commits", {"since": since, "per_page": 100} if since else {"per_page": 100})
        days = {((c.get("commit") or {}).get("author") or {}).get("date", "")[:10] for c in commits or []}
        # An incremental result may include only a few commits from a day. Re-fetch each affected
        # UTC day so its stable daily SourceItem never overwrites earlier commits with a partial set.
        groups: dict[str, list[dict]] = {}
        for day in days - {""}:
            if since:
                start = datetime.fromisoformat(f"{day}T00:00:00+00:00")
                group, _ = self.client.pages(f"/repos/{name}/commits", {"since": _iso(start), "until": _iso(start + timedelta(days=1)), "per_page": 100})
            else:
                group = [c for c in commits or [] if ((c.get("commit") or {}).get("author") or {}).get("date", "").startswith(day)]
            groups[day] = group or []
        for day, group in groups.items():
            first = group[0]
            lines = [f"{c['sha'][:7]} {(c.get('commit') or {}).get('message', '').splitlines()[0]}" for c in group]
            author = (first.get("author") or {}).get("login")
            email = ((first.get("commit") or {}).get("author") or {}).get("email")
            yield SourceItem(self.name, f"{self.account}/{name}/commits/{day}", f"{name}: {len(group)} commits on {day}", "\n".join(lines),
                             _when(((first.get("commit") or {}).get("author") or {}).get("date")), "text/plain",
                             {"kind": "commits", "account": self.account, "repo": name, "day": day,
                              "author": {"login": author, "email": email}, "commits": [{"sha": c["sha"], "author": (c.get("author") or {}).get("login"), "email": ((c.get("commit") or {}).get("author") or {}).get("email")} for c in group]})

    def sync(self, cursor: dict) -> Iterator[SourceItem | Checkpoint | CompletedScan | Tombstone]:
        # Advance only to the instant this scan began. An endpoint listed later cannot
        # miss a mutation that happened during a long scan on the next invocation.
        watermark = _iso(datetime.now(UTC))
        repos, etag = self.client.pages("/user/repos", {"affiliation": "owner,collaborator", "sort": "updated", "per_page": 100}, cursor.get("repos_etag"))
        if repos is None:
            yield Checkpoint(cursor)
            return
        full_scan = not cursor.get("synced_at")
        since = None if full_scan else _iso(_when(cursor["synced_at"]) - timedelta(minutes=5))
        observed, emitted = set(), 0
        repo_names = {repo["full_name"] for repo in repos}
        for missing in set(cursor.get("repos", [])) - repo_names:
            yield Tombstone(self.name, f"{self.account}/{missing}/", prefix=True)
        for repo in repos:
            for item in self._repo_items(repo, since):
                observed.add(item.external_id)
                emitted += 1
                yield item
                if emitted % CHECKPOINT_EVERY == 0:
                    yield Checkpoint({**cursor, "repos_etag": etag})
        next_cursor = {"repos_etag": etag, "repos": sorted(repo_names), "synced_at": watermark}
        if full_scan:
            yield CompletedScan(next_cursor, self.name, frozenset(observed), external_id_prefix=f"{self.account}/")
        else:
            yield Checkpoint(next_cursor)
