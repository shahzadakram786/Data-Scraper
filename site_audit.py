"""What a practice's homepage says about what the practice needs.

Lead types ("needs a website", "needs SEO", "needs bookkeeping") are rules
over facts recorded here: has a mobile viewport, names an analytics vendor,
links a privacy policy. The facts are read once, from the homepage the scraper
fetches anyway for the email and phone, and stored on the lead — so editing a
lead type later changes the next export without re-scraping anything.

No database, and no network beyond the two checks on this machine itself
(network_reachable, tls_works). The fetch lives in email_engine; what the
facts mean lives in lead_types.
"""

import re
import json
import zlib
import socket
import base64
import binascii
from datetime import date
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

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
FETCH_SERVER_ERROR = "server_error"
FETCH_BLOCKED = "blocked"
FETCH_CONNECTION = "connection"

# Outcomes that say nothing lasting about the practice: a slow afternoon, a
# server having a bad minute, bot protection, or this machine's own
# connection. They are never recorded — so the next pass tries again — and
# never count as evidence that a site is down.
TRANSIENT_FETCHES = frozenset({FETCH_TIMEOUT, FETCH_SERVER_ERROR, FETCH_BLOCKED,
                               FETCH_CONNECTION})

# Why a lead has no site of its own.
NO_SITE_LISTED = "none_listed"   # Psychology Today says they list no website
NO_SITE_PROFILE = "profile"      # their "website" is a marketplace or directory page

# How a directory answered "what is this provider's website?". Only "none" is
# evidence: a failed lookup says nothing about the provider.
WEBSITE_LISTED = "listed"
WEBSITE_NONE = "none"
WEBSITE_UNKNOWN = "unknown"

# A domain that does not exist (Windows errno 11001, EAI_NONAME elsewhere).
# Only this is evidence: a temporary resolver failure (11002, EAI_AGAIN —
# "Temporary failure in name resolution") says nothing about the domain.
_DOMAIN_NOT_FOUND_MARKERS = (
    "11001", "name or service not known", "nodename nor servname",
    "no address associated", "non-existent domain",
)
_DNS_TEMPORARY_MARKERS = ("11002", "temporary failure", "try again")

# Addresses that always accept connections. A connection, unlike a name
# lookup, cannot be answered from the DNS cache for minutes after the line
# drops, so it says whether this machine is online right now.
CANARY_ADDRESSES = (("1.1.1.1", 443), ("8.8.8.8", 443))
# A site whose certificate is always valid: if its handshake fails too, the
# fault is this machine's TLS (a proxy, antivirus scanning, stale bundle).
TLS_CANARY_URL = "https://www.google.com/"


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
        if any(marker in text for marker in _DNS_TEMPORARY_MARKERS):
            return FETCH_CONNECTION
        if any(marker in text for marker in _DOMAIN_NOT_FOUND_MARKERS):
            return FETCH_DNS
    return FETCH_CONNECTION


def classify_status(status):
    """The fetch outcome for an HTTP status.

    Only a missing page counts as the site being broken. A 5xx is a server
    error that may pass — 503 literally means "temporarily unavailable", and
    firewall block pages and maintenance pages use it too — so it is tried
    again rather than recorded. 401, 403 and 429 are almost always bot
    protection turning a script away (a person in a browser would see the
    site), so they are "blocked", which is evidence of nothing.
    """
    if status == 200:
        return FETCH_OK
    if status in (404, 410):
        return FETCH_HTTP_ERROR
    if status is not None and status >= 500:
        return FETCH_SERVER_ERROR
    return FETCH_BLOCKED


def network_reachable(addresses=CANARY_ADDRESSES):
    """Whether this machine is online right now."""
    for address in addresses:
        try:
            socket.create_connection(address, timeout=3).close()
            return True
        except OSError:
            continue
    return False


def tls_works():
    """Whether this machine can complete an HTTPS handshake with a site whose
    certificate is always valid."""
    try:
        requests.head(TLS_CANARY_URL, timeout=5)
        return True
    except requests.RequestException:
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


# --------------------------------------------------------------- reading

# Parsing stops here. Practice homepages are a few hundred kilobytes; this
# bounds the time one pathological page can take.
HTML_CAP = 1_000_000

# Vendor fingerprints: (name, needles). Needles are matched against the
# lowercased raw HTML, so script URLs and inline snippets are both seen. The
# name is what a reason line or a debugging session reports as found.
ANALYTICS = (
    ("google-analytics", ("googletagmanager.com/gtag/js", "google-analytics.com/analytics.js",
                          "google-analytics.com/ga.js")),
    ("google-tag-manager", ("googletagmanager.com/gtm.js", "googletagmanager.com/ns.html")),
    ("plausible", ("plausible.io/js",)),
    ("matomo", ("matomo.js", "piwik.js")),
    ("fathom", ("usefathom.com",)),
    ("microsoft-clarity", ("clarity.ms/tag",)),
    ("hotjar", ("static.hotjar.com",)),
)

