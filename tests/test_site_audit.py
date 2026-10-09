"""site_audit: how a homepage fetch went, what the page shows, and how both
are stored on a lead.

The rule the plumbing tests protect: an outcome that says nothing about the
practice — a timeout, bot protection, this machine's own dropped connection —
is never recorded, so it can never be read later as "their site is down".
"""

import json

import pytest
import requests

import site_audit


class TestFetchOutcomes:

    @pytest.mark.parametrize("exc, expected", [
        (requests.exceptions.SSLError("certificate verify failed"), "ssl"),
        (requests.exceptions.ConnectTimeout("timed out"), "timeout"),
        (requests.exceptions.ReadTimeout("read timed out"), "timeout"),
        (requests.exceptions.ConnectionError("[Errno 11001] getaddrinfo failed"), "dns"),
        (requests.exceptions.ConnectionError("Name or service not known"), "dns"),
        (requests.exceptions.ConnectionError("Connection refused"), "connection"),
        (requests.exceptions.TooManyRedirects("loop"), "connection"),
    ])
    def test_exceptions_are_graded(self, exc, expected):
        assert site_audit.classify_fetch_error(exc) == expected

    @pytest.mark.parametrize("status, expected", [
        (200, "ok"), (404, "http_error"), (410, "http_error"), (500, "http_error"),
        (503, "http_error"), (401, "blocked"), (403, "blocked"), (429, "blocked"),
    ])
    def test_statuses_are_graded(self, status, expected):
        assert site_audit.classify_status(status) == expected

    def test_bot_protection_is_never_evidence(self):
        assert site_audit.FETCH_BLOCKED in site_audit.TRANSIENT_FETCHES

    def test_the_canary_reports_a_dead_connection(self, monkeypatch):
        def fail(*_args, **_kwargs):
            raise OSError("no network")
        monkeypatch.setattr(site_audit.socket, "getaddrinfo", fail)
        assert site_audit.network_resolves() is False

    def test_the_canary_reports_a_live_connection(self, monkeypatch):
        monkeypatch.setattr(site_audit.socket, "getaddrinfo",
                            lambda *_a, **_k: [object()])
        assert site_audit.network_resolves() is True


class TestStoredText:

    def test_round_trip(self):
        text = "Telehealth and in-person sessions across Austin."
        assert site_audit.unpack_text(site_audit.pack_text(text)) == text

    def test_empty_stays_empty(self):
        assert site_audit.pack_text("") == ""
        assert site_audit.unpack_text("") == ""

    @pytest.mark.parametrize("damaged", ["not base64 at all!", "aGVsbG8=", "%%%%"])
    def test_damaged_text_reads_as_empty(self, damaged):
        assert site_audit.unpack_text(damaged) == ""


class TestFacts:

    def test_facts_round_trip_through_json(self):
        facts = site_audit.base_facts(True, "ok", 200)
        assert site_audit.load_facts(site_audit.dump_facts(facts)) == facts

    @pytest.mark.parametrize("value", [None, "", "not json", "[1, 2]", 42])
    def test_unreadable_facts_are_empty(self, value):
        assert site_audit.load_facts(value) == {}

    def test_a_dict_is_taken_as_is(self):
        assert site_audit.load_facts({"own_site": False}) == {"own_site": False}

    def test_no_site_facts_name_the_profile(self):
        facts = site_audit.no_site_facts(site_audit.NO_SITE_PROFILE, "growtherapy.com")
        assert facts["own_site"] is False
        assert facts["why"] == "profile"
        assert facts["profile_host"] == "growtherapy.com"

    def test_a_failed_audit_names_the_error(self):
        facts = site_audit.failed_audit("ok", ValueError("bad markup"))
        assert facts["audit_error"] == "ValueError"
        assert facts["own_site"] is True
        assert facts["fetch"] == "ok"


class TestColumns:

    def test_a_read_is_recorded_with_its_date(self):
        facts = site_audit.base_facts(True, "ok", 200)
        cols = site_audit.columns(facts, "hello")
        assert json.loads(cols["Site Facts"]) == facts
        assert site_audit.unpack_text(cols["Site Text"]) == "hello"
        assert cols["Site Checked"] == facts["checked"]

    @pytest.mark.parametrize("fetch", ["timeout", "blocked", "connection"])
    def test_transient_outcomes_are_not_recorded(self, fetch):
        assert site_audit.columns(site_audit.base_facts(True, fetch)) == {}

    @pytest.mark.parametrize("fetch", ["dns", "http_error", "ssl"])
    def test_real_failures_are_recorded(self, fetch):
        assert site_audit.columns(site_audit.base_facts(True, fetch))

    def test_no_facts_records_nothing(self):
        assert site_audit.columns(None) == {}

    def test_no_site_is_recorded(self):
        assert site_audit.columns(site_audit.no_site_facts(site_audit.NO_SITE_LISTED))


from datetime import date

from conftest import fixture

TODAY = date(2026, 10, 9)
MODERN_URL = "https://cedargrove.example/"
DATED_URL = "http://oakpractice.example/"

MODERN = {
    "https": True, "viewport": True, "copyright_year": 2025, "title": True,
    "meta_description": True, "h1": True, "schema_org": True,
    "analytics": ["google-analytics"], "ad_pixels": ["meta"],
    "booking": ["simplepractice"], "portal": True, "contact_form": True,
    "newsletter": ["mailchimp"], "blog": True, "privacy_policy": True,
    "hipaa_notice": True, "terms": True, "accessibility": True, "logo": True,
    "favicon": True, "share_image": True, "video": True, "live_chat": ["tawk"],
    "images": 4, "images_missing_alt": 0, "wordpress_version": None, "builder": "",
}
DATED = {
    "https": False, "viewport": False, "copyright_year": 2016, "title": False,
    "meta_description": False, "h1": False, "schema_org": False, "analytics": [],
    "ad_pixels": [], "booking": [], "portal": False, "contact_form": False,
    "newsletter": [], "blog": False, "privacy_policy": False, "hipaa_notice": False,
    "terms": False, "accessibility": False, "logo": False, "favicon": False,
    "share_image": False, "video": False, "live_chat": [], "images": 4,
    "images_missing_alt": 4, "wordpress_version": "4.9.8", "builder": "wordpress",
}


