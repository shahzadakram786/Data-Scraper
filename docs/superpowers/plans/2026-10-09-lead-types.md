# Lead Types Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user pick the services they sell (Website Development, SEO, Bookkeeping, …) and get back only the medical-practice leads that show evidence of needing them, each with a plain-English reason.

**Architecture:** While a practice's homepage is being read (it already is, for email and phone), `site_audit.py` records plain facts about it on the lead. `lead_types.py` holds the lead-type catalog and decides matches from those facts plus existing columns, **when leads are read** (export, Call List, campaign recipients, Results counts). Matches are never stored, so editing a type changes the next export without re-scraping. The window gets a shared selection (Collect, Results, Outreach), a picker, and a Settings editor in a new `ui_lead_types.py`.

**Tech Stack:** Python 3.12, requests, BeautifulSoup + lxml, SQLite, openpyxl, Tkinter/ttk, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-08-lead-types-design.md` (read it alongside this plan).

## Global Constraints

- Free only: no paid APIs or services, and no new packages. Everything here uses what `requirements.txt` already has.
- Run Python 3.12 explicitly. `python` on PATH is a 3.15 beta with none of the dependencies. Every command below uses:
  `PY="/c/Users/alyak/AppData/Local/Programs/Python/Python312/python.exe"` (Git Bash path; set it once per shell).
- Existing export columns keep their positions. The only export change is `"Lead Types"` appended as the **last** entry of `EXPORT_LAYOUT`.
- Matches are never stored. Only facts are stored, in three internal (never exported) columns: `Site Facts`, `Site Text`, `Site Checked`.
- Every check answers `True`, `False` or `None` ("couldn't check"). `None` is never treated as a match or as a no-match.
- Tk is only touched on the Tk thread. Workers hand results back with `self.post(...)`.
- `app.py` stays front end only. Rules live in `lead_types.py`, evidence in `site_audit.py`, widgets in `ui_lead_types.py`.
- Test fixtures are synthetic, with no real provider data (real PII is already in git history; don't add more).
- User-facing text follows the `ui_help.py` writing rules: say what to do, name things as they appear on screen.
- Commits go on branch `rebuild/lead-platform`, and every message ends with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Two deliberate refinements of the spec, both in the direction the spec's rule points ("never turn couldn't-check into needs-it"):
  1. A new fetch outcome, `blocked` (HTTP 401/403/429 and other non-200, non-404/410/5xx statuses), is transient like `timeout`. Bot protection turning a script away is not a site that's down.
  2. A DNS failure is double-checked against hosts that always resolve. If those fail too, it's this machine's connection and is recorded as `connection` (transient), not `dns`.
- One correction to spec §4.2: `dated_copyright` and `outdated_wordpress` return **False**, not `None`, when the site *was* read but shows no year or no version. The site was looked at and the signal simply isn't there, and re-checking can never change that. `None` would park every non-WordPress site in "not checked" forever.

## Review Focus

1. **The user's own internet drops mid-pass.** Every site then fails DNS, and nothing must be recorded as "site down" (see refinement 2). Pinned by `test_our_own_outage_is_not_blamed_on_the_practice` (Task 7) and the canary tests (Task 1).
2. **Bot protection (Cloudflare 403/429) on a practice site** must read "not checked", never "site down". Pinned by `test_statuses_are_graded` (Task 1) and `test_bot_protection_is_not_a_down_site` (Task 7).
3. **WordPress bundles jQuery as `wp-includes/js/jquery/jquery.min.js?ver=3.7.1`.** Reading that as "WordPress 3.7" would flag nearly every modern WordPress site as outdated. Pinned by `test_bundled_jquery_is_not_the_wordpress_version` (Task 2).
4. **A hand-edited `lead_types.json`** with a typo, a deleted-but-selected type, or a switched-off type passed to the CLI. The app must start, the selection must be pruned, and the CLI must refuse rather than silently export or email everyone. Pinned in Task 4 (`TestTheFile`) and Task 10 (`test_unknown_lead_type_is_refused`, `test_switched_off_lead_type_is_refused`).
5. **The copyright line is in the footer of a long homepage, past the 15,000-character text cap.** It must still be read. A year inserted by JavaScript (no year in the HTML) must read as "not dated", not "not checked". Pinned by `test_the_footer_is_read_before_the_text_is_capped` and `test_a_year_set_by_javascript_is_no_year` (Task 2) and `test_no_year_on_a_read_site_is_not_dated` (Task 3).

---

### Task 1: site_audit plumbing: fetch outcomes, stored text, fact columns

**Files:**
- Create: `site_audit.py`
- Test: `tests/test_site_audit.py`
- Also commit: `docs/superpowers/specs/2026-10-08-lead-types-design.md`, `docs/superpowers/plans/2026-10-09-lead-types.md`

**Interfaces:**
- Consumes: nothing.
- Produces (module `site_audit`):
  - Constants: `SCHEMA_VERSION = 1`, `TEXT_CAP = 15_000`, `FETCH_OK/FETCH_TIMEOUT/FETCH_DNS/FETCH_SSL/FETCH_HTTP_ERROR/FETCH_BLOCKED/FETCH_CONNECTION` (strings `"ok" "timeout" "dns" "ssl" "http_error" "blocked" "connection"`), `TRANSIENT_FETCHES`, `NO_SITE_LISTED = "none_listed"`, `NO_SITE_PROFILE = "profile"`, `WEBSITE_LISTED/WEBSITE_NONE/WEBSITE_UNKNOWN` (`"listed" "none" "unknown"`), `CANARY_HOSTS`.
  - `classify_fetch_error(exc) -> str`, `classify_status(status: int) -> str`, `network_resolves(hosts=CANARY_HOSTS) -> bool`
  - `pack_text(text: str) -> str`, `unpack_text(packed: str) -> str`
  - `dump_facts(facts: dict) -> str`, `load_facts(value: str|dict|None) -> dict`
  - `base_facts(own_site: bool, fetch=None, status=None, today: date=None) -> dict`
  - `no_site_facts(why: str, profile_host="", today=None) -> dict`
  - `failed_audit(fetch: str, exc: Exception, status=None, today=None) -> dict`
  - `columns(facts: dict|None, text="") -> dict` with keys `"Site Facts"`, `"Site Text"`, `"Site Checked"`, or `{}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_site_audit.py`:

```python
"""site_audit: how a homepage fetch went, what the page shows, and how both
are stored on a lead.

The rule the plumbing tests protect: an outcome that says nothing about the
practice — a timeout, bot protection, this machine's own dropped connection —
is never recorded, so it can never be read later as "their site is down".
"""

import json

import pytest
import requests

import site_audit


class TestFetchOutcomes:

    @pytest.mark.parametrize("exc, expected", [
        (requests.exceptions.SSLError("certificate verify failed"), "ssl"),
        (requests.exceptions.ConnectTimeout("timed out"), "timeout"),
        (requests.exceptions.ReadTimeout("read timed out"), "timeout"),
        (requests.exceptions.ConnectionError("[Errno 11001] getaddrinfo failed"), "dns"),
        (requests.exceptions.ConnectionError("Name or service not known"), "dns"),
        (requests.exceptions.ConnectionError("Connection refused"), "connection"),
        (requests.exceptions.TooManyRedirects("loop"), "connection"),
    ])
    def test_exceptions_are_graded(self, exc, expected):
        assert site_audit.classify_fetch_error(exc) == expected

    @pytest.mark.parametrize("status, expected", [
        (200, "ok"), (404, "http_error"), (410, "http_error"), (500, "http_error"),
        (503, "http_error"), (401, "blocked"), (403, "blocked"), (429, "blocked"),
    ])
    def test_statuses_are_graded(self, status, expected):
        assert site_audit.classify_status(status) == expected

    def test_bot_protection_is_never_evidence(self):
        assert site_audit.FETCH_BLOCKED in site_audit.TRANSIENT_FETCHES

    def test_the_canary_reports_a_dead_connection(self, monkeypatch):
        def fail(*_args, **_kwargs):
            raise OSError("no network")
        monkeypatch.setattr(site_audit.socket, "getaddrinfo", fail)
        assert site_audit.network_resolves() is False

    def test_the_canary_reports_a_live_connection(self, monkeypatch):
        monkeypatch.setattr(site_audit.socket, "getaddrinfo",
                            lambda *_a, **_k: [object()])
        assert site_audit.network_resolves() is True


class TestStoredText:

    def test_round_trip(self):
        text = "Telehealth and in-person sessions across Austin."
        assert site_audit.unpack_text(site_audit.pack_text(text)) == text

    def test_empty_stays_empty(self):
        assert site_audit.pack_text("") == ""
        assert site_audit.unpack_text("") == ""

    @pytest.mark.parametrize("damaged", ["not base64 at all!", "aGVsbG8=", "%%%%"])
    def test_damaged_text_reads_as_empty(self, damaged):
        assert site_audit.unpack_text(damaged) == ""


class TestFacts:

    def test_facts_round_trip_through_json(self):
        facts = site_audit.base_facts(True, "ok", 200)
        assert site_audit.load_facts(site_audit.dump_facts(facts)) == facts

    @pytest.mark.parametrize("value", [None, "", "not json", "[1, 2]", 42])
    def test_unreadable_facts_are_empty(self, value):
        assert site_audit.load_facts(value) == {}

    def test_a_dict_is_taken_as_is(self):
        assert site_audit.load_facts({"own_site": False}) == {"own_site": False}

    def test_no_site_facts_name_the_profile(self):
        facts = site_audit.no_site_facts(site_audit.NO_SITE_PROFILE, "growtherapy.com")
        assert facts["own_site"] is False
        assert facts["why"] == "profile"
        assert facts["profile_host"] == "growtherapy.com"

    def test_a_failed_audit_names_the_error(self):
        facts = site_audit.failed_audit("ok", ValueError("bad markup"))
        assert facts["audit_error"] == "ValueError"
        assert facts["own_site"] is True
        assert facts["fetch"] == "ok"


class TestColumns:

    def test_a_read_is_recorded_with_its_date(self):
        facts = site_audit.base_facts(True, "ok", 200)
        cols = site_audit.columns(facts, "hello")
        assert json.loads(cols["Site Facts"]) == facts
        assert site_audit.unpack_text(cols["Site Text"]) == "hello"
        assert cols["Site Checked"] == facts["checked"]

    @pytest.mark.parametrize("fetch", ["timeout", "blocked", "connection"])
    def test_transient_outcomes_are_not_recorded(self, fetch):
        assert site_audit.columns(site_audit.base_facts(True, fetch)) == {}

    @pytest.mark.parametrize("fetch", ["dns", "http_error", "ssl"])
    def test_real_failures_are_recorded(self, fetch):
        assert site_audit.columns(site_audit.base_facts(True, fetch))

    def test_no_facts_records_nothing(self):
        assert site_audit.columns(None) == {}

    def test_no_site_is_recorded(self):
        assert site_audit.columns(site_audit.no_site_facts(site_audit.NO_SITE_LISTED))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_site_audit.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'site_audit'`.

- [ ] **Step 3: Write the module**

Create `site_audit.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_site_audit.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add site_audit.py tests/test_site_audit.py docs/superpowers/specs/2026-10-08-lead-types-design.md docs/superpowers/plans/2026-10-09-lead-types.md
git commit -m "Add site_audit plumbing: grade fetch outcomes and store homepage facts

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: site_audit: read the facts off a homepage

**Files:**
- Modify: `site_audit.py` (append)
- Create: `tests/fixtures/site_modern.html`, `tests/fixtures/site_dated.html`
- Test: `tests/test_site_audit.py` (append)

**Interfaces:**
- Consumes: Task 1's `base_facts`, `failed_audit`, `TEXT_CAP`, `FETCH_OK`.
- Produces:
  - `HTML_CAP = 1_000_000`
  - `read_homepage(html: str, final_url: str, status=200, fetch=FETCH_OK, today: date=None) -> (dict, str)`, which never raises. The fact keys are exactly: `v checked own_site fetch status https viewport copyright_year title meta_description h1 schema_org analytics ad_pixels booking newsletter live_chat portal contact_form blog privacy_policy terms accessibility logo favicon share_image video images images_missing_alt wordpress_version builder hipaa_notice`. List-valued facts: `analytics ad_pixels booking newsletter live_chat`.
  - `newest_copyright_year(text: str, this_year: int) -> int|None`
  - `class EmptyPage(Exception)`

- [ ] **Step 1: Add the two fixtures**

Create `tests/fixtures/site_modern.html` (synthetic, with no real practice):

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cedar Grove Counseling | Therapy in Austin, TX</title>
<meta name="description" content="Individual and couples therapy in Austin, Texas.">
<meta property="og:image" content="https://cedargrove.example/share.jpg">
<link rel="icon" href="/favicon.png">
<script async src="https://www.googletagmanager.com/gtag/js?id=G-ABC123XYZ"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){dataLayer.push(arguments);}
  gtag('js', new Date());
  gtag('config', 'G-ABC123XYZ');
</script>
<script>
!function(f,b,e,v){f.fbq=function(){};}(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '000000000000000');
</script>
<script type="application/ld+json">
{"@context": "https://schema.org", "@graph": [{"@type": "WebSite", "name": "Cedar Grove Counseling"}, {"@type": ["MedicalBusiness", "LocalBusiness"], "name": "Cedar Grove Counseling"}]}
</script>
<script src="https://embed.tawk.to/000000/default" async></script>
</head>
<body>
<header>
  <a class="site-logo" href="/"><img src="/img/cedar-grove-logo.svg" alt="Cedar Grove Counseling"></a>
  <nav>
    <a href="/about">About</a>
    <a href="/blog">Blog</a>
    <a href="/contact">Contact</a>
    <a href="https://cedargrove.clientsecure.me">Client Portal</a>
  </nav>
</header>
<main>
  <h1>Therapy that meets you where you are</h1>
  <p>Telehealth and in-person sessions across Austin. Book online any time.</p>
  <a class="button" href="https://widget-appointment.simplepractice.com/book/cedargrove">Book online</a>
  <iframe src="https://www.youtube.com/embed/abc123" title="Welcome video"></iframe>
  <img src="/img/office.jpg" alt="Our waiting room">
  <img src="/img/team.jpg" alt="The Cedar Grove team">
  <img src="/img/garden.jpg" alt="The garden outside">
  <form class="contact" action="/contact" method="post">
    <input type="text" name="name" placeholder="Your name">
    <input type="email" name="email" placeholder="Email">
    <textarea name="message"></textarea>
    <button type="submit">Send</button>
  </form>
  <form action="https://cedargrove.us1.list-manage.com/subscribe/post" method="post">
    <input type="email" name="EMAIL" placeholder="Join our newsletter">
    <button type="submit">Subscribe</button>
  </form>
</main>
<footer>
  <a href="/privacy-policy">Privacy Policy</a>
  <a href="/notice-of-privacy-practices">Notice of Privacy Practices (HIPAA)</a>
  <a href="/terms-of-use">Terms of Use</a>
  <a href="/accessibility">Accessibility Statement</a>
  <p>&copy; 2025 Cedar Grove Counseling, PLLC</p>
</footer>
</body>
</html>
```

Create `tests/fixtures/site_dated.html`:

```html
<html>
<head>
<title></title>
<meta name="generator" content="WordPress 4.9.8">
<link rel="stylesheet" href="http://oakpractice.example/wp-includes/css/dist/block-library/style.min.css?ver=4.9.8">
</head>
<body>
<table width="800"><tr><td>
<img src="http://oakpractice.example/wp-content/uploads/header.jpg">
<p><b>Oak Street Family Practice</b></p>
<p>Welcome to our practice. Call us at (512) 555-0134 to schedule.</p>
<img src="http://oakpractice.example/wp-content/uploads/doctor.jpg">
<img src="http://oakpractice.example/wp-content/uploads/office.jpg">
<img src="http://oakpractice.example/wp-content/uploads/map.jpg" alt="">
<p><a href="/about-us">About Us</a> | <a href="/services">Services</a> | <a href="/contact-us">Contact</a></p>
<p>Copyright &copy; 2012-2016 Oak Street Family Practice. All rights reserved.</p>
</td></tr></table>
</body>
</html>
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_site_audit.py`:

```python
from datetime import date

from conftest import fixture

TODAY = date(2026, 10, 9)
MODERN_URL = "https://cedargrove.example/"
DATED_URL = "http://oakpractice.example/"

MODERN = {
    "https": True, "viewport": True, "copyright_year": 2025, "title": True,
    "meta_description": True, "h1": True, "schema_org": True,
    "analytics": ["google-analytics"], "ad_pixels": ["meta"],
    "booking": ["simplepractice"], "portal": True, "contact_form": True,
    "newsletter": ["mailchimp"], "blog": True, "privacy_policy": True,
    "hipaa_notice": True, "terms": True, "accessibility": True, "logo": True,
    "favicon": True, "share_image": True, "video": True, "live_chat": ["tawk"],
    "images": 4, "images_missing_alt": 0, "wordpress_version": None, "builder": "",
}
DATED = {
    "https": False, "viewport": False, "copyright_year": 2016, "title": False,
    "meta_description": False, "h1": False, "schema_org": False, "analytics": [],
    "ad_pixels": [], "booking": [], "portal": False, "contact_form": False,
    "newsletter": [], "blog": False, "privacy_policy": False, "hipaa_notice": False,
    "terms": False, "accessibility": False, "logo": False, "favicon": False,
    "share_image": False, "video": False, "live_chat": [], "images": 4,
    "images_missing_alt": 4, "wordpress_version": "4.9.8", "builder": "wordpress",
}


def read(html, url=MODERN_URL, **kwargs):
    return site_audit.read_homepage(html, url, today=TODAY, **kwargs)


