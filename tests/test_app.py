"""The window builds, and its wiring holds.

app.py is 1,600 lines that nothing else tests. These do not try to test the
look of it — they check that every tab constructs, that the pieces are wired to
the store, and that the safety gate in front of sending is actually in front of
it. A window that fails to build is a total outage, and it is the one failure
no other test here would catch.

Skipped automatically where there is no display, so a headless CI run stays
green rather than erroring on Tk.
"""

import os
import sys
import time
import tempfile

import pytest

tkinter = pytest.importorskip("tkinter")


@pytest.fixture(scope="session")
def tk_root():
    """The one Tk root for the whole session, as the real app has.

    Tk must be started exactly once per process. Creating a root, destroying
    it, and creating another corrupts Tcl's interpreter state on Windows — the
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

    config is redirected first so the app can never touch the user's real
    settings file or lead database while under test.
    """
    import config

    settings = dict(config.DEFAULTS,
                    db_path=str(tmp_path / "app.db"),
                    excel_path=str(tmp_path / "app.xlsx"))
    monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setattr(config, "load", lambda path=None: dict(settings))
    monkeypatch.setattr(config, "save", lambda values, path=None: values)

    import app as app_module

    # Each test gets its own window inside the shared root.
    root = tkinter.Toplevel(tk_root)
    root.withdraw()
    window = app_module.LeadGenApp(root)

    # _startup is scheduled with after(50), so give it a moment to actually run
    # rather than asserting against a half-initialised window.
    deadline = time.time() + 5
    while window.store is None and time.time() < deadline:
        root.update()
        time.sleep(0.02)

    yield window

    # Detach the log handlers before tearing the window down. The pane handler
    # re-arms itself on a timer, and a handler left attached to a destroyed
    # widget is exactly the leak the guard in TextHandler._drain exists for —
    # closing them here keeps one test's window out of the next one's.
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

    def test_every_tab_is_present_and_in_work_order(self, gui):
        labels = [gui.notebook.tab(i, "text").strip()
                  for i in range(len(gui.notebook.tabs()))]
        assert labels == ["1. Collect", "2. Enrich", "3. Outreach",
                          "4. Results", "Settings"]

    def test_the_database_is_opened_on_startup(self, gui):
        assert gui.store is not None

    def test_switching_source_swaps_the_visible_options(self, gui):
        gui.source_var.set("npi")
        gui._sync_source()
        assert gui.npi_options.winfo_ismapped() or gui.npi_options.winfo_manager()
        assert not gui.pt_options.winfo_ismapped()

    def test_the_primary_button_describes_the_current_tab(self, gui):
        expected = {0: "Collect from Psychology Today",
                    1: "Find Missing Emails",
                    2: "Send Campaign",
                    3: "Export Spreadsheet",
                    4: "Save Settings"}
        for index, label in expected.items():
            gui.notebook.select(index)
            gui.root.update()
            gui._sync_primary()
            assert gui.primary_btn.cget("text") == label, "tab " + str(index)


class TestResultsWiring:

    def test_metrics_read_from_the_database(self, gui):
        gui.store.add_leads_batch([
            lead(**{"Email": "a@b.example", "Email Status": "valid",
                    "Phone": "(512) 471-3399",
                    "Phone Source": "Provider Website (personal/direct)",
                    "Role": "Owner", "Annual Billing": "480000",
                    "Practice Size": "6"}),
            lead(**{"NPI Number": "2222222222", "Provider Last Name": "Jones",
                    "Phone": "(214) 692-0177"}),
        ])
        gui.refresh_results()

        values = {key: var.get() for key, var in gui.metric_vars.items()}
        assert values["total"] == "2"
        assert values["with_email"] == "1"
        assert values["verified_email"] == "1"
        assert values["with_phone"] == "2"
        assert values["with_billing"] == "1"
        assert float(values["avg_score"]) > 0

    def test_the_tab_breakdown_is_populated(self, gui):
        gui.store.add_leads_batch([lead(Phone="(512) 471-3300")])
        gui.refresh_results()
        assert len(gui.tree.get_children()) == 1

    def test_pending_work_counts_are_shown(self, gui):
        gui.store.add_leads_batch([
            lead(Phone="(512) 471-3300"),
            lead(**{"NPI Number": "2222222222", "Email": "a@b.example"}),
        ])
        gui.refresh_results()
        assert "1 leads have no email" in gui.missing_label.cget("text")
        assert "1 addresses have not been checked" in gui.unverified_label.cget("text")

    def test_live_counts_update_during_a_run(self, gui):
        gui.bump_counts("Texas - Psychology Today", leads=12, emails=7)
        assert gui.metric_vars["total"].get() == "12"
        assert gui.metric_vars["with_email"].get() == "7"

    def test_progress_is_reported_as_a_fraction(self, gui):
        gui.set_progress(30, 120)
        assert gui.progress_label.cget("text") == "30 / 120"
        assert 24 < gui.progress_var.get() < 26


class TestSendGate:
    """The compliance gate has to sit in front of the send button, not behind
    it — an unlawful email cannot be recalled."""

    def test_an_unconfigured_app_reports_what_is_missing(self, gui):
        gui._refresh_compliance()
        text = gui.compliance_banner.cget("text")
        assert "Before you can send" in text
        assert "postal address" in text.lower()

    def test_a_configured_app_reports_ready(self, gui):
        gui.settings.update({
            "from_name": "Shahzad Akram", "from_email": "s@example.com",
            "postal_address": "12 Example Street", "unsubscribe_email": "u@example.com",
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


class TestBannerStates:

    @pytest.mark.parametrize("tone", ["idle", "busy", "ok", "warn", "error"])
    def test_every_tone_renders(self, gui, tone):
        gui.banner.set(tone, tone.title(), "detail")
        assert gui.banner.title.cget("text") == tone.title()

    def test_a_banner_with_no_detail_hides_the_detail_line(self, gui):
        gui.banner.set("ok", "Done")
        assert gui.banner.detail.cget("text") == ""
