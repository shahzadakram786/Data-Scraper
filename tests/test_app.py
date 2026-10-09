"""The window builds, and its wiring holds.

app.py is the largest file here and nothing else tests it. These do not check
how it looks — they check that every screen constructs, that the panels read
from the database, that the guidance exists for each screen, and that the two
gates a user can hurt themselves on are actually in front of them: the "what do
I do next" recommendation and the compliance block on sending.

A window that fails to build is a total outage, and it is the one failure no
other test here would catch.

Skipped automatically where there is no display.
"""

import time
import threading

import pytest

tkinter = pytest.importorskip("tkinter")


@pytest.fixture(scope="session")
def tk_root():
    """The one Tk root for the whole session, as the real app has.

    Tk must be started exactly once per process. Creating a root, destroying it
    and creating another corrupts Tcl's interpreter state on Windows — the
    second one cannot even find init.tcl. That rules out both a root per test
    and the obvious "can I open a display?" probe, since the probe itself would
    be the first create/destroy cycle. So this fixture is the probe: if Tk will
    not start, every test in the file skips.
    """
    try:
        root = tkinter.Tk()
    except Exception as e:                     # no display, no Tcl, headless CI
        pytest.skip("Tk is unavailable here: {0}".format(e))
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def gui(tmp_path, monkeypatch, tk_root):
    """A fully built window pointed at a throwaway database.

    config is redirected first, so the app can never touch the real settings
    file or lead database while under test.
    """
    import config

    settings = dict(config.DEFAULTS,
                    db_path=str(tmp_path / "app.db"),
                    excel_path=str(tmp_path / "app.xlsx"))
    monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "load", lambda path=None: dict(settings))
    monkeypatch.setattr(config, "save", lambda values, path=None: values)

    import app as app_module

    root = tkinter.Toplevel(tk_root)
    root.withdraw()
    window = app_module.LeadGenApp(root)

    # _startup is scheduled with after(50), so give it a moment rather than
    # asserting against a half-initialised window.
    deadline = time.time() + 5
    while window.store is None and time.time() < deadline:
        root.update()
        time.sleep(0.02)

    yield window

    # Detach the log handlers before tearing the window down. The pane handler
    # re-arms itself on a timer, and one left attached to a destroyed widget is
    # exactly the leak the guard in TextHandler._drain exists for.
    for handler in list(window.logger.handlers):
        window.logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    root.destroy()


def lead(**overrides):
    base = {"NPI Number": "1234567890", "Provider First Name": "Jane",
            "Provider Last Name": "Smith", "State": "TX",
            "Source": "Psychology Today"}
    base.update(overrides)
    return base


class TestWindowBuilds:

    def test_every_screen_constructs(self, gui):
        import app as app_module
        assert set(gui.screens) == {key for key, _l, _n in app_module.SCREENS}

    def test_the_rail_follows_the_order_of_the_work(self, gui):
        import app as app_module
        assert [key for key, _l, _n in app_module.SCREENS] == [
            "home", "collect", "enrich", "outreach", "results", "settings"]

    def test_the_database_is_opened_on_startup(self, gui):
        assert gui.store is not None

    def test_navigating_changes_the_visible_screen(self, gui):
        for key in ("collect", "enrich", "outreach", "results", "settings", "home"):
            gui.show(key)
            gui.root.update()
            assert gui.screen == key
            assert gui.screens[key].winfo_manager(), key + " should be packed"

    def test_the_title_bar_describes_the_screen(self, gui):
        gui.show("enrich")
        gui.root.update()
        assert gui.screen_title.cget("text") == "Enrich"
        assert gui.screen_note.cget("text")

    def test_switching_source_swaps_the_visible_options(self, gui):
        gui.show("collect")
        gui.source_var.set("npi")
        gui._sync_source()
        gui.root.update()
        assert gui.npi_options.winfo_manager()
        assert not gui.pt_options.winfo_manager()

    def test_the_primary_button_describes_the_current_screen(self, gui):
        expected = {
            "home": "Go to Next Step",
            "collect": "Collect from Psychology Today",
            "enrich": "Run All Five Passes",
            "outreach": "Send Campaign",
            "results": "Export Spreadsheet",
            "settings": "Save Settings",
        }
        for key, label in expected.items():
            gui.show(key)
            gui.root.update()
            assert gui.primary_btn.cget("text") == label, key

    def test_pasted_npis_are_counted_as_you_type(self, gui):
        gui.show("collect")
        gui.npi_paste.insert("1.0", "NPI 1234567890\nNPI 1987654321")
        gui._count_pasted_npis()
        assert "2 NPI number(s)" in gui.paste_count.cget("text")


