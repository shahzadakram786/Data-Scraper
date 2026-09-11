"""Outreach: compliance gating, suppression, throttling and merge rendering.

Nothing here touches a real mail server. What is being tested is the set of
safeguards — because a bug in any one of them is not a wrong number in a
spreadsheet, it is an unlawful email or a burnt sending domain.
"""

import pytest

import outreach
import storage
from pipeline import LeadPipeline, Reporter


GOOD_SETTINGS = {
    "from_name": "Shahzad Akram",
    "from_email": "shahzad@example.com",
    "postal_address": "12 Example Street, Lahore, Pakistan",
    "unsubscribe_email": "unsubscribe@example.com",
    "smtp_host": "smtp.example.com",
    "smtp_user": "shahzad@example.com",
}


def lead(**overrides):
    base = {"NPI Number": "1234567890", "Provider First Name": "Jane",
            "Provider Last Name": "Smith", "Organization Name": "Oakbend Therapy",
            "City": "Austin", "State": "TX", "Specialty": "Psychologist",
            "Source": "Psychology Today", "Email": "jane@oakbend.example",
            "Email Status": "valid", "Role": "Owner"}
    base.update(overrides)
    return base


# --------------------------------------------------------------- compliance

class TestComplianceGate:
    """US CAN-SPAM: commercial email needs a real postal address and a working
    opt-out. The liability is the sender's, so this is a hard gate."""

    def test_complete_settings_pass(self):
        assert outreach.compliance_problems(GOOD_SETTINGS) == []

    @pytest.mark.parametrize("missing", [
        "from_email", "from_name", "postal_address", "unsubscribe_email",
        "smtp_host", "smtp_user",
    ])
    def test_each_missing_field_is_caught(self, missing):
        settings = dict(GOOD_SETTINGS)
        settings[missing] = ""
        assert outreach.compliance_problems(settings), missing + " should be required"

    def test_a_malformed_unsubscribe_address_is_caught(self):
        settings = dict(GOOD_SETTINGS, unsubscribe_email="not-an-address")
        assert any("valid email" in p for p in outreach.compliance_problems(settings))

    def test_the_footer_carries_the_address_and_the_opt_out(self):
        footer = outreach.build_footer(GOOD_SETTINGS)
        assert "12 Example Street" in footer
        assert "unsubscribe@example.com" in footer

    def test_a_built_message_carries_the_unsubscribe_header(self):
        sender = outreach.EmailSender(GOOD_SETTINGS)
        message = sender.build_message("someone@example.com", "Hi", "Body",
                                       GOOD_SETTINGS)
        assert "unsubscribe@example.com" in message["List-Unsubscribe"]
        assert message["From"] == "Shahzad Akram <shahzad@example.com>"
        # The footer must be in the delivered body, not merely available.
        assert "unsubscribe@example.com" in message.get_content()

    def test_a_send_is_refused_while_settings_are_incomplete(self, store):
        pipeline = LeadPipeline(store=store, npi_engine=None, email_engine=None,
                                pt_engine=None, reporter=Reporter(), settings={})
        result = pipeline.send_campaign("Hi", "Body", dry_run=False)
        assert result.status == storage.RUN_FAILED
        assert "Cannot send yet" in result.message


# ----------------------------------------------------------------- rendering

class TestMergeRendering:

    def test_fields_are_filled_from_the_lead(self):
        body = outreach.render(
            "Hi {{First Name}}, about {{Practice}} in {{City}}, {{State}}", lead())
        assert body == "Hi Jane, about Oakbend Therapy in Austin, TX"

    def test_whitespace_inside_the_braces_is_tolerated(self):
        assert outreach.render("Hi {{  First Name  }}", lead()) == "Hi Jane"

    def test_a_missing_first_name_falls_back_rather_than_leaving_a_hole(self):
        """'Hi ,' is worse than a generic greeting."""
        body = outreach.render("Hi {{First Name}},",
                               lead(**{"Provider First Name": "",
                                       "Organization Name": "Oakbend Therapy"}))
        assert body == "Hi Oakbend,"

    def test_an_unknown_field_is_left_visible_not_silently_blanked(self):
        """A typo must show up in the preview instead of going out as a gap."""
        assert "{{Nickname}}" in outreach.render("Hi {{Nickname}}", lead())

    def test_unknown_fields_are_reported_before_sending(self):
        assert outreach.unresolved_fields("Hi {{Nickname}} at {{Practice}}") == \
            ["Nickname"]
        assert outreach.unresolved_fields("Hi {{First Name}}") == []

    def test_a_campaign_with_a_bad_merge_field_is_refused(self, store):
        pipeline = LeadPipeline(store=store, npi_engine=None, email_engine=None,
                                pt_engine=None, reporter=Reporter(),
                                settings=GOOD_SETTINGS)
        result = pipeline.send_campaign("Hi {{Nickname}}", "Body", dry_run=False)
        assert result.status == storage.RUN_FAILED
        assert "Nickname" in result.message


