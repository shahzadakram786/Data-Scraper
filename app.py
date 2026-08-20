"""
Medical Provider Lead Generation System
---------------------------------------
A complete automated tool to collect medical provider data from the NPI Registry
and find contact information using public sources. Supports Excel and Google Sheets.

INSTALLATION:
1. Ensure Python 3.7+ is installed.
2. Run: pip install requests openpyxl beautifulsoup4 duckduckgo-search gspread oauth2client lxml
3. Run this file: python app.py

USAGE:
1. Select State and Specialty.
2. Choose Output Destination (Excel or Google Sheets).
3. Click 'Collect Provider Data'.
4. Click 'Find Emails' to discover contact details.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import threading
import logging
import os
import json
import re
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from npi_engine import NPIEngine
from email_engine import EmailEngine
from psychology_today_engine import PsychologyTodayEngine
from lead_manager import ExcelManager, GoogleSheetsManager

class TextHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(tk.END, msg + '\n')
            self.text_widget.see(tk.END)
            self.text_widget.configure(state='disabled')
        self.text_widget.after(0, append)

class LeadGenApp:
    STATES = {
        "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
        "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
        "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
        "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
        "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
        "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
        "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
        "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
        "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
        "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
        "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
        "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
        "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY", "Puerto Rico": "PR"
    }
    
    SPECIALTIES = {
        "Family Medicine": "Family Medicine",
        "Internal Medicine": "Internal Medicine",
        "Pediatrics": "Pediatrics",
        "Psychiatry": "Psychiatry",
        "Psychologist": "Psychologist",
        "Clinical Social Worker": "Clinical Social Worker",
        "Nurse Practitioner": "Nurse Practitioner",
        "Nurse Practitioner - Family": "Nurse Practitioner, Family",
        "Nurse Practitioner - Psychiatric/Mental Health": "Nurse Practitioner, Psychiatric/Mental Health",
        "Mental Health Counselor": "Counselor, Mental Health",
        "Mental Health Clinic": "Clinic/Center, Mental Health (Including Community Mental Health Center)",
        "Orthopedics": "Orthopaedic Surgery",
        "Cardiology": "Cardiovascular Disease",
        "OB-GYN": "Obstetrics & Gynecology",
        "Physical Therapy": "Physical Therapist",
        "Chiropractic": "Chiropractor",
        "Urgent Care": "Clinic/Center, Urgent Care",
        "General Practice": "General Practice",
        "Neurology": "Neurology",
        "Dermatology": "Dermatology",
        "Gastroenterology": "Gastroenterology"
    }

    RUN_HISTORY_FILE = "run_history.json"
    EMAIL_WORKERS = 5  # Concurrent email lookups; EmailEngine self-throttles if DDGS rate-limits.
    PT_WORKERS = 3  # Concurrent Psychology Today profile lookups; each one is several HTTP calls
                    # (profile page, website redirect, NPI cross-reference, phone fallback search),
                    # so kept lower than EMAIL_WORKERS to stay polite to all the sites involved.

    def __init__(self, root):
        self.root = root
        self.root.title("Medical Provider Lead Generation System")
        self.root.geometry("850x780")

        self.npi_engine = NPIEngine()
        self.email_engine = EmailEngine()
        self.pt_engine = PsychologyTodayEngine()
        self.lead_manager = None
        self.stop_event = threading.Event()

        self.setup_ui()
        self.setup_logging()

    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Filters Frame
        filter_frame = ttk.LabelFrame(main_frame, text="Search Filters", padding="10")
        filter_frame.pack(fill=tk.X, pady=5)

        ttk.Label(filter_frame, text="Select State:").grid(row=0, column=0, sticky=tk.W, padx=5)
        self.state_var = tk.StringVar(value="All States")
        self.state_cb = ttk.Combobox(filter_frame, textvariable=self.state_var, width=30)
        self.state_cb['values'] = ["All States"] + list(self.STATES.keys())
        self.state_cb.grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(filter_frame, text="Select Specialty:").grid(row=1, column=0, sticky=tk.W, padx=5)
        self.specialty_var = tk.StringVar(value="All Specialties")
        self.specialty_cb = ttk.Combobox(filter_frame, textvariable=self.specialty_var, width=30)
        self.specialty_cb['values'] = ["All Specialties"] + list(self.SPECIALTIES.keys())
        self.specialty_cb.grid(row=1, column=1, padx=5, pady=5)

        # New filters: Min year and corporate suffix check
        ttk.Label(filter_frame, text="Min Last Updated Year:").grid(row=2, column=0, sticky=tk.W, padx=5)
        self.min_year_var = tk.StringVar(value="2010")
        self.min_year_cb = ttk.Combobox(filter_frame, textvariable=self.min_year_var, width=30)
        self.min_year_cb['values'] = ["No Filter", "2005", "2010", "2015", "2018", "2020", "2022", "2024"]
        self.min_year_cb.grid(row=2, column=1, padx=5, pady=5)

        self.exclude_corp_var = tk.BooleanVar(value=True)
        self.exclude_corp_chk = ttk.Checkbutton(filter_frame, text="Exclude Corporate Suffixes (Inc, Corp, Corporation)", variable=self.exclude_corp_var)
        self.exclude_corp_chk.grid(row=3, column=0, columnspan=2, sticky=tk.W, padx=5, pady=5)

        # Decision-maker filter: only keep providers whose own website presents
        # them as running the practice. Applies to Psychology Today collection,
        # which is the flow that visits each provider's website — the NPI
        # registry publishes no ownership information.
        self.owners_only_var = tk.BooleanVar(value=False)
        self.owners_only_chk = ttk.Checkbutton(
            filter_frame,
            text="Owners / CEOs / Founders only (Psychology Today)",
            variable=self.owners_only_var)
        self.owners_only_chk.grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=5, pady=5)

        # Output Destination Frame
        output_frame = ttk.LabelFrame(main_frame, text="Output Destination", padding="10")
        output_frame.pack(fill=tk.X, pady=5)

        self.output_type = tk.StringVar(value="Excel")
        ttk.Radiobutton(output_frame, text="Local Excel (.xlsx)", variable=self.output_type, value="Excel", command=self.toggle_gs_settings).grid(row=0, column=0, padx=5, sticky=tk.W)
        ttk.Radiobutton(output_frame, text="Google Sheets (Cloud)", variable=self.output_type, value="Google", command=self.toggle_gs_settings).grid(row=0, column=1, padx=5, sticky=tk.W)

        # GS Settings Sub-frame
        self.gs_frame = ttk.Frame(output_frame)
        self.gs_frame.grid(row=1, column=0, columnspan=2, pady=5, sticky=tk.W)
        
        ttk.Label(self.gs_frame, text="Service Account JSON:").grid(row=0, column=0, padx=5, sticky=tk.W)
        self.gs_creds_var = tk.StringVar()
        self.gs_creds_entry = ttk.Entry(self.gs_frame, textvariable=self.gs_creds_var, width=40)
        self.gs_creds_entry.grid(row=0, column=1, padx=5)
        ttk.Button(self.gs_frame, text="Browse", command=self.browse_json).grid(row=0, column=2, padx=5)

        ttk.Label(self.gs_frame, text="Spreadsheet Name:").grid(row=1, column=0, padx=5, sticky=tk.W)
        self.gs_name_var = tk.StringVar(value="RCM_Provider_Leads")
        ttk.Entry(self.gs_frame, textvariable=self.gs_name_var, width=40).grid(row=1, column=1, padx=5)

        self.toggle_gs_settings()

        # ZocDoc Import Frame (ZocDoc itself blocks automated scraping, so this
        # takes NPI numbers you've copied by hand from ZocDoc profiles and
        # automates everything after that: NPI lookup, phone-based email
        # search, and a full-name search fallback.)
        zocdoc_frame = ttk.LabelFrame(main_frame, text="Import NPIs (e.g. copied from ZocDoc)", padding="10")
        zocdoc_frame.pack(fill=tk.X, pady=5)
        ttk.Label(zocdoc_frame, text="Paste NPI numbers below — one per line, or any copied text containing them:").pack(anchor=tk.W)
        self.npi_paste_text = scrolledtext.ScrolledText(zocdoc_frame, height=4)
        self.npi_paste_text.pack(fill=tk.X, pady=(4, 5))
        self.zocdoc_btn = ttk.Button(zocdoc_frame, text="Collect from Pasted NPIs", command=self.start_zocdoc_collection)
        self.zocdoc_btn.pack(anchor=tk.W)

        # Buttons Frame
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(fill=tk.X, pady=10)

        self.collect_btn = ttk.Button(btn_frame, text="Collect Provider Data", command=self.start_collection)
        self.collect_btn.pack(side=tk.LEFT, padx=5)

        self.pt_btn = ttk.Button(btn_frame, text="Collect from Psychology Today", command=self.start_pt_collection)
        self.pt_btn.pack(side=tk.LEFT, padx=5)

        self.email_btn = ttk.Button(btn_frame, text="Find Emails", command=self.start_email_finding)
        self.email_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.stop_current_run, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        self.export_btn = ttk.Button(btn_frame, text="Open Result", command=self.open_result)
        self.export_btn.pack(side=tk.LEFT, padx=5)

        self.summary_btn = ttk.Button(btn_frame, text="View Summary", command=self.show_summary)
        self.summary_btn.pack(side=tk.LEFT, padx=5)

        self.dashboard_btn = ttk.Button(btn_frame, text="Dashboard", command=self.show_dashboard)
        self.dashboard_btn.pack(side=tk.LEFT, padx=5)

        # Progress
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(main_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=5)

        # Live Results Panel — one row per sheet tab ("Texas - Psychology
        # Today", "Ohio - NPI Registry", ...), mirroring the workbook's tabs so
        # the panel and the spreadsheet stay easy to cross-reference.
        live_frame = ttk.LabelFrame(main_frame, text="Live Results by Sheet (State - Source)", padding="5")
        live_frame.pack(fill=tk.X, pady=5)

        self.live_total_var = tk.StringVar(value="Total: 0 leads, 0 emails (0.0%)")
        ttk.Label(live_frame, textvariable=self.live_total_var, font=("TkDefaultFont", 9, "bold")).pack(anchor=tk.W, pady=(0, 5))

        self.live_tree = ttk.Treeview(live_frame, columns=("leads", "emails", "rate"), show="tree headings", height=5)
        self.live_tree.heading("#0", text="State - Source")
        self.live_tree.heading("leads", text="Leads")
        self.live_tree.heading("emails", text="Emails")
        self.live_tree.heading("rate", text="Hit Rate")
        self.live_tree.column("#0", width=170)
        self.live_tree.column("leads", width=80, anchor=tk.CENTER)
        self.live_tree.column("emails", width=80, anchor=tk.CENTER)
        self.live_tree.column("rate", width=80, anchor=tk.CENTER)
        self.live_tree.pack(fill=tk.X)

        self.source_totals = {}   # source name -> {"leads": int, "emails": int}
        self.live_tree_items = {}  # source name -> Treeview item id

        # Log
        log_frame = ttk.LabelFrame(main_frame, text="Status Log", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.log_text = scrolledtext.ScrolledText(log_frame, state='disabled', height=8)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def toggle_gs_settings(self):
        if self.output_type.get() == "Google":
            for child in self.gs_frame.winfo_children():
                if isinstance(child, (ttk.Entry, ttk.Button)):
                    child.configure(state=tk.NORMAL)
        else:
            for child in self.gs_frame.winfo_children():
                if isinstance(child, (ttk.Entry, ttk.Button)):
                    child.configure(state=tk.DISABLED)

    def browse_json(self):
        filename = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if filename:
            self.gs_creds_var.set(filename)

    def setup_logging(self):
        self.logger = logging.getLogger("LeadGen")
        self.logger.setLevel(logging.INFO)
        handler = TextHandler(self.log_text)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%H:%M:%S')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)

    def get_manager(self):
        if self.output_type.get() == "Excel":
            return ExcelManager()
        else:
            creds = self.gs_creds_var.get()
            name = self.gs_name_var.get()
            if not creds or not os.path.exists(creds):
                raise Exception("Please select a valid Google Service Account JSON file.")
            return GoogleSheetsManager(creds, name)

    def stop_current_run(self):
        self.stop_event.set()
        self.logger.info("Stop requested — finishing current item(s) then halting...")
        self.stop_btn.config(state=tk.DISABLED)

    def _refresh_live_total_label(self):
        total_leads = sum(s["leads"] for s in self.source_totals.values())
        total_emails = sum(s["emails"] for s in self.source_totals.values())
        rate = (total_emails / total_leads * 100) if total_leads else 0.0
        self.live_total_var.set(f"Total: {total_leads} leads, {total_emails} emails ({rate:.1f}%)")

    def _refresh_live_row(self, source):
        s = self.source_totals[source]
        rate = (s["emails"] / s["leads"] * 100) if s["leads"] else 0.0
        values = (s["leads"], s["emails"], f"{rate:.1f}%")
        if source in self.live_tree_items:
            self.live_tree.item(self.live_tree_items[source], values=values)
        else:
            self.live_tree_items[source] = self.live_tree.insert("", tk.END, text=source, values=values)

    def _seed_live_stats(self, stats):
        """Must run on the main thread (via root.after). Populates the live panel
        from an existing get_stats() snapshot so a run against a pre-populated
        file shows correct running totals instead of starting at zero."""
        self.source_totals = {}
        self.live_tree_items = {}
        for item in self.live_tree.get_children():
            self.live_tree.delete(item)
        for source, s in stats.get("by_source", {}).items():
            self.source_totals[source] = {"leads": s["total"], "emails": s["with_email"]}
            self._refresh_live_row(source)
        self._refresh_live_total_label()

    def _update_live_stats(self, source, leads_delta=0, emails_delta=0):
        """Must run on the main thread (via root.after)."""
        if source not in self.source_totals:
            self.source_totals[source] = {"leads": 0, "emails": 0}
        self.source_totals[source]["leads"] += leads_delta
        self.source_totals[source]["emails"] += emails_delta
        self._refresh_live_row(source)
        self._refresh_live_total_label()

    def start_collection(self):
        try:
            self.lead_manager = self.get_manager()
            state = self.state_var.get()
            specialty = self.specialty_var.get()

            # Read new filters
            exclude_corp = self.exclude_corp_var.get()
            min_year_str = self.min_year_var.get()
            min_year = None
            if min_year_str != "No Filter":
                try:
                    min_year = int(min_year_str)
                except ValueError:
                    pass

            self.stop_event = threading.Event()
            self.collect_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            threading.Thread(target=self.collection_worker, args=(state, specialty, exclude_corp, min_year), daemon=True).start()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def collection_worker(self, state_name, specialty_name, exclude_corp, min_year):
        try:
            states = [self.STATES[state_name]] if state_name != "All States" else list(self.STATES.values())

            # Map selected specialty name to official taxonomy descriptions
            if specialty_name != "All Specialties":
                specs = [self.SPECIALTIES[specialty_name]]
            else:
                specs = list(self.SPECIALTIES.values())

            total = len(states) * len(specs)
            done = 0
            stopped = False

            try:
                seed_stats = self.lead_manager.get_stats()
                self.root.after(0, lambda s=seed_stats: self._seed_live_stats(s))
            except Exception:
                pass

            for s_abbr in states:
                if self.stop_event.is_set():
                    stopped = True
                    break
                for spec in specs:
                    if self.stop_event.is_set():
                        stopped = True
                        break
                    self.logger.info(f"Fetching {spec} in {s_abbr}...")
                    providers = self.npi_engine.fetch_providers(
                        s_abbr, spec, exclude_corp=exclude_corp, min_updated_year=min_year,
                        stop_event=self.stop_event
                    )
                    if providers:
                        counts = self.lead_manager.add_leads_batch(providers)
                        self.logger.info(f"Added {sum(counts.values())} leads to {self.output_type.get()}.")
                        for st, c in counts.items():
                            self.root.after(0, lambda st=st, c=c: self._update_live_stats(st, leads_delta=c))
                    done += 1
                    self.root.after(0, lambda d=done: self.progress_var.set((d / total) * 100))

            if stopped:
                self.logger.info("Collection stopped by user.")
            else:
                self.logger.info("Collection finished.")
            try:
                self._record_stats("collect", self.lead_manager.get_stats())
            except Exception:
                pass
            if stopped:
                self.root.after(0, lambda: messagebox.showinfo("Stopped", "Collection stopped by user."))
            else:
                self.root.after(0, lambda: messagebox.showinfo("Success", "Collection finished."))
        except Exception as e:
            self.logger.error(f"Error: {e}" + chr(10) + traceback.format_exc())
            self.root.after(0, lambda err=e: messagebox.showerror("Error", str(err)))
        finally:
            self.collect_btn.after(0, lambda: self.collect_btn.config(state=tk.NORMAL))
            self.stop_btn.after(0, lambda: self.stop_btn.config(state=tk.DISABLED))

    def start_pt_collection(self):
        try:
            state = self.state_var.get()
            if state == "All States":
                messagebox.showerror(
                    "Error",
                    "Please select a specific state for Psychology Today collection.\n"
                    "Scraping every state at once would take a very long time.")
                return
            self.lead_manager = self.get_manager()
            self.stop_event = threading.Event()
            self.pt_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            # Read the filter here, on the main thread, rather than from the
            # worker — Tk variables aren't safe to touch off the UI thread.
            owners_only = self.owners_only_var.get()
            threading.Thread(target=self.pt_collection_worker,
                             args=(state, owners_only), daemon=True).start()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    @staticmethod
    def _split_pt_name(full_name):
        """Best-effort split of a Psychology Today display name into first/last,
        for the NPI individual lookup and the lead sheet columns. Drops
        parenthetical maiden/alternate names, e.g. 'Shelby Scott (Gregory)'."""
        cleaned = re.sub(r'\([^)]*\)', '', full_name or '').strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)
        if not cleaned:
            return "", ""
        parts = cleaned.split(' ')
        if len(parts) == 1:
            return parts[0], ""
        return " ".join(parts[:-1]), parts[-1]

    def _pick_authentic_phone(self, website_contact, entry):
        """Chooses the number most likely to reach this provider personally.

        For Psychology Today leads the number comes from the provider's **own
        website** — deliberately not from the NPI registry, whose number is the
        practice's registered administrative/billing line and frequently a
        different office altogether. The website scrape already prefers a
        number the site labels as a cell or direct line over the shared office
        line, and discards fax and crisis numbers.

        Psychology Today's own listed number is the last resort: it is usually
        a call-tracking number that forwards, not the provider's real line, so
        it is labelled plainly as such. Returns ("", "") rather than guessing
        when nothing dialable was found.
        """
        site_phone = website_contact.get("phone", "")
        if site_phone and self.email_engine.is_valid_phone(site_phone):
            conf = website_contact.get("phone_confidence", "")
            return site_phone, f"Provider Website ({conf})" if conf else "Provider Website"

        pt_phone = entry.get("phone", "")
        if pt_phone and self.email_engine.is_valid_phone(pt_phone):
            source = "Psychology Today (listing)"
            if self.email_engine.is_tollfree(pt_phone):
                source += " (toll-free)"
            return pt_phone, source
        return "", ""

    def _process_pt_entry(self, entry, state_abbr):
        """Runs the full manual workflow for one Psychology Today provider:
        1. Open the profile; if it lists a website, follow it and scrape it for
           an email and a phone number in the same pass.
        2. Cross-reference the provider against the NPI registry by name/org + state
           to attach a real NPI number (skipped if the match isn't unambiguous).
        3. If still no email, fall back to searching the phone number directly —
           the same "paste the number into the browser" trick used manually.
        Returns a lead dict matching the shared spreadsheet schema.

        The Phone column is taken from the provider's own website, preferring a
        number the site presents as their direct/cell line, and falling back to
        Psychology Today's listing only when the site yields nothing. The NPI
        registry is used to confirm identity (NPI number, address) but **not**
        for the phone — its number is the registered administrative line, often
        a different office entirely. "Phone Source" records what was used.

        "Role" is the ownership title the provider's site gives them (Owner,
        Founder, CEO, ...), or "" if the site doesn't present them as running
        the practice.
        """
        details = self.pt_engine.get_profile_details(entry["pt_id"], entry["profile_url"])
        website = details.get("website", "")
        first_name, last_name = ("", "") if entry["is_org"] else self._split_pt_name(entry["name"])

        website_contact = (
            self.email_engine.scrape_website_contact_info(website, person_name=entry.get("name"))
            if website else
            {"email": "", "phone": "", "phone_confidence": "", "role": ""}
        )
        email = website_contact.get("email", "")
        role = website_contact.get("role", "")

        npi_match = None
        try:
            if entry["is_org"]:
                npi_match = self.npi_engine.find_by_organization(entry["name"], state_abbr, city=entry.get("city"))
            elif first_name and last_name:
                npi_match = self.npi_engine.find_by_name(first_name, last_name, state_abbr, city=entry.get("city"))
        except Exception as e:
            self.logger.warning(f"NPI cross-reference failed for {entry.get('name')}: {e}")

        phone, phone_source = self._pick_authentic_phone(website_contact, entry)

        if not email:
            email = self.email_engine.find_email_by_phone(phone)

        return {
            "Role": role,
            "NPI Number": npi_match["NPI Number"] if npi_match else f"PT-{entry['pt_id']}",
            "Provider First Name": first_name,
            "Provider Last Name": last_name,
            "Organization Name": entry["name"] if entry["is_org"] else (npi_match.get("Organization Name", "") if npi_match else ""),
            "Phone": phone,
            "Email": email,
            "Address": npi_match.get("Address", "") if npi_match else "",
            "City": entry.get("city") or (npi_match.get("City", "") if npi_match else ""),
            "State": state_abbr,
            "Zip": entry.get("zip") or (npi_match.get("Zip", "") if npi_match else ""),
            "Specialty": details.get("specialty", "") or (npi_match.get("Specialty", "") if npi_match else ""),
            "Website": website,
            "Outreach Status": "Not Contacted",
            "Last Updated": "",
            "Source": "Psychology Today",
            "Phone Source": phone_source,
        }

    def _flush_leads(self, leads):
        """Writes a batch of leads and updates the live panel. Returns the
        leads still unsaved: a failed write (most often the workbook being open
        in Excel, which locks the file on Windows) keeps them buffered for the
        next attempt instead of aborting the whole run and throwing away
        everything scraped so far."""
        if not leads:
            return []
        try:
            counts = self.lead_manager.add_leads_batch(leads)
        except Exception as e:
            self.logger.warning(
                f"Could not save {len(leads)} leads yet ({e}). "
                f"If the workbook is open in Excel, close it — will retry.")
            return leads
        for st, c in counts.items():
            self.root.after(0, lambda st=st, c=c: self._update_live_stats(st, leads_delta=c))
        return []

    def pt_collection_worker(self, state_name, owners_only=False):
        stopped = False
        skipped_not_owner = 0
        try:
            state_abbr = self.STATES[state_name]
            self.logger.info(f"Searching Psychology Today for therapists in {state_name}...")
            if owners_only:
                self.logger.info(
                    "Owners/CEOs/Founders filter is ON — only providers whose own "
                    "website presents them as running the practice will be saved.")
            try:
                seed_stats = self.lead_manager.get_stats()
                self.root.after(0, lambda s=seed_stats: self._seed_live_stats(s))
            except Exception:
                pass

            # Show the Psychology Today row straight away, at zero, so it's
            # visible that this source is running rather than leaving the panel
            # showing only the sources already in the file.
            self.root.after(0, lambda: self._update_live_stats("Psychology Today", leads_delta=0))

            # Providers are processed as each listing page arrives rather than
            # after the whole directory has been paged through: a large state
            # is 50+ listing pages, and waiting for all of them before writing
            # anything made the run look like it was doing nothing for minutes.
            done = 0
            queued = 0
            leads_buffer = []

            with ThreadPoolExecutor(max_workers=self.PT_WORKERS) as executor:
                pending = set()

                def handle_completed(futures_done):
                    nonlocal done, leads_buffer, skipped_not_owner
                    for future in futures_done:
                        entry = future_to_entry.pop(future, {})
                        done += 1
                        try:
                            lead = future.result()
                        except Exception as e:
                            self.logger.warning(f"Failed to process {entry.get('name')}: {e}")
                            continue
                        if owners_only and not self.email_engine.is_ownership_role(lead.get("Role")):
                            skipped_not_owner += 1
                            self.logger.info(
                                f"[{done}/{queued}] {entry.get('name')} — skipped "
                                f"(not listed as owner/CEO/founder)")
                            self.root.after(0, lambda d=done, t=queued: self.progress_var.set((d / t) * 100 if t else 0))
                            continue
                        leads_buffer.append(lead)
                        label = f"{lead.get('Provider First Name', '')} {lead.get('Provider Last Name', '')}".strip() or lead.get("Organization Name", "Unknown")
                        self.logger.info(f"[{done}/{queued}] {label} — {'email found' if lead.get('Email') else 'no email'}")
                        self.root.after(0, lambda d=done, t=queued: self.progress_var.set((d / t) * 100 if t else 0))
                        if len(leads_buffer) >= 5:
                            leads_buffer = self._flush_leads(leads_buffer)

                future_to_entry = {}
                for batch_entries in self.pt_engine.iter_listing(state_name, stop_event=self.stop_event):
                    if self.stop_event.is_set():
                        stopped = True
                        break
                    for entry in batch_entries:
                        fut = executor.submit(self._process_pt_entry, entry, state_abbr)
                        future_to_entry[fut] = entry
                        pending.add(fut)
                        queued += 1
                    self.logger.info(f"Queued {queued} Psychology Today providers so far; {done} processed.")
                    # Collect whatever has finished while the next page loads,
                    # without blocking the listing scrape.
                    finished = {f for f in pending if f.done()}
                    pending -= finished
                    handle_completed(finished)

                if not stopped:
                    self.logger.info(f"Listing complete: {queued} providers. Finishing profile lookups...")
                for future in as_completed(list(pending)):
                    pending.discard(future)
                    handle_completed({future})
                    if self.stop_event.is_set():
                        stopped = True
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

            if leads_buffer:
                leads_buffer = self._flush_leads(leads_buffer)
                if leads_buffer:
                    self.logger.error(
                        f"{len(leads_buffer)} scraped leads could not be saved. "
                        f"Close the workbook in Excel and run again.")

            # Re-seed from the file so the "emails" column reflects only leads
            # actually inserted (add_leads_batch silently skips duplicates).
            try:
                final_stats = self.lead_manager.get_stats()
                self.root.after(0, lambda s=final_stats: self._seed_live_stats(s))
            except Exception:
                pass

            if owners_only:
                self.logger.info(
                    f"Owners filter: {skipped_not_owner} providers skipped as not "
                    f"listed as owner/CEO/founder on their own site.")
            if stopped:
                self.logger.info("Psychology Today collection stopped by user.")
            else:
                self.logger.info("Psychology Today collection finished.")
            try:
                self._record_stats("pt_collect", self.lead_manager.get_stats())
            except Exception:
                pass
            if stopped:
                self.root.after(0, lambda: messagebox.showinfo("Stopped", "Psychology Today collection stopped by user."))
            else:
                self.root.after(0, lambda: messagebox.showinfo("Success", "Psychology Today collection finished."))
        except Exception as e:
            self.logger.error(f"Error: {e}" + chr(10) + traceback.format_exc())
            self.root.after(0, lambda err=e: messagebox.showerror("Error", str(err)))
        finally:
            self.pt_btn.after(0, lambda: self.pt_btn.config(state=tk.NORMAL))
            self.stop_btn.after(0, lambda: self.stop_btn.config(state=tk.DISABLED))

    @staticmethod
    def _extract_npi_numbers(text):
        """Pulls NPI numbers out of pasted text. Prefers numbers that appear
        right after the word "NPI" (how ZocDoc and similar sites label them)
        to avoid mistaking an unrelated 10-digit number (e.g. a phone number)
        for an NPI; falls back to any bare 10-digit number starting with 1 or
        2 (the only valid NPI enumeration prefixes) if nothing labeled is found,
        so a plain list of numbers pasted with no context still works."""
        contextual = re.findall(r'NPI[^0-9]{0,10}(\d{10})', text, re.IGNORECASE)
        found = contextual if contextual else re.findall(r'\b[12]\d{9}\b', text)
        seen = set()
        result = []
        for n in found:
            if n not in seen:
                seen.add(n)
                result.append(n)
        return result

    def start_zocdoc_collection(self):
        try:
            raw_text = self.npi_paste_text.get("1.0", tk.END)
            npi_numbers = self._extract_npi_numbers(raw_text)
            if not npi_numbers:
                messagebox.showerror("Error", "No NPI numbers found in the pasted text.")
                return
            self.lead_manager = self.get_manager()
            self.stop_event = threading.Event()
            self.zocdoc_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            threading.Thread(target=self.zocdoc_collection_worker, args=(npi_numbers,), daemon=True).start()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _process_zocdoc_npi(self, npi_number):
        """Runs the manual workflow described for ZocDoc leads, starting from
        an NPI number you already copied by hand (since ZocDoc itself blocks
        automated access): look the NPI up directly (exact match, so there's
        no name-matching ambiguity), search its phone number and collect every
        email the search turns up, and if that comes up empty, fall back to
        searching the provider's full name instead. Returns a lead dict, or
        None if the NPI number isn't registered."""
        match = self.npi_engine.find_by_number(npi_number)
        if not match:
            return None

        phone = match.get("Phone", "")
        emails = self.email_engine.find_all_emails_by_phone(phone) if phone else []

        if not emails:
            full_name = f"{match.get('Provider First Name', '')} {match.get('Provider Last Name', '')}".strip()
            full_name = full_name or match.get("Organization Name", "")
            emails = self.email_engine.find_all_emails_by_name(full_name, match.get("City", ""), match.get("State", ""))

        lead = dict(match)
        lead["Email"] = emails[0] if emails else ""
        lead["All Emails Found"] = ", ".join(emails)
        lead["Source"] = "ZocDoc (NPI Import)"
        return lead

    def zocdoc_collection_worker(self, npi_numbers):
        stopped = False
        try:
            self.logger.info(f"Looking up {len(npi_numbers)} pasted NPI numbers...")
            try:
                seed_stats = self.lead_manager.get_stats()
                self.root.after(0, lambda s=seed_stats: self._seed_live_stats(s))
            except Exception:
                pass

            total = len(npi_numbers)
            done = 0
            leads_buffer = []

            with ThreadPoolExecutor(max_workers=self.PT_WORKERS) as executor:
                future_to_npi = {
                    executor.submit(self._process_zocdoc_npi, npi): npi
                    for npi in npi_numbers
                }
                for future in as_completed(future_to_npi):
                    if self.stop_event.is_set():
                        stopped = True
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
                    npi = future_to_npi[future]
                    try:
                        lead = future.result()
                    except Exception as e:
                        self.logger.warning(f"Failed to process NPI {npi}: {e}")
                        done += 1
                        continue

                    done += 1
                    if not lead:
                        self.logger.info(f"[{done}/{total}] NPI {npi} — not found in the NPI registry")
                        self.root.after(0, lambda d=done, t=total: self.progress_var.set((d / t) * 100 if t else 0))
                        continue

                    leads_buffer.append(lead)
                    label = f"{lead.get('Provider First Name', '')} {lead.get('Provider Last Name', '')}".strip() or lead.get("Organization Name", "Unknown")
                    email_count = len(lead["All Emails Found"].split(", ")) if lead.get("All Emails Found") else 0
                    self.logger.info(f"[{done}/{total}] {label} (NPI {npi}) — {email_count} email(s) found")
                    self.root.after(0, lambda d=done, t=total: self.progress_var.set((d / t) * 100 if t else 0))

                    if len(leads_buffer) >= 5:
                        batch, leads_buffer = leads_buffer, []
                        counts = self.lead_manager.add_leads_batch(batch)
                        for st, c in counts.items():
                            self.root.after(0, lambda st=st, c=c: self._update_live_stats(st, leads_delta=c))

            if leads_buffer:
                counts = self.lead_manager.add_leads_batch(leads_buffer)
                for st, c in counts.items():
                    self.root.after(0, lambda st=st, c=c: self._update_live_stats(st, leads_delta=c))

            try:
                final_stats = self.lead_manager.get_stats()
                self.root.after(0, lambda s=final_stats: self._seed_live_stats(s))
            except Exception:
                pass

            if stopped:
                self.logger.info("ZocDoc NPI import stopped by user.")
            else:
                self.logger.info("ZocDoc NPI import finished.")
            try:
                self._record_stats("zocdoc_import", self.lead_manager.get_stats())
            except Exception:
                pass
            if stopped:
                self.root.after(0, lambda: messagebox.showinfo("Stopped", "ZocDoc NPI import stopped by user."))
            else:
                self.root.after(0, lambda: messagebox.showinfo("Success", "ZocDoc NPI import finished."))
        except Exception as e:
            self.logger.error(f"Error: {e}" + chr(10) + traceback.format_exc())
            self.root.after(0, lambda err=e: messagebox.showerror("Error", str(err)))
        finally:
            self.zocdoc_btn.after(0, lambda: self.zocdoc_btn.config(state=tk.NORMAL))
            self.stop_btn.after(0, lambda: self.stop_btn.config(state=tk.DISABLED))

    def start_email_finding(self):
        try:
            self.lead_manager = self.get_manager()
            self.stop_event = threading.Event()
            self.email_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            threading.Thread(target=self.email_worker, daemon=True).start()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _find_emails_concurrently(self, pending, on_result):
        """Run find_email for each (key, lead) pair in pending using a thread pool.
        Calls on_result(key, updated_lead) on the calling thread as each result
        completes, so callers can safely touch non-thread-safe objects
        (openpyxl worksheets, gspread cell objects) without extra locking.
        Returns True if the run was stopped early via self.stop_event."""
        stopped = False
        with ThreadPoolExecutor(max_workers=self.EMAIL_WORKERS) as executor:
            future_to_key = {
                executor.submit(self.email_engine.find_email, lead): key
                for key, lead in pending
            }
            completed = 0
            for future in as_completed(future_to_key):
                if self.stop_event.is_set():
                    stopped = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                key = future_to_key[future]
                try:
                    updated = future.result()
                except Exception as e:
                    self.logger.warning(f"Email lookup failed: {e}")
                    updated = {}
                name = updated.get('Organization Name') or updated.get('Provider Last Name') or 'Unknown'
                self.logger.info(f"Found email for {name}: {updated.get('Email') or '(none)'}")
                completed += 1
                on_result(key, updated, completed)
        return stopped

    def email_worker(self):
        stopped = False
        try:
            self.logger.info("Starting email discovery...")
            try:
                seed_stats = self.lead_manager.get_stats()
                self.root.after(0, lambda s=seed_stats: self._seed_live_stats(s))
            except Exception:
                pass
            if self.output_type.get() == "Excel":
                import openpyxl
                wb = openpyxl.load_workbook(self.lead_manager.filename)
                try:
                    # Every email in the sheet must stay unique, including ones
                    # discovered here — track what's already used across every
                    # tab so a newly-found email that collides with an existing
                    # lead's email gets dropped instead of duplicated.
                    email_idx = self.lead_manager.headers.index("Email")
                    seen_emails = set()
                    for sn in wb.sheetnames:
                        for row in wb[sn].iter_rows(min_row=2, values_only=True):
                            if row is None or row[0] is None:
                                continue
                            if email_idx < len(row):
                                key = self.lead_manager._normalize_email_key(row[email_idx])
                                if key:
                                    seen_emails.add(key)

                    for sheet_name in wb.sheetnames:
                        if self.stop_event.is_set():
                            stopped = True
                            break
                        ws = wb[sheet_name]
                        rows = list(ws.iter_rows(min_row=2))
                        pending = []
                        row_by_key = {}
                        for i, row in enumerate(rows):
                            if not row[5].value:  # Email column is empty
                                lead = {
                                    self.lead_manager.headers[j]: (row[j].value if j < len(row) else "")
                                    for j in range(len(self.lead_manager.headers))
                                }
                                pending.append((i, lead))
                                row_by_key[i] = row

                        if not pending:
                            continue

                        self.logger.info(f"Finding emails for {len(pending)} leads in {sheet_name}...")

                        def on_result(key, updated, completed, st=sheet_name):
                            row = row_by_key[key]
                            found_email = updated.get("Email", "")
                            email_key = self.lead_manager._normalize_email_key(found_email)
                            if email_key and email_key in seen_emails:
                                self.logger.info(f"Duplicate email {found_email} already used elsewhere — leaving blank.")
                                found_email = ""
                            elif email_key:
                                seen_emails.add(email_key)
                            row[5].value = found_email
                            row[11].value = updated.get("Website", "")
                            if found_email:
                                self.root.after(0, lambda: self._update_live_stats(st, emails_delta=1))
                            # Save periodically to preserve progress
                            if completed % 5 == 0:
                                self.lead_manager.save_workbook(wb)

                        if self._find_emails_concurrently(pending, on_result):
                            stopped = True
                        self.lead_manager.save_workbook(wb)
                        if stopped:
                            break
                finally:
                    wb.close()  # Always release file lock on Windows
            else:
                # Worksheets are named by Source (not by state), so iterate
                # whatever tabs actually exist rather than the state list.
                # Every email must stay unique across all tabs, so reuse the
                # same global cache add_leads_batch relies on.
                self.lead_manager._ensure_global_contact_caches()
                seen_emails = self.lead_manager._seen_emails

                for ws in self.lead_manager.spreadsheet.worksheets():
                    if self.stop_event.is_set():
                        stopped = True
                        break
                    try:
                        data = ws.get_all_records()
                        pending = [(i, dict(row)) for i, row in enumerate(data, 2) if not row.get("Email")]
                        if not pending:
                            continue

                        self.logger.info(f"Finding emails for {len(pending)} leads in {ws.title}...")

                        # Collect all cells to update and do one batch write per sheet
                        cells_to_update = []

                        def on_result(key, updated, completed, st=ws.title):
                            found_email = updated.get("Email", "")
                            email_key = self.lead_manager._normalize_email_key(found_email)
                            if email_key and email_key in seen_emails:
                                self.logger.info(f"Duplicate email {found_email} already used elsewhere — leaving blank.")
                                found_email = ""
                            elif email_key:
                                seen_emails.add(email_key)
                            email_cell = ws.cell(key, 6)   # Col 6 = Email
                            website_cell = ws.cell(key, 12)  # Col 12 = Website
                            email_cell.value = found_email
                            website_cell.value = updated.get("Website", "")
                            cells_to_update.extend([email_cell, website_cell])
                            if found_email:
                                self.root.after(0, lambda: self._update_live_stats(st, emails_delta=1))

                        if self._find_emails_concurrently(pending, on_result):
                            stopped = True

                        # Batch update all changed cells in a single API call
                        if cells_to_update:
                            ws.update_cells(cells_to_update)
                        if stopped:
                            break
                    except Exception as e:
                        self.logger.warning(f"Skipping sheet {ws.title}: {e}")
                        continue

            if stopped:
                self.logger.info("Email finding stopped by user.")
                self.root.after(0, lambda: messagebox.showinfo("Stopped", "Email finding stopped by user."))
                return

            self.logger.info("Email finding finished.")
            try:
                hit_rate = self._record_stats("email_find", self.lead_manager.get_stats())
                self.root.after(0, lambda: messagebox.showinfo(
                    "Success", f"Email finding finished.\nEmail hit rate: {hit_rate}%"))
            except Exception:
                self.root.after(0, lambda: messagebox.showinfo("Success", "Email finding finished."))
        except Exception as e:
            self.logger.error(f"Error: {e}" + chr(10) + traceback.format_exc())
        finally:
            self.email_btn.after(0, lambda: self.email_btn.config(state=tk.NORMAL))
            self.stop_btn.after(0, lambda: self.stop_btn.config(state=tk.DISABLED))

    def _load_run_history(self):
        if not os.path.exists(self.RUN_HISTORY_FILE):
            return []
        try:
            with open(self.RUN_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_run_record(self, record):
        history = self._load_run_history()
        history.append(record)
        history = history[-50:]  # keep last 50 runs
        with open(self.RUN_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    def _record_stats(self, action, stats):
        total = stats.get("total", 0)
        with_email = stats.get("with_email", 0)
        hit_rate = round((with_email / total * 100), 1) if total else 0.0
        self._save_run_record({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "total": total,
            "with_email": with_email,
            "hit_rate": hit_rate
        })
        return hit_rate

    def show_summary(self):
        try:
            manager = self.lead_manager or self.get_manager()
            stats = manager.get_stats()
        except Exception as e:
            messagebox.showerror("Error", f"Could not load summary: {e}")
            return

        total = stats.get("total", 0)
        with_email = stats.get("with_email", 0)
        hit_rate = (with_email / total * 100) if total else 0

        win = tk.Toplevel(self.root)
        win.title("Run Summary")
        win.geometry("500x480")

        overview = ttk.LabelFrame(win, text="Overview", padding="10")
        overview.pack(fill=tk.X, padx=10, pady=10)
        ttk.Label(overview, text=f"Total Leads: {total}").pack(anchor=tk.W)
        ttk.Label(overview, text=f"Emails Found: {with_email}").pack(anchor=tk.W)
        ttk.Label(overview, text=f"Email Hit Rate: {hit_rate:.1f}%").pack(anchor=tk.W)

        state_frame = ttk.LabelFrame(win, text="By State", padding="10")
        state_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        tree = ttk.Treeview(state_frame, columns=("total", "email", "rate"), show="tree headings", height=8)
        tree.heading("#0", text="State")
        tree.heading("total", text="Total")
        tree.heading("email", text="Emails")
        tree.heading("rate", text="Hit Rate")
        tree.column("#0", width=100)
        tree.column("total", width=80, anchor=tk.CENTER)
        tree.column("email", width=80, anchor=tk.CENTER)
        tree.column("rate", width=80, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True)
        for state, s in sorted(stats.get("by_state", {}).items()):
            state_rate = (s["with_email"] / s["total"] * 100) if s["total"] else 0
            tree.insert("", tk.END, text=state, values=(s["total"], s["with_email"], f"{state_rate:.1f}%"))

        history_frame = ttk.LabelFrame(win, text="Recent Runs", padding="10")
        history_frame.pack(fill=tk.X, padx=10, pady=5)
        history_text = scrolledtext.ScrolledText(history_frame, height=6)
        history_text.pack(fill=tk.X)
        history = self._load_run_history()
        if history:
            for record in reversed(history[-10:]):
                line = (f"{record['timestamp']} | {record['action']} | "
                        f"{record['with_email']}/{record['total']} emails ({record['hit_rate']}%)\n")
                history_text.insert(tk.END, line)
        else:
            history_text.insert(tk.END, "No runs recorded yet.\n")
        history_text.configure(state='disabled')

    def show_dashboard(self):
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            import matplotlib.pyplot as plt
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except ImportError:
            messagebox.showerror(
                "Error",
                "matplotlib is required for the dashboard.\nInstall it with: pip install matplotlib")
            return

        try:
            manager = self.lead_manager or self.get_manager()
            stats = manager.get_stats()
        except Exception as e:
            messagebox.showerror("Error", f"Could not load dashboard data: {e}")
            return

        history = self._load_run_history()

        # Colors from the project's data-viz reference palette (light mode, single
        # sequential hue — these are magnitude/ranking charts, not identity charts).
        BLUE = "#2a78d6"
        INK_PRIMARY = "#0b0b0b"
        INK_SECONDARY = "#52514e"
        INK_MUTED = "#898781"
        GRID = "#e1e0d9"
        AXIS = "#c3c2b7"
        SURFACE = "#fcfcfb"

        win = tk.Toplevel(self.root)
        win.title("Analytics Dashboard")
        win.geometry("1050x780")
        win.configure(bg=SURFACE)

        total = stats.get("total", 0)
        with_email = stats.get("with_email", 0)
        hit_rate = (with_email / total * 100) if total else 0
        footer_text = (f"Total Leads: {total}   |   Emails Found: {with_email}   |   "
                        f"Overall Hit Rate: {hit_rate:.1f}%")
        ttk.Label(win, text=footer_text, font=("TkDefaultFont", 10, "bold")).pack(pady=8)

        fig = plt.Figure(figsize=(11, 8), dpi=100, facecolor=SURFACE)
        fig.subplots_adjust(hspace=0.65, wspace=0.4, left=0.14, right=0.96, top=0.94, bottom=0.07)
        ax1 = fig.add_subplot(2, 2, 1)
        ax2 = fig.add_subplot(2, 2, 2)
        ax3 = fig.add_subplot(2, 2, 3)
        ax4 = fig.add_subplot(2, 2, 4)

        def style_axes(ax, title):
            ax.set_facecolor(SURFACE)
            ax.set_title(title, fontsize=10, color=INK_PRIMARY, loc="left", fontweight="bold")
            ax.tick_params(colors=INK_SECONDARY, labelsize=8)
            for spine in ("top", "right"):
                ax.spines[spine].set_visible(False)
            for spine in ("left", "bottom"):
                ax.spines[spine].set_color(AXIS)

        # Chart 1: Leads by state — magnitude ranking, one hue, top 15 by volume.
        by_state = stats.get("by_state", {})
        top_states = sorted(by_state.items(), key=lambda kv: kv[1]["total"], reverse=True)[:15]
        if top_states:
            labels = [s for s, _ in reversed(top_states)]
            values = [d["total"] for _, d in reversed(top_states)]
            ax1.barh(labels, values, color=BLUE, height=0.6, zorder=3)
            ax1.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
            ax1.set_axisbelow(True)
        else:
            ax1.text(0.5, 0.5, "No data yet", ha="center", va="center", color=INK_MUTED,
                      fontsize=9, transform=ax1.transAxes)
        style_axes(ax1, "Leads by State (Top 15)")

        # Chart 2: Email hit rate by state — same top-15-by-volume states, for comparability.
        if top_states:
            rate_labels = [s for s, _ in reversed(top_states)]
            rates = [(d["with_email"] / d["total"] * 100) if d["total"] else 0 for _, d in reversed(top_states)]
            ax2.barh(rate_labels, rates, color=BLUE, height=0.6, zorder=3)
            ax2.set_xlim(0, 100)
            ax2.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
            ax2.set_axisbelow(True)
        else:
            ax2.text(0.5, 0.5, "No data yet", ha="center", va="center", color=INK_MUTED,
                      fontsize=9, transform=ax2.transAxes)
        style_axes(ax2, "Email Hit Rate % (Top 15 states by volume)")

        # Chart 3: Hit rate trend across recent runs — single series over time.
        if history:
            recent = history[-15:]
            xs = list(range(1, len(recent) + 1))
            ys = [r.get("hit_rate", 0) for r in recent]
            ax3.plot(xs, ys, color=BLUE, marker="o", markersize=4, linewidth=2, zorder=3)
            ax3.set_ylim(0, max(100, (max(ys) if ys else 0) + 10))
            ax3.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
            ax3.set_axisbelow(True)
            ax3.set_xticks(xs)
            ax3.set_xticklabels([str(i) for i in xs])
        else:
            ax3.text(0.5, 0.5, "No run history yet", ha="center", va="center", color=INK_MUTED,
                      fontsize=9, transform=ax3.transAxes)
        style_axes(ax3, "Email Hit Rate Trend (Recent Runs)")

        # Chart 4: Leads by specialty — magnitude ranking, top 12.
        by_specialty = stats.get("by_specialty", {})
        top_specialties = sorted(by_specialty.items(), key=lambda kv: kv[1], reverse=True)[:12]
        if top_specialties:
            sp_labels = [s for s, _ in reversed(top_specialties)]
            sp_values = [v for _, v in reversed(top_specialties)]
            ax4.barh(sp_labels, sp_values, color=BLUE, height=0.6, zorder=3)
            ax4.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
            ax4.set_axisbelow(True)
            ax4.tick_params(axis='y', labelsize=7)
        else:
            ax4.text(0.5, 0.5, "No data yet", ha="center", va="center", color=INK_MUTED,
                      fontsize=9, transform=ax4.transAxes)
        style_axes(ax4, "Leads by Specialty (Top 12)")

        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def open_result(self):
        try:
            if self.output_type.get() == "Excel":
                os.startfile(self.lead_manager.filename if self.lead_manager else "RCM_Provider_Leads.xlsx")
            else:
                import webbrowser
                webbrowser.open(self.lead_manager.spreadsheet.url)
        except Exception as e:
            messagebox.showerror("Error", str(e))

if __name__ == "__main__":
    root = tk.Tk()
    app = LeadGenApp(root)
    root.mainloop()
