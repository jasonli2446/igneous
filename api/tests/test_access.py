"""The API answers only local callers: the web app's origin and the API's own pages, by a local host name (#209)."""
import pytest
from fastapi.testclient import TestClient

from igneous import access, main

WEB = "http://localhost:3000"
API = "http://localhost:8000"


@pytest.fixture
def client(test_db, monkeypatch):
    monkeypatch.setattr(access.settings, "web_url", WEB)
    started = []
    monkeypatch.setattr(main, "_start_job", lambda *args, **kwargs: started.append(args) or {"id": "job"})
    c = TestClient(main.app, base_url=API)
    c.started = started
    return c


def test_local_host_names_are_answered_and_others_refused(client, monkeypatch):
    for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000", "localhost"):
        assert client.get("/health", headers={"host": host}).status_code == 200, host
    # DNS rebinding: a site pointing its own name at 127.0.0.1 still names its own host.
    for host in ("evil.example", "evil.example:8000", "localhost.evil.example", "127.0.0.1.evil.example"):
        response = client.get("/health", headers={"host": host})
        assert response.status_code == 400 and "localhost" in response.text, host
    # The web app's own host name is the one other name the API answers to.
    monkeypatch.setattr(access.settings, "web_url", "http://box.lan:3000")
    assert client.get("/health", headers={"host": "box.lan:8000"}).status_code == 200


def test_the_web_app_reads_answers_and_no_one_else_does(client):
    ok = client.get("/health", headers={"origin": WEB})
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == WEB
    preflight = client.options("/facts", headers={"origin": WEB, "access-control-request-method": "POST",
                                                       "access-control-request-headers": "content-type"})
    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == WEB
    for origin in ("http://evil.example", "http://localhost:3001", "null"):
        assert client.get("/health", headers={"origin": origin}).status_code == 403, origin
        refused = client.options("/facts", headers={"origin": origin, "access-control-request-method": "POST"})
        assert refused.status_code == 403 and "access-control-allow-origin" not in refused.headers, origin


def test_another_sites_form_post_never_runs(client):
    """A simple POST needs no preflight, so CORS alone would let it run: the guard refuses it first."""
    assert client.post("/pipeline/run", headers={"origin": "http://evil.example"}).status_code == 403
    assert client.started == []
    assert client.post("/pipeline/run", headers={"origin": WEB}).status_code == 200
    assert client.post("/pipeline/run").status_code == 200  # no Origin: not a browser page (curl, the CLI)
    assert len(client.started) == 2


def test_the_apis_own_docs_page_may_call_it(client):
    assert client.get("/health", headers={"origin": API}).status_code == 200
    assert client.get("/health", headers={"origin": "http://127.0.0.1:8000"}).status_code == 403  # another origin
