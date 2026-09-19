"""Reject nonlocal request authorities before Flask or dataset dispatch runs."""

import re

from werkzeug.wrappers import Response


_LOCAL_AUTHORITY = re.compile(
    r"(?:localhost|127\.0\.0\.1)(?::([0-9]{1,5}))?", re.IGNORECASE | re.ASCII
)


class LocalHostOnly:
    """Guard the two entry points using the app's supported IPv4 loopback URLs."""

    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        # Require the actual Host header. Forwarded headers and SERVER_NAME are
        # not authority for this direct, localhost-only deployment. Full matching
        # rejects suffixes, userinfo, duplicate hosts and malformed ports alike.
        match = _LOCAL_AUTHORITY.fullmatch(environ.get("HTTP_HOST", ""))
        if match is None or (
            match.group(1) is not None and not 1 <= int(match.group(1)) <= 65535
        ):
            response = Response(
                "This address is not supported. Open LookThrough using localhost "
                "or 127.0.0.1 on the app's local port.\n",
                status=400,
                content_type="text/plain; charset=utf-8",
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
            return response(environ, start_response)
        return self.application(environ, start_response)
