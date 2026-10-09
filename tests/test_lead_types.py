"""Lead types: which practices look like buyers of a service, and why.

Every check answers True, False or None, and None — couldn't check — must
never be read as a match or as a no-match. Most of these tests are about that
line, because crossing it is how a list fills with practices that never needed
the service.
"""

import time
from datetime import date

import pytest

import lead_types
import site_audit
from conftest import healthy_site_facts
from lead_types import MATCH, NO_MATCH, NOT_CHECKED

TODAY = date(2026, 10, 9)
DIRECT = "Provider Website (personal/direct, tel: link)"


def lead(facts=None, text="", **fields):
    """A lead with socials on file, so Social Media stays quiet unless a test
    clears them."""
    row = {"NPI Number": "1234567890", "Facebook": "https://facebook.com/cedar",
           "Instagram": "https://instagram.com/cedar"}
    if facts is not None:
        row["Site Facts"] = site_audit.dump_facts(facts)
    if text:
        row["Site Text"] = site_audit.pack_text(text)
    row.update(fields)
    return row


def healthy(**overrides):
    return lead(healthy_site_facts(TODAY, **overrides))


def no_site(why=site_audit.NO_SITE_LISTED, host=""):
    return lead(site_audit.no_site_facts(why, host, today=TODAY))


def run(check_id, row):
    return lead_types.CHECKS[check_id].run(lead_types.Evidence(row, TODAY))


def builtin(type_id):
    return next(t for t in lead_types.builtin_types() if t.id == type_id)


def judge(row, *selected):
    return lead_types.Selection(lead_types.builtin_types(), selected, today=TODAY).judge(row)


SITE_LACKS = [
    ("no_https", "https", False, "no HTTPS"),
    ("no_mobile_layout", "viewport", False, "no mobile layout"),
    ("no_h1", "h1", False, "no H1 heading"),
    ("no_schema_org", "schema_org", False, "no structured business data"),
    ("no_analytics", "analytics", [], "no analytics tag"),
    ("no_ad_pixel", "ad_pixels", [], "no ad tracking"),
    ("no_newsletter", "newsletter", [], "no newsletter signup"),
    ("no_blog", "blog", False, "no blog"),
    ("no_logo", "logo", False, "no logo"),
    ("no_favicon", "favicon", False, "no favicon"),
    ("no_share_image", "share_image", False, "no social share image"),
    ("no_video", "video", False, "no video"),
    ("no_online_booking", "booking", [], "no online booking"),
    ("no_client_portal", "portal", False, "no client portal"),
    ("no_contact_form", "contact_form", False, "no contact form on the homepage"),
    ("no_live_chat", "live_chat", [], "no live chat"),
    ("no_privacy_policy", "privacy_policy", False, "no privacy policy"),
    ("no_hipaa_notice", "hipaa_notice", False, "no HIPAA notice"),
    ("no_terms", "terms", False, "no terms of use"),
    ("no_accessibility_statement", "accessibility", False, "no accessibility statement"),
]


class TestSiteChecks:

    @pytest.mark.parametrize("check_id, key, lacking, reason", SITE_LACKS)
    def test_true_when_the_site_lacks_it(self, check_id, key, lacking, reason):
        assert run(check_id, healthy(**{key: lacking})) == (True, reason)

    @pytest.mark.parametrize("check_id, key, lacking, reason", SITE_LACKS)
    def test_false_when_the_site_has_it(self, check_id, key, lacking, reason):
        assert run(check_id, healthy())[0] is False

    @pytest.mark.parametrize("check_id, key, lacking, reason", SITE_LACKS)
    @pytest.mark.parametrize("row", [
        lead(),                                                      # never looked
        no_site(),                                                   # no site of their own
        lead(site_audit.base_facts(True, "dns", today=TODAY)),       # site down
        lead(site_audit.failed_audit("ok", ValueError(), today=TODAY)),  # unreadable
    ])
    def test_couldnt_check_without_a_site_that_was_read(self, check_id, key, lacking,
                                                         reason, row):
        assert run(check_id, row)[0] is None

    def test_a_fact_older_records_lack_reads_as_couldnt_check(self):
        facts = healthy_site_facts(TODAY)
        del facts["viewport"]
        assert run("no_mobile_layout", lead(facts))[0] is None


