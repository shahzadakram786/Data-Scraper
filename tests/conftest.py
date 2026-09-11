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
