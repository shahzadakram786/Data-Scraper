"""The lead-type controls: the pills-and-button bar, the picker, and the
Settings editor.

Kept out of app.py, which is a window of 2,700 lines already. These widgets
hold no rules about which leads match — that is lead_types.py — they only
show and change what is chosen and defined.
"""

import tkinter as tk
from tkinter import ttk, messagebox

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


class LeadTypesCard(tk.Frame):
    """Settings → Lead Types: every type with its switch and match count, and
    the buttons that add, edit, copy, switch, delete and reset them.

    on_save(types) receives the whole new list whenever anything changes; the
    window saves it and redraws. Built-ins can be switched off and edited but
    not deleted, so a later version can always find them.
    """

    def __init__(self, master, on_save, confirm=None, bg=T.SURFACE):
        super().__init__(master, bg=bg)
        self._on_save = on_save
        self._confirm = confirm or messagebox.askyesno
        self.types = []
        self.counts = {}

        self.note = tk.Label(self, text="", font=T.F_SMALL, anchor="w", justify="left",
                             wraplength=760, padx=10, pady=8)
        self.tree = ttk.Treeview(self, columns=("on", "group", "checks", "matches"),
                                 show="tree headings", height=10, selectmode="browse")
        self.tree.heading("#0", text="Lead type")
        self.tree.column("#0", width=230)
        for column, label, width, anchor in (("on", "On", 50, tk.CENTER),
                                             ("group", "Group", 170, tk.W),
                                             ("checks", "Checks", 70, tk.CENTER),
                                             ("matches", "Matches", 80, tk.CENTER)):
            self.tree.heading(column, text=label)
            self.tree.column(column, width=width, anchor=anchor)
        self.tree.pack(fill=tk.X)
        self.tree.bind("<Double-1>", lambda _e: self.edit())

        buttons = tk.Frame(self, bg=bg)
        buttons.pack(fill=tk.X, pady=(10, 0))
        for text, command in (("Add…", self.add), ("Edit…", self.edit),
                              ("Duplicate", self.duplicate),
                              ("Switch on/off", self.toggle), ("Delete", self.delete)):
            ttk.Button(buttons, text=text, style="Ghost.TButton",
                       command=command).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="Reset to built-in", style="Ghost.TButton",
                   command=self.reset).pack(side=tk.RIGHT)
        self.message = tk.Label(self, text="", font=T.F_SMALL, bg=bg, fg=T.INK_FAINT,
                                anchor="w")
        self.message.pack(fill=tk.X, pady=(6, 0))

    def show(self, types, counts=None, problem=""):
        self.types = [t.copy() for t in types]
        if counts is not None:
            self.counts = dict(counts)
        if problem:
            background, foreground = T.TONES["warn"]
            self.note.configure(text=problem, bg=background, fg=foreground)
            self.note.pack(fill=tk.X, pady=(0, 10), before=self.tree)
        else:
            self.note.pack_forget()
        self._fill()

    def show_counts(self, counts):
        self.counts = dict(counts)
        self._fill()

    def _fill(self):
        keep = self.selected_id()
        self.tree.delete(*self.tree.get_children())
        for t in self.types:
            checks = len(t.checks) + len(t.mentions) + len(t.not_mentions)
            matches = self.counts.get(t.id, "—") if t.enabled else "—"
            self.tree.insert("", tk.END, iid=t.id, text=t.name,
                             values=("On" if t.enabled else "Off", t.group, checks, matches))
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)

    def selected_id(self):
        chosen = self.tree.selection()
        return chosen[0] if chosen else None

    def _selected(self):
        type_id = self.selected_id()
        current = next((t for t in self.types if t.id == type_id), None)
        if current is None:
            self.message.configure(text="Pick a lead type in the list first.")
        return current

    def _commit(self, types, message):
        self.types = types
        self._fill()
        self.message.configure(text=message)
        self._on_save([t.copy() for t in types])

    def add(self):
        LeadTypeEditor(self, None, self.types, on_save=self._added)

    def _added(self, lead_type):
        self._commit(self.types + [lead_type], 'Added "{0}".'.format(lead_type.name))

    def edit(self):
        current = self._selected()
        if current is not None:
            LeadTypeEditor(self, current, self.types, on_save=self._edited)

    def _edited(self, lead_type):
        self._commit([lead_type if t.id == lead_type.id else t for t in self.types],
                     'Saved "{0}".'.format(lead_type.name))

    def duplicate(self):
        current = self._selected()
        if current is None:
            return
        copy = current.copy()
        copy.name = lead_types.copy_name(current.name, self.types)
        copy.id = lead_types.new_id(copy.name, {t.id for t in self.types})
        copy.builtin = False
        self._commit(self.types + [copy],
                     'Added "{0}". Edit it to make it your own.'.format(copy.name))

    def toggle(self):
        current = self._selected()
        if current is None:
            return
        changed = current.copy()
        changed.enabled = not current.enabled
        self._commit([changed if t.id == changed.id else t for t in self.types],
                     '"{0}" is switched {1}.'.format(changed.name,
                                                     "on" if changed.enabled else "off"))

    def delete(self):
        current = self._selected()
        if current is None:
            return
        if current.builtin:
            self.message.configure(
                text="Built-in lead types can be switched off, but not deleted.")
            return
        if not self._confirm("Delete lead type",
                             'Delete "{0}"? This can\'t be undone.'.format(current.name)):
            return
        self._commit([t for t in self.types if t.id != current.id],
                     'Deleted "{0}".'.format(current.name))

    def reset(self):
        if not self._confirm("Reset lead types",
                             "Put every built-in lead type back the way it shipped? "
                             "Your own lead types are kept."):
            return
        self._commit(lead_types.reset_builtins(self.types),
                     "Built-in lead types are back to how they shipped. Your own "
                     "types were kept.")


