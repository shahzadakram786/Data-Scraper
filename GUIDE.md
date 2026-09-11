# How to use it

A step-by-step guide for whoever is actually running this. No programming
needed — the command line section at the end is optional.

---

## Contents

1. [First-time setup](#1-first-time-setup)
2. [Before every session: Health Check](#2-before-every-session-health-check)
3. [Collecting leads](#3-collecting-leads)
4. [Enriching what you collected](#4-enriching-what-you-collected)
5. [Reading the results](#5-reading-the-results)
6. [Sending outreach](#6-sending-outreach)
7. [A weekly routine that works](#7-a-weekly-routine-that-works)
8. [When something goes wrong](#8-when-something-goes-wrong)
9. [Running it from the command line](#9-running-it-from-the-command-line)

---

## 1. First-time setup

**You need this once.** Ten minutes.

### Install

1. Install Python 3.12 from [python.org](https://python.org). On the first
   screen of the installer, tick **"Add Python to PATH"** — if you miss it,
   nothing below will work.
2. Open the project folder, then open a terminal there.
3. Run:

   ```bash
   pip install -r requirements.txt
   ```

4. Start it:

   ```bash
   python app.py
   ```

### Fill in Settings

Open the **Settings** tab and work down it.

**Where results are exported**

- *Local Excel file* — the normal choice. Leave the path as it is.
- *Google Sheets* — only if you want the list live in the cloud. Needs a
  service-account JSON file from Google Cloud.

**Email accuracy** — leave all three ticked. They are what keeps bad addresses
out of your list.

**Network → Proxy** — leave blank unless you have a US VPN or proxy. See
[section 8](#8-when-something-goes-wrong) for why this matters.

**Sending email** — only needed when you get to [section 6](#6-sending-outreach).

Press the button at the bottom (**Save Settings**) when done.

> **The first time you open the app**, it reads your existing
> `RCM_Provider_Leads.xlsx` into its database. This happens once. After that the
> database is the real store and the spreadsheet is something it writes *out*.
> Don't edit the spreadsheet expecting changes to come back.

---

## 2. Before every session: Health Check

**Press the "Health Check" button, top right. It takes about ten seconds.**

This is the single most useful habit. Every source this tool uses can change or
block you without warning, and when that happens the symptom is always the same:
a run that quietly collects less than it should, or nothing.

You want to see five green rows:

```
NPI Registry: OK
Psychology Today: OK
Web Search (DuckDuckGo): OK
Email Verification: OK (reduced)
CMS Open Data: OK
```

**"OK (reduced)" on Email Verification is normal** on a home or office
connection — see [section 8](#8-when-something-goes-wrong).

If anything says **FAILED**, read the detail underneath. It says what broke and
what to do. Don't start a long run until it's green.

---

## 3. Collecting leads

Go to the **1. Collect** tab and pick one of three sources.

### Psychology Today — the main source

Best for therapists, counsellors and psychologists.

1. Choose a **State**.
2. Tick **"Owners / CEOs / Founders only"** if you only want decision makers.
   This cuts volume a lot but every remaining lead is someone who can sign.
3. Leave **"Resume a stopped run"** ticked.
4. Press **Collect from Psychology Today**.

Expect **15–40 minutes for a full state**. Leads appear as they're found — you
can watch the count climb on the Results tab.

> **You can stop any time.** Press **Stop**, and everything collected so far is
> saved. Next time you run the same state it picks up from the listing page it
> reached rather than starting over. The app tells you when it's resuming.

### NPI Registry — bulk volume

Every provider of a given specialty in a state, from the federal registry.

High volume, but **the registry publishes no email addresses** — expect around
1% to arrive with one. You'll need [step 4](#4-enriching-what-you-collected) to
make these usable.

### Paste NPI numbers — for ZocDoc and similar

ZocDoc blocks automated access, so copy the NPI numbers by hand and paste them
in. Anything after that is automatic. The box counts how many it found as you
type.

---

## 4. Enriching what you collected

Go to **2. Enrich**. Three passes, run them in this order. Each one tells you
how much work is waiting.

### a. Find missing email addresses

Searches for an address for every lead that has none, using the practice's own
website first and a corroborated web search second.

**Slow** — roughly 5–15 seconds per lead. Leave it running.

### b. Verify email addresses

Checks every address against its domain's real mail server.

**Fast.** Run it after every collection. This is what stops you emailing
addresses that bounce, which is the fastest way to get your sending domain
blacklisted.

### c. Add Medicare billing volume and practice size

Looks each provider up in federal Medicare data and attaches what they bill per
year, how many clinicians are in their practice, and the phone number they filed
with Medicare.

**Only Medicare-enrolled providers appear.** Expect roughly a third of
NPI/ZocDoc leads and very few therapists. **A blank here is not a bad lead** —
most therapists are cash-pay and never appear in Medicare data at all.

---

## 5. Reading the results

Go to **4. Results**, then press **Open spreadsheet**.

### The Call List tab — start here

The first tab of the workbook. Your best leads, ranked, with a plain-English
reason for each:

```
 #  Name                Phone           Why this lead
 1  Shannon Weisberg    (415) 250-2343  Owner · direct line
 7  Crystal Barrera     (209) 606-3886  Owner · small practice (14 clinicians)
12  M. Dhillon          (281) 737-1167  Owner · $548k Medicare billing · $137k/clinician
```

Two columns are **blank on purpose** — fill them in as you work:

- **Outcome** — a dropdown: Contacted, Replied, Meeting, Won, Lost, Bad Contact
- **Notes** — anything you want

Tick **"Owner-operated small practices only"** on the Results tab to narrow the
Call List to practices that actually buy outsourced billing.

### Email Status — can I send to this?

| Status | Colour | Meaning |
| :--- | :--- | :--- |
| `valid` | green | The mail server confirmed this mailbox. Send. |
| `role` | blue | Shared mailbox (`info@`, `office@`). Real, but not one person. |
| `accept-all` | amber | The domain accepts everything, so it can't be confirmed. |
| `unknown` | grey | Couldn't be checked. Usually the port 25 issue below. |
| `risky` | orange | Probably belongs to someone else. Don't use. |
| `invalid` | red | Would bounce. |

Addresses that would definitely bounce are **removed automatically** rather than
exported with a warning.

### Lead Score — who to call first

0–100. Roughly: a confirmed email is worth the most, then a direct line over a
front desk, then being the owner, with a bonus for billing volume and practice
size. **A bouncing address scores the lead 0**, because it's worse than no
address at all.

### Phone Source — can I trust this number?

From most to least trustworthy:

1. `Provider Website (personal/direct …)` — their cell or direct line
2. `Provider Website (tel: link)` — an unlabelled but deliberate link
3. `Provider Website (business line …)` — front desk
4. `Provider Website (page text)` — check before using; may be someone else on the page
5. `Psychology Today (listing)` — usually a call-tracking number

`Phone Verified` says whether the number matches the one the provider filed with
Medicare — the strongest confirmation available.

---

## 6. Sending outreach

Go to **3. Outreach**.

### Set it up (once)

In **Settings → Sending email**:

1. **Mail provider** — pick Gmail, Outlook or Custom. Host and port fill in.
2. **Username** — your email address.
3. **Password** — ⚠️ **Gmail and Outlook reject your normal password.** You need
   an **App Password**:
   *Google Account → Security → 2-Step Verification → App passwords.*
   This is the single most common reason a first send fails.
   Your password is stored in Windows Credential Manager, not in any file here.
4. **From name / From address** — what recipients see.

In **Settings → Required by law**:

5. **Postal address** — your real business address. **Required by US law**
   (CAN-SPAM) in every commercial email. Sending is blocked without it.
6. **Unsubscribe address** — can be the same mailbox you send from.
7. **Daily send cap** — **start at 10–20.**

Press **Save Settings**, then **Test connection** on the Outreach tab.

### Write and send

1. Write your subject and message. Use merge fields in `{{double braces}}` —
   the list of valid ones is under the editor. A field that doesn't exist shows
   up in the preview rather than going out as a gap.
2. Choose your audience and how many to send.
3. Press **Preview (no send)**. It shows exactly who would receive it *and why
   everyone else was excluded*.
4. Press **Send test to myself**. Check what actually lands.
5. When happy, press **Send Campaign** at the bottom. It asks you to confirm.

Messages go out roughly **one a minute**. A campaign of 25 takes about half an
hour. That pacing is deliberate.

### Warming up — read this before your first campaign

A brand-new sending domain that suddenly emits hundreds of cold emails gets
blacklisted within a day, and recovering is slow and painful.

| Week | Daily cap |
| :--- | :--- |
| 1 | 10–20 |
| 2 | 30–40 |
| 3 | 50–75 |
| 4+ | 100+ if replies are healthy and bounces are low |

### The do-not-contact list

Anyone who asks to be removed, and every address that hard bounces, is never
contacted again. Bounces are added automatically.

**When somebody replies asking to be removed, add them by hand:** Outreach →
**Do-not-contact list** → paste the address → Add. You can block a whole domain
with `@theircompany.com`.

Honouring opt-outs is a legal requirement, not a courtesy.

---

## 7. A weekly routine that works

**Monday — build the list**
1. Health Check
2. Collect one state from Psychology Today (owners only)
3. Enrich: find emails → verify → Medicare data
4. Export, open the Call List

**Tuesday to Thursday — work it**
5. Call from the top of the Call List; fill in Outcome and Notes
6. Send a campaign of 20–40 to the rest
7. Add anyone who asks out to the do-not-contact list

**Friday — review**
8. Results tab: is the verified-email count growing?
9. Run history: any runs marked EMPTY or FAILED?

---

## 8. When something goes wrong

### "Nothing was collected" / the banner turns amber

A run that finishes but adds nothing is **not** reported as success, on purpose.
Either the state is already collected, or a source broke.

**Press Health Check.** It tells you which.

### Email Verification says "OK (reduced)"

Your internet provider blocks outbound port 25, which most home and office
connections do. It means:

- Addresses come back `unknown` instead of `valid`
- Address inference is **switched off entirely**
- Dead domains are *still* caught

**This is the single biggest limit on your results.** A US VPN that allows port
25 (about $5/month) fixes verification, turns inference back on, and makes
Psychology Today and Medicare lookups several times faster. Put it in
**Settings → Proxy**.

### "Psychology Today is unavailable in your region"

It only serves the directory to US traffic. The app detects this and routes
around it automatically through a free reader service — slower, but it works.
A US proxy makes it direct and fast.

### A run failed with a red banner

Read the second line — it says what to do. `ListingParseError` means the site
changed its markup and the scraper needs updating. That's a code change, not a
settings change.

### The spreadsheet won't update

Close it in Excel. Windows locks open files. Your data is safe in the database
either way — just export again.

### Emails aren't sending

1. **Test connection** on the Outreach tab.
2. "Server refused those credentials" → you're using your account password
   instead of an **App Password**.
3. "Cannot send yet" → something required is missing in Settings; the amber bar
   lists exactly what.
4. "Daily cap reached" → working as intended. Come back tomorrow or raise the cap.

### Where are the logs?

`logs/leadgen.log`, in the project folder. It keeps the last few runs, including
ones that crashed overnight.

---

## 9. Running it from the command line

Optional. Useful for scheduling overnight runs — no window needed.

```bash
python cli.py doctor                                  # health check
python cli.py collect-pt --state Texas --owners-only
python cli.py collect-npi --state TX --specialty Psychiatry
python cli.py import-npi --file numbers.txt
python cli.py find-emails
python cli.py verify
python cli.py enrich-cms
python cli.py export --call-list 100 --icp
python cli.py send --template pitch.txt --icp --limit 25      # dry run
python cli.py send --template pitch.txt --icp --limit 25 --send
python cli.py suppress --add someone@practice.com
python cli.py stats
python cli.py runs
```

A template file is a subject line, a blank line, then the body:

```
Subject: Quick question about billing at {{Practice}}

Hi {{First Name}},

I work with {{Specialty}} practices in {{State}} on billing and claims...
```

**Exit codes** (worth alerting on in a scheduled job):

- `0` — worked
- `1` — failed
- `2` — finished but collected nothing, which is what a quietly broken source
  looks like

### A nightly job

```bash
python cli.py collect-pt --state Texas --owners-only
python cli.py find-emails
python cli.py verify
python cli.py export
```

Run them as separate lines, **not** chained with `&&`. A state that is already
fully collected exits with code 2, which `&&` treats as failure and would stop
the rest of the job from running — so the enrichment and export would silently
never happen.

Schedule it with Windows Task Scheduler, or `cron` on a server.
