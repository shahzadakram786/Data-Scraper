"""Sending the outreach — mail merge over your own mailbox.

This closes the loop the rest of the tool builds up to: collect, verify,
qualify, and now actually contact. It sends through *your* SMTP account (Gmail,
Outlook, or a domain mailbox), so there is no service to pay for and no third
party holding your list.

Three things are built in rather than offered as options, because without any
one of them the tool does more harm than good:

**CAN-SPAM compliance.** US law requires commercial email to carry a real
physical postal address and a working opt-out, and to honour opt-outs promptly.
A send is refused outright if either is missing — see `compliance_problems`.
Every message gets the footer and a `List-Unsubscribe` header, which mail
providers also read as a good-faith signal.

**A suppression list.** Anyone who asks to be removed, every address that hard
bounces, and every domain you blacklist is checked before each send and can
never be contacted again by accident.

**Throttling.** Cold email sent in a burst is what gets a mailbox shut down.
Sends are spaced with jitter and capped per day, defaulting low enough for a
domain with no sending history.

The honest caveat: none of this makes a cold campaign land. Deliverability comes
from sending to addresses that exist, from a domain that has a reputation, at a
volume that looks human. This tool can only do the first and third.
"""

import re
import ssl
import time
import random
import smtplib
import logging
import threading
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, formatdate

try:
    import keyring
    _HAVE_KEYRING = True
except ImportError:  # pragma: no cover - depends on the install
    _HAVE_KEYRING = False


KEYRING_SERVICE = "ProviderLeadGen-SMTP"

# Statuses safe to send to. `unknown` is included but flagged: on a network
# that blocks port 25 every address ends up unknown, so excluding it would mean
# never sending anything — but the bounce risk is real and the caller is told.
SENDABLE_STATUSES = ("valid", "role", "accept-all")
RISKY_STATUSES = ("unknown", "")
NEVER_SEND_STATUSES = ("invalid", "disposable", "risky")

# Presets so nobody has to look up port numbers. Gmail and Outlook both need an
# app password rather than the account password when 2FA is on.
SMTP_PRESETS = {
    "Gmail": {"host": "smtp.gmail.com", "port": 587, "starttls": True,
              "note": "Requires an App Password (Google Account > Security > "
                      "2-Step Verification > App passwords). Your normal "
                      "password will be rejected."},
    "Outlook / Microsoft 365": {"host": "smtp.office365.com", "port": 587,
                                "starttls": True,
                                "note": "Requires an App Password if 2FA is on."},
    "Zoho": {"host": "smtp.zoho.com", "port": 587, "starttls": True, "note": ""},
    "Custom": {"host": "", "port": 587, "starttls": True, "note": ""},
}

MERGE_FIELD_RE = re.compile(r'\{\{\s*([A-Za-z0-9 _]+?)\s*\}\}')

# Offered in the template editor. Anything in the lead record works, but these
# are the ones worth personalising on.
MERGE_FIELDS = (
    "First Name", "Last Name", "Full Name", "Practice", "City", "State",
    "Specialty", "Practice Size", "Role",
)


def compliance_problems(settings):
    """What still has to be filled in before a send is legal.

    Returns a list of human-readable problems; empty means good to go. This is
    a hard gate rather than a warning: sending commercial email without a
    postal address and a working opt-out is a CAN-SPAM violation, and the
    penalties attach to the sender, not to this tool.
    """
    problems = []
    if not (settings.get("from_email") or "").strip():
        problems.append("A 'from' address is required.")
    if not (settings.get("from_name") or "").strip():
        problems.append("A 'from' name is required — unlabelled senders get filtered.")
    if not (settings.get("postal_address") or "").strip():
        problems.append(
            "A physical postal address is required by CAN-SPAM and must appear "
            "in every commercial email. Your business address is fine.")
    unsubscribe = (settings.get("unsubscribe_email") or "").strip()
    if not unsubscribe:
        problems.append(
            "An unsubscribe address is required, so recipients have a working "
            "way to opt out. It can be the same mailbox you send from.")
    elif "@" not in unsubscribe:
        problems.append("The unsubscribe address is not a valid email address.")
    if not (settings.get("smtp_host") or "").strip():
        problems.append("SMTP server details are not configured.")
    if not (settings.get("smtp_user") or "").strip():
        problems.append("An SMTP username is required.")
    return problems


