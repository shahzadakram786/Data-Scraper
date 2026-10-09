"""Lead types: which practices look like buyers of a service, and why.

A lead type ("Website Development", "SEO", "Bookkeeping") is a rule over facts
already collected: what the practice's homepage showed (site_audit) and
columns the app fills anyway (Role, Practice Size, Annual Billing). Matching
happens when leads are read — exports, the Call List, campaign recipients, the
Results counts — and is never stored, so editing a type in Settings changes
the very next export without re-scraping anything.

Every check answers True, False or None. None means "couldn't check": no site
was read, or the fact it needs was never recorded. A lead nobody inspected is
reported as not checked, never quietly treated as needing — or not needing —
the service.
"""

import os
import re
import json
import logging
import tempfile
from functools import lru_cache
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from urllib.parse import urlparse

import site_audit
# Shared with explain_lead, so the Why line and a lead type's reason read alike.
from storage import OWNERSHIP_ROLES, is_icp, _to_number, _short_money

MATCH, NO_MATCH, NOT_CHECKED = "match", "no_match", "not_checked"

# WordPress 6.0 shipped in May 2022; anything older is years behind on fixes.
WORDPRESS_OUTDATED_BELOW = (6, 0)
DATED_COPYRIGHT_YEARS = 4
SMALL_PRACTICE_MAX = 5
STAFF_MIN, STAFF_MAX = 2, 25
BILLING_THRESHOLD = 100_000

SOCIAL_COLUMNS = ("LinkedIn", "Facebook", "Instagram", "Other Social")


class Evidence:
    """One lead's recorded facts, parsed once however many types look at them."""

    def __init__(self, lead, today=None):
        self.lead = lead
        self.today = today or date.today()
        self.facts = site_audit.load_facts(lead.get("Site Facts"))
        self._text = None

    @property
    def site_read(self):
        """A homepage of their own was fetched and read without trouble."""
        facts = self.facts
        return (facts.get("own_site") is True and facts.get("fetch") == site_audit.FETCH_OK
                and not facts.get("audit_error"))

    @property
    def text(self):
        if self._text is None:
            self._text = site_audit.unpack_text(self.lead.get("Site Text") or "").lower()
        return self._text

    @property
    def role(self):
        return str(self.lead.get("Role") or "").strip()

    def number(self, header):
        return _to_number(self.lead.get(header))


# ---------------------------------------------------------------- checks

@dataclass(frozen=True)
class Check:
    id: str
    label: str      # how the Settings checklist names it
    run: object     # fn(Evidence) -> (True | False | None, reason)


CHECKS = {}


def _site_lacks(key, reason):
    """True when a site was read and it lacks `key` (False, or an empty vendor
    list). A fact an older record never stored reads as couldn't check."""
    def run(ev):
        if not ev.site_read or key not in ev.facts:
            return None, ""
        return (not ev.facts[key]), reason
    return run


def _no_own_site(ev):
    own = ev.facts.get("own_site")
    if own is False:
        host = ev.facts.get("profile_host")
        return True, ("only a {0} profile".format(host) if host else "no website of their own")
    if own is True:
        return False, ""
    return None, ""


def _site_unreachable(ev):
    if ev.facts.get("own_site") is not True:
        return None, ""
    fetch = ev.facts.get("fetch")
    if fetch == site_audit.FETCH_HTTP_ERROR:
        return True, "site down (HTTP {0})".format(ev.facts.get("status"))
    if fetch == site_audit.FETCH_DNS:
        return True, "site's domain doesn't resolve"
    if fetch == site_audit.FETCH_OK:
        return False, ""
    return None, ""


def _ssl_error(ev):
    if ev.facts.get("own_site") is not True:
        return None, ""
    fetch = ev.facts.get("fetch")
    if fetch == site_audit.FETCH_SSL:
        return True, "SSL certificate error"
    if fetch == site_audit.FETCH_OK:
        return False, ""
    return None, ""