class LeadTypeEditor(tk.Toplevel):
    """Add or edit one lead type. Problems are listed in the dialog rather than
    in a popup, and nothing is saved until they are fixed."""

    def __init__(self, master, lead_type, all_types, on_save):
        super().__init__(master)
        self.title("Edit lead type" if lead_type else "Add a lead type")
        self.configure(bg=T.SURFACE)
        self.transient(master.winfo_toplevel())
        self._original = lead_type
        self._others = [t for t in all_types if lead_type is None or t.id != lead_type.id]
        self._taken = {t.id for t in all_types}
        self._on_save = on_save
        base = lead_type or lead_types.LeadType(id="", name="", group="Custom")
        # Checks this version doesn't know are kept, so a newer file survives
        # being edited here.
        self._unknown_checks = [c for c in base.checks if c not in lead_types.CHECKS]

        form = tk.Frame(self, bg=T.SURFACE)
        form.pack(fill=tk.BOTH, expand=True, padx=18, pady=16)
        self.name_var = tk.StringVar(value=base.name)
        self.group_var = tk.StringVar(value=base.group)
        self.description_var = tk.StringVar(value=base.description)
        self.match_var = tk.StringVar(value=base.match)
        self.mentions_var = tk.StringVar(value=", ".join(base.mentions))
        self.not_mentions_var = tk.StringVar(value=", ".join(base.not_mentions))
        self.check_vars = {check_id: tk.BooleanVar(value=check_id in base.checks)
                           for check_id in lead_types.CHECKS}

        row = 0
        for label, widget in (
                ("Name", ttk.Entry(form, textvariable=self.name_var, width=40)),
                ("Group", ttk.Combobox(form, textvariable=self.group_var, width=38,
                                       values=lead_types.group_names(all_types))),
                ("Description", ttk.Entry(form, textvariable=self.description_var,
                                          width=60))):
            tk.Label(form, text=label, font=T.F_BODY, bg=T.SURFACE, fg=T.INK,
                     anchor="w").grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            widget.grid(row=row, column=1, sticky="w", pady=4)
            row += 1

        tk.Label(form, text="Match when", font=T.F_BODY, bg=T.SURFACE, fg=T.INK,
                 anchor="w").grid(row=row, column=0, sticky="w", pady=(8, 4))
        match_row = tk.Frame(form, bg=T.SURFACE)
        match_row.grid(row=row, column=1, sticky="w", pady=(8, 4))
        ttk.Radiobutton(match_row, text="any of these is true", value="any",
                        variable=self.match_var).pack(side=tk.LEFT)
        ttk.Radiobutton(match_row, text="all of these are true", value="all",
                        variable=self.match_var).pack(side=tk.LEFT, padx=12)
        row += 1

        checks = tk.Frame(form, bg=T.SURFACE)
        checks.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 6))
        for index, check in enumerate(lead_types.CHECKS.values()):
            ttk.Checkbutton(checks, text=check.label,
                            variable=self.check_vars[check.id]).grid(
                row=index // 2, column=index % 2, sticky="w", padx=(0, 18))
        row += 1

        for label, variable in (("Homepage mentions any of", self.mentions_var),
                                ("Homepage doesn't mention any of", self.not_mentions_var)):
            tk.Label(form, text=label, font=T.F_BODY, bg=T.SURFACE, fg=T.INK,
                     anchor="w").grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=variable, width=60).grid(row=row, column=1,
                                                                  sticky="w", pady=4)
            row += 1
        tk.Label(form, text="Comma-separated phrases, e.g. telehealth, book online",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").grid(
            row=row, column=1, sticky="w")
        row += 1

        self.problems = tk.Label(form, text="", font=T.F_SMALL, bg=T.SURFACE, fg=T.BAD,
                                 anchor="w", justify="left")
        self.problems.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))

        actions = tk.Frame(self, bg=T.SURFACE)
        actions.pack(fill=tk.X, padx=18, pady=(0, 16))
        ttk.Button(actions, text="Save", style="Step.TButton",
                   command=self.save).pack(side=tk.RIGHT)
        ttk.Button(actions, text="Cancel", style="Ghost.TButton",
                   command=self.destroy).pack(side=tk.RIGHT, padx=6)
        self.bind("<Escape>", lambda _e: self.destroy())

    def candidate(self):
        name = self.name_var.get().strip()
        original = self._original
        return lead_types.LeadType(
            id=original.id if original else lead_types.new_id(name or "lead type", self._taken),
            name=name,
            group=self.group_var.get().strip() or "Custom",
            description=self.description_var.get().strip(),
            enabled=original.enabled if original else True,
            builtin=bool(original and original.builtin),
            match=self.match_var.get() if self.match_var.get() in ("any", "all") else "any",
            checks=[c for c in lead_types.CHECKS if self.check_vars[c].get()]
            + self._unknown_checks,
            mentions=lead_types.parse_list(self.mentions_var.get()),
            not_mentions=lead_types.parse_list(self.not_mentions_var.get()))

    def save(self):
        candidate = self.candidate()
        problems = lead_types.validate(candidate, self._others)
        if problems:
            self.problems.configure(text="\n".join(problems))
            return False
        self.destroy()
        self._on_save(candidate)
        return True