# --------------------------------------------------------------- suppression

class TestSuppression:

    def test_an_address_can_be_blocked_permanently(self, store):
        store.suppress("jane@oakbend.example", "asked to be removed")
        assert store.is_suppressed("jane@oakbend.example")

    def test_matching_ignores_case_and_spacing(self, store):
        store.suppress("jane@oakbend.example")
        assert store.is_suppressed("  JANE@Oakbend.Example  ")

    def test_a_whole_domain_can_be_blocked(self, store):
        store.suppress("@oakbend.example", "client asked")
        assert store.is_suppressed("anyone@oakbend.example")
        assert not store.is_suppressed("someone@elsewhere.example")

    def test_junk_is_not_accepted_onto_the_list(self, store):
        assert not store.suppress("not-an-address")
        assert not store.suppress("")

    def test_an_empty_address_counts_as_unsendable(self, store):
        assert store.is_suppressed("")

    def test_removal_works(self, store):
        store.suppress("jane@oakbend.example")
        store.unsuppress("jane@oakbend.example")
        assert not store.is_suppressed("jane@oakbend.example")

    def test_suppressed_leads_are_excluded_from_a_campaign(self, store):
        store.add_leads_batch([lead()])
        store.suppress("jane@oakbend.example", "opted out")
        pipeline = LeadPipeline(store=store, npi_engine=None, email_engine=None,
                                pt_engine=None, reporter=Reporter(),
                                settings=GOOD_SETTINGS)
        recipients, excluded = pipeline.recipients_for()
        assert recipients == []
        assert any("opted out" in reason for reason in excluded)


# ------------------------------------------------------------ who gets sent to

class TestAudienceSelection:

    def _pipeline(self, store):
        return LeadPipeline(store=store, npi_engine=None, email_engine=None,
                            pt_engine=None, reporter=Reporter(),
                            settings=GOOD_SETTINGS)

    def test_a_verified_address_is_included(self, store):
        store.add_leads_batch([lead()])
        recipients, _ = self._pipeline(store).recipients_for()
        assert len(recipients) == 1

    @pytest.mark.parametrize("status", ["invalid", "disposable", "risky"])
    def test_addresses_that_would_bounce_are_never_sent_to(self, store, status):
        """Not even with allow_unverified — these are known bad."""
        store.add_leads_batch([lead(**{"Email Status": status})])
        recipients, _ = self._pipeline(store).recipients_for(allow_unverified=True)
        assert recipients == []

    def test_unverified_addresses_are_held_back_by_default(self, store):
        store.add_leads_batch([lead(**{"Email Status": ""})])
        recipients, excluded = self._pipeline(store).recipients_for()
        assert recipients == []
        assert any("not verified" in reason for reason in excluded)

    def test_unverified_addresses_can_be_included_deliberately(self, store):
        """On a network that blocks port 25 everything is unverified, so this
        has to be possible — with the bounce risk made explicit."""
        store.add_leads_batch([lead(**{"Email Status": ""})])
        recipients, _ = self._pipeline(store).recipients_for(allow_unverified=True)
        assert len(recipients) == 1

    def test_leads_with_no_address_are_reported_as_such(self, store):
        store.add_leads_batch([lead(Email="", Phone="(512) 471-3300")])
        recipients, excluded = self._pipeline(store).recipients_for()
        assert recipients == []
        assert excluded.get("no email address") == 1

    def test_nobody_is_contacted_twice(self, store):
        store.add_leads_batch([lead()])
        stored = next(store.iter_leads())
        store.record_send(stored["_id"], stored["Email"], "c1", "Hi", "sent")
        recipients, excluded = self._pipeline(store).recipients_for()
        assert recipients == []
        assert excluded.get("already contacted") == 1

    def test_a_failed_send_does_not_count_as_contacted(self, store):
        """A message that never arrived must not silently burn the lead."""
        store.add_leads_batch([lead()])
        stored = next(store.iter_leads())
        store.record_send(stored["_id"], stored["Email"], "c1", "Hi", "failed",
                          "temporary failure")
        recipients, _ = self._pipeline(store).recipients_for()
        assert len(recipients) == 1

    def test_the_same_address_under_two_lead_records_is_still_one_person(self, store):
        """Two sources can produce two rows for one human. Emailing them twice
        in a week is how a prospect becomes a spam complaint."""
        store.add_leads_batch([lead()])
        stored = next(store.iter_leads())
        store.record_send(stored["_id"], stored["Email"], "c1", "Hi", "sent")
        assert store.already_contacted(lead_id=999, email="jane@oakbend.example")

    def test_the_limit_is_respected(self, store):
        store.add_leads_batch([
            lead(**{"NPI Number": "111111111{0}".format(i),
                    "Email": "jane{0}@oakbend.example".format(i)})
            for i in range(5)])
        recipients, _ = self._pipeline(store).recipients_for(limit=2)
        assert len(recipients) == 2


