"""Medical Provider Lead Generation System — desktop app.

    pip install -r requirements.txt
    python app.py

The window is a front end and nothing more. Every decision it used to make
itself — which phone number to trust, how to split a name, when a run has
failed — lives in pipeline.py, so the same work runs from cli.py, on a
schedule, or in a test.

The layout answers the question a new user actually has, which is not "where is
the feature" but "what do I do now". Home opens with the pipeline's real state
and one recommended action. The rail down the left is the order the work
happens in. Every screen carries its own guidance behind the Guide button, so
the knowledge that used to live only in a markdown file lives where the work
happens.
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
import ui_help
import ui_theme as T
from storage import LeadStore, RUN_OK, RUN_EMPTY, RUN_STOPPED, RUN_FAILED
from pipeline import LeadPipeline, Reporter
# Imported at module scope on purpose. These used to load lazily on a worker
# thread, and a module import running on a worker while the main thread imports
# something else can deadlock CPython's import machinery — a hang with no
# traceback.
#
# Two deliberate exceptions, both still imported only on the main thread:
# lead_manager, which drags in openpyxl and numpy for over a second of startup
# and is only needed to write a spreadsheet (see _load_exporters, called before
# any run's worker starts), and matplotlib, which only the analytics window uses.
from npi_engine import NPIEngine
from email_engine import EmailEngine
from psychology_today_engine import PsychologyTodayEngine
from cms_engine import CMSEngine
from verification import EmailVerifier


class TextHandler(logging.Handler):
    """Routes log records to the activity pane.

    Records arrive on worker threads, so they go through a queue that the Tk
    main loop drains. Touching a widget from a worker thread is the classic way
    to make Tk deadlock, and this is the busiest path in the app.
    """

    def __init__(self, widget):
        super().__init__()
        self.widget = widget
        self.queue = queue.Queue()
        self._stopped = False
        self.widget.after(120, self._drain)

    def emit(self, record):
        try:
            self.queue.put_nowait((self.format(record), record.levelname))
        except Exception:
            pass

    def close(self):
        """Stop the drain loop. Called when the window goes away."""
        self._stopped = True
        super().close()

    def _drain(self):
        """Flush queued records, then schedule the next pass.

        The reschedule has to stop when the widget is destroyed. This loop
        re-arms every 120ms forever, so without the guard a closed window
        leaves callbacks firing at a widget that no longer exists.
        """
        if self._stopped:
            return
        try:
            if not self.widget.winfo_exists():
                self._stopped = True
                return
        except tk.TclError:
            self._stopped = True
            return

        pending = []
        while True:
            try:
                pending.append(self.queue.get_nowait())
            except queue.Empty:
                break
        try:
            self._render(pending)
            self.widget.after(120, self._drain)
        except tk.TclError:
            self._stopped = True

    def _render(self, pending):
        if not pending:
            return
        self.widget.configure(state="normal")
        for message, level in pending:
            tag = level.lower() if level in ("WARNING", "ERROR") else ""
            self.widget.insert(tk.END, message + "\n", tag)
        # Keep the pane bounded: a full-state run emits tens of thousands of
        # lines, and Tk slows to a crawl long before that is readable.
        line_count = int(self.widget.index("end-1c").split(".")[0])
        if line_count > 4000:
            self.widget.delete("1.0", str(line_count - 3000) + ".0")
        self.widget.see(tk.END)
        self.widget.configure(state="disabled")


class StatusBanner(tk.Frame):
    """The one place the app says how things are going.

    It exists because the old version reported a broken scraper and a finished
    run identically — a box saying "Collection finished". A run that collected
    nothing now turns this amber, and a failure turns it red with the remedy.
    """

    def __init__(self, master):
        super().__init__(master, bd=0, highlightthickness=1,
                         highlightbackground=T.RULE)
        self.title = tk.Label(self, anchor="w", font=(T.FONT, 10, "bold"))
        self.title.pack(fill=tk.X, padx=14, pady=(9, 0))
        self.detail = tk.Label(self, anchor="w", justify="left", font=T.F_SMALL,
                               wraplength=880)
        self.detail.pack(fill=tk.X, padx=14, pady=(2, 9))
        self.set("idle", "Ready.", "")

    def set(self, tone, title, detail=""):
        background, foreground = T.TONES.get(tone, T.TONES["idle"])
        self.configure(bg=background)
        for widget in (self.title, self.detail):
            widget.configure(bg=background, fg=foreground)
        self.title.configure(text=title)
        self.detail.configure(text=detail)
        if detail:
            self.detail.pack(fill=tk.X, padx=14, pady=(2, 9))
        else:
            self.detail.pack_forget()


class AppReporter(Reporter):
    """Pipeline callbacks, marshalled onto the Tk thread."""

    def __init__(self, app, logger, stop_event):
        super().__init__(logger, stop_event)
        self.app = app

    def progress(self, done, total):
        self.app.post(self.app.set_progress, done, total)

    def counts(self, sheet_key, leads=0, emails=0):
        self.app.post(self.app.bump_counts, sheet_key, leads, emails)

    def phase(self, text):
        self.log(text)
        self.app.post(lambda: self.app.banner.set("busy", text))


# The order the work actually happens in. The rail follows it, and so does the
# numbering, because doing Enrich before Collect produces nothing.
SCREENS = (
    ("home", "Home", "Overview and what to do next"),
    ("collect", "Collect", "Pull leads from a source"),
    ("enrich", "Enrich", "Fill gaps and verify"),
    ("outreach", "Outreach", "Email the ones worth contacting"),
    ("results", "Results", "Read and export"),
    ("settings", "Settings", "Accounts and preferences"),
)


class LeadGenApp:
    WINDOW_TITLE = "Medical Provider Lead Generation System"

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

    def __init__(self, root):
        self.root = root
        self.root.title(self.WINDOW_TITLE)
        self.root.geometry("1180x860")
        self.root.minsize(1000, 720)

        self.settings = config.load()
        self.store = None
        self.stop_event = threading.Event()
        self.worker = None
        self.sheet_totals = {}
        self.tree_items = {}
        self.screen = "home"
        self.screens = {}
        self.nav_buttons = {}
        self.help_open = False
        self._next_action = None
        # The database fingerprint the screens were last drawn from. refresh()
        # skips the redraw when it has not changed.
        self._shown_version = None

        T.install(self.root)
        self._build()
        self._setup_logging()

        # Work done on worker threads reaches the window only through this
        # queue, which the Tk thread empties on its own timer. Workers used to
        # call root.after() directly. That is itself a call into Tk, and it only
        # works while the main thread is sitting inside mainloop: close the
        # window mid-run, or drive the app without mainloop as the tests do,
        # and it raises "main thread is not in main loop" and the update is lost.
        self._ui_queue = queue.Queue()
        self._ui_closed = False
        self._pump_ui()

        self.root.after(50, self._startup)

    UI_PUMP_MS = 50

    def post(self, fn, *args):
        """Run fn(*args) on the Tk thread soon. Safe to call from any thread —
        it touches nothing but a queue."""
        self._ui_queue.put((fn, args))

    def _pump_ui(self):
        """Run whatever the workers have posted, then check again shortly.

        Stops for good once the window is gone, so a run that outlives its
        window cannot keep calling into a destroyed Tk.
        """
        if self._ui_closed:
            return
        try:
            if not self.root.winfo_exists():
                self._ui_closed = True
                return
        except tk.TclError:
            self._ui_closed = True
            return

        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except tk.TclError:
                self._ui_closed = True     # destroyed while this was queued
                return
            except Exception:
                logging.getLogger("LeadGen").error(
                    "A window update failed:\n%s", traceback.format_exc())

        try:
            self.root.after(self.UI_PUMP_MS, self._pump_ui)
        except tk.TclError:
            self._ui_closed = True

    # ===================================================================
    # Chrome
    # ===================================================================

    def _build(self):
        outer = tk.Frame(self.root, bg=T.GROUND)
        outer.pack(fill=tk.BOTH, expand=True)

        self._build_nav(outer)

        right = tk.Frame(outer, bg=T.GROUND)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._build_topbar(right)

        body = tk.Frame(right, bg=T.GROUND)
        body.pack(fill=tk.BOTH, expand=True)

        self.stage = tk.Frame(body, bg=T.GROUND)
        self.stage.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
                        padx=(20, 20), pady=(0, 0))

        self._build_help_drawer(body)

        self._build_screens()
        self._build_action_bar(right)
        self._build_log(right)

        self.show("home")

    def _build_nav(self, parent):
        rail = tk.Frame(parent, bg=T.NAV_BG, width=214)
        rail.pack(side=tk.LEFT, fill=tk.Y)
        rail.pack_propagate(False)

        brand = tk.Frame(rail, bg=T.NAV_BG)
        brand.pack(fill=tk.X, padx=18, pady=(20, 18))
        tk.Label(brand, text="Provider Leads", font=(T.FONT, 13, "bold"),
                 bg=T.NAV_BG, fg="#ffffff", anchor="w").pack(fill=tk.X)
        tk.Label(brand, text="Lead generation system", font=T.F_TINY,
                 bg=T.NAV_BG, fg=T.NAV_MUTED, anchor="w").pack(fill=tk.X)

        tk.Frame(rail, height=1, bg=T.NAV_RULE).pack(fill=tk.X, padx=14)

        items = tk.Frame(rail, bg=T.NAV_BG)
        items.pack(fill=tk.X, pady=(12, 0))

        for index, (key, label, _note) in enumerate(SCREENS):
            row = tk.Frame(items, bg=T.NAV_BG, cursor="hand2")
            row.pack(fill=tk.X, padx=10, pady=1)

            # Settings is not a stage of the work, so it gets no number.
            number = "" if key == "settings" else str(index)
            marker = tk.Label(row, text=number, font=(T.MONO, 8), width=2,
                              bg=T.NAV_BG, fg=T.NAV_MUTED)
            marker.pack(side=tk.LEFT, padx=(8, 4), pady=8)
            text = tk.Label(row, text=label, font=(T.FONT, 10), bg=T.NAV_BG,
                            fg=T.NAV_TEXT, anchor="w")
            text.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=8)

            for widget in (row, marker, text):
                widget.bind("<Button-1>", lambda _e, k=key: self.show(k))
                widget.bind("<Enter>", lambda _e, r=row, k=key: self._nav_hover(r, k, True))
                widget.bind("<Leave>", lambda _e, r=row, k=key: self._nav_hover(r, k, False))

            self.nav_buttons[key] = (row, marker, text)

        footer = tk.Frame(rail, bg=T.NAV_BG)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=14, pady=16)
        tk.Frame(footer, height=1, bg=T.NAV_RULE).pack(fill=tk.X, pady=(0, 12))
        tk.Label(footer, text="NEXT STEP", font=(T.FONT, 7, "bold"),
                 bg=T.NAV_BG, fg=T.NAV_MUTED, anchor="w").pack(fill=tk.X)
        self.nav_next = tk.Label(footer, text="Checking...", font=T.F_SMALL,
                                 bg=T.NAV_BG, fg="#ffffff", anchor="w",
                                 justify="left", wraplength=170)
        self.nav_next.pack(fill=tk.X, pady=(3, 0))

    def _nav_hover(self, row, key, entering):
        if key == self.screen:
            return
        colour = T.NAV_BG_HOVER if entering else T.NAV_BG
        row.configure(bg=colour)
        for child in row.winfo_children():
            child.configure(bg=colour)

    def _build_topbar(self, parent):
        bar = tk.Frame(parent, bg=T.GROUND)
        bar.pack(fill=tk.X, padx=20, pady=(18, 10))

        titles = tk.Frame(bar, bg=T.GROUND)
        titles.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.screen_title = tk.Label(titles, text="Home", font=T.F_TITLE,
                                     bg=T.GROUND, fg=T.INK, anchor="w")
        self.screen_title.pack(fill=tk.X)
        self.screen_note = tk.Label(titles, text="", font=T.F_SMALL, bg=T.GROUND,
                                    fg=T.INK_FAINT, anchor="w")
        self.screen_note.pack(fill=tk.X)

        buttons = tk.Frame(bar, bg=T.GROUND)
        buttons.pack(side=tk.RIGHT)
        self.help_btn = ttk.Button(buttons, text="Guide", style="Ghost.TButton",
                                   command=self.toggle_help)
        self.help_btn.pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(buttons, text="Health Check", style="Ghost.TButton",
                   command=self.run_health_check).pack(side=tk.RIGHT, padx=(6, 0))

        self.banner = StatusBanner(parent)
        self.banner.pack(fill=tk.X, padx=20, pady=(0, 14))

    def _build_help_drawer(self, parent):
        """A panel that slides in beside the content rather than a modal, so
        you can read the guidance and use the controls at the same time."""
        self.help_panel = tk.Frame(parent, bg=T.SURFACE, width=330,
                                   highlightthickness=1,
                                   highlightbackground=T.RULE)
        head = tk.Frame(self.help_panel, bg=T.SURFACE)
        head.pack(fill=tk.X, padx=16, pady=(14, 6))
        self.help_title = tk.Label(head, text="Guide", font=T.F_HEAD,
                                   bg=T.SURFACE, fg=T.INK, anchor="w")
        self.help_title.pack(side=tk.LEFT)
        tk.Label(head, text="✕", font=(T.FONT, 10), bg=T.SURFACE, fg=T.INK_FAINT,
                 cursor="hand2").pack(side=tk.RIGHT)
        head.winfo_children()[-1].bind("<Button-1>", lambda _e: self.toggle_help())

        outer, self.help_body = T.scrollable(self.help_panel, bg=T.SURFACE)
        outer.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 10))

        foot = tk.Frame(self.help_panel, bg=T.SURFACE)
        foot.pack(fill=tk.X, padx=16, pady=(0, 14))
        tk.Frame(foot, height=1, bg=T.RULE).pack(fill=tk.X, pady=(0, 10))
        ttk.Button(foot, text="Open the full handbook", style="Ghost.TButton",
                   command=self.open_guide).pack(fill=tk.X)

    def toggle_help(self):
        if self.help_open:
            self.help_panel.pack_forget()
            self.help_open = False
            self.help_btn.configure(text="Guide")
        else:
            self.help_panel.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 20),
                                 pady=(0, 6))
            self.help_panel.pack_propagate(False)
            self.help_open = True
            self.help_btn.configure(text="Hide guide")
            self._render_help()

    def _render_help(self):
        for child in self.help_body.winfo_children():
            child.destroy()

        heading, blocks = ui_help.for_screen(self.screen)
        self.help_title.configure(text=heading)

        for block in blocks:
            if isinstance(block, str):
                tk.Label(self.help_body, text=block, font=T.F_SMALL,
                         bg=T.SURFACE, fg=T.INK_SOFT, anchor="w",
                         justify="left", wraplength=282).pack(
                             fill=tk.X, padx=12, pady=(0, 10))
                continue

            kind, payload = block
            if kind == "list":
                for item in payload:
                    row = tk.Frame(self.help_body, bg=T.SURFACE)
                    row.pack(fill=tk.X, padx=12, pady=(0, 7))
                    tk.Label(row, text="—", font=T.F_SMALL, bg=T.SURFACE,
                             fg=T.INK_FAINT).pack(side=tk.LEFT, anchor="n",
                                                  padx=(0, 6))
                    tk.Label(row, text=item, font=T.F_SMALL, bg=T.SURFACE,
                             fg=T.INK_SOFT, anchor="w", justify="left",
                             wraplength=258).pack(side=tk.LEFT, fill=tk.X)
            else:
                fg, bg = {"warn": (T.WARN, T.WARN_BG),
                          "tip": (T.INFO, T.INFO_BG)}.get(kind, (T.INK, T.MUTED_BG))
                box = tk.Frame(self.help_body, bg=bg, highlightthickness=1,
                               highlightbackground=bg)
                box.pack(fill=tk.X, padx=12, pady=(0, 10))
                tk.Label(box, text=("IMPORTANT" if kind == "warn" else "TIP"),
                         font=(T.FONT, 7, "bold"), bg=bg, fg=fg,
                         anchor="w").pack(fill=tk.X, padx=10, pady=(8, 0))
                tk.Label(box, text=payload, font=T.F_SMALL, bg=bg, fg=fg,
                         anchor="w", justify="left", wraplength=262).pack(
                             fill=tk.X, padx=10, pady=(2, 9))

    def open_guide(self):
        path = os.path.abspath("GUIDE.md")
        if os.path.exists(path):
            _open_in_default_app(path)
        else:
            messagebox.showinfo(
                "Handbook",
                "GUIDE.md is not in the project folder. The guidance for each "
                "screen is in this panel.")

    def _build_action_bar(self, parent):
        bar = tk.Frame(parent, bg=T.GROUND)
        bar.pack(fill=tk.X, padx=20, pady=(10, 10))

        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(bar, variable=self.progress_var,
                                        maximum=100,
                                        style="Thin.Horizontal.TProgressbar")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=10)

        self.progress_label = tk.Label(bar, text="", font=T.F_SMALL, bg=T.GROUND,
                                       fg=T.INK_FAINT, width=16, anchor="e")
        self.progress_label.pack(side=tk.LEFT, padx=12)

        self.stop_btn = ttk.Button(bar, text="Stop", command=self.stop_run,
                                   state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT)
        self.primary_btn = ttk.Button(bar, text="Start", style="Primary.TButton",
                                      command=self.run_primary)
        self.primary_btn.pack(side=tk.RIGHT, padx=10)

    def _build_log(self, parent):
        wrap = tk.Frame(parent, bg=T.GROUND)
        wrap.pack(fill=tk.X, padx=20, pady=(0, 14))

        header = tk.Frame(wrap, bg=T.GROUND, cursor="hand2")
        header.pack(fill=tk.X)
        self.log_toggle = tk.Label(header, text="▸  Activity", font=T.F_LABEL,
                                   bg=T.GROUND, fg=T.INK_FAINT, anchor="w")
        self.log_toggle.pack(side=tk.LEFT)
        self.log_tail = tk.Label(header, text="", font=T.F_SMALL, bg=T.GROUND,
                                 fg=T.INK_FAINT, anchor="e")
        self.log_tail.pack(side=tk.RIGHT)
        for widget in (header, self.log_toggle, self.log_tail):
            widget.bind("<Button-1>", lambda _e: self.toggle_log())

        self.log_frame = tk.Frame(wrap, bg=T.GROUND)
        self.log_text = scrolledtext.ScrolledText(
            self.log_frame, state="disabled", height=9, wrap="word",
            font=T.F_MONO, background=T.SURFACE_SUNK, relief="flat",
            borderwidth=0, highlightthickness=1, highlightbackground=T.RULE)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.tag_configure("warning", foreground=T.WARN)
        self.log_text.tag_configure("error", foreground=T.BAD)
        self.log_open = False

    def toggle_log(self):
        if self.log_open:
            self.log_frame.pack_forget()
            self.log_toggle.configure(text="▸  Activity")
        else:
            self.log_frame.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
            self.log_toggle.configure(text="▾  Activity")
        self.log_open = not self.log_open

    # ===================================================================
    # Navigation
    # ===================================================================

    def show(self, key):
        self.screen = key
        for name, (row, marker, text) in self.nav_buttons.items():
            active = (name == key)
            colour = T.NAV_ACTIVE if active else T.NAV_BG
            row.configure(bg=colour)
            marker.configure(bg=colour,
                             fg="#ffffff" if active else T.NAV_MUTED)
            text.configure(bg=colour,
                           fg=T.NAV_TEXT_ACTIVE if active else T.NAV_TEXT,
                           font=(T.FONT, 10, "bold") if active else (T.FONT, 10))

        for name, frame in self.screens.items():
            frame.pack_forget() if name != key else frame.pack(
                fill=tk.BOTH, expand=True)

        label, note = next((l, n) for k, l, n in SCREENS if k == key)
        self.screen_title.configure(text=label)
        self.screen_note.configure(text=note)

        if self.help_open:
            self._render_help()
        self._sync_primary()
        if key in ("home", "results", "enrich", "outreach"):
            self.refresh()

    # ===================================================================
    # Screens
    # ===================================================================

    def _build_screens(self):
        self.screens["home"] = self._build_home()
        self.screens["collect"] = self._build_collect()
        self.screens["enrich"] = self._build_enrich()
        self.screens["outreach"] = self._build_outreach()
        self.screens["results"] = self._build_results()
        self.screens["settings"] = self._build_settings()

    # ------------------------------------------------------------ home

    def _build_home(self):
        outer, page = T.scrollable(self.stage)

        # Next step. The single most useful thing on the screen: it reads the
        # database and names the stage that is actually unfinished, so there is
        # never a question of what to press.
        self.next_card = T.Card(page, padding=18)
        self.next_card.pack(fill=tk.X, pady=(0, 14))
        inner = self.next_card.inner
        tk.Label(inner, text="NEXT STEP", font=(T.FONT, 7, "bold"),
                 bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").pack(fill=tk.X)
        self.next_title = tk.Label(inner, text="Checking your data…",
                                   font=(T.FONT, 14, "bold"), bg=T.SURFACE,
                                   fg=T.INK, anchor="w")
        self.next_title.pack(fill=tk.X, pady=(4, 2))
        self.next_note = tk.Label(inner, text="", font=T.F_SMALL, bg=T.SURFACE,
                                  fg=T.INK_SOFT, anchor="w", justify="left",
                                  wraplength=640)
        self.next_note.pack(fill=tk.X)
        self.next_btn = ttk.Button(inner, text="Go", style="Primary.TButton",
                                   command=self._do_next_action)
        self.next_btn.pack(anchor="w", pady=(12, 0))

        # Funnel. Stages in the order leads pass through them, so a stage much
        # narrower than the one above it is visibly where they are being lost.
        funnel_card = T.Card(page, padding=18)
        funnel_card.pack(fill=tk.X, pady=(0, 14))
        tk.Label(funnel_card.inner, text="YOUR PIPELINE", font=(T.FONT, 7, "bold"),
                 bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").pack(fill=tk.X,
                                                                pady=(0, 12))
        self.funnel_rows = {}
        for key, label, colour in (
                ("total", "Collected", T.BRAND),
                ("with_phone", "Have a phone number", T.INFO),
                ("with_email", "Have an email address", T.BRAND),
                ("verified_email", "Email verified", T.OK),
                ("contacted", "Contacted", T.WARN)):
            row = tk.Frame(funnel_card.inner, bg=T.SURFACE)
            row.pack(fill=tk.X, pady=3)
            tk.Label(row, text=label, font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_SOFT,
                     width=22, anchor="w").pack(side=tk.LEFT)
            count = tk.Label(row, text="—", font=(T.MONO, 10), bg=T.SURFACE,
                             fg=T.INK, width=7, anchor="e")
            count.pack(side=tk.RIGHT)
            bar = T.Bar(row, height=10)
            bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=12)
            self.funnel_rows[key] = (bar, count, colour)

        # Two columns: health on the left, recent runs on the right.
        columns = tk.Frame(page, bg=T.GROUND)
        columns.pack(fill=tk.BOTH, expand=True)
        columns.columnconfigure(0, weight=1, uniform="c")
        columns.columnconfigure(1, weight=1, uniform="c")

        left = T.Card(columns, padding=18)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        tk.Label(left.inner, text="GETTING STARTED", font=(T.FONT, 7, "bold"),
                 bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").pack(fill=tk.X,
                                                                pady=(0, 10))
        for index, (title, body) in enumerate(ui_help.FIRST_RUN, 1):
            row = tk.Frame(left.inner, bg=T.SURFACE)
            row.pack(fill=tk.X, pady=(0, 9))
            tk.Label(row, text=str(index), font=(T.FONT, 8, "bold"),
                     bg=T.SURFACE_SUNK, fg=T.INK_SOFT, width=3).pack(
                         side=tk.LEFT, anchor="n", padx=(0, 10))
            column = tk.Frame(row, bg=T.SURFACE)
            column.pack(side=tk.LEFT, fill=tk.X, expand=True)
            tk.Label(column, text=title, font=(T.FONT, 9, "bold"), bg=T.SURFACE,
                     fg=T.INK, anchor="w").pack(fill=tk.X)
            tk.Label(column, text=body, font=T.F_SMALL, bg=T.SURFACE,
                     fg=T.INK_FAINT, anchor="w", justify="left",
                     wraplength=290).pack(fill=tk.X)

        right = T.Card(columns, padding=18)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        tk.Label(right.inner, text="RECENT RUNS", font=(T.FONT, 7, "bold"),
                 bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").pack(fill=tk.X,
                                                                pady=(0, 10))
        self.recent_runs = tk.Frame(right.inner, bg=T.SURFACE)
        self.recent_runs.pack(fill=tk.BOTH, expand=True)
        ttk.Button(right.inner, text="Full run history", style="Ghost.TButton",
                   command=self.show_run_history).pack(anchor="w", pady=(10, 0))

        return outer

    # --------------------------------------------------------- collect

    def _build_collect(self):
        outer, page = T.scrollable(self.stage)

        card = T.Card(page, padding=18)
        card.pack(fill=tk.X, pady=(0, 14))
        tk.Label(card.inner, text="WHERE SHOULD THE LEADS COME FROM?",
                 font=(T.FONT, 7, "bold"), bg=T.SURFACE, fg=T.INK_FAINT,
                 anchor="w").pack(fill=tk.X, pady=(0, 12))

        self.source_var = tk.StringVar(value="pt")
        sources = (
            ("pt", "Psychology Today directory", "Richest source",
             "Walks a state's therapist directory and follows each provider's "
             "own website for their email, direct phone, role and socials."),
            ("npi", "NPI Registry (bulk)", "High volume",
             "Every provider of a specialty in a state, from the federal "
             "registry. Publishes no email addresses — needs the Enrich step."),
            ("paste", "Paste NPI numbers", "Manual",
             "For numbers copied by hand from ZocDoc or anywhere else."),
        )
        for value, title, tag, description in sources:
            row = tk.Frame(card.inner, bg=T.SURFACE)
            row.pack(fill=tk.X, pady=4)
            head = tk.Frame(row, bg=T.SURFACE)
            head.pack(fill=tk.X)
            ttk.Radiobutton(head, text=title, value=value,
                            variable=self.source_var,
                            command=self._sync_source).pack(side=tk.LEFT)
            T.Pill(head, tag, "brand").pack(side=tk.LEFT, padx=8)
            tk.Label(row, text=description, font=T.F_SMALL, bg=T.SURFACE,
                     fg=T.INK_FAINT, anchor="w", justify="left",
                     wraplength=680).pack(fill=tk.X, padx=(22, 0))

        self.options_card = T.Card(page, padding=18)
        self.options_card.pack(fill=tk.BOTH, expand=True)
        area = self.options_card.inner

        self.pt_options = tk.Frame(area, bg=T.SURFACE)
        self._field(self.pt_options, "State", 0)
        self.pt_state_var = tk.StringVar(value="Texas")
        ttk.Combobox(self.pt_options, textvariable=self.pt_state_var, width=28,
                     values=list(catalog.STATES.keys()), state="readonly"
                     ).grid(row=0, column=1, sticky="w", pady=5)
        self.owners_var = tk.BooleanVar(value=self.settings.get("owners_only", False))
        ttk.Checkbutton(self.pt_options, text="Owners / CEOs / Founders only",
                        variable=self.owners_var).grid(row=1, column=1,
                                                       sticky="w", pady=(10, 0))
        tk.Label(self.pt_options,
                 text="Keeps only providers whose own website presents them as "
                      "running the practice. Fewer leads, all of them able to sign.",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT, anchor="w",
                 justify="left", wraplength=560).grid(row=2, column=1, sticky="w")
        self.resume_var = tk.BooleanVar(value=self.settings.get("resume_runs", True))
        ttk.Checkbutton(self.pt_options,
                        text="Resume a stopped run from where it left off",
                        variable=self.resume_var).grid(row=3, column=1,
                                                       sticky="w", pady=(10, 0))
        self.resume_hint = tk.Label(self.pt_options, text="", font=T.F_SMALL,
                                    bg=T.SURFACE, fg=T.BRAND, anchor="w",
                                    justify="left", wraplength=560)
        self.resume_hint.grid(row=4, column=1, sticky="w")

        self.npi_options = tk.Frame(area, bg=T.SURFACE)
        self._field(self.npi_options, "State", 0)
        self.npi_state_var = tk.StringVar(value="Texas")
        ttk.Combobox(self.npi_options, textvariable=self.npi_state_var, width=28,
                     values=["All States"] + list(catalog.STATES.keys()),
                     state="readonly").grid(row=0, column=1, sticky="w", pady=5)
        self._field(self.npi_options, "Specialty", 1)
        self.specialty_var = tk.StringVar(value="Psychiatry")
        ttk.Combobox(self.npi_options, textvariable=self.specialty_var, width=42,
                     values=["All Specialties"] + list(catalog.SPECIALTIES.keys()),
                     state="readonly").grid(row=1, column=1, sticky="w", pady=5)
        self._field(self.npi_options, "Updated since", 2)
        self.min_year_var = tk.StringVar(
            value=str(self.settings.get("min_updated_year", 2010)))
        ttk.Combobox(self.npi_options, textvariable=self.min_year_var, width=28,
                     values=["No Filter", "2005", "2010", "2015", "2018",
                             "2020", "2022", "2024"],
                     state="readonly").grid(row=2, column=1, sticky="w", pady=5)
        self.exclude_corp_var = tk.BooleanVar(
            value=self.settings.get("exclude_corporate", True))
        ttk.Checkbutton(self.npi_options,
                        text="Exclude corporate entities (Inc, Corp)",
                        variable=self.exclude_corp_var).grid(row=3, column=1,
                                                             sticky="w", pady=8)

        self.paste_options = tk.Frame(area, bg=T.SURFACE)
        tk.Label(self.paste_options,
                 text="Paste NPI numbers below — one per line, or any copied "
                      "text containing them:",
                 font=T.F_BODY, bg=T.SURFACE, fg=T.INK, anchor="w").pack(fill=tk.X)
        self.npi_paste = scrolledtext.ScrolledText(
            self.paste_options, height=10, font=T.F_MONO, relief="flat",
            highlightthickness=1, highlightbackground=T.RULE)
        self.npi_paste.pack(fill=tk.BOTH, expand=True, pady=8)
        self.paste_count = tk.Label(self.paste_options, text="", font=T.F_SMALL,
                                    bg=T.SURFACE, fg=T.INK_FAINT, anchor="w")
        self.paste_count.pack(fill=tk.X)
        self.npi_paste.bind("<KeyRelease>", lambda _e: self._count_pasted_npis())

        self._sync_source()
        return outer

    @staticmethod
    def _field(parent, text, row):
        tk.Label(parent, text=text, font=T.F_BODY, bg=T.SURFACE, fg=T.INK,
                 anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 16),
                                  pady=5)

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
        """Say, before they start, that a half-finished run is waiting."""
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

    # ---------------------------------------------------------- enrich

    def _build_enrich(self):
        outer, page = T.scrollable(self.stage)

        card = T.Card(page, padding=18)
        card.pack(fill=tk.X, pady=(0, 14))
        tk.Label(card.inner,
                 text="Run these top to bottom. Each one uses what the one "
                      "before it found.",
                 font=T.F_BODY, bg=T.SURFACE, fg=T.INK_SOFT, anchor="w",
                 justify="left", wraplength=700).pack(fill=tk.X, pady=(0, 14))

        self.step_find = T.StepRow(
            card.inner, 1, "Find missing email addresses",
            "Searches the practice's own website, then a corroborated web "
            "search. Slow — about 5–15 seconds per lead.",
            lambda: self.start_enrichment("find"))
        self.step_find.pack(fill=tk.X, pady=(0, 4))
        tk.Frame(card.inner, height=1, bg=T.RULE).pack(fill=tk.X, pady=10)

        self.step_verify = T.StepRow(
            card.inner, 2, "Verify email addresses",
            "Checks each address against its real mail server. Fast, and the "
            "one that stops you emailing addresses that bounce.",
            lambda: self.start_enrichment("verify"))
        self.step_verify.pack(fill=tk.X, pady=(0, 4))
        tk.Frame(card.inner, height=1, bg=T.RULE).pack(fill=tk.X, pady=10)

        self.step_cms = T.StepRow(
            card.inner, 3, "Add Medicare billing and practice size",
            "Annual billing, how many clinicians are in the practice, and the "
            "phone number they filed federally. Covers Medicare-enrolled "
            "providers only.",
            lambda: self.start_enrichment("cms"))
        self.step_cms.pack(fill=tk.X)

        options = tk.Frame(card.inner, bg=T.SURFACE)
        options.pack(fill=tk.X, pady=(16, 0))
        self.recheck_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options,
                        text="Re-check things that already have an answer",
                        variable=self.recheck_var).pack(anchor="w")

        legend = T.Card(page, padding=18)
        legend.pack(fill=tk.X)
        tk.Label(legend.inner, text="WHAT THE EMAIL STATUSES MEAN",
                 font=(T.FONT, 7, "bold"), bg=T.SURFACE, fg=T.INK_FAINT,
                 anchor="w").pack(fill=tk.X, pady=(0, 10))
        for status in (verification.VALID, verification.ROLE,
                       verification.ACCEPT_ALL, verification.UNKNOWN,
                       verification.RISKY, verification.DISPOSABLE,
                       verification.INVALID):
            row = tk.Frame(legend.inner, bg=T.SURFACE)
            row.pack(fill=tk.X, pady=2)
            fg, bg = T.STATUS_TONES.get(status, (T.MUTED, T.MUTED_BG))
            tk.Label(row, text=" " + status + " ", font=T.F_LABEL, bg=bg, fg=fg,
                     width=12, anchor="w", padx=5, pady=3).pack(side=tk.LEFT)
            tk.Label(row, text=verification.STATUS_HELP[status], font=T.F_SMALL,
                     bg=T.SURFACE, fg=T.INK_SOFT, anchor="w", justify="left",
                     wraplength=600).pack(side=tk.LEFT, padx=12)

        tk.Label(legend.inner,
                 text="Addresses that would bounce are removed rather than "
                      "exported with a warning label.",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT, anchor="w",
                 wraplength=700).pack(fill=tk.X, pady=(12, 0))
        return outer

    # -------------------------------------------------------- outreach

    def _build_outreach(self):
        outer, page = T.scrollable(self.stage)

        self.compliance_banner = tk.Label(page, anchor="w", justify="left",
                                          wraplength=800, font=T.F_SMALL,
                                          padx=14, pady=10)
        self.compliance_banner.pack(fill=tk.X, pady=(0, 14))

        editor = T.Card(page, padding=18)
        editor.pack(fill=tk.BOTH, expand=True, pady=(0, 14))

        row = tk.Frame(editor.inner, bg=T.SURFACE)
        row.pack(fill=tk.X)
        tk.Label(row, text="Subject", font=T.F_BODY, bg=T.SURFACE, fg=T.INK,
                 width=9, anchor="w").pack(side=tk.LEFT)
        self.subject_var = tk.StringVar(value=self.DEFAULT_SUBJECT)
        ttk.Entry(row, textvariable=self.subject_var).pack(side=tk.LEFT,
                                                           fill=tk.X, expand=True)

        self.body_text = scrolledtext.ScrolledText(
            editor.inner, height=12, wrap="word", font=T.F_BODY, relief="flat",
            highlightthickness=1, highlightbackground=T.RULE)
        self.body_text.pack(fill=tk.BOTH, expand=True, pady=(10, 6))
        self.body_text.insert("1.0", self.DEFAULT_BODY)

        tk.Label(editor.inner,
                 text="Merge fields:   " + "   ".join(
                     "{{" + name + "}}" for name in outreach.MERGE_FIELDS),
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT, anchor="w",
                 justify="left", wraplength=760).pack(fill=tk.X)
        tk.Label(editor.inner,
                 text="Your postal address and an unsubscribe line are added to "
                      "every message automatically — both are legally required.",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT,
                 anchor="w").pack(fill=tk.X, pady=(4, 0))

        controls = T.Card(page, padding=16)
        controls.pack(fill=tk.X)
        bar = tk.Frame(controls.inner, bg=T.SURFACE)
        bar.pack(fill=tk.X)

        tk.Label(bar, text="Send to", font=T.F_BODY, bg=T.SURFACE,
                 fg=T.INK).pack(side=tk.LEFT)
        self.audience_var = tk.StringVar(
            value="Owner-operated small practices (ICP)")
        ttk.Combobox(bar, textvariable=self.audience_var, width=34,
                     state="readonly",
                     values=["Owner-operated small practices (ICP)",
                             "Owners and founders",
                             "Everyone with a usable address"]
                     ).pack(side=tk.LEFT, padx=8)
        tk.Label(bar, text="Max", font=T.F_BODY, bg=T.SURFACE,
                 fg=T.INK).pack(side=tk.LEFT, padx=(14, 6))
        self.send_limit_var = tk.StringVar(value="25")
        ttk.Combobox(bar, textvariable=self.send_limit_var, width=6,
                     values=["5", "10", "25", "50", "100"],
                     state="readonly").pack(side=tk.LEFT)
        self.allow_unverified_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Include unverified addresses",
                        variable=self.allow_unverified_var).pack(side=tk.LEFT,
                                                                 padx=(16, 0))

        actions = tk.Frame(controls.inner, bg=T.SURFACE)
        actions.pack(fill=tk.X, pady=(14, 0))
        ttk.Button(actions, text="Preview (no send)", style="Step.TButton",
                   command=lambda: self.start_send(dry_run=True)).pack(side=tk.LEFT)
        ttk.Button(actions, text="Send test to myself", style="Ghost.TButton",
                   command=self.send_test).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Test connection", style="Ghost.TButton",
                   command=self.test_smtp).pack(side=tk.LEFT)
        ttk.Button(actions, text="Do-not-contact list", style="Ghost.TButton",
                   command=self.show_suppressions).pack(side=tk.LEFT, padx=6)
        return outer

    def _refresh_compliance(self):
        """Green when a send is legal, amber with the exact gaps when not."""
        if not hasattr(self, "compliance_banner"):
            return
        problems = outreach.compliance_problems(self.settings)
        if problems:
            background, foreground = T.TONES["warn"]
            text = "Before you can send:   " + "    ".join(
                "• " + problem for problem in problems)
        else:
            background, foreground = T.TONES["ok"]
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

    # --------------------------------------------------------- results

    def _build_results(self):
        outer, page = T.scrollable(self.stage)

        metrics = T.Card(page, padding=18)
        metrics.pack(fill=tk.X, pady=(0, 14))
        strip = tk.Frame(metrics.inner, bg=T.SURFACE)
        strip.pack(fill=tk.X)
        self.metrics = {}
        for index, (key, label, tone) in enumerate((
                ("total", "Total leads", T.BRAND),
                ("with_email", "With email", T.BRAND),
                ("verified_email", "Verified", T.OK),
                ("with_phone", "With phone", T.BRAND),
                ("with_billing", "Billing data", T.INFO),
                ("avg_score", "Avg. score", T.BRAND))):
            cell = T.Metric(strip, label, tone=tone)
            cell.grid(row=0, column=index, sticky="w", padx=(0, 30))
            self.metrics[key] = cell

        columns = tk.Frame(page, bg=T.GROUND)
        columns.pack(fill=tk.BOTH, expand=True, pady=(0, 14))
        columns.columnconfigure(0, weight=3, uniform="r")
        columns.columnconfigure(1, weight=2, uniform="r")

        left = T.Card(columns, padding=18)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        tk.Label(left.inner, text="LEADS BY TAB", font=(T.FONT, 7, "bold"),
                 bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").pack(fill=tk.X,
                                                                pady=(0, 10))
        self.tree = ttk.Treeview(left.inner, columns=("leads", "emails", "rate"),
                                 show="tree headings", height=7)
        self.tree.heading("#0", text="Tab")
        self.tree.heading("leads", text="Leads")
        self.tree.heading("emails", text="Emails")
        self.tree.heading("rate", text="Hit rate")
        self.tree.column("#0", width=230)
        for column in ("leads", "emails", "rate"):
            self.tree.column(column, width=72, anchor=tk.CENTER)
        self.tree.pack(fill=tk.BOTH, expand=True)

        right = T.Card(columns, padding=18)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        tk.Label(right.inner, text="EMAIL QUALITY", font=(T.FONT, 7, "bold"),
                 bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").pack(fill=tk.X,
                                                                pady=(0, 10))
        self.status_chart = T.BarChart(right.inner,
                                       empty_text="Nothing verified yet")
        self.status_chart.pack(fill=tk.X)

        charts = tk.Frame(page, bg=T.GROUND)
        charts.pack(fill=tk.X, pady=(0, 14))
        charts.columnconfigure(0, weight=1, uniform="c2")
        charts.columnconfigure(1, weight=1, uniform="c2")

        states_card = T.Card(charts, padding=18)
        states_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        tk.Label(states_card.inner, text="LEADS BY STATE",
                 font=(T.FONT, 7, "bold"), bg=T.SURFACE, fg=T.INK_FAINT,
                 anchor="w").pack(fill=tk.X, pady=(0, 10))
        self.state_chart = T.BarChart(states_card.inner)
        self.state_chart.pack(fill=tk.X)

        trend_card = T.Card(charts, padding=18)
        trend_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        tk.Label(trend_card.inner, text="EMAIL HIT RATE, RECENT RUNS",
                 font=(T.FONT, 7, "bold"), bg=T.SURFACE, fg=T.INK_FAINT,
                 anchor="w").pack(fill=tk.X, pady=(0, 10))
        self.trend = T.Sparkline(trend_card.inner)
        self.trend.pack(fill=tk.X)
        tk.Label(trend_card.inner, text="SCORE DISTRIBUTION",
                 font=(T.FONT, 7, "bold"), bg=T.SURFACE, fg=T.INK_FAINT,
                 anchor="w").pack(fill=tk.X, pady=(14, 8))
        self.score_chart = T.BarChart(trend_card.inner)
        self.score_chart.pack(fill=tk.X)

        export = T.Card(page, padding=16)
        export.pack(fill=tk.X)
        bar = tk.Frame(export.inner, bg=T.SURFACE)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Open spreadsheet", style="Step.TButton",
                   command=self.open_result).pack(side=tk.LEFT)
        ttk.Button(bar, text="Analytics", style="Ghost.TButton",
                   command=self.show_dashboard).pack(side=tk.LEFT, padx=6)

        tk.Label(bar, text="Call list", font=T.F_SMALL, bg=T.SURFACE,
                 fg=T.INK_FAINT).pack(side=tk.LEFT, padx=(20, 6))
        self.call_list_var = tk.StringVar(value="100")
        ttk.Combobox(bar, textvariable=self.call_list_var, width=6,
                     values=["0", "50", "100", "250", "500"],
                     state="readonly").pack(side=tk.LEFT)
        tk.Label(bar, text="Min score", font=T.F_SMALL, bg=T.SURFACE,
                 fg=T.INK_FAINT).pack(side=tk.LEFT, padx=(14, 6))
        self.min_score_var = tk.StringVar(value="0")
        ttk.Combobox(bar, textvariable=self.min_score_var, width=5,
                     values=["0", "20", "40", "60", "80"],
                     state="readonly").pack(side=tk.LEFT)
        self.icp_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Owner-operated small practices only",
                        variable=self.icp_only_var).pack(side=tk.LEFT, padx=(14, 0))
        return outer

    # -------------------------------------------------------- settings

    def _build_settings(self):
        outer, page = T.scrollable(self.stage)

        # --- output
        card = T.Card(page, padding=18)
        card.pack(fill=tk.X, pady=(0, 12))
        self._card_title(card.inner, "WHERE RESULTS ARE EXPORTED")
        grid = tk.Frame(card.inner, bg=T.SURFACE)
        grid.pack(fill=tk.X)

        self.output_var = tk.StringVar(value=self.settings.get("output", "excel"))
        ttk.Radiobutton(grid, text="Local Excel file", value="excel",
                        variable=self.output_var,
                        command=self._sync_output).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(grid, text="Google Sheets", value="google",
                        variable=self.output_var,
                        command=self._sync_output).grid(row=0, column=1,
                                                        sticky="w", padx=20)
        self._field(grid, "Excel file", 1)
        self.excel_path_var = tk.StringVar(value=self.settings.get("excel_path"))
        ttk.Entry(grid, textvariable=self.excel_path_var, width=52).grid(
            row=1, column=1, columnspan=2, sticky="w", pady=4)
        self._field(grid, "Service account JSON", 2)
        self.gs_creds_var = tk.StringVar(
            value=self.settings.get("google_credentials"))
        self.gs_creds_entry = ttk.Entry(grid, textvariable=self.gs_creds_var,
                                        width=44)
        self.gs_creds_entry.grid(row=2, column=1, sticky="w", pady=4)
        self.gs_browse = ttk.Button(grid, text="Browse", style="Ghost.TButton",
                                    command=self._browse_json)
        self.gs_browse.grid(row=2, column=2, padx=6)
        self._field(grid, "Spreadsheet name", 3)
        self.gs_name_var = tk.StringVar(
            value=self.settings.get("google_spreadsheet"))
        self.gs_name_entry = ttk.Entry(grid, textvariable=self.gs_name_var,
                                       width=44)
        self.gs_name_entry.grid(row=3, column=1, sticky="w", pady=4)
        self.auto_export_var = tk.BooleanVar(
            value=self.settings.get("auto_export", True))
        ttk.Checkbutton(grid, text="Export automatically when a run finishes",
                        variable=self.auto_export_var).grid(row=4, column=1,
                                                            sticky="w", pady=(8, 0))

        # --- accuracy
        card = T.Card(page, padding=18)
        card.pack(fill=tk.X, pady=(0, 12))
        self._card_title(card.inner, "EMAIL ACCURACY")
        self.verify_var = tk.BooleanVar(value=self.settings.get("verify_emails", True))
        ttk.Checkbutton(card.inner, text="Check addresses against their mail server",
                        variable=self.verify_var).pack(anchor="w")
        self.smtp_var = tk.BooleanVar(value=self.settings.get("smtp_probe", True))
        ttk.Checkbutton(card.inner,
                        text="Confirm individual mailboxes (needs outbound port 25)",
                        variable=self.smtp_var).pack(anchor="w", pady=2)
        self.guess_var = tk.BooleanVar(value=self.settings.get("guess_emails", True))
        ttk.Checkbutton(card.inner,
                        text="Infer addresses at a known practice domain, keeping "
                             "only the ones the mail server confirms",
                        variable=self.guess_var).pack(anchor="w")
        row = tk.Frame(card.inner, bg=T.SURFACE)
        row.pack(fill=tk.X, pady=(10, 0))
        tk.Label(row, text="Send probes as", font=T.F_BODY, bg=T.SURFACE,
                 fg=T.INK).pack(side=tk.LEFT, padx=(0, 12))
        self.sender_var = tk.StringVar(value=self.settings.get("probe_sender", ""))
        ttk.Entry(row, textvariable=self.sender_var, width=38).pack(side=tk.LEFT)
        tk.Label(card.inner,
                 text="Optional. Your real outreach address makes more mail "
                      "servers answer honestly. Blank uses the null sender.",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT, anchor="w",
                 wraplength=700).pack(fill=tk.X, pady=(3, 0))

        # --- network
        card = T.Card(page, padding=18)
        card.pack(fill=tk.X, pady=(0, 12))
        self._card_title(card.inner, "NETWORK")
        row = tk.Frame(card.inner, bg=T.SURFACE)
        row.pack(fill=tk.X)
        tk.Label(row, text="Proxy", font=T.F_BODY, bg=T.SURFACE, fg=T.INK,
                 width=16, anchor="w").pack(side=tk.LEFT)
        self.proxy_var = tk.StringVar(value=self.settings.get("proxy", ""))
        ttk.Entry(row, textvariable=self.proxy_var, width=38).pack(side=tk.LEFT)
        tk.Label(card.inner,
                 text="Psychology Today and CMS serve US traffic only. Leave "
                      "blank to use the free reader fallback, or point this at a "
                      "US VPN/proxy (e.g. http://127.0.0.1:8080) to go direct "
                      "and much faster.",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT, anchor="w",
                 justify="left", wraplength=700).pack(fill=tk.X, pady=(4, 10))
        row = tk.Frame(card.inner, bg=T.SURFACE)
        row.pack(fill=tk.X)
        tk.Label(row, text="Concurrent lookups", font=T.F_BODY, bg=T.SURFACE,
                 fg=T.INK, width=16, anchor="w").pack(side=tk.LEFT)
        self.workers_var = tk.IntVar(value=int(self.settings.get("pt_workers", 5)))
        ttk.Scale(row, from_=1, to=16, variable=self.workers_var,
                  orient=tk.HORIZONTAL, length=200,
                  command=lambda _v: self.workers_label.configure(
                      text=str(self.workers_var.get()))).pack(side=tk.LEFT)
        self.workers_label = tk.Label(row, text=str(self.workers_var.get()),
                                      font=T.F_MONO, bg=T.SURFACE, fg=T.INK_SOFT,
                                      width=3)
        self.workers_label.pack(side=tk.LEFT, padx=8)

        # --- sending
        card = T.Card(page, padding=18)
        card.pack(fill=tk.X, pady=(0, 12))
        self._card_title(card.inner, "SENDING EMAIL")
        grid = tk.Frame(card.inner, bg=T.SURFACE)
        grid.pack(fill=tk.X)
        self._field(grid, "Mail provider", 0)
        self.smtp_preset_var = tk.StringVar(value="Gmail")
        preset = ttk.Combobox(grid, textvariable=self.smtp_preset_var, width=26,
                              state="readonly",
                              values=list(outreach.SMTP_PRESETS.keys()))
        preset.grid(row=0, column=1, sticky="w", pady=4)
        preset.bind("<<ComboboxSelected>>", lambda _e: self._apply_smtp_preset())
        self.smtp_note = tk.Label(grid, text="", font=T.F_SMALL, bg=T.SURFACE,
                                  fg=T.WARN, anchor="w", justify="left",
                                  wraplength=560)
        self.smtp_note.grid(row=1, column=1, columnspan=2, sticky="w")

        self._field(grid, "SMTP server", 2)
        self.smtp_host_var = tk.StringVar(value=self.settings.get("smtp_host", ""))
        ttk.Entry(grid, textvariable=self.smtp_host_var, width=30).grid(
            row=2, column=1, sticky="w", pady=4)
        self.smtp_port_var = tk.StringVar(
            value=str(self.settings.get("smtp_port", 587)))
        ttk.Entry(grid, textvariable=self.smtp_port_var, width=7).grid(
            row=2, column=2, sticky="w", padx=6)

        self._field(grid, "Username", 3)
        self.smtp_user_var = tk.StringVar(value=self.settings.get("smtp_user", ""))
        ttk.Entry(grid, textvariable=self.smtp_user_var, width=38).grid(
            row=3, column=1, columnspan=2, sticky="w", pady=4)

        self._field(grid, "Password", 4)
        self.smtp_pass_var = tk.StringVar(
            value=outreach.load_password(self.settings.get("smtp_user", "")))
        ttk.Entry(grid, textvariable=self.smtp_pass_var, width=38,
                  show="•").grid(row=4, column=1, columnspan=2, sticky="w", pady=4)
        tk.Label(grid, text="Stored in the Windows Credential Manager, never in "
                            "a file in this folder.",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT,
                 anchor="w").grid(row=5, column=1, columnspan=2, sticky="w")

        self._field(grid, "From name", 6)
        self.from_name_var = tk.StringVar(value=self.settings.get("from_name", ""))
        ttk.Entry(grid, textvariable=self.from_name_var, width=38).grid(
            row=6, column=1, columnspan=2, sticky="w", pady=4)
        self._field(grid, "From address", 7)
        self.from_email_var = tk.StringVar(value=self.settings.get("from_email", ""))
        ttk.Entry(grid, textvariable=self.from_email_var, width=38).grid(
            row=7, column=1, columnspan=2, sticky="w", pady=4)

        # --- compliance
        card = T.Card(page, padding=18)
        card.pack(fill=tk.X, pady=(0, 12))
        self._card_title(card.inner, "REQUIRED BY LAW BEFORE YOU CAN SEND")
        grid = tk.Frame(card.inner, bg=T.SURFACE)
        grid.pack(fill=tk.X)
        self._field(grid, "Postal address", 0)
        self.postal_var = tk.StringVar(value=self.settings.get("postal_address", ""))
        ttk.Entry(grid, textvariable=self.postal_var, width=52).grid(
            row=0, column=1, sticky="w", pady=4)
        self._field(grid, "Unsubscribe address", 1)
        self.unsub_var = tk.StringVar(
            value=self.settings.get("unsubscribe_email", ""))
        ttk.Entry(grid, textvariable=self.unsub_var, width=52).grid(
            row=1, column=1, sticky="w", pady=4)
        tk.Label(card.inner,
                 text="US CAN-SPAM requires commercial email to carry a real "
                      "postal address and a working opt-out. Both are added to "
                      "every message automatically, and sending stays blocked "
                      "until they are filled in.",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT, anchor="w",
                 justify="left", wraplength=700).pack(fill=tk.X, pady=(6, 10))
        row = tk.Frame(card.inner, bg=T.SURFACE)
        row.pack(fill=tk.X)
        tk.Label(row, text="Daily send cap", font=T.F_BODY, bg=T.SURFACE,
                 fg=T.INK, width=16, anchor="w").pack(side=tk.LEFT)
        self.daily_cap_var = tk.StringVar(
            value=str(self.settings.get("daily_send_cap", 40)))
        ttk.Combobox(row, textvariable=self.daily_cap_var, width=6,
                     state="readonly",
                     values=["10", "20", "40", "75", "150"]).pack(side=tk.LEFT)
        tk.Label(row, text="  Start low. A domain with no sending history that "
                           "emits hundreds of cold emails is blacklisted in a day.",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT).pack(side=tk.LEFT)

        self._sync_output()
        self._apply_smtp_preset(initial=True)
        return outer

    @staticmethod
    def _card_title(parent, text):
        tk.Label(parent, text=text, font=(T.FONT, 7, "bold"), bg=T.SURFACE,
                 fg=T.INK_FAINT, anchor="w").pack(fill=tk.X, pady=(0, 12))

    def _sync_output(self):
        state = tk.NORMAL if self.output_var.get() == "google" else tk.DISABLED
        for widget in (self.gs_creds_entry, self.gs_browse, self.gs_name_entry):
            widget.configure(state=state)

    def _browse_json(self):
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json")])
        if path:
            self.gs_creds_var.set(path)

    def _apply_smtp_preset(self, initial=False):
        """Fill in host/port for a known provider, and show its gotcha.

        Gmail and Outlook both reject the account password outright when 2FA is
        on, which is the most common reason a first send fails.
        """
        preset = outreach.SMTP_PRESETS.get(self.smtp_preset_var.get(), {})
        if not initial and preset.get("host"):
            self.smtp_host_var.set(preset["host"])
            self.smtp_port_var.set(str(preset["port"]))
        self.smtp_note.configure(text=preset.get("note", ""))

    def _save_smtp_password(self):
        username = self.smtp_user_var.get().strip()
        password = self.smtp_pass_var.get()
        if not (username and password):
            return
        try:
            outreach.store_password(username, password)
        except Exception as e:
            self.logger.warning("Could not save the SMTP password: %s", e)

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
        year = self.min_year_var.get()
        self.settings["min_updated_year"] = None if year == "No Filter" else int(year)
        config.save(self.settings)
        self._save_smtp_password()
        self._refresh_compliance()
        # The next-step recommendation depends on settings as well as data, so
        # the next screen change must redraw even though the database is the same.
        self._shown_version = None
        self.banner.set("ok", "Settings saved.",
                        "Stored in config.json and used by the command line too.")

    def save_settings_quietly(self):
        state = (self.banner.title.cget("text"), self.banner.detail.cget("text"))
        self.save_settings()
        self.banner.set("busy", state[0], state[1])

    # ===================================================================
    # Startup and refresh
    # ===================================================================

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
        if not self.store.get_stats()["total"] and excel_path \
                and os.path.exists(excel_path):
            self.banner.set("busy", "Importing your existing spreadsheet…",
                            "This happens once, so nothing collected before now "
                            "is left behind.")
            self.root.update_idletasks()
            try:
                imported = self.store.import_from_excel(excel_path,
                                                        logger=self.logger)
                if imported:
                    self.logger.info(
                        "Imported %d leads from %s. The spreadsheet is now an "
                        "export of the database rather than the database itself.",
                        imported, os.path.basename(excel_path))
            except Exception as e:
                self.logger.warning("Could not import %s: %s", excel_path, e)

        self.refresh()
        self._refresh_resume_hint()

        stats = self.store.get_stats()
        if stats["total"]:
            self.banner.set(
                "idle", "Ready — {0} leads in the database.".format(stats["total"]),
                "{0} have an email address, {1} confirmed by a mail server.".format(
                    stats["with_email"], stats["verified_email"]))
        else:
            self.banner.set("idle", "Ready.",
                            "Nothing collected yet. The Next step box below says "
                            "where to begin.")

    def refresh(self, force=False):
        """Re-read everything from the database and redraw the screens.

        Skipped when nothing has changed since the last redraw. Every click in
        the rail calls this, and rebuilding the charts, the tab table and the
        recent-runs list for a database that had not changed was pure waiting.
        force=True redraws regardless — used when a run ends.
        """
        if not self.store:
            return
        # Read the fingerprint before the data: a worker committing in between
        # then shows up as a new version next time, instead of being missed.
        version = self.store.data_version()
        if not force and version == self._shown_version:
            return
        self._shown_version = version

        stats = self.store.get_stats()
        outreach_stats = self.store.outreach_stats()

        # --- metrics
        for key in ("total", "with_email", "verified_email", "with_phone",
                    "with_billing"):
            self.metrics[key].set(stats.get(key, 0))
        self.metrics["avg_score"].set(stats.get("avg_score", 0))

        # --- funnel
        total = stats["total"] or 1
        values = {
            "total": stats["total"],
            "with_phone": stats["with_phone"],
            "with_email": stats["with_email"],
            "verified_email": stats["verified_email"],
            "contacted": outreach_stats.get("sent", 0),
        }
        for key, (bar, count, colour) in self.funnel_rows.items():
            value = values.get(key, 0)
            bar.set_segments([(value / total, colour)])
            count.configure(text="{0:,}".format(value))

        # --- tab table
        self.tree.delete(*self.tree.get_children())
        self.tree_items.clear()
        self.sheet_totals.clear()
        for sheet_key, entry in sorted(stats["by_source"].items()):
            bucket = {"leads": entry["total"], "emails": entry["with_email"]}
            self.sheet_totals[sheet_key] = bucket
            self._refresh_row(sheet_key, bucket)

        # --- charts
        self.status_chart.set_rows([
            (status, count, T.STATUS_TONES.get(status, (T.MUTED, ""))[0])
            for status, count in sorted(stats["by_email_status"].items(),
                                        key=lambda kv: -kv[1])])
        self.state_chart.set_rows([
            (state, entry["total"], None)
            for state, entry in sorted(stats["by_state"].items(),
                                       key=lambda kv: -kv[1]["total"])[:8]])

        runs = [r for r in reversed(self.store.recent_runs(15))
                if r.get("hit_rate") is not None]
        self.trend.set_values([r["hit_rate"] for r in runs])

        bands = stats.get("by_score_band", {})
        self.score_chart.set_rows([
            ("70+", bands.get("70+", 0), T.OK),
            ("50-69", bands.get("50-69", 0), T.INFO),
            ("30-49", bands.get("30-49", 0), T.WARN),
            ("under 30", bands.get("under 30", 0), T.MUTED)])

        # --- enrich step states
        # One query for all three backlogs, handed to the next-step box below
        # rather than counted a second time there.
        pending = self.store.pending_counts()
        missing = pending["missing_email"]
        unverified = pending["unverified"]
        needs_cms = pending["needs_cms"]
        self.step_find.set_state(
            "All leads have an address" if not missing
            else "{0} waiting".format(missing),
            "ok" if not missing else "brand", done=not missing and stats["total"] > 0)
        self.step_verify.set_state(
            "Everything checked" if not unverified
            else "{0} unchecked".format(unverified),
            "ok" if not unverified else "warn",
            done=not unverified and stats["with_email"] > 0)
        self.step_cms.set_state(
            "Nothing left to look up" if not needs_cms
            else "{0} to look up".format(needs_cms),
            "ok" if not needs_cms else "brand",
            done=not needs_cms and stats["total"] > 0)

        # --- recent runs
        for child in self.recent_runs.winfo_children():
            child.destroy()
        recent = self.store.recent_runs(5)
        if not recent:
            tk.Label(self.recent_runs, text="No runs yet.", font=T.F_SMALL,
                     bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").pack(fill=tk.X)
        for run in recent:
            row = tk.Frame(self.recent_runs, bg=T.SURFACE)
            row.pack(fill=tk.X, pady=2)
            status = (run["status"] or "").lower()
            tone = {"ok": "ok", "empty": "warn", "failed": "bad",
                    "stopped": "muted"}.get(status, "muted")
            T.Pill(row, status.upper() or "—", tone).pack(side=tk.LEFT)
            tk.Label(row, text=run["action"], font=T.F_SMALL, bg=T.SURFACE,
                     fg=T.INK, anchor="w").pack(side=tk.LEFT, padx=8)
            tk.Label(row, text=(run["started_at"] or "")[5:16].replace("T", " "),
                     font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT,
                     anchor="e").pack(side=tk.RIGHT)

        self._refresh_compliance()
        self._refresh_next_action(stats, outreach_stats, pending)

    def _refresh_next_action(self, stats, outreach_stats, pending=None):
        """Pick the one thing most worth doing, from the real state of the data.

        This is the answer to "I don't know what to do" — it names a stage that
        is actually unfinished rather than offering six equal choices.
        """
        pending = pending or self.store.pending_counts()
        missing = pending["missing_email"]
        unverified = pending["unverified"]
        needs_cms = pending["needs_cms"]
        problems = outreach.compliance_problems(self.settings)

        if stats["total"] == 0:
            action = ("collect", "Collect your first leads",
                      "Nothing in the database yet. Psychology Today is the "
                      "richest source — pick a state and press the button.")
        elif missing:
            action = ("enrich:find", "Find {0} missing email addresses".format(missing),
                      "These leads have a phone number but nothing to email. "
                      "This pass searches each practice's own website first.")
        elif unverified:
            action = ("enrich:verify", "Verify {0} email addresses".format(unverified),
                      "Checking these against their mail servers is what stops "
                      "you sending to addresses that bounce.")
        elif needs_cms:
            action = ("enrich:cms", "Look up {0} providers in Medicare data".format(needs_cms),
                      "Adds annual billing and practice size, which is what "
                      "sizes the deal before you call.")
        elif problems:
            action = ("settings", "Finish setting up sending",
                      "Outreach is blocked until these are filled in: "
                      + "; ".join(problems))
        else:
            sendable, _excluded = self._recipients_preview()
            if sendable:
                action = ("outreach", "Email {0} qualified leads".format(sendable),
                          "Everything is collected, verified and ready. Start "
                          "with a small batch and a preview.")
            else:
                action = ("collect", "Collect another state",
                          "Everyone currently in the database has been "
                          "contacted or excluded. Time for more leads.")

        key, title, note = action
        self._next_action = key
        self.next_title.configure(text=title)
        self.next_note.configure(text=note)
        self.nav_next.configure(text=title)

    def _query_pipeline(self):
        """A pipeline with no engines, for questions the database alone can
        answer — who a campaign would reach — without building verifiers and
        HTTP clients that nothing is going to use."""
        return LeadPipeline(store=self.store, npi_engine=None, email_engine=None,
                            pt_engine=None, reporter=Reporter(self.logger),
                            settings=self.settings)

    def _recipients_preview(self):
        """How many people a default campaign would reach. Never raises — it runs
        on refresh and must not be able to break the Home screen."""
        try:
            recipients, excluded = self._query_pipeline().recipients_for(
                limit=0, icp_only=True)
            return len(recipients), excluded
        except Exception as e:
            self.logger.debug("Could not preview recipients: %s", e)
            return 0, {}

    def _do_next_action(self):
        key = self._next_action or "collect"
        if key.startswith("enrich:"):
            self.show("enrich")
            self.start_enrichment(key.split(":", 1)[1])
        else:
            self.show(key)

    def _refresh_row(self, sheet_key, bucket):
        rate = (bucket["emails"] / bucket["leads"] * 100) if bucket["leads"] else 0.0
        values = (bucket["leads"], bucket["emails"], "{0:.1f}%".format(rate))
        if sheet_key in self.tree_items:
            self.tree.item(self.tree_items[sheet_key], values=values)
        else:
            self.tree_items[sheet_key] = self.tree.insert(
                "", tk.END, text=sheet_key, values=values)

    # ===================================================================
    # Running work
    # ===================================================================

    def _sync_primary(self):
        """The bottom button always says what it will do on this screen."""
        if not hasattr(self, "primary_btn"):
            return
        if self.worker and self.worker.is_alive():
            return
        labels = {
            "home": "Go to Next Step",
            "collect": {"pt": "Collect from Psychology Today",
                        "npi": "Collect from NPI Registry",
                        "paste": "Look Up Pasted NPIs"}.get(
                            getattr(self, "source_var", None)
                            and self.source_var.get(), "Start"),
            "enrich": "Run All Three Passes",
            "outreach": "Send Campaign",
            "results": "Export Spreadsheet",
            "settings": "Save Settings",
        }
        self.primary_btn.configure(text=labels.get(self.screen, "Start"))

    def run_primary(self):
        if self.screen == "home":
            self._do_next_action()
        elif self.screen == "collect":
            self.start_collection()
        elif self.screen == "enrich":
            self.start_enrichment("all")
        elif self.screen == "outreach":
            self.start_send(dry_run=False)
        elif self.screen == "results":
            self.start_export()
        else:
            self.save_settings()

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
        self.refresh(force=True)
        self._refresh_resume_hint()

    def stop_run(self):
        self.stop_event.set()
        self.stop_btn.configure(state=tk.DISABLED)
        self.logger.info("Stop requested — finishing the items already in flight…")

    def _spawn(self, make_job, phase):
        """Start a run.

        make_job is called here, on the Tk thread, once this run's stop event
        exists, and returns the function the worker executes. Everything that
        reads the window — settings, text boxes, dropdowns — belongs in
        make_job and never in the job itself. Tk is not thread-safe, and a
        widget read from a worker shows up as an occasional frozen window
        rather than as an error anyone can trace.
        """
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Already running",
                                "Wait for the current run to finish, or press Stop.")
            return
        self._begin(phase)
        try:
            job = make_job()
        except Exception as e:
            self.logger.error("Could not start: %s\n%s", e, traceback.format_exc())
            self._end()
            self.banner.set("error", "Could not start.", str(e))
            return
        self.worker = threading.Thread(target=self._guarded, args=(job,),
                                       daemon=True)
        self.worker.start()

    def _guarded(self, target):
        try:
            target()
        except Exception as e:
            self.logger.error("Unexpected error: %s\n%s", e, traceback.format_exc())
            self.post(lambda: self.banner.set(
                "error", "The run stopped with an unexpected error.", str(e)))
        finally:
            self.post(self._end)

    def _build_pipeline(self):
        """Fresh engines for each run, so a proxy or worker change takes effect
        without restarting the app.

        Only ever called from a make_job, on the Tk thread. It saves the
        settings form, which reads widgets, and it loads the exporters, which
        must never be imported for the first time on a worker.
        """
        _load_exporters()
        self.save_settings_quietly()
        verifier = None
        if self.settings.get("verify_emails"):
            verifier = EmailVerifier(logger=self.logger,
                                     probe_smtp=self.settings.get("smtp_probe", True),
                                     probe_sender=self.settings.get("probe_sender", ""))
        pipeline = LeadPipeline(
            store=self.store,
            npi_engine=NPIEngine(logger=self.logger),
            email_engine=EmailEngine(logger=self.logger),
            pt_engine=PsychologyTodayEngine(
                logger=self.logger, proxies=config.proxies_for(self.settings)),
            verifier=verifier,
            cms_engine=CMSEngine(logger=self.logger,
                                 proxies=config.proxies_for(self.settings)),
            reporter=AppReporter(self, self.logger, self.stop_event),
            settings=self.settings)
        pipeline.PT_WORKERS = int(self.settings.get("pt_workers", 5))
        pipeline.EMAIL_WORKERS = int(self.settings.get("email_workers", 5))
        return pipeline

    # ------------------------------------------------------ run: collect

    def start_collection(self):
        source = self.source_var.get()
        if source == "pt":
            state_name = self.pt_state_var.get()
            state_abbr = catalog.STATES[state_name]
            owners_only = self.owners_var.get()
            resume = self.resume_var.get()

            def make_job():
                pipeline = self._build_pipeline()
                export = self._export_options()

                def job():
                    result = pipeline.collect_psychology_today(
                        state_name, state_abbr, owners_only=owners_only,
                        resume=resume)
                    self._after_run(pipeline, result,
                                    "Psychology Today — " + state_name,
                                    export_options=export)
                return job

            self._spawn(make_job, "Collecting from Psychology Today…")

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

            def make_job():
                pipeline = self._build_pipeline()
                export = self._export_options()

                def job():
                    result = pipeline.collect_npi(states, specialties,
                                                  exclude_corp=exclude_corp,
                                                  min_year=min_year)
                    self._after_run(pipeline, result, "NPI Registry",
                                    export_options=export)
                return job

            self._spawn(make_job, "Collecting from the NPI Registry…")

        else:
            numbers = LeadPipeline.extract_npi_numbers(
                self.npi_paste.get("1.0", tk.END))
            if not numbers:
                messagebox.showerror("Nothing to look up",
                                     "No NPI numbers were found in the pasted text.")
                return

            def make_job():
                pipeline = self._build_pipeline()
                export = self._export_options()

                def job():
                    result = pipeline.import_npis(numbers)
                    self._after_run(pipeline, result, "Pasted NPI numbers",
                                    export_options=export)
                return job

            self._spawn(make_job, "Looking up {0} NPI numbers…".format(len(numbers)))

    def start_enrichment(self, mode):
        """mode: find | verify | cms | all."""
        recheck = self.recheck_var.get()

        def make_job():
            pipeline = self._build_pipeline()
            if mode in ("verify", "all") and pipeline.verifier is None:
                # Asking for verification explicitly overrides the setting that
                # turns it off during collection. Setting the flag after the
                # pipeline was built, as this used to, changed nothing — the
                # pass then reported that verification was not enabled.
                pipeline.verifier = EmailVerifier(
                    logger=self.logger,
                    probe_smtp=self.settings.get("smtp_probe", True),
                    probe_sender=self.settings.get("probe_sender", ""))
            export = self._export_options()

            def job():
                # A single pass exports when it finishes. All three together
                # export once at the end: writing the same workbook three times
                # in a row was most of the wait after "Run All Three Passes".
                per_pass_export = None if mode == "all" else export
                quiet = (mode == "all")
                added = 0
                if mode in ("find", "all"):
                    result = pipeline.find_missing_emails()
                    added += result.added
                    self._after_run(pipeline, result, "Email discovery",
                                    quiet=quiet, export_options=per_pass_export)
                if mode in ("verify", "all"):
                    result = pipeline.verify_emails(recheck=recheck)
                    added += result.added
                    self._after_run(pipeline, result, "Email verification",
                                    quiet=quiet, export_options=per_pass_export)
                if mode in ("cms", "all"):
                    result = pipeline.enrich_from_cms(recheck=recheck)
                    added += result.added
                    self._after_run(pipeline, result, "Medicare data",
                                    quiet=quiet, export_options=per_pass_export)
                if mode == "all":
                    if added and self.settings.get("auto_export"):
                        try:
                            self._export(export)
                        except Exception as e:
                            self.logger.error("Export failed: %s", e)
                    self.post(lambda: self.banner.set(
                        "ok", "All three enrichment passes finished.",
                        "Check the Results screen for what changed."))
            return job

        phases = {"find": "Finding missing email addresses…",
                  "verify": "Verifying email addresses…",
                  "cms": "Looking up Medicare data…",
                  "all": "Running all three enrichment passes…"}
        self._spawn(make_job, phases.get(mode, "Working…"))

    def start_export(self):
        min_score = int(self.min_score_var.get() or 0) or None

        def make_job():
            export = self._export_options(min_score)
            target = self._export_target()

            def job():
                written = self._export(export)
                self.post(lambda: self.banner.set(
                    "ok" if written else "warn",
                    "Exported {0} leads.".format(written) if written
                    else "Nothing matched the export filter.",
                    target))
            return job

        self._spawn(make_job, "Exporting…")

    # -------------------------------------------------------- run: send

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
                self.show("settings")
                return
            # A database question — no engines needed just to count recipients.
            recipients, _excluded = self._query_pipeline().recipients_for(
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

        def make_job():
            pipeline = self._build_pipeline()
            export = self._export_options()
            open_log = dry_run and not self.log_open

            def job():
                result = pipeline.send_campaign(
                    subject, body, campaign="app", limit=limit,
                    daily_cap=int(self.settings.get("daily_send_cap", 40)),
                    allow_unverified=allow_unverified, dry_run=dry_run,
                    min_seconds=int(self.settings.get("send_min_seconds", 45)),
                    max_seconds=int(self.settings.get("send_max_seconds", 90)),
                    **filters)
                self._after_run(pipeline, result,
                                "Preview" if dry_run else "Outreach",
                                export_options=export)
                if open_log:
                    self.post(self.toggle_log)
            return job

        self._spawn(make_job, "Previewing…" if dry_run else "Sending…")

    def send_test(self):
        """One message to yourself, so you see exactly what lands."""
        address = self.settings.get("from_email")
        if not address:
            messagebox.showerror("No sender",
                                 "Set a 'from' address in Settings first.")
            self.show("settings")
            return
        sample = next(iter(self.store.best_leads(limit=1)), None)
        if sample is None:
            messagebox.showinfo("No leads",
                                "Collect some leads first, so the merge fields "
                                "have something to fill in.")
            return
        # Read the editor here, on the Tk thread. The job used to read these
        # widgets from the worker.
        subject = outreach.render(self.subject_var.get(), sample)
        body = outreach.render(self.body_text.get("1.0", tk.END).strip(), sample)

        def make_job():
            def job():
                sender = outreach.EmailSender(self.settings, logger=self.logger)
                try:
                    ok, detail = sender.send(address, "[TEST] " + subject, body)
                finally:
                    sender.close()
                self.post(lambda: self.banner.set(
                    "ok" if ok else "error",
                    "Test sent to " + address if ok else "Test failed",
                    "Merge fields were filled from your top lead." if ok else detail))
            return job

        self._spawn(make_job, "Sending a test to yourself…")

    def test_smtp(self):
        def make_job():
            def job():
                ok, detail = outreach.EmailSender(
                    self.settings, logger=self.logger).verify_login()
                self.post(lambda: self.banner.set(
                    "ok" if ok else "error",
                    "Mail server connection works." if ok else "Could not sign in.",
                    detail))
            return job

        self._spawn(make_job, "Testing the mail server connection…")

    # ------------------------------------------------------- run: after

    def _after_run(self, pipeline, result, label, quiet=False, export_options=None):
        """Report the outcome honestly — including the outcomes that are not
        failures but are not successes either.

        Runs on the worker. export_options is the snapshot taken by
        _export_options before the run started; pass None to skip exporting
        here, as the three-pass enrichment does so it can export once at the end.
        """
        if export_options is not None and self.settings.get("auto_export") \
                and result.added:
            try:
                self._export(export_options)
            except Exception as e:
                self.logger.error("Export failed: %s", e)

        if quiet and result.status in (RUN_OK, RUN_EMPTY):
            return

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
                                "{0} saved. {1}".format(result.added,
                                                        result.message or ""))
            else:
                self.banner.set(
                    "ok", "{0}: {1} new.".format(label, result.added),
                    result.message or self._export_target())
        self.post(show)

    def _export_target(self):
        if self.settings.get("output") == "google":
            return "Written to Google Sheets."
        return "Written to " + os.path.abspath(self.settings.get("excel_path")) + "."

    def _export_options(self, min_score=None):
        """Everything an export needs from the window, read now.

        Call on the Tk thread, before a run starts. The export itself runs on
        the worker and reads only this snapshot — it used to read the Call List
        and ICP controls directly, from the worker, which Tk does not allow.
        Loading the exporters here also keeps their first import on this thread.
        """
        return {
            "min_score": min_score,
            "call_list": int(self.call_list_var.get() or 0),
            "icp_only": bool(self.icp_only_var.get()),
            "exporters": _load_exporters(),
        }

    def _export(self, options):
        """Write the spreadsheet from a snapshot taken by _export_options.
        Safe to call from a worker: it touches no widgets."""
        exporters = options["exporters"]
        if self.settings.get("output") == "google":
            credentials = self.settings.get("google_credentials")
            if not credentials or not os.path.exists(credentials):
                raise RuntimeError("Select a valid Google service-account JSON "
                                   "file in Settings.")
            exporter = exporters.GoogleSheetsExporter(
                credentials, self.settings.get("google_spreadsheet"),
                logger=self.logger)
            return exporter.export(self.store, min_score=options["min_score"])
        exporter = exporters.ExcelExporter(self.settings.get("excel_path"),
                                           logger=self.logger)
        return exporter.export(self.store, min_score=options["min_score"],
                               call_list=options["call_list"],
                               call_list_icp=options["icp_only"])

    # ===================================================================
    # Live updates during a run
    # ===================================================================

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
        self.metrics["total"].set(sum(b["leads"] for b in self.sheet_totals.values()))
        self.metrics["with_email"].set(
            sum(b["emails"] for b in self.sheet_totals.values()))

    # ===================================================================
    # Extra windows
    # ===================================================================

    def run_health_check(self):
        def make_job():
            # Read on the Tk thread; the job used to read this dropdown from
            # the worker.
            state = self.pt_state_var.get()
            proxies = config.proxies_for(self.settings)

            def job():
                self.post(lambda: self.banner.set(
                    "busy", "Checking every source…"))
                reports = health.run_all(proxies=proxies, logger=self.logger,
                                         state=state)
                self.post(lambda: self._show_health(reports))
            return job

        self._spawn(make_job, "Running health check…")

    def _show_health(self, reports):
        failed = [r["source"] for r in reports if not r["ok"]]
        if failed:
            self.banner.set("error", "Not working: " + ", ".join(failed),
                            "See the details before starting a run.")
        else:
            degraded = [r["source"] for r in reports if r.get("degraded")]
            self.banner.set(
                "ok", "All sources are working.",
                "Reduced capability: " + ", ".join(degraded) if degraded else "")

        window = tk.Toplevel(self.root)
        window.title("Health Check")
        window.geometry("740x560")
        window.configure(bg=T.GROUND)
        outer, frame = T.scrollable(window)
        outer.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        for report in reports:
            tone = "ok" if report["ok"] else "error"
            if report.get("degraded"):
                tone = "warn"
            background, foreground = T.TONES[tone]
            card = T.Card(frame, padding=14)
            card.pack(fill=tk.X, pady=(0, 10))
            head = tk.Frame(card.inner, bg=T.SURFACE)
            head.pack(fill=tk.X, pady=(0, 8))
            tk.Label(head, text=report["source"], font=T.F_HEAD, bg=T.SURFACE,
                     fg=T.INK, anchor="w").pack(side=tk.LEFT)
            tk.Label(head, text=" " + ("OK" if report["ok"] else "FAILED") + " ",
                     font=T.F_LABEL, bg=background, fg=foreground,
                     padx=7, pady=3).pack(side=tk.RIGHT)
            for step in report["steps"]:
                tk.Label(card.inner,
                         text=("✓  " if step["ok"] else "✕  ") + step["name"],
                         font=(T.FONT, 9, "bold"), bg=T.SURFACE,
                         fg=T.OK if step["ok"] else T.BAD,
                         anchor="w").pack(fill=tk.X)
                tk.Label(card.inner, text="     " + step["detail"], font=T.F_SMALL,
                         bg=T.SURFACE, fg=T.INK_SOFT, anchor="w", justify="left",
                         wraplength=640).pack(fill=tk.X, pady=(0, 6))

    def show_run_history(self):
        window = tk.Toplevel(self.root)
        window.title("Run History")
        window.geometry("900x440")
        tree = ttk.Treeview(window, columns=("action", "status", "added", "note"),
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
        tree.column("note", width=430)
        tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)
        tree.tag_configure("failed", foreground=T.BAD)
        tree.tag_configure("empty", foreground=T.WARN)

        for run in self.store.recent_runs(50):
            status = (run["status"] or "").lower()
            tree.insert("", tk.END,
                        text=(run["started_at"] or "")[:19].replace("T", " "),
                        values=(run["action"], status.upper(), run["added"] or 0,
                                run["message"] or ""),
                        tags=(status,) if status in ("failed", "empty") else ())

    def show_suppressions(self):
        window = tk.Toplevel(self.root)
        window.title("Do Not Contact")
        window.geometry("640x460")
        window.configure(bg=T.GROUND)
        frame = tk.Frame(window, bg=T.GROUND)
        frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        tk.Label(frame,
                 text="Nobody on this list is ever emailed again. Bounced "
                      "addresses are added automatically; add anyone who asks "
                      "to be removed.",
                 font=T.F_SMALL, bg=T.GROUND, fg=T.INK_SOFT, anchor="w",
                 justify="left", wraplength=590).pack(fill=tk.X, pady=(0, 10))

        tree = ttk.Treeview(frame, columns=("added", "reason"),
                            show="tree headings")
        tree.heading("#0", text="Address or @domain")
        tree.heading("added", text="Added")
        tree.heading("reason", text="Reason")
        tree.column("#0", width=250)
        tree.column("added", width=90)
        tree.column("reason", width=210)
        tree.pack(fill=tk.BOTH, expand=True)
        for row in self.store.suppressions():
            tree.insert("", tk.END, text=row["value"],
                        values=((row["added_at"] or "")[:10], row["reason"] or ""))

        entry_row = tk.Frame(frame, bg=T.GROUND)
        entry_row.pack(fill=tk.X, pady=(10, 0))
        new_value = tk.StringVar()
        ttk.Entry(entry_row, textvariable=new_value).pack(side=tk.LEFT, fill=tk.X,
                                                          expand=True)

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

    def open_result(self):
        try:
            if self.settings.get("output") == "google":
                exporter = _load_exporters().GoogleSheetsExporter(
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
                self._export(self._export_options())
            _open_in_default_app(os.path.abspath(path))
        except Exception as e:
            messagebox.showerror("Could not open", str(e))

    def show_dashboard(self):
        """The deeper analytics view. matplotlib is imported here and nowhere
        else — it is a heavy dependency that only this window needs."""
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            import matplotlib.pyplot as plt
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        except ImportError:
            messagebox.showerror(
                "Analytics unavailable",
                "matplotlib is required.\nInstall it with: pip install matplotlib")
            return
        if not self.store:
            return

        stats = self.store.get_stats()
        runs = list(reversed(self.store.recent_runs(15)))

        BLUE = "#2a78d6"
        GRID = "#e1e0d9"
        AXIS = "#c3c2b7"
        PAPER = "#fcfcfb"

        window = tk.Toplevel(self.root)
        window.title("Analytics")
        window.geometry("1060x780")
        window.configure(bg=PAPER)

        total = stats["total"] or 1
        ttk.Label(window,
                  text="{0} leads   |   {1} with email ({2:.1f}%)   |   "
                       "{3} verified   |   average score {4}".format(
                           stats["total"], stats["with_email"],
                           stats["with_email"] / total * 100,
                           stats["verified_email"], stats["avg_score"]),
                  font=(T.FONT, 10, "bold")).pack(pady=8)

        figure = plt.Figure(figsize=(11, 8), dpi=100, facecolor=PAPER)
        figure.subplots_adjust(hspace=0.65, wspace=0.4, left=0.16, right=0.96,
                               top=0.94, bottom=0.07)
        axes = [figure.add_subplot(2, 2, i) for i in range(1, 5)]

        def style(axis, title):
            axis.set_facecolor(PAPER)
            axis.set_title(title, fontsize=10, color=T.INK, loc="left",
                           fontweight="bold")
            axis.tick_params(colors=T.INK_SOFT, labelsize=8)
            for spine in ("top", "right"):
                axis.spines[spine].set_visible(False)
            for spine in ("left", "bottom"):
                axis.spines[spine].set_color(AXIS)

        def empty(axis, text):
            axis.text(0.5, 0.5, text, ha="center", va="center", color=T.INK_FAINT,
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
            axes[1].barh([s for s, _ in reversed(top_states)], rates, color=BLUE,
                         height=0.6, zorder=3)
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


def _load_exporters():
    """Import the spreadsheet exporters on first use.

    Deferred because lead_manager pulls in openpyxl and numpy — over a second
    of startup for something only an export needs. Every caller is a Tk
    handler that runs before any worker is started, so this import always
    happens on the main thread and never races another import.
    """
    import lead_manager
    return lead_manager


def _open_in_default_app(path):
    """Open a file the way the desktop would. os.startfile is Windows-only, so
    calling it directly would crash the app the moment this runs anywhere else —
    including the Linux server that is the natural home for scheduled runs."""
    import subprocess
    import sys

    if hasattr(os, "startfile"):          # Windows
        os.startfile(path)                # noqa: S606 - the user's own file
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


if __name__ == "__main__":
    root = tk.Tk()
    LeadGenApp(root)
    root.mainloop()