class TestSiteStatus:

    def test_no_website_listed(self):
        assert run("no_own_site", no_site()) == (True, "no website of their own")

    def test_only_a_marketplace_profile(self):
        row = no_site(site_audit.NO_SITE_PROFILE, "growtherapy.com")
        assert run("no_own_site", row) == (True, "only a growtherapy.com profile")

    def test_a_site_of_their_own(self):
        assert run("no_own_site", healthy())[0] is False

    def test_nobody_looked(self):
        assert run("no_own_site", lead())[0] is None

    def test_a_server_error_is_a_site_down(self):
        row = lead(site_audit.base_facts(True, "http_error", 503, TODAY))
        assert run("site_unreachable", row) == (True, "site down (HTTP 503)")

    def test_a_dead_domain_is_a_site_down(self):
        row = lead(site_audit.base_facts(True, "dns", today=TODAY))
        assert run("site_unreachable", row)[0] is True

    def test_a_working_site_is_not_down(self):
        assert run("site_unreachable", healthy())[0] is False

    @pytest.mark.parametrize("fetch", ["timeout", "blocked", "connection", "ssl"])
    def test_other_failures_say_nothing_about_down(self, fetch):
        row = lead(site_audit.base_facts(True, fetch, today=TODAY))
        assert run("site_unreachable", row)[0] is None

    def test_an_ssl_error(self):
        row = lead(site_audit.base_facts(True, "ssl", today=TODAY))
        assert run("ssl_error", row) == (True, "SSL certificate error")
        assert run("ssl_error", healthy())[0] is False

    def test_a_copyright_four_years_old_is_dated(self):
        assert run("dated_copyright", healthy(copyright_year=2022)) == (True, "© 2022")

    def test_a_recent_copyright_is_not(self):
        assert run("dated_copyright", healthy(copyright_year=2023))[0] is False

    def test_no_year_on_a_read_site_is_not_dated(self):
        assert run("dated_copyright", healthy(copyright_year=None))[0] is False

    def test_missing_title_and_description(self):
        assert run("missing_title_or_description", healthy(title=False)) == \
            (True, "no page title")
        assert run("missing_title_or_description",
                   healthy(title=False, meta_description=False)) == \
            (True, "no page title or meta description")
        assert run("missing_title_or_description", healthy())[0] is False

    @pytest.mark.parametrize("images, missing, expected", [
        (4, 2, True), (4, 1, False), (2, 2, False), (0, 0, False)])
    def test_images_missing_alt(self, images, missing, expected):
        value, reason = run("images_missing_alt",
                            healthy(images=images, images_missing_alt=missing))
        assert value is expected
        if expected:
            assert reason == "{0} of {1} images lack alt text".format(missing, images)

    @pytest.mark.parametrize("version, expected", [
        ("5.8.2", True), ("4.9", True), ("6.0", False), ("6.4.2", False),
        (None, False), ("garbage", False)])
    def test_outdated_wordpress(self, version, expected):
        value, reason = run("outdated_wordpress", healthy(wordpress_version=version))
        assert value is expected
        if expected:
            assert reason.startswith("WordPress ") and reason.endswith("(outdated)")

    def test_no_social_profiles(self):
        assert run("no_social_profiles", healthy() | {"Facebook": "", "Instagram": ""}) == \
            (True, "no social profiles")
        assert run("no_social_profiles", healthy())[0] is False
        assert run("no_social_profiles", lead(Facebook="", Instagram=""))[0] is None

    def test_one_social_platform(self):
        row = healthy() | {"Instagram": ""}
        assert run("one_social_platform", row) == (True, "only on Facebook")
        other = healthy() | {"Facebook": "", "Instagram": "",
                             "Other Social": "https://x.com/cedar"}
        assert run("one_social_platform", other) == (True, "only on x.com")
        assert run("one_social_platform", healthy())[0] is False