def _dated_copyright(ev):
    # A read site with no year (often one a script fills in) shows no sign of
    # being dated. That is a "no", not a "couldn't check": reading it again
    # would find the same nothing.
    if not ev.site_read:
        return None, ""
    year = ev.facts.get("copyright_year")
    if not year:
        return False, ""
    return int(year) <= ev.today.year - DATED_COPYRIGHT_YEARS, "© {0}".format(year)


def _missing_title_or_description(ev):
    if not ev.site_read or "title" not in ev.facts:
        return None, ""
    missing = [label for key, label in (("title", "page title"),
                                        ("meta_description", "meta description"))
               if not ev.facts.get(key)]
    return bool(missing), "no " + " or ".join(missing)


def _social_links(ev):
    return [(column, str(ev.lead.get(column)).strip()) for column in SOCIAL_COLUMNS
            if str(ev.lead.get(column) or "").strip()]


def _contact_scraped(ev):
    """Whether the read that produced these facts also looked for the role and
    social links (the collection scrape does; a Check websites read does not)."""
    return bool(ev.facts.get("contact_scraped"))


def _no_social_profiles(ev):
    if not ev.site_read:
        return None, ""
    if _social_links(ev):
        return False, ""
    if not _contact_scraped(ev):
        return None, ""   # nobody looked for them
    return True, "no social profiles"


def _one_social_platform(ev):
    if not ev.site_read:
        return None, ""
    links = _social_links(ev)
    if len(links) > 1:
        return False, ""
    if not _contact_scraped(ev):
        return None, ""   # one is known; nobody looked for others
    if len(links) != 1:
        return False, ""
    column, url = links[0]
    name = column if column != "Other Social" else (_host(url) or "one platform")
    return True, "only on " + name


def _images_missing_alt(ev):
    if not ev.site_read or "images" not in ev.facts:
        return None, ""
    images = int(ev.facts.get("images") or 0)
    missing = int(ev.facts.get("images_missing_alt") or 0)
    if images < 3:
        return False, ""
    return missing * 2 >= images, "{0} of {1} images lack alt text".format(missing, images)


def _outdated_wordpress(ev):
    # As with the copyright year: a read site that exposes no WordPress
    # version shows no sign of an outdated one.
    if not ev.site_read:
        return None, ""
    version = _version_tuple(ev.facts.get("wordpress_version"))
    if not version:
        return False, ""
    return (version < WORDPRESS_OUTDATED_BELOW,
            "WordPress {0} (outdated)".format(".".join(str(p) for p in version[:2])))


def _role_unknown(ev):
    """Role is only ever found by scraping a practice's own site for it. Empty,
    unless such a scrape ran and found nothing, means nobody has looked — not
    "not the owner"."""
    return not ev.role and not (ev.site_read and _contact_scraped(ev))


def _owner_answers_phone(ev):
    if _role_unknown(ev):
        return None, ""
    owner = ev.role in OWNERSHIP_ROLES
    direct = "personal/direct" in str(ev.lead.get("Phone Source") or "")
    return owner and direct, "owner answers their own line"


def _owner_small_practice(ev):
    if _role_unknown(ev):
        return None, ""
    if ev.role not in OWNERSHIP_ROLES:
        return False, ""
    size = ev.number("Practice Size")
    if size and size > SMALL_PRACTICE_MAX:
        return False, ""
    if size == 1:
        who = "{0} · solo practice".format(ev.role)
    elif size:
        who = "{0} · {1} clinicians".format(ev.role, int(size))
    else:
        who = "{0} of the practice".format(ev.role)
    return True, who + " — likely no in-house finance staff"


def _billing_over_100k(ev):
    billing = ev.number("Annual Billing")
    if billing is None:
        return None, ""
    return billing >= BILLING_THRESHOLD, "${0} Medicare billing".format(_short_money(billing))


def _has_staff(ev):
    size = ev.number("Practice Size")
    if size is None:
        return None, ""
    return STAFF_MIN <= size <= STAFF_MAX, "{0} clinicians".format(int(size))


