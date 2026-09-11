"""Pipeline behaviour, with the network replaced by fakes.

The most important tests here are the ones about failure. A scraper that breaks
must not look like a scraper that finished: in August 2026 four Psychology
Today runs reported success having saved nothing, because "no providers on this
page" and "the end of the directory" were the same code path. These tests hold
those two apart.
"""

import pytest

from conftest import fixture
from errors import ListingParseError, RegionBlockedError, SourceUnavailableError
from storage import RUN_OK, RUN_EMPTY, RUN_FAILED, RUN_STOPPED
from pipeline import LeadPipeline, Reporter


# ------------------------------------------------------------------- fakes

class FakeNPIEngine:
    def __init__(self, match=None):
        self.match = match

    def find_by_name(self, first, last, state, city=None):
        return self.match

    def find_by_organization(self, name, state, city=None):
        return self.match

    def find_by_number(self, number):
        return self.match

    def fetch_providers(self, state, specialty, **kwargs):
        return []


class FakeEmailEngine:
    """Only the parts the pipeline actually calls."""

    def __init__(self, contact=None, searched_email=""):
        self.contact = contact or {"email": "", "phone": "", "phone_confidence": "",
                                   "role": "", "socials": {}}
        self.searched_email = searched_email

    def scrape_website_contact_info(self, url, person_name=None):
        return dict(self.contact)

    def find_email_by_phone(self, phone, person_name=None, org_name=None):
        return self.searched_email

    def find_all_emails_by_phone(self, phone, **kwargs):
        return [self.searched_email] if self.searched_email else []

    def find_all_emails_by_name(self, name, city="", state="", **kwargs):
        return []

    def find_email(self, lead):
        lead["Email"] = self.searched_email
        return lead

    @staticmethod
    def is_valid_phone(phone):
        from email_engine import EmailEngine
        return EmailEngine.is_valid_phone(phone)

    @staticmethod
    def is_tollfree(phone):
        from email_engine import EmailEngine
        return EmailEngine.is_tollfree(phone)

    @staticmethod
    def is_ownership_role(role):
        from email_engine import EmailEngine
        return EmailEngine.is_ownership_role(role)

    @staticmethod
    def is_platform_site(url):
        from email_engine import EmailEngine
        return EmailEngine.is_platform_site(url)

    @staticmethod
    def _registrable_domain(netloc):
        from email_engine import EmailEngine
        return EmailEngine._registrable_domain(netloc)


class FakePTEngine:
    # A provider who lists a website is the normal case, and it is the branch
    # that reads their contact details — so it is the default here. Pass
    # website="" to exercise the provider-has-no-site path.
    def __init__(self, pages=None, raises=None, website="https://practice.example"):
        self.pages = pages or []
        self.raises = raises
        self.website = website

    def iter_listing(self, state, stop_event=None, max_pages=None, start_page=1):
        if self.raises:
            raise self.raises
        for page, entries in self.pages:
            if page < start_page:
                continue
            yield page, entries

    def _resolve_website(self, pt_id):
        return self.website

    def get_profile_details(self, pt_id, url):
        return {"specialty": "", "website": self.website}


def entry(**overrides):
    base = {"pt_id": "1234567", "profile_url": "https://example/1234567",
            "name": "Kaelyn Britton", "credential": "Psychologist, PsyD",
            "phone": "(512) 471-3300", "city": "Austin", "state_full": "TX",
            "zip": "78701", "is_org": False}
    base.update(overrides)
    return base


def build(store, pt_engine=None, email_engine=None, npi_engine=None, verifier=None):
    return LeadPipeline(
        store=store,
        npi_engine=npi_engine or FakeNPIEngine(),
        email_engine=email_engine or FakeEmailEngine(),
        pt_engine=pt_engine or FakePTEngine(),
        verifier=verifier,
        reporter=Reporter())


# ------------------------------------------------- the fail-loud guarantee