class TestColumnChecks:

    def test_an_owner_on_their_own_line(self):
        row = healthy() | {"Role": "Owner", "Phone Source": DIRECT}
        assert run("owner_answers_phone", row) == (True, "owner answers their own line")

    def test_an_owner_behind_a_front_desk(self):
        row = healthy() | {"Role": "Owner", "Phone Source": "Provider Website (business line)"}
        assert run("owner_answers_phone", row)[0] is False

    def test_no_role_after_reading_the_site_is_not_the_owner(self):
        assert run("owner_answers_phone", healthy())[0] is False

    def test_no_role_and_no_site_read_is_couldnt_check(self):
        for check_id in ("owner_answers_phone", "owner_small_practice", "rcm_icp"):
            assert run(check_id, lead())[0] is None, check_id

    @pytest.mark.parametrize("size, expected, fragment", [
        ("1", True, "Owner · solo practice"),
        ("3", True, "Owner · 3 clinicians"),
        ("", True, "Owner of the practice"),
        ("12", False, ""),
    ])
    def test_an_owner_small_practice(self, size, expected, fragment):
        row = healthy() | {"Role": "Owner", "Practice Size": size}
        value, reason = run("owner_small_practice", row)
        assert value is expected
        if expected:
            assert reason.startswith(fragment)
            assert reason.endswith("likely no in-house finance staff")

    def test_a_clinical_director_is_not_an_owner(self):
        row = healthy() | {"Role": "Clinical Director", "Practice Size": "1"}
        assert run("owner_small_practice", row)[0] is False

    def test_billing(self):
        assert run("billing_over_100k", lead(**{"Annual Billing": "230570"})) == \
            (True, "$231k Medicare billing")
        assert run("billing_over_100k", lead(**{"Annual Billing": "50000"}))[0] is False
        assert run("billing_over_100k", lead())[0] is None

    def test_staff(self):
        assert run("has_staff", lead(**{"Practice Size": "14"})) == (True, "14 clinicians")
        assert run("has_staff", lead(**{"Practice Size": "1"}))[0] is False
        assert run("has_staff", lead(**{"Practice Size": "40"}))[0] is False
        assert run("has_staff", lead())[0] is None

    def test_the_rcm_profile(self):
        assert run("rcm_icp", healthy() | {"Role": "Owner", "Phone": "(512) 555-0100"}) == \
            (True, "owner-operated small practice")
        assert run("rcm_icp", healthy() | {"Role": "Clinical Director",
                                           "Phone": "(512) 555-0100"})[0] is False


def custom(**fields):
    base = {"id": "custom", "name": "Custom", "group": "Custom"}
    base.update(fields)
    return lead_types.LeadType(**base)


class TestKeywords:

    def test_a_phrase_the_homepage_mentions(self):
        row = lead(healthy_site_facts(TODAY), text="We offer telehealth across Texas.")
        status, reasons = lead_types.evaluate(custom(mentions=["telehealth"]),
                                              lead_types.Evidence(row, TODAY))
        assert (status, reasons) == (MATCH, ["mentions 'telehealth'"])

    def test_a_phrase_the_homepage_does_not_mention(self):
        row = lead(healthy_site_facts(TODAY), text="Call us to schedule.")
        status, reasons = lead_types.evaluate(custom(not_mentions=["book online"]),
                                              lead_types.Evidence(row, TODAY))
        assert (status, reasons) == (MATCH, ["doesn't mention 'book online'"])

    def test_case_and_spacing_do_not_matter(self):
        row = lead(healthy_site_facts(TODAY), text="You can Book   Online today.")
        status, _ = lead_types.evaluate(custom(not_mentions=["book online"]),
                                        lead_types.Evidence(row, TODAY))
        assert status == NO_MATCH

    def test_whole_phrases_only(self):
        row = lead(healthy_site_facts(TODAY), text="Read our free ebook online.")
        status, _ = lead_types.evaluate(custom(not_mentions=["book online"]),
                                        lead_types.Evidence(row, TODAY))
        assert status == MATCH, "'ebook online' is not 'book online'"

    def test_no_stored_text_is_couldnt_check(self):
        status, _ = lead_types.evaluate(custom(mentions=["telehealth"]),
                                        lead_types.Evidence(healthy(), TODAY))
        assert status == NOT_CHECKED


