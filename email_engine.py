import re
import time
import logging
import threading
import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from urllib.parse import urlparse, urljoin

# Used by the classmethod-level phone validators, which have no access to the
# per-instance logger the rest of the engine uses.
_module_logger = logging.getLogger(__name__)

class EmailEngine:
    EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

    # tel: links are the most reliable phone signal on a page (an explicit,
    # intentional contact number); the plain-text pattern is only a fallback.
    TEL_HREF_REGEX = r'href=["\']tel:([+\d][\d\-.() \s]{6,17})["\']'
    # The plain-text fallback is anchored on both sides so a 10-digit run that
    # merely happens to sit inside a longer number (an NPI, a licence number, a
    # timestamp, "zip+phone" run-ons) can't be mistaken for a phone number.
    PHONE_TEXT_REGEX = r'(?<!\d)(?:\+?1[-.\s]?)?\(?[2-9]\d{2}\)?[-.\s]?[2-9]\d{2}[-.\s]?\d{4}(?!\d)'

    # Toll-free numbers reach an answering service, call centre or intake line
    # rather than the provider's own practice, so they're kept only as a last
    # resort and never allowed to displace a real geographic number.
    TOLLFREE_AREA_CODES = {"800", "833", "844", "855", "866", "877", "888"}

    # Area codes actually assigned in the North American Numbering Plan. A
    # number can satisfy every structural NANP rule and still be undialable
    # because its area code was never handed out — e.g. "(457) 821-5250",
    # scraped from a provider site, looks perfectly well-formed but 457 is
    # unassigned. Anything outside this set is rejected rather than recorded
    # as a provider's phone number.
    #
    # NANP assigns a handful of new codes each year. An unrecognised area code
    # is logged (see _normalize_phone) so a genuinely new one shows up in the
    # run log and can simply be added here.
    ASSIGNED_AREA_CODES = frozenset("""
        201 202 203 204 205 206 207 208 209 210 212 213 214 215 216 217 218 219
        220 223 224 225 226 227 228 229 231 234 236 239 240 242 246 248 249 250
        251 252 253 254 256 260 262 263 264 267 268 269 270 272 274 276 279 281
        284 289 301 302 303 304 305 306 307 308 309 310 312 313 314 315 316 317
        318 319 320 321 323 325 326 330 331 332 334 336 337 339 340 343 345 346
        347 350 351 352 354 360 361 363 364 365 367 368 380 382 385 386 387 401
        402 403 404 405 406 407 408 409 410 412 413 414 415 416 417 418 419 423
        424 425 428 430 431 432 434 435 437 438 440 441 442 443 445 447 448 450
        458 463 464 468 469 470 472 473 474 475 478 479 480 484 501 502 503 504
        505 506 507 508 509 510 512 513 514 515 516 517 518 519 520 530 531 534
        539 540 541 548 551 559 561 562 563 564 567 570 571 572 573 574 575 579
        580 581 582 584 585 586 587 601 602 603 604 605 606 607 608 609 610 612
        613 614 615 616 617 618 619 620 623 626 628 629 630 631 636 639 640 641
        645 646 647 649 650 651 656 657 658 659 660 661 662 664 667 669 670 671
        672 678 680 681 682 683 684 689 701 702 703 704 705 706 707 708 709 712
        713 714 715 716 717 718 719 720 721 724 725 726 727 728 731 732 734 737
        740 742 743 747 753 754 757 758 760 762 763 765 767 769 770 771 772 773
        774 775 778 779 780 781 782 784 785 786 787 801 802 803 804 805 806 807
        808 809 810 812 813 814 815 816 817 818 819 820 825 826 828 829 830 831
        832 838 839 840 843 845 847 848 849 850 854 856 857 858 859 860 862 863
        864 865 867 868 869 870 872 873 876 878 879 902 903 904 905 906 907 908
        909 910 912 913 914 915 916 917 918 919 920 925 928 929 930 931 934 936
        937 938 939 940 941 943 945 947 949 951 952 954 956 959 970 971 972 973
        978 979 980 984 986 989
    """.split()) | TOLLFREE_AREA_CODES

    # File extensions that regex email matching commonly mistakes for a TLD
    # (e.g. "logo@2x.png" from a CSS background-image rule).
    INVALID_TLDS = {
        "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "tiff",
        "css", "js", "woff", "woff2", "ttf", "eot", "pdf", "json", "xml"
    }

    # Mailbox-style prefixes ranked by how likely they are to reach a human
    # at the practice, used to pick deterministically among several matches.
    PREFERRED_PREFIXES = ("info", "contact", "office", "admin", "hello", "frontdesk", "billing", "support")

    # Wording next to a number on the page that marks it as the provider's own
    # direct line rather than a shared practice line.
    PERSONAL_PHONE_HINTS = (
        "cell", "mobile", "direct", "text me", "text or call", "my number",
        "personal", "私", "reach me", "call me", "my cell", "my direct",
    )
    # Wording that marks a number as a shared/business line — kept, but ranked
    # below a direct line.
    BUSINESS_PHONE_HINTS = (
        "office", "main", "front desk", "reception", "intake", "billing",
        "scheduling", "toll", "answering", "practice line", "clinic",
    )
    # Wording that disqualifies a number outright: it is not a number you can
    # reach this provider on at all.
    REJECT_PHONE_HINTS = (
        "fax", "facsimile", "crisis", "hotline", "suicide", "lifeline",
        "emergency", "911", "988", "poison", "text line",
    )
    # How much of the text immediately preceding a tel: link to treat as its
    # label. Kept short on purpose: a wide window would let a "Fax:" entry
    # elsewhere in the same block disqualify unrelated numbers.
    PHONE_LABEL_CHARS = 40

    # Titles that mark someone as the person who runs the practice. Matched
    # against text near the provider's own name so a page that merely mentions
    # some other practice's founder doesn't tag this lead.
    OWNER_TITLE_PATTERNS = (
        (r'\bfounder\b', "Founder"),
        (r'\bco[-\s]?founder\b', "Co-Founder"),
        (r'\bowner\b', "Owner"),
        (r'\bco[-\s]?owner\b', "Co-Owner"),
        (r'\bC\.?E\.?O\.?\b', "CEO"),
        (r'\bchief executive officer\b', "CEO"),
        (r'\bpresident\b', "President"),
        (r'\bprincipal\b', "Principal"),
        (r'\bmanaging (?:partner|director|member)\b', "Managing Partner"),
        (r'\bpractice owner\b', "Owner"),
        (r'\bclinical director\b', "Clinical Director"),
        (r'\bexecutive director\b', "Executive Director"),
    )
    # How far from the provider's name a title still counts as describing them.
    ROLE_CONTEXT_CHARS = 120

    # Therapist marketplaces and multi-state group practices. A provider with a
    # profile on one of these is an employee or a listed clinician, not the
    # owner — the founder named on such a site is someone else entirely (e.g.
    # Thriveworks' pages name AJ Centore as founder on every clinician's
    # profile). Role detection is skipped for these outright.
    PLATFORM_DOMAINS = frozenset({
        "headway.co", "lifestance.com", "sondermind.com", "thriveworks.com",
        "betterhelp.com", "talkspace.com", "helloalma.com", "alma.com",
        "growtherapy.com", "mindfully.com", "zocdoc.com", "rula.com",
        "psychologytoday.com", "brightside.com", "cerebral.com",
        "octave.health", "twochairs.com", "charliehealth.com", "refreshmh.com",
    })

    # Solo practitioners rarely give themselves a title — they write "I opened
    # my private practice in 2015". These first-person statements can only be
    # about the person whose site it is, so they don't need name-anchoring.
    # Checked only after the name-anchored pass finds nothing.
    SELF_OWNERSHIP_PATTERNS = (
        (r'\bI\s+(?:founded|co-founded)\b', "Founder"),
        (r'\bI\s+(?:own|opened|started|established|launched)\s+(?:my|this|the)\b', "Owner"),
        (r'\bmy\s+(?:own\s+)?(?:private\s+)?practice\b', "Owner"),
        (r'\bfounded\s+(?:my|this)\s+(?:own\s+)?(?:private\s+)?practice\b', "Founder"),
        (r'\bowner\s+(?:and|&)\s+(?:therapist|clinician|founder)\b', "Owner"),
    )

    # Which detected roles count as "this person owns/runs the practice" for the
    # Owners-only filter. Clinical Director and Executive Director are recorded
    # in the Role column but deliberately excluded here — they are senior staff
    # roles, not ownership, so they'd dilute a decision-maker list.
    OWNERSHIP_ROLES = frozenset({
        "Owner", "Co-Owner", "Founder", "Co-Founder", "CEO",
        "President", "Principal", "Managing Partner",
    })

    @classmethod
    def is_ownership_role(cls, role):
        return (role or "").strip() in cls.OWNERSHIP_ROLES

    JUNK_LOCAL_PARTS = {
        "noreply", "no-reply", "donotreply", "do-not-reply",
        "mailer-daemon", "postmaster", "webmaster"
    }

    JUNK_DOMAIN_FRAGMENTS = ("sentry.io", "wixpress.com", "godaddy.com", "cloudflare.com")

    # Free mailboxes are legitimate for a solo practitioner, so they're accepted
    # even though they don't match the practice's own domain.
    FREEMAIL_DOMAINS = {
        "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com",
        "hotmail.com", "live.com", "aol.com", "icloud.com", "me.com", "mac.com",
        "protonmail.com", "proton.me", "comcast.net", "sbcglobal.net", "att.net",
        "verizon.net", "msn.com", "bellsouth.net", "cox.net", "charter.net",
    }

    # Shared across worker threads so a rate-limit hit by any one thread
    # slows down the whole pool, not just the thread that hit it.
    _MIN_DELAY = 0.5
    _MAX_DELAY = 20.0

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self._delay_lock = threading.Lock()
        self._current_delay = self._MIN_DELAY

    def _throttle(self):
        with self._delay_lock:
            delay = self._current_delay
        if delay:
            time.sleep(delay)

    def _report_outcome(self, rate_limited):
        with self._delay_lock:
            if rate_limited:
                self._current_delay = min(self._current_delay * 2, self._MAX_DELAY)
            else:
                self._current_delay = max(self._current_delay * 0.9, self._MIN_DELAY)

    @staticmethod
    def _looks_rate_limited(exc):
        msg = str(exc).lower()
        return any(k in msg for k in ("ratelimit", "rate limit", "429", "too many requests", "blocked"))

    def _is_valid_email(self, email):
        e = email.lower()
        if any(x in e for x in ["example.com", "yourname", "domain.com"]):
            return False
        if any(frag in e for frag in self.JUNK_DOMAIN_FRAGMENTS):
            return False
        local, _, domain = e.partition("@")
        if local in self.JUNK_LOCAL_PARTS:
            return False
        tld = domain.rsplit(".", 1)[-1] if "." in domain else ""
        if tld in self.INVALID_TLDS:
            return False
        return True

    def _pick_best_email(self, emails):
        def score(email):
            local = email.split("@")[0].lower()
            for i, prefix in enumerate(self.PREFERRED_PREFIXES):
                if local.startswith(prefix):
                    return i
            return len(self.PREFERRED_PREFIXES)
        return sorted(emails, key=lambda e: (score(e), e))[0]

    def find_email(self, lead_data):
        org_name = lead_data.get("Organization Name")
        city = lead_data.get("City")
        state = lead_data.get("State")
        
        if not org_name:
            # Try with provider name if org name is missing
            org_name = f"{lead_data.get('Provider First Name')} {lead_data.get('Provider Last Name')}"
            
        search_query = f"{org_name} {city} {state} medical practice website"
        website = self._find_website(search_query)
        
        email = ""
        if website:
            lead_data["Website"] = website
            email = self._scrape_website_for_email(website)
            
        if not email:
            # Fallback: direct email search
            email_query = f"{org_name} {city} {state} email contact"
            email = self._search_for_email_directly(email_query)
            
        lead_data["Email"] = email
        return lead_data

    def scrape_website_contact_info(self, url, person_name=None):
        """Scrape an already-known website for the provider's email, phone and
        ownership role in a single pass over the same pages.

        Returns {"email", "phone", "phone_confidence", "role"}. Any value may
        be "" if nothing trustworthy was found. When person_name is given, the
        provider's own direct line is preferred over the practice's shared
        line, and "role" reports whether the site describes them as the
        Owner/Founder/CEO of the practice.
        """
        empty = {"email": "", "phone": "", "phone_confidence": "", "role": ""}
        if not url:
            return dict(empty)
        site_domain = self._registrable_domain(urlparse(url).netloc)
        is_platform = self.is_platform_site(url)
        emails = set()
        candidates = []
        role = ""
        for page, text in self._iter_candidate_pages(url):
            for e in re.findall(self.EMAIL_REGEX, text):
                if self._is_valid_email(e) and self._belongs_to_site(e, site_domain):
                    emails.add(e)
            candidates.extend(self._extract_phones(text))
            if not role and not is_platform:
                try:
                    role = self.detect_role(text, person_name)
                except Exception as e:  # never let a parse hiccup lose the contact info
                    self.logger.debug(f"Role detection failed on {page}: {e}")
            # Stop as soon as there's nothing left to gain: an email, a
            # personal number and a role. A personal number is worth reading
            # the extra pages for rather than settling for the main line.
            if emails and role and any(c["kind"] == "personal" for c in candidates):
                break
        phone, confidence = self._choose_phone(candidates)
        return {
            "email": self._pick_best_email(emails) if emails else "",
            "phone": phone,
            "phone_confidence": confidence,
            "role": role,
        }

    @classmethod
    def _choose_phone(cls, candidates):
        """Picks the number most likely to reach the provider personally, and
        describes where it came from. A number the site labels as a cell or
        direct line wins over an unlabelled one, which in turn wins over one
        labelled as the office/front-desk line; a tel: link beats a number
        merely printed in the page text; and a toll-free intake number is
        always last. Returns ("", "") when there is nothing usable."""
        if not candidates:
            return "", ""
        kind_rank = {"personal": 0, "unknown": 1, "business": 2}
        best = sorted(candidates, key=lambda c: (
            cls.is_tollfree(c["phone"]),
            kind_rank.get(c["kind"], 1),
            not c["link"],
            c["phone"],
        ))[0]
        label = {
            "personal": "personal/direct",
            "business": "business line",
            "unknown": "tel: link" if best["link"] else "page text",
        }[best["kind"]]
        if best["kind"] != "unknown":
            label += " (tel: link)" if best["link"] else " (page text)"
        if cls.is_tollfree(best["phone"]):
            label += " (toll-free)"
        return best["phone"], label

    @staticmethod
    def _registrable_domain(netloc):
        """"www.foo.co.uk:443" -> "foo.co.uk". Good enough to tell "this
        practice's own domain" from "some vendor mentioned in the markup"."""
        host = (netloc or "").split(":")[0].lower().lstrip(".")
        parts = [p for p in host.split(".") if p]
        if len(parts) <= 2:
            return ".".join(parts)
        # Two-part public suffixes we actually see (co.uk, com.au, ...)
        if len(parts[-1]) == 2 and parts[-2] in ("co", "com", "net", "org", "gov", "ac"):
            return ".".join(parts[-3:])
        return ".".join(parts[-2:])

    @classmethod
    def _belongs_to_site(cls, email, site_domain):
        """Only keep an address that plausibly belongs to the practice whose
        site we're reading: one on the site's own domain, or a personal
        mailbox. Provider sites routinely embed third-party addresses in
        markup, comments and embedded fonts/scripts (theme authors, font
        foundries, web designers, booking widgets); those are somebody else's
        contact details and must never be recorded as the provider's."""
        domain = email.split("@")[-1].lower()
        if domain in cls.FREEMAIL_DOMAINS:
            return True
        if not site_domain:
            return True
        reg = cls._registrable_domain(domain)
        return reg == site_domain or domain.endswith("." + site_domain)

    def find_email_by_phone(self, phone):
        """Reverse-lookup fallback for providers with no listed website: search
        the phone number itself, the way a person pasting it into a browser
        would, and see if a contact email surfaces in the results."""
        if not phone:
            return ""
        query = f'"{phone}" email contact'
        return self._search_for_email_directly(query)

    def find_all_emails_by_phone(self, phone, max_results=8):
        """Like find_email_by_phone, but returns every distinct email found
        across the search results instead of picking just one — used for the
        ZocDoc/NPI import flow, where the user wants everything a phone-number
        search turns up, not just the single most-likely mailbox."""
        if not phone:
            return []
        return self._search_for_all_emails(f'"{phone}" email contact', max_results=max_results)

    def find_all_emails_by_name(self, full_name, city="", state="", max_results=8):
        """Fallback for find_all_emails_by_phone when the phone search turns up
        nothing: search the provider's full name instead, the way a person
        would type it into a browser after a phone search comes up empty."""
        if not full_name:
            return []
        query = f'"{full_name}" {city} {state} email contact'.strip()
        return self._search_for_all_emails(query, max_results=max_results)

    def _search_for_all_emails(self, query, max_results=8):
        self._throttle()
        emails = set()
        try:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=max_results)
                for r in results:
                    body = r.get('body', '')
                    for e in re.findall(self.EMAIL_REGEX, body):
                        if self._is_valid_email(e):
                            emails.add(e)
            self._report_outcome(rate_limited=False)
        except Exception as e:
            self.logger.error(f"Error searching for all emails: {e}")
            self._report_outcome(rate_limited=self._looks_rate_limited(e))
        return sorted(emails)

    def _find_website(self, query):
        self._throttle()
        try:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=5)
                for r in results:
                    url = r.get('href', '')
                    # Filter out common non-practice sites
                    if any(x in url.lower() for x in ["nppes", "npiregistry", "yelp", "facebook", "linkedin", "healthgrades", "vitals", "doximity"]):
                        continue
                    self._report_outcome(rate_limited=False)
                    return url
            self._report_outcome(rate_limited=False)
        except Exception as e:
            self.logger.error(f"Error finding website: {e}")
            self._report_outcome(rate_limited=self._looks_rate_limited(e))
        return ""

    def _scrape_website_for_email(self, url):
        try:
            site_domain = self._registrable_domain(urlparse(url).netloc)
            emails = set()
            for page, text in self._iter_candidate_pages(url):
                for e in re.findall(self.EMAIL_REGEX, text):
                    if self._is_valid_email(e) and self._belongs_to_site(e, site_domain):
                        emails.add(e)
                if emails:
                    break
            return self._pick_best_email(emails) if emails else ""
        except Exception as e:
            self.logger.error(f"Error scraping website: {e}")
        return ""

    # Link text / hrefs worth following from the homepage. Contact pages carry
    # the phone and email; about/team pages are where an ownership title
    # actually appears.
    INTERESTING_LINK_RE = re.compile(
        r'contact|about|meet|our[-\s]?team|\bteam\b|staff|who[-\s]?we[-\s]?are'
        r'|\bbio\b|therapist|counselor|provider', re.I)
    # Fallback paths, tried only when the homepage exposes no useful links.
    FALLBACK_PATHS = ("/contact", "/about", "/about-us", "/contact-us")
    # Cap on pages fetched per site, so one sprawling site can't stall a run.
    MAX_PAGES_PER_SITE = 5

    def _iter_candidate_pages(self, url):
        """Yields (page_url, page_html) for the site's homepage and the few
        contact/about pages it actually links to.

        Following the homepage's own navigation is both faster and more
        accurate than guessing fixed paths: practices use every variant of
        /about-me, /meet-the-team, /our-therapists, and guessing them all would
        mean a dozen wasted requests per provider across a whole state."""
        parsed = urlparse(url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"

        home = self._fetch(url)
        if home is None:
            # Homepage unreachable — still try the usual paths directly.
            for path in self.FALLBACK_PATHS:
                html = self._fetch(base_url + path)
                if html is not None:
                    yield base_url + path, html
            return
        yield url, home

        targets = []
        seen = {url.rstrip("/"), base_url.rstrip("/")}
        try:
            for a in BeautifulSoup(home, "lxml").find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("mailto:", "tel:", "#", "javascript:")):
                    continue
                if not self.INTERESTING_LINK_RE.search(f"{href} {a.get_text(' ')}"):
                    continue
                full = urljoin(base_url, href).split("#")[0]
                if urlparse(full).netloc != parsed.netloc:
                    continue  # stay on the practice's own site
                if full.rstrip("/") in seen:
                    continue
                seen.add(full.rstrip("/"))
                targets.append(full)
        except Exception as e:
            self.logger.debug(f"Could not read links from {url}: {e}")

        if not targets:
            targets = [base_url + p for p in self.FALLBACK_PATHS]

        for page in targets[:self.MAX_PAGES_PER_SITE - 1]:
            html = self._fetch(page)
            if html is not None:
                yield page, html

    @staticmethod
    def _fetch(page):
        try:
            response = requests.get(page, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            if response.status_code == 200:
                return response.text
        except requests.RequestException:
            pass
        return None

    def _extract_phones(self, html_text):
        """Finds every phone number on the page and classifies it by how it was
        published and what the surrounding wording says it is.

        Returns a list of dicts: {phone, link (True if a tel: link), kind}
        where kind is "personal", "business" or "unknown". Numbers whose
        context marks them as a fax, crisis line or emergency number are
        dropped outright — those are never a way to reach the provider.
        """
        soup = BeautifulSoup(html_text, "lxml")
        # phone -> {"link": bool, "contexts": [str, ...]}
        info = {}

        def note(phone, is_link, context):
            if not phone:
                return
            rec = info.setdefault(phone, {"link": False, "contexts": []})
            rec["link"] = rec["link"] or is_link
            rec["contexts"].append((context or "").lower())

        # tel: links — the label saying what the number is ("Fax:", "Cell:")
        # sits in the text node immediately before the link, so the context is
        # just that plus the link's own text.
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.lower().startswith("tel:"):
                continue
            label = a.find_previous(string=True) or ""
            note(self._normalize_phone(href[4:]), True,
                 f"{label[-self.PHONE_LABEL_CHARS:]} {a.get_text(' ')}")

        # Plain-text numbers. A number's label sits between it and whatever
        # number came before it ("Office: 555-1111 Fax: 555-2222"), so that gap
        # — not a fixed-width window — is the context. A fixed window would
        # reach back into the *previous* entry and misread its label.
        text = soup.get_text(" ")
        prev_end = 0
        for m in re.finditer(self.PHONE_TEXT_REGEX, text):
            gap = text[prev_end:m.start()]
            note(self._normalize_phone(m.group(0)), False, gap[-self.PHONE_LABEL_CHARS:])
            prev_end = m.end()

        found = []
        for phone, rec in info.items():
            joined = " ".join(rec["contexts"])
            # If the page labels this number as a fax or crisis line anywhere,
            # it is one — no other mention makes it reachable.
            if any(h in joined for h in self.REJECT_PHONE_HINTS):
                continue
            if any(h in joined for h in self.PERSONAL_PHONE_HINTS):
                kind = "personal"
            elif any(h in joined for h in self.BUSINESS_PHONE_HINTS):
                kind = "business"
            else:
                kind = "unknown"
            found.append({"phone": phone, "link": rec["link"], "kind": kind})
        return found

    @classmethod
    def is_platform_site(cls, url):
        """True for therapist marketplaces / large group practices, where a
        listed provider is a clinician rather than the owner."""
        return cls._registrable_domain(urlparse(url or "").netloc) in cls.PLATFORM_DOMAINS

    def detect_role(self, html_text, person_name):
        """Returns the ownership title this page gives the named person
        ("Owner", "Founder", "CEO", ...), or "" if it doesn't describe them as
        running the practice.

        The title has to appear near the person's own name: therapist sites
        routinely name other people (a group practice's founder, a supervisor,
        a testimonial), so an unanchored keyword search would tag almost
        everyone as an owner.
        """
        if not person_name:
            return ""
        text = re.sub(r'\s+', ' ', BeautifulSoup(html_text, "lxml").get_text(" "))
        surname = person_name.strip().split()[-1] if person_name.strip() else ""
        if len(surname) < 3:
            return ""
        for m in re.finditer(r'\b%s\b' % re.escape(surname), text, re.IGNORECASE):
            window_start = max(0, m.start() - self.ROLE_CONTEXT_CHARS)
            window_end = m.end() + self.ROLE_CONTEXT_CHARS
            for pattern, label in self.OWNER_TITLE_PATTERNS:
                for t in re.finditer(pattern, text[window_start:window_end], re.IGNORECASE):
                    t_start = window_start + t.start()
                    t_end = window_start + t.end()
                    # The title must describe *this* person: everything between
                    # the name and the title has to stay inside one sentence.
                    # Without this, "Jane Smith is a therapist here. Our founder,
                    # Bob Jones, ..." would tag Jane as the founder.
                    between = text[min(m.end(), t_end):max(m.start(), t_start)]
                    if re.search(r'[.!?;|•]\s', between) or len(between) > self.ROLE_CONTEXT_CHARS:
                        continue
                    return label

        # No explicit title next to the name — fall back to first-person
        # ownership statements ("I opened my private practice"). These are only
        # safe on a page that is actually about this provider: on a group
        # practice's site the same wording belongs to whichever clinician's bio
        # it is, which would tag everyone there as an owner.
        if not re.search(r'\b%s\b' % re.escape(surname), text, re.IGNORECASE):
            return ""
        for pattern, label in self.SELF_OWNERSHIP_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return label
        return ""

    @classmethod
    def _normalize_phone(cls, raw):
        """Normalizes to "(AAA) NNN-XXXX", returning None for anything that
        isn't a dialable North American number. Ten digits alone is not enough:
        pages are full of 10-digit runs that are not phone numbers (NPI
        numbers, licence numbers, order IDs), so the number must also satisfy
        the NANP rules below or it is discarded rather than recorded as a
        provider's contact number."""
        digits = re.sub(r'\D', '', raw)
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        if len(digits) != 10:
            return None
        if not cls._is_valid_nanp(digits):
            return None
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"

    @classmethod
    def _is_valid_nanp(cls, digits):
        """NANP rules: the area code and the exchange code both start 2-9, the
        area code's middle digit pair can't be '11' (those are service codes
        like 911/411), and 555-01XX is the reserved fictional range. The area
        code must also be one that NANP has actually assigned."""
        if len(digits) != 10:
            return False
        area, exchange, line = digits[0:3], digits[3:6], digits[6:10]
        if area[0] in "01" or exchange[0] in "01":
            return False
        if area[1:] == "11" or exchange[1:] == "11":
            return False
        if exchange == "555" and line.startswith("01"):
            return False
        if len(set(digits)) == 1:  # 0000000000, 1111111111, ...
            return False
        if area not in cls.ASSIGNED_AREA_CODES:
            _module_logger.info(
                f"Discarding {area}-{exchange}-{line}: area code {area} is not "
                f"an assigned NANP area code. If NANP has since assigned it, "
                f"add it to EmailEngine.ASSIGNED_AREA_CODES.")
            return False
        return True

    @classmethod
    def is_valid_phone(cls, phone):
        """Public check used by callers that receive a phone number from
        somewhere other than a scraped page (the NPI registry, a directory
        listing) and still need to know it is genuinely dialable."""
        digits = re.sub(r'\D', '', str(phone or ""))
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        return cls._is_valid_nanp(digits)

    @classmethod
    def is_tollfree(cls, phone):
        digits = re.sub(r'\D', '', str(phone or ""))
        if len(digits) == 11 and digits.startswith('1'):
            digits = digits[1:]
        return len(digits) == 10 and digits[0:3] in cls.TOLLFREE_AREA_CODES

    @classmethod
    def _pick_best_phone(cls, phones):
        """Picks the most frequently-occurring number across the pages checked
        (the main contact number tends to repeat in header/footer/contact page),
        preferring a geographic number over a toll-free intake line."""
        from collections import Counter
        counts = Counter(phones)
        return sorted(set(phones), key=lambda p: (cls.is_tollfree(p), -counts[p], p))[0]

    def _search_for_email_directly(self, query):
        self._throttle()
        try:
            with DDGS() as ddgs:
                results = ddgs.text(query, max_results=5)
                for r in results:
                    body = r.get('body', '')
                    found = [e for e in re.findall(self.EMAIL_REGEX, body) if self._is_valid_email(e)]
                    if found:
                        self._report_outcome(rate_limited=False)
                        return self._pick_best_email(set(found))
            self._report_outcome(rate_limited=False)
        except Exception as e:
            self.logger.error(f"Error searching email directly: {e}")
            self._report_outcome(rate_limited=self._looks_rate_limited(e))
        return ""
