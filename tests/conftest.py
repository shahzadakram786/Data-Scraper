"""Shared test fixtures.

The project root goes on sys.path so the tests import the same modules the app
does, without needing an installed package.
"""

import os
import sys
import logging
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    """Read a saved page. These are the real markup shapes each parser has to
    cope with — checking them in is what turns "did I break the phone
    classifier?" from a 40-minute scrape into a two-second test."""
    with open(os.path.join(FIXTURE_DIR, name), "r", encoding="utf-8") as handle:
        return handle.read()


def healthy_site_facts(today=None, **overrides):
    """Facts for a homepage that was read and lacks nothing any lead type
    looks for. Tests take one thing away and watch exactly one type light up."""
    import site_audit
    facts = site_audit.base_facts(True, site_audit.FETCH_OK, 200, today)
    facts.update({
        "https": True, "viewport": True, "copyright_year": 2025, "title": True,
        "meta_description": True, "h1": True, "schema_org": True,
        "analytics": ["google-analytics"], "ad_pixels": ["meta"],
        "booking": ["simplepractice"], "portal": True, "contact_form": True,
        "newsletter": ["mailchimp"], "blog": True, "privacy_policy": True,
        "hipaa_notice": True, "terms": True, "accessibility": True, "logo": True,
        "favicon": True, "share_image": True, "video": True, "live_chat": ["tawk"],
        "images": 4, "images_missing_alt": 0, "wordpress_version": None,
        "builder": "",
        # Read by the collection scrape, which also looks for the role and
        # social links. A Check websites read does not.
        "contact_scraped": True,
    })
    facts.update(overrides)
    return facts


@pytest.fixture(autouse=True)
def isolated_network_memory(tmp_path, monkeypatch):
    """Keep every test away from the real .cache/network.json, and from anything
    an earlier test taught the verifier about the network.

    Without this, a machine whose port 25 is blocked would make verification
    tests behave differently from a machine where it is open.
    """
    from verification import EmailVerifier
    monkeypatch.setattr(EmailVerifier, "NETWORK_CACHE_PATH",
                        str(tmp_path / "network.json"))
    monkeypatch.setattr(EmailVerifier, "_network_state", None)


@pytest.fixture
def quiet_logger():
    logger = logging.getLogger("test")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


@pytest.fixture
def email_engine(quiet_logger):
    from email_engine import EmailEngine
    return EmailEngine(logger=quiet_logger)


@pytest.fixture
def pt_engine(quiet_logger):
    from psychology_today_engine import PsychologyTodayEngine
    return PsychologyTodayEngine(logger=quiet_logger)


@pytest.fixture
def store(tmp_path, quiet_logger):
    from storage import LeadStore
    return LeadStore(str(tmp_path / "test.db"), logger=quiet_logger)


@pytest.fixture
def offline_verifier(quiet_logger):
    """A verifier that never touches the network.

    DNS and SMTP are stubbed so the rules being tested — role detection, name
    matching, catch-all handling — are tested rather than the internet.
    """
    from verification import EmailVerifier

    class OfflineVerifier(EmailVerifier):
        def __init__(self, **kwargs):
            super().__init__(logger=quiet_logger, **kwargs)
            self.mx_by_domain = {}
            self.catch_all_domains = set()
            self.existing_mailboxes = set()
            self._preflight_done = True   # pretend port 25 is open

        def _lookup_mx(self, domain):
            return self.mx_by_domain.get(domain, [])

        def _smtp_reachable(self):
            return not self.smtp_blocked

        def _rcpt_accepted(self, domain, mx_hosts, email):
            if domain in self.catch_all_domains:
                return True
            return email.lower() in self.existing_mailboxes

    return OfflineVerifier
