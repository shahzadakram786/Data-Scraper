# Medical Provider Lead Generation System

Collects medical provider leads from public sources, finds and **verifies** their
contact details, scores them by how useful they are to contact, and exports to
Excel or Google Sheets.

Everything it uses is free. No paid APIs, no keys, no subscriptions.

```bash
pip install -r requirements.txt
python app.py          # desktop app
python cli.py doctor   # check every source is working
```

> **New here, or handing this to someone else?** Read **[GUIDE.md](GUIDE.md)** —
> a step-by-step walkthrough of setup, collecting, enriching, reading the
> output, and sending. This README is the reference for how it works; the guide
> is how to use it.

---

## What it does

| Source | What it gives you |
| :--- | :--- |
| **Psychology Today** | Walks a state's therapist directory, then follows each provider's *own website* for their email, direct phone, ownership role and social profiles. |
| **NPI Registry** | Bulk provider records by state and specialty, straight from the federal registry. High volume; the registry publishes no email addresses. |
| **Paste NPI numbers** | For numbers copied by hand (e.g. from ZocDoc, which blocks automated access). Looks each one up and searches for contact details. |
| **CMS Medicare data** | Enrichment, not collection. Attaches annual billing volume, practice size and the phone number each provider filed with Medicare. |

Results are filed one tab per state and source — `Texas - Psychology Today`,
`Ohio - NPI Registry` — colour-coded by platform, with a Summary tab in front.

---

## Email verification

Every address is checked before it reaches your list. This is the difference
between a list you can send to and a list that burns your sending domain.

| Status | Meaning |
| :--- | :--- |
| `valid` | The mail server confirmed this mailbox exists. |
| `role` | Shared mailbox (`info@`, `office@`) — deliverable, but not one person. |
| `accept-all` | The domain accepts every address, so this one can't be confirmed. |
| `unknown` | Couldn't be checked (port 25 blocked, timeout, or greylisting). |
| `risky` | Valid syntax, but the domain or mailbox looks like someone else's. |
| `invalid` / `disposable` | No mail server, rejected mailbox, or a throwaway provider. |

How it works, all free:

1. **MX lookup** — a domain with no mail exchanger cannot receive mail at all.
   Cached per domain, so 400 addresses across 120 practices cost 120 lookups.
2. **SMTP conversation** — `MAIL FROM`, then `RCPT TO`, and the server says
   whether the mailbox exists. **No mail is ever sent**; the session closes
   before `DATA`.
3. **Catch-all detection** — before asking about the real address, it asks about
   a random one. A server that accepts `x7f3q9k2@domain` accepts everything, so
   its "yes" proves nothing, and the address is reported `accept-all` instead of
   `valid`.

Addresses that would bounce are **removed**, not exported with a warning label.

> **Port 25.** Step 2 needs outbound port 25, which most home and office ISPs
> block. The app finds out once and remembers it for two hours (in
> `.cache/network.json`), so runs don't each open with an 8-second timeout —
> then keeps running with MX-only checking. Dead domains are still caught;
> addresses are reported `unknown` rather than `valid`. A VPN that allows port
> 25, or running from a server, restores full verification. **Health Check
> always re-tests from scratch**, so press it after connecting a VPN.

### Address inference

For a provider with a practice website but no published address, the standard
mailbox patterns (`first.last@`, `flast@`, `first@`, …) are generated and then
**confirmed against that practice's own mail server**. Only a confirmed mailbox
is recorded. If the domain is a catch-all, or port 25 is blocked, nothing is
returned — a guess nobody confirmed is fiction, and shipping it would undo the
point of verifying at all.

---

## Medicare billing volume and practice size

An enrichment pass (**Enrich → Add Medicare billing volume**, or
`python cli.py enrich-cms`) looks each provider up in two free federal datasets
and attaches:

| Column | From | Why it matters |
| :--- | :--- | :--- |
| `Annual Billing` | Medicare Physician & Other Practitioners | What the provider bills Medicare a year — this is what sizes an RCM contract |
| `Medicare Paid` | same | What they actually received |
| `Patient Volume` | same | Medicare beneficiaries treated |
| `Practice Size` | Doctors & Clinicians | How many clinicians bill under that group |
| `Phone Verified` | Doctors & Clinicians | Whether your number matches the one they filed with Medicare |

Real output from this project's own leads:

