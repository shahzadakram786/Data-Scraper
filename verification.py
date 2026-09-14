"""Email verification and address-pattern inference — no paid API, no key.

Every address the scraper records is a guess until something confirms it. This
module does the confirming, using only what the internet gives away for free:

  1. Syntax and junk filtering (cheap, catches parse artefacts).
  2. Disposable / throwaway domain list.
  3. An MX lookup — a domain with no mail exchanger cannot receive mail at all,
     so anything at it is dead on arrival. This alone removes a large share of
     bad addresses and costs one DNS query per *domain*, cached.
  4. An SMTP conversation with the domain's own mail server: MAIL FROM, then
     RCPT TO. The server says whether the mailbox exists. No mail is ever sent
     — the session is closed before DATA.
  5. Catch-all detection: before asking about the real address, ask about a
     random one. A server that accepts x7f3q9k2@domain accepts everything, so
     its "yes" to the real address proves nothing, and the address is reported
     as accept-all rather than as verified.

Step 4 needs outbound port 25, which many home ISPs and cloud hosts block. That
is detected once (see `smtp_blocked`) and every later check silently downgrades
to MX-only rather than stalling on timeouts.

The same SMTP channel powers `guess_addresses`: for a provider whose practice
domain is known but whose address was never published, the standard mailbox
patterns (first.last@, flast@, first@) are generated and *verified* before any
of them is recorded. A guess the mail server confirms is not a guess any more;
one it rejects is discarded rather than exported as a maybe.
"""

import os
import re
import json
import time
import random
import string
import socket
import smtplib
import logging
import threading

try:  # optional: MX lookups degrade to an A-record check without it
    import dns.resolver
    _HAVE_DNS = True
except ImportError:  # pragma: no cover - depends on the install
    _HAVE_DNS = False


# --- statuses -------------------------------------------------------------
# Ordered best to worst. Exported in the "Email Status" column so a list can be
# sorted by how much each address can be trusted.
VALID = "valid"              # the mail server confirmed this mailbox exists
ACCEPT_ALL = "accept-all"    # domain accepts every address; unconfirmable
ROLE = "role"                # info@/office@ — real, but a shared mailbox
UNKNOWN = "unknown"          # could not check (port 25 blocked, timeout, greylisted)
RISKY = "risky"              # syntactically fine but the domain looks wrong
DISPOSABLE = "disposable"    # throwaway mailbox provider
INVALID = "invalid"          # no mail server, or the server rejected the mailbox

# What each status is worth as a 0-100 confidence number, so the column can be
# sorted numerically and the dashboard can average it.
CONFIDENCE = {
    VALID: 95,
    ACCEPT_ALL: 60,
    ROLE: 70,
    UNKNOWN: 40,
    RISKY: 25,
    DISPOSABLE: 5,
    INVALID: 0,
}

# Shown in the UI legend and in the README.
STATUS_HELP = {
    VALID: "Mail server confirmed this mailbox exists.",
    ACCEPT_ALL: "Domain accepts all addresses, so the mailbox cannot be confirmed.",
    ROLE: "Shared mailbox (info@, office@) - deliverable, but not one person.",
    UNKNOWN: "Could not be checked (port 25 blocked, timeout, or greylisting).",
    RISKY: "Syntactically valid but the domain looks unreliable.",
    DISPOSABLE: "Throwaway/disposable mailbox provider.",
    INVALID: "No mail server, or the server rejected this mailbox.",
}

# Statuses worth putting in front of a salesperson. Anything else is noise at
# best and a bounce at worst.
SENDABLE = frozenset({VALID, ACCEPT_ALL, ROLE, UNKNOWN})


