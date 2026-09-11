"""Parser regression tests.

Every rule these check is a heuristic that was tuned against a real failure —
a Canadian shop's address attached to a Texas psychiatrist, a fax number
recorded as a provider's phone, a group practice's founder tagged onto every
clinician who works there. Without tests those rules cannot be changed safely,
because the only way to find out you broke one was to run a whole state and
read the output.
"""

import pytest

from conftest import fixture


# --------------------------------------------------------------- phone rules

class TestPhoneValidation:

    @pytest.mark.parametrize("raw, expected", [
        ("512-471-3300", "(512) 471-3300"),
        ("(512) 471-3300", "(512) 471-3300"),
        ("+1 512 471 3300", "(512) 471-3300"),
        ("1-512-471-3300", "(512) 471-3300"),
        ("5124713300", "(512) 471-3300"),
    ])
    def test_accepts_and_normalises_real_numbers(self, email_engine, raw, expected):
        assert email_engine._normalize_phone(raw) == expected

    @pytest.mark.parametrize("raw, why", [
        ("457-821-5250", "457 is not an assigned NANP area code"),
        ("123-456-7890", "area code cannot start with 1"),
        ("911-555-1234", "N11 codes are service codes"),
        ("512-555-0123", "555-01xx is the reserved fictional range"),
        ("0000000000", "all one digit"),
        ("1234567890", "an NPI number, not a phone number"),
        ("512-555-014", "too short"),
    ])
    def test_rejects_undialable_numbers(self, email_engine, raw, why):
        assert email_engine._normalize_phone(raw) is None, why

    def test_toll_free_is_recognised(self, email_engine):
        assert email_engine.is_tollfree("(800) 631-1000")
        assert email_engine.is_tollfree("833-631-1000")
        assert not email_engine.is_tollfree("(512) 471-3300")


class TestPhoneExtraction:
    """A page's own wording says what each number is. Getting this wrong means
    exporting a fax line, or a crisis hotline, as a provider's phone number."""

    def test_reads_numbers_and_classifies_them(self, email_engine):
        found = email_engine._extract_phones(fixture("practice_site.html"))
        by_number = {c["phone"]: c for c in found}

        assert "(512) 471-3399" in by_number, "the cell number should be found"
        assert by_number["(512) 471-3399"]["kind"] == "personal"
        assert by_number["(512) 471-3300"]["kind"] == "business"

    def test_discards_fax_and_crisis_numbers(self, email_engine):
        found = {c["phone"] for c in
                 email_engine._extract_phones(fixture("practice_site.html"))}
        assert "(512) 471-3301" not in found, "a fax line is not a way to reach anyone"

    def test_prefers_the_direct_line_over_the_front_desk(self, email_engine):
        candidates = email_engine._extract_phones(fixture("practice_site.html"))
        phone, label = email_engine._choose_phone(candidates)
        assert phone == "(512) 471-3399"
        assert "personal/direct" in label

    def test_no_candidates_yields_nothing_rather_than_a_guess(self, email_engine):
        assert email_engine._choose_phone([]) == ("", "")


# ------------------------------------------------------------- email rules

