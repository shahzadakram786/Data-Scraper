import os
import re
import tempfile
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import logging

class BaseLeadManager:
    # Tab color per Source, used by both output types so the tabs are
    # visually distinguishable at a glance, not just by label.
    TAB_COLORS = {
        "NPI Registry": "1F4E78",
        "Psychology Today": "2E7D32",
        "ZocDoc (NPI Import)": "B45309",
    }
    DEFAULT_TAB_COLOR = "555555"

    # Excel caps sheet names at 31 characters and Google Sheets is no more
    # generous, so full state names are used only when the combined
    # "<State> - <Source>" fits; otherwise the state falls back to its
    # abbreviation. "Texas - Psychology Today" fits (24); "District of
    # Columbia - Psychology Today" (39) becomes "DC - Psychology Today".
    MAX_SHEET_NAME = 31

    STATE_NAMES = {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
        "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
        "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
        "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
        "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
        "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
        "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
        "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
        "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
        "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
        "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
        "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
        "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
    }

    @classmethod
    def _sheet_key_for(cls, lead_data):
        """Leads are filed into one tab per state *and* source — "Texas -
        Psychology Today", "Ohio - NPI Registry" — so each tab answers "what
        did this platform give me in this state". Sheet names can't contain
        \\ / ? * [ ] : and are capped at 31 characters."""
        source = cls._clean_sheet_part(lead_data.get("Source") or "Unknown") or "Unknown"
        abbr = cls._clean_sheet_part(lead_data.get("State") or "").upper()
        if not abbr:
            return source[:cls.MAX_SHEET_NAME]
        full = cls._clean_sheet_part(cls.STATE_NAMES.get(abbr, abbr))
        for state_part in (full, abbr):
            name = f"{state_part} - {source}"
            if len(name) <= cls.MAX_SHEET_NAME:
                return name
        # Even the abbreviation doesn't fit — trim the source rather than the
        # state, so the tab still says which state it belongs to.
        return f"{abbr} - {source}"[:cls.MAX_SHEET_NAME].rstrip(" -")

    @staticmethod
    def _clean_sheet_part(value):
        return re.sub(r'[\\/*?:\[\]]', '', str(value)).strip()

    @classmethod
    def _source_of_sheet(cls, sheet_name):
        """Recovers the Source from a "<State> - <Source>" tab name, so tab
        colouring keeps working now that the name isn't the bare source."""
        return sheet_name.split(" - ", 1)[-1] if " - " in sheet_name else sheet_name

    def __init__(self):
        self.headers = [
            "NPI Number", "Provider First Name", "Provider Last Name",
            "Organization Name", "Phone", "Email", "Address",
            "City", "State", "Zip", "Specialty", "Website", "Outreach Status", "Last Updated",
            "Source", "Phone Source", "All Emails Found",
            # New columns must be appended at the END: rows are written
            # positionally, so inserting one mid-list would shift every value
            # in sheets that already exist under the old header order.
            "Role",
        ]

    def is_duplicate(self, npi, sheet_key):
        raise NotImplementedError

    def add_leads_batch(self, leads):
        raise NotImplementedError

    def get_stats(self):
        raise NotImplementedError

    @staticmethod
    def _normalize_email_key(email):
        e = str(email or "").strip().lower()
        return e or None

    @staticmethod
    def _normalize_phone_key(phone):
        """Strips formatting so "(423) 555-1234", "423-555-1234" and
        "4235551234" all compare as the same number."""
        digits = re.sub(r'\D', '', str(phone or ""))
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        return digits or None


