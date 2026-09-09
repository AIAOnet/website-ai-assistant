"""Bounded robots matching with longest-path precedence and wildcard support."""
from fnmatch import fnmatchcase
from types import SimpleNamespace
from urllib.parse import urlsplit, unquote

from .crawl_policy import CrawlError


class RobotsRules:
    def parse(self, lines):
        self.groups, self.maps = [], []
        agents, directives = [], []
        count = 0
        for line in [*lines, "User-agent: __end__"]:
            key, separator, value = line.split("#", 1)[0].partition(":")
            if not separator:
                continue
            key, value = key.strip().lower(), value.strip()
            if key == "sitemap":
                if value and len(self.maps) < 100:
                    self.maps.append(value)
                continue
            if key == "user-agent":
                if directives:
                    self.groups.append((agents, directives))
                    agents, directives = [], []
                agents.append(value.lower())
            elif agents and key in {"allow", "disallow", "crawl-delay", "request-rate"}:
                count += 1
                if count > 1000 or len(value) > 2048:
                    raise CrawlError("robots_limit")
                directives.append((key, value))

    def selected(self, agent):
        matches = []
        for names, directives in self.groups:
            strength = max((0 if name == "*" else len(name) for name in names
                            if name == "*" or (name and name in agent.lower())), default=-1)
            if strength >= 0:
                matches.append((strength, directives))
        best = max((strength for strength, _ in matches), default=-1)
        return [rule for strength, rules in matches if strength == best for rule in rules]

    def can_fetch(self, agent, url):
        parsed = urlsplit(url)
        path = unquote(parsed.path + ("?" + parsed.query if parsed.query else ""))
        matches = []
        for key, value in self.selected(agent):
            if key not in {"allow", "disallow"} or not value:
                continue
            value = unquote(value)
            anchored = value.endswith("$")
            value = value[:-1] if anchored else value
            pattern = "".join({"[": "[[]", "]": "[]]", "?": "[?]"}.get(c, c) for c in value)
            if fnmatchcase(path, pattern if anchored else pattern + "*"):
                matches.append((len(value.replace("*", "")), key == "allow"))
        return max(matches)[1] if matches else True

    def crawl_delay(self, agent):
        delays = []
        for key, value in self.selected(agent):
            if key == "crawl-delay":
                try:
                    delay = float(value)
                    if 0 <= delay <= 86400:
                        delays.append(delay)
                except ValueError:
                    pass
        return max(delays, default=0)

    def request_rate(self, agent):
        rates = []
        for key, value in self.selected(agent):
            if key == "request-rate":
                try:
                    requests, seconds = (int(part) for part in value.split("/"))
                    if requests > 0 and seconds > 0:
                        rates.append(SimpleNamespace(requests=requests, seconds=seconds))
                except ValueError:
                    pass
        return max(rates, key=lambda rate: rate.seconds / rate.requests) if rates else None

    def site_maps(self):
        return self.maps