def _rcm_icp(ev):
    if _role_unknown(ev):
        return None, ""
    return is_icp(ev.lead), "owner-operated small practice"


# Registration order is the order the Settings checklist shows them in.
for _check_id, _label, _run in (
        ("no_own_site", "Has no website of their own", _no_own_site),
        ("site_unreachable", "Website is down", _site_unreachable),
        ("ssl_error", "Website has an SSL certificate error", _ssl_error),
        ("no_https", "Website doesn't use HTTPS", _site_lacks("https", "no HTTPS")),
        ("no_mobile_layout", "No mobile layout", _site_lacks("viewport", "no mobile layout")),
        ("dated_copyright", "Copyright year 4+ years old", _dated_copyright),
        ("missing_title_or_description", "Missing page title or meta description",
         _missing_title_or_description),
        ("no_h1", "No H1 heading", _site_lacks("h1", "no H1 heading")),
        ("no_schema_org", "No structured business data (schema.org)",
         _site_lacks("schema_org", "no structured business data")),
        ("no_analytics", "No analytics tag", _site_lacks("analytics", "no analytics tag")),
        ("no_ad_pixel", "No ad tracking pixel", _site_lacks("ad_pixels", "no ad tracking")),
        ("no_social_profiles", "No social profiles linked", _no_social_profiles),
        ("one_social_platform", "Only one social platform", _one_social_platform),
        ("no_newsletter", "No newsletter signup",
         _site_lacks("newsletter", "no newsletter signup")),
        ("no_blog", "No blog or articles", _site_lacks("blog", "no blog")),
        ("no_logo", "No logo", _site_lacks("logo", "no logo")),
        ("no_favicon", "No favicon", _site_lacks("favicon", "no favicon")),
        ("no_share_image", "No social share image",
         _site_lacks("share_image", "no social share image")),
        ("no_video", "No video", _site_lacks("video", "no video")),
        ("no_online_booking", "No online booking",
         _site_lacks("booking", "no online booking")),
        ("no_client_portal", "No client portal", _site_lacks("portal", "no client portal")),
        ("no_contact_form", "No contact form on the homepage",
         _site_lacks("contact_form", "no contact form on the homepage")),
        ("no_live_chat", "No live chat", _site_lacks("live_chat", "no live chat")),
        ("owner_answers_phone", "Owner answers their own line", _owner_answers_phone),
        ("owner_small_practice", "Owner-operated, 5 or fewer clinicians",
         _owner_small_practice),
        ("billing_over_100k", "Medicare billing of $100k or more", _billing_over_100k),
        ("has_staff", "2 to 25 clinicians", _has_staff),
        ("rcm_icp", "Owner-operated small practice (the RCM profile)", _rcm_icp),
        ("no_privacy_policy", "No privacy policy",
         _site_lacks("privacy_policy", "no privacy policy")),
        ("no_hipaa_notice", "No HIPAA Notice of Privacy Practices",
         _site_lacks("hipaa_notice", "no HIPAA notice")),
        ("no_terms", "No terms of use", _site_lacks("terms", "no terms of use")),
        ("no_accessibility_statement", "No accessibility statement",
         _site_lacks("accessibility", "no accessibility statement")),
        ("images_missing_alt", "Most images lack alt text", _images_missing_alt),
        ("outdated_wordpress", "Outdated WordPress version", _outdated_wordpress),
):
    CHECKS[_check_id] = Check(_check_id, _label, _run)


@lru_cache(maxsize=256)
def _phrase_re(phrase):
    words = phrase.lower().split()
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)")


def _mentions(ev, phrases):
    if not ev.text:
        return None, ""
    for phrase in phrases:
        if _phrase_re(phrase).search(ev.text):
            return True, "mentions '{0}'".format(phrase)
    return False, ""


