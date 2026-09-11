"""Visual language for the desktop app: palette, ttk styles, reusable widgets.

Pulled out of app.py because the window was carrying its own styling, its own
layout and its own logic in one 1,700-line file, and the styling is the part
that every screen needs to agree on.

The palette is deliberately narrow. A colour here means something: the six
status colours are the same six the spreadsheet paints its Email Status column
with, so a lead that reads amber on screen reads amber in the file. Everything
else is a neutral or the one brand navy, and nothing is coloured for decoration.

Tk gives us no rounded corners, no shadows and no opacity, so hierarchy has to
come from type size, weight, spacing and a single hairline border. That is a
constraint worth designing into rather than fighting.
"""

import tkinter as tk
from tkinter import ttk


# --- palette --------------------------------------------------------------

# Navigation rail. Dark, so the eye treats it as chrome and the content area
# reads as the page.
NAV_BG = "#16293c"
NAV_BG_HOVER = "#1d3550"
NAV_ACTIVE = "#1F4E78"
NAV_TEXT = "#b9c9d9"
NAV_TEXT_ACTIVE = "#ffffff"
NAV_MUTED = "#7793ad"
NAV_RULE = "#23405c"

# Content.
GROUND = "#f4f6f8"       # the page behind the cards
SURFACE = "#ffffff"      # cards, inputs
SURFACE_SUNK = "#eef1f4"  # code blocks, inset panels
INK = "#131820"
INK_SOFT = "#4a5561"
INK_FAINT = "#78838f"
RULE = "#dde3e9"
RULE_STRONG = "#c6cfd8"

BRAND = "#1F4E78"
BRAND_SOFT = "#e7eff7"
BRAND_INK = "#16395a"

# Semantic. Kept separate from the brand: these encode state, not identity.
OK = "#2E7D32";      OK_BG = "#e4f3e5"
INFO = "#1F6FB2";    INFO_BG = "#e3eefa"
WARN = "#B45309";    WARN_BG = "#fbf1e1"
CAUTION = "#C2410C"; CAUTION_BG = "#fcece3"
BAD = "#B3261E";     BAD_BG = "#fbe6e4"
MUTED = "#6b7683";   MUTED_BG = "#ebeef1"

# Banner tones: (background, foreground).
TONES = {
    "idle":  (MUTED_BG, "#44484d"),
    "busy":  (INFO_BG, "#14508c"),
    "ok":    (OK_BG, "#1d6b2a"),
    "warn":  (WARN_BG, "#8a5310"),
    "error": (BAD_BG, "#a3241c"),
}

# Email Status -> (fg, bg). Matches lead_manager.STATUS_FILLS.
STATUS_TONES = {
    "valid":      (OK, OK_BG),
    "role":       (INFO, INFO_BG),
    "accept-all": (WARN, WARN_BG),
    "unknown":    (MUTED, MUTED_BG),
    "risky":      (CAUTION, CAUTION_BG),
    "disposable": (BAD, BAD_BG),
    "invalid":    (BAD, BAD_BG),
}

# --- type -----------------------------------------------------------------

FONT = "Segoe UI"
MONO = "Consolas"

F_DISPLAY = (FONT, 21, "bold")
F_TITLE = (FONT, 15, "bold")
F_HEAD = (FONT, 11, "bold")
F_BODY = (FONT, 10)
F_SMALL = (FONT, 9)
F_TINY = (FONT, 8)
F_METRIC = (FONT, 26, "bold")
F_LABEL = (FONT, 8, "bold")
F_MONO = (MONO, 9)