class TestEvaluation:

    def evaluate(self, lead_type, row):
        return lead_types.evaluate(lead_type, lead_types.Evidence(row, TODAY))

    def test_any_matches_on_one_true_and_reports_only_true_reasons(self):
        lead_type = custom(checks=["no_mobile_layout", "no_h1"])
        assert self.evaluate(lead_type, healthy(viewport=False)) == \
            (MATCH, ["no mobile layout"])

    def test_any_is_no_match_only_when_everything_is_false(self):
        assert self.evaluate(custom(checks=["no_mobile_layout", "no_h1"]), healthy())[0] == NO_MATCH

    def test_any_with_false_and_couldnt_check_is_not_checked(self):
        lead_type = custom(checks=["no_mobile_layout", "has_staff"])
        assert self.evaluate(lead_type, healthy())[0] == NOT_CHECKED

    def test_all_needs_every_check_true(self):
        lead_type = custom(match="all", checks=["no_mobile_layout", "no_h1"])
        assert self.evaluate(lead_type, healthy(viewport=False, h1=False))[0] == MATCH
        assert self.evaluate(lead_type, healthy(viewport=False))[0] == NO_MATCH

    def test_all_with_true_and_couldnt_check_is_not_checked(self):
        lead_type = custom(match="all", checks=["no_mobile_layout", "has_staff"])
        assert self.evaluate(lead_type, healthy(viewport=False))[0] == NOT_CHECKED

    def test_an_unknown_check_is_couldnt_check(self):
        assert self.evaluate(custom(checks=["no_fax_machine"]), healthy())[0] == NOT_CHECKED

    def test_a_type_with_nothing_to_check_is_not_checked(self):
        assert self.evaluate(custom(), healthy())[0] == NOT_CHECKED


BUILTIN_CASES = [
    ("website_development", no_site(), MATCH),
    ("website_development", healthy(), NO_MATCH),
    ("seo", healthy(meta_description=False), MATCH),
    ("seo", healthy(), NO_MATCH),
    ("web_accessibility", healthy(accessibility=False), MATCH),
    ("web_accessibility", healthy(), NO_MATCH),
    ("it_security", healthy(https=False), MATCH),
    ("it_security", healthy(), NO_MATCH),
    ("digital_marketing", healthy(analytics=[]), MATCH),
    ("paid_ads", healthy(ad_pixels=[]), MATCH),
    ("social_media", healthy() | {"Facebook": "", "Instagram": ""}, MATCH),
    ("social_media", healthy(), NO_MATCH),
    ("email_marketing", healthy(newsletter=[]), MATCH),
    ("content_writing", healthy(blog=False), MATCH),
    ("graphic_design", healthy(favicon=False), MATCH),
    ("video_production", healthy(video=False), MATCH),
    ("software_development", healthy(booking=[], portal=False, contact_form=False), MATCH),
    ("software_development", healthy(booking=[]), NO_MATCH),
    ("online_booking", healthy(booking=[]), MATCH),
    ("live_chat", healthy(live_chat=[]), MATCH),
    ("virtual_assistant", healthy() | {"Role": "Owner", "Phone Source": DIRECT}, MATCH),
    ("virtual_assistant", healthy() | {"Role": "Owner"}, NO_MATCH),
    ("bookkeeping", healthy() | {"Role": "Owner", "Practice Size": "1"}, MATCH),
    ("bookkeeping", healthy() | {"Role": "Clinical Director"}, NO_MATCH),
    ("accounting_tax", healthy() | {"Annual Billing": "230570"}, MATCH),
    ("payroll_hr", healthy() | {"Practice Size": "14"}, MATCH),
    ("payroll_hr", healthy(), NOT_CHECKED),
    ("medical_billing", healthy() | {"Role": "Owner", "Phone": "(512) 555-0100"}, MATCH),
    ("legal_compliance", healthy(terms=False), MATCH),
    ("legal_compliance", healthy(), NO_MATCH),
]


class TestBuiltins:

    def test_twenty_types_in_five_groups(self):
        types = lead_types.builtin_types()
        assert len(types) == 20
        assert {t.group for t in types} == set(lead_types.GROUPS)

    def test_ids_are_unique(self):
        ids = [t.id for t in lead_types.builtin_types()]
        assert len(ids) == len(set(ids))

    def test_every_check_they_use_exists(self):
        for lead_type in lead_types.builtin_types():
            for check_id in lead_type.checks:
                assert check_id in lead_types.CHECKS, (lead_type.id, check_id)

    def test_they_are_marked_builtin_and_on(self):
        assert all(t.builtin and t.enabled for t in lead_types.builtin_types())

    def test_software_development_needs_all_three(self):
        assert builtin("software_development").match == "all"

    @pytest.mark.parametrize("type_id, row, expected", BUILTIN_CASES)
    def test_each_type(self, type_id, row, expected):
        status, _reasons = lead_types.evaluate(builtin(type_id),
                                               lead_types.Evidence(row, TODAY))
        assert status == expected


