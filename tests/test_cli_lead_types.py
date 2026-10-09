"""The command line's lead types: a filter you ask for by ID or name, and a
refusal — never a silent "everyone" — when it can't be honoured."""

import logging
from datetime import date

import pytest

import cli
import config
import lead_types
import site_audit
from conftest import healthy_site_facts
from storage import LeadStore

openpyxl = pytest.importorskip("openpyxl")
TODAY = date(2026, 10, 9)


class Recorder(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def logger():
    log = logging.getLogger("test.cli.lead_types")
    handler = Recorder()
    log.addHandler(handler)
    log.propagate = False
    log.messages = handler.messages
    yield log
    log.removeHandler(handler)


@pytest.fixture
def setup(tmp_path, quiet_logger):
    db = str(tmp_path / "leads.db")
    store = LeadStore(db, logger=quiet_logger)
    store.add_leads_batch([
        {"NPI Number": "1111111111", "State": "TX", "Source": "Psychology Today",
         "Phone": "(512) 471-3301",
         "Site Facts": site_audit.dump_facts(
             site_audit.no_site_facts(site_audit.NO_SITE_LISTED, today=TODAY))},
        {"NPI Number": "2222222222", "State": "TX", "Source": "Psychology Today",
         "Phone": "(512) 471-3302", "Facebook": "https://facebook.com/x",
         "Site Facts": site_audit.dump_facts(healthy_site_facts(TODAY))},
    ])
    settings = dict(config.DEFAULTS, db_path=db, auto_export=False,
                    excel_path=str(tmp_path / "out.xlsx"))
    return settings, tmp_path


def run(args, settings, logger, tmp_path):
    parsed = cli.build_parser().parse_args(["--config", str(tmp_path / "config.json")] + args)
    return parsed.func(parsed, settings, logger)


def exported_npis(path):
    workbook = openpyxl.load_workbook(path)
    try:
        sheet = workbook["Texas - Psychology Today"]
        return [row[0].value for row in sheet.iter_rows(min_row=2)]
    finally:
        workbook.close()


class TestExportFlag:

    @pytest.mark.parametrize("value", ["website_development", "Website Development"])
    def test_by_id_or_name(self, setup, logger, value):
        settings, tmp_path = setup
        out = str(tmp_path / "out.xlsx")
        code = run(["export", "--excel", out, "--call-list", "0", "--lead-types", value],
                   settings, logger, tmp_path)
        assert code == cli.EXIT_OK
        assert exported_npis(out) == ["1111111111"]

    def test_all_means_no_filter(self, setup, logger):
        settings, tmp_path = setup
        out = str(tmp_path / "out.xlsx")
        run(["export", "--excel", out, "--call-list", "0", "--lead-types", "all"],
            settings, logger, tmp_path)
        assert len(exported_npis(out)) == 2

    def test_no_flag_means_no_filter(self, setup, logger):
        settings, tmp_path = setup
        out = str(tmp_path / "out.xlsx")
        run(["export", "--excel", out, "--call-list", "0"], settings, logger, tmp_path)
        assert len(exported_npis(out)) == 2

    def test_unknown_lead_type_is_refused(self, setup, logger):
        settings, tmp_path = setup
        out = tmp_path / "out.xlsx"
        code = run(["export", "--excel", str(out), "--lead-types", "web design"],
                   settings, logger, tmp_path)
        assert code == cli.EXIT_FAILED
        assert not out.exists()
        assert any("web design" in m for m in logger.messages)
        assert any("website_development" in m for m in logger.messages), \
            "the error lists the valid ones"

    def test_switched_off_lead_type_is_refused(self, setup, logger):
        settings, tmp_path = setup
        types = lead_types.builtin_types()
        next(t for t in types if t.id == "seo").enabled = False
        lead_types.save(types, config.lead_types_path(str(tmp_path / "config.json")))
        code = run(["export", "--excel", str(tmp_path / "out.xlsx"), "--lead-types", "seo"],
                   settings, logger, tmp_path)
        assert code == cli.EXIT_FAILED
        assert any("Switched off" in m for m in logger.messages)


class TestCommands:

    def test_lead_types_lists_them(self, setup, logger, capsys):
        settings, tmp_path = setup
        assert run(["lead-types"], settings, logger, tmp_path) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert "Website & Online" in out and "website_development" in out

    def test_enrich_sites_is_wired(self):
        parsed = cli.build_parser().parse_args(["enrich-sites", "--recheck"])
        assert parsed.func is cli.cmd_enrich_sites and parsed.recheck

    def test_send_accepts_lead_types(self):
        parsed = cli.build_parser().parse_args(
            ["send", "--template", "pitch.txt", "--lead-types", "seo"])
        assert parsed.lead_types == "seo"
