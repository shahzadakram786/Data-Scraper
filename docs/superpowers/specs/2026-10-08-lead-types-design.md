# Lead Types: pick the service you sell, get the leads that need it

**Date:** 2026-10-08 · **Status:** approved in conversation, awaiting written-spec review
**Sub-project:** 1 of 2. Sub-project 2 (a local-business source for non-medical
prospects) is separate and builds on this one.

---

## 1. Understanding

**What was asked**

- A selector on the lead search where the user picks one or more *lead types*:
  Marketing, Website Development, Bookkeeping, SEO, Social Media, Graphic
  Design, Software Development, Accounting, Legal, and more.
- A lead *matches* a type when that business shows evidence of **needing** the
  service. The user sells these services; the leads are prospective buyers.
- The list is comprehensive and **configurable in the app**: types can be
  switched on/off, renamed, edited, and new ones added.
- Prospects remain the medical practices the existing sources produce
  (Psychology Today, NPI Registry, pasted NPIs). Non-medical prospects come in
  sub-project 2.

**Assumptions confirmed during design**

- Several selected types combine with **any** (OR): "Website Development + SEO"
  returns leads that need either.
- Every match carries a plain-English reason built only from facts actually
  collected, consistent with the app's existing `Why` line.
- Free only: no paid website-audit, technographics or review APIs
  (see memory: free tools only).

**Success criteria**

1. The user picks types, runs a Psychology Today state, and the export and Call
   List contain only matching leads, each with its reason.
2. Changing the selection, or editing a type in Settings, changes the next
   export with **no re-scrape**.
3. A lead the app could not inspect is reported as *not checked*, never
   silently treated as matching or as not matching.
4. Existing exports keep every current column in its current position.

---

## 2. Approach

**Record observations; decide matches at read time.**

While a practice's homepage is being read (it already is, for email and
phone), the app records plain facts about the site. Lead types are rules over
those facts plus columns the app already fills (Role, Practice Size, Annual
Billing, Phone Source, socials). Matching runs when leads are *read*
(exports, Call List, campaign recipients, Results counts) and is never stored.

Rejected alternatives:

- *Filter during collection* (like "Owners only"): discards leads that don't
  match today, so a different selection tomorrow means re-scraping.
- *Store a "Needs" tag at collection*: every tag goes stale the moment a type is
  edited in Settings.

Non-matching leads stay in `leads.db`; they are simply not in that export.

---

## 3. Built-in lead types

Each type lists check IDs from the registry in section 4. Default rule is
`any`. Groups drive the picker layout.

| ID | Name | Group | Checks (`any` unless noted) |
| :--- | :--- | :--- | :--- |
| `website_development` | Website Development | Website & Online | `no_own_site`, `no_mobile_layout`, `dated_copyright`, `site_unreachable` |
| `seo` | SEO | Website & Online | `missing_title_or_description`, `no_h1`, `no_schema_org` |
| `web_accessibility` | Web Accessibility (ADA) | Website & Online | `no_accessibility_statement`, `images_missing_alt` |
| `it_security` | IT Support & Security | Website & Online | `no_https`, `outdated_wordpress`, `ssl_error` |
| `digital_marketing` | Digital Marketing | Marketing | `no_analytics` |
| `paid_ads` | Paid Ads (PPC) | Marketing | `no_ad_pixel` |
| `social_media` | Social Media | Marketing | `no_social_profiles`, `one_social_platform` |
| `email_marketing` | Email Marketing | Marketing | `no_newsletter` |
| `content_writing` | Content Writing | Marketing | `no_blog` |
| `graphic_design` | Graphic Design & Branding | Marketing | `no_logo`, `no_favicon`, `no_share_image` |
| `video_production` | Video Production | Marketing | `no_video` |
| `software_development` | Software Development | Software & Operations | `no_online_booking`, `no_client_portal`, `no_contact_form` — **all** |
| `online_booking` | Online Booking & Scheduling | Software & Operations | `no_online_booking` |
| `live_chat` | Live Chat & Support | Software & Operations | `no_live_chat` |
| `virtual_assistant` | Virtual Assistant / Answering | Software & Operations | `owner_answers_phone` |
| `bookkeeping` | Bookkeeping | Finance & Admin | `owner_small_practice` |
| `accounting_tax` | Accounting & Tax | Finance & Admin | `owner_small_practice`, `billing_over_100k` |
| `payroll_hr` | Payroll & HR | Finance & Admin | `has_staff` |
| `medical_billing` | Medical Billing (RCM) | Finance & Admin | `rcm_icp` |
| `legal_compliance` | Legal & Compliance | Legal | `no_privacy_policy`, `no_hipaa_notice`, `no_terms` |

