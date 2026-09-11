import re
import requests
import time
import logging
from email_engine import EmailEngine

class NPIEngine:
    BASE_URL = "https://npiregistry.cms.hhs.gov/api/"
    
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)

    def fetch_providers(self, state, specialty, max_records=None, exclude_corp=False, min_updated_year=None, stop_event=None):
        """
        Fetch providers from NPI Registry API with optional filters.
        state: 2-letter state abbreviation.
        specialty: Taxonomy description.
        exclude_corp: If True, filters out organizations whose name carries a corporate
        tag (Inc, Corp, Corporation, Incorporated). LLC/PLLC are deliberately kept —
        most solo and small group practices register that way.
        min_updated_year: If specified (integer), filters out records last updated before this year.
        stop_event: Optional threading.Event; checked between pages so a long paginated
        fetch can be aborted mid-flight rather than only between (state, specialty) pairs.
        """
        all_providers = []
        skip = 0
        limit = 200

        while True:
            if stop_event is not None and stop_event.is_set():
                self.logger.info("Stopped by user.")
                break

            params = {
                "version": "2.1",
                "state": state,
                "taxonomy_description": specialty,
                "limit": limit,
                "skip": skip
            }
            
            self.logger.info(f"Fetching records {skip} to {skip + limit} for {specialty} in {state}...")
            
            response = self._make_request(params)
            if not response or "results" not in response:
                break
            
            results = response.get("results", [])
            if not results:
                break
                
            for entry in results:
                provider = self._parse_provider(entry)
                
                # Filter out corporate suffixes if requested
                if exclude_corp and self._is_corporate_entity(provider.get("Organization Name", "")):
                    continue
                
                # Filter by last updated year if requested
                if min_updated_year:
                    last_updated = provider.get("Last Updated", "")
                    if last_updated:
                        try:
                            year = int(last_updated[:4])
                            if year < min_updated_year:
                                continue
                        except ValueError:
                            pass
                
                all_providers.append(provider)
                
            if len(results) < limit:
                break
                
            skip += limit
            if skip >= 1200: # API limit
                self.logger.warning("Reached the NPI API skip limit (1200). Cannot fetch more for this criteria.")
                break
                
            if max_records and len(all_providers) >= max_records:
                # Truncate to max_records if we collected more
                all_providers = all_providers[:max_records]
                break
                
            # Rate limiting: 3 requests per second
            time.sleep(0.34) # ~3 req/sec
            
        return all_providers

    def find_by_number(self, npi_number):
        """Exact lookup by NPI number — used when the number itself is already
        known (e.g. copied by hand from a directory site like ZocDoc that lists
        it directly). This is the most precise possible lookup since an NPI
        number is a unique ID, unlike name-based matching which can be ambiguous.

        Returns a parsed provider dict, or None if the number isn't registered.
        """
        npi_number = str(npi_number).strip()
        if not npi_number.isdigit() or len(npi_number) != 10:
            return None
        params = {"version": "2.1", "number": npi_number}
        response = self._make_request(params)
        results = (response or {}).get("results", [])
        if not results:
            return None
        return self._parse_provider(results[0])

    def find_by_name(self, first_name, last_name, state, city=None):
        """Look up an individual provider's NPI record by exact first/last name + state.
        Used to cross-reference providers found on other sources (e.g. Psychology Today)
        that don't expose an NPI number directly.

        Returns a parsed provider dict, or None if there is no single confident match.
        When multiple providers share the name, narrows by city if given; otherwise
        refuses to guess and returns None so callers don't attach a wrong NPI number.
        """
        if not first_name or not last_name or not state:
            return None
        params = {
            "version": "2.1",
            "first_name": first_name.strip(),
            "last_name": last_name.strip(),
            "state": state,
            "enumeration_type": "NPI-1",
            "limit": 10,
        }
        response = self._make_request(params)
        results = (response or {}).get("results", [])
        if not results:
            return None
        if city:
            narrowed = [r for r in results if self._record_is_in_city(r, city)]
            if not narrowed:
                # A lone same-name record in a different city is not evidence
                # that it's the same person — plenty of therapists share a name
                # within one state. Accepting it here is what attached other
                # people's NPI numbers and phone numbers to these leads.
                self.logger.warning(
                    f"NPI candidates for {first_name} {last_name} are not in "
                    f"{city}, {state} — skipping NPI cross-reference.")
                return None
            results = narrowed
        if len(results) != 1:
            self.logger.warning(
                f"Ambiguous NPI match for {first_name} {last_name} in {state} "
                f"({len(results)} candidates) — skipping NPI cross-reference.")
            return None
        return self._parse_provider(results[0])

    @staticmethod
    def _record_is_in_city(record, city):
        city_upper = city.strip().upper()
        return any(
            (a.get("city") or "").strip().upper() == city_upper
            for a in record.get("addresses", [])
        )

    def find_by_organization(self, org_name, state, city=None):
        """Look up an organization's NPI record by name + state.
        Strips legal suffixes/punctuation and searches with a trailing wildcard since
        the NPI registry requires exact matches otherwise. Refuses to guess when the
        search isn't narrow enough to be confident.

        Returns a parsed provider dict, or None if there is no single confident match.
        """
        if not org_name or not state:
            return None
        cleaned = self._clean_org_name_for_search(org_name)
        if not cleaned:
            return None
        params = {
            "version": "2.1",
            "organization_name": f"{cleaned}*",
            "state": state,
            "enumeration_type": "NPI-2",
            "limit": 10,
        }
        response = self._make_request(params)
        results = (response or {}).get("results", [])
        if not results:
            return None
        if city:
            narrowed = [r for r in results if self._record_is_in_city(r, city)]
            if not narrowed:
                self.logger.warning(
                    f"NPI candidates for organization '{org_name}' are not in "
                    f"{city}, {state} — skipping NPI cross-reference.")
                return None
            results = narrowed
        if len(results) != 1:
            self.logger.warning(
                f"Ambiguous NPI match for organization '{org_name}' in {state} "
                f"({len(results)} candidates) — skipping NPI cross-reference.")
            return None
        return self._parse_provider(results[0])

    @staticmethod
    def _clean_org_name_for_search(name):
        """The NPI registry only supports left-anchored prefix wildcards, matched
        against the exact string it has on file — so "Foo & Bar LLC" won't match
        a query for "Foo Bar*" (the registry stores "Foo AND Bar", or keeps the
        "&", unpredictably). Rewriting punctuation/suffixes to normalize the name
        therefore backfires. Instead, keep just the first couple of words exactly
        as written — distinctive enough for a confident match, short enough that
        punctuation/suffix differences later in the name can't break the prefix.
        """
        words = name.strip().split()
        prefix_words = words[:3] if words and len(words[0]) <= 3 else words[:2]
        return " ".join(prefix_words).strip()

    def _is_corporate_entity(self, name):
        if not name:
            return False
        # Clean special chars/punctuation and split into uppercase words
        cleaned = re.sub(r'[^\w\s]', ' ', name).upper()
        words = set(cleaned.split())
        corp_tags = {
            "INC", "CORP", "CORPORATION", "INCORPORATED"
        }
        return bool(words.intersection(corp_tags))

    def _make_request(self, params, retries=3):
        for attempt in range(retries):
            try:
                response = requests.get(self.BASE_URL, params=params, timeout=15)
                response.raise_for_status()
                return response.json()
            except Exception as e:
                self.logger.error(f"Attempt {attempt + 1} failed: {e}")
                if attempt < retries - 1:
                    time.sleep(2) # Wait before retry
                else:
                    return None

    @staticmethod
    def _authorized_official(basic):
        """An organization (NPI-2) record carries no first_name/last_name — the
        person it names is the "authorized official", the one who registered the
        organization, along with their title. That is the practice owner more
        often than not ("Founder, CEO", "CEO", "Owner"), so it is both the only
        name available for these records and the best decision-maker signal the
        registry publishes. Ignoring it left every organization row in the sheet
        with a blank provider name and a blank Role.

        Returns (first_name, last_name, role) with role normalized to the same
        vocabulary EmailEngine uses, or "" when the title isn't an ownership one.
        """
        first = basic.get("authorized_official_first_name", "") or ""
        last = basic.get("authorized_official_last_name", "") or ""
        title = basic.get("authorized_official_title_or_position", "") or ""
        role = ""
        for pattern, label in EmailEngine.OWNER_TITLE_PATTERNS:
            if re.search(pattern, title, re.IGNORECASE):
                role = label
                # Titles read "Founder, CEO" — the first match wins, but an
                # ownership title always beats a mere directorship.
                if EmailEngine.is_ownership_role(label):
                    break
        return first, last, role

    def _parse_provider(self, entry):
        basic = entry.get("basic", {})
        addresses = entry.get("addresses", [])
        taxonomies = entry.get("taxonomies", [])

        # Primary address (usually the first one or type='LOCATION')
        address_info = {}
        for addr in addresses:
            if addr.get("address_purpose") == "LOCATION":
                address_info = addr
                break
        if not address_info and addresses:
            address_info = addresses[0]
            
        # Specialty
        specialty = ""
        for tax in taxonomies:
            if tax.get("primary", False):
                specialty = tax.get("desc", "")
                break
        if not specialty and taxonomies:
            specialty = taxonomies[0].get("desc", "")

        first_name = basic.get("first_name", "")
        last_name = basic.get("last_name", "")
        role = ""
        if not first_name and not last_name:
            first_name, last_name, role = self._authorized_official(basic)

        return {
            "NPI Number": entry.get("number"),
            "Provider First Name": first_name,
            "Provider Last Name": last_name,
            "Organization Name": basic.get("organization_name", ""),
            "Role": role,
            "Phone": address_info.get("telephone_number", ""),
            "Email": "", # To be filled by EmailEngine
            "Address": address_info.get("address_1", ""),
            "City": address_info.get("city", ""),
            "State": address_info.get("state", ""),
            "Zip": address_info.get("postal_code", ""),
            "Specialty": specialty,
            "Website": "", # To be filled by EmailEngine
            "Outreach Status": "Not Contacted",
            "Last Updated": basic.get("last_updated", ""),
            "Source": "NPI Registry",
            "Phone Source": "NPI Registry" if address_info.get("telephone_number") else ""
        }

