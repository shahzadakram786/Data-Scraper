"""CMS enrichment: parsing, the phone verdict, and scoring.

The network is stubbed throughout. What matters here is that partial coverage
is handled as the normal case — CMS only covers Medicare-enrolled providers and
suppresses anyone under 11 beneficiaries, so most Psychology Today therapists
have no record at all, and that must never look like a failure.
"""

import pytest

import storage
from storage import compute_lead_score
from pipeline import LeadPipeline, Reporter
from cms_engine import CMSEngine, _loads, _number, _digits


# ------------------------------------------------------------ payload parsing

class TestPayloadParsing:

    def test_plain_json_is_read(self):
        assert _loads('[{"a": 1}]') == [{"a": 1}]

    def test_json_wrapped_in_the_readers_markdown_is_recovered(self):
        """The relay prefixes every response with a title/URL header, so the
        payload has to be found rather than assumed to start at byte zero."""
        wrapped = (
            "Title: \n\nURL Source: https://data.cms.gov/whatever\n\n"
            'Markdown Content:\n[{"Rndrng_NPI":"1003000126","Tot_Benes":"328"}]')
        assert _loads(wrapped) == [{"Rndrng_NPI": "1003000126", "Tot_Benes": "328"}]

    def test_an_object_payload_is_recovered_too(self):
        wrapped = 'Markdown Content:\n{"results": [{"npi": "1"}], "count": 1}'
        assert _loads(wrapped)["count"] == 1

    def test_junk_yields_nothing_rather_than_raising(self):
        assert _loads("Access Denied") is None
        assert _loads("") is None

    @pytest.mark.parametrize("raw, expected", [
        ("328", 328),
        ("202783.88", 202783.88),
        ("1,234", 1234),
        ("$5,000.50", 5000.5),
        ("", None),
        (None, None),
        ("*", None),        # CMS's suppression marker
        ("n/a", None),
    ])
    def test_number_coercion(self, raw, expected):
        assert _number(raw) == expected

    @pytest.mark.parametrize("raw, expected", [
        ("7714446200", "7714446200"),
        ("17714446200", "7714446200"),
        ("(771) 444-6200", "7714446200"),
        ("12345", ""),
        (None, ""),
    ])
    def test_phone_digits(self, raw, expected):
        assert _digits(raw) == expected


class TestEngineShaping:
    """The engine turns CMS's field names into ours, and returns {} rather than
    partial junk when a provider is absent."""

    def _engine(self, billing_rows=None, clinician_rows=None):
        engine = CMSEngine()

        def fake_get(url, retries=2):
            if "provider-data" in url:
                return {"results": clinician_rows or []}
            return billing_rows or []

        engine._get_json = fake_get
        return engine

    def test_billing_fields_are_mapped(self):
        engine = self._engine(billing_rows=[{
            "Tot_Sbmtd_Chrg": "202783.88", "Tot_Mdcr_Pymt_Amt": "35325.46",
            "Tot_Benes": "328", "Tot_Srvcs": "399",
            "Rndrng_Prvdr_Type": "Internal Medicine"}])
        result = engine.billing_for("1003000126")
        assert result["annual_billing"] == 202783.88
        assert result["medicare_paid"] == 35325.46
        assert result["patient_volume"] == 328

    def test_a_provider_with_no_billing_record_returns_empty(self):
        """The common case for therapists — not an error."""
        assert self._engine(billing_rows=[]).billing_for("1003000126") == {}

    def test_a_malformed_npi_is_never_looked_up(self):
        engine = self._engine(billing_rows=[{"Tot_Sbmtd_Chrg": "1"}])
        assert engine.billing_for("PT-1234567") == {}
        assert engine.billing_for("123") == {}
        assert engine.billing_for("") == {}

    def test_the_largest_practice_wins_when_a_clinician_has_several(self):
        """Clinicians appear once per enrollment/address. The biggest listed
        group is the affiliation worth pitching."""
        engine = self._engine(clinician_rows=[
            {"num_org_mem": "3", "facility_name": "SIDE CLINIC",
             "telephone_number": "2145550001"},
            {"num_org_mem": "676", "facility_name": "BIG GROUP",
             "telephone_number": "7714446200"},
        ])
        result = engine.enrollment_for("1003000126")
        assert result["practice_size"] == 676
        assert result["practice_name"] == "BIG GROUP"

    def test_results_are_cached(self):
        engine = self._engine(billing_rows=[{"Tot_Benes": "5"}])
        calls = []
        original = engine._get_json
        engine._get_json = lambda url, retries=2: (calls.append(url), original(url))[1]
        engine.billing_for("1003000126")
        engine.billing_for("1003000126")
        assert len(calls) == 1, "the second lookup should come from cache"