def install(root):
    """Apply the whole visual language to a root window."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=GROUND)

    style.configure(".", font=F_BODY, background=GROUND, foreground=INK)

    # Surfaces
    style.configure("TFrame", background=GROUND)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("Sunk.TFrame", background=SURFACE_SUNK)
    style.configure("Nav.TFrame", background=NAV_BG)

    # Text
    style.configure("TLabel", background=GROUND, foreground=INK, font=F_BODY)
    style.configure("Card.TLabel", background=SURFACE, foreground=INK, font=F_BODY)
    style.configure("CardMuted.TLabel", background=SURFACE, foreground=INK_FAINT,
                    font=F_SMALL)
    style.configure("CardHead.TLabel", background=SURFACE, foreground=INK,
                    font=F_HEAD)
    style.configure("Display.TLabel", background=GROUND, foreground=INK,
                    font=F_DISPLAY)
    style.configure("Title.TLabel", background=GROUND, foreground=INK, font=F_TITLE)
    style.configure("Muted.TLabel", background=GROUND, foreground=INK_FAINT,
                    font=F_SMALL)
    style.configure("Soft.TLabel", background=GROUND, foreground=INK_SOFT)
    style.configure("Metric.TLabel", background=SURFACE, foreground=BRAND,
                    font=F_METRIC)
    style.configure("MetricLabel.TLabel", background=SURFACE, foreground=INK_FAINT,
                    font=F_SMALL)
    style.configure("Label.TLabel", background=SURFACE, foreground=INK_FAINT,
                    font=F_LABEL)

    # Controls
    style.configure("TCheckbutton", background=SURFACE, foreground=INK, font=F_BODY)
    style.map("TCheckbutton", background=[("active", SURFACE)])
    style.configure("Ground.TCheckbutton", background=GROUND, foreground=INK)
    style.map("Ground.TCheckbutton", background=[("active", GROUND)])
    style.configure("TRadiobutton", background=SURFACE, foreground=INK, font=F_BODY)
    style.map("TRadiobutton", background=[("active", SURFACE)])

    style.configure("TButton", font=F_BODY, padding=(12, 7))
    style.configure("Primary.TButton", font=(FONT, 10, "bold"), padding=(20, 10),
                    background=BRAND, foreground="#ffffff", borderwidth=0)
    style.map("Primary.TButton",
              background=[("active", BRAND_INK), ("disabled", RULE_STRONG)],
              foreground=[("disabled", INK_FAINT)])
    style.configure("Ghost.TButton", font=F_SMALL, padding=(10, 6))
    style.configure("Step.TButton", font=(FONT, 9, "bold"), padding=(14, 7))

    style.configure("TEntry", fieldbackground=SURFACE, borderwidth=1, padding=5)
    style.configure("TCombobox", fieldbackground=SURFACE, borderwidth=1, padding=4)

    style.configure("TLabelframe", background=SURFACE, bordercolor=RULE,
                    borderwidth=1, relief="solid")
    style.configure("TLabelframe.Label", background=SURFACE, foreground=INK_SOFT,
                    font=F_LABEL)

    style.configure("TNotebook", background=GROUND, borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 8), font=F_BODY)

    style.configure("Treeview", rowheight=25, fieldbackground=SURFACE,
                    background=SURFACE, font=F_SMALL, borderwidth=0)
    style.configure("Treeview.Heading", font=F_LABEL, background=SURFACE_SUNK,
                    foreground=INK_FAINT, relief="flat", padding=(6, 6))
    style.map("Treeview", background=[("selected", BRAND_SOFT)],
              foreground=[("selected", BRAND_INK)])

    style.configure("Thin.Horizontal.TProgressbar", troughcolor=SURFACE_SUNK,
                    background=BRAND, borderwidth=0, thickness=6)
    style.configure("TSeparator", background=RULE)
    style.configure("Vertical.TScrollbar", background=SURFACE_SUNK,
                    troughcolor=GROUND, borderwidth=0, arrowsize=12)
    return style


# --- widgets --------------------------------------------------------------

class Card(tk.Frame):
    """A white panel with a hairline border.

    Tk has no shadow and no radius, so a card is a fill plus one 1px border —
    which is enough to say "this is one object" as long as it is not applied to
    everything on the screen.
    """

    def __init__(self, master, padding=16, **kwargs):
        super().__init__(master, bg=SURFACE, highlightthickness=1,
                         highlightbackground=RULE, highlightcolor=RULE, bd=0,
                         **kwargs)
        self.inner = tk.Frame(self, bg=SURFACE)
        self.inner.pack(fill=tk.BOTH, expand=True, padx=padding, pady=padding)


class SectionTitle(tk.Frame):
    """A heading with an optional one-line explanation under it."""

    def __init__(self, master, title, note="", bg=GROUND):
        super().__init__(master, bg=bg)
        tk.Label(self, text=title, font=F_TITLE, bg=bg, fg=INK,
                 anchor="w").pack(fill=tk.X)
        if note:
            tk.Label(self, text=note, font=F_SMALL, bg=bg, fg=INK_FAINT,
                     anchor="w", justify="left", wraplength=760).pack(
                         fill=tk.X, pady=(3, 0))


class Metric(tk.Frame):
    """One big number with a caption. Used only where the figure is the point."""

    def __init__(self, master, label, value="—", tone=None, bg=SURFACE):
        super().__init__(master, bg=bg)
        self.var = tk.StringVar(value=value)
        self.value_label = tk.Label(self, textvariable=self.var, font=F_METRIC,
                                    bg=bg, fg=tone or BRAND, anchor="w")
        self.value_label.pack(fill=tk.X)
        tk.Label(self, text=label, font=F_SMALL, bg=bg, fg=INK_FAINT,
                 anchor="w").pack(fill=tk.X)

    def set(self, value):
        self.var.set(str(value))


class Pill(tk.Label):
    """A small state chip. Colour carries the meaning, text repeats it — the
    two together so it still reads without colour vision."""

    def __init__(self, master, text, tone="muted", bg=SURFACE):
        fg, fill = {
            "ok": (OK, OK_BG), "info": (INFO, INFO_BG), "warn": (WARN, WARN_BG),
            "caution": (CAUTION, CAUTION_BG), "bad": (BAD, BAD_BG),
            "muted": (MUTED, MUTED_BG), "brand": (BRAND, BRAND_SOFT),
        }.get(tone, (MUTED, MUTED_BG))
        super().__init__(master, text=" " + text + " ", font=F_LABEL,
                         bg=fill, fg=fg, padx=6, pady=3)


class Bar(tk.Canvas):
    """A single horizontal proportion bar.

    Drawn rather than charted: at this size a matplotlib figure would cost more
    to render than the information is worth, and a canvas rectangle is exact.
    """

    def __init__(self, master, height=8, bg=SURFACE, track=SURFACE_SUNK):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self._height = height
        self._track = track
        self._segments = []
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_segments(self, segments):
        """segments: [(fraction, colour), ...] summing to <= 1."""
        self._segments = segments
        self._redraw()

    def _redraw(self):
        self.delete("all")
        width = self.winfo_width() or 1
        self.create_rectangle(0, 0, width, self._height, fill=self._track,
                              outline="")
        x = 0
        for fraction, colour in self._segments:
            span = max(0, min(width - x, width * fraction))
            if span > 0:
                self.create_rectangle(x, 0, x + span, self._height, fill=colour,
                                      outline="")
                x += span


class StepRow(tk.Frame):
    """One stage of a multi-stage job: state, name, what it does, and its own
    button — so a three-pass sequence reads as three things to do rather than a
    radio group where only one can be chosen at a time."""

    def __init__(self, master, index, title, description, command,
                 button_text="Run"):
        super().__init__(master, bg=SURFACE)
        self.columnconfigure(2, weight=1)

        self.marker = tk.Label(self, text=str(index), font=(FONT, 9, "bold"),
                               bg=SURFACE_SUNK, fg=INK_SOFT, width=3, height=1)
        self.marker.grid(row=0, column=0, rowspan=2, padx=(0, 13), pady=2,
                         sticky="n")

        tk.Label(self, text=title, font=F_HEAD, bg=SURFACE, fg=INK,
                 anchor="w").grid(row=0, column=1, columnspan=2, sticky="w")

        self.status = tk.Label(self, text="", font=F_SMALL, bg=SURFACE,
                               fg=INK_FAINT, anchor="w")
        self.status.grid(row=0, column=3, sticky="e", padx=(10, 12))

        tk.Label(self, text=description, font=F_SMALL, bg=SURFACE, fg=INK_SOFT,
                 anchor="w", justify="left", wraplength=520).grid(
                     row=1, column=1, columnspan=3, sticky="w", pady=(2, 0))

        self.button = ttk.Button(self, text=button_text, style="Step.TButton",
                                 command=command)
        self.button.grid(row=0, column=4, rowspan=2, sticky="e", padx=(12, 0))

    def set_state(self, text, tone="muted", done=False):
        self.status.configure(text=text,
                              fg={"ok": OK, "warn": WARN, "muted": INK_FAINT,
                                  "brand": BRAND}.get(tone, INK_FAINT))
        self.marker.configure(
            bg=OK_BG if done else SURFACE_SUNK,
            fg=OK if done else INK_SOFT,
            text="✓" if done else self.marker.cget("text"))


class BarChart(tk.Canvas):
    """Horizontal bars with a label and a value on every row.

    Drawn on a canvas rather than handed to matplotlib: these are rankings of a
    dozen items, and a figure would cost an import, a render and a redraw on
    every resize to say the same thing. One scale places every bar, and every
    label names a value the chart actually reaches.
    """

    ROW = 26
    LABEL_W = 132
    VALUE_W = 62

    def __init__(self, master, bg=SURFACE, colour=BRAND, empty_text="No data yet"):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, height=10)
        self._bg = bg
        self._colour = colour
        self._empty = empty_text
        self._rows = []
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_rows(self, rows):
        """rows: [(label, value, colour|None), ...] — already sorted."""
        self._rows = list(rows)
        self.configure(height=max(self.ROW * len(self._rows), 28))
        self._redraw()

    def _redraw(self):
        self.delete("all")
        width = self.winfo_width() or 1

        if not self._rows:
            self.create_text(12, 14, text=self._empty, anchor="w", fill=INK_FAINT,
                             font=F_SMALL)
            return

        largest = max((value for _label, value, _c in self._rows), default=0) or 1
        track = max(40, width - self.LABEL_W - self.VALUE_W)

        for index, (label, value, colour) in enumerate(self._rows):
            y = index * self.ROW + self.ROW / 2
            self.create_text(0, y, text=_ellipsis(label, 20), anchor="w",
                             fill=INK_SOFT, font=F_SMALL)

            span = track * (value / largest)
            self.create_rectangle(self.LABEL_W, y - 6,
                                  self.LABEL_W + track, y + 6,
                                  fill=SURFACE_SUNK, outline="")
            if span > 0:
                self.create_rectangle(self.LABEL_W, y - 6,
                                      self.LABEL_W + max(span, 2), y + 6,
                                      fill=colour or self._colour, outline="")

            self.create_text(self.LABEL_W + track + 8, y,
                             text="{0:,}".format(value) if isinstance(value, int)
                             else str(value),
                             anchor="w", fill=INK, font=F_SMALL)


class Sparkline(tk.Canvas):
    """A small trend line — the shape of the last N runs, not a chart to read
    exact values off. The final point is emphasised because "where are we now"
    is the question it answers."""

    def __init__(self, master, bg=SURFACE, colour=BRAND, height=64):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, height=height)
        self._bg = bg
        self._colour = colour
        self._values = []
        self._height = height
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_values(self, values):
        self._values = [float(v or 0) for v in values]
        self._redraw()

    def _redraw(self):
        self.delete("all")
        width = self.winfo_width() or 1
        height = self._height
        pad = 8

        if len(self._values) < 2:
            self.create_text(4, height / 2, text="Not enough runs yet",
                             anchor="w", fill=INK_FAINT, font=F_SMALL)
            return

        top = max(self._values + [1])
        step = (width - pad * 2) / (len(self._values) - 1)

        points = []
        for index, value in enumerate(self._values):
            x = pad + step * index
            y = height - pad - (height - pad * 2) * (value / top)
            points.extend([x, y])

        self.create_line(pad, height - pad, width - pad, height - pad,
                         fill=RULE, width=1)
        self.create_line(*points, fill=self._colour, width=2, smooth=False)
        self.create_oval(points[-2] - 3.5, points[-1] - 3.5,
                         points[-2] + 3.5, points[-1] + 3.5,
                         fill=self._colour, outline=SURFACE, width=2)
        self.create_text(width - pad, pad, text="{0:.0f}%".format(self._values[-1]),
                         anchor="ne", fill=INK_SOFT, font=F_SMALL)


def _ellipsis(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def hairline(master, bg=GROUND):
    return tk.Frame(master, height=1, bg=RULE) if bg else None


def scrollable(master, bg=GROUND):
    """A vertically scrolling area. Returns (outer, inner) — pack content into
    inner. Screens outgrow a laptop window quickly and a panel that simply
    clips its bottom is a bug, not a layout."""
    outer = tk.Frame(master, bg=bg)
    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, bd=0)
    bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=bg)

    window = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=bar.set)

    def on_inner(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def on_canvas(event):
        canvas.itemconfigure(window, width=event.width)

    inner.bind("<Configure>", on_inner)
    canvas.bind("<Configure>", on_canvas)

    def on_wheel(event):
        if canvas.winfo_exists():
            canvas.yview_scroll(int(-event.delta / 120), "units")

    canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", on_wheel))
    canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

    canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    bar.pack(side=tk.RIGHT, fill=tk.Y)
    return outer, inner