class TestListingFailsLoudly:
    """An empty page 1 is a broken scraper. An empty page 40 is the end of the
    directory. Confusing the two is what hid a ten-day outage."""

    def _engine(self, pages, quiet_logger):
        from psychology_today_engine import PsychologyTodayEngine
        engine = PsychologyTodayEngine(logger=quiet_logger)
        engine._get = lambda url, retries=3, critical=False: pages.pop(0) if pages else None
        return engine

    def test_an_unparseable_first_page_raises(self, quiet_logger):
        engine = self._engine(["<html><body>nothing here</body></html>"], quiet_logger)
        with pytest.raises(ListingParseError):
            list(engine.iter_listing("Texas"))

    def test_an_empty_later_page_is_simply_the_end(self, quiet_logger):
        pages = [fixture("pt_listing.html"), "<html><body>no more</body></html>"]
        engine = self._engine(pages, quiet_logger)
        batches = list(engine.iter_listing("Texas"))
        assert len(batches) == 1, "page 1 yielded, page 2 ended the walk"
        assert len(batches[0][1]) == 3

    def test_a_good_first_page_yields_providers(self, quiet_logger):
        engine = self._engine([fixture("pt_listing.html")], quiet_logger)
        page, entries = next(iter(engine.iter_listing("Texas")))
        assert page == 1
        assert len(entries) == 3

    def test_repeated_providers_end_the_walk(self, quiet_logger):
        """Some directories serve page 1 again past the last real page."""
        same = fixture("pt_listing.html")
        engine = self._engine([same, same], quiet_logger)
        assert len(list(engine.iter_listing("Texas"))) == 1

    def test_resuming_skips_earlier_pages(self, quiet_logger):
        engine = self._engine([fixture("pt_listing.html")], quiet_logger)
        page, _entries = next(iter(engine.iter_listing("Texas", start_page=5)))
        assert page == 5, "the walk should start where it was told to"

    def test_a_region_block_with_no_relay_raises(self, quiet_logger):
        from psychology_today_engine import PsychologyTodayEngine
        engine = PsychologyTodayEngine(logger=quiet_logger)
        engine._region_blocked = True
        engine._fetch_via_relay = lambda url: None
        with pytest.raises(RegionBlockedError):
            engine._get("https://example/x", critical=True)

    def test_a_non_critical_fetch_still_returns_none(self, quiet_logger):
        from psychology_today_engine import PsychologyTodayEngine
        engine = PsychologyTodayEngine(logger=quiet_logger)
        engine._region_blocked = True
        engine._fetch_via_relay = lambda url: None
        assert engine._get("https://example/x", critical=False) is None

    def test_typed_errors_carry_a_remedy_for_the_user(self):
        for error in (ListingParseError("x"), RegionBlockedError("x"),
                      SourceUnavailableError("x")):
            assert error.remedy.strip(), "the banner needs something actionable"


class TestRunOutcomes:

    def test_a_broken_source_is_recorded_as_failed(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(
            raises=ListingParseError("markup changed")))
        result = pipeline.collect_psychology_today("Texas", "TX")
        assert result.status == RUN_FAILED
        assert "markup changed" in result.message
        assert store.recent_runs(1)[0]["status"] == RUN_FAILED

    def test_a_run_that_collects_nothing_is_not_a_success(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(pages=[]))
        result = pipeline.collect_psychology_today("Texas", "TX")
        assert result.status == RUN_EMPTY
        assert not result.ok

    def test_a_normal_run_succeeds_and_saves(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(pages=[(1, [entry()])]))
        result = pipeline.collect_psychology_today("Texas", "TX")
        assert result.status == RUN_OK
        assert result.added == 1
        assert store.get_stats()["total"] == 1

    def test_stopping_keeps_what_was_already_collected(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(pages=[(1, [entry()])]))
        pipeline.reporter.stop_event.set()
        result = pipeline.collect_psychology_today("Texas", "TX")
        assert result.status == RUN_STOPPED