```
score=63   $547,775   benes=572   practice size=1881   Psychiatry & Neurology
score=55   $230,570   benes=371   practice size=14     Psychiatry & Neurology
score=49   $23,590    benes=14    practice size=-      Psychiatry & Neurology
```

### Coverage is partial, and that is the source, not a bug

Only Medicare-enrolled providers appear, and CMS suppresses anyone with fewer
than 11 beneficiaries. Measured against this project's real data:

| | Psychology Today leads | ZocDoc / NPI leads |
| :--- | :--- | :--- |
| Has billing data | **0%** | **~27%** |
| Has practice size / filed phone | **20%** | **~40%** |

Most therapists are cash-pay or commercial-insurance only and never appear in
Medicare data at all. A blank here means "not a Medicare biller", **not** "a
worse lead" — which is why these columns are a bonus to the lead score rather
than part of its base, and why a provider with no record is never pushed down
the list for it.

> **Not a "collections gap".** The difference between submitted charges and
> Medicare payments is overwhelmingly the contractual write-down every provider
> takes against the Medicare fee schedule — normal economics, not uncollected
> revenue. This tool deliberately does not compute or present that difference as
> a problem to sell into. The honest signal is billing **volume**.

data.cms.gov geo-fences its API exactly like Psychology Today does, so the same
free reader relay handles it — about 2 seconds per lookup, or much faster with a
US proxy set in Settings.

---

## NPI records: a second phone number, and gender

**Enrich → Match NPI records** (`python cli.py enrich-npi`) looks every lead up
in the federal NPI Registry and adds:

| Column | Meaning |
| :--- | :--- |
| `NPI Phone` | The practice phone on the provider's NPI record. `Phone` keeps the number from their own website; this sits beside it, so there are two to try. A lead with no phone at all gets it in `Phone` too. |
| `Gender` | From the provider's own NPI record. Blank for organisations, and for anyone without a confident match — never guessed from a name. |

Address, city, ZIP, specialty and practice name are filled in only where
collection left them empty. Psychology Today leads saved without an NPI number
(`PT-…`) are tried again by name, city and state, and take the number only when
exactly one provider matches. New Psychology Today, NPI and ZocDoc leads get
both columns while they are collected, so the pass is mainly for leads you
already have.

---

## Spreadsheet layout

Every state tab uses the working sheet's column order, so rows paste straight
into it:

```
NPI Number · Provider First Name · Provider Last Name · Organization Name ·
Email · Phone · Outreach Status · Comments · (unnamed) · Date · Address · City ·
State · Zip · Specialty · Website · Last Updated · Source · Phone Source ·
All Emails Found · Role · LinkedIn · Facebook · Instagram · Other Social
```

followed by what this tool adds: `Gender`, `NPI Phone`, `Phone Verified`,
`Email Status`, `Email Confidence`, `Lead Score`, `Practice Size`,
`Annual Billing`, `Medicare Paid`, `Patient Volume`, `First Seen`.

**What you type is kept.** `Outreach Status`, `Comments`, `Date` and the unnamed
column are read back out of the existing workbook (or Google Sheet) before each
export replaces it, and written straight back — as are the Call List's
`Outcome` and `Notes`. Every other column is generated, so an edit to one of
those is overwritten by the next export.

---

## Phone verification

Numbers are graded by where they came from (see Accuracy rules below), and the
CMS pass adds a second, independent check: the number the provider **filed with
Medicare themselves**.

| `Phone Verified` | Meaning |
| :--- | :--- |
| `Confirmed (federal enrollment record)` | Your number matches their Medicare filing |
| `Differs from federal enrollment record` | Both are real; yours may be a better direct line, theirs may be the billing office |
| `From federal enrollment record` | You had no number; this one came from their Medicare filing |
| `No federal enrollment record` | Checked, but CMS does not cover this provider |

A mismatch never overwrites what you scraped — a direct line found on a
practice's own site is often more useful than the billing office number on file.

> **Mobile vs landline cannot be done for free.** US number portability broke
> the old area-code/exchange rules, and every service that resolves line type
> charges for it. Rather than guess, this tool reports where each number came
> from and whether a federal record corroborates it.

---

## Lead scoring

Every lead gets a 0–100 **Lead Score**, so the list sorts itself with the
reachable decision-makers on top:

- **Email** (up to 45) — a confirmed mailbox beats a role mailbox beats an
  unconfirmable one. **A bouncing address scores the lead 0.**
- **Phone** (up to 25) — a direct/cell line beats an unlabelled one, which beats
  a shared front-desk line, which beats a toll-free intake number.
