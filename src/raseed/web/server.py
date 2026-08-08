"""The dashboard's HTTP server, on the standard library.

No web framework. A framework would be the right call for a service with
sessions, forms, uploads and concurrency; this has none of those. It is four
read-only routes for one user. `http.server` covers that without adding a
package to a project whose section 23.1 allowlist does not have one, and
invariant 12 says an install is a conversation, not a convenience.

**It binds to 127.0.0.1 and nothing else.** That is not a default, it is the
security model of this phase: the ledger has no PII in it by construction
(invariant 3), but it is still a record of what someone bought, and it is
reachable by anyone who can reach the socket. Loopback means the only people
who can reach it are people already on the machine.

Exposing this to the internet is a separate, deliberate step, and it does not
happen without authentication landing in the same change. `HOST` is not read
from the environment for exactly that reason: making it public should require
editing code and thinking about it, not flipping a variable.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from urllib.parse import urlparse

from sqlalchemy.orm import Session, sessionmaker

from raseed.db.seed import bootstrap
from raseed.web import data, render

log = logging.getLogger(__name__)

#: Loopback only. See the module docstring: this is the security model, not a
#: default to be overridden from the environment.
HOST: Final[str] = "127.0.0.1"

DEFAULT_PORT: Final[int] = 8770

#: How many months the bar chart covers.
CHART_MONTHS: Final[int] = 6

#: How many receipts the table lists. Enough to scroll, not enough to render a
#: megabyte of HTML on a phone once the ledger has a few years in it.
RECENT_LIMIT: Final[int] = 50


class Dashboard:
    """Renders pages from a database session. Knows nothing about HTTP."""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        clock: Callable[[], dt.datetime],
    ) -> None:
        self._sessions = session_factory
        self._clock = clock

    def index(self) -> str:
        now = self._clock()
        today = now.date()
        with self._sessions() as session:
            user = bootstrap(session)
            session.commit()
            start, end = data.month_bounds(today)
            return render.dashboard(
                overview=data.overview(session, user_id=user.id, today=today),
                buckets=data.monthly_totals(
                    session, user_id=user.id, months=CHART_MONTHS, today=today
                ),
                slices=data.category_totals(session, user_id=user.id, start=start, end=end),
                rows=data.recent(session, user_id=user.id, limit=RECENT_LIMIT),
                generated_at=now,
            )

    def receipt(self, transaction_id: str) -> str | None:
        now = self._clock()
        with self._sessions() as session:
            user = bootstrap(session)
            session.commit()
            found = data.receipt(session, user_id=user.id, transaction_id=transaction_id)
            if found is None:
                return None
            return render.receipt_page(found, generated_at=now)


def handler_for(dashboard: Dashboard) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to one dashboard.

    A closure rather than a class attribute, because `ThreadingHTTPServer`
    instantiates the handler per request and there is nowhere else to put state
    that does not end up global.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "raseed"
        sys_version = ""

        def _send(self, status: HTTPStatus, body: str, content_type: str = "text/html") -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            # Nothing here is cacheable: the ledger changes under it.
            self.send_header("Cache-Control", "no-store")
            # Defence in depth. The page has no scripts and no frames, so say so.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:;",
            )
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"

            if path == "/health":
                self._send(HTTPStatus.OK, "ok", content_type="text/plain")
                return

            if path == "/":
                self._send(HTTPStatus.OK, dashboard.index())
                return

            if path.startswith("/receipt/"):
                page = dashboard.receipt(path.removeprefix("/receipt/"))
                if page is None:
                    self._send(HTTPStatus.NOT_FOUND, render.not_found())
                    return
                self._send(HTTPStatus.OK, page)
                return

            self._send(HTTPStatus.NOT_FOUND, render.not_found())

        def do_POST(self) -> None:
            """There are no writes. Say so explicitly rather than 404ing."""
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Allow", "GET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            """Route access logs through logging instead of stderr."""
            log.debug("%s %s", self.address_string(), format % args)

    return Handler


def serve(
    *,
    session_factory: sessionmaker[Session],
    clock: Callable[[], dt.datetime],
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Start the dashboard on a background thread and return the server.

    Returned rather than blocking, so the caller keeps control: the bot owns the
    main thread and its asyncio loop, and the dashboard is a guest in that
    process reading the same SQLite file.
    """
    dashboard = Dashboard(session_factory=session_factory, clock=clock)
    server = ThreadingHTTPServer((HOST, port), handler_for(dashboard))
    thread = threading.Thread(target=server.serve_forever, name="raseed-web", daemon=True)
    thread.start()
    log.info("dashboard on http://%s:%d", HOST, port)
    return server


__all__ = [
    "CHART_MONTHS",
    "DEFAULT_PORT",
    "HOST",
    "RECENT_LIMIT",
    "Dashboard",
    "handler_for",
    "serve",
]
