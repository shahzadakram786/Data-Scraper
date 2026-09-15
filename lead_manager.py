"""Export the lead database to a spreadsheet — Excel or Google Sheets.

These used to *be* the storage layer, which meant every batch of 50 leads
reloaded and rewrote the entire workbook. Storage is now storage.LeadStore, and
these are exporters: they read the database once and render it. A run therefore
writes the spreadsheet once at the end rather than several hundred times, and
an export can be repeated, filtered or re-pointed at a different file without
touching the collected data.

Both exporters produce identical tabs — "<State> - <Source>", colour-coded by
platform — so a workbook and a Google Sheet stay interchangeable.
"""

import os
import shutil
import tempfile
import logging
from datetime import date, datetime

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from storage import (EXPORT_LAYOUT, EXPORT_FIELD, EDITABLE_FIELDS,
                     GENERATED_SHEETS, NUMERIC_HEADERS, LeadStore,
                     normalize_phone_key)
import verification


# Tab colour per source, so tabs are distinguishable at a glance.
TAB_COLORS = {
    "NPI Registry": "1F4E78",
    "Psychology Today": "2E7D32",
    "ZocDoc (NPI Import)": "B45309",
}
DEFAULT_TAB_COLOR = "555555"

# Email Status colouring. The whole point of verification is that you can see,
# without reading anything, which addresses are safe to send to.
STATUS_FILLS = {
    verification.VALID:      "C6EFCE",  # green — confirmed
    verification.ROLE:       "DDEBF7",  # blue  — shared mailbox, deliverable
    verification.ACCEPT_ALL: "FFF2CC",  # amber — unconfirmable
    verification.UNKNOWN:    "F2F2F2",  # grey  — not checked
    verification.RISKY:      "FCE4D6",  # orange— probably someone else's
    verification.DISPOSABLE: "FFC7CE",  # red   — throwaway
    verification.INVALID:    "FFC7CE",  # red   — will bounce
}


