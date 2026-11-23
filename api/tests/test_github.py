"""GitHub (connectors/github.py) through the real GET client, against a fake API. No network."""
import io
from urllib.error import HTTPError

import pytest
from fakes import FakeGitHub
from sqlalchemy import text

from igneous.connectors import github
from igneous.connectors.base import CompletedScan, SourceItem, load_cursor, run_sync
from igneous.connectors.github import GithubClient, GithubConnector, GithubError, account_for

REPO = {"full_name": "me/repo", "name": "repo", "updated_at": "2024-01-02T00:00:00Z",
        "pushed_at": "2024-01-02T00:00:00Z", "default_branch": "main", "has_issues": True}
ISSUE = {"number": 3, "title": "Bug", "body": "details", "updated_at": "2024-01-02T00:00:00Z",
         "user": {"login": "me"}, "comments": 1}
PULL = {"number": 4, "title": "Fix", "body": "done", "updated_at": "2024-01-02T00:00:00Z", "user": {"login": "me"},
        "comments": 0, "pull_request": {"url": "x"}}
COMMIT = {"sha": "abcdef0123", "commit": {"message": "ship it\n\nlonger text",
                                          "author": {"date": "2024-01-02T12:00:00Z", "email": "me@example.com"}},
          "author": {"login": "me"}}


def api(**routes) -> FakeGitHub:
    return FakeGitHub({"/user/repos": [REPO], "/repos/me/repo/readme": {"encoding": "base64", "content": "IyBSRUFETUU="},
                       "/repos/me/repo/issues": [ISSUE, PULL],
                       "/repos/me/repo/issues/3/comments": [{"body": "looks good", "user": {"login": "reviewer"},
                                                              "created_at": "2024-01-02T00:00:00Z"}],
                       "/repos/me/repo/commits": [COMMIT], **routes})


def connector(fake: FakeGitHub) -> GithubConnector:
    return GithubConnector("token", "me", GithubClient("token", opener=fake))


def stored(s) -> set[str]:
    return set(s.execute(text("SELECT external_id FROM source_items WHERE source = 'github'")).scalars())


def test_lists_issues_and_pull_requests_in_one_listing_and_groups_commits_by_day():
    fake = api()
    events = list(connector(fake).sync({}))
    items = [e for e in events if isinstance(e, SourceItem)]
    assert {i.external_id for i in items} == {"me/me/repo/repo/repo", "me/me/repo/readme", "me/me/repo/issue/3",
                                              "me/me/repo/pr/4", "me/me/repo/commits/2024-01-02"}
    assert any("reviewer:\nlooks good" in (i.body_text or "") for i in items)
    assert next(i for i in items if i.raw_meta["kind"] == "commits").body_text == "abcdef0 ship it"
    assert "/repos/me/repo/pulls" not in fake.paths()  # /issues already lists pull requests
    assert "/repos/me/repo/issues/4/comments" not in fake.paths()  # it has none
    assert isinstance(events[-1], CompletedScan) and events[-1].scopes == ("me",)


def test_an_interrupted_first_sync_never_saves_the_etag(db, monkeypatch):
    monkeypatch.setattr(github, "CHECKPOINT_EVERY", 1)
    fake = api()
    run_sync(db, connector(fake), account="me", limit=1)
    assert "repos_etag" not in load_cursor(db, "github", "me")
    run_sync(db, connector(fake), account="me")
    # The next sync lists the repositories again instead of getting 304 and stopping, and finishes the scan.
    assert [sent for path, _, sent in fake.calls if path == "/user/repos"] == [None, None]
    assert len(stored(db)) == 5 and load_cursor(db, "github", "me")["repos_etag"] == fake.etag


def test_an_unchanged_repository_list_still_syncs_new_issues():
    fake = api(**{"/repos/me/repo/issues": lambda q: [{**ISSUE, "number": 9, "comments": 0}] if "since" in q else []})
    cursor = {"synced_at": "2024-01-01T00:00:00Z", "repos": ["me/repo"], "repos_etag": fake.etag}
    events = list(connector(fake).sync(cursor))
    assert [e.external_id for e in events if isinstance(e, SourceItem)] == ["me/me/repo/issue/9", "me/me/repo/commits/2024-01-02"]
    assert ("/repos/me/repo/issues", {"state": "all", "per_page": "100", "sort": "updated", "direction": "asc",
                                      "since": "2023-12-31T23:55:00Z"}, None) in fake.calls
    assert events[-1].cursor["repos_etag"] == fake.etag and events[-1].cursor["repos"] == ["me/repo"]


def test_a_repository_without_issues_reads_its_pull_requests_and_an_empty_one_has_no_commits():
    fake = api(**{"/user/repos": [{**REPO, "has_issues": False}], "/repos/me/repo/pulls": [{**PULL, "comments": None}],
                  "/repos/me/repo/issues/4/comments": [], "/repos/me/repo/commits": 409})
    items = [e for e in connector(fake).sync({}) if isinstance(e, SourceItem)]
    assert {i.external_id for i in items} == {"me/me/repo/repo/repo", "me/me/repo/readme", "me/me/repo/pr/4"}


def test_empty_commit_messages_are_kept():
    fake = api(**{"/repos/me/repo/commits": [{**COMMIT, "commit": {**COMMIT["commit"], "message": ""}}]})
    days = [e for e in connector(fake).sync({}) if isinstance(e, SourceItem) and e.raw_meta["kind"] == "commits"]
    assert days[0].body_text == "abcdef0 "


def _refusing(status: int, headers: dict):
    def opener(request, timeout=30):
        raise HTTPError(request.full_url, status, "error", headers, io.BytesIO(b'{"message": "denied"}'))
    return opener


def test_rate_limits_are_told_apart_from_a_rejected_token():
    def message(status, headers=None):
        with pytest.raises(GithubError) as e:
            GithubClient("token", opener=_refusing(status, headers or {})).get("/user")
        return str(e.value)

    assert "rejected the token" in message(401)
    assert "rate limit for this token is used up until 00:16 UTC" in message(
        403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000"})
    assert "slow down" in message(403, {"Retry-After": "60"})
    assert "can't read it" in message(403)


def test_a_token_is_checked_before_it_is_stored():
    with pytest.raises(GithubError, match="does not look like"):
        account_for("short")
    assert account_for(" github_pat_" + "a" * 30 + " ", GithubClient("x", opener=FakeGitHub({"/user": {"login": "me"}}))) == "me"
    with pytest.raises(GithubError, match="did not return an account"):
        account_for("github_pat_" + "a" * 30, GithubClient("x", opener=FakeGitHub({"/user": {}})))
