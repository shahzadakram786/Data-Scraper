import re
import json
import time
import logging
import requests
from bs4 import BeautifulSoup

import http_pool
from errors import RegionBlockedError, ListingParseError, SourceUnavailableError


class PsychologyTodayEngine:
    """Scrapes the Psychology Today therapist directory for a given state and
    resolves each provider's real (external) website, mirroring the manual
    workflow: search a state, read each listing row, and if the provider lists a
    website, follow it. Profiles that list no website are left for the caller to
    cross-reference against the NPI registry / phone-based lookup instead.

    Psychology Today geo-fences the directory to US traffic: from anywhere else
    /us/therapists/* answers "403 This page is unavailable in your region",
    which used to end a run silently with zero providers. _get detects that and
    transparently re-fetches through r.jina.ai's free reader relay, which
    returns the same HTML (JSON-LD and all). See _fetch_via_relay.
    """

    BASE_URL = "https://www.psychologytoday.com"
    # The provider's own site sits behind this interstitial. It replaced
    # out.psychologytoday.com/.../website-redirect, which now answers HTTP 500
    # for every profile. Unlike the directory pages this one is NOT geo-fenced,
    # so it is always fetched directly.
    WEBSITE_URL = "https://www.psychologytoday.com/us/profile/{pt_id}/website"
    # Free, unauthenticated reader relay. Only ever used for the geo-fenced
    # directory pages — roughly one request per 20 providers.
    RELAY_URL = "https://r.jina.ai/{url}"
    RELAY_HEADERS = {"x-respond-with": "html"}
    REGION_BLOCK_MARKER = "unavailable in your region"

    # A bare python-requests User-Agent gets served the bot wall; this is the
    # header set a real Chrome navigation sends.
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Windows"',
        "Connection": "keep-alive",
    }
    MAX_PAGES = 60  # safety cap (~1200 providers per state) to avoid runaway scraping

    # Hosts that appear in every /website interstitial regardless of provider —
    # analytics, PT's own assets, and the footer's boilerplate links. The
    # provider's real site is the first URL in the payload that isn't one of
    # these.
    BOILERPLATE_DOMAIN_FRAGMENTS = (
        "psychologytoday.com", "policies.google.com", "sussexdirectories.com",
        "apply.workable.com", "datadoghq", "cloudfront.net", "googletagmanager",
        "gstatic", "doubleclick", "schema.org", "w3.org", "google-analytics",
    )
    # The Nuxt payload escapes every slash, so URLs appear in the HTML as
    # "https:" followed by two escaped-slash sequences then the host.
    _ESCAPED_SLASH = chr(92) + "u002F"

    def __init__(self, logger=None, proxies=None):
        self.logger = logger or logging.getLogger(__name__)
        self.session = requests.Session()
        self.session.headers.update(self.HEADERS)
        if proxies:
            self.session.proxies.update(proxies)
        # Set once the directory answers with a region block, so every later
        # page skips the pointless direct attempt and goes straight to relay.
        self._region_blocked = False

    @staticmethod
    def state_slug(state_full_name):
        return state_full_name.strip().lower().replace(" ", "-")

    def iter_listing(self, state_full_name, stop_event=None, max_pages=None,
                     start_page=1):
        """Yields (page_number, providers) as each listing page is scraped, so
        the caller can start working through providers immediately instead of
        waiting for the whole directory, and can checkpoint its position.

        Each provider is a dict: pt_id, profile_url, name, credential, phone,
        city, state_full, zip, is_org.

        **The first page is treated differently from the rest, on purpose.**
        An empty page 40 means the directory ended; an empty page 1 means the
        scraper is broken — the site is unreachable, geo-blocking us, or has
        changed its markup — and raises rather than returning nothing. Before
        this distinction existed a broken scraper looked exactly like an empty
        state: four runs in August 2026 "finished successfully" having saved
        nothing at all, and the breakage went unnoticed for ten days.

        start_page resumes a previous run partway through the directory.
        """
        slug = self.state_slug(state_full_name)
        cap = max_pages or self.MAX_PAGES
        seen_ids = set()
        total = 0
        page = max(1, int(start_page))
        first_page = page

        while page <= cap:
            if stop_event is not None and stop_event.is_set():
                break

            url = f"{self.BASE_URL}/us/therapists/{slug}"
            if page > 1:
                url += f"?page={page}"

            html = self._get(url, critical=(page == first_page))
            if not html:
                # Only reachable for a later page: page one raises instead.
                self.logger.warning(
                    f"Psychology Today page {page} could not be fetched — "
                    f"stopping here with {total} providers collected.")
                break

            entries = self._parse_listing(html)
            if not entries:
                if page == first_page:
                    raise ListingParseError(
                        f"The Psychology Today directory for {state_full_name} "
                        f"loaded, but no providers could be read out of it. "
                        f"Their page markup has most likely changed.")
                break  # genuine end of the directory

            new_entries = [e for e in entries if e["pt_id"] not in seen_ids]
            if not new_entries:
                break  # same providers repeating: the directory has run out

            seen_ids.update(e["pt_id"] for e in new_entries)
            total += len(new_entries)
            self.logger.info(
                f"Psychology Today: page {page} — {len(new_entries)} providers "
                f"({total} total so far)")
            yield page, new_entries
            page += 1
            time.sleep(1.0)  # be polite to PT's servers

    def fetch_listing(self, state_full_name, stop_event=None, max_pages=None):
        """Scrapes the whole state directory listing and returns a flat list of
        providers. Prefer iter_listing when the caller can process providers as
        they arrive."""
        results = []
        for _page, batch in self.iter_listing(
                state_full_name, stop_event=stop_event, max_pages=max_pages):
            results.extend(batch)
        return results

    def self_check(self, state_full_name="Texas"):
        """Fetch one listing page and report what actually worked.

        This is the ten-second version of a run: it answers "is the scraper
        still working today?" before committing to forty minutes of scraping.
        Returns a dict with ok//detail per step; never raises.
        """
        report = {"source": "Psychology Today", "ok": False, "steps": []}

        def step(name, ok, detail):
            report["steps"].append({"name": name, "ok": ok, "detail": detail})

        url = f"{self.BASE_URL}/us/therapists/{self.state_slug(state_full_name)}"
        try:
            html = self._get(url, critical=True)
        except RegionBlockedError as e:
            step("Fetch directory page", False, str(e))
            return report
        except SourceUnavailableError as e:
            step("Fetch directory page", False, str(e))
            return report
        except Exception as e:
            step("Fetch directory page", False, f"{type(e).__name__}: {e}")
            return report

        via = "the r.jina.ai reader relay" if self._region_blocked else "a direct request"
        step("Fetch directory page", True, f"Loaded {state_full_name} via {via}.")

        entries = self._parse_listing(html)
        if not entries:
            step("Parse provider listing", False,
                 "The page loaded but no providers could be read from its "
                 "JSON-LD. Their markup has probably changed.")
            return report
        step("Parse provider listing", True,
             f"Read {len(entries)} providers from page 1.")

        with_credential = sum(1 for e in entries if e.get("credential"))
        step("Read credentials from listing", bool(with_credential),
             f"{with_credential} of {len(entries)} rows carried a credential line.")

        sample = entries[0]
        try:
            website = self._resolve_website(sample["pt_id"])
        except Exception as e:
            website = ""
            step("Resolve a provider website", False, f"{type(e).__name__}: {e}")
        else:
            step("Resolve a provider website", True,
                 f"{sample.get('name')} -> {website or 'no website listed (normal)'}")

        report["ok"] = all(s["ok"] for s in report["steps"][:2])
        report["sample"] = sample
        return report

    @staticmethod
    def _provider_items(data):
        """Yields the per-provider dicts out of a SearchResultsPage block.

        Psychology Today has shipped two shapes for this: `mainEntity` used to
        be the list of providers directly, and is now an ItemList wrapping them
        in `itemListElement`. Both are handled — iterating the dict form gives
        its string keys, which is what produced
        "'str' object has no attribute 'get'".
        """
        main = data.get("mainEntity")
        if isinstance(main, dict):
            main = main.get("itemListElement")
        if isinstance(main, dict):  # a lone provider rather than a list
            main = [main]
        for item in main or []:
            # An unexpected shape should cost one provider, not the whole run.
            if isinstance(item, dict):
                # ItemList entries are sometimes wrapped as ListItem/{item: ...}
                inner = item.get("item")
                yield inner if isinstance(inner, dict) else item

    @staticmethod
    def _credentials_by_id(soup):
        """Maps pt_id -> the credential line shown under the provider's name on
        the listing ("Psychologist, PsyD", "Licensed Professional Mental Health
        Counselor, MS, LPCMH, NCC").

        This is the same string the profile page exposes as `jobTitle`, so
        reading it here saves one HTTP request *per provider* — the difference
        between one relayed request per 20 providers and twenty-one.
        """
        creds = {}
        for row in soup.select(".results-row"):
            link = row.find("a", href=re.compile(r"/us/therapists/[^/]+/\d+"))
            if not link:
                continue
            m = re.search(r"/(\d+)(?:[/?#]|$)", link["href"])
            if not m:
                continue
            el = row.select_one(".profile-subtitle-credentials")
            if el:
                creds[m.group(1)] = re.sub(r"\s+", " ", el.get_text(" ")).strip()
        return creds

    def _parse_listing(self, html):
        soup = BeautifulSoup(html, "lxml")
        credentials = self._credentials_by_id(soup)
        entries = []
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except (ValueError, TypeError):
                continue
            if isinstance(data, list):  # some pages wrap the blocks in an array
                data = next((d for d in data if isinstance(d, dict)
                             and d.get("@type") == "SearchResultsPage"), None)
            if not isinstance(data, dict) or data.get("@type") != "SearchResultsPage":
                continue
            for item in self._provider_items(data):
                url = item.get("url") or item.get("@id") or ""
                m = re.search(r"/(\d+)$", url)
                if not m:
                    continue
                place = item.get("workLocation") or item.get("location") or {}
                if isinstance(place, list):
                    place = next((p for p in place if isinstance(p, dict)), {})
                addr = (place.get("address") if isinstance(place, dict) else {}) or {}
                if not isinstance(addr, dict):
                    addr = {}
                pt_id = m.group(1)
                entries.append({
                    "pt_id": pt_id,
                    "profile_url": url,
                    "name": (item.get("name") or "").strip(),
                    "credential": credentials.get(pt_id, ""),
                    "phone": (item.get("telephone") or "").strip(),
                    "city": addr.get("addressLocality", ""),
                    "state_full": addr.get("addressRegion", ""),
                    "zip": addr.get("postalCode", ""),
                    "is_org": item.get("@type") == "Organization",
                })
        if not entries:
            self.logger.warning(
                "Psychology Today listing page returned no providers — their "
                "page markup may have changed again. Check the JSON-LD shape.")
        return entries

    def get_profile_details(self, pt_id, profile_url):
        """Visits a provider's profile page. Only used as a fallback when the
        listing row carried no credential line — the website no longer needs to
        be read from here (see _resolve_website), and the profile pages are
        geo-fenced too, so each call costs a relayed request.

        Returns {"specialty": ..., "website": ...}.
        """
        html = self._get(profile_url)
        if not html:
            return {"specialty": "", "website": ""}

        specialty = ""
        m = re.search(r'"jobTitle":"([^"]*)"', html)
        if m:
            specialty = m.group(1)

        return {"specialty": specialty, "website": self._resolve_website(pt_id)}

    def _resolve_website(self, pt_id):
        """Reads the provider's external website out of Psychology Today's own
        "My website" interstitial.

        The page is a Nuxt app whose inlined payload carries the target URL with
        every slash escaped. A 404 means the provider lists no website at all,
        which is the cheapest possible signal — no profile page fetch needed to
        find that out.
        """
        try:
            resp = self.session.get(
                self.WEBSITE_URL.format(pt_id=pt_id), timeout=15)
        except requests.RequestException as e:
            self.logger.warning(f"Could not resolve website for profile {pt_id}: {e}")
            return ""
        if resp.status_code == 404:
            return ""  # provider lists no website
        if resp.status_code != 200:
            self.logger.warning(
                f"Website lookup for profile {pt_id} -> HTTP {resp.status_code}")
            return ""
        return self._first_external_url(resp.text)

    @classmethod
    def _first_external_url(cls, html):
        pattern = re.compile(
            r'"(https?:(?:' + re.escape(cls._ESCAPED_SLASH) + r'|/){2}[^"]+)"')
        for m in pattern.finditer(html):
            url = m.group(1).replace(cls._ESCAPED_SLASH, "/").rstrip(chr(92))
            host = url.split("//", 1)[-1].split("/")[0].lower()
            if any(frag in host for frag in cls.BOILERPLATE_DOMAIN_FRAGMENTS):
                continue
            return url
        return ""

    @classmethod
    def _looks_region_blocked(cls, resp):
        return resp.status_code == 403 or cls.REGION_BLOCK_MARKER in resp.text[:20000].lower()

    def _fetch_via_relay(self, url):
        """Re-fetches a geo-fenced directory page through r.jina.ai's free
        reader. Asking for `x-respond-with: html` gets the fully rendered page
        back, JSON-LD included, so _parse_listing works on it unchanged."""
        try:
            # Pooled per worker: every relayed page goes to the same reader host.
            resp = http_pool.session().get(self.RELAY_URL.format(url=url),
                                           headers=self.RELAY_HEADERS,
                                           proxies=self.session.proxies or None,
                                           timeout=90)
            if resp.status_code == 200:
                return resp.text
            self.logger.warning(f"Reader relay for {url} -> HTTP {resp.status_code}")
        except requests.RequestException as e:
            self.logger.warning(f"Reader relay for {url} failed: {e}")
        return None

    def _get(self, url, retries=3, critical=False):
        """Fetch a directory page, relaying around the region block if needed.

        `critical` marks a fetch the run cannot continue without — the first
        listing page. Those raise a typed error explaining what went wrong and
        what to do about it, instead of returning None and letting the caller
        mistake a dead scraper for an empty directory.
        """
        last_problem = "no response"
        for attempt in range(retries):
            if self._region_blocked:
                html = self._fetch_via_relay(url)
                if html or not critical:
                    return html
                raise RegionBlockedError(
                    "Psychology Today refused this request on geography, and "
                    "the free r.jina.ai reader could not fetch the page either.")
            try:
                resp = self.session.get(url, timeout=15)
                if self._looks_region_blocked(resp):
                    # Not transient — retrying direct would just burn attempts.
                    self._region_blocked = True
                    self.logger.info(
                        "Psychology Today is not serving this region directly "
                        "(HTTP 403 'unavailable in your region'). Falling back to "
                        "the free r.jina.ai reader for directory pages — set a US "
                        "proxy/VPN in Settings to go direct instead.")
                    html = self._fetch_via_relay(url)
                    if html or not critical:
                        return html
                    raise RegionBlockedError(
                        "Psychology Today refused this request on geography, "
                        "and the free r.jina.ai reader could not fetch the "
                        "page either.")
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code == 404:
                    return None
                last_problem = f"HTTP {resp.status_code}"
                self.logger.warning(f"GET {url} -> {last_problem}")
            except requests.RequestException as e:
                last_problem = f"{type(e).__name__}: {e}"
                self.logger.warning(
                    f"GET {url} failed (attempt {attempt + 1}/{retries}): {e}")
            time.sleep(2)

        if critical:
            raise SourceUnavailableError(
                f"Could not load the Psychology Today directory after "
                f"{retries} attempts ({last_problem}).")
        return None
