"""Where lead-type evidence comes from: the homepage the scraper already
fetches, graded so that nothing about this machine's network or a site's bot
protection is ever recorded as the practice's own problem."""

import pytest
import requests

import http_pool
import site_audit
from conftest import fixture

SITE = "https://cedargrove.example/"


class Response:
    def __init__(self, status=200, text="", url=""):
        self.status_code = status
        self.text = text
        self.url = url


class Session:
    """Answers by URL: a Response, or an exception to raise. Anything else 404s."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        answer = self.pages.get(url, Response(404, "", url))
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def web(monkeypatch):
    def install(pages):
        session = Session(pages)
        monkeypatch.setattr(http_pool, "session", lambda: session)
        return session
    return install


class TestTheScrapeRecordsTheHomepage:

    def test_the_facts_and_text_come_with_the_contact_details(self, web, email_engine):
        web({SITE: Response(200, fixture("site_modern.html"), SITE)})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["viewport"] is True
        assert contact["facts"]["booking"] == ["simplepractice"]
        assert "Telehealth" in contact["text"]

    def test_a_marketplace_profile_is_not_their_own_site(self, web, email_engine):
        session = web({})
        contact = email_engine.scrape_website_contact_info(
            "https://growtherapy.com/provider/jane")
        assert contact["facts"]["own_site"] is False
        assert contact["facts"]["profile_host"] == "growtherapy.com"
        assert session.calls == []

    def test_a_directory_page_is_not_their_own_site(self, web, email_engine):
        url = "https://www.yelp.com/biz/cedar-grove"
        web({url: Response(200, "<html><body>listing</body></html>", url)})
        contact = email_engine.scrape_website_contact_info(url)
        assert contact["facts"]["own_site"] is False
        assert contact["facts"]["profile_host"] == "yelp.com"

    def test_a_timeout_is_not_recorded(self, web, email_engine):
        web({SITE: requests.exceptions.ReadTimeout("slow")})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "timeout"
        assert site_audit.columns(contact["facts"]) == {}

    def test_a_dead_domain_is_recorded(self, web, email_engine, monkeypatch):
        monkeypatch.setattr(site_audit, "network_resolves", lambda *a, **k: True)
        web({SITE: requests.exceptions.ConnectionError("getaddrinfo failed")})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "dns"
        assert site_audit.columns(contact["facts"])

    def test_our_own_outage_is_not_blamed_on_the_practice(self, web, email_engine,
                                                          monkeypatch):
        monkeypatch.setattr(site_audit, "network_resolves", lambda *a, **k: False)
        web({SITE: requests.exceptions.ConnectionError("getaddrinfo failed")})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "connection"
        assert site_audit.columns(contact["facts"]) == {}

    def test_bot_protection_is_not_a_down_site(self, web, email_engine):
        web({SITE: Response(403, "Access denied", SITE)})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "blocked"
        assert site_audit.columns(contact["facts"]) == {}

    def test_a_server_error_is_recorded(self, web, email_engine):
        web({SITE: Response(503, "down", SITE)})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "http_error"
        assert contact["facts"]["status"] == 503

    def test_a_redirect_to_https_counts_as_https(self, web, email_engine):
        start = "http://cedargrove.example/"
        web({start: Response(200, fixture("site_modern.html"), SITE)})
        contact = email_engine.scrape_website_contact_info(start)
        assert contact["facts"]["https"] is True

    def test_a_broken_audit_keeps_the_contact_details(self, web, email_engine, monkeypatch):
        html = '<html><body><a href="mailto:hello@cedargrove.example">Email</a></body></html>'
        web({SITE: Response(200, html, SITE)})

        def boom(*_args, **_kwargs):
            raise RuntimeError("audit exploded")
        monkeypatch.setattr(site_audit, "read_homepage", boom)
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["email"] == "hello@cedargrove.example"
        assert contact["facts"]["audit_error"] == "RuntimeError"


class TestReadSite:

    def test_one_request(self, web, email_engine):
        session = web({SITE: Response(200, fixture("site_modern.html"), SITE)})
        facts, text = email_engine.read_site(SITE)
        assert session.calls == [SITE]
        assert facts["viewport"] is True and "Telehealth" in text

    def test_a_profile_costs_nothing(self, web, email_engine):
        session = web({})
        facts, _text = email_engine.read_site("https://www.thriveworks.com/counselor/x")
        assert facts["own_site"] is False and session.calls == []

    def test_no_url_no_facts(self, email_engine):
        assert email_engine.read_site("") == (None, "")


class TestFindEmailRecordsTheSiteItFound:

    def test_site_columns_are_added(self, web, email_engine, monkeypatch):
        web({SITE: Response(200, fixture("site_modern.html"), SITE)})
        monkeypatch.setattr(email_engine, "_find_website", lambda query: SITE)
        monkeypatch.setattr(email_engine, "_search_for_email_directly",
                            lambda *a, **k: "")
        found = email_engine.find_email({"Organization Name": "Cedar Grove Counseling",
                                         "City": "Austin", "State": "TX"})
        assert found["Website"] == SITE
        assert site_audit.load_facts(found["Site Facts"])["viewport"] is True
        assert found["Site Checked"]