def _not_mentions(ev, phrases):
    if not ev.text:
        return None, ""
    found = any(_phrase_re(phrase).search(ev.text) for phrase in phrases)
    return (not found), "doesn't mention " + " or ".join("'{0}'".format(p) for p in phrases)


def _host(url):
    host = urlparse(url if "//" in url else "//" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _version_tuple(value):
    parts = re.findall(r"\d+", str(value or ""))
    return tuple(int(p) for p in parts[:3]) if parts else None


# ------------------------------------------------------------ lead types

@dataclass
class LeadType:
    id: str
    name: str
    group: str = "Custom"
    description: str = ""
    enabled: bool = True
    builtin: bool = False
    match: str = "any"          # "any" or "all" of the checks and keywords
    checks: list = field(default_factory=list)
    mentions: list = field(default_factory=list)
    not_mentions: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    def copy(self):
        return LeadType(**json.loads(json.dumps(self.to_dict())))


GROUPS = ("Website & Online", "Marketing", "Software & Operations",
          "Finance & Admin", "Legal")

BUILTIN_TYPES = (
    {"id": "website_development", "name": "Website Development", "group": "Website & Online",
     "description": "No website of their own, or one that looks dated.",
     "checks": ["no_own_site", "no_mobile_layout", "dated_copyright", "site_unreachable"]},
    {"id": "seo", "name": "SEO", "group": "Website & Online",
     "description": "Search engines get little to go on: no title, description, "
                    "heading or business data.",
     "checks": ["missing_title_or_description", "no_h1", "no_schema_org"]},
    {"id": "web_accessibility", "name": "Web Accessibility (ADA)", "group": "Website & Online",
     "description": "No accessibility statement, or images without alt text.",
     "checks": ["no_accessibility_statement", "images_missing_alt"]},
    {"id": "it_security", "name": "IT Support & Security", "group": "Website & Online",
     "description": "No HTTPS, an outdated WordPress, or a certificate error.",
     "checks": ["no_https", "outdated_wordpress", "ssl_error"]},
    {"id": "digital_marketing", "name": "Digital Marketing", "group": "Marketing",
     "description": "Not measuring their website traffic.",
     "checks": ["no_analytics"]},
    {"id": "paid_ads", "name": "Paid Ads (PPC)", "group": "Marketing",
     "description": "No ad tracking on their site.",
     "checks": ["no_ad_pixel"]},
    {"id": "social_media", "name": "Social Media", "group": "Marketing",
     "description": "No social profiles, or only one.",
     "checks": ["no_social_profiles", "one_social_platform"]},
    {"id": "email_marketing", "name": "Email Marketing", "group": "Marketing",
     "description": "No newsletter signup.",
     "checks": ["no_newsletter"]},
    {"id": "content_writing", "name": "Content Writing", "group": "Marketing",
     "description": "No blog or articles.",
     "checks": ["no_blog"]},
    {"id": "graphic_design", "name": "Graphic Design & Branding", "group": "Marketing",
     "description": "No logo, favicon or social share image.",
     "checks": ["no_logo", "no_favicon", "no_share_image"]},
    {"id": "video_production", "name": "Video Production", "group": "Marketing",
     "description": "No video on their homepage.",
     "checks": ["no_video"]},
    {"id": "software_development", "name": "Software Development",
     "group": "Software & Operations", "match": "all",
     "description": "No online booking, no client portal and no contact form.",
     "checks": ["no_online_booking", "no_client_portal", "no_contact_form"]},
    {"id": "online_booking", "name": "Online Booking & Scheduling",
     "group": "Software & Operations",
     "description": "Clients can't book online.",
     "checks": ["no_online_booking"]},
    {"id": "live_chat", "name": "Live Chat & Support", "group": "Software & Operations",
     "description": "No chat on their site.",
     "checks": ["no_live_chat"]},
    {"id": "virtual_assistant", "name": "Virtual Assistant / Answering",
     "group": "Software & Operations",
     "description": "Good fit: the owner answers their own phone.",
     "checks": ["owner_answers_phone"]},
    {"id": "bookkeeping", "name": "Bookkeeping", "group": "Finance & Admin",
     "description": "Good fit: an owner-operated small practice.",
     "checks": ["owner_small_practice"]},
    {"id": "accounting_tax", "name": "Accounting & Tax", "group": "Finance & Admin",
     "description": "Good fit: owner-operated, or $100k+ in Medicare billing.",
     "checks": ["owner_small_practice", "billing_over_100k"]},
    {"id": "payroll_hr", "name": "Payroll & HR", "group": "Finance & Admin",
     "description": "Good fit: a practice with staff (2–25 clinicians).",
     "checks": ["has_staff"]},
    {"id": "medical_billing", "name": "Medical Billing (RCM)", "group": "Finance & Admin",
     "description": "Your existing profile: owner-operated, small, reachable.",
     "checks": ["rcm_icp"]},
    {"id": "legal_compliance", "name": "Legal & Compliance", "group": "Legal",
     "description": "No privacy policy, HIPAA notice or terms of use.",
     "checks": ["no_privacy_policy", "no_hipaa_notice", "no_terms"]},
)


def builtin_types():
    """Fresh copies of the built-in catalog, safe to edit."""
    return [LeadType(builtin=True, **json.loads(json.dumps(spec))) for spec in BUILTIN_TYPES]


def evaluate(lead_type, evidence):
    """(status, reasons) for one lead against one type. Reasons are given only
    for a match, and only from the checks that were true."""
    results = []
    for check_id in lead_type.checks:
        check = CHECKS.get(check_id)
        results.append(check.run(evidence) if check else (None, ""))
    if lead_type.mentions:
        results.append(_mentions(evidence, lead_type.mentions))
    if lead_type.not_mentions:
        results.append(_not_mentions(evidence, lead_type.not_mentions))

    values = [value for value, _reason in results]
    if not values:
        return NOT_CHECKED, []
    if lead_type.match == "all":
        if all(value is True for value in values):
            status = MATCH
        elif any(value is False for value in values):
            status = NO_MATCH
        else:
            status = NOT_CHECKED
    else:
        if any(value is True for value in values):
            status = MATCH
        elif all(value is False for value in values):
            status = NO_MATCH
        else:
            status = NOT_CHECKED
    reasons = [reason for value, reason in results if value is True and reason] \
        if status == MATCH else []
    return status, reasons


# -------------------------------------------------------------- selection

class Selection:
    """The lead types chosen right now, ready to apply to leads.

    Built on the Tk thread (or by the CLI) and handed to workers. It holds
    plain data and is never changed after construction, so sharing it across
    threads is safe.
    """

    def __init__(self, types, selected_ids=(), today=None):
        self.types = [t for t in types if t.enabled]
        by_id = {t.id: t for t in self.types}
        self.selected = [by_id[i] for i in dict.fromkeys(selected_ids or ()) if i in by_id]
        self.today = today

    @property
    def active(self):
        return bool(self.selected)

    def judge(self, lead):
        return Judgement(self, Evidence(lead, self.today))

    def label(self):
        names = [t.name for t in self.selected]
        if not names:
            return "All leads"
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " or " + names[-1]

    def tally(self, leads):
        """(matches per enabled type, where the selection leaves each lead) —
        what the Results chart and the Settings table show."""
        per_type = {t.id: 0 for t in self.types}
        overall = {MATCH: 0, NO_MATCH: 0, NOT_CHECKED: 0}
        for lead in leads:
            judgement = self.judge(lead)
            for lead_type in self.types:
                if judgement.status_of(lead_type)[0] == MATCH:
                    per_type[lead_type.id] += 1
            overall[judgement.status] += 1
        return per_type, overall


class Judgement:
    """One lead against the current lead types. Each type is evaluated at
    most once, and only when something asks about it."""

    def __init__(self, selection, evidence):
        self.selection = selection
        self.evidence = evidence
        self._results = {}

    def status_of(self, lead_type):
        if lead_type.id not in self._results:
            self._results[lead_type.id] = evaluate(lead_type, self.evidence)
        return self._results[lead_type.id]

    @property
    def status(self):
        if not self.selection.active:
            return MATCH
        statuses = [self.status_of(t)[0] for t in self.selection.selected]
        if MATCH in statuses:
            return MATCH
        if NOT_CHECKED in statuses:
            return NOT_CHECKED
        return NO_MATCH

    @property
    def admitted(self):
        return self.status == MATCH

    def needs_line(self):
        """"needs: no mobile layout, © 2019" for the selected types it
        matches — the tail of the Call List's Why line."""
        reasons = []
        for lead_type in self.selection.selected:
            status, why = self.status_of(lead_type)
            if status == MATCH:
                reasons.extend(r for r in why if r not in reasons)
        return "needs: " + ", ".join(reasons) if reasons else ""

    def describe(self):
        """Every enabled type this lead matches, selected ones first — the
        Lead Types export column."""
        chosen = {t.id for t in self.selection.selected}
        ordered = self.selection.selected + [t for t in self.selection.types
                                             if t.id not in chosen]
        parts = []
        for lead_type in ordered:
            status, why = self.status_of(lead_type)
            if status == MATCH:
                parts.append(lead_type.name + (": " + ", ".join(why) if why else ""))
        return " · ".join(parts)


# ------------------------------------------------------------ the file

FILE_VERSION = 1


@dataclass
class Loaded:
    types: list
    problem: str = ""     # shown in Settings when the file could not be used


def load(path, logger=None):
    """The built-in catalog, with the user's edits and own types from `path`.

    An unreadable file is renamed to .bad (nothing is lost) and the built-in
    list is used instead, with `problem` saying so. A built-in the file does
    not mention — one a later version added — appears with its defaults.
    """
    logger = logger or logging.getLogger(__name__)
    defaults = builtin_types()
    if not os.path.exists(path):
        return Loaded(defaults)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        entries = data.get("types") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ValueError("it holds no list of lead types")
    except (OSError, ValueError) as e:
        kept = _set_aside(path)
        problem = ("Your lead types file couldn't be read ({0}), so the built-in "
                   "list is in use.".format(e))
        if kept:
            problem += " Your file was kept as {0}.".format(os.path.basename(kept))
        logger.warning(problem)
        return Loaded(defaults, problem)
    return Loaded(_merge(entries, defaults, logger))


def _set_aside(path):
    target = path + ".bad"
    if os.path.exists(target):
        target = "{0}.bad-{1:%Y%m%d-%H%M%S}".format(path, datetime.now())
    try:
        os.replace(path, target)
    except OSError:
        return ""
    return target


def _merge(entries, defaults, logger):
    index = {t.id: i for i, t in enumerate(defaults)}
    result = list(defaults)
    seen, warned = set(), set()
    for raw in entries:
        lead_type = _from_dict(raw, logger, warned)
        if lead_type is None or lead_type.id in seen:
            continue
        seen.add(lead_type.id)
        if lead_type.id in index:
            lead_type.builtin = True
            result[index[lead_type.id]] = lead_type
        else:
            lead_type.builtin = False
            result.append(lead_type)
    return result


def _from_dict(raw, logger, warned):
    if not isinstance(raw, dict):
        logger.warning("Skipped a lead type that isn't an entry: %r", raw)
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        logger.warning("Skipped a lead type with no name.")
        return None
    match = raw.get("match", "any")
    if match not in ("any", "all"):
        logger.warning("Lead type %s says match %r; using 'any'.", name, match)
        match = "any"
    checks = [str(c) for c in _as_list(raw.get("checks"))]
    for check_id in checks:
        if check_id not in CHECKS and check_id not in warned:
            warned.add(check_id)
            logger.warning("Lead type %s uses a check this version doesn't know (%s); "
                           "it reads as couldn't check.", name, check_id)
    mentions = parse_list(raw.get("mentions"))
    not_mentions = parse_list(raw.get("not_mentions"))
    enabled = bool(raw.get("enabled", True))
    if not any(c in CHECKS for c in checks) and not mentions and not not_mentions:
        if enabled:
            logger.warning("Lead type %s has nothing this version can check, so it "
                           "is switched off.", name)
        enabled = False
    return LeadType(
        id=str(raw.get("id") or "").strip() or new_id(name),
        name=name,
        group=str(raw.get("group") or "").strip() or "Custom",
        description=str(raw.get("description") or "").strip(),
        enabled=enabled, builtin=bool(raw.get("builtin")), match=match,
        checks=checks, mentions=mentions, not_mentions=not_mentions)


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return parse_list(value)
    return []


def save(types, path):
    """Write the full list, atomically: a crash mid-save leaves the previous
    file exactly as it was (the same pattern as the Excel export)."""
    data = {"version": FILE_VERSION, "types": [t.to_dict() for t in types]}
    folder = os.path.dirname(os.path.abspath(path)) or "."
    handle, temp_path = tempfile.mkstemp(suffix=".json", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2)
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def reset_builtins(types):
    """Built-ins back to how they shipped; the user's own types kept."""
    return builtin_types() + [t for t in types if not t.builtin]


# --------------------------------------------------------------- helpers

def new_id(name, taken=()):
    base = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_") or "lead_type"
    candidate, n = base, 2
    while candidate in taken:
        candidate = "{0}_{1}".format(base, n)
        n += 1
    return candidate


def copy_name(name, types):
    taken = {t.name.lower() for t in types}
    candidate, n = "Copy of " + name, 2
    while candidate.lower() in taken:
        candidate = "Copy of {0} ({1})".format(name, n)
        n += 1
    return candidate


def parse_list(text):
    """"seo, website development" -> ["seo", "website development"]."""
    if isinstance(text, list):
        return [str(part).strip() for part in text if str(part).strip()]
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def resolve(values, types):
    """(ids, unknown) for values that name a type by ID or by name, ignoring case."""
    by_key = {}
    for lead_type in types:
        by_key[lead_type.id.lower()] = lead_type.id
        by_key[lead_type.name.lower()] = lead_type.id
    ids, unknown = [], []
    for value in values:
        key = str(value).strip().lower()
        if not key:
            continue
        if key in by_key:
            if by_key[key] not in ids:
                ids.append(by_key[key])
        else:
            unknown.append(str(value).strip())
    return ids, unknown


def prune_selection(selected, types, logger=None):
    """The selection without types that no longer exist. A switched-off type
    stays selected — switching it back on restores it — but Selection ignores
    it meanwhile."""
    known = {t.id for t in types}
    kept = []
    for type_id in selected or []:
        if type_id in known:
            if type_id not in kept:
                kept.append(type_id)
        elif logger:
            logger.info("Lead type %s no longer exists, so it was taken out of your "
                        "selection.", type_id)
    return kept


def validate(candidate, others):
    """What stops this type being saved, in words for the edit dialog."""
    problems = []
    name = candidate.name.strip()
    if not name:
        problems.append("Give the lead type a name.")
    elif any(o.name.strip().lower() == name.lower() for o in others):
        problems.append('Another lead type is already called "{0}".'.format(name))
    if not candidate.checks and not candidate.mentions and not candidate.not_mentions:
        problems.append("Pick at least one check, or add a keyword.")
    return problems


def grouped(types):
    """[(group, [types])] in picker order: the built-in groups as GROUPS
    lists them, then any group a user made up, alphabetically."""
    order = {name: index for index, name in enumerate(GROUPS)}
    groups = {}
    for lead_type in types:
        groups.setdefault(lead_type.group or "Custom", []).append(lead_type)
    return sorted(groups.items(), key=lambda item: (order.get(item[0], len(order)),
                                                     item[0].lower()))


def group_names(types):
    return [group for group, _members in grouped(builtin_types() + list(types))]