class TestReadingAHomepage:

    @pytest.mark.parametrize("key", sorted(MODERN))
    def test_a_modern_site(self, key):
        facts, _text = read(fixture("site_modern.html"), MODERN_URL)
        assert facts[key] == MODERN[key], key

    @pytest.mark.parametrize("key", sorted(DATED))
    def test_a_dated_site(self, key):
        facts, _text = read(fixture("site_dated.html"), DATED_URL)
        assert facts[key] == DATED[key], key

    def test_a_read_site_is_their_own_and_ok(self):
        facts, _text = read(fixture("site_modern.html"))
        assert facts["own_site"] is True
        assert facts["fetch"] == "ok"
        assert facts["v"] == site_audit.SCHEMA_VERSION
        assert facts["checked"] == "2026-10-09"
        assert "audit_error" not in facts

    def test_the_text_is_what_a_visitor_reads(self):
        _facts, text = read(fixture("site_modern.html"))
        assert "Telehealth and in-person sessions" in text
        assert "dataLayer" not in text, "scripts are not visible text"

    def test_a_failed_fetch_records_only_the_outcome(self):
        facts, text = read("", fetch="timeout", status=None)
        assert facts["fetch"] == "timeout"
        assert "viewport" not in facts
        assert text == ""

    def test_an_empty_page_is_not_evidence_of_anything(self):
        facts, _text = read("   ")
        assert facts["audit_error"] == "EmptyPage"

    def test_garbage_does_not_raise(self):
        facts, _text = read("<<<>>> not html <div")
        assert facts["own_site"] is True
        assert "audit_error" not in facts

    def test_a_parser_crash_costs_the_facts_not_the_call(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise RuntimeError("parser exploded")
        monkeypatch.setattr(site_audit, "BeautifulSoup", boom)
        facts, text = read(fixture("site_modern.html"))
        assert facts["audit_error"] == "RuntimeError"
        assert text == ""

    def test_an_enormous_page_is_cut_short(self):
        html = "<html><body><p>" + ("a" * (site_audit.HTML_CAP + 50_000)) + "</p></body></html>"
        facts, text = read(html)
        assert "images" in facts
        assert len(text) == site_audit.TEXT_CAP


class TestCopyright:

    def test_a_range_counts_as_its_last_year(self):
        assert site_audit.newest_copyright_year("© 2012-2016 Oak Street", 2026) == 2016

    def test_the_newest_of_several_wins(self):
        text = "Copyright 2019 Smith Therapy · © 2023 Smith Therapy"
        assert site_audit.newest_copyright_year(text, 2026) == 2023

    def test_a_future_year_is_not_a_date(self):
        assert site_audit.newest_copyright_year("© 2099 Acme", 2026) is None

    def test_a_phone_number_is_not_a_year(self):
        text = "Copyright Smith Therapy (512) 555-2020"
        assert site_audit.newest_copyright_year(text, 2026) is None

    def test_a_year_set_by_javascript_is_no_year(self):
        html = ('<html><body><h1>Hi</h1><footer>© <span id="year"></span> Smith '
                'Therapy<script>document.getElementById("year").textContent='
                'new Date().getFullYear()</script></footer></body></html>')
        facts, _text = read(html)
        assert facts["copyright_year"] is None

    def test_the_footer_is_read_before_the_text_is_capped(self):
        html = ("<html><body><p>" + ("word " * 4000) + "</p>"
                "<footer>© 2017 Long Page Practice</footer></body></html>")
        facts, text = read(html)
        assert facts["copyright_year"] == 2017
        assert len(text) == site_audit.TEXT_CAP


class TestWordPress:

    def test_the_version_from_a_core_asset(self):
        html = ('<html><head><script src="/wp-includes/js/wp-emoji-release.min.js'
                '?ver=5.8.2"></script></head><body><h1>x</h1></body></html>')
        facts, _text = read(html)
        assert facts["wordpress_version"] == "5.8.2"

    def test_bundled_jquery_is_not_the_wordpress_version(self):
        html = ('<html><head><script src="/wp-includes/js/jquery/jquery.min.js'
                '?ver=3.7.1"></script></head><body><h1>x</h1></body></html>')
        facts, _text = read(html)
        assert facts["wordpress_version"] is None

    def test_a_wix_site_is_named(self):
        html = '<html><head><link href="https://static.wixstatic.com/x.css"></head><body></body></html>'
        facts, _text = read(html)
        assert facts["builder"] == "wix"


class TestSignupForms:

    def test_a_plain_signup_form_counts_as_a_newsletter(self):
        html = ('<html><body><form action="/join"><input type="email" '
                'placeholder="Your email"><button>Subscribe</button></form></body></html>')
        facts, _text = read(html)
        assert facts["newsletter"] == ["signup-form"]

    def test_a_message_form_is_not_a_newsletter(self):
        html = ('<html><body><form><input type="email"><textarea></textarea>'
                '<button>Subscribe to updates</button></form></body></html>')
        facts, _text = read(html)
        assert facts["newsletter"] == []
        assert facts["contact_form"] is True
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_site_audit.py -v`
Expected: the new tests FAIL with `AttributeError: module 'site_audit' has no attribute 'read_homepage'`. Task 1's tests still pass.

- [ ] **Step 4: Implement the reader**

In `site_audit.py`, add `import re` and `from urllib.parse import urlparse` to the imports, plus `from bs4 import BeautifulSoup`. The import block becomes:

```python
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
```

Then append to the end of `site_audit.py`:

```python
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

COPYRIGHT_RE = re.compile(
    r"(?:©|\bcopyright\b)[^0-9]{0,30}((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)\d{2}))?",
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
    """The latest year in a copyright line ("© 2012-2016" -> 2016), or None.

    Years after next year are ignored: they are prices or phone digits that
    happen to follow the word, not dates.
    """
    years = [int(m.group(2) or m.group(1)) for m in COPYRIGHT_RE.finditer(text or "")]
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_site_audit.py -v`
Expected: all PASS. If a single fixture key fails, the fixture and `MODERN`/`DATED` must agree: fix whichever reflects the spec's meaning, not whichever is quicker.

- [ ] **Step 6: Commit**

```bash
git add site_audit.py tests/test_site_audit.py tests/fixtures/site_modern.html tests/fixtures/site_dated.html
git commit -m "Read lead-type evidence off a practice's homepage

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: lead_types: checks, evaluation, built-in catalog, Selection

**Files:**
- Create: `lead_types.py`
- Modify: `tests/conftest.py` (add `healthy_site_facts`)
- Test: `tests/test_lead_types.py`

**Interfaces:**
- Consumes: `site_audit.load_facts`, `unpack_text`, `FETCH_*`, `base_facts`, `no_site_facts`, `NO_SITE_*`, `dump_facts`, `pack_text`, `failed_audit`; `storage.OWNERSHIP_ROLES`, `storage.is_icp`, `storage._to_number`, `storage._short_money`.
- Produces (module `lead_types`):
  - `MATCH = "match"`, `NO_MATCH = "no_match"`, `NOT_CHECKED = "not_checked"`
  - `WORDPRESS_OUTDATED_BELOW = (6, 0)`, `DATED_COPYRIGHT_YEARS = 4`, `SMALL_PRACTICE_MAX = 5`, `STAFF_MIN, STAFF_MAX = 2, 25`, `BILLING_THRESHOLD = 100_000`
  - `class Evidence(lead: dict, today: date=None)` with `.facts`, `.site_read`, `.text`, `.role`, `.number(header)`
  - `class Check` (frozen dataclass `id, label, run`), `CHECKS: dict[str, Check]` (insertion order = Settings checklist order)
  - `@dataclass LeadType(id, name, group="Custom", description="", enabled=True, builtin=False, match="any", checks=[], mentions=[], not_mentions=[])` with `.to_dict()`, `.copy()`
  - `GROUPS` (tuple of 5 names), `BUILTIN_TYPES` (tuple of dicts), `builtin_types() -> list[LeadType]`
  - `evaluate(lead_type, evidence) -> (status, reasons: list[str])`
  - `class Selection(types, selected_ids=(), today=None)` with `.types` (enabled only), `.selected`, `.active`, `.judge(lead) -> Judgement`, `.label() -> str`, `.tally(leads) -> (per_type: dict[id,int], overall: dict[status,int])`
  - `class Judgement` with `.status_of(lead_type) -> (status, reasons)`, `.status`, `.admitted`, `.needs_line() -> str`, `.describe() -> str`
- Produces (tests): `conftest.healthy_site_facts(today=None, **overrides) -> dict`.

- [ ] **Step 1: Add the shared test helper**

Append to `tests/conftest.py`:

```python
def healthy_site_facts(today=None, **overrides):
    """Facts for a homepage that was read and lacks nothing any lead type
    looks for. Tests take one thing away and watch exactly one type light up."""
    import site_audit
    facts = site_audit.base_facts(True, site_audit.FETCH_OK, 200, today)
    facts.update({
        "https": True, "viewport": True, "copyright_year": 2025, "title": True,
        "meta_description": True, "h1": True, "schema_org": True,
        "analytics": ["google-analytics"], "ad_pixels": ["meta"],
        "booking": ["simplepractice"], "portal": True, "contact_form": True,
        "newsletter": ["mailchimp"], "blog": True, "privacy_policy": True,
        "hipaa_notice": True, "terms": True, "accessibility": True, "logo": True,
        "favicon": True, "share_image": True, "video": True, "live_chat": ["tawk"],
        "images": 4, "images_missing_alt": 0, "wordpress_version": None,
        "builder": "",
    })
    facts.update(overrides)
    return facts
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_lead_types.py`:

```python
"""Lead types: which practices look like buyers of a service, and why.

Every check answers True, False or None, and None — couldn't check — must
never be read as a match or as a no-match. Most of these tests are about that
line, because crossing it is how a list fills with practices that never needed
the service.
"""

import time
from datetime import date

import pytest

import lead_types
import site_audit
from conftest import healthy_site_facts
from lead_types import MATCH, NO_MATCH, NOT_CHECKED

TODAY = date(2026, 10, 9)
DIRECT = "Provider Website (personal/direct, tel: link)"


def lead(facts=None, text="", **fields):
    """A lead with socials on file, so Social Media stays quiet unless a test
    clears them."""
    row = {"NPI Number": "1234567890", "Facebook": "https://facebook.com/cedar",
           "Instagram": "https://instagram.com/cedar"}
    if facts is not None:
        row["Site Facts"] = site_audit.dump_facts(facts)
    if text:
        row["Site Text"] = site_audit.pack_text(text)
    row.update(fields)
    return row


def healthy(**overrides):
    return lead(healthy_site_facts(TODAY, **overrides))


def no_site(why=site_audit.NO_SITE_LISTED, host=""):
    return lead(site_audit.no_site_facts(why, host, today=TODAY))


def run(check_id, row):
    return lead_types.CHECKS[check_id].run(lead_types.Evidence(row, TODAY))


def builtin(type_id):
    return next(t for t in lead_types.builtin_types() if t.id == type_id)


def judge(row, *selected):
    return lead_types.Selection(lead_types.builtin_types(), selected, today=TODAY).judge(row)


SITE_LACKS = [
    ("no_https", "https", False, "no HTTPS"),
    ("no_mobile_layout", "viewport", False, "no mobile layout"),
    ("no_h1", "h1", False, "no H1 heading"),
    ("no_schema_org", "schema_org", False, "no structured business data"),
    ("no_analytics", "analytics", [], "no analytics tag"),
    ("no_ad_pixel", "ad_pixels", [], "no ad tracking"),
    ("no_newsletter", "newsletter", [], "no newsletter signup"),
    ("no_blog", "blog", False, "no blog"),
    ("no_logo", "logo", False, "no logo"),
    ("no_favicon", "favicon", False, "no favicon"),
    ("no_share_image", "share_image", False, "no social share image"),
    ("no_video", "video", False, "no video"),
    ("no_online_booking", "booking", [], "no online booking"),
    ("no_client_portal", "portal", False, "no client portal"),
    ("no_contact_form", "contact_form", False, "no contact form on the homepage"),
    ("no_live_chat", "live_chat", [], "no live chat"),
    ("no_privacy_policy", "privacy_policy", False, "no privacy policy"),
    ("no_hipaa_notice", "hipaa_notice", False, "no HIPAA notice"),
    ("no_terms", "terms", False, "no terms of use"),
    ("no_accessibility_statement", "accessibility", False, "no accessibility statement"),
]


class TestSiteChecks:

    @pytest.mark.parametrize("check_id, key, lacking, reason", SITE_LACKS)
    def test_true_when_the_site_lacks_it(self, check_id, key, lacking, reason):
        assert run(check_id, healthy(**{key: lacking})) == (True, reason)

    @pytest.mark.parametrize("check_id, key, lacking, reason", SITE_LACKS)
    def test_false_when_the_site_has_it(self, check_id, key, lacking, reason):
        assert run(check_id, healthy())[0] is False

    @pytest.mark.parametrize("check_id, key, lacking, reason", SITE_LACKS)
    @pytest.mark.parametrize("row", [
        lead(),                                                      # never looked
        no_site(),                                                   # no site of their own
        lead(site_audit.base_facts(True, "dns", today=TODAY)),       # site down
        lead(site_audit.failed_audit("ok", ValueError(), today=TODAY)),  # unreadable
    ])
    def test_couldnt_check_without_a_site_that_was_read(self, check_id, key, lacking,
                                                         reason, row):
        assert run(check_id, row)[0] is None

    def test_a_fact_older_records_lack_reads_as_couldnt_check(self):
        facts = healthy_site_facts(TODAY)
        del facts["viewport"]
        assert run("no_mobile_layout", lead(facts))[0] is None


class TestSiteStatus:

    def test_no_website_listed(self):
        assert run("no_own_site", no_site()) == (True, "no website of their own")

    def test_only_a_marketplace_profile(self):
        row = no_site(site_audit.NO_SITE_PROFILE, "growtherapy.com")
        assert run("no_own_site", row) == (True, "only a growtherapy.com profile")

    def test_a_site_of_their_own(self):
        assert run("no_own_site", healthy())[0] is False

    def test_nobody_looked(self):
        assert run("no_own_site", lead())[0] is None

    def test_a_server_error_is_a_site_down(self):
        row = lead(site_audit.base_facts(True, "http_error", 503, TODAY))
        assert run("site_unreachable", row) == (True, "site down (HTTP 503)")

    def test_a_dead_domain_is_a_site_down(self):
        row = lead(site_audit.base_facts(True, "dns", today=TODAY))
        assert run("site_unreachable", row)[0] is True

    def test_a_working_site_is_not_down(self):
        assert run("site_unreachable", healthy())[0] is False

    @pytest.mark.parametrize("fetch", ["timeout", "blocked", "connection", "ssl"])
    def test_other_failures_say_nothing_about_down(self, fetch):
        row = lead(site_audit.base_facts(True, fetch, today=TODAY))
        assert run("site_unreachable", row)[0] is None

    def test_an_ssl_error(self):
        row = lead(site_audit.base_facts(True, "ssl", today=TODAY))
        assert run("ssl_error", row) == (True, "SSL certificate error")
        assert run("ssl_error", healthy())[0] is False

    def test_a_copyright_four_years_old_is_dated(self):
        assert run("dated_copyright", healthy(copyright_year=2022)) == (True, "© 2022")

    def test_a_recent_copyright_is_not(self):
        assert run("dated_copyright", healthy(copyright_year=2023))[0] is False

    def test_no_year_on_a_read_site_is_not_dated(self):
        assert run("dated_copyright", healthy(copyright_year=None))[0] is False

    def test_missing_title_and_description(self):
        assert run("missing_title_or_description", healthy(title=False)) == \
            (True, "no page title")
        assert run("missing_title_or_description",
                   healthy(title=False, meta_description=False)) == \
            (True, "no page title or meta description")
        assert run("missing_title_or_description", healthy())[0] is False

    @pytest.mark.parametrize("images, missing, expected", [
        (4, 2, True), (4, 1, False), (2, 2, False), (0, 0, False)])
    def test_images_missing_alt(self, images, missing, expected):
        value, reason = run("images_missing_alt",
                            healthy(images=images, images_missing_alt=missing))
        assert value is expected
        if expected:
            assert reason == "{0} of {1} images lack alt text".format(missing, images)

    @pytest.mark.parametrize("version, expected", [
        ("5.8.2", True), ("4.9", True), ("6.0", False), ("6.4.2", False),
        (None, False), ("garbage", False)])
    def test_outdated_wordpress(self, version, expected):
        value, reason = run("outdated_wordpress", healthy(wordpress_version=version))
        assert value is expected
        if expected:
            assert reason.startswith("WordPress ") and reason.endswith("(outdated)")

    def test_no_social_profiles(self):
        assert run("no_social_profiles", healthy() | {"Facebook": "", "Instagram": ""}) == \
            (True, "no social profiles")
        assert run("no_social_profiles", healthy())[0] is False
        assert run("no_social_profiles", lead(Facebook="", Instagram=""))[0] is None

    def test_one_social_platform(self):
        row = healthy() | {"Instagram": ""}
        assert run("one_social_platform", row) == (True, "only on Facebook")
        other = healthy() | {"Facebook": "", "Instagram": "",
                             "Other Social": "https://x.com/cedar"}
        assert run("one_social_platform", other) == (True, "only on x.com")
        assert run("one_social_platform", healthy())[0] is False


class TestColumnChecks:

    def test_an_owner_on_their_own_line(self):
        row = healthy() | {"Role": "Owner", "Phone Source": DIRECT}
        assert run("owner_answers_phone", row) == (True, "owner answers their own line")

    def test_an_owner_behind_a_front_desk(self):
        row = healthy() | {"Role": "Owner", "Phone Source": "Provider Website (business line)"}
        assert run("owner_answers_phone", row)[0] is False

    def test_no_role_after_reading_the_site_is_not_the_owner(self):
        assert run("owner_answers_phone", healthy())[0] is False

    def test_no_role_and_no_site_read_is_couldnt_check(self):
        for check_id in ("owner_answers_phone", "owner_small_practice", "rcm_icp"):
            assert run(check_id, lead())[0] is None, check_id

    @pytest.mark.parametrize("size, expected, fragment", [
        ("1", True, "Owner · solo practice"),
        ("3", True, "Owner · 3 clinicians"),
        ("", True, "Owner of the practice"),
        ("12", False, ""),
    ])
    def test_an_owner_small_practice(self, size, expected, fragment):
        row = healthy() | {"Role": "Owner", "Practice Size": size}
        value, reason = run("owner_small_practice", row)
        assert value is expected
        if expected:
            assert reason.startswith(fragment)
            assert reason.endswith("likely no in-house finance staff")

    def test_a_clinical_director_is_not_an_owner(self):
        row = healthy() | {"Role": "Clinical Director", "Practice Size": "1"}
        assert run("owner_small_practice", row)[0] is False

    def test_billing(self):
        assert run("billing_over_100k", lead(**{"Annual Billing": "230570"})) == \
            (True, "$231k Medicare billing")
        assert run("billing_over_100k", lead(**{"Annual Billing": "50000"}))[0] is False
        assert run("billing_over_100k", lead())[0] is None

    def test_staff(self):
        assert run("has_staff", lead(**{"Practice Size": "14"})) == (True, "14 clinicians")
        assert run("has_staff", lead(**{"Practice Size": "1"}))[0] is False
        assert run("has_staff", lead(**{"Practice Size": "40"}))[0] is False
        assert run("has_staff", lead())[0] is None

    def test_the_rcm_profile(self):
        assert run("rcm_icp", healthy() | {"Role": "Owner", "Phone": "(512) 555-0100"}) == \
            (True, "owner-operated small practice")
        assert run("rcm_icp", healthy() | {"Role": "Clinical Director",
                                           "Phone": "(512) 555-0100"})[0] is False


def custom(**fields):
    base = {"id": "custom", "name": "Custom", "group": "Custom"}
    base.update(fields)
    return lead_types.LeadType(**base)


class TestKeywords:

    def test_a_phrase_the_homepage_mentions(self):
        row = lead(healthy_site_facts(TODAY), text="We offer telehealth across Texas.")
        status, reasons = lead_types.evaluate(custom(mentions=["telehealth"]),
                                              lead_types.Evidence(row, TODAY))
        assert (status, reasons) == (MATCH, ["mentions 'telehealth'"])

    def test_a_phrase_the_homepage_does_not_mention(self):
        row = lead(healthy_site_facts(TODAY), text="Call us to schedule.")
        status, reasons = lead_types.evaluate(custom(not_mentions=["book online"]),
                                              lead_types.Evidence(row, TODAY))
        assert (status, reasons) == (MATCH, ["doesn't mention 'book online'"])

    def test_case_and_spacing_do_not_matter(self):
        row = lead(healthy_site_facts(TODAY), text="You can Book   Online today.")
        status, _ = lead_types.evaluate(custom(not_mentions=["book online"]),
                                        lead_types.Evidence(row, TODAY))
        assert status == NO_MATCH

    def test_whole_phrases_only(self):
        row = lead(healthy_site_facts(TODAY), text="Read our free ebook online.")
        status, _ = lead_types.evaluate(custom(not_mentions=["book online"]),
                                        lead_types.Evidence(row, TODAY))
        assert status == MATCH, "'ebook online' is not 'book online'"

    def test_no_stored_text_is_couldnt_check(self):
        status, _ = lead_types.evaluate(custom(mentions=["telehealth"]),
                                        lead_types.Evidence(healthy(), TODAY))
        assert status == NOT_CHECKED


class TestEvaluation:

    def evaluate(self, lead_type, row):
        return lead_types.evaluate(lead_type, lead_types.Evidence(row, TODAY))

    def test_any_matches_on_one_true_and_reports_only_true_reasons(self):
        lead_type = custom(checks=["no_mobile_layout", "no_h1"])
        assert self.evaluate(lead_type, healthy(viewport=False)) == \
            (MATCH, ["no mobile layout"])

    def test_any_is_no_match_only_when_everything_is_false(self):
        assert self.evaluate(custom(checks=["no_mobile_layout", "no_h1"]), healthy())[0] == NO_MATCH

    def test_any_with_false_and_couldnt_check_is_not_checked(self):
        lead_type = custom(checks=["no_mobile_layout", "has_staff"])
        assert self.evaluate(lead_type, healthy())[0] == NOT_CHECKED

    def test_all_needs_every_check_true(self):
        lead_type = custom(match="all", checks=["no_mobile_layout", "no_h1"])
        assert self.evaluate(lead_type, healthy(viewport=False, h1=False))[0] == MATCH
        assert self.evaluate(lead_type, healthy(viewport=False))[0] == NO_MATCH

    def test_all_with_true_and_couldnt_check_is_not_checked(self):
        lead_type = custom(match="all", checks=["no_mobile_layout", "has_staff"])
        assert self.evaluate(lead_type, healthy(viewport=False))[0] == NOT_CHECKED

    def test_an_unknown_check_is_couldnt_check(self):
        assert self.evaluate(custom(checks=["no_fax_machine"]), healthy())[0] == NOT_CHECKED

    def test_a_type_with_nothing_to_check_is_not_checked(self):
        assert self.evaluate(custom(), healthy())[0] == NOT_CHECKED


BUILTIN_CASES = [
    ("website_development", no_site(), MATCH),
    ("website_development", healthy(), NO_MATCH),
    ("seo", healthy(meta_description=False), MATCH),
    ("seo", healthy(), NO_MATCH),
    ("web_accessibility", healthy(accessibility=False), MATCH),
    ("web_accessibility", healthy(), NO_MATCH),
    ("it_security", healthy(https=False), MATCH),
    ("it_security", healthy(), NO_MATCH),
    ("digital_marketing", healthy(analytics=[]), MATCH),
    ("paid_ads", healthy(ad_pixels=[]), MATCH),
    ("social_media", healthy() | {"Facebook": "", "Instagram": ""}, MATCH),
    ("social_media", healthy(), NO_MATCH),
    ("email_marketing", healthy(newsletter=[]), MATCH),
    ("content_writing", healthy(blog=False), MATCH),
    ("graphic_design", healthy(favicon=False), MATCH),
    ("video_production", healthy(video=False), MATCH),
    ("software_development", healthy(booking=[], portal=False, contact_form=False), MATCH),
    ("software_development", healthy(booking=[]), NO_MATCH),
    ("online_booking", healthy(booking=[]), MATCH),
    ("live_chat", healthy(live_chat=[]), MATCH),
    ("virtual_assistant", healthy() | {"Role": "Owner", "Phone Source": DIRECT}, MATCH),
    ("virtual_assistant", healthy() | {"Role": "Owner"}, NO_MATCH),
    ("bookkeeping", healthy() | {"Role": "Owner", "Practice Size": "1"}, MATCH),
    ("bookkeeping", healthy() | {"Role": "Clinical Director"}, NO_MATCH),
    ("accounting_tax", healthy() | {"Annual Billing": "230570"}, MATCH),
    ("payroll_hr", healthy() | {"Practice Size": "14"}, MATCH),
    ("payroll_hr", healthy(), NOT_CHECKED),
    ("medical_billing", healthy() | {"Role": "Owner", "Phone": "(512) 555-0100"}, MATCH),
    ("legal_compliance", healthy(terms=False), MATCH),
    ("legal_compliance", healthy(), NO_MATCH),
]


class TestBuiltins:

    def test_twenty_types_in_five_groups(self):
        types = lead_types.builtin_types()
        assert len(types) == 20
        assert {t.group for t in types} == set(lead_types.GROUPS)

    def test_ids_are_unique(self):
        ids = [t.id for t in lead_types.builtin_types()]
        assert len(ids) == len(set(ids))

    def test_every_check_they_use_exists(self):
        for lead_type in lead_types.builtin_types():
            for check_id in lead_type.checks:
                assert check_id in lead_types.CHECKS, (lead_type.id, check_id)

    def test_they_are_marked_builtin_and_on(self):
        assert all(t.builtin and t.enabled for t in lead_types.builtin_types())

    def test_software_development_needs_all_three(self):
        assert builtin("software_development").match == "all"

    @pytest.mark.parametrize("type_id, row, expected", BUILTIN_CASES)
    def test_each_type(self, type_id, row, expected):
        status, _reasons = lead_types.evaluate(builtin(type_id),
                                               lead_types.Evidence(row, TODAY))
        assert status == expected


class TestSelection:

    def test_nothing_selected_admits_everything(self):
        judgement = judge(lead())
        assert judgement.admitted
        assert lead_types.Selection(lead_types.builtin_types()).label() == "All leads"

    def test_a_lead_matching_any_selected_type_is_admitted(self):
        judgement = judge(healthy(viewport=False), "seo", "website_development")
        assert judgement.status == MATCH and judgement.admitted

    def test_a_lead_nobody_looked_at_is_not_checked(self):
        judgement = judge(lead(), "website_development")
        assert judgement.status == NOT_CHECKED
        assert not judgement.admitted

    def test_a_lead_that_needs_none_of_them(self):
        assert judge(healthy(), "website_development").status == NO_MATCH

    def test_the_needs_line_lists_each_reason_once(self):
        judgement = judge(healthy(https=False, viewport=False),
                          "website_development", "it_security")
        assert judgement.needs_line() == "needs: no mobile layout, no HTTPS"

    def test_no_match_means_no_needs_line(self):
        assert judge(healthy(), "website_development").needs_line() == ""

    def test_describe_lists_every_matching_type_selected_first(self):
        judgement = judge(healthy(meta_description=False, viewport=False), "seo")
        described = judgement.describe()
        assert described.startswith("SEO: no meta description")
        assert "Website Development: no mobile layout" in described

    def test_describe_is_empty_when_nothing_matches(self):
        assert judge(healthy()).describe() == ""

    def test_switched_off_types_are_ignored(self):
        types = lead_types.builtin_types()
        for lead_type in types:
            if lead_type.id == "seo":
                lead_type.enabled = False
        selection = lead_types.Selection(types, ["seo"])
        assert not selection.active
        assert "seo" not in {t.id for t in selection.types}

    def test_a_selected_type_counts_once(self):
        selection = lead_types.Selection(lead_types.builtin_types(), ["seo", "seo"])
        assert [t.id for t in selection.selected] == ["seo"]

    @pytest.mark.parametrize("selected, label", [
        (["seo"], "SEO"),
        (["website_development", "seo"], "Website Development or SEO"),
        (["website_development", "seo", "bookkeeping"],
         "Website Development, SEO or Bookkeeping"),
    ])
    def test_the_label_reads_as_a_phrase(self, selected, label):
        assert lead_types.Selection(lead_types.builtin_types(), selected).label() == label

    def test_tally(self):
        rows = [no_site(), healthy(viewport=False), healthy(), lead()]
        selection = lead_types.Selection(lead_types.builtin_types(),
                                         ["website_development"], today=TODAY)
        per_type, overall = selection.tally(rows)
        assert per_type["website_development"] == 2
        assert overall == {MATCH: 2, NO_MATCH: 1, NOT_CHECKED: 1}


class TestSpeed:

    ROWS = [healthy(viewport=(i % 2 == 0)) for i in range(10_000)]

    def test_filtering_ten_thousand_leads_takes_under_a_second(self):
        selection = lead_types.Selection(lead_types.builtin_types(),
                                         ["website_development", "seo"], today=TODAY)
        started = time.perf_counter()
        kept = [row for row in self.ROWS if selection.judge(row).admitted]
        elapsed = time.perf_counter() - started
        assert len(kept) == 5_000
        assert elapsed < 1.0, "filtering took {0:.2f}s".format(elapsed)

    def test_counting_every_type_stays_reasonable(self):
        selection = lead_types.Selection(lead_types.builtin_types(), today=TODAY)
        started = time.perf_counter()
        per_type, _overall = selection.tally(self.ROWS)
        elapsed = time.perf_counter() - started
        assert per_type["website_development"] == 5_000
        assert elapsed < 5.0, "counting took {0:.2f}s".format(elapsed)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_lead_types.py -v`
Expected: collection error, `ModuleNotFoundError: No module named 'lead_types'`.

- [ ] **Step 4: Write the module**

Create `lead_types.py`:

```python
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

import re
import json
from functools import lru_cache
from dataclasses import dataclass, field, asdict
from datetime import date
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


def _no_social_profiles(ev):
    if not ev.site_read:
        return None, ""
    return (not _social_links(ev)), "no social profiles"


def _one_social_platform(ev):
    if not ev.site_read:
        return None, ""
    links = _social_links(ev)
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
    """Role is only ever found on a practice's own site. Empty with no site
    read means nobody has looked — not "not the owner"."""
    return not ev.role and not ev.site_read


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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_lead_types.py -v`
Expected: all PASS. If `TestSpeed` fails on time only, profile before loosening the budget: `Evidence` must parse the facts once per lead, and `Judgement` must not evaluate types nobody asked about.

- [ ] **Step 6: Commit**

```bash
git add lead_types.py tests/test_lead_types.py tests/conftest.py
git commit -m "Add lead types: checks, the built-in catalog, and matching

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: lead_types: the user's file, and where it lives

**Files:**
- Modify: `lead_types.py` (append), `config.py`, `.gitignore`
- Test: `tests/test_lead_types.py` (append)

**Interfaces:**
- Consumes: Task 3's `LeadType`, `CHECKS`, `GROUPS`, `builtin_types`.
- Produces:
  - `lead_types.FILE_VERSION = 1`
  - `@dataclass Loaded(types: list, problem: str = "")`
  - `load(path, logger=None) -> Loaded`, `save(types, path) -> None`, `reset_builtins(types) -> list`
  - `new_id(name, taken=()) -> str`, `copy_name(name, types) -> str`
  - `parse_list(text) -> list[str]`, `resolve(values, types) -> (ids, unknown)`
  - `prune_selection(selected, types, logger=None) -> list[str]`
  - `validate(candidate, others) -> list[str]`
  - `grouped(types) -> list[(group, [LeadType])]`, `group_names(types) -> list[str]`
  - `config.LEAD_TYPES_FILE = "lead_types.json"`, `config.lead_types_path(config_path=None) -> str`, `config.DEFAULTS["lead_types_selected"] = []`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lead_types.py`:

```python
import json as _json
import logging
import os

import config


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def recorder():
    logger = logging.getLogger("test.lead_types")
    handler = RecordingHandler()
    logger.addHandler(handler)
    logger.propagate = False
    yield logger, handler.messages
    logger.removeHandler(handler)


def write(path, data):
    path.write_text(data if isinstance(data, str) else _json.dumps(data), encoding="utf-8")


class TestTheFile:

    def test_no_file_means_the_builtins(self, tmp_path):
        loaded = lead_types.load(str(tmp_path / "lead_types.json"))
        assert [t.id for t in loaded.types] == [t.id for t in lead_types.builtin_types()]
        assert loaded.problem == ""

    def test_edits_and_custom_types_round_trip(self, tmp_path):
        path = str(tmp_path / "lead_types.json")
        types = lead_types.builtin_types()
        types[1].name = "Search Optimisation"
        types[1].enabled = False
        types.append(lead_types.LeadType(id="telehealth", name="Telehealth",
                                         mentions=["telehealth"]))
        lead_types.save(types, path)
        loaded = lead_types.load(path).types
        seo = next(t for t in loaded if t.id == "seo")
        assert seo.name == "Search Optimisation" and seo.enabled is False and seo.builtin
        custom_type = loaded[-1]
        assert (custom_type.id, custom_type.builtin, custom_type.mentions) == \
            ("telehealth", False, ["telehealth"])

    def test_builtins_missing_from_the_file_still_appear(self, tmp_path):
        path = tmp_path / "lead_types.json"
        write(path, {"version": 1, "types": [
            {"id": "seo", "name": "SEO+", "group": "Website & Online", "checks": ["no_h1"]}]})
        loaded = lead_types.load(str(path)).types
        assert len(loaded) == 20
        assert next(t for t in loaded if t.id == "seo").name == "SEO+"

    @pytest.mark.parametrize("content", ["{not json", '{"types": "nope"}', "[1, 2, 3]"])
    def test_an_unreadable_file_is_set_aside(self, tmp_path, content):
        path = tmp_path / "lead_types.json"
        write(path, content)
        loaded = lead_types.load(str(path))
        assert len(loaded.types) == 20
        assert "lead_types.json.bad" in loaded.problem
        assert not path.exists()
        assert (tmp_path / "lead_types.json.bad").read_text(encoding="utf-8") == content

    def test_an_older_bad_copy_is_kept(self, tmp_path):
        (tmp_path / "lead_types.json.bad").write_text("first", encoding="utf-8")
        path = tmp_path / "lead_types.json"
        write(path, "{second")
        lead_types.load(str(path))
        assert (tmp_path / "lead_types.json.bad").read_text(encoding="utf-8") == "first"
        assert len(list(tmp_path.glob("lead_types.json.bad-*"))) == 1

    def test_bad_entries_are_skipped_and_good_ones_kept(self, tmp_path, recorder):
        logger, messages = recorder
        path = tmp_path / "lead_types.json"
        write(path, {"types": [42, {"name": ""},
                               {"name": "Telehealth", "mentions": ["telehealth"]}]})
        loaded = lead_types.load(str(path), logger).types
        assert loaded[-1].name == "Telehealth"
        assert loaded[-1].id == "telehealth"
        assert len(messages) == 2

    def test_an_unknown_check_warns_once(self, tmp_path, recorder):
        logger, messages = recorder
        path = tmp_path / "lead_types.json"
        write(path, {"types": [
            {"id": "a", "name": "A", "checks": ["no_fax_machine", "no_h1"]},
            {"id": "b", "name": "B", "checks": ["no_fax_machine", "no_h1"]}]})
        loaded = lead_types.load(str(path), logger).types
        assert sum("no_fax_machine" in m for m in messages) == 1
        assert loaded[-1].checks == ["no_fax_machine", "no_h1"], "kept, so it round-trips"

    def test_an_invalid_match_becomes_any(self, tmp_path):
        path = tmp_path / "lead_types.json"
        write(path, {"types": [{"id": "a", "name": "A", "match": "most", "checks": ["no_h1"]}]})
        assert lead_types.load(str(path)).types[-1].match == "any"

    def test_a_type_with_nothing_runnable_is_switched_off(self, tmp_path):
        path = tmp_path / "lead_types.json"
        write(path, {"types": [{"id": "a", "name": "A", "checks": ["no_fax_machine"]}]})
        assert lead_types.load(str(path)).types[-1].enabled is False

    def test_saving_is_atomic(self, tmp_path, monkeypatch):
        path = tmp_path / "lead_types.json"
        lead_types.save(lead_types.builtin_types(), str(path))
        before = path.read_text(encoding="utf-8")

        def boom(*_args, **_kwargs):
            raise OSError("disk full")
        monkeypatch.setattr(lead_types.json, "dump", boom)
        with pytest.raises(OSError):
            lead_types.save([], str(path))
        assert path.read_text(encoding="utf-8") == before
        assert [p.name for p in tmp_path.iterdir()] == ["lead_types.json"]

    def test_reset_restores_builtins_and_keeps_custom_types(self):
        types = lead_types.builtin_types()
        types[0].name = "Websites"
        types.append(lead_types.LeadType(id="telehealth", name="Telehealth",
                                         mentions=["telehealth"]))
        reset = lead_types.reset_builtins(types)
        assert reset[0].name == "Website Development"
        assert reset[-1].id == "telehealth"


class TestHelpers:

    def test_new_ids_are_slugs_and_unique(self):
        assert lead_types.new_id("Telehealth Setup!") == "telehealth_setup"
        assert lead_types.new_id("Telehealth", {"telehealth"}) == "telehealth_2"
        assert lead_types.new_id("!!!") == "lead_type"

    def test_copy_names_are_unique(self):
        types = lead_types.builtin_types()
        assert lead_types.copy_name("SEO", types) == "Copy of SEO"
        types.append(lead_types.LeadType(id="x", name="Copy of SEO"))
        assert lead_types.copy_name("SEO", types) == "Copy of SEO (2)"

    def test_parse_list(self):
        assert lead_types.parse_list(" seo, ,Website Development ") == \
            ["seo", "Website Development"]
        assert lead_types.parse_list(None) == []

    def test_resolve_by_id_or_name(self):
        types = lead_types.builtin_types()
        ids, unknown = lead_types.resolve(["SEO", "website development", "seo", "nope"], types)
        assert ids == ["seo", "website_development"]
        assert unknown == ["nope"]

    def test_prune_selection_drops_deleted_types(self, recorder):
        logger, messages = recorder
        kept = lead_types.prune_selection(["seo", "gone", "seo"],
                                          lead_types.builtin_types(), logger)
        assert kept == ["seo"]
        assert any("gone" in m for m in messages)

    def test_validate(self):
        others = lead_types.builtin_types()
        assert lead_types.validate(lead_types.LeadType(id="x", name="", checks=["no_h1"]),
                                   others) == ["Give the lead type a name."]
        assert lead_types.validate(lead_types.LeadType(id="x", name="seo", checks=["no_h1"]),
                                   others) == ['Another lead type is already called "seo".']
        assert lead_types.validate(lead_types.LeadType(id="x", name="New"), others) == \
            ["Pick at least one check, or add a keyword."]
        assert lead_types.validate(lead_types.LeadType(id="x", name="New",
                                                       mentions=["telehealth"]), others) == []

    def test_renaming_keeps_its_own_name_valid(self):
        types = lead_types.builtin_types()
        seo = types[1].copy()
        assert lead_types.validate(seo, [t for t in types if t.id != "seo"]) == []

    def test_grouped_follows_the_picker_order(self):
        types = lead_types.builtin_types() + [
            lead_types.LeadType(id="z", name="Z", group="Zebra", checks=["no_h1"]),
            lead_types.LeadType(id="a", name="A", group="Apple", checks=["no_h1"])]
        groups = [group for group, _members in lead_types.grouped(types)]
        assert groups == list(lead_types.GROUPS) + ["Apple", "Zebra"]

    def test_group_names_always_offer_the_builtin_groups(self):
        assert lead_types.group_names([])[:5] == list(lead_types.GROUPS)


class TestConfig:

    def test_the_file_sits_beside_config_json(self, tmp_path):
        assert config.lead_types_path(str(tmp_path / "config.json")) == \
            os.path.join(str(tmp_path), "lead_types.json")

    def test_the_default_follows_the_config_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "config.json"))
        assert config.lead_types_path() == os.path.join(str(tmp_path), "lead_types.json")

    def test_the_selection_is_a_saved_setting(self, tmp_path):
        path = str(tmp_path / "config.json")
        config.save(dict(config.DEFAULTS, lead_types_selected=["seo"]), path)
        assert config.load(path)["lead_types_selected"] == ["seo"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_lead_types.py -v`
Expected: the new tests FAIL with `AttributeError` (no `load`, `lead_types_path`, …). Task 3's tests still pass.

- [ ] **Step 3: Implement**

Change `lead_types.py`'s imports to:

```python
import os
import re
import json
import logging
import tempfile
from functools import lru_cache
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from urllib.parse import urlparse
```

Append to `lead_types.py`:

```python
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
```

In `config.py`, after `CONFIG_FILE = "config.json"` add:

```python
# The lead-type definitions (see lead_types.py), kept beside config.json.
LEAD_TYPES_FILE = "lead_types.json"
```

In `DEFAULTS`, after `"min_updated_year": 2010,` add:

```python

    # Lead types: the IDs chosen in the window, shared by Collect, Results and
    # Outreach. The definitions live in lead_types.json beside this file.
    "lead_types_selected": [],
```

And add this function after `save`:

```python
def lead_types_path(config_path=None):
    """lead_types.json, beside config.json. Read CONFIG_FILE at call time, so
    a test or the CLI pointing config elsewhere moves this with it."""
    folder = os.path.dirname(os.path.abspath(config_path or CONFIG_FILE))
    return os.path.join(folder, LEAD_TYPES_FILE)
```

In `.gitignore`, under `# Local settings: proxy, Google service-account path, worker counts.` / `config.json`, add:

```
# The user's lead-type definitions, and copies set aside when unreadable.
lead_types.json
lead_types.json.bad*
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_lead_types.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lead_types.py config.py .gitignore tests/test_lead_types.py
git commit -m "Keep the user's lead types in lead_types.json, and survive a broken one

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: storage: site columns, new filters, lead-type filtering

**Files:**
- Modify: `storage.py`
- Modify: `tests/test_performance.py:86-100` (pending-count dicts)
- Test: `tests/test_lead_type_storage.py`

**Interfaces:**
- Consumes: `lead_types.Selection` by duck type only (`.active`, `.judge(lead)` → `.admitted`, `.needs_line()`). Storage must **not** import `lead_types`, which imports storage.
- Produces:
  - `INTERNAL_COLUMNS` gains `("Site Facts","site_facts")`, `("Site Text","site_text")`, `("Site Checked","site_checked")`
  - `EXPORT_LAYOUT[-1] == "Lead Types"`
  - `LeadStore.iter_leads(..., needs_site_check=False, created_since=None)`, and the same keys for `count(...)`
  - `LeadStore.PENDING_FILTERS` ends with `"needs_site_check"`, so `pending_counts()` has that key
  - `LeadStore.iter_best_leads(..., lead_types=None)` / `best_leads(..., lead_types=...)`
  - `storage.utcnow() -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lead_type_storage.py`:

```python
"""Storage for lead types: the facts are kept, never exported, and the only
change to the spreadsheet is one column on the end."""

import sqlite3
from datetime import date

import lead_types
import site_audit
import storage
from conftest import healthy_site_facts
from storage import EXPORT_LAYOUT, LeadStore

TODAY = date(2026, 10, 9)


def lead(npi, phone, **fields):
    base = {"NPI Number": npi, "Provider First Name": "Jane", "Provider Last Name": "Smith",
            "State": "TX", "Source": "Psychology Today", "Phone": phone,
            "Facebook": "https://facebook.com/x", "Instagram": "https://instagram.com/x"}
    base.update(fields)
    return base


def facts(value):
    return site_audit.dump_facts(value)


NO_SITE = facts(site_audit.no_site_facts(site_audit.NO_SITE_LISTED, today=TODAY))
HEALTHY = facts(healthy_site_facts(TODAY))


class TestColumns:

    def test_lead_types_is_the_only_new_export_column(self):
        assert EXPORT_LAYOUT[-1] == "Lead Types"
        assert EXPORT_LAYOUT[-2] == "First Seen"
        for internal in ("Site Facts", "Site Text", "Site Checked"):
            assert internal not in EXPORT_LAYOUT

    def test_site_columns_are_stored(self, store):
        store.add_leads_batch([lead("1111111111", "(512) 471-3301", **{
            "Site Facts": NO_SITE, "Site Checked": "2026-10-09"})])
        saved = next(store.iter_leads())
        assert saved["Site Facts"] == NO_SITE
        assert saved["Site Checked"] == "2026-10-09"

    def test_an_older_database_gains_the_site_columns(self, tmp_path, quiet_logger):
        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT, sheet_key TEXT NOT NULL,
            email_key TEXT, phone_key TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, npi TEXT, state TEXT, source TEXT,
            email TEXT, email_status TEXT)""")
        conn.commit()
        conn.close()
        LeadStore(path, logger=quiet_logger)
        conn = sqlite3.connect(path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(leads)")}
        conn.close()
        assert {"site_facts", "site_text", "site_checked"} <= columns


class TestFilters:

    def test_needs_site_check_is_websites_nobody_has_read(self, store):
        store.add_leads_batch([
            lead("1111111111", "(512) 471-3301", Website="https://a.example"),
            lead("2222222222", "(512) 471-3302", Website="https://b.example",
                 **{"Site Checked": "2026-10-01", "Site Facts": HEALTHY}),
            lead("3333333333", "(512) 471-3303"),
        ])
        assert store.count(needs_site_check=True) == 1
        assert store.pending_counts()["needs_site_check"] == 1
        assert [l["Website"] for l in store.iter_leads(needs_site_check=True)] == \
            ["https://a.example"]

    def test_created_since(self, store):
        store.add_leads_batch([lead("1111111111", "(512) 471-3301")])
        with store._lock:
            conn = store._conn()
            conn.execute("UPDATE leads SET created_at = '2026-01-01T00:00:00+00:00'")
            conn.commit()
        since = storage.utcnow()
        store.add_leads_batch([lead("2222222222", "(512) 471-3302")])
        assert [l["NPI Number"] for l in store.iter_leads(created_since=since)] == \
            ["2222222222"]

    def test_utcnow_is_the_stored_format(self):
        assert storage.utcnow()[10] == "T" and storage.utcnow().endswith("+00:00")


class TestBestLeadsByLeadType:

    def seed(self, store):
        store.add_leads_batch([
            lead("1111111111", "(512) 471-3301", **{"Site Facts": NO_SITE}),
            lead("2222222222", "(512) 471-3302", **{"Site Facts": HEALTHY}),
            lead("3333333333", "(512) 471-3303"),
        ])

    def test_only_leads_that_need_it_come_back(self, store):
        self.seed(store)
        selection = lead_types.Selection(lead_types.builtin_types(),
                                         ["website_development"], today=TODAY)
        chosen = store.best_leads(limit=0, lead_types=selection)
        assert [l["NPI Number"] for l in chosen] == ["1111111111"]
        assert "needs: no website of their own" in chosen[0]["Why"]

    def test_nothing_selected_filters_nothing(self, store):
        self.seed(store)
        selection = lead_types.Selection(lead_types.builtin_types(), today=TODAY)
        assert len(store.best_leads(limit=0, lead_types=selection)) == 3

    def test_no_selection_object_filters_nothing(self, store):
        self.seed(store)
        assert len(store.best_leads(limit=0)) == 3
```

In `tests/test_performance.py`, update the two exact dicts:

```python
    def test_the_seeded_backlogs_are_what_they_should_be(self, store):
        seed(store)
        assert store.pending_counts() == {
            "missing_email": 2,   # 3333333333 and the placeholder
            "unverified": 1,      # b@x.example
            "needs_npi": 4,       # all but the one already matched
            "needs_cms": 3,       # real NPIs not yet looked up; not the placeholder
            "needs_site_check": 0,  # none of the seeded leads has a website
        }

    def test_an_empty_database_has_nothing_pending(self, store):
        assert store.pending_counts() == {
            "missing_email": 0, "unverified": 0, "needs_npi": 0, "needs_cms": 0,
            "needs_site_check": 0}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_lead_type_storage.py tests/test_performance.py -v`
Expected: FAIL. `EXPORT_LAYOUT[-1]` is `"First Seen"`, and there's no `needs_site_check`, `created_since`, `utcnow` or `lead_types=` parameter.

- [ ] **Step 3: Implement**

In `storage.py`:

1. Replace `INTERNAL_COLUMNS` with:

```python
# Kept in the database, never exported.
INTERNAL_COLUMNS = (
    # When the NPI details pass last looked this lead up. Gender and NPI Phone
    # are legitimately blank for many leads, so an empty value cannot be what
    # means "not checked yet".
    ("NPI Checked", "npi_checked"),
    # What the practice's homepage showed (site_audit), its visible text for
    # keyword checks, and when it was read. Lead types are decided from these
    # when leads are read and never stored, so editing a type needs no
    # re-scrape.
    ("Site Facts", "site_facts"),
    ("Site Text", "site_text"),
    ("Site Checked", "site_checked"),
)
```

2. In `EXPORT_LAYOUT`, replace the final `"First Seen",\n)` with:

```python
    "First Seen",
    # --- added with lead types: worked out at export time, never stored ---
    "Lead Types",
)
```

3. Change `iter_leads` to accept and pass the two new filters:

```python
    def iter_leads(self, sheet_key=None, state=None, source=None,
                   missing_email=False, has_email=False, unverified=False,
                   needs_cms=False, needs_npi=False, min_score=None,
                   email_statuses=None, order_by=None, limit=None,
                   needs_site_check=False, created_since=None):
```

and inside it:

```python
        where, params = self._filter_sql(
            sheet_key=sheet_key, state=state, source=source,
            missing_email=missing_email, has_email=has_email,
            unverified=unverified, needs_cms=needs_cms, needs_npi=needs_npi,
            min_score=min_score, email_statuses=email_statuses,
            needs_site_check=needs_site_check, created_since=created_since)
```

4. Change `_filter_sql`'s signature to

```python
    def _filter_sql(sheet_key=None, state=None, source=None, missing_email=False,
                    has_email=False, unverified=False, needs_cms=False,
                    needs_npi=False, min_score=None, email_statuses=None,
                    needs_site_check=False, created_since=None):
```

and add, just before `return ((" WHERE " ...`:

```python
        if needs_site_check:
            # Leads with a website nobody has read yet. A read records the
            # date; a transient failure records nothing, so it is tried again.
            where.append("(website IS NOT NULL AND website != '')")
            where.append("(site_checked IS NULL OR site_checked = '')")
        if created_since:
            where.append("created_at >= ?")
            params.append(created_since)
```

5. Replace the `PENDING_FILTERS` lines with:

```python
    # The five enrichment backlogs, in the order the passes run.
    PENDING_FILTERS = ("missing_email", "unverified", "needs_npi", "needs_cms",
                       "needs_site_check")
```

6. Replace `iter_best_leads` with:

```python
    def iter_best_leads(self, owners_only=False, require_email=False,
                        icp_only=False, max_practice_size=ICP_MAX_PRACTICE_SIZE,
                        states=None, min_score=None, lead_types=None):
        """Lazily yield workable leads, best first, each with a `Why` line.

        Lazy because callers usually want the top 25 of several thousand:
        building the whole list and then slicing it means running explain_lead
        on every lead in the database to show a page of them.

        Filtering happens here rather than in SQL for `icp_only`, because the
        ICP test spans several columns and a couple of derived values, and the
        rule is worth keeping readable in one place.

        `lead_types` is a lead_types.Selection. Only leads it admits come back,
        and their Why line ends with what they need. It is taken by its shape
        rather than imported, because lead_types imports this module.
        """
        filtering = lead_types is not None and lead_types.active
        for lead in self.iter_leads(min_score=min_score, has_email=require_email):
            if states and lead.get("State") not in states:
                continue
            if owners_only and str(lead.get("Role") or "").strip() not in OWNERSHIP_ROLES:
                continue
            if icp_only and not is_icp(lead, max_practice_size):
                continue
            if not (lead.get("Email") or lead.get("Phone")):
                continue  # nothing to contact them with
            needs = ""
            if filtering:
                judgement = lead_types.judge(lead)
                if not judgement.admitted:
                    continue
                needs = judgement.needs_line()
            lead["Why"] = " · ".join(part for part in (explain_lead(lead), needs) if part)
            yield lead
```

7. After `_utcnow` at the bottom of the file, add:

```python
def utcnow():
    """An ISO UTC timestamp in the format the leads table stores, for callers
    that want "leads created since this moment"."""
    return _utcnow()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_lead_type_storage.py tests/test_performance.py tests/test_storage.py tests/test_npi_layout.py tests/test_calllist.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add storage.py tests/test_lead_type_storage.py tests/test_performance.py
git commit -m "Store homepage facts on each lead and filter the Call List by lead type

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: exporters: filtered export, the Lead Types column, Call List, Summary

**Files:**
- Modify: `lead_manager.py`
- Test: `tests/test_lead_type_export.py`

**Interfaces:**
- Consumes: `Selection`/`Judgement` from Task 3 (duck-typed: `.judge(lead).admitted`, `.describe()`, `.active`, `.label()`); `store.best_leads(..., lead_types=)` from Task 5.
- Produces:
  - `lead_manager._apply_lead_types(leads, lead_types) -> list`
  - `ExcelExporter.export(store, ..., lead_types=None)`, `GoogleSheetsExporter.export(store, ..., lead_types=None)`
  - Summary sheet row `"Lead types"`. Call List rows filtered by the selection.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lead_type_export.py`:

```python
"""The export with lead types: only the leads that need what was chosen, each
with its reasons in a Lead Types column on the end."""

from datetime import date

import pytest

import lead_types
import site_audit
from conftest import healthy_site_facts

openpyxl = pytest.importorskip("openpyxl")
from lead_manager import ExcelExporter  # noqa: E402

TODAY = date(2026, 10, 9)
TAB = "Texas - Psychology Today"


def lead(npi, phone, facts):
    return {"NPI Number": npi, "Provider First Name": "Jane", "Provider Last Name": "Smith",
            "State": "TX", "Source": "Psychology Today", "Phone": phone,
            "Facebook": "https://facebook.com/x", "Instagram": "https://instagram.com/x",
            "Site Facts": site_audit.dump_facts(facts)}


@pytest.fixture
def seeded(store):
    store.add_leads_batch([
        lead("1111111111", "(512) 471-3301",
             site_audit.no_site_facts(site_audit.NO_SITE_LISTED, today=TODAY)),
        lead("2222222222", "(512) 471-3302", healthy_site_facts(TODAY)),
    ])
    return store


def selection(*ids):
    return lead_types.Selection(lead_types.builtin_types(), ids, today=TODAY)


def rows(path, sheet):
    workbook = openpyxl.load_workbook(path)
    try:
        worksheet = workbook[sheet]
        headers = [c.value for c in worksheet[1]]
        data = [dict(zip(headers, [c.value for c in row]))
                for row in worksheet.iter_rows(min_row=2)]
        return headers, data
    finally:
        workbook.close()


class TestExport:

    def test_only_leads_that_need_it_are_exported(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        written = ExcelExporter(path).export(seeded, call_list=0,
                                             lead_types=selection("website_development"))
        assert written == 1
        _headers, data = rows(path, TAB)
        assert [r["NPI Number"] for r in data] == ["1111111111"]
        assert data[0]["Lead Types"] == "Website Development: no website of their own"

    def test_the_column_is_last(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        ExcelExporter(path).export(seeded, call_list=0, lead_types=selection())
        headers, _data = rows(path, TAB)
        assert headers[-1] == "Lead Types"

    def test_with_nothing_chosen_every_lead_is_exported_with_its_matches(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        assert ExcelExporter(path).export(seeded, call_list=0, lead_types=selection()) == 2
        _headers, data = rows(path, TAB)
        by_npi = {r["NPI Number"]: r for r in data}
        assert by_npi["1111111111"]["Lead Types"].startswith("Website Development")
        assert not by_npi["2222222222"]["Lead Types"]

    def test_no_selection_object_leaves_the_column_blank(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        assert ExcelExporter(path).export(seeded, call_list=0) == 2
        _headers, data = rows(path, TAB)
        assert not any(r["Lead Types"] for r in data)

    def test_nothing_matching_leaves_the_file_alone(self, seeded, tmp_path):
        path = tmp_path / "out.xlsx"
        assert ExcelExporter(str(path)).export(seeded, call_list=0,
                                               lead_types=selection("seo")) == 0
        assert not path.exists()

    def test_the_call_list_follows_the_selection(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        ExcelExporter(path).export(seeded, call_list=10,
                                   lead_types=selection("website_development"))
        _headers, data = rows(path, "Call List")
        assert len(data) == 1
        assert "needs: no website of their own" in data[0]["Why this lead"]

    def test_the_summary_names_the_selection(self, seeded, tmp_path):
        path = str(tmp_path / "out.xlsx")
        ExcelExporter(path).export(seeded, call_list=0,
                                   lead_types=selection("website_development"))
        workbook = openpyxl.load_workbook(path)
        try:
            values = [c.value for row in workbook["Summary"].iter_rows() for c in row]
        finally:
            workbook.close()
        assert "Lead types" in values
        assert "Website Development" in values
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_lead_type_export.py -v`
Expected: FAIL with `TypeError: export() got an unexpected keyword argument 'lead_types'`.

- [ ] **Step 3: Implement**

In `lead_manager.py`:

1. Add a module-level helper after `_as_number`:

```python
def _apply_lead_types(leads, lead_types):
    """The leads a lead-type selection admits, each with its Lead Types column
    filled in. With no selection object at all, leads pass through unchanged
    and the column stays blank."""
    if lead_types is None:
        return list(leads)
    kept = []
    for lead in leads:
        judgement = lead_types.judge(lead)
        if not judgement.admitted:
            continue
        lead["Lead Types"] = judgement.describe()
        kept.append(lead)
    return kept
```

2. In `ExcelExporter.COLUMN_WIDTHS`, add `"Lead Types": 60,` after `"Phone Verified": 32,`.

3. Change `ExcelExporter.export`'s signature and body:

```python
    def export(self, store, only_sheets=None, min_score=None,
               email_statuses=None, progress=None, call_list=100,
               call_list_icp=False, lead_types=None):
```

Add this sentence to its docstring: "lead_types (a lead_types.Selection) keeps only the leads it admits, on every tab and on the Call List, and fills the Lead Types column." Then replace the per-sheet `leads = list(store.iter_leads(...))` with:

```python
            leads = _apply_lead_types(store.iter_leads(
                sheet_key=sheet_name, min_score=min_score,
                email_statuses=email_statuses), lead_types)
```

Replace the "nothing written" warning with:

```python
        if not written:
            chosen = (" (lead types: " + lead_types.label() + ")"
                      if lead_types is not None and lead_types.active else "")
            self.logger.warning(
                "Nothing matched the export filters" + chosen + ", so " +
                os.path.basename(self.filename) + " was left unchanged.")
            return 0
```

and the call-list and summary calls with:

```python
        if call_list:
            self._write_call_list(workbook, store, limit=call_list,
                                  icp_only=call_list_icp, lead_types=lead_types)
        self._write_summary(workbook, store, per_sheet, lead_types=lead_types)
```

4. `_write_call_list(self, workbook, store, limit=100, icp_only=False, lead_types=None)` with
`leads = store.best_leads(limit=limit, icp_only=icp_only, lead_types=lead_types)`.

5. `_write_summary(self, workbook, store, per_sheet, lead_types=None)`. In its `rows` list, after `("Average lead score", stats["avg_score"]),` add:

```python
            ("Lead types", lead_types.label()
             if lead_types is not None and lead_types.active else "All leads"),
```

6. `GoogleSheetsExporter.export(self, store, only_sheets=None, min_score=None, email_statuses=None, progress=None, lead_types=None)` with

```python
            leads = _apply_lead_types(store.iter_leads(
                sheet_key=sheet_name, min_score=min_score,
                email_statuses=email_statuses), lead_types)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_lead_type_export.py tests/test_calllist.py tests/test_integration.py tests/test_npi_layout.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add lead_manager.py tests/test_lead_type_export.py
git commit -m "Export only the leads that need the chosen services, with the reasons

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: email_engine: how the homepage fetch went, and the facts it shows

**Files:**
- Modify: `email_engine.py`
- Test: `tests/test_site_evidence.py`

**Interfaces:**
- Consumes: `site_audit.read_homepage`, `classify_fetch_error`, `classify_status`, `network_resolves`, `failed_audit`, `no_site_facts`, `columns`, constants.
- Produces (`EmailEngine`):
  - `_fetch_home(page) -> {"html", "final_url", "status", "fetch"}` (staticmethod)
  - `_iter_candidate_pages(url, home=None)`: fills the `home` dict with `_fetch_home`'s record
  - `is_directory_site(url) -> bool` (classmethod)
  - `read_site(url) -> (facts|None, text)`: one request
  - `scrape_website_contact_info(...)` gains keys `"facts"` (dict or None) and `"text"` (str)
  - `find_email(lead_data)` adds `"Site Facts"/"Site Text"/"Site Checked"` to `lead_data` when it scraped a site

- [ ] **Step 1: Write the failing tests**

Create `tests/test_site_evidence.py`:

```python
"""Where lead-type evidence comes from: the homepage the scraper already
fetches, graded so that nothing about this machine's network or a site's bot
protection is ever recorded as the practice's own problem."""

import pytest
import requests

import http_pool
import site_audit
from conftest import fixture

SITE = "https://cedargrove.example/"


class Response:
    def __init__(self, status=200, text="", url=""):
        self.status_code = status
        self.text = text
        self.url = url


class Session:
    """Answers by URL: a Response, or an exception to raise. Anything else 404s."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append(url)
        answer = self.pages.get(url, Response(404, "", url))
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def web(monkeypatch):
    def install(pages):
        session = Session(pages)
        monkeypatch.setattr(http_pool, "session", lambda: session)
        return session
    return install


class TestTheScrapeRecordsTheHomepage:

    def test_the_facts_and_text_come_with_the_contact_details(self, web, email_engine):
        web({SITE: Response(200, fixture("site_modern.html"), SITE)})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["viewport"] is True
        assert contact["facts"]["booking"] == ["simplepractice"]
        assert "Telehealth" in contact["text"]

    def test_a_marketplace_profile_is_not_their_own_site(self, web, email_engine):
        session = web({})
        contact = email_engine.scrape_website_contact_info(
            "https://growtherapy.com/provider/jane")
        assert contact["facts"]["own_site"] is False
        assert contact["facts"]["profile_host"] == "growtherapy.com"
        assert session.calls == []

    def test_a_directory_page_is_not_their_own_site(self, web, email_engine):
        url = "https://www.yelp.com/biz/cedar-grove"
        web({url: Response(200, "<html><body>listing</body></html>", url)})
        contact = email_engine.scrape_website_contact_info(url)
        assert contact["facts"]["own_site"] is False
        assert contact["facts"]["profile_host"] == "yelp.com"

    def test_a_timeout_is_not_recorded(self, web, email_engine):
        web({SITE: requests.exceptions.ReadTimeout("slow")})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "timeout"
        assert site_audit.columns(contact["facts"]) == {}

    def test_a_dead_domain_is_recorded(self, web, email_engine, monkeypatch):
        monkeypatch.setattr(site_audit, "network_resolves", lambda *a, **k: True)
        web({SITE: requests.exceptions.ConnectionError("getaddrinfo failed")})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "dns"
        assert site_audit.columns(contact["facts"])

    def test_our_own_outage_is_not_blamed_on_the_practice(self, web, email_engine,
                                                          monkeypatch):
        monkeypatch.setattr(site_audit, "network_resolves", lambda *a, **k: False)
        web({SITE: requests.exceptions.ConnectionError("getaddrinfo failed")})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "connection"
        assert site_audit.columns(contact["facts"]) == {}

    def test_bot_protection_is_not_a_down_site(self, web, email_engine):
        web({SITE: Response(403, "Access denied", SITE)})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "blocked"
        assert site_audit.columns(contact["facts"]) == {}

    def test_a_server_error_is_recorded(self, web, email_engine):
        web({SITE: Response(503, "down", SITE)})
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["facts"]["fetch"] == "http_error"
        assert contact["facts"]["status"] == 503

    def test_a_redirect_to_https_counts_as_https(self, web, email_engine):
        start = "http://cedargrove.example/"
        web({start: Response(200, fixture("site_modern.html"), SITE)})
        contact = email_engine.scrape_website_contact_info(start)
        assert contact["facts"]["https"] is True

    def test_a_broken_audit_keeps_the_contact_details(self, web, email_engine, monkeypatch):
        html = '<html><body><a href="mailto:hello@cedargrove.example">Email</a></body></html>'
        web({SITE: Response(200, html, SITE)})

        def boom(*_args, **_kwargs):
            raise RuntimeError("audit exploded")
        monkeypatch.setattr(site_audit, "read_homepage", boom)
        contact = email_engine.scrape_website_contact_info(SITE)
        assert contact["email"] == "hello@cedargrove.example"
        assert contact["facts"]["audit_error"] == "RuntimeError"


class TestReadSite:

    def test_one_request(self, web, email_engine):
        session = web({SITE: Response(200, fixture("site_modern.html"), SITE)})
        facts, text = email_engine.read_site(SITE)
        assert session.calls == [SITE]
        assert facts["viewport"] is True and "Telehealth" in text

    def test_a_profile_costs_nothing(self, web, email_engine):
        session = web({})
        facts, _text = email_engine.read_site("https://www.thriveworks.com/counselor/x")
        assert facts["own_site"] is False and session.calls == []

    def test_no_url_no_facts(self, email_engine):
        assert email_engine.read_site("") == (None, "")


class TestFindEmailRecordsTheSiteItFound:

    def test_site_columns_are_added(self, web, email_engine, monkeypatch):
        web({SITE: Response(200, fixture("site_modern.html"), SITE)})
        monkeypatch.setattr(email_engine, "_find_website", lambda query: SITE)
        monkeypatch.setattr(email_engine, "_search_for_email_directly",
                            lambda *a, **k: "")
        found = email_engine.find_email({"Organization Name": "Cedar Grove Counseling",
                                         "City": "Austin", "State": "TX"})
        assert found["Website"] == SITE
        assert site_audit.load_facts(found["Site Facts"])["viewport"] is True
        assert found["Site Checked"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_site_evidence.py -v`
Expected: FAIL with `KeyError: 'facts'` and `AttributeError: 'EmailEngine' object has no attribute 'read_site'`.

- [ ] **Step 3: Implement**

In `email_engine.py`:

1. Add `import site_audit` after `import http_pool`.

2. Replace `scrape_website_contact_info` with:

```python
    def scrape_website_contact_info(self, url, person_name=None):
        """Scrape an already-known website for the provider's email, phone,
        ownership role and social media profiles in a single pass over the same
        pages — and record what the homepage shows, for lead types.

        Returns {"email", "phone", "phone_confidence", "role", "socials",
        "facts", "text"}. Any value may be empty if nothing trustworthy was
        found. When person_name is given, the provider's own direct line is
        preferred over the practice's shared line, and "role" reports whether
        the site describes them as the Owner/Founder/CEO of the practice.
        "socials" maps LinkedIn / Facebook / Instagram / Other to the profile
        links the site links out to. "facts" and "text" are site_audit's
        reading of the homepage (None and "" when there was no URL).
        """
        empty = {"email": "", "phone": "", "phone_confidence": "", "role": "",
                 "socials": {}, "facts": None, "text": ""}
        if not url:
            return dict(empty)
        if self.is_platform_site(url):
            # A marketplace profile page (Grow Therapy, Thriveworks, LifeStance,
            # ...) publishes the *marketplace's* contact details: its central
            # intake number, its corporate mailbox and its brand social
            # accounts, identically on every clinician's page. Scraping it
            # attributes all of that to whichever provider happens to be listed
            # — two different Delaware therapists both came back with Grow
            # Therapy's (646) number and Instagram. None of it is the
            # provider's, so nothing here is worth reading; the caller still
            # records the profile URL itself as the Website. For lead types it
            # is plain evidence: they have no site of their own.
            self.logger.info(
                f"{url.split('/')[2]} is a therapist marketplace — its contact "
                f"details and socials belong to the platform, not the provider, "
                f"so they are not recorded.")
            result = dict(empty)
            result["facts"] = self._profile_facts(url)
            return result
        site_domain = self._registrable_domain(urlparse(url).netloc)
        emails = set()
        candidates = []
        socials = {}
        role = ""
        home = {}
        for page, text in self._iter_candidate_pages(url, home=home):
            for e in re.findall(self.EMAIL_REGEX, text):
                if self._is_valid_email(e) and self._belongs_to_site(e, site_domain):
                    emails.add(e)
            candidates.extend(self._extract_phones(text))
            try:
                for column, link in self.extract_socials(text, page).items():
                    socials.setdefault(column, link)
            except Exception as e:  # never let a parse hiccup lose the contact info
                self.logger.debug(f"Social link extraction failed on {page}: {e}")
            if not role:
                try:
                    role = self.detect_role(text, person_name)
                except Exception as e:
                    self.logger.debug(f"Role detection failed on {page}: {e}")
            # Stop as soon as there's nothing left to gain: an email, a
            # personal number, a role and at least one social profile. Social
            # icons usually sit in the footer of the contact/about page, so
            # stopping at the homepage would miss most of them.
            if emails and role and socials and any(c["kind"] == "personal" for c in candidates):
                break
        phone, confidence = self._choose_phone(candidates)
        if self.is_directory_site(url):
            facts, site_text = self._profile_facts(url), ""
        else:
            facts, site_text = self._read_home(url, home)
        return {
            "email": self._pick_best_email(emails) if emails else "",
            "phone": phone,
            "phone_confidence": confidence,
            "role": role,
            "socials": socials,
            "facts": facts,
            "text": site_text,
        }

    def read_site(self, url):
        """(facts, text) for a practice's homepage, in one request. The Check
        websites pass uses this for leads collected before facts were recorded."""
        if not url:
            return None, ""
        if self.is_platform_site(url) or self.is_directory_site(url):
            return self._profile_facts(url), ""
        return self._read_home(url, self._fetch_home(url))

    def _read_home(self, url, home):
        """site_audit's reading of a fetched homepage. Never raises: a failed
        audit must not cost the contact details scraped alongside it."""
        if not home:
            return None, ""
        fetch = home.get("fetch") or site_audit.FETCH_CONNECTION
        try:
            return site_audit.read_homepage(home.get("html") or "",
                                            home.get("final_url") or url,
                                            status=home.get("status"), fetch=fetch)
        except Exception as e:
            self.logger.debug(f"Homepage audit failed on {url}: {e}")
            return site_audit.failed_audit(fetch, e, home.get("status")), ""

    def _profile_facts(self, url):
        return site_audit.no_site_facts(
            site_audit.NO_SITE_PROFILE, self._registrable_domain(urlparse(url).netloc))

    @classmethod
    def is_directory_site(cls, url):
        """A listing page about the practice (Yelp, Healthgrades, a Facebook
        page) rather than a site the practice runs itself."""
        host = urlparse(url or "").netloc.lower()
        return bool(host) and any(fragment in host for fragment in cls.DIRECTORY_DOMAINS)
```

3. In `_iter_candidate_pages`, change the signature to `def _iter_candidate_pages(self, url, home=None):`, add to its docstring "`home`, when given, is filled with how the homepage fetch went (see _fetch_home).", and replace the opening

```python
        home = self._fetch(url)
        if home is None:
```

with

```python
        record = self._fetch_home(url)
        if home is not None:
            home.update(record)
        home_html = record["html"]
        if home_html is None:
```

Then replace the later `yield url, home` with `yield url, home_html`, and `BeautifulSoup(home, "lxml")` with `BeautifulSoup(home_html, "lxml")`.

4. Add after `_fetch`:

```python
    @staticmethod
    def _fetch_home(page):
        """The homepage, with how the request went. Lead types need to tell a
        dead domain from a slow one, which _fetch's None cannot say.

        A DNS failure is double-checked against hosts that always resolve: if
        those fail too, it is this machine's connection rather than the
        practice's domain, and is recorded as a connection problem — which is
        evidence of nothing.
        """
        record = {"html": None, "final_url": page, "status": None,
                  "fetch": site_audit.FETCH_CONNECTION}
        try:
            response = http_pool.session().get(page, timeout=(5, 10))
        except requests.RequestException as e:
            outcome = site_audit.classify_fetch_error(e)
            if outcome == site_audit.FETCH_DNS and not site_audit.network_resolves():
                outcome = site_audit.FETCH_CONNECTION
            record["fetch"] = outcome
            return record
        record["status"] = response.status_code
        record["final_url"] = getattr(response, "url", None) or page
        record["fetch"] = site_audit.classify_status(response.status_code)
        if response.status_code == 200:
            record["html"] = response.text
        return record
```

5. In `find_email`, inside `if website:` after the socials loop, add:

```python
            # The same read also says what the practice's homepage shows, for
            # lead types. Nothing is added for a transient failure.
            lead_data.update(site_audit.columns(contact.get("facts"),
                                                contact.get("text", "")))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_site_evidence.py tests/test_parsers.py tests/test_performance.py -v`
Expected: all PASS. `test_site_pages_and_registry_lookups_go_through_the_pool` must still pass, because `_fetch` is unchanged.

- [ ] **Step 5: Commit**

```bash
git add email_engine.py tests/test_site_evidence.py
git commit -m "Record what each practice's homepage shows while scraping it

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Psychology Today "no website" as evidence, and wiring facts into collection

**Files:**
- Modify: `psychology_today_engine.py`, `pipeline.py`
- Modify: `tests/test_pipeline.py` (`FakePTEngine`), `tests/test_integration.py` (`FakePT`)
- Test: `tests/test_site_evidence.py` (append)

**Interfaces:**
- Consumes: `site_audit.WEBSITE_*`, `no_site_facts`, `columns`; Task 7's `"facts"`/`"text"` keys.
- Produces:
  - `PsychologyTodayEngine.lookup_website(pt_id) -> (url, outcome)`, where outcome is one of `site_audit.WEBSITE_LISTED/NONE/UNKNOWN`. `_resolve_website(pt_id)` returns `lookup_website(pt_id)[0]`.
  - Leads from `process_pt_entry` carry `Site Facts`/`Site Text`/`Site Checked` when there's evidence.
  - `_find_email_for` passes the three site columns through.

- [ ] **Step 1: Update the fakes**

In `tests/test_pipeline.py`, replace `FakePTEngine.__init__` and add `lookup_website`:

```python
    def __init__(self, pages=None, raises=None, website="https://practice.example",
                 website_outcome=None):
        self.pages = pages or []
        self.raises = raises
        self.website = website
        # Psychology Today answers "no website" (404) outright, or the lookup
        # fails ("unknown"). Only the first is evidence.
        self.website_outcome = website_outcome or ("listed" if website else "none")

    def lookup_website(self, pt_id):
        return self.website, self.website_outcome
```

In `tests/test_integration.py`, add to `FakePT`:

```python
    def lookup_website(self, pt_id):
        website = self._resolve_website(pt_id)
        return website, ("listed" if website else "none")
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_site_evidence.py`:

```python
from storage import RUN_OK  # noqa: E402
from pipeline import LeadPipeline, Reporter  # noqa: E402
from test_pipeline import FakeEmailEngine, FakeNPIEngine, FakePTEngine, entry  # noqa: E402


class PTResponse:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class TestPsychologyTodayWebsiteLookup:

    def answer(self, pt_engine, monkeypatch, response):
        def get(url, **_kwargs):
            if isinstance(response, Exception):
                raise response
            return response
        monkeypatch.setattr(pt_engine.session, "get", get)

    def test_a_404_means_no_website_listed(self, pt_engine, monkeypatch):
        self.answer(pt_engine, monkeypatch, PTResponse(404))
        assert pt_engine.lookup_website("123") == ("", site_audit.WEBSITE_NONE)

    def test_a_server_error_says_nothing(self, pt_engine, monkeypatch):
        self.answer(pt_engine, monkeypatch, PTResponse(500))
        assert pt_engine.lookup_website("123") == ("", site_audit.WEBSITE_UNKNOWN)

    def test_a_failed_request_says_nothing(self, pt_engine, monkeypatch):
        self.answer(pt_engine, monkeypatch, requests.exceptions.ConnectTimeout("slow"))
        assert pt_engine.lookup_website("123") == ("", site_audit.WEBSITE_UNKNOWN)

    def test_a_listed_website(self, pt_engine, monkeypatch):
        body = '{"url":"https:' + r"//" + "cedargrove.example" + r"/" + '"}'
        self.answer(pt_engine, monkeypatch, PTResponse(200, body))
        assert pt_engine.lookup_website("123") == \
            ("https://cedargrove.example/", site_audit.WEBSITE_LISTED)
        assert pt_engine._resolve_website("123") == "https://cedargrove.example/"

    def test_a_page_with_no_usable_link_says_nothing(self, pt_engine, monkeypatch):
        self.answer(pt_engine, monkeypatch, PTResponse(200, "<html>nothing</html>"))
        assert pt_engine.lookup_website("123") == ("", site_audit.WEBSITE_UNKNOWN)


def build(store, **engines):
    return LeadPipeline(store=store,
                        npi_engine=engines.get("npi_engine") or FakeNPIEngine(),
                        email_engine=engines.get("email_engine") or FakeEmailEngine(),
                        pt_engine=engines.get("pt_engine") or FakePTEngine(),
                        reporter=Reporter())


class TestCollectionRecordsEvidence:

    def test_no_website_listed_is_recorded(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(pages=[(1, [entry()])], website=""))
        pipeline.collect_psychology_today("Texas", "TX")
        facts = site_audit.load_facts(next(store.iter_leads())["Site Facts"])
        assert facts["own_site"] is False and facts["why"] == "none_listed"

    def test_a_failed_lookup_records_nothing(self, store):
        pipeline = build(store, pt_engine=FakePTEngine(
            pages=[(1, [entry()])], website="", website_outcome="unknown"))
        pipeline.collect_psychology_today("Texas", "TX")
        assert not next(store.iter_leads())["Site Facts"]

    def test_the_homepage_reading_is_kept(self, store):
        facts, text = site_audit.read_homepage(fixture("site_modern.html"), SITE)
        engine = FakeEmailEngine(contact={"email": "", "phone": "", "phone_confidence": "",
                                          "role": "", "socials": {}, "facts": facts,
                                          "text": text})
        pipeline = build(store, pt_engine=FakePTEngine(pages=[(1, [entry()])]),
                         email_engine=engine)
        pipeline.collect_psychology_today("Texas", "TX")
        saved = next(store.iter_leads())
        assert site_audit.load_facts(saved["Site Facts"])["viewport"] is True
        assert "Telehealth" in site_audit.unpack_text(saved["Site Text"])
        assert saved["Site Checked"] == facts["checked"]


class SiteFindingEmailEngine(FakeEmailEngine):
    def find_email(self, lead):
        lead["Website"] = SITE
        facts, text = site_audit.read_homepage(fixture("site_modern.html"), SITE)
        lead.update(site_audit.columns(facts, text))
        lead["Email"] = ""
        return lead


class TestFindEmailsRecordsEvidence:

    def test_the_site_it_found_is_recorded(self, store):
        store.add_leads_batch([{"NPI Number": "1111111111", "State": "TX",
                                "Source": "NPI Registry", "Phone": "(512) 471-3300"}])
        pipeline = build(store, email_engine=SiteFindingEmailEngine())
        pipeline.find_missing_emails()
        saved = next(store.iter_leads())
        assert saved["Website"] == SITE
        assert site_audit.load_facts(saved["Site Facts"])["viewport"] is True
```

These pipelines are built without a verifier, so `LeadPipeline.guess_email` returns `""` at its first line and no mail server is contacted.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_site_evidence.py tests/test_pipeline.py -v`
Expected: the new tests FAIL with `AttributeError: 'PsychologyTodayEngine' object has no attribute 'lookup_website'`, and the collection tests FAIL because `Site Facts` is empty.

- [ ] **Step 4: Implement**

In `psychology_today_engine.py`, add `import site_audit` after `import http_pool`, and replace `_resolve_website` with:

```python
    def lookup_website(self, pt_id):
        """(url, outcome) for the provider's own website, read out of
        Psychology Today's "My website" interstitial.

        The page is a Nuxt app whose inlined payload carries the target URL with
        every slash escaped. outcome is site_audit.WEBSITE_LISTED (url is
        theirs), WEBSITE_NONE (a 404: they list no website at all — the cheapest
        possible signal, and real evidence for lead types) or WEBSITE_UNKNOWN
        (the lookup failed, or the page held no usable link — evidence of
        nothing).
        """
        try:
            resp = self.session.get(
                self.WEBSITE_URL.format(pt_id=pt_id), timeout=15)
        except requests.RequestException as e:
            self.logger.warning(f"Could not resolve website for profile {pt_id}: {e}")
            return "", site_audit.WEBSITE_UNKNOWN
        if resp.status_code == 404:
            return "", site_audit.WEBSITE_NONE
        if resp.status_code != 200:
            self.logger.warning(
                f"Website lookup for profile {pt_id} -> HTTP {resp.status_code}")
            return "", site_audit.WEBSITE_UNKNOWN
        url = self._first_external_url(resp.text)
        return (url, site_audit.WEBSITE_LISTED) if url else ("", site_audit.WEBSITE_UNKNOWN)

    def _resolve_website(self, pt_id):
        """Just the URL, for callers that don't need to know why it is blank."""
        return self.lookup_website(pt_id)[0]
```

In `pipeline.py`, add `import site_audit` after `import storage`. In `process_pt_entry`, replace

```python
        specialty = entry.get("credential", "")
        website = self.pt_engine._resolve_website(entry["pt_id"])
        if not specialty:
            details = self.pt_engine.get_profile_details(entry["pt_id"], entry["profile_url"])
            specialty = details.get("specialty", "")
            website = website or details.get("website", "")
```

with

```python
        specialty = entry.get("credential", "")
        website, website_outcome = self.pt_engine.lookup_website(entry["pt_id"])
        if not specialty:
            details = self.pt_engine.get_profile_details(entry["pt_id"], entry["profile_url"])
            specialty = details.get("specialty", "")
            if not website and details.get("website"):
                website, website_outcome = details["website"], site_audit.WEBSITE_LISTED
```

After `email_source = "Provider website" if email else ""`, add:

```python
        # What their homepage shows, for lead types. Psychology Today saying
        # "no website" is evidence too; a lookup that failed is not.
        if website:
            site = site_audit.columns(contact.get("facts"), contact.get("text", ""))
        elif website_outcome == site_audit.WEBSITE_NONE:
            site = site_audit.columns(site_audit.no_site_facts(site_audit.NO_SITE_LISTED))
        else:
            site = {}
```

Then, just before `self.attach_verification(lead, person_name=entry.get("name"), note=email_source)`, add `lead.update(site)`.

In `_find_email_for`, change the key tuple to:

```python
        for key in ("Email", "Website", "LinkedIn", "Facebook", "Instagram",
                    "Other Social", "Site Facts", "Site Text", "Site Checked"):
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_site_evidence.py tests/test_pipeline.py tests/test_integration.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add psychology_today_engine.py pipeline.py tests/test_pipeline.py tests/test_integration.py tests/test_site_evidence.py
git commit -m "Record lead-type evidence during collection and email discovery

A Psychology Today 404 now counts as 'no website listed'; a failed lookup
counts as nothing.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: pipeline: the Check websites pass, campaign filter, run summary

**Files:**
- Modify: `pipeline.py`
- Test: `tests/test_site_evidence.py` (append)

**Interfaces:**
- Consumes: `email_engine.read_site(url)`, `site_audit.columns`, `store.iter_leads(needs_site_check=, created_since=)`, `lead_types.MATCH/NOT_CHECKED`, `Selection`.
- Produces:
  - `LeadPipeline.check_sites(recheck=False) -> RunResult`. `added` = leads whose facts were recorded. Run action `"site_check"`.
  - `LeadPipeline.recipients_for(..., lead_types=None)`, `send_campaign(..., lead_types=None)`
  - `LeadPipeline.lead_type_summary(selection, since) -> str` (`""` when the selection is inactive or there are no leads)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_site_evidence.py`:

```python
import threading  # noqa: E402
from datetime import date  # noqa: E402

import lead_types  # noqa: E402
import storage  # noqa: E402
from conftest import healthy_site_facts  # noqa: E402
from storage import RUN_EMPTY, RUN_STOPPED  # noqa: E402

TODAY = date(2026, 10, 9)


class SiteReader:
    """Stands in for EmailEngine.read_site: answers by URL."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def read_site(self, url):
        self.calls.append(url)
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


def sites_pipeline(store, answers, reporter=None):
    return LeadPipeline(store=store, npi_engine=None, email_engine=SiteReader(answers),
                        pt_engine=None, reporter=reporter or Reporter())


def site_lead(npi, website="", **fields):
    row = {"NPI Number": npi, "State": "TX", "Source": "Psychology Today",
           "Phone": "(512) 471-33{0}".format(npi[-2:]), "Website": website}
    row.update(fields)
    return row


class TestCheckWebsites:

    def seed(self, store):
        store.add_leads_batch([
            site_lead("1111111111", "https://a.example"),
            site_lead("2222222222", "https://b.example", **{
                "Site Checked": "2026-10-01",
                "Site Facts": site_audit.dump_facts(healthy_site_facts(TODAY))}),
            site_lead("3333333333"),
        ])

    def test_reads_every_website_nobody_has_read(self, store):
        self.seed(store)
        pipeline = sites_pipeline(store, {
            "https://a.example": (healthy_site_facts(TODAY, viewport=False), "hello")})
        result = pipeline.check_sites()
        assert result.status == RUN_OK and result.added == 1
        assert pipeline.email_engine.calls == ["https://a.example"]
        saved = next(l for l in store.iter_leads() if l["NPI Number"] == "1111111111")
        assert site_audit.load_facts(saved["Site Facts"])["viewport"] is False

    def test_recheck_reads_them_all_again(self, store):
        self.seed(store)
        reading = (healthy_site_facts(TODAY), "")
        pipeline = sites_pipeline(store, {"https://a.example": reading,
                                          "https://b.example": reading})
        assert pipeline.check_sites(recheck=True).added == 2

    def test_a_timeout_is_not_recorded_and_is_tried_again(self, store):
        self.seed(store)
        pipeline = sites_pipeline(store, {
            "https://a.example": (site_audit.base_facts(True, "timeout"), "")})
        result = pipeline.check_sites()
        assert result.status == RUN_EMPTY and result.added == 0
        assert store.pending_counts()["needs_site_check"] == 1

    def test_a_reader_crash_is_a_retry_not_a_failed_run(self, store):
        self.seed(store)
        pipeline = sites_pipeline(store, {"https://a.example": RuntimeError("boom")})
        assert pipeline.check_sites().status == RUN_EMPTY

    def test_nothing_to_do(self, store):
        store.add_leads_batch([site_lead("3333333333")])
        result = sites_pipeline(store, {}).check_sites()
        assert result.status == RUN_OK and result.message == "Nothing to do."

    def test_stop_is_honoured(self, store):
        self.seed(store)
        stop = threading.Event()
        stop.set()
        pipeline = sites_pipeline(store, {"https://a.example": (healthy_site_facts(TODAY), "")},
                                  reporter=Reporter(stop_event=stop))
        assert pipeline.check_sites().status == RUN_STOPPED


class TestCampaignsByLeadType:

    def test_recipients_are_only_leads_that_need_it(self, store):
        no_site = site_audit.dump_facts(site_audit.no_site_facts("none_listed", today=TODAY))
        healthy = site_audit.dump_facts(healthy_site_facts(TODAY))
        store.add_leads_batch([
            site_lead("1111111111", Email="a@a.example", **{"Email Status": "valid",
                                                           "Site Facts": no_site}),
            site_lead("2222222222", Email="b@b.example", **{"Email Status": "valid",
                                                           "Site Facts": healthy}),
            site_lead("3333333333", Email="c@c.example", **{"Email Status": "valid"}),
        ])
        pipeline = sites_pipeline(store, {})
        selection = lead_types.Selection(lead_types.builtin_types(),
                                         ["website_development"], today=TODAY)
        recipients, excluded = pipeline.recipients_for(limit=0, lead_types=selection)
        assert [r["NPI Number"] for r in recipients] == ["1111111111"]
        assert excluded == {"doesn't need Website Development": 1,
                            "can't tell yet whether they need Website Development": 1}


class TestRunSummary:

    def test_how_this_runs_leads_fit_the_selection(self, store):
        since = storage.utcnow()
        store.add_leads_batch([
            site_lead("1111111111", **{"Site Facts": site_audit.dump_facts(
                site_audit.no_site_facts("none_listed", today=TODAY))}),
            site_lead("2222222222", **{"Site Facts": site_audit.dump_facts(
                healthy_site_facts(TODAY))}),
            site_lead("3333333333"),
        ])
        selection = lead_types.Selection(lead_types.builtin_types(),
                                         ["website_development"], today=TODAY)
        summary = sites_pipeline(store, {}).lead_type_summary(selection, since)
        assert summary.startswith("1 of 3 new leads need Website Development.")
        assert "1 can't be judged yet" in summary

    def test_no_selection_no_summary(self, store):
        store.add_leads_batch([site_lead("1111111111")])
        selection = lead_types.Selection(lead_types.builtin_types())
        assert sites_pipeline(store, {}).lead_type_summary(selection, "2000-01-01") == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_site_evidence.py -v`
Expected: FAIL with `AttributeError: 'LeadPipeline' object has no attribute 'check_sites'` and `TypeError: recipients_for() got an unexpected keyword argument 'lead_types'`.

- [ ] **Step 3: Implement**

In `pipeline.py`, add `from lead_types import MATCH, NOT_CHECKED` after `from errors import ScraperError`. (Import the constants, not the module: the `lead_types` parameter below would shadow a module of the same name.)

Add after `_find_email_for`:

```python
    def check_sites(self, recheck=False):
        """Read the homepage of every lead with a website nobody has read yet,
        and record what it shows for lead types — one request per lead.

        Leads collected before lead types existed have websites but no facts,
        and NPI leads get their website from Find Emails. A read that says
        nothing about the practice (timeout, bot protection, this connection)
        is not recorded, so the next run tries it again.
        """
        run_id = self.store.start_run("site_check", "recheck" if recheck else "new only")
        if recheck:
            pending = [lead for lead in self.store.iter_leads() if lead.get("Website")]
        else:
            pending = list(self.store.iter_leads(needs_site_check=True))
        if not pending:
            self.reporter.log("Every lead with a website has already been checked.")
            return self._finish(run_id, RUN_OK, 0, "Nothing to do.")

        self.reporter.phase(
            "Checking {0} websites for lead-type evidence...".format(len(pending)))
        total = len(pending)
        done = checked = no_site = broken = retry = 0
        stopped = False

        with ThreadPoolExecutor(max_workers=self.EMAIL_WORKERS) as executor:
            futures = {executor.submit(self.email_engine.read_site, lead["Website"]): lead
                       for lead in pending}
            for future in as_completed(futures):
                if self.reporter.stopped():
                    stopped = True
                    executor.shutdown(wait=False, cancel_futures=True)
                    break
                lead = futures[future]
                done += 1
                try:
                    facts, text = future.result()
                except Exception as e:
                    self.reporter.log("Could not read {0}: {1}".format(
                        lead.get("Website"), e), "warning")
                    facts, text = None, ""
                updates = site_audit.columns(facts, text)
                if not updates:
                    retry += 1
                else:
                    self.store.update_lead(lead["_id"], updates)
                    checked += 1
                    if facts.get("own_site") is False:
                        no_site += 1
                    elif facts.get("fetch") != site_audit.FETCH_OK:
                        broken += 1
                self.reporter.progress(done, total)

        if checked:
            message = ("Checked {0} websites: {1} read, {2} only a directory or "
                       "marketplace profile, {3} down or broken.".format(
                           checked, checked - no_site - broken, no_site, broken))
        else:
            message = ("None of the {0} websites could be read just now — they timed "
                       "out, turned the app away, or this connection dropped. They "
                       "will be tried again next run.".format(done))
        self.reporter.log(message)
        if retry and checked:
            self.reporter.log(
                "{0} websites couldn't be reached right now; they will be tried "
                "again next run.".format(retry), "warning")
        status = RUN_STOPPED if stopped else self._guard_empty(checked, RUN_OK)
        return self._finish(run_id, status, checked, message)

    def lead_type_summary(self, selection, since):
        """One line on how the leads created since `since` fit the chosen lead
        types, for the end of a collection run. Empty when nothing is chosen."""
        if selection is None or not selection.active:
            return ""
        leads = list(self.store.iter_leads(created_since=since))
        if not leads:
            return ""
        matched = unknown = 0
        for lead in leads:
            status = selection.judge(lead).status
            if status == MATCH:
                matched += 1
            elif status == NOT_CHECKED:
                unknown += 1
        line = "{0} of {1} new leads need {2}.".format(matched, len(leads), selection.label())
        if unknown:
            line += (" {0} can't be judged yet — Enrich → Check websites (and Find "
                     "emails, for NPI leads) fills in what is missing.".format(unknown))
        return line
```

Change `recipients_for`'s signature to add `lead_types=None`:

```python
    def recipients_for(self, limit=50, icp_only=False, owners_only=False,
                       states=None, allow_unverified=False, skip_contacted=True,
                       lead_types=None):
```

Add to its docstring: "lead_types (a lead_types.Selection) keeps only the leads that need what was chosen, and counts the rest in `excluded` by why." Then, right after `email_key = email.lower()` inside the loop, add:

```python
            if lead_types is not None and lead_types.active:
                status = lead_types.judge(lead).status
                if status == NOT_CHECKED:
                    drop("can't tell yet whether they need " + lead_types.label())
                    continue
                if status != MATCH:
                    drop("doesn't need " + lead_types.label())
                    continue
```

Change `send_campaign`'s signature to add `lead_types=None` (after `max_seconds=90`), and pass it on:

```python
        recipients, excluded = self.recipients_for(
            limit=effective_limit, icp_only=icp_only, owners_only=owners_only,
            states=states, allow_unverified=allow_unverified, lead_types=lead_types)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_site_evidence.py tests/test_pipeline.py tests/test_outreach.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add pipeline.py tests/test_site_evidence.py
git commit -m "Add the Check websites pass, and target campaigns by lead type

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: CLI: `lead-types`, `enrich-sites`, `--lead-types`

**Files:**
- Modify: `cli.py`
- Test: `tests/test_cli_lead_types.py`

**Interfaces:**
- Consumes: `lead_types.load/resolve/parse_list/grouped/Selection`, `config.lead_types_path`, `pipeline.check_sites`, exporters' `lead_types=`.
- Produces:
  - `cli.lead_type_selection(value, logger, config_path=None) -> Selection | None` (`None` means refuse; already logged)
  - `cli.export(store, settings, logger, ..., selection=None)`. When `selection` is None it fills the column from a no-filter Selection.
  - Commands `lead-types` (`cmd_lead_types`) and `enrich-sites [--recheck]` (`cmd_enrich_sites`). `--lead-types` on `export` and `send`.

No flag means no filter. The CLI deliberately does **not** pick up the window's saved selection, so a scheduled job never silently narrows its output because someone clicked a pill.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli_lead_types.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_cli_lead_types.py -v`
Expected: FAIL with `argparse` errors (`unrecognized arguments: --lead-types`, `invalid choice: 'lead-types'`).

- [ ] **Step 3: Implement**

In `cli.py`:

1. Add to the module docstring's command list:

```
    python cli.py enrich-sites
    python cli.py lead-types
    python cli.py export --lead-types "website_development,seo"
```

2. Add `import lead_types` after `import health`.

3. Replace `export` with:

```python
def export(store, settings, logger, min_score=None, statuses=None,
           call_list=100, icp_only=False, selection=None):
    """Render the database to whichever destination is configured.

    `selection` is a lead_types.Selection. None means no filter, with the
    Lead Types column still filled in.
    """
    if selection is None:
        selection = lead_type_selection(None, logger)
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
                               call_list=call_list, call_list_icp=icp_only,
                               lead_types=selection)
    return exporter.export(store, min_score=min_score, email_statuses=statuses,
                           lead_types=selection)


def lead_type_selection(value, logger, config_path=None):
    """The --lead-types flag as a lead_types.Selection, or None after logging
    why it can't be used.

    No flag, or "all", is no filter. An unknown or switched-off type is an
    error that lists the valid ones — never a silent "no filter", which would
    export everyone or, worse, email them.
    """
    types = lead_types.load(config.lead_types_path(config_path), logger).types
    wanted = lead_types.parse_list(value)
    if not wanted or [w.lower() for w in wanted] == ["all"]:
        return lead_types.Selection(types)
    ids, unknown = lead_types.resolve(wanted, types)
    switched_off = [t.name for t in types if t.id in ids and not t.enabled]
    if unknown or switched_off:
        if unknown:
            logger.error("Unknown lead type(s): %s.", ", ".join(unknown))
        if switched_off:
            logger.error("Switched off in Settings: %s.", ", ".join(switched_off))
        logger.error("Valid lead types: %s", ", ".join(
            "{0} ({1})".format(t.id, t.name) for t in types if t.enabled))
        return None
    return lead_types.Selection(types, ids)
```

4. Add after `cmd_enrich_npi`:

```python
def cmd_enrich_sites(args, settings, logger):
    reporter = ConsoleReporter(logger, quiet=args.quiet)
    pipeline = build_pipeline(settings, logger, reporter)
    result = pipeline.check_sites(recheck=args.recheck)
    if settings.get("auto_export"):
        export(pipeline.store, settings, logger)
    return report_result(result, logger)


def cmd_lead_types(args, settings, logger):
    loaded = lead_types.load(config.lead_types_path(args.config), logger)
    if loaded.problem:
        print(loaded.problem)
        print()
    for group, members in lead_types.grouped(loaded.types):
        print(group)
        for t in members:
            rule = "all of" if t.match == "all" else "any of"
            checks = ", ".join(
                t.checks + ["mentions '{0}'".format(p) for p in t.mentions]
                + ["doesn't mention '{0}'".format(p) for p in t.not_mentions])
            print("  {0:<4}{1:<24}{2:<32}{3} {4}".format(
                "on" if t.enabled else "off", t.id, t.name, rule, checks))
        print()
    return EXIT_OK
```

5. In `cmd_export`, replace the `written = export(...)` line with:

```python
    selection = lead_type_selection(args.lead_types, logger, args.config)
    if selection is None:
        return EXIT_FAILED
    if selection.active:
        logger.info("Lead types: %s", selection.label())
    written = export(store, settings, logger, min_score=args.min_score,
                     statuses=statuses, call_list=args.call_list, icp_only=args.icp,
                     selection=selection)
```

6. In `cmd_send`, after the subject check and before `reporter = ...`, add:

```python
    selection = lead_type_selection(args.lead_types, logger, args.config)
    if selection is None:
        return EXIT_FAILED
```

and pass `lead_types=selection,` in the `pipeline.send_campaign(...)` call (after `allow_unverified=args.allow_unverified,`).

7. In `build_parser`, after the `enrich_npi` parser block add:

```python
    enrich_sites = subparsers.add_parser(
        "enrich-sites", help="Read each lead's homepage for lead-type evidence.")
    enrich_sites.add_argument("--recheck", action="store_true",
                              help="Read sites that were already checked, too.")
    enrich_sites.set_defaults(func=cmd_enrich_sites)

    lead_types_cmd = subparsers.add_parser(
        "lead-types", help="List the lead types and what each one checks.")
    lead_types_cmd.set_defaults(func=cmd_lead_types)
```

After `send.add_argument("--allow-unverified", ...)`'s block add:

```python
    send.add_argument("--lead-types",
                      help='Only send to leads that need these, e.g. "seo,paid_ads" '
                           '(IDs or names).')
```

And after `export_cmd.add_argument("--status", ...)`'s block add:

```python
    export_cmd.add_argument("--lead-types",
                            help='Only leads that need these, e.g. '
                                 '"website_development,seo" (IDs or names). '
                                 '"all" for no filter.')
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_cli_lead_types.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add cli.py tests/test_cli_lead_types.py
git commit -m "Add lead types to the command line: list, check websites, filter

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: the window: shared selection, picker, Collect/Results/Outreach/Enrich

**Files:**
- Create: `ui_lead_types.py` (`LeadTypeBar`, `LeadTypePicker`)
- Modify: `app.py`
- Modify: `tests/test_app.py` (primary-button label + new `TestLeadTypes`)

**Interfaces:**
- Consumes: `lead_types.load/Selection/prune_selection/grouped/NOT_CHECKED`, `config.lead_types_path`, `storage.utcnow`, `pipeline.check_sites/lead_type_summary`, exporters' `lead_types=`, `recipients_for/send_campaign(lead_types=)`.
- Produces:
  - `ui_lead_types.LeadTypeBar(master, on_choose, on_remove, bg=T.SURFACE)` with `.show(selection, note="")`, `.pill_texts()`, `.hint`
  - `ui_lead_types.LeadTypePicker(master, types, selected_ids, on_done, on_manage=None)` with `._vars`, `.filter_var`, `.visible_ids()`, `.selected_ids()`, `.clear()`, `.use()`
  - `LeadGenApp` attributes: `lead_type_defs`, `lead_types_problem`, `lead_type_bars`, `collect_lead_bar`, `results_lead_bar`, `outreach_lead_bar`, `lead_type_chart`, `lead_type_counts`, `metrics["not_checked"]`, `step_sites`
  - `LeadGenApp` methods: `_load_lead_types()`, `_selection()`, `choose_lead_types()`, `_set_lead_type_selection(ids)`, `_remove_lead_type(type_id)`, `_refresh_lead_type_bars()`, `_recount_lead_types(force=False)`, `_show_lead_type_counts(token, selection, per_type, overall)`, `_lead_types_fingerprint()`, `_with_lead_type_summary(pipeline, result, selection, started)`

- [ ] **Step 1: Write the failing tests**

In `tests/test_app.py`, change the expected enrich label in `test_the_primary_button_describes_the_current_screen` to `"enrich": "Run All Five Passes",`. Then append:

```python
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

    def test_enrich_has_a_check_websites_step(self, gui):
        gui.store.add_leads_batch([lead(Phone="(512) 471-3300", Website="https://a.example")])
        gui.refresh(force=True)
        assert gui.step_sites.status.cget("text") == "1 to check"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_app.py -v`
Expected: FAIL with `AttributeError: 'LeadGenApp' object has no attribute 'lead_type_bars'`, and the primary-button label mismatch.

- [ ] **Step 3: Write `ui_lead_types.py` (bar and picker)**

```python
"""The lead-type controls: the pills-and-button bar and the picker. The
Settings editor joins them in the next change.

Kept out of app.py, which is a window of 2,700 lines already. These widgets
hold no rules about which leads match — that is lead_types.py — they only
show and change what is chosen.
"""

import tkinter as tk
from tkinter import ttk

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
```

- [ ] **Step 4: Wire it into `app.py`**

1. Imports: add `import lead_types` after `import health`, and `import ui_lead_types as LT` after `import ui_theme as T`.

2. In `LeadGenApp.__init__`, right after `self._shown_version = None` (before `T.install(self.root)`), add:

```python
        # Lead types: the definitions, the shared selection's three bars, and
        # the latest per-type counts (worked out off the Tk thread).
        self.lead_type_bars = []
        self.lead_type_counts = {}
        self._lead_type_counts_version = None
        self._recount_token = 0
        self._load_lead_types()
```

and right after `self._build()` add `self._refresh_lead_type_bars()`.

3. In `_build_collect`, between the source card's loop and `self.options_card = T.Card(page, padding=18)`, insert:

```python
        needs = T.Card(page, padding=18)
        needs.pack(fill=tk.X, pady=(0, 14))
        tk.Label(needs.inner, text="WHAT KIND OF LEADS DO YOU NEED?",
                 font=(T.FONT, 7, "bold"), bg=T.SURFACE, fg=T.INK_FAINT,
                 anchor="w").pack(fill=tk.X, pady=(0, 10))
        self.collect_lead_bar = LT.LeadTypeBar(needs.inner, self.choose_lead_types,
                                               self._remove_lead_type)
        self.collect_lead_bar.pack(fill=tk.X)
        self.lead_type_bars.append(self.collect_lead_bar)
```

4. In `_sync_source`, before the final `self._sync_primary()`, add `self._refresh_lead_type_bars()`.

5. In `_build_enrich`, replace `self.step_cms.pack(fill=tk.X)` with:

```python
        self.step_cms.pack(fill=tk.X, pady=(0, 4))
        tk.Frame(card.inner, height=1, bg=T.RULE).pack(fill=tk.X, pady=10)

        self.step_sites = T.StepRow(
            card.inner, 5, "Check websites for lead-type evidence",
            "Reads each practice's homepage once — mobile layout, analytics, "
            "booking, privacy policy and the rest — so lead types can be "
            "matched. Needed for leads collected before lead types existed, and "
            "for NPI leads once Find emails has found their website.",
            lambda: self.start_enrichment("sites"))
        self.step_sites.pack(fill=tk.X)
```

6. In `_build_outreach`, after the `bar` row's last widget (the "Include unverified addresses" checkbutton) and before `actions = tk.Frame(...)`, insert:

```python
        types_row = tk.Frame(controls.inner, bg=T.SURFACE)
        types_row.pack(fill=tk.X, pady=(12, 0))
        tk.Label(types_row, text="Lead types", font=T.F_BODY, bg=T.SURFACE,
                 fg=T.INK).pack(side=tk.LEFT, anchor="n")
        self.outreach_lead_bar = LT.LeadTypeBar(types_row, self.choose_lead_types,
                                                self._remove_lead_type)
        self.outreach_lead_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        self.lead_type_bars.append(self.outreach_lead_bar)
```

7. In `_campaign_filters`, return the selection too:

```python
        return {"icp_only": choice.startswith("Owner-operated"),
                "owners_only": choice.startswith("Owners and"),
                "lead_types": self._selection()}
```

8. In `_build_results`:
   - Add `("not_checked", "Not checked", T.WARN)` as the last entry of the metrics tuple.
   - Between the `charts` frame block and `export = T.Card(page, padding=16)`, insert:

```python
        types_card = T.Card(page, padding=18)
        types_card.pack(fill=tk.X, pady=(0, 14))
        tk.Label(types_card.inner, text="LEADS BY LEAD TYPE", font=(T.FONT, 7, "bold"),
                 bg=T.SURFACE, fg=T.INK_FAINT, anchor="w").pack(fill=tk.X, pady=(0, 10))
        self.lead_type_chart = T.BarChart(
            types_card.inner, empty_text="No lead-type evidence yet — collect from "
                                         "Psychology Today, or run Enrich → Check websites.")
        self.lead_type_chart.pack(fill=tk.X)
```

   - At the end of the export card (after the ICP checkbutton, before `return outer`), add:

```python
        types_row = tk.Frame(export.inner, bg=T.SURFACE)
        types_row.pack(fill=tk.X, pady=(12, 0))
        tk.Label(types_row, text="Lead types", font=T.F_SMALL, bg=T.SURFACE,
                 fg=T.INK_FAINT).pack(side=tk.LEFT, anchor="n", padx=(0, 6))
        self.results_lead_bar = LT.LeadTypeBar(types_row, self.choose_lead_types,
                                               self._remove_lead_type)
        self.results_lead_bar.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.lead_type_bars.append(self.results_lead_bar)
```

9. In `refresh`, after `self.step_cms.set_state(...)`'s statement, add:

```python
        needs_sites = pending["needs_site_check"]
        self.step_sites.set_state(
            "Every website checked" if not needs_sites
            else "{0} to check".format(needs_sites),
            "ok" if not needs_sites else "brand",
            done=not needs_sites and stats["with_website"] > 0)
```

and at the very end of `refresh` (after `self._refresh_next_action(...)`) add `self._recount_lead_types()`.

10. In `_sync_primary`, change `"enrich": "Run All Four Passes",` to `"enrich": "Run All Five Passes",`.

11. In `start_collection`, in **each** of the three `make_job` functions, add `selection = self._selection()` and `started = storage.utcnow()` after `export = self._export_options()`, and call the summary helper right after the pipeline call in `job()`. For Psychology Today:

```python
            def make_job():
                pipeline = self._build_pipeline()
                export = self._export_options()
                selection = self._selection()
                started = storage.utcnow()

                def job():
                    result = pipeline.collect_psychology_today(
                        state_name, state_abbr, owners_only=owners_only,
                        resume=resume)
                    self._with_lead_type_summary(pipeline, result, selection, started)
                    self._after_run(pipeline, result,
                                    "Psychology Today — " + state_name,
                                    export_options=export)
                return job
```

Do the same for `collect_npi(...)` and `import_npis(numbers)`.

12. In `start_enrichment`: change the docstring to `"""mode: find | verify | npi | cms | sites | all."""`. After the `cms` block inside `job()` add:

```python
                if mode in ("sites", "all"):
                    result = pipeline.check_sites(recheck=recheck)
                    added += result.added
                    self._after_run(pipeline, result, "Website check",
                                    quiet=quiet, export_options=per_pass_export)
```

Then change `"All four enrichment passes finished."` to `"All five enrichment passes finished."`, add `"sites": "Checking websites for lead-type evidence…",` to `phases`, and change `"all"` to `"Running all five enrichment passes…"`.

13. In `_export_options`, add `"lead_types": self._selection(),` to the returned dict. In `_export`, pass `lead_types=options.get("lead_types")` to **both** exporter `.export(...)` calls.

14. Add a new section of methods (after `_refresh_row`):

```python
    # ===================================================================
    # Lead types
    # ===================================================================

    def _load_lead_types(self):
        """Read lead_types.json (or the built-ins) and drop any selected type
        that no longer exists. Runs before the window is built, so it logs to
        the LeadGen logger directly; a problem is also shown in Settings."""
        log = logging.getLogger("LeadGen")
        loaded = lead_types.load(config.lead_types_path(), log)
        self.lead_type_defs = loaded.types
        self.lead_types_problem = loaded.problem
        self.settings["lead_types_selected"] = lead_types.prune_selection(
            self.settings.get("lead_types_selected") or [], self.lead_type_defs, log)

    def _selection(self):
        """The current choice, as plain data a worker can take with it."""
        return lead_types.Selection(self.lead_type_defs,
                                    self.settings.get("lead_types_selected") or [])

    def choose_lead_types(self):
        LT.LeadTypePicker(self.root, self.lead_type_defs,
                          self.settings.get("lead_types_selected") or [],
                          on_done=self._set_lead_type_selection,
                          on_manage=lambda: self.show("settings"))

    def _set_lead_type_selection(self, ids):
        self.settings["lead_types_selected"] = list(ids)
        config.save(self.settings)
        self._refresh_lead_type_bars()
        self._recount_lead_types(force=True)

    def _remove_lead_type(self, type_id):
        self._set_lead_type_selection(
            [i for i in self.settings.get("lead_types_selected") or [] if i != type_id])

    def _refresh_lead_type_bars(self):
        selection = self._selection()
        note = ""
        if (selection.active and getattr(self, "source_var", None) is not None
                and self.source_var.get() in ("npi", "paste")):
            note = ("NPI leads have no website yet — run Enrich → Find emails or "
                    "Check websites before they can be matched.")
        for bar in self.lead_type_bars:
            bar.show(selection, note if bar is getattr(self, "collect_lead_bar", None) else "")

    def _lead_types_fingerprint(self):
        return (json.dumps([t.to_dict() for t in self.lead_type_defs], sort_keys=True)
                + json.dumps(self.settings.get("lead_types_selected") or []))

    def _recount_lead_types(self, force=False):
        """Count matches per lead type off the Tk thread. Judging every lead
        against every type is real work at 20,000 leads, and refresh() runs
        on every screen change."""
        if not self.store:
            return
        version = (self.store.data_version(), self._lead_types_fingerprint())
        if not force and version == self._lead_type_counts_version:
            return
        self._lead_type_counts_version = version
        self._recount_token += 1
        token, selection, store = self._recount_token, self._selection(), self.store

        def work():
            try:
                per_type, overall = selection.tally(store.iter_leads())
            except Exception as e:
                self.logger.debug("Could not count lead types: %s", e)
                return
            self.post(self._show_lead_type_counts, token, selection, per_type, overall)

        threading.Thread(target=work, daemon=True).start()

    def _show_lead_type_counts(self, token, selection, per_type, overall):
        if token != self._recount_token:
            return  # a newer count is on its way
        self.lead_type_counts = dict(per_type)
        names = {t.id: t.name for t in selection.types}
        self.lead_type_chart.set_rows(sorted(
            ((names[type_id], count, None) for type_id, count in per_type.items() if count),
            key=lambda row: -row[1]))
        self.metrics["not_checked"].set(
            overall[lead_types.NOT_CHECKED] if selection.active else "—")
        if hasattr(self, "lead_types_card"):
            self.lead_types_card.show_counts(per_type)

    def _with_lead_type_summary(self, pipeline, result, selection, started):
        """Add how this run's leads fit the chosen lead types to its result.
        Runs on the worker and touches no widgets."""
        try:
            summary = pipeline.lead_type_summary(selection, started)
        except Exception as e:
            self.logger.debug("Could not summarise lead types: %s", e)
            return
        if summary:
            pipeline.reporter.log(summary)
            result.message = " ".join(part for part in (result.message, summary) if part)
```

Add `import json` to `app.py`'s standard-library imports.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_app.py -v`
Expected: all PASS (they skip only where Tk is unavailable).

- [ ] **Step 6: Launch the window and look**

Run: `"$PY" app.py`, then check by eye:
- Collect shows the "What kind of leads do you need?" card.
- The picker opens and filters.
- Choosing SEO puts a pill on all three screens, and clicking the pill removes it.
- Results shows the new chart and the Not checked metric.
- Enrich shows step 5.

Close the window.

- [ ] **Step 7: Commit**

```bash
git add ui_lead_types.py app.py tests/test_app.py
git commit -m "Add the lead-type selector to Collect, Results, Outreach and Enrich

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: the window: Settings → Lead Types editor

**Files:**
- Modify: `ui_lead_types.py` (add `LeadTypesCard`, `LeadTypeEditor`)
- Modify: `app.py` (Settings card + `_save_lead_types`)
- Test: `tests/test_app.py` (append `TestLeadTypeSettings`)

**Interfaces:**
- Consumes: `lead_types.LeadType/CHECKS/validate/new_id/copy_name/reset_builtins/parse_list/group_names/save/load`, `config.lead_types_path`.
- Produces:
  - `LT.LeadTypesCard(master, on_save, confirm=None, bg=T.SURFACE)` with `.show(types, counts=None, problem="")`, `.show_counts(counts)`, `.tree`, `.note`, `.message`, `.types`, `._confirm`, `.add()`, `.edit()`, `.duplicate()`, `.toggle()`, `.delete()`, `.reset()`, `._added(lt)`, `._edited(lt)`
  - `LT.LeadTypeEditor(master, lead_type, all_types, on_save)` with `name_var`, `group_var`, `description_var`, `match_var`, `mentions_var`, `not_mentions_var`, `check_vars`, `problems`, `.candidate()`, `.save() -> bool`
  - `LeadGenApp.lead_types_card`, `LeadGenApp._save_lead_types(types)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_app.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `"$PY" -m pytest tests/test_app.py -k LeadTypeSettings -v`
Expected: FAIL with `AttributeError: 'LeadGenApp' object has no attribute 'lead_types_card'`.

- [ ] **Step 3: Add the card and editor to `ui_lead_types.py`**

Change the module docstring's first line to `"""The lead-type controls: the pills-and-button bar, the picker, and the Settings editor.`, add `from tkinter import messagebox` to the imports, and append:

```python
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
```

- [ ] **Step 4: Wire the card into Settings**

In `app.py` `_build_settings`, right after the "WHERE RESULTS ARE EXPORTED" card block (before the next `card = T.Card(...)`), insert:

```python
        # --- lead types
        card = T.Card(page, padding=18)
        card.pack(fill=tk.X, pady=(0, 12))
        self._card_title(card.inner, "LEAD TYPES")
        tk.Label(card.inner,
                 text="What each service's buyer looks like. Switch a type off to "
                      "hide it from the picker, or add your own from the same checks "
                      "plus words on the practice's homepage. Saved to "
                      "lead_types.json beside config.json.",
                 font=T.F_SMALL, bg=T.SURFACE, fg=T.INK_FAINT, anchor="w",
                 justify="left", wraplength=760).pack(fill=tk.X, pady=(0, 10))
        self.lead_types_card = LT.LeadTypesCard(card.inner, on_save=self._save_lead_types)
        self.lead_types_card.pack(fill=tk.X)
        self.lead_types_card.show(self.lead_type_defs, counts=self.lead_type_counts,
                                  problem=self.lead_types_problem)
```

Add to the lead-types method section:

```python
    def _save_lead_types(self, types):
        """Persist an edit from Settings, then redraw everything that shows
        lead types. A deleted type leaves the selection."""
        try:
            lead_types.save(types, config.lead_types_path())
        except OSError as e:
            self.banner.set("error", "Could not save lead types.", str(e))
            return
        self.lead_type_defs = types
        self.lead_types_problem = ""
        kept = lead_types.prune_selection(self.settings.get("lead_types_selected") or [],
                                          types, self.logger)
        if kept != (self.settings.get("lead_types_selected") or []):
            self.settings["lead_types_selected"] = kept
            config.save(self.settings)
        self.lead_types_card.show(types, problem="")
        self._refresh_lead_type_bars()
        self._recount_lead_types(force=True)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `"$PY" -m pytest tests/test_app.py -v`
Expected: all PASS.

- [ ] **Step 6: Launch the window and look**

Run: `"$PY" app.py`, then try these in Settings → Lead Types:
- Switch SEO off: it disappears from the picker.
- Add "Telehealth" with mentions `telehealth`.
- Duplicate a type, then delete the copy (you're asked to confirm).
- Reset to built-in.

Restore anything you changed and close the window.

- [ ] **Step 7: Commit**

```bash
git add ui_lead_types.py app.py tests/test_app.py
git commit -m "Add the Lead Types editor to Settings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Docs: README, GUIDE, in-app help

**Files:**
- Modify: `README.md`, `GUIDE.md`, `ui_help.py`

**Interfaces:** none; text only. `TestGuidance` in `tests/test_app.py` guards that every screen still has help.

- [ ] **Step 1: In-app help (`ui_help.py`)**

In `"collect"`, after the first paragraph (`"Pick one source, set the filters, ..."`), add:

```python
            "Above the options, “What kind of leads do you need?” narrows the "
            "result to practices that show signs of needing what you sell — a "
            "website, SEO, bookkeeping and so on. Nothing chosen means every "
            "lead, as before. Everything collected is kept either way; the "
            "choice decides what the export and the Call List show.",
```

In `"enrich"`, replace `"Four passes. Run them top to bottom; ..."` with `"Five passes. Run them top to bottom; each one uses what the one before it found."`, and add to its list:

```python
                "Check websites — reads each practice's homepage once so lead "
                "types can be matched. Run it after Find emails, and once for "
                "leads collected before lead types existed.",
```

In `"results"`, add to its list:

```python
                "Lead Types (the last column) says which services each practice "
                "looks like it needs, and why. “Not checked” counts leads the app "
                "can't judge yet — usually ones whose website nobody has read.",
```

In `"settings"`, add to its list:

```python
                "Lead Types — switch a service off to hide it, rename it, or add "
                "your own from the same checks plus words on the homepage.",
```

- [ ] **Step 2: README**

Insert a new section before `## The Call List`:

````markdown
## Lead types

Pick the services you sell, and the export, Call List and campaigns narrow to
the practices that show signs of needing them. Each match says why.

| Lead type | Matches when… |
| :--- | :--- |
| Website Development | No website of their own · no mobile layout · copyright 4+ years old · site down |
| SEO | No page title or meta description · no H1 · no schema.org business data |
| Web Accessibility (ADA) | No accessibility statement · most images lack alt text |
| IT Support & Security | No HTTPS · WordPress older than 6.0 · SSL error |
| Digital Marketing / Paid Ads | No analytics tag / no ad tracking pixel |
| Social Media | No social profiles, or only one |
| Email Marketing / Content Writing / Video | No newsletter signup / no blog / no video |
| Graphic Design & Branding | No logo, favicon or share image |
| Software Development | No online booking **and** no client portal **and** no contact form |
| Online Booking / Live Chat | No booking widget / no chat widget |
| Virtual Assistant / Answering | *Good fit:* the owner answers their own line |
| Bookkeeping / Accounting & Tax / Payroll & HR | *Good fit:* owner-operated small practice · $100k+ Medicare billing · 2–25 clinicians |
| Medical Billing (RCM) | The ICP rule below, unchanged |
| Legal & Compliance | No privacy policy, HIPAA notice or terms of use |

**Where the evidence comes from.** The practice's homepage, read once, while
Psychology Today collection is fetching it anyway for the email and phone, so
it costs no extra requests. NPI leads have no website until **Find emails**
finds one. **Enrich → Check websites** (`python cli.py enrich-sites`) reads the
homepage of any lead with a website nobody has checked.

**"Not checked" is not "needs it".** A lead nobody could inspect is counted and
reported, never quietly included or left out. A timeout, a site's bot
protection, or this machine's own dropped connection is never recorded as a
site being down. The "good fit" types are worded as fit, not need: no public
page says whether a practice already has a bookkeeper.

**Matches are worked out when you export, never stored**, so switching from SEO
to Bookkeeping, or editing a type in **Settings → Lead Types**, changes the
next export without scraping again. Types live in `lead_types.json` beside
`config.json`.

```bash
python cli.py lead-types                                   # list them
python cli.py export --lead-types "website_development,seo"
python cli.py send --template pitch.txt --lead-types seo   # dry run
```
````

In the `## Command line` block add:

```bash
python cli.py enrich-sites                            # homepage evidence for lead types
python cli.py lead-types                              # what each lead type checks
```

In the `## Spreadsheet layout` paragraph listing what the tool adds, append `` `Lead Types` `` to the end of that list.

In the project layout table, after the `email_engine.py` row, add:

```markdown
| `site_audit.py` | What a practice's homepage shows: the facts lead types are decided from. |
| `lead_types.py` | The lead-type catalog, its checks, and matching. |
| `ui_lead_types.py` | The lead-type picker, the pills on each screen, and the Settings editor. |
```

Update the test count in `## Tests`: run `"$PY" -m pytest -q | tail -1` and put the actual number in place of `283`.

- [ ] **Step 3: GUIDE.md**

In `## 3. Collecting leads`, after the opening line `Go to the **1. Collect** tab and pick one of three sources.`, add:

```markdown
### Choosing what kind of leads you need

Under **What kind of leads do you need?**, press **Choose lead types…** and tick
the services you sell: Website Development, SEO, Bookkeeping, Legal &
Compliance, and so on. The export and the Call List then show only practices
that look like they need one of them, each with the reason in the **Lead
Types** column. Leave it empty for every lead.

Everything you collect is kept whatever you choose, so you can switch to a
different service later and re-export from **Results** without collecting
again. To change what a lead type looks for, or add your own, go to
**Settings → Lead Types**.
```

In `## 4. Enriching what you collected`, change "Four passes" to "Five passes", and add after `### d. ...`'s section:

```markdown
### e. Check websites for lead-type evidence

Reads each practice's homepage once (mobile layout, analytics, booking, privacy
policy and the rest) so lead types can be matched. Run it after **Find
emails**, which is where NPI leads get their website, and once for any leads you
collected before lead types existed. Sites that time out or turn the app away
aren't recorded; they're tried again next time.
```

- [ ] **Step 4: Run the whole suite**

Run: `"$PY" -m pytest -q`
Expected: every test passes. Confirm the count matches the README.

- [ ] **Step 5: Commit**

```bash
git add README.md GUIDE.md ui_help.py
git commit -m "Document lead types in the README, the guide, and the in-app help

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:**
  - §3 catalog → Task 3. §4.1 facts → Task 2. §4.2 checks → Task 3. §4.3 semantics → Task 3.
  - §5.1 columns → Task 5. §5.2 file → Task 4. §5.3 selection setting → Task 4. §5.4 export → Tasks 5–6.
  - §6 components → Tasks 1–12. §7 data flow → Tasks 7–9.
  - §8 screens → Tasks 11–12; §8 CLI → Task 10.
  - §9 errors → Tasks 1, 4, 7, 9. §10 tests → throughout. §11 docs → Task 13.
- **Deviations from the spec, all recorded in Global Constraints:**
  - The `blocked` fetch outcome.
  - The DNS canary.
  - `dated_copyright` / `outdated_wordpress` returning False on a read site with no year or version.
  - The CLI not inheriting the window's selection.
  - The "good fit" reason reading "likely no in-house finance staff" rather than "bookkeeper", since Accounting & Tax shares the check.