def read(html, url=MODERN_URL, **kwargs):
    return site_audit.read_homepage(html, url, today=TODAY, **kwargs)


class TestReadingAHomepage:

    @pytest.mark.parametrize("key", sorted(MODERN))
    def test_a_modern_site(self, key):
        facts, _text = read(fixture("site_modern.html"), MODERN_URL)
        assert facts[key] == MODERN[key], key

    @pytest.mark.parametrize("key", sorted(DATED))
    def test_a_dated_site(self, key):
        facts, _text = read(fixture("site_dated.html"), DATED_URL)
        assert facts[key] == DATED[key], key

    def test_a_read_site_is_their_own_and_ok(self):
        facts, _text = read(fixture("site_modern.html"))
        assert facts["own_site"] is True
        assert facts["fetch"] == "ok"
        assert facts["v"] == site_audit.SCHEMA_VERSION
        assert facts["checked"] == "2026-10-09"
        assert "audit_error" not in facts

    def test_the_text_is_what_a_visitor_reads(self):
        _facts, text = read(fixture("site_modern.html"))
        assert "Telehealth and in-person sessions" in text
        assert "dataLayer" not in text, "scripts are not visible text"

    def test_a_failed_fetch_records_only_the_outcome(self):
        facts, text = read("", fetch="timeout", status=None)
        assert facts["fetch"] == "timeout"
        assert "viewport" not in facts
        assert text == ""

    def test_an_empty_page_is_not_evidence_of_anything(self):
        facts, _text = read("   ")
        assert facts["audit_error"] == "EmptyPage"

    def test_garbage_does_not_raise(self):
        facts, _text = read("<<<>>> not html <div")
        assert facts["own_site"] is True
        assert "audit_error" not in facts

    def test_a_parser_crash_costs_the_facts_not_the_call(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("parser exploded")
        monkeypatch.setattr(site_audit, "BeautifulSoup", boom)
        facts, text = read(fixture("site_modern.html"))
        assert facts["audit_error"] == "RuntimeError"
        assert text == ""

    def test_an_enormous_page_is_cut_short(self):
        html = "<html><body><p>" + ("a" * (site_audit.HTML_CAP + 50_000)) + "</p></body></html>"
        facts, text = read(html)
        assert "images" in facts
        assert len(text) == site_audit.TEXT_CAP


class TestCopyright:

    def test_a_range_counts_as_its_last_year(self):
        assert site_audit.newest_copyright_year("© 2012-2016 Oak Street", 2026) == 2016

    def test_the_newest_of_several_wins(self):
        text = "Copyright 2019 Smith Therapy · © 2023 Smith Therapy"
        assert site_audit.newest_copyright_year(text, 2026) == 2023

    def test_a_future_year_is_not_a_date(self):
        assert site_audit.newest_copyright_year("© 2099 Acme", 2026) is None

    def test_a_phone_number_is_not_a_year(self):
        text = "Copyright Smith Therapy (512) 555-2020"
        assert site_audit.newest_copyright_year(text, 2026) is None

    def test_a_year_set_by_javascript_is_no_year(self):
        html = ('<html><body><h1>Hi</h1><footer>© <span id="year"></span> Smith '
                'Therapy<script>document.getElementById("year").textContent='
                'new Date().getFullYear()</script></footer></body></html>')
        facts, _text = read(html)
        assert facts["copyright_year"] is None

    def test_the_footer_is_read_before_the_text_is_capped(self):
        html = ("<html><body><p>" + ("word " * 4000) + "</p>"
                "<footer>© 2017 Long Page Practice</footer></body></html>")
        facts, text = read(html)
        assert facts["copyright_year"] == 2017
        assert len(text) == site_audit.TEXT_CAP


class TestWordPress:

    def test_the_version_from_a_core_asset(self):
        html = ('<html><head><script src="/wp-includes/js/wp-emoji-release.min.js'
                '?ver=5.8.2"></script></head><body><h1>x</h1></body></html>')
        facts, _text = read(html)
        assert facts["wordpress_version"] == "5.8.2"

    def test_bundled_jquery_is_not_the_wordpress_version(self):
        html = ('<html><head><script src="/wp-includes/js/jquery/jquery.min.js'
                '?ver=3.7.1"></script></head><body><h1>x</h1></body></html>')
        facts, _text = read(html)
        assert facts["wordpress_version"] is None

    def test_a_wix_site_is_named(self):
        html = '<html><head><link href="https://static.wixstatic.com/x.css"></head><body></body></html>'
        facts, _text = read(html)
        assert facts["builder"] == "wix"


class TestSignupForms:

    def test_a_plain_signup_form_counts_as_a_newsletter(self):
        html = ('<html><body><form action="/join"><input type="email" '
                'placeholder="Your email"><button>Subscribe</button></form></body></html>')
        facts, _text = read(html)
        assert facts["newsletter"] == ["signup-form"]

    def test_a_message_form_is_not_a_newsletter(self):
        html = ('<html><body><form><input type="email"><textarea></textarea>'
                '<button>Subscribe to updates</button></form></body></html>')
        facts, _text = read(html)
        assert facts["newsletter"] == []
        assert facts["contact_form"] is True
