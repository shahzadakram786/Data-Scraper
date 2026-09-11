"""Command line interface — the same pipelines the window drives, without it.

    python cli.py doctor
    python cli.py collect-pt --state Texas --owners-only
    python cli.py collect-npi --state TX --specialty Psychiatry
    python cli.py import-npi --file numbers.txt
    python cli.py find-emails
    python cli.py verify
    python cli.py export --min-score 40
    python cli.py stats

This is what makes the tool schedulable: a nightly `collect-pt` on one state
followed by `verify` and `export` needs nobody watching a window, and the exit
code says whether it worked (0 ok, 1 failed, 2 finished but collected nothing —
which is the signature of a source that has quietly broken).
"""

import os
import sys
import logging
import argparse

import config
import catalog
import outreach
import health
import storage
from storage import LeadStore, RUN_OK, RUN_EMPTY, RUN_STOPPED, RUN_FAILED
from pipeline import LeadPipeline, Reporter
# At module scope on purpose — see the note in app.py about importing modules
# from worker threads.
from npi_engine import NPIEngine
from email_engine import EmailEngine
from psychology_today_engine import PsychologyTodayEngine
from cms_engine import CMSEngine
from verification import EmailVerifier
from lead_manager import ExcelExporter, GoogleSheetsExporter


EXIT_OK, EXIT_FAILED, EXIT_EMPTY = 0, 1, 2


class ConsoleReporter(Reporter):
    """Prints progress as a single rewritten line so a long run does not
    scroll a terminal into uselessness."""

    def __init__(self, logger=None, quiet=False):
        super().__init__(logger)
        self.quiet = quiet
        self._last_percent = -1

    def progress(self, done, total):
        if self.quiet or not total:
            return
        percent = int(done / total * 100)
        if percent == self._last_percent:
            return
        self._last_percent = percent
        sys.stderr.write("\r  {0:3d}%  ({1}/{2})".format(percent, done, total))
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")