class TestSelection:

    def test_nothing_selected_admits_everything(self):
        judgement = judge(lead())
        assert judgement.admitted
        assert lead_types.Selection(lead_types.builtin_types()).label() == "All leads"

    def test_a_lead_matching_any_selected_type_is_admitted(self):
        judgement = judge(healthy(viewport=False), "seo", "website_development")
        assert judgement.status == MATCH and judgement.admitted

    def test_a_lead_nobody_looked_at_is_not_checked(self):
        judgement = judge(lead(), "website_development")
        assert judgement.status == NOT_CHECKED
        assert not judgement.admitted

    def test_a_lead_that_needs_none_of_them(self):
        assert judge(healthy(), "website_development").status == NO_MATCH

    def test_the_needs_line_lists_each_reason_once(self):
        judgement = judge(healthy(https=False, viewport=False),
                          "website_development", "it_security")
        assert judgement.needs_line() == "needs: no mobile layout, no HTTPS"

    def test_no_match_means_no_needs_line(self):
        assert judge(healthy(), "website_development").needs_line() == ""

    def test_describe_lists_every_matching_type_selected_first(self):
        judgement = judge(healthy(meta_description=False, viewport=False), "seo")
        described = judgement.describe()
        assert described.startswith("SEO: no meta description")
        assert "Website Development: no mobile layout" in described

    def test_describe_is_empty_when_nothing_matches(self):
        assert judge(healthy()).describe() == ""

    def test_switched_off_types_are_ignored(self):
        types = lead_types.builtin_types()
        for lead_type in types:
            if lead_type.id == "seo":
                lead_type.enabled = False
        selection = lead_types.Selection(types, ["seo"])
        assert not selection.active
        assert "seo" not in {t.id for t in selection.types}

    def test_a_selected_type_counts_once(self):
        selection = lead_types.Selection(lead_types.builtin_types(), ["seo", "seo"])
        assert [t.id for t in selection.selected] == ["seo"]

    @pytest.mark.parametrize("selected, label", [
        (["seo"], "SEO"),
        (["website_development", "seo"], "Website Development or SEO"),
        (["website_development", "seo", "bookkeeping"],
         "Website Development, SEO or Bookkeeping"),
    ])
    def test_the_label_reads_as_a_phrase(self, selected, label):
        assert lead_types.Selection(lead_types.builtin_types(), selected).label() == label

    def test_tally(self):
        rows = [no_site(), healthy(viewport=False), healthy(), lead()]
        selection = lead_types.Selection(lead_types.builtin_types(),
                                         ["website_development"], today=TODAY)
        per_type, overall = selection.tally(rows)
        assert per_type["website_development"] == 2
        assert overall == {MATCH: 2, NO_MATCH: 1, NOT_CHECKED: 1}


class TestSpeed:

    ROWS = [healthy(viewport=(i % 2 == 0)) for i in range(10_000)]

    def test_filtering_ten_thousand_leads_takes_under_a_second(self):
        selection = lead_types.Selection(lead_types.builtin_types(),
                                         ["website_development", "seo"], today=TODAY)
        started = time.perf_counter()
        kept = [row for row in self.ROWS if selection.judge(row).admitted]
        elapsed = time.perf_counter() - started
        assert len(kept) == 5_000
        assert elapsed < 1.0, "filtering took {0:.2f}s".format(elapsed)

    def test_counting_every_type_stays_reasonable(self):
        selection = lead_types.Selection(lead_types.builtin_types(), today=TODAY)
        started = time.perf_counter()
        per_type, _overall = selection.tally(self.ROWS)
        elapsed = time.perf_counter() - started
        assert per_type["website_development"] == 5_000
        assert elapsed < 5.0, "counting took {0:.2f}s".format(elapsed)
