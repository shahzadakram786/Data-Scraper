"""Medical Provider Lead Generation System — desktop app.

    pip install -r requirements.txt
    python app.py

The window is a front end and nothing more. Every decision it used to make
itself — which phone number to trust, how to split a name, when a run has
failed — now lives in pipeline.py, so the same work can be run from cli.py, on
a schedule, or in a test.

The layout follows the order the work actually happens in: Collect, then
Enrich, then Results, with Settings out of the way. One primary button at the
bottom does whatever the current tab is for, so there is never a question of
which of six buttons to press.
"""

import os
import queue
import logging
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog

import config
import catalog
import health
import storage
import outreach
import verification
from storage import LeadStore, RUN_OK, RUN_EMPTY, RUN_STOPPED, RUN_FAILED
from pipeline import LeadPipeline, Reporter


# --- palette ---------------------------------------------------------------
# One place for every colour, so the banner, the status pills and the charts
# agree with each other.
INK = "#1a1a1a"
INK_SOFT = "#5a5a5a"
INK_FAINT = "#8a8a8a"
SURFACE = "#ffffff"
SURFACE_ALT = "#f4f5f7"
BORDER = "#d8dade"
BRAND = "#1F4E78"

TONES = {
    "idle":  ("#eef0f3", "#44484d"),
    "busy":  ("#e5effb", "#14508c"),
    "ok":    ("#e4f4e6", "#1d6b2a"),
    "warn":  ("#fdf2e0", "#8a5310"),
    "error": ("#fdeceb", "#a3241c"),
}


class TextHandler(logging.Handler):
    """Routes log records to the activity pane.

    Records arrive on worker threads, so they go through a queue that the Tk
    main loop drains. Calling into a widget from a worker thread is the classic
    way to make Tk deadlock, and this is the busiest path in the app.
    """

    def __init__(self, widget):
        super().__init__()
        self.widget = widget
        self.queue = queue.Queue()
        self.widget.after(120, self._drain)

    def emit(self, record):
        try:
            self.queue.put_nowait((self.format(record), record.levelname))
        except Exception:
            pass

    def _drain(self):
        pending = []
        while True:
            try:
                pending.append(self.queue.get_nowait())
            except queue.Empty:
                break
        if pending:
            self.widget.configure(state="normal")
            for message, level in pending:
                tag = level.lower() if level in ("WARNING", "ERROR") else ""
                self.widget.insert(tk.END, message + "\n", tag)
            # Keep the pane bounded: a full-state run emits tens of thousands
            # of lines, and Tk slows to a crawl long before that is readable.
            line_count = int(self.widget.index("end-1c").split(".")[0])
            if line_count > 4000:
                self.widget.delete("1.0", str(line_count - 3000) + ".0")
            self.widget.see(tk.END)
            self.widget.configure(state="disabled")
        self.widget.after(120, self._drain)


class StatusBanner(tk.Frame):
    """The one place the app says how things are going.

    It exists because the old version reported a broken scraper and a finished
    run identically — a message box saying "Collection finished". A run that
    collected nothing now turns this amber, and a failure turns it red with the
    remedy spelled out.
    """

    def __init__(self, master):
        super().__init__(master, bd=0, highlightthickness=1,
                         highlightbackground=BORDER)
        self.title = tk.Label(self, anchor="w", font=("Segoe UI", 10, "bold"))
        self.title.pack(fill=tk.X, padx=12, pady=(9, 0))
        self.detail = tk.Label(self, anchor="w", justify="left",
                               font=("Segoe UI", 9), wraplength=940)
        self.detail.pack(fill=tk.X, padx=12, pady=(1, 9))
        self.set("idle", "Ready.", "")

    def set(self, tone, title, detail=""):
        background, foreground = TONES.get(tone, TONES["idle"])
        self.configure(bg=background)
        for widget in (self.title, self.detail):
            widget.configure(bg=background, fg=foreground)
        self.title.configure(text=title)
        self.detail.configure(text=detail)
        self.detail.pack_forget() if not detail else self.detail.pack(
            fill=tk.X, padx=12, pady=(1, 9))


class AppReporter(Reporter):
    """Pipeline callbacks, marshalled onto the Tk thread."""

    def __init__(self, app, logger, stop_event):
        super().__init__(logger, stop_event)
        self.app = app

    def progress(self, done, total):
        self.app.root.after(0, self.app.set_progress, done, total)

    def counts(self, sheet_key, leads=0, emails=0):
        self.app.root.after(0, self.app.bump_counts, sheet_key, leads, emails)

    def phase(self, text):
        self.log(text)
        self.app.root.after(0, lambda: self.app.banner.set("busy", text))


