"""The export with lead types: only the leads that need what was chosen, each
with its reasons in a Lead Types column on the end."""

from datetime import date

import pytest

import lead_types
import site_audit
from conftest import healthy_site_facts

openpyxl = pytest.importorskip("openpyxl")
from lead_manager import ExcelExporter  # noqa: E402

TODAY = date(2026, 10, 9)
TAB = "Texas - Psychology Today"


def lead(npi, phone, facts):
    return {"NPI Number": npi, "Provider First Name": "Jane", "Provider Last Name": "Smith",
            "State": "TX", "Source": "Psychology Today", "Phone": phone,
            "Facebook": "https://facebook.com/x", "Instagram": "https://instagram.com/x",
            "Site Facts": site_audit.dump_facts(facts)}


@pytest.fixture
def seeded(store):
    store.add_leads_batch([
        lead("1111111111", "(512) 471-3301",
             site_audit.no_site_facts(site_audit.NO_SITE_LISTED, today=TODAY)),
        lead("2222222222", "(512) 471-3302", healthy_site_facts(TODAY)),
    ])
    return store


def selection(*ids):
    return lead_types.Selection(lead_types.builtin_types(), ids, today=TODAY)


def rows(path, sheet):
    workbook = openpyxl.load_workbook(path)
    try:
        worksheet = workbook[sheet]
        headers = [c.value for c in worksheet[1]]
        data = [dict(zip(headers, [c.value for c in row]))
                for row in worksheet.iter_rows(min_row=2)]
        return headers, data
    finally:
        workbook.close()


class TestExport:

    def test_only_leads_that_need_it_are_exported(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        written = ExcelExporter(path).export(seeded, call_list=0,
                                             lead_types=selection("website_development"))
        assert written == 1
        _headers, data = rows(path, TAB)
        assert [r["NPI Number"] for r in data] == ["1111111111"]
        assert data[0]["Lead Types"] == "Website Development: no website of their own"

    def test_the_column_is_last(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        ExcelExporter(path).export(seeded, call_list=0, lead_types=selection())
        headers, _data = rows(path, TAB)
        assert headers[-1] == "Lead Types"

    def test_with_nothing_chosen_every_lead_is_exported_with_its_matches(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        assert ExcelExporter(path).export(seeded, call_list=0, lead_types=selection()) == 2
        _headers, data = rows(path, TAB)
        by_npi = {r["NPI Number"]: r for r in data}
        assert by_npi["1111111111"]["Lead Types"].startswith("Website Development")
        assert not by_npi["2222222222"]["Lead Types"]

    def test_no_selection_object_leaves_the_column_blank(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        assert ExcelExporter(path).export(seeded, call_list=0) == 2
        _headers, data = rows(path, TAB)
        assert not any(r["Lead Types"] for r in data)

    def test_nothing_matching_leaves_the_file_alone(self, seeded, tmp_path):
        path = tmp_path / "out.xlsx"
        assert ExcelExporter(str(path)).export(seeded, call_list=0,
                                               lead_types=selection("seo")) == 0
        assert not path.exists()

    def test_the_call_list_follows_the_selection(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        ExcelExporter(path).export(seeded, call_list=10,
                                   lead_types=selection("website_development"))
        _headers, data = rows(path, "Call List")
        assert len(data) == 1
        assert "needs: no website of their own" in data[0]["Why this lead"]

    def test_google_sheets_empties_a_tab_that_matched_nothing(self, seeded):
        """Excel rebuilds the whole workbook; a Google Sheet's tabs persist.
        A tab with no match this time must not keep the last export's rows."""
        gspread = pytest.importorskip("gspread")
        from lead_manager import GoogleSheetsExporter
        from storage import EXPORT_LAYOUT

        class Tab:
            def __init__(self, title):
                self.title = title
                self.rows = [["old", "row"]] * 5
                self.row_count, self.col_count = 100, len(EXPORT_LAYOUT)

            def get_all_values(self):
                return []

            def clear(self):
                self.rows = []

            def update(self, values, start, value_input_option=None):
                self.rows = values

        class Spreadsheet:
            def __init__(self):
                self.tabs = {TAB: Tab(TAB)}

            def worksheets(self):
                return list(self.tabs.values())

            def worksheet(self, title):
                if title not in self.tabs:
                    raise gspread.WorksheetNotFound(title)
                return self.tabs[title]

        exporter = GoogleSheetsExporter.__new__(GoogleSheetsExporter)
        exporter.logger = seeded.logger
        exporter.spreadsheet = Spreadsheet()
        assert exporter.export(seeded, lead_types=selection("seo")) == 0
        assert exporter.spreadsheet.tabs[TAB].rows == [list(EXPORT_LAYOUT)]

    def test_the_summary_names_the_selection(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        ExcelExporter(path).export(seeded, call_list=0,
                                   lead_types=selection("website_development"))
        workbook = openpyxl.load_workbook(path)
        try:
            values = [c.value for row in workbook["Summary"].iter_rows() for c in row]
        finally:
            workbook.close()
        assert "Lead types" in values
        assert "Website Development" in values
