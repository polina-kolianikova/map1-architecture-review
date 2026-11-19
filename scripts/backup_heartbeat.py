#!/usr/bin/env python3
"""Send a bounded backup heartbeat without Docker, credentials, or environment logs."""
import os
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise ValueError("Heartbeat redirects are not allowed")


def validated_url(value):
    if not value or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid heartbeat URL")
    parsed = urlsplit(value)
    identifier = str(UUID(parsed.path.removeprefix("/")))
    if value != f"https://hc-ping.com/{identifier}":
        raise ValueError("Heartbeat requires a canonical Healthchecks HTTPS ping URL")
    return value


def main():
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("start", "success"):
            raise ValueError("Choose start or success")
        endpoint = validated_url(os.environ.get("BACKUP_HEARTBEAT_URL", ""))
        if sys.argv[1] == "start":
            endpoint += "/start"
        request = Request(endpoint, data=b"", method="POST")
        with build_opener(NoRedirect()).open(request, timeout=5) as response:
            if response.status != 200 or response.read(16).strip() != b"OK":
                raise ValueError("Monitor did not accept heartbeat")
    except Exception:
        print("Backup heartbeat was not accepted by monitor", file=sys.stderr)
        return 1
    print("Backup heartbeat accepted by monitor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
