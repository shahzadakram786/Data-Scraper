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
            "enrich": "Run All Three Passes",
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

    def test_then_medicare_data(self, gui):
        gui.store.add_leads_batch([
            lead(**{"Email": "jane@practice.example", "Email Status": "valid"})])
        gui.refresh()
        assert gui._next_action == "enrich:cms"

    def test_then_finishing_the_sending_setup(self, gui):
        gui.store.add_leads_batch([
            lead(**{"Email": "jane@practice.example", "Email Status": "valid",
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
        gui.audience_var.set("Owner-operated small practices (ICP)")
        assert gui._campaign_filters() == {"icp_only": True, "owners_only": False}
        gui.audience_var.set("Owners and founders")
        assert gui._campaign_filters() == {"icp_only": False, "owners_only": True}
        gui.audience_var.set("Everyone with a usable address")
        assert gui._campaign_filters() == {"icp_only": False, "owners_only": False}

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
