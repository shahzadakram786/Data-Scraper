"""CMS open data — billing volume, practice size and federally-filed phones.

Two free federal datasets, both keyed on NPI, both public domain:

  * **Medicare Physician & Other Practitioners (by Provider)** — what a
    provider billed Medicare in a year: submitted charges, payments received,
    patient and service counts. This is the closest thing to a public revenue
    figure for a practice, and it is what sizes an RCM contract.

  * **Doctors & Clinicians (National Downloadable File)** — 3.4M enrollment
    records: the practice a clinician bills under, **how many clinicians are in
    it**, and the phone number they filed with Medicare.

Two things to know before relying on this.

**Coverage is partial, by design of the source.** Only Medicare-enrolled
providers appear, and CMS suppresses anyone with fewer than 11 beneficiaries to
protect patient privacy. Measured against this project's own leads: roughly 29%
of NPI/ZocDoc-sourced providers have billing data and 40% have an enrollment
record, against 0% and 20% for Psychology Today therapists — most therapists are
cash-pay or commercial-insurance only. A provider with no record here is normal,
not an error.

**data.cms.gov geo-fences its API**, answering 403 outside the US exactly like
Psychology Today does. The same free r.jina.ai reader relay gets through, at
about 2 seconds per billing lookup, so this is an enrichment pass rather than
something that runs inline with scraping. Set a US proxy to go direct and it is
several times faster.

What this deliberately does NOT report is a "collections gap" between submitted
charges and Medicare payments. That difference is overwhelmingly the contractual
write-down every provider takes against the Medicare fee schedule, not money
anyone failed to collect, and presenting it as a problem to solve would be
selling a misleading number.
"""

import re
import json
import time
import logging
import threading

import requests

import http_pool
from errors import SourceUnavailableError


