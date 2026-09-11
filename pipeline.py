"""The actual work, with no user interface attached.

Everything here used to live inside the Tkinter window class, which meant none
of it could be tested, scripted, scheduled or run on a server — the logic that
decides which phone number to trust was reachable only by clicking a button.

Now the window is one caller and cli.py is another. A pipeline talks to the
outside world through a Reporter, so the same code path can write to a Tk log
pane, to stdout, or to nothing at all during a test.
"""

import re
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import storage
import outreach
import verification
from errors import ScraperError
from storage import LeadStore, RUN_OK, RUN_EMPTY, RUN_STOPPED, RUN_FAILED
from datetime import datetime, timezone


def _utc_start_of_day():
    """Midnight UTC today, as the stored timestamps are formatted. The daily
    cap counts against this rather than a rolling 24 hours, so the allowance
    resets predictably instead of drifting."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0,
                       microsecond=0).isoformat(timespec="seconds")


class Reporter:
    """How a pipeline talks to whoever started it.

    The default implementation logs and never stops; the app subclasses it to
    marshal onto the Tk thread, and tests pass one that records calls.
    """

    def __init__(self, logger=None, stop_event=None):
        self.logger = logger or logging.getLogger("LeadGen")
        self.stop_event = stop_event or threading.Event()

    def log(self, message, level="info"):
        getattr(self.logger, level, self.logger.info)(message)

    def progress(self, done, total):
        """0-100 completion. total may be 0 while the total is still unknown."""

    def counts(self, sheet_key, leads=0, emails=0):
        """Running per-tab totals, for a live results panel."""

    def phase(self, text):
        """A short description of what is happening right now."""
        self.log(text)

    def stopped(self):
        return self.stop_event.is_set()


class RunResult:
    """What a pipeline run did. `status` is one of storage's RUN_* values."""

    def __init__(self, status, added=0, message="", stats=None, error=None):
        self.status = status
        self.added = added
        self.message = message
        self.stats = stats or {}
        self.error = error

    @property
    def ok(self):
        return self.status == RUN_OK

    def __repr__(self):
        return "<RunResult {0} added={1} {2!r}>".format(
            self.status, self.added, self.message)