AD_PIXELS = (
    ("meta", ("fbevents.js", "fbq('init'", 'fbq("init"')),
    # 'AW-' is a Google Ads conversion ID inside a gtag('config', ...) call.
    ("google-ads", ("googleadservices.com", "googleads.g.doubleclick.net", "'aw-", '"aw-')),
    ("linkedin", ("snap.licdn.com", "_linkedin_partner_id")),
    ("tiktok", ("analytics.tiktok.com",)),
    ("microsoft-ads", ("bat.bing.com",)),
)

BOOKING = (
    ("simplepractice", ("simplepractice.com",)),
    ("calendly", ("calendly.com",)),
    ("acuity", ("acuityscheduling.com", "squarespacescheduling.com")),
    ("jane", ("janeapp.com",)),
    ("zocdoc", ("offsiteschedule.zocdoc.com", "zocdoc.com/practice", "zocdoc.com/doctor")),
    ("intakeq", ("intakeq.com/booking", "intakeq.com/new")),
    ("square", ("squareup.com/appointments", "square.site/book")),
    ("setmore", ("setmore.com",)),
    ("vagaro", ("vagaro.com",)),
    ("mindbody", ("mindbodyonline.com",)),
    ("practice-better", ("practicebetter.io",)),
    ("healthie", ("gethealthie.com",)),
    ("nexhealth", ("nexhealth.com",)),
    ("localmed", ("localmed.com",)),
)

NEWSLETTER = (
    ("mailchimp", ("list-manage.com", "chimpstatic.com")),
    ("constant-contact", ("constantcontact.com", "ctctcdn.com")),
    ("klaviyo", ("klaviyo.com",)),
    ("convertkit", ("convertkit.com", "ck.page")),
    ("mailerlite", ("mailerlite.com", "mlcdn.com")),
    ("brevo", ("sibforms.com", "sendinblue.com")),
    ("aweber", ("aweber.com",)),
    ("substack", ("substack.com/embed",)),
)

LIVE_CHAT = (
    ("tawk", ("embed.tawk.to",)),
    ("intercom", ("widget.intercom.io", "js.intercomcdn.com")),
    ("drift", ("js.driftt.com",)),
    ("livechat", ("cdn.livechatinc.com",)),
    ("tidio", ("code.tidio.co",)),
    ("crisp", ("client.crisp.chat",)),
    ("zendesk", ("static.zdassets.com",)),
    ("hubspot-chat", ("js.usemessages.com",)),
    ("olark", ("static.olark.com",)),
    ("podium", ("connect.podium.com",)),
)

PORTAL_HOSTS = ("clientsecure.me", "therapyportal.com", "mychart", "healow.com",
                "followmyhealth", "patientportal", "patient-portal", "client-portal")
VIDEO_HOSTS = ("youtube.com/embed", "youtube-nocookie.com/embed", "player.vimeo.com",
               "fast.wistia", "wistia.com/embed", "vidyard.com")
CONTACT_FORM_HOSTS = ("jotform.com", "typeform.com", "wufoo.com", "formstack.com",
                      "docs.google.com/forms", "123formbuilder.com", "hsforms.net",
                      "cognitoforms.com")

# First match wins; WordPress sits late because builders embed wp- paths in
# imported content more often than the reverse.
BUILDERS = (
    ("wix", ("static.wixstatic.com", "wix.com website builder")),
    ("squarespace", ("static1.squarespace.com", "squarespace.com")),
    ("webflow", ("data-wf-site", "webflow.com")),
    ("weebly", ("weebly.com", "editmysite.com")),
    ("godaddy", ("img1.wsimg.com", "godaddy website builder")),
    ("duda", ("irp.cdn-website.com",)),
    ("shopify", ("cdn.shopify.com",)),
    ("wordpress", ("wp-content/", "wp-includes/")),
    ("joomla", ("/media/jui/",)),
    ("drupal", ("drupal.settings", "/sites/default/files/")),
)

