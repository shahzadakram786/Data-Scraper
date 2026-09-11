"""Storage rules: what counts as a duplicate, what a lead is worth, and
whether a stopped run can pick up where it left off.

The duplicate rules in particular are load-bearing. Rejecting on phone number
silently discarded every colleague after the first at a group practice, which
is why a repeated phone is explicitly *not* a duplicate here.
"""

import pytest

import storage
from storage import LeadStore, compute_lead_score, RUN_OK, RUN_EMPTY, RUN_FAILED


def lead(**overrides):
    base = {
        "NPI Number": "1234567890",
        "Provider First Name": "Jane",
        "Provider Last Name": "Smith",
        "State": "TX",
        "Source": "Psychology Today",
    }
    base.update(overrides)
    return base


class TestDuplicateRules:

    def test_the_same_provider_is_not_added_twice(self, store):
        assert sum(store.add_leads_batch([lead()]).values()) == 1
        assert sum(store.add_leads_batch([lead()]).values()) == 0

    def test_an_email_belongs_to_exactly_one_lead(self, store):
        store.add_leads_batch([lead(Email="jane@practice.example")])
        added = store.add_leads_batch([
            lead(**{"NPI Number": "2222222222", "Email": "jane@practice.example"})])
        assert sum(added.values()) == 0, "the same address cannot be two leads"

    def test_a_shared_phone_is_kept_and_flagged(self, store):
        """Colleagues at one group practice genuinely publish the same
        front-desk line. Treating that as a duplicate discarded all but one."""
        store.add_leads_batch([lead(Phone="(214) 692-0177")])
        added = store.add_leads_batch([lead(**{
            "NPI Number": "2222222222",
            "Provider Last Name": "Jones",
            "Phone": "(214) 692-0177",
        })])
        assert sum(added.values()) == 1

        jones = [l for l in store.iter_leads() if l["Provider Last Name"] == "Jones"][0]
        assert "shared practice line" in jones["Phone Source"]

    def test_the_same_npi_in_a_different_state_is_a_different_tab(self, store):
        store.add_leads_batch([lead()])
        added = store.add_leads_batch([lead(State="OH")])
        assert sum(added.values()) == 1
        assert set(store.sheet_names()) == {
            "Texas - Psychology Today", "Ohio - Psychology Today"}

    @pytest.mark.parametrize("phone_a, phone_b", [
        ("(423) 555-1234", "423-555-1234"),
        ("4235551234", "+1 423 555 1234"),
    ])
    def test_phone_formatting_does_not_affect_matching(self, phone_a, phone_b):
        assert (storage.normalize_phone_key(phone_a)
                == storage.normalize_phone_key(phone_b))


class TestSheetNaming:

    @pytest.mark.parametrize("state, source, expected", [
        ("TX", "Psychology Today", "Texas - Psychology Today"),
        ("OH", "NPI Registry", "Ohio - NPI Registry"),
        # "District of Columbia - Psychology Today" is 39 characters, over
        # Excel's 31-character limit, so the state falls back to its code.
        ("DC", "Psychology Today", "DC - Psychology Today"),
    ])
    def test_tab_names_fit_excels_limit(self, state, source, expected):
        name = LeadStore.sheet_key_for({"State": state, "Source": source})
        assert name == expected
        assert len(name) <= LeadStore.MAX_SHEET_NAME

    def test_illegal_sheet_characters_are_stripped(self):
        name = LeadStore.sheet_key_for({"State": "TX", "Source": "A/B*C?[D]"})
        assert not any(c in name for c in r'\/*?[]:')

    def test_source_is_recovered_from_a_tab_name(self):
        assert LeadStore.source_of_sheet("Texas - Psychology Today") == "Psychology Today"
        assert LeadStore.source_of_sheet("NPI Registry") == "NPI Registry"


