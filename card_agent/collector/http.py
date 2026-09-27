"""A polite HTTP client: honest User-Agent, robots.txt, one request per page.

No logins, no cookies carried between sites, no headless browser, no retries
against blocks. If a site refuses a plain GET, we record that and move on.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

# A real, well-formed UA that says who we are (the same pattern search crawlers
# use). We deliberately do not impersonate a desktop browser.
USER_AGENT = (
    "Mozilla/5.0 (compatible; credit-card-deal-agent/0.1; "
    "+https://github.com/KevinChunye/credit_card_deal_agent)"
)
ROBOTS_AGENT = "credit-card-deal-agent"


def make_client(
    timeout: float = 20.0, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    return httpx.Client(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    )


class RobotsCache:
    """Fetches each host's robots.txt once and answers can-fetch questions.

    Per RFC 9309: a 4xx robots.txt means "no rules" (allowed); a 5xx or a
    network error means "assume disallowed".
    """

    def __init__(self, client: httpx.Client):
        self.client = client
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._reasons: dict[str, str] = {}

    def allowed(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._parsers:
            self._load(base)
        parser = self._parsers[base]
        if parser is None:
            return False, self._reasons[base]
        if parser.can_fetch(ROBOTS_AGENT, url):
            return True, "allowed by robots.txt"
        return False, "disallowed by robots.txt"

    def _load(self, base: str) -> None:
        try:
            response = self.client.get(f"{base}/robots.txt")
        except httpx.HTTPError as exc:
            self._parsers[base] = None
            self._reasons[base] = f"robots.txt unreachable ({type(exc).__name__}); not fetching"
            return
        parser = RobotFileParser()
        if 400 <= response.status_code < 500:
            parser.parse([])
        elif response.status_code >= 500:
            self._parsers[base] = None
            self._reasons[base] = f"robots.txt returned {response.status_code}; not fetching"
            return
        else:
            parser.parse(response.text.splitlines())
        self._parsers[base] = parser


class HostThrottle:
    """At most one request per host per `min_interval` seconds."""

    def __init__(self, min_interval: float = 2.0, sleep=time.sleep, clock=time.monotonic):
        self.min_interval = min_interval
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        now = self._clock()
        last = self._last.get(host)
        if last is not None and now - last < self.min_interval:
            self._sleep(self.min_interval - (now - last))
        self._last[host] = self._clock()