# Matched against "href + link text", lowercased.
PORTAL_RE = re.compile(r"\b(?:patient|client)[\s_-]*portal\b")
BLOG_RE = re.compile(r"\bblog\b|\barticles?\b|\bresources\b|\bnews\b|\binsights\b")
# "/privacy" as a path of its own, never inside "notice-of-privacy-practices".
PRIVACY_RE = re.compile(r"privacy[\s_-]*(?:policy|statement)|/privacy(?:[/._?#-]|\s|$)")
HIPAA_RE = re.compile(r"hipaa|privacy[\s_-]*practices")
TERMS_RE = re.compile(r"\bterms\b")
ACCESSIBILITY_RE = re.compile(r"accessibility|\bada[\s_-]+(?:statement|compliance|notice)")
LOGO_RE = re.compile(r"logo", re.I)
SUBSCRIBE_RE = re.compile(r"subscribe|newsletter|mailing list")

# The year must follow the mark directly — only spaces, light punctuation and
# the other ways of writing "copyright" may come between. Anything looser
# skips a script-filled (empty) year and reads the street number or phone
# number after the practice's name instead.
COPYRIGHT_RE = re.compile(
    r"(?:©|\(c\)|\bcopyright\b)"
    r"(?:\s|[.,:]|©|\(c\)|\bcopyright\b)*"
    r"((?:19|20)\d{2})(?!\d)"
    r"(?:\s*[-–—]\s*((?:19|20)\d{2}(?!\d)|present\b|now\b|today\b))?",
    re.I)

WP_GENERATOR_RE = re.compile(r"wordpress\s+(\d+(?:\.\d+){0,2})", re.I)
WP_ASSET_RE = re.compile(r"wp-includes/([^\"'\s>?]+)\?ver=(\d+(?:\.\d+){1,2})", re.I)
# Core assets carry the WordPress version in ?ver=; the libraries WordPress
# bundles carry their own (jQuery's 3.7.1 is not WordPress 3.7).
WP_BUNDLED_LIBRARIES = ("jquery", "vendor", "underscore", "backbone", "imagesloaded",
                        "masonry", "hoverintent", "regenerator", "polyfill", "moment",
                        "lodash", "react", "mediaelement", "thickbox", "swfobject")

# schema.org types that describe the business itself, as opposed to the page
# (WebSite, WebPage, BreadcrumbList, ...).
BUSINESS_TYPE_RE = re.compile(
    r"(?:Business|Organization|Organisation|Clinic|Physician|Dentist|Psychologist|"
    r"Hospital|Practice|Service|Therapist)$")


class EmptyPage(Exception):
    """The server answered 200 with nothing in the body."""


def read_homepage(html, final_url, status=200, fetch=FETCH_OK, today=None):
    """(facts, visible text) for one homepage. Never raises.

    `fetch` is how the request went (see classify_*). When it did not go well
    there is no page to read: the facts record only the outcome, and every
    check that needs a page reads it as couldn't check.
    """
    today = today or date.today()
    if fetch != FETCH_OK:
        return base_facts(True, fetch, status, today), ""
    if not html or not html.strip():
        return failed_audit(fetch, EmptyPage(), status, today), ""
    try:
        return _read(html[:HTML_CAP], final_url, status, today)
    except Exception as exc:   # a markup surprise must cost the facts, not the lead
        return failed_audit(fetch, exc, status, today), ""


def _read(html, final_url, status, today):
    facts = base_facts(True, FETCH_OK, status, today)
    lowered = html.lower()
    soup = BeautifulSoup(html, "lxml")
    links = [((a.get("href") or "") + " " + a.get_text(" ", strip=True)).lower()
             for a in soup.find_all("a")]
    images = soup.find_all("img")

    facts["https"] = urlparse(final_url or "").scheme.lower() == "https"
    facts["viewport"] = soup.find("meta", attrs={"name": _named("viewport")}) is not None
    title = soup.find("title")
    facts["title"] = bool(title and title.get_text(strip=True))
    description = soup.find("meta", attrs={"name": _named("description")})
    facts["meta_description"] = bool(description and (description.get("content") or "").strip())
    facts["h1"] = soup.find("h1") is not None
    facts["schema_org"] = any(BUSINESS_TYPE_RE.search(t) for t in _schema_types(soup))
    facts["analytics"] = _detect(lowered, ANALYTICS)
    facts["ad_pixels"] = _detect(lowered, AD_PIXELS)
    facts["booking"] = _detect(lowered, BOOKING)
    facts["newsletter"] = _detect(lowered, NEWSLETTER) or (
        ["signup-form"] if _has_signup_form(soup) else [])
    facts["live_chat"] = _detect(lowered, LIVE_CHAT)
    facts["portal"] = (any(host in lowered for host in PORTAL_HOSTS)
                       or any(PORTAL_RE.search(link) for link in links))
    facts["contact_form"] = (
        any(form.find("textarea") is not None for form in soup.find_all("form"))
        or any(host in lowered for host in CONTACT_FORM_HOSTS))
    facts["blog"] = any(BLOG_RE.search(link) for link in links)
    facts["privacy_policy"] = any(PRIVACY_RE.search(link) for link in links)
    facts["terms"] = any(TERMS_RE.search(link) for link in links)
    facts["accessibility"] = any(ACCESSIBILITY_RE.search(link) for link in links)
    facts["logo"] = _has_logo(soup, images)
    facts["favicon"] = any("icon" in _rel(link) for link in soup.find_all("link"))
    facts["share_image"] = bool(
        soup.find("meta", attrs={"property": re.compile(r"^og:image", re.I)})
        or soup.find("meta", attrs={"name": re.compile(r"^twitter:image", re.I)}))
    facts["video"] = (soup.find("video") is not None
                      or any(host in lowered for host in VIDEO_HOSTS))
    facts["images"] = len(images)
    facts["images_missing_alt"] = sum(
        1 for img in images if not (img.get("alt") or "").strip())
    facts["wordpress_version"] = _wordpress_version(soup, html)
    facts["builder"] = next((name for name, needles in BUILDERS
                             if any(n in lowered for n in needles)), "")

    # Last, because it strips the scripts the checks above read. The whole
    # text is searched before it is capped: the copyright line lives in the
    # footer, which on a long page is past the cap.
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    facts["hipaa_notice"] = (any(HIPAA_RE.search(link) for link in links)
                             or "notice of privacy practices" in text.lower())
    facts["copyright_year"] = newest_copyright_year(text, today.year)
    return facts, text[:TEXT_CAP]