class CMSEngine:
    """Looks up CMS facts by NPI. Safe to share across threads."""

    # "Medicare Physician & Other Practitioners - by Provider", latest release.
    BILLING_DATASET = "8889d81e-2ee7-448f-8713-f071038289b5"
    BILLING_URL = ("https://data.cms.gov/data-api/v1/dataset/{dataset}/data"
                   "?filter[Rndrng_NPI]={npi}&size=1")

    # "Doctors and Clinicians" national downloadable file, Provider Data Catalog.
    CLINICIANS_URL = (
        "https://data.cms.gov/provider-data/api/1/datastore/query/mj5m-pzi6/0"
        "?conditions[0][property]=npi&conditions[0][operator]=%3D"
        "&conditions[0][value]={npi}&limit=5")

    RELAY_URL = "https://r.jina.ai/{url}"
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/plain, */*",
    }
    DIRECT_TIMEOUT = 20
    RELAY_TIMEOUT = 120

    def __init__(self, logger=None, proxies=None):
        self.logger = logger or logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        if proxies:
            self.session.proxies.update(proxies)
        self._lock = threading.Lock()
        # Set once the API answers 403, so every later call skips the pointless
        # direct attempt and goes straight to the relay.
        self._geo_blocked = False
        self._warned_relay = False
        self._billing_cache = {}
        self._clinician_cache = {}

    # ------------------------------------------------------------------ API

    def billing_for(self, npi):
        """Annual Medicare billing for one NPI, or {} if they do not appear.

        Returns {annual_billing, medicare_paid, patient_volume, service_count,
        billing_year_specialty}. Absent is the common case — see the module
        docstring on coverage.
        """
        npi = str(npi or "").strip()
        if not npi.isdigit() or len(npi) != 10:
            return {}
        with self._lock:
            if npi in self._billing_cache:
                return dict(self._billing_cache[npi])

        url = self.BILLING_URL.format(dataset=self.BILLING_DATASET, npi=npi)
        rows = self._get_json(url)
        record = {}
        if isinstance(rows, list) and rows:
            row = rows[0]
            record = {
                "annual_billing": _number(row.get("Tot_Sbmtd_Chrg")),
                "medicare_paid": _number(row.get("Tot_Mdcr_Pymt_Amt")),
                "patient_volume": _number(row.get("Tot_Benes")),
                "service_count": _number(row.get("Tot_Srvcs")),
                "specialty": (row.get("Rndrng_Prvdr_Type") or "").strip(),
            }

        with self._lock:
            self._billing_cache[npi] = record
        return dict(record)

    def enrollment_for(self, npi):
        """Practice affiliation, size and filed phone for one NPI, or {}.

        A clinician with several enrollment records (multiple practices or
        addresses) returns the one with the largest listed practice, which is
        the affiliation worth pitching.
        """
        npi = str(npi or "").strip()
        if not npi.isdigit() or len(npi) != 10:
            return {}
        with self._lock:
            if npi in self._clinician_cache:
                return dict(self._clinician_cache[npi])

        payload = self._get_json(self.CLINICIANS_URL.format(npi=npi))
        rows = (payload or {}).get("results") if isinstance(payload, dict) else None
        record = {}
        if rows:
            best = max(rows, key=lambda r: _number(r.get("num_org_mem")) or 0)
            record = {
                "practice_name": (best.get("facility_name") or "").strip(),
                "practice_size": _number(best.get("num_org_mem")),
                "practice_id": (best.get("org_pac_id") or "").strip(),
                "filed_phone": _digits(best.get("telephone_number")),
                "specialty": (best.get("pri_spec") or "").strip(),
                "telehealth": (best.get("telehlth") or "").strip().upper() == "Y",
                "city": (best.get("citytown") or "").strip(),
                "state": (best.get("state") or "").strip(),
            }

        with self._lock:
            self._clinician_cache[npi] = record
        return dict(record)

    def self_check(self):
        """Ten-second "is CMS reachable and still shaped the same?" check."""
        report = {"source": "CMS Open Data", "ok": False, "steps": []}

        def step(name, ok, detail):
            report["steps"].append({"name": name, "ok": ok, "detail": detail})

        # A provider known to carry both a billing record and an enrollment record.
        try:
            billing = self.billing_for("1003000126")
        except Exception as e:
            step("Look up Medicare billing", False, "{0}: {1}".format(type(e).__name__, e))
            return report

        route = "the r.jina.ai reader relay" if self._geo_blocked else "a direct request"
        if billing.get("annual_billing"):
            step("Look up Medicare billing", True,
                 "Read a billing record via {0}.".format(route))
        else:
            step("Look up Medicare billing", False,
                 "The API answered but carried no billing figures for a provider "
                 "that has them — the dataset may have been re-released with "
                 "different field names.")
            return report

        try:
            enrollment = self.enrollment_for("1003000126")
        except Exception as e:
            step("Look up practice enrollment", False,
                 "{0}: {1}".format(type(e).__name__, e))
            return report

        if enrollment.get("practice_size") or enrollment.get("filed_phone"):
            step("Look up practice enrollment", True,
                 "{0} clinician(s) at {1}.".format(
                     enrollment.get("practice_size") or "?",
                     enrollment.get("practice_name") or "an unnamed practice"))
        else:
            step("Look up practice enrollment", False,
                 "No enrollment record came back for a provider who has one.")

        report["ok"] = all(s["ok"] for s in report["steps"])
        return report

    # ------------------------------------------------------------ internals

    def _get_json(self, url, retries=2):
        """Fetch and decode, going through the reader relay when CMS refuses
        this region. Returns None rather than raising for a single missing
        lookup — a provider with no record must not end a run."""
        last_problem = "no response"
        for attempt in range(retries):
            if not self._geo_blocked:
                try:
                    response = self.session.get(url, timeout=self.DIRECT_TIMEOUT)
                    if response.status_code == 403:
                        self._note_geo_block()
                    elif response.status_code == 200:
                        return _loads(response.text)
                    elif response.status_code == 404:
                        return None
                    else:
                        last_problem = "HTTP {0}".format(response.status_code)
                except requests.RequestException as e:
                    last_problem = "{0}: {1}".format(type(e).__name__, e)

            if self._geo_blocked:
                text = self._fetch_via_relay(url)
                if text is not None:
                    return _loads(text)
                last_problem = "the reader relay returned nothing"

            time.sleep(1.5)

        self.logger.debug("CMS lookup failed (%s): %s", last_problem, url)
        return None

    def _note_geo_block(self):
        with self._lock:
            first_time = not self._geo_blocked
            self._geo_blocked = True
        if first_time and not self._warned_relay:
            self._warned_relay = True
            self.logger.info(
                "data.cms.gov is not serving this region directly (HTTP 403). "
                "Falling back to the free r.jina.ai reader — about 2 seconds "
                "per lookup. Set a US proxy in Settings to go direct instead.")

    def _fetch_via_relay(self, url):
        try:
            # Pooled per worker: every lookup goes to the same reader host, and
            # the handshake was a real share of the ~2 seconds each one took.
            response = http_pool.session().get(self.RELAY_URL.format(url=url),
                                               proxies=self.session.proxies or None,
                                               timeout=self.RELAY_TIMEOUT)
            if response.status_code == 200:
                return response.text
            self.logger.debug("CMS relay -> HTTP %s", response.status_code)
        except requests.RequestException as e:
            self.logger.debug("CMS relay failed: %s", e)
        return None


def _loads(text):
    """Decode a JSON payload that may be wrapped in the relay's markdown.

    The reader prefixes the response with a title/URL header, so the payload
    starts at the first bracket after "Markdown Content:".
    """
    if not text:
        return None
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass

    marker = text.find("Markdown Content:")
    search_from = marker + len("Markdown Content:") if marker >= 0 else 0

    # Whichever bracket comes *first* is the outermost one. Picking "[" before
    # "{" instead would grab the inner array out of {"results": [...]} — which
    # parses cleanly and returns the wrong shape, so the caller sees a list
    # where it expects an object and silently reports no record.
    candidates = []
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener, search_from)
        if start >= 0:
            candidates.append((start, closer))
    candidates.sort()

    for start, closer in candidates:
        end = text.rfind(closer)
        if end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    return None


def _number(value):
    """CMS returns numbers as strings, sometimes with formatting. Returns an
    int for whole values (practice size, patient counts) and a float for money,
    or None when the field is blank or suppressed."""
    if value in (None, "", "*"):  # "*" is CMS's suppression marker
        return None
    text = re.sub(r'[^0-9.\-]', '', str(value))
    if not text or text in ("-", "."):
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return int(number) if number.is_integer() else round(number, 2)


def _digits(value):
    digits = re.sub(r'\D', '', str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""
