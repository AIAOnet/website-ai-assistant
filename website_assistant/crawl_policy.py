"""Canonical URL, public-address, and bounded crawl policy."""
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, quote

from pydantic import BaseModel, ConfigDict, Field

from .site_settings import SiteScope, web_url


class CrawlError(ValueError):
    pass


class CrawlLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    max_pages: int = Field(default=200, ge=1, le=1000)
    max_depth: int = Field(default=4, ge=0, le=10)
    max_urls: int = Field(default=2000, ge=1, le=10000)
    max_sitemaps: int = Field(default=20, ge=0, le=100)
    max_bytes: int = Field(default=2_000_000, ge=1024, le=5_000_000)
    timeout_seconds: float = Field(default=15, ge=1, le=60, allow_inf_nan=False)
    budget_seconds: float = Field(default=600, ge=1, le=3600, allow_inf_nan=False)
    delay_seconds: float = Field(default=.5, ge=.5, le=30, allow_inf_nan=False)


def normalize_url(value, base=None):
    if not isinstance(value, str) or len(value) > 2048:
        raise CrawlError("invalid_url")
    # Validate the unjoined input so urljoin cannot erase malicious dot segments.
    if any(ord(c) < 32 for c in value) or "\\" in value:
        raise CrawlError("invalid_url")
    target = urljoin(base, value) if base else value
    try:
        parsed = web_url(target)
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if any(not label for label in host.split(".")):
            raise ValueError()
        port = parsed.port
    except (ValueError, UnicodeError) as error:
        raise CrawlError("invalid_url") from error
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != {"http":80, "https":443}[parsed.scheme]:
        authority += f":{port}"
    # Keep content-bearing query parameters; bound query variation through max_urls.
    query = sorted((key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=30)
                   if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"})
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    return urlunsplit((parsed.scheme, authority, path, urlencode(query), ""))


def origin(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def public_addresses(host, port, resolver=None):
    """Return verified numeric socket addresses, never resolve again at connect time."""
    try:
        entries = (resolver or socket.getaddrinfo)(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise CrawlError("dns_failed") from error
    if not entries:
        raise CrawlError("dns_failed")
    for family, _, _, _, address in entries:
        ip = ipaddress.ip_address(address[0])
        if (family not in {socket.AF_INET, socket.AF_INET6} or not ip.is_global
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified
                or (getattr(ip, "ipv4_mapped", None) and not ip.ipv4_mapped.is_global)):
            raise CrawlError("private_destination")
    return entries


def initial_redirect_allowed(home, target):
    """Allow HTTPS upgrade and www canonicalization, but never another website."""
    left, right = urlsplit(home), urlsplit(target)
    hosts = {left.hostname, left.hostname[4:] if left.hostname.startswith("www.") else "www." + left.hostname}
    if right.hostname not in hosts or (left.scheme == "https" and right.scheme != "https"):
        return False
    if (left.port or {"http":80,"https":443}[left.scheme]) != (right.port or {"http":80,"https":443}[right.scheme]):
        if not (left.scheme == "http" and left.port in {None,80} and right.scheme == "https" and right.port in {None,443}):
            return False
    # Preserve a user-specified section even while canonicalizing the hostname.
    scoped = origin(home) + (right.path or "/")
    return SiteScope(home).allows(scoped)