# --------------------------------------------------------- the phone verdict

class TestPhoneVerdict:
    """A number the provider filed with Medicare themselves is the strongest
    free corroboration available."""

    def test_a_match_is_confirmed(self):
        verdict = LeadPipeline._phone_verdict(
            {"Phone": "(771) 444-6200"}, {"filed_phone": "7714446200"})
        assert verdict["Phone Verified"] == storage.PHONE_CONFIRMED

    def test_formatting_differences_still_match(self):
        verdict = LeadPipeline._phone_verdict(
            {"Phone": "771-444-6200"}, {"filed_phone": "7714446200"})
        assert verdict["Phone Verified"] == storage.PHONE_CONFIRMED

    def test_a_missing_phone_is_filled_in_from_the_federal_record(self):
        verdict = LeadPipeline._phone_verdict({"Phone": ""},
                                              {"filed_phone": "7714446200"})
        assert verdict["Phone"] == "(771) 444-6200"
        assert verdict["Phone Source"] == storage.PHONE_FROM_CMS

    def test_a_mismatch_is_labelled_not_overwritten(self):
        """A scraped direct line is often better than the billing office number
        on file, so the scraped number stays and the difference is noted."""
        verdict = LeadPipeline._phone_verdict(
            {"Phone": "(512) 471-3399"}, {"filed_phone": "7714446200"})
        assert verdict["Phone Verified"] == storage.PHONE_DIFFERS
        assert "Phone" not in verdict, "the existing number must not be replaced"

    def test_no_federal_record_is_still_recorded_as_checked(self):
        """Without this the provider looks un-enriched forever and gets
        re-queried on every future run."""
        verdict = LeadPipeline._phone_verdict({"Phone": "(512) 471-3399"}, {})
        assert verdict["Phone Verified"] == storage.PHONE_NO_RECORD


# ------------------------------------------------------------------- scoring

class TestCMSScoring:

    def test_billing_volume_raises_the_score(self):
        base = {"Email": "a@b.example", "Email Status": "valid"}
        small = compute_lead_score(dict(base, **{"Annual Billing": 20000}))
        large = compute_lead_score(dict(base, **{"Annual Billing": 2000000}))
        assert large > small > compute_lead_score(base)

    def test_practice_size_raises_the_score(self):
        base = {"Phone": "x", "Phone Source": "Provider Website"}
        solo = compute_lead_score(dict(base, **{"Practice Size": 1}))
        group = compute_lead_score(dict(base, **{"Practice Size": 80}))
        assert group > solo

    def test_a_lead_with_no_cms_record_is_not_penalised(self):
        """Missing data means "not a Medicare biller", not "a worse lead" — most
        therapists will never have it."""
        without = compute_lead_score({
            "Email": "a@b.example", "Email Status": "valid",
            "Phone": "x", "Phone Source": "Provider Website (personal/direct)",
            "Role": "Owner"})
        assert without >= 85, "contactability alone should still score highly"

    def test_a_federally_confirmed_phone_adds_confidence(self):
        base = {"Phone": "x", "Phone Source": "Provider Website"}
        confirmed = compute_lead_score(
            dict(base, **{"Phone Verified": storage.PHONE_CONFIRMED}))
        assert confirmed > compute_lead_score(base)

    def test_the_score_stays_within_range(self):
        maxed = compute_lead_score({
            "Email": "a@b.example", "Email Status": "valid",
            "Phone": "x", "Phone Source": "personal/direct",
            "Role": "Owner", "Website": "w", "LinkedIn": "l",
            "NPI Number": "1234567890", "Annual Billing": 9000000,
            "Practice Size": 5000, "Phone Verified": storage.PHONE_CONFIRMED})
        assert 0 <= maxed <= 100

    def test_a_bouncing_address_still_zeroes_the_lead(self):
        """Even a huge biller is worthless if the only address bounces."""
        assert compute_lead_score({
            "Email": "dead@x.example", "Email Status": "invalid",
            "Annual Billing": 5000000}) < 30