class TestGuidance:
    """The knowledge that used to live only in a markdown file now has to be
    reachable from the screen it applies to."""

    def test_every_screen_has_guidance(self, gui):
        import app as app_module
        import ui_help
        for key, _label, _note in app_module.SCREENS:
            heading, blocks = ui_help.for_screen(key)
            assert heading and blocks, key + " has no guidance"

    def test_the_guide_panel_opens_and_closes(self, gui):
        assert not gui.help_open
        gui.toggle_help()
        gui.root.update()
        assert gui.help_open
        assert gui.help_panel.winfo_manager()
        assert gui.help_btn.cget("text") == "Hide guide"
        gui.toggle_help()
        gui.root.update()
        assert not gui.help_open

    def test_the_panel_shows_the_current_screens_guidance(self, gui):
        import ui_help
        gui.show("outreach")
        gui.toggle_help()
        gui.root.update()
        assert gui.help_title.cget("text") == ui_help.for_screen("outreach")[0]
        assert gui.help_body.winfo_children(), "the panel rendered nothing"

    def test_the_panel_follows_you_between_screens(self, gui):
        import ui_help
        gui.toggle_help()
        gui.show("enrich")
        gui.root.update()
        assert gui.help_title.cget("text") == ui_help.for_screen("enrich")[0]


class TestNextStep:
    """The single answer to "I don't know what to do". It has to name a stage
    that is genuinely unfinished, in the order the work happens."""

    def test_an_empty_database_says_collect(self, gui):
        gui.refresh()
        assert gui._next_action == "collect"
        assert "first leads" in gui.next_title.cget("text").lower()

    def test_leads_without_an_email_lead_to_discovery(self, gui):
        gui.store.add_leads_batch([lead(Phone="(512) 471-3300")])
        gui.refresh()
        assert gui._next_action == "enrich:find"
        assert "1 missing email" in gui.next_title.cget("text")

    def test_unverified_addresses_come_next(self, gui):
        gui.store.add_leads_batch([lead(Email="jane@practice.example")])
        gui.refresh()
        assert gui._next_action == "enrich:verify"

    def test_then_npi_records(self, gui):
        gui.store.add_leads_batch([
            lead(**{"Email": "jane@practice.example", "Email Status": "valid"})])
        gui.refresh()
        assert gui._next_action == "enrich:npi"
        assert "1 to match" in gui.step_npi.status.cget("text")

    def test_then_medicare_data(self, gui):
        gui.store.add_leads_batch([
            lead(**{"Email": "jane@practice.example", "Email Status": "valid",
                    "NPI Checked": "2026-09-15"})])
        gui.refresh()
        assert gui._next_action == "enrich:cms"

    def test_then_finishing_the_sending_setup(self, gui):
        gui.store.add_leads_batch([
            lead(**{"Email": "jane@practice.example", "Email Status": "valid",
                    "NPI Checked": "2026-09-15",
                    "Phone Verified": "No federal enrollment record"})])
        gui.refresh()
        assert gui._next_action == "settings"
        assert "sending" in gui.next_title.cget("text").lower()

    def test_the_rail_shows_the_same_recommendation(self, gui):
        gui.refresh()
        assert gui.nav_next.cget("text") == gui.next_title.cget("text")