**Known limits, stated in the app as well as here**

- Bookkeeping, Accounting & Tax, Payroll & HR and Virtual Assistant are
  **fit** signals, not observed need. No public page reveals whether a practice
  already has a bookkeeper. Their reasons are worded as fit:
  "Owner · solo practice — likely no in-house bookkeeper".
- Not offered, because no free and reliable signal exists: Google Business
  Profile completeness, online reviews/reputation, ad spend.

---

## 4. Facts and checks

### 4.1 Site facts (schema v1)

Produced by `site_audit.audit_homepage` from the **homepage only**, the one page
every path already fetches, which also carries the footer links most checks
need. Stored as JSON in `site_facts`.

```json
{
  "v": 1,
  "checked": "2026-10-08",
  "own_site": true,
  "fetch": "ok",
  "status": 200,
  "https": true,
  "viewport": true,
  "copyright_year": 2019,
  "title": true,
  "meta_description": false,
  "h1": true,
  "schema_org": false,
  "analytics": ["google-analytics"],
  "ad_pixels": [],
  "booking": ["simplepractice"],
  "portal": false,
  "contact_form": true,
  "newsletter": [],
  "blog": false,
  "privacy_policy": true,
  "hipaa_notice": false,
  "terms": false,
  "accessibility": false,
  "logo": true,
  "favicon": true,
  "share_image": false,
  "video": false,
  "live_chat": [],
  "images": 14,
  "images_missing_alt": 10,
  "wordpress_version": "5.8.2",
  "builder": "wordpress"
}
```

- `own_site`: `false` when the provider lists no website (Psychology Today
  answered 404) **or** the Website is a marketplace/directory profile
  (`EmailEngine.PLATFORM_DOMAINS`, `DIRECTORY_DOMAINS`). In both cases nothing
  else is fetched and the other facts are absent. Unknown (key absent) when
  no source has looked, which is the normal state for NPI-bulk leads.
- `fetch`: `ok` · `timeout` · `dns` · `ssl` · `http_error` · `connection`.
  `status` is set when an HTTP response arrived.
- Detection lists (`analytics`, `ad_pixels`, `booking`, `newsletter`,
  `live_chat`) name the vendor found, so the reason line and debugging can say
  *what* was seen. Vendor fingerprints live in `site_audit.py` as data tables
  (script URLs, link hosts, well-known IDs such as `G-`, `GTM-`, `AW-`,
  `fbq(`), each with a short comment.
- `copyright_year`: newest year in a `©`/`&copy;`/`Copyright` phrase, ignoring
  ranges' lower bound. `null` if none.
- `wordpress_version`: from `<meta name="generator">` or `ver=` on core assets,
  `null` if not exposed.

### 4.2 Check registry

Each check is `fn(lead, facts, text) -> (True | False | None, label)`. `None`
means *couldn't check*. A check that needs a site returns `None` unless
`own_site` is `true`, `fetch == "ok"` and there is no `audit_error`, except
where noted.

**"A site was read"** below means exactly that condition. Role is only ever
found on a practice's own site, so the owner-based checks (`owner_*`,
`rcm_icp`) return `None` when Role is empty **and** no site was read. An empty
Role after a site *was* read is a real "not the owner".

