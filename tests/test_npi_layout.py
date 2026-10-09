"""The working-sheet layout, the second phone number, gender, and keeping the
notes people type into the spreadsheet.

Three separate promises live here, and each one fails silently if broken:
the columns land where the working sheet expects them, a database created by
an older version still opens, and a note typed into the exported file is not
erased by the next export.
"""

import sqlite3

import pytest

import storage
from storage import LeadStore, EXPORT_LAYOUT, EXPORT_FIELD
from pipeline import LeadPipeline, Reporter


def lead(**overrides):
    base = {"NPI Number": "1234567890", "Provider First Name": "Jane",
            "Provider Last Name": "Smith", "State": "TX",
            "Source": "Psychology Today"}
    base.update(overrides)
    return base


# The working sheet's own columns, exactly as pasted — including the unnamed
# one between Comments and Date.
WORKING_SHEET = [
    "NPI Number", "Provider First Name", "Provider Last Name", "Organization Name",
    "Email", "Phone", "Outreach Status", "Comments", "", "Date", "Address", "City",
    "State", "Zip", "Specialty", "Website", "Last Updated", "Source",
    "Phone Source", "All Emails Found", "Role", "LinkedIn", "Facebook",
    "Instagram", "Other Social",
]


# -------------------------------------------------------------------- layout

class TestLayout:

    def test_the_sheet_starts_with_the_working_sheet_columns_in_order(self):
        assert list(EXPORT_LAYOUT[:len(WORKING_SHEET)]) == WORKING_SHEET

    def test_gender_and_the_npi_phone_come_straight_after(self):
        """They fill the slots the working sheet leaves empty, so a paste still
        lines up with every column it already has."""
        added = EXPORT_LAYOUT[len(WORKING_SHEET):len(WORKING_SHEET) + 2]
        assert added == ("Gender", "NPI Phone")

    def test_every_stored_field_is_exported_exactly_once(self):
        exported = [EXPORT_FIELD.get(h, h) for h in EXPORT_LAYOUT]
        assert len(exported) == len(set(exported))
        stored = [field for field in exported if field not in storage.GENERATED_FIELDS]
        assert sorted(stored) == sorted(storage.HEADERS)

    def test_generated_columns_are_never_stored(self):
        for field in storage.GENERATED_FIELDS:
            assert field in EXPORT_LAYOUT
            assert field not in storage.HEADER_TO_COLUMN

    def test_only_the_spare_column_is_unnamed(self):
        assert list(EXPORT_LAYOUT).count("") == 1


# ------------------------------------------------------- old databases