class TestEmailAttribution:
    """A search result mentioning a provider is not proof that an address in
    its snippet belongs to them. These are the actual mis-attributions that
    reached the spreadsheet before the corroboration rule existed."""

    def test_rejects_an_unrelated_business_on_an_unrelated_domain(self, email_engine):
        result = {"title": "Leather Accessories Toronto",
                  "body": "Contact us at info@leathershop.example",
                  "href": "https://leathershop.example/contact"}
        assert not email_engine._result_corroborates(
            result, "info@leathershop.example",
            person_name="Amit Patel", org_name="Southlake Psychiatry")

    def test_rejects_a_strangers_freemail_on_a_page_that_names_the_provider(
            self, email_engine):
        result = {"title": "Austin therapist directory",
                  "body": "Dr. Amit Patel was mentioned. Questions to bobsmith92@gmail.com",
                  "href": "https://directory.example/austin"}
        assert not email_engine._result_corroborates(
            result, "bobsmith92@gmail.com", person_name="Amit Patel")

    def test_accepts_a_freemail_box_named_after_the_provider(self, email_engine):
        result = {"title": "Amit Patel, MD",
                  "body": "Reach Dr. Patel at apatel.md@gmail.com",
                  "href": "https://directory.example/austin"}
        assert email_engine._result_corroborates(
            result, "apatel.md@gmail.com", person_name="Amit Patel")

    def test_accepts_an_address_on_the_results_own_domain(self, email_engine):
        result = {"title": "Southlake Psychiatry",
                  "body": "Call or write info@southlakepsych.example",
                  "href": "https://southlakepsych.example/contact"}
        assert email_engine._result_corroborates(
            result, "info@southlakepsych.example",
            person_name="Amit Patel", org_name="Southlake Psychiatry")

    def test_a_quoted_phone_number_corroborates(self, email_engine):
        result = {"title": "Practice listing",
                  "body": "Phone (210) 520-7160 - email office@clinic.example",
                  "href": "https://clinic.example/"}
        assert email_engine._result_corroborates(
            result, "office@clinic.example", phone="210-520-7160")

    @pytest.mark.parametrize("address", [
        "logo@2x.png", "noreply@practice.example", "someone@example.com",
        "hi@sentry.io",
    ])
    def test_junk_addresses_are_filtered(self, email_engine, address):
        assert not email_engine._is_valid_email(address)

    def test_third_party_addresses_on_a_site_are_not_the_practices(self, email_engine):
        assert not email_engine._belongs_to_site(
            "support@bookingwidget.example", "brittonpsych.example")
        assert email_engine._belongs_to_site(
            "kaelyn@brittonpsych.example", "brittonpsych.example")
        # A solo practitioner's personal mailbox is legitimate.
        assert email_engine._belongs_to_site("kaelyn@gmail.com", "brittonpsych.example")

    @pytest.mark.parametrize("netloc, expected", [
        ("www.foo.co.uk:443", "foo.co.uk"),
        ("mail.practice.example", "practice.example"),
        ("practice.example", "practice.example"),
    ])
    def test_registrable_domain(self, email_engine, netloc, expected):
        assert email_engine._registrable_domain(netloc) == expected


# --------------------------------------------------------------- role rules

class TestRoleDetection:

    def test_finds_an_ownership_title_next_to_the_providers_name(self, email_engine):
        role = email_engine.detect_role(fixture("practice_site.html"), "Kaelyn Britton")
        assert role in ("Founder", "Owner")

    def test_does_not_tag_a_clinician_with_someone_elses_title(self, email_engine):
        """The page says Shelby Scott is a therapist and that somebody else
        founded the practice. Tagging Shelby as the founder is the false
        positive that would fill an owners-only list with employees."""
        role = email_engine.detect_role(
            fixture("group_practice_site.html"), "Shelby Scott")
        assert role == "", "a colleague's title must not attach to this provider"

    def test_names_the_actual_founder_correctly(self, email_engine):
        role = email_engine.detect_role(
            fixture("group_practice_site.html"), "Priya Raman")
        assert role == "Founder"

    def test_no_name_means_no_role(self, email_engine):
        assert email_engine.detect_role(fixture("practice_site.html"), "") == ""

    def test_only_ownership_roles_pass_the_decision_maker_filter(self, email_engine):
        assert email_engine.is_ownership_role("Owner")
        assert email_engine.is_ownership_role("CEO")
        # Senior staff, but not the person who owns the practice.
        assert not email_engine.is_ownership_role("Clinical Director")
        assert not email_engine.is_ownership_role("Executive Director")
        assert not email_engine.is_ownership_role("")


# ------------------------------------------------------------- social links