| Check | True when | Label (example) |
| :--- | :--- | :--- |
| `no_own_site` | `own_site is False` (needs no fetch) | "no website of their own" / "only a growtherapy.com profile" |
| `site_unreachable` | `fetch in (dns, http_error)` | "site down (HTTP 503)" |
| `ssl_error` | `fetch == ssl` | "SSL certificate error" |
| `no_https` | final URL is `http:` | "no HTTPS" |
| `no_mobile_layout` | no viewport meta | "no mobile layout" |
| `dated_copyright` | `copyright_year <= this_year - 4` | "© 2019" |
| `missing_title_or_description` | title or meta description missing/empty | "no meta description" |
| `no_h1` | no `<h1>` | "no H1 heading" |
| `no_schema_org` | no schema.org business markup | "no structured business data" |
| `no_analytics` | `analytics == []` | "no analytics" |
| `no_ad_pixel` | `ad_pixels == []` | "no ad tracking" |
| `no_newsletter` | `newsletter == []` | "no newsletter signup" |
| `no_blog` | `blog is False` | "no blog" |
| `no_logo` / `no_favicon` / `no_share_image` | fact is `False` | "no logo" |
| `no_video` | `video is False` | "no video" |
| `no_online_booking` | `booking == []` | "no online booking" |
| `no_client_portal` | `portal is False` | "no client portal" |
| `no_contact_form` | `contact_form is False` | "no contact form" |
| `no_live_chat` | `live_chat == []` | "no live chat" |
| `no_social_profiles` | site read OK and LinkedIn/Facebook/Instagram/Other Social all empty | "no social profiles" |
| `one_social_platform` | exactly one of them | "only on Facebook" |
| `no_privacy_policy` / `no_hipaa_notice` / `no_terms` | fact is `False` | "no HIPAA notice" |
| `no_accessibility_statement` | `accessibility is False` | "no accessibility statement" |
| `images_missing_alt` | `images >= 3` and missing ≥ 50% | "10 of 14 images lack alt text" |
| `outdated_wordpress` | exposed version below `WORDPRESS_OUTDATED_BELOW = (6, 0)`, a commented constant in `lead_types.py`. `None` when no version is exposed | "WordPress 5.8 (outdated)" |
| `owner_answers_phone` | Role ∈ `OWNERSHIP_ROLES` and Phone Source contains `personal/direct` | "owner answers their own line" |
| `owner_small_practice` | Role ∈ `OWNERSHIP_ROLES` and (Practice Size ≤ 5 or unknown) | "Owner · solo practice — likely no in-house bookkeeper" |
| `billing_over_100k` | Annual Billing ≥ 100,000. `None` when blank | "$231k Medicare billing" |
| `has_staff` | 2 ≤ Practice Size ≤ 25. `None` when blank | "14 clinicians" |
| `rcm_icp` | `storage.is_icp(lead)`, unchanged, subject to the Role rule above | "owner-operated small practice" |
| `mentions` (custom) | homepage text contains any listed phrase | "mentions 'telehealth'" |
| `not_mentions` (custom) | homepage text contains none of them | "doesn't mention 'book online'" |

Keyword matching is case-insensitive, whitespace-normalised, whole-phrase.
`None` when there is no stored `site_text`.

### 4.3 Matching semantics

For one lead and one type:

- **any**: *match* if any check is `True`. *No match* if every check is
  `False`. Otherwise *not checked*.
- **all**: *match* if every check is `True`. *No match* if any is `False`.
  Otherwise *not checked*.

For a selection of several types, a lead is **returned** when it *matches* at
least one. It is counted as **not checked** when it matches none and is *not
checked* for at least one selected type. Those leads are excluded from the
filtered output, but their count is always reported with the action that would
check them.

No selection means no lead-type filter, which is today's behaviour.

---

## 5. Data model

### 5.1 `leads.db`

Three new `INTERNAL_COLUMNS` (stored, never exported), following the existing
`NPI Checked` pattern; `_add_missing_columns` adds them to existing databases:

| Header | Column | Content |
| :--- | :--- | :--- |
| `Site Facts` | `site_facts` | JSON from 4.1 |
| `Site Text` | `site_text` | Homepage visible text, whitespace-collapsed, capped at 15,000 chars, zlib-compressed, base64. Decompressed only when a keyword check is evaluated |
| `Site Checked` | `site_checked` | ISO date the homepage was read. Lets the enrich pass skip leads already checked |

