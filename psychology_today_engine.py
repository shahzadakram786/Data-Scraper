import re
import json
import time
import logging
import requests
from bs4 import BeautifulSoup


class PsychologyTodayEngine:
    """Scrapes the Psychology Today therapist directory for a given state and
    resolves each provider's real (external) website, mirroring the manual
    workflow: search a state, open each profile, and if a website is listed,
    follow it. Profiles that list no website are left for the caller to
    cross-reference against the NPI registry / phone-based lookup instead.
    """

    BASE_URL = "https://www.psychologytoday.com"
    REDIRECT_URL = "https://out.psychologytoday.com/us/profile/{pt_id}/website-redirect"
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    }
    MAX_PAGES = 60  # safety cap (~1200 providers per state) to avoid runaway scraping

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)

    @staticmethod
    def state_slug(state_full_name):
        return state_full_name.strip().lower().replace(" ", "-")

    def iter_listing(self, state_full_name, stop_event=None, max_pages=None):
        """Yields one page of providers at a time as each listing page is
        scraped, so the caller can start working through providers immediately
        instead of waiting for the whole directory to be paged through first.
        Each provider is a dict: pt_id, profile_url, name, phone, city,
        state_full, zip, is_org."""
        slug = self.state_slug(state_full_name)
        cap = max_pages or self.MAX_PAGES
        seen_ids = set()
        total = 0
        page = 1
        while page <= cap:
            if stop_event is not None and stop_event.is_set():
                break
            url = f"{self.BASE_URL}/us/therapists/{slug}"
            if page > 1:
                url += f"?page={page}"
            html = self._get(url)
            if not html:
                break
            entries = self._parse_listing(html)
            if not entries:
                break
            new_entries = [e for e in entries if e["pt_id"] not in seen_ids]
            if not new_entries:
                break
            seen_ids.update(e["pt_id"] for e in new_entries)
            total += len(new_entries)
            self.logger.info(
                f"Psychology Today: page {page} — {len(new_entries)} providers "
                f"({total} total so far)")
            yield new_entries
            page += 1
            time.sleep(1.0)  # be polite to PT's servers

    def fetch_listing(self, state_full_name, stop_event=None, max_pages=None):
        """Scrapes the whole state directory listing and returns a flat list of
        providers. Prefer iter_listing when the caller can process providers as
        they arrive."""
        results = []
        for batch in self.iter_listing(state_full_name, stop_event=stop_event, max_pages=max_pages):
            results.extend(batch)
        return results

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

    def _parse_listing(self, html):
        soup = BeautifulSoup(html, "lxml")
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
                entries.append({
                    "pt_id": m.group(1),
                    "profile_url": url,
                    "name": (item.get("name") or "").strip(),
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
        """Visits a provider's profile page. Returns a dict with:
        - specialty: a human-readable job title/credential, if listed
        - website: the resolved external website URL, or "" if none is listed
        """
        html = self._get(profile_url)
        if not html:
            return {"specialty": "", "website": ""}

        specialty = ""
        m = re.search(r'"jobTitle":"([^"]*)"', html)
        if m:
            specialty = m.group(1)

        website = ""
        m = re.search(r'"hasWebsite":(true|false)', html)
        if m and m.group(1) == "true":
            website = self._resolve_website(pt_id)

        return {"specialty": specialty, "website": website}

    def _resolve_website(self, pt_id):
        """Follows Psychology Today's own '/website-redirect' endpoint (the same
        one their 'Visit Website' button uses) and reads the Location header,
        without actually following the redirect to the external site."""
        try:
            resp = requests.get(
                self.REDIRECT_URL.format(pt_id=pt_id),
                headers=self.HEADERS, timeout=10, allow_redirects=False
            )
            if resp.status_code in (301, 302, 303, 307, 308):
                return resp.headers.get("Location", "") or ""
        except requests.RequestException as e:
            self.logger.warning(f"Could not resolve website for profile {pt_id}: {e}")
        return ""

    def _get(self, url, retries=3):
        for attempt in range(retries):
            try:
                resp = requests.get(url, headers=self.HEADERS, timeout=15)
                if resp.status_code == 200:
                    return resp.text
                if resp.status_code == 404:
                    return None
                self.logger.warning(f"GET {url} -> HTTP {resp.status_code}")
            except requests.RequestException as e:
                self.logger.warning(f"GET {url} failed (attempt {attempt + 1}/{retries}): {e}")
            time.sleep(2)
        return None