class LeadGenApp:
    WINDOW_TITLE = "Medical Provider Lead Generation System"

    def __init__(self, root):
        self.root = root
        self.root.title(self.WINDOW_TITLE)
        self.root.geometry("1040x820")
        self.root.minsize(900, 700)

        self.settings = config.load()
        self.store = None
        self.stop_event = threading.Event()
        self.worker = None
        self.sheet_totals = {}
        self.tree_items = {}

        self._build_style()
        self._build_ui()
        self._setup_logging()
        self.root.after(50, self._startup)

    # ------------------------------------------------------------- chrome

    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=("Segoe UI", 9))
        style.configure("TFrame", background=SURFACE)
        style.configure("TLabelframe", background=SURFACE, bordercolor=BORDER)
        style.configure("TLabelframe.Label", background=SURFACE, foreground=INK_SOFT,
                        font=("Segoe UI", 9, "bold"))
        style.configure("TLabel", background=SURFACE, foreground=INK)
        style.configure("Muted.TLabel", foreground=INK_FAINT)
        style.configure("Head.TLabel", font=("Segoe UI", 15, "bold"), foreground=BRAND)
        style.configure("Sub.TLabel", foreground=INK_SOFT)
        style.configure("Metric.TLabel", font=("Segoe UI", 20, "bold"), foreground=BRAND)
        style.configure("MetricLabel.TLabel", foreground=INK_SOFT)
        style.configure("TCheckbutton", background=SURFACE)
        style.configure("TRadiobutton", background=SURFACE)
        style.configure("TNotebook", background=SURFACE_ALT, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(18, 9), font=("Segoe UI", 9))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), padding=(18, 9))
        style.configure("Treeview", rowheight=23, fieldbackground=SURFACE)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        self.root.configure(bg=SURFACE)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=(14, 12, 14, 10))
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Provider Lead Generation",
                  style="Head.TLabel").pack(side=tk.LEFT)
        ttk.Button(header, text="Health Check",
                   command=self.run_health_check).pack(side=tk.RIGHT)
        ttk.Button(header, text="Dashboard",
                   command=self.show_dashboard).pack(side=tk.RIGHT, padx=6)

        self.banner = StatusBanner(outer)
        self.banner.pack(fill=tk.X, pady=(10, 12))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda _e: self._sync_primary())

        self._build_collect_tab()
        self._build_enrich_tab()
        self._build_outreach_tab()
        self._build_results_tab()
        self._build_settings_tab()

        self._build_action_bar(outer)
        self._build_log(outer)
        self._sync_primary()  # everything exists now, so label the button

    def _build_action_bar(self, parent):
        bar = ttk.Frame(parent, padding=(0, 12, 0, 8))
        bar.pack(fill=tk.X)

        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(bar, variable=self.progress_var, maximum=100)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.progress_label = ttk.Label(bar, text="", style="Muted.TLabel", width=16,
                                        anchor="e")
        self.progress_label.pack(side=tk.LEFT, padx=10)

        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop_run,
                                   state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT)

        self.primary_btn = ttk.Button(bar, text="Start", style="Primary.TButton",
                                      command=self.run_primary)
        self.primary_btn.pack(side=tk.RIGHT, padx=8)

    def _build_log(self, parent):
        frame = ttk.LabelFrame(parent, text="Activity", padding=6)
        frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = scrolledtext.ScrolledText(
            frame, state="disabled", height=9, wrap="word",
            font=("Consolas", 9), background=SURFACE_ALT, relief="flat",
            borderwidth=0)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_configure("warning", foreground="#8a5310")
        self.log_text.tag_configure("error", foreground="#a3241c")

    # ---------------------------------------------------------- Collect tab

    def _build_collect_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text="  1. Collect  ")

        ttk.Label(tab, text="Where should the leads come from?",
                  style="Sub.TLabel").pack(anchor=tk.W, pady=(0, 10))

        self.source_var = tk.StringVar(value="pt")
        sources = (
            ("pt", "Psychology Today directory",
             "Walks a state's therapist directory and follows each provider's own "
             "website for their email, direct phone, role and social profiles."),
            ("npi", "NPI Registry (bulk)",
             "Fetches every provider of a given specialty in a state from the "
             "federal registry. High volume, but the registry publishes no emails."),
            ("paste", "Paste NPI numbers",
             "For numbers copied by hand from ZocDoc or anywhere else. Looks each "
             "one up and searches for contact details."),
        )
        for value, title, description in sources:
            row = ttk.Frame(tab)
            row.pack(fill=tk.X, pady=3)
            ttk.Radiobutton(row, text=title, value=value, variable=self.source_var,
                            command=self._sync_source).pack(anchor=tk.W)
            ttk.Label(row, text=description, style="Muted.TLabel",
                      wraplength=880, justify="left").pack(anchor=tk.W, padx=(22, 0))

        ttk.Separator(tab).pack(fill=tk.X, pady=14)

        # Only the options that apply to the chosen source are shown, so the
        # window never asks for a specialty when scraping Psychology Today.
        self.options_area = ttk.Frame(tab)
        self.options_area.pack(fill=tk.BOTH, expand=True)

        self.pt_options = ttk.Frame(self.options_area)
        self._grid_label(self.pt_options, "State", 0)
        self.pt_state_var = tk.StringVar(value="Texas")
        ttk.Combobox(self.pt_options, textvariable=self.pt_state_var, width=28,
                     values=list(catalog.STATES.keys()), state="readonly"
                     ).grid(row=0, column=1, sticky=tk.W, pady=4)
        self.owners_var = tk.BooleanVar(value=self.settings.get("owners_only", False))
        ttk.Checkbutton(self.pt_options,
                        text="Owners / CEOs / Founders only",
                        variable=self.owners_var).grid(row=1, column=1, sticky=tk.W, pady=4)
        ttk.Label(self.pt_options,
                  text="Keeps only providers whose own website presents them as "
                       "running the practice.",
                  style="Muted.TLabel").grid(row=2, column=1, sticky=tk.W)
        self.resume_var = tk.BooleanVar(value=self.settings.get("resume_runs", True))
        ttk.Checkbutton(self.pt_options,
                        text="Resume a stopped run from where it left off",
                        variable=self.resume_var).grid(row=3, column=1, sticky=tk.W, pady=(10, 4))
        self.resume_hint = ttk.Label(self.pt_options, text="", style="Muted.TLabel")
        self.resume_hint.grid(row=4, column=1, sticky=tk.W)

        self.npi_options = ttk.Frame(self.options_area)
        self._grid_label(self.npi_options, "State", 0)
        self.npi_state_var = tk.StringVar(value="Texas")
        ttk.Combobox(self.npi_options, textvariable=self.npi_state_var, width=28,
                     values=["All States"] + list(catalog.STATES.keys()),
                     state="readonly").grid(row=0, column=1, sticky=tk.W, pady=4)
        self._grid_label(self.npi_options, "Specialty", 1)
        self.specialty_var = tk.StringVar(value="Psychiatry")
        ttk.Combobox(self.npi_options, textvariable=self.specialty_var, width=42,
                     values=["All Specialties"] + list(catalog.SPECIALTIES.keys()),
                     state="readonly").grid(row=1, column=1, sticky=tk.W, pady=4)
        self._grid_label(self.npi_options, "Updated since", 2)
        self.min_year_var = tk.StringVar(value=str(self.settings.get("min_updated_year", 2010)))
        ttk.Combobox(self.npi_options, textvariable=self.min_year_var, width=28,
                     values=["No Filter", "2005", "2010", "2015", "2018", "2020", "2022", "2024"],
                     state="readonly").grid(row=2, column=1, sticky=tk.W, pady=4)
        self.exclude_corp_var = tk.BooleanVar(
            value=self.settings.get("exclude_corporate", True))
        ttk.Checkbutton(self.npi_options, text="Exclude corporate entities (Inc, Corp)",
                        variable=self.exclude_corp_var).grid(row=3, column=1,
                                                             sticky=tk.W, pady=4)

        self.paste_options = ttk.Frame(self.options_area)
        ttk.Label(self.paste_options,
                  text="Paste NPI numbers below — one per line, or any copied text "
                       "containing them:").pack(anchor=tk.W)
        self.npi_paste = scrolledtext.ScrolledText(self.paste_options, height=9,
                                                   font=("Consolas", 9))
        self.npi_paste.pack(fill=tk.BOTH, expand=True, pady=6)
        self.paste_count = ttk.Label(self.paste_options, text="", style="Muted.TLabel")
        self.paste_count.pack(anchor=tk.W)
        self.npi_paste.bind("<KeyRelease>", lambda _e: self._count_pasted_npis())

        self._sync_source()

    @staticmethod
    def _grid_label(parent, text, row):
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky=tk.W,
                                          padx=(0, 14), pady=4)

    def _sync_source(self):
        for frame in (self.pt_options, self.npi_options, self.paste_options):
            frame.pack_forget()
        source = self.source_var.get()
        if source == "pt":
            self.pt_options.pack(fill=tk.BOTH, expand=True)
            self._refresh_resume_hint()
        elif source == "npi":
            self.npi_options.pack(fill=tk.BOTH, expand=True)
        else:
            self.paste_options.pack(fill=tk.BOTH, expand=True)
        self._sync_primary()

    def _count_pasted_npis(self):
        numbers = LeadPipeline.extract_npi_numbers(self.npi_paste.get("1.0", tk.END))
        self.paste_count.configure(
            text="{0} NPI number(s) found.".format(len(numbers)) if numbers
            else "No NPI numbers found yet.")
        self._sync_primary()

    def _refresh_resume_hint(self):
        """Tell the user, before they start, that a half-finished run exists."""
        if not self.store:
            return
        state = self.pt_state_var.get()
        job_key = "pt:{0}:owners={1}".format(state, int(self.owners_var.get()))
        checkpoint = self.store.get_checkpoint(job_key)
        if checkpoint and checkpoint.get("next_page", 1) > 1:
            self.resume_hint.configure(
                text="A previous {0} run stopped at listing page {1} — it will "
                     "continue from there.".format(state, checkpoint["next_page"]))
        else:
            self.resume_hint.configure(text="")

    # ----------------------------------------------------------- Enrich tab

    def _build_enrich_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text="  2. Enrich  ")

        ttk.Label(tab, text="Fill in what collection could not find, and check that "
                            "what it did find is real.",
                  style="Sub.TLabel").pack(anchor=tk.W, pady=(0, 12))

        self.enrich_var = tk.StringVar(value="find")
        row = ttk.Frame(tab)
        row.pack(fill=tk.X, pady=4)
        ttk.Radiobutton(row, text="Find missing email addresses", value="find",
                        variable=self.enrich_var,
                        command=self._sync_primary).pack(anchor=tk.W)
        self.missing_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.missing_label.pack(anchor=tk.W, padx=(22, 0))

        row = ttk.Frame(tab)
        row.pack(fill=tk.X, pady=4)
        ttk.Radiobutton(row, text="Verify email addresses", value="verify",
                        variable=self.enrich_var,
                        command=self._sync_primary).pack(anchor=tk.W)
        self.unverified_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.unverified_label.pack(anchor=tk.W, padx=(22, 0))
        self.recheck_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="Re-check addresses that already have a status",
                        variable=self.recheck_var).pack(anchor=tk.W, padx=(22, 0))

        row = ttk.Frame(tab)
        row.pack(fill=tk.X, pady=4)
        ttk.Radiobutton(row, text="Add Medicare billing volume and practice size",
                        value="cms", variable=self.enrich_var,
                        command=self._sync_primary).pack(anchor=tk.W)
        self.cms_label = ttk.Label(row, text="", style="Muted.TLabel")
        self.cms_label.pack(anchor=tk.W, padx=(22, 0))
        ttk.Label(row,
                  text="Looks each provider up in federal Medicare data: what "
                       "they bill per year, how many clinicians are in their "
                       "practice, and the phone number they filed with Medicare. "
                       "Only Medicare-enrolled providers appear — expect roughly "
                       "a third of NPI/ZocDoc leads and very few therapists.",
                  style="Muted.TLabel", wraplength=860,
                  justify="left").pack(anchor=tk.W, padx=(22, 0))

        ttk.Separator(tab).pack(fill=tk.X, pady=14)

        legend = ttk.LabelFrame(tab, text="What the statuses mean", padding=10)
        legend.pack(fill=tk.BOTH, expand=True)
        for status in (verification.VALID, verification.ROLE, verification.ACCEPT_ALL,
                       verification.UNKNOWN, verification.RISKY,
                       verification.DISPOSABLE, verification.INVALID):
            line = ttk.Frame(legend)
            line.pack(fill=tk.X, pady=1)
            chip = tk.Label(line, text=" " + status + " ", width=12, anchor="w",
                            font=("Segoe UI", 8, "bold"),
                            bg=_status_colour(status), fg="#333333")
            chip.pack(side=tk.LEFT, padx=(0, 10))
            ttk.Label(line, text=verification.STATUS_HELP[status],
                      style="Muted.TLabel").pack(side=tk.LEFT)

        ttk.Label(legend,
                  text="Addresses that would bounce are removed rather than "
                       "exported with a warning label.",
                  style="Muted.TLabel", wraplength=880,
                  justify="left").pack(anchor=tk.W, pady=(10, 0))


    # --------------------------------------------------------- Outreach tab

    DEFAULT_SUBJECT = "Quick question about billing at {{Practice}}"
    DEFAULT_BODY = (
        "Hi {{First Name}},\n\n"
        "I work with {{Specialty}} practices in {{State}} on their billing and\n"
        "claims, and came across {{Practice}} while researching practices in\n"
        "{{City}}.\n\n"
        "Most practices I speak to are losing hours to claim follow-up and\n"
        "denials rather than to anything clinical. If that sounds familiar,\n"
        "would you be open to a short call this week?\n\n"
        "If not, no problem at all - just say so and I won't follow up.")

    def _build_outreach_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text="  3. Outreach  ")

        ttk.Label(tab,
                  text="Send a personalised email to the leads you have qualified, "
                       "through your own mailbox.",
                  style="Sub.TLabel").pack(anchor=tk.W)

        # Compliance state goes above everything: a send is refused until it is
        # satisfied, so burying it under the editor would only produce a
        # confusing failure later.
        self.compliance_banner = tk.Label(
            tab, anchor="w", justify="left", wraplength=900,
            font=("Segoe UI", 9), padx=10, pady=7)
        self.compliance_banner.pack(fill=tk.X, pady=(10, 12))

        editor = ttk.LabelFrame(tab, text="Message", padding=10)
        editor.pack(fill=tk.BOTH, expand=True)

        row = ttk.Frame(editor)
        row.pack(fill=tk.X)
        ttk.Label(row, text="Subject").pack(side=tk.LEFT, padx=(0, 10))
        self.subject_var = tk.StringVar(value=self.DEFAULT_SUBJECT)
        ttk.Entry(row, textvariable=self.subject_var).pack(
            side=tk.LEFT, fill=tk.X, expand=True)

        self.body_text = scrolledtext.ScrolledText(editor, height=11, wrap="word",
                                                   font=("Segoe UI", 9))
        self.body_text.pack(fill=tk.BOTH, expand=True, pady=(8, 4))
        self.body_text.insert("1.0", self.DEFAULT_BODY)

        ttk.Label(editor,
                  text="Merge fields:  " + "   ".join(
                      "{{" + name + "}}" for name in outreach.MERGE_FIELDS),
                  style="Muted.TLabel", wraplength=900,
                  justify="left").pack(anchor=tk.W)
        ttk.Label(editor,
                  text="Your postal address and an unsubscribe line are added to "
                       "every message automatically - both are legally required.",
                  style="Muted.TLabel").pack(anchor=tk.W, pady=(4, 0))

        controls = ttk.Frame(tab)
        controls.pack(fill=tk.X, pady=(12, 0))

        ttk.Label(controls, text="Send to").pack(side=tk.LEFT)
        self.audience_var = tk.StringVar(
            value="Owner-operated small practices (ICP)")
        ttk.Combobox(controls, textvariable=self.audience_var, width=34,
                     state="readonly",
                     values=["Owner-operated small practices (ICP)",
                             "Owners and founders",
                             "Everyone with a usable address"]
                     ).pack(side=tk.LEFT, padx=8)

        ttk.Label(controls, text="Max this run").pack(side=tk.LEFT, padx=(16, 6))
        self.send_limit_var = tk.StringVar(value="25")
        ttk.Combobox(controls, textvariable=self.send_limit_var, width=6,
                     values=["5", "10", "25", "50", "100"],
                     state="readonly").pack(side=tk.LEFT)

        self.allow_unverified_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="Include unverified addresses",
                        variable=self.allow_unverified_var).pack(
                            side=tk.LEFT, padx=(16, 0))

        actions = ttk.Frame(tab)
        actions.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(actions, text="Preview (no send)",
                   command=lambda: self.start_send(dry_run=True)).pack(side=tk.LEFT)
        ttk.Button(actions, text="Send test to myself",
                   command=self.send_test).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Test connection",
                   command=self.test_smtp).pack(side=tk.LEFT)
        ttk.Button(actions, text="Do-not-contact list",
                   command=self.show_suppressions).pack(side=tk.LEFT, padx=6)

    def _refresh_compliance(self):
        """Green when a send is legal, amber with the exact gaps when not."""
        if not hasattr(self, "compliance_banner"):
            return
        problems = outreach.compliance_problems(self.settings)
        if problems:
            background, foreground = TONES["warn"]
            text = "Before you can send:   " + "    ".join(
                "- " + problem for problem in problems)
        else:
            background, foreground = TONES["ok"]
            stats = self.store.outreach_stats() if self.store else {}
            text = ("Ready to send as {0} <{1}>.   {2} sent so far, {3} on the "
                    "do-not-contact list.".format(
                        self.settings.get("from_name"),
                        self.settings.get("from_email"),
                        stats.get("sent", 0), stats.get("suppressed", 0)))
        self.compliance_banner.configure(text=text, bg=background, fg=foreground)

    def _campaign_filters(self):
        choice = self.audience_var.get()
        return {"icp_only": choice.startswith("Owner-operated"),
                "owners_only": choice.startswith("Owners and")}

    def start_send(self, dry_run=False):
        subject = self.subject_var.get()
        body = self.body_text.get("1.0", tk.END).strip()
        limit = int(self.send_limit_var.get() or 25)
        allow_unverified = self.allow_unverified_var.get()
        filters = self._campaign_filters()

        if not dry_run:
            problems = outreach.compliance_problems(self.settings)
            if problems:
                messagebox.showerror("Not ready to send", "\n\n".join(problems))
                return
            recipients, _excluded = self._build_pipeline().recipients_for(
                limit=limit, allow_unverified=allow_unverified, **filters)
            if not recipients:
                messagebox.showinfo(
                    "Nobody to send to",
                    "No lead matches this audience, or they have all been "
                    "contacted already. Try Preview to see why.")
                return
            if not messagebox.askyesno(
                    "Send for real?",
                    "This will email {0} people from {1}.\n\n"
                    "Messages go out roughly one a minute to protect your "
                    "sending reputation.\n\nContinue?".format(
                        len(recipients), self.settings.get("from_email"))):
                return

        def job():
            pipeline = self._build_pipeline()
            result = pipeline.send_campaign(
                subject, body, campaign="app", limit=limit,
                daily_cap=int(self.settings.get("daily_send_cap", 40)),
                allow_unverified=allow_unverified, dry_run=dry_run,
                min_seconds=int(self.settings.get("send_min_seconds", 45)),
                max_seconds=int(self.settings.get("send_max_seconds", 90)),
                **filters)
            self._after_run(pipeline, result, "Preview" if dry_run else "Outreach")

        self._spawn(job, "Previewing..." if dry_run else "Sending...")

    def send_test(self):
        """One message to yourself, so you see exactly what lands."""
        address = self.settings.get("from_email")
        if not address:
            messagebox.showerror("No sender",
                                 "Set a 'from' address in Settings first.")
            return
        sample = next(iter(self.store.best_leads(limit=1)), None)
        if sample is None:
            messagebox.showinfo("No leads",
                                "Collect some leads first, so the merge fields "
                                "have something to fill in.")
            return

        def job():
            sender = outreach.EmailSender(self.settings, logger=self.logger)
            subject = outreach.render(self.subject_var.get(), sample)
            body = outreach.render(self.body_text.get("1.0", tk.END).strip(), sample)
            try:
                ok, detail = sender.send(address, "[TEST] " + subject, body)
            finally:
                sender.close()
            self.root.after(0, lambda: self.banner.set(
                "ok" if ok else "error",
                "Test sent to " + address if ok else "Test failed",
                "Merge fields were filled from your top lead." if ok else detail))

        self._spawn(job, "Sending a test to yourself...")

    def test_smtp(self):
        def job():
            ok, detail = outreach.EmailSender(
                self.settings, logger=self.logger).verify_login()
            self.root.after(0, lambda: self.banner.set(
                "ok" if ok else "error",
                "Mail server connection works." if ok else "Could not sign in.",
                detail))
        self._spawn(job, "Testing the mail server connection...")

    def show_suppressions(self):
        window = tk.Toplevel(self.root)
        window.title("Do Not Contact")
        window.geometry("620x440")
        frame = ttk.Frame(window, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame,
                  text="Nobody on this list is ever emailed again. Bounced "
                       "addresses are added automatically; add anyone who asks "
                       "to be removed.",
                  style="Muted.TLabel", wraplength=580,
                  justify="left").pack(anchor=tk.W, pady=(0, 8))

        tree = ttk.Treeview(frame, columns=("added", "reason"), show="tree headings")
        tree.heading("#0", text="Address or @domain")
        tree.heading("added", text="Added")
        tree.heading("reason", text="Reason")
        tree.column("#0", width=250)
        tree.column("added", width=90)
        tree.column("reason", width=200)
        tree.pack(fill=tk.BOTH, expand=True)
        for row in self.store.suppressions():
            tree.insert("", tk.END, text=row["value"],
                        values=((row["added_at"] or "")[:10], row["reason"] or ""))

        entry_row = ttk.Frame(frame)
        entry_row.pack(fill=tk.X, pady=(10, 0))
        new_value = tk.StringVar()
        ttk.Entry(entry_row, textvariable=new_value).pack(
            side=tk.LEFT, fill=tk.X, expand=True)

        def add():
            value = new_value.get().strip()
            if self.store.suppress(value, "added by hand"):
                tree.insert("", 0, text=value.lower(),
                            values=("today", "added by hand"))
                new_value.set("")
                self._refresh_compliance()
            else:
                messagebox.showerror("Not valid",
                                     "Enter an email address or @domain.com")

        ttk.Button(entry_row, text="Add", command=add).pack(side=tk.LEFT, padx=6)

    # ---------------------------------------------------------- Results tab

    def _build_results_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text="  4. Results  ")

        metrics = ttk.Frame(tab)
        metrics.pack(fill=tk.X, pady=(0, 14))
        self.metric_vars = {}
        for index, (key, label) in enumerate((
                ("total", "Total leads"),
                ("with_email", "With email"),
                ("verified_email", "Verified"),
                ("with_phone", "With phone"),
                ("with_billing", "Billing data"),
                ("avg_score", "Avg. score"))):
            cell = ttk.Frame(metrics)
            cell.grid(row=0, column=index, padx=(0, 34), sticky=tk.W)
            var = tk.StringVar(value="—")
            ttk.Label(cell, textvariable=var, style="Metric.TLabel").pack(anchor=tk.W)
            ttk.Label(cell, text=label, style="MetricLabel.TLabel").pack(anchor=tk.W)
            self.metric_vars[key] = var

        panes = ttk.Frame(tab)
        panes.pack(fill=tk.BOTH, expand=True)
        panes.columnconfigure(0, weight=3)
        panes.columnconfigure(1, weight=2)
        panes.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(panes, text="By tab (state and source)", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.tree = ttk.Treeview(left, columns=("leads", "emails", "rate"),
                                 show="tree headings", height=9)
        self.tree.heading("#0", text="Tab")
        self.tree.heading("leads", text="Leads")
        self.tree.heading("emails", text="Emails")
        self.tree.heading("rate", text="Hit rate")
        self.tree.column("#0", width=230)
        for column in ("leads", "emails", "rate"):
            self.tree.column(column, width=78, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)

        right = ttk.LabelFrame(panes, text="Email quality", padding=6)
        right.grid(row=0, column=1, sticky="nsew")
        self.status_tree = ttk.Treeview(right, columns=("count",),
                                        show="tree headings", height=9)
        self.status_tree.heading("#0", text="Status")
        self.status_tree.heading("count", text="Leads")
        self.status_tree.column("#0", width=150)
        self.status_tree.column("count", width=70, anchor=tk.CENTER)
        self.status_tree.pack(fill=tk.BOTH, expand=True)

        self.call_hint = ttk.Label(
            tab,
            text="The Call List tab is written to the front of the workbook: the "
                 "highest-scoring leads, each with a plain-English reason and an "
                 "Outcome dropdown to work from.",
            style="Muted.TLabel", wraplength=920, justify="left")
        self.call_hint.pack(anchor=tk.W, pady=(10, 0))

        actions = ttk.Frame(tab)
        actions.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(actions, text="Open spreadsheet",
                   command=self.open_result).pack(side=tk.LEFT)
        ttk.Button(actions, text="Run history",
                   command=self.show_run_history).pack(side=tk.LEFT, padx=6)
        ttk.Label(actions, text="Min score",
                  style="Muted.TLabel").pack(side=tk.LEFT, padx=(24, 6))
        self.min_score_var = tk.StringVar(value="0")
        ttk.Combobox(actions, textvariable=self.min_score_var, width=5,
                     values=["0", "20", "40", "60", "80"],
                     state="readonly").pack(side=tk.LEFT)

        ttk.Label(actions, text="Call list",
                  style="Muted.TLabel").pack(side=tk.LEFT, padx=(18, 6))
        self.call_list_var = tk.StringVar(value="100")
        ttk.Combobox(actions, textvariable=self.call_list_var, width=6,
                     values=["0", "50", "100", "250", "500"],
                     state="readonly").pack(side=tk.LEFT)
        self.icp_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="Owner-operated small practices only",
                        variable=self.icp_only_var).pack(side=tk.LEFT, padx=(10, 0))

    # --------------------------------------------------------- Settings tab

    def _build_settings_tab(self):
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text="  Settings  ")

        output = ttk.LabelFrame(tab, text="Where results are exported", padding=10)
        output.pack(fill=tk.X)
        self.output_var = tk.StringVar(value=self.settings.get("output", "excel"))
        ttk.Radiobutton(output, text="Local Excel file", value="excel",
                        variable=self.output_var,
                        command=self._sync_output).grid(row=0, column=0, sticky=tk.W)
        ttk.Radiobutton(output, text="Google Sheets", value="google",
                        variable=self.output_var,
                        command=self._sync_output).grid(row=0, column=1, sticky=tk.W,
                                                        padx=20)

        self._grid_label(output, "Excel file", 1)
        self.excel_path_var = tk.StringVar(value=self.settings.get("excel_path"))
        ttk.Entry(output, textvariable=self.excel_path_var, width=52).grid(
            row=1, column=1, columnspan=2, sticky=tk.W, pady=3)

        self._grid_label(output, "Service account JSON", 2)
        self.gs_creds_var = tk.StringVar(value=self.settings.get("google_credentials"))
        self.gs_creds_entry = ttk.Entry(output, textvariable=self.gs_creds_var, width=44)
        self.gs_creds_entry.grid(row=2, column=1, sticky=tk.W, pady=3)
        self.gs_browse = ttk.Button(output, text="Browse", command=self._browse_json)
        self.gs_browse.grid(row=2, column=2, padx=6)

        self._grid_label(output, "Spreadsheet name", 3)
        self.gs_name_var = tk.StringVar(value=self.settings.get("google_spreadsheet"))
        self.gs_name_entry = ttk.Entry(output, textvariable=self.gs_name_var, width=44)
        self.gs_name_entry.grid(row=3, column=1, sticky=tk.W, pady=3)

        self.auto_export_var = tk.BooleanVar(value=self.settings.get("auto_export", True))
        ttk.Checkbutton(output, text="Export automatically when a run finishes",
                        variable=self.auto_export_var).grid(row=4, column=1,
                                                            sticky=tk.W, pady=(8, 0))

        quality = ttk.LabelFrame(tab, text="Email accuracy", padding=10)
        quality.pack(fill=tk.X, pady=12)
        self.verify_var = tk.BooleanVar(value=self.settings.get("verify_emails", True))
        ttk.Checkbutton(quality, text="Check addresses against their mail server",
                        variable=self.verify_var).grid(row=0, column=0, columnspan=3,
                                                       sticky=tk.W)
        self.smtp_var = tk.BooleanVar(value=self.settings.get("smtp_probe", True))
        ttk.Checkbutton(quality,
                        text="Confirm individual mailboxes (needs outbound port 25)",
                        variable=self.smtp_var).grid(row=1, column=0, columnspan=3,
                                                     sticky=tk.W)
        self.guess_var = tk.BooleanVar(value=self.settings.get("guess_emails", True))
        ttk.Checkbutton(quality,
                        text="Infer addresses at a known practice domain, and keep "
                             "only the ones the mail server confirms",
                        variable=self.guess_var).grid(row=2, column=0, columnspan=3,
                                                      sticky=tk.W)
        self._grid_label(quality, "Send probes as", 3)
        self.sender_var = tk.StringVar(value=self.settings.get("probe_sender", ""))
        ttk.Entry(quality, textvariable=self.sender_var, width=38).grid(
            row=3, column=1, sticky=tk.W, pady=3)
        ttk.Label(quality,
                  text="Optional. Your real outreach address makes more mail "
                       "servers answer honestly. Blank uses the null sender.",
                  style="Muted.TLabel", wraplength=760,
                  justify="left").grid(row=4, column=1, sticky=tk.W)

        network = ttk.LabelFrame(tab, text="Network", padding=10)
        network.pack(fill=tk.X)
        self._grid_label(network, "Proxy", 0)
        self.proxy_var = tk.StringVar(value=self.settings.get("proxy", ""))
        ttk.Entry(network, textvariable=self.proxy_var, width=38).grid(
            row=0, column=1, sticky=tk.W, pady=3)
        ttk.Label(network,
                  text="Psychology Today serves its directory to US traffic only. "
                       "Leave blank to use the free reader fallback, or point this "
                       "at a US VPN/proxy (e.g. http://127.0.0.1:8080) to go direct.",
                  style="Muted.TLabel", wraplength=760,
                  justify="left").grid(row=1, column=1, sticky=tk.W)

        self._grid_label(network, "Concurrent lookups", 2)
        speed = ttk.Frame(network)
        speed.grid(row=2, column=1, sticky=tk.W, pady=3)
        self.workers_var = tk.IntVar(value=int(self.settings.get("pt_workers", 5)))
        ttk.Scale(speed, from_=1, to=16, variable=self.workers_var,
                  orient=tk.HORIZONTAL, length=200,
                  command=lambda _v: self.workers_label.configure(
                      text=str(self.workers_var.get()))).pack(side=tk.LEFT)
        self.workers_label = ttk.Label(speed, text=str(self.workers_var.get()), width=3)
        self.workers_label.pack(side=tk.LEFT, padx=8)


        sending = ttk.LabelFrame(tab, text="Sending email", padding=10)
        sending.pack(fill=tk.X, pady=12)

        self._grid_label(sending, "Mail provider", 0)
        self.smtp_preset_var = tk.StringVar(value="Gmail")
        preset_box = ttk.Combobox(sending, textvariable=self.smtp_preset_var,
                                  width=26, state="readonly",
                                  values=list(outreach.SMTP_PRESETS.keys()))
        preset_box.grid(row=0, column=1, sticky=tk.W, pady=3)
        preset_box.bind("<<ComboboxSelected>>", lambda _e: self._apply_smtp_preset())
        self.smtp_note = ttk.Label(sending, text="", style="Muted.TLabel",
                                   wraplength=560, justify="left")
        self.smtp_note.grid(row=1, column=1, columnspan=2, sticky=tk.W)

        self._grid_label(sending, "SMTP server", 2)
        self.smtp_host_var = tk.StringVar(value=self.settings.get("smtp_host", ""))
        ttk.Entry(sending, textvariable=self.smtp_host_var, width=30).grid(
            row=2, column=1, sticky=tk.W, pady=3)
        self.smtp_port_var = tk.StringVar(value=str(self.settings.get("smtp_port", 587)))
        ttk.Entry(sending, textvariable=self.smtp_port_var, width=7).grid(
            row=2, column=2, sticky=tk.W, padx=6)

        self._grid_label(sending, "Username", 3)
        self.smtp_user_var = tk.StringVar(value=self.settings.get("smtp_user", ""))
        ttk.Entry(sending, textvariable=self.smtp_user_var, width=38).grid(
            row=3, column=1, columnspan=2, sticky=tk.W, pady=3)

        self._grid_label(sending, "Password", 4)
        self.smtp_pass_var = tk.StringVar(
            value=outreach.load_password(self.settings.get("smtp_user", "")))
        ttk.Entry(sending, textvariable=self.smtp_pass_var, width=38,
                  show="*").grid(row=4, column=1, columnspan=2, sticky=tk.W, pady=3)
        ttk.Label(sending,
                  text="Stored in the Windows Credential Manager, never in any "
                       "file in this project folder.",
                  style="Muted.TLabel").grid(row=5, column=1, columnspan=2, sticky=tk.W)

        self._grid_label(sending, "From name", 6)
        self.from_name_var = tk.StringVar(value=self.settings.get("from_name", ""))
        ttk.Entry(sending, textvariable=self.from_name_var, width=38).grid(
            row=6, column=1, columnspan=2, sticky=tk.W, pady=3)

        self._grid_label(sending, "From address", 7)
        self.from_email_var = tk.StringVar(value=self.settings.get("from_email", ""))
        ttk.Entry(sending, textvariable=self.from_email_var, width=38).grid(
            row=7, column=1, columnspan=2, sticky=tk.W, pady=3)

        compliance = ttk.LabelFrame(
            tab, text="Required by law before you can send", padding=10)
        compliance.pack(fill=tk.X)

        self._grid_label(compliance, "Postal address", 0)
        self.postal_var = tk.StringVar(value=self.settings.get("postal_address", ""))
        ttk.Entry(compliance, textvariable=self.postal_var, width=52).grid(
            row=0, column=1, sticky=tk.W, pady=3)

        self._grid_label(compliance, "Unsubscribe address", 1)
        self.unsub_var = tk.StringVar(value=self.settings.get("unsubscribe_email", ""))
        ttk.Entry(compliance, textvariable=self.unsub_var, width=52).grid(
            row=1, column=1, sticky=tk.W, pady=3)

        ttk.Label(compliance,
                  text="US CAN-SPAM requires commercial email to carry a real "
                       "postal address and a working opt-out. Both are added to "
                       "every message automatically, and sending is blocked "
                       "until they are filled in.",
                  style="Muted.TLabel", wraplength=620,
                  justify="left").grid(row=2, column=1, sticky=tk.W, pady=(4, 6))

        self._grid_label(compliance, "Daily send cap", 3)
        cap_row = ttk.Frame(compliance)
        cap_row.grid(row=3, column=1, sticky=tk.W, pady=3)
        self.daily_cap_var = tk.StringVar(
            value=str(self.settings.get("daily_send_cap", 40)))
        ttk.Combobox(cap_row, textvariable=self.daily_cap_var, width=6,
                     state="readonly",
                     values=["10", "20", "40", "75", "150"]).pack(side=tk.LEFT)
        ttk.Label(cap_row,
                  text="  Start low. A domain with no sending history that emits "
                       "hundreds of cold emails gets blacklisted in a day.",
                  style="Muted.TLabel").pack(side=tk.LEFT)

        self._sync_output()
        self._apply_smtp_preset(initial=True)


    def _apply_smtp_preset(self, initial=False):
        """Fill in host/port for a known provider, and show its gotcha.

        Gmail and Outlook both reject the account password outright when 2FA is
        on, which is the single most common reason a first send fails.
        """
        preset = outreach.SMTP_PRESETS.get(self.smtp_preset_var.get(), {})
        if not initial and preset.get("host"):
            self.smtp_host_var.set(preset["host"])
            self.smtp_port_var.set(str(preset["port"]))
        self.smtp_note.configure(text=preset.get("note", ""))

    def _save_smtp_password(self):
        """Into the OS credential store, keyed by username."""
        username = self.smtp_user_var.get().strip()
        password = self.smtp_pass_var.get()
        if not (username and password):
            return
        try:
            outreach.store_password(username, password)
        except Exception as e:
            self.logger.warning("Could not save the SMTP password: %s", e)

    def _sync_output(self):
        state = tk.NORMAL if self.output_var.get() == "google" else tk.DISABLED
        for widget in (self.gs_creds_entry, self.gs_browse, self.gs_name_entry):
            widget.configure(state=state)

    def _browse_json(self):
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if path:
            self.gs_creds_var.set(path)

    def save_settings(self):
        self.settings.update({
            "output": self.output_var.get(),
            "excel_path": self.excel_path_var.get().strip() or "RCM_Provider_Leads.xlsx",
            "google_credentials": self.gs_creds_var.get().strip(),
            "google_spreadsheet": self.gs_name_var.get().strip() or "RCM_Provider_Leads",
            "auto_export": self.auto_export_var.get(),
            "verify_emails": self.verify_var.get(),
            "smtp_probe": self.smtp_var.get(),
            "guess_emails": self.guess_var.get(),
            "probe_sender": self.sender_var.get().strip(),
            "proxy": self.proxy_var.get().strip(),
            "pt_workers": int(self.workers_var.get()),
            "email_workers": int(self.workers_var.get()),
            "owners_only": self.owners_var.get(),
            "exclude_corporate": self.exclude_corp_var.get(),
            "resume_runs": self.resume_var.get(),
            "smtp_host": self.smtp_host_var.get().strip(),
            "smtp_port": int(self.smtp_port_var.get() or 587),
            "smtp_user": self.smtp_user_var.get().strip(),
            "from_name": self.from_name_var.get().strip(),
            "from_email": self.from_email_var.get().strip(),
            "postal_address": self.postal_var.get().strip(),
            "unsubscribe_email": self.unsub_var.get().strip(),
            "daily_send_cap": int(self.daily_cap_var.get() or 40),
        })
        self._save_smtp_password()
        year = self.min_year_var.get()
        self.settings["min_updated_year"] = None if year == "No Filter" else int(year)
        config.save(self.settings)
        self._refresh_compliance()
        self.banner.set("ok", "Settings saved.",
                        "They are stored in config.json and used by the command "
                        "line too.")

    # ------------------------------------------------------------- startup

    def _setup_logging(self):
        self.logger = logging.getLogger("LeadGen")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        handler = TextHandler(self.log_text)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s",
                                               datefmt="%H:%M:%S"))
        self.logger.addHandler(handler)
        _setup_file_logging(self.logger)
        self.logger.propagate = False

    def _startup(self):
        """Open the database and, the first time, bring an existing workbook in."""
        try:
            self.store = LeadStore(self.settings["db_path"], logger=self.logger)
        except Exception as e:
            self.banner.set("error", "Could not open the lead database.", str(e))
            return

        try:
            self.store.import_run_history(logger=self.logger)
        except Exception as e:
            self.logger.debug("Could not import run history: %s", e)

        excel_path = self.settings.get("excel_path")
        if not self.store.get_stats()["total"] and excel_path and os.path.exists(excel_path):
            self.banner.set("busy", "Importing your existing spreadsheet...",
                            "This happens once, so nothing collected before now "
                            "is left behind.")
            self.root.update_idletasks()
            try:
                imported = self.store.import_from_excel(excel_path, logger=self.logger)
                if imported:
                    self.logger.info(
                        "Imported %d leads from %s. The spreadsheet is now an "
                        "export of the database rather than the database itself.",
                        imported, os.path.basename(excel_path))
            except Exception as e:
                self.logger.warning("Could not import %s: %s", excel_path, e)

        self.refresh_results()
        self._refresh_resume_hint()
        stats = self.store.get_stats()
        if stats["total"]:
            self.banner.set(
                "idle", "Ready — {0} leads in the database.".format(stats["total"]),
                "{0} have an email address, {1} confirmed by a mail server.".format(
                    stats["with_email"], stats["verified_email"]))
        else:
            self.banner.set("idle", "Ready.",
                            "Nothing collected yet. Pick a source on the Collect "
                            "tab and press the button at the bottom.")

    # ------------------------------------------------------- primary action

    def _sync_primary(self):
        """The bottom button always says what it will do, based on where you
        are — one obvious action instead of six competing buttons."""
        # The tabs are built before the action bar, and building a tab syncs
        # the source picker, so this runs once before the button exists.
        if not hasattr(self, "primary_btn"):
            return
        if self.worker and self.worker.is_alive():
            return
        tab = self.notebook.index(self.notebook.select()) if self.notebook.tabs() else 0
        labels = {
            0: {"pt": "Collect from Psychology Today",
                "npi": "Collect from NPI Registry",
                "paste": "Look Up Pasted NPIs"}.get(self.source_var.get(), "Start"),
            1: {"find": "Find Missing Emails",
                "verify": "Verify Email Addresses",
                "cms": "Look Up Medicare Data"}.get(self.enrich_var.get(), "Start"),
            2: "Send Campaign",
            3: "Export Spreadsheet",
            4: "Save Settings",
        }
        self.primary_btn.configure(text=labels.get(tab, "Start"))

    def run_primary(self):
        tab = self.notebook.index(self.notebook.select())
        if tab == 0:
            self.start_collection()
        elif tab == 1:
            self.start_enrichment()
        elif tab == 2:
            self.start_send(dry_run=False)
        elif tab == 3:
            self.start_export()
        else:
            self.save_settings()

    # -------------------------------------------------------------- running

    def _begin(self, phase):
        self.stop_event = threading.Event()
        self.primary_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self.progress_var.set(0)
        self.progress_label.configure(text="")
        self.banner.set("busy", phase)

    def _end(self):
        self.primary_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self._sync_primary()
        self.refresh_results()
        self._refresh_resume_hint()

    def stop_run(self):
        self.stop_event.set()
        self.stop_btn.configure(state=tk.DISABLED)
        self.logger.info("Stop requested — finishing the items already in flight...")

    def _spawn(self, target, phase):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Already running",
                                "Wait for the current run to finish, or press Stop.")
            return
        self._begin(phase)
        self.worker = threading.Thread(target=self._guarded, args=(target,), daemon=True)
        self.worker.start()

    def _guarded(self, target):
        try:
            target()
        except Exception as e:
            self.logger.error("Unexpected error: %s\n%s", e, traceback.format_exc())
            self.root.after(0, lambda: self.banner.set(
                "error", "The run stopped with an unexpected error.", str(e)))
        finally:
            self.root.after(0, self._end)

    def _build_pipeline(self):
        """Fresh engines each run, so a proxy or worker change takes effect
        without restarting the app."""
        from npi_engine import NPIEngine
        from email_engine import EmailEngine
        from psychology_today_engine import PsychologyTodayEngine
        from verification import EmailVerifier
        from cms_engine import CMSEngine

        self.save_settings_quietly()
        verifier = None
        if self.settings.get("verify_emails"):
            verifier = EmailVerifier(logger=self.logger,
                                     probe_smtp=self.settings.get("smtp_probe", True),
                                     probe_sender=self.settings.get("probe_sender", ""))
        reporter = AppReporter(self, self.logger, self.stop_event)
        pipeline = LeadPipeline(
            store=self.store,
            npi_engine=NPIEngine(logger=self.logger),
            email_engine=EmailEngine(logger=self.logger),
            pt_engine=PsychologyTodayEngine(logger=self.logger,
                                            proxies=config.proxies_for(self.settings)),
            verifier=verifier,
            cms_engine=CMSEngine(logger=self.logger,
                                 proxies=config.proxies_for(self.settings)),
            reporter=reporter,
            settings=self.settings)
        pipeline.PT_WORKERS = int(self.settings.get("pt_workers", 5))
        pipeline.EMAIL_WORKERS = int(self.settings.get("email_workers", 5))
        return pipeline

    def save_settings_quietly(self):
        banner_state = (self.banner.title.cget("text"), self.banner.detail.cget("text"))
        self.save_settings()
        self.banner.set("busy", banner_state[0], banner_state[1])

    # ---------------------------------------------------------- run: collect

    def start_collection(self):
        source = self.source_var.get()
        if source == "pt":
            state_name = self.pt_state_var.get()
            state_abbr = catalog.STATES[state_name]
            owners_only = self.owners_var.get()
            resume = self.resume_var.get()

            def job():
                pipeline = self._build_pipeline()
                result = pipeline.collect_psychology_today(
                    state_name, state_abbr, owners_only=owners_only, resume=resume)
                self._after_run(pipeline, result, "Psychology Today — " + state_name)

            self._spawn(job, "Collecting from Psychology Today...")

        elif source == "npi":
            state = self.npi_state_var.get()
            states = (list(catalog.STATES.values()) if state == "All States"
                      else [catalog.STATES[state]])
            specialty = self.specialty_var.get()
            specialties = (list(catalog.SPECIALTIES.values())
                           if specialty == "All Specialties"
                           else [catalog.SPECIALTIES[specialty]])
            exclude_corp = self.exclude_corp_var.get()
            year = self.min_year_var.get()
            min_year = None if year == "No Filter" else int(year)

            def job():
                pipeline = self._build_pipeline()
                result = pipeline.collect_npi(states, specialties,
                                              exclude_corp=exclude_corp,
                                              min_year=min_year)
                self._after_run(pipeline, result, "NPI Registry")

            self._spawn(job, "Collecting from the NPI Registry...")

        else:
            numbers = LeadPipeline.extract_npi_numbers(self.npi_paste.get("1.0", tk.END))
            if not numbers:
                messagebox.showerror("Nothing to look up",
                                     "No NPI numbers were found in the pasted text.")
                return

            def job():
                pipeline = self._build_pipeline()
                result = pipeline.import_npis(numbers)
                self._after_run(pipeline, result, "Pasted NPI numbers")

            self._spawn(job, "Looking up {0} NPI numbers...".format(len(numbers)))

    def start_enrichment(self):
        mode = self.enrich_var.get()
        recheck = self.recheck_var.get()

        if mode == "find":
            def job():
                pipeline = self._build_pipeline()
                result = pipeline.find_missing_emails()
                self._after_run(pipeline, result, "Email discovery")
            self._spawn(job, "Finding missing email addresses...")
        elif mode == "cms":
            def job():
                pipeline = self._build_pipeline()
                result = pipeline.enrich_from_cms(recheck=recheck)
                self._after_run(pipeline, result, "Medicare data")
            self._spawn(job, "Looking up Medicare billing and practice data...")
        else:
            def job():
                self.settings["verify_emails"] = True
                pipeline = self._build_pipeline()
                result = pipeline.verify_emails(recheck=recheck)
                self._after_run(pipeline, result, "Email verification")
            self._spawn(job, "Verifying email addresses...")

    def start_export(self):
        min_score = int(self.min_score_var.get() or 0) or None

        def job():  # options are read on the Tk thread above, not in here
            self.root.after(0, lambda: self.banner.set("busy", "Exporting..."))
            written = self._export(min_score=min_score)
            self.root.after(0, lambda: self.banner.set(
                "ok" if written else "warn",
                "Exported {0} leads.".format(written) if written
                else "Nothing matched the export filter.",
                self._export_target()))
        self._spawn(job, "Exporting...")

    # ----------------------------------------------------------- run: after

    def _after_run(self, pipeline, result, label):
        """Report the outcome honestly — including the outcomes that are not
        failures but are not successes either."""
        if self.settings.get("auto_export") and result.added:
            try:
                self._export()
            except Exception as e:
                self.logger.error("Export failed: %s", e)

        def show():
            if result.status == RUN_FAILED:
                remedy = getattr(result.error, "remedy", "") or (
                    "Run Health Check to see which part is failing.")
                self.banner.set("error", label + " failed.",
                                (result.message or "") + "  " + remedy)
            elif result.status == RUN_EMPTY:
                self.banner.set(
                    "warn", label + " finished but collected nothing.",
                    (result.message or "") +
                    "  If that is unexpected, press Health Check — a source that "
                    "has quietly broken looks exactly like this.")
            elif result.status == RUN_STOPPED:
                self.banner.set("warn", label + " stopped.",
                                "{0} leads were saved. {1}".format(
                                    result.added, result.message or ""))
            else:
                self.banner.set(
                    "ok", "{0}: {1} new leads.".format(label, result.added),
                    result.message or self._export_target())
        self.root.after(0, show)

    def _export_target(self):
        if self.settings.get("output") == "google":
            return "Written to Google Sheets."
        return "Written to " + os.path.abspath(self.settings.get("excel_path")) + "."

    def _export(self, min_score=None):
        call_list = int(self.call_list_var.get() or 0)
        icp_only = self.icp_only_var.get()
        if self.settings.get("output") == "google":
            from lead_manager import GoogleSheetsExporter
            credentials = self.settings.get("google_credentials")
            if not credentials or not os.path.exists(credentials):
                raise RuntimeError("Select a valid Google service-account JSON file "
                                   "in Settings.")
            exporter = GoogleSheetsExporter(credentials,
                                            self.settings.get("google_spreadsheet"),
                                            logger=self.logger)
        else:
            from lead_manager import ExcelExporter
            exporter = ExcelExporter(self.settings.get("excel_path"), logger=self.logger)
            return exporter.export(self.store, min_score=min_score,
                                   call_list=call_list, call_list_icp=icp_only)
        return exporter.export(self.store, min_score=min_score)

    # ------------------------------------------------------- live UI updates

    def set_progress(self, done, total):
        if total:
            self.progress_var.set(done / total * 100)
            self.progress_label.configure(text="{0} / {1}".format(done, total))
        else:
            self.progress_label.configure(text=str(done))

    def bump_counts(self, sheet_key, leads=0, emails=0):
        bucket = self.sheet_totals.setdefault(sheet_key, {"leads": 0, "emails": 0})
        bucket["leads"] += leads
        bucket["emails"] += emails
        self._refresh_row(sheet_key, bucket)
        self._refresh_metrics_from_totals()

    def _refresh_row(self, sheet_key, bucket):
        rate = (bucket["emails"] / bucket["leads"] * 100) if bucket["leads"] else 0.0
        values = (bucket["leads"], bucket["emails"], "{0:.1f}%".format(rate))
        if sheet_key in self.tree_items:
            self.tree.item(self.tree_items[sheet_key], values=values)
        else:
            self.tree_items[sheet_key] = self.tree.insert(
                "", tk.END, text=sheet_key, values=values)

    def _refresh_metrics_from_totals(self):
        total = sum(b["leads"] for b in self.sheet_totals.values())
        emails = sum(b["emails"] for b in self.sheet_totals.values())
        self.metric_vars["total"].set(str(total))
        self.metric_vars["with_email"].set(str(emails))

    def refresh_results(self):
        """Re-read everything from the database. Cheap — it is all indexed."""
        if not self.store:
            return
        stats = self.store.get_stats()

        for key in ("total", "with_email", "verified_email", "with_phone",
                    "with_billing"):
            self.metric_vars[key].set(str(stats.get(key, 0)))
        self.metric_vars["avg_score"].set(str(stats.get("avg_score", 0)))

        self.tree.delete(*self.tree.get_children())
        self.tree_items.clear()
        self.sheet_totals.clear()
        for sheet_key, values in sorted(stats["by_source"].items()):
            bucket = {"leads": values["total"], "emails": values["with_email"]}
            self.sheet_totals[sheet_key] = bucket
            self._refresh_row(sheet_key, bucket)

        self.status_tree.delete(*self.status_tree.get_children())
        for status, count in sorted(stats["by_email_status"].items(),
                                    key=lambda kv: -kv[1]):
            self.status_tree.insert("", tk.END, text=status, values=(count,))

        missing = self.store.count(missing_email=True)
        unverified = self.store.count(unverified=True)
        self.missing_label.configure(
            text="{0} leads have no email address yet.".format(missing))
        self.unverified_label.configure(
            text="{0} addresses have not been checked yet.".format(unverified))
        needs_cms = self.store.count(needs_cms=True)
        self.cms_label.configure(
            text="{0} providers with a real NPI have not been looked up yet.".format(
                needs_cms))
        self._refresh_compliance()

    # ----------------------------------------------------------- extra views

    def run_health_check(self):
        def job():
            self.root.after(0, lambda: self.banner.set(
                "busy", "Checking every source..."))
            reports = health.run_all(proxies=config.proxies_for(self.settings),
                                     logger=self.logger,
                                     state=self.pt_state_var.get())
            self.root.after(0, lambda: self._show_health(reports))
        self._spawn(job, "Running health check...")

    def _show_health(self, reports):
        failed = [r["source"] for r in reports if not r["ok"]]
        if failed:
            self.banner.set("error", "Not working: " + ", ".join(failed),
                            "See the details below before starting a run.")
        else:
            degraded = [r["source"] for r in reports if r.get("degraded")]
            self.banner.set(
                "ok", "All sources are working.",
                "Reduced capability: " + ", ".join(degraded) if degraded else "")

        window = tk.Toplevel(self.root)
        window.title("Health Check")
        window.geometry("720x520")
        window.configure(bg=SURFACE)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill=tk.BOTH, expand=True)

        for report in reports:
            tone = "ok" if report["ok"] else "error"
            if report.get("degraded"):
                tone = "warn"
            background, foreground = TONES[tone]
            header = tk.Label(frame, text=report["source"], anchor="w",
                              font=("Segoe UI", 10, "bold"),
                              bg=background, fg=foreground, padx=10, pady=5)
            header.pack(fill=tk.X, pady=(10, 2))
            for step in report["steps"]:
                mark = "OK  " if step["ok"] else "FAIL"
                ttk.Label(frame, text="  [{0}]  {1}".format(mark, step["name"]),
                          font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
                ttk.Label(frame, text="        " + step["detail"], style="Muted.TLabel",
                          wraplength=650, justify="left").pack(anchor=tk.W)

    def show_run_history(self):
        window = tk.Toplevel(self.root)
        window.title("Run History")
        window.geometry("880x420")
        tree = ttk.Treeview(window,
                            columns=("action", "status", "added", "note"),
                            show="tree headings")
        tree.heading("#0", text="When")
        tree.heading("action", text="Action")
        tree.heading("status", text="Outcome")
        tree.heading("added", text="Added")
        tree.heading("note", text="Note")
        tree.column("#0", width=150)
        tree.column("action", width=110)
        tree.column("status", width=90, anchor=tk.CENTER)
        tree.column("added", width=70, anchor=tk.CENTER)
        tree.column("note", width=420)
        tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        tree.tag_configure("failed", foreground="#a3241c")
        tree.tag_configure("empty", foreground="#8a5310")

        for run in self.store.recent_runs(50):
            status = (run["status"] or "").lower()
            tree.insert("", tk.END,
                        text=(run["started_at"] or "")[:19].replace("T", " "),
                        values=(run["action"], status.upper(), run["added"] or 0,
                                run["message"] or ""),
                        tags=(status,) if status in ("failed", "empty") else ())

    def open_result(self):
        try:
            if self.settings.get("output") == "google":
                from lead_manager import GoogleSheetsExporter
                exporter = GoogleSheetsExporter(
                    self.settings.get("google_credentials"),
                    self.settings.get("google_spreadsheet"), logger=self.logger)
                webbrowser.open(exporter.url)
                return
            path = self.settings.get("excel_path")
            if not os.path.exists(path):
                if not messagebox.askyesno(
                        "Not exported yet",
                        "The spreadsheet has not been written yet. Export it now?"):
                    return
                self._export()
            _open_in_default_app(os.path.abspath(path))
        except Exception as e:
            messagebox.showerror("Could not open", str(e))

    def show_dashboard(self):
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            import matplotlib.pyplot as plt
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except ImportError:
            messagebox.showerror(
                "Dashboard unavailable",
                "matplotlib is required.\nInstall it with: pip install matplotlib")
            return
        if not self.store:
            return

        stats = self.store.get_stats()
        runs = list(reversed(self.store.recent_runs(15)))

        # Single sequential hue: these are magnitude and ranking charts, not
        # identity charts, so colour carries no extra meaning.
        BLUE = "#2a78d6"
        GRID = "#e1e0d9"
        AXIS = "#c3c2b7"
        PAPER = "#fcfcfb"

        window = tk.Toplevel(self.root)
        window.title("Analytics Dashboard")
        window.geometry("1050x780")
        window.configure(bg=PAPER)

        total = stats["total"] or 1
        ttk.Label(window,
                  text="{0} leads   |   {1} with email ({2:.1f}%)   |   "
                       "{3} verified   |   average score {4}".format(
                           stats["total"], stats["with_email"],
                           stats["with_email"] / total * 100,
                           stats["verified_email"], stats["avg_score"]),
                  font=("Segoe UI", 10, "bold")).pack(pady=8)

        figure = plt.Figure(figsize=(11, 8), dpi=100, facecolor=PAPER)
        figure.subplots_adjust(hspace=0.65, wspace=0.4, left=0.16, right=0.96,
                               top=0.94, bottom=0.07)
        axes = [figure.add_subplot(2, 2, i) for i in range(1, 5)]

        def style(axis, title):
            axis.set_facecolor(PAPER)
            axis.set_title(title, fontsize=10, color=INK, loc="left", fontweight="bold")
            axis.tick_params(colors=INK_SOFT, labelsize=8)
            for spine in ("top", "right"):
                axis.spines[spine].set_visible(False)
            for spine in ("left", "bottom"):
                axis.spines[spine].set_color(AXIS)

        def empty(axis, text):
            axis.text(0.5, 0.5, text, ha="center", va="center", color=INK_FAINT,
                      fontsize=9, transform=axis.transAxes)

        top_states = sorted(stats["by_state"].items(),
                            key=lambda kv: kv[1]["total"], reverse=True)[:15]
        if top_states:
            labels = [s for s, _ in reversed(top_states)]
            axes[0].barh(labels, [d["total"] for _, d in reversed(top_states)],
                         color=BLUE, height=0.6, zorder=3)
            axes[0].grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
            axes[0].set_axisbelow(True)
        else:
            empty(axes[0], "No data yet")
        style(axes[0], "Leads by state (top 15)")

        if top_states:
            rates = [(d["with_email"] / d["total"] * 100) if d["total"] else 0
                     for _, d in reversed(top_states)]
            axes[1].barh([s for s, _ in reversed(top_states)], rates,
                         color=BLUE, height=0.6, zorder=3)
            axes[1].set_xlim(0, 100)
            axes[1].grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
            axes[1].set_axisbelow(True)
        else:
            empty(axes[1], "No data yet")
        style(axes[1], "Email hit rate % (same states)")

        if runs:
            xs = list(range(1, len(runs) + 1))
            ys = [r["hit_rate"] or 0 for r in runs]
            axes[2].plot(xs, ys, color=BLUE, marker="o", markersize=4, linewidth=2,
                         zorder=3)
            axes[2].set_ylim(0, 100)
            axes[2].grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
            axes[2].set_axisbelow(True)
            axes[2].set_xticks(xs)
        else:
            empty(axes[2], "No run history yet")
        style(axes[2], "Email hit rate trend (recent runs)")

        statuses = sorted(stats["by_email_status"].items(), key=lambda kv: kv[1])
        if statuses:
            axes[3].barh([s for s, _ in statuses], [c for _, c in statuses],
                         color=BLUE, height=0.6, zorder=3)
            axes[3].grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
            axes[3].set_axisbelow(True)
        else:
            empty(axes[3], "No addresses checked yet")
        style(axes[3], "Email addresses by verification status")

        canvas = FigureCanvasTkAgg(figure, master=window)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)


def _open_in_default_app(path):
    """Open a file the way the desktop would. os.startfile is Windows-only, so
    calling it directly would crash the app the moment this runs anywhere
    else — including the Linux VPS that is the natural home for scheduled runs."""
    import subprocess
    import sys

    if hasattr(os, "startfile"):          # Windows
        os.startfile(path)                # noqa: S606 - opening the user's own file
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


def _setup_file_logging(logger):
    """Also write the log to logs/leadgen.log.

    The activity pane is wiped when the window closes, so a run that crashed
    overnight left nothing to look at. A rotating file keeps the last few runs
    without growing without limit.
    """
    from logging.handlers import RotatingFileHandler

    try:
        os.makedirs("logs", exist_ok=True)
        handler = RotatingFileHandler(
            os.path.join("logs", "leadgen.log"), maxBytes=2_000_000,
            backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(name)s  %(message)s"))
        logger.addHandler(handler)
        return handler
    except OSError as e:
        logger.warning("Could not open the log file: %s", e)
        return None


def _status_colour(status):
    from lead_manager import STATUS_FILLS
    return "#" + STATUS_FILLS.get(status, "F2F2F2")


if __name__ == "__main__":
    root = tk.Tk()
    LeadGenApp(root)
    root.mainloop()
