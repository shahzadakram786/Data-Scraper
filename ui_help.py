"""In-app guidance, one entry per screen.

The tool accumulated a lot of behaviour that is not discoverable by looking at
it — which pass to run first, why an address reads "unknown", why the first
send fails on a Gmail account password. That knowledge lived only in GUIDE.md,
which means it lived nowhere for anyone who just opened the window.

Each screen gets a panel of its own text. Kept as data rather than scattered
through the widget code so the wording can be reviewed as writing, and so the
window has no opinions about what the words say.

Writing rules for anything added here: say what to do, not how the code works;
name things as they appear on screen; and where a mistake is expensive, say
what it costs.
"""

# Each entry: (heading, [paragraph | ("list", [items]) | ("warn", text) | ("tip", text)])
HELP = {
    "home": (
        "Where to start",
        [
            "This screen shows what you have and what to do next. The blue "
            "button under “Next step” is always a safe thing to press — it "
            "looks at your actual data and picks the stage you have not "
            "finished yet.",
            ("list", [
                "The funnel shows how many leads reach each stage.",
                "A stage that is much smaller than the one above it is where "
                "you are losing leads.",
                "“Verified” is the number you can actually email with confidence.",
            ]),
            ("tip", "Press Health Check before a long run. It takes ten seconds "
                    "and tells you whether the sources are working today."),
        ],
    ),

    "collect": (
        "Collecting leads",
        [
            "Pick one source, set the filters, and press the button at the "
            "bottom. Leads are saved as they are found — you can stop at any "
            "time without losing anything.",
            "Above the options, “What kind of leads do you need?” narrows the "
            "result to practices that show signs of needing what you sell — a "
            "website, SEO, bookkeeping and so on. Nothing chosen means every "
            "lead, as before. Everything collected is kept either way; the "
            "choice decides what the export and the Call List show.",
            ("list", [
                "Psychology Today — therapists and counsellors. The richest "
                "source: it follows each provider's own website for their "
                "email, direct line and role. 15–40 minutes for a full state.",
                "NPI Registry — bulk volume from the federal registry. Fast, "
                "but it publishes no email addresses, so these need the Enrich "
                "step before they are usable.",
                "Paste NPI numbers — for numbers copied by hand from ZocDoc or "
                "anywhere else.",
            ]),
            ("tip", "“Owners / CEOs / Founders only” cuts the count sharply, "
                    "but everything left is someone who can sign a contract."),
            "If a run stops — you press Stop, or the power goes — the next run "
            "on the same state picks up from the listing page it reached "
            "rather than starting again.",
        ],
    ),

    "enrich": (
        "Filling in what collection missed",
        [
            "Five passes. Run them top to bottom; each one uses what the one "
            "before it found.",
            ("list", [
                "Find missing emails — searches the practice's own website, "
                "then a corroborated web search. Slow: 5–15 seconds per lead.",
                "Verify addresses — checks each one against its real mail "
                "server. Fast, and the most important of them.",
                "Match NPI records — the practice phone on the provider's federal "
                "record (kept beside the website number), their gender, and any "
                "missing address details. Gender is left blank rather than "
                "guessed when there is no confident match.",
                "Medicare data — annual billing, practice size, and the phone "
                "number the provider filed federally.",
                "Check websites — reads each practice's homepage once so lead "
                "types can be matched. Run it after Find emails, and once for "
                "leads collected before lead types existed.",
            ]),
            ("warn", "Verify is what stops you emailing addresses that bounce. "
                     "Bounces are the fastest way to get a sending domain "
                     "blacklisted, and that is slow and painful to undo."),
            "Medicare data only covers Medicare-enrolled providers, and hides "
            "anyone with fewer than 11 patients. Expect about a third of "
            "NPI/ZocDoc leads and very few therapists. A blank there means "
            "“not a Medicare biller”, not a worse lead.",
        ],
    ),

    "outreach": (
        "Sending outreach",
        [
            "Mail merge through your own mailbox. Nothing is sent until you "
            "have previewed it and confirmed.",
            ("warn", "Gmail and Outlook reject your normal account password. "
                     "You need an App Password — in Google, that is Account → "
                     "Security → 2-Step Verification → App passwords. This is "
                     "the most common reason a first send fails."),
            ("list", [
                "Preview shows exactly who would receive it, and why everyone "
                "else was left out.",
                "Send test to myself fills the merge fields from your best "
                "lead, so you see what really lands.",
                "Messages go out about one a minute. A batch of 25 takes half "
                "an hour, and that pacing is protecting you.",
            ]),
            ("tip", "Start at 10–20 a day for the first week. A new sending "
                    "domain that suddenly emits hundreds of cold emails gets "
                    "blacklisted within a day."),
            "Anyone who hard bounces is added to the do-not-contact list "
            "automatically. When somebody replies asking to be removed, add "
            "them by hand — honouring that is a legal requirement, not a "
            "courtesy.",
        ],
    ),

    "results": (
        "Reading your results",
        [
            "The Call List is the first tab of the exported workbook: your "
            "best leads, ranked, each with a plain-English reason. Two columns "
            "are left blank for you — Outcome and Notes.",
            ("list", [
                "Lead Score 0–100 — a confirmed email is worth the most, then "
                "a direct line over a front desk, then being the owner.",
                "An address that would bounce scores the lead 0. It is worse "
                "than having no address at all.",
                "Email Status colours here match the colours in the "
                "spreadsheet, so the two read the same way.",
                "Lead Types (the last column) says which services each practice "
                "looks like it needs, and why. “Not checked” counts leads the app "
                "can't judge yet — usually ones whose website nobody has read.",
            ]),
            ("tip", "Tick “Owner-operated small practices only” to narrow the "
                    "Call List to the practices that actually buy outsourced "
                    "billing."),
        ],
    ),

    "settings": (
        "Settings",
        [
            "Everything here is saved to config.json next to the app, and is "
            "shared with the command-line version.",
            ("list", [
                "Leave the three Email accuracy options ticked — they are what "
                "keeps bad addresses out of your list.",
                "Proxy is only needed if you have a US VPN. Leave it blank "
                "otherwise; the app routes around the region block on its own.",
                "Your mail password is stored in the Windows Credential "
                "Manager, never in a file in this folder.",
                "Lead Types — switch a service off to hide it, rename it, or add "
                "your own from the same checks plus words on the homepage.",
            ]),
            ("warn", "A postal address and an unsubscribe address are required "
                     "by US law in commercial email. Sending stays blocked "
                     "until both are filled in. The liability is the sender's, "
                     "which is you."),
        ],
    ),
}


# Shown on the Home screen the very first time, when there is nothing to show.
FIRST_RUN = [
    ("Check the sources are working",
     "Press Health Check, top right. Ten seconds. Five green rows means "
     "everything the tool depends on is reachable today."),
    ("Collect your first leads",
     "Go to Collect, choose Psychology Today and a state, and press the "
     "button. Leads appear as they are found."),
    ("Fill in the gaps",
     "Go to Enrich and run the passes top to bottom. Verify is the one "
     "that matters most."),
    ("Open your call list",
     "Go to Results and press Open spreadsheet. The first tab is your ranked "
     "call list, ready to work."),
]


def for_screen(key):
    """(heading, blocks) for a screen, or a safe fallback."""
    return HELP.get(key, ("Help", ["No guidance for this screen yet."]))