class TestPanelsReadFromTheDatabase:

    def _seed(self, gui):
        gui.store.add_leads_batch([
            lead(**{"Email": "a@b.example", "Email Status": "valid",
                    "Phone": "(512) 471-3399",
                    "Phone Source": "Provider Website (personal/direct)",
                    "Role": "Owner", "Annual Billing": "480000",
                    "Practice Size": "6"}),
            lead(**{"NPI Number": "2222222222", "Provider Last Name": "Jones",
                    "Phone": "(214) 692-0177", "State": "OH"}),
        ])
        gui.refresh()

    def test_metrics_reflect_the_data(self, gui):
        self._seed(gui)
        assert gui.metrics["total"].var.get() == "2"
        assert gui.metrics["with_email"].var.get() == "1"
        assert gui.metrics["verified_email"].var.get() == "1"
        assert gui.metrics["with_phone"].var.get() == "2"
        assert gui.metrics["with_billing"].var.get() == "1"

    def test_the_funnel_counts_each_stage(self, gui):
        self._seed(gui)
        counts = {key: widgets[1].cget("text")
                  for key, widgets in gui.funnel_rows.items()}
        assert counts["total"] == "2"
        assert counts["with_email"] == "1"
        assert counts["contacted"] == "0"

    def test_the_tab_breakdown_is_populated(self, gui):
        self._seed(gui)
        assert len(gui.tree.get_children()) == 2

    def test_the_enrich_steps_show_what_is_outstanding(self, gui):
        self._seed(gui)
        assert "1 waiting" in gui.step_find.status.cget("text")
        assert "Everything checked" in gui.step_verify.status.cget("text")

    def test_live_counts_update_during_a_run(self, gui):
        gui.bump_counts("Texas - Psychology Today", leads=12, emails=7)
        assert gui.metrics["total"].var.get() == "12"
        assert gui.metrics["with_email"].var.get() == "7"

    def test_progress_is_reported_as_a_fraction(self, gui):
        gui.set_progress(30, 120)
        assert gui.progress_label.cget("text") == "30 / 120"
        assert 24 < gui.progress_var.get() < 26


class TestSendGate:
    """The compliance gate must sit in front of the send button, not behind it —
    an unlawful email cannot be recalled."""

    def test_an_unconfigured_app_reports_what_is_missing(self, gui):
        gui._refresh_compliance()
        text = gui.compliance_banner.cget("text")
        assert "Before you can send" in text
        assert "postal address" in text.lower()

    def test_a_configured_app_reports_ready(self, gui):
        gui.settings.update({
            "from_name": "Shahzad Akram", "from_email": "s@example.com",
            "postal_address": "12 Example Street",
            "unsubscribe_email": "u@example.com",
            "smtp_host": "smtp.example.com", "smtp_user": "s@example.com"})
        gui._refresh_compliance()
        assert "Ready to send" in gui.compliance_banner.cget("text")

    def test_the_audience_selector_maps_to_real_filters(self, gui):
        def audience():
            filters = gui._campaign_filters()
            assert filters.pop("lead_types") is not None, "the lead-type choice rides along"
            return filters
        gui.audience_var.set("Owner-operated small practices (ICP)")
        assert audience() == {"icp_only": True, "owners_only": False}
        gui.audience_var.set("Owners and founders")
        assert audience() == {"icp_only": False, "owners_only": True}
        gui.audience_var.set("Everyone with a usable address")
        assert audience() == {"icp_only": False, "owners_only": False}

    def test_the_default_template_only_uses_real_merge_fields(self, gui):
        """A typo here would go out to every recipient as raw {{braces}}."""
        import outreach
        assert outreach.unresolved_fields(gui.DEFAULT_SUBJECT) == []
        assert outreach.unresolved_fields(gui.DEFAULT_BODY) == []


