"""The dashboard's HTTP server, on the standard library.

No web framework. A framework would be the right call for a service with
sessions, forms, uploads and concurrency; this has none of those. It is four
read-only routes for one user. `http.server` covers that without adding a
package to a project whose section 23.1 allowlist does not have one, and
invariant 12 says an install is a conversation, not a convenience.

**It still binds to 127.0.0.1 and nothing else**, and now that there is a public
address that matters more, not less. Cloudflare Tunnel makes an outbound
connection to this loopback socket and terminates TLS at Cloudflare's edge, so
nothing has to listen on a public interface, no port is forwarded, and no
firewall hole is opened. `HOST` is still not read from the environment: making
this listen on 0.0.0.0 should require editing code and thinking about it.

**Every page requires proof of who is asking.** The proof is Telegram's signed
`initData`, sent in an `Authorization: tma <blob>` header and verified in
`raseed.identity` against the bot token. There is no cookie and no session:

- Nothing on the server expires, so there is no session store to grow or leak.
- A forged cross-site request cannot attach a header it does not have, so there
  is no CSRF surface to reason about.
- The blob is Telegram's to issue and expires on its own in an hour.

`/` is the one unauthenticated route and it contains no ledger data: it is a
shell that reads `initData` in the browser and fetches the real page with it.
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
from raseed.identity import AuthError, verify_init_data
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

#: Named ancestors rather than `X-Frame-Options: DENY`, because a Mini App runs
#: inside Telegram's frame and DENY would break it. This is the tighter statement
#: anyway: Telegram may frame this page and nobody else may.
#:
#: `script-src` names telegram.org because the Mini App SDK has to come from
#: Telegram, and `'unsafe-inline'` covers the one boot script in `render.shell`.
#: `connect-src 'self'` is what lets that script fetch the real pages back.
CSP: Final[str] = (
    "default-src 'none'; "
    "script-src https://telegram.org 'unsafe-inline'; "
    "connect-src 'self'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors https://web.telegram.org https://telegram.org"
)


#: The scheme name Telegram documents for passing `initData` in a header.
AUTH_SCHEME: Final[str] = "tma "


class Dashboard:
    """Renders pages from a database session. Knows nothing about HTTP.

    Every method takes a `user_id`, and there is no default. That is deliberate:
    the single-user version read "the first user in the table", and left as it
    was it would have shown the owner's ledger to the first friend who opened
    the link.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        clock: Callable[[], dt.datetime],
        bot_token: str,
        user_id_secret: str,
    ) -> None:
        self._sessions = session_factory
        self._clock = clock
        self._bot_token = bot_token
        self._secret = user_id_secret

    def authenticate(self, header: str | None) -> str:
        """Turn an `Authorization` header into a `user_id`, or refuse.

        Raises:
            AuthError: No header, wrong scheme, bad signature, or stale.
        """
        if not header or not header.startswith(AUTH_SCHEME):
            raise AuthError("could not verify")

        visitor = verify_init_data(
            header.removeprefix(AUTH_SCHEME).strip(),
            bot_token=self._bot_token,
            now=self._clock().timestamp(),
        )
        user_id = visitor.user_id(secret=self._secret)

        with self._sessions() as session:
            # Their first visit may precede their first receipt, so the ledger
            # is created here too rather than only in the bot.
            bootstrap(session, user_id)
            session.commit()
        return user_id

    def index(self, user_id: str) -> str:
        now = self._clock()
        today = now.date()
        with self._sessions() as session:
            start, end = data.month_bounds(today)
            return render.dashboard(
                overview=data.overview(session, user_id=user_id, today=today),
                buckets=data.monthly_totals(
                    session, user_id=user_id, months=CHART_MONTHS, today=today
                ),
                slices=data.category_totals(session, user_id=user_id, start=start, end=end),
                rows=data.recent(session, user_id=user_id, limit=RECENT_LIMIT),
                generated_at=now,
            )

    def receipt(self, user_id: str, transaction_id: str) -> str | None:
        """One receipt, scoped to its owner.

        `data.receipt` filters on `user_id`, so guessing another user's
        transaction ID returns nothing rather than someone else's shopping.
        """
        now = self._clock()
        with self._sessions() as session:
            found = data.receipt(session, user_id=user_id, transaction_id=transaction_id)
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
            # Nothing here is cacheable: the ledger changes under it, and a
            # shared cache must never hold one user's page for another's request.
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            # A Mini App runs inside Telegram's own frame, so blanket DENY would
            # break it. Naming the ancestors is the tighter statement anyway:
            # only Telegram may frame this, nobody else.
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            self.wfile.write(payload)

        def _authenticated(self) -> str | None:
            """The `user_id` behind this request, or None after sending a 401."""
            try:
                return dashboard.authenticate(self.headers.get("Authorization"))
            except AuthError:
                # Logged without the blob. It is a valid credential for an hour.
                log.info("rejected an unauthenticated dashboard request")
                self._send(HTTPStatus.UNAUTHORIZED, render.locked())
                return None

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"

            if path == "/health":
                # Deliberately open and deliberately empty. The tunnel and any
                # uptime check need it, and it reveals nothing but "running".
                self._send(HTTPStatus.OK, "ok", content_type="text/plain")
                return

            if path == "/":
                # No ledger data. Just enough to read initData and come back.
                self._send(HTTPStatus.OK, render.shell())
                return

            # Everything else, including an unknown path. Authenticating before
            # deciding whether a route exists means a stranger cannot map this
            # server by watching which paths 404 and which do not.
            user_id = self._authenticated()
            if user_id is None:
                return
            status, body = self._route(path, user_id)
            self._send(status, body)

        def _route(self, path: str, user_id: str) -> tuple[HTTPStatus, str]:
            if path == "/app":
                return HTTPStatus.OK, dashboard.index(user_id)

            if path.startswith("/receipt/"):
                page = dashboard.receipt(user_id, path.removeprefix("/receipt/"))
                if page is None:
                    return HTTPStatus.NOT_FOUND, render.not_found()
                return HTTPStatus.OK, page

            return HTTPStatus.NOT_FOUND, render.not_found()

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
    bot_token: str,
    user_id_secret: str,
    port: int = DEFAULT_PORT,
) -> ThreadingHTTPServer:
    """Start the dashboard on a background thread and return the server.

    Returned rather than blocking, so the caller keeps control: the bot owns the
    main thread and its asyncio loop, and the dashboard is a guest in that
    process reading the same SQLite file.

    `bot_token` is required, not optional: it is the key `initData` is signed
    with, and a dashboard that cannot verify a signature is a dashboard with no
    authentication.
    """
    dashboard = Dashboard(
        session_factory=session_factory,
        clock=clock,
        bot_token=bot_token,
        user_id_secret=user_id_secret,
    )
    server = ThreadingHTTPServer((HOST, port), handler_for(dashboard))
    thread = threading.Thread(target=server.serve_forever, name="raseed-web", daemon=True)
    thread.start()
    log.info("dashboard on http://%s:%d", HOST, port)
    return server


__all__ = [
    "AUTH_SCHEME",
    "CHART_MONTHS",
    "CSP",
    "DEFAULT_PORT",
    "HOST",
    "RECENT_LIMIT",
    "Dashboard",
    "handler_for",
    "serve",
]
