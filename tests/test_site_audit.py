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
