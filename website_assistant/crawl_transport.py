"""One-hop public HTTP transport with pinned DNS and original-host TLS validation."""
import http.client
import socket
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .crawl_policy import CrawlError, normalize_url, public_addresses

USER_AGENT = "WebsiteAssistant/0.1"


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    headers: dict
    body: bytes
    fetched_at: str = field(default_factory=lambda:datetime.now(timezone.utc).isoformat())


class PublicTransport:
    def fetch(self, url, *, max_bytes, timeout):
        url = normalize_url(url)
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        deadline = time.monotonic() + timeout
        entries = public_addresses(parsed.hostname, port)
        family, kind, proto, _, address = entries[0]
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        sock = None
        response = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CrawlError("request_timeout")
            sock = socket.socket(family, kind, proto)
            sock.settimeout(remaining)
            sock.connect(address)  # Numeric address checked above; no second DNS lookup.
            if parsed.scheme == "https":
                sock.settimeout(max(.001, deadline - time.monotonic()))
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            connection.sock = sock
            connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""),
                               headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Connection": "close"})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            if headers.get("content-encoding", "identity").lower() not in {"", "identity"}:
                raise CrawlError("unsupported_encoding")
            if headers.get("content-length") and int(headers["content-length"]) > max_bytes:
                raise CrawlError("response_too_large")
            body = bytearray()
            while len(body) <= max_bytes and not response.isclosed():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CrawlError("request_timeout")
                sock.settimeout(remaining)
                part = response.read1(min(65536, max_bytes + 1 - len(body)))
                if not part:
                    break
                body.extend(part)
            if len(body) > max_bytes:
                raise CrawlError("response_too_large")
            if response.length not in (None, 0):
                raise CrawlError("incomplete_response")
            return FetchResult(url, response.status, headers, bytes(body))
        except (OSError, http.client.HTTPException, ValueError) as error:
            if isinstance(error, CrawlError):
                raise
            raise CrawlError("fetch_failed") from error
        finally:
            if response is not None:
                response.close()
            connection.close()
            if sock is not None:
                sock.close()
