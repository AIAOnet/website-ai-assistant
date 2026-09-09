import socket
import unittest
from unittest.mock import Mock, patch

from website_assistant.crawl_policy import CrawlError, CrawlLimits, normalize_url, public_addresses
from website_assistant.crawl_transport import FetchResult, PublicTransport
from website_assistant.crawler import WebsiteCrawler
from website_assistant.robots import RobotsRules

HOME = "https://example.com/"


class FixtureTransport:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def fetch(self, url, **kwargs):
        self.calls.append(url)
        item = self.pages.get(url, (404, {}, b""))
        if isinstance(item, Exception):
            raise item
        status, headers, body = item
        return FetchResult(url, status, headers, body.encode() if isinstance(body, str) else body)


def html(body):
    return 200, {"content-type": "text/html; charset=utf-8"}, body


class DiscoveryTests(unittest.TestCase):
    def test_slow_progress_checkpoint_cannot_cause_negative_sleep(self):
        current = [0.0]
        sleeps = []
        def progress(value):
            current[0] += .021  # Simulates persistence work on a mounted volume.
        def sleep(seconds):
            if seconds < 0:
                raise ValueError("sleep length must be non-negative")
            sleeps.append(seconds)
            current[0] += seconds
        crawler = WebsiteCrawler(CrawlLimits(max_pages=1), transport=FixtureTransport({HOME:html("Home")}),
                                 clock=lambda:current[0], sleep=sleep)
        result = crawler.run(HOME, progress=progress)
        self.assertEqual(result["page_count"], 1)
        self.assertTrue(sleeps)

    def run_site(self, pages, home=HOME, **limits):
        current = [0.0]
        def sleep(seconds):
            current[0] += seconds
        transport = FixtureTransport(pages)
        crawler = WebsiteCrawler(CrawlLimits(**limits), transport=transport, clock=lambda:current[0], sleep=sleep)
        result = crawler.run(home)
        return result, transport, current[0]

    def test_homepage_links_tracking_dedup_and_scope(self):
        result, transport, _ = self.run_site({HOME:html('<a href="/a#one">A</a><a href="/a?utm_source=x">A</a><a href="https://elsewhere.example/">External</a>'), HOME+"a":html("A")})
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(transport.calls.count(HOME+"a"), 1)
        self.assertFalse(any("elsewhere" in url for url in transport.calls))

    def test_sitemap_index_discovers_unlinked_page(self):
        result, _, _ = self.run_site({HOME:html("Home"), HOME+"sitemap.xml":(200, {}, '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://example.com/map.xml</loc></sitemap></sitemapindex>'),
            HOME+"map.xml":(200, {}, '<urlset><url><loc>https://example.com/hidden</loc></url></urlset>'), HOME+"hidden":html("Hidden")})
        self.assertEqual({p["url"] for p in result["pages"]}, {HOME, HOME+"hidden"})

    def test_robots_denied_pages_never_fetched(self):
        result, transport, _ = self.run_site({HOME:html('<a href="/private">Private</a>'), HOME+"robots.txt":(200, {}, "User-agent: *\nDisallow: /private\n")})
        self.assertNotIn(HOME+"private", transport.calls)
        self.assertTrue(any(r["reason"] == "robots_denied" for r in result["skipped"]))

    def test_robots_failure_and_home_denial_fail_closed(self):
        for robot in ((503, {}, ""), (200, {}, "User-agent: *\nDisallow: /\n"), (302, {"location":"/other"}, "")):
            result, transport, _ = self.run_site({HOME:html("Home"), HOME+"robots.txt":robot})
            self.assertEqual(result["status"], "failed")
            self.assertNotIn(HOME, transport.calls)

    def test_path_scope_and_redirect_escape(self):
        result, transport, _ = self.run_site({HOME+"shop":html('<a href="/other">Other</a><a href="/shop/a">A</a>'), HOME+"shop/a":(302,{"location":"/other"},"")}, home=HOME+"shop")
        self.assertNotIn(HOME+"other", transport.calls)
        self.assertTrue(any(r["reason"] == "redirect_out_of_scope" for r in result["skipped"]))

    def test_initial_https_www_canonicalization(self):
        first = "http://example.com/"
        final = "https://www.example.com/"
        result, _, _ = self.run_site({first:(301,{"location":final},""), final:html("Home")}, home=first)
        self.assertEqual(result["canonical_homepage"], final)
        self.assertEqual(result["page_count"], 1)

    def test_redirect_loop_and_private_redirect(self):
        for target in (HOME, "http://127.0.0.1/", "https://evil.example/"):
            result, transport, _ = self.run_site({HOME:(302,{"location":target},"")})
            self.assertEqual(result["status"], "failed")
            self.assertLessEqual(len(transport.calls), 2)

    def test_depth_page_and_url_limits(self):
        pages = {HOME:html('<a href="/a">A</a><a href="/b">B</a>'), HOME+"a":html('<a href="/deep">Deep</a>'), HOME+"b":html("B"), HOME+"deep":html("Deep")}
        result, _, _ = self.run_site(pages, max_pages=1)
        self.assertEqual(result["status"], "page_limit")
        result, transport, _ = self.run_site(pages, max_depth=0)
        self.assertEqual(result["page_count"], 1)
        result, _, _ = self.run_site(pages, max_urls=2)
        self.assertEqual(result["page_count"], 2)
        self.assertTrue(any(r["reason"] == "url_limit" for r in result["skipped"]))

    def test_time_budget_and_cooperative_cancellation(self):
        result, _, _ = self.run_site({HOME:html("Home")}, budget_seconds=1)
        self.assertEqual(result["status"], "time_limit")
        transport = FixtureTransport({HOME:html("Home")})
        result = WebsiteCrawler(transport=transport).run(HOME, cancelled=lambda:True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(transport.calls, [])

    def test_crawl_delay_and_rate_limit_backoff(self):
        result, _, elapsed = self.run_site({HOME:html('<a href="/a">A</a><a href="/b">B</a>'), HOME+"robots.txt":(200, {}, "User-agent: *\nCrawl-delay: 2\n"), HOME+"a":(429,{"retry-after":"10"},""), HOME+"b":html("B")})
        self.assertGreaterEqual(elapsed, 16)
        self.assertTrue(any(r["reason"] == "http_429" for r in result["skipped"]))

    def test_unsupported_and_fetch_failure_reported(self):
        result, _, _ = self.run_site({HOME:html('<a href="/a">A</a><a href="/b">B</a>'), HOME+"a":(200,{"content-type":"application/pdf"},"pdf"), HOME+"b":CrawlError("response_too_large")})
        self.assertEqual(result["page_count"], 1)
        self.assertEqual({r["reason"] for r in result["skipped"]}, {"sitemap_unavailable", "unsupported_content_type", "response_too_large"})

    def test_xml_entities_rejected(self):
        for body in ('<!DOCTYPE x [<!ENTITY x "expanded">]><urlset/>', '<!DOCTYPE x><urlset/>'.encode("utf-16")):
            result, _, _ = self.run_site({HOME:html("Home"), HOME+"sitemap.xml":(200,{},body)})
            self.assertTrue(any(r["reason"] == "unsafe_sitemap" for r in result["skipped"]))


class TransportTests(unittest.TestCase):
    def fetch_wire_response(self, wire, max_bytes=1024):
        client, server = socket.socketpair()
        server.sendall(wire)
        server.shutdown(socket.SHUT_WR)
        # Use real HTTP parsing and socket lifetime, with only connect/DNS stubbed.
        proxy = Mock(wraps=client)
        proxy.connect = Mock()
        try:
            with patch("website_assistant.crawl_transport.public_addresses", return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
            ]), patch("website_assistant.crawl_transport.socket.socket", return_value=proxy):
                return PublicTransport().fetch("http://example.com/", max_bytes=max_bytes, timeout=2)
        finally:
            client.close()
            server.close()

    def test_complete_close_responses_and_empty_body(self):
        for framing, body in (
            (b"Content-Length: 4\r\n", b"home"),
            (b"Content-Length: 0\r\n", b""),
            (b"Transfer-Encoding: chunked\r\n", b"4\r\nhome\r\n0\r\n\r\n"),
            (b"", b"home"),
        ):
            with self.subTest(framing=framing):
                result = self.fetch_wire_response(b"HTTP/1.1 200 OK\r\nConnection: close\r\n" + framing + b"\r\n" + body)
                self.assertEqual(result.body, b"" if b"Length: 0" in framing else b"home")

    def test_truncated_responses_are_rejected(self):
        for framing, body in (
            (b"Content-Length: 10\r\n", b"short"),
            (b"Transfer-Encoding: chunked\r\n", b"9\r\nshort"),
        ):
            with self.subTest(framing=framing), self.assertRaises(CrawlError):
                self.fetch_wire_response(b"HTTP/1.1 200 OK\r\nConnection: close\r\n" + framing + b"\r\n" + body)

    def test_real_response_limit_boundary(self):
        wire = b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 4\r\n\r\nhome"
        self.assertEqual(self.fetch_wire_response(wire, 4).body, b"home")
        with self.assertRaises(CrawlError):
            self.fetch_wire_response(wire, 3)

    def test_robots_longest_rule_wildcards_and_specific_agent(self):
        rules = RobotsRules()
        rules.parse(["User-agent: *", "Allow: /", "Disallow: /private", "Allow: /private/public",
                     "Disallow: /*?secret=*$", "Crawl-delay: 1.5"])
        self.assertFalse(rules.can_fetch("WebsiteAssistant", HOME+"private"))
        self.assertTrue(rules.can_fetch("WebsiteAssistant", HOME+"private/public"))
        self.assertFalse(rules.can_fetch("WebsiteAssistant", HOME+"page?secret=123"))
        self.assertEqual(rules.crawl_delay("WebsiteAssistant"), 1.5)
        rules.parse(["User-agent: *", "Disallow: /", "User-agent: WebsiteAssistant", "Allow: /"])
        self.assertTrue(rules.can_fetch("WebsiteAssistant/0.1", HOME))
        self.assertFalse(rules.can_fetch("AnotherBot", HOME))

    def test_url_normalization_and_unsafe_urls(self):
        self.assertEqual(normalize_url("https://EXAMPLE.com:443/a?utm_campaign=x&z=2&a=1#section"), HOME+"a?a=1&z=2")
        for url in ("file:///etc/passwd", "http://localhost/", "https://user:pass@example.com/", "https://example.com/%2e%2e/private"):
            with self.assertRaises(ValueError):
                normalize_url(url)

    def test_private_mixed_and_ipv6_addresses_rejected(self):
        def entry(ip):
            return (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip,443))
        for ip in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1"):
            with self.subTest(ip=ip), self.assertRaises(CrawlError):
                public_addresses("example.com",443,resolver=lambda *a, **k:[entry("93.184.216.34"),entry(ip)])

    def test_numeric_address_is_pinned_and_tls_uses_original_host(self):
        response = Mock()
        response.isclosed.return_value = False
        response.length = None
        response.status = 200
        response.getheaders.return_value = [("Content-Type","text/html")]
        response.read1.side_effect = [b"home",b""]
        connection, sock, secure = Mock(), Mock(), Mock()
        connection.getresponse.return_value = response
        address = ("93.184.216.34",443)
        with patch("website_assistant.crawl_transport.public_addresses", return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",address)]) as resolver, patch("website_assistant.crawl_transport.socket.socket",return_value=sock), patch("website_assistant.crawl_transport.http.client.HTTPConnection",return_value=connection), patch("website_assistant.crawl_transport.ssl.create_default_context") as tls:
            tls.return_value.wrap_socket.return_value = secure
            result = PublicTransport().fetch(HOME,max_bytes=1024,timeout=15)
            resolver.assert_called_once_with("example.com",443)
            sock.connect.assert_called_once_with(address)
            tls.return_value.wrap_socket.assert_called_once_with(sock,server_hostname="example.com")
            self.assertIs(connection.sock, secure)
            self.assertEqual(result.body,b"home")

    def test_size_limit_and_compression_rejected(self):
        for headers, body in (([("Content-Length","2048")],b""), ([("Content-Encoding","gzip")],b""), ([],b"x"*1025)):
            response=Mock(); response.status=200; response.getheaders.return_value=headers
            response.isclosed.return_value=False; response.length=None
            response.read1.return_value=body
            connection=Mock(); connection.getresponse.return_value=response
            with patch("website_assistant.crawl_transport.public_addresses",return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("93.184.216.34",80))]), patch("website_assistant.crawl_transport.socket.socket"), patch("website_assistant.crawl_transport.http.client.HTTPConnection",return_value=connection):
                with self.assertRaises(CrawlError):
                    PublicTransport().fetch("http://example.com/",max_bytes=1024,timeout=15)