class ExcelExporter:
    """Renders the database to a styled .xlsx workbook."""

    HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    HEADER_BORDER = Border(
        left=Side(style="thin", color="16324A"), right=Side(style="thin", color="16324A"),
        top=Side(style="thin", color="16324A"), bottom=Side(style="thin", color="16324A"))

    TITLE_FONT = Font(name="Calibri", bold=True, size=14, color="1F4E78")
    LABEL_FONT = Font(name="Calibri", bold=True, size=10)
    DATA_FONT = Font(name="Calibri", size=10)
    DATA_BORDER = Border(
        left=Side(style="thin", color="D9D9D9"), right=Side(style="thin", color="D9D9D9"),
        top=Side(style="thin", color="D9D9D9"), bottom=Side(style="thin", color="D9D9D9"))
    STRIPE_FILL = PatternFill(start_color="F7F7F7", end_color="F7F7F7", fill_type="solid")

    SUMMARY_SHEET = "Summary"
    # Rendered with thousands separators; these are dollar figures from CMS.
    MONEY_HEADERS = frozenset({"Annual Billing", "Medicare Paid"})
    # Widths that suit the actual content, rather than measuring every cell of
    # every column on every export (which is what made saves crawl).
    COLUMN_WIDTHS = {
        "NPI Number": 13, "Provider First Name": 16, "Provider Last Name": 18,
        "Organization Name": 30, "Phone": 16, "Email": 30, "Address": 30,
        "City": 16, "State": 7, "Zip": 9, "Specialty": 26, "Website": 32,
        "Outreach Status": 15, "Last Updated": 13, "Source": 20,
        "Phone Source": 34, "All Emails Found": 34, "Role": 16,
        "LinkedIn": 26, "Facebook": 26, "Instagram": 26, "Other Social": 26,
        "Email Status": 13, "Email Confidence": 10, "Lead Score": 10,
        "First Seen": 12, "Practice Size": 12, "Annual Billing": 15,
        "Medicare Paid": 14, "Patient Volume": 13, "Phone Verified": 32,
    }
    DEFAULT_WIDTH = 18
    # Above this many rows on a sheet, per-cell styling is skipped — see
    # _write_sheet. Chosen so a normal working file stays fully formatted while
    # a bulk state pull still exports in seconds.
    MAX_STYLED_ROWS = 5000

    def __init__(self, filename="RCM_Provider_Leads.xlsx", logger=None):
        self.filename = filename
        self.logger = logger or logging.getLogger(__name__)

    def export(self, store, only_sheets=None, min_score=None,
               email_statuses=None, progress=None, call_list=100,
               call_list_icp=False):
        """Write the whole database (or a filtered slice) to the workbook.

        Returns the number of rows written. The file is replaced atomically, so
        an interrupted export leaves the previous workbook untouched rather
        than a half-written one.

        call_list adds a ranked "Call List" tab of that many leads at the front
        — the sheet a person actually works from. Pass 0 to leave it out.

        Anything typed into the existing workbook's Outreach Status, Comments,
        Date and unnamed columns (and the Call List's Outcome and Notes) is
        saved to the database first, so the rewrite puts it straight back.
        """
        self._absorb_edits(store)
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)  # drop the default empty sheet

        sheet_names = only_sheets or store.sheet_names()
        written = 0
        per_sheet = {}

        for sheet_name in sheet_names:
            leads = list(store.iter_leads(
                sheet_key=sheet_name, min_score=min_score,
                email_statuses=email_statuses))
            if not leads:
                continue
            worksheet = workbook.create_sheet(sheet_name)
            self._write_sheet(worksheet, leads)
            per_sheet[sheet_name] = len(leads)
            written += len(leads)
            if progress:
                progress(sheet_name, len(leads))

        if not written:
            self.logger.warning(
                "Nothing matched the export filters, so " +
                os.path.basename(self.filename) + " was left unchanged.")
            return 0

        if call_list:
            self._write_call_list(workbook, store, limit=call_list,
                                  icp_only=call_list_icp)
        self._write_summary(workbook, store, per_sheet)
        self._save(workbook)
        self.logger.info(
            "Exported " + str(written) + " leads across " + str(len(per_sheet)) +
            " tabs to " + os.path.basename(self.filename) + ".")
        return written

    # ------------------------------------------------------------ internals

    # Widths for the columns the working-sheet layout adds.
    LAYOUT_WIDTHS = {"Comments": 34, "": 6, "Date": 12, "Gender": 9,
                     "NPI Phone": 16}

    def _write_sheet(self, worksheet, leads):
        """One source tab, in EXPORT_LAYOUT order — the working sheet's own
        column order, so rows paste straight into it."""
        for index, header in enumerate(EXPORT_LAYOUT, 1):
            cell = worksheet.cell(row=1, column=index, value=header or None)
            cell.font = self.HEADER_FONT
            cell.fill = self.HEADER_FILL
            cell.border = self.HEADER_BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center")
            worksheet.column_dimensions[get_column_letter(index)].width = (
                self.COLUMN_WIDTHS.get(header)
                or self.LAYOUT_WIDTHS.get(header, self.DEFAULT_WIDTH))

        worksheet.row_dimensions[1].height = 20
        worksheet.freeze_panes = "A2"
        worksheet.sheet_properties.tabColor = TAB_COLORS.get(
            LeadStore.source_of_sheet(worksheet.title), DEFAULT_TAB_COLOR)

        status_column = EXPORT_LAYOUT.index("Email Status") + 1
        email_column = EXPORT_LAYOUT.index("Email") + 1
        score_column = EXPORT_LAYOUT.index("Lead Score") + 1

        # Styling every cell costs three attribute writes per cell, which at
        # 20,000 leads is over a million operations and roughly 45 seconds. Past
        # a few thousand rows the stripes and borders are not helping anyone
        # read the sheet anyway, so above the threshold only the values are
        # written and the header/filter/freeze do the work.
        decorate = len(leads) <= self.MAX_STYLED_ROWS
        if not decorate:
            self.logger.info(
                "%d rows in %s — writing without per-row styling so the export "
                "stays fast; the header, filter and frozen pane are unaffected.",
                len(leads), worksheet.title)

        fields = [EXPORT_FIELD.get(header, header) for header in EXPORT_LAYOUT]
        for row_number, lead in enumerate(leads, start=2):
            stripe = decorate and (row_number % 2 == 0)
            for index, field in enumerate(fields, 1):
                value = lead.get(field, "")
                # Numbers must be written as numbers, or Excel sorts the column
                # as text and puts 100 before 20 before 9.
                if field in NUMERIC_HEADERS:
                    value = _as_number(value)
                cell = worksheet.cell(row=row_number, column=index, value=value)
                if decorate:
                    cell.font = self.DATA_FONT
                    cell.border = self.DATA_BORDER
                    if stripe:
                        cell.fill = self.STRIPE_FILL
                if field in self.MONEY_HEADERS and value != "":
                    cell.number_format = '#,##0'

            status = str(lead.get("Email Status") or "").strip().lower()
            fill_color = STATUS_FILLS.get(status) if decorate else None
            if fill_color:
                fill = PatternFill(start_color=fill_color, end_color=fill_color,
                                   fill_type="solid")
                worksheet.cell(row=row_number, column=email_column).fill = fill
                worksheet.cell(row=row_number, column=status_column).fill = fill
            worksheet.cell(row=row_number, column=score_column).alignment = \
                Alignment(horizontal="center")

        worksheet.auto_filter.ref = worksheet.dimensions

    # The working sheet: what a caller needs in front of them, in the order
    # they need it, and nothing else. The full record stays on the source tabs.
    CALL_LIST_SHEET = "Call List"
    CALL_LIST_COLUMNS = (
        ("#", 5), ("Name", 26), ("Practice", 30), ("Phone", 16),
        # Only when it is a different number from Phone — a second one to try.
        ("NPI Phone", 16),
        ("Email", 30), ("City", 15), ("State", 7), ("Specialty", 24),
        ("Score", 7), ("Why this lead", 62),
        # For the caller to fill in. Saved to the database before the next
        # export, which writes them back: Outcome is the lead's Outreach Status
        # and Notes its Comments, so the state tab shows the same thing.
        ("Outcome", 14), ("Notes", 34),
        # Which lead each row is, so those edits find their way back.
        ("NPI Number", 13), ("Source Tab", 26),
    )
    CALL_LIST_FIELDS = {"Outcome": "Outreach Status", "Notes": "Comments"}

    @classmethod
    def _call_column(cls, header):
        return [h for h, _w in cls.CALL_LIST_COLUMNS].index(header) + 1
    OUTCOMES = "Not Contacted,Contacted,Replied,Meeting,Won,Lost,Bad Contact"

    def _write_call_list(self, workbook, store, limit=100, icp_only=False):
        """A ranked, self-explaining list to work from, as the first tab.

        Every row carries a plain-English reason, so whoever is dialling knows
        who they are speaking to and what to open with — a score by itself
        tells them nothing. The Outcome column is a dropdown rather than free
        text so the results stay countable later.
        """
        leads = store.best_leads(limit=limit, icp_only=icp_only)
        if not leads:
            return

        worksheet = workbook.create_sheet(self.CALL_LIST_SHEET, 0)
        worksheet.sheet_properties.tabColor = "B45309"

        for index, (header, width) in enumerate(self.CALL_LIST_COLUMNS, 1):
            cell = worksheet.cell(row=1, column=index, value=header)
            cell.font = self.HEADER_FONT
            cell.fill = self.HEADER_FILL
            cell.border = self.HEADER_BORDER
            cell.alignment = Alignment(horizontal="center", vertical="center")
            worksheet.column_dimensions[get_column_letter(index)].width = width

        worksheet.row_dimensions[1].height = 20
        worksheet.freeze_panes = "B2"

        for row_number, lead in enumerate(leads, start=2):
            name = " ".join(filter(None, (lead.get("Provider First Name"),
                                          lead.get("Provider Last Name")))).strip()
            npi_phone = lead.get("NPI Phone", "")
            if normalize_phone_key(npi_phone) == normalize_phone_key(lead.get("Phone")):
                npi_phone = ""
            values = (
                row_number - 1,
                name or lead.get("Organization Name", ""),
                lead.get("Organization Name", "") if name else "",
                lead.get("Phone", ""),
                npi_phone,
                lead.get("Email", ""),
                lead.get("City", ""),
                lead.get("State", ""),
                lead.get("Specialty", ""),
                _as_number(lead.get("Lead Score")),
                lead.get("Why", ""),
                lead.get("Outreach Status") or "Not Contacted",
                lead.get("Comments", ""),
                lead.get("NPI Number", ""),
                lead.get("_sheet_key", ""),
            )
            stripe = (row_number % 2 == 0)
            for index, value in enumerate(values, 1):
                cell = worksheet.cell(row=row_number, column=index, value=value)
                cell.font = self.DATA_FONT
                cell.border = self.DATA_BORDER
                if stripe:
                    cell.fill = self.STRIPE_FILL
            worksheet.cell(row=row_number, column=1).alignment = \
                Alignment(horizontal="center")
            worksheet.cell(row=row_number, column=self._call_column("Score")).alignment = \
                Alignment(horizontal="center")

            # Colour the email cell by how much it can be trusted, same key as
            # the source tabs, so the two read identically.
            fill_color = STATUS_FILLS.get(
                str(lead.get("Email Status") or "").strip().lower())
            if fill_color:
                worksheet.cell(row=row_number, column=self._call_column("Email")).fill = \
                    PatternFill(start_color=fill_color, end_color=fill_color,
                                fill_type="solid")

        outcome_column = get_column_letter(self._call_column("Outcome"))
        validation = DataValidation(type="list", formula1='"' + self.OUTCOMES + '"',
                                    allow_blank=True)
        validation.add("{0}2:{0}{1}".format(outcome_column, len(leads) + 1))
        worksheet.add_data_validation(validation)
        worksheet.auto_filter.ref = "A1:{0}{1}".format(
            get_column_letter(len(self.CALL_LIST_COLUMNS)), len(leads) + 1)

    def _write_summary(self, workbook, store, per_sheet):
        """A front tab that answers "what is in this file?" without scrolling.

        Deliberately the first sheet: whoever opens the workbook should see the
        email-status breakdown before they see 3,000 rows.
        """
        worksheet = workbook.create_sheet(self.SUMMARY_SHEET, 0)
        worksheet.sheet_properties.tabColor = "1F4E78"
        stats = store.get_stats()

        worksheet["A1"] = "RCM Provider Leads"
        worksheet["A1"].font = self.TITLE_FONT
        worksheet.column_dimensions["A"].width = 34
        worksheet.column_dimensions["B"].width = 16
        worksheet.column_dimensions["C"].width = 62

        total = stats["total"] or 1
        rows = [
            ("", ""),
            ("Total leads", stats["total"]),
            ("With an email address", "{0}  ({1:.1f}%)".format(
                stats["with_email"], stats["with_email"] / total * 100)),
            ("With a phone number", "{0}  ({1:.1f}%)".format(
                stats["with_phone"], stats["with_phone"] / total * 100)),
            ("With a website", stats["with_website"]),
            ("Mail-server verified", "{0}  ({1:.1f}%)".format(
                stats["verified_email"], stats["verified_email"] / total * 100)),
            ("Average lead score", stats["avg_score"]),
            ("", ""),
        ]
        row_number = 2
        for label, value in rows:
            if label:
                worksheet.cell(row=row_number, column=1, value=label).font = self.LABEL_FONT
                worksheet.cell(row=row_number, column=2, value=value)
            row_number += 1

        worksheet.cell(row=row_number, column=1,
                       value="Email status").font = self.LABEL_FONT
        worksheet.cell(row=row_number, column=2, value="Leads").font = self.LABEL_FONT
        worksheet.cell(row=row_number, column=3,
                       value="What it means").font = self.LABEL_FONT
        row_number += 1
        for status, count in sorted(stats["by_email_status"].items(),
                                    key=lambda kv: -kv[1]):
            worksheet.cell(row=row_number, column=1, value=status)
            worksheet.cell(row=row_number, column=2, value=count)
            worksheet.cell(row=row_number, column=3,
                           value=verification.STATUS_HELP.get(
                               status, "Not yet checked."))
            fill_color = STATUS_FILLS.get(status)
            if fill_color:
                worksheet.cell(row=row_number, column=1).fill = PatternFill(
                    start_color=fill_color, end_color=fill_color, fill_type="solid")
            row_number += 1

        row_number += 1
        worksheet.cell(row=row_number, column=1, value="Tab").font = self.LABEL_FONT
        worksheet.cell(row=row_number, column=2, value="Leads").font = self.LABEL_FONT
        row_number += 1
        for sheet_name, count in sorted(per_sheet.items()):
            worksheet.cell(row=row_number, column=1, value=sheet_name)
            worksheet.cell(row=row_number, column=2, value=count)
            row_number += 1

    def _absorb_edits(self, store):
        """Save what was typed into the workbook this export is about to replace."""
        if not os.path.exists(self.filename):
            return
        try:
            workbook = openpyxl.load_workbook(self.filename, read_only=True,
                                              data_only=True)
        except Exception as e:
            # Nothing can be read back out of it, but replacing it would still
            # destroy whatever it holds — so a copy is set aside first.
            root, extension = os.path.splitext(self.filename)
            backup = "{0}.unreadable-{1:%Y%m%d-%H%M%S}{2}".format(
                root, datetime.now(), extension)
            try:
                shutil.copy2(self.filename, backup)
                kept = " A copy was kept as " + os.path.basename(backup) + "."
            except OSError:
                kept = ""
            self.logger.warning(
                "Could not read %s to keep the notes typed into it (%s).%s",
                os.path.basename(self.filename), e, kept)
            return

        tabs, call_list = {}, {}
        try:
            for name in workbook.sheetnames:
                rows = workbook[name].iter_rows(values_only=True)
                if name == self.CALL_LIST_SHEET:
                    call_list.update(call_list_edits(rows, self.CALL_LIST_FIELDS))
                elif name not in GENERATED_SHEETS:
                    tabs.update(sheet_edits(name, rows))
        finally:
            workbook.close()

        # The state tab is applied last, so it wins where both were edited.
        changed = store.apply_sheet_edits(call_list, tabs)
        if changed:
            self.logger.info("Kept what was typed into %s for %d leads.",
                             os.path.basename(self.filename), changed)

    def _save(self, workbook):
        """Save via a temp file plus an atomic rename, so a failure partway
        through leaves the existing workbook exactly as it was rather than
        truncated."""
        target_dir = os.path.dirname(os.path.abspath(self.filename)) or "."
        handle, temp_path = tempfile.mkstemp(suffix=".xlsx", dir=target_dir)
        os.close(handle)
        try:
            workbook.save(temp_path)
            os.replace(temp_path, self.filename)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise
        finally:
            workbook.close()