class TestChrome:

    @pytest.mark.parametrize("tone", ["idle", "busy", "ok", "warn", "error"])
    def test_every_banner_tone_renders(self, gui, tone):
        gui.banner.set(tone, tone.title(), "detail")
        assert gui.banner.title.cget("text") == tone.title()

    def test_a_banner_with_no_detail_hides_the_detail_line(self, gui):
        gui.banner.set("ok", "Done")
        assert gui.banner.detail.cget("text") == ""

    def test_the_activity_log_collapses(self, gui):
        assert not gui.log_open, "the log starts out of the way"
        gui.toggle_log()
        gui.root.update()
        assert gui.log_open and gui.log_frame.winfo_manager()
        gui.toggle_log()
        gui.root.update()
        assert not gui.log_open


def _wait_for_worker(gui, timeout=15):
    """Let a run's worker finish, pumping Tk so its after() callbacks land."""
    deadline = time.time() + timeout
    while gui.worker is not None and gui.worker.is_alive() and time.time() < deadline:
        gui.root.update()
        time.sleep(0.02)
    for _ in range(25):
        gui.root.update()
        time.sleep(0.01)


class _FakePipeline:
    """Records which thread each pass runs on. Nothing touches the network."""

    def __init__(self, added=0):
        self.verifier = object()
        self.added = added
        self.threads = []

    def _result(self):
        from pipeline import RunResult
        import storage
        self.threads.append(threading.current_thread())
        return RunResult(storage.RUN_OK, added=self.added, message="")

    def find_missing_emails(self):
        return self._result()

    def verify_emails(self, recheck=False):
        return self._result()

    def enrich_from_npi(self, recheck=False):
        return self._result()

    def enrich_from_cms(self, recheck=False):
        return self._result()

    def check_sites(self, recheck=False):
        return self._result()


class TestResponsiveness:
    """The window was slow for reasons that had nothing to do with the work:
    redrawing unchanged screens, and preparing runs in a way that also read
    widgets from a background thread."""

    @staticmethod
    def _count_stats_calls(gui, monkeypatch):
        calls = []
        real = gui.store.get_stats

        def counting():
            calls.append(1)
            return real()

        monkeypatch.setattr(gui.store, "get_stats", counting)
        return calls

    def test_switching_screens_with_nothing_new_does_not_redraw(self, gui, monkeypatch):
        gui.refresh(force=True)
        calls = self._count_stats_calls(gui, monkeypatch)
        for key in ("results", "enrich", "outreach", "home"):
            gui.show(key)
            gui.root.update()
        assert calls == [], "an unchanged database was redrawn"

    def test_new_data_is_still_redrawn(self, gui, monkeypatch):
        gui.refresh(force=True)
        calls = self._count_stats_calls(gui, monkeypatch)
        gui.store.add_leads_batch([lead(Phone="(512) 471-3300")])
        gui.show("results")
        gui.root.update()
        assert len(calls) == 1
        assert gui.metrics["total"].var.get() == "1"

    def test_saving_settings_redraws_on_the_next_screen(self, gui, monkeypatch):
        """The next-step box depends on settings, not only on data."""
        gui.refresh(force=True)
        calls = self._count_stats_calls(gui, monkeypatch)
        gui.save_settings()
        gui.show("home")
        assert len(calls) == 1

    def test_a_run_is_prepared_on_the_window_thread_and_worked_elsewhere(
            self, gui, monkeypatch):
        """Preparing a run reads settings and widgets. Tk only allows that from
        its own thread; doing it from the worker is the kind of bug that shows
        up as an occasional frozen window rather than an error."""
        built_on = []
        fake = _FakePipeline()

        def build():
            built_on.append(threading.current_thread())
            return fake

        monkeypatch.setattr(gui, "_build_pipeline", build)
        gui.start_enrichment("find")
        _wait_for_worker(gui)

        assert built_on == [threading.main_thread()]
        assert fake.threads and fake.threads[0] is not threading.main_thread()

    def test_running_all_passes_writes_the_spreadsheet_once(
            self, gui, monkeypatch):
        """It used to export after each pass — the same workbook once per pass,
        which was most of the wait at the end."""
        fake = _FakePipeline(added=2)
        monkeypatch.setattr(gui, "_build_pipeline", lambda: fake)
        exports = []
        monkeypatch.setattr(gui, "_export", lambda options: exports.append(options) or 0)
        gui.settings["auto_export"] = True

        gui.start_enrichment("all")
        _wait_for_worker(gui)

        assert len(fake.threads) == 5
        assert len(exports) == 1

    def test_a_run_that_crashes_says_so(self, gui, monkeypatch):
        """The failure banner read the exception after Python had cleared it,
        so a crashed run raised a second error and the window stayed silent."""
        messages = []
        monkeypatch.setattr(gui.banner, "set",
                            lambda tone, title, detail="": messages.append((tone, detail)))

        def make_job():
            def job():
                raise RuntimeError("registry exploded")
            return job

        gui._spawn(make_job, "Working…")
        _wait_for_worker(gui)

        assert ("error", "registry exploded") in messages

    def test_export_choices_are_read_before_the_worker_starts(self, gui, monkeypatch):
        seen = []

        def export(options):
            seen.append((threading.current_thread(), options))
            return 0

        monkeypatch.setattr(gui, "_export", export)
        gui.call_list_var.set("250")
        gui.icp_only_var.set(True)

        gui.start_export()
        _wait_for_worker(gui)

        thread, options = seen[0]
        assert thread is not threading.main_thread()
        assert options["call_list"] == 250
        assert options["icp_only"] is True