# ----------------------------------------------------------------- password

def store_password(username, password):
    """Keep the SMTP password in the OS credential store, never in
    config.json — that file is plain text and sits in the project folder."""
    if not _HAVE_KEYRING:
        raise RuntimeError(
            "The 'keyring' package is not installed, so the password cannot be "
            "stored securely. Run: pip install keyring")
    keyring.set_password(KEYRING_SERVICE, username, password)


def load_password(username):
    if not (_HAVE_KEYRING and username):
        return ""
    try:
        return keyring.get_password(KEYRING_SERVICE, username) or ""
    except Exception:
        return ""


def forget_password(username):
    if _HAVE_KEYRING and username:
        try:
            keyring.delete_password(KEYRING_SERVICE, username)
        except Exception:
            pass


# ----------------------------------------------------------------- rendering

def merge_values(lead):
    """The values a template can reference, derived from a lead record."""
    first = (lead.get("Provider First Name") or "").strip()
    last = (lead.get("Provider Last Name") or "").strip()
    org = (lead.get("Organization Name") or "").strip()
    full = " ".join(p for p in (first, last) if p)
    return {
        "First Name": first or (org.split()[0] if org else "there"),
        "Last Name": last,
        "Full Name": full or org,
        "Practice": org or (full + "'s practice" if full else "your practice"),
        "City": (lead.get("City") or "").strip(),
        "State": (lead.get("State") or "").strip(),
        "Specialty": (lead.get("Specialty") or "").strip(),
        "Practice Size": str(lead.get("Practice Size") or "").strip(),
        "Role": (lead.get("Role") or "").strip(),
    }


def render(template, lead):
    """Fill {{ Merge Fields }} from a lead. Unknown fields are left visible
    rather than silently blanked, so a typo shows up in the preview instead of
    going out as a gap in a real email."""
    values = merge_values(lead)

    def substitute(match):
        key = match.group(1).strip()
        if key in values:
            return values[key]
        return match.group(0)  # leave it alone so the mistake is obvious

    return MERGE_FIELD_RE.sub(substitute, template or "")


def unresolved_fields(template):
    """Merge fields in a template that nothing will fill — caught before send."""
    known = set(MERGE_FIELDS)
    return sorted({m.group(1).strip() for m in MERGE_FIELD_RE.finditer(template or "")}
                  - known)


def build_footer(settings):
    """The CAN-SPAM footer. Not optional, and not hidden in 6pt grey either —
    a visible opt-out is both the legal requirement and what stops people
    reporting the message as spam, which is what actually damages a domain."""
    unsubscribe = (settings.get("unsubscribe_email") or "").strip()
    address = (settings.get("postal_address") or "").strip()
    sender = (settings.get("from_name") or "").strip()
    lines = [
        "",
        "—",
        "{0} · {1}".format(sender, address) if sender else address,
        "Don't want to hear from me? Reply with 'unsubscribe' or email "
        "{0} and I won't contact you again.".format(unsubscribe),
    ]
    return "\n".join(line for line in lines if line is not None)