def sheet_edits(sheet_name, rows):
    """{(tab, NPI number): {field: value}} for the editable columns of one tab.

    Columns are found by header, so a tab someone rearranged still reads
    correctly. Only columns the tab actually has are read: a workbook from an
    older version has no Comments column, and a missing column is not an
    emptied one.
    """
    rows = iter(rows)
    headers = _header_row(rows)
    if not headers or "NPI Number" not in headers:
        return {}
    columns = {}
    for header in EXPORT_LAYOUT:
        field = EXPORT_FIELD.get(header, header)
        if field not in EDITABLE_FIELDS:
            continue
        if header:
            if header in headers:
                columns[field] = headers.index(header)
            continue
        # The unnamed column has no header to find, so it is found by its
        # neighbour: the column straight after the one the layout puts before it.
        before = EXPORT_LAYOUT[EXPORT_LAYOUT.index(header) - 1]
        if before in headers:
            spare = headers.index(before) + 1
            if spare < len(headers) and headers[spare] == "":
                columns[field] = spare
    return _collect_edits(rows, headers.index("NPI Number"), lambda row: sheet_name,
                          columns)


def call_list_edits(rows, fields):
    """The same, for the Call List: each row names its own tab and NPI number."""
    rows = iter(rows)
    headers = _header_row(rows)
    if not headers or "NPI Number" not in headers or "Source Tab" not in headers:
        return {}
    tab_at = headers.index("Source Tab")
    columns = {field: headers.index(header)
               for header, field in fields.items() if header in headers}
    return _collect_edits(rows, headers.index("NPI Number"),
                          lambda row: _cell_text(row[tab_at]) if tab_at < len(row) else "",
                          columns)