# ------------------------------------------------------------- the full pass

class FakeCMS:
    def __init__(self, billing=None, enrollment=None):
        self._billing = billing or {}
        self._enrollment = enrollment or {}

    def billing_for(self, npi):
        return dict(self._billing)

    def enrollment_for(self, npi):
        return dict(self._enrollment)


def build(store, cms):
    return LeadPipeline(store=store, npi_engine=None, email_engine=None,
                        pt_engine=None, cms_engine=cms, reporter=Reporter())


class TestEnrichmentPass:

    def _seed(self, store, **overrides):
        lead = {"NPI Number": "1003000126", "Provider First Name": "Jane",
                "Provider Last Name": "Smith", "State": "TX",
                "Source": "ZocDoc (NPI Import)", "Phone": "(512) 471-3399"}
        lead.update(overrides)
        store.add_leads_batch([lead])
        return next(store.iter_leads())

    def test_billing_and_practice_size_are_written(self, store):
        self._seed(store)
        pipeline = build(store, FakeCMS(
            billing={"annual_billing": 202783.88, "medicare_paid": 35325.46,
                     "patient_volume": 328},
            enrollment={"practice_size": 676, "practice_name": "BIG GROUP",
                        "filed_phone": "7714446200"}))
        result = pipeline.enrich_from_cms()
        assert result.status == storage.RUN_OK

        lead = next(store.iter_leads())
        assert float(lead["Annual Billing"]) == 202783.88
        assert int(lead["Practice Size"]) == 676
        assert lead["Phone Verified"] == storage.PHONE_DIFFERS

    def test_a_provider_cms_does_not_cover_is_marked_and_not_requeried(self, store):
        self._seed(store)
        pipeline = build(store, FakeCMS())
        pipeline.enrich_from_cms()

        lead = next(store.iter_leads())
        assert lead["Phone Verified"] == storage.PHONE_NO_RECORD
        assert lead["Annual Billing"] == ""
        assert store.count(needs_cms=True) == 0, "must not be looked up again"

    def test_placeholder_npis_are_never_looked_up(self, store):
        """Psychology Today leads with no registry match carry a PT-<id>
        placeholder, which CMS cannot resolve."""
        self._seed(store, **{"NPI Number": "PT-1234567",
                             "Source": "Psychology Today"})
        assert store.count(needs_cms=True) == 0

    def test_an_empty_practice_name_is_filled_but_never_overwritten(self, store):
        self._seed(store, **{"Organization Name": "Jane's Own Practice"})
        pipeline = build(store, FakeCMS(
            enrollment={"practice_name": "BIG GROUP", "filed_phone": "7714446200"}))
        pipeline.enrich_from_cms()
        assert next(store.iter_leads())["Organization Name"] == "Jane's Own Practice"

    def test_the_pass_reports_honestly_when_nothing_was_found(self, store):
        self._seed(store)
        pipeline = build(store, FakeCMS())
        result = pipeline.enrich_from_cms()
        assert "0 had Medicare billing data" in result.message

    def test_nothing_to_do_is_not_an_error(self, store):
        pipeline = build(store, FakeCMS())
        result = pipeline.enrich_from_cms()
        assert result.status == storage.RUN_OK