class TestLeadScoring:
    """The score decides what a salesperson sees first, so the ordering
    between these cases matters more than the exact numbers."""

    def test_a_confirmed_direct_owner_outranks_everything(self):
        best = compute_lead_score({
            "Email": "jane@practice.example", "Email Status": "valid",
            "Phone": "(512) 471-3399",
            "Phone Source": "Provider Website (personal/direct (tel: link))",
            "Role": "Owner", "Website": "https://practice.example",
            "NPI Number": "1234567890"})
        weakest = compute_lead_score({"NPI Number": "1234567890"})
        assert best > 90
        assert best > weakest

    def test_a_bouncing_address_scores_zero(self):
        """An address that will bounce is worse than no address: it burns
        sender reputation."""
        assert compute_lead_score({
            "Email": "dead@nowhere.example", "Email Status": "invalid"}) == 0

    def test_a_direct_line_beats_a_shared_one(self):
        direct = compute_lead_score({
            "Phone": "x", "Phone Source": "Provider Website (personal/direct)"})
        shared = compute_lead_score({
            "Phone": "x", "Phone Source": "Provider Website (shared practice line)"})
        assert direct > shared

    def test_an_owner_beats_a_clinical_director(self):
        owner = compute_lead_score({"Role": "Owner"})
        director = compute_lead_score({"Role": "Clinical Director"})
        assert owner > director > 0

    def test_the_score_is_capped(self):
        assert compute_lead_score({
            "Email": "a@b.example", "Email Status": "valid",
            "Phone": "x", "Phone Source": "personal/direct",
            "Role": "Owner", "Website": "w", "LinkedIn": "l",
            "NPI Number": "1234567890"}) <= 100

    def test_the_score_is_recomputed_when_a_lead_changes(self, store):
        store.add_leads_batch([lead()])
        row = next(store.iter_leads())
        before = int(row["Lead Score"])
        store.update_lead(row["_id"], {
            "Email": "jane@practice.example", "Email Status": "valid"})
        after = int(next(store.iter_leads())["Lead Score"])
        assert after > before


class TestQueries:

    def test_filters_find_the_right_leads(self, store):
        store.add_leads_batch([
            lead(**{"NPI Number": "1111111111", "Email": "a@x.example",
                    "Email Status": "valid"}),
            lead(**{"NPI Number": "2222222222", "Email": "b@x.example"}),
            lead(**{"NPI Number": "3333333333"}),
        ])
        assert store.count(missing_email=True) == 1
        assert store.count(has_email=True) == 2
        assert store.count(unverified=True) == 1, "has an email but no status yet"
        assert store.count(email_statuses=["valid"]) == 1

    def test_stats_add_up(self, store):
        store.add_leads_batch([
            lead(**{"NPI Number": "1111111111", "Email": "a@x.example",
                    "Email Status": "valid", "Phone": "(512) 471-3300"}),
            lead(**{"NPI Number": "2222222222", "State": "OH"}),
        ])
        stats = store.get_stats()
        assert stats["total"] == 2
        assert stats["with_email"] == 1
        assert stats["with_phone"] == 1
        assert stats["verified_email"] == 1
        assert stats["by_state"]["TX"]["total"] == 1
        assert stats["by_state"]["OH"]["total"] == 1

    def test_an_update_cannot_steal_another_leads_email(self, store):
        store.add_leads_batch([
            lead(**{"NPI Number": "1111111111", "Email": "taken@x.example"}),
            lead(**{"NPI Number": "2222222222"}),
        ])
        second = [l for l in store.iter_leads() if l["NPI Number"] == "2222222222"][0]
        assert not store.update_lead(second["_id"], {"Email": "taken@x.example"})
        assert store.count(has_email=True) == 1


class TestResumeAndHistory:

    def test_a_checkpoint_survives_a_round_trip(self, store):
        store.set_checkpoint("pt:Texas", {"next_page": 7})
        assert store.get_checkpoint("pt:Texas")["next_page"] == 7

    def test_clearing_a_checkpoint_removes_it(self, store):
        store.set_checkpoint("pt:Texas", {"next_page": 7})
        store.clear_checkpoint("pt:Texas")
        assert store.get_checkpoint("pt:Texas") is None

    def test_a_missing_checkpoint_is_not_an_error(self, store):
        assert store.get_checkpoint("never-run") is None

    def test_runs_record_their_real_outcome(self, store):
        """A run that collected nothing must not be filed as a success — that
        is precisely how four broken runs went unnoticed for ten days."""
        run_id = store.start_run("pt_collect", "Texas")
        store.finish_run(run_id, RUN_EMPTY, added=0,
                         message="no providers parsed", stats=store.get_stats())
        recorded = store.recent_runs(1)[0]
        assert recorded["status"] == RUN_EMPTY
        assert recorded["message"] == "no providers parsed"

    def test_hit_rate_is_computed_from_stats(self, store):
        store.add_leads_batch([
            lead(**{"NPI Number": "1111111111", "Email": "a@x.example"}),
            lead(**{"NPI Number": "2222222222"}),
        ])
        run_id = store.start_run("email_find")
        store.finish_run(run_id, RUN_OK, added=1, stats=store.get_stats())
        assert store.recent_runs(1)[0]["hit_rate"] == 50.0