class _AliveWorker:
    """Stands in for a run in progress."""

    def is_alive(self):
        return True


class TestUpdates:
    """The update button is the one piece of UI that must stay out of the way
    until it matters, and must never interrupt a run."""

    @staticmethod
    def _release(version="9.0.0"):
        import updater
        return updater.UpdateInfo(version, "v" + version, notes="Faster exports.")

    def test_there_is_no_update_button_until_an_update_exists(self, gui):
        assert gui.available_update is None
        assert not gui.update_btn.winfo_manager()

    def test_a_found_update_shows_the_button_with_its_version(self, gui):
        gui._on_update_checked(self._release(), None, False)
        gui.root.update()
        assert gui.available_update.version == "9.0.0"
        assert gui.update_btn.winfo_manager()
        assert "9.0.0" in gui.update_btn.cget("text")

    def test_checking_by_hand_says_when_you_are_up_to_date(self, gui):
        from version import __version__
        gui._on_update_checked(None, None, True)
        assert __version__ in gui.banner.title.cget("text") + gui.banner.detail.cget("text")

    def test_a_failed_background_check_stays_quiet(self, gui):
        """Being offline for a moment is not worth an alarm on screen."""
        gui.banner.set("idle", "Ready.")
        gui._on_update_checked(None, "Could not reach GitHub.", False)
        assert gui.banner.title.cget("text") == "Ready."

    def test_a_failed_check_you_asked_for_is_explained(self, gui):
        gui._on_update_checked(None, "Could not reach GitHub.", True)
        assert "Could not reach GitHub." in gui.banner.detail.cget("text")

    def test_an_update_waits_for_a_running_job(self, gui, monkeypatch):
        """Restarting in the middle of a collection or a send would lose work."""
        import tkinter.messagebox as messagebox
        monkeypatch.setattr(messagebox, "askyesno",
                            lambda *a, **k: pytest.fail("must not ask mid-run"))
        release = self._release()
        gui.worker = _AliveWorker()
        try:
            gui.apply_update(release)
        finally:
            gui.worker = None
        assert gui._update_after_run is release
        assert "finish" in (gui.banner.title.cget("text")
                            + gui.banner.detail.cget("text")).lower()

    def test_the_window_opened_by_an_update_says_it_worked(self, gui):
        from version import __version__
        gui._announce_updated("1.9.0")
        text = gui.banner.title.cget("text") + gui.banner.detail.cget("text")
        assert "1.9.0" in text and __version__ in text

    def test_turning_off_automatic_checks_is_saved(self, gui):
        gui.check_updates_var.set(False)
        gui.save_settings()
        assert gui.settings["check_updates"] is False

    def test_the_selfcheck_starts_and_exits_without_opening_a_window(self):
        """What the .exe updater runs on a new build before trusting it."""
        import os
        import subprocess
        import sys
        project = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run([sys.executable, "app.py", "--selfcheck"],
                                cwd=project, capture_output=True, text=True,
                                timeout=180)
        assert result.returncode == 0, result.stderr