### 5.2 `lead_types.json`

Kept next to `config.json`, gitignored (it's user configuration).

```json
{
  "version": 1,
  "types": [
    {
      "id": "website_development",
      "name": "Website Development",
      "group": "Website & Online",
      "description": "No site of their own, or one that looks dated.",
      "enabled": true,
      "builtin": true,
      "match": "any",
      "checks": ["no_own_site", "no_mobile_layout", "dated_copyright", "site_unreachable"],
      "mentions": [],
      "not_mentions": []
    }
  ]
}
```

- **Load:** start from the built-in catalog. Entries in the file override
  built-ins with the same `id`; non-builtin entries are added. A built-in
  missing from the file (e.g. added by an app update) appears with its
  defaults, so updates can ship new types.
- **Save:** atomic (temp file + `os.replace`), the same pattern as
  `ExcelManager.save_workbook`.
- **IDs** are stable slugs, generated from the name for custom types and
  never changed on rename. Selections reference IDs.
- **Reset to built-in** removes the file's overrides for built-in types.
  Custom types are kept.

### 5.3 `config.json`

New `DEFAULTS` key: `"lead_types_selected": []`, a list of type IDs shared
by Collect, Results and Outreach.

### 5.4 Export

`"Lead Types"` is **appended as the last entry of `EXPORT_LAYOUT`**. It is
computed per lead at export time, not stored and not editable. Value: every
*enabled* type the lead matches, selected types first, as
`Website Development: no mobile layout, © 2019 · SEO: no meta description`.
Applies to both Excel and Google Sheets exporters. `EDITABLE_FIELDS` is
unchanged, so the column is regenerated on every export.

---

## 6. Components

| Unit | Responsibility | Depends on |
| :--- | :--- | :--- |
| `site_audit.py` (new) | `audit_homepage(html, final_url, status, fetch) -> facts`, `visible_text(html) -> str`, `pack_text`/`unpack_text`. Pure: no network, no DB. Vendor fingerprint tables live here | BeautifulSoup/lxml (already used) |
| `lead_types.py` (new) | Built-in catalog (data), check registry, `evaluate(lead, type) -> (status, reasons)`, `filter_leads(leads, selected_ids, types)`, `describe(lead, types, selected_ids) -> str`, `load(path)`/`save(types, path)`/`reset_builtins`, validation | `storage.is_icp`, `storage.OWNERSHIP_ROLES`, `site_audit.unpack_text` |
| `email_engine.py` | Homepage fetch records final URL, status and fetch outcome class. `scrape_website_contact_info` returns `facts` and `text` alongside its current keys, computed from the homepage it already fetched. Marketplace/directory early return yields `{"own_site": false}` | `site_audit` |
| `psychology_today_engine.py` | `_resolve_website` distinguishes **404 (no website listed)** from a failed request, e.g. by returning `(url, listed)` or a sentinel. Today both return `""`, and only the 404 is evidence | — |
| `pipeline.py` | PT collection and Find Emails attach `Site Facts`/`Site Text`/`Site Checked`. New `check_sites(recheck=False)` pass. Run summaries report matches / not checked for the current selection | `site_audit`, `lead_types` |
| `storage.py` | New internal columns. `iter_best_leads(..., lead_types=None)`. Export path accepts the same filter. `"Lead Types"` added to `EXPORT_LAYOUT` | `lead_types` (filter only) |
| `lead_manager.py` | Writes the computed `Lead Types` column. Filtered export | `lead_types` |
| `config.py` | `lead_types_selected` default; `LEAD_TYPES_FILE = "lead_types.json"` | — |
| `cli.py` | `lead-types` (list), `enrich-sites [--recheck]`, `--lead-types` on `export` and `send` | `lead_types`, `pipeline` |
| `app.py` | Collect card + picker dialog, Results pills/chart/metric, Outreach filter, Settings editor (section 8) | `lead_types` |

Lead-type rules and evidence logic live only in `lead_types.py` and
`site_audit.py`. `app.py` stays front-end only, as the README requires.

---

## 7. Data flow

1. **Psychology Today collection.** `process_pt_entry` resolves the website.
   - 404 → `own_site: false` ("listed only on Psychology Today").
   - A marketplace/directory URL → `own_site: false` (profile only).
   - A real site → `scrape_website_contact_info` already fetches the homepage,
     and the audit runs on that HTML. **Zero extra requests.**
   - A failed website lookup → no facts (not checked).
2. **NPI bulk / pasted NPIs.** No website at collection, so no facts. These
   leads are *not checked* until step 3 or 4 inspects them.
3. **Find Emails pass.** When `find_email` discovers and scrapes a site, the
   facts are recorded the same way. When search finds nothing, `own_site`
   stays unknown: a failed search is not proof of no website.
4. **New pass: Enrich → "Check websites for lead-type evidence"**
   (`cli.py enrich-sites`). Every lead with a Website and no `Site Checked`
   (all of them with `--recheck`) gets one homepage fetch. Same worker pool,
   Stop button, progress and run-history recording as the other passes.
   Concurrency comes from the existing `email_workers` setting.
5. **Read time.** Export, Call List, campaign recipients and Results counts all
   call `lead_types.filter_leads` / `describe` with the current selection and
   definitions.

**Deliberately not done:** skipping NPI/email work for leads that don't match
during collection. It would save time, but leads collected under one selection
would come back incomplete under the next.

---

## 8. Screens and CLI

**Collect.** A new card between "Where should the leads come from?" and the
options card:

```
┌ WHAT KIND OF LEADS DO YOU NEED? ─────────────────────────────┐
│ [Website Development ×] [SEO ×]          Choose lead types…  │
│ Leads that need either one. Nothing chosen = every lead.     │
└──────────────────────────────────────────────────────────────┘
```

- **Picker dialog** (shared by Collect, Results and Outreach): filter box;
  types in their groups with a checkbox and one-line description; *Clear*;
  *Manage lead types…* opens Settings. Only enabled types appear.
- With NPI bulk or paste selected and a selection active, a hint says those
  leads need **Enrich → Find emails** or **Check websites** before they can be
  matched.
- **Run summary:** "Collected 312 · 140 match Website Development or SEO ·
  25 not checked yet → Enrich → Check websites".

**Results.**

- The export bar shows the same pills with *Change…*. Re-export applies the new
  selection immediately.
- Metrics strip: add **Not checked** (for the current selection).
- New card **LEADS BY LEAD TYPE**: `T.BarChart` of match counts per enabled
  type.
- Counts are computed in a worker (never on the Tk thread), cached until leads
  or definitions change.

**Outreach.** "Send to" gains the lead-type filter, the same shared selection
and pills, applied in `recipients_for` in addition to the existing audience
filter.

**Settings → Lead Types card.**

- Treeview columns: **On · Lead type · Group · Checks · Matches**.
- Buttons: *Add · Edit · Duplicate · Delete · Reset to built-in*. Built-ins can
  be switched off, renamed and edited but not deleted.
- **Edit dialog:** name, group (editable combobox), description, "Match when
  **any** / **all** of these", checklist of registry checks in plain words,
  *Homepage mentions any of…* and *Homepage doesn't mention any of…*
  (comma-separated phrases).
- Validation: a name is required and must be unique, and at least one check or
  keyword is required.
- Saving writes `lead_types.json` and refreshes the Matches column.
- If the file was unreadable at startup, an amber note says the built-in list
  is in use and names the `.bad` file.

**CLI.**

```bash
python cli.py lead-types                                  # list types, checks, on/off
python cli.py enrich-sites [--recheck]                    # check websites for evidence
python cli.py export --lead-types "website_development,seo"
python cli.py send --template pitch.txt --lead-types seo  # dry run unless --send
```

`--lead-types` accepts IDs or names, case-insensitive. An unknown value is an
error listing the valid ones, never a silent no-filter.

---

## 9. Error handling

- **The audit never breaks a scrape.** It is wrapped exactly like
  `extract_socials`. On failure the lead keeps email/phone/role and its facts
  carry `"audit_error": "<exception type>"` next to the fetch outcome. Any
  `audit_error` makes every site check read `None` (section 4.2).
- **Fetch outcomes are graded.** `dns`, `http_error` and `ssl` are evidence
  (`site_unreachable`, `ssl_error`). `timeout` and `connection` are
  **not checked**: a slow afternoon doesn't make a practice need a new website.
- **Corrupt `lead_types.json`:** log a warning, rename it to
  `lead_types.json.bad` (keeping any older `.bad` by suffixing a timestamp),
  run on built-ins, show the Settings note.
- **Invalid entries:**
  - An unknown check ID evaluates to `None` with one warning per load.
  - An invalid `match` value is treated as `any`.
  - A type with no valid checks or keywords is loaded disabled, with a
    warning.
- **Stale selection:** a selected ID that no longer exists is dropped from
  `lead_types_selected` with a log line.
- **Old facts:** a check needing a key absent from older facts returns `None`.
  `enrich-sites --recheck` refreshes them.
- **Empty pass:** `enrich-sites` that checks nothing finishes `empty`
  (exit code 2), via the existing `_guard_empty`.

---

## 10. Testing

Test-first, in the existing pytest suite. Run with the Python 3.12 interpreter
(`AppData\Local\Programs\Python\Python312`); `python` on PATH lacks the deps.

- **Fixtures** (synthetic, no real provider data, given the PII already in git
  history):
  - `site_modern.html`: viewport, GA4 + GTM, Meta pixel, SimplePractice
    booking, privacy + HIPAA + terms + accessibility links, blog, schema.org,
    og:image, favicon, logo.
  - `site_dated.html`: no viewport, © 2016, WordPress 4.9 generator, no
    analytics, no privacy link, images without alt.
  - The existing `practice_site.html` is reused as a "typical" case.
- **`test_site_audit.py`:** every fact true and false; copyright ranges;
  WordPress version from generator and from `ver=`; empty, garbage, and very
  large input; `pack_text`/`unpack_text` round-trip and cap.
- **`test_lead_types.py`:**
  - Every built-in type matching and not matching.
  - True/False/None for every check; `any` vs `all`; keyword checks with and
    without stored text.
  - Column-based checks (owner, practice size, billing, ICP).
  - Reason wording; `describe` ordering.
  - Load/save round-trip; built-in merge on load; reset; corrupt file →
    `.bad` + built-ins; unknown check; rename keeps selection; deleted ID
    dropped.
- **`test_storage.py`:**
  - `EXPORT_LAYOUT` unchanged except `Lead Types` appended last.
  - New columns added to an old database.
  - `iter_best_leads(lead_types=...)`.
- **`test_pipeline.py`:**
  - PT 404 → `own_site: false`; marketplace profile → `own_site: false`.
  - Failed website lookup → no facts.
  - Audit exception keeps contact info.
  - `check_sites` updates leads, skips checked ones, honours `--recheck` and
    Stop; timeout → not checked.
- **`test_calllist.py`:** Why line includes `needs: …` for the selection.
- **CLI:** `--lead-types` by ID and by name; unknown value errors.
- **`test_performance.py`:** filtering 10,000 leads against 20 types under a
  fixed budget (target < 1 s), with `site_text` decompressed only when a
  keyword check runs.

---

## 11. Documentation

- README: a "Lead types" section (what each type checks, the honesty limits,
  the enrich pass, the CLI flags). Add `site_audit.py` and `lead_types.py` to
  the project layout table; add `enrich-sites` to the command list.
- GUIDE.md: a short step for choosing lead types before collecting, and for
  running "Check websites" on NPI leads.

---

## 12. Out of scope

- Sub-project 2: a local-business source for non-medical prospects. Planned
  next; it will feed the same facts and therefore the same lead types.
- Personalised merge fields from evidence (e.g. `{{Needs}}` in a pitch).
- Google Business Profile, reviews, ad spend: no free reliable signal.
- Any paid API or service.