- **Role** (up to 20) — Owner/Founder/CEO beats Clinical Director beats nothing.
- **Website, socials, confirmed NPI** — small bonuses for a complete record.
- **Billing volume and practice size** (up to 13, bonus) — banded by order of
  magnitude, so a $2M practice outranks a $40k one. Only applies where CMS has
  data; its absence never costs a lead points.

Ties on score break by billing volume, so among equally contactable leads the
bigger practice comes first.

---

## The Call List

The first tab of every export is a ranked working list — the sheet somebody
actually dials from. A score by itself tells a caller nothing, so each row
carries a plain-English reason:

```
 #  Name                Phone           Why this lead
 1  Shannon Weisberg    (415) 250-2343  Owner · direct line
 7  Crystal Barrera     (209) 606-3886  Owner · small practice (14 clinicians)
12  M. Dhillon          (281) 737-1167  Owner · $548k Medicare billing · $137k/clinician
```

Only facts that were actually collected appear — nothing is inferred to pad the
line out. It also has **Outcome** (a dropdown: Contacted / Replied / Meeting /
Won / Lost) and **Notes** columns for whoever works the list. Both are saved
before the next export and written back: Outcome as the lead's
`Outreach Status`, Notes as its `Comments`. `NPI Phone` shows the registry
number only when it differs from `Phone`.

Control the size on the Results tab, or from the command line:

```bash
python cli.py export --call-list 250          # top 250
python cli.py export --call-list 100 --icp    # ICP only
```

### The ICP filter

`--icp` narrows the list to the profile that actually buys revenue cycle
management: **an owner-operated practice small enough not to have its own
billing department, that you can reach.** Concretely — a recognised ownership
role, 25 or fewer clinicians where practice size is known, and at least one
working contact method.

It deliberately does *not* require Medicare billing data, because most
therapists will never have any and requiring it would discard a whole source.

The sharpest signal it surfaces is **revenue per clinician**: a practice billing
$548k across a handful of people has serious money moving through it and nobody
in-house to chase it. That combination is what the `Why` line calls out.

---

## Sending the outreach

The last step: mail-merge to the leads you have qualified, through **your own**
mailbox (Gmail, Outlook, or a domain account). No service to pay for, no third
party holding your list.

```bash
python cli.py send --template pitch.txt --icp --limit 25      # dry run
python cli.py send --template pitch.txt --icp --limit 25 --send
```

A template is a subject line, a blank line, then the body:

```
Subject: Quick question about billing at {{Practice}}

Hi {{First Name}},

I work with {{Specialty}} practices in {{State}} on billing and claims, and
came across {{Practice}} while researching practices in {{City}}.
...
```

Merge fields: `{{First Name}}` `{{Last Name}}` `{{Full Name}}` `{{Practice}}`
`{{City}}` `{{State}}` `{{Specialty}}` `{{Practice Size}}` `{{Role}}`. A field
that doesn't exist is left visible in the preview rather than silently blanked,
so a typo can't go out as a gap in a real email.

### What is enforced, not offered as an option

**CAN-SPAM compliance.** US law requires commercial email to carry a real
physical postal address and a working opt-out, and to honour opt-outs. Sending
is **blocked** until both are configured, and both are added to every message
automatically along with a `List-Unsubscribe` header. The penalties attach to
the sender, which is you.

**A do-not-contact list.** Anyone who asks to be removed, plus every address
that hard bounces, is checked before every send and can never be contacted
again by accident. Bounces are added automatically; a *temporary* failure is
not, because a busy server shouldn't cost you the lead forever.

**Throttling.** Messages go out roughly one a minute with jitter, under a daily
cap that counts every campaign, not just the current one. Defaults are
deliberately low — a domain with no sending history that suddenly emits hundreds
of cold emails is blacklisted within a day.

**Nobody is contacted twice.** Checked on both lead ID and email address, since
the same person can appear under two records from two sources.

**Known-bad addresses are never sent to**, even with `--allow-unverified`.
`invalid`, `disposable` and `risky` are excluded unconditionally.

### Your password

Stored in the **Windows Credential Manager** (macOS Keychain elsewhere) via
`keyring` — never in `config.json`, which is plain text in the project folder.

Gmail and Outlook both reject your normal account password when 2FA is on. You
need an **App Password** (Google Account → Security → 2-Step Verification → App
passwords). The Settings tab says so next to the field, because it's the single
most common reason a first send fails.

### Before your first campaign

