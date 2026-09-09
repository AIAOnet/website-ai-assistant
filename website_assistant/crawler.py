"""Bounded homepage discovery. Produces staged HTML and a report, never an active index."""
import time
from collections import deque
from html.parser import HTMLParser
from urllib.parse import urljoin
from xml.etree import ElementTree

from .crawl_policy import CrawlError, CrawlLimits, normalize_url, origin, initial_redirect_allowed
from .crawl_transport import PublicTransport, USER_AGENT
from .site_settings import SiteScope
from .robots import RobotsRules
from .extraction import decode_html, ExtractionError


class Links(HTMLParser):
    def __init__(self, limit):
        super().__init__(convert_charrefs=True)
        self.links, self.limit = [], limit

    def handle_starttag(self, tag, attrs):
        if tag == "a" and len(self.links) < self.limit:
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


class WebsiteCrawler:
    def __init__(self, limits=None, *, transport=None, clock=time.monotonic, sleep=time.sleep):
        self.limits = limits or CrawlLimits()
        self.transport = transport or PublicTransport()
        self.clock, self.sleep = clock, sleep

    def run(self, homepage, *, cancelled=lambda: False, progress=lambda value: None):
        # All job state is local so instances can be reused without leaking a site.
        home = normalize_url(homepage)
        started = self.clock()
        report = {"homepage": home, "canonical_homepage": None, "pages": [], "skipped": [],
                  "limits": self.limits.model_dump(), "status": "running", "requests": 0}
        robots, last_request = {}, {}
        seen, visited_sitemaps = set(), set()

        def checkpoint():
            progress({"requests": report["requests"], "page_count": len(report["pages"]),
                      "skipped_count": len(report["skipped"])})
            if cancelled():
                raise CrawlError("cancelled")
            if self.clock() - started >= self.limits.budget_seconds:
                raise CrawlError("time_limit")

        def wait(seconds):
            end = self.clock() + seconds
            while self.clock() < end:
                checkpoint()
                remaining = end - self.clock()
                if remaining > 0:
                    self.sleep(min(.2, remaining))

        def fetch(url, delay=None):
            checkpoint()
            host_origin = origin(url)
            interval = max(self.limits.delay_seconds, delay or 0)
            if host_origin in last_request:
                wait(max(0, interval - (self.clock() - last_request[host_origin])))
            checkpoint()
            last_request[host_origin] = self.clock()
            report["requests"] += 1
            result = self.transport.fetch(url, max_bytes=self.limits.max_bytes,
                timeout=min(self.limits.timeout_seconds, self.limits.budget_seconds - (self.clock() - started)))
            checkpoint()
            if result.status in {429, 503}:
                # Conservative numeric Retry-After; no immediate retry storm.
                try:
                    retry = max(1, min(300, float(result.headers.get("retry-after", "5"))))
                except ValueError:
                    retry = 5
                last_request[host_origin] = self.clock() + retry
            return result

        def robot(url):
            host_origin = origin(url)
            if host_origin not in robots:
                response = fetch(host_origin + "/robots.txt")
                parser = RobotsRules()
                if response.status in {404, 410}:
                    parser.parse([])
                elif response.status == 200:
                    parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
                else:
                    # Redirects/errors in robots cannot accidentally authorize a crawl.
                    raise CrawlError("robots_unavailable")
                robots[host_origin] = parser
            return robots[host_origin]

        def allowed_fetch(url):
            rules = robot(url)
            if not rules.can_fetch(USER_AGENT, url):
                raise CrawlError("robots_denied")
            delay = rules.crawl_delay(USER_AGENT) or 0
            rate = rules.request_rate(USER_AGENT)
            if rate and rate.requests:
                delay = max(delay, rate.seconds / rate.requests)
            return fetch(url, delay)

        def document(url, initial=False):
            chain = set()
            for _ in range(6):
                if url in chain:
                    raise CrawlError("redirect_loop")
                chain.add(url)
                response = allowed_fetch(url)
                if response.status not in {301, 302, 303, 307, 308}:
                    return url, response
                location = response.headers.get("location")
                if not location:
                    raise CrawlError("invalid_redirect")
                target = normalize_url(location, url)
                permitted = initial_redirect_allowed(home, target) if initial else scope.allows(target)
                if not permitted:
                    raise CrawlError("redirect_out_of_scope")
                url = target
            raise CrawlError("redirect_limit")

        def skip(url, reason):
            if len(report["skipped"]) < self.limits.max_urls:
                report["skipped"].append({"url": url, "reason": reason})

        try:
            canonical, first = document(home, initial=True)
            report["canonical_homepage"] = canonical
            scope = SiteScope(canonical)
            pending = deque([(canonical, 0, first)])
            queued = {canonical}

            def enqueue(candidate, base, depth):
                checkpoint()
                try:
                    url = normalize_url(candidate, base)
                except ValueError:
                    skip(candidate[:2048], "invalid_url")
                    return
                if not scope.allows(url):
                    skip(url, "out_of_scope")
                elif depth > self.limits.max_depth:
                    skip(url, "depth_limit")
                elif url not in queued:
                    if len(queued) >= self.limits.max_urls:
                        skip(url, "url_limit")
                    else:
                        queued.add(url)
                        pending.append((url, depth, None))

            maps = deque(robot(canonical).site_maps() or [origin(canonical) + "/sitemap.xml"])
            while maps and len(visited_sitemaps) < self.limits.max_sitemaps:
                checkpoint()
                raw = maps.popleft()
                try:
                    url = normalize_url(raw, canonical)
                    if origin(url) != origin(canonical):
                        raise CrawlError("sitemap_out_of_scope")
                    if url in visited_sitemaps:
                        continue
                    visited_sitemaps.add(url)
                    result = allowed_fetch(url)
                    if result.status != 200:
                        raise CrawlError("sitemap_unavailable")
                    if b"\x00" in result.body or b"<!DOCTYPE" in result.body.upper() or b"<!ENTITY" in result.body.upper():
                        raise CrawlError("unsafe_sitemap")
                    root = ElementTree.fromstring(result.body)
                    kind = root.tag.rsplit("}", 1)[-1]
                    if kind not in {"urlset", "sitemapindex"}:
                        raise CrawlError("invalid_sitemap")
                    for child in root:
                        location = next((node.text for node in child if node.tag.rsplit("}", 1)[-1] == "loc"), None)
                        if not location:
                            continue
                        if kind == "sitemapindex":
                            if len(maps) + len(visited_sitemaps) < self.limits.max_sitemaps:
                                maps.append(location)
                            else:
                                skip(location[:2048], "sitemap_limit")
                        else:
                            enqueue(location, canonical, 0)
                except (ValueError, ElementTree.ParseError) as error:
                    if str(error) in {"cancelled", "time_limit"}:
                        raise
                    skip(raw[:2048], str(error) if isinstance(error, CrawlError) else "invalid_sitemap")

            while pending and len(report["pages"]) < self.limits.max_pages:
                checkpoint()
                url, depth, response = pending.popleft()
                if url in seen:
                    continue
                seen.add(url)
                try:
                    final, response = (url, response) if response is not None else document(url)
                    if final != url and final in seen:
                        continue
                    seen.add(final)
                    if response.status != 200:
                        raise CrawlError("http_" + str(response.status))
                    media = response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                    if media not in {"text/html", "application/xhtml+xml"}:
                        raise CrawlError("unsupported_content_type")
                    try:
                        html, encoding = decode_html(response.body,response.headers.get("content-type",""))
                    except ExtractionError as error:
                        raise CrawlError(str(error)) from error
                    report["pages"].append({"url": final, "html": html, "depth": depth,
                                            "fetched_at":response.fetched_at,"encoding":encoding})
                    links = Links(self.limits.max_urls)
                    links.feed(html)
                    for link in links.links:
                        enqueue(link, final, depth + 1)
                except ValueError as error:
                    if str(error) in {"cancelled", "time_limit"}:
                        raise
                    skip(url, str(error) if isinstance(error, CrawlError) else "invalid_page")
            report["status"] = "page_limit" if pending else "complete"
            if not report["pages"]:
                report["status"] = "failed"
        except CrawlError as error:
            report["status"] = str(error) if str(error) in {"cancelled", "time_limit"} else "failed"
            report["error"] = str(error)
        report["page_count"] = len(report["pages"])
        return report
