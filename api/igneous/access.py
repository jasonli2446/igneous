"""Who may call the API. It serves your data on this computer without a login, so only local callers get in.

- The Host a request names must be this computer (localhost, 127.0.0.1, [::1]) or the web app's host
  (IGNEOUS_WEB_URL). A site that points its own name at 127.0.0.1 (DNS rebinding) names its own host and is refused.
- A browser sends the page's Origin. Only the web app and the API's own pages (its /docs) may call; a page on any
  other site is refused before anything runs, even a request a browser sends without asking first (a form posted
  to /pipeline/run).
- CORS lets the web app's origin, and only it, read the answers.
"""
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from igneous.config import settings

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def web_origin() -> str:
    """The web app's origin (scheme://host:port), from IGNEOUS_WEB_URL."""
    url = urlsplit(settings.web_url)
    return f"{url.scheme}://{url.netloc}".lower()


def allowed_host(host: str | None) -> bool:
    """Whether a Host header names this computer or the web app's host."""
    if not host:
        return False
    try:
        name = urlsplit(f"//{host}").hostname
    except ValueError:
        return False
    return name is not None and (name in LOCAL_HOSTS or name == urlsplit(settings.web_url).hostname)


class LocalOnly:
    """Refuses a request whose Host isn't local (400) or whose Origin is another site's (403)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        host = headers.get("host")
        if not allowed_host(host):
            refusal = PlainTextResponse("This API answers only on localhost or the web app's host (IGNEOUS_WEB_URL).",
                                        status_code=400)
            return await refusal(scope, receive, send)
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in (web_origin(), f"{scope['scheme']}://{host}".lower()):
            refusal = PlainTextResponse("Only the web app (IGNEOUS_WEB_URL) may call this API from a browser.",
                                        status_code=403)
            return await refusal(scope, receive, send)
        await self.app(scope, receive, send)


class WebAppCORS(CORSMiddleware):
    """CORS for the web app's origin, as the settings name it now."""

    def is_allowed_origin(self, origin: str) -> bool:
        return origin.lower() == web_origin()