1. **Settings** → fill in SMTP, from name/address, postal address, unsubscribe
   address. Press **Test connection**.
2. **Outreach** → write the message, press **Preview** to see exactly who would
   receive it and why everyone else was excluded.
3. **Send test to myself** — merge fields filled from your best lead, so you see
   what actually lands.
4. Start at 10–20 a day for the first week, then raise the cap.

> **What this cannot do.** Deliverability comes from sending to addresses that
> exist, from a domain with a reputation, at a human volume. This tool handles
> the first and third. It cannot make a cold campaign land, and it cannot repair
> a domain you have already burnt.

---

## Updates

The app checks this repository's GitHub releases (or version tags) when it opens
and every six hours, and shows **Update available** when there is a newer
version. One click downloads it, test-starts the new version in a separate
process, and restarts. It is never applied without a click, it waits for a
running collection or send to finish, and a version that fails its start-up
check is rolled back rather than kept.

- **A git checkout** fast-forwards to the release tag, installs new libraries if
  `requirements.txt` changed, and refuses rather than overwriting local edits.
- **The Windows .exe** downloads the release's `ProviderLeadGen.exe`, verifies
  it against `ProviderLeadGen.exe.sha256`, self-tests it, and swaps it in after
  the running copy exits.

The code is never swapped inside the running process — that is how a run ends
up half on old code and half on new. Updating is a restart of a few seconds;
leads, settings and passwords live outside the code and are untouched.

`python cli.py update --check` checks from the command line; `python cli.py
update` applies it. Publishing an update is `version.py` + a `vX.Y.Z` tag — see
[GUIDE.md](GUIDE.md), section 10.

---

## When something breaks, it says so

Every source here can change or block us without notice. Previously that showed
up as a run that quietly saved nothing: **four Psychology Today runs in August
2026 reported "Collection finished" having collected zero providers**, and the
breakage went unnoticed for ten days.

That cannot happen now:

- An empty **first** listing page raises an error naming the likely cause. An
  empty **later** page is just the end of the directory. These are no longer the
  same code path.
- A run that completes but adds nothing is recorded as `empty`, never `ok`, and
  the status banner turns amber.
- A failure turns the banner red and prints what to do about it.
- **Health Check** (button, or `python cli.py doctor`) tests every source in
  about ten seconds — before you commit to a forty-minute run.

```
Psychology Today: OK
   [ok] Fetch directory page
        Loaded Texas via the r.jina.ai reader relay.
   [ok] Parse provider listing
        Read 20 providers from page 1.
```

---

## Running outside the US

Psychology Today serves its directory to US traffic only; from anywhere else it
answers `403 This page is unavailable in your region`. The scraper detects this
and transparently re-fetches through the free `r.jina.ai` reader (no key, no
cost, roughly one request per 20 providers).

To go direct instead, put a US VPN/proxy in **Settings → Proxy**
(e.g. `http://127.0.0.1:8080`).

---

## Command line

The window is one caller; the CLI is another. This is what makes the tool
schedulable — a nightly collect + verify + export needs nobody watching.

```bash
python cli.py doctor                                  # check every source
python cli.py collect-pt --state Texas --owners-only
python cli.py collect-npi --state TX --specialty Psychiatry
python cli.py import-npi --file numbers.txt
python cli.py find-emails                             # second pass for blanks
python cli.py verify                                  # check every address
python cli.py enrich-npi                              # NPI phone + gender
python cli.py enrich-cms                              # billing volume + practice size
python cli.py send --template pitch.txt --icp         # dry run; add --send to deliver
python cli.py suppress --add someone@practice.com     # never contact again
python cli.py export --min-score 40                   # only the good ones
python cli.py stats
python cli.py runs                                    # recent runs + outcomes
```

Exit codes: `0` ok, `1` failed, **`2` finished but collected nothing** — which is
the signature of a source that has quietly broken, so it is worth alerting on in
a scheduled job.

---

## Storage

`leads.db` (SQLite) is the system of record. The spreadsheet is an **export**.

This used to be the other way round: every batch of 50 leads reloaded the entire
`.xlsx`, walked every row of every tab to rebuild the duplicate-detection sets,
appended, restyled and saved it again — O(n²) over a run, so a big state got
slower the longer it ran, and a crash mid-save could corrupt the file.

The change also buys:

- **Resume** — a stopped or crashed run picks up at the listing page it reached
  instead of starting the state over.