class TestExcelMigration:
    """Leads collected before the database existed must not be stranded in a
    file the app no longer treats as storage."""

    def test_an_existing_workbook_is_imported(self, store, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "legacy.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Texas - Psychology Today"
        sheet.append(["NPI Number", "Provider First Name", "Provider Last Name",
                      "Email", "State", "Source"])
        sheet.append(["1234567890", "Jane", "Smith", "jane@practice.example",
                      "TX", "Psychology Today"])
        sheet.append(["2222222222", "Bob", "Jones", "", "TX", "Psychology Today"])
        workbook.save(path)

        assert store.import_from_excel(str(path)) == 2
        assert store.get_stats()["total"] == 2
        # Running it again must not duplicate anything.
        assert store.import_from_excel(str(path)) == 0

    def test_importing_a_file_that_is_not_there_is_harmless(self, store):
        assert store.import_from_excel("does-not-exist.xlsx") == 0


class TestRunHistoryMigration:

    def _write(self, tmp_path, records):
        import json
        path = tmp_path / "run_history.json"
        path.write_text(json.dumps(records), encoding="utf-8")
        return str(path)

    def test_past_runs_are_brought_across(self, store, tmp_path):
        path = self._write(tmp_path, [
            {"timestamp": "2026-08-07T20:00:00", "action": "pt_collect",
             "total": 821, "with_email": 430, "hit_rate": 52.4},
        ])
        assert store.import_run_history(path) == 1
        assert store.recent_runs(1)[0]["hit_rate"] == 52.4

    def test_a_zero_lead_run_is_relabelled_as_empty(self, store, tmp_path):
        """These runs recorded themselves as successes at the time. They were
        the scraper silently broken, and the history should say so."""
        path = self._write(tmp_path, [
            {"timestamp": "2026-08-20T10:00:00", "action": "pt_collect",
             "total": 0, "with_email": 0, "hit_rate": 0.0},
        ])
        store.import_run_history(path)
        assert store.recent_runs(1)[0]["status"] == RUN_EMPTY

    def test_importing_twice_does_not_duplicate(self, store, tmp_path):
        path = self._write(tmp_path, [
            {"timestamp": "2026-08-07T20:00:00", "action": "pt_collect",
             "total": 10, "with_email": 5, "hit_rate": 50.0},
        ])
        assert store.import_run_history(path) == 1
        assert store.import_run_history(path) == 0

    def test_a_corrupt_history_file_is_survivable(self, store, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json at all", encoding="utf-8")
        assert store.import_run_history(str(path)) == 0


class TestGeneratedSheetsAreNotReimported:
    """The exporter writes Summary and Call List tabs. Reading those back in
    would feed a rendering of the data into the data."""

    def test_generated_tabs_are_skipped_on_import(self, store, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        path = tmp_path / "exported.xlsx"
        workbook = openpyxl.Workbook()

        summary = workbook.active
        summary.title = "Summary"
        summary.append(["NPI Number", "Provider First Name", "State", "Source"])
        summary.append(["9999999999", "Not", "XX", "A Lead"])

        call_list = workbook.create_sheet("Call List")
        call_list.append(["NPI Number", "Provider First Name", "State", "Source"])
        call_list.append(["8888888888", "Also Not", "XX", "A Lead"])

        real = workbook.create_sheet("Texas - Psychology Today")
        real.append(["NPI Number", "Provider First Name", "State", "Source"])
        real.append(["1234567890", "Jane", "TX", "Psychology Today"])
        workbook.save(path)

        assert store.import_from_excel(str(path)) == 1
        assert [l["Provider First Name"] for l in store.iter_leads()] == ["Jane"]