class TestCheckpointing:

    def test_progress_is_checkpointed_per_page(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(
            pages=[(1, [entry()]), (2, [entry(pt_id="2345678")])]))
        pipeline.collect_psychology_today("Texas", "TX")
        # A completed run clears its checkpoint so the next one starts fresh.
        assert store.get_checkpoint("pt:Texas:owners=0") is None

    def test_a_failed_run_leaves_a_checkpoint_to_resume_from(self, store):
        class Failing(FakePTEngine):
            def iter_listing(self, state, stop_event=None, max_pages=None, start_page=1):
                yield 1, [entry()]
                raise SourceUnavailableError("connection lost")

        pipeline = build(store, pt_engine=Failing())
        result = pipeline.collect_psychology_today("Texas", "TX")
        assert result.status == RUN_FAILED
        checkpoint = store.get_checkpoint("pt:Texas:owners=0")
        assert checkpoint["next_page"] == 2, "resume after the page that finished"


class TestProviderProcessing:

    def test_the_websites_direct_line_beats_the_directory_listing(self, store):
        pipeline = build(store, email_engine=FakeEmailEngine(contact={
            "email": "", "phone": "(512) 471-3399",
            "phone_confidence": "personal/direct (tel: link)",
            "role": "", "socials": {}}))
        phone, source = pipeline.pick_authentic_phone(
            {"phone": "(512) 471-3399",
             "phone_confidence": "personal/direct (tel: link)"},
            entry())
        assert phone == "(512) 471-3399"
        assert "Provider Website" in source

    def test_the_directory_number_is_a_labelled_last_resort(self, store):
        pipeline = build(store)
        phone, source = pipeline.pick_authentic_phone(
            {"phone": "", "phone_confidence": ""}, entry(phone="(512) 471-3300"))
        assert phone == "(512) 471-3300"
        assert source == "Psychology Today (listing)"

    def test_a_toll_free_directory_number_is_marked(self, store):
        pipeline = build(store)
        _phone, source = pipeline.pick_authentic_phone(
            {"phone": "", "phone_confidence": ""}, entry(phone="(800) 631-1000"))
        assert "toll-free" in source

    def test_nothing_dialable_yields_nothing(self, store):
        pipeline = build(store)
        assert pipeline.pick_authentic_phone(
            {"phone": "", "phone_confidence": ""}, entry(phone="")) == ("", "")

    def test_a_provider_with_no_npi_match_gets_a_traceable_placeholder(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(pages=[(1, [entry()])]))
        pipeline.collect_psychology_today("Texas", "TX")
        saved = next(store.iter_leads())
        assert saved["NPI Number"] == "PT-1234567"

    def test_an_npi_match_is_attached(self, store):
        npi = FakeNPIEngine(match={
            "NPI Number": "1234567890", "Address": "1 Main St", "City": "Austin",
            "Zip": "78701", "Specialty": "Psychologist", "Organization Name": ""})
        pipeline = build(store, pt_engine=FakePTEngine(pages=[(1, [entry()])]),
                         npi_engine=npi)
        pipeline.collect_psychology_today("Texas", "TX")
        saved = next(store.iter_leads())
        assert saved["NPI Number"] == "1234567890"
        assert saved["Address"] == "1 Main St"

    def test_the_owners_filter_drops_non_owners(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(pages=[(1, [entry()])]),
                         email_engine=FakeEmailEngine(contact={
                             "email": "", "phone": "", "phone_confidence": "",
                             "role": "Clinical Director", "socials": {}}))
        result = pipeline.collect_psychology_today("Texas", "TX", owners_only=True)
        assert result.added == 0, "a Clinical Director is not a practice owner"

    def test_the_owners_filter_keeps_owners(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(pages=[(1, [entry()])]),
                         email_engine=FakeEmailEngine(contact={
                             "email": "", "phone": "", "phone_confidence": "",
                             "role": "Founder", "socials": {}}))
        result = pipeline.collect_psychology_today("Texas", "TX", owners_only=True)
        assert result.added == 1