class TestSocialExtraction:

    def test_records_the_practices_own_profiles(self, email_engine):
        socials = email_engine.extract_socials(
            fixture("practice_site.html"), "https://brittonpsych.example/")
        assert "linkedin.com/in/kaelyn-britton" in socials["LinkedIn"]
        assert "instagram.com/brittonpsych" in socials["Instagram"]
        assert "facebook.com/brittonpsych" in socials["Facebook"]

    def test_ignores_share_buttons_pixels_and_login_walls(self, email_engine):
        socials = email_engine.extract_socials(
            fixture("practice_site.html"), "https://brittonpsych.example/")
        joined = " ".join(socials.values())
        for junk in ("sharer", "intent/tweet", "login.php", "/tr?"):
            assert junk not in joined, junk + " is not a practice profile"

    def test_marketplace_domains_are_recognised(self, email_engine):
        assert email_engine.is_platform_site("https://growtherapy.com/provider/123")
        assert email_engine.is_platform_site("https://www.thriveworks.com/x/")
        assert not email_engine.is_platform_site("https://brittonpsych.example/")


# --------------------------------------------------- Psychology Today listing

class TestListingParser:

    def test_reads_providers_from_the_current_json_ld_shape(self, pt_engine):
        entries = pt_engine._parse_listing(fixture("pt_listing.html"))
        assert len(entries) == 3

        by_id = {e["pt_id"]: e for e in entries}
        first = by_id["1234567"]
        assert first["name"] == "Kaelyn Britton"
        assert first["city"] == "Austin"
        assert first["state_full"] == "TX"
        assert first["zip"] == "78701"
        assert first["phone"] == "(512) 471-3300"
        assert first["is_org"] is False

    def test_reads_the_credential_line_off_the_listing_row(self, pt_engine):
        """Reading this here rather than from each profile page saves one HTTP
        request per provider — the difference between one relayed request per
        twenty providers and twenty-one."""
        entries = {e["pt_id"]: e for e in
                   pt_engine._parse_listing(fixture("pt_listing.html"))}
        assert entries["1234567"]["credential"] == "Psychologist, PsyD"
        assert entries["2345678"]["credential"] == (
            "Licensed Professional Mental Health Counselor, MS, LPCMH, NCC")

    def test_organisations_are_flagged(self, pt_engine):
        entries = {e["pt_id"]: e for e in
                   pt_engine._parse_listing(fixture("pt_listing.html"))}
        assert entries["3456789"]["is_org"] is True

    def test_still_reads_the_older_json_ld_shape(self, pt_engine):
        """mainEntity used to be the provider list directly. Both shapes have
        shipped, and dropping support for either means a silent zero-provider
        run."""
        entries = pt_engine._parse_listing(fixture("pt_listing_legacy.html"))
        assert len(entries) == 2
        assert {e["name"] for e in entries} == {"Marcus Webb", "Dana Whitfield"}

    def test_reads_the_id_from_at_id_when_url_is_absent(self, pt_engine):
        entries = {e["pt_id"]: e for e in
                   pt_engine._parse_listing(fixture("pt_listing_legacy.html"))}
        assert "8765432" in entries

    def test_a_page_with_no_json_ld_yields_nothing(self, pt_engine):
        assert pt_engine._parse_listing("<html><body>Nothing here</body></html>") == []

    def test_website_is_read_out_of_the_escaped_nuxt_payload(self, pt_engine):
        escaped = chr(92) + "u002F"
        payload = ('{"tracking":"https:' + escaped * 2 + 'www.googletagmanager.com'
                   '/gtm.js","target":"https:' + escaped * 2 +
                   'brittonpsych.example' + escaped + 'contact"}')
        assert pt_engine._first_external_url(payload) == \
            "https://brittonpsych.example/contact"

    def test_boilerplate_hosts_are_skipped(self, pt_engine):
        escaped = chr(92) + "u002F"
        payload = '{"a":"https:' + escaped * 2 + 'www.psychologytoday.com' + escaped + 'x"}'
        assert pt_engine._first_external_url(payload) == ""