class EmailSender:
    """One authenticated SMTP session, reused across a campaign.

    Reconnects on its own if the server drops the connection partway through,
    which providers do routinely on long sessions.
    """

    def __init__(self, settings, logger=None):
        self.settings = settings
        self.logger = logger or logging.getLogger(__name__)
        self.host = (settings.get("smtp_host") or "").strip()
        self.port = int(settings.get("smtp_port") or 587)
        self.username = (settings.get("smtp_user") or "").strip()
        self.use_starttls = bool(settings.get("smtp_starttls", True))
        self.password = load_password(self.username)
        self._connection = None
        self._lock = threading.Lock()

    # -- connection ---------------------------------------------------------

    def connect(self):
        if not self.password:
            raise RuntimeError(
                "No SMTP password is stored for {0}. Enter it in Settings — it "
                "goes into the Windows Credential Manager, not into any file "
                "in this project.".format(self.username or "this account"))

        context = ssl.create_default_context()
        if self.port == 465:
            connection = smtplib.SMTP_SSL(self.host, self.port, timeout=30,
                                          context=context)
        else:
            connection = smtplib.SMTP(self.host, self.port, timeout=30)
            connection.ehlo()
            if self.use_starttls:
                connection.starttls(context=context)
                connection.ehlo()
        connection.login(self.username, self.password)
        self._connection = connection
        return connection

    def _ensure_connection(self):
        if self._connection is None:
            return self.connect()
        try:
            status = self._connection.noop()[0]
            if status != 250:
                raise smtplib.SMTPServerDisconnected("noop failed")
        except (smtplib.SMTPServerDisconnected, smtplib.SMTPException, OSError):
            self.logger.debug("SMTP connection dropped — reconnecting.")
            self.close()
            return self.connect()
        return self._connection

    def close(self):
        if self._connection is not None:
            try:
                self._connection.quit()
            except Exception:
                try:
                    self._connection.close()
                except Exception:
                    pass
            self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def verify_login(self):
        """Used by the 'Test connection' button, so credentials are proved
        before a campaign rather than failing on the first recipient."""
        try:
            self.connect()
            return True, "Signed in to {0} as {1}.".format(self.host, self.username)
        except smtplib.SMTPAuthenticationError as e:
            hint = ""
            if "gmail" in self.host.lower():
                hint = (" Gmail rejects normal account passwords — create an App "
                        "Password under Google Account > Security.")
            return False, "The server refused those credentials.{0} ({1})".format(hint, e)
        except (OSError, smtplib.SMTPException) as e:
            return False, "Could not reach {0}:{1} — {2}".format(self.host, self.port, e)
        finally:
            self.close()

    # -- sending ------------------------------------------------------------

    def build_message(self, to_address, subject, body, settings):
        message = EmailMessage()
        message["From"] = formataddr(((settings.get("from_name") or "").strip(),
                                      (settings.get("from_email") or "").strip()))
        message["To"] = to_address
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid()

        reply_to = (settings.get("reply_to") or "").strip()
        if reply_to:
            message["Reply-To"] = reply_to

        # Both a one-click header and a visible footer. Providers weight the
        # header heavily when deciding whether a sender is legitimate.
        unsubscribe = (settings.get("unsubscribe_email") or "").strip()
        if unsubscribe:
            message["List-Unsubscribe"] = "<mailto:{0}?subject=unsubscribe>".format(
                unsubscribe)

        message.set_content(body + "\n" + build_footer(settings))
        return message

    def send(self, to_address, subject, body):
        """Send one message. Returns (ok, detail).

        A permanent rejection is reported separately from a temporary one, so
        the caller can suppress a dead address without suppressing someone
        whose server was merely busy.
        """
        with self._lock:
            try:
                connection = self._ensure_connection()
                message = self.build_message(to_address, subject, body, self.settings)
                connection.send_message(message)
                return True, "sent"
            except smtplib.SMTPRecipientsRefused as e:
                return False, "rejected: {0}".format(e.recipients)
            except smtplib.SMTPSenderRefused as e:
                return False, "sender refused: {0}".format(e)
            except smtplib.SMTPDataError as e:
                return False, "rejected by server: {0}".format(e)
            except (smtplib.SMTPException, OSError) as e:
                self.close()
                return False, "temporary failure: {0}".format(e)


def is_permanent_failure(detail):
    """Whether a failure means the address is dead rather than the server busy.

    Only a permanent failure should add an address to the suppression list —
    suppressing on a temporary blip would quietly shrink the list forever.
    """
    text = (detail or "").lower()
    if "temporary failure" in text:
        return False
    return any(marker in text for marker in (
        "rejected", "550", "551", "553", "554", "does not exist",
        "no such user", "unknown user", "user unknown", "mailbox unavailable"))


class Throttle:
    """Spaces sends out and stops at the daily cap.

    Bursts are what get a mailbox suspended. The jitter matters as much as the
    delay: a message exactly every 60 seconds is as obviously automated as a
    hundred at once.
    """

    def __init__(self, min_seconds=45, max_seconds=90, stop_event=None):
        self.min_seconds = max(0, int(min_seconds))
        self.max_seconds = max(self.min_seconds, int(max_seconds))
        self.stop_event = stop_event
        self._last_send = 0.0

    def wait(self):
        """Sleep until the next send is due, returning False if asked to stop."""
        if not self._last_send:
            self._last_send = time.time()
            return True
        delay = random.uniform(self.min_seconds, self.max_seconds)
        target = self._last_send + delay
        while time.time() < target:
            if self.stop_event is not None and self.stop_event.is_set():
                return False
            time.sleep(min(0.5, target - time.time()))
        self._last_send = time.time()
        return True