- **Re-export any slice** without re-scraping: `--min-score 40`, `--status valid,role`.
- **Real run history**, including the runs that failed.

Your existing `RCM_Provider_Leads.xlsx` is imported automatically the first time
the app opens, so nothing collected before this is left behind.

### Duplicate rules

A lead is a duplicate only when it is the *same lead*: its NPI already exists in
that tab, or its email address already exists anywhere.

A repeated **phone number is not a duplicate.** Several clinicians at one group
practice genuinely publish the same front-desk line, and rejecting on phone
silently discarded every colleague after the first. Those leads are kept, with
`(shared practice line)` noted in `Phone Source`.

---

## Accuracy rules

These exist because each one was, at some point, wrong in the output.

**Emails from search results.** A result mentioning a provider is not proof that
an address in its snippet is theirs. Every search-derived address must clear two
checks: the result is *about this provider* (names them, names their practice
distinctively, or quotes the phone number searched for), **and** the address
could *plausibly be theirs* (on that page's own domain, or a personal mailbox
named after them). Failures are discarded. Earlier runs attached a Canadian
accessories shop's `info@` to a Texas psychiatrist, a colleague's university
mailbox to another, and assorted strangers' Gmail addresses to a third.

**Phone numbers.** Validated as genuinely dialable NANP numbers — including that
the area code is one actually assigned. `(457) 821-5250` is perfectly
well-formed and completely undialable. Numbers labelled as fax, crisis or
emergency lines are discarded outright. What survives is graded in `Phone Source`:

| `Phone Source` | Trust |
| :--- | :--- |
| `Provider Website (personal/direct …)` | **Highest** — labelled as their cell or direct line |
| `Provider Website (tel: link)` | High — an unlabelled intentional link |
| `Provider Website (business line …)` | Medium — front desk, shared |
| `Provider Website (page text)` | Check before use — may belong to someone else on the page |
| `Psychology Today (listing)` | Low — usually a call-tracking number |
| `… (toll-free)` | Low — centralised intake |

**Ownership roles.** A title only counts if it sits next to *that provider's*
name, within one sentence. Without that rule, "Jane Smith is a therapist here.
Our founder, Bob Jones, …" tags Jane as the founder. Abbreviations like `Dr.`
and middle initials are not treated as sentence endings — on therapist sites,
where nearly every name carries one, that mistake hides most genuine owners.
`Clinical Director` and `Executive Director` are recorded but do **not** pass the
owners-only filter: senior staff, not owners.

**Marketplaces are never scraped for contact details.** Grow Therapy,
Thriveworks, LifeStance, Headway, Alma and the rest publish the *platform's*
central intake number, corporate mailbox and brand social accounts identically
on every clinician's page. Two different Delaware therapists both came back with
Grow Therapy's number and Instagram. The profile URL is still recorded as the
`Website`.

**Directories and aggregators** (Healthgrades, Vitals, WebMD, ZocDoc, …) are
excluded from website discovery, so `Website` holds practice sites rather than
listing pages.

---

## Tests

```bash
pytest
```

283 tests over saved copies of the real pages each parser has to handle. Every
accuracy rule above has a test that fails if it regresses — which is what makes
these heuristics safe to change. Previously the only way to find out you had
broken the phone classifier was to run a whole state and read the output.

---

## Settings

`config.json` (created on first save, gitignored) is shared by the app and the
CLI: output destination, proxy, verification options, concurrency. The Google
service-account **path** is stored, never the key itself.

---

## Project layout

| File | Role |
| :--- | :--- |
| `app.py` | Desktop UI. Front end only — no business logic. |
| `cli.py` | Command line, for scripting and scheduling. |
| `pipeline.py` | The actual work: collection and enrichment passes. |
| `storage.py` | SQLite store — leads, runs, resume checkpoints, scoring. |
| `verification.py` | MX / SMTP verification and address inference. |
| `lead_manager.py` | Excel and Google Sheets exporters. |
| `psychology_today_engine.py` | Directory scraping, region-block relay. |
| `npi_engine.py` | NPI registry lookups. |
| `email_engine.py` | Website scraping, phone/role/social extraction, search. |
| `cms_engine.py` | Medicare billing volume, practice size, federally-filed phones. |
| `outreach.py` | Mail merge, CAN-SPAM footer, throttling, bounce classification. |
| `health.py` | The ten-second "is everything still working?" check. |
| `errors.py` | Typed failures, each carrying a remedy for the user. |
| `catalog.py` / `config.py` | States and specialties; settings. |