class ExcelManager(BaseLeadManager):
    HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    HEADER_BORDER = Border(
        left=Side(style="thin", color="16324A"), right=Side(style="thin", color="16324A"),
        top=Side(style="thin", color="16324A"), bottom=Side(style="thin", color="16324A"))

    DATA_FONT = Font(name="Calibri", size=10)
    DATA_BORDER = Border(
        left=Side(style="thin", color="D9D9D9"), right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"), bottom=Side(style="thin", color="D9D9D9"))
    STRIPE_FILL = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")
    EMAIL_FOUND_FILL = PatternFill(start_color="D9F2D9", end_color="D9F2D9", fill_type="solid")

    def __init__(self, filename="RCM_Provider_Leads.xlsx"):
        super().__init__()
        self.filename = filename
        self._initialize_excel()

    def _initialize_excel(self):
        """No-op: the workbook is created on first write by add_leads_batch."""
        pass

    def save_workbook(self, wb):
        """Saves via a temp file + atomic rename instead of writing straight
        to self.filename. If the save fails partway (e.g. the workbook ends
        up with zero visible sheets, disk fills up, the process gets killed),
        the temp file is discarded and the real file is left exactly as it
        was — never truncated or left corrupted mid-write."""
        if not wb.sheetnames:
            return  # nothing to persist; avoid openpyxl's "at least one sheet
                     # must be visible" error, which would otherwise still
                     # truncate the target file before raising
        target_dir = os.path.dirname(os.path.abspath(self.filename)) or "."
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".xlsx", dir=target_dir)
        os.close(tmp_fd)
        try:
            wb.save(tmp_path)
            os.replace(tmp_path, self.filename)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    def _setup_sheet(self, ws):
        for col_num, header in enumerate(self.headers, 1):
            cell = ws.cell(row=1, column=col_num, value=header)
            cell.font = self.HEADER_FONT
            cell.fill = self.HEADER_FILL
            cell.border = self.HEADER_BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 20
        ws.freeze_panes = "A2"
        ws.sheet_properties.tabColor = self.TAB_COLORS.get(
            self._source_of_sheet(ws.title), self.DEFAULT_TAB_COLOR)

    def _ensure_headers(self, ws):
        """Adds header cells for any columns introduced since a sheet was
        created, so a workbook written by an older version keeps working: rows
        are appended positionally, and new columns are only ever appended at
        the end of self.headers, so this is purely additive — existing columns
        and the data already under them are never moved."""
        existing = [ws.cell(row=1, column=i + 1).value for i in range(ws.max_column)]
        for idx, header in enumerate(self.headers, 1):
            if idx <= len(existing) and existing[idx - 1] == header:
                continue
            if idx <= len(existing) and existing[idx - 1] not in (None, ""):
                continue  # a column we don't recognise — leave it alone
            cell = ws.cell(row=1, column=idx, value=header)
            cell.font = self.HEADER_FONT
            cell.fill = self.HEADER_FILL
            cell.border = self.HEADER_BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center")

    def _style_data_row(self, ws, row_num, has_email):
        stripe = (row_num % 2 == 0)
        for col_num in range(1, len(self.headers) + 1):
            cell = ws.cell(row=row_num, column=col_num)
            cell.font = self.DATA_FONT
            cell.border = self.DATA_BORDER
            if stripe:
                cell.fill = self.STRIPE_FILL
        if has_email:
            email_col = self.headers.index("Email") + 1
            ws.cell(row=row_num, column=email_col).fill = self.EMAIL_FOUND_FILL

    def is_duplicate(self, npi, sheet_key):
        """Check if an NPI already exists in the workbook under the given source's tab."""
        if not os.path.exists(self.filename):
            return False
        # Use a context-manager-like approach: load, check, close
        wb = openpyxl.load_workbook(self.filename, read_only=True)
        try:
            if sheet_key not in wb.sheetnames:
                return False
            ws = wb[sheet_key]
            for row in ws.iter_rows(min_row=2, max_col=1, values_only=True):
                if str(row[0]) == str(npi):
                    return True
            return False
        finally:
            wb.close()  # Always close to release Windows file lock

    def add_leads_batch(self, leads):
        """Add a batch of leads to the workbook, one tab per Source.
        A lead is skipped as a duplicate if its NPI already exists in that
        tab, OR if its Email or Phone already exists anywhere in the workbook
        (e.g. two providers sharing one practice's front-desk contact) — every
        email and phone number in the sheet stays unique.
        Returns a dict of {source: count_added} for leads actually inserted."""
        counts_by_source = {}
        if not leads:
            return counts_by_source
        # Create a fresh workbook if file doesn't exist yet, otherwise load existing
        if not os.path.exists(self.filename):
            wb = openpyxl.Workbook()
            # Remove the default empty sheet — we'll add proper source sheets below
            if "Sheet" in wb.sheetnames:
                wb.remove(wb["Sheet"])
        else:
            try:
                wb = openpyxl.load_workbook(self.filename)
            except Exception:
                # File is corrupt (e.g. from a crashed previous run) — start fresh
                os.remove(self.filename)
                wb = openpyxl.Workbook()
                if "Sheet" in wb.sheetnames:
                    wb.remove(wb["Sheet"])
        try:
            email_idx = self.headers.index("Email")
            phone_idx = self.headers.index("Phone")

            seen_npis_by_sheet = {}
            seen_emails = set()
            seen_phones = set()
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                npis = set()
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if row is None or row[0] is None:
                        continue
                    npis.add(str(row[0]))
                    if email_idx < len(row):
                        email_key = self._normalize_email_key(row[email_idx])
                        if email_key:
                            seen_emails.add(email_key)
                    if phone_idx < len(row):
                        phone_key = self._normalize_phone_key(row[phone_idx])
                        if phone_key:
                            seen_phones.add(phone_key)
                seen_npis_by_sheet[sheet_name] = npis

            for lead_data in leads:
                sheet_key = self._sheet_key_for(lead_data)
                npi = str(lead_data.get("NPI Number"))
                email_key = self._normalize_email_key(lead_data.get("Email"))
                phone_key = self._normalize_phone_key(lead_data.get("Phone"))

                if npi in seen_npis_by_sheet.get(sheet_key, ()):
                    continue
                if email_key and email_key in seen_emails:
                    continue
                if phone_key and phone_key in seen_phones:
                    continue

                # Only create the tab once we know this lead will actually be
                # inserted — otherwise a source whose only candidate leads
                # turn out to be duplicates would leave behind an empty tab.
                if sheet_key not in wb.sheetnames:
                    ws = wb.create_sheet(sheet_key)
                    self._setup_sheet(ws)
                    seen_npis_by_sheet[sheet_key] = set()
                else:
                    ws = wb[sheet_key]
                    self._ensure_headers(ws)

                row_data = [lead_data.get(h, "") for h in self.headers]
                ws.append(row_data)
                self._style_data_row(ws, ws.max_row, bool(lead_data.get("Email")))
                counts_by_source[sheet_key] = counts_by_source.get(sheet_key, 0) + 1

                seen_npis_by_sheet[sheet_key].add(npi)
                if email_key:
                    seen_emails.add(email_key)
                if phone_key:
                    seen_phones.add(phone_key)

            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                self._auto_fit_columns(ws)
                if ws.max_row > 1:
                    ws.auto_filter.ref = ws.dimensions
            self.save_workbook(wb)
        finally:
            wb.close()  # Always close to release Windows file lock
        return counts_by_source

    def get_stats(self):
        """Compute total leads, email/website hit counts, and breakdowns by
        source (one per tab), by state, and by specialty (both read from their
        respective columns across all tabs, independent of tab layout)."""
        stats = {"total": 0, "with_email": 0, "with_website": 0,
                  "by_state": {}, "by_specialty": {}, "by_source": {}}
        if not os.path.exists(self.filename):
            return stats
        email_idx = self.headers.index("Email")
        website_idx = self.headers.index("Website")
        specialty_idx = self.headers.index("Specialty")
        state_idx = self.headers.index("State")
        wb = openpyxl.load_workbook(self.filename, read_only=True)
        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                sheet_total = 0
                sheet_email = 0
                for row in ws.iter_rows(min_row=2, values_only=True):
                    if row is None or row[0] is None:
                        continue
                    sheet_total += 1
                    has_email = bool(row[email_idx])
                    if has_email:
                        sheet_email += 1
                    if row[website_idx]:
                        stats["with_website"] += 1
                    specialty = row[specialty_idx] or "Unknown"
                    stats["by_specialty"][specialty] = stats["by_specialty"].get(specialty, 0) + 1
                    state = row[state_idx] or "Unknown"
                    state_bucket = stats["by_state"].setdefault(state, {"total": 0, "with_email": 0})
                    state_bucket["total"] += 1
                    if has_email:
                        state_bucket["with_email"] += 1
                if sheet_total:
                    stats["by_source"][sheet_name] = {"total": sheet_total, "with_email": sheet_email}
                    stats["total"] += sheet_total
                    stats["with_email"] += sheet_email
        finally:
            wb.close()
        return stats

    def _auto_fit_columns(self, ws):
        for col in ws.columns:
            max_length = 0
            column = col[0].column_letter
            for cell in col:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            ws.column_dimensions[column].width = max(10, min(max_length + 2, 45))

