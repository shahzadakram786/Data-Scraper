"""The lead-type controls: the pills-and-button bar and the picker. The
Settings editor joins them in the next change.

Kept out of app.py, which is a window of 2,700 lines already. These widgets
hold no rules about which leads match — that is lead_types.py — they only
show and change what is chosen.
"""

import tkinter as tk
from tkinter import ttk

import lead_types
import ui_theme as T


class LeadTypeBar(tk.Frame):
    """The chosen lead types as pills, with the button that changes them.

    Collect, Results and Outreach each have one, all showing the same shared
    selection. Clicking a pill drops that type; the button opens the picker.
    The bar keeps no state of its own — show() redraws it.
    """

    MAX_PILLS = 6

    def __init__(self, master, on_choose, on_remove, bg=T.SURFACE):
        super().__init__(master, bg=bg)
        self._bg = bg
        self._on_remove = on_remove
        row = tk.Frame(self, bg=bg)
        row.pack(fill=tk.X)
        self.pills = tk.Frame(row, bg=bg)
        self.pills.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.button = ttk.Button(row, text="Choose lead types…", style="Ghost.TButton",
                                 command=on_choose)
        self.button.pack(side=tk.RIGHT)
        self.hint = tk.Label(self, text="", font=T.F_SMALL, bg=bg, fg=T.INK_FAINT,
                             anchor="w", justify="left", wraplength=640)
        self.hint.pack(fill=tk.X, pady=(6, 0))

    def show(self, selection, note=""):
        for child in self.pills.winfo_children():
            child.destroy()
        if not selection.active:
            tk.Label(self.pills, text="Every lead", font=T.F_BODY, bg=self._bg,
                     fg=T.INK_SOFT).pack(side=tk.LEFT)
            text = "Nothing chosen, so every lead is included."
        else:
            for lead_type in selection.selected[:self.MAX_PILLS]:
                pill = T.Pill(self.pills, lead_type.name + "  ×", "brand", bg=self._bg)
                pill.configure(cursor="hand2")
                pill.bind("<Button-1>",
                          lambda _e, type_id=lead_type.id: self._on_remove(type_id))
                pill.pack(side=tk.LEFT, padx=(0, 6))
            extra = len(selection.selected) - self.MAX_PILLS
            if extra > 0:
                tk.Label(self.pills, text="+{0} more".format(extra), font=T.F_SMALL,
                         bg=self._bg, fg=T.INK_FAINT).pack(side=tk.LEFT)
            text = ("Leads that need any of these. Click a name to remove it."
                    if len(selection.selected) > 1
                    else "Leads that need this. Click the name to remove it.")
        self.hint.configure(text=text + ("  " + note if note else ""))

    def pill_texts(self):
        return [child.cget("text").strip() for child in self.pills.winfo_children()]


class LeadTypePicker(tk.Toplevel):
    """Choose lead types: the switched-on types in their groups, with a filter.

    on_done(ids) receives the new selection when "Use these" is pressed;
    closing the window or Cancel changes nothing.
    """

    def __init__(self, master, types, selected_ids, on_done, on_manage=None):
        super().__init__(master)
        self.title("Choose lead types")
        self.configure(bg=T.SURFACE)
        self.transient(master.winfo_toplevel())
        self._types = [t for t in types if t.enabled]
        chosen = set(selected_ids or ())
        self._vars = {t.id: tk.BooleanVar(value=t.id in chosen) for t in self._types}
        self._on_done = on_done
        self._on_manage = on_manage
        self._shown = []

        top = tk.Frame(self, bg=T.SURFACE)
        top.pack(fill=tk.X, padx=18, pady=(16, 8))
        tk.Label(top, text="Filter", font=T.F_BODY, bg=T.SURFACE,
                 fg=T.INK).pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.filter_var, width=32)
        entry.pack(side=tk.LEFT, padx=8)
        self.filter_var.trace_add("write", lambda *_args: self._render())

        self.body = tk.Frame(self, bg=T.SURFACE)
        self.body.pack(fill=tk.BOTH, expand=True, padx=18)

        actions = tk.Frame(self, bg=T.SURFACE)
        actions.pack(fill=tk.X, padx=18, pady=16)
        ttk.Button(actions, text="Clear", style="Ghost.TButton",
                   command=self.clear).pack(side=tk.LEFT)
        if on_manage:
            ttk.Button(actions, text="Manage lead types…", style="Ghost.TButton",
                       command=self._manage).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Use these", style="Step.TButton",
                   command=self.use).pack(side=tk.RIGHT)
        ttk.Button(actions, text="Cancel", style="Ghost.TButton",
                   command=self.destroy).pack(side=tk.RIGHT, padx=6)

        self._render()
        self.bind("<Escape>", lambda _e: self.destroy())
        entry.focus_set()

    def _render(self):
        for child in self.body.winfo_children():
            child.destroy()
        # Names and groups only: descriptions mention neighbouring services
        # ("no online booking" under Software Development), and a filter for
        # "book" should not turn those up.
        needle = self.filter_var.get().strip().lower()
        visible = [t for t in self._types
                   if not needle or needle in (t.name + " " + t.group).lower()]
        self._shown = [t.id for t in visible]
        if not visible:
            tk.Label(self.body, text="No lead type matches “{0}”.".format(
                self.filter_var.get().strip()), font=T.F_SMALL, bg=T.SURFACE,
                fg=T.INK_FAINT).grid(row=0, column=0, sticky="w")
            return
        for index, (group, members) in enumerate(lead_types.grouped(visible)):
            frame = tk.Frame(self.body, bg=T.SURFACE)
            frame.grid(row=index // 2, column=index % 2, sticky="nw",
                       padx=(0, 28), pady=(0, 14))
            tk.Label(frame, text=group.upper(), font=(T.FONT, 7, "bold"),
                     bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").pack(fill=tk.X, pady=(0, 4))
            for lead_type in members:
                ttk.Checkbutton(frame, text=lead_type.name,
                                variable=self._vars[lead_type.id]).pack(anchor="w")
                if lead_type.description:
                    tk.Label(frame, text=lead_type.description, font=T.F_SMALL,
                             bg=T.SURFACE, fg=T.INK_FAINT, anchor="w", justify="left",
                             wraplength=300).pack(anchor="w", padx=(22, 0), pady=(0, 4))

    def visible_ids(self):
        return list(self._shown)

    def selected_ids(self):
        return [t.id for t in self._types if self._vars[t.id].get()]

    def clear(self):
        for var in self._vars.values():
            var.set(False)

    def use(self):
        ids = self.selected_ids()
        self.destroy()
        self._on_done(ids)

    def _manage(self):
        self.destroy()
        self._on_manage()