class TestUpgradingAnOlderDatabase:
    """The real database was created before the Medicare columns existed, and
    CREATE TABLE IF NOT EXISTS never adds a column to a table that already
    exists — so saving a lead, and every screen, failed with "no such column".
    """

    @staticmethod
    def _old_database(path):
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet_key TEXT NOT NULL, email_key TEXT, phone_key TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                "npi" TEXT, "first_name" TEXT, "last_name" TEXT,
                "state" TEXT, "source" TEXT, "email" TEXT)
        """)
        conn.execute("""
            INSERT INTO leads (sheet_key, email_key, created_at, updated_at, npi,
                               first_name, last_name, state, source, email)
            VALUES ('Texas - Psychology Today', 'jane@practice.example', 'x', 'x',
                    '1234567890', 'Jane', 'Smith', 'TX', 'Psychology Today',
                    'jane@practice.example')
        """)
        conn.commit()
        conn.close()

    def test_missing_columns_are_added_and_old_rows_are_kept(self, tmp_path):
        path = str(tmp_path / "old.db")
        self._old_database(path)
        store = LeadStore(path)

        columns = {row[1] for row in store._conn().execute("PRAGMA table_info(leads)")}
        for _header, column in storage.FIELD_COLUMNS + storage.INTERNAL_COLUMNS:
            assert column in columns, column

        kept = next(store.iter_leads())
        assert kept["Provider Last Name"] == "Smith"
        assert kept["Email"] == "jane@practice.example"

    def test_the_upgraded_database_does_everything_the_app_needs(self, tmp_path):
        path = str(tmp_path / "old.db")
        self._old_database(path)
        store = LeadStore(path)

        assert store.get_stats()["total"] == 1
        assert store.pending_counts()["needs_npi"] == 1
        added = store.add_leads_batch([lead(**{"NPI Number": "2222222222"})])
        assert sum(added.values()) == 1

    def test_opening_it_again_changes_nothing(self, tmp_path):
        path = str(tmp_path / "old.db")
        self._old_database(path)
        LeadStore(path)
        second = LeadStore(path)
        assert second.get_stats()["total"] == 1


# ---------------------------------------------------------- NPI record fields

class TestNpiRecord:

    @staticmethod
    def _entry(**basic):
        base = {"first_name": "JANE", "last_name": "SMITH"}
        base.update(basic)
        return {
            "number": "1234567890",
            "basic": base,
            "addresses": [{"address_purpose": "LOCATION",
                           "telephone_number": "512-471-3300",
                           "address_1": "1 Main St", "city": "AUSTIN",
                           "state": "TX", "postal_code": "78701"}],
            "taxonomies": [{"primary": True, "desc": "Psychologist"}],
        }

    @pytest.mark.parametrize("sex, expected", [
        ("F", "Female"), ("M", "Male"), ("", ""), ("X", ""),
    ])
    def test_gender_comes_from_the_record(self, sex, expected, quiet_logger):
        from npi_engine import NPIEngine
        parsed = NPIEngine(logger=quiet_logger)._parse_provider(self._entry(sex=sex))
        assert parsed["Gender"] == expected

    def test_older_responses_that_call_it_gender_still_work(self, quiet_logger):
        from npi_engine import NPIEngine
        parsed = NPIEngine(logger=quiet_logger)._parse_provider(self._entry(gender="F"))
        assert parsed["Gender"] == "Female"

    def test_an_organisation_has_no_gender(self, quiet_logger):
        from npi_engine import NPIEngine
        entry = self._entry()
        entry["basic"] = {"organization_name": "HILL COUNTRY CLINIC"}
        assert NPIEngine(logger=quiet_logger)._parse_provider(entry)["Gender"] == ""

    def test_the_practice_phone_is_kept_as_the_npi_phone(self, quiet_logger):
        from npi_engine import NPIEngine
        parsed = NPIEngine(logger=quiet_logger)._parse_provider(self._entry(sex="F"))
        assert parsed["NPI Phone"] == "(512) 471-3300"


# ---------------------------------------------------------- the details pass

class FakeRegistry:
    def __init__(self, by_number=None, by_name=None):
        self.by_number = by_number or {}
        self.by_name = by_name
        self.number_calls = []
        self.name_calls = []

    def find_by_number(self, npi):
        self.number_calls.append(npi)
        record = self.by_number.get(npi)
        return dict(record) if record else None

    def find_by_name(self, first, last, state, city=None):
        self.name_calls.append((first, last, state, city))
        return dict(self.by_name) if self.by_name else None

    def find_by_organization(self, name, state, city=None):
        return None


RECORD = {"NPI Number": "1234567890", "NPI Phone": "(512) 471-3300",
          "Gender": "Female", "Address": "1 Main St", "City": "Austin",
          "Zip": "78701", "Specialty": "Psychologist"}


def build(store, registry):
    return LeadPipeline(store=store, npi_engine=registry, email_engine=None,
                        pt_engine=None, reporter=Reporter())


class TestNpiDetailsPass:

    def test_a_known_npi_gets_its_phone_and_gender_beside_the_website_number(self, store):
        store.add_leads_batch([lead(**{
            "Phone": "(512) 471-3399",
            "Phone Source": "Provider Website (tel: link)"})])
        build(store, FakeRegistry(by_number={"1234567890": RECORD})).enrich_from_npi()

        saved = next(store.iter_leads())
        assert saved["Phone"] == "(512) 471-3399", "the website number stays in Phone"
        assert saved["NPI Phone"] == "(512) 471-3300"
        assert saved["Gender"] == "Female"
        assert saved["Address"] == "1 Main St"

    def test_a_lead_with_no_phone_gets_the_registry_number(self, store):
        store.add_leads_batch([lead()])
        build(store, FakeRegistry(by_number={"1234567890": RECORD})).enrich_from_npi()
        saved = next(store.iter_leads())
        assert saved["Phone"] == "(512) 471-3300"
        assert saved["Phone Source"] == "NPI Registry"

    def test_collected_details_are_not_overwritten(self, store):
        store.add_leads_batch([lead(Address="99 Other Rd", City="Round Rock")])
        build(store, FakeRegistry(by_number={"1234567890": RECORD})).enrich_from_npi()
        saved = next(store.iter_leads())
        assert saved["Address"] == "99 Other Rd"
        assert saved["City"] == "Round Rock"

    def test_an_unmatched_psychology_today_lead_is_matched_by_name(self, store):
        store.add_leads_batch([lead(**{"NPI Number": "PT-7654321", "City": "Austin"})])
        registry = FakeRegistry(by_name=RECORD)
        build(store, registry).enrich_from_npi()

        saved = next(store.iter_leads())
        assert saved["NPI Number"] == "1234567890"
        assert saved["Gender"] == "Female"
        assert registry.name_calls == [("Jane", "Smith", "TX", "Austin")]

    def test_a_match_already_in_the_tab_keeps_the_placeholder_but_gets_details(self, store):
        """The same provider collected twice: taking the number would break the
        one-provider-per-tab rule, but the phone and gender still apply."""
        store.add_leads_batch([
            lead(**{"NPI Number": "1234567890", "NPI Checked": "2026-09-15"}),
            lead(**{"NPI Number": "PT-7654321", "Email": "jane2@practice.example"}),
        ])
        build(store, FakeRegistry(by_name=RECORD)).enrich_from_npi()

        placeholder = [l for l in store.iter_leads()
                       if l["Email"] == "jane2@practice.example"][0]
        assert placeholder["NPI Number"] == "PT-7654321"
        assert placeholder["NPI Phone"] == "(512) 471-3300"

    def test_nothing_is_guessed_when_the_registry_has_no_match(self, store):
        store.add_leads_batch([lead(**{"NPI Number": "PT-7654321"})])
        build(store, FakeRegistry(by_name=None)).enrich_from_npi()
        saved = next(store.iter_leads())
        assert saved["Gender"] == ""
        assert saved["NPI Number"] == "PT-7654321"

    def test_every_lead_is_marked_checked_and_not_looked_up_twice(self, store):
        store.add_leads_batch([lead()])
        registry = FakeRegistry()
        pipeline = build(store, registry)
        pipeline.enrich_from_npi()
        assert store.pending_counts()["needs_npi"] == 0
        pipeline.enrich_from_npi()
        assert registry.number_calls == ["1234567890"]

    def test_collection_keeps_both_numbers_and_the_gender(self, store):
        from test_pipeline import FakePTEngine, FakeEmailEngine, FakeNPIEngine, entry

        pipeline = LeadPipeline(
            store=store, npi_engine=FakeNPIEngine(match=dict(RECORD)),
            email_engine=FakeEmailEngine(contact={
                "email": "", "phone": "(512) 471-3399",
                "phone_confidence": "personal/direct (tel: link)",
                "role": "", "socials": {}}),
            pt_engine=FakePTEngine(pages=[(1, [entry()])]), reporter=Reporter())
        pipeline.collect_psychology_today("Texas", "TX")

        saved = next(store.iter_leads())
        assert saved["Phone"] == "(512) 471-3399"
        assert saved["NPI Phone"] == "(512) 471-3300"
        assert saved["Gender"] == "Female"
        assert store.pending_counts()["needs_npi"] == 0, \
            "collection already asked the registry"


# ------------------------------------------------- notes survive the export

openpyxl = pytest.importorskip("openpyxl")


def _export(store, path, **kwargs):
    from lead_manager import ExcelExporter
    return ExcelExporter(str(path)).export(store, **kwargs)


def _edit(path, sheet, row, changes):
    workbook = openpyxl.load_workbook(path)
    worksheet = workbook[sheet]
    headers = ["" if c.value is None else str(c.value) for c in worksheet[1]]
    for header, value in changes.items():
        # Assigned rather than passed to cell(): cell(value=None) leaves the
        # old value in place, which is not what emptying a cell does.
        worksheet.cell(row=row, column=headers.index(header) + 1).value = value
    workbook.save(path)
    workbook.close()


def _read(path, sheet, row, header):
    workbook = openpyxl.load_workbook(path)
    try:
        worksheet = workbook[sheet]
        headers = ["" if c.value is None else str(c.value) for c in worksheet[1]]
        return worksheet.cell(row=row, column=headers.index(header) + 1).value
    finally:
        workbook.close()


TAB = "Texas - Psychology Today"


class TestNotesSurviveTheNextExport:

    def test_notes_typed_into_a_tab_are_kept(self, store, tmp_path):
        store.add_leads_batch([lead(Phone="(512) 471-3300")])
        path = tmp_path / "leads.xlsx"
        _export(store, path, call_list=0)
        _edit(path, TAB, 2, {"Outreach Status": "VOICE MAIL",
                             "Comments": "Call back Tuesday",
                             "Date": "2026-09-15", "": "follow up"})

        _export(store, path, call_list=0)

        saved = next(store.iter_leads())
        assert saved["Outreach Status"] == "VOICE MAIL"
        assert saved["Comments"] == "Call back Tuesday"
        assert saved["Date"] == "2026-09-15"
        assert saved["Extra"] == "follow up"
        assert _read(path, TAB, 2, "Comments") == "Call back Tuesday"

    def test_a_real_excel_date_is_kept_as_a_date(self, store, tmp_path):
        import datetime
        store.add_leads_batch([lead(Phone="(512) 471-3300")])
        path = tmp_path / "leads.xlsx"
        _export(store, path, call_list=0)
        _edit(path, TAB, 2, {"Date": datetime.datetime(2026, 9, 15)})
        _export(store, path, call_list=0)
        assert next(store.iter_leads())["Date"] == "2026-09-15"

    def test_clearing_a_note_in_the_sheet_clears_it(self, store, tmp_path):
        store.add_leads_batch([lead(Comments="old note")])
        path = tmp_path / "leads.xlsx"
        _export(store, path, call_list=0)
        _edit(path, TAB, 2, {"Comments": None})
        _export(store, path, call_list=0)
        assert next(store.iter_leads())["Comments"] == ""

    def test_a_workbook_without_the_notes_columns_leaves_notes_alone(self, store, tmp_path):
        """An export from an older version has no Comments column. Its absence
        is not the same as someone emptying it."""
        store.add_leads_batch([lead(Comments="keep me")])
        path = tmp_path / "leads.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = TAB
        sheet.append(["NPI Number", "Outreach Status"])
        sheet.append(["1234567890", "Not Contacted"])
        workbook.save(path)

        _export(store, path, call_list=0)
        assert next(store.iter_leads())["Comments"] == "keep me"

    def test_generated_columns_are_not_read_back(self, store, tmp_path):
        """Only what people are meant to type is kept; the rest is rewritten."""
        store.add_leads_batch([lead(Email="jane@practice.example")])
        path = tmp_path / "leads.xlsx"
        _export(store, path, call_list=0)
        _edit(path, TAB, 2, {"Email": "typed@over.example"})
        _export(store, path, call_list=0)
        assert next(store.iter_leads())["Email"] == "jane@practice.example"

    def test_call_list_outcome_and_notes_are_kept(self, store, tmp_path):
        store.add_leads_batch([lead(Phone="(512) 471-3300", Role="Owner")])
        path = tmp_path / "leads.xlsx"
        _export(store, path, call_list=10)
        _edit(path, "Call List", 2, {"Outcome": "Meeting", "Notes": "Booked Friday"})

        _export(store, path, call_list=10)

        saved = next(store.iter_leads())
        assert saved["Outreach Status"] == "Meeting"
        assert saved["Comments"] == "Booked Friday"
        assert _read(path, "Call List", 2, "Notes") == "Booked Friday"

    def test_the_state_tab_wins_when_both_were_edited(self, store, tmp_path):
        store.add_leads_batch([lead(Phone="(512) 471-3300", Role="Owner")])
        path = tmp_path / "leads.xlsx"
        _export(store, path, call_list=10)
        _edit(path, "Call List", 2, {"Notes": "from the call list"})
        _edit(path, TAB, 2, {"Comments": "from the state tab"})
        _export(store, path, call_list=10)
        assert next(store.iter_leads())["Comments"] == "from the state tab"

    def test_the_exported_tab_uses_the_working_sheet_order(self, store, tmp_path):
        store.add_leads_batch([lead(Phone="(512) 471-3300")])
        path = tmp_path / "leads.xlsx"
        _export(store, path, call_list=0)
        workbook = openpyxl.load_workbook(path)
        try:
            headers = ["" if c.value is None else str(c.value)
                       for c in workbook[TAB][1]]
        finally:
            workbook.close()
        assert headers == list(EXPORT_LAYOUT)
