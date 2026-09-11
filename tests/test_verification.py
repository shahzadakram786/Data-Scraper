"""Email verification rules, with DNS and SMTP stubbed.

These tests are about the decisions, not the network: when an address is
trusted, when it is only "probably deliverable", and when a guess is discarded
rather than exported. See conftest.offline_verifier for the stub.
"""

import pytest

import verification
from verification import EmailVerifier


class TestSyntaxAndObviousJunk:

    @pytest.mark.parametrize("address", [
        "", "not-an-email", "no-at-sign.example", "two@@at.example", "trailing@",
    ])
    def test_malformed_addresses_are_invalid(self, offline_verifier, address):
        assert offline_verifier().verify(address)["status"] == verification.INVALID

    def test_disposable_providers_are_rejected(self, offline_verifier):
        result = offline_verifier().verify("someone@mailinator.com")
        assert result["status"] == verification.DISPOSABLE
        assert result["confidence"] < 10


class TestDomainChecks:

    def test_a_domain_with_no_mail_server_cannot_receive_mail(self, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {}  # nothing resolves
        assert verifier.verify("a@nowhere.example")["status"] == verification.INVALID

    def test_a_working_domain_with_a_confirmed_mailbox_is_valid(self, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        verifier.existing_mailboxes = {"jane@practice.example"}
        result = verifier.verify("jane@practice.example")
        assert result["status"] == verification.VALID
        assert result["confidence"] >= 90

    def test_a_mailbox_the_server_rejects_is_invalid(self, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        verifier.existing_mailboxes = {"jane@practice.example"}
        assert verifier.verify("nobody@practice.example")["status"] == verification.INVALID

    def test_a_catch_all_domain_cannot_confirm_anything(self, offline_verifier):
        """A server that accepts a random address accepts everything, so its
        acceptance of the real address proves nothing."""
        verifier = offline_verifier()
        verifier.mx_by_domain = {"catchall.example": ["mx.catchall.example"]}
        verifier.catch_all_domains = {"catchall.example"}
        result = verifier.verify("anything@catchall.example")
        assert result["status"] == verification.ACCEPT_ALL

    def test_a_confirmed_shared_mailbox_is_flagged_as_a_role_address(
            self, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        verifier.existing_mailboxes = {"info@practice.example"}
        assert verifier.verify("info@practice.example")["status"] == verification.ROLE


class TestFreemailAttribution:
    """The classic mis-attribution: a stranger's gmail scraped off a page that
    merely mentions the provider."""

    def test_a_mailbox_not_carrying_the_providers_name_is_risky(self, offline_verifier):
        result = offline_verifier().verify("bobsmith92@gmail.com",
                                           person_name="Jane Doe")
        assert result["status"] == verification.RISKY

    def test_a_mailbox_named_after_the_provider_is_accepted(self, offline_verifier):
        result = offline_verifier().verify("jane.doe@gmail.com",
                                           person_name="Jane Doe")
        assert result["status"] == verification.ACCEPT_ALL

    def test_without_a_name_no_judgement_is_made(self, offline_verifier):
        result = offline_verifier().verify("anything@gmail.com")
        assert result["status"] == verification.ACCEPT_ALL

    @pytest.mark.parametrize("local, name, expected", [
        ("jsmith", "Jane Smith", True),
        ("jane.smith", "Jane Smith", True),
        ("smithj", "Jane Smith", True),
        ("bobxyz", "Jane Smith", False),
        ("", "Jane Smith", False),
    ])
    def test_name_matching(self, local, name, expected):
        assert EmailVerifier._name_matches_local(local, name) is expected


class TestDegradedNetwork:
    """Most home and office ISPs block outbound port 25. The tool has to stay
    useful anyway, and has to be honest that it could not fully check."""

    def test_without_smtp_a_working_domain_is_unknown_not_valid(self, offline_verifier):
        verifier = offline_verifier(probe_smtp=False)
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        result = verifier.verify("jane@practice.example")
        assert result["status"] == verification.UNKNOWN
        assert "probing is off" in result["detail"].lower()

    def test_dead_domains_are_still_caught_without_smtp(self, offline_verifier):
        verifier = offline_verifier(probe_smtp=False)
        verifier.mx_by_domain = {}
        assert verifier.verify("a@nowhere.example")["status"] == verification.INVALID

    def test_blocked_port_25_downgrades_rather_than_failing(self, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        verifier.smtp_blocked = True
        assert verifier.verify("jane@practice.example")["status"] == verification.UNKNOWN


class TestAddressInference:
    """Inferring an address is only worth doing if the mail server confirms
    it. An unconfirmed guess is fiction and must never be exported."""

    def test_a_confirmed_pattern_is_returned(self, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        verifier.existing_mailboxes = {"jane.smith@practice.example"}
        assert verifier.guess_addresses("Jane", "Smith", "practice.example") == \
            ["jane.smith@practice.example"]

    def test_nothing_is_returned_when_no_pattern_exists(self, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        verifier.existing_mailboxes = set()
        assert verifier.guess_addresses("Jane", "Smith", "practice.example") == []

    def test_a_catch_all_domain_yields_no_guesses(self, offline_verifier):
        """Every guess would "succeed" against a catch-all, which tells us
        nothing — so guessing there would invent addresses."""
        verifier = offline_verifier()
        verifier.mx_by_domain = {"catchall.example": ["mx.catchall.example"]}
        verifier.catch_all_domains = {"catchall.example"}
        assert verifier.guess_addresses("Jane", "Smith", "catchall.example") == []

    def test_freemail_domains_are_never_guessed_at(self, offline_verifier):
        verifier = offline_verifier()
        assert verifier.guess_addresses("Jane", "Smith", "gmail.com") == []

    def test_guessing_is_off_when_smtp_is_unavailable(self, offline_verifier):
        verifier = offline_verifier(probe_smtp=False)
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        assert verifier.guess_addresses("Jane", "Smith", "practice.example") == []

    def test_a_missing_name_or_domain_yields_nothing(self, offline_verifier):
        verifier = offline_verifier()
        assert verifier.guess_addresses("", "", "practice.example") == []
        assert verifier.guess_addresses("Jane", "Smith", "") == []


class TestStatusMetadata:

    def test_every_status_has_a_confidence_and_an_explanation(self):
        for status in (verification.VALID, verification.ACCEPT_ALL, verification.ROLE,
                       verification.UNKNOWN, verification.RISKY,
                       verification.DISPOSABLE, verification.INVALID):
            assert status in verification.CONFIDENCE
            assert verification.STATUS_HELP[status].strip()

    def test_confidence_ordering_matches_intuition(self):
        confidence = verification.CONFIDENCE
        assert confidence[verification.VALID] > confidence[verification.ROLE]
        assert confidence[verification.ROLE] > confidence[verification.ACCEPT_ALL]
        assert confidence[verification.ACCEPT_ALL] > confidence[verification.UNKNOWN]
        assert confidence[verification.UNKNOWN] > confidence[verification.RISKY]
        assert confidence[verification.INVALID] == 0

    def test_only_usable_statuses_are_sendable(self):
        assert verification.INVALID not in verification.SENDABLE
        assert verification.DISPOSABLE not in verification.SENDABLE
        assert verification.RISKY not in verification.SENDABLE
        assert verification.VALID in verification.SENDABLE