class EmailVerifier:
    """Verifies addresses and infers likely ones. Safe to share across threads.

    Per-domain results (MX records, catch-all behaviour, whether the host even
    answers) are cached for the life of the object, so verifying 400 addresses
    across 120 practice domains costs 120 DNS lookups rather than 400, and one
    SMTP session per domain rather than one per address.
    """

    EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

    # Shared mailboxes. Deliverable and worth keeping — for an RCM pitch,
    # office@ or billing@ often reaches the decision maker faster than a
    # clinician's personal box — but flagged so they can be sorted apart.
    ROLE_LOCAL_PARTS = frozenset({
        "info", "contact", "office", "admin", "administrator", "hello", "help",
        "support", "billing", "accounts", "accounting", "frontdesk", "front-desk",
        "reception", "receptionist", "intake", "scheduling", "appointments",
        "referrals", "records", "team", "staff", "clinic", "practice", "mail",
        "enquiries", "inquiries", "general", "hr", "jobs", "careers", "sales",
    })

    # Addresses that exist to be thrown away — never worth putting in a list.
    DISPOSABLE_DOMAINS = frozenset({
        "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
        "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
        "sharklasers.com", "getnada.com", "dispostable.com", "fakeinbox.com",
        "maildrop.cc", "mintemail.com", "spamgourmet.com", "mailnesia.com",
        "tempinbox.com", "emailondeck.com", "moakt.com", "mohmal.com",
    })

    # Big providers that always answer "250 OK" to RCPT regardless of whether
    # the mailbox exists, so an SMTP probe against them is meaningless. Skip
    # the probe and report accept-all rather than pretending to have verified.
    NO_PROBE_DOMAINS = frozenset({
        "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "aol.com",
        "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com",
        "me.com", "mac.com", "protonmail.com", "proton.me",
    })

    # Mailbox patterns practices actually use, most common first. Verified
    # before use — see guess_addresses.
    NAME_PATTERNS = (
        "{first}.{last}", "{first}", "{f}{last}", "{first}{last}",
        "{first}_{last}", "{last}.{first}", "{first}{l}", "{last}",
    )

    # Public resolvers to fall back to when the system one will not answer MX
    # queries. Plenty of consumer routers proxy DNS badly: they resolve A
    # records fine (so the browser works and nothing looks wrong) but time out
    # on MX, which would silently reduce every verification to "unknown". Both
    # of these are free, unauthenticated and answer MX correctly.
    PUBLIC_RESOLVERS = (
        ["1.1.1.1", "1.0.0.1"],   # Cloudflare
        ["8.8.8.8", "8.8.4.4"],   # Google
        ["9.9.9.9"],              # Quad9
    )

    SMTP_PORT = 25
    DNS_TIMEOUT = 3.0
    SMTP_TIMEOUT = 8.0
    # Consecutive connection failures before concluding the network blocks
    # outbound port 25 entirely and giving up on SMTP probing for this session.
    BLOCKED_AFTER_FAILURES = 4

    # How long to wait for the port-25 preflight. A reachable mail server
    # accepts a connection in well under a second even from the far side of
    # the world; a blocked port never answers at all, so waiting longer only
    # lengthens the pause before a run on a network that blocks it.
    PREFLIGHT_TIMEOUT = 5.0

    # What the network allows is a property of the network, not of one
    # verifier. Every run used to build a fresh verifier and rediscover it — a
    # 3-second DNS probe plus an 8-second port-25 timeout, 11 seconds of dead
    # air before any work, on a connection where the answer never changes. It
    # is now remembered for the process and on disk, re-checked after
    # NETWORK_STATE_TTL, and forgotten whenever Health Check runs.
    NETWORK_STATE_TTL = 2 * 60 * 60
    NETWORK_CACHE_PATH = os.path.join(".cache", "network.json")
    _network_lock = threading.Lock()
    _network_state = None    # loaded lazily from NETWORK_CACHE_PATH

    @staticmethod
    def _load_network_state():
        # Always EmailVerifier, never cls: assigning through a subclass would
        # give that subclass its own copy and split the shared memory in two.
        with EmailVerifier._network_lock:
            if EmailVerifier._network_state is None:
                state = {}
                try:
                    with open(EmailVerifier.NETWORK_CACHE_PATH, "r",
                              encoding="utf-8") as handle:
                        loaded = json.load(handle)
                    if isinstance(loaded, dict):
                        state = loaded
                except (OSError, ValueError):
                    pass
                EmailVerifier._network_state = state
            return EmailVerifier._network_state

    @staticmethod
    def _remembered(key):
        """A remembered fact about the network, or None if unknown or stale."""
        entry = EmailVerifier._load_network_state().get(key)
        if not isinstance(entry, dict):
            return None
        try:
            age = time.time() - float(entry.get("checked_at", 0))
        except (TypeError, ValueError):
            return None
        if age < 0 or age > EmailVerifier.NETWORK_STATE_TTL:
            return None
        return entry

    @staticmethod
    def _remember(key, **values):
        EmailVerifier._load_network_state()
        with EmailVerifier._network_lock:
            values["checked_at"] = time.time()
            EmailVerifier._network_state[key] = values
            snapshot = dict(EmailVerifier._network_state)
        try:
            folder = os.path.dirname(EmailVerifier.NETWORK_CACHE_PATH)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(EmailVerifier.NETWORK_CACHE_PATH, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle)
        except OSError:
            pass  # still remembered in memory for this session

    @staticmethod
    def forget_network_state():
        """Make the next verifier check DNS and port 25 from scratch.

        Health Check calls this, so connecting a VPN takes effect the moment
        you check rather than two hours later.
        """
        with EmailVerifier._network_lock:
            EmailVerifier._network_state = {}
        try:
            os.remove(EmailVerifier.NETWORK_CACHE_PATH)
        except OSError:
            pass

    def __init__(self, logger=None, probe_smtp=True, probe_sender="",
                 helo_name="mail.example.com"):
        """probe_sender is the address used in MAIL FROM. Empty means the null
        sender <>, which is what a bounce uses and what a verification probe is
        supposed to use. If you send outreach from a real mailbox, putting that
        address here makes the probe look like exactly what it is and raises
        the number of servers that answer honestly."""
        self.logger = logger or logging.getLogger(__name__)
        self.probe_smtp = probe_smtp
        self.probe_sender = probe_sender
        self.helo_name = helo_name

        self._lock = threading.Lock()
        self._mx_cache = {}         # domain -> [host, ...] (empty list = no mail)
        self._catchall_cache = {}   # domain -> True/False/None(unknown)
        self._dead_hosts = set()    # MX hosts that refused or timed out
        self._connect_failures = 0
        self.smtp_blocked = False   # set once port 25 looks unreachable
        self._warned_blocked = False
        self._warned_no_dns = False
        self._resolver = None       # the resolver that actually answers MX
        self._resolver_checked = False
        self._preflight_done = False

    # ------------------------------------------------------------------ API

    def verify(self, email, person_name=None):
        """Verify one address. Returns {status, confidence, detail}.

        person_name is only used to sharpen the RISKY check — a freemail box
        that carries no part of the provider's name is flagged, because that is
        the shape of the mis-attributions this project has hit before (a
        stranger's gmail scraped off a page that merely mentioned the
        provider).
        """
        email = (email or "").strip().lower()
        if not email:
            return self._result(INVALID, "No address given.")
        if not self.EMAIL_RE.match(email):
            return self._result(INVALID, "Not a syntactically valid address.")

        local, _, domain = email.partition("@")

        if domain in self.DISPOSABLE_DOMAINS:
            return self._result(DISPOSABLE, domain + " is a disposable mailbox provider.")

        is_role = (local in self.ROLE_LOCAL_PARTS
                   or local.split("+")[0] in self.ROLE_LOCAL_PARTS)

        # Checked before the MX lookup: these domains certainly have working
        # mail servers, so the query would only cost time. A personal mailbox
        # at a big freemail provider that has nothing to do with the provider's
        # name is the classic mis-attribution, and is what gets flagged here.
        if domain in self.NO_PROBE_DOMAINS:
            if person_name and not self._name_matches_local(local, person_name):
                return self._result(
                    RISKY,
                    "{0} mailbox that does not carry any part of '{1}' — may "
                    "belong to someone else.".format(domain, person_name))
            return self._result(
                ACCEPT_ALL,
                domain + " accepts mail for any address, so the mailbox itself "
                "cannot be confirmed remotely.")

        mx_hosts = self._mx_for(domain)
        if mx_hosts is None:
            return self._result(UNKNOWN, "Could not look up the domain's mail servers.")
        if not mx_hosts:
            return self._result(
                INVALID,
                domain + " publishes no mail server, so it cannot receive email.")

        if not self.probe_smtp or self.smtp_blocked:
            reason = ("SMTP probing is off." if not self.probe_smtp
                      else "Outbound port 25 is blocked on this network.")
            status = ROLE if is_role else UNKNOWN
            return self._result(
                status, domain + " has a working mail server. " + reason)

        catch_all = self._is_catch_all(domain, mx_hosts)
        if catch_all is None:
            status = ROLE if is_role else UNKNOWN
            return self._result(
                status,
                domain + " has a mail server but it would not answer a probe.")
        if catch_all:
            return self._result(
                ACCEPT_ALL,
                domain + " accepts every address it is offered, so this mailbox "
                "cannot be confirmed.")

        accepted = self._rcpt_accepted(domain, mx_hosts, email)
        if accepted is None:
            status = ROLE if is_role else UNKNOWN
            return self._result(status, "The mail server gave no clear answer.")
        if not accepted:
            return self._result(
                INVALID, "The mail server for " + domain + " rejected this mailbox.")
        if is_role:
            return self._result(
                ROLE, "Confirmed by the mail server; shared/role mailbox.")
        return self._result(VALID, "Confirmed by the mail server.")

    def guess_addresses(self, first_name, last_name, domain, max_candidates=6):
        """Infer this person's likely address at their practice domain, and
        return only the ones the mail server actually confirms.

        This is what turns a provider with a website but no published address
        into a usable lead. It is deliberately conservative: if the domain is a
        catch-all, or port 25 is blocked, or the server will not talk, it
        returns nothing rather than exporting a plausible-looking invention.
        """
        first = re.sub(r'[^a-z]', '', (first_name or "").lower())
        last = re.sub(r'[^a-z]', '', (last_name or "").lower())
        domain = (domain or "").lower().strip()
        if not domain or not (first or last):
            return []
        if domain in self.NO_PROBE_DOMAINS or domain in self.DISPOSABLE_DOMAINS:
            return []  # unverifiable by definition — guessing would be fiction
        if not self.probe_smtp or self.smtp_blocked:
            return []

        mx_hosts = self._mx_for(domain)
        if not mx_hosts:
            return []
        if self._is_catch_all(domain, mx_hosts) is not False:
            # Unknown or catch-all: every guess would "succeed", which tells us
            # nothing at all.
            return []

        locals_to_try = []
        for pattern in self.NAME_PATTERNS[:max_candidates]:
            local = pattern.format(first=first, last=last,
                                   f=first[:1], l=last[:1]).strip("._ ")
            if not local or "{" in local or local in locals_to_try:
                continue
            locals_to_try.append(local)

        confirmed = []
        for local in locals_to_try:
            candidate = local + "@" + domain
            if self._rcpt_accepted(domain, mx_hosts, candidate):
                self.logger.info(
                    "Inferred and confirmed " + candidate +
                    " from the practice's own mail server.")
                confirmed.append(candidate)
                break  # one confirmed mailbox per person is enough
        return confirmed

    # ------------------------------------------------------- internals: DNS

    def _mx_for(self, domain):
        """Mail exchangers for a domain, best-priority first. [] means the
        domain cannot receive mail; None means the lookup itself failed."""
        with self._lock:
            if domain in self._mx_cache:
                return self._mx_cache[domain]

        hosts = self._lookup_mx(domain)

        with self._lock:
            self._mx_cache[domain] = hosts
        return hosts

    def _pick_resolver(self):
        """Finds a resolver that will actually answer MX queries, once.

        The system resolver is tried first, then the public ones. Whichever
        answers is reused for the rest of the session, so a router that times
        out on MX costs one 3-second probe rather than three seconds on every
        single domain.
        """
        if self._resolver_checked:
            return self._resolver
        with self._lock:
            if self._resolver_checked:
                return self._resolver
            self._resolver_checked = True

            # A resolver that answered last time almost certainly still does,
            # and probing the system one first costs a full 3-second timeout on
            # a router that will not answer MX. Only successes are remembered:
            # a failure may just be a moment without internet, and remembering
            # it would quietly disable verification for hours afterwards.
            remembered = self._remembered("resolver")
            if remembered is not None:
                resolver = dns.resolver.Resolver()
                if remembered.get("nameservers"):
                    resolver.nameservers = list(remembered["nameservers"])
                resolver.timeout = self.DNS_TIMEOUT
                resolver.lifetime = self.DNS_TIMEOUT
                self._resolver = resolver
                return resolver

            candidates = [(None, "the system resolver")]
            candidates += [(ns, "/".join(ns)) for ns in self.PUBLIC_RESOLVERS]
            for nameservers, label in candidates:
                try:
                    resolver = dns.resolver.Resolver()
                    if nameservers:
                        resolver.nameservers = list(nameservers)
                    resolver.timeout = self.DNS_TIMEOUT
                    resolver.lifetime = self.DNS_TIMEOUT
                    # Probe with a domain that certainly has MX records.
                    resolver.resolve("gmail.com", "MX")
                except Exception as e:
                    self.logger.debug(
                        "Resolver " + label + " will not answer MX: " + str(e))
                    continue
                if nameservers:
                    self.logger.info(
                        "This network's DNS server does not answer MX queries, "
                        "so email verification is using " + label + " instead.")
                self._remember("resolver",
                               nameservers=list(nameservers) if nameservers else None)
                self._resolver = resolver
                return resolver

            self.logger.warning(
                "No DNS resolver on this machine would answer an MX query. "
                "Email verification falls back to a plain hostname check, so "
                "addresses will be reported as 'unknown' rather than 'valid'.")
            return None

    def _lookup_mx(self, domain):
        resolver = self._pick_resolver() if _HAVE_DNS else None
        if resolver is not None:
            try:
                answers = resolver.resolve(domain, "MX")
                records = sorted(answers, key=lambda r: r.preference)
                return [str(r.exchange).rstrip(".") for r in records]
            except dns.resolver.NXDOMAIN:
                return []          # the domain itself does not exist
            except dns.resolver.NoAnswer:
                pass               # no MX — fall through to the A-record rule
            except Exception as e:
                self.logger.debug("MX lookup for " + domain + " failed: " + str(e))
                return None
        elif not _HAVE_DNS and not self._warned_no_dns:
            self._warned_no_dns = True
            self.logger.info(
                "dnspython is not installed, so mail servers are checked by "
                "hostname only. Run: pip install dnspython — it makes "
                "verification considerably more accurate.")

        # No MX record: RFC 5321 says fall back to the domain's A record.
        try:
            socket.setdefaulttimeout(self.DNS_TIMEOUT)
            socket.gethostbyname(domain)
            return [domain]
        except OSError:
            return []
        except Exception:
            return None

    # ------------------------------------------------------ internals: SMTP

    def _is_catch_all(self, domain, mx_hosts):
        """True if the domain accepts any address at all. None if unknown."""
        with self._lock:
            if domain in self._catchall_cache:
                return self._catchall_cache[domain]

        nonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=14))
        result = self._rcpt_accepted(domain, mx_hosts, nonce + "@" + domain)

        with self._lock:
            self._catchall_cache[domain] = result
        return result

    def _rcpt_accepted(self, domain, mx_hosts, email):
        """Ask the domain's mail server whether it would accept mail for this
        address. True/False, or None when the server gives no usable answer.

        The session is closed after RCPT TO — DATA is never sent, so no message
        is ever delivered or queued.
        """
        if not self._smtp_reachable():
            return None

        for host in mx_hosts[:2]:  # primary, then one backup
            if host in self._dead_hosts:
                continue
            try:
                server = smtplib.SMTP(timeout=self.SMTP_TIMEOUT,
                                      local_hostname=self.helo_name)
                server.connect(host, self.SMTP_PORT)
            except (OSError, smtplib.SMTPException) as e:
                self._note_connect_failure(host, e)
                continue

            try:
                server.ehlo_or_helo_if_needed()
                server.mail(self.probe_sender)
                code, _ = server.rcpt(email)
                self._note_connect_success()
                if code in (250, 251):
                    return True
                if code in (550, 551, 553, 554):
                    return False
                # 450/451/452 greylisting, 421 too busy, 452 over quota —
                # all "ask again later", none of them an answer.
                return None
            except smtplib.SMTPServerDisconnected:
                return None
            except smtplib.SMTPException as e:
                self.logger.debug(
                    "SMTP probe to " + host + " for " + email + " failed: " + str(e))
                return None
            finally:
                try:
                    server.quit()
                except Exception:
                    try:
                        server.close()
                    except Exception:
                        pass
        return None

    # A mail server that is always up, used purely to find out whether this
    # network lets anything out on port 25 at all.
    PREFLIGHT_HOST = "gmail-smtp-in.l.google.com"

    def _smtp_reachable(self):
        """One-shot check that outbound port 25 works, before any real probe.

        Most home and office ISPs block port 25 outright. Discovering that
        through failed probes costs one full timeout per mail server — around
        30 seconds of dead air at the start of a verification run, repeated
        every session. One connection to a known-good server answers the same
        question once, in a few seconds.
        """
        if self.smtp_blocked:
            return False
        if self._preflight_done:
            return True

        with self._lock:
            if self._preflight_done:
                return not self.smtp_blocked
            self._preflight_done = True

        # An ISP that blocks port 25 does so as policy; the answer does not
        # change between one run and the next. Both outcomes are remembered —
        # unlike DNS, where only success is — because this is exactly the probe
        # whose failure costs a full timeout to rediscover.
        remembered = self._remembered("smtp")
        if remembered is not None:
            if remembered.get("blocked"):
                with self._lock:
                    self.smtp_blocked = True
                self._warned_blocked = True
                self.logger.info(
                    "Outbound port 25 was blocked on this network when last "
                    "checked, so mailbox-level checks stay off. Run Health Check "
                    "after connecting a VPN to test it again.")
                return False
            return True

        sock = socket.socket()
        sock.settimeout(self.PREFLIGHT_TIMEOUT)
        try:
            sock.connect((self.PREFLIGHT_HOST, self.SMTP_PORT))
            self._remember("smtp", blocked=False)
            return True
        except OSError as e:
            with self._lock:
                self.smtp_blocked = True
            self._remember("smtp", blocked=True)
            self._warned_blocked = True
            self.logger.warning(
                "Outbound port 25 is blocked on this network (" + str(e) + "), "
                "so individual mailboxes cannot be confirmed. Addresses are "
                "still checked for a working mail server and are reported as "
                "'unknown' rather than 'valid'; dead domains are still caught. "
                "Most home and office ISPs block port 25 — a VPN that allows "
                "it, or running from a server, restores full verification.")
            return False
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _note_connect_failure(self, host, exc):
        """A few failures in a row mean the network blocks port 25 outright, in
        which case every further probe is a guaranteed 8-second timeout. Say so
        once and stop trying for the rest of the session."""
        with self._lock:
            self._dead_hosts.add(host)
            self._connect_failures += 1
            hit_limit = (self._connect_failures >= self.BLOCKED_AFTER_FAILURES
                         and not self.smtp_blocked)
            if hit_limit:
                self.smtp_blocked = True
        if hit_limit and not self._warned_blocked:
            self._warned_blocked = True
            self.logger.warning(
                "Outbound port 25 appears to be blocked on this network (" +
                str(exc) + "). Mailbox-level verification is off for this run "
                "— addresses are still checked for a working mail server, and "
                "are reported as 'unknown' rather than 'valid'. Most home and "
                "office ISPs block port 25; a VPN that allows it, or running "
                "from a server, restores full verification.")

    def _note_connect_success(self):
        with self._lock:
            self._connect_failures = 0

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _name_matches_local(local, person_name):
        """Whether a mailbox name carries any part of the person's name."""
        local = re.sub(r'[^a-z]', '', (local or "").lower())
        if not local:
            return False
        for part in re.findall(r'[a-z]{3,}', (person_name or "").lower()):
            if part in local:
                return True
        return False

    @staticmethod
    def _result(status, detail):
        return {"status": status, "confidence": CONFIDENCE[status], "detail": detail}