def pump_until(gui, condition, timeout=5):
    """Let the window process posted work until condition() holds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        gui.root.update()
        if condition():
            return True
        time.sleep(0.02)
    return False


class TestLeadTypes:
    """The selector: one selection, shown on Collect, Results and Outreach,
    and carried into every export and campaign."""

    def test_three_bars_all_start_on_every_lead(self, gui):
        assert len(gui.lead_type_bars) == 3
        for bar in gui.lead_type_bars:
            assert "every lead" in bar.hint.cget("text").lower()

    def test_choosing_updates_every_bar_and_the_settings(self, gui):
        gui._set_lead_type_selection(["seo"])
        gui.root.update()
        assert gui.settings["lead_types_selected"] == ["seo"]
        for bar in gui.lead_type_bars:
            assert any("SEO" in text for text in bar.pill_texts())

    def test_removing_a_pill_drops_that_type(self, gui):
        gui._set_lead_type_selection(["seo", "bookkeeping"])
        gui._remove_lead_type("seo")
        assert gui.settings["lead_types_selected"] == ["bookkeeping"]

    def test_npi_collection_warns_that_websites_are_needed(self, gui):
        gui.show("collect")
        gui.source_var.set("npi")
        gui._set_lead_type_selection(["seo"])
        gui._sync_source()
        assert "Check websites" in gui.collect_lead_bar.hint.cget("text")

    def test_the_picker_returns_what_was_ticked(self, gui):
        import ui_lead_types as LT
        chosen = []
        picker = LT.LeadTypePicker(gui.root, gui.lead_type_defs, ["seo"], on_done=chosen.append)
        picker._vars["bookkeeping"].set(True)
        picker.use()
        assert chosen == [["seo", "bookkeeping"]]

    def test_the_picker_filter(self, gui):
        import ui_lead_types as LT
        picker = LT.LeadTypePicker(gui.root, gui.lead_type_defs, [], on_done=lambda ids: None)
        picker.filter_var.set("book")
        gui.root.update()
        assert set(picker.visible_ids()) == {"bookkeeping", "online_booking"}
        picker.clear()
        assert picker.selected_ids() == []
        picker.destroy()

    def test_exports_and_campaigns_carry_the_selection(self, gui):
        gui._set_lead_type_selection(["seo"])
        assert gui._export_options()["lead_types"].label() == "SEO"
        assert gui._campaign_filters()["lead_types"].label() == "SEO"

    def test_results_count_lead_types_off_the_tk_thread(self, gui):
        import site_audit
        gui.store.add_leads_batch([lead(Phone="(512) 471-3300", **{
            "Site Facts": site_audit.dump_facts(
                site_audit.no_site_facts(site_audit.NO_SITE_LISTED))})])
        gui._set_lead_type_selection(["website_development"])
        gui._recount_lead_types(force=True)
        assert pump_until(gui, lambda: gui.lead_type_counts.get("website_development") == 1)
        assert gui.metrics["not_checked"].var.get() == "0"

    def test_the_counting_thread_holds_no_reference_to_the_window(self, gui, monkeypatch):
        """If the thread kept the window alive, the window could be destroyed
        on that thread when it finished — outside Tk's main loop, which hangs
        Tk (it froze the test run for minutes before this was fixed)."""
        import gc
        import threading as threading_module
        started = []
        real_thread = threading_module.Thread

        class RecordingThread(real_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                started.append(self)

        import app as app_module
        monkeypatch.setattr(app_module.threading, "Thread", RecordingThread)
        gui._recount_lead_types(force=True)
        assert started, "the count runs on a thread"
        target = started[-1]._target
        referenced = gc.get_referents(*[cell.cell_contents
                                        for cell in (target.__closure__ or ())])
        closure_values = [cell.cell_contents for cell in (target.__closure__ or ())]
        assert gui not in closure_values and gui not in referenced
        assert not any(getattr(value, "__self__", None) is gui for value in closure_values)
        assert pump_until(gui, lambda: "website_development" in gui.lead_type_counts)

    def test_enrich_has_a_check_websites_step(self, gui):
        gui.store.add_leads_batch([lead(Phone="(512) 471-3300", Website="https://a.example")])
        gui.refresh(force=True)
        assert gui.step_sites.status.cget("text") == "1 to check"


class TestLeadTypeSettings:

    def card(self, gui):
        gui.show("settings")
        gui.root.update()
        card = gui.lead_types_card
        card._confirm = lambda *_a, **_k: True
        return card

    def test_every_lead_type_is_listed(self, gui):
        card = self.card(gui)
        assert len(card.tree.get_children()) == len(gui.lead_type_defs) == 20

    def test_switching_a_type_off_is_saved(self, gui):
        import config
        import lead_types
        card = self.card(gui)
        card.tree.selection_set("seo")
        card.toggle()
        saved = lead_types.load(config.lead_types_path()).types
        assert next(t for t in saved if t.id == "seo").enabled is False
        assert next(t for t in gui.lead_type_defs if t.id == "seo").enabled is False

    def test_built_ins_cannot_be_deleted(self, gui):
        card = self.card(gui)
        card.tree.selection_set("seo")
        card.delete()
        assert "not deleted" in card.message.cget("text")
        assert len(gui.lead_type_defs) == 20

    def test_adding_through_the_editor(self, gui):
        import ui_lead_types as LT
        card = self.card(gui)
        editor = LT.LeadTypeEditor(card, None, card.types, on_save=card._added)
        editor.name_var.set("Telehealth")
        editor.mentions_var.set("telehealth, video sessions")
        assert editor.save()
        added = gui.lead_type_defs[-1]
        assert (added.id, added.mentions) == ("telehealth", ["telehealth", "video sessions"])

    def test_the_editor_lists_problems_instead_of_saving(self, gui):
        import ui_lead_types as LT
        card = self.card(gui)
        editor = LT.LeadTypeEditor(card, None, card.types, on_save=card._added)
        editor.name_var.set("SEO")
        editor.check_vars["no_h1"].set(True)
        assert editor.save() is False
        assert "already called" in editor.problems.cget("text")
        editor.destroy()

    def test_deleting_a_selected_custom_type_drops_it_from_the_selection(self, gui):
        import lead_types
        card = self.card(gui)
        card._added(lead_types.LeadType(id="telehealth", name="Telehealth",
                                        mentions=["telehealth"]))
        gui._set_lead_type_selection(["telehealth", "seo"])
        card.tree.selection_set("telehealth")
        card.delete()
        assert gui.settings["lead_types_selected"] == ["seo"]

    def test_a_broken_file_is_explained(self, gui):
        import config
        with open(config.lead_types_path(), "w", encoding="utf-8") as handle:
            handle.write("{broken")
        gui._load_lead_types()
        gui.lead_types_card.show(gui.lead_type_defs, problem=gui.lead_types_problem)
        gui.root.update()
        assert ".bad" in gui.lead_types_card.note.cget("text")
        assert gui.lead_types_card.note.winfo_manager()
