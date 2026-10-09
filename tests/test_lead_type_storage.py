"""Storage for lead types: the facts are kept, never exported, and the only
change to the spreadsheet is one column on the end."""

import sqlite3
from datetime import date

import lead_types
import site_audit
import storage
from conftest import healthy_site_facts
from storage import EXPORT_LAYOUT, LeadStore

TODAY = date(2026, 10, 9)


def lead(npi, phone, **fields):
    base = {"NPI Number": npi, "Provider First Name": "Jane", "Provider Last Name": "Smith",
            "State": "TX", "Source": "Psychology Today", "Phone": phone,
            "Facebook": "https://facebook.com/x", "Instagram": "https://instagram.com/x"}
    base.update(fields)
    return base


def facts(value):
    return site_audit.dump_facts(value)


NO_SITE = facts(site_audit.no_site_facts(site_audit.NO_SITE_LISTED, today=TODAY))
HEALTHY = facts(healthy_site_facts(TODAY))


class TestColumns:

    def test_lead_types_is_the_only_new_export_column(self):
        assert EXPORT_LAYOUT[-1] == "Lead Types"
        assert EXPORT_LAYOUT[-2] == "First Seen"
        for internal in ("Site Facts", "Site Text", "Site Checked"):
            assert internal not in EXPORT_LAYOUT

    def test_site_columns_are_stored(self, store):
        store.add_leads_batch([lead("1111111111", "(512) 471-3301", **{
            "Site Facts": NO_SITE, "Site Checked": "2026-10-09"})])
        saved = next(store.iter_leads())
        assert saved["Site Facts"] == NO_SITE
        assert saved["Site Checked"] == "2026-10-09"

    def test_an_older_database_gains_the_site_columns(self, tmp_path, quiet_logger):
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sheet_key TEXT NOT NULL,
            email_key TEXT, phone_key TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, npi TEXT, state TEXT, source TEXT,
            email TEXT, email_status TEXT)""")
        conn.commit()
        conn.close()
        LeadStore(path, logger=quiet_logger)
        conn = sqlite3.connect(path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
        conn.close()
        assert {"site_facts", "site_text", "site_checked"} <= columns


class TestFilters:

    def test_needs_site_check_is_websites_nobody_has_read(self, store):
        store.add_leads_batch([
            lead("1111111111", "(512) 471-3301", Website="https://a.example"),
            lead("2222222222", "(512) 471-3302", Website="https://b.example",
                 **{"Site Checked": "2026-10-01", "Site Facts": HEALTHY}),
            lead("3333333333", "(512) 471-3303"),
        ])
        assert store.count(needs_site_check=True) == 1
        assert store.pending_counts()["needs_site_check"] == 1
        assert [l["Website"] for l in store.iter_leads(needs_site_check=True)] == \
            ["https://a.example"]

    def test_created_since(self, store):
        store.add_leads_batch([lead("1111111111", "(512) 471-3301")])
        with store._lock:
            conn = store._conn()
            conn.execute("UPDATE leads SET created_at = '2026-01-01T00:00:00+00:00'")
            conn.commit()
        since = storage.utcnow()
        store.add_leads_batch([lead("2222222222", "(512) 471-3302")])
        assert [l["NPI Number"] for l in store.iter_leads(created_since=since)] == \
            ["2222222222"]

    def test_utcnow_is_the_stored_format(self):
        assert storage.utcnow()[10] == "T" and storage.utcnow().endswith("+00:00")


class TestBestLeadsByLeadType:

    def seed(self, store):
        store.add_leads_batch([
            lead("1111111111", "(512) 471-3301", **{"Site Facts": NO_SITE}),
            lead("2222222222", "(512) 471-3302", **{"Site Facts": HEALTHY}),
            lead("3333333333", "(512) 471-3303"),
        ])

    def test_only_leads_that_need_it_come_back(self, store):
        self.seed(store)
        selection = lead_types.Selection(lead_types.builtin_types(),
                                         ["website_development"], today=TODAY)
        chosen = store.best_leads(limit=0, lead_types=selection)
        assert [l["NPI Number"] for l in chosen] == ["1111111111"]
        assert "needs: no website of their own" in chosen[0]["Why"]

    def test_nothing_selected_filters_nothing(self, store):
        self.seed(store)
        selection = lead_types.Selection(lead_types.builtin_types(), today=TODAY)
        assert len(store.best_leads(limit=0, lead_types=selection)) == 3

    def test_no_selection_object_filters_nothing(self, store):
        self.seed(store)
        assert len(store.best_leads(limit=0)) == 3