class GoogleSheetsManager(BaseLeadManager):
    # Sheets API color format: 0-1 floats, keyed the same way as TAB_COLORS' hex values.
    TAB_COLORS_RGB = {
        "NPI Registry": {"red": 0.122, "green": 0.306, "blue": 0.471},
        "Psychology Today": {"red": 0.180, "green": 0.490, "blue": 0.196},
        "ZocDoc (NPI Import)": {"red": 0.706, "green": 0.325, "blue": 0.035},
    }
    DEFAULT_TAB_COLOR_RGB = {"red": 0.33, "green": 0.33, "blue": 0.33}

    def __init__(self, credentials_path, spreadsheet_name):
        super().__init__()
        self.credentials_path = credentials_path
        self.spreadsheet_name = spreadsheet_name
        self.client = self._authenticate()
        self.spreadsheet = self._get_or_create_spreadsheet()
        # In-memory NPI cache per source tab, and global email/phone caches
        # (uniqueness is enforced across all tabs, not just within one), to
        # minimize API calls.
        self._npi_cache = {}
        self._seen_emails = None
        self._seen_phones = None

    def _authenticate(self):
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(self.credentials_path, scope)
        return gspread.authorize(creds)

    def _get_or_create_spreadsheet(self):
        try:
            return self.client.open(self.spreadsheet_name)
        except gspread.SpreadsheetNotFound:
            return self.client.create(self.spreadsheet_name)

    # Tab naming is inherited from BaseLeadManager so both output types produce
    # identically-named tabs. Google Sheets would allow 100-character titles,
    # but matching Excel's 31-character limit keeps the two interchangeable.

    def _get_or_create_worksheet(self, sheet_key):
        try:
            ws = self.spreadsheet.worksheet(sheet_key)
            return ws
        except gspread.WorksheetNotFound:
            ws = self.spreadsheet.add_worksheet(title=sheet_key, rows="1000", cols=str(len(self.headers)))
            ws.append_row(self.headers)
            # Apply basic formatting to the header row. The range is derived
            # from the header count so adding a column can't leave the last one
            # unformatted (it was hardcoded to A1:Q1 for 17 columns).
            last_col = chr(ord("A") + len(self.headers) - 1) if len(self.headers) <= 26 else "Z"
            ws.format(f"A1:{last_col}1", {
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "backgroundColor": {"red": 0.122, "green": 0.306, "blue": 0.471},
                "horizontalAlignment": "CENTER"
            })
            try:
                ws.freeze(rows=1)
                ws.update_tab_color(self.TAB_COLORS_RGB.get(
                    self._source_of_sheet(sheet_key), self.DEFAULT_TAB_COLOR_RGB))
            except Exception:
                pass  # cosmetic only — older gspread versions may not support these
            # Initialize empty cache for this new sheet
            self._npi_cache[sheet_key] = set()
            return ws

    def _get_npi_cache(self, sheet_key):
        """Lazily load and cache all NPIs for a given source's worksheet."""
        if sheet_key not in self._npi_cache:
            try:
                ws = self.spreadsheet.worksheet(sheet_key)
                npi_list = ws.col_values(1)[1:]  # Skip header row
                self._npi_cache[sheet_key] = set(npi_list)
            except gspread.WorksheetNotFound:
                self._npi_cache[sheet_key] = set()
        return self._npi_cache[sheet_key]

    def _ensure_global_contact_caches(self):
        """Lazily scans every tab once for existing Email/Phone values, so
        add_leads_batch can reject a new lead whose email or phone already
        exists anywhere in the spreadsheet, not just within its own tab."""
        if self._seen_emails is not None:
            return
        self._seen_emails = set()
        self._seen_phones = set()
        for ws in self.spreadsheet.worksheets():
            for r in ws.get_all_records():
                email_key = self._normalize_email_key(r.get("Email"))
                if email_key:
                    self._seen_emails.add(email_key)
                phone_key = self._normalize_phone_key(r.get("Phone"))
                if phone_key:
                    self._seen_phones.add(phone_key)

    def is_duplicate(self, npi, sheet_key):
        """Check duplicate using in-memory cache to avoid repeated API calls."""
        cache = self._get_npi_cache(sheet_key)
        return str(npi) in cache

    def add_leads_batch(self, leads):
        """Add multiple leads to Google Sheets using batch writes, one tab per
        Source. A lead is skipped as a duplicate if its NPI already exists in
        that tab, OR if its Email or Phone already exists anywhere in the
        spreadsheet — every email and phone number stays unique.
        Returns a dict of {source: count_added} for leads actually inserted."""
        self._ensure_global_contact_caches()

        leads_by_source = {}
        for lead_data in leads:
            sheet_key = self._sheet_key_for(lead_data)
            leads_by_source.setdefault(sheet_key, []).append(lead_data)

        counts_by_source = {}
        for sheet_key, source_leads in leads_by_source.items():
            # Checking the NPI cache doesn't require the tab to exist yet
            # (falls back to an empty set) — defer creating it until we know
            # there's actually something to insert, so a source whose only
            # candidate leads turn out to be duplicates doesn't leave an
            # empty tab behind.
            npi_cache = self._get_npi_cache(sheet_key)

            rows_to_append = []
            new_npis = []
            for lead_data in source_leads:
                npi = str(lead_data.get("NPI Number"))
                email_key = self._normalize_email_key(lead_data.get("Email"))
                phone_key = self._normalize_phone_key(lead_data.get("Phone"))

                if npi in npi_cache or npi in new_npis:
                    continue
                if email_key and email_key in self._seen_emails:
                    continue
                if phone_key and phone_key in self._seen_phones:
                    continue

                row_data = [lead_data.get(h, "") for h in self.headers]
                rows_to_append.append(row_data)
                new_npis.append(npi)
                if email_key:
                    self._seen_emails.add(email_key)
                if phone_key:
                    self._seen_phones.add(phone_key)

            if rows_to_append:
                # _get_or_create_worksheet resets the NPI cache entry when it
                # creates a brand-new tab, so re-fetch it afterward rather than
                # mutating the (possibly now-stale) `npi_cache` reference above.
                ws = self._get_or_create_worksheet(sheet_key)
                ws.append_rows(rows_to_append, value_input_option="USER_ENTERED")
                self._get_npi_cache(sheet_key).update(new_npis)
                counts_by_source[sheet_key] = len(rows_to_append)

        return counts_by_source

    def get_stats(self):
        """Compute total leads, email/website hit counts, and breakdowns by
        source (one per tab), by state, and by specialty (both read from their
        respective columns across all tabs, independent of tab layout)."""
        stats = {"total": 0, "with_email": 0, "with_website": 0,
                  "by_state": {}, "by_specialty": {}, "by_source": {}}
        for ws in self.spreadsheet.worksheets():
            records = ws.get_all_records()
            sheet_total = len(records)
            if not sheet_total:
                continue
            sheet_email = sum(1 for r in records if r.get("Email"))
            sheet_website = sum(1 for r in records if r.get("Website"))
            stats["by_source"][ws.title] = {"total": sheet_total, "with_email": sheet_email}
            stats["total"] += sheet_total
            stats["with_email"] += sheet_email
            stats["with_website"] += sheet_website
            for r in records:
                specialty = r.get("Specialty") or "Unknown"
                stats["by_specialty"][specialty] = stats["by_specialty"].get(specialty, 0) + 1
                state = r.get("State") or "Unknown"
                state_bucket = stats["by_state"].setdefault(state, {"total": 0, "with_email": 0})
                state_bucket["total"] += 1
                if r.get("Email"):
                    state_bucket["with_email"] += 1
        return stats