# ----------------------------------------------------------------- send flow

class TestSendFlow:

    def _pipeline(self, store, sent_log, fail_with=None):
        class FakeSender:
            def __init__(self, settings, logger=None):
                pass

            def send(self, to, subject, body):
                sent_log.append((to, subject, body))
                if fail_with:
                    return False, fail_with
                return True, "sent"

            def close(self):
                pass

        pipeline = LeadPipeline(store=store, npi_engine=None, email_engine=None,
                                pt_engine=None, reporter=Reporter(),
                                settings=GOOD_SETTINGS)
        outreach.EmailSender_original = outreach.EmailSender
        outreach.EmailSender = FakeSender
        return pipeline

    def teardown_method(self):
        if hasattr(outreach, "EmailSender_original"):
            outreach.EmailSender = outreach.EmailSender_original
            del outreach.EmailSender_original

    def test_a_dry_run_sends_nothing(self, store):
        store.add_leads_batch([lead()])
        sent = []
        pipeline = self._pipeline(store, sent)
        result = pipeline.send_campaign("Hi {{First Name}}", "Body", dry_run=True)
        assert sent == []
        assert result.status == storage.RUN_OK
        assert store.outreach_stats()["sent"] == 0

    def test_a_real_send_records_and_advances_the_lead(self, store):
        store.add_leads_batch([lead()])
        sent = []
        pipeline = self._pipeline(store, sent)
        result = pipeline.send_campaign("Hi {{First Name}}", "Body {{Practice}}",
                                        min_seconds=0, max_seconds=0)
        assert result.status == storage.RUN_OK
        assert len(sent) == 1
        assert sent[0][1] == "Hi Jane"
        assert "Oakbend Therapy" in sent[0][2]
        assert store.outreach_stats()["sent"] == 1
        assert next(store.iter_leads())["Outreach Status"] == "Contacted"

    def test_a_hard_bounce_suppresses_the_address(self, store):
        store.add_leads_batch([lead()])
        pipeline = self._pipeline(store, [], fail_with="rejected: 550 no such user")
        pipeline.send_campaign("Hi", "Body", min_seconds=0, max_seconds=0)
        assert store.is_suppressed("jane@oakbend.example")

    def test_a_temporary_failure_does_not_suppress(self, store):
        """A busy server must not cost the lead forever."""
        store.add_leads_batch([lead()])
        pipeline = self._pipeline(store, [],
                                  fail_with="temporary failure: connection reset")
        pipeline.send_campaign("Hi", "Body", min_seconds=0, max_seconds=0)
        assert not store.is_suppressed("jane@oakbend.example")

    def test_the_daily_cap_stops_a_send(self, store):
        store.add_leads_batch([lead()])
        stored = next(store.iter_leads())
        for i in range(3):
            store.record_send(stored["_id"], "other{0}@x.example".format(i),
                              "c", "s", "sent")
        sent = []
        pipeline = self._pipeline(store, sent)
        result = pipeline.send_campaign("Hi", "Body", daily_cap=3,
                                        min_seconds=0, max_seconds=0)
        assert sent == []
        assert result.status == storage.RUN_STOPPED
        assert "daily cap" in result.message

    def test_an_empty_audience_is_reported_not_treated_as_success(self, store):
        sent = []
        pipeline = self._pipeline(store, sent)
        result = pipeline.send_campaign("Hi", "Body", min_seconds=0, max_seconds=0)
        assert result.status == storage.RUN_EMPTY


class TestFailureClassification:

    @pytest.mark.parametrize("detail", [
        "rejected: {'a@b.com': (550, b'No such user')}",
        "rejected by server: 554 delivery error",
        "rejected: 551 user does not exist",
    ])
    def test_permanent_failures_are_recognised(self, detail):
        assert outreach.is_permanent_failure(detail)

    @pytest.mark.parametrize("detail", [
        "temporary failure: [Errno 104] Connection reset by peer",
        "temporary failure: timed out",
        "",
    ])
    def test_temporary_failures_are_not_treated_as_permanent(self, detail):
        assert not outreach.is_permanent_failure(detail)


class TestThrottle:

    def test_the_first_send_is_not_delayed(self):
        throttle = outreach.Throttle(min_seconds=10, max_seconds=10)
        import time
        start = time.time()
        assert throttle.wait()
        assert time.time() - start < 1

    def test_a_stop_request_interrupts_the_wait(self):
        import threading
        stop = threading.Event()
        throttle = outreach.Throttle(min_seconds=30, max_seconds=30, stop_event=stop)
        throttle.wait()          # first call just arms the clock
        stop.set()
        assert throttle.wait() is False

    def test_the_delay_range_is_sane(self):
        """max below min would make random.uniform raise mid-campaign."""
        throttle = outreach.Throttle(min_seconds=90, max_seconds=10)
        assert throttle.max_seconds >= throttle.min_seconds