def _collect_edits(rows, npi_at, tab_of, columns):
    edits = {}
    if not columns:
        return edits
    for row in rows:
        if not row or npi_at >= len(row):
            continue
        npi, tab = _cell_text(row[npi_at]), tab_of(row)
        if not npi or not tab:
            continue
        edits[(tab, npi)] = {field: (_cell_text(row[index]) if index < len(row) else "")
                             for field, index in columns.items()}
    return edits


def _header_row(rows):
    try:
        return [_cell_text(value) for value in next(rows)]
    except StopIteration:
        return None


def _cell_text(value):
    """A spreadsheet cell as the text the database stores. Excel hands back real
    dates and floats for what people type, and 2026-09-15 must stay 2026-09-15
    rather than become "2026-09-15 00:00:00"."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second) == (0, 0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _as_number(value):
    """Return a number Excel will sort numerically, or "" when the field is
    genuinely empty. Most CMS columns are blank for most leads, and a blank
    must stay blank rather than becoming a misleading zero."""
    if value in (None, ""):
        return ""
    try:
        number = float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


class GoogleSheetsExporter:
    """Renders the database to a Google Sheet, one worksheet per tab.

    Each tab is written in a single batch call rather than row by row, so an
    export costs a couple of API calls per tab regardless of how many leads it
    holds — well inside the free quota.
    """

    TAB_COLORS_RGB = {
        "NPI Registry": {"red": 0.122, "green": 0.306, "blue": 0.471},
        "Psychology Today": {"red": 0.180, "green": 0.490, "blue": 0.196},
        "ZocDoc (NPI Import)": {"red": 0.706, "green": 0.325, "blue": 0.035},
    }
    DEFAULT_TAB_COLOR_RGB = {"red": 0.33, "green": 0.33, "blue": 0.33}

    def __init__(self, credentials_path, spreadsheet_name, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.credentials_path = credentials_path
        self.spreadsheet_name = spreadsheet_name
        self.client = self._authenticate()
        self.spreadsheet = self._open_or_create()

    def _authenticate(self):
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
        scope = ["https://spreadsheets.google.com/feeds",
                 "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(
            self.credentials_path, scope)
        return gspread.authorize(creds)

    def _open_or_create(self):
        import gspread
        try:
            return self.client.open(self.spreadsheet_name)
        except gspread.SpreadsheetNotFound:
            return self.client.create(self.spreadsheet_name)

    @property
    def url(self):
        return self.spreadsheet.url

    def export(self, store, only_sheets=None, min_score=None,
               email_statuses=None, progress=None):
        self._absorb_edits(store)
        fields = [EXPORT_FIELD.get(header, header) for header in EXPORT_LAYOUT]
        written = 0
        for sheet_name in (only_sheets or store.sheet_names()):
            leads = list(store.iter_leads(
                sheet_key=sheet_name, min_score=min_score,
                email_statuses=email_statuses))
            if not leads:
                continue
            worksheet = self._worksheet_for(sheet_name, len(leads))
            values = [list(EXPORT_LAYOUT)]
            values.extend([[str(lead.get(f, "")) for f in fields] for lead in leads])
            worksheet.clear()
            worksheet.update(values, "A1", value_input_option="USER_ENTERED")
            written += len(leads)
            if progress:
                progress(sheet_name, len(leads))
        self.logger.info("Exported " + str(written) + " leads to Google Sheets.")
        return written

    def _absorb_edits(self, store):
        """Save what was typed into the Google Sheet before its tabs are cleared.

        Deliberately not wrapped in a try: if the sheet cannot be read, the
        export fails rather than clearing tabs whose notes were never saved.
        """
        known = set(store.sheet_names())
        edits = {}
        for worksheet in self.spreadsheet.worksheets():
            if worksheet.title in known:
                edits.update(sheet_edits(worksheet.title, worksheet.get_all_values()))
        changed = store.apply_sheet_edits(edits)
        if changed:
            self.logger.info("Kept what was typed into the Google Sheet for %d leads.",
                             changed)

    def _worksheet_for(self, sheet_name, row_count):
        import gspread
        needed_rows = max(row_count + 10, 100)
        columns = len(EXPORT_LAYOUT)
        try:
            worksheet = self.spreadsheet.worksheet(sheet_name)
            if worksheet.row_count < needed_rows or worksheet.col_count < columns:
                worksheet.resize(rows=max(needed_rows, worksheet.row_count),
                                 cols=max(columns, worksheet.col_count))
            return worksheet
        except gspread.WorksheetNotFound:
            pass

        worksheet = self.spreadsheet.add_worksheet(
            title=sheet_name, rows=str(needed_rows), cols=str(columns))
        last_column = get_column_letter(columns)
        worksheet.format("A1:" + last_column + "1", {
            "textFormat": {"bold": True,
                           "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            "backgroundColor": {"red": 0.122, "green": 0.306, "blue": 0.471},
            "horizontalAlignment": "CENTER",
        })
        try:  # cosmetic only — older gspread versions lack these
            worksheet.freeze(rows=1)
            worksheet.update_tab_color(self.TAB_COLORS_RGB.get(
                LeadStore.source_of_sheet(sheet_name), self.DEFAULT_TAB_COLOR_RGB))
        except Exception:
            pass
        return worksheet
