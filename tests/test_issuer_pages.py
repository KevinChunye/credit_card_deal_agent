import httpx

from card_agent.collector.http import HostThrottle, RobotsCache
from card_agent.collector.issuer_pages import analyze_page, cross_check, fetch_and_analyze
from tests.conftest import fixture_path, routed_client


def page(name: str) -> str:
    return fixture_path(f"issuer_pages/{name}").read_text()


def test_static_offer_text():
    analysis = analyze_page(200, page("chase_sapphire_preferred.html"))
    assert not analysis.blocked and not analysis.js_only
    assert analysis.fields_available == ["bonus", "annual_fee", "earn_rates"]
    assert analysis.best("bonus")["amount"] == 75000
    assert analysis.best("bonus")["strength"] == "strong"  # not the 100k referral blurb
    assert analysis.best("annual_fee")["amount"] == 95  # not "No Annual Fee (15)" in the nav
    rates = {hit["rate"] for hit in analysis.methods["static_text"]["earn_rates"]}
    assert {5.0, 3.0, 2.0} <= rates


def test_inline_state_is_readable_but_can_bundle_other_cards():
    analysis = analyze_page(200, page("amex_style_inline_state.html"))
    assert "inline_state" in analysis.working_methods
    # The strong hit is another card's promo. This is why Amex pages are not
    # wired into the collector (see docs/FINDINGS.md).
    assert analysis.best("bonus")["amount"] == 80000
    assert analysis.best("annual_fee")["amount"] == 95
    zero_fee_hits = [h for h in analysis.methods["inline_state"]["annual_fee"] if h["amount"] == 0]
    # "Additional Cards ... have a $0 annual fee" is excluded; only the intro-fee line remains.
    assert [h["evidence"].count("intro annual fee") for h in zero_fee_hits] == [1]


def test_blocked_and_status_codes():
    assert analyze_page(200, page("blocked.html")).blocked
    assert analyze_page(403, "").blocked
    not_found = analyze_page(404, "<html>gone</html>")
    assert not not_found.blocked and "404" in not_found.note
    js_only = analyze_page(
        200, "<html><body><div id=root></div>Please enable JavaScript</body></html>"
    )
    assert js_only.js_only


def test_cross_check_is_unit_aware():
    analysis = analyze_page(200, page("chase_sapphire_preferred.html"))
    checks = cross_check("chase-sapphire-preferred", analysis, 95, 75000, "points")
    assert {c["field"]: c["match"] for c in checks} == {"annual_fee": True, "bonus_amount": True}
    mismatch = cross_check("chase-sapphire-preferred", analysis, 95, 60000, "points")
    assert {c["field"]: c["match"] for c in mismatch}["bonus_amount"] is False
    # A "$200 bonus" page vs a 20,000-point API offer is not compared.
    usd = analyze_page(
        200, "<p>Earn a $200 bonus after you spend $500 on purchases.</p>" + "x " * 900
    )
    assert [c["field"] for c in cross_check("x", usd, None, 20000, "points")] == []


def test_robots_disallow_means_no_fetch():
    client, seen = routed_client(
        {
            "https://bank.example/robots.txt": "User-agent: *\nDisallow: /cards/\n",
            "https://bank.example/cards/a": page("chase_sapphire_preferred.html"),
        }
    )
    analysis = fetch_and_analyze(
        client, RobotsCache(client), HostThrottle(min_interval=0), "https://bank.example/cards/a"
    )
    assert analysis.blocked and "robots" in analysis.note
    assert seen == ["https://bank.example/robots.txt"]


def test_robots_missing_allows_fetch_and_5xx_blocks():
    client, seen = routed_client(
        {
            "https://ok.example/cards/a": page("chase_sapphire_preferred.html"),
            "https://down.example/robots.txt": httpx.Response(503),
        }
    )
    robots = RobotsCache(client)
    ok = fetch_and_analyze(
        client, robots, HostThrottle(min_interval=0), "https://ok.example/cards/a"
    )
    assert ok.best("bonus")["amount"] == 75000
    down = fetch_and_analyze(
        client, robots, HostThrottle(min_interval=0), "https://down.example/cards/b"
    )
    assert down.blocked
    assert "https://down.example/cards/b" not in seen


def test_host_throttle_waits_between_same_host_requests():
    sleeps = []
    clock = iter([0.0, 0.0, 0.5, 2.0, 2.0, 2.0])
    throttle = HostThrottle(min_interval=2.0, sleep=sleeps.append, clock=lambda: next(clock))
    throttle.wait("https://a.example/1")
    throttle.wait("https://a.example/2")
    throttle.wait("https://b.example/1")
    assert sleeps == [1.5]
