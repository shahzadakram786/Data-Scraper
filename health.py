"""Health check — "is everything still working?" in about ten seconds.

Every part of this tool depends on a third party that can change or block us
without notice: Psychology Today re-shapes its markup, DuckDuckGo rate-limits,
an ISP blocks port 25, a router refuses MX queries. When one of those happens
mid-run the symptom is always the same — a run that finishes with far fewer
leads than it should, or none.

Running this first turns a forty-minute mystery into a ten-second answer, and
it is what the "Health Check" button in the app calls.
"""

import logging
import traceback


def _step(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def check_npi(logger=None):
    """The NPI registry is the identity backbone — everything cross-references
    it. Looked up by a known-good number so the answer is unambiguous."""
    from npi_engine import NPIEngine

    report = {"source": "NPI Registry", "ok": False, "steps": []}
    engine = NPIEngine(logger=logger)
    try:
        # A live, long-standing organisational NPI (Mayo Clinic Rochester).
        match = engine.find_by_number("1023011178")
    except Exception as e:
        report["steps"].append(
            _step("Look up a known NPI", False, f"{type(e).__name__}: {e}"))
        return report

    if not match:
        report["steps"].append(_step(
            "Look up a known NPI", False,
            "The registry answered but returned no record for a number that "
            "definitely exists. Their API shape may have changed."))
        return report

    name = (match.get("Organization Name")
            or " ".join(filter(None, (match.get("Provider First Name"),
                                      match.get("Provider Last Name"))))
            or "(unnamed)")
    report["steps"].append(_step("Look up a known NPI", True, f"Resolved to {name}."))
    report["ok"] = True
    return report


def check_psychology_today(proxies=None, logger=None, state="Texas"):
    from psychology_today_engine import PsychologyTodayEngine
    return PsychologyTodayEngine(logger=logger, proxies=proxies).self_check(state)


def check_cms(proxies=None, logger=None):
    """CMS billing and enrollment data — geo-fenced like Psychology Today, so
    this also confirms the reader relay is working for it."""
    from cms_engine import CMSEngine
    return CMSEngine(logger=logger, proxies=proxies).self_check()


def check_search(logger=None):
    """DuckDuckGo backs every email/website fallback. When it rate-limits, hit
    rates collapse and nothing in the log says why."""
    from ddgs import DDGS

    report = {"source": "Web Search (DuckDuckGo)", "ok": False, "steps": []}
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text("psychology practice contact email", max_results=3))
    except Exception as e:
        detail = f"{type(e).__name__}: {e}"
        if any(k in detail.lower() for k in ("ratelimit", "rate limit", "202", "403")):
            detail += (" — this is a rate limit, not a break. Wait a few minutes; "
                       "the engine already backs off automatically.")
        report["steps"].append(_step("Run a test search", False, detail))
        return report

    ok = bool(results)
    report["steps"].append(_step(
        "Run a test search", ok,
        f"Returned {len(results)} results." if ok else
        "The search returned nothing, which usually means a rate limit."))
    report["ok"] = ok
    return report


def check_email_verification(logger=None):
    """MX lookups and, where the network allows it, mailbox-level SMTP checks."""
    from verification import EmailVerifier

    report = {"source": "Email Verification", "ok": False, "steps": []}
    verifier = EmailVerifier(logger=logger)

    hosts = verifier._mx_for("gmail.com")
    dns_ok = bool(hosts)
    report["steps"].append(_step(
        "Look up mail servers (DNS MX)", dns_ok,
        f"Resolved gmail.com to {hosts[0]}." if dns_ok else
        "No resolver on this machine would answer an MX query, so addresses "
        "cannot be checked against a mail server."))

    smtp_ok = verifier._smtp_reachable()
    report["steps"].append(_step(
        "Reach a mail server (SMTP port 25)", smtp_ok,
        "Port 25 is open — individual mailboxes can be confirmed." if smtp_ok else
        "Outbound port 25 is blocked on this network, so mailboxes cannot be "
        "confirmed individually. Dead domains are still caught, and addresses "
        "are reported as 'unknown' rather than 'valid'. This is normal on home "
        "and office internet connections."))

    # DNS alone is enough to be useful; SMTP is a bonus.
    report["ok"] = dns_ok
    report["degraded"] = dns_ok and not smtp_ok
    return report


def run_all(proxies=None, logger=None, state="Texas", include=None):
    """Run every check and return a list of reports.

    include: optional list of keys ("npi", "pt", "search", "email") to run a
    subset — the app uses it to check just the source a run is about to use.
    """
    logger = logger or logging.getLogger(__name__)
    checks = {
        "npi": lambda: check_npi(logger),
        "pt": lambda: check_psychology_today(proxies, logger, state),
        "search": lambda: check_search(logger),
        "email": lambda: check_email_verification(logger),
        "cms": lambda: check_cms(proxies, logger),
    }
    reports = []
    for key in (include or ("npi", "pt", "search", "email", "cms")):
        check = checks.get(key)
        if check is None:
            continue
        try:
            reports.append(check())
        except Exception as e:
            logger.debug(traceback.format_exc())
            reports.append({
                "source": key,
                "ok": False,
                "steps": [_step("Run check", False, f"{type(e).__name__}: {e}")],
            })
    return reports


def format_report(reports):
    """Plain-text rendering, used by the CLI and by the app's dialog."""
    lines = []
    for report in reports:
        overall = "OK" if report["ok"] else "FAILED"
        if report.get("degraded"):
            overall = "OK (reduced)"
        lines.append(f"{report['source']}: {overall}")
        for step in report["steps"]:
            lines.append(f"   [{'ok' if step['ok'] else '!!'}] {step['name']}")
            lines.append(f"        {step['detail']}")
        lines.append("")
    return "\n".join(lines).rstrip()
