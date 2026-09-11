"""SQLite store — the system of record for every lead.

Why this exists: the spreadsheet used to *be* the database. Every flush of 50
leads loaded the whole .xlsx, walked every row of every tab to rebuild the
duplicate-detection sets, appended, restyled and saved it again. That is O(n)
work per flush and O(n^2) over a run, so a big state got slower the longer it
ran; and a crash partway through a save could leave the file unreadable, at
which point the old code deleted it and started over.

Now writes land here — indexed, atomic, microseconds — and the spreadsheet is
generated from this on demand. That single change also buys:

  * resume, because a stopped run knows which listing page it reached;
  * duplicate detection by index instead of a full-file scan;
  * re-export of any slice ("Texas owners with a verified email") without
    re-scraping anything;
  * run history with real outcomes, including failures.

The .xlsx is now an export format rather than storage, which is what it was
always good at.
"""

import os
import re
import json
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone


# Export column order. Everything downstream (Excel tabs, Google Sheets, the
# CSV export) writes rows positionally in this order, so **new columns must be
# appended at the end** — inserting one in the middle would shift every value
# in spreadsheets that already exist under the old header order.
FIELD_COLUMNS = (
    ("NPI Number", "npi"),
    ("Provider First Name", "first_name"),
    ("Provider Last Name", "last_name"),
    ("Organization Name", "org_name"),
    ("Phone", "phone"),
    ("Email", "email"),
    ("Address", "address"),
    ("City", "city"),
    ("State", "state"),
    ("Zip", "zip"),
    ("Specialty", "specialty"),
    ("Website", "website"),
    ("Outreach Status", "outreach_status"),
    ("Last Updated", "last_updated"),
    ("Source", "source"),
    ("Phone Source", "phone_source"),
    ("All Emails Found", "all_emails"),
    ("Role", "role"),
    ("LinkedIn", "linkedin"),
    ("Facebook", "facebook"),
    ("Instagram", "instagram"),
    ("Other Social", "other_social"),
    # --- added with the verification/scoring work ---
    ("Email Status", "email_status"),
    ("Email Confidence", "email_confidence"),
    ("Lead Score", "lead_score"),
    ("First Seen", "first_seen"),
    # --- added with the CMS enrichment pass ---
    # Blank for most Psychology Today therapists: CMS only covers
    # Medicare-enrolled providers and suppresses anyone under 11 beneficiaries.
    ("Practice Size", "practice_size"),
    ("Annual Billing", "annual_billing"),
    ("Medicare Paid", "medicare_paid"),
    ("Patient Volume", "patient_volume"),
    ("Phone Verified", "phone_verified"),
)

# Numeric columns, so the exporter writes them as numbers rather than text —
# otherwise Excel sorts 100 before 20 before 9.
NUMERIC_HEADERS = frozenset({
    "Email Confidence", "Lead Score", "Practice Size", "Annual Billing",
    "Medicare Paid", "Patient Volume",
})

# What the Phone Verified column can say. A federal enrollment record is the
# strongest corroboration available for free: the provider filed that number
# with Medicare themselves.
PHONE_CONFIRMED = "Confirmed (federal enrollment record)"
PHONE_DIFFERS = "Differs from federal enrollment record"
PHONE_FROM_CMS = "From federal enrollment record"
# Written when the CMS pass ran and found nothing. Without it, a provider CMS
# does not cover would look un-enriched forever and be re-queried on every run —
# and for Psychology Today therapists that is most of them.
PHONE_NO_RECORD = "No federal enrollment record"

HEADERS = [header for header, _ in FIELD_COLUMNS]
HEADER_TO_COLUMN = dict(FIELD_COLUMNS)
COLUMN_TO_HEADER = {col: header for header, col in FIELD_COLUMNS}

# Run outcomes. "empty" is deliberately distinct from "ok": a run that finished
# without error but collected nothing is the signature of a scraper that has
# quietly broken, and it must not be reported as a success.
RUN_OK = "ok"
RUN_EMPTY = "empty"
RUN_STOPPED = "stopped"
RUN_FAILED = "failed"


def normalize_email_key(email):
    e = str(email or "").strip().lower()
    return e or None