class LeadPipeline:
    """Collection and enrichment against a LeadStore.

    One instance per run configuration. Engines are injected so tests can pass
    fakes and the app can swap the Psychology Today engine when the proxy
    setting changes.
    """

    PT_WORKERS = 5      # concurrent provider lookups; the work is on each
                        # provider's own site, not on psychologytoday.com
    EMAIL_WORKERS = 5   # DDGS self-throttles across the pool when rate-limited
    VERIFY_WORKERS = 8  # DNS/SMTP is almost all waiting
    CMS_WORKERS = 6     # each lookup is ~2s of relay round-trip, all waiting
    FLUSH_EVERY = 50    # leads buffered before a database write

    def __init__(self, store, npi_engine, email_engine, pt_engine,
                 verifier=None, cms_engine=None, reporter=None, settings=None):
        self.store = store
        self.npi_engine = npi_engine
        self.email_engine = email_engine
        self.pt_engine = pt_engine
        self.verifier = verifier
        self.cms_engine = cms_engine
        self.reporter = reporter or Reporter()
        self.settings = settings or {}
        self.logger = self.reporter.logger

    # --------------------------------------------------------------- helpers

    def _flush(self, leads):
        """Write a batch and report what landed. Returns leads still unsaved.

        A write that fails keeps the batch buffered for the next attempt rather
        than throwing away everything scraped so far.
        """
        if not leads:
            return [], 0
        try:
            counts = self.store.add_leads_batch(leads)
        except Exception as e:
            self.reporter.log(
                "Could not save {0} leads yet ({1}) — keeping them buffered "
                "and retrying.".format(len(leads), e), "warning")
            return leads, 0
        for sheet_key, count in counts.items():
            emails = sum(1 for lead in leads
                         if lead.get("Email")
                         and self.store.sheet_key_for(lead) == sheet_key)
            self.reporter.counts(sheet_key, leads=count, emails=min(emails, count))
        return [], sum(counts.values())

    def _finish(self, run_id, status, added, message=""):
        stats = self.store.get_stats()
        self.store.finish_run(run_id, status, added=added, message=message,
                              stats=stats)
        return RunResult(status, added=added, message=message, stats=stats)

    def _guard_empty(self, added, status):
        """A run that completes without error but adds nothing is suspicious.

        It is the fingerprint of a scraper that has quietly stopped working, so
        it is recorded as `empty` rather than `ok` and surfaced as a warning.
        Genuinely exhausted sources (a state already fully collected) land here
        too, which is why the message says both possibilities.
        """
        if status == RUN_OK and added == 0:
            return RUN_EMPTY
        return status

    # ------------------------------------------------- Psychology Today

    def collect_psychology_today(self, state_name, state_abbr, owners_only=False,
                                 resume=True, max_pages=None):
        """Walk a state's therapist directory, enriching each provider from
        their own website, and save as we go."""
        job_key = "pt:{0}:owners={1}".format(state_name, int(owners_only))
        run_id = self.store.start_run("pt_collect", state_name)

        start_page = 1
        if resume:
            checkpoint = self.store.get_checkpoint(job_key)
            if checkpoint and checkpoint.get("next_page", 1) > 1:
                start_page = int(checkpoint["next_page"])
                self.reporter.log(
                    "Resuming {0} from listing page {1} (a previous run stopped "
                    "there on {2}). Providers already collected are skipped "
                    "automatically.".format(
                        state_name, start_page, checkpoint.get("_updated_at", "")[:10]))

        self.reporter.phase("Searching Psychology Today for therapists in {0}...".format(state_name))
        if owners_only:
            self.reporter.log(
                "Owners/CEOs/Founders filter is ON — only providers whose own "
                "website presents them as running the practice will be saved.")

        buffer = []
        added = 0
        queued = 0
        done = 0
        skipped_not_owner = 0
        stopped = False
        last_page = start_page - 1

        try:
            with ThreadPoolExecutor(max_workers=self.PT_WORKERS) as executor:
                pending = {}

                def drain(futures):
                    nonlocal done, buffer, added, skipped_not_owner
                    for future in futures:
                        entry = pending.pop(future, {})
                        done += 1
                        try:
                            lead = future.result()
                        except Exception as e:
                            self.reporter.log(
                                "Failed to process {0}: {1}".format(
                                    entry.get("name"), e), "warning")
                            continue
                        if owners_only and not self.email_engine.is_ownership_role(
                                lead.get("Role")):
                            skipped_not_owner += 1
                            self.reporter.log(
                                "[{0}/{1}] {2} — skipped (not listed as "
                                "owner/CEO/founder)".format(done, queued, entry.get("name")))
                        else:
                            buffer.append(lead)
                            label = (" ".join(filter(None, (
                                lead.get("Provider First Name"),
                                lead.get("Provider Last Name")))).strip()
                                or lead.get("Organization Name") or "Unknown")
                            self.reporter.log("[{0}/{1}] {2} — {3}".format(
                                done, queued, label,
                                "email found" if lead.get("Email") else "no email"))
                        self.reporter.progress(done, queued)
                        if len(buffer) >= self.FLUSH_EVERY:
                            buffer, saved = self._flush(buffer)
                            added += saved

                for page, entries in self.pt_engine.iter_listing(
                        state_name, stop_event=self.reporter.stop_event,
                        max_pages=max_pages, start_page=start_page):
                    if self.reporter.stopped():
                        stopped = True
                        break
                    last_page = page
                    for entry in entries:
                        future = executor.submit(self.process_pt_entry, entry, state_abbr)
                        pending[future] = entry
                        queued += 1
                    self.reporter.log(
                        "Queued {0} providers so far; {1} processed.".format(queued, done))
                    drain({f for f in list(pending) if f.done()})
                    # Checkpoint the page we have fully queued, so a crash or a
                    # stop resumes from here rather than from page one.
                    self.store.set_checkpoint(job_key, {"next_page": page + 1})

                if not stopped:
                    self.reporter.phase(
                        "Listing complete: {0} providers. Finishing website "
                        "lookups...".format(queued))
                for future in as_completed(list(pending)):
                    drain({future})
                    if self.reporter.stopped():
                        stopped = True
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

            buffer, saved = self._flush(buffer)
            added += saved
            if buffer:
                self.reporter.log(
                    "{0} scraped leads could not be saved.".format(len(buffer)), "error")

            if owners_only:
                self.reporter.log(
                    "Owners filter: {0} providers skipped as not listed as "
                    "owner/CEO/founder on their own site.".format(skipped_not_owner))

            if stopped:
                return self._finish(run_id, RUN_STOPPED, added,
                                    "Stopped by user at listing page {0}.".format(last_page))

            self.store.clear_checkpoint(job_key)
            status = self._guard_empty(added, RUN_OK)
            message = ""
            if status == RUN_EMPTY:
                message = ("The run completed but added no new leads. Either "
                           "this state has already been collected, or every "
                           "provider was filtered out.")
                self.reporter.log(message, "warning")
            return self._finish(run_id, status, added, message)

        except ScraperError as e:
            # A typed failure: the scraper is broken or blocked, not merely
            # finished. Say so loudly rather than reporting a clean finish.
            self.reporter.log(str(e), "error")
            if e.remedy:
                self.reporter.log(e.remedy, "error")
            buffer, saved = self._flush(buffer)
            added += saved
            result = self._finish(run_id, RUN_FAILED, added, str(e))
            result.error = e
            return result
        except Exception as e:
            self.reporter.log("Unexpected error: {0}".format(e), "error")
            buffer, saved = self._flush(buffer)
            added += saved
            result = self._finish(run_id, RUN_FAILED, added, str(e))
            result.error = e
            return result

    def process_pt_entry(self, entry, state_abbr):
        """The full manual workflow for one Psychology Today provider:

        1. Follow the provider's own website (if they list one) and read the
           email, phone, ownership role and social profiles in a single pass.
        2. Cross-reference the NPI registry by name/org + state to attach a
           verified NPI number, skipping ambiguous matches.
        3. If still no email, try the practice's mail server for a standard
           address pattern, then fall back to searching the phone number.

        The Phone column comes from the provider's own website, preferring a
        number the site presents as their direct line, and falls back to the
        Psychology Today listing only when the site yields nothing. The NPI
        registry confirms identity but is never used for the phone — its number
        is the registered administrative line, often a different office.
        """
        # The listing row already carried the credential line the profile page
        # exposes as jobTitle, and the website endpoint 404s by itself when the
        # provider lists no site — so the profile page is only worth fetching
        # when the listing gave us no credential. Profile pages are geo-fenced,
        # so each one costs a relayed request.
        specialty = entry.get("credential", "")
        website = self.pt_engine._resolve_website(entry["pt_id"])
        if not specialty:
            details = self.pt_engine.get_profile_details(entry["pt_id"], entry["profile_url"])
            specialty = details.get("specialty", "")
            website = website or details.get("website", "")

        first_name, last_name = ("", "") if entry["is_org"] else self.split_pt_name(entry["name"])

        contact = (
            self.email_engine.scrape_website_contact_info(website, person_name=entry.get("name"))
            if website else
            {"email": "", "phone": "", "phone_confidence": "", "role": "", "socials": {}}
        )
        email = contact.get("email", "")
        role = contact.get("role", "")
        socials = contact.get("socials", {}) or {}

        npi_match = None
        try:
            if entry["is_org"]:
                npi_match = self.npi_engine.find_by_organization(
                    entry["name"], state_abbr, city=entry.get("city"))
            elif first_name and last_name:
                npi_match = self.npi_engine.find_by_name(
                    first_name, last_name, state_abbr, city=entry.get("city"))
        except Exception as e:
            self.reporter.log("NPI cross-reference failed for {0}: {1}".format(
                entry.get("name"), e), "warning")

        phone, phone_source = self.pick_authentic_phone(contact, entry)
        email_source = "Provider website" if email else ""

        if not email and website:
            # The practice has a domain but publishes no address. Ask its own
            # mail server whether the standard patterns exist — a confirmed
            # mailbox is not a guess (see EmailVerifier.guess_addresses).
            guessed = self.guess_email(first_name, last_name, website)
            if guessed:
                email = guessed
                email_source = "Confirmed by the practice's mail server"

        if not email:
            # Attributed only when the search result actually names this
            # provider or quotes their number — an uncorroborated address off a
            # search snippet belongs to somebody else.
            email = self.email_engine.find_email_by_phone(
                phone,
                person_name=None if entry["is_org"] else entry.get("name"),
                org_name=entry.get("name") if entry["is_org"] else None)
            if email:
                email_source = "Corroborated web search"

        lead = {
            "Role": role,
            "LinkedIn": socials.get("LinkedIn", ""),
            "Facebook": socials.get("Facebook", ""),
            "Instagram": socials.get("Instagram", ""),
            "Other Social": socials.get("Other", ""),
            "NPI Number": npi_match["NPI Number"] if npi_match else "PT-{0}".format(entry["pt_id"]),
            "Provider First Name": first_name,
            "Provider Last Name": last_name,
            "Organization Name": entry["name"] if entry["is_org"] else (
                npi_match.get("Organization Name", "") if npi_match else ""),
            "Phone": phone,
            "Email": email,
            "Address": npi_match.get("Address", "") if npi_match else "",
            "City": entry.get("city") or (npi_match.get("City", "") if npi_match else ""),
            "State": state_abbr,
            "Zip": entry.get("zip") or (npi_match.get("Zip", "") if npi_match else ""),
            "Specialty": specialty or (npi_match.get("Specialty", "") if npi_match else ""),
            "Website": website,
            "Outreach Status": "Not Contacted",
            "Last Updated": "",
            "Source": "Psychology Today",
            "Phone Source": phone_source,
        }
        self.attach_verification(lead, person_name=entry.get("name"),
                                 note=email_source)
        return lead

    def pick_authentic_phone(self, contact, entry):
        """The number most likely to reach this provider personally.

        The site scrape already prefers a number labelled as a cell or direct
        line over the shared office line, and discards fax and crisis numbers.
        Psychology Today's own listed number is the last resort: it is usually
        a call-tracking number that forwards, so it is labelled as such.
        Returns ("", "") rather than guessing when nothing dialable was found.
        """
        site_phone = contact.get("phone", "")
        if site_phone and self.email_engine.is_valid_phone(site_phone):
            confidence = contact.get("phone_confidence", "")
            return site_phone, ("Provider Website ({0})".format(confidence)
                                if confidence else "Provider Website")

        listing_phone = entry.get("phone", "")
        if listing_phone and self.email_engine.is_valid_phone(listing_phone):
            source = "Psychology Today (listing)"
            if self.email_engine.is_tollfree(listing_phone):
                source += " (toll-free)"
            return listing_phone, source
        return "", ""

    @staticmethod
    def split_pt_name(full_name):
        """Best-effort split of a display name into first/last. Drops
        parenthetical maiden/alternate names, e.g. 'Shelby Scott (Gregory)'."""
        cleaned = re.sub(r'\([^)]*\)', '', full_name or '').strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)
        if not cleaned:
            return "", ""
        parts = cleaned.split(' ')
        if len(parts) == 1:
            return parts[0], ""
        return " ".join(parts[:-1]), parts[-1]

    # ------------------------------------------------------- NPI registry

    def collect_npi(self, state_abbrs, specialties, exclude_corp=True, min_year=None):
        """Bulk-collect from the NPI registry across states and specialties."""
        run_id = self.store.start_run(
            "npi_collect",
            "{0} state(s), {1} specialty(ies)".format(len(state_abbrs), len(specialties)))
        total_jobs = len(state_abbrs) * len(specialties)
        done = 0
        added = 0
        stopped = False

        try:
            for state in state_abbrs:
                if self.reporter.stopped():
                    stopped = True
                    break
                for specialty in specialties:
                    if self.reporter.stopped():
                        stopped = True
                        break
                    self.reporter.phase("Fetching {0} in {1}...".format(specialty, state))
                    providers = self.npi_engine.fetch_providers(
                        state, specialty, exclude_corp=exclude_corp,
                        min_updated_year=min_year,
                        stop_event=self.reporter.stop_event)
                    if providers:
                        _unsaved, saved = self._flush(providers)
                        added += saved
                        self.reporter.log("Added {0} leads.".format(saved))
                    done += 1
                    self.reporter.progress(done, total_jobs)

            if stopped:
                return self._finish(run_id, RUN_STOPPED, added, "Stopped by user.")
            status = self._guard_empty(added, RUN_OK)
            message = ""
            if status == RUN_EMPTY:
                message = ("No new providers were returned. Either these "
                           "state/specialty combinations are already collected, "
                           "or the registry filter excluded everything.")
                self.reporter.log(message, "warning")
            return self._finish(run_id, status, added, message)
        except Exception as e:
            self.reporter.log("Unexpected error: {0}".format(e), "error")
            result = self._finish(run_id, RUN_FAILED, added, str(e))
            result.error = e
            return result

    @staticmethod
    def extract_npi_numbers(text):
        """Pull NPI numbers out of pasted text.

        Prefers numbers that appear right after the word "NPI" (how ZocDoc and
        similar sites label them) so an unrelated 10-digit number can't be
        mistaken for one; falls back to any bare 10-digit number starting 1 or
        2 (the only valid NPI prefixes) so a plain list still works.
        """
        contextual = re.findall(r'NPI[^0-9]{0,10}(\d{10})', text, re.IGNORECASE)
        found = contextual if contextual else re.findall(r'\b[12]\d{9}\b', text)
        seen = set()
        result = []
        for number in found:
            if number not in seen:
                seen.add(number)
                result.append(number)
        return result

    def import_npis(self, npi_numbers):
        """Look up pasted NPI numbers and enrich each one (the ZocDoc flow —
        ZocDoc blocks automated access, so the numbers are copied by hand and
        everything after that is automated)."""
        run_id = self.store.start_run("npi_import", "{0} numbers".format(len(npi_numbers)))
        total = len(npi_numbers)
        done = 0
        added = 0
        buffer = []
        stopped = False

        try:
            self.reporter.phase("Looking up {0} pasted NPI numbers...".format(total))
            with ThreadPoolExecutor(max_workers=self.PT_WORKERS) as executor:
                futures = {executor.submit(self.process_npi_number, n): n
                           for n in npi_numbers}
                for future in as_completed(futures):
                    if self.reporter.stopped():
                        stopped = True
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    number = futures[future]
                    done += 1
                    try:
                        lead = future.result()
                    except Exception as e:
                        self.reporter.log(
                            "Failed to process NPI {0}: {1}".format(number, e), "warning")
                        continue
                    if not lead:
                        self.reporter.log(
                            "[{0}/{1}] NPI {2} — not found in the registry".format(
                                done, total, number))
                        self.reporter.progress(done, total)
                        continue
                    buffer.append(lead)
                    label = (" ".join(filter(None, (lead.get("Provider First Name"),
                                                    lead.get("Provider Last Name")))).strip()
                             or lead.get("Organization Name") or "Unknown")
                    found = len([e for e in (lead.get("All Emails Found") or "").split(", ") if e])
                    self.reporter.log("[{0}/{1}] {2} (NPI {3}) — {4} email(s)".format(
                        done, total, label, number, found))
                    self.reporter.progress(done, total)
                    if len(buffer) >= self.FLUSH_EVERY:
                        buffer, saved = self._flush(buffer)
                        added += saved

            buffer, saved = self._flush(buffer)
            added += saved

            if stopped:
                return self._finish(run_id, RUN_STOPPED, added, "Stopped by user.")
            status = self._guard_empty(added, RUN_OK)
            message = ("None of these NPI numbers produced a new lead — they may "
                       "already be in the database." if status == RUN_EMPTY else "")
            if message:
                self.reporter.log(message, "warning")
            return self._finish(run_id, status, added, message)
        except Exception as e:
            self.reporter.log("Unexpected error: {0}".format(e), "error")
            result = self._finish(run_id, RUN_FAILED, added, str(e))
            result.error = e
            return result

    def process_npi_number(self, npi_number):
        """Look an NPI up exactly, then search its phone number for emails,
        falling back to a full-name search. Returns a lead dict or None."""
        match = self.npi_engine.find_by_number(npi_number)
        if not match:
            return None

        phone = match.get("Phone", "")
        person_name = " ".join(filter(None, (match.get("Provider First Name"),
                                             match.get("Provider Last Name")))).strip()
        org_name = match.get("Organization Name", "")

        emails = []
        if phone:
            emails = self.email_engine.find_all_emails_by_phone(
                phone, person_name=person_name or None, org_name=org_name or None)
        if not emails:
            emails = self.email_engine.find_all_emails_by_name(
                person_name or org_name, match.get("City", ""), match.get("State", ""),
                org_name=org_name or None)

        lead = dict(match)
        lead["Email"] = emails[0] if emails else ""
        lead["All Emails Found"] = ", ".join(emails)
        lead["Source"] = "ZocDoc (NPI Import)"
        self.attach_verification(lead, person_name=person_name or org_name)
        return lead

    # -------------------------------------------------- enrichment passes

    def find_missing_emails(self):
        """Second pass over every lead that has no email yet."""
        run_id = self.store.start_run("email_find", "")
        pending = list(self.store.iter_leads(missing_email=True))
        if not pending:
            self.reporter.log("Every lead already has an email address.")
            return self._finish(run_id, RUN_OK, 0, "Nothing to do.")

        self.reporter.phase("Finding emails for {0} leads...".format(len(pending)))
        total = len(pending)
        done = 0
        found = 0
        stopped = False

        with ThreadPoolExecutor(max_workers=self.EMAIL_WORKERS) as executor:
            futures = {executor.submit(self._find_email_for, lead): lead
                       for lead in pending}
            for future in as_completed(futures):
                if self.reporter.stopped():
                    stopped = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                lead = futures[future]
                done += 1
                try:
                    updates = future.result()
                except Exception as e:
                    self.reporter.log("Email lookup failed: {0}".format(e), "warning")
                    updates = None
                label = (lead.get("Organization Name")
                         or lead.get("Provider Last Name") or "Unknown")
                if updates and updates.get("Email"):
                    if self.store.update_lead(lead["_id"], updates):
                        found += 1
                        self.reporter.counts(lead["_sheet_key"], emails=1)
                        self.reporter.log("{0}: {1}".format(label, updates["Email"]))
                elif updates:
                    self.store.update_lead(lead["_id"], updates)
                self.reporter.progress(done, total)

        status = RUN_STOPPED if stopped else RUN_OK
        message = "Found {0} new email addresses across {1} leads.".format(found, done)
        self.reporter.log(message)
        return self._finish(run_id, status, found, message)

    def _find_email_for(self, lead):
        """Website discovery, then the practice's own mail server, then a
        corroborated search. Returns the fields to update."""
        working = dict(lead)
        enriched = self.email_engine.find_email(working)
        updates = {}
        for key in ("Email", "Website", "LinkedIn", "Facebook", "Instagram",
                    "Other Social"):
            value = enriched.get(key)
            if value and value != lead.get(key):
                updates[key] = value

        if not updates.get("Email"):
            website = updates.get("Website") or lead.get("Website")
            guessed = self.guess_email(lead.get("Provider First Name"),
                                       lead.get("Provider Last Name"), website)
            if guessed:
                updates["Email"] = guessed

        if updates.get("Email"):
            person = " ".join(filter(None, (lead.get("Provider First Name"),
                                            lead.get("Provider Last Name")))).strip()
            self.attach_verification(updates, person_name=person or lead.get("Organization Name"))
        return updates

    def verify_emails(self, recheck=False):
        """Check every recorded address against its domain's mail server."""
        if not self.verifier:
            return RunResult(RUN_FAILED, message="Verification is not enabled.")

        run_id = self.store.start_run("verify", "recheck" if recheck else "new only")
        pending = list(self.store.iter_leads(has_email=True) if recheck
                       else self.store.iter_leads(unverified=True))
        if not pending:
            self.reporter.log("Every email address has already been checked.")
            return self._finish(run_id, RUN_OK, 0, "Nothing to check.")

        self.reporter.phase("Verifying {0} email addresses...".format(len(pending)))
        total = len(pending)
        done = 0
        tally = {}
        stopped = False

        with ThreadPoolExecutor(max_workers=self.VERIFY_WORKERS) as executor:
            futures = {executor.submit(self._verify_one, lead): lead for lead in pending}
            for future in as_completed(futures):
                if self.reporter.stopped():
                    stopped = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                lead = futures[future]
                done += 1
                try:
                    result = future.result()
                except Exception as e:
                    self.reporter.log("Verification failed for {0}: {1}".format(
                        lead.get("Email"), e), "warning")
                    self.reporter.progress(done, total)
                    continue
                tally[result["status"]] = tally.get(result["status"], 0) + 1
                self.store.update_lead(lead["_id"], {
                    "Email Status": result["status"],
                    "Email Confidence": result["confidence"],
                })
                self.reporter.progress(done, total)

        summary = ", ".join("{0}: {1}".format(status, count)
                            for status, count in sorted(tally.items(),
                                                        key=lambda kv: -kv[1]))
        message = "Checked {0} addresses — {1}".format(done, summary or "no results")
        self.reporter.log(message)
        if self.verifier.smtp_blocked:
            self.reporter.log(
                "Note: this network blocks outbound port 25, so addresses could "
                "only be checked as far as a working mail server. Dead domains "
                "were still removed.", "warning")
        status = RUN_STOPPED if stopped else RUN_OK
        return self._finish(run_id, status, done, message)

    def enrich_from_cms(self, recheck=False):
        """Third pass: attach Medicare billing volume, practice size and the
        phone number each provider filed with Medicare.

        Coverage is partial and that is the source's nature, not a fault — CMS
        only covers Medicare-enrolled providers and suppresses anyone under 11
        beneficiaries. Against this project's own leads that is roughly 29% of
        NPI/ZocDoc providers and almost no Psychology Today therapists. The
        pass therefore records that it ran even when it finds nothing, so those
        leads are not queried again on every future run.
        """
        if not self.cms_engine:
            return RunResult(RUN_FAILED, message="CMS enrichment is not enabled.")

        run_id = self.store.start_run("cms_enrich", "recheck" if recheck else "new only")
        pending = list(self.store.iter_leads() if recheck
                       else self.store.iter_leads(needs_cms=True))
        if not pending:
            self.reporter.log(
                "Every lead with a real NPI number has already been checked "
                "against CMS.")
            return self._finish(run_id, RUN_OK, 0, "Nothing to do.")

        self.reporter.phase(
            "Looking up {0} providers in CMS Medicare data...".format(len(pending)))
        total = len(pending)
        done = 0
        with_billing = 0
        with_practice = 0
        phones_confirmed = 0
        phones_added = 0
        stopped = False

        with ThreadPoolExecutor(max_workers=self.CMS_WORKERS) as executor:
            futures = {executor.submit(self._cms_updates_for, lead): lead
                       for lead in pending}
            for future in as_completed(futures):
                if self.reporter.stopped():
                    stopped = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                lead = futures[future]
                done += 1
                try:
                    updates = future.result()
                except Exception as e:
                    self.reporter.log("CMS lookup failed for NPI {0}: {1}".format(
                        lead.get("NPI Number"), e), "warning")
                    self.reporter.progress(done, total)
                    continue

                if updates.get("Annual Billing"):
                    with_billing += 1
                if updates.get("Practice Size"):
                    with_practice += 1
                verified = updates.get("Phone Verified", "")
                if verified == storage.PHONE_CONFIRMED:
                    phones_confirmed += 1
                elif verified == storage.PHONE_FROM_CMS:
                    phones_added += 1

                self.store.update_lead(lead["_id"], updates)
                self.reporter.progress(done, total)

        message = (
            "Checked {0} providers — {1} had Medicare billing data, {2} had a "
            "practice size, {3} phone numbers confirmed against federal records, "
            "{4} filled in from them.".format(
                done, with_billing, with_practice, phones_confirmed, phones_added))
        self.reporter.log(message)
        if done and not with_billing:
            self.reporter.log(
                "No billing data at all is normal for therapist leads — most "
                "are cash-pay or commercial-insurance only and never appear in "
                "Medicare data.")
        status = RUN_STOPPED if stopped else RUN_OK
        return self._finish(run_id, status, with_billing + with_practice, message)

    def _cms_updates_for(self, lead):
        """Fields to write for one provider. Always returns a Phone Verified
        value so the pass is recorded as having run."""
        npi = str(lead.get("NPI Number") or "")
        billing = self.cms_engine.billing_for(npi)
        enrollment = self.cms_engine.enrollment_for(npi)

        updates = {}
        if billing.get("annual_billing") is not None:
            updates["Annual Billing"] = billing["annual_billing"]
        if billing.get("medicare_paid") is not None:
            updates["Medicare Paid"] = billing["medicare_paid"]
        if billing.get("patient_volume") is not None:
            updates["Patient Volume"] = billing["patient_volume"]
        if enrollment.get("practice_size") is not None:
            updates["Practice Size"] = enrollment["practice_size"]

        # A practice name from federal enrollment beats an empty column, but
        # never overwrites a name already collected from the provider's own
        # listing — that one is what they call themselves.
        if enrollment.get("practice_name") and not lead.get("Organization Name"):
            updates["Organization Name"] = enrollment["practice_name"]

        updates.update(self._phone_verdict(lead, enrollment))
        return updates

    @staticmethod
    def _phone_verdict(lead, enrollment):
        """Compare the phone we hold against the one filed with Medicare.

        A match is the strongest free corroboration there is: the provider gave
        that number to the federal government themselves. A mismatch is *not*
        treated as an error — a scraped direct line is often better than the
        billing office number on file — it is simply labelled so a caller knows
        which is which.
        """
        filed = enrollment.get("filed_phone") or ""
        if not filed:
            return {"Phone Verified": storage.PHONE_NO_RECORD}

        formatted = "({0}) {1}-{2}".format(filed[0:3], filed[3:6], filed[6:10])
        current = storage.normalize_phone_key(lead.get("Phone"))
        if not current:
            return {"Phone": formatted,
                    "Phone Source": storage.PHONE_FROM_CMS,
                    "Phone Verified": storage.PHONE_FROM_CMS}
        if current == filed:
            return {"Phone Verified": storage.PHONE_CONFIRMED}
        return {"Phone Verified": storage.PHONE_DIFFERS}

    def recipients_for(self, limit=50, icp_only=False, owners_only=False,
                       states=None, allow_unverified=False, skip_contacted=True):
        """Who a campaign would actually go to, and why the rest were left out.

        Returns (recipients, excluded) where excluded is a {reason: count} tally
        — shown before sending, because "50 of 534" is alarming until you can
        see that 255 simply have no email address.
        """
        excluded = {}

        def drop(reason):
            excluded[reason] = excluded.get(reason, 0) + 1

        # Loaded once rather than queried per candidate. Picking 25 recipients
        # out of a 20,000-lead database used to cost 40,000 round trips.
        suppressed = self.store.suppressed_values()
        contacted_emails = self.store.contacted_emails() if skip_contacted else set()
        contacted_ids = self.store.contacted_lead_ids() if skip_contacted else set()

        recipients = []
        for lead in self.store.iter_best_leads(icp_only=icp_only,
                                               owners_only=owners_only,
                                               states=states):
            email = (lead.get("Email") or "").strip()
            if not email:
                drop("no email address")
                continue
            email_key = email.lower()

            status = str(lead.get("Email Status") or "").strip().lower()
            if status in outreach.NEVER_SEND_STATUSES:
                drop("address would bounce or isn't theirs ({0})".format(status))
                continue
            if status not in outreach.SENDABLE_STATUSES and not allow_unverified:
                drop("address not verified yet")
                continue

            domain = "@" + email_key.split("@", 1)[1] if "@" in email_key else ""
            if email_key in suppressed or (domain and domain in suppressed):
                drop("opted out or previously bounced")
                continue
            if email_key in contacted_emails or lead["_id"] in contacted_ids:
                drop("already contacted")
                continue

            recipients.append(lead)
            if limit and len(recipients) >= limit:
                break

        return recipients, excluded

    def send_campaign(self, subject_template, body_template, campaign="",
                      limit=50, daily_cap=50, icp_only=False, owners_only=False,
                      states=None, allow_unverified=False, dry_run=False,
                      min_seconds=45, max_seconds=90):
        """Send one campaign, slowly, to people who have not heard from you.

        Refuses to start if the compliance settings are incomplete: commercial
        email without a postal address and a working opt-out is unlawful in the
        US, and the liability is the sender's.
        """
        problems = outreach.compliance_problems(self.settings)
        if problems and not dry_run:
            message = "Cannot send yet — " + " ".join(problems)
            self.reporter.log(message, "error")
            return RunResult(RUN_FAILED, message=message)

        missing = (outreach.unresolved_fields(subject_template)
                   + outreach.unresolved_fields(body_template))
        if missing:
            message = ("These merge fields don't exist and would go out as raw "
                       "text: " + ", ".join(sorted(set(missing))))
            self.reporter.log(message, "error")
            return RunResult(RUN_FAILED, message=message)

        # The cap counts everything sent today, not just this campaign, so two
        # campaigns in one afternoon cannot quietly double the day's volume.
        start_of_day = _utc_start_of_day()
        already_today = self.store.sent_since(start_of_day)
        remaining_today = max(0, int(daily_cap) - already_today)
        if remaining_today <= 0 and not dry_run:
            message = ("The daily cap of {0} has already been reached ({1} sent "
                       "today). This protects your sending reputation — raise "
                       "the cap in Settings only if the domain is warmed up."
                       .format(daily_cap, already_today))
            self.reporter.log(message, "warning")
            return RunResult(RUN_STOPPED, message=message)

        effective_limit = min(limit, remaining_today) if not dry_run else limit
        recipients, excluded = self.recipients_for(
            limit=effective_limit, icp_only=icp_only, owners_only=owners_only,
            states=states, allow_unverified=allow_unverified)

        for reason, count in sorted(excluded.items(), key=lambda kv: -kv[1]):
            self.reporter.log("  {0} skipped — {1}".format(count, reason))

        if not recipients:
            message = "Nobody to send to. " + (
                "Run email discovery and verification first."
                if excluded else "Every matching lead has already been contacted.")
            self.reporter.log(message, "warning")
            return RunResult(RUN_EMPTY, message=message)

        if dry_run:
            self.reporter.log(
                "DRY RUN — nothing will be sent. {0} recipient(s) would receive:"
                .format(len(recipients)))
            for lead in recipients[:3]:
                self.reporter.log("\n  To: {0}\n  Subject: {1}\n\n{2}\n".format(
                    lead["Email"],
                    outreach.render(subject_template, lead),
                    outreach.render(body_template, lead)
                    + outreach.build_footer(self.settings)))
            return RunResult(RUN_OK, added=0,
                             message="Dry run: {0} would be sent.".format(len(recipients)))

        run_id = self.store.start_run("outreach", campaign or "campaign")
        self.reporter.phase("Sending to {0} recipients, slowly...".format(len(recipients)))
        if allow_unverified:
            self.reporter.log(
                "Including addresses that could not be verified — expect some "
                "bounces. Bounced addresses are suppressed automatically.",
                "warning")

        sender = outreach.EmailSender(self.settings, logger=self.logger)
        throttle = outreach.Throttle(min_seconds, max_seconds,
                                     stop_event=self.reporter.stop_event)
        sent = failed = 0
        stopped = False

        try:
            for index, lead in enumerate(recipients, 1):
                if self.reporter.stopped():
                    stopped = True
                    break
                if not throttle.wait():
                    stopped = True
                    break

                email = lead["Email"]
                subject = outreach.render(subject_template, lead)
                body = outreach.render(body_template, lead)
                ok, detail = sender.send(email, subject, body)

                if ok:
                    sent += 1
                    self.store.record_send(lead["_id"], email, campaign, subject,
                                           "sent")
                    self.reporter.log("[{0}/{1}] sent to {2}".format(
                        index, len(recipients), email))
                else:
                    failed += 1
                    self.store.record_send(lead["_id"], email, campaign, subject,
                                           "failed", detail)
                    self.reporter.log("[{0}/{1}] FAILED {2} — {3}".format(
                        index, len(recipients), email, detail), "warning")
                    # Only a permanent rejection means the address is dead. A
                    # busy server must not cost us the lead forever.
                    if outreach.is_permanent_failure(detail):
                        self.store.suppress(email, "hard bounce: " + detail[:120])
                        self.reporter.log(
                            "  {0} suppressed — it will not be tried again.".format(email))
                self.reporter.progress(index, len(recipients))
        finally:
            sender.close()

        message = "Sent {0}, failed {1}.".format(sent, failed)
        if stopped:
            message = "Stopped. " + message
        self.reporter.log(message)
        return self._finish(run_id, RUN_STOPPED if stopped else RUN_OK, sent, message)

    def _verify_one(self, lead):
        person = " ".join(filter(None, (lead.get("Provider First Name"),
                                        lead.get("Provider Last Name")))).strip()
        return self.verifier.verify(lead.get("Email"), person_name=person or None)

    # ---------------------------------------------------- shared helpers

    def guess_email(self, first_name, last_name, website):
        """Infer an address at the practice's own domain, confirmed by their
        mail server. Returns "" when it cannot be confirmed — see
        EmailVerifier.guess_addresses for why guessing without confirmation is
        not worth doing."""
        if not (self.verifier and website and first_name):
            return ""
        if self.email_engine.is_platform_site(website):
            return ""  # a marketplace domain is not the provider's practice
        domain = self.email_engine._registrable_domain(urlparse(website).netloc)
        if not domain:
            return ""
        try:
            candidates = self.verifier.guess_addresses(first_name, last_name, domain)
        except Exception as e:
            self.reporter.log("Address inference failed for {0}: {1}".format(domain, e),
                              "debug")
            return ""
        for candidate in candidates:
            if not self.store.email_exists(candidate):
                return candidate
        return ""

    def attach_verification(self, lead, person_name=None, note=""):
        """Stamp Email Status / Email Confidence onto a lead in place.

        Done as leads are collected, not only in a later pass, so the status is
        already there the first time anyone opens the export. An address the
        mail server rejects is dropped rather than saved with a warning label —
        exporting a known-bad address helps nobody.
        """
        email = lead.get("Email")
        if not email:
            return lead
        if not self.verifier:
            return lead
        try:
            result = self.verifier.verify(email, person_name=person_name)
        except Exception as e:
            self.reporter.log("Could not verify {0}: {1}".format(email, e), "debug")
            return lead

        if result["status"] in (verification.INVALID, verification.DISPOSABLE):
            self.reporter.log("Discarding {0} — {1}".format(email, result["detail"]))
            lead["Email"] = ""
            lead["Email Status"] = ""
            lead["Email Confidence"] = ""
            return lead

        lead["Email Status"] = result["status"]
        lead["Email Confidence"] = result["confidence"]
        if note:
            lead.setdefault("Phone Source", lead.get("Phone Source", ""))
        return lead