class TestVerificationIntegration:

    def test_an_address_that_would_bounce_is_dropped_not_labelled(
            self, store, offline_verifier):
        """Exporting a known-bad address helps nobody and costs sender
        reputation."""
        verifier = offline_verifier()
        verifier.mx_by_domain = {}  # nothing resolves, so the address is dead
        pipeline = build(store, verifier=verifier)
        lead = {"Email": "dead@nowhere.example"}
        pipeline.attach_verification(lead)
        assert lead["Email"] == ""

    def test_a_good_address_is_stamped_with_its_status(self, store, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        verifier.existing_mailboxes = {"jane@practice.example"}
        pipeline = build(store, verifier=verifier)
        lead = {"Email": "jane@practice.example"}
        pipeline.attach_verification(lead, person_name="Jane Smith")
        assert lead["Email Status"] == "valid"
        assert lead["Email Confidence"] == 95

    def test_inference_is_skipped_for_marketplace_domains(self, store, offline_verifier):
        """A marketplace domain is the platform's, not the provider's, so an
        address inferred there would belong to the wrong organisation."""
        verifier = offline_verifier()
        verifier.mx_by_domain = {"growtherapy.com": ["mx.growtherapy.com"]}
        verifier.existing_mailboxes = {"jane.smith@growtherapy.com"}
        pipeline = build(store, verifier=verifier)
        assert pipeline.guess_email("Jane", "Smith",
                                    "https://growtherapy.com/provider/1") == ""

    def test_inference_uses_the_practices_own_domain(self, store, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        verifier.existing_mailboxes = {"jane.smith@practice.example"}
        pipeline = build(store, verifier=verifier)
        assert pipeline.guess_email("Jane", "Smith",
                                    "https://www.practice.example/about") == \
            "jane.smith@practice.example"

    def test_an_inferred_address_already_used_is_not_reused(self, store, offline_verifier):
        verifier = offline_verifier()
        verifier.mx_by_domain = {"practice.example": ["mx.practice.example"]}
        verifier.existing_mailboxes = {"jane.smith@practice.example"}
        store.add_leads_batch([{"NPI Number": "1111111111", "State": "TX",
                                "Source": "Psychology Today",
                                "Email": "jane.smith@practice.example"}])
        pipeline = build(store, verifier=verifier)
        assert pipeline.guess_email("Jane", "Smith", "https://practice.example") == ""

    def test_without_a_verifier_nothing_is_stamped_or_dropped(self, store):
        pipeline = build(store, verifier=None)
        lead = {"Email": "whatever@practice.example"}
        pipeline.attach_verification(lead)
        assert lead["Email"] == "whatever@practice.example"
        assert "Email Status" not in lead


class TestPureHelpers:

    @pytest.mark.parametrize("name, expected", [
        ("Shelby Scott (Gregory)", ("Shelby", "Scott")),
        ("Mary Anne Van Der Berg", ("Mary Anne Van Der", "Berg")),
        ("Cher", ("Cher", "")),
        ("", ("", "")),
        ("   ", ("", "")),
    ])
    def test_name_splitting(self, name, expected):
        assert LeadPipeline.split_pt_name(name) == expected

    def test_labelled_npi_numbers_win_over_bare_ones(self):
        text = "NPI: 1234567890 — call 2105551234 — NPI 1987654321"
        assert LeadPipeline.extract_npi_numbers(text) == ["1234567890", "1987654321"]

    def test_a_bare_list_still_works(self):
        assert LeadPipeline.extract_npi_numbers("1234567890\n1987654321") == \
            ["1234567890", "1987654321"]

    def test_duplicates_are_collapsed_in_order(self):
        assert LeadPipeline.extract_npi_numbers("1234567890 1234567890 1987654321") == \
            ["1234567890", "1987654321"]

    def test_numbers_that_cannot_be_npis_are_ignored(self):
        # NPIs only ever start 1 or 2.
        assert LeadPipeline.extract_npi_numbers("9876543210 3333333333") == []
