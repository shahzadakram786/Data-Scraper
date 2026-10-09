"""What a practice's homepage says about what the practice needs.

Lead types ("needs a website", "needs SEO", "needs bookkeeping") are rules
over facts recorded here: has a mobile viewport, names an analytics vendor,
links a privacy policy. The facts are read once, from the homepage the scraper
fetches anyway for the email and phone, and stored on the lead — so editing a
lead type later changes the next export without re-scraping anything.

No database, and no network beyond the one DNS sanity check in
network_resolves. The fetch itself lives in email_engine; what the facts mean
lives in lead_types.
"""

import json
import zlib
import socket
import base64
import binascii
from datetime import date

import requests

# Bumped when a fact is added or changes meaning. A check that needs a fact an
# older record lacks reads "couldn't check" until the site is read again.
SCHEMA_VERSION = 1

# Visible homepage text kept for keyword checks. A homepage's words fit in
# this comfortably; the cap stops one sprawling page bloating the database.
TEXT_CAP = 15_000

# How the homepage fetch went.
FETCH_OK = "ok"
FETCH_TIMEOUT = "timeout"
FETCH_DNS = "dns"
FETCH_SSL = "ssl"
FETCH_HTTP_ERROR = "http_error"
FETCH_BLOCKED = "blocked"
FETCH_CONNECTION = "connection"

# Outcomes that say nothing about the practice: a slow afternoon, bot
# protection, or this machine's own connection. They are never recorded — so
# the next pass tries again — and never count as evidence that a site is down.
TRANSIENT_FETCHES = frozenset({FETCH_TIMEOUT, FETCH_BLOCKED, FETCH_CONNECTION})

# Why a lead has no site of its own.
NO_SITE_LISTED = "none_listed"   # Psychology Today says they list no website
NO_SITE_PROFILE = "profile"      # their "website" is a marketplace or directory page

# How a directory answered "what is this provider's website?". Only "none" is
# evidence: a failed lookup says nothing about the provider.
WEBSITE_LISTED = "listed"
WEBSITE_NONE = "none"
WEBSITE_UNKNOWN = "unknown"

_DNS_FAILURE_MARKERS = (
    "getaddrinfo failed", "name or service not known", "nodename nor servname",
    "no address associated", "name resolution", "failed to resolve",
)

# Hosts that always resolve. When they don't either, a DNS failure is this
# machine's connection, not the practice's domain.
CANARY_HOSTS = ("www.google.com", "one.one.one.one")


def classify_fetch_error(exc):
    """The fetch outcome for a requests exception.

    Order matters: requests' SSLError and ConnectTimeout are both kinds of
    ConnectionError, so the specific cases are tested first.
    """
    if isinstance(exc, requests.exceptions.SSLError):
        return FETCH_SSL
    if isinstance(exc, requests.exceptions.Timeout):
        return FETCH_TIMEOUT
    if isinstance(exc, requests.exceptions.ConnectionError):
        text = str(exc).lower()
        if any(marker in text for marker in _DNS_FAILURE_MARKERS):
            return FETCH_DNS
    return FETCH_CONNECTION


def classify_status(status):
    """The fetch outcome for an HTTP status.

    Only a missing page or a server error counts as the site being broken.
    401, 403 and 429 are almost always bot protection turning a script away —
    a person in a browser would see the site — so they are "blocked", which
    is evidence of nothing.
    """
    if status == 200:
        return FETCH_OK
    if status in (404, 410) or (status is not None and status >= 500):
        return FETCH_HTTP_ERROR
    return FETCH_BLOCKED


def network_resolves(hosts=CANARY_HOSTS):
    """Whether this machine can resolve names at all right now."""
    for host in hosts:
        try:
            socket.getaddrinfo(host, 443)
            return True
        except OSError:
            continue
    return False


def pack_text(text):
    """Compress visible text for storage. Empty in, empty out."""
    if not text:
        return ""
    return base64.b64encode(zlib.compress(text.encode("utf-8"), 9)).decode("ascii")


def unpack_text(packed):
    """The inverse of pack_text. Anything unreadable comes back empty rather
    than raising — a damaged value must cost one keyword check, not an export."""
    if not packed:
        return ""
    try:
        return zlib.decompress(base64.b64decode(packed, validate=True)).decode("utf-8")
    except (ValueError, binascii.Error, zlib.error):
        return ""


def dump_facts(facts):
    return json.dumps(facts, sort_keys=True, separators=(",", ":"))


def load_facts(value):
    """Facts as a dict, from the stored JSON (or a dict already). Unreadable
    or missing facts are an empty dict: nothing known, so nothing concluded."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def base_facts(own_site, fetch=None, status=None, today=None):
    facts = {"v": SCHEMA_VERSION, "checked": (today or date.today()).isoformat(),
             "own_site": own_site}
    if fetch is not None:
        facts["fetch"] = fetch
    if status is not None:
        facts["status"] = status
    return facts


def no_site_facts(why, profile_host="", today=None):
    """A lead with no website of its own: none listed, or only a profile page."""
    facts = base_facts(False, today=today)
    facts["why"] = why
    if profile_host:
        facts["profile_host"] = profile_host
    return facts


def failed_audit(fetch, exc, status=None, today=None):
    """The page arrived but could not be read. Every site check reads this as
    couldn't check."""
    facts = base_facts(True, fetch, status, today)
    facts["audit_error"] = type(exc).__name__
    return facts


def columns(facts, text=""):
    """The lead fields that record a homepage read, or {} when there is
    nothing worth recording (no facts, or a transient fetch failure)."""
    if not facts or facts.get("fetch") in TRANSIENT_FETCHES:
        return {}
    return {"Site Facts": dump_facts(facts), "Site Text": pack_text(text),
            "Site Checked": facts.get("checked") or date.today().isoformat()}