def build_logger(verbose=False):
    logger = logging.getLogger("LeadGen")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s",
                                           datefmt="%H:%M:%S"))
    logger.addHandler(handler)

    # Unattended runs are exactly the ones nobody is watching, so the file log
    # matters more here than in the window.
    try:
        from logging.handlers import RotatingFileHandler
        os.makedirs("logs", exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join("logs", "leadgen.log"), maxBytes=2_000_000,
            backupCount=3, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(name)s  %(message)s"))
        logger.addHandler(file_handler)
    except OSError:
        pass  # a read-only working directory must not stop a run

    logger.propagate = False
    return logger


def build_pipeline(settings, logger, reporter):
    """Wire up the store, engines and verifier from settings."""
    store = LeadStore(settings["db_path"], logger=logger)
    migrate_if_needed(store, settings, logger)

    verifier = None
    if settings.get("verify_emails"):
        verifier = EmailVerifier(
            logger=logger,
            probe_smtp=settings.get("smtp_probe", True),
            probe_sender=settings.get("probe_sender", ""))

    pipeline = LeadPipeline(
        store=store,
        npi_engine=NPIEngine(logger=logger),
        email_engine=EmailEngine(logger=logger),
        pt_engine=PsychologyTodayEngine(logger=logger,
                                        proxies=config.proxies_for(settings)),
        verifier=verifier,
        cms_engine=CMSEngine(logger=logger, proxies=config.proxies_for(settings)),
        reporter=reporter,
        settings=settings)
    pipeline.PT_WORKERS = int(settings.get("pt_workers", 5))
    pipeline.EMAIL_WORKERS = int(settings.get("email_workers", 5))
    pipeline.VERIFY_WORKERS = int(settings.get("verify_workers", 8))
    return pipeline


def migrate_if_needed(store, settings, logger):
    """Import an existing workbook the first time we run against an empty
    database, so leads collected before the database existed are not stranded
    in a file the app no longer treats as storage."""
    try:
        store.import_run_history(logger=logger)
    except Exception as e:
        logger.debug("Could not import run history: %s", e)

    if store.get_stats()["total"]:
        return
    excel_path = settings.get("excel_path")
    if excel_path and os.path.exists(excel_path):
        logger.info("First run against a new database — importing %s...", excel_path)
        store.import_from_excel(excel_path, logger=logger)


def export(store, settings, logger, min_score=None, statuses=None,
           call_list=100, icp_only=False):
    """Render the database to whichever destination is configured."""
    if settings.get("output") == "google":
        credentials = settings.get("google_credentials")
        if not credentials or not os.path.exists(credentials):
            logger.error("Google output is selected but the service-account "
                         "JSON path in config.json is missing or wrong.")
            return 0
        exporter = GoogleSheetsExporter(credentials,
                                        settings.get("google_spreadsheet"),
                                        logger=logger)
    else:
        exporter = ExcelExporter(settings.get("excel_path"), logger=logger)
        return exporter.export(store, min_score=min_score, email_statuses=statuses,
                               call_list=call_list, call_list_icp=icp_only)
    return exporter.export(store, min_score=min_score, email_statuses=statuses)


def status_exit_code(result):
    return {RUN_OK: EXIT_OK, RUN_EMPTY: EXIT_EMPTY,
            RUN_STOPPED: EXIT_OK, RUN_FAILED: EXIT_FAILED}.get(result.status, EXIT_FAILED)


def report_result(result, logger):
    logger.info("Run %s — %d lead(s) added. %s",
                result.status.upper(), result.added, result.message or "")
    if result.status == RUN_EMPTY:
        logger.warning(
            "Nothing was collected. If this state should have had providers, "
            "run `python cli.py doctor` — a source that has quietly broken "
            "looks exactly like this.")
    return status_exit_code(result)


# ------------------------------------------------------------------ commands

def cmd_doctor(args, settings, logger):
    reports = health.run_all(proxies=config.proxies_for(settings), logger=logger,
                             state=args.state or "Texas")
    print()
    print(health.format_report(reports))
    print()
    failed = [r["source"] for r in reports if not r["ok"]]
    if failed:
        logger.error("Not working: %s", ", ".join(failed))
        return EXIT_FAILED
    logger.info("All checks passed.")
    return EXIT_OK


def cmd_collect_pt(args, settings, logger):
    state_name, state_abbr = catalog.resolve_state(args.state)
    if not state_name:
        logger.error("Unknown state: %s", args.state)
        return EXIT_FAILED

    reporter = ConsoleReporter(logger, quiet=args.quiet)
    pipeline = build_pipeline(settings, logger, reporter)
    result = pipeline.collect_psychology_today(
        state_name, state_abbr,
        owners_only=args.owners_only,
        resume=not args.no_resume,
        max_pages=args.max_pages)

    if settings.get("auto_export") and result.added:
        export(pipeline.store, settings, logger)
    return report_result(result, logger)


def cmd_collect_npi(args, settings, logger):
    if args.all_states:
        states = list(catalog.STATES.values())
    else:
        _name, abbr = catalog.resolve_state(args.state)
        if not abbr:
            logger.error("Unknown state: %s", args.state)
            return EXIT_FAILED
        states = [abbr]

    if args.all_specialties:
        specialties = list(catalog.SPECIALTIES.values())
    elif args.specialty in catalog.SPECIALTIES:
        specialties = [catalog.SPECIALTIES[args.specialty]]
    elif args.specialty:
        specialties = [args.specialty]  # a raw taxonomy string is fine too
    else:
        logger.error("Give --specialty or --all-specialties.")
        return EXIT_FAILED

    reporter = ConsoleReporter(logger, quiet=args.quiet)
    pipeline = build_pipeline(settings, logger, reporter)
    result = pipeline.collect_npi(
        states, specialties,
        exclude_corp=settings.get("exclude_corporate", True),
        min_year=settings.get("min_updated_year"))

    if settings.get("auto_export") and result.added:
        export(pipeline.store, settings, logger)
    return report_result(result, logger)


def cmd_import_npi(args, settings, logger):
    text = ""
    if args.file:
        with open(args.file, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    if args.npi:
        text += "\n" + "\n".join(args.npi)
    numbers = LeadPipeline.extract_npi_numbers(text)
    if not numbers:
        logger.error("No NPI numbers found in the input.")
        return EXIT_FAILED

    logger.info("Found %d NPI number(s).", len(numbers))
    reporter = ConsoleReporter(logger, quiet=args.quiet)
    pipeline = build_pipeline(settings, logger, reporter)
    result = pipeline.import_npis(numbers)

    if settings.get("auto_export") and result.added:
        export(pipeline.store, settings, logger)
    return report_result(result, logger)


def cmd_find_emails(args, settings, logger):
    reporter = ConsoleReporter(logger, quiet=args.quiet)
    pipeline = build_pipeline(settings, logger, reporter)
    result = pipeline.find_missing_emails()
    if settings.get("auto_export"):
        export(pipeline.store, settings, logger)
    return report_result(result, logger)


def cmd_verify(args, settings, logger):
    settings["verify_emails"] = True
    reporter = ConsoleReporter(logger, quiet=args.quiet)
    pipeline = build_pipeline(settings, logger, reporter)
    result = pipeline.verify_emails(recheck=args.recheck)
    if settings.get("auto_export"):
        export(pipeline.store, settings, logger)
    return report_result(result, logger)


def cmd_enrich_cms(args, settings, logger):
    reporter = ConsoleReporter(logger, quiet=args.quiet)
    pipeline = build_pipeline(settings, logger, reporter)
    result = pipeline.enrich_from_cms(recheck=args.recheck)
    if settings.get("auto_export"):
        export(pipeline.store, settings, logger)
    return report_result(result, logger)


def cmd_send(args, settings, logger):
    """Send a campaign. Defaults to a dry run — you have to ask for a real one."""
    try:
        with open(args.template, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as e:
        logger.error("Could not read the template: %s", e)
        return EXIT_FAILED

    # A template file is "Subject: ..." then a blank line then the body.
    subject, _, body = raw.partition("\n\n")
    if subject.lower().startswith("subject:"):
        subject = subject.split(":", 1)[1].strip()
    else:
        body = raw
        subject = args.subject or ""
    if not subject:
        logger.error("No subject. Put 'Subject: ...' on the first line of the "
                     "template, or pass --subject.")
        return EXIT_FAILED

    reporter = ConsoleReporter(logger, quiet=args.quiet)
    pipeline = build_pipeline(settings, logger, reporter)
    result = pipeline.send_campaign(
        subject, body, campaign=args.campaign or os.path.basename(args.template),
        limit=args.limit, daily_cap=settings.get("daily_send_cap", 40),
        icp_only=args.icp, owners_only=args.owners_only,
        allow_unverified=args.allow_unverified,
        dry_run=not args.send,
        min_seconds=settings.get("send_min_seconds", 45),
        max_seconds=settings.get("send_max_seconds", 90))
    if not args.send:
        logger.info("This was a dry run. Add --send to actually deliver.")
    return report_result(result, logger)


def cmd_suppress(args, settings, logger):
    store = LeadStore(settings["db_path"], logger=logger)
    if args.list:
        rows = store.suppressions()
        if not rows:
            print("Nothing suppressed.")
            return EXIT_OK
        print()
        for row in rows:
            print("  %-40s %s  %s" % (row["value"], (row["added_at"] or "")[:10],
                                      row["reason"] or ""))
        print()
        return EXIT_OK
    if args.remove:
        store.unsuppress(args.remove)
        logger.info("%s removed from the suppression list.", args.remove)
        return EXIT_OK
    if not args.add:
        logger.error("Give --add, --remove or --list.")
        return EXIT_FAILED
    for value in args.add:
        if store.suppress(value, args.reason or "manual"):
            logger.info("%s will never be contacted again.", value)
        else:
            logger.error("%s is not an address or @domain.", value)
    return EXIT_OK


def cmd_export(args, settings, logger):
    store = LeadStore(settings["db_path"], logger=logger)
    if args.excel:
        settings["output"] = "excel"
        settings["excel_path"] = args.excel
    if args.google:
        settings["output"] = "google"
    statuses = [s.strip() for s in args.status.split(",")] if args.status else None
    written = export(store, settings, logger, min_score=args.min_score,
                     statuses=statuses, call_list=args.call_list, icp_only=args.icp)
    return EXIT_OK if written else EXIT_EMPTY


def cmd_stats(args, settings, logger):
    store = LeadStore(settings["db_path"], logger=logger)
    stats = store.get_stats()
    total = stats["total"]
    if not total:
        print("The database is empty. Collect something first.")
        return EXIT_EMPTY

    def percent(value):
        return "{0} ({1:.1f}%)".format(value, value / total * 100)

    print()
    print("  Total leads          {0}".format(total))
    print("  With an email        {0}".format(percent(stats["with_email"])))
    print("  With a phone         {0}".format(percent(stats["with_phone"])))
    print("  Mail-server verified {0}".format(percent(stats["verified_email"])))
    print("  Average lead score   {0}".format(stats["avg_score"]))
    print()
    print("  Email status")
    for status, count in sorted(stats["by_email_status"].items(), key=lambda kv: -kv[1]):
        print("    {0:<12} {1:>6}".format(status, count))
    print()
    print("  Tabs")
    for sheet, values in sorted(stats["by_source"].items()):
        print("    {0:<32} {1:>6} leads, {2:>5} emails".format(
            sheet, values["total"], values["with_email"]))
    print()
    return EXIT_OK


def cmd_runs(args, settings, logger):
    store = LeadStore(settings["db_path"], logger=logger)
    runs = store.recent_runs(args.limit)
    if not runs:
        print("No runs recorded yet.")
        return EXIT_OK
    print()
    print("  {0:<20} {1:<14} {2:<9} {3:>7}  {4}".format(
        "When", "Action", "Status", "Added", "Note"))
    for run in runs:
        print("  {0:<20} {1:<14} {2:<9} {3:>7}  {4}".format(
            (run["started_at"] or "")[:19].replace("T", " "),
            run["action"], (run["status"] or "").upper(), run["added"] or 0,
            (run["message"] or "")[:60]))
    print()
    return EXIT_OK


# --------------------------------------------------------------------- main

def build_parser():
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Medical provider lead generation — command line interface.")
    parser.add_argument("--config", default=config.CONFIG_FILE,
                        help="Path to config.json (default: %(default)s)")
    parser.add_argument("--db", help="Override the database path.")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress the progress line.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check every source is working.")
    doctor.add_argument("--state", default="Texas",
                        help="State to test the directory scrape against.")
    doctor.set_defaults(func=cmd_doctor)

    collect_pt = subparsers.add_parser("collect-pt",
                                       help="Collect from the Psychology Today directory.")
    collect_pt.add_argument("--state", required=True)
    collect_pt.add_argument("--owners-only", action="store_true",
                            help="Keep only providers their own site presents as "
                                 "the owner/founder/CEO.")
    collect_pt.add_argument("--no-resume", action="store_true",
                            help="Start from page 1 even if a previous run stopped partway.")
    collect_pt.add_argument("--max-pages", type=int)
    collect_pt.set_defaults(func=cmd_collect_pt)

    collect_npi = subparsers.add_parser("collect-npi", help="Collect from the NPI registry.")
    collect_npi.add_argument("--state")
    collect_npi.add_argument("--all-states", action="store_true")
    collect_npi.add_argument("--specialty")
    collect_npi.add_argument("--all-specialties", action="store_true")
    collect_npi.set_defaults(func=cmd_collect_npi)

    import_npi = subparsers.add_parser("import-npi",
                                       help="Look up NPI numbers copied by hand.")
    import_npi.add_argument("--file", help="File containing NPI numbers or pasted text.")
    import_npi.add_argument("--npi", nargs="*", help="NPI numbers given directly.")
    import_npi.set_defaults(func=cmd_import_npi)

    find_emails = subparsers.add_parser("find-emails",
                                        help="Second pass for leads with no email.")
    find_emails.set_defaults(func=cmd_find_emails)

    verify = subparsers.add_parser("verify", help="Check recorded addresses are real.")
    verify.add_argument("--recheck", action="store_true",
                        help="Re-check addresses that already have a status.")
    verify.set_defaults(func=cmd_verify)

    enrich_cms = subparsers.add_parser(
        "enrich-cms",
        help="Attach Medicare billing volume, practice size and federally-filed "
             "phone numbers.")
    enrich_cms.add_argument("--recheck", action="store_true",
                            help="Re-query providers already checked.")
    enrich_cms.set_defaults(func=cmd_enrich_cms)

    send = subparsers.add_parser(
        "send", help="Send an outreach campaign (dry run unless --send).")
    send.add_argument("--template", required=True,
                      help="Text file: 'Subject: ...' on line 1, blank line, then "
                           "the body. Use {{First Name}} style merge fields.")
    send.add_argument("--subject", help="Overrides the template's subject line.")
    send.add_argument("--campaign", help="A name for this campaign, for the log.")
    send.add_argument("--limit", type=int, default=25)
    send.add_argument("--icp", action="store_true",
                      help="Only owner-operated small practices.")
    send.add_argument("--owners-only", action="store_true")
    send.add_argument("--allow-unverified", action="store_true",
                      help="Include addresses that could not be verified. Raises "
                           "the bounce rate; bounces are suppressed automatically.")
    send.add_argument("--send", action="store_true",
                      help="Actually deliver. Without this it is a dry run.")
    send.set_defaults(func=cmd_send)

    suppress = subparsers.add_parser(
        "suppress", help="Manage the do-not-contact list.")
    suppress.add_argument("--add", nargs="*", help="Addresses or @domains to block.")
    suppress.add_argument("--remove", help="Take one off the list.")
    suppress.add_argument("--list", action="store_true")
    suppress.add_argument("--reason", help="Why, for the record.")
    suppress.set_defaults(func=cmd_suppress)

    export_cmd = subparsers.add_parser("export", help="Write the spreadsheet.")
    export_cmd.add_argument("--excel", help="Write to this .xlsx path.")
    export_cmd.add_argument("--google", action="store_true", help="Write to Google Sheets.")
    export_cmd.add_argument("--min-score", type=int,
                            help="Only export leads scoring at least this (0-100).")
    export_cmd.add_argument("--call-list", type=int, default=100,
                            help="Rows on the ranked Call List tab (0 to omit).")
    export_cmd.add_argument("--icp", action="store_true",
                            help="Restrict the Call List to owner-operated "
                                 "practices small enough to need outsourced billing.")
    export_cmd.add_argument("--status",
                            help="Only export these email statuses, comma separated "
                                 "(e.g. valid,role).")
    export_cmd.set_defaults(func=cmd_export)

    stats = subparsers.add_parser("stats", help="Show what is in the database.")
    stats.set_defaults(func=cmd_stats)

    runs = subparsers.add_parser("runs", help="Show recent runs and their outcomes.")
    runs.add_argument("--limit", type=int, default=15)
    runs.set_defaults(func=cmd_runs)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logger = build_logger(args.verbose)
    settings = config.load(args.config)
    if args.db:
        settings["db_path"] = args.db
    try:
        return args.func(args, settings, logger)
    except KeyboardInterrupt:
        logger.warning("Interrupted. Progress so far is saved — rerun the same "
                       "command to resume.")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
