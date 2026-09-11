"""Settings, saved to config.json next to the app.

Before this, the proxy, the Google credentials path and every worker count had
to be re-entered in the window on every launch, and the CLI had no way to know
any of them. One file now backs both.

config.json holds a path to a Google service-account key, not the key itself,
and is listed in .gitignore.
"""

import os
import json
import logging

CONFIG_FILE = "config.json"

DEFAULTS = {
    # Storage and output
    "db_path": "leads.db",
    "excel_path": "RCM_Provider_Leads.xlsx",
    "output": "excel",                 # "excel" or "google"
    "google_credentials": "",
    "google_spreadsheet": "RCM_Provider_Leads",
    "auto_export": True,               # write the spreadsheet when a run ends

    # Network
    "proxy": "",

    # Email quality
    "verify_emails": True,             # check addresses as they are collected
    "smtp_probe": True,                # confirm individual mailboxes (needs port 25)
    "probe_sender": "",                # MAIL FROM; empty means the null sender
    "guess_emails": True,              # infer + confirm addresses at known domains

    # Concurrency. Higher is faster until the source starts rate-limiting; the
    # email engine backs off on its own, so these are ceilings rather than targets.
    "pt_workers": 5,
    "email_workers": 5,
    "verify_workers": 8,
    "cms_workers": 6,

    # Outreach. The SMTP password is NOT here — it lives in the OS credential
    # store (Windows Credential Manager / macOS Keychain) via outreach.py,
    # because config.json is plain text sitting in the project folder.
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_starttls": True,
    "from_name": "",
    "from_email": "",
    "reply_to": "",
    # Both are legally required in commercial email in the US. A send is
    # refused until they are filled in — see outreach.compliance_problems.
    "postal_address": "",
    "unsubscribe_email": "",
    # Conservative on purpose: a domain with no sending history that suddenly
    # emits hundreds of cold emails gets its reputation destroyed in a day.
    "daily_send_cap": 40,
    "send_min_seconds": 45,
    "send_max_seconds": 90,
    "send_batch": 25,
    "send_allow_unverified": False,

    # Behaviour
    "resume_runs": True,               # pick up where a stopped run left off
    "owners_only": False,
    "exclude_corporate": True,
    "min_updated_year": 2010,
}


def load(path=CONFIG_FILE):
    """Read settings, filling in anything missing from DEFAULTS."""
    settings = dict(DEFAULTS)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            if isinstance(stored, dict):
                settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (ValueError, OSError) as e:
            logging.getLogger(__name__).warning(
                "Could not read %s (%s) — using defaults.", path, e)
    return settings


def save(settings, path=CONFIG_FILE):
    """Write settings, keeping only known keys so a typo can't accumulate."""
    clean = {key: settings.get(key, default) for key, default in DEFAULTS.items()}
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(clean, handle, indent=2)
    return clean


def proxies_for(settings):
    """requests-style proxy mapping, or None to go direct."""
    proxy = (settings.get("proxy") or "").strip()
    return {"http": proxy, "https": proxy} if proxy else None