def normalize_phone_key(phone):
    """(423) 555-1234, 423-555-1234 and 4235551234 all compare equal."""
    digits = re.sub(r'\D', '', str(phone or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits or None


def compute_lead_score(lead):
    """How useful this lead is to actually contact, 0-100.

    A list sorted by this puts the reachable decision-makers at the top, which
    is the whole job. The weights say: a confirmed mailbox beats everything,
    a direct line beats a front-desk line, and knowing someone owns the
    practice is worth more than knowing their website exists.
    """
    score = 0

    status = str(lead.get("Email Status") or "").strip().lower()
    if lead.get("Email"):
        score += {"valid": 45, "role": 35, "accept-all": 28,
                  "unknown": 22, "risky": 8}.get(status, 25)
        if status in ("invalid", "disposable"):
            score = 0  # a bouncing address is worse than no address

    phone_source = str(lead.get("Phone Source") or "").lower()
    if lead.get("Phone"):
        if "personal/direct" in phone_source:
            score += 25
        elif "shared practice line" in phone_source:
            score += 8
        elif "toll-free" in phone_source or "listing" in phone_source:
            score += 5
        else:
            score += 15

    role = str(lead.get("Role") or "").strip()
    if role in ("Owner", "Co-Owner", "Founder", "Co-Founder", "CEO",
                "President", "Principal", "Managing Partner"):
        score += 20
    elif role:
        score += 8  # Clinical/Executive Director: senior, not the owner

    if lead.get("Website"):
        score += 5
    if any(lead.get(k) for k in ("LinkedIn", "Facebook", "Instagram")):
        score += 3
    if str(lead.get("NPI Number") or "").isdigit():
        score += 2  # identity confirmed against the federal registry

    # A phone number the provider filed with Medicare themselves is worth more
    # than one scraped off a page, however well that page was labelled.
    if str(lead.get("Phone Verified") or "").startswith(("Confirmed", "From")):
        score += 5

    # CMS signals are bonuses rather than part of the base, so that a lead with
    # no Medicare record (most therapists) is not pushed down the list for it —
    # missing data here means "not a Medicare biller", not "a worse lead".
    billing = _to_number(lead.get("Annual Billing"))
    if billing:
        # Banded rather than linear: the useful distinction is the order of
        # magnitude of a practice's billing, not its exact figure.
        for threshold, points in ((1_000_000, 8), (250_000, 6), (50_000, 4), (0, 2)):
            if billing >= threshold:
                score += points
                break

    size = _to_number(lead.get("Practice Size"))
    if size:
        # A larger group is a bigger RCM contract, but also a longer sale, so
        # this is deliberately a smaller weight than billing volume.
        for threshold, points in ((50, 5), (10, 4), (2, 2), (0, 1)):
            if size >= threshold:
                score += points
                break

    return min(score, 100)


# Tabs the exporter writes that are a view of the data rather than data. Named
# here so the importer can never read its own output back in as leads.
GENERATED_SHEETS = frozenset({"Summary", "Call List"})

OWNERSHIP_ROLES = frozenset({
    "Owner", "Co-Owner", "Founder", "Co-Founder", "CEO",
    "President", "Principal", "Managing Partner",
})

# Above this many clinicians a practice almost certainly has its own billing
# department, and is a long enterprise sale rather than an RCM prospect.
ICP_MAX_PRACTICE_SIZE = 25


def explain_lead(lead):
    """One line saying why this lead ranks where it does.

    A score on its own tells a caller nothing actionable. "Owner · solo
    practice · $230k Medicare billing · direct line" tells them who they are
    about to speak to and what to open with. Only facts actually collected are
    listed — nothing is inferred to pad the line out.
    """
    parts = []

    role = str(lead.get("Role") or "").strip()
    if role in OWNERSHIP_ROLES:
        parts.append(role)
    elif role:
        parts.append(role + " (not the owner)")

    size = _to_number(lead.get("Practice Size"))
    if size == 1:
        parts.append("solo practice")
    elif size:
        size = int(size)
        if size <= ICP_MAX_PRACTICE_SIZE:
            parts.append("small practice ({0} clinicians)".format(size))
        else:
            parts.append("large group ({0} clinicians)".format(size))

    billing = _to_number(lead.get("Annual Billing"))
    if billing:
        parts.append("${0} Medicare billing".format(_short_money(billing)))
        # Revenue per clinician is the sharpest RCM signal there is: a lot of
        # money moving through very few people means no billing department.
        if size and size > 0:
            per_head = billing / size
            if per_head >= 100_000 and size <= ICP_MAX_PRACTICE_SIZE:
                parts.append("${0}/clinician".format(_short_money(per_head)))

    phone_source = str(lead.get("Phone Source") or "")
    if "personal/direct" in phone_source:
        parts.append("direct line")
    elif str(lead.get("Phone Verified") or "").startswith(("Confirmed", "From")):
        parts.append("phone confirmed federally")
    elif "shared practice line" in phone_source:
        parts.append("shared front desk")
    elif "listing" in phone_source:
        parts.append("directory number only")

    status = str(lead.get("Email Status") or "").strip().lower()
    if not lead.get("Email"):
        # Only worth saying when there is a phone to use instead — on a lead
        # with no contact details at all it reads as a finding rather than the
        # absence of one.
        if lead.get("Phone"):
            parts.append("no email yet — phone only")
    elif status == "valid":
        parts.append("email confirmed")
    elif status == "role":
        parts.append("shared mailbox")
    elif status == "risky":
        parts.append("email may be someone else's")

    return " · ".join(parts) if parts else "contact details only"


def _short_money(amount):
    """230570 -> 231k, 2400000 -> 2.4M. Exact figures belong in the column;
    this is for a one-line summary a caller reads at a glance."""
    amount = float(amount)
    if amount >= 1_000_000:
        return "{0:.1f}M".format(amount / 1_000_000).replace(".0M", "M")
    if amount >= 1_000:
        return "{0:.0f}k".format(amount / 1_000)
    return "{0:.0f}".format(amount)


def is_icp(lead, max_practice_size=ICP_MAX_PRACTICE_SIZE):
    """Whether this is the profile that actually buys revenue cycle management:
    an owner-operated practice small enough not to have its own billing
    department, that you can reach.

    Deliberately does not require billing data — most therapists will never
    have any, and excluding them would throw away a whole source.
    """
    if str(lead.get("Role") or "").strip() not in OWNERSHIP_ROLES:
        return False
    size = _to_number(lead.get("Practice Size"))
    if size and size > max_practice_size:
        return False
    return bool(lead.get("Email") or lead.get("Phone"))


def _to_number(value):
    """Tolerant numeric read: these columns arrive as ints, floats or the
    strings SQLite stores them as."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return None


class LeadStore:
    """Thread-safe SQLite store. One instance per database file.

    Every public method takes and returns leads as plain dicts keyed by the
    spreadsheet header names in HEADERS, so callers that predate this module
    keep working unchanged.
    """

    SCHEMA_VERSION = 1

    # Sheet naming is unchanged from the spreadsheet era so exported tabs keep
    # the same names: "<State> - <Source>", capped at Excel's 31 characters.
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

    SHARED_PHONE_NOTE = " (shared practice line)"

    def __init__(self, path="leads.db", logger=None):
        self.path = path
        self.logger = logger or logging.getLogger(__name__)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._init_schema()

    # ------------------------------------------------------------ plumbing

    def _conn(self):
        """One connection per thread. SQLite objects are not shareable across
        threads, and the worker pools here are genuinely concurrent."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")     # readers never block writers
            conn.execute("PRAGMA synchronous=NORMAL")   # durable enough, much faster
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _init_schema(self):
        columns = ",\n                ".join(
            '"{0}" TEXT'.format(col) for _, col in FIELD_COLUMNS)
        with self._lock:
            conn = self._conn()
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sheet_key TEXT NOT NULL,
                    email_key TEXT,
                    phone_key TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    """ + columns + """
                );

                -- One provider per source tab. This is the identity rule the
                -- spreadsheet enforced by scanning every row; here it is an index.
                CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_identity
                    ON leads(sheet_key, npi);

                -- An email address belongs to exactly one lead, file-wide.
                CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email
                    ON leads(email_key) WHERE email_key IS NOT NULL;

                -- A repeated phone number is NOT a duplicate (colleagues share a
                -- front-desk line), so this index is for lookups only.
                CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone_key);
                CREATE INDEX IF NOT EXISTS idx_leads_sheet ON leads(sheet_key);
                CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state);
                CREATE INDEX IF NOT EXISTS idx_leads_source ON leads(source);
                CREATE INDEX IF NOT EXISTS idx_leads_email_status ON leads(email_status);

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    action TEXT NOT NULL,
                    target TEXT,
                    status TEXT,
                    added INTEGER DEFAULT 0,
                    total INTEGER DEFAULT 0,
                    with_email INTEGER DEFAULT 0,
                    hit_rate REAL DEFAULT 0,
                    message TEXT
                );

                -- Where a stopped or crashed run had got to, so it can pick up
                -- instead of starting the state again from page 1.
                CREATE TABLE IF NOT EXISTS checkpoints (
                    job_key TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- One row per message actually sent. This is the record that
                -- proves who was contacted and when, and it is what stops the
                -- same person being emailed twice by two different campaigns.
                CREATE TABLE IF NOT EXISTS outreach (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    campaign TEXT,
                    subject TEXT,
                    sent_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    FOREIGN KEY (lead_id) REFERENCES leads(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_outreach_lead ON outreach(lead_id);
                CREATE INDEX IF NOT EXISTS idx_outreach_email ON outreach(email);
                CREATE INDEX IF NOT EXISTS idx_outreach_sent ON outreach(sent_at);

                -- Anyone who asked not to be contacted, plus every address that
                -- hard bounced. Checked before every single send; an entry here
                -- is permanent unless removed by hand.
                CREATE TABLE IF NOT EXISTS suppressions (
                    value TEXT PRIMARY KEY,   -- an address, or "@domain.com"
                    reason TEXT,
                    added_at TEXT NOT NULL
                );
            """)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),))
            conn.commit()

    # ------------------------------------------------------- sheet naming

    @classmethod
    def sheet_key_for(cls, lead):
        """Leads are filed one tab per state and source — "Texas - Psychology
        Today", "Ohio - NPI Registry" — so each tab answers "what did this
        platform give me in this state"."""
        source = cls._clean_sheet_part(lead.get("Source") or "Unknown") or "Unknown"
        abbr = cls._clean_sheet_part(lead.get("State") or "").upper()
        if not abbr:
            return source[:cls.MAX_SHEET_NAME]
        full = cls._clean_sheet_part(cls.STATE_NAMES.get(abbr, abbr))
        for state_part in (full, abbr):
            name = state_part + " - " + source
            if len(name) <= cls.MAX_SHEET_NAME:
                return name
        return (abbr + " - " + source)[:cls.MAX_SHEET_NAME].rstrip(" -")

    @staticmethod
    def _clean_sheet_part(value):
        return re.sub(r'[\\/*?:\[\]]', '', str(value)).strip()

    @staticmethod
    def source_of_sheet(sheet_name):
        return sheet_name.split(" - ", 1)[-1] if " - " in sheet_name else sheet_name

    # -------------------------------------------------------------- writes

    def add_leads_batch(self, leads):
        """Insert a batch, skipping duplicates. Returns {sheet_key: inserted}.

        A lead is a duplicate only when it is the *same lead*: its NPI already
        exists in that tab, or its email address already exists anywhere. A
        repeated phone number is kept — several clinicians at one group
        practice genuinely publish the same front-desk line, and rejecting on
        phone silently discarded every colleague after the first. Those are
        flagged in Phone Source instead.
        """
        if not leads:
            return {}

        now = _utcnow()
        counts = {}
        with self._lock:
            conn = self._conn()
            for lead in leads:
                lead = dict(lead)
                sheet_key = self.sheet_key_for(lead)
                email_key = normalize_email_key(lead.get("Email"))
                phone_key = normalize_phone_key(lead.get("Phone"))

                if phone_key and self._phone_seen(conn, phone_key):
                    source = str(lead.get("Phone Source") or "").strip()
                    if self.SHARED_PHONE_NOTE.strip() not in source:
                        lead["Phone Source"] = (source + self.SHARED_PHONE_NOTE).strip()

                lead.setdefault("First Seen", now[:10])
                lead["Lead Score"] = compute_lead_score(lead)

                db_columns = ["sheet_key", "email_key", "phone_key",
                              "created_at", "updated_at"]
                values = [sheet_key, email_key, phone_key, now, now]
                for header, column in FIELD_COLUMNS:
                    db_columns.append(column)
                    values.append(_as_text(lead.get(header)))

                sql = ("INSERT OR IGNORE INTO leads (" +
                       ", ".join('"{0}"'.format(c) for c in db_columns) +
                       ") VALUES (" + ", ".join("?" * len(values)) + ")")
                cursor = conn.execute(sql, values)
                if cursor.rowcount:
                    counts[sheet_key] = counts.get(sheet_key, 0) + 1
            conn.commit()
        return counts

    @staticmethod
    def _phone_seen(conn, phone_key):
        row = conn.execute(
            "SELECT 1 FROM leads WHERE phone_key = ? LIMIT 1", (phone_key,)).fetchone()
        return row is not None

    def update_lead(self, lead_id, updates):
        """Apply {header: value} changes to one lead and rescore it.

        Used by the email-finding and verification passes. An email that would
        collide with another lead's is rejected here rather than written and
        deduplicated later — the unique index would refuse it anyway.
        """
        if not updates:
            return False
        with self._lock:
            conn = self._conn()
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            if row is None:
                return False

            merged = self.row_to_lead(row)
            merged.update(updates)

            set_parts, values = [], []
            for header, value in updates.items():
                column = HEADER_TO_COLUMN.get(header)
                if column is None:
                    continue
                set_parts.append('"{0}" = ?'.format(column))
                values.append(_as_text(value))

            if "Email" in updates:
                email_key = normalize_email_key(updates.get("Email"))
                if email_key and self._email_taken(conn, email_key, lead_id):
                    self.logger.info(
                        str(updates.get("Email")) + " already belongs to another "
                        "lead — leaving this one blank rather than duplicating it.")
                    return False
                set_parts.append("email_key = ?")
                values.append(email_key)

            set_parts.append('"lead_score" = ?')
            values.append(_as_text(compute_lead_score(merged)))
            set_parts.append("updated_at = ?")
            values.append(_utcnow())

            values.append(lead_id)
            conn.execute("UPDATE leads SET " + ", ".join(set_parts) + " WHERE id = ?",
                         values)
            conn.commit()
        return True

    @staticmethod
    def _email_taken(conn, email_key, exclude_id):
        row = conn.execute(
            "SELECT 1 FROM leads WHERE email_key = ? AND id != ? LIMIT 1",
            (email_key, exclude_id)).fetchone()
        return row is not None

    def email_exists(self, email):
        key = normalize_email_key(email)
        if not key:
            return False
        with self._lock:
            row = self._conn().execute(
                "SELECT 1 FROM leads WHERE email_key = ? LIMIT 1", (key,)).fetchone()
        return row is not None

    # --------------------------------------------------------------- reads

    @staticmethod
    def row_to_lead(row):
        """sqlite3.Row -> the header-keyed dict the rest of the app speaks."""
        lead = {header: (row[column] or "") for header, column in FIELD_COLUMNS}
        lead["_id"] = row["id"]
        lead["_sheet_key"] = row["sheet_key"]
        return lead

    # Ties on score are broken by billing volume, so that among equally
    # contactable leads the bigger practice comes first. Score is stored as
    # text, so both sides need casting or "9" sorts above "80".
    DEFAULT_ORDER = ("CAST(COALESCE(NULLIF(lead_score, ''), '0') AS INTEGER) DESC, "
                     "CAST(COALESCE(NULLIF(annual_billing, ''), '0') AS REAL) DESC, "
                     "id ASC")

    # ORDER BY cannot be a bound parameter, so it is interpolated — which makes
    # it the one place a caller could inject SQL. Only these named orderings are
    # accepted; anything else falls back to the default rather than reaching the
    # database.
    ORDERINGS = {
        "best": DEFAULT_ORDER,
        "newest": "id DESC",
        "oldest": "id ASC",
        "score": "CAST(COALESCE(NULLIF(lead_score, ''), '0') AS INTEGER) DESC",
        "billing": "CAST(COALESCE(NULLIF(annual_billing, ''), '0') AS REAL) DESC",
        "name": "last_name ASC, first_name ASC",
        "state": "state ASC, last_name ASC",
    }

    def iter_leads(self, sheet_key=None, state=None, source=None,
                   missing_email=False, has_email=False, unverified=False,
                   needs_cms=False, min_score=None, email_statuses=None,
                   order_by=None, limit=None):
        """Yields leads matching the filters, as header-keyed dicts.

        order_by takes a key from ORDERINGS ("best", "newest", "name", ...),
        not raw SQL.
        """
        if order_by and order_by not in self.ORDERINGS:
            self.logger.warning(
                "Unknown ordering %r — using the default. Valid options: %s",
                order_by, ", ".join(sorted(self.ORDERINGS)))
            order_by = None
        order_clause = self.ORDERINGS.get(order_by or "best", self.DEFAULT_ORDER)
        where, params = [], []
        if sheet_key:
            where.append("sheet_key = ?")
            params.append(sheet_key)
        if state:
            where.append("state = ?")
            params.append(state)
        if source:
            where.append("source = ?")
            params.append(source)
        if missing_email:
            where.append("(email IS NULL OR email = '')")
        if has_email:
            where.append("(email IS NOT NULL AND email != '')")
        if unverified:
            where.append("(email IS NOT NULL AND email != '') "
                         "AND (email_status IS NULL OR email_status = '')")
        if needs_cms:
            # Only real NPI numbers can be looked up: Psychology Today leads
            # with no registry match carry a "PT-<id>" placeholder. phone_verified
            # is the marker that the pass has run, since every other CMS column
            # is legitimately empty for a provider CMS does not cover.
            where.append("(phone_verified IS NULL OR phone_verified = '')")
            where.append("npi GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'")
        if min_score is not None:
            where.append("CAST(COALESCE(NULLIF(lead_score, ''), '0') AS INTEGER) >= ?")
            params.append(int(min_score))
        if email_statuses:
            placeholders = ", ".join("?" * len(email_statuses))
            where.append("email_status IN (" + placeholders + ")")
            params.extend(email_statuses)

        sql = "SELECT * FROM leads"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY " + order_clause
        if limit:
            sql += " LIMIT " + str(int(limit))

        with self._lock:
            rows = self._conn().execute(sql, params).fetchall()
        for row in rows:
            yield self.row_to_lead(row)

    def iter_best_leads(self, owners_only=False, require_email=False,
                        icp_only=False, max_practice_size=ICP_MAX_PRACTICE_SIZE,
                        states=None, min_score=None):
        """Lazily yield workable leads, best first, each with a `Why` line.

        Lazy because callers usually want the top 25 of several thousand:
        building the whole list and then slicing it means running explain_lead
        on every lead in the database to show a page of them.

        Filtering happens here rather than in SQL for `icp_only`, because the
        ICP test spans several columns and a couple of derived values, and the
        rule is worth keeping readable in one place.
        """
        for lead in self.iter_leads(min_score=min_score, has_email=require_email):
            if states and lead.get("State") not in states:
                continue
            if owners_only and str(lead.get("Role") or "").strip() not in OWNERSHIP_ROLES:
                continue
            if icp_only and not is_icp(lead, max_practice_size):
                continue
            if not (lead.get("Email") or lead.get("Phone")):
                continue  # nothing to contact them with
            lead["Why"] = explain_lead(lead)
            yield lead

    def best_leads(self, limit=100, **filters):
        """The leads worth working, best first. limit=0 means all of them.

        This is the answer to "who do I call today". The ordering is the lead
        score, tie-broken by billing volume, so the most reachable
        decision-makers at the biggest practices come first.
        """
        chosen = []
        for lead in self.iter_best_leads(**filters):
            chosen.append(lead)
            if limit and len(chosen) >= limit:
                break
        return chosen

    def contacted_emails(self):
        """Every address a message has successfully gone to, as one set.

        Loaded once per campaign instead of querying per recipient: the send
        path was doing two round trips for every lead it considered, which on a
        20,000-lead database is 40,000 queries to pick 25 people.
        """
        with self._lock:
            rows = self._conn().execute(
                "SELECT DISTINCT email FROM outreach WHERE status = 'sent'").fetchall()
        return {r["email"] for r in rows if r["email"]}

    def contacted_lead_ids(self):
        with self._lock:
            rows = self._conn().execute(
                "SELECT DISTINCT lead_id FROM outreach WHERE status = 'sent'").fetchall()
        return {r["lead_id"] for r in rows}

    def suppressed_values(self):
        with self._lock:
            rows = self._conn().execute("SELECT value FROM suppressions").fetchall()
        return {r["value"] for r in rows}

    def sheet_names(self):
        with self._lock:
            rows = self._conn().execute(
                "SELECT DISTINCT sheet_key FROM leads ORDER BY sheet_key").fetchall()
        return [r["sheet_key"] for r in rows]

    def count(self, **filters):
        return sum(1 for _ in self.iter_leads(**filters))

    def get_stats(self):
        """Totals and breakdowns, computed by SQL rather than by walking rows.

        Shape matches what the old spreadsheet managers returned, plus
        `by_email_status` and `avg_score` for the verification work.
        """
        stats = {"total": 0, "with_email": 0, "with_website": 0, "with_phone": 0,
                 "verified_email": 0, "with_billing": 0, "phone_confirmed": 0,
                 "total_billing": 0.0, "avg_score": 0.0,
                 "by_state": {}, "by_specialty": {}, "by_source": {},
                 "by_email_status": {}, "by_score_band": {}}
        with self._lock:
            conn = self._conn()
            row = conn.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN email    IS NOT NULL AND email    != '' THEN 1 ELSE 0 END) AS with_email,
                       SUM(CASE WHEN website  IS NOT NULL AND website  != '' THEN 1 ELSE 0 END) AS with_website,
                       SUM(CASE WHEN phone    IS NOT NULL AND phone    != '' THEN 1 ELSE 0 END) AS with_phone,
                       SUM(CASE WHEN email_status IN ('valid','role') THEN 1 ELSE 0 END) AS verified_email,
                       SUM(CASE WHEN annual_billing IS NOT NULL AND annual_billing != '' THEN 1 ELSE 0 END) AS with_billing,
                       SUM(CASE WHEN phone_verified LIKE 'Confirmed%' THEN 1 ELSE 0 END) AS phone_confirmed,
                       SUM(CAST(COALESCE(NULLIF(annual_billing, ''), '0') AS REAL)) AS total_billing,
                       AVG(CAST(COALESCE(NULLIF(lead_score, ''), '0') AS INTEGER)) AS avg_score
                FROM leads
            """).fetchone()
            if row and row["total"]:
                stats["total"] = row["total"]
                stats["with_email"] = row["with_email"] or 0
                stats["with_website"] = row["with_website"] or 0
                stats["with_phone"] = row["with_phone"] or 0
                stats["verified_email"] = row["verified_email"] or 0
                stats["with_billing"] = row["with_billing"] or 0
                stats["phone_confirmed"] = row["phone_confirmed"] or 0
                stats["total_billing"] = round(row["total_billing"] or 0.0, 2)
                stats["avg_score"] = round(row["avg_score"] or 0.0, 1)

            for r in conn.execute("""
                SELECT sheet_key,
                       COUNT(*) AS total,
                       SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) AS with_email
                FROM leads GROUP BY sheet_key ORDER BY sheet_key
            """):
                stats["by_source"][r["sheet_key"]] = {
                    "total": r["total"], "with_email": r["with_email"] or 0}

            for r in conn.execute("""
                SELECT COALESCE(NULLIF(state, ''), 'Unknown') AS state,
                       COUNT(*) AS total,
                       SUM(CASE WHEN email IS NOT NULL AND email != '' THEN 1 ELSE 0 END) AS with_email
                FROM leads GROUP BY state
            """):
                stats["by_state"][r["state"]] = {
                    "total": r["total"], "with_email": r["with_email"] or 0}

            for r in conn.execute("""
                SELECT COALESCE(NULLIF(specialty, ''), 'Unknown') AS specialty,
                       COUNT(*) AS total
                FROM leads GROUP BY specialty
            """):
                stats["by_specialty"][r["specialty"]] = r["total"]

            for r in conn.execute("""
                SELECT COALESCE(NULLIF(email_status, ''), 'unchecked') AS status,
                       COUNT(*) AS total
                FROM leads
                WHERE email IS NOT NULL AND email != ''
                GROUP BY status
            """):
                stats["by_email_status"][r["status"]] = r["total"]

            # Banded in SQL rather than by walking every lead in Python. The
            # window redraws this on every screen change, and a full scan there
            # costs most of a second once the database passes 20,000 leads.
            row = conn.execute("""
                SELECT
                  SUM(CASE WHEN s >= 70 THEN 1 ELSE 0 END) AS high,
                  SUM(CASE WHEN s >= 50 AND s < 70 THEN 1 ELSE 0 END) AS mid,
                  SUM(CASE WHEN s >= 30 AND s < 50 THEN 1 ELSE 0 END) AS low,
                  SUM(CASE WHEN s < 30 THEN 1 ELSE 0 END) AS poor
                FROM (SELECT CAST(COALESCE(NULLIF(lead_score, ''), '0')
                             AS INTEGER) AS s FROM leads)
            """).fetchone()
            stats["by_score_band"] = {
                "70+": (row["high"] if row else 0) or 0,
                "50-69": (row["mid"] if row else 0) or 0,
                "30-49": (row["low"] if row else 0) or 0,
                "under 30": (row["poor"] if row else 0) or 0,
            }
        return stats

    # ---------------------------------------------------------- run history

    def start_run(self, action, target=""):
        with self._lock:
            conn = self._conn()
            cursor = conn.execute(
                "INSERT INTO runs (started_at, action, target, status) "
                "VALUES (?, ?, ?, 'running')", (_utcnow(), action, target))
            conn.commit()
            return cursor.lastrowid

    def finish_run(self, run_id, status, added=0, message="", stats=None):
        stats = stats or {}
        total = stats.get("total", 0)
        with_email = stats.get("with_email", 0)
        hit_rate = round(with_email / total * 100, 1) if total else 0.0
        with self._lock:
            conn = self._conn()
            conn.execute("""
                UPDATE runs SET finished_at = ?, status = ?, added = ?,
                       total = ?, with_email = ?, hit_rate = ?, message = ?
                WHERE id = ?
            """, (_utcnow(), status, added, total, with_email, hit_rate,
                  message, run_id))
            conn.commit()

    def recent_runs(self, limit=25):
        with self._lock:
            rows = self._conn().execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- outreach

    def suppress(self, value, reason=""):
        """Never contact this address (or "@domain.com") again.

        Deliberately has no expiry and no bulk-clear: an opt-out that quietly
        lapses is worse than never having recorded it.
        """
        value = str(value or "").strip().lower()
        if not value or ("@" not in value):
            return False
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO suppressions (value, reason, added_at) "
                "VALUES (?, ?, ?)", (value, reason, _utcnow()))
            conn.commit()
        return True

    def unsuppress(self, value):
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM suppressions WHERE value = ?",
                         (str(value or "").strip().lower(),))
            conn.commit()

    def is_suppressed(self, email):
        """True if this address, or its whole domain, is on the list."""
        email = str(email or "").strip().lower()
        if not email or "@" not in email:
            return True  # nothing sendable is also nothing to send to
        domain = "@" + email.split("@", 1)[1]
        with self._lock:
            row = self._conn().execute(
                "SELECT 1 FROM suppressions WHERE value IN (?, ?) LIMIT 1",
                (email, domain)).fetchone()
        return row is not None

    def suppressions(self):
        with self._lock:
            rows = self._conn().execute(
                "SELECT * FROM suppressions ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]

    def record_send(self, lead_id, email, campaign, subject, status, detail=""):
        """Log one message and move the lead's Outreach Status along."""
        now = _utcnow()
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO outreach (lead_id, email, campaign, subject, "
                "sent_at, status, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lead_id, str(email or "").lower(), campaign, subject, now,
                 status, detail))
            if status == "sent":
                conn.execute(
                    "UPDATE leads SET outreach_status = 'Contacted', updated_at = ? "
                    "WHERE id = ? AND (outreach_status IS NULL "
                    "OR outreach_status IN ('', 'Not Contacted'))",
                    (now, lead_id))
            conn.commit()

    def already_contacted(self, lead_id=None, email=None):
        """Whether a successful send has already gone to this lead or address.

        Checked on both, because the same person can appear under two lead
        records from two sources — and being emailed twice by the same company
        in one week is how a prospect becomes a spam complaint.
        """
        clauses, params = [], []
        if lead_id is not None:
            clauses.append("lead_id = ?")
            params.append(lead_id)
        if email:
            clauses.append("email = ?")
            params.append(str(email).strip().lower())
        if not clauses:
            return False
        with self._lock:
            row = self._conn().execute(
                "SELECT 1 FROM outreach WHERE status = 'sent' AND (" +
                " OR ".join(clauses) + ") LIMIT 1", params).fetchone()
        return row is not None

    def sent_since(self, iso_timestamp):
        """How many messages went out since a moment — used for the daily cap."""
        with self._lock:
            row = self._conn().execute(
                "SELECT COUNT(*) AS n FROM outreach "
                "WHERE status = 'sent' AND sent_at >= ?", (iso_timestamp,)).fetchone()
        return row["n"] if row else 0

    def outreach_history(self, limit=100):
        with self._lock:
            rows = self._conn().execute(
                "SELECT o.*, l.first_name, l.last_name, l.org_name "
                "FROM outreach o LEFT JOIN leads l ON l.id = o.lead_id "
                "ORDER BY o.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def outreach_stats(self):
        with self._lock:
            row = self._conn().execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
                FROM outreach
            """).fetchone()
            suppressed = self._conn().execute(
                "SELECT COUNT(*) AS n FROM suppressions").fetchone()
        return {
            "total": (row["total"] if row else 0) or 0,
            "sent": (row["sent"] if row else 0) or 0,
            "failed": (row["failed"] if row else 0) or 0,
            "suppressed": (suppressed["n"] if suppressed else 0) or 0,
        }

    # ---------------------------------------------------------- checkpoints

    def set_checkpoint(self, job_key, state):
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO checkpoints (job_key, state, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(job_key) DO UPDATE SET state = excluded.state, "
                "updated_at = excluded.updated_at",
                (job_key, json.dumps(state), _utcnow()))
            conn.commit()

    def get_checkpoint(self, job_key):
        with self._lock:
            row = self._conn().execute(
                "SELECT state, updated_at FROM checkpoints WHERE job_key = ?",
                (job_key,)).fetchone()
        if row is None:
            return None
        try:
            state = json.loads(row["state"])
        except ValueError:
            return None
        state["_updated_at"] = row["updated_at"]
        return state

    def clear_checkpoint(self, job_key):
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM checkpoints WHERE job_key = ?", (job_key,))
            conn.commit()

    # ------------------------------------------------------------- imports

    def import_run_history(self, filename="run_history.json", logger=None):
        """Bring the old JSON run log into the runs table.

        Without this the hit-rate trend chart restarts from empty, throwing
        away the record of how the tool has actually performed over time —
        including the flat-zero runs that showed it was broken.
        """
        log = logger or self.logger
        if not os.path.exists(filename):
            return 0
        try:
            with open(filename, "r", encoding="utf-8") as handle:
                history = json.load(handle)
        except (ValueError, OSError) as e:
            log.warning("Could not read " + filename + ": " + str(e))
            return 0
        if not isinstance(history, list):
            return 0

        with self._lock:
            conn = self._conn()
            if conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]:
                return 0  # already has history; don't double-import

            imported = 0
            for record in history:
                if not isinstance(record, dict):
                    continue
                timestamp = record.get("timestamp") or ""
                total = int(record.get("total") or 0)
                # A run that recorded nothing at all was a failure that
                # reported success at the time. Label it truthfully now.
                status = RUN_EMPTY if total == 0 else RUN_OK
                conn.execute("""
                    INSERT INTO runs (started_at, finished_at, action, target,
                                      status, added, total, with_email, hit_rate,
                                      message)
                    VALUES (?, ?, ?, '', ?, 0, ?, ?, ?, ?)
                """, (timestamp, timestamp, record.get("action") or "unknown",
                      status, total, int(record.get("with_email") or 0),
                      float(record.get("hit_rate") or 0.0),
                      "imported from run_history.json"))
                imported += 1
            conn.commit()

        if imported:
            log.info("Imported " + str(imported) + " past runs from " +
                     os.path.basename(filename) + ".")
        return imported

    def import_from_excel(self, filename, logger=None):
        """One-time migration of an existing workbook into the database.

        Runs automatically the first time the app starts against an empty
        database, so the leads collected before this module existed are not
        stranded in a file the app no longer treats as storage. Idempotent:
        duplicate identities are skipped by the same index that protects normal
        inserts, so running it twice imports nothing the second time.
        """
        log = logger or self.logger
        if not os.path.exists(filename):
            return 0
        try:
            import openpyxl
        except ImportError:
            log.warning("openpyxl is not installed, so " + filename +
                        " could not be imported.")
            return 0

        workbook = openpyxl.load_workbook(filename, read_only=True, data_only=True)
        imported = 0
        try:
            for sheet_name in workbook.sheetnames:
                # Tabs this project's own exporter generates. They are a
                # rendering of the database, not data, and re-importing them
                # would feed derived rows back in as if they were leads.
                if sheet_name in GENERATED_SHEETS:
                    continue
                worksheet = workbook[sheet_name]
                rows = worksheet.iter_rows(values_only=True)
                try:
                    header_row = next(rows)
                except StopIteration:
                    continue
                headers = [str(h).strip() if h is not None else "" for h in header_row]

                batch = []
                for row in rows:
                    if row is None or all(v in (None, "") for v in row):
                        continue
                    lead = {}
                    for index, header in enumerate(headers):
                        if header in HEADER_TO_COLUMN and index < len(row):
                            lead[header] = "" if row[index] is None else str(row[index])
                    if not lead.get("NPI Number"):
                        continue
                    # The workbook's own tab name is the authority on where
                    # this lead was filed; Source/State may be blank in older
                    # rows written before those columns were populated.
                    lead.setdefault("Source", self.source_of_sheet(sheet_name))
                    batch.append(lead)

                if batch:
                    imported += sum(self.add_leads_batch(batch).values())
        finally:
            workbook.close()

        if imported:
            log.info("Imported " + str(imported) + " existing leads from " +
                     os.path.basename(filename) + " into the database.")
        return imported


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_text(value):
    """Everything is stored as TEXT so a value that arrives as an int one run
    and a string the next still compares equal for duplicate detection."""
    if value is None:
        return ""
    return str(value)
