"""Source scope validation. This does not perform or authorize network fetching."""
import ipaddress
from urllib.parse import unquote, urlsplit


def web_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise ValueError("Use an HTTP(S) URL without whitespace or backslashes")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Use an HTTP(S) URL without credentials")
    if parsed.port == 0:
        raise ValueError("Invalid port")
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Source must use a public website")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Source must use a public website")
    path = unquote(parsed.path)
    if "\\" in path or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("Ambiguous source path")
    return parsed


class SiteScope:
    def __init__(self, home_url):
        self.home = web_url(home_url) if home_url else None

    def allows(self, url):
        if self.home is None:
            return False
        try:
            target = web_url(url)
        except ValueError:
            return False
        def origin(parsed):
            return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
        root = unquote(self.home.path).rstrip("/")
        path = unquote(target.path)
        return origin(target) == origin(self.home) and (not root or path == root or path.startswith(root + "/"))