def newest_copyright_year(text, this_year):
    """The latest year in a copyright line ("© 2012-2016" -> 2016, "© 2019 –
    Present" -> this year), or None.

    Years after next year are ignored: they are prices or phone digits that
    happen to follow the word, not dates.
    """
    years = []
    for match in COPYRIGHT_RE.finditer(text or ""):
        end = match.group(2)
        if end and not end[0].isdigit():
            years.append(this_year)          # "– Present", "– now"
        else:
            years.append(int(end or match.group(1)))
    years = [year for year in years if 1990 <= year <= this_year + 1]
    return max(years) if years else None


def _named(name):
    return re.compile("^" + re.escape(name) + "$", re.I)


def _detect(lowered, table):
    return [name for name, needles in table if any(n in lowered for n in needles)]


def _rel(link):
    rel = link.get("rel") or []
    return (" ".join(rel) if isinstance(rel, list) else str(rel)).lower()


def _has_logo(soup, images):
    for img in images:
        blob = " ".join(str(img.get(attr) or "") for attr in ("src", "alt", "class", "id"))
        if LOGO_RE.search(blob):
            return True
    return (soup.find(attrs={"class": LOGO_RE}) is not None
            or soup.find(id=LOGO_RE) is not None)


def _has_signup_form(soup):
    """A form that takes only an email address and talks about subscribing.
    A form with a message box is a contact form, whatever its button says."""
    for form in soup.find_all("form"):
        if form.find("textarea") is not None:
            continue
        if form.find("input", attrs={"type": _named("email")}) is None:
            continue
        parts = [form.get_text(" "), str(form.get("action") or "")]
        for field in form.find_all(["input", "button"]):
            parts.append(str(field.get("placeholder") or ""))
            parts.append(str(field.get("value") or ""))
        if SUBSCRIBE_RE.search(" ".join(parts).lower()):
            return True
    return False


def _schema_types(soup):
    types = set()
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        try:
            _collect_types(json.loads(raw), types)
        except ValueError:
            # Hand-written JSON-LD is often invalid (a trailing comma); the
            # declared types are still plain to see.
            types.update(re.findall(r'"@type"\s*:\s*"([A-Za-z]+)"', raw))
    for tag in soup.find_all(attrs={"itemtype": True}):
        types.add(str(tag["itemtype"]).rstrip("/").rsplit("/", 1)[-1])
    return types


def _collect_types(node, out):
    if isinstance(node, dict):
        declared = node.get("@type")
        for value in (declared if isinstance(declared, list) else [declared]):
            if isinstance(value, str):
                out.add(value)
        for value in node.values():
            _collect_types(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_types(value, out)


def _wordpress_version(soup, html):
    generator = soup.find("meta", attrs={"name": _named("generator")})
    if generator is not None:
        match = WP_GENERATOR_RE.search(generator.get("content") or "")
        if match:
            return match.group(1)
    for path, version in WP_ASSET_RE.findall(html):
        if not any(library in path.lower() for library in WP_BUNDLED_LIBRARIES):
            return version
    return None
